# -*- coding: utf-8 -*-
"""存储层：迁移框架 / 每线程连接 / 日志清理 / 旧库升级。

零依赖设计：不依赖 pytest fixture，可直接 `python tests/run_all.py` 跑，也可 `pytest tests`。
"""
import io
import os
import sys
import json
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bellapp import paths                                    # noqa: E402
from bellapp.storage import Database                         # noqa: E402
from bellapp.settings import load_settings, save_settings, DEFAULT_SETTINGS  # noqa: E402


def _tmp():
    return Path(tempfile.mkdtemp(prefix="belltest_"))


V20_SCHEMA = """
CREATE TABLE schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    schedule_type TEXT NOT NULL DEFAULT 'weekly', time_str TEXT NOT NULL,
    week_days TEXT DEFAULT '', date_str TEXT DEFAULT '',
    audio_file TEXT NOT NULL, volume INTEGER DEFAULT 80, play_count INTEGER DEFAULT 1,
    enabled INTEGER DEFAULT 1, profile_id INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
    is_active INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_id INTEGER, schedule_name TEXT,
    audio_file TEXT, execute_time TEXT, status TEXT DEFAULT 'success',
    error_message TEXT DEFAULT '');
"""


def test_fresh_db_reaches_schema_v3():
    d = _tmp()
    try:
        db = Database(d / "bell.db")
        assert db.schema_version() == Database.SCHEMA_VERSION == 3
        names = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"schedules", "profiles", "logs", "calendar_days"} <= names
        idx = {r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "idx_logs_execute_time" in idx and "idx_logs_schedule_id" in idx
        db.close()
    finally:
        shutil.rmtree(str(d), ignore_errors=True)


def test_legacy_v20_db_upgrades_without_data_loss():
    """v2.0 的库（无 calendar_days、无索引、user_version=0）升级后数据必须一字不差"""
    d = _tmp()
    try:
        raw = sqlite3.connect(str(d / "bell.db"))
        raw.executescript(V20_SCHEMA)
        raw.execute("INSERT INTO profiles (name, is_active) VALUES ('默认方案', 1)")
        raw.execute("INSERT INTO schedules (name, schedule_type, time_str, week_days, date_str,"
                    " audio_file, volume, play_count, enabled, profile_id) VALUES"
                    " ('旧任务', 'weekly', '08:00:00', '0,1,2,3,4', '', 'C:/a.mp3', 70, 2, 1, 1)")
        raw.execute("INSERT INTO logs (schedule_id, schedule_name, audio_file, execute_time,"
                    " status, error_message) VALUES (1, '旧任务', 'C:/a.mp3',"
                    " '2026-01-01 08:00:00', 'success', '')")
        raw.commit()
        raw.close()

        db = Database(d / "bell.db")
        assert db.schema_version() == 3
        rows = db.get_schedules(db.get_active_profile_id())
        assert len(rows) == 1
        assert rows[0]["name"] == "旧任务" and rows[0]["week_days"] == "0,1,2,3,4"
        assert rows[0]["volume"] == 70 and rows[0]["play_count"] == 2
        assert len(db.get_logs(10)) == 1
        assert db.get_calendar() == {}
        db.close()
    finally:
        shutil.rmtree(str(d), ignore_errors=True)


def test_settings_keep_unknown_keys_and_add_defaults():
    """旧 settings.json 不得被改写：自定义键保留、新键自动补默认值"""
    d = _tmp()
    old_cfg = paths.CONFIG_PATH
    try:
        cfg = d / "settings.json"
        io.open(str(cfg), "w", encoding="utf-8").write(json.dumps({
            "auto_start": True, "web_port": 9999, "web_token": "oldtoken",
            "custom_legacy_key": "keep-me"}, ensure_ascii=False))
        paths.CONFIG_PATH = cfg
        s = load_settings()
        assert s["custom_legacy_key"] == "keep-me"
        assert s["web_port"] == 9999 and s["web_token"] == "oldtoken"
        assert s["missed_tolerance_sec"] == DEFAULT_SETTINGS["missed_tolerance_sec"]
        assert s["minimize_to_tray"] is True
        save_settings(s)
        after = json.loads(io.open(str(cfg), encoding="utf-8").read())
        assert after["custom_legacy_key"] == "keep-me" and after["web_port"] == 9999
    finally:
        paths.CONFIG_PATH = old_cfg
        shutil.rmtree(str(d), ignore_errors=True)


def test_each_thread_gets_its_own_connection():
    """每线程一条连接：并发读写不得互相干扰（旧实现全局共用一条连接）"""
    d = _tmp()
    try:
        db = Database(d / "bell.db")
        seen = {}
        errors = []

        def worker(name):
            try:
                seen[name] = id(db.conn)
                for i in range(30):
                    db.add_log(i, "任务%s" % name, "a.mp3", "success", "")
                    assert len(db.get_logs(5)) <= 5
            except Exception as e:            # noqa: BLE001
                errors.append("%s: %s" % (name, e))

        threads = [threading.Thread(target=worker, args=("t%d" % i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        assert len(set(seen.values())) == 4, seen          # 4 条不同连接
        assert len(db.get_logs(500)) == 120
        db.close()
    finally:
        shutil.rmtree(str(d), ignore_errors=True)


def test_purge_logs_by_age_and_count():
    d = _tmp()
    try:
        db = Database(d / "bell.db")
        db.conn.execute("INSERT INTO logs (schedule_id, schedule_name, audio_file, execute_time,"
                        " status, error_message) VALUES (1, 'old', 'a',"
                        " datetime('now','localtime','-200 days'), 'success', '')")
        for _ in range(15):
            db.conn.execute("INSERT INTO logs (schedule_id, schedule_name, audio_file,"
                            " execute_time, status, error_message) VALUES (1, 'new', 'a',"
                            " datetime('now','localtime'), 'success', '')")
        db.conn.commit()
        assert len(db.get_logs(100)) == 16
        removed = db.purge_logs(keep_days=90, keep_rows=10)
        assert removed >= 6, removed                        # 1 条超龄 + 至少 5 条超量
        left = db.get_logs(100)
        assert len(left) <= 10
        assert all(r["schedule_name"] == "new" for r in left)
        db.close()
    finally:
        shutil.rmtree(str(d), ignore_errors=True)
