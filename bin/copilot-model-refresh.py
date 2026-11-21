#!/usr/bin/env python3
"""
copilot-model-refresh.py — review subagent model routing against CURRENT
official GitHub Copilot prices and, only after explicit confirmation, apply
local user overrides (copilot-s menu `p`).

What it does
- Fetches the official GitHub Copilot per-token pricing NOW, reusing
  copilot-task-report.py's fixed-URL HTTPS fetcher, strict parser,
  validation and cache (~/.copilot/task-reports/official-pricing-cache.json).
  An explicit refresh bypasses the automatic 24h TTL and 1h failure backoff
  (a snapshot fetched < 5 min ago is reused). On failure the last valid
  snapshot is shown, but nothing is proposed unless that snapshot is < 24h
  old.
- In the same run, fetches the OFFICIAL capability pages (supported-models:
  model list, 1M-context / configurable-reasoning table, per-client table;
  model-comparison: "Recommended models by task") from fixed docs.github.com
  article-body URLs (HTTPS, certificate verification on, redirects refused,
  bounded size/time, verified system-curl fallback only after a Python TLS
  certificate error) and parses them with a strict, versioned stdlib table
  parser into a cached, timestamped, digest-stamped evidence snapshot
  (<COPILOT_HOME>/model-refresh/capability-evidence.json). Unrecognized
  layouts, duplicate/conflicting rows and unknown cell values become
  UNKNOWN (never guessed); stale (> source TTL) or failed evidence is shown
  but never authorizes anything. Remote text is sanitized and is never
  written into generated instructions.
- Evaluates the ten effective routes (seven custom agents, the small/casual
  coder route, the stronger reviewer override and the built-in `task`
  route). Per model and field, fresh live official evidence is used; when
  it is unavailable the dated curated catalog (config/model-role-catalog.json,
  optionally extended by a dated local overlay) is the documented fallback.
  Live evidence that contradicts your LOCAL overlay fails closed; live
  evidence that differs from the shipped catalog is shown as a change.
  Nothing is inferred from price or newness; a model without an official
  task area and Category from either is listed as unassessed and never
  assigned.
- Lists the whole visible model universe (official pricing + official
  capability pages + catalog + your verified records + current routes),
  including unpriced/undocumented models as unknown, with availability
  shown as verified / unverified / not in your complete list — never as a
  claim about your account.
- Besides the cost-led proposals (unchanged 10% rule), lists OPTIONAL
  alternatives per route that are not >= 10% cheaper (possibly more
  expensive) but pass every gate AND have a concrete documented
  differentiator against the current model (e.g. official Copilot CLI
  support the current model lacks). They are never selected automatically;
  `select` adds chosen ones to a frozen proposal, which still needs `yes`.
- Proposes a change automatically only for an unpinned route whose
  replacement passes every evidence gate, is listed in the user's verified availability list
  (Copilot CLI offers no documented programmatic model catalog to verify
  availability), has the route's SPECIFIC reasoning level verified for that
  model (source-backed catalog `reasoning_levels` or the user's dated
  `verified_capabilities` / recorded levels; the generic official
  "configurable reasoning" flag is never taken as proof of a level —
  unknown fails closed), and is
  at least 10% cheaper under a disclosed blended workload assumption on the
  Default-tier rates and, for long_context routes, also on the rates billed
  above the long-context threshold. Fit is categorical, never a measured or
  equal-quality claim. Shipped routing defaults (config/agent-routing.yaml,
  agents/*.agent.md, instructions) are NEVER modified.
- `propose` freezes the proposal with digests of every input it read and an
  evidence digest (pricing snapshot, capability evidence, availability
  records, per-route decisions and alternatives); `select` and `apply`
  re-derive everything from the caches and refuse (change nothing) if any
  of it changed, the proposal expired, was altered, or was already applied.
- `record-verified` (interactive, terminal only) records the model IDs and
  reasoning levels YOU saw in Copilot CLI's /model picker into a separate
  tool-written file (model-refresh/verified-models.json, dated, 30-day
  validity) after an exact `yes`; user-config.json is never written.

Effective resolution (same files for every route/context):
  shipped defaults (agent-routing.yaml)
    -> managed overrides (<COPILOT_HOME>/model-refresh/state.json)
    -> human pins: native `/subagents` values in <COPILOT_HOME>/settings.json
       that this tool did not write (agent default routes only), then
       `pins` in <COPILOT_HOME>/model-refresh/user-config.json (any route).

Applying writes, as one journaled group (rolled back on failure, recovered
on the next run after a crash):
  - <COPILOT_HOME>/settings.json  subagents.agents.<agent>.{model,
    effortLevel,contextTier} — only for agent default routes with a managed
    override; user-set values are never overwritten; unrelated settings are
    preserved (the file is re-serialized as 2-space JSON);
  - <COPILOT_HOME>/instructions/copilot-toolkit-model-routing.instructions.md
    — a generated personal instruction with the effective table (validated
    model IDs / efforts / tiers / OMIT only, no prices or remote text),
    present only while a managed override or user-config pin exists; a
    field a human set in /subagents is written as OMIT, never frozen;
  - <COPILOT_HOME>/model-refresh/effective-routing.json (same lifetime as
    the generated instruction) and state.json.
Backups of every replaced file are kept under
<COPILOT_HOME>/model-refresh/backups/<txn>/ (last 10 transactions).

Honest limits
- Copilot CLI documents subagents.agents.<agent> (model/effortLevel/
  contextTier, "inherit" allowed) but not its precedence against explicit
  task-tool parameters, and one per-agent value cannot express the
  small/casual coder or stronger-reviewer contexts. The shipped
  instructions therefore tell the orchestrator to run `status
  --instructions` (live, read-only) before each toolkit delegation and to
  omit every field printed as OMIT (the task tool has no "inherit" value);
  the generated table is the fallback. Instruction following is not a hard
  guarantee.
- Availability is only as good as the user's verified lists
  (user-config.json `available_models` and/or the recorded
  verified-models.json): Copilot CLI documents no programmatic per-account
  model or reasoning-level listing, and the official per-client/per-plan
  tables do not reflect organization policies.
- The official docs document configurable reasoning only as yes/no; the
  specific levels are never taken from them.
- The blended cost is an assumption-based comparison aid, not a forecast
  and not your bill.

Commands: status [--instructions] | preview [--offline] | propose [--offline] |
          select --proposal-id ID --choose N[,N...] | apply --proposal-id ID |
          discard --proposal-id ID | record-verified | reset [--yes]
Exit codes: 0 ok (propose: changes pending; record-verified: saved), 2 error,
            3 no changes (propose: may still print a PROPOSAL id when optional
            alternatives exist; record-verified: nothing saved),
            4 refused (stale proposal, concurrent edit, unsafe target),
            130 interrupted (nothing half-written).
Machine lines on stdout (propose/select): PROPOSAL<TAB>id,
  ALTERNATIVE<TAB>n<TAB>route<TAB>model, VERIFY<TAB>needed.
User-maintained input (never written by this tool):
  <COPILOT_HOME>/model-refresh/user-config.json — available_models
  {verified_on, models}, verified_capabilities {verified_on, models:
  {model: {reasoning_levels}}}, pins {route: {model?, reasoning_effort?,
  context_tier?}}, optional dated local catalog overlay.
Network: only the fixed official docs.github.com URLs above (no auth, no
  cookies, nothing sent but the GET). Disable with
  COPILOT_MODEL_REFRESH_DOCS_FETCH=0 (or COPILOT_TASK_REPORT_PRICING_FETCH=0,
  which disables all fetching).
Stdlib only.
"""

import argparse
import contextlib
import copy
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import shutil
import signal
import stat
import sys
import tempfile
import time
from datetime import date, datetime, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

SCHEMA_VERSION = 1
STATE_KIND = "copilot-toolkit-model-refresh-state"
PROPOSAL_KIND = "copilot-toolkit-model-refresh-proposal"
JOURNAL_KIND = "copilot-toolkit-model-refresh-journal"
EFFECTIVE_KIND = "copilot-toolkit-effective-routing"
CATALOG_KIND = "copilot-toolkit-model-role-catalog"
USER_CONFIG_KIND = "copilot-toolkit-model-refresh-user-config"
SEEN_KIND = "copilot-toolkit-model-refresh-seen-models"
CAPABILITY_KIND = "copilot-toolkit-model-capability-evidence"
VERIFIED_KIND = "copilot-toolkit-model-refresh-verified-models"
PROPOSAL_FORMAT = 2

EXIT_OK = 0
EXIT_ERROR = 2
EXIT_NO_CHANGES = 3
EXIT_REFUSED = 4

MODEL_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.\-]{0,78}[a-z0-9])?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROPOSAL_ID_RE = re.compile(r"^[0-9a-f]{16}$")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
CONTEXT_TIERS = ("default", "long_context")
INHERIT = "inherit"
CATEGORY_RANK = {"lightweight": 1, "versatile": 2, "powerful": 3}
CLI_CLIENT_VALUES = ("included", "not_included", "not_listed")
FIELDS = ("model", "reasoning_effort", "context_tier")
# Toolkit field -> native settings.json subagents.agents.<agent> field.
SETTINGS_FIELDS = (("model", "model"), ("reasoning_effort", "effortLevel"), ("context_tier", "contextTier"))

ROUTES = (
    {"key": "discovery", "agent": "discovery", "label": "discovery", "kind": "agent"},
    {"key": "planner", "agent": "planner", "label": "planner", "kind": "agent"},
    {"key": "coder", "agent": "coder", "label": "coder", "kind": "agent"},
    {"key": "coder.small_casual", "agent": "coder", "label": "coder (small/casual)", "kind": "context"},
    {"key": "senior-coder", "agent": "senior-coder", "label": "senior-coder", "kind": "agent"},
    {"key": "reviewer", "agent": "reviewer", "label": "reviewer (default)", "kind": "agent"},
    {"key": "reviewer.stronger", "agent": "reviewer", "label": "reviewer (stronger override)", "kind": "context"},
    {"key": "tester", "agent": "tester", "label": "tester", "kind": "agent"},
    {"key": "test-reviewer", "agent": "test-reviewer", "label": "test-reviewer", "kind": "agent"},
    {"key": "task", "agent": "task", "label": "task (built-in)", "kind": "builtin"},
)
ROUTE_BY_KEY = {r["key"]: r for r in ROUTES}
AGENT_NAMES = tuple(r["agent"] for r in ROUTES if r["kind"] == "agent")

SOURCE_LABELS = {
    "default": "shipped default",
    "managed": "managed override",
    "pin:/subagents": "pinned (/subagents)",
    "pin:user-config": "pinned (user-config)",
}

# Disclosed comparison assumption (NOT measured): agentic sessions re-send
# mostly cached context. Share of 1M tokens billed at each rate.
WORKLOAD_MIX = (("input", 0.15), ("cached_input", 0.75), ("cache_write", 0.05), ("output", 0.05))
MIN_SAVING_FRACTION = 0.10
AVAILABILITY_MAX_AGE_DAYS = 30
PROPOSAL_TTL_SECONDS = 15 * 60
LOCK_TIMEOUT_SECONDS = 10
MAX_HISTORY = 20
MAX_BACKUP_SETS = 10
MAX_JSON_BYTES = 2_000_000

STATE_DIRNAME = "model-refresh"
INSTRUCTIONS_FILENAME = "copilot-toolkit-model-routing.instructions.md"
INSTRUCTIONS_MARKER = "<!-- copilot-toolkit:model-refresh:generated v1 -->"
INSTRUCTIONS_TITLE = "Toolkit effective subagent routing (generated)"

# Injection points for tests/offline fixtures (never needed in normal use):
# PRICING_MODULE — a pre-loaded copilot-task-report module;
# PRICING_FETCHER — callable (url, timeout, max_bytes) -> bytes handed to
# that module's existing PRICING_FETCHER hook (same fixed URL/validation).
PRICING_MODULE = None
PRICING_FETCHER = None

# Official capability pages (fixed URLs; the docs.github.com article-body API
# returns the same Markdown the pricing fetcher parses). Each source has its
# own freshness limit: evidence older than ttl_seconds is shown as stale and
# never authorizes a change.
DOCS_API_PREFIX = "https://docs.github.com/api/article/body?pathname="
DOCS_PUBLIC_PREFIX = "https://docs.github.com"
CAPABILITY_SOURCES = (
    {"key": "supported_models", "label": "supported-models",
     "pathname": "/en/copilot/reference/ai-models/supported-models", "ttl_seconds": 3 * 86400,
     "sections": (("listed", "Supported AI models in Copilot"),
                  ("extended", "Models with extended capabilities"),
                  ("clients", "Supported AI models per client"))},
    {"key": "model_comparison", "label": "model-comparison",
     "pathname": "/en/copilot/reference/ai-models/model-comparison", "ttl_seconds": 7 * 86400,
     "sections": (("tasks", "Recommended models by task"),)},
)
CAPABILITY_SOURCE_BY_KEY = {s["key"]: s for s in CAPABILITY_SOURCES}
CAPABILITY_PARSER_VERSION = 1
CAPABILITY_MIN_INTERVAL_SECONDS = 300
CAPABILITY_TIMEOUT_SECONDS = 10
CAPABILITY_DEADLINE_SECONDS = 20
CAPABILITY_MAX_BYTES = 2_000_000
CAPABILITY_MAX_ROWS = 400
DOCS_FETCH_ENV = "COPILOT_MODEL_REFRESH_DOCS_FETCH"
# Injectable transport for tests/offline fixtures: callable
# (url, timeout_seconds, max_bytes) -> bytes. When a PRICING_FETCHER is
# injected but no CAPABILITY_FETCHER, capability pages are NOT fetched at
# all (fixtures never cause live traffic).
CAPABILITY_FETCHER = None


def capability_url(source):
    return DOCS_API_PREFIX + source["pathname"]


def capability_public_url(source):
    return DOCS_PUBLIC_PREFIX + source["pathname"]


class RefreshError(Exception):
    def __init__(self, message, code=EXIT_ERROR):
        Exception.__init__(self, message)
        self.code = code


# ---------------------------------------------------------------- paths ---

def _script_dir():
    return os.path.dirname(os.path.realpath(os.path.abspath(__file__)))


def copilot_home():
    env = os.environ.get("COPILOT_HOME", "").strip()
    return os.path.abspath(os.path.expanduser(env)) if env else os.path.join(os.path.expanduser("~"), ".copilot")


class Paths(object):
    def __init__(self, home=None):
        self.copilot_home = home or copilot_home()
        self.state_dir = os.path.join(self.copilot_home, STATE_DIRNAME)
        self.state = os.path.join(self.state_dir, "state.json")
        self.proposal = os.path.join(self.state_dir, "pending-proposal.json")
        self.journal = os.path.join(self.state_dir, "apply-journal.json")
        self.lock = os.path.join(self.state_dir, ".lock")
        self.backups = os.path.join(self.state_dir, "backups")
        self.seen = os.path.join(self.state_dir, "seen-models.json")
        self.effective = os.path.join(self.state_dir, "effective-routing.json")
        self.user_config = os.path.join(self.state_dir, "user-config.json")
        self.capabilities = os.path.join(self.state_dir, "capability-evidence.json")
        self.verified = os.path.join(self.state_dir, "verified-models.json")
        self.proposal_key = os.path.join(self.state_dir, ".proposal-key")
        self.settings = os.path.join(self.copilot_home, "settings.json")
        self.instructions_dir = os.path.join(self.copilot_home, "instructions")
        self.instructions = os.path.join(self.instructions_dir, INSTRUCTIONS_FILENAME)

    def first_existing(self, env_name, home_name, repo_relative):
        env = os.environ.get(env_name, "").strip()
        if env:
            return env
        candidates = [os.path.join(self.copilot_home, home_name),
                      os.path.join(os.path.expanduser("~"), ".copilot", home_name),
                      os.path.join(_script_dir(), os.pardir, repo_relative)]
        for path in candidates:
            if os.path.isfile(path):
                return os.path.normpath(path)
        return os.path.normpath(candidates[-1])

    def routing_config(self):
        return self.first_existing("COPILOT_MODEL_REFRESH_ROUTING", "agent-routing.yaml",
                                   os.path.join("config", "agent-routing.yaml"))

    def catalog(self):
        return self.first_existing("COPILOT_MODEL_REFRESH_CATALOG", "model-role-catalog.json",
                                   os.path.join("config", "model-role-catalog.json"))


def load_pricing_module():
    """The shared official-pricing implementation in copilot-task-report.py
    (same resolution order as copilot-s: env override, sibling, ~/.local/bin)."""
    global PRICING_MODULE
    if PRICING_MODULE is None:
        env = os.environ.get("COPILOT_TASK_REPORT_HELPER", "").strip()
        sibling = os.path.join(_script_dir(), "copilot-task-report.py")
        fallback = os.path.join(os.path.expanduser("~"), ".local", "bin", "copilot-task-report.py")
        path = env or (sibling if os.path.isfile(sibling) else fallback)
        if not os.path.isfile(path):
            raise RefreshError("copilot-task-report.py (shared official pricing code) not found at %s; set "
                               "COPILOT_TASK_REPORT_HELPER or re-run the installer" % path)
        spec = importlib.util.spec_from_file_location("copilot_task_report_shared", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "refresh_official_pricing_now"):
            raise RefreshError("%s predates explicit pricing refresh support; update the toolkit (scripts/update.sh) "
                               "or re-run the installer" % path)
        PRICING_MODULE = module
    if PRICING_FETCHER is not None:
        PRICING_MODULE.PRICING_FETCHER = PRICING_FETCHER
    return PRICING_MODULE


# ----------------------------------------------------------- file helpers ---

def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON key %r" % key)
        out[key] = value
    return out


def _reject_constant(name):
    raise ValueError("non-standard JSON constant %s" % name)


def parse_json_bytes(raw, where):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                          parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as ex:
        raise RefreshError("%s is not valid strict JSON (%s)" % (where, ex))


def read_bytes(path, max_bytes=MAX_JSON_BYTES):
    """Bytes of a regular file, or None if missing. Refuses oversized files."""
    if not os.path.lexists(path):
        return None
    with open(path, "rb") as f:
        raw = f.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise RefreshError("%s is larger than %d bytes" % (path, max_bytes))
    return raw


def sha256_bytes(raw):
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def file_sha256(path):
    try:
        return sha256_bytes(read_bytes(path, max_bytes=50_000_000))
    except OSError:
        return "unreadable"


def json_bytes(obj):
    return (json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False) + "\n").encode("utf-8")


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path, data, mode=0o600):
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(directory)


def ensure_private_dir(path):
    if os.path.islink(path):
        raise RefreshError("refusing to use %s: it is a symlink" % path, EXIT_REFUSED)
    os.makedirs(path, mode=0o700, exist_ok=True)
    if not os.path.isdir(path):
        raise RefreshError("%s is not a directory" % path, EXIT_REFUSED)


def now_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_local(epoch):
    """The user's LOCAL calendar date at `epoch` (honors TZ). Every user- or
    catalog-entered YYYY-MM-DD (reviewed_on, retrieved_on, expires_on,
    verified_on) is a local calendar date, so comparing against the UTC date
    would wrongly call today's local date "future" east of UTC (or accept
    tomorrow's west of it). No tolerance is added: future stays future."""
    return datetime.fromtimestamp(epoch).date()


def parse_date(value, where):
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise RefreshError("%s must be a YYYY-MM-DD date" % where)
    try:
        return date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        raise RefreshError("%s is not a valid date: %r" % (where, value))


def fmt_age(seconds):
    if seconds is None:
        return "unknown age"
    if seconds < 0:
        return "dated in the future"
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 172800:
        return "%.1fh" % (seconds / 3600.0)
    return "%.1fd" % (seconds / 86400.0)


# Terminal/C1 control characters, zero-width and bidi overrides: never
# echoed from remote documents or exception text.
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def _safe_text(text, limit=200):
    text = re.sub(r"\s+", " ", _CONTROL_RE.sub(" ", str(text))).strip()
    return text if len(text) <= limit else text[:limit - 3] + "..."


# ---------------------------------------------------------- validation ---

def valid_model_id(value):
    return isinstance(value, str) and bool(MODEL_ID_RE.match(value))


def validate_route_values(values, where, allow_partial=False, allow_inherit=False):
    if not isinstance(values, dict):
        raise RefreshError("%s must be an object" % where)
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise RefreshError("%s has unknown field(s): %s" % (where, ", ".join(sorted(unknown))))
    if not allow_partial and set(values) != set(FIELDS):
        raise RefreshError("%s must define %s" % (where, ", ".join(FIELDS)))
    for field, value in values.items():
        if allow_inherit and value == INHERIT:
            continue
        ok = (valid_model_id(value) if field == "model"
              else value in (EFFORTS if field == "reasoning_effort" else CONTEXT_TIERS))
        if not ok:
            raise RefreshError("%s.%s has an invalid value %r" % (where, field, value))
    return dict(values)


# ------------------------------------------------ routing defaults (YAML) ---

def _strip_yaml_comment(line):
    if line.lstrip().startswith("#"):
        return ""
    idx = line.find(" #")
    return line if idx < 0 else line[:idx]


def _yaml_scalar(text, where):
    text = text.strip()
    if not text:
        raise RefreshError("%s: empty value" % where)
    if text[0] in "\"'&*!|>%@`":
        raise RefreshError("%s: unsupported YAML value %r" % (where, text))
    if text == "true":
        return True
    if text == "false":
        return False
    return text


def _yaml_value(text, where):
    text = text.strip()
    if text.startswith("{"):
        if not text.endswith("}"):
            raise RefreshError("%s: unterminated flow mapping" % where)
        out = {}
        body = text[1:-1].strip()
        for part in [p for p in body.split(",") if p.strip()] if body else []:
            key, sep, value = part.partition(":")
            if not sep or not key.strip():
                raise RefreshError("%s: malformed flow mapping entry %r" % (where, part))
            if key.strip() in out:
                raise RefreshError("%s: duplicate key %r" % (where, key.strip()))
            out[key.strip()] = _yaml_scalar(value, where)
        return out
    if text.startswith("["):
        if not text.endswith("]"):
            raise RefreshError("%s: unterminated flow list" % where)
        body = text[1:-1].strip()
        return [_yaml_scalar(p, where) for p in body.split(",")] if body else []
    return _yaml_scalar(text, where)


def parse_routing_yaml(text):
    """Minimal parser for the YAML subset used by config/agent-routing.yaml
    (block mappings, one-line flow maps/lists, plain scalars). Anything else
    is rejected rather than guessed."""
    root = {}
    stack = [(-1, root)]
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_yaml_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if line[indent:indent + 1] == "\t":
            raise RefreshError("agent-routing.yaml line %d: tabs are not supported" % lineno)
        content = line.strip()
        m = re.match(r"^([A-Za-z0-9_.-]+):(?:\s+(.*))?$", content)
        if not m:
            raise RefreshError("agent-routing.yaml line %d: unsupported YAML %r" % (lineno, content))
        key, value = m.group(1), m.group(2)
        while indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if not isinstance(parent, dict):
            raise RefreshError("agent-routing.yaml line %d: unexpected nesting" % lineno)
        if key in parent:
            raise RefreshError("agent-routing.yaml line %d: duplicate key %r" % (lineno, key))
        if value is None or not value.strip():
            child = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _yaml_value(value, "agent-routing.yaml line %d" % lineno)
    return root


def _pick(mapping, keys, where):
    node = mapping
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            raise RefreshError("agent-routing.yaml: missing %s" % ".".join(keys[:keys.index(key) + 1]))
        node = node[key]
    if not isinstance(node, dict):
        raise RefreshError("agent-routing.yaml: %s must be a mapping" % where)
    return node


def shipped_defaults(routing):
    """{route_key: {model, reasoning_effort, context_tier}} from the parsed
    shipped routing defaults (read-only; never modified)."""
    out = {}
    for route in ROUTES:
        key = route["key"]
        if route["kind"] == "agent":
            node = _pick(routing, ["agents", key, "copilot"], key)
        elif key == "task":
            node = _pick(routing, ["built_in_tools", "task", "copilot"], key)
        elif key == "coder.small_casual":
            node = _pick(routing, ["task_routing", "small_casual_implementation"], key)
        else:
            node = _pick(routing, ["task_routing", "code_review", "stronger_override"], key)
        if route["kind"] == "context" and node.get("agent") != route["agent"]:
            raise RefreshError("agent-routing.yaml: %s must route to agent %r" % (key, route["agent"]))
        out[key] = validate_route_values({f: node.get(f) for f in FIELDS}, "agent-routing.yaml %s" % key)
    return out


def load_defaults(paths):
    path = paths.routing_config()
    raw = read_bytes(path)
    if raw is None:
        raise RefreshError("shipped routing defaults not found at %s" % path)
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise RefreshError("%s is not UTF-8" % path)
    return shipped_defaults(parse_routing_yaml(text)), path, sha256_bytes(raw)


# --------------------------------------------------------------- catalog ---

def _validate_levels(levels, where):
    if (not isinstance(levels, list) or not levels or len(set(levels)) != len(levels)
            or not all(level in EFFORTS for level in levels)):
        raise RefreshError("%s must be a non-empty list of distinct values from %s" % (where, ", ".join(EFFORTS)))
    return list(levels)


def _validate_catalog_models(models, sources, where):
    if not isinstance(models, dict):
        raise RefreshError("%s.models must be an object" % where)
    out = {}
    for model_id, entry in models.items():
        w = "%s.models[%r]" % (where, model_id)
        if not valid_model_id(model_id):
            raise RefreshError("%s: invalid model id" % w)
        if not isinstance(entry, dict):
            raise RefreshError("%s must be an object" % w)
        required = ("display_name", "provider", "category", "task_area", "one_million_context",
                    "configurable_reasoning", "copilot_cli_client", "sources")
        optional = ("reasoning_levels",)
        missing = [k for k in required if k not in entry]
        if missing:
            raise RefreshError("%s is missing %s" % (w, ", ".join(missing)))
        unknown = set(entry) - set(required) - set(optional)
        if unknown:
            raise RefreshError("%s has unknown field(s): %s" % (w, ", ".join(sorted(unknown))))
        if "reasoning_levels" in entry:
            # Only for levels a cited source documents for THIS model; never
            # derived from the generic configurable_reasoning flag.
            _validate_levels(entry["reasoning_levels"], "%s.reasoning_levels" % w)
        for key in ("display_name", "provider", "task_area"):
            if not isinstance(entry[key], str) or not entry[key].strip() or len(entry[key]) > 200:
                raise RefreshError("%s.%s must be a non-empty string" % (w, key))
        if not isinstance(entry["category"], str) or entry["category"].lower() not in CATEGORY_RANK:
            raise RefreshError("%s.category must be one of Lightweight/Versatile/Powerful" % w)
        for key in ("one_million_context", "configurable_reasoning"):
            if not isinstance(entry[key], bool):
                raise RefreshError("%s.%s must be true or false" % (w, key))
        if entry["copilot_cli_client"] not in CLI_CLIENT_VALUES:
            raise RefreshError("%s.copilot_cli_client must be one of %s" % (w, ", ".join(CLI_CLIENT_VALUES)))
        srcs = entry["sources"]
        if not isinstance(srcs, list) or not srcs or not all(isinstance(s, str) and s in sources for s in srcs):
            raise RefreshError("%s.sources must list keys of %s.sources" % (w, where))
        out[model_id] = dict(entry)
    return out


def validate_catalog(raw, where, require_kind=True):
    if not isinstance(raw, dict):
        raise RefreshError("%s must be a JSON object" % where)
    allowed = {"schema_version", "kind", "reviewed_on", "expires_on", "notes", "sources", "routes", "models"}
    unknown = set(raw) - allowed
    if unknown:
        raise RefreshError("%s has unknown field(s): %s" % (where, ", ".join(sorted(unknown))))
    if require_kind and (raw.get("schema_version") != SCHEMA_VERSION or raw.get("kind") != CATALOG_KIND):
        raise RefreshError("%s: unsupported catalog schema" % where)
    reviewed = parse_date(raw.get("reviewed_on"), "%s.reviewed_on" % where)
    expires = parse_date(raw.get("expires_on"), "%s.expires_on" % where)
    if expires < reviewed:
        raise RefreshError("%s.expires_on precedes reviewed_on" % where)
    sources = raw.get("sources", {})
    if not isinstance(sources, dict):
        raise RefreshError("%s.sources must be an object" % where)
    for name, src in sources.items():
        if (not isinstance(src, dict) or not isinstance(src.get("url"), str)
                or not src["url"].startswith("https://")):
            raise RefreshError("%s.sources[%r] needs an https url" % (where, name))
        parse_date(src.get("retrieved_on"), "%s.sources[%r].retrieved_on" % (where, name))
    routes = raw.get("routes", {})
    if not isinstance(routes, dict):
        raise RefreshError("%s.routes must be an object" % where)
    for key, policy in routes.items():
        w = "%s.routes[%r]" % (where, key)
        if key not in ROUTE_BY_KEY:
            raise RefreshError("%s: unknown route" % w)
        if not isinstance(policy, dict) or set(policy) - {"accepted_task_areas", "min_category", "note"}:
            raise RefreshError("%s may only define accepted_task_areas, min_category, note" % w)
        areas = policy.get("accepted_task_areas")
        if not isinstance(areas, list) or not areas or not all(isinstance(a, str) and a.strip() for a in areas):
            raise RefreshError("%s.accepted_task_areas must be a non-empty list of strings" % w)
        if "min_category" in policy and str(policy["min_category"]).lower() not in CATEGORY_RANK:
            raise RefreshError("%s.min_category must be Lightweight/Versatile/Powerful" % w)
    models = _validate_catalog_models(raw.get("models", {}), sources, where)
    return {"reviewed_on": reviewed, "expires_on": expires, "sources": sources,
            "routes": routes, "models": models}


def load_catalog(paths, user_config):
    """Shipped catalog plus the optional dated local overlay. Each model and
    route policy keeps its origin and expiry. Returns (catalog, digest)."""
    path = paths.catalog()
    raw = read_bytes(path)
    if raw is None:
        raise RefreshError("model role catalog not found at %s" % path)
    shipped = validate_catalog(parse_json_bytes(raw, path), "catalog %s" % path)
    merged = {"models": {}, "routes": {}, "origins": []}
    origin = {"name": "shipped", "path": path, "reviewed_on": shipped["reviewed_on"],
              "expires_on": shipped["expires_on"], "sources": shipped["sources"]}
    merged["origins"].append(origin)
    for model_id, entry in shipped["models"].items():
        merged["models"][model_id] = dict(entry, origin=origin)
    for key, policy in shipped["routes"].items():
        merged["routes"][key] = dict(policy, origin=origin)
    local = user_config.get("catalog")
    digest_parts = [sha256_bytes(raw)]
    if local is not None:
        local_v = validate_catalog(local, "user-config.json catalog", require_kind=False)
        lorigin = {"name": "local", "path": paths.user_config, "reviewed_on": local_v["reviewed_on"],
                   "expires_on": local_v["expires_on"], "sources": local_v["sources"]}
        merged["origins"].append(lorigin)
        for model_id, entry in local_v["models"].items():
            merged["models"][model_id] = dict(entry, origin=lorigin)
        for key, policy in local_v["routes"].items():
            merged["routes"][key] = dict(policy, origin=lorigin)
        digest_parts.append(hashlib.sha256(json.dumps(local, sort_keys=True).encode("utf-8")).hexdigest())
    return merged, hashlib.sha256("|".join(digest_parts).encode("ascii")).hexdigest()


def origin_unusable(origin, today):
    """Why a catalog origin (shipped or local overlay) may NOT authorize a
    replacement today, or None. Fails closed on dates that cannot be true
    yet or are inconsistent: a review or source retrieval dated after today,
    a source retrieved after the review that cites it, an expiry not after
    the review, or an expired review."""
    name = origin["name"]
    if origin["reviewed_on"] > today:
        return "%s catalog review is dated in the future (%s)" % (name, origin["reviewed_on"].isoformat())
    if origin["expires_on"] <= origin["reviewed_on"]:
        return "%s catalog expires_on is not after its reviewed_on" % name
    for src_name in sorted(origin["sources"]):
        retrieved = parse_date(origin["sources"][src_name].get("retrieved_on"), "source %r retrieved_on" % src_name)
        if retrieved > today:
            return "%s catalog source %r retrieved_on is in the future (%s)" % (name, src_name, retrieved.isoformat())
        if retrieved > origin["reviewed_on"]:
            return "%s catalog source %r was retrieved after the review that cites it" % (name, src_name)
    if origin["expires_on"] < today:
        return "%s catalog review expired %s" % (name, origin["expires_on"].isoformat())
    return None


# ----------------------------------------------------------- user config ---

def load_user_config(paths):
    """User-maintained (never written by this tool). Missing -> empty."""
    raw = read_bytes(paths.user_config)
    if raw is None:
        return {}, None
    data = parse_json_bytes(raw, paths.user_config)
    where = paths.user_config
    if not isinstance(data, dict):
        raise RefreshError("%s must be a JSON object" % where)
    unknown = set(data) - {"schema_version", "kind", "available_models", "verified_capabilities", "pins",
                           "catalog", "notes"}
    if unknown:
        raise RefreshError("%s has unknown field(s): %s" % (where, ", ".join(sorted(unknown))))
    if data.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise RefreshError("%s: unsupported schema_version" % where)
    if data.get("kind", USER_CONFIG_KIND) != USER_CONFIG_KIND:
        raise RefreshError("%s: unexpected kind" % where)
    avail = data.get("available_models")
    if avail is not None:
        if not isinstance(avail, dict) or set(avail) - {"verified_on", "models", "note"}:
            raise RefreshError("%s.available_models must be {verified_on, models}" % where)
        parse_date(avail.get("verified_on"), "%s.available_models.verified_on" % where)
        models = avail.get("models")
        if not isinstance(models, list) or not all(valid_model_id(m) for m in models):
            raise RefreshError("%s.available_models.models must be a list of model IDs" % where)
    caps = data.get("verified_capabilities")
    if caps is not None:
        if not isinstance(caps, dict) or set(caps) - {"verified_on", "models", "note"}:
            raise RefreshError("%s.verified_capabilities must be {verified_on, models}" % where)
        parse_date(caps.get("verified_on"), "%s.verified_capabilities.verified_on" % where)
        cmodels = caps.get("models")
        if not isinstance(cmodels, dict):
            raise RefreshError("%s.verified_capabilities.models must be an object" % where)
        for model_id, rec in cmodels.items():
            w = "%s.verified_capabilities.models[%r]" % (where, model_id)
            if not valid_model_id(model_id):
                raise RefreshError("%s: invalid model id" % w)
            if not isinstance(rec, dict) or set(rec) != {"reasoning_levels"}:
                raise RefreshError("%s must be {\"reasoning_levels\": [...]}" % w)
            _validate_levels(rec["reasoning_levels"], "%s.reasoning_levels" % w)
    pins = data.get("pins", {})
    if not isinstance(pins, dict):
        raise RefreshError("%s.pins must be an object" % where)
    for key, values in pins.items():
        if key not in ROUTE_BY_KEY:
            raise RefreshError("%s.pins: unknown route %r (known: %s)" % (where, key, ", ".join(ROUTE_BY_KEY)))
        validate_route_values(values, "%s.pins[%r]" % (where, key), allow_partial=True)
    return data, sha256_bytes(raw)


def availability(user_config, today, verified=None):
    """(set_of_models or None, description, usable_bool). Combines the
    user-maintained `available_models` list and the tool-recorded (after
    explicit confirmation) verified-models.json; a model counts as
    available when it is in ANY fresh (<= 30 days, not future-dated) list —
    except that a fresh recorded list you marked COMPLETE overrides an
    older-or-same-day user-config list: a model it omits is excluded unless
    user-config.json was verified strictly later (same day = ambiguous =
    excluded, fail closed)."""
    parts, fresh_models, stale_models, any_list = [], set(), set(), False
    uc_fresh, uc_date = set(), None
    avail = user_config.get("available_models")
    if avail:
        any_list = True
        verified_on = parse_date(avail["verified_on"], "available_models.verified_on")
        age = (today - verified_on).days
        models = set(avail["models"])
        if age < 0:
            parts.append("availability list dated in the future (%s) — not used" % avail["verified_on"])
            stale_models |= models
        elif age > AVAILABILITY_MAX_AGE_DAYS:
            parts.append("availability list verified %s is older than %d days — re-verify before replacements"
                         % (avail["verified_on"], AVAILABILITY_MAX_AGE_DAYS))
            stale_models |= models
        else:
            parts.append("verified %s (%d model(s))" % (avail["verified_on"], len(models)))
            uc_fresh, uc_date = models, verified_on
    if verified:
        any_list = True
        models = set(verified["models"])
        if verified_record_fresh(verified, today):
            parts.append("recorded from /model %s (%d model(s)%s)" % (
                verified["verified_on"], len(models), ", complete list" if verified.get("complete_list") else ""))
            fresh_models |= models
            rec_date = parse_date(verified["verified_on"], "verified-models.json verified_on")
            if verified.get("complete_list") is True and uc_fresh and (uc_date is None or uc_date <= rec_date):
                excluded = uc_fresh - models
                uc_fresh = uc_fresh & models
                if excluded:
                    parts.append("%d model(s) in the user-config list (verified %s) are NOT in your complete /model "
                                 "list of %s and are treated as unavailable: %s" % (
                                     len(excluded), avail["verified_on"], verified["verified_on"],
                                     ", ".join(sorted(excluded))))
        else:
            parts.append("recorded /model list %s is stale or future-dated — re-verify (copilot-s p -> v)"
                         % verified["verified_on"])
            stale_models |= models
    fresh_models |= uc_fresh
    if not any_list:
        return None, ("no verified availability list (add available_models to %s/user-config.json, or record "
                      "it with copilot-s p -> v)" % STATE_DIRNAME), False
    if fresh_models:
        return fresh_models, "; ".join(parts), True
    return stale_models, "; ".join(parts), False


def verified_record_fresh(verified, today):
    try:
        age = (today - parse_date(verified["verified_on"], "verified-models.json verified_on")).days
    except RefreshError:
        return False
    return 0 <= age <= AVAILABILITY_MAX_AGE_DAYS


def availability_complete(verified, today):
    """True only when YOU recorded (fresh) that your /model list is complete."""
    return bool(verified and verified.get("complete_list") is True and verified_record_fresh(verified, today))


def load_verified_record(paths):
    """(record_or_None, sha_or_None, warning_or_None). Written by this tool
    only after an explicit `yes` in record-verified; a malformed file is
    ignored with a visible warning (it then verifies nothing)."""
    try:
        raw = read_bytes(paths.verified)
    except (OSError, RefreshError) as ex:
        return None, "unreadable", "verified-models.json unreadable (%s) — ignored" % _safe_text(str(ex), 160)
    if raw is None:
        return None, None, None
    sha = sha256_bytes(raw)
    try:
        doc = parse_json_bytes(raw, paths.verified)
        return validate_verified_record(doc), sha, None
    except RefreshError as ex:
        return None, sha, "%s is invalid (%s) — ignored; record it again with copilot-s p -> v" % (
            paths.verified, _safe_text(str(ex), 200))


def validate_verified_record(doc):
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION or doc.get("kind") != VERIFIED_KIND:
        raise RefreshError("unsupported schema")
    allowed = {"schema_version", "kind", "verified_on", "recorded_at", "source", "complete_list", "models"}
    if set(doc) - allowed:
        raise RefreshError("unknown field(s): %s" % ", ".join(sorted(set(doc) - allowed)))
    parse_date(doc.get("verified_on"), "verified_on")
    if not isinstance(doc.get("complete_list"), bool):
        raise RefreshError("complete_list must be true or false")
    models = doc.get("models")
    if not isinstance(models, dict) or len(models) > 500:
        raise RefreshError("models must be an object")
    for model_id, rec in models.items():
        if not valid_model_id(model_id) or not isinstance(rec, dict) or set(rec) - {"reasoning_levels",
                                                                                     "levels_verified_on"}:
            raise RefreshError("invalid model entry %r" % _safe_text(str(model_id), 80))
        if ("reasoning_levels" in rec) != ("levels_verified_on" in rec):
            raise RefreshError("model %s: reasoning_levels and levels_verified_on go together" % model_id)
        if "reasoning_levels" in rec:
            _validate_levels(rec["reasoning_levels"], "models[%r].reasoning_levels" % model_id)
            parse_date(rec["levels_verified_on"], "models[%r].levels_verified_on" % model_id)
    return doc


def verified_reasoning_levels(model_id, ctx):
    """(levels_or_None, provenance). Source-backed catalog `reasoning_levels`
    (unexpired), else the user's own dated records: `verified_capabilities`
    in user-config.json and/or levels recorded with record-verified (each
    <= 30 days old; when both exist and differ, the newer date wins and a
    same-date disagreement is unknown). Never inferred from the generic
    configurable_reasoning flag: the official docs only say levels vary per
    model (the CLI shows them in its interactive /model picker; no
    programmatic per-model list is documented)."""
    entry = ctx["catalog"]["models"].get(model_id)
    if (entry and entry.get("reasoning_levels")
            and origin_unusable(entry["origin"], ctx["today"]) is None):
        return entry["reasoning_levels"], "%s catalog reviewed %s" % (
            entry["origin"]["name"], entry["origin"]["reviewed_on"].isoformat())
    candidates, stale = [], []
    caps = ctx["user_config"].get("verified_capabilities")
    if caps and model_id in caps["models"]:
        verified = parse_date(caps["verified_on"], "verified_capabilities.verified_on")
        age = (ctx["today"] - verified).days
        if 0 <= age <= AVAILABILITY_MAX_AGE_DAYS:
            candidates.append((verified, caps["models"][model_id]["reasoning_levels"],
                               "your verified_capabilities (%s)" % caps["verified_on"]))
        else:
            stale.append("your verified_capabilities record (%s) is stale or future-dated" % caps["verified_on"])
    rec = (ctx.get("verified") or {}).get("models", {}).get(model_id)
    if rec and "reasoning_levels" in rec:
        verified = parse_date(rec["levels_verified_on"], "levels_verified_on")
        age = (ctx["today"] - verified).days
        if 0 <= age <= AVAILABILITY_MAX_AGE_DAYS:
            candidates.append((verified, rec["reasoning_levels"],
                               "your /model record (%s)" % rec["levels_verified_on"]))
        else:
            stale.append("your /model record of its levels (%s) is stale or future-dated" % rec["levels_verified_on"])
    if candidates:
        candidates.sort(key=lambda c: c[0], reverse=True)
        if (len(candidates) > 1 and candidates[0][0] == candidates[1][0]
                and set(candidates[0][1]) != set(candidates[1][1])):
            return None, "your verified records disagree on its reasoning levels (same date) — unknown"
        return list(candidates[0][1]), candidates[0][2]
    if stale:
        return None, "; ".join(stale)
    return None, "no source-backed or user-verified reasoning levels"


# -------------------------------------------------------- settings/state ---

def read_settings(paths):
    """(data_or_None, raw_sha_or_None, error_or_None). Never raises for a
    malformed file: pins then cannot be read and apply refuses to touch it."""
    try:
        raw = read_bytes(paths.settings, max_bytes=10_000_000)
    except (OSError, RefreshError) as ex:
        return None, "unreadable", "cannot read %s (%s)" % (paths.settings, ex)
    if raw is None:
        return None, None, None
    try:
        data = parse_json_bytes(raw, paths.settings)
    except RefreshError as ex:
        return None, sha256_bytes(raw), str(ex)
    if not isinstance(data, dict):
        return None, sha256_bytes(raw), "%s is not a JSON object" % paths.settings
    return data, sha256_bytes(raw), None


def _settings_agents(data):
    sub = data.get("subagents") if isinstance(data, dict) else None
    agents = sub.get("agents") if isinstance(sub, dict) else None
    return agents if isinstance(agents, dict) else {}


def blank_state():
    return {"schema_version": SCHEMA_VERSION, "kind": STATE_KIND, "overrides": {}, "managed_settings": {},
            "instructions_sha256": None, "history": []}


def read_state(paths):
    raw = read_bytes(paths.state)
    if raw is None:
        return blank_state(), None
    data = parse_json_bytes(raw, paths.state)
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or data.get("kind") != STATE_KIND:
        raise RefreshError("%s has an unsupported schema; move it aside to start over" % paths.state)
    overrides = data.get("overrides", {})
    if not isinstance(overrides, dict):
        raise RefreshError("%s: overrides must be an object" % paths.state)
    for key, ov in overrides.items():
        if key not in ROUTE_BY_KEY or not isinstance(ov, dict):
            raise RefreshError("%s: invalid override %r" % (paths.state, key))
        validate_route_values({f: ov.get(f) for f in FIELDS}, "%s overrides[%r]" % (paths.state, key))
    managed = data.get("managed_settings", {})
    if not isinstance(managed, dict) or not all(
            a in AGENT_NAMES and isinstance(v, dict) and set(v) <= {"model", "effortLevel", "contextTier"}
            for a, v in managed.items()):
        raise RefreshError("%s: invalid managed_settings" % paths.state)
    sha = data.get("instructions_sha256")
    if sha is not None and not (isinstance(sha, str) and re.match(r"^[0-9a-f]{64}$", sha)):
        raise RefreshError("%s: invalid instructions_sha256" % paths.state)
    if not isinstance(data.get("history", []), list):
        raise RefreshError("%s: invalid history" % paths.state)
    data.setdefault("overrides", {})
    data.setdefault("managed_settings", {})
    data.setdefault("history", [])
    return data, sha256_bytes(raw)


def settings_pins(settings_data, state):
    """Human pins from native /subagents settings: every
    subagents.agents.<toolkit agent>.{model,effortLevel,contextTier} value
    except one that still equals the value this tool last wrote there.
    Returns ({agent: {field: value}}, warnings)."""
    pins, warnings = {}, []
    agents = _settings_agents(settings_data)
    managed = state.get("managed_settings", {})
    for agent in AGENT_NAMES:
        node = agents.get(agent)
        if not isinstance(node, dict):
            continue
        for field, native in SETTINGS_FIELDS:
            if native not in node:
                continue
            value = node[native]
            ours = managed.get(agent, {}).get(native)
            if ours is not None and value == ours:
                continue
            ok = value == INHERIT or (valid_model_id(value) if field == "model" else
                                      value in (EFFORTS if field == "reasoning_effort" else CONTEXT_TIERS))
            if not ok:
                warnings.append("/subagents %s.%s=%r is not a value this tool can validate; it is left untouched "
                                "and not copied into the generated table" % (agent, native, value))
                continue
            pins.setdefault(agent, {})[field] = value
    return pins, warnings


def resolve_effective(defaults, overrides, user_pins, native_pins):
    """{route: {"values": {field: value}, "sources": {field: source}}} using
    defaults -> managed overrides -> /subagents pins -> user-config pins."""
    eff = {}
    for route in ROUTES:
        key = route["key"]
        values, sources = {}, {}
        for field in FIELDS:
            value, source = defaults[key][field], "default"
            if key in overrides:
                value, source = overrides[key][field], "managed"
            if route["kind"] == "agent" and field in native_pins.get(route["agent"], {}):
                value, source = native_pins[route["agent"]][field], "pin:/subagents"
            if field in user_pins.get(key, {}):
                value, source = user_pins[key][field], "pin:user-config"
            values[field], sources[field] = value, source
        eff[key] = {"values": values, "sources": sources}
    return eff


def is_pinned(entry):
    return any(s.startswith("pin:") for s in entry["sources"].values())


# ------------------------------------------------------------- pricing ---

def default_rates(snapshot, model_id):
    entry = snapshot["models"].get(model_id) if snapshot else None
    return entry["tiers"]["default"] if entry else None


def long_tier_rates(snapshot, model_id):
    """Rates billed above the model's long-context threshold (the Default
    rates when the official table lists no separate tier)."""
    entry = snapshot["models"].get(model_id) if snapshot else None
    if not entry:
        return None
    return entry["tiers"].get("long_context") or entry["tiers"]["default"]


def blended_cost(rates):
    """USD per 1M tokens under WORKLOAD_MIX. A cache-write share is billed at
    the cache-write rate when one is listed, otherwise at the input rate
    (official 'Not applicable' == ordinary input; 'not listed' == the same
    conservative assumption)."""
    if not rates:
        return None
    cache_write = rates["cache_write"] if rates.get("cache_write_status") == "rate" else rates["input"]
    by_key = {"input": rates["input"], "cached_input": rates["cached_input"], "cache_write": cache_write,
              "output": rates["output"]}
    return sum(share * by_key[key] for key, share in WORKLOAD_MIX)


def refresh_pricing(module, offline, now):
    """Explicit refresh, then the cache-only context. Returns (ctx, note)."""
    if offline:
        note = "offline: using the cached official snapshot (no network request)"
    else:
        result = module.refresh_official_pricing_now(now=now)
        status = result.get("status")
        if status == "refreshed":
            note = "refreshed now from docs.github.com"
        elif status == "fresh":
            note = "reused a snapshot fetched within the last %d min" % (
                module.OFFICIAL_PRICING_MANUAL_MIN_INTERVAL_SECONDS // 60)
        elif status == "disabled":
            note = ("network fetch disabled (%s=0): using the cached snapshot"
                    % module.OFFICIAL_PRICING_FETCH_ENV)
        else:
            note = "REFRESH FAILED (%s): showing the last valid cached snapshot, if any" % (
                result.get("error") or status)
    return module.load_official_pricing_context(now=now), note


# ------------------------------------------- official capability evidence ---

class CapabilityError(Exception):
    """A capability page could not be fetched or recognized."""


_SVG_RE = re.compile(r"<svg\b([^>]{0,4000})>.*?</svg>", re.S | re.I)
_ARIA_LABEL_RE = re.compile(r'\baria-label="([^"<>]{1,60})"')
_TAG_RE = re.compile(r"<[^>]{0,2000}>")
_FOOTNOTE_REF_RE = re.compile(r"\[\^[A-Za-z0-9_-]{1,60}\]")
_MD_LINK_RE = re.compile(r"\[([^\[\]]{0,200})\]\([^()\s]{0,500}\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")
_TOKEN_CELL_RE = re.compile(r"^\[\[([^\[\]]{1,60})\]\]$")
_DISPLAY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .()+\-]{0,79}$")
_SAFE_DOC_TEXT_RE = re.compile("^[A-Za-z0-9][A-Za-z0-9 ,.;:()'\u2019/&+\\-]{0,299}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# Section name -> (required {normalized header: field}, optional {...}).
_SECTION_COLUMNS = {
    "listed": ({"model name": "model", "provider": "provider", "release status": "release_status"}, {}),
    "extended": ({"model": "model", "1 million token context window": "one_million_context",
                  "configurable reasoning": "configurable_reasoning"}, {}),
    "clients": ({"model": "model", "copilot cli": "copilot_cli_client"}, {}),
    "tasks": ({"model": "model", "task area": "task_area"}, {"excels at (primary use case)": "excels_at"}),
}
_FLAG_VALUES = {
    "one_million_context": {"supported": True, "not supported": False},
    "configurable_reasoning": {"supported": True, "not supported": False},
    "copilot_cli_client": {"included": "included", "not included": "not_included"},
}
_TEXT_FIELDS = ("provider", "release_status", "task_area", "excels_at")


def _svg_token(match):
    label = _ARIA_LABEL_RE.search(match.group(1))
    return "[[%s]]" % label.group(1) if label else "[[?]]"


def _clean_doc_cell(cell):
    text = _FOOTNOTE_REF_RE.sub("", cell)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"[\\*`]", "", text)
    return re.sub(r"\s+", " ", _CONTROL_RE.sub(" ", text)).strip()


def _split_doc_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _local_display_to_id(display):
    """Same purely mechanical rule as the shared pricing parser's
    normalize_official_model_name (used only if that is unavailable)."""
    s = re.sub(r"[()]", " ", display.lower())
    s = re.sub(r"[\s_]+", "-", s.strip())
    return re.sub(r"-{2,}", "-", s).strip("-")


def _display_to_id(display, normalize):
    try:
        model_id = (normalize or _local_display_to_id)(display)
    except (ValueError, TypeError):
        return None
    return model_id if valid_model_id(model_id) else None


def _doc_cell_value(field, cell, warnings):
    if field in _FLAG_VALUES:
        s = cell.strip()
        m = _TOKEN_CELL_RE.match(s)
        label = (m.group(1) if m else _clean_doc_cell(s)).strip().lower()
        if label in _FLAG_VALUES[field]:
            return _FLAG_VALUES[field][label]
        warnings.append("an unrecognized %s value was treated as unknown" % field.replace("_", " "))
        return None
    text = _clean_doc_cell(cell)
    if not text:
        return None
    if not _SAFE_DOC_TEXT_RE.match(text):
        warnings.append("an unrecognized %s text was treated as unknown" % field.replace("_", " "))
        return None
    return text


def _markdown_sections(lines):
    """{lowercased heading: [body lines]} (a section runs until the next
    heading of the same or a higher level). A heading that occurs more than
    once maps to None (ambiguous)."""
    sections, stack = {}, []
    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            level, key = len(m.group(1)), _clean_doc_cell(m.group(2)).lower()
            stack = [(lvl, k) for lvl, k in stack if lvl < level]
            sections[key] = None if key in sections else []
            stack.append((level, key))
            continue
        for _lvl, key in stack:
            if sections.get(key) is not None:
                sections[key].append(line)
    return sections


def _parse_section_table(lines, name, normalize):
    table = []
    for line in lines:
        s = line.strip()
        if s.startswith("|"):
            table.append(s)
        elif table:
            break
    if len(table) < 2:
        raise CapabilityError("no table found")
    if len(table) > 2 * CAPABILITY_MAX_ROWS + 2:
        raise CapabilityError("table unexpectedly large")
    header = [_clean_doc_cell(c).lower() for c in _split_doc_row(table[0])]
    if not all(_SEPARATOR_CELL_RE.match(c) for c in _split_doc_row(table[1])):
        raise CapabilityError("malformed table header")
    required, optional = _SECTION_COLUMNS[name]
    col = {}
    for idx, head in enumerate(header):
        field = required.get(head) or optional.get(head)
        if field:
            if field in col:
                raise CapabilityError("column %r appears twice" % head)
            col[field] = idx
    missing = [head for head, field in required.items() if field not in col]
    if missing:
        raise CapabilityError("expected column(s) missing: %s" % ", ".join(missing))
    models, conflicts, warnings = {}, set(), []
    for line in table[2:]:
        cells = _split_doc_row(line)
        if all(not c for c in cells):
            continue
        if len(cells) != len(header):
            raise CapabilityError("a row has %d cells, expected %d" % (len(cells), len(header)))
        display = _clean_doc_cell(cells[col["model"]])
        model_id = _display_to_id(display, normalize) if _DISPLAY_NAME_RE.match(display) else None
        if model_id is None:
            warnings.append("a row with an unrecognized model name was skipped")
            continue
        rec = {"display_name": display}
        for field, idx in sorted(col.items()):
            if field != "model":
                rec[field] = _doc_cell_value(field, cells[idx], warnings)
        if model_id in conflicts:
            continue
        if model_id in models and models[model_id] != rec:
            # Two rows for one model id that disagree: unknown, never guessed.
            conflicts.add(model_id)
            del models[model_id]
            warnings.append("conflicting rows for %s were treated as unknown" % model_id)
            continue
        models[model_id] = rec
    if not models and not conflicts:
        raise CapabilityError("no model rows recognized")
    return {"models": models, "conflicts": sorted(conflicts), "warnings": sorted(set(warnings))[:20]}


def parse_capability_markdown(text, source, normalize=None):
    """{section name: {"status": "ok", models, conflicts, warnings} |
    {"status": "error", "error"}} for one official capability page. A
    missing/duplicated heading, missing column or malformed row makes that
    whole section an error (unknown), never a partial guess."""
    text = _SVG_RE.sub(_svg_token, text)
    sections = _markdown_sections(text.splitlines())
    out = {}
    for name, heading in source["sections"]:
        body = sections.get(heading.lower(), False)
        if body is False:
            out[name] = {"status": "error", "error": "section '%s' not found (page layout changed?)" % heading}
        elif body is None:
            out[name] = {"status": "error", "error": "section '%s' appears more than once" % heading}
        else:
            try:
                out[name] = dict({"status": "ok"}, **_parse_section_table(body, name, normalize))
            except CapabilityError as ex:
                out[name] = {"status": "error", "error": "section '%s': %s" % (heading, ex)}
    return out


def _validate_section(name, sec):
    if not isinstance(sec, dict):
        raise CapabilityError("section %s is not an object" % name)
    if sec.get("status") == "error":
        if set(sec) != {"status", "error"} or not isinstance(sec["error"], str):
            raise CapabilityError("malformed error section %s" % name)
        return {"status": "error", "error": _safe_text(sec["error"], 300)}
    if sec.get("status") != "ok" or set(sec) != {"status", "models", "conflicts", "warnings"}:
        raise CapabilityError("malformed section %s" % name)
    required, optional = _SECTION_COLUMNS[name]
    need = set(required.values()) - {"model"}
    allowed = need | set(optional.values()) | {"display_name"}
    models = sec["models"]
    if not isinstance(models, dict) or len(models) > CAPABILITY_MAX_ROWS:
        raise CapabilityError("malformed models in %s" % name)
    for model_id, rec in models.items():
        if (not valid_model_id(model_id) or not isinstance(rec, dict) or not need <= set(rec) <= allowed
                or not isinstance(rec.get("display_name"), str) or not _DISPLAY_NAME_RE.match(rec["display_name"])):
            raise CapabilityError("malformed model entry in %s" % name)
        for field, value in rec.items():
            if field in _FLAG_VALUES:
                if value is not None and value not in _FLAG_VALUES[field].values():
                    raise CapabilityError("invalid %s in %s" % (field, name))
            elif field in _TEXT_FIELDS:
                if value is not None and not (isinstance(value, str) and _SAFE_DOC_TEXT_RE.match(value)):
                    raise CapabilityError("invalid %s in %s" % (field, name))
    conflicts = sec["conflicts"]
    if not isinstance(conflicts, list) or not all(valid_model_id(c) for c in conflicts):
        raise CapabilityError("malformed conflicts in %s" % name)
    if not isinstance(sec["warnings"], list) or not all(isinstance(w, str) for w in sec["warnings"]):
        raise CapabilityError("malformed warnings in %s" % name)
    return {"status": "ok", "models": models, "conflicts": list(conflicts),
            "warnings": [_safe_text(w, 160) for w in sec["warnings"][:20]]}


def _validate_capability_record(key, rec):
    src = CAPABILITY_SOURCE_BY_KEY[key]
    keys = {"url", "public_url", "fetched_at", "fetched_epoch", "content_sha256", "parser_version", "sections"}
    if not isinstance(rec, dict) or set(rec) != keys:
        raise CapabilityError("malformed record for %s" % key)
    epoch = rec["fetched_epoch"]
    if (rec["url"] != capability_url(src) or rec["public_url"] != capability_public_url(src)
            or rec["parser_version"] != CAPABILITY_PARSER_VERSION
            or not isinstance(epoch, (int, float)) or isinstance(epoch, bool) or epoch != epoch
            or epoch in (float("inf"), float("-inf"))
            or not isinstance(rec["content_sha256"], str) or not _SHA256_HEX_RE.match(rec["content_sha256"])):
        raise CapabilityError("invalid provenance for %s" % key)
    sections = rec["sections"]
    names = {name for name, _heading in src["sections"]}
    if not isinstance(sections, dict) or set(sections) != names:
        raise CapabilityError("unexpected sections for %s" % key)
    return {"url": rec["url"], "public_url": rec["public_url"], "fetched_at": now_iso(epoch),
            "fetched_epoch": float(epoch), "content_sha256": rec["content_sha256"],
            "parser_version": CAPABILITY_PARSER_VERSION,
            "sections": {name: _validate_section(name, sections[name]) for name in sorted(names)}}


def _blank_capability_cache():
    return {"schema_version": SCHEMA_VERSION, "kind": CAPABILITY_KIND, "parser_version": CAPABILITY_PARSER_VERSION,
            "sources": {}, "attempts": {}}


def read_capability_cache(paths):
    """(cache, warning_or_None). Anything malformed is ignored as a whole
    (warning shown); it then simply provides no evidence."""
    blank = _blank_capability_cache()
    try:
        raw = read_bytes(paths.capabilities, max_bytes=4 * CAPABILITY_MAX_BYTES)
    except (OSError, RefreshError) as ex:
        return blank, "capability evidence cache unreadable (%s) — ignored" % _safe_text(ex, 160)
    if raw is None:
        return blank, None
    try:
        doc = parse_json_bytes(raw, paths.capabilities)
        if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION or doc.get("kind") != CAPABILITY_KIND:
            raise CapabilityError("unsupported schema")
        if doc.get("parser_version") != CAPABILITY_PARSER_VERSION:
            return blank, ("capability evidence cache was written by another parser version — ignored until the "
                           "next successful fetch")
        sources, attempts = {}, {}
        raw_sources = doc.get("sources", {})
        raw_attempts = doc.get("attempts", {})
        if not isinstance(raw_sources, dict) or not isinstance(raw_attempts, dict):
            raise CapabilityError("malformed sources/attempts")
        for key, rec in raw_sources.items():
            if key not in CAPABILITY_SOURCE_BY_KEY:
                raise CapabilityError("unknown source %r" % key)
            sources[key] = _validate_capability_record(key, rec)
        for key, att in raw_attempts.items():
            if key in CAPABILITY_SOURCE_BY_KEY and isinstance(att, dict):
                err = att.get("last_error")
                at = att.get("last_attempt_at")
                attempts[key] = {"last_attempt_at": _safe_text(at, 40) if isinstance(at, str) else None,
                                 "last_error": _safe_text(err, 200) if isinstance(err, str) else None}
        return dict(blank, sources=sources, attempts=attempts), None
    except (RefreshError, CapabilityError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as ex:
        return blank, "capability evidence cache is invalid (%s) — ignored" % _safe_text(ex, 160)


def capability_fetch_mode(module):
    """(enabled, reason_if_not)."""
    if os.environ.get(DOCS_FETCH_ENV, "").strip().lower() in ("0", "off", "false", "no"):
        return False, "network fetch disabled (%s=0): using the cached copy" % DOCS_FETCH_ENV
    enabled = getattr(module, "official_pricing_fetch_enabled", None)
    if enabled is not None and not enabled():
        return False, "network fetch disabled (%s=0): using the cached copy" % getattr(
            module, "OFFICIAL_PRICING_FETCH_ENV", "COPILOT_TASK_REPORT_PRICING_FETCH")
    if CAPABILITY_FETCHER is None and PRICING_FETCHER is not None:
        return False, "not fetched: an injected pricing transport has no capability transport (no live traffic)"
    return True, None


def _is_cert_verification_error(ex):
    """True only for ssl.SSLCertVerificationError, raw or wrapped as the
    `reason` of urllib.error.URLError (never for HTTPError)."""
    try:
        import ssl
        import urllib.error
    except ImportError:
        return False
    cert_error = getattr(ssl, "SSLCertVerificationError", None)
    if cert_error is None:
        return False
    if isinstance(ex, cert_error):
        return True
    if isinstance(ex, urllib.error.HTTPError):
        return False
    return isinstance(ex, urllib.error.URLError) and isinstance(getattr(ex, "reason", None), cert_error)


_DOCS_CURL_STATUS_MARKER = "\n@@copilot-model-refresh-curl-status@@ "
_USER_AGENT = "copilot-model-refresh (copilot-cli-toolkit official docs refresh)"
_ACCEPT = "text/markdown, text/plain;q=0.9, */*;q=0.1"


def _curl_docs_fetch(url, timeout, max_bytes, python_error):
    """Bounded HTTPS GET of a FIXED official capability URL via the system
    curl, certificate verification ON (never -k). `-q` first so no ~/.curlrc
    can inject options; no redirects followed; only HTTP 200 from the same
    URL accepted; output capped."""
    import subprocess

    curl = "/usr/bin/curl" if os.path.isfile("/usr/bin/curl") and os.access("/usr/bin/curl", os.X_OK) \
        else shutil.which("curl")
    if not curl:
        raise CapabilityError("Python could not verify the TLS certificate (%s) and no system curl was found for the "
                              "verified fallback" % python_error)
    argv = [curl, "-q", "--fail", "--silent", "--show-error", "--proto", "=https", "--proto-redir", "=https",
            "--tlsv1.2", "--connect-timeout", str(int(timeout)), "--max-time", str(int(CAPABILITY_DEADLINE_SECONDS)),
            "--max-filesize", str(int(max_bytes)), "--header", "User-Agent: " + _USER_AGENT,
            "--header", "Accept: " + _ACCEPT, "--write-out", _DOCS_CURL_STATUS_MARKER + "%{http_code} %{url_effective}",
            "--url", url]
    cap = max_bytes + len(_DOCS_CURL_STATUS_MARKER) + len(url) + 16
    chunks, size = [], 0
    try:
        with tempfile.TemporaryFile() as err_file:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err_file,
                                    shell=False, close_fds=True)
            try:
                while True:
                    chunk = proc.stdout.read1(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > cap:
                        raise CapabilityError("system curl fallback: response too large")
                    chunks.append(chunk)
                returncode = proc.wait(timeout=CAPABILITY_DEADLINE_SECONDS + 5)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=5)
                proc.stdout.close()
            err_file.seek(0)
            err_text = _safe_text(err_file.read(2000).decode("utf-8", "replace"), 200)
    except subprocess.TimeoutExpired:
        raise CapabilityError("system curl fallback timed out")
    except (OSError, subprocess.SubprocessError) as ex:
        raise CapabilityError("system curl fallback could not run (%s)" % type(ex).__name__)
    if returncode != 0:
        raise CapabilityError("system curl fallback failed (exit %d: %s)" % (returncode, err_text or "no message"))
    out = b"".join(chunks)
    marker = _DOCS_CURL_STATUS_MARKER.encode("ascii")
    idx = out.rfind(marker)
    if idx < 0:
        raise CapabilityError("system curl fallback: missing HTTP status trailer")
    body, trailer = out[:idx], out[idx + len(marker):].decode("ascii", "replace").strip()
    status, _, final_url = trailer.partition(" ")
    if status != "200" or final_url != url or len(body) > max_bytes:
        raise CapabilityError("system curl fallback: unexpected status/final URL/size")
    return body


def _default_docs_fetch(url, timeout, max_bytes):
    """Bounded stdlib HTTPS GET of a fixed official URL: certificate
    verification on, every redirect refused, per-operation timeout plus a
    total deadline checked between reads, size cap. Only a Python TLS
    certificate-verification failure falls back to the verified system curl."""
    import http.client
    import ssl
    import urllib.error
    import urllib.request

    class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # urllib then raises HTTPError for the 3xx response

    opener = urllib.request.build_opener(_RefuseRedirect(),
                                         urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": _ACCEPT})
    try:
        deadline = time.monotonic() + CAPABILITY_DEADLINE_SECONDS
        chunks, size = [], 0
        with opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 200:
                raise CapabilityError("unexpected HTTP status %s" % status)
            if (resp.geturl() or url) != url:
                raise CapabilityError("unexpected final URL")
            clen = resp.headers.get("Content-Length")
            if clen and clen.strip().isdigit() and int(clen) > max_bytes:
                raise CapabilityError("response too large")
            while True:
                if time.monotonic() > deadline:
                    raise CapabilityError("download exceeded %ds" % CAPABILITY_DEADLINE_SECONDS)
                chunk = resp.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise CapabilityError("response too large")
                chunks.append(chunk)
        return b"".join(chunks)
    except urllib.error.HTTPError as ex:
        with contextlib.suppress(Exception):
            ex.close()
        raise CapabilityError("HTTP %s%s" % (ex.code, " (redirect refused)" if 300 <= ex.code < 400 else ""))
    except http.client.HTTPException as ex:
        raise CapabilityError("HTTP protocol error (%s)" % type(ex).__name__)
    except OSError as ex:
        if not _is_cert_verification_error(ex):
            raise CapabilityError("%s: %s" % (type(ex).__name__, _safe_text(ex, 160)))
        python_error = _safe_text("%s: %s" % (type(ex).__name__, ex), 200)
        body = _curl_docs_fetch(url, timeout, max_bytes, python_error)
        print("Note: Python could not verify the TLS certificate for docs.github.com (%s); fetched the official "
              "capability page with the system curl instead (HTTPS only, certificate verification ON)."
              % python_error, file=sys.stderr)
        return body


def _fetch_capability_bytes(url):
    if url not in {capability_url(s) for s in CAPABILITY_SOURCES}:
        raise CapabilityError("refused: not a fixed official capability URL")
    fetcher = CAPABILITY_FETCHER or _default_docs_fetch
    raw = fetcher(url, CAPABILITY_TIMEOUT_SECONDS, CAPABILITY_MAX_BYTES)
    if not isinstance(raw, (bytes, bytearray)):
        raise CapabilityError("transport returned no bytes")
    if len(raw) > CAPABILITY_MAX_BYTES:
        raise CapabilityError("response too large")
    return bytes(raw)


def refresh_capabilities(paths, now, module, fetch):
    """Explicit refresh (fetch=True; reuses a copy fetched < 5 min ago) or
    cache only. A failed source keeps its last valid copy (shown with its
    age; stale copies never authorize anything). Only the evidence cache is
    written. Returns the capability context."""
    cache, warning = read_capability_cache(paths)
    notes, changed = {}, False
    if fetch:
        enabled, why = capability_fetch_mode(module)
    else:
        enabled, why = False, "cache only (no network)"
    normalize = getattr(module, "normalize_official_model_name", None)
    for src in CAPABILITY_SOURCES:
        key = src["key"]
        rec = cache["sources"].get(key)
        if not enabled:
            notes[key] = why
            continue
        if rec is not None and 0 <= now - rec["fetched_epoch"] < CAPABILITY_MIN_INTERVAL_SECONDS:
            notes[key] = "reused a copy fetched within the last %d min" % (CAPABILITY_MIN_INTERVAL_SECONDS // 60)
            continue
        changed = True
        try:
            raw = _fetch_capability_bytes(capability_url(src))
            sections = parse_capability_markdown(raw.decode("utf-8"), src, normalize)
            bad = [s["error"] for s in sections.values() if s["status"] != "ok"]
            if len(bad) == len(sections):
                raise CapabilityError("; ".join(bad))
            new = {"url": capability_url(src), "public_url": capability_public_url(src), "fetched_at": now_iso(now),
                   "fetched_epoch": float(now), "content_sha256": sha256_bytes(raw),
                   "parser_version": CAPABILITY_PARSER_VERSION, "sections": sections}
            cache["sources"][key] = _validate_capability_record(key, json.loads(json.dumps(new)))
            cache["attempts"][key] = {"last_attempt_at": now_iso(now), "last_error": None}
            notes[key] = "refreshed now" + (" (partly unrecognized: %s)" % _safe_text("; ".join(bad), 200)
                                            if bad else "")
        except KeyboardInterrupt:
            raise
        except Exception as ex:  # noqa: BLE001 - a broken page/transport must never break p
            err = _safe_text(ex, 200) or type(ex).__name__
            cache["attempts"][key] = {"last_attempt_at": now_iso(now), "last_error": err}
            notes[key] = "REFRESH FAILED (%s): showing the last valid copy, if any" % err
    if changed:
        try:
            ensure_private_dir(paths.state_dir)
            atomic_write_bytes(paths.capabilities, json_bytes(cache))
        except (OSError, RefreshError) as ex:
            warning = ((warning + "; ") if warning else "") + "could not save capability evidence (%s)" % _safe_text(
                ex, 120)
    return capability_context(cache, now, notes, warning)


def _canonical_sha256(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("ascii")).hexdigest()


def pricing_digest(snapshot):
    """Binds the parsed official rates/labels of the snapshot actually used
    (not only its id), so a changed cached rate is detected."""
    if not snapshot:
        return None
    return "%s:%s" % (snapshot.get("snapshot_id"), _canonical_sha256(
        {"fetched_epoch": snapshot.get("fetched_epoch"), "models": snapshot.get("models")}))


def capability_context(cache, now, notes=None, warning=None):
    notes = notes or {}
    sources = {}
    for src in CAPABILITY_SOURCES:
        key = src["key"]
        rec = cache["sources"].get(key)
        if rec is None:
            status, age = "missing", None
        else:
            age = now - rec["fetched_epoch"]
            status = "future" if age < 0 else ("stale" if age > src["ttl_seconds"] else "fresh")
        sources[key] = {"source": src, "record": rec, "status": status, "age": age,
                        "note": notes.get(key) or "cache only (no network)",
                        "last_error": cache["attempts"].get(key, {}).get("last_error")}
    # Binds the PARSED evidence itself (every section, model and value), its
    # provenance and its current freshness — not only the source metadata.
    payload = [[k, s["status"], _canonical_sha256(s["record"]) if s["record"] else None]
               for k, s in sorted(sources.items())]
    return {"sources": sources, "warning": warning,
            "digest": hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()}


def usable_section(cap, key, name):
    """The section's data when its source is fresh and the section parsed
    cleanly, else None (no evidence)."""
    src = cap["sources"].get(key) if cap else None
    if not src or src["status"] != "fresh":
        return None
    sec = src["record"]["sections"].get(name)
    return sec if sec and sec.get("status") == "ok" else None


def all_live_sections_usable(cap):
    return all(usable_section(cap, s["key"], name) is not None for s in CAPABILITY_SOURCES for name, _h in s["sections"])


def unusable_evidence_ids(cap):
    """{model_id: [source labels]} for models listed only in a cached copy
    that is NOT usable now (stale or future-dated). Names only — never facts."""
    out = {}
    for key, src in sorted((cap or {}).get("sources", {}).items()):
        if src["record"] is None or src["status"] == "fresh":
            continue
        where = "%s (%s, fetched %s)" % (src["source"]["label"], src["status"], src["record"]["fetched_at"])
        for sec in src["record"]["sections"].values():
            if sec.get("status") == "ok":
                for model_id in list(sec["models"]) + list(sec["conflicts"]):
                    labels = out.setdefault(model_id, [])
                    if where not in labels:
                        labels.append(where)
    return out


# ------------------------------------------------------------- model facts ---

GATE_FIELDS = ("task_area", "one_million_context", "configurable_reasoning", "copilot_cli_client")
_FIELD_SECTIONS = {
    "task_area": ("model_comparison", "tasks"), "excels_at": ("model_comparison", "tasks"),
    "one_million_context": ("supported_models", "extended"),
    "configurable_reasoning": ("supported_models", "extended"),
    "copilot_cli_client": ("supported_models", "clients"),
}
FIELD_LABELS = {"task_area": "task area", "one_million_context": "1M-token context",
                "configurable_reasoning": "configurable reasoning", "copilot_cli_client": "Copilot CLI support",
                "category": "Category", "excels_at": "'Excels at' text"}


def fact_text(field, value):
    if value is None:
        return "unknown" if field == "copilot_cli_client" else "not documented"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).replace("_", " ") if field == "copilot_cli_client" else str(value)


def _same_fact(field, a, b):
    if field in ("one_million_context", "configurable_reasoning"):
        return bool(a) == bool(b)
    if field == "category":
        return str(a or "").lower() == str(b or "").lower()
    return a == b


def _source_where(cap, key):
    s = cap["sources"][key]
    return "official %s page fetched %s" % (s["source"]["label"], s["record"]["fetched_at"])


def model_facts(model_id, ctx):
    """Per-field facts for one model: fresh live official evidence first,
    else the dated catalog (shipped or local overlay) as fallback, else
    unknown (None). Records per-field basis, problems (fail closed) and
    notes (visible, informational)."""
    entry = ctx["catalog"]["models"].get(model_id)
    snap = ctx["snapshot"]["models"].get(model_id) if ctx["snapshot"] else None
    cap = ctx["capabilities"]
    listed = usable_section(cap, "supported_models", "listed")
    lrow = listed["models"].get(model_id) if listed else None
    f = {"id": model_id, "in_catalog": entry is not None,
         "catalog_origin": entry["origin"]["name"] if entry else None,
         "catalog_reviewed": entry["origin"]["reviewed_on"].isoformat() if entry else None, "priced": snap is not None,
         "display_name": ((snap or {}).get("display_name") or (lrow or {}).get("display_name")
                          or (entry or {}).get("display_name")),
         "provider": (snap or {}).get("provider") or (lrow or {}).get("provider") or (entry or {}).get("provider"),
         "release_status": (snap or {}).get("release_status") or (lrow or {}).get("release_status"),
         "basis": {}, "where": {}, "problems": [], "notes": [], "uses_catalog": False, "catalog_problem": None,
         "documented": False}
    if snap and lrow and snap.get("provider") and lrow.get("provider") and snap["provider"] != lrow["provider"]:
        f["problems"].append("official pages disagree on the provider (pricing: %s, supported models: %s)"
                             % (snap["provider"], lrow["provider"]))
    if listed and model_id in listed["conflicts"]:
        f["problems"].append("the official supported-models list has conflicting rows for it")
    for field in GATE_FIELDS + ("excels_at",):
        src_key, sec_name = _FIELD_SECTIONS[field]
        sec = usable_section(cap, src_key, sec_name)
        row = sec["models"].get(model_id) if sec is not None else None
        conflict = sec is not None and model_id in sec["conflicts"]
        if row is not None or conflict:
            f["documented"] = True
        # The comparison table recommends models; absence there is not a
        # statement about the model, so the dated catalog value (if any)
        # still applies. The capability/per-client tables enumerate models,
        # so absence there IS evidence ("not documented"/"not listed").
        absent_is_unknown = sec is not None and row is None and not conflict and src_key == "model_comparison"
        if sec is not None and not (absent_is_unknown and entry is not None and field in entry):
            if conflict:
                value = None
                if field != "excels_at":
                    f["problems"].append("the official %s page has conflicting rows for its %s (unknown)"
                                         % (cap["sources"][src_key]["source"]["label"], FIELD_LABELS[field]))
            elif row is None:
                value = "not_listed" if field == "copilot_cli_client" else None
            else:
                value = row.get(field)
            f[field], f["basis"][field], f["where"][field] = value, "live", _source_where(cap, src_key)
            if entry is not None and field in entry and not _same_fact(field, entry[field], value):
                msg = "%s: official docs now say %s; %s catalog said %s" % (
                    FIELD_LABELS[field], fact_text(field, value), entry["origin"]["name"],
                    fact_text(field, entry[field]))
                if entry["origin"]["name"] == "local":
                    f["problems"].append(msg + " — your local overlay contradicts current official evidence; re-vet it")
                else:
                    f["notes"].append(msg)
        elif entry is not None and field in entry:
            f[field], f["basis"][field] = entry[field], "catalog"
            f["where"][field] = "%s catalog reviewed %s" % (entry["origin"]["name"],
                                                            entry["origin"]["reviewed_on"].isoformat())
            if absent_is_unknown:
                f["notes"].append("%s: not in the current official model comparison; the dated %s catalog value "
                                  "is used" % (FIELD_LABELS[field], entry["origin"]["name"]))
            if field in GATE_FIELDS:
                f["uses_catalog"] = True
        else:
            f[field], f["basis"][field], f["where"][field] = None, None, None
    live_cat = snap.get("category") if snap else None
    if entry is not None and f["uses_catalog"]:
        # Catalog facts in use: the catalog's Category must still match the
        # live pricing (otherwise the transcription is stale -> re-review).
        f["category"], f["basis"]["category"] = entry["category"], "catalog"
        f["where"]["category"] = "%s catalog reviewed %s" % (entry["origin"]["name"],
                                                            entry["origin"]["reviewed_on"].isoformat())
        problem = origin_unusable(entry["origin"], ctx["today"])
        if problem:
            f["catalog_problem"] = problem
            f["problems"].insert(0, problem)
        if snap is not None:
            if not live_cat:
                f["problems"].append("official Category not available in this pricing snapshot")
            elif live_cat.lower() != entry["category"].lower():
                f["problems"].append("official Category changed (now %s, catalogued %s) — needs re-review"
                                     % (live_cat, entry["category"]))
    else:
        ok = isinstance(live_cat, str) and live_cat.lower() in CATEGORY_RANK
        f["category"] = live_cat if ok else None
        f["basis"]["category"] = "live" if ok else None
        f["where"]["category"] = "official pricing snapshot" if ok else None
        if ok and entry is not None and entry["category"].lower() != live_cat.lower():
            msg = "Category: official pricing now says %s; %s catalog said %s" % (
                live_cat, entry["origin"]["name"], entry["category"])
            if entry["origin"]["name"] == "local":
                f["problems"].append(msg + " — your local overlay contradicts current official evidence; re-vet it")
            else:
                f["notes"].append(msg)
    f["all_live"] = all(f["basis"].get(k) == "live" for k in GATE_FIELDS) and f["basis"].get("category") == "live"
    f["assessed"] = f["task_area"] is not None and f["category"] is not None
    stale_where = unusable_evidence_ids(cap).get(model_id)
    if stale_where and not f["documented"]:
        f["notes"].append("listed only in a copy of the official pages that is not usable now (%s) — its facts are "
                          "unknown and never used" % "; ".join(stale_where))
    return f


def model_universe(ctx):
    """Every model ID the official pricing, the usable official capability
    sections, the catalog, your verified records and the current routes
    mention (unpriced/undocumented ones included, as unknown)."""
    ids = set(ctx["catalog"]["models"])
    if ctx["snapshot"]:
        ids |= set(ctx["snapshot"]["models"])
    for src in CAPABILITY_SOURCES:
        for name, _heading in src["sections"]:
            sec = usable_section(ctx["capabilities"], src["key"], name)
            if sec:
                ids |= set(sec["models"]) | set(sec["conflicts"])
    # Models seen only in a stale/future-dated copy stay visible (as unknown;
    # their facts are never used).
    ids |= set(unusable_evidence_ids(ctx["capabilities"]))
    avail_models = ctx["availability"][0]
    if avail_models:
        ids |= set(avail_models)
    if ctx.get("verified"):
        ids |= set(ctx["verified"]["models"])
    for route in ROUTES:
        ids.add(ctx["effective"][route["key"]]["values"]["model"])
        ids.add(ctx["defaults"][route["key"]]["model"])
    return sorted(m for m in ids if m != INHERIT and valid_model_id(m))


# ------------------------------------------------------------ evaluation ---

def documented_differentiators(cur_f, f, policy, long_route):
    """Concrete, documented facts in the candidate's favour versus the
    CURRENT model (never Category alone, newness or price). Each cites where
    it comes from. Empty = no documented differentiator (listed only)."""
    cur_f = cur_f or {}
    out = []
    cur_cli = cur_f.get("copilot_cli_client")
    if f.get("copilot_cli_client") == "included" and cur_cli != "included":
        out.append("listed as included for Copilot CLI in the official per-client table (%s); current model: %s"
                   % (f["where"].get("copilot_cli_client"), fact_text("copilot_cli_client", cur_cli)))
    if f.get("task_area") in policy["areas"] and cur_f.get("task_area") not in policy["areas"]:
        out.append("official task area '%s' matches this route's baseline (%s); current model: %s"
                   % (f["task_area"], f["where"].get("task_area"), fact_text("task_area", cur_f.get("task_area"))))
    if long_route and f.get("one_million_context") is True and cur_f.get("one_million_context") is not True:
        out.append("official 1M-token context support for this long_context route (%s); current model: %s"
                   % (f["where"].get("one_million_context"), fact_text("one_million_context",
                                                                     cur_f.get("one_million_context"))))
    if f.get("configurable_reasoning") is True and cur_f.get("configurable_reasoning") is not True:
        out.append("official configurable-reasoning support (%s); current model: %s"
                   % (f["where"].get("configurable_reasoning"), fact_text("configurable_reasoning",
                                                                         cur_f.get("configurable_reasoning"))))
    return out


def _facts_evidence_line(model_id, f):
    if f["all_live"]:
        where = "current official docs: %s; Category from the official pricing snapshot" % "; ".join(sorted(
            {f["where"][k] for k in GATE_FIELDS if f["where"].get(k)}))
    else:
        where = "%s catalog reviewed %s" % (f["catalog_origin"], f["catalog_reviewed"])
        live_fields = [FIELD_LABELS[k] for k in GATE_FIELDS if f["basis"].get(k) == "live"]
        if live_fields:
            where += "; live official docs for %s" % ", ".join(live_fields)
    return ("%s: Category %s, task area '%s', 1M context %s, configurable reasoning %s, Copilot CLI %s (%s)" % (
        model_id, f["category"], f["task_area"], fact_text("one_million_context", f["one_million_context"]),
        fact_text("configurable_reasoning", f["configurable_reasoning"]),
        fact_text("copilot_cli_client", f["copilot_cli_client"]), where))


def route_policy(key, defaults, catalog, today, facts=None):
    """(policy_or_None, reason). Default policy: the shipped baseline model's
    own official task area and Category — from current official evidence
    when `facts` (model_facts per model) has it, else from the catalog."""
    baseline = defaults[key]["model"]
    base_entry = catalog["models"].get(baseline)
    if facts is not None:
        base = facts.get(baseline)
    elif base_entry is not None:
        base = {"task_area": base_entry["task_area"], "category": base_entry["category"], "uses_catalog": True,
                "catalog_problem": origin_unusable(base_entry["origin"], today), "in_catalog": True,
                "assessed": True}
    else:
        base = None
    override = catalog["routes"].get(key)
    if override is not None:
        problem = origin_unusable(override["origin"], today)
        if problem:
            return None, "route policy cannot be used: %s" % problem
        min_cat = override.get("min_category") or (base or {}).get("category")
        if not min_cat:
            return None, "route policy has no min_category and the baseline is not catalogued"
        return {"areas": list(override["accepted_task_areas"]), "min_category": min_cat,
                "origin": override["origin"]["name"]}, None
    if base is None or (not base.get("in_catalog") and not base.get("assessed")):
        return None, "shipped baseline %s is not in the catalog (unassessed), so no replacement policy" % baseline
    if base.get("uses_catalog") and base.get("catalog_problem"):
        return None, "catalog entry for the baseline %s cannot be used: %s" % (baseline, base["catalog_problem"])
    if base.get("task_area") is None or base.get("category") is None:
        return None, ("shipped baseline %s has no known official task area/Category right now (unknown), so no "
                      "replacement policy" % baseline)
    return {"areas": [base["task_area"]], "min_category": base["category"],
            "origin": "baseline %s" % baseline}, None


REASON_AVAILABILITY = "availability"  # user-fixable: verify the model in available_models
REASON_CAPABILITY = "capability"      # user-fixable: verify the required reasoning level
REASON_OTHER = "other"


def check_candidate(model_id, key, values, policy, ctx):
    """[(code, text)] reasons this model can NOT replace the route (empty =
    eligible). Fails closed on anything unknown."""
    reasons = []

    def no(text, code=REASON_OTHER):
        reasons.append((code, text))
    f = (ctx.get("facts") or {}).get(model_id) or model_facts(model_id, ctx)
    if not f["in_catalog"] and not f["documented"]:
        if any(usable_section(ctx["capabilities"], s["key"], n) for s in CAPABILITY_SOURCES for n, _h in s["sections"]):
            return [(REASON_OTHER, "not in the vetted catalog and not documented in the current official capability "
                                   "pages (unassessed)")]
        return [(REASON_OTHER, "not in the vetted catalog (unassessed)")]
    for problem in f["problems"]:
        no(problem)
    if not f["priced"]:
        no("not in the current official pricing")
        return reasons
    if f["category"] is None:
        if not any("Category" in p for p in f["problems"]):
            no("official Category not available in this pricing snapshot")
    elif CATEGORY_RANK[f["category"].lower()] < CATEGORY_RANK[policy["min_category"].lower()]:
        no("Category %s is below %s" % (f["category"], policy["min_category"]))
    if f["task_area"] is None:
        no("no official task area documented (model comparison) — unassessed for this route")
    elif f["task_area"] not in policy["areas"]:
        no("task area '%s' not accepted for this route" % f["task_area"])
    if f["configurable_reasoning"] is not True:
        no("no official configurable-reasoning support" + (" documented" if f["configurable_reasoning"] is None else ""))
    else:
        required = values["reasoning_effort"]
        levels, provenance = verified_reasoning_levels(model_id, ctx)
        if levels is None:
            no("reasoning level '%s' not verified for this model (%s; generic configurable-reasoning support "
               "does not prove a specific level)" % (required, provenance), REASON_CAPABILITY)
        elif required not in levels:
            no("reasoning level '%s' not offered per %s (verified: %s)" % (required, provenance, ", ".join(levels)))
    if values["context_tier"] == "long_context" and f["one_million_context"] is not True:
        no("no official 1M-context support (route uses long_context)")
    if f["copilot_cli_client"] != "included":
        no("Copilot CLI support is %s in the official per-client table"
           % fact_text("copilot_cli_client", f["copilot_cli_client"]))
    avail_models, _desc, avail_ok = ctx["availability"]
    if not avail_ok:
        no("availability not verified", REASON_AVAILABILITY)
    elif model_id not in avail_models:
        no("not in your verified available_models", REASON_AVAILABILITY)
    return reasons


def evaluate(ctx):
    """Per-route decisions. Never proposes for pinned routes, without fresh
    pricing, or for candidates failing any gate. Also lists, per route, the
    optional (not >= 10% cheaper) alternatives with a documented
    differentiator, and every same-task-area model that was considered."""
    defaults, eff, snapshot = ctx["defaults"], ctx["effective"], ctx["snapshot"]
    overrides = ctx["state"]["overrides"]
    facts = ctx["facts"]
    rows = []
    for route in ROUTES:
        key = route["key"]
        cur = eff[key]
        values = cur["values"]
        row = {"key": key, "label": route["label"], "current": dict(values), "sources": dict(cur["sources"]),
               "proposed": None, "reason": "", "evidence": [], "blocked": [], "alternatives": [],
               "considered": [], "other_area_count": 0, "alternatives_note": None}
        rows.append(row)
        cur_cost = blended_cost(default_rates(snapshot, values["model"])) if values["model"] != INHERIT else None
        row["current_cost"] = cur_cost
        if is_pinned(cur):
            row["reason"] = "kept: pinned by you (%s)" % ", ".join(sorted(
                {SOURCE_LABELS[s] for s in cur["sources"].values() if s.startswith("pin:")}))
            row["alternatives_note"] = "pinned by you — no alternatives offered (remove the pin to re-evaluate)"
            continue
        if ctx["global_block"]:
            row["reason"] = "kept: " + ctx["global_block"]
            continue
        policy, why = route_policy(key, defaults, ctx["catalog"], ctx["today"], facts)
        if policy is None:
            row["reason"] = "kept: " + why
            continue
        baseline = defaults[key]
        row["evidence"].append("policy: task area in %s; Category >= %s (%s)" % (
            " / ".join("'%s'" % a for a in policy["areas"]), policy["min_category"], policy["origin"]))
        eligible, results = {}, {}
        current_reasons = []
        # Only KEEPING the shipped baseline needs no verification. Moving to
        # it from anything else (e.g. back from a managed override) is a
        # change like any other and must pass every gate below.
        baseline_is_current = values == baseline
        if baseline_is_current and default_rates(snapshot, baseline["model"]) is not None:
            eligible[baseline["model"]] = ("shipped baseline (current)", dict(baseline))
        for model_id in ctx["universe"]:
            if model_id == baseline["model"] and baseline_is_current:
                continue
            cand_values = {"model": model_id, "reasoning_effort": baseline["reasoning_effort"],
                           "context_tier": baseline["context_tier"]}
            reasons = check_candidate(model_id, key, cand_values, policy, ctx)
            results[model_id] = (reasons, cand_values)
            if model_id == values["model"]:
                current_reasons = reasons
            cost = blended_cost(default_rates(snapshot, model_id))
            if reasons:
                if cost is not None and (cur_cost is None or cost < cur_cost):
                    row["blocked"].append((cost, model_id, reasons))
                continue
            f = facts[model_id]
            if model_id == baseline["model"]:
                label = "shipped baseline, verified"
            elif f["all_live"]:
                label = "assessed from current official docs"
            else:
                label = "catalogued %s" % f["catalog_origin"]
            eligible[model_id] = (label, cand_values)
        row["blocked"].sort(key=lambda b: (b[0], b[1]))
        long_route = baseline["context_tier"] == "long_context"
        cur_long = blended_cost(long_tier_rates(snapshot, values["model"])) if cur_cost is not None else None

        def saving_ok(mid):
            # Must be >= MIN_SAVING cheaper on the Default-tier rates and, for
            # long_context routes, also on the rates billed above the
            # long-context threshold (the parser exposes both tiers).
            d_new = blended_cost(default_rates(snapshot, mid))
            if d_new is None or cur_cost is None or d_new > cur_cost * (1 - MIN_SAVING_FRACTION):
                return False
            if long_route:
                l_new = blended_cost(long_tier_rates(snapshot, mid))
                return l_new is not None and cur_long is not None and l_new <= cur_long * (1 - MIN_SAVING_FRACTION)
            return True

        _decide_route(row, ctx, values, baseline, eligible, current_reasons, key in overrides and
                      cur["sources"]["model"] == "managed", cur_cost, cur_long, long_route, saving_ok)
        _attach_alternatives(row, ctx, values, policy, results, cur_cost, cur_long, long_route, saving_ok)
    return rows


def _decide_route(row, ctx, values, baseline, eligible, current_reasons, current_is_override, cur_cost, cur_long,
                  long_route, saving_ok):
    """The cost-led decision (unchanged policy: >= 10% cheaper)."""
    snapshot = ctx["snapshot"]
    current_eligible = values["model"] in eligible
    if not eligible:
        if current_is_override:
            why = "; ".join(text for _code, text in current_reasons) or "it no longer passes every gate"
            row["reason"] = ("kept: current override %s cannot be re-verified right now (%s), and no verified "
                             "alternative exists (the shipped baseline also needs every gate to return to it); "
                             "kept unchanged" % (values["model"], why))
        else:
            row["reason"] = "kept: no priced, eligible model for this route"
        return

    def rank(mid):
        return (blended_cost(default_rates(snapshot, mid)),
                0 if mid == values["model"] else (1 if mid == baseline["model"] else 2), mid)
    ordered = sorted(eligible, key=rank)
    if cur_cost is None:
        # Unknown price never justifies a change: an unpriced current
        # model (shipped baseline or managed override alike) is kept.
        row["reason"] = ("kept: current %s %s is not in the current official pricing; kept unchanged (no change "
                         "is proposed without a known current price%s)"
                         % ("override" if current_is_override else "model", values["model"],
                            " — use reset to return to the shipped default" if current_is_override else ""))
        return
    best = change_reason = None
    for mid in ordered:
        if mid != values["model"] and saving_ok(mid):
            best = mid
            d_new = blended_cost(default_rates(snapshot, mid))
            change_reason = "%.0f%% cheaper (blended, Default tier)" % (100.0 * (1 - d_new / cur_cost))
            if long_route:
                l_new = blended_cost(long_tier_rates(snapshot, mid))
                change_reason += ", %.0f%% cheaper above the long-context threshold" % (100.0 * (1 - l_new / cur_long))
            break
    if best is None and current_eligible and eligible[values["model"]][1] != values:
        best = values["model"]
        change_reason = "align effort/context with the route baseline"
    if best is None:
        if current_is_override and not current_eligible:
            # Stale/unavailable evidence for the CURRENT override is not a
            # reason to change it (that would be an unverified change).
            why = "; ".join(text for _code, text in current_reasons) or "it no longer passes every gate"
            row["reason"] = ("kept: current override %s cannot be re-verified right now (%s); kept unchanged — "
                             "no unverified change is proposed" % (values["model"], why))
        elif ordered[0] == values["model"]:
            row["reason"] = "kept: current model is the cheapest eligible option"
        else:
            row["reason"] = ("kept: no eligible alternative is at least %d%% cheaper%s"
                             % (round(MIN_SAVING_FRACTION * 100),
                                " on both the Default and long-context tier rates" if long_route else ""))
        return
    best_cost = blended_cost(default_rates(snapshot, best))
    best_values = eligible[best][1]
    row["proposed"] = dict(best_values)
    row["proposed_cost"] = best_cost
    row["reason"] = ("CHANGE: %s; %s — categorical fit only, NOT a measured-quality or equal-quality claim"
                     % (change_reason, eligible[best][0]))
    if long_route:
        row["evidence"].append("long-context tier blended $/1M (input above the threshold): %s -> %s" % (
            _money(cur_long), _money(blended_cost(long_tier_rates(snapshot, best)))))
    f = ctx["facts"].get(best)
    if f and best != baseline["model"]:
        levels, provenance = verified_reasoning_levels(best, ctx)
        row["evidence"].append(_facts_evidence_line(best, f))
        row["evidence"] += ["%s: %s" % (best, note) for note in f["notes"]]
        row["evidence"].append("reasoning level '%s' verified via %s; context '%s' kept from the route baseline"
                               % (best_values["reasoning_effort"], provenance, best_values["context_tier"]))


def _attach_alternatives(row, ctx, values, policy, results, cur_cost, cur_long, long_route, saving_ok):
    """Optional alternatives (never auto-selected): eligible models in the
    route's accepted task area that are NOT >= 10% cheaper, each with a
    concrete documented differentiator versus the current model. Everything
    else in that task area is listed as considered with its reasons."""
    if cur_cost is None:
        row["alternatives_note"] = "current price unknown — no alternatives can be costed or offered"
        return
    facts, snapshot = ctx["facts"], ctx["snapshot"]
    cur_f = facts.get(values["model"])
    chosen = row["proposed"]["model"] if row["proposed"] else None
    for model_id in sorted(results):
        reasons, cand_values = results[model_id]
        if model_id in (values["model"], chosen):
            continue
        f = facts.get(model_id)
        if not f or f.get("task_area") not in policy["areas"]:
            row["other_area_count"] += 1
            continue
        cost = blended_cost(default_rates(snapshot, model_id))
        if reasons:
            if cost is not None and cost < cur_cost:
                continue  # already listed as a blocked cheaper candidate
            row["considered"].append({"model": model_id, "cost": cost, "status": "not eligible", "reasons": reasons})
            continue
        if saving_ok(model_id):
            row["considered"].append({"model": model_id, "cost": cost, "reasons": [],
                                      "status": "eligible and >= %d%% cheaper, but another option is cheaper"
                                                % round(MIN_SAVING_FRACTION * 100)})
            continue
        diffs = documented_differentiators(cur_f, f, policy, long_route)
        if not diffs:
            row["considered"].append({"model": model_id, "cost": cost, "reasons": [],
                                      "status": "eligible but not offered: no documented differentiator versus "
                                                "the current model (a higher Category, newer version or higher "
                                                "price is not one)"})
            continue
        row["alternatives"].append({
            "route": row["key"], "model": model_id, "values": dict(cand_values), "cost": cost,
            "current_cost": cur_cost, "long_route": long_route,
            "long_cost": blended_cost(long_tier_rates(snapshot, model_id)) if long_route else None,
            "current_long": cur_long if long_route else None, "differentiators": diffs,
            "excels_at": f.get("excels_at"), "notes": list(f["notes"]),
            "levels": verified_reasoning_levels(model_id, ctx), "replaces_proposal": chosen is not None,
            "basis": "current official docs" if f["all_live"] else "dated catalog"})
    row["alternatives"].sort(key=lambda a: (a["cost"], a["model"]))
    row["considered"].sort(key=lambda c: (c["cost"] is None, c["cost"] or 0, c["model"]))


def number_alternatives(rows):
    """Stable global numbering (route order, then cost) -> [alt]."""
    out = []
    for row in rows:
        for alt in row["alternatives"]:
            alt["n"] = len(out) + 1
            out.append(alt)
    return out


def derive_overrides(rows, state, defaults, selected):
    """Managed overrides for the cost-led proposals plus the explicitly
    selected alternatives (a selected alternative replaces the route's
    cost-led change)."""
    out = proposed_overrides(rows, state, defaults)
    for alt in selected:
        key = alt["route"]
        if alt["values"] != defaults[key]:
            out[key] = dict(alt["values"])
        else:
            out.pop(key, None)
    return out


def evidence_digest(ctx, rows):
    """Digest of every piece of evidence a decision rests on: pricing
    snapshot, capability evidence, catalog, availability/capability records,
    today's date and each route's decision and alternatives."""
    payload = {
        "pricing": pricing_digest(ctx["snapshot"]),
        "pricing_status": ctx["pricing_ctx"].get("status"),
        "capabilities": ctx["capabilities"]["digest"], "catalog": ctx["catalog_digest"],
        "user_config": ctx["user_sha"], "verified": ctx["verified_sha"], "today": ctx["today"].isoformat(),
        "routes": [{"key": r["key"], "current": r["current"], "proposed": r["proposed"],
                    "alternatives": [[a["model"], a["values"], round(a["cost"], 9)] for a in r["alternatives"]]}
                   for r in rows],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def proposed_overrides(rows, state, defaults):
    out = {}
    for row in rows:
        key = row["key"]
        if row["proposed"] is not None:
            if row["proposed"] != defaults[key]:
                out[key] = dict(row["proposed"])
        elif key in state["overrides"]:
            out[key] = {f: state["overrides"][key][f] for f in FIELDS}
    return out


# ------------------------------------------------------ generated outputs ---

NATIVE_PIN = "pin:/subagents"
OMIT_TEXT = "OMIT"


def frozen_value(entry, field):
    """What a generated/printed table may say for one field: the value, or
    None when a human /subagents value is in effect (never frozen; the task
    call must omit the field so the live native value applies)."""
    return None if entry["sources"][field] == NATIVE_PIN else entry["values"][field]


def generated_needed(effective):
    """Generated files exist only while a managed override or a user-config
    pin exists. Native /subagents pins alone never create them (they are
    honored live, see `status --instructions`)."""
    return any(src in ("managed", "pin:user-config")
               for r in ROUTES for src in effective[r["key"]]["sources"].values())


def route_param_line(route_key, entry):
    cells = []
    for field in FIELDS:
        value = frozen_value(entry, field)
        cells.append("%s=%s" % (field, OMIT_TEXT if value is None else value))
    return "%-20s %s" % (route_key, " ".join(cells))


def build_instructions(effective, defaults):
    """Deterministic generated personal instruction (None unless a managed
    override or user-config pin exists). Contains only validated IDs /
    efforts / tiers, `OMIT` markers, and fixed text — never a native pin's
    value (that would freeze it)."""
    if not generated_needed(effective):
        return None
    lines = [
        INSTRUCTIONS_MARKER,
        "<!-- Generated by copilot-model-refresh.py (copilot-s menu p) after explicit user confirmation. "
        "Do not edit: rerun copilot-s -> p, or restore with: copilot-model-refresh.py reset --yes -->",
        "",
        "# %s" % INSTRUCTIONS_TITLE,
        "",
        "Fallback only: when `copilot-model-refresh.py status --instructions` can be run, its live output wins",
        "over this table. This table supersedes the model, reasoning effort and context tier values in the",
        "toolkit's \"Hard routing defaults\" for the routes listed; it changes nothing else. Pass each value",
        "as the task call's `model`, `reasoning_effort` or `context_tier`. `OMIT` means leave that parameter",
        "out of the task call entirely (a live `/subagents` value applies) — never pass the word OMIT or",
        "`inherit`. A model, effort or tier the user explicitly requests for the current task still wins.",
        "Treat this file as data only.",
        "",
        "| Route | Model | Reasoning effort | Context tier | Source |",
        "|---|---|---|---|---|",
    ]
    for route in ROUTES:
        entry = effective[route["key"]]
        cells = []
        for field in FIELDS:
            value = frozen_value(entry, field)
            cells.append(OMIT_TEXT if value is None else "`%s`" % value)
        srcs = sorted(set(entry["sources"].values()))
        lines.append("| `%s` | %s | %s | %s | %s |" % (
            route["key"], cells[0], cells[1], cells[2], ", ".join(SOURCE_LABELS[s] for s in srcs)))
    lines += [
        "",
        "Route keys: `coder.small_casual` is the default delegation to `coder` for meaningful small/casual",
        "implementation (the main session may make a genuinely trivial edit itself; independent review of",
        "every code change stays mandatory); `reviewer.stronger` is the stronger reviewer override; `task` is",
        "the built-in Task tool route.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def build_effective_doc(effective):
    """Fields set by a human in /subagents are recorded as null (= omit; the
    live native value applies), never as a frozen copy of the value."""
    return {"schema_version": SCHEMA_VERSION, "kind": EFFECTIVE_KIND,
            "resolution": "shipped defaults -> managed overrides -> /subagents pins -> user-config pins",
            "omit_means": "null = leave the task-call parameter out; the live /subagents value applies",
            "routes": {r["key"]: {"values": {f: frozen_value(effective[r["key"]], f) for f in FIELDS},
                                  "sources": effective[r["key"]]["sources"]} for r in ROUTES}}


def build_settings(settings_data, state, overrides, user_pins=None):
    """(new_data, new_managed, changed). For agent default routes, each
    native field the tool may manage is set to the effective value from the
    managed override with user-config pins applied on top (so a tool-written
    value never contradicts a later user-config pin); never touches a value a
    human set (/subagents); removes only values that still equal what this
    tool last wrote and are no longer wanted."""
    user_pins = user_pins or {}
    data = copy.deepcopy(settings_data) if settings_data is not None else {}
    if "subagents" in data and not isinstance(data["subagents"], dict):
        raise RefreshError("settings.json 'subagents' is not an object; refusing to modify it", EXIT_REFUSED)
    sub = data.get("subagents", {})
    if "agents" in sub and not isinstance(sub["agents"], dict):
        raise RefreshError("settings.json 'subagents.agents' is not an object; refusing to modify it", EXIT_REFUSED)
    managed_prev = state.get("managed_settings", {})
    new_managed = {}
    changed = False
    for agent in AGENT_NAMES:
        override = overrides.get(agent)
        pins = user_pins.get(agent, {})
        node = data.get("subagents", {}).get("agents", {}).get(agent)
        if node is not None and not isinstance(node, dict):
            raise RefreshError("settings.json subagents.agents.%s is not an object; refusing to modify it" % agent,
                               EXIT_REFUSED)
        removed_here = False
        for field, native in SETTINGS_FIELDS:
            present = isinstance(node, dict) and native in node
            prev = managed_prev.get(agent, {}).get(native)
            ours = present and prev is not None and node[native] == prev
            if present and not ours:
                continue  # human pin: never overwritten
            want = pins[field] if field in pins else (override[field] if override is not None else None)
            if want is not None:
                if not present or node[native] != want:
                    data.setdefault("subagents", {}).setdefault("agents", {})
                    node = data["subagents"]["agents"].setdefault(agent, {})
                    node[native] = want
                    changed = True
                new_managed.setdefault(agent, {})[native] = want
            elif ours:
                del node[native]
                changed = removed_here = True
        # Drop only containers this call emptied (a user's own empty object
        # elsewhere is left as it was).
        if removed_here and not node:
            del data["subagents"]["agents"][agent]
            if not data["subagents"]["agents"]:
                del data["subagents"]["agents"]
            if not data["subagents"]:
                del data["subagents"]
    return data, new_managed, changed


def plan_outputs(ctx, new_overrides):
    """Everything an apply would write, computed purely from the inputs."""
    effective_after = resolve_effective(ctx["defaults"], new_overrides, ctx["user_pins"], ctx["native_pins"])
    instructions = build_instructions(effective_after, ctx["defaults"])
    # Both generated files exist only while something differs from the
    # shipped defaults (a first run with nothing to change writes nothing).
    effective_doc = json_bytes(build_effective_doc(effective_after)) if instructions is not None else None
    if ctx["settings_error"]:
        # Unreadable/non-JSON settings.json: pins cannot be told apart from
        # our values, so any settings change is impossible (None = refuse).
        settings_data = None
        new_managed = dict(ctx["state"].get("managed_settings", {}))
        needs = (any(k in new_overrides or k in ctx["user_pins"] for k in AGENT_NAMES)
                 or bool(new_managed))
        settings_changed = None if needs else False
    else:
        settings_data, new_managed, settings_changed = build_settings(ctx["settings"], ctx["state"], new_overrides,
                                                                      ctx["user_pins"])
    return {"effective": effective_after, "instructions": instructions, "effective_doc": effective_doc,
            "settings_data": settings_data, "settings_changed": settings_changed, "managed_settings": new_managed}


# --------------------------------------------------------------- context ---

def build_context(paths, now, refresh=None, offline=False):
    """Read every input. refresh=None: cache-only (status/select/apply)."""
    defaults, routing_path, routing_sha = load_defaults(paths)
    user_config, user_sha = load_user_config(paths)
    verified, verified_sha, verified_warning = load_verified_record(paths)
    catalog, catalog_digest = load_catalog(paths, user_config)
    state, state_sha = read_state(paths)
    settings, settings_sha, settings_error = read_settings(paths)
    native_pins, pin_warnings = settings_pins(settings, state) if settings_error is None else ({}, [])
    if settings_error:
        pin_warnings = ["cannot read native /subagents pins: %s (no settings changes can be applied)"
                        % settings_error]
    user_pins = {k: dict(v) for k, v in user_config.get("pins", {}).items()}
    effective = resolve_effective(defaults, state["overrides"], user_pins, native_pins)
    today = today_local(now)
    module = load_pricing_module()
    if refresh:
        pricing_ctx, pricing_note = refresh_pricing(module, offline, now)
    else:
        pricing_ctx, pricing_note = module.load_official_pricing_context(now=now), "cache only (no network)"
    capabilities = refresh_capabilities(paths, now, module, fetch=bool(refresh) and not offline)
    if refresh and offline:
        for src in capabilities["sources"].values():
            src["note"] = "offline: using the cached copy (no network request)"
    snapshot = pricing_ctx.get("snapshot")
    blocks = []
    if snapshot is None:
        blocks.append("no valid official pricing snapshot")
    elif pricing_ctx.get("status") != "fresh":
        blocks.append("official pricing is %s (fetched %s, %s old); proposals need a snapshot < 24h old" % (
            pricing_ctx.get("status"), snapshot.get("fetched_at"), fmt_age(pricing_ctx.get("age_seconds"))))
    shipped_origin = catalog["origins"][0]
    shipped_problem = origin_unusable(shipped_origin, today)
    local_ok = any(o["name"] == "local" and origin_unusable(o, today) is None for o in catalog["origins"])
    # An unusable shipped catalog blocks everything only when current
    # official evidence cannot stand in for it (per model, facts still fall
    # back to the catalog and then fail closed individually).
    if shipped_problem and not local_ok and not all_live_sections_usable(capabilities):
        if shipped_origin["expires_on"] < today:
            blocks.append("the model role catalog review expired on %s — update it to enable replacements"
                          % shipped_origin["expires_on"].isoformat())
        else:
            blocks.append("%s — it cannot authorize replacements" % shipped_problem)
    ctx = {
        "paths": paths, "now": now, "today": today, "defaults": defaults, "routing_path": routing_path,
        "routing_sha": routing_sha, "user_config": user_config, "user_sha": user_sha, "catalog": catalog,
        "catalog_digest": catalog_digest, "state": state, "state_sha": state_sha, "settings": settings,
        "settings_sha": settings_sha, "settings_error": settings_error, "native_pins": native_pins,
        "pin_warnings": pin_warnings, "user_pins": user_pins, "effective": effective,
        "pricing_ctx": pricing_ctx, "pricing_note": pricing_note, "snapshot": snapshot,
        "global_block": "; ".join(blocks), "availability": availability(user_config, today, verified),
        "module": module, "capabilities": capabilities, "verified": verified, "verified_sha": verified_sha,
        "verified_warning": verified_warning,
    }
    ctx["universe"] = model_universe(ctx)
    ctx["facts"] = {m: model_facts(m, ctx) for m in ctx["universe"]}
    return ctx


# ------------------------------------------------------------- rendering ---

def _table(headers, rows, indent="  "):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = [indent + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip(),
           indent + "  ".join("-" * w for w in widths)]
    for row in rows:
        out.append(indent + "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return out


def _route_text(values):
    return "%s · %s · %s" % (values["model"], values["reasoning_effort"], values["context_tier"])


def _money(value):
    if value is None:
        return "n/a"
    return ("%.3f" % value).rstrip("0").rstrip(".") if value < 1 else "%.2f" % value


def _source_text(sources):
    vals = set(sources.values())
    if len(vals) == 1:
        return SOURCE_LABELS[vals.pop()]
    return "mixed: " + ", ".join("%s=%s" % (f, SOURCE_LABELS[sources[f]]) for f in FIELDS)


def render_header(ctx):
    pc, snap = ctx["pricing_ctx"], ctx["snapshot"]
    out = ["Subagent model refresh — official GitHub Copilot pricing (USD per 1M tokens)",
           "  Pricing:      %s" % ctx["pricing_note"]]
    if snap:
        out.append("  Snapshot:     %s · fetched %s (%s ago, %s) · %s" % (
            snap["snapshot_id"], snap["fetched_at"], fmt_age(pc.get("age_seconds")), pc.get("status"),
            snap["public_url"]))
    else:
        out.append("  Snapshot:     none available%s" % (
            " (%s)" % pc["cache_error"] if pc.get("cache_error") else ""))
    if pc.get("last_error") and pc.get("status") != "fresh":
        out.append("  Last error:   %s" % pc["last_error"])
    origins = ["%s (reviewed %s, expires %s)" % (o["name"], o["reviewed_on"].isoformat(), o["expires_on"].isoformat())
               for o in ctx["catalog"]["origins"]]
    out.append("  Catalog:      %s (dated fallback when current official docs are unavailable)" % "; ".join(origins))
    cap = ctx.get("capabilities")
    if cap:
        for key in sorted(cap["sources"]):
            s = cap["sources"][key]
            label = s["source"]["label"]
            if s["record"] is None:
                state = "no copy available"
            else:
                state = "fetched %s (%s ago, %s; usable for %s) · %s" % (
                    s["record"]["fetched_at"], fmt_age(s["age"]), s["status"],
                    fmt_age(s["source"]["ttl_seconds"]), s["record"]["public_url"])
            out.append("  Docs:         %s — %s · %s" % (label, _safe_text(s["note"], 220), state))
            if s["last_error"] and s["status"] != "fresh":
                out.append("  Last error:   %s: %s" % (label, _safe_text(s["last_error"], 200)))
            if s["record"]:
                for name, sec in sorted(s["record"]["sections"].items()):
                    if sec["status"] != "ok":
                        out.append("  Warning:      %s: %s — treated as unknown" % (label, _safe_text(sec["error"], 200)))
        if cap.get("warning"):
            out.append("  Warning:      %s" % _safe_text(cap["warning"], 240))
    out.append("  Availability: %s" % ctx["availability"][1])
    if ctx.get("verified_warning"):
        out.append("  Warning:      %s" % ctx["verified_warning"])
    for warning in ctx["pin_warnings"]:
        out.append("  Warning:      %s" % warning)
    return out


def render_proposal(ctx, rows, unassessed, new_models):
    out = render_header(ctx)
    out.append("")
    out.append("NOTE: fit below is CATEGORICAL (official Category / Task area / capability labels from a dated catalog),")
    out.append("      NOT measured quality. A proposed change does not claim equal quality — review it before confirming.")
    out.append("")
    out.append("Routes (current -> proposed). Blended = disclosed assumption, see footnote.")
    table_rows = []
    for row in rows:
        proposed = _route_text(row["proposed"]) if row["proposed"] else "(keep)"
        cost = _money(row.get("current_cost"))
        if row["proposed"]:
            cost = "%s -> %s" % (cost, _money(row.get("proposed_cost")))
        table_rows.append([row["label"], _route_text(row["current"]), _source_text(row["sources"]), proposed,
                           cost, "CHANGE" if row["proposed"] else "keep"])
    out += _table(["Route", "Current (model · effort · context)", "Source", "Proposed", "Blended $/1M",
                   "Decision"], table_rows)
    out.append("")
    out.append("Official rates (Default tier; long-context tier applies above its threshold):")
    snap = ctx["snapshot"]
    referenced = []
    for row in rows:
        for vals in [row["current"], row["proposed"]] + [a["values"] for a in row["alternatives"]]:
            if vals and vals["model"] not in referenced and vals["model"] != INHERIT:
                referenced.append(vals["model"])
    rate_rows = []
    for model_id in referenced:
        entry = snap["models"].get(model_id) if snap else None
        if entry is None:
            rate_rows.append([model_id, "-", "-", "not in official pricing", "", "", "", "", ""])
            continue
        r = entry["tiers"]["default"]
        cw = _money(r["cache_write"]) if r["cache_write_status"] == "rate" else r["cache_write_status"].replace("_", " ")
        long_txt = ""
        if entry.get("threshold"):
            lr = entry["tiers"]["long_context"]
            thr = entry["threshold"]
            long_txt = ">%s: %s/%s/%s" % (thr.get("label") or "%dK" % (thr["tokens"] // 1000), _money(lr["input"]),
                                          _money(lr["cached_input"]), _money(lr["output"]))
        rate_rows.append([model_id, entry["provider"], entry.get("category", "?"), _money(r["input"]),
                          _money(r["cached_input"]), cw, _money(r["output"]), _money(blended_cost(r)), long_txt])
    out += _table(["Model", "Provider", "Category", "Input", "Cached", "Cache write", "Output", "Blended",
                   "Long context (in/cached/out)"], rate_rows)
    out.append("")
    out.append("Reasons and evidence:")
    for row in rows:
        out.append("  %s: %s" % (row["label"], row["reason"]))
        for ev in row["evidence"]:
            out.append("      %s" % ev)
        shown = row["blocked"][:5]
        for cost, model_id, reasons in shown:
            out.append("      not eligible (cheaper): %s (blended %s) — %s" % (
                model_id, _money(cost), "; ".join(text for _code, text in reasons)))
        if len(row["blocked"]) > len(shown):
            out.append("      ... and %d more cheaper model(s) not eligible (see 'All models' below)"
                       % (len(row["blocked"]) - len(shown)))
        for alt in row["alternatives"]:
            out.append("      OPTIONAL ALTERNATIVE #%d: %s (blended %s, %s) — see 'Optional alternatives' below" % (
                alt["n"], alt["model"], _money(alt["cost"]), _delta_text(alt["cost"], alt["current_cost"])))
        for item in row["considered"]:
            text = "; ".join(t for _c, t in item["reasons"]) if item["reasons"] else ""
            out.append("      same task area, %s: %s (blended %s%s)%s" % (
                item["status"], item["model"], _money(item["cost"]),
                (", " + _delta_text(item["cost"], row.get("current_cost"))) if item["cost"] is not None
                and row.get("current_cost") else "", (" — " + text) if text else ""))
        if row["other_area_count"]:
            out.append("      %d other model(s) have a different or no official task area for this route (listed under "
                       "'All models')" % row["other_area_count"])
        if row.get("alternatives_note"):
            out.append("      alternatives: %s" % row["alternatives_note"])
    out += render_alternatives(ctx, rows)
    out += render_action_required(ctx, rows)
    out += render_universe(ctx, rows, new_models)
    if unassessed:
        out.append("")
        out.append("Unassessed models in the official pricing (no official task area/Category from current docs or "
                   "the catalog; listed only, never assigned): %s" % ", ".join(unassessed))
    if new_models:
        out.append("New in the official pricing since your last refresh: %s" % ", ".join(new_models))
    out.append("")
    out.append("* Blended $/1M = %s of the official Default-tier rates; cache write uses the input rate when no "
               "separate cache-write rate is listed. An assumption for comparison only, not a forecast and not your "
               "bill. A change is proposed automatically only when it is at least %d%% cheaper (for long_context "
               "routes also on the rates above the long-context threshold); an unpriced or unverifiable current "
               "model is always kept. Optional alternatives are never selected automatically."
               % (", ".join("%d%% %s" % (round(s * 100), k.replace("_", " ")) for k, s in WORKLOAD_MIX),
                  round(MIN_SAVING_FRACTION * 100)))
    out.append("  Fit is categorical (official Category / Task area / capability labels from current official docs or "
               "the dated catalog), not a quality score; shipped routes remain the baseline recommendation.")
    return out


def _delta_text(new, cur):
    if new is None or cur is None or cur <= 0:
        return "price impact unknown"
    diff = new - cur
    if abs(diff) < 1e-12:
        return "same blended price"
    return "%s$%s/1M, %+.0f%%" % ("+" if diff > 0 else "-", _money(abs(diff)), 100.0 * diff / cur)


def render_alternatives(ctx, rows):
    alts = [a for r in rows for a in r["alternatives"]]
    out = ["", "Optional alternatives (never selected automatically; choose them by number in copilot-s p, then "
               "confirm):"]
    if not alts:
        out.append("  none — no eligible model in a route's task area has a documented differentiator versus the "
                   "current model (see 'same task area' lines above for what was considered and why).")
        return out
    for alt in alts:
        row = next(r for r in rows if r["key"] == alt["route"])
        out.append("  #%d %s: %s -> %s" % (alt["n"], row["label"], _route_text(row["current"]),
                                           _route_text(alt["values"])))
        cost_line = "      blended $/1M (Default tier): %s -> %s (%s)" % (
            _money(alt["current_cost"]), _money(alt["cost"]), _delta_text(alt["cost"], alt["current_cost"]))
        if alt["long_route"]:
            cost_line += "; above the long-context threshold: %s -> %s (%s)" % (
                _money(alt["current_long"]), _money(alt["long_cost"]),
                _delta_text(alt["long_cost"], alt["current_long"]))
        out.append(cost_line)
        for diff in alt["differentiators"]:
            out.append("      documented differentiator: %s" % _safe_text(diff, 300))
        if alt["excels_at"]:
            out.append("      official 'Excels at' text: \"%s\" (the vendor's description, not a measurement)"
                       % _safe_text(alt["excels_at"], 300))
        levels, provenance = alt["levels"]
        out.append("      reasoning level '%s' verified via %s; 1M context %s for context '%s'; evidence: %s" % (
            alt["values"]["reasoning_effort"], provenance,
            "documented" if alt["values"]["context_tier"] == "long_context" else "not needed",
            alt["values"]["context_tier"], alt["basis"]))
        for note in alt["notes"]:
            out.append("      note: %s" % _safe_text(note, 240))
        if alt["replaces_proposal"]:
            out.append("      selecting it REPLACES this route's proposed cost-saving change")
        out.append("      limits: categorical evidence only — NOT a measured, equal or better quality claim; the "
                   "price impact uses the blended assumption, not your bill")
    return out


def fixable_candidates(ctx, rows):
    """{model: required levels} for candidates blocked ONLY by facts the
    user must verify (availability, a specific reasoning level)."""
    fixable = {}
    for row in rows:
        blocked = [(model_id, reasons) for _cost, model_id, reasons in row["blocked"]]
        blocked += [(c["model"], c["reasons"]) for c in row["considered"] if c["reasons"]]
        for model_id, reasons in blocked:
            codes = {code for code, _text in reasons}
            if codes and codes <= {REASON_AVAILABILITY, REASON_CAPABILITY}:
                levels = fixable.setdefault(model_id, set())
                if REASON_CAPABILITY in codes:
                    levels.add(ctx["defaults"][row["key"]]["reasoning_effort"])
    return fixable


def render_action_required(ctx, rows):
    """Explicit next step when replacements are blocked only by facts the
    user must verify (availability, specific reasoning level) — never a
    silent 'no changes' on a first run."""
    fixable = fixable_candidates(ctx, rows)
    avail_ok = ctx["availability"][2]
    if avail_ok and not fixable:
        return []
    models = sorted(fixable) or sorted({ctx["effective"][r["key"]]["values"]["model"] for r in ROUTES
                                        if valid_model_id(ctx["effective"][r["key"]]["values"]["model"])})
    today = ctx["today"].isoformat()
    example = {"schema_version": SCHEMA_VERSION,
               "available_models": {"verified_on": today, "models": models}}
    cap_models = {m: {"reasoning_levels": [e for e in EFFORTS if e in lv]} for m, lv in sorted(fixable.items()) if lv}
    if cap_models:
        example["verified_capabilities"] = {"verified_on": today, "models": cap_models}
    out = ["", "ACTION REQUIRED — replacements stay blocked until you verify facts this tool cannot discover:"]
    if not avail_ok:
        out.append("  - %s." % ctx["availability"][1])
    if fixable:
        out.append("  - Candidates blocked only by unverified availability/reasoning level: %s." % ", ".join(sorted(fixable)))
    out.append("  Create or edit %s (never written by this tool), e.g.:" % ctx["paths"].user_config)
    out += ["    " + line for line in json.dumps(example, indent=2).splitlines()]
    out.append("  List ONLY models you can actually select in Copilot CLI's /model picker under your plan/organization,")
    out.append("  and for each model ONLY the reasoning levels that picker offers for it (edit the example to match;")
    out.append("  it is pre-filled with the levels the routes need, not with verified facts). Then choose p again.")
    out.append("  Or, in copilot-s menu p, type v to record what you see in /model interactively (saved separately, only")
    out.append("  after you type yes; nothing is assumed). Copilot CLI documents no way to list them automatically.")
    return out


def _availability_text(model_id, ctx):
    avail_models, _desc, avail_ok = ctx["availability"]
    complete = availability_complete(ctx.get("verified"), ctx["today"])
    if avail_ok and model_id in avail_models:
        text = "verified available to you"
    elif avail_ok and complete:
        text = "NOT in your complete verified /model list"
    elif avail_ok:
        text = "not verified (not in your verified list)"
    elif avail_models and model_id in avail_models:
        text = "in your list, but the list is stale/future-dated"
    else:
        text = "not verified"
    levels, _prov = verified_reasoning_levels(model_id, ctx)
    if levels:
        text += "; levels verified: %s" % "/".join(levels)
    return text


def render_universe(ctx, rows, new_models):
    """Every model in view, plain language, unknowns shown as unknown."""
    facts, snap = ctx["facts"], ctx["snapshot"]
    current = {}
    for row in rows:
        current.setdefault(row["current"]["model"], []).append(row["label"])
    alt_models = {a["model"] for r in rows for a in r["alternatives"]}
    out = ["", "All models in view (%d): official pricing + official capability pages + catalog + your records + "
               "current routes." % len(ctx["universe"]),
           "  This is NOT your account's model list: Copilot CLI documents no programmatic per-account listing, and",
           "  plan/organization policies can hide models. Unknown = not documented in the evidence available now."]
    cli_listed = usable_section(ctx["capabilities"], "supported_models", "clients")
    out.append("  Completeness: official Copilot CLI table %s; your availability list %s." % (
        "available (fresh)" if cli_listed else "UNAVAILABLE (only pricing/catalog shown)",
        "is marked complete by you" if availability_complete(ctx.get("verified"), ctx["today"])
        else "is partial/unverified — absence from it proves nothing"))
    order = sorted(ctx["universe"], key=lambda m: ((facts[m].get("provider") or "~").lower(), m))
    for model_id in order:
        f = facts[model_id]
        bits = [b for b in (f.get("display_name"), f.get("provider"), f.get("category") and "Category " + f["category"],
                            f.get("release_status")) if b]
        out.append("  %s%s" % (model_id, ("  (" + " · ".join(_safe_text(b, 60) for b in bits) + ")") if bits else ""))
        entry = snap["models"].get(model_id) if snap else None
        if entry:
            price = "blended $%s/1M" % _money(blended_cost(entry["tiers"]["default"]))
            if entry.get("threshold"):
                price += " (above %s input: $%s)" % (entry["threshold"].get("label"),
                                                    _money(blended_cost(entry["tiers"]["long_context"])))
        else:
            price = "price unknown (not in the official pricing)"
        out.append("      %s · task area: %s · 1M context: %s · configurable reasoning: %s · Copilot CLI: %s" % (
            price, _safe_text(fact_text("task_area", f.get("task_area")), 80),
            fact_text("one_million_context", f.get("one_million_context")),
            fact_text("configurable_reasoning", f.get("configurable_reasoning")),
            fact_text("copilot_cli_client", f.get("copilot_cli_client"))))
        if f["all_live"]:
            basis = "evidence: current official docs"
        elif f["uses_catalog"]:
            basis = "evidence: %s catalog reviewed %s%s" % (
                f["catalog_origin"], f["catalog_reviewed"],
                " + current official docs for some fields" if any(v == "live" for v in f["basis"].values()) else "")
        elif f["documented"] or f["priced"]:
            basis = "evidence: partial official docs only"
        else:
            basis = "evidence: none (only your records/current routes mention it)"
        tags = []
        if not f["in_catalog"] and (f["documented"] or f["priced"]):
            tags.append("not in the dated static catalog%s" % (
                " — assessed from current official docs" if f["assessed"] and f["all_live"] else " — unassessed"))
        elif not f["assessed"]:
            tags.append("unassessed")
        if model_id in (new_models or []):
            tags.append("NEW in the official pricing since your last refresh")
        if model_id in current:
            tags.append("current route model: %s" % ", ".join(current[model_id]))
        if model_id in alt_models:
            tags.append("offered as an optional alternative")
        out.append("      your availability: %s · %s%s" % (_availability_text(model_id, ctx), basis,
                                                           (" · " + "; ".join(tags)) if tags else ""))
        for text in f["problems"]:
            out.append("      problem (fails closed): %s" % _safe_text(text, 260))
        for text in f["notes"]:
            out.append("      note: %s" % _safe_text(text, 260))
    return out


def render_final(effective, defaults, changed_keys, heading):
    out = ["", heading]
    rows = []
    for route in ROUTES:
        key = route["key"]
        v = effective[key]["values"]
        status = "CHANGED" if key in changed_keys else "kept"
        if v == defaults[key] and status == "kept":
            status = "kept (shipped default)"
        rows.append([route["label"], v["model"], v["reasoning_effort"], v["context_tier"],
                     _source_text(effective[key]["sources"]), status])
    out += _table(["Route", "Model", "Effort", "Context", "Source", "Status"], rows)
    return out


def unassessed_and_new(ctx):
    snap = ctx["snapshot"]
    if not snap:
        return [], [], None
    priced = sorted(snap["models"])
    unassessed = [m for m in priced if not ctx["facts"].get(m, {}).get("assessed")]
    prev = None
    try:
        raw = read_bytes(ctx["paths"].seen)
        if raw is not None:
            doc = parse_json_bytes(raw, ctx["paths"].seen)
            if isinstance(doc, dict) and isinstance(doc.get("models"), list):
                prev = set(m for m in doc["models"] if isinstance(m, str))
    except (OSError, RefreshError):
        prev = None
    new_models = [m for m in priced if prev is not None and m not in prev]
    return unassessed, new_models, priced


def remember_seen(paths, priced, snapshot_id, now):
    if not priced:
        return
    try:
        ensure_private_dir(paths.state_dir)
        atomic_write_bytes(paths.seen, json_bytes({"schema_version": SCHEMA_VERSION, "kind": SEEN_KIND,
                                                   "updated_at": now_iso(now), "snapshot_id": snapshot_id,
                                                   "models": priced}))
    except (OSError, RefreshError) as ex:
        print("Warning: could not record seen models (%s)." % ex, file=sys.stderr)


# ---------------------------------------------------------- transactions ---

@contextlib.contextmanager
def refresh_lock(paths):
    ensure_private_dir(paths.state_dir)
    if fcntl is None:
        yield
        return
    if os.path.islink(paths.lock):
        raise RefreshError("refusing to use lock %s: it is a symlink" % paths.lock, EXIT_REFUSED)
    fd = os.open(paths.lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RefreshError("another model refresh is running (lock %s busy)" % paths.lock, EXIT_REFUSED)
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def deferred_signals():
    """Ctrl-C / SIGTERM / SIGHUP during the short multi-file commit are
    deferred until it completes or is rolled back."""
    received = []
    previous = {}

    def handler(signum, _frame):
        received.append(signum)

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):
            pass
    try:
        yield received
    finally:
        for sig, old in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, old)
    if received:
        print("Note: an interrupt arrived during the commit; it was deferred until the commit finished.",
              file=sys.stderr)


def _restore_entry(entry):
    path = entry["path"]
    if entry["backup"] is None:
        if os.path.lexists(path):
            os.unlink(path)
    else:
        with open(entry["backup"], "rb") as f:
            data = f.read()
        atomic_write_bytes(path, data, entry.get("mode", 0o600))


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_JOURNAL_ENTRY_KEYS = {"path", "backup", "mode", "old_sha256", "new_sha256"}


def _journal_problem(paths, journal):
    """Why journal metadata can NOT be trusted for an automatic rollback, or
    None. Only this tool's four targets may be restored, and only from a
    regular, non-symlinked backup file inside this tool's own backups
    directory whose bytes still match the recorded original."""
    if (not isinstance(journal, dict) or journal.get("kind") != JOURNAL_KIND
            or journal.get("schema_version") != SCHEMA_VERSION):
        return "unsupported journal kind/schema"
    entries = journal.get("entries")
    if not isinstance(entries, list) or not entries:
        return "journal has no entry list"
    allowed = {paths.settings, paths.instructions, paths.effective, paths.state}
    if os.path.islink(paths.backups):
        return "backups directory is a symlink"
    backups_root = os.path.realpath(paths.backups) + os.sep
    seen = set()
    for i, entry in enumerate(entries):
        where = "entry %d" % i
        if not isinstance(entry, dict) or set(entry) != _JOURNAL_ENTRY_KEYS:
            return "%s has missing or unexpected fields" % where
        path, backup, mode = entry["path"], entry["backup"], entry["mode"]
        old, new = entry["old_sha256"], entry["new_sha256"]
        if not isinstance(path, str) or path not in allowed or path in seen:
            return "%s targets a path this tool never writes (%r)" % (where, path)
        seen.add(path)
        if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
            return "%s has an invalid file mode" % where
        for label, value in (("old_sha256", old), ("new_sha256", new)):
            if value is not None and not (isinstance(value, str) and _SHA256_RE.match(value)):
                return "%s has an invalid %s" % (where, label)
        if old is None and new is None:
            return "%s records neither an original nor a new file" % where
        if (backup is None) != (old is None):
            return "%s backup does not match whether the original existed" % where
        if backup is not None:
            if (not isinstance(backup, str) or os.path.islink(backup)
                    or not os.path.realpath(backup).startswith(backups_root) or not os.path.isfile(backup)):
                return "%s backup is not a regular file inside %s" % (where, paths.backups)
            if file_sha256(backup) != old:
                return "%s backup no longer matches the recorded original" % where
    return None


def recover_journal(paths, report_only=False):
    """Roll back an interrupted commit. A file changed by someone else since
    is never touched (the journal is kept and the conflict reported).
    Unreadable or untrustworthy journal metadata is refused with guidance and
    never deleted automatically."""
    raw = read_bytes(paths.journal)
    if raw is None:
        return None
    guidance = ("nothing was restored or deleted; inspect %s and the backups under %s, restore any file by hand if "
                "needed, then remove the journal" % (paths.journal, paths.backups))
    try:
        journal = parse_json_bytes(raw, paths.journal)
    except RefreshError as ex:
        raise RefreshError("interrupted-apply journal is unreadable (%s); %s" % (ex, guidance), EXIT_REFUSED)
    problem = _journal_problem(paths, journal)
    if problem:
        raise RefreshError("interrupted-apply journal cannot be trusted (%s); %s" % (problem, guidance),
                           EXIT_REFUSED)
    entries = journal["entries"]
    if report_only:
        return "an interrupted apply was detected (%s); it is rolled back automatically by the next p/apply" \
            % paths.journal
    conflicts = []
    for entry in reversed(entries):
        current = file_sha256(entry["path"])
        if current == entry.get("old_sha256"):
            continue
        if current == entry.get("new_sha256"):
            _restore_entry(entry)
        else:
            conflicts.append(entry["path"])
    if conflicts:
        raise RefreshError("an interrupted apply could not be rolled back because these files changed since: %s. "
                           "Backups: %s. Resolve manually, then remove %s"
                           % (", ".join(conflicts), os.path.dirname(entries[0]["backup"] or paths.backups),
                              paths.journal), EXIT_REFUSED)
    os.unlink(paths.journal)
    return "rolled back an interrupted apply (journal %s)" % paths.journal


def _prune_backups(paths):
    try:
        sets = sorted(d for d in os.listdir(paths.backups) if os.path.isdir(os.path.join(paths.backups, d)))
    except OSError:
        return
    for name in sets[:-MAX_BACKUP_SETS]:
        shutil.rmtree(os.path.join(paths.backups, name), ignore_errors=True)


def commit(paths, writes, now):
    """writes: [(path, new_bytes_or_None_to_delete, mode, expected_old_sha)].
    Journaled multi-file commit with backups, a per-file concurrent-edit
    check right before each replace, and rollback on any failure."""
    txn = "%s-%s" % (datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ"), secrets.token_hex(4))
    backup_dir = os.path.join(paths.backups, txn)
    ensure_private_dir(paths.backups)
    os.makedirs(backup_dir, mode=0o700)
    entries = []
    for i, (path, data, mode, expected) in enumerate(writes):
        if os.path.islink(path):
            raise RefreshError("refusing to write %s: it is a symlink (edit the link target yourself)" % path,
                               EXIT_REFUSED)
        current = file_sha256(path)
        if current != expected:
            raise RefreshError("%s changed since the proposal was made; nothing was applied — run p again" % path,
                               EXIT_REFUSED)
        backup = None
        if os.path.lexists(path):
            backup = os.path.join(backup_dir, "%02d-%s.bak" % (i, os.path.basename(path)))
            shutil.copy2(path, backup)
            mode = stat.S_IMODE(os.stat(path).st_mode)
        entries.append({"path": path, "backup": backup, "mode": mode, "old_sha256": current,
                        "new_sha256": sha256_bytes(data)})
    atomic_write_bytes(paths.journal, json_bytes({"schema_version": SCHEMA_VERSION, "kind": JOURNAL_KIND,
                                                  "txn": txn, "created_at": now_iso(now), "entries": entries}))
    done = []
    with deferred_signals():
        try:
            for entry, (path, data, _mode, _expected) in zip(entries, writes):
                if file_sha256(path) != entry["old_sha256"]:
                    raise RefreshError("%s changed during apply; rolled back — run p again" % path, EXIT_REFUSED)
                parent = os.path.dirname(path)
                if data is None:
                    if os.path.lexists(path):
                        os.unlink(path)
                else:
                    os.makedirs(parent, exist_ok=True)
                    atomic_write_bytes(path, data, entry["mode"])
                done.append(entry)
        except BaseException as ex:
            failed = []
            for entry in reversed(done):
                try:
                    _restore_entry(entry)
                except OSError:
                    failed.append(entry["path"])
            if failed:
                raise RefreshError("apply failed (%s) and rollback failed for %s; backups in %s; journal kept at %s"
                                   % (ex, ", ".join(failed), backup_dir, paths.journal))
            os.unlink(paths.journal)
            if isinstance(ex, RefreshError):
                raise
            raise RefreshError("apply failed and was rolled back: %s: %s" % (type(ex).__name__, ex))
        os.unlink(paths.journal)
    _prune_backups(paths)
    return txn


# -------------------------------------------------------------- commands ---

def _check_instruction_target(ctx, desired):
    """The generated instruction may only be created/replaced/removed when
    it is ours; a symlinked directory/file (e.g. a dotfiles checkout) is never
    written through."""
    paths, state = ctx["paths"], ctx["state"]
    current = file_sha256(paths.instructions)
    if sha256_bytes(desired) == current:
        return current, False
    if os.path.islink(paths.instructions_dir) or os.path.islink(paths.instructions):
        raise RefreshError("refusing to write through symlink %s; the generated routing instruction must live in "
                           "a real directory" % (paths.instructions if os.path.islink(paths.instructions)
                                                 else paths.instructions_dir), EXIT_REFUSED)
    if current is not None and current != state.get("instructions_sha256"):
        raise RefreshError("%s exists but was not written by this tool (or was edited); move it aside, then run p "
                           "again" % paths.instructions, EXIT_REFUSED)
    return current, True


def compute_writes(ctx, new_overrides, plan, action, proposal_id, snapshot_id=None):
    paths = ctx["paths"]
    writes = []
    if plan["settings_changed"] is None:
        raise RefreshError("settings.json cannot be read safely (%s); nothing was applied" % ctx["settings_error"],
                           EXIT_REFUSED)
    if plan["settings_changed"]:
        writes.append((paths.settings, json_bytes(plan["settings_data"]), 0o600, ctx["settings_sha"]))
    instr_current, instr_changed = _check_instruction_target(ctx, plan["instructions"])
    if instr_changed:
        writes.append((paths.instructions, plan["instructions"], 0o644, instr_current))
    eff_current = file_sha256(paths.effective)
    if eff_current != sha256_bytes(plan["effective_doc"]):
        writes.append((paths.effective, plan["effective_doc"], 0o600, eff_current))
    state = copy.deepcopy(ctx["state"])

    def values_of(ov):
        return {f: ov[f] for f in FIELDS} if ov else None
    changed_keys = sorted(k for k in ROUTE_BY_KEY
                          if values_of(state["overrides"].get(k)) != values_of(new_overrides.get(k)))
    stamped = {}
    for key, values in new_overrides.items():
        prev = state["overrides"].get(key)
        if prev and values_of(prev) == values_of(values):
            stamped[key] = prev
        else:
            stamped[key] = dict(values_of(values), applied_at=now_iso(ctx["now"]), snapshot_id=snapshot_id)
    state["overrides"] = stamped
    state["managed_settings"] = plan["managed_settings"]
    state["instructions_sha256"] = sha256_bytes(plan["instructions"])
    state["updated_at"] = now_iso(ctx["now"])
    state["history"] = (state.get("history", []) + [{"at": now_iso(ctx["now"]), "action": action,
                                                     "proposal_id": proposal_id, "routes": changed_keys}])[-MAX_HISTORY:]
    writes.append((paths.state, json_bytes(state), 0o600, ctx["state_sha"]))
    return writes, changed_keys


def _sync_needed(ctx, plan):
    paths = ctx["paths"]
    return bool(plan["settings_changed"]) or (
        file_sha256(paths.instructions) != sha256_bytes(plan["instructions"])) or (
        file_sha256(paths.effective) != sha256_bytes(plan["effective_doc"])) or (
        plan["managed_settings"] != ctx["state"].get("managed_settings", {}))


def preflight(ctx, plan):
    """Problems that would make an apply refuse, reported BEFORE asking for
    confirmation (None = none found). The commit re-checks everything."""
    if plan["settings_changed"] is None:
        return "settings.json cannot be read safely (%s); fix it first" % ctx["settings_error"]
    if plan["settings_changed"] and os.path.islink(ctx["paths"].settings):
        return ("%s is a symlink; this tool never writes through it — set the values with /subagents instead"
                % ctx["paths"].settings)
    try:
        _check_instruction_target(ctx, plan["instructions"])
    except RefreshError as ex:
        return str(ex)
    return None


def live_routing_lines(paths):
    """Live per-route task-call parameters for the orchestrator (read-only;
    no network, no pricing, no catalog). Reads the shipped defaults, the
    refresh state, user-config pins and the CURRENT settings.json on every
    call, so a /subagents edit is honored immediately: any field a human set
    there prints as OMIT. Refuses (RefreshError) rather than printing a
    guessed table when an input cannot be trusted."""
    raw = read_bytes(paths.journal)
    if raw is not None:
        raise RefreshError("an interrupted apply is pending (%s); routing state is not trustworthy until the next "
                           "copilot-s p / apply recovers it" % paths.journal, EXIT_REFUSED)
    defaults, _path, _sha = load_defaults(paths)
    user_config, _usha = load_user_config(paths)
    state, _ssha = read_state(paths)
    settings, _sha2, settings_error = read_settings(paths)
    if settings_error:
        raise RefreshError("live /subagents values cannot be read (%s)" % settings_error, EXIT_REFUSED)
    native_pins, warnings = settings_pins(settings, state)
    effective = resolve_effective(defaults, state["overrides"],
                                  {k: dict(v) for k, v in user_config.get("pins", {}).items()}, native_pins)
    lines = ["# Live toolkit routing (copilot-model-refresh.py status --instructions; read-only). Per route, pass",
             "# model / reasoning_effort / context_tier as shown. OMIT = leave that parameter out of the task call",
             "# (a live /subagents value applies); never pass the word OMIT or inherit."]
    lines += ["# warning: %s" % w for w in warnings]
    lines += [route_param_line(r["key"], effective[r["key"]]) for r in ROUTES]
    return lines


def cmd_status(paths, args, now):
    if getattr(args, "instructions", False):
        print("\n".join(live_routing_lines(paths)))
        return EXIT_OK
    note = recover_journal(paths, report_only=True)
    ctx = build_context(paths, now, refresh=None)
    out = render_header(ctx)
    if note:
        out.append("  Warning:      %s" % note)
    if ctx["settings_error"]:
        # Without readable /subagents values any table would be a guess.
        out.append("")
        out.append("Effective routing UNAVAILABLE: %s. Nothing was changed; fix or restore settings.json first."
                   % ctx["settings_error"])
        print("\n".join(out))
        return EXIT_REFUSED
    stored = None
    raw = read_bytes(paths.effective)
    if raw is not None:
        with contextlib.suppress(RefreshError):
            doc = parse_json_bytes(raw, paths.effective)
            stored = doc.get("routes") if isinstance(doc, dict) else None
    current_doc = build_effective_doc(ctx["effective"])["routes"]
    if stored is not None and stored != current_doc:
        out.append("  Drift:        your pins/overrides changed since the generated routing was written — run p to "
                   "re-sync")
    elif stored is None and generated_needed(ctx["effective"]):
        out.append("  Drift:        overrides/user-config pins exist but no generated routing exists — run p")
    out.append("  State dir:    %s" % paths.state_dir)
    out += render_final(ctx["effective"], ctx["defaults"], set(), "Current effective routing:")
    print("\n".join(out))
    return EXIT_OK


def _evaluate_and_render(ctx):
    rows = evaluate(ctx)
    alternatives = number_alternatives(rows)
    unassessed, new_models, priced = unassessed_and_new(ctx)
    new_overrides = proposed_overrides(rows, ctx["state"], ctx["defaults"])
    plan = plan_outputs(ctx, new_overrides)
    lines = render_proposal(ctx, rows, unassessed, new_models)
    return rows, new_overrides, plan, lines, priced, alternatives


def _input_digests(ctx):
    paths = ctx["paths"]
    return {"settings": ctx["settings_sha"], "state": ctx["state_sha"], "user_config": ctx["user_sha"],
            "routing": ctx["routing_sha"], "catalog": ctx["catalog_digest"],
            "instructions": file_sha256(paths.instructions), "effective": file_sha256(paths.effective),
            "verified": ctx["verified_sha"], "capabilities": ctx["capabilities"]["digest"],
            "pricing": pricing_digest(ctx["snapshot"])}


def _expected_outputs(ctx, plan):
    return {"instructions": sha256_bytes(plan["instructions"]), "effective": sha256_bytes(plan["effective_doc"]),
            "settings": sha256_bytes(json_bytes(plan["settings_data"])) if plan["settings_changed"]
            else ctx["settings_sha"]}


def _frozen_alternatives(alternatives):
    return [{"n": a["n"], "route": a["route"], "values": dict(a["values"])} for a in alternatives]


def _read_private_key(path):
    """(key_or_None, problem_or_None). Opened with O_NOFOLLOW and checked on
    the SAME descriptor (no check/use race): must be a regular file owned by
    this user, not group/world accessible, and hold exactly 64 hex chars."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, None
    except OSError as ex:
        return None, "cannot open it safely (%s)" % type(ex).__name__
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "it is not a regular file"
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return None, "it is not owned by you"
        if stat.S_IMODE(st.st_mode) & 0o077:
            return None, "it is readable or writable by other users (mode %o)" % stat.S_IMODE(st.st_mode)
        raw = os.read(fd, 257)
    finally:
        os.close(fd)
    key = raw.strip()
    if len(raw) > 256 or not re.match(rb"^[0-9a-f]{64}$", key):
        return None, "its content is not a valid key"
    return key, None


def _proposal_key(paths, create=False):
    """Private random key (0600, in the state dir) used to seal proposals,
    so the proposal's own editable fields (creation time, expiry,
    overrides, selection, digests) are never trusted on their own.
    create=False (select/apply): a missing or unsafe key verifies nothing
    (None). create=True (propose): an unsafe or invalid key is never reused —
    it is replaced atomically by a new private key (which also invalidates
    any proposal sealed with the exposed one)."""
    if os.path.islink(paths.proposal_key):
        raise RefreshError("refusing to use %s: it is a symlink" % paths.proposal_key, EXIT_REFUSED)
    key, problem = _read_private_key(paths.proposal_key)
    if key is not None:
        return key
    if not create:
        if problem:
            print("Warning: proposal key %s is not trusted (%s); the pending proposal cannot be verified."
                  % (paths.proposal_key, problem), file=sys.stderr)
        return None
    key = secrets.token_hex(32).encode("ascii")
    ensure_private_dir(paths.state_dir)
    atomic_write_bytes(paths.proposal_key, key + b"\n", 0o600)
    return key


def _proposal_seal(key, doc):
    body = {k: v for k, v in doc.items() if k != "seal"}
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def _check_proposal_lifetime_and_seal(paths, doc, now):
    """A proposal is valid only from its real creation time for exactly
    PROPOSAL_TTL_SECONDS: never future-dated, never extended, and only when
    its seal verifies (any edit of the file is refused)."""
    def refuse(message):
        with contextlib.suppress(OSError):
            os.unlink(paths.proposal)
        raise RefreshError(message + "; nothing was applied — run p again", EXIT_REFUSED)
    created, expires = doc.get("created_epoch"), doc.get("expires_epoch")
    if not isinstance(created, (int, float)) or isinstance(created, bool) or created != created:
        refuse("refused: the proposal has no valid creation time")
    if created > now:
        refuse("refused: the proposal is dated in the future (altered file or clock change)")
    if abs(expires - (created + PROPOSAL_TTL_SECONDS)) > 1e-6:
        refuse("refused: the proposal's %d-minute lifetime was altered" % (PROPOSAL_TTL_SECONDS // 60))
    key = _proposal_key(paths)
    seal = doc.get("seal")
    if key is None or not isinstance(seal, str) or not hmac.compare_digest(seal, _proposal_seal(key, doc)):
        refuse("refused: the pending proposal was altered (or its seal cannot be verified)")


def _freeze_proposal(ctx, rows, alternatives, selected, new_overrides, plan, route_changes, created_epoch,
                     expires_epoch):
    proposal_id = secrets.token_hex(8)
    doc = {
        "schema_version": SCHEMA_VERSION, "kind": PROPOSAL_KIND, "format": PROPOSAL_FORMAT,
        "proposal_id": proposal_id, "created_at": now_iso(created_epoch), "created_epoch": created_epoch,
        "expires_epoch": expires_epoch, "overrides": new_overrides, "route_changes": route_changes,
        "digests": _input_digests(ctx), "expected": _expected_outputs(ctx, plan),
        "snapshot_id": ctx["snapshot"]["snapshot_id"], "evidence_digest": evidence_digest(ctx, rows),
        "alternatives": _frozen_alternatives(alternatives), "selected": sorted(selected),
    }
    doc["seal"] = _proposal_seal(_proposal_key(ctx["paths"], create=True), doc)
    atomic_write_bytes(ctx["paths"].proposal, json_bytes(doc))
    return proposal_id


def _print_machine_lines(alternatives, verify_needed, proposal_id):
    for alt in alternatives:
        print("ALTERNATIVE\t%d\t%s\t%s" % (alt["n"], alt["route"], alt["values"]["model"]))
    if verify_needed:
        print("VERIFY\tneeded")
    if proposal_id:
        print("PROPOSAL\t%s" % proposal_id)


def cmd_preview(paths, args, now, freeze=False):
    with refresh_lock(paths):
        note = recover_journal(paths)
        if note:
            print("Note: %s." % note)
        ctx = build_context(paths, now, refresh=True, offline=args.offline)
        if ctx["snapshot"] is None:
            print("\n".join(render_header(ctx)))
            print("\n".join(render_final(ctx["effective"], ctx["defaults"], set(),
                                         "Routing unchanged (no pricing available):")))
            print("Error: no valid official pricing snapshot is available; routing was left unchanged.",
                  file=sys.stderr)
            return EXIT_ERROR
        rows, new_overrides, plan, lines, priced, alternatives = _evaluate_and_render(ctx)
        print("\n".join(lines))
        remember_seen(paths, priced, ctx["snapshot"]["snapshot_id"], now)
        verify_needed = bool(render_action_required(ctx, rows))
        route_changes = [r["key"] for r in rows if r["proposed"] is not None]
        sync = plan["settings_changed"] is not None and _sync_needed(ctx, plan)
        if not route_changes and not sync:
            print("\n".join(render_final(ctx["effective"], ctx["defaults"], set(),
                                         "No routing changes proposed — final effective routing (kept):")))
            if freeze and alternatives and preflight(ctx, plan) is None:
                # Nothing is proposed by default; the frozen (no-op) proposal
                # only lets you add an optional alternative explicitly.
                print("\nNo cost-saving change is proposed. %d optional alternative(s) are listed above; nothing "
                      "changes unless you select one and then confirm." % len(alternatives))
                proposal_id = _freeze_proposal(ctx, rows, alternatives, [], new_overrides, plan, [], now,
                                               now + PROPOSAL_TTL_SECONDS)
                _print_machine_lines(alternatives, verify_needed, proposal_id)
                return EXIT_NO_CHANGES
            if freeze:
                with contextlib.suppress(OSError):
                    os.unlink(paths.proposal)
                _print_machine_lines([], verify_needed, None)
            return EXIT_NO_CHANGES
        if not route_changes and sync:
            print("\nNo model changes, but the generated routing files are out of sync with your pins/overrides; "
                  "applying re-syncs them.")
        print("\n".join(render_final(plan["effective"], ctx["defaults"], set(route_changes),
                                     "Effective routing IF you apply (categorical fit only — not measured or "
                                     "equal quality; optional alternatives NOT included unless you select them):")))
        problem = preflight(ctx, plan)
        if problem:
            with contextlib.suppress(OSError):
                os.unlink(paths.proposal)
            print("\nThese changes cannot be applied: %s." % problem)
            print("\n".join(render_final(ctx["effective"], ctx["defaults"], set(),
                                         "Final effective routing (kept, nothing changed):")))
            return EXIT_REFUSED
        if not freeze:
            return EXIT_OK
        proposal_id = _freeze_proposal(ctx, rows, alternatives, [], new_overrides, plan, route_changes, now,
                                       now + PROPOSAL_TTL_SECONDS)
        _print_machine_lines(alternatives, verify_needed, proposal_id)
        return EXIT_OK


def _load_proposal(paths, proposal_id, now):
    if not PROPOSAL_ID_RE.match(proposal_id or ""):
        raise RefreshError("invalid proposal id", EXIT_REFUSED)
    raw = read_bytes(paths.proposal)
    if raw is None:
        raise RefreshError("no pending proposal (it was applied, discarded or never made); run p again", EXIT_REFUSED)
    doc = parse_json_bytes(raw, paths.proposal)
    if not isinstance(doc, dict) or doc.get("kind") != PROPOSAL_KIND or doc.get("proposal_id") != proposal_id:
        raise RefreshError("the pending proposal does not match %s; run p again" % proposal_id, EXIT_REFUSED)
    expires = doc.get("expires_epoch")
    if not isinstance(expires, (int, float)) or isinstance(expires, bool) or now > expires:
        raise RefreshError("the proposal expired (older than %d min); run p again" % (PROPOSAL_TTL_SECONDS // 60),
                           EXIT_REFUSED)
    if doc.get("format") != PROPOSAL_FORMAT or not isinstance(doc.get("evidence_digest"), str):
        with contextlib.suppress(OSError):
            os.unlink(paths.proposal)
        raise RefreshError("the pending proposal was made by an older version of this tool; nothing was applied — "
                           "run p again", EXIT_REFUSED)
    _check_proposal_lifetime_and_seal(paths, doc, now)
    overrides = doc.get("overrides")
    if (not isinstance(overrides, dict) or not isinstance(doc.get("digests"), dict)
            or not isinstance(doc.get("expected"), dict)):
        raise RefreshError("malformed proposal", EXIT_REFUSED)
    for key, values in overrides.items():
        if key not in ROUTE_BY_KEY:
            raise RefreshError("malformed proposal (route %r)" % key, EXIT_REFUSED)
        validate_route_values(values, "proposal override %s" % key)
    alts, selected = doc.get("alternatives"), doc.get("selected")
    if (not isinstance(alts, list) or not isinstance(selected, list)
            or not all(isinstance(n, int) and not isinstance(n, bool) for n in selected)
            or len(set(selected)) != len(selected)
            or not set(selected) <= {a.get("n") for a in alts if isinstance(a, dict)}):
        raise RefreshError("malformed proposal (alternatives)", EXIT_REFUSED)
    return doc


def _verify_proposal_evidence(paths, doc, ctx):
    """Re-derive the proposal from the current cache-only inputs. Refuses
    (and drops the proposal) if any input file, the pricing snapshot, the
    capability evidence, your availability records, today's date or any
    resulting decision/alternative differs from what was shown."""
    def refuse(message):
        with contextlib.suppress(OSError):
            os.unlink(paths.proposal)
        raise RefreshError(message, EXIT_REFUSED)
    current = _input_digests(ctx)
    stale = sorted(k for k, v in current.items() if doc["digests"].get(k) != v)
    if stale:
        refuse("refused: %s changed since the proposal was shown; nothing was applied — run p again"
               % ", ".join(stale))
    if ctx["snapshot"] is None:
        refuse("refused: the official pricing snapshot is no longer available; nothing was applied — run p again")
    rows = evaluate(ctx)
    alternatives = number_alternatives(rows)
    if evidence_digest(ctx, rows) != doc["evidence_digest"]:
        refuse("refused: the evidence behind the proposal (prices, official capability evidence or its freshness, "
               "availability, date or a resulting decision) changed since it was shown, or the proposal was "
               "altered; nothing was applied — run p again")
    if _frozen_alternatives(alternatives) != doc["alternatives"]:
        refuse("refused: the proposal's optional alternatives do not match a re-derivation (altered?); nothing was "
               "applied — run p again")
    return rows, alternatives


def _parse_choice(text):
    text = (text or "").replace(" ", "")
    if not re.match(r"^[0-9]{1,3}(,[0-9]{1,3}){0,49}$", text):
        raise RefreshError("invalid selection %r (use numbers like 1 or 1,3)" % _safe_text(text, 40), EXIT_REFUSED)
    choice = [int(n) for n in text.split(",")]
    if len(set(choice)) != len(choice) or 0 in choice:
        raise RefreshError("invalid selection (duplicate or zero numbers)", EXIT_REFUSED)
    return sorted(choice)


def cmd_select(paths, args, now):
    """Explicitly add optional alternatives to a frozen proposal. Writes a
    NEW frozen proposal (same expiry, new id) — never any routing file."""
    choice = _parse_choice(args.choose)
    with refresh_lock(paths):
        note = recover_journal(paths)
        if note:
            print("Note: %s." % note)
        doc = _load_proposal(paths, args.proposal_id, now)
        ctx = build_context(paths, now, refresh=None)
        rows, alternatives = _verify_proposal_evidence(paths, doc, ctx)
        by_n = {a["n"]: a for a in alternatives}
        missing = [str(n) for n in choice if n not in by_n]
        if missing:
            raise RefreshError("no optional alternative #%s in this proposal; nothing was selected"
                               % ", #".join(missing), EXIT_REFUSED)
        selected = [by_n[n] for n in choice]
        routes = [a["route"] for a in selected]
        if len(set(routes)) != len(routes):
            raise RefreshError("choose at most one alternative per route; nothing was selected", EXIT_REFUSED)
        new_overrides = derive_overrides(rows, ctx["state"], ctx["defaults"], selected)
        plan = plan_outputs(ctx, new_overrides)
        problem = preflight(ctx, plan)
        if problem:
            with contextlib.suppress(OSError):
                os.unlink(paths.proposal)
            raise RefreshError("these changes cannot be applied: %s; nothing was selected" % problem, EXIT_REFUSED)
        changed = {r["key"] for r in rows if r["proposed"] is not None} | set(routes)
        route_changes = [r["key"] for r in ROUTES if r["key"] in changed]
        proposal_id = _freeze_proposal(ctx, rows, alternatives, choice, new_overrides, plan, route_changes,
                                       doc["created_epoch"], doc["expires_epoch"])
        out = ["", "You selected these OPTIONAL alternatives (your explicit choice — not a recommendation, not a "
                   "measured, equal or better quality claim):"]
        more = False
        for alt in selected:
            row = next(r for r in rows if r["key"] == alt["route"])
            more = more or alt["cost"] > alt["current_cost"]
            out.append("  #%d %s: %s -> %s · blended $/1M %s -> %s (%s)%s" % (
                alt["n"], row["label"], _route_text(row["current"]), _route_text(alt["values"]),
                _money(alt["current_cost"]), _money(alt["cost"]), _delta_text(alt["cost"], alt["current_cost"]),
                " — replaces this route's proposed cost-saving change" if alt["replaces_proposal"] else ""))
        if more:
            out.append("  At least one selection costs MORE than the current route under the blended assumption "
                       "(not your bill).")
        out += render_final(plan["effective"], ctx["defaults"], set(route_changes),
                            "Effective routing IF you apply (includes your selected alternatives; categorical fit "
                            "only — not measured or equal quality):")
        print("\n".join(out))
        print("PROPOSAL\t%s" % proposal_id)
    return EXIT_OK


def cmd_apply(paths, args, now):
    with refresh_lock(paths):
        note = recover_journal(paths)
        if note:
            print("Note: %s." % note)
        doc = _load_proposal(paths, args.proposal_id, now)
        ctx = build_context(paths, now, refresh=None)
        if any(isinstance(h, dict) and h.get("proposal_id") == doc["proposal_id"]
               for h in ctx["state"].get("history", [])):
            with contextlib.suppress(OSError):
                os.unlink(paths.proposal)
            raise RefreshError("refused: proposal %s was already applied (replay); nothing was applied — run p again"
                               % doc["proposal_id"], EXIT_REFUSED)
        rows, alternatives = _verify_proposal_evidence(paths, doc, ctx)
        by_n = {a["n"]: a for a in alternatives}
        selected = [by_n[n] for n in doc["selected"]]
        new_overrides = derive_overrides(rows, ctx["state"], ctx["defaults"], selected)
        if new_overrides != {k: dict(v) for k, v in doc["overrides"].items()}:
            with contextlib.suppress(OSError):
                os.unlink(paths.proposal)
            raise RefreshError("refused: the proposal's overrides do not match a re-derivation from the evidence "
                               "(altered?); nothing was applied — run p again", EXIT_REFUSED)
        plan = plan_outputs(ctx, new_overrides)
        if _expected_outputs(ctx, plan) != doc.get("expected", {}):
            raise RefreshError("refused: the recomputed result differs from the confirmed proposal; nothing was "
                               "applied — run p again", EXIT_REFUSED)
        writes, changed_keys = compute_writes(ctx, new_overrides, plan, "apply", doc["proposal_id"],
                                              snapshot_id=doc.get("snapshot_id"))
        commit(paths, writes, now)
        with contextlib.suppress(OSError):
            os.unlink(paths.proposal)
    changed = set(doc.get("route_changes", [])) | set(changed_keys)
    out = ["Applied. Backups of replaced files: %s" % paths.backups]
    for alt in selected:
        out.append("Included your selected optional alternative #%d: %s -> %s." % (
            alt["n"], alt["route"], _route_text(alt["values"])))
    if any(k in new_overrides for k in AGENT_NAMES) or plan["settings_changed"]:
        out.append("Native /subagents settings updated for agent default routes (values you set yourself were left "
                   "untouched).")
    if plan["instructions"] is not None:
        out.append("Generated routing instruction: %s (new Copilot sessions load it; verify with `copilot "
                   "instruction list`)." % paths.instructions)
    else:
        out.append("No managed override or user-config pin remains, so no generated routing instruction is needed "
                   "(any /subagents values you set stay live).")
    out += render_final(plan["effective"], ctx["defaults"], changed, "Final effective routing:")
    print("\n".join(out))
    return EXIT_OK


def cmd_discard(paths, args, now):
    with refresh_lock(paths):
        raw = read_bytes(paths.proposal)
        if raw is not None:
            with contextlib.suppress(RefreshError):
                doc = parse_json_bytes(raw, paths.proposal)
                if isinstance(doc, dict) and doc.get("proposal_id") == args.proposal_id:
                    os.unlink(paths.proposal)
        ctx = build_context(paths, now, refresh=None)
    print("\n".join(render_final(ctx["effective"], ctx["defaults"], set(),
                                 "Cancelled — nothing was changed. Final effective routing (kept):")))
    return EXIT_OK


def cmd_reset(paths, args, now):
    with refresh_lock(paths):
        note = recover_journal(paths)
        if note:
            print("Note: %s." % note)
        ctx = build_context(paths, now, refresh=None)
        plan = plan_outputs(ctx, {})
        if not ctx["state"]["overrides"] and not _sync_needed(ctx, plan):
            print("\n".join(render_final(ctx["effective"], ctx["defaults"], set(),
                                         "Nothing to reset — final effective routing:")))
            return EXIT_NO_CHANGES
        if not args.yes:
            print("Reset would remove all managed overrides (%s), only the /subagents values this tool wrote, and "
                  "re-sync the generated routing files. Your own pins are kept. Re-run with --yes."
                  % (", ".join(sorted(ctx["state"]["overrides"])) or "none"))
            print("\n".join(render_final(plan["effective"], ctx["defaults"], set(ctx["state"]["overrides"]),
                                         "Effective routing IF you reset:")))
            return EXIT_OK
        writes, changed_keys = compute_writes(ctx, {}, plan, "reset", None)
        commit(paths, writes, now)
        with contextlib.suppress(OSError):
            os.unlink(paths.proposal)
    print("\n".join(["Reset complete. Backups of replaced files: %s" % paths.backups]
                    + render_final(plan["effective"], ctx["defaults"], set(changed_keys), "Final effective routing:")))
    return EXIT_OK


def _ask(prompt):
    """One line from stdin (the terminal), or None on EOF."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    return None if not line else line.rstrip("\r\n")


def _parse_tokens(text):
    return [t for t in re.split(r"[\s,]+", (text or "").strip()) if t]


def cmd_record_verified(paths, args, now):
    """Interactive: record the model IDs and reasoning levels YOU saw in
    Copilot CLI's /model picker. Writes only model-refresh/verified-models.json
    and only after an exact `yes` (never user-config.json, never routing)."""
    if not sys.stdin.isatty():
        raise RefreshError("record-verified is interactive: run it from a terminal (copilot-s p, then v); nothing "
                           "was saved")
    ctx = build_context(paths, now, refresh=None)
    today = ctx["today"].isoformat()
    rows = evaluate(ctx) if ctx["snapshot"] else []
    needed = {m: set(levels) for m, levels in fixable_candidates(ctx, rows).items()}
    for route in ROUTES:
        values = ctx["effective"][route["key"]]["values"]
        if values["model"] != INHERIT and valid_model_id(values["model"]):
            levels = needed.setdefault(values["model"], set())
            if values["reasoning_effort"] in EFFORTS:
                levels.add(values["reasoning_effort"])
    prev = ctx["verified"]
    start_sha = ctx["verified_sha"]
    universe = set(ctx["universe"])
    print("\nRecord what YOU can verify in Copilot CLI (this tool cannot query your account; Copilot CLI documents")
    print("no programmatic model list). In another terminal start `copilot` and type /model:")
    print("  - note every model ID you can select (your plan and organization policies decide this);")
    print("  - highlight a model and use the left/right arrows to see the reasoning levels it offers.")
    print("Models that matter for your routes now (levels they need):")
    for model_id in sorted(needed):
        print("  %s (%s)" % (model_id, ", ".join(e for e in EFFORTS if e in needed[model_id]) or "no level"))
    cli = usable_section(ctx["capabilities"], "supported_models", "clients")
    if cli:
        listed = sorted(m for m, r in cli["models"].items() if r.get("copilot_cli_client") == "included")
        print("Official docs list as included for Copilot CLI (your account may differ): %s" % ", ".join(listed))
    if prev:
        print("Your existing record (verified %s%s): %s" % (
            prev["verified_on"], "" if verified_record_fresh(prev, ctx["today"]) else ", STALE",
            ", ".join(sorted(prev["models"])) or "none"))
    models = None
    for _attempt in range(3):
        answer = _ask("Model IDs you can select in /model (space or comma separated; Enter = cancel): ")
        if answer is None or not answer.strip():
            print("Cancelled — nothing was saved.")
            return EXIT_NO_CHANGES
        tokens = list(dict.fromkeys(t.lower() for t in _parse_tokens(answer)))
        bad = [t for t in tokens if not valid_model_id(t)]
        if not bad and len(tokens) <= 200:
            models = tokens
            break
        print("Not valid model IDs: %s (use the IDs shown by /model, e.g. gpt-6.1-terra)." % ", ".join(
            _safe_text(b, 40) for b in bad[:5]))
    if models is None:
        print("Cancelled — nothing was saved.")
        return EXIT_NO_CHANGES
    unknown = [m for m in models if m not in universe]
    if unknown:
        print("Note: not in the official docs or catalog — recorded, but never assessed or assigned: %s"
              % ", ".join(unknown))
    complete_answer = _ask("Is that EVERY model your /model picker lists? [y/N]: ")
    complete = (complete_answer or "").strip().lower() in ("y", "yes")
    records = {}
    for model_id in models:
        old = (prev or {}).get("models", {}).get(model_id, {})
        old_fresh = bool(old.get("reasoning_levels")) and 0 <= (
            ctx["today"] - parse_date(old["levels_verified_on"], "levels_verified_on")).days <= AVAILABILITY_MAX_AGE_DAYS
        rec = {}
        if model_id in needed:
            keep = ("keep %s (recorded %s)" % ("/".join(old["reasoning_levels"]), old["levels_verified_on"])
                    if old_fresh else "not checked")
            for _attempt in range(2):
                answer = _ask("Reasoning levels /model shows for %s (needed: %s; e.g. low medium high; Enter = %s): "
                              % (model_id, ", ".join(e for e in EFFORTS if e in needed[model_id]) or "none", keep))
                if answer is None or not answer.strip():
                    break
                levels = list(dict.fromkeys(t.lower() for t in _parse_tokens(answer)))
                if levels and all(level in EFFORTS for level in levels):
                    rec = {"reasoning_levels": [e for e in EFFORTS if e in levels], "levels_verified_on": today}
                    break
                print("Use only: %s." % ", ".join(EFFORTS))
        if not rec and old_fresh:
            # Carried over unchanged with its ORIGINAL date (never silently renewed).
            rec = {"reasoning_levels": list(old["reasoning_levels"]), "levels_verified_on": old["levels_verified_on"]}
        records[model_id] = rec
    doc = {"schema_version": SCHEMA_VERSION, "kind": VERIFIED_KIND, "verified_on": today,
           "recorded_at": now_iso(now),
           "source": "your attestation in copilot-s p from Copilot CLI's /model picker",
           "complete_list": complete, "models": records}
    validate_verified_record(doc)
    print("\nAbout to save (availability dated %s, valid %d days; used only for availability and reasoning-level "
          "checks):" % (today, AVAILABILITY_MAX_AGE_DAYS))
    print("  %d model(s), %s: %s" % (len(models), "COMPLETE list" if complete else "partial list",
                                     ", ".join(models)))
    for model_id, rec in records.items():
        if rec:
            print("  %s: levels %s (verified %s)" % (model_id, "/".join(rec["reasoning_levels"]),
                                                     rec["levels_verified_on"]))
    confirm = _ask("Type 'yes' to save this record to %s (anything else cancels): " % paths.verified)
    if confirm != "yes":
        print("Cancelled — nothing was saved.")
        return EXIT_NO_CHANGES
    with refresh_lock(paths):
        if os.path.islink(paths.verified):
            raise RefreshError("refusing to write through symlink %s; nothing was saved" % paths.verified, EXIT_REFUSED)
        if sha256_bytes(read_bytes(paths.verified)) != start_sha:
            raise RefreshError("%s changed while you were entering the record; nothing was saved — try again"
                               % paths.verified, EXIT_REFUSED)
        atomic_write_bytes(paths.verified, json_bytes(doc))
    print("Saved %s. Your routing is unchanged until you confirm a proposal." % paths.verified)
    return EXIT_OK


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(
        prog="copilot-model-refresh.py",
        description="Review subagent routing against current official GitHub Copilot prices and official model "
                    "capability docs, list every model in view, and apply confirmed local overrides (copilot-s menu "
                    "p). Shipped defaults are never modified.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status", help="Show the current effective routing (cache only, no network, no writes)")
    p.add_argument("--instructions", action="store_true",
                   help="Print only the live per-route task-call parameters for the orchestrator (OMIT = leave the "
                        "parameter out; no pricing/catalog needed)")
    p.set_defaults(func=cmd_status)
    for name, freeze in (("preview", False), ("propose", True)):
        p = sub.add_parser(name, help=("Fetch current official prices and capability docs and show proposed "
                                       "changes, optional alternatives and all models"
                                       + (" (no routing writes)" if not freeze else
                                          "; freeze the proposal for apply and print 'PROPOSAL<TAB>id'")))
        p.add_argument("--offline", action="store_true",
                       help="Use the cached pricing snapshot and capability evidence only (no network)")
        p.set_defaults(func=(lambda f: (lambda pa, a, n: cmd_preview(pa, a, n, freeze=f)))(freeze))
    p = sub.add_parser("apply", help="Apply a frozen proposal (refuses if anything changed since)")
    p.add_argument("--proposal-id", required=True)
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser("select", help="Add explicitly chosen optional alternatives to a frozen proposal (writes a "
                                      "new frozen proposal; no routing writes; prints 'PROPOSAL<TAB>id')")
    p.add_argument("--proposal-id", required=True)
    p.add_argument("--choose", required=True, help="Alternative numbers shown by propose, e.g. 1 or 1,3")
    p.set_defaults(func=cmd_select)
    p = sub.add_parser("record-verified", help="Interactively record the models/reasoning levels you verified in "
                                               "Copilot CLI's /model picker (saved only after an exact 'yes')")
    p.set_defaults(func=cmd_record_verified)
    p = sub.add_parser("discard", help="Discard a frozen proposal and show the unchanged routing")
    p.add_argument("--proposal-id", required=True)
    p.set_defaults(func=cmd_discard)
    p = sub.add_parser("reset", help="Remove all managed overrides (keeps your pins); requires --yes")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_reset)
    args = parser.parse_args(argv)
    now = time.time() if now is None else now
    paths = Paths()
    try:
        return args.func(paths, args, now)
    except RefreshError as ex:
        print("copilot-model-refresh: %s" % ex, file=sys.stderr)
        return ex.code
    except KeyboardInterrupt:
        # Outside the (signal-deferred) commit nothing is half-written.
        print("\nInterrupted — subagent routing left unchanged.", file=sys.stderr)
        return 130
    except Exception as ex:  # noqa: BLE001 - a menu helper must never crash the caller's shell
        print("copilot-model-refresh error: %s: %s (routing left unchanged)" % (type(ex).__name__, ex),
              file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
