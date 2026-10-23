#!/usr/bin/env python3
"""
circadian_engine.py — Deterministic Circadian Curve and Multi-Zone LIFX Beam Generator.

Features & Design Rules:
  - 40 zones total (0 = bottom, 39 = top)
  - All hardware transitions are smooth and magical (multi-second transitions, never instant or jarring).
  - Timezone locked to Long Beach, CA (America/Los_Angeles, handles DST automatically, unaffected by travel).
  - Solar sunset calculation: Sunset dynamically calculated for Long Beach (lat 33.7701, lon -118.1937).
  - Morning schedule:
      * Extended sunrise waking curve (06:00 pre-glow, 06:15 alarm ramp, 07:45 full solid warm light on weekdays; 07:00 / 07:15 / 09:00 on weekends — gradient reaches full height 08:15, brightness ramps to full only at 09:00).
      * Weather indicator is a single pixel (zone 39) with a black separator pixel (zone 38) below it.
      * Weather indicator turns off by 10:00 AM, transitioning into full light (or weekday power save).
      * Weekdays: turns off at 10:00 AM to save power, turns back on at 16:30 (4:30 PM).
      * Weekends: remains on all day with full warm ambient light; from 15:30 a warm-down glides 3600K -> 3000K into golden hour (Garrett, 2026-10-10), then the normal evening red-shift and bedtime fade.
  - Evening schedule:
      * Sunset transition: Freezes the beloved amber-glow gradient (red-orange at bottom zone 0 to warm amber at zone 37).
      * Starting around actual solar sunset (~18:35 today in Long Beach), the gradient slowly deepens towards 22:00 (10:00 PM).
      * As 10 PM nears, color shifts to mostly rich red-orange, and top pixels progressively fade off so the light virtually sets downward.
      * At 22:00 (10 PM), the remaining bottom embers smoothly fade off to sleep by 22:30.
"""

import os
import sys
import json
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LONG_BEACH_TZ = os.environ.get("CIRCADIAN_TZ", "America/Los_Angeles")
LONG_BEACH_LAT = float(os.environ.get("CIRCADIAN_LAT", "33.7701"))
LONG_BEACH_LON = float(os.environ.get("CIRCADIAN_LON", "-118.1937"))

# Target fixture — override via env vars or CLI flags
DEFAULT_IP = os.environ.get("LIFX_BEAM_IP", "192.168.1.100")
DEFAULT_MAC = os.environ.get("LIFX_BEAM_MAC", "d0:73:d5:00:00:00")
WEATHER_CACHE = "/tmp/mb_weather.json"
EXEMPTION_FILE = "/tmp/lifx_exemption.json"

# Design Rule: Default transition time in ms for smooth, magical lighting changes
DEFAULT_TRANSITION_MS = 3000

def get_exemption():
    """
    Load active exemption if valid for today in Long Beach time.
    Format:
      {
        "date": "YYYY-MM-DD",
        "schedule": "weekday" | "weekend" | None,
        "preset": "reading" | "amber-glow" | "gradient-hold" | None,
        "power": "on" | "off" | None,
        "expires_at": ISO8601 string | None,
        "zones": [(h,s,b,k), ...] | None,
        "time_shift_minutes": int | None
      }
    """
    if not os.path.exists(EXEMPTION_FILE):
        return None
    try:
        with open(EXEMPTION_FILE, "r") as f:
            data = json.load(f)
        
        # Check explicit expiration timestamp first if present
        if data.get("expires_at"):
            try:
                exp_dt = datetime.fromisoformat(data["expires_at"])
                if datetime.now(timezone.utc) >= exp_dt:
                    clear_exemption()
                    return None
            except Exception:
                pass

        # A FROZEN exemption survives midnight: it holds the captured zones
        # across days until it expires (checked above) or is unfrozen.
        if data.get("frozen"):
            return data

        today_str = get_long_beach_now().strftime("%Y-%m-%d")
        if data.get("date") == today_str:
            return data
        else:
            # Stale exemption from previous day -> remove
            try:
                os.remove(EXEMPTION_FILE)
            except OSError:
                pass
            return None
    except Exception:
        return None

def set_exemption(schedule=None, preset=None, power=None, expires_at=None, zones=None, frozen=False, time_shift_minutes=None):
    """Save an exemption for today (or a multi-day freeze if frozen=True)."""
    today_str = get_long_beach_now().strftime("%Y-%m-%d")
    data = {
        "date": today_str,
        "schedule": schedule,
        "preset": preset,
        "power": power,
        "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at,
        "zones": zones,
        "frozen": bool(frozen),
        "time_shift_minutes": time_shift_minutes,
        "updated_at": get_long_beach_now().isoformat()
    }
    with open(EXEMPTION_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return data

def freeze_current_lighting(expires_at=None):
    """
    FREEZE: capture the beam's current lighting (whatever the engine would
    display right now, including any active preset exemption) and hold it.

    The frozen zones are stored as an explicit zone list in a frozen
    exemption. Unlike daily exemptions, a freeze survives midnight — the
    daemon keeps replaying the exact same colors on every tick until the
    freeze expires (expires_at) or unfreeze() is called. At that point the
    normal circadian schedule resumes automatically on the next tick.
    """
    lb_now = get_long_beach_now()
    cur_min = lb_now.hour * 60 + lb_now.minute
    is_weekend = lb_now.weekday() >= 5
    cond = get_weather_condition()
    zones = calculate_beam_zones(cur_min, is_weekend=is_weekend, weather_state=parse_weather_state(cond))
    return set_exemption(zones=[list(z) for z in zones], frozen=True, expires_at=expires_at)

def unfreeze():
    """RELEASE: drop a frozen hold; the normal schedule resumes on next tick."""
    clear_exemption()

def clear_exemption():
    """Clear active exemption."""
    if os.path.exists(EXEMPTION_FILE):
        try:
            os.remove(EXEMPTION_FILE)
        except OSError:
            pass


def next_transition_minute(now=None):
    """
    Return the next minute-of-day (0..1439) at which the schedule's lit/dark
    state CHANGES, strictly after `now`.

    Scans the live schedule forward (wrapping into the next day). This is the
    canonical hold-expiry target: a manual hold expires at the next scheduled
    schedule change, so the normal circadian schedule ALWAYS resumes there —
    even if the hold was requested past midnight.
    """
    if now is None:
        now = get_long_beach_now()
    start_min = now.hour * 60 + now.minute
    cond = get_weather_condition()
    wstate = parse_weather_state(cond)

    def is_lit(candidate):
        cand_min = candidate.hour * 60 + candidate.minute
        cand_weekend = candidate.weekday() >= 5
        sunset_m = calculate_sunset_minute(candidate)
        zones = calculate_beam_zones(
            cand_min, is_weekend=cand_weekend,
            weather_state=wstate, sunset_min=sunset_m,
            respect_exemption=False,
        )
        return sum(z[2] for z in zones) > 0

    was_lit = is_lit(now)
    for offset in range(1, 1441):
        candidate = now + timedelta(minutes=offset)
        if is_lit(candidate) != was_lit:
            return candidate.hour * 60 + candidate.minute
    # Schedule never changes (shouldn't happen) — hold 12h as safe fallback.
    return (start_min + 720) % 1440


def hold_until_next_schedule(power=None, preset=None):
    """
    Atomically set a hold (power='off' or a preset) that expires at the next
    scheduled lit/dark transition, so the normal circadian schedule resumes
    on its own. Returns (exemption, expiry_local_dt).
    """
    lb_now = get_long_beach_now()
    expiry_min = next_transition_minute(lb_now)
    expiry_dt = lb_now.replace(
        hour=expiry_min // 60, minute=expiry_min % 60,
        second=0, microsecond=0,
    )
    if expiry_min <= lb_now.hour * 60 + lb_now.minute:
        expiry_dt += timedelta(days=1)  # wrapped into the next day
    data = set_exemption(power=power, preset=preset, expires_at=expiry_dt.astimezone(timezone.utc))
    return data, expiry_dt


def get_long_beach_now():
    """Always return current datetime in Long Beach, CA timezone."""
    return datetime.now(ZoneInfo(LONG_BEACH_TZ))

def calculate_sunset_minute(dt=None, lat=LONG_BEACH_LAT, lon=LONG_BEACH_LON, tz_name=LONG_BEACH_TZ):
    """
    Computes exact astronomical sunset for Long Beach, CA in minutes from midnight (0..1439).
    Accounts for day of year, solar declination, equation of time, and local daylight saving time.
    """
    if dt is None:
        dt = datetime.now(ZoneInfo(tz_name))
    day_of_year = dt.timetuple().tm_yday
    zenith = 90.833  # Official solar zenith for sunset (90°50')
    rad = math.pi / 180.0
    deg = 180.0 / math.pi
    
    lng_hour = lon / 15.0
    t = day_of_year + ((18.0 - lng_hour) / 24.0)
    M = (0.9856 * t) - 3.289
    L = (M + (1.916 * math.sin(M * rad)) + (0.020 * math.sin(2 * M * rad)) + 282.634) % 360
    RA = (deg * math.atan(0.91764 * math.tan(L * rad))) % 360
    Lquadrant = (math.floor(L / 90.0)) * 90.0
    RAquadrant = (math.floor(RA / 90.0)) * 90.0
    RA = (RA + (Lquadrant - RAquadrant)) / 15.0
    sinDec = 0.39782 * math.sin(L * rad)
    cosDec = math.cos(math.asin(sinDec))
    cosH = (math.cos(zenith * rad) - (sinDec * math.sin(lat * rad))) / (cosDec * math.cos(lat * rad))
    if cosH > 1:
        return 18 * 60 + 30  # fallback
    if cosH < -1:
        return 18 * 60 + 30
    H = (deg * math.acos(cosH)) / 15.0
    T = H + RA - (0.06571 * t) - 6.622
    UT = (T - lng_hour) % 24.0
    
    hours = int(UT)
    minutes = int(round((UT % 1) * 60))
    if minutes == 60:
        hours = (hours + 1) % 24
        minutes = 0
    utc_dt = datetime(dt.year, dt.month, dt.day, hours, minutes, tzinfo=timezone.utc)
    local_dt = utc_dt.astimezone(ZoneInfo(tz_name))
    return local_dt.hour * 60 + local_dt.minute

def get_weather_condition():
    """Reads NOAA cached forecast from /tmp/mb_weather.json."""
    if not os.path.exists(WEATHER_CACHE):
        return "Clear"
    try:
        with open(WEATHER_CACHE, "r") as f:
            data = json.load(f)
        forecasts = data.get("forecast", [])
        if forecasts:
            return forecasts[0].get("short_forecast", "Clear")
        current = data.get("current", {})
        return current.get("description", "Clear")
    except Exception:
        return "Clear"

def parse_weather_state(condition_str):
    """
    Parses weather string into one of: 'sunny', 'partly_cloudy', 'cloudy', 'rain'.
    """
    if not condition_str:
        return "sunny"
    s = condition_str.lower()
    if any(k in s for k in ["rain", "shower", "drizzle", "storm", "thunderstorm", "mist", "witsy"]):
        return "rain"
    if any(k in s for k in ["partly cloudy", "partly sunny", "scattered clouds", "few clouds", "broken clouds", "mostly sunny"]):
        return "partly_cloudy"
    if any(k in s for k in ["cloudy", "overcast", "fog", "haze"]):
        return "cloudy"
    if any(k in s for k in ["sunny", "clear"]):
        return "sunny"
    return "sunny"

def is_sunny(condition_str):
    state = parse_weather_state(condition_str)
    return state in ("sunny", "partly_cloudy")

# ---------------------------------------------------------------------------
# Weather-driven daytime warm shift (added 2026-10-10 at Garrett's request):
# On cloudy or rainy days the daytime warm-white segments shift a bit warmer
# (lower Kelvin) for a cozier, overcast-matching mood. Offsets in Kelvin;
# negative = warmer. Applied ONLY to daytime segments (sunrise ramp tail,
# morning ambient, afternoon). Sunrise gradient, golden hour and evening are
# untouched — they already run warm.
# Tunable: tweak these values and restart circadian_server.py / run --apply.
# ---------------------------------------------------------------------------
WEATHER_KELVIN_SHIFT = {
    "sunny": 0,
    "partly_cloudy": -150,
    "cloudy": -300,
    "rain": -450,
}

def daytime_kelvin_shift(weather_state):
    return WEATHER_KELVIN_SHIFT.get(weather_state, 0)

def lerp(a, b, t):
    t = max(0.0, min(1.0, t))
    return a + (b - a) * t

def lerp_color(c1, c2, t):
    # c = (h, s, b, k)
    return (
        int(lerp(c1[0], c2[0], t)),
        int(lerp(c1[1], c2[1], t)),
        int(lerp(c1[2], c2[2], t)),
        int(lerp(c1[3], c2[3], t)),
    )

def get_reading_light_zones(num_zones=40):
    """
    Reading light preset:
    - Top zones (30-39): Gradient from red (zone 30) to warm orange (zone 39)
    - Middle zones (8-29): Black / turned off
    - Bottom zones (0-7): Subtle red embers fading off towards the bottom
    """
    zones = [(0, 0, 0, 2500)] * num_zones

    # Bottom embers (zones 0..7): Deep red embers fading downwards
    # Zone 7 is brightest ember, Zone 0 fades down
    for z in range(8):
        frac = (z + 1) / 8.0
        h = 1000  # deep red
        s = 65535
        b = int(22000 * frac)
        k = 2500
        zones[z] = (h, s, b, k)

    # Middle zones (8..29) stay completely black:
    for z in range(8, 30):
        zones[z] = (0, 0, 0, 2500)

    # Top zones (30..39): Red to warm orange gradient
    # Zone 30: red (hue ~1200), Zone 39: warm orange (hue ~7000)
    for z in range(30, num_zones):
        frac = (z - 30) / float(num_zones - 1 - 30)
        h = int(lerp(1200, 7500, frac))
        s = 65535
        b = int(lerp(38000, 48000, frac))
        k = 2700
        zones[z] = (h, s, b, k)

    return zones

def get_nightlight_zones(num_zones=40):
    """
    Nightlight preset:
    - Half the beam lit (bottom 20 zones: 0..19).
    - Top half (zones 20..39) completely off / black.
    - Bottom few pixels (zones 0..2) start in warm golden amber/yellow (hue 8000 -> 4500).
    - Transitions smoothly into rich crimson red across zones 3..19 (hue 2500 -> 400).
    - Brightness: 50% (32768), saturation 65535.
    """
    active_zones = 20
    zones = [(0, 0, 0, 2500)] * num_zones

    for z in range(active_zones):
        if z < 3:
            h = int(8000 - z * 1750)
        else:
            frac = (z - 3) / float(active_zones - 1 - 3)
            h = int(2500 * (1.0 - frac) + 400 * frac)
        zones[z] = (h, 65535, 32768, 2500)

    return zones

def get_amber_glow_zones(num_zones=40):
    """
    Classic amber-glow gradient across active zones:
    Warm yellow/amber at the bottom base, smoothly ascending into rich warm orange and red-orange.
    Base brightness ~42000 (same brightness as evening plateau).
    """
    bottom_h = 7500  # warm amber/gold
    top_h = 1800     # rich red-orange
    sat = 65535
    base_b = 42000
    kelvin = 2500
    active_zones = 28

    zones = []
    for z in range(num_zones):
        if z <= active_zones:
            frac = (z / float(max(1.0, active_zones))) ** 0.8
            h = int(lerp(bottom_h, top_h, frac))
            zones.append((h, sat, base_b, kelvin))
        else:
            zones.append((0, 0, 0, 2500))
    return zones

def calculate_beam_zones(minute_of_day, is_weekend=False, weather_sunny=True, num_zones=40, sunset_min=None, weather_state=None, respect_exemption=True):
    """
    Given minute of day (0..1439), weekend boolean, and weather status,
    returns a list of 40 (h, s, b, k) tuples for the LIFX Beam.
    Zones: 0 is bottom, 39 is top.

    weather_state: 'sunny' | 'partly_cloudy' | 'cloudy' | 'rain' — drives the
    weather indicator pixel AND the daytime warm-Kelvin shift. If omitted, it
    is derived from the legacy weather_sunny boolean (True->sunny, False->cloudy).
    """
    # --- Weather state / daytime warm shift (2026-10-10) ---
    if weather_state is None:
        weather_state = "sunny" if weather_sunny else "cloudy"
    weather_sunny = weather_state in ("sunny", "partly_cloudy")
    wshift = daytime_kelvin_shift(weather_state)

    # Check for active exemptions.
    # respect_exemption=False simulates the PURE schedule (used by hold-expiry
    # scanning) — an active hold must never poison the schedule simulation.
    ex = get_exemption() if respect_exemption else None
    time_shift = 0
    if ex:
        if ex.get("power") == "off":
            return [(0, 0, 0, 2500) for _ in range(num_zones)]
        if ex.get("preset") == "reading":
            return get_reading_light_zones(num_zones)
        if ex.get("preset") == "nightlight":
            return get_nightlight_zones(num_zones)
        if ex.get("preset") == "amber-glow":
            return get_amber_glow_zones(num_zones)
        if ex.get("zones"):
            return [tuple(z) for z in ex["zones"]]
        if ex.get("schedule") == "weekday":
            is_weekend = False
        elif ex.get("schedule") == "weekend":
            is_weekend = True
        if ex.get("time_shift_minutes"):
            time_shift = int(ex["time_shift_minutes"])

    m = (minute_of_day + time_shift) % 1440
    
    if sunset_min is None:
        try:
            sunset_min = calculate_sunset_minute()
        except Exception:
            sunset_min = 18 * 60 + 35  # ~18:35 PDT fallback

    # Schedule milestones:
    # Extended sunrise waking curve: lots of reds & oranges waking up,
    # rising in height and brightness across zones as a rich gradient.
    # On weekdays:
    #   * 06:00 (360) pre-glow red embers
    #   * 06:15 (375) sunrise rise begins
    #   * 07:15 (435) full beam height reached in full rich sunrise gradient!
    #   * 07:15 -> 07:45 ramps brighter and shifts from gradient into solid warm morning light (~3000K)
    #   * 07:45 (465) fully solid warm light
    # Power-save daytime off:
    # Weekdays: Turns off at 10:00 AM (minute 600) to save power, turns back on at 16:30 (minute 990 / 4:30 PM).
    # Weekends: Remains on all day with full warm ambient light.
    if not is_weekend:
        t_wake_start = 6 * 60          # 06:00 (360) pre-glow red embers
        t_alarm_start = 6 * 60 + 15    # 06:15 (375) sunrise gradient begins rising
        t_grad_full = 7 * 60 + 15      # 07:15 (435) full height in rich sunrise gradient
        t_wake_full = 7 * 60 + 45      # 07:45 (465) fully solid warm light
        t_work_off = 10 * 60           # 10:00 (600) weekday daytime power save off
        t_work_on = 16 * 60 + 30       # 16:30 (990) weekday afternoon resume on
        t_midday_warm = 16 * 60 + 30   # resumes directly into afternoon/pre-sunset light
        t_weekend_warm = 0             # unused on weekdays
    else:
        # Weekend: shifted sunrise schedule
        t_wake_start = 7 * 60          # 07:00 (420) pre-glow red embers
        t_alarm_start = 7 * 60 + 15    # 07:15 (435) sunrise gradient begins rising
        t_grad_full = 8 * 60 + 15      # 08:15 (495) full height in rich sunrise gradient
        t_wake_full = 9 * 60           # 09:00 (540) fully solid warm light
        t_work_off = 0
        t_work_on = 0
        t_midday_warm = 10 * 60        # 10:00 (600) fully solid warm/ambient light throughout the day
        t_weekend_warm = 15 * 60 + 30  # 15:30 (930) weekend warm-down begins (Garrett, 2026-10-10)

    # Weather pixel window:
    # Weather indicator turns off by 10:00 AM for both weekdays and weekends
    t_weather_start = (7 * 60) if not is_weekend else (7 * 60 + 40)
    t_weather_end = 10 * 60            # 10:00 AM (weather pixel turns off by 10 AM)

    # Evening & Sunset schedule:
    # Golden hour begins ~45 min before astronomical sunset
    t_sunset_start = max(t_midday_warm + 60, sunset_min - 45)
    t_sunset_peak = sunset_min         # Astronomical sunset
    t_bedtime_start = 21 * 60 + 45      # 21:45 (9:45 PM) bedtime red transition starts
    t_bedtime_off = 22 * 60 + 15       # 22:15 (10:15 PM completely off)

    zones = []

    # 1. Night / Off (after 21:30 or before wake_start)
    if m < t_wake_start or m >= t_bedtime_off:
        return [(0, 0, 0, 2500) for _ in range(num_zones)]

    # 2. Pre-glow: t_wake_start -> t_alarm_start (06:00 -> 06:15)
    # Deep red embers glowing subtly at the bottom
    elif t_wake_start <= m < t_alarm_start:
        prog = (m - t_wake_start) / float(t_alarm_start - t_wake_start)
        active_zones = int(lerp(3, 10, prog))
        peak_b = int(lerp(1500, 10000, prog))
        for z in range(num_zones):
            if z <= active_zones:
                zone_prog = z / float(active_zones) if active_zones > 0 else 0
                # Deep pure red (Hue 0-500) at bottom, rising to red-orange (Hue ~2000)
                h = int(lerp(0, 2000, zone_prog))
                b = int(peak_b * (1.0 - 0.7 * zone_prog))
                zones.append((h, 65535, b, 2500))
            else:
                zones.append((0, 0, 0, 2500))

    # 3. Extended Sunrise Rise & Gradient: t_alarm_start -> t_wake_full (06:15 -> 07:45)
    # Phase A (06:15 -> 07:15): Starts deep red/orange at base, climbs to full beam height (zone 37)
    # by 07:15 while displaying the full, rich sunrise gradient (golden yellow/amber base rising into deep red).
    # Phase B (07:15 -> 07:45): Full beam stays completely lit, ramps brighter, and shifts from gradient
    # into solid warm morning ambient light (~3000K) by 07:45.
    if t_alarm_start <= m < t_wake_full:
        if m < t_grad_full:
            # Phase A: 06:15 -> 07:15 (Sunrise height rise to full beam gradient)
            prog = (m - t_alarm_start) / float(t_grad_full - t_alarm_start)

            # Beam height rises steadily from 10 zones to full height (zone 37) by 07:15
            active_zones = int(lerp(10, num_zones - 3, prog))
            base_bright = int(lerp(10000, 42000, prog))

            # Full rich gradient: warm orange/gold at base, rising up to rich red
            bottom_h = int(lerp(4000, 7500, prog))       # Bottom: warm orange/gold
            top_h = int(lerp(300, 1000, prog))           # Top: deep red
            sat = 65535                                  # Full saturation for the gradient
            kelvin = int(lerp(2500, 2700, prog))

            for z in range(num_zones):
                if z <= active_zones:
                    zone_frac = z / float(active_zones) if active_zones > 0 else 0
                    h = int(lerp(bottom_h, top_h, zone_frac))
                    b = int(base_bright * (1.0 - 0.25 * (1.0 - prog) * zone_frac))
                    zones.append((h, sat, b, kelvin))
                else:
                    zones.append((0, 0, 0, 2500))
        else:
            # Phase B: 07:15 -> 07:45 (Full gradient -> full bright warm light)
            prog = (m - t_grad_full) / float(t_wake_full - t_grad_full)

            # All zones lit up to zone 37
            active_zones = num_zones - 3
            base_bright = int(lerp(42000, 52000, prog))

            # Gradient morphs into uniform warm light (Hue ~8000, ~3000K)
            bottom_h = int(lerp(7500, 8000, prog))
            top_h = int(lerp(1000, 8000, prog ** 0.8))   # Top catches up to warm yellow
            sat = int(lerp(65535, 12000, prog ** 1.5))   # Desaturates from vibrant colors to warm white
            kelvin = max(2500, int(lerp(2700, 3000, prog)) + wshift)

            for z in range(num_zones):
                if z <= active_zones:
                    zone_frac = z / float(active_zones) if active_zones > 0 else 0
                    h = int(lerp(bottom_h, top_h, zone_frac))
                    # Uniform brightness across the beam
                    b = base_bright
                    zones.append((h, sat, b, kelvin))
                else:
                    zones.append((0, 0, 0, 2500))

    # 4. Morning warm light:
    # On weekdays: t_wake_full -> t_work_off (07:45 -> 10:00)
    # On weekends: t_wake_full -> t_midday_warm (09:00 -> 10:00)
    elif not is_weekend and t_wake_full <= m < t_work_off:
        prog = (m - t_wake_full) / float(max(1, t_work_off - t_wake_full))
        h = 8000
        s = int(lerp(12000, 3000, prog))
        b = 52000
        k = max(2500, int(lerp(3000, 3600, prog)) + wshift)
        zones = [(h, s, b, k) for _ in range(num_zones)]

    elif is_weekend and t_wake_full <= m < t_midday_warm:
        prog = (m - t_wake_full) / float(max(1, t_midday_warm - t_wake_full))
        h = 8000
        s = int(lerp(12000, 3000, prog))
        b = 52000
        k = max(2500, int(lerp(3000, 3600, prog)) + wshift)
        zones = [(h, s, b, k) for _ in range(num_zones)]

    # 4b. Weekday daytime power save off: 10:00 AM -> 16:30 (4:30 PM)
    elif not is_weekend and t_work_off <= m < t_work_on:
        return [(0, 0, 0, 2500) for _ in range(num_zones)]

    # 5. Full daytime / Afternoon:
    # Weekdays: anchor the pre-sunset curve baseline identically to 16:30 so the
    # evening progression towards sunset and bedtime is 100% identical.
    elif not is_weekend and t_work_on <= m < t_sunset_start:
        t_start_aft = 16 * 60 + 30
        prog = (m - t_start_aft) / float(max(1, t_sunset_start - t_start_aft))
        t_peak = (t_start_aft + t_sunset_start) / 2.0
        if m < t_peak:
            sub_prog = (m - t_start_aft) / float(max(1, t_peak - t_start_aft))
            k = max(2500, int(lerp(3600, 3800, sub_prog)) + wshift)
        else:
            sub_prog = (m - t_peak) / float(max(1, t_sunset_start - t_peak))
            k = max(2500, int(lerp(3800, 3300, sub_prog)) + wshift)
        s = int(lerp(2000, 6000, prog))
        b = 55000
        zones = [(8500, s, b, k) for _ in range(num_zones)]

    # 5a. Weekend midday hold: 10:00 -> 15:30 solid warm ambient (no cool-up bump).
    elif is_weekend and t_midday_warm <= m < t_weekend_warm:
        zones = [(8500, 2000, 55000, max(2500, 3600 + wshift)) for _ in range(num_zones)]

    # 5b. Weekend afternoon warm-down: starts 15:30 (Garrett, 2026-10-10:
    # "for weekends, start the warmer color tint change around 3:30 PM — stay
    # warm for the next few hours, then the red night shift before bed").
    # Kelvin glides 3600 -> 3000 into golden hour (intro starts at 3000K on
    # weekends for continuity), saturation rises, brightness eases 55000 -> 50000.
    elif is_weekend and t_weekend_warm <= m < t_sunset_start:
        prog = (m - t_weekend_warm) / float(max(1, t_sunset_start - t_weekend_warm))
        k = max(2500, int(lerp(3600, 3000, prog)) + wshift)
        s = int(lerp(2000, 6000, prog))
        b = int(lerp(55000, 50000, prog))
        zones = [(8500, s, b, k) for _ in range(num_zones)]

    # 6. Evening Sunset: t_sunset_start -> t_bedtime_start (Sunset -> 22:00 / 10 PM)
    # Garrett's sunset rule: Yellow/amber should ALWAYS be on the bottom (like a sun setting slowly),
    # transitioning into rich orange and red above, with upper LEDs progressively unlit.
    # Also dimmed / fewer LEDs lit to keep comfortable without harshness or flicker.
    elif t_sunset_start <= m < t_bedtime_start:
        if m < t_sunset_peak:
            intro_prog = (m - t_sunset_start) / float(max(1, t_sunset_peak - t_sunset_start))
            # Bottom: warm yellow/amber (~8500)
            bottom_h = int(lerp(8500, 8000, intro_prog))
            # Top: transitions from amber to deep orange (~3000)
            top_h = int(lerp(8000, 3000, intro_prog))
            sat = int(lerp(20000, 65535, intro_prog))
            bright = int(lerp(50000, 42000, intro_prog))
            # Continuity with the afternoon branch: weekdays arrive at 3300K,
            # weekends glide in from the 15:30 warm-down at 3000K.
            intro_k_start = 3000 if is_weekend else 3300
            kelvin = max(2500, int(lerp(intro_k_start, 2700, intro_prog)) + wshift)  # continue weather warm shift smoothly into golden hour
            # Fewer LEDs lit as sunset approaches (dimming by unlighting upper zones)
            active_zones = lerp(float(num_zones - 2), 24.0, intro_prog)
            zones = []
            for z in range(num_zones):
                if z <= active_zones:
                    frac = z / float(max(1.0, active_zones))
                    h = int(lerp(bottom_h, top_h, frac))
                    # Full brightness on lit LEDs (no PWM flicker dimming)
                    zones.append((h, sat, bright, kelvin))
                else:
                    zones.append((0, 0, 0, 2500))
        else:
            # Past sunset towards bedtime (now anchored at 21:45 / 9:45 PM):
            # Keep the profile steady across the 20:00 - 21:45 wind-down window:
            # Yellow/amber at base (~6800), quickly ascending into red-orange and deep red higher up.
            # Holds stable through 20:00-21:45 ("stays like this for bedtime like 8-945"),
            # and then shifts predominantly red starting at 21:45.
            t_evening_plateau_start = 20 * 60  # 20:00 (8:00 PM)
            
            if m < t_evening_plateau_start:
                # 18:33 -> 20:00 transition from post-sunset to the 8:00 PM wind-down look
                prog_to_plateau = (m - t_sunset_peak) / float(max(1, t_evening_plateau_start - t_sunset_peak))
                bottom_h = int(lerp(8000, 7500, prog_to_plateau))
                top_h = int(lerp(3000, 2000, prog_to_plateau))
                active_zones = lerp(24.0, 30.0, prog_to_plateau)
            else:
                # 20:00 -> 21:45: Holds essentially steady with minimal drift so it stays looking like this
                # More lit across the beam (~28-30 zones / ~3.5-4 sections) until closer to 9:00 PM (21:00),
                # then smoothly recedes toward 16 zones as it nears bedtime transition.
                t_evening_full_hold = 21 * 60      # 21:00 (9:00 PM)
                if m < t_evening_full_hold:
                    # 20:00 -> 21:00: Keep ~28-30 zones lit with warm amber base climbing to warm red-orange
                    hold_prog = (m - t_evening_plateau_start) / float(max(1, t_evening_full_hold - t_evening_plateau_start))
                    bottom_h = int(lerp(7500, 7000, hold_prog))
                    top_h = int(lerp(2000, 1500, hold_prog))
                    active_zones = lerp(30.0, 26.0, hold_prog)
                else:
                    # 21:00 -> 21:45: Gradually shift into red dominance and taper from 26 down to 16 zones approaching bedtime
                    taper_prog = (m - t_evening_full_hold) / float(max(1, t_bedtime_start - t_evening_full_hold))
                    bottom_h = int(lerp(7000, 5500, taper_prog))
                    top_h = int(lerp(1500, 700, taper_prog))
                    active_zones = lerp(26.0, 16.0, taper_prog)

            sat = 65535
            kelvin = 2500
            base_b = 42000

            zones = []
            for z in range(num_zones):
                if z <= int(active_zones):
                    # Power curve on zone fraction so warm amber/orange stays prominent
                    # while smoothly graduating into red-orange near top
                    frac = (z / float(max(1.0, int(active_zones)))) ** 0.8
                    h = int(lerp(bottom_h, top_h, frac))
                    zones.append((h, sat, base_b, kelvin))
                else:
                    zones.append((0, 0, 0, 2500))

    # 7. Bedtime fade-off: 21:45 (9:45 PM) -> 22:15 (10:15 PM)
    # Goes deeply red starting at 9:45 PM, settling into pure deep red embers and fading to darkness
    elif t_bedtime_start <= m < t_bedtime_off:
        prog = (m - t_bedtime_start) / float(t_bedtime_off - t_bedtime_start)
        # Shift bottom and top deeply into red, and virtual setting recedes to zone 0
        active_zones = lerp(14.0, 0.0, prog)
        b = int(lerp(36000, 0, prog))
        if b <= 400:
            return [(0, 0, 0, 2500) for _ in range(num_zones)]
        zones = []
        for z in range(num_zones):
            if z <= active_zones:
                fade_tail = 1.0 if (active_zones - z) >= 1.0 else max(0.0, active_zones - z)
                frac = z / float(max(1.0, active_zones - 1.0))
                # Shifts from deep red-orange down to pure crimson red (Hue 200 - 800)
                cur_bottom_h = int(lerp(5000, 200, prog))
                cur_top_h = int(lerp(500, 800, prog))
                h = int(lerp(cur_bottom_h, cur_top_h, frac))
                zb = int(b * fade_tail)
                zones.append((h, 65535, zb, 2500))
            else:
                zones.append((0, 0, 0, 2500))

    # -------------------------------------------------------------
    # Weather Indicator Logic (Zone 39 = Weather, Zone 38 = Black Separator)
    # Single weather pixel fades in at 07:00, fades out completely at 12:00 (noon)
    # -------------------------------------------------------------
    if t_weather_start <= m < t_weather_end:
        # Fade-in curve: 07:00 -> 07:20 (minute 420 -> 440)
        # Fade-out curve: 11:45 -> 12:00 (minute 705 -> 720)
        fade_factor = 1.0
        if m < t_weather_start + 20:
            fade_factor = max(0.2, (m - t_weather_start) / 20.0)
        elif m > t_weather_end - 15:
            fade_factor = (t_weather_end - m) / 15.0

        if fade_factor > 0:
            # Force Zone 38 to be black (off) as a clean physical visual separator
            # Lerp brightness down so separator stays dark
            zones[38] = (0, 0, 0, 2500)

            # Single weather pixel at top (Zone 39)
            if weather_sunny:
                # Happy yellow: Hue 10000 (~55° golden yellow), Sat 65535, Kelvin 3500K
                w_h, w_s, w_k = 10000, 65535, 3500
            else:
                # Cloudy / Overcast: Soft silver-grey / neutral white
                w_h, w_s, w_k = 8000, 4000, 4500

            base_b = zones[37][2] if (len(zones) > 37 and zones[37][2] > 0) else int(lerp(15000, 50000, min(1.0, (m - t_weather_start) / 45.0)))
            target_b = max(8000, int(base_b * 0.95 * fade_factor))
            zones[39] = (w_h, w_s, target_b, w_k)

    return zones

def hsbk_to_rgb(h, s, b, k):
    """Convert HSBK values (0-65535 scale) to 8-bit RGB (0-255) for web UI preview."""
    h_norm = (h / 65535.0) * 360.0
    s_norm = s / 65535.0
    v_norm = b / 65535.0

    c = v_norm * s_norm
    x = c * (1 - abs((h_norm / 60.0) % 2 - 1))
    m = v_norm - c

    if 0 <= h_norm < 60:
        r1, g1, b1 = c, x, 0
    elif 60 <= h_norm < 120:
        r1, g1, b1 = x, c, 0
    elif 120 <= h_norm < 180:
        r1, g1, b1 = 0, c, x
    elif 180 <= h_norm < 240:
        r1, g1, b1 = 0, x, c
    elif 240 <= h_norm < 300:
        r1, g1, b1 = x, 0, c
    else:
        r1, g1, b1 = c, 0, x

    r_hsv = (r1 + m)
    g_hsv = (g1 + m)
    b_hsv = (b1 + m)

    # Approximate Kelvin adjustment (white balance tint)
    temp = k / 100.0
    if temp <= 66:
        kr = 255
        kg = 99.4708025861 * math.log(temp) - 161.1195681661
        kb = 0 if temp <= 19 else 138.5177312231 * math.log(temp - 10) - 305.0447927307
    else:
        kr = 329.698727446 * ((temp - 60) ** -0.1332047592)
        kg = 288.1221695283 * ((temp - 60) ** -0.0755148492)
        kb = 255

    kr = max(0, min(255, kr)) / 255.0
    kg = max(0, min(255, kg)) / 255.0
    kb = max(0, min(255, kb)) / 255.0

    r = (r_hsv * s_norm + kr * v_norm * (1.0 - s_norm))
    g = (g_hsv * s_norm + kg * v_norm * (1.0 - s_norm))
    b_ch = (b_hsv * s_norm + kb * v_norm * (1.0 - s_norm))

    return (
        int(max(0, min(255, r * 255))),
        int(max(0, min(255, g * 255))),
        int(max(0, min(255, b_ch * 255)))
    )

def fade_to_black_wipe(ip=DEFAULT_IP, mac=DEFAULT_MAC, duration_sec=2.0, steps=8):
    """
    Smoothly fades the current beam state to deep red and wipes pixels off from top
    to bottom over duration_sec (default 2 seconds) until fading to total black.
    Used for early bedtime / manual 'power off' command.
    """
    try:
        import glob
        import time
        site_dirs = glob.glob(os.path.expanduser("~/.venvs/lifx/lib/python*/site-packages"))
        for s_dir in site_dirs:
            if s_dir not in sys.path:
                sys.path.insert(0, s_dir)
        from lifxlan.multizonelight import MultiZoneLight
        beam = MultiZoneLight(mac, ip, 1, 56700, 12345)
        
        # Read current beam state
        try:
            curr_zones = beam.get_color_zones()
        except Exception:
            curr_zones = None

        num_zones = 40
        if not curr_zones or len(curr_zones) < num_zones:
            curr_zones = [(2000, 65535, 35000, 2500) for _ in range(num_zones)]

        # If already off, nothing to do
        if sum(z[2] for z in curr_zones) == 0:
            beam.set_power(0)
            return True

        # Phase 1: Rapidly warm/shift current colors towards red (Hue ~1200) over 400ms
        red_zones = []
        for z in curr_zones:
            red_h = int(lerp(z[0], 1200, 0.75))
            red_s = 65535
            red_b = int(z[2] * 0.85)
            red_zones.append((red_h, red_s, red_b, 2500))

        if hasattr(beam, "extended_set_zone_color"):
            beam.extended_set_zone_color(red_zones, duration=400)
        else:
            beam.set_zone_colors(red_zones, duration=400)
        time.sleep(0.35)

        # Phase 2: Top-to-bottom pixel wipe to black over remaining ~1.65 seconds
        wipe_time = max(0.8, duration_sec - 0.35)
        dt = wipe_time / float(steps)
        step_duration_ms = int(dt * 1000 * 1.5)  # slight overlap for butter-smooth visual wipe

        for s in range(1, steps + 1):
            prog = s / float(steps)
            # Active boundary descends from zone 39 down to -1
            boundary = lerp(num_zones - 1, -1.0, prog)
            dim_factor = 1.0 - (prog * 0.9)

            step_zones = []
            for z in range(num_zones):
                if z <= boundary:
                    fade_tail = 1.0 if (boundary - z) >= 1.0 else max(0.0, boundary - z)
                    orig = red_zones[z]
                    zb = int(orig[2] * dim_factor * fade_tail)
                    step_zones.append((orig[0], orig[1], zb, orig[3]))
                else:
                    step_zones.append((0, 0, 0, 2500))

            if hasattr(beam, "extended_set_zone_color"):
                beam.extended_set_zone_color(step_zones, duration=step_duration_ms)
            else:
                beam.set_zone_colors(step_zones, duration=step_duration_ms)
            time.sleep(dt)

        # Final off packet
        beam.set_power(0)
        return True
    except Exception as e:
        print(f"Error in fade_to_black_wipe: {e}", file=sys.stderr)
        return False

def apply_to_beam(zones, ip=DEFAULT_IP, mac=DEFAULT_MAC, duration_ms=DEFAULT_TRANSITION_MS):
    """
    Sends computed zone colors directly to the physical LIFX Beam.
    Design Rule: Default transition duration is multi-second (3000ms) for magical, smooth fades.
    """
    try:
        import glob
        site_dirs = glob.glob(os.path.expanduser("~/.venvs/lifx/lib/python*/site-packages"))
        for s_dir in site_dirs:
            if s_dir not in sys.path:
                sys.path.insert(0, s_dir)
        from lifxlan.multizonelight import MultiZoneLight
        beam = MultiZoneLight(mac, ip, 1, 56700, 12345)
        total_b = sum(z[2] for z in zones)
        if total_b == 0:
            beam.set_power(0, duration=duration_ms)
        else:
            beam.set_power(65535)
            if hasattr(beam, "extended_set_zone_color"):
                beam.extended_set_zone_color(zones, duration=duration_ms)
            else:
                beam.set_zone_colors(zones, duration=duration_ms)
        return True
    except Exception as e:
        print(f"Error applying to LIFX Beam: {e}", file=sys.stderr)
        return False

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Circadian lighting engine for LIFX Beam")
    parser.add_argument("--apply", action="store_true", help="Calculate current zone colors and apply directly to beam")
    parser.add_argument("--minute", type=int, default=None, help="Override minute of day (0-1439)")
    parser.add_argument("--duration", type=int, default=DEFAULT_TRANSITION_MS, help="Transition duration in ms")
    parser.add_argument("--weekend", action="store_true", help="Force weekend schedule")
    parser.add_argument("--sunny", action="store_true", help="Force sunny weather indicator")
    parser.add_argument("--cloudy", action="store_true", help="Force cloudy weather indicator")
    parser.add_argument("--weather", choices=["sunny", "partly_cloudy", "cloudy", "rain"], help="Force full weather state (drives indicator pixel + daytime warm shift)")
    parser.add_argument("--power-off", action="store_true", help="Smoothly fade to red and wipe pixels off from top to bottom over 2s to black")
    parser.add_argument("--set-exemption", choices=["weekday", "weekend", "reading", "nightlight", "amber-glow", "clear"], help="Set or clear today's temporary exemption")
    parser.add_argument("--extend-hours", type=float, default=None, help="Shift circadian schedule back by N hours (rewinds lighting to N hours ago and proceeds from there)")
    parser.add_argument("--hold-hours", type=float, default=None, help="Hold exemption for N hours before auto-expiring")
    parser.add_argument("--freeze", action="store_true", help="FREEZE the beam's current lighting and hold it across days until --hold-hours expires or --unfreeze is run; normal schedule resumes automatically on release")
    parser.add_argument("--unfreeze", action="store_true", help="RELEASE a frozen lighting hold; normal circadian schedule resumes on next tick")
    args = parser.parse_args()

    if args.freeze:
        exp = None
        if args.hold_hours:
            from datetime import timedelta
            exp = datetime.now(timezone.utc) + timedelta(hours=args.hold_hours)
        data = freeze_current_lighting(expires_at=exp)
        lit = sum(1 for z in data["zones"] if z[2] > 0)
        if exp:
            print(f"FROZEN current lighting ({lit} zones) — holds until {exp.astimezone(ZoneInfo(LONG_BEACH_TZ)).strftime('%a %H:%M %Z')}, then normal schedule resumes.")
        else:
            print(f"FROZEN current lighting ({lit} zones) — holds indefinitely until --unfreeze.")
        sys.exit(0)

    if args.unfreeze:
        unfreeze()
        print("UNFROZEN — normal circadian schedule resumes on the next daemon tick.")
        sys.exit(0)

    if args.set_exemption:
        expires_at = None
        if args.hold_hours:
            from datetime import timedelta
            expires_at = datetime.now(timezone.utc) + timedelta(hours=args.hold_hours)

        if args.set_exemption == "clear":
            clear_exemption()
            print("Cleared active exemption.")
        elif args.set_exemption in ("weekday", "weekend"):
            set_exemption(schedule=args.set_exemption, expires_at=expires_at)
            print(f"Set today's exemption schedule to: {args.set_exemption}")
        elif args.set_exemption == "reading":
            set_exemption(preset="reading", expires_at=expires_at)
            print("Set today's exemption preset to: reading light")
        elif args.set_exemption == "nightlight":
            set_exemption(preset="nightlight", expires_at=expires_at)
            print(f"Set exemption preset to: nightlight (hold: {args.hold_hours}h)")
        elif args.set_exemption == "amber-glow":
            set_exemption(preset="amber-glow", expires_at=expires_at)
            print(f"Set exemption preset to: amber-glow (hold: {args.hold_hours}h)")

    if args.extend_hours is not None:
        expires_at = None
        hold_h = args.hold_hours if args.hold_hours is not None else args.extend_hours
        if hold_h > 0:
            from datetime import timedelta
            expires_at = datetime.now(timezone.utc) + timedelta(hours=hold_h)
        shift_min = int(-args.extend_hours * 60)
        set_exemption(time_shift_minutes=shift_min, expires_at=expires_at)
        exp_str = f" until {expires_at.astimezone(ZoneInfo(LONG_BEACH_TZ)).strftime('%H:%M %Z')}" if expires_at else ""
        print(f"Shifted schedule back by {args.extend_hours}h ({shift_min} min){exp_str}. Normal progression continues from there.")

    if args.power_off:
        success = fade_to_black_wipe(duration_sec=2.0)
        print(f"Fade-to-black wipe executed: {'SUCCESS' if success else 'FAILED'}")
        sys.exit(0 if success else 1)

    lb_now = get_long_beach_now()
    cur_min = args.minute if args.minute is not None else (lb_now.hour * 60 + lb_now.minute)
    is_weekend = args.weekend or (args.minute is None and lb_now.weekday() >= 5)

    cond = get_weather_condition()
    if args.weather:
        wstate = args.weather
    elif args.sunny:
        wstate = "sunny"
    elif args.cloudy:
        wstate = "cloudy"
    else:
        wstate = parse_weather_state(cond)
    sunny = wstate in ("sunny", "partly_cloudy")

    sunset_m = calculate_sunset_minute(lb_now)
    zones = calculate_beam_zones(cur_min, is_weekend=is_weekend, weather_state=wstate, sunset_min=sunset_m)

    sunset_hh = sunset_m // 60
    sunset_mm = sunset_m % 60
    print(f"Long Beach Time: {lb_now.strftime('%Y-%m-%d %H:%M:%S %Z')} | Minute {cur_min} ({cur_min // 60:02d}:{cur_min % 60:02d})")
    print(f"Calculated Sunset: {sunset_hh:02d}:{sunset_mm:02d} | Weekend: {is_weekend} | Weather: {cond} ({wstate}, daytime shift {daytime_kelvin_shift(wstate):+d}K)")
    print(f"Zone 0 (Bottom): {zones[0]}")
    print(f"Zone 37 (Pre-sep): {zones[37]}")
    print(f"Zone 38 (Separator): {zones[38]}")
    print(f"Zone 39 (Weather/Top): {zones[39]}")

    if args.apply:
        success = apply_to_beam(zones, duration_ms=args.duration)
        print(f"Applied to LIFX Beam ({args.duration}ms transition): {'SUCCESS' if success else 'FAILED'}")
