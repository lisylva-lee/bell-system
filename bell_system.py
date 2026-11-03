#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
🔔 智能打铃系统 BellSystem
Windows 7/10/11 通用
基于 Python + tkinter + pygame + SQLite
"""

import os
import sys
import re
import json
import time
import hashlib
import sqlite3
import threading
import datetime
import subprocess
import logging
import queue
from pathlib import Path
from typing import Optional, List, Tuple

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import tkinter.font as tkFont

# 第三方库
try:
    import pygame
except ImportError:
    pygame = None

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None

import webbrowser
# V2.0 Web 远程控制（纯标准库实现，见 web_server.py）
from web_server import BellWebServer

# Windows 下子进程（ffmpeg 转码 / powercfg 等）不弹出黑色控制台窗口
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _row_get(row, key, default=None):
    """兼容 sqlite3.Row / dict 的取值助手"""
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value

# ============================================================
# 路径配置
# ============================================================
APP_DIR = Path(__file__).parent.resolve()
# 重要：onefile 打包后 __file__ 指向临时解压目录(_MEIxxxxx)，数据会随程序退出丢失。
# 必须用 exe 所在目录作数据根目录，保证配置/数据库/日志持久化（绿色免安装版）。
if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).resolve().parent
DATA_DIR = APP_DIR / "data"
LOG_DIR = APP_DIR / "logs"
CONFIG_DIR = APP_DIR / "config"
FFMPEG_DIR = APP_DIR / "ffmpeg"

for d in [DATA_DIR, LOG_DIR, CONFIG_DIR, FFMPEG_DIR]:
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "bell.db"
CONFIG_PATH = CONFIG_DIR / "settings.json"
LOG_FILE = LOG_DIR / f"bell_{datetime.date.today().isoformat()}.log"

# ============================================================
# 全局设置读写（V2.0 起供 GUI 与 Web 共用）
# ============================================================
DEFAULT_SETTINGS = {
    "auto_start": False,
    "prevent_sleep": False,
    "minimize_to_tray": True,
    # V2.0 Web 远程控制
    "web_enabled": False,
    "web_port": 8787,
    "web_token": "",
    # V2.1 调度容错：到点后多久之内仍算"准点"，超出即记为漏打（不再补打）
    "missed_tolerance_sec": 60,
}


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        except Exception:
            pass
    return data


def save_settings(settings: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("BellSystem")

# ============================================================
# 数据库
# ============================================================
class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        # V2.0: Web 服务线程与 GUI/调度线程并发访问，写操作需加锁保证事务原子；
        # WAL 模式允许读写并行，降低 "database is locked" 概率
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self._init_db()

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
        self.conn.close()


# ============================================================
# 音频播放引擎
# ============================================================
class AudioPlayer:
    """音频播放引擎（V2.1 重写：全局可取消 + 序列播放整体可停止）

    旧实现的三个问题：
    1) `_stop_flag` 在每次 play() 开头被复位 → 序列播放时"停止"只停当前一首，后续继续响；
    2) `_playing/_current_file` 被多个播放线程共享 → 状态互相覆盖，Web 的 playing 字段不可信；
    3) 到点打铃与 Web"立即打铃"并发时，pygame.mixer.music 单通道会被抢。
    新实现用"代际号(epoch)"表达取消：stop() 使 epoch+1，所有在途/后续播放立即失效。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._epoch = 0
        self._playing = False
        self._current_file = None
        if pygame:
            try:
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=4096)
            except Exception as e:
                logger.warning(f"pygame.mixer 初始化失败（可能无音频设备）: {e}")

    # ---------- 取消语义 ----------
    def current_epoch(self) -> int:
        with self._lock:
            return self._epoch

    def is_cancelled(self, epoch: Optional[int]) -> bool:
        if epoch is None:
            return False
        with self._lock:
            return epoch != self._epoch

    def stop(self):
        """停止播放：当前曲目立即停，序列播放中的后续曲目也会被跳过"""
        with self._lock:
            self._epoch += 1
            self._playing = False
        if pygame and pygame.mixer.get_init():
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass

    def is_playing(self) -> bool:
        with self._lock:
            return self._playing

    # ---------- 播放 ----------
    def play(self, file_path: str, volume: int = 80, count: int = 1,
             epoch: Optional[int] = None) -> bool:
        """同步播放，返回是否至少播放了一次；epoch 用于响应 stop()"""
        if not pygame:
            logger.error("pygame 未安装，无法播放音频")
            return False
        if epoch is None:
            epoch = self.current_epoch()
        if self.is_cancelled(epoch):
            return False

        path = Path(file_path)
        if not path.exists():
            logger.error(f"音频文件不存在: {file_path}")
            return False

        try:
            # 非 WAV：优先用转码缓存（首次转码，之后直接复用，避免每次打铃都等 ffmpeg）
            if path.suffix.lower() != ".wav":
                cached = self._ensure_wav_cache(path)
                if cached is not None:
                    path = cached

            pygame.mixer.music.load(str(path))
            pygame.mixer.music.set_volume(max(0.0, min(1.0, volume / 100.0)))
            with self._lock:
                self._current_file = str(path)
                self._playing = True

            played = 0
            for _ in range(max(1, int(count))):
                if self.is_cancelled(epoch):
                    break
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    if self.is_cancelled(epoch):
                        pygame.mixer.music.stop()
                        break
                    time.sleep(0.05)
                played += 1
            return played > 0
        except Exception as e:
            logger.error(f"播放失败: {e}")
            return False
        finally:
            with self._lock:
                self._playing = False

    def play_async(self, file_path: str, volume: int = 80, count: int = 1, callback=None):
        """异步播放（回调收到是否成功）"""
        epoch = self.current_epoch()

        def _run():
            ok = self.play(file_path, volume, count, epoch=epoch)
            if callback:
                try:
                    callback(ok)
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def play_many(self, tasks, on_item=None) -> int:
        """按顺序播放一组任务，返回实际播放条数；期间调用 stop() 会整体打断。

        tasks 为可索引对象（sqlite3.Row / dict），需含 audio_file/volume/play_count/name。
        """
        epoch = self.current_epoch()
        played = 0
        total = len(tasks)
        for idx, t in enumerate(tasks):
            if self.is_cancelled(epoch):
                logger.info("序列播放已被停止，剩余 %d 条跳过", total - idx)
                break
            audio = _row_get(t, "audio_file", "")
            volume = int(_row_get(t, "volume", 80) or 80)
            count = int(_row_get(t, "play_count", 1) or 1)
            ok = self.play(audio, volume, count, epoch=epoch)
            if self.is_cancelled(epoch):
                break
            played += 1
            if on_item:
                try:
                    on_item(t, ok)
                except Exception:
                    pass
        return played

    # ---------- 转码缓存 ----------
    def _ensure_wav_cache(self, path: Path) -> Optional[Path]:
        """把非 WAV 音频转成 WAV 并缓存；失败返回 None（调用方退回直接播放原文件）"""
        cache_dir = DATA_DIR / "cache"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            st = path.stat()
            key = "%s|%d|%d" % (str(path).lower(), int(st.st_mtime), st.st_size)
            digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
            stem = re.sub(r"[^0-9A-Za-z_.-]", "_", path.stem)[:40]
            dst = cache_dir / ("%s_%s.wav" % (stem, digest))
            if dst.exists() and dst.stat().st_size > 44:
                return dst
        except Exception as e:
            logger.warning(f"转码缓存目录不可用: {e}")
            return None

        tmp = dst.with_name(dst.stem + ".tmp.wav")
        if not self._convert_to_wav(str(path), str(tmp)):
            try:
                tmp.unlink()
            except OSError:
                pass
            return None
        try:
            tmp.replace(dst)
        except OSError:
            return tmp if tmp.exists() else None
        return dst

    def _convert_to_wav(self, src: str, dst: str) -> bool:
        """用 ffmpeg 转 WAV"""
        ffmpeg_path = FFMPEG_DIR / "ffmpeg.exe"
        if not ffmpeg_path.exists():
            # 尝试系统 PATH 中的 ffmpeg
            ffmpeg_path = "ffmpeg"
        try:
            subprocess.run(
                [str(ffmpeg_path), "-y", "-loglevel", "error", "-i", src,
                 "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", dst],
                capture_output=True, timeout=30,
                creationflags=SUBPROCESS_FLAGS
            )
            return Path(dst).exists()
        except Exception as e:
            logger.warning(f"ffmpeg 转换失败: {e}")
            return False


# ============================================================
# 定时引擎
# ============================================================
class Scheduler:
    POLL_INTERVAL = 1.0          # 远距离等待步长（秒）；临近到点自动收紧到 0.2 秒
    SAFETY_RECHECK = 30.0        # 无 resync 时的兜底重算间隔（应对有人直接改库）

    def __init__(self, db: Database, player: AudioPlayer):
        self.db = db
        self.player = player
        self._running = False
        self._thread = None
        self._pause = False
        self._next_time = None
        self._next_dt = None
        self._next_task_name = None
        self._next_task_id = None
        self._listeners = []
        # V2.0.1: 数据被 GUI/Web 修改后置位，等待循环立即中断并重新规划
        self._resync = threading.Event()
        # V2.1: 计划信息被调度线程写、GUI/Web 线程读，加锁保证读到一致快照
        self._state_lock = threading.RLock()
        self._tolerance = 60
        # V2.2: 日历缓存（每回合刷新；{YYYY-MM-DD: {...}}）
        self._calendar = {}

    def add_listener(self, fn):
        self._listeners.append(fn)

    def _notify(self, event: str, **kwargs):
        for fn in self._listeners:
            try:
                fn(event, **kwargs)
            except:
                pass

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("定时引擎已启动")

    def stop(self):
        self._running = False
        logger.info("定时引擎已停止")

    def reschedule(self):
        """数据变化后调用：打断当前等待循环，立即重新计算下一次任务"""
        self._resync.set()

    def pause(self):
        self._pause = True
        logger.info("定时引擎已暂停")

    def resume(self):
        self._pause = False
        logger.info("定时引擎已恢复")

    @property
    def is_paused(self) -> bool:
        return self._pause

    def get_next_time(self) -> Optional[str]:
        with self._state_lock:
            return self._next_time

    def get_next_info(self):
        """返回 (下一次执行的具体时间 datetime, 任务名称)，供界面精确计算距离"""
        with self._state_lock:
            return self._next_dt, self._next_task_name

    def _set_next(self, next_dt, task):
        with self._state_lock:
            self._next_dt = next_dt
            self._next_task_name = _row_get(task, "name", "") if task is not None else None
            self._next_task_id = _row_get(task, "id", None) if task is not None else None
            self._next_time = next_dt.strftime("%H:%M:%S") if next_dt else None

    def _effective_weekday(self, d: datetime.date, calendar=None) -> Optional[int]:
        """该日期"应该按星期几"打铃。

        None  → 当天放假（holiday），全天不打铃
        0-6   → 生效星期（workday 且指定 mirror_weekday 时按映射的星期，否则按真实星期）
        """
        cal = self._calendar if calendar is None else calendar
        info = cal.get(d.isoformat()) if cal else None
        if info:
            if info.get("kind") == "holiday":
                return None
            mirror = int(info.get("mirror_weekday", -1) if info.get("mirror_weekday") is not None else -1)
            if 0 <= mirror <= 6:
                return mirror
        return d.weekday()

    def _occurrence_on(self, s, d: datetime.date, now: datetime.datetime, calendar=None):
        """任务在日期 d 是否触发；返回触发时刻（可能早于 now，由调用方判断）"""
        try:
            parts = [int(x) for x in str(s["time_str"]).split(":")]
        except ValueError:
            return None
        while len(parts) < 3:
            parts.append(0)
        h, mi, sec = parts[0], parts[1], parts[2]

        eff = self._effective_weekday(d, calendar)
        if eff is None:
            return None

        stype = s["schedule_type"]
        if stype == "daily":
            pass
        elif stype == "weekly":
            if not s["week_days"]:
                return None
            try:
                days = [int(x) for x in str(s["week_days"]).split(",") if x != ""]
            except ValueError:
                return None
            if eff not in days:
                return None
        elif stype in ("date", "once"):
            if str(s["date_str"] or "") != d.isoformat():
                return None
        else:
            return None

        return datetime.datetime.combine(d, datetime.time(h, mi, sec))

    def get_today_plan(self) -> list:
        """今日剩余计划：[(datetime, 任务名, 任务id), ...]（供 GUI 预览，已排除放假与已过点）"""
        now = datetime.datetime.now()
        cal = self.db.get_calendar()
        plan = []
        for s in self.db.get_schedules(self.db.get_active_profile_id()):
            if not s["enabled"]:
                continue
            t = self._occurrence_on(s, now.date(), now, cal)
            if t is not None and t > now:
                plan.append((t, s["name"], s["id"]))
        plan.sort(key=lambda x: x[0])
        return plan

    def _calculate_next(self, now: datetime.datetime, schedules: list) -> Tuple[Optional[datetime.datetime], Optional[dict]]:
        """计算下一次执行时间和对应的任务（V2.2：日历感知 —— 放假日跳过、调休补课日按映射星期）"""
        candidates = []

        for s in schedules:
            if not s["enabled"]:
                continue

            # 容错 HH:MM / HH:MM:SS（手工改库或旧数据可能缺秒）
            try:
                parts = [int(x) for x in str(s["time_str"]).split(":")]
            except ValueError:
                continue
            while len(parts) < 3:
                parts.append(0)
            h, m, sec = parts[0], parts[1], parts[2]
            stype = s["schedule_type"]

            if stype == "daily":
                # 每天（跳过放假日的整天）
                for offset in range(0, 31):
                    d = (now + datetime.timedelta(days=offset)).date()
                    if self._effective_weekday(d) is None:
                        continue
                    t = datetime.datetime.combine(d, datetime.time(h, m, sec))
                    if t > now:
                        candidates.append((t, s))
                        break

            elif stype == "weekly":
                # 每周指定星期（调休补课日按映射星期匹配）
                if not s["week_days"]:
                    continue
                try:
                    week_days = [int(x) for x in str(s["week_days"]).split(",") if x != ""]
                except ValueError:
                    continue
                for offset in range(0, 31):
                    d = (now + datetime.timedelta(days=offset)).date()
                    eff = self._effective_weekday(d)
                    if eff is None:
                        continue
                    t = datetime.datetime.combine(d, datetime.time(h, m, sec))
                    if t <= now:
                        continue
                    if eff in week_days:
                        candidates.append((t, s))
                        break

            elif stype in ("date", "once"):
                # 指定日期 / 一次性（放假日跳过）
                if not s["date_str"]:
                    continue
                try:
                    t = datetime.datetime.strptime(s["date_str"], "%Y-%m-%d")
                    t = t.replace(hour=h, minute=m, second=sec, microsecond=0)
                    if t > now and self._effective_weekday(t.date()) is not None:
                        candidates.append((t, s))
                except ValueError:
                    continue

        if not candidates:
            return None, None

        candidates.sort(key=lambda x: x[0])
        return candidates[0]

    def _run(self):
        """主循环：计算下一次 → 等待 → 执行 → 重复"""
        while self._running:
            try:
                self._schedule_round()
            except Exception as e:
                logger.error(f"定时引擎异常: {e}", exc_info=True)
                time.sleep(10)

    def _schedule_round(self):
        """单个调度回合：算下一次 → 按绝对时间等待 → 到点执行 / 记漏打 / 记暂停

        V2.1 重写要点：
        - 不再"累加 waited 秒数"，改为每轮比较绝对时间：系统时间被调整、进程被挂起、
          笔记本睡眠唤醒后都不会错位（旧实现会长时间空转或提前触发）。
        - 暂停不再"吞掉"任务：暂停期间到点的任务写入 logs(status='paused') 留痕，恢复后不补打。
        - 到点容差可配置（默认 60 秒），超出容差记为 logs(status='missed')，不再只写文件日志。
        """
        self._resync.clear()
        self._tolerance = max(5, int(load_settings().get("missed_tolerance_sec", 60) or 60))
        self._calendar = self.db.get_calendar()

        now = datetime.datetime.now()
        schedules = self.db.get_schedules(self.db.get_active_profile_id())
        next_time, next_task = self._calculate_next(now, schedules)

        if next_time is None:
            self._set_next(None, None)
            self._notify("no_schedule")
            self._wait_idle()
            return

        self._set_next(next_time, next_task)
        self._notify("next_time", time=self._next_time, dt=next_time, name=self._next_task_name)

        last_check = time.monotonic()
        while self._running:
            now = datetime.datetime.now()
            delta = (next_time - now).total_seconds()

            if delta <= 0:
                self._handle_due(next_task, -delta)
                return

            if self._resync.is_set():
                self._resync.clear()
                return  # 数据已变化，重新规划

            if time.monotonic() - last_check >= self.SAFETY_RECHECK and delta > 10:
                last_check = time.monotonic()
                if self._plan_changed(next_time):
                    return  # 计划有变化，重新调度

            # 远处按 1 秒轮询（与旧实现功耗相当），临近到点收紧到 0.2 秒以提高准点精度
            if delta > 5:
                time.sleep(self.POLL_INTERVAL)
            else:
                time.sleep(max(0.05, min(0.2, delta)))

    def _wait_idle(self):
        """无任务时的分片睡眠，及时响应新增任务 / 停止"""
        for _ in range(60):
            if not self._running or self._resync.is_set():
                return
            time.sleep(1)

    def _plan_changed(self, current_next) -> bool:
        """兜底重算：外部直接改库（未走 resync）时发现计划变化"""
        self._calendar = self.db.get_calendar()
        now = datetime.datetime.now()
        schedules = self.db.get_schedules(self.db.get_active_profile_id())
        nt, task = self._calculate_next(now, schedules)
        if nt is None:
            return True
        task_id = _row_get(task, "id", None)
        with self._state_lock:
            current_id = self._next_task_id
        if task_id == current_id and abs((nt - current_next).total_seconds()) < 1:
            return False
        # 只有"出现更早的任务"或"换成了另一个任务"才重排，避免同任务顺延被误判
        return nt < current_next - datetime.timedelta(seconds=1) or task_id != current_id

    def _handle_due(self, task, late_seconds: float):
        """到点处理：暂停 → 记 paused；超出容差 → 记 missed；否则执行"""
        if self._pause:
            self._record_skip(task, "paused", "暂停期间到点，已跳过（恢复后不补打）")
            return
        if late_seconds > self._tolerance:
            self._record_skip(
                task, "missed",
                "延迟 %.0f 秒，超出容差 %d 秒（可能系统睡眠/进程挂起）" % (late_seconds, self._tolerance))
            return
        self._execute_task(task)

    def _record_skip(self, task, status: str, reason: str):
        """把"跳过/漏打"写进执行日志，保证事后可追溯"""
        name = _row_get(task, "name", "")
        audio = _row_get(task, "audio_file", "")
        logger.warning("跳过打铃[%s]: %s → %s", status, name, reason)
        self.db.add_log(_row_get(task, "id", 0), name, audio, status, reason)
        self._notify("skipped", name=name, status=status, reason=reason)

    def _execute_task(self, task):
        """执行一个打铃任务"""
        name = _row_get(task, "name", "")
        audio = _row_get(task, "audio_file", "")
        volume = int(_row_get(task, "volume", 80) or 80)
        play_count = int(_row_get(task, "play_count", 1) or 1)

        logger.info(f"🔔 执行打铃: {name} → {audio}")

        # 先通知
        self._notify("ringing", name=name, audio=audio)

        # 异步播放
        def on_play(result):
            status = "success" if result else "failed"
            error = "" if result else "播放失败（文件缺失 / 格式不支持 / 无音频设备）"
            self.db.add_log(_row_get(task, "id", 0), name, audio, status, error)
            # V2.1: 一次性任务改为"播放成功后才删除"，失败时保留以便重试
            if result and _row_get(task, "schedule_type", "") == "once":
                self.db.delete_schedule(_row_get(task, "id", 0))
                logger.info(f"一次性任务已执行并删除: {name}")
            self._notify("ring_done", name=name, result=status)

        self.player.play_async(audio, volume, play_count, callback=on_play)


# ============================================================
# 主 GUI
# ============================================================
class BellApp:
    def __init__(self, db: Database, player: AudioPlayer, scheduler: Scheduler):
        self.db = db
        self.player = player
        self.scheduler = scheduler
        self.scheduler.add_listener(self._on_scheduler_event)

        self.root = tk.Tk()
        self.root.title("🔔 智能打铃系统")
        self.root.geometry("820x620")
        self.root.minsize(700, 500)

        # 设置图标
        try:
            self.root.iconbitmap(default=APP_DIR / "bell.ico")
        except:
            pass

        # 配色
        self.bg_color = "#f5f5f5"
        self.accent_color = "#2196F3"
        self.root.configure(bg=self.bg_color)

        # V2.2: 列表排序状态 (列名, 是否倒序) 与今日预览刷新节拍
        self._sort_state = ("time", False)
        self._tick = 0
        self._build_ui()
        self._refresh_display()
        self._update_clock()
        self._bind_shortcuts()

        # 关闭时最小化到托盘
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 系统托盘
        self._tray = None
        self._setup_tray()

        # V2.0 Web 远程控制服务
        # 注意：Web 线程禁止直接调 Tk；用队列投递，主线程定时排空后刷新
        self._web_events = queue.Queue()
        self._web = None
        self._start_web_server()
        self.root.after(300, self._drain_web_events)

    def _drain_web_events(self):
        changed = False
        try:
            while True:
                self._web_events.get_nowait()
                changed = True
        except queue.Empty:
            pass
        if changed:
            self._refresh_display()
        self.root.after(400, self._drain_web_events)

    def _build_ui(self):
        """构建界面"""
        root = self.root

        # ---- 顶部标题栏 ----
        header = tk.Frame(root, bg=self.accent_color, height=50)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        # 底部状态栏（先于中部扩展区域声明，保证占住底边）
        status_bar = tk.Frame(root, bg="#e8eaed")
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self._web_label = tk.Label(status_bar, text="", bg="#e8eaed", fg="#555",
                                   font=("微软雅黑", 9), cursor="hand2")
        self._web_label.pack(side=tk.RIGHT, padx=10, pady=3)
        self._web_label.bind("<Button-1>", lambda e: self._open_web_ui())

        tk.Label(header, text="🔔 智能打铃系统", font=("微软雅黑", 16, "bold"),
                 bg=self.accent_color, fg="white").pack(side=tk.LEFT, padx=15, pady=8)

        # 最小化到托盘
        self._btn_minimize = tk.Button(header, text="—", font=("", 14, "bold"),
                                       bg=self.accent_color, fg="white", bd=0,
                                       command=self._minimize_to_tray)
        self._btn_minimize.pack(side=tk.RIGHT, padx=5)

        # ---- 方案切换 + 状态栏 ----
        top_bar = tk.Frame(root, bg=self.bg_color)
        top_bar.pack(fill=tk.X, padx=10, pady=(10, 5))

        tk.Label(top_bar, text="当前方案:", bg=self.bg_color,
                 font=("微软雅黑", 10)).pack(side=tk.LEFT)

        self._profile_var = tk.StringVar()
        self._profile_combo = ttk.Combobox(top_bar, textvariable=self._profile_var,
                                           state="readonly", width=18, font=("微软雅黑", 10))
        self._profile_combo.pack(side=tk.LEFT, padx=5)
        self._profile_combo.bind("<<ComboboxSelected>>", self._on_profile_change)

        self._btn_manage_profile = tk.Button(top_bar, text="管理方案", font=("微软雅黑", 9),
                                             command=self._manage_profiles)
        self._btn_manage_profile.pack(side=tk.LEFT, padx=5)

        tk.Button(top_bar, text="🔊 测试播放", font=("微软雅黑", 9),
                  command=self._test_play).pack(side=tk.LEFT, padx=10)

        self._play_all_btn = tk.Button(top_bar, text="🔔 立即打铃", font=("微软雅黑", 9, "bold"),
                                       bg="#FF9800", fg="white", command=self._immediate_ring)
        self._play_all_btn.pack(side=tk.LEFT, padx=5)

        # ---- 时钟 + 下次打铃 ----
        clock_frame = tk.Frame(root, bg="#ffffff", relief=tk.GROOVE, bd=1)
        clock_frame.pack(fill=tk.X, padx=10, pady=5)

        inner = tk.Frame(clock_frame, bg="#ffffff")
        inner.pack(pady=15)

        # 当前时间
        tk.Label(inner, text="当前时间", font=("微软雅黑", 10), fg="#888",
                 bg="#ffffff").pack()
        self._clock_label = tk.Label(inner, text="00:00:00",
                                     font=("Consolas", 36, "bold"), fg="#333",
                                     bg="#ffffff")
        self._clock_label.pack()

        # 下次打铃
        next_frame = tk.Frame(inner, bg="#ffffff")
        next_frame.pack(pady=(5, 0))

        tk.Label(next_frame, text="下一次打铃:", font=("微软雅黑", 10), fg="#888",
                 bg="#ffffff").pack(side=tk.LEFT)
        self._next_time_label = tk.Label(next_frame, text="--:--:--",
                                          font=("Consolas", 18, "bold"), fg=self.accent_color,
                                          bg="#ffffff")
        self._next_time_label.pack(side=tk.LEFT, padx=10)
        self._next_date_label = tk.Label(next_frame, text="", font=("微软雅黑", 10),
                                          fg="#666", bg="#ffffff")
        self._next_date_label.pack(side=tk.LEFT, padx=5)
        self._next_dist_label = tk.Label(inner, text="暂无任务", font=("微软雅黑", 10),
                                         fg=self.accent_color, bg="#ffffff")
        self._next_dist_label.pack(pady=(2, 0))

        self._pause_btn = tk.Button(inner, text="⏸ 暂停打铃", font=("微软雅黑", 9),
                                    command=self._toggle_pause)
        self._pause_btn.pack(pady=(5, 0))

        # ---- 任务列表 ----
        list_frame = tk.Frame(root, bg=self.bg_color)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # 标题行 + 搜索框（V2.2）
        title_row = tk.Frame(list_frame, bg=self.bg_color)
        title_row.pack(fill=tk.X)
        tk.Label(title_row, text="📋 打铃任务列表", font=("微软雅黑", 11, "bold"),
                 bg=self.bg_color).pack(side=tk.LEFT)
        tk.Label(title_row, text="搜索:", font=("微软雅黑", 9), bg=self.bg_color,
                 fg="#666").pack(side=tk.LEFT, padx=(14, 2))
        self._search_var = tk.StringVar()
        search_entry = tk.Entry(title_row, textvariable=self._search_var, width=18,
                                font=("微软雅黑", 9))
        search_entry.pack(side=tk.LEFT)
        self._search_var.trace_add("write", lambda *a: self._refresh_display())
        tk.Button(title_row, text="清除", font=("微软雅黑", 8),
                  command=lambda: self._search_var.set("")).pack(side=tk.LEFT, padx=4)

        # 今日剩余计划预览（V2.2）
        self._today_label = tk.Label(list_frame, text="今日剩余：—", anchor=tk.W,
                                     font=("微软雅黑", 9), fg="#0b7a3b", bg=self.bg_color)
        self._today_label.pack(fill=tk.X, pady=(3, 4))

        # Treeview
        columns = ("enabled", "time", "week", "name", "audio", "volume", "type")
        self._tree = ttk.Treeview(list_frame, columns=columns, show="headings",
                                   height=10, selectmode="browse")

        # V2.2: 点击表头排序（再点一次反向）
        for col, text in (("enabled", "启用"), ("time", "时间"), ("week", "星期"),
                          ("name", "任务名称"), ("audio", "铃声文件"),
                          ("volume", "音量"), ("type", "类型")):
            self._tree.heading(col, text=text, command=lambda c=col: self._sort_by(c))

        self._tree.column("enabled", width=50, anchor=tk.CENTER)
        self._tree.column("time", width=90, anchor=tk.CENTER)
        self._tree.column("week", width=120, anchor=tk.CENTER)
        self._tree.column("name", width=140)
        self._tree.column("audio", width=200)
        self._tree.column("volume", width=60, anchor=tk.CENTER)
        self._tree.column("type", width=80, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._tree.bind("<Double-1>", lambda e: self._edit_schedule())
        self._tree.bind("<Button-1>", self._on_tree_click)

        # ---- 底部按钮 ----
        btn_frame = tk.Frame(root, bg=self.bg_color)
        btn_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        tk.Button(btn_frame, text="➕ 添加任务", font=("微软雅黑", 9),
                  command=self._add_schedule).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="✏️ 编辑", font=("微软雅黑", 9),
                  command=self._edit_schedule).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="🗑 删除", font=("微软雅黑", 9),
                  command=self._delete_schedule).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="🔊 试听", font=("微软雅黑", 9),
                  command=self._play_selected).pack(side=tk.LEFT, padx=10)

        tk.Button(btn_frame, text="📅 日历", font=("微软雅黑", 9),
                  command=self._open_calendar).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="📥 课表导入/导出", font=("微软雅黑", 9),
                  command=self._open_io).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="📋 执行日志", font=("微软雅黑", 9),
                  command=self._show_logs).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="⚙ 系统设置", font=("微软雅黑", 9),
                  command=self._system_settings).pack(side=tk.LEFT, padx=2)

    def _refresh_display(self):
        """刷新界面数据"""
        # 刷新方案下拉
        active_id = self.db.get_active_profile_id()
        profiles = self.db.get_profiles()
        names = [p["name"] for p in profiles]
        self._profile_combo["values"] = names
        active_name = next((p["name"] for p in profiles if p["id"] == active_id), names[0] if names else "")
        self._profile_var.set(active_name)

        # 刷新任务列表
        for item in self._tree.get_children():
            self._tree.delete(item)

        schedules = self.db.get_schedules(active_id)
        week_map = {0: "一", 1: "二", 2: "三", 3: "四", 4: "五", 5: "六", 6: "日"}
        stype_map = {"daily": "每天", "weekly": "每周", "date": "指定日期", "once": "一次性"}

        keyword = ""
        try:
            keyword = (self._search_var.get() or "").strip().lower()
        except Exception:
            pass

        col_index = {"enabled": 0, "time": 1, "week": 2, "name": 3,
                     "audio": 4, "volume": 5, "type": 6}
        rows = []
        for s in schedules:
            enabled = "✓" if s["enabled"] else "☐"
            stype = stype_map.get(s["schedule_type"], s["schedule_type"])

            week_str = ""
            if s["schedule_type"] == "weekly" and s["week_days"]:
                days = [week_map.get(int(x), "?") for x in s["week_days"].split(",")]
                week_str = "周" + "、".join(days)
            elif s["schedule_type"] in ("date", "once") and s["date_str"]:
                week_str = s["date_str"]

            audio_name = Path(s["audio_file"]).name
            if keyword:
                haystack = " ".join([str(s["name"]), str(s["time_str"]),
                                     str(week_str), audio_name, stype]).lower()
                if keyword not in haystack:
                    continue

            rows.append((s, (enabled, s["time_str"], week_str, s["name"],
                             audio_name, f"{s['volume']}%", stype)))

        # V2.2: 按表头排序（时间列用字符串比较即可，HH:MM:SS 定长）
        sort_col, reverse = self._sort_state
        idx = col_index.get(sort_col, 1)
        rows.sort(key=lambda r: str(r[1][idx]), reverse=reverse)

        for s, values in rows:
            self._tree.insert("", tk.END, iid=str(s["id"]), values=values)

        self._refresh_today_plan()

    def _on_tree_click(self, event):
        """点击树形列表中的启用列切换状态"""
        region = self._tree.identify_region(event.x, event.y)
        if region == "cell":
            col = self._tree.identify_column(event.x)
            if col == "#0" or col == "#1":  # 第一列: 启用
                item = self._tree.identify_row(event.y)
                if item:
                    values = self._tree.item(item, "values")
                    enabled = values[0] == "☐"
                    self.db.toggle_schedule(int(item), enabled)
                    self._after_task_change()

    def _update_clock(self):
        """更新时钟显示"""
        now = datetime.datetime.now()
        self._clock_label.config(text=now.strftime("%H:%M:%S"))

        # 用定时引擎保存的真实下一次时间计算（不再假设当天/明天）
        next_dt, next_name = self.scheduler.get_next_info()
        if next_dt:
            self._next_time_label.config(text=next_dt.strftime("%H:%M:%S"))

            # 相对日期描述
            weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            day_diff = (next_dt.date() - now.date()).days
            if day_diff == 0:
                day_text = "今天"
            elif day_diff == 1:
                day_text = "明天"
            else:
                day_text = next_dt.strftime("%m-%d") + " " + weekday_names[next_dt.weekday()]
            self._next_date_label.config(text=f"{day_text} · {next_name or ''}")

            # 精确剩余时间
            delta = next_dt - now
            total_sec = int(delta.total_seconds())
            if total_sec < 0:
                total_sec = 0
            m, s = divmod(total_sec, 60)
            h, m = divmod(m, 60)
            if h > 0:
                self._next_dist_label.config(text=f"距离：{h}时{m}分{s}秒")
            else:
                self._next_dist_label.config(text=f"距离：{m}分{s}秒")
        else:
            self._next_time_label.config(text="--:--:--")
            self._next_date_label.config(text="")
            self._next_dist_label.config(text="暂无任务")

        # V2.2: 每 30 秒刷新一次今日剩余计划
        self._tick = (self._tick + 1) % 30
        if self._tick == 0:
            self._refresh_today_plan()

        self.root.after(1000, self._update_clock)

    def _on_scheduler_event(self, event: str, **kwargs):
        """调度器事件回调"""
        if event == "next_time":
            pass  # 时钟更新会自己读
        elif event == "ringing":
            # 显示通知
            name = kwargs.get("name", "")
            logger.info(f"🔔 正在打铃: {name}")
        elif event == "ring_done":
            if kwargs.get("result") != "success":
                # V2.2: 打铃失败要能"被人看见"，不能只躺在日志里
                self._notify_tray("⚠ 打铃失败", "「%s」播放失败，请检查铃声文件与音频设备"
                                  % kwargs.get("name", ""))
            self._refresh_display()
        elif event == "skipped":
            # V2.1: 暂停跳过 / 漏打（超出容差）都会走到这里，刷新列表让用户看到留痕
            if kwargs.get("status") == "missed":
                self._notify_tray("⚠ 漏打提醒",
                                  "「%s」延迟超出容差，已记为漏打" % kwargs.get("name", ""))
            self._refresh_display()
        elif event == "no_schedule":
            self._next_time_label.config(text="--:--:--")
            self._next_date_label.config(text="")
            self._next_dist_label.config(text="暂无任务")

    def _after_task_change(self):
        """任务/方案数据变化后的统一收尾：刷新界面 + 定时引擎立即重排"""
        self._refresh_display()
        try:
            self.scheduler.reschedule()
        except Exception:
            pass

    # ---- V2.2 列表交互 ----
    def _sort_by(self, col):
        """点击表头：同列再点一次切换升降序"""
        cur, reverse = self._sort_state
        self._sort_state = (col, (not reverse) if cur == col else False)
        self._refresh_display()

    def _refresh_today_plan(self):
        """刷新"今日剩余"预览"""
        try:
            plan = self.scheduler.get_today_plan()
        except Exception as e:
            logger.warning("计算今日计划失败: %s", e)
            plan = []
        try:
            if not plan:
                self._today_label.config(text="今日剩余：无（已全部执行或今日放假）")
                return
            items = ["%s %s" % (dt.strftime("%H:%M"), name) for dt, name, _sid in plan[:4]]
            more = "" if len(plan) <= 4 else " 等 %d 项" % len(plan)
            self._today_label.config(text="今日剩余 %d 次：%s%s" % (len(plan), "、".join(items), more))
        except Exception:
            pass

    def _notify_tray(self, title, message):
        """托盘气泡告警（无托盘则退化为写日志）"""
        logger.warning("%s: %s", title, message)
        if not self._tray:
            return
        try:
            self._tray.notify(message, title)
        except Exception:
            pass

    def _open_calendar(self):
        CalendarDialog(self.root, self.db, self._after_task_change)

    def _open_io(self):
        ScheduleIODialog(self.root, self.db, self._after_task_change)

    # ---- 任务操作 ----
    def _add_schedule(self):
        ScheduleDialog(self.root, self.db, self._after_task_change)

    def _edit_schedule(self):
        selected = self._tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先选择一个任务")
            return
        sid = int(selected[0])
        row = self.db.get_schedule(sid)
        if row:
            ScheduleDialog(self.root, self.db, self._after_task_change, row)

    def _delete_schedule(self):
        selected = self._tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先选择一个任务")
            return
        if messagebox.askyesno("确认", "确定要删除选中的任务吗？"):
            for item in selected:
                self.db.delete_schedule(int(item))
            self._after_task_change()

    def _play_selected(self):
        """试听选中任务的铃声"""
        selected = self._tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先选择一个任务")
            return
        sid = int(selected[0])
        row = self.db.get_schedule(sid)
        if row:
            self.player.play_async(row["audio_file"], row["volume"], 1)

    def _test_play(self):
        """测试播放"""
        file_path = filedialog.askopenfilename(
            title="选择测试音频文件",
            filetypes=[("音频文件", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac"), ("所有文件", "*.*")]
        )
        if file_path:
            self.player.play_async(file_path, 80, 1)

    def _immediate_ring(self):
        """立即播放所有启用的任务铃声"""
        active_id = self.db.get_active_profile_id()
        schedules = self.db.get_schedules(active_id)
        enabled = [s for s in schedules if s["enabled"]]
        if not enabled:
            messagebox.showinfo("提示", "当前没有启用的任务")
            return

        def play_all():
            # V2.1: 走 play_many，"停止播放"可整体打断整个序列（旧实现下一曲会继续响）
            played = self.player.play_many(
                enabled,
                on_item=lambda s, ok: logger.info(
                    "立即打铃: %s (%s)", _row_get(s, "name", ""), "成功" if ok else "失败"))
            logger.info("立即打铃结束，实际播放 %d 条", played)

        threading.Thread(target=play_all, daemon=True).start()

    # ---- 方案管理 ----
    def _on_profile_change(self, event=None):
        name = self._profile_var.get()
        profiles = self.db.get_profiles()
        for p in profiles:
            if p["name"] == name:
                self.db.activate_profile(p["id"])
                self._after_task_change()
                break

    def _manage_profiles(self):
        ProfileDialog(self.root, self.db, self._after_task_change)

    # ---- 暂停 ----
    def _toggle_pause(self):
        if self.scheduler.is_paused:
            self.scheduler.resume()
            self._pause_btn.config(text="⏸ 暂停打铃")
        else:
            self.scheduler.pause()
            self._pause_btn.config(text="▶ 恢复打铃")

    # ---- 日志 ----
    def _show_logs(self):
        LogDialog(self.root, self.db)

    # ---- Web 远程控制 (V2.0) ----
    def _start_web_server(self, show_error=False):
        settings = load_settings()
        if not settings.get("web_enabled"):
            self._update_web_label(None)
            return
        try:
            self._web_token = str(settings.get("web_token") or "")
            self._web = BellWebServer(
                self.db, self.player, self.scheduler,
                port=int(settings.get("web_port") or 8787),
                token=self._web_token,
                on_change=lambda: self._web_events.put(1))
            self._web.start()
            urls = self._web.get_urls()
            logger.info("🌐 远程控制地址: %s", " , ".join(urls))
            if not self._web_token:
                logger.warning("⚠ 远程控制未设置访问令牌：同一局域网内任何设备都能操作打铃")
            self._update_web_label(urls[0])
            if show_error and urls:
                extra = ""
                if not self._web_token:
                    extra = ("\n\n⚠ 安全提示：当前未设置访问令牌，同一局域网内"
                             "任何人都能打开此页面并操作打铃。\n"
                             "建议在「系统设置 → 访问令牌」中设置一个令牌。")
                messagebox.showinfo(
                    "远程控制已启动",
                    "同一局域网内的手机/电脑用浏览器打开：\n\n" + "\n".join(urls) + extra,
                    parent=self.root)
        except OSError as e:
            self._web = None
            logger.error("Web 服务启动失败: %s", e)
            self._update_web_label(None)
            if show_error:
                messagebox.showerror(
                    "远程控制启动失败",
                    f"端口被占用或无权限：{e}\n可在系统设置中更换端口。",
                    parent=self.root)

    def _stop_web_server(self):
        if self._web:
            self._web.stop()
            self._web = None
        self._update_web_label(None)

    def _restart_web_server(self, show_error=False):
        self._stop_web_server()
        self._start_web_server(show_error=show_error)

    def _open_web_ui(self):
        if self._web and self._web.is_running:
            urls = self._web.get_urls()
            if urls:
                webbrowser.open(urls[-1])  # 优先 127.0.0.1

    def _update_web_label(self, url):
        try:
            if not url:
                self._web_label.config(text="")
                return
            # V2.1: 未设置访问令牌时在状态栏持续提示（局域网内任何人可操作）
            warn = "  ⚠ 未设令牌" if not getattr(self, "_web_token", "") else ""
            self._web_label.config(text=f"🌐 远程控制: {url}{warn}")
        except Exception:
            pass

    # ---- 系统设置 ----
    def _system_settings(self):
        dlg = SettingsDialog(self.root)
        if getattr(dlg, "saved", False):
            # 设置变化后立即生效（含端口/令牌/开关）；启用时弹窗展示访问地址
            self._restart_web_server(show_error=True)

    # ---- 系统托盘 ----
    def _setup_tray(self):
        if not pystray:
            return

        def create_image():
            # V2.1: 优先用 bell.ico（PIL 默认位图字体画不出 emoji，旧图标实际是空白/方块）
            try:
                ico = APP_DIR / "bell.ico"
                if ico.exists():
                    return Image.open(ico).convert("RGBA")
            except Exception:
                pass
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([8, 8, 56, 56], fill="#FF9800")
            draw.rectangle([26, 16, 38, 40], fill="white")
            draw.ellipse([20, 34, 44, 48], fill="white")
            return img

        # V2.1: pystray 的回调跑在它自己的线程里，而 Tk 不是线程安全的。
        # 所有界面操作必须 post 回主线程，否则会随机崩溃/卡死。
        def on_open(icon, item):
            self._post_ui(self._show_window)

        def on_quit(icon, item):
            self._post_ui(self._quit_app)

        menu = pystray.Menu(
            pystray.MenuItem("打开主界面", on_open, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即打铃", lambda: self._post_ui(self._immediate_ring)),
            pystray.MenuItem("暂停打铃", lambda: self._post_ui(self.scheduler.pause)),
            pystray.MenuItem("恢复打铃", lambda: self._post_ui(self.scheduler.resume)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开网页控制台", lambda: self._post_ui(self._open_web_ui)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", on_quit),
        )

        self._tray = pystray.Icon("BellSystem", create_image(), "智能打铃系统", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _post_ui(self, fn, *args, **kwargs):
        """把任意线程的界面操作投递到 Tk 主线程（托盘线程/后台线程必须走这里）"""
        try:
            self.root.after(0, lambda: fn(*args, **kwargs))
        except Exception:
            pass

    def _show_window(self):
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _minimize_to_tray(self):
        self.root.withdraw()

    def _quit_app(self):
        """退出：停托盘 + 退出主循环；资源清理统一在 main() 里做（不再 os._exit 硬杀）"""
        if self._tray:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        self.root.quit()

    def _on_close(self):
        """关闭窗口：按设置决定最小化到托盘还是退出"""
        try:
            to_tray = bool(load_settings().get("minimize_to_tray", True))
        except Exception:
            to_tray = True
        # V2.1: 旧实现无视"最小化到托盘"设置项，只要托盘存在就隐藏
        if self._tray and to_tray:
            self._minimize_to_tray()
        else:
            self._quit_app()

    def _bind_shortcuts(self):
        self.root.bind("<Escape>", lambda e: self._minimize_to_tray())

    def run(self):
        self.root.mainloop()


# ============================================================
# 课表 CSV 导入 / 导出（V2.2）
# ============================================================
CSV_HEADER = ["任务名称", "重复方式", "时间", "星期", "日期", "铃声文件", "音量", "次数", "启用"]
TYPE_TO_CN = {"daily": "每天", "weekly": "每周", "date": "指定日期", "once": "一次性"}
CN_TO_TYPE = {v: k for k, v in TYPE_TO_CN.items()}


def _parse_week_days(text: str) -> str:
    """把 "0,2,4" / "一,三,五" / "周一,周三" / "1,3,5"(1-7) 统一成 "0,2,4" """
    text = (text or "").strip()
    if not text:
        return ""
    cn = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    out = set()
    nums = []
    for part in text.replace("，", ",").replace("、", ",").replace(" ", ",").split(","):
        part = part.strip().lstrip("周")
        if not part:
            continue
        if part in cn:
            out.add(cn[part])
            continue
        try:
            nums.append(int(part))
        except ValueError:
            return None
    if nums:
        # 数字默认按 0-6（周一=0，与导出格式一致，保证 round-trip）；
        # 只有当出现 7 时才按 1-7（周一=1，周日=7）解释。
        one_based = 7 in nums
        for n in nums:
            if one_based:
                n -= 1
            if not (0 <= n <= 6):
                return None
            out.add(n)
    return ",".join(str(d) for d in sorted(out))


def export_schedules_csv(db: Database, path: str, profile_id: int = 0) -> int:
    """导出当前方案课表到 CSV（UTF-8 BOM，Excel 直接打开不乱码），返回条数"""
    import csv
    rows = db.get_schedules(profile_id or db.get_active_profile_id())
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for s in rows:
            w.writerow([
                s["name"], TYPE_TO_CN.get(s["schedule_type"], s["schedule_type"]),
                s["time_str"], s["week_days"] or "", s["date_str"] or "",
                s["audio_file"], s["volume"], s["play_count"],
                "是" if s["enabled"] else "否",
            ])
    return len(rows)


def import_schedules_csv(db: Database, path: str, profile_id: int = 0):
    """从 CSV 批量建任务，返回 (成功数, 错误列表, 警告列表)"""
    import csv
    ok, errors, warnings = 0, [], []
    target_pid = profile_id or db.get_active_profile_id()
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "任务名称" not in reader.fieldnames:
            return 0, ["表头不正确：第一行必须是 " + ",".join(CSV_HEADER)], []
        for idx, row in enumerate(reader, start=2):
            try:
                name = (row.get("任务名称") or "").strip()
                if not name:
                    errors.append("第%d行：任务名称为空" % idx)
                    continue
                stype = CN_TO_TYPE.get((row.get("重复方式") or "").strip(), None)
                if stype is None:
                    stype = (row.get("重复方式") or "").strip().lower()
                if stype not in ("daily", "weekly", "date", "once"):
                    errors.append("第%d行：重复方式无法识别（应为 每天/每周/指定日期/一次性）" % idx)
                    continue
                tstr = (row.get("时间") or "").strip()
                try:
                    parts = [int(x) for x in tstr.split(":")]
                except ValueError:
                    errors.append("第%d行：时间格式错误（应为 HH:MM 或 HH:MM:SS）" % idx)
                    continue
                while len(parts) < 3:
                    parts.append(0)
                if not (0 <= parts[0] <= 23 and 0 <= parts[1] <= 59 and 0 <= parts[2] <= 59):
                    errors.append("第%d行：时间数值超出范围" % idx)
                    continue
                week_days = _parse_week_days(row.get("星期") or "")
                if week_days is None:
                    errors.append("第%d行：星期无法识别（如 0,2,4 或 一,三,五）" % idx)
                    continue
                date_str = (row.get("日期") or "").strip()
                if stype in ("date", "once"):
                    try:
                        datetime.datetime.strptime(date_str, "%Y-%m-%d")
                    except ValueError:
                        errors.append("第%d行：日期格式错误（应为 YYYY-MM-DD）" % idx)
                        continue
                if stype == "weekly" and not week_days:
                    errors.append("第%d行：每周任务必须填星期" % idx)
                    continue
                audio = (row.get("铃声文件") or "").strip()
                if not audio:
                    errors.append("第%d行：铃声文件为空" % idx)
                    continue
                if not Path(audio).exists():
                    warnings.append("第%d行：铃声不存在，已导入但需修正 → %s" % (idx, audio))
                try:
                    volume = max(0, min(100, int(float(row.get("音量") or 80))))
                except ValueError:
                    volume = 80
                try:
                    count = max(1, min(10, int(float(row.get("次数") or 1))))
                except ValueError:
                    count = 1
                enabled = (row.get("启用") or "是").strip() in ("是", "1", "true", "True", "Y", "y", "")
                db.add_schedule({
                    "name": name, "schedule_type": stype,
                    "time_str": "%02d:%02d:%02d" % (parts[0], parts[1], parts[2]),
                    "week_days": week_days, "date_str": date_str,
                    "audio_file": audio, "volume": volume, "play_count": count,
                    "enabled": 1 if enabled else 0, "profile_id": target_pid,
                })
                ok += 1
            except Exception as e:
                errors.append("第%d行：%s" % (idx, e))
    return ok, errors, warnings


# ============================================================
# 任务数据组装（GUI 与 Web 共用语义，便于单测）
# ============================================================
def build_schedule_payload(name, schedule_type, time_str, week_days="", date_str="",
                           audio_file="", volume=80, play_count=1, enabled=1,
                           base=None, active_profile_id=0):
    """组装写入数据库的任务字典。

    base 为编辑前的原始行（sqlite3.Row / dict）：
      - 未在界面上修改的字段（如 profile_id）沿用原值，避免"编辑一次就被改写"；
      - 新建时 profile_id 取当前活动方案。
    """
    if base is not None:
        profile_id = _row_get(base, "profile_id", active_profile_id)
    else:
        profile_id = active_profile_id
    return {
        "name": name,
        "schedule_type": schedule_type,
        "time_str": time_str,
        "week_days": week_days,
        "date_str": date_str,
        "audio_file": audio_file,
        "volume": max(0, min(100, int(volume))),
        "play_count": max(1, min(10, int(play_count))),
        "enabled": 1 if enabled else 0,
        "profile_id": int(profile_id or 0),
    }


# ============================================================
# 任务编辑对话框
# ============================================================
class ScheduleDialog:
    def __init__(self, parent, db, on_save, data=None):
        self.db = db
        self.on_save = on_save
        self.data = data
        self.edit_mode = data is not None

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("编辑打铃任务" if self.edit_mode else "添加打铃任务")
        self.dialog.geometry("520x680")
        self.dialog.minsize(500, 650)
        self.dialog.resizable(True, True)
        self.dialog.transient(parent)
        self.dialog.grab_set()

        self._build_ui()
        if self.edit_mode:
            self._load_data()

        self.dialog.wait_window()

    def _build_ui(self):
        d = self.dialog
        pad = {"padx": 15, "pady": 5}

        # 底部按钮区（先 pack 到最底部，保证始终可见）
        btn_frame = tk.Frame(d)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=15, pady=(10, 10))
        tk.Button(btn_frame, text="保存任务", font=("微软雅黑", 10), width=10,
                  command=self._save).pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_frame, text="取消", font=("微软雅黑", 10), width=10,
                  command=self.dialog.destroy).pack(side=tk.RIGHT, padx=5)

        # 任务名称
        tk.Label(d, text="任务名称:", font=("微软雅黑", 10)).pack(anchor=tk.W, **pad)
        self._name = tk.Entry(d, font=("微软雅黑", 10), width=40)
        self._name.pack(fill=tk.X, **pad)

        # 重复方式
        tk.Label(d, text="重复方式:", font=("微软雅黑", 10)).pack(anchor=tk.W, **pad)
        self._type_var = tk.StringVar(value="weekly")
        type_frame = tk.Frame(d)
        type_frame.pack(fill=tk.X, **pad)
        for val, text in [("daily", "每天"), ("weekly", "每周"), ("date", "指定日期"), ("once", "一次性")]:
            tk.Radiobutton(type_frame, text=text, variable=self._type_var,
                           value=val, command=self._on_type_change).pack(side=tk.LEFT, padx=5)

        # 星期选择
        self._week_frame = tk.Frame(d)
        self._week_frame.pack(fill=tk.X, **pad)
        tk.Label(self._week_frame, text="星期:", font=("微软雅黑", 10)).pack(anchor=tk.W)
        self._week_vars = {}
        week_names = [("一", 0), ("二", 1), ("三", 2), ("四", 3), ("五", 4), ("六", 5), ("日", 6)]
        wf = tk.Frame(self._week_frame)
        wf.pack()
        for cn, ci in week_names:
            var = tk.BooleanVar(value=True if ci < 5 else False)
            self._week_vars[ci] = var
            tk.Checkbutton(wf, text=cn, variable=var).pack(side=tk.LEFT, padx=3)

        # 日期
        self._date_frame = tk.Frame(d)
        self._date_frame.pack(fill=tk.X, **pad)
        tk.Label(self._date_frame, text="日期 (YYYY-MM-DD):", font=("微软雅黑", 10)).pack(anchor=tk.W)
        self._date_entry = tk.Entry(self._date_frame, font=("微软雅黑", 10), width=20)
        self._date_entry.pack(anchor=tk.W)

        # 初始状态
        self._on_type_change()

        # 时间
        tk.Label(d, text="时间 (HH:MM:SS):", font=("微软雅黑", 10)).pack(anchor=tk.W, **pad)
        time_frame = tk.Frame(d)
        time_frame.pack(fill=tk.X, **pad)
        self._hour = tk.Spinbox(time_frame, from_=0, to=23, width=4, font=("微软雅黑", 10),
                                format="%02.0f", justify=tk.CENTER)
        self._hour.pack(side=tk.LEFT)
        tk.Label(time_frame, text=":", font=("微软雅黑", 14)).pack(side=tk.LEFT, padx=2)
        self._minute = tk.Spinbox(time_frame, from_=0, to=59, width=4, font=("微软雅黑", 10),
                                  format="%02.0f", justify=tk.CENTER)
        self._minute.pack(side=tk.LEFT)
        tk.Label(time_frame, text=":", font=("微软雅黑", 14)).pack(side=tk.LEFT, padx=2)
        self._second = tk.Spinbox(time_frame, from_=0, to=59, width=4, font=("微软雅黑", 10),
                                  format="%02.0f", justify=tk.CENTER)
        self._second.pack(side=tk.LEFT)

        self._hour.delete(0, tk.END)
        self._hour.insert(0, "08")
        self._minute.delete(0, tk.END)
        self._minute.insert(0, "00")
        self._second.delete(0, tk.END)
        self._second.insert(0, "00")

        # 铃声文件
        tk.Label(d, text="铃声文件:", font=("微软雅黑", 10)).pack(anchor=tk.W, **pad)
        file_frame = tk.Frame(d)
        file_frame.pack(fill=tk.X, **pad)
        self._audio_file = tk.Entry(file_frame, font=("微软雅黑", 10), width=35)
        self._audio_file.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(file_frame, text="选择", command=self._select_file).pack(side=tk.RIGHT, padx=5)

        # 音量
        tk.Label(d, text="音量:", font=("微软雅黑", 10)).pack(anchor=tk.W, **pad)
        vol_frame = tk.Frame(d)
        vol_frame.pack(fill=tk.X, **pad)
        self._volume = tk.Scale(vol_frame, from_=0, to=100, orient=tk.HORIZONTAL, length=300)
        self._volume.set(80)
        self._volume.pack(side=tk.LEFT)
        self._vol_label = tk.Label(vol_frame, text="80%", font=("微软雅黑", 10))
        self._vol_label.pack(side=tk.LEFT, padx=5)
        self._volume.config(command=lambda v: self._vol_label.config(text=f"{int(v)}%"))

        # 重复次数（V2.1: 之前 Web 端可设 1~10 次，GUI 一保存就被重置为 1）
        tk.Label(d, text="重复次数:", font=("微软雅黑", 10)).pack(anchor=tk.W, **pad)
        count_frame = tk.Frame(d)
        count_frame.pack(fill=tk.X, **pad)
        self._play_count = tk.Spinbox(count_frame, from_=1, to=10, width=5,
                                      font=("微软雅黑", 10), justify=tk.CENTER)
        self._play_count.pack(side=tk.LEFT)
        tk.Label(count_frame, text="次（连续播放，1~10）", font=("微软雅黑", 9),
                 fg="#888").pack(side=tk.LEFT, padx=6)

        # 启用
        self._enabled_var = tk.BooleanVar(value=True)
        tk.Checkbutton(d, text="启用", variable=self._enabled_var,
                       font=("微软雅黑", 10)).pack(anchor=tk.W, **pad)

        # 回车键直接保存
        self.dialog.bind("<Return>", lambda e: self._save())

    def _on_type_change(self):
        stype = self._type_var.get()
        if stype == "weekly":
            self._week_frame.pack(fill=tk.X, padx=15, pady=5)
            self._date_frame.pack_forget()
        elif stype in ("date", "once"):
            self._week_frame.pack_forget()
            self._date_frame.pack(fill=tk.X, padx=15, pady=5)
        else:  # daily
            self._week_frame.pack_forget()
            self._date_frame.pack_forget()

    def _select_file(self):
        file_path = filedialog.askopenfilename(
            title="选择铃声文件",
            filetypes=[("音频文件", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac"), ("所有文件", "*.*")]
        )
        if file_path:
            self._audio_file.delete(0, tk.END)
            self._audio_file.insert(0, file_path)

    def _load_data(self):
        self._name.delete(0, tk.END)
        self._name.insert(0, self.data["name"])
        self._type_var.set(self.data["schedule_type"])
        self._on_type_change()

        if self.data["schedule_type"] == "weekly" and self.data["week_days"]:
            days = [int(x) for x in self.data["week_days"].split(",")]
            for ci, var in self._week_vars.items():
                var.set(ci in days)

        if self.data["date_str"]:
            self._date_entry.delete(0, tk.END)
            self._date_entry.insert(0, self.data["date_str"])

        parts = self.data["time_str"].split(":")
        if len(parts) == 3:
            self._hour.delete(0, tk.END)
            self._hour.insert(0, parts[0])
            self._minute.delete(0, tk.END)
            self._minute.insert(0, parts[1])
            self._second.delete(0, tk.END)
            self._second.insert(0, parts[2])

        self._audio_file.delete(0, tk.END)
        self._audio_file.insert(0, self.data["audio_file"])
        self._volume.set(self.data["volume"])
        self._vol_label.config(text=f"{self.data['volume']}%")
        self._play_count.delete(0, tk.END)
        self._play_count.insert(0, str(self.data["play_count"] or 1))
        self._enabled_var.set(bool(self.data["enabled"]))

    def _save(self):
        name = self._name.get().strip()
        if not name:
            messagebox.showwarning("警告", "请输入任务名称", parent=self.dialog)
            return

        audio = self._audio_file.get().strip()
        if not audio:
            messagebox.showwarning("警告", "请选择铃声文件", parent=self.dialog)
            return

        if not Path(audio).exists():
            if not messagebox.askyesno("确认", "音频文件不存在，是否继续？", parent=self.dialog):
                return

        stype = self._type_var.get()
        time_str = f"{int(float(self._hour.get())):02d}:{int(float(self._minute.get())):02d}:{int(float(self._second.get())):02d}"

        week_days = ""
        if stype == "weekly":
            days = [str(ci) for ci, var in self._week_vars.items() if var.get()]
            if not days:
                messagebox.showwarning("警告", "请至少选择一天", parent=self.dialog)
                return
            week_days = ",".join(days)

        date_str = ""
        if stype in ("date", "once"):
            date_str = self._date_entry.get().strip()
            if not date_str:
                messagebox.showwarning("警告", "请输入日期", parent=self.dialog)
                return
            try:
                datetime.datetime.strptime(date_str, "%Y-%m-%d")
            except:
                messagebox.showwarning("警告", "日期格式错误，请使用 YYYY-MM-DD", parent=self.dialog)
                return

        # V2.1: 编辑时以原始行为基底，只覆盖界面上真正可改的字段。
        # 旧实现把 play_count 硬编码为 1、把 profile_id 强制改成当前方案，
        # 导致"Web 设的播放次数被 GUI 重置"和"公共任务被私有化"两个数据丢失问题。
        try:
            play_count = max(1, min(10, int(float(self._play_count.get()))))
        except (TypeError, ValueError):
            play_count = 1

        data = build_schedule_payload(
            name=name, schedule_type=stype, time_str=time_str,
            week_days=week_days, date_str=date_str, audio_file=audio,
            volume=int(self._volume.get()), play_count=play_count,
            enabled=1 if self._enabled_var.get() else 0,
            base=self.data,
            active_profile_id=self.db.get_active_profile_id())

        if self.edit_mode:
            self.db.update_schedule(self.data["id"], data)
        else:
            self.db.add_schedule(data)

        self.on_save()
        self.dialog.destroy()


# ============================================================
# 方案管理对话框
# ============================================================
class ProfileDialog:
    def __init__(self, parent, db, on_save):
        self.db = db
        self.on_save = on_save

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("铃声方案管理")
        self.dialog.geometry("350x300")
        self.dialog.resizable(False, False)
        self.dialog.transient(parent)
        self.dialog.grab_set()

        self._build_ui()
        self.dialog.wait_window()

    def _build_ui(self):
        d = self.dialog
        tk.Label(d, text="铃声方案管理", font=("微软雅黑", 12, "bold")).pack(pady=10)

        self._listbox = tk.Listbox(d, font=("微软雅黑", 10), height=8)
        self._listbox.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)

        btn_frame = tk.Frame(d)
        btn_frame.pack(fill=tk.X, padx=15, pady=5)

        tk.Button(btn_frame, text="➕ 新建", command=self._add).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="🗑 删除", command=self._delete).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="✓ 切换", command=self._activate).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="关闭", command=self.dialog.destroy).pack(side=tk.RIGHT, padx=2)

        self._refresh_list()

    def _refresh_list(self):
        self._listbox.delete(0, tk.END)
        active_id = self.db.get_active_profile_id()
        for p in self.db.get_profiles():
            mark = " [当前]" if p["id"] == active_id else ""
            self._listbox.insert(tk.END, f"{p['name']}{mark}")

    def _add(self):
        name = simpledialog.askstring("新建方案", "请输入方案名称:", parent=self.dialog)
        if name:
            try:
                self.db.add_profile(name.strip())
                self._refresh_list()
                self.on_save()
            except sqlite3.IntegrityError:
                messagebox.showwarning("警告", "方案名称已存在", parent=self.dialog)

    def _delete(self):
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        profiles = self.db.get_profiles()
        if idx < len(profiles):
            pid = profiles[idx]["id"]
            if pid == self.db.get_active_profile_id():
                messagebox.showwarning("警告", "不能删除当前启用的方案", parent=self.dialog)
                return
            if messagebox.askyesno("确认", f"确定删除方案「{profiles[idx]['name']}」吗？", parent=self.dialog):
                self.db.delete_profile(pid)
                self._refresh_list()
                self.on_save()

    def _activate(self):
        sel = self._listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        profiles = self.db.get_profiles()
        if idx < len(profiles):
            self.db.activate_profile(profiles[idx]["id"])
            self._refresh_list()
            self.on_save()


# ============================================================
# 日志对话框
# ============================================================
class LogDialog:
    STATUS_TEXT = {
        "success": "✅ 成功",
        "failed": "❌ 失败",
        "missed": "⏰ 漏打",
        "paused": "⏸ 暂停跳过",
    }

    def __init__(self, parent, db):
        self.db = db

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("执行日志")
        self.dialog.geometry("760x420")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        self._filter_var = tk.StringVar(value="全部")
        self._build_ui()
        self._reload()
        self.dialog.wait_window()

    def _build_ui(self):
        d = self.dialog
        tk.Label(d, text="📋 执行日志", font=("微软雅黑", 12, "bold")).pack(pady=5)

        # V2.2: 状态筛选 + 导出 CSV
        top = tk.Frame(d)
        top.pack(fill=tk.X, padx=10)
        tk.Label(top, text="状态筛选:", font=("微软雅黑", 9)).pack(side=tk.LEFT)
        combo = ttk.Combobox(top, textvariable=self._filter_var, state="readonly", width=12,
                             values=["全部", "成功", "失败", "漏打", "暂停跳过"],
                             font=("微软雅黑", 9))
        combo.pack(side=tk.LEFT, padx=6)
        combo.bind("<<ComboboxSelected>>", lambda e: self._reload())
        tk.Button(top, text="导出 CSV", command=self._export).pack(side=tk.RIGHT, padx=2)

        columns = ("time", "name", "audio", "status", "detail")
        self._tree = ttk.Treeview(d, columns=columns, show="headings", height=15)
        for col, text, width in (("time", "执行时间", 160), ("name", "任务名称", 140),
                                 ("audio", "铃声文件", 190), ("status", "状态", 90),
                                 ("detail", "说明", 190)):
            self._tree.heading(col, text=text)
            self._tree.column(col, width=width,
                              anchor=tk.CENTER if col in ("status",) else tk.W)

        scrollbar = ttk.Scrollbar(d, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=5)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=5)

        tk.Button(d, text="关闭", command=self.dialog.destroy).pack(pady=5)

    def _rows(self):
        want = self._filter_var.get()
        key = {"成功": "success", "失败": "failed", "漏打": "missed", "暂停跳过": "paused"}.get(want)
        rows = self.db.get_logs(500)
        if key:
            rows = [r for r in rows if r["status"] == key]
        return rows

    def _reload(self):
        for item in self._tree.get_children():
            self._tree.delete(item)
        for log in self._rows():
            self._tree.insert("", tk.END, values=(
                log["execute_time"], log["schedule_name"],
                Path(log["audio_file"] or "").name,
                self.STATUS_TEXT.get(log["status"], log["status"]),
                log["error_message"] or ""
            ))

    def _export(self):
        path = filedialog.asksaveasfilename(
            title="导出执行日志", defaultextension=".csv",
            initialfile="bell_logs_%s.csv" % datetime.date.today().isoformat(),
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            import csv
            rows = self._rows()
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["执行时间", "任务名称", "铃声文件", "状态", "说明"])
                for log in rows:
                    w.writerow([log["execute_time"], log["schedule_name"], log["audio_file"],
                                self.STATUS_TEXT.get(log["status"], log["status"]),
                                log["error_message"] or ""])
            messagebox.showinfo("导出完成", "已导出 %d 条日志到：\n%s" % (len(rows), path),
                                parent=self.dialog)
        except Exception as e:
            messagebox.showerror("导出失败", str(e), parent=self.dialog)


# ============================================================
# 日历（节假日 / 调休补课）对话框 —— V2.2
# ============================================================
class CalendarDialog:
    KIND_TEXT = {"holiday": "放假（全天不打铃）", "workday": "上课（照常打铃）"}

    def __init__(self, parent, db, on_save):
        self.db = db
        self.on_save = on_save

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("节假日 / 调休日历")
        self.dialog.geometry("560x480")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        self._build_ui()
        self._reload()
        self.dialog.wait_window()

    def _build_ui(self):
        d = self.dialog
        tk.Label(d, text="📅 节假日 / 调休日历", font=("微软雅黑", 12, "bold")).pack(pady=8)
        tk.Label(d, text="放假：该日全天不打铃　|　上课：该日照常打铃，可指定「按星期几的课表」（调休补课）",
                 font=("微软雅黑", 8), fg="#888").pack(anchor=tk.W, padx=12)

        form = tk.Frame(d)
        form.pack(fill=tk.X, padx=12, pady=6)

        row1 = tk.Frame(form)
        row1.pack(fill=tk.X, pady=2)
        tk.Label(row1, text="日期:", font=("微软雅黑", 9)).pack(side=tk.LEFT)
        self._day_var = tk.StringVar(value=datetime.date.today().isoformat())
        tk.Entry(row1, textvariable=self._day_var, width=12, font=("微软雅黑", 9)).pack(side=tk.LEFT, padx=4)
        tk.Label(row1, text="(YYYY-MM-DD)", font=("微软雅黑", 8), fg="#999").pack(side=tk.LEFT)

        row2 = tk.Frame(form)
        row2.pack(fill=tk.X, pady=2)
        self._kind_var = tk.StringVar(value="holiday")
        tk.Radiobutton(row2, text="放假（不打铃）", variable=self._kind_var, value="holiday",
                       font=("微软雅黑", 9)).pack(side=tk.LEFT)
        tk.Radiobutton(row2, text="上课（照常打铃）", variable=self._kind_var, value="workday",
                       font=("微软雅黑", 9)).pack(side=tk.LEFT, padx=8)

        row3 = tk.Frame(form)
        row3.pack(fill=tk.X, pady=2)
        tk.Label(row3, text="按星期:", font=("微软雅黑", 9)).pack(side=tk.LEFT)
        self._mirror_var = tk.StringVar(value="不指定")
        ttk.Combobox(row3, textvariable=self._mirror_var, state="readonly", width=10,
                     values=["不指定", "周一", "周二", "周三", "周四", "周五", "周六", "周日"],
                     font=("微软雅黑", 9)).pack(side=tk.LEFT, padx=4)
        tk.Label(row3, text="备注:", font=("微软雅黑", 9)).pack(side=tk.LEFT, padx=(8, 0))
        self._note_var = tk.StringVar()
        tk.Entry(row3, textvariable=self._note_var, width=16, font=("微软雅黑", 9)).pack(side=tk.LEFT, padx=4)

        btns = tk.Frame(d)
        btns.pack(fill=tk.X, padx=12, pady=4)
        tk.Button(btns, text="➕ 添加/覆盖", command=self._add).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="🗑 删除选中", command=self._delete).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="关闭", command=self.dialog.destroy).pack(side=tk.RIGHT, padx=2)

        self._listbox = tk.Listbox(d, font=("微软雅黑", 9), height=12)
        self._listbox.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

    def _reload(self):
        self._listbox.delete(0, tk.END)
        self._days = []
        cal = self.db.get_calendar()
        for day in sorted(cal.keys()):
            info = cal[day]
            self._days.append(day)
            kind = self.KIND_TEXT.get(info["kind"], info["kind"])
            mirror = ""
            if info["kind"] == "workday" and 0 <= info.get("mirror_weekday", -1) <= 6:
                mirror = " · 按周%s" % "一二三四五六日"[info["mirror_weekday"]]
            note = (" · " + info["note"]) if info.get("note") else ""
            self._listbox.insert(tk.END, "%s  %s%s%s" % (day, kind, mirror, note))

    def _add(self):
        day = (self._day_var.get() or "").strip()
        try:
            datetime.datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            messagebox.showwarning("日期格式错误", "请使用 YYYY-MM-DD", parent=self.dialog)
            return
        mirror = ["不指定", "周一", "周二", "周三", "周四", "周五", "周六", "周日"].index(
            self._mirror_var.get() or "不指定") - 1
        self.db.set_calendar_day(day, self._kind_var.get(), mirror, (self._note_var.get() or "").strip())
        self._reload()
        self.on_save()

    def _delete(self):
        sel = self._listbox.curselection()
        if not sel:
            return
        day = self._days[sel[0]]
        if not messagebox.askyesno("确认", "删除 %s 的日历设置？" % day, parent=self.dialog):
            return
        self.db.delete_calendar_day(day)
        self._reload()
        self.on_save()


# ============================================================
# 课表 CSV 导入 / 导出对话框 —— V2.2
# ============================================================
class ScheduleIODialog:
    def __init__(self, parent, db, on_save):
        self.db = db
        self.on_save = on_save

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("课表导入 / 导出")
        self.dialog.geometry("620x460")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        self._build_ui()
        self.dialog.wait_window()

    def _build_ui(self):
        d = self.dialog
        tk.Label(d, text="📥 课表导入 / 导出（CSV）", font=("微软雅黑", 12, "bold")).pack(pady=8)
        tk.Label(d, text="CSV 用 Excel 可直接打开（UTF-8 BOM，不乱码）。列：%s"
                 % ",".join(CSV_HEADER), font=("微软雅黑", 8), fg="#888").pack(anchor=tk.W, padx=12)
        tk.Label(d, text="星期可写 0,2,4 或 一,三,五；导入一律进入「当前方案」。",
                 font=("微软雅黑", 8), fg="#888").pack(anchor=tk.W, padx=12, pady=(0, 6))

        btns = tk.Frame(d)
        btns.pack(fill=tk.X, padx=12)
        tk.Button(btns, text="📤 导出当前方案", command=self._export).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="📥 从 CSV 导入", command=self._import).pack(side=tk.LEFT, padx=2)
        tk.Button(btns, text="关闭", command=self.dialog.destroy).pack(side=tk.RIGHT, padx=2)

        self._text = tk.Text(d, font=("Consolas", 9), wrap=tk.WORD)
        self._text.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

    def _log(self, msg):
        self._text.insert(tk.END, msg + "\n")
        self._text.see(tk.END)

    def _export(self):
        path = filedialog.asksaveasfilename(
            title="导出课表", defaultextension=".csv",
            initialfile="bell_schedule_%s.csv" % datetime.date.today().isoformat(),
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            n = export_schedules_csv(self.db, path)
            self._log("✅ 已导出 %d 条任务 → %s" % (n, path))
        except Exception as e:
            self._log("❌ 导出失败：%s" % e)

    def _import(self):
        path = filedialog.askopenfilename(
            title="选择课表 CSV", filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            ok, errors, warnings = import_schedules_csv(self.db, path)
        except Exception as e:
            self._log("❌ 导入失败：%s" % e)
            return
        self._log("导入完成：成功 %d 条，错误 %d 条，警告 %d 条" % (ok, len(errors), len(warnings)))
        for w in warnings[:20]:
            self._log("  ⚠ " + w)
        for e in errors[:20]:
            self._log("  ✗ " + e)
        if ok:
            self.on_save()


# ============================================================
# 系统设置对话框
# ============================================================
class SettingsDialog:
    def __init__(self, parent):
        self.saved = False  # V2.0: BellApp 据此判断需要重启 Web 服务
        self.settings = self._load_settings()

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("系统设置")
        self.dialog.geometry("420x500")
        self.dialog.resizable(False, False)
        self.dialog.transient(parent)
        self.dialog.grab_set()

        self._build_ui()
        self.dialog.wait_window()

    def _load_settings(self) -> dict:
        return load_settings()

    def _save_settings(self):
        save_settings(self.settings)

    def _build_ui(self):
        d = self.dialog
        tk.Label(d, text="⚙ 系统设置", font=("微软雅黑", 12, "bold")).pack(pady=10)

        self._auto_start_var = tk.BooleanVar(value=self.settings.get("auto_start", False))
        cb1 = tk.Checkbutton(d, text="Windows 启动时自动运行", variable=self._auto_start_var,
                             font=("微软雅黑", 10))
        cb1.pack(anchor=tk.W, padx=20, pady=5)

        self._prevent_sleep_var = tk.BooleanVar(value=self.settings.get("prevent_sleep", False))
        cb2 = tk.Checkbutton(d, text="运行期间禁止系统自动休眠", variable=self._prevent_sleep_var,
                             font=("微软雅黑", 10))
        cb2.pack(anchor=tk.W, padx=20, pady=5)

        self._minimize_tray_var = tk.BooleanVar(value=self.settings.get("minimize_to_tray", True))
        cb3 = tk.Checkbutton(d, text="关闭窗口时最小化到系统托盘", variable=self._minimize_tray_var,
                             font=("微软雅黑", 10))
        cb3.pack(anchor=tk.W, padx=20, pady=5)

        # ---- 🌐 Web 远程控制 (V2.0) ----
        tk.Label(d, text="🌐 Web 远程控制", font=("微软雅黑", 10, "bold"),
                 fg="#2196F3").pack(anchor=tk.W, padx=20, pady=(16, 2))

        self._web_enabled_var = tk.BooleanVar(value=self.settings.get("web_enabled", False))
        tk.Checkbutton(d, text="启用局域网网页远程控制（手机/其他电脑可操作）",
                       variable=self._web_enabled_var,
                       font=("微软雅黑", 10)).pack(anchor=tk.W, padx=20, pady=2)

        port_row = tk.Frame(d)
        port_row.pack(anchor=tk.W, padx=20, pady=2)
        tk.Label(port_row, text="端口:", font=("微软雅黑", 10)).pack(side=tk.LEFT)
        self._web_port_var = tk.StringVar(value=str(self.settings.get("web_port", 8787)))
        tk.Entry(port_row, textvariable=self._web_port_var, width=7,
                 font=("微软雅黑", 10), justify=tk.CENTER).pack(side=tk.LEFT, padx=6)

        token_row = tk.Frame(d)
        token_row.pack(anchor=tk.W, padx=20, pady=2)
        tk.Label(token_row, text="访问令牌:", font=("微软雅黑", 10)).pack(side=tk.LEFT)
        self._web_token_var = tk.StringVar(value=str(self.settings.get("web_token", "")))
        tk.Entry(token_row, textvariable=self._web_token_var, width=16,
                 font=("微软雅黑", 10), show="•").pack(side=tk.LEFT, padx=6)
        tk.Label(d, text="令牌留空不加密；设置后打开网页需输入一次", fg="#999",
                 font=("微软雅黑", 8)).pack(anchor=tk.W, padx=20)

        # V2.1: 到点容差（超出即记为"漏打"，不再静默丢弃）
        tol_row = tk.Frame(d)
        tol_row.pack(anchor=tk.W, padx=20, pady=(8, 2))
        tk.Label(tol_row, text="漏打容差:", font=("微软雅黑", 10)).pack(side=tk.LEFT)
        self._tolerance_var = tk.StringVar(
            value=str(self.settings.get("missed_tolerance_sec", 60)))
        tk.Entry(tol_row, textvariable=self._tolerance_var, width=7,
                 font=("微软雅黑", 10), justify=tk.CENTER).pack(side=tk.LEFT, padx=6)
        tk.Label(tol_row, text="秒（到点后超过此时长记为漏打）", fg="#999",
                 font=("微软雅黑", 8)).pack(side=tk.LEFT)

        tk.Label(d, text="", font=("微软雅黑", 10)).pack(pady=10)

        tk.Button(d, text="保存设置", font=("微软雅黑", 10), width=12,
                  command=self._save).pack(pady=5)
        tk.Button(d, text="取消", font=("微软雅黑", 10), width=12,
                  command=self.dialog.destroy).pack(pady=2)

    def _save(self):
        self.settings["auto_start"] = self._auto_start_var.get()
        self.settings["prevent_sleep"] = self._prevent_sleep_var.get()
        self.settings["minimize_to_tray"] = self._minimize_tray_var.get()
        self.settings["web_enabled"] = bool(self._web_enabled_var.get())
        try:
            self.settings["web_port"] = max(1, min(65535, int(self._web_port_var.get() or 8787)))
        except ValueError:
            self.settings["web_port"] = 8787
        self.settings["web_token"] = self._web_token_var.get().strip()
        try:
            self.settings["missed_tolerance_sec"] = max(
                5, min(3600, int(self._tolerance_var.get() or 60)))
        except ValueError:
            self.settings["missed_tolerance_sec"] = 60
        self.saved = True
        self._save_settings()

        # V2.1: 启用远程控制但没设令牌时，明确告知风险（不强制阻止，尊重用户选择）
        if self.settings["web_enabled"] and not self.settings["web_token"]:
            if not messagebox.askyesno(
                    "安全提示",
                    "你启用了「局域网网页远程控制」但没有设置访问令牌。\n\n"
                    "这意味着同一局域网内的任何设备（手机、电脑）打开网页\n"
                    "都能添加/删除打铃任务、立即打铃。\n\n"
                    "建议设置一个访问令牌。是否仍然继续保存？",
                    parent=self.dialog):
                self.saved = False
                return

        # 设置开机自启
        if self.settings["auto_start"]:
            self._set_auto_start(True)
        else:
            self._set_auto_start(False)

        # 阻止休眠
        if self.settings["prevent_sleep"]:
            self._set_prevent_sleep(True)
        else:
            self._set_prevent_sleep(False)

        messagebox.showinfo("提示", "设置已保存", parent=self.dialog)
        self.dialog.destroy()

    def _set_auto_start(self, enable: bool):
        """设置开机自启 (通过注册表)"""
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE
            )
            if enable:
                exe_path = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
                winreg.SetValueEx(key, "BellSystem", 0, winreg.REG_SZ, f'"{exe_path}"')
            else:
                try:
                    winreg.DeleteValue(key, "BellSystem")
                except:
                    pass
            winreg.CloseKey(key)
        except Exception as e:
            logger.warning(f"设置开机自启失败: {e}")

    def _set_prevent_sleep(self, enable: bool):
        """阻止系统休眠"""
        try:
            if enable:
                subprocess.run(["powercfg", "-change", "-standby-timeout-ac", "0"],
                               capture_output=True, creationflags=SUBPROCESS_FLAGS)
                subprocess.run(["powercfg", "-change", "-hibernate-timeout-ac", "0"],
                               capture_output=True, creationflags=SUBPROCESS_FLAGS)
            else:
                # 恢复默认 30分钟
                subprocess.run(["powercfg", "-change", "-standby-timeout-ac", "30"],
                               capture_output=True, creationflags=SUBPROCESS_FLAGS)
        except Exception as e:
            logger.warning(f"设置电源策略失败: {e}")


# ============================================================
# 启动入口
# ============================================================
def main():
    logger.info("=" * 50)
    logger.info("🔔 智能打铃系统启动")

    # 初始化各组件
    db = Database(DB_PATH)
    player = AudioPlayer()
    scheduler = Scheduler(db, player)

    # 启动定时引擎
    scheduler.start()

    # 启动 GUI
    app = BellApp(db, player, scheduler)
    app.run()

    # 关闭（V2.1: 托盘"退出"也走这里，不再 os._exit 硬杀，避免 WAL 残留）
    try:
        app._stop_web_server()
    except Exception:
        pass
    scheduler.stop()
    try:
        db.close()
    except Exception:
        pass
    logger.info("程序退出")


if __name__ == "__main__":
    main()
