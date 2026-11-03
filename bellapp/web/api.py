# -*- coding: utf-8 -*-
"""Web API：路由、鉴权、校验"""
import os
import json
import re
import hmac
import time
import socket
import threading
import datetime
import logging
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs
from pathlib import Path

from .ui import INDEX_HTML
from .. import __version__

logger = logging.getLogger("BellSystem.Web")

# V2.3.1: 接口版本跟随程序版本（旧实现写死 2.1，升级后接口仍报 2.1）
WEB_VERSION = __version__


ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


MAX_AUDIO_PATH_LEN = 260


MAX_BODY_BYTES = 256 * 1024        # 请求体上限（旧实现允许 1MB）


REQUEST_TIMEOUT = 15               # 单连接读超时，防慢速连接占线程


MAX_CONCURRENT_REQUESTS = 32       # 并发请求上限


AUTH_FAIL_WINDOW = 60              # 令牌失败计数窗口（秒）


AUTH_FAIL_MAX = 5                  # 窗口内允许的失败次数


_auth_fail_lock = threading.Lock()


_auth_fail_map = {}


def _auth_blocked(ip: str) -> bool:
    now = time.time()
    with _auth_fail_lock:
        times = [t for t in _auth_fail_map.get(ip, []) if now - t < AUTH_FAIL_WINDOW]
        _auth_fail_map[ip] = times
        return len(times) >= AUTH_FAIL_MAX


def _auth_fail_add(ip: str):
    with _auth_fail_lock:
        _auth_fail_map.setdefault(ip, []).append(time.time())


def _as_int(value):
    """把请求里的 id 之类安全转成 int，失败返回 None（不再抛异常打成 500）"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def get_local_urls(port: int):
    """枚举本机局域网 IP，返回可访问的 URL 列表"""
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # 不实际发包，仅取路由出口 IP
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        ips.add(socket.gethostbyname(socket.gethostname()))
    except Exception:
        pass
    ips = {ip for ip in ips if ip and not ip.startswith("169.254.")}
    ips.add("127.0.0.1")
    return ["http://%s:%d/" % (ip, port) for ip in sorted(ips)]


_TIME_RE = re.compile(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$")


def validate_audio_path(audio: str):
    """校验 Web 端提交的铃声路径，返回 (ok, 错误信息)。

    Web 端只能填"服务器本机"的音频文件：拒绝 UNC/网络路径（会触发 SMB 外联，
    既慢又可能泄露凭据）、限制后缀白名单、要求文件真实存在。
    """
    if not audio:
        return False, "铃声文件不能为空"
    if len(audio) > MAX_AUDIO_PATH_LEN:
        return False, "铃声路径过长（≤%d 字符）" % MAX_AUDIO_PATH_LEN
    if audio.startswith("\\\\") or audio.startswith("//"):
        return False, "不支持网络路径（UNC），请使用服务器本机路径"
    if "://" in audio:
        return False, "不支持 URL，请使用服务器本机路径"
    ext = os.path.splitext(audio)[1].lower()
    if ext not in ALLOWED_AUDIO_EXT:
        return False, "不支持的音频格式，仅允许 %s" % "/".join(sorted(ALLOWED_AUDIO_EXT))
    if not os.path.exists(audio):
        return False, "铃声文件不存在（请填写服务器本机上的完整路径）"
    if not os.path.isfile(audio):
        return False, "铃声路径不是文件"
    return True, ""


def validate_schedule(data: dict):
    """校验并规范化任务数据，返回 (规范后的dict, 错误信息)"""
    if not isinstance(data, dict):
        return None, "请求体必须是 JSON 对象"

    name = str(data.get("name", "")).strip()
    if not name:
        return None, "任务名称不能为空"
    if len(name) > 50:
        return None, "任务名称过长（≤50 字）"

    audio = str(data.get("audio_file", "")).strip()
    ok, audio_err = validate_audio_path(audio)
    if not ok:
        return None, audio_err

    stype = str(data.get("schedule_type", "")).strip()
    if stype not in ("daily", "weekly", "date", "once"):
        return None, "重复方式必须是 daily/weekly/date/once"

    t = str(data.get("time_str", "")).strip()
    m = _TIME_RE.match(t)
    if not m:
        return None, "时间格式错误，应为 HH:MM 或 HH:MM:SS"
    h, mi, sec = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59 and 0 <= sec <= 59):
        return None, "时间数值超出范围"
    time_str = "%02d:%02d:%02d" % (h, mi, sec)

    week_days = ""
    if stype == "weekly":
        raw = data.get("week_days", "")
        if isinstance(raw, list):
            days = raw
        else:
            days = [x for x in str(raw).split(",") if x != ""]
        try:
            days = sorted({int(x) for x in days})
        except (TypeError, ValueError):
            return None, "星期格式错误"
        if not days or any(d < 0 or d > 6 for d in days):
            return None, "请至少选择一个有效星期（0-6，周一=0）"
        week_days = ",".join(str(d) for d in days)

    date_str = ""
    if stype in ("date", "once"):
        date_str = str(data.get("date_str", "")).strip()
        try:
            datetime.datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return None, "日期格式错误，应为 YYYY-MM-DD"

    try:
        volume = int(data.get("volume", 80))
    except (TypeError, ValueError):
        return None, "音量必须是数字"
    volume = max(0, min(100, volume))

    try:
        play_count = max(1, min(10, int(data.get("play_count", 1))))
    except (TypeError, ValueError):
        play_count = 1

    enabled = 1 if data.get("enabled", 1) in (1, True, "1", "true") else 0

    return {
        "name": name,
        "schedule_type": stype,
        "time_str": time_str,
        "week_days": week_days,
        "date_str": date_str,
        "audio_file": audio,
        "volume": volume,
        "play_count": play_count,
        "enabled": enabled,
        # V2.1: 忽略客户端传来的 profile_id，由服务端决定（防跨方案写入）
        "profile_id": 0,
    }, ""


class ApiHandler(BaseHTTPRequestHandler):
    # 由 BellWebServer 注入：{"db","player","scheduler","token","on_change"}
    ctx = {}
    server_version = "BellSystemWeb/" + WEB_VERSION
    timeout = REQUEST_TIMEOUT          # V2.1: 单连接读超时，防慢速连接长期占用线程

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _ok(self, **kw):
        self._send_json(200, dict({"ok": True}, **kw))

    def _err(self, code: int, msg: str):
        self._send_json(code, {"ok": False, "error": msg})

    def _client_ip(self) -> str:
        try:
            return self.client_address[0]
        except Exception:
            return "?"

    def _authorized(self) -> bool:
        """V2.1 加固：
        - 令牌只接受 X-Auth-Token 请求头（不再接受 ?token=，避免进浏览器历史/代理日志/Referer）
        - hmac.compare_digest 恒定时间比较，避免逐字符时序爆破
        - 同一来源 IP 在窗口内失败过多则临时拒绝，阻止暴力猜测
        """
        token = str(self.ctx.get("token") or "")
        if not token:
            return True
        ip = self._client_ip()
        if _auth_blocked(ip):
            self._blocked = True
            return False
        supplied = self.headers.get("X-Auth-Token", "") or ""
        if supplied and hmac.compare_digest(supplied, token):
            return True
        _auth_fail_add(ip)
        return False

    def _deny(self):
        if getattr(self, "_blocked", False):
            return self._err(429, "令牌尝试过于频繁，请稍后再试")
        return self._err(401, "需要访问令牌")

    def _check_content_type(self) -> bool:
        """只接受 application/json（浏览器跨站"简单请求"无法伪造该类型，从而挡住 CSRF）"""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return ctype == "application/json"

    def _check_origin(self) -> bool:
        """浏览器跨站请求的 Origin 与本站 Host 不一致时拒绝（非浏览器客户端无 Origin，放行）"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            origin_host = (urlsplit(origin).hostname or "").lower()
        except ValueError:
            return False
        host_header = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        if not host_header:
            return True
        if origin_host == host_header:
            return True
        allowed = {str(a).lower() for a in (self.ctx.get("allow_origins") or [])}
        return origin_host in allowed

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            return {}
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _changed(self):
        # 数据变化：先打断调度器当前等待（立即重新规划），再通知 GUI 刷新
        sched = self.ctx.get("scheduler")
        if sched:
            try:
                sched.reschedule()
            except Exception:
                pass
        cb = self.ctx.get("on_change")
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _serve_index(self):
        body = INDEX_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- 数据组装 ----------
    @staticmethod
    def _schedule_json(row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "schedule_type": row["schedule_type"],
            "time_str": row["time_str"],
            "week_days": [int(x) for x in str(row["week_days"] or "").split(",") if x != ""],
            "date_str": row["date_str"] or "",
            "audio_file": row["audio_file"],
            "audio_name": Path(row["audio_file"]).name,
            "volume": row["volume"],
            "play_count": row["play_count"],
            "enabled": bool(row["enabled"]),
            "profile_id": row["profile_id"],
        }

    def _status_payload(self) -> dict:
        db = self.ctx["db"]
        scheduler = self.ctx["scheduler"]
        player = self.ctx["player"]
        now = datetime.datetime.now()
        next_dt, next_name = scheduler.get_next_info()
        next_info = None
        if next_dt:
            next_info = {
                "datetime": next_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "time": next_dt.strftime("%H:%M:%S"),
                "name": next_name or "",
                "in_seconds": max(0, int((next_dt - now).total_seconds())),
            }
        active_id = db.get_active_profile_id()
        active_name = ""
        for p in db.get_profiles():
            if p["id"] == active_id:
                active_name = p["name"]
                break
        return {
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "paused": scheduler.is_paused,
            "playing": player.is_playing(),
            "next": next_info,
            "active_profile": {"id": active_id, "name": active_name},
            "version": WEB_VERSION,
            # V2.1: 让前端能提示"当前没有访问令牌"
            "auth_required": bool(self.ctx.get("token")),
            # V2.2: 最近 24 小时的失败/漏打，前端用红色横幅提示
            "alerts": self._alerts_payload(db),
        }

    @staticmethod
    def _alerts_payload(db) -> list:
        try:
            rows = db.get_recent_alerts(24, 3)
        except Exception:
            return []
        return [{
            "time": r["execute_time"],
            "name": r["schedule_name"] or "",
            "status": r["status"],
            "error": r["error_message"] or "",
        } for r in rows]

    # ---------- GET ----------
    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_index()
        if not path.startswith("/api/"):
            return self._err(404, "未找到")
        if not self._authorized():
            return self._deny()
        try:
            if path == "/api/status":
                return self._ok(status=self._status_payload())
            if path == "/api/schedules":
                rows = self.ctx["db"].get_schedules(self.ctx["db"].get_active_profile_id())
                return self._ok(schedules=[self._schedule_json(r) for r in rows])
            if path == "/api/profiles":
                ps = [{"id": p["id"], "name": p["name"], "is_active": bool(p["is_active"])}
                      for p in self.ctx["db"].get_profiles()]
                return self._ok(profiles=ps)
            if path == "/api/logs":
                q = parse_qs(urlsplit(self.path).query)
                try:
                    limit = min(500, max(1, int((q.get("limit") or ["100"])[0])))
                except ValueError:
                    limit = 100
                logs = [{
                    "id": r["id"], "time": r["execute_time"], "name": r["schedule_name"],
                    "audio": Path(r["audio_file"] or "").name,
                    "status": r["status"], "error": r["error_message"] or "",
                } for r in self.ctx["db"].get_logs(limit)]
                return self._ok(logs=logs)
            return self._err(404, "未知接口")
        except Exception as e:
            # V2.1: 异常细节只进日志，不回传给客户端（避免泄露内部实现）
            logger.error("GET %s 失败: %s", path, e, exc_info=True)
            return self._err(500, "服务器内部错误，请查看服务器日志")

    # ---------- POST ----------
    def do_POST(self):
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            return self._err(404, "未找到")
        if not self._check_content_type():
            return self._err(415, "Content-Type 必须是 application/json")
        if not self._check_origin():
            return self._err(403, "拒绝跨站请求")
        if not self._authorized():
            return self._deny()
        data = self._read_json()
        db = self.ctx["db"]
        player = self.ctx["player"]
        scheduler = self.ctx["scheduler"]
        try:
            if path == "/api/schedules/create":
                norm, err = validate_schedule(data)
                if err:
                    return self._err(400, err)
                norm["profile_id"] = db.get_active_profile_id()
                sid = db.add_schedule(norm)
                self._changed()
                return self._ok(id=sid)

            if path == "/api/schedules/update":
                sid = _as_int(data.get("id"))
                if sid is None:
                    return self._err(400, "任务 id 无效")
                row = db.get_schedule(sid)
                if not row:
                    return self._err(404, "任务不存在")
                norm, err = validate_schedule(data)
                if err:
                    return self._err(400, err)
                # 未显式提供 profile_id 时保持原任务所属方案不变
                if data.get("profile_id") is None:
                    norm["profile_id"] = row["profile_id"]
                db.update_schedule(sid, norm)
                self._changed()
                return self._ok(id=sid)

            if path == "/api/schedules/delete":
                sid = _as_int(data.get("id"))
                if sid is None:
                    return self._err(400, "任务 id 无效")
                row = db.get_schedule(sid)
                if not row:
                    return self._err(404, "任务不存在")
                db.delete_schedule(sid)
                self._changed()
                return self._ok(id=sid)

            if path == "/api/schedules/toggle":
                sid = _as_int(data.get("id"))
                if sid is None:
                    return self._err(400, "任务 id 无效")
                row = db.get_schedule(sid)
                if not row:
                    return self._err(404, "任务不存在")
                enabled = bool(data.get("enabled", not row["enabled"]))
                db.toggle_schedule(sid, enabled)
                self._changed()
                return self._ok(id=sid, enabled=enabled)

            if path == "/api/ring":
                def make_logger(task):
                    # 与定时执行一致：远程触发也写入执行日志
                    def on_play(result):
                        status = "success" if result else "failed"
                        error = "" if result else "播放失败"
                        db.add_log(task["id"], task["name"], task["audio_file"], status, error)
                    return on_play
                sid = _as_int(data.get("schedule_id"))
                if sid is not None:
                    row = db.get_schedule(sid)
                    if not row:
                        return self._err(404, "任务不存在")
                    logger.info("远程立即打铃: %s", row["name"])
                    player.play_async(row["audio_file"], row["volume"],
                                      row["play_count"], callback=make_logger(row))
                    return self._ok(message="正在播放: %s" % row["name"])
                active_id = db.get_active_profile_id()
                enabled = [s for s in db.get_schedules(active_id) if s["enabled"]]
                if not enabled:
                    return self._err(400, "当前没有启用的任务")
                def play_all():
                    # V2.1: 走 play_many，"停止播放"可整体打断整个序列
                    player.play_many(
                        enabled,
                        on_item=lambda s, ok: make_logger(s)(ok))
                threading.Thread(target=play_all, daemon=True).start()
                return self._ok(message="依次播放 %d 个启用任务" % len(enabled))

            if path == "/api/stop":
                player.stop()
                return self._ok(message="已停止播放")

            if path == "/api/pause":
                if "paused" in data:
                    if data["paused"]:
                        scheduler.pause()
                    else:
                        scheduler.resume()
                else:
                    (scheduler.resume if scheduler.is_paused else scheduler.pause)()
                self._changed()
                return self._ok(paused=scheduler.is_paused)

            if path == "/api/profiles/create":
                name = str(data.get("name", "")).strip()
                if not name:
                    return self._err(400, "方案名称不能为空")
                if len(name) > 30:
                    return self._err(400, "方案名称过长（≤30 字）")
                try:
                    pid = db.add_profile(name)
                except Exception:
                    return self._err(400, "方案名称已存在")
                self._changed()
                return self._ok(id=pid)

            if path == "/api/profiles/activate":
                pid = _as_int(data.get("id"))
                if pid is None:
                    return self._err(400, "方案 id 无效")
                if not any(p["id"] == pid for p in db.get_profiles()):
                    return self._err(404, "方案不存在")
                db.activate_profile(pid)
                self._changed()
                return self._ok(id=pid)

            if path == "/api/profiles/delete":
                pid = _as_int(data.get("id"))
                if pid is None:
                    return self._err(400, "方案 id 无效")
                if pid == db.get_active_profile_id():
                    return self._err(400, "不能删除当前启用的方案")
                if not any(p["id"] == pid for p in db.get_profiles()):
                    return self._err(404, "方案不存在")
                db.delete_profile(pid)
                self._changed()
                return self._ok(id=pid)

            return self._err(404, "未知接口")
        except Exception as e:
            # V2.1: 异常细节只进日志，不回传给客户端
            logger.error("POST %s 失败: %s", path, e, exc_info=True)
            return self._err(500, "服务器内部错误，请查看服务器日志")


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """限制并发请求数的 ThreadingHTTPServer（防止局域网设备刷爆线程）

    V2.3.1: Windows 上 SO_REUSEADDR 的语义与 Linux 不同 —— 它允许**另一个进程抢占绑定**
    同一个端口（实机测试中两个实例都成功绑定了 8787，导致"端口被占用"的提示永不触发）。
    因此 Windows 下关闭 SO_REUSEADDR 并显式设置 SO_EXCLUSIVEADDRUSE，让第二个实例直接失败。
    """

    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, server_address, handler,
                 max_workers: int = MAX_CONCURRENT_REQUESTS):
        self._slots = threading.BoundedSemaphore(max_workers)
        ThreadingHTTPServer.__init__(self, server_address, handler)

    def server_bind(self):
        if os.name == "nt":
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except (AttributeError, OSError):
                pass
        ThreadingHTTPServer.server_bind(self)

    def process_request(self, request, client_address):
        if not self._slots.acquire(False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                request.close()
            except Exception:
                pass
            return
        try:
            ThreadingHTTPServer.process_request(self, request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            ThreadingHTTPServer.process_request_thread(self, request, client_address)
        finally:
            self._slots.release()


class BellWebServer:
    """打铃系统 Web 远程控制服务（线程安全，随主程序后台运行）"""

    def __init__(self, db, player, scheduler, port: int = 8787, token: str = "", on_change=None):
        self.db = db
        self.player = player
        self.scheduler = scheduler
        self.port = int(port)
        self.token = str(token or "")
        self.on_change = on_change
        self._httpd = None
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self._httpd is not None:
                return
            handler = type("BoundApiHandler", (ApiHandler,), {
                "ctx": {
                    "db": self.db,
                    "player": self.player,
                    "scheduler": self.scheduler,
                    "token": self.token,
                    "on_change": self.on_change,
                }
            })
            self._httpd = BoundedThreadingHTTPServer(("0.0.0.0", self.port), handler)
            self._thread = threading.Thread(
                target=self._httpd.serve_forever, daemon=True, name="BellWebServer")
            self._thread.start()
            logger.info("🌐 Web 远程控制已启动: %s", ", ".join(self.get_urls()))

    def stop(self):
        with self._lock:
            if self._httpd is None:
                return
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
            self._thread = None
            logger.info("Web 远程控制已停止")

    @property
    def is_running(self) -> bool:
        return self._httpd is not None

    def get_urls(self):
        return get_local_urls(self.port) if self._httpd else []
