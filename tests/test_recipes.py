import json
from urllib.parse import urlencode

import pytest

from agent.services import recipes as rc

TOKEN = "03A" + "x" * 300


def batch_capture(inner: list, rpcid="Ab12c", path="/_/MusicUi/data/batchexecute"):
    freq = json.dumps([[[rpcid, json.dumps(inner), None, "generic"]]])
    return {
        "id": "cap1",
        "url": f"https://flowmusic.google{path}?rpcids={rpcid}&rt=c",
        "method": "POST",
        "page_url": "https://flowmusic.google/create",
        "request_content_type": "application/x-www-form-urlencoded;charset=UTF-8",
        "request_body": urlencode({"f.req": freq, "at": "REDACTED"}),
        "response_text": "",
    }


def batch_response(rpcid: str, payload) -> str:
    chunk = json.dumps([["wrb.fr", rpcid, json.dumps(payload), None, None, None, "generic"]])
    return f")]}}'\n\n{len(chunk)}\n{chunk}\n25\n[[\"e\",4,null,null,123]]\n"


# ─── rendering ───────────────────────────────────────────────

def test_render_escapes_quotes_and_newlines():
    out = rc.render_template('{"p":"{{prompt}}"}', {"prompt": 'say "hi"\nnow'})
    assert json.loads(out)["p"] == 'say "hi"\nnow'


def test_render_filters_and_defaults():
    spec = {"bpm": {"default": None}}
    assert rc.render_template("[{{bpm|jsonval}}]", {}, spec) == "[null]"
    assert rc.render_template("[{{bpm|jsonval}}]", {"bpm": 92}, spec) == "[92]"
    assert rc.render_template("{{q|url}}", {"q": "a b&c"}) == "a%20b%26c"
    assert rc.render_template("{{flag|jsonval}}", {"flag": True}) == "true"


def test_render_missing_required_raises():
    with pytest.raises(rc.RecipeError, match="required"):
        rc.render_template("{{prompt}}", {}, {"prompt": {"required": True}})
    with pytest.raises(rc.RecipeError, match="missing variable"):
        rc.render_template("{{nope}}", {})


def test_builtins_fresh_each_render():
    a = rc.render_template("{{$uuid}}", {})
    b = rc.render_template("{{$uuid}}", {})
    assert a != b and len(a) == 36


def test_batch_command_envelope_roundtrip():
    r = rc.Recipe.from_dict({
        "name": "generate_song", "transport": "batchexecute", "captcha_action": "GEN",
        "rpc": {"path": "/_/X/data/batchexecute", "rpcid": "Ab12c",
                "payload": '[["{{prompt}}",{{bpm|jsonval}}],"__CAPTCHA__"]'},
        "variables": {"bpm": {"default": None}},
    })
    cmd = r.build_command({"prompt": 'lo-fi "rain"'})
    assert cmd["method"] == "batch_rpc"
    env = json.loads(cmd["params"]["freq"])
    inner = json.loads(env[0][0][1])
    assert env[0][0][0] == "Ab12c"
    assert inner == [['lo-fi "rain"', None], "__CAPTCHA__"]
    assert cmd["params"]["captchaAction"] == "GEN"


def test_validate_rejects_bad_recipes():
    with pytest.raises(rc.RecipeError):
        rc.Recipe.from_dict({"name": "Bad-Name", "transport": "fetch", "request": {"url": "/x"}})
    with pytest.raises(rc.RecipeError):
        rc.Recipe.from_dict({"name": "x", "transport": "batchexecute", "rpc": {"path": "/p"}})
    with pytest.raises(rc.RecipeError):
        rc.Recipe.from_dict({"name": "x", "transport": "fetch", "request": {"url": "/x"},
                             "extract": {"a": {"regex": "("}}})


# ─── responses ───────────────────────────────────────────────

def test_decode_batchexecute():
    text = batch_response("Ab12c", [["job-123", None], {"ok": True}])
    decoded = rc.decode_batchexecute(text)
    assert decoded[0]["rpcid"] == "Ab12c"
    assert decoded[0]["data"][0][0] == "job-123"


def test_extract_audio_urls_from_double_escaped_batch():
    url = "https://lh3.googleusercontent.com/music/abc.mp3?sig=1&exp=2"
    text = batch_response("Ab12c", [[url, "https://x.googleusercontent.com/c/cover.jpg"]])
    ex = rc.extract(None, text)
    assert url in ex["audio_urls"]
    assert ex["image_urls"] == ["https://x.googleusercontent.com/c/cover.jpg"]


def test_extract_recipe_rules_path_and_regex():
    r = rc.Recipe.from_dict({
        "name": "poll_song", "transport": "batchexecute",
        "rpc": {"path": "/p", "rpcid": "Pq", "payload": '["{{job_id}}"]'},
        "extract": {"job_id": {"path": [0, 0]}, "state": {"regex": r'"state":"(\w+)"'}},
    })
    text = batch_response("Pq", [["job-9"], {"state": "RUNNING"}])
    ex = rc.extract(r, text)
    assert ex["job_id"] == "job-9"
    assert ex["state"] == "RUNNING"
    assert ex["audio_urls"] == []


def test_extract_json_fetch_response_and_errors():
    body = json.dumps({"track": {"audioUrl": "https://cdn.example.com/t/1.wav"}})
    assert rc.extract(None, body)["audio_urls"] == ["https://cdn.example.com/t/1.wav"]
    assert rc.extract(None, '{"error":"PUBLIC_ERROR_UNSAFE_GENERATION"}')["errors"] == ["PUBLIC_ERROR_UNSAFE_GENERATION"]


def test_audio_url_does_not_stop_at_inner_extension():
    text = '"https://cdn.example.com/a.mp3.html" "https://cdn.example.com/b.m4a"'
    assert rc.extract(None, text)["audio_urls"] == ["https://cdn.example.com/b.m4a"]


# ─── capture → recipe ────────────────────────────────────────

def test_recipe_from_batch_capture():
    inner = [["dreamy lofi with \"rain\"", "[Verse]\nline one"], 84, TOKEN,
             "11111111-2222-3333-4444-555555555555"]
    cap = batch_capture(inner)
    recipe, notes = rc.recipe_from_capture(
        cap, "generate_song",
        {"prompt": 'dreamy lofi with "rain"', "lyrics": "[Verse]\nline one", "bpm": 84},
        captcha_action="MUSIC_GEN",
    )
    assert recipe.transport == "batchexecute"
    assert recipe.rpc["rpcid"] == "Ab12c"
    assert recipe.rpc["path"] == "/_/MusicUi/data/batchexecute"
    assert rc.CAPTCHA_SLOT in recipe.rpc["payload"]
    assert any("UUIDs" in n for n in notes)
    cmd = recipe.build_command({"prompt": "new\tsong", "lyrics": "[Chorus]\nla", "bpm": 120})
    rendered = json.loads(json.loads(cmd["params"]["freq"])[0][0][1])
    assert rendered[0] == ["new\tsong", "[Chorus]\nla"]
    assert rendered[1] == 120
    assert rendered[2] == "__CAPTCHA__"


def test_recipe_from_capture_requires_unique_literal():
    cap = batch_capture([["same", "same"]])
    with pytest.raises(rc.RecipeError, match="2 times"):
        rc.recipe_from_capture(cap, "x", {"prompt": "same"})
    with pytest.raises(rc.RecipeError, match="not found"):
        rc.recipe_from_capture(cap, "x", {"prompt": "absent"})


def test_recipe_from_json_fetch_capture():
    cap = {
        "id": "c2", "url": "https://flowmusic.google/api/v1/songs:generate", "method": "POST",
        "page_url": "https://flowmusic.google/", "request_content_type": "application/json",
        "request_body": json.dumps({"prompt": "epic choir", "duration": 95, "t": TOKEN}),
    }
    recipe, notes = rc.recipe_from_capture(cap, "generate_song", {"prompt": "epic choir", "duration_s": 95},
                                           captcha_action="GEN")
    assert recipe.request["url"] == "/api/v1/songs:generate"
    cmd = recipe.build_command({"prompt": "x\"y", "duration_s": 30})
    assert json.loads(cmd["params"]["body"]) == {"prompt": 'x"y', "duration": 30, "t": "__CAPTCHA__"}


def test_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(rc.config, "RECIPES_DIR", tmp_path)
    r = rc.Recipe.from_dict({"name": "poll_song", "transport": "fetch",
                             "request": {"url": "/api/jobs/{{job_id|url}}", "method": "GET"}})
    rc.save_recipe(r)
    loaded = rc.load_recipe("poll_song")
    assert loaded.variables["job_id"]["required"] is True
    assert loaded.build_command({"job_id": "a/b"})["params"]["url"] == "/api/jobs/a%2Fb"
    assert [x["name"] for x in rc.list_recipes()] == ["poll_song"]


def test_example_recipes_are_valid():
    from pathlib import Path
    folder = Path(__file__).resolve().parent.parent / "recipes" / "examples"
    files = sorted(folder.glob("*.json"))
    assert files
    for path in files:
        r = rc.Recipe.from_dict(json.loads(path.read_text()))
        values = {"prompt": "p", "job_id": "j"}
        r.build_command(values)
