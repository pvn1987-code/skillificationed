"""Speech, one beat at a time.

The first version sent the whole script to Chatterbox as a single utterance.
At 134 words it fell apart, and the failure was invisible until the audio was
transcribed back:

    Number 5, Sydney. Opera House Roof uses over 1 million tiles.
    Number 4, Istanbul is stable.                     <- line truncated to nonsense
    And number 5, Sydney. Opera House Roof uses ...   <- ENTIRE ITEM SAID TWICE
    ...
    Number 1, Vanessa.                                <- "Venice"
    Sinks at the one person you take to number 1. Vanessa sits at number 1.
    One Vanessa sits at 1 to 2 millimeters annually.  <- looping at the tail

"Sydney" appears at 23.14s and again at 28.02s: genuinely duplicated audio, not
a transcription artefact. The news pipeline this was forked from only ever fed
it 72-86 words, so the degradation never showed there.

Synthesising each beat separately fixes it, and fixes more than it set out to:

* Short utterances do not repeat, truncate or loop.
* A beat's duration is then known EXACTLY, so beat spans are exact rather than
  inferred from Whisper's word timings. That removes the drift and the uneven
  item lengths in one move -- no alignment step can misallocate what it is
  never asked to allocate.
* A bad generation is cheap to detect and cheap to redo: one beat, not 46
  seconds of narration.
* Word timings come from transcribing one short clip, where Whisper is far
  more accurate than it is across a minute of continuous speech.
"""
import hashlib
import json
import re
import subprocess
import unicodedata
from pathlib import Path

import config
import videocomposite
import voiceclone

# Chatterbox read at roughly 3 words a second; Edge's Christopher voice, at
# the natural pace this project wants (no rushed narration -- trim the script
# before speeding up the read), measured 1.7-3.1 wps across the flat-tire
# beats, clustered around 2.3-2.5. 3.0 flagged a completely clean, unrepeated
# take (b01: 11 words in 6.6s) as "questionable" purely for reading slower
# than Chatterbox did -- the audio was fine, the baseline was wrong. A
# generation far outside THIS band has gone wrong, most often by saying
# something twice, which lands near double.
#
# This is its own table, separate from config.WORDS_PER_SECOND: that one is a
# coarse pre-flight estimate, this one is calibrated from actually measured
# per-take audio and used to decide whether a take should be REGENERATED, so
# it is worth keeping accurate per language rather than sharing a number
# tuned for a different purpose. Telugu's 1.48 is the mean of 7 real beats
# from the first Telugu build that used a language-aware Whisper pass
# (1.34-1.60 wps); an unlisted language keeps Edge Christopher's English rate.
_WORDS_PER_SECOND_BY_LANGUAGE = {"te": 1.48}
_WORDS_PER_SECOND = _WORDS_PER_SECOND_BY_LANGUAGE.get(config.ASR_LANGUAGE, 2.2)
_MIN_RATIO, _MAX_RATIO = 0.5, 1.7
# A breath between beats. It also gives each rank card a clean frame to land on
# instead of appearing mid-syllable.
_GAP = float(getattr(config, "BEAT_GAP_SECONDS", 0.15))


class NarrationError(RuntimeError):
    pass


def _normalise(text: str) -> list:
    """Words for the repeated-phrase check, any script.

    \\W+ splits on RUNS of non-word characters -- fine for Latin text, but
    Telugu's vowel signs are category Mn (not \\w), so a word like
    "శాంటోరిని" fragmented into single-consonant pieces at every combining
    mark, breaking the whole check for any non-Latin script. Splitting on
    whitespace and stripping only punctuation (by Unicode category) keeps a
    word's combining marks attached to it.
    """
    words = []
    for token in text.lower().split():
        cleaned = "".join(ch for ch in token
                          if not unicodedata.category(ch).startswith("P"))
        if cleaned:
            words.append(cleaned)
    return words


def _repeats(script: str, heard: list, size: int = 4) -> str:
    """A phrase from the script that the audio says more than once, or ''.

    The duration check alone misses a short repeat inside a long beat, and this
    is the failure that matters most: an item announced twice is the thing a
    viewer notices immediately.
    """
    spoken = _normalise(" ".join(str(w.get("word", "")) for w in heard))
    if len(spoken) < size * 2:
        return ""
    seen, wanted = {}, set()
    for words in (_normalise(script),):
        for index in range(len(words) - size + 1):
            wanted.add(tuple(words[index:index + size]))
    for index in range(len(spoken) - size + 1):
        gram = tuple(spoken[index:index + size])
        if gram not in wanted:
            continue
        if gram in seen and index - seen[gram] >= size:
            return " ".join(gram)
        seen.setdefault(gram, index)
    return ""


def _take_key(line: str) -> str:
    """Identity of a take: its words and the voice settings that shaped it.

    Must include EVERY setting either engine reads. Missing REEL_TTS_ENGINE
    here would mean switching engines does not change the key, so a rebuild
    after moving from Chatterbox to Edge would silently reuse the old
    Chatterbox wav under "(reusing voiced take)" instead of actually
    re-synthesising -- exactly the kind of stale-cache bug this key exists
    to prevent, just aimed at the wrong axis.
    """
    raw = "|".join(str(x) for x in (
        line, config.REEL_TTS_ENGINE,
        config.REEL_EDGE_TTS_VOICE, config.REEL_EDGE_TTS_RATE,
        config.REEL_TTS_MODEL, config.REEL_VOICE_SAMPLE,
        config.REEL_EXAGGERATION, config.REEL_CFG_WEIGHT,
        config.REEL_TEMPERATURE, config.REEL_SPEECH_SPEED))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _synthesise_one(line: str, wav: Path, log) -> tuple:
    """One beat's audio plus its word timings, with a retry on a bad take.

    A good take is kept on disk and reused, so rebuilding after a footage or
    caption change re-renders the video without re-voicing a word. Only beats
    whose text or voice settings actually changed are spoken again.
    """
    stamp = wav.with_suffix(".take.json")
    key = _take_key(line)
    words_file = wav.with_suffix(".words.json")
    if wav.exists() and words_file.exists() and stamp.exists():
        try:
            if json.loads(stamp.read_text()).get("key") == key:
                # transcribe_words writes {"words": [...]} but RETURNS the
                # list, so the reuse path has to unwrap it. Iterating the dict
                # yields its keys, and the first cache hit died on
                # `"words"["word"]` -- a bug only a rebuild could reach.
                cached = json.loads(words_file.read_text())
                heard = (cached.get("words") if isinstance(cached, dict)
                         else cached) or []
                log(f"      (reusing voiced take)")
                return wav, videocomposite.probe_duration(wav), heard
        except (OSError, ValueError):
            pass
    expected = max(len(line.split()) / _WORDS_PER_SECOND, 0.6)
    last_problem = ""
    for attempt in (1, 2):
        voiceclone.generate_speech(line, wav)
        duration = videocomposite.probe_duration(wav)
        heard = voiceclone.transcribe_words(wav, words_file)

        ratio = duration / expected
        problem = ""
        if ratio > _MAX_RATIO:
            problem = f"{duration:.1f}s for ~{expected:.1f}s of text"
        elif ratio < _MIN_RATIO:
            problem = f"only {duration:.1f}s — the line was probably cut short"
        else:
            repeated = _repeats(line, heard)
            if repeated:
                problem = f'said "{repeated}" twice'

        if not problem:
            try:
                stamp.write_text(json.dumps({"key": key, "line": line,
                                             "seconds": round(duration, 2)}))
            except OSError:
                pass  # A cache that will not write is a slowdown, not a failure.
            return wav, duration, heard
        last_problem = problem
        if attempt == 1:
            log(f"      ! bad take ({problem}) — regenerating")
    # Autonomy over perfection: a second bad take still ships, but it is on the
    # record in the log and in the manifest rather than silently shipped.
    log(f"      ! kept a questionable take ({last_problem})")
    return wav, videocomposite.probe_duration(wav), heard


def _concat(parts: list, out_path: Path, gaps) -> Path:
    """Join the beat wavs, with per-beat silence between them.

    `gaps` is one figure per part (the last is ignored), NOT a single value.
    It used to be a single uniform gap, while the caption cursor advanced by a
    per-beat gap that had been widened to honour a spec's `duration_sec`
    minimum -- so the padding existed in the caption timeline and nowhere in
    the audio. The flat-tire spec asked for 38s of minimums against 28s of
    speech, and the captions ended up 8 SECONDS behind the voice by the last
    beat, drifting further with every beat. Nothing caught it because each
    beat was individually correct; only the accumulated offset was wrong.
    """
    if isinstance(gaps, (int, float)):
        gaps = [float(gaps)] * len(parts)
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for wav, _duration in parts:
        command += ["-i", str(wav)]
    chains, labels = [], []
    for index, (_wav, _duration) in enumerate(parts):
        last = index == len(parts) - 1
        this_gap = gaps[index] if index < len(gaps) else 0.0
        pad = "" if last else f",apad=pad_dur={max(0.0, this_gap):.3f}"
        # A uniform rate first: the beats all come from one model, but concat
        # refuses to join streams that disagree about format.
        chains.append(f"[{index}:a]aresample=24000,aformat="
                      f"sample_fmts=s16:channel_layouts=mono{pad}[a{index}]")
        labels.append(f"[a{index}]")
    chains.append(f"{''.join(labels)}concat=n={len(parts)}:v=0:a=1[out]")
    command += ["-filter_complex", ";".join(chains), "-map", "[out]",
                "-c:a", "pcm_s16le", str(out_path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise NarrationError(f"could not join beat audio: "
                             f"{result.stderr.strip()[-400:]}")
    return out_path


def narrate(beats: list, day_dir: Path, log, tag: str = "reel") -> dict:
    """Voice every beat, join them, and report exactly where each one sits.

    Returns {audio, duration, spans, words, takes}. `spans` are contiguous and
    exact -- they are measured from the audio that shipped, not inferred.
    """
    parts_dir = day_dir / f"{tag}_beats"
    parts_dir.mkdir(parents=True, exist_ok=True)

    parts, spans, words, takes, gaps = [], [], [], [], []
    cursor = 0.0
    for index, beat in enumerate(beats):
        line = beat["line"].strip()
        if not line:
            continue
        wav = parts_dir / f"b{index:02d}.wav"
        wav, duration, heard = _synthesise_one(line, wav, log)

        # Captions carry the SCRIPT's words with Whisper's timings, never
        # Whisper's transcription -- it turned Venice into "Vanessa" and
        # Marrakesh into "Marach, Ismael", and those would have been burned
        # into the frame. `duration` (this beat's own measured wav length)
        # anchors the last word to the clip's real end, so a script whose
        # tail only near-misses what Whisper heard doesn't collapse into a
        # fraction of a second -- see retime_script's docstring.
        timed = videocomposite.retime_script(line, heard, duration)
        for word in timed:
            words.append({**word,
                          "start": float(word["start"]) + cursor,
                          "end": float(word["end"]) + cursor})

        # A spec may ask for a minimum length. Speech cannot be cut to fit
        # without clipping a word, so a short beat is padded up to the figure
        # and a long one simply runs long -- reported, never truncated.
        floor = float(beat.get("min_seconds") or 0)
        gap = 0.0 if index == len(beats) - 1 else _GAP
        if floor:
            if duration + gap < floor:
                gap = floor - duration
            elif duration > floor + 0.25:
                log(f"      (asked {floor:.1f}s, the line takes {duration:.1f}s)")
        gaps.append(gap)
        spans.append((round(cursor, 3), round(cursor + duration + gap, 3)))
        label = f"#{beat['rank']}" if beat.get("rank") else beat.get("role", "")
        log(f"      {label:6} {duration:5.2f}s  {len(timed):>3} words")
        parts.append((wav, duration))
        takes.append({"beat": label, "seconds": round(duration, 2),
                      "words": len(line.split())})
        cursor += duration + gap

    if not parts:
        raise NarrationError("no beats had anything to say")

    audio = _concat(parts, day_dir / f"{tag}.wav", gaps)
    total = videocomposite.probe_duration(audio)
    # The measured total can differ from the running cursor by a frame or two
    # of encoder padding; trust the file and give the remainder to the last
    # beat so the spans still tile it exactly.
    if spans and abs(total - cursor) > 0.02:
        spans[-1] = (spans[-1][0], round(total, 3))
    (day_dir / f"{tag}.words.json").write_text(json.dumps(words, indent=1))
    return {"audio": audio, "duration": total, "spans": spans,
            "words": words, "takes": takes}
