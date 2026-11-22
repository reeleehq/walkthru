"""The guide ``RenderTarget``: a Demo Document as one phone-readable HTML page of annotated stills.

See :mod:`walkthru.adapters.guide.render_target`. The stills come from
:func:`walkthru.adapters.playwright.stills.play_stills`.
"""

from walkthru.adapters.guide.render_target import (
    GuideRenderTarget,
    guide_html,
    simple_markdown,
)

__all__ = ["GuideRenderTarget", "guide_html", "simple_markdown"]
