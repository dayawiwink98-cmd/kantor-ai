"""Kantor AI Shorts - versi animasi kartun (tanpa GPU, tanpa gambar dari internet).
Alur: topik -> naskah (Gemini) -> suara (edge-tts) -> animasi kartun (Pillow: karakter,
      latar, mulut sinkron suara, subtitle kata demi kata) -> video (FFmpeg) -> upload YouTube.
"""
import asyncio
import bisect
import csv
import json
import math
import os
import random
import subprocess
import sys
import time
from array import array
from pathlib import Path

import edge_tts
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
# Daftar model dicoba berurutan; bila satu ditolak (404/429), lanjut ke berikutnya.
# Bisa diganti lewat variabel GEMINI_MODEL (pisahkan dengan koma).
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODEL", "gemini-3.5-flash,gemini-3.1-flash-lite,gemini-2.5-flash-lite").split(",") if m.strip()]
VOICE = os.environ.get("TTS_VOICE", "id-ID-ArdiNeural")  # alternatif: id-ID-GadisNeural
PRIVACY = os.environ.get("YT_PRIVACY", "private")  # private / unlisted / public
UPLOAD = os.environ.get("UPLOAD", "1") == "1"
W, H, FPS = 720, 1280, 30
FONT = os.environ.get("FONT_PATH", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
WORK = Path("work")
WORK.mkdir(exist_ok=True)

LATAR = ["sawah", "candi", "kota", "laut", "gurun", "hutan", "medan", "malam", "gunung"]
TOKOH_LIST = ["petani", "raja", "prajurit", "pelaut", "cendekiawan", "rakyat"]
EMOSI = ["netral", "senang", "kaget", "sedih", "marah"]


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# ---------------------------------------------------------------- topik & naskah
def pilih_topik():
    with open("topics.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if (r.get("status") or "").strip() != "selesai":
            return rows, r
    sys.exit("Semua topik sudah selesai. Tambahkan topik baru di topics.csv")


def simpan_topik(rows):
    with open("topics.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["topik", "status"])
        w.writeheader()
        for r in rows:
            w.writerow({"topik": r["topik"], "status": r.get("status") or ""})


def tulis_naskah(topik):
    prompt = f"""Kamu penulis naskah YouTube Shorts animasi kartun berbahasa Indonesia (bercerita santai, seru, mudah dipahami).
Topik: {topik}

Aturan narasi:
- Total narasi 90-110 kata (sekitar 40 detik dibacakan).
- Bagian pertama adalah hook 1 kalimat yang mengejutkan atau memancing rasa penasaran. Jangan mulai dengan "Tahukah kamu".
- Hanya gunakan fakta sejarah yang akurat dan umum diketahui. Jika tidak yakin soal angka, tanggal, atau nama, jangan disebutkan.
- Jika topik adalah legenda atau cerita rakyat, ceritakan sebagai legenda ("konon", "menurut cerita"), bukan fakta sejarah.
- Akhiri dengan 1 kalimat penutup yang mengajak penonton berkomentar.
- Bagi menjadi 6-8 bagian, tiap bagian 1-2 kalimat pendek.

Setiap bagian juga menentukan tampilan animasinya:
- "latar" salah satu dari: {", ".join(LATAR)}
  (sawah=desa/pertanian, candi=candi/kerajaan Nusantara, kota=kota kuno/benteng/istana Eropa-Timur Tengah,
   laut=kapal/pelayaran, gurun=Mesir/Timur Tengah, hutan=hutan/pegunungan, medan=perang/pertempuran,
   malam=malam hari/misteri/sedih, gunung=gunung berapi/bencana)
- "tokoh" salah satu dari: {", ".join(TOKOH_LIST)}
  (petani, raja=raja/bangsawan, prajurit, pelaut=penjelajah/pelaut, cendekiawan=ilmuwan/ulama/pujangga, rakyat=orang biasa)
- "emosi" salah satu dari: {", ".join(EMOSI)} (sesuaikan dengan isi kalimat; hook biasanya "kaget")

Balas HANYA JSON dengan bentuk:
{{"judul": "maks 70 karakter", "deskripsi": "2 kalimat", "tag": ["..."],
  "bagian": [{{"narasi": "...", "latar": "...", "tokoh": "...", "emosi": "..."}}]}}"""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.8},
    }
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for percobaan in range(2):
            try:
                r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=120)
                if r.status_code in (404, 429):  # model tidak ada / kuota habis -> coba model lain
                    print(f"Model {model} ditolak (HTTP {r.status_code}), coba model berikutnya")
                    break
                r.raise_for_status()
                bagian = r.json()["candidates"][0]["content"]["parts"]
                teks = "".join(p.get("text", "") for p in bagian if not p.get("thought"))
                data = json.loads(teks)
                assert data["bagian"], "bagian kosong"
                for b in data["bagian"]:  # rapikan pilihan yang tidak dikenal
                    assert b["narasi"].strip(), "narasi kosong"
                    if b.get("latar") not in LATAR:
                        b["latar"] = "sawah"
                    if b.get("tokoh") not in TOKOH_LIST:
                        b["tokoh"] = "rakyat"
                    if b.get("emosi") not in EMOSI:
                        b["emosi"] = "netral"
                print("Naskah dibuat dengan model:", model)
                return data
            except Exception as e:  # noqa: BLE001
                print(f"Naskah gagal ({model}, percobaan {percobaan + 1}): {e}")
                time.sleep(5)
    sys.exit("Gagal membuat naskah.")


# ---------------------------------------------------------------- suara
async def _tts(teks, path):
    try:
        com = edge_tts.Communicate(teks, VOICE, rate="+5%", boundary="WordBoundary")
    except TypeError:  # edge-tts versi lama
        com = edge_tts.Communicate(teks, VOICE, rate="+5%")
    kata = []
    with open(path, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                mulai = ch["offset"] / 1e7
                kata.append((mulai, mulai + ch["duration"] / 1e7, ch["text"]))
    return kata


def suara(teks, path):
    """Mengembalikan daftar (mulai, selesai, kata) bila server memberi waktu per kata, kalau tidak daftar kosong."""
    for percobaan in range(3):
        try:
            return asyncio.run(_tts(teks, str(path)))
        except Exception as e:  # noqa: BLE001
            print(f"Suara gagal (percobaan {percobaan + 1}): {e}")
            time.sleep(3)
    sys.exit("Gagal membuat suara.")


def durasi(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def baca_energi(path):
    """Kekuatan suara per frame (0..1) untuk menggerakkan mulut."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        check=True, capture_output=True,
    ).stdout
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
    """Bagi waktu bicara ke tiap kata sebanding panjangnya (cadangan bila server tak memberi waktu per kata)."""
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


# ---------------------------------------------------------------- kanvas gambar (supersampling)
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

    def speck(self, rng, region, colors, n, size=(6, 3)):
        s = self.s
        m = None
        if region[0] == "poly":
            m = Image.new("L", self.im.size, 0)
            ImageDraw.Draw(m).polygon(self.P(region[1]), fill=255)
            xs, ys = [p[0] for p in region[1]], [p[1] for p in region[1]]
            box = (max(0, min(xs)), max(0, min(ys)), min(self.w, max(xs)), min(self.h, max(ys)))
        else:
            box = region
        cnt = tries = 0
        while cnt < n and tries < n * 8:
            tries += 1
            x, y = rng.uniform(box[0], box[2]), rng.uniform(box[1], box[3])
            if m is not None:
                px, py = min(int(x * s), m.width - 1), min(int(y * s), m.height - 1)
                if m.getpixel((px, py)) == 0:
                    continue
            self.ell(x, y, size[0] * rng.uniform(.5, 1.4), size[1] * rng.uniform(.5, 1.4), rng.choice(colors))
            cnt += 1

    def glow(self, cx, cy, r, col, a=120, blur=40):
        layer = Image.new("RGBA", self.im.size, (0, 0, 0, 0))
        s = self.s
        ImageDraw.Draw(layer).ellipse([(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s], fill=tuple(col) + (a,))
        layer = layer.filter(ImageFilter.GaussianBlur(blur * s))
        self.im.paste(layer, (0, 0), layer)

    def done(self):
        return self.im.resize((int(self.w), int(self.h)), Image.LANCZOS)


def gelap(c, f=0.55):
    return tuple(int(v * f) for v in c[:3])


def terang(c, f=0.35):
    return tuple(int(v + (255 - v) * f) for v in c[:3])


# ---------------------------------------------------------------- elemen latar
def awan(w, h, warna=(255, 255, 255), a=235):
    c = Cv(w, h, 2)
    f = tuple(warna) + (a,)
    c.ell(w * .3, h * .62, w * .22, h * .3, f)
    c.ell(w * .52, h * .45, w * .24, h * .42, f)
    c.ell(w * .72, h * .62, w * .22, h * .32, f)
    c.rect(w * .12, h * .6, w * .88, h * .92, f, r=h * .18)
    return c.done()


def pohon(c, x, dasar, tinggi, hijau=((70, 140, 50), (50, 115, 40), (90, 160, 60)), batang=(105, 48, 28)):
    lb = tinggi * .09
    c.rect(x - lb, dasar - tinggi * .55, x + lb, dasar, batang, outline=gelap(batang, .6), w=2.5)
    c.rect(x - lb * .3, dasar - tinggi * .5, x + lb * .1, dasar - 4, tuple(min(255, v + 25) for v in batang))
    r = tinggi * .3
    cy = dasar - tinggi * .7
    ol = gelap(hijau[1], .55)
    for dx, dy, k, col in [(-.7, .2, .75, hijau[1]), (.7, .2, .75, hijau[1]), (0, -.1, 1.0, hijau[0]),
                           (-.4, -.45, .7, hijau[0]), (.45, -.4, .7, hijau[2])]:
        c.ell(x + dx * r, cy + dy * r, r * k, r * k * .9, col, outline=ol, w=3)
    for dx, dy in [(-.3, .0), (.35, -.25), (0, .35), (-.2, -.5)]:
        c.ell(x + dx * r, cy + dy * r, r * .3, r * .22, hijau[1])


def padi(c, x, y, h):
    for dx in (-.14, 0, .14):
        c.line([(x, y), (x + dx * h * .7, y - h * .55), (x + dx * h * 1.5, y - h)], (62, 142, 44), max(2, h * .07))
    for i in range(6):
        c.ell(x + math.sin(i * 1.7) * h * .05, y - h * (.5 + i * .08), h * .05, h * .04, (228, 192, 48),
              outline=(150, 110, 20), w=1)


def rumput(c, x, y, h, col=(40, 110, 40)):
    for k in range(-3, 4):
        c.poly([(x + k * h * .08 - 3, y), (x + k * h * .35, y - h * (1 - abs(k) * .1)), (x + k * h * .08 + 3, y)], col)


def palem(c, x, dasar, h):
    c.line([(x, dasar), (x + h * .08, dasar - h * .5), (x + h * .03, dasar - h)], (110, 70, 40), h * .06)
    ux, uy = x + h * .03, dasar - h
    for dx, dy in [(-.45, .1), (-.3, -.1), (-.1, -.2), (.15, -.2), (.35, -.08), (.5, .12)]:
        c.line([(ux, uy), (ux + dx * h * .5, uy + dy * h * .3 - h * .08), (ux + dx * h, uy + dy * h * .55 + h * .12)],
               (50, 135, 60), h * .045)


# ---------------------------------------------------------------- latar (9 jenis)
def latar_sawah(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 600, (238, 178, 120), (252, 232, 190))
    c.glow(540, 330, 150, (255, 240, 190), 120, 50)
    c.poly([(0, 520), (120, 470), (260, 510), (420, 455), (600, 505), (720, 470), (720, 600), (0, 600)], (172, 176, 122))
    c.poly([(0, 555), (180, 520), (380, 550), (560, 515), (720, 545), (720, 620), (0, 620)], (122, 168, 84))
    c.grad(580, 1280, (138, 190, 70), (96, 168, 46))
    c.speck(rng, (0, 600, 720, 1280), [(110, 170, 50), (160, 205, 90), (86, 150, 40)], 800, (7, 3))
    for y in range(610, 930, 34):
        k = .45 + (y - 610) / 330
        for x in range(-20, 760, int(54 * k)):
            padi(c, x + rng.uniform(-8, 8), y + rng.uniform(-4, 4), 70 * k)
    pohon(c, 620, 700, 330)
    c.rect(150, 0, 190, 640, (112, 56, 30), outline=(70, 32, 16), w=3)
    jalan = [(720, 880), (560, 910), (330, 990), (0, 1030), (0, 1280), (720, 1280)]
    c.poly(jalan, (204, 114, 52))
    c.speck(rng, ("poly", jalan), [(176, 92, 40), (226, 136, 74), (190, 104, 46)], 260, (10, 4))
    for x, y, h in [(60, 1190, 110), (560, 1230, 130), (330, 1140, 70)]:
        rumput(c, x, y, h)
    return dict(bg=c.done(), awan=[(awan(240, 90), -100, 120, 14), (awan(180, 70), 400, 230, 9)])


def latar_candi(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 640, (110, 185, 235), (225, 240, 250))
    c.poly([(0, 560), (150, 500), (320, 550), (500, 490), (720, 545), (720, 660), (0, 660)], (92, 160, 100))
    c.poly([(0, 600), (220, 560), (460, 600), (720, 570), (720, 680), (0, 680)], (70, 140, 80))
    c.grad(640, 1280, (126, 174, 72), (88, 142, 56))
    c.speck(rng, (0, 660, 720, 1280), [(100, 160, 60), (150, 200, 90), (70, 125, 45)], 600, (7, 3))
    cx, dasar = 360, 690
    for i, lebar in enumerate([540, 450, 360, 270, 180]):
        y1, y0 = dasar - i * 56, dasar - (i + 1) * 56
        warna = (150, 140, 128) if i % 2 == 0 else (166, 156, 142)
        c.rect(cx - lebar / 2, y0, cx + lebar / 2, y1, warna, outline=gelap(warna, .55), w=2.5)
        c.rect(cx - lebar / 2 - 6, y0 - 6, cx + lebar / 2 + 6, y0 + 6, terang(warna, .25), outline=gelap(warna, .55), w=2)
        for k in range(int(lebar / 56)):
            nx = cx - lebar / 2 + 34 + k * 56
            if abs(nx - cx) > 36 and nx < cx + lebar / 2 - 20:
                c.rect(nx - 14, y0 + 16, nx + 14, y1 - 8, gelap(warna, .55), r=10)
    top = dasar - 5 * 56
    for dx, ys in [(-110, top + 10), (110, top + 10), (-70, top - 4), (70, top - 4)]:
        pass
    c.ell(cx, top - 38, 62, 52, (140, 130, 118), outline=(80, 72, 64), w=3)
    c.rect(cx - 8, top - 118, cx + 8, top - 70, (140, 130, 118), outline=(80, 72, 64), w=2.5)
    c.ell(cx, top - 122, 12, 12, (140, 130, 118), outline=(80, 72, 64), w=2.5)
    for dx in (-130, 130, -80, 80):
        c.ell(cx + dx, dasar - 3 * 56 - 24, 26, 22, (156, 146, 132), outline=(80, 72, 64), w=2.5)
    c.poly([(cx - 50, dasar), (cx + 50, dasar), (cx + 26, top + 50), (cx - 26, top + 50)], (196, 186, 168))
    for k in range(1, 10):
        yy = dasar - k * (dasar - top - 50) / 10
        c.line([(cx - 48 + k * 2.2, yy), (cx + 48 - k * 2.2, yy)], (150, 140, 124), 2)
    jalan = [(300, 690), (420, 690), (720, 1000), (720, 1280), (0, 1280), (0, 1000)]
    c.poly(jalan, (210, 188, 140))
    c.speck(rng, ("poly", jalan), [(190, 168, 120), (226, 206, 160)], 260, (9, 4))
    palem(c, 70, 780, 380)
    palem(c, 660, 760, 330)
    return dict(bg=c.done(), awan=[(awan(240, 90), -100, 90, 12), (awan(190, 70), 380, 210, 8)])


def latar_kota(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 640, (110, 165, 230), (228, 238, 250))
    c.ell(360, 500, 150, 120, (170, 120, 100), outline=(100, 66, 56), w=3)
    c.rect(210, 500, 510, 640, (214, 190, 150), outline=(130, 106, 80), w=3)
    for dx in (-190, 190):
        c.rect(360 + dx - 18, 330, 360 + dx + 18, 640, (226, 204, 168), outline=(130, 106, 80), w=3)
        c.poly([(360 + dx - 24, 330), (360 + dx + 24, 330), (360 + dx, 270)], (150, 70, 60), outline=(100, 40, 36), w=2.5)
    c.rect(0, 600, 720, 800, (226, 202, 156), outline=(140, 114, 84), w=3)
    for x in range(0, 740, 40):
        c.rect(x, 584, x + 24, 604, (226, 202, 156), outline=(140, 114, 84), w=2)
    for x in range(40, 720, 120):
        c.rect(x - 34, 660, x + 34, 800, (120, 86, 66), r=0)
        c.ell(x, 660, 34, 34, (120, 86, 66))
        c.rect(x - 24, 668, x + 24, 800, (60, 40, 34))
        c.ell(x, 668, 24, 26, (60, 40, 34))
    c.grad(800, 1280, (204, 180, 136), (166, 138, 100))
    for k in range(0, 9):
        yy = 800 + (k ** 1.6) * 7
        c.line([(0, yy), (720, yy)], (150, 124, 90), 2)
    for x in range(-600, 1300, 120):
        c.line([(360 + (x - 360) * .2, 800), (x, 1280)], (150, 124, 90), 2)
    c.speck(rng, (0, 800, 720, 1280), [(150, 124, 90), (220, 198, 154)], 300, (6, 2.5))
    return dict(bg=c.done(), awan=[(awan(220, 80), -100, 100, 11), (awan(170, 64), 420, 200, 7)])


def latar_laut(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 600, (126, 196, 240), (236, 246, 255))
    c.glow(520, 440, 130, (255, 250, 210), 150, 40)
    c.ell(520, 480, 60, 60, (255, 246, 200))
    c.grad(580, 880, (70, 160, 210), (14, 84, 140))
    c.poly([(150, 580), (200, 560), (250, 580)], (70, 60, 60))
    c.rect(198, 530, 202, 575, (70, 60, 60))
    c.poly([(202, 536), (240, 568), (202, 568)], (245, 235, 205))
    c.rect(36, 330, 74, 900, (120, 76, 40), outline=(70, 42, 20), w=3)
    c.poly([(74, 360), (350, 440), (320, 700), (74, 780)], (246, 236, 208), outline=(160, 140, 100), w=3)
    for k in range(1, 5):
        c.line([(74, 360 + k * 105), (340 - k * 4, 440 + k * 62)], (200, 184, 150), 2)
    c.line([(74, 330), (420, 600)], (80, 60, 40), 3)
    c.rect(0, 870, 720, 1280, (156, 104, 56))
    for k, y in enumerate(range(870, 1280, 46)):
        c.line([(0, y), (720, y)], (100, 62, 30), 3)
        for x in range(-60 + (k % 2) * 90, 760, 180):
            c.line([(x, y), (x, y + 46)], (110, 70, 34), 2)
    c.speck(rng, (0, 880, 720, 1280), [(130, 84, 44), (176, 122, 70)], 250, (8, 2))
    c.rect(0, 840, 720, 872, (130, 84, 44), outline=(80, 50, 24), w=3)
    for x in range(30, 720, 90):
        c.rect(x - 8, 800, x + 8, 872, (140, 92, 50), outline=(80, 50, 24), w=2.5)
    wv = Cv(W, 44, 2)
    for x in range(0, W, 80):
        wv.arc((x, 8, x + 80, 60), 200, 340, (255, 255, 255, 170), 4)
    return dict(bg=c.done(), awan=[(awan(240, 90), -100, 90, 16), (awan(200, 72), 420, 210, 10)],
                ombak=[(wv.done(), y, v) for y, v in [(630, 26), (700, 38), (770, 52), (830, 66)]])


def latar_gurun(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 640, (250, 196, 118), (255, 238, 196))
    c.glow(540, 300, 150, (255, 240, 200), 140, 50)
    c.ell(540, 300, 80, 80, (255, 244, 214))
    for x, w, h in [(120, 200, 150), (300, 140, 100), (610, 220, 170)]:
        c.poly([(x - w / 2, 640), (x, 640 - h), (x + w / 2, 640)], (214, 170, 100), outline=(160, 120, 64), w=2.5)
        c.poly([(x, 640 - h), (x + w / 2, 640), (x + 10, 640)], (186, 142, 78))
    c.poly([(0, 640), (200, 600), (420, 640), (720, 590), (720, 720), (0, 720)], (232, 188, 112))
    c.grad(680, 1280, (228, 182, 108), (206, 160, 88))
    c.speck(rng, (0, 700, 720, 1280), [(196, 150, 80), (240, 202, 130)], 600, (8, 2.5))
    for x, y, h in [(80, 1000, 190), (640, 1090, 150)]:
        c.rect(x - 12, y - h, x + 12, y, (70, 140, 70), r=12, outline=(40, 90, 40), w=2.5)
        c.rect(x - 38, y - h * .6, x - 22, y - h * .3, (70, 140, 70), r=8, outline=(40, 90, 40), w=2.5)
        c.rect(x - 38, y - h * .35, x - 10, y - h * .28, (70, 140, 70), r=4)
        c.rect(x + 22, y - h * .75, x + 38, y - h * .45, (70, 140, 70), r=8, outline=(40, 90, 40), w=2.5)
        c.rect(x + 10, y - h * .5, x + 38, y - h * .43, (70, 140, 70), r=4)
    return dict(bg=c.done(), awan=[(awan(200, 60, (255, 250, 235), 200), -100, 140, 10)])


def latar_hutan(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 640, (170, 220, 200), (236, 246, 220))
    c.glow(300, 200, 170, (255, 255, 210), 140, 60)
    for dasar, tinggi, col, jarak in [(610, 190, (96, 150, 110), 90), (660, 270, (64, 124, 80), 130)]:
        for x in range(-30, 780, jarak):
            pohon(c, x + rng.uniform(-14, 14), dasar, tinggi * rng.uniform(.85, 1.15),
                  hijau=(col, gelap(col, .85), terang(col, .12)), batang=(96, 56, 34))
    c.grad(640, 1280, (96, 156, 64), (60, 118, 44))
    c.speck(rng, (0, 660, 720, 1280), [(70, 130, 46), (120, 176, 76), (50, 100, 36)], 700, (7, 3))
    jalan = [(330, 660), (400, 660), (720, 1040), (720, 1280), (0, 1280), (0, 1010)]
    c.poly(jalan, (176, 126, 74))
    c.speck(rng, ("poly", jalan), [(150, 104, 58), (200, 150, 96)], 240, (9, 3.5))
    pohon(c, 90, 880, 480, hijau=((52, 122, 60), (38, 98, 48), (70, 140, 70)))
    pohon(c, 650, 940, 540, hijau=((52, 122, 60), (38, 98, 48), (70, 140, 70)))
    for x, y, h in [(40, 1200, 120), (600, 1240, 140), (340, 1160, 80)]:
        rumput(c, x, y, h)
    return dict(bg=c.done(), awan=[(awan(220, 80, (255, 255, 255), 160), -100, 70, 9)])


def latar_medan(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 620, (176, 70, 56), (252, 196, 124))
    c.glow(360, 560, 200, (255, 210, 130), 130, 60)
    c.poly([(0, 560), (180, 510), (380, 560), (560, 500), (720, 550), (720, 700), (0, 700)], (110, 70, 62))
    for x in range(10, 720, 38):
        y = 590 + rng.uniform(-8, 8)
        c.rect(x - 6, y - 26, x + 6, y, (50, 30, 30), r=3)
        c.ell(x, y - 32, 6, 6, (50, 30, 30))
        c.line([(x + 10, y + 4), (x + 12, y - 52)], (50, 30, 30), 2)
    c.grad(660, 1280, (128, 86, 54), (92, 62, 40))
    c.speck(rng, (0, 680, 720, 1280), [(104, 70, 44), (150, 106, 70), (80, 54, 34)], 700, (9, 3))
    for x, y in [(90, 900), (640, 860)]:
        c.line([(x, y), (x, y - 480)], (80, 56, 36), 8)
        c.poly([(x, y - 480), (x + 170, y - 455), (x + 150, y - 420), (x + 170, y - 385), (x, y - 405)], (210, 36, 44),
               outline=(110, 18, 24), w=3)
    smoke = awan(260, 150, (70, 60, 60), 190)
    return dict(bg=c.done(), awan=[(smoke, -50, 60, 18), (smoke, 380, 160, 12), (awan(220, 100, (90, 76, 74), 170), 200, 280, 8)])


def latar_malam(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 700, (8, 12, 44), (64, 62, 124))
    c.glow(540, 260, 150, (255, 246, 200), 90, 50)
    c.ell(540, 260, 62, 62, (255, 248, 214))
    c.ell(560, 246, 52, 52, (250, 242, 200))
    c.poly([(0, 600), (160, 540), (340, 590), (520, 530), (720, 590), (720, 760), (0, 760)], (24, 32, 70))
    for x, w in [(120, 120), (560, 150)]:
        c.rect(x - w / 2, 640, x + w / 2, 720, (30, 24, 40))
        c.poly([(x - w / 2 - 14, 644), (x + w / 2 + 14, 644), (x, 570)], (22, 18, 30))
        c.rect(x - 14, 668, x + 14, 704, (255, 210, 110))
    c.grad(720, 1280, (30, 56, 70), (18, 36, 50))
    c.speck(rng, (0, 740, 720, 1280), [(22, 44, 56), (40, 70, 84)], 500, (8, 3))
    for x, h in [(40, 360), (680, 420)]:
        pohon(c, x, 900, h, hijau=((20, 50, 52), (14, 38, 42), (28, 62, 62)), batang=(30, 22, 30))
    estrela = [(rng.uniform(0, W), rng.uniform(0, 560), rng.uniform(1, 2.6), rng.uniform(0, 6.28)) for _ in range(46)]
    return dict(bg=c.done(), awan=[], bintang=estrela)


def latar_gunung(rng):
    c = Cv(W, H, 2, "RGB")
    c.grad(0, 640, (86, 48, 70), (250, 150, 90))
    c.glow(360, 360, 200, (255, 140, 60), 130, 70)
    c.poly([(0, 640), (360, 330), (720, 640)], (90, 70, 62), outline=(50, 36, 32), w=3)
    c.poly([(360, 330), (720, 640), (500, 640)], (70, 54, 48))
    c.poly([(310, 360), (410, 360), (360, 330)], (255, 120, 40))
    for pts in [[(350, 340), (338, 450), (300, 560)], [(370, 340), (384, 440), (430, 560)], [(360, 340), (360, 470), (350, 600)]]:
        c.line(pts, (255, 130, 40), 8)
    c.poly([(0, 640), (200, 600), (400, 650), (720, 610), (720, 720), (0, 720)], (60, 56, 54))
    c.grad(680, 1280, (74, 66, 58), (46, 40, 36))
    c.speck(rng, (0, 700, 720, 1280), [(60, 52, 46), (96, 84, 72), (34, 30, 28)], 700, (9, 3))
    sm = awan(300, 160, (80, 68, 66), 215)
    return dict(bg=c.done(), awan=[(sm, 250, 40, 6), (sm, 120, 170, 9), (awan(240, 120, (110, 96, 92), 190), 380, 240, 5)])


BUAT_LATAR = {"sawah": latar_sawah, "candi": latar_candi, "kota": latar_kota, "laut": latar_laut,
              "gurun": latar_gurun, "hutan": latar_hutan, "medan": latar_medan, "malam": latar_malam,
              "gunung": latar_gunung}


def kamera(scene, t, total):
    """Latar bergerak: awan, ombak, bintang, ditambah zoom perlahan."""
    bg = scene["bg"].copy()
    for spr, x0, y, v in scene.get("awan", []):
        x = (x0 + v * t) % (W + spr.width) - spr.width
        bg.paste(spr, (int(x), int(y)), spr)
    for spr, y, v in scene.get("ombak", []):
        x = -((t * v) % 80)
        for k in range(0, 2):
            bg.paste(spr, (int(x) + k * W, y), spr)
    if scene.get("bintang"):
        d = ImageDraw.Draw(bg)
        for x, y, r, ph in scene["bintang"]:
            b = int(150 + 105 * (0.5 + 0.5 * math.sin(t * 3 + ph)))
            d.ellipse([x - r, y - r, x + r, y + r], fill=(b, b, min(255, b + 20)))
    z = 1 - 0.09 * (t / max(total, 0.1))
    cw, ch = W * z, H * z
    kiri = (W - cw) / 2 + 10 * math.sin(t * .5)
    atas = (H - ch) * .5
    kiri = max(0.0, min(kiri, W - cw))   # jaga agar bingkai tidak keluar dari gambar
    atas = max(0.0, min(atas, H - ch))
    return bg.resize((W, H), Image.BILINEAR, box=(kiri, atas, min(W, kiri + cw), min(H, atas + ch)))


# ---------------------------------------------------------------- karakter kartun
TOKOH = {
    "petani": dict(skin=(228, 130, 56), shirt=(140, 200, 70), pants=(96, 42, 16), hat="caping"),
    "raja": dict(skin=(226, 152, 96), shirt=(186, 36, 46), pants=(66, 34, 22), hat="mahkota"),
    "prajurit": dict(skin=(212, 130, 74), shirt=(120, 84, 56), pants=(72, 56, 46), hat="helm"),
    "pelaut": dict(skin=(236, 162, 102), shirt=(240, 240, 246), pants=(30, 60, 122), hat="topi"),
    "cendekiawan": dict(skin=(236, 172, 116), shirt=(236, 226, 200), pants=(236, 226, 200), hat="sorban"),
    "rakyat": dict(skin=(202, 116, 60), shirt=(238, 132, 42), pants=(52, 72, 112), hat="ikat"),
}
R = 85                      # jari-jari kepala (piksel)
LW, LH, CX, FEET = 480, 760, 240, 722   # kanvas karakter, titik tengah & telapak kaki
YT = FEET - 16 - 150 - 190  # puncak badan
SSK = 3


class Karakter:
    def __init__(self, nama):
        self.sp = TOKOH.get(nama, TOKOH["rakyat"])
        self.badan = self._badan()
        self.lengan = self._lengan()
        self.cache = {}

    def _badan(self):
        sp = self.sp
        c = Cv(LW, LH, SSK)
        sh, ol = sp["shirt"], gelap(sp["shirt"], .5)
        if sp["hat"] == "mahkota":
            c.poly([(CX - 86, YT + 6), (CX + 86, YT + 6), (CX + 104, FEET - 70), (CX - 104, FEET - 70)], (130, 18, 28),
                   outline=(230, 190, 60), w=4)
        if sp["hat"] == "sorban":
            c.poly([(CX - 76, YT + 150), (CX + 76, YT + 150), (CX + 98, FEET - 18), (CX - 98, FEET - 18)], sh, outline=ol, w=3.5)
        else:
            for x0, x1 in [(CX - 45, CX - 11), (CX + 11, CX + 45)]:
                c.rect(x0, YT + 170, x1, FEET - 16, sp["pants"], outline=gelap(sp["pants"], .5), w=3.5)
        c.ell(CX, YT + 4, 28, 15, sp["skin"], outline=gelap(sp["skin"], .5), w=3)
        c.rect(CX - 75, YT, CX + 75, YT + 190, sh, r=42, outline=ol, w=3.5)
        c.rect(CX - 62, YT + 18, CX - 36, YT + 150, terang(sh, .22), r=13)
        if sp["hat"] == "pelaut":
            for k in range(3):
                c.rect(CX - 68, YT + 52 + k * 36, CX + 68, YT + 66 + k * 36, (30, 60, 122))
        if sp["hat"] == "helm":
            c.poly([(CX - 66, YT + 24), (CX - 40, YT + 10), (CX + 70, YT + 150), (CX + 40, YT + 176)], (60, 42, 30))
        if sp["hat"] == "mahkota":
            c.rect(CX - 76, YT + 150, CX + 76, YT + 170, (234, 190, 60), outline=(150, 110, 20), w=3)
            c.ell(CX, YT + 160, 12, 12, (210, 40, 60), outline=(120, 20, 30), w=2)
        if sp["hat"] == "sorban":
            c.rect(CX - 6, YT + 20, CX + 6, YT + 190, gelap(sh, .8))
        if sp["hat"] != "sorban":
            c.ell(CX - 30, FEET - 14, 34, 16, (50, 28, 18), outline=(20, 10, 6), w=2.5)
            c.ell(CX + 30, FEET - 14, 34, 16, (50, 28, 18), outline=(20, 10, 6), w=2.5)
        else:
            c.ell(CX - 36, FEET - 12, 34, 14, (110, 70, 40), outline=(50, 30, 16), w=2.5)
            c.ell(CX + 36, FEET - 12, 34, 14, (110, 70, 40), outline=(50, 30, 16), w=2.5)
        return c.done()

    def _lengan(self):
        sp = self.sp
        c = Cv(320, 320, SSK)
        sh = sp["shirt"]
        c.rect(142, 148, 178, 272, sh, r=18, outline=gelap(sh, .5), w=3.5)
        c.rect(146, 172, 160, 250, terang(sh, .22), r=7)
        if sp["hat"] == "mahkota":
            c.rect(142, 246, 178, 262, (234, 190, 60), outline=(150, 110, 20), w=2.5)
        sk, so = sp["skin"], gelap(sp["skin"], .5)
        c.ell(160, 286, 26, 24, sk, outline=so, w=3.5)
        for dx in (-17, 0, 17):
            c.ell(160 + dx, 304, 9, 11, sk, outline=so, w=2.5)
        c.ell(160, 292, 20, 14, sk)
        return c.done()

    def kepala(self, emosi, mulut, kedip):
        key = (emosi, mulut, kedip)
        if key in self.cache:
            return self.cache[key]
        sp = self.sp
        sk, so = sp["skin"], gelap(sp["skin"], .45)
        c = Cv(300, 290, SSK)
        cx, cy = 150, 195
        c.ell(cx - R * .98, cy + 4, 18, 24, sk, outline=so, w=3.5)
        c.ell(cx + R * .98, cy + 4, 18, 24, sk, outline=so, w=3.5)
        c.ell(cx, cy, R, R * .97, sk, outline=so, w=4.5)
        blush = (min(255, sk[0] + 8), int(sk[1] * .78), int(sk[2] * .8))
        c.ell(cx - R * .6, cy + R * .34, 15, 10, blush)
        c.ell(cx + R * .6, cy + R * .34, 15, 10, blush)
        ex = R * .38
        ey = cy - R * .04
        brow = {"netral": (-.37, -.39), "senang": (-.43, -.41), "kaget": (-.55, -.55), "sedih": (-.47, -.30),
                "marah": (-.29, -.46)}[emosi]   # (ujung dalam, ujung luar)
        for sg in (-1, 1):
            x = cx + sg * ex
            if kedip or emosi == "senang":
                if emosi == "senang" and not kedip:
                    c.arc((x - 17, ey - 14, x + 17, ey + 18), 200, 340, (30, 16, 10), 5)
                else:
                    c.line([(x - 17, ey + 3), (x + 17, ey + 3)], (30, 16, 10), 5)
            else:
                rx, ry = (21, 27) if emosi != "kaget" else (25, 32)
                c.ell(x, ey, rx, ry, (255, 255, 255), outline=(40, 20, 12), w=3.5)
                pr = 9 if emosi != "kaget" else 6
                c.ell(x + 2, ey + 3, pr, pr + 1, (24, 14, 10))
                c.ell(x + 5, ey - 1, 3, 3, (255, 255, 255))
            ind, luar = brow[0] * R, brow[1] * R
            xi, xo = x - sg * 22, x + sg * 22   # dalam = ke arah tengah wajah
            c.line([(xo, cy + luar), ((xi + xo) / 2, cy + min(ind, luar) - 5), (xi, cy + ind)], (60, 26, 12), 6.5)
        c.arc((cx - 2, cy + R * .14, cx + 22, cy + R * .34), 20, 150, so, 3)
        my = cy + R * .56
        if mulut == "tutup":
            if emosi == "senang":
                c.arc((cx - 30, my - 22, cx + 30, my + 18), 20, 160, (90, 30, 18), 5)
            elif emosi == "sedih":
                c.arc((cx - 24, my - 2, cx + 24, my + 30), 200, 340, (90, 30, 18), 5)
            elif emosi == "marah":
                c.line([(cx - 20, my + 6), (cx + 20, my + 2)], (90, 30, 18), 5)
            elif emosi == "kaget":
                c.ell(cx, my + 4, 11, 14, (96, 24, 20), outline=(60, 16, 12), w=2.5)
            else:
                c.arc((cx - 24, my - 16, cx + 24, my + 14), 20, 160, (90, 30, 18), 4.5)
        else:
            rx, ry = (15, 11) if mulut == "kecil" else (23, 21)
            if emosi == "kaget":
                ry += 6
            c.ell(cx, my + 6, rx, ry, (110, 24, 22), outline=(60, 16, 12), w=3)
            if mulut == "lebar":
                c.ell(cx, my + ry * .55 + 6, rx * .6, ry * .4, (232, 100, 110))
        self._topi(c, cx, cy)
        img = c.done()
        self.cache[key] = img
        return img

    def _topi(self, c, cx, cy):
        h = self.sp["hat"]
        bawah = cy - R * .58
        if h == "caping":
            c.poly([(cx - R * 1.55, bawah + 8), (cx + R * 1.55, bawah + 8), (cx, cy - R * 2.0)], (242, 218, 124),
                   outline=(60, 34, 10), w=4)
            for k in range(-6, 7):
                c.line([(cx, cy - R * 2.0 + 4), (cx + k * R * .24, bawah + 4)], (206, 168, 78), 1.8)
            c.line([(cx - R * 1.55, bawah + 8), (cx + R * 1.55, bawah + 8)], (60, 34, 10), 5)
        elif h == "mahkota":
            c.chord((cx - R, cy - R * 1.12, cx + R, cy + R * .3), 180, 360, (30, 20, 24))
            c.poly([(cx - R * .72, cy - R * .98), (cx + R * .72, cy - R * .98), (cx + R * .72, cy - R * 1.28),
                    (cx + R * .36, cy - R * 1.08), (cx, cy - R * 1.5), (cx - R * .36, cy - R * 1.08), (cx - R * .72, cy - R * 1.28)],
                   (240, 196, 64), outline=(150, 110, 20), w=3.5)
            c.ell(cx, cy - R * 1.05, 7, 7, (210, 40, 60), outline=(120, 20, 30), w=2)
        elif h == "helm":
            c.chord((cx - R * 1.06, bawah - R * 1.0, cx + R * 1.06, bawah + R * 1.0), 180, 360, (156, 162, 172),
                    outline=(50, 52, 60), w=4)
            c.rect(cx - R * 1.14, bawah - 4, cx + R * 1.14, bawah + 12, (110, 114, 124), r=6, outline=(50, 52, 60), w=3.5)
            c.poly([(cx - 9, bawah - R * .98), (cx + 9, bawah - R * .98), (cx, bawah - R * 1.3)], (210, 40, 44), outline=(110, 18, 24), w=2.5)
        elif h == "topi":
            c.chord((cx - R * 1.04, bawah - R * .95, cx + R * 1.04, bawah + R * .95), 180, 360, (240, 240, 246),
                    outline=(80, 84, 100), w=4)
            c.rect(cx - R * 1.1, bawah - 4, cx + R * 1.1, bawah + 16, (30, 60, 122), r=6, outline=(14, 30, 70), w=3.5)
            c.ell(cx, bawah + 6, 8, 8, (240, 196, 64), outline=(150, 110, 20), w=2)
        elif h == "sorban":
            c.ell(cx, cy - R * .88, R * 1.05, R * .62, (248, 246, 240), outline=(120, 110, 90), w=4)
            for k in range(3):
                c.arc((cx - R * 1.0, cy - R * 1.3 + k * 12, cx + R * 1.0, cy - R * .5 + k * 12), 190, 350, (190, 180, 160), 3)
            c.ell(cx, cy - R * 1.42, 14, 14, (248, 246, 240), outline=(120, 110, 90), w=3)
            c.ell(cx + R * .5, cy - R * .62, 9, 9, (210, 40, 60), outline=(120, 20, 30), w=2)
        else:  # ikat kepala
            c.chord((cx - R * 1.0, bawah - R * 1.0, cx + R * 1.0, bawah + R * 1.0), 180, 360, (30, 20, 24))
            c.rect(cx - R * 1.02, bawah - 8, cx + R * 1.02, bawah + 16, (200, 36, 44), r=7, outline=(110, 18, 24), w=3.5)
            c.ell(cx + R * .98, bawah + 18, 14, 11, (200, 36, 44), outline=(110, 18, 24), w=3)

    def gambar(self, t, env, emosi, mulut, kedip):
        L = Image.new("RGBA", (LW, LH), (0, 0, 0, 0))
        L.paste(self.badan, (0, 0), self.badan)
        ayun = math.sin(t * 2 * math.pi * 1.25)
        # lengan: kiri tenang, kanan bergerak saat bicara
        sudut_kiri = -(7 + 4 * ayun + env * 12 * (0.5 + 0.5 * math.sin(t * 7.3 + 1)))
        sudut_kanan = 8 + 3 * ayun + env * 48 * (0.5 + 0.5 * math.sin(t * 2 * math.pi * 1.6))
        for sx, ang in [(CX - 75 + 6, sudut_kiri), (CX + 75 - 6, sudut_kanan)]:
            lg = self.lengan.rotate(ang, resample=Image.BICUBIC)
            L.paste(lg, (int(sx - 160), int(YT + 30 - 160)), lg)
        kep = self.kepala(emosi, mulut, kedip)
        sdt = 2.5 * math.sin(t * 2 * math.pi * .8) + env * 4 * math.sin(t * 2 * math.pi * 2.3)
        kep = kep.rotate(sdt, resample=Image.BICUBIC, center=(150, 195))
        naik = 3 * math.sin(t * 2 * math.pi * 1.25 + 1) + env * 4
        cy = YT + 24 - R
        L.paste(kep, (int(CX - 150), int(cy - 195 + naik)), kep)
        return L


# ---------------------------------------------------------------- subtitle kata demi kata
FEET_Y = 850
SUB_CY = 975


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
        self.cache = {}

    def sprite(self, n, aktif):
        key = (n, aktif)
        if key in self.cache:
            return self.cache[key]
        idx = self.chunks[n]
        teks = [self.kata[i][2] for i in idx]
        ukuran = 80
        while True:
            f = ImageFont.truetype(FONT, ukuran)
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
            warna = (255, 214, 64) if i == aktif else (255, 255, 255)
            d.text((x, 85), t, font=f, fill=warna, anchor="lm", stroke_width=9, stroke_fill=(0, 0, 0))
            x += lb + spasi
        self.cache[key] = im
        return im

    def untuk(self, t):
        if not self.kata:
            return None
        i = bisect.bisect_right(self.mulai, t) - 1
        if i < 0:
            return self.sprite(0, -1), 1.0
        im = self.sprite(self.chunk_of[i], i)
        umur = t - self.mulai[i]
        if umur < 0.12:
            k = 1.22 - 0.22 * (umur / 0.12)
            im = im.resize((int(im.width * k), int(im.height * k)), Image.BILINEAR)
        return im, 1.0


# ---------------------------------------------------------------- render segmen
def bikin_bayangan():
    c = Cv(260, 60, 2)
    c.ell(130, 30, 110, 18, (0, 0, 0, 120))
    return c.im.filter(ImageFilter.GaussianBlur(8)).resize((260, 60), Image.LANCZOS)


def bikin_vinyet():
    kecil = Image.new("L", (72, 128), 255)
    ImageDraw.Draw(kecil).ellipse([-10, -14, 82, 142], fill=0)
    kecil = kecil.filter(ImageFilter.GaussianBlur(14)).point(lambda v: int(v * .45))
    return kecil.resize((W, H), Image.BILINEAR)


BAYANGAN, VINYET = bikin_bayangan(), bikin_vinyet()
HITAM = Image.new("RGB", (W, H), (0, 0, 0))


def render_segmen(b, audio, kata_server, out_path, idx):
    dur = durasi(audio)
    total = dur + 0.35
    n_frame = int(total * FPS) + 1
    energi = baca_energi(audio)
    narasi_kata = b["narasi"].split()
    kata = kata_server if len(kata_server) >= 0.6 * len(narasi_kata) else perkirakan_kata(b["narasi"], energi)
    sub = Subtitle(kata)
    scene = BUAT_LATAR[b["latar"]](random.Random(idx * 7919 + 13))
    tokoh = Karakter(b["tokoh"])
    emosi = b["emosi"]
    rng = random.Random(idx + 5)
    kedip_at, t_k = [], rng.uniform(0.8, 1.8)
    while t_k < total:
        kedip_at.append(t_k)
        t_k += rng.uniform(1.8, 3.2)

    proc = subprocess.Popen([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
        "-r", str(FPS), "-i", "-", "-i", str(audio), "-map", "0:v", "-map", "1:a", "-af", "apad",
        "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k", str(out_path),
    ], stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        for f in range(n_frame):
            t = f / FPS
            frame = kamera(scene, t, total)
            frame.paste(BAYANGAN, (W // 2 - 130, FEET_Y - 30), BAYANGAN)
            e = energi[f] if f < len(energi) else 0.0
            e2 = 0.5 * e + 0.5 * (energi[f - 1] if 0 < f <= len(energi) else 0.0)
            mulut = "tutup" if e2 < 0.12 else ("kecil" if e2 < 0.42 else "lebar")
            kedip = any(0 <= t - k < 0.14 for k in kedip_at)
            env = min(1.0, sum(energi[max(0, f - 4):f + 1]) / max(1, len(energi[max(0, f - 4):f + 1])))
            layer = tokoh.gambar(t, env if f < len(energi) else 0.0, emosi, mulut, kedip)
            pop = 1.0
            if t < 0.4:
                u = t / 0.4
                pop = 0.55 + 0.45 * u + 0.12 * math.sin(u * math.pi)
                layer = layer.resize((int(LW * pop), int(LH * pop)), Image.BILINEAR)
            frame.paste(layer, (int(W / 2 - CX * pop), int(FEET_Y - FEET * pop)), layer)
            frame.paste(HITAM, (0, 0), VINYET)
            s = sub.untuk(t)
            if s:
                im, _ = s
                frame.paste(im, (int((W - im.width) / 2), int(SUB_CY - im.height / 2)), im)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    sisa = proc.stderr.read().decode(errors="ignore")
    if proc.wait() != 0:
        raise RuntimeError("FFmpeg gagal: " + sisa[-600:])


def gabung(segmen, out_path):
    daftar = WORK / "list.txt"
    daftar.write_text("".join(f"file '{Path(s).name}'\n" for s in segmen), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(daftar),
         "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_path)])


# ---------------------------------------------------------------- upload
def upload_youtube(video, naskah):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)
    deskripsi = naskah.get("deskripsi", "") + "\n\n#Shorts #sejarah #animasi\nAnimasi dan narasi dibuat dengan bantuan AI."
    body = {
        "snippet": {
            "title": naskah["judul"][:95],
            "description": deskripsi[:4900],
            "tags": naskah.get("tag", [])[:15],
            "categoryId": "27",  # Education
        },
        "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False},
    }
    req = yt.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True),
    )
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print("Terunggah: https://youtube.com/shorts/" + resp["id"])


def main():
    rows, topik_row = pilih_topik()
    topik = topik_row["topik"]
    print("Topik:", topik)
    naskah = tulis_naskah(topik)
    print("Judul:", naskah["judul"])

    segmen = []
    for i, b in enumerate(naskah["bagian"]):
        audio, seg = WORK / f"a{i}.mp3", WORK / f"seg{i}.mp4"
        kata = suara(b["narasi"], audio)
        render_segmen(b, audio, kata, seg, i)
        segmen.append(seg)
        print(f"Bagian {i + 1}/{len(naskah['bagian'])} selesai ({b['latar']}, {b['tokoh']}, {b['emosi']})")

    final = WORK / "final.mp4"
    gabung(segmen, final)
    print("Video jadi:", final)

    if UPLOAD:
        upload_youtube(final, naskah)
        topik_row["status"] = "selesai"
        simpan_topik(rows)


if __name__ == "__main__":
    main()
