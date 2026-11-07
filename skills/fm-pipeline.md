Run the whole flow for a project: lyrics → songs → music videos.

Usage: `/fm-pipeline [project_id] [--mode VISUALIZER|FLOWKIT|NATIVE] [--vertical]`

Detect state and continue from wherever the project is. Never redo finished work.

## 1. Preflight

```bash
curl -s http://127.0.0.1:8300/api/status | python3 -m json.tool
```

- Agent down → tell the user to run `./run.sh`
- `recipes.generate_song` or `recipes.poll_song` missing → `/fm-capture` first,
  then come back. This is a one-time setup per Flow Music UI version.
- No project id → `GET /api/projects`, use the most recent ACTIVE one or ask;
  none → `/fm-create-project`.

## 2. Lyrics

Songs with `instrumental: false` and empty `lyrics` → `/fm-write-lyrics` for
those songs. Show the lyrics and get a go-ahead before generating — that's the
step that spends credits.

## 3. Songs

Queue every DRAFT/FAILED song (`/fm-gen-song`), then monitor every ~30 s until
none are PENDING/PROCESSING/POLLING. Route failures through `/fm-doctor`;
retry once after fixing, then report what's still failing instead of looping.

Pause after this stage: list songs with their files and take count, and let
the user listen and pick takes (`selected_take`) before videos.

## 4. Music videos

For each COMPLETED song without a COMPLETED music video → `/fm-music-video`
with the chosen mode/orientation.

## 5. Report

Table: song | duration | audio file | video file | status. Mention anything
skipped or failed and why.
