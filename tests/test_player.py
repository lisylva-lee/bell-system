# -*- coding: utf-8 -*-
"""播放层：代际号(epoch)取消语义 —— "停止"必须能打断整个序列。"""
import sys
import time
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bellapp.player import AudioPlayer     # noqa: E402


class FakePlayer(AudioPlayer):
    """不打真实音频，只记录"被播放"的条目，并留出被 stop() 打断的时间窗"""

    def __init__(self, delay=0.06):
        AudioPlayer.__init__(self)
        self.played = []
        self.delay = delay

    def play(self, file_path, volume=80, count=1, epoch=None):
        if self.is_cancelled(epoch):
            return False
        self.played.append(file_path)
        time.sleep(self.delay)
        return True


def _tasks(n=5):
    return [{"audio_file": "a%d.wav" % i, "volume": 80, "play_count": 1,
             "name": "n%d" % i} for i in range(n)]


def test_play_many_plays_all_without_stop():
    p = FakePlayer()
    assert p.play_many(_tasks()) == 5
    assert len(p.played) == 5


def test_stop_aborts_whole_sequence():
    p = FakePlayer()
    box = {}
    th = threading.Thread(target=lambda: box.setdefault("n", p.play_many(_tasks())))
    th.start()
    time.sleep(0.10)
    p.stop()
    th.join(timeout=5)
    assert box["n"] < 5, box
    assert not p.is_playing()


def test_epoch_semantics():
    p = FakePlayer()
    e = p.current_epoch()
    assert not p.is_cancelled(e)
    p.stop()
    assert p.is_cancelled(e)
    assert not p.is_cancelled(p.current_epoch())


def test_ffmpeg_probe_is_cached():
    """ffmpeg 只探测一次（旧实现每次播放都重试并刷 WARNING）"""
    from bellapp.player import ffmpeg_path
    first = ffmpeg_path()
    assert isinstance(first, str)
    assert ffmpeg_path() is first, "应返回缓存结果（同一个对象）"


def test_no_legacy_stop_flag_left():
    src = (ROOT / "bellapp" / "player.py").read_text(encoding="utf-8")
    assert "self._stop_flag" not in src
