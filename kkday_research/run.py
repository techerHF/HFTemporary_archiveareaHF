from __future__ import annotations

import csv
import json
import random
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)
BASE = "https://www.kkday.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
    "Cache-Control": "no-cache",
}
SEED_URL = f"{BASE}/zh-tw/category/jp-japan/experiences/list"
LOCALES = ["zh-tw", "en"]
CATEGORY_SLUGS = [
    "experiences", "day-tours", "transport", "attraction-tickets",
    "sightseeing-tours", "restaurants", "accommodation",
    "shopping-discount-vouchers", "luggage-services",
    "other-travel-services", "fresh-food", "wifi-sim-cards",
    "airport-services", "shopping", "travel-services",
]
PRIORITY_COUNTRIES = [
    "jp-japan", "tw-taiwan", "kr-south-korea", "sg-singapore",
    "th-thailand", "vn-vietnam", "my-malaysia", "id-indonesia",
    "hk-hong-kong", "mo-macau",
]
COUNTRY_LIMIT = 20
MAX_WORKERS = 6
TIMEOUT = 50


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def decode_page(html: str):
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="__NUXT_DATA__")
    if script is None or not script.string:
        raise ValueError("missing __NUXT_DATA__")
    flat = json.loads(script.string)
    memo = {}
    wrappers = {"ShallowReactive", "Reactive", "Ref", "ShallowRef", "Readonly"}
    def resolve(x):
        if not isinstance(x, int):
            return x
        if x < 0:
            return None
        if x in memo:
            return memo[x]
        raw = flat[x]
        if isinstance(raw, dict):
            out = {}
            memo[x] = out
            out.update({k: resolve(v) for k, v in raw.items()})
            return out
        if isinstance(raw, list):
            if raw and isinstance(raw[0], str) and raw[0] in wrappers:
                out = resolve(raw[1])
                memo[x] = out
                return out
            out = []
            memo[x] = out
            out.extend(resolve(v) for v in raw)
            return out
        memo[x] = raw
        return raw
    state = None
    for i, raw in enumerate(flat):
        if isinstance(raw, dict) and {"search", "products", "productCount", "totalPage", "location_info", "category_hierarchy"}.issubset(raw):
            candidate = resolve(i)
            if isinstance(candidate, dict) and isinstance(candidate.get("products"), list):
                state = candidate
                break
    if state is None:
        raise ValueError("product-list state not found")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return title, state


def discover_countries():
    response = requests.get(SEED_URL, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    _, state = decode_page(response.text)
    countries = []
    for item in state.get("destinations", []):
        if not isinstance(item, dict) or not item.get("urlName"):
            continue
        countries.append({
            "code": item.get("code", ""),
            "name": item.get("name", ""),
            "url_name": item["urlName"],
            "count": int(item.get("count") or 0),
        })
    by_slug = {c["url_name"]: c for c in countries}
    chosen = []
    for slug in PRIORITY_COUNTRIES:
        if slug in by_slug:
            chosen.append(by_slug.pop(slug))
    chosen.extend(sorted(by_slug.values(), key=lambda x: (-x["count"], x["url_name"])))
    return chosen[:COUNTRY_LIMIT], countries


def fetch_slice(task):
    locale, country, category = task
    url = f"{BASE}/{locale}/category/{country['url_name']}/{category}/list/"
    started = time.monotonic()
    try:
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        title, state = decode_page(response.text) if response.status_code == 200 else ("", {})
        location = state.get("location_info") or {}
        hierarchy = state.get("category_hierarchy") or []
        hierarchy_urls = [str(x.get("url", "")) for x in hierarchy if isinstance(x, dict)]
        location_match = location.get("urlName") == country["url_name"]
        category_match = category in hierarchy_urls
        valid_slice = response.status_code == 200 and location_match and category_match
        products = state.get("products", []) if valid_slice else []
        rows = []
        for product in products:
            if not isinstance(product, dict):
                continue
            product_id = product.get("prod_oid")
            name = str(product.get("name") or "").strip()
            if not product_id or not name or product.get("sale_status") != 1:
                continue
            readable = str(product.get("readable_url") or "").strip()
            product_path = f"/{locale}/product/{product_id}" + (f"-{readable}" if readable else "")
            destinations = product.get("destinations") or []
            rows.append({
                "product_id": str(product_id),
                "prod_mid": str(product.get("prod_mid") or ""),
                "locale": locale,
                "name": name,
                "introduction": str(product.get("introduction") or "").strip(),
                "product_url": f"{BASE}{product_path}",
                "affiliate_url": f"{BASE}{product_path}?cid={'25978' if locale == 'zh-tw' else '25979'}",
                "requested_country_code": country["code"],
                "requested_country_name": country["name"],
                "requested_country_slug": country["url_name"],
                "requested_category_slug": category,
                "source_url": url,
                "source_page_title": title,
                "source_product_count": state.get("productCount"),
                "source_total_page": state.get("totalPage"),
                "product_category_main": (product.get("product_category") or {}).get("main", ""),
                "product_category_sub": json.dumps((product.get("product_category") or {}).get("sub", []), ensure_ascii=False),
                "product_destinations": json.dumps(destinations, ensure_ascii=False),
                "rating_count": product.get("rating_count"),
                "rating_star": product.get("rating_star"),
                "order_count": product.get("show_order_count"),
                "earliest_sale_date": product.get("earliest_sale_date"),
                "sale_status": product.get("sale_status"),
                "currency": product.get("currency"),
                "min_price": product.get("min_price"),
                "official_price": product.get("official_price"),
                "rq_session_id": product.get("rqSessionId", ""),
                "algo_version": product.get("algoVersion", ""),
                "checked_at": now_iso(),
            })
        return {
            "url": url,
            "locale": locale,
            "country_slug": country["url_name"],
            "category_slug": category,
            "status_code": response.status_code,
            "final_url": response.url,
            "content_length": len(response.content),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "title": title,
            "location_url_name": location.get("urlName", ""),
            "hierarchy_urls": hierarchy_urls,
            "location_match": location_match,
            "category_match": category_match,
            "valid_slice": valid_slice,
            "product_count": state.get("productCount"),
            "total_page": state.get("totalPage"),
            "returned_verified_products": len(rows),
            "error": "",
            "products": rows,
        }
    except Exception as exc:
        return {
            "url": url, "locale": locale, "country_slug": country["url_name"],
            "category_slug": category, "status_code": None, "final_url": "",
            "content_length": 0, "elapsed_seconds": round(time.monotonic() - started, 3),
            "title": "", "location_url_name": "", "hierarchy_urls": [],
            "location_match": False, "category_match": False, "valid_slice": False,
            "product_count": None, "total_page": None, "returned_verified_products": 0,
            "error": f"{type(exc).__name__}: {exc}", "products": [],
        }


def write_csv(path, rows, fields):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def main():
    countries, all_countries = discover_countries()
    tasks = [(locale, country, category) for country in countries for category in CATEGORY_SLUGS for locale in LOCALES]
    slice_rows = []
    occurrences = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_slice, task) for task in tasks]
        for future in as_completed(futures):
            result = future.result()
            occurrences.extend(result.pop("products"))
            result["hierarchy_urls"] = json.dumps(result["hierarchy_urls"], ensure_ascii=False)
            slice_rows.append(result)
            time.sleep(random.uniform(0.01, 0.04))

    by_id = defaultdict(list)
    for row in occurrences:
        by_id[row["product_id"]].append(row)
    unique_rows = []
    for product_id, rows in by_id.items():
        locales = sorted({r["locale"] for r in rows})
        sources = sorted({r["source_url"] for r in rows})
        zh_names = sorted({r["name"] for r in rows if r["locale"] == "zh-tw"})
        en_names = sorted({r["name"] for r in rows if r["locale"] == "en"})
        categories = sorted({r["requested_category_slug"] for r in rows})
        countries_seen = sorted({r["requested_country_slug"] for r in rows})
        unique_rows.append({
            "product_id": product_id,
            "occurrence_count": len(rows),
            "distinct_source_count": len(sources),
            "locales": ",".join(locales),
            "present_both_locales": set(locales) == {"en", "zh-tw"},
            "zh_tw_names": json.dumps(zh_names, ensure_ascii=False),
            "en_names": json.dumps(en_names, ensure_ascii=False),
            "categories": ",".join(categories),
            "countries": ",".join(countries_seen),
            "tier_a_two_locales": set(locales) == {"en", "zh-tw"} and len(sources) >= 2,
            "tier_b_two_official_sources": len(sources) >= 2,
            "sample_zh_url": next((r["affiliate_url"] for r in rows if r["locale"] == "zh-tw"), ""),
            "sample_en_url": next((r["affiliate_url"] for r in rows if r["locale"] == "en"), ""),
        })

    slice_rows.sort(key=lambda r: r["url"])
    occurrences.sort(key=lambda r: (int(r["product_id"]), r["locale"], r["source_url"]))
    unique_rows.sort(key=lambda r: int(r["product_id"]))
    valid_slices = [r for r in slice_rows if r["valid_slice"]]
    summary = {
        "mode": "medium_slice_matrix",
        "checked_at": now_iso(),
        "selected_countries": countries,
        "all_country_count_discovered": len(all_countries),
        "categories_tested": CATEGORY_SLUGS,
        "locales": LOCALES,
        "slice_requests": len(slice_rows),
        "valid_slices": len(valid_slices),
        "invalid_slices": len(slice_rows) - len(valid_slices),
        "official_product_occurrences": len(occurrences),
        "unique_product_ids": len(unique_rows),
        "tier_a_two_locales": sum(bool(r["tier_a_two_locales"]) for r in unique_rows),
        "tier_b_two_official_sources": sum(bool(r["tier_b_two_official_sources"]) for r in unique_rows),
        "eligible_for_full_scale": len(unique_rows) >= 500 and sum(bool(r["tier_b_two_official_sources"]) for r in unique_rows) >= 250,
    }
    write_csv(OUTPUT / "slice_results.csv", slice_rows, [
        "url", "locale", "country_slug", "category_slug", "status_code", "final_url",
        "content_length", "elapsed_seconds", "title", "location_url_name", "hierarchy_urls",
        "location_match", "category_match", "valid_slice", "product_count", "total_page",
        "returned_verified_products", "error",
    ])
    if occurrences:
        write_csv(OUTPUT / "product_occurrences.csv", occurrences, list(occurrences[0].keys()))
    if unique_rows:
        write_csv(OUTPUT / "unique_product_summary.csv", unique_rows, list(unique_rows[0].keys()))
    (OUTPUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["eligible_for_full_scale"] else 2


if __name__ == "__main__":
    sys.exit(main())
