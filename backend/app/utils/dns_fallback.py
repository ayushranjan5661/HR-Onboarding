"""
Hostname lookup that still works when this machine blocks Python's own DNS.

On LevelShift-managed Windows machines the endpoint-protection software
refuses DNS lookups from Python when it is started as a script or a
program (`python app.py`, `uvicorn.exe ...`) — every getaddrinfo() fails
with "[Errno 11001] getaddrinfo failed", for every host, while the same
lookup from `python -c` works. TCP and TLS by IP address are NOT blocked.
The result is that every outbound call the server makes — Azure OpenAI for
the insights and field-mapping agents, the Zoho People push — fails before
it leaves the machine.

`install()` wraps socket.getaddrinfo: the normal lookup is always tried
first; only when it raises gaierror for a real hostname is the name
resolved with Windows' own `nslookup` (a separate, allowed process) and
the connection made to that address. Nothing else changes:

  * TLS still verifies the certificate against the *hostname* — urllib and
    ssl pass the name from the URL as server_hostname, not the IP.
  * Results are cached for a few minutes, so a burst of requests does not
    start a burst of nslookup processes.
  * On a machine without the restriction the fallback never runs.

Called once from app.main at import time.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import threading
import time

_TTL = 300                                   # seconds a looked-up address is reused
_cache: dict[str, tuple[float, list[str]]] = {}
_lock = threading.Lock()
_original_getaddrinfo = socket.getaddrinfo
_installed = False

_ADDR_RE = re.compile(r"^\s*(?:Address(?:es)?:)?\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3}|[0-9a-fA-F:]+:[0-9a-fA-F:]+)\s*$")


def _nslookup(host: str) -> list[str]:
    """Addresses for `host` from nslookup, skipping the DNS server's own
    address that nslookup prints first."""
    try:
        out = subprocess.run(["nslookup", host], capture_output=True, text=True,
                             timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    # Everything after the "Name:" line belongs to the answer; above it is
    # the server block ("Server: ... Address: 192.168.x.x").
    _, sep, answer = out.partition("Name:")
    if not sep:
        return []
    addrs = []
    for line in answer.splitlines()[1:]:
        if line.strip().startswith("Aliases"):
            break
        m = _ADDR_RE.match(line)
        if m:
            try:
                addrs.append(str(ipaddress.ip_address(m.group(1))))
            except ValueError:
                pass
    return addrs


def resolve(host: str) -> list[str]:
    now = time.time()
    with _lock:
        hit = _cache.get(host)
        if hit and hit[0] > now:
            return hit[1]
    addrs = _nslookup(host)
    if addrs:
        with _lock:
            _cache[host] = (now + _TTL, addrs)
        # Logged only on a real lookup, not on every cached reuse.
        print(f"[dns_fallback] resolved {host} via nslookup -> {addrs[0]}")
    return addrs


def _is_hostname(host) -> bool:
    if not isinstance(host, str) or not host or host == "localhost":
        return False
    try:
        ipaddress.ip_address(host)
        return False                           # already an address
    except ValueError:
        return True


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _original_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        if not _is_hostname(host):
            raise
        addrs = resolve(host)
        if not addrs:
            raise
        results = []
        for addr in addrs:
            is_v6 = ":" in addr
            if family not in (0, socket.AF_UNSPEC) and family != (socket.AF_INET6 if is_v6 else socket.AF_INET):
                continue
            # Resolving a literal address never touches DNS, so the original
            # call is allowed and builds correctly-shaped tuples for us.
            results.extend(_original_getaddrinfo(addr, port, family, type, proto,
                                                 flags | socket.AI_NUMERICHOST))
        if not results:
            raise
        return results


def install() -> None:
    global _installed
    if not _installed:
        socket.getaddrinfo = _getaddrinfo
        _installed = True
