# -*- coding: utf-8 -*-
"""ComfyUI 本地 HTTP 访问 helper。

为什么不用 urllib.request.urlopen：urllib 默认读取 HTTP_PROXY/HTTPS_PROXY 环境变量，
把发往 127.0.0.1:8188 的请求也交给系统代理。一旦代理异常（断网、代理进程退出），
后端就会误判「ComfyUI 未运行」甚至无法出图。ComfyUI 地址几乎总是本机回环地址，
对回环地址发请求永远不应该走代理，这里统一用空 ProxyHandler 的 opener。
"""

from __future__ import annotations

import urllib.request
from urllib.parse import urlparse

# 不走任何代理的 opener（用于本机回环地址）
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "::1", "localhost")


def open(url, *, timeout: float = 30, **kwargs):
    """请求 ComfyUI；回环地址强制直连，其它地址走默认代理规则。

    `url` 既可以是 URL 字符串（可带 data/method 关键字），也可以是现成的
    urllib.request.Request 对象。
    """
    req = url if isinstance(url, urllib.request.Request) else urllib.request.Request(url, **kwargs)
    if is_loopback(req.full_url):
        return _NO_PROXY_OPENER.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)
