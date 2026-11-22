"""Annotated stills from a Demo Document: play it on a page and take one picture per step.

A Demo Document already describes a step-by-step visual guide: steps as page commands, cues
(highlight rings, numbered callouts, spotlights) targeted by resilient ``Target``\\ s, narration
as the caption. :func:`play_stills` plays it with :func:`~walkthru.core.engine.play` and a
:class:`StillCamera` observer, which draws each step's cues with a
:class:`~walkthru.adapters.playwright.cues.DomOverlayCueRenderer` as the engine announces them,
screenshots the step when it ends, and clears the overlays. The result is ``{step_id: path}``,
which :func:`with_posters` writes into the steps' ``poster`` field (the existing "still for this
step") and :class:`~walkthru.adapters.guide.GuideRenderTarget` lays out as a guide. The same
document still drives the narrated video unchanged.

**The still is an observer, not a command**, so a document needs no extra step to be shot, and
the same document films and shoots. Which steps are shot is :func:`play_stills`'s ``shoot``
predicate (default: command steps and pause beats; text cards and B-roll have no screen).

**What is in the picture** is the union of the step's cue targets, padded, in full-page
coordinates (a locator's ``bounding_box`` is viewport-relative: the camera adds the scroll
offset), cut to ``max_height`` from the top when given; a step with no targeted cue is shot as
the viewport. Per-shot viewport width stays a ``page.set_viewport`` command before the step: a
short, wide region reads better on a phone when shot narrow.

``read_only=True`` aborts every non-GET request the page makes while the stills are taken, so a
guide can be shot against a live account without changing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Optional, Union

from walkthru.core.events import CueBegin, StepEnter, StepExit
from walkthru.core.schema import Beat, DemoDocument, Rect, Step, AssetRef

if TYPE_CHECKING:  # type-checker only
    from walkthru.ports import CueRenderer, ElementLocator

__all__ = [
    "DEFAULT_STILL_PAD",
    "DEFAULT_JPEG_QUALITY",
    "StillCamera",
    "default_shoot",
    "play_stills",
    "still_clip",
    "with_posters",
    "block_writes",
]

#: Pixels of context kept around the union of a step's cue targets.
DEFAULT_STILL_PAD = 24
DEFAULT_JPEG_QUALITY = 85
_IMAGE_SUFFIX = {"jpeg": ".jpg", "png": ".png"}
_IMAGE_MIME = {"jpeg": "image/jpeg", "png": "image/png"}

#: Scroll offset, page size and viewport, read when a still is taken.
_PAGE_GEOMETRY_SCRIPT = """() => ({
  sx: window.scrollX, sy: window.scrollY,
  pw: document.documentElement.scrollWidth, ph: document.documentElement.scrollHeight,
  vw: window.innerWidth, vh: window.innerHeight})"""


def still_clip(
    rects: Iterable[Rect],
    *,
    scroll: tuple[float, float],
    page_size: tuple[float, float],
    viewport: tuple[float, float],
    pad: float = DEFAULT_STILL_PAD,
    max_height: Optional[float] = None,
) -> dict:
    """The full-page clip (``{x, y, width, height}``) a still takes.

    ``rects`` are viewport-relative (as a locator returns them); their union is moved by
    ``scroll``, padded by ``pad``, kept inside the page, and cut to ``max_height`` from its top.
    No rects: the viewport, in page coordinates.

    >>> still_clip([Rect(x=10, y=10, width=20, height=20), Rect(x=50, y=5, width=10, height=10)],
    ...            scroll=(0, 100), page_size=(800, 2000), viewport=(800, 600), pad=5)
    {'x': 5.0, 'y': 100.0, 'width': 60.0, 'height': 35.0}
    >>> still_clip([], scroll=(0, 300), page_size=(800, 2000), viewport=(800, 600))
    {'x': 0.0, 'y': 300.0, 'width': 800.0, 'height': 600.0}
    """
    sx, sy = scroll
    pw, ph = page_size
    rects = list(rects)
    if not rects:
        x0, y0 = sx, sy
        x1, y1 = sx + viewport[0], sy + viewport[1]
    else:
        x0 = min(r.x for r in rects) + sx - pad
        y0 = min(r.y for r in rects) + sy - pad
        x1 = max(r.x + r.width for r in rects) + sx + pad
        y1 = max(r.y + r.height for r in rects) + sy + pad
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(float(pw), x1), min(float(ph), y1)
    if max_height is not None:
        y1 = min(y1, y0 + max_height)
    return {
        "x": float(x0),
        "y": float(y0),
        "width": float(max(1.0, x1 - x0)),
        "height": float(max(1.0, y1 - y0)),
    }


def default_shoot(step: Step) -> bool:
    """Shoot command steps and pause beats; text cards and B-roll have no screen of their own."""
    return not isinstance(step, Beat) or step.beat_kind == "pause"


@dataclass
class StillCamera:
    """An engine observer that draws each step's cues and screenshots the step as it ends.

    Use it as one of :func:`~walkthru.core.engine.play`'s ``observers`` (:func:`play_stills`
    does). ``shots`` maps step ids to the pictures taken; ``missed`` lists ``(cue_id, error)``
    for cues whose target could not be found (the still is taken without them).
    """

    page: Any
    out_dir: Path
    renderer: "CueRenderer"
    locator: "ElementLocator"
    shoot: Callable[[Step], bool] = default_shoot
    pad: float = DEFAULT_STILL_PAD
    max_height: Optional[float] = None
    image_format: str = "jpeg"
    quality: int = DEFAULT_JPEG_QUALITY
    shots: dict[str, Path] = field(default_factory=dict)
    missed: list[tuple[str, str]] = field(default_factory=list)
    _rects: list[Rect] = field(default_factory=list, init=False, repr=False)
    _current: Optional[Step] = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if self.image_format not in _IMAGE_SUFFIX:
            raise ValueError(
                f"image_format must be one of {sorted(_IMAGE_SUFFIX)}, got {self.image_format!r}"
            )
        self.out_dir = Path(self.out_dir)

    async def __call__(self, event: Any) -> None:
        if isinstance(event, StepEnter):
            self._current, self._rects = event.step, []
        elif isinstance(event, CueBegin) and self._shooting():
            await self._draw(event.cue)
        elif isinstance(event, StepExit) and self._shooting():
            try:
                self.shots[event.step.id] = await self._take(event.step.id)
            finally:
                await self.renderer.clear()
                self._current = None

    def _shooting(self) -> bool:
        return self._current is not None and self.shoot(self._current)

    async def _draw(self, cue) -> None:
        rect = None
        if cue.target is not None:
            try:
                rect = await self.locator.bounds(cue.target)
            except LookupError as e:  # the target is not on this page: shoot without it
                self.missed.append((cue.id, str(e)))
                return
            self._rects.append(rect)
        await self.renderer.show(cue, rect)

    async def _take(self, step_id: str) -> Path:
        g = await self.page.evaluate(_PAGE_GEOMETRY_SCRIPT)
        clip = still_clip(
            self._rects,
            scroll=(g["sx"], g["sy"]),
            page_size=(g["pw"], g["ph"]),
            viewport=(g["vw"], g["vh"]),
            pad=self.pad,
            max_height=self.max_height,
        )
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"{step_id}{_IMAGE_SUFFIX[self.image_format]}"
        extra = {"quality": self.quality} if self.image_format == "jpeg" else {}
        await self.page.screenshot(
            path=str(path), clip=clip, full_page=True, type=self.image_format, **extra
        )
        return path


async def block_writes(page: Any) -> Callable[[], Any]:
    """Abort every non-GET request ``page`` makes; returns an async callable that lifts it.

    A guide shot against a live account then cannot change it, whatever a step clicks.
    """

    async def handler(route, request=None):
        req = request if request is not None else route.request
        if req.method.upper() in ("GET", "HEAD", "OPTIONS"):
            await route.continue_()
        else:
            await route.abort()

    await page.route("**/*", handler)

    async def lift():
        await page.unroute("**/*", handler)

    return lift


async def play_stills(
    document: DemoDocument,
    page: Any,
    out_dir: Union[str, Path],
    *,
    player: Optional[Any] = None,
    renderer: Optional["CueRenderer"] = None,
    locator: Optional["ElementLocator"] = None,
    waiter: Optional[Callable] = None,
    shoot: Callable[[Step], bool] = default_shoot,
    pad: float = DEFAULT_STILL_PAD,
    max_height: Optional[float] = None,
    image_format: str = "jpeg",
    quality: int = DEFAULT_JPEG_QUALITY,
    read_only: bool = False,
    observers: Iterable[Callable] = (),
) -> dict[str, Path]:
    """Play ``document`` on ``page`` and return ``{step_id: still}``, cues drawn on each.

    Args:
        player: runs the steps' commands; default a
            :class:`~walkthru.adapters.playwright.player.PlaywrightCommandPlayer` on ``page`` (pass
            one with your own ``commands=`` table for an app vocabulary).
        renderer: draws the cues; default a
            :class:`~walkthru.adapters.playwright.cues.DomOverlayCueRenderer` (numbered callouts,
            no callout text: a guide lists the texts under the picture).
        locator: resolves cue targets; default a ``PlaywrightElementLocator``.
        waiter: resolves steps' ``waitFor`` gates; default a ``PlaywrightReadinessWaiter``.
        shoot: which steps get a still (default :func:`default_shoot`).
        pad, max_height: the clip around the step's cue targets (see :func:`still_clip`).
        image_format, quality: ``"jpeg"`` (default; small enough to inline in one HTML file) or
            ``"png"``.
        read_only: abort every non-GET request while shooting (see :func:`block_writes`).
        observers: more engine observers to run alongside the camera.

    Raises:
        RuntimeError: a step's command failed (the stills after it would show the wrong screen).
    """
    from walkthru.adapters.playwright.cues import DomOverlayCueRenderer
    from walkthru.adapters.playwright.locator import PlaywrightElementLocator
    from walkthru.adapters.playwright.player import PlaywrightCommandPlayer
    from walkthru.adapters.playwright.readiness import PlaywrightReadinessWaiter
    from walkthru.core.engine import play

    locator = locator or PlaywrightElementLocator(page)
    camera = StillCamera(
        page=page,
        out_dir=Path(out_dir),
        renderer=renderer or DomOverlayCueRenderer(page, locator=locator),
        locator=locator,
        shoot=shoot,
        pad=pad,
        max_height=max_height,
        image_format=image_format,
        quality=quality,
    )
    player = player or PlaywrightCommandPlayer(page)
    waiter = waiter or PlaywrightReadinessWaiter(page).wait
    lift = await block_writes(page) if read_only else None
    try:
        outcome = await play(
            document, player.play, observers=(camera, *observers), waiter=waiter
        )
    finally:
        if lift is not None:
            await lift()
    if not outcome.ok:
        failures = "; ".join(
            f"step {e.step.id!r} ({e.command.id}): {e.error!r}" for e in outcome.errors
        )
        raise RuntimeError(f"stills not taken: {failures}")
    return camera.shots


def with_posters(
    document: DemoDocument,
    stills: Mapping[str, Union[str, Path]],
    *,
    mime: Optional[str] = None,
) -> DemoDocument:
    """``document`` with each shot step's ``poster`` set to its still (a new document).

    >>> from walkthru.core.schema import Beat, Section, Timing
    >>> doc = DemoDocument(id="d", sections=[Section(id="s", steps=[
    ...     Beat(id="b", beat_kind="pause", timing=Timing(duration_ms=0))])])
    >>> with_posters(doc, {"b": "b.jpg"}).sections[0].steps[0].poster.uri
    'b.jpg'
    """
    sections = []
    for section in document.sections:
        steps = []
        for step in section.steps:
            if step.id in stills:
                path = str(stills[step.id])
                kind = mime or _IMAGE_MIME.get(
                    "png" if path.lower().endswith(".png") else "jpeg"
                )
                step = step.model_copy(update={"poster": AssetRef(uri=path, mime=kind)})
            steps.append(step)
        sections.append(section.model_copy(update={"steps": steps}))
    return document.model_copy(update={"sections": sections})
