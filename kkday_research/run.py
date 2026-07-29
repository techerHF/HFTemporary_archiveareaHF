from __future__ import annotations

import csv
import json
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

CATEGORY_BASES = [
    "https://www.kkday.com/zh-tw/category/tw-taiwan/experiences/list",
    "https://www.kkday.com/zh-tw/category/jp-japan/experiences/list",
]
REQUEST_TIMEOUT = 40
MAX_PRODUCT_CHECKS = 25
MAX_JS_FILES = 15

PRODUCT_HREF_RE = re.compile(
    r"(?:https?:)?(?:\\/\\/|//)?(?:www\\.)?kkday\\.com(?:\\/|/)(?:zh-tw|en)(?:\\/|/)product(?:\\/|/)(\d+)([^\"'<> ]*)",
    re.I,
)
ENDPOINT_HINT_RE = re.compile(
    r"[^\"'\\s]{0,160}(?:ajax_productlist|productlist|product-list|search/products|api/v\d+|page_size|pageSize|current_page|currentPage)[^\"'\\s]{0,220}",
    re.I,
)


@dataclass
class FetchResult:
    requested_url: str
    status_code: int | None
    final_url: str
    title: str
    content_length: int
    elapsed_seconds: float
    error: str


@dataclass
class ProductResult:
    product_id: str
    source_listing_url: str
    requested_url: str
    status_code: int | None
    final_url: str
    page_title: str
    verified: bool
    failure_reason: str
    content_length: int
    checked_at: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch(
    session: requests.Session,
    url: str,
    *,
    referer: str | None = None,
) -> tuple[FetchResult, str]:
    headers = {"Referer": referer} if referer else None
    started = time.monotonic()
    try:
        response = session.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        elapsed = time.monotonic() - started
        text = response.text
        soup = BeautifulSoup(text, "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        return (
            FetchResult(
                requested_url=url,
                status_code=response.status_code,
                final_url=response.url,
                title=title,
                content_length=len(response.content),
                elapsed_seconds=round(elapsed, 3),
                error="",
            ),
            text,
        )
    except requests.RequestException as exc:
        elapsed = time.monotonic() - started
        return (
            FetchResult(
                requested_url=url,
                status_code=None,
                final_url="",
                title="",
                content_length=0,
                elapsed_seconds=round(elapsed, 3),
                error=f"{type(exc).__name__}: {exc}",
            ),
            "",
        )


def normalize_href(href: str, base_url: str) -> str:
    href = unescape(href).replace("\\/", "/").strip()
    if href.startswith("//"):
        href = "https:" + href
    return urljoin(base_url, href)


def extract_product_links(html: str, base_url: str) -> dict[str, str]:
    links: dict[str, str] = {}
    if not html:
        return links
    soup = BeautifulSoup(html, "lxml")
    for anchor in soup.select('a[href*="/product/"]'):
        href = normalize_href(anchor.get("href", ""), base_url)
        match = re.search(r"/(?:zh-tw|en)/product/(\d+)", urlparse(href).path, re.I)
        if match:
            links.setdefault(match.group(1), href)
    decoded_variants = {html, unescape(html), html.replace("\\/", "/")}
    for variant in decoded_variants:
        for match in PRODUCT_HREF_RE.finditer(variant):
            product_id = match.group(1)
            raw = match.group(0).replace("\\/", "/")
            if raw.startswith("//"):
                raw = "https:" + raw
            elif not raw.startswith("http"):
                raw = "https://www.kkday.com/" + raw.lstrip("/")
            links.setdefault(product_id, raw)
    return links


def inspect_listing_html(html: str, base_url: str) -> tuple[list[str], list[str], list[str]]:
    soup = BeautifulSoup(html, "lxml")
    script_urls = [urljoin(base_url, tag.get("src")) for tag in soup.find_all("script", src=True)]
    endpoint_hints = sorted({re.sub(r"\\/", "/", hit) for hit in ENDPOINT_HINT_RE.findall(html)})
    category_links = sorted(
        {
            normalize_href(anchor.get("href", ""), base_url)
            for anchor in soup.select('a[href*="/category/"][href*="/experiences/list"]')
        }
    )
    return script_urls, endpoint_hints, category_links


def valid_direct_product(
    product_id: str,
    source_listing_url: str,
    result: FetchResult,
    html: str,
) -> ProductResult:
    failure: list[str] = []
    if result.status_code != 200:
        failure.append(f"HTTP_{result.status_code}")
    final_path = urlparse(result.final_url).path
    match = re.search(r"/product/(\d+)", final_path)
    if not match:
        failure.append("FINAL_URL_NOT_PRODUCT")
    elif match.group(1) != product_id:
        failure.append(f"PRODUCT_ID_MISMATCH_{match.group(1)}")
    normalized_title = re.sub(r"\s+", " ", result.title).strip()
    if not normalized_title:
        failure.append("MISSING_TITLE")
    lowered = normalized_title.lower()
    if any(token in lowered for token in ("access denied", "captcha", "robot", "kkday.com")) and len(html) < 5000:
        failure.append("ANTI_BOT_PAGE")
    if len(html) < 5_000:
        failure.append("CONTENT_TOO_SHORT")
    return ProductResult(
        product_id=product_id,
        source_listing_url=source_listing_url,
        requested_url=result.requested_url,
        status_code=result.status_code,
        final_url=result.final_url,
        page_title=normalized_title,
        verified=not failure,
        failure_reason="|".join(failure),
        content_length=result.content_length,
        checked_at=now_iso(),
    )


def write_csv(path: Path, rows: Iterable[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    session = requests.Session()
    session.headers.update(HEADERS)

    listing_rows: list[dict] = []
    all_product_links: dict[str, tuple[str, str]] = {}
    all_script_urls: set[str] = set()
    endpoint_hints: set[str] = set()
    discovered_categories: set[str] = set()

    for base in CATEGORY_BASES:
        fetched, html = fetch(session, base)
        links = extract_product_links(html, base)
        scripts, hints, categories = inspect_listing_html(html, base)
        all_script_urls.update(scripts)
        endpoint_hints.update(hints)
        discovered_categories.update(categories)
        for product_id, product_url in links.items():
            all_product_links.setdefault(product_id, (product_url, base))
        listing_rows.append(
            {
                **asdict(fetched),
                "category_base": base,
                "product_link_count": len(links),
                "product_links": json.dumps(links, ensure_ascii=False, sort_keys=True),
                "script_count": len(scripts),
                "endpoint_hint_count": len(hints),
                "discovered_category_count": len(categories),
            }
        )
        time.sleep(random.uniform(0.6, 1.1))

    js_rows: list[dict] = []
    for script_url in sorted(all_script_urls)[:MAX_JS_FILES]:
        fetched, js_text = fetch(session, script_url, referer=CATEGORY_BASES[0])
        hints = sorted({re.sub(r"\\/", "/", hit) for hit in ENDPOINT_HINT_RE.findall(js_text)})
        endpoint_hints.update(hints)
        js_rows.append(
            {
                **asdict(fetched),
                "endpoint_hint_count": len(hints),
                "endpoint_hints": json.dumps(hints[:100], ensure_ascii=False),
            }
        )
        time.sleep(random.uniform(0.2, 0.45))

    product_rows: list[dict] = []
    for product_id, (product_url, source_listing_url) in list(sorted(all_product_links.items(), key=lambda item: int(item[0])))[:MAX_PRODUCT_CHECKS]:
        fetched, html = fetch(session, product_url, referer=source_listing_url)
        product_rows.append(
            asdict(valid_direct_product(product_id, source_listing_url, fetched, html))
        )
        time.sleep(random.uniform(0.45, 0.8))

    verified_count = sum(1 for row in product_rows if row["verified"])
    status_counts: dict[str, int] = {}
    for row in product_rows:
        key = str(row["status_code"])
        status_counts[key] = status_counts.get(key, 0) + 1

    summary = {
        "mode": "diagnostic_v2_slug_and_endpoint_discovery",
        "finished_at": now_iso(),
        "listing_pages": len(listing_rows),
        "unique_candidate_product_ids": len(all_product_links),
        "full_slug_urls_found": sum(1 for url, _ in all_product_links.values() if re.search(r"/product/\d+-", urlparse(url).path)),
        "discovered_country_or_category_urls": len(discovered_categories),
        "script_urls_found": len(all_script_urls),
        "endpoint_hints_found": len(endpoint_hints),
        "products_checked": len(product_rows),
        "products_verified": verified_count,
        "product_status_counts": status_counts,
        "diagnostic_passed": len(all_product_links) >= 10 and verified_count >= 5,
    }

    write_csv(
        OUTPUT / "listing_pages.csv",
        listing_rows,
        [
            "category_base", "requested_url", "status_code", "final_url", "title",
            "content_length", "elapsed_seconds", "error", "product_link_count",
            "product_links", "script_count", "endpoint_hint_count", "discovered_category_count",
        ],
    )
    write_csv(
        OUTPUT / "products_checked.csv",
        product_rows,
        [
            "product_id", "source_listing_url", "requested_url", "status_code", "final_url",
            "page_title", "verified", "failure_reason", "content_length", "checked_at",
        ],
    )
    write_csv(
        OUTPUT / "javascript_inspection.csv",
        js_rows,
        [
            "requested_url", "status_code", "final_url", "title", "content_length",
            "elapsed_seconds", "error", "endpoint_hint_count", "endpoint_hints",
        ],
    )
    (OUTPUT / "endpoint_hints.json").write_text(
        json.dumps(sorted(endpoint_hints), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUTPUT / "discovered_categories.json").write_text(
        json.dumps(sorted(discovered_categories), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUTPUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["diagnostic_passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
