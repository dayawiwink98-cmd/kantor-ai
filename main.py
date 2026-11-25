"""Kantor AI Shorts - versi ringan (tanpa GPU, tanpa kunci Pexels).
Alur: topik -> naskah (Gemini) -> suara (edge-tts) -> gambar (Wikimedia Commons)
      -> video (FFmpeg) -> upload YouTube.
"""
import asyncio
import csv
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import edge_tts
import requests

GEMINI_KEY = os.environ["GEMINI_API_KEY"]
# Daftar model dicoba berurutan; bila satu ditolak (404/429), lanjut ke berikutnya.
# Bisa diganti lewat variabel GEMINI_MODEL (pisahkan dengan koma).
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODEL", "gemini-3.5-flash,gemini-3.1-flash-lite,gemini-2.5-flash-lite").split(",") if m.strip()]
VOICE = os.environ.get("TTS_VOICE", "id-ID-ArdiNeural")  # alternatif: id-ID-GadisNeural
PRIVACY = os.environ.get("YT_PRIVACY", "private")  # private / unlisted / public
UPLOAD = os.environ.get("UPLOAD", "1") == "1"
W, H = 720, 1280
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
WORK = Path("work")
WORK.mkdir(exist_ok=True)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "KantorAIShorts/1.0 (proyek pribadi pembuat Shorts sejarah)"}
LISENSI_AMAN = ("public domain", "cc0", "pd")  # hanya domain publik / CC0, tanpa kewajiban atribusi


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


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
    prompt = f"""Kamu penulis naskah YouTube Shorts sejarah berbahasa Indonesia.
Topik: {topik}

Aturan:
- Total narasi 90-110 kata (sekitar 40 detik dibacakan).
- Bagian pertama adalah hook 1 kalimat yang mengejutkan atau memancing rasa penasaran. Jangan mulai dengan "Tahukah kamu".
- Hanya gunakan fakta sejarah yang akurat dan umum diketahui. Jika tidak yakin soal angka, tanggal, atau nama, jangan disebutkan.
- Akhiri dengan 1 kalimat penutup yang mengajak penonton berkomentar.
- Bagi menjadi 6-8 bagian, tiap bagian 1-2 kalimat pendek.
- Tiap bagian punya "kata_kunci": nama SPESIFIK (tempat, tokoh, peristiwa, atau artefak) untuk dicari di arsip
  foto Wikimedia Commons, dalam bahasa Inggris atau nama aslinya. Contoh: Majapahit, Gajah Mada statue,
  Borobudur, Hagia Sophia, Battle of Surabaya 1945, Roman Colosseum. Hindari kata generik seperti "war" atau "king".

Balas HANYA JSON dengan bentuk:
{{"judul": "maks 70 karakter", "deskripsi": "2 kalimat", "tag": ["..."],
  "bagian": [{{"narasi": "...", "kata_kunci": "..."}}]}}"""
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
                print("Naskah dibuat dengan model:", model)
                return data
            except Exception as e:  # noqa: BLE001
                print(f"Naskah gagal ({model}, percobaan {percobaan + 1}): {e}")
                time.sleep(5)
    sys.exit("Gagal membuat naskah.")


async def _tts(teks, path):
    await edge_tts.Communicate(teks, VOICE, rate="+5%").save(path)


def suara(teks, path):
    asyncio.run(_tts(teks, str(path)))


def durasi(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def gambar_cadangan(stem):
    """Latar polos bila tidak ada gambar yang cocok, supaya alur tidak berhenti."""
    out = Path(f"{stem}.png")
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x1b1b2f:s=1080x1080",
         "-frames:v", "1", str(out)])
    return out


def cari_gambar(queries, stem, dipakai):
    """Cari gambar domain publik di Wikimedia Commons. Mengembalikan (teks kredit, path) atau (None, None)."""
    for q in queries:
        time.sleep(1)  # sopan terhadap server Wikimedia
        r = requests.get(COMMONS_API, headers=UA, timeout=60, params={
            "action": "query", "format": "json", "generator": "search",
            "gsrnamespace": 6, "gsrsearch": q, "gsrlimit": 20,
            "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1080,
        })
        r.raise_for_status()
        halaman = sorted(r.json().get("query", {}).get("pages", {}).values(),
                         key=lambda p: p.get("index", 0))
        for p in halaman:
            judul = p.get("title", "")
            info = (p.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata", {})
            lisensi = meta.get("LicenseShortName", {}).get("value", "")
            if judul in dipakai:
                continue
            if info.get("mime") not in ("image/jpeg", "image/png"):
                continue
            if info.get("width", 0) < 800 or not lisensi.lower().startswith(LISENSI_AMAN):
                continue
            link = info.get("thumburl") or info.get("url")
            if not link:
                continue
            out_path = Path(f"{stem}{'.png' if info['mime'] == 'image/png' else '.jpg'}")
            with requests.get(link, headers=UA, stream=True, timeout=120) as dl:
                if dl.status_code != 200:
                    continue
                with open(out_path, "wb") as fh:
                    for chunk in dl.iter_content(1 << 20):
                        fh.write(chunk)
            dipakai.add(judul)
            artis = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip()
            kredit = f"{judul.replace('File:', '')[:80]} ({lisensi}{', ' + artis[:40] if artis else ''})"
            return kredit, out_path
    return None, None


def buat_segmen(gambar, audio, teks, dur, out_path, idx):
    txt = WORK / f"t{idx}.txt"
    txt.write_text(textwrap.fill(teks, 20), encoding="utf-8")
    # latar buram + gambar utuh di tengah + zoom perlahan + teks
    vf = (
        f"[0:v]split[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:5[bg];"
        f"[b]scale={W}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,scale={W * 2}:{H * 2},"
        f"zoompan=z='min(zoom+0.0006,1.2)':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps=30,"
        f"drawtext=fontfile={FONT}:textfile={txt}:fontsize=44:fontcolor=white:"
        f"borderw=3:bordercolor=black:x=(w-text_w)/2:y=h*0.78:line_spacing=10,format=yuv420p[v]"
    )
    run([
        "ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", str(gambar), "-i", str(audio),
        "-t", f"{dur + 0.25:.2f}", "-filter_complex", vf, "-af", "apad=pad_dur=0.25",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out_path),
    ])


def gabung(segmen, out_path):
    daftar = WORK / "list.txt"
    daftar.write_text("".join(f"file '{Path(s).name}'\n" for s in segmen), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(daftar),
         "-c", "copy", "-movflags", "+faststart", str(out_path)])


def upload_youtube(video, naskah, kredit):
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
    deskripsi = naskah.get("deskripsi", "") + "\n\n#Shorts #sejarah"
    if kredit:
        deskripsi += "\n\nGambar dari Wikimedia Commons:\n" + "\n".join(kredit[:8])
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

    segmen, kredit, dipakai = [], [], set()
    for i, b in enumerate(naskah["bagian"]):
        audio, seg = WORK / f"a{i}.mp3", WORK / f"seg{i}.mp4"
        suara(b["narasi"], audio)
        info, gambar = cari_gambar([b["kata_kunci"], topik], WORK / f"g{i}", dipakai)
        if info:
            kredit.append(info)
        else:
            print(f"Gambar tidak ditemukan untuk '{b['kata_kunci']}', pakai latar polos")
            gambar = gambar_cadangan(WORK / f"g{i}")
        buat_segmen(gambar, audio, b["narasi"], durasi(audio), seg, i)
        segmen.append(seg)
        print(f"Bagian {i + 1}/{len(naskah['bagian'])} selesai")

    final = WORK / "final.mp4"
    gabung(segmen, final)
    print("Video jadi:", final)

    if UPLOAD:
        upload_youtube(final, naskah, kredit)
        topik_row["status"] = "selesai"
        simpan_topik(rows)


if __name__ == "__main__":
    main()
