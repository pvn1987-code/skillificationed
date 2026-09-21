"""Assembles the reel: beat-matched B-roll, karaoke captions, layered CTA.

Three deliberate choices, each from a specific failure of the first version:

1. CUTS FOLLOW THE SCRIPT, NOT A CLOCK. v1 changed clip every 3 seconds
   regardless of what was being said, so a line about a mathematical proof
   played over circuit boards. Stock footage that matches the topic but not the
   sentence reads as filler. Each beat now carries its own clip and the cut
   lands where the spoken subject changes.

2. KARAOKE CAPTIONS. The whole phrase is on screen and the spoken word is
   highlighted, so a muted autoplay viewer has something to track rather than
   reading ahead and waiting. Word timings come from Whisper.

3. LAYERED CTA. A quiet handle runs the whole way and the follow card closes
   it out, because an end card alone only reaches viewers who stayed to the
   last frame -- a minority.

Captions are rendered with Pillow into a per-frame PNG sequence and composited
with ONE overlay via the image2 demuxer. The local ffmpeg is built without
libass/libfreetype, so drawtext and subtitles do not exist in it:

  $ ffmpeg -h filter=drawtext   ->  Unknown filter 'drawtext'

Pillow also means the reel uses fonts/Anton (config.DISPLAY_FONT), the same
face as the carousel, so the two read as one channel.
"""
import difflib
import os
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import config

# Instagram lays its caption, handle and buttons over the bottom of a reel, so
# nothing readable may sit inside this strip.
_SAFE_BOTTOM = config.SAFE_BOTTOM
_TAIL = 0.35
_SIDE_MARGIN = 90
_TOP_Y = 150  # top-of-frame chrome (the rank counter)


class CompositeError(RuntimeError):
    pass


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=60)
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise CompositeError(f"could not read duration of {path.name}")


def _rgb(value: str) -> tuple:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(config.ROOT / "fonts" / config.DISPLAY_FONT), size)


# --- timing -----------------------------------------------------------------

def _norm(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


def retime_script(script: str, words: list) -> list:
    """Script words carrying Whisper's timings.

    Captions must show what was WRITTEN, not what the recogniser heard. Whisper
    turned "real world AI news" into "real-world iNews", and that mistranscription
    burned straight into the frame. The script is known exactly, so only the
    timing needs recovering: match the two sequences, take timings where they
    agree, and interpolate across the gaps.
    """
    tokens = script.split()
    if not tokens:
        return words
    if not words:
        return []

    heard = [_norm(w["word"]) for w in words]
    written = [_norm(t) for t in tokens]
    timed = [None] * len(tokens)
    matcher = difflib.SequenceMatcher(None, heard, written, autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            source, target = block.a + offset, block.b + offset
            timed[target] = (float(words[source]["start"]), float(words[source]["end"]))

    # Interpolate anything unmatched between its nearest timed neighbours, so a
    # misheard word still lands on the right moment rather than vanishing.
    anchors = [i for i, t in enumerate(timed) if t]
    if not anchors:
        span = (float(words[-1]["end"]) - float(words[0]["start"])) / len(tokens)
        start0 = float(words[0]["start"])
        return [{"word": t, "start": start0 + i * span, "end": start0 + (i + 1) * span}
                for i, t in enumerate(tokens)]

    first, last = anchors[0], anchors[-1]
    for index in range(len(tokens)):
        if timed[index]:
            continue
        if index < first:
            base = timed[first][0]
            step = 0.16 * (first - index)
            timed[index] = (max(base - step, 0.0), max(base - step + 0.14, 0.05))
        elif index > last:
            base = timed[last][1]
            step = 0.16 * (index - last - 1)
            timed[index] = (base + step, base + step + 0.14)
        else:
            before = max(a for a in anchors if a < index)
            after = min(a for a in anchors if a > index)
            gap_start, gap_end = timed[before][1], timed[after][0]
            slots = after - before
            share = max((gap_end - gap_start) / slots, 0.08)
            position = index - before - 1
            timed[index] = (gap_start + position * share,
                            gap_start + (position + 1) * share)

    return [{"word": token, "start": timed[i][0], "end": timed[i][1]}
            for i, token in enumerate(tokens)]


def plan_captions(words: list, duration: float, fallback_text: str = "") -> list:
    """Caption pages as [(start, end, [word dicts])].

    Pages break on sentence ends as well as on length: chunking purely by count
    puts a full stop mid-caption, which reads as a mistake however well the
    timing lines up.
    """
    size = max(config.REEL_CAPTION_WORDS, 1)
    if not words:
        if not fallback_text.strip():
            return []
        tokens = fallback_text.split()
        groups = [tokens[i:i + size] for i in range(0, len(tokens), size)]
        span = duration / max(len(groups), 1)
        return [(i * span, (i + 1) * span,
                 [{"word": w, "start": i * span, "end": (i + 1) * span} for w in g])
                for i, g in enumerate(groups)]

    groups, group = [], []
    for word in words:
        group.append(word)
        if len(group) >= size or str(word["word"]).rstrip().endswith((".", "!", "?")):
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    # A one-word tail ("today.") flashes like a stutter; fold it backwards.
    folded = []
    for candidate in groups:
        if len(candidate) == 1 and folded and len(folded[-1]) <= size:
            folded[-1].extend(candidate)
        else:
            folded.append(candidate)

    pages = [(float(g[0]["start"]), float(g[-1]["end"]), g) for g in folded]
    # Close short gaps so a page does not blink out between phrases.
    for index in range(len(pages) - 1):
        start, end, group = pages[index]
        if 0 < pages[index + 1][0] - end < 0.35:
            pages[index] = (start, pages[index + 1][0], group)
    return pages


def align_beats(beat_word_counts: list, words: list, duration: float) -> list:
    """[(start, end)] per beat, by walking the transcript word by word.

    Whisper is transcribing our own synthesised text, so the word sequence
    matches the script -- but not always the COUNT: it merges or drops a word
    here and there. Allocating each beat its exact count and giving the
    remainder to the last one made any shortfall land entirely on the final
    beat, which silently starved the CTA to a zero-length span and dropped the
    closing card. Counts are therefore scaled to the words that actually came
    back, so a deficit is shared out instead of falling on one beat.
    """
    total = sum(beat_word_counts)
    if not words or total == 0:
        share = duration / max(len(beat_word_counts), 1)
        return [(i * share, (i + 1) * share) for i in range(len(beat_word_counts))]

    available = len(words)
    # Scale each beat's share to the transcript, keeping at least one word each
    # and making the shares add up exactly.
    scaled = [max(1, round(count * available / total)) for count in beat_word_counts]
    drift = sum(scaled) - available
    index = len(scaled) - 1
    while drift > 0 and index >= 0:
        take = min(drift, scaled[index] - 1)
        scaled[index] -= take
        drift -= take
        index -= 1
    if drift < 0:
        scaled[-1] += -drift

    spans, cursor = [], 0
    for position, count in enumerate(scaled):
        last = position == len(scaled) - 1
        take = available - cursor if last else min(count, available - cursor)
        if take <= 0:
            # Nothing left: pin a short span at the end rather than a zero one,
            # so the beat still earns a shot.
            spans.append((max(duration - 0.4, 0.0), duration))
            continue
        chunk = words[cursor:cursor + take]
        spans.append((float(chunk[0]["start"]), float(chunk[-1]["end"])))
        cursor += take

    # The first beat starts with the reel and the last runs to the end of the
    # audio: trailing silence belongs to the closing card, not to nothing.
    if spans:
        spans[0] = (0.0, spans[0][1])
        spans[-1] = (spans[-1][0], max(duration, spans[-1][1]))
    return spans


# --- sidecar subtitle files -------------------------------------------------

def _ass_time(seconds: float) -> str:
    seconds = max(seconds, 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def _srt_time(seconds: float) -> str:
    seconds = max(seconds, 0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(secs):02d},{int(secs % 1 * 1000):03d}"


def write_ass(pages: list, out_path: Path) -> Path:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {config.REEL_W}
PlayResY: {config.REEL_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Anton,{config.REEL_CAPTION_SIZE},&H00FFFFFF,&H00FFFFFF,&H00101010,&H96000000,0,0,0,0,100,100,2,0,1,7,4,5,{_SIDE_MARGIN},{_SIDE_MARGIN},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for start, end, group in pages:
        text = " ".join(w["word"] for w in group).replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 1,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,"
                     f"{{\\fad(60,60)}}{text.upper()}\n")
    out_path.write_text("".join(lines))
    return out_path


def write_srt(pages: list, out_path: Path) -> Path:
    blocks = []
    for index, (start, end, group) in enumerate(pages, 1):
        text = " ".join(w["word"] for w in group)
        blocks.append(f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
    out_path.write_text("\n".join(blocks))
    return out_path


# --- caption track ----------------------------------------------------------

def _layout(draw, tokens: list, size: int) -> tuple:
    """Word boxes for a page, each reserving its HIGHLIGHTED width.

    Reserving the widest state is what keeps the spacing still. Laying out at
    the idle size and then drawing the active word larger makes every gap
    around it move as the highlight travels, which is the jitter that showed up
    as uneven spacing. Now each word owns a fixed box and simply fills more of
    it when active, so nothing reflows.
    """
    font = _font(size)
    big = _font(round(size * config.REEL_HIGHLIGHT_SCALE))
    max_width = config.REEL_W - 2 * _SIDE_MARGIN
    space = draw.textlength(" ", font=font)

    reserved = [draw.textlength(token, font=big) for token in tokens]
    lines, current = [], []
    for token, width in zip(tokens, reserved):
        line_width = sum(w for _, w in current) + space * max(len(current) - 1, 0)
        if current and line_width + space + width > max_width:
            lines.append(current)
            current = [(token, width)]
        else:
            current.append((token, width))
    if current:
        lines.append(current)

    line_height = round(size * 1.16)
    boxes, index = [], 0
    top = -(line_height * len(lines)) // 2
    for row, line in enumerate(lines):
        line_width = sum(w for _, w in line) + space * max(len(line) - 1, 0)
        x = (config.REEL_W - line_width) / 2
        for token, width in line:
            boxes.append((index, token, x + width / 2,
                          top + row * line_height + line_height / 2))
            x += width + space
            index += 1
    return boxes, font, big


def _draw_page(draw, tokens: list, active: int, centre_y: int) -> None:
    boxes, font, big = _layout(draw, tokens, config.REEL_CAPTION_SIZE)
    idle, hot = _rgb(config.REEL_CAPTION_IDLE), _rgb(config.REEL_HIGHLIGHT)
    for index, token, cx, dy in boxes:
        is_active = config.REEL_KARAOKE and index == active
        draw.text((cx, centre_y + dy), token,
                  font=big if is_active else font,
                  fill=(hot if is_active else idle) + (255,), anchor="mm",
                  stroke_width=9 if is_active else 8,
                  stroke_fill=(10, 10, 10, 240))


def _scrim(canvas: Image.Image) -> None:
    """A soft dark gradient behind the caption band, and a plate at the foot.

    Stock footage cannot be relied on to be dark: "ink diffusing water" came
    back pale lavender, where the white captions and the amber follow pill both
    lost their contrast. Baking a gentle scrim into the caption
    track makes legibility independent of whatever the library returns, which
    matters more than keeping every frame pristine.
    """
    width, height = canvas.size
    centre = int(height * config.CAPTION_BAND)
    reach = 380
    # The lower floor has to RAMP in. Stepping it produced a hard horizontal
    # seam straight across the frame, which looked like a rendering fault.
    floor_from = height - _SAFE_BOTTOM - 520
    floor_to = height - _SAFE_BOTTOM - 200
    band = Image.new("L", (1, height), 0)
    pixels = band.load()
    for y in range(height):
        distance = abs(y - centre)
        value = 0.0 if distance > reach else 116 * (1 - distance / reach) ** 1.5
        if y >= floor_from:
            ramp = min((y - floor_from) / max(floor_to - floor_from, 1), 1.0)
            # Smoothstep, so neither end of the ramp shows an edge.
            value = max(value, 62 * (ramp * ramp * (3 - 2 * ramp)))
        pixels[0, y] = int(value)
    mask = band.resize((width, height))
    canvas.paste(Image.new("RGBA", (width, height), (8, 8, 12, 255)), (0, 0), mask)


def _plate(draw, cx: int, cy: int, text: str, font) -> None:
    """A small rounded backing so a label survives a bright frame."""
    half_w = draw.textlength(text, font=font) / 2 + 22
    half_h = font.size / 2 + 12
    draw.rounded_rectangle([cx - half_w, cy - half_h, cx + half_w, cy + half_h],
                           radius=half_h, fill=(10, 10, 14, 130))


def _draw_follow_card(draw, progress: float) -> None:
    """The closing follow card. `progress` 0->1 drives a short scale-in."""
    ease = min(max(progress, 0.0), 1.0)
    ease = 1 - (1 - ease) ** 3
    scale = 0.86 + 0.14 * ease
    alpha = int(255 * min(ease * 1.6, 1.0))

    centre_x = config.REEL_W // 2
    centre_y = config.REEL_H - _SAFE_BOTTOM - 150
    label = config.REEL_FOLLOW_TEXT
    handle = config.INSTAGRAM_HANDLE

    size = round(66 * scale)
    font = _font(size)
    hfont = _font(round(40 * scale))
    text_w = draw.textlength(label, font=font)
    pad_x, pad_y = round(58 * scale), round(30 * scale)
    half_w, half_h = text_w / 2 + pad_x, size / 2 + pad_y

    accent = _rgb(config.REEL_HIGHLIGHT)
    draw.rounded_rectangle(
        [centre_x - half_w, centre_y - half_h, centre_x + half_w, centre_y + half_h],
        radius=half_h, fill=accent + (alpha,), outline=(12, 12, 14, alpha), width=5)
    draw.text((centre_x, centre_y), label, font=font, fill=(14, 14, 16, alpha),
              anchor="mm")
    draw.text((centre_x, centre_y + half_h + 46), handle, font=hfont,
              fill=(255, 255, 255, alpha), anchor="mm",
              stroke_width=6, stroke_fill=(10, 10, 10, alpha))


def _caption_for(items: list, rank: int) -> str:
    for _start, _end, item_rank, caption in items:
        if item_rank == rank:
            return caption
    return ""


def _draw_rank_card(draw, rank: int, caption: str, progress: float,
                    label: str = "") -> None:
    """The countdown reveal: a large numeral over the item's name.

    This is the beat of the format. It animates in on the cut, holds while the
    item is being named, then gets out of the way of the caption -- the numeral
    is what makes the list legible to someone watching with the sound off, and
    the reveal is what the whole structure is counting toward.

    `progress` 0->1 drives a short rise-and-settle; the card is drawn in the
    upper third, clear of both the caption band and Instagram's own furniture.
    """
    ease = min(max(progress, 0.0), 1.0)
    ease = 1 - (1 - ease) ** 3
    alpha = int(255 * min(ease * 2.0, 1.0))
    rise = int(46 * (1 - ease))

    centre_x = config.REEL_W // 2
    # Sits high enough that the name plate clears the caption band below it:
    # during the reveal both are on screen at once, and at 470 they crowded.
    numeral_y = 420 + rise
    accent = _rgb(config.REEL_HIGHLIGHT)

    # "No." sits above the numeral so the numeral itself can be enormous
    # without the word competing with it for size.
    small = _font(46)
    draw.text((centre_x, numeral_y - 150), label or config.RANK_LABEL, font=small,
              fill=accent + (alpha,), anchor="mm",
              stroke_width=5, stroke_fill=(10, 10, 12, alpha))

    numeral = _font(240)
    text = str(rank)
    draw.text((centre_x, numeral_y), text, font=numeral,
              fill=(255, 255, 255, alpha), anchor="mm",
              stroke_width=14, stroke_fill=(10, 10, 12, alpha))

    if caption:
        # A spec may put a second line under the name -- "Median Income:
        # ~$143k" -- which is often the most interesting thing on screen. The
        # name goes in the plate; the rest sits under it, smaller, so the plate
        # stays the strong shape and the detail reads as detail.
        lines = [part.strip() for part in caption.split("\n") if part.strip()]
        label = lines[0].upper()
        name_font = _font(72)
        # Shrink rather than wrap: a wrapped name at this size collides with
        # the caption band below it.
        while draw.textlength(label, font=name_font) > config.REEL_W - 120 \
                and name_font.size > 34:
            name_font = _font(name_font.size - 4)
        width = draw.textlength(label, font=name_font)
        box_y = numeral_y + 168
        half_h = name_font.size / 2 + 20
        draw.rounded_rectangle(
            [centre_x - width / 2 - 34, box_y - half_h,
             centre_x + width / 2 + 34, box_y + half_h],
            radius=half_h, fill=accent + (alpha,))
        draw.text((centre_x, box_y), label, font=name_font,
                  fill=(14, 14, 16, alpha), anchor="mm")

        sub_y = box_y + half_h + 34
        sub_font = _font(44)
        for extra in lines[1:2]:
            text = extra.upper()
            while draw.textlength(text, font=sub_font) > config.REEL_W - 140 \
                    and sub_font.size > 26:
                sub_font = _font(sub_font.size - 3)
            draw.text((centre_x, sub_y), text, font=sub_font,
                      fill=(255, 255, 255, alpha), anchor="mm",
                      stroke_width=7, stroke_fill=(10, 10, 12, alpha))


def _draw_rank_chip(draw, rank: int, total: int) -> None:
    """A small persistent "4/10" marker in the corner.

    Cheap retention insurance: a viewer who arrives mid-scroll can see at a
    glance that the list is not finished, which is the completion bias the
    countdown format runs on. It stays up for the whole item, long after the
    reveal card has gone.
    """
    text = f"{rank}/{total}"
    font = _font(40)
    x = config.REEL_W - 110
    y = _TOP_Y
    width = draw.textlength(text, font=font)
    draw.rounded_rectangle([x - width / 2 - 22, y - 34, x + width / 2 + 22, y + 34],
                           radius=34, fill=(10, 10, 14, 150))
    draw.text((x, y), text, font=font, fill=_rgb(config.REEL_HIGHLIGHT) + (235,),
              anchor="mm")


def _draw_credits(draw, lines: list, alpha: int) -> None:
    """Closing attribution. CC BY and CC BY-SA files legally require it.

    Deliberately small and last: it is a licence obligation, not a design
    element, and it must not eat into the frames that are still selling the
    video.
    """
    if not lines:
        return
    font = _font(26)
    y = config.REEL_H - _SAFE_BOTTOM + 60
    draw.text((config.REEL_W // 2, y - 40), "FOOTAGE", font=_font(22),
              fill=(190, 194, 204, alpha), anchor="mm")
    for line in lines[:5]:
        draw.text((config.REEL_W // 2, y), line[:78], font=font,
                  fill=(226, 229, 236, alpha), anchor="mm")
        y += 34


def render_caption_track(pages: list, duration: float, out_dir: Path,
                         items: list | None = None,
                         credits: list | None = None,
                         rank_label: str = "") -> tuple:
    """A transparent PNG per frame, for one image2 input and one overlay.

    Frames whose visible state is identical are hard-linked rather than
    redrawn, so a 20s reel costs a few dozen renders instead of 600. The
    countdown furniture is quantised for the same reason: the reveal card
    animates in 12 steps, not 45, so the whole item still collapses to a
    handful of distinct frames.

    `items` is [(start, end, rank, caption), ...] -- one entry per numbered
    item, driving the reveal card and the persistent chip.
    `credits` is the attribution lines rendered over the closing card.
    """
    items = items or []
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("f_*.png"):
        stale.unlink()

    fps = config.REEL_FPS
    total_frames = max(int(round((duration + _TAIL) * fps)), 1)
    # Captions sit in a LOWER BAND, not the middle of the frame. At
    # (REEL_H - SAFE_BOTTOM)//2 they landed at 780px of 1920 -- 41% down,
    # directly over the subject of every shot, with the scrim darkening the
    # same spot. The footage is the thing being explained; covering its centre
    # with type is the one place text must not go.
    centre_y = int(config.REEL_H * config.CAPTION_BAND)
    card_start = duration + _TAIL - config.REEL_END_CARD_SECONDS
    handle_font = _font(30)

    cache = {}
    for frame in range(total_frames):
        t = frame / fps
        page_index, active = -1, -1
        for index, (start, end, group) in enumerate(pages):
            if start <= t < end:
                page_index = index
                for position, word in enumerate(group):
                    if float(word["start"]) <= t < float(word["end"]):
                        active = position
                        break
                break
        # Quantise the card animation so it collapses to a handful of states.
        card = -1
        if config.REEL_END_CARD_SECONDS > 0 and t >= card_start:
            card = min(int((t - card_start) / 0.05), 12)

        # Which item owns this frame, and how far into its reveal we are.
        rank, chip_rank, reveal = -1, -1, -1
        for start, end, item_rank, _caption in items:
            if start <= t < end:
                chip_rank = item_rank
                if t < start + config.RANK_CARD_SECONDS:
                    rank = item_rank
                    reveal = min(int((t - start) / (config.RANK_CARD_SECONDS / 12)), 12)
                break
        credits_phase = -1
        if credits and card >= 0:
            credits_phase = min(int((t - card_start) / 0.15), 8)

        key = (page_index, active, card, rank, reveal, chip_rank, credits_phase)
        target = out_dir / f"f_{frame:05d}.png"
        if key in cache:
            os.link(cache[key], target)
            continue

        canvas = Image.new("RGBA", (config.REEL_W, config.REEL_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        _scrim(canvas)
        if config.REEL_SHOW_HANDLE and config.INSTAGRAM_HANDLE:
            # Sits lower than the old label so it clears the caption band
            # entirely, and carries a faint shadow instead of a plate: enough
            # to stay legible over white footage without announcing itself.
            handle_y = config.REEL_H - _SAFE_BOTTOM + 52
            alpha = max(0, min(255, config.REEL_HANDLE_ALPHA))
            if config.REEL_HANDLE_PLATE:
                _plate(draw, config.REEL_W // 2, handle_y,
                       config.INSTAGRAM_HANDLE, handle_font)
            draw.text((config.REEL_W // 2, handle_y), config.INSTAGRAM_HANDLE,
                      font=handle_font, fill=(255, 255, 255, alpha),
                      anchor="mm", stroke_width=2,
                      stroke_fill=(0, 0, 0, max(0, alpha - 45)))
        if config.RANK_CHIP and chip_rank > 0 and items:
            _draw_rank_chip(draw, chip_rank, len(items))
        if rank > 0:
            _draw_rank_card(draw, rank, _caption_for(items, rank), reveal / 12,
                            label=rank_label)
        if page_index >= 0:
            _draw_page(draw, [w["word"] for w in pages[page_index][2]], active, centre_y)
        if card >= 0:
            _draw_follow_card(draw, card / 12)
        if credits_phase >= 0:
            _draw_credits(draw, credits, int(200 * min(credits_phase / 8 * 1.6, 1.0)))
        canvas.save(target)
        cache[key] = target

    return out_dir, total_frames


# --- assembly ---------------------------------------------------------------

def build_segments(beat_spans: list, clip_indices: list, clip_durations: list,
                   duration: float) -> list:
    """(clip index, in-point, length), cutting where the subject changes.

    A beat too short to earn its own shot extends the previous one instead --
    below about 1.6s a cut reads as a glitch. A beat that runs long is split
    into several shots from the same clip at advancing in-points, so the visual
    still matches the sentence but the frame keeps moving.
    """
    segments = []
    last_index = len(beat_spans) - 1
    for position, ((start, end), clip) in enumerate(zip(beat_spans, clip_indices)):
        length = max(end - start, 0.0)
        if length <= 0.01:
            continue
        # The closing card is never merged away, however short it runs: it is
        # on its own pinned plate, and folding it into the previous shot means
        # the follow card plays over a news clip instead.
        if segments and length < config.REEL_MIN_SHOT and position != last_index:
            previous = segments[-1]
            segments[-1] = (previous[0], previous[1], round(previous[2] + length, 2))
            continue
        remaining, step = length, 0
        while remaining > 0.01:
            take = min(remaining, config.REEL_MAX_SHOT)
            if remaining - take < config.REEL_MIN_SHOT:
                take = remaining
            available = clip_durations[clip]
            in_point = min(step * config.REEL_MAX_SHOT, max(available - take, 0))
            segments.append((clip, round(in_point, 2), round(take, 2)))
            remaining -= take
            step += 1
    if not segments:
        raise CompositeError("no segments planned")

    # Beat spans end at the last spoken WORD, but the audio carries trailing
    # silence and the reel adds a tail, so the segments alone leave the video
    # track short of the audio -- a second of nothing at the end, with the
    # follow card falling outside the frames that exist. Stretch the final
    # shot to cover it.
    target = duration + _TAIL
    shortfall = target - sum(length for _, _, length in segments)
    if shortfall > 0.01:
        clip, in_point, length = segments[-1]
        length = round(length + shortfall, 2)
        in_point = round(min(in_point, max(clip_durations[clip] - length, 0)), 2)
        segments[-1] = (clip, in_point, length)
    return segments


def tile_spans(spans: list, total: float) -> list:
    """Make beat spans contiguous, so the video track cannot drift.

    align_beats bounds each beat by its first and last SPOKEN word, which
    leaves the pause between two beats belonging to neither. The video track is
    built by concatenating segment lengths, so every unallocated pause makes
    the footage run ahead of the voice by that much, and the error accumulates:
    in a 13-beat countdown the drift reached several seconds and item number
    one was captioned VENICE over footage of clouds.

    Each beat is therefore extended to the moment the next beat's first word is
    spoken. The pause after a line belongs to that line's shot, and every rank
    card appears exactly as its item is named.
    """
    if not spans:
        return spans
    tiled = []
    for index, (start, end) in enumerate(spans):
        start = 0.0 if index == 0 else tiled[-1][1]
        if index + 1 < len(spans):
            end = spans[index + 1][0]
        else:
            end = max(total, end)
        # Spans can overlap by a rounding hair; never emit a backwards one.
        tiled.append((start, max(end, start + 0.05)))
    return tiled


def build_segments_multi(beat_spans: list, beat_clips: list,
                         clip_durations: list, duration: float) -> list:
    """Like build_segments, but each beat carries SEVERAL clips of its own.

    The countdown needs this: an item gets about four seconds, and retention
    work is consistent that a static shot past roughly four seconds is where
    viewers leave, so each item is deliberately cut into two shots of its own
    authentic footage rather than held on one.

    In-points advance per clip, so a clip reused later in the reel resumes
    where it left off instead of replaying the same opening frames.
    """
    segments = []
    used_from = {}
    last_index = len(beat_spans) - 1
    for position, ((start, end), clips) in enumerate(zip(beat_spans, beat_clips)):
        length = max(end - start, 0.0)
        if length <= 0.01 or not clips:
            continue
        # Below the minimum a cut reads as a glitch, so a very short beat
        # extends the previous shot instead. The final beat is never merged:
        # it carries the closing card.
        if segments and length < config.REEL_MIN_SHOT and position != last_index:
            previous = segments[-1]
            segments[-1] = (previous[0], previous[1], round(previous[2] + length, 2))
            continue
        shots = max(1, min(len(clips), int(length // config.REEL_MIN_SHOT) or 1))
        share = length / shots
        for index in range(shots):
            clip = clips[index % len(clips)]
            take = share if index < shots - 1 else length - share * (shots - 1)
            available = clip_durations[clip]
            offset = used_from.get(clip, 0.0)
            if offset + take > available:
                offset = 0.0          # wrap rather than run off the end
            in_point = round(min(offset, max(available - take, 0.0)), 2)
            segments.append((clip, in_point, round(take, 2)))
            used_from[clip] = in_point + take
    if not segments:
        raise CompositeError("no segments planned")

    # Beat spans end at the last spoken word, but the audio carries trailing
    # silence and the reel adds a tail, so stretch the final shot to cover it.
    target = duration + _TAIL
    shortfall = target - sum(length for _, _, length in segments)
    if shortfall > 0.01:
        clip, in_point, length = segments[-1]
        length = round(length + shortfall, 2)
        in_point = round(min(in_point, max(clip_durations[clip] - length, 0)), 2)
        segments[-1] = (clip, in_point, length)
    return segments


def assemble(clips: list, audio: Path, segments: list, frames_dir: Path,
             out_path: Path, duration: float) -> Path:
    if not clips:
        raise CompositeError("no B-roll clips to assemble")
    total = duration + _TAIL

    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for clip in clips:
        command += ["-i", str(clip)]
    command += ["-framerate", str(config.REEL_FPS),
                "-i", str(frames_dir / "f_%05d.png")]
    command += ["-i", str(audio)]
    caption_index, audio_index = len(clips), len(clips) + 1

    chains, labels = [], []
    for position, (clip, start, length) in enumerate(segments):
        tag = f"seg{position}"
        chains.append(
            f"[{clip}:v]trim=start={start}:duration={length},setpts=PTS-STARTPTS,"
            f"scale={config.REEL_W}:{config.REEL_H}:force_original_aspect_ratio=increase,"
            f"crop={config.REEL_W}:{config.REEL_H},fps={config.REEL_FPS},"
            f"setsar=1,format=yuv420p[{tag}]")
        labels.append(f"[{tag}]")
    chains.append(f"{''.join(labels)}concat=n={len(segments)}:v=1:a=0[joined]")
    chains.append(f"[joined][{caption_index}:v]overlay=0:0:format=auto:"
                  f"eof_action=pass[captioned]")
    chains.append("[captioned]format=yuv420p[final]")

    command += [
        "-filter_complex", ";".join(chains),
        "-map", "[final]", "-map", f"{audio_index}:a",
        "-t", f"{total:.2f}",
        # Instagram's documented reel envelope: H.264 high, AAC stereo.
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
        "-r", str(config.REEL_FPS), "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        str(out_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise CompositeError(f"ffmpeg failed: {result.stderr.strip()[-700:]}")
    return out_path
