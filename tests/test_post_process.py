import shutil
import subprocess

import pytest

from agent.services import post_process as pp

LYRICS = """[Intro]

[Verse 1]
Neon rain on the window
City hums a lullaby
[Chorus]
Hold on, hold on
We are the light
Hold on
[Outro]
fade
"""


def test_parse_sections_keeps_empty_tagged_sections():
    secs = pp.parse_sections(LYRICS)
    assert [s["label"] for s in secs] == ["Intro", "Verse 1", "Chorus", "Outro"]
    assert secs[0]["lines"] == []
    assert len(secs[2]["lines"]) == 3


def test_parse_sections_untagged():
    secs = pp.parse_sections("just one line\nand another")
    assert len(secs) == 1 and secs[0]["lines"] == ["just one line", "and another"]


def test_plan_scenes_covers_song():
    plan = pp.plan_scenes(61.0, LYRICS, 8)
    assert plan["scene_count"] == 8
    assert plan["scenes"][-1]["end"] == 61.0
    assert plan["scenes"][-1]["duration"] == 5.0
    assert plan["sections"][-1]["end"] == 61.0
    all_lines = [l for s in plan["scenes"] for l in s["lyrics"]]
    assert all_lines == ["Neon rain on the window", "City hums a lullaby", "Hold on, hold on",
                         "We are the light", "Hold on", "fade"]


def test_plan_instrumental():
    plan = pp.plan_scenes(20, "", 8, instrumental=True)
    assert plan["scene_count"] == 3
    assert {s["section"] for s in plan["scenes"]} == {"Instrumental"}


def test_commands_shape(tmp_path):
    cmd = pp.mux_cmd(tmp_path / "v.mp4", tmp_path / "a.mp3", tmp_path / "o.mp4", "loop", "VERTICAL")
    assert cmd[:4] == ["ffmpeg", "-y", "-stream_loop", "-1"]
    assert "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1" in cmd
    assert "-shortest" in cmd
    assert "-stream_loop" not in pp.mux_cmd(tmp_path / "v", tmp_path / "a", tmp_path / "o", "trim")
    text = pp.concat_list_text([tmp_path / "it's.mp4"])
    assert "it'\\''s.mp4" in text


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
async def test_visualizer_and_mux_render(tmp_path):
    audio = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    str(audio)], check=True, capture_output=True)
    vis = await pp.make_visualizer(audio, tmp_path / "vis.mp4")
    assert abs(await pp.probe_duration(vis) - 3) < 0.5

    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=1",
                    str(clip)], check=True, capture_output=True)
    joined = await pp.concat([clip, clip], tmp_path / "joined.mp4", "VERTICAL")
    out = await pp.mux(joined, audio, tmp_path / "final.mp4", "loop", "VERTICAL")
    assert abs(await pp.probe_duration(out) - 3) < 0.5
