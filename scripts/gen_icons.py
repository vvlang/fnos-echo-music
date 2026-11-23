#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate crisp 64x64 and 256x256 app icons for Echo Music Sync."""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent / "fpk"


def render_icon(size: int) -> Image.Image:
    # Render at 4x supersampling for silky anti-aliased edges
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Diagonal gradient rounded square background
    bg = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    px = bg.load()
    for y in range(s):
        for x in range(s):
            t = (x + y) / (2.0 * s)
            r = int(46 + (122 - 46) * t)
            g = int(124 + (82 - 124) * t)
            b = int(252 + (248 - 252) * t)
            px[x, y] = (r, g, b, 255)

    mask = Image.new("L", (s, s), 0)
    mdraw = ImageDraw.Draw(mask)
    pad = int(s * 0.04)
    radius = int(s * 0.24)
    mdraw.rounded_rectangle([pad, pad, s - pad, s - pad], radius=radius, fill=255)
    img.paste(bg, (0, 0), mask)

    draw = ImageDraw.Draw(img)

    # Echo Mini / AIYeon silhouette (retro portable Hi-Fi player body)
    bx0, by0 = int(s * 0.24), int(s * 0.16)
    bx1, by1 = int(s * 0.76), int(s * 0.84)
    br = int(s * 0.08)
    draw.rounded_rectangle(
        [bx0, by0, bx1, by1],
        radius=br,
        fill=(20, 26, 42, 165),
        outline=(255, 255, 255, 235),
        width=max(2, int(s * 0.025)),
    )

    # Screen panel (top half of player)
    sx0, sy0 = int(s * 0.30), int(s * 0.23)
    sx1, sy1 = int(s * 0.70), int(s * 0.49)
    draw.rounded_rectangle(
        [sx0, sy0, sx1, sy1],
        radius=int(s * 0.035),
        fill=(242, 247, 255, 245),
    )

    # Mini equalizer bars inside screen
    bar_w = int(s * 0.04)
    gap = int(s * 0.028)
    heights = [0.42, 0.78, 0.58, 0.86, 0.48]
    total_w = len(heights) * bar_w + (len(heights) - 1) * gap
    start_x = (sx0 + sx1 - total_w) // 2
    base_y = sy1 - int(s * 0.04)
    max_h = (sy1 - sy0) - int(s * 0.08)
    for i, h_ratio in enumerate(heights):
        x0 = start_x + i * (bar_w + gap)
        x1 = x0 + bar_w
        y0 = base_y - int(max_h * h_ratio)
        draw.rounded_rectangle(
            [x0, y0, x1, base_y],
            radius=max(2, bar_w // 2),
            fill=(79, 130, 255, 255) if i % 2 == 0 else (132, 92, 255, 255),
        )

    # Click-wheel / DAC control ring (bottom half)
    cx, cy = s // 2, int(s * 0.66)
    r_out = int(s * 0.115)
    r_in = int(s * 0.042)
    draw.ellipse(
        [cx - r_out, cy - r_out, cx + r_out, cy + r_out],
        fill=(255, 255, 255, 38),
        outline=(255, 255, 255, 235),
        width=max(2, int(s * 0.022)),
    )
    draw.ellipse(
        [cx - r_in, cy - r_in, cx + r_in, cy + r_in],
        fill=(255, 255, 255, 245),
    )

    # Sync / transfer badge on bottom-right corner
    bcx, bcy = int(s * 0.76), int(s * 0.76)
    br_badge = int(s * 0.13)
    draw.ellipse(
        [bcx - br_badge, bcy - br_badge, bcx + br_badge, bcy + br_badge],
        fill=(34, 197, 134, 255),
        outline=(255, 255, 255, 245),
        width=max(2, int(s * 0.018)),
    )
    # Right arrow inside green badge
    aw = int(s * 0.06)
    ah = int(s * 0.048)
    stroke = max(2, int(s * 0.022))
    draw.line([bcx - aw, bcy, bcx + aw - stroke, bcy], fill=(255, 255, 255, 255), width=stroke)
    draw.polygon(
        [(bcx + aw, bcy), (bcx + int(aw * 0.1), bcy - ah), (bcx + int(aw * 0.1), bcy + ah)],
        fill=(255, 255, 255, 255),
    )

    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    ui_imgs = ROOT / "app" / "ui" / "images"
    ui_imgs.mkdir(parents=True, exist_ok=True)
    img64 = render_icon(64)
    img256 = render_icon(256)
    img64.save(ROOT / "ICON.PNG")
    img256.save(ROOT / "ICON_256.PNG")
    img64.save(ui_imgs / "icon_64.png")
    img256.save(ui_imgs / "icon_256.png")
    print("Generated icons:", ROOT / "ICON.PNG", ROOT / "ICON_256.PNG", ui_imgs)


if __name__ == "__main__":
    main()
