"""Playwright adapters — ``ElementLocator``, ``Recorder`` and ``ReadinessWaiter`` (PLAN §3.4).

The three ports that resolve to Playwright on the Python side. All operate on an injected,
duck-typed Playwright object, so the core stays vendor-free and they are unit-testable without a
browser; install the optional extra (``pip install walkthru[playwright]``) only to drive a real
browser. :class:`DomOverlayCueRenderer` is the Python ``CueRenderer`` (cues as DOM overlays), and
:func:`play_stills` shoots one annotated still per step (for
:class:`~walkthru.adapters.guide.GuideRenderTarget`); the driver.js ``CueRenderer`` is its TS
counterpart.

Two recorders: :class:`PlaywrightRecorder` (Playwright's own WebM screencast, any browser) and
:class:`CdpScreencastRecorder` (Chrome's DevTools screencast: crisp device-resolution frames with
timestamps, encoded as a constant-rate mp4 — what a narrated tour is cut from).
"""

from walkthru.adapters.playwright.cursor import (
    DEFAULT_CURSOR_SIZE,
    cursor_script,
    install_synthetic_cursor,
)
from walkthru.adapters.playwright.cues import (
    CUE_LAYER_ID,
    DEFAULT_CUE_COLOR,
    DomOverlayCueRenderer,
    cue_overlay_payload,
)
from walkthru.adapters.playwright.gestures import GESTURES, Hand
from walkthru.adapters.playwright.stills import (
    StillCamera,
    block_writes,
    default_shoot,
    play_stills,
    still_clip,
    with_posters,
)
from walkthru.adapters.playwright.player import (
    PAGE_COMMANDS,
    PlaywrightCommandPlayer,
    UnknownCommandError,
)
from walkthru.adapters.playwright.locator import (
    ElementNotFoundError,
    PlaywrightElementLocator,
    build_locator,
)
from walkthru.adapters.playwright.readiness import (
    DEFAULT_READINESS_TIMEOUT_MS,
    PlaywrightReadinessWaiter,
    ReadinessTimeoutError,
)
from walkthru.adapters.playwright.screencast import (
    CdpScreencastRecorder,
    screencast_launch_args,
)
from walkthru.adapters.playwright.recorder import (
    DEFAULT_VIDEO_MIME,
    PlaywrightRecorder,
    RecorderError,
    RecorderStateError,
    new_recording_page,
)

__all__ = [
    "CUE_LAYER_ID",
    "DEFAULT_CUE_COLOR",
    "DomOverlayCueRenderer",
    "cue_overlay_payload",
    "StillCamera",
    "block_writes",
    "default_shoot",
    "play_stills",
    "still_clip",
    "with_posters",
    "GESTURES",
    "Hand",
    "DEFAULT_CURSOR_SIZE",
    "cursor_script",
    "install_synthetic_cursor",
    "PAGE_COMMANDS",
    "PlaywrightCommandPlayer",
    "UnknownCommandError",
    "PlaywrightElementLocator",
    "ElementNotFoundError",
    "build_locator",
    "PlaywrightReadinessWaiter",
    "ReadinessTimeoutError",
    "DEFAULT_READINESS_TIMEOUT_MS",
    "PlaywrightRecorder",
    "RecorderError",
    "RecorderStateError",
    "new_recording_page",
    "DEFAULT_VIDEO_MIME",
    "CdpScreencastRecorder",
    "screencast_launch_args",
]
