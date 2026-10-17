# -*- coding: utf-8 -*-
"""漫画工作室应用。

自包含：自己的数据表（schema）、业务服务（service）、AI 任务（ai）、
出图回调（hooks）。彼此零外部依赖——只通过 shared 与 engine.sdk 使用平台能力。

前两行 import 是「注册副作用」：把漫画的表结构注册进 shared.db，
把出图回调注册进 engine.sdk。任何漫画子模块被导入时都会执行。
"""

from apps.comic.backend import schema as _schema  # noqa: F401
from apps.comic.backend import hooks as _hooks    # noqa: F401


def startup() -> None:
    """平台/独立进程启动时的漫画侧整理（幂等，失败绝不影响启动）。"""
    from apps.comic.backend import repo as crepo
    from apps.comic.backend import service

    try:
        # 历史出图按项目归位 + 绝对路径改相对（方便整目录迁移）
        service.migrate_legacy_outputs()
        service.normalize_stored_paths()
    except Exception as exc:  # pragma: no cover - 防御性
        print(f"[comic] 历史出图归位跳过：{exc}")

    try:
        # 上次中断遗留的「排队中」分镜 / 场景 / 角色：收敛为可重试状态
        fixed = sum(service.reconcile_project(p["id"]) for p in crepo.list_projects())
        if fixed:
            print(f"[comic] 已收敛上次中断遗留的漫画状态 {fixed} 处")
    except Exception as exc:  # pragma: no cover - 防御性
        print(f"[comic] 状态收敛跳过：{exc}")
