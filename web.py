#!/usr/bin/env python3
"""Car counter web page (LAN only, no login): http://carcounter.local:8080/

  /                today's count, live health, hourly + 14-day charts, latest photos
  /day/YYYY-MM-DD  one day: hourly chart + every event with its photo
  /days            all days, totals table
  /summary.csv     daily/hourly summary (rebuilt hourly by maintenance.py)
  /settings        live sensor reading, tunable settings, recalibrate
"""

import html
import ipaddress
import socket
import json
import os
import signal
import subprocess
import sys
import time
import mimetypes
import re
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

import alerts
import carcount_data as cd

SRC = Path(__file__).resolve().parent
STARTED = time.time()
SERVICES = {   # restart button: service -> files whose changes need that restart
    'web': ('carcount-web', 'Web page', ['web.py', 'carcount_data.py', 'ups_monitor.py', 'alerts.py']),
    'counter': ('carcount', 'Counter', ['carcounter.py', 'carcount_data.py']),
    'alerts': ('carcount-alerts', 'Notifications', ['alerts.py', 'carcount_data.py']),
}

PORT = 8080
THUMB_SIZE = (480, 270)
STALE_S = 180            # health row older than this -> counter not reporting
_thumb_lock = threading.Lock()

CSS = """
:root{--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;
--axis:#c3c2b7;--bar:#2a78d6;--bar-hi:#1c5cab;--border:rgba(11,11,11,.10);--good:#0ca30c;--warn:#fab219;
--bad:#d03b3b;--link:#256abf}
@media (prefers-color-scheme:dark){:root{--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--bar:#3987e5;--bar-hi:#86b6ef;--border:rgba(255,255,255,.10);--link:#86b6ef}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
a{color:var(--link)}
header{display:flex;flex-wrap:wrap;align-items:center;gap:10px 20px;margin-bottom:16px}
header h1{font-size:20px;margin:0}
nav{display:flex;flex-wrap:wrap;gap:8px}
nav a{display:inline-block;padding:7px 14px;border-radius:8px;border:1px solid var(--axis);
background:var(--surface);color:var(--ink);font-weight:600;text-decoration:none}
nav a:hover{border-color:var(--bar)}
nav a.active{background:var(--bar);border-color:var(--bar);color:#fff}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 10px;color:var(--ink2);font-weight:600}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;margin-bottom:16px}
.row>.card{margin-bottom:0}
.hero{display:flex;gap:32px;flex-wrap:wrap;align-items:flex-end}
.hero .big{font-size:56px;font-weight:700;line-height:1;font-variant-numeric:tabular-nums}
.hero .sub{color:var(--ink2)}
.hero .mid{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.summary .hero{align-items:flex-end}
.summary .status{margin-left:auto;align-self:center;text-align:right}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px 16px}
.stats div{font-variant-numeric:tabular-nums}
.stats span{display:block;color:var(--muted);font-size:12px}
.pill{display:inline-flex;align-items:center;gap:6px;font-weight:600}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block}
svg.chart{width:100%;max-width:720px;height:auto;display:block}
svg .grid{stroke:var(--grid);stroke-width:1}
svg .axis{stroke:var(--axis);stroke-width:1}
svg text{fill:var(--muted);font-size:12px;font-family:inherit}
svg .line{fill:none;stroke:var(--bar);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .area{fill:var(--bar);opacity:.08}
svg .end,svg .pt{fill:var(--bar);stroke:var(--surface);stroke-width:2}
svg .pt,svg .guide{visibility:hidden}
svg .guide{stroke:var(--axis);stroke-width:1}
svg g.col:hover .pt,svg g.col:hover .guide{visibility:visible}
svg .hit{fill:transparent}
.photos{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.photos figure{margin:0}
.photos img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:6px;background:var(--grid);display:block}
.photos figcaption{font-size:13px;color:var(--ink2);margin-top:4px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--grid);vertical-align:middle}
th{color:var(--muted);font-weight:600;font-size:13px}
td img{width:160px;border-radius:4px;display:block}
.muted{color:var(--muted)}
.viewer img{display:block;width:100%;max-height:75vh;object-fit:contain;background:#000;border-radius:8px}
.vbar{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.vtitle{flex:1;text-align:center;display:flex;flex-direction:column;gap:2px}
.navbtn{display:inline-block;padding:10px 16px;border-radius:8px;border:1px solid var(--axis);background:var(--surface);
color:var(--ink);font-weight:600;text-decoration:none;white-space:nowrap}
.navbtn:hover{border-color:var(--bar)}
.navbtn.disabled{opacity:.35}
@media (max-width:600px){.vbar{flex-wrap:wrap}.vtitle{order:-1;flex-basis:100%}.navbtn{flex:1;text-align:center}}
.msg{padding:10px 14px;border-radius:8px;margin-bottom:16px;border:1px solid var(--border);background:var(--surface)}
fieldset{border:0;padding:0;margin:0}
.field{display:grid;grid-template-columns:minmax(180px,260px) 140px 1fr;gap:4px 14px;align-items:center;
padding:10px 0;border-bottom:1px solid var(--grid)}
.field label{font-weight:600}
.field .help{color:var(--ink2);font-size:13px}
.field .err{color:var(--bad);font-size:13px;grid-column:1/-1}
input[type=number],input[type=time],input[type=password],select{width:140px;box-sizing:border-box;padding:6px 8px;border:1px solid var(--axis);border-radius:6px;
background:var(--page);color:var(--ink);font:inherit}
input[type=checkbox]{width:20px;height:20px}
input[type=password]{width:100%;max-width:260px}
button:disabled{opacity:.5;cursor:default}
button{font:inherit;font-weight:600;padding:8px 16px;border-radius:8px;border:1px solid var(--axis);
background:var(--surface);color:var(--ink);cursor:pointer}
button.primary{background:var(--bar);border-color:var(--bar);color:#fff}
.buttons{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.live{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums}
@media (max-width:600px){
.field{grid-template-columns:1fr}
input[type=number],input[type=time],input[type=password],select{font-size:16px}   /* iOS zooms into inputs below 16px */
main{padding:12px}
.card{padding:14px}
.hero{gap:20px}
.hero .big{font-size:48px}
.summary .status{margin-left:0;text-align:left;flex-basis:100%}
.photos{grid-template-columns:repeat(2,1fr);gap:8px}
.photos figcaption{font-size:12px}
table.events tr.head{display:none}
table.events tr{display:grid;grid-template-columns:128px 1fr 1fr;gap:2px 10px;padding:10px 0;
border-bottom:1px solid var(--grid)}
table.events td{border:0;padding:0;font-size:14px}
table.events td.ph{grid-row:1/span 3;grid-column:1}
table.events td.ph img{width:128px}
table.events td[data-label]::before{content:attr(data-label);display:block;color:var(--muted);font-size:11px}
}
"""

# Christmas theme (WEB_THEME setting; Auto = 15 Nov to 6 Jan). Scoped to body.xmas.
XMAS_CSS = """
body.xmas{--page:#fbf6ee;--surface:#fffdf9;--ink:#1d1a17;--ink2:#5a4f45;--muted:#8a7e72;--grid:#ece3d6;
--axis:#d4c6b3;--bar:#c8102e;--bar-hi:#8e0b20;--border:rgba(140,20,30,.14);--good:#1f8a4c;--warn:#e0a100;--link:#13703f}
@media (prefers-color-scheme:dark){body.xmas{--page:#0a1c14;--surface:#11281e;--ink:#f4efe6;--ink2:#cfc5b4;
--muted:#93a597;--grid:#1d3a2c;--axis:#2d5240;--bar:#ef4b5f;--bar-hi:#ff8f9c;--border:rgba(255,215,140,.14);
--good:#3ccf7c;--link:#8fdcae}}
body.xmas::before{content:"";display:block;height:6px;
background:repeating-linear-gradient(-45deg,#c8102e 0 10px,#fff 10px 20px)}
.xmas header h1 .tag{display:block;font-size:13px;font-weight:500;color:var(--muted)}
.xmas .card{box-shadow:0 1px 0 rgba(200,16,46,.08)}
.xmas .summary{position:relative;border-color:rgba(200,16,46,.3);
background:linear-gradient(135deg,rgba(200,16,46,.05),transparent 40%,rgba(19,112,63,.06))}
.xmas .summary h2::after{content:" 🎄"}
.xmas .hero .big{color:var(--bar)}
.lights{position:relative;display:flex;justify-content:space-between;height:24px;margin:-4px 0 12px;overflow:hidden}
.lights::before{content:"";position:absolute;left:0;right:0;top:3px;height:8px;border-top:2px solid #2f6b48;
border-radius:50%}
.lights i{flex:none;width:9px;height:14px;margin-top:8px;border-radius:50%/60% 60% 40% 40%;background:currentColor;
box-shadow:0 0 8px currentColor;animation:twinkle 2.4s ease-in-out infinite;color:#ff3b3b}
.lights i:nth-child(4n+2){color:#ffc93c;animation-delay:.6s}
.lights i:nth-child(4n+3){color:#33d17a;animation-delay:1.2s}
.lights i:nth-child(4n){color:#4ea8ff;animation-delay:1.8s}
@keyframes twinkle{0%,100%{opacity:1}50%{opacity:.3}}
@media (max-width:600px){.lights i:nth-child(n+19){display:none}}
.snow{position:fixed;inset:0;pointer-events:none;overflow:hidden;z-index:5}
.snow span{position:absolute;top:-2em;color:#a9c9e8;opacity:.75;animation:fall linear infinite}
@media (prefers-color-scheme:dark){.snow span{color:#fff;opacity:.6}}
@keyframes fall{to{transform:translateY(110vh) translateX(3vw) rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.snow{display:none}.lights i{animation:none}}
header h1{display:flex;align-items:center;gap:10px}
header h1 .wagon{width:64px;flex:none}
.wagon svg,.lane .drive svg{display:block;width:100%;height:auto}
.scene{margin:-4px 0 16px}
.scene .lights{margin:0}
.lane{position:relative;height:58px;overflow:hidden}
.lane::before{content:"";position:absolute;left:0;right:0;bottom:0;height:26px;border-radius:6px;
background:#3a3c40;border-top:3px solid #f4f1ea;box-shadow:inset 0 -2px 0 #2a2b2e}
.lane::after{content:"";position:absolute;left:8px;right:8px;bottom:11px;height:3px;
background:repeating-linear-gradient(90deg,#f2c94c 0 18px,transparent 18px 34px)}
.lane .drive{position:absolute;bottom:3px;left:-100px;width:90px;z-index:2;visibility:hidden}
.lane .drive.go{visibility:visible;animation:drive 6s linear forwards}
.lane .drive.go svg{display:block;animation:bounce .3s ease-in-out infinite alternate}
@keyframes drive{from{left:-100px}to{left:calc(100% + 10px)}}
@keyframes bounce{to{transform:translateY(-1.5px)}}
@media (prefers-reduced-motion:reduce){.lane .drive.go{animation:none;left:calc(50% - 45px)}.lane .drive.go svg{animation:none}}
.cheer{display:inline-block;margin-top:6px;font-weight:600;color:var(--link)}
"""
WAGON_SVG = (Path(__file__).resolve().parent / 'static' / 'wagon.svg').read_text().strip()
WAGON_ICON = 'data:image/svg+xml,' + quote(WAGON_SVG)
NAV_EMOJI = {'/': '🎄', '/days': '📅', '/settings': '⚙️', '/power': '🔋', '/alerts': '🔔', '/summary.csv': '📜'}
# (left %, fall seconds, start delay s, size px, flake)
SNOW = [(4, 14, 0, 14, '❄'), (11, 19, 6, 10, '❅'), (19, 16, 11, 16, '❆'), (27, 22, 3, 11, '❄'),
        (35, 17, 9, 13, '❅'), (44, 20, 1, 18, '❄'), (52, 15, 13, 10, '❆'), (60, 23, 5, 14, '❅'),
        (68, 18, 8, 12, '❄'), (76, 21, 2, 16, '❆'), (84, 16, 12, 11, '❅'), (92, 19, 7, 15, '❄')]
LIGHTS = '<div class="lights" aria-hidden="true">' + '<i></i>' * 32 + '</div>'
# Today page: lights + a road the wagon drives along once for every new car
SNOWFALL = '<div class="snow" aria-hidden="true">' + ''.join(
    f'<span style="left:{x}%;animation-duration:{t}s;animation-delay:-{d}s;font-size:{z}px">{f}</span>'
    for x, t, d, z, f in SNOW) + '</div>'


def xmas():
    return cd.festive(cd.load_settings()['WEB_THEME'])


def xmas_countdown(today=None):
    """(number, label) for the summary card, or None outside December."""
    today = today or date.today()
    if today.month != 12 or today.day > 26:
        return None
    if today.day == 25:
        return '🎄', 'Merry Christmas!'
    if today.day == 26:
        return '🎁', 'Happy Boxing Day'
    n = (date(today.year, 12, 25) - today).days
    return n, f'sleep{"s" * (n != 1)} till Christmas 🎅'


# ---------------------------------------------------------------- helpers
def esc(x):
    return html.escape(str(x))


def photo_url(path, kind='file'):
    """Absolute photo path from the CSV -> /file/usb/... URL (or None if it no longer exists)."""
    if not path:
        return None
    for key, root in cd.ROOTS.items():
        p = Path(path)
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        return f'/{kind}/{key}/{rel.as_posix()}' if p.exists() else None
    return None


def line_chart(values, labels, tips, label_every=1, height=170, upto=None, ymax=None):
    """Single-series line chart as inline SVG. Hovering a column shows a guide line,
    a marker and a tooltip (<title>). `upto` = number of points to draw (e.g. hours so far
    today); None values are gaps (no data); `ymax` fixes the top of the scale."""
    n = len(values)
    upto = n if upto is None else max(1, min(upto, n))
    W, H, left, right, top, bottom = 480, height, 28, 10, 10, 22
    pw, ph = W - left - right, H - top - bottom
    if ymax is None:
        vmax = max([v for v in values if v is not None] + [1])
        step = max(1, -(-int(vmax) // 4))   # ~4 gridlines, integer steps
        ymax = step * -(-int(vmax) // step)
    else:
        step = max(1, ymax // 4)
    col = pw / n
    xs = [left + col * (i + 0.5) for i in range(n)]
    ys = [None if v is None else top + ph - ph * min(v, ymax) / ymax for v in values]
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img">']
    for v in range(0, ymax + 1, step):
        y = top + ph - ph * v / ymax
        out.append(f'<line class="grid" x1="{left}" x2="{W - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{v}</text>')
    out.append(f'<line class="axis" x1="{left}" x2="{W - right}" y1="{top + ph}" y2="{top + ph}"/>')
    for i in range(0, n, label_every):
        out.append(f'<text x="{xs[i]:.1f}" y="{H - 6}" text-anchor="middle">{esc(labels[i])}</text>')
    # one polyline per run of consecutive values (None = gap)
    runs, cur = [], []
    for i in range(upto):
        if ys[i] is None:
            if cur:
                runs.append(cur)
            cur = []
        else:
            cur.append(i)
    if cur:
        runs.append(cur)
    for run in runs:
        pts = ' '.join(f'{xs[i]:.1f},{ys[i]:.1f}' for i in run)
        out.append(f'<polygon class="area" points="{xs[run[0]]:.1f},{top + ph} {pts} {xs[run[-1]]:.1f},{top + ph}"/>')
        out.append(f'<polyline class="line" points="{pts}"/>' if len(run) > 1 else
                   f'<circle class="end" cx="{xs[run[0]]:.1f}" cy="{ys[run[0]]:.1f}" r="3"/>')
    if runs:   # latest point always marked; others appear on hover
        i = runs[-1][-1]
        out.append(f'<circle class="end" cx="{xs[i]:.1f}" cy="{ys[i]:.1f}" r="4"/>')
    for i in range(upto):
        if ys[i] is None:
            continue
        out.append(f'<g class="col"><title>{esc(tips[i])}</title>'
                   f'<line class="guide" x1="{xs[i]:.1f}" x2="{xs[i]:.1f}" y1="{top}" y2="{top + ph}"/>'
                   f'<circle class="pt" cx="{xs[i]:.1f}" cy="{ys[i]:.1f}" r="4"/>'
                   f'<rect class="hit" x="{left + i * col:.1f}" y="{top}" width="{col:.1f}" height="{ph + bottom}"/></g>')
    out.append('</svg>')
    return ''.join(out)


def hourly_chart(rows, day):
    h = cd.hourly(rows)
    upto = datetime.now().hour + 1 if day == cd.today() else None
    return line_chart(h, [f'{i:02d}' for i in range(24)],
                     [f'{day} {i:02d}:00–{i:02d}:59 · {n} car{"s" * (n != 1)}' for i, n in enumerate(h)],
                     label_every=3, upto=upto)


def status_badge(kind, text):
    color = {'good': 'var(--good)', 'warn': 'var(--warn)', 'bad': 'var(--bad)'}[kind]
    icon = {'good': '✓', 'warn': '!', 'bad': '✕'}[kind]
    return f'<span class="pill"><span class="dot" style="background:{color}"></span>{icon} {esc(text)}</span>'


NAV = [('/', 'Today'), ('/days', 'All Days'), ('/settings', 'Settings'), ('/power', 'Power'),
       ('/alerts', 'Alerts'), ('/summary.csv', 'Download Summary')]


def events_version(day):
    """Changes whenever a car is logged for `day` (size + mtime of its events.csv files)."""
    parts = []
    for root in cd.read_roots():
        try:
            st = (root / day / 'events.csv').stat()
            parts.append(f'{st.st_size}-{st.st_mtime_ns}')
        except OSError:
            pass
    return '|'.join(parts)


# reload the page as soon as a new car is logged (checked every 3 s while the page is visible)
WATCH_JS = """<script>
(() => {
  const url = '/api/version?day=%s';
  let v = %s;
  async function check() {
    if (document.hidden) return;
    try {
      const d = await (await fetch(url, {cache: 'no-store'})).json();
      if (d.v !== v) location.reload();
    } catch (e) {}
  }
  setInterval(check, 3000);
  document.addEventListener('visibilitychange', check);
})();
</script>"""


DRIVE_JS = """<script>
// The wagon drives across once on the first visit, then once for every car counted since the last view.
(function () {
  const n = %d; let prev = null;
  try { prev = sessionStorage.getItem('cars'); sessionStorage.setItem('cars', n); } catch (e) {}
  const k = prev === null ? 1 : Math.min(n - (+prev), 5);
  const el = document.getElementById('drive');
  if (el && k > 0) { el.style.animationIterationCount = k; el.classList.add('go'); }
})();
</script>"""


def page(title, body, refresh=None, active=None, watch=None, snow=True, drive=None):
    if watch:
        body += WATCH_JS % (watch, json.dumps(events_version(watch)))
    festive = xmas()
    nav = ''.join(f'<a href="{u}"{' class="active" aria-current="page"' if u == active else ''}>'
                  f'{NAV_EMOJI[u] + " " if festive else ""}{t}</a>' for u, t in NAV)
    h1 = (f'<span class="wagon">{WAGON_SVG}</span><span>Car Counter'
          '<span class="tag">🛷 Sleigh spotting on our street</span></span>') if festive else 'Car Counter'
    css = CSS + XMAS_CSS if festive else CSS
    extra = ((LIGHTS if drive is None else '') + (SNOWFALL if snow else '')) if festive else ''
    if festive and drive is not None:
        body += DRIVE_JS % drive
    icon = f'<link rel="icon" href="{WAGON_ICON}">' if festive else ''
    body_cls = ' class="xmas"' if festive else ''
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ''
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#f9f9f7" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0d0d0d" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Car Counter">{icon}{meta}<title>{esc(title)}</title>
<style>{css}</style></head><body{body_cls}><main>
<header><h1>{h1}</h1><nav>{nav}</nav></header>
{extra}{body}</main></body></html>"""


# ---------------------------------------------------------------- pages
THROTTLE_BITS = {0: 'under-voltage', 1: 'CPU speed capped', 2: 'throttled', 3: 'temperature limit'}


def throttle_flags(value):
    """vcgencmd get_throttled -> (problems now, problems earlier since boot) as text lists."""
    try:
        v = int(value or '0', 16)
    except ValueError:
        return [], []
    now = [t for b, t in THROTTLE_BITS.items() if v & (1 << b)]
    past = [t for b, t in THROTTLE_BITS.items() if v & (1 << (b + 16)) and t not in now]
    return now, past


def health_status():
    """(kind, text, latest health row or None, age in s) - the one-line status."""
    rows = cd.health(cd.today())
    if not rows:
        return 'bad', 'No health data today - is carcount.service running?', None, None
    h = rows[-1]
    age = (datetime.now() - datetime.fromisoformat(h['timestamp'])).total_seconds()
    if age > STALE_S:
        return 'bad', f'Counter not reporting (last update {int(age // 60)} min ago)', h, age
    if float(h.get('frames_per_s') or 0) < 50:
        return 'bad', 'Lidar not sending data', h, age
    if h.get('camera') != 'ok':
        return 'warn', 'Counting, but camera unavailable', h, age
    if float(h.get('cpu_temp_c') or 0) >= cd.load_settings()['ALERT_CPU_TEMP']:
        return 'warn', f'Running - CPU hot, {h["cpu_temp_c"]} °C', h, age
    now_f = throttle_flags(h.get('throttled'))[0]
    if now_f:
        return 'warn', 'Running - ' + ', '.join(now_f) + ' now', h, age
    return 'good', 'Running', h, age


def paused_pill(age):
    cfg = cd.load_settings()
    if age is not None and age <= STALE_S and not cd.in_window(cfg['COUNT_START'], cfg['COUNT_STOP']):
        return f' <span class="pill">⏸ Counting paused · counting hours {cfg["COUNT_START"]}-{cfg["COUNT_STOP"]}</span>'
    return ''


def health_card():
    kind, text, h, age = health_status()
    if h is None:
        return f'<div class="card" id="status"><h2>Status</h2>{status_badge(kind, text)}</div>'
    badge = status_badge(kind, text) + paused_pill(age)
    now_f, past_f = throttle_flags(h.get('throttled'))
    if past_f and not now_f:
        badge += (f'<br><span class="muted" style="font-size:13px">Earlier since boot: {", ".join(past_f)} '
                  '(cleared - often a brief dip at power-on)</span>')
    power = '—'
    if h.get('ac_power') != '':
        power = ('Mains' if h['ac_power'] == '1' else 'BATTERY') + \
                (f' · {float(h["battery_v"]):.2f} V {float(h["battery_pct"]):.0f}%' if h.get('battery_v') else '')

    def stat(label, val):
        return f'<div><span>{label}</span>{esc(val) if val not in ("", None) else "—"}</div>'
    stats = ''.join([
        stat('Lidar', f'{h["frames_per_s"]} fps · {h["valid_pct"]}% valid'),
        stat('Beam baseline', f'{h["baseline_cm"]} cm' if h['baseline_cm'] else 'no target'),
        stat('CPU temp', f'{h["cpu_temp_c"]} °C'),
        stat('Fan', f'{h["fan_rpm"]} rpm' if h['fan_rpm'] not in ('', '0') else 'no speed sensor'),
        stat('Lidar temp', f'{h["lidar_temp_c"]} °C' if h['lidar_temp_c'] else ''),
        stat('Storage', f'{h["storage"].upper()} · {h["disk_free_gb"]} GB free'),
        stat('Camera', h['camera']),
        stat('Power', power),
        stat('Sensor errors (last min)', h['sensor_errors']),
    ])
    return f'<div class="card" id="status"><h2>Status <span class="muted">· {esc(h["timestamp"][11:16])}</span></h2>' \
           f'<p>{badge}</p><div class="stats">{stats}</div></div>'


def view_url(e):
    return f'/view/{e["timestamp"][:10]}/{Path(e["photo"]).name}'


def day_photos(day):
    """Events of a day that still have a photo on disk, oldest first."""
    return [e for e in cd.events(day) if photo_url(e.get('photo'))]


def neighbour(day, step):
    """First/last photo event on the nearest earlier (step=-1) or later (+1) day that has photos."""
    days = cd.all_days()
    others = [d for d in days if (d < day if step < 0 else d > day)]
    for d in (reversed(others) if step < 0 else others):
        ph = day_photos(d)
        if ph:
            return ph[-1] if step < 0 else ph[0]
    return None


def view_page(day, name):
    photos = day_photos(day)
    idx = next((i for i, e in enumerate(photos) if Path(e['photo']).name == name), None)
    if idx is None:
        return None
    e = photos[idx]
    prev = photos[idx - 1] if idx > 0 else neighbour(day, -1)
    nxt = photos[idx + 1] if idx + 1 < len(photos) else neighbour(day, +1)

    def nav(ev, label, key):
        if not ev:
            return f'<span class="navbtn disabled" aria-disabled="true">{label}</span>'
        return f'<a class="navbtn" id="{key}" href="{view_url(ev)}" title="{esc(ev["timestamp"][:19].replace("T", " "))}">{label}</a>'
    dt = datetime.fromisoformat(e['timestamp'])
    details = ' · '.join(x for x in [
        f'Car #{e["count_today"]}', f'{e["distance_cm"]} cm' if e.get('distance_cm') else '',
        f'blocked {e["duration_s"]} s' if e.get('duration_s') else '',
        f'photo {idx + 1} of {len(photos)} this day'] if x)
    preload = ''.join(f'<link rel="prefetch" href="{photo_url(ev["photo"])}">' for ev in (prev, nxt) if ev)
    body = f"""{preload}
<div class="viewer">
<div class="vbar">{nav(prev, '◀ Earlier', 'prev')}
<div class="vtitle"><strong>{dt:%a %d %b %Y · %H:%M:%S}</strong><span class="muted">{esc(details)}</span></div>
{nav(nxt, 'Later ▶', 'next')}</div>
<a href="{photo_url(e['photo'])}" title="Open full-size image"><img id="photo" src="{photo_url(e['photo'])}" alt="Car at {dt:%H:%M:%S}"></a>
<p class="muted"><a href="/day/{day}">← Back to {dt:%a %d %b}</a> · Arrow keys or swipe to move · Esc for the day page</p>
</div>
<script>
document.addEventListener('keydown', ev => {{
  if (ev.key === 'ArrowLeft' && document.getElementById('prev')) location.href = document.getElementById('prev').href;
  if (ev.key === 'ArrowRight' && document.getElementById('next')) location.href = document.getElementById('next').href;
  if (ev.key === 'Escape') location.href = '/day/{day}';
}});
let x0 = null;
const img = document.getElementById('photo');
img.addEventListener('touchstart', ev => {{ x0 = ev.touches[0].clientX; }}, {{passive: true}});
img.addEventListener('touchend', ev => {{
  if (x0 === null) return;
  const dx = ev.changedTouches[0].clientX - x0; x0 = null;
  const a = document.getElementById(dx > 0 ? 'prev' : 'next');
  if (Math.abs(dx) > 50 && a) {{ ev.preventDefault(); location.href = a.href; }}
}});
</script>"""
    return page(f'Car {dt:%Y-%m-%d %H:%M:%S}', body, snow=False)


def photos_grid(evs, limit=None, show_date=False):
    items = []
    today = cd.today()
    for e in reversed(evs):
        t = photo_url(e.get('photo'), 'thumb')
        if not t:
            continue
        full = view_url(e)
        items.append(f'<figure><a href="{full}"><img loading="lazy" src="{t}" alt="Car at {esc(e["timestamp"][11:19])}"></a>'
                     f'<figcaption>{esc(photo_day_label(e, today) if show_date else "")}'
                     f'{esc(e["timestamp"][11:19])} · #{esc(e["count_today"])}</figcaption></figure>')
        if limit and len(items) >= limit:
            break
    return f'<div class="photos">{"".join(items)}</div>' if items else '<p class="muted">No photos yet.</p>'


PHOTOS_PER_PAGE = 12


def photo_day_label(e, today):
    d = e['timestamp'][:10]
    return '' if d == today else f'{date.fromisoformat(d):%a %d %b} '


def photo_page(p):
    """Events with photos for page p (1 = newest), newest first, across days; and whether older ones exist."""
    need = p * PHOTOS_PER_PAGE + 1
    found = []
    for d in reversed(cd.all_days()):
        found.extend(e for e in reversed(cd.events(d)) if photo_url(e.get('photo'), 'thumb'))
        if len(found) >= need:
            break
    start = (p - 1) * PHOTOS_PER_PAGE
    return found[start:start + PHOTOS_PER_PAGE], len(found) > start + PHOTOS_PER_PAGE


def photos_section(p):
    evs, older = photo_page(p)
    def btn(label, target, ok):
        return (f'<a class="navbtn" href="/?p={target}#photos">{label}</a>' if ok
                else f'<span class="navbtn disabled" aria-disabled="true">{label}</span>')
    if not evs:
        span = 'No photos' + (' on this page' if p > 1 else ' yet')
    else:
        span = f'{evs[-1]["timestamp"][:16].replace("T", " ")} – {evs[0]["timestamp"][11:16]}' \
            if evs[0]['timestamp'][:10] == evs[-1]['timestamp'][:10] else \
            f'{evs[-1]["timestamp"][:16].replace("T", " ")} – {evs[0]["timestamp"][:16].replace("T", " ")}'
    bar = (f'<div class="vbar">{btn("◀ Newer", p - 1, p > 1)}'
           f'<div class="vtitle"><strong>{"Latest" if p == 1 else f"Page {p}"}</strong>'
           f'<span class="muted">{esc(span)}</span></div>{btn("Older ▶", p + 1, older)}</div>')
    return (f'<div class="card" id="photos"><h2>{"🎁 " if xmas() else ""}Photos</h2>{bar}'
            f'{photos_grid(list(reversed(evs)), show_date=True)}'
            f'{bar if len(evs) > 4 else ""}</div>')


def dashboard(p=1):
    today = date.today()
    t = today.isoformat()
    evs = cd.events(t)
    yday = len(cd.events((today - timedelta(days=1)).isoformat()))
    last = evs[-1]['timestamp'][11:19] if evs else '—'
    days = [(today - timedelta(days=i)) for i in range(13, -1, -1)]
    totals = [len(cd.events(d.isoformat())) for d in days]
    daily = line_chart(totals, [d.strftime('%d') for d in days],
                      [f'{d:%a %d %b} · {n} car{"s" * (n != 1)}' for d, n in zip(days, totals)], label_every=2)
    hour = sum(1 for e in evs if e['timestamp'][11:13] == f'{datetime.now():%H}')
    festive = xmas()
    kind, text, _, age = health_status()
    if festive and kind == 'good':
        text += ' · Rudolph is on watch 🦌'
    status = status_badge(kind, text) + paused_pill(age)
    extra, cheer = '', ''
    if festive:
        cd_ = xmas_countdown()
        if cd_:
            extra = f'<div><div class="mid">{cd_[0]}</div><div class="sub">{cd_[1]}</div></div>'
        if len(evs) > yday > 0:
            cheer = '<div class="cheer">🎉 Busier than yesterday!</div>'
    em = (lambda e: e + ' ') if festive else (lambda e: '')
    scene = (f'<div class="scene" aria-hidden="true">{LIGHTS}<div class="lane">'
             f'<span class="drive" id="drive">{WAGON_SVG}</span></div></div>') if festive else ''
    if kind != 'good':
        status += ' <a href="/settings#status">Details →</a>'
    body = f"""
<div class="card summary"><h2>Today · {today:%a %d %b %Y}</h2><div class="hero">
<div><div class="big">{len(evs)}</div><div class="sub">{em('🚗')}cars today</div>{cheer}</div>
<div><div class="mid">{hour}</div><div class="sub">{em('⏰')}this hour</div></div>
<div><div class="mid">{yday}</div><div class="sub">{em('📆')}yesterday</div></div>
<div><div class="mid">{last}</div><div class="sub">{em('🛷')}last car</div></div>{extra}
<div class="status">{status}</div></div></div>
{scene}<div class="row"><div class="card"><h2>{em('⭐')}Cars per hour · today</h2>{hourly_chart(evs, t)}<p><a href="/day/{t}">Today's events →</a></p></div>
<div class="card"><h2>{em('❄️')}Cars per day · last 14 days</h2>{daily}<p><a href="/days">All days →</a></p></div></div>
{photos_section(p)}"""
    if p > 1:   # browsing older photos: don't auto-refresh back to the newest
        return page('Car Counter', body, active='/')
    return page('Car Counter', body, refresh=60, active='/', watch=t, drive=len(evs))


def day_page(d):
    evs = cd.events(d)
    dt = date.fromisoformat(d)
    prev, nxt = dt - timedelta(days=1), dt + timedelta(days=1)
    rows = []
    for e in reversed(evs):
        t = photo_url(e.get('photo'), 'thumb')
        img = f'<a href="{view_url(e)}"><img loading="lazy" src="{t}" alt=""></a>' if t else '<span class="muted">—</span>'
        rows.append(f'<tr><td data-label="#">{esc(e["count_today"])}</td><td data-label="Time">{esc(e["timestamp"][11:23])}</td>'
                    f'<td data-label="Distance">{esc(e["distance_cm"])} cm</td><td data-label="Strength">{esc(e["strength"])}</td>'
                    f'<td data-label="Blocked for">{esc(e["duration_s"])} s</td><td class="ph">{img}</td></tr>')
    table = ('<table class="events"><tr class="head"><th>#</th><th>Time</th><th>Distance</th><th>Strength</th><th>Blocked for</th><th>Photo</th></tr>'
             + ''.join(rows) + '</table>') if rows else '<p class="muted">No cars logged.</p>'
    body = f"""<p><a href="/day/{prev}">← {prev:%a %d %b}</a> &nbsp; <a href="/day/{nxt}">{nxt:%a %d %b} →</a></p>
<div class="card"><h2>{dt:%A %d %B %Y} · {len(evs)} cars</h2>{hourly_chart(evs, d)}</div>
<div class="card"><h2>Events</h2>{table}</div>"""
    return page(f'Cars {d}', body, watch=d if d == cd.today() else None)


def days_page():
    rows = []
    for d in reversed(cd.all_days()):
        evs = cd.events(d)
        h = cd.hourly(evs)
        peak = max(range(24), key=lambda i: h[i]) if evs else None
        rows.append(f'<tr><td><a href="/day/{d}">{date.fromisoformat(d):%a %d %b %Y}</a></td><td>{len(evs)}</td>'
                    f'<td>{f"{peak:02d}:00 ({h[peak]})" if peak is not None else "—"}</td></tr>')
    body = ('<div class="card"><h2>All days</h2><table><tr><th>Date</th><th>Cars</th><th>Busiest hour</th></tr>'
            + ''.join(rows) + '</table></div>')
    return page('All days', body, active='/days')


def signal_counter(sig, unit='carcount'):
    """Send a signal to a service's main process (default the counter). Returns error text or None."""
    try:
        pid = int(subprocess.run(['systemctl', 'show', '-p', 'MainPID', '--value', unit],
                                 capture_output=True, text=True, timeout=5).stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return f'could not find the counter process ({e})'
    if not pid:
        return 'the counter is not running - changes apply when it starts'
    try:
        os.kill(pid, sig)
    except OSError as e:
        return f'could not signal the counter ({e})'
    return None


def fmt(v):
    return f'{v:g}' if isinstance(v, float) else str(v)


APPLY_NOTE = {'live': 'applies immediately', 'restart': 'counter restarts (~5 s)',
              'next run': 'applies at next hourly maintenance', 'ups': 'applies within a few seconds',
              'alerts': 'applies within 30 s', 'web': 'applies on the next page load'}
POWER_KEYS = [k for k, st in cd.SETTING.items() if st['group'] == 'Power']
ALERT_KEYS = [k for k, st in cd.SETTING.items() if st['group'] == 'Notifications']
COUNTER_KEYS = [k for k in cd.SETTING if k not in POWER_KEYS and k not in ALERT_KEYS]


def settings_cards(keys, values, errors):
    groups = {}
    for key in keys:
        st, v = cd.SETTING[key], values[key]
        if st['type'] == 'bool':
            inp = f'<input type="checkbox" name="{key}" id="{key}" value="1"{" checked" if v else ""}>'
        elif st['type'] == 'choice':
            opts = ''.join(f'<option{" selected" if o == v else ""}>{o}</option>' for o in cd.CHOICES[key])
            inp = f'<select name="{key}" id="{key}">{opts}</select>'
        elif st['type'] == 'time':
            inp = f'<input type="time" name="{key}" id="{key}" value="{esc(v)}" required>'
        else:
            step = '1' if st['type'] == 'int' else 'any'
            inp = (f'<input type="number" name="{key}" id="{key}" value="{esc(fmt(v))}" '
                   f'min="{st["min"]}" max="{st["max"]}" step="{step}" required>')
        default = 'on' if st['default'] is True else 'off' if st['default'] is False else fmt(st['default'])
        help_ = f'{esc(st["help"])} ' if st['help'] else ''
        err = f'<div class="err">✕ {esc(errors[key])}</div>' if key in errors else ''
        groups.setdefault(st['group'], []).append(
            f'<div class="field"><label for="{key}">{esc(st["label"])}</label>{inp}'
            f'<div class="help">{help_}<span class="muted">Default {esc(default)} · {APPLY_NOTE[st["apply"]]}</span></div>{err}</div>')
    return ''.join(f'<div class="card"><h2>{esc(g)}</h2><fieldset>{"".join(f)}</fieldset></div>'
                   for g, f in groups.items())


def banner(msg, errors):
    if errors:
        return f'<div class="msg">{status_badge("bad", "Not saved - fix the highlighted values")}</div>'
    return f'<div class="msg">{esc(msg)}</div>' if msg else ''


def settings_form(action, keys, values, errors, title):
    return f"""<form method="post" action="{action}">{settings_cards(keys, values, errors)}
<div class="buttons"><button class="primary" type="submit">Save &amp; apply</button>
<button type="submit" formaction="{action}/reset" onclick="return confirm('Reset {title} to defaults?')">Reset to defaults</button></div>
</form>"""


def settings_page(values=None, errors=None, msg=None):
    values = values or cd.load_settings()
    errors = errors or {}
    body = f"""{banner(msg, errors)}
{health_card()}
<div class="card"><h2>Live beam reading</h2>
<div class="hero"><div><div class="live" id="l-dist">—</div><div class="sub">distance</div></div>
<div><div class="live" id="l-amp">—</div><div class="sub">strength</div></div>
<div><div class="live" id="l-thr">—</div><div class="sub" id="l-thr-label">counts below</div></div>
<div><div class="live" id="l-state">—</div><div class="sub">state</div></div></div>
<p class="muted" id="l-base"></p>
<form method="post" action="/recalibrate" class="buttons" style="margin-top:4px">
<button type="submit">Recalibrate baseline</button>
<span class="muted" style="align-self:center">Keep the beam clear for the calibration time.</span></form></div>
{settings_form('/settings', COUNTER_KEYS, values, errors, 'counter settings')}
<p class="muted">UPS / battery settings are on the <a href="/power">Power</a> page.</p>
{system_card()}
<script>
async function poll() {{
  try {{
    const r = await fetch('/api/live', {{cache: 'no-store'}});
    const d = await r.json();
    const $ = id => document.getElementById(id);
    if (!d.time) {{ $('l-state').textContent = 'offline'; $('l-base').textContent = 'Counter not running or no sensor data.'; return; }}
    const lost = d.amp < d.min_amp;
    $('l-dist').textContent = lost ? 'lost' : d.dist + ' cm';
    $('l-amp').textContent = d.amp;
    $('l-thr').textContent = d.threshold + ' cm';
    $('l-thr-label').textContent = d.baseline === null ? 'counts below (no target)' : 'counts below';
    $('l-state').textContent = d.state === 'calibrating' ? 'calibrating' : d.blocked ? '■ blocked' : '○ clear';
    const sched = d.counting === false ? ' (counting hours ' + d.count_hours + ')'
      : d.photos === false && d.photo_hours !== 'all day' ? ' · photos off (photo hours ' + d.photo_hours + ')' : '';
    $('l-base').textContent = (d.baseline === null ? 'No far-side target: ' : 'Baseline ' + d.baseline + ' cm · ')
      + d.count + ' counted today · ' + (d.state === 'in event' ? 'car passing' : d.state === 'paused' ? 'counting paused' : d.state) + sched;
  }} catch (e) {{}}
}}
poll(); setInterval(poll, 500);
</script>"""
    return page('Settings', body, active='/settings')


def service_started(unit):
    """Start time (epoch) of a service's main process, or None."""
    try:
        pid = subprocess.run(['systemctl', 'show', '-p', 'MainPID', '--value', unit],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if not pid or pid == '0':
            return None
        if pid == str(os.getpid()):
            return STARTED
        et = subprocess.run(['ps', '-o', 'etimes=', '-p', pid], capture_output=True, text=True, timeout=5).stdout
        return time.time() - int(et.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def check_code(files):
    """Import the changed modules in a separate Python, so errors are caught before restarting."""
    mods = ', '.join(Path(f).stem for f in files)
    try:
        r = subprocess.run([sys.executable, '-c', f'import {mods}'], cwd=SRC, capture_output=True,
                           text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return 'checking the code timed out'
    if r.returncode:
        return (r.stderr.strip().splitlines() or ['unknown error'])[-1] + \
            ('\n' + '\n'.join(r.stderr.strip().splitlines()[-4:-1]) if r.stderr.count('\n') > 1 else '')
    return None


def system_card():
    rows = []
    for key, (unit, label, files) in SERVICES.items():
        started = service_started(unit)
        if started is None:
            state = status_badge('bad', 'not running')
        else:
            changed = [f for f in files if (SRC / f).stat().st_mtime > started]
            state = (status_badge('warn', 'changed since start: ' + ', '.join(changed)) if changed
                     else status_badge('good', f'running since {datetime.fromtimestamp(started):%d %b %H:%M}'))
        rows.append(f'<div class="field"><label>{label}</label>'
                    f'<form method="post" action="/restart/{key}"><button type="submit">Restart</button></form>'
                    f'<div class="help">{state}</div></div>')
    return ('<div class="card"><h2>System</h2>' + ''.join(rows) +
            '<p class="muted">Restart after editing the code. The code is checked first; if it has an error, '
            'nothing is restarted and the error is shown here. Restarting the counter pauses counting for '
            'about 10 s while it recalibrates.</p></div>')


def restart_page(label):
    body = f"""<div class="card"><h2>Restarting</h2><p>{status_badge('warn', f'{label} is restarting…')}</p>
<p class="muted" id="wait">This page returns to Settings when it is back (about 5–10 s).</p></div>
<script>
const t0 = Date.now();
setTimeout(async function poll() {{
  try {{ const r = await fetch('/settings', {{cache: 'no-store'}});
         if (r.ok) {{ location.href = '/settings?msg=' + encodeURIComponent('{label} restarted.'); return; }} }} catch (e) {{}}
  if (Date.now() - t0 > 60000) {{ document.getElementById('wait').textContent =
      'Still not back after a minute - check: journalctl -u carcount-web -n 30'; return; }}
  setTimeout(poll, 1000);
}}, 2500);
</script>"""
    return page('Restarting', body, active='/settings')


def save_from_form(form, keys):
    """Validate the posted keys; save on success. Returns (values, errors, changed_keys)."""
    values = cd.load_settings()
    old = dict(values)
    errors = {}
    for key in keys:
        st = cd.SETTING[key]
        raw = form.get(key, [''])[0] if st['type'] != 'bool' else (key in form)
        v, err = cd.parse_setting(key, raw)
        values[key] = raw if err and st['type'] not in ('bool', 'choice') else v
        if err:
            errors[key] = err
    if errors:
        return values, errors, []
    changed = [k for k in keys if values[k] != old[k]]
    if changed:
        cd.save_settings(values)
    return values, errors, changed


def reset_settings(keys):
    values = cd.load_settings()
    changed = [k for k in keys if values[k] != cd.SETTING[k]['default']]
    for k in keys:
        values[k] = cd.SETTING[k]['default']
    cd.save_settings(values)
    return changed


def apply_message(changed):
    if not changed:
        return 'No changes.'
    msg = 'Saved: ' + ', '.join(cd.SETTING[k]['label'] for k in changed) + '.'
    counter = [k for k in changed if k in COUNTER_KEYS and cd.SETTING[k]['apply'] != 'web']
    if counter:
        err = signal_counter(signal.SIGHUP)
        if err:
            return msg + f' Note: {err}.'
        if any(cd.SETTING[k]['apply'] == 'restart' for k in counter):
            return msg + ' The counter is restarting to apply camera changes.'
    if any(k in ALERT_KEYS for k in changed) and service_started('carcount-alerts') is None:
        return msg + ' The notification service is not running - they apply when it starts.'
    if any(k in POWER_KEYS for k in changed) and not ups_service()['active']:
        return msg + ' The UPS monitor is not running - they apply when it starts.'
    return msg + ' Applied.'


# ---------------------------------------------------------------- power
def ups_service():
    def q(cmd):
        try:
            return subprocess.run(['systemctl', cmd, 'carcount-ups'], capture_output=True,
                                  text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return 'unknown'
    return {'active': q('is-active') == 'active', 'enabled': q('is-enabled') == 'enabled'}


def read_ups_direct():
    """Read the UPS ourselves when ups_monitor isn't running (carcount is in the gpio and i2c groups)."""
    try:
        import gpiod
        import smbus2
        import ups_monitor as um
        with gpiod.request_lines(um.GPIOCHIP, consumer='carcount-web',
                                 config={um.PLD_PIN: gpiod.LineSettings(direction=gpiod.line.Direction.INPUT)}) as r:
            ac = r.get_value(um.PLD_PIN) == gpiod.line.Value.ACTIVE
        with smbus2.SMBus(um.I2C_BUS) as bus:
            volts, pct = um.read_gauge(bus)
        return {'ac': ac, 'volts': round(volts, 3), 'pct': round(pct, 1), 'direct': True}
    except Exception:
        return None


def input_voltage():
    try:
        out = subprocess.run(['vcgencmd', 'pmic_read_adc', 'EXT5V_V'], capture_output=True,
                             text=True, timeout=3).stdout
        return float(re.search(r'=([\d.]+)V', out)[1])
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None


def dur(s):
    s = int(s)
    return f'{s // 3600} h {s % 3600 // 60} min' if s >= 3600 else f'{s // 60} min {s % 60} s'


def power_card():
    svc = ups_service()
    st = cd.ups_state()
    if not st:
        st = read_ups_direct()
    vin = input_voltage()
    if not st:
        badge = status_badge('bad', 'Cannot read the UPS (I2C/GPIO error)')
    elif st.get('pending') and st.get('shutdown_enabled'):
        badge = status_badge('bad', f'Shutdown pending: {st["pending"]}')
    elif not st['ac']:
        badge = status_badge('warn', 'On battery - mains power lost' +
                             (f' {dur(st["on_battery_s"])} ago' if st.get('on_battery_s') else ''))
    else:
        badge = status_badge('good', 'On mains power')

    def stat(label, val):
        return f'<div><span>{label}</span>{esc(val) if val not in ("", None) else "—"}</div>'
    if svc['active']:
        mon = 'running' + ('' if svc['enabled'] else ' (not enabled at boot)')
        shut = ('enabled' if st and st.get('shutdown_enabled') else 'disabled (log only)')
        if st and st.get('grace_left_s'):
            shut += f' · grace {st["grace_left_s"]} s left'
    else:
        mon = 'not running' + (' (enabled)' if svc['enabled'] else '')
        shut = 'inactive (monitor not running)'
    stats = ''.join([
        stat('Power source', None if not st else 'Mains' if st['ac'] else 'Battery'),
        stat('Battery voltage', None if not st or st.get('volts') is None else f'{st["volts"]:.2f} V'),
        stat('Battery charge', None if not st or st.get('pct') is None else f'{st["pct"]:.0f} %'),
        stat('Pi input voltage', None if vin is None else f'{vin:.2f} V'),
        stat('UPS monitor', mon),
        stat('Low-battery shutdown', shut),
    ])
    notes = []
    if st and st.get('direct'):
        notes.append('Read directly by the web page because the UPS monitor is not running.')
    notes.append('Battery voltage and charge are only meaningful with batteries installed.')
    if vin is not None and vin < 4.9:
        notes.append(f'Input voltage is low ({vin:.2f} V) - check the extension cord and power supply.')
    if not svc['active']:
        notes.append('After testing the batteries, start the monitor with: '
                     'sudo systemctl enable --now carcount-ups')
    return (f'<h2>UPS status <span class="muted">· {datetime.now():%H:%M:%S}</span></h2><p>{badge}</p>'
            f'<div class="stats">{stats}</div>' + ''.join(f'<p class="muted">{esc(n)}</p>' for n in notes))


def battery_chart():
    rows = [r for r in cd.health(cd.today()) if r.get('battery_pct')]
    if not rows:
        return '<p class="muted">No battery history yet - it is logged once the UPS monitor is running.</p>'
    buckets = [[] for _ in range(144)]          # 10-minute buckets
    for r in rows:
        t = r['timestamp']
        try:
            buckets[int(t[11:13]) * 6 + int(t[14:16]) // 10].append(float(r['battery_pct']))
        except (ValueError, IndexError):
            pass
    vals = [round(sum(b) / len(b)) if b else None for b in buckets]
    labels = [f'{i // 6:02d}' for i in range(144)]
    tips = [f'{i // 6:02d}:{i % 6 * 10:02d} · {v} %' if v is not None else '' for i, v in enumerate(vals)]
    now = datetime.now()
    return line_chart(vals, labels, tips, label_every=18, ymax=100,
                      upto=now.hour * 6 + now.minute // 10 + 1)


def power_events(limit=25):
    try:
        out = subprocess.run(['journalctl', '-u', 'carcount-ups', '--since', '-14 days', '-o', 'cat',
                              '--no-pager'], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return '<p class="muted">Could not read the system journal.</p>'
    keep = ('Power ', 'SHUTDOWN', 'Battery low', 'Would shut down', 'Started:', 'Setting UPS_',
            'gauge read failed', 'wake alarm')
    lines = [ln for ln in out.splitlines() if any(k in ln for k in keep)][-limit:]
    if not lines:
        return '<p class="muted">No power events logged in the last 14 days.</p>'
    rows = []
    for ln in reversed(lines):
        ts, _, rest = ln.partition(',')
        msg = rest.split(' ', 2)[-1] if rest else ln
        rows.append(f'<tr><td style="white-space:nowrap">{esc(ts[:16])}</td><td>{esc(msg)}</td></tr>')
    return '<table><tr><th>Time</th><th>Event</th></tr>' + ''.join(rows) + '</table>'


# ---------------------------------------------------------------- alerts
KEY_RE = re.compile(r'^[A-Za-z0-9]{30}$')


def save_keys(form):
    try:                                   # keep a key saved on its own (e.g. user key first, token later)
        raw = json.loads(cd.PUSHOVER_PATH.read_text())
        old = (raw.get('user') or '', raw.get('token') or '')
    except (OSError, ValueError, AttributeError):
        old = ('', '')
    user = form.get('user', [''])[0].strip() or old[0]
    token = form.get('token', [''])[0].strip() or old[1]
    if not KEY_RE.match(user) or (token and not KEY_RE.match(token)):
        return 'Not saved: the user key and API token are each 30 letters and digits.'
    if not token:
        cd.save_pushover_keys(user, '')
        return 'User key saved. Now add the API token.'
    cd.save_pushover_keys(user, token)
    return 'Pushover keys saved. Use "Send test notification" to check them.'


def alerts_page(values=None, errors=None, msg=None):
    values = values or cd.load_settings()
    errors = errors or {}
    keys = cd.pushover_keys()
    running = service_started('carcount-alerts') is not None
    try:
        user_only = not keys and bool(json.loads(cd.PUSHOVER_PATH.read_text()).get('user'))
    except (OSError, ValueError, AttributeError):
        user_only = False
    status = (status_badge('good', 'Pushover keys saved') if keys else
              status_badge('warn', 'User key saved - API token missing') if user_only else
              status_badge('warn', 'Pushover keys not set')) + ' ' + \
        (status_badge('good', 'notification service running') if running else
         status_badge('bad', 'notification service not running'))
    hint = '•••••• saved - leave blank to keep' if keys else ''
    log_rows = ''.join(
        f'<tr><td>{esc(r["time"][5:16].replace("T", " "))}</td><td>{esc(r["title"])}</td>'
        f'<td>{esc(r["message"]).replace(chr(10), "<br>")}'
        f'{"<br>" + status_badge("bad", r["error"]) if r.get("error") else ""}</td></tr>'
        for r in alerts.recent(30))
    body = f"""{banner(msg, errors)}
<div class="card"><h2>Pushover</h2><p>{status}</p>
<form method="post" action="/alerts/keys"><fieldset>
<div class="field"><label for="user">User key</label>
<input type="password" name="user" id="user" autocomplete="off" placeholder="{hint}">
<div class="help">On <a href="https://pushover.net" target="_blank" rel="noopener">pushover.net</a> after
signing in: "Your User Key", top right.</div></div>
<div class="field"><label for="token">API token</label>
<input type="password" name="token" id="token" autocomplete="off" placeholder="{hint}">
<div class="help">On pushover.net: "Create an Application/API Token", name it Car Counter, copy the API Token.</div></div>
</fieldset>
<div class="buttons"><button class="primary" type="submit">Save keys</button>
<button type="submit" formaction="/alerts/test"{"" if keys else " disabled"}>Send test notification</button></div></form>
<p class="muted">Install the Pushover app on your phone and sign in with the same account. The keys are stored
only on the Pi, never in the code repository.</p></div>
{settings_form('/alerts', ALERT_KEYS, values, errors, 'notification settings')}
<div class="card"><h2>Recent notifications</h2>
{f'<table class="log"><tr><th>When</th><th>Type</th><th>Message</th></tr>{log_rows}</table>' if log_rows
 else '<p class="muted">None sent yet.</p>'}</div>"""
    return page('Alerts', body, active='/alerts')


def power_page(values=None, errors=None, msg=None):
    values = values or cd.load_settings()
    errors = errors or {}
    body = f"""{banner(msg, errors)}
<div class="card" id="power-card">{power_card()}</div>
<div class="row"><div class="card"><h2>Battery charge · today (%)</h2>{battery_chart()}</div>
<div class="card"><h2>Power events · last 14 days</h2>{power_events()}</div></div>
{settings_form('/power', POWER_KEYS, values, errors, 'power settings')}
<p class="muted">Shutdown only ever happens while running on battery. On mains power the Pi is never shut down,
whatever these settings say.</p>
<script>
setInterval(async () => {{
  try {{
    const r = await fetch('/api/power-card', {{cache: 'no-store'}});
    if (r.ok) document.getElementById('power-card').innerHTML = await r.text();
  }} catch (e) {{}}
}}, 3000);
</script>"""
    return page('Power', body, active='/power')


# ---------------------------------------------------------------- files
def safe_path(key, rel):
    root = cd.ROOTS.get(key)
    if not root:
        return None
    p = (root / rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p if p.is_file() and p.suffix.lower() in ('.jpg', '.csv') else None


def thumbnail(src):
    dst = src.parent.parent / 'thumbs' / src.name
    if dst.exists():
        return dst
    from PIL import Image
    with _thumb_lock:
        if not dst.exists():
            dst.parent.mkdir(exist_ok=True)
            with Image.open(src) as im:
                im.thumbnail(THUMB_SIZE)
                im.save(dst, quality=80)
    return dst


# ---------------------------------------------------------------- request checks
def tailscale_name():
    """This Pi's MagicDNS name (e.g. carcounter.<tailnet>.ts.net), or None."""
    try:
        out = subprocess.run(['tailscale', 'status', '--json'], capture_output=True, text=True, timeout=5).stdout
        return (json.loads(out).get('Self', {}).get('DNSName') or '').rstrip('.').lower() or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def allowed_hosts():
    host = socket.gethostname().lower()
    names = {'localhost', host, f'{host}.local'}
    ts = tailscale_name()
    if ts:
        names.add(ts)
    return names


ALLOWED_HOSTS = allowed_hosts()
_ts_checked = [time.time()]


def host_ok(hostport):
    """Host header must name this Pi or be a plain IP address. Blocks DNS rebinding, where a
    malicious site points its own domain name at the Pi so your browser treats it as same-site."""
    if not hostport:
        return False
    h = hostport.strip().lower()
    if h.startswith('['):                       # [ipv6]:port
        h = h[1:h.find(']')] if ']' in h else h
    elif h.count(':') == 1:
        h = h.rsplit(':', 1)[0]
    h = h.rstrip('.')
    if h in ALLOWED_HOSTS:
        return True
    if h.endswith('.ts.net') and time.time() - _ts_checked[0] > 60:   # Tailscale wasn't up at start?
        _ts_checked[0] = time.time()
        ts = tailscale_name()
        if ts:
            ALLOWED_HOSTS.add(ts)
        return h == ts
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def same_origin(headers):
    """A form POST must come from one of this site's own pages (blocks cross-site request forgery).
    Browsers always send Origin on POSTs; tools like curl send nothing and are allowed."""
    if headers.get('Sec-Fetch-Site') in ('cross-site', 'same-site'):
        return False
    src = headers.get('Origin') or headers.get('Referer')
    if not src or src == 'null':
        return not src                          # 'null' = sandboxed/opaque origin -> reject
    netloc = src.split('://', 1)[-1].split('/', 1)[0]
    return netloc.lower() == (headers.get('Host') or '').lower()


class Handler(BaseHTTPRequestHandler):
    def refused(self, post=False):
        """True (and a 403 sent) if this request fails the host / origin checks."""
        if not host_ok(self.headers.get('Host')):
            why = 'Unknown host name'
        elif post and not same_origin(self.headers):
            why = 'Form submitted from another site'
        else:
            return False
        self.send_error(403, why)
        return True

    def do_GET(self):
        if self.refused():
            return
        path = unquote(self.path.split('?')[0])
        try:
            if path == '/':
                q = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
                try:
                    p = max(1, min(int(q.get('p', ['1'])[0]), 10000))
                except ValueError:
                    p = 1
                return self.send_html(dashboard(p))
            if path == '/days':
                return self.send_html(days_page())
            if path == '/settings':
                q = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
                return self.send_html(settings_page(msg=q.get('msg', [None])[0]))
            if path == '/alerts':
                q = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
                return self.send_html(alerts_page(msg=q.get('msg', [None])[0]))
            if path == '/power':
                q = parse_qs(self.path.split('?', 1)[1]) if '?' in self.path else {}
                return self.send_html(power_page(msg=q.get('msg', [None])[0]))
            if path == '/api/power-card':
                return self.send_html(power_card())
            if path in ('/api/live', '/api/version'):
                if path == '/api/live':
                    data = json.dumps(cd.live_state() or {}).encode()
                else:
                    day = parse_qs(self.path.split('?', 1)[1] if '?' in self.path else '').get('day', [''])[0]
                    data = json.dumps({'v': events_version(day) if cd.DAY_RE.match(day) else ''}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            m = re.fullmatch(r'/view/(\d{4}-\d{2}-\d{2})/([\w.-]+\.jpg)', path)
            if m:
                html_ = view_page(m[1], m[2])
                return self.send_html(html_) if html_ else self.send_error(404, 'Photo not found')
            m = re.fullmatch(r'/day/(\d{4}-\d{2}-\d{2})', path)
            if m:
                return self.send_html(day_page(m[1]))
            if path == '/summary.csv':
                for root in cd.read_roots():
                    if (root / 'summary_daily.csv').exists():
                        return self.send_file(root / 'summary_daily.csv')
                return self.send_error(404, 'No summary yet (maintenance runs hourly)')
            m = re.fullmatch(r'/(file|thumb)/(usb|sd)/(.+)', path)
            if m:
                p = safe_path(m[2], m[3])
                if not p:
                    return self.send_error(404)
                return self.send_file(thumbnail(p) if m[1] == 'thumb' else p, cache=True)
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if self.refused(post=True):
            return
        try:
            n = min(int(self.headers.get('Content-Length') or 0), 100_000)
            form = parse_qs(self.rfile.read(n).decode(errors='replace'), keep_blank_values=True)
            if self.path == '/alerts/keys':
                return self.redirect('/alerts?msg=' + quote(save_keys(form)))
            if self.path == '/alerts/test':
                err = alerts.send(alerts.OK, 'Test notification from the car counter.')
                return self.redirect('/alerts?msg=' + quote(f'Test NOT sent: {err}' if err else
                                                            'Test notification sent - check your phone.'))
            for base, keys, page_fn in (('/settings', COUNTER_KEYS, settings_page), ('/power', POWER_KEYS, power_page),
                                        ('/alerts', ALERT_KEYS, alerts_page)):
                if self.path == base:
                    values, errors, changed = save_from_form(form, keys)
                    if errors:
                        return self.send_html(page_fn(values, errors))
                    return self.redirect(f'{base}?msg=' + quote(apply_message(changed)))
                if self.path == base + '/reset':
                    changed = reset_settings(keys)
                    return self.redirect(f'{base}?msg=' + quote('Reset to defaults. ' + apply_message(changed)))
            m = re.fullmatch(r'/restart/(web|counter|alerts)', self.path)
            if m:
                unit, label, files = SERVICES[m[1]]
                err = check_code(files)
                if err:
                    return self.redirect('/settings?msg=' + quote(f'{label} NOT restarted - the code has an error: {err}'))
                if m[1] == 'web':
                    self.send_html(restart_page(label))
                    self.wfile.flush()
                    threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGTERM)).start()
                    return None
                if m[1] == 'alerts':
                    err = signal_counter(signal.SIGTERM, unit)
                    return self.redirect('/settings?msg=' + quote(err or 'Notification service restarting.'))
                err = signal_counter(signal.SIGTERM)
                return self.redirect('/settings?msg=' + quote(err or 'Counter restarting - counting resumes in about 10 s.'))
            if self.path == '/recalibrate':
                err = signal_counter(signal.SIGUSR1)
                return self.redirect('/settings?msg=' + quote(err or 'Recalibrating - keep the beam clear.'))
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def redirect(self, url):
        self.send_response(303)
        self.send_header('Location', url)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def send_html(self, text):
        data = text.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, p, cache=False):
        data = p.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(p.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(data)))
        if cache:
            self.send_header('Cache-Control', 'max-age=86400')
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass   # keep the journal quiet


def main():
    srv = ThreadingHTTPServer(('', PORT), Handler)
    print(f'Serving on http://0.0.0.0:{PORT}/', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
