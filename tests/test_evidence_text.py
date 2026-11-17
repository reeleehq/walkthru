"""The pure helpers behind passage capture: normalisation, word runs, line merging, routing."""

from __future__ import annotations

from walkthru.evidence import (
    find_word_run,
    fuzzy_word_run,
    is_pdf_source,
    merge_line_boxes,
    normalize_text,
    skeleton,
)


def test_normalize_folds_typography_not_wording():
    assert normalize_text("“Don’t” — stop") == normalize_text('"Don\'t" - stop')
    assert normalize_text("ﬁnal­ly​ ok") == "finally ok"
    assert normalize_text("  Many\n\t spaces ") == "many spaces"
    assert normalize_text("cat") != normalize_text("cut")


def test_skeleton_ignores_line_break_hyphens():
    assert skeleton("recog-\nnition") == skeleton("recognition")
    assert skeleton("self-driven") == skeleton("self driven") == skeleton("selfdriven")


def test_find_word_run():
    words = ["It", "was", "the", "recog-", "nition", "rate", "that", "rose."]
    assert find_word_run(words, "recognition rate") == (3, 5)
    assert find_word_run(words, "rate that rose") == (5, 7)
    assert find_word_run(words, "absent words") is None
    assert find_word_run(words, "") is None


def test_fuzzy_word_run_tolerates_ocr_noise():
    words = ["Some", "intro.", "Tbe", "quick", "brovvn", "fox", "jumps", "here"]
    first, last, score = fuzzy_word_run(words, "The quick brown fox jumps")
    assert (first, last) == (2, 6) and score > 80
    assert fuzzy_word_run(["lorem", "ipsum"], "The quick brown fox")[2] < 60


def test_merge_line_boxes_keeps_columns_apart_and_orders_by_line():
    boxes = [(10, 50, 40, 60), (300, 10, 340, 20), (10, 10, 50, 20), (52, 11, 90, 20)]
    assert merge_line_boxes(boxes) == [
        (10.0, 10.0, 90.0, 20.0),
        (300.0, 10.0, 340.0, 20.0),
        (10.0, 50.0, 40.0, 60.0),
    ]


def test_is_pdf_source(tmp_path):
    assert is_pdf_source("https://example.org/x.pdf")
    assert is_pdf_source("file:///tmp/paper.PDF")
    assert not is_pdf_source("https://example.org/x.html")
    blob = tmp_path / "download"
    blob.write_bytes(b"%PDF-1.7\n...")
    assert is_pdf_source(blob)
    page = tmp_path / "page.html"
    page.write_text("<p>hi</p>")
    assert not is_pdf_source(page)
