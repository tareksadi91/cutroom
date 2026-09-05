# Clip drag snapping, reordering, keyboard nudge, and clipboard

Status: approved design, not yet implemented.
Touches: `src/ui.html` only. No server, renderer, or project-schema changes.

## Motivation

Backlog items: dragging a clip already on the timeline has no snap at all
(free pixel positioning only), and there is no copy/paste. Both are small on
their own, but the snap fix interacts with drag-to-crossfade, reordering,
and keyboard control closely enough that building them separately would
produce inconsistent behavior at the seams (pun acknowledged). They ship
together.

## Existing invariants this design must not break

- **Dragging a clip already on the timeline never ripples anything**
  (`ui.html:970-975`, dated 2026-08-27). Overlapping one clip onto another
  is how a crossfade is authored — the tool's most common act. A prior
  attempt at nearest-seam snapping was removed for fighting this gesture.
  This design keeps that lesson but narrows what "fighting the gesture"
  means: a magnet capped at a fraction of a grid cell cannot swallow a
  crossfade a person would actually drag; a magnet in the 1+ second range
  can and did.
- **Media-pool insert (`insertAt()`) still ripples on purpose** — bringing
  in new footage grows the film, so everything after the insert point
  shifts, in every lane, to keep unrelated lanes in sync
  (`ui.html:1362-1363`). This design does not touch that function's
  behavior, only shares its seam-finding math.
- **A cut is undone by dragging the right half back over the seam**
  (`ui.html:1112-1114`, the razor's own comment). This has never actually
  been reachable, because nothing before this design lets a drag aim at a
  clip's *end*. Fixing that is in scope here.
- **The renderer's overlap check is lane-blind**: two clips blend into a
  crossfade whenever they overlap in time, in one lane or across lanes
  (header tip, `ui.html:331-333`). Any operation that changes a clip's `t` can
  therefore create a fault in a *different* lane than the one being edited,
  even when the edited lane's own arrangement looks fine.
- **Clamp the delta at the earliest clip, never each clip at zero**
  (`ui.html:989-991`). Any new group operation (nudge) must reuse this
  pattern, not reinvent it.

## Overview of the six pieces

1. Shared magnet primitive, used by both the existing bin-drop path and
   the new move path.
2. Existing-clip drag: snap-to-abut near a seam, or move-within-lane
   reorder when dropped on the flush neighbor it started next to.
3. Arrow-key clip nudge, frame-accurate, coexisting with playhead scrub.
4. Media-pool insert: unchanged behavior, shared indicator styling only.
5. Copy/paste.
6. Testing convention.

---

## 1. Shared magnet primitive

Extract the radius math out of `nearestPoint()` into a pure function:

```
magnet(t, points, pxPerSecond, secondsCap) -> nearest point within radius, or null
```

holding exactly the three rules `nearestPoint()` already enforces — merge
points closer than one rendered pixel, cap the radius at a share of the
gap to the next point, cap it at `SEAM_GRAB_PX` pixels — plus one new,
*optional* rule, active only when a caller passes `secondsCap`:

- **`nearestPoint()` passes no `secondsCap` and is byte-for-byte unchanged**
  — same formula, same radius; both assertions in the existing
  `test_a_bin_drop_only_snaps_to_a_seam_you_are_pointing_at`
  (`test_server.py:1714`, its two checks at `1767` and `1787`) still pass
  untouched. Bin-drop keeps its current, already-correct behavior; this
  design does not touch it.
- **The move path (piece 2) passes `secondsCap = max(0.125s, 3px worth of
  seconds at the current zoom)`.** Plain drags already quantize to the
  0.25s grid (`snap()`, `ui.html:422`, applied at `ui.html:992`), so coarse
  positioning is already solved — the magnet's only job is landing exactly
  on an edge the grid can't express (a razor seam at 3.041667s, a media
  duration of 4.37s). Using the bin-drop radius as-is for this purpose is
  wrong: at the zoom this tool actually opens to (`fitZoom()`,
  `ui.html:1892-1897`, picks the widest `ZOOMS` step — `[6, 10, 18, 34, 64,
  120]` — that fits the whole cut; for anything longer than a couple of
  minutes that's 6px/s), the existing radius resolves to roughly 1.4
  seconds — enough to pull a deliberate 1-second crossfade drag flush and
  destroy it.

  A pure seconds cap has the opposite failure at that same low zoom: 0.125s
  at 6px/s is 0.75px, effectively disabling the magnet. There is no single
  constant that is both "small enough to never eat a crossfade" and "large
  enough to be hittable by hand" at every zoom — the two pressures are in
  real tension at 6px/s, and no fix here removes that. The 3px floor keeps
  the magnet from being literally inert; it does **not** make precision
  edge-snapping comfortable at the lowest zoom, and the spec is explicit
  about that rather than implying one constant solves it: **snapping onto
  a specific frame-exact seam is a task the tool already expects zoom for**
  (the razor's own frame-vs-grid distinction, `ui.html:1112-1115`, already
  assumes this). Between 64px/s and 100px/s the cap is a clean 0.125s. At
  120px/s — the top `ZOOMS` step — 0.125s of screen space is 15px, one
  more than `SEAM_GRAB_PX` (14px, `ui.html:1292`), so the existing pixel
  cap binds there instead and the effective radius is 14/120 ≈ 0.117s, not
  0.125s. Both caps stay in force at every zoom; whichever is tighter at
  that zoom wins.

`magnet()` is defined **inside** the existing `// >>> seam-pick` /
`// <<< seam-pick` region (`test_server.py:1723-1726` extracts it verbatim
for testing), placed immediately before `nearestPoint()`, which becomes a
thin wrapper calling `magnet(t, insertPoints(), px, null)`. It does not
get its own marker pair — the seam-pick region simply grows to include
it, so the existing extraction test keeps working with no changes to its
harness, and a future move-path test (piece 6) that also needs `magnet()`
in scope can extract the same region.

The move path calls the same `magnet()` with its own candidate set: every
clip's `t` **and** its end (`t + dur(c)`) **for clips in the current drop
lane**, plus 0 and the latest end-time among those same non-selected,
same-lane clips (**not** `totalLen()`, which maxes over every clip
including the dragged one — using it verbatim would make the dragged
clip's own moving end a zero-distance candidate against itself whenever
it's the last clip in the cut, pinning it in place). Candidates are
computed fresh from `DOC.clips` each call, excluding every uid in
`selected()` — **not just `SEL`** — so the dragged clip (and the rest of a
multi-selection, where applicable) is never a candidate for its own
magnet. Missing this exclusion makes the magnet a no-op that snaps a clip
to where it already is, since it's always the nearest point to itself.

`ev.altKey` suppresses the magnet on the move path exactly as it already
does on the bin-drop path (`ui.html:1404`, `1435`) and inside the move
handler's own `snap()` calls (`ui.html:992`) — free at no extra cost, one
shared convention.

## 2. Existing-clip drag: snap-to-abut and move-within-lane reorder

Applies to **move mode only** (dragging a clip's body). Trim-left and
trim-right are unchanged in this design — see Non-goals.

During the move handler's `onpointermove` (`ui.html:984` on), replace the
plain `snap(t0+ds, ev.altKey)` call with a per-move resolution that
decides one of four outcomes — swap, seam-land, reorder, or free — checked
**in this order** (swap first —
its hit-test is deliberately broader than the seam magnet's, so it must
win any case where both would otherwise match), and stores the result in
a `pendingTarget` variable in the same closure that already holds `c`,
`mode`, and `group` — read back at `onpointerup` to decide what to commit,
since that handler takes no event of its own (`ui.html:1032`) and relies
entirely on state already tracked through `onpointermove`, exactly as the
existing free-move commit already does for `c.t`:

1. **Swap candidates are fixed once, at drag start**, the same way `g0` is
   snapshotted before any movement happens (`ui.html:964`): look up the
   clip immediately before and the clip immediately after the dragged
   clip in its *starting* lane, and keep whichever of those (if either) is
   currently flush against it (zero gap, zero overlap). This gives at most
   two fixed candidate clips for the whole gesture — never recomputed
   against whatever the pointer happens to be over as the drag continues.
   If the pointer is currently over the full body of one of these two
   fixed candidates: highlight that candidate's entire body, and set
   `pendingTarget = {type:'swap', clip: thatCandidate}`.

   The **first** time a gesture's drop lane differs from `lane0`, latch
   swap off for the rest of that gesture (a sticky flag, checked once it
   trips — not re-evaluated as `lane !== lane0` on every pointermove,
   which would silently re-offer swap if the pointer wanders back to
   `lane0` later in the same drag). The fixed candidates belonged to the
   starting lane and may no longer be flush against anything by the time
   the pointer returns. Once latched off, falls through to step 2 for the
   remainder of the gesture.

   Only offered when `selected().length === 1`; a multi-selection drag
   never sets a swap target — see Non-goals.

2. **Otherwise, test both the dragged clip's proposed start and its
   proposed end** against `magnet()`'s move-path candidate set (built from
   clips in the *current* drop lane only — reorder and seam-land are both
   single-lane operations; a drop near a seam in some other lane is
   covered by case (a) below), and take whichever of the two is nearer to
   a candidate. (Not "whichever edge faces the direction of travel" — that
   rule breaks down under small jitters and gives the wrong answer for a
   clip being nudged back toward a seam it just left.) If a candidate is
   within the capped radius, classify it against the **contiguous flush
   run** (no gaps, no existing crossfades) that currently contains the
   dragged clip's own slot in `lane0`:
   - **(a) The seam is one of the clip's own current edges** (it hasn't
     effectively moved), **or it lies outside that run entirely** — beyond
     a gap or an existing crossfade, or the drop lane isn't `lane0` at all:
     set `pendingTarget = {type:'seam', point}`. On drop this commits as
     an ordinary move, `c.t` set so the nearer edge lands exactly on the
     seam. No ripple: this is the *existing* free move, made precise
     instead of pixel-guesswork.
   - **(b) The seam is a slot boundary *inside* that run**, other than the
     clip's own current edges — i.e. there's at least one other clip
     between the dragged clip's old slot and this seam, all still within
     the same unbroken flush run: set `pendingTarget = {type:'reorder',
     point}` — see step 3. This is the ordinary case the feature exists
     for: reordering within an already-tight run of clips.

   (The earlier draft of this spec had these two cases backwards — flagged
   and fixed before implementation.)
3. **Reorder** (`pendingTarget.type === 'reorder'`, set by step 2): this is
   the general **move-within-lane** operation swap is a special case of.
   On drop: extract the clip from its current slot; every clip strictly
   between the old slot and the target seam, **within the same contiguous
   flush run the target seam belongs to**, in that same lane only, shifts
   by exactly the moved clip's duration, in the direction that closes the
   vacated slot and opens the target one. Clips outside that run, and
   every clip in every other lane, are untouched — no ripple beyond the
   run, no change to any other lane's absolute timing. The run's total
   occupied span does not change, since nothing new was added, only
   reordered. (A target seam outside the run the clip's old slot belongs
   to — beyond a gap, beyond a crossfade, or in another lane — is
   classified as case (a) in step 2, a plain seam-land, never a reorder.)
4. **Neither swap nor a magnet hit** — `pendingTarget = null`, free
   placement, byte-identical to today's behavior (subject to the alt-key
   rule above).
5. Arrow-key nudge (piece 3) never goes through any of the above — it is
   always a raw ±1-frame move with no magnet, no swap, and no reorder,
   which is how a crossfade or a deliberate gap gets authored under this
   design: mouse drag near another clip always resolves to
   flush-or-swap-or-reorder-or-free; only the keyboard makes an
   intentional overlap or gap.

**The swap commit itself**, given clips `A` (moves first, at drop) and `B`
(currently flush after it, i.e. `B.t == A.t + A.dur` before the swap): the
result must occupy exactly the same combined span `[A.t, A.t + A.dur +
B.dur]` as before, in the opposite order. That is **not** "trade `t`" —
trading raw `t` values only preserves the span when the two durations are
equal, and this design explicitly expects them not to be. The correct
commit, from a snapshot of both clips' original `t`/duration taken before
either is touched: `B.t = A.t_old; A.t = A.t_old + B.dur_old`. (Read
generally — whichever of the pair sits earlier keeps that earlier clip's
old start for whichever clip ends up first, and the one that ends up
second starts immediately after, using the *other* clip's original
duration. The order in the file above assumed `A` was earlier; if the
dragged clip was the *later* of the pair, swap the roles.)

`pendingTarget` on `pointercancel` is handled identically to
`pointerup` — that handler is already shared (`ui.html:1032`) specifically
so a lost gesture (alt-tab, a touch becoming a scroll) doesn't leave
`DRAGGING` stuck, and a cancelled plain move already commits wherever it
last was rather than reverting to the drag's start. Swap and reorder
follow the same existing convention rather than inventing a new
revert-on-cancel rule — and are protected the same way a cancelled plain
move already is, by the legality gate below, which applies regardless of
which of the two events ends the gesture.

**Seam-land (step 2's first branch) needs no new gating** — it is still a
single clip's `t` following the pointer, already covered continuously by
the existing `keepIfLegal()` loop during the drag (`ui.html:1239`), same
as any other free move.

**Swap and reorder are a different shape of commit — two or more clips'
`t` values change at once, at drop, with no continuous per-pointermove
preview of the combined result — so each needs its own gate, the same way
`insertAt()` already gates its own commit** (`ui.html:1377-1389`):
snapshot every `t` about to change, apply all of them, call
`timelineFault()` once, and on a fault revert every one of them and
`note()` the reason instead of landing the change. Same-lane
span-invariance for a swap does not guarantee legality — a swap between
two different-duration clips can incidentally nest one of them inside an
unrelated clip on **another** lane, since the renderer's overlap check is
lane-blind. Reorder has the identical exposure for the same reason. Free
placement (step 4) is already continuously gated by the existing
`keepIfLegal()` loop during the drag and needs no new gate.

Hover highlighting cannot call `draw()` — nothing may repaint while a drag
is live (`ui.html:2070-2072`) — so it is applied and cleared via direct
`classList` manipulation on the existing card elements, matching how
`showSeam()`'s `mark()` already avoids rebuilding a selector out of a uid
(`ui.html:1353-1358`); follow that pattern, not the drag handler's
`CSS.escape` one, for anything new. The move path gets its own indicator
element/label — it must not reuse `showSeam()` as-is, since that function
hard-codes the text "insert here" and a crossfade-fit badge that are both
meaningless for a reorder that inserts nothing.

The window `blur` handler that resets `DRAGGING` (`ui.html:2129`) is
extended to also clear any move-path highlight, so a lost gesture (alt-tab,
a touch becoming a scroll) never strands one on screen.

The existing `onpointerup` already ends with `draw(); save(); inspect(c);`
(`ui.html:1047`), and `inspect()` already re-shows the clip head
(`showClipHead`, `ui.html:1840`) — no extra repaint call is needed after a
swap or reorder commits; it falls out of the handler's existing tail.

## 3. Arrow-key clip nudge

In the keydown handler (`ui.html:2052` on):

- If `SEL` is set, focus is not on `INPUT`, `SELECT`, or `TEXTAREA`, and
  not `DRAGGING`: ArrowLeft/ArrowRight move the selected clip and its
  whole `MULTI` group by one frame; Shift+Arrow moves by one second's
  worth of frames (`DOC.fps` frames). No `SEL` → arrows keep scrubbing the
  playhead exactly as today, unchanged. **This focus check is local to the
  new nudge branch only** — a widened condition checked before deciding
  whether to nudge, not a change to the handler's existing top-level
  `document.activeElement.tagName==='INPUT'` early return (`ui.html:2053`).
  Widening that shared guard would also change Space/zoom/razor/⌘Z/⌘A
  behavior whenever a `<select>` has focus, which is out of scope here.
- **Frame-exact arithmetic, not repeated float addition**: each press
  computes `t = (Math.round(t * DOC.fps) + dir) / DOC.fps` per clip (dir
  is ±1 or ±DOC.fps frames for Shift), rounded to 6 decimals to match
  `render.snap()`. Rounding a running float to 3 decimals every press (the
  drag commit's own rounding, `ui.html:1043`) drifts off the frame grid
  over repeated presses; this must not.
- **Clamp the group delta at the earliest clip** (`minT`), exactly the
  drag's own rule (`ui.html:989-991`, `992-994`) — never clamp each clip
  at zero independently, which stacks the group onto one instant.
- **Legality-gated** the same way as piece 2's commits: apply, check
  `timelineFault()`, revert and `note()` on failure, so arrow nudge cannot
  become the one path that freely builds a nested clip.
- **Debounce the resulting `save()`** by ~300ms trailing. Every landed
  save writes a server-side snapshot pair (`_commit()`), and the undo list
  is derived from those snapshots (`ui.html:1959-1977`); holding an arrow
  for a second without debouncing would write dozens of undo entries for
  one held gesture, and ⌘Z would then walk back one frame at a time
  through all of them, permanently, for that project. `save()` also
  invalidates the undo cache (`invalidateUndo()`, `ui.html:731`); the
  debounce must be **flushed** (its trailing save forced through
  immediately) before ⌘Z/⌘⇧Z can run, before any other code path calls
  `save()`, and on blur/unload — otherwise an in-flight nudge inside the
  debounce window can be silently overwritten by a restore.
- Do not call `inspect()` per keypress — it moves `CLOCK` to the clip and
  reseeks the monitor (`ui.html:1836-1840`). Update the inspector's
  position field (`#f-t`) directly instead.
- Nudging touches `t` only, so the razor's two-grid constraint on `in`
  at rate ≠ 1 (`seamFor()`, `ui.html:1122-1134`) does not apply here —
  noted so nobody goes looking for it.
- Update the header's keyboard tip (`ui.html:312`, currently
  `← → one frame` unconditionally) and the clip card's `title` string
  (`ui.html:931`) to reflect that arrows move the clip when one is
  selected. The UI's own text is part of this change, not an afterthought.
- **Known, accepted cost**: with a clip selected, plain-frame playhead
  scrubbing via the arrow keys is unavailable until the clip is
  deselected (Escape, or clicking empty timeline) — confirmed as the
  intended behavior despite this firing often (any click selects a clip;
  the razor auto-selects the cut's right half after every cut,
  `ui.html:1167`). Not a gap to fix, a trade-off made knowingly.

## 4. Media-pool insert

No behavior change. `insertAt()` keeps rippling every lane on insert,
which is correct — new footage grows the film. The only shared code is
the underlying `magnet()` primitive (piece 1) and, where practical, the
seam-bar's geometry (position/height), *not* its label or fit-badge logic,
which piece 2's move indicator does not use.

## 5. Copy/paste

New, independent of pieces 1–4.

- **Cmd+C**, with a selection and not `DRAGGING`: structural-clone every
  own property of each selected clip via `{...c, uid: newUid-withheld}` —
  not an enumerated field list (`razor()`'s own `{...c, uid:newUid(), …}`
  at `ui.html:1156` is the standing example of a spread-clone; an
  enumerated list is what breaks silently the next time a clip field is
  added) — into a module-level `CLIPBOARD` array. No persistence across
  reload, no system-clipboard integration. Do not `preventDefault()` when
  nothing is selected, so a plain Cmd+C with no clip selected still lets
  the browser do whatever it would otherwise do.
- **Cmd+V**, with a non-empty `CLIPBOARD` and not `DRAGGING`: create fresh
  clips with `newUid()` for each copied clip, keeping their original lanes
  and relative time offsets from each other. **Anchor on `frameSnap(CLOCK)`,
  unless that would collide with the exact source clip(s) just copied** —
  the ordinary case of select, Cmd+C, Cmd+V with the playhead untouched,
  since selecting a clip parks `CLOCK` on it (`inspect()`, `ui.html:1838`).
  In that case, anchor instead immediately after the latest end-time among
  the *originally copied* clips (flush, on the same lane(s)) — matching
  "duplicate" in most editors: copy something, paste, get an adjacent copy,
  with no extra step. Copy, move the playhead elsewhere, then paste still
  anchors at `frameSnap(CLOCK)` as the general case.
  - **Filter against live media before pasting**: a clipboard entry whose
    `mid` no longer exists in `MEDIA` (the source was removed after copy,
    which cascade-deletes clips but not clipboard entries) is dropped from
    the paste, with a `note()` naming what was skipped and why, rather
    than creating a clip `validate()` will refuse.
  - Carry an `OFFLINE` entry across to the new uid if the copied clip had
    one, or the pasted copy renders as a normal card and misreports itself
    until the next reload.
  - Check the freshly-minted uids don't collide with any live uid before
    committing (cheap, and `validate()` enforces uniqueness).
  - Refused via `timelineFault()`, same gate as everything else in this
    design — for a video clip, pasting on top of itself is refused
    outright ("would start at the same instant"); for an audio stem, two
    identical overlapping copies are legal and will double that stem's
    level on export, which is correct behavior under the existing rule,
    not a bug this feature introduces.
  - Select the pasted clips afterward, the same way `razor()` selects the
    cut's right half (`ui.html:1167`).
  - No auto-snap on paste — it lands as a free placement (subject to the
    legality gate above); drag or arrow-nudge it into place afterward if
    it needs to be flush against something. This mirrors the existing
    free-drop-is-the-default philosophy, not an oversight.

## 6. Testing

As established in piece 1, `magnet()` lives inside the existing
`// >>> seam-pick` / `// <<< seam-pick` region (`test_server.py:1724-1730`
extracts that region verbatim and pipes it through `node`, skipping with a
printed message if `shutil.which("node")` is `None`) — no new marker pair
for it, and the existing test's assertions and harness inputs are
unchanged since `nearestPoint()`'s name, signature, and behavior are
unchanged.

The move-path target resolution (the swap/reorder/seam-land decision) and
the swap/reorder commit logic get their **own** `// >>> move-target` /
`// <<< move-target` marker pair, placed immediately after the `seam-pick`
region. Its test harness extracts **both** regions (seam-pick, then
move-target) and concatenates them before running under `node`, so
`magnet()` is already in scope for the move-target logic to call. The
swap/reorder commit also calls `timelineFault()`, which is neither region
— splice it in the same way `test_a_drag_stops_at_a_full_overlap_instead_of_nesting`
already does (`test_server.py:1830-1831`), rather than re-deriving it.

New coverage needed, run the same way:

- At the lowest zoom (`PX = 6`), the move-path magnet radius is small
  (pixel-floored, not zero) but a 0.25s or larger deliberate crossfade
  drag still lands as a crossfade, not a flush snap. Between 64px/s and
  100px/s the radius is exactly 0.125s; at 120px/s it's bounded by
  `SEAM_GRAB_PX` instead, ≈0.117s — assert the tighter of the two caps
  wins at every `ZOOMS` step, not a single constant everywhere.
- `nearestPoint()` (bin-drop) is unaffected: `test_server.py:1767` and
  `1787`'s existing assertions still hold with no changes.
- The dragged clip's own points are excluded from the move-path candidate
  set.
- Swap of two flush, unequal-duration same-lane clips: verify the actual
  commit formula (`B.t = A.t_old; A.t = A.t_old + B.dur_old`), not just
  that "nothing after them moved" — a wrong formula that still happens to
  leave the tail alone should still fail this test on the swapped clips'
  own positions.
- Swap that would nest one clip inside a clip on another lane is refused
  and reverted, with both original `t` values intact afterward.
- Reorder to a seam that is *inside* the clip's own flush run (case (b))
  shifts only the clips strictly between old and new position, in that
  lane, and leaves every other lane untouched.
- A seam beyond a gap, beyond an existing crossfade, or in another lane
  (case (a)) always classifies as a plain seam-land, never a reorder —
  including when it's further from the clip's old slot than an in-run
  seam would be.
- Nudge arithmetic stays exactly on the frame grid after many consecutive
  presses (no float drift).
- Nudge respects the group-delta clamp (doesn't stack a group at zero).
- Paste anchors flush after the copied clip(s) when the playhead is still
  parked on the source; anchors at `frameSnap(CLOCK)` when the playhead
  has moved elsewhere.
- Paste drops a clipboard entry whose media was removed, with a note.
- Paste of a clip onto its own former instant (general case, playhead
  moved back to that instant) is refused (video) / permitted (audio stem),
  matching `timelineFault()`'s existing rule.

## Non-goals (this design)

- **Trim-left/trim-right get no magnet.** The most useful place for one —
  closing a razor cut by dragging a trimmed edge back onto its sibling's
  seam — is real, but a trim magnet must additionally satisfy `seamFor()`'s
  two-grid rule at rate ≠ 1 (`ui.html:1122-1134`), which a `t`-only move
  magnet does not need to. Deferred as its own follow-up.
- **Swap and move-within-lane never apply to a multi-clip selection.**
  Dragging a `MULTI` selection keeps today's plain free-move-together,
  unchanged. A group version of either operation (preserving internal
  order, treating the group as one block) is real complexity deferred
  until there's a concrete need for it.
- **No modifier key is required to trigger swap.** Considered and
  rejected: the ambiguity a modifier would resolve (swap vs. a
  drag-to-crossfade landing in the same place) doesn't exist under this
  design, since mouse drag no longer creates crossfades at all — that's
  arrow-key nudge's job. Swap fires on a plain drop onto a clip's
  *original* immediate flush neighbor and nowhere else.
- **No predictive "this will leave a gap" badge on the swap highlight.**
  Unnecessary: a same-lane swap between flush neighbors cannot leave a
  gap by construction. The rarer cross-lane legality fault is handled by
  the standard gate-and-revert-with-note pattern already used everywhere
  else in this file, not by a new predictive UI.
