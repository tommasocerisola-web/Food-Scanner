"""
Motore di scoring: Nutri-Score ricalibrato + rischio per singolo additivo.

Differenza chiave rispetto a Yuka: Yuka penalizza *quanti* additivi ci sono,
quindi un solo conservante molto tossico dentro un prodotto altrimenti pulito
viene "diluito". Qui ogni additivo porta il proprio rischio 0-100 e il punteggio
del peggiore domina la componente additivi (65% del peso della componente).

Score finale = media pesata di componenti note (se un dato manca, i pesi delle
altre componenti vengono rinormalizzati e la `confidence` scende).
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

ENGINE_VERSION = "0.2-beta"

# --------------------------------------------------------------------------- #
# Tabelle Nutri-Score (per 100 g)
# --------------------------------------------------------------------------- #
# Soglie dei punti negativi/positivi dell'algoritmo Nutri-Score 2017 (alimenti
# solidi). Ogni soglia superata vale 1 punto; il massimo e' indicato tra parentesi.
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
# Verdure/frutta/frutta a guscio in percentuale sul totale.
FRUIT_THRESHOLDS: tuple[tuple[float, float], ...] = ((40, 1), (60, 2), (80, 5))
BEVERAGE_FRUIT_THRESHOLDS: tuple[tuple[float, float], ...] = ((40, 2), (60, 4), (80, 10))

# Conversione Nutri-Score -> 0-100 a fasce, allineata alle lettere A-E.
# Una mappatura lineare semplice (-15..40) premierebbe troppo i prodotti medi:
# un Nutri-Score 0 (fascia B/C) finirebbe a 72/100. Qui invece:
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

# NOVA (grado di lavorazione industriale) -> componente 0-100.
NOVA_SCORES: dict[int, float] = {1: 100.0, 2: 80.0, 3: 55.0, 4: 20.0}

_NUTRIENT_LABELS = {
    "energy_kcal": ("Energia", "kcal"),
    "sugars": ("Zuccheri", "g"),
    "saturated_fat": ("Grassi saturi", "g"),
    "salt": ("Sale", "g"),
}
_POSITIVE_LABELS = {
    "fiber": ("Fibre", "fiber", "g"),
    "protein": ("Proteine", "protein", "g"),
    "fruits_veg_nuts": ("Frutta/verdura", "fruits_veg_nuts", "%"),
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
    """Parametri tarabili dell'algoritmo (il "ricalibraggio" richiesto)."""

    weights: Weights = field(default_factory=Weights)
    # Quanto il singolo additivo peggiore pesa rispetto alla media degli additivi.
    worst_additive_weight: float = 0.65
    # Pressione aggiuntiva dovuta al numero di additivi (oltre i primi 2).
    additive_count_step: float = 2.5
    additive_count_free: int = 2
    additive_count_cap: float = 10.0
    # Soglie di allerta sui singoli additivi.
    critical_risk: float = 70.0
    warning_risk: float = 45.0
    # Il biologico non deve penalizzare: neutro 50, premio pieno se certificato.
    organic_neutral: float = 50.0


class ScoreEngine:
    """Motore puro e senza I/O: riceve un ProductRecord, restituisce uno ScoreResult."""

    def __init__(self, table: AdditiveTable | None = None, config: EngineConfig | None = None) -> None:
        self.table = table
        self.config = config or EngineConfig()
        self.weights = self.config.weights.normalized()

    # ------------------------------------------------------------------ #
    # API principale
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
        # Shrinkage: con dati incompleti il punteggio non deve dichiarare
        # eccellenza (un prodotto vuoto non e' "ottimo"): si avvicina a 50 in
        # proporzione alla copertura dei dati.
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
    # Additivi
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
                details={"count": 0, "note": "nessun additivo dichiarato"},
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
    # Nutrienti
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
                "missing": f"dati nutrizionali incompleti ({present}/{4 if not is_beverage else 2} valori chiave)",
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
            salt = facts.sodium * 2.5  # 1 g di sodio = 2.5 g di sale

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

        # Regola Nutri-Score: se i punti negativi sono >= 11 e la frutta < 5,
        # le proteine non contano (evita di premiare snack proteici salati).
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
    # Lavorazione e biologico
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
        # Stima prudente quando NOVA manca: pochi additivi = poco lavorato.
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
    # Spiegazioni
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
                "Nutrienti non valutati: " + str(nutrition.details.get("missing", "dati insufficienti"))
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
                shown = "n/d" if value is None else f"{value:g} {unit}"
                reasons.append(f"{label} {shown}/100 g: {points}/10 punti negativi")
            for key, points in nutrition.details.get("positive_points", {}).items():
                if key == "total" or not points:
                    continue
                label, attribute, unit = _POSITIVE_LABELS.get(key, (key, "", ""))
                value = getattr(facts, attribute) if attribute else None
                shown = "n/d" if value is None else f"{value:g} {unit}"
                reasons.append(f"{label} {shown}: {points} punti positivi")

        if not hits:
            reasons.append("Additivi: nessuno dichiarato in etichetta.")
        else:
            top = hits[0]
            suffix = f" \u2014 {top.why}" if top.why else ""
            reasons.append(
                f"Additivo peggiore: {top.e_number} ({top.name}) rischio {top.risk:.0f}/100{suffix}"
            )
            if len(hits) > 3:
                reasons.append(f"{len(hits)} additivi dichiarati: il peso maggiore va al peggiore.")

        if product.nova_group:
            label = {1: "non lavorato", 2: "minimamente lavorato", 3: "lavorato", 4: "ultra-processato"}.get(
                product.nova_group, f"gruppo {product.nova_group}"
            )
            reasons.append(f"Lavorazione NOVA {product.nova_group}: {label}.")
        if product.organic:
            reasons.append("Certificato biologico: bonus in etichetta.")
        return tuple(reasons)

    # ------------------------------------------------------------------ #
    # Allerte
    # ------------------------------------------------------------------ #
    def _alerts(self, hits: tuple[AdditiveHit, ...], product: ProductRecord) -> tuple[Alert, ...]:
        alerts: list[Alert] = []
        for hit in hits:
            if hit.risk >= self.config.critical_risk:
                alerts.append(
                    Alert(
                        level="critico",
                        code="additive_critical",
                        message=(
                            f"{hit.e_number} ({hit.name}) rischio {hit.risk:.0f}/100"
                            + (" [punteggio stimato]" if hit.risk_estimated else "")
                        ),
                    )
                )
            elif hit.risk >= self.config.warning_risk:
                alerts.append(
                    Alert(
                        level="attenzione",
                        code="additive_warning",
                        message=f"{hit.e_number} ({hit.name}) rischio {hit.risk:.0f}/100",
                    )
                )
        if len(hits) > 5:
            alerts.append(
                Alert(
                    level="info",
                    code="many_additives",
                    message=f"{len(hits)} additivi dichiarati",
                )
            )
        if product.nova_group == 4:
            alerts.append(Alert(level="info", code="ultra_processed", message="Prodotto ultra-processato (NOVA 4)"))
        if product.nutrition_data_per and product.nutrition_data_per.lower() != "100g":
            alerts.append(
                Alert(
                    level="info",
                    code="per_serving",
                    message=f"Dati nutrizionali per {product.nutrition_data_per}: verifica i valori per 100 g/ml",
                )
            )
        return tuple(alerts)


def _declared_dose(product: ProductRecord, row: Any) -> float | None:
    """Percentuale dichiarata negli ingredienti, se Open Food Facts la stima."""
    for key in (row.key, f"en:{row.e_number.lower()}", f"en:{row.key.split(':', 1)[-1]}"):
        if key in product.additive_doses:
            return product.additive_doses[key]
    return None


def _dose_factor(percent: float | None) -> float:
    """Riduce il rischio quando la dose dichiarata e' molto bassa.

    0,01% -> 0,30 · 0,1% -> 0,55 · 1% -> 0,80 · >=10% -> 1,00.
    Conservativo: senza dato la dose non riduce nulla.
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
    """(valori chiave presenti, minimo accettabile).

    Calcolare lo score con un solo valore e trattare gli altri come 0 gonfia il
    risultato: sotto il minimo la componente nutrienti vale None (peso
    rinormalizzato, confidence piu' bassa).
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
    """Interpola il Nutri-Score nelle fasce A-E e restituisce 0-100."""
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
