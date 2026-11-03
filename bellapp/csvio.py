# -*- coding: utf-8 -*-
"""课表 CSV 导入导出 + 任务数据组装"""
import csv
import datetime
import logging
from pathlib import Path

from .util import _row_get
from .storage import Database

logger = logging.getLogger("BellSystem.Csv")

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
