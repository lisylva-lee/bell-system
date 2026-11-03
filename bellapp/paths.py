# -*- coding: utf-8 -*-
"""路径与运行环境（V2.3：支持只读介质回退到 %APPDATA%）"""
import os
import sys
import logging
from pathlib import Path

logger = logging.getLogger("BellSystem.Paths")

# Windows 下子进程（ffmpeg 等）不弹出黑色控制台窗口
SUBPROCESS_FLAGS = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

APP_DIR = Path(__file__).parent.parent.resolve()
# 重要：onefile 打包后 __file__ 指向临时解压目录(_MEIxxxxx)，数据会随程序退出丢失。
# 必须用 exe 所在目录作数据根目录（bellapp/ 的父目录），保证配置/数据库/日志持久化。
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent

DATA_DIR = APP_DIR / "data"
LOG_DIR = APP_DIR / "logs"
CONFIG_DIR = APP_DIR / "config"
FFMPEG_DIR = APP_DIR / "ffmpeg"
DB_PATH = DATA_DIR / "bell.db"
CONFIG_PATH = CONFIG_DIR / "settings.json"
LOG_FILE = LOG_DIR / "bell.log"


def _ensure_dirs(dirs):
    """创建数据目录；只读介质（U 盘写保护 / 共享盘无权限）时回退到 %APPDATA%"""
    global DATA_DIR, LOG_DIR, CONFIG_DIR, FFMPEG_DIR, DB_PATH, CONFIG_PATH, LOG_FILE
    try:
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return
    except OSError as e:
        logger.warning("数据目录不可写（%s），回退到用户目录", e)

    base = Path(os.environ.get("APPDATA") or Path.home()) / "BellSystem"
    DATA_DIR = base / "data"
    LOG_DIR = base / "logs"
    CONFIG_DIR = base / "config"
    FFMPEG_DIR = APP_DIR / "ffmpeg"
    for d in (DATA_DIR, LOG_DIR, CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    DB_PATH = DATA_DIR / "bell.db"
    CONFIG_PATH = CONFIG_DIR / "settings.json"
    LOG_FILE = LOG_DIR / "bell.log"


_ensure_dirs([DATA_DIR, LOG_DIR, CONFIG_DIR, FFMPEG_DIR])
