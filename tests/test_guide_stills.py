"""Annotated stills and the HTML guide (#45): cue overlays, still clips, the guide page.

The unit tests drive fakes; the last test plays a real document in headless Chromium and skips
when no Playwright browser is installed.
"""

from __future__ import annotations

import asyncio
import base64
import re

import pytest

from walkthru.adapters.guide import GuideRenderTarget, guide_html, simple_markdown
from walkthru.adapters.playwright import (
    DomOverlayCueRenderer,
    StillCamera,
    still_clip,
    with_posters,
)
from walkthru.adapters.playwright.player import PlaywrightCommandPlayer
from walkthru.core.events import CueBegin, StepEnter, StepExit
from walkthru.core.schema import (
    Anchor,
    Beat,
    CalloutCue,
    Command,
    CommandStep,
    CursorCue,
    DemoDocument,
    HighlightCue,
    Locator,
    Meta,
    NarrationAnchor,
    NarrationSegment,
    Rect,
    Section,
    SpotlightCue,
    Target,
    Timing,
    Tracks,
)

T0 = Timing(duration_ms=0)


def _target(css: str) -> Target:
    return Target(primary=Locator(strategy="css", value=css))


def _doc(**tracks) -> DemoDocument:
    return DemoDocument(
        id="guide",
        meta=Meta(title="Make a <thing>", description="Three steps."),
        sections=[
            Section(
                id="s1",
                title="Start",
                steps=[
                    Beat(
                        id="intro",
                        beat_kind="textCard",
                        text="Open **Settings**.",
                        timing=T0,
                    ),
                    CommandStep(
                        id="open",
                        command=Command(id="page.goto", params={"url": "about:blank"}),
                        timing=T0,
                    ),
                    CommandStep(
                        id="unshot",
                        command=Command(id="page.wait", params={"ms": 0}),
                        timing=T0,
                    ),
                ],
            ),
            Section(id="s2", title="Empty", steps=[]),
        ],
        tracks=Tracks(**tracks),
    )


class _FakePage:
    def __init__(self, geometry=None):
        self.evaluated, self.shots = [], []
        self.geometry = geometry or {
            "sx": 0,
            "sy": 200,
            "pw": 800,
            "ph": 3000,
            "vw": 800,
            "vh": 600,
        }

    async def evaluate(self, script, arg=None):
        self.evaluated.append(arg)
        return self.geometry if arg is None else None

    async def screenshot(self, **kw):
        self.shots.append(kw)


class _FakeLocator:
    def __init__(self, rects):
        self.rects = rects

    async def bounds(self, target):
        rect = self.rects.get(target.primary.value)
        if rect is None:
            raise LookupError(f"no {target.primary.value}")
        return rect


# --- cue renderer ------------------------------------------------------------------


def test_callouts_are_numbered_in_order_and_clear_restarts_the_count():
    page = _FakePage()
    r = DomOverlayCueRenderer(page)
    a, box = Anchor(step_id="s"), Rect(x=0, y=0, width=1, height=1)

    async def go():
        await r.show(CalloutCue(id="c1", anchor=a, text="one"), box)
        await r.show(HighlightCue(id="h", anchor=a), box)
        await r.show(CalloutCue(id="c2", anchor=a, text="two"), box)
        await r.clear()
        await r.show(CalloutCue(id="c3", anchor=a, text="three"), box)

    asyncio.run(go())
    numbers = [p.get("number") for p in page.evaluated if p["kind"] == "callout"]
    assert numbers == [1, 2, 1]
    assert all(p["text"] is None for p in page.evaluated if p["kind"] == "callout")


def test_a_cursor_cue_draws_nothing_and_a_targeted_cue_resolves_its_rect():
    page = _FakePage()
    locator = _FakeLocator({"#go": Rect(x=5, y=6, width=7, height=8)})
    r = DomOverlayCueRenderer(page, locator=locator, callout_text=True)
    a = Anchor(step_id="s")

    async def go():
        await r.show(CursorCue(id="k", anchor=a, target=_target("#go")))
        await r.show(CalloutCue(id="c", anchor=a, text="Go", target=_target("#go")))

    asyncio.run(go())
    assert [p["kind"] for p in page.evaluated] == ["callout"]
    assert page.evaluated[0]["rect"] == {"x": 5, "y": 6, "width": 7, "height": 8}
    assert page.evaluated[0]["text"] == "Go"


def test_spotlight_opacity_becomes_its_dim_colour():
    page = _FakePage()
    asyncio.run(
        DomOverlayCueRenderer(page).show(
            SpotlightCue(id="s", anchor=Anchor(step_id="s"), opacity=0.3),
            Rect(x=0, y=0, width=1, height=1),
        )
    )
    assert page.evaluated[0]["color"] == "rgba(0,0,0,0.3)"


# --- still clips and the camera ----------------------------------------------------


def test_still_clip_keeps_inside_the_page_and_cuts_to_max_height():
    clip = still_clip(
        [Rect(x=-10, y=0, width=50, height=900)],
        scroll=(0, 0),
        page_size=(800, 2000),
        viewport=(800, 600),
        pad=20,
        max_height=300,
    )
    assert clip == {"x": 0.0, "y": 0.0, "width": 60.0, "height": 300.0}


def test_the_camera_shoots_the_union_of_a_steps_targets_in_page_coordinates(tmp_path):
    page = _FakePage()
    locator = _FakeLocator(
        {
            "#a": Rect(x=100, y=50, width=40, height=20),
            "#b": Rect(x=300, y=80, width=10, height=10),
        }
    )
    renderer = DomOverlayCueRenderer(page, locator=locator)
    cam = StillCamera(
        page=page,
        out_dir=tmp_path,
        renderer=renderer,
        locator=locator,
        pad=10,
        scroll_into_view=False,  # a fake page has no locators to scroll
    )
    step = CommandStep(id="st", command=Command(id="page.wait"), timing=T0)
    a = Anchor(step_id="st")
    cues = [
        HighlightCue(id="h", anchor=a, target=_target("#a")),
        CalloutCue(id="c", anchor=a, text="here", target=_target("#b")),
        CalloutCue(id="lost", anchor=a, text="gone", target=_target("#missing")),
    ]

    async def go():
        await cam(StepEnter(step))
        for cue in cues:
            await cam(CueBegin(cue))
        await cam(StepExit(step))

    asyncio.run(go())
    (shot,) = page.shots
    # union x 100..310, y 50..90; +scrollY 200; pad 10
    assert shot["clip"] == {"x": 90.0, "y": 240.0, "width": 230.0, "height": 60.0}
    assert shot["full_page"] is True and shot["type"] == "jpeg"
    assert cam.shots == {"st": tmp_path / "st.jpg"}
    assert [m[0] for m in cam.missed] == ["lost"]
    assert page.evaluated[-1]["kind"] == "clear"  # overlays gone after the shot


def test_the_camera_skips_steps_its_predicate_declines(tmp_path):
    page = _FakePage()
    cam = StillCamera(
        page=page,
        out_dir=tmp_path,
        renderer=DomOverlayCueRenderer(page),
        locator=_FakeLocator({}),
    )
    card = Beat(id="card", beat_kind="textCard", text="x", timing=T0)

    async def go():
        await cam(StepEnter(card))
        await cam(StepExit(card))

    asyncio.run(go())
    assert page.shots == [] and cam.shots == {}


def test_with_posters_sets_only_the_shot_steps():
    doc = with_posters(_doc(), {"open": "/tmp/open.png"})
    steps = {s.id: s for sec in doc.sections for s in sec.steps}
    assert steps["open"].poster.uri == "/tmp/open.png"
    assert steps["open"].poster.mime == "image/png"
    assert steps["unshot"].poster is None


# --- the guide page ----------------------------------------------------------------


def test_simple_markdown_escapes_before_it_formats():
    assert (
        simple_markdown("<b>x</b> **y**")
        == "<p>&lt;b&gt;x&lt;/b&gt; <strong>y</strong></p>"
    )


def test_a_link_cannot_break_out_of_its_href():
    html = simple_markdown('[x](https://a"onmouseover="alert(1))')
    assert '"onmouseover' not in html


def test_a_code_span_is_not_formatted_further():
    assert (
        simple_markdown("`**a**` **b**")
        == "<p><code>**a**</code> <strong>b</strong></p>"
    )


def test_a_poster_uri_cannot_inject_attributes_and_relative_posters_read_from_base_dir(
    tmp_path,
):
    from walkthru.core.schema import AssetRef

    (tmp_path / "shots").mkdir()
    (tmp_path / "shots" / "open.png").write_bytes(b"png")
    doc = _doc()
    steps = doc.sections[0].steps
    steps[1] = steps[1].model_copy(update={"poster": AssetRef(uri="shots/open.png")})
    steps[2] = steps[2].model_copy(
        update={"poster": AssetRef(uri='https://x/y.png" onerror="alert(1)')}
    )
    html = guide_html(doc, base_dir=tmp_path)
    assert "data:image/png;base64," in html
    assert 'onerror="alert' not in html


def test_guide_lays_out_text_cards_figures_callouts_and_narration(tmp_path):
    png = tmp_path / "open.png"
    png.write_bytes(b"\x89PNG fake")
    a = Anchor(step_id="open")
    doc = _doc(
        cues=[
            CalloutCue(id="c1", anchor=a, text="The menu"),
            CalloutCue(id="c2", anchor=a, text="Its <Save> item"),
        ],
        narration=[
            NarrationSegment(
                id="n",
                text="Open the menu.",
                anchor=NarrationAnchor(step_id="open", duration_ms=1),
            )
        ],
    )
    html = guide_html(doc, {"open": png})
    assert "<h1>Make a &lt;thing&gt;</h1>" in html
    assert "<strong>Settings</strong>" in html
    assert "<h2>Start</h2>" in html and "<h2>Empty</h2>" not in html
    assert html.count("<figure") == 1  # "unshot" has no still
    data = base64.b64encode(png.read_bytes()).decode()
    assert f'src="data:image/png;base64,{data}"' in html
    assert re.search(
        r'<ol class="callouts"><li>The menu</li><li>Its &lt;Save&gt; item</li></ol>',
        html,
    )
    assert '<span class="step-no">1.</span>Open the menu.' in html
    assert "lazy" not in html


def test_the_target_writes_one_file_and_falls_back_to_posters(tmp_path):
    jpg = tmp_path / "p.jpg"
    jpg.write_bytes(b"\xff\xd8 fake")
    doc = with_posters(_doc(), {"open": jpg})
    out = tmp_path / "out" / "guide.html"
    ref = asyncio.run(GuideRenderTarget(out).export(doc))
    assert ref.uri == str(out) and ref.mime == "text/html"
    assert "data:image/jpeg;base64," in out.read_text()


def test_callout_numbers_count_callouts_that_could_not_be_drawn(tmp_path):
    """A callout whose target is missing, or that has none, still takes its number, so badge
    n stays next to the guide's list item n."""
    page = _FakePage()
    locator = _FakeLocator({"#b": Rect(x=0, y=0, width=10, height=10)})
    renderer = DomOverlayCueRenderer(page, locator=locator)
    cam = StillCamera(
        page=page,
        out_dir=tmp_path,
        renderer=renderer,
        locator=locator,
        scroll_into_view=False,
    )
    step = CommandStep(id="st", command=Command(id="page.wait"), timing=T0)
    a = Anchor(step_id="st")

    async def go():
        await cam(StepEnter(step))
        for cue in (
            CalloutCue(id="gone", anchor=a, text="one", target=_target("#missing")),
            CalloutCue(id="free", anchor=a, text="two"),
            CalloutCue(id="here", anchor=a, text="three", target=_target("#b")),
        ):
            await cam(CueBegin(cue))

    asyncio.run(go())
    drawn = [p for p in page.evaluated if p and p.get("kind") == "callout"]
    assert [p["number"] for p in drawn] == [3]


def test_the_camera_stops_shooting_at_the_first_failed_command(tmp_path):
    from walkthru.core.events import CommandError

    page = _FakePage()
    cam = StillCamera(
        page=page,
        out_dir=tmp_path,
        renderer=DomOverlayCueRenderer(page),
        locator=_FakeLocator({}),
        scroll_into_view=False,
    )
    bad = CommandStep(id="bad", command=Command(id="page.click"), timing=T0)
    after = CommandStep(id="after", command=Command(id="page.wait"), timing=T0)

    async def go():
        await cam(StepEnter(bad))
        await cam(CommandError(bad.command, RuntimeError("no"), bad))
        await cam(StepExit(bad))
        await cam(StepEnter(after))
        await cam(StepExit(after))

    asyncio.run(go())
    assert cam.shots == {} and cam.failed is not None and page.shots == []


# --- page.fill: `value` is accepted (#45's trap) -----------------------------------


def test_fill_accepts_playwrights_own_name_for_the_text():
    class Page:
        def __init__(self):
            self.calls = []

        async def fill(self, selector, value, **kw):
            self.calls.append((selector, value, kw))

    page = Page()
    player = PlaywrightCommandPlayer(page)
    asyncio.run(
        player.play(Command(id="page.fill", params={"selector": "#q", "value": "hi"}))
    )
    asyncio.run(
        player.play(Command(id="page.fill", params={"selector": "#q", "text": "yo"}))
    )
    assert page.calls == [("#q", "hi", {}), ("#q", "yo", {})]
    with pytest.raises(ValueError, match="not both"):
        asyncio.run(
            player.play(
                Command(
                    id="page.fill", params={"selector": "#q", "text": "a", "value": "b"}
                )
            )
        )


# --- end to end, in a real browser -------------------------------------------------

PAGE_HTML = """<!doctype html><html><body style="margin:0;font:16px sans-serif;background:#fff">
<div style="height:900px"></div>
<button id="save" style="position:absolute;left:40px;top:1000px;width:120px;height:40px">Save</button>
<form id="f" method="post" action="/never"></form>
<div style="height:1500px"></div></body></html>"""


async def _with_page(fn):
    """Run ``fn(page)`` in headless Chromium, or skip without a Playwright browser."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as e:  # browsers not installed (`playwright install chromium`)
            pytest.skip(f"no Playwright browser available: {e}")
        page = await browser.new_page(viewport={"width": 800, "height": 600})
        try:
            return await fn(page)
        finally:
            await browser.close()


def test_block_writes_aborts_posts_and_lets_gets_reach_other_routes():
    """Non-GET requests of the whole context are aborted (a popup's too), and a GET goes on
    to the context's other route handlers (fallback, not continue)."""
    pytest.importorskip("playwright.async_api")
    from walkthru.adapters.playwright import block_writes

    async def go(page):
        async def mock(route):
            await route.fulfill(
                status=200, body="<p id=ok>mocked</p>", content_type="text/html"
            )

        await page.context.route("https://app.invalid/**", mock)
        lift = await block_writes(page)
        try:
            await page.goto("https://app.invalid/home")
            got = await page.text_content("#ok")
            posted = await page.evaluate(
                """() => fetch('https://app.invalid/save', {method: 'POST'})
                     .then(() => 'sent', () => 'blocked')"""
            )
            async with page.context.expect_page() as popup_info:
                await page.evaluate(
                    "() => { window.open('https://app.invalid/home'); }"
                )
            popup = await popup_info.value
            await popup.wait_for_load_state()
            popup_posted = await popup.evaluate(
                """() => fetch('https://app.invalid/x', {method: 'POST'})
                     .then(() => 'sent', () => 'blocked')"""
            )
        finally:
            await lift()
        return got, posted, popup_posted

    assert asyncio.run(_with_page(go)) == ("mocked", "blocked", "blocked")


INNER_SCROLL_HTML = """<!doctype html><html><body style="margin:0">
<div id="pane" style="height:300px;overflow:auto">
  <div style="height:2000px"></div>
  <button id="deep" style="display:block;width:100px;height:30px">Deep</button>
  <div style="height:500px"></div>
</div></body></html>"""


def test_a_target_inside_a_scrolled_container_is_brought_into_view(tmp_path):
    pytest.importorskip("playwright.async_api")
    Image = pytest.importorskip("PIL.Image")
    from walkthru.adapters.playwright import play_stills

    src = tmp_path / "inner.html"
    src.write_text(INNER_SCROLL_HTML)
    a = Anchor(step_id="open")
    doc = DemoDocument(
        id="inner",
        sections=[
            Section(
                id="s",
                steps=[
                    CommandStep(
                        id="open",
                        command=Command(id="page.goto", params={"url": src.as_uri()}),
                        timing=T0,
                    )
                ],
            )
        ],
        tracks=Tracks(cues=[HighlightCue(id="h", anchor=a, target=_target("#deep"))]),
    )

    async def go(page):
        return await play_stills(doc, page, tmp_path / "s", image_format="png", pad=10)

    shots = asyncio.run(_with_page(go))
    assert Image.open(shots["open"]).size == (110, 50)  # x=0: the left pad is clamped


def test_export_pdf_prints_the_written_guide(tmp_path):
    pytest.importorskip("pdfdol.chromium")
    pytest.importorskip("playwright.sync_api")
    jpg = tmp_path / "p.jpg"
    jpg.write_bytes(b"\xff\xd8 fake")
    target = GuideRenderTarget(tmp_path / "g.html", stills={"open": jpg})

    async def go():
        await target.export(_doc())
        try:
            return await target.export_pdf(tmp_path / "g.pdf")
        except Exception as e:  # noqa: BLE001 - no Chromium for pdfdol here
            if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                pytest.skip(f"no Playwright browser available: {e}")
            raise

    pdf = asyncio.run(go())
    assert pdf.read_bytes().startswith(b"%PDF")


def test_play_stills_draws_cues_on_a_real_page_and_the_guide_inlines_them(tmp_path):
    pytest.importorskip("playwright.async_api")
    Image = pytest.importorskip("PIL.Image")
    from playwright.async_api import async_playwright

    from walkthru.adapters.playwright import play_stills

    src = tmp_path / "app.html"
    src.write_text(PAGE_HTML)
    a = Anchor(step_id="save")
    doc = DemoDocument(
        id="e2e",
        sections=[
            Section(
                id="s",
                steps=[
                    CommandStep(
                        id="open",
                        command=Command(id="page.goto", params={"url": src.as_uri()}),
                        timing=T0,
                    ),
                    CommandStep(
                        id="save",
                        command=Command(id="page.scroll", params={"selector": "#save"}),
                        timing=T0,
                    ),
                ],
            )
        ],
        tracks=Tracks(
            cues=[
                HighlightCue(
                    id="h",
                    anchor=a,
                    target=_target("#save"),
                    color="#00ff00",
                    thickness=6,
                ),
                CalloutCue(id="c", anchor=a, text="Save it", target=_target("#save")),
            ]
        ),
    )

    async def go():
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch()
            except (
                Exception
            ) as e:  # browsers not installed (`playwright install chromium`)
                pytest.skip(f"no Playwright browser available: {e}")
            page = await browser.new_page(viewport={"width": 800, "height": 600})
            try:
                shots = await play_stills(
                    doc,
                    page,
                    tmp_path / "stills",
                    image_format="png",
                    pad=20,
                    read_only=True,
                )
                layer_left = await page.evaluate(
                    "() => document.getElementById('walkthru-cue-layer')"
                )
            finally:
                await browser.close()
            return shots, layer_left

    shots, layer_left = asyncio.run(go())
    assert set(shots) == {"open", "save"}
    assert layer_left is None  # overlays cleared after each still
    img = Image.open(shots["save"]).convert("RGB")
    # the clip is the button (120x40) padded by 20, highlight ring padding 6 + thickness 6
    assert img.size == (160, 80)
    ring = img.getpixel((20 - 6 + 2, 40))  # left edge of the ring, mid-height
    assert ring == (0, 255, 0)
    html = guide_html(doc, shots)
    assert html.count("data:image/png;base64,") == 2
    assert "<li>Save it</li>" in html
