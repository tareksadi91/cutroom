#!/usr/bin/env python3
"""The cut room: a local timeline the director drags and the agent edits.

    ./cutroom serve threshold          # serve + open the browser
    ./cutroom add threshold /abs/path/to/a.mp4 /abs/path/to/b.mp4
    ./cutroom new threshold

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
     not land inside the root. There is no other way to name an output.
  2. Never delete any file, anywhere, including its own derived output — there
     is no unlink, no rmtree, no shutil.move in this program, and ffmpeg is
     always invoked with -n so it may create a file but never replace one.
     test_server.py greps this file to keep it that way.
  3. Never follow a symlink out of the project directory — writable() compares
     the REALPATH, so a `derived` symlinked at /tmp resolves outside the root
     and is refused before anything is opened.
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
import mimetypes
import os
import pathlib
import re
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
    """
    directory = pathlib.Path(directory)
    candidate = directory / f"{stem}{suffix}"
    n = 1
    while os.path.lexists(candidate):
        n += 1
        candidate = directory / f"{stem}-{n}{suffix}"
    return candidate


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
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def snapshot_dir(name):
    return mkdirs(project_dir(name) / ".snapshots")


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")


def _snapshot(name, text, stamp=None):
    path = writable(snapshot_dir(name) / f"{stamp or _stamp()}.json")
    path.write_text(text)
    return path


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
    .snapshots/ FIRST, so even that swap loses nothing.
    """
    path = project_path(name)
    text = json.dumps(merged, indent=2)
    stamp = _stamp()
    # Write the new state beside the file, then swap atomically. This file is
    # the whole cut and it is rewritten on every drag; a truncated write is the
    # one way to lose it. The tmp name carries a per-write random suffix so
    # concurrent writers never share a path even without the locks.
    tmp = writable(path.with_name(f"{name}.json.tmp.{stamp}.{uuid.uuid4().hex[:8]}"))
    tmp.write_text(text)
    if path.read_bytes() != seen:
        # Refused. The tmp file stays where it is — cutroom does not delete —
        # and it is inside the root, named for the moment it was written.
        return None
    _snapshot(name, seen.decode(), stamp + "-prior")   # what we are replacing
    writable(path, existing_ok=True)
    tmp.replace(path)
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
    edited = render_mod.snap_project(body["edited"])
    problems = render_mod.validate(edited, check_files=False)
    if problems:
        return 422, {"problems": problems}
    edited["version"] = current["version"] + 1
    landed = _commit(name, edited, seen)
    if landed is None:
        return 409, json.loads(project_path(name).read_text())
    return 200, landed


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
        merged.update({k: v for k, v in body.items() if k != "version"})
        return _guarded_write(name, {"current": current, "seen": seen, "edited": merged})


def edit_project(name, mutate):
    """THE way an agent edits the cut. Returns (status, payload), like a PUT.

        server.edit_project("threshold", lambda p: p["clips"].pop(3))

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
    """{"dur", "w", "h"} for a source, or {} if ffprobe cannot say.

    Cached in `media` so the UI never re-probes on load. A source that has gone
    missing keeps whatever it last said — the cached numbers are what let a
    clip stay on the timeline, correctly sized, while its file is away.
    """
    info = render_mod.source_info(path)
    if not info:
        return {}
    return {"dur": info["dur"], "w": info["w"], "h": info["h"]}


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


def pick_media(name):
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
            "`cutroom add <project> <absolute path>` or paste the path above"]}
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
    return add_media(name, paths)


def thumb(name, project, mid):
    """One poster frame per media item, 160px wide, made once and never again.

    Reads the source, writes into the project's own thumbs/ directory, and uses
    ffmpeg -n so it can create the jpg but can never replace one.
    """
    entry = render_mod.media_index(project).get(mid)
    if entry is None:
        return None
    src = servable(project, entry["path"])
    if src is None or not src.is_file():
        return None
    out = mkdirs(project_dir(name) / "thumbs") / f"{mid}.jpg"
    if out.is_file():
        return out
    writable(out)
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-n", "-ss", "0.2", "-i", str(src),
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

    Tools come from the project's own `passes` map, {name: absolute script},
    and are invoked as `script src dst [args]`. A name from that map, never a
    string from the request, and never shell=True.
    """
    project = json.loads(project_path(name).read_text())
    passes = project.get("passes") or {}
    if pass_name not in passes:
        return 400, {"problems": [
            f"{pass_name!r} is not one of this project's passes "
            f"({', '.join(sorted(passes)) or 'none configured'}) — add it to "
            f"\"passes\" in {project_path(name).name} as an absolute path"]}
    script = pathlib.Path(passes[pass_name])
    if not script.is_file():
        return 500, {"problems": [f"{pass_name} points at {script}, which is not a file"]}

    clip = next((c for c in project["clips"] if c["uid"] == uid), None)
    if clip is None:
        return 404, {"problems": [f"no clip {uid}"]}
    entry = render_mod.media_index(project).get(clip.get("mid"))
    if entry is None:
        return 404, {"problems": [f"{uid} names media {clip.get('mid')!r}, which is not "
                                  f"in this project"]}
    src = servable(project, entry["path"])
    if src is None or not src.is_file():
        return 404, {"problems": [f"{entry['path']} is not readable — the clip is OFFLINE"]}

    derived = mkdirs(project_dir(name) / "derived")
    dst = writable(free_name(derived, f"{src.stem}__{pass_name}", ".mp4"))
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

    added = []

    def mutate(p):
        media = p.setdefault("media", [])
        entry2 = {"mid": _next_mid(p), "path": str(dst),
                  "label": f"{entry.get('label', src.stem)} · {pass_name}",
                  **probe(dst)}
        media.append(entry2)
        added.append(entry2)
        for c in p["clips"]:
            if c["uid"] == uid:
                c["mid"] = entry2["mid"]

    status, payload = edit_project(name, mutate)
    if status != 200:
        return status, payload
    return 200, {"ok": True, "stdout": proc.stdout.strip(),
                 "media": added[0], "out": str(dst), "project": payload}


# --------------------------------------------------------------------- export
def export_path(name, project):
    """<name>/renders/<name>_v<NNN>.mp4, stamped with the project version.

    The version in the name is the whole point: it maps a delivered file back
    to the exact cut that made it, and that cut is still in .snapshots/, so any
    render can be reproduced rather than remembered. A second export of the
    same version lands beside the first as -2 rather than on top of it.
    """
    out = mkdirs(project_dir(name) / "renders")
    return writable(free_name(out, f"{name}_v{project['version']:03d}", ".mp4"))


def export(name, t_from=None, t_to=None):
    project = render_mod.load(project_path(name))
    missing = render_mod.offline(project)
    if missing:
        return 422, {"problems": [f"{uid}: {why} — put the file back or re-point "
                                  f"the clip, then export again"
                                  for uid, why in missing]}
    out = export_path(name, project)
    try:
        render_mod.render(project, out, t_from, t_to)
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

    # -- helpers ------------------------------------------------------------
    def _send(self, status, payload, ctype="application/json"):
        blob = json.dumps(payload).encode() if ctype == "application/json" else payload
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

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
        self.wfile.write(payload)

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
                a, _, b = rng[6:].partition("-")
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
                    self.wfile.write(chunk)
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
        route = urllib.parse.unquote(self.path.split("?")[0])
        name = self.project_name
        try:
            if route == "/":
                return self._send(200, (HERE / "ui.html").read_bytes(),
                                  "text/html; charset=utf-8")
            if route == "/project":
                project = self._project()
                return self._send(200, {
                    "project": project,
                    "offline": [{"uid": u, "why": w}
                                for u, w in render_mod.offline(project)],
                    "passes": sorted((project.get("passes") or {}).keys())})
            if route == "/history":
                return self._send(200, {"snapshots": history(name)})
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
                if out is None:
                    return self._send(404, {"problems": ["no thumbnail"]})
                return self._file(out)
            if route.startswith("/renders/"):
                leaf = route[len("/renders/"):]
                if not re.fullmatch(r"[A-Za-z0-9._-]+", leaf):
                    return self._send(404, {"problems": [f"no {route}"]})
                path = project_dir(name) / "renders" / leaf
                if not path.is_file() or path.is_symlink():
                    return self._send(404, {"problems": [f"no {route}"]})
                return self._file(path)
        except Refused as e:
            return self._send(400, {"problems": [str(e)]})
        self._send(404, {"problems": [f"no {route}"]})

    def do_PUT(self):
        if self.path != "/project":
            return self._send(404, {"problems": [f"no {self.path}"]})
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
        try:
            raw = self.headers.get("Content-Length")
            if raw is None and require_length:
                raise TypeError("no Content-Length")
            length = int(raw or 0)
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
                return self._send(*add_media(name, paths))

            if route == "/media/pick":
                return self._send(*pick_media(name))

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

            if route.startswith("/history/"):
                return self._send(*restore(name, route.split("/", 2)[2]))
        except Refused as e:
            return self._send(400, {"problems": [str(e)]})

        self._send(404, {"problems": [f"no {route}"]})

    def log_message(self, fmt, *args):
        pass  # the terminal is for render output, not a request log


# ------------------------------------------------------------------- the CLI
BLANK = {"fps": 24, "resolution": [720, 1280], "version": 1,
         "media": [], "clips": [], "passes": {}}


def create(name, fps=24, resolution=(720, 1280)):
    """A new project: one JSON file and its directory. Refuses to touch an
    existing one — that is the no-overwrite rule, not politeness."""
    check_name(name)
    mkdirs(root())
    path = writable(project_path(name))
    mkdirs(project_dir(name))
    doc = dict(BLANK, name=name, fps=fps, resolution=list(resolution))
    path.write_text(json.dumps(doc, indent=2))
    return doc


def serve(name, port):
    check_name(name)
    if not project_path(name).is_file():
        sys.exit(f"No project at {project_path(name)} — make one with "
                 f"`cutroom new {name}`.")
    try:
        json.loads(project_path(name).read_text())
    except json.JSONDecodeError as e:
        # Starting empty would look exactly like having lost the cut.
        sys.exit(f"{project_path(name)} is malformed at line {e.lineno}: {e.msg}")

    Handler.project_name = name
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"cut room — {name} — {url}   (ctrl-c to stop)")
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

    p = sub.add_parser("serve", help="serve the timeline")
    p.add_argument("project")
    p.add_argument("--port", type=int, default=8420)

    p = sub.add_parser("export", help="render the cut to mp4")
    p.add_argument("project")

    p = sub.add_parser("ls", help="list projects")
    sub.add_parser("check", help="run the test suites")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "new":
            w, _, h = a.res.partition("x")
            doc = create(a.project, a.fps, (int(w), int(h)))
            print(f"{project_path(a.project)}  {doc['fps']}fps "
                  f"{doc['resolution'][0]}x{doc['resolution'][1]}")
        elif a.cmd == "add":
            status, payload = add_media(a.project, [os.path.abspath(p) for p in a.paths])
            if status != 200:
                sys.exit("\n".join(payload.get("problems", [str(payload)])))
            for m in payload["added"]:
                print(f"  {m['mid']}  {m.get('dur', '?')}s  {m['path']}")
            for mid in payload["already"]:
                print(f"  {mid}  already in the project")
        elif a.cmd == "serve":
            serve(a.project, a.port)
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
