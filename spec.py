"""Ingests a hand-written reel spec, so no model is involved in the script.

The shape is the one you supply:

    {"intro": {"on_screen_text", "voiceover", "pexels_query", "duration_sec"},
     "items": [{"rank", "place_name", "on_screen_text", "voiceover",
                "pexels_query", "duration_sec"}],
     "outro": {"on_screen_text", "voiceover", "pexels_query", "duration_sec"}}

Three things it is worth knowing this does to your file:

1. `voiceover` is spoken VERBATIM. Your lines already say "Number 10.", so the
   pipeline must not announce the number again -- and because your text is the
   authority, nothing is rewritten to fit a word budget.

2. `on_screen_text` like "10. Salar de Uyuni, Bolivia" has its leading rank
   stripped for the card, because the card already draws a large "NO. 10"
   above the name. Your text is still what appears; only the duplicate numeral
   goes.

3. `duration_sec` is treated as a MINIMUM, not a cut. Speech cannot be
   truncated to fit without clipping a word, so a beat runs for as long as its
   line takes to say; if that is shorter than your figure, it is padded to it.
   The build reports where the two disagreed.
"""
import json
import re
from pathlib import Path


class SpecError(RuntimeError):
    pass


# "10. Santorini, Greece" / "3 - Great Barrier Reef" -> the name alone.
_LEADING_RANK = re.compile(r"^\s*\d+\s*[.)\-:]\s*")


def _kind_of(name: str) -> str:
    """A cheap hint for entity disambiguation; Wikidata does the real work."""
    lowered = name.lower()
    if any(word in lowered for word in
           ("national park", "falls", "reef", "mountain", "lake", "pyramid",
            "temple", "ruins", "canyon", "glacier", "volcano", "coast")):
        return "landmark"
    return "place" if "," in name else "other"


_RANK_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
               7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
               12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen"}


def _ensure_rank_spoken(line: str, rank: int) -> str:
    """Guarantee the spoken line announces the rank the card will draw.

    A verbatim spec takes the number out of the pipeline's hands, which is fine
    when a person wrote it and checked it. A generated one gets the same
    treatment, so this is the backstop: if the voiceover does not actually say
    the number, it is prefixed. The burned-in numeral and the voice can never
    disagree, however the script was produced.
    """
    lowered = line.lower()
    word = _RANK_WORDS.get(rank, str(rank))
    if re.search(rf"\b(number\s+)?({rank}|{word})\b", lowered):
        return line
    return f"Number {word}. {line}"


def load(path: Path) -> dict:
    """One spec FILE to the plan structure the builder already understands."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise SpecError(f"could not read {path}: {exc}") from exc
    return load_dict(raw, source=f"hand-written spec ({Path(path).name}); "
                                 "facts not model-checked")


def load_dict(raw: dict, source: str = "spec") -> dict:
    """One spec, from a file or from the planner, to the builder's structure.

    Both paths go through here on purpose: the planner emits this same shape, so
    a generated reel and a hand-written one are rendered by identical code and
    cannot drift apart.
    """
    entries = raw.get("items") or []
    if not entries:
        raise SpecError("the spec has no items")

    # "countdown" (the default) or "howto". A countdown counts DOWN to a
    # reveal; a tutorial counts UP through a sequence, and the three things
    # that follow from that are all wrong if the format is assumed:
    # the order, the card label, and whether the number is spoken as a rank.
    fmt = (raw.get("format") or "countdown").strip().lower()
    if fmt not in ("countdown", "howto"):
        raise SpecError(f"format must be 'countdown' or 'howto', not {fmt!r}")
    howto = fmt == "howto"

    items = []
    for entry in entries:
        name = (entry.get("place_name") or entry.get("name") or "").strip()
        if not name:
            raise SpecError(f"item {entry.get('rank')} has no place_name")
        # Keep the line break: the second line is usually the payload
        # ("Median Income: ~$143k") and the card renders it beneath the name.
        raw_caption = (entry.get("on_screen_text") or name).strip()
        caption = "\n".join(
            _LEADING_RANK.sub("", part.strip()) if index == 0 else part.strip()
            for index, part in enumerate(raw_caption.split("\n")) if part.strip())
        line = (entry.get("voiceover") or "").strip()
        if not line:
            raise SpecError(f"'{name}' has no voiceover")
        rank = int(entry.get("rank") or len(items) + 1)
        items.append({
            "rank": rank,
            "name": name,
            "kind": entry.get("kind") or _kind_of(name),
            # A tutorial's line is left exactly as written. Forcing "Number
            # three." in front of a step is worse than redundant -- it frames
            # the step as a ranking, and the card no longer says "NO." either.
            "line": line if howto else _ensure_rank_spoken(line, rank),
            "caption": caption,
            "query": (entry.get("pexels_query") or "").strip(),
            "min_seconds": float(entry.get("duration_sec") or 0),
            # Whether a viewer could recognise this thing from a picture.
            # Absent (a hand-written spec) means "trust the query", which is
            # how specs behaved before the planner learned to emit this.
            "depictable": bool(entry.get("depictable", False)),
            # Clip ids a person has watched and chosen. Outranks the query,
            # because it is the only input backed by someone's eyes.
            "pinned": [str(v).strip() for v in (entry.get("pexels_ids") or [])
                       if str(v).strip()],
            # Same idea, for a specific named subject no stock library has:
            # a YouTube id a person watched and verified as actually Creative
            # Commons licensed, e.g. [{"id": "...", "start": 12, "seconds": 6}].
            "youtube_ids": entry.get("youtube_ids") or [],
            # Photographs instead of clips for this step. Set per item, or for
            # the whole spec with a top-level "stills": true.
            "stills": bool(entry.get("stills", raw.get("stills", False))),
        })
    # Steps ascend, ranks descend. Getting this backwards would play a
    # tutorial from its last step to its first.
    items.sort(key=lambda i: i["rank"] if howto else -i["rank"])

    intro = raw.get("intro") or {}
    outro = raw.get("outro") or {}
    return {
        "topic": (raw.get("topic") or intro.get("on_screen_text")
                  or "spec").strip(),
        "title": (intro.get("on_screen_text") or "").strip(),
        "hook": (intro.get("voiceover") or "").strip(),
        # The hook is the shot that decides whether anyone watches, and its
        # pexels_query was being dropped here: every spec opened on whatever
        # generic mood footage `visuals.generic` returned. The flat-tire build
        # asked for "car flat tire roadside" and opened on aerial clouds.
        "hook_query": (intro.get("pexels_query") or "").strip(),
        "cta_query": (outro.get("pexels_query") or "").strip(),
        # A hand-written spec has no mid-roll tease and the builder simply
        # omits it rather than inventing one; a generated spec supplies one.
        "tease": (raw.get("tease") or "").strip(),
        "items": items,
        "cta": (outro.get("voiceover") or "").strip(),
        "caption": (outro.get("on_screen_text") or "").strip(),
        "format": fmt,
        "sources_note": raw.get("sources_note") or source,
        # Speak the lines exactly as written.
        "verbatim": True,
        "_cached": True,
    }
