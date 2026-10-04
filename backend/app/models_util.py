# -*- coding: utf-8 -*-
"""数据层通用工具：ID 生成、时间、文件落盘。"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image

from app.config import settings

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def prompt_slug(prompt: str, max_len: int = 32) -> str:
    """从提示词生成文件名片段，超长或非 ASCII 时回退为 hash。"""
    text = (prompt or "").strip().lower()
    ascii_only = text.encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_RE.sub("-", ascii_only).strip("-")
    if len(slug) < 3:
        return hashlib.md5((prompt or "").encode("utf-8")).hexdigest()[:8]
    return slug[:max_len].strip("-")


def today_dir() -> Path:
    d = settings.output_dir_path / datetime.now().strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_subdir_path(subdir: str | None) -> Path:
    """出图落盘目录：传了 subdir 就用它（按项目归档），否则退回按日期分目录。

    subdir 由服务层给出（形如 `comics/雨夜天台_1a2b3c`），这里只做安全校验，
    绝不允许跳出 output_dir（防 `..` / 绝对路径 / 盘符）。
    """
    base = settings.output_dir_path
    if not subdir:
        return today_dir()
    rel = Path(str(subdir).replace("\\", "/").strip("/"))
    if rel.is_absolute() or any(part in ("..", "") for part in rel.parts):
        return today_dir()
    target = (base / rel).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return today_dir()
    target.mkdir(parents=True, exist_ok=True)
    return target


def build_output_path(prompt: str, seed: int, ext: str = "png",
                      subdir: str | None = None) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"{ts}_{seed}_{prompt_slug(prompt)}.{ext}"
    return output_subdir_path(subdir) / fname


def relative_url(path: Path) -> str:
    """转换为可通过静态文件服务访问的 URL。"""
    try:
        rel = path.resolve().relative_to(settings.project_root)
        return "/" + rel.as_posix()
    except ValueError:
        return path.as_posix()


def stored_path(path: Path | str) -> str:
    """数据库里存的文件路径：项目根下统一存 `data/...` 相对 posix 路径。

    存相对路径是为了数据可以整目录迁移（换电脑 / 换盘符不改库）；
    读取端用 `_resolve_stored_path` / `resolve_stored()` 还原成绝对路径。
    项目根之外的路径（异常情况）退回绝对 posix，保证仍可读。
    """
    p = Path(path)
    try:
        return p.resolve().relative_to(settings.project_root).as_posix()
    except (ValueError, OSError):
        return p.as_posix()


def resolve_stored(value: str | Path | None) -> Path | None:
    """把库里存的路径（相对 `data/...` 或绝对）还原成本地绝对路径。"""
    if not value:
        return None
    s = str(value).strip().replace("\\", "/")
    if not s:
        return None
    if s.startswith("/"):
        return settings.project_root / s.lstrip("/")
    p = Path(s)
    if p.is_absolute():
        return p
    if s.startswith("data/") or s.startswith("./"):
        return settings.project_root / s
    return settings.project_root / s if (settings.project_root / s).exists() else p


def save_image(img: Image.Image, prompt: str, seed: int,
               subdir: str | None = None) -> Path:
    path = build_output_path(prompt, seed, subdir=subdir)
    img.save(path, format="PNG", optimize=True)
    return path


def decode_base64_image(data: str) -> Image.Image:
    """解析前端传来的 base64 图片（支持 data URL 前缀）。"""
    if "," in data and data.strip().lower().startswith("data:"):
        data = data.split(",", 1)[1]
    raw = base64.b64decode(data)
    return Image.open(io.BytesIO(raw)).convert("RGB")


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"
