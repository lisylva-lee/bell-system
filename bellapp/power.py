# -*- coding: utf-8 -*-
"""Windows 系统集成：开机自启 + 防休眠

V2.3 起「防止休眠」不再用 `powercfg -change` 永久改写系统电源策略
（旧实现只在取消勾选时改回 30 分钟，且不恢复电池/休眠设置，属于对用户系统的持久副作用），
改为 `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`：
只在**本进程存活期间**生效，程序退出/崩溃后系统自动恢复默认策略。
"""
import os
import sys
import ctypes
import logging

logger = logging.getLogger("BellSystem.Power")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

_sleep_held = False


def set_prevent_sleep(enable: bool) -> bool:
    """启用/禁用"运行期间禁止系统休眠"。仅 Windows 生效，失败只记日志不抛异常。"""
    global _sleep_held
    if os.name != "nt":
        logger.info("非 Windows 平台，跳过防休眠设置")
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        if enable:
            flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            if kernel32.SetThreadExecutionState(ctypes.c_uint(flags)):
                _sleep_held = True
                logger.info("已禁止系统休眠（仅本进程运行期间有效）")
                return True
            logger.warning("SetThreadExecutionState 调用失败")
            return False
        if kernel32.SetThreadExecutionState(ctypes.c_uint(ES_CONTINUOUS)):
            _sleep_held = False
            logger.info("已恢复系统默认休眠策略")
            return True
        return False
    except Exception as e:
        logger.warning("设置防休眠失败: %s", e)
        return False


def set_auto_start(enable: bool) -> bool:
    """开机自启（HKCU\\...\\Run），失败只记日志"""
    if os.name != "nt":
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        try:
            if enable:
                exe_path = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
                winreg.SetValueEx(key, "BellSystem", 0, winreg.REG_SZ, '"%s"' % exe_path)
                logger.info("已设置开机自启: %s", exe_path)
            else:
                try:
                    winreg.DeleteValue(key, "BellSystem")
                    logger.info("已取消开机自启")
                except FileNotFoundError:
                    pass
            return True
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        logger.warning("设置开机自启失败: %s", e)
        return False
