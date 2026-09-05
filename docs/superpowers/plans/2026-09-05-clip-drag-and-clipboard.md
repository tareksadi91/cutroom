# Clip Drag Snapping, Reordering, Keyboard Nudge, and Clipboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a clip already on the timeline a precise, crossfade-safe snap when dragged, a same-lane reorder/swap that never disturbs anything outside the edit, frame-accurate keyboard nudging, and copy/paste — all in `src/ui.html`.

**Architecture:** Six new pure functions (magnet radius math, lane-scoped candidate building, flush-run detection, swap/reorder target classification, a shared reindex-and-relayout commit primitive, and a paste-anchor resolver) get built and unit-tested in isolation first, following this codebase's existing convention of extracting marked regions of `ui.html` and running them under `node`. Only after those are solid does the plan wire them into the real pointer-drag handler, the keydown handler, and Cmd+C/Cmd+V — the parts that touch the live DOM and can only be verified by hand in a browser, matching how every other interactive feature in this file has been verified.

**Tech Stack:** Vanilla JS (`src/ui.html`), Python test harness that extracts marked JS regions and runs them under `node` (`src/test_server.py`), no build step, no framework.

**Spec:** `docs/superpowers/specs/2026-09-05-clip-drag-and-clipboard-design.md` (commit `bdb8ac4`) — read it alongside this plan. This plan implements its behavior; where the spec describes an outcome and this plan picks a specific algorithm to produce it (the reindex-and-relayout commit, in particular), the plan's algorithm is what to build — it was checked against the spec's own worked examples (see Task 4) and produces identical results, including the exact swap formula the spec derives by hand.

## Global Constraints

- Touches `src/ui.html` and `src/test_server.py` only. No server, renderer, or project-schema changes (spec, line 4).
- Every new marked-region function must not alter `nearestPoint()`'s name, signature, or behavior — the existing test `test_a_bin_drop_only_snaps_to_a_seam_you_are_pointing_at` (`test_server.py:1714`) depends on both, unchanged (spec §1).
- Trim-left/trim-right dragging is out of scope — no magnet, no change (spec, Non-goals).
- Swap and reorder never apply to a multi-clip selection (`selected().length !== 1`) — falls through to today's plain free-move-together (spec, Non-goals). This plan goes one step further than the spec states explicitly: a multi-selection drag gets no magnet/seam-land benefit either, only single-clip drags do (see Task 5's explicit note on this).
- No modifier key gates swap; it fires on a plain drop (spec, Non-goals).
- Every commit that moves more than one clip's `t` (swap, reorder, group nudge) is legality-gated via `timelineFault()` with snapshot/revert-on-fault, the same shape `insertAt()` already uses (`ui.html:1377-1389`).
- `1e-6` is this file's standing epsilon for time-equality comparisons (`timelineFault()`, `seamFor()`, `insertPoints()` all use it) — use the same value in every new function, not a fresh one.

---

## File Structure

All changes land in two existing files — no new files:

- **`src/ui.html`** — six new pure functions (Tasks 1–4, 6, 7), added inside or immediately after the existing `// >>> seam-pick` / `// <<< seam-pick` marked region (`ui.html:1286-1330`) so `magnet()` shares that region, plus a brand-new `// >>> move-target` / `// <<< move-target` region. That region ends up holding more than its name suggests — the drag classification/commit functions (Tasks 2–4) it's named for, plus `nudgeGroup()` (Task 6) and `pasteAnchor()` (Task 7), which piggyback on the same region rather than each getting their own marker pair, since they're small, share the same `node`-harness test convention, and don't need to be extracted independently of each other. Wiring changes (Tasks 5–7) modify the existing `card()` function's drag handlers (`ui.html:909-1049`) and the keydown handler (`ui.html:2052-2094`).
- **`src/test_server.py`** — new test functions following the exact existing convention: extract a marked region verbatim, splice in any other regions/stubs it calls, write a small fixture + assertions to a temp `.mjs` file, run it with `node`, skip gracefully if `node` isn't installed (see `test_a_bin_drop_only_snaps_to_a_seam_you_are_pointing_at`, `test_server.py:1714-1811`, as the exact template).

---

### Task 1: `magnet()` — shared radius primitive, extracted from `nearestPoint()`

**Files:**
- Modify: `src/ui.html:1286-1330` (the `seam-pick` region grows to include the new function, placed immediately before `nearestPoint()`)
- Test: `src/test_server.py` (new test function, placed after `test_a_bin_drop_only_snaps_to_a_seam_you_are_pointing_at`, i.e. after line 1811)

**Interfaces:**
- Produces: `magnet(t, points, pxPerSecond, secondsCap)` → the nearest object in `points` (each shaped `{t, ...anything}`) within the capped radius, or `null`. `secondsCap` is optional; when `null`/`undefined`, behaves exactly as today's `nearestPoint()` radius math (pixel cap + gap-share cap only). When a number, the effective radius is also capped at `secondsCap * pxPerSecond` pixels, whichever of the caps is tighter.
- Produces: `moveMagnetCap(pxPerSecond)` → the seconds cap for the move path: `Math.max(0.125, 3 / pxPerSecond)`.
- Consumes: nothing new — reads only its own parameters.

- [ ] **Step 1: Write the failing test for `magnet()`'s new `secondsCap` behavior**

Add to `src/test_server.py`, right after `test_a_bin_drop_only_snaps_to_a_seam_you_are_pointing_at` (after line 1811):

```python
def test_magnet_seconds_cap_is_optional_and_tighter_wins():
    """magnet() is nearestPoint()'s radius math pulled out to a pure function,
    plus one new optional rule: a caller can also cap the radius in seconds,
    and whichever of the pixel/gap-share/seconds caps is tightest wins.

    nearestPoint() must keep calling it with no seconds cap at all, so its own
    radius is completely unchanged — this test proves the new parameter is
    opt-in, not a change to the existing formula.
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> seam-pick")
    b = html.index("// <<< seam-pick")
    region = html[a:b]
    assert "function magnet(" in region, "magnet() must live inside the seam-pick region"
    assert "function nearestPoint" in region, "the seam-pick markers moved"
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; magnet() is JS)")
        return

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
let PX = 6;
let DOC = {fps: 24, clips: []};
function insertPoints() { return []; }   // unused by this test, magnet() only
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

// Two points 10 seconds apart, at 10px/s (100px apart on screen).
const points = [{t: 0, tag: 'a'}, {t: 10, tag: 'b'}];

// No secondsCap: behaves exactly like today's fixed pixel/gap-share math.
// SEAM_GRAB_PX=14, gap-share=0.25 of 100px=25px -> radius is 14px = 1.4s.
let r = magnet(1.3, points, 10, null);
if (!r || r.tag !== 'a') fail('no-cap: expected to snap to a within 1.4s, got ' + JSON.stringify(r));
r = magnet(1.5, points, 10, null);
if (r !== null) fail('no-cap: 1.5s away should be outside the 1.4s pixel-cap radius');

// secondsCap = 0.125: even though the pixel/gap-share caps would allow 1.4s,
// the seconds cap is tighter and must win.
r = magnet(0.1, points, 10, 0.125);
if (!r || r.tag !== 'a') fail('secondsCap: expected to snap within 0.125s, got ' + JSON.stringify(r));
r = magnet(0.2, points, 10, 0.125);
if (r !== null) fail('secondsCap: 0.2s away should be outside a 0.125s cap');

// At a high enough zoom the pixel cap (14px) is tighter than a 0.125s seconds
// cap (0.125*120=15px) -- whichever is tighter wins, not always the seconds
// one. Prove it by bracketing the PIXEL cap's own edge (14px), not the
// seconds cap's -- a point outside 14px but still inside 15px must be null,
// or the seconds cap would be winning instead of the pixel one.
r = magnet(13.5/120, points, 120, 0.125);   // 13.5px away, inside the 14px pixel cap
if (!r) fail('13.5px should be inside the 14px pixel cap');
r = magnet(14.5/120, points, 120, 0.125);   // 14.5px away: outside 14px, inside 15px
if (r !== null) fail('14.5px should be null -- the tighter 14px pixel cap must bind, not the 15px seconds cap');

console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "magnet.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()


def test_move_magnet_cap_matches_every_zoom_step():
    """The move-path's own radius (max(0.125s, 3px worth of seconds)), worked
    out against every real ZOOMS step. See the design spec piece 1 for why
    these exact numbers: 6px/s and 10px/s exceed one grid cell (0.25s) on
    purpose -- Alt-held-drag is the safety net there, not this radius.
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> seam-pick")
    b = html.index("// <<< seam-pick")
    region = html[a:b]
    assert "function moveMagnetCap(" in region, "moveMagnetCap() must live inside the seam-pick region"
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; moveMagnetCap() is JS)")
        return

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
let PX = 6;
let DOC = {fps: 24, clips: []};
function insertPoints() { return []; }
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };
const ZOOMS = [6, 10, 18, 34, 64, 120];
const expected = [0.5, 0.3, 0.16666666666666666, 0.125, 0.125, 0.125];
ZOOMS.forEach((px, i) => {
  const got = moveMagnetCap(px);
  if (Math.abs(got - expected[i]) > 1e-9)
    fail(`at ${px}px/s expected cap ${expected[i]}, got ${got}`);
});
console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "cap.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()
```

Note: `moveMagnetCap()` returns the *seconds cap you pass into `magnet()`*, not the final pixel-bounded radius — that's why its own expected values are the flat `max(0.125, 3/px)` table (0.5, 0.3, 0.167, 0.125, 0.125, 0.125), not the `SEAM_GRAB_PX`-adjusted 120px/s figure (≈0.117s) from the spec — that adjustment happens inside `magnet()` itself when it takes the tighter of the two caps, not inside `moveMagnetCap()`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "magnet or move_magnet_cap" -v`
Expected: FAIL — `assert "function magnet(" in region` fails, since the function doesn't exist yet.

- [ ] **Step 3: Implement `magnet()` and `moveMagnetCap()`**

In `src/ui.html`, insert immediately before `function nearestPoint(t, pxPerSecond) {` (currently line 1308, inside the `seam-pick` region):

```js
// The pure radius rule nearestPoint() already enforces (merge points inside
// one rendered pixel, cap by a share of the gap to the next point, cap by
// SEAM_GRAB_PX), plus one optional rule: a caller can ALSO cap the radius in
// seconds. Passing no secondsCap reproduces nearestPoint()'s exact existing
// radius -- this function must not change what nearestPoint() does.
function magnet(t, points, pxPerSecond, secondsCap) {
  const px = pxPerSecond || PX;
  const pts = points
    .slice()
    .sort((a, b) => a.t - b.t)
    .filter((p, i, all) => i === 0 || (p.t - all[i - 1].t) * px > 1);
  if (!pts.length) return null;
  const near = pts.reduce((a, b) => Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a);
  const gapPx = pts.reduce((m, p) =>
    p === near ? m : Math.min(m, Math.abs(p.t - near.t) * px), Infinity);
  let radius = Math.min(SEAM_GRAB_PX, gapPx * SEAM_GRAB_SHARE);
  if (secondsCap != null) radius = Math.min(radius, secondsCap * px);
  return Math.abs(near.t - t) * px <= radius ? near : null;
}
// The move path's own radius cap, in seconds: see the design spec piece 1 for
// the full derivation. 0.125s (half the 0.25s drag grid) is the target from
// 18px/s up; a 3px pixel floor keeps the magnet from going fully inert at the
// low zoom this tool actually opens a multi-minute cut to (6-10px/s), at the
// KNOWN, ACCEPTED cost that a small deliberate crossfade needs Alt held at
// those zooms -- see Task 5's wiring of ev.altKey.
function moveMagnetCap(pxPerSecond) {
  return Math.max(0.125, 3 / pxPerSecond);
}
```

Then replace the body of `nearestPoint()` (currently lines 1308-1329) so it becomes a thin wrapper:

```js
function nearestPoint(t, pxPerSecond) {
  return magnet(t, insertPoints(), pxPerSecond || PX, null);
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "magnet or move_magnet_cap or bin_drop_only_snaps" -v`
Expected: all three PASS (the pre-existing bin-drop test included, to confirm no regression).

- [ ] **Step 5: Run the full suite**

Run: `cd ~/Documents/projects/cutroom && ./cutroom check`
Expected: all tests pass (149 + the 2 new ones = 151).

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/projects/cutroom
git add src/ui.html src/test_server.py
git commit -m "$(cat <<'EOF'
Extract magnet() from nearestPoint(), add the move-path radius cap

nearestPoint() becomes a thin wrapper with its exact existing radius
unchanged. magnet() adds one optional seconds cap on top of the
existing pixel/gap-share caps, so the move path (next task) can use a
tighter radius without touching bin-drop's proven behavior.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ey7eU6rXzoe6sGR31M3g8D
EOF
)"
```

---

### Task 2: Move-path candidates and flush-run detection

**Files:**
- Modify: `src/ui.html` — new `// >>> move-target` / `// <<< move-target` region, inserted immediately after `// <<< seam-pick` (after line 1330)
- Test: `src/test_server.py`

**Interfaces:**
- Consumes: `magnet(t, points, pxPerSecond, secondsCap)`, `moveMagnetCap(pxPerSecond)` (Task 1); `dur(c)`, `endOf(c)` (already global, `ui.html:419-420`)
- Produces: `moveCandidates(excludeUids, lane)` → array of `{t, clip, edge}` where `edge` is `'start'`, `'end'`, or `null` (for the two synthetic candidates: `{t: 0, clip: null, edge: null}` and `{t: <latest end among included clips>, clip: null, edge: null}`). Reads `DOC.clips` live, filtering to `c.lane === lane && !excludeUids.has(c.uid)`.
- Produces: `flushRun(originalT, originalDur, lane, excludeUid)` → `{members: [...clips sorted by t], start, end}`. `members` contains every OTHER clip (`c.lane === lane && c.uid !== excludeUid`) that chains flush (gap/overlap `< 1e-6`) outward from `[originalT, originalT + originalDur]` in either direction. `start`/`end` are the run's bounds *including* the dragged clip's own original span (so a clip alone with no flush neighbors returns `{members: [], start: originalT, end: originalT + originalDur}`).

- [ ] **Step 1: Write the failing tests**

Add to `src/test_server.py`, after the two tests from Task 1:

```python
def test_move_candidates_excludes_selection_and_uses_non_selected_end():
    """The move-path candidate set is scoped to one lane, excludes every
    selected clip (not just the lead), and its 'end of the lane' synthetic
    point is the latest end among the clips that remain -- never totalLen(),
    which would make the dragged clip's own moving end a candidate against
    itself whenever it's the last clip in the cut.
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> move-target")
    b = html.index("// <<< move-target")
    region = html[a:b]
    assert "function moveCandidates(" in region
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; moveCandidates() is JS)")
        return

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
let PX = 10;
let DOC = {fps: 24, clips: [
  {uid:'a', t:0, in:0, out:2, rate:1, lane:0},
  {uid:'b', t:2, in:0, out:3, rate:1, lane:0},
  {uid:'c', t:5, in:0, out:1, rate:1, lane:0},          // last clip in lane 0
  {uid:'x', t:0, in:0, out:9, rate:1, lane:1},           // different lane
]};
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

// Dragging 'c' (the last clip): its own moving end must NOT be a candidate.
const cands = moveCandidates(new Set(['c']), 0);
if (cands.some(p => p.clip && p.clip.uid === 'c'))
  fail('dragged clip must be excluded from its own candidate set');
const endCand = cands.find(p => p.clip === null && p.edge === null && p.t > 0);
if (!endCand || Math.abs(endCand.t - 5) > 1e-9)
  fail('end-of-lane candidate must be the latest end among non-dragged clips (5), got ' +
       (endCand && endCand.t));

// Only lane 0's clips are candidates -- lane 1's clip must not appear.
if (cands.some(p => p.clip && p.clip.uid === 'x'))
  fail('candidates must be scoped to the current drop lane only');

// Every remaining clip contributes both a start and an end candidate.
const aStarts = cands.filter(p => p.clip && p.clip.uid === 'a' && p.edge === 'start');
const aEnds   = cands.filter(p => p.clip && p.clip.uid === 'a' && p.edge === 'end');
if (aStarts.length !== 1 || aEnds.length !== 1)
  fail('expected exactly one start and one end candidate per remaining clip');

console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "candidates.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()


def test_flush_run_chains_outward_and_stops_at_a_gap_or_crossfade():
    """A flush run is the maximal chain of same-lane clips touching the
    dragged clip's own original span with zero gap and zero overlap in
    either direction -- a gap OR an existing crossfade both break the chain,
    since both are excluded by the spec ('no gaps, no existing crossfades').
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> move-target")
    b = html.index("// <<< move-target")
    region = html[a:b]
    assert "function flushRun(" in region
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; flushRun() is JS)")
        return

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
let PX = 10;
// A[0,2] flush B[2,5] flush C[5,6], then a GAP, then D[8,9] flush E[9,10].
let DOC = {fps: 24, clips: [
  {uid:'A', t:0, in:0, out:2, rate:1, lane:0},
  {uid:'B', t:2, in:0, out:3, rate:1, lane:0},
  {uid:'C', t:5, in:0, out:1, rate:1, lane:0},
  {uid:'D', t:8, in:0, out:1, rate:1, lane:0},
  {uid:'E', t:9, in:0, out:1, rate:1, lane:0},
]};
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

// Dragging B (t=2, dur=3): the run must include A and C but stop before the
// gap at 6-8, and must NOT include D or E.
const run = flushRun(2, 3, 0, 'B');
const ids = run.members.map(c => c.uid).sort();
if (ids.join(',') !== 'A,C') fail('expected members [A,C], got [' + ids.join(',') + ']');
if (Math.abs(run.start - 0) > 1e-9 || Math.abs(run.end - 6) > 1e-9)
  fail('expected run bounds [0,6], got [' + run.start + ',' + run.end + ']');

// Dragging D (t=8, dur=1): its run is just itself + E, independent of the
// first run across the gap.
const run2 = flushRun(8, 1, 0, 'D');
if (run2.members.map(c=>c.uid).join(',') !== 'E')
  fail('expected members [E], got [' + run2.members.map(c=>c.uid).join(',') + ']');
if (Math.abs(run2.start - 8) > 1e-9 || Math.abs(run2.end - 10) > 1e-9)
  fail('expected run bounds [8,10], got [' + run2.start + ',' + run2.end + ']');

// A clip with no flush neighbor at all is a run of one.
DOC.clips = [{uid:'lone', t:3, in:0, out:2, rate:1, lane:0}];
const run3 = flushRun(3, 2, 0, 'lone');
if (run3.members.length !== 0) fail('a lone clip must have zero members');
if (Math.abs(run3.start - 3) > 1e-9 || Math.abs(run3.end - 5) > 1e-9)
  fail('a lone clip run must be exactly its own span');

// An existing CROSSFADE (overlap) between the dragged clip and a neighbor
// breaks the chain just like a gap does.
DOC.clips = [
  {uid:'A', t:0, in:0, out:2, rate:1, lane:0},
  {uid:'B', t:1.5, in:0, out:3, rate:1, lane:0},   // overlaps A by 0.5s
];
const run4 = flushRun(1.5, 3, 0, 'B');
if (run4.members.length !== 0)
  fail('an existing crossfade must break the flush chain, got members ' +
       run4.members.map(c=>c.uid).join(','));

console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "flushrun.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "move_candidates or flush_run" -v`
Expected: FAIL — `html.index("// >>> move-target")` raises `ValueError` (substring not found).

- [ ] **Step 3: Implement the `move-target` region, `moveCandidates()`, and `flushRun()`**

In `src/ui.html`, insert immediately after `// <<< seam-pick` (after line 1330, before `function clearSeam() {`):

```js
// >>> move-target
// ── existing-clip drag: snap-to-abut, swap, and same-lane reorder ─────────
// See docs/superpowers/specs/2026-09-05-clip-drag-and-clipboard-design.md.
//
// Every clip's start and end, in ONE lane, excluding the clips currently
// being dragged. Two synthetic points -- 0, and the latest end among the
// clips that remain -- round the set out the same way insertPoints() adds
// an append point. ⚠️ NEVER totalLen(): it maxes over EVERY clip including
// the dragged one, which would make a clip's own moving end a zero-distance
// candidate against itself whenever it is the last clip in the lane.
function moveCandidates(excludeUids, lane) {
  const rest = DOC.clips.filter(c => c.lane === lane && !excludeUids.has(c.uid));
  const pts = [];
  for (const c of rest) {
    pts.push({t: c.t, clip: c, edge: 'start'});
    pts.push({t: endOf(c), clip: c, edge: 'end'});
  }
  pts.push({t: 0, clip: null, edge: null});
  const latestEnd = rest.length ? Math.max(...rest.map(endOf)) : 0;
  pts.push({t: latestEnd, clip: null, edge: null});
  return pts;
}
// The maximal chain of same-lane clips touching the dragged clip's own
// ORIGINAL span with zero gap and zero overlap, extended outward in both
// directions. A gap OR an existing crossfade both break the chain -- this
// mirrors "no gaps, no existing crossfades" in the design spec exactly.
// `members` never includes the dragged clip itself (it is not a real,
// unmoved member of anything during its own drag); `start`/`end` DO include
// its original span, since that is where the chain starts growing from.
function flushRun(originalT, originalDur, lane, excludeUid) {
  const E = 1e-6;
  const rest = DOC.clips.filter(c => c.lane === lane && c.uid !== excludeUid)
                        .sort((a, b) => a.t - b.t);
  const members = [];
  let start = originalT, end = originalT + originalDur;
  let grew = true;
  while (grew) {
    grew = false;
    for (const c of rest) {
      if (members.includes(c)) continue;
      if (Math.abs(c.t - end) < E) { members.push(c); end = endOf(c); grew = true; }
      else if (Math.abs(endOf(c) - start) < E) { members.push(c); start = c.t; grew = true; }
    }
  }
  members.sort((a, b) => a.t - b.t);
  return {members, start, end};
}
// <<< move-target
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "move_candidates or flush_run" -v`
Expected: both PASS.

- [ ] **Step 5: Run the full suite**

Run: `cd ~/Documents/projects/cutroom && ./cutroom check`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/projects/cutroom
git add src/ui.html src/test_server.py
git commit -m "$(cat <<'EOF'
Add move-path candidate builder and flush-run detection

moveCandidates() is the lane-scoped, selection-excluded point set the
move-path magnet will search. flushRun() finds the contiguous,
gap-free and crossfade-free run of clips the dragged clip started in
-- the boundary the seam-land/reorder classification (next task) uses
to decide which of the two a magnet hit means.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ey7eU6rXzoe6sGR31M3g8D
EOF
)"
```

---

### Task 3: Swap-neighbor lookup and target classification

**Files:**
- Modify: `src/ui.html` — inside the `move-target` region
- Test: `src/test_server.py`

**Interfaces:**
- Consumes: `magnet()`, `moveMagnetCap()` (Task 1); `moveCandidates()`, `flushRun()` (Task 2); `dur(c)`, `endOf(c)` (global)
- Produces: `findSwapNeighbors(originalT, originalDur, lane) → {before: clip|null, after: clip|null}` — the clip immediately before/after the dragged clip's *original* slot in `lane`, only if flush (zero gap, zero overlap) against it.
- Produces: `resolveMoveTarget(args) → {type:'swap', clip} | {type:'seam', landT} | {type:'reorder', run, newIndex} | null`, where `args = {draggedUid, origT, proposedT, dur0, lane0, dropLane, swapNeighbors, run, hoveredClip, pxPerSecond, altKey}`. `draggedUid` is excluded from the magnet's own candidate set internally (via `moveCandidates`), so the dragged clip's own live, moving position is never a candidate against itself. `origT` is the dragged clip's ORIGINAL `t` (same value as `t0` in the drag handler) — required to compute where the clip currently sits *among its own run's members*, which is not always index 0 (only the spec's own worked example happens to start there); do not substitute `run.start` for it, since that's only equal to `origT` when the dragged clip happens to be first in its run. `hoveredClip` is whichever clip (if any) the pointer's raw screen coordinate currently sits fully inside — the caller (Task 5) supplies it via a DOM hit-test; this function does not touch the DOM.

**A note on why the run-membership check, not a distance check, decides seam vs. reorder:** a candidate seam belongs to case (b) (reorder) exactly when moving the dragged clip's start there would reorder some *other* member of its own flush run — i.e. the candidate's owning clip (or the run's own outward boundary) is inside `run.members` (or equals `run.start`/`run.end`) **and** applying it would actually change the dragged clip's position among those members. A candidate is case (a) (plain seam-land) whenever it *doesn't* imply moving another clip — either because it's outside the run entirely (a different run, a gap, a different lane, the run's own boundary when the dragged clip is already there) or because, worked through the reindex below, nothing else would move. This plan computes both the newIndex and whether it changes anything in one pass, so there is no separate "is it my own edge" special case to get wrong.

- [ ] **Step 1: Write the failing tests**

Add to `src/test_server.py`:

```python
def test_find_swap_neighbors_requires_exact_flushness():
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> move-target")
    b = html.index("// <<< move-target")
    region = html[a:b]
    assert "function findSwapNeighbors(" in region
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; findSwapNeighbors() is JS)")
        return

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
let PX = 10;
let DOC = {fps: 24, clips: [
  {uid:'A', t:0, in:0, out:2, rate:1, lane:0},
  {uid:'B', t:2, in:0, out:3, rate:1, lane:0},     // flush after A's original slot
  {uid:'C', t:5.1, in:0, out:1, rate:1, lane:0},   // 0.1s gap after B -- NOT flush
]};
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

// Dragging B (t=2, dur=3): before=A (flush), after=C should be null (gapped).
const n = findSwapNeighbors(2, 3, 0);
if (!n.before || n.before.uid !== 'A') fail('expected before=A, got ' + JSON.stringify(n.before));
if (n.after !== null) fail('expected after=null across a 0.1s gap, got ' + JSON.stringify(n.after));

// Dragging A (t=0, dur=2): no clip before it at all.
const n2 = findSwapNeighbors(0, 2, 0);
if (n2.before !== null) fail('expected before=null at the head of the lane');
if (!n2.after || n2.after.uid !== 'B') fail('expected after=B, got ' + JSON.stringify(n2.after));

console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "neighbors.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()


def test_resolve_move_target_classification():
    """The full swap / seam-land / reorder / free decision, against the
    exact worked example from the design spec: run X[0,2], B[2,5], C[5,6].
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    # resolveMoveTarget() calls magnet()/moveMagnetCap(), which live in
    # seam-pick, not move-target -- both regions must be in scope together.
    sa, sb = html.index("// >>> seam-pick"), html.index("// <<< seam-pick")
    ma, mb = html.index("// >>> move-target"), html.index("// <<< move-target")
    region = html[sa:sb] + html[ma:mb]
    assert "function resolveMoveTarget(" in region
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; resolveMoveTarget() is JS)")
        return

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
let PX = 34;   // a zoom step where the move-magnet cap is a clean 0.125s
let DOC = {fps: 24, clips: [
  {uid:'X', t:0, in:0, out:2, rate:1, lane:0},
  {uid:'B', t:2, in:0, out:3, rate:1, lane:0},
  {uid:'C', t:5, in:0, out:1, rate:1, lane:0},
]};
function insertPoints() { return []; }   // unused here, resolveMoveTarget never calls nearestPoint
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

const dur0 = 2, lane0 = 0, origT = 0;
const swapNeighbors = findSwapNeighbors(origT, dur0, lane0);   // X has no 'before', 'after'=B
const run = flushRun(origT, dur0, lane0, 'X');                  // members [B,C], bounds [0,6]

// 1) Hovering B's body (the fixed swap neighbor) -> swap, regardless of the
//    nearby magnet candidates.
let res = resolveMoveTarget({
  draggedUid: 'X', origT, proposedT: 2.4, dur0, lane0, dropLane: lane0, swapNeighbors, run,
  hoveredClip: DOC.clips[1], pxPerSecond: PX, altKey: false});
if (!res || res.type !== 'swap' || res.clip.uid !== 'B')
  fail('expected swap with B, got ' + JSON.stringify(res));

// 2) Proposed start near C's start (t=5), NOT hovering B's body -> this is a
//    slot boundary INSIDE the run (C is a member) other than X's own edge ->
//    reorder, landing X immediately before C (newIndex counts run members
//    with original t < 5: just B -> newIndex=1).
res = resolveMoveTarget({
  draggedUid: 'X', origT, proposedT: 4.95, dur0, lane0, dropLane: lane0, swapNeighbors, run,
  hoveredClip: null, pxPerSecond: PX, altKey: false});
if (!res || res.type !== 'reorder' || res.newIndex !== 1)
  fail('expected reorder at newIndex 1, got ' + JSON.stringify(res));

// 3) Proposed start near the run's own end (t=6, appending after C) ->
//    reorder, newIndex = run.members.length (2): dragged clip goes last.
res = resolveMoveTarget({
  draggedUid: 'X', origT, proposedT: 6.03, dur0, lane0, dropLane: lane0, swapNeighbors, run,
  hoveredClip: null, pxPerSecond: PX, altKey: false});
if (!res || res.type !== 'reorder' || res.newIndex !== 2)
  fail('expected reorder at newIndex 2 (append), got ' + JSON.stringify(res));

// 4) Proposed start near X's OWN original position (t=0, its own edge, run
//    boundary equal to where it already is) -> this is a no-op reorder
//    (newIndex identical to X's current index, 0) -- classify as 'seam',
//    a plain positional move, never a multi-clip commit for a no-op.
res = resolveMoveTarget({
  draggedUid: 'X', origT, proposedT: 0.02, dur0, lane0, dropLane: lane0, swapNeighbors, run,
  hoveredClip: null, pxPerSecond: PX, altKey: false});
if (!res || res.type !== 'seam')
  fail('expected a plain seam-land landing back on its own original slot, got ' +
       JSON.stringify(res));

// 5) A seam candidate belonging to a DIFFERENT run entirely (across a gap)
//    always classifies as 'seam', never 'reorder', regardless of distance.
DOC.clips.push({uid:'D', t:9, in:0, out:1, rate:1, lane:0});   // isolated, gap after C
res = resolveMoveTarget({
  draggedUid: 'X', origT, proposedT: 8.97, dur0, lane0, dropLane: lane0, swapNeighbors, run,
  hoveredClip: null, pxPerSecond: PX, altKey: false});
if (!res || res.type !== 'seam')
  fail('a seam outside the run must classify as seam-land, got ' + JSON.stringify(res));

// 6) Nothing in radius, nothing hovered -> free (null).
res = resolveMoveTarget({
  draggedUid: 'X', origT, proposedT: 20, dur0, lane0, dropLane: lane0, swapNeighbors, run,
  hoveredClip: null, pxPerSecond: PX, altKey: false});
if (res !== null) fail('expected free placement (null), got ' + JSON.stringify(res));

// 7) altKey suppresses the magnet (and therefore reorder/seam-land), but
//    NOT swap -- swap is a deliberate whole-body hover, not a proximity
//    magnet, and altKey never touched it in the spec.
res = resolveMoveTarget({
  draggedUid: 'X', origT, proposedT: 4.95, dur0, lane0, dropLane: lane0, swapNeighbors, run,
  hoveredClip: null, pxPerSecond: PX, altKey: true});
if (res !== null) fail('altKey must suppress the seam/reorder magnet, got ' + JSON.stringify(res));

// 8) THE CASE THAT CATCHES A WRONG currentIndex FORMULA: drag the LAST, and
// SHORTEST, member of a 3-clip run toward an EARLIER slot boundary. A[0,4],
// B[4,4.5], C[4.5,5] flush; dragging C (dur0=0.5, origT=4.5) to land at t=4
// (B's own start) is a real reorder -- C moves from index 2 to index 1 among
// its run's members [A,B]. A formula that derives "current index" from
// run.start and dur0 instead of from origT gets this wrong (it would count
// only A, matching newIndex=1, and wrongly call it a no-op).
DOC.clips = [
  {uid:'A', t:0,   in:0, out:4,   rate:1, lane:0},
  {uid:'B', t:4,   in:0, out:0.5, rate:1, lane:0},
  {uid:'C', t:4.5, in:0, out:0.5, rate:1, lane:0},
];
const dur0c = 0.5, origTc = 4.5;
const runC = flushRun(origTc, dur0c, 0, 'C');           // members [A,B], bounds [0,5]
const swapNeighborsC = findSwapNeighbors(origTc, dur0c, 0);
res = resolveMoveTarget({
  draggedUid: 'C', origT: origTc, proposedT: 3.97, dur0: dur0c, lane0: 0, dropLane: 0,
  swapNeighbors: swapNeighborsC, run: runC, hoveredClip: null, pxPerSecond: PX, altKey: false});
if (!res || res.type !== 'reorder' || res.newIndex !== 1)
  fail('dragging the last, shortest run member to an earlier slot must reorder '
       + '(newIndex 1), got ' + JSON.stringify(res));

console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "resolve.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "swap_neighbors or resolve_move_target" -v`
Expected: FAIL — functions don't exist yet.

- [ ] **Step 3: Implement `findSwapNeighbors()` and `resolveMoveTarget()`**

Add inside the `move-target` region, after `flushRun()`:

```js
// The clip immediately before/after the dragged clip's ORIGINAL slot in
// `lane`, kept only if it is exactly flush (zero gap, zero overlap) --
// fixed from the drag's starting position, never recomputed against
// wherever the pointer currently is, per the design spec.
function findSwapNeighbors(originalT, originalDur, lane) {
  const E = 1e-6;
  const rest = DOC.clips.filter(c => c.lane === lane);
  const before = rest.find(c => Math.abs(endOf(c) - originalT) < E) || null;
  const after  = rest.find(c => Math.abs(c.t - (originalT + originalDur)) < E) || null;
  return {before, after};
}
// How many places forward the dragged clip would move if reindexed to land
// at `landT` among its own run's members (used by both the classification
// below and commitReorder(), Task 4, so the two never compute it differently).
function reorderIndexFor(landT, run) {
  const E = 1e-6;
  return run.members.filter(c => c.t < landT - E).length;
}
// The full swap / seam-land / reorder / free decision for one pointermove.
// Nothing here touches the DOM or DOC beyond reading it -- `hoveredClip` is
// supplied by the caller from its own hit-test.
function resolveMoveTarget({draggedUid, origT, proposedT, dur0, lane0, dropLane, swapNeighbors,
                             run, hoveredClip, pxPerSecond, altKey}) {
  const E = 1e-6;
  // 1) Swap: only the two fixed original neighbors, only same lane, only
  // when the pointer is over that neighbor's own body right now.
  if (dropLane === lane0 && hoveredClip &&
      ((swapNeighbors.before && hoveredClip === swapNeighbors.before) ||
       (swapNeighbors.after  && hoveredClip === swapNeighbors.after))) {
    return {type: 'swap', clip: hoveredClip};
  }
  if (altKey) return null;
  // 2) Otherwise, magnet on both of the dragged clip's proposed edges,
  // whichever is nearer wins. The dragged clip's own uid is excluded here --
  // without this, its own live (moving) t would be a candidate against
  // itself, exactly the no-op-magnet bug moveCandidates() was built to avoid.
  const cap = moveMagnetCap(pxPerSecond);
  const cands = moveCandidates(new Set([draggedUid]), dropLane);
  const hitStart = magnet(proposedT, cands, pxPerSecond, cap);
  const hitEnd   = magnet(proposedT + dur0, cands, pxPerSecond, cap);
  let hit = null, landT = null;
  if (hitStart && hitEnd) {
    hit = Math.abs(hitStart.t - proposedT) <= Math.abs(hitEnd.t - (proposedT + dur0))
      ? hitStart : hitEnd;
  } else hit = hitStart || hitEnd;
  if (!hit) return null;
  landT = (hit === hitStart) ? hit.t : hit.t - dur0;
  // 3) Classify: is the landing point inside the dragged clip's own run?
  if (dropLane !== lane0 || (landT < run.start - E) || (landT > run.end + E)) {
    return {type: 'seam', landT};
  }
  const newIndex = reorderIndexFor(landT, run);
  // How many of the run's OTHER members currently sit before the dragged
  // clip's own ORIGINAL position -- NOT run.start, which only coincides with
  // origT when the dragged clip happens to be first in its run. Using
  // run.start here silently misclassifies a real reorder as a no-op
  // whenever the dragged clip is 2nd-or-later in its run (see Task 3's own
  // regression test for the concrete case this was wrong on).
  const currentIndex = run.members.filter(c => c.t < origT - E).length;
  if (newIndex === currentIndex) return {type: 'seam', landT};
  return {type: 'reorder', run, newIndex};
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "swap_neighbors or resolve_move_target" -v`
Expected: both PASS. If `resolveMoveTarget`'s case 2/3 tests fail on the exact `newIndex`, check `reorderIndexFor`'s `<` vs `<=` against the epsilon direction in the failing assertion message — the test's worked numbers are the source of truth (they were checked by hand against the spec's own example).

- [ ] **Step 5: Run the full suite**

Run: `cd ~/Documents/projects/cutroom && ./cutroom check`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/projects/cutroom
git add src/ui.html src/test_server.py
git commit -m "$(cat <<'EOF'
Add swap-neighbor lookup and the swap/seam/reorder classification

resolveMoveTarget() is the single decision point piece 2 of the spec
describes in prose: swap (fixed original neighbors, broadest
hit-test) beats the magnet, which then splits into a plain seam-land
or a same-run reorder purely by whether landing there would actually
reindex any other clip -- never by a separate 'is this my own edge'
special case, since a true no-op reorder and a plain seam-land land
in the identical place anyway.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ey7eU6rXzoe6sGR31M3g8D
EOF
)"
```

---

### Task 4: Shared commit primitive (reindex-and-relayout) with legality gate

**Files:**
- Modify: `src/ui.html` — inside the `move-target` region
- Test: `src/test_server.py`

**Interfaces:**
- Consumes: `flushRun()` (Task 2), a `run` shaped the way it returns (Task 3's `resolveMoveTarget` already computed `newIndex` by the time this task's functions are called — `commitReorder` takes it as a plain argument, it doesn't call `reorderIndexFor()` itself); `timelineFault()` (`ui.html:1247`, existing); `dur(c)` (global)
- Produces: `commitReorder(draggedClip, run, newIndex)` → mutates `draggedClip.t` and every clip in `run.members` in place, laying the run's members plus the dragged clip out flush from `run.start`, in the new order. Also handles the swap case (a 2-member reindex is exactly a swap — see the worked check in Step 1).
- Produces: `commitWithGate(clips, mutate)` → calls `mutate()`, then `timelineFault()`; on a fault, restores every clip in `clips` to its pre-`mutate()` `.t` and returns the fault string; on success returns `null`. Generic — Task 5 (swap/reorder) and Task 6 (group nudge) both use it.

**Why this is the same operation for swap and reorder, and why it's provably correct:** reindexing a flush run and relaying it out flush from `run.start` cannot change the run's total span, because the sum of the members' durations plus the dragged clip's duration is invariant under reordering — only *which* clip sits at *which* offset changes. Worked by hand against the spec's own swap example (flush `A[t,t+a]` then `B[t+a,t+a+b]`, swapped): `run = flushRun` of either clip has `members=[the other one]`; reindexing `[A,B]` to `[B,A]` and relaying out from `run.start = t` gives `B.t = t`, `A.t = t + b` — exactly `B.t = A.t_old; A.t = A.t_old + B.dur_old`, the formula the spec derives independently. Step 1's test asserts this equivalence directly, not just "span preserved."

- [ ] **Step 1: Write the failing tests**

Add to `src/test_server.py`:

```python
def test_commit_reorder_matches_the_specs_hand_derived_swap_formula():
    """A 2-member reindex (swap) must produce EXACTLY B.t=A.t_old,
    A.t=A.t_old+B.dur_old -- not just 'nothing after them moved'. A wrong
    formula that happens to leave the tail alone should still fail this.
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> move-target")
    b = html.index("// <<< move-target")
    region = html[a:b]
    assert "function commitReorder(" in region
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; commitReorder() is JS)")
        return

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
let PX = 10;
let DOC = {fps: 24, clips: [
  {uid:'A', t:0, in:0, out:2, rate:1, lane:0},    // dur 2
  {uid:'B', t:2, in:0, out:5, rate:1, lane:0},    // dur 5, unequal to A on purpose
]};
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

const A = DOC.clips[0], B = DOC.clips[1];
const aTOld = A.t, bDurOld = dur(B);
const run = flushRun(A.t, dur(A), 0, 'A');          // members=[B], bounds=[0,7]
commitReorder(A, run, 1);                            // A moves to index 1 (after B)

if (Math.abs(B.t - aTOld) > 1e-9)
  fail('expected B.t = A.t_old (' + aTOld + '), got ' + B.t);
if (Math.abs(A.t - (aTOld + bDurOld)) > 1e-9)
  fail('expected A.t = A.t_old + B.dur_old (' + (aTOld+bDurOld) + '), got ' + A.t);
if (Math.abs(endOf(A) - 7) > 1e-9 || Math.abs(B.t - 0) > 1e-9)
  fail('the pair must occupy exactly the same combined span as before');

console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "swapformula.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()


def test_commit_reorder_moves_only_the_run_and_leaves_other_lanes_alone():
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> move-target")
    b = html.index("// <<< move-target")
    region = html[a:b]
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; commitReorder() is JS)")
        return

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
let PX = 10;
let DOC = {fps: 24, clips: [
  {uid:'X', t:0, in:0, out:2, rate:1, lane:0},
  {uid:'B', t:2, in:0, out:3, rate:1, lane:0},
  {uid:'C', t:5, in:0, out:1, rate:1, lane:0},
  {uid:'Y', t:0, in:0, out:9, rate:1, lane:1},     // a different lane entirely
]};
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

const X = DOC.clips[0];
const run = flushRun(X.t, dur(X), 0, 'X');   // members [B,C], bounds [0,6]
commitReorder(X, run, 2);                     // append X after C

const B = DOC.clips.find(c=>c.uid==='B'), C = DOC.clips.find(c=>c.uid==='C');
const Y = DOC.clips.find(c=>c.uid==='Y');
if (Math.abs(B.t - 0) > 1e-9 || Math.abs(C.t - 3) > 1e-9 || Math.abs(X.t - 4) > 1e-9)
  fail('expected B@0, C@3, X@4, got B@' + B.t + ' C@' + C.t + ' X@' + X.t);
if (Y.t !== 0) fail('a clip in a different lane must never move');

console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "reorderscope.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()


def test_commit_with_gate_reverts_a_cross_lane_nesting_fault():
    """Same-lane span-invariance does not guarantee legality -- a swap
    between unequal-duration clips can nest one of them inside a clip on a
    DIFFERENT lane, since the renderer's overlap check is lane-blind. The
    gate must revert every touched clip's t, not just refuse silently.
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> move-target")
    b = html.index("// <<< move-target")
    region = html[a:b]
    assert "function commitWithGate(" in region
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; commitWithGate() is JS)")
        return

    # timelineFault() lives outside both drag-clamp and move-target -- splice
    # it in exactly the way test_a_drag_stops_at_a_full_overlap_instead_of_nesting
    # already does (test_server.py:1830-1831).
    html_full = html
    tf_region = html_full[html_full.index("function timelineFault() {"):
                           html_full.index("// How far the clip before a seam reaches")]

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
const hasVideo = () => true;
let PX = 10;
// Lane 0: A[0,2] flush B[2,10] (dur 8). Lane 1: Z[1.9,2.4] -- a legal 0.1s/
// 0.4s crossfade pair straddling the A|B seam TODAY. After A and B swap
// (B moves to [0,8], A to [8,10]), Z sits fully nested inside the new B --
// illegal only AFTER the swap, which is the property this test actually
// needs: a fixture that's already faulty proves nothing about the commit.
let DOC = {fps: 24, clips: [
  {uid:'A', t:0, in:0, out:2, rate:1, lane:0},
  {uid:'B', t:2, in:0, out:8, rate:1, lane:0},
  {uid:'Z', t:1.9, in:0, out:0.5, rate:1, lane:1},
]};
__REGION_TIMELINEFAULT__
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

const A = DOC.clips[0], B = DOC.clips[1];
if (timelineFault()) fail('the fixture must start legal -- got: ' + timelineFault());
const aTOld = A.t, bTOld = B.t;
const run = flushRun(A.t, dur(A), 0, 'A');
const fault = commitWithGate([A, B], () => commitReorder(A, run, 1));
if (!fault) fail('expected a fault (Z would end up nested inside the swapped B)');
if (Math.abs(A.t - aTOld) > 1e-9 || Math.abs(B.t - bTOld) > 1e-9)
  fail('a reverted commit must restore BOTH clips to their exact original t');

console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "gate.mjs"
        js.write_text(
            harness.replace("__REGION_TIMELINEFAULT__", tf_region)
                   .replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()


def test_commit_with_gate_still_allows_repair_of_an_already_faulty_cut():
    """Mirrors the existing drag-clamp test's own 'a cut that is ALREADY
    faulty must still be draggable' case (test_server.py:1877-1886) -- a
    commit must not refuse just because the cut was illegal before it ran,
    or a timeline that arrived broken could never be nudged, swapped, or
    reordered back out of trouble.
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> move-target")
    b = html.index("// <<< move-target")
    region = html[a:b]
    tf_region = html[html.index("function timelineFault() {"):
                      html.index("// How far the clip before a seam reaches")]
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; commitWithGate() is JS)")
        return

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
const hasVideo = () => true;
let PX = 10;
// Already illegal: A and B start at the same instant.
let DOC = {fps: 24, clips: [
  {uid:'A', t:0, in:0, out:2, rate:1, lane:0, label:'A'},
  {uid:'B', t:0, in:0, out:1, rate:1, lane:0, label:'B'},
]};
__REGION_TIMELINEFAULT__
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

const A = DOC.clips[0], B = DOC.clips[1];
if (!timelineFault()) fail('the fixture must start faulty for this test to mean anything');
const fault = commitWithGate([B], () => { B.t = 5; });
if (fault) fail('a commit on an already-faulty cut must not be refused, got: ' + fault);
if (B.t !== 5) fail('the mutation must be kept, not reverted, on an already-faulty cut');

console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "gate_repair.mjs"
        js.write_text(
            harness.replace("__REGION_TIMELINEFAULT__", tf_region)
                   .replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "commit_reorder or commit_with_gate" -v`
Expected: FAIL — functions don't exist yet.

- [ ] **Step 3: Implement `commitReorder()` and `commitWithGate()`**

Add inside the `move-target` region, after `resolveMoveTarget()`:

```js
// Reindex the dragged clip to `newIndex` among its own run's members and
// relay the whole run out flush from run.start, each clip keeping its own
// duration. This is provably span-preserving (durations are invariant under
// reordering), so it needs no directional shift logic of its own -- and a
// 2-member run reindexed this way reproduces the spec's own hand-derived
// swap formula exactly (see the test in this task).
function commitReorder(draggedClip, run, newIndex) {
  const rest = run.members.slice();
  rest.splice(newIndex, 0, draggedClip);
  let t = run.start;
  for (const c of rest) { c.t = t; t += dur(c); }
}
// Snapshot every clip's t, run the mutation, check legality once, and
// revert everything on a fault -- the same shape insertAt() already uses
// for its own single-clip creation (ui.html:1377-1389), generalized to any
// number of clips whose t might change in one commit. Mirrors keepIfLegal()'s
// own gate.armed rule (ui.html:1241-1242): a cut that was ALREADY faulty
// before this commit must still be allowed to change, or a timeline that
// arrived illegal (e.g. from a hand-edited or agent-written project) could
// never be nudged, swapped, or reordered back out of trouble.
function commitWithGate(clips, mutate) {
  const wasFaulty = !!timelineFault();
  const before = clips.map(c => c.t);
  mutate();
  const fault = timelineFault();
  if (fault && !wasFaulty) {
    clips.forEach((c, i) => { c.t = before[i]; });
    return fault;
  }
  return null;
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "commit_reorder or commit_with_gate" -v`
Expected: all three PASS.

- [ ] **Step 5: Run the full suite**

Run: `cd ~/Documents/projects/cutroom && ./cutroom check`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd ~/Documents/projects/cutroom
git add src/ui.html src/test_server.py
git commit -m "$(cat <<'EOF'
Add the shared reindex-and-relayout commit, gated for cross-lane faults

commitReorder() handles both swap and same-run reorder as one
operation -- reindexing a flush run and relaying it out from its own
start cannot change the run's total span, which is checked here
against the spec's own hand-derived swap formula, not just against
"nothing after them moved". commitWithGate() generalizes insertAt()'s
existing snapshot/check/revert pattern to any number of clips, since a
swap between unequal-duration clips can still nest one of them inside
an unrelated clip on another lane.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ey7eU6rXzoe6sGR31M3g8D
EOF
)"
```

---

### Task 5: Wire the resolution pipeline into the real drag handler

**Files:**
- Modify: `src/ui.html:909-1049` (the `card()` function's `onpointerdown`/`onpointermove`/`onpointerup` handlers)
- Modify: `src/ui.html:2129` (the window `blur` handler)

**Interfaces:**
- Consumes: `resolveMoveTarget()`, `commitWithGate()`, `commitReorder()`, `findSwapNeighbors()`, `flushRun()` (Tasks 2-4); existing `keepIfLegal()`, `snap()`, `g0`, `gate`, `minT`, `minLane` (all already in `card()`'s closure, unchanged)
- Produces: nothing new for other tasks to consume — this is leaf wiring.

This task is DOM-dependent (pointer events, `classList`, live element lookup) and cannot be driven by the `node`-harness convention this file's pure-logic tests use. Its verification step is a manual check in a real running instance, matching how every other piece of interactive wiring in this codebase has been verified (e.g. the AGPL-link placement change, verified "in the actual browser against a live `cutroom serve` instance").

**An explicit scope decision, not an oversight:** a multi-clip (`MULTI`) selection gets NO magnet/seam-land benefit from this plan, not just no swap/reorder — `pendingTarget` is only ever resolved when `singleMove` (`group.length === 1`). The spec's own candidate-exclusion wording ("excluding every uid in `selected()` — not just `SEL` — so the dragged clip (and the rest of a multi-selection, where applicable) is never a candidate for its own magnet") could be read as implying group drags should still get seam-land. This plan deliberately keeps that narrower: extending seam-land to a group is a real, separate feature (which member's edge does the magnet aim from? does the whole group need to test the same candidate, or does each member get its own?) that the spec never actually specifies for the group case, and guessing an answer here risks the exact kind of silent design decision this project's review process exists to catch. A multi-selection drag keeps today's exact, unchanged free-move-together behavior. Follow up with the user/spec author before extending this, rather than assuming.

- [ ] **Step 1: Add the swap-neighbor and flush-run snapshot at drag start**

In `src/ui.html`, inside `card()`'s `d.onpointerdown = e => {` handler, immediately after the existing line (`ui.html:969`) `const minT    = Math.min(...g0.map(([,T]) => T));`, add:

```js
    // Only meaningful for a single-clip move: swap/reorder never apply to a
    // multi-selection (see spec Non-goals). Computed once, from the ORIGINAL
    // position, exactly like g0 above -- other clips in lane0 are never
    // mutated during this drag (only `group`'s own members are), so nothing
    // here needs to be recomputed mid-gesture.
    const singleMove = mode === 'move' && group.length === 1;
    const swapNeighbors = singleMove ? findSwapNeighbors(t0, dur(c), lane0) : null;
    const run = singleMove ? flushRun(t0, dur(c), lane0, c.uid) : null;
    // Latched off the first time the drop lane differs from lane0 -- a
    // sticky flag, not re-evaluated as `lane !== lane0` on every
    // pointermove (see spec piece 2), since re-checking live would silently
    // re-offer swap if the pointer wanders back to lane0 later in the drag.
    let swapLatchedOff = false;
    let pendingTarget = null;
```

- [ ] **Step 2: Replace the entire move-mode block in `onpointermove` with the resolution pipeline**

Still in `src/ui.html`, inside `d.onpointermove = ev => {`, replace the ENTIRE existing `if (mode==='move') { ... }` block (`ui.html:986-1013`, from `if (mode==='move') {` through the `d.style.top = (8 + dl*LANE_H) + 'px';` line and its closing `}`) with the following. Do not patch this in with several separate insert-after-this-line edits — the horizontal delta (`want`) has to be computed strictly *after* `dropLane` is known (since `resolveMoveTarget` needs it), which is *before* the vertical lane-tracking code runs today; replacing the whole block in one piece is what keeps that order correct instead of leaving `keepIfLegal(want, ...)` referencing a `want` that isn't in scope yet:

```js
      if (mode==='move') {
        // The grabbed clip sets the delta; everyone else follows it, so the shape of
        // the selection is rigid and only its position changes.
        const proposedT = t0 + ds;
        // Vertical: one lane per LANE_H of travel. Dropping one row past the
        // last lane creates a new one, so a clip never needs a button first.
        const wantLane = lane0 + Math.round((ev.clientY-y0)/LANE_H);
        const next = Math.max(0, Math.min(laneCount, wantLane));
        if (next !== lane) {
          lane = next;
          document.querySelectorAll('.lane').forEach((L,i) =>
            L.classList.toggle('target', i===lane));
        }
        const dropLane = lane;
        if (singleMove && dropLane !== lane0) swapLatchedOff = true;
        let hoveredClip = null;
        if (singleMove && !swapLatchedOff) {
          // elementsFromPoint (plural), not elementFromPoint: the dragged
          // card itself (.clip.dragging, z-index:20, no pointer-events:none
          // -- ui.html:214) is always topmost under the pointer, so the
          // singular form would always return the dragged card and never
          // see what's underneath it.
          const stack = document.elementsFromPoint(ev.clientX, ev.clientY);
          const cardEl = stack.find(n => n.classList && n.closest('.clip')
                           && n.closest('.clip').dataset.uid !== c.uid);
          hoveredClip = cardEl
            ? DOC.clips.find(x => x.uid === cardEl.closest('.clip').dataset.uid) : null;
        }
        pendingTarget = singleMove ? resolveMoveTarget({
          draggedUid: c.uid, origT: t0, proposedT, dur0: dur(c), lane0, dropLane,
          swapNeighbors: swapLatchedOff ? {before: null, after: null} : swapNeighbors,
          run, hoveredClip, pxPerSecond: PX, altKey: ev.altKey,
        }) : null;
        // A pending swap or reorder still lets the card follow the pointer
        // (through the same continuous clamp below) for visual feedback --
        // only a resolved seam-land pins it exactly on the target. Either
        // way, the COMMIT in onpointerup (Step 4) never trusts wherever the
        // card ends up visually; swap explicitly reads t0, and reorder's
        // commitReorder() never reads the dragged clip's t at all.
        const landedT = (pendingTarget && pendingTarget.type === 'seam')
          ? pendingTarget.landT : snap(proposedT, ev.altKey);
        // ⚠️ Clamp the DELTA at the earliest clip, never each clip at zero on its own:
        // per-clip clamping folds the whole selection onto t=0, losing the shape AND
        // stacking clips on one instant, which the renderer rejects as a full overlap.
        const want = Math.max(landedT - t0, -minT);
        const dt = goodDt = keepIfLegal(want, goodDt,
          v => { for (const [x, xt] of g0) x.t = xt + v; }, gate);
        document.querySelectorAll('.clip.swap-target').forEach(n => n.classList.remove('swap-target'));
        if (pendingTarget && pendingTarget.type === 'swap') {
          // Matching on dataset.uid, not interpolating a uid into a
          // selector, per the existing warning at ui.html:1353-1358 about a
          // hand-edited uid containing a quote character.
          for (const el of document.querySelectorAll('.clip'))
            if (el.dataset.uid === pendingTarget.clip.uid) el.classList.add('swap-target');
        }
        // Same clamp on the vertical: the group hits lane 0, not the grabbed clip.
        const dl = Math.max(lane - lane0, -minLane);
        for (const [x, xt, xl] of g0) {
          x.t = xt + dt;
          if (x === c) continue;                     // the grabbed card is redrawn below
          const n = document.querySelector(`.clip[data-uid="${CSS.escape(x.uid)}"]`);
          if (n) { n.style.left = x.t*PX+'px'; n.style.top = (8 + dl*LANE_H) + 'px'; }
        }
        d.style.top = (8 + dl*LANE_H) + 'px';
      }
```

- [ ] **Step 3: Add the swap-target CSS**

Add near the existing `.clip.seam-l`/`.clip.seam-r` rules (find them with `grep -n "\.seam-l" src/ui.html` and add alongside):

```css
.clip.swap-target { outline: 2px solid var(--accent); outline-offset: -2px; }
```

- [ ] **Step 4: Commit the resolved target in `onpointerup`**

In `src/ui.html`, inside `d.onpointerup = d.onpointercancel = () => {` (`ui.html:1032`), the existing body ends with the 3-decimal rounding loop and then `draw(); save(); inspect(c);`. Add the commit **after** that rounding loop and **before** `draw(); save(); inspect(c);` — not earlier in the handler — so `commitReorder()`'s own precise, frame-derived positions are the final values, never re-quantized by the rounding loop afterward (which only touches `group`, i.e. `[c]` for a single move, and would otherwise leave the swap/reorder partner at full precision while `c` gets rounded to 3 decimals, reopening a sub-millisecond gap in what should be an exactly flush pair):

```js
      document.querySelectorAll('.clip.swap-target').forEach(n => n.classList.remove('swap-target'));
      let commitFault = null;
      if (singleMove && pendingTarget && pendingTarget.type === 'swap') {
        // Use t0, never live c.t: Step 2 lets the card follow the pointer
        // during a pending swap, so by the time this runs c.t may not be
        // t0 any more. `other` (the swap target) is never mutated during
        // this drag, so its live .t is safe to read as-is.
        const other = pendingTarget.clip;
        const [earlier, later] = t0 < other.t ? [c, other] : [other, c];
        const start = Math.min(t0, other.t);
        const pairRun = {members: [later], start, end: start + dur(earlier) + dur(later)};
        commitFault = commitWithGate([c, other], () => commitReorder(earlier, pairRun, 1));
      } else if (singleMove && pendingTarget && pendingTarget.type === 'reorder') {
        // commitReorder() never reads the dragged clip's current t (only
        // writes it), so no t0-vs-live concern here.
        commitFault = commitWithGate([c, ...pendingTarget.run.members],
          () => commitReorder(c, pendingTarget.run, pendingTarget.newIndex));
      }
      if (commitFault) note(commitFault, 'var(--warn)');
      pendingTarget = null;
```

- [ ] **Step 5: Extend the window `blur` handler**

Replace `src/ui.html:2129`:

```js
window.addEventListener('blur', () => { DRAGGING = false; });
```

with:

```js
window.addEventListener('blur', () => {
  DRAGGING = false;
  document.querySelectorAll('.clip.swap-target').forEach(n => n.classList.remove('swap-target'));
});
```

- [ ] **Step 6: Manual verification in a real browser**

This step has no automated test — DOM pointer-drag interaction in this file has none, by established convention. Follow it exactly:

1. Run `cd ~/Documents/projects/cutroom && ./cutroom serve <some-test-project>` (or use an existing project under `~/cutroom-projects/`).
2. **Seam-land**: at a zoom of 34px/s or higher (press `+` a few times), drag a clip to within a frame or two of another clip's edge and release. It should land exactly flush, no gap, no overlap.
3. **Crossfade still works via Alt**: at the same zoom, hold `Alt` and drag a clip to overlap another by a fraction of a second. It should NOT snap flush — the overlap should land exactly where released, becoming a crossfade.
4. **Crossfade without Alt at low zoom**: zoom all the way out (press `-` repeatedly, or reload a long cut). Drag a clip to overlap its neighbor by roughly a quarter-second. Per the spec's own honest accounting, this MAY snap flush instead of landing the crossfade — that's expected at 6-10px/s; if it does, redo the same drag with `Alt` held and confirm the crossfade lands correctly.
5. **Swap**: drag a clip fully onto its immediate flush neighbor's body (not near either one's edge, into the middle of the neighbor's card) and release. The neighbor should highlight while hovering; on release, the two clips should trade places, occupying exactly the same combined span as before.
6. **Swap does not apply across a gap**: confirm that hovering a clip that is NOT currently flush against the dragged clip's original position never highlights, even if you drag onto its body.
7. **Reorder**: in a lane with three or more flush clips (e.g. `A`,`B`,`C` all touching), drag `A` toward `C`'s far edge and release. `B` and `C` should shift left by `A`'s duration, `A` should land flush after `C`, and the run's total span should be unchanged (check the clip immediately after the run, if any, hasn't moved).
8. **Reorder never touches another lane**: with the same setup, add an unrelated clip on a different lane overlapping the same time range, and repeat step 7 — confirm it does not move.
9. **A cross-lane fault reverts cleanly**: construct (or find) a case where a swap/reorder would nest a clip on another lane (per Task 4's test fixture, adapted to real clips) and confirm both clips end up back at a LEGAL position with a warning note, rather than a broken one. Note this isn't necessarily their exact pre-drag position — `commitWithGate` reverts to whatever `c`'s position was at the moment of drop (which the continuous clamp already guarantees is legal on its own), not to `t0`, since the pointer may have moved between grabbing the clip and triggering the swap/reorder highlight.
10. Confirm `git status` shows no accidental project-file changes from this manual testing (or discard them) before moving on.

- [ ] **Step 7: Run the full suite**

Run: `cd ~/Documents/projects/cutroom && ./cutroom check`
Expected: all tests pass (no regressions from the wiring change — the pure-logic tests from Tasks 1-4 don't exercise this wiring at all, so this is purely a check that nothing else broke).

- [ ] **Step 8: Commit**

```bash
cd ~/Documents/projects/cutroom
git add src/ui.html
git commit -m "$(cat <<'EOF'
Wire seam-land/swap/reorder into the existing-clip drag handler

Verified by hand in a real browser -- this file has no automated
coverage for pointer-drag DOM interaction, only for the pure logic
that decides what a drag should do, which the last four commits
already cover. Swap and reorder preview via a highlight only and
commit at drop; seam-land and free placement keep following the
pointer continuously through the existing clamp, unchanged.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ey7eU6rXzoe6sGR31M3g8D
EOF
)"
```

---

### Task 6: Arrow-key clip nudge

**Files:**
- Modify: `src/ui.html` — new pure function near the `move-target` region (or immediately after it); wiring inside the existing keydown handler (`ui.html:2052-2094`)
- Test: `src/test_server.py`

**Interfaces:**
- Consumes: `commitWithGate()` (Task 4); `dur(c)`, `endOf(c)` (global)
- Produces: `nudgeGroup(g0, frames)` → applies a frame-exact, group-delta-clamped nudge to every `[clip, originalT]` pair in `g0` (the same shape the existing drag handler already snapshots at `ui.html:964` — the caller builds a fresh `g0` from each clip's *current* `t` before every single call, exactly once per keypress, never reused across multiple presses), mutating each clip's `.t`. No separate `group` parameter — every clip it needs is already in `g0`. The clamp floor (`0`) is derived from `g0` itself, not passed in separately. Returns nothing — callers read the mutated clips directly, matching `commitReorder()`'s own style.

- [ ] **Step 1: Write the failing test**

```python
def test_nudge_group_is_frame_exact_and_clamps_the_group_not_each_clip():
    """Frame-integer arithmetic (no float drift after many presses), and the
    group clamps at its EARLIEST member, never each clip at zero on its own
    -- the same rule the existing drag handler already follows
    (ui.html:989-991), reused here rather than reinvented.
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> move-target")
    b = html.index("// <<< move-target")
    region = html[a:b]
    assert "function nudgeGroup(" in region
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; nudgeGroup() is JS)")
        return

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
let PX = 10;
let DOC = {fps: 24, clips: [
  {uid:'a', t:0.5, in:0, out:2, rate:1, lane:0},
  {uid:'b', t:3, in:0, out:1, rate:1, lane:1},
]};
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

// 24 single-frame presses at 24fps must land EXACTLY 1.0s later, not drift.
// Each press rebuilds g0 fresh from the clip's CURRENT t, exactly like the
// real keydown handler does -- reusing one stale g0 across many presses
// would just recompute the identical delta every time and never accumulate.
const A = DOC.clips[0];
for (let i = 0; i < 24; i++) nudgeGroup([[A, A.t]], 1);
if (Math.abs(A.t - 1.5) > 1e-9) fail('expected exactly 1.5 after 24 frame-presses, got ' + A.t);

// Group clamp: two clips move together; clamping must stop the WHOLE group
// at the earliest member's zero, not fold each clip to its own zero.
DOC.clips = [{uid:'x', t:0.5, in:0, out:2, rate:1, lane:0},
             {uid:'y', t:3,   in:0, out:1, rate:1, lane:0}];
const X = DOC.clips[0], Y = DOC.clips[1];
for (let i = 0; i < 48; i++) nudgeGroup([[X, X.t], [Y, Y.t]], -1);
if (Math.abs(X.t - 0) > 1e-9) fail('expected X clamped at exactly 0, got ' + X.t);
if (Math.abs(Y.t - 2.5) > 1e-9)
  fail('expected Y to stay 2.5 ahead of X (shape preserved), got ' + Y.t);

console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "nudge.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "nudge_group" -v`
Expected: FAIL — `nudgeGroup` doesn't exist yet.

- [ ] **Step 3: Implement `nudgeGroup()`**

Add inside the `move-target` region, after `commitWithGate()`:

```js
// One arrow-key press for a selected clip (and its whole MULTI group).
// `frames` is signed (±1 for a plain press, ±DOC.fps for Shift). Frame-INTEGER
// arithmetic, not repeated float addition on a running value, so many presses
// never drift off the frame grid the way rounding a running float to 3
// decimals every press (the drag commit's own rounding, ui.html:1043) would.
// The clamp is on the GROUP's delta at its earliest member, exactly
// ui.html:989-991's own rule, reused rather than reinvented.
function nudgeGroup(g0, frames) {
  const wantDelta = frames / DOC.fps;
  const earliest = Math.min(...g0.map(([, t0]) => t0));
  const delta = Math.max(wantDelta, -earliest);   // floor: earliest member can't go below 0
  for (const [clip, t0] of g0) {
    const frameIndex = Math.round(t0 * DOC.fps) + Math.round(delta * DOC.fps);
    clip.t = Math.round((frameIndex / DOC.fps) * 1e6) / 1e6;
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "nudge_group" -v`
Expected: PASS.

- [ ] **Step 5: Wire the nudge into the keydown handler**

In `src/ui.html`, the existing keydown handler (`ui.html:2052-2094`) currently has, near the top:

```js
document.addEventListener('keydown', e => {
  if (document.activeElement.tagName==='INPUT') return;
  if (e.code==='Space') { e.preventDefault(); document.getElementById('play').click(); }
  if (e.key==='='||e.key==='+') zoom(1);
  if (e.key==='-') zoom(-1);
  if (e.key==='ArrowLeft')  { e.preventDefault(); scrubTo(CLOCK - (e.shiftKey?1:1/DOC.fps)); }
  if (e.key==='ArrowRight') { e.preventDefault(); scrubTo(CLOCK + (e.shiftKey?1:1/DOC.fps)); }
```

Add a module-level debounce handle near the other module state (find `let RAF = null, T0 = 0, CLOCK = 0;` at `ui.html:414` and add alongside it):

```js
let NUDGE_SAVE_TIMER = null;
// Returns the flush's own promise so a caller that must not race a pending
// nudge (undoRedo(), a manual history restore) can await it before doing
// anything else.
function flushNudgeSave() {
  if (!NUDGE_SAVE_TIMER) return Promise.resolve();
  clearTimeout(NUDGE_SAVE_TIMER);
  NUDGE_SAVE_TIMER = null;
  return save();
}
```

**Replace the whole quoted block above** (`ui.html:2052-2058`: the `document.addEventListener('keydown', e => {` line's `INPUT` early-return, through both existing `ArrowLeft`/`ArrowRight` lines) with:

```js
document.addEventListener('keydown', e => {
  const tag = document.activeElement.tagName;
  const nudgeable = SEL && !DRAGGING && !e.metaKey && !e.ctrlKey
    && tag !== 'INPUT' && tag !== 'SELECT' && tag !== 'TEXTAREA';
  if (nudgeable && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
    e.preventDefault();
    const dir = e.key === 'ArrowLeft' ? -1 : 1;
    const frames = dir * (e.shiftKey ? DOC.fps : 1);
    const group = selected();
    const g0 = group.map(x => [x, x.t]);   // fresh snapshot, this press only
    // commitWithGate snapshots every clip's t itself, runs the mutation, and
    // reverts on a fault -- nudgeGroup must only ever be called from inside
    // this callback, never applied first and checked after.
    const fault = commitWithGate(group, () => nudgeGroup(g0, frames));
    if (fault) note(fault, 'var(--warn)');
    else {
      const f = document.getElementById('f-t');
      if (f && SEL) f.value = DOC.clips.find(x => x.uid === SEL).t;
      draw();
      if (NUDGE_SAVE_TIMER) clearTimeout(NUDGE_SAVE_TIMER);
      NUDGE_SAVE_TIMER = setTimeout(() => { NUDGE_SAVE_TIMER = null; save(); }, 300);
    }
    return;
  }
  if (document.activeElement.tagName==='INPUT') return;
  if (e.code==='Space') { e.preventDefault(); document.getElementById('play').click(); }
  if (e.key==='='||e.key==='+') zoom(1);
  if (e.key==='-') zoom(-1);
  if (!SEL) {
    if (e.key==='ArrowLeft')  { e.preventDefault(); scrubTo(CLOCK - (e.shiftKey?1:1/DOC.fps)); }
    if (e.key==='ArrowRight') { e.preventDefault(); scrubTo(CLOCK + (e.shiftKey?1:1/DOC.fps)); }
  }
```

`draw()` re-renders every card from `DOC.clips`, which is safe here since nothing is mid-drag (the `!DRAGGING` guard above already ensures that), unlike the warning at `ui.html:2070-2072` about `draw()` during a live drag. Guarding on `!e.metaKey && !e.ctrlKey` keeps `Cmd+←`/`Cmd+→` (browser back/forward) and `Cmd+⇧+←`/`→`-style combos out of the nudge path.

**The flush must be awaited, not fired and forgotten**, everywhere a pending nudge could race a `save()` or a history restore:

- In `undoRedo()` (`ui.html:1980`), change its first line from `if (!DOC || DRAGGING || UNDO_BUSY) return;` to await the flush *before* that guard runs, since the guard itself doesn't care about a pending nudge but the fetch just below it does:
  ```js
  async function undoRedo(dir) {
    await flushNudgeSave();
    if (!DOC || DRAGGING || UNDO_BUSY) return;
  ```
- In `HIST.onchange` (`ui.html:1936`), add `await flushNudgeSave();` as its first line too — a manual history restore calls `invalidateUndo()` and adopts a new document the same way `undoRedo()` does, with the identical exposure to a pending nudge.
- In the `window.addEventListener('blur', ...)` handler from Task 5, add `flushNudgeSave();` alongside the existing `DRAGGING = false;` line — a `blur` handler can't be `async`, so this one is fire-and-forget by necessity (there's no later code in that handler for it to race).

- [ ] **Step 6: Update the header keyboard tip and the clip card title**

Find the header tip text (`grep -n "one frame" src/ui.html`, expect `ui.html:312`) and update it to note the conditional behavior, e.g. change `← → one frame` to `← → one frame (moves selected clip)`.

In `card()`, update the `d.title` string (`ui.html:931`) — currently:

```js
  d.title = `drag to move · drag edges to trim · ${c.label} · ${dur(c).toFixed(2)}s · ${off ? 'OFFLINE — ' + (OFFLINE[c.uid].why)
                                                       : (m ? m.path : c.mid)}`;
```

Prepend `arrow keys to nudge · ` to the string.

- [ ] **Step 7: Run the full suite**

Run: `cd ~/Documents/projects/cutroom && ./cutroom check`
Expected: all tests pass.

- [ ] **Step 8: Manual verification**

1. Select a clip, press `ArrowRight` several times — confirm it moves one frame per press and the inspector's position field updates.
2. Hold `ArrowRight` for a second or two, release, then press `Cmd+Z` once — confirm it undoes the WHOLE held nudge in one step, not one frame at a time (this is the debounce-flush working).
3. With nothing selected, confirm `ArrowLeft`/`ArrowRight` still scrub the playhead exactly as before.
4. Select a clip, open the inspector's tool `<select>` (if a post-pass is configured) or the header's history `<select>`, and confirm arrow keys typed while that dropdown has focus do NOT nudge the clip (they should do nothing or behave as native `<select>` arrow navigation).

- [ ] **Step 9: Commit**

```bash
cd ~/Documents/projects/cutroom
git add src/ui.html src/test_server.py
git commit -m "$(cat <<'EOF'
Add frame-accurate arrow-key clip nudge

Selection wins: with a clip selected, arrows move it (Shift = one
second) instead of scrubbing the playhead: confirmed with the user as
the intended trade-off despite this firing often (any click selects a
clip; the razor auto-selects the cut's right half after every cut).
Frame-integer arithmetic avoids the float drift a running-total
approach would accumulate; the debounced save is flushed before
undo/redo and on blur so a held key produces one undo step, not one
per frame.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ey7eU6rXzoe6sGR31M3g8D
EOF
)"
```

---

### Task 7: Copy/paste

**Files:**
- Modify: `src/ui.html` — new pure function near the `move-target` region; wiring inside the keydown handler
- Test: `src/test_server.py`

**Interfaces:**
- Consumes: `frameSnap(t)` (`ui.html:1119`, existing); `endOf(c)`, `dur(c)`, `newUid()` (global); `timelineFault()` (existing)
- Produces: `pasteAnchor(copiedClips, clock)` → the anchor time (a number) for the earliest of `copiedClips` (each `{t, ...}`), given the current playhead `clock`.

- [ ] **Step 1: Write the failing test**

```python
def test_paste_anchor_lands_after_the_source_when_playhead_is_untouched():
    """Selecting a clip parks the playhead on it (inspect() sets
    CLOCK = c.t). The ordinary select/copy/paste gesture must not dead-end
    by trying to paste exactly on top of the clip it copied -- it anchors
    flush after the copied clip(s) instead. Moving the playhead first still
    anchors at the playhead, unchanged.
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> move-target")
    b = html.index("// <<< move-target")
    region = html[a:b]
    assert "function pasteAnchor(" in region
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; pasteAnchor() is JS)")
        return

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
let PX = 10;
let DOC = {fps: 24, clips: []};
function frameSnap(t) { return Math.round(t * DOC.fps) / DOC.fps; }
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

// Playhead untouched (still parked on the copied clip's own t): anchor
// flush after the LATEST end among the copied clips.
const copied = [{t: 2, in:0, out:5, rate:1}, {t: 2, in:0, out:2, rate:1}];  // two lanes, same start, ends at 7 and 4
let anchor = pasteAnchor(copied, 2);
if (Math.abs(anchor - 7) > 1e-9) fail('expected anchor at latest end (7), got ' + anchor);

// Playhead moved elsewhere: anchor there instead.
anchor = pasteAnchor(copied, 10.3);
if (Math.abs(anchor - frameSnap(10.3)) > 1e-9)
  fail('expected anchor at frameSnap(clock), got ' + anchor);

console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "pasteanchor.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "paste_anchor" -v`
Expected: FAIL — `pasteAnchor` doesn't exist yet.

- [ ] **Step 3: Implement `pasteAnchor()`**

Add inside the `move-target` region, after `nudgeGroup()`:

```js
// Where a paste's earliest clip should land. Selecting a clip parks CLOCK on
// it (inspect(), ui.html:1838), so the ordinary select/Cmd+C/Cmd+V gesture
// with the playhead untouched would otherwise try to paste exactly on top
// of the source it just copied and get refused. Anchor flush after the
// copied clip(s) instead in exactly that case; anchor at the playhead
// (frame-snapped) in the general case of copy, move the playhead, paste.
function pasteAnchor(copiedClips, clock) {
  const earliestT = Math.min(...copiedClips.map(c => c.t));
  const snapped = frameSnap(clock);
  if (Math.abs(snapped - earliestT) < 1e-6) {
    return Math.max(...copiedClips.map(endOf));
  }
  return snapped;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "paste_anchor" -v`
Expected: PASS.

- [ ] **Step 5: Wire Cmd+C / Cmd+V into the keydown handler**

Add a module-level `CLIPBOARD` array near `MULTI`/`DRAGGING` (`ui.html:400`):

```js
let CLIPBOARD = [];
```

In the keydown handler, alongside the existing `Cmd+A` block (`ui.html:2076-2082`), add:

```js
  if ((e.key==='c'||e.key==='C') && (e.metaKey||e.ctrlKey) && !DRAGGING) {
    const sel = selected();
    if (!sel.length) return;   // let the browser's own copy happen when nothing is selected
    e.preventDefault();
    CLIPBOARD = sel.map(c => ({...c}));
    note(`${sel.length} clip${sel.length>1?'s':''} copied`);
    return;
  }
  if ((e.key==='v'||e.key==='V') && (e.metaKey||e.ctrlKey) && !DRAGGING && CLIPBOARD.length) {
    e.preventDefault();
    const known = new Set(MEDIA.map(m => m.mid));
    const usable = CLIPBOARD.filter(c => known.has(c.mid));
    const dropped = CLIPBOARD.length - usable.length;
    if (!usable.length) { note('nothing to paste — the copied media no longer exists', 'var(--warn)'); return; }
    const anchor = pasteAnchor(usable, CLOCK);
    const earliestT = Math.min(...usable.map(c => c.t));
    const liveUids = new Set(DOC.clips.map(x => x.uid));
    const fresh = usable.map(c => {
      let uid = newUid();
      while (liveUids.has(uid)) uid = newUid();
      liveUids.add(uid);
      const off = OFFLINE[c.uid];
      const clone = {...c, uid, t: c.t - earliestT + anchor};
      // Spread + override, not a bare reference: OFFLINE[c.uid].uid still
      // names the deleted source clip, and reusing that object as-is would
      // carry the wrong uid into the new entry.
      if (off) OFFLINE[uid] = {...off, uid};
      return clone;
    });
    DOC.clips.push(...fresh);
    const fault = timelineFault();
    if (fault) {
      for (const f of fresh) { DOC.clips.pop(); if (OFFLINE[f.uid]) delete OFFLINE[f.uid]; }
      note(`${fault}. Nothing was pasted.`, 'var(--warn)');
      return;
    }
    clearSel();
    SEL = fresh[0].uid; MULTI = new Set(fresh.slice(1).map(f => f.uid));
    draw(); save(); inspect(DOC.clips.find(x => x.uid === SEL));
    note(`pasted ${fresh.length} clip${fresh.length>1?'s':''}`
         + (dropped ? ` · ${dropped} skipped, source media no longer exists` : ''));
    return;
  }
```

- [ ] **Step 6: Run the full suite**

Run: `cd ~/Documents/projects/cutroom && ./cutroom check`
Expected: all tests pass.

- [ ] **Step 7: Manual verification**

1. Select a clip, `Cmd+C`, `Cmd+V` — confirm a copy appears flush after the original (not refused).
2. Select the same clip, `Cmd+C`, move the playhead elsewhere (scrub or `ArrowLeft`/`ArrowRight` with nothing selected), `Cmd+V` — confirm the copy lands at the playhead instead.
3. Select two clips on different lanes, `Cmd+C`, `Cmd+V` — confirm both paste, preserving their relative lane and time offset from each other.
4. Remove the source media of a copied clip from the bin (the `×` button) after copying but before pasting, then `Cmd+V` — confirm the paste either drops just that entry with a note, or (if it was the only copied clip) refuses cleanly with a note, rather than creating a broken clip.
5. `Cmd+C` with nothing selected — confirm no error, and the browser's own copy behavior (if any) is unaffected.
6. Paste an OFFLINE clip (drag in media, then make its file inaccessible, or use an existing OFFLINE clip in a test project) and confirm the pasted copy also shows as OFFLINE rather than rendering as a normal card.

- [ ] **Step 8: Commit**

```bash
cd ~/Documents/projects/cutroom
git add src/ui.html src/test_server.py
git commit -m "$(cat <<'EOF'
Add Cmd+C / Cmd+V clipboard for clips

Paste anchors flush after the copied clip(s) when the playhead is
still parked on the source -- the ordinary select/copy/paste gesture,
which would otherwise dead-end trying to paste exactly on top of what
it just copied -- and at the playhead in the general case of copying,
moving the playhead, then pasting. Filters out clipboard entries whose
source media was removed, carries OFFLINE status across, and is
refused (with a note, nothing created) the same way any other illegal
placement already is.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Ey7eU6rXzoe6sGR31M3g8D
EOF
)"
```

---

### Task 8: Media-pool insert regression check

**Files:**
- None modified — this is a verification-only task, since the spec requires `insertAt()`'s behavior to be completely unchanged.

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing new.

- [ ] **Step 1: Confirm the two pre-existing bin-drop assertions still hold**

Run: `cd ~/Documents/projects/cutroom && python3 -m pytest src/test_server.py -k "bin_drop_only_snaps" -v`
Expected: PASS — this is the same test from before Task 1, re-run here specifically to document that the whole feature landed without touching bin-drop.

- [ ] **Step 2: Manual verification**

1. Drag a media-pool item onto a lane far from any seam — confirm it drops exactly where released, no ripple, unchanged.
2. Drag a media-pool item onto a seam between two existing clips — confirm the insert-and-ripple behavior (everything after moves right, in every lane) is exactly as it was before this feature.

- [ ] **Step 3: No commit needed**

This task produces no code change — if Step 1 or Step 2 surfaces a regression, stop and fix it as a new task before proceeding; do not fold an unplanned fix silently into Task 9.

---

### Task 9: Full-suite run and final manual QA pass

**Files:**
- None modified — final verification only.

- [ ] **Step 1: Run the complete test suite**

Run: `cd ~/Documents/projects/cutroom && ./cutroom check`
Expected: every test passes, old and new (149 original + roughly 15 new = ~164).

- [ ] **Step 2: Re-run every manual verification checklist from Tasks 5, 6, 7, and 8 in a single continuous session**

Open one real project in the browser and run through all of them back to back, rather than one at a time in isolation — this is where an interaction between features (e.g. nudging a clip that was just pasted, or swapping two clips right after an undo) is most likely to surface, and none of the automated tests exercise cross-feature sequences.

- [ ] **Step 3: Confirm no stray project-file changes**

Run: `cd ~/Documents/projects/cutroom && git status`
Expected: only the files this plan intended to change (`src/ui.html`, `src/test_server.py`) — no project JSON, media, or `.snapshots/` files from manual testing. If any appear, they came from testing against a real project under version control by mistake; discard them (`git checkout -- <path>`) rather than committing them.

- [ ] **Step 4: Update `NOTES.md`'s test count if it's referenced there**

Run: `grep -n "149 tests\|test count" src/../NOTES.md 2>/dev/null || grep -rn "149" NOTES.md 2>/dev/null`
If a stale count is found, update it to match Step 1's actual final count. (The spec's own Non-goals section for the *separate* open-source-readiness backlog already flags `NOTES.md`'s stale test count as a known, unrelated cleanup item — do not scope-creep into fixing unrelated stale claims there, only the count this plan's own commits changed.)

---

## Self-Review

**Spec coverage** — every numbered piece of the spec has a task:
- Piece 1 (magnet primitive) → Task 1.
- Piece 2 (drag: seam-land/swap/reorder) → Tasks 2, 3, 4, 5.
- Piece 3 (arrow-key nudge) → Task 6.
- Piece 4 (media-pool insert, unchanged) → Task 8.
- Piece 5 (copy/paste) → Task 7.
- Piece 6 (testing convention) → threaded through every task's Step 1/2, using the exact existing extraction pattern.
- Non-goals (no trim magnet, no multi-select swap/reorder/magnet, no modifier key, no predictive badge) — Task 5's wiring only ever calls `resolveMoveTarget` for `mode==='move'` with `group.length===1` (`singleMove`), so trim and multi-select are structurally excluded, not just documented as excluded; Task 5 also carries an explicit note on the deliberate choice to withhold the magnet from multi-selection drags too, beyond what the spec's Non-goals states outright.

**Placeholder scan** — no TBD/TODO markers; every code block is complete, runnable JS or Python, not a description of what to write.

**Type/name consistency, checked across tasks** — `magnet()` (Task 1) is called with the exact same four-argument shape in Task 3's `resolveMoveTarget()`. `flushRun()`'s return shape (`{members, start, end}`) is used identically in Task 3 (classification), Task 4 (`commitReorder()`'s `run` parameter), and Task 5's wiring. `commitWithGate(clips, mutate)`'s signature (now returning `null` on an already-faulty cut it didn't make worse, not just on success) is used identically in Task 4's own tests, Task 5's swap/reorder commit, and Task 6's nudge commit. `pendingTarget`'s three non-null shapes (`{type:'swap',clip}`, `{type:'seam',landT}`, `{type:'reorder',run,newIndex}`) are produced only in Task 3 and consumed only in Task 5 — checked field-for-field. `resolveMoveTarget`'s `origT` (Task 3, added during review — the classification's `currentIndex` must be computed from the dragged clip's actual original position among its run's members, not from `run.start`, which only coincides with it when the clip happens to be first in its run) is threaded through correctly to Task 5's one call site (`origT: t0`).

**Two rounds of independent review already folded into this document, not left as open findings:** an earlier draft of Task 5 patched the move-mode block with several separate "insert after this line" edits, which put a `keepIfLegal(want, ...)` call ahead of the `want` it depends on — Task 5 Step 2 now replaces the whole block in one piece specifically to avoid that class of error recurring, with an explicit note not to re-fragment it into partial edits. An earlier draft also read `document.elementFromPoint` for swap hover-detection, which always returns the dragged card itself (topmost, no `pointer-events:none`) and would have made swap permanently unreachable; fixed to `elementsFromPoint` with an explicit skip-self.

**One thing still flagged for the implementer, not silently resolved:** line numbers throughout this plan were correct as of spec commit `bdb8ac4` / plan-writing time, but Tasks 1-4 add real lines to `src/ui.html` above where Task 5 operates. Before starting Task 5, re-locate the `card()` function's drag handler in the CURRENT file rather than trusting this plan's absolute line numbers for it.

---

**Plan complete and saved to `docs/superpowers/plans/2026-09-05-clip-drag-and-clipboard.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
