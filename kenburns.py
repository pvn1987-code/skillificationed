"""Turns a still into a moving 9:16 shot.

Necessary, not decorative. The only authentic footage of most people and many
brands is a photograph, and a photograph held static in a feed reads as a dead
frame -- the thing short-form retention work warns about most consistently. A
slow push with a little drift keeps the frame alive without drawing attention
to itself.

Two implementation notes that cost time to find:

* zoompan jitters badly when it zooms a source at final resolution, because it
  rounds its crop window to whole pixels. Upscaling roughly 3x first makes each
  rounding step subpixel at output size, which is the standard fix.
* Faces sit in the upper part of a portrait, so a centre crop of a 3:2 press
  photo tends to cut foreheads. Portraits are anchored above centre instead.
"""
import subprocess
from pathlib import Path

import config


class KenBurnsError(RuntimeError):
    pass


# Zoompan is fed a source this many times the output width; see the note above.
_SUPERSAMPLE = 3


def render(image: Path, out_path: Path, seconds: float | None = None,
           direction: str = "in", anchor: str = "centre") -> Path:
    """One still to one portrait clip with slow motion applied.

    `direction` "in" pushes toward the subject and "out" pulls away; alternating
    them across an item's two shots is what stops the pair looking like the same
    photograph twice.
    `anchor` "top" biases the crop upward, for portraits of people.
    """
    seconds = seconds or config.KENBURNS_SECONDS
    fps = config.REEL_FPS
    frames = max(int(round(seconds * fps)), 2)
    zoom = max(config.KENBURNS_ZOOM, 1.01)

    big_w, big_h = config.REEL_W * _SUPERSAMPLE, config.REEL_H * _SUPERSAMPLE
    # Crop offset into the upscaled frame. A portrait keeps the head room that
    # a centre crop would spend on the subject's chest.
    y_expr = "(ih-oh)/2" if anchor != "top" else "(ih-oh)*0.28"

    if direction == "out":
        # Start zoomed and ease back. Counting down from `zoom` needs the
        # per-frame step precomputed: zoompan has no frame-count variable that
        # survives being used inside min()/max() reliably across builds.
        step = (zoom - 1.0) / frames
        z_expr = f"max({zoom}-on*{step:.8f},1.0)"
    else:
        step = (zoom - 1.0) / frames
        z_expr = f"min(1.0+on*{step:.8f},{zoom})"

    chain = (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={big_w}:{big_h}:(iw-ow)/2:{y_expr},"
        f"zoompan=z='{z_expr}':d={frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={config.REEL_W}x{config.REEL_H}:fps={fps},"
        f"setsar=1,format=yuv420p"
    )
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-i", str(image),
        "-vf", chain, "-frames:v", str(frames),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(fps), str(out_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not out_path.exists():
        raise KenBurnsError(f"ffmpeg failed on {image.name}: "
                            f"{result.stderr.strip()[-400:]}")
    return out_path
