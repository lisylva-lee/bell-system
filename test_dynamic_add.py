#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""动态添加任务场景测试"""
import datetime
from simulate_scheduler import FakeDB, FakePlayer, Scheduler

db = FakeDB(); player = FakePlayer(); s = Scheduler(db, player)
t = datetime.datetime(2026, 8, 19, 14, 10, 30)

# 14:10:30 添加 3 个任务 (模拟用户操作)
for i, ts in enumerate(["14:13:00", "14:14:00", "14:15:00"]):
    db.add_schedule({"name": f"任务{ts[:5]}", "schedule_type": "daily",
        "time_str": ts, "week_days": "", "date_str": "", "audio_file": f"b{ts[:5]}.mp3",
        "volume": 80, "play_count": 1, "enabled": 1, "profile_id": 1})
print(f"[{t.strftime('%H:%M:%S')}] 用户添加 3 个 daily 任务: 14:13 / 14:14 / 14:15")
executed = s.simulate(t, rounds=6)
print(f"结果: {executed}")
print("✅ 全部执行" if executed[:3]==['14:13:00','14:14:00','14:15:00'] else "❌ 有任务丢失")