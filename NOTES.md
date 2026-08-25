# Rewrite notes

What the standalone version kept, what it deleted, the drag-and-drop decision,
and how each hard boundary is enforced and proved.

## Kept, deliberately unchanged

The render arithmetic was the expensive part of the old tool — nine P1 findings
from an independent review and three fix rounds — so it was moved, not
redesigned.

- **`render.py`**: `frames_at` / `on_grid` / `snap` / `snap_project`,
  `canon_clip` / `canonicalise`, `src_frames`, `duration`, `source_info` and its
  `(path, mtime_ns, size)` probe cache, the whole of `validate()` (grid rule,
  negative `t`/`in`, duplicate uid, `out <= in`, `rate <= 0`, same-`t` collapse,
  overlap-longer-than-the-shorter-clip, nesting, reach-past, trim-past-the-end
  in FRAMES, fps mismatch, VFR refusal, unprobeable-is-a-refusal), the
  `build_graph` left fold with its lead/gap/xfade/abut branches and frame
  quantisation of every gap, `settb=AVTB` on every segment, and `ENCODE`.
- **`server.py`**: the two-lock version guard (`threading.Lock` for this process
  + advisory `flock` for the agent), the snapshot-and-atomic-swap commit, the
  byte-comparison net before the rename, `edit_project()` (was `edit_edl`) and
  the written contract in the module docstring, the hand-rolled Range support
  including suffix ranges and 416s, the 400-not-a-dropped-connection handling,
  the non-blocking job lock, and `127.0.0.1`-only binding.
- **`ui.html`**: the design as approved. Same palette, type scale, three-column
  grid, header zones, five-second ruler, lane surfaces without borders, the
  crossfade hatch that spans the lane stack, drag/trim/lane-drag behaviour,
  program monitor with two cross-faded video elements, zoom steps, info-icon
  tooltips, and — verbatim, markers and all — the save loop with its coalescing
  and its `keep mine` / `take theirs` conflict banner.
- **The suites**: every test that still describes this program, including all
  the mutation-tested render tests. 35 render tests, 42 server tests.

Beat tint was kept as a *mechanism* and re-keyed: the hue now comes from the
media item rather than from a film's beat number, so a timeline still reads as
colour fields and two cuts of one source are visibly one source.

## Deleted, and why

| Gone | Why |
| --- | --- |
| `REPO`, `films/<film>/`, `film_dir` everywhere | Film coupling. A project is one JSON file at a fixed root; nothing joins a film directory. |
| `import_ledger`, `shot-ledger.md`, `clips/selected`, `clips/proto`, `drift_beat`, `camera_drift`, the 92s ceiling, the 5 Mbps print | Film-specific. `drift_beat` was a required clip field and a validator rule; it is not a timeline concept. |
| `bin_list()`, `probe_all()`, `_inside()`, the `probe.json` cache | Auto-indexing. `bin_list` globbed two directories; that whole concept is gone. Media enters only through `add_media()`, which is only ever called with paths a person named. Probe results now live in the `media` entry itself. |
| `safe_path()` | Replaced by `servable()`. The old gate confined a *relative* path to a film directory by resolve-and-compare-prefix. Paths are now arbitrary and absolute, so the rule inverts: membership in a list, compared as an exact string. |
| `preserve_raw`, `raw_rel`, `audit_raw`, `BackupSuspect`, `_sha`, the passes.jsonl ledger, `over_end` / `over_end_problems`, `BUST` cache-busting in the UI | All of it existed because a pass overwrote the master in place. A pass now writes a new file, so there is no backup to make, no injective backup-name mapping to get right, no "is this existing backup complete" decode audit, no checksum ledger, and no way for a pass to shorten a file a trim already depends on. About 180 lines of the most dangerous code in the tool deleted by a design change rather than made safer. |
| `TOOLS` (a hard-coded list of one film's scripts) | Passes come from the project's own `"passes"` map now. |
| `snap_cut()` / `--snap` | A one-off migration for a file written before the grid rule existed. No such file exists in a new tool. |
| numpy and PIL in the tests | "Stdlib only" should be true of the tests too. Frames are now read as raw `rgb24` bytes out of ffmpeg and measured with `sum()` and `max()`. |

## Drag-and-drop: what was chosen, and why

**Chosen: a CLI `cutroom add <project> <path>…`, plus an “Add media…” button
that opens the OS file picker server-side, plus a paste-a-path field — and a
real drop target that reads a path when the browser offers one and says so
loudly when it does not.**

The brief offered a `POST /media` endpoint called with a dropped file's *name*.
That cannot be built here: a name is not a path, so the server would have to go
find the file, and finding means scanning a directory — the one thing this
rewrite exists to forbid. The two ideas are structurally incompatible, and the
no-scanning rule wins.

So the path has to come from somewhere that actually has one:

- **`cutroom add`** — the shell already resolved it. Always works, including
  globs, and it is the route an agent uses too.
- **The picker** — `osascript -e 'choose file … with multiple selections
  allowed'` runs on the server side, where a path is a path, and returns exactly
  what was chosen. macOS only; elsewhere the endpoint returns 501 and names the
  CLI. Chosen over a browser file input because `<input type=file>` gives the
  page a `File` object with no path either.
- **The path field** — universal, no picker required, and the thing an agent or
  a copied path from Finder (⌥⌘C) lands in.
- **The drop target** — kept because Finder-drag is the gesture the director
  will try first. It reads `text/uri-list` / `text/plain` for a `file://` URL,
  which some browsers do supply; when nothing usable arrives it turns the hint
  under the button red and prints *“Your browser did not hand over that file's
  path — it is not allowed to. Use ‘Add media…’, paste the absolute path below,
  or run `cutroom add <project> <path>`.”* It is never a silent no-op, which is
  the failure mode the brief called out.

Dropping a file onto a **lane** does the same thing and then places the clip at
the drop point, so the gesture is complete when the path does arrive.

## The four boundaries: enforcement and proof

Every one is enforced at a single choke point, so there is no second path that
could drift out of agreement with it.

### 1. Never write outside `~/cutroom-projects/`

**Enforced by `server.writable(path, existing_ok=False)`.** Every write in the
program goes through it — project JSON, snapshots, derived outputs, renders,
thumbnails, the lock sidecar. It refuses a relative path outright, computes
`os.path.realpath(target)` and requires it to be the root or under it, and
(unless explicitly allowed) refuses a target that already exists. `mkdirs()`,
`free_name()` and `export_path()` are the only ways to name an output and all
three call it.

*Tests:* `test_nothing_can_be_written_outside_the_projects_root` (four spellings
of “outside”, plus a relative path, plus the inside case),
`test_an_existing_file_is_never_a_write_target`.

### 2. Never delete any file, including its own derived output

**Enforced by absence.** There is no `unlink`, `rmtree`, `os.remove`,
`os.rmdir`, `shutil.move`, `os.truncate` or `rename` anywhere in `server.py` or
`render.py`. ffmpeg is invoked with `-n`, never `-y`. Re-running a pass takes
the next free name (`x__pass-2.mp4`) rather than replacing anything. A failed
pass leaves its partial file where it is. A refused commit leaves its temp file
in place. Deleting a clip removes a timeline entry and nothing else.

There is exactly one in-place `replace()` in the program: the atomic swap of the
project JSON, which is a rename onto a name cutroom owns and which writes the
state being replaced into `.snapshots/` first — so even that loses nothing.

`render.render()` refuses an existing output *before* starting ffmpeg, and that
ordering is load-bearing: ffmpeg 8.1.1 answers `-n` on an existing file with
“File already exists. Exiting.” **and exit code 0** (measured). `-n` alone is a
guarantee whose failure is invisible.

*Tests:* `test_the_program_contains_no_way_to_delete_a_file` (greps both modules
with comments stripped, checks the open-modes are only `rb` and the lock's
`a+`, counts the single `replace()`), `test_a_failed_pass_leaves_its_own_
wreckage_and_deletes_nothing`, `test_running_the_same_pass_twice_writes_a_
second_file_not_over_the_first`, `test_removing_a_clip_removes_no_file_and_no_
media`, `test_the_renderer_refuses_to_overwrite_an_existing_output`,
`test_the_page_never_offers_to_delete_anything`.

### 3. Never follow a symlink out of the project directory

**Enforced by the same `writable()`**, because it compares **realpaths** rather
than doing a lexical check. If `~/cutroom-projects/<name>/derived` is a symlink
to somewhere else, `realpath(derived/x.mp4)` lands outside the root and the
write is refused before anything is opened. `mkdirs()` refuses it too, so the
directory is not even created. `/renders/<file>` additionally refuses a leaf
that is a symlink.

This is the accident, exactly: a symlink placed where a directory used to be.

*Tests:* `test_a_symlinked_project_subdirectory_cannot_smuggle_a_write_out`
(asserts the file in the symlink target is untouched and no new file appeared
there), `test_a_pass_through_a_symlinked_derived_directory_is_refused_not_
followed`.

### 4. Never accept a media path that is not already in `media`

**Enforced by `server.servable(project, path)`** — an exact-string membership
test over the project's own `media` list. Not a prefix check, not
resolve-and-compare-prefix. Requests address media by `mid`, so a path never
travels in a URL at all; the mid is resolved against the project and the path it
names still has to satisfy `servable()`. Two independent gates, and the
pass runner uses the same one.

*Tests:* `test_a_path_is_servable_only_if_it_is_in_the_media_list` — prefix
matches (`a.mp4.bak`, `a.mp4x`), traversal to the same real file
(`clips/../clips/a.mp4`), `//` and `./` respellings, a case variant (macOS is
case-insensitive and the file system would happily open it), `/etc/passwd`, a
relative path, and non-strings. Plus
`test_the_http_layer_refuses_an_encoded_path_dressed_up_as_a_mid`
(`..%2f..%2fetc%2fpasswd`, double-encoded, a fully-encoded absolute path, a mid
with `../` appended — all 404 while `/media/m01` is 200),
`test_media_that_was_never_added_cannot_be_reached_by_any_route` (a readable
file *beside* an allowed one), and
`test_a_pass_cannot_address_media_that_is_not_in_the_project`.

### And the rule underneath all four

Sources are opened `"rb"` and in no other mode. `open_source()` is the only
place in the program that opens a media file, and a pass never opens one at all
— the external tool gets the source as an input argument.
`test_rendering_does_not_touch_the_sources` asserts bytes, mtime **and** the
directory listing beside every source are identical after a render.

## Verification actually run

Against real footage at
`~/Documents/projects/animation-studio/films/01-threshold/clips/selected/`,
read-only throughout — `stat` of size and mtime taken before and after every
step and diffed, unchanged each time; the directory still holds its 41 files.

1. `cutroom new threshold`, then `cutroom add` with three real clips by
   absolute path (8.04s, 4.04s, 4.96s; 720×1280 24fps).
2. A cut with an overlap, written through `edit_project()`: `1.1` [0.0–2.0),
   `1.2` at t=1.5 trimmed 0.5–2.5 (**0.5s crossfade**), `1.3` at t=3.5 on
   **lane 1** (proving lanes do not gate the blend). Timeline predicts
   **5.0s = 120 frames**.
3. `cutroom export threshold` → `renders/threshold_v003.mp4`:
   **5.000000s, 120 frames counted**, 720×1280, 24/1, 10.24 Mbps. Prediction and
   file agree exactly.
4. Live server: `/`, `/project` (3 clips, 3 media, 0 offline), a 206 Range
   response, `/thumb/m02` 200, `/renders/…` 200, and four traversal spellings
   all 404.
5. A real post pass (`desat`, an ffmpeg `hue=s=0` script) on clip `c3`:
   wrote `derived/b01_13_arrival_seedance20__desat.mp4`, added it as `m04`,
   re-pointed the clip, left `m03` on the list and the original byte-identical.
6. Re-exported at v005: 5.000000s, 120 frames again.
7. Both suites: **77 tests, all green** (35 render, 42 server), run repeatedly.

## Residual concerns

- The macOS picker is `osascript`; on Linux the endpoint returns 501 and the
  page falls back to the path field and the CLI. Nothing is silently broken,
  but the picker is not portable.
- `flock` is advisory. A writer that ignores it can still lose an edit; that is
  a contract, stated in the module docstring, not a guarantee — unchanged from
  the reviewed version and unchangeable in principle.
- A pass that crashes leaves a partial file in `derived/`. That is the deliberate
  consequence of never deleting: the director removes it by hand, or ignores it.
- `duration()` is exact at `rate 1.0` and can be one frame out for a retimed
  clip. Unchanged, still honest, still tested with a `<= 1` tolerance.
