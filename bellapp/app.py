# -*- coding: utf-8 -*-
"""应用装配入口：日志 → 存储 → 播放 → 调度 → 界面 → 退出清理"""
import logging

from . import __version__
from .paths import DB_PATH
from .logsetup import setup_logging
from .settings import load_settings
from .storage import Database
from .player import AudioPlayer
from .scheduler import Scheduler
from .power import set_prevent_sleep
from .ui.main_window import BellApp


def main():
    logger = setup_logging()
    logger.info("=" * 50)
    logger.info("🔔 智能打铃系统启动 (v%s)", __version__)

    db = Database(DB_PATH)
    try:
        removed = db.purge_logs()
        if removed:
            logger.info("清理历史执行日志 %d 条", removed)
    except Exception as e:
        logger.warning("清理历史日志失败: %s", e)

    player = AudioPlayer()
    scheduler = Scheduler(db, player)
    scheduler.start()

    if load_settings().get("prevent_sleep"):
        set_prevent_sleep(True)

    app = BellApp(db, player, scheduler)
    try:
        app.run()
    finally:
        # V2.3: 统一清理链（托盘"退出"也走这里，不再 os._exit 硬杀）
        try:
            app._stop_web_server()
        except Exception:
            pass
        scheduler.stop()
        try:
            set_prevent_sleep(False)      # 恢复系统默认休眠策略
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass
        logger.info("程序退出")


if __name__ == "__main__":
    main()
