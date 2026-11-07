Show project status.

Usage: `/fm-status` (all projects) · `/fm-status <project_id>`

```bash
curl -s http://127.0.0.1:8300/api/status | python3 -m json.tool
curl -s http://127.0.0.1:8300/api/projects
```

With a project id:

```bash
curl -s http://127.0.0.1:8300/api/projects/<PID> | python3 -m json.tool
curl -s "http://127.0.0.1:8300/api/requests?project_id=<PID>&limit=20"
```

Print:

1. Health line: extension ✓/✗, Flow Music tab ✓/✗, ffmpeg ✓/✗, queue counts
2. Recipes: which of `generate_song`, `poll_song`, `extend_song`, `cover_song`,
   `generate_lyrics`, `upload_audio`, `generate_music_video` are recorded
3. Songs table: # | title | status | lyrics? | takes | duration | file
4. Music videos table: song | mode | status | output
5. Next step:
   - recipes missing → `/fm-capture generate_song`
   - drafts without lyrics → `/fm-write-lyrics`
   - drafts ready → `/fm-gen-song`
   - songs done, no video → `/fm-music-video`
   - failures → `/fm-doctor`
