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


def _file() -> Path:
    return settings.data_dir_path / "gameassets" / "sets.json"


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


now_iso = _now  # 对外别名：素材记录的时间戳与内部保持同一口径


def _read() -> dict:
    f = _file()
    if not f.exists():
        return {"sets": []}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
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
        "plan": None,            # 策划方案（流水线阶段 1 产出）
        "master": None,          # 场景基准图 {path,url,seed,palette:[hex]}
        "assets": [],
        "pipeline": {"status": "idle", "steps": [], "total": 0, "done": 0,
                     "started_at": None, "finished_at": None, "error": ""},
        "created_at": _now(),
    }


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
