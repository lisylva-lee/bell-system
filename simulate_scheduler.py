#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""调试版调度模拟"""
import datetime, sqlite3, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

class FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        c = self.conn.cursor()
        c.execute("""CREATE TABLE schedules (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, schedule_type TEXT, time_str TEXT, week_days TEXT,
            date_str TEXT, audio_file TEXT, volume INTEGER, play_count INTEGER,
            enabled INTEGER, profile_id INTEGER DEFAULT 0, created_at TEXT)""")
        c.execute("""CREATE TABLE profiles (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE, is_active INTEGER DEFAULT 0, created_at TEXT)""")
        c.execute("""CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id INTEGER, schedule_name TEXT, audio_file TEXT,
            execute_time TEXT, status TEXT, error_message TEXT)""")
        c.execute("INSERT INTO profiles (name, is_active) VALUES ('默认', 1)")
        self.conn.commit()
    def get_schedules(self, profile_id=0):
        if profile_id > 0:
            return self.conn.execute("SELECT * FROM schedules WHERE profile_id=? OR profile_id=0 ORDER BY time_str", (profile_id,)).fetchall()
        return self.conn.execute("SELECT * FROM schedules ORDER BY time_str").fetchall()
    def add_schedule(self, data):
        c = self.conn.cursor()
        c.execute("""INSERT INTO schedules (name, schedule_type, time_str, week_days, date_str,
            audio_file, volume, play_count, enabled, profile_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (data["name"], data["schedule_type"], data["time_str"], data.get("week_days",""),
             data.get("date_str",""), data["audio_file"], data.get("volume",80),
             data.get("play_count",1), data.get("enabled",1), data.get("profile_id",0)))
        self.conn.commit(); return c.lastrowid
    def delete_schedule(self, sid):
        self.conn.execute("DELETE FROM schedules WHERE id=?", (sid,)); self.conn.commit()
    def get_active_profile_id(self):
        r = self.conn.execute("SELECT id FROM profiles WHERE is_active=1").fetchone()
        return r["id"] if r else 0
    def add_log(self, *a):
        self.conn.execute("INSERT INTO logs (schedule_id,schedule_name,audio_file,execute_time,status) VALUES (?,?,?,?,?)",
                          (a[0] if len(a)>0 else 0, '', '', datetime.datetime.now().strftime('%H:%M:%S'), 'success'))
        self.conn.commit()

class FakePlayer:
    def __init__(self): self.played = []
    def play_async(self, file_path, volume=80, count=1, callback=None):
        self.played.append((file_path, volume))

class Scheduler:
    def __init__(self, db, player):
        self.db = db; self.player = player
        self._next_dt = None; self._next_time = None; self._next_task_name = None
        self._next_task_id = None
    
    def _calculate_next(self, now, schedules):
        candidates = []
        for s in schedules:
            if not s["enabled"]: continue
            h, m, sec = map(int, s["time_str"].split(":"))
            stype = s["schedule_type"]
            if stype == "daily":
                t = now.replace(hour=h, minute=m, second=sec, microsecond=0)
                if t <= now: t += datetime.timedelta(days=1)
                candidates.append((t, s))
            elif stype == "weekly":
                if not s["week_days"]: continue
                week_days = [int(x) for x in s["week_days"].split(",")]
                for offset in range(14):
                    t = now + datetime.timedelta(days=offset)
                    t = t.replace(hour=h, minute=m, second=sec, microsecond=0)
                    if t <= now: continue
                    if t.weekday() in week_days:
                        candidates.append((t, s)); break
            elif stype in ("date", "once"):
                if not s["date_str"]: continue
                try:
                    t = datetime.datetime.strptime(s["date_str"], "%Y-%m-%d")
                    t = t.replace(hour=h, minute=m, second=sec, microsecond=0)
                    if t > now: candidates.append((t, s))
                except: continue
        if not candidates: return None, None
        candidates.sort(key=lambda x: x[0])
        return candidates[0]

    def simulate(self, start_time, rounds=50):
        t = start_time
        executed = []
        for r in range(rounds):
            schedules = self.db.get_schedules(self.db.get_active_profile_id())
            next_time, next_task = self._calculate_next(t, schedules)
            if next_time is None:
                print(f"[{t.strftime('%H:%M:%S')}] 无任务, 等30s")
                t += datetime.timedelta(seconds=30); continue

            # 首次进入: 设置 _next_dt
            self._next_dt = next_time
            self._next_task_id = next_task["id"]
            wait_seconds = (next_time - t).total_seconds()
            if wait_seconds <= 0: wait_seconds = 1

            # 模拟等待
            waited = 0
            while waited < wait_seconds:
                t += datetime.timedelta(seconds=1)
                waited += 1
                if waited % 5 == 0:
                    # 距目标时间 <=6秒时不重算（避免到点瞬间把 daily 任务推到明天误判 REPLAN）
                    if self._next_dt and (self._next_dt - t).total_seconds() <= 6:
                        continue
                    schedules2 = self.db.get_schedules(self.db.get_active_profile_id())
                    nt2, task2 = self._calculate_next(t, schedules2)
                    if task2 is not None:
                        # 同一任务（id相同）只是时间滚动到第二天，不触发 REPLAN
                        same_task = task2["id"] == self._next_task_id
                        if nt2 != self._next_dt and not same_task:
                            print(f"  [REPLAN at {t.strftime('%H:%M:%S')}] nt2={nt2} != _next_dt={self._next_dt}")
                            break  # 退出 while 重新调度
            else:
                # 没 break：到点执行
                if abs((t - next_time).total_seconds()) <= 5:
                    print(f"  ✅ [{t.strftime('%H:%M:%S')}] 执行: {next_task['name']} (next={next_time})")
                    executed.append(next_task["time_str"])
                    # 异步播放，立即返回
                    self.player.play_async(next_task["audio_file"], next_task["volume"])
                    if next_task["schedule_type"] == "once":
                        self.db.delete_schedule(next_task["id"])
                else:
                    print(f"  ⚠️ [{t.strftime('%H:%M:%S')}] 错过: {next_task['name']} (距next={abs((t - next_time).total_seconds()):.1f}s)")
                continue  # 继续下一轮
            # 如果 break 了（REPLAN），继续下一轮调度的 while 循环
        print(f"\n=== 执行结果: {executed} ===")
        return executed

def main():
    db = FakeDB(); player = FakePlayer(); s = Scheduler(db, player)
    start = datetime.datetime(2026, 8, 19, 14, 10, 30)
    # 添加 3 个 daily 任务
    for i, tstr in enumerate(["14:13:00", "14:14:00", "14:15:00"]):
        db.add_schedule({"name": f"任务{tstr[:5]}", "schedule_type": "daily",
            "time_str": tstr, "week_days": "", "date_str": "",
            "audio_file": f"bell_{tstr[:5]}.mp3", "volume": 80, "play_count": 1,
            "enabled": 1, "profile_id": 1})
    print("=== 3 daily 任务: 14:13 / 14:14 / 14:15 ===")
    s.simulate(start)

if __name__ == "__main__":
    main()