"""
Client Open Food Facts: fetch per barcode, normalizzazione, cache e batch paralleli.

Solo standard library (urllib + sqlite3): nessuna dipendenza da installare, quindi
il motore resta leggero e veloce da avviare. Il client e' iniettabile (`transport`)
per poterlo testare senza rete.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import NutrientFacts, ProductRecord

DEFAULT_BASE_URL = "https://world.openfoodfacts.org"
DEFAULT_USER_AGENT = "generic_01-foodscore/0.2-beta (food scanner app; contact: owner)"

# Campi richiesti: chiedere solo il necessario riduce payload e latenza.
DEFAULT_FIELDS = ",".join(
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
        "additives",
        "ingredients",
        "nutriments",
        "nutrition_data_per",
        "nova_group",
        "nutriscore_grade",
        "product_quantity_unit",
        "image_url",
        "image_front_url",
        "image_nutrition_url",
        "image_ingredients_url",
    )
)

# Solo parole intere: con un substring "cola" matcherebbe "cocoa" (e Nutella
# veniva trattata come bevanda).
_BEVERAGE_WORDS = re.compile(r"\b(beverages?|drinks?|sodas?|juices?|waters?|colas?)\b")
_BEVERAGE_UNITS = {"ml", "cl", "l"}
# Categoria ombrello che contiene sia cibi sia bevande: non basta per dire "bevanda".
_PARENT_BEVERAGE = re.compile(r"foods?\s+and\s+beverages")


class OFFError(Exception):
    """Errore di recupero/scoring di un prodotto."""


class RateLimiter:
    """Spaziatura adattiva: rallenta quando OFF risponde 429, poi si riprende."""

    def __init__(self, rpm: int = 60, max_interval: float = 5.0) -> None:
        self._base = 60.0 / rpm if rpm > 0 else 0.0
        self._interval = self._base
        self._max = max_interval
        self._lock = threading.Lock()
        self._next = 0.0

    @property
    def interval(self) -> float:
        with self._lock:
            return self._interval

    def acquire(self) -> None:
        with self._lock:
            interval = self._interval
            if interval <= 0:
                return
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + interval
        if wait > 0:
            time.sleep(wait)

    def penalize(self) -> None:
        """Dopo un 429: raddoppia la distanza tra le richieste."""
        with self._lock:
            self._interval = min(self._max, max(1.0, self._interval * 2))

    def relax(self) -> None:
        """Dopo un successo: torna piano piano verso il ritmo nominale."""
        with self._lock:
            if self._interval > self._base:
                self._interval = max(self._base, self._interval * 0.9)


class ProductCache:
    """Cache su SQLite: le ri-scansioni dello stesso barcode non toccano la rete."""

    def __init__(
        self, path: Path | str, ttl_seconds: float = 30 * 24 * 3600, signature: str = ""
    ) -> None:
        self.path = Path(path)
        self.ttl = ttl_seconds
        # La firma e' l'hash dei campi richiesti: se cambia la lista dei campi,
        # i payload vecchi non vanno riusati (mancano pezzi, es. `ingredients`).
        self.signature = signature
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS products_v2 ("
                "barcode TEXT NOT NULL, signature TEXT NOT NULL, fetched_at REAL NOT NULL, "
                "payload TEXT NOT NULL, PRIMARY KEY (barcode, signature))"
            )

    def get(self, barcode: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_at, payload FROM products_v2 WHERE barcode = ? AND signature = ?",
                (barcode, self.signature),
            ).fetchone()
        if row is None:
            return None
        fetched_at, payload = row
        if self.ttl and time.time() - fetched_at > self.ttl:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

    def put(self, barcode: str, payload: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO products_v2 (barcode, signature, fetched_at, payload) "
                "VALUES (?, ?, ?, ?)",
                (barcode, self.signature, time.time(), json.dumps(payload, ensure_ascii=False)),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class OFFClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        fields: str = DEFAULT_FIELDS,
        cache_path: Path | str | None = Path("cache/off.sqlite"),
        cache_ttl_days: float = 30,
        rpm: int = 60,
        timeout: float = 15.0,
        retries: int = 5,
        transport: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.fields = fields
        self.timeout = timeout
        self.retries = retries
        self._transport = transport or self._http_get
        self._limiter = RateLimiter(rpm)
        signature = hashlib.sha1(fields.encode("utf-8")).hexdigest()[:12]
        self._cache = (
            ProductCache(
                cache_path, ttl_seconds=cache_ttl_days * 24 * 3600, signature=signature
            )
            if cache_path
            else None
        )

    # ------------------------------------------------------------------ #
    # Rete
    # ------------------------------------------------------------------ #
    def product_url(self, barcode: str) -> str:
        query = urllib.parse.urlencode({"fields": self.fields})
        return f"{self.base_url}/api/v2/product/{urllib.parse.quote(str(barcode))}.json?{query}"

    def _http_get(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            return json.load(response)

    def _fetch_json(self, barcode: str, use_cache: bool = True) -> dict[str, Any]:
        if use_cache and self._cache is not None:
            cached = self._cache.get(barcode)
            if cached is not None:
                return cached

        url = self.product_url(barcode)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            self._limiter.acquire()
            try:
                payload = self._transport(url)
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    raise OFFError(f"prodotto {barcode} non trovato") from error
                last_error = error
                if error.code == 429:
                    self._limiter.penalize()
                    time.sleep(_retry_delay(attempt, error.headers, floor=3.0))
                    continue
                if error.code in (500, 502, 503, 504):
                    time.sleep(_retry_delay(attempt, error.headers))
                    continue
                raise OFFError(f"HTTP {error.code} per {barcode}") from error
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                time.sleep(_retry_delay(attempt, None))
                continue
            if use_cache and self._cache is not None:
                self._cache.put(barcode, payload)
            self._limiter.relax()
            return payload

        raise OFFError(f"impossibile recuperare {barcode}: {last_error}")

    # ------------------------------------------------------------------ #
    # API pubblica
    # ------------------------------------------------------------------ #
    def fetch_raw(self, barcode: str, use_cache: bool = True) -> dict[str, Any]:
        payload = self._fetch_json(str(barcode).strip(), use_cache=use_cache)
        if int(payload.get("status", 0)) == 0 or not payload.get("product"):
            raise OFFError(f"prodotto {barcode} non presente in Open Food Facts")
        return payload["product"]

    def fetch(self, barcode: str, use_cache: bool = True) -> ProductRecord:
        return normalize_product(self.fetch_raw(barcode, use_cache=use_cache))

    def fetch_many(
        self,
        barcodes: Iterable[str],
        workers: int = 4,
        use_cache: bool = True,
        retry_failed: bool = True,
    ) -> dict[str, ProductRecord | Exception]:
        codes = [str(code).strip() for code in barcodes if str(code).strip()]

        def load(code: str) -> tuple[str, ProductRecord | Exception]:
            try:
                return code, self.fetch(code, use_cache=use_cache)
            except Exception as error:  # noqa: BLE001 - il batch non deve abortire
                return code, error

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            results = dict(pool.map(load, codes))

        # Secondo giro sequenziale per i falliti: con il rate limiter rallentato
        # i 429 transitori si recuperano invece di perdere il prodotto.
        if retry_failed:
            for code in [c for c, value in results.items() if isinstance(value, Exception)]:
                time.sleep(2.0)
                try:
                    results[code] = self.fetch(code, use_cache=use_cache)
                except Exception as error:  # noqa: BLE001
                    results[code] = error
        return results

    def close(self) -> None:
        if self._cache is not None:
            self._cache.close()


# --------------------------------------------------------------------------- #
# Normalizzazione
# --------------------------------------------------------------------------- #
def _retry_delay(attempt: int, headers: Any, floor: float = 1.0) -> float:
    """Backoff esponenziale, rispettando Retry-After quando OFF lo manda."""
    if headers is not None:
        raw = headers.get("Retry-After")
        if raw:
            try:
                return max(0.0, min(60.0, float(raw)))
            except (TypeError, ValueError):
                pass
    return min(60.0, max(floor, 1.0 * 2**attempt))


def _number(block: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = block.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            number = float(value)
            return number if math.isfinite(number) else None
        if isinstance(value, str):
            try:
                number = float(value.replace(",", "."))
            except ValueError:
                continue
            return number if math.isfinite(number) else None
    return None


def _first_text(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_beverage_tag(tag: str) -> bool:
    plain = tag.split(":", 1)[-1].lower().replace("-", " ").replace("_", " ")
    if _PARENT_BEVERAGE.search(plain):
        return False
    return bool(_BEVERAGE_WORDS.search(plain))


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def normalize_product(raw: dict[str, Any]) -> ProductRecord:
    """Trasforma il JSON di Open Food Facts in un ProductRecord tipizzato."""
    nutriments = raw.get("nutriments") or {}
    data_per = _first_text(raw, "nutrition_data_per") or None

    kcal = _number(nutriments, "energy-kcal_100g", "energy-kcal")
    if kcal is None:
        kj = _number(nutriments, "energy-kj_100g", "energy_100g", "energy-kj", "energy")
        kcal = kj / 4.184 if kj is not None else None

    salt = _number(nutriments, "salt_100g", "salt")
    sodium = _number(nutriments, "sodium_100g", "sodium")
    if salt is None and sodium is not None:
        salt = sodium * 2.5

    facts = NutrientFacts(
        kcal=kcal,
        sugars=_number(nutriments, "sugars_100g", "sugars"),
        added_sugars=_number(nutriments, "added-sugars_100g", "added-sugars"),
        salt=salt,
        sodium=sodium,
        saturated_fat=_number(nutriments, "saturated-fat_100g", "saturated-fat"),
        fat=_number(nutriments, "fat_100g", "fat"),
        fiber=_number(nutriments, "fiber_100g", "fiber"),
        protein=_number(nutriments, "proteins_100g", "proteins"),
        fruits_veg_nuts=_number(
            nutriments,
            "fruits-vegetables-legumes-estimate-from-ingredients_100g",
            "fruits-vegetables-nuts-estimate-from-ingredients_100g",
        ),
    )

    additives = list(_as_tuple(raw.get("additives_tags")))
    if not additives:
        additives = list(_as_tuple(raw.get("additives_original_tags")))

    # Percentuali dichiarate negli ingredienti (OFF le stima dall'ordine):
    # servono per pesare il rischio in base alla dose, quando disponibili.
    doses: dict[str, float] = {}
    for ingredient in raw.get("ingredients") or []:
        if not isinstance(ingredient, dict):
            continue
        key = str(ingredient.get("id") or "")
        if not key.startswith("en:e"):
            continue
        percent = ingredient.get("percent_estimate")
        if percent is None:
            percent = ingredient.get("percent_min")
        try:
            value = float(percent)
        except (TypeError, ValueError):
            continue
        if value > 0:
            doses[key] = max(doses.get(key, 0.0), value)

    categories = _as_tuple(raw.get("categories_tags"))
    labels = _as_tuple(raw.get("labels_tags"))
    unit = _first_text(raw, "product_quantity_unit").lower()
    is_beverage = unit in _BEVERAGE_UNITS or any(_is_beverage_tag(c) for c in categories)

    nova_raw = raw.get("nova_group") or (raw.get("nutriments") or {}).get("nova-group_100g")
    try:
        nova = int(float(nova_raw)) if nova_raw is not None else None
    except (TypeError, ValueError):
        nova = None

    return ProductRecord(
        barcode=str(raw.get("code", "")),
        name=_first_text(raw, "product_name_it", "product_name"),
        brand=_first_text(raw, "brands"),
        quantity=_first_text(raw, "quantity") or None,
        categories=categories,
        labels=labels,
        ingredients_text=_first_text(raw, "ingredients_text_it", "ingredients_text") or None,
        additives=tuple(dict.fromkeys(additives)),
        nutriments=facts,
        nova_group=nova,
        nutriscore_grade=(_first_text(raw, "nutriscore_grade") or None),
        is_beverage=is_beverage,
        nutrition_data_per=data_per,
        additive_doses=doses,
        image_url=_first_text(raw, "image_front_url", "image_url") or None,
        image_nutrition_url=_first_text(raw, "image_nutrition_url") or None,
        image_ingredients_url=_first_text(raw, "image_ingredients_url") or None,
    )
