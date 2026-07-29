from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)
URLS = [
    "https://www.kkday.com/zh-tw/destination/jp-japan",
    "https://www.kkday.com/en/destination/jp-japan",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
}


def resolve_devalue(flat):
    memo = {}
    wrappers = {"ShallowReactive", "Reactive", "Ref", "ShallowRef", "Readonly"}
    def resolve(value):
        if not isinstance(value, int):
            return value
        if value < 0:
            return None
        if value in memo:
            return memo[value]
        raw = flat[value]
        if isinstance(raw, dict):
            out = {}
            memo[value] = out
            out.update({k: resolve(v) for k, v in raw.items()})
            return out
        if isinstance(raw, list):
            if raw and isinstance(raw[0], str) and raw[0] in wrappers:
                out = resolve(raw[1])
                memo[value] = out
                return out
            out = []
            memo[value] = out
            out.extend(resolve(v) for v in raw)
            return out
        memo[value] = raw
        return raw
    return resolve


def compact(value, depth=0):
    if depth > 5:
        return "<max-depth>"
    if isinstance(value, dict):
        return {k: compact(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [compact(v, depth + 1) for v in value[:100]]
    return value


def main():
    session = requests.Session()
    session.headers.update(HEADERS)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "pages": []}
    for url in URLS:
        response = session.get(url, timeout=60, allow_redirects=True)
        soup = BeautifulSoup(response.text, "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        hrefs = sorted({urljoin(response.url, a.get("href")) for a in soup.find_all("a", href=True)})
        category_hrefs = [h for h in hrefs if "/category/" in urlparse(h).path]
        page = {
            "requested_url": url,
            "status_code": response.status_code,
            "final_url": response.url,
            "title": title,
            "content_length": len(response.content),
            "category_hrefs": category_hrefs,
            "nuxt_dict_candidates": [],
        }
        script = soup.find("script", id="__NUXT_DATA__")
        if script and script.string:
            flat = json.loads(script.string)
            resolve = resolve_devalue(flat)
            interesting_terms = ("category", "product", "destination", "activity", "service", "transport", "ticket")
            for i, raw in enumerate(flat):
                if isinstance(raw, dict):
                    keys = [str(k) for k in raw.keys()]
                    joined = " ".join(keys).lower()
                    if any(term in joined for term in interesting_terms) and len(raw) <= 50:
                        try:
                            value = resolve(i)
                        except Exception:
                            continue
                        text = json.dumps(value, ensure_ascii=False)
                        if "CATEGORY_" in text or "/category/" in text or "productCategory" in text:
                            page["nuxt_dict_candidates"].append({"index": i, "keys": keys, "value": compact(value)})
        report["pages"].append(page)
    report["success"] = all(p["status_code"] == 200 for p in report["pages"])
    (OUTPUT / "destination_category_routes.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT / "summary.json").write_text(json.dumps({
        "success": report["success"],
        "category_href_counts": [len(p["category_hrefs"]) for p in report["pages"]],
        "nuxt_candidate_counts": [len(p["nuxt_dict_candidates"]) for p in report["pages"]],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print((OUTPUT / "summary.json").read_text(encoding="utf-8"))
    return 0 if report["success"] else 2


if __name__ == "__main__":
    sys.exit(main())
