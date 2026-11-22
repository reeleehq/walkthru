"""film_document and mux_narration (#46): the filmed-tour driver and the narration mux.

The unit tests inject every effect (player, hand, cue renderer, locator, recorder, pacer); the
last test films a real page in headless Chromium with ffmpeg, and skips without them.
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from walkthru.adapters.playwright import (
    FilmStopped,
    LiveCues,
    arrival_ring,
    film_document,
    mux_narration,
)
from walkthru.core.events import CueBegin, DemoEnd, StepEnter
from walkthru.core.schema import (
    Anchor,
    AssetRef,
    CalloutCue,
    Command,
    CommandStep,
    DemoDocument,
    HighlightCue,
    Locator,
    NarrationAnchor,
    NarrationSegment,
    Rect,
    Section,
    Target,
    Timing,
    Tracks,
)


def _target(css):
    return Target(primary=Locator(strategy="css", value=css))


def _doc(*, cues=(), narration=()):
    steps = [
        CommandStep(
            id=sid,
            command=Command(id="app.do", params={"what": sid}),
            timing=Timing(duration_ms=1000),
        )
        for sid in ("a", "b")
    ]
    return DemoDocument(
        id="tour",
        sections=[Section(id="s", steps=steps)],
        tracks=Tracks(cues=list(cues), narration=list(narration)),
    )


class _Log:
    def __init__(self):
        self.events = []


class _Renderer:
    def __init__(self, log, name="cues"):
        self.log, self.name = log, name

    async def show(self, cue, rect=None):
        self.log.events.append((self.name, "show", cue.id, rect))

    async def clear(self):
        self.log.events.append((self.name, "clear"))


class _Locator:
    def __init__(self, rects):
        self.rects = rects

    async def bounds(self, target):
        try:
            return self.rects[target.primary.value]
        except KeyError:
            raise LookupError(target.primary.value) from None


class _Player:
    def __init__(self, log):
        self.log = log

    async def play(self, command):
        self.log.events.append(("command", command.params["what"]))
        return "done"


class _Hand:
    def __init__(self, log):
        self.log = log

    async def run(self, gesture):
        self.log.events.append(("gesture", *gesture))


class _Recorder:
    def __init__(self, log, out):
        self.log, self.out, self.origin = log, out, None

    async def start(self):
        import time

        self.origin = time.time()
        self.log.events.append(("rec", "start"))

    async def stop(self):
        self.log.events.append(("rec", "stop"))
        return AssetRef(uri=str(self.out), mime="video/mp4")


async def _no_pacing(event):
    return None


def _film(doc, tmp_path, log, *, gestures=None, **kw):
    return asyncio.run(
        film_document(
            doc,
            page=None,
            out=tmp_path / "tour.mp4",
            gestures=gestures,
            hand=_Hand(log),
            player=_Player(log),
            cue_renderer=_Renderer(log),
            locator=_Locator({"#go": Rect(x=1, y=2, width=3, height=4)}),
            recorder=_Recorder(log, tmp_path / "tour.mp4"),
            waiter=lambda condition: None,
            pacer=_no_pacing,
            lead_in_ms=0,
            **kw,
        )
    )


def test_gestures_run_in_order_with_the_command_where_it_falls(tmp_path):
    log = _Log()
    filmed = _film(
        _doc(),
        tmp_path,
        log,
        gestures={"a": [("hover", "#go"), ("command",), ("pause", 100)]},
    )
    assert [e for e in log.events if e[0] in ("gesture", "command")] == [
        ("gesture", "hover", "#go"),
        ("command", "a"),
        ("gesture", "pause", 100),
        ("command", "b"),  # no gestures: just its command
    ]
    assert log.events[0] == ("rec", "start") and log.events[-1] == ("rec", "stop")
    assert filmed.outcome.ok and set(filmed.in_points) == {"a", "b"}
    assert filmed.in_points["a"] <= filmed.in_points["b"]
    assert filmed.recording.uri.endswith("tour.mp4")


def test_cues_are_drawn_after_the_command_and_cleared_when_the_next_step_starts(
    tmp_path,
):
    log = _Log()
    a = Anchor(step_id="a")
    _film(
        _doc(
            cues=[
                HighlightCue(id="ring", anchor=a, target=_target("#go")),
                CalloutCue(id="lost", anchor=a, text="x", target=_target("#nope")),
            ]
        ),
        tmp_path,
        log,
    )
    seq = [e[:3] for e in log.events if e[0] in ("command", "cues")]
    assert seq[: seq.index(("command", "b"))] == [
        ("cues", "clear"),  # step a starts
        ("command", "a"),
        ("cues", "show", "ring"),
        ("cues", "show", "lost"),  # undrawable callout still takes its number
        ("cues", "clear"),  # step b starts: a's cues come down
    ]
    ring = next(e for e in log.events if e[:3] == ("cues", "show", "ring"))
    assert ring[3] == Rect(x=1, y=2, width=3, height=4)


def test_a_failed_step_stops_the_film_and_still_stops_the_recorder(tmp_path):
    log = _Log()

    class Failing(_Player):
        async def play(self, command):
            raise RuntimeError("button gone")

    with pytest.raises(FilmStopped, match="step 'a' failed") as stopped:
        asyncio.run(
            film_document(
                _doc(),
                page=None,
                out=tmp_path / "t.mp4",
                hand=_Hand(log),
                player=Failing(log),
                cue_renderer=_Renderer(log),
                locator=_Locator({}),
                recorder=_Recorder(log, tmp_path / "t.mp4"),
                waiter=lambda c: None,
                pacer=_no_pacing,
                lead_in_ms=0,
            )
        )
    assert log.events[-1] == ("rec", "stop")
    assert stopped.value.filmed.recording is not None  # what was recorded is not lost
    assert isinstance(stopped.value.__cause__, RuntimeError)


def test_gestures_for_a_beat_or_an_unknown_step_are_refused_before_filming(tmp_path):
    from walkthru.core.schema import Beat

    log = _Log()
    doc = _doc()
    doc.sections[0].steps.append(
        Beat(id="pause", beat_kind="pause", timing=Timing(duration_ms=100))
    )
    with pytest.raises(ValueError, match=r"\['pause', 'typo'\]"):
        _film(
            doc, tmp_path, log, gestures={"pause": [("pause", 1)], "typo": [], "a": []}
        )
    assert log.events == []  # the recorder never started


def test_a_hand_and_a_player_may_be_plain_async_callables(tmp_path):
    log = _Log()

    async def hand(gesture):
        log.events.append(("own-gesture", *gesture))

    async def player(command):
        log.events.append(("own-command", command.params["what"]))

    asyncio.run(
        film_document(
            _doc(),
            page=None,
            out=tmp_path / "t.mp4",
            gestures={"a": [("wheel_dialog", 3), ("command",)]},
            hand=hand,
            player=player,
            cue_renderer=_Renderer(log),
            locator=_Locator({}),
            recorder=_Recorder(log, tmp_path / "t.mp4"),
            waiter=lambda c: None,
            pacer=_no_pacing,
            lead_in_ms=0,
        )
    )
    assert [e for e in log.events if e[0].startswith("own")] == [
        ("own-gesture", "wheel_dialog", 3),
        ("own-command", "a"),
        ("own-command", "b"),
    ]


def test_a_recorder_that_fails_to_stop_does_not_hide_the_failed_step(tmp_path):
    log = _Log()

    class Failing(_Player):
        async def play(self, command):
            raise RuntimeError("button gone")

    class Broken(_Recorder):
        async def stop(self):
            raise OSError("no frames")

    with pytest.raises(FilmStopped, match="button gone") as stopped:
        asyncio.run(
            film_document(
                _doc(),
                page=None,
                out=tmp_path / "t.mp4",
                hand=_Hand(log),
                player=Failing(log),
                cue_renderer=_Renderer(log),
                locator=_Locator({}),
                recorder=Broken(log, tmp_path / "t.mp4"),
                waiter=lambda c: None,
                pacer=_no_pacing,
                lead_in_ms=0,
            )
        )
    assert stopped.value.filmed.recording is None


def test_extra_observers_see_the_failure_before_the_film_stops(tmp_path):
    from walkthru.core.events import CommandError

    log = _Log()
    seen = []

    class Failing(_Player):
        async def play(self, command):
            raise RuntimeError("x")

    with pytest.raises(FilmStopped):
        asyncio.run(
            film_document(
                _doc(),
                page=None,
                out=tmp_path / "t.mp4",
                hand=_Hand(log),
                player=Failing(log),
                cue_renderer=_Renderer(log),
                locator=_Locator({}),
                recorder=_Recorder(log, tmp_path / "t.mp4"),
                waiter=lambda c: None,
                pacer=_no_pacing,
                lead_in_ms=0,
                observers=[lambda e: seen.append(type(e))],
            )
        )
    assert CommandError in seen


def test_a_cue_waits_for_its_offset_and_comes_down_after_its_duration():
    log = _Log()
    t = [0.0]

    class Hiding(_Renderer):
        async def hide(self, cue_id):
            self.log.events.append((self.name, "hide", cue_id))

    cues = LiveCues(Hiding(log), _Locator({}), clock=lambda: t[0])
    late = HighlightCue(
        id="late", anchor=Anchor(step_id="a", local_offset_ms=50), duration_ms=30
    )
    now = HighlightCue(id="now", anchor=Anchor(step_id="a"))

    async def go():
        await cues(StepEnter(step=None))
        await cues(CueBegin(now))
        await cues(CueBegin(late))
        assert [e[2] for e in log.events if e[1] == "show"] == ["now"]
        await asyncio.sleep(0.12)  # past the offset and the duration
        await cues(DemoEnd(outcome=None))

    asyncio.run(go())
    assert [e[:3] for e in log.events if e[1] in ("show", "hide")] == [
        ("cues", "show", "now"),
        ("cues", "show", "late"),
        ("cues", "hide", "late"),
    ]


def test_the_arrival_ring_comes_down_with_the_steps_cues():
    log = _Log()
    cues = LiveCues(
        _Renderer(log), _Locator({}), also_clear=(_Renderer(log, "arrive"),)
    )

    async def go():
        await cues(StepEnter(step=None))
        await cues(DemoEnd(outcome=None))

    asyncio.run(go())
    assert log.events.count(("arrive", "clear")) == 2


def test_live_cues_clear_at_the_end_too():
    log = _Log()
    cues = LiveCues(_Renderer(log), _Locator({}))

    async def go():
        await cues(StepEnter(step=None))
        await cues(CueBegin(HighlightCue(id="h", anchor=Anchor(step_id="a"))))
        await cues(DemoEnd(outcome=None))

    asyncio.run(go())
    assert log.events == [
        ("cues", "clear"),
        ("cues", "show", "h", None),
        ("cues", "clear"),
    ]


def test_the_arrival_ring_moves_with_the_pointer():
    log = _Log()
    ring = arrival_ring(
        _Renderer(log, "arrive"), _Locator({"#a": Rect(x=0, y=0, width=1, height=1)})
    )

    async def go():
        await ring(Locator(strategy="css", value="#a"))
        await ring(Locator(strategy="css", value="#gone"))

    asyncio.run(go())
    assert [e[:2] for e in log.events] == [
        ("arrive", "clear"),
        ("arrive", "show"),
        ("arrive", "clear"),  # a target it cannot find: nothing is ringed
    ]


def test_mux_lays_the_narration_at_the_in_point_and_copies_the_picture(tmp_path):
    doc = _doc(
        narration=[
            NarrationSegment(
                id="n",
                text="hi",
                anchor=NarrationAnchor(step_id="a", duration_ms=800),
                audio_ref=AssetRef(uri=str(tmp_path / "n.wav")),
            )
        ]
    )
    calls = []

    def assembler(slots, *, output):
        calls.append(slots)
        output.write_bytes(b"wav")
        return output

    out = mux_narration(
        tmp_path / "tour.mp4",
        doc,
        tmp_path / "final.mp4",
        at_s=0.25,
        assembler=assembler,
        runner=lambda argv, check: calls.append(argv),
    )
    argv = calls[-1]
    assert out == tmp_path / "final.mp4"
    assert (
        argv[argv.index("-af") + 1] == "adelay=250:all=1"
    )  # baked in, not a start time
    assert argv[argv.index("-c:v") + 1] == "copy"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg")
def test_mux_through_real_ffmpeg_starts_the_voice_at_the_in_point(tmp_path):
    import subprocess

    import numpy as np

    video, voice = tmp_path / "v.mp4", tmp_path / "n.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=64x36:r=10:d=3",
         "-pix_fmt", "yuv420p", str(video)], check=True)  # fmt: skip
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:r=48000:d=1",
         str(voice)], check=True)  # fmt: skip
    doc = _doc(
        narration=[
            NarrationSegment(
                id="n",
                text="hi",
                anchor=NarrationAnchor(step_id="a", duration_ms=1000),
                audio_ref=AssetRef(uri=str(voice)),
            )
        ]
    )

    def assembler(slots, *, output):  # the voice alone, at the track's time 0
        import shutil as sh

        sh.copy(voice, output)
        return output

    out = mux_narration(video, doc, tmp_path / "f.mp4", at_s=1.0, assembler=assembler)
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(out), "-f", "s16le", "-ac", "1", "-ar", "8000", "-"],
        capture_output=True, check=True).stdout  # fmt: skip
    a = np.abs(np.frombuffer(pcm, np.int16)).astype(float)
    assert a[: int(0.8 * 8000)].max() < 50  # silence before the in-point
    assert a[int(1.2 * 8000) : int(1.8 * 8000)].mean() > 1000  # the voice after it


def test_mux_refuses_a_document_with_no_narration_audio(tmp_path):
    with pytest.raises(ValueError, match="no narration audio"):
        mux_narration(tmp_path / "v.mp4", _doc(), tmp_path / "o.mp4", at_s=0)


PAGE = """<!doctype html><html><body style="margin:0;font:20px sans-serif">
<button id="go" style="margin:40px;width:160px;height:50px"
  onclick="document.body.style.background='#cfc'">Go</button></body></html>"""


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg")
def test_a_real_page_is_filmed_with_its_gestures_and_cues(tmp_path):
    pytest.importorskip("playwright.async_api")
    from playwright.async_api import async_playwright

    from walkthru.adapters.playwright import screencast_launch_args

    src = tmp_path / "app.html"
    src.write_text(PAGE)
    go = Locator(strategy="css", value="#go")
    doc = DemoDocument(
        id="real",
        sections=[
            Section(
                id="s",
                steps=[
                    CommandStep(
                        id="open",
                        command=Command(id="page.goto", params={"url": src.as_uri()}),
                        timing=Timing(duration_ms=400),
                    ),
                    CommandStep(
                        id="press",
                        command=Command(id="page.click", params={"selector": "#go"}),
                        timing=Timing(duration_ms=600),
                    ),
                ],
            )
        ],
        tracks=Tracks(
            cues=[
                CalloutCue(
                    id="c",
                    anchor=Anchor(step_id="press"),
                    text="Go!",
                    target=Target(primary=go),
                )
            ]
        ),
    )

    async def run():
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(args=screencast_launch_args(1.0))
            except Exception as e:  # browsers not installed
                pytest.skip(f"no Playwright browser available: {e}")
            page = await browser.new_page(viewport={"width": 480, "height": 270})
            try:
                return await film_document(
                    doc,
                    page,
                    tmp_path / "real.mp4",
                    gestures={"press": [("glide", go), ("command",)]},
                    fps=10,
                )
            finally:
                await browser.close()

    filmed = asyncio.run(run())
    assert filmed.outcome.ok
    assert (tmp_path / "real.mp4").stat().st_size > 0
    assert 0 <= filmed.in_points["open"] < filmed.in_points["press"]
    assert filmed.missed_cues == ()
