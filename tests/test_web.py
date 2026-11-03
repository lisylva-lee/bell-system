# -*- coding: utf-8 -*-
"""Web 控制台：鉴权 / 跨站拦截 / 路径白名单 / 错误脱敏 / 告警透出。"""
import io
import sys
import json
import time
import shutil
import socket
import tempfile
import datetime
import http.client
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bellapp.storage import Database                      # noqa: E402
from bellapp.player import AudioPlayer                    # noqa: E402
from bellapp.scheduler import Scheduler                   # noqa: E402
from bellapp.web import api as W                          # noqa: E402


def _free_port():
    for port in range(19000, 19100):
        try:
            s = socket.socket(); s.bind(("127.0.0.1", port)); s.close()
            return port
        except OSError:
            continue
    raise RuntimeError("没有空闲端口")


class Server(object):
    def __init__(self, token=""):
        self.dir = Path(tempfile.mkdtemp(prefix="bellweb_"))
        self.db = Database(self.dir / "bell.db")
        self.player = AudioPlayer()
        self.scheduler = Scheduler(self.db, self.player)
        W._auth_fail_map.clear()
        self.port = _free_port()
        self.srv = W.BellWebServer(self.db, self.player, self.scheduler,
                                   port=self.port, token=token, on_change=lambda: None)
        self.srv.start()
        time.sleep(0.3)

    def call(self, method, path, body=None, ctype="application/json", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=6)
        h = {}
        if ctype:
            h["Content-Type"] = ctype
        if headers:
            h.update(headers)
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        raw = r.read().decode("utf-8", "replace")
        conn.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def wav(self):
        p = self.dir / "a.wav"
        p.write_bytes(b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt "
                      + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
                      + (1).to_bytes(2, "little") + (8000).to_bytes(4, "little")
                      + (8000).to_bytes(4, "little") + (1).to_bytes(2, "little")
                      + (8).to_bytes(2, "little") + b"data" + (0).to_bytes(4, "little"))
        return str(p)

    def close(self):
        self.srv.stop()
        self.db.close()
        shutil.rmtree(str(self.dir), ignore_errors=True)


def _payload(audio, **kw):
    d = {"name": "任务", "schedule_type": "daily", "time_str": "07:30",
         "audio_file": audio, "volume": 80, "enabled": True}
    d.update(kw)
    return json.dumps(d)


def test_csrf_and_content_type_blocked():
    s = Server("")
    try:
        body = _payload(s.wav())
        st, _ = s.call("POST", "/api/schedules/create", body, ctype="text/plain",
                       headers={"Origin": "http://evil.example"})
        assert st == 415, st
        st, _ = s.call("POST", "/api/schedules/create", body,
                       headers={"Origin": "http://evil.example"})
        assert st == 403, st
        assert s.db.get_schedules(0) == []
    finally:
        s.close()


def test_audio_path_hardened():
    s = Server("")
    try:
        for audio, expect in ((r"\\attacker-share\x.mp3", "网络路径"),
                              ("C:/definitely/not/exist.mp3", "不存在"),
                              (str(s.dir / "evil.txt"), "格式")):
            st, body = s.call("POST", "/api/schedules/create", _payload(audio))
            assert st == 400 and expect in body["error"], (audio, st, body)
        st, _ = s.call("POST", "/api/schedules/create", _payload(s.wav()))
        assert st == 200, st
    finally:
        s.close()


def test_profile_id_ignored():
    s = Server("")
    try:
        st, body = s.call("POST", "/api/schedules/create",
                          _payload(s.wav(), profile_id=999))
        assert st == 200
        assert s.db.get_schedule(body["id"])["profile_id"] == s.db.get_active_profile_id()
    finally:
        s.close()


def test_token_header_only_and_rate_limited():
    s = Server("secret123")
    try:
        assert s.call("GET", "/api/status", headers={"X-Auth-Token": "secret123"})[0] == 200
        assert s.call("GET", "/api/status?token=secret123")[0] == 401     # query 令牌失效
        codes = [s.call("GET", "/api/status", headers={"X-Auth-Token": "bad"})[0]
                 for _ in range(7)]
        assert codes[0] == 401 and codes[-1] == 429, codes
    finally:
        s.close()


def test_web_version_follows_package_version():
    """接口版本必须跟随程序版本（旧实现写死 2.1）"""
    from bellapp import __version__
    assert W.WEB_VERSION == __version__, (W.WEB_VERSION, __version__)


def test_port_cannot_be_bound_twice():
    """同一端口不能被第二个实例抢占绑定（Windows 上 SO_REUSEADDR 语义会允许，必须独占）"""
    s = Server("")
    try:
        from http.server import BaseHTTPRequestHandler

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

        try:
            W.BoundedThreadingHTTPServer(("0.0.0.0", s.port), H)
        except OSError:
            pass                                   # 期望：端口被占用，绑定失败
        else:
            raise AssertionError("同一端口被重复绑定成功，说明没有独占绑定")
    finally:
        s.close()


def test_error_not_leaked_and_alerts():
    s = Server("")
    try:
        st, body = s.call("POST", "/api/schedules/update",
                          json.dumps({"id": "not-a-number", "name": "x"}))
        assert st == 400 and "int()" not in json.dumps(body), (st, body)
        s.db.add_log(1, "坏文件", "b.mp3", "failed", "文件缺失")
        s.db.add_log(2, "漏打铃", "c.mp3", "missed", "延迟 300 秒")
        st, body = s.call("GET", "/api/status")
        assert st == 200
        assert body["status"]["auth_required"] is False
        assert len(body["status"]["alerts"]) == 2, body["status"]["alerts"]
    finally:
        s.close()
