"""lineage.paths — D4: when are two paths the same bytes?

The make-or-break piece. Everything cross-guest and cross-host depends on it,
and both ways of getting it wrong are live in a real estate:

- **Same string, same file.** `plex:/mnt/store/x` and `rutorrent:/mnt/store/x`
  are both binds of the same ZFS dataset. Path matching is right here by luck.
- **Different string, same file.** `gitlab:/mnt/releases/x` and the host's
  `/beta/releases/x` are the same bytes. Path matching misses it entirely.
- **Same string, different file.** Two containers each binding a *different*
  source at `/mnt/store`. Path matching invents a dependency.

So a path string tells you nothing in either direction, and the resolver is not
an optimisation -- it is the thing that decides whether the graph is real.

## The model

An artifact's identity is `(volume, rel)`:

- **Local filesystem** -> `dev://<host>/<major:minor>` plus the path *within*
  that filesystem. Device numbers are global to a kernel, so every container on
  a Proxmox node resolves a shared dataset to the same volume automatically --
  bind mounts, nested datasets and all. That falls out of `mountinfo`'s `root`
  field and needs no per-container configuration.
- **Network filesystem** -> `nfs://<server>` or `smb://<server>/<share>` plus
  the path *as the server sees it*. This is what crosses machines.

Two artifacts are the same when any equivalence key matches. A local path that
lives under an NFS export gets a second key in the server's `nfs://` namespace,
which is the bridge that lets "the server wrote it" meet "the client read it".

Fixtures in the tests are real `/proc/self/mountinfo` lines from a live
Proxmox 9.1 node, not invented ones.
"""
from __future__ import annotations

import re

NETWORK_FS = {
    "nfs": "nfs", "nfs4": "nfs", "nfsd": "nfs",
    "cifs": "smb", "smb3": "smb", "smbfs": "smb",
    "fuse.sshfs": "sshfs", "ceph": "ceph", "glusterfs": "gluster",
}

# Filesystems that are kernel bookkeeping, never anybody's data.
PSEUDO_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "mqueue", "hugetlbfs", "debugfs", "tracefs", "configfs", "fusectl",
    "pstore", "bpf", "securityfs", "autofs", "ramfs", "efivarfs",
    "binfmt_misc", "nsfs", "rpc_pipefs", "fuse.lxcfs", "overlay",
}

_OCTAL = re.compile(r"\\([0-7]{3})")


def _unescape(s):
    """mountinfo octal-escapes space, tab, newline and backslash."""
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), s)


class Mount:
    __slots__ = ("mount_id", "parent_id", "dev", "root", "mountpoint", "fstype", "source")

    def __init__(self, mount_id, parent_id, dev, root, mountpoint, fstype, source):
        self.mount_id, self.parent_id = mount_id, parent_id
        self.dev, self.root, self.mountpoint = dev, root, mountpoint
        self.fstype, self.source = fstype, source

    @property
    def is_pseudo(self):
        return self.fstype in PSEUDO_FS

    @property
    def is_network(self):
        return self.fstype in NETWORK_FS

    def __repr__(self):
        return f"<Mount {self.mountpoint} {self.fstype} {self.dev} root={self.root}>"


def parse_mountinfo(text):
    """`/proc/<pid>/mountinfo` -> [Mount].

    Chosen over /etc/mtab and `findmnt` because it is the only source that
    carries the `root` field -- the path *inside* the filesystem that this
    mount exposes. That single field is what makes bind mounts and nested
    datasets resolvable without parsing anyone's container config.
    """
    mounts = []
    for line in str(text).splitlines():
        line = line.strip()
        if not line:
            continue
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        lf, rf = left.split(), right.split()
        if len(lf) < 6 or len(rf) < 2:
            continue
        try:
            mounts.append(Mount(
                mount_id=int(lf[0]), parent_id=int(lf[1]), dev=lf[2],
                root=_unescape(lf[3]), mountpoint=_unescape(lf[4]),
                fstype=rf[0], source=_unescape(rf[1]),
            ))
        except ValueError:
            continue
    return mounts


# --------------------------------------------------------------------------
def _norm(p):
    p = str(p).replace("\\", "/")
    while "//" in p[1:]:
        p = p[0] + p[1:].replace("//", "/")
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/") or "/"
    return p or "/"


def _join(base, rest):
    base, rest = _norm(base), _norm(rest)
    if rest in ("", "/"):
        return base
    if base in ("", "/"):
        return rest
    return f"{base.rstrip('/')}/{rest.lstrip('/')}"


def find_mount(path, mounts):
    """Longest mountpoint prefix wins -- a nested dataset must beat its parent."""
    path = _norm(path)
    best = None
    for m in mounts:
        mp = _norm(m.mountpoint)
        if path == mp or path.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(_norm(best.mountpoint)) < len(mp):
                best = m
    return best


class VolumeRef:
    __slots__ = ("volume", "rel", "abs", "fstype", "host", "container")

    def __init__(self, volume, rel, abs_path, fstype, host, container=None):
        self.volume, self.rel, self.abs = volume, rel, abs_path
        self.fstype, self.host, self.container = fstype, host, container

    def key(self):
        return (self.volume, self.rel)

    def __eq__(self, other):
        return isinstance(other, VolumeRef) and self.key() == other.key()

    def __hash__(self):
        return hash(self.key())

    def __repr__(self):
        return f"<{self.volume}{self.rel}>"


def _split_nfs(source):
    """`192.0.2.22:/beta` -> ('192.0.2.22', '/beta')."""
    server, _, export = source.rpartition(":")
    return (server, _norm(export)) if server else ("", _norm(source))


def _split_smb(source):
    """`//fs01/data` or `\\\\fs01\\data` -> ('fs01', 'data')."""
    s = _norm(source).lstrip("/")
    server, _, share = s.partition("/")
    return server, share.split("/")[0] if share else ""


def resolve(path, mounts, host, container=None):
    """A path, as seen from one namespace, to a storage-identity reference."""
    path = _norm(path)
    m = find_mount(path, mounts)
    if m is None:
        return VolumeRef(f"unknown://{host}", path, path, "?", host, container)

    mp = _norm(m.mountpoint)
    remainder = "/" if path == mp or mp == "/" and path == "/" else path[len(mp.rstrip("/")):] or "/"
    if mp == "/":
        remainder = path

    if m.is_network:
        proto = NETWORK_FS[m.fstype]
        if proto == "smb":
            server, share = _split_smb(m.source)
            volume = f"smb://{server}/{share}"
            rel = _join(m.root, remainder)
        else:
            server, export = _split_nfs(m.source)
            volume = f"{proto}://{server}"
            # Network refs are expressed in the SERVER's absolute path, which is
            # what lets a client's read meet the server's write.
            rel = _join(export, _join(m.root if m.root != "/" else "", remainder))
        return VolumeRef(volume, _norm(rel), path, m.fstype, host, container)

    return VolumeRef(f"dev://{host}/{m.dev}", _norm(_join(m.root, remainder)),
                     path, m.fstype, host, container)


# --------------------------------------------------------------------------
# Exports -- the bridge between "the server wrote it" and "the client read it"
# --------------------------------------------------------------------------
def parse_exports(text):
    """`exportfs -s` or /etc/exports -> the set of exported paths.

    Only the paths matter here. Client ACLs and options decide who may read
    them, not whether two references are the same bytes.
    """
    paths = set()
    for line in str(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith('"'):
            end = line.find('"', 1)
            if end > 0:
                paths.add(_norm(line[1:end]))
                continue
        first = line.split()[0]
        if first.startswith("/"):
            paths.add(_norm(first))
    return paths


def equivalence_keys(ref, exports=()):
    """Every identity this reference can legitimately be matched on.

    A local path under an NFS export also lives in that server's `nfs://`
    namespace. Emitting both keys is what makes the cross-host join work
    without either side having to know about the other.
    """
    keys = {ref.key()}
    if ref.volume.startswith("dev://") and exports:
        for e in exports:
            e = _norm(e)
            if ref.abs == e or ref.abs.startswith(e.rstrip("/") + "/"):
                keys.add((f"nfs://{ref.host}", ref.abs))
                break
    return keys


def same_artifact(a, b, exports_by_host=None):
    exports_by_host = exports_by_host or {}
    ka = equivalence_keys(a, exports_by_host.get(a.host, ()))
    kb = equivalence_keys(b, exports_by_host.get(b.host, ()))
    return bool(ka & kb)


def canonical_key(ref, exports_by_host=None):
    """One stable key for grouping. Prefers the network form, because that is
    the one that can be recognised from another machine."""
    keys = equivalence_keys(ref, (exports_by_host or {}).get(ref.host, ()))
    network = sorted(k for k in keys if not k[0].startswith("dev://"))
    return network[0] if network else sorted(keys)[0]


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------
def nt_device_map():
    """`{'\\\\device\\\\harddiskvolume5': 'c:'}` for every mounted drive letter.

    ETW reports paths in NT form -- `\\Device\\HarddiskVolume10\\projects\\x` --
    not as `D:\\projects\\x`. Without this translation the noise filter never
    fires (it matches `c:/windows/`), and an ETW observation can never be
    matched against one from any other source.

    ponytail: QueryDosDeviceW over the 26 letters, via ctypes. No pywin32, no
    WMI call, no subprocess -- and Win32_Volume would not do it anyway, since
    its DeviceID is a `\\\\?\\Volume{guid}` form rather than the HarddiskVolumeN
    name ETW actually emits.
    """
    import string
    mapping = {}
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.QueryDosDeviceW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        k32.QueryDosDeviceW.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        for letter in string.ascii_uppercase:
            if k32.QueryDosDeviceW(f"{letter}:", buf, 1024):
                mapping[buf.value.strip().lower()] = network_target(buf.value) or f"{letter.lower()}:"
    except Exception:
        pass
    return mapping


# \Device\LanmanRedirector\;Y:000000000022fe0f\192.0.2.23\beta
# \Device\Mup\fs01\data
_REDIRECTOR = re.compile(
    r"^\\device\\(?:lanmanredirector|mup)\\(?:;[a-z]:[0-9a-f]*\\)?(?P<rest>.+)$", re.I)


def network_target(device_string):
    """A mapped network drive resolved to its UNC form, or None if it is local.

    This matters more than it looks. On the machine this was written on, `Y:`
    is `\\\\Device\\\\LanmanRedirector\\\\;Y:…\\\\192.0.2.23\\\\beta` -- the *same*
    ZFS dataset observed directly on that server. Mapping it to a drive letter
    would file it under `dev://thisbox/y:` and make the cross-machine link
    permanently unformable, which is the container-path mistake one level up.
    """
    m = _REDIRECTOR.match(str(device_string).strip())
    if not m:
        return None
    rest = m.group("rest").replace("\\", "/").strip("/")
    return f"//{rest}" if rest else None


def to_dos_path(nt_path, device_map=None):
    """`\\Device\\HarddiskVolume10\\projects\\x` -> `d:/projects/x`.

    An unmapped device is left in NT form rather than guessed at. A volume with
    no drive letter is real (mount points, hidden partitions) and inventing one
    would silently merge two different volumes.
    """
    p = _norm(nt_path)
    low = p.lower()
    if not low.startswith("/device/"):
        # Already a DOS path. Lowercase the drive letter so it compares equal
        # to observe.normalize_path's output -- two spellings of one path is
        # how a link silently fails to form.
        return p[0].lower() + p[1:] if re.match(r"^[a-zA-Z]:/", p) else p
    device_map = device_map if device_map is not None else nt_device_map()
    # Longest device name first: HarddiskVolume1 must not shadow HarddiskVolume10.
    for device in sorted(device_map, key=len, reverse=True):
        d = _norm(device).lower()
        if low == d or low.startswith(d.rstrip("/") + "/"):
            return _norm(device_map[device] + p[len(d):])
    return p


def resolve_windows(path, host, volume_guids=None):
    """UNC shares cross machines; drive letters do not.

    `\\\\fs01\\data\\x` -> `smb://fs01/data` + `/x`, which is directly
    comparable with a Linux CIFS mount of the same share -- the case that
    matters in a mixed estate.
    """
    p = _norm(path)
    if p.startswith("//"):
        server, share = _split_smb(p)
        rest = p[len("//") + len(server) + 1 + len(share):] or "/"
        return VolumeRef(f"smb://{server}/{share}", _norm(rest), p, "smb", host)
    m = re.match(r"^([a-zA-Z]):(/.*)?$", p)
    if m:
        letter, rest = m.group(1).lower(), m.group(2) or "/"
        guid = (volume_guids or {}).get(letter)
        volume = f"vol://{guid}" if guid else f"dev://{host}/{letter}:"
        return VolumeRef(volume, _norm(rest), p, "ntfs", host)
    return VolumeRef(f"unknown://{host}", p, p, "?", host)
