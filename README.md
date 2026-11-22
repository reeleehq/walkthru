# walkthru

Turn a sequence of application **commands** into an **editable, re-renderable demo/tour
artifact** — and play that sequence while observers record video, draw visual cues, and
narrate.

`walkthru` owns the **representation** (the *Demo Document*) and the **playback/capture
engine**. It does **not** render the final video — it hands a validated artifact off to a
renderer (the `reelee` ecosystem, Remotion, moviepy/ffmpeg, …). *Owning representation, not
pixels, is the load-bearing boundary of the whole design.*

It also shows **cited sources** on screen: capture a web page or PDF at a quoted passage, with the passage's boxes verified, and render a clip where a highlighter sweeps across it in sync with narration ([below](#show-a-source-on-screen-its-quoted-passage-highlighted)).

## Two modes, one data model, one engine

- **Generative** — an author supplies a Demo Document (commands + annotations); `walkthru`
  plays it while recording, with optional cues (highlight, spotlight, hotspot, callout,
  synthetic cursor), pauses, and narration.
- **Capture** — a human operates the app manually; `walkthru` records the video **and** the
  underlying command stream **and** annotations, producing the *same* Demo Document.

The only difference between the modes is *who fills in the document*.

## The core

```
play(demoDoc, executor, observers) -> Outcome
```

A pure higher-order function that walks the document and emits lifecycle events
(`onStepEnter` → `beforeCommand` → `afterCommand` → `onStepExit`, `onCueBegin/End`,
`onNarration`, …). It never records, renders, or speaks — every effect is an injected
observer or port.

## Quickstart

```bash
pip install walkthru
```

```python
import asyncio
from walkthru import DemoDocument, Section, CommandStep, Command, Timing, play

doc = DemoDocument(
    id="demo",
    sections=[
        Section(
            id="s1",
            steps=[
                CommandStep(
                    id="step-1",
                    command=Command(id="app.open"),
                    timing=Timing(duration_ms=500),
                ),
                CommandStep(
                    id="step-2",
                    command=Command(id="app.save"),
                    timing=Timing(duration_ms=800),
                ),
            ],
        )
    ],
)


async def executor(command):  # your app's command bus (acture, Playwright, …)
    print("run", command.id)
    return {"ok": True}


asyncio.run(play(doc, executor))  # → runs step-1, step-2; returns an Outcome
```

Three runnable scripts in [`examples/`](./examples) go further — a **generative** demo (commands +
cues + narration, with JSON + WebVTT hand-off), a **capture** demo (record a human's actions into the
same document and replay it), and a **narration** demo (segment → time → caption). All are pure-core,
no optional deps:

```bash
python examples/generative_demo.py
python examples/capture_demo.py
python examples/narration_demo.py
```

## Narration & captions

Narration is a track of editable **text** anchored to steps (the Descript model: text is the source
of truth; audio and timing are regenerable). Turning authored text into timed, voiced narration with
captions is a three-step, fully composable pipeline:

```python
from walkthru import realize_narration, pace_steps_to_narration, resolve_timeline
from walkthru.adapters.export import narration_to_webvtt, narration_to_srt
from walkthru.adapters.gif import GifRenderTarget  # screencast + camera -> GIF
from walkthru.adapters.playwright import (
    PlaywrightCommandPlayer,  # run a document's commands against a real page
    install_synthetic_cursor,  # a visible pointer: screencasts do not record one
)
from walkthru.adapters.synth import MixingSynthesizer, mixing_duration_ms

# 1. Synthesize each segment and time it from its own audio (anchor.duration_ms ← measured clip).
synth = MixingSynthesizer(
    voice_query="narrative_story", out_dir="tts"
)  # [synth] extra → ElevenLabs
realized = await realize_narration(doc, synth=synth, measure_ms=mixing_duration_ms)

# 2. (optional) Hold each beat at least as long as its line is spoken — narration-led pacing.
paced = pace_steps_to_narration(realized.document, policy="max")

# 3. Captions fall straight out of the resolved timeline — WebVTT (web-native) and SRT (universal).
open("demo.vtt", "w").write(narration_to_webvtt(paced))
open("demo.srt", "w").write(narration_to_srt(paced))
```

Because each segment is synthesized and measured independently, the beat boundaries fall out of
`resolve_timeline` with **no alignment math**. Synthesis is the `Synthesizer` port and the audio
assembler is an injected seam, so the whole flow is testable with fakes and pulls in no media stack;
`MixingSynthesizer` (the `[synth]` extra) is the hosted-voice tier. See `examples/narration_demo.py`
for an offline run (no API key) and `DECISIONS.md` §D8/§D12.

## A step-by-step guide from the same document

The document that films a tour also makes a visual guide: one annotated still per step (highlight rings, numbered callouts, spotlights drawn on the page as DOM overlays), laid out as one self-contained, phone-readable HTML page with the callout texts listed under each picture and the narration as its caption.

```python
from walkthru.adapters.playwright import play_stills
from walkthru.adapters.guide import GuideRenderTarget

shots = await play_stills(doc, page, "stills/", read_only=True)  # {step_id: jpg}, cues drawn on each
guide = GuideRenderTarget("guide.html", stills=shots)
await guide.export(doc)              # one HTML file, images inlined
guide.export_pdf("guide.pdf")        # optional: printed by pdfdol's headless Chromium
```

`read_only=True` aborts the page's non-GET requests while shooting, so a guide can be shot against a live account. A text-card beat becomes prose, a section a chapter.

## Film a tour

`film_document` plays a document in real time while recording the screen: each step runs its gestures (a pointer that glides, hovers, clicks and scrolls visibly) or its command, its cues are drawn once its effect is on screen and stay up for its hold, and the pointer rings each target it reaches. It returns the recording and each step's in-point; `mux_narration` lays the assembled narration under it.

```python
from walkthru.adapters.playwright import film_document, mux_narration

filmed = await film_document(doc, page, "tour.mp4",
                             gestures={"open-menu": [("glide", menu), ("command",)]})
mux_narration("tour.mp4", doc, "tour-narrated.mp4", at_s=filmed.in_points["open-menu"])
```

## Show a source on screen, its quoted passage highlighted

For explainer, educational or historical videos that cite sources: capture the web page or PDF at the quoted passage, then render a short clip where the camera eases toward the passage and a highlighter sweeps across it while the narration speaks those words.

```bash
pip install "walkthru[evidence]"      # Playwright + PyMuPDF + Pillow (and ffmpeg on PATH)
playwright install chromium
pip install "walkthru[ocr]"           # optional: scanned PDFs (ocracy + Tesseract)
```

```python
from walkthru import capture_passage, render_highlight_clip

cap = capture_passage(
    "https://example.org/article",
    quote="the exact words being cited",
    out="shots/a.png",
)
render_highlight_clip(cap, "clips/a.mp4", duration=8.0, sweep=(1.0, 3.6))
```

`sweep=(start, end)` is when, in seconds from the clip's start, the marker crosses the passage: set it to the times the narration speaks the quote (from your TTS or forced-alignment word timings).

**Highlight only what was verified.** `capture.found` is true only when the passage's boxes passed a check, and `capture.rects` is empty otherwise; a clip of an unverified capture is a gentle push-in with no highlight. When that happens, show the page without a highlight and say so (in the script, the description or the log), never draw boxes by hand. `capture.reason` says why (`not-in-text`, `occluded`, `off-screen`, `invisible`, `mismatch`, `ocr-mismatch`, …) and `capture.verified_by` which check passed:

- **Web pages** (`dom`): the quote is located among the *visible* text nodes (typography folded: curly quotes, dashes, ligatures, soft hyphens, whitespace; paragraph boundaries count as spaces). The DOM range's text must spell the quote, no text in it may be drawn invisibly (zero size, transparent), every box must be on screen, and a caret hit test at six points in each box must land in that very text node. That catches cookie walls, sticky bars, a curtain nested inside the quote's own element, a bar over half a line, and clipped text. Consent dialogs are dismissed and large fixed overlays hidden first; each occurrence is tried until one verifies.
- **Born-digital PDFs** (`pdf-text`): the boxes are those of the very whole words that spell the quote ("the rapist" never matches "therapist"), matched across line-break hyphenation, merged one bar per line, on a 16:9 window that holds the whole passage (rotated pages included).
- **Scanned PDFs** (`ocr`): a page covered by images, or holding invisible text (an OCR layer), has a text layer whose boxes can sit well off the glyphs, so they are never used; the window is re-read by OCR near where the text layer put the passage, and accepted only if the OCR'd words match the quote word for word (a dropped "not" or a changed digit fails).

Licences: walkthru is MIT; the `evidence` extra pulls in PyMuPDF, which is AGPL-3.0 (or commercial), so check that fits before you ship software that bundles it. Playwright is Apache-2.0, Pillow is MIT-CMU, and Tesseract is Apache-2.0.

To capture many sources, open one browser and pass it along: `with open_capture_page() as page: capture_passage(url, quote, out=..., page=page)` (from `walkthru.evidence`). Each capture writes a JSON sidecar beside its PNG, so `render_highlight_clip("shots/a.json", ...)` works in another process. Light pages get a multiply-blend yellow marker; dark pages an amber tint and underline, since multiply is invisible on dark themes.

## Ecosystem-biased, ecosystem-independent

The core and ports depend on **nothing** from our ecosystem. Integration with
[`acture`](https://github.com/thorwhalen/acture) (the command layer),
[`reelee`](https://github.com/thorwhalen/reelee) (the renderer), and `zodal` ships as
**optional adapters**. You can `pip install walkthru` / `npm i acture-walkthru` and use the
core with your own adapters.

## Packages

| Package | Registry | Role |
|---|---|---|
| `walkthru` | PyPI | Python core + schema SSOT + render hand-off |
| `acture-walkthru` | npm | TS core + the live capture/play engine over `acture` |

## Status

The **Python side is implemented**: the Demo Document schema (the SSOT), the pure play/capture
engine, dependency-free JSON + WebVTT + SRT export, segmented narration (synthesize → time → pace →
assemble), and the first adapters (Playwright locator/recorder, the `mixing`/ElevenLabs voice
synthesizer, and the [`reelee`](https://github.com/thorwhalen/reelee) Ken Burns render target). The TypeScript
package currently ships the **schema seam** — a Zod validator codegened from the Pydantic SSOT — and
its live capture/play engine over `acture` lands next.

See [`PLAN.md`](./PLAN.md) for the implementation plan, [`DECISIONS.md`](./DECISIONS.md) for design
decisions and deviations from the brief, and the repo's **enhancement issues** for the running
development journal. Source documents live in [`misc/docs/`](./misc/docs/).

## License

MIT — see [`LICENSE`](./LICENSE).
