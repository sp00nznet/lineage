"""lineage.inspectors — the six things a relevance expression can ask a Windows box.

The inspector set IS the surface area of the language. Every one added is a
permanent compatibility commitment and a new thing that can hang the agent, so
this list grows slowly and on evidence.

Cost discipline matters here more than anywhere else in the project: these run
on every endpoint on a short interval. The rule is that anything spawning a
process gets cached for the life of one evaluation cycle, and anything that
cannot be cached does not become an inspector.

Build a fresh table per cycle with build_inspectors(); the cache dies with it.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys

from .relevance import RelevanceError, Version
from .update import update_ring as _update_ring

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    import winreg

    _HIVES = {
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
        "HKCR": winreg.HKEY_CLASSES_ROOT,
        "HKU": winreg.HKEY_USERS,
    }


def _require_windows(name):
    raise RelevanceError(f"{name} is only available on Windows (running {sys.platform})")


# --------------------------------------------------------------------------
# 1. Files
# --------------------------------------------------------------------------
def file_exists(path):
    return os.path.isfile(str(path))


def dir_exists(path):
    return os.path.isdir(str(path))


def file_version(path):
    """VS_FIXEDFILEINFO file version, or an empty Version if absent.

    Empty compares as 0.0.0.0, so `file_version(p) >= "138.0"` is false for a
    missing file without needing a separate existence check. Guard with
    file_exists() anyway when the distinction matters.
    """
    path = str(path)
    if not IS_WINDOWS:
        _require_windows("file_version")
    if not os.path.isfile(path):
        return Version("")

    ver = ctypes.WinDLL("version", use_last_error=True)
    ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, wintypes.LPDWORD]
    ver.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    ver.GetFileVersionInfoW.restype = wintypes.BOOL
    ver.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR,
                                   ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT)]
    ver.VerQueryValueW.restype = wintypes.BOOL

    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return Version("")
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return Version("")

    block = wintypes.LPVOID()
    length = wintypes.UINT()
    if not ver.VerQueryValueW(buf, "\\", ctypes.byref(block), ctypes.byref(length)) or not length.value:
        return Version("")

    # VS_FIXEDFILEINFO: [0]=signature [1]=strucVersion [2]=fileVersionMS [3]=fileVersionLS
    fixed = ctypes.cast(block, ctypes.POINTER(ctypes.c_uint32))
    ms, ls = fixed[2], fixed[3]
    return Version(f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}")


# --------------------------------------------------------------------------
# 2. Registry
# --------------------------------------------------------------------------
def _split_reg(path):
    path = str(path).replace("/", "\\").lstrip("\\")
    hive_name, _, sub = path.partition("\\")
    hive = _HIVES.get(hive_name.upper())
    if hive is None:
        raise RelevanceError(f"unknown registry hive: {hive_name!r} (use HKLM, HKCU, HKCR, HKU)")
    return hive, sub


def reg_exists(path):
    if not IS_WINDOWS:
        _require_windows("reg_exists")
    hive, sub = _split_reg(path)
    try:
        winreg.CloseKey(winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY))
        return True
    except OSError:
        return False


def reg_value(path, name=""):
    """Value data, or None if the key or value is absent. name="" is (Default)."""
    if not IS_WINDOWS:
        _require_windows("reg_value")
    hive, sub = _split_reg(path)
    try:
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            value, _kind = winreg.QueryValueEx(k, str(name))
            return value
    except OSError:
        return None


# --------------------------------------------------------------------------
# 3. Services  (ctypes, not `sc query` -- see note)
# --------------------------------------------------------------------------
_SERVICE_STATES = {
    1: "stopped", 2: "start_pending", 3: "stop_pending",
    4: "running", 5: "continue_pending", 6: "pause_pending", 7: "paused",
}


def service_state(name):
    """One of: running, stopped, paused, *_pending, or "" if not installed.

    ponytail: this is ctypes into advapi32 rather than parsing `sc query`,
    which would be five lines. A process spawn per service check, every cycle,
    on every endpoint is precisely the "the agent pegged the CPU" failure this
    project cannot afford. Cheap call, no spawn, no cache needed.
    """
    if not IS_WINDOWS:
        _require_windows("service_state")

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    SC_MANAGER_CONNECT = 0x0001
    SERVICE_QUERY_STATUS = 0x0004

    class SERVICE_STATUS(ctypes.Structure):
        _fields_ = [
            ("dwServiceType", wintypes.DWORD),
            ("dwCurrentState", wintypes.DWORD),
            ("dwControlsAccepted", wintypes.DWORD),
            ("dwWin32ExitCode", wintypes.DWORD),
            ("dwServiceSpecificExitCode", wintypes.DWORD),
            ("dwCheckPoint", wintypes.DWORD),
            ("dwWaitHint", wintypes.DWORD),
        ]

    scm = adv.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
    if not scm:
        raise RelevanceError("cannot open the service control manager")
    try:
        svc = adv.OpenServiceW(scm, str(name), SERVICE_QUERY_STATUS)
        if not svc:
            return ""
        try:
            status = SERVICE_STATUS()
            if not adv.QueryServiceStatus(svc, ctypes.byref(status)):
                return ""
            return _SERVICE_STATES.get(status.dwCurrentState, "unknown")
        finally:
            adv.CloseServiceHandle(svc)
    finally:
        adv.CloseServiceHandle(scm)


# --------------------------------------------------------------------------
# 4. Machine identity
# --------------------------------------------------------------------------
def hostname():
    return platform.node()


def os_caption():
    if IS_WINDOWS:
        v = reg_value(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion", "ProductName")
        if v:
            return _win11_caption(str(v), os_build())
    return platform.system()


def _win11_caption(product_name, build):
    """ProductName was never updated for Windows 11 -- a build 26200 box still says
    "Windows 10 Pro" (seen on real hardware). The build number is the authority;
    22000 is Windows 11's first build, per Microsoft's own guidance."""
    if build >= "10.0.22000" and product_name.startswith("Windows 10"):
        return "Windows 11" + product_name[len("Windows 10"):]
    return product_name


def os_build():
    """Full build as a Version, e.g. 10.0.26100.2314 -- so `os_build() >= "10.0.22000"`
    is the correct way to ask "Windows 11 or later"."""
    if not IS_WINDOWS:
        return Version(platform.release())
    cv = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    major = reg_value(cv, "CurrentMajorVersionNumber") or 10
    minor = reg_value(cv, "CurrentMinorVersionNumber") or 0
    build = reg_value(cv, "CurrentBuildNumber") or "0"
    ubr = reg_value(cv, "UBR") or 0
    return Version(f"{major}.{minor}.{build}.{ubr}")


def os_arch():
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else (
        "x64" if platform.machine().endswith("64") else "x86")


def domain():
    """AD domain, or "" for workgroup machines."""
    if not IS_WINDOWS:
        return ""
    v = reg_value(r"HKLM\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters", "Domain")
    return str(v or "")


# --------------------------------------------------------------------------
# 5. Installed software  (cached per cycle -- the registry scan is not free)
# --------------------------------------------------------------------------
_UNINSTALL_KEYS = (
    r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
)


def _scan_installed():
    """{display name lower: version string}. Registry, not winget list.

    ponytail: the uninstall keys are the fast, always-present source and cover
    everything with an installer. winget's own catalog is for *deciding what to
    install* (server side), not for detection (endpoint side) -- shelling out to
    winget here would cost a second per cycle to learn what the registry
    already knows. Add a winget path only for Store/MSIX apps, which have no
    uninstall key, and only when something actually needs them.
    """
    found = {}
    if not IS_WINDOWS:
        return found
    for root in _UNINSTALL_KEYS:
        hive, sub = _split_reg(root)
        try:
            key = winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    child = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, child, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as sk:
                        try:
                            name = winreg.QueryValueEx(sk, "DisplayName")[0]
                        except OSError:
                            continue
                        if not name:
                            continue
                        try:
                            ver = winreg.QueryValueEx(sk, "DisplayVersion")[0]
                        except OSError:
                            ver = ""
                        found.setdefault(str(name).strip().lower(), str(ver))
                except OSError:
                    continue
    return found


def _match_installed(cache, needle):
    needle = str(needle).strip().lower()
    if needle in cache:
        return cache[needle]
    for name, ver in cache.items():
        if needle in name:
            return ver
    return None


# --------------------------------------------------------------------------
# 6. CIM/WMI  (expensive -- last resort)
# --------------------------------------------------------------------------
def _cim_value(cache, wql, prop, namespace="root/cimv2"):
    """First matching instance's property as a string; "" if nothing matched.

    A *failed* query raises rather than returning "". This distinction is the
    whole point: `cim_value(...) != "1"` must not report "not encrypted"
    because the query was malformed, aimed at the wrong namespace, or run
    without the rights it needed. A check that cannot run is an error, not a
    finding. (Learned the hard way -- Win32_EncryptableVolume is not in
    root/cimv2, and the first version of this happily reported every machine
    as unencrypted.)

    ponytail: shells out to PowerShell Get-CimInstance, a few hundred ms per
    distinct query, cached per cycle. If content starts leaning on this, that
    is the signal to add a native WMI client -- not to raise the cache TTL.
    """
    key = (str(wql), str(prop), str(namespace))
    if key in cache:
        cached = cache[key]
        if isinstance(cached, RelevanceError):
            raise cached
        return cached
    if not IS_WINDOWS:
        _require_windows("cim_value")

    cmd = [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        f"(Get-CimInstance -Namespace {_ps_quote(namespace)} -Query {_ps_quote(wql)} -ErrorAction Stop"
        f" | Select-Object -First 1 -ExpandProperty {_ps_bare(prop)}) -as [string]",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        err = RelevanceError(f"cim_value: {type(e).__name__}: {e}")
        cache[key] = err
        raise err

    if proc.returncode != 0:
        detail = " ".join((proc.stderr or "").split())[:200] or f"exit {proc.returncode}"
        err = RelevanceError(f"cim_value failed in {namespace}: {detail}")
        cache[key] = err
        raise err

    out = proc.stdout.strip()
    cache[key] = out
    return out


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def _ps_bare(s):
    """Property names go in unquoted, so allow only identifier characters."""
    s = str(s)
    if not s.replace("_", "").isalnum():
        raise RelevanceError(f"invalid property name: {s!r}")
    return s


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------
def build_inspectors(agent_id=""):
    """Fresh table with a fresh per-cycle cache. One per evaluation cycle.

    agent_id feeds update_ring(), so staged rollout is expressed as ordinary
    relevance -- `update_ring() < 5` is a canary ring -- rather than as a
    second targeting mechanism bolted on beside this one.
    """
    installed_cache = {}
    cim_cache = {}

    def _installed_map():
        if "map" not in installed_cache:
            installed_cache["map"] = _scan_installed()
        return installed_cache["map"]

    def package_version(name):
        return Version(_match_installed(_installed_map(), name) or "")

    def installed(name):
        return _match_installed(_installed_map(), name) is not None

    return {
        "file_exists": file_exists,
        "dir_exists": dir_exists,
        "file_version": file_version,
        "reg_exists": reg_exists,
        "reg_value": reg_value,
        "service_state": service_state,
        "hostname": hostname,
        "os_caption": os_caption,
        "os_build": os_build,
        "os_arch": os_arch,
        "domain": domain,
        "installed": installed,
        "package_version": package_version,
        "cim_value": lambda wql, prop, namespace="root/cimv2": _cim_value(cim_cache, wql, prop, namespace),
        "version": Version,
        "update_ring": lambda: _update_ring(agent_id),
    }
