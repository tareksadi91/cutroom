# cutroom

A local timeline you drag in a browser and an agent edits through the same
file. Python 3 standard library plus ffmpeg. No dependencies, no build step.

**One rule above all others: cutroom never writes to, moves, or deletes your
media.** It reads. Everything below follows from that.

## Why it is standalone

The first version of this tool lived inside a film repository and shared its
git history. On 2026-08-25 a merge of that branch replaced three media
directories with symlinks the branch was carrying; git deleted the real
directories to place them, and 226 clips — 1.9 GB, every rendered shot of the
film — went with them. They were gitignored, so git had no copy, and the
recovery came from a cloud sync that was simultaneously trying to re-propagate
the deletion. A timeline tool has no business being able to do that, so this
one lives in its own repository with no shared history with any film, keeps its
projects in `~/cutroom-projects/`, never scans a directory, and cannot spell a
deletion.

## Start it

```sh
./cutroom new threshold                      # ~/cutroom-projects/threshold.json
./cutroom add threshold /abs/path/to/a.mp4 /abs/path/to/b.mp4
./cutroom serve threshold                    # http://127.0.0.1:8420, opens a browser
```

Other commands: `./cutroom export <project>`, `./cutroom ls`,
`./cutroom check` (both test suites).

`new` takes `--fps` and `--res 720x1280`. `serve` takes `--port`.

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

Every added file is recorded in the project's `media` list with its path, a
label, and its duration and size as ffprobe last reported them. That list is
also the security boundary: a path is readable by cutroom if and only if it is
on it.

## The model

A project is **one JSON file** at `~/cutroom-projects/<name>.json`.

```json
{
  "name": "threshold", "fps": 24, "resolution": [720, 1280], "version": 7,
  "media": [{"mid": "m01", "path": "/…/b01_11_street.mp4",
             "label": "1.1 street", "dur": 8.042, "w": 720, "h": 1280}],
  "clips": [{"uid": "c00", "mid": "m01", "lane": 0, "t": 0.0,
             "in": 0.0, "out": 1.5, "rate": 1.0, "label": "1.1", "note": ""}],
  "passes": {"desat": "/abs/path/to/desat.py"}
}
```

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

A pass is an external script named in the project's `"passes"` map and invoked
as `script src dst [args]`. It reads `src` and writes `dst` —
`~/cutroom-projects/<name>/derived/<stem>__<pass>.mp4` — which cutroom then
adds to `media` and points the clip at. Your original is opened read-only and
is not touched, moved or renamed, so there are no `_raw` backups to keep, no
rule about never overwriting one, and no checksum ledger to prove one is
complete. A pass that crashes halfway leaves a partial file in `derived/` and
nothing of yours is different. cutroom ships no passes.

## What it will never do

- **Scan, index, or watch any directory.** It has no `os.walk`, no `rglob`, no
  `scandir`. The only `glob` in the program lists its own snapshots.
- **Write to a path outside `~/cutroom-projects/`.** Every write goes through
  one guard that resolves the target and refuses anything that lands elsewhere.
- **Delete anything, anywhere, ever — including its own derived files.** There
  is no `unlink`, no `rmtree`, no `shutil.move`, no `rename` in the program,
  and ffmpeg is always invoked with `-n`. Running the same pass twice writes a
  second file rather than replacing the first. Deleting a clip removes the clip
  from the timeline and nothing from the disk.
- **Follow a symlink out of the project directory.** The write guard compares
  realpaths, so a `derived` symlinked somewhere else resolves outside the root
  and is refused before anything is opened. This is the exact shape of the
  accident that destroyed the footage.
- **Accept a media path that is not already in `media`.** The gate is an
  exact-string membership test against the project's own list — not a prefix
  check, not resolve-and-compare. A path that merely resembles an allowed one
  is a 404.

The one file cutroom ever replaces is the project JSON itself, by an atomic
rename onto a name it owns, and only after writing the state being replaced
into `.snapshots/`.

## Tests

```sh
./cutroom check          # or: python3 src/test_render.py && python3 src/test_server.py
```

Bare asserts, no pytest. Every test builds its own fixtures in a temporary
directory; none addresses real footage.
