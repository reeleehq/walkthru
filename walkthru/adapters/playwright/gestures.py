"""A pointer that *does* things visibly: the gesture vocabulary of a filmed tour.

:class:`Hand` wraps a Playwright page (duck-typed, like the rest of this package, so it is
unit-testable without a browser) and offers glide / hover / click / type / smooth-scroll / pause.
Gestures are also data: ``("hover", target)``, ``("type", target, "text")``, ``("scroll_by", 400)``
run through :meth:`Hand.run`, so a tour can keep its choreography as a list. Targets are
walkthru :class:`~walkthru.core.schema.Locator`\\ s resolved by
:func:`~walkthru.adapters.playwright.locator.build_locator`, or whatever the injected
``locator_of`` resolves.

``on_arrive`` is the live-cue hook: an async callable told which target the pointer just reached
(ring the element as the pointer lands) -- the seam a ``CueRenderer`` plugs into.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Optional

from walkthru.adapters.playwright.locator import build_locator

DEFAULT_GLIDE_STEPS = 28
DEFAULT_AIM_MS = 180
DEFAULT_PRESS_MS = 90
DEFAULT_TYPE_DELAY_MS = 110
DEFAULT_SCROLL_PAD = 120
DEFAULT_SETTLE_TIMEOUT_MS = 10_000

#: Gesture names :meth:`Hand.run` understands.
GESTURES = (
    "hover",
    "glide",
    "click",
    "type",
    "clear",
    "key",
    "pause",
    "scroll_into",
    "scroll_by",
    "scroll_top",
)


class Hand:
    """What a person's hand does on a page, done visibly (the pointer glides; nothing teleports).

    Args:
        page: a Playwright ``Page`` (anything with ``mouse``, ``keyboard``, ``evaluate``,
            ``wait_for_timeout`` and ``wait_for_function``).
        locator_of: ``target -> Playwright locator``; default :func:`build_locator` on ``page``.
        glide_steps: intermediate mouse positions per glide (more is slower and smoother).
        aim_ms: how long the pointer rests on a target before a click.
        press_ms: how long a click holds the button down.
        type_delay_ms: delay between typed keys.
        scroll_pad: pixels kept above an element scrolled into view.
        on_arrive: ``async (target) -> None`` called each time the pointer reaches a target.
    """

    def __init__(
        self,
        page: Any,
        *,
        locator_of: Optional[Callable[[Any], Any]] = None,
        glide_steps: int = DEFAULT_GLIDE_STEPS,
        aim_ms: int = DEFAULT_AIM_MS,
        press_ms: int = DEFAULT_PRESS_MS,
        type_delay_ms: int = DEFAULT_TYPE_DELAY_MS,
        scroll_pad: int = DEFAULT_SCROLL_PAD,
        on_arrive: Optional[Callable[[Any], Awaitable[None]]] = None,
    ):
        self.page = page
        self._locator_of = locator_of or (lambda target: build_locator(page, target))
        self.glide_steps = glide_steps
        self.aim_ms = aim_ms
        self.press_ms = press_ms
        self.type_delay_ms = type_delay_ms
        self.scroll_pad = scroll_pad
        self.on_arrive = on_arrive

    async def _center(self, target: Any) -> tuple[float, float]:
        element = self._locator_of(target).first
        await element.scroll_into_view_if_needed()
        box = await element.bounding_box()
        if box is None:
            raise RuntimeError(f"not on screen: {target}")
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    async def glide(self, target: Any) -> tuple[float, float]:
        """Move the pointer to the center of ``target``, visibly; returns its position."""
        x, y = await self._center(target)
        await self.page.mouse.move(x, y, steps=self.glide_steps)
        if self.on_arrive is not None:
            await self.on_arrive(target)
        return x, y

    hover = glide

    async def click(self, target: Any) -> None:
        """Glide to ``target``, rest ``aim_ms``, press and release."""
        await self.glide(target)
        await self.page.wait_for_timeout(self.aim_ms)
        await self.page.mouse.down()
        await self.page.wait_for_timeout(self.press_ms)
        await self.page.mouse.up()

    async def type(self, target: Any, text: str) -> None:
        """Click ``target`` and type ``text`` key by key."""
        await self.click(target)
        await self.page.keyboard.type(text, delay=self.type_delay_ms)

    async def clear(self, target: Any) -> None:
        """Click ``target`` and empty it."""
        await self.click(target)
        await self.page.keyboard.press("ControlOrMeta+a")
        await self.page.keyboard.press("Backspace")

    async def key(self, name: str) -> None:
        await self.page.keyboard.press(name)

    async def pause(self, ms: int) -> None:
        await self.page.wait_for_timeout(int(ms))

    async def _settle(self) -> None:
        """Wait until the page stops moving after a smooth scroll."""
        await self.page.wait_for_function(
            """() => new Promise(r => {
                const el = document.scrollingElement; let last = -1, same = 0;
                const tick = () => {
                    const now = el.scrollTop;
                    same = now === last ? same + 1 : 0; last = now;
                    if (same >= 4) r(true); else requestAnimationFrame(tick);
                }; tick();
            })""",
            timeout=DEFAULT_SETTLE_TIMEOUT_MS,
        )

    async def _scroll_to_y(self, y_js: str) -> None:
        await self.page.evaluate(f"window.scrollTo({{top: {y_js}, behavior: 'smooth'}})")
        await self._settle()

    async def scroll_top(self) -> None:
        await self._scroll_to_y("0")

    async def scroll_by(self, dy: int) -> None:
        await self._scroll_to_y(f"window.scrollY + {int(dy)}")

    async def scroll_into(self, target: Any) -> None:
        """Smooth-scroll so ``target`` sits ``scroll_pad`` pixels below the top of the page."""
        element = self._locator_of(target).first
        box = await element.bounding_box()
        if box is None:
            raise RuntimeError(f"not on screen: {target}")
        pad = json.dumps(self.scroll_pad)
        await self._scroll_to_y(f"window.scrollY + {box['y']} - {pad}")

    async def run(self, gesture: tuple) -> None:
        """Run one gesture given as data: ``(name, *args)``, name one of :data:`GESTURES`."""
        name, *args = gesture
        if name not in GESTURES:
            raise ValueError(f"unknown gesture {name!r}; known: {', '.join(GESTURES)}")
        await getattr(self, name)(*args)

    async def run_all(self, gestures: Any) -> None:
        """Run gestures in order."""
        for gesture in gestures:
            await self.run(gesture)
