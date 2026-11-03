# -*- coding: utf-8 -*-
"""界面冒烟：主窗口能建起来、能跑一轮刷新、能正常退出（无显示环境自动跳过）。"""
import sys
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bellapp import paths                     # noqa: E402
from bellapp.storage import Database          # noqa: E402
from bellapp.player import AudioPlayer        # noqa: E402
from bellapp.scheduler import Scheduler       # noqa: E402


def test_main_window_smoke():
    try:
        import tkinter as tk
        probe = tk.Tk()
        probe.destroy()
    except Exception as e:                     # noqa: BLE001
        print("跳过：本机无可用显示（%s）" % e)
        return

    from bellapp.ui.main_window import BellApp

    d = Path(tempfile.mkdtemp(prefix="bellgui_"))
    old_cfg = paths.CONFIG_PATH
    try:
        paths.CONFIG_PATH = d / "settings.json"
        db = Database(d / "bell.db")
        db.add_schedule({"name": "冒烟任务", "schedule_type": "daily", "time_str": "08:00:00",
                         "week_days": "", "date_str": "", "audio_file": "x.mp3",
                         "volume": 80, "play_count": 1, "enabled": 1,
                         "profile_id": db.get_active_profile_id()})
        player = AudioPlayer()
        scheduler = Scheduler(db, player)
        app = BellApp(db, player, scheduler)
        app.root.withdraw()                    # 不闪窗
        for _ in range(3):
            app.root.update()
        # 关键控件都建出来了
        assert app._tree is not None
        assert app._search_var.get() == ""
        assert app._today_label.cget("text")
        assert len(app._tree.get_children()) == 1
        # 搜索过滤可用
        app._search_var.set("冒烟")
        app._refresh_display()
        assert len(app._tree.get_children()) == 1
        app._search_var.set("不存在的任务")
        app._refresh_display()
        assert len(app._tree.get_children()) == 0
        app._quit_app()
        app.root.update()
        app.root.destroy()
        db.close()
    finally:
        paths.CONFIG_PATH = old_cfg
        shutil.rmtree(str(d), ignore_errors=True)
