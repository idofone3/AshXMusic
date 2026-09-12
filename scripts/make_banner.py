"""Generate the AshXMusic banner (assets/banner.png) with PIL.

Also emits assets/banner_wide.png (1500x500, GitHub social preview ratio)
and assets/logo.png (square 512, used by the bot /start and stream slate).
"""
import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "assets")
os.makedirs(ASSETS, exist_ok=True)

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(w, h, c1, c2, c3):
    """Three-stop diagonal gradient."""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(0, w, 4):
            t = (x / w * 0.55) + (y / h * 0.45)
            c = lerp(c1, c2, t) if t < 0.5 else lerp(c2, c3, (t - 0.5) * 2)
            for dx in range(4):
                if x + dx < w:
                    px[x + dx, y] = c
    return img


def glow_rings(img):
    """Soft neon rings + sparkles for a music vibe."""
    w, h = img.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = int(w * 0.80), int(h * 0.42)
    for r, alpha, wd in ((h * 0.42, 70, 5), (h * 0.30, 90, 4),
                         (h * 0.19, 120, 3), (h * 0.10, 150, 3)):
        d.ellipse([cx - r, cy - r * 0.96, cx + r, cy + r * 0.96],
                  outline=(30, 215, 96, alpha), width=max(2, int(wd)))
    # glowing dot = the "note head"
    d.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], fill=(30, 215, 96, 230))
    layer = layer.filter(ImageFilter.GaussianBlur(3))
    img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    return img


def draw_wordmark(img, cy_frac=0.44, scale=1.0):
    w, h = img.size
    d = ImageDraw.Draw(img)
    f_big = font(int(h * 0.20 * scale))
    f_sub = font(int(h * 0.052 * scale))
    title = "AshXMusic"
    sub = "24/7 TELEGRAM RADIO  •  YT MUSIC QUEUE"
    tw = d.textlength(title, font=f_big)
    x = (w - tw) / 2
    y = h * cy_frac - h * 0.10
    # soft shadow
    d.text((x + 5, y + 6), title, font=f_big, fill=(0, 0, 0))
    # two-tone wordmark
    d.text((x, y), "Ash", font=f_big, fill=(255, 255, 255))
    d.text((x + d.textlength("Ash", font=f_big), y), "X",
           font=f_big, fill=(30, 215, 96))
    d.text((x + d.textlength("AshX", font=f_big), y), "Music",
           font=f_big, fill=(255, 255, 255))
    sw = d.textlength(sub, font=f_sub)
    d.text(((w - sw) / 2, y + h * 0.26), sub, font=f_sub,
           fill=(255, 255, 255, 210))
    return img


def add_music_note(img):
    """Draw a big note glyph near the rings."""
    w, h = img.size
    d = ImageDraw.Draw(img)
    f = font(int(h * 0.34))
    d.text((int(w * 0.72), int(h * 0.12)), "♪", font=f, fill=(30, 215, 96))
    return img


def vignette(img):
    w, h = img.size
    m = Image.new("L", (w, h), 0)
    dm = ImageDraw.Draw(m)
    dm.ellipse([-w * 0.25, -h * 0.35, w * 1.25, h * 1.35], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(w * 0.08))
    dark = Image.new("RGB", (w, h), (5, 8, 14))
    return Image.composite(img, dark, m)


def main():
    c1, c2, c3 = (10, 14, 26), (28, 32, 62), (12, 46, 40)

    # ---- wide banner 1500x500 (README) ----
    img = gradient(1500, 500, c1, c2, c3)
    img = glow_rings(img)
    img = draw_wordmark(img)
    img = add_music_note(img)
    img = vignette(img)
    img.save(os.path.join(ASSETS, "banner_wide.png"), optimize=True)

    # ---- stream overlay banner 1280x300 (bottom-third look) ----
    img2 = gradient(1280, 300, c1, c2, c3)
    img2 = glow_rings(img2)
    img2 = draw_wordmark(img2, cy_frac=0.40, scale=0.8)
    img2 = vignette(img2)
    img2.save(os.path.join(ASSETS, "banner.png"), optimize=True)

    # ---- square logo 512 ----
    img3 = gradient(512, 512, c1, c2, c3)
    img3 = glow_rings(img3)
    d = ImageDraw.Draw(img3)
    f = font(110)
    t = "AshX"
    tw = d.textlength(t, font=f)
    d.text(((512 - tw * 2.1) / 2, 150), "Ash", font=f, fill=(255, 255, 255))
    d.text(((512 - tw * 2.1) / 2 + d.textlength("Ash", font=f), 150),
           "X", font=f, fill=(30, 215, 96))
    d.text(((512 - tw * 2.1) / 2 + d.textlength("AshX", font=f), 150),
           "Music", font=f, fill=(255, 255, 255))
    f2 = font(30)
    s = "24/7 TELEGRAM RADIO"
    d.text(((512 - d.textlength(s, font=f2)) / 2, 300), s, font=f2,
           fill=(210, 220, 235))
    img3 = vignette(img3)
    img3.save(os.path.join(ASSETS, "logo.png"), optimize=True)

    for n in ("banner_wide.png", "banner.png", "logo.png"):
        p = os.path.join(ASSETS, n)
        print(n, os.path.getsize(p) // 1024, "KB")


if __name__ == "__main__":
    main()
