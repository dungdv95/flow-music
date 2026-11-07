Generate songs in Google Flow Music.

Usage: `/fm-gen-song <project_id>` (all drafts) · `/fm-gen-song <song_id>`

## Preflight

```bash
curl -s http://127.0.0.1:8300/api/status | python3 -m json.tool
```

- `extension_connected: true`, a `flow_music_tab` present
- `recipes.generate_song` and `recipes.poll_song` = `ok` — otherwise run
  `/fm-capture generate_song` first. Don't try to hand-write the payload.

## Queue

For each song in `DRAFT` or `FAILED` that has a `prompt` or `lyrics`
(instrumentals need only a prompt):

```bash
curl -s -X POST http://127.0.0.1:8300/api/songs/<SID>/generate
```

Optional body `{"variables": {...}}` overrides recipe variables for this run
(e.g. a one-off BPM). The worker spaces generations by
`GENERATION_MIN_INTERVAL_S` and runs one at a time — queue them all and let it
work; never loop calls yourself.

## Monitor

```bash
curl -s http://127.0.0.1:8300/api/requests/summary
curl -s "http://127.0.0.1:8300/api/songs?project_id=<PID>" | python3 -c "
import json,sys
for s in json.load(sys.stdin):
    print(f\"{s['status']:10} {s['title'][:30]:30} takes={len(s['takes'])} {s.get('audio_duration_s') or ''} {s.get('error_message') or ''}\")"
```

Check every ~30 s. Requests go `PENDING → PROCESSING → POLLING → COMPLETED`.
On `FAILED` → `/fm-doctor <request_id>`.

## Results

Each finished song has `takes` (Flow Music may return several versions), each
downloaded to `output/<project>/songs/<song>_<n>.<ext>`. `audio_path` points to
the selected take. Let the user listen, then pick one:

```bash
curl -s -X PATCH http://127.0.0.1:8300/api/songs/<SID> -H 'content-type: application/json' -d '{"selected_take": 1}'
```

Not happy → edit `prompt`/`lyrics` and generate again (the song row is reused),
or `/fm-extend` / `/fm-cover`. Next step for videos: `/fm-music-video <SID>`.
