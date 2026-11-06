"""Motore di scoring food: Open Food Facts -> punteggio 0-100 spiegabile."""
from __future__ import annotations

from .additives import AdditiveRow, AdditiveTable, DEFAULT_TABLE_PATH
from .models import (
    AdditiveHit,
    Alert,
    ComponentScore,
    NutrientFacts,
    ProductRecord,
    ScoreResult,
)
from .off_client import OFFClient, OFFError, normalize_product
from .scoring import EngineConfig, ScoreEngine, Weights

__all__ = [
    "AdditiveHit",
    "AdditiveRow",
    "AdditiveTable",
    "Alert",
    "ComponentScore",
    "DEFAULT_TABLE_PATH",
    "EngineConfig",
    "NutrientFacts",
    "OFFClient",
    "OFFError",
    "ProductRecord",
    "ScoreEngine",
    "ScoreResult",
    "Weights",
    "normalize_product",
]
__version__ = "0.3-beta"
