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
"""Checks for render.py — the validator and the render graph.

    python3 test_render.py

Bare asserts, no pytest, no numpy, no PIL: a frame is read as raw rgb24 bytes
out of ffmpeg and measured with the standard library. Every fixture is
synthesised into a temporary directory. NO TEST EVER ADDRESSES REAL FOOTAGE.
"""
import array
import math
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import render


def proj(*clips, **kw):
    """A project whose media list covers whatever the clips name."""
    media = kw.pop("media", None)
    if media is None:
        media = [{"mid": c["mid"], "path": kw.get("dir_", ".") + "/" + c["mid"] + ".mp4",
                  "label": c["mid"], "dur": 99.0, "w": 720, "h": 1280}
                 for c in {c["mid"]: c for c in clips}.values()]
    kw.pop("dir_", None)
    base = {"name": "t", "fps": 24, "resolution": [720, 1280], "version": 1,
            "media": media, "clips": list(clips), "passes": {}}
    base.update(kw)
    return base


def clip(uid, t, dur, rate=1.0, lane=0, mid="a"):
    return {"uid": uid, "label": uid, "lane": lane, "t": t, "mid": mid,
            "in": 0.0, "out": dur, "rate": rate, "note": ""}


def files(d, *names):
    """A media list pointing at real files in `d`."""
    return [{"mid": pathlib.Path(n).stem, "path": str(pathlib.Path(d) / n),
             "label": n, "dur": 99.0, "w": 720, "h": 1280} for n in names]


# --------------------------------------------------------------- the validator

def test_duration_divides_by_rate():
    # rate below 1.0 is SLOWER and therefore LONGER.
    assert render.duration(clip("a", 0, 7.0, rate=0.7)) == 10.0
    assert render.duration(clip("a", 0, 4.0)) == 4.0


def test_overlap_longer_than_a_clip_is_rejected():
    # b is 1.0s long but starts 2.0s before a ends -> a 2.0s xfade into a 1.0s clip
    bad = proj(clip("a", 0.0, 3.0), clip("b", 1.0, 1.0))
    problems = render.validate(bad, check_files=False)
    assert any("overlap" in p for p in problems), problems


def test_a_clean_cut_and_a_legal_overlap_pass():
    ok = proj(clip("a", 0.0, 2.0), clip("b", 2.0, 2.0), clip("c", 3.5, 2.0))
    assert render.validate(ok, check_files=False) == []


def test_duplicate_uid_is_rejected():
    bad = proj(clip("a", 0.0, 2.0), clip("a", 2.0, 2.0))
    problems = render.validate(bad, check_files=False)
    assert any("duplicate uid" in p for p in problems), problems


def test_out_less_than_or_equal_to_in_is_rejected():
    bad = proj(clip("a", 0.0, 0.0))
    problems = render.validate(bad, check_files=False)
    assert any("out" in p.lower() and "not after" in p.lower() for p in problems), problems


def test_rate_zero_or_negative_returns_problems_not_crash():
    # rate <= 0 must be caught before duration() is called
    for rate in (0.0, -1.0):
        bad = proj(clip("a", 0.0, 2.0, rate=rate), clip("b", 2.0, 2.0))
        problems = render.validate(bad, check_files=False)
        assert any("rate" in p.lower() for p in problems), (rate, problems)


def test_nesting_is_rejected():
    bad = proj(clip("a", 0.0, 10.0), clip("b", 2.0, 2.0))
    problems = render.validate(bad, check_files=False)
    hit = [p for p in problems if "contains" in p.lower() or "nested" in p.lower()]
    assert hit, problems
    assert "a" in hit[0] and "b" in hit[0], hit[0]


def test_reach_past_is_rejected():
    # a reaches past b into c: a=[0,10), b=[1,2), c=[3,4)
    bad = proj(clip("a", 0.0, 10.0), clip("b", 1.0, 1.0), clip("c", 3.0, 1.0))
    problems = render.validate(bad, check_files=False)
    hit = [p for p in problems if "reaches past" in p.lower()]
    assert hit, problems
    assert "a" in hit[0] and "b" in hit[0] and "c" in hit[0], hit[0]


def test_overlap_not_nesting_should_pass():
    # a=[0,5), b=[3,7). a ends before b: ordinary overlap, not nesting.
    ok = proj(clip("a", 0.0, 5.0), clip("b", 3.0, 4.0))
    assert render.validate(ok, check_files=False) == []


def test_a_negative_t_or_in_is_rejected():
    """ffmpeg clamps a negative trim start and a negative t would place a clip
    before the film begins, so in both cases the fold's arithmetic and the
    rendered file quietly disagree."""
    problems = render.validate(proj(clip("a", -1.0, 2.0)), check_files=False)
    assert any("t must not be negative" in p for p in problems), problems

    c = clip("b", 0.0, 2.0)
    c["in"] = -0.5
    problems = render.validate(proj(c), check_files=False)
    assert any("in must not be negative" in p for p in problems), problems

    assert render.validate(proj(clip("a", 0.0, 2.0)), check_files=False) == []


def test_an_edit_point_must_be_a_frame():
    """The end of three rounds of modelling ffmpeg.

    Every counterexample an independent review produced — out=0.4208333333,
    in=0.01 — was an edit point off the frame grid, and each fix that modelled
    the arithmetic of trim -> setpts -> fps moved the error rather than removing
    it. A real NLE does not permit an off-grid edit point. Neither does this
    one, which is what lets src_frames() be arithmetic instead of a model."""
    c = clip("a", 0.0, 1.0)
    c["in"], c["out"] = 0.0, 0.4208333333
    problems = render.validate(proj(c), check_files=False)
    assert any("out" in p and "frame grid" in p for p in problems), problems
    assert any("nearest frame is 10" in p for p in problems), problems

    c = clip("a", 0.0, 1.0)
    c["in"] = 0.01
    problems = render.validate(proj(c), check_files=False)
    assert any("in" in p and "frame grid" in p for p in problems), problems

    problems = render.validate(proj(clip("a", 0.3, 1.0)), check_files=False)  # 7.2 frames
    assert any("t" in p and "frame grid" in p for p in problems), problems

    # on the grid, and a whisker off it, both pass — GRID_SLOP is float noise,
    # not an edit
    ok = proj(clip("a", 0.0, 0.5), clip("b", 0.5 + 1e-9, 1.0))
    assert render.validate(ok, check_files=False) == []


def test_snapping_is_what_keeps_a_drag_on_the_grid():
    """The server snaps every save before validating, so the rule above only
    ever fires on a hand-edited file — a pixel is not a frame and a drag has to
    land somewhere."""
    c = clip("a", 0.3007, 1.0)
    c["in"], c["out"] = 0.01, 0.4208333333
    snapped = render.snap_project(proj(c))["clips"][0]
    assert render.frames_at(snapped["t"], 24) == 7 and snapped["in"] == 0.0, snapped
    assert render.frames_at(snapped["out"], 24) == 10, snapped
    assert render.validate(render.snap_project(proj(c)), check_files=False) == []
    # and snapping is idempotent — a saved file does not drift on re-save
    twice = render.snap_project(render.snap_project(proj(c)))
    assert twice["clips"][0] == snapped, twice["clips"][0]


def test_duration_is_arithmetic_on_the_grid():
    """No ceilings, no rounding mode, no boundary model: (out - in) * fps."""
    assert render.src_frames({"in": 0.0, "out": 0.5}, 24) == 12
    assert render.src_frames({"in": 0.25, "out": 0.75}, 24) == 12
    assert render.src_frames({"in": 6 / 24, "out": 18 / 24}, 24) == 12

    assert abs(render.duration({"in": 0.0, "out": 1.4, "rate": 1.0})
               - 1.41667) < 1e-4          # 33.6 -> 34 frames
    assert abs(render.duration({"in": 0.0, "out": 1.4, "rate": 0.7})
               - 2.04167) < 1e-4
    assert abs(render.duration({"in": 0.0, "out": 1.5, "rate": 0.7})
               - 2.125) < 1e-4
    assert render.duration({"in": 0.0, "out": 7.0, "rate": 0.7}) == 10.0


# ------------------------------------------------------ media, mid and OFFLINE

def test_a_clip_naming_media_the_project_does_not_have_is_refused():
    """A mid that names nothing is a broken reference, not a file that moved,
    so it is a problem on the SAVE path as well as the export path."""
    bad = proj(clip("a", 0.0, 1.0, mid="ghost"), media=[])
    problems = render.validate(bad, check_files=False)
    assert any("ghost" in p and "media" in p for p in problems), problems


def test_a_missing_source_blocks_export_but_never_the_edit():
    """THE rule. A file that has moved must not stop the director cutting, must
    not rewrite the project and must not drop the clip — it blocks export, by
    name, and nothing else."""
    with tempfile.TemporaryDirectory() as d:
        media = files(d, "gone.mp4")
        p = proj(clip("a", 0.0, 1.0, mid="gone"), media=media)

        # the save path does not look at the disk at all
        assert render.validate(p, check_files=False) == []
        # the export path names it, and says the clip is OFFLINE
        problems = render.validate(p, check_files=True)
        assert any("source missing" in x and "gone.mp4" in x for x in problems), problems
        assert any("OFFLINE" in x for x in problems), problems
        # and offline() reports it without touching the document
        before = dict(p)
        assert render.offline(p) == [("a", f"missing {media[0]['path']}")], render.offline(p)
        assert p == before and len(p["clips"]) == 1, "the project was modified"

        # render refuses rather than rendering a hole
        try:
            render.render(p, pathlib.Path(d) / "out.mp4")
            assert False, "render accepted a missing source"
        except ValueError as e:
            assert "source missing" in str(e), e


def test_offline_reports_an_unknown_mid_too():
    p = proj(clip("a", 0.0, 1.0, mid="ghost"), media=[])
    assert render.offline(p) == [("a", "unknown media 'ghost'")], render.offline(p)


# ----------------------------------------------------------------- the fixtures

def _lavfi(path, colour, seconds=2.0, size="720x1280", vf=None):
    """A fixture clip. `vf` paints a feature on it — a solid colour cannot show
    a stretch, and every geometry assertion below needs something off-centre."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=c={colour}:s={size}:d={seconds}:r=24",
         *(["-vf", vf] if vf else []),
         "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", str(path)],
        check=True)
    return path


def _lavfi_src(path, src, seconds=2.0):
    """A fixture from an arbitrary lavfi source string (testsrc2, noise, ...)."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src,
         "-t", str(seconds),
         "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", str(path)],
        check=True)
    return path


def _lavfi_tone(path, colour, hz, seconds=2.0, size="720x1280"):
    """A visual source with a distinct, measurable tone."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"color=c={colour}:s={size}:d={seconds}:r=24", "-f", "lavfi",
         "-i", f"sine=f={hz}:d={seconds}", "-shortest",
         "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", "-c:a", "aac",
         str(path)], check=True)


def _tone(path, hz=440, seconds=2.0):
    """Audio-only media — no picture at all."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=f={hz}:d={seconds}", "-c:a", "aac", str(path)], check=True)


def _audio_rms(path, at, seconds=0.1):
    """RMS of a short mono window, independent of the render graph."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at), "-t", str(seconds), "-i", str(path),
         "-map", "0:a:0", "-ac", "1", "-f", "f32le", "pipe:1"],
        capture_output=True, check=True).stdout
    samples = array.array("f")
    samples.frombytes(raw[:len(raw) - len(raw) % samples.itemsize])
    if not samples:
        return 0.0
    return math.sqrt(sum(v * v for v in samples) / len(samples))


def _probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def _raw_frame(path, at):
    """(w, h, rgb24 bytes) of one extracted frame. No numpy, no PIL."""
    wh = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True, check=True).stdout.strip().split("x")
    w, h = int(wh[0]), int(wh[1])
    buf = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True).stdout
    assert len(buf) >= w * h * 3, f"short frame at {at}: {len(buf)} of {w * h * 3}"
    return w, h, buf[:w * h * 3]


def _frame_rgb(path, at):
    """Mean R, G, B of one extracted frame."""
    w, h, buf = _raw_frame(path, at)
    n = w * h
    return [sum(buf[i::3]) / n for i in range(3)]


def _row_maxima(path, at):
    """The brightest sample in each row — enough to find letterbox bars."""
    w, h, buf = _raw_frame(path, at)
    stride = w * 3
    return [max(buf[y * stride:(y + 1) * stride]) for y in range(h)]


def _frame_count(path):
    return render.frame_count(path)


def _black_spans(path):
    """[(start, end)] of every fully-black run in the file, via blackdetect.

    Whole-file, so it catches a stray filler frame anywhere — sampling a few
    timestamps by hand would walk straight past a single black frame.
    """
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path),
         "-vf", "blackdetect=d=0.001:pix_th=0.10", "-f", "null", "-"],
        capture_output=True, text=True, check=True)
    spans = []
    for line in r.stderr.splitlines():
        if "black_start" in line:
            parts = {}
            for tok in line.split():
                if tok.startswith("black_"):
                    k, _, v = tok.partition(":")
                    parts[k] = float(v)
            spans.append((parts["black_start"], parts["black_end"]))
    return spans


# -------------------------------------------------------------- the real graph

def test_render_duration_subtracts_the_overlap():
    """The classic bug is adding the overlap instead of subtracting it."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        for name, colour in (("r.mp4", "red"), ("g.mp4", "green"), ("b.mp4", "blue")):
            _lavfi(d / name, colour)
        p = proj(
            clip("a", 0.0, 1.0, mid="r"),
            clip("b", 1.0, 1.0, mid="g"),    # hard cut
            clip("c", 1.5, 1.0, mid="b"),    # 0.5s overlap with b
            # d abuts AFTER the overlap. Without it the film ENDS on the xfade,
            # so acc is never read again and the sign of the overlap term in
            # `acc += gap + d` is unobservable — flip it and the file is still
            # 2.5s. This clip is what makes the assertion discriminate.
            clip("d", 2.5, 1.0, mid="r"),
            media=files(d, "r.mp4", "g.mp4", "b.mp4"))
        out = render.render(p, d / "out.mp4")
        # 1.0 + 1.0 + 1.0 - 0.5 + 1.0 = 3.5, tolerance one frame
        assert abs(_probe_duration(out) - 3.5) < 1 / 24 + 0.02, _probe_duration(out)
        # and the timeline said so before ffmpeg was ever started
        assert abs(render.timeline_length(p) - 3.5) < 1e-6, render.timeline_length(p)


def test_a_frame_mid_overlap_is_a_mix_of_both_sources():
    """The check that actually fails if the xfade chain is wired wrong.

    A wrong offset still produces a 2.5s file — it just shows pure green or
    pure blue at the moment the two should be halfway through each other.
    """
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        for name, colour in (("r.mp4", "red"), ("g.mp4", "green"), ("b.mp4", "blue")):
            _lavfi(d / name, colour)
        p = proj(clip("a", 0.0, 1.0, mid="r"),
                 clip("b", 1.0, 1.0, mid="g"),
                 clip("c", 1.5, 1.0, mid="b"),
                 media=files(d, "r.mp4", "g.mp4", "b.mp4"))
        out = render.render(p, d / "out.mp4")
        r, g, b = _frame_rgb(out, 1.75)          # dead centre of the 1.5-2.0s xfade
        assert r < 40, f"red should be long gone, got {r:.0f}"
        assert g > 60, f"green should still be present, got {g:.0f}"
        assert b > 60, f"blue should have arrived, got {b:.0f}"


def test_an_off_shape_source_is_letterboxed_not_stretched():
    """Fit-inside-and-pad, never fill. Kills `force_original_aspect_ratio` and
    `pad=` both: without the first the bars vanish, without the second the
    output is not 720x1280 (and 717-wide is not even encodable as yuv420p)."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        # 1456x816 — landscape, so fitting inside 720x1280 leaves ~438px bars.
        # 816x1456 (the obvious "off-size" choice) is within 0.4% of the target
        # aspect and letterboxes to a 1px sliver: not a test, a coin flip.
        _lavfi(d / "wide.mp4", "red", size="1456x816",
               vf="drawbox=x=200:y=120:w=180:h=140:color=white:t=fill")
        p = proj(clip("a", 0.0, 1.0, mid="wide"), media=files(d, "wide.mp4"))
        out = render.render(p, d / "out.mp4")

        w, h, _ = _raw_frame(out, 0.5)
        assert (w, h) == (720, 1280), (w, h)

        rows = _row_maxima(out, 0.5)
        content = [i for i, v in enumerate(rows) if v > 20]
        top, bottom = content[0], len(rows) - 1 - content[-1]
        # Stretched to fill, there is no bar at all and top == 0.
        assert top > 300 and bottom > 300, f"expected letterbox bars, got {top}/{bottom}"
        # Centred, and the aspect ratio survived: 1456x816 fitted into 720 wide
        # is 720x404, so ~438px of bar per side.
        assert abs(top - bottom) < 20, f"bars are not centred: {top}/{bottom}"


def test_the_encode_clears_the_five_mbps_floor():
    """The only thing guarding ENCODE. Fall back to ffmpeg's default (crf 23)
    and this source lands near 2.8 Mbps."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        # Solid colour is useless here: it compresses to ~17 kbps at ANY crf.
        _lavfi_src(d / "noisy.mp4", "testsrc2=s=720x1280:r=24")
        p = proj(clip("a", 0.0, 2.0, mid="noisy"), media=files(d, "noisy.mp4"))
        out = render.render(p, d / "out.mp4")
        mbps = render.bitrate(out) / 1e6
        assert mbps > 5, f"{mbps:.2f} Mbps is under the 5 Mbps floor"


def test_a_real_gap_becomes_black_and_lengthens_the_film():
    """A gap the director actually meant: half a second of nothing."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi(d / "r.mp4", "red")
        _lavfi(d / "g.mp4", "green")
        p = proj(clip("a", 0.0, 1.0, mid="r"),
                 clip("b", 1.5, 1.0, mid="g"),   # 0.5s hole between them
                 media=files(d, "r.mp4", "g.mp4"))
        out = render.render(p, d / "out.mp4")
        assert abs(_probe_duration(out) - 2.5) < 1 / 24 + 0.02, _probe_duration(out)
        assert max(_frame_rgb(out, 1.25)) < 10, _frame_rgb(out, 1.25)
        spans = _black_spans(out)
        assert any(s < 1.3 < e_ for s, e_ in spans), spans


def test_a_mid_timeline_gap_does_not_shift_what_follows_it():
    """A gap in the MIDDLE, with clips after it.

    The gap branch's `acc += gap + d` is only observable through the POSITION
    of the next clip: put the gap on the last clip and a corrupted acc is never
    read again, so total duration stays right and nothing notices.

        r [0.0-1.0)  g [1.0-2.0)  black [2.0-2.5)  b [2.5-3.5)  w [3.5-4.5)
    """
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        for name, colour in (("r.mp4", "red"), ("g.mp4", "green"),
                             ("b.mp4", "blue"), ("w.mp4", "white")):
            _lavfi(d / name, colour)
        p = proj(clip("a", 0.0, 1.0, mid="r"),
                 clip("b", 1.0, 1.0, mid="g"),
                 clip("c", 2.5, 1.0, mid="b"),   # 0.5s hole before c
                 clip("d", 3.5, 1.0, mid="w"),   # and a clip AFTER the hole
                 media=files(d, "r.mp4", "g.mp4", "b.mp4", "w.mp4"))
        out = render.render(p, d / "out.mp4")

        def at(t):
            return _frame_rgb(out, t)

        # Position first — it is the assertion that discriminates. Probe points
        # sit well inside each shot, never on a boundary a one-frame rounding
        # could straddle.
        r, g, b = at(0.5)
        assert r > 150 and g < 40 and b < 40, f"t=0.5 should be red, got {at(0.5)}"
        r, g, b = at(1.5)
        assert r < 40 and g > 60 and b < 40, f"t=1.5 should be green, got {at(1.5)}"
        assert max(at(2.25)) < 10, f"t=2.25 should be the gap's black, got {at(2.25)}"
        r, g, b = at(3.0)
        assert b > 150 and r < 40 and g < 40, f"t=3.0 should be blue, got {at(3.0)}"
        # The load-bearing pair: d is positioned off acc, so a gap-branch acc
        # error moves it. 3.7 is inside d but inside the phantom filler a
        # corrupted acc would insert, so it fails on a shift in either direction.
        assert min(at(3.7)) > 150, f"t=3.7 should be white — d shifted? got {at(3.7)}"
        assert min(at(4.3)) > 150, f"t=4.3 should be white — d shifted? got {at(4.3)}"

        assert abs(_probe_duration(out) - 4.5) < 1 / 24 + 0.02, _probe_duration(out)


def test_a_lead_gap_becomes_black_before_the_first_clip():
    """First clip at t > 0 means the film opens on black, not on the clip."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi(d / "r.mp4", "red")
        p = proj(clip("a", 0.5, 1.0, mid="r"), media=files(d, "r.mp4"))
        out = render.render(p, d / "out.mp4")
        assert abs(_probe_duration(out) - 1.5) < 1 / 24 + 0.02, _probe_duration(out)
        assert max(_frame_rgb(out, 0.25)) < 10, _frame_rgb(out, 0.25)
        assert _frame_rgb(out, 1.0)[0] > 100, _frame_rgb(out, 1.0)


def test_a_subframe_gap_is_snapped_away_before_it_can_flash_black():
    """A browser drag lands t on a float, and two failure modes follow from
    treating any gap > 1e-6 as real:
      - 1.0001 - 1.0 = 9.999999999998899e-05, which ffmpeg refuses to parse as
        a duration and the whole render dies;
      - 0.005 parses fine and quietly punches one black frame into a hard cut.

    Neither is modelled now. 0.0001s is 0.0024 of a frame — float noise, inside
    GRID_SLOP, and build_graph quantises it away. 0.005s and 0.019s are real
    fractions of a frame, so they are refused outright, and the server snaps
    every save, which is how they stop reaching the renderer at all.
    """
    for slop, legal in ((0.0001, True), (0.005, False), (0.019, False)):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            _lavfi(d / "r.mp4", "red")
            _lavfi(d / "g.mp4", "green")
            p = proj(clip("a", 0.0, 1.0, mid="r"),
                     clip("b", 1.0 + slop, 1.0, mid="g"),
                     media=files(d, "r.mp4", "g.mp4"))
            problems = render.validate(p, check_files=False)
            if legal:
                assert problems == [], (slop, problems)
            else:
                assert any("frame grid" in x for x in problems), (slop, problems)
                p = render.snap_project(p)
                assert render.validate(p, check_files=False) == [], slop

            out = render.render(p, d / f"out{slop}.mp4")   # must not raise
            assert abs(_probe_duration(out) - 2.0) < 1 / 24 + 0.02, _probe_duration(out)
            assert _black_spans(out) == [], f"slop {slop} flashed black: {_black_spans(out)}"


def test_ffmpeg_emits_exactly_what_the_graph_assumes_at_rate_one():
    """The check whose absence hid the original bug. At rate 1.0 the prediction
    is exact — by construction, and measured here on three on-grid trims
    including one that does not start at zero."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi_src(d / "s.mp4", "testsrc2=s=320x240:r=24", seconds=2.0)
        for f_in, f_out in ((0, 24), (0, 12), (6, 18)):
            c = clip("a", 0.0, 1.0, mid="s")
            c["in"], c["out"] = f_in / 24, f_out / 24
            p = proj(c, media=files(d, "s.mp4"), resolution=[320, 240])
            out = render.render(p, d / f"o{f_in}_{f_out}.mp4")
            frames = _frame_count(out)
            assert frames == f_out - f_in, (f_in, f_out, frames)
            assert frames == round(render.duration(c, 24) * 24), frames


def test_a_retimed_clip_lands_within_one_frame_of_its_prediction():
    """The honest claim, and the only one made. The fps filter's resample of
    shifted timestamps is a real requantisation and is NOT modelled; a retimed
    clip may land one frame from its predicted length."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi_src(d / "s.mp4", "testsrc2=s=320x240:r=24", seconds=2.0)
        for f_out, rate in ((10, 0.8), (5, 0.4), (9, 0.72), (7, 2.0), (11, 0.8)):
            c = clip("a", 0.0, 1.0, rate=rate, mid="s")
            c["in"], c["out"] = 0.0, f_out / 24
            p = proj(c, media=files(d, "s.mp4"), resolution=[320, 240])
            out = render.render(p, d / f"r{f_out}_{rate}.mp4")
            predicted = round(render.duration(c, 24) * 24)
            assert abs(_frame_count(out) - predicted) <= 1, \
                f"{f_out} frames at rate {rate}: predicted {predicted}, " \
                f"ffmpeg wrote {_frame_count(out)}"


def test_an_on_grid_trim_does_not_shift_what_follows_it():
    """The consequence the frame model exists for, measured where it hurts: the
    filler before the next clip is computed from the accumulator, so a duration
    one frame wrong pays that frame into the black and moves the next shot.

        a  [0.0 .. 0.5)   black to t=1.0   b  [1.0 .. 2.0)
    """
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi(d / "r.mp4", "red")
        _lavfi(d / "g.mp4", "green")
        p = proj(clip("a", 0.0, 0.5, mid="r"), clip("b", 1.0, 1.0, mid="g"),
                 media=files(d, "r.mp4", "g.mp4"))
        out = render.render(p, d / "out.mp4")
        assert abs(_probe_duration(out) - 2.0) < 1 / 24 + 0.02, _probe_duration(out)
        assert max(_frame_rgb(out, 0.75)) < 10, _frame_rgb(out, 0.75)
        assert _frame_rgb(out, 25 / 24)[1] > 60, _frame_rgb(out, 25 / 24)


def test_the_timeline_length_is_what_the_export_measures():
    """What the header promises and what the file holds, in frames — the check
    the director actually runs after an export."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi(d / "r.mp4", "red")
        _lavfi(d / "g.mp4", "green")
        a = clip("a", 0.0, 1.0, mid="r")
        b = clip("b", 1.5, 1.0, mid="g")
        b["in"], b["out"] = 6 / 24, 30 / 24
        p = proj(a, b, media=files(d, "r.mp4", "g.mp4"), resolution=[320, 240])
        out = render.render(p, d / "out.mp4")
        assert render.timeline_length(p) == 2.5, render.timeline_length(p)
        assert _frame_count(out) == 60, _frame_count(out)


# ------------------------------------------- what the source itself must be

def test_a_trim_past_the_end_of_the_source_is_rejected():
    """ffmpeg clamps it silently and every xfade offset after the clip moves."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi(d / "r.mp4", "red", seconds=1.0)
        bad = proj(clip("a", 0.0, 3.0, mid="r"), media=files(d, "r.mp4"))
        problems = render.validate(bad, check_files=True)
        assert any("claims 72 frames" in p and "holds 24" in p for p in problems), problems

        ok = proj(clip("a", 0.0, 21 / 24, mid="r"), media=files(d, "r.mp4"))
        assert render.validate(ok, check_files=True) == []

        try:
            render.render(bad, d / "out.mp4")
            assert False, "render accepted a trim past the end of the source"
        except ValueError as e:
            assert "but it holds" in str(e), e


def test_a_trim_may_not_claim_a_frame_the_source_does_not_have():
    """A whole frame of slack was a whole frame too much: out=1.040 against a
    1.000s 24fps source passed, and duration() then modelled 25 frames where
    ffmpeg emits 24."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi(d / "r.mp4", "red", seconds=1.0)
        bad = proj(clip("a", 0.0, 25 / 24, mid="r"), media=files(d, "r.mp4"))
        problems = render.validate(bad, check_files=True)
        assert any("claims 25 frames" in p for p in problems), problems
        ok = proj(clip("a", 0.0, 1.0, mid="r"), media=files(d, "r.mp4"))
        assert render.validate(ok, check_files=True) == []


def test_a_source_on_another_fps_grid_is_refused():
    """duration() assumes the source sits on the project's own frame grid — the
    same trim and rate yields a different frame count off a 25fps file."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi_src(d / "p25.mp4", "testsrc2=s=320x240:r=25", seconds=2.0)
        p = proj(clip("a", 0.0, 1.0, mid="p25"), media=files(d, "p25.mp4"))
        problems = render.validate(p, check_files=True)
        assert any("25" in x and "24fps" in x for x in problems), problems
        assert any("one grid" in x for x in problems), problems


def test_an_unprobeable_source_is_a_refusal_not_permission():
    """A failed probe used to return None, which turned the past-the-end check
    off entirely. Every number this renderer computes is a frame count off the
    file; if ffprobe cannot read it, the graph is guessing."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        (d / "junk.mp4").write_text("this is not a video")
        p = proj(clip("a", 0.0, 1.0, mid="junk"), media=files(d, "junk.mp4"))
        problems = render.validate(p, check_files=True)
        assert any("ffprobe cannot read" in x for x in problems), problems


def test_a_variable_frame_rate_source_is_refused():
    """Nominal rate is a CLAIM. A real VFR file reports r_frame_rate=24/1 with
    avg_frame_rate nowhere near it — a 24fps project would accept it on the
    nominal number and cut nothing like what it predicted."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        # dense for the first second, then one frame every twelfth, timestamps
        # passed through: nominal 24, average nowhere near it.
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=s=160x120:d=3:r=24",
             "-vf", "select='lt(n,24)+not(mod(n,12))',setpts=PTS",
             "-fps_mode", "passthrough", "-c:v", "libx264", "-crf", "20",
             "-pix_fmt", "yuv420p", str(d / "vfr.mp4")], check=True)
        info = render.source_info(d / "vfr.mp4")
        assert abs(info["fps"] - 24) < 0.01, info      # the claim
        assert abs(info["avg_fps"] - 24) > 1, info     # the reality

        p = proj(clip("a", 0.0, 0.5, mid="vfr"), media=files(d, "vfr.mp4"))
        problems = render.validate(p, check_files=True)
        assert any("averages" in x and "variable frame rate" in x
                   for x in problems), problems


# ------------------------------------------------------- reading, and only that

def test_rendering_does_not_touch_the_sources():
    """The whole promise, measured: bytes, size and mtime of every source are
    identical after a render, and the render only ever created its own output."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi(d / "r.mp4", "red")
        _lavfi(d / "g.mp4", "green")
        before = {n: ((d / n).read_bytes(), (d / n).stat().st_mtime_ns)
                  for n in ("r.mp4", "g.mp4")}
        listing = sorted(p.name for p in d.iterdir())
        p = proj(clip("a", 0.0, 1.0, mid="r"), clip("b", 1.0, 1.0, mid="g"),
                 media=files(d, "r.mp4", "g.mp4"))
        render.render(p, d / "out.mp4")
        for n, (blob, mtime) in before.items():
            assert (d / n).read_bytes() == blob, f"{n} was modified"
            assert (d / n).stat().st_mtime_ns == mtime, f"{n}'s mtime moved"
        assert sorted(q.name for q in d.iterdir()) == sorted(listing + ["out.mp4"])


def test_the_renderer_refuses_to_overwrite_an_existing_output():
    """cutroom may create a file; it may never replace one.

    The refusal is in render(), NOT in ffmpeg's -n, and that is deliberate:
    ffmpeg 8.1.1 answers -n with "File 'x' already exists. Exiting." and an
    exit code of ZERO (measured). It protects the file and reports success, so
    -n alone would be a guarantee whose failure is invisible.
    """
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi(d / "r.mp4", "red")
        p = proj(clip("a", 0.0, 1.0, mid="r"), media=files(d, "r.mp4"))
        out = d / "out.mp4"
        out.write_bytes(b"SOMETHING THAT IS ALREADY HERE")
        try:
            render.render(p, out)
            assert False, "the render overwrote an existing file"
        except FileExistsError as e:
            assert "never overwrites" in str(e), e
        assert out.read_bytes() == b"SOMETHING THAT IS ALREADY HERE"


def test_validate_counts_frames_the_way_probe_does():
    """A container can say less time than it holds pictures, and then the two
    halves of this program disagree about one frame.

    exit-reel decoded 248 frames but its container said 10.333008s, which floors
    to 247 at 24fps. probe() had already recorded 248, so a clip using the whole
    file was legal on the save path and REFUSED at export — a cut that renders
    perfectly, rejected. A validator that refuses a legal edit is as bad as one
    that passes an illegal one.

    The stub is the point: if validate() goes back to flooring the duration, the
    counted 30 is ignored and this fails.
    """
    with tempfile.TemporaryDirectory() as d:
        src = _lavfi(pathlib.Path(d) / "a.mp4", "black", seconds=1.0, size="160x120")
        project = {"fps": 24, "resolution": [160, 120],
                   "media": [{"mid": "m01", "path": str(src), "dur": 30 / 24,
                              "w": 160, "h": 120}],
                   "clips": [{"uid": "c01", "mid": "m01", "lane": 0, "t": 0.0,
                              "in": 0.0, "out": 30 / 24, "rate": 1.0}]}

        real = render.source_frames
        render.source_frames = lambda _p: 30
        try:
            problems = render.validate(project, check_files=True)
        finally:
            render.source_frames = real
        assert not [p for p in problems if "claims" in p], (
            f"validate refused a clip the decode says is legal: {problems}")


def test_validate_still_refuses_a_trim_past_a_countable_end():
    """The counted number must still be able to say no."""
    with tempfile.TemporaryDirectory() as d:
        src = _lavfi(pathlib.Path(d) / "b.mp4", "black", seconds=1.0, size="160x120")
        project = {"fps": 24, "resolution": [160, 120],
                   "media": [{"mid": "m01", "path": str(src), "dur": 2.0,
                              "w": 160, "h": 120}],
                   "clips": [{"uid": "c01", "mid": "m01", "lane": 0, "t": 0.0,
                              "in": 0.0, "out": 2.0, "rate": 1.0}]}
        problems = render.validate(project, check_files=True)
        assert any("claims" in p for p in problems), (
            f"a 2.0s trim of a 1.0s source should be refused, got {problems}")


# -------------------------------------------------------------------- audio

def test_an_audio_only_source_is_valid_but_not_a_video_clip():
    """A stem has no frames and no grid, so every picture rule must skip it."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _tone(d / "stem.m4a", 440, seconds=1.0)
        info = render.source_info(d / "stem.m4a")
        assert info["fps"] is None and info["w"] is None, info
        assert info["audio"] is True and abs(info["dur"] - 1.0) < 0.05, info

        project = proj(clip("s", 0.0, 1.0, mid="stem"), media=files(d, "stem.m4a"))
        assert not render.validate(project, check_files=True)
        assert render.video_clips(project) == []


def test_a_legacy_clip_keeps_its_sound_and_a_non_boolean_audio_is_refused():
    """Projects written before audio existed have no `audio` key at all."""
    assert render.audio_enabled(clip("a", 0.0, 1.0)) is True
    assert render.audio_enabled(dict(clip("a", 0.0, 1.0), audio=False)) is False
    assert render.audio_enabled(dict(clip("a", 0.0, 1.0), audio="false")) is None
    problems = render.validate(
        proj(dict(clip("a", 0.0, 1.0), audio="false")), check_files=False)
    assert any("audio must be true or false" in p for p in problems), problems


def test_a_video_tone_and_an_audio_only_tone_are_muxed():
    """Losing the audio graph or its output map loses a real stem."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi(d / "picture.mp4", "red", seconds=1.0)
        _tone(d / "stem.m4a", 660, seconds=1.0)
        out = render.render(proj(
            clip("p", 0.0, 1.0, mid="picture"),
            clip("s", 0.0, 1.0, mid="stem"),
            media=files(d, "picture.mp4", "stem.m4a")), d / "out.mp4")
        assert render.has_audio(out), "the muxed movie lost its audio stream"


def test_disabling_every_clip_audio_leaves_no_audio_stream():
    """The video map must not invent audio when all source audio is off."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi_tone(d / "picture.mp4", "red", 440, seconds=1.0)
        c = dict(clip("p", 0.0, 1.0, mid="picture"), audio=False)
        out = render.render(proj(c, media=files(d, "picture.mp4")), d / "out.mp4")
        assert not render.has_audio(out), "disabled audio was muxed anyway"


def test_crossfaded_tones_keep_their_midpoint_power():
    """Linear amplitude fades make two uncorrelated tones audibly dip."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi_tone(d / "a.mp4", "red", 440)
        _lavfi_tone(d / "b.mp4", "blue", 660)
        out = render.render(proj(
            clip("a", 0.0, 2.0, mid="a"),
            clip("b", 1.0, 2.0, mid="b"),
            media=files(d, "a.mp4", "b.mp4")), d / "out.mp4")
        before, midpoint = _audio_rms(out, 0.25), _audio_rms(out, 1.45)
        assert before * 0.86 < midpoint < before * 1.22, (before, midpoint)


def test_an_all_audio_timeline_has_a_black_picture_through_its_end():
    """An audio-only edit still needs a mappable video stream."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _tone(d / "a.m4a", 440, seconds=1.0)
        _tone(d / "b.m4a", 660, seconds=1.0)
        out = render.render(proj(
            clip("a", 0.0, 1.0, mid="a"),
            clip("b", 1.0, 1.0, mid="b"),
            media=files(d, "a.m4a", "b.m4a")), d / "out.mp4")
        assert abs(_probe_duration(out) - 2.0) < 1 / 24 + 0.02
        assert max(_frame_rgb(out, 0.5)) < 10
        assert max(_frame_rgb(out, 1.5)) < 10


def test_a_slowed_audio_clip_keeps_sound_through_its_retimed_duration():
    """A sub-0.5 rate needs chained atempo or the tail comes out silent."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _tone(d / "s.m4a", 440, seconds=1.0)
        out = render.render(proj(
            clip("s", 0.0, 1.0, rate=0.4, mid="s"),
            media=files(d, "s.m4a")), d / "out.mp4")
        assert abs(_probe_duration(out) - 2.5) < 1 / 24 + 0.05, _probe_duration(out)
        assert _audio_rms(out, 2.2) > 0.01, "the retimed tail went silent"


def test_a_trim_past_the_end_of_the_sound_is_refused_not_silently_exported():
    """The mix is anchored on a silence, so probing the output cannot tell a
    stem that played from one that trimmed to nothing. Only validation can."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        # three seconds of picture, one second of sound
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y",
             "-f", "lavfi", "-i", "color=c=red:s=720x1280:d=3:r=24",
             "-f", "lavfi", "-i", "sine=f=440:d=1",
             "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(d / "short.mp4")], check=True)
        info = render.source_info(d / "short.mp4")
        assert info["audio"] and info["audio_dur"] < 1.5, info

        media = files(d, "short.mp4")
        late = dict(clip("a", 0.0, 1.0, mid="short"), **{"in": 2.0, "out": 3.0})
        problems = render.validate(proj(late, media=media), check_files=True)
        assert any("starts past" in p and "export silent" in p for p in problems), problems

        # Sound that merely stops part way through the shot is a real cut,
        # and so is the same trim with the clip's audio switched off.
        assert not render.validate(
            proj(clip("a", 0.0, 3.0, mid="short"), media=media), check_files=True)
        assert not render.validate(
            proj(dict(late, audio=False), media=media), check_files=True)


def test_a_second_longer_audio_stream_does_not_vouch_for_the_rendered_one():
    """The graph maps a:0. Measuring the longest stream instead would let a
    silent trim through on any file carrying a commentary track."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y",
             "-f", "lavfi", "-i", "color=c=red:s=720x1280:d=3:r=24",
             "-f", "lavfi", "-i", "sine=f=440:d=1",     # a:0, one second
             "-f", "lavfi", "-i", "sine=f=660:d=3",     # a:1, the full three
             "-map", "0:v", "-map", "1:a", "-map", "2:a",
             "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", "-c:a", "aac",
             str(d / "two.mp4")], check=True)
        assert render.source_info(d / "two.mp4")["audio_dur"] < 1.5

        late = dict(clip("a", 0.0, 1.0, mid="two"), **{"in": 2.0, "out": 3.0})
        problems = render.validate(proj(late, media=files(d, "two.mp4")),
                                   check_files=True)
        assert any("starts past" in p for p in problems), problems


def test_an_export_that_lost_its_mapped_audio_raises_instead_of_shipping():
    """ffmpeg can drop a mapped stream without failing. The graph knowing there
    was sound is the only thing that can catch it, so the check is wired here
    by making the probe report the loss."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _lavfi_tone(d / "picture.mp4", "red", 440, seconds=1.0)
        project = proj(clip("p", 0.0, 1.0, mid="picture"),
                       media=files(d, "picture.mp4"))
        real = render.has_audio
        render.has_audio = lambda path: False
        try:
            render.render(project, d / "out.mp4")
        except RuntimeError as e:
            assert "no audio stream" in str(e), e
        else:
            raise AssertionError("a lost audio stream was shipped as a good export")
        finally:
            render.has_audio = real


def test_one_mixed_timeline_carries_every_audio_case_at_once():
    """The acceptance gate. Audio-only lead, a video tone, a silent video over
    it, and an audio-only tail have each passed alone; this is the one export
    where they have to hold together, and it is the shape a real cut has."""
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        _tone(d / "lead.m4a", 440, seconds=1.0)
        _lavfi_tone(d / "tone.mp4", "red", 660, seconds=2.0)
        _lavfi(d / "silent.mp4", "blue", seconds=2.0)
        _tone(d / "tail.m4a", 880, seconds=1.0)
        out = render.render(proj(
            clip("lead", 0.0, 1.0, mid="lead"),
            clip("tone", 1.0, 2.0, mid="tone"),
            clip("silent", 2.0, 2.0, lane=1, mid="silent"),
            clip("tail", 4.0, 1.0, mid="tail"),
            media=files(d, "lead.m4a", "tone.mp4", "silent.mp4", "tail.m4a"),
        ), d / "out.mp4")

        assert render.has_audio(out), "the mixed export lost its audio stream"
        assert abs(_probe_duration(out) - 5.0) < 1 / 24 + 0.02, _probe_duration(out)

        assert max(_frame_rgb(out, 0.5)) < 10, "the audio-only lead was not black"
        assert _frame_rgb(out, 1.5)[0] > 150, "the video tone did not paint red"
        assert _frame_rgb(out, 3.5)[2] > 150, "the silent video did not paint blue"
        assert max(_frame_rgb(out, 4.5)) < 10, "the audio-only tail was not black"

        # Every stem that should sound does — including the one past the end
        # of the picture, which is what caught stems being mixed at t=0.
        for at, what in ((0.5, "lead"), (1.5, "video tone"), (4.5, "tail")):
            assert _audio_rms(out, at) > 0.01, f"{what} is silent at {at}s"


if __name__ == "__main__":
    # An optional substring argument runs one test. Used to demonstrate a fix
    # FAILING FIRST against a patched copy of the module it fixes.
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    ran = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and only in name:
            fn()
            ran += 1
            print("ok", name)
    assert ran, f"no test matched {only!r}"
    print("all ok")
