# -*- coding: utf-8 -*-
"""分镜项目存储。

为什么又是 JSON 而不是 SQLite
----------------------------
和团子衣橱同一个判断：`app.db` 只承载引擎产物（任务 + 出图），业务资产自己管。
分镜项目天然是一整棵文档（分镜列表 + 每格状态 + 渲染结果混在一起改），
关系型反而要拆四张表再 join 回来。几百个项目、单机单用户，JSON 足够。

    data/storyboard/projects.json

原子写（临时文件 + os.replace）+ RLock，覆盖「一边出图一边改状态」的并发写。
分镜出图与渲染都是后台线程在改同一份数据，这把锁不是可选项。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from shared.config import settings
from shared.models_util import new_id


_LOCK = threading.RLock()
MAX_PROJECTS = 200

RENDER_IDLE = "none"
RENDER_RUNNING = "rendering"
RENDER_DONE = "done"
RENDER_FAILED = "failed"


# ============================================================
#  路径与读写
# ============================================================

def data_dir() -> Path:
    return settings.data_dir_path / "storyboard"


def projects_path() -> Path:
    return data_dir() / "projects.json"


def outputs_dir() -> Path:
    return settings.data_dir_path / "outputs" / "storyboard"


def ensure_ready() -> None:
    with _LOCK:
        data_dir().mkdir(parents=True, exist_ok=True)
        outputs_dir().mkdir(parents=True, exist_ok=True)
        if not projects_path().exists():
            _write(_empty())


def _empty() -> dict:
    return {"version": 1, "projects": []}


def _read() -> dict:
    path = projects_path()
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        backup = path.with_suffix(f".broken-{int(time.time())}.json")
        try:
            path.replace(backup)
            print(f"[storyboard] 项目库解析失败（已备份到 {backup.name}）：{exc}")
        except Exception:
            print(f"[storyboard] 项目库解析失败：{exc}")
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("version", 1)
    data.setdefault("projects", [])
    return data


def _write(data: dict) -> None:
    path = projects_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_iso() -> str:
    """给 apps 层用的公开时间戳（_now 是本模块私有的）。"""
    return _now()


# ============================================================
#  结构
# ============================================================

def new_beat(index: int) -> dict:
    return {
        "index": index,
        "beat": "",
        "narration": "",
        "action": "",
        "scene": "",            # 场景描述（可留空，留给画风/动作去交代）
        "shot": "中景",
        "camera": "push_in",
        "duration": 3.0,
        "image_id": "",
        "image_url": "",
        "job_id": "",
        "status": "empty",      # empty / imaging / ready / failed
        "error": "",
        # 旁白（每格一句，时长会反过来决定这一格的画面时长）
        "audio_path": "",
        "audio_url": "",
        "audio_duration": 0.0,
        "audio_status": "none",  # none / recording / ready / failed
        "audio_error": "",
    }


def new_voice() -> dict:
    """整片共用的音色设置（每格只是文本不同）。"""
    return {
        "speaker": "",      # 音色参考音频；留空用 IndexTTS 内置示例音色
        "emo": "",          # 情绪参考音频
        "emo_alpha": 1.0,   # 情绪强度 0~1
        "emo_text": "",     # 用一句话描述情绪（IndexTTS-2.5 才支持）
        "speed": 1.0,       # 语速（仅 2.5 生效）
        "lang": "ZH",       # 语言（仅 2.5 生效）
        "path": "",         # 兼容：整片一条外部配音（没有逐格旁白时才用）
        "backend": "",      # 最近一次用到的后端：indextts / stub
        "status": "none",   # none / running / done / failed
        "error": "",
    }


def new_cast() -> dict:
    """定妆照：整部片的角色身份证，后面每一格都以它为 image_1。"""
    return {"status": "none", "image_id": "", "image_url": "", "job_id": "",
            "prompt": "", "error": ""}


def new_render() -> dict:
    return {"status": RENDER_IDLE, "progress": 0.0, "message": "",
            "path": "", "url": "", "duration": 0.0, "size_mb": 0.0,
            "started_at": "", "finished_at": "", "error": ""}


def audio_dir(pid: str) -> Path:
    return data_dir() / pid / "audio"


def make_project(*, topic: str, style: str, preset: str, tone: str = "",
                 extra: str = "", subject: str = "", total_seconds: float = 24.0,
                 aspect: str = "9:16", width: int = 1080, height: int = 1920,
                 xfade: float = 0.5, use_llm: bool = True) -> dict:
    pid = new_id("sb")
    now = _now()
    return {
        "id": pid,
        "title": "",
        "topic": topic,
        "style": style,
        "preset": preset,
        "tone": tone,
        "extra": extra,
        "subject": subject,
        "lock": "",                 # 角色锁文本（用户可编辑，也可从定妆照回填）
        "aspect": aspect,
        "total_seconds": float(total_seconds),
        "width": int(width),
        "height": int(height),
        "xfade": float(xfade),
        "use_llm": bool(use_llm),
        "cast": new_cast(),
        "beats": [],
        "voice": new_voice(),
        "render": new_render(),
        "created_at": now,
        "updated_at": now,
    }


# ============================================================
#  增删改查
# ============================================================

def list_projects() -> list[dict]:
    with _LOCK:
        items = _read()["projects"]
    items = sorted(items, key=lambda p: p.get("updated_at") or "", reverse=True)
    return items


def get_project(pid: str) -> Optional[dict]:
    with _LOCK:
        for p in _read()["projects"]:
            if p.get("id") == pid:
                return p
    return None


def add_project(project: dict) -> dict:
    with _LOCK:
        data = _read()
        data["projects"].insert(0, project)
        if len(data["projects"]) > MAX_PROJECTS:
            # 只删最老的记录，产物文件留给用户自己清（不擅自动磁盘）
            data["projects"] = data["projects"][:MAX_PROJECTS]
        _write(data)
    return project


def update_project(pid: str, **fields) -> Optional[dict]:
    """浅更新顶层字段。"""
    with _LOCK:
        data = _read()
        for p in data["projects"]:
            if p.get("id") == pid:
                p.update(fields)
                p["updated_at"] = _now()
                _write(data)
                return p
    return None


def mutate(pid: str, fn: Callable[[dict], Any]) -> Optional[dict]:
    """在锁内改项目（用于改嵌套的 beats / cast / render）。

    fn 直接就地修改传入的 project，返回值被忽略。锁内读-改-写，
    避免出图回调与渲染线程互相覆盖。
    """
    with _LOCK:
        data = _read()
        for p in data["projects"]:
            if p.get("id") == pid:
                fn(p)
                p["updated_at"] = _now()
                _write(data)
                return p
    return None


def delete_project(pid: str) -> Optional[dict]:
    with _LOCK:
        data = _read()
        for i, p in enumerate(data["projects"]):
            if p.get("id") == pid:
                data["projects"].pop(i)
                _write(data)
                return p
    return None


def set_beat_image(pid: str, index: int, **fields) -> Optional[dict]:
    """按 1-based 序号更新某一格的状态。"""
    def _fn(p: dict) -> None:
        for b in p.get("beats") or []:
            if int(b.get("index") or 0) == int(index):
                b.update(fields)
                return
    return mutate(pid, _fn)


def set_cast(**fields) -> Callable[[dict], None]:
    def _fn(p: dict) -> None:
        p.setdefault("cast", new_cast()).update(fields)
    return _fn


def stats() -> dict:
    items = list_projects()
    done = sum(1 for p in items if (p.get("render") or {}).get("status") == RENDER_DONE)
    return {"projects": len(items), "rendered": done,
            "images": sum(1 for p in items for b in (p.get("beats") or [])
                          if b.get("status") == "ready")}
