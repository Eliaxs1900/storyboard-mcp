# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.10,<2", "av>=14", "pillow>=10.1", "pillow-heif>=0.16", "pydantic>=2.7"]
# ///
"""storyboard-mcp: MCP server that turns a video, or a list of images, into pages of
panels an assistant can look at (it cannot play a video). No ffmpeg needed.

Tools (they all take the same JSON sections; only "input" is required)
  media_info       what the input is: the video's duration, fps, frames, size and
                   rotation, or each image's name, format and size
  make_storyboard  pages of panels: grid (contact sheet), storyboard (number above,
                   notes box below), list or strip; PNG / JPG pages or one PDF, plus
                   a shot list (JSON / CSV) and the full-size frames if asked. Also
                   animated (MP4, WebM, GIF, WebP): the page with every panel playing
                   its part of the video (mosaic) or the panels one after another
                   (animatic); and a colour script (dominant colours per panel)
  extract_frames   chosen video frames at full size, or copies of chosen images
  get_schema       the JSON Schema of every tool with a minimal and a full example

Sections: input (video | images | folder), select (which frames / images become
panels), panels (notes per panel), layout, style, output.

The Pydantic models below are the single source of truth: the tools' input schemas,
the error messages, get_schema and schema.json (`server.py --schema`, checked by
tests/test_server.py) all come from them.

How frames are chosen (select)
  - stretches (default for video): the range is cut into `count` equal stretches and
    one frame is taken from each: the sharpest one (mode "sharpest") or the one in
    the middle ("even"). Every frame of the range is measured first (sharpness =
    variance of the edge image of a small grey copy), so filters can drop blurry
    frames (min_sharpness / drop_blurriest_pct), picks too close in time (min_gap_s)
    or near-duplicates of the previous pick (min_difference).
  - exact positions (step_pct, step_s, at_pct, at_s, at_frames, at_images,
    at_names): the server seeks to each one, without decoding the whole video.
  - panels[] with positions: each panel says which frame or image it shows.
  - shots (mode "shots", video): one panel per shot. Cuts are jumps between two
    consecutive tiny copies that stand out from the movement around them (flashes
    are left out); dissolves are stretches where the middle frame is the mix of the
    ends; fades are frames getting steadily closer to a plain (black) frame.
Images work the same way, with the list order in place of time.

Animated output (output.format mp4, webm, gif or webp) always comes with the still
pages (PNG), an image of sample frames and the JSON, since an assistant cannot play it.

Decoding uses PyAV (bundled FFmpeg libraries). Phone videos store a rotation and
phone photos an EXIF orientation; pictures are turned to look the way they were shot.

bin/ffmpeg is a copy of the FFmpeg command line tool for anything PyAV is not
the right fit for, built from the official sources by build-ffmpeg.sh (LGPL, see
bin/LICENSE-ffmpeg.md). `server.py --is-ffmpeg-updated` tells whether it is the
newest FFmpeg release on the official GitHub repository (github.com/FFmpeg/FFmpeg).
"""
import base64
import bisect
import csv
import difflib
import fnmatch
import hashlib
import io
import json
import math
import os
import re
import shutil
import statistics
import string
import struct
import subprocess
import sys
import tempfile
import types as pytypes
import typing
import urllib.request
from array import array
from collections import deque
from dataclasses import dataclass, field, replace
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

import anyio
import av
import mcp.types as mt
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat
from pydantic import (AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, ValidationError,
                      ValidationInfo, WithJsonSchema, field_validator, model_validator)

try:  # HEIC / HEIF photos (iPhone)
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF = True
except Exception:  # pragma: no cover - optional
    HEIF = False

VERSION = "2.1.0"
REPO = "https://github.com/Eliaxs1900/storyboard-mcp"
MAX_IMAGES = 1000                 # images in input.images / input.folder
MAX_PANELS = 500                  # panels in one call
MAX_PAGES = 200
MAX_PAGE_PIXELS = 150_000_000     # one page (e.g. 12 000 × 12 500 px)
ANIMATED = ("mp4", "webm", "gif", "webp")
MAX_VIDEO_S = 300                 # length of an animated output
MAX_VIDEO_FRAMES = 3600           # duration × fps
MAX_VIDEOS = 10                   # mosaic: one video per page
MAX_SIDE = {"mp4": 3840, "webm": 3840, "gif": 1280, "webp": 1280}     # longest side, px
DEFAULT_SIDE = {"mp4": 1920, "webm": 1920, "gif": 960, "webp": 960}
Image.MAX_IMAGE_PIXELS = 200_000_000
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"} | (
    {".heic", ".heif"} if HEIF else set())
SUPPORTED_IMAGES = "jpg, png, webp, gif, bmp, tiff" + (", heic" if HEIF else "")

ASPECT_RATIOS = {"16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "1:1": 1.0, "4:5": 0.8,
                 "1.85:1": 1.85, "2:1": 2.0, "2.39:1": 2.39}
Aspect = Literal["16:9", "9:16", "4:3", "3:4", "1:1", "4:5", "1.85:1", "2:1", "2.39:1"]
PAGE_INCHES = {"A4": (8.268, 11.693), "Letter": (8.5, 11.0), "16:9": (7.5, 13.333)}   # (short, long) side

# Texts drawn on the pages (layout.language).
STRINGS = {
    "es": {"frames": "fotogramas", "images": "imágenes", "sharpness": "nitidez", "of": "de",
           "first_frame": "primer fotograma", "last_frame": "último fotograma", "page": "página {p} de {n}",
           "range": "rango", "measured": "medidos", "dropped": "descartados", "threshold": "umbral",
           "min": "mín", "median": "mediana", "max": "máx",
           "mode": {"sharpest": "el más nítido por tramo", "even": "el del centro de cada tramo", "all": "todos",
                    "best": "los más nítidos", "positions": "posiciones exactas", "panels": "elegidos en panels",
                    "shots": "el más nítido de cada plano"},
           "mode_images": {"sharpest": "la más nítida por tramo", "even": "la del centro de cada tramo",
                           "all": "todas", "best": "las más nítidas", "positions": "posiciones exactas",
                           "panels": "elegidas en panels"},
           "notes": {"shot": "PLANO", "camera": "CÁMARA", "action": "ACCIÓN", "dialogue": "DIÁLOGO",
                     "sound": "SONIDO", "transition": "TRANSICIÓN", "duration_s": "DURACIÓN"},
           "shots": "planos", "cuts": "cortes", "fades": "fundidos", "dissolves": "encadenados",
           "panel": "viñeta", "panels": "viñetas", "color_script": "Guion de color", "colors": "colores por viñeta",
           "samples": "Fotogramas de muestra", "loop": "bucle", "speed": "velocidad",
           "trans": {"cut": "corte", "dissolve": "encadenado", "end": "fin", "start": "inicio",
                     "fade_out": {"black": "fundido a negro", "white": "fundido a blanco", None: "fundido"},
                     "fade_in": {"black": "desde negro", "white": "desde blanco", None: "fundido"}}},
    "en": {"frames": "frames", "images": "images", "sharpness": "sharpness", "of": "of",
           "first_frame": "first frame", "last_frame": "last frame", "page": "page {p} of {n}",
           "range": "range", "measured": "measured", "dropped": "dropped", "threshold": "threshold",
           "min": "min", "median": "median", "max": "max",
           "mode": {"sharpest": "sharpest per stretch", "even": "middle of each stretch", "all": "all",
                    "best": "the sharpest", "positions": "exact positions", "panels": "chosen in panels",
                    "shots": "sharpest of each shot"},
           "notes": {"shot": "SHOT", "camera": "CAMERA", "action": "ACTION", "dialogue": "DIALOGUE",
                     "sound": "SOUND", "transition": "TRANSITION", "duration_s": "DURATION"},
           "shots": "shots", "cuts": "cuts", "fades": "fades", "dissolves": "dissolves",
           "panel": "panel", "panels": "panels", "color_script": "Colour script", "colors": "colours per panel",
           "samples": "Sample frames", "loop": "loop", "speed": "speed",
           "trans": {"cut": "cut", "dissolve": "dissolve", "end": "end", "start": "start",
                     "fade_out": {"black": "fade to black", "white": "fade to white", None: "fade out"},
                     "fade_in": {"black": "from black", "white": "from white", None: "fade in"}}},
}

# Theme defaults; every key can be overridden in "style".
THEMES = {
    "dark": dict(background="#000000", text="#ffffff", text_secondary="#a3a3a3", accent="#facc15",
                 label_background="#000000", label_text="#facc15", panel_border="#3f3f46",
                 notes_background="#0a0a0a", notes_text="#e5e5e5", notes_rule="#2e2e2e", guides="#22d3ee"),
    "light": dict(background="#ffffff", text="#000000", text_secondary="#525252", accent="#b45309",
                  label_background="#ffffff", label_text="#000000", panel_border="#d4d4d4",
                  notes_background="#ffffff", notes_text="#171717", notes_rule="#e5e5e5", guides="#0891b2"),
}

# Rows per fixed page (landscape, portrait) when layout.rows is not given.
DEFAULT_ROWS = {"grid": (3, 5), "storyboard": (2, 3), "list": (2, 4), "strip": (1, 1)}

# Template defaults; columns, gap, margin and border_width can be overridden.
TEMPLATES = {
    "grid": dict(columns=4, gap=0, margin=0, border=0, caption="overlay", notes=False),
    "storyboard": dict(columns=3, gap=28, margin=48, border=1, caption="line", notes=True),
    "list": dict(columns=1, gap=24, margin=48, border=1, caption="line", notes=True),
    "strip": dict(columns=8, gap=12, margin=24, border=1, caption="overlay", notes=True),
}

INSTRUCTIONS = f"""\
Turns a video, or a list of images, into pages of panels you can look at (you cannot
play a video), with one JSON entry per panel.
Tools: media_info (what the input is) -> make_storyboard (pages as PNG, JPG or PDF, or
an animated MP4, WebM, GIF or WebP; the answer includes a preview of page 1) ->
extract_frames (chosen frames at full size). get_schema returns the JSON Schema of every
tool with examples.
All tools take the same JSON sections; only "input" is required:
  input   exactly one of {{"video": path}}, {{"images": [paths]}}, {{"folder": path}}
  select  which frames / images become panels (default: video, the sharpest frame of
          each of 12 equal stretches; images, all of them in order; mode "shots": one
          panel per shot of a video, with its in, out, duration and cut / fade / dissolve)
  panels  notes per panel: shot, camera, action, dialogue, sound, transition, duration_s
  layout  template (grid | storyboard | list | strip), columns, rows, page (auto, A4,
          Letter, 16:9), labels, numbering, guides, language
  style   theme (dark | light) and any colour, font or size
  output  dir, name, format (png | jpg | pdf, or animated: mp4 | webm | gif | webp, see
          output.animation), shot_list (json, csv), frames, color_script
- Paths: absolute or ~/... (a relative path is resolved against the server's working
  directory). Files go to output.dir, or to a temporary folder named in the answer.
- Positions: video frames 0..frames-1; seconds from the first frame (0.0); % from the
  first (0) to the last frame or image (100); images by index 0..count-1 or file name.
- Exact times: step_s / at_s / at_pct (+ snap_s to avoid blurred frames). The sharpest
  of each stretch: count / every_s (irregular times). The N sharpest: mode "best".
- Animated formats: you cannot play them, so the answer also gives the still pages
  (PNG), an image of sample frames (videos[].samples) and the JSON; look at those.
- A mistake in the arguments returns every problem with its path, e.g.
  "layout.columns: Input should be greater than or equal to 1 (got 0)".
Guide with example images: {REPO}/blob/main/docs/formatos.md"""


# ============================================================================ schema
class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _checking(info: ValidationInfo | None) -> bool:
    """Paths are checked on disk unless validating examples (context skip_path_checks)."""
    return not (info and info.context and info.context.get("skip_path_checks"))


def _abs(p: str) -> Path:
    return Path(p).expanduser().resolve()


def _relative_hint(p: str) -> str:
    if Path(p).is_absolute() or p.startswith("~"):
        return ""
    return f" (relative paths are resolved against the server's working directory, {os.getcwd()}; " \
           "give an absolute path)"


_HEX = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
_ES_COLOURS = {"blanco": "white", "negro": "black", "rojo": "red", "verde": "green", "azul": "blue",
               "amarillo": "yellow", "naranja": "orange", "gris": "gray", "morado": "purple", "violeta": "violet",
               "rosa": "pink", "marrón": "brown", "marron": "brown", "dorado": "gold", "plateado": "silver",
               "celeste": "skyblue", "turquesa": "turquoise", "granate": "maroon", "beis": "beige"}


def _check_color(v: str) -> str:
    s = v.strip()
    if _HEX.fullmatch(s):
        return s.lower()
    if re.fullmatch(r"[A-Za-z]+", s):
        try:
            ImageColor.getrgb(s.lower())
            return s.lower()
        except ValueError:
            pass
    en = _ES_COLOURS.get(s.lower())
    raise ValueError(f'"{v}" is not a valid colour; use #RRGGBB or #RRGGBBAA (also #RGB, #RGBA) '
                     'or a CSS colour name in English such as "white", "black" or "navy"'
                     + (f' (for "{v}": "{en}")' if en else ""))


Color = Annotated[str, AfterValidator(_check_color), WithJsonSchema({
    "type": "string", "pattern": "^(#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})|[A-Za-z]+)$"})]


def _ci(lit):
    """Accept the values of a Literal in any case ("PDF", "a4"), and give back the canonical spelling."""
    canon = {str(c).lower(): c for c in typing.get_args(lit)}
    return Annotated[lit, BeforeValidator(lambda v: canon.get(v.strip().lower(), v) if isinstance(v, str) else v)]


def _out_format(v):
    if isinstance(v, str):
        v = {"jpeg": "jpg", "mpeg4": "mp4", "m4v": "mp4"}.get(v.strip().lower(), v.strip().lower())
    return v


OutFormat = Annotated[Literal["png", "jpg", "pdf", "mp4", "webm", "gif", "webp"], BeforeValidator(_out_format)]


def _rgba(c: str) -> tuple[int, int, int, int]:
    return ImageColor.getcolor(c, "RGBA")  # type: ignore[return-value]


def _image_file(v: str, info: ValidationInfo) -> str:
    if not _checking(info):
        return v
    p = _abs(v)
    if not p.is_file():
        raise ValueError(f"image not found: {p}" + _relative_hint(v))
    try:
        with Image.open(p):
            pass
    except Exception:
        raise ValueError(f"not an image this server can read: {p.name} (supported: {SUPPORTED_IMAGES})")
    return str(p)


ImageFile = Annotated[str, AfterValidator(_image_file)]
Pct = Annotated[float, Field(ge=0, le=100)]
NonNegFloat = Annotated[float, Field(ge=0)]
NonNegInt = Annotated[int, Field(ge=0)]


class InputSpec(Model):
    """What to turn into panels: exactly one of video, images or folder."""
    video: str | None = Field(None, description="Path of a video file (mp4, mov, mkv, webm, avi...). "
                                                "Absolute or ~/...", examples=["~/Movies/clip.mp4"])
    images: list[ImageFile] | None = Field(
        None, min_length=1, max_length=MAX_IMAGES,
        description=f"Paths of image files, in panel order (1 to {MAX_IMAGES}). Formats: {SUPPORTED_IMAGES}.",
        examples=[["~/shots/001.jpg", "~/shots/002.jpg"]])
    folder: str | None = Field(None, description="A folder: every image in it (not in subfolders) becomes "
                                                 "the input, sorted by file name (img2 before img10).")
    pattern: str | None = Field(None, description='With folder: only file names matching this pattern, '
                                                  'e.g. "*.png" or "shot_*". Default: every image.')

    @field_validator("video")
    @classmethod
    def _video(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is None or not _checking(info):
            return v
        p = _abs(v)
        if p.is_dir():
            raise ValueError(f"{p} is a folder; for a folder of images use input.folder")
        if not p.is_file():
            raise ValueError(f"file not found: {p}" + _relative_hint(v))
        return str(p)

    @field_validator("folder")
    @classmethod
    def _folder(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is None or not _checking(info):
            return v
        p = _abs(v)
        if not p.is_dir():
            raise ValueError(f"folder not found: {p}" + _relative_hint(v))
        return str(p)

    @model_validator(mode="after")
    def _exactly_one(self):
        given = [k for k in ("video", "images", "folder") if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError('give exactly one of "video", "images" or "folder"'
                             + (f" (got {', '.join(given)})" if given else ""))
        if self.pattern is not None and self.folder is None:
            raise ValueError('"pattern" only works with "folder"')
        return self


class SelectSpec(Model):
    """Which frames (video) or images become panels. Default: video, the sharpest frame of each
    of 12 equal stretches; images, every image in order. Exact positions (step_pct, step_s,
    at_pct, at_s, at_frames, at_images, at_names) can be combined; when any is given, mode,
    count, every_* and the min_* filters are not used."""
    mode: _ci(Literal["auto", "sharpest", "even", "best", "all", "shots"]) = Field(
        "auto", description='"sharpest": the range is cut into count equal stretches and the least blurry '
                            'frame of each is taken (picks at least half a stretch apart). "even": the frame in '
                            'the middle of each stretch. "best": the count sharpest of the whole range, never '
                            'the same picture twice, in their order (they may all come from one part of a '
                            'video). "all": every frame / image of the range '
                            f'(at most {MAX_PANELS}). "shots" (video): one panel per shot, found by its cuts, '
                            'fades and dissolves; each panel shows the sharpest frame of its shot and the answer '
                            'gives each shot\'s start, end, duration and transitions (count is not used). "auto" '
                            '(default): "sharpest" for video; for images "all", or "sharpest" when count or '
                            'every_pct is given.')
    count: int = Field(12, ge=1, le=MAX_PANELS, description="How many panels: number of equal stretches "
                                                            '(sharpest, even) or of pictures ("best").')
    every_s: float | None = Field(None, gt=0, description="Video, instead of count: cut the range into stretches "
                                  "of N seconds and take one frame from each (the sharpest; \"even\": the middle "
                                  "one). Times come out irregular; for exactly every N seconds use step_s.")
    every_pct: float | None = Field(None, gt=0, le=100,
                                    description="Instead of count: one stretch every N % of the range.")
    step_pct: float | None = Field(None, gt=0, le=100, description="Exact positions: one panel every N % of the "
                                   "range, both ends included (25 -> 0, 25, 50, 75 and 100 %).")
    step_s: float | None = Field(None, gt=0, description="Video, exact positions: a frame exactly every N seconds "
                                 "from the start of the range (0, N, 2N...), even if it is blurred: add snap_s to "
                                 "move the blurred ones to a sharp frame nearby.")
    at_pct: list[Pct] | None = Field(None, description="Exact positions in % (0 = first frame or image, "
                                                       "100 = last).", examples=[[0, 50, 100]])
    at_s: list[NonNegFloat] | None = Field(None, description="Video, exact positions: times in seconds "
                                                             "(0.0 = first frame). Times given by a person often "
                                                             "fall on a blurred frame: add snap_s (0.5).",
                                           examples=[[1.5, 12]])
    at_frames: list[NonNegInt] | None = Field(None, description="Video, exact positions: frame numbers "
                                                                "(0 = first frame).", examples=[[0, 240]])
    at_images: list[NonNegInt] | None = Field(None, description="Images, exact positions: indexes in the "
                                                                "input (0 = first image).")
    at_names: list[str] | None = Field(None, description='Images, exact positions: file names, e.g. '
                                                         '["IMG_0042.jpg"].')
    snap_s: float = Field(0, ge=0, le=10, description="Video, exact positions (in select or in panels[]): when "
                          "the exact frame is clearly blurrier (under 80 %) than the sharpest within ± N seconds, use "
                          "the nearest frame there that is not; sharp positions stay exact (0.5 suits handheld "
                          "video). warnings lists the positions moved.")
    start_s: float | None = Field(None, ge=0, description="Video: only this part, from this second...")
    end_s: float | None = Field(None, gt=0, description="Video: ... to this second.")
    start_pct: float | None = Field(None, ge=0, le=100, description="Only this part, from this % (0 = first "
                                                                    "frame or image) ...")
    end_pct: float | None = Field(None, ge=0, le=100, description="... to this % (100 = last).")
    min_sharpness: float = Field(0, ge=0, description="Never pick a frame or image less sharp than this "
                                 "(typical values: selection.sharpness_stats of an earlier answer).")
    drop_blurriest_pct: float = Field(0, ge=0, lt=100, description="Ignore the blurriest N % of the measured "
                                      "frames or images (30 = keep the sharpest 70 %). The sharpness value this "
                                      "gives is selection.threshold. Sharpness measures edges: a flat picture "
                                      "(plain colours, a logo) scores low without being blurred.")
    min_gap_s: float = Field(0, ge=0, description="Video: drop a pick closer than N seconds to the previous one.")
    min_difference: float = Field(0, ge=0, le=255, description="Drop a pick that looks almost the same as the "
                                  "previous one kept (\"best\": as any one kept): mean difference 0-255 of small "
                                  "64×36 colour copies. 1-2 removes only near-identical pictures, 3 suits small "
                                  "movements on a fixed background, 4-8 removes repeats of a still shot. \"best\" "
                                  "uses 1 unless you set it (0 = only exact repeats).")
    analysis_width: int = Field(480, ge=64, le=4096, description="Width in px at which sharpness is measured "
                                "(bigger = finer and slower).")
    shot_threshold: float = Field(20, ge=1, le=255, description='Mode "shots": how big a jump between two '
                                  "consecutive frames counts as a cut: mean difference 0-255 of small 64×36 "
                                  "colour copies, which must also be twice the movement around it. Fades and "
                                  "dissolves need 60 % of it over half a second. Lower (10-15) finds cuts "
                                  "between similar shots; higher (30+) ignores fast movement.")
    min_shot_s: float = Field(0.4, ge=0, le=60, description='Mode "shots": shortest shot, s. A cut closer than '
                              "this to the previous one is not used (flashes, very fast editing).")
    fades: bool = Field(True, description='Mode "shots": also find gradual transitions (fades from or to a plain '
                                          "colour, usually black, and dissolves); false = only cuts.")


class Panel(Model):
    """One panel's notes (all optional). Without positions, panels[i] goes with the i-th chosen
    panel. With positions (all panels must have one), the panels themselves are the selection:
    each shows the frame or image it names, in the given order."""
    frame: int | None = Field(None, ge=0, description="Video: frame number this panel shows (select.snap_s "
                                                       "applies).")
    at_s: float | None = Field(None, ge=0, description="Video: second this panel shows (select.snap_s applies).")
    at_pct: float | None = Field(None, ge=0, le=100, description="Position this panel shows, in %.")
    image: int | None = Field(None, ge=0, description="Images: index of the image this panel shows (0 = first).")
    image_name: str | None = Field(None, description="Images: file name of the image this panel shows.")
    number: str | None = Field(None, max_length=24, description='Number to print instead of the automatic '
                                                                'one, e.g. "12A".')
    shot: str | None = Field(None, max_length=200, description='Shot size or type, e.g. "PG", "plano medio", '
                                                               '"close-up".')
    camera: str | None = Field(None, max_length=300, description='Camera movement, e.g. "paneo a la derecha".')
    action: str | None = Field(None, max_length=2000, description="What happens.")
    dialogue: str | None = Field(None, max_length=2000, description="Dialogue or voice-over.")
    sound: str | None = Field(None, max_length=1000, description="Sound effects or music.")
    transition: str | None = Field(None, max_length=200, description='Transition to the next panel, e.g. '
                                   '"corte", "fundido a negro", "encadenado". The animatic plays it: a text with '
                                   '"fund" or "fade" is a fade through black, with "encaden" or "dissolv" a '
                                   "dissolve, anything else a cut.")
    duration_s: float | None = Field(None, gt=0, le=36000, description="Planned duration, in seconds; the "
                                                                       "animatic shows the panel this long.")


LabelField = Literal["number", "frame", "time", "timecode", "pct", "sharpness", "sharpness_pct", "anchor",
                     "image", "name", "size", "shot_in", "shot_out", "duration", "transition"]
NoteField = Literal["shot", "camera", "action", "dialogue", "sound", "transition", "duration_s"]
VIDEO_LABELS = {"frame", "time", "timecode", "anchor"}
IMAGE_LABELS = {"image", "name", "size"}
SHOT_LABELS = {"shot_in", "shot_out", "duration", "transition"}
TEMPLATE_KEYS = {"number", "n", "frame", "frames_total", "time", "timecode", "pct", "pct1", "sharpness",
                 "sharpness_pct", "anchor", "image", "name", "images_total", "size", "shot_in", "shot_out",
                 "duration", "transition"}
VIDEO_KEYS = {"frame", "frames_total", "time", "timecode", "anchor"}
IMAGE_KEYS = {"image", "name", "images_total", "size"}


class LabelSpec(Model):
    """Caption of each panel: a band over the picture (grid, strip) or a line next to the panel
    number (storyboard, list)."""
    show: bool = Field(True, description="false: no caption (panel numbers in storyboard and list stay).")
    fields: list[_ci(LabelField)] | None = Field(
        None, description='What the caption says, in this order. Video: number, frame (#123), time (4.1s), '
                          'timecode (0:00:04.1), pct (20%), sharpness, sharpness_pct, anchor (first / last '
                          'frame). Images: number, image (#3), name (file name), size (4032×3024), pct, '
                          'sharpness, sharpness_pct. Select mode "shots" only: shot_in (0:00:04.1), shot_out, '
                          'duration (2.5s), transition (how the shot ends: corte, fundido a negro, '
                          'encadenado...). Default: video ["number", "time", "frame", "anchor"], mode "shots" '
                          '["number", "shot_in", "duration", "transition"], images ["number", "name"].')
    template: str | None = Field(None, max_length=200, description='Free text instead of fields, with '
                                 'placeholders: {number} {frame} {frames_total} {time} {timecode} {pct} {pct1} '
                                 '{sharpness} {sharpness_pct} {anchor} {image} {name} {images_total} {size} '
                                 '(mode "shots": {shot_in} {shot_out} {duration} {transition}). '
                                 'E.g. "{number}. {timecode}".')
    position: _ci(Literal["top", "bottom"]) = Field("top", description="grid and strip: band at the top or the "
                                                                  "bottom of the picture.")


class NumberingSpec(Model):
    """Panel numbers (panels[i].number overrides one)."""
    start: int = Field(1, ge=0, le=1_000_000, description="First number.")
    step: int = Field(1, ge=1, le=10_000, description="Increment: 10 with start 10 and format \"SH{n:03d}\" "
                                                      "gives SH010, SH020, SH030...")
    format: str = Field("{n}", max_length=40, description='{n} is the running number and accepts Python '
                        'format specs: "5.{n}" -> 5.1, 5.2...; "SH{n:03d}" -> SH001; "{n}A" -> 1A.')


class NotesSpec(Model):
    """Notes box of each panel, filled with the texts of panels[i]."""
    show: bool | None = Field(None, description="Default: true for storyboard and list; for strip, true when "
                                                "panels[] has notes; false for grid.")
    fields: list[_ci(NoteField)] = Field(["shot", "camera", "action", "dialogue", "sound", "transition", "duration_s"],
                                    description="Which panels[] texts are printed, in this order.")
    blank_lines: int = Field(3, ge=0, le=20, description="Ruled empty lines in the box of a panel without "
                                                         "notes, to write by hand (0 = no box).")
    max_lines: int = Field(8, ge=1, le=60, description='Most text lines per box; longer notes end in "…".')


class GuidesSpec(Model):
    """Lines drawn over each picture."""
    aspects: list[Aspect] = Field([], description="Frame lines for these aspect ratios: what a crop to "
                                                  "2.39:1, 9:16... would keep.", examples=[["2.39:1"]])
    shade: bool = Field(True, description="Darken what falls outside the first aspect in aspects.")
    safe_zone: bool = Field(False, description="Draw the safe area: a dashed box of safe_zone_pct of the "
                                               "picture.")
    safe_zone_pct: float = Field(90, ge=50, le=100, description="Size of the safe area, % of the picture.")
    thirds: bool = Field(False, description="Draw the rule-of-thirds grid.")


class LayoutSpec(Model):
    """How the panels are arranged on the pages."""
    template: _ci(Literal["grid", "storyboard", "list", "strip"]) = Field(
        "grid", description='"grid": contact sheet, caption band over each picture. "storyboard": panel '
                            'number above each picture and a notes box below. "list": one panel per row, '
                            'picture left, number and notes right. "strip": one row of panels with notes '
                            'below.')
    columns: int | None = Field(None, ge=1, le=24, description="Panels per row. Default: grid 4, storyboard 3, "
                                                               "list 1, strip all the panels (up to 8).")
    rows: int | None = Field(None, ge=1, le=200, description="Rows per page; more panels make more pages. "
                             'Default: all rows on one page (page "auto"), as many as fit (fixed page), '
                             "strip 1.")
    page: _ci(Literal["auto", "A4", "Letter", "16:9"]) = Field(
        "auto", description='"auto": the page grows to fit the panels. "A4", "Letter", "16:9": fixed page '
                            "size (at output.dpi); the panels are scaled to fit.")
    orientation: _ci(Literal["landscape", "portrait"]) = Field("landscape", description="Fixed pages only.")
    panel_width: int = Field(480, ge=64, le=4096, description='Width of each picture, px. With a fixed page '
                             "it is computed to fill the page.")
    aspect: Literal["source", "16:9", "9:16", "4:3", "3:4", "1:1", "4:5", "1.85:1", "2:1", "2.39:1"] = Field(
        "source", description='Shape of the picture boxes: "source" = the video\'s (or the first image\'s) '
                              "shape, or a fixed ratio.")
    fit: _ci(Literal["contain", "cover"]) = Field("contain", description='"contain": the whole picture, with bars '
                                             'if its shape differs from the box; "cover": fill the box, '
                                             "cropping the centre.")
    margin: int | None = Field(None, ge=0, le=2000, description="Space around the page content, px. Default: "
                                                                "grid 0, storyboard and list 48, strip 24.")
    gap: int | None = Field(None, ge=0, le=2000, description="Space between panels, px. Default: grid 0, "
                                                             "storyboard 28, list 24, strip 12.")
    padding: int | None = Field(None, ge=0, le=500, description="Space inside the header, footer, captions "
                                                                "and notes boxes, px. Default: from the text "
                                                                "size.")
    header: bool = Field(True, description="Band on top with a title and a subtitle.")
    title: str | None = Field(None, max_length=200, description='Header title. Default: the video\'s file '
                              'name, or the folder name / "N imágenes". "" hides it.')
    subtitle: str | None = Field(None, max_length=300, description="Header second line. Default: the "
                                 "video's size, fps, duration, frames and codec, or the images' count, "
                                 'formats and sizes. "" hides it.')
    footer: bool = Field(True, description="Band at the bottom: how the panels were chosen, and the page number "
                                           "when there are several pages.")
    footer_text: str | None = Field(None, max_length=300, description="Replaces the automatic footer text "
                                                                      "(the page number stays).")
    label: LabelSpec = Field(default_factory=LabelSpec)
    numbering: NumberingSpec = Field(default_factory=NumberingSpec)
    notes: NotesSpec = Field(default_factory=NotesSpec)
    guides: GuidesSpec = Field(default_factory=GuidesSpec)
    language: _ci(Literal["es", "en"]) = Field("es", description="Language of the texts drawn on the pages "
                                                            "(header, footer, captions, notes titles).")


def _font_check(v: str | None, info: ValidationInfo) -> str | None:
    if v is None or not _checking(info):
        return v
    return resolve_font(v)


class StyleSpec(Model):
    """Colours, fonts and sizes. Everything has a default from the theme; set only what you
    want to change. Colours: "#RRGGBB", "#RRGGBBAA" (with alpha), "#RGB", "#RGBA" or a CSS name
    ("white", "navy")."""
    theme: _ci(Literal["dark", "light"]) = Field("dark", description='"dark": black page (#000000) and white text; '
                                                                '"light": white page (#ffffff) and black text.')
    background: Color | None = Field(None, description='Page background (also the bars of "contain"). Default: '
                                     'dark #000000, light #ffffff. PNG keeps alpha: "#00000000" is transparent.')
    text: Color | None = Field(None, description="Main text: title, panel numbers. Default: dark #ffffff, "
                                                 "light #000000.")
    text_secondary: Color | None = Field(None, description="Subtitle, footer, captions in storyboard and list. "
                                                           "Default: dark #a3a3a3, light #525252.")
    accent: Color | None = Field(None, description="Notes titles (ACCIÓN, DIÁLOGO...). Default: dark #facc15, "
                                                   "light #b45309.")
    label_background: Color | None = Field(None, description="Caption band over the picture (grid, strip). "
                                           "Default: dark #000000, light #ffffff. Alpha allowed: #000000aa.")
    label_text: Color | None = Field(None, description="Caption band text. Default: dark #facc15, light #000000.")
    panel_border: Color | None = Field(None, description="Border of pictures and notes boxes. Default: dark "
                                                         "#3f3f46, light #d4d4d4.")
    border_width: int | None = Field(None, ge=0, le=40, description="Border width, px (0 = none). Default: "
                                                                     "grid 0, the others 1.")
    notes_background: Color | None = Field(None, description="Notes boxes. Default: dark #0a0a0a, light #ffffff.")
    notes_text: Color | None = Field(None, description="Notes text. Default: dark #e5e5e5, light #171717.")
    notes_rule: Color | None = Field(None, description="Ruled lines of empty notes boxes. Default: dark #2e2e2e, "
                                                       "light #e5e5e5.")
    guides: Color | None = Field(None, description="Guide lines (aspects, safe area, thirds). Default: dark "
                                                   "#22d3ee, light #0891b2.")
    font: str | None = Field(None, description='Font file (.ttf, .otf, .ttc) or installed font name ("Helvetica", '
                             '"Arial", "DejaVu Sans"). Default: Arial, Helvetica or DejaVu Sans, whichever exists.')
    font_bold: str | None = Field(None, description="Bold font, same rules. Default: the bold version of font.")
    title_size: int | None = Field(None, ge=6, le=400, description="Header title, px. Default: from the page width.")
    text_size: int | None = Field(None, ge=6, le=400, description="Subtitle and footer, px. Default: from the "
                                                                  "page width.")
    label_size: int | None = Field(None, ge=6, le=400, description="Captions, px. Default: picture width / 22.")
    number_size: int | None = Field(None, ge=6, le=400, description="Panel numbers (storyboard, list), px. "
                                                                    "Default: 1.45 × label_size.")
    notes_size: int | None = Field(None, ge=6, le=400, description="Notes text, px. Default: 0.92 × label_size.")
    sharpen: float = Field(0.5, ge=0, le=2, description="Sharpening after shrinking each picture (0 = none).")

    @field_validator("font", "font_bold")
    @classmethod
    def _fonts(cls, v: str | None, info: ValidationInfo) -> str | None:
        return _font_check(v, info)


def _safe_name(v: str | None) -> str | None:
    if v is not None and (not v.strip() or re.search(r'[/\\:*?"<>|]', v)):
        raise ValueError(f'"{v}" is not a valid file name (no folders and none of / \\ : * ? " < > |)')
    return v


FileName = Annotated[str, AfterValidator(_safe_name)]


class AnimationSpec(Model):
    """Animated output: used when output.format is mp4, webm, gif or webp. The still pages (PNG), an
    image of sample frames and the JSON are always written too: they are what an assistant can look at.
    An animatic shows one card at a time: the panel drawn as in layout.template (header, number and
    caption, picture, its notes whenever panels[] has notes, with any template, and footer), as big as
    max_side allows; with layout.page "16:9" (or "A4", "Letter") the card, and the video, have that
    shape (the panel is scaled to fit with its caption and notes). A clean 16:9 slideshow, only the
    pictures: layout {"page": "16:9", "header": false, "footer": false, "label": {"show": false}} and
    animation {"progress": false}; add layout "aspect": "16:9" and "fit": "cover" to fill the frame
    without bands (cropping pictures of another shape)."""
    kind: _ci(Literal["auto", "mosaic", "animatic"]) = Field(
        "auto", description='"mosaic" (video input): the page itself, with every panel playing its part of the '
                            "video in a loop, all at once, and a progress bar under each panel. \"animatic\": the "
                            "panels one after another as still cards, each on screen for its duration, with its "
                            "number, caption and notes, and a cut, fade or dissolve between them; with images, a "
                            'timed slideshow. "auto" (default): mosaic for video, animatic for images.')
    fps: int = Field(12, ge=1, le=30, description="Frames per second of the animation.")
    duration_s: float | None = Field(
        None, gt=0, le=MAX_VIDEO_S, description="Length of the video, s. Mosaic: the loop (default: the longest "
                                                "part a panel plays, at most 8 s); a longer part plays faster, a "
                                                "shorter one starts over. Animatic: the total (default: the sum of "
                                                "the panel durations); if given, every duration and transition is "
                                                "scaled to fit (videos[].scale; the captions keep the shots' own "
                                                f"durations). At most {MAX_VIDEO_S} s and {MAX_VIDEO_FRAMES} frames "
                                                "(duration_s × fps). Smaller files: a lower max_side, fps or "
                                                "duration_s (quality does not change a gif).")
    panel_s: float = Field(2.0, ge=0.1, le=60, description="Animatic: seconds on screen for a panel without a "
                           'duration of its own (panels[].duration_s, or the shot length in select mode "shots").')
    transition: _ci(Literal["cut", "fade", "dissolve"]) = Field(
        "cut", description='Animatic: transition out of a panel (into the next one) when it has none of its own: '
                           '"cut", "fade" (through black) or "dissolve" (mixes into the next). panels[].transition, '
                           'and in select mode "shots" the detected transitions, come first. After the last panel '
                           "a fade or dissolve fades to black, but in gif and webp a dissolve goes back into the "
                           "first panel, so the loop has no jump.")
    fade_s: float = Field(0.5, gt=0, le=5, description="Animatic: length of each fade or dissolve, s (never more "
                                                       "than half a panel). Transitions do not lengthen the video: "
                                                       "a dissolve is centred on the change and a fade takes the end "
                                                       "of one panel and the start of the next, so the total is the "
                                                       'sum of the durations. In select mode "shots" the detected '
                                                       "lengths are used instead.")
    max_side: int | None = Field(None, ge=64, le=3840, description="Longest side of the video, px; the page or "
                                 "panel is scaled down to fit. Default: 1920 for mp4 and webm, 960 for gif and "
                                 "webp (gif and webp: at most 1280). For a light file: 640 (with fps 8, a 6-panel "
                                 "mosaic of 3.3 s is about 1.6 MB as gif and 0.4 MB as webp with quality 60).")
    progress: bool = Field(True, description="Progress bars: one under each panel showing where its part is "
                                             "(mosaic), or one along the bottom with a mark per panel (animatic).")


class StoryboardOutput(Model):
    """Where and how the pages are written."""
    dir: str | None = Field(None, description="Folder for every file (created if missing). Default: "
                                              "<system temp>/storyboard-mcp/<input name>/. The answer has "
                                              "the full paths.")
    name: FileName | None = Field(None, max_length=120, description="Base file name, no extension. Default: "
                                  "<input name>_<template>. Several pages add _p1, _p2...")
    format: OutFormat = Field("png", description='"png" (lossless; keeps a transparent background), "jpg", '
                              '"pdf" (one file, one PDF page per page), or animated: "mp4" (H.264), "webm" (VP9), '
                              '"gif" or "webp" (both loop forever), set up in output.animation; with these the '
                              "pages are also written as PNG. Any case.")
    animation: AnimationSpec = Field(default_factory=AnimationSpec)
    quality: int = Field(90, ge=1, le=100, description="JPEG quality (jpg, and the pictures inside a pdf); also the "
                                                       "quality of mp4, webm and webp (higher = better and bigger).")
    dpi: int = Field(150, ge=50, le=600, description="Resolution of fixed pages (A4, Letter, 16:9) and of the pdf.")
    shot_list: list[_ci(Literal["json", "csv"])] = Field([], description="Also write the shot list, one row per "
                                                         "panel with its position and notes: <name>_shots.json "
                                                         "and/or <name>_shots.csv (UTF-8 with BOM).")
    csv_style: _ci(Literal["standard", "excel_es"]) = Field(
        "standard", description='"standard": comma separator, dot decimals, English column names, every column. '
                                '"excel_es": for Excel in Spanish (and other languages with decimal commas): '
                                "semicolon separator, decimal commas, Spanish column names and transitions (the "
                                "words of the captions: corte, encadenado, fundido a negro...), no always-empty columns, and panel numbers kept as text (3.1 is not "
                                "turned into a date). Columns: n, number, page, frame, time_s, timecode, pct (the "
                                "panel's frame), shot_in_s, shot_out_s, shot_first_frame, shot_last_frame, "
                                "transition_in(_s), transition_out(_s) (mode \"shots\"), image, name, the notes, "
                                "sharpness, colors, file, size, source. duration_s: the planned one, or the shot's.")
    frames: bool = Field(False, description="Also save each panel's picture at full size in <dir>/<name>_frames/ "
                                            "(video frames as JPEG, images copied unchanged).")
    also_without_labels: bool = Field(False, description="Also write the pages without captions "
                                                         "(<name>_nolabels...).")
    color_script: bool = Field(False, description="Also write the colour script: <name>_colors.png, a strip with "
                               "the dominant colours of each panel's picture (one frame per panel), in order, with "
                               "the picture above, the hex and % of each colour and the panel's time or name when "
                               "they fit; and add colors (hex and % of the picture) to each panel in the JSON. The "
                               "answer's preview includes the strip.")
    color_count: int = Field(5, ge=1, le=12, description="Colour script: dominant colours per panel.")
    preview: bool = Field(True, description="Include a small image of page 1 in the answer (and, with an animated "
                                            "format, of the sample frames).")


class FramesOutput(Model):
    """Where and how the frames or images are written."""
    dir: str | None = Field(None, description="Folder (created if missing). Default: <system temp>/"
                                              "storyboard-mcp/<input name>/frames/.")
    format: _ci(Literal["jpg", "png", "original"]) | None = Field(
        None, description='Video frames: "jpg" (default) or "png". Images: "original" (default: the file is '
                          'copied byte for byte) or converted to "jpg" / "png".')
    names: _ci(Literal["numbered", "original"]) = Field(
        "numbered", description='Images: "numbered" (default: 001_<name>, 002_<name>... in panel order) or '
                                '"original" (the original file name; a repeated name gets _2, _3...). Video '
                                "frames are always f<frame, 5 digits>.<ext>.")
    quality: int = Field(95, ge=1, le=100, description="JPEG quality.")


class MediaInfoArgs(Model):
    input: InputSpec


class StoryboardArgs(Model):
    input: InputSpec
    select: SelectSpec = Field(default_factory=SelectSpec)
    panels: list[Panel] = Field([], max_length=MAX_PANELS, description="Notes per panel, in panel order (or "
                                "with positions, to choose the panels yourself).")
    layout: LayoutSpec = Field(default_factory=LayoutSpec)
    style: StyleSpec = Field(default_factory=StyleSpec)
    output: StoryboardOutput = Field(default_factory=StoryboardOutput)


class FramesArgs(Model):
    input: InputSpec
    select: SelectSpec = Field(default_factory=SelectSpec)
    output: FramesOutput = Field(default_factory=FramesOutput)


class SchemaArgs(Model):
    tool: Literal["media_info", "make_storyboard", "extract_frames"] | None = Field(
        None, description="Only this tool. Default: every tool.")


# ---------------------------------------------------------------------------- answers
class VideoInfo(Model):
    path: str = Field(description="Absolute path of the video.")
    duration_s: float | None = Field(None, description="Length in seconds.")
    fps: float | None = Field(None, description="Frames per second.")
    frames: int | None = Field(None, description="Number of frames; frame numbers go from 0 to frames - 1.")
    size: list[int] = Field(description="[width, height] as stored in the file.")
    rotation_deg: int = Field(0, description="0, 90, 180 or 270: clockwise turn applied to every picture "
                                             "(phones film sideways).")
    displayed_size: list[int] = Field(description="[width, height] after that turn: size of the full frames.")
    codec: str | None = None


class ImageInfo(Model):
    index: int = Field(description="Position in the input, 0 = first.")
    path: str
    name: str
    format: str | None = Field(None, description='"JPEG", "PNG", "HEIF"...')
    size: list[int] = Field(description="[width, height] as stored.")
    displayed_size: list[int] = Field(description="[width, height] after the EXIF orientation.")
    bytes: int
    duplicate_of: int | None = Field(None, description="Index of an earlier image with exactly the same bytes.")


class MediaInfoResult(Model):
    kind: Literal["video", "images"]
    video: VideoInfo | None = None
    count: int | None = Field(None, description="Images: how many.")
    folder: str | None = None
    formats: dict[str, int] | None = Field(None, description='Images: count per format, e.g. {"JPEG": 10}.')
    sizes: list[list[int]] | None = Field(None, description="Images: the different displayed sizes "
                                                            "[width, height], most common first.")
    images: list[ImageInfo] | None = None


class Dropped(Model):
    frame: int | None = None
    image: int | None = None
    name: str | None = None
    reason: str = Field(description='"min_gap_s", "min_difference" or "min_sharpness" (mode "all"). Mode '
                                    '"best" lists only the repeats it skipped before reaching count, not the '
                                    'pictures below the threshold: use mode "all" to list those.')
    similar_to: int | None = Field(None, description="min_difference / min_gap_s: frame or image index of the "
                                                     "kept panel it was compared with.")
    difference: float | None = Field(None, description="min_difference: the measured difference, 0-255.")


class ColorShare(Model):
    hex: str = Field(description='Colour, "#rrggbb".')
    pct: float = Field(description="Share of the picture with this colour, %.")


class PanelResult(Model):
    n: int = Field(description="Order of the panel, 1 = first.")
    number: str = Field(description="Number printed on the panel.")
    page: int | None = Field(None, description="Page where it is.")
    frame: int | None = Field(None, description="Video: frame number.")
    time_s: float | None = Field(None, description="Video: seconds from the first frame.")
    timecode: str | None = Field(None, description="Video: h:mm:ss.s")
    pct: float | None = Field(None, description="Position, 0 = first frame or image, 100 = last.")
    image: int | None = Field(None, description="Images: index in the input.")
    name: str | None = Field(None, description="Images: file name.")
    source: str | None = Field(None, description="Images: path of the original image.")
    sharpness: float | None = Field(None, description="Higher = sharper (variance of the edges).")
    sharpness_pct: int | None = Field(None, description="Sharpness as a % of the sharpest panel of this answer "
                                                        "(not a percentile).")
    anchor: str | None = Field(None, description='"first_frame" / "last_frame" on the first / last frame '
                                                 "of the video.")
    file: str | None = Field(None, description="Full-size frame or image copy (output.frames / extract_frames), "
                                               "upright (rotation already applied).")
    size: list[int] | None = Field(None, description="[width, height] of file, px.")
    notes: dict[str, Any] | None = Field(None, description="The panel's notes, as given in panels[].")
    shot_in_s: float | None = Field(None, description='Select mode "shots": when the shot starts (its first '
                                                      "frame), s. At a dissolve the change is put in its middle; "
                                                      "plain (black) frames between two shots: in their middle "
                                                      "after a fade, at the cut otherwise.")
    shot_out_s: float | None = Field(None, description='Mode "shots": when it ends: the next shot\'s first frame, '
                                                       "or the end of the range, s.")
    duration_s: float | None = Field(None, description='Mode "shots": shot_out_s - shot_in_s, s.')
    shot_frames: list[int] | None = Field(None, description='Mode "shots": [first, last] frame of the shot.')
    transition_in: Literal["start", "cut", "fade_in", "dissolve"] | None = Field(
        None, description='Mode "shots": how the shot begins: "start" (the range starts there), "cut", "fade_in" '
                          '(from a plain colour, usually black) or "dissolve" (mixed with the shot before).')
    transition_out: Literal["cut", "fade_out", "dissolve", "end"] | None = Field(
        None, description='Mode "shots": how it ends: "cut", "fade_out" (to a plain colour, usually black), '
                          '"dissolve" (mixed with the next shot) or "end" (the range ends there).')
    transition_in_s: float | None = Field(None, description="Length of a gradual transition_in (fade or dissolve), s.")
    transition_out_s: float | None = Field(None, description="Length of a gradual transition_out, s.")
    colors: list[ColorShare] | None = Field(None, description="output.color_script: dominant colours of the "
                                                              "picture, most common first.")
    clip_s: list[float] | None = Field(None, description="Animated mosaic: [start, end] of the part of the video "
                                                         "this panel plays, s.")
    speed: float | None = Field(None, description="Animated mosaic: playback speed of that part (1 = real time, "
                                                  "4 = four times faster).")
    video_s: list[float] | None = Field(None, description="Animatic: [start, end] when this panel is on screen in "
                                                          "the video, s.")
    video_transition: Literal["cut", "fade", "dissolve"] | None = Field(
        None, description="Animatic: how this panel goes into the next one (the last one: how the video ends; a "
                          "dissolve there goes back into the first panel).")
    video_transition_s: float | None = Field(None, description="Animatic: length of that fade or dissolve, s.")


class PageResult(Model):
    page: int
    path: str | None = Field(None, description="Image of the page; with format pdf, the PDF file (this is page "
                                               "number `page` of it).")
    size: list[int] = Field(description="[width, height] px.")
    panels: list[int] = Field(description="n of the panels on this page.")


class ShotsInfo(Model):
    found: int = Field(description="Shots found in the range.")
    cuts: int = Field(description="Transitions between shots that are cuts.")
    fades: int = Field(description="... that go through a plain colour (fade out, then cut or fade in).")
    dissolves: int = Field(description="... that are dissolves.")
    threshold: float = Field(description="select.shot_threshold used.")
    min_shot_s: float = Field(description="select.min_shot_s used.")


class SelectionResult(Model):
    mode: str = Field(description='"sharpest", "even", "best", "all", "shots", "positions" or "panels".')
    range_s: list[float] | None = Field(None, description="Video: [start, end] in seconds.")
    range_pct: list[float]
    measured: int = Field(description="Frames or images whose sharpness was measured (only these values are "
                                      "comparable with each other).")
    threshold: float = Field(description="Frames or images below this sharpness were never picked.")
    sharpness_stats: dict[str, float] | None = Field(None, description="min, p25, median, p75, max of the "
                                                                       "measured ones (nearest measured value, "
                                                                       "not interpolated): use it for min_sharpness.")
    shots: ShotsInfo | None = Field(None, description='Mode "shots": what was found.')


class VideoResult(Model):
    kind: Literal["mosaic", "animatic"]
    page: int | None = Field(None, description="Mosaic: the page it animates.")
    path: str = Field(description="The video or animated image.")
    format: str = Field(description='"mp4", "webm", "gif" or "webp".')
    codec: str = Field(description='"h264", "vp9", "gif", "webp"...')
    size: list[int] = Field(description="[width, height] px.")
    fps: int
    frames: int
    duration_s: float
    bytes: int
    loop: bool = Field(description="true: it plays forever (gif, webp); mp4 and webm play once unless the player "
                                   "loops them.")
    samples: str = Field(description="PNG with sample frames of the video and their times: open it to see what the "
                                     "video looks like.")
    sample_times_s: list[float] = Field(description="Times of those frames in the video, s (animatic: the middle "
                                                    "of every panel and of every transition).")
    scale: float | None = Field(None, description="Animatic: factor applied to every duration and transition to "
                                                  "fit animation.duration_s (1 = as given).")


class StoryboardResult(Model):
    pages: list[PageResult]
    pdf: str | None = None
    pages_without_labels: list[str] | None = None
    shot_list: dict[str, str] | None = Field(None, description='{"json": path, "csv": path}')
    frames_dir: str | None = None
    videos: list[VideoResult] | None = Field(None, description="Animated output (output.format mp4, webm, gif or "
                                                               "webp): one video, or one per page (mosaic).")
    color_script: str | None = Field(None, description="output.color_script: the PNG strip of dominant colours.")
    panels: list[PanelResult]
    dropped: list[Dropped]
    selection: SelectionResult
    layout: dict[str, Any] = Field(description="Template, columns, rows per page, picture size and page size used.")
    input: MediaInfoResult
    warnings: list[str] = []


class FramesResult(Model):
    dir: str
    files: list[PanelResult]
    dropped: list[Dropped]
    selection: SelectionResult
    input: MediaInfoResult
    warnings: list[str] = []


# ============================================================================ errors
class ToolError(Exception):
    """A problem the caller can fix; the message says what and how."""


_UNION_TAGS = {"int", "str", "float", "bool", "list", "dict", "none"}


def _loc_str(loc) -> str:
    out = ""
    for p in loc:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += ("." if out else "") + str(p)
    return out or "(arguments)"


def _inner_model(ann):
    origin = typing.get_origin(ann)
    if origin is Annotated:
        return _inner_model(typing.get_args(ann)[0])
    if origin in (list, typing.Union, pytypes.UnionType):
        for a in typing.get_args(ann):
            m = _inner_model(a)
            if m:
                return m
        return None
    return ann if isinstance(ann, type) and issubclass(ann, BaseModel) else None


def _model_at(model, loc):
    cur = model
    for part in loc:
        if isinstance(part, int):
            continue
        f = cur.model_fields.get(part) if cur else None
        if f is None:
            return None
        cur = _inner_model(f.annotation)
    return cur


def _got(v) -> str:
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        s = repr(v)
    return f" (got {s[:120]}{'…' if len(s) > 120 else ''})"


def _problems_text(tool: str, problems: list[str]) -> str:
    head = f"{tool}: {len(problems)} problem{'s' if len(problems) != 1 else ''} in the arguments:"
    tail = f'What each field accepts: call get_schema with {{"tool": "{tool}"}}.' if tool != "get_schema" else ""
    return "\n".join([head, *[f"- {p}" for p in problems], tail]).strip()


OBJECT_HINTS = {"InputSpec": '{"video": "/abs/clip.mp4"}', "SelectSpec": '{"count": 8}',
                "LayoutSpec": '{"template": "storyboard"}', "StyleSpec": '{"theme": "light"}',
                "StoryboardOutput": '{"dir": "/abs/folder", "format": "pdf"}', "FramesOutput": '{"dir": "/abs/folder"}',
                "Panel": '{"shot": "PG", "action": "..."}', "LabelSpec": '{"fields": ["number", "time"]}',
                "AnimationSpec": '{"kind": "animatic", "fps": 12}',
                "NumberingSpec": '{"format": "5.{n}"}', "NotesSpec": '{"fields": ["action", "dialogue"]}',
                "GuidesSpec": '{"aspects": ["2.39:1"], "thirds": true}'}


def format_validation_error(tool: str, model: type[BaseModel], err: ValidationError) -> str:
    problems, seen = [], set()
    for e in err.errors(include_url=False):
        loc = tuple(p for p in e["loc"] if not (isinstance(p, str) and ("[" in p or p in _UNION_TAGS)))
        path, typ = _loc_str(loc), e["type"]
        if typ == "extra_forbidden":
            parent = _model_at(model, loc[:-1])
            name = str(loc[-1])
            allowed = list(parent.model_fields) if parent else []
            close = difflib.get_close_matches(name, allowed, n=1, cutoff=0.6)
            msg = f'unknown field "{name}".' + (f' Did you mean "{close[0]}"?' if close else "") + \
                  (f" Allowed here: {', '.join(allowed)}." if allowed else "")
        elif typ == "missing":
            msg = "required, but missing."
        elif typ in ("model_type", "model_attributes_type", "dict_type"):
            sub = _model_at(model, loc)
            fields = ", ".join(sub.model_fields) if sub else ""
            hint = OBJECT_HINTS.get(sub.__name__, "") if sub else ""
            msg = "expected an object" + (f" like {hint}" if hint else "") + \
                  (f" (fields: {fields})" if fields else "") + _got(e.get("input"))
        elif typ == "list_type":
            msg = "expected a list, e.g. [" + json.dumps(e.get("input"), ensure_ascii=False) + "]" + _got(e.get("input"))
        elif typ in ("value_error", "assertion_error"):
            msg = re.sub(r"^(Value error|Assertion failed), ", "", e["msg"])
        else:
            msg = e["msg"] + _got(e.get("input"))
        if (path, msg) not in seen:
            seen.add((path, msg))
            problems.append(f"{path}: {msg}")
    return _problems_text(tool, problems)


# ============================================================================ fonts
FONT_DIRS = [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts",
             Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts",
             Path.home() / ".local/share/fonts", Path("C:/Windows/Fonts")]
DEFAULT_FONTS = [("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
                 ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                 ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
                 ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf")]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


@lru_cache(maxsize=1)
def _font_index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for d in FONT_DIRS:
        if d.is_dir():
            for p in sorted(d.rglob("*")):
                if p.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    idx.setdefault(_norm(p.stem), str(p))
    return idx


def resolve_font(spec: str) -> str:
    """Path of a font given as a file or as an installed font's name."""
    p = Path(spec).expanduser()
    if p.is_file():
        try:
            ImageFont.truetype(str(p), 12)
            return str(p)
        except OSError:
            raise ValueError(f'"{spec}" is not a font file this server can read (.ttf, .otf, .ttc)')
    hit = _font_index().get(_norm(spec))
    if hit:
        return hit
    close = difflib.get_close_matches(_norm(spec), list(_font_index()), n=3, cutoff=0.6)
    raise ValueError(f'font "{spec}" not found; give the path of a .ttf, .otf or .ttc file or the name of an '
                     "installed font" + (f" (similar: {', '.join(Path(_font_index()[c]).stem for c in close)})"
                                         if close else ""))


def _bold_of(path: str) -> str | None:
    stem = Path(path).stem
    for cand in (re.sub(r"(?i)regular|book|roman", "Bold", stem), stem + " Bold", stem + "-Bold", stem + "bd"):
        if cand != stem:
            hit = _font_index().get(_norm(cand))
            if hit:
                return hit
    return None


@lru_cache(maxsize=256)
def _ft(path: str | None, size: int):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


# ============================================================================ media
def _tenths(t: float) -> float:
    """Seconds rounded to tenths, as every caption shows them (time_s in the JSON keeps 3 decimals)."""
    return round(t + 1e-9, 1)


def _hms(t: float) -> str:
    t = _tenths(t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:04.1f}"


def _rotation(st) -> int:
    """Clockwise degrees to turn the decoded pictures so they look the way they were
    filmed. Phones film sideways and store that turn in the file, as a display matrix
    (current FFmpeg) or as an old "rotate" tag."""
    try:
        r = st.metadata.get("rotate") if st.metadata else None
        if r:
            return int(r) % 360
    except Exception:
        pass
    try:  # display matrix (PyAV >= 14): int32 3x3 matrix in 16.16 fixed point, read like
        # FFmpeg's av_display_rotation_get(), which gives the counter-clockwise angle
        m = (st.codec_context.coded_side_data or {}).get("display_matrix")
        if m and len(m) >= 20:
            a, b, _, c, d = (v / 65536 for v in struct.unpack("=5i", m[:20]))
            ccw = -math.degrees(math.atan2(b / (math.hypot(b, d) or 1), a / (math.hypot(a, c) or 1)))
            return round(-ccw) % 360
    except Exception:
        pass
    for sd in getattr(st, "side_data", []) or []:  # display matrix (older PyAV)
        rot = getattr(sd, "rotation", None)
        if rot is not None:
            return int(-rot) % 360
    return 0


def _offset(st) -> float:
    """Time of the stream start (containers often start at a small non-zero pts):
    every time reported is measured from it, so the first frame is 0.0 s."""
    try:
        return float(st.start_time * st.time_base) if st.start_time is not None and st.time_base else 0.0
    except Exception:
        return 0.0


def _sharpness(im: Image.Image, analysis_width: int) -> float:
    g = im.convert("L")
    if g.width > analysis_width:
        g = g.resize((analysis_width, max(1, round(g.height * analysis_width / g.width))))
    return ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0]


def _tiny(im: Image.Image) -> Image.Image:
    return im.convert("RGB").resize((64, 36), Image.BILINEAR)


def _difference(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute difference, 0-255, of two tiny colour copies."""
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def _percentile(values: list[float], pct: float) -> float:
    v = sorted(values)
    if not v:
        return 0.0
    k = max(0, min(len(v) - 1, round(pct / 100 * (len(v) - 1))))
    return v[k]


def _natural(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


class VideoSource:
    kind = "video"

    def __init__(self, path: str):
        self.path = path
        self.name = Path(path).stem
        c, st = self._open()
        try:
            fps = float(st.average_rate) if st.average_rate else None
            dur = float(st.duration * st.time_base) if st.duration else (c.duration / 1e6 if c.duration else None)
            n = st.frames or (round(dur * fps) if dur and fps else None)
            self.rot = _rotation(st)
            w, h = st.codec_context.width, st.codec_context.height
            self.info = VideoInfo(path=path, duration_s=round(dur, 3) if dur else None, fps=fps, frames=n,
                                  size=[w, h], rotation_deg=self.rot,
                                  displayed_size=[h, w] if self.rot in (90, 270) else [w, h],
                                  codec=st.codec_context.name)
        finally:
            c.close()
        self.fps = fps or 30.0
        self.frames = n
        self.duration = dur or ((n or 1) / self.fps)
        self.span = (n - 1) / self.fps if n else self.duration      # time of the last frame
        self.count = n or 0
        self.aspect = self.info.displayed_size[0] / max(1, self.info.displayed_size[1])
        self.measured: list[float] = []          # every sharpness measured in this call (one scale)

    def _open(self):
        try:
            c = av.open(self.path)
        except Exception as e:
            raise ToolError(f"input.video: cannot open {self.path} as a video ({e})")
        if not c.streams.video:
            c.close()
            raise ToolError(f"input.video: {self.path} has no video stream")
        st = c.streams.video[0]
        try:
            st.thread_type = "AUTO"                # decode on several cores
        except Exception:
            pass
        return c, st

    def result(self, with_items: bool = True) -> MediaInfoResult:
        return MediaInfoResult(kind="video", video=self.info)

    def _image(self, fr, width: int | None = None) -> Image.Image:
        if width and fr.width > width:
            im = fr.reformat(width=width, height=max(2, round(fr.height * width / fr.width)),
                             format="rgb24").to_image()
        else:
            im = fr.to_image()
        return im.rotate(-self.rot, expand=True) if self.rot else im

    def decode(self, start_s: float = 0.0, end_s: float | None = None):
        """(index, time_s, frame) for the frames in [start_s, end_s], in order."""
        c, st = self._open()
        tb = float(st.time_base) if st.time_base else 0.0
        off = _offset(st)
        try:
            for i, fr in enumerate(c.decode(st)):
                t = float(fr.pts * tb) - off if fr.pts is not None else i / self.fps
                if t < start_s - 1e-6:
                    continue
                if end_s is not None and t > end_s + 1e-6:
                    break
                yield i, t, fr
        finally:
            c.close()

    def around(self, t: float, window_s: float):
        """Seek near t and yield (index, time_s, frame) for the frames in [t - window, t + window].
        Frame index is estimated from the time (seeking does not give it)."""
        c, st = self._open()
        tb = float(st.time_base) if st.time_base else 0.0
        off = _offset(st)
        lo, hi = max(0.0, t - window_s), t + max(window_s, 0.5 / self.fps)
        try:
            if tb:
                c.seek(int((max(0.0, lo - 1.0) + off) / tb), stream=st, backward=True, any_frame=False)
            last, given = None, False
            for fr in c.decode(st):
                ft = float(fr.pts * tb) - off if fr.pts is not None else 0.0
                if ft < lo - 0.5 / self.fps:
                    last = (ft, fr)
                    continue
                if ft > hi:
                    if not given:                 # nothing inside the window: give the next frame
                        yield round(ft * self.fps), ft, fr
                        given = True
                    break
                yield round(ft * self.fps), ft, fr
                given, last = True, None
            if not given and last is not None:    # t beyond the last frame: give the last one
                yield round(last[0] * self.fps), last[0], last[1]
        finally:
            c.close()

    def near(self, t: float, snap_s: float, analysis_width: int, probe_s: float = 0.0):
        """The frame at t, or, when it is clearly blurrier (under 80 %) than the sharpest within
        ± snap_s, the nearest one that is not: (index, time, sharpness, full image, local best).
        probe_s > 0 (without snap_s) also measures the frames within ± probe_s, only to give the
        sharpness of the sharpest of them as local best (else None)."""
        last_i = (self.frames - 1) if self.frames else None
        if snap_s <= 0 and t <= 0:
            for i, ft, fr in self.decode():
                return 0, 0.0, self.sharpness(fr, analysis_width), self._image(fr), None
        if snap_s <= 0 and last_i is not None and t >= self.span - 0.5 / self.fps:
            found = None
            for i, ft, fr in self.around(self.span, 1.0 / self.fps):
                found = fr
            if found is not None:
                return last_i, self.span, self.sharpness(found, analysis_width), self._image(found), None
        probe = snap_s <= 0 < probe_s
        cands = []
        for i, ft, fr in self.around(t, snap_s if snap_s > 0 else probe_s):
            if snap_s > 0:
                sc = self.sharpness(fr, analysis_width)
            elif probe:
                sc = _sharpness(self._image(fr, analysis_width), analysis_width)   # not a candidate: not counted
            else:
                sc = 0.0
            cands.append((i, ft, sc, fr))
        if not cands:
            raise ToolError(f"no frame found near {t:.2f} s")
        exact = min(cands, key=lambda c: abs(c[1] - t))
        best = max(c[2] for c in cands)
        if snap_s > 0 and exact[2] < 0.8 * best:      # the nearest frame that is sharp enough
            i, ft, sc, fr = min((c for c in cands if c[2] >= 0.8 * best), key=lambda c: abs(c[1] - t))
        else:
            i, ft, sc, fr = exact
        if snap_s <= 0:
            sc = self.sharpness(fr, analysis_width)
        if last_i is not None:
            i = min(i, last_i)
        return i, max(0.0, ft), sc, self._image(fr), (best if probe else None)

    def sharpness(self, fr, analysis_width: int) -> float:
        sc = _sharpness(self._image(fr, analysis_width), analysis_width)
        self.measured.append(sc)
        return sc

    def measure(self, t0: float, t1: float, analysis_width: int, tiny: bool):
        """Every frame in [t0, t1]: (index, time, sharpness, tiny grey copy or None)."""
        out = []
        for i, t, fr in self.decode(t0, t1):
            im = self._image(fr, analysis_width)
            sc = _sharpness(im, analysis_width)
            self.measured.append(sc)
            out.append((i, t, sc, _tiny(im) if tiny else None))
        return out

    def scan(self, t0: float, t1: float, analysis_width: int, scan: "ShotScan"):
        """Like measure (without keeping the tiny copies), feeding every frame to the shot scan."""
        out = []
        for i, t, fr in self.decode(t0, t1):
            im = self._image(fr, analysis_width)
            sc = _sharpness(im, analysis_width)
            self.measured.append(sc)
            out.append((i, t, sc, None))
            scan.add(_tiny(im))
        return out

    def frames_at(self, times: list[float], width: int | None):
        """(t, picture) of the frame nearest to each time (sorted, no repeats), decoding forward and
        seeking again over gaps of more than 2 s."""
        if not times:
            return
        c, st = self._open()
        tb = float(st.time_base) if st.time_base else 0.0
        off = _offset(st)
        try:
            k = 0
            while k < len(times):
                seek_t = max(0.0, times[k] - 1.0)
                if tb:
                    c.seek(int((seek_t + off) / tb), stream=st, backward=True, any_frame=False)
                prev = None
                for fr in c.decode(st):
                    ft = float(fr.pts * tb) - off if fr.pts is not None else 0.0
                    while k < len(times) and ft >= times[k] - 1e-6:
                        near = fr if prev is None or abs(ft - times[k]) <= abs(prev[0] - times[k]) else prev[1]
                        yield times[k], self._image(near, width)
                        k += 1
                    if k >= len(times):
                        return
                    prev = (ft, fr)
                    if tb and ft >= seek_t and times[k] - ft > 2.0:
                        break                     # far ahead: seek instead of decoding everything between
                else:                             # end of the video: the rest get the last frame
                    if prev is None:
                        raise ToolError(f"no frame found near {times[k]:.2f} s")
                    while k < len(times):
                        yield times[k], self._image(prev[1], width)
                        k += 1
        finally:
            c.close()

    def fetch(self, picks, sink):
        """Full-size image of each pick, given to sink(pick, image)."""
        ready = [p for p in picks if p.image is not None]
        for p in ready:
            sink(p, p.image)
            p.image = None
        rest = [p for p in picks if p not in ready]
        if not rest:
            return
        if len(rest) > 24 and all(p.exact_index for p in rest):   # many picks: one pass, not many seeks
            want = {p.key: p for p in rest}
            last = max(want)
            for i, t, fr in self.decode(max(0.0, min(p.t for p in rest) - 0.5)):
                if i in want:
                    sink(want[i], self._image(fr))
                if i >= last:
                    break
            return
        for p in rest:
            im = self.near(p.t, 0, 64)[3]
            sink(p, im)


class ImageSource:
    kind = "images"

    def __init__(self, paths: list[str], name: str, folder: str | None = None):
        self.paths = [Path(p) for p in paths]
        self.name = name
        self.folder = folder
        self.count = len(paths)
        self._info: list[ImageInfo] | None = None
        self.measured: list[float] = []
        first = self.infos()[0].displayed_size
        self.aspect = first[0] / max(1, first[1])

    def infos(self) -> list[ImageInfo]:
        if self._info is None:
            out = []
            for i, p in enumerate(self.paths):
                try:
                    with Image.open(p) as im:
                        w, h = im.size
                        orient = im.getexif().get(0x0112, 1)
                        fmt = im.format
                except Exception as e:
                    raise ToolError(f"input: cannot read image {p} ({e})")
                out.append(ImageInfo(index=i, path=str(p), name=p.name, format=fmt, size=[w, h],
                                     displayed_size=[h, w] if orient in (5, 6, 7, 8) else [w, h],
                                     bytes=p.stat().st_size))
            by_size: dict[int, list[ImageInfo]] = {}          # identical files: hash only same-size ones
            for x in out:
                by_size.setdefault(x.bytes, []).append(x)
            for group in (g for g in by_size.values() if len(g) > 1):
                seen: dict[str, int] = {}
                for x in group:
                    digest = hashlib.sha1(Path(x.path).read_bytes()).hexdigest()
                    if digest in seen:
                        x.duplicate_of = seen[digest]
                    else:
                        seen[digest] = x.index
            self._info = out
        return self._info

    def result(self, with_items: bool = True) -> MediaInfoResult:
        infos = self.infos()
        formats: dict[str, int] = {}
        for x in infos:
            formats[x.format or "?"] = formats.get(x.format or "?", 0) + 1
        counts: dict[tuple[int, int], int] = {}
        for x in infos:
            key = (x.displayed_size[0], x.displayed_size[1])
            counts[key] = counts.get(key, 0) + 1
        sizes = [list(k) for k, _ in sorted(counts.items(), key=lambda kv: -kv[1])]
        return MediaInfoResult(kind="images", count=len(infos), folder=self.folder, formats=formats,
                               sizes=sizes, images=infos if with_items else None)

    def open(self, i: int, draft: int | None = None) -> Image.Image:
        try:
            with Image.open(self.paths[i]) as im:
                if draft and im.format == "JPEG":
                    im.draft("RGB", (draft, draft))
                im = ImageOps.exif_transpose(im)
                im.load()
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA" if ("A" in im.getbands() or "transparency" in im.info) else "RGB")
                return im
        except Exception as e:
            raise ToolError(f"cannot read image {self.paths[i]} ({e})")

    def measure(self, i0: int, i1: int, analysis_width: int, tiny: bool, only: list[int] | None = None):
        out = []
        for i in (only if only is not None else range(i0, i1 + 1)):
            im = self.open(i, draft=analysis_width)
            sc = _sharpness(im, analysis_width)
            self.measured.append(sc)
            out.append((i, None, sc, _tiny(im) if tiny else None))
        return out

    def fetch(self, picks, sink):
        for p in picks:
            sink(p, self.open(p.key))


def make_source(inp: InputSpec):
    if inp.video:
        return VideoSource(inp.video)
    if inp.images:
        parents = {str(Path(p).parent) for p in inp.images}
        folder = parents.pop() if len(parents) == 1 else None      # all in one folder: name it after it
        return ImageSource(list(inp.images), name=Path(folder).name if folder else "imagenes", folder=folder)
    folder = Path(inp.folder)
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
             and not p.name.startswith(".")
             and (inp.pattern is None or fnmatch.fnmatch(p.name.lower(), inp.pattern.lower()))]
    if not files:
        raise ToolError(f"input.folder: no images ({SUPPORTED_IMAGES}) in {folder}"
                        + (f' matching "{inp.pattern}"' if inp.pattern else ""))
    if len(files) > MAX_IMAGES:
        raise ToolError(f"input.folder: {len(files)} images in {folder}; the limit is {MAX_IMAGES}. "
                        "Narrow it with input.pattern or split the folder")
    files.sort(key=lambda p: _natural(p.name))
    return ImageSource([str(p) for p in files], name=folder.name or "imagenes", folder=str(folder))


# ============================================================================ shots
FLAT_STD = 6.0            # a frame whose colour channels all vary less than this is plain (black, white...)
LINEAR = 0.3              # dissolve: the middle frame is the mix of the ends, give or take 30 % of their difference
SHOT_WINDOWS_S = (0.5, 1.5)


class ShotScan:
    """What shot detection needs from every frame, measured on its tiny 64×36 copy in one pass:
    the jump from the frame before (d1) and from two before (d2), the brightness and contrast, and
    whether the frame is the middle of a dissolve-like window (lin). Memory: a few numbers per
    frame plus the last 1.5 s of tiny copies."""

    def __init__(self, fps: float, threshold: float, fades: bool):
        self.fps, self.thr, self.fades = fps, threshold, fades
        self.ks = sorted({max(2, round(fps * s / 2) * 2) for s in SHOT_WINDOWS_S})    # even window lengths
        self.ring: deque = deque(maxlen=self.ks[-1] + 1)
        self.d1, self.d2, self.luma, self.sd = array("f"), array("f"), array("f"), array("f")
        self.lin = bytearray()

    def add(self, tiny: Image.Image):
        st, r = ImageStat.Stat(tiny), self.ring
        self.luma.append(0.299 * st.mean[0] + 0.587 * st.mean[1] + 0.114 * st.mean[2])
        self.sd.append(max(st.stddev))
        self.d1.append(_difference(r[-1], tiny) if r else 0.0)
        self.d2.append(_difference(r[-2], tiny) if len(r) > 1 else 0.0)
        r.append(tiny)
        self.lin.append(0)
        if self.fades:
            n = len(self.lin)
            for k in self.ks:
                if len(r) > k:
                    a, m, b = r[-1 - k], r[-1 - k // 2], r[-1]
                    dk = _difference(a, b)
                    if dk >= 0.6 * self.thr and _difference(m, Image.blend(a, b, 0.5)) <= LINEAR * dk:
                        self.lin[n - 1 - k // 2] = 1


@dataclass
class _Cut:
    """A boundary between two shots, as positions in the scan (0 = first frame of the range)."""
    split: int                  # first frame of the next shot
    out: str                    # how the shot before ends: cut | fade_out | dissolve
    into: str                   # how the next one starts: cut | fade_in | dissolve
    out_n: int = 0              # frames of the shot before taken by the transition
    in_n: int = 0               # frames of the next shot taken by it
    out_len: int = 0            # length to report (a dissolve: the whole of it)
    in_len: int = 0
    color: str | None = None    # fades: "black", "white" or None (another plain colour)
    strength: float = 0.0


def _find_cuts(s: ShotScan, min_shot_s: float) -> tuple[list[_Cut], tuple, tuple]:
    """Boundaries between shots, and how the range starts and ends: ("start" | "fade_in", frames,
    colour) and ("end" | "fade_out", frames, colour)."""
    n, fps, T = len(s.d1), s.fps, s.thr
    d1, d2, luma, sd = s.d1, s.d2, s.luma, s.sd
    flat = [v < FLAT_STD for v in sd]
    m = max(1, round(min_shot_s * fps))               # shortest shot, frames
    covered = bytearray(n)
    cuts: list[_Cut] = []
    head, tail = ("start", 0, None), ("end", 0, None)

    def mark(lo: int, hi: int):
        for j in range(max(0, lo), min(n, hi + 1)):
            covered[j] = 1

    def energy(j: int, ref: float) -> float:          # how far a frame is from the plain colour
        return abs(luma[j] - ref) + sd[j]

    def walk(j0: int, step: int, ref: float) -> tuple[int, float, float]:
        """Frames leading away from a plain run while they get further from its colour, allowing short
        plateaus (encoders repeat frames): (frames, gain, biggest single step). A fade gains gradually;
        a cut gains almost everything in its first step."""
        best, cur, flat_steps, big = j0, j0, 0, 0.0
        while 0 <= cur + step < n and abs(cur + step - j0) <= 5 * fps:
            nxt = cur + step
            e_next, e_best = energy(nxt, ref), energy(best, ref)
            if e_next > e_best + 0.15:
                big = max(big, e_next - e_best)
                best, flat_steps = nxt, 0
            elif e_next < e_best - 1.0 or flat_steps >= 3:
                break
            else:
                flat_steps += 1
            cur = nxt
        return abs(best - j0), energy(best, ref) - energy(j0, ref), big

    def inside(j0: int, step: int, limit: int, ref: float) -> int:
        """Plain frames at the edge of the run that are still fading (getting closer to its colour)."""
        k = 0
        while k < limit and energy(j0 + k * step, ref) > energy(j0 + (k + 1) * step, ref) + 0.15:
            k += 1
        return k

    # 1. plain runs (black, white...): a fade out, a fade in, or a cut, on each side
    i = 0
    while i < n:
        if not flat[i]:
            i += 1
            continue
        a = i
        while i + 1 < n and flat[i + 1]:
            i += 1
        b, i = i, i + 1
        ref = statistics.median(luma[a:b + 1])
        color = "black" if ref < 64 else "white" if ref > 192 else None
        w_out, g_out, big_out = walk(a, -1, ref) if a > 0 else (0, 0.0, 0.0)
        w_in, g_in, big_in = walk(b, 1, ref) if b < n - 1 else (0, 0.0, 0.0)
        f_out = s.fades and w_out >= 2 and g_out >= 6 and big_out < 0.6 * g_out
        f_in = s.fades and w_in >= 2 and g_in >= 6 and big_in < 0.6 * g_in
        i_out = inside(a, 1, b - a, ref) if f_out else 0
        i_in = inside(b, -1, b - a - i_out, ref) if f_in else 0
        n_out, n_in = (w_out + i_out if f_out else 0), (w_in + i_in if f_in else 0)
        mark(a - n_out - 1, b + n_in + 1)
        held = (b - i_in) - (a + i_out) + 1 >= max(m, round(fps))   # 1 s or more of it: a shot of its own
        if a == 0 and b == n - 1:
            continue                                   # the whole range is plain: one shot
        if a == 0:                                     # starts plain: fading in from it, or a plain shot
            if f_in:
                head = ("fade_in", n_in, color)
            elif held:
                cuts.append(_Cut(b + 1, "cut", "cut", strength=1000.0))
            continue
        if b == n - 1:
            if f_out:
                tail = ("fade_out", n_out, color)
            elif held:
                cuts.append(_Cut(a, "cut", "cut", strength=1000.0))
            continue
        if f_out and f_in:                             # fade out, hold, fade in: the change in the middle
            mid = (a + i_out + b - i_in + 1) // 2
            cuts.append(_Cut(mid, "fade_out", "fade_in", n_out, n_in, n_out, n_in, color, 1000.0))
        elif f_out:                                    # fade out, hold, cut: the hold ends the shot before
            cuts.append(_Cut(b + 1, "fade_out", "cut", n_out, 0, n_out, 0, color, 1000.0))
        elif f_in:                                     # cut, hold, fade in: the hold starts the next one
            cuts.append(_Cut(a, "cut", "fade_in", 0, n_in, 0, n_in, color, 1000.0))
        elif held:                                     # cut, a plain shot (black, a card of one colour), cut
            cuts += [_Cut(a, "cut", "cut", strength=1000.0), _Cut(b + 1, "cut", "cut", strength=1000.0)]
        else:                                          # a few plain frames between two cuts: one cut
            cuts.append(_Cut((a + b + 1) // 2, "cut", "cut", strength=1000.0))
    # 2. cuts: a jump that stands out from the movement around it and is not a flash
    w = max(3, round(fps / 4))
    for i in range(1, n):
        if d1[i] < T or covered[i]:
            continue
        around = sorted(d1[j] for j in range(max(1, i - w), min(n, i + w + 1)) if abs(j - i) > 1)
        if around and d1[i] < 2 * around[len(around) // 2]:
            continue
        if (i + 1 < n and d2[i + 1] < 0.5 * d1[i]) or (i >= 2 and d2[i] < 0.5 * d1[i]):
            continue                                   # one odd frame (a flash): the picture comes back
        cuts.append(_Cut(i, "cut", "cut", strength=d1[i]))
        mark(i - 1, i + 1)
    # 3. dissolves: runs of windows whose middle frame is the mix of their ends
    k0 = s.ks[0]
    i = 0
    while i < n:
        if not s.lin[i] or covered[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and not covered[j + 1] and (s.lin[j + 1] or (j + 2 < n and s.lin[j + 2])):
            j += 1
        run, c = j - i + 1, (i + j) // 2
        est = run / 0.7 if run < 0.7 * k0 else run + 0.3 * k0       # length the run implies
        base = statistics.median(d2[max(2, c - 2):c + 3]) if c > 1 else 0.0
        lo = hi = c
        lim = round(1.5 * fps)
        while lo - 1 > 1 and d2[lo - 1] >= 0.5 * base and d1[lo - 1] < T and c - lo < lim:
            lo -= 1
        while hi + 1 < n and d2[hi + 1] >= 0.5 * base and d1[hi + 1] < T and hi - c < lim:
            hi += 1
        length = round(min(max(hi - lo + 1, 0.7 * est), 1.4 * est))
        if run >= 3 and length >= max(3, 0.2 * fps):
            cuts.append(_Cut(c, "dissolve", "dissolve", length // 2, length - length // 2, length, length,
                             strength=0.8 * T))
        elif max(d1[max(1, c - 2):c + 3]) >= 0.4 * T:                # a blended frame or two at a cut
            cuts.append(_Cut(c, "cut", "cut", strength=0.6 * T))
        mark(c - length // 2 - 1, c + length // 2 + 1)
        i = j + 1
    # 4. no shot shorter than min_shot_s: of two boundaries too close, keep the stronger
    kept: list[_Cut] = []
    for cu in sorted(cuts, key=lambda x: x.split):
        if cu.split < m or n - cu.split < m:
            continue
        if kept and cu.split - kept[-1].split < m:
            if cu.strength > kept[-1].strength:
                kept[-1] = cu
            continue
        kept.append(cu)
    return kept, head, tail


@dataclass
class ShotData:
    first: int                  # frame numbers
    last: int
    t_in: float
    t_out: float
    trans_in: str
    trans_out: str
    in_s: float = 0.0
    out_s: float = 0.0
    color_in: str | None = None
    color_out: str | None = None


def _make_shots(meta: list, s: ShotScan, cuts: list[_Cut], head: tuple, tail: tuple, thr: float,
                t_end: float) -> list[tuple[int, ShotData]]:
    """(position in meta of the frame to show, shot) per shot: the sharpest frame clear of the
    transitions, at or above the sharpness threshold when the shot has one."""
    n, fps = len(meta), s.fps
    splits = [0] + [c.split for c in cuts] + [n]
    out = []
    for k in range(len(splits) - 1):
        a, b = splits[k], splits[k + 1] - 1
        cin = cuts[k - 1] if k > 0 else None
        cout = cuts[k] if k < len(cuts) else None
        if cin:
            trans_in, in_n, in_len, col_in = cin.into, cin.in_n, cin.in_len, cin.color
        else:
            trans_in, in_n, in_len, col_in = head[0], head[1], head[1], head[2]
        if cout:
            trans_out, out_n, out_len, col_out = cout.out, cout.out_n, cout.out_len, cout.color
        else:
            trans_out, out_n, out_len, col_out = tail[0], tail[1], tail[1], tail[2]
        t_in, t_out = meta[a][1], (meta[b + 1][1] if b + 1 < n else t_end)
        core = [j for j in range(a + in_n, b - out_n + 1) if s.sd[j] >= FLAT_STD] or list(range(a, b + 1))
        good = [j for j in core if meta[j][2] >= thr] or core
        rep = max(good, key=lambda j: meta[j][2])
        out.append((rep, ShotData(meta[a][0], meta[b][0], t_in, t_out, trans_in, trans_out,
                                  in_len / fps if trans_in in ("fade_in", "dissolve") else 0.0,
                                  out_len / fps if trans_out in ("fade_out", "dissolve") else 0.0,
                                  col_in, col_out)))
    return out


# ============================================================================ selection
@dataclass(eq=False)
class Pick:
    key: int                       # frame number (video) or image index
    t: float | None = None         # seconds (video)
    sharpness: float | None = None
    tiny: Any = None
    note: Panel | None = None
    image: Image.Image | None = None
    exact_index: bool = True       # key is the decoded frame index (not estimated from time)
    shot: ShotData | None = None   # mode "shots"
    span: tuple[float, float] | None = None    # part of the video it stands for: its stretch or shot, s


@dataclass
class Chosen:
    picks: list[Pick]
    dropped: list[Dropped]
    mode: str
    range_s: list[float] | None
    range_pct: list[float]
    measured: int
    threshold: float
    stats: dict[str, float] | None
    pct_of: Callable
    warnings: list[str] = field(default_factory=list)
    shots: ShotsInfo | None = None


def _given(sel: SelectSpec, name: str) -> bool:
    v = getattr(sel, name)
    return v not in (None, [], 0, 0.0) if name in ("snap_s", "min_gap_s") else v not in (None, [])


EXACT_FIELDS = ("step_pct", "step_s", "at_pct", "at_s", "at_frames", "at_images", "at_names")
VIDEO_ONLY = ("every_s", "step_s", "at_s", "at_frames", "snap_s", "start_s", "end_s", "min_gap_s")
IMAGE_ONLY = ("at_images", "at_names")
SHOT_FIELDS = ("shot_threshold", "min_shot_s", "fades")
PANEL_POS = ("frame", "at_s", "at_pct", "image", "image_name")


def check_selection(kind: str, sel: SelectSpec, panels: list[Panel]) -> list[str]:
    """Problems that depend on the input kind or on several fields at once."""
    p = []
    if kind == "images":
        if sel.mode == "shots":
            p.append('select.mode: "shots" finds the shots of a video; with images use "all", "sharpest", '
                     '"even" or "best"')
        for f in VIDEO_ONLY:
            if _given(sel, f):
                p.append(f"select.{f}: only for video input; with images use count, every_pct, step_pct, "
                         "at_pct, at_images, at_names, start_pct or end_pct")
    else:
        for f in IMAGE_ONLY:
            if _given(sel, f):
                p.append(f"select.{f}: only for image input; with a video use at_frames, at_s or at_pct")
    if sel.every_s and sel.every_pct:
        p.append("select.every_s: give every_s or every_pct, not both")
    if sel.start_s is not None and sel.start_pct is not None:
        p.append("select.start_s: give start_s or start_pct, not both")
    if sel.end_s is not None and sel.end_pct is not None:
        p.append("select.end_s: give end_s or end_pct, not both")
    positioned = []
    for i, pn in enumerate(panels):
        given = [k for k in PANEL_POS if getattr(pn, k) is not None]
        if len(given) > 1:
            p.append(f"panels[{i}]: give only one position ({', '.join(given)} given)")
        if kind == "images" and set(given) & {"frame", "at_s"}:
            p.append(f"panels[{i}].{given[0]}: only for video input; with images use image, image_name or at_pct")
        if kind == "video" and set(given) & {"image", "image_name"}:
            p.append(f"panels[{i}].{given[0]}: only for image input; with a video use frame, at_s or at_pct")
        positioned.append(bool(given))
    if any(positioned) and not all(positioned):
        missing = [f"panels[{i}]" for i, x in enumerate(positioned) if not x]
        p.append("panels: either every panel has a position (frame, at_s, at_pct, image or image_name) or none "
                 f"has; without one: {', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}")
    if any(positioned) and any(_given(sel, f) for f in EXACT_FIELDS):
        p.append("select: exact positions in select and positions in panels[] at the same time; use one of them")
    return p


def _stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"min": round(min(values), 1), "p25": round(_percentile(values, 25), 1),
            "median": round(_percentile(values, 50), 1), "p75": round(_percentile(values, 75), 1),
            "max": round(max(values), 1)}


def choose(src, sel: SelectSpec, panels: list[Panel]) -> Chosen:
    video = src.kind == "video"
    warnings: list[str] = []
    n_items = src.count
    if n_items <= 0:
        raise ToolError(f"input.video: no frames can be read from {Path(src.path).name} (it gives no length nor "
                        "number of frames). An animated WebP cannot be read as a video: use the GIF, MP4 or WebM "
                        "version, or convert it")
    if video:
        span = src.span
        t0 = sel.start_s if sel.start_s is not None else (span * sel.start_pct / 100 if sel.start_pct is not None else 0.0)
        t1 = sel.end_s if sel.end_s is not None else (span * sel.end_pct / 100 if sel.end_pct is not None else span)
        t0, t1 = max(0.0, t0), min(span, t1)
        if t1 < t0 or (t1 == t0 and span > 0 and (sel.start_s is not None or sel.start_pct is not None)):
            raise ToolError(f"select: empty range, start {t0:.2f} s is not before end {t1:.2f} s "
                            f"(the last frame is at {span:.2f} s)")
        rng_pct = [round(100 * t0 / span, 1) if span else 0.0, round(100 * t1 / span, 1) if span else 100.0]
        rng_s = [round(t0, 2), round(t1, 2)]
    else:
        last = n_items - 1
        i0 = round(last * sel.start_pct / 100) if sel.start_pct is not None else 0
        i1 = round(last * sel.end_pct / 100) if sel.end_pct is not None else last
        if i1 < i0:
            raise ToolError(f"select: empty range, start_pct gives image {i0} and end_pct image {i1}")
        rng_pct = [round(100 * i0 / last, 1) if last else 0.0, round(100 * i1 / last, 1) if last else 100.0]
        rng_s = None

    def pct_of(p: Pick) -> float:
        if video:
            return round(min(100.0, 100 * (p.t or 0) / src.span), 1) if src.span else 0.0
        return round(100 * p.key / (n_items - 1), 1) if n_items > 1 else 0.0

    pos_panels = bool(panels) and all(any(getattr(p, k) is not None for k in PANEL_POS) for p in panels)
    exact = any(_given(sel, f) for f in EXACT_FIELDS)
    ignored = [f for f in ("count", "every_s", "every_pct", "mode", "min_sharpness", "drop_blurriest_pct",
                           "min_gap_s", "min_difference") if f in sel.model_fields_set]
    if (pos_panels or exact) and ignored:
        warnings.append(f"select.{', select.'.join(ignored)}: not used with exact positions")
    shot_set = [f for f in SHOT_FIELDS if f in sel.model_fields_set]
    if shot_set and (sel.mode != "shots" or pos_panels or exact):
        warnings.append(f"select.{', select.'.join(shot_set)}: only used with mode \"shots\"")

    # ---- one panel per shot
    if video and sel.mode == "shots" and not (pos_panels or exact):
        skip = [f for f in ("count", "every_s", "every_pct", "min_gap_s", "min_difference")
                if f in sel.model_fields_set]
        if skip:
            warnings.append(f"select.{', select.'.join(skip)}: not used with mode \"shots\" (one panel per shot)")
        scan = ShotScan(src.fps, sel.shot_threshold, sel.fades)
        meta = src.scan(t0, t1, sel.analysis_width, scan)
        if not meta:
            raise ToolError("select: no frames in the requested range")
        sh = [m[2] for m in meta]
        thr = max(sel.min_sharpness, _percentile(sh, sel.drop_blurriest_pct) if sel.drop_blurriest_pct > 0 else 0.0)
        cuts, head, tail = _find_cuts(scan, sel.min_shot_s)
        shots = _make_shots(meta, scan, cuts, head, tail, thr, meta[-1][1] + 1.0 / src.fps)
        if len(shots) > MAX_PANELS:
            raise ToolError(f"select: {len(shots)} shots found; the limit is {MAX_PANELS} panels per call. Do the "
                            f"video in parts with start_s and end_s (about {MAX_PANELS * (t1 - t0) / len(shots):.0f}"
                            " s each), or raise shot_threshold / min_shot_s")
        picks = [Pick(key=meta[j][0], t=meta[j][1], sharpness=meta[j][2], shot=sd, span=(sd.t_in, sd.t_out))
                 for j, sd in shots]
        _notes_by_order(picks, panels, warnings)
        kinds = ["dissolve" if c.out == "dissolve" else "fade" if "fade" in c.out + c.into else "cut" for c in cuts]
        info = ShotsInfo(found=len(shots), cuts=kinds.count("cut"), fades=kinds.count("fade"),
                         dissolves=kinds.count("dissolve"), threshold=sel.shot_threshold, min_shot_s=sel.min_shot_s)
        return Chosen(picks, [], "shots", rng_s, rng_pct, len(sh), round(thr, 1), _stats(sh), pct_of, warnings,
                      shots=info)

    # ---- panels choose themselves / exact positions
    if pos_panels or exact:
        picks: list[Pick] = []
        local_best: list[float | None] = []        # video, few positions: sharpest frame around each one
        if video:
            if pos_panels:
                targets = [(p.frame / src.fps if p.frame is not None else p.at_s if p.at_s is not None
                            else src.span * p.at_pct / 100, p) for p in panels]
            else:
                ts: list[float] = []
                if sel.step_pct:
                    k = int(math.floor(100 / sel.step_pct + 1e-9))
                    ts += [t0 + (t1 - t0) * min(100.0, j * sel.step_pct) / 100 for j in range(k + 1)]
                if sel.step_s:
                    k = int(math.floor((t1 - t0) / sel.step_s + 1e-9))
                    ts += [t0 + j * sel.step_s for j in range(k + 1)]
                ts += [src.span * x / 100 for x in sel.at_pct or []]
                ts += list(sel.at_s or [])
                ts += [f / src.fps for f in sel.at_frames or []]
                late = [x for x in ts if x > src.span + 1.0 / src.fps]
                if late:
                    warnings.append(f"select: {len(late)} position(s) after the last frame ({src.span:.2f} s) "
                                    "were moved to the last frame")
                targets = [(x, None) for x in sorted({round(min(max(x, 0.0), src.span), 4) for x in ts})]
            late = [x for x, note in targets if note is not None and x > src.span + 1.0 / src.fps]
            if late:
                warnings.append(f"panels: {len(late)} position(s) after the last frame ({src.span:.2f} s) were "
                                "moved to the last frame")
            seen, moved = set(), []
            probe = 0.5 if (sel.snap_s <= 0 and len(targets) <= 24) else 0.0   # few positions: check around
            for tg, note in targets:
                tg = min(max(tg, 0.0), src.span)
                i, t, sc, im, local = src.near(tg, sel.snap_s, sel.analysis_width, probe)
                if note is None and i in seen:
                    continue
                seen.add(i)
                if sel.snap_s > 0 and abs(t - tg) > 0.75 / src.fps:
                    moved.append((tg, t))
                picks.append(Pick(key=i, t=t, sharpness=sc, note=note, image=im, exact_index=False))
                local_best.append(local)
            if moved:
                warnings.append(f"select.snap_s: {len(moved)} position(s) were on blurred frames and moved to the "
                                "nearest sharp one: " + ", ".join(f"{a:.2f} → {b:.2f} s" for a, b in moved[:8])
                                + (" …" if len(moved) > 8 else ""))
        else:
            names = {p.name: k for k, p in enumerate(src.paths)}
            lower = {p.name.lower(): k for k, p in enumerate(src.paths)}

            def by_name(nm: str, where: str) -> int:
                k = names.get(nm, lower.get(nm.lower()))
                if k is None:
                    close = difflib.get_close_matches(nm, list(names), n=1, cutoff=0.8)
                    raise ToolError(f'{where}: no image named "{nm}" in the input'
                                    + (f' (did you mean "{close[0]}"?)' if close else ""))
                return k

            def by_index(k: int, where: str) -> int:
                if k >= n_items:
                    raise ToolError(f"{where}: image {k} does not exist; the input has {n_items} images "
                                    f"(indexes 0 to {n_items - 1})")
                return k

            if pos_panels:
                idx = []
                for j, p in enumerate(panels):
                    where = f"panels[{j}]"
                    k = (by_index(p.image, where + ".image") if p.image is not None else
                         by_name(p.image_name, where + ".image_name") if p.image_name is not None else
                         round((n_items - 1) * p.at_pct / 100))
                    idx.append((k, p))
            else:
                ks: set[int] = set()
                if sel.step_pct:
                    k = int(math.floor(100 / sel.step_pct + 1e-9))
                    ks |= {i0 + round((i1 - i0) * min(100.0, j * sel.step_pct) / 100) for j in range(k + 1)}
                ks |= {round((n_items - 1) * x / 100) for x in sel.at_pct or []}
                ks |= {by_index(k, f"select.at_images[{j}]") for j, k in enumerate(sel.at_images or [])}
                ks |= {by_name(nm, f"select.at_names[{j}]") for j, nm in enumerate(sel.at_names or [])}
                idx = [(k, None) for k in sorted(ks)]
            picks = [Pick(key=k, note=note) for k, note in idx]
            sharp = {m[0]: m[2] for m in src.measure(0, 0, sel.analysis_width, False,
                                                     only=sorted({k for k, _ in idx}))}
            for p in picks:
                p.sharpness = sharp[p.key]
        if len(picks) > MAX_PANELS:
            raise ToolError(f"select: {len(picks)} panels; the limit is {MAX_PANELS}")
        if not pos_panels:
            _notes_by_order(picks, panels, warnings)
        if video:                                  # blurred: next to the other panels, or to the frames around
            top = max(p.sharpness for p in picks)
            blurry = [str(k + 1) for k, p in enumerate(picks)
                      if (len(picks) > 2 and p.sharpness < 0.5 * top)
                      or (k < len(local_best) and local_best[k] and p.sharpness < 0.5 * local_best[k])]
            if blurry:
                warnings.append(f"select: panel(s) {', '.join(blurry)} look blurred (sharpness under half the "
                                "sharpest panel's, or the sharpest frame within 0.5 s); "
                                + ("raise snap_s" if sel.snap_s else "add snap_s (e.g. 0.5)")
                                + " to use the nearest sharp frame, or move the positions (start_s)")
        return Chosen(picks, [], "panels" if pos_panels else "positions", rng_s, rng_pct, len(src.measured), 0.0,
                      _stats(src.measured), pct_of, warnings)

    # ---- stretches / best / all
    mode = sel.mode
    if mode == "auto":                          # images: every one, unless a number of panels is asked for
        mode = "sharpest" if (video or "count" in sel.model_fields_set or sel.every_pct) else "all"
    if mode in ("best", "all") and (sel.every_s or sel.every_pct):
        warnings.append(f'select.every_s / every_pct: not used with mode "{mode}"')
    need_tiny = sel.min_difference > 0 or mode == "best"
    meta = src.measure(t0, t1, sel.analysis_width, need_tiny) if video else \
        src.measure(i0, i1, sel.analysis_width, need_tiny)
    if not meta:
        raise ToolError("select: no frames in the requested range")
    sh = [m[2] for m in meta]
    thr = max(sel.min_sharpness, _percentile(sh, sel.drop_blurriest_pct) if sel.drop_blurriest_pct > 0 else 0.0)

    def pos(m) -> float:
        return m[1] if video else float(m[0])

    def ref(m) -> dict:
        return {"frame": m[0]} if video else {"image": m[0], "name": src.paths[m[0]].name}

    def clash(m, other, same: float | None = None) -> dict | None:
        """Why m cannot be kept next to another kept pick (as Dropped fields), or None."""
        if video and sel.min_gap_s > 0 and abs(m[1] - other[1]) < sel.min_gap_s:
            return {"reason": "min_gap_s", "similar_to": other[0]}
        limit = sel.min_difference if same is None else same
        if m[3] is not None and other[3] is not None and (limit > 0 or same is not None):
            d = _difference(m[3], other[3])
            if d < limit or (limit == 0 and d == 0):
                return {"reason": "min_difference", "similar_to": other[0], "difference": round(d, 2)}
        return None

    dropped: list[Dropped] = []
    kept: list = []
    span_of: dict[int, tuple[float, float]] = {}
    asked, empty = 0, 0
    if mode == "all":
        for m in meta:
            why = {"reason": "min_sharpness"} if m[2] < thr else (clash(m, kept[-1]) if kept else None)
            if why:
                dropped.append(Dropped(**ref(m), **why))
            else:
                kept.append(m)
    elif mode == "best":
        asked = sel.count
        same = sel.min_difference if "min_difference" in sel.model_fields_set else 1.0
        repeats = 0
        for m in sorted((m for m in meta if m[2] >= thr), key=lambda m: -m[2]):
            if len(kept) >= sel.count:
                break
            why = next((w for w in (clash(m, k, same) for k in kept) if w), None)
            if why:
                repeats += 1
                if len(dropped) < 50:
                    dropped.append(Dropped(**ref(m), **why))
                continue
            kept.append(m)
        kept.sort(key=pos)
        if repeats:
            warnings.append(f"select: {repeats} picture(s) sharper than some chosen ones were left out as repeats "
                            f"(see dropped: similar_to, difference); min_difference {same:g} decides what counts "
                            "as a repeat (lower it to keep more, 0 = only exact repeats)")
    else:
        lo, hi = (t0, t1) if video else (float(i0), float(i1))
        width = hi - lo
        if sel.every_s or sel.every_pct:
            step = sel.every_s if sel.every_s else (width * sel.every_pct / 100 if width else 1.0)
            nseg = max(1, math.ceil(width / step - 1e-9)) if width > 0 else 1
            bounds = [lo + k * step for k in range(nseg)] + [hi + 1e-6]
        else:
            nseg = max(1, min(sel.count, len(meta)))
            bounds = [lo + k * width / nseg for k in range(nseg)] + [hi + 1e-6]
        asked = nseg
        seg: list[list] = [[] for _ in range(nseg)]
        k = 0
        for m in meta:
            while k < nseg - 1 and pos(m) >= bounds[k + 1] - 1e-9:
                k += 1
            if m[2] >= thr:
                seg[k].append(m)
        for k, cand in enumerate(seg):
            if not cand:
                empty += 1
                continue
            if mode == "sharpest":
                order = sorted(cand, key=lambda m: -m[2])
            else:
                mid = (bounds[k] + min(bounds[k + 1], hi)) / 2
                order = sorted(cand, key=lambda m: abs(pos(m) - mid))
            half = (bounds[k + 1] - bounds[k]) / 2
            pick, first_why = None, None
            # sharpest: keep picks at least half a stretch apart when possible; filters: take the
            # next best frame of the stretch instead of leaving it empty
            for spaced in ((True, False) if mode == "sharpest" else (False,)):
                for m in order:
                    if kept and spaced and pos(m) - pos(kept[-1]) < half:
                        continue
                    why = clash(m, kept[-1]) if kept else None
                    if why:
                        first_why = first_why or why
                        continue
                    pick = m
                    break
                if pick:
                    break
            if pick:
                kept.append(pick)
                span_of[pick[0]] = (bounds[k], min(bounds[k + 1], hi))
            else:
                dropped.append(Dropped(**ref(order[0]), **(first_why or {"reason": "min_difference"})))
    if not kept:
        raise ToolError("select: every frame was filtered out; lower min_sharpness, drop_blurriest_pct, "
                        "min_difference or min_gap_s")
    if len(kept) > MAX_PANELS:
        raise ToolError(f'select: {len(kept)} panels; the limit is {MAX_PANELS}. Use mode "sharpest", "even" '
                        'or "best" with count, or a shorter range')
    if asked and len(kept) < asked:
        why = []
        if dropped:
            reasons = {d.reason for d in dropped}
            fix = []
            if "min_difference" in reasons:
                fix.append(f"lower min_difference (now {sel.min_difference:g}; 2-3 suits a fixed background)")
            if "min_gap_s" in reasons:
                fix.append(f"lower min_gap_s (now {sel.min_gap_s:g})")
            why.append(f"{len(dropped)} removed by the filters (see dropped)"
                       + (f"; to get more, {' or '.join(fix)} or ask for fewer panels" if fix else ""))
        if empty:
            why.append(f"{empty} stretch(es) had nothing sharper than the threshold" if thr > 0 else
                       f"{empty} stretch(es) had no frames")
        if mode == "best" and not why:
            why.append("there are not that many different pictures")
        warnings.append(f"select: {asked} panels asked for, {len(kept)} given: " + "; ".join(why))
    picks = [Pick(key=m[0], t=m[1], sharpness=m[2], tiny=m[3], span=span_of.get(m[0])) for m in kept]
    _notes_by_order(picks, panels, warnings)
    return Chosen(picks, dropped, mode, rng_s, rng_pct, len(sh), round(thr, 1), _stats(sh), pct_of, warnings)


def _notes_by_order(picks: list[Pick], panels: list[Panel], warnings: list[str]):
    for k, p in enumerate(picks):
        p.note = panels[k] if k < len(panels) else None
    if len(panels) > len(picks):
        warnings.append(f"panels: {len(panels)} entries for {len(picks)} panels; panels[{len(picks)}] to "
                        f"panels[{len(panels) - 1}] were not used")


# ============================================================================ drawing
@dataclass
class Look:
    """Resolved style: colours as RGBA, fonts as paths, sizes in px."""
    bg: tuple
    text: tuple
    text2: tuple
    accent: tuple
    label_bg: tuple
    label_fg: tuple
    border: tuple
    notes_bg: tuple
    notes_fg: tuple
    rule: tuple
    guides: tuple
    border_width: int
    regular: str | None
    bold: str | None
    sharpen: float
    user_sizes: dict = field(default_factory=dict)
    title_size: int = 0
    text_size: int = 0
    label_size: int = 0
    number_size: int = 0
    notes_size: int = 0

    def font(self, size: float, bold: bool = False):
        return _ft(self.bold if bold else self.regular, max(6, int(round(size))))

    def set_sizes(self, panel_w: int, page_w: int):
        u = self.user_sizes
        self.text_size = u.get("text_size") or max(18, round(page_w / 64))
        self.title_size = u.get("title_size") or round(self.text_size * 1.3)
        self.label_size = u.get("label_size") or max(12, round(panel_w / 22))
        self.number_size = u.get("number_size") or round(self.label_size * 1.45)
        self.notes_size = u.get("notes_size") or max(11, round(self.label_size * 0.92))


SIZE_FIELDS = ("title_size", "text_size", "label_size", "number_size", "notes_size")


def resolve_look(style: StyleSpec, template: str) -> Look:
    base = THEMES[style.theme]
    col = {k: _rgba(getattr(style, k) or v) for k, v in base.items()}
    regular, bold = style.font, style.font_bold
    if regular:
        bold = bold or _bold_of(regular) or regular
    else:
        for r, b in DEFAULT_FONTS:
            if Path(r).is_file():
                regular, bold = r, (bold or (b if Path(b).is_file() else r))
                break
    bw = style.border_width if style.border_width is not None else TEMPLATES[template]["border"]
    return Look(bg=col["background"], text=col["text"], text2=col["text_secondary"], accent=col["accent"],
                label_bg=col["label_background"], label_fg=col["label_text"], border=col["panel_border"],
                notes_bg=col["notes_background"], notes_fg=col["notes_text"], rule=col["notes_rule"],
                guides=col["guides"], border_width=bw, regular=regular, bold=bold, sharpen=style.sharpen,
                user_sizes={k: getattr(style, k) for k in SIZE_FIELDS if getattr(style, k)})


def _fit_picture(im: Image.Image, w: int, h: int, fit: str, bg: tuple, sharpen: float) -> Image.Image:
    shrunk = im.width > w or im.height > h
    if fit == "cover":
        pic = Image.alpha_composite(Image.new("RGBA", (w, h), bg),
                                    ImageOps.fit(im.convert("RGBA"), (w, h), Image.LANCZOS))
    else:
        c = ImageOps.contain(im.convert("RGBA"), (w, h), Image.LANCZOS)
        pic = Image.new("RGBA", (w, h), bg)
        pic.alpha_composite(c, ((w - c.width) // 2, (h - c.height) // 2))
    if sharpen > 0 and shrunk:
        rgb = pic.convert("RGB").filter(ImageFilter.UnsharpMask(radius=1.0, percent=int(120 * sharpen), threshold=2))
        rgb.putalpha(pic.getchannel("A"))
        pic = rgb
    return pic


def _ellipsize(d: ImageDraw.ImageDraw, text: str, font, maxw: float) -> str:
    if d.textlength(text, font=font) <= maxw:
        return text
    while text and d.textlength(text + "…", font=font) > maxw:
        text = text[:-1]
    return (text.rstrip() + "…") if text else ""


def _wrap(d: ImageDraw.ImageDraw, text: str, font, width: float, first_width: float | None = None) -> list[str]:
    lines: list[str] = []
    for para in str(text).split("\n"):
        line = ""
        for word in para.split():
            avail = first_width if (first_width is not None and not lines) else width
            cand = f"{line} {word}".strip()
            if line and d.textlength(cand, font=font) > avail:
                lines.append(line)
                line = word
            else:
                line = cand
        lines.append(line)
    return lines


def _dashed(d: ImageDraw.ImageDraw, p0, p1, fill, width: int, dash: int):
    (x0, y0), (x1, y1) = p0, p1
    length = math.hypot(x1 - x0, y1 - y0)
    k = 0.0
    while k < length:
        e = min(length, k + dash)
        d.line((x0 + (x1 - x0) * k / length, y0 + (y1 - y0) * k / length,
                x0 + (x1 - x0) * e / length, y0 + (y1 - y0) * e / length), fill=fill, width=width)
        k += dash * 2


@dataclass
class View:
    """One panel ready to draw."""
    n: int
    number: str
    picture: Image.Image                 # RGBA, exactly panel_w × panel_h
    caption: str                         # caption text (overlay band, or next to the number)
    notes: list[tuple[str, str]]         # (title, text)
    notes_lines: int = 0


@dataclass
class Geo:
    template: str
    cols: int
    panel_w: int
    panel_h: int
    margin: int
    gap: int
    pad: int
    caption_h: int
    notes: bool
    notes_gap: int
    text_w: int
    page_w: int
    page_h: int | None
    header_h: int
    footer_h: int
    line_h: int
    lines: list[int]
    rows_per_page: int = 1

    def notes_height(self, lines: int) -> int:
        return 2 * self.pad + lines * self.line_h if (self.notes and lines) else 0

    def row_height(self, lines: int) -> int:
        nh = self.notes_height(lines)
        if self.template == "list":
            return max(self.panel_h, self.caption_h + (self.notes_gap + nh if nh else 0))
        return self.caption_h + self.panel_h + (self.notes_gap + nh if nh else 0)


def _notes_layout(d, look: Look, notes: list[tuple[str, str]], width: int, max_lines: int):
    """Lines of a notes box: each a list of (kind, text) with kind "title" or "text"."""
    ft, fb = look.font(look.notes_size), look.font(look.notes_size * 0.82, bold=True)
    lines: list[list[tuple[str, str]]] = []
    for title, text in notes:
        tw = d.textlength(title + "  ", font=fb)
        parts = _wrap(d, text, ft, width, first_width=max(10.0, width - tw))
        lines.append([("title", title), ("text", parts[0] if parts else "")])
        lines += [[("text", x)] for x in parts[1:]]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        kind, txt = lines[-1][-1]
        lines[-1][-1] = (kind, txt.rstrip() + " …")
    return lines


def _plan(L: LayoutSpec, out: StoryboardOutput, look: Look, n: int, aspect: float, notes_on: bool,
          notes_texts: list[list[tuple[str, str]]]) -> Geo:
    t = TEMPLATES[L.template]
    cols = 1 if L.template == "list" else max(1, min(L.columns or t["columns"], n))
    gap = L.gap if L.gap is not None else t["gap"]
    margin = L.margin if L.margin is not None else t["margin"]
    fixed = L.page != "auto"
    page_w = page_h = None
    if fixed:
        short, long_ = PAGE_INCHES[L.page]
        w_in, h_in = (long_, short) if L.orientation == "landscape" else (short, long_)
        page_w, page_h = round(w_in * out.dpi), round(h_in * out.dpi)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    scale = 1.0
    for _ in range(16):
        if fixed:
            avail = page_w - 2 * margin
            base = avail * 0.45 if L.template == "list" else (avail - (cols - 1) * gap) / cols
            panel_w = max(64, int(base * scale))
        else:
            panel_w = L.panel_width
        panel_h = max(16, round(panel_w / aspect))
        text_w = 0
        if L.template == "list":
            text_w = (page_w - 2 * margin - panel_w - gap) if fixed else max(360, panel_w)
        content_w = cols * (panel_w + (gap + text_w if L.template == "list" else 0)) + (cols - 1) * gap
        look.set_sizes(panel_w, page_w if fixed else content_w + 2 * margin)
        pad = L.padding if L.padding is not None else max(6, round(look.text_size * 0.5))
        line_h = round(look.notes_size * 1.4)
        header_h = pad * 2 + round(look.title_size * 1.2) + pad // 2 + round(look.text_size * 1.3) if L.header else 0
        footer_h = pad * 2 + round(look.text_size * 1.3) if L.footer else 0
        caption_h = round(look.number_size * 1.25) + pad // 2 if t["caption"] == "line" else 0
        nw = (text_w if L.template == "list" else panel_w) - 2 * pad
        lines = [len(_notes_layout(probe, look, nt, nw, L.notes.max_lines)) if nt else
                 (L.notes.blank_lines if notes_on else 0) for nt in notes_texts]
        geo = Geo(L.template, cols, panel_w, panel_h, margin, gap, pad, caption_h, notes_on, max(4, pad // 2),
                  text_w, page_w if fixed else content_w + 2 * margin, page_h, header_h, footer_h, line_h, lines)
        if not fixed:
            return geo
        target = L.rows or min(DEFAULT_ROWS[L.template][0 if L.orientation == "landscape" else 1],
                               math.ceil(n / cols))
        need = target * geo.row_height(max(lines or [0])) + (target - 1) * gap
        avail_h = page_h - 2 * margin - (header_h + gap if header_h else 0) - (footer_h + gap if footer_h else 0)
        if need <= avail_h or panel_w <= 64:
            return geo
        scale *= max(0.3, min(0.98, avail_h / need))
    return geo


def _draw_notes(layer: Image.Image, box, v: View, look: Look, geo: Geo, max_lines: int, blank_lines: int):
    x, y, w, h = box
    d = ImageDraw.Draw(layer)
    d.rectangle((x, y, x + w - 1, y + h - 1), fill=look.notes_bg)
    if look.border_width:
        d.rectangle((x, y, x + w - 1, y + h - 1), outline=look.border, width=look.border_width)
    pad = geo.pad
    ft, fb = look.font(look.notes_size), look.font(look.notes_size * 0.82, bold=True)
    if v.notes:
        yy = y + pad
        for line in _notes_layout(d, look, v.notes, w - 2 * pad, max_lines):
            xx = x + pad
            for kind, txt in line:
                if kind == "title":
                    d.text((xx, yy + geo.line_h * 0.5), txt, font=fb, fill=look.accent, anchor="lm")
                    xx += d.textlength(txt + "  ", font=fb)
                else:
                    d.text((xx, yy + geo.line_h * 0.5), txt, font=ft, fill=look.notes_fg, anchor="lm")
            yy += geo.line_h
    else:                                     # ruled lines down the whole box, to write by hand
        for k in range(1, max(blank_lines, (h - 2 * pad) // geo.line_h) + 1):
            yy = y + pad + k * geo.line_h - 2
            if yy < y + h - pad // 2:
                d.line((x + pad, yy, x + w - pad, yy), fill=look.rule, width=1)


def _draw_guides(pic: Image.Image, g: GuidesSpec, look: Look, label_font) -> Image.Image:
    if not (g.aspects or g.safe_zone or g.thirds):
        return pic
    w, h = pic.size
    over = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    lw = max(1, round(w / 400))
    for k, a in enumerate(g.aspects):
        r = ASPECT_RATIOS[a]
        if r > w / h + 1e-6:
            gh = w / r
            box = (0, (h - gh) / 2, w - 1, (h + gh) / 2)
        else:
            gw = h * r
            box = ((w - gw) / 2, 0, (w + gw) / 2, h - 1)
        if k == 0 and g.shade:
            for rect in ((0, 0, w, box[1]), (0, box[3], w, h), (0, 0, box[0], h), (box[2], 0, w, h)):
                if rect[2] - rect[0] > 0.5 and rect[3] - rect[1] > 0.5:
                    d.rectangle(rect, fill=(0, 0, 0, 120))
        d.rectangle(box, outline=look.guides, width=lw)
        d.text((box[0] + 4 * lw, box[1] + 3 * lw), a, font=label_font, fill=look.guides, anchor="la")
    if g.safe_zone:
        inset = (100 - g.safe_zone_pct) / 200
        x0, y0, x1, y1 = w * inset, h * inset, w * (1 - inset), h * (1 - inset)
        dash = max(4, round(w / 60))
        for p0, p1 in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
            _dashed(d, p0, p1, look.guides, lw, dash)
    if g.thirds:
        for k in (1, 2):
            d.line((w * k / 3, 0, w * k / 3, h), fill=look.guides, width=lw)
            d.line((0, h * k / 3, w, h * k / 3), fill=look.guides, width=lw)
    return Image.alpha_composite(pic, over)


def _overlay_label(pic: Image.Image, text: str, look: Look, position: str) -> Image.Image:
    w, h = pic.size
    fs = look.label_size
    band = fs + max(8, fs // 2)
    over = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    y0 = 0 if position == "top" else h - band
    d.rectangle((0, y0, w, y0 + band - 1), fill=look.label_bg)
    size, f = fs, look.font(fs, bold=True)
    while d.textlength(text, font=f) > w - 12 and size > max(8, fs * 0.6):
        size -= 1
        f = look.font(size, bold=True)
    d.text((6, y0 + band / 2), _ellipsize(d, text, f, w - 12), font=f, fill=look.label_fg, anchor="lm")
    return Image.alpha_composite(pic, over)


def _caption_line(d, x, y, w, v: View, look: Look, show_caption: bool):
    fn = look.font(look.number_size, bold=True)
    base = y + round(look.number_size * 0.95)
    d.text((x, base), v.number, font=fn, fill=look.text, anchor="ls")
    if show_caption and v.caption:
        fc = look.font(look.label_size)
        nw = d.textlength(v.number + "   ", font=fn)
        d.text((x + w, base), _ellipsize(d, v.caption, fc, w - nw), font=fc, fill=look.text2, anchor="rs")


def _cell_overlay(v: View, geo: Geo, look: Look, L: LayoutSpec, with_labels: bool) -> Image.Image | None:
    """What is drawn over a picture (guides, caption band), on a transparent layer; None if nothing."""
    over = None
    clear = Image.new("RGBA", (geo.panel_w, geo.panel_h), (0, 0, 0, 0))
    if L.guides.aspects or L.guides.safe_zone or L.guides.thirds:
        over = _draw_guides(clear, L.guides, look, look.font(max(9, look.label_size * 0.75), bold=True))
    if TEMPLATES[geo.template]["caption"] == "overlay" and with_labels and L.label.show and v.caption:
        over = _overlay_label(over or clear, v.caption, look, L.label.position)
    return over


def _draw_cell(layer, x, y, v: View, geo: Geo, look: Look, L: LayoutSpec, with_labels: bool, row_h: int):
    d = ImageDraw.Draw(layer)
    bw = look.border_width
    over = _cell_overlay(v, geo, look, L, with_labels)
    if geo.template == "list":
        px, py = x, y
        tx = x + geo.panel_w + geo.gap
        _caption_line(d, tx, y, geo.text_w, v, look, with_labels and L.label.show)
        nh = row_h - geo.caption_h - geo.notes_gap
        if geo.notes and v.notes_lines and nh > 0:
            _draw_notes(layer, (tx, y + geo.caption_h + geo.notes_gap, geo.text_w, nh), v, look, geo,
                        L.notes.max_lines, L.notes.blank_lines)
    else:
        if geo.caption_h:
            _caption_line(d, x, y, geo.panel_w, v, look, with_labels and L.label.show)
        px, py = x, y + geo.caption_h
        nh = row_h - geo.caption_h - geo.panel_h - geo.notes_gap
        if geo.notes and v.notes_lines and nh > 0:
            _draw_notes(layer, (x, py + geo.panel_h + geo.notes_gap, geo.panel_w, nh), v, look, geo,
                        L.notes.max_lines, L.notes.blank_lines)
    if bw:
        d.rectangle((px - bw, py - bw, px + geo.panel_w + bw - 1, py + geo.panel_h + bw - 1), outline=look.border,
                    width=bw)
    return over, (px, py)


def render_pages(views: list[View], geo: Geo, look: Look, L: LayoutSpec, header: tuple[str, str],
                 footer: str, strings: dict, with_labels: bool, cut: set | None = None, parts: bool = False) -> list:
    """The pages as images. parts=True: for each page, (the page without the pictures, [(view, (x, y),
    overlay or None)]), to animate the pictures."""
    per_page = geo.cols * geo.rows_per_page
    chunks = [views[k:k + per_page] for k in range(0, len(views), per_page)]
    pages = []
    cell_w = geo.panel_w + (geo.gap + geo.text_w if geo.template == "list" else 0)
    content_w = geo.cols * cell_w + (geo.cols - 1) * geo.gap
    title, subtitle = header
    inset = geo.pad if geo.margin < geo.pad else 0          # text never touches the page edge
    for pno, chunk in enumerate(chunks, 1):
        rows = [chunk[k:k + geo.cols] for k in range(0, len(chunk), geo.cols)]
        row_hs = [max(geo.row_height(v.notes_lines) for v in r) for r in rows]
        body_h = sum(row_hs) + geo.gap * (len(rows) - 1)
        W = geo.page_w
        H = geo.page_h or (geo.margin * 2 + (geo.header_h + geo.gap if geo.header_h else 0) + body_h
                           + (geo.gap + geo.footer_h if geo.footer_h else 0))
        if W * H > MAX_PAGE_PIXELS:
            raise ToolError(f"a page would be {W}×{H} px; the limit is {MAX_PAGE_PIXELS // 1_000_000} Mpx. "
                            "Use layout.rows to split the panels into pages, or a smaller layout.panel_width")
        page = Image.new("RGBA", (W, H), look.bg)
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        x0 = (W - content_w) // 2
        tx, right = x0 + inset, x0 + content_w - inset
        y = geo.margin
        page_txt = strings["page"].format(p=pno, n=len(chunks)) if len(chunks) > 1 else ""
        fs = look.font(look.text_size)
        if geo.header_h:
            ft = look.font(look.title_size, bold=True)
            yy = y + geo.pad
            if page_txt and not geo.footer_h:
                d.text((right, yy), page_txt, font=fs, fill=look.text2, anchor="ra")
            room = right - tx - (d.textlength(page_txt + "   ", font=fs) if page_txt and not geo.footer_h else 0)
            if title:
                shown = _ellipsize(d, title, ft, room)
                if shown != title and cut is not None:
                    cut.add("layout.title")
                d.text((tx, yy), shown, font=ft, fill=look.text, anchor="la")
                yy += round(look.title_size * 1.2) + geo.pad // 2
            if subtitle:
                shown = _ellipsize(d, subtitle, fs, room)
                if shown != subtitle and cut is not None:
                    cut.add("layout.subtitle")
                d.text((tx, yy), shown, font=fs, fill=look.text2, anchor="la")
            y += geo.header_h + geo.gap
        pastes = []
        for r, rh in zip(rows, row_hs):
            x = x0
            for v in r:
                pastes.append((v, *_draw_cell(layer, x, y, v, geo, look, L, with_labels, rh)))
                x += cell_w + geo.gap
            y += rh + geo.gap
        if geo.footer_h:
            fy = (H - geo.margin - geo.footer_h) if geo.page_h else y
            pw = d.textlength(page_txt + "   ", font=fs) if page_txt else 0
            txt = footer
            while txt and d.textlength(txt, font=fs) > right - tx - pw and " · " in txt:
                txt = txt.rsplit(" · ", 1)[0]
                if d.textlength(txt + " …", font=fs) <= right - tx - pw:
                    txt += " …"
                    break
            shown = _ellipsize(d, txt, fs, right - tx - pw)
            if shown != footer and cut is not None and L.footer_text is not None:
                cut.add("layout.footer_text")
            d.text((tx, fy + geo.pad), shown, font=fs, fill=look.text2, anchor="la")
            if page_txt:
                d.text((right, fy + geo.pad), page_txt, font=fs, fill=look.text2, anchor="ra")
        page = Image.alpha_composite(page, layer)
        if parts:
            pages.append((page, [(v, xy, over) for v, over, xy in pastes]))
            continue
        for v, over, (px, py) in pastes:
            page.alpha_composite(v.picture if over is None else Image.alpha_composite(v.picture, over), (px, py))
        pages.append(page)
    return pages


# ============================================================================ animation
CODEC_NAMES = {"libx264": "h264", "h264_videotoolbox": "h264", "mpeg4": "mpeg4", "libvpx-vp9": "vp9",
               "libvpx": "vp8", "libwebp_anim": "webp", "gif": "gif"}


@lru_cache(maxsize=None)
def _has_encoder(name: str) -> bool:
    try:
        av.codec.Codec(name, "w")
        return True
    except Exception:
        return False


def _encoder(fmt: str, quality: int, size: tuple[int, int], fps: int) -> tuple[str, dict, int | None]:
    """(encoder, options, bit rate) for mp4, webm or webp: the best one this PyAV has."""
    rate = int(size[0] * size[1] * fps * 0.12 * (0.4 + quality / 100))     # for encoders without a quality mode
    if fmt == "mp4":
        if _has_encoder("libx264"):
            return "libx264", {"crf": str(round(min(40, max(14, 40 - 0.22 * quality)))), "preset": "medium"}, None
        return ("h264_videotoolbox" if _has_encoder("h264_videotoolbox") else "mpeg4"), {}, rate
    if fmt == "webm":
        crf = str(round(min(50, max(15, 52 - 0.25 * quality))))
        if _has_encoder("libvpx-vp9"):
            return "libvpx-vp9", {"crf": crf, "b": "0", "deadline": "good", "cpu-used": "4", "row-mt": "1"}, None
        return "libvpx", {"crf": crf}, rate
    return "libwebp_anim", {"quality": str(quality), "lossless": "0"}, None


def _pull(sink) -> list:
    out = []
    while True:
        try:
            out.append(sink.pull())
        except (av.error.BlockingIOError, av.error.EOFError):
            return out


def _write_gif(path: Path, size: tuple[int, int], fps: int, n: int, frame: Callable, palette_from: list[int]):
    """GIF with one palette for the whole animation: FFmpeg's palettegen over some frames, then
    paletteuse with ordered dithering (it stays still where the picture does)."""
    w, h = size
    tb = Fraction(1, fps)
    g = av.filter.Graph()
    src = g.add_buffer(width=w, height=h, format="rgb24", time_base=tb)
    gen, sink = g.add("palettegen", "max_colors=256:stats_mode=full"), g.add("buffersink")
    src.link_to(gen)
    gen.link_to(sink)
    g.configure()
    for k, j in enumerate(palette_from):
        vf = av.VideoFrame.from_image(frame(j))
        vf.pts, vf.time_base = k, tb
        src.push(vf)
    src.push(None)
    palette = _pull(sink)[-1]
    g = av.filter.Graph()
    src = g.add_buffer(width=w, height=h, format="rgb24", time_base=tb)
    pal = g.add_buffer(width=palette.width, height=palette.height, format=palette.format.name, time_base=tb)
    use, sink = g.add("paletteuse", "dither=bayer:bayer_scale=4:diff_mode=rectangle"), g.add("buffersink")
    src.link_to(use, 0, 0)
    pal.link_to(use, 0, 1)
    use.link_to(sink)
    g.configure()
    palette.pts, palette.time_base = 0, tb
    pal.push(palette)
    pal.push(None)
    with av.open(str(path), "w", format="gif", options={"loop": "0"}) as c:
        st = c.add_stream("gif", rate=fps)
        st.width, st.height, st.pix_fmt = w, h, "pal8"

        def put(frames):
            for f in frames:
                for pkt in st.encode(f):
                    c.mux(pkt)
        for j in range(n):
            vf = av.VideoFrame.from_image(frame(j))
            vf.pts, vf.time_base = j, tb
            src.push(vf)
            put(_pull(sink))
        src.push(None)
        put(_pull(sink))
        for pkt in st.encode():
            c.mux(pkt)
    return "gif"


def _write_video(path: Path, fmt: str, size: tuple[int, int], fps: int, quality: int, n: int,
                 frame: Callable[[int], Image.Image], palette_from: list[int]) -> str:
    """Encode frames 0..n-1 (frame(j): RGB image of exactly size) into path; returns the codec."""
    if fmt == "gif":
        return _write_gif(path, size, fps, n, frame, palette_from)
    name, opts, rate = _encoder(fmt, quality, size, fps)
    tb = Fraction(1, fps)
    copts = {"movflags": "+faststart"} if fmt == "mp4" else {"loop": "0"} if fmt == "webp" else {}
    with av.open(str(path), "w", format=fmt, options=copts) as c:
        st = c.add_stream(name, rate=fps)
        st.width, st.height, st.pix_fmt = size[0], size[1], "yuv420p"
        if rate:
            st.bit_rate = rate
        st.options = opts
        for j in range(n):
            vf = av.VideoFrame.from_image(frame(j))
            vf.pts, vf.time_base = j, tb
            for pkt in st.encode(vf):
                c.mux(pkt)
        for pkt in st.encode():
            c.mux(pkt)
    return CODEC_NAMES.get(name, name)


def _even(v: int) -> int:
    return max(2, v - v % 2)


def _jpeg(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    _flatten(im).save(buf, "JPEG", quality=92)
    return buf.getvalue()


def _unjpeg(b: bytes) -> Image.Image:
    return Image.open(io.BytesIO(b)).convert("RGB")


def _rgb(c: tuple) -> tuple[int, int, int]:
    """An RGBA colour as seen over white (what a video shows)."""
    a = c[3] / 255 if len(c) > 3 else 1.0
    return tuple(round(c[k] * a + 255 * (1 - a)) for k in range(3))


def _check_frames(n: int, fps: int, where: str):
    if n > MAX_VIDEO_FRAMES:
        raise ToolError(f"{where}: the video would have {n} frames ({n / fps:.0f} s × {fps} fps); the limit is "
                        f"{MAX_VIDEO_FRAMES}. Lower output.animation.fps or duration_s")


def _bar(pic: Image.Image, frac: float, mark: float | None, h: int, fill, tick):
    """Progress bar along the bottom of a picture: done part in fill, mark = where the still frame is."""
    w, ph = pic.size
    y0 = ph - h
    pic.paste((0, 0, 0), (0, y0, w, ph), Image.new("L", (w, h), 150))
    d = ImageDraw.Draw(pic)
    if frac > 0:
        d.rectangle((0, y0, max(0, round(frac * w) - 1), ph - 1), fill=fill)
    if mark is not None:
        x = round(mark * (w - 1))
        d.rectangle((max(0, x - 1), max(0, y0 - h // 2), min(w - 1, x + 1), ph - 1), fill=tick)


def _samples_image(items: list[tuple[str, Image.Image]], look: Look, title: str, cols: int) -> Image.Image:
    """Sample frames of a video in a grid, each with its time: a still to look at instead of the video."""
    tw = min(480, max(im.width for _, im in items))
    thumbs = []
    for _, im in items:
        t = im.copy()
        t.thumbnail((tw, tw * 4), Image.LANCZOS)
        thumbs.append(t)
    th, fs, pad = max(t.height for t in thumbs), 18, 16
    cols = max(1, min(cols, len(items)))
    rows = math.ceil(len(items) / cols)
    head = round(fs * 2.2)
    W, H = pad + cols * (tw + pad), pad + head + rows * (th + round(fs * 1.7) + pad)
    page = Image.new("RGB", (W, H), _rgb(look.bg))
    d = ImageDraw.Draw(page)
    d.text((pad, pad), _ellipsize(d, title, look.font(fs * 1.15, bold=True), W - 2 * pad),
           font=look.font(fs * 1.15, bold=True), fill=_rgb(look.text))
    for i, ((cap, _), t) in enumerate(zip(items, thumbs)):
        r, c = divmod(i, cols)
        x, y = pad + c * (tw + pad), pad + head + r * (th + round(fs * 1.7) + pad)
        page.paste(t, (x + (tw - t.width) // 2, y))
        d.rectangle((x + (tw - t.width) // 2 - 1, y - 1, x + (tw + t.width) // 2, y + t.height), outline=_rgb(look.border))
        d.text((x, y + th + 5), cap, font=look.font(fs), fill=_rgb(look.text2))
    return page


def _spans(picks: list[Pick], end: float, min_s: float = 1.0) -> list[tuple[float, float]]:
    """Part of the video each panel stands for: its stretch or shot; otherwise from its frame to the
    next panel's in time (the last one, to the end of the range), at least min_s long."""
    order = sorted(range(len(picks)), key=lambda k: picks[k].t or 0.0)
    after = {a: picks[b].t for a, b in zip(order, order[1:])}
    out = []
    for k, p in enumerate(picks):
        if p.span:
            out.append(p.span)
            continue
        a, b = p.t or 0.0, after.get(k, end)
        if b - a < min_s:
            b = min(end, a + min_s)
            a = max(0.0, min(a, b - min_s))
        out.append((a, b))
    return out


def _decode_width(src: "VideoSource", w: int, h: int) -> int:
    """Width (as stored) to decode at so that the upright picture covers a w × h box."""
    dw, dh = src.info.displayed_size
    s = max(w / max(1, dw), h / max(1, dh))
    return max(16, min(src.info.size[0], round(src.info.size[0] * s) + 2))


def make_mosaic(src, picks: list[Pick], end: float, parts: list, geo: Geo, look: Look, L: LayoutSpec,
                O: StoryboardOutput, name: str, out_dir: Path, strings: dict):
    """The pages with every panel playing its part of the video at once. Returns (videos, {panel
    index: (clip [start, end], speed)})."""
    A, fps = O.animation, O.animation.fps
    spans = _spans(picks, end)
    D = A.duration_s or min(8.0, max(1.0, max(b - a for a, b in spans)))
    F = max(1, round(D * fps))
    _check_frames(F, fps, "output.animation.duration_s")
    if len(parts) > MAX_VIDEOS:
        raise ToolError(f"output.format: a mosaic is one video per page and there would be {len(parts)} pages; the "
                        f"limit is {MAX_VIDEOS}. Use layout.page \"auto\" (one page) or fewer panels")
    side = A.max_side or DEFAULT_SIDE[O.format]
    bg = _rgb(look.bg) + (255,)
    clips: dict[int, tuple[list[float], float]] = {}
    videos = []
    for pno, (base, slots) in enumerate(parts, 1):
        s = min(1.0, side / max(base.size))
        W, H = _even(round(base.width * s)), _even(round(base.height * s))
        canvas = _flatten(base).resize((W, H), Image.LANCZOS)
        bar_h = max(3, round(min(W, H) * 0.008))
        tiles = []
        for v, (px, py), over in slots:
            k = v.n - 1
            x0, y0 = round(px * s), round(py * s)
            pw, ph = max(2, round((px + geo.panel_w) * s) - x0), max(2, round((py + geo.panel_h) * s) - y0)
            ov = None if over is None else over if over.size == (pw, ph) else over.resize((pw, ph), Image.LANCZOS)
            a, b = spans[k]
            length = max(b - a, 1e-6)
            if length <= D:                        # real time, starting over when it ends
                speed, times = 1.0, [a + (j / fps) % length for j in range(F)]
            else:                                  # faster, to fit the loop
                speed, times = length / D, [a + length * j / F for j in range(F)]
            pics = {t: _jpeg(_fit_picture(im, pw, ph, L.fit, bg, look.sharpen))
                    for t, im in src.frames_at(sorted(set(times)), _decode_width(src, pw, ph))}
            still = min(1.0, max(0.0, ((picks[k].t or a) - a) / length))
            tiles.append((x0, y0, ov, times, pics, a, length, still))
            clips[k] = ([round(a, 3), round(b, 3)], round(speed, 2))

        def frame(j: int, canvas=canvas, tiles=tiles, bar_h=bar_h) -> Image.Image:
            im = canvas.copy()
            for x0, y0, ov, times, pics, a, length, still in tiles:
                pic = _unjpeg(pics[times[j]])
                if ov is not None:
                    pic.paste(ov, (0, 0), ov)
                if A.progress:
                    _bar(pic, (times[j] - a) / length, still, bar_h, _rgb(look.accent), _rgb(look.text))
                im.paste(pic, (x0, y0))
            return im

        path = out_dir / f"{name}{f'_p{pno}' if len(parts) > 1 else ''}.{O.format}"
        codec = _write_video(path, O.format, (W, H), fps, O.quality, F, frame,
                             sorted({round(i * (F - 1) / 23) for i in range(24)}))
        js = sorted({round(i * (F - 1) / 5) for i in range(6)})
        title = (f"{strings['samples']} · {path.name} · {W}×{H} · {fps} fps · {F / fps:.1f} s "
                 f"({strings['loop']})")
        spath = path.with_name(path.stem + "_samples.png")
        _samples_image([(f"{j / fps:.2f} s", frame(j)) for j in js], look, title, 3).save(spath, optimize=True)
        videos.append(VideoResult(kind="mosaic", page=pno, path=str(path), format=O.format, codec=codec,
                                  size=[W, H], fps=fps, frames=F, duration_s=round(F / fps, 3),
                                  bytes=path.stat().st_size, loop=O.format in ("gif", "webp"), samples=str(spath),
                                  sample_times_s=[round(j / fps, 2) for j in js]))
    return videos, clips


def _transition_of(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("encaden", "dissolv", "disolv", "cross", "mix")):
        return "dissolve"
    if any(w in t for w in ("fund", "fade")):
        return "fade"
    return "cut"


@dataclass
class _Slot:
    """One panel in the animatic: when it is on screen and how it comes and goes (s)."""
    start: float
    end: float
    fade_in: float = 0.0        # from black
    fade_out: float = 0.0       # to black
    mix_in: float = 0.0         # dissolve with the panel before (whole length, centred on start)
    mix_out: float = 0.0        # dissolve with the next one (centred on end)
    mix_first: float = 0.0      # the last panel of a looping gif / webp: dissolves into the first one


def animatic_timeline(picks: list[Pick], A: AnimationSpec, loop: bool = False) -> tuple[list[_Slot], float, float]:
    """Duration and transitions of every panel: panels[].duration_s / transition first, then what
    shot detection found, then panel_s / transition; scaled to duration_s if given. After the last
    panel a fade or dissolve fades to black, except that in a loop (gif, webp) a dissolve goes back
    into the first panel. Returns (slots, total, scale)."""
    n = len(picks)
    durs = [p.note.duration_s if p.note and p.note.duration_s else
            (p.shot.t_out - p.shot.t_in) if p.shot else A.panel_s for p in picks]
    slots = [_Slot(0.0, d) for d in durs]
    for k, p in enumerate(picks):
        user = p.note.transition if p.note and p.note.transition else None
        if user is None and p.shot:                      # what the video does
            if k == 0 and p.shot.trans_in == "fade_in":
                slots[k].fade_in = p.shot.in_s
            if p.shot.trans_out == "dissolve" and k + 1 < n:
                slots[k].mix_out = slots[k + 1].mix_in = p.shot.out_s
            elif p.shot.trans_out == "fade_out":
                slots[k].fade_out = p.shot.out_s
            if k + 1 < n and picks[k + 1].shot and picks[k + 1].shot.trans_in == "fade_in":
                slots[k + 1].fade_in = picks[k + 1].shot.in_s
            continue
        kind = _transition_of(user) if user else A.transition
        if kind == "dissolve" and k + 1 < n:
            slots[k].mix_out = slots[k + 1].mix_in = A.fade_s
        elif kind == "dissolve" and loop and n > 1:
            slots[k].mix_first = A.fade_s
        elif kind in ("fade", "dissolve"):               # a dissolve into nothing (the last panel) fades out
            slots[k].fade_out = A.fade_s
            if k + 1 < n:
                slots[k + 1].fade_in = A.fade_s
    total = sum(durs)
    f = A.duration_s / total if A.duration_s else 1.0
    t = 0.0
    for sl, d in zip(slots, durs):
        d *= f
        sl.start, sl.end = t, t + d
        sl.fade_in, sl.fade_out = min(sl.fade_in * f, d / 2), min(sl.fade_out * f, d / 2)
        sl.mix_in, sl.mix_out, sl.mix_first = min(sl.mix_in * f, d), min(sl.mix_out * f, d), min(sl.mix_first * f, d / 2)
        t += d
    for a, b in zip(slots, slots[1:]):                   # a dissolve fits in both panels
        a.mix_out = b.mix_in = min(a.mix_out, b.mix_in)
    return slots, t, f


def plan_card(L: LayoutSpec, O: StoryboardOutput, look: Look, aspect: float, notes_on: bool,
              notes: list[list[tuple[str, str]]], side: int) -> tuple[Geo, Look, LayoutSpec, int, tuple[int, int]]:
    """Geometry of an animatic card: one panel drawn like the page template, as big as fits in
    side × side. Text sizes follow the video size, not the picture's (a big picture would get huge
    notes); style sizes still win. Returns (geo, look, layout, notes lines, page size)."""
    lk = replace(look)
    label = max(14, round(side / 48))
    text = max(14, round(side / 64))
    lk.user_sizes = {"label_size": label, "number_size": round(label * 1.45), "notes_size": max(12, round(label * 0.92)),
                     "text_size": text, "title_size": round(text * 1.3)} | look.user_sizes
    if L.page != "auto":                               # a fixed page (16:9, A4...) is the size of the video
        Lc = L.model_copy(update={"columns": 1, "rows": 1})
        geo = _plan(Lc, O, lk, 1, aspect, notes_on, notes)
        geo.rows_per_page = 1
        return geo, lk, Lc, max(geo.lines or [0]), (geo.page_w, geo.page_h)
    pw = side
    for _ in range(4):
        Lc = L.model_copy(update={"columns": 1, "rows": 1, "page": "auto", "panel_width": max(64, pw)})
        geo = _plan(Lc, O, lk, 1, aspect, notes_on, notes)
        lines = max(geo.lines or [0])
        W = geo.page_w
        H = geo.margin * 2 + (geo.header_h + geo.gap if geo.header_h else 0) + geo.row_height(lines) + \
            (geo.gap + geo.footer_h if geo.footer_h else 0)
        if max(W, H) <= side * 1.02 or pw <= 64:
            break
        pw = int(pw * side / max(W, H) * 0.99)
    geo.rows_per_page = 1
    return geo, lk, Lc, lines, (W, H)


def make_animatic(picks: list[Pick], views: list[View], card_pics: dict[int, bytes], card_notes: list, card,
                  look: Look, O: StoryboardOutput, header: tuple[str, str], footers: list[str], strings: dict,
                  name: str, out_dir: Path) -> tuple[VideoResult, list[_Slot]]:
    """The panels one after another, each drawn as a card (the page template, one panel), with its
    duration and its cut, fade or dissolve."""
    A, fps = O.animation, O.animation.fps
    slots, total, scale = animatic_timeline(picks, A, loop=O.format in ("gif", "webp"))
    if total > MAX_VIDEO_S:
        raise ToolError(f"output.animation.duration_s: the animatic would last {total:.0f} s (the sum of the panel "
                        f"durations); the limit is {MAX_VIDEO_S} s. Give output.animation.duration_s to scale it "
                        "down, or use fewer panels or a shorter panel_s")
    F = max(1, round(total * fps))
    _check_frames(F, fps, "output.animation")
    geo, lk, Lc, lines, (cw, ch) = card
    side = A.max_side or DEFAULT_SIDE[O.format]
    s = min(1.0, side / max(cw, ch))
    W, H = _even(round(cw * s)), _even(round(ch * s))
    black = Image.new("RGB", (W, H), (0, 0, 0))
    starts = [sl.start for sl in slots]
    bar_h = max(4, round(min(W, H) * 0.008))

    @lru_cache(maxsize=4)
    def card_image(k: int) -> Image.Image:
        v = views[k]
        notes = (card_notes[k] or [("", " ")]) if geo.notes else []      # same box on every card, empty or not
        cv = View(v.n, v.number, _unjpeg(card_pics[k]).convert("RGBA"), v.caption, notes, lines if geo.notes else 0)
        page = render_pages([cv], geo, lk, Lc, header, footers[k], strings, with_labels=True)[0]
        return _flatten(page).resize((W, H), Image.LANCZOS)

    def frame(j: int) -> Image.Image:
        t = (j + 0.5) / fps
        k = max(0, min(len(slots) - 1, bisect.bisect_right(starts, t) - 1))
        sl = slots[k]
        im = card_image(k)
        if sl.mix_out and k + 1 < len(slots) and t > sl.end - sl.mix_out / 2:
            im = Image.blend(im, card_image(k + 1), min(1.0, (t - sl.end + sl.mix_out / 2) / sl.mix_out))
        elif sl.mix_first and t > sl.end - sl.mix_first:
            im = Image.blend(im, card_image(0), min(1.0, (t - sl.end + sl.mix_first) / sl.mix_first))
        elif sl.mix_in and k > 0 and t < sl.start + sl.mix_in / 2:
            im = Image.blend(card_image(k - 1), im, max(0.0, (t - sl.start + sl.mix_in / 2) / sl.mix_in))
        elif sl.fade_out and t > sl.end - sl.fade_out:
            im = Image.blend(im, black, min(1.0, (t - sl.end + sl.fade_out) / sl.fade_out))
        elif sl.fade_in and t < sl.start + sl.fade_in:
            im = Image.blend(black, im, max(0.0, (t - sl.start) / sl.fade_in))
        else:
            im = im.copy()
        if A.progress:
            d = ImageDraw.Draw(im)
            d.rectangle((0, H - bar_h, W - 1, H - 1), fill=(0, 0, 0))
            d.rectangle((0, H - bar_h, max(0, round(W * t / total) - 1), H - 1), fill=_rgb(look.accent))
            if len(slots) <= 100:
                for x in starts[1:]:
                    xx = round(W * x / total)
                    d.rectangle((xx - 1, H - bar_h, xx, H - 1), fill=_rgb(look.text))
        return im

    def at(t: float) -> int:                           # the frame showing time t
        return max(0, min(F - 1, round(t * fps - 0.5)))

    mids = [at((sl.start + sl.end) / 2) for sl in slots]
    path = out_dir / f"{name}.{O.format}"
    codec = _write_video(path, O.format, (W, H), fps, O.quality, F, frame,
                         sorted(set(mids[:: max(1, len(mids) // 24)]) | {0, F - 1}))
    tr, num = strings["trans"], [v.number for v in views]
    events = [(mids[k], f"{strings['panel']} {num[k]}") for k in range(len(slots))]
    for k, sl in enumerate(slots):                     # the samples show the transitions too
        if sl.mix_out and k + 1 < len(slots):
            events.append((at(sl.end), f"{tr['dissolve']} {num[k]}–{num[k + 1]}"))
        if sl.mix_first:
            events.append((at(sl.end - sl.mix_first / 2), f"{tr['dissolve']} {num[k]}–{num[0]}"))
        if sl.fade_out:
            events.append((at(sl.end - sl.fade_out / 2), f"{num[k]}: {tr['fade_out']['black']}"))
        if sl.fade_in:
            events.append((at(sl.start + sl.fade_in / 2), f"{num[k]}: {tr['fade_in']['black']}"))
    first: dict[int, str] = {}
    for j, cap in sorted(events):                      # one sample per frame, in order
        first.setdefault(j, cap)
    events = sorted(first.items())
    if len(events) > 16:
        events = [events[round(i * (len(events) - 1) / 15)] for i in range(16)]
    title = f"{strings['samples']} · {path.name} · {W}×{H} · {fps} fps · {F / fps:.1f} s"
    spath = path.with_name(path.stem + "_samples.png")
    _samples_image([(f"{j / fps:.1f} s · {cap}", frame(j)) for j, cap in events], look, title, 4).save(
        spath, optimize=True)
    video = VideoResult(kind="animatic", path=str(path), format=O.format, codec=codec, size=[W, H], fps=fps,
                        frames=F, duration_s=round(F / fps, 3), bytes=path.stat().st_size,
                        loop=O.format in ("gif", "webp"), samples=str(spath),
                        sample_times_s=[round(j / fps, 2) for j, _ in events], scale=round(scale, 4))
    return video, slots


def dominant_colors(im: Image.Image, n: int) -> list[ColorShare]:
    """The n most common colours of a picture (median cut on a small copy), most common first."""
    small = im.convert("RGB")
    small.thumbnail((160, 160))
    q = small.quantize(colors=n, method=Image.Quantize.MEDIANCUT, kmeans=2, dither=Image.Dither.NONE)
    pal = q.getpalette() or []
    counts = sorted((c for c in (q.getcolors() or []) if c[0]), reverse=True)
    total = sum(c for c, _ in counts) or 1
    return [ColorShare(hex="#%02x%02x%02x" % tuple(pal[3 * i:3 * i + 3]), pct=round(100 * c / total, 1))
            for c, i in counts]


def color_script_image(colors: list[list[ColorShare]], numbers: list[str], captions: list[str],
                       pictures: list[Image.Image], look: Look, title: str, subtitle: str) -> Image.Image:
    """The colour script: one column per panel, in order, with its picture on top (when wide enough),
    its dominant colours stacked by share (with their hex and % when they fit), its number and its
    time or name."""
    n = len(colors)
    cw = max(16, min(160, 2400 // max(1, n)))
    gap = 6 if cw >= 60 else 3 if cw >= 30 else 1
    pad, fs = 28, 20
    th = round(cw * pictures[0].height / max(1, pictures[0].width)) if cw >= 40 and pictures else 0
    band = max(160, round(cw * 1.5))
    nfs = max(10, min(22, cw // 4))
    cfs = max(9, min(15, cw // 9))
    num_h = round(nfs * 1.6) if cw >= 24 else 0
    cap_h = round(cfs * 1.6) if cw >= 60 else 0
    W = 2 * pad + n * cw + (n - 1) * gap
    head = round(fs * 1.3) + round(fs * 0.85 * 1.6)
    H = pad + head + pad // 2 + (th + gap if th else 0) + band + num_h + cap_h + pad
    page = Image.new("RGB", (max(W, 480), H), _rgb(look.bg))
    d = ImageDraw.Draw(page)
    d.text((pad, pad), _ellipsize(d, title, look.font(fs * 1.15, bold=True), page.width - 2 * pad),
           font=look.font(fs * 1.15, bold=True), fill=_rgb(look.text))
    d.text((pad, pad + round(fs * 1.45)), _ellipsize(d, subtitle, look.font(fs * 0.85), page.width - 2 * pad),
           font=look.font(fs * 0.85), fill=_rgb(look.text2))
    y0 = pad + head + pad // 2
    small = look.font(cfs)
    for k, (cols, num, cap) in enumerate(zip(colors, numbers, captions)):
        x = pad + k * (cw + gap)
        y = y0
        if th:
            page.paste(_flatten(pictures[k]).resize((cw, th), Image.LANCZOS), (x, y))
            y += th + gap
        total = sum(c.pct for c in cols) or 1.0
        yy = y
        for j, c in enumerate(cols):
            h = (y + band - yy) if j == len(cols) - 1 else round(band * c.pct / total)
            rgb = ImageColor.getrgb(c.hex)
            d.rectangle((x, yy, x + cw - 1, yy + max(1, h) - 1), fill=rgb)
            if cw >= 84 and h >= cfs + 6:                # the hex and the share, in black or white
                ink = (0, 0, 0) if 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2] > 140 else (255, 255, 255)
                d.text((x + 5, yy + 3), f"{c.hex} {c.pct:g}%", font=small, fill=ink)
            yy += h
        d.rectangle((x - 1, y - 1, x + cw, y + band), outline=_rgb(look.border))   # dark colours on a dark page
        if num_h:
            d.text((x + cw / 2, y + band + num_h / 2 + 2), num, font=look.font(nfs, bold=True),
                   fill=_rgb(look.text2), anchor="mm")
        if cap_h:
            d.text((x + cw / 2, y + band + num_h + cap_h / 2), _ellipsize(d, cap, small, cw), font=small,
                   fill=_rgb(look.text2), anchor="mm")
    return page


# ============================================================================ tools
def _default_dir(src, sub: str = "") -> Path:
    safe = re.sub(r"[^\w.-]+", "_", src.name).strip("._") or "input"
    return Path(tempfile.gettempdir()) / "storyboard-mcp" / safe / sub


def _out_dir(given: str | None, default: Path) -> Path:
    d = _abs(given) if given else default
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ToolError(f"output.dir: cannot create {d} ({e.strerror})")
    return d


def tool_media_info(args: MediaInfoArgs):
    return make_source(args.input).result(), []


def _anchor(i: int, src) -> str | None:
    if src.kind != "video":
        return None
    if i == 0:
        return "first_frame"
    if src.frames and i == src.frames - 1:
        return "last_frame"
    return None


def _transition_word(kind: str, color: str | None, strings) -> str:
    w = strings["trans"][kind]
    return w.get(color, w[None]) if isinstance(w, dict) else w


def _label_text(fields, template, src, number: str, p: Pick, pct: float, strings, sharp_pct) -> str:
    anchor = _anchor(p.key, src)
    anchor_txt = strings[anchor] if anchor else ""
    s = p.shot
    shot_vals = {"shot_in": _hms(s.t_in), "shot_out": _hms(s.t_out), "duration": f"{_tenths(s.t_out - s.t_in):.1f}s",
                 "transition": _transition_word(s.trans_out, s.color_out, strings)} if s else \
        {"shot_in": "", "shot_out": "", "duration": "", "transition": ""}
    if template is not None:
        vals = {"number": number, "n": number, "pct": f"{pct:.0f}", "pct1": f"{pct:.1f}",
                "sharpness": f"{p.sharpness:.0f}" if p.sharpness is not None else "",
                "sharpness_pct": sharp_pct} | shot_vals
        if src.kind == "video":
            vals |= {"frame": p.key, "frames_total": src.frames, "time": f"{_tenths(p.t):.1f}", "timecode": _hms(p.t),
                     "anchor": anchor_txt}
        else:
            info = src.infos()[p.key]
            vals |= {"image": p.key, "name": info.name, "images_total": src.count,
                     "size": f"{info.displayed_size[0]}×{info.displayed_size[1]}"}
        return template.format(**vals).strip()
    parts = []
    for f in fields:
        if f in SHOT_LABELS:
            if shot_vals[f]:
                parts.append(shot_vals[f])
        elif f == "number":
            parts.append(number)
        elif f in ("frame", "image"):
            parts.append(f"#{p.key}")
        elif f == "time":
            parts.append(f"{_tenths(p.t):.1f}s")
        elif f == "timecode":
            parts.append(_hms(p.t))
        elif f == "pct":
            parts.append(f"{pct:.0f}%")
        elif f == "sharpness" and p.sharpness is not None:
            parts.append(f"{strings['sharpness']} {p.sharpness:.0f}")
        elif f == "sharpness_pct":
            parts.append(f"{strings['sharpness']} {sharp_pct}%")
        elif f == "anchor" and anchor_txt:
            parts.append(anchor_txt)
        elif f == "name":
            parts.append(src.infos()[p.key].name)
        elif f == "size":
            s = src.infos()[p.key].displayed_size
            parts.append(f"{s[0]}×{s[1]}")
    return " · ".join(parts)


def check_layout(kind: str, L: LayoutSpec, out: StoryboardOutput | None, shots: bool = False) -> list[str]:
    p = []
    for k, f in enumerate(L.label.fields or []):
        if kind == "images" and f in VIDEO_LABELS:
            p.append(f'layout.label.fields[{k}]: "{f}" is only for video input; with images use number, image, '
                     "name, size, pct, sharpness or sharpness_pct")
        if kind == "video" and f in IMAGE_LABELS:
            p.append(f'layout.label.fields[{k}]: "{f}" is only for image input; with a video use number, frame, '
                     "time, timecode, pct, sharpness, sharpness_pct or anchor")
        if f in SHOT_LABELS and not shots:
            p.append(f'layout.label.fields[{k}]: "{f}" needs select.mode "shots" (one panel per shot of a video)')
    if L.label.template is not None:
        keys: set[str] = set()
        try:
            keys = {fn.split(".")[0].split("[")[0].split("!")[0].split(":")[0]
                    for _, fn, _, _ in string.Formatter().parse(L.label.template) if fn}
        except ValueError as e:
            p.append(f"layout.label.template: {e} (use {{placeholder}}; write {{{{ and }}}} for braces)")
        for k in sorted(keys - TEMPLATE_KEYS):
            p.append(f"layout.label.template: unknown placeholder {{{k}}}; available: "
                     + " ".join("{" + x + "}" for x in sorted(TEMPLATE_KEYS)))
        for k in sorted(keys & (VIDEO_KEYS if kind == "images" else IMAGE_KEYS)):
            p.append(f"layout.label.template: {{{k}}} is only for {'video' if kind == 'images' else 'image'} input")
        if not shots:
            for k in sorted(keys & SHOT_LABELS):
                p.append(f'layout.label.template: {{{k}}} needs select.mode "shots"')
    try:
        L.numbering.format.format(n=1)
        if "{n" not in L.numbering.format:
            p.append('layout.numbering.format: must contain {n}, e.g. "{n}" or "5.{n}"')
    except (KeyError, IndexError, ValueError) as e:
        p.append(f'layout.numbering.format: "{L.numbering.format}" is not valid ({e}); use {{n}}, e.g. '
                 '"SH{n:03d}"')
    return p


CSV_ES = {"n": "n", "number": "viñeta", "page": "página", "frame": "fotograma", "time_s": "segundos",
          "timecode": "tiempo", "pct": "%", "image": "imagen", "name": "nombre", "shot": "plano",
          "camera": "cámara", "action": "acción", "dialogue": "diálogo", "sound": "sonido",
          "transition": "transición", "duration_s": "duración (s)", "sharpness": "nitidez", "file": "archivo",
          "size": "tamaño", "source": "original", "shot_in_s": "entrada (s)", "shot_out_s": "salida (s)",
          "transition_in": "transición de entrada", "transition_out": "transición de salida", "colors": "colores",
          "shot_first_frame": "primer fotograma", "shot_last_frame": "último fotograma",
          "transition_in_s": "transición de entrada (s)", "transition_out_s": "transición de salida (s)"}



def _flatten(im: Image.Image) -> Image.Image:
    if im.mode == "RGBA":
        return Image.alpha_composite(Image.new("RGBA", im.size, (255, 255, 255, 255)), im).convert("RGB")
    return im.convert("RGB")


def _preview(page: Image.Image, longest: int = 1200) -> mt.ImageContent:
    im = _flatten(page)
    im.thumbnail((longest, longest), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82)
    return mt.ImageContent(type="image", data=base64.b64encode(buf.getvalue()).decode(), mimeType="image/jpeg")


def _selection_result(c: Chosen) -> SelectionResult:
    return SelectionResult(mode=c.mode, range_s=c.range_s, range_pct=c.range_pct, measured=c.measured,
                           threshold=c.threshold, sharpness_stats=c.stats, shots=c.shots)


def _shots_on(kind: str, sel: SelectSpec, panels: list[Panel]) -> bool:
    """True when the panels will be the shots of a video (mode "shots" and no exact positions)."""
    return kind == "video" and sel.mode == "shots" and not any(_given(sel, f) for f in EXACT_FIELDS) and \
        not any(getattr(p, k) is not None for p in panels for k in PANEL_POS)


def _shot_fields(r: PanelResult, p: Pick):
    s = p.shot
    if s is None:
        return
    r.shot_in_s, r.shot_out_s, r.duration_s = round(s.t_in, 3), round(s.t_out, 3), round(s.t_out - s.t_in, 3)
    r.shot_frames, r.transition_in, r.transition_out = [s.first, s.last], s.trans_in, s.trans_out
    r.transition_in_s = round(s.in_s, 2) if s.in_s else None
    r.transition_out_s = round(s.out_s, 2) if s.out_s else None


def check_output(kind: str, O: StoryboardOutput) -> list[str]:
    p = []
    A = O.animation
    if O.format not in ANIMATED:
        if "animation" in O.model_fields_set:
            p.append('output.animation: only used with an animated output.format: "mp4", "webm", "gif" or "webp"')
        return p
    if A.kind == "mosaic" and kind != "video":
        p.append('output.animation.kind: "mosaic" needs a video (each panel plays its part of it); with images '
                 'use "animatic", a timed slideshow')
    if A.max_side and A.max_side > MAX_SIDE[O.format]:
        p.append(f"output.animation.max_side: {A.max_side} px is too big for {O.format} (at most "
                 f"{MAX_SIDE[O.format]} px); use mp4 or webm for bigger videos")
    return p


def tool_make_storyboard(args: StoryboardArgs):
    src = make_source(args.input)
    L, S, O = args.layout, args.style, args.output
    shots_on = _shots_on(src.kind, args.select, args.panels)
    problems = check_selection(src.kind, args.select, args.panels) + check_layout(src.kind, L, O, shots_on) + \
        check_output(src.kind, O)
    if problems:
        raise ToolError(_problems_text("make_storyboard", problems))
    chosen = choose(src, args.select, args.panels)
    picks = chosen.picks
    strings = STRINGS[L.language]
    look = resolve_look(S, L.template)
    t = TEMPLATES[L.template]
    aspect = ASPECT_RATIOS[L.aspect] if L.aspect != "source" else src.aspect
    note_fields = ("shot", "camera", "action", "dialogue", "sound", "transition", "duration_s")
    has_notes = any(p.note and any(getattr(p.note, f) not in (None, "") for f in note_fields) for p in picks)
    notes_on = L.notes.show if L.notes.show is not None else (has_notes if L.template == "strip" else t["notes"])
    animated = O.format in ANIMATED
    kind = (O.animation.kind if O.animation.kind != "auto" else "mosaic" if src.kind == "video" else "animatic") \
        if animated else None
    if "color_count" in O.model_fields_set and not O.color_script:
        chosen.warnings.append("output.color_count: not used without output.color_script")

    def note_items(p: Pick, on: bool) -> list[tuple[str, str]]:
        if not (on and p.note):
            return []
        out = []
        for f in L.notes.fields:
            val = getattr(p.note, f)
            if val not in (None, ""):
                out.append((strings["notes"][f], f"{val:g} s" if f == "duration_s" else str(val)))
        return out

    notes_texts = [note_items(p, notes_on) for p in picks]
    geo = _plan(L, O, look, len(picks), aspect, notes_on, notes_texts)
    if geo.page_h:
        avail_h = geo.page_h - 2 * geo.margin - (geo.header_h + geo.gap if geo.header_h else 0) - \
            (geo.footer_h + geo.gap if geo.footer_h else 0)
        row_h = geo.row_height(max(geo.lines or [0]))
        fit_rows = max(1, (avail_h + geo.gap) // (row_h + geo.gap))
        geo.rows_per_page = min(L.rows, fit_rows) if L.rows else min(fit_rows, math.ceil(len(picks) / geo.cols))
        if L.rows and L.rows > fit_rows:     # only when the panels cannot shrink any more
            chosen.warnings.append(f"layout.rows: only {fit_rows} row(s) fit on a {L.page} page; using {fit_rows}")
    else:
        geo.rows_per_page = L.rows or (1 if L.template == "strip" else math.ceil(len(picks) / geo.cols))
    per_page = geo.cols * geo.rows_per_page
    pages_n = math.ceil(len(picks) / per_page)
    if pages_n > MAX_PAGES:
        raise ToolError(f"{pages_n} pages; the limit is {MAX_PAGES}. Use more layout.rows or columns, or fewer panels")
    card = card_notes = None
    if kind == "animatic":                   # the cards are planned first: pictures are made once, at their size
        card_on = L.notes.show if L.notes.show is not None else has_notes
        card_notes = [note_items(p, card_on) for p in picks]
        card = plan_card(L, O, look, aspect, card_on, card_notes, O.animation.max_side or DEFAULT_SIDE[O.format])

    out_dir = _out_dir(O.dir, _default_dir(src))
    name = O.name or f"{src.name}_{L.template}"
    frames_dir = None
    if O.frames:
        frames_dir = out_dir / f"{name}_frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
    order = {id(p): k for k, p in enumerate(picks)}
    files: dict[int, str] = {}
    pictures: dict[int, Image.Image] = {}
    colors: dict[int, list[ColorShare]] = {}
    card_pics: dict[int, bytes] = {}

    def sink(p: Pick, im: Image.Image):
        k = order[id(p)]
        if p.sharpness is None:
            p.sharpness = _sharpness(im, args.select.analysis_width)
        if frames_dir is not None:
            if src.kind == "video":
                dest = frames_dir / f"f{p.key:05d}.jpg"
                im.convert("RGB").save(dest, quality=95)
            else:
                dest = frames_dir / f"{k + 1:03d}_{src.paths[p.key].name}"
                shutil.copy2(src.paths[p.key], dest)
            files[k] = (str(dest), [im.width, im.height])
        pictures[k] = _fit_picture(im, geo.panel_w, geo.panel_h, L.fit, look.bg, look.sharpen)
        if O.color_script:
            colors[k] = dominant_colors(im, O.color_count)
        if card:
            card_pics[k] = _jpeg(_fit_picture(im, card[0].panel_w, card[0].panel_h, L.fit, look.bg, look.sharpen))

    src.fetch(picks, sink)
    top = max((p.sharpness or 0) for p in picks) or 1.0
    fields = L.label.fields or (["number", "shot_in", "duration", "transition"] if shots_on else
                                ["number", "time", "frame", "anchor"] if src.kind == "video" else ["number", "name"])
    if t["caption"] == "line":
        fields = [f for f in fields if f != "number"]
    views, results = [], []
    for k, p in enumerate(picks):
        number = p.note.number if (p.note and p.note.number) else \
            L.numbering.format.format(n=L.numbering.start + k * L.numbering.step)
        pct = chosen.pct_of(p)
        sp = round(100 * (p.sharpness or 0) / top)
        caption = _label_text(fields, L.label.template, src, number, p, pct, strings, sp)
        views.append(View(k + 1, number, pictures.pop(k), caption, notes_texts[k], geo.lines[k]))
        notes = {kk: vv for kk, vv in p.note.model_dump(exclude_none=True).items()
                 if kk not in PANEL_POS and kk != "number"} if p.note else {}
        r = PanelResult(n=k + 1, number=number, page=k // per_page + 1, pct=pct,
                        sharpness=round(p.sharpness, 1) if p.sharpness is not None else None, sharpness_pct=sp,
                        file=files[k][0] if k in files else None, size=files[k][1] if k in files else None,
                        notes=notes or None, colors=colors.get(k))
        if src.kind == "video":
            r.frame, r.time_s, r.timecode, r.anchor = p.key, round(p.t, 3), _hms(p.t), _anchor(p.key, src)
        else:
            r.image, r.name, r.source = p.key, src.paths[p.key].name, str(src.paths[p.key])
        _shot_fields(r, p)
        results.append(r)

    if src.kind == "video":
        info = src.info
        title = Path(src.path).name
        sub = [f"{info.displayed_size[0]}×{info.displayed_size[1]}"]
        if info.fps:
            sub.append(f"{info.fps:.3g} fps")
        sub.append(_hms(src.duration))
        if info.frames:
            sub.append(f"{info.frames:,} {strings['frames']}".replace(",", "."))
        if info.codec:
            sub.append(info.codec)
    else:
        res = src.result(with_items=False)
        title = Path(src.folder).name if src.folder else f"{src.count} {strings['images']}"
        shown = (f"{len(picks)} {strings['of']} " if len(picks) < src.count else "")
        sub = [f"{shown}{src.count} {strings['images']}" if (src.folder or shown) else "",
               ", ".join(sorted(res.formats)),
               ", ".join(f"{w}×{h}" for w, h in res.sizes[:3]) + (" …" if len(res.sizes) > 3 else "")]
    title = L.title if L.title is not None else title
    subtitle = L.subtitle if L.subtitle is not None else " · ".join(x for x in sub if x)
    if not (title or subtitle):
        geo.header_h = 0
    word = strings["frames"] if src.kind == "video" else strings["images"]
    modes = strings.get("mode_images", strings["mode"]) if src.kind == "images" else strings["mode"]
    if chosen.shots:
        sh = chosen.shots
        foot = [f"{sh.found} {strings['shots']} ({modes['shots']}) · {strings['cuts']}: {sh.cuts}, "
                f"{strings['fades']}: {sh.fades}, {strings['dissolves']}: {sh.dissolves}"]
    else:
        foot = [f"{len(picks)} {word} ({modes[chosen.mode]})"]
    if chosen.range_s:
        foot.append(f"{strings['range']} {_hms(chosen.range_s[0])}–{_hms(chosen.range_s[1])} "
                    f"({chosen.range_pct[0]:.0f}–{chosen.range_pct[1]:.0f} %)")
    elif chosen.range_pct != [0.0, 100.0]:
        foot.append(f"{strings['range']} {chosen.range_pct[0]:.0f}–{chosen.range_pct[1]:.0f} %")
    if chosen.measured:
        foot.append(f"{strings['measured']} {chosen.measured:,}".replace(",", "."))
    if chosen.stats:
        s = chosen.stats
        foot.append(f"{strings['sharpness']} {strings['min']} {s['min']:.0f} · {strings['median']} "
                    f"{s['median']:.0f} · {strings['max']} {s['max']:.0f}")
    if chosen.dropped:
        foot.append(f"{strings['dropped']} {len(chosen.dropped)}")
    if chosen.threshold > 0:
        foot.append(f"{strings['threshold']} {chosen.threshold:.0f}")
    footer = L.footer_text if L.footer_text is not None else " · ".join(foot)
    still = "png" if animated else O.format          # animated output: the pages are PNG

    def write(imgs: list[Image.Image], base: str) -> tuple[str | None, list[str]]:
        if still == "pdf":
            path = out_dir / f"{base}.pdf"
            flat = [_flatten(p) for p in imgs]
            flat[0].save(path, "PDF", save_all=True, append_images=flat[1:], resolution=float(O.dpi),
                         quality=O.quality)
            return str(path), []
        paths = []
        for k, im in enumerate(imgs, 1):
            path = out_dir / f"{base}{f'_p{k}' if len(imgs) > 1 else ''}.{still}"
            if still == "png":
                (im if look.bg[3] < 255 else im.convert("RGB")).save(path, optimize=True, dpi=(O.dpi, O.dpi))
            else:
                _flatten(im).save(path, quality=O.quality, dpi=(O.dpi, O.dpi))
            paths.append(str(path))
        return None, paths

    cut: set[str] = set()
    pages = render_pages(views, geo, look, L, (title, subtitle), footer, strings, with_labels=True, cut=cut)
    if cut:
        chosen.warnings.append(f"{', '.join(sorted(cut))}: shortened with … to fit the page; use a shorter text, a "
                               "smaller style.title_size / text_size or a wider page")
    pdf, paths = write(pages, name)
    page_results = [PageResult(page=k, path=paths[k - 1] if paths else pdf, size=list(pg.size),
                               panels=[v.n for v in views[(k - 1) * per_page:k * per_page]])
                    for k, pg in enumerate(pages, 1)]
    nolabels = None
    if O.also_without_labels:
        bare = render_pages(views, geo, look, L, (title, subtitle), footer, strings, with_labels=False)
        p2, paths2 = write(bare, f"{name}_nolabels")
        nolabels = [p2] if p2 else paths2

    videos = None
    if kind == "mosaic":
        parts = render_pages(views, geo, look, L, (title, subtitle), footer, strings, with_labels=True, parts=True)
        videos, clips = make_mosaic(src, picks, chosen.range_s[1], parts, geo, look, L, O, name, out_dir, strings)
        for k, (clip, speed) in clips.items():
            results[k].clip_s, results[k].speed = clip, speed
    elif kind == "animatic":
        footers = []
        for k, p in enumerate(picks):
            where = f" · {_hms(p.t)}" if src.kind == "video" else f" · {src.paths[p.key].name}"
            num = "" if views[k].number == str(k + 1) else f" ({views[k].number})"
            footers.append(L.footer_text if L.footer_text is not None else
                           f"{strings['panel']} {k + 1} {strings['of']} {len(picks)}{num}{where}")
        video, slots = make_animatic(picks, views, card_pics, card_notes, card, look, O, (title, subtitle), footers,
                                     strings, name, out_dir)
        videos = [video]
        for k, (r, sl) in enumerate(zip(results, slots)):
            r.video_s = [round(sl.start, 3), round(sl.end, 3)]
            nxt = slots[k + 1] if k + 1 < len(slots) else None
            if sl.mix_out or sl.mix_first:
                r.video_transition, length = "dissolve", sl.mix_out or sl.mix_first
            elif sl.fade_out or (nxt and nxt.fade_in):
                r.video_transition, length = "fade", sl.fade_out + (nxt.fade_in if nxt else 0.0)
            else:
                r.video_transition, length = "cut", 0.0
            r.video_transition_s = round(length, 3) if length else None

    color_path = None
    if O.color_script:
        sub = f"{len(picks)} {strings['panels']} · {O.color_count} {strings['colors']}"
        caps = [_hms(p.shot.t_in if p.shot else p.t) if src.kind == "video" else src.paths[p.key].name for p in picks]
        color_img = color_script_image([colors[k] for k in range(len(picks))], [v.number for v in views], caps,
                                       [v.picture for v in views], look, f"{strings['color_script']} · {title}", sub)
        color_path = out_dir / f"{name}_colors.png"
        color_img.save(color_path, optimize=True)

    shot = None
    if O.shot_list:
        shot = {}
        rows = [r.model_dump(exclude_none=True) for r in results]
        if "json" in O.shot_list:
            pj = out_dir / f"{name}_shots.json"
            pj.write_text(json.dumps({"input": src.result(with_items=False).model_dump(exclude_none=True),
                                      "panels": rows}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            shot["json"] = str(pj)
        if "csv" in O.shot_list:
            pc = out_dir / f"{name}_shots.csv"
            note_cols = ("shot", "camera", "action", "dialogue", "sound", "transition", "duration_s")
            cols = ["n", "number", "page", "frame", "time_s", "timecode", "pct", "shot_in_s", "shot_out_s",
                    "shot_first_frame", "shot_last_frame", "transition_in", "transition_in_s", "transition_out",
                    "transition_out_s", "image", "name", *note_cols, "sharpness", "colors", "file", "size", "source"]
            excel = O.csv_style == "excel_es"

            def value(r: dict, c: str, p: Pick):
                if c == "duration_s":                   # the planned duration, or else the shot's length
                    return (r.get("notes") or {}).get(c, r.get(c))
                if excel and p.shot and c in ("transition_in", "transition_out"):   # the words of the captions
                    kind, color = (p.shot.trans_in, p.shot.color_in) if c == "transition_in" else \
                        (p.shot.trans_out, p.shot.color_out)
                    return _transition_word(kind, color, STRINGS["es"])
                if c == "colors":
                    return ", ".join(f"{x['hex']} {x['pct']:g}%" for x in r.get("colors") or [])
                if c in ("shot_first_frame", "shot_last_frame"):
                    return (r.get("shot_frames") or [None, None])[c == "shot_last_frame"]
                return (r.get("notes") or {}).get(c) if c in note_cols else r.get(c)

            def cell(c, v):
                if v in (None, ""):
                    return ""
                if c == "size":
                    return "×".join(map(str, v))
                if excel and c == "number":
                    return f'="{v}"'                    # stays text: "3.1" is not read as a date
                if excel and c == "timecode":
                    return v.replace(".", ",")
                if excel and isinstance(v, float):
                    return f"{v:g}".replace(".", ",")
                return v

            table = [[cell(c, value(r, c, p)) for c in cols] for r, p in zip(rows, picks)]
            if excel:                               # for people: Spanish headers, no always-empty columns
                keep = [j for j in range(len(cols)) if any(row[j] != "" for row in table)]
                cols = [CSV_ES.get(cols[j], cols[j]) for j in keep]
                table = [[row[j] for j in keep] for row in table]
            with open(pc, "w", newline="", encoding="utf-8-sig") as fh:   # BOM: Excel opens the accents right
                w = csv.writer(fh, delimiter=";" if excel else ",")
                w.writerow(cols)
                w.writerows(table)
            shot["csv"] = str(pc)
    result = StoryboardResult(
        pages=page_results, pdf=pdf, pages_without_labels=nolabels, shot_list=shot,
        frames_dir=str(frames_dir) if frames_dir else None, videos=videos,
        color_script=str(color_path) if color_path else None, panels=results, dropped=chosen.dropped,
        selection=_selection_result(chosen),
        layout={"template": L.template, "columns": geo.cols, "rows_per_page": geo.rows_per_page,
                "picture_size": [geo.panel_w, geo.panel_h], "page": L.page, "page_size": list(pages[0].size),
                "theme": S.theme},
        input=src.result(with_items=False), warnings=chosen.warnings)
    extra = []
    if O.preview:
        extra.append(_preview(pages[0]))
        if videos:
            extra.append(_preview(Image.open(videos[0].samples)))
        if color_path:
            extra.append(_preview(color_img))
    return result, extra


def _save_still(im: Image.Image, dest: Path, fmt: str, quality: int):
    if fmt == "png":
        im.save(dest, optimize=True)
    else:
        _flatten(im).save(dest, quality=quality)


def tool_extract_frames(args: FramesArgs):
    src = make_source(args.input)
    problems = check_selection(src.kind, args.select, [])
    fmt = args.output.format or ("jpg" if src.kind == "video" else "original")
    if src.kind == "video" and fmt == "original":
        problems.append('output.format: "original" is only for images; video frames are written as "jpg" or "png"')
    if problems:
        raise ToolError(_problems_text("extract_frames", problems))
    chosen = choose(src, args.select, [])
    out_dir = _out_dir(args.output.dir, _default_dir(src, "frames"))
    results: dict[int, PanelResult] = {}
    order = {id(p): k for k, p in enumerate(chosen.picks)}
    used: set[str] = set()

    def dest_name(k: int, orig: Path, ext: str) -> str:
        if args.output.names == "numbered":
            return f"{k + 1:03d}_{orig.stem}{ext}"
        base, n = f"{orig.stem}{ext}", 2
        while base.lower() in used:
            base, n = f"{orig.stem}_{n}{ext}", n + 1
        used.add(base.lower())
        return base

    def sink(p: Pick, im: Image.Image):
        k = order[id(p)]
        if p.sharpness is None:
            p.sharpness = _sharpness(im, args.select.analysis_width)
        if src.kind == "video":
            dest = out_dir / f"f{p.key:05d}.{fmt}"
            _save_still(im, dest, fmt, args.output.quality)
            r = PanelResult(n=k + 1, number=str(k + 1), frame=p.key, time_s=round(p.t, 3), timecode=_hms(p.t),
                            pct=chosen.pct_of(p), anchor=_anchor(p.key, src), file=str(dest))
        else:
            orig = src.paths[p.key]
            if fmt == "original":
                dest = out_dir / dest_name(k, orig, orig.suffix)
                shutil.copy2(orig, dest)
            else:
                dest = out_dir / dest_name(k, orig, f".{fmt}")
                _save_still(im, dest, fmt, args.output.quality)
            r = PanelResult(n=k + 1, number=str(k + 1), image=p.key, name=orig.name, source=str(orig),
                            pct=chosen.pct_of(p), file=str(dest))
        r.sharpness, r.size = round(p.sharpness, 1), [im.width, im.height]
        _shot_fields(r, p)
        results[k] = r

    src.fetch(chosen.picks, sink)
    top = max((r.sharpness or 0) for r in results.values()) or 1.0
    for r in results.values():
        r.sharpness_pct = round(100 * (r.sharpness or 0) / top)
    return FramesResult(dir=str(out_dir), files=[results[k] for k in sorted(results)], dropped=chosen.dropped,
                        selection=_selection_result(chosen), input=src.result(with_items=False),
                        warnings=chosen.warnings), []


# ============================================================================ registry
EXAMPLES: dict[str, dict[str, Any]] = {
    "media_info": {
        "minimal": {"input": {"video": "/Users/ana/Movies/clip.mp4"}},
        "full": {"input": {"folder": "/Users/ana/Pictures/rodaje", "pattern": "*.jpg"}},
    },
    "make_storyboard": {
        "minimal": {"input": {"video": "/Users/ana/Movies/clip.mp4"}},
        "full": {
            "input": {"video": "/Users/ana/Movies/clip.mp4"},
            "select": {"at_s": [1.5, 6, 12.5, 20], "snap_s": 0.5},
            "panels": [
                {"shot": "PG", "camera": "fijo", "action": "Amanece sobre la ciudad.", "sound": "tráfico lejano"},
                {"shot": "PM", "camera": "travelling lateral", "action": "Ana cruza la calle.",
                 "dialogue": "ANA: Llego tarde.", "transition": "corte"},
                {"number": "2A", "shot": "PP", "action": "Mira el reloj.", "duration_s": 2.5},
                {"shot": "PG", "camera": "grúa ascendente", "transition": "fundido a negro"},
            ],
            "layout": {"template": "storyboard", "columns": 2, "page": "A4", "orientation": "landscape",
                       "numbering": {"start": 1, "format": "5.{n}"},
                       "label": {"fields": ["timecode", "frame"]},
                       "guides": {"aspects": ["2.39:1"], "thirds": False}, "title": "Escena 5 — La calle",
                       "language": "es"},
            "style": {"theme": "light", "accent": "#c2410c", "font": "Helvetica"},
            "output": {"dir": "/Users/ana/Desktop/storyboard", "name": "escena5", "format": "pdf", "dpi": 150,
                       "shot_list": ["json", "csv"]},
        },
        "shots": {
            "input": {"video": "/Users/ana/Movies/trailer.mp4"},
            "select": {"mode": "shots", "shot_threshold": 20, "min_shot_s": 0.4},
            "layout": {"template": "storyboard", "label": {"fields": ["shot_in", "duration", "transition"]}},
            "output": {"dir": "/Users/ana/Desktop/planos", "shot_list": ["json", "csv"], "color_script": True},
        },
        "mosaic": {
            "input": {"video": "/Users/ana/Movies/clip.mp4"},
            "select": {"count": 8},
            "output": {"dir": "/Users/ana/Desktop/mosaico", "format": "gif",
                       "animation": {"kind": "mosaic", "fps": 12, "duration_s": 6}},
        },
        "animatic": {
            "input": {"video": "/Users/ana/Movies/clip.mp4"},
            "select": {"at_s": [1.5, 6, 12.5], "snap_s": 0.5},
            "panels": [{"shot": "PG", "action": "Amanece.", "duration_s": 3, "transition": "encadenado"},
                       {"shot": "PM", "dialogue": "ANA: Llego tarde.", "duration_s": 2},
                       {"shot": "PP", "action": "Mira el reloj.", "transition": "fundido a negro"}],
            "layout": {"template": "storyboard"},
            "output": {"dir": "/Users/ana/Desktop/animatica", "format": "mp4",
                       "animation": {"kind": "animatic", "panel_s": 2, "fade_s": 0.5}},
        },
    },
    "extract_frames": {
        "minimal": {"input": {"video": "/Users/ana/Movies/clip.mp4"}, "select": {"at_pct": [0, 50, 100]}},
        "full": {"input": {"images": ["/Users/ana/burst/IMG_1.jpg", "/Users/ana/burst/IMG_2.jpg",
                                      "/Users/ana/burst/IMG_3.jpg"]},
                 "select": {"mode": "sharpest", "count": 1},
                 "output": {"dir": "/Users/ana/Desktop/mejor", "format": "original"}},
    },
}


@dataclass
class ToolSpec:
    args: type[BaseModel]
    result: type[BaseModel] | None
    fn: Callable
    description: str
    read_only: bool


TOOLS: dict[str, ToolSpec] = {
    "media_info": ToolSpec(MediaInfoArgs, MediaInfoResult, tool_media_info, (
        "What the input is. Video: duration, fps, number of frames, size, rotation and codec (fast: it does "
        "not decode the video). Images: how many, and each one's index, name, format and size. Call it first "
        "to know the limits for positions (frames 0 to frames - 1). An image with duplicate_of has exactly the "
        "same bytes as the earlier image of that index.\n"
        'Example: {"input": {"video": "/abs/clip.mp4"}} or {"input": {"folder": "/abs/photos"}}.'), True),
    "make_storyboard": ToolSpec(StoryboardArgs, StoryboardResult, tool_make_storyboard, (
        "Turn a video or a list of images into pages of panels you can look at, plus one JSON entry per "
        "panel. Templates (layout.template): grid (contact sheet, the default), storyboard (number above each "
        "picture, notes box below), list (picture left, notes right), strip (one row). Pages as PNG or JPG "
        "files, or one multi-page PDF (output.format); optional shot list (JSON / CSV) and full-size frames.\n"
        'Only input is required: {"input": {"video": "/abs/clip.mp4"}} gives a grid of the 12 sharpest '
        "frames (one per stretch of the video). The answer lists the page files (open them to see the "
        "panels; page 1 also comes as an image) and, per panel, its number, page, frame and time (video) or "
        "image index and name (images), sharpness and notes.\n"
        "To write a storyboard: look at a first result, then call again with the same select plus panels[] "
        "(one object per panel, in order: shot, camera, action, dialogue, sound, transition, duration_s) and "
        'layout.template "storyboard"; or give each panel its own position (frame, at_s, at_pct, image, '
        "image_name).\n"
        "Recipes: 8 sharp, different frames: "
        '{"select": {"count": 8, "min_difference": 3}}. One every 10 %: {"select": {"step_pct": 10}}. '
        'The 4 sharpest photos, no repeats: {"input": {"folder": "/abs/photos"}, "select": {"mode": "best", '
        '"count": 4}}. Printable storyboard: {"layout": {"template": "storyboard", "page": "A4"}, '
        '"style": {"theme": "light"}, "output": {"format": "pdf", "shot_list": ["csv"]}} (add "csv_style": '
        '"excel_es" for Excel in Spanish).\n'
        'One panel per shot: {"select": {"mode": "shots"}}: each panel gets shot_in_s, shot_out_s, duration_s '
        'and transition_in / transition_out (cut, fade_in / fade_out, dissolve).\n'
        'Animated: "output": {"format": "gif"} (or "mp4", "webm", "webp"). With a video, the page itself with '
        'every panel playing its part of the video (mosaic); with images, a timed slideshow; "animation": '
        '{"kind": "animatic"} plays the panels one after another with their duration_s and transition. You '
        "cannot watch it: the answer has the still page and videos[].samples, an image of sample frames.\n"
        'Colour script: "output": {"color_script": true} (dominant colours per panel).\n'
        'If fewer panels than asked come out, warnings says why and what to change. get_schema {"tool": '
        '"make_storyboard"} has full examples.'), False),
    "extract_frames": ToolSpec(FramesArgs, FramesResult, tool_extract_frames, (
        "Save chosen video frames at full size (JPEG or PNG), or copies of chosen images, to look at details "
        "the panels are too small to show. Same input and select as make_storyboard, e.g. "
        '{"input": {"video": "/abs/clip.mp4"}, "select": {"at_frames": [120, 480]}} or '
        '{"select": {"at_s": [12.5], "snap_s": 0.5}} (the sharpest frame within ± 0.5 s). Frames are saved '
        "upright (rotation applied), at displayed_size, as f<frame, 5 digits>.jpg (frame 42 -> f00042.jpg). "
        'Images are copied byte for byte as 001_<name>, 002_<name>... (output.names "original" keeps the '
        'names). {"select": {"mode": "shots"}} saves the sharpest frame of each shot, with the shot\'s start, '
        "end and transitions. Each entry of files gives its path (file) and size in px."), False),
    "get_schema": ToolSpec(SchemaArgs, None, lambda a: (schema_document(a.tool), []), (
        "The JSON Schema of the tools' arguments and answers, with examples of each (minimal and full; for "
        "make_storyboard also shots, mosaic and animatic), and the conventions (paths, positions, defaults, "
        "limits). The same information the tools' input schemas give, in one document. "
        '{"tool": "make_storyboard"} returns only that tool.'), True),
}


def _clean_schema(s):
    """Drop pydantic's "title" keys: they repeat the field names."""
    if isinstance(s, dict):
        return {k: _clean_schema(v) for k, v in s.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(s, list):
        return [_clean_schema(x) for x in s]
    return s


def input_schema(name: str) -> dict:
    return _clean_schema(TOOLS[name].args.model_json_schema())


def output_schema(name: str) -> dict | None:
    r = TOOLS[name].result
    return _clean_schema(r.model_json_schema(mode="serialization")) if r else None


CONVENTIONS = [
    "Every tool takes the same sections: input (required), select, panels, layout, style, output. "
    "Unknown keys are rejected with the list of allowed ones.",
    "input: exactly one of video (path), images (list of paths) or folder (path, with optional pattern).",
    "Paths: absolute or ~/... A relative path is resolved against the server's working directory.",
    "Video positions: frame numbers 0..frames-1; seconds from the first frame (0.0); % from the first "
    "frame (0) to the last (100). Images: index 0..count-1, file name, or % of the list.",
    "Colours: #RRGGBB, #RRGGBBAA, #RGB, #RGBA or a CSS name. Sizes in px.",
    "Defaults: video -> the sharpest frame of each of 12 stretches; images -> all; layout grid with 4 "
    "columns; theme dark (black page); output png in a temporary folder named in the answer.",
    "Answers are JSON (also as structured content). make_storyboard also returns a small image of page 1.",
    "Animated output (output.format mp4, webm, gif, webp) always comes with the still pages as PNG, an image of "
    f"sample frames (videos[].samples) and the JSON. Limits: {MAX_VIDEO_S} s, {MAX_VIDEO_FRAMES} frames, 30 fps, "
    "3840 px (gif and webp 1280 px).",
]


def schema_document(tool: str | None = None) -> dict:
    doc = {"server": "storyboard", "version": VERSION, "conventions": CONVENTIONS, "tools": {}}
    for name, spec in TOOLS.items():
        if name == "get_schema" or (tool and name != tool):
            continue
        doc["tools"][name] = {"description": spec.description, "input_schema": input_schema(name),
                              "output_schema": output_schema(name), "examples": EXAMPLES[name]}
    return doc


def run_tool(name: str, arguments: dict | None) -> tuple[dict, list]:
    """Validate the arguments and run a tool; returns (JSON answer, extra content).
    Raises ToolError with a message meant for the caller."""
    if name not in TOOLS:
        close = difflib.get_close_matches(name, list(TOOLS), n=1)
        raise ToolError(f'unknown tool "{name}"' + (f'; did you mean "{close[0]}"?' if close else "")
                        + f". Tools: {', '.join(TOOLS)}")
    spec = TOOLS[name]
    try:
        args = spec.args.model_validate(arguments or {})
    except ValidationError as e:
        raise ToolError(format_validation_error(name, spec.args, e)) from None
    result, extra = spec.fn(args)
    data = result.model_dump(mode="json", exclude_none=True) if isinstance(result, BaseModel) else result
    return data, extra


# ============================================================================ MCP
server = Server("storyboard", version=VERSION, instructions=INSTRUCTIONS)


@server.list_tools()
async def _list_tools() -> list[mt.Tool]:
    return [mt.Tool(name=name, description=spec.description, inputSchema=input_schema(name),
                    outputSchema=output_schema(name),
                    annotations=mt.ToolAnnotations(readOnlyHint=spec.read_only, destructiveHint=False,
                                                   idempotentHint=True, openWorldHint=False))
            for name, spec in TOOLS.items()]


@server.call_tool(validate_input=False)
async def _call_tool(name: str, arguments: dict) -> mt.CallToolResult:
    try:
        data, extra = await anyio.to_thread.run_sync(lambda: run_tool(name, arguments))
    except ToolError as e:
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=str(e))], isError=True)
    except Exception as e:  # a bug or an unreadable file: say what failed
        return mt.CallToolResult(content=[mt.TextContent(type="text", text=f"{name} failed: "
                                                                           f"{type(e).__name__}: {e}")],
                                 isError=True)
    text = mt.TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=1))
    return mt.CallToolResult(content=[text, *extra], structuredContent=data if TOOLS[name].result else None,
                             isError=False)


@server.list_resources()
async def _list_resources() -> list[mt.Resource]:
    return [mt.Resource(uri="storyboard://schema", name="schema", title="storyboard JSON Schema",
                        mimeType="application/json",
                        description="JSON Schema of every tool's arguments and answers, with examples.")]


@server.read_resource()
async def _read_resource(uri) -> list[ReadResourceContents]:
    if str(uri) != "storyboard://schema":
        raise ValueError(f"unknown resource {uri}; the only one is storyboard://schema")
    return [ReadResourceContents(content=json.dumps(schema_document(), ensure_ascii=False, indent=1),
                                 mime_type="application/json")]


async def _serve():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


# ----------------------------------------------------------------------------- bundled ffmpeg
FFMPEG = Path(__file__).resolve().parent / "bin" / "ffmpeg"
FFMPEG_REPO = "FFmpeg/FFmpeg"  # official GitHub mirror of git.ffmpeg.org


def _version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag.lstrip("n").split("."))


def _bundled_ffmpeg_tag() -> str:
    """Release tag of bin/ffmpeg ("n9.0.2"), as `ffmpeg -version` reports it."""
    out = subprocess.run([str(FFMPEG), "-hide_banner", "-version"], capture_output=True,
                         text=True, timeout=30, check=True).stdout
    m = re.match(r"ffmpeg version (n?\d+(?:\.\d+)+)\b", out)
    if not m:
        first = out.splitlines()[0] if out else ""
        raise ValueError(f"`ffmpeg -version` no dice una versión publicada: {first!r}")
    v = m.group(1)
    return v if v.startswith("n") else "n" + v


def _latest_ffmpeg_tag() -> str:
    """Newest release tag on the official GitHub repository: nX.Y or nX.Y.Z, -dev tags left out.

    One request lists every tag starting with "n", so the check costs one of the 60
    requests an hour GitHub allows without a token (GITHUB_TOKEN is used if set)."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{FFMPEG_REPO}/git/matching-refs/tags/n",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "storyboard-mcp"})
    if os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    with urllib.request.urlopen(req, timeout=30) as r:
        refs = json.load(r)
    tags = [ref["ref"].rsplit("/", 1)[-1] for ref in refs]
    releases = [t for t in tags if re.fullmatch(r"n\d+\.\d+(?:\.\d+)?", t)]
    if not releases:
        raise ValueError("el repositorio no tiene etiquetas de versión publicadas")
    return max(releases, key=_version_key)


def is_ffmpeg_updated() -> int:
    """--is-ffmpeg-updated: 0 when bin/ffmpeg is the newest FFmpeg release on the official
    GitHub repository, 1 when there is a newer one, 2 when it cannot be told."""
    try:
        have = _bundled_ffmpeg_tag()
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"No se puede ejecutar {FFMPEG}: {e}", file=sys.stderr)
        return 2
    try:
        latest = _latest_ffmpeg_tag()
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"No se pudo consultar github.com/{FFMPEG_REPO}: {e}", file=sys.stderr)
        return 2
    if _version_key(have) >= _version_key(latest):
        print(f"bin/ffmpeg está al día: es {have}, la última versión publicada en "
              f"github.com/{FFMPEG_REPO}.")
        return 0
    print(f"Hay una versión más nueva: bin/ffmpeg es {have} y la última publicada en "
          f"github.com/{FFMPEG_REPO} es {latest}.\nPara actualizarlo: ./build-ffmpeg.sh {latest}")
    return 1


if __name__ == "__main__":
    if "--is-ffmpeg-updated" in sys.argv[1:]:
        sys.exit(is_ffmpeg_updated())
    if "--schema" in sys.argv[1:]:
        print(json.dumps(schema_document(), ensure_ascii=False, indent=1))
        sys.exit(0)
    anyio.run(_serve)
