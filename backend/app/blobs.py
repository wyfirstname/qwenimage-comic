# -*- coding: utf-8 -*-
"""任务参数大字段外置存储（jobs.params_json 的瘦身层）。

背景（2026-10-04 事故）：
    分镜融合任务把参考图编码成 data URL（base64，单张 1~1.5MB）随 payload 一起
    落到 `jobs.params_json`，一条任务的参数就有 4~5MB。实测本机 jobs 表因此涨到
    **151MB**，最终把 `app.db-wal` 写坏，整个漫画模块报
    `database disk image is malformed`。

做法：
    写库前把 payload 里的「超长字符串」抽成 `data/blobs/<job_id>/<n>` 文件，
    payload 中只留 `@blob:<job_id>/<n>` 引用；执行任务前（run_job 从库里恢复
    参数时）再展开还原。对上层完全透明 —— 引擎拿到的 payload 和以前一模一样。

生命周期：
    blob 只在「任务已入库、尚未执行」这段窗口内需要：
      * 任务结束（成功 / 失败）后由 run_job 调 release(job_id) 删除；
      * 启动时 gc_all() 清掉上次异常退出留下的残渣（此时排队中的任务都会被
        标记为失败，它们的参数不会再被使用）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings

# 超过这个字符数的字符串才外置。正常提示词最长约 3KB，图片 base64 都在 1MB 以上。
MIN_BLOB_LEN = 4096

BLOB_PREFIX = "@blob:"

# job_id 只允许字母数字下划线短横，避免用 token 做路径穿越
_JOB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TOKEN_RE = re.compile(r"^@blob:([A-Za-z0-9_-]{1,64})/(\d+)$")


def _blob_root() -> Path:
    return settings.data_dir_path / "blobs"


def _job_dir(job_id: str) -> Path | None:
    if not job_id or not _JOB_ID_RE.fullmatch(job_id):
        return None
    return _blob_root() / job_id


def pack(payload: dict, job_id: str) -> dict:
    """把 payload 里的超长字符串外置成 blob 文件，返回新的 payload。

    原 payload 不被修改；写盘失败时退化返回原值（宁可数据库大一点，也不能丢参数）。
    """
    job_dir = _job_dir(job_id)
    if job_dir is None:
        return payload
    counter = [0]
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - 防御性
        print(f"[blobs] 目录创建失败，参数按原样入库：{exc}")
        return payload
    return _pack_value(payload, job_dir, counter)


def _pack_value(value: Any, job_dir: Path, counter: list[int]) -> Any:
    if isinstance(value, str):
        if len(value) < MIN_BLOB_LEN:
            return value
        index = counter[0]
        counter[0] += 1
        if _write_blob(job_dir / str(index), value):
            return f"{BLOB_PREFIX}{job_dir.name}/{index}"
        return value
    if isinstance(value, list):
        return [_pack_value(v, job_dir, counter) for v in value]
    if isinstance(value, dict):
        return {k: _pack_value(v, job_dir, counter) for k, v in value.items()}
    return value


def _write_blob(path: Path, value: str) -> bool:
    """原子写：先写临时文件再 rename，避免读到半截内容。"""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        os.replace(tmp, path)
        return True
    except OSError as exc:  # pragma: no cover - 防御性
        print(f"[blobs] 写入失败（参数按原样入库）：{exc}")
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def unpack(payload: Any) -> Any:
    """把 `@blob:` 引用还原成原始字符串（返回新对象）。

    blob 文件缺失时保留 token 原样返回，让解码环节给出明确报错，
    而不是静默换成空图。
    """
    if isinstance(payload, str):
        m = _TOKEN_RE.match(payload)
        if not m:
            return payload
        path = _job_dir(m.group(1))
        if path is None:
            return payload
        blob = path / m.group(2)
        try:
            return blob.read_text(encoding="utf-8")
        except OSError:
            print(f"[blobs] 引用丢失：{payload}")
            return payload
    if isinstance(payload, list):
        return [unpack(v) for v in payload]
    if isinstance(payload, dict):
        return {k: unpack(v) for k, v in payload.items()}
    return payload


def dumps(payload: dict, job_id: str) -> str:
    """入库用：外置大字段后序列化。"""
    return json.dumps(pack(payload, job_id), ensure_ascii=False)


def loads(text: str) -> dict:
    """出库用：反序列化并展开大字段。"""
    return unpack(json.loads(text))


def release(job_id: str) -> None:
    """任务结束后删除该任务的 blob 目录。"""
    job_dir = _job_dir(job_id)
    if job_dir is None or not job_dir.exists():
        return
    try:
        shutil.rmtree(job_dir, ignore_errors=True)
    except OSError:  # pragma: no cover - 防御性
        pass


def gc_all() -> int:
    """启动时清理：进程刚起来，没有任何任务在执行，blob 全部是残渣。"""
    root = _blob_root()
    if not root.exists():
        return 0
    removed = 0
    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        else:
            try:
                child.unlink()
                removed += 1
            except OSError:
                pass
    return removed
