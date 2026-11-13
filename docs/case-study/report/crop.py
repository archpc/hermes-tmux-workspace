#!/usr/bin/env python3
"""Cut readable regions out of the full-window captures. Deterministic: sips only.

Geometry was measured from the captures (row pitch from text-row profiles):
  shadowed iTerm2 captures (3116x1810, 1604x950, 1092x950): window at (46,32),
    row r top = 112 + 32 r, column c left = 56 + 16 c
  baseline captures (3024x1728, no shadow): row r top = 68 + 34 r, column c left = 10 + 16.7 c
"""
import subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ART = HERE.parent.parent / "ux-redesign"
SHOTS = HERE.parent / "shots" / "png"
OUT = HERE / "figures" / "img"

G = {"shadow": (56, 112, 16.0, 32.0), "base": (10, 68, 16.7, 34.0)}


def crop(src, out, geom, c0, c1, r0, r1, pad=8, pady=3, maxw=1800):
    x0, y0, cw, ch = G[geom]
    x = max(0, int(x0 + c0 * cw) - pad)
    y = max(0, int(y0 + r0 * ch) - pady)
    w = int((c1 - c0) * cw) + 2 * pad
    h = int((r1 - r0) * ch) + 2 * pady
    dst = OUT / out
    subprocess.run(["sips", "-c", str(h), str(w), "--cropOffset", str(y), str(x),
                    str(src), "--out", str(dst)], check=True, capture_output=True)
    if w > maxw:
        subprocess.run(["sips", "--resampleWidth", str(maxw), str(dst)],
                       check=True, capture_output=True)
    print(out, w, h)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    base, final = ART / "baseline", ART / "final"
    crop(base / "v2-overview.png", "v2-overview.png", "base", 0, 96, 0, 18)
    crop(base / "v2-overview.png", "v2-tabs.png", "base", 0, 80, 47, 48, pad=6)
    crop(base / "v2-worker-1.png", "v2-worker.png", "base", 0, 96, 0, 30)
    crop(SHOTS / "prod-01-overview-running.png", "prod-overview.png", "shadow", 35, 152, 0, 12)
    crop(SHOTS / "prod-02-worker-running.png", "prod-tabs.png", "shadow", 0, 100, 50, 51, pad=6)
    crop(SHOTS / "prod-02-worker-running.png", "prod-worker.png", "shadow", 0, 96, 0, 15)
    crop(SHOTS / "prod-04-overview-completed.png", "prod-overview-done.png", "shadow", 35, 152, 0, 12)
    crop(final / "01-full-overview.png", "lab-attention.png", "shadow", 35, 152, 12, 17)
    crop(final / "10-full-worker-run-ended-with-error.png", "lab-failed-head.png", "shadow", 0, 112, 0, 1)
    crop(final / "10-full-worker-run-ended-with-error.png", "lab-failed-tail.png", "shadow", 0, 112, 45, 50)
    crop(final / "08-narrow-overview.png", "lab-narrow-overview.png", "shadow", 0, 61, 0, 24, pad=10)
    crop(final / "09-narrow-worker-running.png", "lab-narrow-worker.png", "shadow", 0, 61, 0, 24, pad=10)
    crop(final / "06-quarter-overview.png", "lab-quarter-overview.png", "shadow", 0, 93, 0, 24, pad=10)


if __name__ == "__main__":
    main()
