from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
}
BASE = "https://www.kkday.com"
LIST_URLS = [
    f"{BASE}/zh-tw/product/productlist?destination=D-JP-112&page={page}&sort=prec&tab_key="
    for page in (1, 2, 3)
] + [
    f"{BASE}/zh-tw/product/productlist?destination=D-TW-110&page={page}&sort=prec&tab_key="
    for page in (1, 2)
]
TEST_PRODUCT_IDS = [166716, 150665, 601584, 5297]


def decode_nuxt(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="__NUXT_DATA__")
    if script is None or not script.string:
        raise ValueError("missing __NUXT_DATA__")
    flat = json.loads(script.string)
    memo: dict[int, object] = {}

    def resolve(index_or_value):
        if not isinstance(index_or_value, int):
            return index_or_value
        if index_or_value < 0:
            return None
        if index_or_value in memo:
            return memo[index_or_value]
        value = flat[index_or_value]
        if isinstance(value, dict):
            output = {}
            memo[index_or_value] = output
            output.update({key: resolve(ref) for key, ref in value.items()})
            return output
        if isinstance(value, list):
            if value and isinstance(value[0], str) and value[0] in {"ShallowReactive", "Reactive", "Ref", "ShallowRef", "Readonly"}:
                output = resolve(value[1])
                memo[index_or_value] = output
                return output
            output = []
            memo[index_or_value] = output
            output.extend(resolve(ref) for ref in value)
            return output
        memo[index_or_value] = value
        return value

    candidates = []
    for index, value in enumerate(flat):
        if isinstance(value, dict) and {"search", "products", "productCount", "totalPage"}.issubset(value):
            candidates.append(resolve(index))
    if not candidates:
        raise ValueError("product-list state not found")
    return candidates[0]


def fetch_json(session: requests.Session, path: str, params: dict, headers: dict | None = None) -> dict:
    response = session.get(f"{BASE}{path}", params=params, headers=headers, timeout=45, allow_redirects=True)
    result = {
        "requested_url": response.request.url,
        "status_code": response.status_code,
        "final_url": response.url,
        "content_type": response.headers.get("content-type", ""),
        "content_length": len(response.content),
        "text_preview": response.text[:1000],
    }
    try:
        result["json"] = response.json()
    except ValueError:
        result["json"] = None
    return result


def main() -> int:
    session = requests.Session()
    session.headers.update(HEADERS)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "listing_pages": [],
        "fetch_product_tests": [],
        "fetch_products_by_ids_tests": [],
    }

    all_listing_ids: list[int] = []
    for url in LIST_URLS:
        response = session.get(url, timeout=60, allow_redirects=True)
        row = {
            "requested_url": url,
            "status_code": response.status_code,
            "final_url": response.url,
            "content_length": len(response.content),
            "title": "",
            "error": "",
        }
        soup = BeautifulSoup(response.text, "lxml")
        row["title"] = soup.title.get_text(" ", strip=True) if soup.title else ""
        try:
            state = decode_nuxt(response.text)
            products = state.get("products", [])
            ids = [int(product["prod_oid"]) for product in products if product.get("prod_oid")]
            row.update({
                "search": state.get("search"),
                "product_count": state.get("productCount"),
                "total_page": state.get("totalPage"),
                "returned_product_count": len(products),
                "product_ids": ids,
                "product_names": [product.get("name", "") for product in products],
            })
            all_listing_ids.extend(ids)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        report["listing_pages"].append(row)

    for product_id in TEST_PRODUCT_IDS:
        report["fetch_product_tests"].append(
            {
                "product_id": product_id,
                **fetch_json(
                    session,
                    "/api/_nuxt/product/fetch-product",
                    {"id": product_id, "rtk": ""},
                    {"market": "zh-tw", "Referer": f"{BASE}/zh-tw/product/{product_id}"},
                ),
            }
        )

    report["fetch_products_by_ids_tests"].append(
        fetch_json(
            session,
            "/api/_nuxt/product/fetch-products-by-ids",
            [("prod_mids[]", product_id) for product_id in TEST_PRODUCT_IDS] + [("count", len(TEST_PRODUCT_IDS))],
            {"market": "zh-tw", "Referer": LIST_URLS[0]},
        )
    )

    page_id_sets = [tuple(row.get("product_ids", [])) for row in report["listing_pages"][:3]]
    report["summary"] = {
        "japan_pages_are_distinct": len(set(page_id_sets)) == len(page_id_sets) and all(page_id_sets),
        "japan_unique_ids_across_3_pages": len(set().union(*(set(ids) for ids in page_id_sets))),
        "fetch_product_200_count": sum(row["status_code"] == 200 and row.get("json") is not None for row in report["fetch_product_tests"]),
        "batch_endpoint_status": report["fetch_products_by_ids_tests"][0]["status_code"],
        "diagnostic_passed": False,
    }
    report["summary"]["diagnostic_passed"] = bool(
        report["summary"]["japan_pages_are_distinct"]
        and report["summary"]["japan_unique_ids_across_3_pages"] >= 25
        and report["summary"]["fetch_product_200_count"] >= 2
    )

    (OUTPUT / "pagination_and_api_diagnostic.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUTPUT / "summary.json").write_text(
        json.dumps(report["summary"], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0 if report["summary"]["diagnostic_passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
