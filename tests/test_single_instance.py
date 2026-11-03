# -*- coding: utf-8 -*-
"""单实例锁：同一目录只能跑一个实例（实机曾因双开导致每次打铃响两遍）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bellapp import single_instance as si      # noqa: E402


def test_mutex_name_is_per_install_dir():
    name = si._mutex_name()
    assert name.startswith("Local\\BellSystem_"), name
    assert len(name) > len("Local\\BellSystem_")


def test_acquire_is_idempotent_within_process():
    """同一进程内重复调用不应把自己挡在门外（互斥体归本进程所有）"""
    si.release()
    assert si.acquire() is True
    assert si.acquire() is True
    si.release()


def test_second_process_is_rejected():
    """真实场景：另起一个进程应被识别为"已有实例在运行"（用子进程验证）"""
    import subprocess
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from bellapp import single_instance as si;"
        "print('ACQUIRED' if si.acquire() else 'REJECTED')"
    ) % str(ROOT)
    si.release()
    assert si.acquire() is True
    try:
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=60)
        assert "REJECTED" in (out.stdout or ""), (out.stdout, out.stderr)
    finally:
        si.release()
    # 释放后子进程应能拿到锁
    out2 = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, timeout=60)
    assert "ACQUIRED" in (out2.stdout or ""), (out2.stdout, out2.stderr)


def test_release_is_idempotent():
    si.release()
    si.release()
    assert si.acquire() is True
    si.release()


def test_focus_existing_never_raises():
    assert isinstance(si.focus_existing(), bool)
