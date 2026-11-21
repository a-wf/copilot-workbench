#!/usr/bin/env python3
"""Offline behavioral contracts for the local-only model refresh transaction.

All facts/prices in this file are SYNTHETIC fixtures, not model-quality or
availability claims. Every writable path is under a fresh temporary HOME.
The real shared pricing parser runs, but its transport is always injected.
"""

import contextlib
import copy
import functools
import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "bin" / "copilot-model-refresh.py"
REPORT = ROOT / "bin" / "copilot-task-report.py"
NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc).timestamp()
DAY = 86400
CHEAP = "fixture-cheap"
UNKNOWN = "fixture-unassessed"
NEW = "gpt-6.2-sol"
DOC_ONLY = "fixture-docs-only"
USER_ONLY = "fixture-user-only"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RefreshCase(unittest.TestCase):
    # Preserve the existing cost-led contracts as EXPLICIT legacy-policy
    # coverage. New balanced/compact tests below use the unadapted defaults.
    TEST_POLICY = "cost-led"
    TEST_DETAILS = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="model-refresh-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        # A non-default COPILOT_HOME catches accidental settings/state writes
        # to ~/.copilot, while the shared pricing module is explicitly isolated.
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith(("COPILOT_", "XDG_"))}
        clean_env.update(
            HOME=str(self.home), COPILOT_HOME=str(self.home / "custom-copilot"),
            COPILOT_TASK_REPORT_PRICING_FETCH="1", PYTHONDONTWRITEBYTECODE="1",
            TZ="UTC0",
        )
        self.env_patch = mock.patch.dict(os.environ, clean_env, clear=True)
        self.env_patch.start()
        if hasattr(time, "tzset"):
            time.tzset()
        self.addCleanup(self.restore_environment)
        self.mod = load_module("model_refresh_under_test", HELPER)
        if self.TEST_POLICY:
            self.mod.build_context = functools.partial(
                self.mod.build_context, policy=self.TEST_POLICY)
        for target in ("socket.getaddrinfo", "socket.create_connection",
                       "socket.socket.connect", "socket.socket.connect_ex"):
            guard = mock.patch(target, side_effect=AssertionError("live network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.pricing = load_module("refresh_pricing_under_test", REPORT)
        self.paths = self.mod.Paths()
        self.pricing.SUPPORT_DIR = str(self.home / "isolated-pricing")
        self.mod.PRICING_MODULE = self.pricing
        self.routing = self.home / "routing.yaml"
        shutil.copyfile(ROOT / "config" / "agent-routing.yaml", self.routing)
        self.catalog_path = self.home / "catalog.json"
        os.environ["COPILOT_MODEL_REFRESH_ROUTING"] = str(self.routing)
        os.environ["COPILOT_MODEL_REFRESH_CATALOG"] = str(self.catalog_path)
        self.defaults, _, _ = self.mod.load_defaults(self.paths)
        self.ids = sorted({v["model"] for v in self.defaults.values()} | {CHEAP})
        self.catalog = {
            "schema_version": 1, "kind": self.mod.CATALOG_KIND,
            "reviewed_on": "2026-11-15", "expires_on": "2026-12-15",
            "sources": {"fixture": {
                "url": "https://docs.github.com/fixture-not-a-model-claim",
                "retrieved_on": "2026-11-15",
            }},
            "routes": {},
            "models": {mid: {
                "display_name": mid, "provider": "OpenAI",
                "category": "Powerful", "task_area": "Fixture coding",
                "one_million_context": True, "configurable_reasoning": True,
                "copilot_cli_client": "included", "sources": ["fixture"],
            } for mid in self.ids},
        }
        self.config = {
            "available_models": {"verified_on": "2026-11-16", "models": self.ids},
            "verified_capabilities": {
                "verified_on": "2026-11-16",
                "models": {mid: {"reasoning_levels": ["low", "medium", "high", "xhigh"]}
                           for mid in self.ids},
            },
        }
        self.save_catalog()
        self.save_config()
        self.initial_settings = {"theme": "user-theme", "subagents": {
            "maxConcurrent": 3, "agents": {"unrelated-agent": {"model": "human-choice"}}}}
        self.write_json(self.paths.settings, self.initial_settings)
        os.chmod(self.paths.settings, 0o640)
        self.fetches = []
        self.markdown = self.make_markdown()
        self.mod.PRICING_FETCHER = self.fetch
        self.pricing.PRICING_FETCHER = self.fetch

    def restore_environment(self):
        self.env_patch.stop()
        if hasattr(time, "tzset"):
            time.tzset()

    @contextlib.contextmanager
    def local_timezone(self, tz):
        try:
            with mock.patch.dict(os.environ, {"TZ": tz}):
                time.tzset()
                yield
        finally:
            time.tzset()

    def write(self, path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())

    def write_json(self, path, data):
        self.write(path, json.dumps(data, indent=2))

    def read_json(self, path):
        return json.loads(Path(path).read_text())

    def save_catalog(self):
        self.write_json(self.catalog_path, self.catalog)

    def save_config(self):
        self.write_json(self.paths.user_config, self.config)

    def make_markdown(self, cheap=5, cheap_long=None, baseline=10, categories=None):
        lines = [
            "## Pricing tables", "", "Prices are in USD per 1 million tokens.", "", "### OpenAI",
            "| Model | Release status | Category | Tier | Threshold (input tokens) | Input | Cached input | Cache write | Output |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for mid in self.ids + [UNKNOWN]:
            cost = cheap if mid == CHEAP else (0.01 if mid == UNKNOWN else baseline)
            category = (categories or {}).get(mid, "Powerful")
            tiers = [("Default", "Not applicable", cost)]
            if mid == CHEAP and cheap_long is not None:
                tiers = [("Default", "≤ 272K", cost), ("Long context", "> 272K", cheap_long)]
            for tier, threshold, rate in tiers:
                lines.append("| %s | GA | %s | %s | %s | $%s | $%s | $%s | $%s |" %
                             (mid, category, tier, threshold, rate, rate, rate, rate))
        return "\n".join(lines).encode()

    def fetch(self, url, timeout, max_bytes):
        self.fetches.append((url, timeout, max_bytes))
        return self.markdown

    def seed_cache(self, age=0, markdown=None, now=NOW):
        snapshot = self.pricing.build_official_snapshot(
            self.markdown if markdown is None else markdown, now - age)
        self.write_json(self.pricing.official_pricing_cache_path(), snapshot)
        return snapshot

    def cli(self, *args, now=NOW):
        args = list(args)
        if args and args[0] in ("preview", "propose"):
            if self.TEST_POLICY == "cost-led" and "--cost-led" not in args:
                args.append("--cost-led")
            if self.TEST_DETAILS and "--details" not in args:
                args.append("--details")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.mod.main(list(args), now=now)
        return rc, out.getvalue(), err.getvalue()

    def proposal(self):
        rc, out, err = self.cli("propose")
        self.assertEqual(rc, 0, out + err)
        markers = [line.split("\t")[1] for line in out.splitlines()
                   if line.startswith("PROPOSAL\t")]
        self.assertEqual(len(markers), 1, out)
        self.assertRegex(markers[0], r"^[0-9a-f]{16}$")
        return markers[0]

    def apply(self):
        proposal_id = self.proposal()
        rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 0, out + err)
        return out

    def routing_bytes(self):
        return {p: Path(p).read_bytes() if Path(p).exists() else None for p in (
            self.paths.settings, self.paths.instructions, self.paths.effective, self.paths.state)}

    def decisions(self, now=NOW):
        self.seed_cache(now=now)
        return {r["key"]: r for r in self.mod.evaluate(
            self.mod.build_context(self.paths, now))}

    def live_routes(self, output):
        rows = {}
        for line in output.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            key, *cells = line.split()
            self.assertIn(key, self.defaults, line)
            self.assertNotIn(key, rows, line)
            rows[key] = dict(cell.split("=", 1) for cell in cells)
            self.assertEqual(set(rows[key]), set(self.mod.FIELDS))
        self.assertEqual(set(rows), set(self.defaults))
        return rows

    def assert_no_changes(self, rows, reason=None):
        self.assertEqual(len(rows), 10)
        for row in rows.values():
            self.assertIsNone(row["proposed"], row)
            if reason:
                reasons = row["reason"] + repr(row["blocked"])
                self.assertIn(reason, reasons)


class TestRefreshLifecycle(RefreshCase):
    def test_help_is_effect_free_even_without_catalog_or_pricing(self):
        self.catalog_path.unlink()
        before = sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))
        with mock.patch.object(self.mod, "load_pricing_module", side_effect=AssertionError("loaded pricing")):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as ex:
                    self.mod.main(["--help"], now=NOW)
        self.assertEqual(ex.exception.code, 0)
        self.assertIn("propose", out.getvalue())
        self.assertEqual(before, sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")))

    def test_status_is_cache_only_and_does_not_write(self):
        before = self.routing_bytes()
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)
        self.assertIn("Current effective routing", out)
        self.assertEqual(self.fetches, [])
        self.assertEqual(before, self.routing_bytes())
        self.assertFalse(Path(self.paths.state_dir, ".lock").exists())

    def test_preview_discloses_all_routes_costs_and_evidence_without_routing_writes(self):
        before = self.routing_bytes()
        rc, out, err = self.cli("preview")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.routing_bytes(), before)
        self.assertFalse(Path(self.paths.proposal).exists())
        self.assertEqual(self.fetches, [(
            self.pricing.OFFICIAL_PRICING_API_URL,
            self.pricing.OFFICIAL_PRICING_TIMEOUT_SECONDS,
            self.pricing.OFFICIAL_PRICING_MAX_BYTES,
        )])
        for text in ("NOT measured quality", "not a forecast", "15% input", "75% cached input",
                     "5% cache write", "5% output", "reviewed 2026-11-15",
                     "verified 2026-11-16", "Default tier", "Cache write", "1M context",
                     "Effective routing IF", "10.00", "5.00"):
            self.assertIn(text, out)
        for route in self.mod.ROUTES:
            self.assertIn(route["label"], out)
        self.assertIn(UNKNOWN, out)
        self.assertIn("Unassessed", out)
        self.assert_no_unknown_assignment()

    def assert_no_unknown_assignment(self):
        rows = self.decisions()
        self.assertTrue(all(r["proposed"]["model"] != UNKNOWN
                            for r in rows.values() if r["proposed"]))

    def test_propose_freezes_inputs_for_fifteen_minutes_and_discard_keeps_settings(self):
        before = self.routing_bytes()
        proposal_id = self.proposal()
        doc = self.read_json(self.paths.proposal)
        self.assertEqual(doc["expires_epoch"] - doc["created_epoch"], 900)
        self.assertEqual(len(doc["route_changes"]), 10)
        self.assertEqual(set(doc["digests"]), {
            "settings", "state", "user_config", "routing", "catalog", "instructions", "effective",
            "verified", "capabilities", "pricing", "benchmarks", "economics"})
        self.assertEqual(doc["digests"]["economics"], self.mod.economics_digest())
        self.assertEqual(self.routing_bytes(), before)
        rc, out, err = self.cli("discard", "--proposal-id", proposal_id)
        self.assertEqual(rc, 0, err)
        self.assertIn("Cancelled", out)
        self.assertIn("Final effective routing", out)
        self.assertFalse(Path(self.paths.proposal).exists())
        self.assertEqual(self.routing_bytes(), before)

    def test_repeated_proposal_is_deterministic_for_identical_frozen_inputs(self):
        before = self.routing_bytes()
        self.proposal()
        first = self.read_json(self.paths.proposal)
        proposal_id = self.proposal()
        second = self.read_json(self.paths.proposal)
        for field in ("overrides", "route_changes", "digests", "expected", "snapshot_id",
                      "created_epoch", "expires_epoch"):
            self.assertEqual(first[field], second[field], field)
        self.assertEqual(self.routing_bytes(), before)
        self.assertEqual(len(self.fetches), 1, "second proposal reuses the fresh official snapshot")
        rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 0, out + err)

    def test_first_run_action_required_is_editable_advisory_not_verified_facts(self):
        Path(self.paths.user_config).unlink()
        rc, out, err = self.cli("propose")
        self.assertEqual(rc, 3, out + err)
        self.assertIn("ACTION REQUIRED", out)
        self.assertIn(self.paths.user_config, out)
        example = out.split("e.g.:\n", 1)[1].split("\n  List ONLY", 1)[0]
        data = json.loads(example)
        self.assertEqual(data["available_models"]["verified_on"], "2026-11-16")
        self.assertIn(CHEAP, data["available_models"]["models"])
        self.assertIn(CHEAP, data["verified_capabilities"]["models"])
        self.assertIn("not with verified facts", out)
        self.assertFalse(Path(self.paths.user_config).exists())
        self.assertFalse(Path(self.paths.proposal).exists())

    def test_no_savings_returns_three_and_final_kept_table(self):
        self.markdown = self.make_markdown(cheap=9.01)
        rc, out, err = self.cli("propose")
        self.assertEqual(rc, 3, out + err)
        self.assertNotIn("PROPOSAL\t", out)
        self.assertIn("final effective routing (kept)", out)

    def test_expired_id_wrong_id_and_tampered_result_refuse_without_writes(self):
        for case in ("expired", "wrong-id", "tampered"):
            with self.subTest(case=case):
                proposal_id = self.proposal()
                before = self.routing_bytes()
                now = NOW
                if case == "expired":
                    now += 901
                elif case == "wrong-id":
                    proposal_id = "0" * 16
                else:
                    doc = self.read_json(self.paths.proposal)
                    doc["overrides"]["coder"]["model"] = "unexpected-model"
                    self.write_json(self.paths.proposal, doc)
                rc, out, err = self.cli("apply", "--proposal-id", proposal_id, now=now)
                self.assertEqual(rc, 4, out + err)
                self.assertEqual(before, self.routing_bytes())

    def test_changed_input_digests_refuse_apply(self):
        mutations = {
            "settings": lambda: self.write_json(self.paths.settings, {"theme": "changed-by-human"}),
            "user_config": lambda: self.write(self.paths.user_config, Path(self.paths.user_config).read_bytes() + b"\n"),
            "routing": lambda: self.write(self.routing, self.routing.read_bytes() + b"\n# edited\n"),
            "catalog": lambda: self.write(self.catalog_path, self.catalog_path.read_bytes() + b"\n"),
            "instructions": lambda: self.write(self.paths.instructions, "a human file\n"),
            "effective": lambda: self.write(self.paths.effective, "{}\n"),
            "state": lambda: self.write_json(self.paths.state, self.mod.blank_state()),
        }
        for key, mutate in mutations.items():
            with self.subTest(input=key):
                proposal_id = self.proposal()
                mutate()
                before = self.routing_bytes()
                rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
                self.assertEqual(rc, 4, out + err)
                self.assertIn(key, err)
                self.assertEqual(before, self.routing_bytes())
                # Restore only this fixture's edits, not implementation state.
                for path in (self.paths.instructions, self.paths.effective, self.paths.state):
                    Path(path).unlink(missing_ok=True)

    def test_keyboard_interrupt_before_commit_is_130_and_keeps_settings(self):
        before = self.routing_bytes()
        with mock.patch.object(self.mod, "build_context", side_effect=KeyboardInterrupt):
            rc, out, err = self.cli("propose")
        self.assertEqual(rc, 130, out + err)
        self.assertIn("Interrupted", err)
        self.assertEqual(before, self.routing_bytes())


class TestManualPricingRefresh(RefreshCase):
    def test_manual_refresh_reuses_under_five_minutes_then_bypasses_daily_ttl(self):
        self.seed_cache(age=299)
        rc, out, err = self.cli("preview")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.fetches, [])
        self.assertIn("within the last 5 min", out)
        self.seed_cache(age=300)
        rc, out, err = self.cli("preview")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.fetches), 1)

    def test_manual_refresh_bypasses_automatic_failure_backoff(self):
        self.seed_cache(age=600)
        self.mod.PRICING_FETCHER = mock.Mock(side_effect=OSError("fixture offline"))
        self.cli("preview")
        self.mod.PRICING_FETCHER = self.fetch
        rc, out, err = self.cli("preview", now=NOW + 1)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.fetches), 1)
        self.assertIn("refreshed now", out)

    def test_stale_expired_and_future_price_cache_retained_for_reference_not_assignment(self):
        for age in (DAY, 8 * DAY, -1):
            with self.subTest(age=age):
                self.seed_cache(age=age)
                cache = Path(self.pricing.official_pricing_cache_path()).read_bytes()
                self.mod.PRICING_FETCHER = mock.Mock(side_effect=OSError("fixture offline"))
                rc, out, err = self.cli("propose")
                self.assertEqual(rc, 3, out + err)
                self.assertIn("proposals need a snapshot < 24h old", out)
                self.assertNotIn("PROPOSAL\t", out)
                self.assertEqual(Path(self.pricing.official_pricing_cache_path()).read_bytes(), cache)
                self.assertFalse(Path(self.paths.state).exists())

    def test_failed_first_fetch_does_not_fall_back_to_legacy_prices(self):
        self.write_json(self.pricing.PRICING_FILE, {
            "models": {mid: {"input_per_million": 0, "output_per_million": 0} for mid in self.ids}})
        self.mod.PRICING_FETCHER = mock.Mock(side_effect=OSError("offline fixture"))
        rc, out, err = self.cli("propose")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("no valid official pricing snapshot", err)
        self.assertIn("not $0", err)
        self.assertFalse(Path(self.paths.proposal).exists())

    def test_malformed_markdown_and_cache_never_produce_a_mapping(self):
        for raw in (b"not a pricing table", b'{"models": [not-json]}', b"\xff"):
            with self.subTest(raw=raw):
                self.write(self.pricing.official_pricing_cache_path(), b'{"broken": true}')
                self.mod.PRICING_FETCHER = mock.Mock(return_value=raw)
                rc, out, err = self.cli("propose")
                self.assertEqual(rc, 2, out + err)
                self.assertFalse(Path(self.paths.state).exists())
                self.assertFalse(Path(self.paths.proposal).exists())
                self.assertEqual(Path(self.pricing.official_pricing_cache_path()).read_bytes(), b'{"broken": true}')

    def test_kill_switch_and_offline_never_fetch(self):
        self.seed_cache(age=600)
        for args in (("preview", "--offline"), ("preview",)):
            with self.subTest(args=args):
                os.environ["COPILOT_TASK_REPORT_PRICING_FETCH"] = "0"
                rc, out, err = self.cli(*args)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(self.fetches, [])

    def test_category_labels_are_optional_bounded_and_consistent_across_tiers(self):
        snap = self.pricing.build_official_snapshot(self.make_markdown(cheap_long=5), NOW)
        self.assertEqual(snap["models"][CHEAP]["category"], "Powerful")
        self.assertEqual(snap["models"][CHEAP]["release_status"], "GA")
        conflicting = self.make_markdown(cheap_long=5).replace(
            b"| fixture-cheap | GA | Powerful | Long context",
            b"| fixture-cheap | GA | Versatile | Long context")
        snap = self.pricing.build_official_snapshot(conflicting, NOW)
        self.assertNotIn("category", snap["models"][CHEAP])
        for entry in snap["models"].values():
            entry.pop("category", None)
            entry.pop("release_status", None)
        self.pricing.validate_official_snapshot(snap)  # legacy snapshots remain valid
        snap["models"][CHEAP]["category"] = 42
        with self.assertRaises(self.pricing.OfficialPricingError):
            self.pricing.validate_official_snapshot(snap)

    def test_missing_live_category_unknown_cost_not_zero_or_eligible(self):
        snapshot = self.seed_cache()
        del snapshot["models"][CHEAP]["category"]
        self.write_json(self.pricing.official_pricing_cache_path(), snapshot)
        ctx = self.mod.build_context(self.paths, NOW)
        self.assert_no_changes({r["key"]: r for r in self.mod.evaluate(ctx)})
        self.assertIsNone(self.mod.blended_cost(self.mod.default_rates(snapshot, "missing-model")))
        reasons = repr(self.mod.evaluate(ctx))
        self.assertIn("Category not available", reasons)


class TestCategoricalSelection(RefreshCase):
    def assert_unpriced_current_is_kept(self, missing, alternative):
        self.markdown = b"\n".join(
            line for line in self.markdown.splitlines()
            if not any(line.startswith(("| %s |" % mid).encode()) for mid in missing))
        self.seed_cache()
        ctx = self.mod.build_context(self.paths, NOW)
        rows = {r["key"]: r for r in self.mod.evaluate(ctx)}
        self.assert_no_changes(rows, "known current price")
        self.assertEqual({r["current"]["context_tier"] for r in rows.values()},
                         {"default", "long_context"})
        for key, row in rows.items():
            self.assertIsNone(row["current_cost"], row)
            policy, why = self.mod.route_policy(key, self.defaults, ctx["catalog"], ctx["today"])
            self.assertIsNone(why)
            candidate = alternative(key)
            self.assertEqual(self.mod.check_candidate(
                candidate, key, self.defaults[key], policy, ctx), [], row)
            self.assertIsNotNone(self.mod.default_rates(ctx["snapshot"], candidate))
        before = self.routing_bytes()
        rc, out, err = self.cli("propose", "--offline")
        self.assertEqual(rc, 3, out + err)
        self.assertNotIn("PROPOSAL\t", out)
        self.assertEqual(self.routing_bytes(), before)

    def test_unpriced_shipped_baselines_stay_for_default_and_long_routes(self):
        self.assert_unpriced_current_is_kept(
            {v["model"] for v in self.defaults.values()}, lambda _key: CHEAP)

    def test_unpriced_managed_models_stay_despite_priced_eligible_alternatives(self):
        self.apply()
        self.markdown = self.make_markdown(baseline=1)
        self.assert_unpriced_current_is_kept({CHEAP}, lambda key: self.defaults[key]["model"])

    def test_all_ten_routes_keep_exact_baseline_effort_and_context(self):
        rows = self.decisions()
        self.assertEqual(set(rows), set(self.defaults))
        for key, row in rows.items():
            self.assertEqual(row["proposed"], dict(self.defaults[key], model=CHEAP), row)
            self.assertIn("NOT a measured-quality or equal-quality claim", row["reason"])
        self.assertEqual(rows["coder.small_casual"]["proposed"]["reasoning_effort"], "low")
        self.assertEqual(rows["tester"]["proposed"]["reasoning_effort"], "xhigh")
        self.assertEqual(rows["reviewer.stronger"]["proposed"]["context_tier"], "long_context")

    def test_each_gate_fails_closed_even_for_a_cheap_model(self):
        changes = (
            ("task_area", "Different task area", "task area"),
            ("category", "Lightweight", "below"),
            ("configurable_reasoning", False, "configurable-reasoning"),
            ("copilot_cli_client", "not_included", "Copilot CLI"),
            ("copilot_cli_client", "not_listed", "Copilot CLI"),
        )
        original = copy.deepcopy(self.catalog["models"][CHEAP])
        for field, value, reason in changes:
            with self.subTest(field=field, value=value):
                self.catalog["models"][CHEAP] = dict(original, **{field: value})
                self.save_catalog()
                self.assert_no_changes(self.decisions(), reason)

    def test_live_category_drift_blocks_instead_of_trusting_curated_label(self):
        self.markdown = self.make_markdown(categories={CHEAP: "Versatile"})
        self.assert_no_changes(self.decisions(), "Category changed")

    def test_one_million_context_required_only_for_long_context_routes(self):
        self.catalog["models"][CHEAP]["one_million_context"] = False
        self.save_catalog()
        for key, row in self.decisions().items():
            if self.defaults[key]["context_tier"] == "long_context":
                self.assertIsNone(row["proposed"], row)
                self.assertIn("1M-context", repr(row["blocked"]))
            else:
                self.assertEqual(row["proposed"]["model"], CHEAP)

    def test_generic_reasoning_flag_is_not_evidence_of_any_exact_level(self):
        self.config.pop("verified_capabilities")
        self.save_config()
        self.assert_no_changes(self.decisions(), "not verified")

    def test_only_exact_verified_effort_is_allowed_without_downgrade(self):
        self.config["verified_capabilities"]["models"][CHEAP]["reasoning_levels"] = ["low"]
        self.save_config()
        for key, row in self.decisions().items():
            if self.defaults[key]["reasoning_effort"] == "low":
                self.assertEqual(row["proposed"]["reasoning_effort"], "low")
            else:
                self.assertIsNone(row["proposed"], row)
                self.assertIn("not offered", repr(row["blocked"]))

    def test_source_backed_specific_levels_can_replace_user_capability_record(self):
        self.config.pop("verified_capabilities")
        self.save_config()
        self.catalog["models"][CHEAP]["reasoning_levels"] = ["low", "medium", "high", "xhigh"]
        self.save_catalog()
        self.assertTrue(all(r["proposed"] for r in self.decisions().values()))

    def test_missing_stale_future_or_excluded_availability_blocks(self):
        for verified in (None, "2026-10-16", "2026-11-17", "excluded"):
            with self.subTest(verified=verified):
                self.config["available_models"] = {"verified_on": "2026-11-16", "models": self.ids}
                if verified is None:
                    self.config.pop("available_models")
                elif verified == "excluded":
                    self.config["available_models"]["models"] = [m for m in self.ids if m != CHEAP]
                else:
                    self.config["available_models"]["verified_on"] = verified
                self.save_config()
                self.assert_no_changes(self.decisions(), "availab")

    def test_thirty_day_availability_and_capability_boundary_is_inclusive(self):
        self.config["available_models"]["verified_on"] = "2026-10-17"
        self.config["verified_capabilities"]["verified_on"] = "2026-10-17"
        self.save_config()
        self.assertTrue(all(r["proposed"] for r in self.decisions().values()))
        for verified in ("2026-10-16", "2026-11-17"):
            self.config["verified_capabilities"]["verified_on"] = verified
            self.save_config()
            self.assert_no_changes(self.decisions(), "stale or future")

    def test_catalog_expiry_missing_entry_and_missing_required_data_refuse(self):
        self.catalog["expires_on"] = "2026-11-15"
        self.save_catalog()
        self.assert_no_changes(self.decisions(), "catalog review expired")
        self.catalog["expires_on"] = "2026-12-15"
        self.catalog["models"].pop(CHEAP)
        self.save_catalog()
        self.assert_no_changes(self.decisions())
        del self.catalog["models"][self.defaults["coder"]["model"]]["task_area"]
        self.save_catalog()
        rc, out, err = self.cli("propose")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("missing task_area", err)

    def test_future_catalog_review_cannot_authorize_a_replacement(self):
        self.catalog["reviewed_on"] = "2026-11-17"
        self.save_catalog()
        self.assert_no_changes(self.decisions())

    def test_future_source_retrieval_cannot_authorize_a_replacement(self):
        self.catalog["sources"]["fixture"]["retrieved_on"] = "2026-11-17"
        self.save_catalog()
        self.assert_no_changes(self.decisions())

    def test_expired_availability_retains_existing_managed_routes_not_unverified_reselection(self):
        self.apply()
        self.config["available_models"]["verified_on"] = "2026-10-16"
        self.save_config()
        rows = self.decisions()
        self.assert_no_changes(rows)
        self.assertTrue(all("availab" in (r["reason"] + repr(r["blocked"])) for r in rows.values()))

    def test_managed_route_needs_verified_availability_even_when_baseline_is_now_cheaper(self):
        self.apply()
        self.markdown = self.make_markdown(cheap=5, baseline=4)
        self.config["available_models"]["models"] = [mid for mid in self.ids if mid != CHEAP]
        for verified_on in ("2026-10-16", "2026-11-16"):
            with self.subTest(verified_on=verified_on):
                self.config["available_models"]["verified_on"] = verified_on
                self.save_config()
                rows = self.decisions()
                if verified_on == "2026-10-16":
                    self.assert_no_changes(rows, "availab")
                else:
                    # The current override's missing availability must not
                    # block a genuinely verified, >=10%-cheaper alternative.
                    for key, row in rows.items():
                        self.assertEqual(row["proposed"], self.defaults[key], row)
                        self.assertAlmostEqual(row["current_cost"], 5)
                        self.assertAlmostEqual(row["proposed_cost"], 4)

    def test_invalid_overlay_dates_cannot_authorize_candidate_or_route_policy(self):
        invalid_dates = (
            ("future-review", "2026-11-17", "2026-12-15", "2026-11-15", "future"),
            ("future-source", "2026-11-15", "2026-12-15", "2026-11-17", "future"),
            ("zero-validity", "2026-11-15", "2026-11-15", "2026-11-15", "not after"),
            ("source-after-review", "2026-11-15", "2026-12-15", "2026-11-16", "after the review"),
        )
        for target in ("candidate", "route-policy"):
            for name, reviewed, expires, retrieved, reason in invalid_dates:
                with self.subTest(target=target, dates=name):
                    overlay = {
                        "reviewed_on": reviewed, "expires_on": expires,
                        "sources": copy.deepcopy(self.catalog["sources"]),
                        "models": {}, "routes": {},
                    }
                    overlay["sources"]["fixture"]["retrieved_on"] = retrieved
                    if target == "candidate":
                        overlay["models"][CHEAP] = copy.deepcopy(self.catalog["models"][CHEAP])
                    else:
                        overlay["routes"] = {
                            key: {"accepted_task_areas": ["Fixture coding"], "min_category": "Powerful"}
                            for key in self.defaults}
                    self.config["catalog"] = overlay
                    self.save_config()
                    self.assert_no_changes(self.decisions(), reason)

    def test_dated_local_overlay_can_assess_new_model_without_editing_shipped_catalog(self):
        before = self.catalog_path.read_bytes()
        self.config["catalog"] = {
            "reviewed_on": "2026-11-16", "expires_on": "2026-12-01",
            "sources": copy.deepcopy(self.catalog["sources"]),
            "models": {UNKNOWN: copy.deepcopy(self.catalog["models"][CHEAP])},
        }
        self.config["available_models"]["models"].append(UNKNOWN)
        self.config["verified_capabilities"]["models"][UNKNOWN] = {
            "reasoning_levels": ["low", "medium", "high", "xhigh"]}
        self.save_config()
        rows = self.decisions()
        self.assertTrue(all(r["proposed"]["model"] == UNKNOWN for r in rows.values()))
        self.assertEqual(before, self.catalog_path.read_bytes())

    def test_ten_percent_threshold_and_long_tier_savings_both_required(self):
        for price, expected in ((9, True), (9.001, False), (10, False)):
            with self.subTest(price=price):
                self.markdown = self.make_markdown(cheap=price)
                for row in self.decisions().values():
                    self.assertEqual(row["proposed"] is not None, expected, row)
        self.markdown = self.make_markdown(cheap=5, cheap_long=9.1)
        for key, row in self.decisions().items():
            self.assertEqual(row["proposed"] is not None,
                             self.defaults[key]["context_tier"] == "default", row)

    def test_disclosed_blend_is_not_just_output_price_and_handles_unlisted_cache_write(self):
        rates = {"input": 10, "cached_input": 2, "cache_write": 12,
                 "cache_write_status": "rate", "output": 20}
        self.assertAlmostEqual(self.mod.blended_cost(rates), 4.6)
        rates.update(cache_write=None, cache_write_status="not_listed")
        self.assertAlmostEqual(self.mod.blended_cost(rates), 4.5)


@unittest.skipUnless(hasattr(time, "tzset"), "local calendar tests require POSIX TZ support")
class TestLocalCalendarDates(RefreshCase):
    def midnight_samples(self):
        # POSIX TZ signs are reversed: UTC-14 is UTC+14, UTC+12 is UTC-12.
        for tz, hour, utc_day in (("UTC-14", 10, 16), ("UTC+12", 12, 17)):
            midnight = datetime(2026, 11, utc_day, hour, tzinfo=timezone.utc).timestamp()
            for delta in (-1, 0):
                local_day = datetime(2026, 11, 17).date() + timedelta(days=-1 if delta else 0)
                yield tz, midnight + delta, local_day

    def calendar_inputs(self, local_day):
        today = local_day.isoformat()
        self.catalog["reviewed_on"] = today
        self.catalog["expires_on"] = (local_day + timedelta(days=60)).isoformat()
        self.catalog["sources"]["fixture"]["retrieved_on"] = today
        self.config["available_models"]["verified_on"] = today
        self.config["verified_capabilities"]["verified_on"] = today
        self.save_catalog()
        self.save_config()

    def test_local_today_accepted_but_next_local_day_rejected_across_both_midnights(self):
        for tz, epoch, local_day in self.midnight_samples():
            with self.subTest(tz=tz, epoch=epoch), self.local_timezone(tz):
                self.calendar_inputs(local_day)
                rows = self.decisions(now=epoch)
                self.assertEqual(self.mod.build_context(self.paths, epoch)["today"], local_day)
                self.assertTrue(all(row["proposed"] for row in rows.values()), rows)
                tomorrow = (local_day + timedelta(days=1)).isoformat()
                for gate in ("catalog", "availability", "capabilities"):
                    with self.subTest(future_gate=gate):
                        self.calendar_inputs(local_day)
                        if gate == "catalog":
                            self.catalog["reviewed_on"] = tomorrow
                            self.catalog["sources"]["fixture"]["retrieved_on"] = tomorrow
                            self.save_catalog()
                        elif gate == "availability":
                            self.config["available_models"]["verified_on"] = tomorrow
                        else:
                            self.config["verified_capabilities"]["verified_on"] = tomorrow
                        self.save_config()
                        self.assert_no_changes(self.decisions(now=epoch))

    def test_thirty_day_boundary_uses_local_calendar_for_availability_and_capabilities(self):
        for tz, epoch, local_day in self.midnight_samples():
            with self.subTest(tz=tz, epoch=epoch), self.local_timezone(tz):
                self.calendar_inputs(local_day)
                thirty_days = (local_day - timedelta(days=30)).isoformat()
                thirty_one_days = (local_day - timedelta(days=31)).isoformat()
                for record in ("available_models", "verified_capabilities"):
                    self.config[record]["verified_on"] = thirty_days
                self.save_config()
                self.assertTrue(all(row["proposed"] for row in self.decisions(now=epoch).values()))
                for stale_record in ("available_models", "verified_capabilities"):
                    with self.subTest(stale_record=stale_record):
                        for record in ("available_models", "verified_capabilities"):
                            self.config[record]["verified_on"] = (
                                thirty_one_days if record == stale_record else thirty_days)
                        self.save_config()
                        self.assert_no_changes(self.decisions(now=epoch))


class TestApplyPinsAndRouting(RefreshCase):
    def test_apply_settings_generated_instruction_snapshot_and_final_table_agree(self):
        sources = {p: p.read_bytes() for p in (
            ROOT / "config" / "agent-routing.yaml", ROOT / "instructions" / "copilot-instructions.md",
            *sorted((ROOT / "agents").glob("*.agent.md")))}
        out = self.apply()
        self.assertIn("Final effective routing", out)
        settings = self.read_json(self.paths.settings)
        self.assertEqual(settings["theme"], "user-theme")
        self.assertEqual(settings["subagents"]["maxConcurrent"], 3)
        self.assertEqual(settings["subagents"]["agents"]["unrelated-agent"], {"model": "human-choice"})
        state = self.read_json(self.paths.state)
        doc = self.read_json(self.paths.effective)
        self.assertEqual(len(doc["routes"]), 10)
        instruction = Path(self.paths.instructions).read_text()
        self.assertNotIn("applyTo", instruction)
        self.assertIn("as the task call's `model`, `reasoning_effort` or `context_tier`", instruction)
        self.assertIn("status --instructions", instruction)
        self.assertIn(self.mod.INSTRUCTIONS_MARKER, instruction)
        # A generated personal instruction must not restore the retired
        # mandatory-delegation wording over the shipped trivial-edit exception.
        prose = " ".join(instruction.split())
        self.assertIn("default delegation to `coder` for meaningful small/casual", prose)
        self.assertIn("main session may make a genuinely trivial edit itself", prose)
        self.assertIn("independent review of every code change stays mandatory", prose)
        self.assertNotIn("mandatory delegation to `coder`", prose)
        self.assertEqual(sum(line.startswith("| `") for line in instruction.splitlines()), 10)
        self.assertNotIn("http", instruction)
        for route in self.mod.ROUTES:
            key = route["key"]
            values = doc["routes"][key]["values"]
            self.assertEqual(values, dict(self.defaults[key], model=CHEAP))
            self.assertIn("| `%s` | `%s` | `%s` | `%s` |" % (
                key, values["model"], values["reasoning_effort"], values["context_tier"]), instruction)
            if route["kind"] == "agent":
                for field, native in self.mod.SETTINGS_FIELDS:
                    self.assertEqual(settings["subagents"]["agents"][route["agent"]][native], values[field])
            else:
                self.assertNotIn(key, settings["subagents"]["agents"])
        self.assertEqual(set(state["overrides"]), set(self.defaults))
        self.assertEqual(state["history"][-1]["action"], "apply")
        self.assertEqual(state["instructions_sha256"], self.mod.file_sha256(self.paths.instructions))
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.settings).st_mode), 0o640)
        for p in (self.paths.state, self.paths.effective):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertFalse(Path(self.paths.journal).exists())
        self.assertFalse(Path(self.paths.proposal).exists())
        self.assertEqual({p: p.read_bytes() for p in sources}, sources)
        self.assertFalse((self.home / ".copilot").exists())

    def test_user_native_and_user_config_pins_take_precedence_without_writes_to_them(self):
        settings = copy.deepcopy(self.initial_settings)
        settings["subagents"]["agents"]["coder"] = {
            "model": "native-choice", "effortLevel": "inherit", "contextTier": "inherit"}
        self.write_json(self.paths.settings, settings)
        self.config["pins"] = {
            "coder": {"model": "user-config-choice"},
            "coder.small_casual": {"model": "small-pin"},
            "reviewer.stronger": {"reasoning_effort": "max"},
        }
        self.save_config()
        config_before = Path(self.paths.user_config).read_bytes()
        self.apply()
        after = self.read_json(self.paths.settings)
        self.assertEqual(after["subagents"]["agents"]["coder"], settings["subagents"]["agents"]["coder"])
        routes = self.read_json(self.paths.effective)["routes"]
        self.assertEqual(routes["coder"]["values"], {
            "model": "user-config-choice", "reasoning_effort": None, "context_tier": None})
        self.assertEqual(routes["coder.small_casual"]["values"]["model"], "small-pin")
        self.assertEqual(routes["reviewer.stronger"]["values"]["reasoning_effort"], "max")
        self.assertEqual(Path(self.paths.user_config).read_bytes(), config_before)
        self.assertNotIn("coder", self.read_json(self.paths.state)["managed_settings"])
        instruction = Path(self.paths.instructions).read_text()
        self.assertIn("| `coder` | `user-config-choice` | OMIT | OMIT |", instruction)
        self.assertNotIn("native-choice", instruction)
        rc, out, err = self.cli("status", "--instructions")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.live_routes(out)["coder"], {
            "model": "user-config-choice", "reasoning_effort": "OMIT", "context_tier": "OMIT"})

    def test_post_apply_native_edit_becomes_a_pin_and_reset_keeps_it(self):
        self.apply()
        settings = self.read_json(self.paths.settings)
        settings["subagents"]["agents"]["discovery"]["model"] = "human-post-change"
        self.write_json(self.paths.settings, settings)
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Drift:", out)
        self.assertIn("human-post-change", out)
        self.apply()
        self.assertIsNone(self.read_json(self.paths.effective)["routes"]["discovery"]["values"]["model"])
        self.assertNotIn("human-post-change", Path(self.paths.instructions).read_text())
        settings = self.read_json(self.paths.settings)
        settings["subagents"]["agents"]["discovery"]["model"] = "human-second-change"
        self.write_json(self.paths.settings, settings)
        rc, out, err = self.cli("status", "--instructions")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.live_routes(out)["discovery"]["model"], "OMIT")
        self.assertNotIn("human-post-change", out)
        self.assertNotIn("human-second-change", out)
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("Drift:", out, "another human pin value must not stale a frozen table")
        self.assertIn("human-second-change", out)
        rc, out, err = self.cli("reset", "--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read_json(self.paths.settings)["subagents"]["agents"]["discovery"],
                         {"model": "human-second-change"})
        state = self.read_json(self.paths.state)
        self.assertEqual(state["overrides"], {})
        self.assertEqual(state["managed_settings"], {})
        self.assertIsNone(state["instructions_sha256"])
        self.assertEqual(state["history"][-1]["action"], "reset")
        self.assertFalse(Path(self.paths.instructions).exists())
        self.assertFalse(Path(self.paths.effective).exists())

    def test_new_user_config_pin_removes_conflicting_previously_managed_native_model(self):
        self.apply()
        self.config["pins"] = {"coder": {"model": "later-user-pin"}}
        self.save_config()
        self.apply()
        route = self.read_json(self.paths.effective)["routes"]["coder"]["values"]
        self.assertEqual(route["model"], "later-user-pin")
        native = self.read_json(self.paths.settings)["subagents"]["agents"].get("coder", {})
        self.assertIn(native.get("model"), (None, route["model"]),
                      "a stale tool-written native model must not conflict with its effective table")

    def test_later_user_config_pins_sync_all_managed_native_fields_and_survive_reset(self):
        self.apply()
        pins = {"model": "later-user-pin", "reasoning_effort": "high", "context_tier": "long_context"}
        self.config["pins"] = {"coder": pins}
        self.save_config()
        config_before = Path(self.paths.user_config).read_bytes()
        self.apply()
        expected_native = {native: pins[field] for field, native in self.mod.SETTINGS_FIELDS}
        for stage in ("after-apply", "after-reset"):
            with self.subTest(stage=stage):
                if stage == "after-reset":
                    rc, out, err = self.cli("reset", "--yes")
                    self.assertEqual(rc, 0, out + err)
                    self.assertEqual(self.read_json(self.paths.state)["overrides"], {})
                self.assertEqual(self.read_json(self.paths.settings)["subagents"]["agents"]["coder"],
                                 expected_native)
                self.assertEqual(self.read_json(self.paths.state)["managed_settings"]["coder"],
                                 expected_native)
                self.assertEqual(self.read_json(self.paths.effective)["routes"]["coder"]["values"], pins)
                self.assertEqual(Path(self.paths.user_config).read_bytes(), config_before)
                self.assertEqual(self.read_json(self.paths.settings)["theme"], "user-theme")

    def test_reset_is_dry_by_default_then_restores_only_managed_fields_and_keeps_backups(self):
        self.apply()
        before = self.routing_bytes()
        backups_before = {str(p): p.read_bytes() for p in Path(self.paths.backups).rglob("*.bak")}
        self.assertTrue(backups_before)
        self.assertIn(self.initial_settings, [json.loads(data) for data in backups_before.values()])
        rc, out, err = self.cli("reset")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("Re-run with --yes", out)
        self.assertEqual(self.routing_bytes(), before)
        rc, out, err = self.cli("reset", "--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read_json(self.paths.settings), self.initial_settings)
        self.assertFalse(Path(self.paths.instructions).exists())
        self.assertFalse(Path(self.paths.effective).exists())
        self.assertEqual(self.read_json(self.paths.state)["overrides"], {})
        for path, data in backups_before.items():
            self.assertEqual(Path(path).read_bytes(), data)
        self.assertEqual(self.cli("reset", "--yes")[0], 3)

    def test_unmanaged_or_edited_instruction_and_symlink_targets_are_refused(self):
        for target in ("foreign", "settings-symlink", "instruction-symlink", "directory-symlink"):
            with self.subTest(target=target):
                saved_settings = Path(self.paths.settings).read_bytes()
                instr_dir = Path(self.paths.instructions_dir)
                instr_dir.mkdir(exist_ok=True)
                external = self.home / ("external-" + target)
                if target == "foreign":
                    self.write(self.paths.instructions, "human maintained instruction")
                elif target == "settings-symlink":
                    external.write_bytes(saved_settings)
                    Path(self.paths.settings).unlink()
                    Path(self.paths.settings).symlink_to(external)
                elif target == "instruction-symlink":
                    external.write_text("external user instruction")
                    Path(self.paths.instructions).symlink_to(external)
                else:
                    external.mkdir()
                    instr_dir.rmdir()
                    instr_dir.symlink_to(external, target_is_directory=True)
                before = self.routing_bytes()
                rc, out, err = self.cli("propose")
                self.assertEqual(rc, 4, out + err)
                self.assertEqual(before, self.routing_bytes())
                self.assertFalse(Path(self.paths.proposal).exists())
                if target == "settings-symlink":
                    self.assertEqual(external.read_bytes(), saved_settings)
                    Path(self.paths.settings).unlink()
                    Path(self.paths.settings).write_bytes(saved_settings)
                elif target == "directory-symlink":
                    instr_dir.unlink()
                else:
                    Path(self.paths.instructions).unlink()

    def test_edited_tool_instruction_is_never_overwritten_by_reset(self):
        self.apply()
        self.write(self.paths.instructions, Path(self.paths.instructions).read_bytes() + b"\nHuman edit\n")
        before = self.routing_bytes()
        rc, out, err = self.cli("reset", "--yes")
        self.assertEqual(rc, 4, out + err)
        self.assertEqual(self.routing_bytes(), before)

    def test_malformed_settings_are_not_replaced_or_reported_as_success(self):
        self.write(self.paths.settings, b'{"subagents": invalid}')
        before = self.routing_bytes()
        rc, out, err = self.cli("propose")
        self.assertEqual(rc, 4, out + err)
        self.assertIn("cannot be read safely", out + err)
        self.assertEqual(before, self.routing_bytes())

    def test_apply_rederives_frozen_proposal_from_cache_without_network(self):
        proposal_id = self.proposal()
        proposal = self.read_json(self.paths.proposal)
        self.mod.PRICING_FETCHER = mock.Mock(side_effect=AssertionError("apply fetched"))
        with mock.patch.object(self.mod, "_fetch_capability_bytes", side_effect=AssertionError("apply fetched")), \
                mock.patch.object(self.mod, "evaluate", wraps=self.mod.evaluate) as evaluate:
            rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 0, out + err)
        evaluate.assert_called_once()
        state = self.read_json(self.paths.state)
        applied = state["overrides"]
        self.assertEqual({key: {field: rec[field] for field in self.mod.FIELDS}
                          for key, rec in applied.items()},
                         {key: dict(value, model=CHEAP) for key, value in self.defaults.items()})
        for rec in applied.values():
            self.assertEqual(rec["snapshot_id"], proposal["snapshot_id"])
            self.assertEqual(rec["applied_at"], self.mod.now_iso(NOW))
        self.assertEqual(state["history"][-1]["proposal_id"], proposal_id)


class TestLiveInstructions(RefreshCase):
    def test_live_native_fields_omit_on_every_read_without_catalog_pricing_or_writes(self):
        settings = copy.deepcopy(self.initial_settings)
        settings["subagents"]["agents"]["coder"] = {
            "model": "native-first", "effortLevel": "max", "contextTier": "inherit"}
        self.write_json(self.paths.settings, settings)
        self.catalog_path.unlink()
        with mock.patch.object(self.mod, "load_pricing_module", side_effect=AssertionError("pricing read")), \
                mock.patch.object(self.mod, "load_catalog", side_effect=AssertionError("catalog read")), \
                mock.patch.object(self.mod, "read_settings", wraps=self.mod.read_settings) as reads:
            for model, effort, context in (
                ("native-first", "max", "inherit"), ("native-second", "inherit", "long_context")
            ):
                settings["subagents"]["agents"]["coder"] = {
                    "model": model, "effortLevel": effort, "contextTier": context}
                self.write_json(self.paths.settings, settings)
                before = self.routing_bytes()
                rc, out, err = self.cli("status", "--instructions")
                self.assertEqual(rc, 0, out + err)
                routes = self.live_routes(out)
                self.assertEqual(routes["coder"], {
                    "model": "OMIT", "reasoning_effort": "OMIT", "context_tier": "OMIT"})
                for key in self.defaults:
                    if key != "coder":
                        self.assertEqual(routes[key], self.defaults[key])
                self.assertNotIn("native-first", out)
                self.assertNotIn("native-second", out)
                self.assertEqual(self.routing_bytes(), before)
            self.assertEqual(reads.call_count, 2)
        self.assertEqual(self.fetches, [])
        self.assertFalse(Path(self.paths.state).exists())
        self.assertFalse(Path(self.paths.instructions).exists())
        self.assertFalse(Path(self.paths.effective).exists())
        self.assertFalse(Path(self.paths.lock).exists())

    def test_native_pins_alone_never_generate_or_claim_drift(self):
        settings = copy.deepcopy(self.initial_settings)
        settings["subagents"]["agents"]["coder"] = {
            "model": "native-only", "effortLevel": "inherit", "contextTier": "default"}
        self.write_json(self.paths.settings, settings)
        self.markdown = self.make_markdown(cheap=9.5)
        before = self.routing_bytes()
        rc, out, err = self.cli("propose")
        self.assertEqual(rc, 3, out + err)
        self.assertEqual(self.routing_bytes(), before)
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("native-only", out)
        self.assertNotIn("Drift:", out)
        self.assertFalse(Path(self.paths.instructions).exists())
        self.assertFalse(Path(self.paths.effective).exists())

    def test_malformed_settings_refuses_normal_and_live_status_without_guessed_rows(self):
        self.write(self.paths.settings, b'{"subagents": invalid}')
        before = self.routing_bytes()
        for args in (("status",), ("status", "--instructions")):
            with self.subTest(args=args):
                rc, out, err = self.cli(*args)
                self.assertEqual(rc, 4, out + err)
                self.assertNotIn("Current effective routing:", out)
                self.assertNotIn("model=", out)
                self.assertIn("cannot", (out + err).lower())
                if len(args) == 1:
                    self.assertIn("UNAVAILABLE", out)
                self.assertEqual(self.routing_bytes(), before)
        self.assertEqual(self.fetches, [])


class TestJournalTransaction(RefreshCase):
    def test_tampered_journal_refuses_before_any_restore_and_keeps_guided_evidence(self):
        old = Path(self.paths.settings).read_bytes()
        new = b'{"intermediate": true}\n'
        cases = ("arbitrary-target", "outside-backup", "symlink-backup", "string-mode",
                 "negative-mode", "backup-hash", "invalid-hash", "unexpected-field")
        for case in cases:
            with self.subTest(case=case):
                self.write(self.paths.settings, new)
                backup = Path(self.paths.backups) / case / "settings.bak"
                foreign = self.home / ("foreign-" + case)
                outside_backup = self.home / ("outside-backup-" + case)
                self.write(backup, old)
                self.write(foreign, b"foreign user file\n")
                self.write(outside_backup, old)
                entry = {
                    "path": self.paths.settings, "backup": str(backup), "mode": 0o640,
                    "old_sha256": self.mod.sha256_bytes(old),
                    "new_sha256": self.mod.sha256_bytes(new),
                }
                entries = [entry]
                if case == "arbitrary-target":
                    entries.append(dict(
                        entry, path=str(foreign),
                        new_sha256=self.mod.sha256_bytes(foreign.read_bytes())))
                elif case == "outside-backup":
                    entry["backup"] = str(outside_backup)
                elif case == "symlink-backup":
                    backup.unlink()
                    backup.symlink_to(outside_backup)
                elif case == "string-mode":
                    entry["mode"] = "0600"
                elif case == "negative-mode":
                    entry["mode"] = -1
                elif case == "backup-hash":
                    backup.write_bytes(b"tampered backup")
                elif case == "invalid-hash":
                    entry["new_sha256"] = "not-a-sha256"
                else:
                    entry["unexpected"] = True
                self.write_json(self.paths.journal, {
                    "schema_version": 1, "kind": self.mod.JOURNAL_KIND, "entries": entries})
                journal_before = Path(self.paths.journal).read_bytes()
                before = self.routing_bytes()
                rc, out, err = self.cli("propose")
                self.assertEqual(rc, 4, out + err)
                self.assertIn("nothing was restored or deleted", err)
                self.assertIn("inspect", err)
                self.assertIn(self.paths.journal, err)
                self.assertIn(self.paths.backups, err)
                self.assertEqual(self.routing_bytes(), before)
                self.assertEqual(Path(self.paths.journal).read_bytes(), journal_before)
                self.assertEqual(foreign.read_bytes(), b"foreign user file\n")
                self.assertEqual(outside_backup.read_bytes(), old)
        self.assertEqual(self.fetches, [])

    def test_valid_journal_recovers_new_target_with_null_backup_and_restores_existing_target(self):
        old = Path(self.paths.settings).read_bytes()
        new = b'{"intermediate": true}\n'
        backup = Path(self.paths.backups) / "legitimate-crash" / "settings.bak"
        self.write(backup, old)
        self.write(self.paths.settings, new)
        self.write(self.paths.effective, b'{"partial": true}\n')
        self.write_json(self.paths.journal, {
            "schema_version": 1, "kind": self.mod.JOURNAL_KIND, "entries": [
                {"path": self.paths.settings, "backup": str(backup), "mode": 0o640,
                 "old_sha256": self.mod.sha256_bytes(old), "new_sha256": self.mod.sha256_bytes(new)},
                {"path": self.paths.effective, "backup": None, "mode": 0o600,
                 "old_sha256": None, "new_sha256": self.mod.file_sha256(self.paths.effective)},
            ],
        })
        self.seed_cache()
        rc, out, err = self.cli("propose", "--offline")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("rolled back an interrupted apply", out)
        self.assertEqual(Path(self.paths.settings).read_bytes(), old)
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.settings).st_mode), 0o640)
        self.assertFalse(Path(self.paths.effective).exists())
        self.assertFalse(Path(self.paths.journal).exists())
        self.assertEqual(backup.read_bytes(), old)

    def test_each_replace_failure_rolls_back_bytes_and_permissions(self):
        for path in (self.paths.settings, self.paths.instructions, self.paths.effective, self.paths.state):
            with self.subTest(failed_target=path):
                proposal_id = self.proposal()
                before = self.routing_bytes()
                original_replace = self.mod.os.replace
                failed = []

                def fail_once(src, dest):
                    if os.fspath(dest) == path and not failed:
                        failed.append(dest)
                        raise OSError("injected replace failure at " + path)
                    return original_replace(src, dest)

                with mock.patch.object(self.mod.os, "replace", side_effect=fail_once):
                    rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
                self.assertEqual(rc, 2, out + err)
                self.assertTrue(failed)
                self.assertIn("rolled back", err)
                self.assertEqual(self.routing_bytes(), before)
                self.assertEqual(stat.S_IMODE(os.stat(self.paths.settings).st_mode), 0o640)
                self.assertFalse(Path(self.paths.journal).exists())
                self.assertEqual(list(Path(self.paths.copilot_home).rglob(".tmp-*")), [])

    def test_concurrent_edit_between_replacements_is_preserved_and_previous_file_rolled_back(self):
        proposal_id = self.proposal()
        before = Path(self.paths.settings).read_bytes()
        original_write = self.mod.atomic_write_bytes

        def mutate_after_first(path, data, mode=0o600):
            result = original_write(path, data, mode)
            if path == self.paths.settings:
                self.write(self.paths.instructions, "concurrent user instruction")
            return result

        with mock.patch.object(self.mod, "atomic_write_bytes", side_effect=mutate_after_first):
            rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 4, out + err)
        self.assertEqual(Path(self.paths.settings).read_bytes(), before)
        self.assertEqual(Path(self.paths.instructions).read_text(), "concurrent user instruction")
        self.assertFalse(Path(self.paths.state).exists())

    def test_process_crash_leaves_recoverable_journal_and_next_propose_restores_all_targets(self):
        proposal_id = self.proposal()
        before = self.routing_bytes()
        # Real abrupt process exit, not an exception which commit() catches.
        child = (
            "import importlib.util,os,sys\n"
            "s=importlib.util.spec_from_file_location('r',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
            "m.load_pricing_module().SUPPORT_DIR=sys.argv[4]\n"
            "original=m.atomic_write_bytes\n"
            "def crash(path,data,mode=0o600):\n"
            " original(path,data,mode)\n"
            " if path==m.Paths().instructions: os._exit(77)\n"
            "m.atomic_write_bytes=crash\n"
            "sys.exit(m.main(['apply','--proposal-id',sys.argv[2]],now=float(sys.argv[3])))\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", child, str(HELPER), proposal_id, str(NOW),
             self.pricing.SUPPORT_DIR],
            env=dict(os.environ, COPILOT_TASK_REPORT_PRICING_FETCH="0"),
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 77, result.stdout + result.stderr)
        self.assertTrue(Path(self.paths.journal).exists())
        self.assertNotEqual(before, self.routing_bytes())
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("interrupted apply", out)
        self.assertTrue(Path(self.paths.journal).exists(), "status must not recover/write")
        rc, out, err = self.cli("propose", "--offline")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("rolled back an interrupted apply", out)
        self.assertEqual(self.routing_bytes(), before)
        self.assertFalse(Path(self.paths.journal).exists())
        self.assertEqual(stat.S_IMODE(os.stat(self.paths.settings).st_mode), 0o640)

    def test_recovery_refuses_to_clobber_a_post_crash_human_edit(self):
        target = self.paths.settings
        old = Path(target).read_bytes()
        backup = Path(self.paths.backups) / "crashed" / "settings.bak"
        self.write(backup, old)
        self.write(target, "human edited after crash")
        self.write_json(self.paths.journal, {
            "schema_version": 1, "kind": self.mod.JOURNAL_KIND, "entries": [{
                "path": target, "backup": str(backup), "mode": 0o640,
                "old_sha256": self.mod.sha256_bytes(old),
                "new_sha256": self.mod.sha256_bytes(b"tool intermediate"),
            }],
        })
        rc, out, err = self.cli("propose")
        self.assertEqual(rc, 4, out + err)
        self.assertIn("changed since", err)
        self.assertEqual(Path(target).read_text(), "human edited after crash")
        self.assertTrue(Path(self.paths.journal).exists())

    def test_backup_creation_and_journal_creation_fail_before_any_target_write(self):
        for operation in ("backup", "journal"):
            with self.subTest(operation=operation):
                proposal_id = self.proposal()
                before = self.routing_bytes()
                if operation == "backup":
                    patch = mock.patch.object(self.mod.shutil, "copy2", side_effect=OSError("backup denied"))
                else:
                    original = self.mod.atomic_write_bytes

                    def fail_journal(path, data, mode=0o600):
                        if path == self.paths.journal:
                            raise OSError("journal denied")
                        return original(path, data, mode)
                    patch = mock.patch.object(self.mod, "atomic_write_bytes", side_effect=fail_journal)
                with patch:
                    rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
                self.assertEqual(rc, 2, out + err)
                self.assertEqual(before, self.routing_bytes())
                self.assertFalse(Path(self.paths.journal).exists())


class TestProposalSealing(RefreshCase):
    def assert_independent_seal(self, doc, key):
        body = {field: value for field, value in doc.items() if field != "seal"}
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        self.assertEqual(doc["seal"], hmac.new(key, raw, hashlib.sha256).hexdigest())

    def test_key_is_created_only_for_a_frozen_proposal_private_reused_and_not_a_routing_write(self):
        before = self.routing_bytes()
        for command in ("status", "preview"):
            rc, out, err = self.cli(command)
            self.assertEqual(rc, 0, out + err)
            self.assertFalse(Path(self.paths.proposal_key).exists())
            self.assertEqual(self.routing_bytes(), before)
        first_id = self.proposal()
        key_path = Path(self.paths.proposal_key)
        raw_key = key_path.read_bytes()
        self.assertRegex(raw_key, rb"^[0-9a-f]{64}\n$")
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
        self.assert_independent_seal(self.read_json(self.paths.proposal), raw_key.strip())
        self.assertEqual(self.routing_bytes(), before)
        rc, out, err = self.cli("discard", "--proposal-id", first_id)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(key_path.read_bytes(), raw_key)
        self.proposal()
        self.assertEqual(key_path.read_bytes(), raw_key)
        self.assert_independent_seal(self.read_json(self.paths.proposal), raw_key.strip())
        self.assertEqual(self.routing_bytes(), before)

    def test_no_changes_does_not_create_a_key_or_pending_proposal(self):
        self.markdown = self.make_markdown(cheap=9.01)
        before = self.routing_bytes()
        rc, out, err = self.cli("propose")
        self.assertEqual(rc, 3, out + err)
        self.assertFalse(Path(self.paths.proposal_key).exists())
        self.assertFalse(Path(self.paths.proposal).exists())
        self.assertEqual(self.routing_bytes(), before)

    def test_key_creation_failure_never_produces_an_applyable_proposal_or_routing_write(self):
        original_write = self.mod.atomic_write_bytes

        def deny_key(path, data, mode=0o600):
            if path == self.paths.proposal_key:
                raise PermissionError("fixture proposal key creation denied")
            return original_write(path, data, mode)

        before = self.routing_bytes()
        with mock.patch.object(self.mod, "atomic_write_bytes", side_effect=deny_key):
            rc, out, err = self.cli("propose")
        self.assertEqual(rc, 2, err)
        self.assertIn("key creation denied", err)
        self.assertNotIn("PROPOSAL\t", out)
        self.assertFalse(Path(self.paths.proposal_key).exists())
        self.assertFalse(Path(self.paths.proposal).exists())
        self.assertEqual(self.routing_bytes(), before)

    def test_missing_corrupt_changed_or_nonregular_key_refuses_apply_without_recreating_it(self):
        key_path = Path(self.paths.proposal_key)
        for corruption in ("missing", "malformed", "oversized", "changed", "directory"):
            with self.subTest(corruption=corruption):
                proposal_id = self.proposal()
                before = self.routing_bytes()
                key_path.unlink()
                if corruption == "directory":
                    key_path.mkdir()
                    key_before = None
                elif corruption == "missing":
                    key_before = None
                else:
                    raw = {"malformed": b"not-a-key\n", "oversized": b"a" * 257,
                           "changed": b"f" * 64 + b"\n"}[corruption]
                    self.write(key_path, raw)
                    key_before = raw
                rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
                self.assertEqual(rc, 4, err)
                self.assertIn("seal", err)
                self.assertEqual(self.routing_bytes(), before)
                if corruption == "missing":
                    self.assertFalse(key_path.exists())
                elif corruption == "directory":
                    self.assertTrue(key_path.is_dir())
                    key_path.rmdir()
                else:
                    self.assertEqual(key_path.read_bytes(), key_before)
                    key_path.unlink()

    def test_symlink_key_refuses_propose_and_apply_and_preserves_the_external_target(self):
        key_path = Path(self.paths.proposal_key)
        target = self.home / "external-fixture.key"
        target_bytes = b"a" * 64 + b"\n"
        self.write(target, target_bytes)
        before = self.routing_bytes()
        key_path.symlink_to(target)
        rc, out, err = self.cli("propose")
        self.assertEqual(rc, 4, err)
        self.assertIn("symlink", err)
        self.assertNotIn("PROPOSAL\t", out)
        self.assertFalse(Path(self.paths.proposal).exists())
        self.assertEqual(target.read_bytes(), target_bytes)
        self.assertEqual(self.routing_bytes(), before)
        key_path.unlink()
        proposal_id = self.proposal()
        key_path.unlink()
        key_path.symlink_to(target)
        rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 4, err)
        self.assertIn("symlink", err)
        self.assertEqual(target.read_bytes(), target_bytes)
        self.assertTrue(key_path.is_symlink())
        self.assertEqual(self.routing_bytes(), before)

    def test_existing_key_must_be_private_before_it_can_authorize_apply(self):
        proposal_id = self.proposal()
        key_path = Path(self.paths.proposal_key)
        os.chmod(key_path, 0o644)
        before = self.routing_bytes()
        rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        if rc == 0:
            # Repairing permissions is safe too; accepting an exposed key is not.
            self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600,
                             "apply accepted a group/world-readable proposal key without repairing its mode")
        else:
            self.assertIn(rc, (2, 4), err)
            self.assertEqual(self.routing_bytes(), before)

    def test_seal_binds_overrides_selection_outputs_digests_and_metadata(self):
        for field in ("overrides", "selected", "expected", "digests", "route_changes", "snapshot_id", "seal"):
            with self.subTest(field=field):
                proposal_id = self.proposal()
                doc = self.read_json(self.paths.proposal)
                if field == "overrides":
                    doc[field]["coder"] = dict(self.defaults["coder"])
                elif field == "selected":
                    doc[field] = [1]
                elif field in ("expected", "digests"):
                    doc[field]["settings"] = "0" * 64
                elif field == "route_changes":
                    doc[field] = []
                elif field == "snapshot_id":
                    doc[field] = "sha256:" + "0" * 16
                else:
                    doc[field] = "0" * 64
                self.write_json(self.paths.proposal, doc)
                before = self.routing_bytes()
                rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
                self.assertEqual(rc, 4, err)
                self.assertIn("altered", err)
                self.assertEqual(self.routing_bytes(), before)

    def test_json_reformatting_preserves_valid_seal_and_accepts_exact_ttl_boundary(self):
        proposal_id = self.proposal()
        doc = self.read_json(self.paths.proposal)
        key = Path(self.paths.proposal_key).read_bytes().strip()
        self.assert_independent_seal(doc, key)
        self.write(self.paths.proposal, json.dumps(doc, indent=4, sort_keys=True))
        rc, out, err = self.cli("apply", "--proposal-id", proposal_id, now=NOW + 900)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(Path(self.paths.proposal_key).read_bytes().strip(), key)
        self.assertFalse(Path(self.paths.proposal).exists())

    def test_new_propose_rotates_unsafe_key_and_invalidates_old_seal_without_routing_writes(self):
        key_path = Path(self.paths.proposal_key)
        before = self.routing_bytes()
        for corruption in ("writable", "malformed"):
            with self.subTest(corruption=corruption):
                old_id = self.proposal()
                old_doc = Path(self.paths.proposal).read_bytes()
                old_key = key_path.read_bytes()
                if corruption == "writable":
                    os.chmod(key_path, 0o622)
                else:
                    self.write(key_path, b"invalid fixture key\n")
                new_id = self.proposal()
                new_doc = Path(self.paths.proposal).read_bytes()
                new_key = key_path.read_bytes()
                self.assertNotEqual(new_key, old_key)
                self.assertRegex(new_key, rb"^[0-9a-f]{64}\n$")
                self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
                self.assert_independent_seal(json.loads(new_doc), new_key.strip())
                self.assertEqual(self.routing_bytes(), before)
                self.write(self.paths.proposal, old_doc)
                rc, out, err = self.cli("apply", "--proposal-id", old_id)
                self.assertEqual(rc, 4, err)
                self.assertIn("seal", err)
                self.assertEqual(self.routing_bytes(), before)
                self.assertEqual(key_path.read_bytes(), new_key)
                self.write(self.paths.proposal, new_doc)
        rc, out, err = self.cli("apply", "--proposal-id", new_id)
        self.assertEqual(rc, 0, out + err)

    def test_foreign_owner_on_open_descriptor_is_rejected_before_key_content_is_read(self):
        proposal_id = self.proposal()
        key_path = Path(self.paths.proposal_key)
        key_before = key_path.read_bytes()
        inode = key_path.stat().st_ino
        original_fstat = os.fstat

        def foreign_owner(fd):
            st = original_fstat(fd)
            if st.st_ino == inode:
                fields = list(st)
                fields[4] = st.st_uid + 1
                return os.stat_result(fields)
            return st

        before = self.routing_bytes()
        with mock.patch.object(self.mod.os, "fstat", side_effect=foreign_owner), \
                mock.patch.object(self.mod.os, "read", wraps=os.read) as read:
            rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 4, err)
        self.assertIn("not owned by you", err)
        read.assert_not_called()
        self.assertEqual(key_path.read_bytes(), key_before)
        self.assertEqual(self.routing_bytes(), before)

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires POSIX no-follow open")
    def test_symlink_inserted_at_open_is_not_followed_or_read(self):
        proposal_id = self.proposal()
        key_path = Path(self.paths.proposal_key)
        key_before = key_path.read_bytes()
        target = self.home / "late-linked-fixture.key"
        self.write(target, key_before)
        os.chmod(target, 0o600)
        original_open = os.open
        raced = []

        def insert_symlink(path, flags, *args, **kwargs):
            if os.fspath(path) == self.paths.proposal_key:
                self.assertTrue(flags & os.O_NOFOLLOW, "key open must refuse late symlinks")
                key_path.unlink()
                key_path.symlink_to(target)
                raced.append(True)
            return original_open(path, flags, *args, **kwargs)

        before = self.routing_bytes()
        with mock.patch.object(self.mod.os, "open", side_effect=insert_symlink), \
                mock.patch.object(self.mod.os, "read", wraps=os.read) as read:
            rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 4, err)
        self.assertEqual(raced, [True])
        read.assert_not_called()
        self.assertTrue(key_path.is_symlink())
        self.assertEqual(target.read_bytes(), key_before)
        self.assertEqual(self.routing_bytes(), before)

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "requires POSIX FIFO")
    def test_fifo_key_is_refused_without_blocking_read_or_routing_writes(self):
        proposal_id = self.proposal()
        key_path = Path(self.paths.proposal_key)
        key_path.unlink()
        os.mkfifo(key_path, 0o600)
        original_open = os.open

        def nonblocking_open(path, flags, *args, **kwargs):
            if os.fspath(path) == self.paths.proposal_key:
                self.assertTrue(flags & os.O_NONBLOCK, "key open must not block on a nonregular file")
            return original_open(path, flags, *args, **kwargs)

        before = self.routing_bytes()
        with mock.patch.object(self.mod.os, "open", side_effect=nonblocking_open), \
                mock.patch.object(self.mod.os, "read", wraps=os.read) as read:
            rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 4, err)
        self.assertIn("not a regular file", err)
        read.assert_not_called()
        self.assertTrue(stat.S_ISFIFO(key_path.stat().st_mode))
        self.assertEqual(self.routing_bytes(), before)


class TerminalInput(io.StringIO):
    def isatty(self):
        return True


class LiveRefreshCase(RefreshCase):
    """Synthetic values, with the official article-body layout observed
    2026-11-21: HTML wrappers, blank rows, SVG aria labels, footnotes, links,
    extra client columns, and the comparison's Further reading column.
    All transport is injected; no test contacts a public or private service.
    """

    def setUp(self):
        super().setUp()
        self.ids.append(NEW)
        self.doc_facts = {mid: {
            "provider": "OpenAI", "release_status": "GA", "task_area": "Fixture coding",
            "excels_at": "Synthetic fixture tasks", "one_million_context": "Supported",
            "configurable_reasoning": "Supported", "copilot_cli_client": "Included",
        } for mid in self.ids + [DOC_ONLY]}
        self.config["available_models"]["models"] = list(self.ids)
        self.config["verified_capabilities"]["models"][NEW] = {
            "reasoning_levels": ["low", "medium", "high", "xhigh"]}
        self.save_config()
        self.markdown = self.live_prices()
        self.docs_fetches = []
        self.events = []
        self.mod.CAPABILITY_FETCHER = self.fetch_docs

    def fetch(self, url, timeout, max_bytes):
        self.events.append(url)
        return super().fetch(url, timeout, max_bytes)

    def live_prices(self, new=5, cheap=15, new_long=None):
        lines = self.make_markdown(cheap=cheap).decode().splitlines()
        for i, line in enumerate(list(lines)):
            if line.startswith("| %s |" % NEW):
                default = line.replace("$10", "$%s" % new)
                if new_long is not None:
                    default = default.replace("Not applicable", "≤ 272K")
                    lines.append(line.replace("Default | Not applicable", "Long context | > 272K")
                                 .replace("$10", "$%s" % new_long))
                lines[i] = default
        return "\n".join(lines).encode()

    @staticmethod
    def icon(label):
        return '<svg viewBox="0 0 16 16" aria-label="%s"><path d="M1 1"/></svg>' % label

    def pages(self):
        def table(headers, cells):
            lines = ["<div class=\"ghd-tool rowheaders\">", "",
                     "| " + " | ".join(headers) + " |",
                     "| " + " | ".join("---" for _ in headers) + " |"]
            for row in cells:
                lines += ["| " + " | ".join("" for _ in headers) + " |",
                          "| " + " | ".join(row) + " |"]
            return "\n".join(lines + ["", "</div>", ""])

        models = sorted(self.doc_facts)
        name = lambda mid: "GPT-6.2 Sol[^fixture]" if mid == NEW else mid
        supported = "## Supported AI models in Copilot\n\n" + table(
            ["Model name", "Provider", "Release status"],
            [[name(mid), self.doc_facts[mid]["provider"], self.doc_facts[mid]["release_status"]]
             for mid in models])
        supported += "\n## Models with extended capabilities\n\n" + table(
            ["Model", "1 million token context window", "Configurable reasoning"],
            [[name(mid), self.icon(self.doc_facts[mid]["one_million_context"]),
              self.icon(self.doc_facts[mid]["configurable_reasoning"])] for mid in models])
        supported += "\n## Supported AI models per client\n\n" + table(
            ["Model", "GitHub.com", "Copilot CLI", "Visual Studio Code"],
            [[name(mid), self.icon("Included"), self.icon(self.doc_facts[mid]["copilot_cli_client"]),
              self.icon("Included")] for mid in models])
        comparison = "## Recommended models by task\n\n" + table(
            ["Model", "Task area", "Excels at (primary use case)", "Further reading"],
            [[name(mid), self.doc_facts[mid]["task_area"], self.doc_facts[mid]["excels_at"],
              "[Fixture model card](https://docs.github.com/fixture)"] for mid in models])
        return {"supported_models": supported.encode(), "model_comparison": comparison.encode()}

    def fetch_docs(self, url, timeout, max_bytes):
        self.docs_fetches.append((url, timeout, max_bytes))
        self.events.append(url)
        for src in self.mod.CAPABILITY_SOURCES:
            if url == self.mod.capability_url(src):
                return self.pages()[src["key"]]
        raise AssertionError("unexpected source: " + url)

    def seed_docs(self, ages=None):
        cache = self.mod._blank_capability_cache()
        pages = self.pages()
        for src in self.mod.CAPABILITY_SOURCES:
            epoch = NOW - (ages or {}).get(src["key"], 0)
            raw = pages[src["key"]]
            cache["sources"][src["key"]] = {
                "url": self.mod.capability_url(src), "public_url": self.mod.capability_public_url(src),
                "fetched_at": self.mod.now_iso(epoch), "fetched_epoch": epoch,
                "content_sha256": self.mod.sha256_bytes(raw),
                "parser_version": self.mod.CAPABILITY_PARSER_VERSION,
                "sections": self.mod.parse_capability_markdown(
                    raw.decode(), src, self.pricing.normalize_official_model_name),
            }
        self.write_json(self.paths.capabilities, cache)
        return cache

    def context(self, ages=None):
        self.seed_cache()
        self.seed_docs(ages)
        return self.mod.build_context(self.paths, NOW)

    def rows(self, ctx):
        return {r["key"]: r for r in self.mod.evaluate(ctx)}

    def verified_doc(self, models=None, date="2026-11-16", complete=True):
        return {
            "schema_version": 1, "kind": self.mod.VERIFIED_KIND, "verified_on": date,
            "recorded_at": self.mod.now_iso(NOW),
            "source": "fixture: user attested in /model", "complete_list": complete,
            "models": models if models is not None else {NEW: {}},
        }

    def record(self, *answers):
        with mock.patch.object(self.mod.sys, "stdin", TerminalInput("\n".join(answers) + "\n")):
            return self.cli("record-verified")

    def premium(self, cheap=5):
        self.markdown = self.live_prices(new=12, cheap=cheap, new_long=18)
        for mid in {v["model"] for v in self.defaults.values()}:
            self.doc_facts[mid]["copilot_cli_client"] = "Not included"
        self.seed_cache()
        self.seed_docs()

    def numbered(self, proposal_id, route, model=NEW):
        doc = self.read_json(self.paths.proposal)
        self.assertEqual(doc["proposal_id"], proposal_id)
        return next(a["n"] for a in doc["alternatives"]
                    if a["route"] == route and a["values"]["model"] == model)


class TestLiveCapabilityRefresh(LiveRefreshCase):
    def test_one_p_refreshes_prices_and_both_pages_with_provenance_and_shared_five_min_reuse(self):
        self.seed_cache(age=600)
        self.seed_docs({key: 600 for key in self.pages()})
        before = self.routing_bytes()
        config_before = Path(self.paths.user_config).read_bytes()
        catalog_before = self.catalog_path.read_bytes()
        rc, out, err = self.cli("propose")
        self.assertEqual(rc, 0, out + err)
        urls = [self.pricing.OFFICIAL_PRICING_API_URL] + [
            self.mod.capability_url(src) for src in self.mod.CAPABILITY_SOURCES]
        self.assertEqual(self.events, urls)
        for url, timeout, max_bytes in self.docs_fetches:
            self.assertIn(url, urls[1:])
            self.assertEqual((timeout, max_bytes), (10, 2_000_000))
        cache = self.read_json(self.paths.capabilities)
        for src in self.mod.CAPABILITY_SOURCES:
            rec = cache["sources"][src["key"]]
            self.assertEqual(rec["fetched_epoch"], NOW)
            self.assertEqual(rec["content_sha256"], self.mod.sha256_bytes(self.pages()[src["key"]]))
            self.assertEqual(rec["url"], self.mod.capability_url(src))
            self.assertTrue(all(s["status"] == "ok" for s in rec["sections"].values()))
        self.assertEqual(self.routing_bytes(), before)
        self.assertEqual(Path(self.paths.user_config).read_bytes(), config_before)
        self.assertEqual(self.catalog_path.read_bytes(), catalog_before)
        self.assertIn(NEW, out)
        self.assertEqual(self.cli("propose", now=NOW + 299)[0], 0)
        self.assertEqual(self.events, urls)
        self.assertEqual(self.cli("propose", now=NOW + 300)[0], 0)
        self.assertEqual(self.events, urls + urls)

    def test_offline_and_kill_switches_control_only_the_documented_transports(self):
        for args, env, price_calls in (
            (("preview", "--offline"), {}, 0),
            (("preview",), {"COPILOT_TASK_REPORT_PRICING_FETCH": "0"}, 0),
            (("preview",), {"COPILOT_MODEL_REFRESH_DOCS_FETCH": "0"}, 1),
        ):
            with self.subTest(args=args, env=env):
                self.seed_cache(age=600)
                self.seed_docs({key: 600 for key in self.pages()})
                self.fetches.clear()
                self.docs_fetches.clear()
                with mock.patch.dict(os.environ, env):
                    rc, out, err = self.cli(*args)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(len(self.fetches), price_calls)
                self.assertEqual(self.docs_fetches, [])

    def test_new_documented_model_absent_static_catalog_can_replace_even_expired_catalog(self):
        self.catalog["expires_on"] = "2026-11-15"
        self.save_catalog()
        before = self.catalog_path.read_bytes()
        ctx = self.context()
        self.assertNotIn(NEW, ctx["catalog"]["models"])
        self.assertTrue(ctx["facts"][NEW]["all_live"])
        for key, row in self.rows(ctx).items():
            self.assertEqual(row["proposed"], dict(self.defaults[key], model=NEW))
            self.assertIn("current official docs", " ".join(row["evidence"]))
        self.assertEqual(self.catalog_path.read_bytes(), before)

    def test_every_source_of_known_models_is_visible_with_honest_unknown_price_and_availability(self):
        catalog_only = "fixture-catalog-only"
        self.catalog["models"][catalog_only] = copy.deepcopy(self.catalog["models"][CHEAP])
        self.save_catalog()
        self.config["available_models"]["models"].append(USER_ONLY)
        self.save_config()
        settings = copy.deepcopy(self.initial_settings)
        settings["subagents"]["agents"]["coder"] = {"model": "fixture-current-only"}
        self.write_json(self.paths.settings, settings)
        ctx = self.context()
        expected = set(self.ids) | {DOC_ONLY, USER_ONLY, UNKNOWN, catalog_only, "fixture-current-only"}
        self.assertEqual(set(ctx["universe"]), expected)
        rc, out, err = self.cli("preview", "--offline")
        self.assertEqual(rc, 0, out + err)
        section = out.split("All models in view", 1)[1].split("Optional alternatives", 1)[0]
        for mid in expected:
            self.assertIn("  " + mid, section)
        self.assertIn("price unknown (not in the official pricing)", section)
        self.assertIn("NOT your account's model list", out)
        self.assertFalse(ctx["facts"][UNKNOWN]["assessed"])
        self.assertFalse(ctx["facts"][USER_ONLY]["assessed"])
        self.assertEqual(self.rows(ctx)["coder"]["reason"].split(":")[0], "kept")

    def test_unverified_live_model_is_listed_and_actionable_but_never_assigned(self):
        self.config["available_models"]["models"].remove(NEW)
        self.config["verified_capabilities"]["models"].pop(NEW)
        self.save_config()
        ctx = self.context()
        self.assert_no_changes(self.rows(ctx))
        rc, out, err = self.cli("propose", "--offline")
        self.assertEqual(rc, 3, out + err)
        self.assertIn(NEW, out)
        self.assertIn("VERIFY\tneeded", out)
        self.assertIn("not verified", self.mod._availability_text(NEW, ctx))

    def test_exact_levels_are_per_model_not_inferred_from_generic_live_flag(self):
        self.config["verified_capabilities"]["models"][NEW]["reasoning_levels"] = ["low"]
        self.save_config()
        ctx = self.context()
        for key, row in self.rows(ctx).items():
            if self.defaults[key]["reasoning_effort"] == "low":
                self.assertEqual(row["proposed"], dict(self.defaults[key], model=NEW))
            else:
                self.assertIsNone(row["proposed"], row)
                self.assertIn("not offered", repr(row["blocked"]))
        self.config["verified_capabilities"]["models"].pop(NEW)
        self.save_config()
        ctx = self.mod.build_context(self.paths, NOW)
        self.assertTrue(ctx["facts"][NEW]["configurable_reasoning"])
        self.assert_no_changes(self.rows(ctx), "generic configurable-reasoning")

    def test_per_client_svg_values_use_cli_column_not_another_clients_inclusion(self):
        for label, value, eligible in (
            ("Included", "included", True), ("Not included", "not_included", False),
            ("Yes", None, False), ("", None, False),
        ):
            with self.subTest(label=label):
                self.doc_facts[NEW]["copilot_cli_client"] = label
                ctx = self.context()
                self.assertEqual(ctx["facts"][NEW]["copilot_cli_client"], value)
                self.assertEqual(self.rows(ctx)["tester"]["proposed"] is not None, eligible)
        cache = self.seed_docs()
        del cache["sources"]["supported_models"]["sections"]["clients"]["models"][NEW]
        self.write_json(self.paths.capabilities, cache)
        ctx = self.mod.build_context(self.paths, NOW)
        self.assertEqual(ctx["facts"][NEW]["copilot_cli_client"], "not_listed")
        self.assert_no_changes(self.rows(ctx), "Copilot CLI")

    def test_conflicting_rows_and_changed_layout_never_guess_or_assign(self):
        src = self.mod.CAPABILITY_SOURCE_BY_KEY["model_comparison"]
        raw = self.pages()["model_comparison"].decode()
        variants = (
            raw,
            raw.replace("## Recommended models by task", "## Renamed table"),
            raw + "\n## Recommended models by task\n\n" + raw.split("\n", 1)[1],
            raw.replace("| Model | Task area |", "| Model | Undocumented heading |"),
            raw.replace("| GPT-6.2 Sol[^fixture] | Fixture coding | Synthetic fixture tasks |",
                        "| GPT-6.2 Sol[^fixture] | Fixture coding |"),
            raw.replace("\n\n</div>", "\n| GPT-6.2 Sol | Different tasks | Other fixture | none |\n\n</div>"),
        )
        for index, text in enumerate(variants):
            with self.subTest(variant=index):
                sections = self.mod.parse_capability_markdown(text, src)
                cache = self.seed_docs()
                cache["sources"]["model_comparison"]["sections"] = sections
                self.write_json(self.paths.capabilities, cache)
                self.seed_cache()
                ctx = self.mod.build_context(self.paths, NOW)
                self.assertEqual(self.rows(ctx)["tester"]["proposed"] is not None, index == 0)
                if index == 5:
                    self.assertIn(NEW, sections["tasks"]["conflicts"])

    def test_live_change_supersedes_shipped_facts_but_contradictory_local_overlay_fails_closed(self):
        self.catalog["models"][NEW] = dict(self.catalog["models"][CHEAP], copilot_cli_client="not_included")
        self.save_catalog()
        ctx = self.context()
        self.assertIsNotNone(self.rows(ctx)["tester"]["proposed"])
        self.assertIn("catalog said", repr(ctx["facts"][NEW]["notes"]))
        self.config["catalog"] = {
            "reviewed_on": "2026-11-15", "expires_on": "2026-12-15",
            "sources": copy.deepcopy(self.catalog["sources"]),
            "models": {NEW: copy.deepcopy(self.catalog["models"][NEW])},
        }
        self.save_config()
        ctx = self.mod.build_context(self.paths, NOW)
        self.assert_no_changes(self.rows(ctx), "local overlay contradicts")

    def test_source_specific_ttl_inclusive_boundary_and_future_evidence_fail_closed(self):
        for src in self.mod.CAPABILITY_SOURCES:
            for age, usable in ((src["ttl_seconds"], True), (src["ttl_seconds"] + 1, False), (-1, False)):
                with self.subTest(source=src["key"], age=age):
                    ctx = self.context({src["key"]: age})
                    self.assertEqual(ctx["capabilities"]["sources"][src["key"]]["status"] == "fresh", usable)
                    self.assertEqual(self.rows(ctx)["tester"]["proposed"] is not None, usable)
                    if not usable:
                        self.assertEqual(self.rows(ctx)["tester"]["current"], self.defaults["tester"])

    def test_stale_page_only_models_remain_visible_as_unknown_not_silently_dropped(self):
        ctx = self.context({s["key"]: s["ttl_seconds"] + 1 for s in self.mod.CAPABILITY_SOURCES})
        self.assert_no_changes(self.rows(ctx))
        self.assertIn(DOC_ONLY, ctx["universe"], "previously discovered unpriced model must remain visible")
        self.assertFalse(ctx["facts"][DOC_ONLY]["all_live"])

    def test_failed_refresh_keeps_old_copy_and_age_without_renewing_expired_evidence(self):
        old = self.seed_docs({s["key"]: s["ttl_seconds"] + 1 for s in self.mod.CAPABILITY_SOURCES})
        self.seed_cache()
        self.mod.CAPABILITY_FETCHER = mock.Mock(side_effect=OSError("fixture offline"))
        rc, out, err = self.cli("propose")
        self.assertEqual(rc, 3, out + err)
        self.assertIn("REFRESH FAILED", out)
        self.assertIn("stale", out.lower())
        self.assertEqual(self.read_json(self.paths.capabilities)["sources"], old["sources"])
        self.assertFalse(Path(self.paths.state).exists())

    def test_corrupt_unknown_source_parser_version_and_wrong_source_identity_verify_nothing(self):
        base = self.seed_docs()
        variants = [b'{"broken":', b'{"schema_version": 1, "schema_version": 1}']
        for field, value in (
            ("url", "https://docs.github.com/api/article/body?pathname=/en/unrelated"),
            ("public_url", "https://example.invalid/"),
            ("parser_version", 999), ("content_sha256", "not-a-digest"), ("fetched_epoch", True),
        ):
            doc = copy.deepcopy(base)
            doc["sources"]["supported_models"][field] = value
            variants.append(json.dumps(doc).encode())
        doc = copy.deepcopy(base)
        doc["sources"]["other-source"] = doc["sources"]["supported_models"]
        variants.append(json.dumps(doc).encode())
        self.seed_cache()
        for raw in variants:
            with self.subTest(raw=raw[:140]):
                self.write(self.paths.capabilities, raw)
                ctx = self.mod.build_context(self.paths, NOW)
                self.assertTrue(ctx["capabilities"]["warning"])
                self.assert_no_changes(self.rows(ctx))
                self.assertFalse(ctx["facts"][NEW]["all_live"])

    def test_fixed_url_allowlist_rejects_unauthorized_sources_before_transport(self):
        fetch = mock.Mock(side_effect=AssertionError("unauthorized fetch"))
        with mock.patch.object(self.mod, "CAPABILITY_FETCHER", fetch):
            for url in ("http://docs.github.com/api/article/body", "https://example.invalid/",
                        self.mod.capability_url(self.mod.CAPABILITY_SOURCES[0]) + "&extra=1"):
                with self.subTest(url=url), self.assertRaises(self.mod.CapabilityError):
                    self.mod._fetch_capability_bytes(url)
        fetch.assert_not_called()

    def test_https_response_identity_size_and_request_headers_are_checked_offline(self):
        import urllib.request

        url = self.mod.capability_url(self.mod.CAPABILITY_SOURCES[0])
        for status, final, body, headers, succeeds in (
            (200, url, b"abc", {}, True),
            (302, url, b"abc", {}, False),
            (200, "https://example.invalid/", b"abc", {}, False),
            (200, url, b"abcd", {}, False),
            (200, url, b"abc", {"Content-Length": "4"}, False),
        ):
            with self.subTest(status=status, final=final, headers=headers, size=len(body)):
                response = io.BytesIO(body)
                response.status = status
                response.headers = headers
                response.geturl = lambda: final
                opener = mock.Mock()
                opener.open.return_value = response
                with mock.patch.object(urllib.request, "build_opener", return_value=opener):
                    if succeeds:
                        self.assertEqual(self.mod._default_docs_fetch(url, 10, 3), b"abc")
                    else:
                        with self.assertRaises(self.mod.CapabilityError):
                            self.mod._default_docs_fetch(url, 10, 3)
                request = opener.open.call_args.args[0]
                self.assertEqual(request.full_url, url)
                self.assertEqual(request.get_method(), "GET")
                self.assertIsNone(request.data)
                self.assertNotIn("authorization", {k.lower() for k, v in request.header_items()})
                self.assertNotIn("cookie", {k.lower() for k, v in request.header_items()})
                self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)

    def test_only_certificate_verification_failure_uses_verified_curl_fallback(self):
        import ssl
        import urllib.error
        import urllib.request

        url = self.mod.capability_url(self.mod.CAPABILITY_SOURCES[0])
        failures = (
            (urllib.error.URLError(ssl.SSLCertVerificationError(1, "fixture certificate")), True),
            (TimeoutError("fixture timeout"), False),
            (urllib.error.URLError("fixture disconnected"), False),
            (urllib.error.HTTPError(url, 302, "fixture redirect", {}, None), False),
        )
        for error, fallback in failures:
            with self.subTest(error=type(error).__name__, fallback=fallback):
                opener = mock.Mock()
                opener.open.side_effect = error
                with mock.patch.object(urllib.request, "build_opener", return_value=opener), \
                        mock.patch.object(self.mod, "_curl_docs_fetch", return_value=b"fixture") as curl, \
                        contextlib.redirect_stderr(io.StringIO()):
                    if fallback:
                        self.assertEqual(self.mod._default_docs_fetch(url, 10, 20), b"fixture")
                        curl.assert_called_once()
                    else:
                        with self.assertRaises(self.mod.CapabilityError):
                            self.mod._default_docs_fetch(url, 10, 20)
                        curl.assert_not_called()

    def test_curl_fallback_disables_user_curlrc_auth_and_redirect_options(self):
        url = self.mod.capability_url(self.mod.CAPABILITY_SOURCES[0])
        trailer = (self.mod._DOCS_CURL_STATUS_MARKER + "200 " + url).encode()
        proc = mock.Mock(stdout=io.BytesIO(b"fixture" + trailer))
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        with mock.patch.object(subprocess, "Popen", return_value=proc) as popen:
            self.assertEqual(self.mod._curl_docs_fetch(url, 10, 20, "fixture certificate"), b"fixture")
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1], "-q")
        for dangerous in ("-k", "--insecure", "-L", "--location", "-u", "--user", "-b", "--cookie"):
            self.assertNotIn(dangerous, argv)
        self.assertEqual(argv[argv.index("--proto") + 1], "=https")
        self.assertEqual(argv[argv.index("--max-filesize") + 1], "20")
        self.assertEqual(argv[argv.index("--url") + 1], url)
        self.assertFalse(popen.call_args.kwargs["shell"])


class TestCompleteAvailabilityAuthority(LiveRefreshCase):
    def lists(self, config_date, recorded_date, complete=True, recorded_models=None):
        self.config["available_models"] = {"verified_on": config_date, "models": list(self.ids)}
        self.save_config()
        included = set(self.ids) - {NEW} if recorded_models is None else set(recorded_models)
        self.write_json(self.paths.verified, self.verified_doc(
            {mid: {} for mid in sorted(included)}, date=recorded_date, complete=complete))
        return self.context()

    def test_complete_list_excludes_older_same_day_config_and_inclusive_thirty_day_boundary(self):
        for config_date, recorded_date in (
            ("2026-11-15", "2026-11-16"),
            ("2026-11-16", "2026-11-16"),
            ("2026-10-17", "2026-10-17"),
        ):
            with self.subTest(config_date=config_date, recorded_date=recorded_date):
                ctx = self.lists(config_date, recorded_date)
                models, description, usable = ctx["availability"]
                self.assertTrue(usable)
                self.assertEqual(models, set(self.ids) - {NEW})
                self.assertIn("treated as unavailable", description)
                self.assertIn("NOT in your complete", self.mod._availability_text(NEW, ctx))
                self.assertIn(NEW, ctx["universe"], "excluded models remain visible, not authorized")
                self.assert_no_changes(self.rows(ctx))
                policy, why = self.mod.route_policy(
                    "tester", self.defaults, ctx["catalog"], ctx["today"], ctx["facts"])
                self.assertIsNone(why)
                reasons = self.mod.check_candidate(NEW, "tester", self.defaults["tester"], policy, ctx)
                self.assertEqual({code for code, text in reasons}, {self.mod.REASON_AVAILABILITY})

    def test_newer_config_partial_stale_and_future_record_keep_independent_fresh_config_models(self):
        for config_date, recorded_date, complete in (
            ("2026-11-16", "2026-11-15", True),
            ("2026-11-15", "2026-11-16", False),
            ("2026-11-15", "2026-10-16", True),
            ("2026-11-15", "2026-11-17", True),
        ):
            with self.subTest(config_date=config_date, recorded_date=recorded_date, complete=complete):
                ctx = self.lists(config_date, recorded_date, complete=complete)
                self.assertTrue(ctx["availability"][2])
                self.assertEqual(ctx["availability"][0], set(self.ids))
                self.assertIn("verified available to you", self.mod._availability_text(NEW, ctx))
                for key, row in self.rows(ctx).items():
                    self.assertEqual(row["proposed"], dict(self.defaults[key], model=NEW))

    def test_empty_fresh_complete_list_cannot_authorize_a_model_from_older_config(self):
        ctx = self.lists("2026-11-15", "2026-11-16", recorded_models=[])
        self.assertEqual(ctx["availability"][0], set())
        self.assert_no_changes(self.rows(ctx))
        before = self.routing_bytes()
        rc, out, err = self.cli("propose", "--offline")
        self.assertEqual(rc, 3, out + err)
        self.assertNotIn("PROPOSAL\t", out)
        self.assertFalse(Path(self.paths.proposal).exists())
        self.assertEqual(self.routing_bytes(), before)

    def test_complete_list_change_refuses_frozen_apply_and_fresh_proposal_excludes_the_model(self):
        self.lists("2026-11-15", "2026-11-16", complete=False)
        before = self.routing_bytes()
        rc, out, err = self.cli("propose", "--offline")
        self.assertEqual(rc, 0, out + err)
        doc = self.read_json(self.paths.proposal)
        self.assertTrue(all(values["model"] == NEW for values in doc["overrides"].values()))
        record = self.read_json(self.paths.verified)
        record["complete_list"] = True
        self.write_json(self.paths.verified, record)
        rc, out, err = self.cli("apply", "--proposal-id", doc["proposal_id"])
        self.assertEqual(rc, 4, err)
        self.assertIn("verified", err)
        self.assertEqual(self.routing_bytes(), before)
        rc, out, err = self.cli("propose", "--offline")
        self.assertEqual(rc, 3, out + err)
        self.assertNotIn("PROPOSAL\t", out)
        self.assertIn("treated as unavailable", out)
        self.assertEqual(self.routing_bytes(), before)
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.docs_fetches, [])

    def test_strictly_newer_config_can_propose_and_apply_a_model_absent_from_complete_record(self):
        self.lists("2026-11-16", "2026-11-15")
        before = self.routing_bytes()
        config_before = Path(self.paths.user_config).read_bytes()
        record_before = Path(self.paths.verified).read_bytes()
        rc, out, err = self.cli("propose", "--offline")
        self.assertEqual(rc, 0, out + err)
        doc = self.read_json(self.paths.proposal)
        self.assertEqual(self.routing_bytes(), before)
        self.assertEqual(set(doc["overrides"]), set(self.defaults))
        self.assertTrue(all(values["model"] == NEW for values in doc["overrides"].values()))
        rc, out, err = self.cli("apply", "--proposal-id", doc["proposal_id"])
        self.assertEqual(rc, 0, out + err)
        applied = self.read_json(self.paths.state)["overrides"]
        self.assertEqual({key: {field: values[field] for field in self.mod.FIELDS}
                          for key, values in applied.items()},
                         {key: dict(values, model=NEW) for key, values in self.defaults.items()})
        self.assertEqual(Path(self.paths.user_config).read_bytes(), config_before)
        self.assertEqual(Path(self.paths.verified).read_bytes(), record_before)
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.docs_fetches, [])


class TestVerifiedImport(LiveRefreshCase):
    def setUp(self):
        super().setUp()
        self.config = {}
        self.save_config()
        self.context()

    def test_pipe_is_refused_and_every_nonexact_confirmation_is_effect_free(self):
        before = self.routing_bytes()
        config = Path(self.paths.user_config).read_bytes()
        with mock.patch.object(self.mod.sys, "stdin", io.StringIO(NEW + "\ny\nlow\n yes\n")):
            rc, out, err = self.cli("record-verified")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("terminal", err)
        for confirm in ("", "y", "YES", " yes", "yes ", "no"):
            with self.subTest(confirm=confirm):
                rc, out, err = self.record(NEW, "yes", "low high", confirm)
                self.assertEqual(rc, 3, out + err)
                self.assertFalse(Path(self.paths.verified).exists())
                self.assertEqual(self.routing_bytes(), before)
                self.assertEqual(Path(self.paths.user_config).read_bytes(), config)

    def test_exact_yes_records_unknown_models_and_only_entered_levels_without_routing_writes(self):
        before = self.routing_bytes()
        config = Path(self.paths.user_config).read_bytes()
        rc, out, err = self.record(NEW + ", " + USER_ONLY, "yes", "low high", "yes")
        self.assertEqual(rc, 0, out + err)
        doc = self.read_json(self.paths.verified)
        self.assertEqual(doc["models"], {
            NEW: {"reasoning_levels": ["low", "high"], "levels_verified_on": "2026-11-16"},
            USER_ONLY: {},
        })
        self.assertTrue(doc["complete_list"])
        self.assertEqual(self.routing_bytes(), before)
        self.assertEqual(Path(self.paths.user_config).read_bytes(), config)
        self.assertEqual(self.docs_fetches, [])
        self.assertEqual(self.fetches, [])
        ctx = self.mod.build_context(self.paths, NOW)
        self.assertIn(USER_ONLY, ctx["universe"])
        self.assertIn("NOT in your complete", self.mod._availability_text(CHEAP, ctx))
        self.assertFalse(ctx["facts"][USER_ONLY]["assessed"])
        for key, row in self.rows(ctx).items():
            self.assertEqual(row["proposed"] is not None, self.defaults[key]["reasoning_effort"] in ("low", "high"))

    def test_carried_levels_keep_original_date_and_expired_levels_are_not_silently_renewed(self):
        settings = copy.deepcopy(self.initial_settings)
        settings["subagents"]["agents"]["coder"] = {"model": NEW}
        self.write_json(self.paths.settings, settings)
        for old_date, expected in (
            ("2026-10-17", {"reasoning_levels": ["low"], "levels_verified_on": "2026-10-17"}),
            ("2026-10-16", {}),
        ):
            with self.subTest(old_date=old_date):
                self.write_json(self.paths.verified, self.verified_doc({
                    NEW: {"reasoning_levels": ["low"], "levels_verified_on": old_date}}))
                rc, out, err = self.record(NEW, "no", "", "yes")
                self.assertEqual(rc, 0, out + err)
                doc = self.read_json(self.paths.verified)
                self.assertEqual(doc["verified_on"], "2026-11-16")
                self.assertEqual(doc["models"][NEW], expected)

    def test_same_date_reasoning_conflict_is_unknown_and_newer_verified_record_wins(self):
        self.config["verified_capabilities"] = {
            "verified_on": "2026-11-16", "models": {NEW: {"reasoning_levels": ["high"]}}}
        self.save_config()
        for date, expected in (("2026-11-16", None), ("2026-11-15", ["high"])):
            with self.subTest(date=date):
                self.write_json(self.paths.verified, self.verified_doc({
                    NEW: {"reasoning_levels": ["low"], "levels_verified_on": date}}))
                ctx = self.mod.build_context(self.paths, NOW)
                levels, provenance = self.mod.verified_reasoning_levels(NEW, ctx)
                self.assertEqual(levels, expected)
                if expected is None:
                    self.assertIn("disagree", provenance)

    def test_invalid_stale_and_future_records_never_authorize_live_replacements(self):
        for doc in (
            {"kind": "wrong"},
            self.verified_doc(date="2026-10-16"),
            self.verified_doc(date="2026-11-17"),
            self.verified_doc({NEW: {"reasoning_levels": ["xhigh"]}}),
        ):
            with self.subTest(doc=doc):
                self.write_json(self.paths.verified, doc)
                ctx = self.mod.build_context(self.paths, NOW)
                self.assert_no_changes(self.rows(ctx))
                self.assertFalse(ctx["availability"][2])

    def test_concurrent_edit_or_symlink_at_save_is_refused_without_clobbering(self):
        original_ask = self.mod._ask
        human = self.verified_doc({USER_ONLY: {}})

        def concurrent(prompt):
            if prompt.startswith("Type 'yes'"):
                self.write_json(self.paths.verified, human)
            return original_ask(prompt)

        with mock.patch.object(self.mod, "_ask", side_effect=concurrent):
            rc, out, err = self.record(NEW, "no", "high", "yes")
        self.assertEqual(rc, 4, out + err)
        self.assertIn("changed while", err)
        self.assertEqual(self.read_json(self.paths.verified), human)
        Path(self.paths.verified).unlink()
        target = self.home / "human-record.json"
        self.write_json(target, human)
        Path(self.paths.verified).symlink_to(target)
        rc, out, err = self.record(NEW, "no", "high", "yes")
        self.assertEqual(rc, 4, out + err)
        self.assertIn("symlink", err)
        self.assertEqual(self.read_json(target), human)


class TestOptionalSelectionAndEvidence(LiveRefreshCase):
    def test_premium_is_explicit_only_with_documented_relevance_and_both_price_differences(self):
        self.premium()
        before = self.routing_bytes()
        proposal_id = self.proposal()
        doc = self.read_json(self.paths.proposal)
        self.assertEqual(doc["selected"], [])
        self.assertTrue(all(v["model"] == CHEAP for v in doc["overrides"].values()))
        n = self.numbered(proposal_id, "reviewer.stronger")
        ctx = self.mod.build_context(self.paths, NOW)
        rows = self.mod.evaluate(ctx)
        self.mod.number_alternatives(rows)
        rendered = "\n".join(self.mod.render_alternatives(ctx, rows))
        self.assertIn("official differentiator", rendered)
        self.assertIn("official per-client table", rendered)
        self.assertIn("+$2.00/1M, +20%", rendered)
        self.assertIn("+$8.00/1M, +80%", rendered)
        self.assertIn("NOT a measured, equal or better quality claim", rendered)
        self.assertNotIn("Synthetic fixture tasks", rendered)
        with mock.patch.object(self.mod, "_fetch_capability_bytes", side_effect=AssertionError("select fetched")), \
                mock.patch.object(self.mod, "refresh_pricing", side_effect=AssertionError("select fetched")):
            rc, out, err = self.cli("select", "--proposal-id", proposal_id, "--choose", str(n), now=NOW + 1)
        self.assertEqual(rc, 0, out + err)
        selected = self.read_json(self.paths.proposal)
        self.assertNotEqual(selected["proposal_id"], proposal_id)
        self.assertNotEqual(selected["seal"], doc["seal"])
        self.assertEqual(selected["created_epoch"], doc["created_epoch"])
        self.assertEqual(selected["expires_epoch"], doc["expires_epoch"])
        self.assertEqual(selected["selected"], [n])
        self.assertEqual(selected["overrides"]["reviewer.stronger"],
                         dict(self.defaults["reviewer.stronger"], model=NEW))
        self.assertIn("costs MORE", out)
        self.assertEqual(self.routing_bytes(), before)
        rc, out, err = self.cli("apply", "--proposal-id", selected["proposal_id"], now=NOW + 1)
        self.assertEqual(rc, 0, out + err)
        state = self.read_json(self.paths.state)
        applied = state["overrides"]
        self.assertEqual({key: {field: rec[field] for field in self.mod.FIELDS}
                          for key, rec in applied.items()}, selected["overrides"])
        for rec in applied.values():
            self.assertEqual(rec["snapshot_id"], selected["snapshot_id"])
            self.assertEqual(rec["applied_at"], self.mod.now_iso(NOW + 1))
        self.assertEqual(state["history"][-1]["proposal_id"], selected["proposal_id"])
        instruction = Path(self.paths.instructions).read_text()
        self.assertNotIn("Synthetic fixture tasks", instruction)
        self.assertNotIn("docs.github.com", instruction)
        self.assertNotIn("12.00", instruction)

    def test_tampered_frozen_alternative_is_refused_by_select_without_routing_or_key_changes(self):
        self.premium()
        proposal_id = self.proposal()
        n = self.numbered(proposal_id, "tester")
        doc = self.read_json(self.paths.proposal)
        next(a for a in doc["alternatives"] if a["n"] == n)["values"]["model"] = "fixture-forged-premium"
        self.write_json(self.paths.proposal, doc)
        before = self.routing_bytes()
        key_before = Path(self.paths.proposal_key).read_bytes()
        rc, out, err = self.cli("select", "--proposal-id", proposal_id, "--choose", str(n))
        self.assertEqual(rc, 4, err)
        self.assertIn("altered", err)
        self.assertEqual(self.routing_bytes(), before)
        self.assertEqual(Path(self.paths.proposal_key).read_bytes(), key_before)

    def test_select_refuses_unsafe_key_without_repairing_it_or_writing_routing(self):
        self.premium()
        proposal_id = self.proposal()
        n = self.numbered(proposal_id, "tester")
        key_path = Path(self.paths.proposal_key)
        key_before = key_path.read_bytes()
        os.chmod(key_path, 0o644)
        before = self.routing_bytes()
        rc, out, err = self.cli("select", "--proposal-id", proposal_id, "--choose", str(n))
        self.assertEqual(rc, 4, err)
        self.assertIn("not trusted", err)
        self.assertEqual(key_path.read_bytes(), key_before)
        self.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o644)
        self.assertEqual(self.routing_bytes(), before)

    def test_newness_category_price_and_excels_at_alone_do_not_offer_an_alternative(self):
        self.markdown = self.live_prices(new=12, cheap=11)
        ctx = self.context()
        rows = self.rows(ctx)
        self.assert_no_changes(rows)
        self.assertTrue(all(not r["alternatives"] for r in rows.values()))
        self.assertIn("no documented official differentiator", repr(rows["tester"]["considered"]))

    def test_optional_only_noop_proposal_decline_never_changes_baseline(self):
        self.premium(cheap=11)
        before = self.routing_bytes()
        rc, out, err = self.cli("propose", "--offline")
        self.assertEqual(rc, 3, out + err)
        doc = self.read_json(self.paths.proposal)
        self.assertTrue(doc["alternatives"])
        self.assertEqual(doc["overrides"], {})
        self.assertEqual(doc["route_changes"], [])
        self.assertIn("NONE is preselected", out)
        self.assertIn("nothing changes until you confirm with an exact yes", out)
        rc, out, err = self.cli("discard", "--proposal-id", doc["proposal_id"])
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.routing_bytes(), before)

    def test_invalid_duplicate_zero_missing_and_two_choices_for_one_route_never_write_routing(self):
        self.premium(cheap=11)
        rc, out, err = self.cli("propose", "--offline")
        self.assertEqual(rc, 3, out + err)
        doc = self.read_json(self.paths.proposal)
        same_route = [a["n"] for a in doc["alternatives"] if a["route"] == "tester"]
        self.assertEqual(len(same_route), 2)
        before = self.routing_bytes()
        frozen = Path(self.paths.proposal).read_bytes()
        for choice in ("", "0", "999", "1,1", "-1", "yes", "1; echo nope",
                       ",".join(map(str, same_route))):
            with self.subTest(choice=choice):
                rc, out, err = self.cli("select", "--proposal-id", doc["proposal_id"], "--choose", choice)
                self.assertEqual(rc, 4, out + err)
                self.assertEqual(self.routing_bytes(), before)
                self.assertEqual(Path(self.paths.proposal).read_bytes(), frozen)

    def test_pins_unknown_current_price_and_unknown_candidate_price_offer_no_alternatives(self):
        self.premium()
        self.config["pins"] = {"tester": {"model": self.defaults["tester"]["model"]}}
        self.save_config()
        ctx = self.mod.build_context(self.paths, NOW)
        self.assertEqual(self.rows(ctx)["tester"]["alternatives"], [])
        self.markdown = b"\n".join(line for line in self.markdown.splitlines()
                                  if not line.startswith(("| %s |" % NEW).encode()))
        self.seed_cache()
        ctx = self.mod.build_context(self.paths, NOW)
        self.assertTrue(all(a["model"] != NEW for r in self.rows(ctx).values() for a in r["alternatives"]))
        baseline = self.defaults["coder"]["model"]
        self.markdown = b"\n".join(line for line in self.markdown.splitlines()
                                  if not line.startswith(("| %s |" % baseline).encode()))
        self.seed_cache()
        row = self.rows(self.mod.build_context(self.paths, NOW))["coder"]
        self.assertIsNone(row["current_cost"])
        self.assertIsNone(row["proposed"])
        self.assertEqual(row["alternatives"], [])

    def test_changed_verified_record_and_cache_content_identity_refuse_select_and_apply(self):
        for command, changed in (("select", "verified"), ("apply", "capability-content"), ("apply", "pricing")):
            with self.subTest(command=command, changed=changed):
                # These are independent input mutations: do not carry the
                # first case's complete-list exclusion into later proposals.
                Path(self.paths.verified).unlink(missing_ok=True)
                self.premium()
                proposal_id = self.proposal()
                before = self.routing_bytes()
                n = self.numbered(proposal_id, "tester")
                if changed == "verified":
                    self.write_json(self.paths.verified, self.verified_doc())
                elif changed == "capability-content":
                    doc = self.read_json(self.paths.capabilities)
                    doc["sources"]["model_comparison"]["content_sha256"] = "f" * 64
                    self.write_json(self.paths.capabilities, doc)
                else:
                    self.seed_cache(markdown=self.live_prices(new=13, cheap=5))
                args = ("--choose", str(n)) if command == "select" else ()
                rc, out, err = self.cli(command, "--proposal-id", proposal_id, *args)
                self.assertEqual(rc, 4, out + err)
                self.assertIn("changed", err)
                self.assertEqual(self.routing_bytes(), before)

    def test_tampered_parsed_capability_evidence_refuses_apply_even_when_route_choices_are_unchanged(self):
        self.context()
        proposal_id = self.proposal()
        before = self.routing_bytes()
        doc = self.read_json(self.paths.capabilities)
        doc["sources"]["model_comparison"]["sections"]["tasks"]["models"][NEW]["excels_at"] = "Altered evidence"
        self.write_json(self.paths.capabilities, doc)
        rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 4, "changed parsed capability evidence accepted: rc=%s %s %s" %
                         (rc, out[:140], err))
        self.assertEqual(self.routing_bytes(), before)

    def test_tampered_parsed_prices_refuse_apply_even_when_cheapest_model_is_unchanged(self):
        self.context()
        proposal_id = self.proposal()
        before = self.routing_bytes()
        path = self.pricing.official_pricing_cache_path()
        doc = self.read_json(path)
        rates = doc["models"][NEW]["tiers"]["default"]
        for field in ("input", "cached_input", "cache_write", "output"):
            rates[field] = 6
        self.write_json(path, doc)
        rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 4, "changed parsed price evidence accepted: rc=%s %s %s" %
                         (rc, out[:140], err))
        self.assertEqual(self.routing_bytes(), before)

    def test_expiring_evidence_inside_proposal_ttl_refuses_without_network(self):
        for evidence in ("supported_models", "pricing"):
            with self.subTest(evidence=evidence):
                self.seed_cache(age=DAY - 100 if evidence == "pricing" else 0)
                self.seed_docs({"supported_models": 3 * DAY - 100} if evidence == "supported_models" else {})
                rc, out, err = self.cli("propose", "--offline")
                self.assertEqual(rc, 0, out + err)
                proposal_id = self.read_json(self.paths.proposal)["proposal_id"]
                before = self.routing_bytes()
                rc, out, err = self.cli("apply", "--proposal-id", proposal_id, now=NOW + 101)
                self.assertEqual(rc, 4, out + err)
                self.assertEqual(self.routing_bytes(), before)
                self.assertEqual(self.events, [])

    def test_future_dated_proposal_is_refused(self):
        self.context()
        proposal_id = self.proposal()
        before = self.routing_bytes()
        doc = self.read_json(self.paths.proposal)
        doc["created_epoch"] = NOW + 60
        doc["created_at"] = self.mod.now_iso(NOW + 60)
        self.write_json(self.paths.proposal, doc)
        rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 4, "future-dated proposal accepted: rc=%s %s %s" % (rc, out[:140], err))
        self.assertEqual(self.routing_bytes(), before)

    def test_altered_expiry_cannot_extend_frozen_fifteen_minute_ttl(self):
        self.context()
        proposal_id = self.proposal()
        before = self.routing_bytes()
        doc = self.read_json(self.paths.proposal)
        doc["expires_epoch"] = NOW + 3600
        self.write_json(self.paths.proposal, doc)
        rc, out, err = self.cli("apply", "--proposal-id", proposal_id, now=NOW + 901)
        self.assertEqual(rc, 4, "proposal TTL extension accepted: rc=%s %s %s" % (rc, out[:140], err))
        self.assertEqual(self.routing_bytes(), before)

    def test_applied_proposal_replay_and_old_format_are_refused(self):
        self.context()
        proposal_id = self.proposal()
        frozen = Path(self.paths.proposal).read_bytes()
        rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 0, out + err)
        self.write(self.paths.proposal, frozen)
        before = self.routing_bytes()
        rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 4, out + err)
        self.assertIn("replay", err)
        self.assertEqual(self.routing_bytes(), before)
        doc = json.loads(frozen)
        doc["format"] = 1
        self.write_json(self.paths.proposal, doc)
        rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 4, out + err)
        self.assertIn("older version", err)
        self.assertEqual(self.routing_bytes(), before)


class TestProposalScopedReview(LiveRefreshCase):
    """Actual helper transactions; all model facts and transport are synthetic."""

    def setUp(self):
        super().setUp()
        self.config.pop("available_models")
        self.save_config()
        self.markdown = self.live_prices(new=5, cheap=6)

    def protected_bytes(self):
        return dict(self.routing_bytes(), **{
            str(p): Path(p).read_bytes() if Path(p).exists() else None
            for p in (self.paths.user_config, self.paths.verified, self.routing, self.catalog_path)
        })

    def machine(self, output, marker):
        return [line.split("\t")[1:] for line in output.splitlines()
                if line.startswith(marker + "\t")]

    def frozen(self, output):
        markers = self.machine(output, "PROPOSAL")
        self.assertEqual(len(markers), 1, output)
        doc = self.read_json(self.paths.proposal)
        self.assertEqual(markers, [[doc["proposal_id"]]])
        key = Path(self.paths.proposal_key).read_bytes().strip()
        body = {k: v for k, v in doc.items() if k != "seal"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True).encode("ascii")
        self.assertEqual(doc["seal"], hmac.new(key, canonical, hashlib.sha256).hexdigest())
        return doc

    def review_proposal(self):
        self.context()
        rc, out, err = self.cli("propose", "--offline", "--review")
        self.assertEqual(rc, 0, out + err)
        return self.frozen(out), out

    def answer(self, doc, *answers, now=NOW + 1):
        with mock.patch.object(self.mod, "refresh_pricing", side_effect=AssertionError("review fetched prices")), \
                mock.patch.object(self.mod, "_fetch_capability_bytes",
                                  side_effect=AssertionError("review fetched docs")):
            return self.cli("review", "--proposal-id", doc["proposal_id"], *answers, now=now)

    def test_first_proposal_needs_no_inventory_and_accept_all_is_one_session_only_answer(self):
        before = self.protected_bytes()
        with mock.patch.object(self.mod, "_ask", side_effect=AssertionError("asked for inventory")):
            rc, out, err = self.cli("propose", "--review")
        self.assertEqual(rc, 0, out + err)
        first = self.frozen(out)
        self.assertEqual(set(first["overrides"]), set(self.defaults))
        self.assertTrue(all(v == dict(self.defaults[k], model=NEW)
                            for k, v in first["overrides"].items()))
        self.assertEqual(self.machine(out, "CHECK"), [[NEW, ",".join(self.defaults)]])
        self.assertEqual(self.machine(out, "LEVEL"), [])
        self.assertEqual(self.machine(out, "REVIEW"), [["pending"]])
        self.assertLess(out.index("Routes (current -> proposed)"), out.index("CHECK\t"))
        self.assertNotIn("ACTION REQUIRED", out)
        self.assertNotIn("VERIFY\t", out)
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(len(self.docs_fetches), 2)
        self.assertEqual(self.protected_bytes(), before)

        rc, out, err = self.answer(first, "--available-all")
        self.assertEqual(rc, 0, out + err)
        checked = self.frozen(out)
        self.assertEqual(checked["review"], {
            "round": 1, "available": [NEW], "rejected": [], "levels": {}})
        self.assertEqual(self.machine(out, "REVIEW"), [["complete"]])
        self.assertEqual(self.machine(out, "CHECK"), [])
        self.assertEqual(checked["digests"], first["digests"])
        self.assertEqual(checked["snapshot_id"], first["snapshot_id"])
        self.assertEqual(checked["created_epoch"], first["created_epoch"])
        self.assertEqual(checked["expires_epoch"], first["expires_epoch"])
        self.assertNotEqual(checked["proposal_id"], first["proposal_id"])
        self.assertNotEqual(checked["seal"], first["seal"])
        self.assertEqual(self.protected_bytes(), before)
        rc, out, err = self.answer(checked, "--available-all")
        self.assertEqual(rc, 4, out + err)
        self.assertIn("nothing", err)
        self.assertEqual(self.protected_bytes(), before)

        rc, out, err = self.cli("apply", "--proposal-id", checked["proposal_id"], now=NOW + 2)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read_json(self.paths.effective)["routes"]["tester"]["values"],
                         dict(self.defaults["tester"], model=NEW))
        for path in (self.paths.user_config, self.paths.verified, self.routing, self.catalog_path):
            self.assertEqual(self.protected_bytes()[str(path)], before[str(path)])
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(len(self.docs_fetches), 2)

    def test_reject_repropose_accept_uses_same_snapshot_and_excludes_rejected_in_every_route(self):
        before = self.protected_bytes()
        first, _ = self.review_proposal()
        rc, out, err = self.answer(first, "--unavailable", NEW.upper())
        self.assertEqual(rc, 0, out + err)
        revised = self.frozen(out)
        self.assertEqual(revised["review"]["rejected"], [NEW])
        self.assertEqual(self.machine(out, "CHECK"), [[CHEAP, ",".join(self.defaults)]])
        self.assertTrue(all(v == dict(self.defaults[k], model=CHEAP)
                            for k, v in revised["overrides"].items()))
        self.assertTrue(all(a["values"]["model"] != NEW for a in revised["alternatives"]))
        for field in ("snapshot_id", "digests", "created_epoch", "expires_epoch"):
            self.assertEqual(revised[field], first[field])
        self.assertNotEqual(revised["proposal_id"], first["proposal_id"])
        rc, out, err = self.answer(first, "--available-all")
        self.assertEqual(rc, 4, out + err)
        self.assertIn("does not match", err)

        rc, out, err = self.answer(revised, "--available-all", now=NOW + 2)
        self.assertEqual(rc, 0, out + err)
        checked = self.frozen(out)
        self.assertEqual(checked["review"]["available"], [CHEAP])
        self.assertEqual(checked["review"]["rejected"], [NEW])
        self.assertEqual(self.machine(out, "REVIEW"), [["complete"]])
        ctx = self.mod.build_context(self.paths, NOW + 2)
        ctx["review"] = self.mod.validate_review(checked["review"])
        for key, row in self.rows(ctx).items():
            self.assertEqual(row["proposed"], dict(self.defaults[key], model=CHEAP))
            self.assertNotIn(NEW, row["tentative"])
            self.assertTrue(all(a["model"] != NEW for a in row["alternatives"]))
        self.assertEqual(self.protected_bytes(), before)
        self.assertEqual(self.events, [])

    def test_exhausting_candidates_removes_proposal_and_keeps_current_mapping(self):
        before = self.protected_bytes()
        first, _ = self.review_proposal()
        rc, out, err = self.answer(first, "--unavailable", NEW)
        self.assertEqual(rc, 0, out + err)
        second = self.frozen(out)
        rc, out, err = self.answer(second, "--unavailable", CHEAP, now=NOW + 2)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("No eligible replacement remains", out)
        self.assertEqual(self.machine(out, "REVIEW"), [["complete"]])
        self.assertEqual(self.machine(out, "PROPOSAL"), [])
        self.assertEqual(self.machine(out, "CHECK"), [])
        self.assertFalse(Path(self.paths.proposal).exists())
        self.assertEqual(self.protected_bytes(), before)
        self.assertEqual(self.events, [])

    def test_invalid_out_of_map_and_unasked_level_answers_never_change_proposal(self):
        self.config.pop("verified_capabilities")
        self.save_config()
        doc, _ = self.review_proposal()
        before = self.protected_bytes()
        frozen = Path(self.paths.proposal).read_bytes()
        for args in (
            (), ("--unavailable", ""), ("--unavailable", USER_ONLY),
            ("--unavailable", UNKNOWN), ("--unavailable", self.defaults["coder"]["model"]),
            ("--unavailable", NEW + ",../invalid"),
            ("--available-all", "--level", USER_ONLY + ":low=yes"),
            ("--available-all", "--level", NEW + ":max=yes"),
            ("--available-all", "--level", NEW + ":high=unknown"),
            ("--available-all", "--level", NEW + ":high=yes", "--level", NEW + ":high=no"),
            ("--unavailable", NEW, "--level", NEW + ":high=yes"),
        ):
            with self.subTest(args=args):
                rc, out, err = self.answer(doc, *args)
                self.assertEqual(rc, 2, out + err)
                self.assertEqual(Path(self.paths.proposal).read_bytes(), frozen)
                self.assertEqual(self.protected_bytes(), before)

    def test_availability_does_not_attest_levels_and_exact_yes_no_never_downgrades(self):
        self.config.pop("verified_capabilities")
        self.save_config()
        before = self.protected_bytes()
        doc, out = self.review_proposal()
        efforts = {v["reasoning_effort"] for v in self.defaults.values()}
        self.assertEqual({tuple(row) for row in self.machine(out, "LEVEL")},
                         {(NEW, effort) for effort in efforts})
        rc, out, err = self.answer(doc, "--available-all")
        self.assertEqual(rc, 0, out + err)
        available = self.frozen(out)
        self.assertEqual(available["review"]["levels"], {})
        self.assertEqual(self.machine(out, "CHECK"), [])
        self.assertEqual({tuple(row) for row in self.machine(out, "LEVEL")},
                         {(NEW, effort) for effort in efforts})
        answers = []
        for effort in sorted(efforts):
            answers += ["--level", "%s:%s=%s" % (NEW, effort, "no" if effort == "high" else "yes")]
        rc, out, err = self.answer(available, *answers, now=NOW + 2)
        self.assertEqual(rc, 0, out + err)
        revised = self.frozen(out)
        high_routes = {k for k, v in self.defaults.items() if v["reasoning_effort"] == "high"}
        for key, values in revised["overrides"].items():
            self.assertEqual(values["reasoning_effort"], self.defaults[key]["reasoning_effort"])
            self.assertEqual(values["context_tier"], self.defaults[key]["context_tier"])
            self.assertEqual(values["model"], CHEAP if key in high_routes else NEW)
        self.assertEqual(self.machine(out, "CHECK"), [[CHEAP, ",".join(
            k for k in self.defaults if k in high_routes)]])
        self.assertEqual(self.machine(out, "LEVEL"), [[CHEAP, "high"]])
        rc, out, err = self.answer(revised, "--available-all", "--level", CHEAP + ":high=yes", now=NOW + 3)
        self.assertEqual(rc, 0, out + err)
        checked = self.frozen(out)
        self.assertEqual(self.machine(out, "REVIEW"), [["complete"]])
        self.assertFalse(checked["review"]["levels"][NEW]["high"])
        self.assertEqual(self.protected_bytes(), before)

    def test_unknown_level_keeps_apply_tentative_and_no_for_every_level_exhausts_candidates(self):
        self.config.pop("verified_capabilities")
        self.save_config()
        before = self.protected_bytes()
        first, _ = self.review_proposal()
        rc, out, err = self.answer(first, "--available-all")
        self.assertEqual(rc, 0, out + err)
        unknown = self.frozen(out)
        rc, out, err = self.cli("apply", "--proposal-id", unknown["proposal_id"], now=NOW + 1)
        self.assertEqual(rc, 4, out + err)
        self.assertIn("tentative", err)
        self.assertEqual(self.protected_bytes(), before)
        first, _ = self.review_proposal()
        efforts = sorted({v["reasoning_effort"] for v in self.defaults.values()})
        for mid in (NEW, CHEAP):
            args = ["--available-all"]
            for effort in efforts:
                args += ["--level", "%s:%s=no" % (mid, effort)]
            rc, out, err = self.answer(first, *args)
            if mid == NEW:
                self.assertEqual(rc, 0, out + err)
                first = self.frozen(out)
            else:
                self.assertEqual(rc, 3, out + err)
                self.assertNotIn("PROPOSAL\t", out)
        self.assertEqual(self.protected_bytes(), before)

    def test_pending_availability_cannot_apply_or_answer_a_plain_proposal(self):
        before = self.protected_bytes()
        doc, _ = self.review_proposal()
        rc, out, err = self.cli("apply", "--proposal-id", doc["proposal_id"])
        self.assertEqual(rc, 4, out + err)
        self.assertIn("tentative", err)
        self.assertEqual(self.protected_bytes(), before)
        self.config["available_models"] = {"verified_on": "2026-11-16", "models": list(self.ids)}
        self.save_config()
        plain = self.proposal()
        frozen = Path(self.paths.proposal).read_bytes()
        rc, out, err = self.cli("review", "--proposal-id", plain, "--available-all")
        self.assertEqual(rc, 4, out + err)
        self.assertIn("without --review", err)
        self.assertEqual(Path(self.paths.proposal).read_bytes(), frozen)

    def test_selected_premiums_are_reviewed_and_carried_by_route_values_not_old_numbers(self):
        self.premium(cheap=11)
        before = self.protected_bytes()
        rc, out, err = self.cli("propose", "--offline", "--review")
        self.assertEqual(rc, 3, out + err)
        first = self.frozen(out)
        self.assertEqual(first["overrides"], {})
        self.assertEqual(first["selected"], [])
        self.assertEqual(self.machine(out, "CHECK"), [], "unselected alternatives must not ask questions")
        keep_n = self.numbered(first["proposal_id"], "tester", NEW)
        drop_n = self.numbered(first["proposal_id"], "coder", CHEAP)
        rc, out, err = self.cli("select", "--proposal-id", first["proposal_id"],
                                "--choose", "%s,%s" % (keep_n, drop_n), now=NOW + 1)
        self.assertEqual(rc, 0, out + err)
        selected = self.frozen(out)
        self.assertEqual({row[0] for row in self.machine(out, "CHECK")}, {NEW, CHEAP})
        self.assertEqual(self.machine(out, "REVIEW"), [["pending"]])
        self.assertIn("costs MORE", out)
        self.assertEqual(self.protected_bytes(), before)
        rc, out, err = self.answer(selected, "--unavailable", CHEAP, now=NOW + 2)
        self.assertEqual(rc, 0, out + err)
        checked = self.frozen(out)
        self.assertEqual(checked["overrides"], {"tester": dict(self.defaults["tester"], model=NEW)})
        kept = [a for a in checked["alternatives"] if a["n"] in checked["selected"]]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["route"], "tester")
        self.assertEqual(kept[0]["values"], dict(self.defaults["tester"], model=NEW))
        self.assertNotEqual(kept[0]["n"], keep_n, "the renumbered table must not reuse stale selections")
        self.assertTrue(all(a["values"]["model"] != CHEAP for a in checked["alternatives"]))
        self.assertIn("Dropped:", out)
        self.assertEqual(self.machine(out, "SELECTED"), [["1"]])
        self.assertEqual(self.machine(out, "REVIEW"), [["complete"]])
        for field in ("snapshot_id", "digests", "created_epoch", "expires_epoch"):
            self.assertEqual(checked[field], first[field])
        self.assertEqual(self.protected_bytes(), before)

    def test_changed_inputs_tampering_and_expiry_refuse_review_and_checked_apply(self):
        original = self.protected_bytes()
        for command in ("review", "apply"):
            for changed in ("settings", "user_config", "routing", "catalog", "verified",
                            "pricing", "capabilities", "review-seal", "expired"):
                with self.subTest(command=command, changed=changed):
                    for path, raw in original.items():
                        if raw is None:
                            Path(path).unlink(missing_ok=True)
                        else:
                            self.write(path, raw)
                    doc, _ = self.review_proposal()
                    if command == "apply":
                        rc, out, err = self.answer(doc, "--available-all")
                        self.assertEqual(rc, 0, out + err)
                        doc = self.frozen(out)
                    if changed == "settings":
                        data = copy.deepcopy(self.initial_settings)
                        data["theme"] = "edited-after-proposal"
                        self.write_json(self.paths.settings, data)
                    elif changed == "user_config":
                        data = copy.deepcopy(self.config)
                        data["pins"] = {"tester": {"model": USER_ONLY}}
                        self.write_json(self.paths.user_config, data)
                    elif changed == "routing":
                        self.write(self.routing, Path(self.routing).read_bytes() + b"\n# changed\n")
                    elif changed == "catalog":
                        data = copy.deepcopy(self.catalog)
                        data["models"][CHEAP]["display_name"] = "Changed fixture display name"
                        self.write_json(self.catalog_path, data)
                    elif changed == "verified":
                        self.write_json(self.paths.verified, self.verified_doc())
                    elif changed == "pricing":
                        data = self.read_json(self.pricing.official_pricing_cache_path())
                        data["models"][NEW]["tiers"]["default"]["input"] = 6
                        self.write_json(self.pricing.official_pricing_cache_path(), data)
                    elif changed == "capabilities":
                        data = self.read_json(self.paths.capabilities)
                        data["sources"]["model_comparison"]["sections"]["tasks"]["models"][NEW]["excels_at"] = "edited"
                        self.write_json(self.paths.capabilities, data)
                    elif changed == "review-seal":
                        data = copy.deepcopy(doc)
                        data["review"]["levels"][NEW] = {"max": True}
                        self.write_json(self.paths.proposal, data)
                    before = self.protected_bytes()
                    args = ("--available-all",) if command == "review" else ()
                    now = NOW + 901 if changed == "expired" else NOW + 2
                    with mock.patch.object(self.mod, "refresh_pricing", side_effect=AssertionError("fetched")), \
                            mock.patch.object(self.mod, "_fetch_capability_bytes",
                                              side_effect=AssertionError("fetched")):
                        rc, out, err = self.cli(command, "--proposal-id", doc["proposal_id"], *args, now=now)
                    self.assertEqual(rc, 4, out + err)
                    self.assertIn("expired" if changed == "expired" else
                                  "altered" if changed == "review-seal" else "changed", err)
                    self.assertEqual(self.protected_bytes(), before)

    def test_evidence_expiring_within_original_ttl_refuses_review_without_refetch(self):
        before = self.protected_bytes()
        for evidence in ("pricing", "supported_models"):
            with self.subTest(evidence=evidence):
                self.seed_cache(age=DAY - 100 if evidence == "pricing" else 0)
                self.seed_docs({"supported_models": 3 * DAY - 100} if evidence == "supported_models" else {})
                rc, out, err = self.cli("propose", "--offline", "--review")
                self.assertEqual(rc, 0, out + err)
                doc = self.frozen(out)
                rc, out, err = self.answer(doc, "--available-all", now=NOW + 101)
                self.assertEqual(rc, 4, out + err)
                self.assertIn("changed", err)
                self.assertEqual(self.protected_bytes(), before)
                self.assertEqual(self.events, [])

    def test_review_preserves_native_pins_config_pins_and_unknown_price_fallback(self):
        settings = copy.deepcopy(self.initial_settings)
        settings["subagents"]["agents"]["coder"] = {"model": "native-choice", "effortLevel": "inherit"}
        self.write_json(self.paths.settings, settings)
        self.config["pins"] = {"reviewer.stronger": {"reasoning_effort": "max"}}
        self.save_config()
        task_model = self.defaults["task"]["model"]
        self.markdown = b"\n".join(line for line in self.markdown.splitlines()
                                  if not line.startswith(("| %s |" % task_model).encode()))
        before = self.protected_bytes()
        doc, out = self.review_proposal()
        self.assertNotIn("coder", doc["overrides"])
        self.assertNotIn("reviewer.stronger", doc["overrides"])
        self.assertNotIn("task", doc["overrides"])
        self.assertIn("native-choice", out)
        self.assertIn("not in the current official pricing", out)
        rc, out, err = self.answer(doc, "--available-all")
        self.assertEqual(rc, 0, out + err)
        checked = self.frozen(out)
        self.assertEqual(self.protected_bytes(), before)
        rc, out, err = self.cli("apply", "--proposal-id", checked["proposal_id"], now=NOW + 2)
        self.assertEqual(rc, 0, out + err)
        after = self.read_json(self.paths.settings)["subagents"]["agents"]
        self.assertEqual(after["coder"], settings["subagents"]["agents"]["coder"])
        self.assertNotIn("task", after)
        self.assertEqual(Path(self.paths.user_config).read_bytes(), before[self.paths.user_config])
        rc, out, err = self.cli("status", "--instructions")
        self.assertEqual(rc, 0, out + err)
        routes = self.live_routes(out)
        self.assertEqual(routes["coder"]["model"], "OMIT")
        self.assertEqual(routes["task"], self.defaults["task"])
        self.assertEqual(routes["reviewer.stronger"]["reasoning_effort"], "max")


BENCHMARK_HELPER = ROOT / "bin" / "copilot_model_benchmarks.py"


def comparison_fixture(pair, values=None, efforts=("max", "max"), updated=True,
                       reverse=False):
    """Entirely synthetic provider layouts; not copied public/source data."""
    names = [n for _mid, n in pair["models"]]
    if pair["provider"] == "benchleader":
        metrics = values or (
            ("Terminal-Bench", "Terminal-Bench", "70%", "80%"),
            ("Coding score", "BenchLeader", "72", "82"),
            ("Agents & tools score", "BenchLeader", "90", "60"),
        )
        columns = ["%s%s" % (n, " (%s)" % e if e else "")
                   for n, e in zip(names, efforts)]
        if reverse:
            columns.reverse()
        lines = ["---", "url: " + pair["page"], "---",
                 "# %s vs %s" % tuple(names)]
        if updated:
            lines.append("Updated 16 November 2026")
        lines += ["## Every benchmark", "| Metric | Publisher | %s | %s |" % tuple(columns),
                  "|---|---|---|---|"]
        for label, publisher, a, b in metrics:
            if reverse:
                a, b = b, a
            lines.append("| %s | %s | %s | %s |" % (label, publisher, a, b))
    else:
        metrics = values or (("Terminal-Bench 4.0", "86%", "70%"),)
        columns = list(reversed(names)) if reverse else names
        lines = ["# %s vs %s" % tuple(names), "- Canonical page: " + pair["page"]]
        if updated:
            lines.append("- Last updated: November 16, 2026")
        lines += ["## Category Breakdown", "### Coding",
                  "- %s public-lane score: 90 (Supported: synthetic fixture)" % names[0],
                  "- %s public-lane score: 60 (Supported: synthetic fixture)" % names[1],
                  "| Benchmark | %s | %s | Winner |" % tuple(columns),
                  "|---|---|---|---|"]
        for label, a, b in metrics:
            if reverse:
                a, b = b, a
            lines.append("| %s | %s | %s | irrelevant remote winner |" % (label, a, b))
    return ("\n".join(lines) + "\n").encode()


class TestPublicBenchmarkEvidence(unittest.TestCase):
    """Offline parsing, cache, comparison and bounded transport contracts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="benchmark-fixture-")
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "state" / "benchmark-evidence.json"
        self.b = load_module("benchmark_fixture_test", BENCHMARK_HELPER)
        self.clock = mock.patch.object(self.b.time, "monotonic", return_value=100.0)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        for target in ("socket.getaddrinfo", "socket.create_connection",
                       "socket.socket.connect", "socket.socket.connect_ex"):
            patch = mock.patch(target, side_effect=AssertionError("live network forbidden"))
            patch.start()
            self.addCleanup(patch.stop)
        self.calls = []

    def fetch(self, url, timeout, max_bytes, deadline):
        self.calls.append((url, timeout, max_bytes, deadline))
        pair = next(p for p in self.b.PAIRS if p["url"] == url)
        return comparison_fixture(pair)

    def refreshed(self, now=NOW, fetcher=None):
        return self.b.refresh(str(self.cache), now, True, fetcher=fetcher or self.fetch)

    def compared(self, ctx):
        return self.b.comparison(ctx, "gpt-6.1-sol", NEW, "max", "max")

    def test_fixed_registry_and_exact_public_urls_only(self):
        self.assertEqual(len(self.b.PAIRS), 4)
        self.assertEqual({p["provider"] for p in self.b.PAIRS}, {"benchleader", "benchlm"})
        self.assertEqual({frozenset(m for m, _ in p["models"]) for p in self.b.PAIRS},
                         {frozenset(("claude-sonnet-5.5", "gpt-6.1-terra")),
                          frozenset(("gpt-6.1-sol", NEW))})
        self.assertEqual(self.b.ALLOWED_URLS, frozenset(p["url"] for p in self.b.PAIRS))
        for p in self.b.PAIRS:
            self.assertTrue(p["url"].startswith("https://"))
            self.assertNotRegex(p["url"], r"[@?#]")

    def test_fetch_all_fresh_attributable_values_no_average_or_winner(self):
        ctx = self.refreshed()
        self.assertEqual(self.calls, [(p["url"], 8, 1_000_000, 120.0) for p in self.b.PAIRS])
        self.assertTrue(all(s["status"] == "fresh" for s in ctx["pairs"].values()))
        comp = self.compared(ctx)
        self.assertTrue(comp["supports"])
        self.assertTrue(comp["mixed"])
        self.assertTrue(comp["providers_disagree"])
        self.assertEqual({e["provider"] for e in comp["fresh"]}, {"BenchLeader", "BenchLM"})
        entry = next(e for e in comp["fresh"] if e["provider"] == "BenchLeader")
        metric = next(m for m in entry["metrics"] if m["key"] == "terminal_bench")
        self.assertEqual((metric["current"], metric["candidate"], metric["publisher"], metric["unit"]),
                         (70.0, 80.0, "Terminal-Bench", "%"))
        self.assertEqual(entry["updated_on"], "2026-11-16")
        self.assertEqual(entry["fetched_at"], self.b._now_iso(NOW))
        self.assertEqual(entry["efforts"], {"gpt-6.1-sol": "max", NEW: "max"})
        other = next(e for e in comp["fresh"] if e["provider"] == "BenchLM")
        self.assertEqual(other["efforts"], {"gpt-6.1-sol": None, NEW: None})
        self.assertFalse({"average", "overall_winner", "recommendation"} & comp.keys())
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)
        self.assertNotIn("irrelevant remote winner", self.cache.read_text())
        self.assertNotIn("synthetic fixture", self.cache.read_text())
        self.assertEqual(self.b.comparison(ctx, CHEAP, NEW)["fresh"], [])

    def test_reverse_columns_preserve_exact_model_identity(self):
        for pair in self.b.PAIRS:
            with self.subTest(provider=pair["provider"], pair=pair["id"]):
                normal = self.b.parse_page(pair, comparison_fixture(pair))
                reverse = self.b.parse_page(pair, comparison_fixture(pair, reverse=True))
                self.assertEqual(normal, reverse)

    def test_cache_reuse_five_minutes_freshness_and_future_are_exact(self):
        self.refreshed()
        self.calls.clear()
        self.refreshed(now=NOW + 299)
        self.assertEqual(self.calls, [])
        self.refreshed(now=NOW + 300)
        self.assertEqual(len(self.calls), 4)
        cache, warning = self.b.read_cache(str(self.cache))
        self.assertIsNone(warning)
        for age, state in ((0, "fresh"), (86400, "fresh"),
                           (86401, "stale"), (-1, "future")):
            with self.subTest(age=age):
                ctx = self.b.context(cache, NOW + 300 + age)
                self.assertEqual({s["status"] for s in ctx["pairs"].values()}, {state})
                self.assertEqual(self.compared(ctx)["supports"], state == "fresh")

    def test_partial_error_retains_copy_and_sanitizes_error_without_poisoning_other_sources(self):
        old = self.refreshed()
        pid = self.b.PAIRS[0]["id"]

        def partial(url, *args):
            if url == self.b.PAIRS[0]["url"]:
                raise OSError("\x1b]8;;https://evil.invalid\x07bad\x1b[31m\n\u202eerror")
            return self.fetch(url, *args)

        ctx = self.refreshed(now=NOW + 300, fetcher=partial)
        self.assertEqual(ctx["pairs"][pid]["record"], old["pairs"][pid]["record"])
        self.assertIn("REFRESH FAILED", ctx["pairs"][pid]["note"])
        self.assertNotRegex(ctx["pairs"][pid]["note"], r"[\x00-\x1f\u202a-\u202e]")
        self.assertTrue(all(s["status"] == "fresh" for s in ctx["pairs"].values()))
        stale = self.refreshed(now=NOW + DAY + 1,
                               fetcher=lambda *a: (_ for _ in ()).throw(OSError("unavailable")))
        self.assertEqual(stale["pairs"][pid]["status"], "stale")
        self.assertIsNotNone(stale["pairs"][pid]["record"])

    def test_missing_and_malformed_bodies_are_nonfatal_unknown(self):
        for raw in (b"not a comparison", b"\xff", None, "not bytes", b"x" * 1_000_001):
            with self.subTest(raw_type=type(raw).__name__):
                self.cache.unlink(missing_ok=True)
                ctx = self.refreshed(fetcher=lambda *args: raw)
                self.assertEqual({s["status"] for s in ctx["pairs"].values()}, {"missing"})
                self.assertFalse(self.compared(ctx)["supports"])
                self.assertTrue(all(s["last_error"] for s in ctx["pairs"].values()))

    def test_shared_budget_skips_remaining_requests_after_deadline(self):
        with mock.patch.object(self.b.time, "monotonic", side_effect=(100, 100, 121, 121, 121)):
            ctx = self.refreshed()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(s["status"] == "missing" for s in ctx["pairs"].values()), 3)
        self.assertTrue(all("deadline" in s["last_error"] for s in list(ctx["pairs"].values())[1:]))

    def test_cache_only_and_kill_switches_do_not_fetch_or_write(self):
        self.refreshed()
        before = self.cache.read_bytes(), self.cache.stat().st_mtime_ns
        ctx = self.b.refresh(str(self.cache), NOW + DAY + 1, False, "offline",
                             fetcher=mock.Mock(side_effect=AssertionError("offline fetch")))
        self.assertEqual(before, (self.cache.read_bytes(), self.cache.stat().st_mtime_ns))
        self.assertFalse(self.compared(ctx)["supports"])
        self.assertIn("offline", self.b.fetch_disabled_reason(offline=True))
        for value in ("0", "off", "false", "no"):
            with mock.patch.dict(os.environ, {self.b.FETCH_ENV: value}):
                self.assertIn("disabled", self.b.fetch_disabled_reason())
        with mock.patch.dict(os.environ, {self.b.FETCH_ENV: "1"}):
            self.assertIsNone(self.b.fetch_disabled_reason())
            self.assertEqual(self.b.fetch_disabled_reason(extra_disabled="fixture"), "fixture")

    def test_all_sources_unavailable_does_not_claim_quality_or_support(self):
        ctx = self.refreshed(fetcher=lambda *a: (_ for _ in ()).throw(TimeoutError("timeout")))
        self.assertFalse(self.compared(ctx)["supports"])
        self.assertEqual(self.compared(self.b.unavailable_context("missing module"))["fresh"], [])
        self.assertEqual({s["status"] for s in ctx["pairs"].values()}, {"missing"})

    def test_title_canonical_headers_and_layout_mismatch_refuse(self):
        for pair in self.b.PAIRS:
            good = comparison_fixture(pair)
            mutations = (good.replace(pair["page"].encode(), b"https://wrong.invalid/pair"),
                         good.replace(pair["models"][0][1].encode(), b"Another model", 1),
                         good.replace(b"Every benchmark", b"Another section")
                         if pair["provider"] == "benchleader" else
                         good.replace(b"Category Breakdown", b"Another section"),
                         good.replace(b"Metric | Publisher", b"Metric | Not publisher")
                         if pair["provider"] == "benchleader" else
                         good.replace(b"| Winner |", b"| Not winner |"))
            for i, raw in enumerate(mutations):
                with self.subTest(pair=pair["id"], mutation=i):
                    with self.assertRaises(self.b.BenchmarkError):
                        self.b.parse_page(pair, raw)

    def test_unknown_zero_invalid_units_ranges_and_missing_are_not_invented(self):
        pair = self.b.PAIRS[1]
        for raw, expected in (("0%", 0.0), ("100%", 100.0), ("n/a", None), ("—", None),
                              ("coming soon", None), ("101%", None), ("-1%", None),
                              ("80", None), ("NaN%", None), ("inf%", None),
                              ("80% estimated", None), ("1e2%", None)):
            with self.subTest(value=raw):
                parsed = self.b.parse_page(pair, comparison_fixture(pair, values=(
                    ("Terminal-Bench", "Terminal-Bench", raw, "80%"),)))
                metric = parsed["metrics"]["terminal_bench"]
                self.assertEqual(metric["values"]["gpt-6.1-sol"]["value"], expected)
        parsed = self.b.parse_page(pair, comparison_fixture(pair, updated=False))
        self.assertIsNone(parsed["updated_on"])

    def test_duplicate_conflicting_and_other_effort_rows_never_support(self):
        pair = self.b.PAIRS[1]
        for values in (
                (("Terminal-Bench", "Terminal-Bench", "70%", "80%"),
                 ("Terminal-Bench", "Terminal-Bench", "70%", "80%"),
                 ("Coding score", "BenchLeader", "50", "40")),
                (("Terminal-Bench", "Terminal-Bench", "70%", "80%"),
                 ("Terminal-Bench", "Terminal-Bench", "90%", "99%"),
                 ("Coding score", "BenchLeader", "50", "40")),
                (("Terminal-Bench", "Terminal-Bench", "70%", "80%†"),)):
            with self.subTest(values=values):
                self.cache.unlink(missing_ok=True)
                ctx = self.refreshed(fetcher=lambda url, *a: comparison_fixture(pair, values=values)
                                     if url == pair["url"] else b"unsupported layout")
                comp = self.compared(ctx)
                self.assertFalse(comp["supports"])
                if len(values) > 1:
                    m = ctx["pairs"][pair["id"]]["record"]["metrics"]["terminal_bench"]
                    self.assertTrue(m["conflict"])
                    self.assertTrue(all(v["value"] is None for v in m["values"].values()))
                else:
                    self.assertEqual(comp["fresh"][0]["metrics"][0]["why_not"],
                                     "measured at another effort setting")

    def test_benchlm_estimated_composite_is_reference_not_support(self):
        pair = self.b.PAIRS[3]
        raw = comparison_fixture(pair, values=(("Terminal-Bench 4.0", "90%", "80%"),))
        raw = raw.replace(b"90 (Supported", b"30 (Estimated")
        ctx = self.refreshed(fetcher=lambda url, *a: raw if url == pair["url"] else b"unsupported")
        comp = self.compared(ctx)
        self.assertFalse(comp["supports"])
        metric = next(m for m in comp["fresh"][0]["metrics"] if m["kind"] == "composite")
        self.assertFalse(metric["comparable"])
        self.assertIn("estimated", metric["why_not"])

    def test_cache_schema_registry_duplicates_nonfinite_and_model_identity_refuse(self):
        self.refreshed()
        good = self.cache.read_bytes()
        doc = json.loads(good)
        pid = self.b.PAIRS[1]["id"]
        mutations = []
        for edit in (
                lambda d: d.update(schema_version=99),
                lambda d: d.update(registry="0" * 64),
                lambda d: d["records"][pid].update(url="https://private.invalid"),
                lambda d: d["records"][pid]["efforts"].update({NEW: "invented"}),
                lambda d: d["records"][pid]["metrics"]["terminal_bench"]["values"][NEW].update(value=True),
                lambda d: d["records"][pid]["metrics"]["terminal_bench"]["values"][NEW].update(value=101),
                lambda d: d["records"][pid]["metrics"]["terminal_bench"].update(label="Injected label"),
                lambda d: d["records"][pid].update(fetched_epoch=float("nan"))):
            changed = copy.deepcopy(doc)
            edit(changed)
            mutations.append(json.dumps(changed).encode())
        mutations += [b'{"schema_version":1,"schema_version":1}',
                      b"x" * (self.b.MAX_CACHE_BYTES + 1)]
        for raw in mutations:
            with self.subTest(prefix=raw[:50]):
                self.cache.write_bytes(raw)
                got, warning = self.b.read_cache(str(self.cache))
                self.assertEqual(got["records"], {})
                self.assertIsNotNone(warning)
        self.cache.write_bytes(good)
        self.assertIsNone(self.b.read_cache(str(self.cache))[1])

    def test_cache_symlinks_refuse_and_do_not_mutate_targets(self):
        self.refreshed()
        real = self.cache.with_name("real.json")
        self.cache.rename(real)
        self.cache.symlink_to(real)
        before = real.read_bytes()
        cache, warning = self.b.read_cache(str(self.cache))
        self.assertEqual(cache["records"], {})
        self.assertIsNotNone(warning)
        with self.assertRaises(OSError):
            self.b._write_cache(str(self.cache), self.b.blank_cache())
        self.assertEqual(real.read_bytes(), before)

    def test_impossible_or_forged_cached_provenance_dates_are_refused(self):
        self.refreshed()
        good = json.loads(self.cache.read_bytes())
        for field, value in (("updated_on", "2026-99-99"),
                             ("fetched_at", "not a timestamp"),
                             ("fetched_at", "2000-01-01T00:00:00Z"),
                             ("fetched_epoch", True),
                             ("fetched_epoch", 0),
                             ("fetched_epoch", -1),
                             ("fetched_epoch", 32503680000),
                             ("fetched_epoch", "not an epoch"),
                             ("notes", ["unsafe \x1b[31mnote"]),
                             ("notes", ["unsafe \u202enote"])):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(good)
                changed["records"][self.b.PAIRS[1]["id"]][field] = value
                self.cache.write_text(json.dumps(changed))
                doc, warning = self.b.read_cache(str(self.cache))
                self.assertEqual(doc["records"], {})
                self.assertIsNotNone(warning)

    def test_safe_text_strips_terminal_controls_bidi_and_bounds_exceptions(self):
        unsafe = "\x1b[31mred\x1b[0m\x1b]8;;https://evil.invalid\x07link\x1b]8;;\x07" \
                 "\r\n\x00\u202e\u2066\u200b" + "x" * 500
        clean = self.b.safe_text(unsafe)
        self.assertLessEqual(len(clean), 200)
        self.assertNotRegex(clean, r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
        self.assertNotIn("evil.invalid", clean)

    def test_nonallowlisted_url_variants_are_rejected_before_dns(self):
        url = self.b.PAIRS[0]["url"]
        for variant in ("http:" + url[6:], url + "?auth=secret", url + "#fragment",
                        url + "/", url.replace("www.", "user@www."),
                        "https://127.0.0.1/path", "https://evil.invalid/path"):
            with self.subTest(url=variant):
                with mock.patch.object(self.b, "_public_address") as dns:
                    with self.assertRaises(self.b.BenchmarkError):
                        self.b.default_fetch(variant, 8, 1_000_000, 120)
                    dns.assert_not_called()

    def test_dns_every_resolved_address_must_be_global_unicast(self):
        import socket

        def info(ip):
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            return family, socket.SOCK_STREAM, 6, "", (ip, 443)

        for ip in ("127.0.0.1", "10.0.0.1", "169.254.1.2", "0.0.0.0",
                   "192.0.2.1", "224.0.0.1", "::1", "fc00::1", "fe80::1",
                   "::ffff:127.0.0.1", "not-an-ip"):
            with self.subTest(ip=ip), mock.patch.object(
                    socket, "getaddrinfo", return_value=[info("8.8.8.8"), info(ip)]):
                with self.assertRaises(self.b.BenchmarkError):
                    self.b._public_address("fixture.invalid")
        with mock.patch.object(socket, "getaddrinfo", return_value=[info("8.8.8.8")]):
            self.assertEqual(self.b._public_address("fixture.invalid"), (socket.AF_INET, "8.8.8.8"))
        with mock.patch.object(socket, "getaddrinfo", return_value=[]):
            with self.assertRaises(self.b.BenchmarkError):
                self.b._public_address("fixture.invalid")

    def test_headers_refuse_redirect_nontext_status_and_declared_oversize(self):
        self.b._check_headers(200, "text/markdown; charset=utf-8", "10", 10)
        for status, ctype, size in ((301, "text/plain", "0"), (302, "text/plain", "0"),
                                     (404, "text/plain", "0"), (200, "text/html", "0"),
                                     (200, "", "0"), (200, "text/plain", "11")):
            with self.subTest(status=status, ctype=ctype, size=size):
                with self.assertRaises(self.b.BenchmarkError):
                    self.b._check_headers(status, ctype, size, 10)

    def test_python_transport_pins_ip_keeps_tls_hostname_no_auth_and_bounds_bytes(self):
        import http.client
        import socket
        import ssl

        pair = self.b.PAIRS[0]
        response = mock.Mock(status=200)
        response.getheader.side_effect = lambda name: (
            "text/markdown" if name == "Content-Type" else None)
        response.read.side_effect = [b"fixture", b""]
        sock, tls = mock.Mock(), mock.Mock()
        with mock.patch.object(socket, "create_connection", return_value=sock) as create, \
                mock.patch.object(ssl.SSLContext, "wrap_socket", return_value=tls) as wrap, \
                mock.patch.object(http.client.HTTPSConnection, "request", autospec=True,
                                  side_effect=lambda conn, *a, **k: conn.connect()) as request, \
                mock.patch.object(http.client.HTTPSConnection, "getresponse", return_value=response):
            raw = self.b._python_fetch(pair["url"], "www.benchleader.com", "/fixed",
                                       (socket.AF_INET, "8.8.8.8"), 8, 100, 120)
        self.assertEqual(raw, b"fixture")
        create.assert_called_once_with(("8.8.8.8", 443), 8)
        wrap.assert_called_once_with(sock, server_hostname="www.benchleader.com")
        headers = request.call_args.kwargs["headers"]
        self.assertEqual(set(headers), {"User-Agent", "Accept", "Accept-Encoding", "Connection"})
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertEqual(request.call_args.args[1:3], ("GET", "/fixed"))
        self.assertEqual(response.read.call_args_list, [mock.call(65536), mock.call(65536)])
        tls.close.assert_called_once()

    def test_python_body_cap_and_end_of_read_total_deadline_are_enforced(self):
        import http.client

        response = mock.Mock(status=200)
        response.getheader.side_effect = lambda name: "text/plain" if name == "Content-Type" else None
        with mock.patch.object(http.client.HTTPSConnection, "request"), \
                mock.patch.object(http.client.HTTPSConnection, "getresponse", return_value=response):
            response.read.side_effect = [b"x" * 11]
            with self.assertRaisesRegex(self.b.BenchmarkError, "large"):
                self.b._python_fetch("", "fixture.invalid", "/", (2, "8.8.8.8"), 8, 10, 120)
            # Simulate a slow final read: the request must not be accepted
            # when it completes AFTER the shared total deadline.
            def slow_eof(_size):
                self.b.time.monotonic.return_value = 121
                return b""
            response.read.side_effect = slow_eof
            with self.assertRaisesRegex(self.b.BenchmarkError, "deadline"):
                self.b._python_fetch("", "fixture.invalid", "/", (2, "8.8.8.8"), 8, 10, 120)

    def test_python_tls_verification_failure_only_uses_verified_fallback_same_pin(self):
        import ssl

        pair = self.b.PAIRS[0]
        for error, fallback in ((ssl.SSLCertVerificationError("untrusted certificate"), True),
                                (TimeoutError("timeout"), False),
                                (self.b.BenchmarkError("redirect refused"), False)):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(self.b, "_public_address", return_value=(2, "8.8.8.8")), \
                    mock.patch.object(self.b, "_python_fetch", side_effect=error), \
                    mock.patch.object(self.b, "_curl_fetch", return_value=b"fixture") as curl:
                if fallback:
                    self.assertEqual(self.b.default_fetch(pair["url"], 8, 100, 120), b"fixture")
                    self.assertEqual(curl.call_args.args,
                                     (pair["url"], "www.benchleader.com", (2, "8.8.8.8"), 8, 100, 120))
                else:
                    with self.assertRaises(self.b.BenchmarkError):
                        self.b.default_fetch(pair["url"], 8, 100, 120)
                    curl.assert_not_called()

    def test_default_fetch_rechecks_shared_deadline_after_dns_before_connecting(self):
        pair = self.b.PAIRS[0]

        def slow_dns(_host):
            self.b.time.monotonic.return_value = 121
            return 2, "8.8.8.8"

        with mock.patch.object(self.b, "_public_address", side_effect=slow_dns), \
                mock.patch.object(self.b, "_python_fetch", return_value=b"fixture") as fetch:
            with self.assertRaisesRegex(self.b.BenchmarkError, "deadline"):
                self.b.default_fetch(pair["url"], 8, 100, 120)
            fetch.assert_not_called()

    def test_expired_budget_never_starts_dns_and_late_download_is_refused(self):
        pair = self.b.PAIRS[0]
        with mock.patch.object(self.b, "_public_address") as dns:
            self.b.time.monotonic.return_value = 121
            with self.assertRaisesRegex(self.b.BenchmarkError, "before DNS"):
                self.b.default_fetch(pair["url"], 8, 100, 120)
            dns.assert_not_called()

        self.b.time.monotonic.return_value = 100

        def late_download(*args):
            self.b.time.monotonic.return_value = 121
            return b"complete synthetic body"

        with mock.patch.object(self.b, "_public_address", return_value=(2, "8.8.8.8")), \
                mock.patch.object(self.b, "_python_fetch", side_effect=late_download):
            with self.assertRaisesRegex(self.b.BenchmarkError, "after download"):
                self.b.default_fetch(pair["url"], 8, 100, 120)

    def test_bounded_dns_uses_daemon_and_abandons_timeout_result(self):
        # A fake worker models the timeout deterministically: no actual DNS,
        # sleeping or test-owned background thread is needed.
        worker = mock.Mock()
        worker.is_alive.return_value = True
        with mock.patch.object(self.b.threading, "Thread", return_value=worker) as thread, \
                mock.patch.object(self.b.socket, "getaddrinfo") as dns:
            with self.assertRaisesRegex(self.b.BenchmarkError, "DNS lookup exceeded"):
                self.b._getaddrinfo_bounded("fixture.invalid", 8)
            dns.assert_not_called()
        self.assertTrue(thread.call_args.kwargs["daemon"])
        worker.start.assert_called_once_with()
        worker.join.assert_called_once_with(8)

    def test_curl_late_successful_response_is_refused(self):
        pair = self.b.PAIRS[0]
        proc = mock.Mock()
        proc.stdout.read1.side_effect = [
            b"fixture" + self.b._CURL_MARKER.encode() + b"200 0 text/markdown", b""]
        proc.poll.return_value = 0

        def late_wait(*args, **kwargs):
            self.b.time.monotonic.return_value = 121
            return 0

        proc.wait.side_effect = late_wait
        with mock.patch.object(subprocess, "Popen", return_value=proc):
            with self.assertRaisesRegex(self.b.BenchmarkError, "deadline"):
                self.b._curl_fetch(pair["url"], "www.benchleader.com", (2, "8.8.8.8"), 8, 100, 120)

    def test_curl_fallback_no_auth_config_redirects_insecure_tls_or_proxy_and_exact_limits(self):
        pair = self.b.PAIRS[0]
        proc = mock.Mock()
        proc.stdout.read1.side_effect = [b"fixture" + self.b._CURL_MARKER.encode()
                                       + b"200 0 text/markdown", b""]
        proc.wait.return_value = proc.poll.return_value = 0
        with mock.patch.object(subprocess, "Popen", return_value=proc) as popen:
            raw = self.b._curl_fetch(pair["url"], "www.benchleader.com", (2, "8.8.8.8"), 8, 100, 120)
        self.assertEqual(raw, b"fixture")
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1], "-q")
        for opt, value in (("--proto", "=https"), ("--max-redirs", "0"),
                           ("--resolve", "www.benchleader.com:443:8.8.8.8"),
                           ("--connect-timeout", "8"), ("--max-time", "20"),
                           ("--max-filesize", "100")):
            self.assertEqual(argv[argv.index(opt) + 1], value)
        self.assertFalse(set(argv) & {"-k", "--insecure", "-L", "--location", "--cookie", "--user"})
        self.assertEqual(popen.call_args.kwargs["env"], {"PATH": "/usr/bin:/bin"})
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_curl_response_trailer_redirect_and_oversize_fail_closed(self):
        pair = self.b.PAIRS[0]
        for output in (b"no trailer", b"x" + self.b._CURL_MARKER.encode() + b"302 0 text/plain",
                       b"x" + self.b._CURL_MARKER.encode() + b"200 1 text/plain",
                       b"x" * 101 + self.b._CURL_MARKER.encode() + b"200 0 text/plain"):
            with self.subTest(output=output[:60]):
                proc = mock.Mock()
                proc.stdout.read1.side_effect = [output, b""]
                proc.wait.return_value = proc.poll.return_value = 0
                with mock.patch.object(subprocess, "Popen", return_value=proc):
                    with self.assertRaises(self.b.BenchmarkError):
                        self.b._curl_fetch(pair["url"], "www.benchleader.com", (2, "8.8.8.8"), 8, 100, 120)

    def test_support_requires_both_explicit_model_efforts_to_match_the_route(self):
        ctx = self.refreshed()
        for current, candidate, expected in (
                ("max", "max", True), ("max", "low", False), ("low", "max", False),
                ("low", "low", False), (None, "max", False), ("max", None, False), (None, None, False)):
            with self.subTest(current=current, candidate=candidate):
                comp = self.b.comparison(ctx, "gpt-6.1-sol", NEW, current, candidate)
                self.assertEqual(comp["supports"], expected)
                self.assertEqual(len(comp["fresh"]), 2, "mismatches remain visible as context")
                leader = next(e for e in comp["fresh"] if e["provider"] == "BenchLeader")
                self.assertEqual(leader["setup_matches"], expected)
                self.assertEqual(leader["efforts"], {"gpt-6.1-sol": "max", NEW: "max"})
                self.assertFalse(next(e for e in comp["fresh"] if e["provider"] == "BenchLM")["setup_matches"])

        # Matching means each model's own level, not an invented requirement
        # that the two models must have identical levels.
        self.cache.unlink()
        pair = self.b.PAIRS[1]
        ctx = self.refreshed(fetcher=lambda url, *a: comparison_fixture(
            pair, efforts=("max", "low")) if url == pair["url"] else b"unrecognized")
        self.assertTrue(self.b.comparison(ctx, "gpt-6.1-sol", NEW, "max", "low")["supports"])
        self.assertFalse(self.b.comparison(ctx, "gpt-6.1-sol", NEW, "max", "high")["supports"])
        for efforts in ((None, "max"), ("max", None)):
            with self.subTest(source_efforts=efforts):
                self.cache.unlink()
                ctx = self.refreshed(fetcher=lambda url, *a: comparison_fixture(
                    pair, efforts=efforts) if url == pair["url"] else b"unrecognized")
                self.assertFalse(self.b.comparison(ctx, "gpt-6.1-sol", NEW, "max", "max")["supports"])

    def test_unknown_source_effort_with_higher_score_is_context_only(self):
        pair = self.b.PAIRS[3]
        raw = comparison_fixture(pair, values=(("Terminal-Bench 4.0", "70%", "80%"),))
        ctx = self.refreshed(fetcher=lambda url, *a: raw if url == pair["url"] else b"unrecognized")
        comp = self.b.comparison(ctx, "gpt-6.1-sol", NEW, "xhigh", "xhigh")
        self.assertFalse(comp["supports"])
        self.assertEqual(len(comp["fresh"]), 1)
        self.assertFalse(comp["fresh"][0]["setup_matches"])
        metric = next(m for m in comp["fresh"][0]["metrics"] if m["key"] == "terminal_bench_40")
        self.assertEqual((metric["current"], metric["candidate"]), (70, 80))
        self.assertTrue(metric["comparable"], "numeric context is not erased or turned into a support claim")

    def test_long_malicious_heading_is_skipped_valid_pages_remain_safe(self):
        self.assertEqual(self.b.MAX_LINE_CHARS, 20_000)
        noise = "## " + " " * (self.b.MAX_LINE_CHARS + 100) + "\x1b[31m\u202euntrusted"
        self.assertIsNone(self.b._heading(noise))
        self.assertEqual(self.b._heading("####\tExpected heading"), (4, "Expected heading"))
        self.assertIsNone(self.b._heading("####### Too many levels"))
        self.assertIsNone(self.b._heading("##No separator"))
        for pair in self.b.PAIRS:
            with self.subTest(pair=pair["id"]):
                raw = comparison_fixture(pair)
                title = "# %s vs %s\n" % tuple(n for _mid, n in pair["models"])
                attack = raw.replace(title.encode(), (title + noise + "\n").encode(), 1)
                self.assertEqual(self.b.parse_page(pair, attack), self.b.parse_page(pair, raw))
                self.assertNotIn("untrusted", repr(self.b.parse_page(pair, attack)))
        self.assertEqual(self.b._lines("x" * 20_000 + "\n" + "y" * 20_001),
                         ["x" * 20_000, ""])

    def test_oversized_cells_and_scores_are_unknown_not_invented(self):
        pair = self.b.PAIRS[1]
        raw = comparison_fixture(pair, values=(
            ("Terminal-Bench", "Terminal-Bench", "9" * 1500 + "%", "80%"),
            ("Coding score", "BenchLeader", "72", "82")))
        parsed = self.b.parse_page(pair, raw)
        values = parsed["metrics"]["terminal_bench"]["values"]
        self.assertIsNone(values["gpt-6.1-sol"]["value"])
        self.assertEqual(values[NEW]["value"], 80)
        bounded = self.b._cell("[label](https://unused.invalid/" + "x" * 25_000 + ")\x1b[31m\u202e")
        self.assertLessEqual(len(bounded), 300)
        self.assertNotRegex(bounded, r"[\x1b\u202a-\u202e]")


class TestBalancedBenchmarkRefresh(LiveRefreshCase):
    """The unadapted new default: actual CLI, offline official and comparison
    adapters, temporary settings only. No production-function replacements."""

    TEST_POLICY = None
    TEST_DETAILS = False
    machine = TestProposalScopedReview.machine
    frozen = TestProposalScopedReview.frozen
    protected_bytes = TestProposalScopedReview.protected_bytes

    def setUp(self):
        super().setUp()
        self.b, error = self.mod.load_benchmark_module()
        self.assertIsNone(error)
        self.benchmark_fetches = []
        self.benchmark_efforts = {"gpt-6.1-sol": "xhigh", NEW: "xhigh",
                                  "claude-sonnet-5.5": "high", "gpt-6.1-terra": "high"}
        self.benchmark_pages = {}
        self.mod.BENCHMARK_FETCHER = self.fetch_benchmarks
        # No savings or official differentiators: the compared options
        # below must come from actual, attributable synthetic benchmark rows.
        self.markdown = self.live_prices(new=12, cheap=15, new_long=18)

    def fetch_benchmarks(self, url, timeout, max_bytes):
        self.benchmark_fetches.append((url, timeout, max_bytes))
        pair = next(p for p in self.b.PAIRS if p["url"] == url)
        return self.benchmark_pages.get(url) or comparison_fixture(
            pair, efforts=tuple(self.benchmark_efforts[mid] for mid, _ in pair["models"]))

    def context(self, ages=None, benchmark_age=0):
        self.seed_cache()
        self.seed_docs(ages)
        self.b.refresh(self.paths.benchmarks, NOW - benchmark_age, True,
                       fetcher=lambda url, timeout, cap, deadline:
                       self.fetch_benchmarks(url, timeout, cap))
        return self.mod.build_context(self.paths, NOW)

    def offered(self, *args):
        rc, out, err = self.cli("propose", "--review", *args)
        self.assertEqual(rc, 3, out + err)
        doc = self.frozen(out)
        self.assertEqual(doc["policy"], "balanced")
        return doc, out

    def select_option(self, doc, route, model=NEW, now=NOW + 1):
        n = next(a["n"] for a in doc["alternatives"]
                 if a["route"] == route and a["values"]["model"] == model)
        rc, out, err = self.cli("select", "--proposal-id", doc["proposal_id"],
                                "--choose", str(n), now=now)
        self.assertEqual(rc, 0, out + err)
        return self.frozen(out), out

    def whole_home(self):
        return {str(p.relative_to(self.home)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.home.rglob("*") if p.is_file()}

    def benchmark_alt(self, ctx, current, candidate, current_effort, candidate_effort, n=1):
        return {"n": n, "current_model": current, "model": candidate,
                "values": {"model": candidate, "reasoning_effort": candidate_effort, "context_tier": "default"},
                "benchmark": self.mod.benchmark_comparison(
                    ctx, current, candidate, current_effort, candidate_effort)}

    def install_managed_drift(self, key="planner", model=None):
        """Test-owned, synchronized prior-apply fixture; no production writes
        or user files are used to manufacture the starting managed state."""
        model = model or self.defaults[key]["model"]
        values = dict(self.defaults[key], model=model, reasoning_effort="low", context_tier="default")
        state = self.mod.blank_state()
        state["overrides"] = {key: values}
        native = {"model": model, "effortLevel": "low", "contextTier": "default"}
        state["managed_settings"] = {key: native}
        settings = copy.deepcopy(self.initial_settings)
        settings["subagents"]["agents"][key] = native
        self.write_json(self.paths.settings, settings)
        self.write_json(self.paths.state, state)
        ctx = self.context()
        plan = self.mod.plan_outputs(ctx, state["overrides"])
        self.write(self.paths.instructions, plan["instructions"])
        self.write(self.paths.effective, plan["effective_doc"])
        self.write_json(self.paths.settings, plan["settings_data"])
        state["managed_settings"] = plan["managed_settings"]
        state["instructions_sha256"] = self.mod.sha256_bytes(plan["instructions"])
        self.write_json(self.paths.state, state)
        self.assertFalse(self.mod._sync_needed(self.mod.build_context(self.paths, NOW), plan))
        return values

    def test_default_preview_and_propose_have_no_preselection_even_with_savings(self):
        self.markdown = self.live_prices(new=5, cheap=6)
        before = self.protected_bytes()
        rc, preview, err = self.cli("preview")
        self.assertEqual(rc, 3, preview + err)
        self.assertFalse(Path(self.paths.proposal).exists())
        self.assertIn("balanced mode", preview)
        self.assertIn("NONE is preselected", preview)
        self.assertNotIn("CHANGE proposed", preview)
        doc, out = self.offered()
        self.assertEqual(doc["selected"], [])
        self.assertEqual(doc["route_changes"], [])
        self.assertEqual(doc["overrides"], {})
        self.assertEqual(self.protected_bytes(), before)
        self.assertEqual(self.mod.DEFAULT_POLICY, "balanced")
        self.assertEqual(self.machine(out, "CHECK"), [])
        self.assertEqual(self.machine(out, "LEVEL"), [])
        self.assertEqual(self.machine(out, "REVIEW"), [["complete"]])
        self.assertEqual(self.machine(out, "SELECTED"), [["0"]])
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(len(self.docs_fetches), 2)
        self.assertEqual(len(self.benchmark_fetches), 4)

    def test_explicit_cost_led_retains_legacy_automatic_saving_proposals(self):
        self.markdown = self.live_prices(new=5, cheap=6)
        before = self.protected_bytes()
        rc, out, err = self.cli("propose", "--review", "--cost-led")
        self.assertEqual(rc, 0, out + err)
        doc = self.frozen(out)
        self.assertEqual(doc["policy"], "cost-led")
        self.assertEqual(set(doc["overrides"]), set(self.defaults))
        self.assertTrue(all(v["model"] == NEW for v in doc["overrides"].values()))
        self.assertIn("CHANGE proposed", out)
        self.assertEqual(self.protected_bytes(), before)

    def test_real_comparisons_offer_sol_and_terra_only_for_exact_pairs(self):
        for effort in ("medium", "xhigh"):
            with self.subTest(source_effort=effort):
                self.benchmark_efforts.update({"gpt-6.1-sol": effort, NEW: effort})
                Path(self.paths.benchmarks).unlink(missing_ok=True)
                rows = self.rows(self.context())
                self.assertTrue(all(r["proposed"] is None for r in rows.values()))
                for key in ("planner", "coder", "tester"):
                    options = [a for a in rows[key]["alternatives"] if a["model"] == NEW]
                    if self.defaults[key]["reasoning_effort"] != effort:
                        self.assertEqual(options, [])
                        continue
                    self.assertEqual(len(options), 1)
                    alt = options[0]
                    self.assertEqual(alt["bases"], ["benchmark"])
                    self.assertEqual(alt["values"], dict(self.defaults[key], model=NEW))
                    self.assertTrue(alt["benchmark"]["supports"])
                for key in ("reviewer", "test-reviewer"):
                    alt = next(a for a in rows[key]["alternatives"] if a["model"] == "gpt-6.1-terra")
                    self.assertEqual(alt["bases"], ["benchmark"])
                    self.assertEqual(alt["values"], dict(self.defaults[key], model="gpt-6.1-terra"))
                self.assertFalse(rows["reviewer.stronger"]["alternatives"])
                self.assertFalse(rows["discovery"]["alternatives"])
                self.assertFalse(rows["task"]["alternatives"])

    def test_compact_first_has_scores_sources_setup_caveats_and_no_full_report(self):
        doc, out = self.offered()
        self.assertNotIn("===== Details (full report) =====", out)
        self.assertNotIn("Reasons and evidence:", out)
        self.assertNotIn("Official rates (Default tier", out)
        self.assertLess(out.index("Current routes"), out.index("Options —"))
        for text in ("BenchLeader", "BenchLM", "70%", "80%", "86%", "page updated 2026-11-16",
                     "compared at gpt-6.1-sol 'xhigh'", "reasoning effort and harness not stated",
                     "same levels as this route", "not independent confirmation", "no average, overall winner",
                     "not proof of", "different directions", "composite scores disagree",
                     "NOT comprehensive", "official $/1M", "not your bill"):
            self.assertIn(text, out)
        for pair in self.b.PAIRS:
            self.assertIn(pair["page"], out)
        for route in self.mod.ROUTES:
            self.assertIn(route["label"], out)
        self.assertNotIn("irrelevant remote winner", out)
        self.assertNotIn("Synthetic fixture tasks", out)
        self.assertEqual(doc["format"], 5)
        self.assertIsNone(doc["draft"], "plain helper proposals retain explicit selection")
        self.assertIn("benchmarks", doc["digests"])

    def test_details_are_full_cached_read_only_and_match_numbering(self):
        doc, compact = self.offered()
        before, calls = self.whole_home(), list(self.benchmark_fetches)
        with mock.patch.object(self.mod, "refresh_pricing", side_effect=AssertionError("details fetched")), \
                mock.patch.object(self.mod, "_fetch_capability_bytes", side_effect=AssertionError("details fetched")), \
                mock.patch.object(self.b, "default_fetch", side_effect=AssertionError("details fetched")), \
                mock.patch.object(self.b, "_write_cache", side_effect=AssertionError("details wrote")):
            rc, out, err = self.cli("details", "--proposal-id", doc["proposal_id"])
        self.assertEqual(rc, 0, out + err)
        self.assertIn("numbers below match it", out)
        self.assertIn("===== Details (full report) =====", out)
        self.assertIn("Reasons and evidence:", out)
        self.assertIn("sha256", out)
        self.assertIn("===== Recap", out)
        self.assertGreater(len(out), len(compact))
        self.assertEqual(self.machine(out, "PROPOSAL"), [])
        self.assertEqual(self.machine(out, "ALTERNATIVE"), [])
        self.assertEqual(self.whole_home(), before)
        self.assertEqual(self.benchmark_fetches, calls)
        rc, out, err = self.cli("propose", "--review", "--details")
        self.assertEqual(rc, 3, out + err)
        self.assertLess(out.index("Current routes"), out.index("===== Details (full report) ====="))

    def test_details_on_expired_or_tampered_proposal_never_deletes_or_repairs_it(self):
        for failure in ("expired", "tampered"):
            with self.subTest(failure=failure):
                doc, _ = self.offered()
                if failure == "tampered":
                    doc["policy"] = "cost-led"
                    self.write_json(self.paths.proposal, doc)
                before = self.whole_home()
                rc, out, err = self.cli("details", "--proposal-id", doc["proposal_id"],
                                        now=NOW + 901 if failure == "expired" else NOW)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("not used", out)
                self.assertEqual(self.whole_home(), before)

    def test_missing_optional_module_and_transport_error_leave_official_behavior_intact(self):
        before = self.protected_bytes()
        for failure in ("module", "transport"):
            with self.subTest(failure=failure):
                if failure == "module":
                    patch = mock.patch.object(self.mod, "load_benchmark_module",
                                              return_value=(None, "fixture module unavailable"))
                else:
                    Path(self.paths.benchmarks).unlink(missing_ok=True)
                    patch = mock.patch.object(self.mod, "BENCHMARK_FETCHER",
                                              side_effect=TimeoutError("fixture unavailable"))
                with patch:
                    rc, out, err = self.cli("propose", "--review")
                self.assertEqual(rc, 3, out + err)
                self.assertNotIn("PROPOSAL\t", out)
                self.assertIn("unavailable" if failure == "module" else "refresh(es) failed", out)
                self.assertEqual(self.protected_bytes(), before)

    def test_injected_pricing_without_benchmark_adapter_cannot_make_real_traffic(self):
        self.mod.BENCHMARK_FETCHER = None
        with mock.patch.object(self.b, "default_fetch") as transport:
            rc, out, err = self.cli("propose", "--review")
        self.assertEqual(rc, 3, out + err)
        transport.assert_not_called()
        self.assertIn("injected pricing transport has no comparison transport", out)
        self.assertFalse(Path(self.paths.benchmarks).exists())

    def test_offline_and_both_fetch_switches_prevent_benchmark_requests(self):
        self.context()
        self.benchmark_fetches.clear()
        for environment, flags in (({}, ("--offline",)),
                                   ({self.b.FETCH_ENV: "0"}, ()),
                                   ({"COPILOT_TASK_REPORT_PRICING_FETCH": "0"}, ())):
            with self.subTest(environment=environment, flags=flags):
                with mock.patch.dict(os.environ, environment):
                    before = Path(self.paths.benchmarks).read_bytes()
                    rc, out, err = self.cli("propose", "--review", *flags, now=NOW + 301)
                self.assertEqual(rc, 3, out + err)
                self.assertEqual(self.benchmark_fetches, [])
                self.assertEqual(Path(self.paths.benchmarks).read_bytes(), before)
                self.assertIn("4 of 4 reviewed comparison pages fresh", out)

    def test_stale_future_and_malformed_comparisons_cannot_offer_premium_options(self):
        for age in (DAY + 1, -1):
            with self.subTest(age=age):
                Path(self.paths.benchmarks).unlink(missing_ok=True)
                ctx = self.context(benchmark_age=age)
                self.assertTrue(all(not r["alternatives"] for r in self.rows(ctx).values()))
                rc, out, err = self.cli("propose", "--review", "--offline")
                self.assertEqual(rc, 3, out + err)
                self.assertNotIn("PROPOSAL\t", out)
                self.assertIn("stale/future-dated", out)
        Path(self.paths.benchmarks).write_text('{"schema_version":1,"schema_version":1}')
        rc, out, err = self.cli("propose", "--review", "--offline")
        self.assertEqual(rc, 3, out + err)
        self.assertNotIn("PROPOSAL\t", out)
        self.assertIn("cache is invalid", out)

    def test_benchmark_cannot_override_category_context_pins_price_or_exact_effort_gates(self):
        self.benchmark_efforts.update({"gpt-6.1-sol": "medium", NEW: "medium"})
        for change in ("category", "context", "effort", "price", "pin", "stale-docs"):
            with self.subTest(change=change):
                # Restore fixture inputs only; do not repair implementation state.
                self.config["verified_capabilities"]["models"][NEW] = {
                    "reasoning_levels": ["low", "medium", "high", "xhigh"]}
                self.config.pop("pins", None)
                self.doc_facts[NEW]["one_million_context"] = "Supported"
                self.markdown = self.live_prices(new=12, cheap=15, new_long=18)
                self.catalog["expires_on"] = "2026-12-15"
                if change == "category":
                    self.markdown = self.markdown.replace(
                        ("| %s | GA | Powerful" % NEW).encode(),
                        ("| %s | GA | Lightweight" % NEW).encode())
                elif change == "context":
                    self.doc_facts[NEW]["one_million_context"] = "Not supported"
                elif change == "effort":
                    self.config["verified_capabilities"]["models"][NEW]["reasoning_levels"] = ["low"]
                elif change == "price":
                    self.markdown = b"\n".join(line for line in self.markdown.splitlines()
                                              if not line.startswith(("| %s |" % NEW).encode()))
                elif change == "pin":
                    self.config["pins"] = {"planner": {"model": self.defaults["planner"]["model"]}}
                else:
                    self.catalog["expires_on"] = "2026-11-15"
                self.save_config()
                self.save_catalog()
                ctx = self.context({"supported_models": 4 * DAY, "model_comparison": 4 * DAY}
                                   if change == "stale-docs" else None)
                row = self.rows(ctx)["planner"]
                self.assertFalse(any(a["model"] == NEW for a in row["alternatives"]), row)
                self.assertIsNone(row["proposed"])

    def test_long_context_saving_requires_both_tiers_even_with_benchmark_support(self):
        self.benchmark_efforts.update({"gpt-6.1-sol": "medium", NEW: "medium"})
        self.markdown = self.live_prices(new=5, cheap=15, new_long=18)
        rows = self.rows(self.context())
        planner = next(a for a in rows["planner"]["alternatives"] if a["model"] == NEW)
        coder = next(a for a in rows["coder"]["alternatives"] if a["model"] == NEW)
        self.assertNotIn("saving", planner["bases"])
        self.assertIn("benchmark", planner["bases"])
        self.assertIn("saving", coder["bases"])
        self.assertEqual((planner["cost"], planner["long_cost"]), (5, 18))
        self.mod.number_alternatives(list(rows.values()))
        rendered = "\n".join(self.mod.render_options(self.mod.build_context(self.paths, NOW),
                                                    list(rows.values())))
        self.assertIn("above the long-context threshold $10.00 -> $18.00", rendered)
        self.assertIn("+$8.00/1M, +80%", rendered)

    def test_only_explicit_selection_changes_one_route_then_apply_and_replay_refuses(self):
        before = self.protected_bytes()
        doc, _ = self.offered()
        selected, out = self.select_option(doc, "tester")
        self.assertEqual(selected["route_changes"], ["tester"])
        self.assertEqual(selected["overrides"], {"tester": dict(self.defaults["tester"], model=NEW)})
        self.assertEqual(selected["created_epoch"], doc["created_epoch"])
        self.assertEqual(selected["expires_epoch"], doc["expires_epoch"])
        self.assertNotEqual(selected["seal"], doc["seal"])
        self.assertEqual(self.protected_bytes(), before)
        self.assertIn("costs MORE", out)
        fetched = (len(self.fetches), len(self.docs_fetches), len(self.benchmark_fetches))
        rc, out, err = self.cli("apply", "--proposal-id", selected["proposal_id"], now=NOW + 2)
        self.assertEqual(rc, 0, out + err)
        state = self.read_json(self.paths.state)
        self.assertEqual(set(state["overrides"]), {"tester"})
        self.assertEqual(state["overrides"]["tester"]["model"], NEW)
        self.assertEqual(self.read_json(self.paths.user_config), self.config)
        self.assertEqual((len(self.fetches), len(self.docs_fetches), len(self.benchmark_fetches)), fetched)
        after = self.routing_bytes()
        rc, out, err = self.cli("apply", "--proposal-id", selected["proposal_id"], now=NOW + 3)
        self.assertEqual(rc, 4, out + err)
        self.assertEqual(self.routing_bytes(), after)

    def test_selection_before_verification_only_selected_model_exact_level_questions(self):
        self.config.pop("available_models")
        self.config["verified_capabilities"]["models"].pop(NEW)
        self.save_config()
        before = self.protected_bytes()
        doc, out = self.offered()
        self.assertEqual(self.machine(out, "CHECK"), [])
        self.assertEqual(self.machine(out, "LEVEL"), [])
        selected, out = self.select_option(doc, "tester")
        self.assertEqual(self.machine(out, "CHECK"), [[NEW, "tester"]])
        self.assertEqual(self.machine(out, "LEVEL"), [[NEW, "xhigh"]])
        rc, out, err = self.cli("review", "--proposal-id", selected["proposal_id"],
                                "--available-all", now=NOW + 1)
        self.assertEqual(rc, 0, out + err)
        checked = self.frozen(out)
        self.assertEqual(self.machine(out, "CHECK"), [])
        self.assertEqual(self.machine(out, "LEVEL"), [[NEW, "xhigh"]])
        rc, out, err = self.cli("review", "--proposal-id", checked["proposal_id"],
                                "--level", NEW + ":xhigh=yes", now=NOW + 2)
        self.assertEqual(rc, 0, out + err)
        completed = self.frozen(out)
        self.assertEqual(self.machine(out, "REVIEW"), [["complete"]])
        self.assertEqual(self.protected_bytes(), before)
        rc, out, err = self.cli("apply", "--proposal-id", completed["proposal_id"], now=NOW + 3)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read_json(self.paths.user_config), self.config)
        self.assertFalse(Path(self.paths.verified).exists())

    def test_tentative_selection_cannot_apply_before_exact_verification(self):
        self.config.pop("available_models")
        self.save_config()
        doc, _ = self.offered()
        selected, _ = self.select_option(doc, "tester")
        before = self.protected_bytes()
        rc, out, err = self.cli("apply", "--proposal-id", selected["proposal_id"])
        self.assertEqual(rc, 4, out + err)
        self.assertIn("not confirmed", err)
        self.assertEqual(self.protected_bytes(), before)

    def test_rejection_drops_selection_without_downgrade_or_replacing_with_cheapest(self):
        self.config.pop("available_models")
        self.config["verified_capabilities"]["models"].pop(NEW)
        self.save_config()
        before = self.protected_bytes()
        for rejection in ("availability", "effort"):
            with self.subTest(rejection=rejection):
                doc, _ = self.offered()
                selected, _ = self.select_option(doc, "tester")
                flags = ("--unavailable", NEW) if rejection == "availability" else (
                    "--available-all", "--level", NEW + ":xhigh=no")
                rc, out, err = self.cli("review", "--proposal-id", selected["proposal_id"],
                                        *flags, now=NOW + 1)
                self.assertEqual(rc, 3, out + err)
                if self.machine(out, "PROPOSAL"):
                    dropped = self.frozen(out)
                    self.assertEqual(dropped["selected"], [])
                    self.assertEqual(dropped["overrides"], {})
                self.assertIn("Dropped:", out)
                self.assertEqual(self.protected_bytes(), before)

    def test_benchmark_cache_changes_or_expiry_refuse_select_review_apply_without_fetch(self):
        for command in ("select", "review", "apply"):
            for mutation in ("metric", "identity", "expiry", "registry", "effort"):
                with self.subTest(command=command, mutation=mutation):
                    self.config["available_models"] = {"verified_on": "2026-11-16", "models": self.ids}
                    if command == "review":
                        self.config.pop("available_models")
                    self.save_config()
                    Path(self.paths.benchmarks).unlink(missing_ok=True)
                    self.context(benchmark_age=DAY - 100 if mutation == "expiry" else 0)
                    doc, _ = self.offered("--offline")
                    if command in ("review", "apply"):
                        doc, _ = self.select_option(doc, "tester", now=NOW)
                    if mutation != "expiry":
                        cache = self.read_json(self.paths.benchmarks)
                        pid = self.b.PAIRS[1]["id"]
                        if mutation == "metric":
                            cache["records"][pid]["metrics"]["terminal_bench"]["values"][NEW]["value"] = 81
                        elif mutation == "identity":
                            cache["records"][pid]["url"] += "?changed"
                        elif mutation == "effort":
                            cache["records"][pid]["efforts"] = {"gpt-6.1-sol": "medium", NEW: "medium"}
                        else:
                            cache["registry"] = "0" * 64
                        self.write_json(self.paths.benchmarks, cache)
                    before = self.protected_bytes()
                    calls = (len(self.fetches), len(self.docs_fetches), len(self.benchmark_fetches))
                    flags = ("--choose", "1") if command == "select" else (
                        ("--available-all",) if command == "review" else ())
                    rc, out, err = self.cli(command, "--proposal-id", doc["proposal_id"], *flags,
                                            now=NOW + 101 if mutation == "expiry" else NOW + 1)
                    self.assertEqual(rc, 4, out + err)
                    self.assertIn("changed", err)
                    self.assertEqual(self.protected_bytes(), before)
                    self.assertEqual((len(self.fetches), len(self.docs_fetches),
                                      len(self.benchmark_fetches)), calls)

    def test_tampered_policy_and_old_frozen_v3_are_refused(self):
        for mutation in ("policy", "v3"):
            with self.subTest(mutation=mutation):
                doc, _ = self.offered()
                if mutation == "policy":
                    doc["policy"] = "cost-led"
                else:
                    doc["format"] = 3
                self.write_json(self.paths.proposal, doc)
                before = self.protected_bytes()
                rc, out, err = self.cli("select", "--proposal-id", doc["proposal_id"], "--choose", "1")
                self.assertEqual(rc, 4, out + err)
                self.assertIn("altered" if mutation == "policy" else "older version", err)
                self.assertEqual(self.protected_bytes(), before)

    def test_frozen_workload_policy_change_refuses_even_when_equal_rates_hide_cost_difference(self):
        doc, _ = self.offered()
        doc, _ = self.select_option(doc, "tester")
        before = self.protected_bytes()
        changed_mix = (("input", 0.8), ("cached_input", 0.1), ("cache_write", 0.05), ("output", 0.05))
        with mock.patch.object(self.mod, "WORKLOAD_MIX", changed_mix):
            rc, out, err = self.cli("apply", "--proposal-id", doc["proposal_id"], now=NOW + 2)
        self.assertEqual(rc, 4, out + err)
        self.assertEqual(self.protected_bytes(), before)

    def test_frozen_threshold_and_availability_age_changes_refuse_unchanged_route_decisions(self):
        # Neither change affects these fresh, benchmark-only fixtures; the
        # refusal must come from binding the policy, not changed winners.
        for name, value in (("MIN_SAVING_FRACTION", 0.2), ("AVAILABILITY_MAX_AGE_DAYS", 31)):
            with self.subTest(policy=name):
                doc, _ = self.offered()
                doc, _ = self.select_option(doc, "tester")
                before = self.protected_bytes()
                with mock.patch.object(self.mod, name, value):
                    rc, out, err = self.cli("apply", "--proposal-id", doc["proposal_id"], now=NOW + 2)
                self.assertEqual(rc, 4, out + err)
                self.assertIn("economics changed", err)
                self.assertEqual(self.protected_bytes(), before)

    def test_terminal_color_is_localized_and_machine_lines_are_bare_exact(self):
        class TtyOutput(io.StringIO):
            def isatty(self):
                return True

        with mock.patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": "",
                                          self.mod.COLOR_ENV: "auto", "COLUMNS": "100"}):
            stream = TtyOutput()
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
                rc = self.mod.main(["propose", "--review"], now=NOW)
        self.assertEqual(rc, 3, stream.getvalue())
        out = stream.getvalue()
        self.assertIn("\x1b[1;36m#", out)
        self.assertIn("\x1b[1;35mthird-party:", out)
        self.assertIn("\x1b[1;33mCaution:", out)
        self.assertIn("\x1b[0m", out)
        doc = self.frozen(out)
        actual = [line for line in out.splitlines()
                  if line.startswith(("REVIEW\t", "SELECTED\t", "ALTERNATIVE\t", "PROPOSAL\t"))]
        expected = ["REVIEW\tcomplete", "SELECTED\t0"]
        expected += ["ALTERNATIVE\t%d\t%s\t%s" % (a["n"], a["route"], a["values"]["model"])
                     for a in doc["alternatives"]]
        expected += ["PROPOSAL\t" + doc["proposal_id"]]
        self.assertEqual(actual, expected)
        self.assertNotIn("\x1b", "\n".join(actual))

    def test_no_color_dumb_and_nonterminal_have_no_ansi(self):
        for environment in ({"TERM": "xterm", "NO_COLOR": "1", self.mod.COLOR_ENV: "always"},
                            {"TERM": "dumb", "NO_COLOR": "", self.mod.COLOR_ENV: "always"},
                            {"TERM": "xterm", "NO_COLOR": "", self.mod.COLOR_ENV: "auto"},
                            {"TERM": "xterm", "NO_COLOR": "", self.mod.COLOR_ENV: "never"}):
            with self.subTest(environment=environment), mock.patch.dict(os.environ, environment):
                rc, out, err = self.cli("propose", "--review")
                self.assertEqual(rc, 3, out + err)
                self.assertNotIn("\x1b", out + err)

    def test_width_wraps_without_truncating_names_prices_or_source_urls(self):
        self.benchmark_efforts.update({"gpt-6.1-sol": "medium", NEW: "medium"})
        for width in ("40", "80", "140"):
            with self.subTest(width=width), mock.patch.dict(os.environ, {"COLUMNS": width}):
                doc, out = self.offered()
                for model in (NEW, "gpt-6.1-sol", "claude-sonnet-5.5", "gpt-6.1-terra"):
                    self.assertIn(model, out)
                for text in ("$10.00", "$12.00", "$18.00"):
                    self.assertIn(text, out)
                for p in self.b.PAIRS:
                    self.assertIn(p["page"], out)
                self.assertNotIn("gpt-6.2-...", out)
                self.assertEqual(self.mod.term_width(), int(width))
        with mock.patch.dict(os.environ, {"COLUMNS": "1"}):
            self.assertEqual(self.mod.term_width(), 40)
        with mock.patch.dict(os.environ, {"COLUMNS": "1000"}):
            self.assertEqual(self.mod.term_width(), 140)

    def test_untrusted_cached_provenance_never_emits_terminal_controls_or_bidi(self):
        self.context()
        cache = self.read_json(self.paths.benchmarks)
        cache["records"][self.b.PAIRS[1]["id"]]["fetched_at"] = (
            "\x1b]8;;https://evil.invalid\x07forged\x1b]8;;\x07\u202e")
        self.write_json(self.paths.benchmarks, cache)
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            rc, out, err = self.cli("details")
        self.assertEqual(rc, 0, out + err)
        self.assertNotRegex(out, r"[\x1b\x00\u202a-\u202e\u2066-\u2069]")

    def test_mismatched_benchmark_does_not_block_independent_saving_or_official_option(self):
        self.benchmark_efforts.update({"gpt-6.1-sol": "max", NEW: "max"})
        for basis in ("saving", "official"):
            with self.subTest(basis=basis):
                self.markdown = self.live_prices(new=5 if basis == "saving" else 12, cheap=15)
                self.doc_facts["gpt-6.1-sol"]["copilot_cli_client"] = (
                    "Included" if basis == "saving" else "Not included")
                row = self.rows(self.context())["tester"]
                alt = next(a for a in row["alternatives"] if a["model"] == NEW)
                self.assertIn(basis, alt["bases"])
                self.assertNotIn("benchmark", alt["bases"])
                self.assertFalse(alt["benchmark"]["supports"])
                self.assertIsNone(row["proposed"])
                text = "\n".join(self.mod._bench_lines(alt, "xhigh", {}, "", 3))
                self.assertIn("setup differs", text)
                self.assertIn("context only", text)
                self.assertIn("compared at gpt-6.1-sol 'max'", text)
                self.assertNotIn("same levels as this route", text)

    def test_short_metrics_show_both_directions_and_exact_hidden_counts_per_source(self):
        for pair in self.b.PAIRS:
            if pair["provider"] == "benchleader":
                self.benchmark_pages[pair["url"]] = comparison_fixture(
                    pair, efforts=tuple(self.benchmark_efforts[mid] for mid, _ in pair["models"]), values=(
                        ("Terminal-Bench", "Terminal-Bench", "70%", "80%"),
                        ("Terminal-Bench 4.0 (AA)", "Artificial Analysis", "50%", "60%"),
                        ("Terminal-Bench 4.0 (Vals)", "Vals AI", "60%", "70%"),
                        ("FrontierCode 2.0 (AA)", "Artificial Analysis", "90%", "40%"),
                        ("SWE Atlas v1.1", "Scale AI SEAL", "90%", "50%"),
                        ("Coding score", "BenchLeader", "72", "72"),
                        ("Agents & tools score", "BenchLeader", "90", "n/a")))
            else:
                self.benchmark_pages[pair["url"]] = comparison_fixture(pair, values=(
                    ("Terminal-Bench 4.1", "86%", "70%"),
                    ("Terminal-Bench 4.0", "50%", "60%"),
                    ("FrontierCode 2.0", "70%", "70%")))
        ctx = self.context()
        alt = self.benchmark_alt(ctx, "gpt-6.1-sol", NEW, "xhigh", "xhigh")
        out = "\n".join(self.mod._bench_lines(alt, "xhigh", {}, "", 3))
        leader, lm = out.split("third-party: BenchLM", 1)
        self.assertIn("current gpt-6.1-sol 70% · option gpt-6.2-sol 80%", leader)
        self.assertIn("current gpt-6.1-sol 90% · option gpt-6.2-sol 40%", leader)
        self.assertIn("+4 more metric(s)", leader)
        self.assertIn("1 higher for the current model, 2 higher for the option, 0 equal, 1 not comparable", leader)
        self.assertIn("current gpt-6.1-sol 86% · option gpt-6.2-sol 70%", lm)
        self.assertIn("current gpt-6.1-sol 50% · option gpt-6.2-sol 60%", lm)
        self.assertIn("+1 more metric(s)", lm)
        self.assertIn("1 higher for the current model, 0 higher for the option, 0 equal, 0 not comparable", lm)
        self.assertIn("no overall winner", out)
        full = "\n".join(self.mod._bench_lines(alt, "xhigh", {}, "", None))
        self.assertIn("current gpt-6.1-sol 90% · option gpt-6.2-sol 50%", full)
        self.assertIn("not comparable: only one model has a value", full)
        self.assertNotIn("more metric(s)", full)

    def test_reversed_pairs_have_independent_labels_prices_and_repeated_directed_notes(self):
        sonnet, terra = "claude-sonnet-5.5", "gpt-6.1-terra"
        self.markdown = b"\n".join(line.replace(b"$10", b"$5") if line.startswith(
            ("| %s |" % sonnet).encode()) else line for line in self.markdown.splitlines())
        ctx = self.context()
        all_rows = self.mod.evaluate(ctx)
        self.mod.number_alternatives(all_rows)
        rows = [r for r in all_rows if r["key"] in ("coder.small_casual", "reviewer", "test-reviewer")]
        text = "\n".join(self.mod.render_options(ctx, rows))
        self.assertIn("current %s 80%% · option %s 70%%" % (terra, sonnet), text)
        self.assertIn("current %s 70%% · option %s 80%%" % (sonnet, terra), text)
        self.assertIn("-$5.00/1M, -50%", text)
        self.assertIn("+$5.00/1M, +100%", text)
        forward_n = next(a["n"] for r in rows if r["key"] == "reviewer"
                         for a in r["alternatives"] if a["model"] == terra)
        last = text[text.index("#%d " % next(a["n"] for r in rows if r["key"] == "test-reviewer"
                                           for a in r["alternatives"] if a["model"] == terra)):]
        self.assertIn("same comparison, same direction (current %s -> option %s) as option #%d" %
                      (sonnet, terra, forward_n), last)
        for note in ("same levels as this route", "context only", "different directions",
                     "composite scores disagree", "caution:"):
            self.assertIn(note, last)

    def test_repeated_directed_reference_restates_its_own_mismatching_setup(self):
        ctx = self.context()
        shown = {}
        first = self.benchmark_alt(ctx, "gpt-6.1-sol", NEW, "xhigh", "xhigh", 7)
        initial = "\n".join(self.mod._bench_lines(first, "xhigh", shown, "", 3))
        self.assertIn("same levels as this route", initial)
        later = self.benchmark_alt(ctx, "gpt-6.1-sol", NEW, "medium", "medium", 9)
        text = "\n".join(self.mod._bench_lines(later, "medium", shown, "", 3))
        self.assertIn("as option #7", text)
        self.assertIn("this route uses 'medium' -> 'medium'", text)
        self.assertIn("setup differs", text)
        self.assertNotIn("same levels as this route", text)
        for note in ("context only", "different directions", "composite scores disagree", "caution:"):
            self.assertIn(note, text)

    def test_same_model_alignment_is_explicit_and_removes_default_override_only_after_apply(self):
        old = self.install_managed_drift()
        before = self.protected_bytes()
        doc, out = self.offered()
        self.assertEqual(doc["selected"], [])
        self.assertEqual(doc["overrides"], {"planner": old})
        self.assertEqual(doc["route_changes"], [])
        self.assertIn("drift:", out)
        self.assertIn("same model", out)
        self.assertEqual(self.protected_bytes(), before)
        detail_before = self.whole_home()
        rc, details, err = self.cli("details", "--proposal-id", doc["proposal_id"])
        self.assertEqual(rc, 0, details + err)
        self.assertEqual(self.whole_home(), detail_before)
        selected, _ = self.select_option(doc, "planner", model=old["model"])
        self.assertEqual(selected["overrides"], {})
        self.assertEqual(selected["route_changes"], ["planner"])
        self.assertEqual(self.protected_bytes(), before)
        rc, out, err = self.cli("apply", "--proposal-id", selected["proposal_id"], now=NOW + 2)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read_json(self.paths.state)["overrides"], {})
        self.assertFalse(Path(self.paths.instructions).exists())
        self.assertFalse(Path(self.paths.effective).exists())
        rc, live, err = self.cli("status", "--instructions")
        self.assertEqual(rc, 0, live + err)
        self.assertEqual(self.live_routes(live)["planner"], self.defaults["planner"])
        self.assertEqual(self.read_json(self.paths.settings)["subagents"]["agents"]["unrelated-agent"],
                         {"model": "human-choice"})

    def test_same_model_alignment_checks_selected_exact_level_and_keeps_nondefault_model(self):
        old = self.install_managed_drift(model=NEW)
        self.config.pop("available_models")
        self.config["verified_capabilities"]["models"].pop(NEW)
        self.save_config()
        before = self.protected_bytes()
        doc, _ = self.offered()
        selected, out = self.select_option(doc, "planner", model=NEW)
        self.assertEqual(selected["overrides"]["planner"], dict(self.defaults["planner"], model=NEW))
        self.assertEqual(self.machine(out, "CHECK"), [[NEW, "planner"]])
        self.assertEqual(self.machine(out, "LEVEL"), [[NEW, "medium"]])
        self.assertEqual(self.protected_bytes(), before)
        rc, out, err = self.cli("review", "--proposal-id", selected["proposal_id"], "--available-all",
                                "--level", NEW + ":medium=yes", now=NOW + 1)
        self.assertEqual(rc, 0, out + err)
        checked = self.frozen(out)
        self.assertEqual(self.machine(out, "REVIEW"), [["complete"]])
        self.assertEqual(self.protected_bytes(), before)
        rc, out, err = self.cli("apply", "--proposal-id", checked["proposal_id"], now=NOW + 2)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read_json(self.paths.effective)["routes"]["planner"]["values"],
                         dict(self.defaults["planner"], model=NEW))
        self.assertEqual(self.read_json(self.paths.user_config), self.config)
        self.assertNotEqual(old, self.read_json(self.paths.effective)["routes"]["planner"]["values"])

    def test_denied_alignment_never_downgrades_or_selects_a_cheaper_replacement(self):
        for answer in ("availability", "level"):
            with self.subTest(answer=answer):
                old = self.install_managed_drift(model=NEW)
                self.config.pop("available_models", None)
                self.config["verified_capabilities"]["models"].pop(NEW, None)
                self.save_config()
                before = self.protected_bytes()
                doc, _ = self.offered()
                doc, _ = self.select_option(doc, "planner", model=NEW)
                flags = ("--unavailable", NEW) if answer == "availability" else (
                    "--available-all", "--level", NEW + ":medium=no")
                rc, out, err = self.cli("review", "--proposal-id", doc["proposal_id"], *flags, now=NOW + 1)
                self.assertEqual(rc, 3, out + err)
                self.assertIn("Dropped:", out)
                if self.machine(out, "PROPOSAL"):
                    refused = self.frozen(out)
                    self.assertEqual(refused["selected"], [])
                    self.assertEqual(refused["overrides"], {"planner": old})
                    self.assertEqual(refused["route_changes"], [])
                self.assertEqual(self.protected_bytes(), before)

    def test_pinned_drift_is_never_offered_or_changed(self):
        for pin in ("user-config", "native"):
            with self.subTest(pin=pin):
                self.config.pop("pins", None)
                self.save_config()
                self.install_managed_drift()
                if pin == "user-config":
                    self.config["pins"] = {"planner": {"reasoning_effort": "high"}}
                    self.save_config()
                else:
                    settings = self.read_json(self.paths.settings)
                    settings["subagents"]["agents"]["planner"]["effortLevel"] = "high"
                    self.write_json(self.paths.settings, settings)
                ctx = self.mod.build_context(self.paths, NOW)
                row = self.rows(ctx)["planner"]
                self.assertEqual(row["alternatives"], [])
                self.assertIsNone(row["proposed"])
                self.assertIn("pinned by you", row["reason"])
                self.assertEqual(row["current"]["reasoning_effort"], "high")
                before = self.protected_bytes()
                rc, out, err = self.cli("propose", "--review", "--offline")
                self.assertEqual(rc, 0, out + err)
                self.assertIn("generated routing files are out of sync", out)
                doc = self.frozen(out)
                self.assertEqual(doc["policy"], "balanced")
                self.assertEqual(doc["selected"], [])
                self.assertEqual(doc["route_changes"], [])
                self.assertFalse(any(a["route"] == "planner" for a in doc["alternatives"]))
                self.assertEqual(self.protected_bytes(), before)

    def test_blocked_alignment_discloses_drift_and_preserves_current_values(self):
        for gate in ("effort", "context", "price"):
            with self.subTest(gate=gate):
                base = self.defaults["planner"]["model"]
                self.config["verified_capabilities"]["models"][base] = {
                    "reasoning_levels": ["low", "medium", "high", "xhigh"]}
                self.doc_facts[base]["one_million_context"] = "Supported"
                self.markdown = self.live_prices(new=12, cheap=15, new_long=18)
                old = self.install_managed_drift()
                if gate == "effort":
                    self.config["verified_capabilities"]["models"][base]["reasoning_levels"] = ["low"]
                    self.save_config()
                elif gate == "context":
                    self.doc_facts[base]["one_million_context"] = "Not supported"
                    self.seed_docs()
                else:
                    self.markdown = b"\n".join(line for line in self.markdown.splitlines()
                                              if not line.startswith(("| %s |" % base).encode()))
                    self.seed_cache()
                before = self.protected_bytes()
                rc, out, err = self.cli("propose", "--offline")
                self.assertEqual(rc, 3, out + err)
                row = self.rows(self.mod.build_context(self.paths, NOW))["planner"]
                self.assertFalse(any("alignment" in a["bases"] for a in row["alternatives"]))
                self.assertIn("drift:", out)
                self.assertIn("no correction can be offered right now", out)
                self.assertEqual(self.protected_bytes(), before)
                self.assertEqual(self.read_json(self.paths.state)["overrides"]["planner"], old)

    def test_alignment_basis_is_part_of_frozen_evidence(self):
        self.install_managed_drift()
        doc, _ = self.offered()
        doc, _ = self.select_option(doc, "planner", model=self.defaults["planner"]["model"])
        before = self.protected_bytes()
        original = self.mod.evaluate

        def altered_basis(ctx):
            rows = original(ctx)
            for row in rows:
                for alt in row["alternatives"]:
                    if "alignment" in alt["bases"]:
                        alt["bases"] = []
            return rows

        with mock.patch.object(self.mod, "evaluate", side_effect=altered_basis):
            rc, out, err = self.cli("apply", "--proposal-id", doc["proposal_id"], now=NOW + 1)
        self.assertEqual(rc, 4, out + err)
        self.assertIn("evidence", err)
        self.assertEqual(self.protected_bytes(), before)


class TestDraftMapping(LiveRefreshCase):
    """The balanced p transaction, using real parsers and synthetic transport.

    Stable table rows are ROUTES order, not the renumberable option numbers.
    All writes and availability answers stay inside the temporary HOME.
    """

    TEST_POLICY = None
    TEST_DETAILS = False
    machine = TestProposalScopedReview.machine
    frozen = TestProposalScopedReview.frozen
    protected_bytes = TestProposalScopedReview.protected_bytes
    whole_home = TestBalancedBenchmarkRefresh.whole_home

    def setUp(self):
        super().setUp()
        self.config.pop("available_models")
        self.save_config()
        self.markdown = self.live_prices(new=5, cheap=6)
        self.b, error = self.mod.load_benchmark_module()
        self.assertIsNone(error)
        self.benchmark_fetches = []
        self.benchmark_pages = {}
        self.mod.BENCHMARK_FETCHER = self.fetch_benchmarks
        self.before = self.protected_bytes()
        self.clock = NOW

    def fetch_benchmarks(self, url, timeout, max_bytes):
        self.benchmark_fetches.append(url)
        pair = next(p for p in self.b.PAIRS if p["url"] == url)
        # Other/unspecified effort is context only, not evidence of quality.
        return self.benchmark_pages.get(url, comparison_fixture(pair, efforts=("max", "max")))

    def draft(self):
        rc, out, err = self.cli("propose", "--review", "--draft")
        self.assertEqual(rc, 0, out + err)
        doc = self.frozen(out)
        self.assertEqual(doc["format"], 5)
        self.assertEqual(doc["policy"], "balanced")
        self.assertEqual(self.protected_bytes(), self.before)
        return doc, out

    def step(self, doc, command, expected_rc=0):
        self.clock += 1
        rc, out, err = self.cli("draft", "--proposal-id", doc["proposal_id"],
                                "--do", command, now=self.clock)
        self.assertEqual(rc, expected_rc, out + err)
        self.assertEqual(self.protected_bytes(), self.before)
        return (self.frozen(out) if self.machine(out, "PROPOSAL") else None), out

    def answer(self, doc, *args):
        self.clock += 1
        rc, out, err = self.cli("review", "--proposal-id", doc["proposal_id"],
                                *args, now=self.clock)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.protected_bytes(), self.before)
        return self.frozen(out), out

    def row(self, doc, route):
        return next(r for r in doc["draft"]["rows"] if r["route"] == route)

    def index(self, route):
        return next(i for i, r in enumerate(self.mod.ROUTES, 1) if r["key"] == route)

    def calls(self):
        return (list(self.fetches), list(self.docs_fetches), list(self.benchmark_fetches))

    def assert_original(self, first, doc, evidence=True):
        for field in ("created_epoch", "expires_epoch", "digests", "snapshot_id"):
            self.assertEqual(first[field], doc[field], field)
        if evidence:
            self.assertEqual(first["evidence_digest"], doc["evidence_digest"])

    def test_first_draft_is_unselected_full_numbered_mapping_before_any_check(self):
        doc, out = self.draft()
        self.assertEqual(doc["selected"], [])
        self.assertEqual(doc["overrides"], {})
        self.assertEqual(doc["route_changes"], [])
        self.assertEqual(self.machine(out, "DRAFT"), [["choose", "10", "0"]])
        for marker in ("CHECK", "LEVEL", "REVIEW", "SELECTED", "ALTERNATIVE", "VERIFY"):
            self.assertEqual(self.machine(out, marker), [], marker)
        section = out.split("Draft mapping — every route", 1)[1].split("Why (short", 1)[0]
        for i, route in enumerate(self.mod.ROUTES, 1):
            self.assertRegex(section, r"(?m)^\s+%d\s+%s\b" % (i, route["key"].replace(".", r"\.")))
        # Unspecified width may use a wide native table; narrower terminals
        # currently use blocks. Validate the actual model/price cells here,
        # leaving the explicit user table requirement to the next test.
        self.assertIn("gpt-6-luna", section)
        self.assertIn(NEW, section)
        self.assertIn("10.00", section)
        self.assertIn("5.00", section)
        self.assertLess(out.index("Draft mapping — every route"), out.index("a = accept all"))
        self.assertIn("Savings (price only)", out)
        self.assertIn("NOT a quality claim", out)
        self.assertEqual(tuple(map(len, self.calls())), (1, 2, 4))

    def test_requested_mapping_is_rendered_as_a_table_not_only_route_blocks(self):
        # User requested a mapping table BEFORE choice, not just a heading
        # that calls individually wrapped blocks a table.
        with mock.patch.dict(os.environ, {"COLUMNS": "140"}):
            _, out = self.draft()
        section = out.split("Draft mapping — every route", 1)[1].split("Why (short", 1)[0]
        self.assertRegex(section, r"(?m)^\s*#\s+Route\s+Current\s+Draft\s+")

    def test_wrapped_table_preserves_cells_width_and_alignment_with_color(self):
        tables = []
        for color in ("never", "always"):
            with self.subTest(color=color), mock.patch.dict(os.environ, {
                "COLUMNS": "140", "TERM": "xterm", "NO_COLOR": "",
                self.mod.COLOR_ENV: color,
            }):
                _, out = self.draft()
                if color == "always":
                    self.assertIn("\x1b[", out)
                else:
                    self.assertNotIn("\x1b", out)
                plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
                section = plain.split("Draft mapping — every route", 1)[1].split("Why (short", 1)[0]
                lines = section.splitlines()
                header_index = next(i for i, line in enumerate(lines) if re.match(r"^\s*#\s+Route\s+", line))
                header = lines[header_index]
                headers = ("#", "Route", "Current", "Draft", "$/1M", "Basis", "Opt", "Status")
                starts = [header.index(h) for h in headers]
                groups = []
                for line in lines[header_index + 2:]:
                    if not line.strip():
                        continue
                    self.assertLessEqual(len(line), 140, line)
                    if line[starts[0]:starts[1]].strip():
                        groups.append([])
                    self.assertTrue(groups, line)
                    groups[-1].append([
                        line[start:starts[c + 1] if c + 1 < len(starts) else None].strip()
                        for c, start in enumerate(starts)
                    ])
                self.assertEqual(len(groups), 10)
                self.assertTrue(any(len(group) > 1 for group in groups), "fixture must exercise wrapping")
                for i, (route, group) in enumerate(zip(self.mod.ROUTES, groups), 1):
                    cells = [" ".join(line[c] for line in group if line[c]) for c in range(len(headers))]
                    values = self.defaults[route["key"]]
                    suffix = " long" if values["context_tier"] == "long_context" else ""
                    self.assertEqual(cells, [
                        str(i), route["key"], "%s %s%s" % (values["model"], values["reasoning_effort"], suffix),
                        "%s %s%s" % (NEW, values["reasoning_effort"], suffix),
                        "10.00 -> 5.00 (-50%)", "Savings (price only)", "2", "draft · check /model",
                    ])
                    for c in (0, 1, 2, 3, 6):
                        self.assertTrue(all(not line[c] for line in group[1:]),
                                        "identity/option columns must not wrap")
                tables.append(lines[header_index:])
                self.assertLess(plain.index("Draft mapping — every route"), plain.index("a = accept all"))
                self.assertIn("blended assumption", plain)
                for line in out.splitlines():
                    if line.startswith(("DRAFT\t", "PROPOSAL\t")):
                        self.assertNotIn("\x1b", line)
        self.assertEqual(tables[0], tables[1], "ANSI styling must not alter visible column padding")

    def test_narrow_fallback_keeps_every_route_and_untruncated_model_identity(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "80", self.mod.COLOR_ENV: "never"}):
            _, out = self.draft()
        section = out.split("Draft mapping — every route", 1)[1].split("Why (short", 1)[0]
        self.assertNotRegex(section, r"(?m)^\s*#\s+Route\s+Current\s+Draft\s+")
        self.assertIn("now:", section)
        self.assertIn("draft:", section)
        for i, route in enumerate(self.mod.ROUTES, 1):
            self.assertRegex(section, r"(?m)^\s+%d\s+%s\b" % (i, re.escape(route["key"])))
            self.assertIn(self.defaults[route["key"]]["model"], section)
        self.assertIn(NEW, section)
        self.assertLess(out.index("Draft mapping — every route"), out.index("a = accept all"))

    def test_zero_accepted_final_is_real_generated_routing_reconciliation_not_verification(self):
        # Start from the existing test-owned managed-state fixture, then
        # make a native effort pin that leaves the generated files stale.
        TestBalancedBenchmarkRefresh.install_managed_drift(self)
        settings = self.read_json(self.paths.settings)
        settings["subagents"]["agents"]["planner"]["effortLevel"] = "high"
        self.write_json(self.paths.settings, settings)
        self.before = self.protected_bytes()
        doc, _ = self.draft()
        doc, _ = self.step(doc, "k 1,2,3,4,5,6,7,8,9,10")
        final, out = self.step(doc, "a")
        self.assertEqual(self.machine(out, "DRAFT"), [["final", "0", "0"]])
        self.assertEqual(self.machine(out, "REVIEW"), [["complete"]])
        self.assertEqual(self.machine(out, "SELECTED"), [["0"]])
        self.assertEqual(self.machine(out, "CHECK"), [])
        self.assertEqual(final["route_changes"], [])
        self.assertIn("yes re-syncs them", out)
        rc, out, err = self.cli("apply", "--proposal-id", final["proposal_id"], now=self.clock + 1)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read_json(self.paths.settings)["subagents"]["agents"]["planner"]["effortLevel"],
                         "high")
        # Generated routing intentionally does not freeze native pin values:
        # null/OMIT delegates the field to the live setting asserted above.
        self.assertIsNone(self.read_json(self.paths.effective)["routes"]["planner"]["values"]["reasoning_effort"])
        rc, live, err = self.cli("status", "--instructions", now=self.clock + 1)
        self.assertEqual(rc, 0, live + err)
        self.assertEqual(self.live_routes(live)["planner"]["reasoning_effort"], "OMIT")

    def test_accept_all_then_verify_and_apply_only_after_separate_confirmation(self):
        first, _ = self.draft()
        accepted, out = self.step(first, "a")
        self.assertEqual(self.machine(out, "DRAFT"), [["verify", "0", "10"]])
        self.assertEqual(set(accepted["route_changes"]), set(self.defaults))
        self.assertEqual(self.machine(out, "CHECK"), [[NEW, ",".join(self.defaults)]])
        self.assertEqual(self.machine(out, "LEVEL"), [])
        calls = self.calls()
        checked, out = self.answer(accepted, "--available-all")
        self.assertEqual(self.machine(out, "DRAFT"), [["final", "0", "10"]])
        self.assertEqual(self.machine(out, "CHECK"), [])
        self.assert_original(first, checked, evidence=False)
        rc, out, err = self.cli("apply", "--proposal-id", checked["proposal_id"], now=self.clock + 1)
        self.assertEqual(rc, 0, out + err)
        live = self.read_json(self.paths.effective)["routes"]
        for key in self.defaults:
            self.assertEqual(live[key]["values"], dict(self.defaults[key], model=NEW))
        self.assertEqual(self.calls(), calls)
        self.assertFalse(Path(self.paths.verified).exists())
        self.assertEqual(self.read_json(self.paths.user_config), self.config)
        self.assertEqual(self.read_json(self.paths.settings)["subagents"]["agents"]["unrelated-agent"],
                         {"model": "human-choice"})

    def test_partial_accept_edit_withdraw_and_apply_preserves_other_accepted_row(self):
        first, _ = self.draft()
        a, b = self.index("planner"), self.index("tester")
        selected, out = self.step(first, "a %d,%d" % (a, b))
        self.assertEqual(self.machine(out, "DRAFT"), [["verify", "0", "2"]])
        self.assertEqual(self.row(selected, "coder")["decision"], "kept")
        checked, _ = self.answer(selected, "--available-all")
        edit, _ = self.step(checked, "e")
        self.assertEqual(self.row(edit, "tester"), self.row(checked, "tester"))
        withdrawn, _ = self.step(edit, "k %d" % a)
        self.assertEqual(self.row(withdrawn, "planner")["decision"], "kept")
        final, out = self.step(withdrawn, "a")
        self.assertEqual(self.machine(out, "DRAFT"), [["final", "0", "1"]])
        self.assertEqual(final["route_changes"], ["tester"])
        self.assertEqual(final["review"], checked["review"])
        self.assert_original(first, final, evidence=False)
        rc, out, err = self.cli("apply", "--proposal-id", final["proposal_id"], now=self.clock + 1)
        self.assertEqual(rc, 0, out + err)
        routes = self.read_json(self.paths.effective)["routes"]
        self.assertEqual(routes["tester"]["values"], dict(self.defaults["tester"], model=NEW))
        self.assertEqual(routes["planner"]["values"], self.defaults["planner"])
        self.assertEqual(routes["coder"]["values"], self.defaults["coder"])

    def test_reproposal_remembers_exact_subset_preserves_approval_and_exhausts(self):
        first, _ = self.draft()
        checked, _ = self.step(first, "a")
        checked, _ = self.answer(checked, "--available-all")
        edit, _ = self.step(checked, "e")
        keep = self.row(edit, "tester")
        calls = self.calls()
        second, _ = self.step(edit, "r 1,3")
        self.assertEqual(second["draft"]["subset"], [1, 3])
        self.assertEqual(self.row(second, "tester"), keep)
        for i in (1, 3):
            self.assertEqual(second["draft"]["rows"][i - 1]["values"]["model"], CHEAP)
            self.assertEqual(second["draft"]["rows"][i - 1]["decision"], "open")
        exhausted, out = self.step(second, "r")
        for i in (1, 3):
            self.assertIsNone(exhausted["draft"]["rows"][i - 1]["values"])
            self.assertEqual(exhausted["draft"]["rows"][i - 1]["flag"], "exhausted")
        self.assertIn("no further recommendable candidate", out)
        self.assertEqual(self.row(exhausted, "tester"), keep)
        self.assertEqual(exhausted["review"], checked["review"])
        third, _ = self.step(exhausted, "r 2")
        self.assertEqual(third["draft"]["subset"], [2])
        fourth, _ = self.step(third, "r")
        self.assertEqual(fourth["draft"]["rows"][1]["flag"], "exhausted")
        self.assert_original(first, fourth, evidence=False)
        self.assertEqual(self.calls(), calls)

    def test_invalid_commands_are_effect_free_and_inspection_is_read_only(self):
        doc, _ = self.draft()
        for command in ("r", "k", "a 0", "a 11", "a 1,1", "o 1.99", "e", "yes",
                        "r 01", "o 1,2", "a; echo unsafe", "r " + "1" * 65):
            with self.subTest(command=command):
                before = self.whole_home()
                same, out = self.step(doc, command, expected_rc=5)
                self.assertEqual(same, doc)
                self.assertEqual(self.whole_home(), before)
                self.assertIn("Not accepted:", out)
        for command in ("o", "o 3"):
            with self.subTest(command=command):
                before = self.whole_home()
                same, out = self.step(doc, command)
                self.assertEqual(same, doc)
                self.assertEqual(self.whole_home(), before)
                self.assertIn("o ROW.K", out)
                self.assertEqual(self.machine(out, "ALTERNATIVE"), [])
                if command == "o 3":
                    self.assertIn("https://www.benchleader.com/compare/gpt-6-1-sol-vs-gpt-6-2-sol", out)
        picked, _ = self.step(doc, "o 3.2")
        self.assertEqual(picked["draft"]["rows"][2]["values"]["model"], CHEAP)
        self.assertEqual(picked["draft"]["rows"][2]["decision"], "open")

    def test_keep_everything_then_accept_is_noop_and_removes_proposal(self):
        doc, _ = self.draft()
        doc, _ = self.step(doc, "k 1,2,3,4,5,6,7,8,9,10")
        final, out = self.step(doc, "a", expected_rc=3)
        self.assertIsNone(final)
        self.assertEqual(self.machine(out, "DRAFT"), [["none", "0", "0"]])
        self.assertIn("Final effective routing", out)
        self.assertFalse(Path(self.paths.proposal).exists())

    def test_direct_apply_and_review_and_legacy_select_cannot_bypass_draft_acceptance(self):
        for phase in ("choose", "verify"):
            with self.subTest(phase=phase):
                doc, _ = self.draft()
                if phase == "verify":
                    doc, _ = self.step(doc, "a")
                rc, out, err = self.cli("apply", "--proposal-id", doc["proposal_id"])
                self.assertEqual(rc, 4, out + err)
                self.assertIn("not been accepted" if phase == "choose" else "not confirmed", err)
                self.assertEqual(self.protected_bytes(), self.before)
        doc, _ = self.draft()
        for command, args in (("review", ("--available-all",)), ("select", ("--choose", "1"))):
            with self.subTest(command=command):
                rc, out, err = self.cli(command, "--proposal-id", doc["proposal_id"], *args)
                self.assertEqual(rc, 4, out + err)
                self.assertEqual(self.protected_bytes(), self.before)

    def test_shared_model_no_replaces_all_using_rows_only_and_reaccepts_replacements(self):
        doc, _ = self.draft()
        # Row 1 explicitly uses the second candidate; all other rows use NEW.
        doc, _ = self.step(doc, "o 1.2")
        doc, _ = self.step(doc, "a")
        survivor = doc["draft"]["rows"][0]
        calls = self.calls()
        revised, out = self.answer(doc, "--unavailable", NEW)
        self.assertEqual(revised["draft"]["rows"][0], survivor)
        self.assertEqual(revised["review"]["available"], [CHEAP])
        self.assertEqual(revised["review"]["rejected"], [NEW])
        self.assertEqual(self.machine(out, "DRAFT"), [["choose", "9", "1"]])
        for row in revised["draft"]["rows"][1:]:
            self.assertEqual(row["values"]["model"], CHEAP)
            self.assertEqual(row["decision"], "open")
        self.assertTrue(all(a["values"]["model"] != NEW for a in revised["alternatives"]))
        accepted, out = self.step(revised, "a")
        self.assertEqual(self.machine(out, "DRAFT"), [["final", "0", "10"]])
        self.assertEqual(self.machine(out, "CHECK"), [], "CHEAP was already checked")
        self.assertEqual(self.calls(), calls)
        self.assert_original(doc, accepted, evidence=False)

    def test_effort_no_only_redrafts_matching_pair_and_preserves_other_yes_answers(self):
        self.config["verified_capabilities"]["models"].pop(NEW)
        self.save_config()
        self.before = self.protected_bytes()
        doc, _ = self.draft()
        doc, out = self.step(doc, "a")
        levels = self.machine(out, "LEVEL")
        self.assertEqual({p[1] for p in levels}, {"low", "medium", "high", "xhigh"})
        flags = ["--available-all"]
        for mid, effort in levels:
            flags += ["--level", "%s:%s=%s" % (mid, effort, "no" if effort == "xhigh" else "yes")]
        revised, out = self.answer(doc, *flags)
        tester = self.row(revised, "tester")
        self.assertEqual(tester["values"], dict(self.defaults["tester"], model=CHEAP))
        self.assertEqual(tester["decision"], "open")
        for row in revised["draft"]["rows"]:
            if row["route"] != "tester":
                self.assertEqual(row, self.row(doc, row["route"]))
        self.assertEqual(revised["review"]["levels"][NEW]["medium"], True)
        self.assertEqual(revised["review"]["levels"][NEW]["xhigh"], False)
        doc, out = self.step(revised, "a")
        self.assertEqual(self.machine(out, "CHECK"), [[CHEAP, "tester"]])
        final, out = self.answer(doc, "--available-all")
        self.assertEqual(self.machine(out, "DRAFT"), [["final", "0", "10"]])

    def test_details_uses_cache_and_is_read_only_even_with_tampered_or_expired_draft(self):
        doc, _ = self.draft()
        calls = self.calls()
        for kind in ("valid", "tamper", "expired"):
            with self.subTest(kind=kind):
                saved = copy.deepcopy(doc)
                if kind == "tamper":
                    saved["draft"]["rows"][0]["decision"] = "accepted"
                self.write_json(self.paths.proposal, saved)
                before = self.whole_home()
                rc, out, err = self.cli("details", "--proposal-id", doc["proposal_id"],
                                        now=NOW + 901 if kind == "expired" else NOW)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("===== Details (full report) =====", out)
                self.assertEqual(self.whole_home(), before)
                self.assertEqual(self.calls(), calls)
                self.assertEqual(self.machine(out, "PROPOSAL"), [])

    def test_superseded_stale_expired_and_tampered_drafts_refuse_without_fetch_or_routing_write(self):
        for kind in ("superseded", "settings", "pricing", "benchmarks", "tamper", "expiry"):
            with self.subTest(kind=kind):
                doc, _ = self.draft()
                calls = self.calls()
                if kind == "superseded":
                    self.step(doc, "r 1")
                elif kind == "settings":
                    self.write_json(self.paths.settings, dict(self.initial_settings, theme="changed"))
                elif kind in ("pricing", "benchmarks"):
                    path = self.pricing.official_pricing_cache_path() if kind == "pricing" else self.paths.benchmarks
                    self.write_json(path, {})
                elif kind == "tamper":
                    doc["draft"]["subset"] = [1]
                    self.write_json(self.paths.proposal, doc)
                before = self.protected_bytes()
                rc, out, err = self.cli("draft", "--proposal-id", doc["proposal_id"], "--do", "a",
                                        now=NOW + 901 if kind == "expiry" else self.clock)
                self.assertEqual(rc, 4, out + err)
                self.assertEqual(self.protected_bytes(), before)
                self.assertEqual(self.calls(), calls)
                # Restore test-owned inputs for the next independent case.
                self.write_json(self.paths.settings, self.initial_settings)
                self.before = self.protected_bytes()

    def test_signed_inconsistent_draft_or_selection_fails_closed(self):
        mutations = (
            ("accepted-no-selection", lambda d: d["draft"]["rows"][0].update(decision="accepted")),
            ("open-confirm", lambda d: d["draft"].update(phase="confirm")),
            ("unknown-phase", lambda d: d["draft"].update(phase="final")),
            ("duplicate-subset", lambda d: d["draft"].update(subset=[1, 1])),
            ("boolean-step", lambda d: d["draft"].update(step=True)),
            ("wrong-basis", lambda d: d["draft"]["rows"][0].update(basis="benchmark")),
            ("unknown-values", lambda d: d["draft"]["rows"][0]["values"].update(model="not-offered")),
            ("extended-expiry", lambda d: d.update(expires_epoch=d["expires_epoch"] + 60)),
        )
        for kind, mutate in mutations:
            with self.subTest(kind=kind):
                doc, _ = self.draft()
                mutate(doc)
                key = Path(self.paths.proposal_key).read_bytes().strip()
                body = {k: v for k, v in doc.items() if k != "seal"}
                doc["seal"] = hmac.new(key, json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                     ensure_ascii=True).encode("ascii"), hashlib.sha256).hexdigest()
                self.write_json(self.paths.proposal, doc)
                rc, out, err = self.cli("draft", "--proposal-id", doc["proposal_id"], "--do", "a")
                self.assertEqual(rc, 4, out + err)
                self.assertEqual(self.protected_bytes(), self.before)

    def test_pins_are_kept_and_not_selectable_or_reproposable(self):
        settings = copy.deepcopy(self.initial_settings)
        settings["subagents"]["agents"]["coder"] = {"model": "native-fixture-pin"}
        self.write_json(self.paths.settings, settings)
        self.config["pins"] = {"tester": {"model": CHEAP}}
        self.save_config()
        self.before = self.protected_bytes()
        doc, _ = self.draft()
        for route in ("coder", "tester"):
            self.assertEqual(self.row(doc, route)["decision"], "kept")
            self.assertIsNone(self.row(doc, route)["values"])
            for verb in ("r", "o"):
                _, out = self.step(doc, "%s %s" % (verb, str(self.index(route)) + (".1" if verb == "o" else "")),
                                   expected_rc=5)
                self.assertIn("pinned", out)


class TestDraftRecommendationPolicy(LiveRefreshCase):
    """Policy tests on evaluated eligible options, not external model claims."""

    TEST_POLICY = None
    TEST_DETAILS = False

    def setUp(self):
        super().setUp()
        self.b, error = self.mod.load_benchmark_module()
        self.assertIsNone(error)
        self.benchmark_fetches = []
        self.benchmark_efforts = {"gpt-6.1-sol": "xhigh", NEW: "xhigh",
                                  "claude-sonnet-5.5": "high", "gpt-6.1-terra": "high"}
        self.benchmark_pages = {}
        self.mod.BENCHMARK_FETCHER = self.fetch_benchmarks
        self.markdown = self.live_prices(new=12, cheap=15, new_long=18)

    def source_metrics(self, current="70%", candidate="80%", effort="xhigh", extra=()):
        # Each subcase changes the fixture page. Remove the preceding case's
        # cache so the real five-minute reuse policy cannot hide that change.
        Path(self.paths.benchmarks).unlink(missing_ok=True)
        for pair in self.b.PAIRS:
            if pair["provider"] == "benchleader":
                self.benchmark_pages[pair["url"]] = comparison_fixture(
                    pair, efforts=(effort, effort),
                    values=(("Terminal-Bench", "Terminal-Bench", current, candidate), *extra))

    def ranking(self, key="tester", ages=None, benchmark_age=0):
        ctx = self.context(ages, benchmark_age)
        row = self.rows(ctx)[key]
        return ctx, row, self.mod.draft_ranking(ctx, row)

    def test_unique_fresh_exact_effort_nonregressive_benchmark_precedes_savings(self):
        self.markdown = self.live_prices(new=12, cheap=5)
        self.source_metrics()
        ctx, row, ranked = self.ranking()
        self.assertEqual([(a["model"], basis) for a, basis in ranked],
                         [(NEW, "benchmark"), (CHEAP, "savings")])
        alt = ranked[0][0]
        self.assertEqual(self.mod.draft_evidence(alt), ("favours", 1, 0))
        rows = self.mod.evaluate(ctx)
        self.mod.number_alternatives(rows)
        text = "\n".join(self.mod.render_draft_options(
            ctx, rows, self.mod.initial_draft(ctx, rows),
            self.mod.ROUTES.index(self.mod.ROUTE_BY_KEY["tester"]) + 1))
        for token in ("70%", "80%", "page updated", "same levels", "reference"):
            self.assertIn(token, text)
        cache = self.read_json(self.paths.benchmarks)
        for pair in self.b.PAIRS:
            if "gpt-6.1-sol" in {mid for mid, _ in pair["models"]}:
                self.assertIn(pair["page"], text)
            record = cache["records"][pair["id"]]
            self.assertEqual(record["url"], pair["url"])
            self.assertEqual(record["content_sha256"],
                             hashlib.sha256(self.benchmark_pages.get(pair["url"]) or comparison_fixture(
                                 pair, efforts=tuple(self.benchmark_efforts[mid] for mid, _ in pair["models"]))
                             ).hexdigest())
        self.assertTrue(row["alternatives"])

    def test_equal_comparable_results_still_recommend_price_only_savings(self):
        self.markdown = self.live_prices(new=5, cheap=6)
        self.source_metrics(current="80%", candidate="80%")
        _, _, ranked = self.ranking()
        alt, basis = ranked[0]
        self.assertEqual((alt["model"], basis), (NEW, "savings"))
        self.assertEqual(self.mod.draft_evidence(alt), ("none", 0, 0))
        self.assertIn("NOT", self.mod.draft_basis_text(alt, basis))

    def test_adverse_and_mixed_comparable_metrics_are_never_automatic_savings(self):
        self.markdown = self.live_prices(new=5, cheap=6)
        for kind, a, b, extra in (
            ("adverse", "80%", "70%", ()),
            ("mixed", "70%", "80%", (("SWE Atlas v1.1", "Scale AI SEAL", "90%", "60%"),)),
        ):
            with self.subTest(kind=kind):
                self.source_metrics(a, b, extra=extra)
                _, row, ranked = self.ranking()
                alt = next(x for x in row["alternatives"] if x["model"] == NEW)
                self.assertIn("saving", alt["bases"])
                self.assertEqual(self.mod.draft_evidence(alt)[0], kind)
                self.assertNotIn(NEW, [x["model"] for x, _ in ranked])
                self.assertIn("tradeoff", self.mod._not_drafted_text(alt))

    def test_wrong_effort_or_stale_results_do_not_sell_quality_or_block_independent_savings(self):
        self.markdown = self.live_prices(new=5, cheap=6)
        for effort, age in (("max", 0), ("xhigh", 8 * DAY)):
            with self.subTest(effort=effort, age=age):
                self.source_metrics("80%", "70%", effort)
                _, _, ranked = self.ranking(benchmark_age=age)
                self.assertEqual((ranked[0][0]["model"], ranked[0][1]), (NEW, "savings"))

    def test_equal_official_rates_and_documented_advantage_precede_cheaper_neutral_option(self):
        self.markdown = self.live_prices(new=10, cheap=5)
        self.doc_facts["gpt-6.1-sol"]["copilot_cli_client"] = "Not included"
        _, _, ranked = self.ranking("planner")
        # Several synthetic included-client candidates share the same
        # advantage and price; deterministic ID order resolves their tie.
        self.assertEqual(ranked[0][1], "official")
        self.assertIn((NEW, "official"), [(a["model"], b) for a, b in ranked])
        official = [a["model"] for a, b in ranked if b == "official"]
        self.assertEqual(official, sorted(official))
        self.assertTrue(ranked[0][0]["differentiators"])
        self.assertIn((CHEAP, "savings"), [(a["model"], b) for a, b in ranked])

    def test_official_advantage_at_different_price_is_explicit_only_and_manual_pickable(self):
        self.markdown = self.live_prices(new=12, cheap=15, new_long=18)
        self.doc_facts["gpt-6.1-sol"]["copilot_cli_client"] = "Not included"
        ctx, row, ranked = self.ranking("planner")
        alt = next(a for a in row["alternatives"] if a["model"] == NEW)
        self.assertIn("official", alt["bases"])
        self.assertNotIn(NEW, [a["model"] for a, _ in ranked])
        rows = self.mod.evaluate(ctx)
        draft = self.mod.initial_draft(ctx, rows)
        i = next(i for i, r in enumerate(rows, 1) if r["key"] == "planner")
        k = next(k for k, a in enumerate(rows[i - 1]["alternatives"], 1) if a["model"] == NEW)
        manual, _ = self.mod.apply_draft_command(ctx, rows, draft, "o", None, (i, k))
        self.assertEqual(manual["rows"][i - 1]["basis"], "manual")
        self.assertEqual(manual["rows"][i - 1]["decision"], "open")
        self.assertEqual(manual["rows"][i - 1]["values"], dict(self.defaults["planner"], model=NEW))

    def test_long_context_same_price_and_savings_require_both_real_tiers(self):
        self.doc_facts["gpt-6.1-sol"]["copilot_cli_client"] = "Not included"
        for default, long, expected in ((10, None, True), (10, 18, False), (5, 18, False), (5, 5, True)):
            with self.subTest(default=default, long=long):
                self.markdown = self.live_prices(new=default, cheap=15, new_long=long)
                _, _, ranked = self.ranking("planner")
                self.assertEqual(NEW in [a["model"] for a, _ in ranked], expected)
        # A weighted tie is not equality of the official rates themselves.
        ctx, _, _ = self.ranking("tester")
        snap = copy.deepcopy(ctx["snapshot"])
        rates = snap["models"]["gpt-6.1-sol"]["tiers"]["default"]
        snap["models"][NEW]["tiers"]["default"] = dict(rates, input=rates["input"] + 1)
        self.assertFalse(self.mod._same_official_price(snap, "gpt-6.1-sol", NEW, False))

    def test_multiple_distinct_favourable_candidates_have_no_automatic_benchmark_winner(self):
        self.markdown = self.live_prices(new=5, cheap=6)
        self.source_metrics()
        ctx, row, _ = self.ranking()
        # Unit fixture: two separately eligible identities with equally
        # favourable measured evidence; the ranking must not break quality
        # ties with price. Real provider parsing/provenance is covered above.
        row = copy.deepcopy(row)
        new = next(a for a in row["alternatives"] if a["model"] == NEW)
        cheap = next(a for a in row["alternatives"] if a["model"] == CHEAP)
        cheap["benchmark"] = copy.deepcopy(new["benchmark"])
        ranked = self.mod.draft_ranking(ctx, row)
        self.assertEqual(ranked, [])
        cheap["benchmark"]["providers_disagree"] = True
        ranked = self.mod.draft_ranking(ctx, row)
        self.assertEqual([(a["model"], b) for a, b in ranked], [(NEW, "benchmark")])

    def test_deterministic_price_ties_use_model_id_and_alignment_has_priority(self):
        self.markdown = self.live_prices(new=5, cheap=5)
        _, _, ranked = self.ranking("planner")
        self.assertEqual([a["model"] for a, _ in ranked], sorted([CHEAP, NEW]))
        TestBalancedBenchmarkRefresh.install_managed_drift(self)
        ctx = self.mod.build_context(self.paths, NOW)
        row = self.rows(ctx)["planner"]
        ranked = self.mod.draft_ranking(ctx, row)
        self.assertEqual((ranked[0][0]["model"], ranked[0][1]), ("gpt-6.1-sol", "alignment"))


# Share the established benchmark fixtures without inheriting their tests.
for _name in ("fetch_benchmarks", "context", "whole_home"):
    setattr(TestDraftRecommendationPolicy, _name, getattr(TestBalancedBenchmarkRefresh, _name))


if __name__ == "__main__":
    unittest.main()
