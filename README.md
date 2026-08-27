# cutroom

A local timeline you drag in a browser and an agent edits through the same
file. Python 3 standard library plus ffmpeg. No dependencies, no build step.

**One rule above all others: cutroom never writes to, moves, or deletes your
media.** It reads. Everything below follows from that.

## Requirements

Python 3 and ffmpeg. That is the whole list — there is nothing to install, no
package to add, no build step, no lockfile.

```sh
python3 --version     # developed on 3.12; no version-specific syntax is used
ffmpeg -version       # ffmpeg and ffprobe must both be on PATH
git clone <this repo> cutroom && cd cutroom && ./cutroom check
```

macOS and Linux. The **Add media…** button uses the operating system's own file
picker and is macOS-only; every other route works everywhere.

## Why it is standalone

The first version of this tool lived inside a film repository and shared its
git history. On 2026-08-25 a merge of that branch replaced three media
directories with symlinks the branch was carrying; git deleted the real
directories to place them, and 226 clips — 1.9 GB, every rendered shot of the
film — went with them. They were gitignored, so git had no copy, and the
recovery came from a cloud sync that was simultaneously trying to re-propagate
the deletion. A timeline tool has no business being able to do that, so this
one lives in its own repository with no shared history with any film, keeps its
projects in `~/cutroom-projects/`, never scans a directory, and contains no
deletion primitive at all — no `unlink`, no `rmtree`, no `shutil.move`.

## Start it

```sh
./cutroom new myfilm                      # ~/cutroom-projects/myfilm.json
./cutroom add myfilm /abs/path/to/a.mp4 /abs/path/to/b.mp4
./cutroom add myfilm --copy /abs/path/*.mp4   # import copies, not references
./cutroom serve myfilm                    # http://127.0.0.1:8420, opens a browser
```

## Editing

**Drag from the media panel onto the timeline** and a marker shows the seam it
will land on, lighting the clip on either side. Drop, and it is *inserted*
there: everything from that instant onward moves right by exactly the new
clip's length. Nothing lands on top of anything, and no gap is opened.

The ripple preserves the *relative* shape of the cut downstream: deliberate gaps
and deliberate crossfades are carried along, never rewritten. **Hold alt** while
dropping to place the clip exactly where the cursor is instead, with no ripple
and no snapping.

**A clip already on the timeline drags freely** — no seam, no ripple. That is
deliberate. Dragging one clip onto another is how you make a crossfade, and it
is the common act; snapping every drag to the nearest seam fought the primary
gesture in order to serve the rarer one. A clip arriving from the media panel is
different: it has no position to respect, and landing it on top of the cut was
never what anyone meant.

An edit that would produce a cut the renderer cannot express — a clip nested
inside another, a crossfade longer than the shorter clip it joins, two clips
starting on the same frame — is **refused before anything moves**, and the
marker turns amber during the drag so you find out while the clip is still in
your hand. The check mirrors the renderer's own rules expression for
expression; a differential test agrees with it on four thousand random
timelines.

**Retime** a clip with the `rate` field in the inspector: below 1.0 is slower
and occupies more timeline, above 1.0 is faster. Frames are duplicated or
dropped, never interpolated, so the *pattern* matters more than the amount —
rates of the form n/(n+1) (0.5, 0.667, 0.75, 0.8) hold frames evenly, and
anything else beats irregularly. The monitor plays the retime truthfully, so
what you see is what renders.

## Cut a clip in two

Park the playhead and press **S**, or hit **✂ cut** in the header. Every clip
the playhead is inside splits at the frame under it — the two halves cover
exactly what the one covered, so nothing is thrown away and the cut is undone
by dragging the right half back over the seam. The right half is selected, so
`⌫` after `S` trims the tail. The seam lands on a *frame*, not on the
quarter-second grid a drag snaps to.

Other commands: `./cutroom export <project>`, `./cutroom ls`,
`./cutroom check` (both test suites).

`new` takes `--fps` and `--res 720x1280`. `serve` takes `--port` and
`--passes-dir` (see **Post passes** — without it, passes are off).

## Add media

Media enters **only** when you hand cutroom a path. There are three ways, and
they are the same way:

1. **`./cutroom add <project> <path>…`** — the reliable one. Shell globs work,
   because the shell expands them before cutroom sees anything.
2. **“Add media…” in the page** — opens the operating system's own file picker,
   server-side, and adds whatever you choose. macOS only.
3. **Paste an absolute path** into the field under that button.

There is also a real drop target on the media panel. Drag a file onto it and,
*if your browser hands over the path*, it is added. Most will not: a browser is
not allowed to tell a page where a dropped file lives on disk, and cutroom may
not go looking for it by name, because looking means scanning a directory —
the exact habit that cost 226 clips. So when the path is withheld the drop
target **says so** and points you at the other two routes. It never fails
silently.

### Reference, or copy

By default the project *references* the file where it lies. With `--copy` — and
from the page, with **copy the file into this project**, which is on by default
— cutroom first copies it into `<project>/media/` and references the copy.

The copy is not what keeps your original safe. Nothing here can write to a
source: it is opened `"rb"` and there is no `unlink`, `rename` or `move`
anywhere in the program, which a test enforces by reading the source. What the
copy buys is **survival** — the cut stops depending on the folder it came from,
so a source folder that is moved, re-organised or emptied by a merge leaves the
cut room still holding everything it needs to render. Forty-five 720x1280 clips
come to roughly 350 MB.

Every added file is recorded in the project's `media` list with its path, a
label, and its duration and size as ffprobe last reported them. That list is
also the security boundary: a path is readable by cutroom if and only if it is
on it.

## The model

A project is **one JSON file** at `~/cutroom-projects/<name>.json`.

```json
{
  "name": "myfilm", "fps": 24, "resolution": [720, 1280], "version": 7,
  "media": [{"mid": "m01", "path": "/…/street.mp4",
             "label": "1.1 street", "dur": 8.042, "w": 720, "h": 1280}],
  "clips": [{"uid": "c00", "mid": "m01", "lane": 0, "t": 0.0,
             "in": 0.0, "out": 1.5, "rate": 1.0, "label": "1.1", "note": ""}]
}
```

There is **no `passes` key**, and a `PUT` cannot add one — nor add to `media`.
Anything that names an executable comes from the command line that started the
server, never from a document a client can write.

- **Clips reference media by `mid`, never by path.** Re-pointing a shot is a
  one-field edit and the timeline survives a file being moved.
- **A missing source is not an error.** The clip draws OFFLINE, keeps its trim
  and its position, and blocks only export — naming what is missing. The
  project is never rewritten to “clean up” a file that went away.
- **Lanes are a workspace, not layers.** Overlap in *time* is what blends, in
  one lane or across lanes. The renderer sorts by time and ignores lane.
- **Edit points are frames, not floats.** `t`, `in` and `out` snap to the frame
  grid on save. At `rate 1.0` the frame count is exact by construction; a
  retimed clip can carry up to one frame through the fps resample.
- **Autosave with a version guard.** Every change writes the project file and
  snapshots both the state it replaced and the state it landed. A stale write
  is refused with a conflict, never last-writer-wins.
- **An agent edits through `PUT /project` or `edit_project()`, never the file
  directly** — both take the same advisory lock.

Everything else cutroom owns lives beside the project file:

```
~/cutroom-projects/<name>.json          the cut
~/cutroom-projects/<name>/derived/      post-pass outputs
~/cutroom-projects/<name>/renders/      exports
~/cutroom-projects/<name>/.snapshots/   autosave history
~/cutroom-projects/<name>/thumbs/       poster frames
~/cutroom-projects/<name>/work/         a scratch cwd for passes
```

## Post passes

```sh
./cutroom serve myfilm --passes-dir ~/my-video-tools
```

A pass is an external script **in the directory given at startup**, invoked as
`script src dst [args]`. The project names a pass by name only; the name is
resolved inside that directory and must still land inside it, so a separator, a
`..`, a leading dot, an absolute path or a symlink pointing at `/bin/rm` are
all refused. **Without `--passes-dir` there are no passes at all** — the safe
default, because anything that can run an executable can delete a file.

It reads `src` and writes `dst` —
`~/cutroom-projects/<name>/derived/<stem>__<pass>.mp4` — which cutroom then
adds to `media` and points the clip at. `dst` already exists when your script
starts: cutroom creates it empty with `O_CREAT|O_EXCL` to claim the name before
launching anything, so **your pass must overwrite it** (`ffmpeg -y`, not `-n`).

Your original is opened read-only and is not touched, moved or renamed, so
there are no `_raw` backups to keep, no rule about never overwriting one, and
no checksum ledger to prove one is complete. A pass that crashes halfway leaves
a partial file in `derived/` and nothing of yours is different. If you
re-pointed the clip while the pass was running, the derivative is still written
and still added to `media`, and you get a conflict naming both rather than a
silent re-point over your edit. cutroom ships no passes.

## What it will never do

- **Scan, index, or watch any directory.** It has no `os.walk`, no `rglob`, no
  `scandir`. The three `glob`s in the program list its own snapshots, its own
  project files, and the `--passes-dir` you pointed it at. None of them can
  reach media: media enters only when you hand over a path.
- **Write to a path outside `~/cutroom-projects/`.** Every write goes through
  one guard that resolves the target and refuses anything that lands elsewhere.
- **Delete or overwrite media or a derived output.** There is no `unlink`, no
  `rmtree`, no `shutil.move`, no `rename` in the program, and every file it
  creates is created with `O_CREAT|O_EXCL` — a name already in use is refused,
  not replaced, and ffmpeg is only ever pointed at a zero-byte file cutroom
  claimed one syscall earlier. Running the same pass twice writes a second file
  rather than replacing the first. Deleting a clip removes the clip from the
  timeline and nothing from the disk.
- **Follow a symlink out of the project directory.** The write guard compares
  realpaths, so a `derived` symlinked somewhere else resolves outside the root
  and is refused before anything is opened; a served render is resolved the
  same way, whole, because checking only the last component follows a symlinked
  parent straight out. This is the exact shape of the accident that destroyed
  the footage.
- **Accept a media path that is not already in `media`.** The gate is an
  exact-string membership test against the project's own list — not a prefix
  check, not resolve-and-compare. A path that merely resembles an allowed one
  is a 404.

The one file cutroom ever replaces is the project JSON itself, by an atomic
rename onto a name it owns, and only after writing the state being replaced
into `.snapshots/`. That rename does destroy what was at the destination — it
is the atomic-write pattern, and it is why the guarantee above is written as
*never deletes or overwrites media or derived outputs, and updates its own
project file atomically via replace-after-snapshot* rather than as a “deletes
nothing, ever” that the rename would make untrue.

## Tests

```sh
./cutroom check          # or: python3 src/test_render.py && python3 src/test_server.py
```

Bare asserts, no pytest. Every test builds its own fixtures in a temporary
directory; none addresses real footage.

## License

[GNU Affero General Public License v3.0](LICENSE).

cutroom is a server you point a browser at, so the fork that matters is a hosted
one. Plain GPL would not reach it: nobody running a service *distributes* the
program, so nobody would owe anyone their changes. AGPL closes that — if you run
a modified cutroom and let other people use it over a network, they are entitled
to your source.

Use it, change it, run it, cut your film with it. If you hand a modified version
to anyone, by copy or over a wire, hand them the source too.
