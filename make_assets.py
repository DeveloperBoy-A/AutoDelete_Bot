# -*- coding: utf-8 -*-
"""Tokyo_Updates | Credits: Tokyo_Updates
Optional helper: regenerates the Tokyo Auto Delete Bot start banner + profile picture.
Requires Pillow (pip install pillow). Not needed at runtime."""
import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter

__credits__ = 'Tokyo_Updates'
POPPINS = '/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf'
CJK = '/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc'


def Tokyo_gradient(w, h, top, bottom):
    img = Image.new('RGB', (w, h))
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        c = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        for x in range(w):
            px[x, y] = c
    return img


def Tokyo_glow_circle(base, cx, cy, r, color, blur=90, alpha=120):
    layer = Image.new('RGBA', base.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((cx - r, cy - r, cx + r, cy + r), fill=color + (alpha,))
    layer = layer.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(layer)


def Tokyo_draw_trash_clock(d, cx, cy, s, color=(255, 255, 255)):
    """Simple clock-with-bin icon drawn with shapes."""
    # clock ring
    d.ellipse((cx - s, cy - s, cx + s, cy + s), outline=color, width=max(6, s // 14))
    d.line((cx, cy, cx, cy - s * 0.6), fill=color, width=max(6, s // 14))
    d.line((cx, cy, cx + s * 0.4, cy + s * 0.15), fill=color, width=max(6, s // 14))
    d.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=color)
    for i in range(12):
        a = math.radians(i * 30)
        r1, r2 = s * 0.86, s * 0.95
        d.line((cx + r1 * math.sin(a), cy - r1 * math.cos(a), cx + r2 * math.sin(a), cy - r2 * math.cos(a)), fill=color, width=4)


def Tokyo_banner(path):
    w, h = 1280, 720
    img = Tokyo_gradient(w, h, (20, 8, 48), (88, 16, 80)).convert('RGBA')
    Tokyo_glow_circle(img, 1000, 200, 260, (255, 70, 150))
    Tokyo_glow_circle(img, 200, 620, 280, (90, 80, 255))
    d = ImageDraw.Draw(img)
    # sakura-like dots
    import random
    random.seed(7)
    for _ in range(40):
        x, y, r = random.randint(0, w), random.randint(0, h), random.randint(3, 9)
        d.ellipse((x, y, x + r, y + r), fill=(255, 190, 220, random.randint(70, 160)))
    # icon
    Tokyo_draw_trash_clock(d, 960, 360, 170, (255, 255, 255))
    d.ellipse((960 - 200, 360 - 200, 960 + 200, 360 + 200), outline=(255, 120, 190, 150), width=3)
    # texts
    d.text((90, 120), '東京', font=ImageFont.truetype(CJK, 90), fill=(255, 150, 200))
    d.text((84, 235), 'TOKYO', font=ImageFont.truetype(POPPINS, 150), fill=(255, 255, 255))
    d.text((90, 400), 'AUTO DELETE BOT', font=ImageFont.truetype(POPPINS, 62), fill=(255, 130, 190))
    d.rounded_rectangle((90, 500, 560, 506), radius=3, fill=(255, 255, 255, 120))
    d.text((90, 535), 'Clean groups  •  Auto timer  •  Force-Sub', font=ImageFont.truetype(POPPINS, 30), fill=(230, 220, 245))
    d.text((90, 625), '@Tokyo_Updates', font=ImageFont.truetype(POPPINS, 30), fill=(255, 190, 220))
    img.convert('RGB').save(path, quality=92)


def Tokyo_profile(path):
    s = 640
    img = Tokyo_gradient(s, s, (24, 8, 56), (110, 20, 90)).convert('RGBA')
    Tokyo_glow_circle(img, 480, 150, 190, (255, 70, 150), blur=70)
    d = ImageDraw.Draw(img)
    Tokyo_draw_trash_clock(d, s // 2, 255, 140)
    f = ImageFont.truetype(POPPINS, 92)
    t = 'TOKYO'
    d.text(((s - d.textlength(t, font=f)) / 2, 420), t, font=f, fill=(255, 255, 255))
    f2 = ImageFont.truetype(POPPINS, 38)
    t2 = 'AUTO DELETE'
    d.text(((s - d.textlength(t2, font=f2)) / 2, 530), t2, font=f2, fill=(255, 130, 190))
    img.convert('RGB').save(path, quality=92)


if __name__ == '__main__':
    Tokyo_banner('assets/start.jpg')
    Tokyo_profile('assets/bot_profile.jpg')
    print('done | Credits: Tokyo_Updates')
