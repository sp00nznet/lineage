"""D4. Fixtures are real /proc/self/mountinfo lines from a live Proxmox 9.1 node
(pve-a) and its real `exportfs -s` output. Invented fixtures would have hidden
every bug that actually mattered here.

Runs under pytest, or standalone: `python tests/test_paths.py`
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lineage.observe import is_noise  # noqa: E402
from lineage.paths import (  # noqa: E402
    canonical_key, equivalence_keys, find_mount, parse_exports, parse_mountinfo,
    resolve, resolve_windows, same_artifact, to_dos_path,
)

# Real device names from this machine's QueryDosDevice.
DEVMAP = {
    r"\device\harddiskvolume5": "c:",
    r"\device\harddiskvolume10": "d:",
    r"\device\harddiskvolume1": "e:",
}

# --- real host mountinfo (pve-a) ---------------------------------------
HOST = parse_mountinfo("""
32 2 252:1 / / rw,relatime shared:1 - ext4 /dev/mapper/pve-root rw,errors=remount-ro
51 32 259:0 / /media/nvme rw,relatime shared:49 - ext4 /dev/nvme0n1 rw
55 32 0:40 / /beta rw,relatime shared:53 - zfs beta rw,xattr,noacl,casesensitive
57 32 0:41 / /gamma rw,relatime shared:55 - zfs gamma rw,xattr,noacl,casesensitive
61 55 0:44 / /beta/archive rw,relatime shared:59 - zfs beta/archive rw,xattr,noacl
63 55 0:43 / /beta/releases rw,relatime shared:61 - zfs beta/releases rw,xattr,noacl
1066 32 0:109 / /alpha ro,relatime shared:593 - zfs alpha ro,xattr,noacl,casesensitive
""")

# --- real container mount namespaces -------------------------------------
PLEX = parse_mountinfo("""
661 617 252:7 / / rw,relatime shared:377 - ext4 /dev/mapper/pve-vm--114--disk--0 rw,stripe=16
662 661 0:40 / /mnt/store rw,relatime shared:382 master:53 - zfs beta rw,xattr,noacl
""")
RUTORRENT = parse_mountinfo("""
887 833 252:8 / / rw,relatime shared:468 - ext4 /dev/mapper/pve-vm--116--disk--0 rw,stripe=16
888 887 0:40 / /mnt/store rw,relatime shared:469 master:53 - zfs beta rw,xattr,noacl
""")
GITLAB = parse_mountinfo("""
1177 1133 7:0 / / rw,relatime shared:592 - ext4 /dev/loop0 rw
1178 1177 0:43 / /mnt/releases rw,relatime shared:595 master:61 - zfs beta/releases rw,xattr,noacl
""")

# A remote client of pve-a's /beta export -- the servarr shape.
SERVARR = parse_mountinfo("""
25 1 0:24 / / rw,relatime shared:1 - ext4 /dev/mapper/pve-vm--123--disk--0 rw
310 25 0:57 / /mnt/store rw,relatime shared:170 - nfs4 192.0.2.23:/beta rw,vers=4.2
""")

EXPORTS = parse_exports("""
/beta  100.69.21.63(sync,wdelay,hide,no_subtree_check,sec=sys,ro,secure,root_squash)
/alpha  192.0.2.0/24(sync,wdelay,hide,no_subtree_check,sec=sys,ro,secure)
/beta  192.0.2.0/24(sync,wdelay,hide,no_subtree_check,sec=sys,rw,secure)
/gamma  192.0.2.0/24(sync,wdelay,hide,no_subtree_check,sec=sys,rw,secure)
/beta/releases  192.0.2.0/24(sync,wdelay,hide,no_subtree_check,sec=sys,ro,secure)
""")
EXPORTS_BY_HOST = {"pve-a": EXPORTS}


def h(p):
    return resolve(p, HOST, "pve-a")


# ==========================================================================
def test_mountinfo_parsing():
    assert len(HOST) == 7
    beta = find_mount("/beta/movies/x.mkv", HOST)
    assert beta.dev == "0:40" and beta.fstype == "zfs" and beta.source == "beta"
    # Longest prefix wins: a nested dataset must beat its parent.
    rel = find_mount("/beta/releases/app.tar", HOST)
    assert rel.dev == "0:43", rel


def test_octal_escapes_in_mountpoints():
    m = parse_mountinfo(
        "99 32 0:99 / /mnt/my\\040share rw - cifs //fs01/my\\040share rw")[0]
    assert m.mountpoint == "/mnt/my share"


# --------------------------------------------------------------------------
# The three failure modes, all verified against the live estate
# --------------------------------------------------------------------------
def test_same_string_same_file():
    """plex and rutorrent both bind the `beta` dataset at /mnt/store.

    Path matching happens to be right here -- and the resolver has to agree,
    or it would break a link that genuinely exists.
    """
    a = resolve("/mnt/store/movies/x.mkv", PLEX, "pve-a", container="lxc:114")
    b = resolve("/mnt/store/movies/x.mkv", RUTORRENT, "pve-a", container="lxc:116")
    assert a.key() == b.key() == ("dev://pve-a/0:40", "/movies/x.mkv")
    assert same_artifact(a, b)


def test_different_string_same_file():
    """gitlab:/mnt/releases/app.tar IS host:/beta/releases/app.tar.

    Different strings, same bytes, via a bind of a nested ZFS dataset. Pure
    path matching misses this completely -- and it is a real dependency.
    """
    inside = resolve("/mnt/releases/app.tar", GITLAB, "pve-a", container="lxc:111")
    outside = h("/beta/releases/app.tar")
    assert inside.key() == outside.key() == ("dev://pve-a/0:43", "/app.tar")
    assert same_artifact(inside, outside)


def test_same_string_different_file():
    """Two containers binding different sources at the same mountpoint.

    Fabricating this link is worse than missing one: it makes the scream test
    cry wolf until people stop reading it.
    """
    gamma_ct = parse_mountinfo(
        "10 9 0:41 / /mnt/store rw - zfs gamma rw")
    alpha_ct = parse_mountinfo(
        "10 9 0:109 / /mnt/store rw - zfs alpha rw")
    a = resolve("/mnt/store/data.csv", gamma_ct, "pve-a", container="lxc:200")
    b = resolve("/mnt/store/data.csv", alpha_ct, "pve-a", container="lxc:201")
    assert a.key() != b.key()
    assert not same_artifact(a, b)


def test_nested_mount_not_propagated_into_a_container():
    """/beta/releases is its own dataset on the host, and is NOT in plex's
    namespace. So plex:/mnt/store/releases/x is the (empty) underlying
    directory of `beta`, not the releases dataset -- and must not link to it.

    Subtle, correct, and it falls out of the device+root model for free.
    """
    in_plex = resolve("/mnt/store/releases/app.tar", PLEX, "pve-a", container="lxc:114")
    real = h("/beta/releases/app.tar")
    assert in_plex.key() == ("dev://pve-a/0:40", "/releases/app.tar")
    assert real.key() == ("dev://pve-a/0:43", "/app.tar")
    assert not same_artifact(in_plex, real)


# --------------------------------------------------------------------------
# The cross-host bridge
# --------------------------------------------------------------------------
def test_nfs_client_meets_the_server_that_exports_it():
    """The join that no single machine could make.

    servarr (on the other node) reads /mnt/store/x; pve-a wrote /beta/x.
    Same bytes, different hosts, different paths, and the only thing linking
    them is that pve-a exports /beta.
    """
    client = resolve("/mnt/store/incoming/f.mkv", SERVARR, "pve-b", container="lxc:123")
    assert client.key() == ("nfs://192.0.2.23", "/beta/incoming/f.mkv")

    server = h("/beta/incoming/f.mkv")
    assert server.key() == ("dev://pve-a/0:40", "/incoming/f.mkv")

    # Without the export table they look unrelated.
    assert not same_artifact(client, server)

    # With it, the server's local path gains a key in its own nfs:// namespace.
    keys = equivalence_keys(server, EXPORTS)
    assert ("nfs://pve-a", "/beta/incoming/f.mkv") in keys

    # But it still does not match, because the client named the server by IP
    # and the server calls itself "pve-a". Host-identity reconciliation is a
    # separate problem; asserting the limitation keeps it from being forgotten.
    assert not same_artifact(client, server, {"pve-a": EXPORTS})


def test_export_bridge_uses_the_servers_own_name():
    """The client names the server by IP, the server knows itself by hostname.

    They only unify if both sides agree on the identifier -- which is a real
    limitation, recorded here rather than papered over. Host-identity
    reconciliation (IP <-> hostname <-> FQDN) is its own problem.
    """
    server = resolve("/beta/x", HOST, "192.0.2.23")
    client = resolve("/mnt/store/x", SERVARR, "pve-b")
    assert same_artifact(client, server, {"192.0.2.23": EXPORTS})


def test_paths_outside_any_export_do_not_bridge():
    ref = h("/media/nvme/scratch/tmp.bin")
    assert equivalence_keys(ref, EXPORTS) == {("dev://pve-a/259:0", "/scratch/tmp.bin")}


def test_exports_parsing():
    assert EXPORTS == {"/beta", "/alpha", "/gamma", "/beta/releases"}
    assert parse_exports('"/srv/my share" 10.0.0.0/8(rw)\n# note\n') == {"/srv/my share"}


def test_canonical_key_prefers_the_portable_form():
    """Grouping must use the identity another machine could recognise."""
    assert canonical_key(h("/beta/x"), EXPORTS_BY_HOST) == ("nfs://pve-a", "/beta/x")
    assert canonical_key(h("/media/nvme/x"), EXPORTS_BY_HOST)[0].startswith("dev://")


# --------------------------------------------------------------------------
def test_root_filesystem_and_edge_cases():
    assert h("/etc/fstab").key() == ("dev://pve-a/252:1", "/etc/fstab")
    assert h("/beta").key() == ("dev://pve-a/0:40", "/")
    assert resolve("/anything", [], "h").volume.startswith("unknown://")


def test_windows_unc_is_comparable_with_a_linux_cifs_mount():
    win = resolve_windows(r"\\fs01\data\reports\q3.xlsx", "WS-0412")
    assert win.key() == ("smb://fs01/data", "/reports/q3.xlsx")
    lin = resolve(
        "/mnt/data/reports/q3.xlsx",
        parse_mountinfo("40 25 0:60 / /mnt/data rw - cifs //fs01/data rw"),
        "srv-04")
    assert same_artifact(win, lin)


def test_windows_drive_letters_are_host_scoped():
    a = resolve_windows(r"D:\data\x.csv", "WS-1")
    b = resolve_windows(r"D:\data\x.csv", "WS-2")
    assert a.key() != b.key(), "D: on two machines is not the same volume"
    assert a.key() == ("dev://WS-1/d:", "/data/x.csv")
    # A volume GUID, when known, is machine-independent.
    g = resolve_windows(r"D:\data\x.csv", "WS-1", {"d": "abc-123"})
    assert g.key() == ("vol://abc-123", "/data/x.csv")


def test_nt_device_paths_translate_to_drive_letters():
    """ETW never says D:\\x — it says \\Device\\HarddiskVolume10\\x.

    Found on the first live capture: every path came back in NT form, so the
    noise filter never fired and nothing could ever match a path from another
    source.
    """
    assert to_dos_path(r"\Device\HarddiskVolume10\projects\x.rs", DEVMAP) == "d:/projects/x.rs"
    assert to_dos_path(r"\Device\HarddiskVolume5\Windows\System32\ucrtbase.dll",
                       DEVMAP) == "c:/Windows/System32/ucrtbase.dll"
    # Longest match first, or HarddiskVolume1 shadows HarddiskVolume10.
    assert to_dos_path(r"\Device\HarddiskVolume1\a", DEVMAP) == "e:/a"
    # An unmapped device keeps its NT form. Volumes with no drive letter are
    # real, and inventing one would silently merge two different volumes.
    assert to_dos_path(r"\Device\HarddiskVolume99\x", DEVMAP) == "/Device/HarddiskVolume99/x"
    # Already-DOS paths pass through.
    assert to_dos_path(r"C:\data\x.csv", DEVMAP) == "c:/data/x.csv"


def test_translated_nt_paths_finally_hit_the_noise_filter():
    """The concrete consequence of the bug above."""
    raw = r"\Device\HarddiskVolume5\Windows\System32\ucrtbase.dll"
    assert not is_noise(raw), "NT form slips past a filter written for c:/windows/"
    assert is_noise(to_dos_path(raw, DEVMAP)), "translated, it is correctly noise"


def test_mapped_network_drives_resolve_to_the_server_not_a_drive_letter():
    """Real device strings from this machine.

    `Y:` here is `\\Device\\LanmanRedirector\\;Y:…\\192.0.2.23\\beta` — the same
    ZFS dataset observed directly on that server. Filing it under
    `dev://thisbox/y:` would make the cross-machine link permanently unformable.
    """
    from lineage.paths import network_target
    assert network_target(r"\Device\LanmanRedirector\;Y:000000000022fe0f\192.0.2.23\beta") \
        == "//192.0.2.23/beta"
    assert network_target(r"\Device\Mup\fs01\data") == "//fs01/data"
    assert network_target(r"\Device\HarddiskVolume5") is None

    netmap = {r"\device\lanmanredirector\;y:000000000022fe0f\192.0.2.23\beta":
              "//192.0.2.23/beta"}
    dos = to_dos_path(r"\Device\LanmanRedirector\;Y:000000000022fe0f\192.0.2.23\beta\incoming\f.mkv",
                      netmap)
    assert dos == "//192.0.2.23/beta/incoming/f.mkv"
    ref = resolve_windows(dos, "WS-0412")
    assert ref.key() == ("smb://192.0.2.23/beta", "/incoming/f.mkv")


def test_a_windows_write_can_reach_the_linux_side_of_the_same_dataset():
    """The payoff: Y:\\incoming\\f.mkv on Windows and /beta/incoming/f.mkv on
    pve-a are the same bytes, once host aliases collapse IP to hostname."""
    from lineage.graph import canonical_volume
    aliases = {"192.0.2.23": "pve-a"}
    win = resolve_windows("//192.0.2.23/beta/incoming/f.mkv", "WS-0412")
    assert canonical_volume(win.volume, aliases) == "smb://pve-a/beta"
    # The Linux side of the same dataset, bridged through its NFS export.
    lin = resolve("/beta/incoming/f.mkv", HOST, "pve-a")
    assert ("nfs://pve-a", "/beta/incoming/f.mkv") in equivalence_keys(lin, EXPORTS)
    # Still two namespaces (smb:// vs nfs://). Uniting them needs the server's
    # share table alongside its export table — recorded, not yet built.


def test_translated_nt_path_resolves_like_any_windows_path():
    ref = resolve_windows(to_dos_path(r"\Device\HarddiskVolume10\projects\out.csv", DEVMAP),
                          "WS-0412")
    assert ref.key() == ("dev://WS-0412/d:", "/projects/out.csv")


# --------------------------------------------------------------------------
def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _main()
