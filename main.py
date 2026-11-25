"""Cerita Horor Bersambung (animasi kartun, tanpa GPU) - gaya 'Titik Sunyi'.

Alur: topics.csv (seri & part) -> naskah per adegan (Gemini, nyambung dengan part sebelumnya lewat state.json)
      -> suara (edge-tts, kata demi kata) -> animasi (anim.py) -> video (FFmpeg) -> upload YouTube.

Uji tanpa API key / tanpa internet:  python main.py --uji
"""
import asyncio
import csv
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import anim
from anim import EFEK, EMOSI, LATAR, POSE, SHOT, render_kartu_akhir, render_segmen

UJI = "--uji" in sys.argv
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODEL", "gemini-3.5-flash,gemini-3.1-flash-lite,gemini-2.5-flash-lite").split(",") if m.strip()]
VOICE = os.environ.get("TTS_VOICE", "id-ID-ArdiNeural")           # suara narator (pria)
VOICE_WANITA = os.environ.get("TTS_VOICE_WANITA", "id-ID-GadisNeural")  # dipakai bila tokoh wanita yang bicara
TTS_RATE = os.environ.get("TTS_RATE", "-6%")                     # sedikit lambat = lebih mencekam
TTS_PITCH = os.environ.get("TTS_PITCH", "-6Hz")
PRIVACY = os.environ.get("YT_PRIVACY", "private")                 # private / unlisted / public
UPLOAD = os.environ.get("UPLOAD", "1") == "1" and not UJI
WORK = Path("work")
WORK.mkdir(exist_ok=True)
TOPIK_CSV, STATE_JSON = Path("topics.csv"), Path("state.json")
KOLOM = ["seri", "part", "total", "premis", "arah", "status"]
GENDER, KULIT, BAJU = ("pria", "wanita"), ("sawo", "terang", "gelap"), tuple(anim.BAJU)


# ================================================================ topik & state
def baca_topik():
    with open(TOPIK_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    premis = {}
    for r in rows:  # premis cukup ditulis di part 1; part berikutnya mewarisinya
        r["seri"] = (r.get("seri") or "").strip()
        if (r.get("premis") or "").strip():
            premis[r["seri"]] = r["premis"].strip()
        r["premis"] = premis.get(r["seri"], "")
    return rows


def pilih_topik(rows):
    for r in rows:
        if r["seri"] and (r.get("status") or "").strip() != "selesai":
            return r
    sys.exit("Semua part sudah selesai. Tambahkan seri baru di topics.csv")


def simpan_topik(rows):
    with open(TOPIK_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=KOLOM)
        w.writeheader()
        asli = {}
        for r in rows:  # tulis premis hanya di part pertama tiap seri
            p = r["premis"] if r["seri"] not in asli else ""
            asli[r["seri"]] = True
            w.writerow({k: (p if k == "premis" else (r.get(k) or "")) for k in KOLOM})


def muat_state():
    if STATE_JSON.exists():
        return json.loads(STATE_JSON.read_text(encoding="utf-8"))
    return {}


def simpan_state(st):
    STATE_JSON.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ================================================================ naskah
FORMAT_JSON = """{
  "judul": "judul pendek mencekam tanpa kata 'Part' (maks 60 karakter)",
  "deskripsi": "2 kalimat teaser tanpa spoiler",
  "tag": ["horor", "ceritahoror", "..."],
  "narator": "nama tokoh 'aku' (isi di part 1, sama di part berikutnya)",
  "tokoh": [{"nama": "Bima", "gender": "pria", "rambut": "pendek", "kulit": "sawo", "baju": "abu", "kacamata": false}],
  "bagian": [
    {"narasi": "satu kalimat pendek", "latar": "kamar_malam", "shot": "lebar",
     "tokoh": [{"nama": "Bima", "pose": "berdiri", "emosi": "takut"}], "bicara": "Bima", "efek": ["getar"]},
    {"narasi": "...", "latar": "lorong", "shot": "cctv", "tokoh": [], "cctv": {"jam": "03:00:41", "kecepatan": 1},
     "efek": ["glitch"]}
  ],
  "ringkasan": "3-4 kalimat: apa yang terjadi di part ini (untuk kesinambungan part berikutnya)",
  "cliffhanger": "1 kalimat: kejadian terakhir yang menggantung",
  "kartu_akhir": {"waktu": "03:00:00", "teks": "JANGAN BUKA PINTU JAM 03.00"}
}"""

CONTOH = [  # contoh irama kalimat (bukan untuk disalin isinya)
    "Jangan pernah buka pintu kalau ada yang mengetuk jam tiga pagi.", "Aku Bima. Ini istriku, Laras.",
    "Kami pindah ke rumah kontrakan yang murah.", "Terlalu murah, sebenarnya.",
    "Malam pertama, pintu depan diketuk. Satu kali.", "Malam kedua, dua kali.", "Malam ketujuh, tujuh kali.",
    "Jadi kupasang kamera kecil di ruang tamu.", "Tidak ada siapa pun di depan pintu.",
    "Tapi pintunya bergetar dari dalam.", "Rambut panjang. Gaun putih. Itu Laras.",
    "Padahal Laras sedang tidur di sampingku."]


def buat_prompt(row, st):
    part, total = int(row["part"]), int(row["total"])
    s = st.get(row["seri"], {})
    p = f"""Kamu penulis naskah cerita horor bersambung berbahasa Indonesia untuk video pendek animasi kartun
(YouTube Shorts / Reels / TikTok, vertikal 9:16, durasi 60-80 detik). Sudut pandang orang pertama ("aku"), gaya bercerita
pelan, dingin, jujur, seperti pengakuan. Seri: "{row['seri']}". Ini PART {part} dari {total}.

PREMIS SERI: {row['premis']}
ARAH CERITA PART INI: {row['arah']}
"""
    if s.get("ringkasan"):
        p += "\nRINGKASAN PART SEBELUMNYA:\n" + "\n".join(s["ringkasan"]) + "\n"
        p += f"CLIFFHANGER TERAKHIR: {s.get('cliffhanger', '')}\n"
        p += f"NARATOR: {s.get('narator', '')}. TOKOH YANG SUDAH ADA (pakai persis, jangan diubah): " \
             f"{json.dumps(s.get('tokoh', []), ensure_ascii=False)}\n"
    p += f"""
ALUR KHAS (wajib diikuti urutannya):
1. HOOK (adegan 1, maks 6 detik): {'satu larangan atau fakta janggal yang spesifik, mengandung jam atau angka. Contoh bentuk: "Jangan pernah memutar rekaman CCTV jam dua lewat tiga belas."' if part == 1 else 'kalimat yang langsung menyambung cliffhanger part sebelumnya, misalnya mengingatkan apa yang baru dilihat tokoh, tetap mengandung jam atau angka.'} Jangan mulai dengan "Tahukah kamu".
2. PERKENALAN: {"singkat saja, 'Aku [Nama].' lalu tokoh lain." if part == 1 else "tidak perlu memperkenalkan lagi; sambungkan langsung dari cliffhanger part sebelumnya dalam 1 kalimat."}
3. NORMAL TAPI JANGGAL: 2-3 adegan keseharian dengan satu detail kecil yang terasa salah.
4. POLA BERULANG: kejanggalan terjadi lagi, makin jelas (jam yang sama, hitungan yang bertambah).
5. BUKTI: tokoh memeriksa (rekaman, pesan, foto). Kalimat jadi sangat pendek (3-6 kata).
6. {"TWIST + CLIFFHANGER: kalimat terakhir diawali 'Padahal...' atau pertanyaan yang mengubah arti semua tadi. Kalimat terakhir maks 9 kata." if part < total else "JAWABAN + TWIST AKHIR: ungkap penyebabnya, beri satu putaran terakhir yang membuat bulu kuduk berdiri, tutup dengan kalimat pendek yang menggantung."}

ATURAN NARASI:
- Total narasi 140-180 kata, 14-18 adegan. Satu adegan = satu kalimat (maks 14 kata). Irama seperti ini: {' / '.join(CONTOH[:6])}
- Tulis jam dan angka dengan huruf agar dibacakan benar ("jam dua lewat tiga belas", bukan "02.13").
- Tanpa gore, tanpa darah, tanpa kekerasan eksplisit, tanpa bunuh diri, tanpa tokoh/merek nyata. Seram lewat suasana dan implikasi.
- Konsisten: nama, jam kunci, aturan misterinya tidak berubah antar part.

ATURAN VISUAL (tiap adegan pilih dari daftar ini saja):
- latar: {', '.join(LATAR)}
- shot: lebar (tokoh kecil, lihat ruangan), sedang (tokoh agak dekat), closeup (wajah penuh; untuk reaksi/ketakutan),
  ranjang (zoom ke tempat tidur; hanya di kamar_malam/kamar_pagi), cctv (tampilan kamera pengawas hijau, wajib ada
  "cctv": {{"jam": "HH:MM:SS", "kecepatan": 1 atau 16}}; jam harus naik masuk akal antar adegan cctv), cctv_zoom (wajah penuh di CCTV; pakai tokoh emosi seram).
- pose tokoh: berdiri, duduk (di meja dapur / sofa ruang_tamu / di kasur), tidur (hanya di kamar; maks 2 orang)
- emosi: {', '.join(EMOSI)} (seram = mata menyala, hanya untuk sosok hantu/kerasukan)
- efek (opsional, hemat): getar (gemetar), kedip (lampu berkedip), glitch (gangguan gambar, cocok di cctv), kejut (kilatan + dentuman, pakai maks 2 kali, untuk twist)
- Variasikan shot: jangan sama lebih dari 3 adegan berturut-turut. Pakai cctv/cctv_zoom untuk adegan bukti. Pakai closeup untuk puncak ketakutan.
- "bicara" = nama tokoh yang mulutnya bergerak (biasanya narator bila ia tampil; tokoh lain bila narasi adalah ucapannya dalam tanda kutip). Kosongkan jika tidak ada.
- Tokoh: gender {'/'.join(GENDER)}, rambut {'/'.join(anim.RAMBUT)}, kulit {'/'.join(KULIT)}, baju {'/'.join(BAJU)}. Maks 4 tokoh per seri; tokoh hantu cukup ditulis dengan emosi seram pada adegannya.
- kartu_akhir.waktu = jam kunci cerita (HH:MM:SS); kartu_akhir.teks = peringatan khas seri, huruf besar, maks 5 kata.

Balas HANYA JSON valid dengan bentuk ini (jumlah adegan sesuai aturan, bukan hanya 2):
{FORMAT_JSON}"""
    return p


def _nama_cocok(nama, cast):
    for k in cast:
        if str(nama).strip().lower() == k.lower():
            return k
    return None


def _rapikan_tokoh(t):
    nama = str(t.get("nama", "")).strip()[:14] or "Tokoh"
    rambut = t.get("rambut") if t.get("rambut") in anim.RAMBUT else "pendek"
    gender = t.get("gender") if t.get("gender") in GENDER else "pria"
    return dict(nama=nama, gender=gender, rambut=rambut, kulit=t.get("kulit") if t.get("kulit") in KULIT else "sawo",
                baju=t.get("baju") if t.get("baju") in BAJU else "abu", kacamata=bool(t.get("kacamata")))


def validasi(data, st_seri, part, cek_kata=True):
    """Rapikan keluaran model supaya tidak pernah membuat render error."""
    cast = {t["nama"]: t for t in st_seri.get("tokoh", [])}
    for t in data.get("tokoh") or []:
        if isinstance(t, dict):
            r = _rapikan_tokoh(t)
            if _nama_cocok(r["nama"], cast) is None:
                cast[r["nama"]] = r
    assert cast, "tokoh kosong"
    narator = _nama_cocok(data.get("narator") or st_seri.get("narator") or "", cast) or next(iter(cast))
    bagian = data.get("bagian") or []
    assert len(bagian) >= 8, "adegan terlalu sedikit"
    out = []
    for b in bagian[:24]:
        narasi = re.sub(r"\s+", " ", str(b.get("narasi", ""))).strip()
        assert narasi, "narasi kosong"
        shot = b.get("shot") if b.get("shot") in SHOT else "sedang"
        latar = b.get("latar") if b.get("latar") in LATAR else "kamar_malam"
        if shot == "ranjang" and latar not in ("kamar_malam", "kamar_pagi"):
            shot = "sedang"
        tokoh, dipakai = [], set()
        for t in b.get("tokoh") or []:
            t = {"nama": t} if isinstance(t, str) else t
            if not isinstance(t, dict):
                continue
            nm = _nama_cocok(t.get("nama", ""), cast)
            if nm and nm not in dipakai and len(tokoh) < 3:
                dipakai.add(nm)
                tokoh.append(dict(nama=nm, pose=t.get("pose") if t.get("pose") in POSE else "berdiri",
                                  emosi=t.get("emosi") if t.get("emosi") in EMOSI else "netral"))
        if shot in ("closeup", "cctv_zoom") and not tokoh:
            tokoh = [dict(nama=narator, pose="berdiri", emosi="takut")]
        if sum(t["pose"] == "tidur" for t in tokoh) > 2:
            for t in tokoh:
                t["pose"] = "berdiri"
        if "bicara" in b:
            bicara = _nama_cocok(b.get("bicara") or "", cast)
        else:
            bicara = narator if narator in dipakai else None
        if bicara not in dipakai:
            bicara = None
        item = dict(narasi=narasi, latar=latar, shot=shot, tokoh=tokoh, bicara=bicara,
                    efek=[e for e in (b.get("efek") or []) if e in EFEK][:3])
        if shot in ("cctv", "cctv_zoom"):
            c = b.get("cctv") if isinstance(b.get("cctv"), dict) else {}
            jam = str(c.get("jam", ""))
            jam = jam if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", jam) else "02:13:00"
            kec = c.get("kecepatan") if c.get("kecepatan") in (1, 2, 4, 8, 16, 32) else 1
            item["cctv"] = dict(jam=jam, kecepatan=kec)
        out.append(item)
    kata = sum(len(b["narasi"].split()) for b in out)
    assert not cek_kata or 100 <= kata <= 280, f"jumlah kata aneh: {kata}"
    ka = data.get("kartu_akhir") if isinstance(data.get("kartu_akhir"), dict) else {}
    waktu = str(ka.get("waktu", ""))
    waktu = waktu if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", waktu) else "02:13:00"
    return dict(judul=str(data.get("judul") or "Cerita Horor")[:70], deskripsi=str(data.get("deskripsi") or ""),
                tag=[str(x) for x in (data.get("tag") or [])][:12], narator=narator, tokoh=list(cast.values()),
                bagian=out, ringkasan=str(data.get("ringkasan") or ""), cliffhanger=str(data.get("cliffhanger") or ""),
                kartu_akhir=dict(waktu=waktu, teks=str(ka.get("teks") or "")[:60]))


def tulis_naskah(row, st):
    import requests
    part = int(row["part"])
    prompt = buat_prompt(row, st)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}}
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for percobaan in range(3):
            try:
                r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=180)
                if r.status_code in (404, 429):
                    print(f"Model {model} ditolak (HTTP {r.status_code}), coba model berikutnya")
                    break
                r.raise_for_status()
                bag = r.json()["candidates"][0]["content"]["parts"]
                teks = "".join(p.get("text", "") for p in bag if not p.get("thought"))
                data = validasi(json.loads(teks), st.get(row["seri"], {}), part)
                print("Naskah dibuat dengan model:", model)
                return data
            except Exception as e:  # noqa: BLE001
                print(f"Naskah gagal ({model}, percobaan {percobaan + 1}): {e}")
                time.sleep(5)
    sys.exit("Gagal membuat naskah.")


def naskah_contoh():
    """Contoh lengkap (dipakai --uji) sekaligus gambaran hasil yang dituju."""
    def T(n, p="berdiri", e="netral"):
        return dict(nama=n, pose=p, emosi=e)
    B, L = "Bima", "Laras"
    s = [
        ("Jangan pernah buka pintu kalau ada yang mengetuk jam tiga pagi.", "luar_malam", "lebar", [T(B, e="takut")], B, ["getar"]),
        ("Aku Bima. Ini istriku, Laras.", "ruang_tamu", "sedang", [T(B, "duduk", "senang"), T(L, "duduk", "senang")], B, []),
        ("Kami pindah ke rumah kontrakan yang murah.", "ruang_tamu", "lebar", [T(B, e="senang"), T(L, e="senang")], B, []),
        ("Terlalu murah, sebenarnya.", "dapur", "closeup", [T(B)], B, []),
        ("Malam pertama, pintu depan diketuk. Satu kali.", "kamar_malam", "ranjang", [T(B, "tidur"), T(L, "tidur")], None, []),
        ("Jam tiga tepat.", "kamar_malam", "lebar", [T(B, "tidur"), T(L, "tidur")], None, ["kedip"]),
        ("Malam kedua, dua kali.", "kamar_malam", "ranjang", [T(B, "tidur", "takut"), T(L, "tidur")], None, []),
        ("Malam ketujuh, tujuh kali.", "lorong", "sedang", [T(B, e="takut")], B, ["getar"]),
        ("Jadi kupasang kamera kecil di ruang tamu.", "rak_buku", "lebar", [T(B)], B, []),
        ("Jam tiga. Aku memutar rekamannya.", "ruang_tamu", "sedang", [T(B, "duduk", "takut")], B, []),
        ("Tidak ada siapa pun di depan pintu.", "lorong", "cctv", [], None, []),
        ("Tapi pintunya bergetar dari dalam.", "lorong", "cctv", [], None, ["glitch"]),
        ("Lalu seseorang berjalan ke arah pintu.", "lorong", "cctv", [T(L, e="seram")], None, []),
        ("Rambut panjang. Gaun putih. Itu Laras.", "lorong", "cctv_zoom", [T(L, e="seram")], None, ["glitch", "getar"]),
        ("Padahal Laras sedang tidur di sampingku.", "kamar_malam", "ranjang", [T(B, "tidur", "kaget"), T(L, "tidur")], None, ["kejut"]),
    ]
    jam = iter(["03:00:00", "03:00:41", "03:01:02", "03:01:10"])
    bagian = []
    for n, la, sh, tk, bc, ef in s:
        d = dict(narasi=n, latar=la, shot=sh, tokoh=tk, efek=ef)
        if bc:
            d["bicara"] = bc
        if sh.startswith("cctv"):
            d["cctv"] = dict(jam=next(jam), kecepatan=1)
        bagian.append(d)
    return dict(judul="Rumah Nomor 13", deskripsi="Setiap jam tiga pagi, pintu depan diketuk.", tag=["horor"], narator=B,
                tokoh=[dict(nama=B, gender="pria", rambut="pendek", kulit="sawo", baju="abu"),
                       dict(nama=L, gender="wanita", rambut="panjang", kulit="terang", baju="putih")],
                bagian=bagian + [], ringkasan="Bima dan Laras pindah ke rumah nomor 13; pintu diketuk tiap jam tiga.",
                cliffhanger="Rekaman memperlihatkan Laras di depan pintu, padahal Laras tertidur di sampingnya.",
                kartu_akhir=dict(waktu="03:00:00", teks="Jangan buka pintu jam 03.00"))


# ================================================================ suara
def _komunikasi(teks, voice):
    import edge_tts
    try:
        return edge_tts.Communicate(teks, voice, rate=TTS_RATE, pitch=TTS_PITCH, boundary="WordBoundary")
    except TypeError:  # edge-tts versi lama tanpa parameter boundary
        return edge_tts.Communicate(teks, voice, rate=TTS_RATE, pitch=TTS_PITCH)


async def _tts(teks, path, voice):
    com = _komunikasi(teks, voice)
    kata = []
    with open(path, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                mulai = ch["offset"] / 1e7
                kata.append((mulai, mulai + ch["duration"] / 1e7, ch["text"]))
    return kata


def suara(teks, path, voice):
    """Daftar (mulai, selesai, kata) bila server memberi waktu per kata, kalau tidak daftar kosong."""
    if UJI:  # suara tiruan: nada bergetar sepanjang kalimat (hanya untuk uji tampilan)
        dur = max(1.2, 0.36 * len(teks.split()))
        anim.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=f=170:d={dur:.2f}",
                  "-af", "tremolo=f=4.5:d=0.95,volume=0.8", str(path)])
        return []
    for percobaan in range(3):
        try:
            return asyncio.run(_tts(teks, str(path), voice))
        except Exception as e:  # noqa: BLE001
            print(f"Suara gagal (percobaan {percobaan + 1}): {e}")
            time.sleep(3)
    sys.exit("Gagal membuat suara.")


# ================================================================ render paralel
_KAR = {}


def _worker(args):
    b, specs, audio, kata, out, pengantar = args
    cast = {}
    for s in specs:
        k = json.dumps(s, sort_keys=True)
        if k not in _KAR:
            _KAR[k] = anim.Karakter(s)
        cast[s["nama"]] = _KAR[k]
    render_segmen(b, cast, audio, kata, out, set(pengantar))
    return out


def gabung(segmen, out_path):
    daftar = WORK / "list.txt"
    daftar.write_text("".join(f"file '{Path(s).name}'\n" for s in segmen), encoding="utf-8")
    anim.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(daftar),
              "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out_path)])


# ================================================================ upload
def upload_youtube(video, naskah, row):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(None, refresh_token=os.environ["YT_REFRESH_TOKEN"], token_uri="https://oauth2.googleapis.com/token",
                        client_id=os.environ["YT_CLIENT_ID"], client_secret=os.environ["YT_CLIENT_SECRET"],
                        scopes=["https://www.googleapis.com/auth/youtube.upload"])
    creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)
    part, total = int(row["part"]), int(row["total"])
    tag_hash = "#horor #ceritahoror #bersambung #shorts #animasi"
    deskripsi = (f"{naskah['deskripsi']}\n\nPart {part} dari {total} - {row['seri']}\n"
                 + ("Nonton part berikutnya, ikuti channel ini!\n" if part < total else "TAMAT.\n")
                 + f"\n{tag_hash}\nCerita fiksi. Animasi dan narasi dibuat dengan bantuan AI.")
    body = {"snippet": {"title": f"Part {part} | {naskah['judul']}"[:95], "description": deskripsi[:4900],
                        "tags": (naskah["tag"] + ["horor", "cerita horor", "shorts"])[:15], "categoryId": "24"},
            "status": {"privacyStatus": PRIVACY, "selfDeclaredMadeForKids": False}}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print("Terunggah: https://youtube.com/shorts/" + resp["id"])


# ================================================================ main
def main():
    if UJI:
        row = dict(seri="Rumah Nomor 13 (uji)", part="1", total="3", premis="", arah="", status="")
        rows, st = [row], {}
        naskah = validasi(naskah_contoh(), {}, 1, cek_kata=False)
    else:
        rows = baca_topik()
        row = pilih_topik(rows)
        st = muat_state()
        print(f"Seri: {row['seri']} - Part {row['part']}/{row['total']}")
        naskah = tulis_naskah(row, st)
    part, total = int(row["part"]), int(row["total"])
    print("Judul:", naskah["judul"], "| adegan:", len(naskah["bagian"]), "| narator:", naskah["narator"])

    kenal = set(t["nama"] for t in st.get(row["seri"], {}).get("tokoh", [])) if part > 1 else set()
    gender = {t["nama"]: t["gender"] for t in naskah["tokoh"]}
    kerja = []
    for i, b in enumerate(naskah["bagian"]):
        audio, seg = WORK / f"a{i}.mp3", WORK / f"seg{i}.mp4"
        pembicara = b["bicara"]
        voice = VOICE_WANITA if pembicara and pembicara != naskah["narator"] and gender.get(pembicara) == "wanita" else VOICE
        kata = suara(b["narasi"], audio, voice)
        baru = set()
        if b["shot"] in ("lebar", "sedang", "ranjang"):
            baru = {t["nama"] for t in b["tokoh"]} - kenal
            kenal |= baru
        kerja.append((b, naskah["tokoh"], str(audio), kata, str(seg), sorted(baru)))
    print("Suara selesai, mulai render animasi...")

    n = max(1, min(4, os.cpu_count() or 1))
    with ProcessPoolExecutor(max_workers=n) as ex:
        for k, hasil in enumerate(ex.map(_worker, kerja), 1):
            print(f"Bagian {k}/{len(kerja)} selesai")
    segmen = [k[4] for k in kerja]

    ka = naskah["kartu_akhir"]
    kartu = WORK / "kartu.mp4"
    render_kartu_akhir(dict(judul="BERSAMBUNG…" if part < total else "", besar=f"PART {part + 1}" if part < total else "TAMAT",
                            kecil=ka["teks"] if part < total else "Ikuti untuk seri horor berikutnya",
                            rec=ka["waktu"], efek=["glitch"]), kartu)
    segmen.append(kartu)

    final = WORK / ("final_uji.mp4" if UJI else "final.mp4")
    gabung(segmen, final)
    print("Video jadi:", final, f"({anim.durasi(final):.1f} detik)")

    if UPLOAD:
        upload_youtube(final, naskah, row)
        s = st.setdefault(row["seri"], {})
        s["narator"] = naskah["narator"]
        s["tokoh"] = naskah["tokoh"]
        s.setdefault("ringkasan", []).append(f"Part {part}: {naskah['ringkasan']}")
        s["cliffhanger"] = naskah["cliffhanger"]
        simpan_state(st)
        row["status"] = "selesai"
        simpan_topik(rows)


if __name__ == "__main__":
    main()
