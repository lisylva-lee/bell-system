# -*- coding: utf-8 -*-
"""主窗口"""
import os
import sys
import queue
import datetime
import threading
import logging
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None

from ..paths import APP_DIR
from ..settings import load_settings
from ..util import _row_get
from ..storage import Database
from ..player import AudioPlayer
from ..scheduler import Scheduler
from ..web.api import BellWebServer
from .dialogs import (ScheduleDialog, ProfileDialog, LogDialog, CalendarDialog,
                      ScheduleIODialog, SettingsDialog)

logger = logging.getLogger("BellSystem.UI")

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
