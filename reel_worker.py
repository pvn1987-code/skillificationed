#!/usr/bin/env python3
"""Runs INSIDE .venv-reel. Never imported by the carousel pipeline.

Isolated on purpose: mlx, transformers and friends stay out of the venv the
carousel depends on, so installing or breaking them cannot affect the twice-
daily publish. voiceclone.py talks to this over subprocess + JSON.

Two jobs, both local and offline once the models are cached:
  tts    Chatterbox speech, cloned from a reference clip when one is configured
  asr    Whisper word-level timestamps, for the burned-in captions
  probe  report which entry points this install actually exposes

mlx_audio's kwarg names have moved between releases, so rather than hard-code a
signature this filters kwargs against the real one via inspect. If an entry
point disappears entirely, `probe` says so instead of failing cryptically.
"""
import argparse
import importlib
import inspect
import json
import sys
from pathlib import Path

# Tried in order; the first that imports wins.
_TTS_ENTRIES = [
    ("mlx_audio.tts.generate", "generate_audio"),
    ("mlx_audio.tts", "generate_audio"),
]
# Verified against mlx-audio 0.5.3 via `probe`: this is the only public
# transcription entry point, and it filters kwargs against the model's own
# generate() signature. Whisper's accepts word_timestamps.
_ASR_ENTRIES = [
    ("mlx_audio.stt.generate", "generate_transcription"),
]


def _resolve(entries: list):
    """First importable (module, attr) pair from a candidate list."""
    tried = []
    for module_name, attr in entries:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            tried.append(f"{module_name}: {exc}")
            continue
        function = getattr(module, attr, None)
        if function is not None:
            return function
        tried.append(f"{module_name}.{attr}: missing")
    raise SystemExit("No usable entry point.\n  " + "\n  ".join(tried))


def _supported(function, **kwargs) -> dict:
    """Drop kwargs this build does not accept, keeping the call forward safe."""
    try:
        allowed = set(inspect.signature(function).parameters)
    except (TypeError, ValueError):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in allowed}


def do_tts(args) -> None:
    function = _resolve(_TTS_ENTRIES)
    kwargs = {
        "text": args.text,
        "model": args.model,
        "file_prefix": args.out_prefix,
        "audio_format": "wav",
        "join_audio": True,
        "verbose": False,
        # Chatterbox's expressiveness knobs. Verified present on its generate();
        # _supported() drops them harmlessly on any model that lacks them.
        "exaggeration": args.exaggeration,
        "cfg_weight": args.cfg_weight,
        "temperature": args.temperature,
    }
    if args.ref_audio:
        # ref_audio is the cloning input. Deliberately NOT 'voice': that names
        # a built-in preset, so passing a file path there would be wrong.
        kwargs["ref_audio"] = args.ref_audio
    # Verified against 0.5.3: with join_audio=True and stream=False the joined
    # file is written to exactly "<file_prefix>.wav", which is the contract
    # voiceclone.generate_speech relies on. `save` is NOT needed here -- it
    # only gates saving of *streamed* audio (the CLI rejects it without
    # --stream), so adding it would be noise at best.
    function(**_supported(function, **kwargs))


def _words_from(result) -> list:
    """Flatten whatever shape the backend returns into [{word,start,end}].

    Word timestamps live under segments[].words in every shape seen so far,
    but some builds return a top-level 'words' instead.
    """
    import dataclasses
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        result = dataclasses.asdict(result)
    elif hasattr(result, "to_dict"):
        result = result.to_dict()
    elif not isinstance(result, dict):
        result = getattr(result, "__dict__", {}) or {}

    words = []
    for entry in result.get("words") or []:
        words.append(entry)
    for segment in result.get("segments") or []:
        segment = segment if isinstance(segment, dict) else getattr(segment, "__dict__", {})
        for entry in segment.get("words") or []:
            words.append(entry if isinstance(entry, dict)
                         else getattr(entry, "__dict__", {}))

    cleaned = []
    for entry in words:
        text = str(entry.get("word", entry.get("text", ""))).strip()
        start, end = entry.get("start"), entry.get("end")
        if text and start is not None and end is not None:
            cleaned.append({"word": text, "start": float(start), "end": float(end)})
    return cleaned


def do_asr(args) -> None:
    # Without a language hint, Whisper auto-detects from the audio -- and on a
    # short (7-9s) non-English clip it guesses wrong often enough to matter.
    # A Telugu beat came back transcribed as "lho munchi kovulu... ಈવी
    # રक्तल् lho chakker..." -- Kannada and Gujarati
    # characters mixed into a phonetic Latin guess, sharing not one real word
    # with the actual script apart from a bare digit. retime_script's alignment
    # then had exactly one anchor (that digit) to work with, and everything
    # after it collapsed into a compressed pile instead of tracking the voice.
    if args.backend == "mlx_whisper":
        import mlx_whisper
        kwargs = {"path_or_hf_repo": args.model, "word_timestamps": True}
        if args.language:
            kwargs["language"] = args.language
        result = mlx_whisper.transcribe(args.audio, **kwargs)
    else:
        function = _resolve(_ASR_ENTRIES)
        # word_timestamps rides through **kwargs to Whisper's generate().
        kwargs = {"model": args.model, "audio": args.audio,
                  "output_path": args.out.removesuffix(".json"),
                  "format": "json", "verbose": False, "word_timestamps": True}
        if args.language:
            kwargs["language"] = args.language
        result = function(**_supported(function, **kwargs))

    words = _words_from(result)
    text = ""
    if isinstance(result, dict):
        text = str(result.get("text") or "")
    payload = {"text": text.strip(), "words": words}
    with open(args.out, "w") as handle:
        json.dump(payload, handle, indent=2)
    if not words:
        # Not fatal: videocomposite.py falls back to even spacing.
        print("  ! no word timestamps returned; captions will be evenly spaced",
              file=sys.stderr)


def do_faces(args) -> None:
    """Counts sampled frames containing a face.

    Slug screening cannot do this job: the page text describes the subject, not
    who is standing in shot, so "chalkboard equations" returned a close-up of a
    teacher's face with nothing in the slug to catch it. Identifiable people on
    an automated feed beside a named company imply an endorsement that does not
    exist, so the pixels have to be checked.

    YuNet rather than Haar cascades: OpenCV 5 dropped CascadeClassifier
    altogether, and YuNet is both more accurate and a 230KB model.
    """
    import cv2

    model = Path(args.model)
    if not model.exists():
        raise SystemExit(f"face model missing: {model}")
    detector = cv2.FaceDetectorYN_create(str(model), "", (320, 320), 0.7, 0.3, 5000)

    capture = cv2.VideoCapture(args.video)
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    checked = hits = 0
    for step in range(args.samples):
        if total > 0:
            capture.set(cv2.CAP_PROP_POS_FRAMES,
                        int(total * (step + 0.5) / args.samples))
        ok, frame = capture.read()
        if not ok:
            break
        checked += 1
        # Fixed detector input, so a face has to be reasonably large in frame
        # to register -- one too small to survive the downscale is too small
        # to identify anyone by.
        height, width = frame.shape[:2]
        scale = 320 / max(width, height)
        small = cv2.resize(frame, (int(width * scale), int(height * scale)))
        detector.setInputSize((small.shape[1], small.shape[0]))
        _, faces = detector.detect(small)
        if faces is not None and len(faces):
            hits += 1
    capture.release()
    print(json.dumps({"checked": checked, "with_faces": hits}))


def do_probe(_args) -> None:
    report = {"python": sys.version.split()[0]}
    for label, entries in (("tts", _TTS_ENTRIES), ("asr", _ASR_ENTRIES)):
        try:
            function = _resolve(entries)
            report[label] = {
                "entry": f"{function.__module__}.{function.__name__}",
                "params": sorted(inspect.signature(function).parameters),
            }
        except SystemExit as exc:
            report[label] = {"error": str(exc)}
    try:
        import mlx_whisper  # noqa: F401
        report["mlx_whisper"] = "available"
    except ImportError:
        report["mlx_whisper"] = "not installed (fine, mlx_audio is the default)"
    print(json.dumps(report, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    tts = sub.add_parser("tts")
    tts.add_argument("--text", required=True)
    tts.add_argument("--out-prefix", required=True)
    tts.add_argument("--model", required=True)
    tts.add_argument("--ref-audio", default="")
    tts.add_argument("--exaggeration", type=float, default=0.5)
    tts.add_argument("--cfg-weight", type=float, default=0.5)
    tts.add_argument("--temperature", type=float, default=0.8)
    tts.set_defaults(func=do_tts)

    asr = sub.add_parser("asr")
    asr.add_argument("--audio", required=True)
    asr.add_argument("--out", required=True)
    asr.add_argument("--model", required=True)
    asr.add_argument("--backend", default="mlx_audio")
    asr.add_argument("--language", default="",
                     help="ISO 639-1 hint (e.g. 'te'); empty lets Whisper guess")
    asr.set_defaults(func=do_asr)

    faces = sub.add_parser("faces")
    faces.add_argument("--video", required=True)
    faces.add_argument("--samples", type=int, default=6)
    faces.add_argument("--model", required=True)
    faces.set_defaults(func=do_faces)

    sub.add_parser("probe").set_defaults(func=do_probe)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
