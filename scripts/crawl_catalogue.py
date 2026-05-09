#!/usr/bin/env python3
"""Slowly crawl PerfectDraft keg pages and write parsed catalogue data.

This is a developer maintenance script. It is intentionally not used by the
Home Assistant integration at runtime.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pprint
import random
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOP_PATH = ROOT / "custom_components" / "perfectdraft" / "shop.py"
CATALOGUE_PATH = ROOT / "custom_components" / "perfectdraft" / "catalogue.py"
DEFAULT_OUTPUT = ROOT / "catalogue-crawl.json"
DEFAULT_HISTORY = ROOT / "catalogue-history.json"
DEFAULT_DELAY = 90
USER_AGENT = (
    "PerfectDraftCatalogueMaintenance/0.1 "
    "(manual low-frequency catalogue update script)"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_shop_module():
    return _load_module("perfectdraft_shop", SHOP_PATH)


def _load_catalogue_module():
    return _load_module("perfectdraft_catalogue", CATALOGUE_PATH)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as err:
        raise RuntimeError(f"{path} is not valid JSON: {err}") from err
    return data if isinstance(data, dict) else {}


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", "ignore")


def _sitemap_urls(source: str) -> list[str]:
    xml = _fetch(source)
    root = ET.fromstring(xml)
    namespace = ""
    if root.tag.startswith("{"):
        namespace = root.tag.split("}", 1)[0] + "}"
    return [
        loc.text.strip()
        for loc in root.findall(f".//{namespace}loc")
        if loc.text
    ]


def _product_urls(sitemap: str) -> list[str]:
    urls: list[str] = []
    pending = [sitemap]
    seen: set[str] = set()
    while pending:
        source = pending.pop(0)
        if source in seen:
            continue
        seen.add(source)
        for url in _sitemap_urls(source):
            if url.endswith(".xml"):
                pending.append(url)
            elif _looks_like_single_keg_url(url):
                urls.append(url)
    return sorted(set(urls))


def _looks_like_single_keg_url(url: str) -> bool:
    lower = url.lower()
    if not re.search(r"/en-gb/.*6l.*keg", lower):
        return False
    if re.search(r"\d+\s*-?\s*x\s*-?\s*6l", lower) or "6l-kegs" in lower:
        return False
    return not any(
        excluded in lower
        for excluded in ("pack", "bundle", "machine", "starter", "multipack")
    )


def _normalise_text(value: object) -> str:
    text = str(value or "").casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _field_value(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("value")
    if value in (None, ""):
        return None
    return str(value)


def _identity_keys(entry: dict[str, object]) -> set[str]:
    keys: set[str] = set()
    for field in ("sku", "gtin", "sap_product_code"):
        value = _field_value(entry.get(field))
        if value:
            keys.add(f"{field}:{value.casefold()}")

    name = _normalise_text(entry.get("name") or entry.get("product_name"))
    brewery = _normalise_text(_field_value(entry.get("brewery")))
    if name:
        keys.add(f"name:{name}")
    if name and brewery:
        keys.add(f"name_brewery:{name}|{brewery}")
    return keys


def _static_products() -> dict[str, dict]:
    catalogue = _load_catalogue_module()
    products = {
        str(product_id): dict(entry)
        for product_id, entry in getattr(catalogue, "PERFECTDRAFT_CATALOGUE", {}).items()
        if isinstance(entry, dict)
    }
    for product_id, entry in getattr(catalogue, "CATALOGUE_OVERRIDES", {}).items():
        if isinstance(entry, dict):
            products.setdefault(str(product_id), dict(entry))
    return products


def _history_products(history: dict) -> dict[str, dict]:
    products = history.get("products")
    return {
        str(product_id): dict(entry)
        for product_id, entry in products.items()
        if isinstance(products, dict) and isinstance(entry, dict)
    } if isinstance(products, dict) else {}


def _merge_history(
    *,
    history: dict,
    static_products: dict[str, dict],
    current_products: dict[str, dict],
    generated_at: str,
    source_sitemap: str,
) -> dict:
    """Merge current crawl results into a durable catalogue history."""
    products: dict[str, dict] = {}

    for product_id, entry in static_products.items():
        seeded = dict(entry)
        seeded.setdefault("catalogue_source", "static")
        products[product_id] = seeded

    for product_id, entry in _history_products(history).items():
        merged = dict(products.get(product_id) or {})
        merged.update(entry)
        products[product_id] = merged

    seen = set(current_products)
    for product_id, entry in current_products.items():
        previous = dict(products.get(product_id) or {})
        merged = dict(previous)
        merged.update(entry)
        merged["first_seen"] = previous.get("first_seen") or generated_at
        merged["last_seen"] = generated_at
        merged["retired"] = False
        merged.pop("retired_at", None)
        products[product_id] = merged

    if current_products:
        for product_id, entry in list(products.items()):
            if product_id in seen:
                continue
            if entry.get("retired") is True:
                continue
            entry["retired"] = True
            entry["retired_at"] = generated_at
            entry.setdefault("last_seen", entry.get("catalogue_last_seen"))
            products[product_id] = entry

    aliases = dict(history.get("aliases") or {})
    if current_products:
        aliases.update(_alias_candidates(products, seen, existing_only=True))

    return {
        "schema_version": 1,
        "updated_at": generated_at,
        "source_sitemap": source_sitemap,
        "products": dict(sorted(products.items())),
        "aliases": dict(sorted(aliases.items())),
    }


def _alias_candidates(
    products: dict[str, dict],
    active_ids: set[str],
    *,
    existing_only: bool = False,
) -> dict[str, str]:
    """Return likely old-id -> current-id mappings from stable product identity."""
    key_to_ids: dict[str, set[str]] = {}
    for product_id, entry in products.items():
        for key in _identity_keys(entry):
            key_to_ids.setdefault(key, set()).add(product_id)

    aliases: dict[str, str] = {}
    for product_id, entry in products.items():
        if existing_only and entry.get("retired") is not True:
            continue
        matches: set[str] = set()
        for key in _identity_keys(entry):
            matches.update(key_to_ids.get(key, set()))
        matches.discard(product_id)
        active_matches = sorted(matches & active_ids)
        if len(active_matches) == 1:
            aliases[product_id] = active_matches[0]
    return aliases


def _write_python_catalogue(
    path: Path,
    history: dict,
    generated_at: str,
) -> None:
    """Write a reviewable Python catalogue fragment from history."""
    products = history.get("products") or {}
    aliases = history.get("aliases") or {}
    body = [
        '"""Generated PerfectDraft catalogue snapshot.',
        "",
        "Review this file before copying values into",
        "custom_components/perfectdraft/catalogue.py.",
        f"Generated at: {generated_at}",
        '"""',
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        f"PERFECTDRAFT_CATALOGUE: dict[str, dict[str, Any]] = {pprint.pformat(products, sort_dicts=True, width=100)}",
        "",
        f"CATALOGUE_ALIASES: dict[str, str] = {pprint.pformat(aliases, sort_dicts=True, width=100)}",
        "",
    ]
    path.write_text("\n".join(body))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sitemap",
        default="https://www.perfectdraft.com/en-gb/sitemap.xml",
        help="Sitemap URL to seed the crawl from.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="JSON output path.",
    )
    parser.add_argument(
        "--history",
        type=Path,
        default=DEFAULT_HISTORY,
        help="Durable history JSON path used to retain removed products and ID changes.",
    )
    parser.add_argument(
        "--python-output",
        type=Path,
        help="Optional review file containing Python catalogue data generated from history.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Seconds to wait between product-page fetches.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of product pages to fetch. 0 means no limit.",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="Only list matching product URLs without fetching product pages.",
    )
    args = parser.parse_args()

    shop = _load_shop_module()
    urls = _product_urls(args.sitemap)
    if args.limit:
        urls = urls[: args.limit]

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    output = {
        "generated_at": generated_at,
        "source_sitemap": args.sitemap,
        "products": {},
        "historical_products": {},
        "retired_products": {},
        "alias_candidates": {},
        "errors": {},
    }

    if args.discover_only:
        output["product_urls"] = urls
        args.output.write_text(json.dumps(output, indent=2, sort_keys=True))
        print(f"Wrote {len(urls)} discovered product URLs to {args.output}")
        return 0

    for index, url in enumerate(urls, start=1):
        print(f"[{index}/{len(urls)}] {url}", flush=True)
        try:
            page = _fetch(url)
            data = shop.parse_shop_product_page(url, page)
            key = data.get("bp_product_id") or data.get("website_product_id") or url
            output["products"][str(key)] = data
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            output["errors"][url] = str(err)

        args.output.write_text(json.dumps(output, indent=2, sort_keys=True))
        if index < len(urls):
            time.sleep(args.delay + random.uniform(0, min(args.delay * 0.2, 30)))

    history = _merge_history(
        history=_load_json(args.history),
        static_products=_static_products(),
        current_products=output["products"],
        generated_at=generated_at,
        source_sitemap=args.sitemap,
    )
    retired = {
        product_id: entry
        for product_id, entry in history["products"].items()
        if entry.get("retired") is True
    }
    output["historical_products"] = history["products"]
    output["retired_products"] = retired
    output["alias_candidates"] = history["aliases"]

    args.history.write_text(json.dumps(history, indent=2, sort_keys=True))
    print(f"Wrote {len(output['products'])} products to {args.output}")
    print(f"Wrote {len(history['products'])} historical products to {args.history}")
    if retired:
        print(f"Marked {len(retired)} products as retired/missing from the current crawl")
    if history["aliases"]:
        print(f"Found {len(history['aliases'])} likely ID alias candidates")
    if args.python_output:
        _write_python_catalogue(args.python_output, history, generated_at)
        print(f"Wrote reviewable Python catalogue to {args.python_output}")
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
