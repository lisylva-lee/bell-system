# -*- coding: utf-8 -*-
"""SQLite 存储层（V2.3：PRAGMA user_version 迁移 + 每线程连接）"""
import sqlite3
import threading
import datetime
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("BellSystem.Storage")

def _migrate_v1_baseline(db):
    """v1 = v2.0 基线表结构（已由 _init_db 的 IF NOT EXISTS 建好，这里只占位）"""
    return


def _migrate_v2_log_indexes(db):
    """v2 = v2.1 执行日志索引"""
    c = db.conn.cursor()
    c.execute("CREATE INDEX IF NOT EXISTS idx_logs_execute_time ON logs(execute_time)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_logs_schedule_id ON logs(schedule_id)")
    db.conn.commit()


def _migrate_v3_calendar(db):
    """v3 = v2.2 节假日/调休日历表"""
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS calendar_days (
            day TEXT PRIMARY KEY,
            kind TEXT NOT NULL DEFAULT 'holiday',
            mirror_weekday INTEGER DEFAULT -1,
            note TEXT DEFAULT ''
        )
    """)
    db.conn.commit()


# 迁移列表：只做加法（新增表/索引/列），永不删除或重命名既有列
MIGRATIONS = [
    (1, _migrate_v1_baseline),
    (2, _migrate_v2_log_indexes),
    (3, _migrate_v3_calendar),
]


class Database:
    SCHEMA_VERSION = 3

    def __init__(self, db_path: Path):
        self.db_path = db_path
        # 写操作串行化；读操作在 WAL 下可并发
        self.lock = threading.RLock()
        # V2.3: 每个线程一条独立连接（旧实现全局共用一条连接，
        # 隐式事务可能被别的线程 commit 提前提交，且 fetch 结果存在互相干扰风险）
        self._local = threading.local()
        self._conns = []
        self._conns_lock = threading.Lock()
        self._init_db()

    @property
    def conn(self):
        """当前线程的 SQLite 连接（首次访问时创建）"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError:
                pass
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    # ---- 迁移 ----
    def schema_version(self) -> int:
        try:
            row = self.conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.DatabaseError:
            return 0

    def _apply_migrations(self):
        current = self.schema_version()
        for version, func in MIGRATIONS:
            if current < version:
                logger.info("数据库迁移 → v%d (%s)", version, func.__name__)
                func(self)
                self.conn.execute("PRAGMA user_version=%d" % version)
                self.conn.commit()

    # ---- 日志清理 ----
    def purge_logs(self, keep_days: int = 90, keep_rows: int = 10000) -> int:
        """清理过旧/过多的执行日志，返回删除条数（旧实现只增不减）"""
        with self.lock:
            removed = 0
            try:
                cur = self.conn.execute(
                    "DELETE FROM logs WHERE execute_time < datetime('now','localtime',?)",
                    ("-%d days" % int(keep_days),))
                removed += cur.rowcount or 0
                cur = self.conn.execute(
                    "DELETE FROM logs WHERE id NOT IN "
                    "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)", (int(keep_rows),))
                removed += cur.rowcount or 0
                self.conn.commit()
            except sqlite3.DatabaseError as e:
                logger.warning("清理日志失败: %s", e)
            return removed

    def _init_db(self):
        c = self.conn.cursor()
        # 定时任务
        c.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                schedule_type TEXT NOT NULL DEFAULT 'weekly',
                time_str TEXT NOT NULL,
                week_days TEXT DEFAULT '',
                date_str TEXT DEFAULT '',
                audio_file TEXT NOT NULL,
                volume INTEGER DEFAULT 80,
                play_count INTEGER DEFAULT 1,
                enabled INTEGER DEFAULT 1,
                profile_id INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # 铃声方案
        c.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                is_active INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # 执行日志
        c.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER,
                schedule_name TEXT,
                audio_file TEXT,
                execute_time TEXT,
                status TEXT DEFAULT 'success',
                error_message TEXT DEFAULT ''
            )
        """)
        # V2.1: 执行日志按时间倒序查询，加索引避免全表扫描
        c.execute("CREATE INDEX IF NOT EXISTS idx_logs_execute_time ON logs(execute_time)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_logs_schedule_id ON logs(schedule_id)")
        # V2.2: 节假日 / 调休补课日历（纯新增表，不影响既有表结构与数据）
        #   kind='holiday' → 该日全天不打铃
        #   kind='workday' → 该日照常打铃；mirror_weekday 0-6 表示"按星期几的课表"（调休补课），-1 表示按真实星期
        c.execute("""
            CREATE TABLE IF NOT EXISTS calendar_days (
                day TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'holiday',
                mirror_weekday INTEGER DEFAULT -1,
                note TEXT DEFAULT ''
            )
        """)
        # 检查是否有默认方案
        c.execute("SELECT COUNT(*) FROM profiles")
        if c.fetchone()[0] == 0:
            c.execute("INSERT INTO profiles (name, is_active) VALUES (?, ?)", ("默认方案", 1))
        self.conn.commit()
        # V2.3: 基线表建好后按 user_version 顺序补齐后续版本的结构
        self._apply_migrations()

    def get_schedules(self, profile_id: int = 0) -> List[sqlite3.Row]:
        if profile_id > 0:
            return self.conn.execute(
                "SELECT * FROM schedules WHERE profile_id=? OR profile_id=0 ORDER BY time_str",
                (profile_id,)
            ).fetchall()
        return self.conn.execute("SELECT * FROM schedules ORDER BY time_str").fetchall()

    def get_schedule(self, sid: int):
        """按 id 取单个任务（GUI/Web 共用，避免各自写裸 SQL 绕过锁）"""
        return self.conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()

    def add_schedule(self, data: dict) -> int:
        with self.lock:
            return self._add_schedule_impl(data)

    def _add_schedule_impl(self, data: dict) -> int:
        c = self.conn.cursor()
        c.execute("""
            INSERT INTO schedules (name, schedule_type, time_str, week_days, date_str, audio_file, volume, play_count, enabled, profile_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["name"], data["schedule_type"], data["time_str"],
            data.get("week_days", ""), data.get("date_str", ""),
            data["audio_file"], data.get("volume", 80),
            data.get("play_count", 1), data.get("enabled", 1),
            data.get("profile_id", 0)
        ))
        self.conn.commit()
        return c.lastrowid

    def update_schedule(self, sid: int, data: dict):
        with self.lock:
            self._update_schedule_impl(sid, data)

    def _update_schedule_impl(self, sid: int, data: dict):
        self.conn.execute("""
            UPDATE schedules SET name=?, schedule_type=?, time_str=?, week_days=?, date_str=?,
            audio_file=?, volume=?, play_count=?, enabled=?, profile_id=?
            WHERE id=?
        """, (
            data["name"], data["schedule_type"], data["time_str"],
            data.get("week_days", ""), data.get("date_str", ""),
            data["audio_file"], data.get("volume", 80),
            data.get("play_count", 1), data.get("enabled", 1),
            data.get("profile_id", 0), sid
        ))
        self.conn.commit()

    def delete_schedule(self, sid: int):
        with self.lock:
            self.conn.execute("DELETE FROM schedules WHERE id=?", (sid,))
            self.conn.commit()

    def toggle_schedule(self, sid: int, enabled: bool):
        with self.lock:
            self.conn.execute("UPDATE schedules SET enabled=? WHERE id=?", (1 if enabled else 0, sid))
            self.conn.commit()

    # 方案
    def get_profiles(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()

    def add_profile(self, name: str) -> int:
        with self.lock:
            c = self.conn.cursor()
            c.execute("INSERT INTO profiles (name) VALUES (?)", (name,))
            self.conn.commit()
            return c.lastrowid

    def delete_profile(self, pid: int):
        with self.lock:
            self.conn.execute("DELETE FROM profiles WHERE id=?", (pid,))
            self.conn.execute("UPDATE schedules SET profile_id=0 WHERE profile_id=?", (pid,))
            self.conn.commit()

    def activate_profile(self, pid: int):
        with self.lock:
            self.conn.execute("UPDATE profiles SET is_active=0")
            self.conn.execute("UPDATE profiles SET is_active=1 WHERE id=?", (pid,))
            self.conn.commit()

    def get_active_profile_id(self) -> int:
        r = self.conn.execute("SELECT id FROM profiles WHERE is_active=1").fetchone()
        return r["id"] if r else 0

    # 日志
    def add_log(self, schedule_id: int, name: str, audio: str, status: str = "success", error: str = ""):
        with self.lock:
            self.conn.execute("""
                INSERT INTO logs (schedule_id, schedule_name, audio_file, execute_time, status, error_message)
                VALUES (?, ?, ?, datetime('now','localtime'), ?, ?)
            """, (schedule_id, name, audio, status, error))
            self.conn.commit()

    def get_logs(self, limit: int = 100) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM logs ORDER BY execute_time DESC LIMIT ?", (limit,)
        ).fetchall()

    # ---- V2.2 日历 ----
    def get_calendar(self) -> dict:
        """返回 {YYYY-MM-DD: {kind, mirror_weekday, note}}；空表返回 {}"""
        try:
            rows = self.conn.execute(
                "SELECT day, kind, mirror_weekday, note FROM calendar_days").fetchall()
        except sqlite3.DatabaseError:
            return {}
        return {
            r["day"]: {
                "kind": r["kind"] or "holiday",
                "mirror_weekday": int(r["mirror_weekday"] if r["mirror_weekday"] is not None else -1),
                "note": r["note"] or "",
            }
            for r in rows
        }

    def set_calendar_day(self, day: str, kind: str, mirror_weekday: int = -1, note: str = ""):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO calendar_days (day, kind, mirror_weekday, note) VALUES (?,?,?,?)",
                (day, kind, int(mirror_weekday), note))
            self.conn.commit()

    def delete_calendar_day(self, day: str):
        with self.lock:
            self.conn.execute("DELETE FROM calendar_days WHERE day=?", (day,))
            self.conn.commit()

    # ---- V2.2 告警 ----
    def get_recent_alerts(self, hours: int = 24, limit: int = 5) -> List[sqlite3.Row]:
        """最近 N 小时内"失败 / 漏打"的执行记录（供失败告警用）"""
        since = (datetime.datetime.now() - datetime.timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        return self.conn.execute(
            "SELECT * FROM logs WHERE status IN ('failed','missed') AND execute_time >= ? "
            "ORDER BY execute_time DESC LIMIT ?", (since, limit)).fetchall()

    def close(self):
        """关闭本线程与其他线程创建的所有连接"""
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()
