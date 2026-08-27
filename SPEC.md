# cutroom — a standalone timeline

**One rule above all others: cutroom never writes to, moves, or deletes your media.**

It reads. That is the whole relationship. Everything below follows from it.

## Why it is standalone

The first version lived inside a film repo and shared its git history. On
2026-08-25 a merge of that branch replaced three media directories with symlinks
the branch was carrying, git deleted the directories to place them, and 226 clips
— 1.9 GB, every rendered shot of beats 1 through 7 — went with them. They were
gitignored, so git had no copy. Recovery came from a cloud sync that was also
actively fighting the restore.

A timeline tool has no business being able to do that. So:

- **Its own repository.** No shared history with any film, no branch of cutroom
  can ever be merged into a repo that holds footage.
- **No auto-indexing.** It never scans a folder, never walks a tree, never
  discovers anything. Media enters only when the director drags a file in.
- **Read-only on sources.** Sources are opened `"rb"` and nothing else, ever.

## The model

**A project is one JSON file** at `~/cutroom-projects/<name>.json`. It holds the
cut and a list of media references. Nothing else on disk belongs to cutroom
except its derived outputs and renders.

```json
{
  "name": "myfilm",
  "fps": 24,
  "resolution": [720, 1280],
  "version": 7,
  "media": [
    {"mid": "m01", "path": "/…/clips/street.mp4",
     "label": "1.1 street", "dur": 8.042, "w": 720, "h": 1280}
  ],
  "clips": [
    {"uid": "c00", "mid": "m01", "lane": 0, "t": 0.0,
     "in": 0.0, "out": 1.5, "rate": 1.0, "label": "1.1", "note": ""}
  ]
}
```

- **`media`** is the allowlist. A file is in it because the director dragged it in.
  A path not in `media` is not servable, not readable, not renderable. There is no
  other way for cutroom to learn a path exists.
- **`clips`** reference media by `mid`, never by path. Re-pointing a shot is a
  one-field edit and the timeline survives a file being moved.
- **A missing source is not an error.** The clip renders as OFFLINE on the timeline
  and blocks only export, naming what is missing. The project is never rewritten to
  "clean up" a missing file.

## Post passes write derivatives, never in place

A pass reads a source and writes a **new** file to
`~/cutroom-projects/<name>/derived/<stem>__<pass>.mp4`, then adds it to `media` and
re-points the clip. The original is never opened for writing.

This deletes an entire category of machinery the first version needed: no
`_raw` backups, no "never overwrite an existing backup" rule, no audit of whether a
backup is complete, no checksum ledger to prove one is. The guarantee is structural.
A pass that crashes halfway leaves a partial file in `derived/` and touches nothing
of yours.

Passes are external scripts, invoked as `script src dst [args]`. **The
executable comes from `--passes-dir`, given to the server at startup, and never
from project data** — the project names a *pass*, the name is resolved inside
that directory, and a name carrying a separator, a `..` or a leading dot is
refused. With no `--passes-dir` there are no passes at all. cutroom ships none.
Point it at the film's `studio/tools/` and it uses those.

`dst` already exists when the tool starts: cutroom creates it with
`O_CREAT|O_EXCL` to claim the name atomically before launching anything, so two
servers can never be handed the same free name. A pass therefore **overwrites**
its destination (an ffmpeg pass needs `-y`, not `-n`) — the one file it may.

A pass that finishes after the clip has been re-pointed by hand does **not**
silently take the clip back: the derivative is written and added to `media`,
and the answer is a conflict naming both.

## What it does

Drag files in. Arrange them in lanes. Trim. Cut a clip in two at the playhead.
Overlap for a crossfade. Play it in a program monitor. Export an mp4.

- **Lanes are a workspace, not layers.** Overlap in *time* is what blends, in one
  lane or across lanes. The renderer sorts by time and ignores lane.
- **Edit points are frames, not floats.** `t`, `in` and `out` snap to the frame
  grid on save. At `rate 1.0` the frame count is exact by construction; a retimed
  clip carries up to one frame through the fps resample, and says so.
- **The razor cuts on a frame and removes nothing.** `S` (or ✂) splits every
  clip the playhead is inside; the halves cover exactly the span the one
  covered, so the edit is reversible by dragging.
- **Media may be referenced or copied in.** `add --copy` writes the file to
  `<project>/media/` first and references the copy. This is not the safety
  property — a source is opened `"rb"` and there is no way to write to one —
  it is the *survival* property: the cut stops depending on the folder it came
  from. The copy is written through the same `O_CREAT|O_EXCL` descriptor that
  claimed its name, so it can never land on an existing file.
- **Autosave with a version guard.** Every change writes the project file and
  snapshots the prior state. A stale write is refused with a conflict, never
  last-writer-wins.
- **An agent edits through the API or `edit_project()`, never the file directly** —
  both take the same advisory lock. A writer that ignores the lock can still lose an
  edit; the code cannot enforce what it cannot see.

## What it will not do

- Scan, index, or watch any directory.
- Write to a path outside `~/cutroom-projects/`.
- **Delete or overwrite media or a derived output.** Every file cutroom creates
  is created with `O_CREAT|O_EXCL`, so a name already in use is refused rather
  than replaced, and there is no `unlink`, `rmtree` or `shutil.move` in the
  program at all. Running the same pass twice writes a second file.
- Follow a symlink out of its project directory — on the write side *or* the
  read side, and by resolving the whole path rather than only its last
  component.
- Accept a media path that is not already in the project's `media` list.

It **does** update one file in place: its own project JSON, by writing a temp
file and renaming it over the old one, after the state being replaced has gone
into `.snapshots/`. That rename destroys the previous destination inode — which
is what makes “deletes nothing, ever” the wrong sentence and this the right
one: *cutroom never deletes or overwrites media or derived outputs, and updates
its own project file atomically via replace-after-snapshot.*

## Layout

```
src/render.py    project -> one ffmpeg graph -> mp4
src/server.py    stdlib http.server: project API, Range, passes, export
src/ui.html      the timeline
src/test_*.py    bare-assert suites, no pytest
~/cutroom-projects/<name>.json          the cut
~/cutroom-projects/<name>/derived/      pass outputs
~/cutroom-projects/<name>/renders/      exports
~/cutroom-projects/<name>/.snapshots/   autosave history
```

Python 3 stdlib, ffmpeg, a browser. No dependencies, no build step.
