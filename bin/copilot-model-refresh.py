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
- Evaluates the ten effective routes (seven custom agents, the small/casual
  coder route, the stronger reviewer override and the built-in `task`
  route) against a dated, curated catalog of OFFICIAL categorical labels
  (config/model-role-catalog.json, optionally extended by a dated local
  overlay). Nothing is inferred from price or newness; models missing from
  the catalog are listed as unassessed and never assigned.
- Proposes a change only for an unpinned route whose replacement passes
  every catalog gate, is listed in the user's verified availability list
  (Copilot CLI offers no documented programmatic model catalog to verify
  availability), has the route's SPECIFIC reasoning level verified for that
  model (source-backed catalog `reasoning_levels` or the user's dated
  `verified_capabilities`; the generic official "configurable reasoning"
  flag is never taken as proof of a level — unknown fails closed), and is
  at least 10% cheaper under a disclosed blended workload assumption on the
  Default-tier rates and, for long_context routes, also on the rates billed
  above the long-context threshold. Fit is categorical, never a measured or
  equal-quality claim. Shipped routing defaults (config/agent-routing.yaml,
  agents/*.agent.md, instructions) are NEVER modified.
- `propose` freezes the proposal with digests of every input it read;
  `apply` refuses (changes nothing) if any of them changed since.

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
- Availability is only as good as the user's `available_models` list.
- The blended cost is an assumption-based comparison aid, not a forecast.

Commands: status [--instructions] | preview | propose | apply --proposal-id ID |
          discard --proposal-id ID | reset [--yes]
Exit codes: 0 ok (propose: changes pending), 2 error, 3 no changes,
            4 refused (stale proposal, concurrent edit, unsafe target),
            130 interrupted (nothing half-written).
User-maintained input (never written by this tool):
  <COPILOT_HOME>/model-refresh/user-config.json — available_models
  {verified_on, models}, verified_capabilities {verified_on, models:
  {model: {reasoning_levels}}}, pins {route: {model?, reasoning_effort?,
  context_tier?}}, optional dated local catalog overlay.
Stdlib only.
"""

import argparse
import contextlib
import copy
import hashlib
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


def availability(user_config, today):
    """(set_of_models or None, description, usable_bool)."""
    avail = user_config.get("available_models")
    if not avail:
        return None, ("no verified availability list (add available_models to %s/user-config.json)"
                      % STATE_DIRNAME), False
    verified = parse_date(avail["verified_on"], "available_models.verified_on")
    age = (today - verified).days
    models = set(avail["models"])
    if age < 0:
        return models, "availability list dated in the future (%s) — not used" % avail["verified_on"], False
    if age > AVAILABILITY_MAX_AGE_DAYS:
        return models, ("availability list verified %s is older than %d days — re-verify before replacements"
                        % (avail["verified_on"], AVAILABILITY_MAX_AGE_DAYS)), False
    return models, "verified %s (%d model(s))" % (avail["verified_on"], len(models)), True


def verified_reasoning_levels(model_id, ctx):
    """(levels_or_None, provenance). Source-backed catalog `reasoning_levels`
    (unexpired), else the user's own `verified_capabilities` record (<= 30
    days old). Never inferred from the generic configurable_reasoning flag:
    the official docs only say levels vary per model (the CLI shows them in
    its interactive /model picker; no programmatic per-model list is
    documented)."""
    entry = ctx["catalog"]["models"].get(model_id)
    if (entry and entry.get("reasoning_levels")
            and origin_unusable(entry["origin"], ctx["today"]) is None):
        return entry["reasoning_levels"], "%s catalog reviewed %s" % (
            entry["origin"]["name"], entry["origin"]["reviewed_on"].isoformat())
    caps = ctx["user_config"].get("verified_capabilities")
    if caps and model_id in caps["models"]:
        verified = parse_date(caps["verified_on"], "verified_capabilities.verified_on")
        age = (ctx["today"] - verified).days
        if 0 <= age <= AVAILABILITY_MAX_AGE_DAYS:
            return caps["models"][model_id]["reasoning_levels"], "your verified_capabilities (%s)" % caps["verified_on"]
        return None, "your verified_capabilities record (%s) is stale or future-dated" % caps["verified_on"]
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


# ------------------------------------------------------------ evaluation ---

def route_policy(key, defaults, catalog, today):
    """(policy_or_None, reason). Default policy: the shipped baseline model's
    own official task area and category (both from the catalog)."""
    baseline = defaults[key]["model"]
    base_entry = catalog["models"].get(baseline)
    override = catalog["routes"].get(key)
    if override is not None:
        problem = origin_unusable(override["origin"], today)
        if problem:
            return None, "route policy cannot be used: %s" % problem
        min_cat = override.get("min_category") or (base_entry or {}).get("category")
        if not min_cat:
            return None, "route policy has no min_category and the baseline is not catalogued"
        return {"areas": list(override["accepted_task_areas"]), "min_category": min_cat,
                "origin": override["origin"]["name"]}, None
    if base_entry is None:
        return None, "shipped baseline %s is not in the catalog (unassessed), so no replacement policy" % baseline
    problem = origin_unusable(base_entry["origin"], today)
    if problem:
        return None, "catalog entry for the baseline %s cannot be used: %s" % (baseline, problem)
    return {"areas": [base_entry["task_area"]], "min_category": base_entry["category"],
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
    entry = ctx["catalog"]["models"].get(model_id)
    if entry is None:
        return [(REASON_OTHER, "not in the vetted catalog (unassessed)")]
    problem = origin_unusable(entry["origin"], ctx["today"])
    if problem:
        no(problem)
    live = ctx["snapshot"]["models"].get(model_id) if ctx["snapshot"] else None
    if live is None:
        no("not in the current official pricing")
        return reasons
    live_cat = live.get("category")
    if not live_cat:
        no("official Category not available in this pricing snapshot")
    elif live_cat.lower() != entry["category"].lower():
        no("official Category changed (now %s, catalogued %s) — needs re-review" % (live_cat, entry["category"]))
    if CATEGORY_RANK[entry["category"].lower()] < CATEGORY_RANK[policy["min_category"].lower()]:
        no("Category %s is below %s" % (entry["category"], policy["min_category"]))
    if entry["task_area"] not in policy["areas"]:
        no("task area '%s' not accepted for this route" % entry["task_area"])
    if not entry["configurable_reasoning"]:
        no("no official configurable-reasoning support")
    else:
        required = values["reasoning_effort"]
        levels, provenance = verified_reasoning_levels(model_id, ctx)
        if levels is None:
            no("reasoning level '%s' not verified for this model (%s; generic configurable-reasoning support "
               "does not prove a specific level)" % (required, provenance), REASON_CAPABILITY)
        elif required not in levels:
            no("reasoning level '%s' not offered per %s (verified: %s)" % (required, provenance, ", ".join(levels)))
    if values["context_tier"] == "long_context" and not entry["one_million_context"]:
        no("no official 1M-context support (route uses long_context)")
    if entry["copilot_cli_client"] != "included":
        no("Copilot CLI support is %s in the official per-client table" % entry["copilot_cli_client"].replace("_", " "))
    avail_models, _desc, avail_ok = ctx["availability"]
    if not avail_ok:
        no("availability not verified", REASON_AVAILABILITY)
    elif model_id not in avail_models:
        no("not in your verified available_models", REASON_AVAILABILITY)
    return reasons


def evaluate(ctx):
    """Per-route decisions. Never proposes for pinned routes, without fresh
    pricing, or for candidates failing any catalog/availability gate."""
    defaults, eff, snapshot = ctx["defaults"], ctx["effective"], ctx["snapshot"]
    overrides = ctx["state"]["overrides"]
    rows = []
    for route in ROUTES:
        key = route["key"]
        cur = eff[key]
        values = cur["values"]
        row = {"key": key, "label": route["label"], "current": dict(values), "sources": dict(cur["sources"]),
               "proposed": None, "reason": "", "evidence": [], "blocked": []}
        rows.append(row)
        cur_cost = blended_cost(default_rates(snapshot, values["model"])) if values["model"] != INHERIT else None
        row["current_cost"] = cur_cost
        if is_pinned(cur):
            row["reason"] = "kept: pinned by you (%s)" % ", ".join(sorted(
                {SOURCE_LABELS[s] for s in cur["sources"].values() if s.startswith("pin:")}))
            continue
        if ctx["global_block"]:
            row["reason"] = "kept: " + ctx["global_block"]
            continue
        policy, why = route_policy(key, defaults, ctx["catalog"], ctx["today"])
        if policy is None:
            row["reason"] = "kept: " + why
            continue
        baseline = defaults[key]
        row["evidence"].append("policy: task area in %s; Category >= %s (%s)" % (
            " / ".join("'%s'" % a for a in policy["areas"]), policy["min_category"], policy["origin"]))
        eligible = {}
        current_reasons = []
        # Only KEEPING the shipped baseline needs no verification. Moving to
        # it from anything else (e.g. back from a managed override) is a
        # change like any other and must pass every gate below.
        baseline_is_current = values == baseline
        if baseline_is_current and default_rates(snapshot, baseline["model"]) is not None:
            eligible[baseline["model"]] = ("shipped baseline (current)", dict(baseline))
        for model_id in sorted(ctx["catalog"]["models"]):
            if model_id == baseline["model"] and baseline_is_current:
                continue
            cand_values = {"model": model_id, "reasoning_effort": baseline["reasoning_effort"],
                           "context_tier": baseline["context_tier"]}
            reasons = check_candidate(model_id, key, cand_values, policy, ctx)
            if model_id == values["model"]:
                current_reasons = reasons
            cost = blended_cost(default_rates(snapshot, model_id))
            if reasons:
                if cost is not None and (cur_cost is None or cost < cur_cost):
                    row["blocked"].append((cost, model_id, reasons))
                continue
            eligible[model_id] = ("shipped baseline, verified" if model_id == baseline["model"] else
                                  "catalogued %s" % ctx["catalog"]["models"][model_id]["origin"]["name"],
                                  cand_values)
        row["blocked"].sort()
        current_is_override = key in overrides and cur["sources"]["model"] == "managed"
        current_eligible = values["model"] in eligible
        if not eligible:
            if current_is_override:
                why = "; ".join(text for _code, text in current_reasons) or "it no longer passes every gate"
                row["reason"] = ("kept: current override %s cannot be re-verified right now (%s), and no verified "
                                 "alternative exists (the shipped baseline also needs every gate to return to it); "
                                 "kept unchanged" % (values["model"], why))
            else:
                row["reason"] = "kept: no priced, eligible model for this route"
            continue

        def rank(mid):
            return (blended_cost(default_rates(snapshot, mid)),
                    0 if mid == values["model"] else (1 if mid == baseline["model"] else 2), mid)
        ordered = sorted(eligible, key=rank)
        long_route = baseline["context_tier"] == "long_context"
        cur_long = blended_cost(long_tier_rates(snapshot, values["model"])) if cur_cost is not None else None

        def saving_ok(mid):
            # Must be >= MIN_SAVING cheaper on the Default-tier rates and, for
            # long_context routes, also on the rates billed above the
            # long-context threshold (the parser exposes both tiers).
            d_new = blended_cost(default_rates(snapshot, mid))
            if d_new > cur_cost * (1 - MIN_SAVING_FRACTION):
                return False
            if long_route:
                l_new = blended_cost(long_tier_rates(snapshot, mid))
                return l_new <= cur_long * (1 - MIN_SAVING_FRACTION)
            return True

        best = None
        if cur_cost is None:
            # Unknown price never justifies a change: an unpriced current
            # model (shipped baseline or managed override alike) is kept.
            row["reason"] = ("kept: current %s %s is not in the current official pricing; kept unchanged (no change "
                             "is proposed without a known current price%s)"
                             % ("override" if current_is_override else "model", values["model"],
                                " — use reset to return to the shipped default" if current_is_override else ""))
            continue
        else:
            for mid in ordered:
                if mid != values["model"] and saving_ok(mid):
                    best = mid
                    d_new = blended_cost(default_rates(snapshot, mid))
                    change_reason = "%.0f%% cheaper (blended, Default tier)" % (100.0 * (1 - d_new / cur_cost))
                    if long_route:
                        l_new = blended_cost(long_tier_rates(snapshot, mid))
                        change_reason += ", %.0f%% cheaper above the long-context threshold" % (
                            100.0 * (1 - l_new / cur_long))
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
            continue
        best_cost = blended_cost(default_rates(snapshot, best))
        best_values = eligible[best][1]
        row["proposed"] = dict(best_values)
        row["proposed_cost"] = best_cost
        row["reason"] = ("CHANGE: %s; %s — categorical fit only, NOT a measured-quality or equal-quality claim"
                         % (change_reason, eligible[best][0]))
        if long_route:
            row["evidence"].append("long-context tier blended $/1M (input above the threshold): %s -> %s" % (
                _money(cur_long), _money(blended_cost(long_tier_rates(snapshot, best)))))
        entry = ctx["catalog"]["models"].get(best)
        if entry and best != baseline["model"]:
            levels, provenance = verified_reasoning_levels(best, ctx)
            row["evidence"].append("%s: Category %s, task area '%s', 1M context %s, configurable reasoning %s, "
                                   "Copilot CLI %s (%s catalog reviewed %s)" % (
                                       best, entry["category"], entry["task_area"],
                                       "yes" if entry["one_million_context"] else "no",
                                       "yes" if entry["configurable_reasoning"] else "no",
                                       entry["copilot_cli_client"].replace("_", " "), entry["origin"]["name"],
                                       entry["origin"]["reviewed_on"].isoformat()))
            row["evidence"].append("reasoning level '%s' verified via %s; context '%s' kept from the route baseline"
                                   % (best_values["reasoning_effort"], provenance, best_values["context_tier"]))
    return rows


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
        "Route keys: `coder.small_casual` is the mandatory small/casual implementation delegation to `coder`;",
        "`reviewer.stronger` is the stronger reviewer override; `task` is the built-in Task tool route.",
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
    """Read every input. refresh=None: cache-only (status)."""
    defaults, routing_path, routing_sha = load_defaults(paths)
    user_config, user_sha = load_user_config(paths)
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
    if shipped_problem and not local_ok:
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
        "global_block": "; ".join(blocks), "availability": availability(user_config, today),
        "module": module,
    }
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
    out.append("  Catalog:      %s" % "; ".join(origins))
    out.append("  Availability: %s" % ctx["availability"][1])
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
        for vals in (row["current"], row["proposed"]):
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
        for cost, model_id, reasons in row["blocked"][:3]:
            out.append("      not eligible: %s (blended %s) — %s" % (
                model_id, _money(cost), "; ".join(text for _code, text in reasons)))
    out += render_action_required(ctx, rows)
    if unassessed:
        out.append("")
        out.append("Unassessed models in the official pricing (listed only, never assigned): %s"
                   % ", ".join(unassessed))
    if new_models:
        out.append("New in the official pricing since your last refresh: %s" % ", ".join(new_models))
    out.append("")
    out.append("* Blended $/1M = %s of the official Default-tier rates; cache write uses the input rate when no "
               "separate cache-write rate is listed. An assumption for comparison only, not a forecast. A change is "
               "proposed only when it is at least %d%% cheaper (for long_context routes also on the rates above the "
               "long-context threshold); an unpriced or unverifiable current model is always kept."
               % (", ".join("%d%% %s" % (round(s * 100), k.replace("_", " ")) for k, s in WORKLOAD_MIX),
                  round(MIN_SAVING_FRACTION * 100)))
    out.append("  Fit is categorical (official Category / Task area / capability labels from the dated catalog), "
               "not a quality score; shipped routes remain the baseline recommendation.")
    return out


def render_action_required(ctx, rows):
    """Explicit next step when replacements are blocked only by facts the
    user must verify (availability, specific reasoning level) — never a
    silent 'no changes' on a first run."""
    fixable = {}  # model -> set of required levels
    for row in rows:
        for _cost, model_id, reasons in row["blocked"]:
            codes = {code for code, _text in reasons}
            if codes and codes <= {REASON_AVAILABILITY, REASON_CAPABILITY}:
                levels = fixable.setdefault(model_id, set())
                if REASON_CAPABILITY in codes:
                    levels.add(ctx["defaults"][row["key"]]["reasoning_effort"])
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
    unassessed = [m for m in priced if m not in ctx["catalog"]["models"]]
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
    unassessed, new_models, priced = unassessed_and_new(ctx)
    new_overrides = proposed_overrides(rows, ctx["state"], ctx["defaults"])
    plan = plan_outputs(ctx, new_overrides)
    lines = render_proposal(ctx, rows, unassessed, new_models)
    return rows, new_overrides, plan, lines, priced


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
        rows, new_overrides, plan, lines, priced = _evaluate_and_render(ctx)
        print("\n".join(lines))
        remember_seen(paths, priced, ctx["snapshot"]["snapshot_id"], now)
        route_changes = [r["key"] for r in rows if r["proposed"] is not None]
        sync = plan["settings_changed"] is not None and _sync_needed(ctx, plan)
        if not route_changes and not sync:
            if freeze:
                with contextlib.suppress(OSError):
                    os.unlink(paths.proposal)
            print("\n".join(render_final(ctx["effective"], ctx["defaults"], set(),
                                         "No routing changes proposed — final effective routing (kept):")))
            return EXIT_NO_CHANGES
        if not route_changes and sync:
            print("\nNo model changes, but the generated routing files are out of sync with your pins/overrides; "
                  "applying re-syncs them.")
        print("\n".join(render_final(plan["effective"], ctx["defaults"], set(route_changes),
                                     "Effective routing IF you apply (categorical fit only — not measured or "
                                     "equal quality):")))
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
        proposal_id = secrets.token_hex(8)
        doc = {
            "schema_version": SCHEMA_VERSION, "kind": PROPOSAL_KIND, "proposal_id": proposal_id,
            "created_at": now_iso(now), "created_epoch": now, "expires_epoch": now + PROPOSAL_TTL_SECONDS,
            "overrides": new_overrides, "route_changes": route_changes,
            "digests": {"settings": ctx["settings_sha"], "state": ctx["state_sha"], "user_config": ctx["user_sha"],
                        "routing": ctx["routing_sha"], "catalog": ctx["catalog_digest"],
                        "instructions": file_sha256(paths.instructions), "effective": file_sha256(paths.effective)},
            "expected": {"instructions": sha256_bytes(plan["instructions"]),
                         "effective": sha256_bytes(plan["effective_doc"]),
                         "settings": sha256_bytes(json_bytes(plan["settings_data"]))
                         if plan["settings_changed"] else ctx["settings_sha"]},
            "snapshot_id": ctx["snapshot"]["snapshot_id"],
        }
        atomic_write_bytes(paths.proposal, json_bytes(doc))
        print("PROPOSAL\t%s" % proposal_id)
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
    overrides = doc.get("overrides")
    if (not isinstance(overrides, dict) or not isinstance(doc.get("digests"), dict)
            or not isinstance(doc.get("expected"), dict)):
        raise RefreshError("malformed proposal", EXIT_REFUSED)
    for key, values in overrides.items():
        if key not in ROUTE_BY_KEY:
            raise RefreshError("malformed proposal (route %r)" % key, EXIT_REFUSED)
        validate_route_values(values, "proposal override %s" % key)
    return doc


def cmd_apply(paths, args, now):
    with refresh_lock(paths):
        note = recover_journal(paths)
        if note:
            print("Note: %s." % note)
        doc = _load_proposal(paths, args.proposal_id, now)
        ctx = build_context(paths, now, refresh=None)
        current = {"settings": ctx["settings_sha"], "state": ctx["state_sha"], "user_config": ctx["user_sha"],
                   "routing": ctx["routing_sha"], "catalog": ctx["catalog_digest"],
                   "instructions": file_sha256(paths.instructions), "effective": file_sha256(paths.effective)}
        stale = sorted(k for k, v in current.items() if doc["digests"].get(k) != v)
        if stale:
            with contextlib.suppress(OSError):
                os.unlink(paths.proposal)
            raise RefreshError("refused: %s changed since the proposal was shown; nothing was applied — run p again"
                               % ", ".join(stale), EXIT_REFUSED)
        new_overrides = {k: dict(v) for k, v in doc["overrides"].items()}
        plan = plan_outputs(ctx, new_overrides)
        expected = doc.get("expected", {})
        got = {"instructions": sha256_bytes(plan["instructions"]), "effective": sha256_bytes(plan["effective_doc"]),
               "settings": sha256_bytes(json_bytes(plan["settings_data"])) if plan["settings_changed"]
               else ctx["settings_sha"]}
        if got != expected:
            raise RefreshError("refused: the recomputed result differs from the confirmed proposal; nothing was "
                               "applied — run p again", EXIT_REFUSED)
        writes, changed_keys = compute_writes(ctx, new_overrides, plan, "apply", doc["proposal_id"],
                                              snapshot_id=doc.get("snapshot_id"))
        commit(paths, writes, now)
        with contextlib.suppress(OSError):
            os.unlink(paths.proposal)
    changed = set(doc.get("route_changes", [])) | set(changed_keys)
    out = ["Applied. Backups of replaced files: %s" % paths.backups]
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


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(
        prog="copilot-model-refresh.py",
        description="Review subagent routing against current official GitHub Copilot prices and apply confirmed "
                    "local overrides (copilot-s menu p). Shipped defaults are never modified.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("status", help="Show the current effective routing (cache only, no network, no writes)")
    p.add_argument("--instructions", action="store_true",
                   help="Print only the live per-route task-call parameters for the orchestrator (OMIT = leave the "
                        "parameter out; no pricing/catalog needed)")
    p.set_defaults(func=cmd_status)
    for name, freeze in (("preview", False), ("propose", True)):
        p = sub.add_parser(name, help=("Fetch current official prices and show proposed changes"
                                       + (" (no routing writes)" if not freeze else
                                          "; freeze the proposal for apply and print 'PROPOSAL<TAB>id'")))
        p.add_argument("--offline", action="store_true", help="Use the cached pricing snapshot only")
        p.set_defaults(func=(lambda f: (lambda pa, a, n: cmd_preview(pa, a, n, freeze=f)))(freeze))
    p = sub.add_parser("apply", help="Apply a frozen proposal (refuses if anything changed since)")
    p.add_argument("--proposal-id", required=True)
    p.set_defaults(func=cmd_apply)
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
