"""Capture a web page at a quoted passage with Playwright, and verify the passage's boxes.

The DOM already knows where its text is, so no OCR is involved. The procedure, in order:

1. **Load** the page (tolerating client-side redirects), dismiss a consent dialog by clicking a
   button whose accessible name is one of ``dismiss_labels`` (in every frame, iframes included),
   and hide large fixed/sticky overlays.
2. **Locate.** Walk the visible text nodes (``checkVisibility()``), building a normalised text with
   one map entry per *output* character back to ``(node, offset)``; find the quote in it; build a
   ``Range`` over it; centre it in whichever element scrolls (falling back to the window).
3. **Verify** — the part that makes a highlight safe to draw:

   * ``range.toString()``, normalised, must spell the quote (spacing aside);
   * no text node in the range may hold characters that draw nothing (zero size, transparent);
   * every box of the passage (one per line fragment of each text node in the range) must lie
     inside the viewport;
   * a caret hit test (``caretPositionFromPoint``) at six points in each box must land in that
     very text node, inside the passage: not in a cookie banner or sticky bar drawn over it (even
     one nested in the quote's own element), and not in an ancestor, which is what a point over
     text clipped away by ``overflow: hidden`` returns;
   * the passage must still verify, with the same boxes, after the screenshot.

   Occurrences are tried in order until one passes (or only ``nth``, when given).

The page is a seam: pass ``page=`` (a Playwright *sync* ``Page``) to reuse one browser across many
captures, or let :func:`open_capture_page` open one. Playwright is only imported when a page has
to be opened.
"""

from __future__ import annotations

import struct
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator, Optional, Sequence, Union
from urllib.parse import urlparse

from walkthru.evidence.capture import Capture, missing_extra
from walkthru.evidence.text import DROP, FOLDS, merge_line_boxes, normalize_text

__all__ = [
    "DEFAULT_DISMISS_LABELS",
    "DEFAULT_USER_AGENT",
    "PASSAGE_LOCATE_JS",
    "PASSAGE_VERIFY_JS",
    "capture_html",
    "judge_passage",
    "locate_args",
    "open_capture_page",
]

#: Button names that close a consent or newsletter dialog, tried in this order.
DEFAULT_DISMISS_LABELS: tuple[str, ...] = (
    "Consent",
    "Accept all",
    "Accept All",
    "Accept all cookies",
    "Accept",
    "I agree",
    "I Accept",
    "Agree",
    "Got it",
    "OK",
    "Allow all",
    "Reject all",
    "Reject All",
    "Decline",
    "Close",
    "No thanks",
    "Dismiss",
)

#: A desktop browser UA: some sites serve a stripped page to an obviously headless client.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

#: Browser-side locator (argument: :func:`locate_args`). Finds the ``nth`` occurrence of the quote
#: among visible text nodes, stores a ``Range`` over it under ``key`` and (if ``scroll``) centres
#: it. Block boundaries and ``<br>`` count as spaces, so words in adjacent paragraphs never fuse.
#: Engine-agnostic: evaluate it on any page you drive, sync or async.
PASSAGE_LOCATE_JS = r"""
({quote, nth, folds, drop, key, scroll}) => {
  const dropSet = new Set(drop);
  const fold = ch => {
    let o = '';
    for (const c of ch.normalize('NFKC')) { if (!dropSet.has(c)) o += (folds[c] ?? c); }
    return o.toLowerCase();
  };
  // Append one source string to a normalised buffer, one map entry per OUTPUT code unit.
  const flatten = (state, text, node) => {
    for (let i = 0; i < text.length; ) {
      const ch = String.fromCodePoint(text.codePointAt(i)), len = ch.length, c = fold(ch);
      for (let k = 0; k < c.length; k++) {
        let o = c[k];
        if (/\s/.test(o)) { if (state.lastSpace) continue; o = ' '; state.lastSpace = true; }
        else state.lastSpace = false;
        state.flat += o; state.map.push([node, i, len]);
      }
      i += len;
    }
  };
  const blocks = new Map();
  const blockOf = el => {
    if (blocks.has(el)) return blocks.get(el);
    let e = el;
    while (e && e !== document.body) {
      const d = getComputedStyle(e).display;
      if (!d.startsWith('inline') && d !== 'contents') break;
      e = e.parentElement;
    }
    blocks.set(el, e || document.body);
    return e || document.body;
  };
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'TEXTAREA']);
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
    acceptNode: n => {
      if (n.nodeType === Node.ELEMENT_NODE) {
        if (SKIP.has(n.tagName)) return NodeFilter.FILTER_REJECT;
        return n.tagName === 'BR' ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      }
      const p = n.parentElement;
      if (!p) return NodeFilter.FILTER_REJECT;
      if (p.checkVisibility && !p.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}))
        return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }});
  const page = {flat: '', map: [], lastSpace: true};
  const gap = n => {
    if (!page.lastSpace) { page.flat += ' '; page.map.push([n, 0, 0]); page.lastSpace = true; }
  };
  let node, lastBlock = null;
  while ((node = walker.nextNode())) {
    if (node.nodeType === Node.ELEMENT_NODE) { gap(node); continue; }  // <br>
    const b = blockOf(node.parentElement);
    if (lastBlock && b !== lastBlock) gap(node);
    lastBlock = b;
    flatten(page, node.nodeValue, node);
  }
  const qs = {flat: '', map: [], lastSpace: true};
  flatten(qs, quote, null);
  const q = qs.flat.trim();
  if (!q) return {found: false, occurrences: 0};
  let at = -1;
  for (let k = 0; k <= nth; k++) {
    at = page.flat.indexOf(q, at + 1);
    if (at < 0) return {found: false, occurrences: k};
  }
  const [sn, so] = page.map[at], [en, eo, elen] = page.map[at + q.length - 1];
  const r = document.createRange();
  r.setStart(sn, so); r.setEnd(en, eo + elen);
  (window.__walkthruRanges = window.__walkthruRanges || {})[key] = r;
  if (scroll) {  // centre the quote itself, in whichever element scrolls
    sn.parentElement.scrollIntoView({block: 'center', behavior: 'instant'});
    const off = () => { const b = r.getBoundingClientRect(); return b.top + b.height / 2 - innerHeight / 2; };
    let sc = sn.parentElement;
    while (sc && !(/(auto|scroll|overlay)/.test(getComputedStyle(sc).overflowY) && sc.scrollHeight > sc.clientHeight))
      sc = sc.parentElement;
    (sc || window).scrollBy({top: off(), behavior: 'instant'});
    // Wrong scroller guessed: fall back to the window.
    if (Math.abs(off()) > innerHeight * 0.25) window.scrollBy({top: off(), behavior: 'instant'});
  }
  return {found: true};
}
"""

#: Browser-side check of the range stored under ``key`` (argument: ``{"key": key}``). Returns the
#: range's text, one box per line fragment of each text node, whether each box is on screen and
#: whether a caret hit test at six points inside it lands in that very text node (not a banner
#: over it, not an ancestor under clipped text), and how many text nodes hold visible characters
#: yet draw nothing (zero size, transparent). Judge the result with :func:`judge_passage`.
PASSAGE_VERIFY_JS = r"""
({key}) => {
  const r = (window.__walkthruRanges || {})[key];
  if (!r) return null;
  const caretAt = (x, y) => {
    if (document.caretPositionFromPoint) {
      const c = document.caretPositionFromPoint(x, y);
      return c ? [c.offsetNode, c.offset] : null;
    }
    if (document.caretRangeFromPoint) {
      const c = document.caretRangeFromPoint(x, y);
      return c ? [c.startContainer, c.startOffset] : null;
    }
    return null;
  };
  const root = r.commonAncestorContainer;
  const texts = [];
  if (root.nodeType === Node.TEXT_NODE) texts.push(root);
  else {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) if (r.intersectsNode(n)) texts.push(n);
  }
  const boxes = [];
  let invisible = 0;
  for (const n of texts) {
    const s0 = n === r.startContainer ? r.startOffset : 0;
    const s1 = n === r.endContainer ? r.endOffset : n.nodeValue.length;
    const sub = document.createRange();
    sub.setStart(n, s0); sub.setEnd(n, s1);
    const cs = n.parentElement ? getComputedStyle(n.parentElement) : null;
    let drawn = 0;
    for (const b of sub.getClientRects()) {
      if (b.width <= 1 || b.height <= 1) continue;
      drawn++;
      const onscreen = b.left >= -0.5 && b.top >= -0.5 && b.right <= innerWidth + 0.5 && b.bottom <= innerHeight + 0.5;
      let hit = onscreen;
      if (onscreen) {
        for (const fx of [0.15, 0.5, 0.85]) for (const fy of [0.3, 0.7]) {
          const c = caretAt(b.left + fx * b.width, b.top + fy * b.height);
          if (!c || c[0] !== n || c[1] < s0 || c[1] > s1) hit = false;
        }
      }
      boxes.push({box: [b.left, b.top, b.right, b.bottom], onscreen, hit});
    }
    const hasInk = /\S/.test(n.nodeValue.slice(s0, s1));
    const clear = cs && (cs.color === 'transparent' || /rgba\([^)]*,\s*0(\.0+)?\)$/.test(cs.color));
    if (hasInk && (drawn === 0 || clear)) invisible++;
  }
  return {matched: r.toString(), boxes, invisible, dpr: devicePixelRatio};
}
"""

# Hide large fixed/sticky layers (modals, paywall curtains) and undo a scroll lock.
_HIDE_OVERLAYS_JS = r"""
(minFrac) => {
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if ((cs.position === 'fixed' || cs.position === 'sticky')
        && el.getBoundingClientRect().height > innerHeight * minFrac)
      el.style.setProperty('display', 'none', 'important');
  }
  document.documentElement.style.overflow = 'auto';
  document.body.style.overflow = 'auto';
}
"""


@contextmanager
def open_capture_page(
    *,
    viewport: tuple[int, int] = (1600, 900),
    device_scale_factor: float = 1.5,
    color_scheme: str = "light",
    user_agent: str = DEFAULT_USER_AGENT,
    locale: str = "en-US",
    browser_type: str = "chromium",
    launch_options: Optional[dict] = None,
) -> Iterator[Any]:
    """Open a headless browser page configured for source captures; close it on exit.

    Light colour scheme by default: a page that follows ``prefers-color-scheme`` then renders
    light, where a multiply highlighter reads best. Reuse one page across many captures::

        with open_capture_page() as page:
            for url, quote in sources:
                capture_passage(url, quote, out=f"shots/{n}.png", page=page)
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise missing_extra(
            "playwright", "evidence", purpose="Capturing a web page"
        ) from e
    with sync_playwright() as pw:
        browser = getattr(pw, browser_type).launch(**(launch_options or {}))
        try:
            ctx = browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]},
                device_scale_factor=device_scale_factor,
                color_scheme=color_scheme,
                user_agent=user_agent,
                locale=locale,
            )
            yield ctx.new_page()
        finally:
            browser.close()


def _as_url(source: Union[str, Path]) -> str:
    s = str(source)
    if urlparse(s).scheme in ("http", "https", "file", "data", "about"):
        return s
    return Path(s).resolve().as_uri()


def _goto(page: Any, url: str, *, timeout_ms: int, settle_ms: int) -> None:
    """Navigate, tolerating client-side redirects ("interrupted by another navigation")."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except (
        Exception
    ) as e:  # Playwright's Error type is not importable without the extra
        if "interrupted by another navigation" not in str(e):
            raise
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    if settle_ms:
        page.wait_for_timeout(settle_ms)


def _dismiss_dialogs(
    page: Any, labels: Sequence[str], *, after_ms: int
) -> Optional[str]:
    """Click the first visible button named one of ``labels``, in any frame; returns its name."""
    for frame in page.frames:
        for label in labels:
            try:
                button = frame.get_by_role("button", name=label, exact=True)
                if button.count() and button.first.is_visible():
                    button.first.click(timeout=1500)
                    page.wait_for_timeout(after_ms)
                    return label
            except (
                Exception
            ):  # a detached frame or a button that vanished: try the next
                continue
    return None


def _png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG's IHDR chunk (no imaging library needed)."""
    with path.open("rb") as f:
        head = f.read(24)
    w, h = struct.unpack(">II", head[16:24])
    return int(w), int(h)


def _same_boxes(a: Sequence[dict], b: Sequence[dict], *, tol: float = 1.0) -> bool:
    return len(a) == len(b) and all(
        abs(p - q) <= tol for x, y in zip(a, b) for p, q in zip(x["box"], y["box"])
    )


def capture_html(
    source: Union[str, Path],
    quote: Optional[str] = None,
    *,
    out: Union[str, Path],
    nth: Optional[int] = None,
    page: Any = None,
    page_factory: Callable[[], ContextManager[Any]] = open_capture_page,
    dismiss_labels: Sequence[str] = DEFAULT_DISMISS_LABELS,
    hide_overlays: bool = True,
    overlay_min_height_frac: float = 0.25,
    settle_ms: int = 1500,
    scroll_settle_ms: int = 400,
    max_occurrences: int = 6,
    timeout_ms: int = 45_000,
) -> Capture:
    """Screenshot a web page centred on ``quote``, with the quote's verified boxes.

    Args:
        source: ``http(s)://`` or ``file://`` URL, or a local HTML path.
        quote: The passage to locate; ``None`` just captures the page.
        out: Image path (written as PNG) — a ``.json`` sidecar is written beside it.
        nth: Use exactly this occurrence; by default occurrences are tried until one verifies.
        page: A Playwright sync ``Page`` to drive. When ``None`` one is opened with
            ``page_factory`` and closed afterwards.
        page_factory: Context manager factory yielding a page (default
            :func:`open_capture_page`).
        dismiss_labels: Button names that close consent dialogs; ``()`` to skip.
        hide_overlays: Hide fixed/sticky layers taller than ``overlay_min_height_frac`` of the
            viewport. Smaller sticky bars stay, and the hit test catches them if they cover
            the quote.
        settle_ms: Wait after load, for late layout and consent dialogs.
        scroll_settle_ms: Wait after centring the quote, for lazy content.
        max_occurrences: How many occurrences to try when ``nth`` is not given.
        timeout_ms: Navigation timeout.
    """
    if page is None:
        with page_factory() as opened:
            return capture_html(
                source, quote, out=out, nth=nth, page=opened, page_factory=page_factory,
                dismiss_labels=dismiss_labels, hide_overlays=hide_overlays,
                overlay_min_height_frac=overlay_min_height_frac, settle_ms=settle_ms,
                scroll_settle_ms=scroll_settle_ms, max_occurrences=max_occurrences,
                timeout_ms=timeout_ms,
            )  # fmt: skip

    png = Path(out).with_suffix(".png")
    png.parent.mkdir(parents=True, exist_ok=True)
    _goto(page, _as_url(source), timeout_ms=timeout_ms, settle_ms=settle_ms)
    dismissed = (
        _dismiss_dialogs(page, dismiss_labels, after_ms=600) if dismiss_labels else None
    )
    if hide_overlays:
        page.evaluate(_HIDE_OVERLAYS_JS, overlay_min_height_frac)

    details: dict[str, Any] = {"dismissed": dismissed}
    result: dict[str, Any] = {"reason": "no-quote"}
    if quote and normalize_text(quote):
        tries = [nth] if nth is not None else range(max_occurrences)
        result = {"reason": "not-in-text"}
        for k in tries:
            located = page.evaluate(PASSAGE_LOCATE_JS, locate_args(quote, nth=k))
            if not located.get("found"):
                break
            if scroll_settle_ms:
                page.wait_for_timeout(scroll_settle_ms)
            result = judge_passage(page.evaluate(PASSAGE_VERIFY_JS, _KEY), quote, nth=k)
            if result.get("found"):
                break

    page.screenshot(path=str(png))
    if result.get("found"):  # the screenshot must show what was verified, unchanged
        after = judge_passage(
            page.evaluate(PASSAGE_VERIFY_JS, _KEY), quote, nth=result["nth"]
        )
        if not (after.get("found") and _same_boxes(result["boxes"], after["boxes"])):
            result = {**result, "found": False, "reason": "unstable"}

    found = bool(result.get("found"))
    scale = result.get("dpr", 1.0)
    rects = (
        tuple(
            tuple(v * scale for v in r)
            for r in merge_line_boxes(b["box"] for b in result["boxes"])
        )
        if found
        else ()
    )
    if "boxes" in result:
        details["occluded"] = sum(
            1 for b in result["boxes"] if b["onscreen"] and not b["hit"]
        )
        details["offscreen"] = sum(1 for b in result["boxes"] if not b["onscreen"])
    capture = Capture(
        png=png,
        size=_png_size(png),
        rects=rects,
        found=found,
        matched=result.get("matched"),
        verified_by="dom" if found else None,
        source=str(source),
        quote=quote,
        reason=None if found else result.get("reason"),
        nth=result.get("nth"),
        details=details,
    )
    capture.save()
    return capture


def locate_args(
    quote: str, *, nth: int = 0, key: str = "passage", scroll: bool = True
) -> dict[str, Any]:
    """The argument object for :data:`PASSAGE_LOCATE_JS` (fold tables included).

    ``key`` names the stored range, so several passages can be located on one page; pass the
    same ``{"key": key}`` to :data:`PASSAGE_VERIFY_JS`. ``scroll=False`` locates without moving
    the page (for a demo whose scrolling is itself a step).
    """
    return {
        "quote": quote,
        "nth": nth,
        "folds": FOLDS,
        "drop": sorted(DROP),
        "key": key,
        "scroll": scroll,
    }


_KEY = {"key": "passage"}


def judge_passage(state: Optional[dict], quote: str, *, nth: int = 0) -> dict[str, Any]:
    """Decide from a :data:`PASSAGE_VERIFY_JS` result whether the passage is verified.

    Returns ``{"found": bool, "reason": ..., "matched", "boxes", "dpr", "nth"}``. In order: the
    range's text must spell the quote (spacing aside: block boundaries are spaces to the locator
    but not to ``Range.toString()``); no text in the range may be invisible; every box must be on
    screen; every box must pass the caret hit test.
    """
    if not state:
        return {"found": False, "reason": "not-in-text"}
    boxes = state["boxes"]
    base = {
        "matched": state["matched"],
        "boxes": boxes,
        "dpr": state["dpr"],
        "nth": nth,
    }

    def letters(text: str) -> str:
        return normalize_text(text).replace(" ", "")

    if letters(state["matched"]) != letters(quote) or not boxes:
        return {**base, "found": False, "reason": "mismatch"}
    if state.get("invisible"):
        return {**base, "found": False, "reason": "invisible"}
    if not all(b["onscreen"] for b in boxes):
        return {**base, "found": False, "reason": "off-screen"}
    if not all(b["hit"] for b in boxes):
        return {**base, "found": False, "reason": "occluded"}
    return {**base, "found": True}
