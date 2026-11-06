"""
Adattatori multi-sorgente.

Ogni fonte (Open Food Facts, Yuka, una tabella EFSA, un export fatto a mano)
parla un dialetto diverso: chiavi in italiano o inglese, punteggi su scale
diverse, additivi come lista o stringa. Qui tutto viene normalizzato in
`SourceProduct`, cosi' il confronto tra fonti lavora su un solo formato.

Formati accettati:
  * JSON: lista di prodotti, {"products": [...]}, {"product": {...}},
    dizionario barcode -> prodotto;
  * JSONL: una riga per prodotto;
  * CSV/TSV: con intestazione, delimitatore riconosciuto automaticamente
    (virgola, punto e virgola, tab).

Uso:
    from engine.sources import load_source
    prodotti = load_source("yuka", "export.json", score_max=100)
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .additives import canonical

# Alias accettati per i campi. Il primo che esiste vince.
BARCODE_FIELDS = ("barcode", "code", "ean", "gtin", "gtin13", "gtin14", "codice", "id")
NAME_FIELDS = ("product_name", "name", "title", "nome", "prodotto")
BRAND_FIELDS = ("brands", "brand", "brand_name", "marca", "brands_tags")
SCORE_FIELDS = ("score", "rating", "note", "punteggio", "risk_score")
GRADE_FIELDS = ("grade", "nutriscore_grade", "giudizio", "rating_grade")
ADDITIVE_FIELDS = ("additives", "additives_tags", "additivi", "additives_e")
INGREDIENT_FIELDS = ("ingredients_text", "ingredients", "ingredienti")


@dataclass(frozen=True)
class SourceProduct:
    """Un prodotto visto da una fonte, ridotto ai campi confrontabili."""

    source: str
    barcode: str = ""
    name: str = ""
    brand: str = ""
    score: float | None = None
    grade: str | None = None
    additives: tuple[str, ...] = ()
    ingredients: str = ""
    quantity: str = ""
    categories: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    nova_group: int | None = None
    nutrients: dict[str, float | None] = field(default_factory=dict)
    price: float | None = None
    price_per_100g: float | None = None
    price_observations: int = 0
    stores: tuple[str, ...] = ()
    currency: str = ""
    fetched_at: str = ""

    @property
    def label(self) -> str:
        return self.name or self.barcode or "?"

    def to_dict(self) -> dict[str, Any]:
        """Forma serializzabile, rileggibile da `load_source`."""
        return {
            "source": self.source,
            "barcode": self.barcode,
            "name": self.name,
            "brand": self.brand,
            "quantity": self.quantity,
            "categories": list(self.categories),
            "labels": list(self.labels),
            "nova_group": self.nova_group,
            "grade": self.grade,
            "score": self.score,
            "additives": list(self.additives),
            "ingredients": self.ingredients,
            "nutrients": {key: value for key, value in self.nutrients.items() if value is not None},
            "price": self.price,
            "price_per_100g": self.price_per_100g,
            "price_observations": self.price_observations,
            "stores": list(self.stores),
            "currency": self.currency,
            "fetched_at": self.fetched_at,
        }


# --------------------------------------------------------------------------- #
# Normalizzazione testo / codici
# --------------------------------------------------------------------------- #
def norm_text(value: Any) -> str:
    """'Crème Brûlée & Co.' -> 'creme brulee and co'."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_key(name: str, brand: str = "") -> str:
    """Chiave di confronto nome+marca, indipendente da accenti e punteggiatura."""
    return " ".join(part for part in (norm_text(brand), norm_text(name)) if part)


def barcode_key(barcode: str) -> str:
    """Chiave barcode robusta: solo cifre, zeri iniziali ignorati.

    Gli EAN-8 (8 cifre) restano interi anche se iniziano per zero; sotto le 8
    cifre il codice non e' un GTIN e non viene usato per il matching.
    """
    digits = re.sub(r"\D", "", str(barcode or ""))
    if len(digits) < 8:
        return ""
    trimmed = digits.lstrip("0")
    return trimmed if len(trimmed) >= 8 else digits


# --------------------------------------------------------------------------- #
# Caricamento
# --------------------------------------------------------------------------- #
def load_source(name: str, path: Path | str, score_max: float = 100.0) -> list[SourceProduct]:
    """Legge un export e restituisce i prodotti normalizzati della fonte."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    if not text.strip():
        return []
    if text.lstrip()[:1] in "[{":
        records = _records_from_json(text)
    else:
        records = _records_from_csv(text)

    products: list[SourceProduct] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        product = _product_from_record(name, record, score_max)
        if product is not None:
            products.append(product)
    return products


def _records_from_json(text: str) -> list[Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # JSONL: un oggetto per riga.
        records: list[Any] = []
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line or line in "[]{}":
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
    return _records_from_payload(payload)


def _records_from_payload(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("products", "results", "data", "items", "prodotti", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        if isinstance(payload.get("product"), dict):
            # Il record esterno porta il barcode, quello interno i dettagli:
            # _product_from_record li unisce.
            return [payload]
        if payload and all(isinstance(value, dict) for value in payload.values()):
            records = []
            for key, value in payload.items():
                record = dict(value)
                record.setdefault("barcode", key)
                records.append(record)
            return records
        return [payload]
    return []


def _records_from_csv(text: str) -> list[dict[str, Any]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [dict(row) for row in csv.DictReader(io.StringIO(text), dialect=dialect)]


def _product_from_record(source: str, record: dict[str, Any], score_max: float) -> SourceProduct | None:
    record = _unwrap(record)
    barcode = re.sub(r"\D", "", _clean(_pick(record, BARCODE_FIELDS)))
    name = _clean(_pick(record, NAME_FIELDS))
    brand = _clean(_pick(record, BRAND_FIELDS))
    if not name and not barcode:
        return None
    return SourceProduct(
        source=source,
        barcode=barcode,
        name=name,
        brand=brand,
        score=_score(_pick(record, SCORE_FIELDS), score_max),
        grade=_grade(_pick(record, GRADE_FIELDS)),
        additives=_additives(_pick(record, ADDITIVE_FIELDS)),
        ingredients=_clean(_pick(record, INGREDIENT_FIELDS)),
        quantity=_clean(_pick(record, ("quantity", "quantita", "peso"))),
        categories=_tuple(_pick(record, ("categories_tags", "categories", "categorie"))),
        labels=_tuple(_pick(record, ("labels_tags", "labels", "etichette"))),
        nova_group=_int(_pick(record, ("nova_group", "nova"))),
        nutrients=_nutrients(record),
        price=_number(_pick(record, ("price", "prezzo"))),
        price_per_100g=_number(_pick(record, ("price_per_100g", "prezzo_per_100g"))),
        price_observations=_int(_pick(record, ("price_observations", "osservazioni_prezzo"))) or 0,
        stores=_tuple(_pick(record, ("stores", "negozi"))),
        currency=_clean(_pick(record, ("currency", "valuta"))),
        fetched_at=_clean(_pick(record, ("fetched_at", "scaricato_il"))),
    )


# --------------------------------------------------------------------------- #
# Nutrienti e campi estesi
# --------------------------------------------------------------------------- #
NUTRIENT_ALIASES: dict[str, tuple[str, ...]] = {
    "kcal": ("kcal", "calories", "energy_kcal", "energy-kcal_100g", "energy-kcal", "energia_kcal"),
    "sugars": ("sugars_100g", "sugars", "zuccheri", "zuccheri_100g"),
    "added_sugars": ("added-sugars_100g", "added-sugars", "added_sugars", "zuccheri_aggiunti"),
    "salt": ("salt_100g", "salt", "sale", "sale_100g"),
    "sodium": ("sodium_100g", "sodium", "sodio", "sodio_100g"),
    "saturated_fat": ("saturated-fat_100g", "saturated-fat", "saturated_fat", "grassi_saturi"),
    "fat": ("fat_100g", "fat", "grassi"),
    "fiber": ("fiber_100g", "fiber", "fibre", "fibre_100g", "fibra"),
    "protein": ("proteins_100g", "proteins", "protein", "proteine"),
    "fruits_veg_nuts": (
        "fruits-vegetables-nuts-estimate-from-ingredients_100g",
        "fruits-vegetables-legumes-estimate-from-ingredients_100g",
        "fruits_veg_nuts",
        "frutta_verdura",
    ),
}


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
    return number if math.isfinite(number) else None


def _num(block: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        number = _number(block.get(key))
        if number is not None:
            return number
    return None


def _nutrients_from_block(block: dict[str, Any]) -> dict[str, float]:
    """Accetta sia il blocco OFF (`sugars_100g`) sia chiavi canoniche (`sugars`)."""
    kcal = _num(block, "energy-kcal_100g", "energy-kcal", "kcal", "calories", "energia_kcal")
    if kcal is None:
        kj = _num(block, "energy-kj_100g", "energy_100g", "energy-kj", "energy", "energia_kj")
        kcal = kj / 4.184 if kj is not None else None

    sodium = _num(block, "sodium_100g", "sodium", "sodio")
    salt = _num(block, "salt_100g", "salt", "sale")
    if salt is None and sodium is not None:
        salt = sodium * 2.5

    values = {
        "kcal": kcal,
        "sugars": _num(block, "sugars_100g", "sugars", "zuccheri"),
        "added_sugars": _num(block, "added-sugars_100g", "added-sugars", "added_sugars", "zuccheri_aggiunti"),
        "salt": salt,
        "sodium": sodium,
        "saturated_fat": _num(block, "saturated-fat_100g", "saturated-fat", "saturated_fat", "grassi_saturi"),
        "fat": _num(block, "fat_100g", "fat", "grassi"),
        "fiber": _num(block, "fiber_100g", "fiber", "fibre_100g", "fibre", "fibra"),
        "protein": _num(block, "proteins_100g", "proteins", "protein", "proteine"),
        "fruits_veg_nuts": _num(
            block,
            "fruits-vegetables-nuts-estimate-from-ingredients_100g",
            "fruits-vegetables-legumes-estimate-from-ingredients_100g",
            "fruits_veg_nuts",
            "frutta_verdura",
        ),
    }
    return {key: round(float(value), 3) for key, value in values.items() if value is not None}


def _nutrients(record: dict[str, Any]) -> dict[str, float]:
    for key in ("nutriments", "nutrients"):
        block = record.get(key)
        if isinstance(block, dict) and block:
            return _nutrients_from_block(block)
    flat: dict[str, Any] = {}
    for key, aliases in NUTRIENT_ALIASES.items():
        value = _pick(record, aliases)
        if value is not None:
            flat[key] = value
    return _nutrients_from_block(flat) if flat else {}


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(part.strip() for part in re.split(r"[,;|]", value) if part.strip())
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _unwrap(record: dict[str, Any]) -> dict[str, Any]:
    """Appiattisce {"code": ..., "product": {...}} mantenendo i campi esterni."""
    for key in ("product", "prodotto"):
        nested = record.get(key)
        if isinstance(nested, dict):
            merged = {k: v for k, v in record.items() if k != key}
            for nested_key, value in nested.items():
                merged.setdefault(nested_key, value)
            return merged
    return record


def _pick(record: dict[str, Any], fields: tuple[str, ...]) -> Any:
    lowered = {str(key).lower(): value for key, value in record.items()}
    for field in fields:
        value = lowered.get(field)
        if value not in (None, "", [], {}):
            return value
    return None


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value if item not in (None, ""))
    if isinstance(value, dict):
        return ", ".join(str(item) for item in value.values() if isinstance(item, str))
    return str(value).strip()


def _score(raw: Any, score_max: float) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        text = raw.strip().replace(",", ".")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    elif isinstance(raw, (int, float)):
        value = float(raw)
    else:
        return None
    if score_max and score_max > 0 and score_max != 100:
        value = value / float(score_max) * 100.0
    return max(0.0, min(100.0, round(value, 1)))


def _grade(raw: Any) -> str | None:
    text = _clean(raw).strip().lower()
    if not text:
        return None
    if len(text) == 1 and text in "abcde":
        return text.upper()
    return text


def _additives(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        items = re.split(r"[,;|]", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = []
        for item in raw:
            if isinstance(item, dict):
                item = item.get("id") or item.get("e_number") or item.get("name") or ""
            items.append(str(item))
    else:
        return ()

    found: list[str] = []
    for item in items:
        token = canonical(item)
        if token and token not in found:
            found.append(token)
    return tuple(found)
