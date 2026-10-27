#!/usr/bin/env python3
"""Снимок статьи Википедии через MediaWiki API (plain text, заголовки == ... ==).

    python3 scripts/fetch_wiki.py "https://ru.wikipedia.org/wiki/Шреддер_(Черепашки-ниндзя)" shredder --words 1000

Пишет data-src/<slug>.raw.txt и data-src/<slug>.meta.json. Берётся конкретная ревизия (revid),
чтобы озвучка совпадала с текстом на странице и атрибуция CC BY-SA указывала версию.
Для своих статей этот шаг не нужен: prepare_article.py читает текст сайта напрямую.
"""
import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("slug")
    ap.add_argument("--words", type=int, default=1000)
    a = ap.parse_args()

    u = urllib.parse.urlparse(a.url)
    title = urllib.parse.unquote(u.path.split("/wiki/", 1)[1]).replace("_", " ")
    lang = u.netloc.split(".")[0]
    q = urllib.parse.urlencode({"action": "query", "prop": "extracts|revisions", "rvprop": "ids|timestamp",
                                "explaintext": 1, "exsectionformat": "wiki", "format": "json", "titles": title})
    req = urllib.request.Request(f"https://{u.netloc}/w/api.php?{q}",
                                 headers={"User-Agent": "article-audio/0.1 (audio versions of articles)"})
    page = next(iter(json.load(urllib.request.urlopen(req, timeout=30))["query"]["pages"].values()))

    src = ROOT / "data-src"
    src.mkdir(exist_ok=True)
    (src / f"{a.slug}.raw.txt").write_text(page["extract"], encoding="utf-8")
    rev = page["revisions"][0]
    meta = {"slug": a.slug, "lang": lang, "title": page["title"], "sourceUrl": a.url, "sourceName": "Википедия",
            "revid": rev["revid"], "revTimestamp": rev["timestamp"], "license": "CC BY-SA 4.0",
            "licenseUrl": "https://creativecommons.org/licenses/by-sa/4.0/deed.ru", "maxWords": a.words}
    (src / f"{a.slug}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{page['title']}: ревизия {rev['revid']} от {rev['timestamp']}, {len(page['extract'])} символов")


if __name__ == "__main__":
    main()
