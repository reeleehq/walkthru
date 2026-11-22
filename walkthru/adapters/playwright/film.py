"""Film a Demo Document: play it on a page in real time, with live cues, while recording it.

The pieces were all here — :class:`~walkthru.observers.pacing.WallClockPacer` (each step at its
authored moment), :class:`~walkthru.observers.marks.StepMarks` (when each step really started),
:class:`~walkthru.adapters.playwright.screencast.CdpScreencastRecorder` (the screen as a
constant-rate mp4), :class:`~walkthru.adapters.playwright.gestures.Hand` (a pointer that glides,
clicks and scrolls visibly) and :class:`~walkthru.adapters.playwright.cues.DomOverlayCueRenderer`
(cues drawn on the page) — but every filmed tour composed them by hand. :func:`film_document`
is that composition:

- each step runs its **gestures** when it has some (``gestures={step_id: [("hover", target),
  ("command",), ...]}``; ``("command",)`` runs the step's own command where it falls among
  them), else its command, through the ``player``;
- its **cues** are drawn once the step's effect is on screen (the engine announces them after the
  command and its readiness gate) and stay up until the next step starts, so they are filmed for
  the step's whole hold; with ``ring_on_arrive`` the pointer also rings each target it reaches;
- the result carries the recording, each step's **in-point** (seconds into the recording) and the
  steps that **overran** their slot.

:func:`mux_narration` then lays the document's assembled narration under the recording, starting
at the first step's in-point (the narration track's time 0).

Limits: an overlay is placed when it is drawn, so a cue on an element that then moves (a fixed
header under a scroll, an inner scroller, a layout shift) stays where the element was; draw it
after the move (a later step, or the gesture's ``on_arrive``). A custom ``recorder`` needs
``start()``, ``stop() -> AssetRef`` and ``origin`` (the wall-clock time of video time 0).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from walkthru.core.events import CommandError, CueBegin, DemoEnd, Outcome, StepEnter
from walkthru.core.schema import (
    Anchor,
    AssetRef,
    CalloutCue,
    DemoDocument,
    HighlightCue,
    Target,
)

if TYPE_CHECKING:  # type-checker only
    from walkthru.ports import CueRenderer, ElementLocator

__all__ = [
    "COMMAND_GESTURE",
    "DEFAULT_LEAD_IN_MS",
    "ARRIVE_LAYER_ID",
    "Filmed",
    "LiveCues",
    "arrival_ring",
    "film_document",
    "mux_narration",
]

#: The gesture that runs the step's own command, where it falls among its gestures.
COMMAND_GESTURE = "command"
#: Recorded before the first step, so the film has a frame at time 0 and the first step's
#: in-point is after it.
DEFAULT_LEAD_IN_MS = 200
#: The overlay layer of the arrival ring (separate from the cues', so each clears on its own).
ARRIVE_LAYER_ID = "walkthru-arrive-layer"


@dataclass
class LiveCues:
    """An engine observer drawing each step's cues as the engine announces them, and clearing
    them when the next step starts (and at the end), so they stay up through the step's hold.

    Put it after the pacer in the observer list: the clear on the next ``StepEnter`` then happens
    once the pacer has released that step, not as soon as the previous one ended.
    """

    renderer: "CueRenderer"
    locator: Optional["ElementLocator"] = None
    missed: list[tuple[str, str]] = field(default_factory=list)

    async def __call__(self, event: Any) -> None:
        if isinstance(event, (StepEnter, DemoEnd)):
            await self.renderer.clear()
        elif isinstance(event, CueBegin):
            cue = event.cue
            rect = None
            if cue.target is not None and self.locator is not None:
                try:
                    rect = await self.locator.bounds(cue.target)
                except LookupError as e:  # not on screen: film without it
                    self.missed.append((cue.id, str(e)))
                    if isinstance(cue, CalloutCue):  # it keeps its number all the same
                        try:
                            await self.renderer.show(cue, None)
                        except LookupError:
                            pass
                    return
            await self.renderer.show(cue, rect)


def arrival_ring(
    renderer: "CueRenderer", locator: "ElementLocator"
) -> Callable[[Any], Any]:
    """A :class:`~walkthru.adapters.playwright.gestures.Hand` ``on_arrive`` hook that rings each
    target the pointer reaches (and lets go of the previous ring). A target the locator cannot
    find is not ringed."""

    async def on_arrive(target: Any) -> None:
        await renderer.clear()
        wrapped = target if isinstance(target, Target) else Target(primary=target)
        try:
            rect = await locator.bounds(wrapped)
        except LookupError:
            return
        await renderer.show(
            HighlightCue(id="arrive", anchor=Anchor(step_id="arrive")), rect
        )

    return on_arrive


@dataclass(frozen=True)
class Filmed:
    """What :func:`film_document` made."""

    recording: AssetRef
    #: Seconds into the recording at which each step started.
    in_points: dict[str, float]
    #: Steps that ran past their slot, and by how many milliseconds.
    overruns_ms: dict[str, int]
    outcome: Outcome
    #: ``(cue_id, why)`` for cues whose target was not on screen.
    missed_cues: tuple[tuple[str, str], ...] = ()


async def film_document(
    document: DemoDocument,
    page: Any,
    out: Union[str, Path],
    *,
    gestures: Optional[Mapping[str, Sequence[tuple]]] = None,
    hand: Optional[Any] = None,
    player: Optional[Any] = None,
    cue_renderer: Optional["CueRenderer"] = None,
    locator: Optional["ElementLocator"] = None,
    recorder: Optional[Any] = None,
    waiter: Optional[Callable] = None,
    pacer: Optional[Callable] = None,
    ring_on_arrive: bool = True,
    callout_text: bool = True,
    lead_in_ms: int = DEFAULT_LEAD_IN_MS,
    fps: Optional[int] = None,
    stop_on_error: bool = True,
    overrun_tolerance_ms: int = 150,
    observers: Iterable[Callable] = (),
) -> Filmed:
    """Film ``document`` on ``page`` into ``out`` (an mp4): paced, gestured, cued, recorded.

    Args:
        gestures: ``{step_id: [gesture, ...]}``, each gesture as
            :meth:`~walkthru.adapters.playwright.gestures.Hand.run` takes it, plus
            ``("command",)`` for the step's own command. A step without gestures runs its command.
        hand: the :class:`~walkthru.adapters.playwright.gestures.Hand`; default one on ``page``
            whose ``on_arrive`` rings each target when ``ring_on_arrive``.
        player: runs commands; default a ``PlaywrightCommandPlayer`` on ``page``.
        cue_renderer: draws the document's cues; default a ``DomOverlayCueRenderer`` (callout text
            drawn beside its badge when ``callout_text``).
        locator: resolves cue targets; default a ``PlaywrightElementLocator``.
        recorder: default a ``CdpScreencastRecorder(page, save_as=out)`` (Chromium only; launch the
            browser with :func:`~walkthru.adapters.playwright.screencast.screencast_launch_args`).
        waiter: resolves ``waitFor`` gates; default a ``PlaywrightReadinessWaiter``.
        pacer: default a :class:`~walkthru.observers.pacing.WallClockPacer` (real time).
        lead_in_ms: recorded before the first step (a first frame at video time 0).
        stop_on_error: a failing command stops the film (raises) rather than filming on.
        observers: more observers, run after the pacer, the marks and the cues.

    Raises:
        RuntimeError: a step's command or gesture failed and ``stop_on_error``.
    """
    import asyncio

    from walkthru.adapters.playwright.cues import DomOverlayCueRenderer
    from walkthru.adapters.playwright.gestures import Hand
    from walkthru.adapters.playwright.locator import PlaywrightElementLocator
    from walkthru.adapters.playwright.player import PlaywrightCommandPlayer
    from walkthru.adapters.playwright.readiness import PlaywrightReadinessWaiter
    from walkthru.adapters.playwright.screencast import CdpScreencastRecorder
    from walkthru.core.engine import play
    from walkthru.core.timeline import resolve_timeline
    from walkthru.observers import StepMarks, WallClockPacer, in_points, overruns

    gestures = dict(gestures or {})
    locator = locator or PlaywrightElementLocator(page)
    cue_renderer = cue_renderer or DomOverlayCueRenderer(
        page, locator=locator, callout_text=callout_text
    )
    if hand is None:
        on_arrive = (
            arrival_ring(DomOverlayCueRenderer(page, layer=ARRIVE_LAYER_ID), locator)
            if ring_on_arrive
            else None
        )
        hand = Hand(page, on_arrive=on_arrive)
    player = player or PlaywrightCommandPlayer(page)
    waiter = waiter or PlaywrightReadinessWaiter(page).wait
    pacer = pacer or WallClockPacer()
    kw = {} if fps is None else {"fps": fps}
    recorder = recorder or CdpScreencastRecorder(page, save_as=Path(out), **kw)

    current: dict[str, str] = {}

    def track(event):
        if isinstance(event, StepEnter):
            current["step"] = event.step.id
        elif isinstance(event, CommandError) and stop_on_error:
            raise RuntimeError(f"step {event.step.id!r} failed: {event.error!r}") from (
                event.error
            )

    async def executor(command):
        ops = gestures.get(current.get("step", ""))
        if not ops:
            return await player.play(command)
        result = None
        for op in ops:
            if op[0] == COMMAND_GESTURE:
                result = await player.play(command)
            else:
                await hand.run(op)
        return result

    marks = StepMarks()
    cues = LiveCues(cue_renderer, locator)
    await recorder.start()
    try:
        await asyncio.sleep(lead_in_ms / 1000)
        outcome = await play(
            document,
            executor,
            observers=(pacer, marks, cues, track, *observers),
            waiter=waiter,
        )
    finally:
        recording = await recorder.stop()
    timeline = resolve_timeline(document)
    return Filmed(
        recording=recording,
        in_points=in_points(marks.starts, origin=recorder.origin),
        overruns_ms=overruns(
            timeline, marks.starts, end=marks.end, tolerance_ms=overrun_tolerance_ms
        ),
        outcome=outcome,
        missed_cues=tuple(cues.missed),
    )


def mux_narration(
    video: Union[str, Path],
    document: DemoDocument,
    out: Union[str, Path],
    *,
    at_s: float,
    audio_path: Optional[Union[str, Path]] = None,
    assembler: Optional[Callable] = None,
    ffmpeg: str = "ffmpeg",
    runner: Callable[..., Any] = subprocess.run,
) -> Path:
    """``video`` with ``document``'s narration under it, starting ``at_s`` into the video.

    The narration track is assembled from the document's timeline
    (:func:`~walkthru.narration.assemble.assemble_narration_audio`, segment ``audioRef``\\ s
    placed at their timeline times); its time 0 is the first step's start, so ``at_s`` is
    normally ``Filmed.in_points[first_step_id]``. The picture is copied, not re-encoded.

    Raises:
        ValueError: the document has no narration audio to lay down.
    """
    from walkthru.core.timeline import resolve_timeline
    from walkthru.narration.assemble import assemble_narration_audio

    out = Path(out)
    audio = Path(audio_path) if audio_path else out.with_suffix(".narration.wav")
    track = assemble_narration_audio(
        resolve_timeline(document), audio, assembler=assembler
    )
    if track is None:
        raise ValueError(
            "the document has no narration audio (no segment has an audioRef)"
        )
    argv = [
        ffmpeg, "-v", "error", "-y",
        "-i", str(video),
        "-itsoffset", f"{max(0.0, at_s):.3f}", "-i", str(track),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        str(out),
    ]  # fmt: skip
    runner(argv, check=True)
    return out
