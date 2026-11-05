"""
tools_creator.py — Agatha's creator & marketing toolkit.

  1. create_clips          cut short-form clips from a video link/file (transcript-aware,
                           9:16 reframing, burned-in captions, optional beat-synced music)
  2. generate_captions     platform-aware hooks, captions, hashtags, CTAs (limits enforced in code)
  3. stage_scheduled_post  schedule uploads (needs your explicit 'confirm schedule')
  4. plan_ad_campaign      budget split, audience segments, ad copy, test plan; export + optimise
  5. build_media_brief     billboard / radio / newspaper / PR / influencer briefs + outreach drafts

Safety posture: nothing here spends money or publishes on its own.
  * Campaigns are DRAFTS (files). No ad-platform account is touched.
  * Scheduled posts only go out after you confirm the schedule.

Optional dependencies (each feature degrades gracefully without them):
  ffmpeg + ffprobe (system)   required for clips
  yt-dlp                      video links          pip install yt-dlp
  faster-whisper              transcripts/captions pip install faster-whisper
  librosa                     beat sync            pip install librosa
  dateparser                  natural-language scheduling times (a basic parser is built in)
"""
import csv
import base64
import glob
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from tools_artifacts import get_artifact_path, record_artifact

_BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("AGATHA_DATA_DIR", os.path.join(_BASE, "agatha_data"))
CREATOR_DIR = os.path.join(DATA_DIR, "creator")
OUTPUT_DIR = os.getenv("AGATHA_CLIP_DIR", os.path.join(_BASE, "creator_output"))
MAX_SOURCE_MINUTES = int(os.getenv("AGATHA_MAX_SOURCE_MINUTES", "180"))

# Hooks injected by main_agent.configure_creator() so this module stays decoupled.
_hooks = {
    "llm": None,        # callable(system: str, prompt: str) -> str | None
    "search": None,     # callable(query: str) -> list[{title,url,snippet}]
    "poster": None,     # callable(platform, content, extra) -> str | None
    "image": None,      # callable(prompt: str, size: str) -> bytes
    "notify": print,    # callable(message)
    "platforms": set(),
    "normalize": lambda p: (p or "").strip().lower(),
}


def configure(llm=None, search=None, poster=None, image=None, notify=None, platforms=None, normalize=None):
    if llm is not None:
        _hooks["llm"] = llm
    if search is not None:
        _hooks["search"] = search
    if poster is not None:
        _hooks["poster"] = poster
    if image is not None:
        _hooks["image"] = image
    if notify is not None:
        _hooks["notify"] = notify
    if platforms is not None:
        _hooks["platforms"] = set(platforms)
    if normalize is not None:
        _hooks["normalize"] = normalize


# ------------------------------------------------------------------ helpers --
def _ask(system: str, prompt: str):
    fn = _hooks["llm"]
    if not fn:
        return None
    try:
        return fn(system, prompt)
    except Exception as e:
        print(f"[Creator] LLM call failed: {e}")
        return None


def _extract_json(raw):
    """Tolerant JSON extraction — small local models wrap JSON in prose/fences."""
    if not raw:
        return None
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    for pattern in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pattern, raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                continue
    return None


def _run(cmd, cwd=None, timeout=3600):
    if cmd and cmd[0] in ("ffmpeg", "ffprobe"):
        executable = _media_tool_path(cmd[0])
        if not executable:
            raise RuntimeError(f"{cmd[0]} is not available")
        cmd = [executable, *cmd[1:]]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError(" | ".join(tail) or f"{cmd[0]} failed")
    return r.stdout


def _media_tool_path(name: str) -> str | None:
    configured = os.getenv(f"AGATHA_{name.upper()}_PATH", "").strip()
    candidates = [configured, shutil.which(name)]
    candidates.extend(os.path.join(prefix, name) for prefix in (
        os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
    ))
    return next((path for path in candidates if path and os.path.isfile(path) and os.access(path, os.X_OK)), None)


def _slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", s or "video").strip("-").lower()[:n] or "video"


def _is_url(s: str) -> bool:
    return bool(re.match(r"https?://", (s or "").strip(), re.IGNORECASE))


def _fit(text: str, limit: int) -> str:
    """Trim to `limit` at a word boundary; return '' if that would gut the text."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut if len(cut) >= limit * 0.5 else ""


def _fit_list(items, count: int, limit: int) -> list:
    out, seen = [], set()
    for it in items or []:
        it = " ".join(str(it).strip().strip('"').split())
        if not it or len(it) > limit:  # ad platforms reject over-length copy, and a chopped headline reads badly: drop it
            continue
        if it.lower() not in seen:
            seen.add(it.lower())
            out.append(it)
        if len(out) == count:
            break
    return out


def _money(x: float, cur: str = "USD") -> str:
    sym = {"USD": "$", "EUR": "€", "GBP": "£", "NGN": "₦"}.get(cur.upper(), cur.upper() + " ")
    return f"{sym}{x:,.0f}" if abs(x) >= 100 else f"{sym}{x:,.2f}"


def _probe(path: str) -> dict:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                "-of", "json", path])
    data = json.loads(out or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }


# ============================================================ 1. VIDEO CLIPS ==
_HOOK_WORDS = {"secret", "never", "always", "why", "how", "biggest", "mistake", "truth", "stop", "wrong",
               "best", "worst", "free", "nobody", "everyone", "actually", "problem", "changed", "learned",
               "crazy", "insane", "story", "reason", "first", "finally", "warning", "hack"}


def _download_video(url: str, workdir: str):
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp isn't installed. Run: pip install yt-dlp")
    opts = {"outtmpl": os.path.join(workdir, "source.%(ext)s"), "noplaylist": True, "quiet": True,
            "no_warnings": True, "merge_output_format": "mp4",
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b"}
    ffmpeg = _media_tool_path("ffmpeg")
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
        if (info.get("duration") or 0) > MAX_SOURCE_MINUTES * 60:
            raise RuntimeError(f"that video is {info['duration'] // 60} min long; my limit is {MAX_SOURCE_MINUTES} "
                               f"(AGATHA_MAX_SOURCE_MINUTES). Give me a shorter source or a local file.")
        ydl.download([url])
    files = [f for f in glob.glob(os.path.join(workdir, "source.*")) if not f.endswith((".part", ".vtt", ".json"))]
    if not files:
        raise RuntimeError("the download finished but I couldn't find the video file")
    return files[0], info.get("title") or "video"


def _download_music(url: str, workdir: str) -> str:
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp isn't installed, so I can't download the song link.")
    opts = {
        "outtmpl": os.path.join(workdir, "song.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
    }
    ffmpeg = _media_tool_path("ffmpeg")
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    music_files = glob.glob(os.path.join(workdir, "song.*"))
    music_files = [path for path in music_files if os.path.splitext(path)[1].lower() in {".mp3", ".m4a", ".wav", ".opus"}]
    if not music_files or not os.path.isfile(music_files[0]) or os.path.getsize(music_files[0]) == 0:
        raise RuntimeError("the song link downloaded no usable audio file")
    return music_files[0]


def _transcribe(path: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None, "faster-whisper isn't installed (pip install faster-whisper), so no transcript"
    try:
        model = WhisperModel(os.getenv("WHISPER_MODEL", "base"), device="cpu", compute_type="int8")
        segs, _ = model.transcribe(path, vad_filter=True)
        return [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
                for s in segs if s.text.strip()], ""
    except Exception as e:
        return None, f"transcription failed ({e})"


def _hook_score(text: str, span: float) -> float:
    words = text.split()
    if len(words) < 8:
        return 0.0
    density = min(len(words) / max(span, 1.0), 4.0) / 4.0
    punct = min(text.count("?") + text.count("!"), 4) / 4.0
    digits = min(len(re.findall(r"\d", text)), 6) / 6.0
    hooks = sum(1 for w in words if w.lower().strip(".,!?\"'") in _HOOK_WORDS)
    return density + punct * 0.8 + digits * 0.3 + min(hooks, 4) * 0.4


def _top_windows(windows, k):
    chosen = []
    for w in sorted(windows, key=lambda w: w["score"], reverse=True):
        if all(w["end"] <= c["start"] or w["start"] >= c["end"] for c in chosen):
            chosen.append(w)
        if len(chosen) == k:
            break
    return sorted(chosen, key=lambda w: w["start"])


def _snap_to_speech(w, segments, duration, clip_seconds):
    start, end = w["start"], w["end"]
    inside = [s for s in segments if s["end"] > start and s["start"] < end]
    if inside:
        if inside[0]["start"] >= start - 3:
            start = max(0.0, inside[0]["start"] - 0.15)
        if inside[-1]["end"] <= end + 3:
            end = min(duration, inside[-1]["end"] + 0.25)
    end = min(end, start + clip_seconds + 3, duration)
    text = " ".join(s["text"] for s in segments if s["end"] > start and s["start"] < end)
    return {"start": start, "end": end, "text": text, "score": w["score"]}


def _pick_moments(segments, duration, clip_seconds, n, instruction):
    if not segments:  # no transcript: evenly spaced, avoiding the very start/end
        moments = []
        for i in range(n):
            centre = duration * (i + 1) / (n + 1)
            s = max(0.0, min(centre - clip_seconds / 2, duration - clip_seconds))
            moments.append({"start": s, "end": min(duration, s + clip_seconds), "text": "", "score": 0.0})
        return moments

    step = max(4, clip_seconds // 4)
    windows, t = [], 0.0
    while t + 8 <= duration:
        end = min(t + clip_seconds, duration)
        text = " ".join(s["text"] for s in segments if s["end"] > t and s["start"] < end)
        windows.append({"start": t, "end": end, "text": text, "score": _hook_score(text, end - t)})
        t += step
    cands = [_snap_to_speech(w, segments, duration, clip_seconds)
             for w in _top_windows(windows, max(n * 3, n))]
    cands = [c for c in cands if c["end"] - c["start"] >= 6] or cands
    if len(cands) <= n:
        return cands

    listing = "\n".join(f"[{i}] ({c['start']:.0f}s-{c['end']:.0f}s) {c['text'][:220]}" for i, c in enumerate(cands))
    raw = _ask(
        "You pick the best moments from a video for TikTok/Reels/Shorts. Reply with ONLY JSON.",
        f"Candidates:\n{listing}\n\nUser request: {instruction or 'best standalone moments'}\n\n"
        f"Pick the {n} best: a strong opening line, understandable on its own, a clear payoff. "
        f'Return a JSON list like [{{"id": 2, "hook": "on-screen hook, max 8 words"}}].')
    picked, hooks = [], {}
    data = _extract_json(raw)
    if isinstance(data, list):
        for item in data:
            try:
                i = int(item["id"])
            except Exception:
                continue
            if 0 <= i < len(cands) and i not in hooks:
                hooks[i] = str(item.get("hook", "")).strip()
                picked.append(i)
    for i in sorted(range(len(cands)), key=lambda i: cands[i]["score"], reverse=True):
        if len(picked) >= n:
            break
        if i not in picked:
            picked.append(i)
    result = []
    for i in picked[:n]:
        c = dict(cands[i])
        if hooks.get(i):
            c["hook"] = hooks[i]
        result.append(c)
    return sorted(result, key=lambda c: c["start"])


def _beat_info(music_path: str):
    try:
        import librosa
        import numpy as np
    except Exception as e:
        return None, f"beat analysis could not load librosa ({e}); install it in Agatha's .venv with .venv/bin/python -m pip install librosa"
    try:
        y, sr = librosa.load(music_path, sr=22050, mono=True, duration=600)
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
        beats = [float(b) for b in beats]
        if len(beats) < 8:
            return None, "I couldn't find a steady beat in that track"
        return {"tempo": float(np.atleast_1d(tempo)[0]), "beats": beats}, ""
    except Exception as e:
        return None, f"beat detection failed ({e})"


def generate_image(prompt: str, size: str = "1024x1024") -> str:
    prompt = (prompt or "").strip()
    if not prompt:
        return "Could not generate image: describe the image you want."
    image_generator = _hooks["image"]
    if not image_generator:
        return "Could not generate image: configure OPENAI_API_KEY to use OpenAI image generation."
    size = size if size in ("1024x1024", "1024x1536", "1536x1024") else "1024x1024"
    try:
        generation_prompt = prompt
        if re.search(r"\b(?:3d|three[- ]dimensional|render|cgi)\b", prompt, re.IGNORECASE):
            generation_prompt += (
                " Create a striking, professionally art-directed 3D render with clear silhouette, "
                "convincing depth, physically based materials, detailed surface texture, cinematic "
                "key and rim lighting, deliberate composition, and a refined color palette. Keep the "
                "subject recognizable and avoid text, logos, and visual clutter."
            )
        image_data = image_generator(generation_prompt, size)
        if not image_data:
            raise RuntimeError("the image provider returned no image data")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        path = os.path.join(OUTPUT_DIR, f"image_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.png")
        with open(path, "wb") as image_file:
            image_file.write(image_data)
        artifact_id = record_artifact("generated_image", path, title=prompt[:80])
        return f"Generated image saved to {path}\nDownload: /artifact/{artifact_id}"
    except Exception as e:
        return f"Could not generate image: {e}"


def _comfyui_workflow_configured() -> bool:
    return bool(os.getenv("AGATHA_COMFYUI_WORKFLOW", "").strip())


def _comfyui_generate_video(description: str, duration: int, workdir: str) -> str:
    workflow_path = os.path.expanduser(os.getenv("AGATHA_COMFYUI_WORKFLOW", "").strip())
    if not workflow_path or not os.path.isfile(workflow_path):
        raise RuntimeError("set AGATHA_COMFYUI_WORKFLOW to a ComfyUI API-format workflow JSON file")
    base_url = os.getenv("AGATHA_COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
    with open(workflow_path, "r", encoding="utf-8") as workflow_file:
        workflow = json.load(workflow_file)

    def replace_tokens(value):
        if isinstance(value, dict):
            return {key: replace_tokens(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_tokens(item) for item in value]
        if isinstance(value, str):
            return (value.replace("{{PROMPT}}", description)
                         .replace("{{DURATION}}", str(duration)))
        return value

    request = urllib.request.Request(
        f"{base_url}/prompt",
        data=json.dumps({"prompt": replace_tokens(workflow)}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        prompt_id = json.load(response).get("prompt_id")
    if not prompt_id:
        raise RuntimeError("ComfyUI did not return a prompt ID; check that the workflow is API format")

    deadline = time.monotonic() + 900
    output_entries = []
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f"{base_url}/history/{prompt_id}", timeout=30) as response:
            history = json.load(response)
        run = history.get(prompt_id, {})
        output_entries = [
            entry for node in run.get("outputs", {}).values()
            for key in ("videos", "gifs", "images")
            for entry in node.get(key, [])
            if isinstance(entry, dict) and os.path.splitext(entry.get("filename", ""))[1].lower()
            in (".mp4", ".mov", ".webm", ".mkv")
        ]
        if output_entries:
            break
        if run.get("status", {}).get("status_str") == "error":
            raise RuntimeError("ComfyUI workflow failed; inspect its console for the node error")
        time.sleep(2)
    if not output_entries:
        raise RuntimeError("ComfyUI did not produce a video within 15 minutes")

    entry = output_entries[0]
    query = urllib.parse.urlencode({
        "filename": entry["filename"], "subfolder": entry.get("subfolder", ""),
        "type": entry.get("type", "output"),
    })
    with urllib.request.urlopen(f"{base_url}/view?{query}", timeout=120) as response:
        output_path = os.path.join(workdir, "comfyui_video" + os.path.splitext(entry["filename"])[1])
        with open(output_path, "wb") as video_file:
            shutil.copyfileobj(response, video_file)
    return output_path


def generate_video(description: str, music_path: str = "", music_url: str = "", duration: int = 20,
                   bpm: int = 120) -> str:
    """Generate a vertical beat-cut video from AI-generated still images."""
    description = (description or "").strip()
    if not description:
        return "Could not generate video: describe the visuals you want."
    missing_tools = [name for name in ("ffmpeg", "ffprobe") if not _media_tool_path(name)]
    if missing_tools:
        return "Could not generate video: install FFmpeg (macOS: brew install ffmpeg)."
    use_comfyui = _comfyui_workflow_configured()
    if not _hooks["image"] and not use_comfyui:
        return ("Could not generate video: configure a local ComfyUI API workflow with "
                "AGATHA_COMFYUI_WORKFLOW, or configure an image-generation provider.")
    duration = max(8, min(int(duration or 20), 90))
    bpm = max(60, min(int(bpm or 120), 200))
    music_path = (music_path or "").strip().strip("\"'")
    music_url = (music_url or "").strip()
    if music_path and _is_url(music_path):
        music_url, music_path = music_path, ""
    if music_url and not _is_url(music_url):
        return "Could not generate video: the song source must be an HTTP or HTTPS link."
    if music_path and not os.path.isfile(os.path.expanduser(music_path)):
        return f"Could not generate video: I can't find the song file '{music_path}'."

    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        workdir = os.path.join(OUTPUT_DIR, f"generated_video_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}")
        os.makedirs(workdir, exist_ok=True)
        music = os.path.expanduser(music_path) if music_path else ""
        if music_url:
            _hooks["notify"]("Downloading the song audio...")
            music = _download_music(music_url, workdir)

        beat = None
        if music:
            beat, why = _beat_info(music)
            if beat is None:
                return f"Could not generate video: {why}."
            bpm = int(round(beat["tempo"])) or bpm

        if use_comfyui:
            _hooks["notify"]("Generating footage with the local ComfyUI workflow...")
            source = _comfyui_generate_video(description, duration, workdir)
            source_info = _probe(source)
            if source_info["duration"] < 2:
                raise RuntimeError("ComfyUI produced a video shorter than 2 seconds")
            if music:
                cut_points = [beat_time for beat_time in beat["beats"]
                              if 0 < beat_time < min(source_info["duration"], duration)]
                cut_points = [0.0, *cut_points[:7]]
                if len(cut_points) < 3:
                    raise RuntimeError("the song has too few detected beats for a synced video")
                durations = [right - left for left, right in zip(cut_points, cut_points[1:])]
                durations.append(max(0.5, min(source_info["duration"], duration) - cut_points[-1]))
                segments = []
                for index, scene_duration in enumerate(durations):
                    segment = f"comfyui_segment_{index:02d}.mp4"
                    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss",
                          f"{cut_points[index]:.3f}", "-i", os.path.basename(source), "-t",
                          f"{scene_duration:.3f}", "-vf",
                          "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
                          "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                          "-pix_fmt", "yuv420p", segment], cwd=workdir)
                    segments.append(segment)
                with open(os.path.join(workdir, "comfyui_concat.txt"), "w", encoding="utf-8") as concat_file:
                    concat_file.writelines(f"file '{segment}'\n" for segment in segments)
                path = os.path.join(workdir, "generated_video_synced.mp4")
                _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                      "-i", "comfyui_concat.txt", "-i", music, "-map", "0:v", "-map", "1:a", "-t",
                      f"{min(source_info['duration'], duration):.3f}", "-af",
                      f"afade=t=out:st={max(duration - 1.2, 0):.2f}:d=1.2", "-c:v", "libx264",
                      "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
                      "-b:a", "160k", "-movflags", "+faststart", os.path.basename(path)], cwd=workdir)
                artifact_id = record_artifact("generated_video", path, title=description[:80])
                return (f"Generated ComfyUI footage and synced {len(segments) - 1} cuts to detected song beats "
                        f"at {path}\nDownload: /artifact/{artifact_id}")
            artifact_id = record_artifact("generated_video", source, title=description[:80])
            return (f"Generated a video with the local ComfyUI workflow at {source}\n"
                    f"Download: /artifact/{artifact_id}")

        beat_seconds = 60.0 / bpm
        bar_seconds = beat_seconds * 4
        cut_count = max(2, min(8, int(duration / bar_seconds)))
        scene_durations = [bar_seconds] * (cut_count - 1)
        scene_durations.append(max(0.5, duration - sum(scene_durations)))
        scene_durations[-1] = max(0.5, duration - sum(scene_durations[:-1]))

        segments = []
        for index, scene_duration in enumerate(scene_durations):
            _hooks["notify"](f"Generating visual {index + 1} of {len(scene_durations)}...")
            visual_prompt = (f"Create a visually coherent vertical video scene, frame {index + 1} of "
                             f"{len(scene_durations)}. Overall concept: {description}. "
                             "Use cinematic composition, no text, no logos, and maintain a consistent visual style.")
            image_data = _hooks["image"](visual_prompt, "1024x1536")
            if not image_data:
                raise RuntimeError(f"image generation returned no data for scene {index + 1}")
            image_name = f"scene_{index:02d}.png"
            with open(os.path.join(workdir, image_name), "wb") as image_file:
                image_file.write(image_data)
            segment_name = f"segment_{index:02d}.mp4"
            _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", "30",
                  "-i", image_name, "-t", f"{scene_duration:.3f}", "-vf",
                  "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
                  "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                  segment_name], cwd=workdir)
            segments.append(segment_name)

        with open(os.path.join(workdir, "concat.txt"), "w", encoding="utf-8") as concat_file:
            concat_file.writelines(f"file '{name}'\n" for name in segments)
        output_name = "generated_video.mp4"
        concat_cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
                      "-i", "concat.txt"]
        if music:
            concat_cmd += ["-i", music, "-map", "0:v", "-map", "1:a", "-t", f"{duration:.3f}",
                           "-af", f"afade=t=out:st={max(duration - 1.2, 0):.2f}:d=1.2",
                           "-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
        else:
            concat_cmd += ["-t", f"{duration:.3f}", "-an", "-c:v", "copy"]
        concat_cmd += ["-movflags", "+faststart", output_name]
        _run(concat_cmd, cwd=workdir)
        path = os.path.join(workdir, output_name)
        artifact_id = record_artifact("generated_video", path, title=description[:80])
        beat_source = "song analysis" if music else f"a {bpm} BPM beat grid"
        return (f"Generated a {duration}s vertical video with {len(segments)} beat-aligned scenes "
                f"({beat_source}) at {path}\nDownload: /artifact/{artifact_id}")
    except Exception as e:
        return f"Could not generate video: {e}"


def _ts(sec: float) -> str:
    sec = max(sec, 0)
    return f"{int(sec // 3600):02d}:{int(sec % 3600 // 60):02d}:{int(sec % 60):02d},{int(round((sec % 1) * 1000)) % 1000:03d}"


def _write_srt(segments, start, end, path, max_words=6) -> bool:
    out, idx = [], 1
    for s in segments:
        if s["end"] <= start or s["start"] >= end:
            continue
        words = s["text"].split()
        if not words:
            continue
        s0, s1 = max(s["start"], start) - start, min(s["end"], end) - start
        chunks = [words[i:i + max_words] for i in range(0, len(words), max_words)]
        span = (s1 - s0) / len(chunks)
        for i, ch in enumerate(chunks):
            out.append(f"{idx}\n{_ts(s0 + i * span)} --> {_ts(s0 + (i + 1) * span)}\n{' '.join(ch)}\n")
            idx += 1
    if not out:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return True


_SUB_STYLE = "FontSize=11,Bold=1,Outline=2,Shadow=0,Alignment=2,MarginV=75"


def _video_graph(reframe: str, srt_name: str = "") -> str:
    """filter_complex fragment that ends in the [v] label."""
    if reframe == "blur":
        g = ("[0:v]split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
             "boxblur=25:5[bg];[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
             "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=30")
    elif reframe == "none":
        g = "[0:v]setsar=1,fps=30"
    else:
        g = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30"
    if srt_name:
        g += f",subtitles={srt_name}:force_style='{_SUB_STYLE}'"
    return g + "[v]"


_ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]


def _render_clip(src, start, dur, out_name, workdir, reframe, srt_name, music, music_off, keep_audio, src_audio):
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", src]
    if music:
        cmd += ["-ss", f"{music_off:.3f}", "-i", music]
    fade = f"afade=t=out:st={max(dur - 1.2, 0):.2f}:d=1.2"
    graph = _video_graph(reframe, srt_name)
    use_audio = True
    if music and keep_audio and src_audio:
        graph += (f";[0:a]anull[o];[1:a]volume=0.25[m];[o][m]amix=inputs=2:duration=first:"
                  f"dropout_transition=0,volume=1.6,{fade}[a]")
    elif music:
        graph += f";[1:a]{fade}[a]"
    elif src_audio:
        graph += f";[0:a]{fade}[a]"
    else:
        use_audio = False
    cmd += ["-t", f"{dur:.3f}", "-filter_complex", graph, "-map", "[v]"]
    if use_audio:
        cmd += ["-map", "[a]"]
    cmd += _ENC + [out_name]
    _run(cmd, cwd=workdir)
    return os.path.join(workdir, out_name)


def _render_montage(src, moments, beat, clip_seconds, music, reframe, workdir):
    """One video whose cuts land on the beat. Returns (path, n_cuts, seconds)."""
    bt, k = beat["beats"], 4  # one cut per bar
    last = 1
    while (last + 1) * k < len(bt) and bt[(last + 1) * k] - bt[0] <= clip_seconds:
        last += 1
    if last < 2:
        raise RuntimeError("the track is too short or too sparse to cut a montage from")
    seg_files, total = [], 0.0
    m = len(moments)
    for i in range(last):
        d = bt[(i + 1) * k] - bt[i * k]
        mo = moments[i % m]
        room = max((mo["end"] - mo["start"]) - d, 0.1)
        s = mo["start"] + ((i // m) * d) % room
        name = f"seg_{i:03d}.mp4"
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{s:.3f}", "-i", src,
              "-t", f"{d:.3f}", "-filter_complex", _video_graph(reframe), "-map", "[v]", "-an",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", name], cwd=workdir)
        seg_files.append(name)
        total += d
    with open(os.path.join(workdir, "concat.txt"), "w") as f:
        f.writelines(f"file '{n}'\n" for n in seg_files)
    out = "montage.mp4"
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "concat.txt",
          "-ss", f"{bt[0]:.3f}", "-i", music, "-map", "0:v", "-map", "1:a", "-t", f"{total:.3f}",
          "-af", f"afade=t=out:st={max(total - 1.2, 0):.2f}:d=1.2", "-c:v", "copy",
          "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out], cwd=workdir)
    for n in seg_files + ["concat.txt"]:
        try:
            os.remove(os.path.join(workdir, n))
        except OSError:
            pass
    return os.path.join(workdir, out), last, total


def create_clips(source: str, instruction: str = "", num_clips: int = 3, clip_seconds: int = 30,
                 music_path: str = "", music_url: str = "", mode: str = "clips", reframe: str = "crop", captions: bool = True,
                 keep_original_audio: bool = False, platform: str = "tiktok") -> str:
    """Cut short-form clips from a video URL or local file.

    mode='clips'   -> separate clips; with music, each is trimmed to whole bars of the song.
    mode='montage' -> ONE video of several moments cut on the beat of `music_path`.
    reframe        -> 'crop' (centre-crop 9:16), 'blur' (fit inside a blurred 9:16 backdrop), 'none'.
    """
    source = (source or "").strip()
    if not source:
        return "Could not make clips: I need a video link or a file path."
    missing_tools = [name for name in ("ffmpeg", "ffprobe") if not _media_tool_path(name)]
    if missing_tools:
        missing = " and ".join(missing_tools)
        return (f"Could not make clips: couldn't find {missing}. Install FFmpeg (macOS: brew install ffmpeg), "
                "or set AGATHA_FFMPEG_PATH and AGATHA_FFPROBE_PATH to the executable paths.")
    num_clips = int(max(1, min(int(num_clips or 3), 8)))
    clip_seconds = int(max(8, min(int(clip_seconds or 30), 90)))
    reframe = reframe if reframe in ("crop", "blur", "none") else "crop"
    mode = mode if mode in ("clips", "montage") else "clips"
    notes = []

    music = ""
    music_url = (music_url or "").strip()
    if music_path:
        candidate = music_path.strip().strip("\"'")
        if _is_url(candidate):
            music_url = candidate
        else:
            music = os.path.expanduser(candidate)
        if music and not os.path.isfile(music):
            return f"Could not make clips: I can't find the music file '{music_path}'."
    if music_url and not _is_url(music_url):
        return "Could not make clips: the song source must be an HTTP or HTTPS link."

    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        if _is_url(source):
            workdir = os.path.join(OUTPUT_DIR, f"clips_{datetime.now():%Y%m%d_%H%M%S}")
            os.makedirs(workdir, exist_ok=True)
            _hooks["notify"]("Downloading the video...")
            src, title = _download_video(source, workdir)
        else:
            src = os.path.expanduser(source.strip("\"'"))
            if not os.path.isfile(src):
                return f"Could not make clips: I can't find '{source}'."
            title = os.path.splitext(os.path.basename(src))[0]
            workdir = os.path.join(OUTPUT_DIR, f"{_slug(title)}_{datetime.now():%Y%m%d_%H%M%S}")
            os.makedirs(workdir, exist_ok=True)

        if music_url:
            _hooks["notify"]("Downloading the song audio...")
            music = _download_music(music_url, workdir)

        info = _probe(src)
        if info["duration"] < 8:
            return "Could not make clips: that video is shorter than 8 seconds."
        if info["duration"] > MAX_SOURCE_MINUTES * 60:
            return f"Could not make clips: that file is longer than my {MAX_SOURCE_MINUTES}-minute limit."

        _hooks["notify"]("Reading the video's speech to find the best moments...")
        segments, why = _transcribe(src)
        if segments is None:
            notes.append(f"{why}. I spaced the clips evenly instead of choosing by content, and can't add captions")
            captions = False
        moments = _pick_moments(segments, info["duration"], clip_seconds, num_clips, instruction)

        beat = None
        if music:
            beat, why = _beat_info(music)
            if beat is None:
                notes.append(f"{why}; the music is added without beat sync")
        if mode == "montage" and (beat is None or len(moments) < 2):
            notes.append("montage needs a detectable beat and at least 2 moments; made separate clips instead")
            mode = "clips"

        results = []
        if mode == "montage":
            _hooks["notify"]("Cutting a beat-synced montage...")
            path, cuts, secs = _render_montage(src, moments, beat, clip_seconds, music, reframe, workdir)
            results.append({"file": path, "start": 0, "end": secs, "hook": "", "text": "",
                            "note": f"{cuts} cuts on the beat at ~{beat['tempo']:.0f} BPM, {secs:.0f}s"})
            notes.append("montage clips have no burned-in captions (they mix several moments)")
        else:
            bars = beat["beats"][::4] if beat else []
            for i, mo in enumerate(moments, 1):
                start, dur = mo["start"], mo["end"] - mo["start"]
                music_off = 0.0
                if bars and len(bars) > 2:
                    music_off = bars[0]
                    reach = [b - bars[0] for b in bars[1:] if b - bars[0] <= max(dur, 8)]
                    dur = min(reach, key=lambda x: abs(x - clip_seconds)) if reach else dur
                dur = min(dur, info["duration"] - start)
                srt_name = ""
                if captions and segments:
                    name = f"clip_{i:02d}.srt"
                    if _write_srt(segments, start, start + dur, os.path.join(workdir, name)):
                        srt_name = name
                _hooks["notify"](f"Rendering clip {i} of {len(moments)}...")
                out = _render_clip(src, start, dur, f"clip_{i:02d}.mp4", workdir, reframe, srt_name,
                                   music, music_off, keep_original_audio, info["has_audio"])
                results.append({"file": out, "start": start, "end": start + dur, "hook": mo.get("hook", ""),
                                "text": mo.get("text", ""), "note": ""})

        # captions per clip (best-effort; skipped without an LLM)
        for r in results:
            if not os.path.isfile(r["file"]) or os.path.getsize(r["file"]) == 0:
                raise RuntimeError(f"the render did not produce a usable file: {r['file']}")

        with open(os.path.join(workdir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"source": source, "title": title, "clips": results}, f, indent=2)

        artifact_links = []
        for index, result in enumerate(results, 1):
            artifact_id = record_artifact("video_clip", result["file"], title=f"{title} - clip {index}")
            if get_artifact_path(artifact_id) != result["file"]:
                raise RuntimeError("clips were rendered but could not be registered for dashboard download")
            artifact_links.append(f"  Download clip {index}: /artifact/{artifact_id}")
        for r in results:
            seed = r["text"] or title
            cap = _captions_for(f"{r.get('hook') or ''} {seed}".strip()[:600], [platform], variants=1)
            if cap and platform_key(platform) in cap:
                r["caption"] = cap[platform_key(platform)][0]
    except Exception as e:
        return f"Could not make clips: {e}"

    lines = [f"Made {len(results)} {'montage' if mode == 'montage' else 'clip(s)'} from '{title}' in {workdir}"]
    for i, r in enumerate(results, 1):
        span = f"{r['start']:.0f}s-{r['end']:.0f}s" if mode != "montage" else r["note"]
        lines.append(f"  {i}. {os.path.basename(r['file'])} ({span})" + (f" — hook: {r['hook']}" if r.get("hook") else ""))
        cap = r.get("caption")
        if cap:
            lines.append(f"     caption: {cap['caption'].splitlines()[0][:120]}")
    lines.extend(artifact_links)
    if notes:
        lines.append("Notes: " + "; ".join(notes) + ".")
    lines.append("Only cut and post footage and music you own or have permission to use.")
    return "\n".join(lines)


# ============================================================== 2. CAPTIONS ==
_PLATFORM_ALIASES = {"twitter": "x", "reels": "instagram", "ig": "instagram", "insta": "instagram",
                     "shorts": "youtube", "yt": "youtube", "fb": "facebook", "tik tok": "tiktok", "bsky": "bluesky"}
_CAPTION_LIMITS = {"tiktok": 2200, "instagram": 2200, "x": 280, "youtube": 100, "linkedin": 3000,
                   "bluesky": 300, "facebook": 2000}
_CAPTION_STYLE = {
    "tiktok": "hook in the first line (max 8 words), conversational, 3-5 hashtags",
    "instagram": "first line is the hook (it shows before 'more'), short lines, 5-10 relevant hashtags at the end",
    "x": "under 280 characters INCLUDING hashtags, punchy, 0-2 hashtags",
    "youtube": "this is the TITLE (max 70 characters), keyword-forward, curiosity without clickbait; hashtags optional",
    "linkedin": "professional but human, an insight or short story, line breaks, 3 hashtags",
    "bluesky": "under 300 characters, natural voice, minimal hashtags",
    "facebook": "friendly and conversational, end with a question, 0-3 hashtags",
}


def platform_key(p: str) -> str:
    p = (p or "").strip().lower()
    return _PLATFORM_ALIASES.get(p, p)


def _captions_for(content: str, platforms, tone: str = "", goal: str = "engagement", cta: str = "",
                  audience: str = "", variants: int = 1):
    """-> {platform: [ {hook, caption, hashtags, cta, chars, limit}, ... ]} or None if no LLM."""
    if not _hooks["llm"]:
        return None
    out = {}
    for raw in platforms:
        p = platform_key(raw)
        if p not in _CAPTION_LIMITS:
            continue
        limit = _CAPTION_LIMITS[p]
        reply = _ask(
            "You are a social media copywriter. Reply with ONLY JSON.",
            f"Write {variants} caption variant(s) for {p}.\nContent: {content}\n"
            f"Goal: {goal}. Tone: {tone or 'natural, confident, human'}. "
            f"{('Audience: ' + audience + '. ') if audience else ''}"
            f"{('Call to action: ' + cta + '. ') if cta else ''}\n"
            f"Platform rules: {_CAPTION_STYLE[p]}. Max {limit} characters.\n"
            'Return {"variants": [{"hook": "...", "caption": "...", "hashtags": ["#a", "#b"], "cta": "..."}]}')
        data = _extract_json(reply)
        items = (data or {}).get("variants") if isinstance(data, dict) else (data if isinstance(data, list) else None)
        built = []
        for it in items or []:
            if not isinstance(it, dict) or not str(it.get("caption", "")).strip():
                continue
            caption = str(it["caption"]).strip()
            tags = [("#" + t.lstrip("#").replace(" ", "")) for t in (it.get("hashtags") or []) if str(t).strip()][:10]
            cta_text = str(it.get("cta") or "").strip()
            body = caption + (f"\n{cta_text}" if cta_text and cta_text.lower() not in caption.lower() else "")
            while tags and len(body) + 2 + len(" ".join(tags)) > limit:
                tags.pop()
            if len(body) + (2 + len(" ".join(tags)) if tags else 0) > limit:
                body = _fit(body, limit - (2 + len(" ".join(tags)) if tags else 0)) or body[:limit]
            full = body + (("\n\n" + " ".join(tags)) if tags else "")
            built.append({"hook": str(it.get("hook", "")).strip(), "caption": full, "hashtags": tags,
                          "cta": cta_text, "chars": len(full), "limit": limit})
        if not built and reply and not data:  # model ignored the JSON instruction: use its text
            text = _fit(reply.strip(), limit)
            if text:
                built.append({"hook": "", "caption": text, "hashtags": [], "cta": "", "chars": len(text), "limit": limit})
        if built:
            out[p] = built[:variants]
    return out or None


def generate_captions(content: str, platforms: str = "tiktok,instagram,x", tone: str = "",
                      goal: str = "engagement", cta: str = "", audience: str = "", variants: int = 2) -> str:
    """Hooks, captions, hashtags and CTAs per platform, with each platform's length limit enforced."""
    if not (content or "").strip():
        return "Could not write captions: tell me what the content is about."
    names = platforms if isinstance(platforms, list) else re.split(r"[,\s/&]+|\band\b", platforms or "")
    names = [platform_key(n) for n in names if n and n.strip()]
    unknown = [n for n in names if n not in _CAPTION_LIMITS]
    names = [n for n in names if n in _CAPTION_LIMITS] or ["tiktok", "instagram", "x"]
    if not _hooks["llm"]:
        return "Could not write captions: no LLM is configured."
    result = _captions_for(content, names, tone, goal, cta, audience, int(max(1, min(int(variants or 2), 3))))
    if not result:
        return "Could not write captions right now — the language model didn't return anything usable. Try again."
    lines = []
    for p, items in result.items():
        for i, it in enumerate(items, 1):
            label = p.upper() + (f" — option {i}" if len(items) > 1 else "")
            lines.append(f"{label}" + (f"\nHook: {it['hook']}" if it["hook"] else "") +
                         f"\n{it['caption']}\n({it['chars']}/{it['limit']} characters)")
    if unknown:
        lines.append(f"(Skipped unsupported platform(s): {', '.join(unknown)}. Supported: {', '.join(_CAPTION_LIMITS)}.)")
    return "\n\n".join(lines)


# ========================================================= 3. SCHEDULED POSTS ==
SCHEDULE_FILE = os.path.join(CREATOR_DIR, "schedule.json")
MISSED_GRACE_MIN = int(os.getenv("AGATHA_MISSED_GRACE_MIN", "30"))
_sched_lock = threading.RLock()
_pending_schedule = {}
_sched_thread = None
_sched_stop = threading.Event()
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _load_schedule() -> list:
    with _sched_lock:
        try:
            with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []


def _save_schedule(posts: list):
    with _sched_lock:
        os.makedirs(CREATOR_DIR, exist_ok=True)
        tmp = SCHEDULE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(posts, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SCHEDULE_FILE)


def _fallback_parse(t: str, now: datetime):
    s = t.lower().strip()
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})[ t](\d{1,2}):(\d{2})", s)
    if m:
        return datetime(*[int(x) for x in m.groups()])
    m = re.search(r"\bin\s+(\d+)\s*(min|minute|hour|hr|day|week)s?\b", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        step = {"min": timedelta(minutes=n), "minute": timedelta(minutes=n), "hour": timedelta(hours=n),
                "hr": timedelta(hours=n), "day": timedelta(days=n), "week": timedelta(weeks=n)}[unit]
        return now + step
    tm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", s) or re.search(r"\b(\d{1,2}):(\d{2})()\b", s)
    if not tm:
        return None
    hour, minute, ampm = int(tm.group(1)), int(tm.group(2) or 0), tm.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    day = None
    if "tomorrow" in s:
        day = (now + timedelta(days=1)).date()
    elif "today" in s or "tonight" in s:
        day = now.date()
    else:
        for i, wd in enumerate(_WEEKDAYS):
            if wd in s:
                ahead = (i - now.weekday()) % 7
                day = (now + timedelta(days=ahead)).date()
                break
    result = datetime.combine(day or now.date(), datetime.min.time()).replace(hour=hour, minute=minute)
    if result <= now:
        result += timedelta(days=7 if day and any(wd in s for wd in _WEEKDAYS) else 1)
    return result


def parse_when(text: str, now: datetime = None):
    """Natural-language time -> naive local datetime (None if not understood)."""
    now = now or datetime.now()
    text = (text or "").strip()
    if not text:
        return None
    try:
        import dateparser
        d = dateparser.parse(text, settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now})
        if d:
            return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
    except ImportError:
        pass
    except Exception:
        pass
    return _fallback_parse(text, now)


def stage_scheduled_post(platform: str, content: str, when: str, file_path: str = "",
                         image_url: str = "", title: str = "") -> str:
    """Stage a post for a future time. Nothing is queued until the user says 'confirm schedule'."""
    global _pending_schedule
    p = _hooks["normalize"](platform_key(platform))
    supported = _hooks["platforms"]
    if supported and p not in supported:
        return f"Could not schedule: '{platform}' isn't supported. Supported: {', '.join(sorted(supported))}."
    dt = parse_when(when)
    if dt is None:
        return f"Could not schedule: I couldn't understand the time '{when}'. Try 'tomorrow at 5pm' or '2026-10-25 17:30'."
    now = datetime.now()
    if dt < now + timedelta(seconds=20):
        return f"Could not schedule: {dt:%a %b %d, %I:%M %p} is in the past. Pick a future time."
    if dt > now + timedelta(days=366):
        return "Could not schedule: that's more than a year away."
    extra = {}
    if p in ("youtube", "tiktok"):
        fp = os.path.expanduser((file_path or "").strip().strip("\"'"))
        if not fp or not os.path.isfile(fp):
            return f"Could not schedule: {p} posts need a video file that exists. I can't find '{file_path}'."
        extra["file_path"] = fp
        if title:
            extra["title"] = title
    elif p == "instagram":
        if not (image_url or "").startswith("http"):
            return "Could not schedule: Instagram posts need a public image_url (https://...)."
        extra["image_url"] = image_url
    if not (content or "").strip() and p != "youtube":
        return "Could not schedule: there's no post text."
    _pending_schedule = {"platform": p, "content": (content or "").strip(), "extra": extra, "when": dt.isoformat()}
    preview = _fit(content, 160) or (content or "")[:160]
    media = f" with {os.path.basename(extra['file_path'])}" if "file_path" in extra else ""
    running = "" if (_sched_thread and _sched_thread.is_alive()) else " (the scheduler isn't running right now)"
    return (f"Ready to schedule a {p} post{media} for {dt:%a %b %d at %I:%M %p}: \"{preview}\". "
            f"Say 'confirm schedule' to lock it in, or 'cancel schedule' to discard it. "
            f"I have to be running at that time for it to go out{running}.")


def confirm_scheduled_post() -> str:
    global _pending_schedule
    if not _pending_schedule:
        return "There's no staged schedule to confirm."
    post = dict(_pending_schedule, id="p_" + uuid.uuid4().hex[:6], status="pending",
                created=datetime.now().isoformat(), result="")
    _pending_schedule = {}
    with _sched_lock:
        posts = _load_schedule()
        posts.append(post)
        _save_schedule(posts)
    when = datetime.fromisoformat(post["when"])
    return f"Scheduled {post['id']}: {post['platform']} on {when:%a %b %d at %I:%M %p}. Say 'list scheduled posts' to review or 'cancel scheduled {post['id']}' to remove it."


def cancel_pending_schedule() -> str:
    global _pending_schedule
    if not _pending_schedule:
        return "There's no staged schedule to cancel."
    _pending_schedule = {}
    return "Discarded. Nothing was scheduled."


def has_pending_schedule() -> bool:
    return bool(_pending_schedule)


def list_scheduled_posts() -> str:
    posts = _load_schedule()
    if not posts:
        return "No scheduled posts."
    pending = sorted([p for p in posts if p["status"] == "pending"], key=lambda p: p["when"])
    other = sorted([p for p in posts if p["status"] != "pending"], key=lambda p: p["when"], reverse=True)[:5]
    lines = []
    for p in pending + other:
        when = datetime.fromisoformat(p["when"])
        lines.append(f"{p['id']} [{p['status']}] {p['platform']} {when:%a %b %d %I:%M %p} — {_fit(p['content'], 60) or p['content'][:60]}"
                     + (f"  ({p['result'][:80]})" if p["status"] in ("failed", "missed", "unknown") and p.get("result") else ""))
    return "\n".join(lines)


def cancel_scheduled_post(post_id: str) -> str:
    with _sched_lock:
        posts = _load_schedule()
        for p in posts:
            if p["id"] == post_id.strip():
                if p["status"] != "pending":
                    return f"Could not cancel {post_id}: it's already {p['status']}."
                p["status"] = "cancelled"
                _save_schedule(posts)
                return f"Cancelled {post_id}."
    return f"Could not cancel: no scheduled post with id '{post_id}'."


def _publish(post: dict):
    poster = _hooks["poster"]
    if not poster:
        return False, "no posting function is configured"
    try:
        result = poster(post["platform"], post["content"], post.get("extra", {}))
    except Exception as e:
        return False, str(e)
    if result is None:
        return False, f"posting to {post['platform']} isn't available (its module failed to load)"
    low = str(result).strip().lower()
    if low.startswith(("error", "failed", "could not", "couldn't", "can't")) or "not configured" in low:
        return False, str(result)
    return True, str(result)


def _process_due():
    now, due = datetime.now(), []
    with _sched_lock:
        posts = _load_schedule()
        for p in posts:
            if p["status"] != "pending":
                continue
            when = datetime.fromisoformat(p["when"])
            if when > now:
                continue
            if (now - when).total_seconds() > MISSED_GRACE_MIN * 60:
                p["status"], p["result"] = "missed", f"was {int((now - when).total_seconds() // 60)} min overdue; not posted automatically"
                _hooks["notify"](f"Scheduled post {p['id']} ({p['platform']}) was missed while I was off. Say 'list scheduled posts' to decide what to do.")
            else:
                p["status"] = "posting"  # saved BEFORE the call so a crash can never double-post
                due.append(dict(p))
        _save_schedule(posts)
    for p in due:
        ok, result = _publish(p)
        with _sched_lock:
            posts = _load_schedule()
            for q in posts:
                if q["id"] == p["id"]:
                    q["status"], q["result"] = ("posted" if ok else "failed"), result[:300]
            _save_schedule(posts)
        _hooks["notify"](f"Scheduled {p['platform']} post {p['id']} " + ("went out." if ok else f"failed: {result[:160]}"))


def _scheduler_loop(poll: int):
    while not _sched_stop.wait(poll):
        try:
            _process_due()
        except Exception as e:
            print(f"[Scheduler] error: {e}")


def start_scheduler(poll_seconds: int = 20):
    """Call once at startup. Safe to call repeatedly."""
    global _sched_thread
    if _sched_thread and _sched_thread.is_alive():
        return
    with _sched_lock:
        posts = _load_schedule()
        changed = False
        for p in posts:
            if p["status"] == "posting":  # crashed mid-post: don't risk a duplicate
                p["status"], p["result"] = "unknown", "I stopped while posting this; check the platform before retrying"
                changed = True
        if changed:
            _save_schedule(posts)
        overdue = sum(1 for p in posts if p["status"] == "pending")
    _sched_stop.clear()
    _sched_thread = threading.Thread(target=_scheduler_loop, args=(poll_seconds,), daemon=True, name="agatha-scheduler")
    _sched_thread.start()
    if overdue:
        _hooks["notify"](f"{overdue} scheduled post(s) are queued.")


def stop_scheduler():
    _sched_stop.set()


# ================================================================ 4. CAMPAIGNS ==
CAMPAIGN_DIR = os.path.join(CREATOR_DIR, "campaigns")
EXPORT_DIR = os.path.join(CREATOR_DIR, "exports")
BRIEF_DIR = os.path.join(CREATOR_DIR, "briefs")

_GOAL_ALIASES = {"conversions": "sales", "purchases": "sales", "revenue": "sales", "sell": "sales", "brand": "awareness",
                 "reach": "awareness", "followers": "awareness", "growth": "awareness", "clicks": "traffic",
                 "visits": "traffic", "signups": "leads", "sign-ups": "leads", "lead": "leads",
                 "listens": "streams", "music": "streams", "streaming": "streams"}
_GOAL_WEIGHTS = {
    "sales": {"google_ads": .35, "meta": .35, "tiktok_ads": .15, "youtube_ads": .15},
    "awareness": {"tiktok_ads": .30, "meta": .30, "youtube_ads": .25, "google_ads": .15},
    "traffic": {"google_ads": .40, "meta": .30, "tiktok_ads": .20, "x_ads": .10},
    "leads": {"google_ads": .40, "linkedin_ads": .30, "meta": .30},
    "streams": {"tiktok_ads": .35, "meta": .35, "youtube_ads": .30},
}
_OFFLINE_WEIGHTS = {"billboard": .15, "radio": .12, "newspaper": .10, "influencer": .20, "pr": .05}
_PLATFORM_ALIASES2 = {"google": "google_ads", "adwords": "google_ads", "search": "google_ads", "facebook": "meta",
                      "instagram": "meta", "fb": "meta", "meta_ads": "meta", "tiktok": "tiktok_ads",
                      "youtube": "youtube_ads", "x": "x_ads", "twitter": "x_ads", "linkedin": "linkedin_ads",
                      "billboards": "billboard", "ooh": "billboard", "press": "pr", "media": "pr",
                      "newspapers": "newspaper", "influencers": "influencer"}
_LABELS = {"google_ads": "Google Ads (Search)", "meta": "Meta (Facebook/Instagram)", "tiktok_ads": "TikTok Ads",
           "youtube_ads": "YouTube Ads", "x_ads": "X Ads", "linkedin_ads": "LinkedIn Ads", "billboard": "Billboard",
           "radio": "Radio", "newspaper": "Newspaper", "influencer": "Influencer", "pr": "PR / media coverage"}
_COPY_SPECS = {
    "google_ads": {"keywords": (12, 40, "search phrases a buyer would type"),
                   "negative_keywords": (6, 30, "words that signal the wrong audience (free, jobs, diy...)"),
                   "headlines": (10, 30, "ad headlines"), "descriptions": (4, 90, "ad descriptions")},
    "meta": {"primary_texts": (3, 125, "feed ad text"), "headlines": (3, 40, "ad headlines"),
             "interests": (8, 40, "interest/behaviour targeting ideas")},
    "tiktok_ads": {"ad_texts": (3, 100, "ad captions"), "video_hooks": (3, 80, "the first 2 seconds of the video"),
                   "interests": (8, 40, "interest targeting ideas")},
    "youtube_ads": {"video_hooks": (3, 80, "the first 5 seconds (before the skip button)"),
                    "headlines": (3, 40, "headlines"), "interests": (8, 40, "affinity/in-market audience ideas")},
    "x_ads": {"posts": (3, 260, "promoted post text")},
    "linkedin_ads": {"intro_texts": (3, 150, "sponsored post intro text"), "headlines": (3, 70, "headlines"),
                     "job_targets": (6, 40, "job titles or industries to target")},
}


def _goal_key(goal: str) -> str:
    g = (goal or "sales").strip().lower()
    g = _GOAL_ALIASES.get(g, g)
    return g if g in _GOAL_WEIGHTS else "sales"


def _platform_list(platforms, goal: str) -> list:
    if isinstance(platforms, str):
        platforms = re.split(r"[,/&]+|\band\b|\s{2,}", platforms)
    out = []
    for p in platforms or []:
        p = (p or "").strip().lower().replace(" ", "_")
        p = _PLATFORM_ALIASES2.get(p, p)
        if (p in _LABELS) and p not in out:
            out.append(p)
    if not out:
        weights = _GOAL_WEIGHTS[goal]
        out = sorted(weights, key=weights.get, reverse=True)[:3]
    return out


def allocate_budget(goal: str, platforms: list, total: float, duration_days: int, currency: str = "USD") -> dict:
    """Deterministic budget split (no LLM): goal-weighted, then drop channels too small to learn from."""
    goal = _goal_key(goal)
    weights = {p: _GOAL_WEIGHTS[goal].get(p, _OFFLINE_WEIGHTS.get(p, 0.12)) for p in platforms}
    dropped, warnings = [], []
    min_test = float(os.getenv("AGATHA_MIN_TEST_BUDGET", "100"))
    enforce = currency.upper() in ("USD", "EUR", "GBP") or "AGATHA_MIN_TEST_BUDGET" in os.environ
    while enforce and len(weights) > 1:
        s = sum(weights.values())
        small = [p for p in weights if p != "pr" and total * weights[p] / s < min_test]
        if not small:
            break
        worst = min(small, key=lambda p: weights[p])
        dropped.append(worst)
        del weights[worst]
    if dropped:
        warnings.append(f"Dropped {', '.join(_LABELS[p] for p in dropped)}: with {_money(total, currency)} each would get less than "
                        f"~{_money(min_test, currency)}, too little to learn anything. Fewer channels, tested properly, beats spreading thin.")
    heavy = [p for p in weights if p in ("billboard", "radio", "newspaper")]
    if heavy:
        warnings.append(f"{', '.join(_LABELS[p] for p in heavy)} often {'has' if len(heavy) == 1 else 'have'} higher minimums than online ads. Get a rate card "
                        f"(say 'media brief for {heavy[0]}') before committing this share; if it's out of reach, move it online.")
    if not enforce:
        warnings.append(f"I don't know sensible minimum test budgets in {currency}; avoid splitting across more channels than you can learn from.")
    s = sum(weights.values()) or 1
    rows = [{"platform": p, "pct": round(100 * w / s, 1), "amount": round(total * w / s, 2),
             "daily": round(total * w / s / max(duration_days, 1), 2)} for p, w in weights.items()]
    return {"rows": rows, "dropped": dropped, "warnings": warnings}


def _save_campaign(c: dict) -> str:
    os.makedirs(CAMPAIGN_DIR, exist_ok=True)
    path = os.path.join(CAMPAIGN_DIR, f"{c['id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)
    return path


def _load_campaign(cid: str):
    try:
        with open(os.path.join(CAMPAIGN_DIR, f"{(cid or '').strip()}.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _gen_audience(product, goal, audience, location):
    data = _extract_json(_ask(
        "You are a performance marketing strategist. Reply with ONLY JSON.",
        f"Product/offer: {product}\nGoal: {goal}\nStated audience: {audience or 'not specified'}\n"
        f"Location: {location or 'not specified'}\n"
        'Return {"summary": "one sentence", "segments": [{"name": "...", "who": "...", "interests": ["..."], '
        '"why": "why they would buy"}]} with exactly 3 distinct segments to test against each other.'))
    if isinstance(data, dict) and isinstance(data.get("segments"), list) and data["segments"]:
        segs = []
        for s in data["segments"][:3]:
            if isinstance(s, dict) and s.get("name"):
                segs.append({"name": str(s["name"])[:60], "who": str(s.get("who", ""))[:200],
                             "interests": [str(i)[:40] for i in (s.get("interests") or [])][:8],
                             "why": str(s.get("why", ""))[:200]})
        if segs:
            return {"summary": str(data.get("summary", ""))[:250], "segments": segs}
    return {"summary": audience or "", "segments": [{"name": "Your stated audience", "who": audience or "(not specified)",
                                                    "interests": [], "why": ""}]}


def _gen_platform_copy(platform, product, goal, audience_summary, location, landing_url):
    spec = _COPY_SPECS.get(platform)
    if not spec or not _hooks["llm"]:
        return {}
    keys = "; ".join(f'"{k}": list of {n} strings, each max {m} characters ({d})' for k, (n, m, d) in spec.items())
    data = _extract_json(_ask(
        "You are a direct-response copywriter and media buyer. Reply with ONLY JSON.",
        f"Platform: {_LABELS[platform]}\nProduct/offer: {product}\nGoal: {goal}\nAudience: {audience_summary}\n"
        f"Location: {location or 'n/a'}\nLanding page: {landing_url or 'n/a'}\n"
        f"Write specific, honest copy (no fake claims, no made-up numbers or testimonials).\nReturn JSON with: {keys}"))
    if not isinstance(data, dict):
        return {}
    return {k: _fit_list(data.get(k), n, m) for k, (n, m, _) in spec.items() if data.get(k)}


def plan_ad_campaign(product: str, goal: str = "sales", budget: float = 0, duration_days: int = 14,
                     platforms: str = "", audience: str = "", location: str = "", currency: str = "USD",
                     landing_url: str = "") -> str:
    """Create a DRAFT campaign plan: budget split, audiences, ad copy, test plan. Nothing is launched or spent."""
    product = (product or "").strip()
    if not product:
        return "Could not plan a campaign: tell me what you're promoting."
    try:
        budget, duration_days = float(budget or 0), int(duration_days)
    except Exception:
        return "Could not plan a campaign: budget and duration need to be numbers."
    if budget <= 0:
        return "Could not plan a campaign: what's your total budget (and currency)? I don't want to guess how much you'll spend."
    if not 1 <= duration_days <= 365:
        return "Could not plan a campaign: duration needs to be 1-365 days."
    goal = _goal_key(goal)
    plats = _platform_list(platforms, goal)
    alloc = allocate_budget(goal, plats, budget, duration_days, currency)

    audience_data = _gen_audience(product, goal, audience, location) if _hooks["llm"] else \
        {"summary": audience, "segments": [{"name": "Your stated audience", "who": audience or "(not specified)", "interests": [], "why": ""}]}
    copy = {r["platform"]: _gen_platform_copy(r["platform"], product, goal, audience_data["summary"] or audience,
                                              location, landing_url) for r in alloc["rows"]}
    angles = []
    if _hooks["llm"]:
        data = _extract_json(_ask("You are a creative strategist. Reply with ONLY a JSON list of strings.",
                                  f"Product: {product}\nGoal: {goal}\nAudience: {audience_data['summary']}\n"
                                  "Give 4 distinct creative angles to A/B test (each under 15 words): e.g. problem-first, "
                                  "social proof, behind-the-scenes, offer-led."))
        angles = _fit_list(data if isinstance(data, list) else [], 4, 120)

    p1, p2 = max(1, round(duration_days * 0.25)), max(2, round(duration_days * 0.6))
    cid = "c_" + uuid.uuid4().hex[:6]
    campaign = {
        "id": cid, "created": datetime.now().isoformat(), "status": "draft", "product": product, "goal": goal,
        "budget": budget, "currency": currency.upper(), "duration_days": duration_days, "location": location,
        "landing_url": landing_url, "budget_plan": alloc, "audience": audience_data, "copy": copy,
        "creative_angles": angles,
        "test_plan": [
            f"Days 1-{p1} (learn): per channel, run 3 creatives x 2 audiences at equal budget. Don't edit mid-phase.",
            f"Days {p1 + 1}-{p2} (cut): pause the bottom half of ads once each has roughly 1,000 impressions or 50 clicks; move budget to the winners.",
            f"Days {p2 + 1}-{duration_days} (scale): raise winners' budget about 20% every 2-3 days while cost per result holds; "
            "refresh creative if click-through falls or people are seeing the ad more than ~3 times.",
        ],
        "tracking": [
            "Install the platform pixel / conversion tracking BEFORE spending a cent.",
            f"Tag every link: ?utm_source=<platform>&utm_medium=paid&utm_campaign={cid}",
            "Offline channels (billboard/radio/newspaper): use a unique URL, QR code or promo code per channel.",
        ],
        "results": [],
    }
    path = _save_campaign(campaign)

    lines = [f"Draft campaign {cid} for '{product}': goal {goal}, {duration_days} days, {_money(budget, currency)}.", "Budget:"]
    for r in alloc["rows"]:
        lines.append(f"  - {_LABELS[r['platform']]}: {_money(r['amount'], currency)} ({r['pct']}%, ~{_money(r['daily'], currency)}/day)")
    lines += [f"  ! {w}" for w in alloc["warnings"]]
    lines.append("Audiences to test: " + "; ".join(s["name"] for s in audience_data["segments"]))
    if angles:
        lines.append("Creative angles: " + "; ".join(angles))
    if not _hooks["llm"] or not any(copy.values()):
        lines.append("(Ad copy wasn't generated — no language model was available. The budget and test plan are still valid.)")
    lines.append(f"Saved to {path}. NOTHING has been launched or spent.")
    lines.append(f"Next: 'export campaign {cid}' for a launch checklist + Google Ads import file. Once it's running, give me the numbers and I'll optimise it.")
    return "\n".join(lines)


def list_campaigns() -> str:
    files = sorted(glob.glob(os.path.join(CAMPAIGN_DIR, "c_*.json")), key=os.path.getmtime, reverse=True)
    if not files:
        return "No campaigns yet. Ask me to plan one."
    lines = []
    for fp in files[:10]:
        try:
            c = json.load(open(fp, encoding="utf-8"))
            lines.append(f"{c['id']} [{c['status']}] {c['product'][:40]} — {c['goal']}, {_money(c['budget'], c['currency'])}, "
                         f"{c['duration_days']}d, {len(c.get('results', []))} result log(s)")
        except Exception:
            continue
    return "\n".join(lines)


def export_campaign(campaign_id: str, fmt: str = "markdown") -> str:
    """fmt: 'markdown' (launch checklist) or 'google_ads_csv' (Google Ads Editor-style import file)."""
    c = _load_campaign(campaign_id)
    if not c:
        return f"Could not export: no campaign with id '{campaign_id}'. Say 'list campaigns'."
    os.makedirs(EXPORT_DIR, exist_ok=True)
    cur = c["currency"]
    if (fmt or "").lower() in ("google_ads_csv", "google", "csv"):
        g = c["copy"].get("google_ads", {})
        row = next((r for r in c["budget_plan"]["rows"] if r["platform"] == "google_ads"), None)
        if not row or not g.get("headlines") or not g.get("descriptions"):
            return "Could not export a Google Ads file: this campaign has no Google Ads budget or generated search copy."
        if len(g["headlines"]) < 3 or len(g["descriptions"]) < 2:
            return "Could not export a Google Ads file: Google needs at least 3 headlines and 2 descriptions, and fewer survived the length limits."
        name = f"{_slug(c['product'], 24)}-{c['id']}"
        heads = [f"Headline {i}" for i in range(1, 16)]
        descs = [f"Description {i}" for i in range(1, 5)]
        cols = ["Campaign", "Campaign Type", "Campaign Daily Budget", "Ad Group", "Keyword", "Criterion Type",
                "Ad type", "Final URL"] + heads + descs
        path = os.path.join(EXPORT_DIR, f"{c['id']}_google_ads.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerow({"Campaign": name, "Campaign Type": "Search", "Campaign Daily Budget": row["daily"]})
            w.writerow({"Campaign": name, "Ad Group": "Core"})
            for k in g.get("keywords", []):
                w.writerow({"Campaign": name, "Ad Group": "Core", "Keyword": k, "Criterion Type": "Phrase"})
                w.writerow({"Campaign": name, "Ad Group": "Core", "Keyword": k, "Criterion Type": "Exact"})
            for k in g.get("negative_keywords", []):
                w.writerow({"Campaign": name, "Keyword": k, "Criterion Type": "Negative Phrase"})
            ad = {"Campaign": name, "Ad Group": "Core", "Ad type": "Responsive search ad", "Final URL": c.get("landing_url", "")}
            ad.update({h: v for h, v in zip(heads, g["headlines"][:15])})
            ad.update({d: v for d, v in zip(descs, g["descriptions"][:4])})
            w.writerow(ad)
        warn = "" if c.get("landing_url") else " Add your Final URL before importing."
        return (f"Saved {path}. Headlines are ≤30 and descriptions ≤90 characters, as Google requires. In Google Ads Editor use "
                f"Account > Import > From file; try a tiny test import first, since Editor's column names change occasionally.{warn}")
    lines = [f"# Campaign {c['id']}: {c['product']}", f"Goal: {c['goal']} | {c['duration_days']} days | {_money(c['budget'], cur)} "
             f"| Location: {c.get('location') or 'n/a'}", "", "## Budget"]
    for r in c["budget_plan"]["rows"]:
        lines.append(f"- {_LABELS[r['platform']]}: {_money(r['amount'], cur)} ({r['pct']}%, ~{_money(r['daily'], cur)}/day)")
    lines += [f"- NOTE: {w}" for w in c["budget_plan"]["warnings"]]
    lines += ["", "## Audiences to test", c["audience"].get("summary", "")]
    for s in c["audience"]["segments"]:
        lines.append(f"- **{s['name']}**: {s['who']} | interests: {', '.join(s['interests'])} | {s['why']}")
    if c["creative_angles"]:
        lines += ["", "## Creative angles to A/B test"] + [f"- {a}" for a in c["creative_angles"]]
    for plat, fields in c["copy"].items():
        if fields:
            lines += ["", f"## {_LABELS[plat]} copy"]
            for k, vals in fields.items():
                lines.append(f"**{k.replace('_', ' ')}**")
                lines += [f"- {v}" for v in vals]
    lines += ["", "## Test plan"] + [f"- {t}" for t in c["test_plan"]] + ["", "## Before you spend"] + [f"- [ ] {t}" for t in c["tracking"]]
    path = os.path.join(EXPORT_DIR, f"{c['id']}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return f"Saved the launch checklist to {path}."


def log_campaign_results(campaign_id: str, platform: str, spend: float, impressions: int = 0, clicks: int = 0,
                         conversions: int = 0, revenue: float = 0, notes: str = "") -> str:
    c = _load_campaign(campaign_id)
    if not c:
        return f"Could not log results: no campaign with id '{campaign_id}'."
    p = _PLATFORM_ALIASES2.get(platform_key(platform).replace(" ", "_"), platform_key(platform).replace(" ", "_"))
    if p not in _LABELS:
        return f"Could not log results: unknown platform '{platform}'. Use one of: {', '.join(_LABELS)}."
    try:
        entry = {"date": datetime.now().isoformat(timespec="minutes"), "platform": p, "spend": float(spend),
                 "impressions": int(impressions), "clicks": int(clicks), "conversions": int(conversions),
                 "revenue": float(revenue), "notes": notes}
    except Exception:
        return "Could not log results: spend, impressions, clicks, conversions and revenue must be numbers."
    c["results"].append(entry)
    if c["status"] == "draft":
        c["status"] = "running"
    _save_campaign(c)
    return f"Logged {_LABELS[p]} results for {c['id']}. Say 'optimize campaign {c['id']}' for recommendations."


def _channel_metrics(results):
    agg = {}
    for r in results:
        a = agg.setdefault(r["platform"], {"spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0, "revenue": 0.0})
        for k in a:
            a[k] += r.get(k, 0)
    for a in agg.values():
        a["ctr"] = a["clicks"] / a["impressions"] if a["impressions"] else 0.0
        a["cpc"] = a["spend"] / a["clicks"] if a["clicks"] else None
        a["cvr"] = a["conversions"] / a["clicks"] if a["clicks"] else 0.0
        a["cpa"] = a["spend"] / a["conversions"] if a["conversions"] else None
        a["roas"] = a["revenue"] / a["spend"] if a["spend"] and a["revenue"] else None
        a["enough"] = a["clicks"] >= 100 or a["conversions"] >= 10
    return agg


def optimize_campaign(campaign_id: str) -> str:
    """Numbers first (computed in code), then LLM suggestions on audiences and creative."""
    c = _load_campaign(campaign_id)
    if not c:
        return f"Could not optimise: no campaign with id '{campaign_id}'."
    if not c["results"]:
        return f"Campaign {c['id']} has no results yet. Tell me spend, impressions, clicks and conversions per channel and I'll log them."
    cur, agg = c["currency"], _channel_metrics(c["results"])
    lines = [f"Results so far for {c['id']} ({c['product']}):"]
    for p, a in agg.items():
        bits = [f"spend {_money(a['spend'], cur)}", f"CTR {a['ctr'] * 100:.2f}%"]
        if a["cpc"] is not None:
            bits.append(f"CPC {_money(a['cpc'], cur)}")
        if a["cpa"] is not None:
            bits.append(f"cost/conversion {_money(a['cpa'], cur)}")
        if a["roas"] is not None:
            bits.append(f"ROAS {a['roas']:.2f}x")
        lines.append(f"  - {_LABELS[p]}: " + ", ".join(bits) + ("" if a["enough"] else "  (too little data to judge yet)"))

    ready = {p: a for p, a in agg.items() if a["enough"]}
    if any(a["roas"] for a in ready.values()):
        score, better = (lambda a: a["roas"] or 0), "higher ROAS"
    elif any(a["cpa"] for a in ready.values()):
        score, better = (lambda a: -(a["cpa"] if a["cpa"] is not None else 1e12)), "lower cost per conversion"
    else:
        score, better = (lambda a: a["ctr"]), "higher click-through"
    if len(ready) >= 2:
        order = sorted(ready, key=lambda p: score(ready[p]), reverse=True)
        best, worst = order[0], order[-1]
        planned = {r["platform"]: r["amount"] for r in c["budget_plan"]["rows"]}
        remaining = max(planned.get(worst, 0) - agg[worst]["spend"], 0)
        shift = round(remaining * 0.2, 2)
        if worst not in planned:
            tail = f"{_LABELS[worst]} wasn't budgeted in this plan; consider pausing it and putting that money on {_LABELS[best]}."
        elif shift > 0:
            tail = f"Move about {_money(shift, cur)} (20% of {_LABELS[worst]}'s remaining budget) to {_LABELS[best]}, then re-check in 2-3 days."
        else:
            tail = f"{_LABELS[worst]} has nothing left to move."
        lines.append(f"Budget: {_LABELS[best]} leads on {better}; {_LABELS[worst]} trails. " + tail)
    else:
        lines.append("Budget: not enough data on at least two channels to reallocate yet (I wait for ~100 clicks or 10 conversions each). Let it run.")

    if _hooks["llm"]:
        table = "\n".join(f"{p}: spend {a['spend']:.0f}, impressions {a['impressions']}, clicks {a['clicks']}, "
                          f"conversions {a['conversions']}, revenue {a['revenue']:.0f}" for p, a in agg.items())
        notes = "; ".join(r["notes"] for r in c["results"] if r.get("notes"))[:400]
        tip = _ask("You are a performance marketer. Be concrete and brief; don't invent data.",
                   f"Product: {c['product']}\nGoal: {c['goal']}\nAudience segments: "
                   f"{'; '.join(s['name'] + ' (' + s['who'] + ')' for s in c['audience']['segments'])}\n"
                   f"Results:\n{table}\nUser notes: {notes or 'none'}\n"
                   "Give 3 audience refinements and 3 creative tests to try next, one line each, tied to these numbers.")
        if tip:
            lines.append("\nIdeas to test next (from the language model, treat as hypotheses):\n" + tip.strip())
    return "\n".join(lines)


# ============================================================ 5. MEDIA BRIEFS ==
_MEDIA_RULES = {
    "billboard": "One image, one message, ~7 words or fewer, readable in 3-5 seconds, high contrast, a simple URL or QR/short code.",
    "radio": "A 30s spot is about 75 words (15s ≈ 35-40). One idea, say the brand and the call to action at least twice, give a memorable URL or phone number.",
    "newspaper": "Strong headline, one benefit, one offer, clear call to action; ask for sizes, placement options and the publisher's audited circulation.",
    "pr": "Lead with what's newsworthy, not what you're selling: a date, a number, a first, a local angle or a human story.",
    "influencer": "Pick creators by audience fit and engagement quality, not follower count; agree deliverables, usage rights, disclosure and a unique code or link.",
}
_VENDOR_QUERY = {"billboard": "billboard outdoor advertising companies {loc}", "radio": "radio advertising stations rates {loc}",
                 "newspaper": "newspaper advertising rates {loc}", "pr": "journalists blogs covering {product} {loc}",
                 "influencer": "influencer marketing agencies {loc}"}


def build_media_brief(medium: str, product: str, goal: str = "awareness", budget: float = 0,
                      location: str = "", audience: str = "", notes: str = "") -> str:
    """Offline/earned media: creative options, an outreach email, and (if search is on) vendors to contact."""
    medium = _PLATFORM_ALIASES2.get((medium or "").strip().lower(), (medium or "").strip().lower())
    if medium not in _MEDIA_RULES:
        return f"Could not build a brief: I handle {', '.join(_MEDIA_RULES)}. Which one?"
    if not (product or "").strip():
        return "Could not build a brief: tell me what you're promoting."
    loc = location.strip()
    sections = [f"# {_LABELS[medium]} brief: {product}", f"Goal: {goal} | Budget: {budget or 'not set'} | Market: {loc or 'not set'}",
                "", f"## Rules of thumb\n{_MEDIA_RULES[medium]}"]

    kind = {"pr": "a press release (headline, 2-sentence lede, one quote, a short boilerplate) AND a 90-word pitch email to a journalist",
            "radio": "two 30-second radio scripts (about 75 words each) with [SFX]/[VO] cues",
            "billboard": "five billboard line options (max 7 words each) with a one-line visual idea for each",
            "newspaper": "three ad concepts: headline, one-sentence body, offer and call to action",
            "influencer": "a creator brief: the key message, 3 must-include points, 3 things to avoid, and a suggested post format"}[medium]
    creative = _ask("You are an advertising copywriter. Be specific and honest; invent no statistics, awards or quotes "
                    "(mark any quote as [placeholder]).",
                    f"Product: {product}\nGoal: {goal}\nAudience: {audience or 'general local audience'}\nMarket: {loc or 'n/a'}\n"
                    f"Notes: {notes or 'none'}\nWrite {kind}.")
    sections += ["", "## Creative options", creative.strip() if creative else "(No language model available. The rules of thumb above still apply.)"]

    if _hooks["search"] and medium in _VENDOR_QUERY:
        try:
            hits = _hooks["search"](_VENDOR_QUERY[medium].format(loc=loc, product=product).strip())[:5]
            if hits:
                sections += ["", "## Possible vendors / outlets (from a web search; verify before contacting)"]
                sections += [f"- {h.get('title', '')[:80]} — {h.get('url', '')}" for h in hits]
        except Exception as e:
            sections += ["", f"(Vendor search failed: {e})"]

    if medium != "pr":
        sections += ["", "## Quote-request email (copy, fill the brackets)",
                     f"Subject: Rate card and availability: {product} campaign" + (f" ({loc})" if loc else ""),
                     "", "Hi [name],", "",
                     f"I'm planning a {medium} campaign for {product}" + (f" in {loc}" if loc else "") +
                     (f" with a budget around {budget}" if budget else "") + " and would like to book in [month/dates]. Could you send:",
                     "1. Your current rate card and any package discounts",
                     "2. Audience data (reach/impressions or circulation) and how it's measured or audited",
                     "3. Available slots or locations for [dates]",
                     "4. Production costs, file specs and creative deadlines",
                     "5. Booking and cancellation terms",
                     "6. What proof-of-performance reporting you provide", "", "Thanks,", "[Your name]"]
    sections += ["", "## Track it", "Offline media is invisible to your analytics: give this channel its own URL, QR code or promo code, "
                 "and ask new customers how they heard about you."]
    text = "\n".join(sections)
    try:
        os.makedirs(BRIEF_DIR, exist_ok=True)
        path = os.path.join(BRIEF_DIR, f"{medium}_{_slug(product, 20)}_{datetime.now():%Y%m%d_%H%M}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        text += f"\n\n(Saved to {path})"
    except OSError:
        pass
    return text


# ============================================== tool registry (merged by main) ==
def _s(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


_STR, _INT, _NUM = {"type": "string"}, {"type": "integer"}, {"type": "number"}
TOOL_SCHEMAS = [
    _s("generate_image", "Generate an image from a text prompt using the configured OpenAI image service.",
       {"prompt": {**_STR, "description": "Detailed visual description"},
        "size": {**_STR, "description": "1024x1024, 1024x1536, or 1536x1024"}}, ["prompt"]),
    _s("generate_video", "Generate a vertical video from AI-generated visual scenes, cut on the song's bars or a BPM beat grid.",
       {"description": {**_STR, "description": "Visual concept and style for the video"},
        "music_path": {**_STR, "description": "Optional local song file"},
        "music_url": {**_STR, "description": "Optional song URL to analyze and include as audio"},
        "duration": _INT, "bpm": _INT}, ["description"]),
    _s("create_clips", "Cut short-form clips (TikTok/Reels/Shorts) from a video URL or local file. Optionally sync to a song's beat.",
       {"source": {**_STR, "description": "Video URL or local file path"},
        "instruction": {**_STR, "description": "What kind of moments to pick"},
        "num_clips": _INT, "clip_seconds": _INT,
        "music_path": {**_STR, "description": "Optional local audio file to sync the cuts to"},
        "music_url": {**_STR, "description": "Optional HTTP/HTTPS song link; audio is downloaded into the output folder"},
        "mode": {**_STR, "description": "'clips' or 'montage' (one beat-cut video)"},
        "reframe": {**_STR, "description": "'crop', 'blur' or 'none'"}, "platform": _STR}, ["source"]),
    _s("generate_captions", "Write hooks, captions, hashtags and CTAs for social platforms, within each platform's limits.",
       {"content": {**_STR, "description": "What the post/video is about"},
        "platforms": {**_STR, "description": "Comma-separated, e.g. tiktok,instagram,x"},
        "tone": _STR, "goal": _STR, "cta": _STR, "audience": _STR, "variants": _INT}, ["content"]),
    _s("stage_scheduled_post", "Stage a post for a future time. The user must still say 'confirm schedule'.",
       {"platform": _STR, "content": _STR, "when": {**_STR, "description": "e.g. 'tomorrow at 5pm'"},
        "file_path": {**_STR, "description": "Video file (YouTube/TikTok)"}, "image_url": _STR, "title": _STR},
       ["platform", "content", "when"]),
    _s("list_scheduled_posts", "List scheduled and recent posts.", {}, []),
    _s("cancel_scheduled_post", "Cancel a pending scheduled post by id.", {"post_id": _STR}, ["post_id"]),
    _s("plan_ad_campaign", "Create a DRAFT ad campaign plan (budget split, audiences, ad copy, test plan). Launches nothing.",
       {"product": _STR, "goal": {**_STR, "description": "sales, awareness, traffic, leads or streams"},
        "budget": _NUM, "duration_days": _INT,
        "platforms": {**_STR, "description": "Comma-separated: google, meta, tiktok, youtube, x, linkedin, billboard, radio, newspaper, influencer, pr"},
        "audience": _STR, "location": _STR, "currency": _STR, "landing_url": _STR}, ["product", "budget"]),
    _s("list_campaigns", "List saved campaign drafts.", {}, []),
    _s("export_campaign", "Export a campaign as a launch checklist (markdown) or a Google Ads import CSV.",
       {"campaign_id": _STR, "fmt": {**_STR, "description": "'markdown' or 'google_ads_csv'"}}, ["campaign_id"]),
    _s("log_campaign_results", "Record real performance numbers for one channel of a campaign.",
       {"campaign_id": _STR, "platform": _STR, "spend": _NUM, "impressions": _INT, "clicks": _INT,
        "conversions": _INT, "revenue": _NUM, "notes": _STR}, ["campaign_id", "platform", "spend"]),
    _s("optimize_campaign", "Analyse logged results: budget shifts plus audience and creative ideas.",
       {"campaign_id": _STR}, ["campaign_id"]),
    _s("build_media_brief", "Brief for billboard, radio, newspaper, PR/media coverage or influencer marketing, with outreach draft.",
       {"medium": {**_STR, "description": "billboard, radio, newspaper, pr or influencer"}, "product": _STR,
        "goal": _STR, "budget": _NUM, "location": _STR, "audience": _STR, "notes": _STR}, ["medium", "product"]),
]

FUNCTIONS = {
    "generate_image": generate_image, "generate_video": generate_video,
    "create_clips": create_clips, "generate_captions": generate_captions,
    "stage_scheduled_post": stage_scheduled_post, "list_scheduled_posts": list_scheduled_posts,
    "cancel_scheduled_post": cancel_scheduled_post, "plan_ad_campaign": plan_ad_campaign,
    "list_campaigns": list_campaigns, "export_campaign": export_campaign,
    "log_campaign_results": log_campaign_results, "optimize_campaign": optimize_campaign,
    "build_media_brief": build_media_brief,
    # confirm_scheduled_post is deliberately NOT here: only the user's own typed/spoken
    # 'confirm schedule' may lock a schedule in, never the language model.
}

TOOL_INTENTS = {
    r"\b(?:generate|create|make)\b.*\b(?:image|picture|artwork|illustration)\b": {"generate_image"},
    r"\b(?:generate|create|make)\b.*\bvideo\b": {"generate_video", "create_clips"},
    r"\b(?:clip|clips|shorts?|reels?|highlights?|montage)\b": {"create_clips"},
    r"\b(?:caption|captions|hashtags?|hooks?)\b": {"generate_captions"},
    r"\b(?:schedule|scheduled|queue)\b.*\b(?:post|posts|upload|video|content|reel|clip|short|tweet)\b|"
    r"\b(?:post|upload|content)\b.*\b(?:tomorrow|later|scheduled?)\b":
        {"stage_scheduled_post", "list_scheduled_posts", "cancel_scheduled_post"},
    r"\b(?:ad|ads|advert\w*|campaigns?|adsets?|ad sets?|google ads|meta ads|facebook ads|tiktok ads|"
    r"target audience|marketing plan|promote|promotion)\b":
        {"plan_ad_campaign", "list_campaigns", "export_campaign", "log_campaign_results", "optimize_campaign"},
    r"\b(?:billboards?|radio|newspapers?|press release|media coverage|pr pitch|influencers?|media brief)\b":
        {"build_media_brief"},
}
