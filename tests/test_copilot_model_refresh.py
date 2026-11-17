#!/usr/bin/env python3
"""Offline behavioral contracts for the local-only model refresh transaction.

All facts/prices in this file are SYNTHETIC fixtures, not model-quality or
availability claims. Every writable path is under a fresh temporary HOME.
The real shared pricing parser runs, but its transport is always injected.
"""

import contextlib
import copy
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
            "settings", "state", "user_config", "routing", "catalog", "instructions", "effective"})
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

    def test_apply_uses_frozen_proposal_not_network_or_reselection(self):
        proposal_id = self.proposal()
        self.mod.PRICING_FETCHER = mock.Mock(side_effect=AssertionError("apply fetched"))
        with mock.patch.object(self.mod, "evaluate", side_effect=AssertionError("reselected")):
            rc, out, err = self.cli("apply", "--proposal-id", proposal_id)
        self.assertEqual(rc, 0, out + err)


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
            "original=m.atomic_write_bytes\n"
            "def crash(path,data,mode=0o600):\n"
            " original(path,data,mode)\n"
            " if path==m.Paths().instructions: os._exit(77)\n"
            "m.atomic_write_bytes=crash\n"
            "sys.exit(m.main(['apply','--proposal-id',sys.argv[2]],now=float(sys.argv[3])))\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", child, str(HELPER), proposal_id, str(NOW)],
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


if __name__ == "__main__":
    unittest.main()
