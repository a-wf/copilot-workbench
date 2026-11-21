"""
copilot_model_benchmarks.py — bounded, read-only ingestion of a FIXED,
reviewed list of public third-party model comparison pages for
copilot-model-refresh.py (copilot-s menu `p`). A library, not a command.

What it does
- GETs only the exact HTTPS URLs listed in PAIRS below (one page per model
  pair and provider, reviewed by hand; there is no URL built from user
  input, settings or remote content). None of your data is sent: no query
  string, no cookies, no auth, a fixed User-Agent and Accept header (like
  any web request, the site sees your IP address and the request time).
- Transport: the host must be the provider's fixed host; it is resolved
  once and EVERY resolved address must be a public (global) IP (no
  loopback/private/link-local/reserved targets), and the connection is
  pinned to that vetted address with TLS certificate verification ON
  (system curl fallback only after a Python certificate-verification
  error, pinned with --resolve, never -k). Redirects are refused, only
  HTTP 200 with a text/markdown (or text/plain) body is accepted, each
  request has a timeout, all requests share one deadline, and the body
  size is capped.
- Parsing: one strict, versioned stdlib parser per provider page layout.
  It verifies the page identity (canonical URL, title naming exactly the
  two expected models, column headers), reads the publisher's
  "updated" date, and extracts ONLY an allowlisted set of metrics with
  numeric sanity checks. Duplicate/conflicting rows, unknown values and
  unknown layouts become UNKNOWN (never guessed). Reasoning effort is
  recorded only where the page states it per column; otherwise it is
  unknown. Remote free text is never stored or displayed: labels come
  from the allowlist below, names from the registry, values are numbers.
- Cache: <COPILOT_HOME>/model-refresh/benchmark-evidence.json, reused for
  5 minutes, "fresh" for 24 hours (local policy); older copies are shown as
  stale reference only and never used to offer anything.

Limits (shown to the user)
- Coverage is limited to the reviewed pairs below — NOT comprehensive.
- Aggregators may republish the same underlying runs, so two sources
  agreeing is not independent confirmation; neither is "primary".
- Scores are not about your account, plan, organization policy, harness or
  the reasoning level your route uses, and are never proof of quality,
  availability or of an exact reasoning level. Third-party prices are never
  read (their blended price assumptions differ from copilot-model-refresh).

Stdlib only.
"""

import contextlib
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from datetime import date, datetime, timezone

SCHEMA_VERSION = 1
CACHE_KIND = "copilot-toolkit-model-benchmark-evidence"
PARSER_VERSION = 1
CACHE_FILENAME = "benchmark-evidence.json"
MIN_INTERVAL_SECONDS = 300          # reuse a copy fetched < 5 min ago
FRESH_SECONDS = 24 * 3600           # local freshness policy
TIMEOUT_SECONDS = 8                 # per request (connect / each read)
DEADLINE_SECONDS = 20               # all requests of one refresh together
MAX_BYTES = 1_000_000
# Longest page line ever inspected (reviewed pages: < 3,000 chars). Longer
# lines are skipped, never parsed, so no pattern ever runs on them.
MAX_LINE_CHARS = 20_000
MAX_CACHE_BYTES = 4_000_000
FETCH_ENV = "COPILOT_MODEL_REFRESH_BENCHMARKS_FETCH"
USER_AGENT = "copilot-model-refresh (copilot-cli-toolkit third-party comparison refresh)"
ACCEPT = "text/markdown, text/plain;q=0.5"
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

# ------------------------------------------------------------ registry ---
# Reviewed providers. `metrics`: the ONLY rows ever read, in display
# priority order: (key, row label exactly as published, publisher column
# value or None, unit, kind). kind "benchmark" = a named benchmark result as
# republished by the aggregator; "composite" = the aggregator's own computed
# category score (its own scale; not a benchmark).
PROVIDERS = {
    "benchleader": {
        "label": "BenchLeader",
        "host": "www.benchleader.com",
        "about": "https://www.benchleader.com/methodology",
        "layout": "benchleader-compare-md-v1",
        "limits": ("aggregator; compares each model at the reasoning-effort setting named in each column "
                   "(its best-ranked one, not necessarily your route's level); values marked as measured at "
                   "another effort are not comparable; harness per publisher"),
        "metrics": (
            ("terminal_bench", "Terminal-Bench", "Terminal-Bench", "%", "benchmark"),
            ("terminal_bench_40_aa", "Terminal-Bench 4.0 (AA)", "Artificial Analysis", "%", "benchmark"),
            ("terminal_bench_40_vals", "Terminal-Bench 4.0 (Vals)", "Vals AI", "%", "benchmark"),
            ("frontiercode_20_aa", "FrontierCode 2.0 (AA)", "Artificial Analysis", "%", "benchmark"),
            ("swe_atlas_11", "SWE Atlas v1.1", "Scale AI SEAL", "%", "benchmark"),
            ("bl_coding_score", "Coding score", "BenchLeader", "score", "composite"),
            ("bl_agents_score", "Agents & tools score", "BenchLeader", "score", "composite"),
        ),
    },
    "benchlm": {
        "label": "BenchLM",
        "host": "benchlm.ai",
        "about": "https://benchlm.ai/score-sources",
        "layout": "benchlm-compare-md-v1",
        "limits": ("aggregator; the comparison page does not state the reasoning-effort setting or harness "
                   "(unknown); scores the aggregator marks as estimated are not comparable"),
        "metrics": (
            ("terminal_bench_41", "Terminal-Bench 4.1", None, "%", "benchmark"),
            ("terminal_bench_40", "Terminal-Bench 4.0", None, "%", "benchmark"),
            ("frontiercode_20", "FrontierCode 2.0", None, "%", "benchmark"),
            ("blm_coding_lane", "Coding", None, "score", "composite"),
            ("blm_agentic_lane", "Agentic", None, "score", "composite"),
        ),
    },
}

# Reviewed pages: exact URL per provider and model pair. `models` maps the
# GitHub Copilot model ID to the display name the page must use.
PAIRS = (
    {"id": "benchleader:claude-sonnet-5.5:gpt-6.1-terra", "provider": "benchleader",
     "url": "https://www.benchleader.com/compare/claude-sonnet-5-5-vs-gpt-6-1-terra.md",
     "page": "https://www.benchleader.com/compare/claude-sonnet-5-5-vs-gpt-6-1-terra",
     "models": (("claude-sonnet-5.5", "Claude Sonnet 5.5"), ("gpt-6.1-terra", "GPT-6.1 Terra"))},
    {"id": "benchleader:gpt-6.1-sol:gpt-6.2-sol", "provider": "benchleader",
     "url": "https://www.benchleader.com/compare/gpt-6-1-sol-vs-gpt-6-2-sol.md",
     "page": "https://www.benchleader.com/compare/gpt-6-1-sol-vs-gpt-6-2-sol",
     "models": (("gpt-6.1-sol", "GPT-6.1 Sol"), ("gpt-6.2-sol", "GPT-6.2 Sol"))},
    {"id": "benchlm:claude-sonnet-5.5:gpt-6.1-terra", "provider": "benchlm",
     "url": "https://benchlm.ai/compare/claude-sonnet-5-5-vs-gpt-6-1-terra",
     "page": "https://benchlm.ai/compare/claude-sonnet-5-5-vs-gpt-6-1-terra",
     "models": (("claude-sonnet-5.5", "Claude Sonnet 5.5"), ("gpt-6.1-terra", "GPT-6.1 Terra"))},
    {"id": "benchlm:gpt-6.1-sol:gpt-6.2-sol", "provider": "benchlm",
     "url": "https://benchlm.ai/compare/gpt-6-1-sol-vs-gpt-6-2-sol",
     "page": "https://benchlm.ai/compare/gpt-6-1-sol-vs-gpt-6-2-sol",
     "models": (("gpt-6.1-sol", "GPT-6.1 Sol"), ("gpt-6.2-sol", "GPT-6.2 Sol"))},
)
PAIR_BY_ID = {p["id"]: p for p in PAIRS}
ALLOWED_URLS = frozenset(p["url"] for p in PAIRS)


class BenchmarkError(Exception):
    pass


def registry_digest():
    """Binds the reviewed registry (URLs, names, metric allowlists) and the
    parser version: a cache written for another registry is ignored."""
    payload = {"parser_version": PARSER_VERSION,
               "providers": {k: {"host": v["host"], "layout": v["layout"], "metrics": [list(m) for m in v["metrics"]]}
                             for k, v in sorted(PROVIDERS.items())},
               "pairs": [{"id": p["id"], "provider": p["provider"], "url": p["url"], "page": p["page"],
                          "models": [list(m) for m in p["models"]]} for p in PAIRS]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _validate_registry():
    for p in PAIRS:
        prov = PROVIDERS[p["provider"]]
        for url in (p["url"], p["page"]):
            if not url.startswith("https://%s/" % prov["host"]) or "?" in url or "#" in url:
                raise BenchmarkError("registry URL %s is not a fixed https URL on %s" % (url, prov["host"]))
        if len(p["models"]) != 2 or p["models"][0][0] == p["models"][1][0]:
            raise BenchmarkError("registry pair %s must name two different models" % p["id"])


_validate_registry()

# ------------------------------------------------------------ sanitizing ---
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def safe_text(text, limit=200):
    """Remote/exception text -> one printable line: ANSI/OSC sequences,
    control, zero-width and bidi characters removed, length capped."""
    text = _CONTROL_RE.sub(" ", _ANSI_RE.sub(" ", str(text)))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _canonical_sha256(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


def _now_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------- transport ---

def _check_deadline(deadline, where):
    """Raise once the shared monotonic budget of this refresh is used up: a
    late result is never accepted, even if it is complete and valid."""
    if time.monotonic() > deadline:
        raise BenchmarkError("%s: the %ds deadline for all comparison pages was exceeded" % (where, DEADLINE_SECONDS))


def _getaddrinfo_bounded(host, timeout):
    """socket.getaddrinfo has no timeout of its own: run it in a daemon
    thread and stop waiting after `timeout` seconds (the lookup thread is
    abandoned, never joined again; nothing uses its late result)."""
    box = {}

    def run():
        try:
            box["infos"] = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)
        except BaseException as ex:  # noqa: BLE001 - handed back to the caller
            box["error"] = ex
    worker = threading.Thread(target=run, name="copilot-benchmark-dns", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise BenchmarkError("DNS lookup exceeded %ds" % timeout)
    if "error" in box:
        raise box["error"]
    return box["infos"]


def _public_address(host):
    """The first resolved address of `host`, only if EVERY resolved address
    is a public (global) unicast IP. Refuses otherwise (no SSRF to local or
    private networks, even via DNS). The lookup is bounded by
    TIMEOUT_SECONDS; the caller re-checks the shared deadline afterwards."""
    try:
        infos = _getaddrinfo_bounded(host, TIMEOUT_SECONDS)
    except (OSError, UnicodeError) as ex:
        raise BenchmarkError("DNS lookup failed (%s)" % type(ex).__name__)
    addrs = []
    for family, _type, _proto, _canon, sockaddr in infos:
        if family not in (socket.AF_INET, getattr(socket, "AF_INET6", None)):
            continue
        try:
            ip = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
        except ValueError:
            raise BenchmarkError("DNS returned an unparsable address")
        mapped = getattr(ip, "ipv4_mapped", None)
        check = mapped or ip
        if (not check.is_global or check.is_multicast or check.is_loopback or check.is_link_local
                or check.is_private or check.is_reserved or check.is_unspecified):
            raise BenchmarkError("refused: %s resolves to a non-public address" % host)
        addrs.append((family, str(ip)))
    if not addrs:
        raise BenchmarkError("DNS returned no address")
    return addrs[0]


def _is_cert_error(ex):
    import ssl
    cert_error = getattr(ssl, "SSLCertVerificationError", None)
    return cert_error is not None and isinstance(ex, cert_error)


def _check_headers(status, ctype, clen, max_bytes):
    if 300 <= status < 400:
        raise BenchmarkError("HTTP %d (redirect refused)" % status)
    if status != 200:
        raise BenchmarkError("HTTP %d" % status)
    media = (ctype or "").split(";", 1)[0].strip().lower()
    if media not in ("text/markdown", "text/plain"):
        raise BenchmarkError("unexpected content type %s" % safe_text(media or "none", 60))
    if clen and clen.strip().isdigit() and int(clen) > max_bytes:
        raise BenchmarkError("response too large")


def _python_fetch(url, host, path, family_ip, timeout, max_bytes, deadline):
    import http.client
    import ssl

    ip = family_ip[1]
    context = ssl.create_default_context()
    _check_deadline(deadline, "before connecting")
    # Each blocking operation (connect, TLS, every read) is bounded by the
    # per-request timeout and never by more than what is left of the budget.
    timeout = max(0.1, min(timeout, deadline - time.monotonic()))

    class _Pinned(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((ip, 443), self.timeout)
            try:
                self.sock = context.wrap_socket(sock, server_hostname=host)
            except BaseException:
                sock.close()
                raise

    conn = _Pinned(host, 443, timeout=timeout, context=context)
    try:
        conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": ACCEPT,
                                           "Accept-Encoding": "identity", "Connection": "close"})
        resp = conn.getresponse()
        _check_headers(resp.status, resp.getheader("Content-Type"), resp.getheader("Content-Length"), max_bytes)
        chunks, size = [], 0
        while True:
            _check_deadline(deadline, "download")
            chunk = resp.read(65536)
            # Checked AFTER every read too (including the final empty one):
            # a body that completes late is refused, not accepted.
            _check_deadline(deadline, "download")
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise BenchmarkError("response too large")
            chunks.append(chunk)
        return b"".join(chunks)
    except http.client.HTTPException as ex:
        raise BenchmarkError("HTTP protocol error (%s)" % type(ex).__name__)
    finally:
        conn.close()


_CURL_MARKER = "\n@@copilot-model-benchmarks-curl@@ "


def _curl_fetch(url, host, family_ip, timeout, max_bytes, deadline):
    """Verified system-curl fallback (only after a Python certificate
    verification error): `-q` ignores ~/.curlrc, HTTPS only, no redirects,
    certificate verification ON, connection pinned to the vetted address."""
    import subprocess

    curl = "/usr/bin/curl" if os.access("/usr/bin/curl", os.X_OK) else shutil.which("curl")
    if not curl:
        raise BenchmarkError("Python could not verify the TLS certificate and no system curl was found")
    left = deadline - time.monotonic()
    if left < 1:
        raise BenchmarkError("system curl fallback: the %ds deadline for all comparison pages was exceeded"
                             % DEADLINE_SECONDS)
    remaining = int(left)
    timeout = max(1, min(int(timeout), remaining))
    ip = family_ip[1]
    pin = "%s:443:%s" % (host, ("[%s]" % ip) if ":" in ip else ip)
    argv = [curl, "-q", "--silent", "--show-error", "--proto", "=https", "--max-redirs", "0", "--tlsv1.2",
            "--resolve", pin, "--connect-timeout", str(int(timeout)), "--max-time", str(remaining),
            "--max-filesize", str(int(max_bytes)), "--header", "User-Agent: " + USER_AGENT,
            "--header", "Accept: " + ACCEPT, "--header", "Accept-Encoding: identity",
            "--write-out", _CURL_MARKER + "%{http_code} %{num_redirects} %{content_type}", "--url", url]
    cap = max_bytes + 400
    chunks, size = [], 0
    try:
        with tempfile.TemporaryFile() as err_file:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err_file,
                                    shell=False, close_fds=True, env={"PATH": "/usr/bin:/bin"})
            try:
                while True:
                    chunk = proc.stdout.read1(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > cap:
                        raise BenchmarkError("response too large")
                    chunks.append(chunk)
                returncode = proc.wait(timeout=remaining + 5)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=5)
                proc.stdout.close()
            err_file.seek(0)
            err_text = safe_text(err_file.read(1000).decode("utf-8", "replace"), 160)
    except subprocess.TimeoutExpired:
        raise BenchmarkError("system curl fallback timed out")
    except (OSError, subprocess.SubprocessError) as ex:
        raise BenchmarkError("system curl fallback could not run (%s)" % type(ex).__name__)
    if returncode != 0:
        raise BenchmarkError("system curl fallback failed (exit %d: %s)" % (returncode, err_text or "no message"))
    _check_deadline(deadline, "system curl fallback")
    out = b"".join(chunks)
    idx = out.rfind(_CURL_MARKER.encode("ascii"))
    if idx < 0:
        raise BenchmarkError("system curl fallback: missing status trailer")
    body = out[:idx]
    parts = out[idx + len(_CURL_MARKER):].decode("ascii", "replace").strip().split(" ", 2)
    if len(parts) < 2 or not parts[0].isdigit() or parts[1] != "0":
        raise BenchmarkError("system curl fallback: unexpected status trailer")
    _check_headers(int(parts[0]), parts[2] if len(parts) > 2 else "", None, max_bytes)
    if len(body) > max_bytes:
        raise BenchmarkError("response too large")
    return body


def default_fetch(url, timeout, max_bytes, deadline):
    """Bounded GET of one registry URL (see module docstring)."""
    if url not in ALLOWED_URLS:
        raise BenchmarkError("refused: not a reviewed comparison URL")
    m = re.match(r"^https://([a-z0-9.-]+)(/[A-Za-z0-9._~/-]*)$", url)
    if not m:
        raise BenchmarkError("refused: malformed registry URL")
    host, path = m.group(1), m.group(2)
    _check_deadline(deadline, "before DNS")
    family_ip = _public_address(host)
    _check_deadline(deadline, "after DNS")
    try:
        raw = _python_fetch(url, host, path, family_ip, timeout, max_bytes, deadline)
    except BenchmarkError:
        raise
    except OSError as ex:
        if not _is_cert_error(ex):
            raise BenchmarkError("%s: %s" % (type(ex).__name__, safe_text(ex, 140)))
        raw = _curl_fetch(url, host, family_ip, timeout, max_bytes, deadline)
    _check_deadline(deadline, "after download")
    return raw


# --------------------------------------------------------------- parsing ---

_MD_LINK_RE = re.compile(r"\[([^\[\]]{0,200})\]\([^()\s]{0,500}\)")
_MONTHS = {m: i + 1 for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july",
                                            "august", "september", "october", "november", "december"))}
_VALUE_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,4})?)\s*(%)?\s*(\u2020)?$")
_MISSING = ("", "\u2013", "-", "\u2014", "coming soon", "n/a")


def _cell(text):
    text = _MD_LINK_RE.sub(lambda m: m.group(1), text[:1000])
    return safe_text(text.replace("**", "").replace("`", ""), 300)


def _row(line):
    line = line.strip()
    if not (line.startswith("|") and line.endswith("|")) or len(line) < 2:
        return None
    return [_cell(c) for c in line[1:-1].split("|")]


def _is_separator(cells):
    return bool(cells) and all(re.match(r"^:?-{3,}:?$", c) for c in cells)


def _date_from(day, month, year):
    month_no = _MONTHS.get(month.lower()) or next(
        (v for k, v in _MONTHS.items() if len(month) == 3 and k.startswith(month.lower())), None)
    if not month_no:
        return None
    try:
        return date(int(year), month_no, int(day)).isoformat()
    except ValueError:
        return None


def _value(text, unit):
    """(number_or_None, other_effort, problem_or_None)."""
    if text.strip().lower() in _MISSING:
        return None, False, None
    m = _VALUE_RE.match(text.strip())
    if not m:
        return None, False, "unrecognized value"
    number = float(m.group(1))
    got_unit = "%" if m.group(2) else "score"
    if got_unit != unit:
        return None, False, "unexpected unit"
    if not 0 <= number <= 100:
        return None, False, "out of range"
    return number, bool(m.group(3)), None


def _expected_title(pair, title):
    names = [n for _m, n in pair["models"]]
    return title in ("%s vs %s" % (names[0], names[1]), "%s vs %s" % (names[1], names[0]))


def _lines(text):
    """Page lines, each bounded: over-long lines become empty (skipped)."""
    return [ln if len(ln) <= MAX_LINE_CHARS else "" for ln in text.splitlines()]


def _heading(line):
    """(level, text) for an ATX heading line, else None. Linear string
    operations only (no backtracking pattern on untrusted text)."""
    if not line.startswith("#") or len(line) > MAX_LINE_CHARS:
        return None
    level = len(line) - len(line.lstrip("#"))
    rest = line[level:]
    if level > 6 or not rest[:1].isspace():
        return None
    return level, rest.strip()


def _section(lines, title):
    """Lines after the first heading whose text is exactly `title`, up to
    the next heading of the same or higher level."""
    out, inside, level = [], False, 0
    for line in lines:
        h = _heading(line)
        if h:
            if inside and h[0] <= level:
                break
            if not inside and h[1] == title:
                inside, level = True, h[0]
                continue
        if inside:
            out.append(line)
    return out if inside else None


def expected_label(provider, spec):
    """(display label, publisher) stored for an allowlisted metric."""
    if provider == "benchlm" and spec[4] == "composite":
        return "%s category score" % spec[1], "BenchLM"
    return spec[1], spec[2]


def _store_metric(provider, metrics, spec, values, notes):
    key = spec[0]
    if key in metrics:
        # Same allowlisted row twice: conflicting/duplicated -> unknown.
        metrics[key]["values"] = {m: {"value": None, "other_effort": False} for m in metrics[key]["values"]}
        metrics[key]["conflict"] = True
        notes.append("%s: listed more than once on the page — treated as unknown" % spec[1])
        return
    label, publisher = expected_label(provider, spec)
    metrics[key] = {"key": key, "label": label, "publisher": publisher, "unit": spec[3], "kind": spec[4],
                    "values": values, "conflict": False, "estimated": False}


def _parse_benchleader(pair, text):
    prov = PROVIDERS["benchleader"]
    lines = _lines(text)
    if not lines or lines[0].strip() != "---":
        raise BenchmarkError("unrecognized layout (no front matter)")
    try:
        end = next(i for i in range(1, min(len(lines), 40)) if lines[i].strip() == "---")
    except StopIteration:
        raise BenchmarkError("unrecognized layout (front matter not closed)")
    urls = [ln for ln in lines[1:end] if ln.startswith("url:")]
    if len(urls) != 1 or urls[0][4:].strip().strip('"') != pair["page"]:
        raise BenchmarkError("page identity mismatch (canonical url)")
    body = lines[end + 1:]
    titles = [ln[2:].strip() for ln in body if ln.startswith("# ")]
    if not titles or not _expected_title(pair, safe_text(titles[0], 200)):
        raise BenchmarkError("page identity mismatch (title)")
    updated = None
    for ln in body[:60]:
        m = re.match(r"^Updated (\d{1,2}) ([A-Za-z]{3,9}) (\d{4})\b", ln.strip())
        if m:
            updated = _date_from(m.group(1), m.group(2), m.group(3))
            break
    section = _section(body, "Every benchmark")
    if section is None:
        raise BenchmarkError("unrecognized layout (no 'Every benchmark' section)")
    names = {n: mid for mid, n in pair["models"]}
    header_cols, efforts = None, {}
    by_label = {(s[1], s[2]): s for s in prov["metrics"]}
    metrics, notes = {}, []
    for line in section:
        cells = _row(line)
        if cells is None or _is_separator(cells):
            continue
        if len(cells) >= 2 and cells[0] == "Metric" and cells[1] == "Publisher":
            if len(cells) != 4:
                raise BenchmarkError("unrecognized layout (benchmark table columns)")
            cols = []
            for cell in cells[2:]:
                m = re.match(r"^(.*?) \(([a-z]{3,8})\)$", cell)
                name, effort = (m.group(1), m.group(2)) if m else (cell, None)
                if name not in names:
                    raise BenchmarkError("page identity mismatch (column %s)" % safe_text(name, 60))
                if effort is not None and effort not in EFFORTS:
                    effort = None
                cols.append((names[name], effort))
            if len({c[0] for c in cols}) != 2:
                raise BenchmarkError("page identity mismatch (columns)")
            if header_cols is not None and cols != header_cols:
                raise BenchmarkError("inconsistent benchmark table headers")
            header_cols = cols
            efforts = {mid: eff for mid, eff in cols}
            continue
        if header_cols is None or len(cells) != 4:
            continue
        spec = by_label.get((cells[0], cells[1]))
        if spec is None:
            continue
        values = {}
        for (mid, _eff), raw in zip(header_cols, cells[2:]):
            number, other, problem = _value(raw, spec[3])
            if problem:
                notes.append("%s: %s for %s — treated as unknown" % (spec[1], problem, mid))
            values[mid] = {"value": number, "other_effort": other}
        _store_metric("benchleader", metrics, spec, values, notes)
    if header_cols is None:
        raise BenchmarkError("unrecognized layout (no benchmark table header)")
    return {"updated_on": updated, "efforts": efforts, "metrics": metrics, "notes": notes,
            "layout": prov["layout"]}


def _parse_benchlm(pair, text):
    prov = PROVIDERS["benchlm"]
    lines = _lines(text)
    titles = [ln[2:].strip() for ln in lines if ln.startswith("# ")]
    if not titles or not _expected_title(pair, safe_text(titles[0], 200)):
        raise BenchmarkError("page identity mismatch (title)")
    canon = [ln for ln in lines[:40] if ln.startswith("- Canonical page:")]
    if len(canon) != 1 or canon[0].split(":", 1)[1].strip() != pair["page"]:
        raise BenchmarkError("page identity mismatch (canonical page)")
    updated = None
    for ln in lines[:40]:
        m = re.match(r"^- Last updated: ([A-Za-z]{3,9}) (\d{1,2}), (\d{4})$", ln.strip())
        if m:
            updated = _date_from(m.group(2), m.group(1), m.group(3))
            break
    breakdown = _section(lines, "Category Breakdown")
    if breakdown is None:
        raise BenchmarkError("unrecognized layout (no 'Category Breakdown' section)")
    names = {n: mid for mid, n in pair["models"]}
    by_label = {s[1]: s for s in prov["metrics"] if s[4] == "benchmark"}
    lanes = {s[1]: s for s in prov["metrics"] if s[4] == "composite"}
    metrics, notes = {}, []
    category, cols, seen_header = None, None, False
    for line in breakdown:
        h = _heading(line)
        if h and h[0] == 3:
            category, cols = safe_text(h[1], 80), None
            continue
        stripped = line.strip()
        lane_name, marker, lane_rest = stripped[2:].partition(" public-lane score: ")
        if stripped.startswith("- ") and marker and lane_name and lane_rest and category in lanes:
            name, rest = safe_text(lane_name, 80), safe_text(lane_rest, 120)
            if name not in names:
                raise BenchmarkError("page identity mismatch (lane score for %s)" % safe_text(name, 60))
            spec = lanes[category]
            m = re.match(r"^(\d{1,3}(?:\.\d{1,4})?) \((Supported|Estimated)\b", rest)
            entry = metrics.get(spec[0])
            if entry is None:
                label, publisher = expected_label("benchlm", spec)
                entry = {"key": spec[0], "label": label, "publisher": publisher,
                         "unit": spec[3], "kind": spec[4], "values": {}, "conflict": False, "estimated": False}
                metrics[spec[0]] = entry
            mid = names[name]
            if mid in entry["values"]:
                entry["conflict"] = True
                notes.append("%s: listed more than once — treated as unknown" % entry["label"])
            if m and 0 <= float(m.group(1)) <= 100:
                entry["values"][mid] = {"value": float(m.group(1)), "other_effort": False}
                if m.group(2) == "Estimated":
                    entry["estimated"] = True
            else:
                entry["values"][mid] = {"value": None, "other_effort": False}
            continue
        cells = _row(line)
        if cells is None or _is_separator(cells):
            continue
        if cells and cells[0] == "Benchmark":
            if len(cells) != 4 or cells[3] != "Winner":
                raise BenchmarkError("unrecognized layout (benchmark table columns)")
            if cells[1] not in names or cells[2] not in names or cells[1] == cells[2]:
                raise BenchmarkError("page identity mismatch (columns)")
            cols, seen_header = [names[cells[1]], names[cells[2]]], True
            continue
        if cols is None or len(cells) != 4:
            continue
        spec = by_label.get(cells[0])
        if spec is None:
            continue
        values = {}
        for mid, raw in zip(cols, cells[1:3]):
            number, other, problem = _value(raw, spec[3])
            if problem:
                notes.append("%s: %s for %s — treated as unknown" % (spec[1], problem, mid))
            values[mid] = {"value": number, "other_effort": other}
        _store_metric("benchlm", metrics, spec, values, notes)
    if not seen_header and not metrics:
        raise BenchmarkError("unrecognized layout (no benchmark tables)")
    for entry in metrics.values():
        if entry["conflict"]:
            entry["values"] = {m: {"value": None, "other_effort": False} for m in entry["values"]}
        for mid, _n in pair["models"]:
            entry["values"].setdefault(mid, {"value": None, "other_effort": False})
    return {"updated_on": updated, "efforts": {mid: None for mid, _n in pair["models"]}, "metrics": metrics,
            "notes": notes, "layout": prov["layout"]}


_PARSERS = {"benchleader": _parse_benchleader, "benchlm": _parse_benchlm}


def parse_page(pair, raw):
    """bytes -> parsed record body (raises BenchmarkError)."""
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeError:
        raise BenchmarkError("page is not valid UTF-8")
    parsed = _PARSERS[pair["provider"]](pair, text)
    if not any(any(v["value"] is not None for v in m["values"].values()) for m in parsed["metrics"].values()):
        raise BenchmarkError("no allowlisted metric with a value for both or either model was found")
    return parsed


# ----------------------------------------------------------------- cache ---

def blank_cache():
    return {"schema_version": SCHEMA_VERSION, "kind": CACHE_KIND, "parser_version": PARSER_VERSION,
            "registry": registry_digest(), "records": {}, "attempts": {}}


def _valid_number(v):
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and 0 <= v <= 100)


def validate_record(pair_id, rec):
    """Strict structural check of one cached record (raises BenchmarkError)."""
    pair = PAIR_BY_ID.get(pair_id)
    if pair is None or not isinstance(rec, dict):
        raise BenchmarkError("unknown pair %r" % pair_id)
    keys = {"url", "page", "fetched_at", "fetched_epoch", "content_sha256", "parser_version", "layout",
            "updated_on", "efforts", "metrics", "notes"}
    if set(rec) != keys:
        raise BenchmarkError("record fields")
    if rec["url"] != pair["url"] or rec["page"] != pair["page"] or rec["parser_version"] != PARSER_VERSION:
        raise BenchmarkError("record identity")
    if rec["layout"] != PROVIDERS[pair["provider"]]["layout"]:
        raise BenchmarkError("record layout")
    epoch = rec["fetched_epoch"]
    if (not isinstance(epoch, (int, float)) or isinstance(epoch, bool) or epoch != epoch
            or not 0 < epoch < 32503680000 or not isinstance(rec["content_sha256"], str)
            or not re.match(r"^[0-9a-f]{64}$", rec["content_sha256"])):
        raise BenchmarkError("record provenance")
    # The timestamp must be exactly the normalized form of the epoch this
    # tool wrote with it (no free text, no forged or inconsistent time).
    if not isinstance(rec["fetched_at"], str) or rec["fetched_at"] != _now_iso(epoch):
        raise BenchmarkError("record fetch time")
    if rec["updated_on"] is not None and not _real_date(rec["updated_on"]):
        raise BenchmarkError("record date")
    model_ids = {m for m, _n in pair["models"]}
    if not isinstance(rec["efforts"], dict) or set(rec["efforts"]) != model_ids or not all(
            e is None or e in EFFORTS for e in rec["efforts"].values()):
        raise BenchmarkError("record efforts")
    allowed = {s[0]: s for s in PROVIDERS[pair["provider"]]["metrics"]}
    if not isinstance(rec["metrics"], dict):
        raise BenchmarkError("record metrics")
    for key, m in rec["metrics"].items():
        spec = allowed.get(key)
        if (spec is None or not isinstance(m, dict)
                or set(m) != {"key", "label", "publisher", "unit", "kind", "values", "conflict", "estimated"}
                or m["key"] != key or m["unit"] != spec[3] or m["kind"] != spec[4]
                or not isinstance(m["label"], str) or not isinstance(m["conflict"], bool)
                or not isinstance(m["estimated"], bool) or not isinstance(m["values"], dict)
                or set(m["values"]) != model_ids):
            raise BenchmarkError("record metric %r" % key)
        if (m["label"], m["publisher"]) != expected_label(pair["provider"], spec):
            raise BenchmarkError("record metric label %r" % key)
        for v in m["values"].values():
            if (not isinstance(v, dict) or set(v) != {"value", "other_effort"} or not _valid_number(v["value"])
                    or not isinstance(v["other_effort"], bool)):
                raise BenchmarkError("record metric value %r" % key)
    if not isinstance(rec["notes"], list) or len(rec["notes"]) > 50 or not all(
            isinstance(n, str) and len(n) <= 300 and n == safe_text(n, 300) for n in rec["notes"]):
        raise BenchmarkError("record notes")  # only ever written sanitized; anything else is forged
    return rec


def _real_date(value):
    """True only for an existing calendar date written as YYYY-MM-DD."""
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return False
    try:
        return date(int(value[0:4]), int(value[5:7]), int(value[8:10])).isoformat() == value
    except ValueError:
        return False


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key")
        out[key] = value
    return out


def _reject_constant(name):
    raise ValueError("non-standard JSON constant %s" % name)


def read_cache(path):
    """(cache, warning_or_None). Anything malformed is ignored as a whole."""
    blank = blank_cache()
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return blank, None
    except OSError as ex:
        return blank, "third-party comparison cache unreadable (%s) — ignored" % type(ex).__name__
    try:
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(MAX_CACHE_BYTES + 1)
    except OSError as ex:
        return blank, "third-party comparison cache unreadable (%s) — ignored" % type(ex).__name__
    if len(raw) > MAX_CACHE_BYTES:
        return blank, "third-party comparison cache is too large — ignored"
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
        if (not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION
                or doc.get("kind") != CACHE_KIND):
            raise BenchmarkError("unsupported schema")
        if doc.get("parser_version") != PARSER_VERSION or doc.get("registry") != registry_digest():
            return blank, ("third-party comparison cache was written for another parser/registry version — "
                           "ignored until the next successful fetch")
        records, attempts = {}, {}
        if not isinstance(doc.get("records"), dict) or not isinstance(doc.get("attempts"), dict):
            raise BenchmarkError("malformed records/attempts")
        for pid, rec in doc["records"].items():
            records[pid] = validate_record(pid, rec)
        for pid, att in doc["attempts"].items():
            if pid in PAIR_BY_ID and isinstance(att, dict):
                err, at = att.get("last_error"), att.get("last_attempt_at")
                attempts[pid] = {"last_attempt_at": safe_text(at, 40) if isinstance(at, str) else None,
                                 "last_error": safe_text(err, 200) if isinstance(err, str) else None}
        return dict(blank, records=records, attempts=attempts), None
    except (BenchmarkError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as ex:
        return blank, "third-party comparison cache is invalid (%s) — ignored" % safe_text(ex, 120)


def _write_cache(path, cache):
    """Atomic private write (0600) of the evidence cache."""
    directory = os.path.dirname(path)
    if os.path.islink(directory) or os.path.islink(path):
        raise OSError("refusing to write through a symlink")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    data = json.dumps(cache, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    fd, tmp = tempfile.mkstemp(prefix=".benchmark-evidence.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------- refresh ---

def fetch_disabled_reason(offline=False, extra_disabled=None):
    if offline:
        return "offline: cached copies only (no network request)"
    if os.environ.get(FETCH_ENV, "").strip().lower() in ("0", "off", "false", "no"):
        return "network fetch disabled (%s=0): cached copies only" % FETCH_ENV
    return extra_disabled


def refresh(path, now, fetch, disabled_reason=None, fetcher=None):
    """fetch=True: GET every reviewed page not fetched within the last 5
    minutes (one shared deadline; failures keep the last valid copy) and
    save the cache. fetch=False: cache only (no network, no writes).
    Returns the comparison context."""
    cache, warning = read_cache(path)
    notes, changed = {}, False
    deadline = time.monotonic() + DEADLINE_SECONDS
    transport = fetcher or default_fetch
    for pair in PAIRS:
        pid = pair["id"]
        rec = cache["records"].get(pid)
        if not fetch:
            notes[pid] = disabled_reason or "cache only (no network)"
            continue
        if rec is not None and 0 <= now - rec["fetched_epoch"] < MIN_INTERVAL_SECONDS:
            notes[pid] = "reused a copy fetched within the last %d min" % (MIN_INTERVAL_SECONDS // 60)
            continue
        changed = True
        try:
            if time.monotonic() > deadline:
                raise BenchmarkError("skipped: the %ds deadline for all comparison pages was reached"
                                     % DEADLINE_SECONDS)
            raw = transport(pair["url"], TIMEOUT_SECONDS, MAX_BYTES, deadline)
            if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_BYTES:
                raise BenchmarkError("transport returned no bytes or too many")
            parsed = parse_page(pair, raw)
            new = {"url": pair["url"], "page": pair["page"], "fetched_at": _now_iso(now),
                   "fetched_epoch": float(now), "content_sha256": hashlib.sha256(bytes(raw)).hexdigest(),
                   "parser_version": PARSER_VERSION, "layout": parsed["layout"], "updated_on": parsed["updated_on"],
                   "efforts": parsed["efforts"], "metrics": parsed["metrics"],
                   "notes": [safe_text(n, 300) for n in parsed["notes"][:50]]}
            cache["records"][pid] = validate_record(pid, json.loads(json.dumps(new)))
            cache["attempts"][pid] = {"last_attempt_at": _now_iso(now), "last_error": None}
            notes[pid] = "refreshed now"
        except KeyboardInterrupt:
            raise
        except Exception as ex:  # noqa: BLE001 - a broken page/transport must never break p
            err = safe_text(ex, 200) or type(ex).__name__
            cache["attempts"][pid] = {"last_attempt_at": _now_iso(now), "last_error": err}
            notes[pid] = "REFRESH FAILED (%s): showing the last valid copy, if any" % err
    if changed:
        try:
            _write_cache(path, cache)
        except OSError as ex:
            warning = ((warning + "; ") if warning else "") + "could not save third-party comparisons (%s)" % (
                type(ex).__name__)
    return context(cache, now, notes, warning)


def context(cache, now, notes=None, warning=None):
    notes = notes or {}
    pairs = {}
    for pair in PAIRS:
        pid = pair["id"]
        rec = cache["records"].get(pid)
        if rec is None:
            status, age = "missing", None
        else:
            age = now - rec["fetched_epoch"]
            status = "future" if age < 0 else ("stale" if age > FRESH_SECONDS else "fresh")
        pairs[pid] = {"pair": pair, "provider": PROVIDERS[pair["provider"]], "record": rec, "status": status,
                      "age": age, "note": notes.get(pid) or "cache only (no network)",
                      "last_error": cache["attempts"].get(pid, {}).get("last_error")}
    payload = {"registry": registry_digest(),
               "pairs": [[pid, s["status"], _canonical_sha256(s["record"]) if s["record"] else None]
                         for pid, s in sorted(pairs.items())]}
    return {"pairs": pairs, "warning": warning, "available": True,
            "digest": hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()}


def unavailable_context(reason):
    """Context used when this module cannot be loaded or used at all."""
    reason = safe_text(reason, 200)
    return {"pairs": {}, "warning": reason, "available": False,
            "digest": hashlib.sha256(("unavailable:" + reason).encode("utf-8")).hexdigest()}


# ------------------------------------------------------------ comparison ---

def comparison(bctx, current, candidate, current_effort=None, candidate_effort=None):
    """Evidence for the exact pair (current model, candidate) from every
    reviewed source: {"fresh": [entry], "stale": [entry], "supports": bool,
    "mixed": bool, "providers_disagree": bool}.
    An entry lists each allowlisted metric with both values as published.
    A metric is `comparable` only when both values exist, neither was
    measured at another effort setting, the row is not duplicated and the
    aggregator does not mark it as estimated. An entry's `setup_matches`
    is True only when the page STATES the reasoning effort of both columns
    and they equal the route's levels (current_effort for the current
    model, candidate_effort for the candidate); an unstated or different
    effort is context only. `supports` = a comparable metric in a FRESH
    entry whose setup matches reports a higher value for the candidate
    (other metrics may favour the current model) — used only to OFFER an
    option, never to choose one."""
    out = {"fresh": [], "stale": [], "supports": False, "mixed": False, "providers_disagree": False}
    if not bctx or not bctx.get("available"):
        return out
    want = {current, candidate}
    for pid in sorted(bctx["pairs"]):
        st = bctx["pairs"][pid]
        pair, rec = st["pair"], st["record"]
        if {m for m, _n in pair["models"]} != want or rec is None:
            continue
        order = [s[0] for s in st["provider"]["metrics"]]
        metrics = []
        for key in sorted(rec["metrics"], key=order.index):
            m = rec["metrics"][key]
            cur, cand = m["values"][current], m["values"][candidate]
            comparable = (cur["value"] is not None and cand["value"] is not None and not cur["other_effort"]
                          and not cand["other_effort"] and not m["conflict"] and not m["estimated"])
            if cur["value"] is None and cand["value"] is None:
                continue
            why = None
            if not comparable:
                if m["conflict"]:
                    why = "row duplicated on the page"
                elif m["estimated"]:
                    why = "marked as estimated by the aggregator"
                elif cur["other_effort"] or cand["other_effort"]:
                    why = "measured at another effort setting"
                else:
                    why = "only one model has a value"
            metrics.append({"key": key, "label": m["label"], "publisher": m["publisher"], "unit": m["unit"],
                            "kind": m["kind"], "current": cur["value"], "candidate": cand["value"],
                            "comparable": comparable, "why_not": why})
        efforts = dict(rec["efforts"])
        setup_matches = (current_effort is not None and candidate_effort is not None
                         and efforts.get(current) == current_effort and efforts.get(candidate) == candidate_effort)
        entry = {"pair_id": pid, "provider": st["provider"]["label"], "limits": st["provider"]["limits"],
                 "page": pair["page"], "updated_on": rec["updated_on"], "fetched_at": rec["fetched_at"],
                 "age": st["age"], "status": st["status"], "efforts": efforts, "metrics": metrics,
                 "setup_matches": setup_matches}
        if st["status"] == "fresh":
            out["fresh"].append(entry)
            if setup_matches and any(x["comparable"] and x["candidate"] > x["current"] for x in metrics):
                out["supports"] = True
        else:
            out["stale"].append(entry)
    # Disclosures (never a verdict): listed comparable metrics point in both
    # directions, and/or two providers' own composite scores disagree.
    dirs = {(x["candidate"] > x["current"]) - (x["candidate"] < x["current"])
            for e in out["fresh"] for x in e["metrics"] if x["comparable"]}
    out["mixed"] = 1 in dirs and -1 in dirs
    by_provider = {}
    for e in out["fresh"]:
        for x in e["metrics"]:
            if x["comparable"] and x["kind"] == "composite" and x["candidate"] != x["current"]:
                by_provider.setdefault(e["provider"], set()).add(x["candidate"] > x["current"])
    seen = set()
    for value in by_provider.values():
        seen |= value
    out["providers_disagree"] = len(by_provider) > 1 and len(seen) > 1
    return out
