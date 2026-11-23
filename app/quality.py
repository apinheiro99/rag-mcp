"""Quality gate do corpus antes de chunking/embedding."""
from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

from .extraction import ExtractedDoc

_MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "â€™", "â€œ", "â€", "ï¿½", "�")


class QualityGateError(Exception):
    pass


@dataclass(frozen=True)
class QualityReport:
    status: str
    total_chars: int
    segments: int
    control_chars: int
    control_char_ratio: float
    replacement_chars: int
    replacement_char_ratio: float
    mojibake_markers: int
    mojibake_marker_ratio: float
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _control_count(text: str) -> int:
    return sum(
        1 for ch in text
        if unicodedata.category(ch) == "Cc" and ch not in "\n\r\t"
    )


def analyze_document_quality(extracted: ExtractedDoc, config: dict[str, Any]) -> QualityReport:
    text = extracted.full_text()
    total = len(text)
    control = _control_count(text)
    replacement = text.count("\ufffd")
    mojibake = sum(text.count(marker) for marker in _MOJIBAKE_MARKERS)

    denom = max(1, total)
    control_ratio = control / denom
    replacement_ratio = replacement / denom
    mojibake_ratio = mojibake / denom

    reasons: list[str] = []
    bad = False
    warn = False

    min_chars = int(config.get("min_text_chars", 50))
    max_control = float(config.get("max_control_char_ratio", 0.01))
    max_replacement = float(config.get("max_replacement_char_ratio", 0.002))
    max_mojibake = float(config.get("max_mojibake_marker_ratio", 0.003))
    warning_factor = float(config.get("warning_ratio_factor", 0.5))

    if total < min_chars:
        bad = True
        reasons.append(f"texto extraído muito curto: {total} < {min_chars} caracteres")

    checks = (
        ("caracteres de controle", control_ratio, max_control),
        ("caracteres de substituição", replacement_ratio, max_replacement),
        ("marcadores de mojibake", mojibake_ratio, max_mojibake),
    )
    for label, ratio, limit in checks:
        if ratio > limit:
            bad = True
            reasons.append(f"{label}: {ratio:.4%} > limite {limit:.4%}")
        elif ratio > limit * warning_factor and ratio > 0:
            warn = True
            reasons.append(f"{label}: {ratio:.4%} próximo do limite {limit:.4%}")

    status = "bad" if bad else "warning" if warn else "good"
    return QualityReport(
        status=status,
        total_chars=total,
        segments=len(extracted.segments),
        control_chars=control,
        control_char_ratio=round(control_ratio, 8),
        replacement_chars=replacement,
        replacement_char_ratio=round(replacement_ratio, 8),
        mojibake_markers=mojibake,
        mojibake_marker_ratio=round(mojibake_ratio, 8),
        reasons=reasons,
    )
