# -*- coding: utf-8 -*-
"""漫画工作室的数据访问层（项目 / 角色 / 分镜 / 成品页）。"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.db import db
from app.models_util import new_id, now_iso

# ============================================================
#  Projects
# ============================================================


def create_project(
    title: str,
    style: str = "jp_bw",
    layout: str = "grid_2x2",
    width: int = 768,
    height: int = 768,
    steps: int = 8,
    guidance: float = 4.0,
    negative: str = "",
    char_negative: str = "",
    ref_strength: float = 0.55,
    synopsis: str = "",
    keep_style: bool = True,
    use_ref: bool = True,
) -> dict:
    pid = new_id("cmic")
    ts = now_iso()
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO comic_projects (id, title, style, layout, width, height, steps,
                                        guidance, negative, char_negative, ref_strength,
                                        synopsis, keep_style, use_ref,
                                        created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pid, title, style, layout, width, height, steps, guidance, negative,
             char_negative, float(ref_strength),
             synopsis, 1 if keep_style else 0, 1 if use_ref else 0, ts, ts),
        )
    return get_project(pid)  # type: ignore[return-value]


def update_project(project_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.write() as conn:
        conn.execute(f"UPDATE comic_projects SET {cols} WHERE id = ?",
                     (*fields.values(), project_id))


def get_project(project_id: str) -> Optional[dict]:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM comic_projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row) if row else None


def list_projects() -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM comic_panels x WHERE x.project_id = p.id) AS panel_count,
                   (SELECT COUNT(*) FROM comic_panels x WHERE x.project_id = p.id
                     AND x.status = 'done') AS done_count,
                   (SELECT COUNT(*) FROM comic_panels x WHERE x.project_id = p.id
                     AND x.dialogue IS NOT NULL AND TRIM(x.dialogue) != '') AS dialogue_count,
                   (SELECT COUNT(*) FROM comic_characters c WHERE c.project_id = p.id) AS char_count,
                   (SELECT COUNT(*) FROM comic_characters c WHERE c.project_id = p.id
                     AND c.ref_url IS NOT NULL) AS char_ready,
                   (SELECT COUNT(*) FROM comic_scenes s WHERE s.project_id = p.id) AS scene_count,
                   (SELECT COUNT(*) FROM comic_renders r WHERE r.project_id = p.id) AS render_count
            FROM comic_projects p
            ORDER BY p.updated_at DESC
            """
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # 卡片只需要梗概摘要，不要把整篇剧本塞进列表接口
        d["synopsis_brief"] = re.sub(r"\s+", " ", (d.get("synopsis") or "")).strip()[:80]
        d.pop("synopsis", None)
        out.append(d)
    return out


def delete_project(project_id: str) -> None:
    with db.write() as conn:
        conn.execute("DELETE FROM comic_projects WHERE id = ?", (project_id,))


# ============================================================
#  Characters
# ============================================================


def create_character(project_id: str, name: str, role: str = "", appearance: str = "",
                     outfit: str = "", personality: str = "", ref_path: str | None = None,
                     ref_url: str | None = None, seed: int = -1,
                     gender: str = "", age: str = "", background: str = "",
                     detail_prompt: str = "", negative: str = "") -> dict:
    cid = new_id("char")
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO comic_characters (id, project_id, name, role, gender, age, appearance,
                                          outfit, personality, background, detail_prompt,
                                          negative, ref_path, ref_url, seed, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cid, project_id, name, role, gender, age, appearance, outfit, personality,
             background, detail_prompt, negative, ref_path, ref_url, seed, now_iso()),
        )
    return get_character(cid)  # type: ignore[return-value]


def update_character(character_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields.setdefault("updated_at", now_iso())
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.write() as conn:
        conn.execute(f"UPDATE comic_characters SET {cols} WHERE id = ?",
                     (*fields.values(), character_id))


def get_character(character_id: str) -> Optional[dict]:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM comic_characters WHERE id = ?", (character_id,)).fetchone()
    return dict(row) if row else None


def list_characters(project_id: str) -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM comic_characters WHERE project_id = ? ORDER BY created_at ASC",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_character(character_id: str) -> None:
    with db.write() as conn:
        conn.execute("DELETE FROM comic_characters WHERE id = ?", (character_id,))


# ============================================================
#  Scenes（场景设定）
# ============================================================

SCENE_FIELDS = (
    "seq", "name", "location", "time_of_day", "atmosphere", "desc", "prompt",
    "last_prompt", "status", "job_id", "image_id", "image_url", "seed", "error",
)


def create_scene(project_id: str, name: str, **fields: Any) -> dict:
    sid = new_id("scne")
    ts = now_iso()
    data = {k: fields.get(k) for k in SCENE_FIELDS}
    data["name"] = name
    data["last_prompt"] = data.get("last_prompt") or ""
    data["status"] = data.get("status") or "draft"
    data["seq"] = int(data.get("seq") or _next_scene_seq(project_id))
    data["seed"] = int(data.get("seed") if data.get("seed") is not None else -1)
    cols = ["id", "project_id", *SCENE_FIELDS, "created_at", "updated_at"]
    values = [sid, project_id, *[data.get(k) for k in SCENE_FIELDS], ts, ts]
    placeholders = ", ".join("?" for _ in cols)
    with db.write() as conn:
        conn.execute(
            f"INSERT INTO comic_scenes ({', '.join(cols)}) VALUES ({placeholders})", values
        )
    return get_scene(sid)  # type: ignore[return-value]


def update_scene(scene_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.write() as conn:
        conn.execute(f"UPDATE comic_scenes SET {cols} WHERE id = ?",
                     (*fields.values(), scene_id))


def get_scene(scene_id: str) -> Optional[dict]:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM comic_scenes WHERE id = ?", (scene_id,)).fetchone()
    return dict(row) if row else None


def list_scenes(project_id: str) -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM comic_scenes WHERE project_id = ? ORDER BY seq ASC, created_at ASC",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_scene(scene_id: str) -> None:
    with db.write() as conn:
        conn.execute("UPDATE comic_panels SET scene_id = NULL WHERE scene_id = ?", (scene_id,))
        conn.execute("DELETE FROM comic_scenes WHERE id = ?", (scene_id,))


def _next_scene_seq(project_id: str) -> int:
    with db.read() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM comic_scenes WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return int(row["m"]) + 1 if row else 1


def swap_scene_seq(scene_id: str, direction: int) -> None:
    scene = get_scene(scene_id)
    if not scene:
        return
    scenes = list_scenes(scene["project_id"])
    idx = next((i for i, s in enumerate(scenes) if s["id"] == scene_id), None)
    if idx is None:
        return
    target = idx + direction
    if target < 0 or target >= len(scenes):
        return
    a, b = scenes[idx], scenes[target]
    ts = now_iso()
    with db.write() as conn:
        conn.execute("UPDATE comic_scenes SET seq = ?, updated_at = ? WHERE id = ?",
                     (b["seq"], ts, a["id"]))
        conn.execute("UPDATE comic_scenes SET seq = ?, updated_at = ? WHERE id = ?",
                     (a["seq"], ts, b["id"]))


# ============================================================
#  Panels
# ============================================================


PANEL_FIELDS = (
    "seq", "page", "cell", "shot", "scene", "dialogue", "sfx", "scene_id", "character_ids",
    "extra_prompt", "prompt", "status", "job_id", "image_id", "image_url",
    "seed", "error",
)


def create_panel(project_id: str, **fields: Any) -> dict:
    pid = new_id("panel")
    ts = now_iso()
    data = {k: fields.get(k) for k in PANEL_FIELDS}
    data["character_ids"] = json.dumps(data.get("character_ids") or [], ensure_ascii=False)
    data["status"] = data.get("status") or "draft"
    data["seq"] = int(data.get("seq") or 0)
    data["page"] = int(data.get("page") or 1)
    data["cell"] = int(data.get("cell") or 0)
    data["seed"] = int(data.get("seed") if data.get("seed") is not None else -1)
    cols = ["id", "project_id", *PANEL_FIELDS, "created_at", "updated_at"]
    values = [pid, project_id, *[data.get(k) for k in PANEL_FIELDS], ts, ts]
    placeholders = ", ".join("?" for _ in cols)
    with db.write() as conn:
        conn.execute(
            f"INSERT INTO comic_panels ({', '.join(cols)}) VALUES ({placeholders})", values
        )
    return get_panel(pid)  # type: ignore[return-value]


def update_panel(panel_id: str, **fields: Any) -> None:
    if not fields:
        return
    if "character_ids" in fields and not isinstance(fields["character_ids"], str):
        fields["character_ids"] = json.dumps(fields["character_ids"] or [], ensure_ascii=False)
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.write() as conn:
        conn.execute(f"UPDATE comic_panels SET {cols} WHERE id = ?",
                     (*fields.values(), panel_id))


def get_panel(panel_id: str) -> Optional[dict]:
    with db.read() as conn:
        row = conn.execute("SELECT * FROM comic_panels WHERE id = ?", (panel_id,)).fetchone()
    return dict(row) if row else None


def list_panels(project_id: str) -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM comic_panels WHERE project_id = ? ORDER BY seq ASC, created_at ASC",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_panel(panel_id: str) -> None:
    with db.write() as conn:
        conn.execute("DELETE FROM comic_panels WHERE id = ?", (panel_id,))


def next_seq(project_id: str) -> int:
    with db.read() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM comic_panels WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return int(row["m"]) + 1 if row else 1


def renumber(project_id: str, page_size: int = 4) -> None:
    """按 seq 重新分配 page / cell（排版用）。"""
    panels = list_panels(project_id)
    ts = now_iso()
    with db.write() as conn:
        for i, p in enumerate(panels):
            conn.execute(
                "UPDATE comic_panels SET seq = ?, page = ?, cell = ?, updated_at = ? WHERE id = ?",
                (i + 1, i // page_size + 1, i % page_size, ts, p["id"]),
            )


def swap_seq(panel_id: str, direction: int) -> None:
    """与相邻分镜交换顺序（direction: -1 上移 / 1 下移）。"""
    panel = get_panel(panel_id)
    if not panel:
        return
    panels = list_panels(panel["project_id"])
    idx = next((i for i, p in enumerate(panels) if p["id"] == panel_id), None)
    if idx is None:
        return
    target = idx + direction
    if target < 0 or target >= len(panels):
        return
    a, b = panels[idx], panels[target]
    ts = now_iso()
    with db.write() as conn:
        conn.execute("UPDATE comic_panels SET seq = ?, updated_at = ? WHERE id = ?",
                     (b["seq"], ts, a["id"]))
        conn.execute("UPDATE comic_panels SET seq = ?, updated_at = ? WHERE id = ?",
                     (a["seq"], ts, b["id"]))


def project_stats(project_id: str) -> dict:
    with db.read() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done,
                   SUM(CASE WHEN status IN ('queued','running') THEN 1 ELSE 0 END) AS active,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM comic_panels WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "done": int(row["done"] or 0),
        "active": int(row["active"] or 0),
        "failed": int(row["failed"] or 0),
    }


# ============================================================
#  Renders（成品页）
# ============================================================


def create_render(project_id: str, path: str, url: str, layout: str,
                  page: int, width: int, height: int) -> dict:
    rid = new_id("rnd")
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO comic_renders (id, project_id, path, url, layout, page, width, height, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rid, project_id, path, url, layout, page, width, height, now_iso()),
        )
    return {"id": rid, "project_id": project_id, "path": path, "url": url,
            "layout": layout, "page": page, "width": width, "height": height}


def list_renders(project_id: str, limit: int = 30) -> list[dict]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM comic_renders WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def update_render(render_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db.write() as conn:
        conn.execute(f"UPDATE comic_renders SET {cols} WHERE id = ?",
                     (*fields.values(), render_id))


def delete_render(render_id: str) -> None:
    with db.write() as conn:
        conn.execute("DELETE FROM comic_renders WHERE id = ?", (render_id,))
