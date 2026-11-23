import pytest

from app.extraction import ExtractedDoc, Segment
from app.quality import analyze_document_quality


QUALITY_CFG = {
    "min_text_chars": 50,
    "max_control_char_ratio": 0.01,
    "max_replacement_char_ratio": 0.002,
    "max_mojibake_marker_ratio": 0.003,
    "warning_ratio_factor": 0.5,
}


def test_quality_report_accepts_normal_text():
    doc = ExtractedDoc([
        Segment("A clean paragraph about software design. " * 20, {"page": 1})
    ])

    report = analyze_document_quality(doc, QUALITY_CFG)

    assert report.status == "good"
    assert report.control_chars == 0
    assert report.reasons == []


def test_quality_report_rejects_control_character_pollution():
    text = ("normal words " * 30) + ("\x03garbage\x14" * 20)
    doc = ExtractedDoc([Segment(text, {"page": 1})])

    report = analyze_document_quality(doc, QUALITY_CFG)

    assert report.status == "bad"
    assert report.control_chars > 0
    assert any("caracteres de controle" in reason for reason in report.reasons)


def test_quality_report_rejects_too_little_text():
    report = analyze_document_quality(
        ExtractedDoc([Segment("tiny", {"page": 1})]),
        QUALITY_CFG,
    )

    assert report.status == "bad"
    assert any("muito curto" in reason for reason in report.reasons)
