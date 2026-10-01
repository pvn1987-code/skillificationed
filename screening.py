"""Rejects B-roll containing identifiable people, and verifies subject content.

The slug screen in stock.py reads the page text, which describes a clip's
subject rather than who is standing in shot -- "chalkboard equations" returned
a close-up of a teacher's face and the slug said nothing about a person. Only
the pixels settle it, so this samples frames and looks for faces.

Detection runs in .venv-reel via reel_worker, so opencv never enters the venv
the carousel depends on. Results are cached by filename: decoding a clip twice
across rebuilds is pure waste.

verify_subjects() is the same idea aimed at a different failure: the slug gate
proves a clip's PAGE DESCRIPTION carries the query's words, never that the
frame shows the subject. See config.VISION_CHECK for the failure this caught.
"""
import json
import subprocess

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

import config

_CACHE = config.ROOT / "state" / "face_screen.json"
_VISION_CACHE = config.ROOT / "state" / "vision_screen.json"


class VisionError(RuntimeError):
    pass


class VisionQuotaExhausted(VisionError):
    pass


def _load() -> dict:
    if _CACHE.exists():
        try:
            return json.loads(_CACHE.read_text())
        except (OSError, ValueError):
            return {}
    return {}


def _save(cache: dict) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(cache, indent=2))
    except OSError:
        pass  # A cache that cannot be written is a slowdown, not a failure.


def has_faces(path) -> bool:
    """True when the clip shows a face in enough sampled frames to matter.

    Fails OPEN: if the model or the reel venv is missing, the clip is allowed
    rather than the build dying. The slug screen still applies, and a missing
    face model is a setup problem to report, not a reason to produce no reel.
    """
    if not config.REEL_REJECT_FACES:
        return False
    python = config.REEL_VENV / "bin" / "python"
    if not (python.exists() and config.REEL_FACE_MODEL.exists()):
        return False

    cache = _load()
    key = str(path.name if hasattr(path, "name") else path)
    if key in cache:
        return cache[key] >= config.REEL_FACE_MIN_HITS

    try:
        result = subprocess.run(
            [str(python), str(config.ROOT / "reel_worker.py"), "faces",
             "--video", str(path), "--model", str(config.REEL_FACE_MODEL),
             "--samples", str(config.REEL_FACE_SAMPLES)],
            capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return False
        hits = int(json.loads(result.stdout)["with_faces"])
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return False

    cache[key] = hits
    _save(cache)
    return hits >= config.REEL_FACE_MIN_HITS


def _load_vision() -> dict:
    if _VISION_CACHE.exists():
        try:
            return json.loads(_VISION_CACHE.read_text())
        except (OSError, ValueError):
            return {}
    return {}


def _save_vision(cache: dict) -> None:
    try:
        _VISION_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _VISION_CACHE.write_text(json.dumps(cache, indent=2))
    except OSError:
        pass


def _extract_frame(path) -> bytes:
    """One representative frame (ffmpeg's own `thumbnail` pick, not just
    frame zero, which is disproportionately a fade-in or a title card)."""
    result = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vf", "thumbnail,scale=512:-1", "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
        capture_output=True, timeout=30)
    if result.returncode != 0 or not result.stdout:
        raise VisionError(f"frame extraction failed for {path}")
    return result.stdout


_VISION_SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {"type": "boolean"}}},
    "required": ["verdicts"],
}


def _ask_vision(subject: str, paths: list) -> list:
    """One Gemini call, N frames in, N booleans out, same order.

    Same client/retry shape as planner._ask -- a ClientError whose message
    names RESOURCE_EXHAUSTED/429 is the free tier's daily cap, not a bug.
    """
    client = genai.Client(api_key=config.GEMINI_API_KEY)
    parts = [types.Part.from_text(text=(
        f"Each image below is a candidate video frame, in order. For EACH "
        f"one, answer whether it clearly, visibly shows: {subject!r}. "
        f"Return exactly {len(paths)} booleans in `verdicts`, same order."))]
    for path in paths:
        parts.append(types.Part.from_bytes(data=_extract_frame(path),
                                           mime_type="image/jpeg"))
    try:
        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=types.Content(parts=parts),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_VISION_SCHEMA, temperature=0.0),
        )
    except genai_errors.ClientError as exc:
        message = str(exc)
        if "RESOURCE_EXHAUSTED" in message or "429" in message:
            raise VisionQuotaExhausted(str(exc)) from exc
        raise VisionError(str(exc)) from exc
    except genai_errors.ServerError as exc:
        raise VisionError(str(exc)) from exc
    verdicts = (json.loads(response.text) or {}).get("verdicts") or []
    if len(verdicts) != len(paths):
        return [True] * len(paths)      # malformed reply: fail open, not shut
    return [bool(v) for v in verdicts]


def verify_subjects(subject: str, paths: list) -> list:
    """True/False per path: does that clip's frame actually show `subject`?

    Fails OPEN on a missing key or an exhausted daily quota -- a slug match
    that cannot be double-checked right now is still better than a build that
    produces nothing. Cached by filename + subject, since the same clip can be
    asked about under a different query in a different reel.
    """
    if not config.VISION_CHECK or not paths:
        return [True] * len(paths)
    if not config.GEMINI_API_KEY:
        return [True] * len(paths)

    cache = _load_vision()
    verdicts: list = [None] * len(paths)
    to_check = []
    for index, path in enumerate(paths):
        key = f"{path.name if hasattr(path, 'name') else path}|{subject}"
        if key in cache:
            verdicts[index] = cache[key]
        else:
            to_check.append((index, key, path))

    if to_check:
        try:
            fresh = _ask_vision(subject, [p for _, _, p in to_check])
        except VisionQuotaExhausted as exc:
            print(f"      ! vision check quota exhausted, allowing unchecked: {exc}")
            fresh = [True] * len(to_check)
        except VisionError as exc:
            print(f"      ! vision check failed, allowing unchecked: {exc}")
            fresh = [True] * len(to_check)
        for (index, key, _path), verdict in zip(to_check, fresh):
            verdicts[index] = verdict
            cache[key] = verdict
        _save_vision(cache)

    return verdicts
