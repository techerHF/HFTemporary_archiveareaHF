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
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
}

CATEGORY_BASES = [
    "https://www.kkday.com/zh-tw/category/tw-taiwan/experiences/list",
    "https://www.kkday.com/zh-tw/category/jp-japan/experiences/list",
]
PAGE_NUMBERS = [1, 2, 3]
MAX_PRODUCT_CHECKS = 30
REQUEST_TIMEOUT = 35

PRODUCT_PATTERNS = [
    re.compile(r"https?://www\.kkday\.com/zh-tw/product/(\d+)(?:[-/?#][^\"'<>\\ ]*)?", re.I),
    re.compile(r"/zh-tw/product/(\d+)(?:[-/?#][^\"'<>\\ ]*)?", re.I),
    re.compile(r"https?:\\/\\/www\.kkday\.com\\/zh-tw\\/product\\/(\d+)", re.I),
    re.compile(r"\\/zh-tw\\/product\\/(\d+)", re.I),
]


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


def page_url(base: str, page: int) -> str:
    if page == 1:
        return base
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}page={page}"


def fetch(session: requests.Session, url: str) -> tuple[FetchResult, str]:
    started = time.monotonic()
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
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


def extract_product_ids(html: str) -> set[str]:
    if not html:
        return set()
    candidates = {html, unescape(html), html.replace("\\/", "/")}
    product_ids: set[str] = set()
    for candidate in candidates:
        for pattern in PRODUCT_PATTERNS:
            product_ids.update(pattern.findall(candidate))
    return product_ids


def valid_direct_product(product_id: str, result: FetchResult, html: str) -> ProductResult:
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
    if "access denied" in lowered or "captcha" in lowered or "robot" in lowered:
        failure.append("ANTI_BOT_PAGE")
    if len(html) < 5_000:
        failure.append("CONTENT_TOO_SHORT")
    verified = not failure
    return ProductResult(
        product_id=product_id,
        requested_url=f"https://www.kkday.com/zh-tw/product/{product_id}",
        status_code=result.status_code,
        final_url=result.final_url,
        page_title=normalized_title,
        verified=verified,
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

    listing_results: list[dict] = []
    all_ids: set[str] = set()

    for base in CATEGORY_BASES:
        previous_ids: set[str] | None = None
        for page in PAGE_NUMBERS:
            url = page_url(base, page)
            fetched, html = fetch(session, url)
            ids = extract_product_ids(html)
            all_ids.update(ids)
            listing_results.append(
                {
                    **asdict(fetched),
                    "category_base": base,
                    "page_number": page,
                    "product_id_count": len(ids),
                    "product_ids": ",".join(sorted(ids, key=int)),
                    "same_as_previous_page": previous_ids == ids if previous_ids is not None else False,
                }
            )
            previous_ids = ids
            time.sleep(random.uniform(0.45, 0.9))

    product_results: list[dict] = []
    for product_id in sorted(all_ids, key=int)[:MAX_PRODUCT_CHECKS]:
        product_url = f"https://www.kkday.com/zh-tw/product/{product_id}"
        fetched, html = fetch(session, product_url)
        product_results.append(asdict(valid_direct_product(product_id, fetched, html)))
        time.sleep(random.uniform(0.35, 0.75))

    verified_count = sum(1 for row in product_results if row["verified"])
    repeated_pages = sum(1 for row in listing_results if row["same_as_previous_page"])
    summary = {
        "mode": "diagnostic",
        "started_and_finished_at": now_iso(),
        "category_bases": CATEGORY_BASES,
        "pages_attempted": len(listing_results),
        "unique_candidate_product_ids": len(all_ids),
        "products_checked": len(product_results),
        "products_verified": verified_count,
        "products_failed": len(product_results) - verified_count,
        "listing_pages_repeated_from_previous": repeated_pages,
        "diagnostic_passed": len(all_ids) >= 10 and verified_count >= 5,
    }

    write_csv(
        OUTPUT / "listing_pages.csv",
        listing_results,
        [
            "category_base",
            "page_number",
            "requested_url",
            "status_code",
            "final_url",
            "title",
            "content_length",
            "elapsed_seconds",
            "error",
            "product_id_count",
            "product_ids",
            "same_as_previous_page",
        ],
    )
    write_csv(
        OUTPUT / "products_checked.csv",
        product_results,
        [
            "product_id",
            "requested_url",
            "status_code",
            "final_url",
            "page_title",
            "verified",
            "failure_reason",
            "content_length",
            "checked_at",
        ],
    )
    (OUTPUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["diagnostic_passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
