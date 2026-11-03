# -*- coding: utf-8 -*-
"""日志初始化：按天轮转（旧实现把文件名在导入时固定为启动当天，跨天不换、无上限）"""
import sys
import logging
import logging.handlers

from .paths import LOG_DIR


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    handlers = []
    try:
        fh = logging.handlers.TimedRotatingFileHandler(
            str(LOG_DIR / "bell.log"), when="midnight", backupCount=30, encoding="utf-8")
        fh.suffix = "%Y-%m-%d"
        handlers.append(fh)
    except Exception:
        pass
    # --windowed 打包后 sys.stderr 为 None，此时不要加 StreamHandler
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=level,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=handlers, force=True)
    return logging.getLogger("BellSystem")
