# -*- coding: utf-8 -*-
"""任务与图片的数据库访问层。"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from app import blobs
from app.db import db
from app.models_util import now_iso


# ============================================================
#  Jobs
# ============================================================
def create_job(
    job_id: str,
    prompt: str,
    negative: str,
    params: dict[str, Any],
    seed: int,
    count: int,
    mode: str,
    status: str = "queued",
) -> None:
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, status, mode, prompt, negative, params_json,
                              seed, count, progress, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            # 参考图等大字段外置成 blob 文件，绝不把 MB 级 base64 写进数据库
            (job_id, status, mode, prompt, negative,
             blobs.dumps(params, job_id), seed, count,
             "排队中" if status == "queued" else "运行中", now_iso()),
        )


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.write() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def mark_job_running(job_id: str) -> None:
    update_job(job_id, status="running", started_at=now_iso(), message="运行中", progress=1)


def mark_job_progress(job_id: str, progress: int, message: str = "") -> None:
    update_job(job_id, progress=max(0, min(100, int(progress))), message=message)


def mark_job_done(job_id: str, status: str = "succeeded", error: str | None = None) -> None:
    update_job(
        job_id,
        status=status,
        progress=100 if status == "succeeded" else 100,
        finished_at=now_iso(),
        error=error,
        message={"succeeded": "已完成", "failed": "失败", "canceled": "已取消"}.get(status, status),
    )


def get_job(job_id: str) -> Optional[dict]:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(limit: int = 20, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM jobs"
    args: list[Any] = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with db.read() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def count_active_jobs() -> int:
    with db.read() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE status IN ('queued', 'running')"
        ).fetchone()
    return int(row["c"]) if row else 0


def delete_job(job_id: str) -> None:
    with db.write() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


# ============================================================
#  Images
# ============================================================
def insert_image(record: dict[str, Any]) -> None:
    keys = (
        "id", "job_id", "path", "url", "width", "height", "steps", "guidance",
        "seed", "prompt", "negative", "mode", "favorite", "flagged",
        "elapsed_ms", "created_at",
    )
    values = [record.get(k) for k in keys]
    placeholders = ", ".join("?" for _ in keys)
    with db.write() as conn:
        conn.execute(
            f"INSERT INTO images ({', '.join(keys)}) VALUES ({placeholders})", values
        )


def get_image(image_id: str) -> Optional[dict]:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    return dict(row) if row else None


def images_by_job(job_id: str) -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM images WHERE job_id = ? ORDER BY created_at ASC", (job_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def query_gallery(
    page: int = 1,
    page_size: int = 24,
    keyword: str = "",
    favorite_only: bool = False,
    job_id: str | None = None,
) -> tuple[int, list[dict]]:
    where: list[str] = []
    args: list[Any] = []
    if keyword:
        where.append("prompt LIKE ?")
        args.append(f"%{keyword}%")
    if favorite_only:
        where.append("favorite = 1")
    if job_id:
        where.append("job_id = ?")
        args.append(job_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    with db.read() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM images{clause}", args).fetchone()["c"]
        offset = max(0, (page - 1) * page_size)
        rows = conn.execute(
            f"SELECT * FROM images{clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*args, page_size, offset),
        ).fetchall()
    return int(total), [dict(r) for r in rows]


def set_favorite(image_id: str, favorite: bool) -> None:
    with db.write() as conn:
        conn.execute(
            "UPDATE images SET favorite = ? WHERE id = ?", (1 if favorite else 0, image_id)
        )


def delete_image(image_id: str) -> Optional[dict]:
    with db.write() as conn:
        row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
    return dict(row)


def all_image_paths() -> Iterable[str]:
    with db.read() as conn:
        rows = conn.execute("SELECT path FROM images").fetchall()
    return [r["path"] for r in rows]


def gallery_stats() -> dict[str, Any]:
    with db.read() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM images").fetchone()["c"]
        fav = conn.execute("SELECT COUNT(*) AS c FROM images WHERE favorite = 1").fetchone()["c"]
        jobs = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
    return {"images": int(total), "favorites": int(fav), "jobs": int(jobs)}
