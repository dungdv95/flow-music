"""ffmpeg helpers for music videos, plus the lyric-section scene planner."""
from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
from pathlib import Path
from typing import Optional

SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


class FFmpegError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise FFmpegError("ffmpeg/ffprobe not found — brew install ffmpeg")


async def _run(cmd: list[str]) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise FFmpegError(f"{cmd[0]} failed: {err.decode(errors='replace')[-1500:]}")
    return out.decode(errors="replace")


async def probe_duration(path: Path) -> float:
    require_ffmpeg()
    out = await _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ])
    return float(json.loads(out)["format"]["duration"])


def frame_size(orientation: str) -> tuple[int, int]:
    return (1080, 1920) if orientation == "VERTICAL" else (1920, 1080)


def visualizer_cmd(audio: Path, out: Path, image: Optional[Path], orientation: str = "HORIZONTAL",
                   color: str = "white") -> list[str]:
    """Still image (or dark background) with a waveform along the bottom."""
    w, h = frame_size(orientation)
    wave_h = h // 5
    if image:
        inputs = ["-loop", "1", "-i", str(image), "-i", str(audio)]
        bg = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
              f"crop={w}:{h},setsar=1[bg]")
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=0x101018:s={w}x{h}:r=30", "-i", str(audio)]
        bg = "[0:v]setsar=1[bg]"
    graph = (
        f"{bg};"
        f"[1:a]showwaves=s={w}x{wave_h}:mode=cline:rate=30:colors={color}@0.85,format=rgba[wave];"
        f"[bg][wave]overlay=0:{h - wave_h - h // 20}:format=auto,format=yuv420p[v]"
    )
    return [
        "ffmpeg", "-y", *inputs, "-filter_complex", graph,
        "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-r", "30", "-c:a", "aac", "-b:a", "256k", "-shortest", "-movflags", "+faststart", str(out),
    ]


def mux_cmd(video: Path, audio: Path, out: Path, fit: str = "loop",
            orientation: Optional[str] = None) -> list[str]:
    """Put the song under a video. fit=loop repeats visuals to song length;
    fit=trim cuts at whichever ends first."""
    loop = ["-stream_loop", "-1"] if fit == "loop" else []
    vf = []
    if orientation:
        w, h = frame_size(orientation)
        vf = ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"]
    return [
        "ffmpeg", "-y", *loop, "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0", *vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "256k", "-shortest", "-movflags", "+faststart", str(out),
    ]


def concat_list_text(clips: list[Path]) -> str:
    lines = []
    for clip in clips:
        safe = str(Path(clip).resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    return "\n".join(lines) + "\n"


async def make_visualizer(audio: Path, out: Path, image: Optional[Path] = None,
                          orientation: str = "HORIZONTAL") -> Path:
    require_ffmpeg()
    out.parent.mkdir(parents=True, exist_ok=True)
    await _run(visualizer_cmd(audio, out, image, orientation))
    return out


async def mux(video: Path, audio: Path, out: Path, fit: str = "loop",
              orientation: Optional[str] = None) -> Path:
    require_ffmpeg()
    out.parent.mkdir(parents=True, exist_ok=True)
    await _run(mux_cmd(video, audio, out, fit, orientation))
    return out


async def concat(clips: list[Path], out: Path, orientation: str = "HORIZONTAL") -> Path:
    """Concatenate clips, re-encoding so mismatched sources join cleanly."""
    require_ffmpeg()
    if not clips:
        raise FFmpegError("no clips to concat")
    out.parent.mkdir(parents=True, exist_ok=True)
    list_file = out.with_suffix(".concat.txt")
    list_file.write_text(concat_list_text(clips))
    w, h = frame_size(orientation)
    await _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=30",
        "-an", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", str(out),
    ])
    list_file.unlink(missing_ok=True)
    return out


# ─── Scene planning ──────────────────────────────────────────


def parse_sections(lyrics: str) -> list[dict]:
    """Split ``[Verse] / [Chorus]`` tagged lyrics into sections.

    Untagged text becomes one section. Lines inside brackets that aren't on
    their own line (e.g. "(ooh)") stay as lyric text.
    """
    sections: list[dict] = []
    # Text before the first tag goes into an implicit section, dropped if empty.
    current, implicit = {"label": "Intro", "lines": []}, True
    for raw in (lyrics or "").splitlines():
        m = SECTION_RE.match(raw)
        if m:
            if current["lines"] or not implicit:
                sections.append(current)
            current, implicit = {"label": m.group(1).strip(), "lines": []}, False
        elif raw.strip():
            current["lines"].append(raw.strip())
    if current["lines"] or not implicit:
        sections.append(current)
    return sections


def plan_scenes(duration_s: float, lyrics: str = "", clip_seconds: float = 8.0,
                instrumental: bool = False) -> dict:
    """Lay fixed-length video clips across a song and tag each with the lyric
    section it falls in.

    Section timing is an estimate: each section gets time in proportion to its
    line count (instrumental gaps count as two lines). It is a starting point
    for writing scene prompts, not beat-accurate sync.
    """
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")
    clip_seconds = max(1.0, float(clip_seconds))
    sections = [] if instrumental else parse_sections(lyrics)
    if not sections:
        sections = [{"label": "Instrumental", "lines": []}]

    weights = [max(len(s["lines"]), 2) for s in sections]
    total = sum(weights)
    t = 0.0
    timed = []
    for sec, wgt in zip(sections, weights):
        span = duration_s * wgt / total
        timed.append({**sec, "start": round(t, 2), "end": round(t + span, 2)})
        t += span
    timed[-1]["end"] = round(duration_s, 2)

    n = math.ceil(duration_s / clip_seconds)
    scenes = []
    for i in range(n):
        start = i * clip_seconds
        end = min(duration_s, start + clip_seconds)
        mid = (start + end) / 2
        sec = next((s for s in timed if s["start"] <= mid < s["end"]), timed[-1])
        # Lines of this section that land inside this clip's window.
        lines = sec["lines"]
        if lines:
            per_line = (sec["end"] - sec["start"]) / len(lines)
            idx = [j for j in range(len(lines))
                   if start <= sec["start"] + (j + 0.5) * per_line < end]
            clip_lines = [lines[j] for j in idx]
        else:
            clip_lines = []
        scenes.append({
            "index": i,
            "start": round(start, 2),
            "end": round(end, 2),
            "duration": round(end - start, 2),
            "section": sec["label"],
            "lyrics": clip_lines,
        })
    return {
        "duration_s": round(duration_s, 2),
        "clip_seconds": clip_seconds,
        "scene_count": n,
        "sections": [{"label": s["label"], "start": s["start"], "end": s["end"],
                      "line_count": len(s["lines"])} for s in timed],
        "scenes": scenes,
    }
