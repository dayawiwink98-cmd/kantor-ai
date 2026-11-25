"""Mesin animasi: kartun 2D bergaya 'cerita horor bersambung' (tanpa GPU, tanpa gambar internet).

Isi: kanvas gambar, 9 latar, tokoh dengan rambut/hijab/kacamata, 6 jenis shot (lebar, sedang, close-up,
ranjang, CCTV, CCTV zoom), efek (getar, kedip lampu, glitch, kejut), subtitle kata demi kata, kartu akhir.
"""
import bisect
import datetime
import math
import os
import random
import subprocess
import zlib
from array import array
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H, FPS = 720, 1280, 30
FONT = os.environ.get("FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FONT_SERIF = os.environ.get("FONT_SERIF_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf")
FONT_MONO = os.environ.get("FONT_MONO_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")
KANAL = os.environ.get("KANAL", "BISIK")

LATAR = ["kamar_malam", "kamar_pagi", "dapur", "ruang_tamu", "lorong", "luar_malam", "rak_buku", "kamar_mandi", "gelap"]
SHOT = ["lebar", "sedang", "closeup", "ranjang", "cctv", "cctv_zoom"]
EMOSI = ["netral", "senang", "kaget", "sedih", "marah", "takut", "seram"]
EFEK = ["getar", "kedip", "glitch", "kejut"]
POSE = ["berdiri", "duduk", "tidur"]

_FC = {}


def font(path, size):
    k = (path, int(size))
    if k not in _FC:
        try:
            _FC[k] = ImageFont.truetype(path, int(size))
        except OSError:
            try:
                _FC[k] = ImageFont.truetype("DejaVuSans-Bold.ttf", int(size))
            except OSError:
                _FC[k] = ImageFont.load_default(int(size))
    return _FC[k]


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def durasi(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True)
    return float(out.stdout.strip())


def baca_energi(path):
    """Kekuatan suara per frame (0..1) untuk menggerakkan mulut."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        check=True, capture_output=True).stdout
    a = array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    langkah = 16000 // FPS
    rms = []
    for i in range(0, len(a), langkah):
        seg = a[i:i + langkah]
        rms.append(math.sqrt(sum(x * x for x in seg) / max(1, len(seg))))
    if not rms:
        return [0.0]
    puncak = sorted(rms)[int(len(rms) * 0.92)] or max(rms) or 1.0
    return [min(1.0, v / puncak) for v in rms]


def perkirakan_kata(narasi, energi):
    """Cadangan bila server TTS tidak memberi waktu per kata."""
    kata = narasi.split()
    puncak = max(energi) or 1.0
    ada = [i for i, e in enumerate(energi) if e > 0.08 * puncak]
    mulai = ada[0] / FPS if ada else 0.0
    akhir = (ada[-1] + 1) / FPS if ada else len(energi) / FPS
    bobot = [len(k) + 1 + (4 if k[-1] in ",.?!;:" else 0) for k in kata]
    total, t, hasil = sum(bobot), mulai, []
    for k, b in zip(kata, bobot):
        d = (akhir - mulai) * b / total
        hasil.append((t, t + d, k))
        t += d
    return hasil


# ================================================================ kanvas gambar (supersampling)
class Cv:
    def __init__(self, w, h, s=2, mode="RGBA"):
        self.w, self.h, self.s = w, h, s
        self.im = Image.new(mode, (int(w * s), int(h * s)), (0, 0, 0, 0) if mode == "RGBA" else (0, 0, 0))
        self.d = ImageDraw.Draw(self.im)

    def P(self, pts):
        return [(x * self.s, y * self.s) for x, y in pts]

    def poly(self, pts, fill, outline=None, w=0):
        self.d.polygon(self.P(pts), fill=fill)
        if outline:
            self.line(list(pts) + [pts[0]], outline, w or 2)

    def ell(self, cx, cy, rx, ry, fill=None, outline=None, w=0):
        s = self.s
        self.d.ellipse([(cx - rx) * s, (cy - ry) * s, (cx + rx) * s, (cy + ry) * s],
                       fill=fill, outline=outline, width=int(w * s) if outline else 0)

    def rect(self, x0, y0, x1, y1, fill, r=0, outline=None, w=0):
        s = self.s
        box = [x0 * s, y0 * s, x1 * s, y1 * s]
        if r:
            self.d.rounded_rectangle(box, radius=r * s, fill=fill, outline=outline, width=int(w * s) if outline else 0)
        else:
            self.d.rectangle(box, fill=fill, outline=outline, width=int(w * s) if outline else 0)

    def line(self, pts, fill, w):
        s = self.s
        self.d.line(self.P(pts), fill=fill, width=max(1, int(w * s)), joint="curve")
        for x, y in (pts[0], pts[-1]):
            self.ell(x, y, w / 2, w / 2, fill)

    def arc(self, box, a0, a1, fill, w):
        s = self.s
        self.d.arc([box[0] * s, box[1] * s, box[2] * s, box[3] * s], a0, a1, fill=fill, width=max(1, int(w * s)))

    def chord(self, box, a0, a1, fill, outline=None, w=0):
        s = self.s
        self.d.chord([box[0] * s, box[1] * s, box[2] * s, box[3] * s], a0, a1, fill=fill,
                     outline=outline, width=int(w * s) if outline else 0)

    def grad(self, y0, y1, c0, c1):
        n = max(1, int((y1 - y0) * self.s))
        for i in range(n):
            t = i / max(1, n - 1)
            col = tuple(int(c0[k] + (c1[k] - c0[k]) * t) for k in range(3))
            yy = y0 * self.s + i
            self.d.line([(0, yy), (self.im.width, yy)], fill=col)

    def speck(self, rng, box, colors, n, size=(6, 3)):
        for _ in range(n):
            x, y = rng.uniform(box[0], box[2]), rng.uniform(box[1], box[3])
            self.ell(x, y, size[0] * rng.uniform(.5, 1.4), size[1] * rng.uniform(.5, 1.4), rng.choice(colors))

    def glow(self, cx, cy, r, col, a=120, blur=40):
        layer = Image.new("RGBA", self.im.size, (0, 0, 0, 0))
        s = self.s
        ImageDraw.Draw(layer).ellipse([(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s], fill=tuple(col) + (a,))
        layer = layer.filter(ImageFilter.GaussianBlur(blur * s))
        self.im.paste(layer, (0, 0), layer)

    def teks(self, xy, s, ukuran, fill, path=None, anchor="mm"):
        self.d.text((xy[0] * self.s, xy[1] * self.s), s, font=font(path or FONT, ukuran * self.s), fill=fill, anchor=anchor)

    def done(self):
        return self.im.resize((int(self.w), int(self.h)), Image.LANCZOS)


def gelap(c, f=0.55):
    return tuple(int(v * f) for v in c[:3])


def terang(c, f=0.35):
    return tuple(int(v + (255 - v) * f) for v in c[:3])


def campur(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ================================================================ latar (9 jenis)
# Tiap latar mengembalikan dict:
#   bg (RGB), dep (RGBA benda depan: selimut/meja/sofa, atau None), lantai (y telapak kaki orang berdiri),
#   duduk_y (garis benda depan untuk pose duduk), duduk_x, berdiri_x (opsional), amb (jenis suara latar)
def _selimut(c, warna):
    c.rect(118, 640, 602, 792, warna, r=20, outline=gelap(warna, .5), w=4)
    c.rect(118, 640, 602, 676, terang(warna, .16), r=16)
    for x in (190, 290, 395, 500):
        c.line([(x, 690), (x + 10, 780)], gelap(warna, .8), 3)


def latar_kamar(rng, malam=True):
    c = Cv(W, H, 2, "RGB")
    if malam:
        w0, w1, f0, f1 = (22, 30, 62), (44, 56, 100), (42, 36, 52), (20, 18, 30)
        kayu, sel, bantal, kaca = (74, 50, 46), (50, 68, 120), (186, 194, 222), (14, 18, 40)
    else:
        w0, w1, f0, f1 = (238, 216, 182), (222, 196, 158), (178, 130, 86), (138, 98, 64)
        kayu, sel, bantal, kaca = (150, 100, 66), (96, 150, 190), (250, 246, 238), (196, 228, 250)
    YL = 790
    c.grad(0, YL, w0, w1)
    c.grad(YL, H, f0, f1)
    c.speck(rng, (0, 0, W, YL), [gelap(w1, .93), terang(w0, .04)], 700, (4, 2))
    c.rect(0, YL - 26, W, YL, gelap(w1, .7))
    for y in range(YL + 44, H, 62):
        c.line([(0, y), (W, y)], gelap(f1, .75), 2)
    # jendela + tirai
    c.rect(36, 190, 206, 470, kaca, outline=gelap(w1, .5), w=5)
    if malam:
        c.glow(121, 330, 120, (110, 140, 255), 80, 40)
        c.ell(150, 262, 24, 24, (236, 240, 255))
        c.ell(160, 256, 20, 20, kaca)
    c.line([(121, 190), (121, 470)], gelap(w1, .5), 4)
    c.line([(36, 330), (206, 330)], gelap(w1, .5), 4)
    tirai = gelap(w1, 1.3) if malam else (200, 90, 80)
    c.poly([(20, 170), (84, 170), (64, 500), (20, 500)], tirai, outline=gelap(tirai, .5), w=3)
    c.poly([(160, 170), (222, 170), (222, 500), (178, 500)], tirai, outline=gelap(tirai, .5), w=3)
    # bingkai lukisan
    c.rect(290, 240, 470, 372, gelap(kayu, .9), outline=gelap(kayu, .5), w=4)
    c.rect(302, 252, 458, 360, terang(w1, .2))
    c.poly([(302, 360), (360, 290), (410, 340), (440, 310), (458, 360)], gelap(w1, .85))
    # papan kepala + ranjang
    c.rect(120, 430, 600, 650, kayu, r=24, outline=gelap(kayu, .5), w=4)
    c.rect(140, 452, 580, 640, terang(kayu, .1), r=18)
    c.rect(110, 740, 610, 834, kayu, r=10, outline=gelap(kayu, .5), w=4)
    for px in (245, 475):
        c.rect(px - 84, 560, px + 84, 634, bantal, r=34, outline=gelap(bantal, .5), w=3)
    _selimut(c, sel)
    # nakas + jam digital + lampu
    c.rect(622, 612, 714, 806, kayu, r=8, outline=gelap(kayu, .5), w=4)
    c.rect(634, 650, 702, 716, gelap(kayu, .8), r=4)
    c.ell(668, 683, 5, 5, (220, 190, 120))
    c.rect(636, 566, 700, 606, (10, 10, 12), r=5, outline=(60, 60, 64), w=2)
    c.teks((668, 587), "02:13" if malam else "06:30", 17, (255, 70, 50) if malam else (80, 80, 80), FONT_MONO)
    c.rect(655, 480, 683, 560, gelap(kayu, 1.1) if malam else (230, 200, 120), r=6)
    c.poly([(632, 484), (706, 484), (690, 430), (648, 430)], (226, 206, 150) if not malam else (110, 100, 86),
           outline=gelap(kayu, .5), w=3)
    dep = Cv(W, H, 2, "RGBA")
    _selimut(dep, sel)
    return dict(bg=c.done(), dep=dep.done(), lantai=1020, duduk_y=660, duduk_x=[245, 475],
                berdiri_x=[104, 620, 360], amb="sunyi" if malam else "hangat", kamar=True,
                pindah=[(245, 580), (475, 580)])


def latar_dapur(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 820, (226, 190, 120), (206, 166, 100))
    c.speck(rng, (0, 0, W, 820), [(214, 176, 108), (232, 198, 132), (200, 160, 96)], 700, (5, 2.5))
    for y in range(560, 820, 52):  # keramik
        c.line([(0, y), (W, y)], (178, 140, 84), 2)
    for x in range(0, W, 52):
        c.line([(x, 560), (x, 820)], (178, 140, 84), 2)
    c.grad(820, H, (120, 82, 52), (84, 56, 36))
    c.rect(60, 160, 250, 400, (22, 30, 60), outline=(120, 84, 50), w=7)
    c.line([(155, 160), (155, 400)], (120, 84, 50), 5)
    c.rect(470, 200, 650, 360, (150, 100, 60), outline=(100, 66, 38), w=4)
    for k in range(3):
        c.rect(486, 214 + k * 44, 634, 244 + k * 44, (110, 74, 44), outline=(80, 52, 30), w=2)
    c.line([(360, 0), (360, 150)], (60, 40, 30), 4)
    c.glow(360, 190, 150, (255, 214, 120), 150, 50)
    c.poly([(300, 190), (420, 190), (396, 140), (324, 140)], (244, 190, 80), outline=(120, 80, 30), w=3)
    c.ell(360, 192, 30, 8, (255, 240, 180))

    def meja(cv):
        cv.rect(-10, 860, W + 10, H + 10, (144, 98, 60), outline=(80, 52, 30), w=5)
        cv.rect(-10, 860, W + 10, 888, (170, 120, 74))
        cv.ell(250, 930, 80, 20, (240, 236, 226), outline=(170, 160, 140), w=3)
        cv.ell(250, 922, 52, 11, (214, 160, 84))
        cv.ell(480, 930, 80, 20, (240, 236, 226), outline=(170, 160, 140), w=3)
        cv.ell(480, 922, 52, 11, (200, 80, 60))
        cv.rect(350, 880, 380, 960, (210, 230, 240, 255), r=8, outline=(120, 150, 170), w=3)
    meja(c)
    dep = Cv(W, H, 2, "RGBA")
    meja(dep)
    return dict(bg=c.done(), dep=dep.done(), lantai=840, duduk_y=874, duduk_x=[200, 520, 360], amb="hangat")


def latar_ruang_tamu(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 860, (52, 46, 70), (70, 60, 88))
    c.speck(rng, (0, 0, W, 860), [(46, 40, 62), (76, 66, 94)], 600, (4, 2))
    c.grad(860, H, (60, 44, 40), (34, 26, 26))
    c.glow(360, 430, 260, (110, 150, 255), 80, 70)
    c.rect(200, 320, 520, 520, (20, 22, 30), r=10, outline=(60, 60, 70), w=6)
    c.rect(212, 332, 508, 508, (150, 180, 236), r=4)
    for k in range(40):
        y = 332 + k * 4.4
        c.line([(212, y), (508, y)], (120, 150, 210), 1)
    c.rect(300, 520, 420, 540, (30, 30, 36))
    c.rect(60, 400, 140, 760, (90, 60, 40), outline=(50, 34, 22), w=3)
    c.poly([(30, 400), (170, 400), (140, 320), (60, 320)], (200, 170, 110), outline=(110, 84, 50), w=3)

    def sofa(cv):
        cv.rect(30, 820, 690, 1010, (112, 56, 62), r=36, outline=(60, 28, 34), w=5)
        cv.rect(30, 900, 690, 1060, (136, 70, 76), r=26, outline=(60, 28, 34), w=5)
        cv.line([(360, 910), (360, 1050)], (60, 28, 34), 4)
    sofa(c)
    dep = Cv(W, H, 2, "RGBA")
    c2 = dep
    c2.rect(30, 930, 690, 1060, (136, 70, 76), r=26, outline=(60, 28, 34), w=5)
    c2.line([(360, 940), (360, 1050)], (60, 28, 34), 4)
    return dict(bg=c.done(), dep=dep.done(), lantai=1130, duduk_y=940, duduk_x=[190, 520, 360], amb="sunyi")


def latar_lorong(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, H, (22, 24, 34), (10, 10, 16))
    vx, vy = 360, 560
    c.poly([(0, 0), (240, 330), (240, 800), (0, H)], (40, 44, 62))       # dinding kiri
    c.poly([(W, 0), (480, 330), (480, 800), (W, H)], (36, 40, 58))       # dinding kanan
    c.poly([(0, 0), (W, 0), (480, 330), (240, 330)], (26, 28, 42))       # plafon
    c.poly([(0, H), (W, H), (480, 800), (240, 800)], (58, 44, 40))       # lantai
    c.rect(240, 330, 480, 800, (60, 70, 96))                              # ujung lorong
    c.glow(360, 560, 150, (200, 220, 255), 120, 50)
    for k in range(1, 7):
        t = k / 7
        c.line([(240 * t, 330 * t + 800 * 0 + (H * (1 - t)) * 0), (240 * t, H - (H - 800) * t)], (30, 32, 46), 2)
    c.poly([(40, 250), (150, 330), (150, 760), (40, 900)], (84, 60, 48), outline=(40, 28, 22), w=3)    # pintu kiri
    c.poly([(680, 250), (570, 330), (570, 760), (680, 900)], (84, 60, 48), outline=(40, 28, 22), w=3)  # pintu kanan
    c.ell(130, 560, 6, 6, (220, 190, 100))
    c.ell(590, 560, 6, 6, (220, 190, 100))
    c.rect(286, 420, 434, 780, (50, 56, 80), outline=(28, 32, 48), w=3)
    c.ell(300, 40, 3, 3, (0, 0, 0))
    c.glow(360, 60, 160, (255, 236, 170), 70, 50)
    return dict(bg=c.done(), dep=None, lantai=1120, amb="sunyi")


def latar_luar(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 700, (8, 12, 40), (48, 56, 110))
    for _ in range(50):
        x, y, r = rng.uniform(0, W), rng.uniform(0, 420), rng.uniform(1, 2.4)
        c.ell(x, y, r, r, (230, 236, 255))
    c.ell(590, 170, 52, 52, (250, 246, 220))
    c.ell(606, 160, 46, 46, (8, 12, 40))
    c.rect(90, 380, 560, 790, (68, 66, 90), outline=(30, 28, 44), w=4)
    c.poly([(60, 390), (325, 250), (590, 390)], (96, 50, 56), outline=(40, 20, 24), w=4)
    for x, y, on in [(150, 480, True), (330, 480, False), (470, 480, False), (150, 650, False), (470, 650, True)]:
        c.rect(x, y, x + 90, y + 100, (255, 206, 100) if on else (26, 30, 52), outline=(30, 28, 44), w=4)
        if on:
            c.glow(x + 45, y + 50, 80, (255, 200, 90), 70, 30)
    c.rect(250, 650, 330, 790, (60, 40, 34), outline=(30, 20, 18), w=3)
    c.ell(318, 724, 5, 5, (230, 190, 80))
    c.grad(790, H, (30, 38, 52), (16, 20, 30))
    c.rect(0, 800, W, 830, (70, 72, 88))
    for x in range(10, W, 46):
        c.rect(x, 780, x + 14, 840, (50, 52, 70), outline=(24, 26, 40), w=2)
    c.rect(612, 330, 628, 860, (50, 52, 66))
    c.rect(560, 322, 660, 340, (50, 52, 66))
    c.glow(560, 346, 150, (255, 226, 140), 110, 40)
    c.ell(560, 346, 14, 10, (255, 244, 200))
    c.speck(rng, (0, 850, W, H), [(24, 30, 44), (44, 54, 72)], 300, (9, 3))
    return dict(bg=c.done(), dep=None, lantai=1100, amb="sunyi")


def latar_rak_buku(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, H, (86, 66, 52), (56, 42, 34))
    c.rect(40, 80, 680, 1120, (96, 62, 40), outline=(52, 32, 20), w=6)
    cols = [(180, 60, 56), (60, 110, 170), (220, 180, 70), (70, 140, 96), (140, 90, 170), (230, 230, 220), (60, 60, 80)]
    for r in range(6):
        y0, y1 = 100 + r * 170, 260 + r * 170
        c.rect(52, y1, 668, y1 + 12, (70, 44, 28))
        x = 60
        while x < 650:
            wb = rng.randint(14, 34)
            hb = rng.randint(100, 150)
            col = rng.choice(cols)
            c.rect(x, y1 - hb, x + wb, y1, col, outline=gelap(col, .55), w=2)
            c.line([(x + 4, y1 - hb + 14), (x + wb - 4, y1 - hb + 14)], terang(col, .4), 2)
            x += wb + 2
    c.rect(440, 360, 484, 392, (10, 10, 12), r=4, outline=(60, 60, 66), w=2)
    c.ell(462, 376, 10, 10, (30, 40, 70), outline=(110, 120, 150), w=2)
    c.ell(458, 372, 3, 3, (210, 230, 255))
    c.grad(1120, H, (110, 80, 56), (76, 54, 38))
    return dict(bg=c.done(), dep=None, lantai=1230, amb="sunyi")


def latar_kamar_mandi(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 830, (196, 226, 226), (168, 204, 206))
    for y in range(0, 830, 60):
        c.line([(0, y), (W, y)], (140, 176, 180), 2)
    for x in range(0, W, 60):
        c.line([(x, 0), (x, 830)], (140, 176, 180), 2)
    c.grad(830, H, (96, 112, 116), (62, 74, 80))
    c.rect(150, 160, 570, 540, (214, 232, 240), r=14, outline=(120, 130, 140), w=10)
    c.poly([(180, 180), (260, 180), (190, 520), (180, 520)], (240, 248, 252))
    c.poly([(380, 190), (420, 190), (340, 520), (300, 520)], (234, 244, 250))
    c.rect(210, 560, 510, 650, (232, 236, 238), r=18, outline=(150, 156, 160), w=4)
    c.rect(340, 540, 380, 566, (150, 156, 164), r=4)
    c.rect(220, 650, 500, 830, (214, 218, 220), outline=(150, 156, 160), w=4)
    c.rect(560, 250, 716, 830, (120, 150, 190, 255), outline=(70, 90, 120), w=4)
    for x in range(580, 716, 34):
        c.line([(x, 260), (x, 820)], (90, 120, 160), 3)
    c.glow(360, 60, 170, (230, 240, 255), 90, 40)
    return dict(bg=c.done(), dep=None, lantai=1060, amb="sunyi")


def latar_gelap(rng):
    c = Cv(W, H, 1, "RGB")
    c.grad(0, H, (4, 4, 8), (0, 0, 0))
    return dict(bg=c.done(), dep=None, lantai=1000, amb="sunyi")


BUAT_LATAR = {
    "kamar_malam": lambda r: latar_kamar(r, True), "kamar_pagi": lambda r: latar_kamar(r, False),
    "dapur": latar_dapur, "ruang_tamu": latar_ruang_tamu, "lorong": latar_lorong, "luar_malam": latar_luar,
    "rak_buku": latar_rak_buku, "kamar_mandi": latar_kamar_mandi, "gelap": latar_gelap,
}
_BG = {}


def ambil_latar(nama):
    if nama not in _BG:
        d = BUAT_LATAR[nama](random.Random(len(nama) * 131 + 7))
        d["blur"] = d["bg"].filter(ImageFilter.GaussianBlur(9))
        _BG[nama] = d
    return _BG[nama]


# ================================================================ karakter kartun
KULIT = {"sawo": (212, 142, 86), "terang": (238, 186, 140), "gelap": (148, 96, 62)}
BAJU = {"abu": (112, 122, 142), "putih": (240, 238, 232), "biru": (66, 110, 190), "merah": (196, 56, 60),
        "hijau": (70, 150, 96), "hitam": (52, 54, 64), "kuning": (230, 188, 62), "cokelat": (140, 94, 62),
        "ungu": (132, 86, 172), "oranye": (228, 128, 52)}
RAMBUT = ["pendek", "panjang", "hijab", "kuncir", "botak"]
HITAM_RAMBUT = (32, 24, 30)
R = 85
LW, LH, CX, FEET = 480, 760, 240, 722
YT = FEET - 16 - 150 - 190
HW, HH = 300, 400
HCX, HCY = 150, 195
SSK = 3


class Karakter:
    def __init__(self, spec):
        self.spec = spec
        self.nama = spec["nama"]
        self.kulit = KULIT.get(spec.get("kulit"), KULIT["sawo"])
        self.baju = BAJU.get(spec.get("baju"), BAJU["abu"])
        self.wanita = spec.get("gender") == "wanita"
        self.rambut = spec.get("rambut") if spec.get("rambut") in RAMBUT else "pendek"
        self.kacamata = bool(spec.get("kacamata"))
        self.celana = (58, 64, 92) if not self.wanita else gelap(self.baju, .72)
        self.scarf = terang(self.baju, .45) if self.baju != BAJU["putih"] else (222, 214, 200)
        self.badan = self._badan()
        self.lengan = self._lengan()
        self.cache_hi, self.cache = {}, {}

    # ---------- badan & lengan
    def _badan(self):
        sk, sh = self.kulit, self.baju
        c = Cv(LW, LH, SSK)
        ol = gelap(sh, .5)
        if self.wanita:
            rok = self.celana
            c.poly([(CX - 66, YT + 110), (CX + 66, YT + 110), (CX + 100, FEET - 16), (CX - 100, FEET - 16)], rok,
                   outline=gelap(rok, .5), w=3.5)
            c.ell(CX - 34, FEET - 12, 32, 14, (110, 70, 44), outline=(50, 30, 16), w=2.5)
            c.ell(CX + 34, FEET - 12, 32, 14, (110, 70, 44), outline=(50, 30, 16), w=2.5)
            half = 66
        else:
            for x0, x1 in [(CX - 45, CX - 11), (CX + 11, CX + 45)]:
                c.rect(x0, YT + 170, x1, FEET - 16, self.celana, outline=gelap(self.celana, .5), w=3.5)
            c.ell(CX - 30, FEET - 14, 34, 16, (50, 28, 18), outline=(20, 10, 6), w=2.5)
            c.ell(CX + 30, FEET - 14, 34, 16, (50, 28, 18), outline=(20, 10, 6), w=2.5)
            half = 75
        c.ell(CX, YT + 4, 28, 15, sk, outline=gelap(sk, .5), w=3)
        c.rect(CX - half, YT, CX + half, YT + 190, sh, r=42, outline=ol, w=3.5)
        c.rect(CX - half + 13, YT + 18, CX - half + 39, YT + 150, terang(sh, .22), r=13)
        return c.done()

    def _lengan(self):
        sh, sk = self.baju, self.kulit
        c = Cv(320, 320, SSK)
        c.rect(142, 148, 178, 272, sh, r=18, outline=gelap(sh, .5), w=3.5)
        c.rect(146, 172, 160, 250, terang(sh, .22), r=7)
        so = gelap(sk, .5)
        c.ell(160, 286, 26, 24, sk, outline=so, w=3.5)
        for dx in (-17, 0, 17):
            c.ell(160 + dx, 304, 9, 11, sk, outline=so, w=2.5)
        c.ell(160, 292, 20, 14, sk)
        return c.done()

    # ---------- rambut
    def _rambut_belakang(self, c, cx, cy):
        h = HITAM_RAMBUT
        if self.rambut == "panjang":
            c.rect(cx - R * 1.12, cy - R * 1.05, cx + R * 1.12, cy + R * 2.1, h, r=R * .6, outline=gelap(h, .4), w=3)
        elif self.rambut == "kuncir":
            c.ell(cx + R * 1.12, cy - R * .2, R * .3, R * .55, h, outline=gelap(h, .4), w=3)
        elif self.rambut == "hijab":
            s = self.scarf
            c.ell(cx, cy - R * .04, R * 1.24, R * 1.2, s, outline=gelap(s, .55), w=4)
            c.poly([(cx - R * 1.12, cy + R * .2), (cx + R * 1.12, cy + R * .2), (cx + R * 1.3, cy + R * 1.9),
                    (cx - R * 1.3, cy + R * 1.9)], s, outline=gelap(s, .55), w=4)

    def _rambut_depan(self, c, cx, cy):
        h = HITAM_RAMBUT
        if self.rambut == "hijab":
            s = self.scarf
            c.chord((cx - R * 1.0, cy - R * 1.12, cx + R * 1.0, cy - R * .12), 180, 360, s)
            c.line([(cx - R * .94, cy - R * .56), (cx + R * .94, cy - R * .56)], gelap(s, .55), 3)
            return
        if self.rambut == "botak":
            return
        c.chord((cx - R * 1.05, cy - R * 1.14, cx + R * 1.05, cy - R * .1), 180, 360, h, outline=gelap(h, .4), w=3)
        c.poly([(cx - R * .95, cy - R * .62), (cx - R * .5, cy - R * .3), (cx - R * .1, cy - R * .62),
                (cx + R * .3, cy - R * .34), (cx + R * .95, cy - R * .62)], h)
        if self.rambut == "panjang":
            for sg in (-1, 1):
                c.rect(cx + sg * R * .78 - R * .17, cy - R * .62, cx + sg * R * .78 + R * .17, cy + R * 1.2, h,
                       r=R * .16)

    # ---------- kepala (resolusi tinggi di-cache)
    def kepala_hi(self, emosi, mulut, kedip):
        key = (emosi, mulut, kedip)
        if key in self.cache_hi:
            return self.cache_hi[key]
        sk = campur(self.kulit, (172, 188, 186), .55) if emosi == "seram" else self.kulit
        so = gelap(sk, .45)
        c = Cv(HW, HH, SSK)
        cx, cy = HCX, HCY
        self._rambut_belakang(c, cx, cy)
        if self.rambut != "hijab":
            c.ell(cx - R * .98, cy + 4, 18, 24, sk, outline=so, w=3.5)
            c.ell(cx + R * .98, cy + 4, 18, 24, sk, outline=so, w=3.5)
        c.ell(cx, cy, R, R * .97, sk, outline=so, w=4.5)
        if emosi != "seram":
            blush = (min(255, sk[0] + 8), int(sk[1] * .78), int(sk[2] * .8))
            c.ell(cx - R * .6, cy + R * .34, 15, 10, blush)
            c.ell(cx + R * .6, cy + R * .34, 15, 10, blush)
        ex, ey = R * .38, cy - R * .04
        brow = {"netral": (-.37, -.39), "senang": (-.43, -.41), "kaget": (-.55, -.55), "sedih": (-.47, -.30),
                "marah": (-.29, -.46), "takut": (-.56, -.36), "seram": None}[emosi]
        for sg in (-1, 1):
            x = cx + sg * ex
            if emosi == "seram":
                c.glow(x, ey, 30, (150, 255, 225), 190, 9)
                c.ell(x, ey, 19, 24, (226, 255, 246))
                c.ell(x, ey, 8, 10, (255, 255, 255))
            elif kedip or emosi == "senang":
                if emosi == "senang" and not kedip:
                    c.arc((x - 17, ey - 14, x + 17, ey + 18), 200, 340, (30, 16, 10), 5)
                else:
                    c.line([(x - 17, ey + 3), (x + 17, ey + 3)], (30, 16, 10), 5)
            else:
                rx, ry = (21, 27) if emosi not in ("kaget", "takut") else (25, 32)
                c.ell(x, ey, rx, ry, (255, 255, 255), outline=(40, 20, 12), w=3.5)
                pr = 9 if emosi not in ("kaget", "takut") else 5.5
                c.ell(x + 2, ey + 3, pr, pr + 1, (24, 14, 10))
                c.ell(x + 5, ey - 1, 3, 3, (255, 255, 255))
            if brow:
                ind, luar = brow[0] * R, brow[1] * R
                xi, xo = x - sg * 22, x + sg * 22
                c.line([(xo, cy + luar), ((xi + xo) / 2, cy + min(ind, luar) - 5), (xi, cy + ind)], (60, 26, 12), 6.5)
        if self.kacamata:
            for sg in (-1, 1):
                c.ell(cx + sg * ex, ey, 29, 31, None, outline=(40, 34, 40), w=3.5)
            c.line([(cx - ex + 29, ey), (cx + ex - 29, ey)], (40, 34, 40), 3.5)
        c.arc((cx - 2, cy + R * .14, cx + 22, cy + R * .34), 20, 150, so, 3)
        my = cy + R * .56
        kel = (90, 30, 18)
        if mulut == "tutup":
            if emosi == "senang":
                c.arc((cx - 30, my - 22, cx + 30, my + 18), 20, 160, kel, 5)
            elif emosi == "sedih":
                c.arc((cx - 24, my - 2, cx + 24, my + 30), 200, 340, kel, 5)
            elif emosi == "marah":
                c.line([(cx - 20, my + 6), (cx + 20, my + 2)], kel, 5)
            elif emosi == "kaget":
                c.ell(cx, my + 4, 11, 14, (96, 24, 20), outline=(60, 16, 12), w=2.5)
            elif emosi == "takut":
                c.line([(cx - 20, my + 8), (cx - 10, my + 2), (cx, my + 8), (cx + 10, my + 2), (cx + 20, my + 8)], kel, 4)
            elif emosi == "seram":
                c.line([(cx - 16, my + 8), (cx + 16, my + 8)], (60, 70, 70), 4)
            else:
                c.arc((cx - 24, my - 16, cx + 24, my + 14), 20, 160, kel, 4.5)
        else:
            rx, ry = (15, 11) if mulut == "kecil" else (23, 21)
            if emosi in ("kaget", "takut"):
                ry += 6
            c.ell(cx, my + 6, rx, ry, (110, 24, 22), outline=(60, 16, 12), w=3)
            if mulut == "lebar":
                c.ell(cx, my + ry * .55 + 6, rx * .6, ry * .4, (232, 100, 110))
        self._rambut_depan(c, cx, cy)
        if emosi == "takut":
            c.poly([(cx + R * .86, cy - R * .5), (cx + R * .78, cy - R * .3), (cx + R * .94, cy - R * .3)],
                   (130, 200, 255), outline=(60, 130, 200), w=2)
            c.ell(cx + R * .86, cy - R * .26, 8, 9, (130, 200, 255), outline=(60, 130, 200), w=2)
        self.cache_hi[key] = c.im
        return c.im

    def kepala(self, emosi, mulut, kedip):
        key = (emosi, mulut, kedip)
        if key not in self.cache:
            self.cache[key] = self.kepala_hi(emosi, mulut, kedip).resize((HW, HH), Image.LANCZOS)
        return self.cache[key]

    def gambar(self, t, env, emosi, mulut, kedip):
        """Seluruh badan berdiri, kanvas LW x LH (telapak kaki di FEET)."""
        L = Image.new("RGBA", (LW, LH), (0, 0, 0, 0))
        L.paste(self.badan, (0, 0), self.badan)
        ayun = math.sin(t * 2 * math.pi * 1.25)
        sudut_kiri = -(7 + 4 * ayun + env * 10 * (0.5 + 0.5 * math.sin(t * 7.3 + 1)))
        sudut_kanan = 8 + 3 * ayun + env * 34 * (0.5 + 0.5 * math.sin(t * 2 * math.pi * 1.6))
        half = 66 if self.wanita else 75
        for sx, ang in [(CX - half + 6, sudut_kiri), (CX + half - 6, sudut_kanan)]:
            lg = self.lengan.rotate(ang, resample=Image.BICUBIC)
            L.paste(lg, (int(sx - 160), int(YT + 30 - 160)), lg)
        kep = self.kepala(emosi, mulut, kedip)
        sdt = 2.5 * math.sin(t * 2 * math.pi * .8) + env * 4 * math.sin(t * 2 * math.pi * 2.3)
        kep = kep.rotate(sdt, resample=Image.BICUBIC, center=(HCX, HCY))
        naik = 3 * math.sin(t * 2 * math.pi * 1.25 + 1) + env * 4
        cy = YT + 24 - R
        L.paste(kep, (int(CX - HCX), int(cy - HCY + naik)), kep)
        return L


# ================================================================ pewarnaan, CCTV, efek
def _siapkan_global():
    y, x = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt(((x - W / 2) / (W * .62)) ** 2 + ((y - H / 2) / (H * .62)) ** 2)
    vig = np.clip(1.08 - 0.62 * d ** 2.2, 0.35, 1.0)
    scan = np.ones((H, W), np.float32)
    scan[::3] = 0.8
    rs = np.random.RandomState(3)
    noise = [rs.randn(H, W, 1).astype(np.float32) for _ in range(6)]
    return vig[..., None], scan[..., None], noise


VIG, SCAN, NOISE = _siapkan_global()
LUM = np.array([0.30, 0.59, 0.11], np.float32)
TINT_CCTV = np.array([0.52, 1.0, 0.66], np.float32)


def warnai(frame, f, cctv=False, kedip=1.0, flash=0.0):
    a = np.asarray(frame, dtype=np.float32)
    if cctv:
        g = a @ LUM
        g = np.clip((g - 100) * 1.35 + 118, 0, 255)
        a = g[..., None] * TINT_CCTV
        a = a * SCAN
        a = a * (VIG * 0.55 + 0.45)
        a = a + NOISE[f % 6] * 9
    else:
        a = a * VIG
        a = a + NOISE[f % 6] * 3
    if kedip != 1.0:
        a = a * kedip
    if flash > 0:
        a = a + flash * 255
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def glitch(img, f, kuat=1.0):
    a = np.array(img)
    rs = random.Random(f * 131)
    for _ in range(rs.randint(2, 4)):
        y0 = rs.randint(0, H - 120)
        tinggi = rs.randint(20, 110)
        a[y0:y0 + tinggi] = np.roll(a[y0:y0 + tinggi], int(rs.randint(-44, 44) * kuat), axis=1)
    a[:, :, 0] = np.roll(a[:, :, 0], int(7 * kuat), axis=1)
    a[:, :, 2] = np.roll(a[:, :, 2], int(-7 * kuat), axis=1)
    return Image.fromarray(a)


def format_jam(detik):
    detik = int(detik) % 86400
    return f"{detik // 3600:02d}:{detik % 3600 // 60:02d}:{detik % 60:02d}"


def parse_jam(s):
    try:
        h, m, d = (int(v) for v in s.split(":"))
        return h * 3600 + m * 60 + d
    except Exception:  # noqa: BLE001
        return 2 * 3600 + 13 * 60


def overlay_cctv(img, t, info, zoom=False):
    d = ImageDraw.Draw(img)
    mono = font(FONT_MONO, 26)
    hijau = (150, 255, 180)
    sh = (0, 0, 0)
    if int(t * 1.6) % 2 == 0:
        d.ellipse([46, 128, 66, 148], fill=(255, 40, 40))
    d.text((78, 138), "REC", font=mono, fill=hijau, anchor="lm", stroke_width=2, stroke_fill=sh)
    if info.get("kecepatan", 1) > 1:
        d.text((150, 138), f"▶▶ x{info['kecepatan']}", font=font(FONT_MONO, 22), fill=hijau, anchor="lm",
               stroke_width=2, stroke_fill=sh)
    tgl = info.get("tanggal") or datetime.date.today().strftime("%d-%m-%Y")
    d.text((W - 46, 138), tgl, font=mono, fill=hijau, anchor="rm", stroke_width=2, stroke_fill=sh)
    jam = format_jam(parse_jam(info.get("jam", "02:13:00")) + t * info.get("kecepatan", 1))
    d.text((W - 46, H - 70), jam, font=mono, fill=hijau, anchor="rm", stroke_width=2, stroke_fill=sh)
    d.text((46, H - 70), "CAM 01  " + (info.get("label") or "KAMAR").upper(), font=mono, fill=hijau, anchor="lm",
           stroke_width=2, stroke_fill=sh)
    if zoom:
        d.text((46, 176), "ZOOM x4", font=font(FONT_MONO, 20), fill=hijau, anchor="lm", stroke_width=2, stroke_fill=sh)
    for (x, y, dx, dy) in [(30, 112, 1, 1), (W - 30, 112, -1, 1), (30, H - 50, 1, -1), (W - 30, H - 50, -1, -1)]:
        d.line([(x, y), (x + dx * 34, y)], fill=hijau, width=3)
        d.line([(x, y), (x, y + dy * 34)], fill=hijau, width=3)
    return img


def watermark(img):
    d = ImageDraw.Draw(img)
    f = font(FONT_SERIF, 19)
    kata = KANAL.upper().split(" ", 1)
    y = 44
    for baris in kata:
        x = 42
        for ch in baris:
            d.text((x, y), ch, font=f, fill=(238, 232, 220, 255), anchor="lm", stroke_width=1, stroke_fill=(0, 0, 0))
            x += f.getlength(ch) + 4
        y += 24
    d.line([(42, 30), (60, 30)], fill=(238, 232, 220), width=2)
    return img


def label_nama(img, nama, cx, y):
    d = ImageDraw.Draw(img)
    f = font(FONT, 21)
    lb = f.getlength(nama.upper()) + 26
    d.rounded_rectangle([cx - lb / 2, y - 18, cx + lb / 2, y + 18], radius=18, fill=(250, 250, 246),
                        outline=(30, 30, 36), width=3)
    d.text((cx, y), nama.upper(), font=f, fill=(30, 30, 36), anchor="mm")


# ================================================================ subtitle kata demi kata
SUB_CY = 840


def kelompok(kata):
    out, cur = [], []
    for i, (_, _, k) in enumerate(kata):
        if cur:
            huruf = sum(len(kata[j][2]) for j in cur) + len(cur) + len(k)
            if len(cur) >= 3 or huruf > 15 or kata[cur[-1]][2][-1:] in ",.?!;:":
                out.append(cur)
                cur = []
        cur.append(i)
    if cur:
        out.append(cur)
    return out


class Subtitle:
    def __init__(self, kata):
        self.kata = kata
        self.chunks = kelompok(kata)
        self.chunk_of = {i: n for n, c in enumerate(self.chunks) for i in c}
        self.mulai = [k[0] for k in kata]
        self.akhir = kata[-1][1] if kata else 0
        self.cache = {}

    def sprite(self, n, aktif):
        key = (n, aktif)
        if key in self.cache:
            return self.cache[key]
        idx = self.chunks[n]
        teks = [self.kata[i][2] for i in idx]
        ukuran = 76
        while True:
            f = font(FONT, ukuran)
            lebar = [f.getlength(t) for t in teks]
            spasi = f.getlength(" ")
            total = sum(lebar) + spasi * (len(teks) - 1)
            if total <= 640 or ukuran <= 44:
                break
            ukuran -= 4
        im = Image.new("RGBA", (W, 170), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        x = (W - total) / 2
        for i, t, lb in zip(idx, teks, lebar):
            warna = (255, 196, 40) if i == aktif else (255, 255, 255)
            d.text((x, 85), t, font=f, fill=warna, anchor="lm", stroke_width=9, stroke_fill=(0, 0, 0))
            x += lb + spasi
        self.cache[key] = im
        return im

    def untuk(self, t):
        if not self.kata:
            return None
        i = bisect.bisect_right(self.mulai, t) - 1
        if i < 0:
            return None
        im = self.sprite(self.chunk_of[i], i)
        umur = t - self.mulai[i]
        if umur < 0.12:
            k = 1.18 - 0.18 * (umur / 0.12)
            im = im.resize((int(im.width * k), int(im.height * k)), Image.BILINEAR)
        return im


# ================================================================ satu adegan
def potong(img, cx, cy, z, geser=(0, 0)):
    cw, ch = W / z, H / z
    x0 = min(max(cx - cw / 2 + geser[0], 0), W - cw)
    y0 = min(max(cy - ch / 2 + geser[1], 0), H - ch)
    return img.resize((W, H), Image.BILINEAR, box=(x0, y0, x0 + cw, y0 + ch)), x0, y0


def hash01(a):
    return (math.sin(a * 12.9898) * 43758.5453) % 1.0


class Adegan:
    """Satu adegan siap dirender. `b` hasil validasi naskah; `cast` dict nama->Karakter."""

    def __init__(self, b, cast, total, energi, kata, pengantar):
        self.b, self.cast, self.total, self.energi = b, cast, total, energi
        self.shot, self.efek = b["shot"], set(b.get("efek", []))
        self.bgd = ambil_latar(b["latar"])
        self.sub = Subtitle(kata)
        self.pengantar = pengantar            # nama tokoh yang baru pertama kali tampil -> beri label
        self.cctv = b.get("cctv") or {}
        if self.shot in ("cctv", "cctv_zoom"):
            self.cctv = dict(self.cctv or {"jam": "02:13:00", "kecepatan": 1})
            self.cctv.setdefault("label", {"kamar_malam": "KAMAR", "kamar_pagi": "KAMAR", "ruang_tamu": "RUANG TAMU",
                                           "lorong": "LORONG", "dapur": "DAPUR", "luar_malam": "TERAS",
                                           "rak_buku": "RUANG BACA", "kamar_mandi": "KM"}.get(b["latar"], "KAMAR"))
        rng = random.Random(zlib.crc32(b["narasi"].encode()) % 9999)
        self.kedip_at, tk = [], rng.uniform(0.6, 1.6)
        while tk < total:
            self.kedip_at.append(tk)
            tk += rng.uniform(1.8, 3.2)
        self.tokoh = [t for t in b["tokoh"] if t["nama"] in cast]
        pb = self.bgd
        for t in self.tokoh:
            if t["pose"] == "tidur" and not pb.get("kamar"):
                t["pose"] = "berdiri"
            if t["pose"] == "duduk" and "duduk_y" not in pb:
                t["pose"] = "berdiri"
        if self.shot == "ranjang" and not pb.get("kamar"):
            self.shot = "sedang"

    def _mulut(self, f):
        e = self.energi[f] if f < len(self.energi) else 0.0
        p = self.energi[f - 1] if 0 < f <= len(self.energi) else 0.0
        e2 = 0.5 * e + 0.5 * p
        return "tutup" if e2 < 0.12 else ("kecil" if e2 < 0.42 else "lebar")

    def _env(self, f):
        sl = self.energi[max(0, f - 4):f + 1]
        return min(1.0, sum(sl) / max(1, len(sl))) if f < len(self.energi) else 0.0

    def frame(self, t, f):
        b, shot, bgd = self.b, self.shot, self.bgd
        prog = t / max(self.total, .1)
        geser = (0, 0)
        if "getar" in self.efek or self.shot in ("cctv_zoom",):
            geser = (6 * math.sin(t * 31) + 3 * math.sin(t * 17), 5 * math.sin(t * 27 + 1))
        kz = 1.0
        flash = 0.0
        if "kejut" in self.efek and t < .5:
            kz = 1 + .2 * (1 - t / .5) ** 2
            flash = .55 * max(0, 1 - t / .18) if t < .18 else 0.0
        mulut_bersuara = self._mulut(f)
        env = self._env(f)
        pembicara = b.get("bicara")

        def keadaan(tk):
            if tk["nama"] == pembicara:
                return mulut_bersuara, env
            return "tutup", env * .25

        kedip = any(0 <= t - k < 0.14 for k in self.kedip_at)
        ada = self.tokoh
        if shot in ("closeup", "cctv_zoom"):
            img, _, _ = potong(bgd["blur"], 360, 640, 1.5 * (1 + .03 * prog) * kz, geser)
            if shot == "cctv_zoom":
                img = potong(bgd["blur"], 360, 640, 1.5, geser)[0]
            if ada:
                tk = ada[0]
                kar = self.cast[tk["nama"]]
                mulut, e = keadaan(tk)
                emosi = tk["emosi"] if tk["emosi"] in EMOSI else "netral"
                d = ImageDraw.Draw(img)
                bahu = kar.baju
                naik = 6 * math.sin(t * 2 * math.pi * .9)
                d.ellipse([360 - 380, 1080 + naik, 360 + 380, 1500], fill=bahu, outline=gelap(bahu, .5), width=6)
                hi = kar.kepala_hi(emosi, mulut, kedip and emosi != "seram")
                skala = (3.5 if shot == "closeup" else 3.6) * (1 + .02 * prog) * kz
                tw, th = int(HW * skala), int(HH * skala)
                kep = hi.resize((tw, th), Image.BILINEAR)
                sdt = 1.8 * math.sin(t * 2 * math.pi * .7) + e * 3 * math.sin(t * 2 * math.pi * 2.1)
                kep = kep.rotate(sdt, resample=Image.BICUBIC, center=(HCX * skala, HCY * skala))
                px = int(360 - HCX * skala + geser[0] * 2)
                py = int((560 if shot == "closeup" else 640) - HCY * skala + naik + geser[1] * 2)
                img.paste(kep, (px, py), kep)
        else:
            if shot == "lebar" or shot == "cctv":
                cx, cy, z = 360, 640, 1.0
            elif shot == "ranjang":
                cx, cy, z = 360, 650, 1.7
            else:
                z = 1.5
                xs = self._xs_dunia()
                cx = sum(xs) / len(xs) if xs else 360
                cy = bgd["lantai"] - 330
            z *= (1 + .05 * prog) * kz
            img, x0, y0 = potong(bgd["bg"], cx, cy, z, geser)
            ks = .62 * z
            xs = self._xs_dunia()
            urut = sorted(zip(self.tokoh, xs), key=lambda p: self._y_dunia(p[0]))
            pakai_dep = False
            for tk, xw in urut:
                kar = self.cast[tk["nama"]]
                mulut, e = keadaan(tk)
                emosi = tk["emosi"] if tk["emosi"] in EMOSI else "netral"
                pose = tk["pose"]
                if pose == "tidur":
                    pakai_dep = True
                    px, py = bgd["pindah"][self._idx_bed(tk)]
                    sx, sy = (px - x0) * z, (py - y0) * z
                    hk = kar.kepala(emosi, mulut, kedip)
                    s = .56 * z
                    kep = hk.resize((int(HW * s), int(HH * s)), Image.BILINEAR)
                    kep = kep.rotate(8 * (1 if self._idx_bed(tk) == 0 else -1) + 1.5 * math.sin(t * 1.3),
                                     resample=Image.BICUBIC, center=(HCX * s, HCY * s))
                    d = ImageDraw.Draw(img)
                    d.ellipse([sx - 66 * z, sy + 34 * z, sx + 66 * z, sy + 90 * z], fill=kar.baju,
                              outline=gelap(kar.baju, .5), width=3)
                    img.paste(kep, (int(sx - HCX * s), int(sy - HCY * s)), kep)
                    tinggi_kepala = sy - 90 * z
                else:
                    layer = kar.gambar(t, e, emosi, mulut, kedip)
                    if pose == "duduk":
                        pakai_dep = True
                        sy = (bgd["duduk_y"] + 30 - y0) * z
                        yf = sy + (FEET - 556) * ks
                    else:
                        yf = (bgd["lantai"] - y0) * z
                    sx = (xw - x0) * z
                    pop = 1.0
                    if t < .35:
                        u = t / .35
                        pop = .7 + .3 * u + .06 * math.sin(u * math.pi)
                    k = ks * pop
                    sp = layer.resize((int(LW * k), int(LH * k)), Image.BILINEAR if k < 1.15 else Image.LANCZOS)
                    img.paste(sp, (int(sx - CX * k), int(yf - FEET * k)), sp)
                    tinggi_kepala = yf - (FEET - (YT + 24 - R - R * 1.15)) * k
                if tk["nama"] in self.pengantar and t < 2.6:
                    label_nama(img, tk["nama"], sx, max(70, tinggi_kepala - 26))
            if pakai_dep and bgd.get("dep") is not None:
                dep, _, _ = potong(bgd["dep"], cx, cy, z, geser)
                img.paste(dep, (0, 0), dep)
        # --- pewarnaan & efek
        kedip_lampu = 1.0
        if "kedip" in self.efek:
            kedip_lampu = 0.35 if (t * 5.3) % 1.7 < .35 or hash01(f) < .08 else 1.0
        cctv = shot in ("cctv", "cctv_zoom")
        img = warnai(img, f, cctv=cctv, kedip=kedip_lampu, flash=flash)
        if "glitch" in self.efek and (hash01(f * 3.7) < .28 or (t < .3 and "kejut" in self.efek)):
            img = glitch(img, f, 1.2)
        elif cctv and hash01(f * 1.3) < .05:
            img = glitch(img, f, .5)
        if cctv:
            overlay_cctv(img, t, self.cctv, zoom=shot == "cctv_zoom")
        watermark(img)
        s = self.sub.untuk(t)
        if s:
            img.paste(s, (int((W - s.width) / 2), int(SUB_CY - s.height / 2)), s)
        return img

    def _idx_bed(self, tk):
        urut = [t for t in self.tokoh if t["pose"] == "tidur"]
        return min(urut.index(tk), 1)

    def _xs_dunia(self):
        bgd, xs = self.bgd, []
        n = len(self.tokoh)
        tidur = 0
        for i, tk in enumerate(self.tokoh):
            if tk["pose"] == "tidur":
                xs.append(bgd["pindah"][min(tidur, 1)][0])
                tidur += 1
            elif tk["pose"] == "duduk":
                dx = bgd["duduk_x"]
                xs.append(dx[min(i, len(dx) - 1)])
            else:
                if "berdiri_x" in bgd:
                    xs.append(bgd["berdiri_x"][min(i, len(bgd["berdiri_x"]) - 1)])
                else:
                    xs.append({1: [360], 2: [230, 490], 3: [150, 360, 570]}.get(n, [150, 360, 570])[min(i, 2)])
        return xs

    def _y_dunia(self, tk):
        return 0 if tk["pose"] == "tidur" else (self.bgd.get("duduk_y", 0) if tk["pose"] == "duduk" else self.bgd["lantai"])


# ================================================================ suara latar & render
def _ffmpeg_proc(total, audio, out_path, amb, kejut, kejut_t=0.05):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-"]
    if audio:
        cmd += ["-i", str(audio)]
    else:
        cmd += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "anoisesrc=color=white:amplitude=0.0001:sample_rate=44100"]
    kelas = "brown" if amb == "sunyi" else "pink"
    cmd += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", f"anoisesrc=color={kelas}:amplitude=0.7:sample_rate=44100"]
    f = ["[1:a]aresample=44100,apad[v]"]
    if amb == "sunyi":
        f.append("[2:a]lowpass=f=260,volume=0.13[n]")
    else:
        f.append("[2:a]lowpass=f=900,highpass=f=120,volume=0.20[n]")
    n_in = 2
    mix = "[v][n]"
    if kejut:
        cmd += ["-f", "lavfi", "-t", "1.2", "-i", "anoisesrc=color=brown:amplitude=1.0:sample_rate=44100"]
        f.append(f"[3:a]lowpass=f=380,afade=t=out:st=0.08:d=1.0,volume=3.2,adelay={int(kejut_t * 1000)}|{int(kejut_t * 1000)}[s]")
        mix += "[s]"
        n_in = 3
    f.append(f"{mix}amix=inputs={n_in}:duration=first:dropout_transition=0,volume={n_in * .95:.2f},alimiter=limit=0.95[a]")
    cmd += ["-filter_complex", ";".join(f), "-map", "0:v", "-map", "[a]", "-t", f"{total:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-maxrate", "3500k", "-bufsize", "7000k",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k", str(out_path)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def _selesai(proc):
    try:
        proc.stdin.close()
    except BrokenPipeError:
        pass
    sisa = proc.stderr.read().decode(errors="ignore")
    if proc.wait() != 0:
        raise RuntimeError("FFmpeg gagal: " + sisa[-700:])


def render_segmen(b, cast, audio, kata_server, out_path, pengantar, ekor=0.3):
    dur = durasi(audio)
    total = dur + ekor
    energi = baca_energi(audio)
    n_kata = len(b["narasi"].split())
    kata = kata_server if len(kata_server) >= 0.6 * n_kata else perkirakan_kata(b["narasi"], energi)
    ad = Adegan(b, cast, total, energi, kata, pengantar)
    proc = _ffmpeg_proc(total, audio, out_path, ad.bgd.get("amb", "sunyi"), "kejut" in ad.efek)
    try:
        for f in range(int(total * FPS) + 1):
            proc.stdin.write(ad.frame(f / FPS, f).tobytes())
    except BrokenPipeError:
        pass
    _selesai(proc)


def render_kartu_akhir(info, out_path, durasi_s=3.6):
    """Kartu penutup: 'BERSAMBUNG... PART N' (atau 'TAMAT')."""
    proc = _ffmpeg_proc(durasi_s, None, out_path, "sunyi", True, kejut_t=0.3)
    judul = info.get("judul", "BERSAMBUNG…")
    besar = info.get("besar", "PART 2")
    kecil = info.get("kecil", "")
    rec = info.get("rec", "02:13:00")
    try:
        for f in range(int(durasi_s * FPS) + 1):
            t = f / FPS
            im = Image.new("RGB", (W, H), (0, 0, 0))
            d = ImageDraw.Draw(im)

            def fade(t0, t1=.6):
                return max(0.0, min(1.0, (t - t0) / t1))

            def spasi(y, s, fnt, warna, jarak, a):
                lebar = sum(fnt.getlength(ch) + jarak for ch in s) - jarak
                x = (W - lebar) / 2
                for ch in s:
                    d.text((x, y), ch, font=fnt, fill=tuple(int(v * a) for v in warna), anchor="lm")
                    x += fnt.getlength(ch) + jarak

            a = fade(0.1)
            fr = font(FONT_MONO, 22)
            teks_rec = f"REC {rec}"
            lebar_rec = sum(fr.getlength(ch) + 5 for ch in teks_rec) - 5
            if int(t * 1.6) % 2 == 0:
                x_dot = (W - lebar_rec) / 2 - 30
                d.ellipse([x_dot, 508, x_dot + 16, 524], fill=(int(220 * a), int(40 * a), int(40 * a)))
            spasi(516, teks_rec, fr, (210, 60, 60), 5, a)
            spasi(618, judul.upper(), font(FONT_SERIF, 34), (236, 230, 220), 8, fade(0.5))
            spasi(700, besar.upper(), font(FONT_SERIF, 60), (232, 170, 60), 6, fade(1.0))
            if kecil:
                spasi(790, kecil.upper(), font(FONT, 18), (130, 124, 116), 4, fade(1.5))
            im = warnai(im, f, cctv=False)
            if "glitch" in info.get("efek", []) and hash01(f) < .1:
                im = glitch(im, f, .6)
            watermark(im)
            proc.stdin.write(im.tobytes())
    except BrokenPipeError:
        pass
    _selesai(proc)
