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
  the mutation-tested render tests. 35 render tests, 42 server tests — 52
  server tests after the second review round below.

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
| `TOOLS` (a hard-coded list of one film's scripts) | Passes come from the project's own `"passes"` map now. ⚠️ **Superseded:** that map was itself a P1 — it let a PUT name `/bin/rm` — and passes now come from `--passes-dir` at startup. |
| `snap_cut()` / `--snap` | A one-off migration for a file written before the grid rule existed. No such file exists in a new tool. |
| numpy and PIL in the tests | "Stdlib only" should be true of the tests too. Frames are now read as raw `rgb24` bytes out of ffmpeg and measured with `sum()` and `max()`. |

## Drag-and-drop: what was chosen, and why

**Chosen: a CLI `cutroom add <project> <path>…`, plus an “Add media…” button
that opens the OS file picker server-side.**

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
The page deliberately has no path field or file-drop target. Agents use the CLI;
directors use the picker. This keeps the interface from advertising browser
behaviour that cannot reliably supply an absolute path.

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

> ⚠️ **SUPERSEDED — this heading was false as written; see "P1-7" below.** The
> project file's `tmp.replace(path)` destroys the previous destination, so the
> rule is now stated as *never deletes or overwrites media or derived outputs,
> and updates its own project file atomically via replace-after-snapshot*. The
> `-n` sentence below is also out of date: the destination is claimed with
> `O_CREAT|O_EXCL` before ffmpeg starts, which is strictly stronger, and `-y`
> then overwrites nothing but that empty claim.

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
a real film's `clips/selected/` directory,
read-only throughout — `stat` of size and mtime taken before and after every
step and diffed, unchanged each time; the directory still holds its 41 files.

1. `cutroom new myfilm`, then `cutroom add` with three real clips by
   absolute path (8.04s, 4.04s, 4.96s; 720×1280 24fps).
2. A cut with an overlap, written through `edit_project()`: `1.1` [0.0–2.0),
   `1.2` at t=1.5 trimmed 0.5–2.5 (**0.5s crossfade**), `1.3` at t=3.5 on
   **lane 1** (proving lanes do not gate the blend). Timeline predicts
   **5.0s = 120 frames**.
3. `cutroom export myfilm` → `renders/myfilm_v003.mp4`:
   **5.000000s, 120 frames counted**, 720×1280, 24/1, 10.24 Mbps. Prediction and
   file agree exactly.
4. Live server: `/`, `/project` (3 clips, 3 media, 0 offline), a 206 Range
   response, `/thumb/m02` 200, `/renders/…` 200, and four traversal spellings
   all 404.
5. A real post pass (`desat`, an ffmpeg `hue=s=0` script) on clip `c3`:
   wrote `derived/arrival__desat.mp4`, added it as `m04`,
   re-pointed the clip, left `m03` on the list and the original byte-identical.
6. Re-exported at v005: 5.000000s, 120 frames again.
7. Both suites: **77 tests, all green** (35 render, 42 server), run repeatedly.

## Residual concerns

- The macOS picker is `osascript`; on Linux the endpoint returns 501 and the
  page points at the CLI. The picker is not portable.
- `flock` is advisory. A writer that ignores it can still lose an edit; that is
  a contract, stated in the module docstring, not a guarantee — unchanged from
  the reviewed version and unchangeable in principle.
- A pass that crashes leaves a partial file in `derived/`. That is the deliberate
  consequence of never deleting: the director removes it by hand, or ignores it.
- `duration()` is exact at `rate 1.0` and can be one frame out for a retimed
  clip. Unchanged, still honest, still tested with a `<= 1` tolerance.

## After the first commit: two things a real browser found

Verified by loading the page headlessly (gstack `browse`) against the real
project, which caught two things no test would have:

1. **A `<video>` aborting a Range request printed a stack trace per seek.**
   `BrokenPipeError` / `ConnectionResetError` out of `wfile.write` is *normal*
   when a video element decides it has read enough and drops the socket, and the
   terminal filled with tracebacks during ordinary scrubbing — which is how a
   real failure gets missed. `_file()` now swallows exactly those two, and
   nothing else.
2. **The opening zoom was tuned for a 92-second film.** At 10 px/s a five-second
   cut is fifty pixels of timeline. Two wider steps were added to `ZOOMS`
   (64 and 120 px/s, the original four untouched) and `fitZoom()` picks the
   widest step that fits the stage — on load only, so the director's own zoom
   is never overridden. The ruler shows single seconds at those two new steps
   and keeps its five-second marks everywhere the design was drawn at.

The OFFLINE state was checked the same way, by moving a source out from under a
saved cut: the clip stays where it is, hatched red and labelled OFFLINE, the
media row reads "file not found", the header says export is blocked, the
inspector names the missing path — and the project file is byte-identical
afterwards.

---

# The adversarial review, round two: the remaining seven, and what each cost

Codex reviewed the standalone rewrite adversarially and found seven boundary
escapes. Two were closed in `91facbb` (the passes-directory fix and the
allowlist-forging PUT). This is the other five, the two P2s, and the test suite
they broke.

**Every fix below was written against a test that failed first.** The evidence
is not "the test passes now" — it is that the same test, run against a copy of
the tree with that one fix reverted, fails, and fails *for the reason the
finding names*. Both suites are green: **35 render, 52 server, 87 total.**

## P1-3 — a symlinked PARENT served /etc/passwd

`/renders/<leaf>` checked `path.is_symlink()`, which asks about the last
component only, and `path.is_file()`, which follows a symlinked parent without
comment. Replace `<project>/renders` with a symlink and request a file through
it: the final path is not itself a symlink, the leaf check passes, the file is
served.

**Changed.** `_inside_project(name, path)` resolves the WHOLE path and requires
the result to sit inside the project directory. `/renders/` and `/thumb/` both
go through it. Media deliberately does not — media lives wherever the
director's footage lives and its gate is `servable()`, an exact-string
membership test.

*Failing first:* `test_a_symlinked_renders_directory_cannot_serve_a_file_from_outside`
against the leaf-only check → `AssertionError: [200, 404]`. The 200 is the file
from outside the project being served.

## P1-4 — `writable()` was check-then-use

It resolved a path and returned it; the open or the ffmpeg run happened later.
Swap the checked directory for an outward symlink in between and the write
follows it.

**Changed.** `writable()` is now documented as a CHECK, not an enforcement, and
the enforcement is `_open_new(path)`: it re-runs `writable()` and opens the
final component `O_CREAT|O_EXCL|O_NOFOLLOW` in the same breath. `write_new()`
writes through that descriptor — a path is never re-opened by name to be
written. `claim()` is the same open, closed immediately, for reserving a
destination a SUBPROCESS will write.

**The residual, stated plainly and in the docstrings.** Two windows remain, and
neither is closable from here:

1. **The parent chain.** `O_NOFOLLOW` covers the LAST component only. An
   attacker who can swap a PARENT directory for a symlink in the microseconds
   between `realpath()` and `open()` still wins. Closing that needs an
   `openat()` walk of every component, which the Python standard library does
   not expose (`os.open` has no `dir_fd` resolution loop and macOS has no
   `O_PATH`). Bounded by: this is a single-user directory under `$HOME`, so the
   race needs an attacker who already has the account.
2. **ffmpeg and external passes do their own open.** cutroom creates the
   destination first with `O_CREAT|O_EXCL|O_NOFOLLOW` on a fresh unique name,
   then hands over that path — so the only thing either program can write over
   is cutroom's own zero-byte claim, and O_EXCL proved the name was free at
   claim time. But between that create and the subprocess's open, the file
   could be replaced by a symlink and the subprocess would follow it. The write
   is in another program; there is no flag here that reaches it.

Both are written into `writable()`, `_open_new()`, `claim()` and
`render.claim_output()` as ⚠️ RESIDUAL paragraphs rather than smoothed over. A
boundary you can only mostly enforce must not be written down as one you can —
that is the same mistake as P1-7, one layer down.

*Failing first:* `test_writable_is_re_verified_at_the_moment_of_the_write`
against an `_open_new` that trusts the earlier check →
`AssertionError: the write followed the swapped directory out`.

## P1-5 — a finishing pass silently ate a concurrent edit

`run_pass()` read the clip, ran a subprocess for minutes, then re-pointed the
clip at the derivative — without checking it still named the media the pass had
been started against. Start a pass on `c1→m01`, save `c1→m02` while it runs,
and the pass silently re-points to its own output. The edit is gone and the
answer is 200.

**Changed.** The mid is captured before the tool starts and re-checked inside
`mutate()`, under both locks, at completion. The derivative is written and
added to `media` either way — the work is never thrown away — but the re-point
happens only if the clip still names the original mid. Otherwise: **409**,
naming `was`, `now`, the new mid and the path, with a message that says the
clip was NOT re-pointed. A clip deleted while the pass ran is the same answer
with `now: null`.

The page was wrong about this too and is fixed with it: a 409 from `/pass` now
adopts the returned project and reports *"pass done, clip NOT re-pointed"*.
Calling it "failed" and dropping the response would hide a file that exists.

*Failing first:* `test_a_pass_that_finishes_after_the_clip_moved_does_not_take_it_back`
against the unconditional re-point → status 200 with `clips[0].mid == 'm03'`,
i.e. the director's `m02` save overwritten in silence.

## P1-6 — two servers could collide on one derivative

The job lock is a process-local `threading.Lock`. It serialises one server's
threads and knows nothing about a second server on another port. Both call
`free_name()`, both are told `x__desat.mp4`, and the second ffmpeg truncates
the first's output. Looking is not taking.

**Changed.** `claim_free(directory, stem, suffix)` walks the same name sequence
but CREATES each candidate with `O_CREAT|O_EXCL`; the loser of a race gets
`FileExistsError` and moves to the next name. It is used for derived outputs,
renders and the CLI's output. `free_name()` survives for the "what would the
name be" question and now says in its docstring that it only looks.

*Failing first:* `test_two_processes_cannot_claim_the_same_derivative_name`
against `free_name` + create → both calls return the same path. The test also
asserts the bug directly (`free_name()` twice gives one answer) and then races
four real subprocesses for four distinct files.

## P1-7 — the "never deletes any file" claim was false as written

Every save ends in `tmp.replace(path)`, and a rename onto an existing name
destroys the previous destination. The BEHAVIOUR is right — it is the
atomic-write pattern and the state being replaced goes into `.snapshots/`
first. The CLAIM was wrong, and a boundary written wider than the code can hold
is worse than no boundary, because it is the sentence somebody trusts.

**Changed everywhere it appears** — `SPEC.md`, `README.md`, the `server.py`
module docstring, `_commit()`, and the test's own docstring — to what is true
and enforceable:

> cutroom never deletes or overwrites media or derived outputs, and updates its
> own project file atomically via replace-after-snapshot.

And the real rule is now enforced rather than asserted: a write into `derived/`
or `renders/` refuses when the destination exists, because every create is
`O_EXCL`. That also replaced `ffmpeg -n` as the mechanism —
`render.claim_output()` explains why, and it is the stronger reason of the two:
ffmpeg 8.1.1 answers `-n` on an existing file with "File already exists.
Exiting." **and exit code 0**, so `-n` alone is a guarantee whose failure is
invisible. `-y` is now correct precisely because the destination is a zero-byte
file this program created one syscall earlier.

*Failing first:* `test_the_program_contains_no_way_to_delete_a_file` — which now
also greps SPEC.md and README.md — against the old SPEC wording →
`AssertionError: the un-keepable version of boundary 2 is back`.

**A consequence to know about:** a pass's destination now exists when the tool
starts, so an external pass must OVERWRITE it (`ffmpeg -y`, not `-n`). That is
the price of claiming the name atomically, and it is documented in SPEC.md,
README.md, `run_pass()`'s docstring and the page's tooltip.

## P2-8 — `Range: bytes=` was a full-file 206

`bytes=` and `bytes=-` parsed into `start=0, end=EOF` and were answered 206
with the entire file: partial content for a request that named no range.

**Changed.** An empty byte-range set, and a spec with no `-` at all, are
malformed → 416 with `Content-Range: bytes */<size>`. Suffix ranges and normal
ranges are untouched.

*Failing first:* `test_a_range_that_names_nothing_is_malformed_not_the_whole_file`
→ `AssertionError: ('bytes=', 206, 21960)` — 21960 being the whole file.

## P2-9 — a malformed body was a KeyError, not a 400

`{"clips": [{}]}` reached `render.snap_project()`, which indexes `c["t"]`. The
handler catches only `Refused`, so a malformed request became a dropped
connection.

**Changed.** `render.shape_problems(project)` answers "is this a project at
all" — types and presence of `fps`, `resolution`, `media[].mid/path`,
`clips[].uid/mid/t/in/out/rate` — and `_guarded_write()` runs it FIRST, before
`snap_project()` or `validate()` touch a field, returning 400. Both doors (PUT
and `edit_project`) share it, because both go through `_guarded_write`.

*Failing first:* `test_a_malformed_project_body_is_a_400_not_a_crash` →
`http.client.RemoteDisconnected: Remote end closed connection without response`.

## The renderer's own CLI (P1-2's family)

Codex named it as missing coverage and it was also a live hole: `render.py -o`
was handed straight to ffmpeg with no `writable()` guard at all — a second way
to name an output, which is exactly what boundary 1 claims does not exist.

**Changed.** `render.main()` imports `server` lazily (a top-level import would
be a cycle — `server` imports `render`) and puts `-o` through `mkdirs()` +
`claim()`, so it must land inside `~/cutroom-projects/` and on a free name.
With no `-o` it claims a free name in the project's own `renders/`. `render()`
gained `claimed=False`; when False it claims the output itself, which is what
keeps the library, the CLI and the tests under one rule.

*Failing first:* `test_the_renderer_cli_cannot_write_outside_the_projects_root`
against the unguarded `-o` → it rendered a file into an arbitrary directory and
printed `…/new.mp4  0.31 Mbps`.

## The two tests that matter most

**1. A hostile pass name cannot execute** —
`test_a_hostile_pass_name_cannot_execute`. Twelve spellings (`/bin/rm`,
`../../bin/rm`, `../rm`, `rm`, a name carried in a hand-written `"passes"` map
still in the project file, a symlink inside the passes directory pointing at
`/bin/rm`, `./negate.py`, `..`, `.hidden`, `negate.py/../../../../bin/rm`, the
empty string, `sub/negate.py`), each with a canary file passed as an argument,
asserted through both `run_pass()` and `POST /pass`, plus the bare-name case
with no `--passes-dir` (501). It also asserts a real pass in the directory DOES
run, so the gate is being measured and not a dead path.

Against the pre-fix code this test does not merely fail — it *executes*
`/bin/rm` with the source path as its first argument. The harness output was
`('rm', 500, {'problems': ['rm: -rf: No such file or directory']})`: that error
is rm reporting on its fourth argument, having already reached the first three.
This is the finding, reproduced. (It ran inside a temporary copy; no real media
was ever addressed.)

**2. A PUT cannot forge the allowlist** —
`test_a_put_cannot_add_media_and_the_forged_path_stays_unservable`. The proof is
not that the write is rejected but that the forged path is still 404 afterwards:
the cut saves (200), `media` is byte-identical to what it was, `passes` is
absent, and `/media/leak`, `/media/m99` and `/thumb/leak` are all 404 while
`/media/m01` still serves the whole file. Failing first: the stored allowlist
came back holding `/etc/passwd`.

## The rest of the new coverage

- `test_what_an_external_pass_script_actually_receives` — the contract from the
  TOOL's side, which is the part cutroom does not control: `argv` is exactly
  `[script, src, dst, *args]`, the cwd is the project's own `work/`, `dst`
  exists and is empty (the claim), `src` is byte- and mtime-identical
  afterwards, and an argument that would be a shell redirect is passed through
  as an argument and creates no file. Failing first: `dst` did not exist when
  the tool started.
- `test_a_symlinked_renders_directory…` also covers a symlink INSIDE `renders/`
  pointing out, and asserts a real render still serves 200.
- Both suites gained a substring argument (`python3 src/test_server.py <name>`)
  so a single test can be run against a patched copy of the module it fixes.
  That is how every "failing first" above was produced.

## The test suite my two earlier fixes had broken

`test_server.py` seeded passes as `{"passes": {"name": "/abs/path"}}` in project
data — the field that no longer exists and whose existence was the P1. Every
such test now builds a real passes directory through a new
`passes_dir(root, {name: source})` context manager, points `server.PASSES_DIR`
at it (resolved, because an unresolved `/tmp` would fail every containment
check), and refers to the pass BY NAME. Shared tool sources are module
constants (`NEGATE`, `COPY`). Also updated: the glob count (three now — the
project's snapshots, the root's project files, the operator's passes
directory), `BLANK` no longer carrying a `passes` key, `/project` listing
passes from `--passes-dir` rather than from the document, and the delete-grep
test rewritten to the narrower true rule.

## Residual concerns, this round

- **The `writable()` residual above** — the parent-chain race and the
  subprocess's own open. Named in four docstrings; not closable in stdlib.
- **`restore()` cannot restore an old `media` list.** It goes through
  `write_project()`, which forces `media` to the current value — correct for
  the allowlist rule (media may only enter through `add_media`) and it means a
  snapshot restore brings back the CUT, not the media list. Since media only
  ever grows, the safe direction is the one that happens; worth knowing before
  someone reports it as a bug.
- **A failed pass or a failed ffmpeg now leaves a zero-byte file** where it
  used to leave nothing, because the name is claimed before the subprocess
  runs. `export()` therefore validates the cut BEFORE claiming, so an
  unrenderable timeline does not litter `renders/`. `thumb()` treats an empty
  claim as "no thumbnail" rather than serving a broken image. cutroom cannot
  tidy either up; that is the deliberate consequence of never deleting.
- **`PASSES_DIR` is trusted once it is given.** Anything in that directory can
  be run. That is the operator's decision, made on the command line, and the
  whole point of the redesign is that it can no longer be made by a document.
