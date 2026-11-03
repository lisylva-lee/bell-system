# -*- coding: utf-8 -*-
"""通用小工具"""

def _row_get(row, key, default=None):
    """兼容 sqlite3.Row / dict 的取值助手"""
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value
