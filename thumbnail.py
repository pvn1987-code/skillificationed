#!/usr/bin/env python3
"""A thumbnail for a built countdown reel, cut from its own verified footage.

The tile is the whole click decision on a channel grid and in search, and until
now there was none -- YouTube grabbed whatever frame it liked, which for a
countdown is usually a rank card mid-animation or a caption caught between
pages.

Two rules shape it.

* The background is the reel's OWN sourced footage, not a fresh stock search.
  This project exists to prove the footage under an item really is that item
  (`visuals.py`'s sourcing ladder), and a thumbnail pulled from somewhere else
  would advertise the one thing the pipeline refuses to do. Only EXACT-tier
  shots are eligible.
* It does not spoil number one. The frame is drawn from ranks 2-5 -- near the
  top, so the best-looking footage, but never the reveal the whole script is
  counting toward. Nothing on the tile names the item, so no rank is given
  away by the picture alone.

Frames come from the cached clip rather than the finished mp4: the rendered
video already has rank cards and karaoke captions burned into it, and a
thumbnail built on top of those is text over text.

  ./thumbnail.py <slug>            write thumbnail.jpg + thumbnail_16x9.jpg
  ./thumbnail.py <slug> --rank 4   force a particular item's footage
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

import config
import videocomposite as vc

# YouTube's custom-thumbnail spec is 1280x720. The vertical one is what a
# Shorts tile and the channel grid actually show, so both are written.
WIDE = (1280, 720)

# Ranks 2-5: high enough that the footage is the strong stuff, never rank 1.
PREFERRED_RANKS = (2, 3, 4, 5)


def _log(message=""):
    print(message, flush=True)


# Best first. EXACT is footage verified to be the subject; ILLUSTRATIVE is
# footage a person asked for by name for that item -- still the item's own
# sourcing, still better than a stock cloud. GENERIC is the hook's mood
# footage and is the last resort.
TIER_ORDER = ("EXACT", "ILLUSTRATIVE")


def _clip_for(reel: dict, rank: int | None) -> tuple:
    """(clip path, item name) for the thumbnail, or (None, "").

    Walks tier first, then rank. Insisting on EXACT everywhere looked right
    until "Top 10 richest counties" was tried: not one of its ten items has
    exact footage -- the RUNBOOK names that topic as the honest example -- so
    an EXACT-only rule skipped all ten and put the generic cloud shot from the
    hook on the tile. A whole class of topic would have shipped that way.
    """
    by_rank = {f.get("rank"): f for f in reel.get("footage") or []}
    if rank:
        ranks = [rank]
    else:
        # Ranks 2-5 first, then any other real item, never the reveal.
        ranks = list(PREFERRED_RANKS) + [
            r for r in sorted(by_rank) if r not in PREFERRED_RANKS
            and r not in (0, 1)]
    for tier in TIER_ORDER:
        for candidate in ranks:
            entry = by_rank.get(candidate)
            if not entry:
                continue
            for shot in entry.get("shots") or []:
                if shot.get("tier") != tier:
                    continue
                path = config.ASSET_CACHE / str(shot.get("file") or "")
                if path.exists():
                    return path, entry.get("name", "")
    # Nothing of any item resolved. A generic background beats no thumbnail.
    for shot in (by_rank.get(0) or {}).get("shots") or []:
        path = config.ASSET_CACHE / str(shot.get("file") or "")
        if path.exists():
            return path, ""
    return None, ""


def _frame(clip: Path, out: Path) -> Path | None:
    """A still from the middle of the clip, where it is least likely to be
    mid-transition or still settling from a camera move."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(clip)],
        capture_output=True, text=True)
    try:
        seek = max(0.0, float(probe.stdout.strip()) / 2)
    except ValueError:
        seek = 0.0
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(seek), "-i", str(clip),
         "-frames:v", "1", "-y", str(out)], capture_output=True)
    return out if result.returncode == 0 and out.exists() else None


def _scrim(canvas: Image.Image, top_frac: float = 0.42) -> Image.Image:
    """Darken the lower part so white type holds against any footage.

    Drawn as a gradient rather than a flat box: a hard edge across a
    photograph reads as a banner stuck on top, which is exactly the homemade
    look the tile is trying to avoid.
    """
    width, height = canvas.size
    start = int(height * top_frac)
    overlay = Image.new("L", (1, height), 0)
    draw = ImageDraw.Draw(overlay)
    for y in range(start, height):
        t = (y - start) / max(1, height - start)
        draw.point((0, y), fill=int(235 * (t ** 0.85)))
    mask = overlay.resize((width, height))
    dark = Image.new("RGB", (width, height), (6, 7, 10))
    return Image.composite(dark, canvas, mask)


def _split_title(title: str, item_count: int = 0, howto: bool = False) -> tuple:
    """"Top Ten Cities To Visit Before You Die" -> ("TOP 10", "CITIES TO ...").

    The count is rendered separately and huge, because "TOP 10" is the genre
    signal that makes someone read the rest of the tile at all.
    """
    # A tutorial is not a ranking. The first flat-tire build put "TOP 7" over
    # "HOW TO CHANGE A FLAT TIRE", which promises a countdown the video does
    # not contain. Steps get a step count instead, and only when there are
    # enough of them for the number to be a selling point.
    if howto:
        return (f"{item_count} STEPS" if item_count >= 3 else ""), \
               (title or "").upper()
    words = (title or "").strip().split()
    lowered = [w.lower().strip(".,") for w in words]
    counts = {"ten": "10", "five": "5", "seven": "7", "twelve": "12",
              "fifteen": "15", "twenty": "20", "three": "3", "six": "6",
              "eight": "8", "nine": "9"}
    if len(words) >= 2 and lowered[0] == "top":
        second = lowered[1]
        number = counts.get(second, second if second.isdigit() else "")
        if number:
            return f"TOP {number}", " ".join(words[2:]).upper()
    # A title that does not carry its own count still gets the badge. "TOP 10"
    # is the genre signal that makes someone read the rest of the tile, and
    # "Richest US Counties" -- a real plan title -- would otherwise have gone
    # out with no badge at all despite being a ten-item countdown.
    if item_count:
        return f"TOP {item_count}", (title or "").upper()
    return "", (title or "").upper()


def _wrap(draw, text: str, font, max_width: int) -> list:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _compose(frame: Path, count: str, subject: str, size: tuple,
             out: Path) -> Path:
    width, height = size
    canvas = vc.fit_cover(Image.open(frame).convert("RGB"), width, height) \
        if hasattr(vc, "fit_cover") else _fit_cover(Image.open(frame).convert("RGB"), width, height)
    tall = height > width
    canvas = _scrim(canvas, 0.38 if tall else 0.30)
    draw = ImageDraw.Draw(canvas)
    accent = vc._rgb(config.REEL_HIGHLIGHT)
    margin = int(width * 0.06)
    text_w = width - 2 * margin

    # Sized off the canvas so both crops read the same. The 16:9 fractions are
    # LARGER relative to their canvas than the vertical ones, because a wide
    # thumbnail is shown small -- a search result is a couple of hundred pixels
    # across, and type scaled to look right at full size vanishes there.
    count_size = int(height * (0.115 if tall else 0.15))
    subj_size = int(height * (0.072 if tall else 0.092))
    count_font = vc._font(count_size)
    subj_font = vc._font(subj_size)
    while subj_size > 20:
        lines = _wrap(draw, subject, subj_font, text_w)
        if len(lines) <= (3 if tall else 2):
            break
        subj_size -= 4
        subj_font = vc._font(subj_size)
    lines = _wrap(draw, subject, subj_font, text_w)

    line_h = int(subj_size * 1.04)
    block_h = line_h * len(lines) + (int(count_size * 1.1) if count else 0)
    y = height - int(height * 0.07) - block_h

    if count:
        draw.text((margin, y), count, font=count_font, fill=accent,
                  stroke_width=max(3, count_size // 18),
                  stroke_fill=(10, 10, 12), anchor="la")
        y += int(count_size * 1.1)
    for line in lines:
        draw.text((margin, y), line, font=subj_font, fill=(255, 255, 255),
                  stroke_width=max(3, subj_size // 20),
                  stroke_fill=(10, 10, 12), anchor="la")
        y += line_h

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=94)
    return out


def _fit_cover(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / image.width, height / image.height)
    resized = image.resize((max(1, round(image.width * scale)),
                            max(1, round(image.height * scale))),
                           Image.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def build(slug: str, rank: int | None = None) -> int:
    day_dir = config.OUTPUT_DIR / slug if hasattr(config, "OUTPUT_DIR") \
        else config.ROOT / "output" / slug
    reel_json, plan_json = day_dir / "reel.json", day_dir / "plan.json"
    if not reel_json.exists():
        _log(f"X no reel.json in {day_dir} — build the reel first")
        return 1
    reel = json.loads(reel_json.read_text())
    plan = json.loads(plan_json.read_text()) if plan_json.exists() else {}

    clip, name = _clip_for(reel, rank)
    if not clip:
        _log("X no usable footage found for a thumbnail")
        return 1
    _log(f"  footage: {clip.name}" + (f"  ({name})" if name else ""))

    frame = _frame(clip, day_dir / "thumbnail_frame.jpg")
    if not frame:
        _log("X could not extract a frame")
        return 1

    count, subject = _split_title(plan.get("title") or reel.get("title") or "",
                                  len(plan.get("items") or []),
                                  howto=plan.get("format") == "howto")
    _log(f"  title: {count!r} + {subject!r}")
    vertical = _compose(frame, count, subject, (config.REEL_W, config.REEL_H),
                        day_dir / "thumbnail.jpg")
    wide = _compose(frame, count, subject, WIDE, day_dir / "thumbnail_16x9.jpg")
    _log(f"  -> {vertical}")
    _log(f"  -> {wide}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slug")
    parser.add_argument("--rank", type=int, default=None,
                        help="force a particular item's footage (1 spoils the reveal)")
    args = parser.parse_args()
    return build(args.slug, args.rank)


if __name__ == "__main__":
    sys.exit(main())
