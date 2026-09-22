#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标 AppIcon.icns (圆角渐变 + HAP 字样)。"""
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "build"
ICONSET = OUT / "AppIcon.iconset"
ICNS = OUT / "AppIcon.icns"


def find_font(size):
    for p in ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "/System/Library/Fonts/Helvetica.ttc",
              "/System/Library/Fonts/SFNS.ttf",
              "/Library/Fonts/Arial.ttf"]:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def make_master(size=1024):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (size, size))
    d = ImageDraw.Draw(grad)
    c1, c2 = (37, 99, 235), (124, 58, 237)
    for y in range(size):
        t = y / size
        d.line([(0, y), (size, y)],
               fill=(int(c1[0] + (c2[0] - c1[0]) * t),
                     int(c1[1] + (c2[1] - c1[1]) * t),
                     int(c1[2] + (c2[2] - c1[2]) * t), 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    font = find_font(int(size * 0.30))
    text = "HAP"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1] - size * 0.03),
           text, font=font, fill=(255, 255, 255, 255))
    font2 = find_font(int(size * 0.10))
    sub = "调试助手"
    b2 = d.textbbox((0, 0), sub, font=font2)
    d.text(((size - (b2[2] - b2[0])) / 2 - b2[0], size * 0.66),
           sub, font=font2, fill=(255, 255, 255, 230))
    return img


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if ICONSET.exists():
        for f in ICONSET.iterdir():
            f.unlink()
    ICONSET.mkdir(exist_ok=True)
    master = make_master(1024)
    sizes = [16, 32, 64, 128, 256, 512, 1024]
    for s in sizes:
        master.resize((s, s), Image.LANCZOS).save(ICONSET / f"icon_{s}x{s}.png")
        if s <= 512:
            master.resize((s * 2, s * 2), Image.LANCZOS).save(ICONSET / f"icon_{s}x{s}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)], check=True)
    print(f"已生成 {ICNS}")


if __name__ == "__main__":
    sys.exit(main())
