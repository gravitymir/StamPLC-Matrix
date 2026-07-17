"""
StamPLC — Compressor Control
============================

M5Stack StamPLC (ESP32-S3) firmware that drives:
  * SSR (solid-state relay) on GPIO 41 — main compressor switch
  * Servo on GPIO 40 (PWM) — mechanical status indicator on the OFF/ON lever
  * Local LCD, physical buttons A/B/C, speaker
  * ESP-NOW broadcast to a companion Atom Matrix indicator
  * HTTP web UI + JSON API on port 80
  * Persistent daily schedule (per-weekday) with permanent + user entries
  * Persistent timezone offset (survives power loss)

See docs/api.md for the HTTP endpoints and docs/hardware.md for wiring.
"""

import os, sys, io
import M5
from M5 import *
from m5espnow import M5ESPNow
from hardware import RTC
from hardware import PWM
from hardware import Pin
import network
import socket
import time
import json


# ----- CONFIG (edit for your network) -----
WIFI_SSID = 'YourWiFiSSID'
WIFI_PASS = 'YourWiFiPassword'

# Permanent OFF times — locked in the web UI. Day bitmask: bit0=Mon..bit6=Sun.
# 31 = Mon-Fri, 96 = weekends, 127 = all days.
PERMANENT_OFF_TIMES = [
    {'time': '12:55', 'days': 31},
    {'time': '17:55', 'days': 31},
]

SCHEDULE_FILE = 'schedules.json'
TZ_FILE       = 'tz.json'


# ---------- globals ----------
rect0 = None
Init = None
espnow_0 = None
rtc = None
wlan_sta = None
pwm40 = None
pin41 = None
http_sock = None

espnow_mac = None
espnow_data = None
time_delay = None
relay_status = False

user_off_times = []
user_on_times = []
last_minute_str = None
tz_offset_hours = 0


# ---------- helpers ----------
def log(*args):
    print('[%6d ms] ' % time.ticks_ms(), *args)


def move_servo_then_release(angle):
    angle = max(-30, min(180, angle))
    duty = max(0, 31 + angle * 92 // 180)
    pwm40.duty(duty)
    time.sleep_ms(1000)
    pwm40.duty(0)
    log('servo: moved to', angle, 'deg, released')


def url_decode(s):
    out = ''
    i = 0
    while i < len(s):
        c = s[i]
        if c == '%' and i + 2 < len(s):
            try:
                out += chr(int(s[i+1:i+3], 16))
                i += 3
                continue
            except:
                pass
        if c == '+':
            out += ' '
        else:
            out += c
        i += 1
    return out


def parse_query(qs):
    params = {}
    for pair in qs.split('&'):
        if '=' in pair:
            k, v = pair.split('=', 1)
            params[url_decode(k)] = url_decode(v)
    return params


def valid_time(s):
    if not s or len(s) != 5 or s[2] != ':':
        return False
    try:
        h = int(s[:2]); m = int(s[3:])
        return 0 <= h <= 23 and 0 <= m <= 59
    except:
        return False


def normalize_entry(e):
    if isinstance(e, str):
        return {'time': e, 'days': 127}
    days = int(e.get('days', 127)) & 127
    return {'time': e.get('time', ''), 'days': days if days else 127}


# ---------- timezone offset (persistent) ----------
def load_tz():
    global tz_offset_hours
    try:
        with open(TZ_FILE, 'r') as f:
            d = json.loads(f.read())
        tz_offset_hours = int(d.get('offset', 0))
        log('TZ offset loaded:', tz_offset_hours, 'h')
    except Exception as e:
        log('TZ: starting at 0,', e)
        tz_offset_hours = 0


def save_tz():
    try:
        with open(TZ_FILE, 'w') as f:
            f.write(json.dumps({'offset': tz_offset_hours}))
        log('TZ offset saved:', tz_offset_hours, 'h')
    except Exception as e:
        log('save_tz error:', e)


def _days_in_month(y, mo):
    if mo == 2:
        if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0):
            return 29
        return 28
    return [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1]


def adjusted_datetime():
    """RTC + tz_offset_hours, with manual day/month/year rollover.
    Avoids time.mktime/localtime which can apply unexpected TZ on some MicroPython builds."""
    now = rtc.datetime()
    if tz_offset_hours == 0:
        return now
    y, mo, d, wd, h, m, s, us = now
    h += tz_offset_hours
    while h >= 24:
        h -= 24; d += 1; wd = (wd + 1) % 7
        if d > _days_in_month(y, mo):
            d = 1; mo += 1
            if mo > 12: mo = 1; y += 1
    while h < 0:
        h += 24; d -= 1; wd = (wd - 1) % 7
        if d < 1:
            mo -= 1
            if mo < 1: mo = 12; y -= 1
            d = _days_in_month(y, mo)
    return (y, mo, d, wd, h, m, s, us)


# ---------- schedule storage + check ----------
def load_schedules():
    global user_off_times, user_on_times
    try:
        with open(SCHEDULE_FILE, 'r') as f:
            d = json.loads(f.read())
        user_off_times = [normalize_entry(e) for e in d.get('off', []) if valid_time(normalize_entry(e)['time'])]
        user_on_times  = [normalize_entry(e) for e in d.get('on',  []) if valid_time(normalize_entry(e)['time'])]
        log('schedules loaded:', len(user_off_times), 'off,', len(user_on_times), 'on')
    except Exception as e:
        log('schedules: starting fresh,', e)
        user_off_times = []
        user_on_times = []


def save_schedules():
    try:
        with open(SCHEDULE_FILE, 'w') as f:
            f.write(json.dumps({'off': user_off_times, 'on': user_on_times}))
        log('schedules saved')
    except Exception as e:
        log('save_schedules error:', e)


def add_schedule(kind, time_str, days_mask):
    global user_off_times, user_on_times
    if not valid_time(time_str): return False
    days_mask = int(days_mask) & 127
    if days_mask == 0: return False
    perm_times = [p['time'] for p in PERMANENT_OFF_TIMES]
    if kind == 'off':
        if time_str in perm_times: return False
        for e in user_off_times:
            if e['time'] == time_str:
                e['days'] = days_mask; save_schedules(); return True
        user_off_times.append({'time': time_str, 'days': days_mask})
        user_off_times.sort(key=lambda x: x['time'])
    elif kind == 'on':
        for e in user_on_times:
            if e['time'] == time_str:
                e['days'] = days_mask; save_schedules(); return True
        user_on_times.append({'time': time_str, 'days': days_mask})
        user_on_times.sort(key=lambda x: x['time'])
    else:
        return False
    save_schedules()
    return True


def del_schedule(kind, time_str):
    global user_off_times, user_on_times
    if kind == 'off':
        before = len(user_off_times)
        user_off_times = [e for e in user_off_times if e['time'] != time_str]
        if len(user_off_times) < before: save_schedules(); return True
    elif kind == 'on':
        before = len(user_on_times)
        user_on_times = [e for e in user_on_times if e['time'] != time_str]
        if len(user_on_times) < before: save_schedules(); return True
    return False


def schedule_check():
    global last_minute_str
    now = adjusted_datetime()
    current = '%02d:%02d' % (now[4], now[5])
    if current == last_minute_str: return
    last_minute_str = current
    today_bit = 1 << now[3]
    for e in PERMANENT_OFF_TIMES:
        if e['time'] == current and (e['days'] & today_bit):
            log('SCHEDULE: permanent auto-OFF at', current); turn_off(); break
    for e in user_off_times:
        if e['time'] == current and (e['days'] & today_bit):
            log('SCHEDULE: user auto-OFF at', current, 'days=', e['days']); turn_off(); break
    for e in user_on_times:
        if e['time'] == current and (e['days'] & today_bit):
            log('SCHEDULE: user auto-ON at', current, 'days=', e['days']); turn_on(); break


# ---------- web page ----------
HTML = """<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Compressor</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 body{
   font-family:-apple-system,Segoe UI,Roboto,sans-serif;
   background:linear-gradient(135deg,#0f2027 0%,#203a43 50%,#2c5364 100%);
   min-height:100vh;padding:18px 16px 8px;color:#fff
 }
 .wrap{max-width:440px;margin:0 auto;display:flex;flex-direction:column;gap:18px}
 .card{
   background:rgba(0,0,0,0.35);backdrop-filter:blur(14px);
   border:1px solid rgba(255,255,255,0.08);
   border-radius:22px;padding:22px 20px;
   box-shadow:0 24px 70px rgba(0,0,0,0.5)
 }

 .clock-card{text-align:center;padding:18px 16px;cursor:pointer;transition:transform .1s}
 .clock-card:active{transform:scale(0.98)}
 .clock{
   font-family:ui-monospace,Menlo,Consolas,monospace;
   font-size:2.6em;font-weight:700;letter-spacing:4px;
   color:#fff;text-shadow:0 0 24px rgba(56,239,125,0.35)
 }
 .clock-date{
   margin-top:4px;font-size:0.85em;letter-spacing:3px;
   text-transform:uppercase;color:rgba(255,255,255,0.55)
 }
 .tz-controls{
   margin-top:12px;font-size:0.78em;letter-spacing:1px;
   color:rgba(255,255,255,0.45);font-family:ui-monospace,Menlo,Consolas,monospace
 }
 .tz-offset{color:#9be0bf;font-weight:700}
 .tap-hint{
   font-size:0.65em;letter-spacing:3px;text-transform:uppercase;
   color:rgba(255,255,255,0.3);margin-top:8px
 }

 .main-card{text-align:center}
 #btn{
   width:100%;padding:28px 16px;font-size:1.4em;font-weight:700;
   border:none;border-radius:18px;cursor:pointer;color:#fff;
   letter-spacing:1.5px;transition:transform .15s;
   box-shadow:0 10px 30px rgba(0,0,0,0.35);
   text-shadow:0 2px 4px rgba(0,0,0,0.3);line-height:1.2
 }
 #btn .state{display:block;font-size:1.4em;margin-top:6px;letter-spacing:3px}
 #btn.on { background:linear-gradient(135deg,#11998e,#38ef7d) }
 #btn.off{ background:linear-gradient(135deg,#1a1a1a 0%,#6a6a6a 50%,#c0c0c0 100%) }
 #btn.unknown{ background:#555 }
 #btn:active{ transform:scale(0.97) }
 .status{margin-top:18px;font-size:0.85em;letter-spacing:2px;color:rgba(255,255,255,0.65)}
 .dot{
   display:inline-block;width:9px;height:9px;border-radius:50%;
   margin-right:8px;vertical-align:middle;background:#3ad26b;
   box-shadow:0 0 8px #3ad26b;animation:pulse 1.6s infinite
 }
 .dot.dead{ background:#888;box-shadow:none;animation:none }
 @keyframes pulse{ 0%,100%{opacity:1} 50%{opacity:.35} }

 .sched-title{
   font-size:0.85em;letter-spacing:3px;text-transform:uppercase;
   color:rgba(255,255,255,0.7);margin-bottom:14px;font-weight:600
 }
 .sched-title.off{color:#e8b4b4}
 .sched-title.on {color:#9be0bf}
 ul.sched{list-style:none;padding:0;margin:0 0 14px 0}
 ul.sched li{
   display:flex;align-items:center;justify-content:space-between;
   padding:8px 12px;margin-bottom:6px;
   background:rgba(255,255,255,0.06);border-radius:10px;gap:8px
 }
 ul.sched li.perm{ background:rgba(255,255,255,0.04); color:rgba(255,255,255,0.7) }
 ul.sched li .t{
   font-family:ui-monospace,Menlo,Consolas,monospace;
   font-size:1.05em;min-width:54px
 }
 ul.sched li .dpat{display:inline-flex;gap:3px;flex:1;justify-content:center}
 ul.sched li .dpat span{
   font-family:ui-monospace,Menlo,Consolas,monospace;
   font-size:0.85em;width:18px;text-align:center;
   border-radius:4px;padding:2px 0
 }
 ul.sched li .dpat .d-on{background:rgba(56,239,125,0.25);color:#dffce9}
 ul.sched li .dpat .d-off{color:rgba(255,255,255,0.25)}
 ul.sched li .lock{opacity:0.7;font-size:0.9em}
 ul.sched li button.del{
   background:#a02a2a;color:#fff;border:0;border-radius:6px;
   width:28px;height:28px;font-size:1em;cursor:pointer
 }
 .add-row{display:flex;flex-direction:column;gap:8px}
 .add-time-row{display:flex;gap:8px;align-items:center}
 .add-time-row input[type=time]{
   flex:1;padding:10px 12px;font-size:1em;
   background:rgba(255,255,255,0.08);color:#fff;
   border:1px solid rgba(255,255,255,0.15);border-radius:10px;
   font-family:ui-monospace,Menlo,Consolas,monospace
 }
 .add-time-row button{
   padding:10px 18px;font-weight:700;
   background:#2c7be5;color:#fff;border:0;border-radius:10px;cursor:pointer
 }
 .dow{display:flex;gap:4px;flex-wrap:wrap;justify-content:space-between}
 .dow label{
   flex:1;min-width:36px;text-align:center;
   padding:8px 0;border-radius:8px;
   background:rgba(255,255,255,0.06);
   font-family:ui-monospace,Menlo,Consolas,monospace;font-size:0.9em;
   cursor:pointer;user-select:none
 }
 .dow label input{display:none}
 .dow label.on{background:#2c7be5;color:#fff;font-weight:700}

 .footer{text-align:center;padding:18px 12px 8px;opacity:0.55;transition:opacity .25s}
 .footer:hover{opacity:1}
 .footer a{color:inherit;text-decoration:none;display:inline-block}
 .footer .brand{font-size:1em;font-weight:700;letter-spacing:3px;text-transform:uppercase}
 .footer .title{font-size:0.75em;letter-spacing:3px;text-transform:uppercase;color:rgba(255,255,255,0.5);margin-top:2px}

 /* Modal */
 .modal-bg{
   display:none;position:fixed;inset:0;
   background:rgba(0,0,0,0.7);backdrop-filter:blur(10px);
   align-items:center;justify-content:center;z-index:100;padding:20px
 }
 .modal-bg.show{display:flex}
 .modal{
   background:linear-gradient(135deg,#1a2a3a,#2c5364);
   border-radius:22px;padding:28px 24px;max-width:360px;width:100%;
   border:1px solid rgba(255,255,255,0.15);
   box-shadow:0 30px 80px rgba(0,0,0,0.7);text-align:center
 }
 .modal h3{font-size:0.95em;letter-spacing:3px;text-transform:uppercase;
   color:rgba(255,255,255,0.75);margin-bottom:18px;font-weight:700}
 .modal .row{
   display:flex;justify-content:space-between;align-items:center;
   padding:8px 0;font-size:0.9em;
   font-family:ui-monospace,Menlo,Consolas,monospace
 }
 .modal .row .label{color:rgba(255,255,255,0.55);letter-spacing:2px;text-transform:uppercase;font-size:0.78em}
 .modal .row .val{font-size:1.1em;color:#fff}
 .modal .offset-big{
   font-size:3em;font-weight:700;color:#9be0bf;
   font-family:ui-monospace,Menlo,Consolas,monospace;
   margin:16px 0 8px;letter-spacing:2px
 }
 .modal .step-row{
   display:flex;gap:16px;justify-content:center;margin:18px 0
 }
 .modal .step-row button{
   width:60px;height:60px;border-radius:50%;border:0;
   background:rgba(255,255,255,0.12);color:#fff;
   font-size:1.6em;font-weight:700;cursor:pointer;
   transition:transform .08s
 }
 .modal .step-row button:active{transform:scale(0.88);background:rgba(255,255,255,0.25)}
 .modal .actions{display:flex;gap:12px;margin-top:22px}
 .modal .actions button{
   flex:1;padding:14px;border-radius:12px;border:0;
   font-weight:700;font-size:1em;cursor:pointer;letter-spacing:1px
 }
 .modal .actions .cancel{background:rgba(255,255,255,0.1);color:#fff}
 .modal .actions .set{background:#2c7be5;color:#fff;box-shadow:0 6px 18px rgba(44,123,229,0.4)}
 .modal .actions button:active{transform:scale(0.97)}
</style></head><body>
<div class="wrap">

  <div class="card clock-card" onclick="openModal()">
    <div class="clock" id="clock">--:--:--</div>
    <div class="clock-date" id="date">— — —</div>
    <div class="tz-controls">
      RTC <span id="rawClock">--:--:--</span> · offset <span class="tz-offset" id="tzOffset">+0h</span>
    </div>
    <div class="tap-hint">tap to adjust</div>
  </div>

  <div class="card main-card">
    <button id="btn" class="unknown" onclick="toggle()">Compressor</button>
    <div class="status"><span id="dot" class="dot"></span><span id="live">connecting...</span></div>
  </div>

  <div class="card">
    <div class="sched-title off">Auto-OFF Schedule</div>
    <ul id="off-list" class="sched"></ul>
    <div class="add-row">
      <div class="dow" id="off-dow"></div>
      <div class="add-time-row">
        <input type="time" id="off-time">
        <button onclick="add('off')">+ Add</button>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="sched-title on">Auto-ON Schedule</div>
    <ul id="on-list" class="sched"></ul>
    <div class="add-row">
      <div class="dow" id="on-dow"></div>
      <div class="add-time-row">
        <input type="time" id="on-time">
        <button onclick="add('on')">+ Add</button>
      </div>
    </div>
  </div>

  <div class="footer">
    <a href="https://astechlab.net/" target="_blank" rel="noopener">
      <div class="brand">ASTECHLAB</div>
      <div class="title">ICS Division</div>
    </a>
  </div>

</div>

<!-- TZ adjust modal -->
<div class="modal-bg" id="tzModal" onclick="if(event.target.id==='tzModal') closeModal()">
  <div class="modal">
    <h3>Timezone Offset</h3>
    <div class="row"><span class="label">RTC time</span><span class="val" id="modalRtc">--:--:--</span></div>
    <div class="offset-big" id="modalOffset">+0h</div>
    <div class="row"><span class="label">Local will be</span><span class="val" id="modalLocal">--:--:--</span></div>
    <div class="step-row">
      <button onclick="modalDelta(-1)">−</button>
      <button onclick="modalDelta(1)">+</button>
    </div>
    <div class="actions">
      <button class="cancel" onclick="closeModal()">Cancel</button>
      <button class="set"    onclick="saveModal()">Set</button>
    </div>
  </div>
</div>

<script>
const DOW = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
const DOW_SHORT = ['M','T','W','T','F','S','S'];
const DEFAULT_DAYS = [true, true, true, true, true, false, false];

function buildDow(id){
  const row = document.getElementById(id);
  row.innerHTML = '';
  for (let i = 0; i < 7; i++){
    const lab = document.createElement('label');
    lab.className = DEFAULT_DAYS[i] ? 'on' : '';
    lab.title = DOW[i];
    lab.innerHTML = '<input type="checkbox" value="'+(1<<i)+'"' + (DEFAULT_DAYS[i] ? ' checked' : '') + '>' + DOW_SHORT[i];
    const cb = lab.querySelector('input');
    cb.addEventListener('change', () => lab.classList.toggle('on', cb.checked));
    row.appendChild(lab);
  }
}
function getDowMask(id){
  let m = 0;
  for (const cb of document.querySelectorAll('#'+id+' input[type=checkbox]')){
    if (cb.checked) m |= parseInt(cb.value);
  }
  return m;
}
buildDow('off-dow');
buildDow('on-dow');

const pad = n => String(n).padStart(2,'0');
function fmtOffset(o){ return (o > 0 ? '+' : (o < 0 ? '' : '+')) + o + 'h'; }

async function update(){
  try{
    const r = await fetch('/status', {cache:'no-store'});
    const s = (await r.text()).trim();
    const b = document.getElementById('btn');
    b.innerHTML = 'Compressor<br><span class="state">'+s+'</span>';
    b.className = s==='ON' ? 'on' : 'off';
    document.getElementById('live').textContent = 'live';
    document.getElementById('dot').className = 'dot';
  }catch(e){
    document.getElementById('live').textContent = 'reconnecting...';
    document.getElementById('dot').className = 'dot dead';
  }
}
async function toggle(){
  try{ await fetch('/toggle', {cache:'no-store'}); update(); }catch(e){}
}

async function updateClock(){
  try{
    const r = await fetch('/time', {cache:'no-store'});
    const d = await r.json();
    document.getElementById('clock').textContent = pad(d.h)+':'+pad(d.m)+':'+pad(d.s);
    document.getElementById('date').textContent =
      DOW[d.wd] + ' · ' + d.day + '.' + pad(d.month) + '.' + d.year;
    document.getElementById('rawClock').textContent = pad(d.raw_h)+':'+pad(d.raw_m)+':'+pad(d.raw_s);
    document.getElementById('tzOffset').textContent = fmtOffset(d.offset);
  }catch(e){}
}

// ---- TZ modal ----
let modalOffset = 0;
let modalRtcH = 0, modalRtcM = 0, modalRtcS = 0;

async function openModal(){
  try{
    const r = await fetch('/time', {cache:'no-store'});
    const d = await r.json();
    modalOffset = d.offset;
    modalRtcH = d.raw_h; modalRtcM = d.raw_m; modalRtcS = d.raw_s;
    updateModalDisplay();
    document.getElementById('tzModal').classList.add('show');
  }catch(e){}
}
function closeModal(){
  document.getElementById('tzModal').classList.remove('show');
}
function modalDelta(delta){
  modalOffset = Math.max(-12, Math.min(14, modalOffset + delta));
  updateModalDisplay();
}
function updateModalDisplay(){
  document.getElementById('modalOffset').textContent = fmtOffset(modalOffset);
  document.getElementById('modalRtc').textContent =
    pad(modalRtcH) + ':' + pad(modalRtcM) + ':' + pad(modalRtcS);
  let localH = (modalRtcH + modalOffset) % 24;
  if (localH < 0) localH += 24;
  document.getElementById('modalLocal').textContent =
    pad(localH) + ':' + pad(modalRtcM) + ':' + pad(modalRtcS);
}
async function saveModal(){
  try{
    await fetch('/tz/set?offset=' + modalOffset, {cache:'no-store'});
    closeModal();
    updateClock();
  }catch(e){}
}

// ---- schedule ----
async function refreshSched(){
  try{
    const r = await fetch('/schedule', {cache:'no-store'});
    const d = await r.json();
    render('off', d.off_permanent, d.off_user);
    render('on',  [],              d.on_user);
  }catch(e){}
}
function daysHtml(mask){
  let s = '<span class="dpat">';
  for (let i = 0; i < 7; i++){
    const on = (mask & (1<<i)) ? 'd-on' : 'd-off';
    s += '<span class="'+on+'" title="'+DOW[i]+'">'+DOW_SHORT[i]+'</span>';
  }
  return s + '</span>';
}
function render(kind, perm, user){
  const ul = document.getElementById(kind+'-list');
  let html = '';
  for (const e of perm){
    html += '<li class="perm"><span class="t">'+e.time+'</span>'+daysHtml(e.days)+'<span class="lock">🔒</span></li>';
  }
  for (const e of user){
    html += '<li><span class="t">'+e.time+'</span>'+daysHtml(e.days)
         + '<button class="del" title="delete" onclick="del(\\''+kind+'\\',\\''+e.time+'\\')">×</button></li>';
  }
  if (!html) html = '<li style="opacity:.4">— empty —</li>';
  ul.innerHTML = html;
}

async function add(kind){
  const t = document.getElementById(kind+'-time').value;
  if (!t) return;
  const days = getDowMask(kind+'-dow');
  if (!days){ alert('Pick at least one day'); return; }
  await fetch('/schedule/add?type='+kind+'&time='+encodeURIComponent(t)+'&days='+days, {cache:'no-store'});
  refreshSched();
}
async function del(kind, t){
  await fetch('/schedule/del?type='+kind+'&time='+encodeURIComponent(t), {cache:'no-store'});
  refreshSched();
}

update(); refreshSched(); updateClock();
setInterval(update, 500);
setInterval(refreshSched, 3000);
setInterval(updateClock, 1000);
</script></body></html>"""


# ---------- relay actions ----------
def play_sound():
    global time_delay
    time_delay = 100
    Speaker.begin()
    Speaker.setVolumePercentage(1)
    Speaker.tone(3800, time_delay)
    time.sleep_ms(time_delay + 50)
    Speaker.tone(3800, time_delay)
    time.sleep_ms(time_delay + 225)
    Speaker.tone(3800, time_delay)
    time.sleep_ms(time_delay + 50)
    Speaker.tone(3800, time_delay)
    time.sleep_ms(time_delay + 50)
    Speaker.end()


def turn_off():
    global relay_status
    if not relay_status:
        log('turn_off: already OFF, ignoring')
        return 'relay already OFF'
    pin41.value(1)
    move_servo_then_release(0)
    play_sound()
    espnow_0.broadcast_data(0)
    rect0.setColor(color=0x666666, fill_c=0x666666)
    Init.setColor(0xffffff, 0x666666)
    Init.setText('     OFF       ')
    relay_status = False
    espnow_0.broadcast_data(0)
    time.sleep_ms(200)
    espnow_0.broadcast_data(0)
    time.sleep_ms(200)
    espnow_0.broadcast_data(0)
    log('OFF: GPIO41 HIGH (SSR OFF), servo 0 deg')
    return 'relay is OFF'


def turn_on():
    global relay_status
    if relay_status:
        log('turn_on: already ON, ignoring')
        return 'relay already ON'
    move_servo_then_release(60)
    play_sound()
    relay_status = True
    rect0.setColor(color=0x009900, fill_c=0x009900)
    Init.setColor(0xffffff, 0x009900)
    Init.setText('      ON       ')
    espnow_0.broadcast_data(1)
    time.sleep_ms(200)
    espnow_0.broadcast_data(1)
    time.sleep_ms(200)
    espnow_0.broadcast_data(1)
    time.sleep_ms(200)
    espnow_0.broadcast_data(1)
    pin41.value(0)
    log('ON: noise settled, GPIO41 LOW (SSR ON)')
    return 'relay is ON'


# ---------- button callbacks ----------
def btnA_wasPressed_event(state):
    log('button A pressed')
    if relay_status: print(turn_off())
    else:            print(turn_on())


def btnB_wasPressed_event(state):
    log('button B pressed')
    if relay_status: print(turn_off())
    else:            print(turn_on())


def btnC_wasPressed_event(state):
    log('button C pressed')
    if relay_status: print(turn_off())
    else:            print(turn_on())


# ---------- ESP-NOW callback ----------
def espnow_recv_callback(espnow_obj):
    global espnow_mac, espnow_data
    espnow_mac, espnow_data = espnow_obj.recv_data()
    log('ESP-NOW <-', espnow_mac, espnow_data)
    cmd = espnow_0._bytes_to(espnow_data, 0)
    if cmd == 1:   print(turn_on())
    elif cmd == 0: print(turn_off())
    elif cmd == 2: espnow_0.broadcast_data(1 if relay_status else 0)


# ---------- HTTP server ----------
def start_http():
    global http_sock
    http_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    http_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    http_sock.bind(('0.0.0.0', 80))
    http_sock.listen(2)
    http_sock.setblocking(False)
    log('HTTP: server listening on port 80')


def handle(path):
    global tz_offset_hours

    if path == '/':
        return HTML.encode(), 'text/html'
    if path == '/status':
        return (b'ON' if relay_status else b'OFF'), 'text/plain'
    if path == '/toggle':
        if relay_status: turn_off()
        else:            turn_on()
        return (b'ON' if relay_status else b'OFF'), 'text/plain'
    if path == '/on':
        turn_on()
        return (b'ON' if relay_status else b'OFF'), 'text/plain'
    if path == '/off':
        turn_off()
        return (b'ON' if relay_status else b'OFF'), 'text/plain'

    if path == '/time':
        raw = rtc.datetime()
        adj = adjusted_datetime()
        body = json.dumps({
            'year': adj[0], 'month': adj[1], 'day': adj[2],
            'wd': adj[3], 'h': adj[4], 'm': adj[5], 's': adj[6],
            'raw_year': raw[0], 'raw_month': raw[1], 'raw_day': raw[2],
            'raw_wd': raw[3], 'raw_h': raw[4], 'raw_m': raw[5], 'raw_s': raw[6],
            'offset': tz_offset_hours,
        })
        return body.encode(), 'application/json'

    if path.startswith('/tz/set?'):
        params = parse_query(path.split('?', 1)[1])
        try:
            new_offset = int(params.get('offset', '0'))
            tz_offset_hours = max(-12, min(14, new_offset))
            save_tz()
            log('TZ offset set to', tz_offset_hours, 'h')
            return b'OK', 'text/plain'
        except Exception as e:
            log('tz/set error:', e)
            return b'BAD', 'text/plain'

    if path == '/schedule':
        body = json.dumps({
            'off_permanent': PERMANENT_OFF_TIMES,
            'off_user':      user_off_times,
            'on_user':       user_on_times,
        })
        return body.encode(), 'application/json'

    if path.startswith('/schedule/add?'):
        params = parse_query(path.split('?', 1)[1])
        try: days = int(params.get('days', '127'))
        except: days = 127
        ok = add_schedule(params.get('type', ''), params.get('time', ''), days)
        log('schedule add:', params, 'ok=', ok)
        return (b'OK' if ok else b'REJECTED'), 'text/plain'

    if path.startswith('/schedule/del?'):
        params = parse_query(path.split('?', 1)[1])
        ok = del_schedule(params.get('type', ''), params.get('time', ''))
        log('schedule del:', params, 'ok=', ok)
        return (b'OK' if ok else b'NOT_FOUND'), 'text/plain'

    if path.startswith('/servo/'):
        angle = max(0, min(180, int(path.rsplit('/', 1)[1])))
        duty = 31 + angle * 92 // 180
        pwm40.duty(duty)
        return ('servo %d deg' % angle).encode(), 'text/plain'

    log('handle: unknown path', path)
    return b'unknown', 'text/plain'


def serve_one():
    try:
        conn, addr = http_sock.accept()
    except OSError:
        return
    try:
        conn.settimeout(0.5)
        req = conn.recv(1024).decode()
        path = req.split(' ', 2)[1]
        if path not in ('/status', '/schedule', '/time'):
            log('HTTP <-', addr[0], path)
        body, ctype = handle(path)
        conn.send(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: ')
        conn.send(ctype.encode())
        conn.send(b'\r\nCache-Control: no-store\r\n\r\n')
        conn.send(body)
    except Exception as e:
        log('HTTP error:', e)
    finally:
        conn.close()


# ---------- main lifecycle ----------
def setup():
    global rect0, Init, espnow_0, rtc, wlan_sta, pwm40, pin41, relay_status

    log('=== StamPLC boot ===')

    M5.begin()
    log('M5 hardware initialized')

    Widgets.fillScreen(0x000000)
    rect0 = Widgets.Rectangle(-8, -4, 259, 142, 0x000000, 0x000000)
    Init = Widgets.Label("label", 0, 39, 1.0, 0x000000, 0x000000, Widgets.FONTS.Montserrat48)

    BtnA.setCallback(type=BtnA.CB_TYPE.WAS_PRESSED, cb=btnA_wasPressed_event)
    BtnB.setCallback(type=BtnB.CB_TYPE.WAS_PRESSED, cb=btnB_wasPressed_event)
    BtnC.setCallback(type=BtnC.CB_TYPE.WAS_PRESSED, cb=btnC_wasPressed_event)
    log('buttons A/B/C registered')

    rtc = RTC()
    log('RTC raw:', rtc.datetime())

    load_tz()
    load_schedules()
    log('Adjusted time:', adjusted_datetime())

    log('WiFi: resetting radio...')
    wlan_sta = network.WLAN(network.STA_IF)
    wlan_sta.active(False)
    time.sleep_ms(500)
    wlan_sta.active(True)
    log('WiFi: connecting to', WIFI_SSID)
    wlan_sta.connect(WIFI_SSID, WIFI_PASS)

    ip = '0.0.0.0'
    for i in range(50):
        if wlan_sta.isconnected():
            break
        time.sleep_ms(200)
    if wlan_sta.isconnected():
        ip = wlan_sta.ifconfig()[0]
        log('WiFi: CONNECTED, IP =', ip)
    else:
        log('WiFi: FAILED to associate (continuing anyway)')

    espnow_0 = M5ESPNow(0)
    espnow_0.set_irq_callback(espnow_recv_callback)
    espnow_0.set_ap_ssid('StamPLCRelay')
    log('ESP-NOW: ready, ssid="StamPLCRelay"')

    pwm40 = PWM(Pin(40), freq=50, duty=0)
    log('PWM: servo init on G40, 50Hz, idle (no pulses)')

    pin41 = Pin(41, Pin.OUT, value=1)
    log('GPIO41: digital output, initial HIGH (SSR OFF)')

    relay_status = True
    print(turn_off())

    start_http()

    log('=== setup complete ===')
    log('open in browser:  http://%s/' % ip)


def loop():
    M5.update()
    serve_one()
    schedule_check()


if __name__ == '__main__':
    try:
        setup()
        while True:
            loop()
    except (Exception, KeyboardInterrupt) as e:
        try:
            from utility import print_error_msg
            print_error_msg(e)
        except ImportError:
            print("please update to latest firmware")
