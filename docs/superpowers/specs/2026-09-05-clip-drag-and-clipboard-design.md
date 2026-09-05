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
  (header tip, `ui.html:336`). Any operation that changes a clip's `t` can
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
magnet(t, points, pxPerSecond) -> nearest point within radius, or null
```

holding exactly the three rules `nearestPoint()` already enforces — merge
points closer than one rendered pixel, cap the radius at a share of the
gap to the next point, cap it in pixels — plus one new rule:

- **Cap the radius in seconds as well as pixels: `min(existing cap, 0.125s)`.**
  Plain drags already quantize to the 0.25s grid (`snap()`, `ui.html:409`,
  applied at `ui.html:992`), so coarse positioning is already solved. The
  magnet's only job is landing exactly on an edge the grid can't express —
  a razor seam at 3.041667s, a media duration of 4.37s. At the working
  zoom levels in `ZOOMS`, the old radius (`SEAM_GRAB_PX` capped only by
  pixels and gap-share) works out to roughly 1.4 seconds — enough to pull
  a deliberate 1-second crossfade drag flush and destroy it. Capping at an
  eighth of a second means the magnet cannot move a release point far
  enough to eat any crossfade a person would actually author, at any zoom.

`nearestPoint()` keeps its exact name, signature, and `// >>> seam-pick` /
`// <<< seam-pick` markers (an existing test extracts that region verbatim
and re-implements `insertPoints()` against it — do not rename or reshape
either). It becomes a thin wrapper calling `magnet(t, insertPoints(), px)`.

The move path (piece 2) calls the same `magnet()` with its own candidate
set: every non-dragged, non-selected clip's `t` **and** its end
(`t + dur(c)`), plus 0 and the end of the cut. Candidates are computed
fresh from `DOC.clips` each call, excluding every uid in `selected()` —
**not just `SEL`** — so the dragged clip (and the rest of a multi-selection,
where applicable) is never a candidate for its own magnet. Missing this
exclusion makes the magnet a no-op that snaps a clip to where it already
is, since it's always the nearest point to itself.

`ev.altKey` suppresses the magnet on the move path exactly as it already
does on the bin-drop path (`ui.html:1404`, `1435`) and inside the move
handler's own `snap()` calls (`ui.html:992`) — free at no extra cost, one
shared convention.

## 2. Existing-clip drag: snap-to-abut and move-within-lane reorder

Applies to **move mode only** (dragging a clip's body). Trim-left and
trim-right are unchanged in this design — see Non-goals.

During the move handler's `onpointermove` (`ui.html:984` on), replace the
plain `snap(t0+ds, ev.altKey)` call with:

1. Test both the dragged clip's proposed start and its proposed end
   against `magnet()`'s move-path candidate set, and take whichever of the
   two is nearer to a candidate. (Not "whichever edge faces the direction
   of travel" — that rule breaks down under small jitters and gives the
   wrong answer for a clip being nudged back toward a seam it just left.)
2. **If within the capped radius of a seam** (a clip start or end): the
   clip previews flush so that its nearer edge lands exactly on that seam.
   On drop, this commits as an ordinary move — `c.t` is set accordingly.
   No ripple, matching the 2026-08-27 rule: this is the *existing* free
   move, made precise instead of pixel-guesswork.
3. **If the pointer is over the body of a clip that is the dragged clip's
   *original* immediate flush neighbor** (the clip that was, before this
   drag started, touching the dragged clip's start or end with zero gap
   and zero overlap, on the same lane) — not any clip dragged near, only
   one of these two, fixed at drag start like the `g0` snapshot pattern
   already used for group drags: highlight that neighbor's entire body.
   On drop, this commits a **swap**: the two clips trade `t`. Each keeps
   its own `in`/`out`/`rate`/duration.

   This is safe by construction for the *same-lane* arrangement: two
   clips that are flush neighbors occupy a combined span of
   `A.dur + B.dur` regardless of which one comes first, so nothing else in
   that lane needs to move, ever. It generalizes the same reasoning as a
   move onto a distant seam (below) to the smallest possible window — a
   swap **is** a move-within-lane with a window of one clip, not a
   separate mechanism.

   A clip's body reached by the pointer that is **not** one of the two
   fixed original-neighbor candidates is never highlighted for swap; it
   falls back to whichever of that clip's own start/end is within the
   magnet radius, same as any other seam target, or to free placement if
   neither is close enough.

   Only offered when `selected().length === 1`. A multi-selection drag
   never triggers swap — see Non-goals.

4. **If dropped on a same-lane seam that is not adjacent to the dragged
   clip's current slot** (i.e., there is at least one other clip between
   the old slot and the target): this is the general **move-within-lane**
   reorder that piece 2's swap is a special case of. On drop: extract the
   clip from its current slot; every clip strictly between the old slot
   and the target seam, in that same lane only, shifts by exactly the
   moved clip's duration, in the direction that closes the vacated slot
   and opens the target one. Clips outside that window, and every clip in
   every other lane, are untouched — no ripple beyond the window, no
   change to any other lane's absolute timing. The lane's total occupied
   span does not change, since nothing new was added, only reordered.
5. **Neither** — free placement, byte-identical to today's behavior
   (subject to the alt-key rule above).
6. Arrow-key nudge (piece 3) never goes through any of the above — it is
   always a raw ±1-frame move with no magnet and no reorder, which is how
   a crossfade or a deliberate gap gets authored under this design: mouse
   drag near another clip always resolves to flush-or-swap-or-far-move;
   only the keyboard makes an intentional overlap or gap.

Step 2 (seam-land) needs no new gating — it is still a single clip's `t`
following the pointer, already covered continuously by the existing
`keepIfLegal()` loop during the drag (`ui.html:1239`), same as any other
free move.

**Steps 3 and 4 (swap, move-within-lane) are a different shape of commit —
two or more clips' `t` values change at once, at drop, with no
continuous per-pointermove preview of the combined result — so each needs
its own gate, the same way `insertAt()` already gates its own commit**
(`ui.html:1377-1389`): snapshot every `t` about to change, apply all of
them, call `timelineFault()` once, and on a fault revert every one of them
and `note()` the reason instead of landing the change. Same-lane
span-invariance for a swap does not guarantee legality — a swap between
two different-duration clips can incidentally nest one of them inside an
unrelated clip on **another** lane, since the renderer's overlap check is
lane-blind. Move-within-lane (step 4) has the identical exposure for the
same reason. Free placement (step 5) is already continuously gated by the
existing `keepIfLegal()` loop during the drag and needs no new gate.

Hover highlighting cannot call `draw()` — nothing may repaint while a drag
is live (`ui.html:2086-2088`) — so it is applied and cleared via direct
`classList` manipulation on the existing card elements, matching how
`showSeam()`'s `mark()` already avoids rebuilding a selector out of a uid
(`ui.html:1353-1358`); follow that pattern, not the drag handler's
`CSS.escape` one, for anything new. The move path gets its own indicator
element/label — it must not reuse `showSeam()` as-is, since that function
hard-codes the text "insert here" and a crossfade-fit badge that are both
meaningless for a reorder that inserts nothing.

The window `blur` handler that resets `DRAGGING` (`ui.html:2113`) is
extended to also clear any move-path highlight, so a lost gesture (alt-tab,
a touch becoming a scroll) never strands one on screen.

After a swap or move-within-lane commits, re-show the clip head for the
clip that was dragged (`showClipHead`) rather than calling `scrubTo()` —
`scrubTo()` clears `HEAD_UID` via `paint()` (`ui.html:1613`) and would
flip the monitor off the clip just placed.

## 3. Arrow-key clip nudge

In the keydown handler (`ui.html:2052` on):

- If `SEL` is set, focus is not on `INPUT`, `SELECT`, or `TEXTAREA`
  (widened from the current `INPUT`-only check — the inspector's tool
  dropdown and the header's history dropdown are both reachable while a
  clip is selected), and not `DRAGGING`: ArrowLeft/ArrowRight move the
  selected clip and its whole `MULTI` group by one frame; Shift+Arrow
  moves by one second's worth of frames (`DOC.fps` frames). No `SEL` →
  arrows keep scrubbing the playhead exactly as today, unchanged.
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
  is derived from those snapshots (`ui.html:1940-1960`); holding an arrow
  for a second without debouncing would write dozens of undo entries for
  one held gesture, and ⌘Z would then walk back one frame at a time
  through all of them, permanently, for that project.
- Do not call `inspect()` per keypress — it moves `CLOCK` to the clip and
  reseeks the monitor (`ui.html:1855-1858`). Update the inspector's
  position field (`#f-t`) directly instead.
- Nudging touches `t` only, so the razor's two-grid constraint on `in`
  at rate ≠ 1 (`seamFor()`, `ui.html:1122-1134`) does not apply here —
  noted so nobody goes looking for it.
- Update the header's keyboard tip (`ui.html:314`, currently
  `← → one frame` unconditionally) and the clip card's `title` string
  (`ui.html:931`) to reflect that arrows move the clip when one is
  selected. The UI's own text is part of this change, not an afterthought.

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
  not an enumerated field list (`rebaseOnto()`, `ui.html:592`, is the
  standing example of what an enumerated list costs when a field is added
  later) — into a module-level `CLIPBOARD` array. No persistence across
  reload, no system-clipboard integration. Do not `preventDefault()` when
  nothing is selected, so a plain Cmd+C with no clip selected still lets
  the browser do whatever it would otherwise do.
- **Cmd+V**, with a non-empty `CLIPBOARD` and not `DRAGGING`: create fresh
  clips with `newUid()` for each copied clip, keeping their original lanes
  and relative time offsets from each other. Anchor the earliest copied
  clip's `t` on `frameSnap(CLOCK)` (not raw `CLOCK`), so the paste lands on
  the exact frame the monitor is showing and is on-grid both locally and
  on disk.
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

Mark the shared `magnet()` function and the move-within-lane / swap commit
logic with their own `// >>> name` / `// <<< name` comment pair(s),
following the exact convention already used for `seam-pick`
(`test_server.py:1724-1730` extracts that region verbatim and pipes it
through `node`, skipping with a printed message if `shutil.which("node")`
is `None`). Do not touch the existing `seam-pick` markers or
`nearestPoint()`'s name/signature — an existing test depends on both.

New coverage needed, run the same way:

- Magnet radius is capped at 0.125s regardless of zoom, at every step in
  `ZOOMS` — a 0.25s (or larger) deliberate crossfade drag must remain
  authorable at every zoom level.
- The dragged clip's own points are excluded from its candidate set.
- Swap of two flush, unequal-duration same-lane clips leaves nothing
  after them moved, in that lane or any other.
- Swap that would nest one clip inside a clip on another lane is refused
  and reverted, with both original `t` values intact afterward.
- Move-within-lane to a distant seam shifts only the clips strictly
  between old and new position, in that lane, and leaves every other lane
  untouched.
- Nudge arithmetic stays exactly on the frame grid after many consecutive
  presses (no float drift).
- Nudge respects the group-delta clamp (doesn't stack a group at zero).
- Paste drops a clipboard entry whose media was removed, with a note.
- Paste of a clip onto its own former instant is refused (video) /
  permitted (audio stem), matching `timelineFault()`'s existing rule.

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
