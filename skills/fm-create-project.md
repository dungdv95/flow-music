Create a music project (album, channel series, or a batch of songs with one sound).

Usage: `/fm-create-project`

Ask the user (skip what they already gave):

1. **Name** and short **description** (theme / concept of the album or channel)
2. **Artist style** — the sound every song shares, as a style prompt: genre,
   era, instrumentation, mood, vocal type. E.g. `90s R&B, warm Rhodes, airy female vocal, 88 BPM feel`.
   No real artist names — describe the sound instead.
3. **Language** of lyrics (`en`, `vi`, …)
4. **Songs** — how many, and a one-line idea for each (optional now)

Then:

```bash
curl -s -X POST http://127.0.0.1:8300/api/projects -H 'content-type: application/json' -d '{
  "name": "<name>", "description": "<desc>", "artist_style": "<style>", "language": "<lang>"
}'
```

For each song idea create a draft (lyrics can come later via `/fm-write-lyrics`):

```bash
curl -s -X POST http://127.0.0.1:8300/api/songs -H 'content-type: application/json' -d '{
  "project_id": "<PID>", "title": "<title>", "prompt": "<song-specific prompt>",
  "instrumental": false, "bpm": 96, "duration_s": 150, "vocal": "<vocal description>",
  "display_order": 1
}'
```

Song `prompt` = what's specific to this song (topic, mood shift, arrangement).
The project's `artist_style` is appended automatically at generation time.
Flow Music tracks run up to ~3 minutes; longer songs are built with `/fm-extend`.

Finish with a table: # | title | prompt | bpm | duration, and suggest
`/fm-write-lyrics <PID>` next (or `/fm-gen-song` for instrumentals).
