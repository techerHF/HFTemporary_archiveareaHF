from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
}
URLS = {
    "frontend_bundle": "https://cdn.kkday.com/web-b2c/_nuxt/4a9T1Oj1.js",
    "taiwan_listing": "https://www.kkday.com/zh-tw/category/tw-taiwan/experiences/list",
    "japan_listing": "https://www.kkday.com/zh-tw/category/jp-japan/experiences/list",
}
NEEDLES = [
    "getProductListUrl",
    "product/productlist",
    "ProductList",
    "pageSize",
    "page_size",
    "currentPage",
    "current_page",
    "api.kkday",
    "api-",
    "prod_oid",
]


def contexts(text: str, needle: str, radius: int = 1200) -> list[str]:
    found: list[str] = []
    start = 0
    while len(found) < 30:
        index = text.find(needle, start)
        if index < 0:
            break
        found.append(text[max(0, index - radius): min(len(text), index + len(needle) + radius)])
        start = index + len(needle)
    return found


def main() -> int:
    session = requests.Session()
    session.headers.update(HEADERS)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "fetches": {}, "needle_counts": {}}
    success = True
    for name, url in URLS.items():
        try:
            response = session.get(url, timeout=60, allow_redirects=True)
            text = response.text
            (OUTPUT / f"{name}.txt").write_text(text, encoding="utf-8")
            report["fetches"][name] = {
                "requested_url": url,
                "status_code": response.status_code,
                "final_url": response.url,
                "content_length": len(response.content),
            }
            if response.status_code != 200:
                success = False
            if name == "frontend_bundle":
                all_contexts = {}
                for needle in NEEDLES:
                    hits = contexts(text, needle)
                    report["needle_counts"][needle] = len(hits)
                    all_contexts[needle] = hits
                (OUTPUT / "frontend_contexts.json").write_text(
                    json.dumps(all_contexts, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                url_candidates = sorted(set(re.findall(r"https?://[^\"'\\s)]+", text)))
                path_candidates = sorted(set(re.findall(r"/[A-Za-z0-9_./?=&-]{8,}", text)))
                (OUTPUT / "url_candidates.json").write_text(
                    json.dumps(url_candidates, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                (OUTPUT / "path_candidates.json").write_text(
                    json.dumps([p for p in path_candidates if any(k in p.lower() for k in ("product", "search", "api"))], ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except requests.RequestException as exc:
            success = False
            report["fetches"][name] = {"requested_url": url, "error": f"{type(exc).__name__}: {exc}"}
    report["success"] = success
    (OUTPUT / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if success else 2


if __name__ == "__main__":
    sys.exit(main())
