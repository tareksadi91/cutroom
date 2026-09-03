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
"""The cut room: a local timeline the director drags and the agent edits.

    ./cutroom serve myfilm          # serve + open the browser
    ./cutroom add myfilm /abs/path/to/a.mp4 /abs/path/to/b.mp4
    ./cutroom new myfilm

A project is ONE JSON file at ~/cutroom-projects/<name>.json. This server is a
thin shell around it — every rule about what a valid cut is lives in render.py,
so a drag and a hand-edited JSON hit exactly the same checks.

THE WRITING CONTRACT — read this before editing a cut from an agent:

    An agent edits the cut through PUT /project or through edit_project(),
    NEVER by writing <name>.json directly.

Both take an advisory flock on <name>/.lock and hold it across the whole
read-check-write, so the browser and an agent serialise against each other and
neither can land on top of an edit it never saw. A plain `path.write_text()`
from anywhere takes no lock, and there is no compare-then-swap that can defend
against it: the compare and the rename are two syscalls, and an outside writer
can always land between them. Nothing here can enforce that contract — flock is
advisory, so a writer that ignores it still wins the race and the edit it
overwrites is gone. This is a contract, not a guarantee, and it is written down
because that is the only thing that makes it hold.

THE FOUR BOUNDARIES, and where each one lives:

  1. Never write outside ~/cutroom-projects/  — every write goes through
     writable(), which realpath()s the target and refuses anything that does
     not land inside the root, and then through _open_new(), which re-runs that
     check and opens the final component O_CREAT|O_EXCL|O_NOFOLLOW. There is no
     other way to name an output.
  2. Never delete or overwrite media or a derived output — there is no unlink,
     no rmtree, no shutil.move in this program, and every file cutroom creates
     is created with O_EXCL, so a name already in use is refused rather than
     replaced. The one file cutroom does replace is its OWN project JSON, by an
     atomic rename onto a name it owns, after the state being replaced has been
     written into .snapshots/. That is the atomic-write pattern, and it is
     stated here rather than hidden behind a "never deletes anything" that the
     rename makes untrue. test_server.py greps this file to keep it that way.
  3. Never follow a symlink out of the project directory — writable() compares
     the REALPATH, so a `derived` symlinked at /tmp resolves outside the root
     and is refused before anything is opened. On the READ side, the same rule:
     a served render is resolved WHOLE and must land inside the project
     directory, because checking only the last component follows a symlinked
     parent straight out (a `renders` -> /etc symlink served /etc/passwd).
  4. Never accept a media path that is not already in `media` — servable() is
     the only gate, and it is an exact-string membership test against the
     project's own list. Not a prefix check, not a resolve-and-compare.

Media is opened "rb" and in no other mode, ever. A post pass hands the source
to an external tool as its INPUT argument and the tool's output lands on a
fresh name under derived/.

Binds 127.0.0.1. Never 0.0.0.0.
"""
import argparse
import contextlib
import datetime
import fcntl
import http.server
import json
import math
import mimetypes
import os
import pathlib
import re
import secrets
import subprocess
import sys
import threading
import urllib.parse
import uuid
import webbrowser

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import render as render_mod

HERE = pathlib.Path(__file__).resolve().parent

# The one directory cutroom may write to. A module global rather than a
# constant so the tests can point it at a temporary directory — the guard below
# reads it on every call, so there is no window where a stale root is in force.
ROOT = pathlib.Path.home() / "cutroom-projects"

NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class Refused(Exception):
    """A boundary said no. Always answered, never swallowed."""


def root():
    return pathlib.Path(ROOT)


def check_name(name):
    """A project name is a bare filename, not a path.

    This is the first of the two things standing between a URL and the disk;
    writable() is the second. Neither trusts the other.
    """
    if not isinstance(name, str) or not NAME_OK.match(name) or ".." in name:
        raise Refused(f"{name!r} is not a project name — letters, digits, . _ - only")
    return name


def project_path(name):
    return root() / f"{check_name(name)}.json"


def project_dir(name):
    return root() / check_name(name)


# ------------------------------------------------------------ boundary 1 and 3
def writable(path, existing_ok=False):
    """Return `path` if cutroom may write it, else raise Refused.

    EVERY write in this program goes through here — the project file, a
    snapshot, a derived pass output, a render, a thumbnail. There is no second
    way to name an output, which is what makes "never writes outside
    ~/cutroom-projects/" a property of the code rather than a habit.

    It compares REALPATHS, so it is also the symlink rule: if `derived` is a
    symlink to /tmp, then realpath(derived/x.mp4) is /tmp/x.mp4, which is not
    inside the root, and the write is refused before anything is opened. The
    resolution happens on the ancestors that exist, so a not-yet-created file
    inside a real directory still passes.

    And unless `existing_ok`, the target must not exist at all — cutroom
    creates files, it does not replace them. The single exception is the
    project JSON itself (see _commit), whose previous state is snapshotted
    before the swap, so nothing is lost even there.

    ⚠️ THIS IS A CHECK, NOT AN ENFORCEMENT. It returns a path, and whatever
    opens that path does so LATER — which is a check-then-use window: swap a
    checked directory for a symlink pointing outward in between and the write
    follows it. Nothing that calls writable() may treat its answer as a
    permission that survives; the enforcement is _open_new() below, which
    re-runs this check and then opens the file in the same breath.
    """
    path = pathlib.Path(path)
    if not path.is_absolute():
        raise Refused(f"{path} is not an absolute path")
    base = os.path.realpath(root())
    real = os.path.realpath(path)
    if real != base and not real.startswith(base + os.sep):
        raise Refused(
            f"{path} is outside {root()} — cutroom writes nowhere else "
            f"(it resolves to {real})")
    if not existing_ok and os.path.lexists(path):
        raise Refused(
            f"{path} already exists — cutroom never overwrites a file, so this "
            f"output needs a name nothing is using")
    return path


def _open_new(path):
    """Create `path` and return an open fd, or raise. THE only way a file is
    created here.

    writable() is re-run immediately before the open rather than trusted from
    whenever the caller happened to compute the name, and the open itself
    carries the two flags that make the last step atomic:

      O_EXCL     — the name must be free. Two processes racing for the same
                   derived name cannot both win, so one pass can never truncate
                   another's output (a process-local threading.Lock does not
                   span two servers; this does).
      O_NOFOLLOW — the final component must not be a symlink. A `x.mp4`
                   symlinked at the director's master between the check and the
                   open fails with ELOOP instead of writing through it.

    ⚠️ RESIDUAL, stated rather than papered over: O_NOFOLLOW covers the LAST
    component only. An attacker who can swap a PARENT directory for a symlink
    in the microseconds between realpath() and open() still wins — closing that
    needs an openat() walk of every component, which the stdlib does not
    expose. Inside a single-user ~/cutroom-projects that race needs an attacker
    who already has the account; it is named here because a boundary you can
    only mostly enforce must not be written down as one you can.
    """
    writable(path)                      # re-verified NOW, not earlier
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)


def claim(path):
    """Create `path` as an empty file and return it — the name is now taken.

    This is how a destination is reserved BEFORE a subprocess is launched at
    it. ffmpeg and an external pass do their own opening, which cutroom cannot
    put O_NOFOLLOW on; what it can do is own the name first, atomically, so the
    only thing either of them can ever write over is cutroom's own empty claim.

    ⚠️ RESIDUAL: between this create and the subprocess's open, the file could
    be replaced by a symlink; the subprocess would follow it. Same window as
    _open_new's, and the same reason it cannot be closed from here — the write
    is in another program. It is bounded by O_EXCL having proved the name was
    free at claim time, and it is stated here because "ffmpeg cannot escape"
    would be a claim this code cannot enforce.
    """
    os.close(_open_new(path))
    return pathlib.Path(path)


def write_new(path, text):
    """Write `text` to a file that did not exist a moment ago, through the fd
    _open_new() returns — so nothing between the check and the write can
    redirect it."""
    with os.fdopen(_open_new(path), "w") as fh:
        fh.write(text)
    return pathlib.Path(path)


def mkdirs(path):
    """mkdir -p, but only ever inside the root."""
    writable(path, existing_ok=True)
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)
    return pathlib.Path(path)


def free_name(directory, stem, suffix):
    """<stem><suffix>, or <stem>-2<suffix>, <stem>-3<suffix>… — the first name
    in `directory` that nothing is using.

    This is what replaces "overwrite the old one". A pass run twice makes two
    files and the director throws away whichever they like, by hand.

    LOOKING is all this does. Use claim_free() to actually take one: between
    this lexists() and a create, another process can take the same name.
    """
    directory = pathlib.Path(directory)
    candidate = directory / f"{stem}{suffix}"
    n = 1
    while os.path.lexists(candidate):
        n += 1
        candidate = directory / f"{stem}-{n}{suffix}"
    return candidate


def claim_free(directory, stem, suffix, limit=10000, create=claim):
    """The first free name in `directory`, CREATED, so it is now this process's.

    `create` is what takes the name. The default claims it as an empty file for
    a subprocess to overwrite; copy_in() passes a creator that writes the whole
    file through the same descriptor, so nothing re-opens the path by name.


    free_name() + create is check-then-use across processes: the job lock is a
    threading.Lock, which serialises one server's threads and knows nothing
    about a second server on another port. Two of them ask for the next free
    name at the same instant, both are told `x__desat.mp4`, and the second
    ffmpeg truncates the first's output. So the loser of the race finds the
    name taken by O_EXCL and moves to the next one instead.
    """
    directory = pathlib.Path(directory)
    # Fail fast on a directory that is not ours — otherwise "outside the root"
    # would be retried ten thousand times as if it were a name collision.
    writable(directory, existing_ok=True)
    for n in range(1, limit + 1):
        candidate = directory / (f"{stem}{suffix}" if n == 1 else f"{stem}-{n}{suffix}")
        try:
            return create(candidate)
        except Refused:
            if not os.path.lexists(candidate):
                raise           # not a collision — a boundary said no
            continue            # the name is taken — try the next one
        except FileExistsError:
            continue            # lost the race to another process — same answer
    raise Refused(f"no free name for {stem}{suffix} in {directory} after {limit} tries")


# ---------------------------------------------------------------- boundary 4
def servable(project, path):
    """The media gate: a path is readable if and only if it IS one of this
    project's media paths.

    An exact string membership test against the project's own list, and
    deliberately nothing cleverer:

      - not a prefix check — "/clips/a.mp4.bak" is a prefix-string match of
        "/clips/a.mp4" and must not be servable;
      - not resolve-then-compare — the old gate confined paths to one
        directory, and every resolve-and-compare-prefix gate ever written has
        had a hole in it. Here the answer is already written down, so there is
        nothing to derive.

    Which means "/a/b/../b/x.mp4", "/a/b//x.mp4" and "%2fa%2fb%2fx.mp4" are all
    404 even when /a/b/x.mp4 is allowed. They are not the string the director
    added, so they are not media.
    """
    if not isinstance(path, str):
        return None
    for m in project.get("media", []):
        if m.get("path") == path:
            return pathlib.Path(path)
    return None


def _inside_project(name, path):
    """The resolved `path` if it is a real file inside <name>/, else None.

    THE READ-SIDE SYMLINK RULE, and it is a whole-chain rule because the
    alternative was a hole: checking `path.is_symlink()` asks only about the
    LAST component, and `is_file()` happily follows a symlinked parent. Replace
    <project>/renders with a symlink to /etc and request /renders/passwd — the
    final path is not itself a symlink, so the leaf check passes, and the file
    is served. Resolving the WHOLE path and requiring the result to stay inside
    the project directory is the only version of this check that has no last
    link to be fooled by.

    Media is deliberately NOT routed through here: media lives wherever the
    director's footage lives, and its gate is servable(), an exact-string
    membership test. This is for the files cutroom made itself.
    """
    base = os.path.realpath(project_dir(name))
    real = os.path.realpath(path)
    if real != base and not real.startswith(base + os.sep):
        return None
    return pathlib.Path(real) if os.path.isfile(real) else None


def open_source(path):
    """The ONLY way this program opens a media file: read-only, no exceptions.

    Passes do not use it — an external tool gets the source as an argument —
    which leaves this function as the whole of cutroom's relationship with
    footage.
    """
    return pathlib.Path(path).open("rb")


# ------------------------------------------------------------------- the locks
# One lock per project. The read-check-write is a single critical section: two
# PUTs racing the same valid version must not both pass the guard against the
# pre-write file, and the lock is what makes that true instead of merely likely.
_locks = {}
_locks_mu = threading.Lock()


def _named_lock(kind, name):
    key = (kind, check_name(name))
    with _locks_mu:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def _project_lock(name):
    return _named_lock("project", name)


# A second lock, held by the jobs that write FILES rather than the project: a
# post pass and a render. It is taken non-blocking, so a double-click gets a
# 409 instead of two ffmpeg processes running at once. It is deliberately NOT
# the project lock — a render runs for minutes and must not freeze saving.
def _job_lock(name):
    return _named_lock("job", name)


@contextlib.contextmanager
def _flock(name):
    """An advisory flock on <name>/.lock, held across a whole edit.

    The thread lock above only serialises writers INSIDE this process, and the
    declared model is a browser and an agent — two processes. flock is what
    makes them queue for the same file. It is taken on a sidecar rather than on
    the project JSON itself because the write is a rename: the fd would be
    locking an inode that stops being the file halfway through.

    Advisory means every writer must ask. That is why edit_project() exists and
    why the contract is in the module docstring: a process that writes
    <name>.json without coming through here is not serialised by anything.
    """
    lock_path = mkdirs(project_dir(name)) / ".lock"
    writable(lock_path, existing_ok=True)
    # O_NOFOLLOW: the sidecar is cutroom's own file and must not be a symlink
    # pointing at something of the director's. It is never written, only locked.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def snapshot_dir(name):
    return mkdirs(project_dir(name) / ".snapshots")


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")


def _snapshot(name, text, stamp=None):
    return write_new(snapshot_dir(name) / f"{stamp or _stamp()}.json", text)


def _swap_in(tmp, path):
    """THE one place this program replaces a file. Both callers are below.

    Kept as a single function because "cutroom overwrites exactly one kind of
    thing, by rename onto a name it owns" is a property the test suite asserts
    against the source text, and it is only meaningful if there is one call
    site to point at. Widening it needs a reason written down here, not a
    second replace() somewhere else in the file.

    Callers: the project JSON (after snapshotting the state being replaced),
    and the pending stash (which is a scratch file the page rewrites while a
    conflict is open, and whose previous content is by definition older
    unsaved work from the same episode).
    """
    writable(path, existing_ok=True)
    tmp.replace(path)
    return path


def _write_over(path, text):
    """Atomically rewrite `path`, which may or may not already exist.

    write_new() refuses a file that exists, which is right for a snapshot —
    history is append-only. The pending stash is the one thing that wants
    replacing: ONE file per conflict, rewritten as the director keeps working,
    rather than a new file per drag.
    """
    path = pathlib.Path(path)
    tmp = write_new(path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex[:8]}"), text)
    return _swap_in(tmp, path)


def _commit(name, merged, seen):
    """Serialise `merged`, snapshot the state it replaces, swap it into place.

    Returns the document, or None if the file moved under us.

    Callers hold both locks. `seen` is the bytes this edit was computed from;
    they are compared once more before the rename as a courtesy to a writer
    that ignored the flock — it turns "your edit was silently destroyed" into
    a 409 MOST of the time. It cannot do more than that: the compare and the
    rename are two syscalls and an outside writer can land between them. The
    flock is the actual mechanism; this is a net under it.

    The project JSON is the one file cutroom replaces, and the replacement is a
    rename onto a name it owns. The state being replaced is written to
    .snapshots/ FIRST, so even that swap loses nothing. The rename does destroy
    the previous DESTINATION inode — that is what replace() is — which is why
    the guarantee is written everywhere as "never deletes or overwrites media
    or a derived output, and updates its own project file atomically via
    replace-after-snapshot" rather than as a "never deletes anything".
    """
    path = project_path(name)
    text = json.dumps(merged, indent=2)
    stamp = _stamp()
    # Write the new state beside the file, then swap atomically. This file is
    # the whole cut and it is rewritten on every drag; a truncated write is the
    # one way to lose it. The tmp name carries a per-write random suffix so
    # concurrent writers never share a path even without the locks.
    tmp = write_new(
        path.with_name(f"{name}.json.tmp.{stamp}.{uuid.uuid4().hex[:8]}"), text)
    if path.read_bytes() != seen:
        # Refused. The tmp file stays where it is — cutroom does not delete —
        # and it is inside the root, named for the moment it was written.
        return None
    _snapshot(name, seen.decode(), stamp + "-prior")   # what we are replacing
    _swap_in(tmp, path)
    # Snapshot only what actually landed, so a refused write leaves no history
    # entry claiming a state the file never held.
    _snapshot(name, text, stamp)
    return merged


def _guarded_write(name, body):
    """Shared tail of write_project and edit_project: snap, validate, bump, commit.

    check_files is False on purpose and this is the whole "a missing source is
    not an error" rule: the cut can be edited and saved with footage offline.
    Only /render looks at the disk.
    """
    current, seen = body["current"], body["seen"]
    # SHAPE FIRST, and this ordering is the whole fix for a 500 that should
    # have been a 400: snap_project() and validate() both index fields
    # directly, so a body like {"clips": [{}]} raised KeyError out of render.py
    # before any rule ran, and the handler only knew how to answer Refused.
    # "Is this a project at all" is a different question from "is this a
    # renderable cut", and it has to be asked first.
    malformed = render_mod.shape_problems(body["edited"])
    if malformed:
        return 400, {"problems": malformed}
    edited = render_mod.snap_project(body["edited"])
    problems = render_mod.validate(edited, check_files=False)
    if problems:
        return 422, {"problems": problems}
    edited["version"] = current["version"] + 1
    landed = _commit(name, edited, seen)
    if landed is None:
        return 409, json.loads(project_path(name).read_text())
    return 200, landed


# Set once at startup from --passes-dir. None means post passes are disabled
# entirely, which is the correct default: a tool that can run an executable is
# a tool that can delete a file, so it stays off until the operator opts in.
PASSES_DIR = None


def pass_names():
    """The passes this server will run: the file names in --passes-dir.

    Listing a directory of scripts the operator pointed at is not the indexing
    rule being broken — that rule is about MEDIA, which enters only by hand.
    This walks no tree, follows nothing, and returns names, not paths.
    """
    if PASSES_DIR is None:
        return []
    base = pathlib.Path(PASSES_DIR).resolve()
    if not base.is_dir():
        return []
    # A symlink inside the directory pointing at /bin/rm is not a pass, and it
    # is not offered as one either — the listing applies the same containment
    # rule run_pass() does, so the page can never show a name that would be
    # refused when clicked.
    return sorted(p.name for p in base.glob("*")
                  if not p.name.startswith(".")
                  and p.is_file() and base in p.resolve().parents)


def write_project(name, body):
    """Version-guarded write. Returns (http status, payload).

    The read of the current version and the write of the new one happen inside
    the locks, held for the whole function — otherwise two concurrent PUTs
    carrying the same valid version both read the same "current", both pass the
    guard, and the second writer's replace() clobbers the first's edit.

    TWO locks, because there are two kinds of racer. The thread lock covers
    concurrent PUTs inside this process. The flock covers the other process in
    the declared model — the agent — and it is the only one of the two that can.
    """
    with _project_lock(name), _flock(name):
        seen = project_path(name).read_bytes()
        current = json.loads(seen)

        # version is an integer contract; 3.0 == 3 in Python but is not the
        # int the client is supposed to be round-tripping.
        sent = body.get("version")
        if not (isinstance(sent, int) and not isinstance(sent, bool)
                and sent == current["version"]):
            # Somebody else wrote since this client read, or the client sent
            # something that isn't the integer contract. Hand back the truth
            # and let the page re-render rather than losing one of the two
            # edits.
            return 409, current

        merged = dict(current)
        # ⚠️ `media` is the allowlist servable() consults, and a PUT may not
        # touch it. It used to be mergeable, which meant a client could append
        # {"mid": "leak", "path": "/etc/passwd"} and then GET it: a perfect
        # gate on a list the caller could edit. Media enters ONLY through the
        # deliberate add path, where it is validated. Same for `passes`, which
        # no longer exists in project data at all.
        merged.update({k: v for k, v in body.items()
                       if k not in ("version", "media", "passes")})
        merged["media"] = current.get("media", [])
        merged.pop("passes", None)
        return _guarded_write(name, {"current": current, "seen": seen, "edited": merged})


def edit_project(name, mutate):
    """THE way an agent edits the cut. Returns (status, payload), like a PUT.

        server.edit_project("myfilm", lambda p: p["clips"].pop(3))

    `mutate` is handed the current project inside the lock and may change it in
    place or return a new one. The version bump, the validation, the snapshot
    and the atomic swap are all this function's job, and — the point — the whole
    read-modify-write happens under the same flock the server takes, so an
    agent's edit and a drag in the browser queue for each other instead of
    overwriting each other.

    Writing <name>.json directly is the thing this exists to replace. It takes
    no lock, so it can land between the server's read and its rename and be
    gone without a trace.
    """
    with _project_lock(name), _flock(name):
        seen = project_path(name).read_bytes()
        current = json.loads(seen)
        edited = json.loads(seen)     # mutate gets its own copy, never `current`
        returned = mutate(edited)     # called exactly once, in place or not
        if returned is not None:
            edited = returned
        return _guarded_write(name, {"current": current, "seen": seen, "edited": edited})


def history(name):
    d = snapshot_dir(name)
    return sorted(p.stem for p in d.glob("*.json"))


# %Y%m%dT%H%M%S%f, then the suffix that marks it as a stash rather than history.
PENDING_NAME = re.compile(r"[0-9]{8}T[0-9]{12}-pending")


def pending_dir(name):
    return mkdirs(project_dir(name) / ".pending")


def pending_list(name):
    """The stashes waiting for this project, newest first."""
    d = pending_dir(name)
    return [{"stamp": p.stem, "bytes": p.stat().st_size}
            for p in sorted(d.glob("*.json"), reverse=True)]


def save_pending(name, body):
    """Put the page's unsaved cut on disk while a conflict is open.

    THE HOLE THIS FILLS: a 409 stops the page saving until the director picks
    keep-mine or take-theirs, and every drag made in between lived only in the
    tab. That is the one place the tool's own promise — nothing is silently
    lost — was not true, and a closed tab was enough to break it.

    ⚠️⚠️ IT WRITES TO `.pending/`, NOT `.snapshots/`, AND THAT SEPARATION IS THE
    WHOLE SAFETY ARGUMENT. Filing stashes as snapshots was the first design and
    it was wrong: /history/<stamp> restores ANY snapshot, and this route takes a
    document from ANY caller with no version and no proof a conflict ever
    happened. Together they made an unversioned authoritative write path — plant
    a stash, restore it, and the cut becomes something it never was. Worse, it
    turned `.snapshots/` from "every state this file actually held" into "every
    state somebody asserted", and on this project the history is the thing you
    fall back on when something eats your work.

    So a stash is NOT history. history() globs `.snapshots/` and restore() can
    only name a file there, so nothing written here is reachable by either.
    Recovery is a deliberate act that goes back out through the normal validated,
    version-guarded project write — see pending_list().

    ONE file per conflict, not one per drag: the page keeps the stamp it was
    given and sends it back, and the file is replaced in place.

    `media` is dropped and pinned empty. A stash is restorable, and media is
    the allowlist servable() consults — a route that let a POST body put a path
    into a document the server will later hand back is exactly the gate that
    was closed once already. write_project pins media from the file too, so
    this is the second of two independent locks, not the only one.
    """
    doc = body.get("doc")
    if not isinstance(doc, dict):
        return 400, {"problems": ["doc must be an object"]}
    malformed = render_mod.shape_problems(doc)
    if malformed:
        return 400, {"problems": malformed}
    stamp = body.get("stamp") or (_stamp() + "-pending")
    if not PENDING_NAME.fullmatch(str(stamp)):
        return 400, {"problems": [f"bad pending name {stamp!r}"]}
    keep = {k: v for k, v in doc.items() if k not in ("media", "passes")}
    keep["media"] = []
    _write_over(pending_dir(name) / f"{stamp}.json", json.dumps(keep, indent=2))
    return 200, {"stamp": stamp}


def restore(name, stamp):
    """Restore a snapshot, bumping version so any open page sees a conflict."""
    if not re.fullmatch(r"[0-9A-Za-z._-]+", str(stamp or "")):
        return 400, {"problems": [f"bad snapshot name {stamp!r}"]}
    snap = snapshot_dir(name) / f"{stamp}.json"
    if not snap.is_file():
        return 404, {"problems": [f"no snapshot {stamp}"]}
    body = json.loads(snap.read_text())
    body["version"] = json.loads(project_path(name).read_text())["version"]
    return write_project(name, body)


# --------------------------------------------------------------------- media
def probe(path):
    """{"dur", "w", "h", "kind", "has_video", "has_audio"}, or {} if ffprobe
    cannot say.

    Cached in `media` so the UI never re-probes on load. A source that has gone
    missing keeps whatever it last said — the cached numbers are what let a
    clip stay on the timeline, correctly sized, while its file is away.
    """
    info = render_mod.source_info(path)
    if not info:
        return {}
    # ⚠️ FLOORED TO WHOLE FRAMES, NOT THE CONTAINER'S DURATION. A container can
    # claim more time than it holds pictures: b02_22_claim_short reported
    # 3.194987s but decodes 76 frames, which at 24fps is 3.166667s. Recording the
    # container number put a clip on the timeline whose last 0.028s has no frame,
    # and TWO things went wrong with it. The monitor played the element past its
    # final frame, the decoder dropped to readyState 1 and seeked, and paint()
    # — correctly — hid a video with no picture, so the preview flashed BLACK at
    # the end of that clip. And validate() then REFUSED the same clip at export
    # (render.py: want 77 frames > have 76, "retrim it"), so `cutroom add` was
    # handing the renderer a length it would not accept.
    # COUNTED, not computed. nb_frames is no safer than duration — this same
    # file claims 77 there and decodes 76. Falling back to floor(dur * rate)
    # only when the decode cannot answer, which is still nearer the truth than
    # the container's own number.
    dur, fps = info["dur"], info.get("fps")
    if fps:
        frames = render_mod.source_frames(path)
        if frames:
            dur = frames / fps
        elif dur is not None:
            dur = math.floor(dur * fps + 1e-6) / fps
    # The UI has to tell a stem from a shot: an audio-only card carries no
    # poster frame, and a source with no sound gets no audio control.
    return {"dur": dur, "w": info["w"], "h": info["h"],
            "kind": "video" if fps else "audio",
            "has_video": bool(fps), "has_audio": bool(info["audio"])}


def _next_mid(project):
    used = {m["mid"] for m in project.get("media", [])}
    n = 1
    while f"m{n:02d}" in used:
        n += 1
    return f"m{n:02d}"


def add_media(name, paths, label=None):
    """Put files on the project's allowlist. The ONLY way media enters cutroom.

    Nothing here scans, walks, globs or watches. Each path is one the director
    named — on the command line, through the file picker, or by pasting it into
    the page — and it is stored verbatim, because verbatim is what servable()
    will compare against later.

    Returns (status, payload). Already-present paths are reported, not
    duplicated: adding the same file twice must not give one file two mids and
    split the timeline's idea of it in half.
    """
    entries, already, bad = [], [], []
    for p in paths:
        if not isinstance(p, str) or not p:
            bad.append(f"{p!r} is not a path")
            continue
        if not os.path.isabs(p):
            bad.append(f"{p} is not an absolute path — cutroom does not guess a "
                       f"working directory")
            continue
        if not pathlib.Path(p).is_file():
            bad.append(f"{p} is not a file")
            continue
        entries.append(p)
    if bad:
        return 400, {"problems": bad}
    if not entries:
        return 400, {"problems": ["no paths given"]}

    added = []

    def mutate(project):
        media = project.setdefault("media", [])
        have = {m["path"]: m for m in media}
        for p in entries:
            if p in have:
                already.append(have[p]["mid"])
                continue
            entry = {"mid": _next_mid(project), "path": p,
                     "label": label or pathlib.Path(p).stem, **probe(p)}
            media.append(entry)
            added.append(entry)

    status, payload = edit_project(name, mutate)
    if status != 200:
        return status, payload
    return 200, {"added": added, "already": already, "project": payload}


def copy_into_new(src, dst):
    """Copy `src` to `dst`, which must not exist — the second of exactly two
    writing opens in this program, and like the first it writes through the
    descriptor _open_new() just created rather than re-opening a path by name.

    The source side is open_source(): read-only, the only relationship this
    program has with footage.
    """
    with open_source(src) as fh, os.fdopen(_open_new(dst), "wb") as out:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
        # The project is about to reference this file as the master copy of a
        # shot. Closing is not the same as landing: without the flush+fsync a
        # power cut can leave the reference pointing at a truncated file, which
        # is the exact failure the copy exists to prevent.
        out.flush()
        os.fsync(out.fileno())
    return pathlib.Path(dst)


def copy_in(name, paths):
    """Copy sources INTO the project, then put the copies on the allowlist.

    The read-only guarantee never needed this. A source is opened by
    open_source() as "rb" and there is no unlink, rename, truncate or move
    anywhere in this program — a test greps for all of them. What a copy buys
    is SURVIVAL, which is a different property: the cut stops depending on a
    film repo, and a git merge that empties one of 226 clips (2026-08-25)
    leaves the cut room still holding everything it needs to render.

    The copy lands on a name claimed with O_EXCL, so an existing file is never
    replaced — importing the same source twice writes <stem>-2.mp4 and leaves
    the first alone, which is the same rule a pass follows.
    """
    check_name(name)
    if not project_path(name).is_file():
        return 400, {"problems": [f"no project {name} — make one with "
                                  f"`cutroom new {name}`"]}
    bad = []
    for pth in paths:
        if not isinstance(pth, str) or not os.path.isabs(pth):
            bad.append(f"{pth!r} is not an absolute path")
        elif not pathlib.Path(pth).is_file():
            bad.append(f"{pth} is not a file")
    if bad:
        return 400, {"problems": bad}
    if not paths:
        return 400, {"problems": ["no paths given"]}

    media_dir = mkdirs(project_dir(name) / "media")
    copies, failed = [], None
    for pth in paths:
        src = pathlib.Path(pth)
        try:
            dst = claim_free(media_dir, src.stem, src.suffix or ".mp4",
                             create=lambda cand: copy_into_new(src, cand))
        except (OSError, Refused) as e:
            # Disk full on file 45 of 45 must not orphan the 44 that landed.
            # They are real files inside the project; allowlist them, then say
            # what stopped. Nothing is deleted — a partial copy stays on disk
            # under its own claimed name and is simply not referenced.
            failed = f"{pth}: {e}"
            break
        copies.append(str(dst))

    if not copies:
        return 500, {"problems": [failed or "nothing was copied"]}
    status, payload = add_media(name, copies)
    if failed and status == 200:
        return 207, {**payload, "problems": [failed]}
    return status, payload


def pick_media(name, copy=False):
    """Open the operating system's own file picker, server-side.

    The browser cannot hand a page the absolute path of a dropped file — that
    is a deliberate browser security property and no amount of JavaScript gets
    around it — and cutroom may not go looking for a file by name, because
    looking means scanning. So the picker runs HERE, where a path is a path,
    and it returns exactly what the director chose.

    macOS only, because that is where this is verified to work; everywhere else
    it says so and points at the CLI, which is the portable route.
    """
    if sys.platform != "darwin":
        return 501, {"problems": [
            "the native picker is macOS-only — add media with "
            "`cutroom add <project> <absolute path>`"]}
    script = ('set picked to choose file with prompt "Add media to cutroom" '
              'with multiple selections allowed\n'
              'set out to ""\n'
              'repeat with f in picked\n'
              '  set out to out & POSIX path of f & linefeed\n'
              'end repeat\n'
              'return out')
    proc = subprocess.run(["osascript", "-e", script],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        # Cancelling is the overwhelmingly common non-zero exit and is not an
        # error: nothing was chosen, so nothing is added.
        if "cancel" in (proc.stderr or "").lower():
            return 200, {"added": [], "already": [], "cancelled": True}
        return 500, {"problems": [proc.stderr.strip() or "the picker failed"]}
    paths = [p for p in proc.stdout.splitlines() if p.strip()]
    if not paths:
        return 200, {"added": [], "already": [], "cancelled": True}
    return copy_in(name, paths) if copy else add_media(name, paths)


def thumb(name, project, mid):
    """One poster frame per media item, 160px wide, made once and never again.

    Reads the source, writes into the project's own thumbs/ directory, and the
    jpg is CLAIMED with O_EXCL before ffmpeg is started — so an existing
    thumbnail is handed back rather than re-made, and ffmpeg only ever writes
    into a name this process proved was free a moment ago.
    """
    entry = render_mod.media_index(project).get(mid)
    if entry is None:
        return None
    src = servable(project, entry["path"])
    if src is None or not src.is_file():
        return None
    # Nothing to grab a frame from. Asking ffmpeg anyway leaves an empty
    # claimed file behind, which is then reported as a broken thumbnail
    # forever, because a claim is never remade.
    if entry.get("has_video") is False:
        return None
    out = mkdirs(project_dir(name) / "thumbs") / f"{mid}.jpg"
    try:
        claim(out)
    except (Refused, FileExistsError):
        # Already there (or taken by another server in this instant). Made
        # once and never again — that is the point, not a fallback. An EMPTY
        # file is the claim of a run that failed; it is reported as "no
        # thumbnail" rather than served as a broken image, and it is left
        # where it is because cutroom does not delete.
        return out if out.is_file() and out.stat().st_size > 0 else None
    proc = subprocess.run(
        # -y overwrites the empty file claim() just created and nothing else:
        # the name was free, exclusively, one syscall ago.
        ["ffmpeg", "-v", "error", "-y", "-ss", "0.2", "-i", str(src),
         "-frames:v", "1", "-vf", "scale=160:-2", str(out)],
        capture_output=True, text=True)
    return out if out.is_file() and proc.returncode == 0 else None


# ---------------------------------------------------------------- post passes
def run_pass(name, uid, pass_name, args):
    """Run one external tool on a clip's source and re-point the clip at the result.

    The source is an INPUT ARGUMENT and nothing else. The tool writes a NEW file
    under derived/, on a name nothing is using, and that file is then added to
    media and the clip is re-pointed at it. There is no backup to keep, no _raw
    to audit, no checksum ledger to prove one is complete — because the original
    is never opened for writing by anybody. A tool that crashes halfway leaves a
    partial file in derived/ and touches nothing of the director's.

    ⚠️ THE EXECUTABLE NEVER COMES FROM PROJECT DATA. It used to: `passes` was a
    {name: absolute script} map read out of the project JSON, which meant a PUT
    adding {"passes": {"nuke": "/bin/rm"}} followed by POST /pass ran
    `/bin/rm <source> <dst>` and DELETED THE DIRECTOR'S FOOTAGE. The one thing
    this program exists to make impossible, reachable through a config field.

    Now: the server is given a passes DIRECTORY at startup (--passes-dir, no
    default), the project names a pass, and the name is resolved inside that
    directory. A name carrying a separator, a `..`, or a leading dot is refused
    before it touches the filesystem, and the resolved path must still sit
    inside the directory. With no --passes-dir there are no passes at all.

    THE DESTINATION IS CLAIMED BEFORE THE TOOL RUNS. claim_free() creates it
    with O_CREAT|O_EXCL, so the name is this process's before a subprocess
    exists — two servers cannot both be told the same free name and have the
    second truncate the first's output. The tool therefore receives a path that
    already exists as an empty file and MUST overwrite it (an ffmpeg-based pass
    needs -y, not -n); what it can never do is land on a name something else
    was using, because O_EXCL proved it was free.

    AND THE CLIP MAY HAVE MOVED. A pass is a subprocess that runs for minutes
    while the director keeps cutting. The mid is captured before the tool
    starts and re-checked under the lock at the end: if the clip now names
    different media, the derivative is still written and still added to
    `media`, and the answer is a 409 naming both — never a silent re-point on
    top of an edit made in the meantime.
    """
    if PASSES_DIR is None:
        return 501, {"problems": [
            "no passes directory configured — start the server with "
            "--passes-dir <dir> to enable post passes"]}
    if (not pass_name or "/" in pass_name or "\\" in pass_name
            or pass_name.startswith(".") or ".." in pass_name):
        return 400, {"problems": [f"{pass_name!r} is not a plain pass name"]}
    # Resolve the DIRECTORY too, and at use time: a PASSES_DIR that still spells
    # a symlink (/tmp -> /private/tmp) would make every containment check below
    # compare a resolved child against an unresolved parent and refuse
    # everything — or, worse, be "fixed" by dropping the resolve.
    base = pathlib.Path(PASSES_DIR).resolve()
    script = (base / pass_name).resolve()
    # resolve() first, THEN confirm containment: a symlink inside the passes
    # directory pointing at /bin/rm must not become a runnable pass.
    if base not in script.parents or not script.is_file():
        return 400, {"problems": [
            f"{pass_name!r} is not a pass in {base} "
            f"({', '.join(pass_names()) or 'none found'})"]}
    project = json.loads(project_path(name).read_text())

    clip = next((c for c in project["clips"] if c["uid"] == uid), None)
    if clip is None:
        return 404, {"problems": [f"no clip {uid}"]}
    started_mid = clip.get("mid")
    entry = render_mod.media_index(project).get(started_mid)
    if entry is None:
        return 404, {"problems": [f"{uid} names media {started_mid!r}, which is not "
                                  f"in this project"]}
    src = servable(project, entry["path"])
    if src is None or not src.is_file():
        return 404, {"problems": [f"{entry['path']} is not readable — the clip is OFFLINE"]}

    derived = mkdirs(project_dir(name) / "derived")
    # `desat.py` names the derivative `x__desat.mp4`, not `x__desat.py.mp4`.
    # Safe to take the stem: pass_name has already been refused if it holds a
    # separator, so this can only ever drop an extension.
    tag = pathlib.PurePosixPath(pass_name).stem or pass_name
    dst = claim_free(derived, f"{src.stem}__{tag}", ".mp4")
    # cwd matters, and not for tidiness: a tool that scratches into a RELATIVE
    # directory (and several do, and one of them deletes every png in it first)
    # must litter inside the project, not wherever the server was started.
    work = mkdirs(project_dir(name) / "work")
    cmd = [str(script), str(src), str(dst), *args]
    if script.suffix == ".py" or not os.access(script, os.X_OK):
        cmd.insert(0, sys.executable)
    # A list, always. No shell, so a filename or a flag can never be parsed as
    # anything but one argument.
    proc = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True)
    if proc.returncode != 0 or not dst.is_file():
        # Whatever it managed to write stays where it is. Deleting it would be
        # the one thing this program does not do, and a half-written derivative
        # costs the director a look, not a shot.
        return 500, {"problems": [proc.stderr.strip() or f"{pass_name} failed"],
                     "partial": str(dst) if os.path.lexists(dst) else None}

    added, conflict = [], []

    def mutate(p):
        media = p.setdefault("media", [])
        entry2 = {"mid": _next_mid(p), "path": str(dst),
                  "label": f"{entry.get('label', src.stem)} · {tag}",
                  **probe(dst)}
        media.append(entry2)
        added.append(entry2)
        # The derivative goes on the allowlist either way. What is conditional
        # is the RE-POINT: only if the clip still names the media this pass was
        # started against. Anything else and the director changed the clip
        # while ffmpeg was running, and re-pointing would delete that edit
        # without saying so — which is exactly how a finishing pass quietly
        # eats a save.
        for c in p["clips"]:
            if c["uid"] == uid:
                if c.get("mid") == started_mid:
                    c["mid"] = entry2["mid"]
                else:
                    conflict.append(c.get("mid"))
                break
        else:
            conflict.append(None)      # the clip itself is gone

    status, payload = edit_project(name, mutate)
    if status != 200:
        return status, payload
    if conflict:
        now = conflict[0]
        gone = ("was removed while the pass ran" if now is None
                else f"now names {now!r}, not {started_mid!r}")
        return 409, {"problems": [
            f"{uid} {gone}, so {pass_name} did NOT re-point it. The derivative "
            f"is written and is on the media list as {added[0]['mid']} "
            f"({dst}) — point the clip at it yourself if that is what you "
            f"want, or ignore it."],
            "media": added[0], "out": str(dst), "was": started_mid,
            "now": now, "project": payload}
    return 200, {"ok": True, "stdout": proc.stdout.strip(),
                 "media": added[0], "out": str(dst), "project": payload}


# --------------------------------------------------------------------- export
def export_path(name, project):
    """<name>/renders/<name>_v<NNN>.mp4, stamped with the project version.

    The version in the name is the whole point: it maps a delivered file back
    to the exact cut that made it, and that cut is still in .snapshots/, so any
    render can be reproduced rather than remembered. A second export of the
    same version lands beside the first as -2 rather than on top of it.

    The name is CLAIMED, not merely chosen: it comes back as a zero-byte file
    this process owns, so two servers exporting at the same instant get two
    files instead of one truncated one.
    """
    out = mkdirs(project_dir(name) / "renders")
    return claim_free(out, f"{name}_v{project['version']:03d}", ".mp4")


def export(name, t_from=None, t_to=None):
    path = project_path(name)
    try:
        project = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return 400, {"problems": [f"{path} is malformed at line {e.lineno}: {e.msg}"]}
    # SHAPE FIRST — validate()/canonicalise() index project["fps"] and
    # clip["in"]/["out"] directly, so a hand-edited file missing a field
    # raised KeyError out of render.py before any rule ran. See the same
    # comment on shape_problems() in edit_project().
    malformed = render_mod.shape_problems(project)
    if malformed:
        return 400, {"problems": malformed}
    missing = render_mod.offline(project)
    if missing:
        return 422, {"problems": [f"{uid}: {why} — put the file back or re-point "
                                  f"the clip, then export again"
                                  for uid, why in missing]}
    # Refuse an unrenderable cut BEFORE claiming a name. render() checks these
    # again — it is a library and its other callers are not this one — but
    # claiming first would leave a zero-byte file in renders/ every time
    # somebody hit export on a cut that was never going to render, and cutroom
    # cannot tidy that up afterwards.
    problems = render_mod.validate(project)
    if problems:
        return 422, {"problems": problems}
    if not project["clips"]:
        return 422, {"problems": ["the timeline has no clips."]}
    out = export_path(name, project)
    try:
        render_mod.render(project, out, t_from, t_to, claimed=True)
    except subprocess.CalledProcessError as e:
        return 500, {"problems": [(e.stderr or "")[-2000:]]}
    except ValueError as e:
        return 422, {"problems": [str(e)]}
    return 200, {"out": out.name, "path": str(out),
                 "seconds": round(render_mod.timeline_length(project), 6),
                 "frames": render_mod.frame_count(out),
                 "mbps": round(render_mod.bitrate(out) / 1e6, 2)}


# --------------------------------------------------------------------- server
class Handler(http.server.BaseHTTPRequestHandler):
    project_name = None

    bound_port = None

    # -- every request, read or write -----------------------------------------
    def _host_is_ours(self):
        """True if the client thinks it reached THIS server. Sends the refusal.

        ⚠️ ON READS TOO, not only on mutations. Under DNS rebinding a name the
        attacker controls resolves to 127.0.0.1, the browser then treats this
        server as same-origin, and a page can simply READ: `/` hands over the
        capability token, `/project` hands over every absolute media path on
        this machine, `/media/<mid>` hands over the footage. No CORS header
        helps once the browser believes the origin is its own — the Host header
        is the only thing that still says which name was dialled.
        """
        host = (self.headers.get("Host") or "").strip()
        allowed = {f"127.0.0.1:{self.bound_port}", f"localhost:{self.bound_port}",
                   f"[::1]:{self.bound_port}"}
        if host in allowed:
            return True
        self._send(403, {"problems": [
            f"refused: Host {host!r} is not this server's address. If you are "
            f"seeing this in a browser, open http://127.0.0.1:{self.bound_port}/ directly."]})
        return False

    def _not_cross_site(self):
        """Reject a request a browser itself labels cross-site.

        Sec-Fetch-Site is set by the browser and cannot be spoofed by page
        script. It is the only thing that separates the page's own
        `<img src="/thumb/...">` from the same tag on someone else's site —
        a token cannot, because an img tag sends no headers. Absent (curl, an
        agent, an old browser) means allowed: those are not this vector.
        """
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._send(403, {"problems": ["refused: cross-site request"]})
            return False
        return True

    # -- the mutation gate ----------------------------------------------------
    def _allowed_to_mutate(self):
        """True if this request may change anything. Sends the refusal itself.

        Three independent checks, because each one alone has a hole:
          Host   — stops DNS rebinding, where a name the attacker controls
                   resolves to 127.0.0.1 and the browser then treats this
                   server as same-origin.
          Origin — stops an ordinary cross-site POST from a page.
          token  — the one that actually holds. Origin can be absent (curl, a
                   local agent) and forged by any non-browser client; the token
                   cannot be read cross-origin.

        A local agent driving the HTTP API sends the token too; it is printed at
        startup. An agent in this process should call edit_project() instead and
        never touches this path.
        """
        if not self._host_is_ours():
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in (f"http://127.0.0.1:{self.bound_port}",
                                                 f"http://localhost:{self.bound_port}"):
            self._send(403, {"problems": [
                f"refused: {origin} is not allowed to change this cut"]})
            return False
        token = self.headers.get("X-Cutroom-Token")
        if not token or not secrets.compare_digest(token, SESSION_TOKEN):
            self._send(403, {"problems": [
                "refused: missing or wrong X-Cutroom-Token. Reload the page; a "
                "command-line caller must send the token printed when the server started."]})
            return False
        return True

    # -- helpers ------------------------------------------------------------
    def _write(self, blob):
        """Write, and treat a dropped socket as normal. The range path below
        already did this; these two did not, and now that the monitor plays
        UNMUTED video a cancelled request is a routine event on every path —
        a <video> opens a request, decides it has seen enough and goes away.
        A traceback per scrub is how a real failure gets missed."""
        try:
            self.wfile.write(blob)
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _send(self, status, payload, ctype="application/json"):
        blob = json.dumps(payload).encode() if ctype == "application/json" else payload
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._write(blob)

    def _project(self):
        return json.loads(project_path(self.project_name).read_text())

    def _range_not_satisfiable(self, size):
        """416: malformed Range header, or a range that starts past EOF.

        A dropped connection is worse than an error response — a bad Range must
        not 500, and it must not silently close the socket either.
        """
        payload = json.dumps(
            {"problems": [f"range not satisfiable for {size} byte file"]}).encode()
        self.send_response(416)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write(payload)

    def _file(self, path):
        """Static send with Range support, read-only.

        SimpleHTTPRequestHandler has none, and without it Safari refuses to
        play an mp4 at all and Chrome cannot seek — which is the whole tool.
        """
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range", "")
        start, end = 0, size - 1
        partial = rng.startswith("bytes=")
        if partial:
            try:
                spec = rng[6:].strip()
                a, sep, b = spec.partition("-")
                a, b = a.strip(), b.strip()
                # "bytes=" and "bytes=-" name no range at all. Answering them
                # with a 206 of the whole file is a lie about what was sent:
                # the client asked for nothing, and RFC 7233 calls a byte-range
                # set with no first-pos AND no suffix-length malformed. 416.
                if not sep or (a == "" and b == ""):
                    raise ValueError(f"malformed range {rng!r}")
                if a == "" and b != "":
                    # Suffix range (RFC 7233 §2.1): "bytes=-500" means the
                    # last 500 bytes, not "no start so start at 0".
                    start = max(0, size - int(b))
                    end = size - 1
                else:
                    start = int(a) if a else 0
                    end = int(b) if b else size - 1
                    end = min(end, size - 1)
                if size == 0 or start < 0 or start >= size or start > end:
                    raise ValueError("range out of bounds")
            except ValueError:
                return self._range_not_satisfiable(size)

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open_source(path) as fh:
                fh.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    if not self._write(chunk):
                        break
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # NORMAL, and it happens constantly: a <video> element opens a
            # range, decides it has seen enough and drops the socket. It is not
            # an error and it must not print a traceback — the terminal is for
            # render output, and a wall of stack traces during ordinary
            # scrubbing is how a real failure gets missed.
            pass

    # -- routes -------------------------------------------------------------
    def do_GET(self):
        if not self._host_is_ours():
            return
        route = urllib.parse.unquote(self.path.split("?")[0])
        name = self.project_name
        # /thumb is a GET that DOES something: it runs ffmpeg and writes a jpg.
        # It has to stay a GET because the page loads it with an <img> tag, and
        # an img tag cannot carry a token — so this is what keeps another site's
        # img tag from driving ffmpeg on this machine.
        if route.startswith("/thumb/") and not self._not_cross_site():
            return
        try:
            if route == "/":
                # The token is injected, never stored in ui.html: it is new every
                # run, and a file on disk is not where a capability belongs.
                page = (HERE / "ui.html").read_text()
                page = page.replace("__CUTROOM_TOKEN__", SESSION_TOKEN, 1)
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if route == "/project":
                project = self._project()
                return self._send(200, {
                    "project": project,
                    "offline": [{"uid": u, "why": w}
                                for u, w in render_mod.offline(project)],
                    # From --passes-dir, NEVER from the project document. The
                    # page shows what this server can actually run.
                    "passes": pass_names()})
            if route == "/history":
                return self._send(200, {"snapshots": history(name)})
            if route == "/pending":
                return self._send(200, {"pending": pending_list(name)})
            if route.startswith("/media/"):
                # By mid, never by path. The mid is looked up in the project and
                # the path it names still has to satisfy servable() — the two
                # gates are independent on purpose.
                mid = route[len("/media/"):]
                project = self._project()
                entry = render_mod.media_index(project).get(mid)
                path = servable(project, entry["path"]) if entry else None
                if path is None or not path.is_file():
                    return self._send(404, {"problems": [f"no media {mid}"]})
                return self._file(path)
            if route.startswith("/thumb/"):
                project = self._project()
                out = thumb(name, project, route[len("/thumb/"):])
                out = _inside_project(name, out) if out is not None else None
                if out is None:
                    return self._send(404, {"problems": ["no thumbnail"]})
                return self._file(out)
            if route.startswith("/renders/"):
                leaf = route[len("/renders/"):]
                if not re.fullmatch(r"[A-Za-z0-9._-]+", leaf):
                    return self._send(404, {"problems": [f"no {route}"]})
                path = _inside_project(name, project_dir(name) / "renders" / leaf)
                if path is None:
                    return self._send(404, {"problems": [f"no {route}"]})
                return self._file(path)
        except Refused as e:
            return self._send(400, {"problems": [str(e)]})
        self._send(404, {"problems": [f"no {route}"]})

    def do_PUT(self):
        if self.path != "/project":
            return self._send(404, {"problems": [f"no {self.path}"]})
        if not self._allowed_to_mutate():
            return
        # A PUT is a whole document, so its Content-Length is required: an
        # empty body is a malformed save, not a save of nothing.
        body = self._body(require_length=True)
        if body is None:
            return
        try:
            self._send(*write_project(self.project_name, body))
        except Refused as e:
            self._send(400, {"problems": [str(e)]})

    def _body(self, require_length=False):
        """Parsed JSON object, or None after a 400 has already been sent."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and ctype != "application/json":
            self._send(415, {"problems": [
                f"refused: {ctype} — this route takes application/json"]})
            return None
        if self.headers.get("Transfer-Encoding"):
            # A chunked body has no Content-Length to cap, and this server
            # never needs one.
            self._send(411, {"problems": ["refused: send a Content-Length, not chunked"]})
            return None
        try:
            raw = self.headers.get("Content-Length")
            if raw is None and require_length:
                raise TypeError("no Content-Length")
            # A NEGATIVE length is the trap: int("-1") parses, slips under any
            # "> MAX_BODY" test, and read(-1) then reads until EOF unbounded.
            if raw is not None and not str(raw).strip().isdigit():
                raise ValueError(f"Content-Length {raw!r} is not a whole number")
            length = int(raw or 0)
            if length > MAX_BODY:
                # Before the read, not after: the point is to not take the bytes.
                self._send(413, {"problems": [
                    f"refused: {length} bytes is over the {MAX_BODY} byte limit"]})
                return None
            body = json.loads(self.rfile.read(length)) if length else {}
        except (TypeError, ValueError) as e:
            # Missing/non-numeric Content-Length, or a body that isn't JSON
            # (json.JSONDecodeError is a ValueError subclass). A malformed
            # request is a 400, not a dropped connection and a stderr traceback
            # that log_message was specifically told to hide.
            self._send(400, {"problems": [f"malformed request: {e}"]})
            return None
        if not isinstance(body, dict):
            self._send(400, {"problems": ["body must be a JSON object"]})
            return None
        return body

    def do_POST(self):
        if not self._allowed_to_mutate():
            return
        route = urllib.parse.unquote(self.path.split("?")[0])
        name = self.project_name
        body = self._body()
        if body is None:
            return
        try:
            if route == "/media":
                paths = body.get("paths")
                if not (isinstance(paths, list) and paths
                        and all(isinstance(p, str) for p in paths)):
                    return self._send(400, {"problems": [
                        "paths must be a non-empty list of absolute path strings"]})
                if body.get("copy"):
                    return self._send(*copy_in(name, paths))
                return self._send(*add_media(name, paths))

            if route == "/media/pick":
                return self._send(*pick_media(name, bool(body.get("copy"))))

            if route == "/pass":
                uid, pass_name = body.get("uid"), body.get("pass")
                args = body.get("args") or []
                if not (isinstance(uid, str) and isinstance(pass_name, str)
                        and isinstance(args, list)
                        and all(isinstance(a, str) for a in args)):
                    return self._send(400, {"problems": [
                        "uid and pass must be strings and args a list of strings"]})
                lock = _job_lock(name)
                if not lock.acquire(blocking=False):
                    return self._send(409, {"problems": ["a pass or a render is "
                                                         "already running"]})
                try:
                    return self._send(*run_pass(name, uid, pass_name, args))
                finally:
                    lock.release()

            if route == "/render":
                lock = _job_lock(name)
                if not lock.acquire(blocking=False):
                    return self._send(409, {"problems": ["a pass or a render is "
                                                         "already running"]})
                try:
                    return self._send(*export(name, body.get("from"), body.get("to")))
                finally:
                    lock.release()

            if route == "/pending":
                return self._send(*save_pending(name, body))

            if route.startswith("/history/"):
                return self._send(*restore(name, route.split("/", 2)[2]))
        except Refused as e:
            return self._send(400, {"problems": [str(e)]})

        self._send(404, {"problems": [f"no {route}"]})

    def log_message(self, fmt, *args):
        pass  # the terminal is for render output, not a request log


# ------------------------------------------------------------------- the CLI
# No `passes` key: the executable a pass runs comes from --passes-dir at
# startup, never from project data. See run_pass for what that cost to learn.
BLANK = {"fps": 24, "resolution": [720, 1280], "version": 1,
         "media": [], "clips": []}


def create(name, fps=24, resolution=(720, 1280)):
    """A new project: one JSON file and its directory. Refuses to touch an
    existing one — that is the no-overwrite rule, not politeness."""
    check_name(name)
    doc = dict(BLANK, name=name, fps=fps, resolution=list(resolution))
    # Same shape check every other door into a project runs (PUT /project,
    # a pass's edit) — so bad fps/resolution is refused HERE, before any
    # file exists, instead of surfacing as a KeyError from deep in render.py
    # the first time something reads project["fps"].
    problems = render_mod.shape_problems(doc)
    if problems:
        raise Refused("; ".join(problems))
    mkdirs(root())
    writable(project_path(name))          # the friendly refusal, with a reason
    mkdirs(project_dir(name))
    write_new(project_path(name), json.dumps(doc, indent=2))
    return doc


# A capability token, new every run. The page is handed it when it loads; a
# website you happen to be visiting cannot read it, because reading the page
# means reading a cross-origin response body and the browser will not allow it.
#
# ⚠️ WHY A TOKEN AND NOT JUST AN ORIGIN CHECK. A cross-origin
# `Content-Type: text/plain` POST is a "simple request" — the browser sends it
# with NO preflight, so a hostile page can reach a loopback server before any
# CORS rule is consulted. Measured before this existed: a POST carrying
# `Origin: https://attacker.example` ran a real /render and the reply handed
# back an absolute path on this machine. Origin is also absent on some requests
# and forgeable by anything that is not a browser, so it cannot be the only gate.
SESSION_TOKEN = secrets.token_urlsafe(32)
MAX_BODY = 32 * 1024 * 1024      # a project document, not a media upload


def serve(name, port, passes_dir=None, open_browser=True):
    global PASSES_DIR, SESSION_TOKEN
    # New for every serve, not merely for every interpreter: two serves in one
    # process would otherwise share a capability.
    SESSION_TOKEN = secrets.token_urlsafe(32)
    check_name(name)
    if passes_dir is not None:
        PASSES_DIR = pathlib.Path(passes_dir).expanduser().resolve()
        if not PASSES_DIR.is_dir():
            sys.exit(f"--passes-dir {PASSES_DIR} is not a directory")
    if not project_path(name).is_file():
        sys.exit(f"No project at {project_path(name)} — make one with "
                 f"`cutroom new {name}`.")
    try:
        doc = json.loads(project_path(name).read_text())
    except json.JSONDecodeError as e:
        # Starting empty would look exactly like having lost the cut.
        sys.exit(f"{project_path(name)} is malformed at line {e.lineno}: {e.msg}")
    problems = render_mod.shape_problems(doc)
    if problems:
        # Refused before a port is bound or a browser tab opens — a project
        # that will throw KeyError the first time the UI loads it should
        # never get that far.
        sys.exit(f"{project_path(name)} is not a valid project:\n"
                  + "\n".join(f"  {p}" for p in problems))

    Handler.project_name = name
    Handler.bound_port = port
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"cut room — {name} — {url}   (ctrl-c to stop)")
    print(f"  passes: {PASSES_DIR if PASSES_DIR else 'disabled (no --passes-dir)'}")
    print(f"  token:  {SESSION_TOKEN}   (send as X-Cutroom-Token to change anything)")
    # Opening is a convenience for the first start, not a rule. A restart is the
    # common case while editing this file, and each one used to spawn another tab
    # — fifteen of them in one session before anybody counted.
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cutroom")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("new", help="create a project")
    p.add_argument("project")
    p.add_argument("--fps", type=int, default=24)
    p.add_argument("--res", default="720x1280")

    p = sub.add_parser("add", help="add media files by absolute path")
    p.add_argument("project")
    p.add_argument("paths", nargs="+")
    p.add_argument("--copy", action="store_true",
                   help="copy each file into the project first and reference the "
                        "copy, so the cut no longer depends on where it came from. "
                        "The source is still only ever read.")

    p = sub.add_parser("serve", help="serve the timeline")
    p.add_argument("project")
    p.add_argument("--port", type=int, default=8420)
    p.add_argument("--passes-dir", default=None,
                   help="directory of post-pass scripts. Without it, passes are "
                        "disabled entirely — the safe default, since anything "
                        "that can run an executable can delete a file.")
    p.add_argument("--no-open", action="store_true",
                   help="do not open a browser tab. The tab is a convenience on "
                        "the first start; on a restart it is another tab.")

    p = sub.add_parser("export", help="render the cut to mp4")
    p.add_argument("project")

    p = sub.add_parser("ls", help="list projects")
    sub.add_parser("check", help="run the test suites")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "new":
            w, x, h = a.res.partition("x")
            try:
                if not x:
                    raise ValueError
                resolution = (int(w), int(h))
            except ValueError:
                raise Refused(f"--res must be WIDTHxHEIGHT of integers, got {a.res!r}")
            doc = create(a.project, a.fps, resolution)
            print(f"{project_path(a.project)}  {doc['fps']}fps "
                  f"{doc['resolution'][0]}x{doc['resolution'][1]}")
        elif a.cmd == "add":
            want = [os.path.abspath(p) for p in a.paths]
            status, payload = (copy_in(a.project, want) if a.copy
                               else add_media(a.project, want))
            if status not in (200, 207):
                sys.exit("\n".join(payload.get("problems", [str(payload)])))
            for problem in payload.get("problems", []):
                print(f"  STOPPED: {problem}")
            for m in payload["added"]:
                print(f"  {m['mid']}  {m.get('dur', '?')}s  {m['path']}")
            for mid in payload["already"]:
                print(f"  {mid}  already in the project")
        elif a.cmd == "serve":
            serve(a.project, a.port, a.passes_dir, not a.no_open)
        elif a.cmd == "export":
            status, payload = export(a.project)
            if status != 200:
                sys.exit("\n".join(payload.get("problems", [str(payload)])))
            print(f"{payload['path']}  {payload['seconds']}s  "
                  f"{payload['frames']} frames  {payload['mbps']} Mbps")
        elif a.cmd == "ls":
            for p in sorted(root().glob("*.json")):
                print(p.stem)
        elif a.cmd == "check":
            for suite in ("test_render.py", "test_server.py"):
                subprocess.run([sys.executable, str(HERE / suite)], check=True)
        else:
            ap.print_help()
    except Refused as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
