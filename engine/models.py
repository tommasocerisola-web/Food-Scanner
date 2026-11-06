"""Modelli dati del motore: prodotto normalizzato, additivo, componenti di score."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# Soglie di valutazione finale (stile Yuka: 4 fasce).
GRADE_THRESHOLDS: tuple[tuple[int, str], ...] = (
    (75, "eccellente"),
    (50, "buono"),
    (25, "scarso"),
    (0, "pessimo"),
)


def grade_for(score: float) -> str:
    for threshold, label in GRADE_THRESHOLDS:
        if score >= threshold:
            return label
    return "pessimo"


@dataclass(frozen=True)
class NutrientFacts:
    """Valori per 100 g/ml. None = dato assente in Open Food Facts."""

    kcal: float | None = None
    sugars: float | None = None
    added_sugars: float | None = None
    salt: float | None = None
    sodium: float | None = None
    saturated_fat: float | None = None
    fat: float | None = None
    fiber: float | None = None
    protein: float | None = None
    fruits_veg_nuts: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kcal": _r(self.kcal),
            "sugars": _r(self.sugars),
            "added_sugars": _r(self.added_sugars),
            "salt": _r(self.salt),
            "sodium": _r(self.sodium),
            "saturated_fat": _r(self.saturated_fat),
            "fat": _r(self.fat),
            "fiber": _r(self.fiber),
            "protein": _r(self.protein),
            "fruits_veg_nuts": _r(self.fruits_veg_nuts),
        }


@dataclass(frozen=True)
class ProductRecord:
    """Prodotto Open Food Facts normalizzato e pronto per lo scoring."""

    barcode: str
    name: str = ""
    brand: str = ""
    quantity: str | None = None
    categories: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    ingredients_text: str | None = None
    additives: tuple[str, ...] = ()  # chiavi OFF, es. ('en:e322',)
    nutriments: NutrientFacts = field(default_factory=NutrientFacts)
    nova_group: int | None = None
    nutriscore_grade: str | None = None
    is_beverage: bool = False
    nutrition_data_per: str | None = None
    additive_doses: dict[str, float] = field(default_factory=dict)
    image_url: str | None = None
    image_nutrition_url: str | None = None
    image_ingredients_url: str | None = None

    @property
    def organic(self) -> bool:
        return any(_is_bio_label(label) for label in self.labels)

    def to_dict(self) -> dict[str, Any]:
        return {
            "barcode": self.barcode,
            "name": self.name,
            "brand": self.brand,
            "quantity": self.quantity,
            "categories": list(self.categories),
            "labels": list(self.labels),
            "ingredients_text": self.ingredients_text,
            "additives": list(self.additives),
            "nutriments": self.nutriments.to_dict(),
            "nova_group": self.nova_group,
            "nutriscore_grade": self.nutriscore_grade,
            "is_beverage": self.is_beverage,
            "nutrition_data_per": self.nutrition_data_per,
            "additive_doses": dict(self.additive_doses),
            "image_url": self.image_url,
            "image_nutrition_url": self.image_nutrition_url,
            "image_ingredients_url": self.image_ingredients_url,
            "organic": self.organic,
        }


@dataclass(frozen=True)
class AdditiveHit:
    """Additivo trovato nel prodotto, con il rischio applicato."""

    key: str
    e_number: str
    name: str
    classes: tuple[str, ...]
    risk: float
    risk_estimated: bool
    why: str = ""
    base_risk: float | None = None
    dose_percent: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "e_number": self.e_number,
            "key": self.key,
            "name": self.name,
            "classes": list(self.classes),
            "risk": _r(self.risk),
            "base_risk": _r(self.base_risk),
            "dose_percent": _r(self.dose_percent),
            "risk_estimated": self.risk_estimated,
            "why": self.why,
        }


@dataclass(frozen=True)
class ComponentScore:
    """Punteggio 0-100 di una singola componente (o None se dato assente)."""

    name: str
    score: float | None
    weight: float
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": _r(self.score),
            "weight": self.weight,
            "details": self.details,
        }


@dataclass(frozen=True)
class Alert:
    level: str  # 'critico' | 'attenzione' | 'info'
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class ScoreResult:
    """Risultato completo: pronto per essere serializzato verso la futura UI."""

    score: int
    grade: str
    product: ProductRecord
    components: tuple[ComponentScore, ...]
    additives: tuple[AdditiveHit, ...]
    alerts: tuple[Alert, ...] = ()
    reasons: tuple[str, ...] = ()
    confidence: float = 1.0
    additive_coverage: float = 1.0
    engine_version: str = "0.2-beta"

    @property
    def worst_additive(self) -> AdditiveHit | None:
        return max(self.additives, key=lambda hit: hit.risk, default=None)

    @property
    def unscored_additives(self) -> int:
        return sum(1 for hit in self.additives if hit.risk_estimated)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "grade": self.grade,
            "confidence": round(self.confidence, 3),
            "additive_coverage": round(self.additive_coverage, 3),
            "engine_version": self.engine_version,
            "product": self.product.to_dict(),
            "components": [component.to_dict() for component in self.components],
            "additives": [hit.to_dict() for hit in self.additives],
            "alerts": [alert.to_dict() for alert in self.alerts],
            "reasons": list(self.reasons),
            "worst_additive": self.worst_additive.to_dict() if self.worst_additive else None,
            "unscored_additives": self.unscored_additives,
        }


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #
_BIO = ("organic", "bio", "eu-organic", "ab-agriculture-biologique", "agriculture-biologique")


def _is_bio_label(label: str) -> bool:
    plain = label.split(":", 1)[-1].lower()
    return any(marker == plain or marker in plain for marker in _BIO)


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0
