#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
🔔 智能打铃系统 BellSystem
Windows 7/10/11 通用
基于 Python + tkinter + pygame + SQLite
"""

import os
import sys
import json
import time
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

    def close(self):
        self.conn.close()


# ============================================================
# 音频播放引擎
# ============================================================
class AudioPlayer:
    def __init__(self):
        self._playing = False
        self._stop_flag = False
        self._current_file = None
        if pygame:
            try:
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=4096)
            except Exception as e:
                logger.warning(f"pygame.mixer 初始化失败: {e}")

    def play(self, file_path: str, volume: int = 80, count: int = 1) -> bool:
        """播放音频文件，返回是否成功"""
        if not pygame:
            logger.error("pygame 未安装，无法播放音频")
            return False

        path = Path(file_path)
        if not path.exists():
            logger.error(f"音频文件不存在: {file_path}")
            return False

        try:
            # 尝试用 ffmpeg 转换非 WAV 格式
            ext = path.suffix.lower()
            if ext not in (".wav",):
                wav_path = DATA_DIR / f"_play_temp_{path.stem}.wav"
                if not self._convert_to_wav(str(path), str(wav_path)):
                    # 直接尝试 pygame 播放
                    pass
                else:
                    path = wav_path

            pygame.mixer.music.load(str(path))
            pygame.mixer.music.set_volume(max(0.0, min(1.0, volume / 100.0)))
            self._current_file = str(path)
            self._stop_flag = False
            self._playing = True

            for _ in range(count):
                if self._stop_flag:
                    break
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    if self._stop_flag:
                        pygame.mixer.music.stop()
                        break
                    time.sleep(0.1)

            self._playing = False
            # 清理临时文件
            if path != Path(file_path):
                try:
                    path.unlink()
                except:
                    pass
            return True
        except Exception as e:
            logger.error(f"播放失败: {e}")
            self._playing = False
            return False

    def play_async(self, file_path: str, volume: int = 80, count: int = 1, callback=None):
        """异步播放"""
        def _run():
            result = self.play(file_path, volume, count)
            if callback:
                callback(result)
        threading.Thread(target=_run, daemon=True).start()

    def stop(self):
        """停止播放"""
        self._stop_flag = True
        if pygame and pygame.mixer.get_init():
            try:
                pygame.mixer.music.stop()
            except:
                pass
        self._playing = False

    def is_playing(self) -> bool:
        return self._playing

    def _convert_to_wav(self, src: str, dst: str) -> bool:
        """用 ffmpeg 转 WAV"""
        ffmpeg_path = FFMPEG_DIR / "ffmpeg.exe"
        if not ffmpeg_path.exists():
            # 尝试系统 PATH 中的 ffmpeg
            ffmpeg_path = "ffmpeg"
        try:
            subprocess.run(
                [str(ffmpeg_path), "-y", "-i", src, "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", dst],
                capture_output=True, timeout=30
            )
            return Path(dst).exists()
        except Exception as e:
            logger.warning(f"ffmpeg 转换失败: {e}")
            return False


# ============================================================
# 定时引擎
# ============================================================
class Scheduler:
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
        return self._next_time

    def get_next_info(self):
        """返回 (下一次执行的具体时间 datetime, 任务名称)，供界面精确计算距离"""
        return self._next_dt, self._next_task_name

    def _calculate_next(self, now: datetime.datetime, schedules: list) -> Tuple[Optional[datetime.datetime], Optional[dict]]:
        """计算下一次执行时间和对应的任务"""
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
                # 每天
                t = now.replace(hour=h, minute=m, second=sec, microsecond=0)
                if t <= now:
                    t += datetime.timedelta(days=1)
                candidates.append((t, s))

            elif stype == "weekly":
                # 每周指定星期
                if not s["week_days"]:
                    continue
                week_days = [int(x) for x in s["week_days"].split(",")]
                for offset in range(14):  # 往后看14天
                    t = now + datetime.timedelta(days=offset)
                    t = t.replace(hour=h, minute=m, second=sec, microsecond=0)
                    if t <= now:
                        continue
                    # Python weekday: Monday=0, Sunday=6
                    if t.weekday() in week_days:
                        candidates.append((t, s))
                        break

            elif stype == "date":
                # 指定日期
                if not s["date_str"]:
                    continue
                try:
                    t = datetime.datetime.strptime(s["date_str"], "%Y-%m-%d")
                    t = t.replace(hour=h, minute=m, second=sec, microsecond=0)
                    if t > now:
                        candidates.append((t, s))
                except:
                    continue

            elif stype == "once":
                # 一次性
                if not s["date_str"]:
                    continue
                try:
                    t = datetime.datetime.strptime(s["date_str"], "%Y-%m-%d")
                    t = t.replace(hour=h, minute=m, second=sec, microsecond=0)
                    if t > now:
                        candidates.append((t, s))
                except:
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
        """单个调度回合：计算下一次、更新显示、等待、到点执行"""
        self._resync.clear()
        now = datetime.datetime.now()
        active_profile = self.db.get_active_profile_id()
        schedules = self.db.get_schedules(active_profile)

        next_time, next_task = self._calculate_next(now, schedules)

        if next_time is None:
            # 没有任务：清空显示，分段睡眠以便及时响应新增任务/暂停
            self._next_dt = None
            self._next_task_name = None
            self._next_time = None
            self._notify("no_schedule")
            for _ in range(30):
                if not self._running or self._resync.is_set():
                    break
                time.sleep(1)
            return

        # 更新显示信息（精确到具体时刻，供界面计算真实距离）
        self._next_dt = next_time
        self._next_task_name = next_task["name"] if next_task else ""
        self._next_task_id = next_task["id"] if next_task else None
        self._next_time = next_time.strftime("%H:%M:%S")
        self._notify("next_time", time=self._next_time, dt=next_time, name=self._next_task_name)

        # 计算等待秒数
        wait_seconds = (next_time - now).total_seconds()
        if wait_seconds <= 0:
            wait_seconds = 1

        # 等待，每秒检查；每5秒重新计算（应对任务新增/修改/停用）
        waited = 0
        while waited < wait_seconds and self._running:
            if self._pause:
                time.sleep(1)
                waited += 1
                continue
            time.sleep(1)
            waited += 1
            if self._resync.is_set():
                self._resync.clear()
                return  # 数据已变化，重新规划
            if waited % 5 == 0:
                now2 = datetime.datetime.now()
                # 距目标时间 <=6秒时不重算：此时重算会把到点的 daily 任务推到明天，
                # 返回的下一个任务 id 变化导致误判 REPLAN，白等一轮（用户遇到的核心 bug）
                if self._next_dt and (self._next_dt - now2).total_seconds() <= 6:
                    continue
                schedules2 = self.db.get_schedules(self.db.get_active_profile_id())
                nt2, task2 = self._calculate_next(now2, schedules2)
                if task2 is not None:
                    # 同一任务（id相同）只是时间滚动到第二天，不触发 REPLAN
                    same_task = task2["id"] == self._next_task_id
                    if nt2 != self._next_dt and not same_task:
                        return  # 计划有变化，重新调度

        if not self._running or self._pause:
            return

        # 执行打铃
        now = datetime.datetime.now()
        if abs((now - next_time).total_seconds()) <= 5:  # 5秒误差内
            self._execute_task(next_task)
        else:
            logger.info(f"错过执行时间，跳过: {next_task['name']}")

    def _execute_task(self, task: dict):
        """执行一个打铃任务"""
        name = task["name"]
        audio = task["audio_file"]
        volume = task["volume"]
        play_count = task["play_count"]

        logger.info(f"🔔 执行打铃: {name} → {audio}")

        # 先通知
        self._notify("ringing", name=name, audio=audio)

        # 异步播放
        def on_play(result):
            status = "success" if result else "failed"
            error = "" if result else "播放失败"
            self.db.add_log(task["id"], name, audio, status, error)
            self._notify("ring_done", name=name, result=status)

        self.player.play_async(audio, volume, play_count, callback=on_play)

        # 一次性任务执行后自动删除
        if task["schedule_type"] == "once":
            self.db.delete_schedule(task["id"])
            logger.info(f"一次性任务已删除: {name}")


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

        # 标题行
        tk.Label(list_frame, text="📋 打铃任务列表", font=("微软雅黑", 11, "bold"),
                 bg=self.bg_color).pack(anchor=tk.W)

        # Treeview
        columns = ("enabled", "time", "week", "name", "audio", "volume", "type")
        self._tree = ttk.Treeview(list_frame, columns=columns, show="headings",
                                   height=10, selectmode="browse")

        self._tree.heading("enabled", text="启用")
        self._tree.heading("time", text="时间")
        self._tree.heading("week", text="星期")
        self._tree.heading("name", text="任务名称")
        self._tree.heading("audio", text="铃声文件")
        self._tree.heading("volume", text="音量")
        self._tree.heading("type", text="类型")

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

        for s in schedules:
            enabled = "✓" if s["enabled"] else "☐"
            stype_map = {"daily": "每天", "weekly": "每周", "date": "指定日期", "once": "一次性"}
            stype = stype_map.get(s["schedule_type"], s["schedule_type"])

            week_str = ""
            if s["schedule_type"] == "weekly" and s["week_days"]:
                days = [week_map.get(int(x), "?") for x in s["week_days"].split(",")]
                week_str = "周" + "、".join(days)
            elif s["schedule_type"] == "date" and s["date_str"]:
                week_str = s["date_str"]
            elif s["schedule_type"] == "once" and s["date_str"]:
                week_str = s["date_str"]

            self._tree.insert("", tk.END, iid=str(s["id"]), values=(
                enabled, s["time_str"], week_str, s["name"],
                Path(s["audio_file"]).name, f"{s['volume']}%", stype
            ))

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

    # ---- 任务操作 ----
    def _add_schedule(self):
        ScheduleDialog(self.root, self.db, self._after_task_change)

    def _edit_schedule(self):
        selected = self._tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先选择一个任务")
            return
        sid = int(selected[0])
        row = self.db.conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
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
        row = self.db.conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
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
            for s in enabled:
                logger.info(f"立即打铃: {s['name']}")
                self.player.play(s["audio_file"], s["volume"], s["play_count"])
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
            self._web = BellWebServer(
                self.db, self.player, self.scheduler,
                port=int(settings.get("web_port") or 8787),
                token=str(settings.get("web_token") or ""),
                on_change=lambda: self._web_events.put(1))
            self._web.start()
            urls = self._web.get_urls()
            logger.info("🌐 远程控制地址: %s", " , ".join(urls))
            self._update_web_label(urls[0])
            if show_error and urls:
                messagebox.showinfo(
                    "远程控制已启动",
                    "同一局域网内的手机/电脑用浏览器打开：\n\n" + "\n".join(urls),
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
            self._web_label.config(text=f"🌐 远程控制: {url}" if url else "")
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
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse([8, 8, 56, 56], fill="#FF9800")
            draw.text((20, 18), "🔔", font=None, fill="white")
            return img

        def on_open(icon, item):
            self._show_window()

        def on_quit(icon, item):
            icon.stop()
            self.root.quit()
            os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem("打开主界面", on_open, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("立即打铃", lambda: self._immediate_ring()),
            pystray.MenuItem("暂停打铃", lambda: self.scheduler.pause()),
            pystray.MenuItem("恢复打铃", lambda: self.scheduler.resume()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开网页控制台", lambda: self._open_web_ui()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", on_quit),
        )

        self._tray = pystray.Icon("BellSystem", create_image(), "智能打铃系统", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()

    def _minimize_to_tray(self):
        self.root.withdraw()

    def _on_close(self):
        """关闭窗口时最小化到托盘"""
        if self._tray:
            self._minimize_to_tray()
        else:
            self.root.quit()

    def _bind_shortcuts(self):
        self.root.bind("<Escape>", lambda e: self._minimize_to_tray())

    def run(self):
        self.root.mainloop()


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

        data = {
            "name": name,
            "schedule_type": stype,
            "time_str": time_str,
            "week_days": week_days,
            "date_str": date_str,
            "audio_file": audio,
            "volume": int(self._volume.get()),
            "play_count": 1,
            "enabled": 1 if self._enabled_var.get() else 0,
            "profile_id": self.db.get_active_profile_id(),
        }

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
    def __init__(self, parent, db):
        self.db = db

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("执行日志")
        self.dialog.geometry("700x400")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        self._build_ui()
        self.dialog.wait_window()

    def _build_ui(self):
        d = self.dialog
        tk.Label(d, text="📋 执行日志", font=("微软雅黑", 12, "bold")).pack(pady=5)

        columns = ("time", "name", "audio", "status")
        tree = ttk.Treeview(d, columns=columns, show="headings", height=15)
        tree.heading("time", text="执行时间")
        tree.heading("name", text="任务名称")
        tree.heading("audio", text="铃声文件")
        tree.heading("status", text="状态")
        tree.column("time", width=170)
        tree.column("name", width=150)
        tree.column("audio", width=250)
        tree.column("status", width=80, anchor=tk.CENTER)

        scrollbar = ttk.Scrollbar(d, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=5)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=5)

        for log in self.db.get_logs(200):
            status_text = "✅ 成功" if log["status"] == "success" else "❌ 失败"
            tree.insert("", tk.END, values=(
                log["execute_time"], log["schedule_name"],
                Path(log["audio_file"]).name, status_text
            ))

        tk.Button(d, text="关闭", command=self.dialog.destroy).pack(pady=5)


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
        self.saved = True
        self._save_settings()

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
                subprocess.run(["powercfg", "-change", "-standby-timeout-ac", "0"], capture_output=True)
                subprocess.run(["powercfg", "-change", "-hibernate-timeout-ac", "0"], capture_output=True)
            else:
                # 恢复默认 30分钟
                subprocess.run(["powercfg", "-change", "-standby-timeout-ac", "30"], capture_output=True)
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

    # 关闭
    app._stop_web_server()
    scheduler.stop()
    db.close()
    logger.info("程序退出")


if __name__ == "__main__":
    main()