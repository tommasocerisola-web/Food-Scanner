"""
Network adapters for the three sources, all reachable without a mandatory key.

  * Open Food Facts — public v2 API, products and additives;
  * USDA FoodData Central — DEMO_KEY (30 requests/hour) or FDC_API_KEY;
  * Open Prices — public v1 API, price/store/date per barcode.

Every function returns `SourceProduct`, the same shape as the offline exports:
the merger does not know (and must not know) where the data comes from.
"""
from __future__ import annotations

import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping

from .additives import canonical
from .merge import gtin14
from .models import ProductRecord
from .off_client import RateLimiter, normalize_product
from .sources import SourceProduct

OFF_BASE = "https://world.openfoodfacts.org"
FDC_BASE = "https://api.nal.usda.gov/fdc/v1"
PRICES_BASE = "https://prices.openfoodfacts.org"
USER_AGENT = "generic_01-foodscore/0.3-beta (food scanner app; contact: owner)"
TIMEOUT = 25.0

OFF_FIELDS = ",".join(
    (
        "code",
        "product_name",
        "product_name_it",
        "brands",
        "quantity",
        "categories_tags",
        "labels_tags",
        "ingredients_text",
        "ingredients_text_it",
        "additives_tags",
        "additives_original_tags",
        "ingredients",
        "nutriments",
        "nutrition_data_per",
        "nova_group",
        "nutriscore_grade",
        "product_quantity_unit",
    )
)

_OFF_LIMITER = RateLimiter(rpm=60)
_FDC_LIMITER = RateLimiter(rpm=30)
_PRICES_LIMITER = RateLimiter(rpm=30)

SOURCES = ("off", "fdc", "openprices")


class FetchError(Exception):
    """Network error, unexpected response or source limit."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _retry_delay(headers: Any, attempt: int) -> float:
    if headers is not None:
        raw = headers.get("Retry-After")
        if raw:
            try:
                return max(0.0, min(60.0, float(raw)))
            except (TypeError, ValueError):
                pass
    return min(30.0, max(1.0, 1.5 * 2**attempt))


def _get_json(url: str, limiter: RateLimiter | None = None, attempts: int = 3) -> Any:
    if limiter is not None:
        limiter.acquire()
    last: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                if limiter is not None:
                    limiter.relax()
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            last = error
            if error.code == 429 or 500 <= error.code < 600:
                if limiter is not None:
                    limiter.penalize()
                if attempt + 1 < attempts:
                    time.sleep(_retry_delay(error.headers, attempt))
                continue
            raise FetchError(f"HTTP {error.code} on {url.split('?', 1)[0]}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last = error
            time.sleep(min(8.0, 1.5 * (attempt + 1)))
    raise FetchError(f"request failed on {url.split('?', 1)[0]} ({last})")


# --------------------------------------------------------------------------- #
# Open Food Facts
# --------------------------------------------------------------------------- #
def fetch_off(barcode: str) -> SourceProduct | None:
    code = str(barcode or "").strip()
    if not code:
        return None
    url = f"{OFF_BASE}/api/v2/product/{urllib.parse.quote(code)}.json?fields={OFF_FIELDS}"
    try:
        payload = _get_json(url, _OFF_LIMITER)
    except FetchError as error:
        if "HTTP 404" in str(error):
            return None
        raise
    if not isinstance(payload, dict) or payload.get("status") != 1:
        return None
    raw = payload.get("product")
    if not isinstance(raw, dict):
        return None
    record = normalize_product({**raw, "code": raw.get("code") or payload.get("code") or code})
    return _from_off(record, code)


def _from_off(record: ProductRecord, barcode: str) -> SourceProduct:
    nutrients = {key: value for key, value in record.nutriments.to_dict().items() if value is not None}
    return SourceProduct(
        source="off",
        barcode=str(record.barcode or barcode),
        name=record.name,
        brand=record.brand,
        quantity=record.quantity or "",
        categories=record.categories,
        labels=record.labels,
        ingredients=record.ingredients_text or "",
        grade=record.nutriscore_grade.upper() if record.nutriscore_grade else None,
        nova_group=record.nova_group,
        additives=tuple(canonical(tag) for tag in record.additives),
        nutrients=nutrients,
        fetched_at=_now(),
    )


# --------------------------------------------------------------------------- #
# USDA FoodData Central
# --------------------------------------------------------------------------- #
def fdc_api_key() -> str:
    return os.environ.get("FDC_API_KEY", "").strip() or "DEMO_KEY"


def fetch_fdc(gtin: str = "", name: str = "", brand: str = "") -> SourceProduct | None:
    """Look the product up by name and verify the GTIN among the candidates.

    The FDC API does not accept the barcode as a query (it returns 0 results),
    but every branded food exposes `gtinUpc`: search by name/brand and keep only
    the candidate with the same GTIN. That way different products are not merged.
    """
    target = gtin14(gtin)
    query = (name or brand or "").strip()
    if not target or not query:
        return None

    api_key = fdc_api_key()
    # The OFF name is sometimes a commercial title ("12 Pack Hazelnut Spread..."):
    # if the name search misses the GTIN, retry with the brand.
    queries = [query]
    if brand and brand.strip().lower() != query.lower():
        queries.append(brand.strip())

    food = None
    for text in queries:
        payload = _fdc_search(text, api_key)
        foods = payload.get("foods") if isinstance(payload, dict) else None
        matches = [
            candidate
            for candidate in (foods or [])
            if isinstance(candidate, dict)
            and gtin14(str(candidate.get("gtinUpc") or "")) == target
        ]
        if matches:
            food = max(matches, key=lambda item: len(item.get("foodNutrients") or []))
            break

    if food is None:
        return None
    return _from_fdc(food, gtin)


def _fdc_search(query: str, api_key: str) -> Any:
    params = {
        "api_key": api_key,
        "query": query,
        "dataType": "Branded",
        "pageSize": "50",
    }
    url = f"{FDC_BASE}/foods/search?{urllib.parse.urlencode(params)}"
    # With DEMO_KEY a 429 means the hourly quota is gone: retrying is useless.
    attempts = 1 if api_key == "DEMO_KEY" else 3
    try:
        return _get_json(url, _FDC_LIMITER, attempts=attempts)
    except FetchError as error:
        if api_key == "DEMO_KEY":
            raise FetchError(
                "USDA FDC: DEMO_KEY limit (30 requests/hour). "
                "Set FDC_API_KEY (free at fdc.nal.usda.gov) to continue."
            ) from error
        raise


def _from_fdc(food: Mapping[str, Any], gtin: str) -> SourceProduct:
    nutrients = _fdc_nutrients(food)
    sodium = nutrients.get("sodium")
    if "salt" not in nutrients and sodium is not None:
        nutrients["salt"] = round(float(sodium) * 2.5, 3)
    category = str(food.get("foodCategory") or "").strip()
    return SourceProduct(
        source="fdc",
        barcode=str(food.get("gtinUpc") or gtin),
        name=str(food.get("description") or "").strip(),
        brand=str(food.get("brandName") or food.get("brandOwner") or "").strip(),
        quantity=str(food.get("packageWeight") or "").strip(),
        categories=(category,) if category else (),
        ingredients=str(food.get("ingredients") or ""),
        nutrients={key: round(float(value), 3) for key, value in nutrients.items()},
        fetched_at=_now(),
    )


def _fdc_nutrients(food: Mapping[str, Any]) -> dict[str, float]:
    found: dict[str, float] = {}
    energy_kj: float | None = None
    for item in food.get("foodNutrients") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("nutrientName") or "").strip().lower()
        unit = str(item.get("unitName") or "").strip().lower()
        value = _number(item.get("value"))
        if value is None:
            continue
        if name.startswith("energy"):
            if unit == "kcal":
                found["kcal"] = value
            elif unit == "kj" and energy_kj is None:
                energy_kj = value
        elif "added sugar" in name:
            found.setdefault("added_sugars", value)
        elif "sugars" in name:
            found.setdefault("sugars", value)
        elif "fatty acids, total saturated" in name:
            found.setdefault("saturated_fat", value)
        elif name.startswith("total lipid"):
            found.setdefault("fat", value)
        elif "fiber" in name:
            found.setdefault("fiber", value)
        elif name == "protein":
            found.setdefault("protein", value)
        elif name.startswith("sodium"):
            found.setdefault("sodium", value / 1000.0)  # mg -> g
    if "kcal" not in found and energy_kj is not None:
        found["kcal"] = energy_kj / 4.184
    return found


# --------------------------------------------------------------------------- #
# Open Prices
# --------------------------------------------------------------------------- #
def fetch_openprices(barcode: str, size: int = 50) -> SourceProduct | None:
    code = str(barcode or "").strip()
    if not code:
        return None
    params = {"product_code": code, "size": str(size), "order_by": "-date"}
    url = f"{PRICES_BASE}/api/v1/prices?{urllib.parse.urlencode(params)}"
    try:
        payload = _get_json(url, _PRICES_LIMITER)
    except FetchError as error:
        if "HTTP 404" in str(error):
            return None
        raise

    items = payload.get("items") if isinstance(payload, dict) else None
    if not items:
        return None

    prices: list[float] = []
    per_100: list[float] = []
    stores: set[str] = set()
    currency = ""
    product: Mapping[str, Any] = {}

    for item in items:
        if not isinstance(item, dict):
            continue
        price = _number(item.get("price"))
        if price is None:
            continue
        product = item.get("product") or product
        prices.append(price)
        currency = currency or str(item.get("currency") or "")
        quantity = _number((item.get("product") or {}).get("product_quantity"))
        unit = str((item.get("product") or {}).get("product_quantity_unit") or "").lower()
        if quantity and quantity > 0 and unit in ("g", "ml", "kg", "l"):
            grams = quantity * 1000 if unit in ("kg", "l") else quantity
            per_100.append(price / grams * 100.0)
        location = item.get("location") or {}
        store = str(location.get("osm_name") or location.get("osm_brand") or "").strip()
        if store:
            stores.add(store)

    if not prices:
        return None

    return SourceProduct(
        source="openprices",
        barcode=str(product.get("code") or code),
        name=str(product.get("product_name") or ""),
        brand=str(product.get("brands") or ""),
        quantity=str(product.get("quantity") or ""),
        price=round(statistics.median(prices), 2),
        price_per_100g=round(statistics.median(per_100), 3) if per_100 else None,
        price_observations=len(prices),
        stores=tuple(sorted(stores))[:10],
        currency=currency,
        fetched_at=_now(),
    )


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #
def fetch_source(source: str, barcode: str, name: str = "", brand: str = "") -> SourceProduct | None:
    if source == "off":
        return fetch_off(barcode)
    if source == "fdc":
        return fetch_fdc(gtin=barcode, name=name, brand=brand)
    if source == "openprices":
        return fetch_openprices(barcode)
    raise ValueError(f"unknown source: {source}")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().replace(",", "."))
        except ValueError:
            return None
    else:
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
