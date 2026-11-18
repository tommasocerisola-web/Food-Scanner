"""
Scoring engine: recalibrated Nutri-Score + per-additive risk.

Key difference from Yuka: Yuka penalises *how many* additives a product has, so a
single very toxic preservative inside an otherwise clean product gets "diluted".
Here every additive carries its own 0-100 risk and the worst one dominates the
additive component (65% of the component weight).

Final score = weighted average of the known components (if a value is missing,
the other weights are renormalized and `confidence` drops).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .additives import AdditiveTable
from .models import (
    AdditiveHit,
    Alert,
    ComponentScore,
    NutrientFacts,
    ProductRecord,
    ScoreResult,
    grade_for,
    mean,
)

ENGINE_VERSION = "0.3-beta"

# --------------------------------------------------------------------------- #
# Nutri-Score tables (per 100 g)
# --------------------------------------------------------------------------- #
# Negative/positive point thresholds of the 2017 Nutri-Score algorithm (solid
# foods). Each crossed threshold is worth 1 point; the maximum is in parentheses.
SOLID_THRESHOLDS: dict[str, tuple[float, ...]] = {
    "energy_kcal": (80, 160, 240, 320, 400, 480, 560, 640, 720, 800),  # max 10
    "sugars": (4.5, 9, 13.5, 18, 22.5, 27, 31, 36, 40, 45),  # max 10
    "saturated_fat": (1, 2, 3, 4, 5, 6, 7, 8, 9, 10),  # max 10
    "salt": (0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.1, 2.4, 2.7, 3.0),  # max 10
    "fiber": (0.9, 1.9, 2.8, 3.7, 4.7),  # max 5
    "protein": (1.6, 3.2, 4.8, 6.4, 8.0),  # max 5
}
BEVERAGE_THRESHOLDS: dict[str, tuple[float, ...]] = {
    "energy_kcal": (0, 7.2, 14.3, 21.5, 28.7, 35.9, 43.0, 50.2, 57.4, 64.5),  # 0..270 kJ
    "sugars": (0, 1.5, 3, 4.5, 6, 7.5, 9, 10.5, 12, 13.5),
}
# Fruit/vegetables/nuts as a percentage of the total.
FRUIT_THRESHOLDS: tuple[tuple[float, float], ...] = ((40, 1), (60, 2), (80, 5))
BEVERAGE_FRUIT_THRESHOLDS: tuple[tuple[float, float], ...] = ((40, 2), (60, 4), (80, 10))

# Nutri-Score -> 0-100 band conversion, aligned with the A-E letters.
# A plain linear mapping (-15..40) would reward average products too much:
# a Nutri-Score of 0 (band B/C) would end up at 72/100. Here instead:
#   A (<= -1) -> 85..100 · B (0..2) -> 70..85 · C (3..10) -> 50..70
#   D (11..18) -> 25..50 · E (>= 19) -> 0..25
SOLID_NS_BANDS: tuple[tuple[float, float, float], ...] = (
    (-15.0, 100.0, 85.0),
    (-1.0, 85.0, 70.0),
    (2.0, 70.0, 50.0),
    (10.0, 50.0, 25.0),
    (18.0, 25.0, 0.0),
    (40.0, 0.0, 0.0),
)
BEVERAGE_NS_BANDS: tuple[tuple[float, float, float], ...] = (
    (-10.0, 100.0, 90.0),
    (1.0, 90.0, 75.0),
    (5.0, 75.0, 55.0),
    (9.0, 55.0, 25.0),
    (15.0, 25.0, 0.0),
    (20.0, 0.0, 0.0),
)

# NOVA (degree of industrial processing) -> 0-100 component.
NOVA_SCORES: dict[int, float] = {1: 100.0, 2: 80.0, 3: 55.0, 4: 20.0}

_NUTRIENT_LABELS = {
    "energy_kcal": ("Energy", "kcal"),
    "sugars": ("Sugars", "g"),
    "saturated_fat": ("Saturated fat", "g"),
    "salt": ("Salt", "g"),
}
_POSITIVE_LABELS = {
    "fiber": ("Fiber", "fiber", "g"),
    "protein": ("Protein", "protein", "g"),
    "fruits_veg_nuts": ("Fruits/vegetables", "fruits_veg_nuts", "%"),
}


@dataclass(frozen=True)
class Weights:
    nutrition: float = 0.50
    additives: float = 0.35
    processing: float = 0.10
    organic: float = 0.05

    def normalized(self) -> "Weights":
        total = self.nutrition + self.additives + self.processing + self.organic
        if total <= 0:
            return Weights()
        return Weights(
            nutrition=self.nutrition / total,
            additives=self.additives / total,
            processing=self.processing / total,
            organic=self.organic / total,
        )


@dataclass(frozen=True)
class EngineConfig:
    """Tunable algorithm parameters (the requested "recalibration")."""

    weights: Weights = field(default_factory=Weights)
    # How much the single worst additive weighs against the average additive.
    worst_additive_weight: float = 0.65
    # Extra pressure coming from the number of additives (beyond the first 2).
    additive_count_step: float = 2.5
    additive_count_free: int = 2
    additive_count_cap: float = 10.0
    # Alert thresholds on individual additives.
    critical_risk: float = 70.0
    warning_risk: float = 45.0
    # Organic must not penalise: neutral 50, full bonus when certified.
    organic_neutral: float = 50.0


class ScoreEngine:
    """Pure engine with no I/O: takes a ProductRecord, returns a ScoreResult."""

    def __init__(self, table: AdditiveTable | None = None, config: EngineConfig | None = None) -> None:
        self.table = table
        self.config = config or EngineConfig()
        self.weights = self.config.weights.normalized()

    # ------------------------------------------------------------------ #
    # Main API
    # ------------------------------------------------------------------ #
    def score(self, product: ProductRecord, table: AdditiveTable | None = None) -> ScoreResult:
        table = table or self.table
        hits = self._resolve_additives(product, table)

        nutrition = self._nutrition_component(product)
        additives = self._additive_component(hits)
        processing = self._processing_component(product, hits)
        organic = self._organic_component(product)

        components = (nutrition, additives, processing, organic)
        known = [component for component in components if component.score is not None]
        total_weight = sum(component.weight for component in known)
        raw = (
            sum(component.score * component.weight for component in known) / total_weight
            if total_weight
            else 50.0
        )
        # Shrinkage: with incomplete data the score must not claim excellence
        # (an empty product is not "great"): it moves towards 50 in proportion
        # to the data coverage.
        adjusted = 50.0 + (raw - 50.0) * total_weight
        score = int(round(max(0.0, min(100.0, adjusted))))
        additive_coverage = (
            1.0 if not hits else sum(1 for hit in hits if not hit.risk_estimated) / len(hits)
        )
        return ScoreResult(
            score=score,
            grade=grade_for(score),
            product=product,
            components=components,
            additives=hits,
            alerts=self._alerts(hits, product),
            reasons=self._reasons(product, nutrition, additives, processing, hits),
            confidence=total_weight,
            additive_coverage=additive_coverage,
            engine_version=ENGINE_VERSION,
        )

    # ------------------------------------------------------------------ #
    # Additives
    # ------------------------------------------------------------------ #
    def _resolve_additives(self, product: ProductRecord, table: AdditiveTable | None) -> tuple[AdditiveHit, ...]:
        if table is None:
            return ()
        hits: list[AdditiveHit] = []
        seen: set[str] = set()
        for token in product.additives:
            row = table.get(token)
            if row is None:
                continue
            if row.e_number.lower() in seen:
                continue
            seen.add(row.e_number.lower())
            risk, estimated = row.default_risk()
            dose = _declared_dose(product, row)
            effective = risk * _dose_factor(dose) if dose else risk
            hits.append(
                AdditiveHit(
                    key=row.key,
                    e_number=row.e_number,
                    name=row.display_name,
                    classes=row.classes,
                    risk=effective,
                    risk_estimated=estimated,
                    why=row.why,
                    base_risk=risk,
                    dose_percent=dose,
                )
            )
        return tuple(sorted(hits, key=lambda hit: hit.risk, reverse=True))

    def _additive_component(self, hits: tuple[AdditiveHit, ...]) -> ComponentScore:
        cfg = self.config
        if not hits:
            return ComponentScore(
                name="additives",
                score=100.0,
                weight=self.weights.additives if self.table is not None else 0.0,
                details={"count": 0, "note": "no declared additives"},
            )

        risks = [hit.risk for hit in hits]
        worst = max(risks)
        average = mean(risks)
        excess = max(0, len(risks) - cfg.additive_count_free)
        count_pressure = min(cfg.additive_count_cap, cfg.additive_count_step * excess)
        penalty = worst * cfg.worst_additive_weight + average * (1 - cfg.worst_additive_weight) + count_pressure
        score = max(0.0, min(100.0, 100.0 - penalty))
        return ComponentScore(
            name="additives",
            score=score,
            weight=self.weights.additives,
            details={
                "count": len(risks),
                "worst_risk": round(worst, 1),
                "worst_additive": hits[0].e_number,
                "mean_risk": round(average, 1),
                "count_pressure": round(count_pressure, 1),
                "penalty": round(penalty, 1),
                "worst_weight": cfg.worst_additive_weight,
                "estimated": sum(1 for hit in hits if hit.risk_estimated),
            },
        )

    # ------------------------------------------------------------------ #
    # Nutrients
    # ------------------------------------------------------------------ #
    def _nutrition_component(self, product: ProductRecord) -> ComponentScore:
        score, details = self.nutrition_score(product.nutriments, product.is_beverage)
        return ComponentScore(
            name="nutrition",
            score=score,
            weight=self.weights.nutrition,
            details=details,
        )

    def nutrition_score(
        self, facts: NutrientFacts, is_beverage: bool = False
    ) -> tuple[float | None, dict[str, Any]]:
        present, minimum = _key_nutrient_coverage(facts, is_beverage)
        if present < minimum:
            return None, {
                "missing": f"incomplete nutrition data ({present}/{4 if not is_beverage else 2} key values)",
                "coverage": f"{present}/{4 if not is_beverage else 2}",
            }

        if is_beverage:
            negative, positive = self._beverage_points(facts)
            ns = negative["total"] - positive["total"]
            return _ns_to_score(ns, beverage=True), {
                "negative_points": negative,
                "positive_points": positive,
                "nutri_score": ns,
                "profile": "beverage",
            }

        negative, positive, details = self._solid_points(facts)
        ns = negative["total"] - positive["total"]
        details.update(
            {
                "negative_points": negative,
                "positive_points": positive,
                "nutri_score": ns,
                "profile": "solid",
            }
        )
        return _ns_to_score(ns), details

    def _points(self, value: float | None, thresholds: tuple[float, ...]) -> int:
        if value is None:
            return 0
        return sum(1 for threshold in thresholds if value > threshold)

    def _solid_points(
        self, facts: NutrientFacts
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        t = SOLID_THRESHOLDS
        salt = facts.salt
        if salt is None and facts.sodium is not None:
            salt = facts.sodium * 2.5  # 1 g of sodium = 2.5 g of salt

        negative = {
            "energy_kcal": self._points(facts.kcal, t["energy_kcal"]),
            "sugars": self._points(facts.sugars, t["sugars"]),
            "saturated_fat": self._points(facts.saturated_fat, t["saturated_fat"]),
            "salt": self._points(salt, t["salt"]),
        }
        negative["total"] = sum(negative.values())

        fiber_points = self._points(facts.fiber, t["fiber"])
        protein_points = self._points(facts.protein, t["protein"])
        fruit_points = _fruit_points(facts.fruits_veg_nuts, FRUIT_THRESHOLDS)

        # Nutri-Score rule: if negative points are >= 11 and fruit is < 5,
        # protein does not count (avoids rewarding salty protein snacks).
        if negative["total"] >= 11 and fruit_points < 5:
            protein_points = 0

        positive = {
            "fiber": fiber_points,
            "protein": protein_points,
            "fruits_veg_nuts": fruit_points,
        }
        positive["total"] = sum(positive.values())
        details = {
            "salt_g": None if salt is None else round(salt, 3),
            "salt_from_sodium": facts.salt is None and facts.sodium is not None,
        }
        return negative, positive, details

    def _beverage_points(self, facts: NutrientFacts) -> tuple[dict[str, Any], dict[str, Any]]:
        t = BEVERAGE_THRESHOLDS
        negative = {
            "energy_kcal": self._points(facts.kcal, t["energy_kcal"]),
            "sugars": self._points(facts.sugars, t["sugars"]),
        }
        negative["total"] = sum(negative.values())
        positive = {
            "fruits_veg_nuts": _fruit_points(facts.fruits_veg_nuts, BEVERAGE_FRUIT_THRESHOLDS),
        }
        positive["total"] = sum(positive.values())
        return negative, positive

    # ------------------------------------------------------------------ #
    # Processing and organic
    # ------------------------------------------------------------------ #
    def _processing_component(self, product: ProductRecord, hits: tuple[AdditiveHit, ...]) -> ComponentScore:
        nova = product.nova_group
        if nova in NOVA_SCORES:
            return ComponentScore(
                name="processing",
                score=NOVA_SCORES[nova],
                weight=self.weights.processing,
                details={"nova_group": nova, "estimated": False},
            )
        # Cautious estimate when NOVA is missing: few additives = little processed.
        if not hits:
            return ComponentScore(
                name="processing",
                score=None,
                weight=self.weights.processing,
                details={"nova_group": None, "estimated": True},
            )
        estimated = max(20.0, 90.0 - 8.0 * len(hits))
        return ComponentScore(
            name="processing",
            score=estimated,
            weight=self.weights.processing,
            details={"nova_group": None, "estimated": True},
        )

    def _organic_component(self, product: ProductRecord) -> ComponentScore:
        neutral = self.config.organic_neutral
        return ComponentScore(
            name="organic",
            score=100.0 if product.organic else neutral,
            weight=self.weights.organic,
            details={"organic": product.organic},
        )

    # ------------------------------------------------------------------ #
    # Explanations
    # ------------------------------------------------------------------ #
    def _reasons(
        self,
        product: ProductRecord,
        nutrition: ComponentScore,
        additives: ComponentScore,
        processing: ComponentScore,
        hits: tuple[AdditiveHit, ...],
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        facts = product.nutriments

        if nutrition.score is None:
            reasons.append(
                "Nutrients not scored: " + str(nutrition.details.get("missing", "insufficient data"))
            )
        else:
            values = {
                "energy_kcal": (facts.kcal, "kcal"),
                "sugars": (facts.sugars, "g"),
                "saturated_fat": (facts.saturated_fat, "g"),
                "salt": (nutrition.details.get("salt_g"), "g"),
            }
            for key, points in nutrition.details.get("negative_points", {}).items():
                if key == "total" or not points:
                    continue
                label, unit = _NUTRIENT_LABELS.get(key, (key, ""))
                value = values.get(key, (None, ""))[0]
                shown = "n/a" if value is None else f"{value:g} {unit}"
                reasons.append(f"{label} {shown}/100 g: {points}/10 negative points")
            for key, points in nutrition.details.get("positive_points", {}).items():
                if key == "total" or not points:
                    continue
                label, attribute, unit = _POSITIVE_LABELS.get(key, (key, "", ""))
                value = getattr(facts, attribute) if attribute else None
                shown = "n/a" if value is None else f"{value:g} {unit}"
                reasons.append(f"{label} {shown}: {points} positive points")

        if not hits:
            reasons.append("Additives: none declared on the label.")
        else:
            top = hits[0]
            suffix = f" \u2014 {top.why}" if top.why else ""
            reasons.append(
                f"Worst additive: {top.e_number} ({top.name}) risk {top.risk:.0f}/100{suffix}"
            )
            if len(hits) > 3:
                reasons.append(f"{len(hits)} declared additives: the worst one carries the most weight.")

        if product.nova_group:
            label = {1: "unprocessed", 2: "minimally processed", 3: "processed", 4: "ultra-processed"}.get(
                product.nova_group, f"group {product.nova_group}"
            )
            reasons.append(f"Processing NOVA {product.nova_group}: {label}.")
        if product.organic:
            reasons.append("Certified organic: label bonus.")
        return tuple(reasons)

    # ------------------------------------------------------------------ #
    # Alerts
    # ------------------------------------------------------------------ #
    def _alerts(self, hits: tuple[AdditiveHit, ...], product: ProductRecord) -> tuple[Alert, ...]:
        alerts: list[Alert] = []
        for hit in hits:
            if hit.risk >= self.config.critical_risk:
                alerts.append(
                    Alert(
                        level="critical",
                        code="additive_critical",
                        message=(
                            f"{hit.e_number} ({hit.name}) risk {hit.risk:.0f}/100"
                            + (" [estimated score]" if hit.risk_estimated else "")
                        ),
                    )
                )
            elif hit.risk >= self.config.warning_risk:
                alerts.append(
                    Alert(
                        level="warning",
                        code="additive_warning",
                        message=f"{hit.e_number} ({hit.name}) risk {hit.risk:.0f}/100",
                    )
                )
        if len(hits) > 5:
            alerts.append(
                Alert(
                    level="info",
                    code="many_additives",
                    message=f"{len(hits)} declared additives",
                )
            )
        if product.nova_group == 4:
            alerts.append(Alert(level="info", code="ultra_processed", message="Ultra-processed product (NOVA 4)"))
        if product.nutrition_data_per and product.nutrition_data_per.lower() != "100g":
            alerts.append(
                Alert(
                    level="info",
                    code="per_serving",
                    message=f"Nutrition data per {product.nutrition_data_per}: check the values per 100 g/ml",
                )
            )
        return tuple(alerts)


def _declared_dose(product: ProductRecord, row: Any) -> float | None:
    """Percentage declared in the ingredients, when Open Food Facts estimates it."""
    for key in (row.key, f"en:{row.e_number.lower()}", f"en:{row.key.split(':', 1)[-1]}"):
        if key in product.additive_doses:
            return product.additive_doses[key]
    return None


def _dose_factor(percent: float | None) -> float:
    """Lowers the risk when the declared dose is very low.

    0.01% -> 0.30 · 0.1% -> 0.55 · 1% -> 0.80 · >=10% -> 1.00.
    Conservative: without the data the dose reduces nothing.
    """
    if percent is None or percent <= 0:
        return 1.0
    return max(0.2, min(1.0, 0.3 + 0.25 * math.log10(percent / 0.01)))


def _fruit_points(percent: float | None, thresholds: tuple[tuple[float, float], ...]) -> int:
    if percent is None:
        return 0
    points = 0
    for minimum, value in thresholds:
        if percent > minimum:
            points = int(value)
    return points


def _key_nutrient_coverage(facts: NutrientFacts, is_beverage: bool) -> tuple[int, int]:
    """(key values present, acceptable minimum).

    Scoring with a single value and treating the others as 0 inflates the
    result: below the minimum the nutrition component is None (weight
    renormalized, lower confidence).
    """
    if is_beverage:
        return sum(value is not None for value in (facts.kcal, facts.sugars)), 2
    salt = facts.salt if facts.salt is not None else facts.sodium
    present = sum(
        value is not None
        for value in (facts.kcal, facts.sugars, facts.saturated_fat, salt)
    )
    return present, 3


def _ns_to_score(ns: float, beverage: bool = False) -> float:
    """Interpolates the Nutri-Score inside the A-E bands and returns 0-100."""
    bands = BEVERAGE_NS_BANDS if beverage else SOLID_NS_BANDS
    previous_ns, previous_score = bands[0][0], bands[0][1]
    for limit, score_at_limit, _ in bands[1:]:
        if ns <= limit:
            span = limit - previous_ns or 1.0
            ratio = (ns - previous_ns) / span
            score = previous_score + (score_at_limit - previous_score) * ratio
            return max(0.0, min(100.0, score))
        previous_ns, previous_score = limit, score_at_limit
    return 0.0
