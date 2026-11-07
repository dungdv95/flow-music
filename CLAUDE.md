# Flow Music Kit

Base URL: `http://127.0.0.1:8300` (extension WebSocket: `9300`). Sister project
of Flow Kit (`../flowkit`, `:8100`), which makes the visuals for music videos.

## Pre-flight

```bash
curl -s http://127.0.0.1:8300/api/status
# Need: extension_connected: true, a flow_music_tab, recipes.generate_song + poll_song = "ok"
```

Plus **one signed-in `https://flowmusic.google/` tab left open** — only the page
can sign Flow Music requests, so nothing runs headless.

## How to work

- Use the `/fm-*` skills; workflows and rules live in `skills/`.
- Never hand-write or guess Flow Music payloads. Every operation is a **recipe**
  recorded from the real UI (`/fm-capture`). If a recipe is missing, record it.
- Never loop API calls in scripts — queue jobs (`POST /api/songs/<id>/generate`)
  and let the worker pace them.
- On any error (request FAILED, stuck POLLING, `extension_connected: false`,
  4xx/5xx from :8300, `UNUSUAL_ACTIVITY`, `NO_OUTPUT`, `CAPTCHA_FAILED`) → `/fm-doctor`.
- Write lyrics with original words only; no real artist names in prompts.
- Captures hold the user's response bodies — delete them when a recipe is done.

## Skills

| Skill | When |
|---|---|
| `/fm-capture <recipe>` | Record a UI action as a recipe (first-time setup, or after a Flow Music UI change) |
| `/fm-create-project` | New project (album/channel) + draft songs |
| `/fm-write-lyrics` | Write `[Verse]/[Chorus]` lyrics for drafts |
| `/fm-gen-song` | Generate songs in Flow Music, download takes |
| `/fm-remix` | Extend, cover, or import songs |
| `/fm-music-video` | Visualizer / Flow Kit scenes / native music video |
| `/fm-pipeline` | Whole flow, resumable |
| `/fm-status` | Project dashboard |
| `/fm-doctor` | Diagnose errors |

## Layout

- `extension/` — Chrome MV3 bridge: runs commands in the flowmusic.google tab,
  mints reCAPTCHA through the page, capture mode (`page.js` hooks fetch/XHR only while on)
- `agent/services/recipes.py` — templating, batchexecute envelope/decoding, extractors, capture→recipe
- `agent/services/bridge.py` — WS command channel + generation rate guard
- `agent/worker/processor.py` — PENDING → PROCESSING → POLLING → COMPLETED, downloads takes
- `agent/services/post_process.py` — ffmpeg (visualizer, concat, mux) + lyric-aware scene planner
- `recipes/*.json` — recorded recipes (gitignored); `recipes/examples/` shows the format
- Tests: `venv/bin/python -m pytest`
