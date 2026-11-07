Diagnose Flow Music Kit errors and prescribe the fix.

Usage: `/fm-doctor` (triage) · `/fm-doctor <request_id>` · `/fm-doctor "<error text>"`

## Triage

```bash
curl -s http://127.0.0.1:8300/api/status | python3 -m json.tool
curl -s "http://127.0.0.1:8300/api/requests?status=FAILED&limit=20" | python3 -m json.tool
curl -s "http://127.0.0.1:8300/api/requests?status=POLLING" | python3 -m json.tool
```

Classify by `error_message` text, not HTTP status.

## Error taxonomy

| Error contains | Cause | Fix |
|---|---|---|
| `connection refused` on :8300 | agent not running | `./run.sh` (or `venv/bin/python -m agent.main`) |
| `EXTENSION_NOT_CONNECTED` / `extension_connected: false` | extension not loaded / Chrome closed / wrong port | load `extension/` unpacked in `chrome://extensions`, click Reconnect in the popup. Port must be 9300 |
| `NO_FLOW_MUSIC_TAB`, `TAB_DISCARDED` | no usable flowmusic.app tab | open https://flowmusic.app, sign in, keep the tab open (pin it so Chrome doesn't discard it) |
| `RECIPE: recipe 'x' not found` / HTTP 409 `not recorded yet` | operation never captured | `/fm-capture x` |
| `RECIPE: missing required variable` | song row lacks a field the recipe needs | set it on the song (`PATCH /api/songs/<id>`) or give a default in the recipe's `variables` |
| `rendered payload is not valid JSON` | a placeholder sits outside quotes without `|jsonval`, or vice versa | fix the placeholder filter in `recipes/<name>.json` |
| `NO_OUTPUT: response had no audio URL and no job_id` | extractors don't match the live response | `POST /api/recipes/<name>/run` with real variables, read `text_preview`, fix `extract` (`/fm-capture` step 3–5) |
| `POLL_TIMEOUT` | poll never saw audio URLs | check `poll_song` extractors against a capture of a finished poll; raise `POLL_TIMEOUT_S` for long songs |
| `CAPTCHA_FAILED: NO_GRECAPTCHA` | page hasn't loaded reCAPTCHA (yet) | reload the Flow Music tab and retry |
| `CAPTCHA_FAILED: NO_SITEKEY` | page doesn't expose its site key | add `"captcha_sitekey": "<key>"` to the recipe (find it in page source: `render=` / `sitekey`) |
| `UNUSUAL_ACTIVITY`, `COOLDOWN` | Google flagged the traffic | stop for a while. The agent already cools down (`UNUSUAL_ACTIVITY_COOLDOWN_S`). Raise `GENERATION_MIN_INTERVAL_S`. Check `captcha_action` is the page's real action. Don't loop retries |
| `PUBLIC_ERROR_*` (e.g. UNSAFE) | content rejected | rephrase prompt/lyrics (no real artist names, no copyrighted lyrics) |
| `QUOTA`, `RESOURCE_EXHAUSTED` | daily credits used up | wait for reset or upgrade plan |
| `HTTP_401` / `HTTP_403` / `NO_AT_TOKEN` | tab signed out or session expired | sign in again in the tab, reload |
| `HTTP_400` | payload shape changed (Flow Music UI update) | re-capture that recipe |
| `DOWNLOAD_FAILED` | signed URL expired or needs session | `POST /api/requests/<id>/retry`; if it repeats, check `page_download` in extension errors |
| `ffmpeg/ffprobe not found` | ffmpeg missing | `brew install ffmpeg` |

Retry a failed job after fixing: `POST /api/requests/<id>/retry`.

Transient errors (timeouts, extension disconnects, captcha hiccups) are already
retried `MAX_RETRIES` times by the worker — don't add your own retry loops.
