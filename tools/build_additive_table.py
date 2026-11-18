"""
Build the additive table from Open Food Facts.

Downloads the official Open Food Facts additive taxonomy and produces:

  data/additives.csv      -> ALL additives (E-number, EN/IT name, classes, risk to fill)
  data/preservatives.csv  -> preservatives only (class "preservative")

The `risk_score` column is deliberately fillable by hand (0 = harmless,
100 = maximum toxicity). The scoring engine reads these files at runtime.

Usage:
    python tools/build_additive_table.py
    python tools/build_additive_table.py --input /tmp/additives.json   # offline
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data.curation import curated as curated_entry  # noqa: E402
from data.curation import why_for_class  # noqa: E402
from engine.additives import DEFAULT_RISK_BY_CLASS, FALLBACK_RISK  # noqa: E402

TAXONOMY_URL = "https://static.openfoodfacts.org/data/taxonomies/additives.json"
USER_AGENT = "generic_01-foodscore/0.3-beta (+https://github.com/tommasocerisola-web)"
FIELDNAMES = [
    "e_number",
    "key",
    "name_en",
    "name_it",
    "classes",
    "is_preservative",
    "risk_score",
    "basis",
    "why",
    "notes",
]

_LANG_PREFIX = re.compile(r"^[a-z]{2}:")
_E_PREFIX = re.compile(r"^E\d+[a-z]{0,3}\s*[-–]\s*", re.IGNORECASE)
_SUFFIX = re.compile(r"^(e\d+)([a-z]{1,3})$")


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def download(url: str = TAXONOMY_URL, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (constant URL)
        return json.load(resp)


# --------------------------------------------------------------------------- #
# Taxonomy parsing
# --------------------------------------------------------------------------- #
def clean_class(raw: str) -> str:
    """'en:anti-caking-agent' -> 'anti caking agent'."""
    raw = _LANG_PREFIX.sub("", raw.strip())
    return raw.replace("-", " ").strip()


def parse_classes(entry: dict) -> list[str]:
    """Extract the functional classes (preservative, colour, ...)."""
    found: list[str] = []

    def push(block) -> None:
        if isinstance(block, str):
            for chunk in block.split(","):
                if chunk.strip():
                    found.append(clean_class(chunk))
        elif isinstance(block, dict):
            # {"en": "en:acidity-regulator, en:acid"} or {"it": "Conservante"}
            for value in block.values():
                push(value)
        elif isinstance(block, list):
            for value in block:
                push(value)

    push(entry.get("mandatory_additive_class"))
    push(entry.get("additives_classes"))
    push(entry.get("additives_class"))

    seen: list[str] = []
    for name in found:
        if name and name not in seen:
            seen.append(name)
    return seen


def parse_e_number(key: str, entry: dict) -> str | None:
    """Build 'E322', 'E341iii' ... from e_number or from the key."""
    raw = (entry.get("e_number") or {}).get("en")
    if isinstance(raw, str) and raw.strip():
        token = raw.strip().split()[0]
        token = re.sub(r"[^0-9A-Za-z]", "", token)
        if token[:1].isdigit():
            token = "E" + token
        return token if len(token) > 1 else None

    short = _LANG_PREFIX.sub("", key)
    match = _SUFFIX.match(short)
    if match:
        return "E" + match.group(1)[1:] + match.group(2)
    match = re.match(r"^e(\d+[a-z]{0,3})$", short)
    return "E" + match.group(1) if match else None


def _name(entry: dict, lang: str) -> str:
    names = entry.get("name") or {}
    value = names.get(lang) or ""
    return _E_PREFIX.sub("", str(value)).strip()


def in_preservative_family(e_number: str) -> bool:
    """Under the EU scheme the E2xx family is the preservatives."""
    digits = re.sub(r"\D", "", e_number)[:3]
    return len(digits) == 3 and 200 <= int(digits) <= 299


def sort_key(row: dict) -> tuple[int, str]:
    digits = re.sub(r"\D", "", row["e_number"])[:4]
    return (int(digits) if digits else 10**6, row["e_number"].lower())


def build_rows(taxonomy: dict) -> list[dict]:
    rows: list[dict] = []
    for key, entry in taxonomy.items():
        if not isinstance(entry, dict):
            continue
        e_number = parse_e_number(key, entry)
        if not e_number:
            continue
        # Entries without a functional class stay in the table: an additive with
        # an empty class beats an additive invisible to the scoring.
        classes = parse_classes(entry)
        # OFF does not classify every E2xx: when the class is missing entirely,
        # the E2xx family (preservatives under the EU scheme) is the only
        # information available and we use it. When a class is there, the
        # taxonomy wins (e.g. E260 = acidity regulator, not preservative).
        if not classes and in_preservative_family(e_number):
            classes = ["preservative"]

        # Risk and explanation: curated by hand when the additive is known,
        # otherwise estimated from the functional class (basis = "class").
        curated = curated_entry(e_number)
        if curated is not None:
            risk, why = curated
            basis = "curated"
        else:
            known = [DEFAULT_RISK_BY_CLASS[c] for c in classes if c in DEFAULT_RISK_BY_CLASS]
            risk = max(known) if known else FALLBACK_RISK
            basis = "class"
            why = why_for_class(classes)

        rows.append(
            {
                "e_number": e_number,
                "key": key,
                "name_en": _name(entry, "en"),
                "name_it": _name(entry, "it"),
                "classes": ";".join(classes),
                "is_preservative": "true" if "preservative" in classes else "false",
                "risk_score": str(int(round(risk))),
                "basis": basis,
                "why": why,
                "notes": "",
            }
        )

    # Dedup by e_number. The taxonomy has "base" entries (en:e322) and variants
    # (en:e322i): the base entry must win, otherwise E322 would end up with the
    # name/classes of the variant.
    def is_base(row: dict) -> bool:
        return row["key"].lower() == "en:" + row["e_number"].lower()

    unique: dict[str, dict] = {}
    for row in rows:
        slot = row["e_number"].lower()
        existing = unique.get(slot)
        if existing is None or (is_base(row) and not is_base(existing)):
            if existing is not None:
                # keep the classes already collected from the variant
                merged_classes = [c for c in existing["classes"].split(";") if c]
                for cls in row["classes"].split(";"):
                    if cls and cls not in merged_classes:
                        merged_classes.append(cls)
                row = dict(row, classes=";".join(merged_classes))
                if existing["is_preservative"] == "true":
                    row["is_preservative"] = "true"
            unique[slot] = row
        else:
            if not existing["name_it"] and row["name_it"]:
                existing["name_it"] = row["name_it"]
            if not existing["name_en"] and row["name_en"]:
                existing["name_en"] = row["name_en"]
            merged = existing["classes"].split(";") if existing["classes"] else []
            for cls in row["classes"].split(";"):
                if cls and cls not in merged:
                    merged.append(cls)
            existing["classes"] = ";".join(merged)
            if row["is_preservative"] == "true":
                existing["is_preservative"] = "true"
    return sorted(unique.values(), key=sort_key)


def load_existing(path: Path) -> dict[str, dict]:
    """Rows already filled: hand-written scores must never be lost."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            (row.get("e_number") or "").strip().lower(): row
            for row in csv.DictReader(handle)
            if (row.get("e_number") or "").strip()
        }


def preserve_edits(rows: list[dict], existing: dict[str, dict]) -> int:
    kept = 0
    for row in rows:
        prev = existing.get(row["e_number"].lower())
        if not prev:
            continue
        if (prev.get("risk_score") or "").strip():
            row["risk_score"] = prev["risk_score"].strip()
            kept += 1
        if (prev.get("notes") or "").strip():
            row["notes"] = prev["notes"].strip()
        if (prev.get("why") or "").strip():
            row["why"] = prev["why"].strip()
    return kept


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build data/additives.csv from Open Food Facts")
    parser.add_argument("--input", type=Path, default=None, help="Already downloaded taxonomy JSON")
    parser.add_argument("--out", type=Path, default=Path("data"), help="Output folder")
    parser.add_argument("--url", default=TAXONOMY_URL)
    parser.add_argument(
        "--no-preserve",
        action="store_true",
        help="Rewrite risk_score/why from the curation instead of keeping existing values",
    )
    args = parser.parse_args(argv)

    if args.input:
        taxonomy = json.loads(args.input.read_text(encoding="utf-8"))
        origine = str(args.input)
    else:
        print(f"Downloading taxonomy: {args.url}", file=sys.stderr)
        taxonomy = download(args.url)
        origine = args.url

    rows = build_rows(taxonomy)
    kept = 0
    if not args.no_preserve:
        kept = preserve_edits(rows, load_existing(args.out / "additives.csv"))
        if kept:
            print(f"Kept {kept} already filled scores", file=sys.stderr)
    preservatives = [row for row in rows if row["is_preservative"] == "true"]

    write_csv(args.out / "additives.csv", rows)
    write_csv(args.out / "preservatives.csv", preservatives)

    stamp = _dt.date.today().isoformat()
    meta = {
        "generated_at": stamp,
        "source": origine,
        "additives": len(rows),
        "preservatives": len(preservatives),
        "unscored": sum(1 for row in rows if not row["risk_score"]),
        "curated": sum(1 for row in rows if row["basis"] == "curated"),
        "class_based": sum(1 for row in rows if row["basis"] == "class"),
    }
    (args.out / "additives.meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(
        f"OK: {len(rows)} additives ({len(preservatives)} preservatives) -> {args.out}/additives.csv",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
