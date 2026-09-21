"""Rejects B-roll containing identifiable people.

The slug screen in pexels.py reads the page text, which describes a clip's
subject rather than who is standing in shot -- "chalkboard equations" returned
a close-up of a teacher's face and the slug said nothing about a person. Only
the pixels settle it, so this samples frames and looks for faces.

Detection runs in .venv-reel via reel_worker, so opencv never enters the venv
the carousel depends on. Results are cached by filename: decoding a clip twice
across rebuilds is pure waste.
"""
import json
import subprocess

import config

_CACHE = config.ROOT / "state" / "face_screen.json"


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
