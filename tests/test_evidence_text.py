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
from walkthru.evidence.text import tokens_agree


def test_normalize_folds_typography_not_wording():
    assert normalize_text("“Don’t” — stop") == normalize_text('"Don\'t" - stop')
    assert normalize_text("ﬁnal­ly​ ok") == "finally ok"
    assert normalize_text("  Many\n\t spaces ") == "many spaces"
    assert normalize_text("cat") != normalize_text("cut")


def test_skeleton_drops_hyphens_but_keeps_word_boundaries():
    assert skeleton("self-driven") == skeleton("selfdriven")
    assert skeleton("rose \u2014 sharply") == skeleton("rose sharply")
    assert skeleton("the rapist") != skeleton("therapist")


def test_find_word_run():
    words = ["It", "was", "the", "recog-", "nition", "rate", "that", "rose."]
    ends = [False, False, False, True, False, False, False, True]
    assert find_word_run(words, "recognition rate", line_ends=ends) == (3, 5)
    assert find_word_run(words, "recognition rate") is None, "a hyphen mid-line is not a break"
    assert find_word_run(words, "rate that rose", line_ends=ends) == (5, 7)
    assert find_word_run(["self-", "driven", "cars"], "self-driven cars", line_ends=[True, False, False]) == (0, 2)
    assert find_word_run(words, "absent words") is None
    assert find_word_run(words, "") is None


def test_fuzzy_word_run_tolerates_ocr_noise():
    words = ["Some", "intro.", "Tbe", "quick", "brovvn", "fox", "jumps", "here"]
    first, last, score = fuzzy_word_run(words, "The quick brown fox jumps")
    assert (first, last) == (2, 6) and score > 80
    assert fuzzy_word_run(["lorem", "ipsum"], "The quick brown fox")[2] < 60


def test_tokens_agree_rejects_meaning_changes():
    assert tokens_agree("The qu1ck brovvn fox", "the quick brown fox")
    assert tokens_agree("recog nition rate", "recognition rate")
    assert not tokens_agree("the drug did reduce it", "the drug did not reduce it")
    assert not tokens_agree("rose by 21 %", "rose by 12 %")
    assert not tokens_agree("a b c extra", "a b c")


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


def _serve(routes):
    """A local HTTP server answering ``path -> (content_type, body)``; returns ``(server, base)``."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            ctype, body = routes[self.path]
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_is_pdf_source_sniffs_extensionless_urls():
    server, base = _serve(
        {
            "/pdf/123": ("application/pdf", b"%PDF-1.4 x"),
            "/blob/9": ("application/octet-stream", b"%PDF-1.4 x"),
            "/article": ("text/html", b"<p>hi</p>"),
        }
    )
    try:
        assert not is_pdf_source(f"{base}/pdf/123")  # off by default: stays offline
        assert is_pdf_source(f"{base}/pdf/123", sniff=True)
        assert is_pdf_source(f"{base}/blob/9", sniff=True)  # by %PDF- prefix
        assert not is_pdf_source(f"{base}/article", sniff=True)
        assert not is_pdf_source("http://127.0.0.1:9/x", sniff=True)  # unreachable: not a PDF
        assert is_pdf_source(f"{base}/anything", sniff=lambda url: True)
    finally:
        server.shutdown()


def test_capture_passage_routes_by_kind_and_sniff(monkeypatch, tmp_path):
    import pytest
    import walkthru.evidence.pdf as pdf_mod
    from walkthru.evidence import capture_passage

    seen = []
    monkeypatch.setattr(pdf_mod, "capture_pdf", lambda src, q, **kw: seen.append(src) or "pdf")
    assert capture_passage("http://x.test/pdf/1", "q", out=tmp_path / "o", kind="pdf") == "pdf"
    assert capture_passage("http://x.test/pdf/2", "q", out=tmp_path / "o", sniff=lambda u: True) == "pdf"
    assert seen == ["http://x.test/pdf/1", "http://x.test/pdf/2"]
    with pytest.raises(ValueError, match="kind"):
        capture_passage("x", "q", out=tmp_path / "o", kind="doc")
