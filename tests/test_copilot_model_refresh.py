#!/usr/bin/env python3
"""Offline behavioral contracts for the local-only model refresh transaction.

All facts/prices in this file are SYNTHETIC fixtures, not model-quality or
availability claims. Every writable path is under a fresh temporary HOME.
The real shared pricing parser runs, but its transport is always injected.
"""

import contextlib
import copy
import hashlib
import hmac
import importlib.util
import io
import json
import os
from pathlib import Path
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
            "verified", "capabilities", "pricing"})
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
        self.assertIn("documented differentiator", rendered)
        self.assertIn("official per-client table", rendered)
        self.assertIn("+$2.00/1M, +20%", rendered)
        self.assertIn("+$8.00/1M, +80%", rendered)
        self.assertIn("vendor's description, not a measurement", rendered)
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
        self.assertIn("no documented differentiator", repr(rows["tester"]["considered"]))

    def test_optional_only_noop_proposal_decline_never_changes_baseline(self):
        self.premium(cheap=11)
        before = self.routing_bytes()
        rc, out, err = self.cli("propose", "--offline")
        self.assertEqual(rc, 3, out + err)
        doc = self.read_json(self.paths.proposal)
        self.assertTrue(doc["alternatives"])
        self.assertEqual(doc["overrides"], {})
        self.assertEqual(doc["route_changes"], [])
        self.assertIn("nothing changes unless you select one", out)
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
        self.assertIn("dropped", out)
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


if __name__ == "__main__":
    unittest.main()
