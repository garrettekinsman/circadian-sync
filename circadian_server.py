#!/usr/bin/env python3
"""
circadian_server.py — Local lightweight web server for Circadian Rhythm Beam Control.

Features:
- Visualizes 24h circadian curve of color temperature, brightness, and zone distribution.
- Interactive time scrubber (00:00 - 23:59).
- Real-time LIFX Beam UDP updates as slider moves (smooth multi-second transitions, debounced).
- Dedicated "Go to Now" / "Live Clock" button that snaps to current Long Beach time.
- Single weather pixel indicator (zone 39) with black separator (zone 38) fading off at noon.
- Actual astronomical sunset matching with frozen amber-glow gradient slowly setting to 10 PM and fading off.
"""

import os
import sys
import json
import time
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# Ensure we can import circadian_engine from current directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import circadian_engine

# lifxlan is optional — everything runs in simulation mode without hardware
beam_device = None
DEFAULT_IP = os.environ.get("LIFX_BEAM_IP", "192.168.1.100")
DEFAULT_MAC = os.environ.get("LIFX_BEAM_MAC", "d0:73:d5:00:00:00")

try:
    from lifxlan.multizonelight import MultiZoneLight  # type: ignore
    beam_device = MultiZoneLight(DEFAULT_MAC, DEFAULT_IP, 1, 56700, 12345)
except Exception as e:
    print(f"[circadian_server] Note: LIFX beam connection setup error: {e}", file=sys.stderr)

# Global State initialized with Long Beach current time
lb_initial = circadian_engine.get_long_beach_now()
initial_minute = lb_initial.hour * 60 + lb_initial.minute

state = {
    "live_mode": True,  # Default to live clock following actual time
    "selected_minute": initial_minute,
    "is_weekend": lb_initial.weekday() >= 5,
    "weather_state": circadian_engine.parse_weather_state(circadian_engine.get_weather_condition()),
    "weather_sunny": circadian_engine.is_sunny(circadian_engine.get_weather_condition()),
    "weather_override": False,  # True = manual UI override, stops auto-following NOAA cache
    "last_applied_minute": -1,
    "last_applied_ts": 0.0,
    "sunset_minute": circadian_engine.calculate_sunset_minute(lb_initial)
}
state_lock = threading.Lock()

def send_to_beam(zones, duration_ms=2000):
    global beam_device
    if not beam_device:
        return
    try:
        total_b = sum(z[2] for z in zones)
        if total_b == 0:
            beam_device.set_power(0, duration=duration_ms)
        else:
            beam_device.set_power(65535)
            if hasattr(beam_device, "extended_set_zone_color"):
                beam_device.extended_set_zone_color(zones, duration=duration_ms)
            else:
                beam_device.set_zone_colors(zones, duration=duration_ms)
    except Exception as e:
        print(f"[circadian_server] Error sending to LIFX Beam: {e}", file=sys.stderr)

def live_clock_worker():
    while True:
        # Auto-follow live weather unless manually overridden in the UI:
        # re-read the NOAA cache every tick so rain/cloudy warm shifts kick in
        # as soon as the daily forecast changes.
        if not state["weather_override"]:
            wstate = circadian_engine.parse_weather_state(circadian_engine.get_weather_condition())
            with state_lock:
                state["weather_state"] = wstate
                state["weather_sunny"] = wstate in ("sunny", "partly_cloudy")
        with state_lock:
            live = state["live_mode"]
            weekend = state["is_weekend"]
            wstate = state["weather_state"]
        
        # Check active exemption (auto-resets if new day)
        ex = circadian_engine.get_exemption()
        if ex:
            if ex.get("schedule") == "weekday":
                weekend = False
            elif ex.get("schedule") == "weekend":
                weekend = True

        if live:
            now = circadian_engine.get_long_beach_now()
            cur_min = now.hour * 60 + now.minute
            sunset_m = circadian_engine.calculate_sunset_minute(now)
            zones = circadian_engine.calculate_beam_zones(cur_min, is_weekend=weekend, weather_state=wstate, sunset_min=sunset_m)
            # Smooth 3.5s transition for automatic live clock ticks
            print(f"[circadian_server] Tick {cur_min // 60:02d}:{cur_min % 60:02d} sending to beam (exemption: {ex})...", flush=True)
            send_to_beam(zones, duration_ms=3500)
            with state_lock:
                state["selected_minute"] = cur_min
                state["last_applied_minute"] = cur_min
                state["sunset_minute"] = sunset_m
                state["is_weekend"] = weekend
        time.sleep(30)

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LIFX Beam — Circadian Rhythm Control</title>
<style>
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #f0883e;
    --blue: #58a6ff;
    --green: #238636;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    padding: 24px;
    display: flex;
    justify-content: center;
  }
  .container {
    width: 100%;
    max-width: 880px;
  }
  header {
    margin-bottom: 20px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  h1 { font-size: 22px; font-weight: 600; }
  .badge {
    background: #238636;
    color: #fff;
    padding: 3px 8px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
  }
  .badge.live {
    background: #1f6feb;
    box-shadow: 0 0 8px rgba(31, 111, 235, 0.6);
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 20px;
  }
  .time-display {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 12px;
  }
  .time-left {
    display: flex;
    align-items: baseline;
    gap: 12px;
  }
  .current-time {
    font-size: 40px;
    font-weight: 700;
    letter-spacing: -1px;
    color: #fff;
    font-variant-numeric: tabular-nums;
  }
  .tz-tag {
    font-size: 12px;
    color: var(--muted);
    font-weight: 500;
  }
  .phase-name {
    font-size: 14px;
    color: var(--accent);
    font-weight: 600;
    text-align: right;
  }
  .sunset-pill {
    font-size: 12px;
    color: #e3b341;
    margin-top: 3px;
  }
  .slider-container {
    margin: 18px 0;
  }
  input[type="range"] {
    width: 100%;
    height: 10px;
    border-radius: 5px;
    background: linear-gradient(to right, #000 0%, #1a0800 24%, #d35400 29%, #f39c12 40%, #ffffff 50%, #f39c12 70%, #d35400 78%, #7e1e02 87%, #000 90%, #000 100%);
    outline: none;
    cursor: pointer;
    -webkit-appearance: none;
  }
  input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    background: #fff;
    border: 2px solid #58a6ff;
    box-shadow: 0 0 10px rgba(0,0,0,0.6);
    cursor: grab;
    transition: transform 0.1s ease;
  }
  input[type="range"]::-webkit-slider-thumb:active {
    transform: scale(1.15);
    cursor: grabbing;
  }
  .beam-preview-wrap {
    margin: 22px 0 16px 0;
  }
  .beam-preview-label {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 8px;
  }
  .beam-bar {
    display: flex;
    height: 42px;
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,0.2);
    background: #000;
    box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  }
  .zone-pixel {
    flex: 1;
    height: 100%;
    transition: background-color 0.4s ease-out;
  }
  .zone-pixel.separator {
    border-left: 1px solid rgba(255,255,255,0.1);
    border-right: 1px solid rgba(255,255,255,0.1);
  }
  .controls-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-top: 16px;
  }
  .btn-action {
    background: #21262d;
    border: 1px solid var(--border);
    color: var(--text);
    padding: 10px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    display: flex;
    align-items: center;
    justify-content: space-between;
    transition: all 0.2s ease;
  }
  .btn-action:hover { background: #30363d; border-color: #8b949e; }
  .btn-action.active {
    background: #1f6feb;
    border-color: #388bfd;
    color: #fff;
  }
  .btn-now {
    background: #238636;
    border-color: #2ea043;
    color: #fff;
    font-weight: 600;
  }
  .btn-now:hover { background: #2ea043; }
  .quick-buttons {
    display: flex;
    gap: 8px;
    margin-top: 16px;
    flex-wrap: wrap;
    align-items: center;
  }
  .btn-quick {
    background: #21262d;
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 12px;
    border-radius: 4px;
    font-size: 12px;
    cursor: pointer;
    transition: all 0.15s ease;
  }
  .btn-quick:hover { background: #30363d; color: #fff; }
  canvas {
    width: 100%;
    height: 140px;
    border-radius: 6px;
    background: #090d13;
    border: 1px solid var(--border);
    margin-top: 8px;
  }
  .status-line {
    font-size: 11px;
    color: var(--muted);
    margin-top: 10px;
    display: flex;
    justify-content: space-between;
  }
  .rule-pill {
    display: inline-block;
    padding: 2px 6px;
    background: #1f242c;
    border-radius: 4px;
    color: #7ee787;
    font-size: 11px;
  }
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>LIFX Beam Circadian Controller</h1>
      <p style="font-size: 12px; color: var(--muted); margin-top: 3px;">
        40 Zones · Astronomical Sunset · Smooth Magical Fades
      </p>
    </div>
    <span class="badge live" id="beamStatus">Live Clock</span>
  </header>

  <div class="card">
    <div class="time-display">
      <div class="time-left">
        <div class="current-time" id="timeStr">19:15</div>
        <div class="tz-tag">Long Beach, CA</div>
      </div>
      <div>
        <div class="phase-name" id="phaseStr">Evening Sunset Amber</div>
        <div class="sunset-pill" id="sunsetStr">Sunset: 18:35 PDT</div>
      </div>
    </div>

    <div class="slider-container">
      <input type="range" id="timeSlider" min="0" max="1439" value="1155" step="1">
    </div>

    <div class="beam-preview-wrap">
      <div class="beam-preview-label">
        <span>Zone 0 (Bottom)</span>
        <span>Beam Preview (Zone 38 Black Sep · Zone 39 Weather until Noon)</span>
        <span>Zone 39 (Top)</span>
      </div>
      <div class="beam-bar" id="beamBar"></div>
    </div>

    <div class="controls-grid">
      <button class="btn-action btn-now" id="btnNow" onclick="goNow()">
        <span>⏱️ Go to "Now"</span>
        <span id="nowTimeBadge">Live</span>
      </button>

      <button class="btn-action" id="btnSchedule" onclick="toggleSchedule()">
        <span>Schedule</span>
        <span id="scheduleText">Weekday</span>
      </button>

      <button class="btn-action active" id="btnWeather" onclick="toggleWeather()">
        <span>Weather Pixel</span>
        <span id="weatherText">Sunny (Happy Yellow)</span>
      </button>

      <button class="btn-action" id="btnPowerOff" onclick="triggerPowerOff()" style="border-color: #da3633; color: #f85149;">
        <span>🛑 Power Off</span>
        <span>2s Wipe</span>
      </button>
    </div>

    <div class="quick-buttons">
      <span style="font-size: 12px; color: var(--muted); margin-right: 4px;">Milestones:</span>
      <button class="btn-quick" onclick="setTime(370)">06:10 (Pre-glow bottom)</button>
      <button class="btn-quick" onclick="setTime(395)">06:35 (Rising Alarm)</button>
      <button class="btn-quick" onclick="setTime(420)">07:00 (Weather Pixel Appears)</button>
      <button class="btn-quick" onclick="setTime(480)">08:00 (Warm Morning + Weather Pixel)</button>
      <button class="btn-quick" onclick="setTime(600)">10:00 (Weather Off / Day Off/On)</button>
      <button class="btn-quick" onclick="setTime(990)">16:30 (Weekday Resumes On)</button>
      <button class="btn-quick" onclick="setTime(1115)">18:35 (Sunset Amber-Glow)</button>
      <button class="btn-quick" onclick="setTime(1260)">21:00 (Red-Orange Setting)</button>
      <button class="btn-quick" onclick="setTime(1320)">22:00 (10 PM Bedtime Fade)</button>
      <button class="btn-quick" onclick="setTime(1360)">22:40 (Off / Sleep)</button>
    </div>

    <div class="status-line">
      <span id="hwUpdateStatus"><span class="rule-pill">Design Rule</span> Transitions: 2.0s smooth fade</span>
      <span id="zoneStats">Loading zone info...</span>
    </div>
  </div>

  <div class="card">
    <div style="font-size: 14px; font-weight: 600; margin-bottom: 6px;">24-Hour Circadian Trajectory & Sunset Curve</div>
    <p style="font-size: 12px; color: var(--muted); margin-bottom: 12px;">
      AM schedule fixed to morning routine · Weather pixel turns off at noon · Sunset follows astronomical time and sets to 10 PM fade-off.
    </p>
    <canvas id="curveCanvas" width="800" height="140"></canvas>
  </div>
</div>

<script>
let state = {
  minute: 1155,
  live: true,
  is_weekend: false,
  sunny: true,
  sunset_minute: 1115
};

const beamBar = document.getElementById("beamBar");
for (let i = 0; i < 40; i++) {
  const p = document.createElement("div");
  p.className = "zone-pixel" + (i === 38 ? " separator" : "");
  p.id = "z" + i;
  beamBar.appendChild(p);
}

const slider = document.getElementById("timeSlider");
let debounceTimer = null;
let lastUdpSendTime = 0;

function minToTimeStr(m) {
  const hh = String(Math.floor(m / 60)).padStart(2, '0');
  const mm = String(m % 60).padStart(2, '0');
  return `${hh}:${mm}`;
}

function updatePhaseLabel(m, isWeekend, sunsetM) {
  const wakeStart = isWeekend ? 420 : 360;
  const alarmStart = isWeekend ? 435 : 375;
  const wakeFull = isWeekend ? 525 : 465;
  const weatherOff = 600; // 10:00 AM
  const workOff = 600;    // 10:00 AM (weekday off)
  const workOn = 990;     // 16:30 (4:30 PM weekday back on)
  const sunsetStart = sunsetM - 45;
  const fadeOffStart = 1305; // 21:45 (9:45 PM)
  const bedtimeOff = 1335;   // 22:15 (10:15 PM)

  let text = "Sleep / Off";
  if (m >= wakeStart && m < alarmStart) text = "Pre-Glow (Dim Red Embers Bottom)";
  else if (m >= alarmStart && m < wakeFull) text = "Extended Sunrise (Red & Orange Gradient Rising)";
  else if (m >= wakeFull && m < weatherOff) text = "Morning Warm Ambient (~3000K) + Weather Pixel";
  else if (!isWeekend && m >= workOff && m < workOn) text = "Daytime Power Save (Off until 4:30 PM)";
  else if ((!isWeekend && m >= workOn && m < sunsetStart) || (isWeekend && m >= weatherOff && m < sunsetStart)) text = "Full Daytime Ambient";
  else if (m >= sunsetStart && m < sunsetM) text = "Sunset Approaching (Transitioning to Amber-Glow)";
  else if (m >= sunsetM && m < 1200) text = "Post-Sunset Transition (Descending to Evening Plateau)";
  else if (m >= 1200 && m < fadeOffStart) text = "Evening Wind-Down Plateau (8:00 - 9:45 PM Amber Base into Red)";
  else if (m >= fadeOffStart && m < bedtimeOff) text = "9:45 PM Bedtime Fade-Off (Deep Crimson Embers to Black)";
  
  document.getElementById("phaseStr").textContent = text;
  document.getElementById("sunsetStr").textContent = `Astronomical Sunset: ${minToTimeStr(sunsetM)} PDT`;
}

function fetchState() {
  fetch('/api/state')
    .then(r => r.json())
    .then(data => {
      state = data;
      slider.value = state.minute;
      updateUI(false);
      renderCurve();
    });
}

function updateUI(sendUDP = true) {
  document.getElementById("timeStr").textContent = minToTimeStr(state.minute);
  updatePhaseLabel(state.minute, state.is_weekend, state.sunset_minute);

  document.getElementById("scheduleText").textContent = state.is_weekend ? "Weekend" : "Weekday";
  document.getElementById("btnSchedule").classList.toggle("active", state.is_weekend);

  document.getElementById("weatherText").textContent = state.sunny ? "Sunny (Happy Yellow)" : "Cloudy / Warm";
  document.getElementById("btnWeather").classList.toggle("active", state.sunny);

  const badge = document.getElementById("beamStatus");
  if (state.live) {
    badge.textContent = "Live Clock";
    badge.className = "badge live";
    document.getElementById("btnNow").classList.add("active");
  } else {
    badge.textContent = "Manual Scrubber";
    badge.className = "badge";
    document.getElementById("btnNow").classList.remove("active");
  }

  // Fetch zone RGB preview & update hardware
  const params = new URLSearchParams({
    minute: state.minute,
    weekend: state.is_weekend ? "1" : "0",
    sunny: state.sunny ? "1" : "0",
    send: sendUDP ? "1" : "0"
  });

  fetch('/api/zones?' + params.toString())
    .then(r => r.json())
    .then(data => {
      if (data.rgb_zones) {
        for (let i = 0; i < 40; i++) {
          const rgb = data.rgb_zones[i];
          const el = document.getElementById("z" + i);
          if (el) el.style.backgroundColor = `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
        }
      }
      if (data.status) {
        document.getElementById("zoneStats").textContent = data.status;
      }
    });
}

// Scrubber events with smooth real-time throttled hardware updates
slider.addEventListener("input", (e) => {
  state.minute = parseInt(e.target.value);
  state.live = false;
  
  // Real-time smooth streaming: trigger immediate update throttled to 250ms
  const nowTs = Date.now();
  if (nowTs - lastUdpSendTime > 250) {
    lastUdpSendTime = nowTs;
    updateUI(true);
  } else {
    updateUI(false);
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      lastUdpSendTime = Date.now();
      updateUI(true);
    }, 250);
  }
});

slider.addEventListener("change", (e) => {
  state.minute = parseInt(e.target.value);
  state.live = false;
  updateUI(true);
});

function goNow() {
  fetch('/api/go_now')
    .then(r => r.json())
    .then(data => {
      state.live = true;
      state.minute = data.minute;
      slider.value = state.minute;
      updateUI(true);
      renderCurve();
    });
}

function triggerPowerOff() {
  state.live = false;
  fetch('/api/power_off')
    .then(r => r.json())
    .then(data => {
      // Set preview to 0 brightness
      for (let i = 0; i < 40; i++) {
        const el = document.getElementById("z" + i);
        if (el) el.style.background = "#000000";
      }
      document.getElementById("phaseStr").textContent = "Powered Off (Wiped to Black)";
      const badge = document.getElementById("beamStatus");
      badge.textContent = "Powered Off";
      badge.className = "badge";
      document.getElementById("btnNow").classList.remove("active");
    });
}

function setTime(m) {
  state.minute = m;
  slider.value = m;
  state.live = false;
  updateUI(true);
}

function toggleSchedule() {
  state.is_weekend = !state.is_weekend;
  updateUI(true);
  renderCurve();
}

function toggleWeather() {
  state.sunny = !state.sunny;
  updateUI(true);
}

function renderCurve() {
  const canvas = document.getElementById("curveCanvas");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;

  ctx.clearRect(0, 0, w, h);

  // Time grid lines
  ctx.strokeStyle = "#21262d";
  ctx.lineWidth = 1;
  for (let hr = 0; hr <= 24; hr += 3) {
    const x = (hr / 24) * w;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.stroke();

    ctx.fillStyle = "#8b949e";
    ctx.font = "10px sans-serif";
    ctx.fillText(`${hr}:00`, x + 4, h - 6);
  }

  // Draw sunset marker
  const sunsetX = (state.sunset_minute / 1440) * w;
  ctx.strokeStyle = "rgba(227, 179, 65, 0.4)";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(sunsetX, 0);
  ctx.lineTo(sunsetX, h - 16);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#e3b341";
  ctx.font = "9px sans-serif";
  ctx.fillText("Sunset", sunsetX - 14, 12);

  // Draw curve sample
  ctx.beginPath();
  ctx.lineWidth = 3;
  ctx.strokeStyle = "#f0883e";

  const wakeStart = state.is_weekend ? 420 : 360;
  const wakeFull = state.is_weekend ? 525 : 465;
  const sunsetStart = state.sunset_minute - 45;
  const fadeOffStart = 1305; // 21:45
  const bedtimeOff = 1335;   // 22:15

  for (let x = 0; x < w; x++) {
    const m = (x / w) * 1440;
    let bNorm = 0;
    if (m >= wakeStart && m < wakeFull) {
      bNorm = (m - wakeStart) / (wakeFull - wakeStart);
    } else if (m >= wakeFull && m < sunsetStart) {
      bNorm = 1.0;
    } else if (m >= sunsetStart && m < fadeOffStart) {
      const p = (m - sunsetStart) / (fadeOffStart - sunsetStart);
      bNorm = 1.0 - 0.55 * p; // slowly sets towards 10 PM
    } else if (m >= fadeOffStart && m < bedtimeOff) {
      bNorm = 0.45 * (1.0 - (m - fadeOffStart) / (bedtimeOff - fadeOffStart));
    }
    const y = h - 24 - (bNorm * (h - 40));
    if (x === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();

  // Current time scrubber line
  const curX = (state.minute / 1440) * w;
  ctx.strokeStyle = "#58a6ff";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(curX, 0);
  ctx.lineTo(curX, h);
  ctx.stroke();
}

fetchState();

// Auto tick when in Live mode
setInterval(() => {
  if (state.live) {
    fetch('/api/state')
      .then(r => r.json())
      .then(data => {
        state.minute = data.minute;
        state.sunset_minute = data.sunset_minute;
        slider.value = state.minute;
        updateUI(false);
      });
  }
}, 15000);
</script>
</body>
</html>
"""

class CircadianHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence verbose request logging
        pass

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        qs = parse_qs(url.query)

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
            return

        if path == "/api/state":
            lb_now = circadian_engine.get_long_beach_now()
            cur_lb_min = lb_now.hour * 60 + lb_now.minute
            sunset_m = circadian_engine.calculate_sunset_minute(lb_now)
            ex = circadian_engine.get_exemption()
            with state_lock:
                if state["live_mode"]:
                    state["selected_minute"] = cur_lb_min
                state["sunset_minute"] = sunset_m
                resp = {
                    "minute": state["selected_minute"],
                    "live": state["live_mode"],
                    "is_weekend": state["is_weekend"],
                    "sunny": state["weather_sunny"],
                    "weather_state": state["weather_state"],
                    "weather_override": state["weather_override"],
                    "sunset_minute": sunset_m,
                    "exemption": ex
                }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        if path == "/api/exemption":
            action = qs.get("action", ["get"])[0]
            if action == "clear":
                circadian_engine.clear_exemption()
                resp = {"status": "ok", "exemption": None}
            elif action == "set":
                sched = qs.get("schedule", [None])[0]
                preset = qs.get("preset", [None])[0]
                power = qs.get("power", [None])[0]
                hold_min = qs.get("hold_minutes", [None])[0]
                exp = None
                if hold_min:
                    try:
                        from datetime import timedelta, timezone
                        exp = datetime.now(timezone.utc) + timedelta(minutes=float(hold_min))
                    except Exception:
                        pass
                ex = circadian_engine.set_exemption(schedule=sched, preset=preset, power=power, expires_at=exp)
                resp = {"status": "ok", "exemption": ex}
            else:
                resp = {"status": "ok", "exemption": circadian_engine.get_exemption()}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        if path == "/api/go_now":
            lb_now = circadian_engine.get_long_beach_now()
            cur_lb_min = lb_now.hour * 60 + lb_now.minute
            sunset_m = circadian_engine.calculate_sunset_minute(lb_now)
            with state_lock:
                state["live_mode"] = True
                state["selected_minute"] = cur_lb_min
                state["sunset_minute"] = sunset_m
                weekend = state["is_weekend"]
                if not state["weather_override"]:
                    state["weather_state"] = circadian_engine.parse_weather_state(circadian_engine.get_weather_condition())
                    state["weather_sunny"] = state["weather_state"] in ("sunny", "partly_cloudy")
                wstate = state["weather_state"]
            
            # Immediately send to beam with smooth 2500ms transition
            zones = circadian_engine.calculate_beam_zones(cur_lb_min, is_weekend=weekend, weather_state=wstate, sunset_min=sunset_m)
            threading.Thread(target=send_to_beam, args=(zones, 2500), daemon=True).start()

            resp = {"status": "ok", "minute": cur_lb_min, "sunset_minute": sunset_m}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        if path == "/api/power_off":
            with state_lock:
                state["live_mode"] = False
            threading.Thread(target=circadian_engine.fade_to_black_wipe, kwargs={"duration_sec": 2.0}, daemon=True).start()
            resp = {"status": "ok", "action": "power_off"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        if path == "/api/zones":
            minute = int(qs.get("minute", [str(state["selected_minute"])])[0])
            weekend = qs.get("weekend", ["1" if state["is_weekend"] else "0"])[0] == "1"
            # Optional explicit weather state override (sunny|partly_cloudy|cloudy|rain);
            # falls back to the legacy sunny boolean, then to current server state.
            wstate = qs.get("weather", [None])[0]
            if wstate not in circadian_engine.WEATHER_KELVIN_SHIFT:
                if qs.get("sunny"):
                    wstate = "sunny" if qs["sunny"][0] == "1" else "cloudy"
                else:
                    wstate = state["weather_state"]
            do_send = qs.get("send", ["0"])[0] == "1"

            lb_now = circadian_engine.get_long_beach_now()
            sunset_m = circadian_engine.calculate_sunset_minute(lb_now)
            zones = circadian_engine.calculate_beam_zones(minute, is_weekend=weekend, weather_state=wstate, sunset_min=sunset_m)
            rgb_zones = [circadian_engine.hsbk_to_rgb(*z) for z in zones]

            # Real-time hardware update with smooth 2000ms transition
            if do_send:
                now_ts = time.time()
                with state_lock:
                    state["selected_minute"] = minute
                    state["is_weekend"] = weekend
                    state["weather_state"] = wstate
                    state["weather_sunny"] = wstate in ("sunny", "partly_cloudy")
                    state["weather_override"] = True
                    last_ts = state["last_applied_ts"]
                    # Allow responsive rate of up to ~4 updates/sec while dragging
                    if (now_ts - last_ts) >= 0.22 or minute != state["last_applied_minute"]:
                        state["last_applied_ts"] = now_ts
                        state["last_applied_minute"] = minute
                        # Design Rule: Always smooth transition (2000ms)
                        threading.Thread(target=send_to_beam, args=(zones, 2000), daemon=True).start()

            z0 = zones[0]
            z37 = zones[37]
            z38 = zones[38]
            z39 = zones[39]
            status_desc = f"Bottom: {z0[3]}K (Bri {int(z0[2]/655.35)}%) · Z38 (Sep): {'OFF' if z38[2]==0 else f'Bri {int(z38[2]/655.35)}%'} · Z39 (Top): {'OFF' if z39[2]==0 else f'{z39[3]}K (Bri {int(z39[2]/655.35)}%)'}"

            resp = {
                "minute": minute,
                "rgb_zones": rgb_zones,
                "status": status_desc
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

def run_server(port=8850):
    t = threading.Thread(target=live_clock_worker, daemon=True)
    t.start()
    server = HTTPServer(("0.0.0.0", port), CircadianHandler)
    print(f"[circadian_server] Running on http://0.0.0.0:{port} (local: http://localhost:{port})")
    server.serve_forever()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8850)
    args = parser.parse_args()
    run_server(args.port)
