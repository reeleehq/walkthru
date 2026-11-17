"""PDF passage capture: born-digital text layers, and scanned pages re-boxed by OCR.

The PDFs are generated here with PyMuPDF, so nothing is downloaded. The OCR seam is faked for the
logic; one test runs real Tesseract through ``ocracy`` when both are installed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")
PIL = pytest.importorskip("PIL.Image")

from walkthru.evidence import Capture, capture_passage, page_image_coverage  # noqa: E402

LINES = [
    "Requirements written late are expensive to change, and the",
    "recog-",
    "nition rate rose sharply once reviews moved earlier in the",
    "project. Nobody measured how much the reviews themselves cost.",
]
#: Where each line's baseline sits, in points. Wide spacing leaves blank bands between lines, so
#: a box shifted by half a line lands on paper instead of ink.
BASELINES = [200, 260, 320, 380]
QUOTE = "recognition rate rose sharply"


def _write_born_digital(path: Path, *, pages: int = 2) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=612, height=792)
        if i == pages - 1:  # the quote is on the last page
            for text, y in zip(LINES, BASELINES):
                page.insert_text((72, y), text, fontsize=16)
        else:
            page.insert_text((72, 200), "A title page with nothing quoted on it.", fontsize=16)
    doc.save(str(path))
    return path


def _write_scanned(path: Path, *, layer_shift: float = 30.0) -> Path:
    """A one-page 'scan': the born-digital page as an image, plus an invisible text layer that
    sits ``layer_shift`` points below the glyphs (as a poor OCR layer does)."""
    src = pymupdf.open(str(_write_born_digital(path.with_suffix(".src.pdf"), pages=1)))
    png = src[0].get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=png)
    for text, y in zip(LINES, BASELINES):
        page.insert_text((72, y + layer_shift), text, fontsize=16, render_mode=3)
    doc.save(str(path))
    return path


def _ink_fraction(cap: Capture) -> float:
    img = PIL.open(cap.png).convert("L")
    ink = total = 0
    for x0, y0, x1, y1 in cap.rects:
        px = img.crop((int(x0), int(y0), int(x1), int(y1))).tobytes()  # mode L: a byte per pixel
        total += len(px)
        ink += sum(1 for v in px if v < 128)
    return ink / max(1, total)


def test_born_digital_quote_across_a_hyphenated_line_break(tmp_path):
    pdf = _write_born_digital(tmp_path / "paper.pdf")
    cap = capture_passage(pdf, QUOTE, out=tmp_path / "shot")
    assert cap.found and cap.verified_by == "pdf-text" and cap.page == 1
    assert cap.matched == "recog- nition rate rose sharply"
    assert len(cap.rects) == 2, "one bar per line"
    W, H = cap.size
    assert abs(W / H - 16 / 9) < 0.01
    for x0, y0, x1, y1 in cap.rects:
        assert 0 <= x0 < x1 <= W and 0 <= y0 < y1 <= H
    assert _ink_fraction(cap) > 0.05
    assert Capture.load(cap.png) == cap


def test_absent_quote_and_no_quote(tmp_path):
    pdf = _write_born_digital(tmp_path / "paper.pdf")
    missing = capture_passage(pdf, "a sentence this paper never says", out=tmp_path / "a")
    assert not missing.found and missing.reason == "not-in-text" and missing.rects == ()
    assert missing.png.exists()
    plain = capture_passage(pdf, None, out=tmp_path / "b", page=0)
    assert not plain.found and plain.reason == "no-quote" and plain.page == 0


def test_nth_counts_occurrences_through_the_document(tmp_path):
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page().insert_text((72, 200), "the same phrase again", fontsize=14)
    pdf = tmp_path / "twice.pdf"
    doc.save(str(pdf))
    assert capture_passage(pdf, "same phrase", out=tmp_path / "a").page == 0
    second = capture_passage(pdf, "same phrase", out=tmp_path / "b", nth=1)
    assert second.found and second.page == 1 and second.nth == 1
    assert not capture_passage(pdf, "same phrase", out=tmp_path / "c", nth=2).found


def test_scanned_page_never_uses_the_text_layer_boxes(tmp_path):
    pdf = _write_scanned(tmp_path / "scan.pdf")
    assert page_image_coverage(pymupdf.open(str(pdf))[0]) > 0.9

    unverified = capture_passage(pdf, QUOTE, out=tmp_path / "a", ocr=None)
    assert not unverified.found and unverified.reason == "scanned-needs-ocr"

    seen = []

    def fake_ocr(png):
        seen.append(png)
        return [
            ("noise", (10, 10, 40, 30)),
            ("recognition", (100, 300, 260, 330)),
            ("rate", (270, 300, 330, 330)),
            ("rose", (100, 360, 160, 390)),
            ("sharply", (170, 360, 280, 390)),
        ]

    cap = capture_passage(pdf, QUOTE, out=tmp_path / "b", ocr=fake_ocr)
    assert seen == [cap.png], "OCR reads the rendered window, whose pixels the boxes refer to"
    assert cap.found and cap.verified_by == "ocr"
    assert cap.rects == ((100, 300, 330, 330), (100, 360, 280, 390))


def test_scanned_page_with_ocr_that_does_not_match_is_not_found(tmp_path):
    pdf = _write_scanned(tmp_path / "scan.pdf")
    cap = capture_passage(
        pdf, QUOTE, out=tmp_path / "shot", ocr=lambda png: [("lorem", (0, 0, 9, 9)), ("ipsum", (9, 0, 19, 9))]
    )
    assert not cap.found and cap.reason == "ocr-mismatch" and cap.rects == ()
    assert cap.details["ocr_score"] < 85


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_scanned_page_real_ocr_lands_on_the_glyphs(tmp_path):
    pytest.importorskip("ocracy")
    pdf = _write_scanned(tmp_path / "scan.pdf")
    cap = capture_passage(pdf, "rate rose sharply once reviews moved earlier", out=tmp_path / "shot")
    assert cap.found and cap.verified_by == "ocr", cap.details
    assert _ink_fraction(cap) > 0.05
    # The shifted text layer's boxes would have landed on blank paper:
    layer = capture_passage(
        pdf, "rate rose sharply once reviews moved earlier", out=tmp_path / "layer",
        scan_coverage=1.01,  # pretend it is born-digital, to see where the layer points
    )  # fmt: skip
    assert layer.verified_by == "pdf-text" and _ink_fraction(layer) < 0.02
