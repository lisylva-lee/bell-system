# -*- coding: utf-8 -*-
"""设置读写（只增键，旧配置文件无需改动）"""
import json
import logging

from . import paths

logger = logging.getLogger("BellSystem.Settings")

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
    if paths.CONFIG_PATH.exists():
        try:
            with open(paths.CONFIG_PATH, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        except Exception:
            pass
    return data


def save_settings(settings: dict):
    with open(paths.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
