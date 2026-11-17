"""Text normalisation and box geometry for passage capture — pure Python, no vendors.

One normaliser serves both sides of a comparison: the quote a caller wrote and the text a page or
PDF actually holds. It folds the differences that are typography, not wording (curly vs straight
quotes, en/em dashes, ligatures, soft hyphens, runs of whitespace, case), and it works **one
character at a time**, so the browser-side twin (in :mod:`walkthru.evidence.html`) can keep a map
from every *output* character back to the source character it came from. NFKC can turn one
character into several (``"ﬁ"`` → ``"fi"``); a per-output-character map is what lets a match that
starts or ends inside such an expansion still resolve to a real DOM offset.

The fold table :data:`FOLDS` and the drop set :data:`DROP` are the single source of truth: the
browser receives them as arguments rather than keeping its own copy.

>>> normalize_text("“Don’t”  stop—ever")
'"don\\'t" stop-ever'
>>> normalize_text("ﬁnal\\u00adly")
'finally'
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, Optional, Sequence

__all__ = [
    "FOLDS",
    "DROP",
    "Box",
    "normalize_text",
    "skeleton",
    "merge_line_boxes",
    "find_word_run",
    "fuzzy_word_run",
    "similarity",
]

#: A box in image pixels: ``(x0, y0, x1, y1)``.
Box = tuple[float, float, float, float]

#: Typographic variants folded to one ASCII form before comparing.
FOLDS: dict[str, str] = {
    "‘": "'",  # left single quotation mark
    "’": "'",  # right single quotation mark / apostrophe
    "‚": "'",  # single low-9 quotation mark
    "‛": "'",  # single high-reversed-9 quotation mark
    "′": "'",  # prime
    "“": '"',  # left double quotation mark
    "”": '"',  # right double quotation mark
    "„": '"',  # double low-9 quotation mark
    "″": '"',  # double prime
    "«": '"',  # left guillemet
    "»": '"',  # right guillemet
    "‐": "-",  # hyphen
    "‑": "-",  # non-breaking hyphen
    "‒": "-",  # figure dash
    "–": "-",  # en dash
    "—": "-",  # em dash
    "−": "-",  # minus sign
}

#: Characters that carry no wording and are dropped: soft hyphen, zero-width space/joiners, BOM.
DROP: frozenset[str] = frozenset({"­", "​", "‌", "‍", "⁠", "﻿"})

_WS = re.compile(r"\s+")


def _fold_char(ch: str) -> str:
    """Normalise one source character (it may become zero, one or several characters)."""
    out = []
    for c in unicodedata.normalize("NFKC", ch):
        if c in DROP:
            continue
        out.append(FOLDS.get(c, c))
    return "".join(out).lower()


def normalize_text(text: str) -> str:
    """Fold typography, collapse whitespace and lowercase — the form quotes are compared in.

    >>> normalize_text("  A  quick\\n\\tfox ")
    'a quick fox'
    """
    return _WS.sub(" ", "".join(_fold_char(c) for c in text)).strip()


def skeleton(text: str) -> str:
    """:func:`normalize_text` with all whitespace and hyphens removed.

    PDFs break words across lines with a hyphen (``"recog-\\nnition"``) and a text layer may or may
    not keep it; comparing skeletons makes ``"recognition"``, ``"recog- nition"`` and
    ``"self-driven"``/``"selfdriven"`` equal, while every letter still has to match.

    >>> skeleton("recog-\\nnition") == skeleton("recognition")
    True
    """
    return normalize_text(text).replace(" ", "").replace("-", "")


def merge_line_boxes(
    boxes: Iterable[Sequence[float]], *, gap: float = 1.5
) -> list[Box]:
    """Merge the boxes that sit on one line into one bar per line, in reading order.

    A DOM range reports a box per text fragment (and, for a fully selected element, that
    element's box too); a PDF reports one per word. Two boxes share a line when they overlap
    vertically by more than half the smaller height and the horizontal gap between them is at
    most ``gap`` line-heights — so the two columns of a two-column layout stay apart.

    >>> merge_line_boxes([(0, 0, 10, 10), (12, 1, 30, 10), (0, 20, 8, 30)])
    [(0.0, 0.0, 30.0, 10.0), (0.0, 20.0, 8.0, 30.0)]
    """
    rows: list[list[float]] = []
    for b in sorted((tuple(map(float, b)) for b in boxes), key=lambda b: (b[1], b[0])):
        x0, y0, x1, y1 = b
        h = y1 - y0
        for row in rows:
            rh = row[3] - row[1]
            overlap = min(row[3], y1) - max(row[1], y0)
            near = x0 - row[2] <= gap * max(h, rh) and row[0] - x1 <= gap * max(h, rh)
            if overlap > 0.5 * min(h, rh) and near:
                row[0], row[1] = min(row[0], x0), min(row[1], y0)
                row[2], row[3] = max(row[2], x1), max(row[3], y1)
                break
        else:
            rows.append([x0, y0, x1, y1])
    rows.sort(key=lambda r: (r[1], r[0]))
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def find_word_run(
    words: Sequence[str], quote: str, *, nth: int = 0
) -> Optional[tuple[int, int]]:
    """Index range ``(first, last)`` of the words that spell ``quote`` (its ``nth`` occurrence).

    Matching is on :func:`skeleton` s, so line-break hyphenation and spacing never prevent a
    match, while every letter must. Returns ``None`` when there are fewer than ``nth + 1``
    occurrences.

    >>> find_word_run(["The", "recog-", "nition", "rate", "rose"], "recognition rate")
    (1, 3)
    >>> find_word_run(["a", "b", "a", "b"], "a b", nth=1)
    (2, 3)
    >>> find_word_run(["a", "b"], "c") is None
    True
    """
    q = skeleton(quote)
    if not q:
        return None
    flat, owner = [], []
    for i, w in enumerate(words):
        s = skeleton(w)
        flat.append(s)
        owner.extend([i] * len(s))
    text = "".join(flat)
    at = -1
    for _ in range(nth + 1):
        at = text.find(q, at + 1)
        if at < 0:
            return None
    return owner[at], owner[at + len(q) - 1]


def similarity(a: str, b: str) -> float:
    """String similarity in ``[0, 100]`` — ``rapidfuzz`` when installed, else :mod:`difflib`.

    >>> similarity("abc", "abc")
    100.0
    """
    try:
        from rapidfuzz.fuzz import ratio
    except ImportError:  # pragma: no cover - exercised where rapidfuzz is absent
        return 100.0 * SequenceMatcher(None, a, b).ratio()
    return float(ratio(a, b))


def fuzzy_word_run(
    words: Sequence[str], quote: str, *, slack: int = 2
) -> Optional[tuple[int, int, float]]:
    """Best ``(first, last, score)`` window of ``words`` matching ``quote``, scored 0..100.

    For OCR output, where a letter or a word break may be misread: every window whose length is
    within ``slack`` words of the quote's is scored by :func:`similarity` on normalised text, and
    the best one wins. The caller decides what score is good enough.

    >>> fuzzy_word_run(["noise", "the", "qu1ck", "brown", "fox", "end"], "the quick brown fox")[:2]
    (1, 4)
    """
    q = normalize_text(quote)
    n = len(q.split())
    if not n or not words:
        return None
    norm = [normalize_text(w) for w in words]
    best: Optional[tuple[int, int, float]] = None
    for length in range(max(1, n - slack), n + slack + 1):
        for i in range(0, len(norm) - length + 1):
            score = similarity(" ".join(norm[i : i + length]), q)
            if best is None or score > best[2]:
                best = (i, i + length - 1, score)
    return best
