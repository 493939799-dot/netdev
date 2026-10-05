import math, random
from PIL import Image, ImageDraw

W, H = 1600, 1000
random.seed(7)
img = Image.new("RGB", (W, H), (7, 7, 7))

glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
cx, cy, R = 130, 26, 430
for i in range(70, 0, -1):
    r = R * i / 70
    a = int(30 * (1 - i / 70) ** 1.6)
    gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(61, 220, 132, a))
img = Image.alpha_composite(img.convert("RGBA"), glow)

dots = Image.new("RGBA", (W, H), (0, 0, 0, 0))
dd = ImageDraw.Draw(dots)
CX, CY = 820, 470
TH = math.radians(-12)
cos_t, sin_t = math.cos(TH), math.sin(TH)
STEP, DOT = 14, 7

def bands(x, y):
    dx, dy = x - CX, y - CY
    u = cos_t * dx + sin_t * dy
    v = -sin_t * dx + cos_t * dy
    d1 = math.hypot(u / 660.0, v / 340.0)
    d2 = math.hypot(u / 400.0, v / 170.0)
    g1 = math.exp(-((d1 - 1.0) / 0.13) ** 2)
    g2 = 0.9 * math.exp(-((d2 - 1.0) / 0.11) ** 2)
    return g1 + g2

y = 40
while y < H - 10:
    x = 20
    while x < W - 10:
        d = bands(x, y)
        n = random.random()
        if n < d * 0.95 + 0.06:
            if d > 1.02 and random.random() < 0.20:
                c = (61, 220, 132); a = int(200 + 55 * min(d, 1.4))
            elif d > 0.55:
                c = (64, 118, 82); a = int(170 + 70 * min(d, 1.3))
            else:
                c = (74, 76, 74) if random.random() < 0.5 else (92, 94, 92)
                a = int(120 + 100 * min(d + 0.2, 1.0))
            s = DOT + (3 if (d > 0.95 and random.random() < 0.14) else 0)
            dd.rectangle([x, y, x + s, y + s], fill=c + (a,))
        x += STEP
    y += STEP

img = Image.alpha_composite(img, dots)
import sys
img.convert("RGB").save(sys.argv[1] if len(sys.argv)>1 else "backdrop.png", optimize=True)
print("saved")
