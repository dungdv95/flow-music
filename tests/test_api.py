"""End-to-end through the HTTP API with a fake extension.

The fake extension answers bridge commands the way the real one would after
Flow Music replied, so the worker's submit → poll → download path runs for real.
"""
import asyncio
import json
import subprocess
import shutil

import pytest
from fastapi.testclient import TestClient

from agent import config
from agent.main import app
from agent.services import recipes as rc
from agent.services.bridge import get_bridge


class FakeWS:
    """Stands in for the extension socket: replies to commands from a script."""

    def __init__(self, handler):
        self.handler = handler
        self.sent = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if "method" in msg:
            reply = self.handler(msg)
            asyncio.get_running_loop().call_soon(get_bridge().resolve, {"id": msg["id"], **reply})


def batch_text(rpcid, payload):
    chunk = json.dumps([["wrb.fr", rpcid, json.dumps(payload), None, None, None, "generic"]])
    return f")]}}'\n\n{len(chunk)}\n{chunk}\n"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(config, "WORKER_TICK_S", 0.5)
    monkeypatch.setattr(config, "POLL_INTERVAL_S", 1)
    monkeypatch.setattr(config, "GENERATION_MIN_INTERVAL_S", 0)
    with TestClient(app) as c:
        yield c


def wait_for(client, path, pred, timeout=20):
    import time
    end = time.time() + timeout
    while time.time() < end:
        data = client.get(path).json()
        if pred(data):
            return data
        time.sleep(0.3)
    raise AssertionError(f"timeout waiting on {path}: {data}")


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_full_song_flow(client, tmp_path, monkeypatch):
    audio = tmp_path / "song.mp3"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=330:duration=4", str(audio)],
                   check=True, capture_output=True)
    polls = {"n": 0}

    def handler(msg):
        p = msg["params"]
        if msg["method"] == "batch_rpc" and p["rpcid"] == "Gen01":
            assert p["captchaAction"] == "MUSIC_GEN"
            inner = json.loads(json.loads(p["freq"])[0][0][1])
            assert inner[0] == "sunrise synthwave. retro 80s"
            return {"status": 200, "data": batch_text("Gen01", [["job-42"]])}
        if msg["method"] == "batch_rpc" and p["rpcid"] == "Poll1":
            polls["n"] += 1
            if polls["n"] < 2:
                return {"status": 200, "data": batch_text("Poll1", [["job-42", "RUNNING"]])}
            return {"status": 200, "data": batch_text("Poll1", [["job-42", "DONE",
                    "https://media.example.invalid/a/song.mp3?sig=x"]])}
        if msg["method"] == "page_download":
            import base64
            return {"status": 200, "data": {"status": 200, "contentType": "audio/mpeg",
                                            "base64": base64.b64encode(audio.read_bytes()).decode()}}
        return {"error": f"unexpected {msg['method']}"}

    # The capture the extension would have posted while the user clicked Create.
    bridge = get_bridge()
    secret = bridge.callback_secret
    freq = json.dumps([["Gen01", json.dumps(["my sample prompt", None, "03A" + "t" * 250]), None, "generic"]])
    from urllib.parse import urlencode
    r = client.post("/api/ext/capture", headers={"x-callback-secret": secret}, json={
        "url": "https://flowmusic.google/_/MusicUi/data/batchexecute?rpcids=Gen01",
        "method": "POST", "status": 200, "pageUrl": "https://flowmusic.google/",
        "requestContentType": "application/x-www-form-urlencoded",
        "requestBody": urlencode({"f.req": f"[{freq}]", "at": "SECRET_AT"}),
        "responseText": batch_text("Gen01", [["job-1"]]),
    })
    assert r.status_code == 200
    assert client.post("/api/ext/capture", headers={"x-callback-secret": "nope"}, json={}).status_code == 403
    cap = client.get(f"/api/captures/{r.json()['id']}").json()
    assert "SECRET_AT" not in cap["request_body"]

    r = client.post("/api/recipes/from-capture", json={
        "capture_id": cap["id"], "name": "generate_song", "captcha_action": "MUSIC_GEN",
        "sample_values": {"prompt": "my sample prompt"},
        "extract": {"job_id": {"path": [0, 0]}}, "poll": "poll_song",
    })
    assert r.status_code == 200, r.text
    assert r.json()["extract_preview_on_capture"]["job_id"] == "job-1"
    rc.save_recipe(rc.Recipe.from_dict({
        "name": "poll_song", "transport": "batchexecute",
        "rpc": {"path": "/_/MusicUi/data/batchexecute", "rpcid": "Poll1", "payload": '["{{job_id}}"]'},
        "extract": {"failed": {"regex": r'"(FAILED)"'}},
    }))
    status = client.get("/api/status").json()
    assert status["recipes"]["generate_song"] == "ok"
    assert status["recipes"]["cover_song"] == "missing"

    project = client.post("/api/projects", json={"name": "Đêm Neon", "artist_style": "retro 80s"}).json()
    assert project["slug"] == "dem_neon"
    song = client.post("/api/songs", json={"project_id": project["id"], "title": "Sunrise",
                                           "prompt": "sunrise synthwave", "instrumental": True}).json()

    get_bridge().attach(FakeWS(handler))
    req = client.post(f"/api/songs/{song['id']}/generate").json()
    assert req["status"] == "PENDING"
    done = wait_for(client, f"/api/requests/{req['id']}", lambda d: d["status"] in ("COMPLETED", "FAILED"))
    assert done["status"] == "COMPLETED", done
    assert done["job_id"] == "job-42"

    song = client.get(f"/api/songs/{song['id']}").json()
    assert song["status"] == "COMPLETED"
    assert song["remote_id"] == "job-42"
    assert song["audio_duration_s"] == pytest.approx(4, abs=0.5)
    assert client.get(f"/api/songs/{song['id']}/audio").status_code == 200

    # Cover needs a recipe that isn't recorded → clear 409, nothing queued.
    r = client.post(f"/api/songs/{song['id']}/cover", json={"prompt": "acoustic"})
    assert r.status_code == 409

    mv = client.post("/api/music-videos", json={"song_id": song["id"], "mode": "VISUALIZER",
                                                "orientation": "VERTICAL", "clip_seconds": 2}).json()
    assert mv["plan"]["scene_count"] == 2
    out = client.post(f"/api/music-videos/{mv['id']}/visualizer").json()
    assert out["status"] == "COMPLETED"
    assert out["output_path"].endswith("sunrise_visualizer.mp4")
