# -*- coding: utf-8 -*-
"""单实例保护（V2.3.1）

实机测试发现：同一个 exe 被启动两次（双击两次、或上次被强杀后残留子进程）时，
两个实例会各自跑一个调度线程 → **每次打铃响两遍、执行日志翻倍**。
这里用 Windows 命名互斥把第二个实例挡在门外，并尽量把已有窗口唤到前台。

互斥名带上 exe 所在目录的哈希：不同目录下的两套程序互不影响（方便并行测试新版本）。
非 Windows 平台直接放行。
"""
import os
import sys
import ctypes
import hashlib
import logging

logger = logging.getLogger("BellSystem.Instance")

WINDOW_TITLE = "🔔 智能打铃系统"
_handle = None
_released = False


def _mutex_name() -> str:
    try:
        from .paths import APP_DIR
        key = hashlib.sha1(str(APP_DIR).lower().encode("utf-8", "replace")).hexdigest()[:10]
    except Exception:
        key = "default"
    return "Local\\BellSystem_%s" % key


def acquire() -> bool:
    """返回 True = 本进程是唯一实例；False = 已有实例在运行"""
    global _handle, _released
    if os.name != "nt":
        return True
    if _handle is not None:
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, _mutex_name())
        if not handle:
            logger.warning("创建互斥体失败，按单实例继续运行")
            return True
        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            logger.warning("检测到已有实例在运行，本次启动退出")
            return False
        _handle = handle
        _released = False
        return True
    except Exception as e:            # 任何异常都放行，不能让单实例检查挡住主流程
        logger.warning("单实例检查失败（放行）: %s", e)
        return True


def release():
    """释放互斥（进程退出时系统也会自动释放，这里显式释放便于测试）"""
    global _handle, _released
    if _handle and os.name == "nt":
        try:
            ctypes.windll.kernel32.CloseHandle(_handle)
        except Exception:
            pass
    _handle = None
    _released = True


def focus_existing() -> bool:
    """把已有实例的窗口唤到前台；窗口在托盘里（已隐藏）时返回 False"""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return False
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception as e:
        logger.debug("唤醒已有窗口失败: %s", e)
        return False


def notify_already_running():
    """无 GUI 依赖的提示框（第二个实例启动时用）"""
    msg = ("智能打铃系统已经在运行。\n\n"
           "请到屏幕右下角的系统托盘查看图标（双击可打开主界面）。\n"
           "同时运行两个实例会导致每次打铃响两遍，已自动阻止本次启动。")
    try:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(0, msg, "智能打铃系统", 0x40)
        else:
            print(msg)
    except Exception:
        pass
