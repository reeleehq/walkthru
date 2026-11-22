"""The :class:`Capture` result and :func:`capture_passage`, which routes a source to its capturer.

A capture is a still image of a cited source plus the boxes of a quoted passage on it, and the
outcome of the check that those boxes really hold that passage. **Verification is part of the
result**: ``found`` is true only when the check passed, and ``rects`` is empty otherwise, so a
caller cannot draw a highlight over text that was not confirmed to be there. ``reason`` says why a
capture was not verified; ``verified_by`` says which check vouched for one that was.

Each capture also writes a JSON sidecar next to its PNG (``<out>.json``), so a capture made in one
process can be rendered in another (:meth:`Capture.load`).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union
from urllib.parse import urlparse

__all__ = [
    "Capture",
    "EvidenceDependencyError",
    "capture_passage",
    "is_pdf_source",
    "sniff_pdf_url",
    "missing_extra",
]

PathLike = Union[str, Path]


class EvidenceDependencyError(ImportError):
    """An optional dependency of :mod:`walkthru.evidence` is not installed."""


def missing_extra(package: str, extra: str, *, purpose: str) -> EvidenceDependencyError:
    """An actionable error naming the pip extra that installs ``package``."""
    return EvidenceDependencyError(
        f"{purpose} needs `{package}`, which is not installed. "
        f'Install it with: pip install "walkthru[{extra}]"'
    )


@dataclass(frozen=True)
class Capture:
    """A captured source image and the verified boxes of a quoted passage on it.

    Attributes:
        png: The captured image.
        size: ``(width, height)`` of ``png`` in pixels.
        rects: One box per line of the passage, ``(x0, y0, x1, y1)`` in ``png`` pixels, in
            reading order. **Empty unless** ``found``.
        found: The passage was located *and* verified.
        matched: The text the located boxes actually hold (``None`` when nothing was located).
        verified_by: Which check vouched for ``rects``: ``"dom"`` (browser range text plus a hit
            test at every box), ``"pdf-text"`` (the boxes of the very words that spell the quote
            in a born-digital text layer) or ``"ocr"`` (word boxes read from the pixels of a
            scanned page). ``None`` when not found.
        source: The source as given (URL or path).
        quote: The quote as given.
        reason: Why the capture is not verified — ``"no-quote"``, ``"not-in-text"``,
            ``"mismatch"``, ``"invisible"``, ``"off-screen"``, ``"occluded"``, ``"unstable"``,
            ``"ocr-mismatch"``, ``"ocr-failed"`` or ``"scanned-needs-ocr"``. ``None`` when found.
        page: PDF page index (0-based), ``None`` for web pages.
        nth: Which occurrence of the quote was used (0-based), when one was located.
        details: Diagnostics (occluded box count, OCR score, …); free-form.
    """

    png: Path
    size: tuple[int, int]
    rects: tuple[tuple[float, float, float, float], ...] = ()
    found: bool = False
    matched: Optional[str] = None
    verified_by: Optional[str] = None
    source: str = ""
    quote: Optional[str] = None
    reason: Optional[str] = None
    page: Optional[int] = None
    nth: Optional[int] = None
    details: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.rects and not self.found:
            raise ValueError("an unverified Capture must not carry rects")
        if self.found and not self.verified_by:
            raise ValueError("a found Capture must say what verified it")

    @property
    def sidecar(self) -> Path:
        """The JSON file this capture is saved to (next to ``png``)."""
        return self.png.with_suffix(".json")

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready dict (paths as strings, tuples as lists)."""
        d = asdict(self)
        d["png"] = str(self.png)
        d["size"] = list(self.size)
        d["rects"] = [list(r) for r in self.rects]
        return d

    def save(self) -> Path:
        """Write the JSON sidecar; returns its path."""
        self.sidecar.write_text(
            json.dumps(self.to_dict(), indent=1, ensure_ascii=False)
        )
        return self.sidecar

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Capture":
        """Inverse of :meth:`to_dict`."""
        d = dict(d)
        d["png"] = Path(d["png"])
        d["size"] = tuple(d["size"])
        d["rects"] = tuple(tuple(r) for r in d.get("rects") or ())
        return cls(**d)

    @classmethod
    def load(cls, path: PathLike) -> "Capture":
        """Load a capture from its JSON sidecar (or from its PNG's path)."""
        p = Path(path)
        if p.suffix.lower() != ".json":
            p = p.with_suffix(".json")
        return cls.from_dict(json.loads(p.read_text()))


#: Seconds a content sniff may take before the source is treated as not-a-PDF.
DEFAULT_SNIFF_TIMEOUT_S = 5.0


def sniff_pdf_url(url: str, *, timeout: float = DEFAULT_SNIFF_TIMEOUT_S) -> bool:
    """True when ``url`` serves a PDF: ``Content-Type: application/pdf`` or a ``%PDF-`` prefix.

    One small ranged GET (the first bytes only). Any network error or timeout is ``False``, so a
    flaky sniff falls back to the browser route rather than failing the capture.
    """
    import urllib.request

    request = urllib.request.Request(url, headers={"Range": "bytes=0-4"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ctype = (
                response.headers.get("Content-Type", "").split(";")[0].strip().lower()
            )
            if ctype == "application/pdf":
                return True
            return response.read(5) == b"%PDF-"
    except Exception:
        return False


def is_pdf_source(
    source: PathLike,
    *,
    sniff: Union[bool, Callable[[str], bool]] = False,
) -> bool:
    """True when ``source`` names a PDF: a ``.pdf`` path/URL, or a local file starting ``%PDF``.

    An http(s) URL whose path has no file extension (``/pdf/<id>`` style endpoints) is only
    resolved by asking the server: pass ``sniff=True`` (:func:`sniff_pdf_url`) or a
    ``url -> bool`` callable. Off by default, so this stays a pure, offline check.

    >>> is_pdf_source("https://example.org/paper.pdf?download=1")
    True
    >>> is_pdf_source("https://example.org/article")
    False
    """
    s = str(source)
    parsed = urlparse(s)
    if parsed.scheme in ("http", "https", "file"):
        if parsed.path.lower().endswith(".pdf"):
            return True
        if sniff and parsed.scheme != "file" and not Path(parsed.path).suffix:
            return (sniff_pdf_url if sniff is True else sniff)(s)
        return False
    p = Path(s)
    if p.suffix.lower() == ".pdf":
        return True
    try:
        with p.open("rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def capture_passage(
    source: PathLike,
    quote: Optional[str] = None,
    *,
    out: PathLike,
    nth: Optional[int] = None,
    kind: Optional[str] = None,
    sniff: Union[bool, Callable[[str], bool]] = True,
    **options: Any,
) -> Capture:
    """Capture ``source`` (web page or PDF) at ``quote``; returns a verified :class:`Capture`.

    Args:
        source: An ``http(s)://`` URL, a ``file://`` URL or local path to an HTML file, or a
            PDF (local path or URL ending in ``.pdf``).
        quote: The passage to locate. ``None`` captures the source without a passage
            (``found=False``, ``reason="no-quote"``).
        out: Where to write the image; ``.png`` is used whatever the suffix, and a ``.json``
            sidecar is written beside it.
        nth: Use exactly this occurrence of ``quote`` (0-based). By default each occurrence is
            tried in turn until one verifies.
        kind: ``"pdf"`` or ``"html"`` to say what ``source`` is; ``None`` (default) decides from
            the source: see :func:`is_pdf_source`.
        sniff: for an http(s) URL with no file extension, ask the server (a ranged GET) whether it
            serves a PDF, so ``/pdf/<id>`` endpoints route to the PDF capturer. ``False`` skips the
            request; a ``url -> bool`` callable replaces it. Ignored when ``kind`` is given.
        **options: Passed to :func:`~walkthru.evidence.html.capture_html` or
            :func:`~walkthru.evidence.pdf.capture_pdf` — e.g. ``page=`` to reuse one browser
            page across many captures, ``dismiss_labels=``, ``ocr=``.

    Rule for callers: highlight only ``capture.rects`` of a capture whose ``found`` is true.
    Otherwise show the page without a highlight, and say so.
    """
    if kind not in (None, "pdf", "html"):
        raise ValueError(f"kind must be 'pdf', 'html' or None, got {kind!r}")
    if kind == "pdf" or (kind is None and is_pdf_source(source, sniff=sniff)):
        from walkthru.evidence.pdf import capture_pdf

        return capture_pdf(source, quote, out=out, nth=nth, **options)
    from walkthru.evidence.html import capture_html

    return capture_html(source, quote, out=out, nth=nth, **options)
