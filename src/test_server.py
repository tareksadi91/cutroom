#!/usr/bin/env python3
"""Checks for server.py — the boundaries, the version guard and the snapshots.

    python3 test_server.py

Bare asserts, no pytest. Every test points server.ROOT at its own temporary
directory and synthesises its own fixtures. NO TEST EVER ADDRESSES REAL
FOOTAGE, and nothing here writes outside the directory it made.
"""
import contextlib
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
        dragged = clip("c000", 0.3007, "m01", **{"in": 0.01})
        dragged["out"] = 0.4208333333
        status, payload = server.write_project(
            name, {"version": 3, "media": [media_entry("m01", src, dur=2.0)],
                   "clips": [dragged]})
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
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4")
        tool = root / "tool.py"
        tool.write_text("import shutil,sys; shutil.copyfile(sys.argv[1], sys.argv[2])\n")
        # Written by hand, because a clip naming media the project does not have
        # cannot be SAVED — validate() refuses it. The pass has to refuse it too.
        doc = json.loads(server.project_path(name).read_text())
        doc.update({"passes": {"copy": str(tool)},
                    "media": [media_entry("m01", src)],
                    "clips": [clip("c1", 0.0, "m99")]})
        server.project_path(name).write_text(json.dumps(doc))
        status, payload = server.run_pass(name, "c1", "copy", [])
        assert status == 404, (status, payload)
        assert "m99" in payload["problems"][0], payload

        # and a mid that IS in media but whose path has been taken off the list
        # is refused by servable(), not by the file system
        doc["media"] = []
        doc["clips"] = [clip("c1", 0.0, "m01")]
        server.project_path(name).write_text(json.dumps(doc))
        status, payload = server.run_pass(name, "c1", "copy", [])
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
    with project() as (name, root), tempfile.TemporaryDirectory() as elsewhere:
        src = synth(root / "src" / "x.mp4")
        (root / name / "derived").symlink_to(elsewhere, target_is_directory=True)
        tool = root / "tool.py"
        tool.write_text("import shutil,sys; shutil.copyfile(sys.argv[1], sys.argv[2])\n")
        server.edit_project(name, lambda p: p.update(
            {"passes": {"copy": str(tool)},
             "media": [media_entry("m01", src)],
             "clips": [clip("c1", 0.0, "m01")]}))
        try:
            server.run_pass(name, "c1", "copy", [])
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
    """BOUNDARY 2, enforced by reading the program.

    Every other boundary can be tested behaviourally; this one is best proved
    by the absence of the call. A deletion cutroom cannot spell is a deletion
    no future edit can reach by accident.
    """
    for mod in ("server.py", "render.py"):
        src = (pathlib.Path(server.HERE) / mod).read_text()
        # strip comments and docstrings so the words may be DISCUSSED but the
        # calls may not be made
        code = "\n".join(line.split("#")[0] for line in src.splitlines())
        for forbidden in ("unlink(", "rmtree(", "os.remove(", "os.rmdir(",
                          "shutil.move(", "os.truncate(", "rename("):
            assert forbidden not in code, f"{mod} can spell {forbidden}"
        # The only mode this program ever opens a file in is "rb" — plus "a+"
        # on the advisory lock sidecar, which is opened and never written.
        modes = sorted(set(re.findall(r"open\([^)]*?[\"']([rwax]b?\+?)[\"']", code)))
        assert modes in ([], ["rb"], ["a+", "rb"]), (mod, modes)
        assert '"-y"' not in code, f"{mod} passes ffmpeg -y and could overwrite"
    # exactly one replace() in the whole program: the project JSON swap, whose
    # prior state is snapshotted first.
    code = (pathlib.Path(server.HERE) / "server.py").read_text()
    assert code.count(".replace(") == 1, "a second in-place replace appeared"
    assert "tmp.replace(path)" in code


def test_a_failed_pass_leaves_its_own_wreckage_and_deletes_nothing():
    """A tool that crashes halfway leaves a partial file in derived/ and
    touches nothing of the director's. Cleaning that up would mean deleting,
    and deleting is the one thing this program does not do."""
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4")
        before = src.read_bytes()
        tool = root / "half.py"
        tool.write_text("import sys\n"
                        "open(sys.argv[2], 'ab').write(b'HALF A FILE')\n"
                        "sys.exit(3)\n")
        server.edit_project(name, lambda p: p.update(
            {"passes": {"half": str(tool)},
             "media": [media_entry("m01", src)],
             "clips": [clip("c1", 0.0, "m01")]}))
        status, payload = server.run_pass(name, "c1", "half", [])
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
        # the one glob is over cutroom's own snapshots, never over media
        assert code.count(".glob(") == 2, "a new glob appeared — check what it walks"


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

def _copy_tool(root):
    """A pass: reads argv[1], writes argv[2]. Exactly the documented contract."""
    tool = root / "tools" / "double.py"
    tool.parent.mkdir(parents=True, exist_ok=True)
    tool.write_text(
        "import subprocess, sys\n"
        "subprocess.run(['ffmpeg', '-v', 'error', '-n', '-i', sys.argv[1],\n"
        "                '-vf', 'negate', '-c:v', 'libx264', '-crf', '20',\n"
        "                '-pix_fmt', 'yuv420p', sys.argv[2]], check=True)\n")
    return tool


def test_a_pass_writes_a_derivative_and_leaves_the_source_untouched():
    """The rewrite in one test. A pass reads the source, writes a NEW file into
    derived/, adds it to media and re-points the clip. There is no backup to
    keep and no _raw to audit, because the original is never opened for writing
    by anyone."""
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4", dur=1.0)
        before, mtime = src.read_bytes(), src.stat().st_mtime_ns
        neighbours = sorted(p.name for p in src.parent.iterdir())
        tool = _copy_tool(root)
        server.edit_project(name, lambda p: p.update(
            {"passes": {"negate": str(tool)},
             "media": [media_entry("m01", src)],
             "clips": [clip("c1", 0.0, "m01")]}))

        status, payload = server.run_pass(name, "c1", "negate", [])
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
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4", dur=1.0)
        tool = _copy_tool(root)
        server.edit_project(name, lambda p: p.update(
            {"passes": {"negate": str(tool)},
             "media": [media_entry("m01", src)],
             "clips": [clip("c1", 0.0, "m01")]}))
        first = server.run_pass(name, "c1", "negate", [])[1]["out"]
        blob = pathlib.Path(first).read_bytes()
        # re-point back to the original and run it again
        server.edit_project(name, lambda p: p["clips"][0].update({"mid": "m01"}))
        second = server.run_pass(name, "c1", "negate", [])[1]["out"]
        assert first != second, first
        assert second.endswith("x__negate-2.mp4"), second
        assert pathlib.Path(first).read_bytes() == blob, "the first was overwritten"


def test_an_unknown_pass_is_refused_and_the_allowlist_is_the_project_file():
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4")
        server.edit_project(name, lambda p: p.update(
            {"media": [media_entry("m01", src)], "clips": [clip("c1", 0.0, "m01")]}))
        status, payload = server.run_pass(name, "c1", "rm", ["-rf", "/"])
        assert status == 400, (status, payload)
        assert "not one of this project's passes" in payload["problems"][0], payload
        srv, port = start_server(name)
        try:
            wrong, _ = post(port, "/pass", {"uid": "c1", "pass": 3})
        finally:
            stop_server(srv)
        assert wrong == 400, wrong


def test_a_pass_runs_in_the_projects_own_work_directory():
    """A tool that scratches into a RELATIVE directory must litter inside the
    project, not wherever the server happened to be started."""
    with project() as (name, root):
        src = synth(root / "src" / "x.mp4")
        tool = root / "scratcher.py"
        tool.write_text(
            "import os, shutil, sys\n"
            "os.makedirs('_scratch', exist_ok=True)\n"
            "open('_scratch/marker.txt', 'x').write('here')\n"
            "shutil.copyfile(sys.argv[1], sys.argv[2])\n")
        server.edit_project(name, lambda p: p.update(
            {"passes": {"scratch": str(tool)},
             "media": [media_entry("m01", src)],
             "clips": [clip("c1", 0.0, "m01")]}))
        here = pathlib.Path.cwd() / "_scratch"
        assert not here.exists(), "fixture collision with the cwd"
        status, _ = server.run_pass(name, "c1", "scratch", [])
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
            assert doc == {"name": "film", "fps": 24, "resolution": [1080, 1920],
                           "version": 1, "media": [], "clips": [], "passes": {}}, doc
            assert (pathlib.Path(d) / "film").is_dir()
            try:
                server.create("film")
                assert False, "creating over an existing project was allowed"
            except server.Refused as e:
                assert "already exists" in str(e), e
            assert json.loads((pathlib.Path(d) / "film.json").read_text()) == doc
        finally:
            server.ROOT = old


if __name__ == "__main__":
    for name_, fn in sorted(globals().items()):
        if name_.startswith("test_"):
            fn()
            print("ok", name_)
    print("all ok")
