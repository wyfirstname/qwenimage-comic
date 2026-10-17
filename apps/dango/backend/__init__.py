# -*- coding: utf-8 -*-
"""团子衣橱后端包。

门户启动时会调用这里的 `startup()`（如果存在），用于把资料库文件准备好。
"""

from __future__ import annotations


def startup() -> None:
    from apps.dango.backend import store

    store.ensure_ready()
