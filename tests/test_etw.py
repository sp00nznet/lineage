"""The ETW correlator. Field names and event shapes come from the live
Microsoft-Windows-Kernel-File manifest, read off a real machine -- including the
doubled ThreadId/IssuingThreadId templates that vary by Windows build.

Testable without administrator rights or a live trace, which is the point of
keeping the correlator a pure state machine.

Runs under pytest, or standalone: `python tests/test_etw.py`
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lineage.etw import (  # noqa: E402
    CREATE, CREATE_NEW_FILE, DELETE_PATH, KEYWORDS, NAME_CREATE, PROCESS_PROVIDER,
    PROCESS_START, WRITE_EV, Correlator, capture_command, correlate, field, parse_decoded,
)
from lineage.observe import READ, WRITE  # noqa: E402


def ev(eid, pid=100, ts=1.0, image=None, **data):
    return {"id": eid, "pid": pid, "ts": ts, "image": image, "data": data}


# --------------------------------------------------------------------------
def test_keywords_exclude_read():
    """Read volume is enormous and mostly noise; opens carry the name anyway."""
    assert KEYWORDS == 0x1690
    assert not (KEYWORDS & 0x100), "READ must stay off"
    for required in (0x10, 0x80, 0x200, 0x400, 0x1000):
        assert KEYWORDS & required


def test_field_aliases_span_both_template_versions():
    """The provider ships ThreadId on older builds and IssuingThreadId on newer,
    and both templates are present in one manifest. A consumer that knows only
    one name silently loses the field on the other build."""
    assert field({"IssuingThreadId": 7}, "thread_id") == 7
    assert field({"ThreadId": 9}, "thread_id") == 9
    assert field({}, "thread_id", "?") == "?"
    # DeletePath calls it FilePath, Create calls it FileName.
    assert field({"FilePath": "/x"}, "file_name") == "/x"
    assert field({"FileName": "/y"}, "file_name") == "/y"


# --------------------------------------------------------------------------
# The correlation problem
# --------------------------------------------------------------------------
def test_write_has_no_name_and_must_borrow_it_from_the_create():
    """The whole reason this module exists. A Write says only "object 0xAB"."""
    c = Correlator(host="WS")
    c.feed(ev(CREATE, pid=42, ts=1.0, FileObject="0xAB",
              FileName=r"C:\data\out.csv", IssuingThreadId=7))
    c.feed(ev(WRITE_EV, pid=42, ts=2.0, FileObject="0xAB", FileKey="0xK", IOSize=4096))

    out = c.flush()
    assert len(out) == 1
    assert out[0]["path"] == "c:/data/out.csv"
    assert out[0]["direction"] == WRITE
    assert c.coverage()["unresolved_total"] == 0


def test_open_without_a_write_is_a_read():
    c = Correlator()
    c.feed(ev(CREATE, FileObject="0xA", FileName=r"C:\data\in.csv"))
    out = c.flush()
    assert len(out) == 1 and out[0]["direction"] == READ


def test_create_new_file_counts_as_a_write_immediately():
    c = Correlator()
    c.feed(ev(CREATE_NEW_FILE, FileObject="0xA", FileName=r"C:\data\new.csv"))
    assert c.flush()[0]["direction"] == WRITE


def test_namecreate_is_the_second_chance_for_an_unseen_handle():
    """A handle opened before the trace started has no Create event. The
    FileKey map can still name it."""
    c = Correlator()
    c.feed(ev(NAME_CREATE, FileKey="0xK1", FileName=r"C:\data\pre-existing.log"))
    c.feed(ev(WRITE_EV, pid=9, FileObject="0xUNSEEN", FileKey="0xK1"))
    out = c.flush()
    assert len(out) == 1
    assert out[0]["path"] == "c:/data/pre-existing.log" and out[0]["direction"] == WRITE
    assert c.coverage()["unresolved_total"] == 0


def test_unnameable_writes_are_counted_not_dropped():
    """A tap that drops what it cannot name and reports the rest looks
    complete. State the gap instead."""
    c = Correlator()
    for i in range(5):
        c.feed(ev(WRITE_EV, pid=1, FileObject=f"0x{i}", FileKey=f"0xK{i}"))
    assert c.flush() == []
    cov = c.coverage()
    assert cov["unresolved"]["write_unnamed"] == 5
    assert cov["unresolved_total"] == 5
    assert "not a reason to treat the rest as complete" in cov["note"].lower()


def test_delete_and_rename_carry_their_own_path():
    c = Correlator()
    c.feed(ev(DELETE_PATH, pid=3, FileObject="0xA", FileKey="0xK",
              FilePath=r"C:\data\gone.csv", InfoClass=13))
    out = c.flush()
    assert len(out) == 1
    assert out[0]["path"] == "c:/data/gone.csv" and out[0]["direction"] == WRITE


def test_actor_prefers_the_image_then_falls_back_to_pid():
    c = Correlator()
    c.feed(ev(CREATE, pid=77, image=r"C:\legacy\load.exe",
              FileObject="0xA", FileName=r"C:\d\x"))
    c.feed(ev(WRITE_EV, pid=77, FileObject="0xA", FileKey="0xK"))
    c.feed(ev(WRITE_EV, pid=88, FileObject="0xB", FileKey="0xUNKNOWN"))
    assert c.flush()[0]["pid"] == 77

    c2 = Correlator()
    c2.feed(ev(NAME_CREATE, FileKey="0xK", FileName=r"C:\d\y"))
    c2.feed(ev(WRITE_EV, pid=88, FileKey="0xK", FileObject="0xZ"))
    assert c2.flush()[0]["actor"] == "pid:88"


# --------------------------------------------------------------------------
# Bounded state -- an agent that runs for months
# --------------------------------------------------------------------------
def test_handle_map_is_bounded_and_eviction_is_counted():
    """An unbounded dict keyed by every file object the machine ever opened is
    a memory leak with a schedule."""
    c = Correlator(max_handles=100)
    for i in range(250):
        c.feed(ev(CREATE, FileObject=f"0x{i}", FileName=rf"C:\d\f{i}.dat"))
    assert len(c.handles) == 100
    assert c.coverage()["handles_evicted"] == 150
    assert len(c.flush()) == 100


def test_key_map_is_bounded_too():
    c = Correlator(max_keys=50)
    for i in range(120):
        c.feed(ev(NAME_CREATE, FileKey=f"0xK{i}", FileName=rf"C:\d\f{i}"))
    assert len(c.keys) == 50 and c.coverage()["keys_evicted"] == 70


def test_flush_resets_so_a_long_run_does_not_re_emit():
    c = Correlator()
    c.feed(ev(CREATE, FileObject="0xA", FileName=r"C:\d\x"))
    assert len(c.flush()) == 1
    assert c.flush() == [], "flush must not re-emit the same handle forever"


def test_a_realistic_interleaved_stream():
    """Two processes, overlapping handles, one pre-existing, one delete."""
    c = Correlator(host="WS-0412")
    c.feed(ev(NAME_CREATE, ts=0.0, FileKey="0xK9", FileName=r"C:\logs\old.log"))
    c.feed(ev(CREATE, pid=10, ts=1.0, FileObject="0xA", FileName=r"C:\data\a.csv"))
    c.feed(ev(CREATE, pid=20, ts=1.1, FileObject="0xB", FileName=r"C:\data\b.csv"))
    c.feed(ev(WRITE_EV, pid=20, ts=1.2, FileObject="0xB", FileKey="0xKB"))
    c.feed(ev(WRITE_EV, pid=30, ts=1.3, FileObject="0xGONE", FileKey="0xK9"))
    c.feed(ev(DELETE_PATH, pid=10, ts=1.4, FileObject="0xA", FilePath=r"C:\data\tmp.$$$"))

    out = {(o["path"], o["direction"]) for o in c.flush()}
    assert ("c:/data/a.csv", READ) in out          # opened, never written
    assert ("c:/data/b.csv", WRITE) in out         # written
    assert ("c:/logs/old.log", WRITE) in out       # named via the key map
    assert ("c:/data/tmp.$$$", WRITE) in out       # delete
    assert c.coverage()["unresolved_total"] == 0


# --------------------------------------------------------------------------
def test_capture_command_is_bounded_and_circular():
    """An unbounded trace session fills the disk of the machine it was meant
    to observe."""
    cmd = capture_command("lineage-test", r"C:\tmp\t.etl", 30, max_mb=64)
    start = " ".join(cmd["start"])
    assert "Microsoft-Windows-Kernel-File" in start
    assert "0x1690" in start
    assert "-max 64" in start and "Circular" in start
    assert cmd["stop"] == ["logman", "stop", "lineage-test", "-ets"]
    # Actor names come from the process provider, joined to the same session.
    assert any(PROCESS_PROVIDER in a for a in cmd["add"])


def test_process_start_names_a_short_lived_actor():
    """The README's first blocker: a month of `pid:29428 -> pid:97780` edges
    is a month of noise. A process born mid-capture is in no snapshot; its
    ProcessStart event is the only thing that ever names it."""
    c = Correlator(images={4: "System"})
    c.feed({"provider": "process", "id": PROCESS_START, "pid": 900, "ts": 1.0,
            "data": {"ImageName": "c:/jobs/eom-close.exe"}})
    c.feed(ev(CREATE, pid=900, FileObject="0xA", FileName="c:/data/eom.csv"))
    c.feed(ev(WRITE_EV, pid=900, FileObject="0xA"))
    # pid reuse: a later process with the same pid is a different actor.
    c.feed({"provider": "process", "id": PROCESS_START, "pid": 900, "ts": 2.0,
            "data": {"ImageName": "c:/tools/other.exe"}})
    c.feed(ev(CREATE, pid=900, FileObject="0xB", FileName="c:/data/other.csv"))
    out = {o["path"]: o["actor"] for o in c.flush()}
    assert out["c:/data/eom.csv"] == "c:/jobs/eom-close.exe"
    assert out["c:/data/other.csv"] == "c:/tools/other.exe"
    assert c.events == 5


def test_decoded_lines_and_nt_image_names():
    """Both providers through the decoder's line format, and the image name
    goes through the same NT->DOS translation as file paths -- an actor named
    `/device/harddiskvolume3/...` would never match one named from a snapshot."""
    p = parse_decoded(r"P|1|900|133000000000000000|||\Device\HarddiskVolume3\jobs\x.exe" + "\r\n")
    f = parse_decoded(r"F|12|900|133000000000000000|0xA|0xK|\Device\HarddiskVolume3\d\a.csv")
    assert p["provider"] == "process" and p["pid"] == 900
    assert f["data"]["FileName"] == r"\Device\HarddiskVolume3\d\a.csv"
    assert parse_decoded("garbage") is None
    devices = {r"\device\harddiskvolume3": "c:"}
    out = correlate([p, f], "h", devices=devices).flush()
    assert out[0]["actor"] == "c:/jobs/x.exe"
    assert out[0]["path"] == "c:/d/a.csv"


def test_image_survives_from_create_to_flush():
    """The bug the first live run exposed: every actor came out as `pid:29428`.

    flush() happens long after the Create event, so the image name has to be
    carried on the handle record -- reading it off the event at flush time is
    reading an event that is long gone.
    """
    c = Correlator()
    c.feed(ev(CREATE, pid=29428, image=r"C:\Program Files\Unity\Unity.exe",
              FileObject="0xA", FileName=r"D:\projects\therealm\x.meta"))
    out = c.flush()
    assert out[0]["actor"] == r"C:\Program Files\Unity\Unity.exe"
    assert out[0]["actor"] != "pid:29428"


# --------------------------------------------------------------------------
def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _main()
