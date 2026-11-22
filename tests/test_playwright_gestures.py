"""The gesture vocabulary, against a recording fake page (no browser)."""

import asyncio

import pytest

from walkthru.adapters.playwright import GESTURES, Hand


class _Rec:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def __getattr__(self, method):
        async def call(*args, **kw):
            self.log.append((self.name, method, args, kw))

        return call


class FakeElement:
    def __init__(self, log, box):
        self.log, self.box = log, box
        self.first = self

    async def scroll_into_view_if_needed(self):
        self.log.append(("el", "scroll_into_view", (), {}))

    async def bounding_box(self):
        return self.box


class FakePage:
    def __init__(self, box={"x": 100, "y": 200, "width": 40, "height": 20}):
        self.log = []
        self.mouse = _Rec(self.log, "mouse")
        self.keyboard = _Rec(self.log, "keyboard")
        self.box = box

    async def wait_for_timeout(self, ms):
        self.log.append(("page", "wait", (ms,), {}))

    async def evaluate(self, js):
        self.log.append(("page", "evaluate", (js,), {}))

    async def wait_for_function(self, js, timeout=None):
        self.log.append(("page", "settle", (), {}))


def _hand(page, **kw):
    return Hand(page, locator_of=lambda t: FakeElement(page.log, page.box), **kw)


def test_glide_moves_to_element_center_and_announces_arrival():
    page, arrived = FakePage(), []

    async def on_arrive(target):
        arrived.append(target)

    pos = asyncio.run(_hand(page, glide_steps=7, on_arrive=on_arrive).glide("t"))
    assert pos == (120, 210)
    assert ("mouse", "move", (120, 210), {"steps": 7}) in page.log
    assert arrived == ["t"]


def test_click_rests_then_presses_and_type_types():
    page = FakePage()
    asyncio.run(_hand(page, aim_ms=5, press_ms=6, type_delay_ms=9).run(("type", "t", "hi")))
    kinds = [(n, m) for n, m, *_ in page.log]
    assert kinds.index(("mouse", "down")) < kinds.index(("mouse", "up")) < kinds.index(("keyboard", "type"))
    assert ("page", "wait", (5,), {}) in page.log and ("page", "wait", (6,), {}) in page.log
    assert ("keyboard", "type", ("hi",), {"delay": 9}) in page.log


def test_scroll_gestures_settle_and_scroll_into_uses_the_pad():
    page = FakePage()
    hand = _hand(page, scroll_pad=50)
    asyncio.run(hand.run_all([("scroll_top",), ("scroll_by", 400), ("scroll_into", "t")]))
    scripts = [a[0] for n, m, a, _ in page.log if m == "evaluate"]
    assert "top: 0" in scripts[0]
    assert "window.scrollY + 400" in scripts[1]
    assert "+ 200 - 50" in scripts[2]
    assert sum(1 for _, m, *_ in page.log if m == "settle") == 3


def test_offscreen_target_raises_and_unknown_gesture_is_rejected():
    with pytest.raises(RuntimeError, match="not on screen"):
        asyncio.run(_hand(FakePage(box=None)).glide("t"))
    with pytest.raises(ValueError, match="unknown gesture"):
        asyncio.run(_hand(FakePage()).run(("dance",)))
    assert "hover" in GESTURES
