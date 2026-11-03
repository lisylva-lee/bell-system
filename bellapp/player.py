# -*- coding: utf-8 -*-
"""音频播放引擎（代际号取消语义 + 转码缓存）"""
import os
import re
import time
import shutil
import hashlib
import threading
import subprocess
import logging
from pathlib import Path
from typing import Optional

from .paths import DATA_DIR, FFMPEG_DIR, SUBPROCESS_FLAGS
from .util import _row_get

try:
    import pygame
except ImportError:
    pygame = None

logger = logging.getLogger("BellSystem.Player")

_UNSET = object()
_FFMPEG_CACHE = _UNSET


def ffmpeg_path() -> str:
    """探测一次 ffmpeg 并缓存结果；找不到时返回空串。

    旧实现每次播放都重新拼路径并起一次 subprocess，无 ffmpeg 的机器上
    每次打铃都会失败一次并打一条 WARNING（实机日志里每条铃声前后都有）。
    """
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE is not _UNSET:
        return _FFMPEG_CACHE
    try:
        local = FFMPEG_DIR / "ffmpeg.exe"
        if local.exists():
            _FFMPEG_CACHE = str(local)
            logger.info("使用内置 ffmpeg: %s", _FFMPEG_CACHE)
            return _FFMPEG_CACHE
    except Exception:
        pass
    found = shutil.which("ffmpeg") or ""
    _FFMPEG_CACHE = found
    if found:
        logger.info("使用系统 ffmpeg: %s", found)
    else:
        logger.info("未找到 ffmpeg：非 WAV 音频交给 pygame 直接解码（M4A/AAC 可能不支持）")
    return _FFMPEG_CACHE

class AudioPlayer:
    """音频播放引擎（V2.1 重写：全局可取消 + 序列播放整体可停止）

    旧实现的三个问题：
    1) `_stop_flag` 在每次 play() 开头被复位 → 序列播放时"停止"只停当前一首，后续继续响；
    2) `_playing/_current_file` 被多个播放线程共享 → 状态互相覆盖，Web 的 playing 字段不可信；
    3) 到点打铃与 Web"立即打铃"并发时，pygame.mixer.music 单通道会被抢。
    新实现用"代际号(epoch)"表达取消：stop() 使 epoch+1，所有在途/后续播放立即失效。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._epoch = 0
        self._playing = False
        self._current_file = None
        if pygame:
            try:
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=4096)
            except Exception as e:
                logger.warning(f"pygame.mixer 初始化失败（可能无音频设备）: {e}")

    # ---------- 取消语义 ----------
    def current_epoch(self) -> int:
        with self._lock:
            return self._epoch

    def is_cancelled(self, epoch: Optional[int]) -> bool:
        if epoch is None:
            return False
        with self._lock:
            return epoch != self._epoch

    def stop(self):
        """停止播放：当前曲目立即停，序列播放中的后续曲目也会被跳过"""
        with self._lock:
            self._epoch += 1
            self._playing = False
        if pygame and pygame.mixer.get_init():
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass

    def is_playing(self) -> bool:
        with self._lock:
            return self._playing

    # ---------- 播放 ----------
    def play(self, file_path: str, volume: int = 80, count: int = 1,
             epoch: Optional[int] = None) -> bool:
        """同步播放，返回是否至少播放了一次；epoch 用于响应 stop()"""
        if not pygame:
            logger.error("pygame 未安装，无法播放音频")
            return False
        if epoch is None:
            epoch = self.current_epoch()
        if self.is_cancelled(epoch):
            return False

        path = Path(file_path)
        if not path.exists():
            logger.error(f"音频文件不存在: {file_path}")
            return False

        try:
            # 非 WAV：优先用转码缓存（首次转码，之后直接复用，避免每次打铃都等 ffmpeg）
            if path.suffix.lower() != ".wav":
                cached = self._ensure_wav_cache(path)
                if cached is not None:
                    path = cached

            pygame.mixer.music.load(str(path))
            pygame.mixer.music.set_volume(max(0.0, min(1.0, volume / 100.0)))
            with self._lock:
                self._current_file = str(path)
                self._playing = True

            played = 0
            for _ in range(max(1, int(count))):
                if self.is_cancelled(epoch):
                    break
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    if self.is_cancelled(epoch):
                        pygame.mixer.music.stop()
                        break
                    time.sleep(0.05)
                played += 1
            return played > 0
        except Exception as e:
            logger.error(f"播放失败: {e}")
            return False
        finally:
            with self._lock:
                self._playing = False

    def play_async(self, file_path: str, volume: int = 80, count: int = 1, callback=None):
        """异步播放（回调收到是否成功）"""
        epoch = self.current_epoch()

        def _run():
            ok = self.play(file_path, volume, count, epoch=epoch)
            if callback:
                try:
                    callback(ok)
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()

    def play_many(self, tasks, on_item=None) -> int:
        """按顺序播放一组任务，返回实际播放条数；期间调用 stop() 会整体打断。

        tasks 为可索引对象（sqlite3.Row / dict），需含 audio_file/volume/play_count/name。
        """
        epoch = self.current_epoch()
        played = 0
        total = len(tasks)
        for idx, t in enumerate(tasks):
            if self.is_cancelled(epoch):
                logger.info("序列播放已被停止，剩余 %d 条跳过", total - idx)
                break
            audio = _row_get(t, "audio_file", "")
            volume = int(_row_get(t, "volume", 80) or 80)
            count = int(_row_get(t, "play_count", 1) or 1)
            ok = self.play(audio, volume, count, epoch=epoch)
            if self.is_cancelled(epoch):
                break
            played += 1
            if on_item:
                try:
                    on_item(t, ok)
                except Exception:
                    pass
        return played

    # ---------- 转码缓存 ----------
    def _ensure_wav_cache(self, path: Path) -> Optional[Path]:
        """把非 WAV 音频转成 WAV 并缓存；失败返回 None（调用方退回直接播放原文件）"""
        cache_dir = DATA_DIR / "cache"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            st = path.stat()
            key = "%s|%d|%d" % (str(path).lower(), int(st.st_mtime), st.st_size)
            digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
            stem = re.sub(r"[^0-9A-Za-z_.-]", "_", path.stem)[:40]
            dst = cache_dir / ("%s_%s.wav" % (stem, digest))
            if dst.exists() and dst.stat().st_size > 44:
                return dst
        except Exception as e:
            logger.warning(f"转码缓存目录不可用: {e}")
            return None

        tmp = dst.with_name(dst.stem + ".tmp.wav")
        if not self._convert_to_wav(str(path), str(tmp)):
            try:
                tmp.unlink()
            except OSError:
                pass
            return None
        try:
            tmp.replace(dst)
        except OSError:
            return tmp if tmp.exists() else None
        return dst

    def _convert_to_wav(self, src: str, dst: str) -> bool:
        """用 ffmpeg 转 WAV（无 ffmpeg 时静默返回 False，由调用方退回直接播放）"""
        exe = ffmpeg_path()
        if not exe:
            return False
        try:
            subprocess.run(
                [exe, "-y", "-loglevel", "error", "-i", src,
                 "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", dst],
                capture_output=True, timeout=30,
                creationflags=SUBPROCESS_FLAGS
            )
            return Path(dst).exists()
        except Exception as e:
            logger.warning(f"ffmpeg 转换失败: {e}")
            return False
