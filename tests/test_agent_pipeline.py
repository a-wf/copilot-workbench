#!/usr/bin/env python3
"""
Regression tests for the 7-role agent pipeline redesign:

  1. Agent inventory/frontmatter expectations — exactly the 7 expected
     `agents/*.agent.md` files exist, each with the expected `name`/
     `model`/`tools` frontmatter, and the retired `fixer.agent.md` is
     gone. Also asserts every agent's `description` explicitly reinforces
     bounded scope / no-duplicated-work, per the pipeline's cost-control
     design.
  2. Retired `fixer` agent cleanup on install — a previously-installed,
     manifest-managed `~/.copilot/agents/fixer.agent.md` (or a symlink
     into this repo checkout) is removed and pruned from the install
     manifest by `scripts/install.sh`; an unmanaged file at that path is
     always left untouched. Mirrors the installer smoke test in
     .github/workflows/ci.yml as a reusable, individually-diagnosable
     unit test.

Self-contained: stdlib-only (unittest), no network, no writes outside
per-test temp directories. Test environments are hermetic: toolkit-related
env vars (COPILOT_HOME, COPILOT_TASK_REPORTS_DIR, etc.) are cleared/
overridden per test so no test can read or write real user data.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS_DIR = os.path.join(REPO_ROOT, "agents")
ROUTING_CONFIG = os.path.join(REPO_ROOT, "config", "agent-routing.yaml")
INSTALL_SH = os.path.join(REPO_ROOT, "scripts", "install.sh")

EXPECTED_AGENTS = {
    # This is this release's shipped, CI-validated *repository default* for
    # each agent — not a restriction on what any individual installation
    # may run. Personal overrides belong in `/subagents` inside a Copilot
    # CLI session and don't touch this repo's frontmatter at all. If you
    # intentionally change a shipped default in `agents/*.agent.md` source
    # (not a personal `/subagents` override), update this map to match, per
    # README.md's "Agent pipeline" section.
    "discovery": {
        "model": "gemini-3.8-flash",
        "reasoningEffort": "low",
        "tools": ["read", "search"],
    },
    "planner": {
        "model": "gpt-6-sol",
        "reasoningEffort": "high",
        "tools": ["read", "search", "web"],
    },
    "coder": {
        "model": "gpt-6-luna",
        "reasoningEffort": "max",
        "tools": ["*"],
    },
    "senior-coder": {
        "model": "claude-opus-5.5",
        "reasoningEffort": "high",
        "tools": ["*"],
    },
    "reviewer": {
        "model": "claude-opus-5.5",
        "reasoningEffort": "high",
        "tools": ["read", "search", "execute"],
    },
    "tester": {
        "model": "gpt-6-luna",
        "reasoningEffort": "max",
        "tools": ["*"],
    },
    "test-reviewer": {
        "model": "claude-opus-5.5",
        "reasoningEffort": "high",
        "tools": ["read", "search", "execute"],
    },
}

EXPECTED_ROUTING = {
    "discovery": {
        "role": "Codebase Mapping Specialist",
        "model": "gemini-3.8-flash",
        "reasoning_effort": "low",
        "context_tier": "long_context",
    },
    "planner": {
        "role": "Implementation Planner",
        "model": "gpt-6-sol",
        "reasoning_effort": "high",
        "context_tier": "long_context",
    },
    "coder": {
        "role": "Routine Implementation Engineer",
        "model": "gpt-6-luna",
        "reasoning_effort": "max",
        "context_tier": "default",
    },
    "senior-coder": {
        "role": "Senior Software Engineer / Architect",
        "model": "claude-opus-5.5",
        "reasoning_effort": "high",
        "context_tier": "long_context",
    },
    "reviewer": {
        "role": "Code Review Specialist",
        "model": "claude-opus-5.5",
        "reasoning_effort": "high",
        "context_tier": "long_context",
    },
    "tester": {
        "role": "Test Engineer",
        "model": "gpt-6-luna",
        "reasoning_effort": "max",
        "context_tier": "default",
    },
    "test-reviewer": {
        "role": "Test Coverage Reviewer",
        "model": "claude-opus-5.5",
        "reasoning_effort": "high",
        "context_tier": "long_context",
    },
}


def parse_frontmatter(path):
    """Parse the `key: value` YAML-ish frontmatter block of an agent file.

    Returns a dict with string values for scalar fields (name, description,
    model) and a list of strings for the `tools` field. Deliberately
    simple/regex-based to match this repo's frontmatter style, not a full
    YAML parser.
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if not match:
        raise AssertionError(f"{path}: no frontmatter block found")
    block = match.group(1)
    result = {}
    for line in block.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "tools":
            tools_match = re.findall(r'"([^"]*)"', value)
            result[key] = tools_match
        else:
            result[key] = value
    return result


def parse_agent_routing(path):
    """Parse config/agent-routing.yaml's intentionally-small schema.

    The project avoids a PyYAML dependency for tests. This parser is
    deliberately limited to the exact repository-owned format:
    agent-name -> role scalar + one-line copilot map.
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    result = {}
    current = None
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        agent_match = re.match(r"^  ([a-z][a-z-]*):$", line)
        if agent_match:
            current = agent_match.group(1)
            result[current] = {}
            continue
        if current is None:
            continue
        role_match = re.match(r"^    role: (.+)$", line)
        if role_match:
            result[current]["role"] = role_match.group(1)
            continue
        copilot_match = re.match(r"^    copilot: \{ (.+) \}$", line)
        if copilot_match:
            for item in copilot_match.group(1).split(", "):
                key, _, value = item.partition(": ")
                result[current][key] = value
    return result


class TestAgentInventory(unittest.TestCase):
    def test_exactly_expected_agent_files_exist(self):
        found = {
            os.path.splitext(os.path.splitext(name)[0])[0]
            for name in os.listdir(AGENTS_DIR)
            if name.endswith(".agent.md")
        }
        self.assertEqual(found, set(EXPECTED_AGENTS.keys()))

    def test_fixer_agent_is_retired(self):
        self.assertFalse(
            os.path.exists(os.path.join(AGENTS_DIR, "fixer.agent.md")),
            "agents/fixer.agent.md must be deleted; coder/senior-coder "
            "now handle targeted fixes directly",
        )

    def test_each_agent_frontmatter_matches_shipped_repo_defaults(self):
        # Validates this release's shipped repository defaults, which CI
        # keeps in sync with EXPECTED_AGENTS above. This does not forbid a
        # personal `/subagents` model override for your own installation —
        # that's a per-installation setting, not a source-file change, and
        # is out of scope for this test. It only catches an *unintentional*
        # drift between agents/*.agent.md source and the documented spec;
        # an intentional source-frontmatter change must update
        # EXPECTED_AGENTS in the same commit.
        for name, expected in EXPECTED_AGENTS.items():
            path = os.path.join(AGENTS_DIR, f"{name}.agent.md")
            with self.subTest(agent=name):
                self.assertTrue(os.path.isfile(path), f"missing {path}")
                fm = parse_frontmatter(path)
                self.assertEqual(fm.get("name"), name)
                self.assertEqual(
                    fm.get("model"),
                    expected["model"],
                    f"{name}.agent.md's shipped default model changed; if "
                    "intentional, update EXPECTED_AGENTS to match (personal "
                    "overrides should use /subagents instead of editing "
                    "source)",
                )
                self.assertEqual(fm.get("tools"), expected["tools"])
                self.assertEqual(
                    fm.get("reasoningEffort"),
                    expected["reasoningEffort"],
                    f"{name}.agent.md's shipped default reasoningEffort "
                    "changed; if intentional, update EXPECTED_AGENTS to "
                    "match",
                )
                self.assertTrue(
                    fm.get("description"),
                    f"{name}.agent.md must have a non-empty description",
                )

    def test_structured_routing_config_matches_agent_defaults(self):
        routing = parse_agent_routing(ROUTING_CONFIG)
        self.assertEqual(routing, EXPECTED_ROUTING)

        for name, expected in EXPECTED_AGENTS.items():
            with self.subTest(agent=name):
                self.assertEqual(routing[name]["model"], expected["model"])
                self.assertEqual(
                    routing[name]["reasoning_effort"],
                    expected["reasoningEffort"],
                )

    def test_discovery_description_is_read_only_and_bounded(self):
        fm = parse_frontmatter(os.path.join(AGENTS_DIR, "discovery.agent.md"))
        description = fm.get("description", "").lower()
        self.assertIn("read-only", description)
        self.assertIn("never", description)
        self.assertTrue(
            "writes code" in description or "write code" in description,
            f"discovery.agent.md must disclaim writing code: {description!r}",
        )
        self.assertIn("shell", description)

    def test_planner_description_is_optional_and_ambiguity_only(self):
        fm = parse_frontmatter(os.path.join(AGENTS_DIR, "planner.agent.md"))
        description = fm.get("description", "").lower()
        self.assertIn("optional", description)
        self.assertIn("never writes code", description)
        self.assertTrue(
            "save tokens" in description or "save money" in description,
            f"planner.agent.md must call out its token/cost savings: {description!r}",
        )

    def test_coder_description_is_low_cost_default_and_disclaims_ownership(self):
        fm = parse_frontmatter(os.path.join(AGENTS_DIR, "coder.agent.md"))
        description = fm.get("description", "").lower()
        self.assertIn("low-cost", description)
        self.assertIn("never tests, reviews, or approves its own work", description)
        self.assertIn("escalate to senior-coder", description)

    def test_senior_coder_description_is_escalation_scoped_and_disclaims_ownership(self):
        fm = parse_frontmatter(os.path.join(AGENTS_DIR, "senior-coder.agent.md"))
        description = fm.get("description", "").lower()
        self.assertIn("complex", description)
        self.assertIn("higher-cost", description)
        self.assertIn("never tests, reviews, or approves its own work", description)

    def test_reviewer_description_states_exact_invocation_budget(self):
        fm = parse_frontmatter(os.path.join(AGENTS_DIR, "reviewer.agent.md"))
        description = fm.get("description", "").lower()
        self.assertIn("one comprehensive review", description)
        self.assertIn("up to 3 bounded focused-verification rounds", description)
        self.assertIn("never a second broad review", description)
        self.assertIn("never implementation", description)
        self.assertIn("never tests", description)
        self.assertTrue(
            "tokens" in description or "money" in description,
            f"reviewer.agent.md must call out its token/cost cost discipline: {description!r}",
        )

    def test_tester_description_states_exact_invocation_budget(self):
        fm = parse_frontmatter(os.path.join(AGENTS_DIR, "tester.agent.md"))
        description = fm.get("description", "").lower()
        self.assertIn("one initial test run", description)
        self.assertIn("up to 3 fix/retest rounds", description)
        self.assertIn("max 4 invocations", description)
        self.assertIn("never implements fixes itself", description)

    def test_test_reviewer_description_is_complex_only_and_bounded(self):
        fm = parse_frontmatter(os.path.join(AGENTS_DIR, "test-reviewer.agent.md"))
        description = fm.get("description", "").lower()
        self.assertIn("complex/high-risk tasks only", description)
        self.assertIn("never writes/runs tests or implements", description)
        self.assertIn("one pass plus up to 3 focused verification rounds", description)
        self.assertIn("skip entirely for routine/low-risk changes", description)

    def test_no_agent_claims_review_or_test_ownership_except_owners(self):
        # coder/senior-coder must never claim to test/review/approve their
        # own work; the pipeline relies on reviewer/tester/test-reviewer
        # being the only stages that do that.
        for name in ("coder", "senior-coder"):
            path = os.path.join(AGENTS_DIR, f"{name}.agent.md")
            fm = parse_frontmatter(path)
            description = fm.get("description", "").lower()
            self.assertIn("never", description)
            self.assertTrue(
                "test" in description or "review" in description,
                f"{name}.agent.md description should disclaim "
                f"testing/reviewing its own work: {description!r}",
            )


class BaseHomeTestCase(unittest.TestCase):
    # Toolkit-related env vars that could otherwise point install.sh at
    # real user data (task reports, OTel output, helper scripts, etc.) if
    # they happen to be set in the ambient test-runner environment. Tests
    # must be hermetic: always cleared/overridden below so a run never
    # reads from or writes to anything outside the per-test temp HOME.
    TOOLKIT_ENV_VARS = (
        "COPILOT_HOME",
        "COPILOT_TASK_REPORTS_DIR",
        "COPILOT_TASK_REPORT_HELPER",
        "COPILOT_OTEL_DIR",
        "COPILOT_OTEL_ENABLED",
        "COPILOT_OTEL_EXPORTER_TYPE",
        "COPILOT_OTEL_FILE_EXPORTER_PATH",
        "COPILOT_OTEL_RUN_TS",
        "COPILOT_S_VERSION",
    )

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agent-pipeline-test-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def env(self):
        e = dict(os.environ)
        for var in self.TOOLKIT_ENV_VARS:
            e.pop(var, None)
        e["HOME"] = self.home
        return e

    def run_install(self):
        return subprocess.run(
            ["bash", INSTALL_SH],
            env=self.env(),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def manifest_path(self):
        return os.path.join(self.home, ".copilot-cli-toolkit", "install-manifest.txt")

    def fixer_path(self):
        return os.path.join(self.home, ".copilot", "agents", "fixer.agent.md")


class TestRetiredFixerCleanup(BaseHomeTestCase):
    def test_manifest_managed_fixer_is_removed_and_pruned(self):
        os.makedirs(os.path.join(self.home, ".copilot", "agents"), exist_ok=True)
        os.makedirs(os.path.join(self.home, ".copilot-cli-toolkit"), exist_ok=True)
        with open(self.fixer_path(), "w", encoding="utf-8") as f:
            f.write("stale fixer content\n")
        with open(self.manifest_path(), "w", encoding="utf-8") as f:
            f.write(self.fixer_path() + "\n")

        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertFalse(os.path.exists(self.fixer_path()))
        with open(self.manifest_path(), "r", encoding="utf-8") as f:
            manifest_lines = f.read().splitlines()
        self.assertNotIn(self.fixer_path(), manifest_lines)

    def test_symlink_into_repo_fixer_is_removed(self):
        os.makedirs(os.path.join(self.home, ".copilot", "agents"), exist_ok=True)
        os.symlink(
            os.path.join(AGENTS_DIR, "coder.agent.md"),
            self.fixer_path(),
        )

        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertFalse(os.path.exists(self.fixer_path()))
        self.assertFalse(os.path.islink(self.fixer_path()))

    def test_unmanaged_fixer_file_is_preserved(self):
        os.makedirs(os.path.join(self.home, ".copilot", "agents"), exist_ok=True)
        with open(self.fixer_path(), "w", encoding="utf-8") as f:
            f.write("not toolkit-managed\n")

        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertTrue(os.path.isfile(self.fixer_path()))
        with open(self.fixer_path(), "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "not toolkit-managed\n")

    def test_unmanaged_symlink_outside_repo_is_preserved(self):
        os.makedirs(os.path.join(self.home, ".copilot", "agents"), exist_ok=True)
        outside_target = os.path.join(self.home, "not-in-repo.md")
        with open(outside_target, "w", encoding="utf-8") as f:
            f.write("unrelated file\n")
        os.symlink(outside_target, self.fixer_path())

        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertTrue(os.path.islink(self.fixer_path()))
        self.assertEqual(os.path.realpath(self.fixer_path()), os.path.realpath(outside_target))

    def test_new_agents_are_installed_via_existing_glob(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)

        for name in ("discovery", "senior-coder"):
            installed = os.path.join(self.home, ".copilot", "agents", f"{name}.agent.md")
            self.assertTrue(os.path.islink(installed), f"{installed} should be installed")

    def test_agent_routing_config_is_installed(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)

        installed = os.path.join(self.home, ".copilot", "agent-routing.yaml")
        self.assertTrue(os.path.islink(installed), f"{installed} should be installed")
        self.assertEqual(os.path.realpath(installed), os.path.realpath(ROUTING_CONFIG))


if __name__ == "__main__":
    unittest.main()
