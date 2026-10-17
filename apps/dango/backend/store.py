# -*- coding: utf-8 -*-
"""团子衣橱的本地资料库（团子 / 衣柜）。

为什么不用 SQLite
-----------------
平台已有的 `app.db` 只承载「任务 + 出图」两种记录，它们是引擎的产物，
结构由 shared 层统一管理。团子与衣服属于**业务资产**，结构会长（版本历史、
槽位、标签、锚点……），塞进 shared 的库就要让 shared 反过来认识 apps 的概念，
违反分层。所以这里用一份独立的 JSON 文档：

    data/dango/library.json

单机、单用户、数据量在几百条量级，读写都是毫秒级；代价只是「不能并发写」，
用 RLock + 临时文件原子替换（os.replace）解决。真到几万条那天再换 SQLite，
只要保持本模块的函数签名不变，routes 一行都不用改。

设计要点
--------
* **团子是资产，不是 prompt**：每个团子固定保存一张「素体」图（base）与
  当前造型（current），后面所有换装都从这两张图出发，identity 才锁得住。
* **历史不丢**：每次换装结果 push 进 history（上限 30），用户可以随时回到
  上一版；这也是「脱下衣服」最省事的实现——回到上一步或回到素体即可。
* **衣服跨团子复用**：衣柜是全局的，衣服记录只存图与槽位，不绑团子。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from shared.config import settings
from shared.models_util import new_id


MAX_HISTORY = 30


# ============================================================
#  路径与读写
# ============================================================

_LOCK = threading.RLock()


def library_dir() -> Path:
    return settings.data_dir_path / "dango"


def library_path() -> Path:
    return library_dir() / "library.json"


def _empty() -> dict:
    return {"version": 1, "characters": [], "outfits": []}


def ensure_ready() -> None:
    """确保目录与资料库文件存在（门户启动时调用）。"""
    with _LOCK:
        library_dir().mkdir(parents=True, exist_ok=True)
        if not library_path().exists():
            _write(_empty())


def _read() -> dict:
    path = library_path()
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # 坏文件不该让整个应用挂掉
        backup = path.with_suffix(f".broken-{int(time.time())}.json")
        try:
            path.replace(backup)
            print(f"[dango] 资料库解析失败（已备份到 {backup.name}）：{exc}")
        except Exception:
            print(f"[dango] 资料库解析失败：{exc}")
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("version", 1)
    data.setdefault("characters", [])
    data.setdefault("outfits", [])
    return data


def _write(data: dict) -> None:
    """原子写：先落临时文件再 os.replace，避免半截 JSON 把资料库写坏。"""
    path = library_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
#  「形象」记录的规范化（团子的 base / current / history 共用）
# ============================================================

_IMAGE_KEYS = ("image_id", "url", "path", "width", "height", "seed", "prompt",
               "outfit_id", "outfit_name", "slot", "note")


def make_look(*, image_id: str, url: str, path: str = "", width: int = 0,
              height: int = 0, seed: int = -1, prompt: str = "",
              outfit_id: str = "", outfit_name: str = "", slot: str = "",
              note: str = "") -> dict:
    """一张「形象图」的完整描述。换装结果、素体、历史版本都用这个结构。"""
    return {
        "image_id": image_id, "url": url, "path": path,
        "width": width, "height": height, "seed": seed, "prompt": prompt,
        "outfit_id": outfit_id, "outfit_name": outfit_name, "slot": slot,
        "note": note, "created_at": _now(),
    }


def _clean_look(look: dict | None) -> Optional[dict]:
    if not look or not isinstance(look, dict):
        return None
    out = {k: look.get(k) for k in _IMAGE_KEYS}
    out["created_at"] = look.get("created_at") or _now()
    return out


# ============================================================
#  团子
# ============================================================

def list_characters() -> list[dict]:
    with _LOCK:
        return list(_read()["characters"])


def get_character(cid: str) -> Optional[dict]:
    cid = (cid or "").strip()
    if not cid:
        return None
    with _LOCK:
        for c in _read()["characters"]:
            if c.get("id") == cid:
                return c
    return None


def add_character(*, name: str, base: dict, params: dict) -> dict:
    """新建团子：素体（base）同时作为初始的当前造型（current）。"""
    record = {
        "id": new_id("dan"),
        "name": (name or "").strip() or "无名团子",
        "created_at": _now(),
        "updated_at": _now(),
        "params": dict(params or {}),
        "base": _clean_look(base),
        "current": _clean_look(base),
        "history": [],
        "worn": {},          # slot -> outfit_id，记录「现在穿着什么」
    }
    with _LOCK:
        data = _read()
        data["characters"].insert(0, record)
        _write(data)
    return record


def update_character(cid: str, **fields: Any) -> Optional[dict]:
    """改名字 / 备忘等轻量字段。"""
    with _LOCK:
        data = _read()
        for c in data["characters"]:
            if c.get("id") == cid:
                for key in ("name", "notes"):
                    if key in fields and fields[key] is not None:
                        c[key] = str(fields[key]).strip()
                c["updated_at"] = _now()
                _write(data)
                return c
    return None


def adopt_look(cid: str, look: dict) -> Optional[dict]:
    """把一张新图设为团子的当前造型，旧造型进历史。

    `worn` 同时更新：换上某个槽位的衣服就记一笔，回到历史版本就整体覆盖。
    """
    with _LOCK:
        data = _read()
        for c in data["characters"]:
            if c.get("id") != cid:
                continue
            clean = _clean_look(look)
            if not clean:
                return None
            old = c.get("current")
            if old:
                c.setdefault("history", []).insert(0, old)
                del c["history"][MAX_HISTORY:]
            c["current"] = clean
            c["updated_at"] = _now()

            slot = (clean.get("slot") or "").strip()
            if slot:
                if slot == "full":
                    # 整套换装视为重置换衣状态
                    c["worn"] = {"full": clean.get("outfit_id")} if clean.get("outfit_id") else {}
                elif clean.get("outfit_id"):
                    c.setdefault("worn", {})[slot] = clean["outfit_id"]
            _write(data)
            return c
    return None


def restore_look(cid: str, index: int = 0) -> Optional[dict]:
    """回到历史造型。index=0 是上一版；index=-1 表示回到素体。"""
    with _LOCK:
        data = _read()
        for c in data["characters"]:
            if c.get("id") != cid:
                continue
            if index < 0:
                base = c.get("base")
                if not base:
                    return None
                c.setdefault("history", []).insert(0, c.get("current"))
                del c["history"][MAX_HISTORY:]
                c["current"] = base
                c["worn"] = {}
                c["updated_at"] = _now()
                _write(data)
                return c
            history = c.get("history") or []
            if not history:
                return None
            idx = max(0, min(int(index), len(history) - 1))
            prev = history.pop(idx)
            c.setdefault("history", []).insert(0, c.get("current"))
            del c["history"][MAX_HISTORY:]
            c["current"] = prev
            c["updated_at"] = _now()
            _write(data)
            return c
    return None


def delete_character(cid: str) -> bool:
    with _LOCK:
        data = _read()
        before = len(data["characters"])
        data["characters"] = [c for c in data["characters"] if c.get("id") != cid]
        if len(data["characters"]) == before:
            return False
        _write(data)
        return True


# ============================================================
#  衣柜
# ============================================================

def list_outfits() -> list[dict]:
    with _LOCK:
        return list(_read()["outfits"])


def get_outfit(fid: str) -> Optional[dict]:
    fid = (fid or "").strip()
    if not fid:
        return None
    with _LOCK:
        for o in _read()["outfits"]:
            if o.get("id") == fid:
                return o
    return None


def add_outfit(*, name: str, slot: str, image: dict, prompt: str = "",
               tags: list[str] | None = None, params: dict | None = None) -> dict:
    record = {
        "id": new_id("fit"),
        "name": (name or "").strip() or "未命名单品",
        "slot": slot or "full",
        "created_at": _now(),
        "image": _clean_look(image),
        "prompt": prompt or "",
        "tags": [t for t in (tags or []) if t][:8],
        "params": dict(params or {}),
    }
    with _LOCK:
        data = _read()
        data["outfits"].insert(0, record)
        _write(data)
    return record


def update_outfit(fid: str, **fields: Any) -> Optional[dict]:
    with _LOCK:
        data = _read()
        for o in data["outfits"]:
            if o.get("id") != fid:
                continue
            if fields.get("name"):
                o["name"] = str(fields["name"]).strip()
            if fields.get("slot"):
                o["slot"] = str(fields["slot"]).strip()
            if fields.get("tags") is not None:
                o["tags"] = [str(t) for t in fields["tags"]][:8]
            _write(data)
            return o
    return None


def delete_outfit(fid: str) -> bool:
    with _LOCK:
        data = _read()
        before = len(data["outfits"])
        data["outfits"] = [o for o in data["outfits"] if o.get("id") != fid]
        if len(data["outfits"]) == before:
            return False
        _write(data)
        return True


def stats() -> dict:
    with _LOCK:
        data = _read()
    return {"characters": len(data["characters"]), "outfits": len(data["outfits"])}
