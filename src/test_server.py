#!/usr/bin/env python3
"""Checks for server.py — the boundaries, the version guard and the snapshots.

    python3 test_server.py

Bare asserts, no pytest. Every test points server.ROOT at its own temporary
directory and synthesises its own fixtures. NO TEST EVER ADDRESSES REAL
FOOTAGE, and nothing here writes outside the directory it made.
"""
import contextlib
import errno
import http.client
import http.server
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import render
import server


@contextlib.contextmanager
def project(name="t", clips=None, media=None, **kw):
    """A project rooted in a fresh temporary ~/cutroom-projects."""
    with tempfile.TemporaryDirectory() as d:
        old, server.ROOT = server.ROOT, pathlib.Path(d)
        try:
            doc = server.create(name)
            doc.update(kw)
            doc["version"] = kw.get("version", 3)
            if clips is not None:
                doc["clips"] = clips
            if media is not None:
                doc["media"] = media
            server.project_path(name).write_text(json.dumps(doc, indent=2))
            yield name, pathlib.Path(d)
        finally:
            server.ROOT = old


@contextlib.contextmanager
def passes_dir(root, tools=None):
    """A --passes-dir with `tools` in it, installed on the module global.

    The executable a pass runs comes from the SERVER'S STARTUP CONFIGURATION
    and never from project data, so every pass test has to build one of these
    instead of writing {"passes": {"name": "/abs/path"}} into the project — the
    field that used to make `{"nuke": "/bin/rm"}` a runnable pass and is gone.

    `tools` is {name: source}. The pass is then referred to BY NAME.
    """
    d = pathlib.Path(root) / "passes"
    d.mkdir(parents=True, exist_ok=True)
    for tool_name, source in (tools or {}).items():
        (d / tool_name).write_text(source)
    old, server.PASSES_DIR = server.PASSES_DIR, d.resolve()
    try:
        yield d.resolve()
    finally:
        server.PASSES_DIR = old


# A pass: reads argv[1], writes argv[2]. Exactly the documented contract —
# including that the destination ALREADY EXISTS as the empty file cutroom
# claimed to reserve the name, so the tool overwrites it (-y, never -n).
NEGATE = ("import subprocess, sys\n"
          "subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', sys.argv[1],\n"
          "                '-vf', 'negate', '-c:v', 'libx264', '-crf', '20',\n"
          "                '-pix_fmt', 'yuv420p', sys.argv[2]], check=True)\n")

COPY = "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2])\n"


def synth(path, dur=1.0, size="160x120"):
    """A throwaway clip. Every test that touches a file makes its own."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc2=s={size}:d={dur}:r=24",
                    "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
                    str(path)], check=True)
    return path


def clip(uid, t, mid, **kw):
    c = {"uid": uid, "label": uid, "lane": 0, "t": t, "mid": mid,
         "in": 0.0, "out": 0.5, "rate": 1.0, "note": ""}
    c.update(kw)
    return c


def media_entry(mid, path, dur=1.0, w=160, h=120):
    return {"mid": mid, "path": str(path), "label": pathlib.Path(path).stem,
            "dur": dur, "w": w, "h": h}


def request(port, method, route, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=600)
    blob = json.dumps(body).encode() if body is not None else b""
    h = {"Content-Type": "application/json", "Content-Length": str(len(blob))}
    h.update(headers or {})
    conn.request(method, route, body=blob, headers=h)
    resp = conn.getresponse()
    status, raw, hdrs = resp.status, resp.read(), dict(resp.getheaders())
    conn.close()
    return status, raw, hdrs


def post(port, route, body=None):
    status, raw, _ = request(port, "POST", route, body or {})
    return status, json.loads(raw or b"{}")


def start_server(name):
    """Real ThreadingHTTPServer on an ephemeral port, for the tests that need
    the socket layer (Range parsing, malformed requests, the concurrent-PUT
    race) rather than calling write_project directly."""
    server.Handler.project_name = name
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def stop_server(srv):
    srv.shutdown()
    srv.server_close()


# ============================================================ the version guard

def test_a_matching_version_writes_and_bumps():
    with project() as (name, root):
        status, payload = server.write_project(name, {"version": 3, "clips": []})
        assert status == 200, (status, payload)
        assert payload["version"] == 4, payload
        assert json.loads(server.project_path(name).read_text())["version"] == 4


def test_a_stale_version_is_refused_and_hands_back_the_current_file():
    """Without this, the agent writes the project, the open page saves over it,
    and the edit vanishes silently."""
    with project() as (name, root):
        status, payload = server.write_project(name, {"version": 1, "clips": []})
        assert status == 409, status
        assert payload["version"] == 3, payload
        assert json.loads(server.project_path(name).read_text())["version"] == 3


def test_a_float_version_does_not_satisfy_the_integer_guard():
    """3.0 == 3 in Python; the version contract is an int, not a number."""
    with project() as (name, root):
        status, payload = server.write_project(name, {"version": 3.0, "clips": []})
        assert status == 409, (status, payload)
        assert json.loads(server.project_path(name).read_text())["version"] == 3


def test_every_write_snapshots_both_the_state_it_replaces_and_the_one_it_lands():
    """The project JSON is the one file cutroom replaces. It is replaced by a
    rename, and the state being replaced is written to .snapshots/ first — so
    even that swap cannot lose a state."""
    with project() as (name, root):
        server.write_project(name, {"version": 3, "clips": []})
        server.write_project(name, {"version": 4, "clips": []})
        snaps = sorted(p.name for p in server.snapshot_dir(name).glob("*.json"))
        assert len(snaps) == 4, snaps
        assert sum(1 for s in snaps if s.endswith("-prior.json")) == 2, snaps
        priors = [json.loads((server.snapshot_dir(name) / s).read_text())["version"]
                  for s in snaps if s.endswith("-prior.json")]
        assert sorted(priors) == [3, 4], priors


def test_an_invalid_cut_is_refused_before_it_is_written():
    with project() as (name, root):
        bad = {"version": 3, "media": [media_entry("m01", "/nowhere/x.mp4")],
               "clips": [clip("a", 0, "m01", out=0.0)]}
        status, payload = server.write_project(name, bad)
        assert status == 422, (status, payload)
        assert payload["problems"], payload
        assert json.loads(server.project_path(name).read_text())["version"] == 3


def test_two_concurrent_puts_with_the_same_version_yield_one_200_and_one_real_409():
    """Without a lock around read-check-write, both racers can read the same
    'current', both pass the guard, and the loser's replace() either clobbers
    the winner or throws and drops the connection."""
    with project() as (name, root):
        srv, port = start_server(name)
        results, mu = [], threading.Lock()

        def fire():
            status, _, _ = request(port, "PUT", "/project", {"version": 3, "clips": []})
            with mu:
                results.append(status)

        try:
            threads = [threading.Thread(target=fire) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            stop_server(srv)

        assert sorted(results) == [200, 409], results
        assert json.loads(server.project_path(name).read_text())["version"] == 4


def test_a_drag_is_snapped_to_the_grid_on_save():
    """An edit point is a frame, and a drag is a pixel. The server snaps every
    save BEFORE validating, so the browser cannot write an off-grid value."""
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4", dur=2.0)
        # media is seeded through the fixture, never through the PUT body: a
        # save may not touch the allowlist servable() consults.
        server.project_path(name).write_text(json.dumps(dict(
            json.loads(server.project_path(name).read_text()),
            media=[media_entry("m01", src, dur=2.0)], version=3), indent=2))
        dragged = clip("c000", 0.3007, "m01", **{"in": 0.01})
        dragged["out"] = 0.4208333333
        status, payload = server.write_project(
            name, {"version": 3, "clips": [dragged]})
        assert status == 200, payload

        saved = json.loads(server.project_path(name).read_text())["clips"][0]
        assert render.frames_at(saved["t"], 24) == 7 and saved["in"] == 0.0, saved
        assert render.frames_at(saved["out"], 24) == 10, saved

        # the agent's door snaps by the same rule
        status, payload = server.edit_project(
            name, lambda p: {**p, "clips": [dict(p["clips"][0], t=0.917)]})
        assert status == 200, payload
        assert render.frames_at(payload["clips"][0]["t"], 24) == 22, payload["clips"][0]


def test_edit_project_is_the_documented_way_in_and_behaves_like_a_put():
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4", dur=1.0)
        server.add_media(name, [str(src)])

        status, payload = server.edit_project(
            name, lambda p: p["clips"].append(clip("c000", 0.0, "m01")))
        assert status == 200, payload
        assert len(payload["clips"]) == 1, payload

        status, payload = server.edit_project(name, lambda p: {**p, "fps": 24})
        assert status == 200, payload

        # an edit that breaks the cut is refused, not written
        was = json.loads(server.project_path(name).read_text())
        status, payload = server.edit_project(
            name, lambda p: {**p, "clips": [dict(p["clips"][0], t=-5.0)]})
        assert status == 422, (status, payload)
        assert any("negative" in x for x in payload["problems"]), payload
        assert json.loads(server.project_path(name).read_text()) == was

    # The contract itself, written where an agent will read it. flock is
    # advisory, so this sentence is load-bearing in a way a check cannot be.
    doc = " ".join(server.__doc__.lower().split())
    assert "never by writing <name>.json directly" in doc, server.__doc__
    assert "edit_project" in doc and "flock" in doc, server.__doc__


def test_the_flock_serialises_an_agent_writing_in_the_real_window():
    """Compare-then-replace cannot be made safe: the compare and the rename are
    two syscalls and an outside writer lands between them. So the window is held
    open ON PURPOSE here — a sleep spliced into a copy of the module — and a real
    second PROCESS edits through edit_project() during it. flock is what makes
    that process queue instead of interleave, and the test asserts BOTH edits
    survive rather than the last writer winning."""
    import importlib.util
    with project() as (name, root):
        src = (pathlib.Path(server.HERE) / "server.py").read_text()
        marker = "    tmp.replace(path)\n"
        assert marker in src, "the window this test aims at has moved"
        slow_path = root / "server_slow.py"
        slow_path.write_text(
            src.replace(marker, "    __import__('time').sleep(1.0)\n" + marker, 1))

        spec = importlib.util.spec_from_file_location("server_slow", slow_path)
        slow = importlib.util.module_from_spec(spec)
        sys.modules["server_slow"] = slow
        spec.loader.exec_module(slow)
        slow.ROOT = server.ROOT

        agent = root / "agent.py"
        agent.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(server.HERE)!r})\n"
            f"sys.path.insert(0, {str(root)!r})\n"
            "import server_slow as s\n"
            f"s.ROOT = {str(server.ROOT)!r}\n"
            "def mutate(p):\n"
            "    p['agent'] = True\n"
            f"status, payload = s.edit_project({name!r}, mutate)\n"
            "print(status)\n")

        result = {}

        def browser():
            result["put"] = slow.write_project(
                name, {"version": 3, "clips": [], "browser": True})

        t = threading.Thread(target=browser)
        t.start()
        started = time.monotonic()
        time.sleep(0.4)                             # now inside the held window
        proc = subprocess.run([sys.executable, str(agent)],
                              capture_output=True, text=True, timeout=60)
        waited = time.monotonic() - started
        t.join(timeout=60)

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "200", (proc.stdout, proc.stderr)
        assert result["put"][0] == 200, result["put"]
        assert waited > 0.9, \
            f"the agent did not queue behind the browser's write ({waited:.2f}s)"

        final = json.loads(server.project_path(name).read_text())
        assert final.get("browser") is True, f"the browser's edit was lost: {final}"
        assert final.get("agent") is True, f"the agent's edit was lost: {final}"
        assert final["version"] == 5, final


# ================================================================== the history

def test_restore_bumps_the_version_so_an_open_page_reloads():
    with project() as (name, root):
        server.write_project(name, {"version": 3, "clips": []})
        stamp = [s for s in server.history(name) if not s.endswith("-prior")][0]
        status, payload = server.restore(name, stamp)
        assert status == 200, (status, payload)
        assert payload["version"] == 5, payload


def test_a_history_stamp_cannot_escape_the_snapshot_directory():
    with project() as (name, root):
        status, payload = server.restore(name, "../../../../etc/passwd")
        assert status == 400, (status, payload)
        status, payload = server.restore(name, "nope")
        assert status == 404, (status, payload)


def test_posting_a_history_stamp_restores_it_and_bumps_the_version():
    with project() as (name, root):
        server.write_project(name, {"version": 3, "clips": []})
        stamp = [s for s in server.history(name) if not s.endswith("-prior")][0]
        srv, port = start_server(name)
        try:
            status, payload = post(port, "/history/" + stamp)
        finally:
            stop_server(srv)
        assert status == 200, (status, payload)
        assert payload["version"] == 5, payload


# ====================================================== BOUNDARY 4: the media gate

def test_a_path_is_servable_only_if_it_is_in_the_media_list():
    """The whole gate, and every way of nearly being on the list.

    Not a prefix check, not a resolve-and-compare: an exact string membership
    test against the project's own list. Everything else is 404, including a
    path that names the same bytes.
    """
    allowed = "/Volumes/footage/clips/a.mp4"
    p = {"media": [media_entry("m01", allowed)]}
    assert server.servable(p, allowed) is not None

    for spelling in [
            "/Volumes/footage/clips/a.mp4.bak",       # prefix-string match
            "/Volumes/footage/clips/a.mp4x",          # prefix-string match
            "/Volumes/footage/clips/../clips/a.mp4",  # traversal to the same file
            "/Volumes/footage/clips//a.mp4",          # same file, different string
            "/Volumes/footage/clips/./a.mp4",
            "/Volumes/footage/clips/A.mp4",           # macOS is case-insensitive
            "/etc/passwd",
            "../../../etc/passwd",
            "clips/a.mp4",
            "", None, 7, ["/Volumes/footage/clips/a.mp4"]]:
        assert server.servable(p, spelling) is None, spelling

    # and an empty project serves nothing at all
    assert server.servable({"media": []}, allowed) is None
    assert server.servable({}, allowed) is None


def test_the_http_layer_refuses_an_encoded_path_dressed_up_as_a_mid():
    """Media is addressed by mid, so a path never travels in a URL — and a URL
    that spells one anyway, encoded separators and all, is a 404."""
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4")
        server.add_media(name, [str(src)])
        srv, port = start_server(name)
        try:
            ok, _, _ = request(port, "GET", "/media/m01")
            attempts = ["/media/" + urllib.parse.quote(str(src), safe=""),
                        "/media/..%2f..%2fetc%2fpasswd",
                        "/media/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                        "/media/" + urllib.parse.quote("../" * 6 + "etc/passwd", safe=""),
                        "/media/m01/../../etc/passwd",
                        "/media/",
                        "/etc/passwd",
                        "/" + urllib.parse.quote(str(src), safe="")]
            got = [request(port, "GET", a)[0] for a in attempts]
        finally:
            stop_server(srv)
        assert ok == 200, ok
        assert all(s == 404 for s in got), list(zip(attempts, got))


def test_media_that_was_never_added_cannot_be_reached_by_any_route():
    """The file exists, is readable, and sits right next to one that IS on the
    list. It is still 404, because being on the list is the only qualification.
    """
    with project() as (name, root):
        allowed = synth(root / "src" / "yes.mp4")
        secret = synth(root / "src" / "no.mp4")
        server.add_media(name, [str(allowed)])
        doc = json.loads(server.project_path(name).read_text())
        assert server.servable(doc, str(allowed)) is not None
        assert server.servable(doc, str(secret)) is None
        srv, port = start_server(name)
        try:
            assert request(port, "GET", "/media/m02")[0] == 404
            assert request(port, "GET", "/thumb/m02")[0] == 404
        finally:
            stop_server(srv)


def test_a_pass_cannot_address_media_that_is_not_in_the_project():
    with project() as (name, root), passes_dir(root, {"copy.py": COPY}):
        src = synth(root / "src" / "x.mp4")
        # Written by hand, because a clip naming media the project does not have
        # cannot be SAVED — validate() refuses it. The pass has to refuse it too.
        doc = json.loads(server.project_path(name).read_text())
        doc.update({"media": [media_entry("m01", src)],
                    "clips": [clip("c1", 0.0, "m99")]})
        server.project_path(name).write_text(json.dumps(doc))
        status, payload = server.run_pass(name, "c1", "copy.py", [])
        assert status == 404, (status, payload)
        assert "m99" in payload["problems"][0], payload

        # and a mid that IS in media but whose path has been taken off the list
        # is refused by servable(), not by the file system
        doc["media"] = []
        doc["clips"] = [clip("c1", 0.0, "m01")]
        server.project_path(name).write_text(json.dumps(doc))
        status, payload = server.run_pass(name, "c1", "copy.py", [])
        assert status == 404, (status, payload)


# =========================================== BOUNDARY 1 and 3: where writes land

def test_nothing_can_be_written_outside_the_projects_root():
    with project() as (name, root):
        for outside in ["/tmp/cutroom-escape.mp4",
                        str(root.parent / "escape.mp4"),
                        str(root / ".." / "escape.mp4"),
                        str(root) + "/../escape.mp4"]:
            try:
                server.writable(outside)
                assert False, f"{outside} was accepted"
            except server.Refused as e:
                assert "outside" in str(e), e
        # and a relative path is refused rather than resolved against a cwd
        try:
            server.writable("derived/x.mp4")
            assert False, "a relative path was accepted"
        except server.Refused as e:
            assert "absolute" in str(e), e
        # inside is fine
        assert server.writable(root / name / "derived" / "x.mp4")


def test_a_symlinked_project_subdirectory_cannot_smuggle_a_write_out():
    """BOUNDARY 3. `derived` replaced by a symlink to somewhere else is exactly
    the shape of the accident this rewrite exists for: git placed a symlink
    where a directory had been. Here the write is refused, because writable()
    compares REALPATHS — a lexical check would sail straight through."""
    with project() as (name, root), tempfile.TemporaryDirectory() as elsewhere:
        elsewhere = pathlib.Path(elsewhere)
        (elsewhere / "precious.mp4").write_bytes(b"IRREPLACEABLE")
        derived = root / name / "derived"
        derived.symlink_to(elsewhere, target_is_directory=True)

        try:
            server.writable(derived / "new.mp4")
            assert False, "a write through the symlink was accepted"
        except server.Refused as e:
            assert "outside" in str(e), e
        try:
            server.mkdirs(derived)
            assert False, "mkdirs followed the symlink out"
        except server.Refused as e:
            assert "outside" in str(e), e
        assert (elsewhere / "precious.mp4").read_bytes() == b"IRREPLACEABLE"
        assert sorted(p.name for p in elsewhere.iterdir()) == ["precious.mp4"]


def test_a_pass_through_a_symlinked_derived_directory_is_refused_not_followed():
    with project() as (name, root), tempfile.TemporaryDirectory() as elsewhere, \
            passes_dir(root, {"copy.py": COPY}):
        src = synth(root / "src" / "x.mp4")
        (root / name / "derived").symlink_to(elsewhere, target_is_directory=True)
        server.edit_project(name, lambda p: p.update(
            {"media": [media_entry("m01", src)],
             "clips": [clip("c1", 0.0, "m01")]}))
        try:
            server.run_pass(name, "c1", "copy.py", [])
            assert False, "the pass wrote through the symlink"
        except server.Refused as e:
            assert "outside" in str(e), e
        assert list(pathlib.Path(elsewhere).iterdir()) == []


def test_a_project_name_is_a_filename_and_never_a_path():
    with project() as (name, root):
        for bad in ["../escape", "a/b", "/etc/passwd", "..", "", ".hidden", None, 7]:
            try:
                server.check_name(bad)
                assert False, f"{bad!r} was accepted as a project name"
            except server.Refused:
                pass
        assert server.check_name("threshold-2.final") == "threshold-2.final"


def test_an_existing_file_is_never_a_write_target():
    with project() as (name, root):
        taken = server.mkdirs(root / name / "derived") / "x.mp4"
        taken.write_bytes(b"ALREADY HERE")
        try:
            server.writable(taken)
            assert False, "an existing file was accepted as a write target"
        except server.Refused as e:
            assert "already exists" in str(e), e
        # free_name is how everything gets a name nothing is using
        assert server.free_name(taken.parent, "x", ".mp4").name == "x-2.mp4"
        (taken.parent / "x-2.mp4").write_bytes(b"AND SO IS THIS")
        assert server.free_name(taken.parent, "x", ".mp4").name == "x-3.mp4"
        assert taken.read_bytes() == b"ALREADY HERE"


# ================================================= BOUNDARY 2: it deletes nothing

def test_the_program_contains_no_way_to_delete_a_file():
    """BOUNDARY 2, enforced by reading the program — and stated at the width the
    program can actually keep.

    The old wording was "never deletes any file, anywhere, ever". It was false
    as written: every save ends in tmp.replace(path), and a rename onto an
    existing name destroys what was at the destination. The BEHAVIOUR is right
    (it is the atomic-write pattern, and the state being replaced is
    snapshotted first); the CLAIM was wrong, and a boundary written wider than
    the code can hold is worse than no boundary, because it is the sentence
    somebody trusts.

    So: no deletion primitives at all, media and derived outputs are never
    overwritten (O_EXCL on every create), and exactly one replace() — the
    project file's own atomic swap.
    """
    for mod in ("server.py", "render.py"):
        src = (pathlib.Path(server.HERE) / mod).read_text()
        # strip comments and docstrings so the words may be DISCUSSED but the
        # calls may not be made
        code = "\n".join(line.split("#")[0] for line in src.splitlines())
        for forbidden in ("unlink(", "rmtree(", "os.remove(", "os.rmdir(",
                          "shutil.move(", "os.truncate(", "rename("):
            assert forbidden not in code, f"{mod} can spell {forbidden}"
        # Sources are opened "rb" and in no other mode. The one writing mode is
        # "w" through os.fdopen() of a descriptor that O_CREAT|O_EXCL just
        # created — a path is never re-opened by name to be written, because
        # re-opening by name is the check-then-use window all over again.
        for call, mode in re.findall(r"(\w*open)\([^)]*?[\"']([rwax]b?\+?)[\"']", code):
            assert (call, mode) == ("open", "rb"), (mod, call, mode)
        # The one writing open, spelled out, because the regex above cannot see
        # through a nested call and a rule nothing can violate is not a rule.
        # A file is written by fdopen()ing the descriptor _open_new() just
        # created — never by re-opening a path by name, which would be the
        # check-then-use window all over again.
        assert code.count("fdopen(") == (2 if mod == "server.py" else 0), mod
        if mod == "server.py":
            # two, and both on a descriptor O_CREAT|O_EXCL just made: the
            # project JSON's atomic swap, and copy_in()'s import of a source.
            assert 'os.fdopen(_open_new(path), "w")' in code, \
                "the project write is no longer on an _open_new descriptor"
            assert 'os.fdopen(_open_new(dst), "wb")' in code, \
                "the media copy is no longer on an _open_new descriptor"
        # Every file this program creates is created with O_EXCL, which is what
        # makes "never overwrites a derived output" true of ffmpeg's writes too:
        # the destination is claimed first, so -y can only land on our own
        # zero-byte claim.
        assert "O_EXCL" in code, f"{mod} creates a file without claiming the name"
        assert code.count('"-y"') <= 1, f"{mod} has an unaccounted-for ffmpeg -y"
    # exactly one replace() in the whole program: the project JSON swap, whose
    # prior state is snapshotted first.
    code = (pathlib.Path(server.HERE) / "server.py").read_text()
    assert code.count(".replace(") == 1, "a second in-place replace appeared"
    assert "tmp.replace(path)" in code
    # and the claim is stated at that width everywhere a reader will meet it
    for doc in (server.__doc__,
                (pathlib.Path(server.HERE).parent / "SPEC.md").read_text(),
                (pathlib.Path(server.HERE).parent / "README.md").read_text()):
        flat = " ".join(doc.lower().split())
        assert "delete anything, anywhere, ever" not in flat, \
            "the un-keepable version of boundary 2 is back"


def test_a_failed_pass_leaves_its_own_wreckage_and_deletes_nothing():
    """A tool that crashes halfway leaves a partial file in derived/ and
    touches nothing of the director's. Cleaning that up would mean deleting,
    and deleting is the one thing this program does not do."""
    half = ("import sys\n"
            "open(sys.argv[2], 'ab').write(b'HALF A FILE')\n"
            "sys.exit(3)\n")
    with project() as (name, root), passes_dir(root, {"half.py": half}):
        src = synth(root / "src" / "x.mp4")
        before = src.read_bytes()
        server.edit_project(name, lambda p: p.update(
            {"media": [media_entry("m01", src)],
             "clips": [clip("c1", 0.0, "m01")]}))
        status, payload = server.run_pass(name, "c1", "half.py", [])
        assert status == 500, (status, payload)
        partial = root / name / "derived" / "x__half.mp4"
        assert partial.read_bytes() == b"HALF A FILE", "the wreckage was cleaned up"
        assert src.read_bytes() == before, "the source was touched"
        # and the clip still points at the original
        doc = json.loads(server.project_path(name).read_text())
        assert doc["clips"][0]["mid"] == "m01", doc["clips"]


def test_removing_a_clip_removes_no_file_and_no_media():
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4")
        server.add_media(name, [str(src)])
        server.edit_project(name, lambda p: p["clips"].append(clip("c1", 0.0, "m01")))
        status, payload = server.edit_project(name, lambda p: p["clips"].clear())
        assert status == 200, payload
        assert payload["clips"] == []
        assert payload["media"], "the media list was pruned"
        assert src.is_file(), "the file was deleted"


# ================================================== media enters only by hand

def test_nothing_is_indexed_and_nothing_is_scanned():
    """A file sitting inside the project's own directory is not media. There is
    no discovery of any kind: the media list starts empty and stays empty until
    a path is handed in."""
    with project() as (name, root):
        synth(root / name / "derived" / "lying_around.mp4")
        synth(root / name / "renders" / "old.mp4")
        doc = json.loads(server.project_path(name).read_text())
        assert doc["media"] == [], doc["media"]
        srv, port = start_server(name)
        try:
            status, raw, _ = request(port, "GET", "/project")
        finally:
            stop_server(srv)
        assert json.loads(raw)["project"]["media"] == []
        # and the source has no directory-walking machinery at all
        code = (pathlib.Path(server.HERE) / "server.py").read_text()
        for forbidden in ("os.walk", "iterdir(", "scandir(", "rglob("):
            assert forbidden not in code, f"server.py can spell {forbidden}"
        # Three globs, none of them over media: this project's own snapshots,
        # the project files in the root, and the operator's --passes-dir.
        assert code.count(".glob(") == 3, "a new glob appeared — check what it walks"


def test_add_media_takes_absolute_paths_and_nothing_else():
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4")
        status, payload = server.add_media(name, ["relative/x.mp4"])
        assert status == 400 and "absolute" in payload["problems"][0], payload
        status, payload = server.add_media(name, [str(root / "src" / "ghost.mp4")])
        assert status == 400 and "not a file" in payload["problems"][0], payload
        status, payload = server.add_media(name, [])
        assert status == 400, payload

        status, payload = server.add_media(name, [str(src)])
        assert status == 200, payload
        entry = payload["added"][0]
        assert entry["mid"] == "m01" and entry["path"] == str(src), entry
        assert abs(entry["dur"] - 1.0) < 0.1 and entry["w"] == 160, entry

        # adding the same file twice does not give one file two mids
        status, payload = server.add_media(name, [str(src)])
        assert status == 200 and payload["added"] == [], payload
        assert payload["already"] == ["m01"], payload
        doc = json.loads(server.project_path(name).read_text())
        assert len(doc["media"]) == 1, doc["media"]


def test_the_media_endpoint_adds_and_the_cli_adds_the_same_way():
    with project() as (name, root):
        a, b = synth(root / "src" / "a.mp4"), synth(root / "src" / "b.mp4")
        srv, port = start_server(name)
        try:
            status, payload = post(port, "/media", {"paths": [str(a)]})
            bad, _ = post(port, "/media", {"paths": "not a list"})
            empty, _ = post(port, "/media", {})
        finally:
            stop_server(srv)
        assert status == 200 and payload["added"][0]["mid"] == "m01", payload
        assert bad == 400 and empty == 400

        server.main(["add", name, str(b)])
        doc = json.loads(server.project_path(name).read_text())
        assert [m["path"] for m in doc["media"]] == [str(a), str(b)], doc["media"]


def test_media_is_served_by_mid_with_range_support():
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4")
        server.add_media(name, [str(src)])
        size = src.stat().st_size
        srv, port = start_server(name)
        try:
            whole, body, h = request(port, "GET", "/media/m01")
            part, chunk, ph = request(port, "GET", "/media/m01",
                                      headers={"Range": "bytes=0-99"})
            suffix, tail, sh = request(port, "GET", "/media/m01",
                                       headers={"Range": "bytes=-50"})
        finally:
            stop_server(srv)
        assert whole == 200 and len(body) == size, (whole, len(body), size)
        assert h["Accept-Ranges"] == "bytes"
        assert part == 206 and len(chunk) == 100, (part, len(chunk))
        assert ph["Content-Range"] == f"bytes 0-99/{size}", ph
        assert suffix == 206 and len(tail) == 50 and tail == src.read_bytes()[-50:]
        assert sh["Content-Range"] == f"bytes {size - 50}-{size - 1}/{size}", sh


def test_a_bad_range_is_a_416_not_a_dropped_connection():
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4")
        server.add_media(name, [str(src)])
        size = src.stat().st_size
        srv, port = start_server(name)
        try:
            bad, _, h1 = request(port, "GET", "/media/m01",
                                 headers={"Range": "bytes=abc-def"})
            past, _, h2 = request(port, "GET", "/media/m01",
                                  headers={"Range": "bytes=999999999-"})
            past2, _, h3 = request(port, "GET", "/media/m01",
                                   headers={"Range": "bytes=99999999-999999999"})
        finally:
            stop_server(srv)
        assert bad == 416 and h1["Content-Range"] == f"bytes */{size}", (bad, h1)
        assert past == 416 and h2["Content-Range"] == f"bytes */{size}", (past, h2)
        assert past2 == 416 and h3["Content-Range"] == f"bytes */{size}", (past2, h3)


def test_a_malformed_request_is_a_400_not_a_dropped_connection():
    with project() as (name, root):
        srv, port = start_server(name)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("PUT", "/project", body=b"not json",
                         headers={"Content-Length": "8"})
            resp = conn.getresponse()
            not_json = resp.status
            resp.read()
            conn.close()

            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(b"PUT /project HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            raw = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                raw += chunk
            s.close()

            not_object, _ = post(port, "/media", None)
            unknown, _ = post(port, "/nope")
        finally:
            stop_server(srv)
        assert not_json == 400, not_json
        assert raw and b"400" in raw.split(b"\r\n", 1)[0], raw[:80]
        assert not_object == 400, not_object
        assert unknown == 404, unknown
        assert json.loads(server.project_path(name).read_text())["version"] == 3


# ================================================ passes write derivatives only

def test_a_pass_writes_a_derivative_and_leaves_the_source_untouched():
    """The rewrite in one test. A pass reads the source, writes a NEW file into
    derived/, adds it to media and re-points the clip. There is no backup to
    keep and no _raw to audit, because the original is never opened for writing
    by anyone."""
    with project() as (name, root), passes_dir(root, {"negate.py": NEGATE}):
        src = synth(root / "src" / "x.mp4", dur=1.0)
        before, mtime = src.read_bytes(), src.stat().st_mtime_ns
        neighbours = sorted(p.name for p in src.parent.iterdir())
        server.edit_project(name, lambda p: p.update(
            {"media": [media_entry("m01", src)],
             "clips": [clip("c1", 0.0, "m01")]}))

        status, payload = server.run_pass(name, "c1", "negate.py", [])
        assert status == 200, payload

        out = root / name / "derived" / "x__negate.mp4"
        assert out.is_file(), sorted(p.name for p in (root / name / "derived").iterdir())
        assert payload["out"] == str(out), payload

        # the source: same bytes, same mtime, same neighbours
        assert src.read_bytes() == before, "the source was rewritten"
        assert src.stat().st_mtime_ns == mtime, "the source's mtime moved"
        assert sorted(p.name for p in src.parent.iterdir()) == neighbours, \
            "something appeared or vanished beside the source"

        doc = json.loads(server.project_path(name).read_text())
        assert [m["mid"] for m in doc["media"]] == ["m01", "m02"], doc["media"]
        assert doc["media"][1]["path"] == str(out)
        assert doc["clips"][0]["mid"] == "m02", "the clip was not re-pointed"
        # and the derivative is now servable, because it is on the list
        assert server.servable(doc, str(out)) is not None


def test_running_the_same_pass_twice_writes_a_second_file_not_over_the_first():
    with project() as (name, root), passes_dir(root, {"negate.py": NEGATE}):
        src = synth(root / "src" / "x.mp4", dur=1.0)
        server.edit_project(name, lambda p: p.update(
            {"media": [media_entry("m01", src)],
             "clips": [clip("c1", 0.0, "m01")]}))
        first = server.run_pass(name, "c1", "negate.py", [])[1]["out"]
        blob = pathlib.Path(first).read_bytes()
        # re-point back to the original and run it again
        server.edit_project(name, lambda p: p["clips"][0].update({"mid": "m01"}))
        second = server.run_pass(name, "c1", "negate.py", [])[1]["out"]
        assert first != second, first
        assert second.endswith("x__negate-2.mp4"), second
        assert pathlib.Path(first).read_bytes() == blob, "the first was overwritten"


def test_an_unknown_pass_is_refused_and_the_allowlist_is_the_passes_directory():
    with project() as (name, root), passes_dir(root, {"negate.py": NEGATE}) as pd:
        src = synth(root / "src" / "x.mp4")
        server.edit_project(name, lambda p: p.update(
            {"media": [media_entry("m01", src)], "clips": [clip("c1", 0.0, "m01")]}))
        status, payload = server.run_pass(name, "c1", "rm", ["-rf", "/"])
        assert status == 400, (status, payload)
        assert "is not a pass in" in payload["problems"][0], payload
        # and the refusal names what IS available, from the directory
        assert "negate.py" in payload["problems"][0], payload
        assert server.pass_names() == ["negate.py"], server.pass_names()
        srv, port = start_server(name)
        try:
            wrong, _ = post(port, "/pass", {"uid": "c1", "pass": 3})
            listed, raw, _ = request(port, "GET", "/project")
        finally:
            stop_server(srv)
        assert wrong == 400, wrong
        # the page is offered the server's passes, never the project's
        assert json.loads(raw)["passes"] == ["negate.py"], raw[:200]
        assert str(pd) not in json.loads(raw)["project"].get("passes", ""), \
            "a passes map is back in project data"


def test_a_pass_runs_in_the_projects_own_work_directory():
    """A tool that scratches into a RELATIVE directory must litter inside the
    project, not wherever the server happened to be started."""
    scratcher = ("import os, shutil, sys\n"
                 "os.makedirs('_scratch', exist_ok=True)\n"
                 "open('_scratch/marker.txt', 'x').write('here')\n"
                 "shutil.copyfile(sys.argv[1], sys.argv[2])\n")
    with project() as (name, root), passes_dir(root, {"scratch.py": scratcher}):
        src = synth(root / "src" / "x.mp4")
        server.edit_project(name, lambda p: p.update(
            {"media": [media_entry("m01", src)],
             "clips": [clip("c1", 0.0, "m01")]}))
        here = pathlib.Path.cwd() / "_scratch"
        assert not here.exists(), "fixture collision with the cwd"
        status, _ = server.run_pass(name, "c1", "scratch.py", [])
        assert status == 200
        assert (root / name / "work" / "_scratch" / "marker.txt").is_file()
        assert not here.exists(), "the pass scratched next to the server"


# ============================================================ OFFLINE and export

def test_a_missing_source_blocks_export_by_name_and_rewrites_nothing():
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4", dur=1.0)
        server.add_media(name, [str(src)])
        server.edit_project(name, lambda p: p["clips"].append(
            clip("c1", 0.0, "m01", out=0.5)))
        moved = root / "src" / "moved.mp4"
        shutil.copyfile(src, moved)
        os.replace(src, root / "src" / "away.mp4")     # the director moved it
        was = json.loads(server.project_path(name).read_text())

        srv, port = start_server(name)
        try:
            status, payload = post(port, "/render")
            listing, raw, _ = request(port, "GET", "/project")
        finally:
            stop_server(srv)
        assert status == 422, (status, payload)
        assert "c1" in payload["problems"][0] and "x.mp4" in payload["problems"][0]
        assert json.loads(server.project_path(name).read_text()) == was, \
            "the project was rewritten because a file was missing"
        assert json.loads(raw)["offline"] == [{"uid": "c1", "why": f"missing {src}"}]
        assert not list((root / name / "renders").glob("*.mp4")), \
            "a blocked export left a file"

        # the cut still saves, with the source still missing
        status, payload = server.edit_project(
            name, lambda p: p["clips"][0].update({"t": 0.5}))
        assert status == 200, payload


def test_export_writes_a_version_stamped_file_that_matches_the_timeline():
    with project() as (name, root):
        a = synth(root / "src" / "a.mp4", dur=1.0)
        b = synth(root / "src" / "b.mp4", dur=1.0)
        server.add_media(name, [str(a), str(b)])
        server.edit_project(name, lambda p: p.update(
            {"resolution": [160, 120],
             "clips": [clip("c1", 0.0, "m01", out=1.0),
                       clip("c2", 0.75, "m02", out=1.0)]}))   # 0.25s overlap
        doc = json.loads(server.project_path(name).read_text())
        status, payload = server.export(name)
        assert status == 200, payload
        assert payload["out"] == f"{name}_v{doc['version']:03d}.mp4", payload
        assert (root / name / "renders" / payload["out"]).is_file()
        # 1.0 + 1.0 - 0.25 = 1.75s = 42 frames, and the file must hold exactly that
        assert payload["seconds"] == 1.75, payload
        assert payload["frames"] == 42, payload

        # a second export of the same version lands beside the first
        status, second = server.export(name)
        assert status == 200 and second["out"].endswith("-2.mp4"), second
        assert (root / name / "renders" / payload["out"]).is_file()


def test_a_second_job_is_refused_rather_than_run_alongside_the_first():
    """Two ffmpeg processes writing at once is a corrupt render."""
    with project() as (name, root):
        srv, port = start_server(name)
        lock = server._job_lock(name)
        lock.acquire()
        try:
            status, payload = post(port, "/render")
        finally:
            lock.release()
            stop_server(srv)
        assert status == 409, (status, payload)
        assert "already running" in payload["problems"][0], payload


def test_an_unrenderable_cut_is_a_422_and_the_server_keeps_answering():
    with project() as (name, root):
        srv, port = start_server(name)
        try:
            status, payload = post(port, "/render")
            after, _ = post(port, "/history/nope")
        finally:
            stop_server(srv)
        assert status == 422, (status, payload)
        assert "no clips" in payload["problems"][0].lower(), payload
        assert after == 404, "the server must still be answering"


# ==================================================================== the page

def test_the_page_does_not_discard_an_edit_queued_behind_a_409():
    """Run against the real save loop lifted out of ui.html.

    Sequence: a drag saves; while that PUT is in flight a second drag queues
    (SAVE_AGAIN); the PUT comes back 409. Replacing the local document with the
    server's copy at that point deletes the queued drag, and the loop then saves
    the unchanged server copy and reports "saved" — the one thing this tool may
    never do.
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> save-loop")
    b = html.index("// <<< save-loop")
    region = html[a:b]
    assert "saveOnce" in region and "async function save()" in region, \
        "the save-loop markers no longer wrap the save loop"
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; the save loop is JS)")
        return

    harness = r"""
// --- the smallest DOM the save loop touches ---------------------------------
const STATUS = {textContent: '', style: {}, clicks: {},
  append(...xs) { for (const x of xs) {
      if (typeof x === 'string') this.textContent += x;
      else { this.textContent += x.textContent; this.clicks[x.textContent] = x.onclick; }
  } }};
const INSP = {innerHTML: ''};
const document = {getElementById: id => id === 'status' ? STATUS : INSP};
const el = (t, c) => ({tag: t, textContent: '', onclick: null});
let DOC = null, SEL = null, DRAWS = 0;
function draw() { DRAWS++; }
function inspect() {}
__REGION__
// --- the sequence ------------------------------------------------------------
DOC = {version: 3, clips: [{uid: 'c000', t: 0}]};
const SERVER = {version: 9, clips: [{uid: 'c000', t: 0}], who: 'agent'};
let calls = 0;
globalThis.fetch = async (url, opts) => {
  calls++;
  const sent = JSON.parse(opts.body);
  if (calls === 1) {
    DOC.clips[0].t = 5;      // the director drags again while the PUT is away
    save();                  // queues behind the in-flight save
    return {status: 409, ok: false, json: async () => SERVER};
  }
  return {status: 200, ok: true, json: async () => ({version: sent.version + 1})};
};
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };
await save();
if (DOC.clips[0].t !== 5) fail('the queued drag was discarded (t=' + DOC.clips[0].t + ')');
if (STATUS.textContent === 'saved') fail('reported "saved" for a save that lost an edit');
if (!/NOT SAVED/.test(STATUS.textContent)) fail('no conflict warning: ' + STATUS.textContent);
if (calls !== 1) fail('kept saving into an unresolved conflict (' + calls + ' PUTs)');
// and the way out: "keep mine" re-bases the local cut and saves it
STATUS.clicks['keep mine']();
await new Promise(r => setTimeout(r, 0));
if (calls !== 2) fail('keep mine did not save');
if (DOC.clips[0].t !== 5) fail('keep mine lost the edit');
if (DOC.version !== 10) fail('keep mine did not re-base onto the server version');
if (STATUS.textContent !== 'saved') fail('keep mine did not report the save');
console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "saveloop.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()


def test_the_page_says_so_when_a_drop_carries_no_path():
    """A browser is not allowed to hand a page a dropped file's absolute path,
    and cutroom may not go looking for it by name. The drop target must
    therefore FAIL LOUDLY and point at the two routes that do work, rather than
    doing nothing and looking broken."""
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    assert "pathsFromDrop" in html and "addNote('bad')" in html, "the drop target is gone"
    assert "did not hand over that file’s path" in html, "the loud failure is gone"
    assert "cutroom add " in html, "the fallback the director can actually use is gone"
    assert "/media/pick" in html and "Add media…" in html, "the picker is gone"
    assert "paste an absolute path" in html, "the universal fallback is gone"


def test_the_page_never_offers_to_delete_anything():
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    assert "method: 'DELETE'" not in html and 'method: "DELETE"' not in html
    assert "cutroom deletes nothing" in html


# ======================================================================== CLI

def test_the_cli_makes_a_project_and_refuses_to_stand_on_one():
    with tempfile.TemporaryDirectory() as d:
        old, server.ROOT = server.ROOT, pathlib.Path(d)
        try:
            server.main(["new", "film", "--fps", "24", "--res", "1080x1920"])
            doc = json.loads((pathlib.Path(d) / "film.json").read_text())
            # no "passes" key: the executable a pass runs comes from
            # --passes-dir at startup and never from project data
            assert doc == {"name": "film", "fps": 24, "resolution": [1080, 1920],
                           "version": 1, "media": [], "clips": []}, doc
            assert (pathlib.Path(d) / "film").is_dir()
            try:
                server.create("film")
                assert False, "creating over an existing project was allowed"
            except server.Refused as e:
                assert "already exists" in str(e), e
            assert json.loads((pathlib.Path(d) / "film.json").read_text()) == doc
        finally:
            server.ROOT = old


# ================================================ what the adversarial review found
# One test per finding, each written against the broken version first and seen
# to fail there. The order is the order of how much each one matters.

def test_a_hostile_pass_name_cannot_execute():
    """THE MOST IMPORTANT TEST IN THIS REPOSITORY.

    A pass runs an executable. If a name can be steered at one cutroom did not
    put there, then the tool whose entire reason for existing is that it cannot
    destroy footage runs `/bin/rm <the director's footage>`. That was a real
    reachable state: `passes` was a {name: absolute path} map in project data,
    and a PUT could write `{"nuke": "/bin/rm"}`.

    Five spellings of the escape, and a canary that would be gone if any of
    them ran anything.
    """
    with project() as (name, root), tempfile.TemporaryDirectory() as outside:
        src = synth(root / "src" / "x.mp4")
        canary = pathlib.Path(outside) / "IRREPLACEABLE.mp4"
        canary.write_bytes(b"226 CLIPS")
        server.edit_project(name, lambda p: p.update(
            {"media": [media_entry("m01", src)], "clips": [clip("c1", 0.0, "m01")]}))

        # a project file that still spells the old field, by hand. It is data,
        # and data never names an executable again.
        doc = json.loads(server.project_path(name).read_text())
        doc["passes"] = {"nuke": "/bin/rm", "rm": "/bin/rm"}
        server.project_path(name).write_text(json.dumps(doc))

        # 1..4: with a passes directory configured, every way of pointing out of it
        with passes_dir(root, {"negate.py": NEGATE}) as pd:
            (pd / "sneaky").symlink_to("/bin/rm")       # a link, not a copy
            hostile = ["/bin/rm", "../../bin/rm", "../rm", "rm", "nuke",
                       "sneaky", "./negate.py", "..", ".hidden",
                       "negate.py/../../../../bin/rm", "", "sub/negate.py"]
            for spelling in hostile:
                status, payload = server.run_pass(
                    name, "c1", spelling, ["-rf", str(canary)])
                assert status == 400, (spelling, status, payload)
                assert canary.read_bytes() == b"226 CLIPS", f"{spelling} RAN"
                assert src.is_file(), f"{spelling} reached the source"
            # the symlink is not even offered as a pass
            assert server.pass_names() == ["negate.py"], server.pass_names()
            # and the one real pass in the directory does run, so this test is
            # measuring the gate and not a dead code path
            assert server.run_pass(name, "c1", "negate.py", [])[0] == 200

        # 5: a bare, legitimate-looking name with NO --passes-dir at all
        assert server.PASSES_DIR is None
        status, payload = server.run_pass(name, "c1", "negate.py", [])
        assert status == 501, (status, payload)
        assert "--passes-dir" in payload["problems"][0], payload

        # and the same through the HTTP door, which is where a PUT would arrive
        with passes_dir(root, {"negate.py": NEGATE}):
            srv, port = start_server(name)
            try:
                got = [post(port, "/pass", {"uid": "c1", "pass": s,
                                            "args": ["-rf", str(canary)]})[0]
                       for s in hostile]
            finally:
                stop_server(srv)
        assert all(s == 400 for s in got), list(zip(hostile, got))
        assert canary.read_bytes() == b"226 CLIPS", "a hostile pass ran over HTTP"
        assert sorted(p.name for p in pathlib.Path(outside).iterdir()) \
            == ["IRREPLACEABLE.mp4"]


def test_a_put_cannot_add_media_and_the_forged_path_stays_unservable():
    """`media` is the allowlist servable() consults. A PUT that could append to
    it is a gate on a list the caller writes — put `/etc/passwd` on it and then
    GET it. So a PUT may not touch `media` AT ALL, and the proof is not that
    the write is rejected but that the forged path is still 404 afterwards."""
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4")
        server.add_media(name, [str(src)])
        secret = root / "src" / "not-added.mp4"
        synth(secret)
        before = json.loads(server.project_path(name).read_text())

        srv, port = start_server(name)
        try:
            status, raw, _ = request(port, "PUT", "/project", {
                "version": before["version"],
                "clips": [],
                "media": before["media"] + [
                    {"mid": "leak", "path": "/etc/passwd", "label": "x"},
                    {"mid": "m99", "path": str(secret), "label": "y"}],
                "passes": {"nuke": "/bin/rm"}})
            after = json.loads(raw)
            leaked = [request(port, "GET", r)[0]
                      for r in ("/media/leak", "/media/m99", "/thumb/leak")]
            still, ok_raw, _ = request(port, "GET", "/media/m01")
        finally:
            stop_server(srv)

        assert status == 200, after          # the CUT saved; the allowlist did not
        assert after["media"] == before["media"], after["media"]
        assert "passes" not in after, after
        stored = json.loads(server.project_path(name).read_text())
        assert stored["media"] == before["media"], stored["media"]
        assert "passes" not in stored, stored
        assert leaked == [404, 404, 404], leaked
        assert still == 200 and len(ok_raw) == src.stat().st_size
        # and the gate itself agrees, on the stored document
        assert server.servable(stored, "/etc/passwd") is None
        assert server.servable(stored, str(secret)) is None


def test_a_symlinked_renders_directory_cannot_serve_a_file_from_outside():
    """P1-3, the read-side twin of the write rule.

    `is_symlink()` asks about the LAST component only, and `is_file()` follows
    a symlinked parent without comment. Replace <project>/renders with a
    symlink and request a file through it: the final path is not itself a
    symlink, so a leaf-only check passes it and the file is served. The whole
    chain has to be resolved, or the check is decoration.
    """
    with project() as (name, root), tempfile.TemporaryDirectory() as elsewhere:
        elsewhere = pathlib.Path(elsewhere)
        (elsewhere / "passwd").write_bytes(b"root:x:0:0:")
        (elsewhere / "deep").mkdir()
        (elsewhere / "deep" / "inner.mp4").write_bytes(b"NOT YOURS")
        (root / name / "renders").symlink_to(elsewhere, target_is_directory=True)

        srv, port = start_server(name)
        try:
            got = [request(port, "GET", r)[0]
                   for r in ("/renders/passwd", "/renders/deep")]
        finally:
            stop_server(srv)
        assert got == [404, 404], got

    # and the legitimate case still works: a real file in a real renders/
    with project() as (name, root):
        real = server.mkdirs(root / name / "renders") / "out.mp4"
        real.write_bytes(b"A RENDER")
        # a symlink INSIDE renders pointing out is refused too — same rule,
        # applied to the last component rather than the parent
        (real.parent / "outward.mp4").symlink_to("/etc/passwd")
        srv, port = start_server(name)
        try:
            ok, body, _ = request(port, "GET", "/renders/out.mp4")
            out = request(port, "GET", "/renders/outward.mp4")[0]
        finally:
            stop_server(srv)
        assert (ok, body) == (200, b"A RENDER"), (ok, body)
        assert out == 404, out


def test_writable_is_re_verified_at_the_moment_of_the_write():
    """P1-4. writable() returns a path and the open happens later — so what it
    checked and what gets written are two different questions unless the check
    is repeated with the write. Here the checked directory is swapped for an
    outward symlink in between, which is the accident's own shape."""
    with project() as (name, root), tempfile.TemporaryDirectory() as elsewhere:
        elsewhere = pathlib.Path(elsewhere)
        (elsewhere / "precious.mp4").write_bytes(b"IRREPLACEABLE")
        derived = server.mkdirs(root / name / "derived")
        target = server.writable(derived / "precious.mp4")   # checked: inside

        # the swap: derived is now a symlink pointing out of the root
        (derived / "placeholder").mkdir()
        os.rename(derived, root / name / "derived-was")
        (root / name / "derived").symlink_to(elsewhere, target_is_directory=True)

        for write in (lambda: server.write_new(target, "x"),
                      lambda: server.claim(target)):
            try:
                write()
                assert False, "the write followed the swapped directory out"
            except server.Refused as e:
                assert "outside" in str(e), e
        assert (elsewhere / "precious.mp4").read_bytes() == b"IRREPLACEABLE"
        assert sorted(p.name for p in elsewhere.iterdir()) == ["precious.mp4"]

    # And the second belt, for the window the check cannot cover. writable() is
    # stubbed out here to stand in for a race it loses — the point is that
    # _open_new() does not rely on it alone: a symlink sitting at the
    # destination name is refused by the OPEN, with O_EXCL answering first and
    # O_NOFOLLOW behind it, and nothing is written through the link either way.
    with project() as (name, root), tempfile.TemporaryDirectory() as elsewhere:
        bait = pathlib.Path(elsewhere) / "precious.mp4"
        bait.write_bytes(b"IRREPLACEABLE")
        link = server.mkdirs(root / name / "derived") / "x.mp4"
        link.symlink_to(bait)
        real_writable, server.writable = server.writable, lambda p, **kw: p
        try:
            for write in (lambda: server.write_new(link, "OVERWRITTEN"),
                          lambda: server.claim(link)):
                try:
                    write()
                    assert False, "the open followed a symlink at the destination"
                except OSError as e:
                    assert e.errno in (errno.EEXIST, errno.ELOOP), e
        finally:
            server.writable = real_writable
        assert bait.read_bytes() == b"IRREPLACEABLE"
        assert link.is_symlink(), "the link itself was replaced"


def test_a_pass_that_finishes_after_the_clip_moved_does_not_take_it_back():
    """P1-5. A pass reads the clip, runs ffmpeg for minutes, then re-points the
    clip — and in between the director saved a different source onto it. The
    old code re-pointed anyway and said 200: a finishing pass silently eating
    an edit made while it ran. The derivative is still written and still added
    to media; what it may not do is decide the conflict by itself."""
    slow = ("import shutil, sys, time\n"
            "time.sleep(1.0)\n"
            "shutil.copyfile(sys.argv[1], sys.argv[2])\n")
    with project() as (name, root), passes_dir(root, {"slow.py": slow}):
        a = synth(root / "src" / "a.mp4")
        b = synth(root / "src" / "b.mp4")
        server.add_media(name, [str(a), str(b)])
        server.edit_project(name, lambda p: p["clips"].append(clip("c1", 0.0, "m01")))

        result = {}
        t = threading.Thread(
            target=lambda: result.update(
                zip(("status", "payload"), server.run_pass(name, "c1", "slow.py", []))))
        t.start()
        time.sleep(0.4)                       # the pass is running
        status, _ = server.edit_project(      # the director re-points the clip
            name, lambda p: p["clips"][0].update({"mid": "m02"}))
        assert status == 200
        t.join(timeout=60)

        assert result["status"] == 409, result
        payload = result["payload"]
        assert payload["was"] == "m01" and payload["now"] == "m02", payload
        assert "m01" in payload["problems"][0] and "m02" in payload["problems"][0], \
            payload["problems"]

        doc = json.loads(server.project_path(name).read_text())
        assert doc["clips"][0]["mid"] == "m02", "the pass overwrote the edit"
        # the work is not thrown away: it is on the list, addressable, on disk
        assert doc["media"][-1]["path"] == payload["out"], doc["media"]
        assert doc["media"][-1]["mid"] == payload["media"]["mid"]
        assert pathlib.Path(payload["out"]).is_file()
        assert server.servable(doc, payload["out"]) is not None

    # the same shape when the clip is deleted rather than re-pointed
    with project() as (name, root), passes_dir(root, {"slow.py": slow}):
        a = synth(root / "src" / "a.mp4")
        server.add_media(name, [str(a)])
        server.edit_project(name, lambda p: p["clips"].append(clip("c1", 0.0, "m01")))
        result = {}
        t = threading.Thread(
            target=lambda: result.update(
                zip(("status", "payload"), server.run_pass(name, "c1", "slow.py", []))))
        t.start()
        time.sleep(0.4)
        server.edit_project(name, lambda p: p["clips"].clear())
        t.join(timeout=60)
        assert result["status"] == 409, result
        assert result["payload"]["now"] is None, result["payload"]
        assert "removed" in result["payload"]["problems"][0], result["payload"]

    # and the page treats that 409 as "done, not re-pointed" rather than as a
    # failure — the derivative exists, and a page that discarded the response
    # would hide a file the director now owns
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("const r = await fetch('/pass'")
    region = html[a:a + 1400]
    assert "r.status === 409" in region and "adopt(b.project)" in region, \
        "the page still calls a pass conflict a failure and drops the project"


def test_two_processes_cannot_claim_the_same_derivative_name():
    """P1-6. The job lock is a threading.Lock: it serialises one server's
    threads and knows nothing about a second server on another port. Both ask
    for the next free name, both are told the same one, and the second ffmpeg
    truncates the first's output. Looking is not taking."""
    with project() as (name, root):
        derived = server.mkdirs(root / name / "derived")

        # the bug, in two lines: asking twice gives the same answer
        assert server.free_name(derived, "x__desat", ".mp4") \
            == server.free_name(derived, "x__desat", ".mp4")

        # claiming twice cannot
        first = server.claim_free(derived, "x__desat", ".mp4")
        second = server.claim_free(derived, "x__desat", ".mp4")
        assert first != second, first
        assert (first.name, second.name) == ("x__desat.mp4", "x__desat-2.mp4")

        # and across real processes, racing on purpose
        script = root / "racer.py"
        script.write_text(
            "import pathlib, sys, time\n"
            f"sys.path.insert(0, {str(server.HERE)!r})\n"
            "import server\n"
            f"server.ROOT = pathlib.Path({str(root)!r})\n"
            "time.sleep(float(sys.argv[1]) - time.time())\n"
            f"print(server.claim_free(pathlib.Path({str(derived)!r}), 'race', '.mp4'))\n")
        at = time.time() + 2.0
        procs = [subprocess.Popen([sys.executable, str(script), str(at)],
                                  stdout=subprocess.PIPE, text=True) for _ in range(4)]
        names = sorted(p.communicate()[0].strip() for p in procs)
        assert all(n for n in names), names
        assert len(set(names)) == 4, names
        assert all(pathlib.Path(n).is_file() for n in names), names


def test_a_range_that_names_nothing_is_malformed_not_the_whole_file():
    """P2-8. `Range: bytes=` and `bytes=-` were parsed into start=0, end=EOF and
    answered 206 with the entire file — a partial-content response to a request
    that named no range. RFC 7233 calls that malformed."""
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4")
        server.add_media(name, [str(src)])
        size = src.stat().st_size
        srv, port = start_server(name)
        try:
            got = {}
            for spelling in ("bytes=", "bytes=-", "bytes= - ", "bytes=-0",
                             "bytes=,", "bytes=0", "bytes=--5"):
                status, body, h = request(port, "GET", "/media/m01",
                                          headers={"Range": spelling})
                got[spelling] = (status, len(body), h.get("Content-Range"))
            # the real ones still work
            ok = request(port, "GET", "/media/m01",
                         headers={"Range": "bytes=0-9"})[0]
            tail = request(port, "GET", "/media/m01",
                           headers={"Range": "bytes=-5"})[0]
        finally:
            stop_server(srv)
        for spelling, (status, length, cr) in got.items():
            assert status == 416, (spelling, status, length)
            assert cr == f"bytes */{size}", (spelling, cr)
            assert length < size, (spelling, length)
        assert (ok, tail) == (206, 206), (ok, tail)


def test_a_malformed_project_body_is_a_400_not_a_crash():
    """P2-9. `{"clips": [{}]}` reached render.py, which indexed c["t"] and
    raised KeyError — an exception the handler had no branch for, so a
    malformed request became a dropped connection or a 500. "Is this a project
    at all" has to be asked before any rule that reads a field."""
    with project() as (name, root):
        was = server.project_path(name).read_bytes()
        srv, port = start_server(name)
        try:
            bodies = [
                {"version": 3, "clips": [{}]},
                {"version": 3, "clips": [{"uid": "c1"}]},
                {"version": 3, "clips": "not a list"},
                {"version": 3, "clips": [7]},
                {"version": 3, "clips": [], "fps": "twenty four"},
                {"version": 3, "clips": [], "resolution": [720]},
                {"version": 3, "clips": [dict(clip("c1", 0.0, "m01"), t="0")]},
                {"version": 3, "clips": [dict(clip("c1", 0.0, "m01"), out=None)]},
                {"version": 3, "clips": [dict(clip("c1", 0.0, "m01"), mid=7)]},
            ]
            got = []
            for body in bodies:
                status, raw, _ = request(port, "PUT", "/project", body)
                got.append((status, json.loads(raw or b"{}")))
            alive, _ = post(port, "/history/nope")
        finally:
            stop_server(srv)
        for (status, payload), body in zip(got, bodies):
            assert status == 400, (body, status, payload)
            assert payload["problems"], (body, payload)
        assert alive == 404, "the server stopped answering"
        assert server.project_path(name).read_bytes() == was, "a bad body was written"

        # the agent's door is the same door
        status, payload = server.edit_project(name, lambda p: {**p, "clips": [{}]})
        assert status == 400, (status, payload)


def test_the_renderer_cli_cannot_write_outside_the_projects_root():
    """The renderer is a program too. `-o` used to be handed straight to
    ffmpeg with no guard at all, which is a second way to name an output — and
    boundary 1 is the claim that there is no second way."""
    with project() as (name, root), tempfile.TemporaryDirectory() as elsewhere:
        src = synth(root / "src" / "x.mp4", dur=1.0)
        server.add_media(name, [str(src)])
        server.edit_project(name, lambda p: p.update(
            {"resolution": [160, 120], "clips": [clip("c1", 0.0, "m01", out=0.5)]}))
        elsewhere = pathlib.Path(elsewhere)
        (elsewhere / "master.mp4").write_bytes(b"IRREPLACEABLE")

        runner = root / "cli.py"
        runner.write_text(
            "import pathlib, sys\n"
            f"sys.path.insert(0, {str(server.HERE)!r})\n"
            "import server, render\n"
            f"server.ROOT = pathlib.Path({str(root)!r})\n"
            "sys.argv = ['render.py'] + sys.argv[1:]\n"
            "render.main()\n")

        def run(*args):
            return subprocess.run([sys.executable, str(runner),
                                   str(server.project_path(name)), *args],
                                  capture_output=True, text=True, timeout=600)

        for out in (str(elsewhere / "new.mp4"),          # outside the root
                    str(elsewhere / "master.mp4"),       # outside, and existing
                    str(root / ".." / "escape.mp4"),
                    "relative.mp4"):
            r = run("-o", out)
            assert r.returncode != 0, (out, r.stdout)
            assert "outside" in r.stderr or "is not an absolute path" in r.stderr, \
                (out, r.stderr)
        assert (elsewhere / "master.mp4").read_bytes() == b"IRREPLACEABLE"
        assert sorted(p.name for p in elsewhere.iterdir()) == ["master.mp4"]
        assert not (pathlib.Path.cwd() / "relative.mp4").exists()

        # inside the root it renders, and refuses to stand on its own output
        good = root / name / "renders" / "cli.mp4"
        r = run("-o", str(good))
        assert r.returncode == 0, r.stderr
        assert good.is_file() and good.stat().st_size > 0
        blob = good.read_bytes()
        r = run("-o", str(good))
        assert r.returncode != 0 and "already exists" in r.stderr, r.stderr
        assert good.read_bytes() == blob, "the second render overwrote the first"

        # and with no -o at all it picks a free name in the project's renders/
        r = run()
        assert r.returncode == 0, r.stderr
        assert (root / name / "renders" / f"{name}_cli.mp4").is_file(), r.stdout


def test_what_an_external_pass_script_actually_receives():
    """The pass contract, from the tool's side, because the tool is the part
    cutroom does not control and the part that can do damage.

    argv is [script, src, dst, *args] and nothing else; the cwd is the
    project's own work/ directory; dst exists already and is empty, because
    cutroom claimed the name before the tool was launched; src is the
    director's file, unchanged, and the tool is never given a way to name
    anything else.
    """
    reporter = ("import json, os, sys\n"
                "open('report.json', 'w').write(json.dumps({\n"
                "    'argv': sys.argv,\n"
                "    'cwd': os.getcwd(),\n"
                "    'dst_existed': os.path.exists(sys.argv[2]),\n"
                "    'dst_size': os.path.getsize(sys.argv[2]),\n"
                "    'src_bytes': os.path.getsize(sys.argv[1]),\n"
                "}))\n"
                "open(sys.argv[2], 'wb').write(open(sys.argv[1], 'rb').read())\n")
    with project() as (name, root), passes_dir(root, {"report.py": reporter}) as pd:
        src = synth(root / "src" / "x.mp4")
        before, mtime = src.read_bytes(), src.stat().st_mtime_ns
        server.add_media(name, [str(src)])
        server.edit_project(name, lambda p: p["clips"].append(clip("c1", 0.0, "m01")))

        status, payload = server.run_pass(name, "c1", "report.py", ["--strength", "0.5"])
        assert status == 200, payload

        work = root / name / "work"
        report = json.loads((work / "report.json").read_text())
        dst = root / name / "derived" / "x__report.mp4"
        assert report["argv"] == [str(pd / "report.py"), str(src), str(dst),
                                  "--strength", "0.5"], report["argv"]
        assert pathlib.Path(report["cwd"]).resolve() == work.resolve(), report["cwd"]
        assert report["dst_existed"] is True, "the name was not claimed first"
        assert report["dst_size"] == 0, "the claim was not empty"
        assert report["src_bytes"] == len(before)
        assert src.read_bytes() == before and src.stat().st_mtime_ns == mtime
        assert dst.read_bytes() == before, "the tool's output is not what landed"

        # no shell anywhere: an argument that would be a redirect in one is an
        # argument here, and the file it names is never created
        status, payload = server.run_pass(
            name, "c1", "report.py", [">", str(root / "shelled.txt"), "&&", "rm"])
        assert status in (200, 409), payload
        assert not (root / "shelled.txt").exists(), "the args went through a shell"
        report = json.loads((work / "report.json").read_text())
        assert report["argv"][3:] == [">", str(root / "shelled.txt"), "&&", "rm"]
        code = (pathlib.Path(server.HERE) / "server.py").read_text()
        assert "shell=True" not in code, "server.py can spell shell=True"


def test_copy_in_copies_and_never_touches_the_source():
    """`--copy` puts the COPY on the allowlist and leaves the original alone.

    This is the survival property, not the safety one: safety is that nothing
    can write to a source at all. What the copy buys is a cut that still
    renders after the folder it came from is gone — the failure that cost this
    project 226 clips.
    """
    with project() as (name, root):
        src = synth(root / "outside" / "take.mp4", dur=0.5)
        before, mtime = src.read_bytes(), src.stat().st_mtime_ns

        status, payload = server.copy_in(name, [str(src)])
        assert status == 200, payload
        added = payload["added"]
        assert len(added) == 1
        held = pathlib.Path(added[0]["path"])

        assert held != src, "the allowlist still points at the original"
        assert held.parent == server.project_dir(name) / "media", held
        assert held.read_bytes() == before, "the copy is not the file"
        assert src.read_bytes() == before and src.stat().st_mtime_ns == mtime, \
            "the source was written to"
        assert added[0].get("dur"), "the copy was not probed"

        # a second import does not replace the first — O_EXCL, same as a pass
        status, payload = server.copy_in(name, [str(src)])
        assert status == 200, payload
        second = pathlib.Path(payload["added"][0]["path"])
        assert second != held and second.exists() and held.read_bytes() == before

        # and the copy is servable, because it is genuinely on the allowlist
        doc = json.loads(server.project_path(name).read_text())
        assert server.servable(doc, str(held)) == held


def test_copy_in_refuses_what_add_media_refuses():
    with project() as (name, root):
        status, payload = server.copy_in(name, ["relative/path.mp4"])
        assert status == 400 and "absolute" in payload["problems"][0]
        status, payload = server.copy_in(name, [str(root / "nope.mp4")])
        assert status == 400 and "not a file" in payload["problems"][0]
        assert not (server.project_dir(name) / "media").exists(), \
            "a refused import still made the directory"


def test_the_razor_cuts_on_a_frame_and_loses_nothing():
    """Run the real razor out of ui.html.

    The two halves must cover exactly what the one covered — a razor that
    rounds the seam differently on each side leaves a gap or an overlap, and an
    overlap in this tool is a crossfade, which would mean cutting a clip in
    two silently dissolved it into itself.
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> razor")
    b = html.index("// <<< razor")
    region = html[a:b]
    assert "function razor()" in region, "the razor markers no longer wrap the razor"
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; the razor is JS)")
        return

    harness = r"""
const dur = c => (c.out - c.in) / c.rate;
const endOf = c => c.t + dur(c);
let SEL = null, DRAWS = 0, SAVES = 0, NOTES = [];
const newUid = () => 'u' + (++DRAWS);
function draw() {} function save() { SAVES++; } function inspect() {}
function note(m) { NOTES.push(m); }
let DOC = {fps: 24, clips: []}, CLOCK = 0;
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

// one clip, cut off the frame grid: the seam must land on a frame
DOC.clips = [{uid:'c0', mid:'m01', lane:0, t:0, in:0.5, out:2.5, rate:1.0,
              label:'a', note:''}];
CLOCK = 0.7719;
razor();
if (DOC.clips.length !== 2) fail('did not split (' + DOC.clips.length + ')');
const [L, R] = DOC.clips;
if (Math.abs(R.t * 24 - Math.round(R.t * 24)) > 1e-9) fail('seam is not on a frame');
if (Math.abs(endOf(L) - R.t) > 1e-9) fail('gap or overlap at the seam');
if (Math.abs(L.out - R.in) > 1e-9) fail('the source is not continuous across the cut');
if (Math.abs(dur(L) + dur(R) - 2.0) > 1e-9) fail('the halves do not cover the whole');
if (R.mid !== L.mid || R.lane !== L.lane) fail('the right half changed source or lane');
if (SEL !== R.uid) fail('the right half was not selected');
if (SAVES !== 1) fail('saved ' + SAVES + ' times');

// a retimed clip: the seam still lands where the playhead is
DOC.clips = [{uid:'c0', mid:'m01', lane:0, t:1.0, in:0, out:4.0, rate:2.0,
              label:'a', note:''}];
CLOCK = 1.5;
razor();
const [L2, R2] = DOC.clips;
if (Math.abs(R2.t - 1.5) > 1e-9) fail('retimed seam moved: ' + R2.t);
if (Math.abs(R2.in - 1.0) > 1e-9) fail('retimed source point wrong: ' + R2.in);
if (Math.abs(endOf(R2) - 3.0) > 1e-9) fail('retimed tail ends wrong: ' + endOf(R2));

// THE ONE THAT BITES: a slow retime, put through the SERVER'S OWN SNAPPING.
// render.snap_project() rounds t, in and out to the frame grid on every save.
// A seam picked at the playhead alone survives that at rate 1.0 and 2.0 and
// NOT at 0.5 — `in` rounds one way, `t` stays put, and the halves end up
// overlapping by a frame. An overlap here is a crossfade, so the clip would
// dissolve into itself with nothing on screen to say so.
const snapAll = () => DOC.clips.forEach(c => {
  for (const k of ['t','in','out']) c[k] = Math.round(c[k]*24)/24;
});
for (const rate of [1.0, 0.5, 1.5, 2.0, 0.25]) {
  for (const off of [1, 2, 3, 5, 7]) {            // frames in from the head
    DOC.clips = [{uid:'c0', mid:'m01', lane:0, t:0.5, in:0.25, out:2.25, rate,
                  label:'a', note:''}];
    CLOCK = 0.5 + off/24;
    razor();
    if (DOC.clips.length !== 2) continue;         // refused: nothing to check
    snapAll();                                    // what the server writes back
    const [a, b] = DOC.clips;
    if (Math.abs(endOf(a) - b.t) > 1e-9)
      fail('rate ' + rate + ' off ' + off + ': seam is ' +
           (endOf(a) - b.t).toFixed(6) + 's out after the server snapped it');
    if (Math.abs(a.out - b.in) > 1e-9)
      fail('rate ' + rate + ' off ' + off + ': source discontinuous after snap');
    if (dur(a) < 1e-9 || dur(b) < 1e-9)
      fail('rate ' + rate + ' off ' + off + ': made a zero-length half');
  }
}

// a rate with no frame-exact seam is REFUSED, not cut into an overlap
DOC.clips = [{uid:'c0', mid:'m01', lane:0, t:0, in:0, out:4.0, rate:0.93,
              label:'a', note:''}];
CLOCK = 1.0; SAVES = 0; NOTES = [];
razor();
if (DOC.clips.length !== 1) fail('cut at a rate with no exact seam');
if (SAVES !== 0) fail('saved a refused cut');
if (!/no frame-exact seam/.test(NOTES.join(' '))) fail('refusal was silent: ' + NOTES.join(' '));

// the playhead outside every clip cuts nothing and saves nothing
DOC.clips = [{uid:'c0', mid:'m01', lane:0, t:0, in:0, out:1, rate:1.0,
              label:'a', note:''}];
CLOCK = 5; SAVES = 0;
razor();
if (DOC.clips.length !== 1) fail('cut a clip the playhead is not inside');
if (SAVES !== 0) fail('saved for a cut that did not happen');

// exactly on a clip's own edge is not a cut either — it would make a zero clip
CLOCK = 0; razor();
CLOCK = 1; razor();
if (DOC.clips.length !== 1) fail('cut at an edge made an empty clip');
console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "razor.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()


def test_adopting_a_server_document_never_bins_a_local_edit():
    """The save loop already states the rule: take the version NUMBER, never
    the object graph. adopt() is the other door into the same hazard, and a
    copy-in of 45 masters is a whole second of window in which the director can
    drag, trim or cut while the request is away.
    """
    html = (pathlib.Path(server.HERE) / "ui.html").read_text()
    a = html.index("// >>> adopt")
    b = html.index("// <<< adopt")
    region = html[a:b]
    assert "function adopt" in region, "the adopt markers no longer wrap adopt()"
    node = shutil.which("node")
    if node is None:
        print("   (skipped: node is not installed; adopt is JS)")
        return

    harness = r"""
let DOC = null, MEDIA = [], OFFLINE = {};
function markOffline() {} function drawBin() {} function draw() {} function reselect() {}
__REGION__
const fail = m => { console.error('FAIL: ' + m); process.exit(1); };

// the director cuts a clip while a copy-in is in flight; the copy comes back
DOC = {version: 4, media: [{mid:'m01'}], clips: [{uid:'c0', t:0, in:0, out:1}]};
const mine = DOC.clips[0];
DOC.clips.push({uid:'c1', t:1, in:1, out:2});          // the razor, mid-flight
adopt({version: 5, media: [{mid:'m01'}, {mid:'m02'}],
       clips: [{uid:'c0', t:0, in:0, out:1}]});         // server's older copy

if (DOC.clips.length !== 2) fail('the edit made during the request was binned');
if (DOC.clips[0] !== mine) fail('clip objects were swapped out from under the handlers');
if (DOC.media.length !== 2) fail('the new media did not arrive');
if (MEDIA.length !== 2) fail('the bin was not rebuilt from the new media');
if (DOC.version !== 5) fail('the version was not taken (next save would 409)');
console.log('js ok');
"""
    with tempfile.TemporaryDirectory() as d:
        js = pathlib.Path(d) / "adopt.mjs"
        js.write_text(harness.replace("__REGION__", region))
        r = subprocess.run([node, str(js)], capture_output=True, text=True)
        assert r.returncode == 0, (r.stdout + r.stderr).strip()


if __name__ == "__main__":
    # An optional substring argument runs one test. Used to demonstrate a fix
    # FAILING FIRST against a patched copy of the module it fixes.
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    ran = 0
    for name_, fn in sorted(globals().items()):
        if name_.startswith("test_") and only in name_:
            fn()
            ran += 1
            print("ok", name_)
    assert ran, f"no test matched {only!r}"
    print("all ok")
