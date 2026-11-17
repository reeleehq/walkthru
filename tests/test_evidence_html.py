"""Web-page passage capture against local HTML fixtures, in a real headless browser.

Skipped cleanly when Playwright or its Chromium build is not installed. Every page is loaded over
``file://``, so the suite stays offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from walkthru.evidence import Capture, capture_passage

FIXTURES = Path(__file__).parent / "fixtures" / "evidence"
PIL = pytest.importorskip("PIL.Image")


@pytest.fixture(scope="module")
def page():
    pytest.importorskip("playwright.sync_api")
    from walkthru.evidence import open_capture_page

    try:
        cm = open_capture_page(viewport=(1000, 600), device_scale_factor=1.0)
        p = cm.__enter__()
    except Exception as e:  # browsers not installed (`playwright install chromium`)
        pytest.skip(f"no Playwright browser available: {e}")
    yield p
    cm.__exit__(None, None, None)


def _capture(page, name, quote, tmp_path, **kw) -> Capture:
    kw.setdefault("settle_ms", 0)
    kw.setdefault("scroll_settle_ms", 50)
    return capture_passage(FIXTURES / name, quote, out=tmp_path / "shot", page=page, **kw)


def _ink_fraction(cap: Capture, *, dark_text: bool = True) -> float:
    """Fraction of the pixels inside the capture's rects that are glyph ink."""
    img = PIL.open(cap.png).convert("L")
    ink = total = 0
    for x0, y0, x1, y1 in cap.rects:
        region = img.crop((int(x0), int(y0), int(x1), int(y1)))
        px = region.tobytes()  # mode L: a byte per pixel
        total += len(px)
        ink += sum(1 for v in px if (v < 128 if dark_text else v > 128))
    return ink / max(1, total)


def test_quote_crossing_inline_tags_is_found_and_verified(page, tmp_path):
    cap = _capture(page, "article.html", "found that most defects arise early in design", tmp_path)
    assert cap.found and cap.verified_by == "dom" and cap.reason is None
    assert cap.matched == "found that most defects arise early in design"
    assert cap.size == (1000, 600)
    assert cap.rects, "a verified capture carries the passage's boxes"
    for x0, y0, x1, y1 in cap.rects:  # on screen, roughly centred vertically by the scroll
        assert 0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 600
    assert 150 < cap.rects[0][1] < 450
    assert _ink_fraction(cap) > 0.03, "the boxes must sit over the glyphs"
    assert Capture.load(cap.sidecar) == cap


def test_curly_quotes_dashes_and_ligatures_fold(page, tmp_path):
    cap = _capture(page, "article.html", 'He called it "a tiny problem" - and moved on', tmp_path)
    assert cap.found, cap.reason
    assert "“a tiny problem”" in cap.matched
    lig = _capture(page, "article.html", "The final word", tmp_path)
    assert lig.found and lig.matched == "The ﬁnal word"


def test_an_occurrence_that_fails_verification_is_skipped_for_the_next(page, tmp_path):
    quote = "An echo of the phrase only ever reached this"
    cap = _capture(page, "article.html", quote, tmp_path)
    assert cap.found and cap.nth == 1  # occurrence 0 sits in a zero-height clipped box
    pinned = _capture(page, "article.html", quote, tmp_path, nth=0)
    assert not pinned.found and pinned.rects == ()


def test_a_range_spanning_hidden_text_is_not_verified(page, tmp_path):
    # The visible words spell the quote, but the DOM range over them also holds hidden text:
    # range.toString() differs from the quote, so the capture refuses to vouch for the boxes.
    cap = _capture(page, "article.html", "Total cost: forty units per seat", tmp_path)
    assert not cap.found and cap.reason == "mismatch"
    assert "REDACTED" in cap.matched


def test_quote_inside_a_scrolling_panel_is_scrolled_into_view(page, tmp_path):
    cap = _capture(page, "article.html", "the sentence nobody scrolls to", tmp_path)
    assert cap.found, cap.reason


def test_absent_quote_reports_not_found_without_boxes(page, tmp_path):
    cap = _capture(page, "article.html", "words that appear nowhere on the page", tmp_path)
    assert not cap.found and cap.reason == "not-in-text" and cap.rects == ()
    assert cap.png.exists(), "the page is still captured, to show without a highlight"


def test_no_quote_captures_the_page(page, tmp_path):
    cap = _capture(page, "article.html", None, tmp_path)
    assert not cap.found and cap.reason == "no-quote" and cap.png.exists()


def test_a_bar_drawn_over_the_quote_is_reported_as_occluded(page, tmp_path):
    cap = _capture(page, "overlay.html", "This sentence sits under a promotional banner", tmp_path)
    assert not cap.found
    assert cap.reason == "occluded"
    assert cap.rects == ()
    assert cap.details["occluded"] >= 1


def test_dark_theme_page_is_captured(page, tmp_path):
    cap = _capture(page, "dark.html", "light text on a near-black page", tmp_path)
    assert cap.found, cap.reason
    assert _ink_fraction(cap, dark_text=False) > 0.03


def test_consent_dialog_is_dismissed(page, tmp_path):
    cap = _capture(
        page, "consent.html", "the passage we came for", tmp_path, hide_overlays=False
    )
    assert cap.details["dismissed"] == "Accept all"
    assert cap.found, cap.reason


def test_consent_dialog_left_up_is_caught_by_the_hit_test(page, tmp_path):
    cap = _capture(
        page, "consent.html", "the passage we came for", tmp_path,
        hide_overlays=False, dismiss_labels=(),
    )  # fmt: skip
    assert not cap.found and cap.reason == "occluded"


def test_rects_are_in_device_pixels(page, tmp_path):
    ctx = page.context.browser.new_context(
        viewport={"width": 800, "height": 500}, device_scale_factor=2.0
    )
    try:
        cap = _capture(ctx.new_page(), "article.html", "most defects arise early", tmp_path)
    finally:
        ctx.close()
    assert cap.found and cap.size == (1600, 1000)
    assert _ink_fraction(cap) > 0.03
