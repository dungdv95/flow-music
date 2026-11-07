import base64
import json
import shutil
import subprocess
import uuid

import pytest

from agent.services import flowmusic_api as fm


def test_create_song_args_match_web_form():
    args, lyrics_map = fm.build_create_song_args(
        title="Last Tram Home", sound_prompt="synth-pop", lyrics="[Verse]\nla", bpm=112, length_s=120)
    text = "[Verse]\nla\n[End - 2:00]"
    assert args["sound_prompt"] == "synth-pop, 112 bpm"
    assert args["lyrics_id"] == str(uuid.uuid5(fm.LYRICS_NAMESPACE, text))
    assert lyrics_map == {args["lyrics_id"]: text}
    assert args["title"] == "Last Tram Home" and args["seed"] is None


def test_instrumental_and_empty():
    args, m = fm.build_create_song_args(title="", sound_prompt="", instrumental=True)
    assert m == {} and args["lyrics_id"] == "" and args["title"] == "Untitled"
    args, m = fm.build_create_song_args(title="x", sound_prompt="", bpm=90)
    assert args["sound_prompt"] == "90 bpm" and m == {}


def test_music_video_credits():
    assert fm.music_video_credits(120) == 1500
    assert fm.music_video_credits(8.01) == 101
    assert fm.music_video_credits(0) == 0


def sse(*events):
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)


def test_parse_stream_finds_tool_return():
    text = sse(("begin", {}), ("conversation_id", {"id": "conv-1"}),
               ("message", {"kind": "response", "parts": [{"part_kind": "tool-call", "tool_call_id": "t1"}]}),
               ("message", {"kind": "request", "parts": [{"part_kind": "tool-return", "tool_call_id": "t1",
                                                          "content": json.dumps({"clip_id": "c1", "clip_id_b": "c2"})}]}),
               ("final", {}))
    out = fm.tool_result_from_events(fm.parse_sse(text), "t1")
    assert out["conversation_id"] == "conv-1"
    assert out["content"] == {"clip_id": "c1", "clip_id_b": "c2"}
    err = fm.tool_result_from_events(fm.parse_sse(sse(("error", {"type": "moderation", "message": "no"}))), "t1")
    assert err["error"]["type"] == "moderation"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_native_song_and_video_flow(client, tmp_path, monkeypatch):
    from tests.test_api import FakeWS, wait_for
    from agent.services.bridge import get_bridge

    monkeypatch.setattr(fm.config, "GENERATION_MIN_INTERVAL_S", 0)
    audio = tmp_path / "a.m4a"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=500:duration=3", "-c:a", "aac",
                    str(audio)], check=True, capture_output=True)
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=2",
                    str(video)], check=True, capture_output=True)
    state = {"calls": [], "clip_polls": 0, "mv": False}

    def handler(msg):
        p = msg["params"]
        if msg["method"] == "page_download":
            f = video if p["url"].endswith(".mp4") else audio
            return {"data": {"status": 200, "contentType": "video/mp4" if f is video else "audio/mp4",
                             "base64": base64.b64encode(f.read_bytes()).decode()}}
        assert msg["method"] == "page_api"
        path, method = p["path"], p["method"]
        state["calls"].append((method, path))
        ok = lambda body, ct="application/json": {"data": {"status": 200, "contentType": ct, "text": body}}
        if path == "/models":
            return ok(json.dumps({"models": [{"public_name": "Lyria 4", "is_default": True}]}))
        if path == "/producer/tool-call":
            state["last_part"] = p["body"]["part"]
            state["ctx"] = p["body"]["client_context"]
            if p["body"]["part"]["tool_name"] == "video__create_music_video":
                state["mv"] = True
            return ok(json.dumps({"job_id": "job-" + p["body"]["part"]["tool_name"]}))
        if path.startswith("/messages/") and not path.startswith("/messages/chat-"):
            tcid = state["last_part"]["tool_call_id"]
            content = {"clip_id": "c1", "clip_id_b": "c2"} if "create_song" in path else {"music_video_id": "mv1"}
            return ok(sse(("conversation_id", {"id": "conv"}),
                          ("message", {"parts": [{"part_kind": "tool-return", "tool_call_id": tcid,
                                                  "content": content}]})), "text/event-stream")
        if path == "/conversation":
            state.setdefault("chats", []).append(p["body"]["parts"][0]["content"])
            return ok(json.dumps({"job_id": f"chat-{len(state['chats'])}"}))
        if path.startswith("/messages/chat-"):
            return ok(sse(("conversation_id", {"id": "conv-mv"}), ("complete", {})), "text/event-stream")
        if path == "/conversations/conv-mv":
            msgs = [{"parts": [{"part_kind": "tool-call", "tool_name": "video__propose_music_video",
                                "args": {"inputs": {**state["want"], "start_s": float(state["want"]["start_s"])}}}]}]
            if len(state.get("chats", [])) >= 2:
                state["mv"] = True
                msgs.append({"parts": [{"part_kind": "tool-return", "tool_name": "video__create_music_video",
                                        "content": {"job_id": "mvjob", "message": "submitted job"}}]})
            return ok(json.dumps({"id": "conv-mv", "messages": msgs}))
        if path == "/music-video/mvjob/status":
            return ok(json.dumps({"state": {"status": "completed", "final_video_url": "https://media.invalid/mv.mp4",
                                            "token_estimate": 6000}}))
        if path == "/clips":
            state["clip_polls"] += 1
            done = state["clip_polls"] > 1
            def clip(cid):
                return {"id": cid, "audio_url": f"https://media.invalid/{cid}.m4a" if done else None,
                        "wav_url": f"https://media.invalid/{cid}.wav",
                        "image_url": "https://media.invalid/cover.jpg",
                        "duration": {"status": "completed" if done else "pending", "value": "3.0"},
                        "video_url": "https://media.invalid/mv.mp4" if state["mv"] else None}
            return ok(json.dumps({"clips": {cid: clip(cid) for cid in p["body"]["clip_ids"]}}))
        return {"data": {"status": 404, "text": "{}"}}

    monkeypatch.setattr("agent.worker.processor.config.POLL_INTERVAL_S", 1)
    monkeypatch.setattr(fm.FlowMusicAPI, "wait_for_clips", _fast_wait(fm.FlowMusicAPI.wait_for_clips))
    get_bridge().attach(FakeWS(handler))

    project = client.post("/api/projects", json={"name": "Native"}).json()
    song = client.post("/api/songs", json={"project_id": project["id"], "title": "Tram", "prompt": "synth-pop",
                                           "lyrics": "[Verse]\nhi", "bpm": 112, "duration_s": 120}).json()
    req = client.post(f"/api/songs/{song['id']}/generate").json()
    assert req["recipe"] == "native:audio__create_song"
    done = wait_for(client, f"/api/requests/{req['id']}", lambda d: d["status"] in ("COMPLETED", "FAILED"))
    assert done["status"] == "COMPLETED", done
    assert state["last_part"]["args"]["sound_prompt"] == "synth-pop, 112 bpm"
    assert state["ctx"]["selected_model"] == "Lyria 4"
    assert list(state["ctx"]["lyrics_id_map"].values()) == ["[Verse]\nhi\n[End - 2:00]"]
    song = client.get(f"/api/songs/{song['id']}").json()
    assert song["status"] == "COMPLETED" and song["remote_id"] == "c1"
    assert [t["clip_id"] for t in song["takes"]] == ["c1", "c2"]
    assert song["cover_path"]

    mv = client.post("/api/music-videos", json={"song_id": song["id"], "mode": "NATIVE"}).json()
    r = client.post(f"/api/music-videos/{mv['id']}/native", json={"prompt": "anime"})
    assert r.status_code == 402 and r.json()["detail"]["estimated_credits"] == 38
    state["want"] = {"clip_id": "c2", "start_s": 0, "duration_s": 3.0, "aspect_ratio": "16:9",
                     "resolution": "720p", "render_lyrics": False, "user_message": "anime"}
    r = client.post(f"/api/music-videos/{mv['id']}/native", json={"prompt": "anime", "confirm": True, "take": 1})
    assert r.status_code == 200, r.text
    done = wait_for(client, f"/api/requests/{r.json()['id']}", lambda d: d["status"] in ("COMPLETED", "FAILED"))
    assert done["status"] == "COMPLETED", done
    assert "flowmusic.app/song/c2" in state["chats"][0] and "don't generate yet" in state["chats"][0]
    assert state["chats"][1] == fm.GENERATE_MESSAGE
    assert done["result"]["mv_job_id"] == "mvjob"
    mv = client.get(f"/api/music-videos/{mv['id']}").json()
    assert mv["status"] == "COMPLETED" and mv["output_path"].endswith(".mp4")


def _fast_wait(orig):
    async def wrapped(self, clip_ids, **kw):
        kw["every_s"] = 0.2
        return await orig(self, clip_ids, **kw)
    return wrapped


def test_proposal_mismatch_detection():
    want = {"clip_id": "c", "start_s": 28, "duration_s": 30, "aspect_ratio": "16:9",
            "resolution": "720p", "render_lyrics": True}
    assert fm.proposal_mismatches(want, {**want, "start_s": 28.0}) == []
    bad = fm.proposal_mismatches(want, {**want, "duration_s": 60, "render_lyrics": False})
    assert len(bad) == 2
    assert "30 seconds starting at 0:28" in fm.proposal_message({**want, "user_message": "anime"})
