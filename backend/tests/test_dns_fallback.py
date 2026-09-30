"""nslookup parsing for the DNS fallback (app/utils/dns_fallback.py)."""
import socket

from app.utils import dns_fallback as d

SINGLE = """Server:  fbdc.levelshift.com
Address:  192.168.2.139

Name:    apid69e4.eastus.cloudapp.azure.com
Address:  20.232.91.180
Aliases:  designstudioopenai.openai.azure.com
\t  eastus.api.cognitive.microsoft.com
"""

MULTI = """Server:  fbdc.levelshift.com
Address:  192.168.2.139

Name:    people.zoho.in
Addresses:  2406:da1a:6f2::10
\t  169.148.148.221
\t  169.148.148.222

"""


class _Proc:
    def __init__(self, out):
        self.stdout = out


def test_parses_single_and_multiple_addresses(monkeypatch):
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: _Proc(SINGLE))
    assert d._nslookup("x") == ["20.232.91.180"]          # not the DNS server's own address
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: _Proc(MULTI))
    assert d._nslookup("x") == ["2406:da1a:6f2::10", "169.148.148.221", "169.148.148.222"]
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: _Proc("*** can't find x: Non-existent domain"))
    assert d._nslookup("x") == []


def test_fallback_only_runs_when_normal_lookup_fails(monkeypatch):
    calls = []

    def failing(host, port, *a, **k):
        calls.append(host)
        if host == "blocked.example":
            raise socket.gaierror(11001, "getaddrinfo failed")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    monkeypatch.setattr(d, "_original_getaddrinfo", failing)
    monkeypatch.setattr(d, "resolve", lambda host: ["20.232.91.180"])
    out = d._getaddrinfo("blocked.example", 443)
    assert out[0][4] == ("20.232.91.180", 443)
    assert calls == ["blocked.example", "20.232.91.180"]  # normal first, then numeric

    # IPs and localhost are never sent to nslookup.
    monkeypatch.setattr(d, "_original_getaddrinfo",
                        lambda *a, **k: (_ for _ in ()).throw(socket.gaierror(11001, "x")))
    for host in ("localhost", "10.0.0.1"):
        try:
            d._getaddrinfo(host, 80)
            raise AssertionError("should have raised")
        except socket.gaierror:
            pass
