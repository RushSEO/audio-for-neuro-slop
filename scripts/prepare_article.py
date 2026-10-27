#!/usr/bin/env python3
"""Снимок статьи -> структура для страницы и для озвучки.

Вход:  data-src/<slug>.raw.txt  (plain text, заголовки в вики-разметке == ... ==)
       data-src/<slug>.meta.json
       data-src/lexicon.<lang>.json
Выход: src/data/articles/<slug>.json

Каждое предложение получает id. Страница показывает поле text, озвучка читает поле speak,
и по id плеер подсвечивает то предложение, которое сейчас звучит.
"""
import json
import re
import sys
from pathlib import Path

from num2words import num2words

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data-src"
OUT = ROOT / "src" / "data" / "articles"

HEADING = re.compile(r"^(={2,4})\s*(.+?)\s*\1$")
# точка после этих сокращений не конец предложения
ABBR = ("англ.", "яп.", "кит.", "нем.", "фр.", "т. е.", "т. д.", "т. п.", "т. н.", "и др.", "г.", "гг.", "см.", "др.", "св.", "им.")
SENT_END = re.compile(r"([.!?…][»\"”)]?)\s+(?=[«\"(—A-ZА-ЯЁ0-9])")
ABBR_RX = re.compile(r"(?<!\w)(" + "|".join(re.escape(a[:-1]) for a in sorted(ABBR, key=len, reverse=True)) + r")\.(?= )")


def split_sentences(par: str) -> list[str]:
    """Режет абзац на предложения, не ломая сокращения вроде «англ. Shredder»."""
    protected = ABBR_RX.sub(lambda m: m[1] + "\u0000", par)   # точку сокращения временно прячем
    parts, start = [], 0
    for m in SENT_END.finditer(protected):
        parts.append(protected[start:m.end(1)])
        start = m.end()
    parts.append(protected[start:])
    return [p.replace("\u0000", ".").strip() for p in parts if p.strip()]


def ordinal(n: int, case: str = "nominative", gender: str = "m") -> str:
    return num2words(n, lang="ru", to="ordinal", case=case, gender=gender)


def normalize_numbers_ru(s: str) -> str:
    """Числа, которые TTS читает в неверном падеже: годы и порядковые «2-м»."""
    s = re.sub(r"\b(\d{3,4})\s+(году)\b", lambda m: f"{ordinal(int(m[1]), 'prepositional')} {m[2]}", s)
    s = re.sub(r"\b(\d{3,4})\s+(года)\b", lambda m: f"{ordinal(int(m[1]), 'genitive')} {m[2]}", s)
    s = re.sub(r"\b(\d+)-м\b", lambda m: ordinal(int(m[1]), "prepositional"), s)
    s = re.sub(r"\b(\d+)-го\b", lambda m: ordinal(int(m[1]), "genitive"), s)
    s = re.sub(r"\b(\d+)\s+(место)\b", lambda m: f"{ordinal(int(m[1]), 'nominative', 'n')} {m[2]}", s)
    return s


def make_speaker(lang: str):
    lex_path = SRC / f"lexicon.{lang}.json"
    rules = json.loads(lex_path.read_text(encoding="utf-8"))["rules"] if lex_path.exists() else []
    compiled = [(re.compile(p), r) for p, r in rules]

    def speak(text: str) -> str:
        s = text
        for rx, rep in compiled:
            s = rx.sub(rep, s)
        if lang == "ru":
            s = normalize_numbers_ru(s)
        s = re.sub(r"^—\s*", "", s)          # «— Питер Лэрд.» -> «Питер Лэрд.»
        s = re.sub(r"\s{2,}", " ", s).strip()
        s = re.sub(r"\s+([,.;:!?])", r"\1", s)
        return s

    return speak


def parse(raw: str):
    """Блоки: h2/h3, p, quote (цитата после строки с двоеточием), cite (строка «— Автор»)."""
    blocks, prev_line, blank_run = [], None, 0
    for line in raw.split("\n"):
        t = line.strip()
        if not t:
            blank_run += 1
            continue
        m = HEADING.match(t)
        if m:
            blocks.append({"type": f"h{len(m[1])}", "text": m[2]})
        elif t.startswith("— ") and blocks and blocks[-1]["type"] == "quote":
            blocks.append({"type": "cite", "text": t})
        elif blank_run == 1 and prev_line and prev_line.endswith(":"):
            blocks.append({"type": "quote", "text": t})
        else:
            blocks.append({"type": "p", "text": t})
        prev_line, blank_run = t, 0
    return blocks


def main(slug: str):
    meta = json.loads((SRC / f"{slug}.meta.json").read_text(encoding="utf-8"))
    raw = (SRC / f"{slug}.raw.txt").read_text(encoding="utf-8")
    speak = make_speaker(meta.get("lang", "ru"))
    max_words = int(meta.get("maxWords") or 10**9)

    blocks, words, sid, done = [], 0, 0, False
    for b in parse(raw):
        if done:
            break
        if b["type"].startswith("h"):
            sid += 1
            blocks.append({**b, "id": f"s{sid}", "speak": speak(b["text"]) + "."})
            continue
        sents = []
        for s in split_sentences(b["text"]):
            sid += 1
            sents.append({"id": f"s{sid}", "text": s, "speak": speak(s)})
            words += len(s.split())
            if words >= max_words:   # дочитываем предложение до конца и останавливаемся
                done = True
                break
        blocks.append({"type": b["type"], "sentences": sents})

    while blocks and blocks[-1]["type"].startswith("h"):   # не оставляем пустой заголовок в конце
        blocks.pop()

    # главы: заголовок открывает главу; заголовки подряд без текста склеиваются («Биография. Mirage и Image»)
    chapters, pending = [{"title": "Вступление", "first": None}], []
    for b in blocks:
        if b["type"].startswith("h"):
            pending.append(b)
            continue
        if pending:
            chapters.append({"title": ". ".join(h["text"] for h in pending), "first": pending[0]["id"]})
            pending = []
        if chapters[-1]["first"] is None:
            chapters[-1]["first"] = b["sentences"][0]["id"]

    out = {
        **meta,
        "wordsRead": words,
        "wordsTotalInSnapshot": len([w for line in raw.split("\n") if not HEADING.match(line.strip()) for w in line.split()]),
        "blocks": blocks,
        "chapters": chapters,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{slug}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n_sent = sum(len(b.get("sentences", [])) for b in blocks)
    print(f"{slug}: {words} слов, {n_sent} предложений, {len(chapters)} глав -> {OUT / (slug + '.json')}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "shredder")
