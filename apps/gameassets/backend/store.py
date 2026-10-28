# -*- coding: utf-8 -*-
"""素材集存储：data/gameassets/sets.json，一个文件管全部素材集。

体量评估：一个素材集 = 元信息 + 基准图记录 + 十几张素材记录（各 ~1KB），
几十个集也才几百 KB，JSON 文件足够，不进 SQLite。
图片本体存 data/images/gameassets/（走引擎 output_subdir），记录里存 path/url。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from shared.config import settings
from shared.models_util import new_id

_LOCK = threading.Lock()

# 八个子阶段（顺序即依赖顺序）。策划→基准图→六类素材，各自独立状态。
STAGES: list[tuple[str, str]] = [
    ("plan", "自动策划"),
    ("master", "基准图"),
    ("scene", "场景"),
    ("character", "角色"),
    ("pet", "宠物"),
    ("weapon", "武器"),
    ("building", "建筑"),
    ("monster", "野怪"),
]
STAGE_IDS = [k for k, _ in STAGES]
STAGE_LABELS = dict(STAGES)


def _empty_stage() -> dict:
    return {"status": "idle", "done": 0, "total": 0, "error": "", "items": []}


def _empty_pipeline() -> dict:
    return {"stages": {k: _empty_stage() for k in STAGE_IDS}, "log": []}


def _file() -> Path:
    return settings.data_dir_path / "gameassets" / "sets.json"


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


now_iso = _now  # 对外别名：素材记录的时间戳与内部保持同一口径


def _normalize(data: dict) -> dict:
    """把任意历史版本的 pipeline 结构归一到 stages+log（内存内，写库时持久化）。"""
    from .presets import ASSET_KINDS, build_stage_items

    for s in data.get("sets", []):
        p = s.get("pipeline") or {}
        if not isinstance(p.get("stages"), dict):
            old_status = p.get("status")
            stages = {k: _empty_stage() for k in STAGE_IDS}
            if s.get("plan"):
                stages["plan"]["status"] = "done"
                # 旧数据没展开过条目表——补上，让每类能看到要出几张、可单独生成
                items = build_stage_items(s["plan"])
                for kind in ASSET_KINDS:
                    stages[kind]["items"] = items[kind]
                    stages[kind]["total"] = len(items[kind])
            if s.get("master"):
                stages["master"]["status"] = "done"
            if old_status in ("planning", "running") and not s.get("plan"):
                stages["plan"] = {**_empty_stage(), "status": "failed",
                                  "error": "服务中断，策划未完成（可重新生成）"}
            s["pipeline"] = {"stages": stages,
                             "log": [{"ts": _now(), "level": "warn",
                                      "msg": "检测到旧版进度数据，已重置为分类模式（已有素材保留）"}]}
        else:
            for k in STAGE_IDS:  # 补齐缺的阶段/字段
                st = p["stages"].setdefault(k, _empty_stage())
                st.setdefault("items", [])
                st.setdefault("done", 0)
                st.setdefault("total", 0)
                st.setdefault("error", "")
                st.setdefault("status", "idle")
            # 条目表为空但有方案 → 补展开（历史遗留数据）
            if s.get("plan"):
                items = build_stage_items(
                    s["plan"], {k: p["stages"][k]["items"] for k in ASSET_KINDS})
                for kind in ASSET_KINDS:
                    st = p["stages"][kind]
                    if not st["items"]:
                        st["items"] = items[kind]
                        st["total"] = len(items[kind])
            # 注意：这里不翻 queued/running——正常运行的阶段也会被并发读到。
            # "服务被杀留下假 running" 的清理只在启动时做一次，见 recover_interrupted()。
        p.setdefault("log", [])
    return data


def ensure_item_ids() -> None:
    """仅服务启动时调用：给缺 id 的条目补稳定 id 并落盘。

    条目 id 是方案编辑对账的锚——不能每次读盘重生成（内存态不落盘，
    并发读会各得一套 id），所以这里用 mutate 真正写下去。
    """
    from .presets import ASSET_KINDS, build_stage_items
    for s in list_sets():
        need = any(not it.get("id")
                   for kind in ASSET_KINDS
                   for it in s["pipeline"]["stages"][kind]["items"])
        if s.get("plan") and need:
            def _apply(t: dict, _s=s) -> None:
                items = build_stage_items(
                    _s["plan"], {k: t["pipeline"]["stages"][k]["items"]
                                 for k in ASSET_KINDS})
                for kind in ASSET_KINDS:
                    t["pipeline"]["stages"][kind]["items"] = items[kind]
                    t["pipeline"]["stages"][kind]["total"] = len(items[kind])
            mutate(s["id"], _apply)
            print(f"[gameassets] 条目 id 已补齐：{s['id']}")
        elif need:  # 没有方案的旧集：只补 id 字段，不动结构
            def _patch(t: dict) -> None:
                from shared.models_util import new_id as _nid
                for kind in ASSET_KINDS:
                    for it in t["pipeline"]["stages"][kind]["items"]:
                        it.setdefault("id", _nid("itm"))
            mutate(s["id"], _patch)


def recover_interrupted() -> None:
    """仅服务启动时调用一次：此刻不可能有活任务，假 running/queued 一定是上次进程被杀留下的。"""
    for s in list_sets():
        stuck = [k for k, st in s["pipeline"]["stages"].items()
                 if st["status"] in ("queued", "running")]
        if not stuck:
            continue
        def _mark(t: dict, stuck=stuck) -> None:
            for k in stuck:
                t["pipeline"]["stages"][k]["status"] = "failed"
                t["pipeline"]["stages"][k]["error"] = "服务重启，任务中断（可重试）"
        mutate(s["id"], _mark)
        append_log(s["id"], "warn",
                   "检测到上次服务中断的未完成任务：" + "、".join(STAGE_LABELS[k] for k in stuck)
                   + "（可单独重试）")
        print(f"[gameassets] 清理中断任务：{s['id']} {stuck}")


def _read() -> dict:
    f = _file()
    if not f.exists():
        return {"sets": []}
    try:
        return _normalize(json.loads(f.read_text(encoding="utf-8")))
    except Exception as exc:  # pragma: no cover - 防御性
        print(f"[gameassets] 读取 sets.json 失败：{exc}")
        return {"sets": []}


def _write(data: dict) -> None:
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    _tmp = f.with_suffix(".json.tmp")
    _tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    _tmp.replace(f)  # 原子替换，避免写一半崩掉整个库


def make_set(*, name: str, idea: str, style: str, style_name: str, time_preset: str,
             time_name: str, desc: str, threshold: float, scale: str) -> dict:
    from .presets import ANIM_CONFIRM_DEFAULT      # 单一出处，避免两处默认值漂移
    return {
        "id": new_id("gas"),
        "name": name,
        "idea": idea,            # 用户的一句话需求
        "style": style,
        "style_name": style_name,
        "time_preset": time_preset,
        "time_name": time_name,
        "desc": desc,
        "threshold": threshold,
        "scale": scale,
        "anim": {"anim_level": "lite",        # 角色/宠物动画档位（2.5D 45° 帧集）
                 # 人工确认：每个朝向先出 1 张确认帧，通过后才继续（13.2）
                 "anim_confirm": ANIM_CONFIRM_DEFAULT},
        "artdir": None,          # 美术方向契约（八轴，规则推导/用户手填）
        "artdir_edited": False,  # 用户手填过 → 自动推导不再覆盖
        "plan": None,            # 策划方案（plan 阶段产出）
        "master": None,          # 场景基准图 {path,url,seed,palette:[hex]}
        "assets": [],
        "pipeline": _empty_pipeline(),
        "created_at": _now(),
    }


def append_log(sid: str, level: str, msg: str) -> None:
    """操作日志（每类动作/异常都落一条，前端日志面板直接显示）。"""
    from datetime import datetime

    def _apply(t: dict) -> None:
        log = t["pipeline"]["log"]
        log.append({"ts": datetime.now().strftime("%H:%M:%S"),
                    "level": level, "msg": msg[:300]})
        del log[:-200]           # 上限 200 条
    mutate(sid, _apply)


def list_sets() -> list[dict]:
    return _read()["sets"]


def get_set(sid: str) -> Optional[dict]:
    return next((s for s in _read()["sets"] if s["id"] == sid), None)


def mutate(sid: str, fn: Callable[[dict], Any]) -> Optional[dict]:
    """取出→改→存，全程持锁（同一集被并发出图时不至于互相覆盖）。"""
    with _LOCK:
        data = _read()
        target = next((s for s in data["sets"] if s["id"] == sid), None)
        if target is None:
            return None
        fn(target)
        _write(data)
        return target


def add_set(s: dict) -> dict:
    with _LOCK:
        data = _read()
        data["sets"].insert(0, s)
        _write(data)
    return s


def delete_set(sid: str) -> bool:
    with _LOCK:
        data = _read()
        before = len(data["sets"])
        data["sets"] = [s for s in data["sets"] if s["id"] != sid]
        if len(data["sets"]) == before:
            return False
        _write(data)
        return True
