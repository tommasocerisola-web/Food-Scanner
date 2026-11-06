"""
Unione multi-fonte: mediana pesata, provenienza, conflitti e confidenza.

Le tre fonti previste (tutte senza chiave API obbligatoria) hanno punti di forza
diversi, quindi il merger non sceglie una fonte "vincente": per **ogni campo**
prende la mediana pesata delle fonti che lo riportano, registra quale fonte ha
fornito il valore e segnala i conflitti oltre tolleranza. Un campo assente in una
fonte non la penalizza: il peso viene semplicemente rinormalizzato.

Pesi:
  * Open Food Facts 1.0 — additivi, Nutri-Score, NOVA, etichette;
  * USDA FDC       0.9 — nutrienti (peso 1.1 sul campo), ingredienti, GTIN;
  * Open Prices    0.7 — prezzo, confezione, negozio.

La confidenza finale combina copertura dei campi, accordo tra fonti e numero di
fonti presenti: un prodotto visto da una sola fonte non puo' avere confidenza
alta, anche se tutti i campi sono pieni.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable, Mapping, Sequence

from .additives import AdditiveTable
from .sources import SourceProduct, norm_text

NUTRIENTS = (
    "kcal",
    "sugars",
    "added_sugars",
    "salt",
    "sodium",
    "saturated_fat",
    "fat",
    "fiber",
    "protein",
    "fruits_veg_nuts",
)

SOURCE_PROFILES: dict[str, dict[str, Any]] = {
    "off": {
        "label": "Open Food Facts",
        "weight": 1.0,
        "fields": {
            "nutrients": 1.0,
            "additives": 1.0,
            "grade": 1.0,
            "nova": 1.0,
            "labels": 1.0,
            "categories": 0.8,
            "identity": 1.0,
            "risk": 1.0,
        },
    },
    "fdc": {
        "label": "USDA FoodData Central",
        "weight": 0.9,
        "fields": {
            "nutrients": 1.1,
            "additives": 0.2,
            "ingredients": 0.9,
            "grade": 0.0,
            "nova": 0.0,
            "labels": 0.3,
            "categories": 0.4,
            "identity": 0.9,
            "risk": 0.8,
        },
    },
    "openprices": {
        "label": "Open Prices",
        "weight": 0.7,
        "fields": {
            "nutrients": 0.0,
            "additives": 0.0,
            "grade": 0.0,
            "nova": 0.0,
            "labels": 0.0,
            "categories": 0.0,
            "identity": 0.6,
            "price": 1.0,
            "risk": 0.3,
        },
    },
}
DEFAULT_PROFILE: dict[str, Any] = {"label": "fonte", "weight": 0.5, "fields": {}}

# (tolleranza relativa, soglia minima assoluta) oltre cui due fonti si contraddicono.
NUTRIENT_TOLERANCE: dict[str, tuple[float, float]] = {
    "kcal": (0.15, 15.0),
    "sugars": (0.20, 0.7),
    "added_sugars": (0.25, 0.7),
    "salt": (0.25, 0.15),
    "sodium": (0.25, 0.06),
    "saturated_fat": (0.25, 0.3),
    "fat": (0.20, 0.5),
    "fiber": (0.30, 0.4),
    "protein": (0.20, 0.5),
    "fruits_veg_nuts": (0.35, 5.0),
}

SCORE_SPREAD = 15.0          # punti: oltre, i punteggi delle fonti divergono
ADDITIVE_WORST_WEIGHT = 0.65  # stesso peso del motore: il peggiore domina
ADDITIVE_MEAN_WEIGHT = 0.35
ADDITIVE_EXTRA_PER_ITEM = 2.5
ADDITIVE_EXTRA_CAP = 10.0
RISK_SPREAD = 15.0            # punti: oltre, la tabella di consenso va rivista


# --------------------------------------------------------------------------- #
# Codici e statistiche
# --------------------------------------------------------------------------- #
def gtin14(value: str) -> str:
    """GTIN-8/UPC-12/EAN-13/GTIN-14 -> GTIN-14 con zeri iniziali.

    Serve perche' lo stesso prodotto puo' arrivare come UPC-12 (US) o EAN-13
    (EU): normalizzare a 14 cifre evita falsi negativi.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) not in (8, 12, 13, 14):
        return ""
    return digits.zfill(14)


def weighted_median(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Mediana pesata (basso): il primo valore che raggiunge meta' del peso."""
    usable = sorted(
        (float(value), float(weight))
        for value, weight in pairs
        if value is not None and weight > 0 and math.isfinite(float(value))
    )
    if not usable:
        return None
    total = sum(weight for _, weight in usable)
    cumulative = 0.0
    for value, weight in usable:
        cumulative += weight
        if cumulative >= total / 2.0:
            return round(value, 3)
    return round(usable[-1][0], 3)


def _profile(source: str, profiles: Mapping[str, Mapping[str, Any]] | None = None) -> Mapping[str, Any]:
    table = profiles if profiles is not None else SOURCE_PROFILES
    return table.get(source) or DEFAULT_PROFILE


def _field_weight(
    source: str,
    field_name: str,
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> float:
    profile = _profile(source, profiles)
    weight = float(profile.get("weight", DEFAULT_PROFILE["weight"]))
    specific = (profile.get("fields") or {}).get(field_name)
    if specific is None:
        return weight
    return weight * float(specific)


# --------------------------------------------------------------------------- #
# Nutrienti
# --------------------------------------------------------------------------- #
def merge_nutrients(
    entries: Mapping[str, SourceProduct],
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    values: dict[str, float] = {}
    provenance: dict[str, str] = {}
    contributors: dict[str, int] = {}
    conflicts: list[dict[str, Any]] = []
    agreements: list[float] = []

    for nutrient in NUTRIENTS:
        samples: list[tuple[str, float, float]] = []
        for source, product in entries.items():
            raw = product.nutrients.get(nutrient)
            if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                continue
            number = float(raw)
            if not math.isfinite(number):
                continue
            weight = _field_weight(source, "nutrients", profiles)
            if weight <= 0:
                continue
            samples.append((source, number, weight))
        if not samples:
            continue

        median = weighted_median([(value, weight) for _, value, weight in samples])
        if median is None:
            continue
        values[nutrient] = median
        best = min(samples, key=lambda item: (abs(item[1] - median), -item[2], item[0]))
        provenance[nutrient] = best[0]
        contributors[nutrient] = len({source for source, _, _ in samples})

        spread = max(value for _, value, _ in samples) - min(value for _, value, _ in samples)
        tolerance, floor = NUTRIENT_TOLERANCE.get(nutrient, (0.25, 0.5))
        reference = max(abs(median), floor)
        relative = spread / reference
        agreement = 1.0 if len(samples) == 1 else max(0.0, 1.0 - min(1.0, relative / tolerance))
        agreements.append(agreement)

        if len({source for source, _, _ in samples}) > 1 and spread >= floor and relative > tolerance:
            conflicts.append(
                {
                    "campo": nutrient,
                    "valori": {source: round(value, 2) for source, value, _ in samples},
                    "mediana": median,
                    "spread": round(spread, 2),
                    "tolleranza": tolerance,
                }
            )

    return {
        "values": values,
        "provenance": provenance,
        "contributors": contributors,
        "conflicts": conflicts,
        "agreement": round(sum(agreements) / len(agreements), 3) if agreements else 0.0,
    }


# --------------------------------------------------------------------------- #
# Additivi
# --------------------------------------------------------------------------- #
def merge_additives(
    entries: Mapping[str, SourceProduct],
    table: AdditiveTable | None = None,
) -> dict[str, Any]:
    presence: dict[str, set[str]] = {}
    for source, product in entries.items():
        for tag in product.additives:
            presence.setdefault(tag, set()).add(source)

    risks: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for tag, sources in sorted(presence.items()):
        row = table.get(tag) if table is not None else None
        if row is None:
            unknown.append({"tag": tag, "fonti": sorted(sources)})
            continue
        risk, estimated = row.default_risk()
        risks.append(
            {
                "tag": tag,
                "risk": round(float(risk), 1),
                "stimato": bool(estimated),
                "fonti": sorted(sources),
            }
        )
    risks.sort(key=lambda item: (-item["risk"], item["tag"]))

    score = None
    if risks:
        worst = max(item["risk"] for item in risks)
        mean_risk = sum(item["risk"] for item in risks) / len(risks)
        extra = min(ADDITIVE_EXTRA_CAP, ADDITIVE_EXTRA_PER_ITEM * max(0, len(risks) - 2))
        penalty = ADDITIVE_WORST_WEIGHT * worst + ADDITIVE_MEAN_WEIGHT * mean_risk + extra
        score = round(max(0.0, min(100.0, 100.0 - penalty)), 1)

    return {
        "additivi": risks,
        "sconosciuti": unknown,
        "punteggio": score,
        "dichiarati": any(product.additives for product in entries.values()),
        "stimati": sum(1 for item in risks if item["stimato"]),
    }


# --------------------------------------------------------------------------- #
# Punteggi, scelte uniche, prezzo
# --------------------------------------------------------------------------- #
def merge_scores(
    entries: Mapping[str, SourceProduct],
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    samples: list[tuple[str, float, float]] = []
    for source, product in entries.items():
        if product.score is None:
            continue
        weight = _field_weight(source, "score", profiles)
        if weight <= 0:
            continue
        samples.append((source, float(product.score), weight))
    if not samples:
        return {"valore": None, "provenienza": None, "spread": None, "conflitto": False, "fonti": 0}

    median = weighted_median([(value, weight) for _, value, weight in samples])
    spread = max(value for _, value, _ in samples) - min(value for _, value, _ in samples)
    best = min(samples, key=lambda item: (abs(item[1] - median), -item[2], item[0]))
    return {
        "valore": median,
        "provenienza": best[0],
        "spread": round(spread, 2),
        "conflitto": len(samples) > 1 and spread > SCORE_SPREAD,
        "fonti": len(samples),
    }


def _merge_choice(
    entries: Mapping[str, SourceProduct],
    attribute: str,
    field_name: str,
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    votes: dict[str, list[tuple[float, str]]] = {}
    for source, product in entries.items():
        value = getattr(product, attribute, None)
        if value is None or value == "" or value == ():
            continue
        weight = _field_weight(source, field_name, profiles)
        if weight <= 0:
            continue
        votes.setdefault(str(value), []).append((weight, source))
    if not votes:
        return {"valore": None, "provenienza": None, "conflitto": False, "valori": {}}

    totals = {value: sum(weight for weight, _ in items) for value, items in votes.items()}
    winner = max(totals, key=lambda value: (totals[value], value))
    provenance = max(votes[winner], key=lambda item: item[0])[1]
    return {
        "valore": winner,
        "provenienza": provenance,
        "conflitto": len(votes) > 1,
        "valori": {value: [source for _, source in items] for value, items in votes.items()},
    }


def merge_price(
    entries: Mapping[str, SourceProduct],
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    per_100: list[tuple[str, float, float]] = []
    package: list[tuple[str, float, float]] = []
    for source, product in entries.items():
        weight = _field_weight(source, "price", profiles)
        if weight <= 0:
            continue
        if product.price_per_100g is not None:
            per_100.append((source, float(product.price_per_100g), weight))
        elif product.price is not None:
            package.append((source, float(product.price), weight))

    samples = per_100 or package
    if not samples:
        return {"valore": None, "base": "", "osservazioni": 0, "negozi": [], "fonti": []}

    median = weighted_median([(value, weight) for _, value, weight in samples])
    observations = sum(product.price_observations for product in entries.values())
    stores = sorted({store for product in entries.values() for store in product.stores})
    return {
        "valore": median,
        "base": "100g" if per_100 else "confezione",
        "osservazioni": observations,
        "negozi": stores[:10],
        "fonti": sorted({source for source, _, _ in samples}),
    }


def _union(entries: Mapping[str, SourceProduct], attribute: str) -> list[str]:
    seen: list[str] = []
    for product in entries.values():
        for value in getattr(product, attribute, ()) or ():
            if value not in seen:
                seen.append(value)
    return sorted(seen, key=norm_text)


def _identity(
    entries: Mapping[str, SourceProduct],
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    ranked = sorted(
        entries.items(),
        key=lambda item: (-_field_weight(item[0], "identity", profiles), item[0]),
    )

    def pick(attribute: str) -> tuple[str, str | None]:
        for source, product in ranked:
            value = getattr(product, attribute, "")
            if value:
                return str(value), source
        return "", None

    name, name_source = pick("name")
    brand, brand_source = pick("brand")
    quantity, quantity_source = pick("quantity")
    return {
        "nome": name,
        "nome_fonte": name_source,
        "marca": brand,
        "marca_fonte": brand_source,
        "quantita": quantity,
        "quantita_fonte": quantity_source,
    }


# --------------------------------------------------------------------------- #
# Unione di un gruppo
# --------------------------------------------------------------------------- #
def merge_group(
    entries: Mapping[str, SourceProduct],
    table: AdditiveTable | None = None,
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Unisce lo stesso prodotto visto da piu' fonti in un record con provenienza."""
    if not entries:
        raise ValueError("gruppo vuoto")

    nutrients = merge_nutrients(entries, profiles)
    additives = merge_additives(entries, table)
    scores = merge_scores(entries, profiles)
    grade = _merge_choice(entries, "grade", "grade", profiles)
    nova = _merge_choice(entries, "nova_group", "nova", profiles)
    if isinstance(nova["valore"], str) and nova["valore"].isdigit():
        nova["valore"] = int(nova["valore"])
    identity = _identity(entries, profiles)
    price = merge_price(entries, profiles)

    nutrient_coverage = len(nutrients["values"]) / len(NUTRIENTS)
    additives_coverage = 1.0 if (additives["dichiarati"] or "off" in entries) else 0.0
    grade_coverage = 1.0 if grade["valore"] else 0.0
    price_coverage = 1.0 if price["valore"] is not None else 0.0
    coverage = round(
        0.50 * nutrient_coverage
        + 0.20
        + 0.15 * additives_coverage
        + 0.10 * grade_coverage
        + 0.05 * price_coverage,
        3,
    )
    source_count = len(entries)
    confidence = round(
        0.5 * coverage
        + 0.3 * nutrients["agreement"]
        + 0.2 * min(1.0, source_count / 3.0),
        3,
    )

    flags: list[str] = []
    if nutrients["conflicts"]:
        flags.append("nutrienti_discordanti")
    if scores["conflitto"]:
        flags.append("punteggi_discordanti")
    if grade["conflitto"]:
        flags.append("grade_discordante")
    if nova["conflitto"]:
        flags.append("nova_discordante")
    if additives["sconosciuti"]:
        flags.append("additivi_sconosciuti")
    if source_count == 1:
        flags.append("una_sola_fonte")
    if price["valore"] is None:
        flags.append("senza_prezzo")
    if not additives["dichiarati"] and "off" not in entries:
        flags.append("additivi_non_dichiarati")

    barcode = entries[next(iter(entries))].barcode

    return {
        "barcode": barcode,
        "gtin": gtin14(barcode),
        "nome": identity["nome"],
        "marca": identity["marca"],
        "quantita": identity["quantita"],
        "nutrienti": nutrients["values"],
        "provenienza_nutrienti": nutrients["provenance"],
        "additivi": additives["additivi"],
        "additivi_sconosciuti": additives["sconosciuti"],
        "punteggio_additivi": additives["punteggio"],
        "additivi_dichiarati": additives["dichiarati"],
        "grade": grade["valore"],
        "nova": nova["valore"],
        "etichette": _union(entries, "labels"),
        "categorie": _union(entries, "categories"),
        "prezzo": price["valore"],
        "prezzo_base": price["base"],
        "prezzo_osservazioni": price["osservazioni"],
        "negozi": price["negozi"],
        "punteggio_fonti": scores,
        "conflitti": nutrients["conflicts"],
        "fonti": sorted(entries),
        "copertura": coverage,
        "confidenza": confidence,
        "flag": flags,
    }


def merge_products(
    groups: Iterable[Mapping[str, SourceProduct]],
    table: AdditiveTable | None = None,
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    merged = [merge_group(entries, table, profiles) for entries in groups if entries]
    merged.sort(key=lambda item: (norm_text(item["nome"]), item["barcode"]))
    return merged


# --------------------------------------------------------------------------- #
# Consenso sulle tabelle di rischio additivi
# --------------------------------------------------------------------------- #
def _e_sort(key: str) -> tuple[int, str]:
    match = re.fullmatch(r"e(\d+)([a-z]*)", str(key).lower())
    if match:
        return (int(match.group(1)), match.group(2))
    return (10**9, str(key))


def merge_risk_tables(
    tables: Mapping[str, Mapping[str, float | None]],
    profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Mediana pesata del rischio per E-number, con copertura e buchi."""
    numbers = sorted({str(key) for table in tables.values() for key in table}, key=_e_sort)
    total_weight = sum(_field_weight(source, "risk", profiles) for source in tables) or 1.0
    rows: list[dict[str, Any]] = []

    for number in numbers:
        samples: list[tuple[str, float, float]] = []
        missing: list[str] = []
        for source, table in tables.items():
            risk = table.get(number)
            if risk is None:
                missing.append(source)
                continue
            weight = _field_weight(source, "risk", profiles)
            if weight <= 0:
                continue
            samples.append((source, float(risk), weight))

        median = weighted_median([(value, weight) for _, value, weight in samples])
        spread = (
            max(value for _, value, _ in samples) - min(value for _, value, _ in samples)
            if samples
            else None
        )
        if not samples:
            agreement = 0.0
        elif len(samples) == 1:
            agreement = 1.0
        else:
            agreement = max(0.0, 1.0 - min(1.0, float(spread) / 30.0))
        coverage = sum(weight for _, _, weight in samples) / total_weight
        confidence = round(coverage * agreement, 3) if samples else 0.0

        reasons: list[str] = []
        if len(samples) < 2:
            reasons.append("una sola fonte")
        if missing:
            reasons.append("manca in: " + ", ".join(sorted(missing)))
        if spread is not None and spread > RISK_SPREAD:
            reasons.append(f"fonti discordi (±{spread:.0f})")

        rows.append(
            {
                "e_number": number,
                "risk_consensus": median,
                "n_sources": len(samples),
                "fonti": sorted(source for source, _, _ in samples),
                "missing_in": sorted(missing),
                "spread": round(float(spread), 1) if spread is not None else None,
                "agreement": round(agreement, 3),
                "confidence": confidence,
                "needs_review": bool(reasons),
                "reasons": reasons,
            }
        )
    return rows
