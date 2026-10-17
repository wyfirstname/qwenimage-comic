# -*- coding: utf-8 -*-
"""出图完成 → 自动回填到分镜格 / 定妆照。

为什么用引擎回调而不是让前端来确认
----------------------------------
分镜的工作流是「点一下 → 等出图 → 看结果」，一个格子一张图，没有挑选的余地。
让前端在任务完成后调一次「采纳」接口是多余的一跳，而且用户切走页面后
那格就永远停在 imaging。引擎的 job hook 在出图落库的那一刻回写业务数据，
前端只需要轮询项目详情，看到 status 变成 ready 就行——状态只有一个来源。

为什么 hook 要在 backend/__init__.py 里 import
---------------------------------------------
见 comic 的同名做法：模块被 import 时执行注册，谁挂载谁注册，
engine 层依旧不认识任何 app。
"""

from __future__ import annotations

from engine.sdk import engine

from apps.storyboard.backend import store


def _on_image(payload: dict, result: dict, ok: bool) -> None:
    sb = (payload or {}).get("storyboard") or {}
    pid = sb.get("pid")
    kind = sb.get("kind")
    if not pid or not kind:
        return

    job_id = str((payload or {}).get("job_id") or "")

    if not ok:
        message = str((result or {}).get("error") or "出图失败")
        if kind == "cast":
            store.mutate(pid, store.set_cast(status="failed", error=message,
                                             job_id=job_id))
        elif kind == "shot":
            store.set_beat_image(pid, int(sb.get("index") or 0),
                                 status="failed", error=message, job_id=job_id)
        return

    image_id = str((result or {}).get("id") or "")
    url = str((result or {}).get("url") or "")
    path = str((result or {}).get("path") or "")
    if not image_id:
        return

    if kind == "cast":
        store.mutate(pid, store.set_cast(status="ready", image_id=image_id,
                                         image_url=url, image_path=path,
                                         job_id=job_id, error=""))
    elif kind == "shot":
        store.set_beat_image(pid, int(sb.get("index") or 0),
                             status="ready", image_id=image_id, image_url=url,
                             image_path=path, job_id=job_id, error="")


engine.register_job_hook(_on_image)
