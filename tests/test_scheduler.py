# -*- coding: utf-8 -*-
"""调度层：日历感知 / 暂停留痕 / 漏打留痕 / 今日计划。"""
import sys
import shutil
import tempfile
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bellapp.storage import Database      # noqa: E402
from bellapp.player import AudioPlayer    # noqa: E402
from bellapp.scheduler import Scheduler   # noqa: E402


def _db():
    d = Path(tempfile.mkdtemp(prefix="bellsched_"))
    return Database(d / "bell.db"), d


def _row(stype="daily", tstr="08:00:00", wd="", ds="", name="t1", sid=1):
    return {"id": sid, "name": name, "schedule_type": stype, "time_str": tstr,
            "week_days": wd, "date_str": ds, "enabled": 1,
            "audio_file": "x.mp3", "volume": 80, "play_count": 1}


def test_holiday_skips_whole_day():
    db, d = _db()
    try:
        sch = Scheduler(db, AudioPlayer())
        now = datetime.datetime(2026, 11, 3, 7, 0, 0)      # 周二
        sch._calendar = {}
        assert sch._calculate_next(now, [_row()])[0] == datetime.datetime(2026, 11, 3, 8, 0)
        sch._calendar = {"2026-11-03": {"kind": "holiday", "mirror_weekday": -1, "note": ""}}
        assert sch._calculate_next(now, [_row()])[0] == datetime.datetime(2026, 11, 4, 8, 0)
    finally:
        db.close(); shutil.rmtree(str(d), ignore_errors=True)


def test_workday_mirror_weekday():
    """周六补周一：mirror_weekday=0 时按周一课表触发"""
    db, d = _db()
    try:
        sch = Scheduler(db, AudioPlayer())
        now = datetime.datetime(2026, 11, 6, 7, 0, 0)      # 周五
        rows = [_row("weekly", "08:00:00", "0")]
        sch._calendar = {}
        assert sch._calculate_next(now, rows)[0] == datetime.datetime(2026, 11, 9, 8, 0)
        sch._calendar = {"2026-11-07": {"kind": "workday", "mirror_weekday": 0, "note": ""}}
        assert sch._calculate_next(now, rows)[0] == datetime.datetime(2026, 11, 7, 8, 0)
    finally:
        db.close(); shutil.rmtree(str(d), ignore_errors=True)


def test_holiday_skips_date_task():
    db, d = _db()
    try:
        sch = Scheduler(db, AudioPlayer())
        now = datetime.datetime(2026, 11, 3, 7, 0, 0)
        sch._calendar = {"2026-11-03": {"kind": "holiday", "mirror_weekday": -1, "note": ""}}
        assert sch._calculate_next(now, [_row("once", "08:00:00", "", "2026-11-03")])[0] is None
    finally:
        db.close(); shutil.rmtree(str(d), ignore_errors=True)


def test_pause_logs_paused_instead_of_swallowing():
    db, d = _db()
    try:
        sch = Scheduler(db, AudioPlayer())
        sid = db.add_schedule({"name": "暂停任务", "schedule_type": "daily",
                               "time_str": "08:00:00", "week_days": "", "date_str": "",
                               "audio_file": "x.mp3", "volume": 80, "play_count": 1,
                               "enabled": 1, "profile_id": db.get_active_profile_id()})
        hit = []
        sch._execute_task = lambda task: hit.append(task["name"])
        sch.pause()
        sch._handle_due(db.get_schedule(sid), 0.2)
        assert hit == []
        logs = db.get_logs(5)
        assert logs[0]["status"] == "paused" and "暂停" in logs[0]["error_message"]
    finally:
        db.close(); shutil.rmtree(str(d), ignore_errors=True)


def test_missed_and_within_tolerance():
    db, d = _db()
    try:
        sch = Scheduler(db, AudioPlayer())
        sch._tolerance = 60
        sid = db.add_schedule({"name": "任务", "schedule_type": "daily",
                               "time_str": "08:00:00", "week_days": "", "date_str": "",
                               "audio_file": "x.mp3", "volume": 80, "play_count": 1,
                               "enabled": 1, "profile_id": db.get_active_profile_id()})
        row = db.get_schedule(sid)
        hit = []
        sch._execute_task = lambda task: hit.append(task["name"])
        sch._handle_due(row, 3.0)                       # 容差内 → 执行
        assert hit == ["任务"]
        sch._handle_due(row, 300.0)                     # 超容差 → 记漏打
        assert db.get_logs(1)[0]["status"] == "missed"
    finally:
        db.close(); shutil.rmtree(str(d), ignore_errors=True)


def test_once_task_deleted_only_on_success():
    db, d = _db()
    try:
        sid = db.add_schedule({"name": "一次性", "schedule_type": "once",
                               "time_str": "08:00:00", "week_days": "",
                               "date_str": "2099-01-01", "audio_file": "x.mp3",
                               "volume": 80, "play_count": 1, "enabled": 1,
                               "profile_id": db.get_active_profile_id()})
        row = db.get_schedule(sid)

        class Player:
            def __init__(self, ok):
                self.ok = ok

            def play_async(self, path, volume=80, count=1, callback=None):
                callback(self.ok)

        Scheduler(db, Player(False))._execute_task(row)
        assert db.get_schedule(sid) is not None          # 失败保留
        Scheduler(db, Player(True))._execute_task(row)
        assert db.get_schedule(sid) is None              # 成功删除
    finally:
        db.close(); shutil.rmtree(str(d), ignore_errors=True)


def test_today_plan_respects_calendar():
    db, d = _db()
    try:
        sch = Scheduler(db, AudioPlayer())
        later = (datetime.datetime.now() + datetime.timedelta(minutes=30)).strftime("%H:%M:%S")
        db.add_schedule({"name": "稍后", "schedule_type": "daily", "time_str": later,
                         "week_days": "", "date_str": "", "audio_file": "x.mp3",
                         "volume": 80, "play_count": 1, "enabled": 1,
                         "profile_id": db.get_active_profile_id()})
        plan = sch.get_today_plan()
        assert len(plan) == 1 and plan[0][1] == "稍后"
        db.set_calendar_day(datetime.date.today().isoformat(), "holiday", -1, "放假")
        assert sch.get_today_plan() == []
    finally:
        db.close(); shutil.rmtree(str(d), ignore_errors=True)
