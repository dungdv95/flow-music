Extend, cover, or import songs.

Usage: `/fm-remix extend <song_id>` · `/fm-remix cover <song_id> "<new style>"` · `/fm-remix import <project_id> <file>`

Extend and cover create a **new song row** linked to the source
(`parent_song_id`); the source stays untouched. Both need the source's
`remote_id` (set automatically after `/fm-gen-song`, or after an upload).

## Extend (make a song longer / add a section)

Needs recipe `extend_song` (`/fm-capture extend_song`).

```bash
curl -s -X POST http://127.0.0.1:8300/api/songs/<SID>/extend -H 'content-type: application/json' -d '{
  "lyrics": "[Bridge]\n...\n[Final Chorus]\n...",
  "continue_at": 142.0
}'
```

`continue_at` = seconds into the source where the continuation starts (omit for
the end). Lyrics should only contain the *new* sections.

## Cover (same melody, new style)

Needs recipe `cover_song`.

```bash
curl -s -X POST http://127.0.0.1:8300/api/songs/<SID>/cover -H 'content-type: application/json' -d '{
  "prompt": "stripped acoustic, fingerpicked guitar, intimate male vocal"
}'
```

## Import a local file

```bash
curl -s -X POST http://127.0.0.1:8300/api/songs/import -H 'content-type: application/json' -d '{
  "project_id": "<PID>", "title": "<title>", "path": "/abs/path/song.mp3", "upload": false
}'
```

`upload: true` also uploads it to Flow Music (needs recipe `upload_audio`), which
gives it a `remote_id` so extend/cover work on it. Only import audio the user
owns or has rights to.

Monitor like `/fm-gen-song`.
