Write song lyrics for draft songs.

Usage: `/fm-write-lyrics <project_id> [song_id]`

Two ways — prefer **A** (you write them; free, fully controllable). Use **B**
only when the user wants Flow Music's own lyric writer and the
`generate_lyrics` recipe is recorded.

## A — write them yourself

```bash
curl -s http://127.0.0.1:8300/api/projects/<PID>
curl -s "http://127.0.0.1:8300/api/songs?project_id=<PID>"
```

For each song without lyrics (or the one given), write lyrics that fit the
project's `artist_style`, `language`, and the song's `prompt`, `bpm`, `duration_s`.

Format rules (Flow Music / Lyria reads section tags):

- One tag per line: `[Intro]`, `[Verse 1]`, `[Pre-Chorus]`, `[Chorus]`,
  `[Verse 2]`, `[Bridge]`, `[Outro]`. Instrumental parts get a tag with no lines
  (`[Instrumental Break]`).
- Length budget: ~2–2.5 lines per 10 s of song at mid tempo. A 150 s song ≈
  `Intro / V1 / Chorus / V2 / Chorus / Bridge / Chorus / Outro`, 28–36 lines.
- Short singable lines (5–10 syllables), a chorus hook repeated verbatim, concrete
  images over abstractions.
- Original words only — never reuse lyrics from existing songs or name real artists.
- Vietnamese: keep tones natural for singing; avoid long compound sentences.

Save:

```bash
curl -s -X PATCH http://127.0.0.1:8300/api/songs/<SID> -H 'content-type: application/json' \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"lyrics": open(sys.argv[1]).read()}))' /path/to/lyrics.txt)"
```

(Write lyrics to a temp file first so quotes and newlines survive.)

Show the user each song's lyrics and ask for changes before `/fm-gen-song`.

## B — Flow Music writes them

```bash
curl -s -X POST http://127.0.0.1:8300/api/songs/<SID>/lyrics
```

Poll `GET /api/requests/<RID>` until COMPLETED; the lyrics land on the song.
