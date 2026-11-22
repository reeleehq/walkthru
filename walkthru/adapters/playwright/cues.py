"""A Python ``CueRenderer``: draw a Demo Document's cues on the live page as DOM overlays.

Until now the only planned :class:`~walkthru.ports.CueRenderer` was driver.js on the TS side, so
nothing in Python could draw a cue: a step and its cues could be *filmed* only by an app that drew
its own. :class:`DomOverlayCueRenderer` draws them on the page itself, as absolutely positioned
elements in one overlay layer, so a screenshot or a screencast of the page shows them:

- ``highlight``: a ring around the target (``color``, ``thickness``, ``padding``, ``shape``);
- ``callout``: a numbered badge at the target's top-left corner, the number being the callout's
  order among the callouts shown since the last :meth:`~DomOverlayCueRenderer.clear` (so a guide
  can list the callout texts in the same order); ``callout_text=True`` also draws the text;
- ``spotlight``: the page dimmed around a cut-out over the target;
- ``hotspot``: a dot at the target's centre;
- ``cursor``: nothing (the synthetic cursor of :mod:`~walkthru.adapters.playwright.cursor` and
  :class:`~walkthru.adapters.playwright.gestures.Hand` move the pointer).

A cue's rect is what the :class:`~walkthru.ports.ElementLocator` returns (viewport coordinates);
the overlay script adds the page's scroll offset, so the overlay stays on its element when the page
scrolls and lands in the right place on a full-page screenshot. Like the rest of this package it
drives an injected, duck-typed page (only ``evaluate`` is used) and imports nothing from
``playwright``.

>>> import asyncio
>>> from walkthru.core.schema import Anchor, CalloutCue, HighlightCue, Rect
>>> class FakePage:
...     def __init__(self): self.calls = []
...     async def evaluate(self, script, arg=None): self.calls.append(arg)
>>> page = FakePage()
>>> r = DomOverlayCueRenderer(page)
>>> a = Anchor(step_id="s1")
>>> asyncio.run(r.show(HighlightCue(id="h", anchor=a), Rect(x=1, y=2, width=3, height=4)))
>>> asyncio.run(r.show(CalloutCue(id="c", anchor=a, text="Here"), Rect(x=0, y=0, width=1, height=1)))
>>> [(c["kind"], c.get("number")) for c in page.calls]
[('highlight', None), ('callout', 1)]
>>> asyncio.run(r.clear()); page.calls[-1]
{'kind': 'clear', 'layer': 'walkthru-cue-layer'}
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from walkthru.core.schema import (
    CalloutCue,
    Cue,
    CursorCue,
    HighlightCue,
    HotspotCue,
    Rect,
    SpotlightCue,
)

if TYPE_CHECKING:  # type-checker only — never imported at runtime (firewall)
    from walkthru.ports import ElementLocator

__all__ = [
    "DEFAULT_CUE_COLOR",
    "CUE_LAYER_ID",
    "DomOverlayCueRenderer",
    "cue_overlay_payload",
]

#: The default colour of rings, badges and hotspots (a saturated orange that reads on light and
#: dark UIs alike).
DEFAULT_CUE_COLOR = "#ff5a1f"
DEFAULT_THICKNESS = 3
DEFAULT_PADDING = 6
DEFAULT_SPOTLIGHT_OPACITY = 0.55
DEFAULT_BADGE_SIZE = 26
DEFAULT_HOTSPOT_SIZE = 18
#: The id of the one element every overlay is drawn in; :meth:`DomOverlayCueRenderer.clear`
#: removes it.
CUE_LAYER_ID = "walkthru-cue-layer"

#: Draws one overlay (or clears the layer). Coordinates arrive viewport-relative and are moved to
#: document coordinates here, with the scroll offset at the moment of drawing.
_OVERLAY_SCRIPT = r"""
(p) => {
  const doc = document.documentElement;
  let layer = document.getElementById(p.layer);
  if (p.kind === 'clear') { if (layer) layer.remove(); return; }
  if (!layer) {
    layer = document.createElement('div');
    layer.id = p.layer;
    layer.setAttribute('aria-hidden', 'true');
    Object.assign(layer.style, {position: 'absolute', left: '0px', top: '0px', width: '0px',
      height: '0px', overflow: 'visible', pointerEvents: 'none', zIndex: '2147483647'});
    doc.appendChild(layer);
  }
  const x = p.rect.x + window.scrollX, y = p.rect.y + window.scrollY;
  const el = document.createElement('div');
  el.dataset.walkthruCue = p.kind;
  const base = {position: 'absolute', boxSizing: 'border-box', pointerEvents: 'none'};
  if (p.kind === 'highlight') {
    const pad = p.padding;
    Object.assign(el.style, base, {left: (x - pad) + 'px', top: (y - pad) + 'px',
      width: (p.rect.width + 2 * pad) + 'px', height: (p.rect.height + 2 * pad) + 'px',
      border: p.thickness + 'px solid ' + p.color,
      borderRadius: p.shape === 'circle' ? '50%' : '6px'});
  } else if (p.kind === 'spotlight') {
    Object.assign(el.style, base, {left: x + 'px', top: y + 'px', width: p.rect.width + 'px',
      height: p.rect.height + 'px', borderRadius: '6px',
      boxShadow: '0 0 0 200vmax ' + p.color});
  } else if (p.kind === 'hotspot') {
    const s = p.size;
    Object.assign(el.style, base, {left: (x + p.rect.width / 2 - s / 2) + 'px',
      top: (y + p.rect.height / 2 - s / 2) + 'px', width: s + 'px', height: s + 'px',
      borderRadius: '50%', background: p.color, boxShadow: '0 0 0 4px rgba(255,255,255,0.8)'});
  } else if (p.kind === 'callout') {
    const s = p.size;
    Object.assign(el.style, base, {left: (x - s / 2) + 'px', top: (y - s / 2) + 'px',
      display: 'flex', alignItems: 'center', gap: '6px'});
    const badge = document.createElement('div');
    badge.textContent = String(p.number);
    Object.assign(badge.style, {width: s + 'px', height: s + 'px', borderRadius: '50%',
      background: p.color, color: '#fff', font: 'bold ' + Math.round(s * 0.55) + 'px sans-serif',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      boxShadow: '0 0 0 2px #fff, 0 1px 4px rgba(0,0,0,0.4)'});
    el.appendChild(badge);
    if (p.text) {
      const t = document.createElement('div');
      t.textContent = p.text;
      Object.assign(t.style, {background: '#fff', color: '#111', border: '2px solid ' + p.color,
        borderRadius: '6px', padding: '4px 8px', font: '14px sans-serif', maxWidth: '280px',
        boxShadow: '0 1px 4px rgba(0,0,0,0.3)'});
      el.appendChild(t);
    }
  }
  layer.appendChild(el);
}
"""


def cue_overlay_payload(
    cue: Cue,
    rect: Rect,
    *,
    number: Optional[int] = None,
    color: str = DEFAULT_CUE_COLOR,
    callout_text: bool = False,
    layer: str = CUE_LAYER_ID,
) -> Optional[dict]:
    """What the overlay script draws for ``cue`` at ``rect``, or ``None`` for nothing.

    The cue's own style fields win over the defaults. Pure, so the mapping is testable without a
    page.

    >>> from walkthru.core.schema import Anchor, HighlightCue, Rect
    >>> p = cue_overlay_payload(HighlightCue(id="h", anchor=Anchor(step_id="s"), thickness=5),
    ...                         Rect(x=0, y=0, width=10, height=10))
    >>> p["kind"], p["thickness"], p["padding"], p["color"]
    ('highlight', 5, 6, '#ff5a1f')
    """
    common = {"layer": layer, "rect": rect.model_dump()}
    if isinstance(cue, HighlightCue):
        return {
            **common,
            "kind": "highlight",
            "color": cue.color or color,
            "thickness": DEFAULT_THICKNESS if cue.thickness is None else cue.thickness,
            "padding": DEFAULT_PADDING if cue.padding is None else cue.padding,
            "shape": cue.shape or "rect",
        }
    if isinstance(cue, SpotlightCue):
        opacity = DEFAULT_SPOTLIGHT_OPACITY if cue.opacity is None else cue.opacity
        return {
            **common,
            "kind": "spotlight",
            "color": cue.color or f"rgba(0,0,0,{opacity})",
        }
    if isinstance(cue, HotspotCue):
        return {
            **common,
            "kind": "hotspot",
            "color": color,
            "size": cue.size or DEFAULT_HOTSPOT_SIZE,
        }
    if isinstance(cue, CalloutCue):
        return {
            **common,
            "kind": "callout",
            "color": color,
            "number": number,
            "size": DEFAULT_BADGE_SIZE,
            "text": cue.text if callout_text else None,
        }
    if isinstance(cue, CursorCue):
        return None
    raise TypeError(f"not a cue: {type(cue).__name__}")


class DomOverlayCueRenderer:
    """A :class:`~walkthru.ports.CueRenderer` that draws cues as DOM overlays on a page.

    Args:
        page: a Playwright ``Page`` (injected, duck-typed: only ``evaluate`` is used).
        color: the colour of rings, badges and hotspots a cue does not set itself.
        callout_text: draw a callout's text beside its badge (a filmed tour); off for stills,
            whose callout texts a guide lists under the picture.
        locator: resolves a cue's ``target`` when :meth:`show` gets no ``rect``; defaults to a
            :class:`~walkthru.adapters.playwright.locator.PlaywrightElementLocator` on ``page``.
        layer: the overlay layer's element id.
    """

    def __init__(
        self,
        page: Any,
        *,
        color: str = DEFAULT_CUE_COLOR,
        callout_text: bool = False,
        locator: Optional["ElementLocator"] = None,
        layer: str = CUE_LAYER_ID,
    ):
        self._page = page
        self._color = color
        self._callout_text = callout_text
        self._locator = locator
        self._layer = layer
        self._callouts = 0

    @property
    def callouts_shown(self) -> int:
        """How many callouts are drawn since the last :meth:`clear` (the next badge is this + 1)."""
        return self._callouts

    async def show(self, cue: Cue, rect: Optional[Rect] = None) -> None:
        """Draw ``cue`` at ``rect`` (viewport coordinates), resolving its target when ``rect`` is
        ``None``. A cue with neither draws nothing."""
        if rect is None and cue.target is not None:
            rect = await self._locator_().bounds(cue.target)
        if rect is None:
            return
        number = None
        if isinstance(cue, CalloutCue):
            self._callouts += 1
            number = self._callouts
        payload = cue_overlay_payload(
            cue,
            rect,
            number=number,
            color=self._color,
            callout_text=self._callout_text,
            layer=self._layer,
        )
        if payload is not None:
            await self._page.evaluate(_OVERLAY_SCRIPT, payload)

    async def clear(self) -> None:
        """Remove every overlay, and restart callout numbering."""
        self._callouts = 0
        await self._page.evaluate(
            _OVERLAY_SCRIPT, {"kind": "clear", "layer": self._layer}
        )

    def _locator_(self) -> "ElementLocator":
        if self._locator is None:
            from walkthru.adapters.playwright.locator import PlaywrightElementLocator

            self._locator = PlaywrightElementLocator(self._page)
        return self._locator
