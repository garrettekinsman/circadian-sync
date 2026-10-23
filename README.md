# Circadian Sync — Agent-Controlled Circadian Lighting

Autonomous circadian rhythm synchronization for indoor spaces — designed for
**winter climates where natural light disappears for months**, and for
**habitats on other worlds like Mars, where rooms have no windows at all**.

## Why This Exists

### Winter Climates & Seasonal Depression

In high-latitude winters, the sun sets at 3 PM and natural light drops far
below the threshold human biology needs. Circadian rhythms drift, melatonin
gets confused, and seasonal affective disorder (SAD) follows. The cure isn't
a brighter bulb — it's **dynamic light that mimics the sky**: full-spectrum
daylight at the right hours, a gradual sunset in the evening, and deep
red-amber light at night that doesn't suppress melatonin.

This project drives a multi-zone light fixture (LIFX Beam or any addressable
zone array) along a true astronomical curve — sunrise, midday, real solar
sunset computed for your latitude, and a down-setting evening fade that
emulates the sun sinking below the horizon.

### Life on Mars (and Anywhere Indoors)

On Mars, most of your life happens indoors. Hab modules, workshops, and
sleeping quarters will often have **no windows — for cost and radiation
reasons**. Thick shielding and regolith coverage keep you alive; they also
keep sunlight out. A habitat that never sees the sky needs its lighting to
carry the full circadian load:

- **Sol-cycle sync:** configurable for 24h Earth days or 24h 39m sols.
- **Melatonin protection:** evening zones fade below 2200K and under
  melatonin-suppression thresholds so crew sleep architecture survives
  windowless living.
- **Zonal discipline:** light never bleeds into sleeping quarters — each
  zone array follows its own schedule role.
- **Autonomous operation:** runs headless under agent/system supervision
  (systemd or similar), with override exemptions for work shifts and alerts.

The same hardware and code that fights SAD in a Long Beach apartment is the
seed of habitat life-support lighting on another planet. Same biology,
same physics.

## What It Does

- **40-zone gradient geometry** — emulates atmospheric light gradients along
  the fixture; amber always sinks to the bottom of the array, like a real
  sunset.
- **Astronomical solar tracking** — exact sunset computed from your
  latitude/longitude, updated daily, DST handled automatically.
- **Sub-perceptual transitions** — all changes use 2,000–3,500 ms hardware
  interpolation; light never jars.
- **Holds & exemptions** — one command (`lifx-beam hold`) suspends the
  schedule; it auto-resumes on expiry. Never a manual fight with the daemon.
- **Weather-aware daytime shift** — overcast days get warmer, softer
  ambient light.
- **Live web console** — scrub the 24-hour curve, preview any moment,
  toggle live-clock mode.

## Layout

```text
circadian_engine.py   # zone math: astronomical curve -> 40-zone vectors
circadian_server.py   # daemon: 30s tick, web console, UDP dispatch
render_sim.py         # offline render of the curve (no hardware needed)
bin/lifx-beam         # CLI: presets, gradient, hold, sync
```

## Quick Start

```bash
pip install lifxlan
```

Point it at your fixture and location via environment variables:

```bash
export LIFX_BEAM_IP=192.168.1.100   # your fixture's IP
export LIFX_BEAM_MAC=d0:73:d5:xx:xx:xx
export CIRCADIAN_TZ=America/Los_Angeles
export CIRCADIAN_LAT=33.7701
export CIRCADIAN_LON=-118.1937
```

Then:

```bash
python3 circadian_server.py        # daemon + web console on :8080
python3 render_sim.py              # simulate the curve, no hardware needed
bin/lifx-beam preset amber-glow    # manual override
bin/lifx-beam hold --until 22:00   # schedule hold, auto-resumes
```

## Safety Notes

- The schedule simulation never trusts active holds (`respect_exemption`
  is isolated), so a stuck hold can't poison the light forever.
- Blackout transitions use a controlled zone wipe, not a global flash-off.
- All daemon state is local — no cloud, no accounts, no telemetry.

## License

MIT
