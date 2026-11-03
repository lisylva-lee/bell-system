# -*- coding: utf-8 -*-
"""各对话框：任务编辑 / 方案 / 日志 / 日历 / 课表导入导出 / 系统设置"""
import sys
import csv
import datetime
import logging
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

from ..paths import APP_DIR, SUBPROCESS_FLAGS
from ..settings import load_settings, save_settings
from ..csvio import (CSV_HEADER, export_schedules_csv, import_schedules_csv,
                     build_schedule_payload)
from ..power import set_auto_start, set_prevent_sleep

logger = logging.getLogger("BellSystem.UI")

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
        """开机自启（实现见 bellapp.power）"""
        set_auto_start(bool(enable))

    def _set_prevent_sleep(self, enable: bool):
        """防休眠（实现见 bellapp.power，不再改写系统电源策略）"""
        set_prevent_sleep(bool(enable))
