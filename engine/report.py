"""Rendering testuale del risultato: score, componenti, perche' e additivi."""
from __future__ import annotations

from .models import ScoreResult

_LABELS = {
    "nutrition": "Nutrienti",
    "additives": "Additivi",
    "processing": "Lavorazione",
    "organic": "Biologico",
}
_LEVELS = {"critico": "!!", "attenzione": " !", "info": " i"}


def _bar(value: float | None, width: int = 20) -> str:
    if value is None:
        return "?" * width
    filled = int(round(width * value / 100.0))
    return "#" * filled + "." * (width - filled)


def _wrap(text: str, width: int = 66) -> list[str]:
    """Spezza le spiegazioni lunghe senza dipendere da textwrap nel formato."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


def render_report(result: ScoreResult) -> str:
    product = result.product
    lines: list[str] = []
    title = product.name or "(senza nome)"
    brand = f" — {product.brand}" if product.brand else ""
    lines.append(f"{product.barcode}  {title}{brand}")
    coverage = (
        ""
        if result.additive_coverage >= 1.0
        else f"  ·  copertura additivi {result.additive_coverage * 100:.0f}%"
    )
    lines.append(
        f"Score {result.score:>3}/100  ·  {result.grade.upper()}  ·  "
        f"confidenza {result.confidence * 100:.0f}%{coverage}"
    )
    lines.append("")

    for component in result.components:
        value = component.score
        shown = "  n/d" if value is None else f"{value:5.1f}"
        share = f"{component.weight * 100:>4.0f}%"
        lines.append(f"  {_LABELS.get(component.name, component.name):<11} {shown}  {share}  {_bar(value)}")

    if result.reasons:
        lines.append("")
        lines.append("Perché:")
        for reason in result.reasons:
            chunked = _wrap(reason)
            lines.append(f"  • {chunked[0]}")
            for extra in chunked[1:]:
                lines.append(f"    {extra}")

    lines.append("")
    if result.additives:
        lines.append(f"Additivi ({len(result.additives)}):")
        for hit in result.additives:
            tag = " (stima)" if hit.risk_estimated else ""
            dose = "" if hit.dose_percent is None else f" · dose ~{hit.dose_percent:g}%"
            lines.append(
                f"  {hit.e_number:<10} {hit.name[:30]:<30} rischio {hit.risk:5.1f}{tag}{dose}"
            )
            if hit.why:
                for chunk in _wrap(hit.why, width=72):
                    lines.append(f"             {chunk}")
    else:
        lines.append("Additivi: nessuno dichiarato")

    if result.alerts:
        lines.append("")
        lines.append("Allerte:")
        for alert in result.alerts:
            lines.append(f"  [{_LEVELS.get(alert.level, ' -')}] {alert.message}")

    return "\n".join(lines)
