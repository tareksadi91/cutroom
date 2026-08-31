#!/usr/bin/env python3
# cutroom — a local timeline that cannot harm your media.
# Copyright (C) 2026 Tarek Sadi
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. It is distributed WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
# the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/>.
"""Render a project to a film. The project JSON is the single source of truth.

    ./render.py ~/cutroom-projects/myfilm.json
    ./render.py ~/cutroom-projects/myfilm.json --from 12 --to 30 \\
        -o ~/cutroom-projects/myfilm/renders/part.mp4

-o is not a free hand: like every other write in cutroom it goes through
server.writable(), so it must land inside ~/cutroom-projects/ and on a name
nothing is using. Left off, the render goes to the project's own renders/.

Lanes are organizational. Overlap means crossfade, same lane or not, and the
renderer flattens every clip into one time-ordered chain.

A clip names its footage by `mid`, never by path. `media` maps mid -> an
absolute path plus what ffprobe last said about it, and it is also the
allowlist: a path is readable because the director put it there, and for no
other reason. This module never scans, walks or discovers anything, and it
opens a source read-only. Its only output is the file it was asked to write.
"""
import argparse
import json
import math
import os
import pathlib
import subprocess
import sys

ENCODE = ["-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", "-an"]
EPS = 1e-6  # float slop; timeline values come from a browser


def load(path):
    """Read a project, with every near-grid value at its exact frame time.

    The canonicalising happens here rather than in the caller because every
    path into the renderer goes through this function or through render(), and
    a value that reaches the graph nominally is a value the graph can read two
    ways. See canon_clip().
    """
    return canonicalise(json.loads(pathlib.Path(path).read_text()))


# An edit point is a FRAME, not a float. Everything below rests on that, and
# validate() enforces it, because three rounds of chasing the arithmetic of
# trim -> setpts -> fps proved the alternative: every attempt to MODEL what
# ffmpeg does with an off-grid edit point fixed one case and moved the error to
# another. A real NLE does not offer the choice, and neither does this one.
# In FRAMES. 0.02 of a frame is 0.8ms at 24fps: it accepts a frame value
# written with three decimal places (1.417s is 34.008 frames — the project is a
# hand-editable file and must not require sixteen digits to stay legal) and it
# refuses every real edit point off the grid, the smallest of which,
# in=0.01, is a quarter of a frame out.
GRID_SLOP = 0.02


def frames_at(seconds, fps):
    """`seconds` as a frame index on the fps grid, rounded to the nearest."""
    return round(seconds * fps)


def on_grid(seconds, fps):
    return abs(seconds * fps - round(seconds * fps)) <= GRID_SLOP


def snap(seconds, fps):
    """The nearest frame boundary, as seconds.

    Six decimal places, not sixteen: a saved project is read and edited by
    hand, and 1.416667 names frame 34 exactly as well as 1.4166666666666667
    does — to within a thousandth of GRID_SLOP.
    """
    return round(round(seconds * fps) / fps, 6)


def snap_project(project):
    """A copy of `project` with every edit point on the frame grid.

    The server snaps every incoming save before validating it, so a browser
    drag — which produces a float from a pixel — can never write an off-grid
    value, and the grid rule in validate() only ever fires on a file somebody
    edited by hand.
    """
    fps = project["fps"]
    out = dict(project)
    out["clips"] = [dict(c, **{k: snap(c[k], fps) for k in ("t", "in", "out")})
                    for c in project["clips"]]
    return out


def canon_clip(clip, fps):
    """One clip with every near-grid edit point replaced by its exact frame time.

    ACCEPTANCE and COMPUTATION are two different questions, and conflating them
    is what put the ambiguity back. GRID_SLOP exists so a hand-written 1.417 is
    accepted as frame 34 — but computing from the nominal 1.417 leaves exactly
    the ambiguity the grid rule removed: at 24fps `out=0.416` and `out=0.417`
    both name frame 10 and both pass, and the raw graph emits 10 frames for one
    and 11 for the other (measured). So a value is either REFUSED, or it is the
    exact frame time by the time anything computes with it.

    A value further from the grid than GRID_SLOP is left alone, deliberately:
    it is not this function's business to move a real edit point, it is
    validate()'s business to refuse it.
    """
    return dict(clip, **{k: (frames_at(clip[k], fps) / fps
                             if on_grid(clip[k], fps) else clip[k])
                         for k in ("t", "in", "out")})


def canonicalise(project):
    """`project` with every near-grid edit point at its exact frame time.

    Applied at every door into the graph — load(), render(), build_graph() —
    rather than only on the way in through PUT /project, so a hand-edited file,
    the CLI, /render and a history restore all compute on the same values.
    """
    fps = project["fps"]
    return dict(project, clips=[canon_clip(c, fps) for c in project["clips"]])


def src_frames(clip, fps=24):
    """How many source frames `trim=start=in:end=out` keeps.

    Exactly (out - in) * fps. No ceilings, no rounding mode, no model of
    ffmpeg's boundary behaviour — because validate() guarantees both endpoints
    are frames. Off the grid this is only the nearest whole answer, which is
    what the grid rule exists to prevent.
    """
    return frames_at(clip["out"], fps) - frames_at(clip["in"], fps)


def duration(clip, fps=24):
    """On-screen seconds, quantised to the frame grid ffmpeg will emit.

    src_frames() source frames, then setpts divides by rate and the fps filter
    requantises to floor(frames / rate + 0.5).

    HONESTLY: at rate == 1.0 this is exact, by construction and by measurement
    (frames 0->24, 0->12 and 6->18 each emitted exactly their own count through
    the full graph). At rate != 1.0 it can be off BY ONE FRAME, because the fps
    filter's resample is a genuine requantisation of shifted timestamps and
    this is not a model of it. floor(x + 0.5) matched all 11 on-grid cases
    measured here (ffmpeg 8.1.1) where plain round() missed 3 exact halves, but
    a retimed clip landing one frame from its predicted length is a known
    residual, not a bug to be surprised by. It costs a frame of black or a
    frame of overlap at one cut; nothing accumulates, because every clip's t is
    absolute.
    """
    return math.floor(src_frames(clip, fps) / clip["rate"] + 0.5) / fps


# ffprobe is the only way to know what a source really is, and validate() runs
# before every export. Cache on (path, mtime_ns, size) so a timeline of 27
# clips costs one probe each per change, not one per drag.
_probe_cache = {}


def source_info(path):
    """{"dur", "fps", "avg_fps", "w", "h"}, or None if ffprobe cannot say.

    None is a REFUSAL, not permission — see validate(). A source nobody can
    probe is a source whose frame count nobody can predict, and this renderer's
    whole arithmetic is a frame count.
    """
    path = pathlib.Path(path)
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _probe_cache:
        _probe_cache[key] = _probe_source(path)
    return _probe_cache[key]


def _probe_source(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,duration,r_frame_rate,avg_frame_rate",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True).stdout.strip().split(",")
    except (subprocess.CalledProcessError, OSError):
        return None
    # csv order follows the STREAM field order, not the -show_entries order:
    # width, height, r_frame_rate, avg_frame_rate, duration. BOTH rates are
    # kept. r_frame_rate is nominal — a variable-rate file can claim 24/1 there
    # while averaging 10 real frames a second, and a frame-grid model would
    # accept it and cut nothing like what it predicted. validate() refuses when
    # they disagree; here they are only reported.
    def rate(text):
        try:
            num, _, den = text.partition("/")
            r = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            return None
        return r if r > 0 else None

    def whole(text):
        try:
            return int(text)
        except ValueError:
            return None

    if len(out) < 5:
        return None
    w, h = whole(out[0]), whole(out[1])
    r_fps, avg_fps = rate(out[2]), rate(out[3])
    try:
        seconds = float(out[4])
    except ValueError:
        seconds = None
    if seconds is None or r_fps is None:
        return None
    return {"dur": seconds, "fps": r_fps, "avg_fps": avg_fps, "w": w, "h": h}


def source_frames(path):
    """How many video frames `path` ACTUALLY decodes, or None.

    Nothing else here is allowed to answer this question. A container's
    `duration` and its `nb_frames` are both CLAIMS, and both overstate: one file
    in the courier reports duration 3.194987s (76.7 frames) and nb_frames 77,
    while it decodes 76. A timeline built on either number owns a frame that
    does not exist — the preview plays past the end and goes black, and
    validate() refuses the same clip at export. Decoding is the only proof, and
    it costs ~0.8s for an 8s clip on a path that runs once per file.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    try:
        n = int(out.split(",")[0])
    except (ValueError, IndexError):
        return None
    return n if n > 0 else None


def source_duration(path):
    """Seconds of video in `path`, or None. Kept as its own name because that
    is what most callers want to ask."""
    info = source_info(path)
    return info["dur"] if info else None


# ------------------------------------------------------------------ the media
# A clip carries `mid`. `media` maps it to a path. Nothing else does, and
# nothing here ever turns a path back into permission — see server.servable().

def media_index(project):
    return {m["mid"]: m for m in project.get("media", [])}


def clip_path(project, clip, index=None):
    """The absolute path a clip's media names, or None if the mid is unknown.

    Unknown mid and missing file are two different states and both are
    survivable: the timeline draws the clip OFFLINE and only export is blocked.
    Neither ever rewrites the project or drops the clip.
    """
    entry = (index if index is not None else media_index(project)).get(clip.get("mid"))
    return entry["path"] if entry else None


def offline(project):
    """[(uid, what is wrong)] for every clip whose footage cannot be read now.

    Deliberately cheap — a stat, not a probe — because the UI asks on every
    load and a missing file must never cost a decode.
    """
    index = media_index(project)
    out = []
    for c in project["clips"]:
        entry = index.get(c.get("mid"))
        if entry is None:
            out.append((c["uid"], f"unknown media {c.get('mid')!r}"))
        elif not pathlib.Path(entry["path"]).is_file():
            out.append((c["uid"], f"missing {entry['path']}"))
    return out


def shape_problems(project):
    """Is this a project AT ALL — the question that comes before validate().

    validate() answers "is this cut renderable", and every one of its rules
    indexes a field: c["out"], c["rate"], project["fps"]. Handed a document
    that is merely malformed — `{"clips": [{}]}` from a hand-written PUT — it
    raised KeyError from inside the validator, which reached the HTTP layer as
    a 500 for what is plainly a 400. Types and presence are checked here first,
    so nothing downstream ever indexes a field that might not be there.

    Deliberately shallow: it says nothing about whether the cut makes sense.
    That is validate()'s job and it is a different job.
    """
    if not isinstance(project, dict):
        return [f"a project must be a JSON object, got {type(project).__name__}"]

    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    problems = []
    fps = project.get("fps")
    if not num(fps) or fps <= 0:
        problems.append(f"fps must be a positive number, got {fps!r}")
    res = project.get("resolution")
    if not (isinstance(res, list) and len(res) == 2
            and all(num(v) and v > 0 for v in res)):
        problems.append(f"resolution must be [width, height], got {res!r}")

    media = project.get("media", [])
    if not isinstance(media, list):
        problems.append(f"media must be a list, got {media!r}")
    else:
        for i, m in enumerate(media):
            if not (isinstance(m, dict) and isinstance(m.get("mid"), str)
                    and isinstance(m.get("path"), str)):
                problems.append(
                    f"media[{i}] must be an object with a string mid and path, got {m!r}")

    clips = project.get("clips")
    if not isinstance(clips, list):
        return problems + [f"clips must be a list, got {clips!r}"]
    for i, c in enumerate(clips):
        if not isinstance(c, dict):
            problems.append(f"clips[{i}] must be an object, got {c!r}")
            continue
        where = f"clips[{i}]" if not isinstance(c.get("uid"), str) else c["uid"]
        for field in ("uid", "mid"):
            if not isinstance(c.get(field), str):
                problems.append(f"{where}: {field} must be a string, got {c.get(field)!r}")
        for field in ("t", "in", "out", "rate"):
            if not num(c.get(field)):
                problems.append(f"{where}: {field} must be a number, got {c.get(field)!r}")
    return problems


def validate(project, check_files=True):
    """Every rule that makes a project renderable. Empty list means valid.

    Lives here rather than in the server so the same rules apply whether a
    change arrived from a drag or from the agent writing JSON by hand.

    check_files=False is the SAVE path and check_files=True is the EXPORT path,
    and the difference is deliberate: a source that has moved must not stop the
    director from cutting, so a missing file is a problem only when a render
    depends on it.
    """
    problems = []
    fps = project["fps"]
    index = media_index(project)
    clips = sorted(project["clips"], key=lambda c: c["t"])

    seen = set()
    valid_clips_indices = []
    for i, c in enumerate(clips):
        # `c` is what the file says and is what the messages quote; `cc` is what
        # anything COUNTING has to use. See canon_clip(): out=0.416 and
        # out=0.417 both name frame 10 and both pass, so the frame count must
        # come from the frame, not from the float that named it.
        cc = canon_clip(c, fps)
        if c["uid"] in seen:
            problems.append(f"duplicate uid {c['uid']} — uid is the address and must be unique")
        seen.add(c["uid"])
        if c["out"] <= c["in"]:
            problems.append(f"{c['uid']}: out ({c['out']}) is not after in ({c['in']})")
        if c["rate"] <= 0:
            problems.append(f"{c['uid']}: rate must be positive, got {c['rate']}")
        # A negative t or in is not a cut the graph can express: ffmpeg clamps
        # a negative trim start to 0 and a negative t would place a clip before
        # the film starts, so the fold's arithmetic and the output diverge
        # silently — the one failure mode this validator exists to prevent.
        if c["t"] < 0:
            problems.append(f"{c['uid']}: t must not be negative, got {c['t']}")
        if c["in"] < 0:
            problems.append(f"{c['uid']}: in must not be negative, got {c['in']}")
        # An edit point is a frame. See the GRID_SLOP comment: this rule is what
        # lets src_frames() be arithmetic instead of a model of ffmpeg. The
        # server snaps every save, so this fires on a hand-edited file.
        for field in ("t", "in", "out"):
            if not on_grid(c[field], fps):
                problems.append(
                    f"{c['uid']}: {field} {c[field]!r} is not on the {fps}fps frame "
                    f"grid ({c[field] * fps:.3f} frames) — nearest frame is "
                    f"{frames_at(c[field], fps)} at {snap(c[field], fps):.6f}s")
        # A mid that names nothing is a broken reference at any time — it is not
        # a file that moved, it is a clip pointing at media the project does not
        # have — so it is reported on the save path too.
        entry = index.get(c.get("mid"))
        if entry is None:
            problems.append(
                f"{c['uid']}: mid {c.get('mid')!r} is not in this project's media — "
                f"a clip may only name footage the director added")
        elif check_files:
            src = pathlib.Path(entry["path"])
            name = src.name
            if not src.is_file():
                problems.append(
                    f"{c['uid']}: source missing — {entry['path']} (the clip is OFFLINE; "
                    f"re-point it or put the file back, nothing has been changed)")
            else:
                info = source_info(src)
                if info is None:
                    # Not permission. Every number this renderer computes is a
                    # frame count off this file; if ffprobe cannot read it, the
                    # graph is guessing.
                    problems.append(
                        f"{c['uid']}: ffprobe cannot read {name} — a source whose "
                        f"frame count cannot be measured cannot be cut")
                else:
                    # A trim past the end of the file: ffmpeg clamps it without
                    # a word, the segment comes out short, and every xfade
                    # offset after it moves.
                    #
                    # Compared in FRAMES, because that is the unit that goes
                    # wrong. A whole frame of slack let out=1.040 pass against a
                    # 1.000s source and duration() then modelled 25 frames where
                    # ffmpeg emits 24. The trim may claim no more frames than
                    # the file actually holds.
                    # COUNTED, falling back to the duration. probe() records a
                    # media's length from a decode, so a project can legally
                    # hold a clip whose out is the 248th frame of a file whose
                    # CONTAINER says 10.333008s — which floors to 247 and
                    # refused a cut that renders perfectly. The two halves of
                    # this program have to count frames the same way.
                    have = source_frames(src)
                    if have is None:
                        have = math.floor(info["dur"] * fps + 1e-6)
                    want = math.ceil(cc["out"] * fps - 1e-9)
                    if want > have:
                        problems.append(
                            f"{c['uid']}: out {c['out']:.3f}s claims {want} frames of "
                            f"{name} but it holds {have} ({info['dur']:.3f}s) — retrim it")
                    # And the assumption duration() rests on, made explicit.
                    # Same trim, same rate, 14 frames from a 24fps source and 13
                    # from a 25fps one. Mixed rates are not modelled here and
                    # must not be guessed at.
                    if abs(info["fps"] - fps) > 0.01:
                        problems.append(
                            f"{c['uid']}: {name} is {info['fps']:.3f}fps but the project "
                            f"is {fps}fps — the cut's frame arithmetic assumes one "
                            f"grid; conform the source first")
                    # Nominal rate is a claim, average rate is what the file
                    # actually does. A variable-rate source satisfies no
                    # frame-grid model, so it is refused rather than cut badly.
                    elif (info.get("avg_fps") is not None
                          and abs(info["fps"] - info["avg_fps"]) > 0.01):
                        problems.append(
                            f"{c['uid']}: {name} claims {info['fps']:.3f}fps but "
                            f"averages {info['avg_fps']:.3f} — a variable frame rate "
                            f"cannot sit on a frame grid; conform the source first")
        # Only include clip in overlap checks if it passed per-clip validation
        if c["out"] > c["in"] and c["rate"] > 0 and c["t"] >= 0 and c["in"] >= 0:
            valid_clips_indices.append(i)

    # Use only valid clips for overlap checks to avoid calling duration() on
    # invalid clips — and the CANONICAL ones, so the geometry below measures
    # frames rather than the floats that named them.
    valid_clips = [canon_clip(clips[i], fps) for i in valid_clips_indices]

    # Two clips starting at the exact same instant collapse to a zero-length
    # shot: the overlap equals the first clip's whole duration, so the xfade
    # offset lands at 0 and consumes it entirely rather than showing it first.
    for a, b in zip(valid_clips, valid_clips[1:]):
        if abs(a["t"] - b["t"]) <= EPS:
            problems.append(
                f"{a['uid']}/{b['uid']}: both start at t={a['t']:.3f} — a full overlap "
                f"collapses to a zero-length shot for {a['uid']}")

    for a, b in zip(valid_clips, valid_clips[1:]):
        ov = (a["t"] + duration(a, fps)) - b["t"]
        if ov > EPS:
            shortest = min(duration(a, fps), duration(b, fps))
            if ov > shortest + EPS:
                problems.append(
                    f"{a['uid']}/{b['uid']}: overlap {ov:.2f}s exceeds the shorter clip "
                    f"({shortest:.2f}s) — an xfade cannot outlast its shortest input")

    # Renderer assumes: no nesting and no clip reaching past its successor.
    # Check nesting: for consecutive clips, end(a) <= end(b) + EPS
    for a, b in zip(valid_clips, valid_clips[1:]):
        if a["t"] + duration(a, fps) > b["t"] + duration(b, fps) + EPS:
            problems.append(
                f"{a['uid']} contains {b['uid']} — a clip nested inside another cannot be "
                f"expressed in a crossfade chain; move or trim one")

    # Check reach-past: for clips two apart, end(a) <= c["t"] + EPS
    for a, b, c in zip(valid_clips, valid_clips[1:], valid_clips[2:]):
        if a["t"] + duration(a, fps) > c["t"] + EPS:
            problems.append(
                f"{a['uid']} reaches past {b['uid']} into {c['uid']} — a crossfade chain is "
                f"pairwise, so a clip may only overlap its immediate neighbour")

    return problems


def build_graph(project):
    """Return (ffmpeg inputs, filter_complex, final label).

    One graph, one encode.

    Every gap is quantised to whole frames before it becomes a filler, for the
    same reason duration() is: the timeline arrives from a browser drag and
    carries float slop. A raw 9.99e-05s gap is not a gap, it is rounding — and
    handing it to ffmpeg as d=9.999999999998899e-05 kills the whole render
    ("Unable to parse \"d\" option value"). A 0.001-0.02s gap parses fine and
    is worse: it renders one real black frame in the middle of a clean cut.
    Sub-frame slop therefore falls through to the abut branch and disappears.
    """
    project = canonicalise(project)  # trim endpoints are frames here, never floats
    w, h = project["resolution"]
    fps = project["fps"]
    index = media_index(project)
    clips = sorted(project["clips"], key=lambda c: c["t"])

    inputs, chains = [], []
    for i, c in enumerate(clips):
        inputs += ["-i", str(clip_path(project, c, index))]
        chains.append(
            f"[{i}:v]"
            f"trim=start={c['in']}:end={c['out']},"
            f"setpts=(PTS-STARTPTS)/{c['rate']},"
            f"scale={w}:{h}:flags=lanczos:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
            f"fps={fps},"
            f"format=yuv420p,"
            # xfade refuses to mix inputs on different timebases, and concat's
            # output timebase (microseconds) never matches a source's (1/fps)
            # — normalise every segment and every accumulator to one timebase.
            f"settb=AVTB"
            f"[v{i}]")

    def black(seconds, label):
        chains.append(
            f"color=c=black:s={w}x{h}:d={seconds}:r={fps},format=yuv420p,settb=AVTB[{label}]")

    acc_label, acc = None, 0.0
    lead_frames = round(clips[0]["t"] * fps) if clips else 0
    if lead_frames > 0:
        lead = lead_frames / fps
        black(lead, "lead")
        acc_label, acc = "lead", lead

    for i, c in enumerate(clips):
        d = duration(c, fps)
        if acc_label is None:
            acc_label, acc = f"v{i}", d
            continue
        # Quantise first, then branch off the frame count — never off the float.
        gap_frames = round((c["t"] - acc) * fps)
        gap = gap_frames / fps
        nxt = f"m{i}"
        if gap_frames > 0:
            black(gap, f"g{i}")
            chains.append(f"[{acc_label}][g{i}]concat=n=2:v=1:a=0,settb=AVTB[c{i}]")
            chains.append(f"[c{i}][v{i}]concat=n=2:v=1:a=0,settb=AVTB[{nxt}]")
            acc += gap + d
        elif gap_frames < 0:
            ov = -gap
            chains.append(
                f"[{acc_label}][v{i}]"
                f"xfade=transition=fade:duration={ov}:offset={acc - ov},settb=AVTB[{nxt}]")
            acc += gap + d
        else:
            chains.append(f"[{acc_label}][v{i}]concat=n=2:v=1:a=0,settb=AVTB[{nxt}]")
            acc += d
        acc_label = nxt

    return inputs, ";".join(chains), acc_label


def timeline_length(project):
    """Predicted seconds of output, on the frame grid. What the UI shows and
    what an export must match."""
    fps = project["fps"]
    project = canonicalise(project)
    if not project["clips"]:
        return 0.0
    return max(c["t"] + duration(c, fps) for c in project["clips"])


def claim_output(out_path):
    """Create `out_path`, exclusively, and hand it back. The name is now ours.

    O_EXCL rather than "check, then let ffmpeg use -n", for two reasons that
    both bit:

      - ffmpeg 8.1.1 answers -n on an existing file with "File already exists.
        Exiting." and an EXIT CODE OF ZERO (measured). It protects the file and
        reports success, so -n alone is a guarantee whose failure is invisible.
      - a check followed by a subprocess is check-then-use: between deciding
        the name is free and ffmpeg opening it, something else can take it.
        O_EXCL makes taking the name and proving it was free the same syscall.

    O_NOFOLLOW too, so the name cannot be a symlink at something else. ffmpeg
    then overwrites the empty file we just made and nothing else — which is why
    the encoder is invoked with -y here and it is still true that cutroom never
    replaces a file it did not create one syscall earlier.

    ⚠️ RESIDUAL: ffmpeg does its own open, so the window between this create
    and that open cannot be closed from here. See server.claim().
    """
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(out_path,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except FileExistsError:
        raise FileExistsError(
            f"{out_path} already exists — cutroom never overwrites a file; "
            f"render to a name nothing is using")
    os.close(fd)
    return out_path


def render(project, out_path, t_from=None, t_to=None, claimed=False):
    """Render `project` into `out_path`.

    `claimed` says the caller already created out_path with O_CREAT|O_EXCL
    (server.export_path does, so that the name is reserved across processes
    before this is called). Left False, this function claims it itself, which
    is what makes the CLI and the tests obey the same no-overwrite rule.
    """
    out_path = pathlib.Path(out_path)
    # Every door into the graph canonicalises, not just load(): render() is
    # called directly by the server, by the tests and by main().
    project = canonicalise(project)
    problems = validate(project)
    # Raise, don't sys.exit: this is a library function and its callers include
    # a server handler that has to turn a bad project into a response, not die.
    if problems:
        raise ValueError("this cut will not render:\n  " + "\n  ".join(problems))
    if not project["clips"]:
        raise ValueError("the timeline has no clips.")

    inputs, graph, label = build_graph(project)
    # NOT a bug when a range render is slow: -ss/-to sit AFTER -filter_complex,
    # so they are output-side seeks. Every source is still decoded from t=0 and
    # pushed through the whole graph; only the muxing is trimmed. Input-side -ss
    # can't work here — one filter_complex spans every input, and each input
    # needs its own offset, so seeking the inputs would shift the cut. Making a
    # partial render actually cheap means splitting into one ffmpeg per segment,
    # which is a different design, not a flag.
    trim = []
    if t_from is not None:
        trim += ["-ss", str(t_from)]
    if t_to is not None:
        trim += ["-to", str(t_to)]

    # An output name that is already taken is refused HERE, before ffmpeg is
    # started, by taking the name atomically rather than by asking whether it
    # is free. See claim_output() for why -n is not the mechanism.
    if not claimed:
        claim_output(out_path)
    # capture, don't stream: the server hands this stderr to the page, and an
    # exit code alone is useless when a filter graph is what went wrong.
    proc = subprocess.run(
        # -y overwrites the zero-byte file claimed above — the only file this
        # program's ffmpeg is ever pointed at, and one it created itself.
        ["ffmpeg", "-v", "error", "-y", *inputs,
         "-filter_complex", graph, "-map", f"[{label}]", *trim, *ENCODE, str(out_path)],
        check=True, capture_output=True, text=True)
    # Size, not existence: the destination was created empty before ffmpeg ran,
    # so "the file is there" no longer proves anything was written into it.
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise RuntimeError(
            f"ffmpeg wrote nothing to {out_path}: {(proc.stderr or '').strip()[-2000:]}")
    return out_path


def bitrate(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=bit_rate",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return int(out) if out.isdigit() else 0


def frame_count(path):
    """Frames actually written, counted rather than inferred from duration."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return int(out) if out.isdigit() else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project", nargs="?")
    ap.add_argument("-o", "--out", default=None,
                    help="output mp4, inside ~/cutroom-projects/ (default: the "
                         "project's own renders/ directory)")
    ap.add_argument("--from", dest="t_from", type=float)
    ap.add_argument("--to", dest="t_to", type=float)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    if a.check:
        subprocess.run([sys.executable,
                        str(pathlib.Path(__file__).with_name("test_render.py"))],
                       check=True)
        return
    if not a.project:
        ap.error("need a project file, or --check")

    # THE CLI IS A WRITE PATH TOO. -o used to be handed straight to ffmpeg, so
    # `render.py p.json -o ~/footage/master.mp4` walked around every boundary
    # the server enforces — a second way to name an output, which is precisely
    # what boundary 1 says does not exist. The guard lives in server.py; it is
    # imported HERE rather than at module scope because server imports this
    # module, and a top-level import back would be a cycle.
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import server as boundary

    try:
        if a.out is None:
            name = pathlib.Path(a.project).stem
            out_dir = boundary.mkdirs(boundary.project_dir(name) / "renders")
            out = boundary.claim_free(out_dir, f"{name}_cli", ".mp4")
        else:
            asked = pathlib.Path(a.out).expanduser().absolute()
            # mkdirs() is guarded too, so an outward -o is refused here rather
            # than creating a directory on the way to being refused.
            boundary.mkdirs(asked.parent)
            out = boundary.claim(asked)
    except (boundary.Refused, FileExistsError) as e:
        sys.exit(str(e))

    try:
        out = render(load(pathlib.Path(a.project)), out, a.t_from, a.t_to, claimed=True)
    except ValueError as e:
        sys.exit(str(e))
    print(f"{out}  {bitrate(out) / 1e6:.2f} Mbps")


if __name__ == "__main__":
    main()
