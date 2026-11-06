"""
Tabella additivi: lettura del CSV, lookup per chiave/E-number e rischio per additivo.

La tabella e' la parte "compilabile": data/additives.csv contiene una riga per
additivo con la colonna `risk_score` (0-100). Finche' un additivo non e'
compilato, il motore usa un default per classe funzionale (documentato sotto) e
marca il risultato come stimato, cosi' il report resta onesto.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

DEFAULT_TABLE_PATH = Path(__file__).resolve().parent.parent / "data" / "additives.csv"

# Rischio provvisorio per classe funzionale, usato SOLO per additivi non compilati.
# Volutamente conservativo: il valore vero deve arrivare dalla tabella compilata.
DEFAULT_RISK_BY_CLASS: dict[str, float] = {
    "preservative": 40.0,
    "colour": 35.0,
    "color": 35.0,
    "sweetener": 45.0,
    "flavour enhancer": 40.0,
    "emulsifier": 30.0,
    "stabiliser": 25.0,
    "thickener": 20.0,
    "acidity regulator": 20.0,
    "antioxidant": 30.0,
    "anti caking agent": 25.0,
    "glazing agent": 20.0,
    "humectant": 20.0,
    "raising agent": 15.0,
    "gelling agent": 20.0,
    "packaging gas": 5.0,
    "propellant": 20.0,
    "sequestrant": 25.0,
    "bulking agent": 15.0,
    "firming agent": 15.0,
    "flour treatment agent": 20.0,
    "foaming agent": 15.0,
    "wax": 10.0,
}
FALLBACK_RISK = 30.0

_KEY_LANG = re.compile(r"^[a-z]{2}:")
# Open Food Facts usa varianti tipo en:e322i / en:e451i per lo stesso additivo.
# Le lettere (e150a, e150b...) sono invece additivi distinti: non si toccano.
_ROMAN_VARIANT = re.compile(r"^(e\d+)(i{1,3}|iv|v|vi{0,3}|ix|x)$", re.IGNORECASE)


def canonical(value: str) -> str:
    """'en:e322' | 'E322' | 'e 322' -> 'e322'."""
    token = value.strip().lower()
    token = _KEY_LANG.sub("", token)
    token = re.sub(r"[\s\-_]", "", token)
    return token


def _strip_roman_suffix(token: str) -> str:
    match = _ROMAN_VARIANT.match(token)
    return match.group(1) if match else token


@dataclass(frozen=True)
class AdditiveRow:
    e_number: str
    key: str
    name_en: str
    name_it: str
    classes: tuple[str, ...]
    is_preservative: bool
    risk: float | None
    why: str = ""
    basis: str = ""
    notes: str = ""

    @property
    def display_name(self) -> str:
        return self.name_it or self.name_en or self.e_number

    def default_risk(self) -> tuple[float, bool]:
        """(rischio, stimato). Gli additivi con `basis = classe` sono stime."""
        if self.risk is not None:
            return self.risk, self.basis.strip().lower().startswith("class")
        known = [DEFAULT_RISK_BY_CLASS[cls] for cls in self.classes if cls in DEFAULT_RISK_BY_CLASS]
        if known:
            return max(known), True
        return FALLBACK_RISK, True


class AdditiveTable:
    """Collezione di additivi indicizzata per chiave canonica e per nome."""

    def __init__(self, rows: Iterable[AdditiveRow], source: Path | None = None) -> None:
        self.source = source
        self._rows: dict[str, AdditiveRow] = {}
        self._by_name: dict[str, AdditiveRow] = {}
        # `_all` tiene una riga per E-number: `_rows` e' solo l'indice di lookup
        # (chiave OFF, E-number, nome) e conterebbe due volte le varianti.
        self._all: dict[str, AdditiveRow] = {}
        for row in rows:
            self._all.setdefault(canonical(row.e_number), row)
            self._rows[canonical(row.key) or canonical(row.e_number)] = row
            self._rows.setdefault(canonical(row.e_number), row)
            for name in (row.name_it, row.name_en):
                if name:
                    self._by_name.setdefault(name.strip().lower(), row)

    # ------------------------------------------------------------------ #
    # Costruzione
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Path | str | None = None) -> "AdditiveTable":
        table_path = Path(path) if path else DEFAULT_TABLE_PATH
        if not table_path.exists():
            raise FileNotFoundError(
                f"Tabella additivi non trovata: {table_path}\n"
                "Generala con: python tools/build_additive_table.py"
            )
        rows: list[AdditiveRow] = []
        with table_path.open(newline="", encoding="utf-8") as handle:
            for raw in csv.DictReader(handle):
                if not (raw.get("e_number") or "").strip():
                    continue
                classes = tuple(
                    cls.strip() for cls in (raw.get("classes") or "").split(";") if cls.strip()
                )
                rows.append(
                    AdditiveRow(
                        e_number=raw["e_number"].strip(),
                        key=(raw.get("key") or raw["e_number"]).strip(),
                        name_en=(raw.get("name_en") or "").strip(),
                        name_it=(raw.get("name_it") or "").strip(),
                        classes=classes,
                        is_preservative=(raw.get("is_preservative") or "").strip().lower()
                        in {"true", "1", "yes", "si", "sì"},
                        risk=_parse_risk(raw.get("risk_score")),
                        why=(raw.get("why") or "").strip(),
                        basis=(raw.get("basis") or "").strip(),
                        notes=(raw.get("notes") or "").strip(),
                    )
                )
        return cls(rows, source=table_path)

    # ------------------------------------------------------------------ #
    # Interrogazione
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._all)

    def __iter__(self) -> Iterator[AdditiveRow]:
        return iter(self._all.values())

    def get(self, token: str) -> AdditiveRow | None:
        """Risolve anche le varianti: 'en:e322i' -> riga E322."""
        key = canonical(token)
        row = self._rows.get(key)
        if row is not None:
            return row
        base = _strip_roman_suffix(key)
        return self._rows.get(base) if base != key else None

    def get_by_name(self, name: str) -> AdditiveRow | None:
        return self._by_name.get(name.strip().lower())

    def preserve_only(self) -> list[AdditiveRow]:
        return [row for row in self._all.values() if row.is_preservative]

    def unscored(self) -> list[AdditiveRow]:
        return [row for row in self._all.values() if row.risk is None]

    def stats(self) -> dict[str, int]:
        rows = list(self._all.values())
        return {
            "total": len(rows),
            "preservatives": sum(1 for row in rows if row.is_preservative),
            "scored": sum(1 for row in rows if row.risk is not None),
            "unscored": sum(1 for row in rows if row.risk is None),
        }


def _parse_risk(raw: str | None) -> float | None:
    text = (raw or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return max(0.0, min(100.0, value))
