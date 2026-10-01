"""Speech and word timestamps.

Three engines behind config.TTS_ENGINE:

  "edge" (default) -- Microsoft's free cloud neural voices via `edge-tts`.
      Mature and consistently clean; the whole reason to switch to it is that
      VOICE_SAMPLE has never actually been set here, so Chatterbox's one real
      advantage (zero-shot cloning) was never in use, and its costs (rough
      takes, the long-input degradation this module used to document) were
      being paid for nothing. Runs in the light venv -- no torch, no mlx.
  "sarvam" -- Sarvam AI's Bulbul model, a paid (small free credit, then
      metered) API built specifically for Indian languages rather than a
      generic multilingual model with Telugu bolted on. Worth the cost for
      Telugu specifically: Google Cloud TTS's te-IN voices are Standard tier
      only, likely a downgrade from Edge's neural te-IN voices, so it isn't
      offered here as a third option.
  "chatterbox" -- the local Apple-Silicon-native engine, still available for
      when a cloned voice is actually wanted. A subprocess call into
      .venv-reel; this module never imports mlx directly, so the carousel's
      venv stays exactly as it is.

Word timestamps always come from mlx_whisper in .venv-reel regardless of
engine -- transcribing a finished wav is the same job either way.
"""
import asyncio
import base64
import json
import re
import subprocess
from pathlib import Path

import edge_tts
import requests

import config

# Chatterbox reads "AI" as a single syllable and runs it into the next word --
# "real world AI news" came back as "ANews". Tested four respellings and had
# Whisper report what it heard: "AI" -> "ANews.", "A.I." -> "ABI iNews.",
# "ay-eye" -> "AII", and "A. I." -> "AI News." Letter, full stop, space is the
# only form it reads as letters, so initialisms are respelled that way for the
# ENGINE ONLY. Captions keep the original text: they take their words from the
# script and only their timings from Whisper, and the normaliser strips
# punctuation, so "A. I." and "AI" still match during alignment.
_SPOKEN_COMPOUNDS = {
    "OpenAI": "Open A. I.",
    "ChatGPT": "Chat G. P. T.",
    "xAI": "X. A. I.",
    "DeepSeek": "Deep Seek",
}
# Read letter by letter. Deliberately excludes acronyms said as words (NASA,
# NATO, NVIDIA) and anything that collides with a real word in lower case.
_INITIALISMS = (
    "AGI", "AI", "API", "ASML", "AWS", "CEO", "CFO", "CTO", "CPU", "GPT", "GPU",
    "HBM", "IBM", "IPO", "LLM", "NPU", "SDK", "TPU", "TSMC", "UI", "EU", "FTC",
    "FBI", "SEC", "DOJ", "UK", "UN", "US", "ML", "AR", "VR", "OS",
)
_INITIALISM_RE = re.compile(r"\b(" + "|".join(_INITIALISMS) + r")\b")


def spoken_form(text: str) -> str:
    """Respells initialisms so the engine says the letters. Display text only
    ever uses the original."""
    for written, spoken in _SPOKEN_COMPOUNDS.items():
        text = text.replace(written, spoken)
    text = _INITIALISM_RE.sub(
        lambda m: ". ".join(m.group(1)) + ".", text)
    # A respelling that lands before punctuation can leave ".." or ". ,".
    text = re.sub(r"\.\s*([.,;:!?])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


class VoiceError(RuntimeError):
    pass


class DependencyError(VoiceError):
    """Something is not installed. Callers exit 2 rather than retrying."""


def worker_python() -> Path:
    python = config.REEL_VENV / "bin" / "python"
    if not python.exists():
        raise DependencyError(
            f"{config.REEL_VENV.name} is missing — run ./setup_reel_venv.sh first")
    return python


def _run(arguments: list, timeout: int) -> str:
    command = [str(worker_python()), str(config.ROOT / "reel_worker.py"), *arguments]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise VoiceError(
            f"reel_worker {arguments[0]} failed:\n{result.stderr.strip()[-700:]}")
    if result.stderr.strip():
        print(f"    {result.stderr.strip()[-300:]}")
    return result.stdout


def probe() -> dict:
    """What the reel venv actually exposes. Loads no models, hits no network."""
    return json.loads(_run(["probe"], timeout=180))


def generate_speech(script_text: str, out_path: Path) -> Path:
    """Writes spoken audio for `script_text`, via config.REEL_TTS_ENGINE."""
    if not script_text.strip():
        raise VoiceError("empty script")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if config.REEL_TTS_ENGINE == "edge":
        return _generate_speech_edge(script_text, out_path)
    if config.REEL_TTS_ENGINE == "sarvam":
        return _generate_speech_sarvam(script_text, out_path)
    return _generate_speech_chatterbox(script_text, out_path)


def _generate_speech_edge(script_text: str, out_path: Path) -> Path:
    """Edge TTS: a cloud neural voice, no respelling -- it already reads
    "AI" and "GPT" correctly, so Chatterbox's letter-by-letter workaround
    (spoken_form) would only make a mature engine sound worse."""
    mp3_path = out_path.with_suffix(".edge.mp3")
    try:
        asyncio.run(edge_tts.Communicate(
            script_text, config.REEL_EDGE_TTS_VOICE,
            rate=config.REEL_EDGE_TTS_RATE).save(str(mp3_path)))
    except Exception as exc:  # noqa: BLE001 -- network/service errors, all fatal here
        raise VoiceError(f"edge-tts failed: {exc}") from exc
    if not mp3_path.exists() or mp3_path.stat().st_size == 0:
        raise VoiceError(f"edge-tts produced no audio for {out_path.name}")
    result = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(mp3_path), "-c:a", "pcm_s16le", str(out_path)],
        capture_output=True, text=True, timeout=120)
    mp3_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise VoiceError(f"mp3->wav failed: {result.stderr.strip()[-300:]}")
    return _retime(out_path)


_SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
# The API caps a single request around 2500 characters; split at sentence
# boundaries (Telugu దండం included) well under that so no sentence is cut
# mid-word.
_SARVAM_MAX_CHARS = 2000


def _sarvam_chunks(text: str) -> list:
    sentences = re.split(r"(?<=[.!?।॥])\s+", text.strip())
    chunks, current = [], ""
    for sentence in sentences:
        if current and len(current) + 1 + len(sentence) > _SARVAM_MAX_CHARS:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def _generate_speech_sarvam(script_text: str, out_path: Path) -> Path:
    """Sarvam AI (Bulbul): chunks long scripts at sentence boundaries to stay
    under the API's per-request character cap, then concatenates the chunk
    wavs with ffmpeg before handing off to _retime."""
    if not config.REEL_SARVAM_API_KEY:
        raise DependencyError("SARVAM_API_KEY is not set in .env")
    chunk_paths = []
    try:
        for index, chunk in enumerate(_sarvam_chunks(script_text)):
            payload = {
                "text": chunk,
                "language_code": config.REEL_SARVAM_TTS_LANGUAGE,
                "model": config.REEL_SARVAM_TTS_MODEL,
                "pace": config.REEL_SARVAM_TTS_PACE,
                "speech_sample_rate": 24000,
                "output_audio_codec": "wav",
            }
            if config.REEL_SARVAM_TTS_SPEAKER:
                payload["speaker"] = config.REEL_SARVAM_TTS_SPEAKER
            try:
                response = requests.post(
                    _SARVAM_TTS_URL,
                    headers={"api-subscription-key": config.REEL_SARVAM_API_KEY},
                    json=payload, timeout=120)
                response.raise_for_status()
            except requests.RequestException as exc:
                raise VoiceError(f"sarvam TTS request failed: {exc}") from exc
            audios = response.json().get("audios") or []
            if not audios:
                raise VoiceError(f"sarvam TTS returned no audio for chunk {index}")
            chunk_path = out_path.with_suffix(f".sarvam{index}.wav")
            chunk_path.write_bytes(base64.b64decode(audios[0]))
            chunk_paths.append(chunk_path)

        if len(chunk_paths) == 1:
            chunk_paths[0].replace(out_path)
        else:
            concat_inputs = []
            for chunk_path in chunk_paths:
                concat_inputs += ["-i", str(chunk_path)]
            filter_inputs = "".join(f"[{i}:a]" for i in range(len(chunk_paths)))
            result = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *concat_inputs,
                 "-filter_complex",
                 f"{filter_inputs}concat=n={len(chunk_paths)}:v=0:a=1",
                 "-c:a", "pcm_s16le", str(out_path)],
                capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                raise VoiceError(
                    f"sarvam chunk concat failed: {result.stderr.strip()[-300:]}")
    finally:
        for chunk_path in chunk_paths:
            chunk_path.unlink(missing_ok=True)
    return _retime(out_path)


def _generate_speech_chatterbox(script_text: str, out_path: Path) -> Path:
    """Writes spoken audio for `script_text`. No network once cached."""
    # The engine gets the respelled text; everything downstream keeps the original.
    script_text = spoken_form(script_text)
    prefix = out_path.with_suffix("")

    reference = ""
    if config.REEL_VOICE_SAMPLE:
        reference = str(Path(config.REEL_VOICE_SAMPLE).expanduser())
        if not Path(reference).exists():
            raise DependencyError(
                f"REEL_VOICE_SAMPLE points at a missing file: {reference}")

    _run(["tts", "--text", script_text, "--out-prefix", str(prefix),
          "--model", config.REEL_TTS_MODEL, "--ref-audio", reference,
          "--exaggeration", str(config.REEL_EXAGGERATION),
          "--cfg-weight", str(config.REEL_CFG_WEIGHT),
          "--temperature", str(config.REEL_TEMPERATURE)],
         timeout=1800)

    # mlx_audio appends its own suffix conventions; accept whichever it wrote.
    if out_path.exists():
        return _retime(out_path)
    for candidate in sorted(prefix.parent.glob(f"{prefix.name}*.wav")):
        if candidate != out_path:
            candidate.rename(out_path)
        return _retime(out_path)
    raise VoiceError(f"no audio produced for {out_path.name}")


def _retime(path: Path) -> Path:
    """Slows the rendered speech to REEL_SPEECH_SPEED, in place.

    Chatterbox accepts a `speed` argument and then ignores it -- its own source
    says "Ignored (Chatterbox doesn't support speed adjustment)" -- so the rate
    change has to happen on the audio. atempo preserves pitch. This runs before
    transcription on purpose: Whisper then times the words against the audio
    that actually ships, so captions cannot drift.
    """
    speed = config.REEL_SPEECH_SPEED
    if abs(speed - 1.0) < 0.01:
        return path
    retimed = path.with_name(f"{path.stem}.retimed.wav")
    result = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-filter:a", f"atempo={speed}", "-c:a", "pcm_s16le", str(retimed)],
        capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise VoiceError(f"atempo failed: {result.stderr.strip()[-300:]}")
    retimed.replace(path)
    return path


def transcribe_words(audio_path: Path, out_json: Path) -> list:
    """Word-level timestamps, for caption timing. Empty list is survivable."""
    args = ["asr", "--audio", str(audio_path), "--out", str(out_json),
           "--model", config.REEL_ASR_MODEL, "--backend", config.REEL_ASR_BACKEND]
    if config.REEL_ASR_LANGUAGE:
        args += ["--language", config.REEL_ASR_LANGUAGE]
    _run(args, timeout=1800)
    if not out_json.exists():
        return []
    return json.loads(out_json.read_text()).get("words") or []
