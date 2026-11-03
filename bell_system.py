#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""🔔 智能打铃系统 BellSystem — 程序入口

v2.3 起实现已按层拆分到 `bellapp/` 包；本文件保留两个用途：
  1) CI 的打包命令 `pyinstaller ... bell_system.py` 无需改动；
  2) 兼容旧的 `import bell_system` 用法（下方重新导出全部公开名字）。
"""
import logging

from bellapp.app import main
from bellapp.paths import (APP_DIR, DATA_DIR, LOG_DIR, CONFIG_DIR, FFMPEG_DIR,
                           DB_PATH, CONFIG_PATH, LOG_FILE, SUBPROCESS_FLAGS)
from bellapp.settings import DEFAULT_SETTINGS, load_settings, save_settings
from bellapp.storage import Database
from bellapp.player import AudioPlayer
from bellapp.scheduler import Scheduler
from bellapp.csvio import (CSV_HEADER, TYPE_TO_CN, CN_TO_TYPE, _parse_week_days,
                           export_schedules_csv, import_schedules_csv,
                           build_schedule_payload)
from bellapp.power import set_auto_start, set_prevent_sleep
from bellapp.logsetup import setup_logging
from bellapp.util import _row_get
from bellapp.ui.main_window import BellApp
from bellapp.ui.dialogs import (ScheduleDialog, ProfileDialog, LogDialog,
                                CalendarDialog, ScheduleIODialog, SettingsDialog)

logger = logging.getLogger("BellSystem")

if __name__ == "__main__":
    main()
