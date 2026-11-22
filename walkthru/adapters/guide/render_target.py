"""A Demo Document as a step-by-step visual guide: one self-contained, phone-readable HTML page.

The video render targets film a tour; this one lays the same document out as a document:

- each section is a chapter (its ``title`` a heading);
- a ``textCard`` beat is prose (its ``text``, a small Markdown subset: see :func:`simple_markdown`);
- a step with a still is a figure: the picture (cues already drawn on it, see
  :func:`~walkthru.adapters.playwright.stills.play_stills`), the texts of its callout cues as a
  numbered list matching the badges, and its narration as the caption;
- other steps (no still, no text) are left out.

Stills come from ``stills={step_id: path}`` or, failing that, the step's ``poster``. They are
inlined as data URIs, so the page is one file that works offline and can be sent as is; never
``loading="lazy"``, which prints blank. A tap on a picture toggles it to full width; print CSS keeps
a figure on one page. PDF is pdfdol's job (:meth:`GuideRenderTarget.export_pdf`), not
reimplemented here.

>>> from walkthru.core.schema import Beat, DemoDocument, Meta, Section, Timing
>>> doc = DemoDocument(id="d", meta=Meta(title="Hello"), sections=[Section(id="s", title="Start",
...     steps=[Beat(id="b", beat_kind="textCard", text="Open **Settings**.",
...                 timing=Timing(duration_ms=0))])])
>>> html = guide_html(doc)
>>> "<h1>Hello</h1>" in html and "<strong>Settings</strong>" in html
True
"""

from __future__ import annotations

import base64
import html as _html
import mimetypes
import re
from pathlib import Path
from typing import Callable, Mapping, Optional, Union

from walkthru.core.schema import AssetRef, Beat, CalloutCue, DemoDocument, Step

__all__ = ["GuideRenderTarget", "guide_html", "simple_markdown", "DEFAULT_GUIDE_CSS"]

Markdown = Callable[[str], str]

#: The page's style: a single readable column, figures full width on a phone, print-safe.
DEFAULT_GUIDE_CSS = """
:root { --ink: #1b1b1f; --muted: #5d5d66; --line: #e3e3e8; --accent: #ff5a1f; --bg: #ffffff; }
@media (prefers-color-scheme: dark) {
  :root { --ink: #ececf1; --muted: #a3a3ad; --line: #34343b; --bg: #141417; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 17px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 760px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 1.7em; line-height: 1.2; margin: 0 0 8px; }
.lede { color: var(--muted); margin: 0 0 24px; }
h2 { font-size: 1.3em; margin: 40px 0 12px; padding-top: 12px; border-top: 1px solid var(--line); }
figure { margin: 20px 0 28px; }
figure img { display: block; width: 100%; height: auto; border: 1px solid var(--line);
  border-radius: 8px; cursor: zoom-in; }
figure.zoomed img { width: auto; max-width: none; cursor: zoom-out; }
figure.zoomed { overflow-x: auto; }
figcaption { margin-top: 8px; }
.step-no { color: var(--accent); font-weight: 700; margin-right: 6px; }
ol.callouts { margin: 8px 0 0; padding-left: 0; list-style: none; counter-reset: c; }
ol.callouts li { counter-increment: c; margin: 4px 0; padding-left: 34px; position: relative; }
ol.callouts li::before { content: counter(c); position: absolute; left: 0; top: 1px;
  width: 24px; height: 24px; border-radius: 50%; background: var(--accent); color: #fff;
  font: bold 13px/24px sans-serif; text-align: center; }
code { font-size: 0.92em; background: rgba(127,127,127,0.15); padding: 1px 4px; border-radius: 4px; }
@media print {
  main { max-width: none; padding: 0; }
  figure { break-inside: avoid; page-break-inside: avoid; }
  figure img { cursor: default; }
  h2 { break-after: avoid; page-break-after: avoid; }
}
"""

_ZOOM_SCRIPT = (
    "document.querySelectorAll('figure img').forEach(function(img){"
    "img.addEventListener('click',function(){img.parentNode.classList.toggle('zoomed');});});"
)

_CODE_SPAN = re.compile(r"`([^`]+)`")
_INLINE_RULES = (
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![*\w])\*([^*]+)\*(?![*\w])"), r"<em>\1</em>"),
    (re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)"), r'<a href="\2">\1</a>'),
)


def simple_markdown(text: str) -> str:
    """The Markdown subset a text card needs: paragraphs, ``- `` lists, bold, italic, code, links.

    Everything is escaped first, so a text card cannot inject markup. Pass ``markdown=`` to
    :func:`guide_html` for a full Markdown renderer.

    >>> simple_markdown("Tap **Save**.\\n\\n- one\\n- `two`")
    '<p>Tap <strong>Save</strong>.</p>\\n<ul><li>one</li><li><code>two</code></li></ul>'
    """

    def inline(s: str) -> str:
        # quote=True: an escaped `"` cannot close the href a link rule writes
        s = _html.escape(s, quote=True)
        codes: list[str] = []

        def stash(m):  # a code span's text is not formatted further
            codes.append(m.group(1))
            return f"\x00{len(codes) - 1}\x00"

        s = _CODE_SPAN.sub(stash, s)
        for pattern, repl in _INLINE_RULES:
            s = pattern.sub(repl, s)
        return re.sub(
            r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>", s
        )

    blocks = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(ln.startswith(("- ", "* ")) for ln in lines):
            items = "".join(f"<li>{inline(ln[2:])}</li>" for ln in lines)
            blocks.append(f"<ul>{items}</ul>")
        else:
            blocks.append(f"<p>{'<br>'.join(inline(ln) for ln in lines)}</p>")
    return "\n".join(blocks)


def _data_uri(path: Union[str, Path], mime: Optional[str] = None) -> str:
    """``path``'s bytes as a data URI (its mime from ``mime`` or the suffix)."""
    path = Path(path)
    kind = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{kind};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _still_of(
    step: Step, stills: Mapping[str, Union[str, Path]], *, base_dir: Path
) -> Optional[str]:
    """The data URI of ``step``'s still, from ``stills`` or its ``poster``.

    A local poster (a path, relative ones read under ``base_dir``, or a ``file://`` url) is
    inlined; a ``data:`` uri is used as is; a remote (``http(s)``) poster is linked, so that
    figure needs the network.
    """
    if step.id in stills:
        return _data_uri(stills[step.id])
    poster: Optional[AssetRef] = step.poster
    if poster is None:
        return None
    uri = poster.uri
    if uri.startswith(("data:", "http://", "https://")):
        return uri
    if uri.startswith("file://"):
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        uri = url2pathname(urlparse(uri).path)
    return _data_uri(Path(base_dir) / uri, poster.mime)


def guide_html(
    document: DemoDocument,
    stills: Optional[Mapping[str, Union[str, Path]]] = None,
    *,
    title: Optional[str] = None,
    markdown: Markdown = simple_markdown,
    css: str = DEFAULT_GUIDE_CSS,
    number_steps: bool = True,
    base_dir: Union[str, Path] = ".",
) -> str:
    """``document`` as one self-contained HTML guide (see the module docstring).

    Args:
        stills: ``{step_id: image path}``; a step not in it falls back to its ``poster``.
        title: the page title; default the document's ``meta.title``, else its id.
        markdown: renders a text card's text to HTML (default :func:`simple_markdown`).
        css: the page's style sheet.
        number_steps: prefix each figure's caption with its number in the guide.
        base_dir: where a relative ``poster`` path is read from (the document's folder).
    """
    stills = stills or {}
    esc = _html.escape
    title = title or document.meta.title or document.id
    callouts: dict[str, list[str]] = {}
    for cue in document.tracks.cues:
        if isinstance(cue, CalloutCue):
            callouts.setdefault(cue.anchor.step_id, []).append(cue.text)
    narration: dict[str, list[str]] = {}
    for seg in document.tracks.narration:
        narration.setdefault(seg.anchor.step_id, []).append(seg.text)

    body = [f"<h1>{esc(title)}</h1>"]
    if document.meta.description:
        body.append(f'<p class="lede">{esc(document.meta.description)}</p>')
    figure_no = 0
    for section in document.sections:
        parts = []
        for step in section.steps:
            if isinstance(step, Beat) and step.beat_kind == "textCard":
                if step.text:
                    parts.append(markdown(step.text))
                continue
            src = _still_of(step, stills, base_dir=Path(base_dir))
            if src is None:
                continue
            figure_no += 1
            words = " ".join(narration.get(step.id, []))
            number = (
                f'<span class="step-no">{figure_no}.</span>' if number_steps else ""
            )
            caption = (
                f"<figcaption>{number}{esc(words)}</figcaption>"
                if (words or number)
                else ""
            )
            items = callouts.get(step.id, [])
            listed = (
                '<ol class="callouts">'
                + "".join(f"<li>{esc(t)}</li>" for t in items)
                + "</ol>"
                if items
                else ""
            )
            alt = esc(words or f"Step {figure_no}", quote=True)
            parts.append(
                f'<figure id="{esc(step.id, quote=True)}"><img src="{esc(src, quote=True)}" alt="{alt}">'
                f"{caption}{listed}</figure>"
            )
        if parts:
            if section.title:
                body.append(f"<h2>{esc(section.title)}</h2>")
            body.extend(parts)
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{esc(title)}</title><style>{css}</style></head>"
        f"<body><main>\n{chr(10).join(body)}\n</main><script>{_ZOOM_SCRIPT}</script></body></html>\n"
    )


class GuideRenderTarget:
    """A :class:`~walkthru.ports.RenderTarget` that writes a Demo Document as an HTML guide.

    Args:
        out: where to write the HTML page.
        stills: ``{step_id: image path}`` (what
            :func:`~walkthru.adapters.playwright.stills.play_stills` returns); steps not in it use
            their ``poster``.
        **options: passed to :func:`guide_html` (``title``, ``markdown``, ``css``,
            ``number_steps``).
    """

    def __init__(
        self,
        out: Union[str, Path],
        *,
        stills: Optional[Mapping[str, Union[str, Path]]] = None,
        **options,
    ):
        self._out = Path(out)
        self._stills = dict(stills or {})
        self._options = options

    def html(self, document: DemoDocument) -> str:
        """The guide's HTML, without writing it."""
        return guide_html(document, self._stills, **self._options)

    async def export(self, artifact: DemoDocument) -> AssetRef:
        """Write the guide and return its reference."""
        self._out.parent.mkdir(parents=True, exist_ok=True)
        self._out.write_text(self.html(artifact), encoding="utf-8")
        return AssetRef(uri=str(self._out), mime="text/html")

    async def export_pdf(self, pdf_path: Union[str, Path], **pdf_options) -> Path:
        """Print the written guide (await :meth:`export` first) to PDF with pdfdol's headless
        Chromium, with print CSS (``media="print"``) unless ``pdf_options`` says otherwise.

        pdfdol drives Playwright's sync API, which refuses to run inside an event loop, so it
        runs in a worker thread. Needs pdfdol and a Playwright Chromium.
        """
        import asyncio

        try:
            from pdfdol.chromium import html_to_pdf_w_chromium
        except ImportError as e:
            raise ImportError(
                f"export_pdf needs pdfdol's Chromium printer (pdfdol.chromium): {e}"
            ) from e
        if not self._out.is_file():
            raise FileNotFoundError(
                f"no guide at {self._out}: await export(document) first"
            )
        pdf_options.setdefault("media", "print")
        await asyncio.to_thread(
            html_to_pdf_w_chromium, str(self._out), str(pdf_path), **pdf_options
        )
        return Path(pdf_path)
