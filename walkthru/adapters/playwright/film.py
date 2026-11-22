"""Film a Demo Document: play it on a page in real time, with live cues, while recording it.

The pieces were all here — :class:`~walkthru.observers.pacing.WallClockPacer` (each step at its
authored moment), :class:`~walkthru.observers.marks.StepMarks` (when each step really started),
:class:`~walkthru.adapters.playwright.screencast.CdpScreencastRecorder` (the screen as a
constant-rate mp4), :class:`~walkthru.adapters.playwright.gestures.Hand` (a pointer that glides,
clicks and scrolls visibly) and :class:`~walkthru.adapters.playwright.cues.DomOverlayCueRenderer`
(cues drawn on the page) — but every filmed tour composed them by hand. :func:`film_document`
is that composition:

- each command step runs its **gestures** when it has some (``gestures={step_id: [("hover",
  target), ("command",), ...]}``; ``("command",)`` runs the step's own command where it falls
  among them, and a list without it does not run the command at all: a scroll-only shot), else
  its command, through the ``player``. Gestures keyed to a step that is not a command step (a
  beat, a typo) are refused before anything is filmed;
- its **cues** are drawn once the step's effect is on screen (the engine announces them after the
  command and its readiness gate), each at its anchor's ``localOffsetMs`` into the step when that
  moment is later, taken down after its ``durationMs`` when it has one, else when the next step
  starts, so they are filmed through the step's hold; with ``ring_on_arrive`` the pointer also
  rings each target it reaches (in its own colour), cleared with the step's cues;
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

import asyncio
import subprocess
import time
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
    CommandStep,
    DemoDocument,
    HighlightCue,
    Target,
)

if TYPE_CHECKING:  # type-checker only
    from walkthru.ports import CueRenderer, ElementLocator

__all__ = [
    "ARRIVE_COLOR",
    "ARRIVE_LAYER_ID",
    "COMMAND_GESTURE",
    "DEFAULT_LEAD_IN_MS",
    "Filmed",
    "FilmStopped",
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
#: The arrival ring's colour: not the cues' orange, so a viewer does not read it as a cue.
ARRIVE_COLOR = "#3b82f6"


@dataclass
class LiveCues:
    """An engine observer drawing each step's cues on time while a document is filmed.

    A cue is shown at its anchor's ``localOffsetMs`` into the step (at once when the step's
    effect arrived later than that), hidden after its ``durationMs`` when it has one (needs a
    renderer with ``hide(cue_id)``), and every overlay comes down when the next step starts and
    at the end; ``also_clear`` are more renderers cleared then (the arrival ring's). Put it
    after the pacer in the observer list, so "the next step starts" is when the pacer releases it.
    """

    renderer: "CueRenderer"
    locator: Optional["ElementLocator"] = None
    also_clear: Sequence["CueRenderer"] = ()
    clock: Callable[[], float] = time.monotonic
    missed: list[tuple[str, str]] = field(default_factory=list)
    _step_t0: float = field(default=0.0, init=False, repr=False)
    _pending: list[asyncio.Task] = field(default_factory=list, init=False, repr=False)

    async def __call__(self, event: Any) -> None:
        if isinstance(event, (StepEnter, DemoEnd)):
            for task in self._pending:
                task.cancel()
            self._pending.clear()
            for renderer in (self.renderer, *self.also_clear):
                await renderer.clear()
            self._step_t0 = self.clock()
        elif isinstance(event, CueBegin):
            await self._cue(event.cue)

    async def _cue(self, cue) -> None:
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
        elapsed = self.clock() - self._step_t0
        wait = cue.anchor.local_offset_ms / 1000 - elapsed
        if wait > 0:
            self._pending.append(asyncio.ensure_future(self._later(wait, cue, rect)))
        else:
            await self._show(cue, rect)

    async def _later(self, wait: float, cue, rect) -> None:
        await asyncio.sleep(wait)
        await self._show(cue, rect)

    async def _show(self, cue, rect) -> None:
        await self.renderer.show(cue, rect)
        hide = getattr(self.renderer, "hide", None)
        if cue.duration_ms is not None and hide is not None:

            async def take_down():
                await asyncio.sleep(cue.duration_ms / 1000)
                await hide(cue.id)

            self._pending.append(asyncio.ensure_future(take_down()))


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

    recording: Optional[AssetRef]
    #: Seconds into the recording at which each step started.
    in_points: dict[str, float]
    #: Steps that ran past their slot, and by how many milliseconds.
    overruns_ms: dict[str, int]
    outcome: Optional[Outcome]
    #: ``(cue_id, why)`` for cues whose target was not on screen.
    missed_cues: tuple[tuple[str, str], ...] = ()


class FilmStopped(RuntimeError):
    """A step failed and stopped the film. ``filmed`` is what was recorded up to it (its
    ``recording`` is ``None`` when the recorder could not finish the take either)."""

    def __init__(self, message: str, *, filmed: Filmed):
        super().__init__(message)
        self.filmed = filmed


def _check_gestures(document: DemoDocument, gestures: Mapping[str, Any]) -> None:
    """Refuse gestures the engine would never run: keyed to a beat, or to no step."""
    commands = {
        step.id
        for section in document.sections
        for step in section.steps
        if isinstance(step, CommandStep)
    }
    stray = sorted(set(gestures) - commands)
    if stray:
        raise ValueError(
            f"gestures for {stray}, which are not command steps of this document (a beat "
            "runs no command, so its gestures would never run; or the id is misspelt)"
        )


def _runner(obj: Any, method: str) -> Callable:
    """``obj.<method>`` when it has one, else ``obj`` itself (any async callable)."""
    return getattr(obj, method, obj)


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
        gestures: ``{command_step_id: [gesture, ...]}``, each gesture run by ``hand``, plus
            ``("command",)`` for the step's own command (a list without it does not run the
            command). A step without gestures runs its command.
        hand: runs a gesture: a :class:`~walkthru.adapters.playwright.gestures.Hand` (its
            ``run``) or any async callable ``(gesture) -> None`` (an app's own vocabulary).
            Default a ``Hand`` on ``page`` whose ``on_arrive`` rings each target when
            ``ring_on_arrive`` (a ``hand`` you pass rings only if you gave it that hook).
        player: runs commands: anything with an async ``play(command)``, or an async callable
            ``(command)``. Default a ``PlaywrightCommandPlayer`` on ``page``.
        cue_renderer: draws the document's cues; default a ``DomOverlayCueRenderer`` (callout text
            drawn beside its badge when ``callout_text``).
        locator: resolves cue targets; default a ``PlaywrightElementLocator``.
        recorder: default a ``CdpScreencastRecorder(page, save_as=out, fps=fps)`` (Chromium only;
            launch the browser with
            :func:`~walkthru.adapters.playwright.screencast.screencast_launch_args`). A recorder
            you pass writes where it was told; ``out`` and ``fps`` are then unused.
        waiter: resolves ``waitFor`` gates; default a ``PlaywrightReadinessWaiter``.
        pacer: default a :class:`~walkthru.observers.pacing.WallClockPacer` (real time).
        lead_in_ms: recorded before the first step (a first frame at video time 0).
        stop_on_error: a failing command stops the film (:class:`FilmStopped`, carrying what was
            recorded). Without it the film goes on; the failed step's cues are still drawn.
        observers: more observers, run after the pacer, the marks and the cues, and before the
            failure check (so they see a :class:`~walkthru.core.events.CommandError`).

    Raises:
        ValueError: ``gestures`` names a step that is not a command step.
        FilmStopped: a step failed and ``stop_on_error``.
    """
    from walkthru.adapters.playwright.cues import DomOverlayCueRenderer
    from walkthru.adapters.playwright.gestures import Hand
    from walkthru.adapters.playwright.locator import PlaywrightElementLocator
    from walkthru.adapters.playwright.player import PlaywrightCommandPlayer
    from walkthru.adapters.playwright.readiness import PlaywrightReadinessWaiter
    from walkthru.adapters.playwright.screencast import CdpScreencastRecorder
    from walkthru.core.engine import play
    from walkthru.observers import StepMarks, WallClockPacer

    gestures = dict(gestures or {})
    _check_gestures(document, gestures)
    locator = locator or PlaywrightElementLocator(page)
    cue_renderer = cue_renderer or DomOverlayCueRenderer(
        page, locator=locator, callout_text=callout_text
    )
    also_clear: list = []
    if hand is None:
        on_arrive = None
        if ring_on_arrive:
            ring = DomOverlayCueRenderer(
                page, layer=ARRIVE_LAYER_ID, color=ARRIVE_COLOR
            )
            on_arrive = arrival_ring(ring, locator)
            also_clear.append(ring)
        hand = Hand(page, on_arrive=on_arrive)
    run_gesture = _runner(hand, "run")
    run_command = _runner(player or PlaywrightCommandPlayer(page), "play")
    waiter = waiter or PlaywrightReadinessWaiter(page).wait
    pacer = pacer or WallClockPacer()
    if recorder is None:
        kw = {} if fps is None else {"fps": fps}
        recorder = CdpScreencastRecorder(page, save_as=Path(out), **kw)

    current: dict[str, str] = {}
    failure: dict[str, CommandError] = {}

    def track(event):
        if isinstance(event, StepEnter):
            current["step"] = event.step.id
        elif isinstance(event, CommandError) and stop_on_error:
            failure["error"] = event
            raise FilmStopped(
                f"step {event.step.id!r} failed: {event.error!r}",
                filmed=Filmed(None, {}, {}, None),
            ) from event.error

    async def executor(command):
        ops = gestures.get(current.get("step", ""))
        if not ops:
            return await run_command(command)
        result = None
        for op in ops:
            if op[0] == COMMAND_GESTURE:
                result = await run_command(command)
            else:
                await run_gesture(op)
        return result

    marks = StepMarks()
    cues = LiveCues(cue_renderer, locator, also_clear=tuple(also_clear))
    outcome: Optional[Outcome] = None
    recording: Optional[AssetRef] = None
    await recorder.start()
    try:
        await asyncio.sleep(lead_in_ms / 1000)
        outcome = await play(
            document,
            executor,
            observers=(pacer, marks, cues, *observers, track),
            waiter=waiter,
        )
    except FilmStopped as stopped:
        try:
            recording = await recorder.stop()
        except Exception:  # noqa: BLE001 - the failed step stays the error the caller sees
            recording = None
        raise FilmStopped(
            str(stopped),
            filmed=_filmed(
                document, recording, marks, recorder, None, cues, overrun_tolerance_ms
            ),
        ) from failure["error"].error
    except BaseException:
        try:
            await recorder.stop()
        except Exception:  # noqa: BLE001 - keep the original error primary
            pass
        raise
    recording = await recorder.stop()
    return _filmed(
        document, recording, marks, recorder, outcome, cues, overrun_tolerance_ms
    )


def _filmed(document, recording, marks, recorder, outcome, cues, tolerance) -> Filmed:
    from walkthru.core.timeline import resolve_timeline
    from walkthru.observers import in_points, overruns

    try:
        points = in_points(marks.starts, origin=recorder.origin)
    except Exception:  # noqa: BLE001 - a recorder that never started has no origin
        points = {}
    return Filmed(
        recording=recording,
        in_points=points,
        overruns_ms=overruns(
            resolve_timeline(document),
            marks.starts,
            end=marks.end,
            tolerance_ms=tolerance,
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
    normally ``Filmed.in_points[first_step_id]``. The delay is baked into the audio (silence
    before it), so a player that ignores a stream's start time still plays it in sync; the
    picture is copied, not re-encoded.

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
    delay_ms = max(0, round(at_s * 1000))
    argv = [
        ffmpeg, "-v", "error", "-y",
        "-i", str(video),
        "-i", str(track),
        "-map", "0:v:0", "-map", "1:a:0",
        "-af", f"adelay={delay_ms}:all=1",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        str(out),
    ]  # fmt: skip
    runner(argv, check=True)
    return out
