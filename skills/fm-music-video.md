Make a music video for a finished song.

Usage: `/fm-music-video <song_id> [VISUALIZER|FLOWKIT|NATIVE] [HORIZONTAL|VERTICAL]`

Default mode is **FLOWKIT** if Flow Kit is reachable, else ask. Default
orientation HORIZONTAL (VERTICAL for Shorts/TikTok/Reels).

| mode | what you get | needs |
|---|---|---|
| VISUALIZER | cover art (or dark bg) + live waveform. Minutes, free | ffmpeg |
| FLOWKIT | AI scenes that follow the lyrics, 8 s per clip, via Flow Kit (`../flowkit`, :8100) | Flow Kit running + its Flow tab |
| NATIVE | Flow Music's own music-video feature (paid tiers) | recipe `generate_music_video` |

## Step 1 — create + plan

```bash
curl -s http://127.0.0.1:8300/api/songs/<SID>        # must be COMPLETED with audio_path
curl -s -X POST http://127.0.0.1:8300/api/music-videos -H 'content-type: application/json' -d '{
  "song_id": "<SID>", "mode": "FLOWKIT", "orientation": "HORIZONTAL", "clip_seconds": 8
}' | python3 -m json.tool
```

The reply's `plan` splits the song into `scene_count` clips of `clip_seconds`,
each tagged with its lyric `section` and the `lyrics` lines that fall in it
(timing is estimated from line counts — good for mood, not beat-exact).

## VISUALIZER

```bash
curl -s -X POST http://127.0.0.1:8300/api/music-videos/<MID>/visualizer -H 'content-type: application/json' \
  -d '{"image_path": "/optional/cover.jpg"}'
```

Done — `output_path` in the reply.

## NATIVE

```bash
curl -s -X POST http://127.0.0.1:8300/api/music-videos/<MID>/native -H 'content-type: application/json' \
  -d '{"prompt": "<visual direction>"}'
```

Poll `GET /api/requests/<RID>`; the video downloads to `output_path`.

## FLOWKIT

1. **Check Flow Kit:** `curl -s http://127.0.0.1:8300/api/music-videos/flowkit/health`
   must show `reachable: true, extension_connected: true`. If not, tell the user
   to start Flow Kit (`cd ../flowkit && ./run.sh` or their usual command) with
   its flow.google.com tab open.

2. **Write the visual treatment.** From the plan, decide:
   - a visual concept for the whole song (story, performer, or abstract — match
     the song's mood and `song_prompt`);
   - recurring **entities** (singer/character, locations, props) so scenes stay
     consistent — Flow Kit makes one reference image per entity;
   - one scene per `plan.scenes[i]`: an image `prompt` and a `video_prompt`
     whose action fits that clip's lyrics and section energy (verses calmer,
     choruses wider shots / more motion, bridge = change of setting or light).
     **No dialogue or lip-sync in video prompts**, and add
     `Audio: none. Negative: dialogue, singing voice, subtitles, text` — the song
     is laid on afterwards, generated audio is discarded.
   Write each scene's prompts back into the plan (`scenes[i].prompt`,
   `scenes[i].video_prompt`) and save:
   `PUT /api/music-videos/<MID>/plan` with `{"plan": {...}}`.
   Show the user the treatment table before spending credits.

3. **Generate in Flow Kit.** Read and follow Flow Kit's own skills from
   `../flowkit/skills/` (they target `http://127.0.0.1:8100`):
   `fk-create-project.md` (project with the entities, `allow_music: false`,
   one video, the scenes in plan order, same orientation) →
   `fk-gen-refs.md` → `fk-gen-images.md` → `fk-gen-videos.md`.
   Store the Flow Kit ids on the project here:
   `PATCH /api/projects/<PID>` `{"flowkit_project_id": "<FK PID>"}`.

4. **Collect clips.** For each Flow Kit scene in `display_order`, download its
   video (`horizontal_video_url` / `vertical_video_url`, or the local file Flow
   Kit saved) to `output/<project>/videos/clips_<MID8>/scene_<NNN>.mp4`.

5. **Attach the song:**

```bash
curl -s -X POST http://127.0.0.1:8300/api/music-videos/<MID>/attach -H 'content-type: application/json' -d '{
  "clip_paths": ["/abs/.../scene_000.mp4", "/abs/.../scene_001.mp4"],
  "fit": "trim",
  "flowkit_video_id": "<FK VID>"
}'
```

Clips are concatenated (re-encoded to the target frame size, their own audio
dropped), the song is laid underneath, output trimmed to the song length.
`fit: "loop"` repeats the visuals if they're shorter than the song.

Already have a finished video (Flow Kit concat, stock footage, hand edit)? Use
`{"video_path": "/abs/video.mp4", "fit": "loop"}` instead.

## Finish

Report `output_path`, duration (`ffprobe`), and offer: a VERTICAL cut for
Shorts (new music video with `orientation: VERTICAL`), or re-generating weak
scenes in Flow Kit and re-running step 5.
