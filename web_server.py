#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""兼容壳：Web 控制台实现已移到 `bellapp/web/api.py`

保留本文件是为了让旧的 `from web_server import BellWebServer` 继续可用。
"""
from bellapp.web.api import (  # noqa: F401
    WEB_VERSION, ALLOWED_AUDIO_EXT, MAX_BODY_BYTES, REQUEST_TIMEOUT,
    MAX_CONCURRENT_REQUESTS, AUTH_FAIL_WINDOW, AUTH_FAIL_MAX,
    get_local_urls, validate_audio_path, validate_schedule,
    ApiHandler, BoundedThreadingHTTPServer, BellWebServer,
    _auth_fail_map, _auth_blocked, _auth_fail_add, _as_int,
)
