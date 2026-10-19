#!/usr/bin/env python3
"""
Phase 0 proxy diagnostic (read-only).

Loads PROXY_LIST exactly the way the bot does, then sends a few requests to a
neutral IP-echo endpoint through every proxy using different clients, so we can
see WHICH layer fails: secret content, network, credentials, or the client.

Safety: credentials are never printed (only length / first character of the
username). Proxy IPs are masked to their first three octets.

Exit code: 0 = every proxy passes the same path the bot uses
           1 = something failed
           2 = PROXY_LIST empty or unusable
"""
import os
import platform
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

TEST_URL = os.environ.get("DIAG_TEST_URL", "https://api.ipify.org")
TIMEOUT = 15
IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
URL_SAFE = re.compile(r"^[A-Za-z0-9._~-]+$")
QUOTES = re.compile(r"[\"'`]")
HTTP_407 = re.compile(r"\b407\b")

METHODS = [
    ("tcp", "TCP"),
    ("curl", "curl-CLI"),
    ("cffi_url", "cffi-URL(bot)"),
    ("cffi_auth", "cffi-proxy_auth"),
]


# ----------------------------------------------------------------- helpers
def mask_ip(ip):
    return ".".join(ip.split(".")[:3] + ["x"]) if IPV4.match(ip) else "?"


def hint(s):
    return (s[:1] + "*" * (len(s) - 1)) if s else "(empty)"


def scrub(text, secrets):
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text.replace("\n", " ").strip()


def classify_text(s):
    low = s.lower()
    if HTTP_407.search(s):
        return "AUTH_407"
    if "timed out" in low or "timeout" in low or "(28)" in s:
        return "TIMEOUT"
    if "refused" in low or "resolve" in low or "unreachable" in low or "reset" in low:
        return "CONN_ERR"
    if "ssl" in low or "tls" in low:
        return "TLS_ERR"
    return "ERR"


def ok_body(body, proxy_ip):
    body = body.strip()
    if IPV4.match(body):
        same = "exit IP == proxy IP" if body == proxy_ip else "exit IP differs from proxy IP"
        return "OK", same
    return "ERR", "200 but body is not an IP (block page?): " + body[:60].replace("\n", " ")


# ----------------------------------------------------------------- secret parsing
def parse_secret(raw):
    entries = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        e = {"lineno": lineno, "nfields": len(parts), "flags": [], "testable": False,
             "ip": "", "port": "", "user": "", "pwd": ""}
        if len(parts) >= 4:
            e["ip"], e["port"], e["user"] = parts[0], parts[1], parts[2]
            e["pwd"] = ":".join(parts[3:])
        f = e["flags"]
        if len(parts) != 4:
            f.append(f"{len(parts)} fields; the bot needs exactly 4 and silently DROPS this line")
        if QUOTES.search(line):
            f.append("contains quote characters (they become part of host/credentials)")
        if not line.isascii():
            f.append("contains non-ASCII characters (BOM / smart quotes / hidden chars?)")
        if len(parts) >= 4:
            if not IPV4.match(e["ip"]):
                f.append("ip field is not a plain IPv4 address")
            if not (e["port"].isdigit() and 0 < int(e["port"]) < 65536):
                f.append("port field is not a valid port")
            if not e["user"] or not e["pwd"]:
                f.append("empty username or password")
            else:
                if not URL_SAFE.match(e["user"]):
                    f.append("username has characters that need URL-encoding inside a proxy URL")
                if not URL_SAFE.match(e["pwd"]):
                    f.append("password has characters that need URL-encoding inside a proxy URL")
            e["testable"] = (IPV4.match(e["ip"]) is not None and e["port"].isdigit()
                             and 0 < int(e["port"]) < 65536 and bool(e["user"]) and bool(e["pwd"]))
        entries.append(e)
    return entries


def report_secret(raw, entries):
    print("== SECRET HYGIENE (values are never printed) ==")
    print(f"PROXY_LIST: {len(raw)} chars, {len(entries)} non-empty line(s)")
    print(f"CRLF line endings: {'yes (harmless, the bot strips them)' if chr(13) in raw else 'no'}")
    print(f"BOM at start: {'YES (breaks the first line)' if raw.startswith(chr(0xFEFF)) else 'no'}")
    four = [e for e in entries if e["nfields"] >= 4]
    print(f"distinct usernames: {len({e['user'] for e in four})} | "
          f"distinct passwords: {len({e['pwd'] for e in four})}")
    for e in entries:
        flags = "; ".join(e["flags"]) if e["flags"] else "none"
        print(f"  line {e['lineno']}: fields={e['nfields']} ip={mask_ip(e['ip'])} port={e['port'] or '?'} "
              f"user={hint(e['user'])} ({len(e['user'])} chars) pwd={len(e['pwd'])} chars | flags: {flags}")
    print()


# ----------------------------------------------------------------- the tests
def t_tcp(e):
    t0 = time.time()
    try:
        with socket.create_connection((e["ip"], int(e["port"])), timeout=8):
            return "OK", f"{(time.time() - t0) * 1000:.0f}ms"
    except TimeoutError:
        return "TIMEOUT", "no response in 8s"
    except OSError as ex:
        return "CONN_ERR", type(ex).__name__


def t_curl(e):
    """Plain curl CLI: the neutral reference client (same thing you ran in cmd)."""
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    cfg = f'proxy-user = "{esc(e["user"])}:{esc(e["pwd"])}"\n'  # via stdin, not argv
    cmd = ["curl", "-sS", "--max-time", str(TIMEOUT), "-x", f"http://{e['ip']}:{e['port']}",
           "-K", "-", "-o", "-", "-w", "\n@@%{http_connect}|%{http_code}", TEST_URL]
    try:
        p = subprocess.run(cmd, input=cfg, capture_output=True, text=True, timeout=TIMEOUT + 10)
    except FileNotFoundError:
        return "UNSUPPORTED", "curl CLI not found"
    except subprocess.TimeoutExpired:
        return "TIMEOUT", "curl hung"
    secrets = [e["user"], e["pwd"]]
    err = scrub(p.stderr, secrets)
    if "\n@@" in p.stdout:
        body, _, tail = p.stdout.rpartition("\n@@")
        connect, _, code = tail.strip().partition("|")
        if connect == "407" or code == "407":
            return "AUTH_407", "proxy rejected the credentials"
        if code == "200" and connect in ("200", "000"):
            return ok_body(body, e["ip"])
        if connect == "200":
            return "ERR", f"tunnel opened, request failed (HTTP {code}) {err[:80]}"
    return classify_text(err), err[:120]


def t_cffi(e, mode):
    """curl_cffi, the client the bot uses. mode 'url' = creds embedded in URL (bot today)."""
    try:
        from curl_cffi import requests as cr
    except Exception:
        return "UNSUPPORTED", "curl_cffi not importable"
    secrets = [e["user"], e["pwd"]]
    try:
        if mode == "url":
            purl = f"http://{e['user']}:{e['pwd']}@{e['ip']}:{e['port']}"
            r = cr.get(TEST_URL, proxies={"http": purl, "https": purl},
                       impersonate="chrome", timeout=TIMEOUT)
        else:
            purl = f"http://{e['ip']}:{e['port']}"
            r = cr.get(TEST_URL, proxies={"http": purl, "https": purl},
                       proxy_auth=(e["user"], e["pwd"]), impersonate="chrome", timeout=TIMEOUT)
    except TypeError as ex:
        return "UNSUPPORTED", scrub(str(ex), secrets)[:100]
    except Exception as ex:
        msg = scrub(str(ex), secrets)
        return classify_text(msg), msg[:120]
    if r.status_code == 407:
        return "AUTH_407", "proxy rejected the credentials"
    if r.status_code != 200:
        return "ERR", f"HTTP {r.status_code}"
    return ok_body(r.text, e["ip"])


def run_proxy(e):
    res = {"tcp": t_tcp(e)}
    if res["tcp"][0] != "OK":
        for k in ("curl", "cffi_url", "cffi_auth"):
            res[k] = ("SKIP", "no TCP connection")
        return res
    res["curl"] = t_curl(e)
    res["cffi_url"] = t_cffi(e, "url")
    res["cffi_auth"] = t_cffi(e, "auth")
    return res


# ----------------------------------------------------------------- reporting
def print_env():
    try:
        curl_v = subprocess.run(["curl", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    except Exception:
        curl_v = "curl CLI not found"
    try:
        import curl_cffi
        cffi_v = f"curl_cffi {getattr(curl_cffi, '__version__', '?')} / libcurl {getattr(curl_cffi, '__curl_version__', '?')}"
    except Exception:
        cffi_v = "curl_cffi NOT installed"
    print("== ENVIRONMENT ==")
    print(f"python {platform.python_version()} | {platform.system()} | runner={os.environ.get('RUNNER_OS', 'local')}")
    print(f"{curl_v}")
    print(f"{cffi_v}")
    print(f"test url: {TEST_URL}\n")


def results_table(testable, results):
    w = [6, 18] + [17] * len(METHODS)
    head = ["line", "proxy"] + [lbl for _, lbl in METHODS]
    out = ["".join(h.ljust(x) for h, x in zip(head, w))]
    for e in testable:
        r = results[e["lineno"]]
        row = [str(e["lineno"]), f"{mask_ip(e['ip'])}:{e['port']}"] + [r[k][0] for k, _ in METHODS]
        out.append("".join(c.ljust(x) for c, x in zip(row, w)))
    return out


def detail_lines(testable, results):
    out = []
    for e in testable:
        for k, lbl in METHODS:
            st, det = results[e["lineno"]][k]
            if det and st != "SKIP":
                out.append(f"  line {e['lineno']} {lbl}: {st} - {det}")
    return out


def verdict(entries, results):
    n = len(results)
    ok = lambda k: [i for i, r in results.items() if r[k][0] == "OK"]
    reach, curl_ok, url_ok, auth_ok = ok("tcp"), ok("curl"), ok("cffi_url"), ok("cffi_auth")
    bad_lines = [e["lineno"] for e in entries if not e["testable"] or e["nfields"] != 4]
    if not reach:
        return ("NETWORK: the runner could not open a TCP connection to any proxy.",
                ["This is not an authentication problem. Check the proxy IPs/ports and whether the provider blocks GitHub (Azure) IP ranges."], 1)
    if len(url_ok) == n and not bad_lines:
        return ("ALL GOOD: every proxy authenticates using the same method the bot uses.", [], 0)
    if len(url_ok) == n:
        return (f"WORKING, BUT lines {bad_lines} are malformed and the bot would drop or break them.",
                ["Fix those lines in the secret (see the flags in the hygiene section)."], 1)
    if not curl_ok and all(results[i]["curl"][0] == "AUTH_407" for i in reach):
        return ("CREDENTIALS REJECTED: every reachable proxy answers 407, even to plain curl.",
                ["The proxies are up and reachable; they refuse the username/password in PROXY_LIST.",
                 "So the problem is the credential VALUES (stale, changed or mis-pasted), not curl_cffi and not the network.",
                 "Compare the username hint above (first character + length) with your provider dashboard,",
                 "re-export the CURRENT proxy list and replace the PROXY_LIST secret.",
                 "Also check for an environment-scoped PROXY_LIST secret that shadows the repository one."], 1)
    if len(curl_ok) > len(url_ok):
        extra = (["cffi with proxy_auth works, so the bot should pass credentials via proxy_auth."]
                 if len(auth_ok) >= len(curl_ok) else
                 ["proxy_auth did not fix it either; check the curl_cffi version and the per-proxy details."])
        return ("CLIENT-SIDE: plain curl authenticates but curl_cffi with URL-embedded credentials (the bot's method) does not.",
                extra, 1)
    failing = sorted(set(results) - set(url_ok))
    notes = ["Likely a per-line problem (stale credentials on some lines, dead proxies). Read the per-proxy details."]
    if bad_lines:
        notes.append(f"Lines {bad_lines} are also malformed (see the flags in the hygiene section).")
    return (f"MIXED: {len(url_ok)}/{n} proxies pass the bot's method; failing lines: {failing}.", notes, 1)


def write_summary(md):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def main():
    raw = os.environ.get("PROXY_LIST", "")
    print_env()
    entries = parse_secret(raw)
    report_secret(raw, entries)

    testable = [e for e in entries if e["testable"]]
    if not testable:
        msg = ("PROXY_LIST is empty/unset in this job, or no line is usable. "
               "Check the `env:` mapping and whether the secret is repository- or environment-scoped.")
        print("== VERDICT ==\n" + msg)
        write_summary("## Proxy diagnostic\n**" + msg + "**")
        return 2

    print(f"== TESTING {len(testable)} PROXY(IES) against {TEST_URL} ==")
    with ThreadPoolExecutor(max_workers=min(6, len(testable))) as pool:
        outs = list(pool.map(run_proxy, testable))
    results = {e["lineno"]: r for e, r in zip(testable, outs)}

    table = results_table(testable, results)
    details = detail_lines(testable, results)
    print("\n".join(table))
    if details:
        print("\nDetails:\n" + "\n".join(details))

    head, notes, rc = verdict(entries, results)
    print("\n== VERDICT ==")
    print(head)
    for n in notes:
        print("  - " + n)

    md = ["## Proxy diagnostic", f"**{head}**", "", "```", *table, "```"]
    if details:
        md += ["", "Details:", "```", *details, "```"]
    if notes:
        md += [""] + [f"- {n}" for n in notes]
    write_summary("\n".join(md))
    return rc


if __name__ == "__main__":
    sys.exit(main())
