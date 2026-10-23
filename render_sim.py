#!/usr/bin/env python3
import os
import sys
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.expanduser("~/projects/infra/skills/lifx"))
import circadian_engine

def render_reading_light_sim(output_path="/tmp/reading_light_sim.png"):
    zones = circadian_engine.get_reading_light_zones(40)
    rgb_zones = [circadian_engine.hsbk_to_rgb(*z) for z in zones]

    # Create an elegant high-res vertical render of the LIFX Beam
    width = 600
    height = 950
    img = Image.new("RGB", (width, height), (13, 17, 23)) # dark background
    draw = ImageDraw.Draw(img)

    # Title & Labels
    title_text = "LIFX Beam — Reading Light Simulation"
    draw.text((40, 30), title_text, fill=(230, 237, 243))
    subtitle = "Exemption Preset: Red-Orange Top Gradient | Inactive Mid | Red Ember Bottom"
    draw.text((40, 55), subtitle, fill=(139, 148, 158))

    # Beam dimensions (Zone 39 top, Zone 0 bottom)
    beam_x = 260
    beam_w = 64
    beam_top_y = 110
    beam_bottom_y = 870
    total_zones = 40
    zone_h = (beam_bottom_y - beam_top_y) / float(total_zones)

    # Draw Beam housing shadow/outline
    draw.rounded_rectangle(
        [beam_x - 6, beam_top_y - 6, beam_x + beam_w + 6, beam_bottom_y + 6],
        radius=8,
        fill=(22, 27, 34),
        outline=(48, 54, 61),
        width=2
    )

    # Render each zone (Zone 39 at top down to Zone 0 at bottom)
    for i in range(total_zones):
        zone_idx = (total_zones - 1) - i # Zone 39 down to 0
        r, g, b = rgb_zones[zone_idx]
        zy = beam_top_y + i * zone_h
        
        # Glow effect if pixel is on
        bri = zones[zone_idx][2]
        if bri > 0:
            glow_alpha = int(30 * (bri / 65535.0))
            # Subtle side halo glow
            draw.rectangle([beam_x - 14, zy, beam_x + beam_w + 14, zy + zone_h], fill=(min(255, r + 20), min(255, g + 20), min(255, b + 20)))
        
        # Zone cell
        draw.rectangle([beam_x, zy, beam_x + beam_w, zy + zone_h - 1], fill=(r, g, b))

        # Annotations on side
        if zone_idx == 39:
            draw.text((beam_x + beam_w + 24, zy + 2), f"Zone 39 (Top): Warm Orange ({r},{g},{b})", fill=(240, 136, 62))
        elif zone_idx == 30:
            draw.text((beam_x + beam_w + 24, zy + 2), f"Zone 30: Deep Red ({r},{g},{b})", fill=(230, 80, 60))
        elif zone_idx == 20:
            draw.text((beam_x + beam_w + 24, zy + 2), "Zones 8–29: Off / Black (Reading Focus)", fill=(110, 118, 129))
        elif zone_idx == 7:
            draw.text((beam_x + beam_w + 24, zy + 2), f"Zone 7: Red Ember Peak ({r},{g},{b})", fill=(200, 60, 50))
        elif zone_idx == 0:
            draw.text((beam_x + beam_w + 24, zy + 2), f"Zone 0 (Bottom): Subtle Ember Fade ({r},{g},{b})", fill=(140, 40, 30))

    img.save(output_path, "PNG")
    print(f"Simulation saved to {output_path}")

if __name__ == "__main__":
    render_reading_light_sim()
