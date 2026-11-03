# -*- coding: utf-8 -*-
"""课表 CSV：往返一致 / 坏行报错 / 星期写法 / 任务 payload 组装。"""
import io
import sys
import csv
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bellapp.storage import Database     # noqa: E402
from bellapp.csvio import (CSV_HEADER, _parse_week_days, export_schedules_csv,  # noqa: E402
                           import_schedules_csv, build_schedule_payload)


def _db():
    d = Path(tempfile.mkdtemp(prefix="bellcsv_"))
    return Database(d / "bell.db"), d


def _wav(p: Path) -> str:
    p.write_bytes(b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt "
                  + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
                  + (1).to_bytes(2, "little") + (8000).to_bytes(4, "little")
                  + (8000).to_bytes(4, "little") + (1).to_bytes(2, "little")
                  + (8).to_bytes(2, "little") + b"data" + (0).to_bytes(4, "little"))
    return str(p)


def test_week_parse_variants():
    assert _parse_week_days("0,2,4") == "0,2,4"
    assert _parse_week_days("一,三,五") == "0,2,4"
    assert _parse_week_days("周一、周三") == "0,2"
    assert _parse_week_days("1,3,7") == "0,2,6"      # 出现 7 才按 1-7 解释
    assert _parse_week_days("周日") == "6"
    assert _parse_week_days("乱写") is None


def test_csv_roundtrip():
    db, d = _db()
    try:
        audio = _wav(d / "a.wav")
        pid = db.get_active_profile_id()
        db.add_schedule({"name": "周一三五", "schedule_type": "weekly", "time_str": "08:00:00",
                         "week_days": "0,2,4", "date_str": "", "audio_file": audio,
                         "volume": 65, "play_count": 3, "enabled": 1, "profile_id": pid})
        db.add_schedule({"name": "每天午休", "schedule_type": "daily", "time_str": "12:00:00",
                         "week_days": "", "date_str": "", "audio_file": audio,
                         "volume": 40, "play_count": 1, "enabled": 0, "profile_id": pid})
        path = str(d / "schedule.csv")
        assert export_schedules_csv(db, path) == 2
        head = io.open(path, encoding="utf-8-sig").readline().strip()
        assert head == ",".join(CSV_HEADER), head

        for r in db.get_schedules(pid):
            db.delete_schedule(r["id"])
        ok, errors, _w = import_schedules_csv(db, path)
        assert ok == 2 and not errors, (ok, errors)
        got = {r["name"]: r for r in db.get_schedules(pid)}
        assert got["周一三五"]["week_days"] == "0,2,4"
        assert got["周一三五"]["volume"] == 65 and got["周一三五"]["play_count"] == 3
        assert got["每天午休"]["enabled"] == 0
    finally:
        db.close(); shutil.rmtree(str(d), ignore_errors=True)


def test_bad_rows_reported_valid_imported():
    db, d = _db()
    try:
        audio = _wav(d / "b.wav")
        path = d / "bad.csv"
        with io.open(str(path), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            w.writerow(["好行", "每天", "09:00:00", "", "", audio, 80, 1, "是"])
            w.writerow(["坏时间", "每天", "9点", "", "", audio, 80, 1, "是"])
            w.writerow(["坏类型", "每小时", "09:00:00", "", "", audio, 80, 1, "是"])
            w.writerow(["每周没星期", "每周", "09:00:00", "", "", audio, 80, 1, "是"])
            w.writerow(["坏日期", "一次性", "09:00:00", "", "2026/01/01", audio, 80, 1, "是"])
        ok, errors, _w = import_schedules_csv(db, str(path))
        assert ok == 1 and len(errors) == 4, (ok, errors)
    finally:
        db.close(); shutil.rmtree(str(d), ignore_errors=True)


def test_build_payload_keeps_original_fields():
    """编辑任务不得把 play_count 重置、不得把公共任务私有化"""
    base = {"id": 7, "play_count": 3, "profile_id": 0}
    p = build_schedule_payload(name="课间铃", schedule_type="daily", time_str="09:00:00",
                              audio_file="a.mp3", volume=80, play_count=3,
                              enabled=1, base=base, active_profile_id=2)
    assert p["play_count"] == 3 and p["profile_id"] == 0
    p2 = build_schedule_payload(name="新", schedule_type="daily", time_str="09:00:00",
                               audio_file="a.mp3", play_count=99, volume=999,
                               active_profile_id=2)
    assert p2["play_count"] == 10 and p2["volume"] == 100 and p2["profile_id"] == 2
