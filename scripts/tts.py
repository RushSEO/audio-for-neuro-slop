#!/usr/bin/env python3
"""Озвучка статьи: по предложению за раз -> один mp3 + точные тайминги для плеера.

    python3 scripts/tts.py shredder                 # движок выбирается сам (см. pick_engine)
    TTS_ENGINE=elevenlabs ELEVENLABS_API_KEY=... python3 scripts/tts.py shredder

Движки (TTS_ENGINE):
  elevenlabs  ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL (по умолч. eleven_multilingual_v2)
  openai      OPENAI_API_KEY, OPENAI_TTS_VOICE (по умолч. onyx), OPENAI_TTS_INSTRUCTIONS
  google      GOOGLE_TTS_API_KEY, GOOGLE_TTS_VOICE (по умолч. ru-RU-Chirp3-HD-Charon)
  piper       PIPER_MODEL=путь/к/ru_RU-*.onnx (локально и бесплатно, нужен файл голоса)
  espeak      без ключей и моделей, звучит роботом — только для проверки механики

Ключи берутся только из переменных окружения, в коде и в репозитории их нет.

Вход:  src/data/articles/<slug>.json (scripts/prepare_article.py)
Выход: public/audio/<slug>.mp3 и src/data/audio/<slug>.json (длительность, главы, тайминги предложений)
Кэш:   .audio-cache/<движок>-<голос>/<sha>.wav — меняется текст одного предложения,
       переозвучивается только оно; не изменилось ничего — mp3 не пересобирается.
"""
import base64
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RATE = 24000                     # все куски приводятся к 24 кГц, моно, 16 бит
PAUSE = {"sentence": 0.28, "paragraph": 0.6, "before_heading": 0.9, "after_heading": 0.65}
MP3_BITRATE = "64k"              # речь в моно: ~0.5 МБ на минуту


# ---------------------------------------------------------------- движки

def http(url: str, body: dict, headers: dict, timeout=120) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise SystemExit(f"{url}: HTTP {e.code} {e.read()[:300]!r}")
    raise SystemExit(f"{url}: не ответил")


class ElevenLabs:
    name = "elevenlabs"

    def __init__(self):
        self.key = os.environ["ELEVENLABS_API_KEY"]
        self.voice = os.environ.get("ELEVENLABS_VOICE_ID") or sys.exit("нужен ELEVENLABS_VOICE_ID (голос из библиотеки ElevenLabs)")
        self.model = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")

    def synth(self, text, prev="", nxt=""):
        # pcm_24000 = сырой 16-битный PCM 24 кГц; previous/next_text держат интонацию между предложениями
        pcm = http(f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice}?output_format=pcm_24000",
                   {"text": text, "model_id": self.model, "previous_text": prev, "next_text": nxt},
                   {"xi-api-key": self.key})
        return pcm, RATE


class OpenAI:
    name = "openai"

    def __init__(self):
        self.key = os.environ["OPENAI_API_KEY"]
        self.voice = os.environ.get("OPENAI_TTS_VOICE", "onyx")
        self.instructions = os.environ.get("OPENAI_TTS_INSTRUCTIONS", "Спокойный дикторский тон, русский язык, ровный темп.")

    def synth(self, text, prev="", nxt=""):
        pcm = http("https://api.openai.com/v1/audio/speech",
                   {"model": "gpt-4o-mini-tts", "voice": self.voice, "input": text,
                    "instructions": self.instructions, "response_format": "pcm"},
                   {"Authorization": f"Bearer {self.key}"})
        return pcm, 24000          # pcm у OpenAI — 24 кГц, 16 бит, моно


class Google:
    name = "google"

    def __init__(self):
        self.key = os.environ["GOOGLE_TTS_API_KEY"]
        self.voice = os.environ.get("GOOGLE_TTS_VOICE", "ru-RU-Chirp3-HD-Charon")

    def synth(self, text, prev="", nxt=""):
        raw = http(f"https://texttospeech.googleapis.com/v1/text:synthesize?key={self.key}",
                   {"input": {"text": text}, "voice": {"languageCode": "ru-RU", "name": self.voice},
                    "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": RATE}}, {})
        wav_bytes = base64.b64decode(json.loads(raw)["audioContent"])
        return read_wav_bytes(wav_bytes)


class Piper:
    name = "piper"

    def __init__(self):
        self.model = os.environ["PIPER_MODEL"]
        self.voice = Path(self.model).stem
        from piper import PiperVoice           # pip install piper-tts
        self.v = PiperVoice.load(self.model)

    def synth(self, text, prev="", nxt=""):
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            with wave.open(f.name, "wb") as w:
                self.v.synthesize_wav(text, w)
            return read_wav_bytes(Path(f.name).read_bytes())


class Espeak:
    """espeak-ng через libespeak-ng из pip-пакета espeakng-loader. Без сети и ключей."""
    name = "espeak"

    def __init__(self):
        import espeakng_loader as el            # pip install espeakng-loader
        self.voice = os.environ.get("ESPEAK_VOICE", "ru")
        lib = ctypes.CDLL(el.get_library_path())
        lib.espeak_Synth.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
                                     ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        lib.espeak_Initialize.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        data_parent = str(Path(el.get_data_path()).parent).encode()
        self.rate = lib.espeak_Initialize(2, 0, data_parent, 0)   # 2 = AUDIO_OUTPUT_SYNCHRONOUS
        if self.rate <= 0:
            sys.exit("espeak-ng не инициализировался")
        lib.espeak_SetVoiceByName(self.voice.encode())
        lib.espeak_SetParameter(1, int(os.environ.get("ESPEAK_RATE", "160")), 0)   # 1 = скорость, слов/мин
        self.buf = bytearray()
        CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_short), ctypes.c_int, ctypes.c_void_p)

        def cb(wav, n, events):
            if wav and n > 0:
                self.buf.extend(ctypes.string_at(wav, n * 2))
            return 0

        self._cb = CB(cb)
        lib.espeak_SetSynthCallback(self._cb)
        self.lib = lib

    def synth(self, text, prev="", nxt=""):
        self.buf = bytearray()
        b = text.encode("utf-8")
        self.lib.espeak_Synth(b, len(b) + 1, 0, 1, 0, 1, None, None)   # flags 1 = UTF-8
        self.lib.espeak_Synchronize()
        return bytes(self.buf), self.rate


ENGINES = {"elevenlabs": ElevenLabs, "openai": OpenAI, "google": Google, "piper": Piper, "espeak": Espeak}


def pick_engine():
    name = os.environ.get("TTS_ENGINE")
    if not name:
        name = next((n for n, env in [("elevenlabs", "ELEVENLABS_API_KEY"), ("openai", "OPENAI_API_KEY"),
                                       ("google", "GOOGLE_TTS_API_KEY"), ("piper", "PIPER_MODEL")] if os.environ.get(env)), "espeak")
    if name not in ENGINES:
        sys.exit(f"неизвестный TTS_ENGINE={name}; есть: {', '.join(ENGINES)}")
    return ENGINES[name]()


# ---------------------------------------------------------------- аудио

def read_wav_bytes(b: bytes):
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(b)
        f.flush()
        with wave.open(f.name) as w:
            if w.getsampwidth() != 2:
                sys.exit("ожидался 16-битный WAV")
            frames = w.readframes(w.getnframes())
            if w.getnchannels() == 2:   # в моно: берём левый канал
                frames = b"".join(frames[i:i + 2] for i in range(0, len(frames), 4))
            return frames, w.getframerate()


def to_rate(pcm: bytes, rate: int) -> bytes:
    """Ресэмплинг куска в 24 кГц через ffmpeg (разные движки отдают 22.05/24/44.1 кГц)."""
    if rate == RATE:
        return pcm
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                        "-ar", str(RATE), "-f", "s16le", "pipe:1"], input=pcm, capture_output=True, check=True)
    return p.stdout


def trim(pcm: bytes, thresh=200) -> bytes:
    """Срезаем тишину по краям куска, чтобы паузы задавали мы, а не движок."""
    a = memoryview(pcm).cast("h")
    i, j = 0, len(a) - 1
    while i < j and abs(a[i]) < thresh:
        i += 1
    while j > i and abs(a[j]) < thresh:
        j -= 1
    pad = int(RATE * 0.03)
    return pcm[max(0, i - pad) * 2:min(len(a), j + pad) * 2]


def silence(sec: float) -> bytes:
    return b"\x00\x00" * int(RATE * sec)


# ---------------------------------------------------------------- сборка

def utterances(article):
    """Плоский список того, что читаем: (id, speak, вид паузы перед куском)."""
    out, prev_type = [], None
    for b in article["blocks"]:
        if b["type"].startswith("h"):
            out.append((b["id"], b["speak"], "before_heading" if out else None, "heading"))
            prev_type = "heading"
            continue
        for k, s in enumerate(b["sentences"]):
            if not out:
                gap = None
            elif k == 0:
                gap = "after_heading" if prev_type == "heading" else "paragraph"
            else:
                gap = "sentence"
            out.append((s["id"], s["speak"], gap, "sentence"))
            prev_type = "sentence"
    return out


def main(slug: str):
    article = json.loads((ROOT / "src/data/articles" / f"{slug}.json").read_text(encoding="utf-8"))
    eng = pick_engine()
    voice = getattr(eng, "voice", "")
    cache = ROOT / ".audio-cache" / f"{eng.name}-{voice}".replace("/", "_")
    cache.mkdir(parents=True, exist_ok=True)

    items = utterances(article)
    pieces, marks, pos, fresh = [], {}, 0.0, 0
    for n, (sid, text, gap, kind) in enumerate(items):
        if gap:
            pieces.append(silence(PAUSE[gap]))
            pos += PAUSE[gap]
        key = hashlib.sha256(f"{eng.name}|{voice}|{text}".encode()).hexdigest()[:20]
        f = cache / f"{key}.pcm"
        if f.exists():
            pcm = f.read_bytes()
        else:
            prev = items[n - 1][1] if n else ""
            nxt = items[n + 1][1] if n + 1 < len(items) else ""
            raw, rate = eng.synth(text, prev, nxt)
            pcm = trim(to_rate(raw, rate))
            f.write_bytes(pcm)
            fresh += 1
            print(f"  {sid:>4} {len(pcm) / 2 / RATE:5.1f} c  {text[:70]}")
        dur = len(pcm) / 2 / RATE
        marks[sid] = [round(pos, 3), round(pos + dur, 3)]
        pieces.append(pcm)
        pos += dur

    pcm_all = b"".join(pieces)
    digest = hashlib.sha256(pcm_all).hexdigest()[:16]
    mp3 = ROOT / "public/audio" / f"{slug}.mp3"
    meta_path = ROOT / "src/data/audio" / f"{slug}.json"
    old = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    if old.get("pcmHash") != digest or not mp3.exists():
        mp3.parent.mkdir(parents=True, exist_ok=True)
        # loudnorm: ровная громкость между кусками и разными движками (-18 LUFS — норма для речи в вебе)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0",
                        "-af", "loudnorm=I=-18:TP=-1.5:LRA=11", "-ar", str(RATE),
                        "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-id3v2_version", "3",
                        "-metadata", f"title={article['title']}", "-metadata", f"comment={article['sourceUrl']}",
                        str(mp3)], input=pcm_all, check=True)
        generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    else:
        generated = old.get("generatedAt")

    chapters = [{"title": c["title"], "start": marks[c["first"]][0] if c["first"] else 0.0} for c in article["chapters"]]
    chapters[0]["start"] = 0.0
    meta = {
        "src": f"/audio/{slug}.mp3",
        "duration": round(pos, 2),
        "bytes": mp3.stat().st_size,
        "engine": eng.name,
        "voice": voice,
        "pcmHash": digest,
        "generatedAt": generated,
        "chapters": chapters,
        "marks": marks,
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{slug}: {eng.name}/{voice}, {pos / 60:.1f} мин, {mp3.stat().st_size / 1e6:.1f} МБ, "
          f"новых кусков {fresh} из {len(items)} -> {mp3.relative_to(ROOT)}")


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        sys.exit("нужен ffmpeg (brew install ffmpeg)")
    main(sys.argv[1] if len(sys.argv) > 1 else "shredder")
