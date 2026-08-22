#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
🌐 BellSystem Web 远程控制服务 (V2.0)

- 仅用 Python 标准库 (http.server)，不引入任何第三方依赖，保持绿色免安装
- 网页内嵌为本模块字符串，PyInstaller --onefile 打包无需额外数据文件
- 提供局域网内浏览器/手机访问：查看状态、任务增删改查、立即打铃、暂停、方案切换、日志
- 可选访问令牌(Token)保护
"""

import json
import re
import socket
import threading
import datetime
import logging
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs
from pathlib import Path

logger = logging.getLogger("BellSystem.Web")

WEB_VERSION = "2.0"


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


# ============================================================
# 请求校验工具
# ============================================================
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$")


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
    if not audio:
        return None, "铃声文件不能为空"

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
        "profile_id": int(data.get("profile_id", 0) or 0),
    }, ""


# ============================================================
# HTTP 处理器
# ============================================================
class ApiHandler(BaseHTTPRequestHandler):
    # 由 BellWebServer 注入：{"db","player","scheduler","token","on_change"}
    ctx = {}
    server_version = "BellSystemWeb/" + WEB_VERSION

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

    def _authorized(self) -> bool:
        token = str(self.ctx.get("token") or "")
        if not token:
            return True
        if self.headers.get("X-Auth-Token", "") == token:
            return True
        q = parse_qs(urlsplit(self.path).query)
        supplied = (q.get("token") or [""])[0]
        return supplied == token

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 1000000:
            return {}
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _changed(self):
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
        }

    # ---------- GET ----------
    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_index()
        if not path.startswith("/api/"):
            return self._err(404, "未找到")
        if not self._authorized():
            return self._err(401, "需要访问令牌")
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
            logger.error("GET %s 失败: %s", path, e, exc_info=True)
            return self._err(500, "服务器内部错误: %s" % e)

    # ---------- POST ----------
    def do_POST(self):
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            return self._err(404, "未找到")
        if not self._authorized():
            return self._err(401, "需要访问令牌")
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
                sid = int(data.get("id", 0) or 0)
                row = db.conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
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
                sid = int(data.get("id", 0) or 0)
                row = db.conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
                if not row:
                    return self._err(404, "任务不存在")
                db.delete_schedule(sid)
                self._changed()
                return self._ok(id=sid)

            if path == "/api/schedules/toggle":
                sid = int(data.get("id", 0) or 0)
                row = db.conn.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
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
                sid = data.get("schedule_id")
                if sid:
                    row = db.conn.execute(
                        "SELECT * FROM schedules WHERE id=?", (int(sid),)).fetchone()
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
                    for s in enabled:
                        logger.info("远程立即打铃: %s", s["name"])
                        ok = player.play(s["audio_file"], s["volume"], s["play_count"])
                        make_logger(s)(ok)
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
                pid = int(data.get("id", 0) or 0)
                if not any(p["id"] == pid for p in db.get_profiles()):
                    return self._err(404, "方案不存在")
                db.activate_profile(pid)
                self._changed()
                return self._ok(id=pid)

            if path == "/api/profiles/delete":
                pid = int(data.get("id", 0) or 0)
                if pid == db.get_active_profile_id():
                    return self._err(400, "不能删除当前启用的方案")
                if not any(p["id"] == pid for p in db.get_profiles()):
                    return self._err(404, "方案不存在")
                db.delete_profile(pid)
                self._changed()
                return self._ok(id=pid)

            return self._err(404, "未知接口")
        except Exception as e:
            logger.error("POST %s 失败: %s", path, e, exc_info=True)
            return self._err(500, "服务器内部错误: %s" % e)


# ============================================================
# 服务封装
# ============================================================
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
            self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
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


# ============================================================
# 内嵌网页控制台
# ============================================================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>🔔 打铃远程控制</title>
<style>
:root{--bg:#f2f4f8;--card:#fff;--accent:#2196F3;--accent2:#FF9800;--txt:#333;--sub:#888;--ok:#4CAF50;--bad:#f44336;--line:#e5e8ee}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--txt);padding-bottom:40px}
header{background:var(--accent);color:#fff;padding:14px 16px;display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:5}
header h1{font-size:17px;flex:1;font-weight:700}
select{border:1px solid rgba(255,255,255,.5);background:rgba(255,255,255,.15);color:#fff;border-radius:6px;padding:5px 8px;font-size:13px;max-width:140px}
select option{color:#333}
main{max-width:680px;margin:0 auto;padding:12px}
.card{background:var(--card);border-radius:12px;padding:16px;margin-bottom:12px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.clock{font-size:44px;font-weight:700;text-align:center;font-variant-numeric:tabular-nums;letter-spacing:1px}
.nextline{text-align:center;color:var(--accent);margin-top:6px;font-size:15px;font-weight:600}
.subline{text-align:center;color:var(--sub);font-size:13px;margin-top:2px;min-height:18px}
.btnrow{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
button{border:0;border-radius:8px;padding:10px 14px;font-size:14px;cursor:pointer;background:#eef1f6;color:var(--txt);transition:.15s}
button:active{transform:scale(.96)}
button.primary{background:var(--accent);color:#fff}
button.warn{background:var(--accent2);color:#fff}
h2{font-size:15px;margin-bottom:10px}
.task{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}
.task:last-child{border-bottom:0}
.task .info{flex:1;min-width:0;cursor:pointer}
.task .t{font-weight:700;font-size:17px;font-variant-numeric:tabular-nums}
.task .n{font-size:14px;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.task .m{font-size:12px;color:var(--sub);margin-top:1px}
.task.off .t,.task.off .n{color:#bbb;text-decoration:line-through}
.iconbtn{background:transparent;font-size:16px;padding:6px}
.switch{position:relative;width:42px;height:24px;flex-shrink:0;display:inline-block}
.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#ccc;border-radius:24px;transition:.2s;cursor:pointer}
.slider:before{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.2s}
.switch input:checked+.slider{background:var(--ok)}
.switch input:checked+.slider:before{transform:translateX(18px)}
.empty{text-align:center;color:var(--sub);padding:18px 0;font-size:13px}
details summary{cursor:pointer;font-size:15px;font-weight:700}
.log{display:flex;gap:8px;padding:7px 0;border-bottom:1px solid var(--line);font-size:13px;align-items:center}
.log .lt{color:var(--sub);white-space:nowrap;font-variant-numeric:tabular-nums}
.log .ln{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.log .ls{white-space:nowrap}
.log.ok .ls{color:var(--ok)}.log.bad .ls{color:var(--bad)}
#conn{position:fixed;bottom:0;left:0;right:0;text-align:center;font-size:12px;color:var(--sub);padding:6px;background:var(--bg);z-index:6}
#conn.bad{color:var(--bad)}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:20;align-items:flex-end;justify-content:center}
.modal.show{display:flex}
.sheet{background:#fff;width:100%;max-width:680px;border-radius:16px 16px 0 0;padding:18px 16px 24px;max-height:88vh;overflow-y:auto}
.sheet h3{margin-bottom:12px;font-size:16px}
.field{margin-bottom:12px}
.field label{display:block;font-size:13px;color:var(--sub);margin-bottom:4px}
.field input[type=text],.field input[type=date],.field input[type=time]{width:100%;border:1px solid var(--line);border-radius:8px;padding:9px 10px;font-size:15px}
.radios{display:flex;gap:6px;flex-wrap:wrap}
.radios label{cursor:pointer}
.radios input{display:none}
.radios span{display:inline-block;border:1px solid var(--line);border-radius:8px;padding:7px 12px;font-size:13px}
.radios input:checked+span{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:700}
.weeks{display:flex;gap:8px;flex-wrap:wrap}
.weeks label{cursor:pointer}
.weeks input{display:none}
.weeks span{display:flex;width:36px;height:36px;border-radius:50%;border:1px solid var(--line);align-items:center;justify-content:center;font-size:13px}
.weeks input:checked+span{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:700}
.volrow{display:flex;align-items:center;gap:10px}
.volrow input{flex:1}
.sheetbtns{display:flex;gap:10px;margin-top:16px}
.sheetbtns button{flex:1;padding:12px;font-size:15px}
.toast{position:fixed;top:60px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.75);color:#fff;padding:8px 18px;border-radius:20px;font-size:13px;z-index:30;display:none;max-width:80vw}
</style>
</head>
<body>
<header>
  <h1>🔔 打铃远程控制</h1>
  <select id="profileSel" onchange="switchProfile(this.value)"></select>
</header>
<main>
  <section class="card">
    <div class="clock" id="clock">--:--:--</div>
    <div class="nextline" id="nextTime">--</div>
    <div class="subline" id="nextDist"></div>
    <div class="btnrow">
      <button id="pauseBtn" class="primary" onclick="togglePause()">⏸ 暂停打铃</button>
      <button class="warn" onclick="ringAll()">🔔 立即打铃</button>
      <button onclick="stopPlay()">⏹ 停止播放</button>
    </div>
  </section>

  <section class="card">
    <h2>📋 打铃任务</h2>
    <div id="taskList"><div class="empty">加载中…</div></div>
    <div class="btnrow"><button class="primary" onclick="openEditor()">➕ 添加任务</button></div>
  </section>

  <details class="card">
    <summary>📜 执行日志（点击展开）</summary>
    <div id="logList" style="margin-top:10px"><div class="empty">加载中…</div></div>
    <div class="btnrow"><button onclick="loadLogs()">刷新日志</button></div>
  </details>
</main>
<div id="conn">连接中…</div>
<div class="toast" id="toast"></div>

<div class="modal" id="modal">
  <div class="sheet">
    <h3 id="modalTitle">添加任务</h3>
    <input type="hidden" id="f_id">
    <div class="field"><label>任务名称</label><input type="text" id="f_name" maxlength="50" placeholder="如：上午第一节课"></div>
    <div class="field"><label>重复方式</label>
      <div class="radios" id="f_type">
        <label><input type="radio" name="stype" value="daily" onchange="typeChanged()"><span>每天</span></label>
        <label><input type="radio" name="stype" value="weekly" checked onchange="typeChanged()"><span>每周</span></label>
        <label><input type="radio" name="stype" value="date" onchange="typeChanged()"><span>指定日期</span></label>
        <label><input type="radio" name="stype" value="once" onchange="typeChanged()"><span>一次性</span></label>
      </div>
    </div>
    <div class="field" id="weekField"><label>星期</label>
      <div class="weeks" id="f_weeks"></div>
    </div>
    <div class="field" id="dateField" style="display:none"><label>日期</label><input type="date" id="f_date"></div>
    <div class="field"><label>时间</label><input type="time" id="f_time" value="08:00"></div>
    <div class="field"><label>铃声文件（服务器电脑上的完整路径）</label>
      <input type="text" id="f_audio" list="audioList" placeholder="如 C:\bells\class.mp3">
      <datalist id="audioList"></datalist>
    </div>
    <div class="field"><label>音量：<output id="f_volOut">80</output>%</label>
      <div class="volrow"><input type="range" id="f_vol" min="0" max="100" value="80" oninput="document.getElementById('f_volOut').value=this.value"></div>
    </div>
    <div class="field"><label style="display:flex;align-items:center;gap:8px;color:var(--txt);font-size:14px">
      <input type="checkbox" id="f_enabled" checked style="width:18px;height:18px"> 启用该任务</label></div>
    <div class="sheetbtns">
      <button onclick="closeEditor()">取消</button>
      <button class="primary" onclick="saveTask()">保存</button>
    </div>
  </div>
</div>

<script>
let TOKEN = localStorage.getItem('bell_token') || '';
let SCHEDULES = [], NEXT = null, NEXT_LEFT = 0, LAST_TICK = Date.now();

const $ = id => document.getElementById(id);
const WEEK = ['一','二','三','四','五','六','日'];
const TYPE_TXT = {daily:'每天', weekly:'每周', date:'指定日期', once:'一次性'};

function toast(msg, ms){
  ms = ms || 2200;
  const t = $('toast'); t.textContent = msg; t.style.display='block';
  clearTimeout(t._h); t._h = setTimeout(function(){t.style.display='none';}, ms);
}

async function api(path, opts, tries){
  opts = opts || {}; tries = tries || 0;
  const sep = path.indexOf('?') >= 0 ? '&' : '?';
  const url = TOKEN ? path + sep + 'token=' + encodeURIComponent(TOKEN) : path;
  let r;
  try { r = await fetch(url, Object.assign({headers:{'Content-Type':'application/json'}}, opts)); }
  catch(e){ setConn(false); throw new Error('网络错误'); }
  if(r.status === 401){
    if(tries >= 2) throw new Error('令牌错误');
    const t = prompt('请输入访问令牌 (Token)') || '';
    TOKEN = t; localStorage.setItem('bell_token', t);
    return api(path, opts, tries+1);
  }
  setConn(true);
  const j = await r.json();
  if(!j.ok) throw new Error(j.error || '请求失败');
  return j;
}

function setConn(ok){
  const c = $('conn');
  c.textContent = ok ? '● 已连接' : '● 连接断开，自动重试中…';
  c.className = ok ? '' : 'bad';
}

function esc(s){ const d=document.createElement('div'); d.textContent=(s==null?'':s); return d.innerHTML; }

// ---------- 状态 ----------
async function pollStatus(){
  try{
    const j = await api('/api/status');
    const s = j.status;
    $('clock').textContent = s.now.slice(11);
    $('pauseBtn').textContent = s.paused ? '▶ 恢复打铃' : '⏸ 暂停打铃';
    NEXT = s.next; NEXT_LEFT = s.next ? s.next.in_seconds : 0; LAST_TICK = Date.now();
    renderNext();
    renderProfiles(s.active_profile);
  }catch(e){ /* 连接状态已在 api() 中更新 */ }
}

function renderNext(){
  if(!NEXT){ $('nextTime').textContent='暂无任务'; $('nextDist').textContent=''; return; }
  const left = Math.max(0, NEXT_LEFT - Math.floor((Date.now()-LAST_TICK)/1000));
  $('nextTime').textContent = '下一次：' + NEXT.time + ' · ' + NEXT.name;
  const h=Math.floor(left/3600), m=Math.floor(left%3600/60), sec=left%60;
  $('nextDist').textContent = '距离 ' + (h>0? h+' 时 ':'') + m+' 分 '+sec+' 秒';
}

// ---------- 方案 ----------
let PROFILES = [];
function renderProfiles(active){
  const sel = $('profileSel');
  sel.innerHTML = PROFILES.map(function(p){
    return '<option value="'+p.id+'"'+(active && p.id===active.id?' selected':'')+'>'+esc(p.name)+(p.is_active?' ✓':'')+'</option>';
  }).join('');
}
async function loadProfiles(){
  try{ const j = await api('/api/profiles'); PROFILES = j.profiles; }catch(e){}
}
async function switchProfile(id){
  try{ await api('/api/profiles/activate', {method:'POST', body:JSON.stringify({id:+id})});
    toast('已切换方案'); loadSchedules(); }
  catch(e){ toast(e.message); pollStatus(); }
}

// ---------- 任务 ----------
async function loadSchedules(){
  try{
    const j = await api('/api/schedules'); SCHEDULES = j.schedules;
    $('audioList').innerHTML = [...new Set(SCHEDULES.map(function(s){return s.audio_file;}))]
      .map(function(f){ return '<option value="'+esc(f)+'">'; }).join('');
    const box = $('taskList');
    if(!SCHEDULES.length){ box.innerHTML = '<div class="empty">暂无任务，点击下方按钮添加</div>'; return; }
    box.innerHTML = SCHEDULES.map(function(s){
      const meta = [TYPE_TXT[s.schedule_type] || s.schedule_type];
      if(s.schedule_type==='weekly' && s.week_days.length)
        meta.push('周' + s.week_days.map(function(d){return WEEK[d];}).join('、'));
      if((s.schedule_type==='date'||s.schedule_type==='once') && s.date_str) meta.push(s.date_str);
      meta.push(s.volume + '%');
      return '<div class="task'+(s.enabled?'':' off')+'">'
        +'<label class="switch"><input type="checkbox"'+(s.enabled?' checked':'')+' onchange="toggleTask('+s.id+',this.checked)"><span class="slider"></span></label>'
        +'<div class="info" onclick="openEditor('+s.id+')">'
        +'<div class="t">'+s.time_str.slice(0,5)+'</div>'
        +'<div class="n">'+esc(s.name)+'</div>'
        +'<div class="m">'+meta.map(esc).join(' · ')+'</div></div>'
        +'<button class="iconbtn" title="播放" onclick="ringOne('+s.id+')">▶</button>'
        +'<button class="iconbtn" title="编辑" onclick="openEditor('+s.id+')">✏️</button>'
        +'<button class="iconbtn" title="删除" onclick="delTask('+s.id+')">🗑</button>'
        +'</div>';
    }).join('');
  }catch(e){ $('taskList').innerHTML = '<div class="empty">'+esc(e.message)+'</div>'; }
}

async function toggleTask(id, enabled){
  try{ await api('/api/schedules/toggle', {method:'POST', body:JSON.stringify({id:id, enabled:enabled})}); }
  catch(e){ toast(e.message); }
  loadSchedules();
}
async function delTask(id){
  const s = SCHEDULES.find(function(x){return x.id===id;});
  if(!confirm('确定删除任务「'+(s?s.name:id)+'」吗？')) return;
  try{ await api('/api/schedules/delete', {method:'POST', body:JSON.stringify({id:id})}); toast('已删除'); }
  catch(e){ toast(e.message); }
  loadSchedules();
}
async function ringOne(id){
  try{ const j = await api('/api/ring', {method:'POST', body:JSON.stringify({schedule_id:id})}); toast(j.message||'播放中'); }
  catch(e){ toast(e.message); }
}
async function ringAll(){
  try{ const j = await api('/api/ring', {method:'POST', body:'{}'}); toast(j.message||'播放中'); }
  catch(e){ toast(e.message); }
}
async function stopPlay(){
  try{ await api('/api/stop', {method:'POST', body:'{}'}); toast('已停止'); }
  catch(e){ toast(e.message); }
}
async function togglePause(){
  try{ const j = await api('/api/pause', {method:'POST', body:'{}'});
    $('pauseBtn').textContent = j.paused ? '▶ 恢复打铃' : '⏸ 暂停打铃';
    toast(j.paused ? '已暂停打铃' : '已恢复打铃'); }
  catch(e){ toast(e.message); }
}

// ---------- 编辑弹窗 ----------
function buildWeeks(){
  $('f_weeks').innerHTML = WEEK.map(function(w,i){
    return '<label><input type="checkbox" value="'+i+'"'+(i<5?' checked':'')+'><span>'+w+'</span></label>';
  }).join('');
}
function typeChanged(){
  const t = document.querySelector('input[name=stype]:checked').value;
  $('weekField').style.display = t==='weekly' ? '' : 'none';
  $('dateField').style.display = (t==='date'||t==='once') ? '' : 'none';
}
function openEditor(id){
  const s = id ? SCHEDULES.find(function(x){return x.id===id;}) : null;
  $('modalTitle').textContent = s ? '编辑任务' : '添加任务';
  $('f_id').value = s ? s.id : '';
  $('f_name').value = s ? s.name : '';
  document.querySelector('input[name=stype][value="'+(s?s.schedule_type:'weekly')+'"]').checked = true;
  document.querySelectorAll('#f_weeks input').forEach(function(cb){
    cb.checked = s && s.schedule_type==='weekly' ? s.week_days.indexOf(+cb.value)>=0 : (+cb.value)<5;
  });
  $('f_date').value = s ? s.date_str : '';
  $('f_time').value = s ? s.time_str.slice(0,5) : '08:00';
  $('f_audio').value = s ? s.audio_file : '';
  $('f_vol').value = s ? s.volume : 80; $('f_volOut').value = s ? s.volume : 80;
  $('f_enabled').checked = s ? !!s.enabled : true;
  typeChanged();
  $('modal').classList.add('show');
}
function closeEditor(){ $('modal').classList.remove('show'); }
async function saveTask(){
  const type = document.querySelector('input[name=stype]:checked').value;
  const payload = {
    id: $('f_id').value ? +$('f_id').value : undefined,
    name: $('f_name').value.trim(),
    schedule_type: type,
    time_str: $('f_time').value,
    week_days: [...document.querySelectorAll('#f_weeks input:checked')].map(function(cb){return +cb.value;}),
    date_str: $('f_date').value,
    audio_file: $('f_audio').value.trim(),
    volume: +$('f_vol').value,
    enabled: $('f_enabled').checked,
  };
  if(!payload.name) return toast('请输入任务名称');
  if(!payload.audio_file) return toast('请填写铃声文件路径');
  if(type==='weekly' && !payload.week_days.length) return toast('请至少选择一个星期');
  if((type==='date'||type==='once') && !payload.date_str) return toast('请选择日期');
  try{
    await api(payload.id ? '/api/schedules/update' : '/api/schedules/create',
      {method:'POST', body:JSON.stringify(payload)});
    toast('已保存'); closeEditor(); loadSchedules();
  }catch(e){ toast(e.message); }

// ---------- 日志 ----------
async function loadLogs(){
  try{
    const j = await api('/api/logs?limit=100');
    const box = $('logList');
    if(!j.logs.length){ box.innerHTML='<div class="empty">暂无日志</div>'; return; }
    box.innerHTML = j.logs.map(function(l){
      return '<div class="log '+(l.status==='success'?'ok':'bad')+'">'
        +'<span class="lt">'+esc(l.time||'')+'</span>'
        +'<span class="ln">'+esc(l.name||'')+'（'+esc(l.audio||'')+'）</span>'
        +'<span class="ls">'+(l.status==='success'?'✅ 成功':'❌ '+esc(l.error||'失败'))+'</span></div>';
    }).join('');
  }catch(e){ $('logList').innerHTML = '<div class="empty">'+esc(e.message)+'</div>'; }
}

// ---------- 启动 ----------
buildWeeks();
loadProfiles().then(loadSchedules);
pollStatus(); loadLogs();
setInterval(renderNext, 1000);
setInterval(pollStatus, 2000);
setInterval(loadSchedules, 15000);
</script>
</body>
</html>"""
