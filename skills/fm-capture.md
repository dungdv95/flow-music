Record a Flow Music UI action and turn it into a recipe the agent can replay.

Usage: `/fm-capture <recipe_name>` — e.g. `/fm-capture generate_song`

Known recipe names (`GET /api/recipes` → `known`): `generate_song`, `poll_song`,
`get_song`, `extend_song`, `cover_song`, `generate_lyrics`, `upload_audio`,
`generate_music_video`. Start with **generate_song** and **poll_song** — nothing
else works until those two exist.

## Why this exists

Flow Music has no public API. Its private calls are signed by the page, and
their payload shapes are unknown until watched. Never guess a payload — a wrong
slot is often *accepted* and silently ignored. Record one real action instead.

## Step 1 — preflight

```bash
curl -s http://127.0.0.1:8300/api/status | python3 -m json.tool
```

Need `extension_connected: true` and a `flow_music_tab` entry. If not → `/fm-doctor`.

## Step 2 — record

```bash
curl -s -X DELETE http://127.0.0.1:8300/api/captures          # start clean
curl -s -X POST http://127.0.0.1:8300/api/captures/mode -H 'content-type: application/json' -d '{"enabled": true}'
```

Then ask the user to do **exactly one** action in the Flow Music tab, using
**distinctive sample values** so they can be found in the payload:

| recipe | user does | sample values to type |
|---|---|---|
| generate_song | Create a song | prompt `zzsample neon rain synthwave`, lyrics `[Verse]\nzzline one`, BPM `117` (odd numbers are easier to find), duration if the UI has it |
| poll_song | nothing extra — the polling calls appear while the song from generate_song renders | — |
| generate_lyrics | Write lyrics from a prompt | prompt `zzsample lyrics about lighthouses` |
| extend_song / cover_song | Extend / Cover an existing track | prompt `zzsample acoustic cover` |
| upload_audio | Upload a small audio file | — |

Wait until the result is fully visible in the UI (song playable), then stop:

```bash
curl -s -X POST http://127.0.0.1:8300/api/captures/mode -H 'content-type: application/json' -d '{"enabled": false}'
```

## Step 3 — find the right capture

```bash
curl -s "http://127.0.0.1:8300/api/captures?q=zzsample" | python3 -m json.tool
curl -s "http://127.0.0.1:8300/api/captures" | python3 -m json.tool   # everything, newest first
```

For each candidate, `GET /api/captures/<id>` shows `parsed_rpc` (rpcid + inner
payload for batchexecute calls) and `extracted` (audio URLs, uuids, errors found
in the response). Identify:

- **submit call** — its request body contains the sample prompt. Its response
  usually holds a job/track id.
- **poll call** — repeated calls after the submit whose body contains that job id;
  the last one's response has the audio URL(s) (`extracted.audio_urls`).

Write down where the id sits. For batchexecute, `data` in `GET /api/captures/<id>`
→ `parsed_rpc` / the response is a nested array — express its position as a
`path` like `[0, 1, 0]`; otherwise use a `regex` with one capture group.

## Step 4 — create the recipe

```bash
curl -s -X POST http://127.0.0.1:8300/api/recipes/from-capture -H 'content-type: application/json' -d '{
  "capture_id": "<submit capture id>",
  "name": "generate_song",
  "sample_values": {"prompt": "zzsample neon rain synthwave", "lyrics": "[Verse]\nzzline one", "bpm": 117},
  "captcha_action": "<see below, or null>",
  "extract": {"job_id": {"path": [0, 0]}},
  "poll": "poll_song"
}' | python3 -m json.tool
```

- `sample_values` keys become variables. Standard names the agent fills from a
  song row: `prompt`, `lyrics`, `title`, `instrumental`, `bpm`, `duration_s`,
  `vocal`, `language`, `remote_id`, `job_id`, `continue_at`. Values must occur
  **exactly once** in the body.
- Read `notes` in the reply and fix what they flag:
  - **reCAPTCHA token found** → the body carried one and it was replaced with
    `__CAPTCHA__`. Set `captcha_action` to the action the page uses. Find it
    in the page JS (DevTools → Sources → search `grecaptcha.enterprise.execute`
    / `action:`) — ask the user to check if unsure. Without the right action the
    server rejects the request.
  - **UUIDs left as constants** → a per-request id (client request id, seed) must
    be fresh each time: edit the recipe and swap it for `{{$uuid}}` (or
    `{{$seed|jsonval}}` / `{{$now_ms|jsonval}}` for numbers). A workspace/project
    id stays constant.
- `extract_preview_on_capture` shows what the extractors pull from the captured
  response — verify `job_id` (or `audio_urls`) is right.

Poll recipe — same command on the poll capture with
`"sample_values": {"job_id": "<the job id seen in that body>"}` and extractors:
`audio_urls` is automatic; add `"failed": {"regex": "\"(FAILED|ERROR)\""}` or
whatever the response shows on failure. Optional `remote_id` / `lyrics` extractors
are stored on the song when present.

Lyrics recipe must extract `lyrics`; upload recipe must extract `remote_id`.

## Step 5 — verify

```bash
curl -s -X POST http://127.0.0.1:8300/api/recipes/generate_song/render -H 'content-type: application/json' \
  -d '{"variables": {"prompt": "test \"quotes\"", "lyrics": "", "bpm": 90}}'          # dry run, sends nothing
curl -s -X POST http://127.0.0.1:8300/api/recipes/poll_song/test-extract -H 'content-type: application/json' \
  -d '{"capture_id": "<poll capture with audio>"}'                                    # extractor check
```

Then one live song with `/fm-gen-song`. Edit any recipe with
`PUT /api/recipes/<name>` (full JSON) or directly in `recipes/<name>.json`.

## Step 6 — clean up

```bash
curl -s -X DELETE http://127.0.0.1:8300/api/captures
```

Captures hold response bodies from the user's account; don't leave them around.
The `at` CSRF token is redacted on save and headers/cookies are never recorded.
