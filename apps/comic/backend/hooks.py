# -*- coding: utf-8 -*-
"""漫画工作室对「出图完成」的响应（引擎回调注册）。

原本这段回写逻辑写在 engine/generation.py 里，导致引擎反向依赖漫画应用。
现在改成由漫画应用主动向引擎注册回调：引擎只负责出图，出完通知业务侧，
谁关心谁注册——engine 层因此完全不认识 apps。
"""

from __future__ import annotations

from engine.sdk import engine

from apps.comic.backend import service as comic_service


def _on_image(payload: dict, result: dict, ok: bool) -> None:
    """出图成功/失败时把结果回写到分镜 / 场景 / 角色记录。"""
    panel_id = (payload or {}).get("comic_panel_id")
    scene_id = (payload or {}).get("comic_scene_id")
    character_id = (payload or {}).get("comic_character_id")
    if not (panel_id or scene_id or character_id):
        return

    if ok:
        if panel_id:
            comic_service.on_panel_image(panel_id, result)
        if scene_id:
            comic_service.on_scene_image(scene_id, result)
        if character_id:
            comic_service.on_character_image(character_id, result)
    else:
        message = (result or {}).get("error", "出图失败")
        if panel_id:
            comic_service.on_panel_error(panel_id, message)
        if scene_id:
            comic_service.on_scene_error(scene_id, message)
        if character_id:
            comic_service.on_character_error(character_id, message)


engine.register_job_hook(_on_image)
