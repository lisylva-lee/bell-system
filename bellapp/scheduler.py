# -*- coding: utf-8 -*-
"""定时调度引擎（绝对时间轮询 + 日历感知）"""
import time
import datetime
import threading
import logging
from typing import Optional, List, Tuple

from .util import _row_get
from .settings import load_settings
from .storage import Database
from .player import AudioPlayer

logger = logging.getLogger("BellSystem.Scheduler")

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
