# -*- coding: utf-8 -*-
"""内嵌网页控制台源码"""

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
.alertbar{background:#fdecea;border:1px solid #f5b5ae;color:#8c1d13;border-radius:10px;padding:10px 12px;font-size:13px;margin-bottom:12px;line-height:1.6}
.alertbar b{font-weight:700}
.secwarn{background:#fff4e5;border:1px solid #ffcc80;color:#8a5300;border-radius:10px;padding:10px 12px;font-size:13px;margin-bottom:12px;line-height:1.5}
.toast{position:fixed;top:60px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.75);color:#fff;padding:8px 18px;border-radius:20px;font-size:13px;z-index:30;display:none;max-width:80vw}
</style>
</head>
<body>
<header>
  <h1>🔔 打铃远程控制</h1>
  <select id="profileSel" onchange="switchProfile(this.value)"></select>
</header>
<main>
  <div class="alertbar" id="alertBar" style="display:none"></div>
  <div class="secwarn" id="secWarn" style="display:none">⚠ 未设置访问令牌：同一局域网内任何设备都能打开本页操作打铃。建议在电脑端「系统设置 → 访问令牌」设置。</div>
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

var $ = function(id){ return document.getElementById(id); };
var WEEK = ['一','二','三','四','五','六','日'];
var TYPE_TXT = {daily:'每天', weekly:'每周', date:'指定日期', once:'一次性'};

function toast(msg, ms){
  ms = ms || 2200;
  var t = $('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(t._h);
  t._h = setTimeout(function(){ t.style.display = 'none'; }, ms);
}

function setConn(ok){
  var c = $('conn');
  c.textContent = ok ? '● 已连接' : '● 连接断开，自动重试中…';
  c.className = ok ? '' : 'bad';
}

function esc(s){
  var d = document.createElement('div');
  d.textContent = (s === null || s === undefined) ? '' : String(s);
  return d.innerHTML;
}

function authHeaders(){
  // V2.1: 令牌只走请求头，不再拼进 URL（避免进浏览器历史/代理日志/Referer）
  var h = {'Content-Type':'application/json'};
  if (TOKEN) h['X-Auth-Token'] = TOKEN;
  return h;
}

function api(path, opts, tries){
  opts = opts || {};
  tries = tries || 0;
  return fetch(path, Object.assign({}, opts, {headers: authHeaders()}))
    .then(function(r){
      if (r.status === 401) {
        if (tries >= 2) throw new Error('令牌错误');
        var t = prompt('请输入访问令牌 (Token)') || '';
        TOKEN = t;
        localStorage.setItem('bell_token', t);
        return api(path, opts, tries + 1);
      }
      setConn(true);
      return r.json();
    })
    .then(function(j){
      if (!j || !j.ok) throw new Error((j && j.error) || '请求失败');
      return j;
    })
    .catch(function(e){
      if (e && e.message === 'Failed to fetch') { setConn(false); throw new Error('网络错误'); }
      throw e;
    });
}

// ---------- 状态 ----------
function pollStatus(){
  return api('/api/status').then(function(j){
    var s = j.status;
    $('clock').textContent = s.now.slice(11);
    $('pauseBtn').textContent = s.paused ? '▶ 恢复打铃' : '⏸ 暂停打铃';
    NEXT = s.next;
    NEXT_LEFT = s.next ? s.next.in_seconds : 0;
    LAST_TICK = Date.now();
    renderNext();
    renderProfiles(s.active_profile);
    var warn = $('secWarn');
    if (warn) warn.style.display = (s.auth_required === false) ? 'block' : 'none';
    renderAlerts(s.alerts);
  }).catch(function(){});
}

function renderNext(){
  if (!NEXT) {
    $('nextTime').textContent = '暂无任务';
    $('nextDist').textContent = '';
    return;
  }
  var left = Math.max(0, NEXT_LEFT - Math.floor((Date.now() - LAST_TICK) / 1000));
  $('nextTime').textContent = '下一次：' + NEXT.time + ' · ' + NEXT.name;
  var h = Math.floor(left / 3600);
  var m = Math.floor(left % 3600 / 60);
  var sec = left % 60;
  $('nextDist').textContent = '距离 ' + (h > 0 ? h + ' 时 ' : '') + m + ' 分 ' + sec + ' 秒';
}

// ---------- 失败/漏打告警 ----------
function renderAlerts(alerts){
  var box = $('alertBar');
  if (!box) return;
  if (!alerts || !alerts.length) { box.style.display = 'none'; return; }
  var html = '<b>⚠ 最近 24 小时有 ' + alerts.length + ' 次异常</b><br>';
  for (var i = 0; i < alerts.length; i++) {
    var a = alerts[i];
    var tag = a.status === 'missed' ? '漏打' : '失败';
    html += esc(a.time || '') + ' · ' + esc(a.name || '') + ' · ' + tag
         + (a.error ? '（' + esc(a.error) + '）' : '') + '<br>';
  }
  box.innerHTML = html;
  box.style.display = 'block';
}

// ---------- 方案 ----------
var PROFILES = [];
function loadProfiles(){
  return api('/api/profiles').then(function(j){ PROFILES = j.profiles; }).catch(function(){});
}
function renderProfiles(active){
  var html = '';
  for (var i = 0; i < PROFILES.length; i++) {
    var p = PROFILES[i];
    var sel = (active && p.id === active.id) ? ' selected' : '';
    html += '<option value="' + p.id + '"' + sel + '>' + esc(p.name) + (p.is_active ? ' ✓' : '') + '</option>';
  }
  $('profileSel').innerHTML = html;
}
function switchProfile(id){
  api('/api/profiles/activate', {method:'POST', body:JSON.stringify({id:+id})})
    .then(function(j){ toast('已切换方案'); loadProfiles(); loadSchedules(); })
    .catch(function(e){ toast(e.message); pollStatus(); });
}

// ---------- 任务 ----------
function loadSchedules(){
  return api('/api/schedules').then(function(j){
    SCHEDULES = j.schedules;
    var files = [];
    for (var k = 0; k < SCHEDULES.length; k++) {
      if (files.indexOf(SCHEDULES[k].audio_file) < 0) files.push(SCHEDULES[k].audio_file);
    }
    var dl = '';
    for (var k2 = 0; k2 < files.length; k2++) dl += '<option value="' + esc(files[k2]) + '">';
    $('audioList').innerHTML = dl;

    var box = $('taskList');
    if (!SCHEDULES.length) {
      box.innerHTML = '<div class="empty">暂无任务，点击下方按钮添加</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < SCHEDULES.length; i++) {
      var s = SCHEDULES[i];
      var meta = [TYPE_TXT[s.schedule_type] || s.schedule_type];
      if (s.schedule_type === 'weekly' && s.week_days.length) {
        var names = [];
        for (var d = 0; d < s.week_days.length; d++) names.push(WEEK[s.week_days[d]]);
        meta.push('周' + names.join('、'));
      }
      if ((s.schedule_type === 'date' || s.schedule_type === 'once') && s.date_str) meta.push(s.date_str);
      meta.push(s.volume + '%');
      html += '<div class="task' + (s.enabled ? '' : ' off') + '">'
        + '<label class="switch"><input type="checkbox"' + (s.enabled ? ' checked' : '') + ' onchange="toggleTask(' + s.id + ',this.checked)"><span class="slider"></span></label>'
        + '<div class="info" onclick="openEditor(' + s.id + ')">'
        + '<div class="t">' + esc(s.time_str.slice(0,5)) + '</div>'
        + '<div class="n">' + esc(s.name) + '</div>'
        + '<div class="m">' + esc(meta.join(' · ')) + '</div></div>'
        + '<button class="iconbtn" title="播放" onclick="ringOne(' + s.id + ')">▶</button>'
        + '<button class="iconbtn" title="编辑" onclick="openEditor(' + s.id + ')">✏️</button>'
        + '<button class="iconbtn" title="删除" onclick="delTask(' + s.id + ')">🗑</button>'
        + '</div>';
    }
    box.innerHTML = html;
  }).catch(function(e){
    $('taskList').innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
  });
}

function findTask(id){
  for (var i = 0; i < SCHEDULES.length; i++) if (SCHEDULES[i].id === id) return SCHEDULES[i];
  return null;
}

function toggleTask(id, enabled){
  api('/api/schedules/toggle', {method:'POST', body:JSON.stringify({id:id, enabled:enabled})})
    .catch(function(e){ toast(e.message); });
  loadSchedules();
}
function delTask(id){
  var s = findTask(id);
  if (!confirm('确定删除任务「' + (s ? s.name : id) + '」吗？')) return;
  api('/api/schedules/delete', {method:'POST', body:JSON.stringify({id:id})})
    .then(function(){ toast('已删除'); })
    .catch(function(e){ toast(e.message); });
  loadSchedules();
}
function ringOne(id){
  api('/api/ring', {method:'POST', body:JSON.stringify({schedule_id:id})})
    .then(function(j){ toast(j.message || '播放中'); })
    .catch(function(e){ toast(e.message); });
}
function ringAll(){
  api('/api/ring', {method:'POST', body:'{}'})
    .then(function(j){ toast(j.message || '播放中'); })
    .catch(function(e){ toast(e.message); });
}
function stopPlay(){
  api('/api/stop', {method:'POST', body:'{}'})
    .then(function(){ toast('已停止'); })
    .catch(function(e){ toast(e.message); });
}
function togglePause(){
  api('/api/pause', {method:'POST', body:'{}'})
    .then(function(j){
      $('pauseBtn').textContent = j.paused ? '▶ 恢复打铃' : '⏸ 暂停打铃';
      toast(j.paused ? '已暂停打铃' : '已恢复打铃');
    })
    .catch(function(e){ toast(e.message); });
}

// ---------- 编辑弹窗 ----------
function buildWeeks(){
  var html = '';
  for (var i = 0; i < WEEK.length; i++) {
    html += '<label><input type="checkbox" value="' + i + '"' + (i < 5 ? ' checked' : '') + '><span>' + WEEK[i] + '</span></label>';
  }
  $('f_weeks').innerHTML = html;
}
function setTypeRadio(v){
  var radios = document.querySelectorAll('input[name=stype]');
  for (var i = 0; i < radios.length; i++) radios[i].checked = (radios[i].value === v);
}
function getType(){
  var radios = document.querySelectorAll('input[name=stype]');
  for (var i = 0; i < radios.length; i++) if (radios[i].checked) return radios[i].value;
  return 'weekly';
}
function typeChanged(){
  var t = getType();
  $('weekField').style.display = (t === 'weekly') ? '' : 'none';
  $('dateField').style.display = (t === 'date' || t === 'once') ? '' : 'none';
}
function openEditor(id){
  // 每次打开都重建星期复选框，杜绝残留勾选状态
  buildWeeks();
  var s = id ? findTask(id) : null;
  $('modalTitle').textContent = s ? '编辑任务' : '添加任务';
  $('f_id').value = s ? s.id : '';
  $('f_name').value = s ? s.name : '';
  setTypeRadio(s ? s.schedule_type : 'weekly');
  var boxes = document.querySelectorAll('#f_weeks input');
  for (var i = 0; i < boxes.length; i++) {
    var v = +boxes[i].value;
    if (s && s.schedule_type === 'weekly') {
      boxes[i].checked = s.week_days.indexOf(v) >= 0;
    } else {
      boxes[i].checked = v < 5;
    }
  }
  $('f_date').value = s ? s.date_str : '';
  $('f_time').value = s ? s.time_str.slice(0,5) : '08:00';
  $('f_audio').value = s ? s.audio_file : '';
  $('f_vol').value = s ? s.volume : 80;
  $('f_volOut').value = s ? s.volume : 80;
  $('f_enabled').checked = s ? !!s.enabled : true;
  typeChanged();
  $('modal').classList.add('show');
}
function closeEditor(){ $('modal').classList.remove('show'); }
function saveTask(){
  var type = getType();
  var boxes = document.querySelectorAll('#f_weeks input:checked');
  var days = [];
  for (var i = 0; i < boxes.length; i++) days.push(+boxes[i].value);
  var payload = {
    name: $('f_name').value.trim(),
    schedule_type: type,
    time_str: $('f_time').value,
    week_days: days,
    date_str: $('f_date').value,
    audio_file: $('f_audio').value.trim(),
    volume: +$('f_vol').value,
    enabled: $('f_enabled').checked
  };
  var fid = $('f_id').value;
  if (fid) payload.id = +fid;
  if (!payload.name) { toast('请输入任务名称'); return; }
  if (!payload.audio_file) { toast('请填写铃声文件路径'); return; }
  if (type === 'weekly' && !payload.week_days.length) { toast('请至少选择一个星期'); return; }
  if ((type === 'date' || type === 'once') && !payload.date_str) { toast('请选择日期'); return; }
  var url = fid ? '/api/schedules/update' : '/api/schedules/create';
  api(url, {method:'POST', body:JSON.stringify(payload)})
    .then(function(){ toast('已保存'); closeEditor(); loadSchedules(); })
    .catch(function(e){ toast(e.message); });
}

// ---------- 日志 ----------
function loadLogs(){
  return api('/api/logs?limit=100').then(function(j){
    var box = $('logList');
    if (!j.logs.length) { box.innerHTML = '<div class="empty">暂无日志</div>'; return; }
    var html = '';
    for (var i = 0; i < j.logs.length; i++) {
      var l = j.logs[i];
      var cls = l.status === 'success' ? 'ok' : 'bad';
      var st = l.status === 'success' ? '✅ 成功' : '❌ ' + esc(l.error || '失败');
      html += '<div class="log ' + cls + '">'
        + '<span class="lt">' + esc(l.time || '') + '</span>'
        + '<span class="ln">' + esc(l.name || '') + '（' + esc(l.audio || '') + '）</span>'
        + '<span class="ls">' + st + '</span></div>';
    }
    box.innerHTML = html;
  }).catch(function(e){
    $('logList').innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
  });
}

// ---------- 启动 ----------
buildWeeks();
loadProfiles();
loadSchedules();
pollStatus();
loadLogs();
setInterval(renderNext, 1000);
setInterval(pollStatus, 2000);
setInterval(loadSchedules, 15000);
</script>
</body>
</html>"""
