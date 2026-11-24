"""Tests for incident-lab."""

import io
import json
import os
import re
import statistics
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace

from incident_lab.bundle import (
    create_bundle,
    default_bundle_path,
    validate_bundle,
)
from incident_lab.cli import main, parse_jitter_range, parse_seed_spec
from incident_lab.compare import compare_results, compare_shifts
from incident_lab.debrief import (
    generate_debrief, generate_shift_debrief, grade,
)
from incident_lab.difficulty import (
    EASY, HARD, NORMAL, apply_difficulty, list_difficulties,
    parse_difficulty,
)
from incident_lab.drill import run_drill
from incident_lab.export_html import (
    render_postmortem_html,
    render_shift_postmortem_html,
)
from incident_lab.export_pdf import (
    render_postmortem_pdf,
    render_shift_postmortem_pdf,
)
from incident_lab.history import (
    entry_from_result, load_history, record_run, render_all_operators,
    summarize_all, summarize_operator,
)
from incident_lab.policy import (
    DEFAULT_POLICY, EscalationPolicy, Level, Responder, apply_rota,
    current_rota_week, get_policy, list_policies, load_policy_file,
    load_rota_file, parse_ack_timeout_overrides, policy_template,
    policy_to_dict,
)
from incident_lab.replay import (
    load_result, load_saved, load_shift, load_shift_or_result,
)
from incident_lab.scenarios import (
    Scenario, get, list_scenarios, load_scenario_file, scenario_to_dict,
)
from incident_lab.shift import simulate_shift
from incident_lab.simulator import (
    error_budget_burn_pct, error_budget_minutes, simulate, simulate_batch,
)
from incident_lab.stats import aggregate_stats
from incident_lab.timeline import Timeline
from incident_lab.whatif import WhatIfResult, what_if


class TestAckAndEscalation(unittest.TestCase):
    def test_ack_within_timeout_does_not_escalate(self):
        result = simulate(get("cert-expiry"), DEFAULT_POLICY)
        self.assertEqual(result.score.escalations, 0)
        self.assertNotIn("escalated", result.timeline.kinds())
        self.assertIn("acked", result.timeline.kinds())
        self.assertTrue(result.score.resolved)
        self.assertEqual(result.score.time_to_ack_min, 2)

    def test_missed_ack_escalates_to_secondary(self):
        result = simulate(get("latency-spike"), DEFAULT_POLICY)
        self.assertEqual(result.score.escalations, 1)
        self.assertEqual(result.timeline.kinds().count("escalated"), 1)
        # Primary times out at 5m, secondary acks 4m later.
        self.assertEqual(result.score.time_to_ack_min, 9)

    def test_missed_acks_escalate_to_secondary_then_manager(self):
        result = simulate(get("deploy-regression"), DEFAULT_POLICY)
        self.assertEqual(result.score.escalations, 2)
        self.assertEqual(result.timeline.kinds().count("escalated"), 2)
        # 5m primary timeout + 5m secondary timeout + 6m manager ack.
        self.assertEqual(result.score.time_to_ack_min, 16)
        self.assertTrue(result.score.resolved)

    def test_ack_exactly_at_timeout_counts_as_ack(self):
        scenario = Scenario(
            name="edge", service="svc", title="edge", severity="low",
            description="", ack_delay_min=(5, None, None),
            mitigate_delay_min=1, resolve_delay_min=1,
        )
        result = simulate(scenario, DEFAULT_POLICY)
        self.assertEqual(result.score.escalations, 0)
        self.assertEqual(result.score.time_to_ack_min, 5)

    def test_nobody_acks_is_unresolved_and_scores_zero(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        result = simulate(scenario, DEFAULT_POLICY)
        self.assertFalse(result.score.resolved)
        self.assertEqual(result.score.points, 0)
        self.assertIsNone(result.score.time_to_ack_min)
        self.assertIn("unacked", result.timeline.kinds())
        self.assertEqual(result.score.escalations, 2)


class TestPolicies(unittest.TestCase):
    def test_registry_lists_all_policies(self):
        self.assertEqual(list_policies(), ["standard", "fast", "sev1"])
        self.assertIs(get_policy("standard"), DEFAULT_POLICY)

    def test_unknown_policy_raises_key_error(self):
        with self.assertRaises(KeyError):
            get_policy("no-such-policy")

    def test_fast_policy_has_tighter_timeouts(self):
        fast = get_policy("fast")
        self.assertEqual(
            [level.ack_timeout_min for level in fast.levels], [2, 2, 5])

    def test_fast_policy_acks_in_time_on_quick_scenario(self):
        # cert-expiry: primary acks in 2m, inside the fast 2m timeout.
        result = simulate(get("cert-expiry"), get_policy("fast"))
        self.assertEqual(result.score.escalations, 0)
        self.assertEqual(result.score.time_to_ack_min, 2)
        self.assertEqual(result.score.points, 100)

    def test_fast_policy_late_ack_earns_partial_credit(self):
        # disk-full: primary takes 3m, past the fast 2m timeout, so the
        # ack is 1m late: credit 1 - 1/2 = 0.5. Base score is
        # 100 - (23-15)//2 = 96, so the effective score is 96 * 0.5 = 48.
        result = simulate(get("disk-full"), get_policy("fast"))
        self.assertEqual(result.score.escalations, 0)
        self.assertTrue(result.score.resolved)
        self.assertEqual(result.score.time_to_ack_min, 3)
        self.assertEqual(result.score.ack_late_min, 1)
        self.assertEqual(result.score.ack_credit, 0.5)
        self.assertEqual(result.score.points, 48)


class TestPolicyFile(unittest.TestCase):
    def _write_policy_file(self, data):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with handle:
            if isinstance(data, str):
                handle.write(data)
            else:
                json.dump(data, handle)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def _two_policy_data(self):
        return {"policies": {
            "relaxed": {"levels": [
                {"name": "ana", "role": "primary", "ack_timeout_min": 10},
                {"name": "ben", "role": "secondary", "ack_timeout_min": 10},
                {"name": "carla", "role": "manager", "ack_timeout_min": 20},
            ]},
            "brisk": {"levels": [
                {"name": "ana", "role": "primary", "ack_timeout_min": 1},
                {"name": "ben", "role": "secondary", "ack_timeout_min": 1},
            ]},
        }}

    def test_loads_named_policies_with_timeouts(self):
        path = self._write_policy_file(self._two_policy_data())
        policies = load_policy_file(path)
        self.assertEqual(sorted(policies), ["brisk", "relaxed"])
        relaxed = policies["relaxed"]
        self.assertEqual(relaxed.name, "relaxed")
        self.assertEqual(
            [level.ack_timeout_min for level in relaxed.levels],
            [10, 10, 20])
        self.assertEqual(
            [level.responder.name for level in relaxed.levels],
            ["ana", "ben", "carla"])
        self.assertEqual(
            [level.responder.role for level in relaxed.levels],
            ["primary", "secondary", "manager"])

    def test_loaded_policy_changes_simulation(self):
        # latency-spike: primary misses, secondary acks 4m after the
        # primary timeout. With relaxed 10m timeouts that is 10 + 4 = 14m.
        path = self._write_policy_file(self._two_policy_data())
        result = simulate(get("latency-spike"),
                          load_policy_file(path)["relaxed"])
        self.assertEqual(result.score.escalations, 1)
        self.assertEqual(result.score.time_to_ack_min, 14)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_policy_file("/no/such/policy-file.json")

    def test_malformed_json_raises(self):
        path = self._write_policy_file("{not json")
        with self.assertRaises(json.JSONDecodeError):
            load_policy_file(path)

    def test_missing_policies_key_raises(self):
        path = self._write_policy_file({"levels": []})
        with self.assertRaises(ValueError):
            load_policy_file(path)

    def test_empty_policies_raises(self):
        path = self._write_policy_file({"policies": {}})
        with self.assertRaises(ValueError):
            load_policy_file(path)

    def test_empty_levels_raises(self):
        path = self._write_policy_file(
            {"policies": {"empty": {"levels": []}}})
        with self.assertRaises(ValueError):
            load_policy_file(path)

    def test_zero_timeout_raises(self):
        path = self._write_policy_file({"policies": {"bad": {"levels": [
            {"name": "ana", "role": "primary", "ack_timeout_min": 0},
        ]}}})
        with self.assertRaises(ValueError):
            load_policy_file(path)

    def test_missing_role_raises(self):
        path = self._write_policy_file({"policies": {"bad": {"levels": [
            {"name": "ana", "ack_timeout_min": 5},
        ]}}})
        with self.assertRaises(ValueError):
            load_policy_file(path)

    def test_cli_uses_file_policy_by_name(self):
        path = self._write_policy_file(self._two_policy_data())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "cert-expiry", "--policy-file", path,
                      "--policy", "relaxed"]), 0)
        out = buf.getvalue()
        self.assertIn("policy:   relaxed", out)
        self.assertIn("100/100", out)

    def test_cli_single_policy_file_needs_no_policy_flag(self):
        path = self._write_policy_file({"policies": {
            "relaxed": self._two_policy_data()["policies"]["relaxed"]}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "cert-expiry", "--policy-file", path]), 0)
        self.assertIn("policy:   relaxed", buf.getvalue())

    def test_cli_multi_policy_file_without_policy_flag_fails(self):
        path = self._write_policy_file(self._two_policy_data())
        self.assertEqual(
            main(["run", "cert-expiry", "--policy-file", path]), 2)

    def test_cli_missing_policy_file_fails(self):
        self.assertEqual(
            main(["run", "cert-expiry", "--policy-file",
                  "/no/such/policy-file.json"]), 2)

    def test_cli_invalid_policy_file_fails(self):
        path = self._write_policy_file("{not json")
        self.assertEqual(
            main(["run", "cert-expiry", "--policy-file", path]), 2)

    def test_cli_file_policy_overrides_builtin_name(self):
        # A file policy called "fast" wins over the built-in fast (2/2/5).
        path = self._write_policy_file({"policies": {"fast": {"levels": [
            {"name": "ana", "role": "primary", "ack_timeout_min": 7},
            {"name": "ben", "role": "secondary", "ack_timeout_min": 7},
            {"name": "carla", "role": "manager", "ack_timeout_min": 7},
        ]}}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "cert-expiry", "--policy-file", path,
                      "--policy", "fast", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(
            [level["ack_timeout_min"] for level in data["policy"]["levels"]],
            [7, 7, 7])


class TestRota(unittest.TestCase):
    def _write_rota_file(self, data):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with handle:
            if isinstance(data, str):
                handle.write(data)
            else:
                json.dump(data, handle)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def _rota_data(self):
        return {"rota": {
            "primary": ["ana", "dave", "erin"],
            "secondary": ["ben", "fay"],
            "manager": ["carla"],
        }}

    def _run_json(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(argv), 0)
        return json.loads(buf.getvalue())

    def test_loads_rota_by_role(self):
        path = self._write_rota_file(self._rota_data())
        rota = load_rota_file(path)
        self.assertEqual(rota, {
            "primary": ("ana", "dave", "erin"),
            "secondary": ("ben", "fay"),
            "manager": ("carla",),
        })

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_rota_file("/no/such/rota-file.json")

    def test_malformed_json_raises(self):
        path = self._write_rota_file("{not json")
        with self.assertRaises(json.JSONDecodeError):
            load_rota_file(path)

    def test_missing_rota_key_raises(self):
        path = self._write_rota_file({"rotation": {}})
        with self.assertRaises(ValueError):
            load_rota_file(path)

    def test_empty_rotation_list_raises(self):
        path = self._write_rota_file({"rota": {"primary": []}})
        with self.assertRaises(ValueError):
            load_rota_file(path)

    def test_empty_name_raises(self):
        path = self._write_rota_file({"rota": {"primary": ["ana", ""]}})
        with self.assertRaises(ValueError):
            load_rota_file(path)

    def test_apply_rota_picks_by_week_and_wraps(self):
        rota = load_rota_file(self._write_rota_file(self._rota_data()))
        names = [
            [level.responder.name for level in
             apply_rota(DEFAULT_POLICY, rota, week).levels]
            for week in (0, 1, 2, 3, 4)
        ]
        self.assertEqual(names, [
            ["ana", "ben", "carla"],
            ["dave", "fay", "carla"],
            ["erin", "ben", "carla"],
            ["ana", "fay", "carla"],
            ["dave", "ben", "carla"],
        ])

    def test_apply_rota_leaves_unlisted_roles_alone(self):
        rota = {"primary": ("dave",)}
        resolved = apply_rota(DEFAULT_POLICY, rota, 0)
        self.assertEqual(
            [level.responder.name for level in resolved.levels],
            ["dave", "ben", "carla"])
        self.assertEqual(
            [level.responder.role for level in resolved.levels],
            ["primary", "secondary", "manager"])

    def test_apply_rota_keeps_timeouts_and_ignores_unknown_roles(self):
        rota = {"primary": ("dave",), "pager-duty": ("zoe",)}
        resolved = apply_rota(DEFAULT_POLICY, rota, 0)
        self.assertEqual(
            [level.ack_timeout_min for level in resolved.levels],
            [5, 5, 10])

    def test_apply_rota_rejects_negative_week(self):
        rota = load_rota_file(self._write_rota_file(self._rota_data()))
        with self.assertRaises(ValueError):
            apply_rota(DEFAULT_POLICY, rota, -1)

    def test_current_rota_week_is_iso_week(self):
        import datetime
        self.assertEqual(
            current_rota_week(),
            datetime.date.today().isocalendar()[1])

    def test_cli_run_pages_rota_responder_and_records_week(self):
        path = self._write_rota_file(self._rota_data())
        data = self._run_json(["run", "cert-expiry", "--rota-file", path,
                               "--rota-week", "1", "--json"])
        self.assertEqual(data["rota_week"], 1)
        self.assertEqual(
            [level["responder"]["name"]
             for level in data["policy"]["levels"]],
            ["dave", "fay", "carla"])
        self.assertEqual(
            [level["ack_timeout_min"]
             for level in data["policy"]["levels"]],
            [5, 5, 10])

    def test_cli_run_prints_rota_week(self):
        path = self._write_rota_file(self._rota_data())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "cert-expiry", "--rota-file", path,
                      "--rota-week", "2"]), 0)
        out = buf.getvalue()
        self.assertIn("rota week: 2", out)
        self.assertIn("erin (primary)", out)

    def test_cli_run_without_week_uses_current_week(self):
        path = self._write_rota_file(self._rota_data())
        data = self._run_json(["run", "cert-expiry", "--rota-file", path,
                               "--json"])
        self.assertEqual(data["rota_week"], current_rota_week())

    def test_cli_run_without_rota_file_has_null_rota_week(self):
        data = self._run_json(["run", "cert-expiry", "--json"])
        self.assertIsNone(data["rota_week"])

    def test_cli_rota_week_without_rota_file_fails(self):
        self.assertEqual(
            main(["run", "cert-expiry", "--rota-week", "1"]), 2)

    def test_cli_negative_rota_week_fails(self):
        path = self._write_rota_file(self._rota_data())
        self.assertEqual(
            main(["run", "cert-expiry", "--rota-file", path,
                  "--rota-week", "-1"]), 2)

    def test_cli_missing_rota_file_fails(self):
        self.assertEqual(
            main(["run", "cert-expiry", "--rota-file",
                  "/no/such/rota-file.json"]), 2)

    def test_cli_invalid_rota_file_fails(self):
        path = self._write_rota_file("{not json")
        self.assertEqual(
            main(["run", "cert-expiry", "--rota-file", path]), 2)

    def test_cli_batch_records_rota_week(self):
        path = self._write_rota_file(self._rota_data())
        data = self._run_json(["run", "disk-full", "--seeds", "1,2",
                               "--rota-file", path, "--rota-week", "1",
                               "--json"])
        self.assertEqual(data["rota_week"], 1)
        for run in data["runs"]:
            self.assertEqual(run["rota_week"], 1)
            self.assertEqual(
                run["policy"]["levels"][0]["responder"]["name"], "dave")

    def test_cli_drill_pages_rota_responder(self):
        import sys
        path = self._write_rota_file(self._rota_data())
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("ack 3\nmitigate 20\nresolve 12\n")
        try:
            data = self._run_json(
                ["drill", "disk-full", "--rota-file", path,
                 "--rota-week", "2", "--json"])
        finally:
            sys.stdin = old_stdin
        self.assertEqual(data["rota_week"], 2)
        self.assertEqual(
            data["policy"]["levels"][0]["responder"]["name"], "erin")

    def test_replay_round_trips_rota_week(self):
        path = self._write_rota_file(self._rota_data())
        data = self._run_json(["run", "cert-expiry", "--rota-file", path,
                               "--rota-week", "1", "--json"])
        saved = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with saved:
            json.dump(data, saved)
        self.addCleanup(os.unlink, saved.name)
        loaded = load_saved(saved.name)
        self.assertEqual(loaded.result.rota_week, 1)
        self.assertEqual(
            loaded.result.policy.levels[0].responder.name, "dave")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["replay", saved.name]), 0)
        self.assertIn("rota week: 1", buf.getvalue())

    def test_replay_rejects_bad_rota_week(self):
        data = self._run_json(["run", "cert-expiry", "--json"])
        data["rota_week"] = -1
        saved = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with saved:
            json.dump(data, saved)
        self.addCleanup(os.unlink, saved.name)
        with self.assertRaises(ValueError):
            load_saved(saved.name)

    def test_whatif_keeps_rota_week_without_policy_swap(self):
        path = self._write_rota_file(self._rota_data())
        data = self._run_json(["run", "disk-full", "--rota-file", path,
                               "--rota-week", "1", "--json"])
        saved = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with saved:
            json.dump(data, saved)
        self.addCleanup(os.unlink, saved.name)
        out = self._run_json(["what-if", saved.name, "--difficulty",
                              "hard", "--json"])
        self.assertEqual(out["what_if"]["rota_week"], 1)
        self.assertEqual(
            out["what_if"]["policy"]["levels"][0]["responder"]["name"],
            "dave")

    def test_whatif_drops_rota_week_when_policy_swapped(self):
        path = self._write_rota_file(self._rota_data())
        data = self._run_json(["run", "disk-full", "--rota-file", path,
                               "--rota-week", "1", "--json"])
        saved = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with saved:
            json.dump(data, saved)
        self.addCleanup(os.unlink, saved.name)
        out = self._run_json(["what-if", saved.name, "--policy", "fast",
                              "--json"])
        self.assertIsNone(out["what_if"]["rota_week"])
        self.assertEqual(
            out["what_if"]["policy"]["levels"][0]["responder"]["name"],
            "ana")


class TestPolicyTemplates(unittest.TestCase):
    def _write_policy_file(self, data):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with handle:
            if isinstance(data, str):
                handle.write(data)
            else:
                json.dump(data, handle)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_policy_to_dict_matches_standard_policy(self):
        self.assertEqual(policy_to_dict(DEFAULT_POLICY), {"levels": [
            {"name": "ana", "role": "primary", "ack_timeout_min": 5},
            {"name": "ben", "role": "secondary", "ack_timeout_min": 5},
            {"name": "carla", "role": "manager", "ack_timeout_min": 10},
        ]})

    def test_policy_template_defaults_to_standard(self):
        self.assertEqual(policy_template("standard"), {"policies": {
            "standard": policy_to_dict(DEFAULT_POLICY)}})

    def test_policy_template_uses_fast_timeouts(self):
        data = policy_template("fast")
        self.assertEqual(
            [level["ack_timeout_min"]
             for level in data["policies"]["fast"]["levels"]],
            [2, 2, 5])

    def test_policy_template_unknown_policy_raises(self):
        with self.assertRaises(KeyError):
            policy_template("no-such-policy")

    def test_cli_policy_template_prints_shareable_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["policy-template"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data, {"policies": {
            "standard": policy_to_dict(DEFAULT_POLICY)}})

    def test_cli_policy_template_round_trips_through_policy_file(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["policy-template", "fast"]), 0)
        path = self._write_policy_file(buf.getvalue())
        policies = load_policy_file(path)
        self.assertEqual(policies["fast"], get_policy("fast"))

    def test_cli_policy_template_unknown_policy_fails(self):
        self.assertEqual(main(["policy-template", "no-such-policy"]), 2)

    def test_cli_policy_template_out_writes_file(self):
        path = os.path.join(tempfile.gettempdir(),
                            "incident-lab-policy-template.json")
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["policy-template", "standard", "--out", path]), 0)
        self.assertIn("wrote policy template", buf.getvalue())
        self.assertIn(path, buf.getvalue())
        policies = load_policy_file(path)
        self.assertEqual(policies["standard"], DEFAULT_POLICY)

    def test_cli_policy_template_out_bad_path_fails(self):
        self.assertEqual(
            main(["policy-template", "--out", "/no/such/dir/policy.json"]),
            2)

    def test_template_can_be_edited_and_used_for_a_run(self):
        # The whole point of the template: save it, edit a timeout,
        # pass it back with --policy-file and the run uses the edit.
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["policy-template"]), 0)
        data = json.loads(buf.getvalue())
        data["policies"]["standard"]["levels"][0]["ack_timeout_min"] = 1
        path = self._write_policy_file(data)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--policy-file", path,
                      "--json"]), 0)
        run_data = json.loads(buf.getvalue())
        self.assertEqual(
            [level["ack_timeout_min"]
             for level in run_data["policy"]["levels"]],
            [1, 5, 10])
        # disk-full's primary acks in 3m, past the edited 1m timeout:
        # a late ack still counts (no escalation), but it is 2m late
        # against a 1m timeout, so the credit decays to zero.
        self.assertEqual(run_data["score"]["escalations"], 0)
        self.assertEqual(run_data["score"]["ack_late_min"], 2)
        self.assertEqual(run_data["score"]["ack_credit"], 0.0)
        self.assertEqual(run_data["score"]["points"], 0)


class TestTimeline(unittest.TestCase):
    def test_events_are_in_time_order_for_every_scenario(self):
        for name in list_scenarios():
            result = simulate(get(name), DEFAULT_POLICY)
            times = [event.t_min for event in result.timeline]
            self.assertEqual(times, sorted(times), f"scenario {name}")

    def test_out_of_order_record_is_rejected(self):
        timeline = Timeline()
        timeline.record(10, "paged", "ana")
        with self.assertRaises(ValueError):
            timeline.record(9, "acked", "ana")

    def test_render_contains_each_event(self):
        result = simulate(get("cert-expiry"), DEFAULT_POLICY)
        rendered = result.timeline.render()
        for kind in ("declared", "paged", "acked", "mitigated", "resolved"):
            self.assertIn(kind, rendered)


class TestScoring(unittest.TestCase):
    def test_scoring_math_on_known_timelines(self):
        # cert-expiry: ack 2m, mitigate 10m, no escalations -> 100.
        self.assertEqual(
            simulate(get("cert-expiry"), DEFAULT_POLICY).score.points, 100)
        # disk-full: ack 3m, mitigate 23m, no escalations ->
        # 100 - 0 - (23-15)//2 = 96.
        self.assertEqual(
            simulate(get("disk-full"), DEFAULT_POLICY).score.points, 96)
        # latency-spike: ack 9m, mitigate 44m, 1 escalation ->
        # 100 - 15 - (9-5) - (44-15)//2 = 67.
        self.assertEqual(
            simulate(get("latency-spike"), DEFAULT_POLICY).score.points, 67)
        # deploy-regression: ack 16m, mitigate 61m, 2 escalations ->
        # 100 - 30 - (16-5) - (61-15)//2 = 36.
        self.assertEqual(
            simulate(get("deploy-regression"), DEFAULT_POLICY).score.points,
            36)
        # queue-backlog: ack 4m, mitigate 29m, no escalations ->
        # 100 - 0 - (29-15)//2 = 93.
        self.assertEqual(
            simulate(get("queue-backlog"), DEFAULT_POLICY).score.points, 93)
        # dns-outage: ack 8m, mitigate 48m, 1 escalation ->
        # 100 - 15 - (8-5) - (48-15)//2 = 66.
        self.assertEqual(
            simulate(get("dns-outage"), DEFAULT_POLICY).score.points, 66)

    def test_new_scenarios_fields_match_timeline(self):
        queue = simulate(get("queue-backlog"), DEFAULT_POLICY).score
        self.assertEqual(queue.time_to_ack_min, 4)
        self.assertEqual(queue.time_to_mitigate_min, 29)
        self.assertEqual(queue.time_to_resolve_min, 44)
        self.assertEqual(queue.escalations, 0)
        dns = simulate(get("dns-outage"), DEFAULT_POLICY).score
        self.assertEqual(dns.time_to_ack_min, 8)
        self.assertEqual(dns.time_to_mitigate_min, 48)
        self.assertEqual(dns.time_to_resolve_min, 73)
        self.assertEqual(dns.escalations, 1)

    def test_score_fields_match_timeline(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        score = result.score
        self.assertEqual(score.time_to_ack_min, 3)
        self.assertEqual(score.time_to_mitigate_min, 23)
        self.assertEqual(score.time_to_resolve_min, 35)


class TestLateAckPartialCredit(unittest.TestCase):
    def _scenario(self, delay, timeout=5, mitigate=8, resolve=5):
        return Scenario(
            name="late", service="svc", title="late", severity="high",
            description="", ack_delay_min=(delay, None, None),
            mitigate_delay_min=mitigate, resolve_delay_min=resolve,
        )

    def test_on_time_ack_keeps_full_credit(self):
        result = simulate(get("cert-expiry"), DEFAULT_POLICY)
        self.assertEqual(result.score.ack_late_min, 0)
        self.assertEqual(result.score.ack_credit, 1.0)
        self.assertEqual(result.score.points, 100)

    def test_ack_exactly_at_timeout_keeps_full_credit(self):
        result = simulate(self._scenario(5), DEFAULT_POLICY)
        self.assertEqual(result.score.ack_late_min, 0)
        self.assertEqual(result.score.ack_credit, 1.0)

    def test_late_ack_keeps_partial_credit(self):
        # 3m ack against a 2m timeout: 1m late, credit 1 - 1/2 = 0.5.
        # Base score 100 - (23-15)//2 = 96, effective score 96 * 0.5 = 48.
        result = simulate(get("disk-full"), get_policy("fast"))
        self.assertEqual(result.score.time_to_ack_min, 3)
        self.assertEqual(result.score.ack_late_min, 1)
        self.assertEqual(result.score.ack_credit, 0.5)
        self.assertEqual(result.score.points, 48)
        self.assertTrue(result.score.resolved)
        self.assertEqual(result.score.escalations, 0)

    def test_late_ack_credit_decays_linearly(self):
        # 7m ack against a 5m timeout: 2m late, credit 1 - 2/5 = 0.6.
        # Base score 100 - (7-5) - (15-15)//2 = 98, effective 98 * 0.6
        # = 58.8, rounded to 59.
        result = simulate(self._scenario(7), DEFAULT_POLICY)
        self.assertEqual(result.score.ack_late_min, 2)
        self.assertAlmostEqual(result.score.ack_credit, 0.6)
        self.assertEqual(result.score.points, 59)

    def test_ack_at_twice_the_timeout_keeps_no_credit(self):
        # 10m ack against a 5m timeout: 5m late, credit 0. The incident
        # still resolves, but at 0 points.
        result = simulate(self._scenario(10), DEFAULT_POLICY)
        self.assertEqual(result.score.ack_late_min, 5)
        self.assertEqual(result.score.ack_credit, 0.0)
        self.assertEqual(result.score.points, 0)
        self.assertTrue(result.score.resolved)
        self.assertEqual(result.score.time_to_ack_min, 10)

    def test_very_late_ack_keeps_no_credit(self):
        # 15m ack against a 5m timeout: 10m late, past twice the timeout.
        result = simulate(self._scenario(15), DEFAULT_POLICY)
        self.assertEqual(result.score.ack_late_min, 10)
        self.assertEqual(result.score.ack_credit, 0.0)
        self.assertEqual(result.score.points, 0)
        self.assertTrue(result.score.resolved)

    def test_unacked_run_has_no_lateness_or_credit(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        result = simulate(scenario, DEFAULT_POLICY)
        self.assertIsNone(result.score.ack_late_min)
        self.assertIsNone(result.score.ack_credit)
        self.assertEqual(result.score.points, 0)

    def test_late_ack_timeline_marks_the_lateness(self):
        result = simulate(get("disk-full"), get_policy("fast"))
        acked = [event for event in result.timeline
                 if event.kind == "acked"]
        self.assertEqual(len(acked), 1)
        self.assertEqual(acked[0].t_min, 3)
        self.assertIn("late by 1m", acked[0].detail)
        self.assertIn("50%", acked[0].detail)

    def test_json_reports_lateness_credit_and_effective_score(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--policy", "fast", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["score"]["ack_late_min"], 1)
        self.assertEqual(data["score"]["ack_credit"], 0.5)
        self.assertEqual(data["score"]["points"], 48)
        self.assertEqual(data["score"]["time_to_ack_min"], 3)

    def test_console_reports_lateness_credit_and_effective_score(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--policy", "fast"]), 0)
        out = buf.getvalue()
        self.assertIn("late by:", out)
        self.assertIn("1m", out)
        self.assertIn("ack credit:", out)
        self.assertIn("50%", out)
        self.assertIn("48/100", out)

    def test_late_ack_replay_round_trip(self):
        original = simulate(get("disk-full"), get_policy("fast"))
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(original.to_dict(), path)
        self.addCleanup(os.unlink, path.name)
        loaded = load_result(path.name)
        self.assertEqual(loaded.score.to_dict(), original.score.to_dict())
        json_buf = io.StringIO()
        with redirect_stdout(json_buf):
            self.assertEqual(main(["replay", path.name]), 0)
        run_buf = io.StringIO()
        with redirect_stdout(run_buf):
            self.assertEqual(
                main(["run", "disk-full", "--policy", "fast"]), 0)
        self.assertEqual(json_buf.getvalue(), run_buf.getvalue())


class TestCli(unittest.TestCase):
    def test_list_names_all_scenarios(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["list"]), 0)
        for name in list_scenarios():
            self.assertIn(name, buf.getvalue())

    def test_run_prints_timeline_and_score(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "cert-expiry"]), 0)
        out = buf.getvalue()
        self.assertIn("acked", out)
        self.assertIn("points:", out)
        self.assertIn("100/100", out)

    def test_run_unknown_scenario_fails(self):
        self.assertEqual(main(["run", "no-such-scenario"]), 2)

    def test_run_fast_policy_changes_outcome(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "cert-expiry", "--policy", "fast"]),
                             0)
        out = buf.getvalue()
        self.assertIn("policy:   fast", out)
        self.assertIn("100/100", out)

    def test_run_fast_policy_json_names_policy(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "cert-expiry", "--policy", "fast", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["policy"]["name"], "fast")
        self.assertEqual(
            [level["ack_timeout_min"] for level in data["policy"]["levels"]],
            [2, 2, 5])

    def test_run_unknown_policy_fails(self):
        self.assertEqual(main(["run", "cert-expiry", "--policy", "bogus"]), 2)


class TestJsonOutput(unittest.TestCase):
    def test_event_and_timeline_to_dict(self):
        timeline = Timeline()
        timeline.record(0, "declared", "monitor", "disk 97% full")
        timeline.record(3, "acked", "ana")
        data = timeline.to_dict()
        self.assertEqual(data, {"events": [
            {"t_min": 0, "kind": "declared", "actor": "monitor",
             "detail": "disk 97% full"},
            {"t_min": 3, "kind": "acked", "actor": "ana", "detail": ""},
        ]})

    def test_score_to_dict_known_values(self):
        # disk-full: ack 3m, mitigate 23m, resolve 35m, no escalations, 96.
        # The mitigate delay is 20m (23-3) and the resolve delay 12m.
        score = simulate(get("disk-full"), DEFAULT_POLICY).score.to_dict()
        self.assertEqual(score, {
            "time_to_ack_min": 3,
            "time_to_mitigate_min": 23,
            "time_to_resolve_min": 35,
            "escalations": 0,
            "resolved": True,
            "points": 96,
            "ack_late_min": 0,
            "ack_credit": 1.0,
            "mitigate_delay_min": 20,
            "resolve_delay_min": 12,
            "hints_used": 0,
            "hint_cost_points": 0,
            # disk-full has a 99.9% SLO: 43.2m monthly budget, 23m of
            # downtime burns 53.2% of it.
            "slo_percent": 99.9,
            "downtime_min": 23,
            "error_budget_burn_pct": 53.2,
            # No regions to verify, so no verification time.
            "verify_delay_min": 0,
        })

    def test_result_to_dict_unacked_serializes_nulls(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        data = simulate(scenario, DEFAULT_POLICY).to_dict()
        self.assertIsNone(data["score"]["time_to_ack_min"])
        self.assertIsNone(data["score"]["time_to_mitigate_min"])
        self.assertIsNone(data["score"]["time_to_resolve_min"])
        self.assertEqual(data["score"]["points"], 0)
        self.assertFalse(data["score"]["resolved"])
        self.assertEqual(data["incident"]["id"], "ghost")
        self.assertEqual(data["policy"]["name"], "standard")
        self.assertEqual(len(data["policy"]["levels"]), 3)
        self.assertEqual(data["policy"]["levels"][0]["responder"]["name"],
                         "ana")
        self.assertEqual(data["policy"]["levels"][0]["ack_timeout_min"], 5)
        # JSON round-trip: nulls survive.
        self.assertEqual(json.loads(json.dumps(data)), data)

    def test_run_json_output_parses_and_matches_human_values(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "cert-expiry", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["incident"]["id"], "cert-expiry")
        self.assertEqual(data["incident"]["service"], "edge-tls")
        self.assertEqual(data["incident"]["severity"], "high")
        self.assertEqual(data["score"]["points"], 100)
        self.assertEqual(data["score"]["time_to_ack_min"], 2)
        self.assertEqual(
            [event["kind"] for event in data["timeline"]["events"]],
            ["declared", "paged", "acked", "mitigated", "resolved"])

    def test_run_json_latency_spike_values(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "latency-spike", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["score"]["points"], 67)
        self.assertEqual(data["score"]["time_to_ack_min"], 9)
        self.assertEqual(data["score"]["escalations"], 1)


class TestReplay(unittest.TestCase):
    def _write_result_file(self, data):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with handle:
            if isinstance(data, str):
                handle.write(data)
            else:
                json.dump(data, handle)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def _saved_run(self, scenario_name, policy=DEFAULT_POLICY):
        return simulate(get(scenario_name), policy).to_dict()

    def test_round_trip_rebuilds_the_same_result(self):
        original = simulate(get("dns-outage"), DEFAULT_POLICY)
        path = self._write_result_file(original.to_dict())
        loaded = load_result(path)
        self.assertEqual(loaded.incident.id, original.incident.id)
        self.assertEqual(loaded.incident.service, "edge-dns")
        self.assertEqual(loaded.incident.severity, "critical")
        self.assertEqual(loaded.policy.name, "standard")
        self.assertEqual(
            [level.ack_timeout_min for level in loaded.policy.levels],
            [5, 5, 10])
        self.assertEqual(loaded.score.to_dict(), original.score.to_dict())
        self.assertEqual(
            [event.to_dict() for event in loaded.timeline],
            [event.to_dict() for event in original.timeline])

    def test_unacked_run_round_trips_with_nulls(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        original = simulate(scenario, DEFAULT_POLICY)
        path = self._write_result_file(original.to_dict())
        loaded = load_result(path)
        self.assertFalse(loaded.score.resolved)
        self.assertEqual(loaded.score.points, 0)
        self.assertIsNone(loaded.score.time_to_ack_min)
        self.assertIn("unacked", loaded.timeline.kinds())

    def test_replay_output_matches_run_output(self):
        # Save a --json run, then replay it: the human-readable output
        # must be identical to what `run` printed for the same scenario.
        json_buf = io.StringIO()
        with redirect_stdout(json_buf):
            self.assertEqual(
                main(["run", "dns-outage", "--json"]), 0)
        path = self._write_result_file(json_buf.getvalue())
        replay_buf = io.StringIO()
        with redirect_stdout(replay_buf):
            self.assertEqual(main(["replay", path]), 0)
        run_buf = io.StringIO()
        with redirect_stdout(run_buf):
            self.assertEqual(main(["run", "dns-outage"]), 0)
        self.assertEqual(replay_buf.getvalue(), run_buf.getvalue())

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_result("/no/such/replay.json")

    def test_malformed_json_raises(self):
        path = self._write_result_file("{not json")
        with self.assertRaises(json.JSONDecodeError):
            load_result(path)

    def test_top_level_array_raises(self):
        path = self._write_result_file([1, 2, 3])
        with self.assertRaises(ValueError):
            load_result(path)

    def test_missing_score_raises(self):
        data = self._saved_run("cert-expiry")
        del data["score"]
        path = self._write_result_file(data)
        with self.assertRaises(ValueError):
            load_result(path)

    def test_out_of_order_events_raise(self):
        data = self._saved_run("cert-expiry")
        events = data["timeline"]["events"]
        # declared at 0m, acked at 2m: swapping them breaks time order.
        events[0], events[2] = events[2], events[0]
        path = self._write_result_file(data)
        with self.assertRaises(ValueError):
            load_result(path)

    def test_bad_severity_raises(self):
        data = self._saved_run("cert-expiry")
        data["incident"]["severity"] = "apocalyptic"
        path = self._write_result_file(data)
        with self.assertRaises(ValueError):
            load_result(path)

    def test_zero_timeout_raises(self):
        data = self._saved_run("cert-expiry")
        data["policy"]["levels"][0]["ack_timeout_min"] = 0
        path = self._write_result_file(data)
        with self.assertRaises(ValueError):
            load_result(path)

    def test_cli_missing_file_fails(self):
        self.assertEqual(main(["replay", "/no/such/replay.json"]), 2)

    def test_cli_malformed_json_fails(self):
        path = self._write_result_file("{not json")
        self.assertEqual(main(["replay", path]), 2)

    def test_cli_wrong_shape_fails(self):
        path = self._write_result_file({"incident": {"id": "x"}})
        self.assertEqual(main(["replay", path]), 2)


class TestAckTimeoutOverrides(unittest.TestCase):
    def test_positional_list_overrides_every_level(self):
        policy = parse_ack_timeout_overrides(["120,300,600"], DEFAULT_POLICY)
        self.assertEqual(
            [level.ack_timeout_min for level in policy.levels],
            [120, 300, 600])
        self.assertEqual(policy.name, "standard")

    def test_positional_tokens_may_be_space_separated(self):
        policy = parse_ack_timeout_overrides(
            ["120", "300", "600"], DEFAULT_POLICY)
        self.assertEqual(
            [level.ack_timeout_min for level in policy.levels],
            [120, 300, 600])

    def test_named_by_role_overrides_only_that_level(self):
        policy = parse_ack_timeout_overrides(["primary=7"], DEFAULT_POLICY)
        self.assertEqual(
            [level.ack_timeout_min for level in policy.levels], [7, 5, 10])

    def test_named_by_level_number_overrides_only_that_level(self):
        policy = parse_ack_timeout_overrides(["L3=20"], DEFAULT_POLICY)
        self.assertEqual(
            [level.ack_timeout_min for level in policy.levels], [5, 5, 20])

    def test_positional_override_changes_escalation_timing(self):
        # latency-spike: primary never acks, secondary acks 4m after the
        # primary timeout. With a 120m primary timeout that is 120 + 4.
        policy = parse_ack_timeout_overrides(["120,300,600"], DEFAULT_POLICY)
        result = simulate(get("latency-spike"), policy)
        self.assertEqual(result.score.escalations, 1)
        self.assertEqual(result.score.time_to_ack_min, 124)

    def test_named_override_very_late_ack_scores_zero(self):
        # disk-full: primary acks in 3m, but the override gives the
        # primary only 1m, so the ack is 2m late — past twice the timeout
        # — and keeps no credit. The incident still resolves, at 0 points.
        policy = parse_ack_timeout_overrides(["primary=1"], DEFAULT_POLICY)
        result = simulate(get("disk-full"), policy)
        self.assertEqual(result.score.escalations, 0)
        self.assertTrue(result.score.resolved)
        self.assertEqual(result.score.time_to_ack_min, 3)
        self.assertEqual(result.score.ack_late_min, 2)
        self.assertEqual(result.score.ack_credit, 0.0)
        self.assertEqual(result.score.points, 0)

    def test_level_number_override_very_late_ack_scores_zero(self):
        # latency-spike: primary times out at 5m, secondary acks 4m later
        # but the override gives the secondary only 1m, so the ack is 3m
        # late — past twice the timeout — and keeps no credit.
        policy = parse_ack_timeout_overrides(["L2=1"], DEFAULT_POLICY)
        result = simulate(get("latency-spike"), policy)
        self.assertEqual(result.score.escalations, 1)
        self.assertTrue(result.score.resolved)
        self.assertEqual(result.score.time_to_ack_min, 9)
        self.assertEqual(result.score.ack_late_min, 3)
        self.assertEqual(result.score.ack_credit, 0.0)
        self.assertEqual(result.score.points, 0)

    def test_zero_timeout_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_ack_timeout_overrides(["0,5,10"], DEFAULT_POLICY)

    def test_negative_timeout_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_ack_timeout_overrides(["primary=-3"], DEFAULT_POLICY)

    def test_non_integer_timeout_is_rejected(self):
        for bad in ("abc", "2.5", "5m"):
            with self.assertRaises(ValueError, msg=bad):
                parse_ack_timeout_overrides([f"primary={bad}"], DEFAULT_POLICY)

    def test_wrong_number_of_positional_timeouts_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_ack_timeout_overrides(["120,300"], DEFAULT_POLICY)
        with self.assertRaises(ValueError):
            parse_ack_timeout_overrides(["120,300,600,900"], DEFAULT_POLICY)

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_ack_timeout_overrides(["oncall=5"], DEFAULT_POLICY)

    def test_unknown_level_number_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_ack_timeout_overrides(["L9=5"], DEFAULT_POLICY)

    def test_mixing_positional_and_named_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_ack_timeout_overrides(
                ["120,300,600", "primary=5"], DEFAULT_POLICY)
        with self.assertRaises(ValueError):
            parse_ack_timeout_overrides(
                ["primary=5", "120,300,600"], DEFAULT_POLICY)

    def test_duplicate_level_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_ack_timeout_overrides(
                ["primary=1", "L1=2"], DEFAULT_POLICY)

    def test_cli_positional_overrides_appear_in_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "latency-spike", "--ack-timeouts",
                                   "120,300,600", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(
            [level["ack_timeout_min"] for level in data["policy"]["levels"]],
            [120, 300, 600])
        self.assertEqual(data["score"]["time_to_ack_min"], 124)
        self.assertEqual(data["score"]["escalations"], 1)

    def test_cli_named_overrides_appear_in_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "cert-expiry", "--ack-timeouts",
                                   "primary=120", "manager=30", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(
            [level["ack_timeout_min"] for level in data["policy"]["levels"]],
            [120, 5, 30])

    def test_cli_human_output_shows_effective_timeouts(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "cert-expiry", "--ack-timeouts",
                                   "120,300,600"]), 0)
        self.assertIn(
            "timeouts: ana (primary) 120m, ben (secondary) 300m, "
            "carla (manager) 600m", buf.getvalue())

    def test_cli_bad_timeout_fails(self):
        self.assertEqual(
            main(["run", "cert-expiry", "--ack-timeouts", "primary=0"]), 2)
        self.assertEqual(
            main(["run", "cert-expiry", "--ack-timeouts", "abc"]), 2)
        self.assertEqual(
            main(["run", "cert-expiry", "--ack-timeouts", "120,300"]), 2)

    def test_cli_both_timeout_flags_fail(self):
        self.assertEqual(
            main(["run", "cert-expiry", "--ack-timeout", "5",
                  "--ack-timeouts", "120,300,600"]), 2)

    def test_cli_overrides_apply_on_top_of_named_policy(self):
        # fast is 2/2/5; overriding only the primary gives 10/2/5.
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "cert-expiry", "--policy", "fast",
                                   "--ack-timeouts", "primary=10", "--json"]),
                             0)
        data = json.loads(buf.getvalue())
        self.assertEqual(
            [level["ack_timeout_min"] for level in data["policy"]["levels"]],
            [10, 2, 5])


class TestSeededAckDelays(unittest.TestCase):
    def test_same_seed_reproduces_identical_result(self):
        first = simulate(get("disk-full"), DEFAULT_POLICY, seed=7)
        second = simulate(get("disk-full"), DEFAULT_POLICY, seed=7)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_same_seed_reproduces_identical_json_bytes(self):
        def run_json():
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(
                    main(["run", "disk-full", "--seed", "7", "--json"]), 0)
            return buf.getvalue()
        self.assertEqual(run_json(), run_json())

    def test_different_seeds_vary_the_ack_delay(self):
        # disk-full's fixed ack is 3m; the seed jitters it by up to 1m,
        # so across many seeds at least two distinct ack times appear.
        ack_times = {
            simulate(get("disk-full"), DEFAULT_POLICY, seed=seed)
            .score.time_to_ack_min
            for seed in range(20)
        }
        self.assertGreater(len(ack_times), 1)

    def test_seeded_delay_stays_within_one_minute_of_fixed_delay(self):
        for seed in range(20):
            result = simulate(get("disk-full"), DEFAULT_POLICY, seed=seed)
            self.assertIn(result.score.time_to_ack_min, (2, 3, 4))

    def test_seed_is_recorded_in_json_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--seed", "7", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["seed"], 7)

    def test_unseeded_run_records_null_seed(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "disk-full", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertIsNone(data["seed"])

    def test_unseeded_run_uses_fixed_delays(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        self.assertIsNone(result.seed)
        self.assertEqual(result.score.time_to_ack_min, 3)

    def test_seed_shown_in_human_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "disk-full", "--seed", "7"]), 0)
        self.assertIn("seed:     7", buf.getvalue())

    def test_seed_does_not_change_who_acks(self):
        # latency-spike: primary misses, secondary acks. The seed only
        # jitters the acking responder's delay, so the escalation count
        # and the acking level stay the same for every seed.
        for seed in range(20):
            result = simulate(get("latency-spike"), DEFAULT_POLICY,
                              seed=seed)
            self.assertEqual(result.score.escalations, 1)
            self.assertIn(result.score.time_to_ack_min, (8, 9, 10))

    def test_seeded_unacked_run_records_the_seed(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        result = simulate(scenario, DEFAULT_POLICY, seed=3)
        self.assertEqual(result.seed, 3)
        self.assertFalse(result.score.resolved)

    def test_seeded_replay_round_trip(self):
        original = simulate(get("disk-full"), DEFAULT_POLICY, seed=7)
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(original.to_dict(), path)
        self.addCleanup(os.unlink, path.name)
        loaded = load_result(path.name)
        self.assertEqual(loaded.seed, 7)
        self.assertEqual(loaded.to_dict(), original.to_dict())
        replay_buf = io.StringIO()
        with redirect_stdout(replay_buf):
            self.assertEqual(main(["replay", path.name]), 0)
        run_buf = io.StringIO()
        with redirect_stdout(run_buf):
            self.assertEqual(main(["run", "disk-full", "--seed", "7"]), 0)
        self.assertEqual(replay_buf.getvalue(), run_buf.getvalue())

    def test_bad_seed_in_replay_file_raises(self):
        data = simulate(get("cert-expiry"), DEFAULT_POLICY).to_dict()
        data["seed"] = "seven"
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(data, path)
        self.addCleanup(os.unlink, path.name)
        with self.assertRaises(ValueError):
            load_result(path.name)


class TestJitterRange(unittest.TestCase):
    def _scenario(self, delay):
        return Scenario(
            name="jitter", service="svc", title="jitter", severity="high",
            description="", ack_delay_min=(delay, None, None),
            mitigate_delay_min=8, resolve_delay_min=5,
        )

    def test_jitter_range_parses(self):
        self.assertEqual(parse_jitter_range("0-3"), (0, 3))
        self.assertEqual(parse_jitter_range("2-2"), (2, 2))
        self.assertEqual(parse_jitter_range("0-0"), (0, 0))

    def test_jitter_range_rejects_bad_specs(self):
        for bad in ("3-1", "abc", "1", "1-2-3", "-1-2", "0--1", "1.5-3",
                    "1-x"):
            with self.assertRaises(ValueError, msg=bad):
                parse_jitter_range(bad)

    def test_same_seed_and_jitter_reproduce_identical_result(self):
        first = simulate(get("disk-full"), DEFAULT_POLICY, seed=7,
                         jitter=(0, 3))
        second = simulate(get("disk-full"), DEFAULT_POLICY, seed=7,
                          jitter=(0, 3))
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_jitter_zero_zero_means_no_jitter(self):
        # Magnitude is always 0, so every seed gives the fixed delay.
        for seed in range(10):
            result = simulate(get("disk-full"), DEFAULT_POLICY, seed=seed,
                              jitter=(0, 0))
            self.assertEqual(result.score.time_to_ack_min, 3)

    def test_jitter_bounds_respected(self):
        # Fixed delay 10, jitter 2-3: the ack lands at 7, 8, 12 or 13,
        # never closer to 10 and never further away.
        ack_times = {
            simulate(self._scenario(10), DEFAULT_POLICY, seed=seed,
                     jitter=(2, 3)).score.time_to_ack_min
            for seed in range(30)
        }
        self.assertTrue(ack_times <= {7, 8, 12, 13},
                        f"unexpected ack times: {ack_times}")
        self.assertGreater(len(ack_times), 1)

    def test_wider_jitter_varies_more_than_default(self):
        # Across 30 seeds, the 0-3 range reaches ack times the default
        # 0-1 range cannot.
        wide = {
            simulate(get("disk-full"), DEFAULT_POLICY, seed=seed,
                     jitter=(0, 3)).score.time_to_ack_min
            for seed in range(30)
        }
        self.assertTrue(wide - {2, 3, 4}, f"wide range stayed narrow: {wide}")

    def test_simulate_rejects_bad_jitter(self):
        for bad in ((3, 1), (-1, 2), (0, -1), (1,), (1, 2, 3), (1.5, 3)):
            with self.assertRaises(ValueError, msg=str(bad)):
                simulate(get("disk-full"), DEFAULT_POLICY, seed=1,
                         jitter=bad)

    def test_jitter_without_seed_is_ignored_by_simulate(self):
        # No RNG without a seed, so a jitter argument changes nothing.
        result = simulate(get("disk-full"), DEFAULT_POLICY, jitter=(0, 3))
        self.assertIsNone(result.seed)
        self.assertIsNone(result.jitter)
        self.assertEqual(result.score.time_to_ack_min, 3)

    def test_jitter_recorded_in_json_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "disk-full", "--seed", "7",
                                   "--jitter", "0-3", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["seed"], 7)
        self.assertEqual(data["jitter"], [0, 3])

    def test_jitter_defaults_to_zero_to_one_with_seed(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--seed", "7", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["jitter"], [0, 1])

    def test_unseeded_run_records_null_jitter(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "disk-full", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertIsNone(data["jitter"])

    def test_jitter_shown_in_human_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "disk-full", "--seed", "7",
                                   "--jitter", "0-3"]), 0)
        self.assertIn("jitter:   0-3m", buf.getvalue())

    def test_jitter_without_seed_fails(self):
        self.assertEqual(
            main(["run", "disk-full", "--jitter", "0-3"]), 2)

    def test_cli_bad_jitter_fails(self):
        for bad in ("3-1", "abc", "1", "1-2-3", "-1-2"):
            with self.subTest(bad=bad):
                # --jitter=<spec> so a leading "-" reaches the validator
                # instead of being read as an option flag by argparse.
                self.assertEqual(
                    main(["run", "disk-full", "--seed", "7",
                          f"--jitter={bad}"]), 2)

    def test_jitter_does_not_change_who_acks(self):
        # latency-spike: primary misses, secondary acks 4m after the
        # timeout. Even with a wide jitter the escalation count stays 1
        # and the ack stays within 5 + 4 +/- 3.
        for seed in range(20):
            result = simulate(get("latency-spike"), DEFAULT_POLICY,
                              seed=seed, jitter=(0, 3))
            self.assertEqual(result.score.escalations, 1)
            self.assertIn(result.score.time_to_ack_min, range(6, 13))

    def test_jitter_replay_round_trip(self):
        original = simulate(get("disk-full"), DEFAULT_POLICY, seed=7,
                            jitter=(0, 3))
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(original.to_dict(), path)
        self.addCleanup(os.unlink, path.name)
        loaded = load_result(path.name)
        self.assertEqual(loaded.jitter, (0, 3))
        self.assertEqual(loaded.to_dict(), original.to_dict())
        replay_buf = io.StringIO()
        with redirect_stdout(replay_buf):
            self.assertEqual(main(["replay", path.name]), 0)
        run_buf = io.StringIO()
        with redirect_stdout(run_buf):
            self.assertEqual(main(["run", "disk-full", "--seed", "7",
                                   "--jitter", "0-3"]), 0)
        self.assertEqual(replay_buf.getvalue(), run_buf.getvalue())

    def test_bad_jitter_in_replay_file_raises(self):
        data = simulate(get("cert-expiry"), DEFAULT_POLICY).to_dict()
        data["jitter"] = "wide"
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(data, path)
        self.addCleanup(os.unlink, path.name)
        with self.assertRaises(ValueError):
            load_result(path.name)

    def test_jitter_range_reversed_in_replay_file_raises(self):
        data = simulate(get("cert-expiry"), DEFAULT_POLICY).to_dict()
        data["jitter"] = [3, 0]
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(data, path)
        self.addCleanup(os.unlink, path.name)
        with self.assertRaises(ValueError):
            load_result(path.name)


class TestSeededMitigateResolveDelays(unittest.TestCase):
    def _scenario(self, mitigate, resolve):
        return Scenario(
            name="delays", service="svc", title="delays", severity="high",
            description="", ack_delay_min=(3, None, None),
            mitigate_delay_min=mitigate, resolve_delay_min=resolve,
        )

    def test_same_seed_reproduces_identical_delays(self):
        first = simulate(get("disk-full"), DEFAULT_POLICY, seed=7,
                         jitter=(0, 3))
        second = simulate(get("disk-full"), DEFAULT_POLICY, seed=7,
                          jitter=(0, 3))
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.score.mitigate_delay_min,
                         second.score.mitigate_delay_min)
        self.assertEqual(first.score.resolve_delay_min,
                         second.score.resolve_delay_min)

    def test_different_seeds_vary_mitigate_and_resolve_delays(self):
        # disk-full fixes mitigate at 20m and resolve at 12m; across
        # many seeds the seeded jitter must move both.
        mitigates = {
            simulate(get("disk-full"), DEFAULT_POLICY, seed=seed)
            .score.mitigate_delay_min
            for seed in range(30)
        }
        resolves = {
            simulate(get("disk-full"), DEFAULT_POLICY, seed=seed)
            .score.resolve_delay_min
            for seed in range(30)
        }
        self.assertGreater(len(mitigates), 1)
        self.assertGreater(len(resolves), 1)

    def test_seeded_delays_stay_within_jitter_of_fixed_delays(self):
        # Default jitter 0-1: mitigate 20 -> 19..21, resolve 12 -> 11..13.
        for seed in range(30):
            result = simulate(get("disk-full"), DEFAULT_POLICY, seed=seed)
            self.assertIn(result.score.mitigate_delay_min, (19, 20, 21))
            self.assertIn(result.score.resolve_delay_min, (11, 12, 13))

    def test_wider_jitter_moves_delays_further(self):
        # Fixed mitigate 20, resolve 12, jitter 2-3: mitigate lands at
        # 17, 18, 22 or 23 and resolve at 9, 10, 14 or 15.
        mitigates = {
            simulate(get("disk-full"), DEFAULT_POLICY, seed=seed,
                     jitter=(2, 3)).score.mitigate_delay_min
            for seed in range(30)
        }
        resolves = {
            simulate(get("disk-full"), DEFAULT_POLICY, seed=seed,
                     jitter=(2, 3)).score.resolve_delay_min
            for seed in range(30)
        }
        self.assertTrue(mitigates <= {17, 18, 22, 23},
                        f"unexpected mitigate delays: {mitigates}")
        self.assertTrue(resolves <= {9, 10, 14, 15},
                        f"unexpected resolve delays: {resolves}")
        self.assertGreater(len(mitigates), 1)
        self.assertGreater(len(resolves), 1)

    def test_jitter_zero_zero_keeps_fixed_delays(self):
        for seed in range(10):
            result = simulate(get("disk-full"), DEFAULT_POLICY, seed=seed,
                              jitter=(0, 0))
            self.assertEqual(result.score.mitigate_delay_min, 20)
            self.assertEqual(result.score.resolve_delay_min, 12)

    def test_unseeded_run_uses_fixed_delays(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        self.assertEqual(result.score.mitigate_delay_min, 20)
        self.assertEqual(result.score.resolve_delay_min, 12)
        self.assertIsNone(result.seed)

    def test_seeded_delays_never_below_one_minute(self):
        # Mitigate 2m and resolve 1m with a wide jitter: the clamp keeps
        # both steps at least 1 minute long.
        for seed in range(30):
            result = simulate(self._scenario(2, 1), DEFAULT_POLICY,
                              seed=seed, jitter=(0, 5))
            self.assertGreaterEqual(result.score.mitigate_delay_min, 1)
            self.assertGreaterEqual(result.score.resolve_delay_min, 1)

    def test_delays_recorded_in_json_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--seed", "7", "--json"]), 0)
        data = json.loads(buf.getvalue())
        score = data["score"]
        self.assertIsInstance(score["mitigate_delay_min"], int)
        self.assertIsInstance(score["resolve_delay_min"], int)
        # The recorded delays agree with the timeline: mitigate lands
        # ack + mitigate delay after t=0, resolve after that.
        self.assertEqual(
            score["mitigate_delay_min"],
            score["time_to_mitigate_min"] - score["time_to_ack_min"])
        self.assertEqual(
            score["resolve_delay_min"],
            score["time_to_resolve_min"] - score["time_to_mitigate_min"])

    def test_unseeded_json_records_fixed_delays(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "disk-full", "--json"]), 0)
        score = json.loads(buf.getvalue())["score"]
        self.assertEqual(score["mitigate_delay_min"], 20)
        self.assertEqual(score["resolve_delay_min"], 12)

    def test_seeded_unacked_run_records_null_delays(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        result = simulate(scenario, DEFAULT_POLICY, seed=3)
        self.assertIsNone(result.score.mitigate_delay_min)
        self.assertIsNone(result.score.resolve_delay_min)

    def test_seeded_delays_replay_round_trip(self):
        original = simulate(get("disk-full"), DEFAULT_POLICY, seed=7,
                            jitter=(0, 3))
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(original.to_dict(), path)
        self.addCleanup(os.unlink, path.name)
        loaded = load_result(path.name)
        self.assertEqual(loaded.score.mitigate_delay_min,
                         original.score.mitigate_delay_min)
        self.assertEqual(loaded.score.resolve_delay_min,
                         original.score.resolve_delay_min)
        self.assertEqual(loaded.to_dict(), original.to_dict())
        replay_buf = io.StringIO()
        with redirect_stdout(replay_buf):
            self.assertEqual(main(["replay", path.name]), 0)
        run_buf = io.StringIO()
        with redirect_stdout(run_buf):
            self.assertEqual(main(["run", "disk-full", "--seed", "7",
                                   "--jitter", "0-3"]), 0)
        self.assertEqual(replay_buf.getvalue(), run_buf.getvalue())

    def test_replay_file_without_delay_fields_loads_as_null(self):
        # Saved runs from before 0.11.0 have no delay fields.
        data = simulate(get("disk-full"), DEFAULT_POLICY).to_dict()
        del data["score"]["mitigate_delay_min"]
        del data["score"]["resolve_delay_min"]
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(data, path)
        self.addCleanup(os.unlink, path.name)
        loaded = load_result(path.name)
        self.assertIsNone(loaded.score.mitigate_delay_min)
        self.assertIsNone(loaded.score.resolve_delay_min)


class TestSeedBatch(unittest.TestCase):
    def test_seed_spec_parses_comma_list(self):
        self.assertEqual(parse_seed_spec("1,2,3"), [1, 2, 3])
        self.assertEqual(parse_seed_spec("1, 2, 3"), [1, 2, 3])

    def test_seed_spec_parses_range(self):
        self.assertEqual(parse_seed_spec("1-5"), [1, 2, 3, 4, 5])
        self.assertEqual(parse_seed_spec("3-3"), [3])

    def test_seed_spec_parses_single_seed(self):
        self.assertEqual(parse_seed_spec("7"), [7])

    def test_seed_spec_dedupes_keeping_order(self):
        self.assertEqual(parse_seed_spec("2,1,2"), [2, 1])

    def test_seed_spec_rejects_bad_specs(self):
        for bad in ("", "3-1", "abc", "1,,2", "1-2-3", "-1", "1,-2",
                    "1.5", "1-", "1,"):
            with self.assertRaises(ValueError, msg=bad):
                parse_seed_spec(bad)

    def test_batch_runs_once_per_seed(self):
        batch = simulate_batch(get("disk-full"), DEFAULT_POLICY, [1, 2, 3])
        self.assertEqual(batch.seeds, [1, 2, 3])
        self.assertEqual(len(batch.runs), 3)
        for run, seed in zip(batch.runs, [1, 2, 3]):
            self.assertEqual(run.seed, seed)
            self.assertEqual(run.score.points,
                             simulate(get("disk-full"), DEFAULT_POLICY,
                                      seed=seed).score.points)

    def test_batch_is_deterministic(self):
        first = simulate_batch(get("disk-full"), DEFAULT_POLICY, [1, 2, 3])
        second = simulate_batch(get("disk-full"), DEFAULT_POLICY, [1, 2, 3])
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_batch_distribution_matches_individual_runs(self):
        seeds = [1, 2, 3, 4]
        batch = simulate_batch(get("disk-full"), DEFAULT_POLICY, seeds)
        points = [simulate(get("disk-full"), DEFAULT_POLICY, seed=seed)
                  .score.points for seed in seeds]
        dist = batch.score_distribution()
        self.assertEqual(dist["min"], min(points))
        self.assertEqual(dist["max"], max(points))
        self.assertAlmostEqual(dist["mean"], sum(points) / len(points))
        self.assertEqual(dist["median"], statistics.median(points))
        self.assertEqual([entry["seed"] for entry in dist["per_seed"]],
                         seeds)
        self.assertEqual([entry["points"] for entry in dist["per_seed"]],
                         points)

    def test_batch_rejects_empty_seeds(self):
        with self.assertRaises(ValueError):
            simulate_batch(get("disk-full"), DEFAULT_POLICY, [])

    def test_batch_records_default_jitter(self):
        batch = simulate_batch(get("disk-full"), DEFAULT_POLICY, [1, 2])
        self.assertEqual(batch.jitter, (0, 1))

    def test_batch_json_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--seeds", "1,2,3", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["seeds"], [1, 2, 3])
        self.assertEqual(data["jitter"], [0, 1])
        self.assertEqual(len(data["runs"]), 3)
        self.assertEqual([run["seed"] for run in data["runs"]], [1, 2, 3])
        dist = data["score_distribution"]
        self.assertEqual(len(dist["per_seed"]), 3)
        self.assertEqual([entry["seed"] for entry in dist["per_seed"]],
                         [1, 2, 3])
        self.assertEqual(dist["min"], min(dist["per_seed"],
                                          key=lambda e: e["points"])["points"])
        self.assertEqual(dist["max"], max(dist["per_seed"],
                                          key=lambda e: e["points"])["points"])

    def test_batch_range_spec_via_cli(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--seeds", "1-3", "--json"]), 0)
        self.assertEqual(json.loads(buf.getvalue())["seeds"], [1, 2, 3])

    def test_batch_human_output_shows_distribution(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--seeds", "1,2,3"]), 0)
        out = buf.getvalue()
        self.assertIn("seeds:    1, 2, 3", out)
        self.assertIn("score distribution (3 runs):", out)
        self.assertIn("mean:", out)
        self.assertIn("median:", out)
        self.assertIn("min:", out)
        self.assertIn("max:", out)
        for seed in (1, 2, 3):
            self.assertIn(f"seed {seed}:", out)

    def test_batch_with_jitter(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--seeds", "1,2",
                      "--jitter", "0-3", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["jitter"], [0, 3])
        self.assertEqual([run["jitter"] for run in data["runs"]],
                         [[0, 3], [0, 3]])

    def test_seed_and_seeds_cannot_combine(self):
        self.assertEqual(
            main(["run", "disk-full", "--seed", "7", "--seeds", "1,2"]), 2)

    def test_cli_bad_seeds_fails(self):
        for bad in ("3-1", "abc", "1,,2", "-1"):
            with self.subTest(bad=bad):
                # --seeds=<spec> so a leading "-" reaches the validator
                # instead of being read as an option flag by argparse.
                self.assertEqual(
                    main(["run", "disk-full", f"--seeds={bad}"]), 2)

    def test_jitter_without_seed_or_seeds_fails(self):
        self.assertEqual(
            main(["run", "disk-full", "--jitter", "0-3"]), 2)


class TestDebrief(unittest.TestCase):
    def _save_run(self, result):
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(result.to_dict(), path)
        self.addCleanup(os.unlink, path.name)
        return path.name

    def test_clean_run_has_no_bad_findings_or_actions(self):
        # cert-expiry: primary acks in 2m, mitigates in 10m, resolves
        # in 15m, no escalations, 100 points.
        result = simulate(get("cert-expiry"), DEFAULT_POLICY)
        debrief = generate_debrief(result)
        self.assertEqual(debrief.points, 100)
        self.assertEqual(debrief.grade, "A")
        self.assertEqual(debrief.escalations, 0)
        self.assertEqual(debrief.went_badly, [])
        self.assertEqual(debrief.action_items, [])
        self.assertIn("Ack arrived on time (ana acked 2m after being "
                      "paged, within the 5m timeout)",
                      debrief.went_well)
        self.assertIn("Mitigated within the 15m target (10m)",
                      debrief.went_well)
        self.assertIn("Incident resolved in 15m", debrief.went_well)

    def test_missed_ack_becomes_bad_finding_and_action(self):
        # latency-spike: primary misses, secondary acks; mitigation
        # takes 44m, past the 15m target.
        result = simulate(get("latency-spike"), DEFAULT_POLICY)
        debrief = generate_debrief(result)
        self.assertIn("ana (primary) missed the page — no ack within 5m",
                      debrief.went_badly)
        self.assertIn("Mitigation took 44m, 29m past the 15m target",
                      debrief.went_badly)
        owners = {item["owner"] for item in debrief.action_items}
        self.assertEqual(owners, {"ana", "on-call lead"})
        ana_item = next(item for item in debrief.action_items
                        if item["owner"] == "ana")
        self.assertIn("paging delivery", ana_item["action"])

    def test_late_ack_keeps_partial_credit_finding(self):
        # disk-full on the fast policy: primary acks 1m late, 50%
        # credit, 48 points.
        from incident_lab.policy import get_policy
        result = simulate(get("disk-full"), get_policy("fast"))
        debrief = generate_debrief(result)
        self.assertEqual(debrief.points, 48)
        self.assertEqual(debrief.grade, "D")
        self.assertIn("Ack was 1m late, keeping only 50% of the score",
                      debrief.went_badly)
        self.assertEqual(debrief.action_items[0]["owner"], "ana")

    def test_nobody_acks_blames_every_level_and_adds_fallback(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=1, resolve_delay_min=1,
        )
        result = simulate(scenario, DEFAULT_POLICY)
        debrief = generate_debrief(result)
        self.assertEqual(debrief.points, 0)
        self.assertEqual(debrief.grade, "D")
        self.assertFalse(debrief.resolved)
        self.assertEqual(debrief.went_well, [])
        self.assertIn("Nobody acked the page; the incident was never "
                      "mitigated or resolved", debrief.went_badly)
        self.assertEqual(len(debrief.went_badly), 4)  # 3 misses + nobody
        fallback = next(item for item in debrief.action_items
                        if item["owner"] == "on-call lead")
        self.assertIn("fallback page", fallback["action"])

    def test_grade_boundaries(self):
        self.assertEqual(grade(100), "A")
        self.assertEqual(grade(90), "A")
        self.assertEqual(grade(89), "B")
        self.assertEqual(grade(75), "B")
        self.assertEqual(grade(74), "C")
        self.assertEqual(grade(50), "C")
        self.assertEqual(grade(49), "D")
        self.assertEqual(grade(0), "D")

    def test_debrief_dict_has_summary_findings_and_timeline(self):
        result = simulate(get("cert-expiry"), DEFAULT_POLICY)
        data = generate_debrief(result).to_dict()
        self.assertEqual(data["incident"]["id"], "cert-expiry")
        self.assertEqual(data["summary"]["points"], 100)
        self.assertEqual(data["summary"]["grade"], "A")
        self.assertEqual(data["went_badly"], [])
        self.assertEqual(data["action_items"], [])
        self.assertIn("acked", data["timeline"])

    def test_render_is_markdown_with_sections(self):
        result = simulate(get("latency-spike"), DEFAULT_POLICY)
        text = generate_debrief(result).render()
        self.assertIn("# Postmortem: p99 latency over 2s on checkout-api",
                      text)
        self.assertIn("## Timeline", text)
        self.assertIn("## What went well", text)
        self.assertIn("## What went badly", text)
        self.assertIn("## Action items", text)
        self.assertIn("- [ ] Check ana's paging delivery", text)
        self.assertIn("(owner: ana)", text)

    def test_render_with_no_findings_says_none(self):
        result = simulate(get("cert-expiry"), DEFAULT_POLICY)
        text = generate_debrief(result).render()
        self.assertIn("## What went badly\n\n- none", text)
        self.assertIn("## Action items\n\n- none", text)

    def test_cli_debrief_from_saved_run(self):
        result = simulate(get("cert-expiry"), DEFAULT_POLICY)
        path = self._save_run(result)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["debrief", path]), 0)
        self.assertIn("# Postmortem: TLS certificate expires in 6 hours",
                      buf.getvalue())
        self.assertIn("Score: 100/100 (grade A)", buf.getvalue())

    def test_cli_debrief_json(self):
        result = simulate(get("latency-spike"), DEFAULT_POLICY)
        path = self._save_run(result)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["debrief", path, "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["summary"]["escalations"], 1)
        # latency-spike: 44m of downtime against a 99.95% SLO (21.6m
        # monthly budget) burns 203.7% of the budget, so the debrief
        # adds a third action item about the SLO and the response.
        self.assertEqual(len(data["action_items"]), 3)
        self.assertEqual(data["action_items"][0]["owner"], "ana")
        self.assertEqual(data["summary"]["slo_percent"], 99.95)
        self.assertEqual(data["summary"]["downtime_min"], 44)
        self.assertEqual(data["summary"]["error_budget_burn_pct"], 203.7)
        self.assertTrue(any(
            "error budget" in item["action"]
            for item in data["action_items"]))

    def test_cli_debrief_missing_file_fails(self):
        self.assertEqual(main(["debrief", "/tmp/no-such-run.json"]), 2)


class TestScenarioTemplates(unittest.TestCase):
    def _write_scenario_file(self, data):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with handle:
            if isinstance(data, str):
                handle.write(data)
            else:
                json.dump(data, handle)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def _cache_down_data(self):
        return {"scenarios": {"cache-down": {
            "service": "sessions-cache",
            "title": "Cache cluster unreachable",
            "severity": "high",
            "description": "The sessions cache stopped accepting writes.",
            "ack_delay_min": [2, None, None],
            "mitigate_delay_min": 15,
            "resolve_delay_min": 10,
        }}}

    def test_template_prints_shareable_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["template", "disk-full"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data, {"scenarios": {
            "disk-full": scenario_to_dict(get("disk-full"))}})

    def test_template_round_trips_through_scenario_file(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["template", "dns-outage"]), 0)
        path = self._write_scenario_file(buf.getvalue())
        scenarios = load_scenario_file(path)
        self.assertEqual(scenarios["dns-outage"], get("dns-outage"))

    def test_template_unknown_scenario_fails(self):
        self.assertEqual(main(["template", "no-such-scenario"]), 2)

    def test_loads_named_scenarios(self):
        scenarios = load_scenario_file(
            self._write_scenario_file(self._cache_down_data()))
        self.assertEqual(list(scenarios), ["cache-down"])
        scenario = scenarios["cache-down"]
        self.assertEqual(scenario.service, "sessions-cache")
        self.assertEqual(scenario.severity, "high")
        self.assertEqual(scenario.ack_delay_min, (2, None, None))
        self.assertEqual(scenario.mitigate_delay_min, 15)
        self.assertEqual(scenario.resolve_delay_min, 10)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_scenario_file("/no/such/scenario-file.json")

    def test_malformed_json_raises(self):
        path = self._write_scenario_file("{not json")
        with self.assertRaises(json.JSONDecodeError):
            load_scenario_file(path)

    def test_missing_scenarios_key_raises(self):
        path = self._write_scenario_file({"policies": {}})
        with self.assertRaises(ValueError):
            load_scenario_file(path)

    def test_empty_scenarios_raises(self):
        path = self._write_scenario_file({"scenarios": {}})
        with self.assertRaises(ValueError):
            load_scenario_file(path)

    def test_bad_severity_raises(self):
        data = self._cache_down_data()
        data["scenarios"]["cache-down"]["severity"] = "catastrophic"
        path = self._write_scenario_file(data)
        with self.assertRaises(ValueError):
            load_scenario_file(path)

    def test_empty_ack_delays_raises(self):
        data = self._cache_down_data()
        data["scenarios"]["cache-down"]["ack_delay_min"] = []
        path = self._write_scenario_file(data)
        with self.assertRaises(ValueError):
            load_scenario_file(path)

    def test_zero_mitigate_delay_raises(self):
        data = self._cache_down_data()
        data["scenarios"]["cache-down"]["mitigate_delay_min"] = 0
        path = self._write_scenario_file(data)
        with self.assertRaises(ValueError):
            load_scenario_file(path)

    def test_cli_runs_file_scenario(self):
        path = self._write_scenario_file(self._cache_down_data())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "cache-down", "--scenario-file", path]), 0)
        out = buf.getvalue()
        self.assertIn("incident: Cache cluster unreachable [high]", out)
        self.assertIn("service:  sessions-cache", out)
        self.assertIn("points:           99/100", out)

    def test_cli_file_scenario_overrides_builtin_name(self):
        # A file scenario called "disk-full" wins over the built-in one.
        data = self._cache_down_data()
        data["scenarios"]["disk-full"] = data["scenarios"].pop("cache-down")
        path = self._write_scenario_file(data)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--scenario-file", path,
                      "--json"]), 0)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["incident"]["service"], "sessions-cache")

    def test_cli_missing_scenario_file_fails(self):
        self.assertEqual(
            main(["run", "cache-down", "--scenario-file",
                  "/no/such/scenario-file.json"]), 2)

    def test_cli_invalid_scenario_file_fails(self):
        path = self._write_scenario_file("{not json")
        self.assertEqual(
            main(["run", "cache-down", "--scenario-file", path]), 2)

    def test_cli_unknown_file_scenario_fails(self):
        path = self._write_scenario_file(self._cache_down_data())
        self.assertEqual(
            main(["run", "no-such-scenario", "--scenario-file", path]), 2)


class TestDifficulty(unittest.TestCase):
    def _run(self, scenario, difficulty):
        policy = apply_difficulty(DEFAULT_POLICY, difficulty)
        return simulate(get(scenario), policy, difficulty=difficulty)

    def test_parse_names_and_aliases(self):
        self.assertIs(parse_difficulty("easy"), EASY)
        self.assertIs(parse_difficulty("normal"), NORMAL)
        self.assertIs(parse_difficulty("hard"), HARD)
        self.assertIs(parse_difficulty("1"), EASY)
        self.assertIs(parse_difficulty("2"), NORMAL)
        self.assertIs(parse_difficulty("3"), HARD)
        self.assertIs(parse_difficulty(" HARD "), HARD)

    def test_parse_unknown_difficulty_raises(self):
        for bad in ("brutal", "0", "4", "", "medium"):
            with self.assertRaises(ValueError):
                parse_difficulty(bad)

    def test_list_difficulties_is_easiest_first(self):
        self.assertEqual(list_difficulties(), ["easy", "normal", "hard"])

    def test_easy_widens_timeouts_and_hard_tightens_them(self):
        easy = apply_difficulty(DEFAULT_POLICY, EASY)
        self.assertEqual(
            [level.ack_timeout_min for level in easy.levels], [8, 8, 15])
        hard = apply_difficulty(DEFAULT_POLICY, HARD)
        self.assertEqual(
            [level.ack_timeout_min for level in hard.levels], [3, 3, 6])
        normal = apply_difficulty(DEFAULT_POLICY, NORMAL)
        self.assertEqual(
            [level.ack_timeout_min for level in normal.levels], [5, 5, 10])

    def test_same_scenario_scores_differently_per_difficulty(self):
        # disk-full: ack 3m, mitigate 23m. Only the grace minutes move:
        # easy 100 - (23-20)//2 = 99, normal 100 - (23-15)//2 = 96,
        # hard 100 - (23-10)//2 = 94.
        self.assertEqual(self._run("disk-full", EASY).score.points, 99)
        self.assertEqual(self._run("disk-full", NORMAL).score.points, 96)
        self.assertEqual(self._run("disk-full", HARD).score.points, 94)

    def test_hard_escalates_faster_and_scores_lower(self):
        # latency-spike on hard: primary's timeout is 3m not 5m, so the
        # secondary is paged at 3m and acks at 7m; the ack is 1m late
        # (delay 4m vs a 3m timeout), so the base 65 keeps 2/3 credit.
        hard = self._run("latency-spike", HARD)
        self.assertEqual(hard.score.time_to_ack_min, 7)
        self.assertEqual(hard.score.points, 43)
        normal = self._run("latency-spike", NORMAL)
        self.assertEqual(normal.score.time_to_ack_min, 9)
        self.assertEqual(normal.score.points, 67)
        self.assertLess(hard.score.points, normal.score.points)

    def test_difficulty_defaults_to_normal(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        self.assertIs(result.difficulty, NORMAL)
        self.assertEqual(result.score.points, 96)

    def test_seeded_default_jitter_widens_with_difficulty(self):
        self.assertEqual(
            simulate(get("disk-full"), DEFAULT_POLICY, seed=7,
                     difficulty=EASY).jitter, (0, 0))
        self.assertEqual(
            simulate(get("disk-full"), DEFAULT_POLICY, seed=7,
                     difficulty=NORMAL).jitter, (0, 1))
        self.assertEqual(
            simulate(get("disk-full"), DEFAULT_POLICY, seed=7,
                     difficulty=HARD).jitter, (0, 3))

    def test_explicit_jitter_overrides_difficulty_default(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY, seed=7,
                          jitter=(0, 0), difficulty=HARD)
        self.assertEqual(result.jitter, (0, 0))

    def test_json_carries_the_difficulty(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--difficulty", "hard",
                      "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["difficulty"], "hard")

    def test_human_output_shows_difficulty_and_scaled_timeouts(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--difficulty", "hard"]), 0)
        out = buf.getvalue()
        self.assertIn("difficulty: hard", out)
        self.assertIn("ana (primary) 3m", out)
        self.assertIn("points:           94/100", out)

    def test_invalid_difficulty_fails_with_exit_code_2(self):
        self.assertEqual(
            main(["run", "disk-full", "--difficulty", "brutal"]), 2)

    def test_ack_timeout_override_still_wins_over_difficulty(self):
        # Hard scales the timeouts to 3/3/6, but an explicit override
        # sets them back to 5/5/10. The difficulty's scoring targets
        # still apply, so disk-full scores 94 (hard's 10m mitigate
        # grace) rather than the normal 96.
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--difficulty", "hard",
                      "--ack-timeouts", "5,5,10"]), 0)
        out = buf.getvalue()
        self.assertIn("ana (primary) 5m", out)
        self.assertIn("points:           94/100", out)

    def test_difficulty_replay_round_trip(self):
        result = self._run("disk-full", HARD)
        path = self._write_run(result)
        loaded = load_result(path)
        self.assertIs(loaded.difficulty, HARD)
        self.assertEqual(loaded.score.points, 94)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["replay", path]), 0)
        self.assertIn("difficulty: hard", buf.getvalue())

    def test_bad_difficulty_in_replay_file_raises(self):
        result = self._run("disk-full", HARD)
        data = result.to_dict()
        data["difficulty"] = "brutal"
        path = self._write_json(data)
        with self.assertRaises(ValueError):
            load_result(path)

    def test_old_run_without_difficulty_replays_as_none(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        data = result.to_dict()
        del data["difficulty"]
        path = self._write_json(data)
        loaded = load_result(path)
        self.assertIsNone(loaded.difficulty)

    def test_debrief_shows_difficulty_and_uses_its_targets(self):
        result = self._run("cert-expiry", EASY)
        debrief = generate_debrief(result)
        self.assertIs(debrief.difficulty, EASY)
        self.assertIn("Difficulty: easy", debrief.render())
        self.assertEqual(debrief.to_dict()["summary"]["difficulty"], "easy")
        # cert-expiry mitigates in 10m: inside easy's 20m target.
        self.assertIn("Mitigated within the 20m target",
                      " ".join(debrief.went_well))
        hard = generate_debrief(self._run("disk-full", HARD))
        self.assertIn("past the 10m target",
                      " ".join(hard.went_badly))

    def test_batch_json_carries_the_difficulty(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--seeds", "1,2",
                      "--difficulty", "hard", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["difficulty"], "hard")
        self.assertTrue(
            all(run["difficulty"] == "hard" for run in data["runs"]))

    def _write_run(self, result):
        return self._write_json(result.to_dict())

    def _write_json(self, data):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(data, handle)
        return handle.name


class TestCompare(unittest.TestCase):
    def _run(self, scenario, policy=DEFAULT_POLICY, **kwargs):
        return simulate(get(scenario), policy, **kwargs)

    def _write_run(self, result):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(result.to_dict(), handle)
        return handle.name

    def test_same_run_compares_identical(self):
        result = self._run("disk-full")
        comparison = compare_results(result, result)
        self.assertTrue(comparison.same_scenario)
        self.assertTrue(comparison.same_policy)
        self.assertTrue(comparison.timelines_identical)
        self.assertTrue(all(row["delta"] == 0
                            for row in comparison.score_rows))
        self.assertEqual(comparison.timeline_only_a, [])
        self.assertEqual(comparison.timeline_only_b, [])
        self.assertIn("identical", comparison.render())

    def test_different_scenarios_flagged_and_scored(self):
        a = self._run("disk-full")      # 96 points, no escalations
        b = self._run("latency-spike")  # 67 points, one escalation
        comparison = compare_results(a, b)
        self.assertFalse(comparison.same_scenario)
        self.assertIn("different scenarios", comparison.render())
        rows = {row["metric"]: row for row in comparison.score_rows}
        self.assertEqual(rows["points"]["delta"], 67 - 96)
        self.assertEqual(rows["escalations"]["delta"], 1)
        self.assertEqual(rows["time_to_ack_min"]["delta"], 9 - 3)
        # latency-spike escalates, so B has an escalation event A does
        # not; the declared events differ too (different titles), so
        # neither run's declared event matches the other's.
        self.assertTrue(any(event["kind"] == "escalated"
                            for event in comparison.timeline_only_b))
        self.assertTrue(any(event["kind"] == "declared"
                            for event in comparison.timeline_only_a))

    def test_seeded_runs_share_events_with_shifts(self):
        a = self._run("disk-full", seed=1)
        b = self._run("disk-full", seed=2)
        comparison = compare_results(a, b)
        self.assertTrue(comparison.same_scenario)
        self.assertEqual(len(comparison.timeline_matched), 5)
        self.assertEqual(comparison.timeline_only_a, [])
        self.assertEqual(comparison.timeline_only_b, [])
        for item in comparison.timeline_matched:
            self.assertEqual(item["shift"], item["t_b"] - item["t_a"])

    def test_unacked_run_has_null_deltas(self):
        nobody = Scenario(
            name="silent", service="svc", title="silent",
            severity="low", description="",
            ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=5,
        )
        unacked = simulate(nobody, DEFAULT_POLICY)
        acked = self._run("disk-full")
        comparison = compare_results(unacked, acked)
        rows = {row["metric"]: row for row in comparison.score_rows}
        self.assertIsNone(rows["time_to_ack_min"]["delta"])
        self.assertIsNone(rows["time_to_mitigate_min"]["delta"])
        self.assertEqual(rows["points"]["delta"], 96)
        self.assertIn("-", comparison.render())

    def test_different_policies_flagged(self):
        from incident_lab.policy import get_policy
        a = self._run("disk-full")
        b = self._run("disk-full", policy=get_policy("fast"))
        comparison = compare_results(a, b)
        self.assertFalse(comparison.same_policy)
        self.assertIn("different policies", comparison.render())

    def test_cli_compare_human_and_json(self):
        path_a = self._write_run(self._run("disk-full", seed=1))
        path_b = self._write_run(self._run("disk-full", seed=2))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["compare", path_a, path_b]), 0)
        output = buf.getvalue()
        self.assertIn(f"A: {path_a}", output)
        self.assertIn(f"B: {path_b}", output)
        self.assertIn("points", output)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["compare", path_a, path_b, "--json"]),
                             0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["same_scenario"])
        self.assertTrue(data["timeline"]["identical"]
                        or data["timeline"]["matched"])
        self.assertEqual(data["run_a"]["scenario"], "disk-full")

    def test_cli_compare_bad_files_fail(self):
        path = self._write_run(self._run("disk-full"))
        self.assertEqual(main(["compare", path, "/tmp/no-such-run.json"]),
                         2)
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            handle.write("{not json")
        self.assertEqual(main(["compare", path, handle.name]), 2)


class TestHints(unittest.TestCase):
    def test_every_builtin_scenario_has_hints(self):
        for name in list_scenarios():
            hints = get(name).hints
            self.assertGreaterEqual(len(hints), 1, name)
            for hint in hints:
                self.assertIsInstance(hint, str)
                self.assertTrue(hint, name)

    def test_one_hint_costs_five_points(self):
        # disk-full scores 96 with no hints; one hint costs 5.
        result = simulate(get("disk-full"), DEFAULT_POLICY, hints=1)
        self.assertEqual(result.score.points, 91)
        self.assertEqual(result.score.hints_used, 1)
        self.assertEqual(result.score.hint_cost_points, 5)
        self.assertEqual(len(result.hints), 1)
        self.assertEqual(result.hints[0].text, get("disk-full").hints[0])
        self.assertEqual(result.hints[0].cost_points, 5)

    def test_hints_drawn_in_order(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY, hints=2)
        self.assertEqual(result.score.points, 86)
        self.assertEqual(result.score.hints_used, 2)
        self.assertEqual(result.score.hint_cost_points, 10)
        self.assertEqual([hint.text for hint in result.hints],
                         list(get("disk-full").hints[:2]))

    def test_no_hints_by_default(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        self.assertEqual(result.score.points, 96)
        self.assertEqual(result.score.hints_used, 0)
        self.assertEqual(result.score.hint_cost_points, 0)
        self.assertEqual(result.hints, [])
        self.assertEqual(result.to_dict()["hints"], [])
        self.assertEqual(result.to_dict()["score"]["hints_used"], 0)

    def test_hint_cost_applies_after_late_ack_credit(self):
        # disk-full on the fast policy: base 96 scaled by 0.5 credit is
        # 48, then one hint costs 5 more.
        from incident_lab.policy import get_policy
        result = simulate(get("disk-full"), get_policy("fast"), hints=1)
        self.assertEqual(result.score.ack_credit, 0.5)
        self.assertEqual(result.score.points, 43)
        self.assertEqual(result.score.hint_cost_points, 5)

    def test_hint_cost_floors_at_zero(self):
        scenario = Scenario(
            name="costly", service="svc", title="costly", severity="low",
            description="", ack_delay_min=(30, None, None),
            mitigate_delay_min=100, resolve_delay_min=100,
            hints=("Check the obvious first.", "Check it again."),
        )
        result = simulate(scenario, DEFAULT_POLICY, hints=2)
        self.assertEqual(result.score.points, 0)
        self.assertEqual(result.score.hints_used, 2)
        self.assertEqual(result.score.hint_cost_points, 10)

    def test_unacked_run_records_hints_but_still_scores_zero(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
            hints=("Page the manager directly.",),
        )
        result = simulate(scenario, DEFAULT_POLICY, hints=1)
        self.assertFalse(result.score.resolved)
        self.assertEqual(result.score.points, 0)
        self.assertEqual(result.score.hints_used, 1)
        self.assertEqual(result.score.hint_cost_points, 5)
        self.assertEqual(len(result.hints), 1)

    def test_more_hints_than_available_fails(self):
        with self.assertRaises(ValueError):
            simulate(get("cert-expiry"), DEFAULT_POLICY, hints=3)

    def test_negative_hints_fails(self):
        with self.assertRaises(ValueError):
            simulate(get("disk-full"), DEFAULT_POLICY, hints=-1)

    def test_scenario_without_hints_rejects_any_hint(self):
        scenario = Scenario(
            name="plain", service="svc", title="plain", severity="low",
            description="", ack_delay_min=(2, None, None),
            mitigate_delay_min=5, resolve_delay_min=5,
        )
        self.assertEqual(scenario.hints, ())
        with self.assertRaises(ValueError):
            simulate(scenario, DEFAULT_POLICY, hints=1)

    def test_cli_run_hints_prints_hint_and_reduced_score(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "disk-full", "--hints", "1"]), 0)
        out = buf.getvalue()
        self.assertIn("hints used (1", out)
        self.assertIn(get("disk-full").hints[0], out)
        self.assertIn("hint cost:        -5 pts (1 hint(s))", out)
        self.assertIn("points:           91/100", out)

    def test_cli_run_hints_json_records_hints(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "disk-full", "--hints", "2", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["score"]["points"], 86)
        self.assertEqual(data["score"]["hints_used"], 2)
        self.assertEqual(data["score"]["hint_cost_points"], 10)
        self.assertEqual(
            [hint["text"] for hint in data["hints"]],
            list(get("disk-full").hints[:2]))
        self.assertTrue(all(hint["cost_points"] == 5
                            for hint in data["hints"]))

    def test_cli_run_too_many_hints_fails(self):
        self.assertEqual(main(["run", "cert-expiry", "--hints", "3"]), 2)

    def test_cli_run_negative_hints_fails(self):
        self.assertEqual(main(["run", "disk-full", "--hints", "-1"]), 2)

    def test_cli_run_hints_with_seeds_fails(self):
        self.assertEqual(
            main(["run", "disk-full", "--hints", "1", "--seeds", "1,2"]), 2)

    def test_replay_round_trips_hints(self):
        original = simulate(get("disk-full"), DEFAULT_POLICY, hints=2)
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(original.to_dict(), handle)
        loaded = load_result(handle.name)
        self.assertEqual(loaded.to_dict(), original.to_dict())
        self.assertEqual(loaded.score.hints_used, 2)
        self.assertEqual([hint.text for hint in loaded.hints],
                         list(get("disk-full").hints[:2]))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["replay", handle.name]), 0)
        self.assertIn(get("disk-full").hints[0], buf.getvalue())
        self.assertIn("points:           86/100", buf.getvalue())

    def test_replay_of_old_run_without_hints_defaults_to_none(self):
        data = simulate(get("disk-full"), DEFAULT_POLICY).to_dict()
        del data["hints"]
        del data["score"]["hints_used"]
        del data["score"]["hint_cost_points"]
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(data, handle)
        loaded = load_result(handle.name)
        self.assertEqual(loaded.hints, [])
        self.assertEqual(loaded.score.hints_used, 0)
        self.assertEqual(loaded.score.hint_cost_points, 0)

    def test_template_includes_hints(self):
        data = scenario_to_dict(get("disk-full"))
        self.assertEqual(data["hints"], list(get("disk-full").hints))

    def test_scenario_file_hints_load_and_validate(self):
        payload = {"scenarios": {"cache-down": {
            "service": "sessions-cache",
            "title": "Cache cluster unreachable",
            "severity": "high",
            "description": "The sessions cache stopped accepting writes.",
            "ack_delay_min": [2, None, None],
            "mitigate_delay_min": 15,
            "resolve_delay_min": 10,
            "hints": ["Check the cache cluster health endpoint first."],
        }}}
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(payload, handle)
        scenarios = load_scenario_file(handle.name)
        self.assertEqual(scenarios["cache-down"].hints,
                         ("Check the cache cluster health endpoint first.",))
        result = simulate(scenarios["cache-down"], DEFAULT_POLICY, hints=1)
        self.assertEqual(result.score.points, 94)

        bad = {"scenarios": {"broken": dict(
            payload["scenarios"]["cache-down"], hints=["ok", ""])}}
        with open(handle.name, "w", encoding="utf-8") as out:
            json.dump(bad, out)
        with self.assertRaises(ValueError):
            load_scenario_file(handle.name)
        bad = {"scenarios": {"broken": dict(
            payload["scenarios"]["cache-down"], hints="not a list")}}
        with open(handle.name, "w", encoding="utf-8") as out:
            json.dump(bad, out)
        with self.assertRaises(ValueError):
            load_scenario_file(handle.name)


class TestStats(unittest.TestCase):
    def _runs(self):
        # disk-full 96 (A), cert-expiry 100 (A), latency-spike 67 (C).
        return [
            simulate(get("disk-full"), DEFAULT_POLICY),
            simulate(get("cert-expiry"), DEFAULT_POLICY),
            simulate(get("latency-spike"), DEFAULT_POLICY),
        ]

    def _write_runs(self, results):
        paths = []
        for result in results:
            handle = tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8")
            with handle:
                json.dump(result.to_dict(), handle)
            paths.append(handle.name)
        return paths

    def test_points_distribution(self):
        stats = aggregate_stats(self._runs())
        self.assertEqual(stats.count, 3)
        self.assertAlmostEqual(stats.points_mean, (96 + 100 + 67) / 3)
        self.assertEqual(stats.points_min, 67)
        self.assertEqual(stats.points_max, 100)
        self.assertEqual(stats.points_median, 96)

    def test_mean_response_times(self):
        stats = aggregate_stats(self._runs())
        self.assertAlmostEqual(stats.mean_time_to_ack_min, (3 + 2 + 9) / 3)
        self.assertAlmostEqual(stats.mean_time_to_mitigate_min,
                               (23 + 10 + 44) / 3)
        self.assertAlmostEqual(stats.mean_time_to_resolve_min,
                               (35 + 15 + 64) / 3)
        self.assertEqual(stats.acked, 3)
        self.assertEqual(stats.resolved, 3)

    def test_grade_counts(self):
        stats = aggregate_stats(self._runs())
        self.assertEqual(stats.grade_counts, {"A": 2, "B": 0, "C": 1, "D": 0})

    def test_unacked_run_excluded_from_means(self):
        ghost = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        results = self._runs() + [simulate(ghost, DEFAULT_POLICY)]
        stats = aggregate_stats(results)
        self.assertEqual(stats.count, 4)
        self.assertEqual(stats.points_min, 0)
        self.assertEqual(stats.acked, 3)
        self.assertEqual(stats.resolved, 3)
        # Means only cover the runs that reached each milestone.
        self.assertAlmostEqual(stats.mean_time_to_ack_min, (3 + 2 + 9) / 3)
        self.assertAlmostEqual(stats.mean_time_to_resolve_min,
                               (35 + 15 + 64) / 3)
        self.assertEqual(stats.grade_counts["D"], 1)

    def test_no_run_acked_means_no_mean_ack(self):
        ghost = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        stats = aggregate_stats([simulate(ghost, DEFAULT_POLICY)])
        self.assertEqual(stats.acked, 0)
        self.assertIsNone(stats.mean_time_to_ack_min)
        self.assertIsNone(stats.mean_time_to_mitigate_min)
        self.assertIsNone(stats.mean_time_to_resolve_min)

    def test_same_scenario_flag_and_by_scenario(self):
        same = aggregate_stats([
            simulate(get("disk-full"), DEFAULT_POLICY),
            simulate(get("disk-full"), DEFAULT_POLICY, seed=3),
        ])
        self.assertTrue(same.same_scenario)
        self.assertEqual(same.by_scenario,
                         {"disk-full": {"runs": 2, "points_mean": 96.0}})
        mixed = aggregate_stats(self._runs())
        self.assertFalse(mixed.same_scenario)
        self.assertEqual(mixed.by_scenario["disk-full"]["runs"], 1)
        self.assertEqual(mixed.by_scenario["latency-spike"]["runs"], 1)

    def test_escalations_and_hints_totals(self):
        results = self._runs()
        hinted = simulate(get("disk-full"), DEFAULT_POLICY, hints=2)
        stats = aggregate_stats(results + [hinted])
        self.assertEqual(stats.escalations_total, 1)
        self.assertAlmostEqual(stats.escalations_mean, 0.25)
        self.assertEqual(stats.hints_total, 2)

    def test_empty_list_raises(self):
        with self.assertRaises(ValueError):
            aggregate_stats([])

    def test_label_count_must_match_runs(self):
        with self.assertRaises(ValueError):
            aggregate_stats(self._runs(), labels=["only-one"])

    def test_default_labels_number_runs(self):
        stats = aggregate_stats(self._runs())
        self.assertEqual([row["label"] for row in stats.runs],
                         ["run 1", "run 2", "run 3"])

    def test_cli_stats_human_and_json(self):
        paths = self._write_runs(self._runs())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["stats"] + paths), 0)
        out = buf.getvalue()
        self.assertIn("runs: 3", out)
        self.assertIn("mean:   87.7/100", out)
        self.assertIn("A: 2", out)
        self.assertIn("C: 1", out)
        self.assertIn("multiple scenarios", out)
        self.assertIn("by scenario:", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["stats"] + paths + ["--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["count"], 3)
        self.assertFalse(data["same_scenario"])
        self.assertEqual(data["points"]["min"], 67)
        self.assertEqual(data["points"]["max"], 100)
        self.assertEqual(data["grades"], {"A": 2, "B": 0, "C": 1, "D": 0})
        self.assertEqual(len(data["runs"]), 3)
        self.assertEqual(data["runs"][0]["label"], paths[0])
        self.assertEqual(data["runs"][0]["scenario"], "disk-full")

    def test_cli_stats_single_run(self):
        paths = self._write_runs([simulate(get("disk-full"),
                                           DEFAULT_POLICY)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["stats"] + paths), 0)
        out = buf.getvalue()
        self.assertIn("runs: 1", out)
        self.assertIn("mean:   96.0/100", out)
        self.assertNotIn("multiple scenarios", out)

    def test_cli_stats_bad_files_fail(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-run.json")
        self.assertEqual(main(["stats", missing]), 2)
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            handle.write("{not json")
        self.assertEqual(main(["stats", handle.name]), 2)
        # No run files at all is an argparse error (exit 2).
        with self.assertRaises(SystemExit) as caught:
            main(["stats"])
        self.assertEqual(caught.exception.code, 2)


class TestWhatIf(unittest.TestCase):
    def _run(self, scenario, policy=DEFAULT_POLICY, **kwargs):
        return simulate(get(scenario), policy, **kwargs)

    def _write_run(self, result):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(result.to_dict(), handle)
        return handle.name

    def test_no_overrides_reproduces_the_original(self):
        original = self._run("disk-full")
        outcome = what_if(original)
        self.assertEqual(outcome.replayed.score.points,
                         original.score.points)
        self.assertEqual(outcome.replayed.timeline.to_dict(),
                         original.timeline.to_dict())
        self.assertEqual(outcome.points_delta, 0)
        self.assertEqual(outcome.grade_delta, "A (unchanged)")
        self.assertEqual(outcome.overrides, [])

    def test_no_overrides_reproduces_a_seeded_run(self):
        # The recorded delays already carry the seed's jitter, so the
        # what-if replays them exactly instead of drawing jitter again.
        original = self._run("disk-full", seed=7, jitter=(0, 3))
        outcome = what_if(original)
        self.assertEqual(outcome.replayed.score.points,
                         original.score.points)
        self.assertEqual(outcome.replayed.timeline.to_dict(),
                         original.timeline.to_dict())

    def test_longer_timeout_turns_a_late_ack_into_an_on_time_ack(self):
        # disk-full on the fast policy: the 3m ack is 1m late against
        # the 2m timeout, so the credit halves the score to 48 (D).
        original = self._run("disk-full", get_policy("fast"))
        self.assertEqual(original.score.points, 48)
        outcome = what_if(original, ack_timeout=5)
        self.assertEqual(outcome.replayed.score.points, 96)
        self.assertEqual(outcome.replayed.score.ack_credit, 1.0)
        self.assertEqual(outcome.points_delta, 48)
        self.assertEqual(outcome.grade_delta, "D -> A")
        self.assertEqual(outcome.overrides,
                         ["ack timeout: every level 5m"])

    def test_named_ack_timeout_override(self):
        # latency-spike: the secondary acks 4m after the primary's
        # 5m timeout. Giving the secondary a 3m timeout makes that ack
        # late, so the score drops instead of rising.
        original = self._run("latency-spike")
        self.assertEqual(original.score.points, 67)
        outcome = what_if(original, ack_timeouts=["secondary=3"])
        self.assertEqual(outcome.replayed.score.ack_late_min, 1)
        self.assertLess(outcome.replayed.score.points,
                        original.score.points)
        self.assertEqual(outcome.points_delta,
                         outcome.replayed.score.points - 67)

    def test_positional_ack_timeout_override(self):
        original = self._run("disk-full", get_policy("fast"))
        outcome = what_if(original, ack_timeouts=["5,5,10"])
        self.assertEqual(outcome.replayed.score.points, 96)

    def test_difficulty_override_matches_a_fresh_run_at_that_difficulty(self):
        original = self._run("latency-spike")
        outcome = what_if(original, difficulty=HARD)
        fresh = self._run("latency-spike", apply_difficulty(
            DEFAULT_POLICY, HARD), difficulty=HARD)
        self.assertEqual(outcome.replayed.score.points,
                         fresh.score.points)
        self.assertEqual(outcome.replayed.timeline.to_dict(),
                         fresh.timeline.to_dict())
        self.assertEqual(outcome.overrides,
                         ["difficulty: normal -> hard"])

    def test_difficulty_override_from_a_hard_run_back_to_normal(self):
        original = self._run("disk-full",
                             apply_difficulty(DEFAULT_POLICY, HARD),
                             difficulty=HARD)
        outcome = what_if(original, difficulty=NORMAL)
        self.assertEqual(outcome.replayed.score.points, 96)
        self.assertEqual(
            [level.ack_timeout_min
             for level in outcome.replayed.policy.levels],
            [5, 5, 10])

    def test_difficulty_and_ack_timeout_combine_with_timeout_winning(self):
        original = self._run("disk-full", get_policy("fast"))
        outcome = what_if(original, ack_timeout=5, difficulty=HARD)
        # Hard scoring targets still apply (ack grace 3m, mitigate
        # grace 10m), but every timeout is the explicit 5m.
        self.assertEqual(outcome.replayed.score.points, 94)
        self.assertEqual(
            [level.ack_timeout_min
             for level in outcome.replayed.policy.levels],
            [5, 5, 5])

    def _write_policy_file(self, data):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with handle:
            if isinstance(data, str):
                handle.write(data)
            else:
                json.dump(data, handle)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_policy_override_replays_under_the_named_policy(self):
        # disk-full on the fast policy scores 48 (D): the 3m ack is
        # 1m late against the 2m timeout. Under standard the same ack
        # is on time and the incident scores 96 (A).
        original = self._run("disk-full", get_policy("fast"))
        self.assertEqual(original.score.points, 48)
        outcome = what_if(original, policy=DEFAULT_POLICY)
        self.assertEqual(outcome.replayed.score.points, 96)
        self.assertEqual(outcome.replayed.policy.name, "standard")
        self.assertEqual(
            [level.ack_timeout_min
             for level in outcome.replayed.policy.levels],
            [5, 5, 10])
        self.assertEqual(outcome.points_delta, 48)
        self.assertEqual(outcome.grade_delta, "D -> A")
        self.assertEqual(outcome.overrides,
                         ["policy: fast -> standard"])

    def test_policy_override_matches_a_fresh_run_at_that_policy(self):
        original = self._run("latency-spike")
        outcome = what_if(original, policy=get_policy("fast"))
        fresh = self._run("latency-spike", get_policy("fast"))
        self.assertEqual(outcome.replayed.score.points,
                         fresh.score.points)
        self.assertEqual(outcome.replayed.timeline.to_dict(),
                         fresh.timeline.to_dict())

    def test_policy_override_keeps_the_original_difficulty(self):
        # An easy run replayed under fast stays easy: the fast
        # timeouts are scaled by the easy factor (2m -> 3m, 5m -> 8m)
        # instead of being used as-is.
        original = self._run(
            "disk-full", apply_difficulty(DEFAULT_POLICY, EASY),
            difficulty=EASY)
        self.assertEqual(original.score.points, 99)
        outcome = what_if(original, policy=get_policy("fast"))
        self.assertEqual(
            [level.ack_timeout_min
             for level in outcome.replayed.policy.levels],
            [3, 3, 8])
        self.assertEqual(outcome.replayed.difficulty.name, "easy")
        fresh = self._run("disk-full",
                          apply_difficulty(get_policy("fast"), EASY),
                          difficulty=EASY)
        self.assertEqual(outcome.replayed.score.points,
                         fresh.score.points)

    def test_policy_and_difficulty_combine(self):
        original = self._run("disk-full")
        outcome = what_if(original, policy=get_policy("fast"),
                          difficulty=HARD)
        # Fast timeouts scaled to hard (2m -> 1m, 5m -> 3m); the 3m
        # ack is 2m late against the 1m timeout, so it keeps no credit.
        self.assertEqual(
            [level.ack_timeout_min
             for level in outcome.replayed.policy.levels],
            [1, 1, 3])
        self.assertEqual(outcome.overrides,
                         ["policy: standard -> fast",
                          "difficulty: normal -> hard"])
        fresh = self._run("disk-full",
                          apply_difficulty(get_policy("fast"), HARD),
                          difficulty=HARD)
        self.assertEqual(outcome.replayed.score.points,
                         fresh.score.points)

    def test_policy_and_ack_timeout_combine_with_timeout_winning(self):
        original = self._run("disk-full", get_policy("fast"))
        outcome = what_if(original, policy=DEFAULT_POLICY, ack_timeout=5)
        self.assertEqual(
            [level.ack_timeout_min
             for level in outcome.replayed.policy.levels],
            [5, 5, 5])
        self.assertEqual(outcome.replayed.score.points, 96)
        self.assertEqual(outcome.overrides,
                         ["policy: fast -> standard",
                          "ack timeout: every level 5m"])

    def test_policy_override_with_seeded_run_replays_recorded_delays(self):
        # The seeded ack delay is replayed exactly, not drawn again:
        # the time-to-ack is unchanged under the new policy.
        original = self._run("disk-full", get_policy("fast"),
                             seed=7, jitter=(0, 3))
        outcome = what_if(original, policy=DEFAULT_POLICY)
        self.assertEqual(outcome.replayed.score.time_to_ack_min,
                         original.score.time_to_ack_min)
        self.assertEqual(outcome.replayed.policy.name, "standard")

    def test_shorter_policy_chain_can_leave_the_incident_unacked(self):
        # latency-spike is acked by the secondary. Under a one-level
        # chain there is no secondary, so nobody acks.
        solo = EscalationPolicy(
            name="solo",
            levels=(Level(Responder("ana", "primary"), 5),))
        original = self._run("latency-spike")
        self.assertEqual(original.score.points, 67)
        outcome = what_if(original, policy=solo)
        self.assertEqual(outcome.replayed.score.points, 0)
        self.assertFalse(outcome.replayed.score.resolved)
        self.assertEqual(outcome.overrides,
                         ["policy: standard -> solo"])

    def test_unacked_run_stays_unacked(self):
        ghost = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        original = simulate(ghost, DEFAULT_POLICY)
        outcome = what_if(original, ack_timeout=30)
        self.assertEqual(outcome.replayed.score.points, 0)
        self.assertFalse(outcome.replayed.score.resolved)
        self.assertEqual(outcome.points_delta, 0)

    def test_hints_are_kept_and_still_cost_points(self):
        original = self._run("disk-full", hints=1)
        self.assertEqual(original.score.points, 91)
        outcome = what_if(original, ack_timeout=5)
        self.assertEqual(outcome.replayed.score.hints_used, 1)
        self.assertEqual(outcome.replayed.score.points, 91)

    def test_conflicting_ack_overrides_fail(self):
        original = self._run("disk-full")
        with self.assertRaises(ValueError):
            what_if(original, ack_timeout=5, ack_timeouts=["5,5,10"])

    def test_bad_ack_timeout_fails(self):
        original = self._run("disk-full")
        with self.assertRaises(ValueError):
            what_if(original, ack_timeout=0)
        with self.assertRaises(ValueError):
            what_if(original, ack_timeouts=["bogus=5"])

    def test_inconsistent_saved_run_fails(self):
        # latency-spike waits out the primary's 5m timeout before the
        # secondary acks, so a time-to-ack of 1m cannot be real.
        original = self._run("latency-spike")
        tampered = replace(
            original,
            score=replace(original.score, time_to_ack_min=1))
        with self.assertRaises(ValueError):
            what_if(tampered)

    def test_what_if_result_round_trips_to_json(self):
        original = self._run("disk-full", get_policy("fast"))
        outcome = what_if(original, ack_timeout=5)
        data = outcome.to_dict()
        self.assertEqual(data["points_delta"], 48)
        self.assertEqual(data["grade"], "D")
        self.assertEqual(data["what_if_grade"], "A")
        self.assertEqual(data["grade_delta"], "D -> A")
        self.assertEqual(data["what_if"]["score"]["points"], 96)
        self.assertEqual(data["original"]["score"]["points"], 48)
        # The JSON round-trips through load_result for both runs.
        for key in ("original", "what_if"):
            handle = tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8")
            with handle:
                json.dump(data[key], handle)
            self.assertEqual(load_result(handle.name).score.points,
                             data[key]["score"]["points"])

    def test_cli_what_if_human_and_json(self):
        path = self._write_run(self._run("disk-full",
                                         get_policy("fast")))
        with open(path, encoding="utf-8") as handle:
            before = handle.read()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["what-if", path, "--ack-timeout", "5"]),
                             0)
        out = buf.getvalue()
        self.assertIn("48/100 (grade D) -> 96/100 (grade A)", out)
        self.assertIn("delta:            +48 points", out)
        self.assertIn("what-if timeline:", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["what-if", path, "--ack-timeout", "5",
                                   "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["points_delta"], 48)
        self.assertEqual(data["what_if"]["score"]["points"], 96)
        # The original run file is not modified by the what-if.
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_cli_what_if_difficulty(self):
        path = self._write_run(self._run("latency-spike"))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["what-if", path, "--difficulty",
                                   "hard"]), 0)
        out = buf.getvalue()
        self.assertIn("override: difficulty: normal -> hard", out)
        self.assertIn("67/100 (grade C) -> 43/100 (grade D)", out)

    def test_cli_what_if_policy(self):
        path = self._write_run(self._run("disk-full",
                                         get_policy("fast")))
        with open(path, encoding="utf-8") as handle:
            before = handle.read()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["what-if", path, "--policy",
                                   "standard"]), 0)
        out = buf.getvalue()
        self.assertIn("override: policy: fast -> standard", out)
        self.assertIn("48/100 (grade D) -> 96/100 (grade A)", out)
        self.assertIn("delta:            +48 points", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["what-if", path, "--policy",
                                   "standard", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["points_delta"], 48)
        self.assertEqual(data["what_if"]["score"]["points"], 96)
        self.assertEqual(data["what_if"]["policy"]["name"], "standard")
        self.assertEqual(data["overrides"],
                         ["policy: fast -> standard"])
        # The original run file is not modified by the what-if.
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_cli_what_if_unknown_policy_fails(self):
        path = self._write_run(self._run("disk-full"))
        self.assertEqual(main(["what-if", path, "--policy", "bogus"]),
                         2)

    def test_cli_what_if_policy_file(self):
        policy_path = self._write_policy_file({"policies": {
            "relaxed": {"levels": [
                {"name": "ana", "role": "primary", "ack_timeout_min": 10},
                {"name": "ben", "role": "secondary",
                 "ack_timeout_min": 10},
                {"name": "carla", "role": "manager",
                 "ack_timeout_min": 20},
            ]},
            "brisk": {"levels": [
                {"name": "ana", "role": "primary", "ack_timeout_min": 1},
                {"name": "ben", "role": "secondary", "ack_timeout_min": 1},
            ]},
        }})
        path = self._write_run(self._run("disk-full",
                                         get_policy("fast")))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["what-if", path, "--policy-file",
                                   policy_path, "--policy", "relaxed"]),
                             0)
        out = buf.getvalue()
        self.assertIn("override: policy: fast -> relaxed", out)
        self.assertIn("48/100 (grade D) -> 96/100 (grade A)", out)
        # A file with exactly one policy needs no --policy flag.
        single_path = self._write_policy_file({"policies": {
            "relaxed": {"levels": [
                {"name": "ana", "role": "primary", "ack_timeout_min": 10},
                {"name": "ben", "role": "secondary",
                 "ack_timeout_min": 10},
                {"name": "carla", "role": "manager",
                 "ack_timeout_min": 20},
            ]}}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["what-if", path, "--policy-file",
                                   single_path]), 0)
        self.assertIn("override: policy: fast -> relaxed",
                      buf.getvalue())
        # A file with more than one policy and no --policy flag fails.
        self.assertEqual(main(["what-if", path, "--policy-file",
                              policy_path]), 2)
        # A missing or malformed policy file fails.
        self.assertEqual(main(["what-if", path, "--policy-file",
                              "/no/such/policy-file.json",
                              "--policy", "relaxed"]), 2)
        bad_path = self._write_policy_file("{not json")
        self.assertEqual(main(["what-if", path, "--policy-file",
                              bad_path, "--policy", "relaxed"]), 2)

    def test_cli_what_if_bad_files_and_args_fail(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-run.json")
        self.assertEqual(main(["what-if", missing, "--ack-timeout", "5"]),
                         2)
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            handle.write("{not json")
        self.assertEqual(main(["what-if", handle.name, "--ack-timeout",
                              "5"]), 2)
        path = self._write_run(self._run("disk-full"))
        # No override at all is not a what-if.
        self.assertEqual(main(["what-if", path]), 2)
        # Both ack override forms conflict.
        self.assertEqual(main(["what-if", path, "--ack-timeout", "5",
                               "--ack-timeouts", "5,5,10"]), 2)
        # Unknown difficulty.
        self.assertEqual(main(["what-if", path, "--difficulty",
                              "brutal"]), 2)
        # A batch file is not a single run.
        batch = simulate_batch(get("disk-full"), DEFAULT_POLICY, [1, 2])
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(batch.to_dict(), handle)
        self.assertEqual(main(["what-if", handle.name, "--ack-timeout",
                              "5"]), 2)


class TestWhatIfInCompareAndStats(unittest.TestCase):
    def _run(self, scenario, policy=DEFAULT_POLICY, **kwargs):
        return simulate(get(scenario), policy, **kwargs)

    def _write_json(self, data):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(data, handle)
        return handle.name

    def _whatif_path(self):
        # disk-full on the fast policy scores 48 (grade D); replayed
        # with 5m timeouts it scores 96 (grade A).
        original = self._run("disk-full", get_policy("fast"))
        outcome = what_if(original, ack_timeout=5)
        return self._write_json(outcome.to_dict()), original, outcome

    def test_load_saved_normal_run_has_no_what_if(self):
        path = self._write_json(self._run("disk-full").to_dict())
        loaded = load_saved(path)
        self.assertEqual(loaded.result.score.points, 96)
        self.assertIsNone(loaded.what_if)

    def test_load_saved_what_if_returns_replay_and_provenance(self):
        path, _, outcome = self._whatif_path()
        loaded = load_saved(path)
        self.assertEqual(loaded.result.score.points, 96)
        self.assertEqual(loaded.what_if.points_delta, 48)
        self.assertEqual(loaded.what_if.base_points, 48)
        self.assertEqual(loaded.what_if.overrides,
                         ["ack timeout: every level 5m"])
        self.assertEqual(loaded.what_if.base_result.score.points, 48)

    def test_load_saved_what_if_with_wrong_delta_fails(self):
        _, _, outcome = self._whatif_path()
        data = outcome.to_dict()
        data["points_delta"] = 1
        with self.assertRaises(ValueError):
            load_saved(self._write_json(data))

    def test_load_saved_what_if_missing_replay_fails(self):
        _, _, outcome = self._whatif_path()
        data = outcome.to_dict()
        del data["what_if"]
        with self.assertRaises(ValueError):
            load_saved(self._write_json(data))

    def test_compare_base_run_against_its_what_if(self):
        original = self._run("disk-full", get_policy("fast"))
        loaded = load_saved(self._write_json(
            what_if(original, ack_timeout=5).to_dict()))
        comparison = compare_results(original, loaded.result,
                                     whatif_b=loaded.what_if)
        rows = {row["metric"]: row for row in comparison.score_rows}
        self.assertEqual(rows["points"]["delta"], 48)
        self.assertIn("what-if replay", comparison.render())
        self.assertIn("+48 points vs base", comparison.render())
        data = comparison.to_dict()
        self.assertIsNone(data["run_a"]["what_if"])
        self.assertEqual(data["run_b"]["what_if"]["points_delta"], 48)
        self.assertEqual(data["run_b"]["what_if"]["base_points"], 48)

    def test_compare_two_what_if_replays(self):
        original = self._run("disk-full", get_policy("fast"))
        first = load_saved(self._write_json(
            what_if(original, ack_timeout=5).to_dict()))
        second = load_saved(self._write_json(
            what_if(original, policy=get_policy("standard")).to_dict()))
        comparison = compare_results(first.result, second.result,
                                     whatif_a=first.what_if,
                                     whatif_b=second.what_if)
        # Both replays score 96 on the same incident, so the score
        # deltas are all zero — but both sides are flagged as replays.
        rows = {row["metric"]: row for row in comparison.score_rows}
        self.assertEqual(rows["points"]["delta"], 0)
        self.assertIn("A is a what-if replay", comparison.render())
        self.assertIn("B is a what-if replay", comparison.render())

    def test_cli_compare_run_against_what_if(self):
        original = self._run("disk-full", get_policy("fast"))
        base_path = self._write_json(original.to_dict())
        whatif_path = self._write_json(
            what_if(original, ack_timeout=5).to_dict())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["compare", base_path, whatif_path]), 0)
        output = buf.getvalue()
        self.assertIn("what-if replay", output)
        self.assertIn("+48 points vs base", output)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["compare", base_path, whatif_path, "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["run_b"]["what_if"]["points_delta"], 48)

    def test_stats_flags_what_if_rows(self):
        base = self._run("disk-full")
        original = self._run("disk-full", get_policy("fast"))
        loaded = load_saved(self._write_json(
            what_if(original, ack_timeout=5).to_dict()))
        stats = aggregate_stats([base, loaded.result],
                                whatifs=[None, loaded.what_if])
        self.assertEqual(stats.count, 2)
        self.assertEqual(stats.whatif_count, 1)
        self.assertIsNone(stats.runs[0]["what_if"])
        self.assertEqual(stats.runs[1]["what_if"]["points_delta"], 48)
        self.assertEqual(stats.to_dict()["what_if_runs"], 1)
        self.assertIn("what-if runs: 1", stats.render())

    def test_stats_whatif_list_length_must_match(self):
        with self.assertRaises(ValueError):
            aggregate_stats([self._run("disk-full")], whatifs=[])

    def test_cli_stats_mixes_runs_and_what_if(self):
        run_path = self._write_json(self._run("disk-full").to_dict())
        original = self._run("disk-full", get_policy("fast"))
        whatif_path = self._write_json(
            what_if(original, ack_timeout=5).to_dict())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["stats", run_path, whatif_path]), 0)
        self.assertIn("what-if runs: 1", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["stats", run_path, whatif_path, "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["what_if_runs"], 1)
        self.assertEqual(data["runs"][1]["what_if"]["points_delta"], 48)
        self.assertIsNone(data["runs"][0]["what_if"])
class TestPurgeVerification(unittest.TestCase):
    """Per-region purge verification: confirm the fix in every region
    before the incident is declared mitigated.

    A scenario may carry ``regions``; with ``--verify-purge
    per-region`` the responder checks the fix from a cold edge node
    in each one, at 3m per region, before mitigation completes. The
    verification time is part of the mitigate delay (so the score,
    the downtime and the error-budget burn all move with it) and is
    recorded on its own (``score.verify_delay_min``), so a what-if
    replay can turn verification on or off without stacking it.
    ``cdn-purge-failure`` is the scenario it was built for: four
    regions, a purge the dashboard claims worked, and a responder
    who has to check for real.
    """

    def _write_json(self, data):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(data, handle)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_regions_on_purge_scenarios(self):
        self.assertEqual(
            get("cdn-purge-failure").regions,
            ("us-east", "us-west", "eu-west", "ap-southeast"))
        self.assertEqual(
            get("fraud-rules-misfire").regions,
            ("us-east", "us-west", "eu-west"))
        self.assertEqual(
            get("key-rotation").regions,
            ("us-east", "us-west", "eu-west"))
        self.assertEqual(get("disk-full").regions, ())

    def test_verified_run_costs_three_minutes_per_region(self):
        result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                          purge_verification="per-region")
        score = result.score
        self.assertEqual(result.purge_verification, "per-region")
        self.assertEqual(result.regions,
                         ("us-east", "us-west", "eu-west", "ap-southeast"))
        # Four regions at 3m each: 12m of verification inside the
        # mitigate delay (30m of work + 12m of checks).
        self.assertEqual(score.verify_delay_min, 12)
        self.assertEqual(score.mitigate_delay_min, 42)
        self.assertEqual(score.time_to_ack_min, 8)
        self.assertEqual(score.time_to_mitigate_min, 50)
        self.assertEqual(score.time_to_resolve_min, 65)
        # 100 - 3 (8m to ack past the 5m grace) - 17 (50m to mitigate
        # past the 15m grace, halved) = 80, scaled by the 0.4
        # late-ack credit = 32 — two points less than the unverified
        # 34, the price of knowing every region purged.
        self.assertEqual(score.points, 32)
        # Downtime 50m against a 99.9% budget of 43.2m.
        self.assertEqual(score.downtime_min, 50)
        self.assertEqual(score.error_budget_burn_pct, 115.7)

    def test_verified_timeline_records_one_event_per_region(self):
        result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                          purge_verification="per-region")
        verified = [event for event in result.timeline.events
                    if event.kind == "verified"]
        self.assertEqual(
            [(event.t_min, event.actor) for event in verified],
            [(11, "us-east"), (14, "us-west"),
             (17, "eu-west"), (20, "ap-southeast")])
        self.assertTrue(all(event.detail.startswith("fix confirmed in ")
                            for event in verified))
        # Every verification lands before the mitigated event.
        mitigated = next(event for event in result.timeline.events
                         if event.kind == "mitigated")
        self.assertTrue(all(event.t_min < mitigated.t_min
                            for event in verified))

    def test_unverified_run_records_no_verification(self):
        result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY)
        self.assertEqual(result.purge_verification, "off")
        self.assertEqual(result.score.verify_delay_min, 0)
        self.assertEqual(
            [event for event in result.timeline.events
             if event.kind == "verified"], [])
        self.assertEqual(result.score.points, 34)

    def test_per_region_needs_regions(self):
        with self.assertRaises(ValueError):
            simulate(get("disk-full"), DEFAULT_POLICY,
                     purge_verification="per-region")
        with self.assertRaises(ValueError):
            simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                     purge_verification="sometimes")

    def test_verification_is_not_jittered(self):
        # The seed varies the response, not the check: the same seed
        # replays exactly, and the verification time is 12m for
        # every seed.
        first = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                         seed=7, purge_verification="per-region")
        second = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                          seed=7, purge_verification="per-region")
        self.assertEqual(first.to_dict(), second.to_dict())
        for seed in range(1, 6):
            result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                              seed=seed, purge_verification="per-region")
            self.assertEqual(result.score.verify_delay_min, 12)

    def test_json_round_trip_keeps_verification(self):
        result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                          purge_verification="per-region")
        path = self._write_json(result.to_dict())
        self.assertEqual(load_result(path).to_dict(), result.to_dict())

    def test_old_run_without_verification_fields_loads(self):
        result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                          purge_verification="per-region")
        data = result.to_dict()
        data.pop("regions")
        data.pop("purge_verification")
        data["score"].pop("verify_delay_min")
        path = self._write_json(data)
        loaded = load_result(path)
        self.assertEqual(loaded.regions, ())
        self.assertEqual(loaded.purge_verification, "off")
        self.assertEqual(loaded.score.verify_delay_min, 0)

    def test_what_if_turns_verification_on(self):
        base = simulate(get("cdn-purge-failure"), DEFAULT_POLICY)
        outcome = what_if(base, purge_verification="per-region")
        self.assertEqual(
            outcome.overrides,
            ["purge verification: off -> per-region"])
        self.assertEqual(outcome.points_delta, -2)
        self.assertEqual(outcome.replayed.score.points, 32)
        self.assertEqual(outcome.replayed.score.verify_delay_min, 12)
        self.assertEqual(outcome.replayed.score.time_to_mitigate_min, 50)
        self.assertEqual(
            [event.kind for event in outcome.replayed.timeline.events
             if event.kind == "verified"],
            ["verified"] * 4)

    def test_what_if_turns_verification_off(self):
        base = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                        purge_verification="per-region")
        outcome = what_if(base, purge_verification="off")
        self.assertEqual(
            outcome.overrides,
            ["purge verification: per-region -> off"])
        self.assertEqual(outcome.points_delta, 2)
        self.assertEqual(outcome.replayed.score.points, 34)
        self.assertEqual(outcome.replayed.score.verify_delay_min, 0)
        self.assertEqual(outcome.replayed.score.time_to_mitigate_min, 38)

    def test_what_if_same_verification_reproduces_the_run(self):
        base = simulate(get("cdn-purge-failure"), DEFAULT_POLICY)
        outcome = what_if(base, purge_verification="off")
        self.assertEqual(outcome.overrides, [])
        self.assertEqual(outcome.points_delta, 0)
        self.assertEqual(outcome.replayed.to_dict(), base.to_dict())

    def test_what_if_verification_needs_regions(self):
        base = simulate(get("disk-full"), DEFAULT_POLICY)
        with self.assertRaises(ValueError):
            what_if(base, purge_verification="per-region")
        # A run saved before regions existed has none either.
        verified = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                            purge_verification="per-region")
        data = verified.to_dict()
        data.pop("regions")
        data.pop("purge_verification")
        data["score"].pop("verify_delay_min")
        with self.assertRaises(ValueError):
            what_if(load_result(self._write_json(data)),
                    purge_verification="per-region")

    def test_cli_run_verifies_per_region(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main([
                "run", "cdn-purge-failure",
                "--verify-purge", "per-region"]), 0)
        out = buf.getvalue()
        self.assertIn("purge verification: per-region (4 regions, +12m)",
                      out)
        self.assertIn("verify delay:     12m (4 regions)", out)
        self.assertIn("32/100", out)

    def test_cli_run_verification_needs_regions(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main([
                "run", "disk-full", "--verify-purge", "per-region"]), 2)
        self.assertIn("no regions to verify", err.getvalue())

    def test_cli_run_bad_verification_value_fails(self):
        with self.assertRaises(SystemExit):
            main(["run", "cdn-purge-failure",
                  "--verify-purge", "sometimes"])

    def test_cli_what_if_verification(self):
        base = simulate(get("cdn-purge-failure"), DEFAULT_POLICY)
        path = self._write_json(base.to_dict())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main([
                "what-if", path, "--verify-purge", "per-region"]), 0)
        out = buf.getvalue()
        self.assertIn("purge verification: off -> per-region", out)
        self.assertIn("-2 points", out)
        self.assertIn("verify delay:     0m -> 12m", out)

    def test_cli_what_if_needs_an_override(self):
        base = simulate(get("cdn-purge-failure"), DEFAULT_POLICY)
        path = self._write_json(base.to_dict())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["what-if", path]), 2)
        self.assertIn("--verify-purge", err.getvalue())

    def test_debrief_names_verified_regions(self):
        result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                          purge_verification="per-region")
        debrief = generate_debrief(result)
        self.assertIn("verified in every region", " ".join(debrief.went_well))
        self.assertIn("us-east", " ".join(debrief.went_well))
        self.assertIn("Purge verification: per-region (4 regions, +12m)",
                      debrief.render())
        self.assertEqual(debrief.to_dict()["summary"]["verify_delay_min"],
                         12)

    def test_debrief_flags_unverified_fix(self):
        result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY)
        debrief = generate_debrief(result)
        self.assertIn("never verified per region",
                      " ".join(debrief.went_badly))
        actions = [item["action"] for item in debrief.action_items]
        self.assertTrue(any("every region" in action for action in actions))
        self.assertTrue(any(item["owner"] == "on-call lead"
                            for item in debrief.action_items))

    def test_compare_diffs_verification(self):
        base = simulate(get("cdn-purge-failure"), DEFAULT_POLICY)
        verified = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                            purge_verification="per-region")
        comparison = compare_results(base, verified)
        rows = {row["metric"]: row for row in comparison.score_rows}
        self.assertEqual(rows["verify_delay_min"]["delta"], 12)
        self.assertFalse(comparison.same_purge_verification)
        self.assertIn("different purge verification",
                      comparison.render())

    def test_stats_counts_verified_runs(self):
        base = simulate(get("cdn-purge-failure"), DEFAULT_POLICY)
        verified = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                            purge_verification="per-region")
        summary = aggregate_stats([base, verified])
        self.assertEqual(summary.verified_runs, 1)
        self.assertEqual(summary.mean_verify_delay_min, 12.0)
        self.assertIn("purge verified per region: 1 run(s)",
                      summary.render())
        row = summary.to_dict()["runs"][1]
        self.assertEqual(row["purge_verification"], "per-region")
        self.assertEqual(row["verify_delay_min"], 12)

    def test_drill_with_verification_matches_run(self):
        drill = run_drill(
            get("cdn-purge-failure"), DEFAULT_POLICY,
            input_stream=io.StringIO("ack 8\nmitigate 30\nresolve 15\n"),
            prompt_stream=io.StringIO(),
            purge_verification="per-region")
        run = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                       purge_verification="per-region")
        self.assertEqual(drill.to_dict(), run.to_dict())
        self.assertEqual(drill.score.points, 32)

    def test_shift_with_verification(self):
        shift = simulate_shift(
            [get("cdn-purge-failure"), get("fraud-rules-misfire")],
            DEFAULT_POLICY, slo_percent=99.5,
            purge_verification="per-region")
        self.assertEqual(shift.purge_verification, "per-region")
        self.assertEqual(len(shift.runs), 2)
        self.assertEqual(shift.skipped, [])
        # cdn-purge-failure verified: 32 points, 50m downtime;
        # fraud-rules-misfire verified: 1 escalation, primary misses
        # at 5m, secondary acks 2m later (7m), mitigate 39m (30m of
        # work + 9m of verification), so 100 - 15 - 2 - 15 = 68.
        self.assertEqual([run.score.points for run in shift.runs],
                         [32, 68])
        self.assertEqual(shift.shift_points, 50.0)
        self.assertIn("purge verification: per-region", shift.render())
        path = self._write_json(shift.to_dict())
        self.assertEqual(load_shift(path).to_dict(), shift.to_dict())

    def test_shift_verification_needs_regions_for_every_incident(self):
        with self.assertRaises(ValueError):
            simulate_shift(
                [get("cdn-purge-failure"), get("disk-full")],
                DEFAULT_POLICY, slo_percent=99.5,
                purge_verification="per-region")

    def test_batch_with_verification(self):
        batch = simulate_batch(get("cdn-purge-failure"), DEFAULT_POLICY,
                               [1, 2], purge_verification="per-region")
        self.assertEqual(batch.purge_verification, "per-region")
        self.assertTrue(all(run.score.verify_delay_min == 12
                            for run in batch.runs))
        self.assertEqual(batch.to_dict()["purge_verification"],
                         "per-region")

    def test_template_round_trips_regions(self):
        data = scenario_to_dict(get("cdn-purge-failure"))
        self.assertEqual(data["regions"],
                         ["us-east", "us-west", "eu-west", "ap-southeast"])
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"cdn-purge-failure": data}}, handle)
        self.addCleanup(os.unlink, handle.name)
        loaded = load_scenario_file(handle.name)
        self.assertEqual(loaded["cdn-purge-failure"],
                         get("cdn-purge-failure"))

    def test_scenario_file_rejects_bad_regions(self):
        for regions in (["us-east", "us-east"], ["us-east", 7], [""]):
            handle = tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8")
            with handle:
                json.dump({"scenarios": {"bad": {
                    "service": "svc", "title": "t", "severity": "low",
                    "description": "", "ack_delay_min": [2],
                    "mitigate_delay_min": 5, "resolve_delay_min": 5,
                    "regions": regions}}}, handle)
            self.addCleanup(os.unlink, handle.name)
            with self.assertRaises(ValueError):
                load_scenario_file(handle.name)

    def test_bundle_manifest_carries_verification(self):
        result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                          purge_verification="per-region")
        path = self._write_json(result.to_dict())
        out = path + "-bundle.zip"
        self.addCleanup(lambda: os.path.exists(out) and os.unlink(out))
        manifest = create_bundle(path, out)
        self.assertEqual(manifest["purge_verification"], "per-region")
        self.assertEqual(manifest["verify_delay_min"], 12)
        self.assertEqual(validate_bundle(out), manifest)

    def test_html_postmortem_shows_verification(self):
        result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                          purge_verification="per-region")
        text = render_postmortem_html(result)
        self.assertIn("Purge verification", text)
        self.assertIn("per-region (4 regions, +12m)", text)
        self.assertIn("verified in every region", text)

    def test_pdf_postmortem_shows_verification(self):
        result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                          purge_verification="per-region")
        data = render_postmortem_pdf(result)
        self.assertIn(b"Purge verification: per-region", data)

    def test_fraud_rules_regions_cost_differently(self):
        # eu-west takes 5m to confirm (the test card must clear
        # 3-D Secure with a European issuer); the US regions take
        # 2m each. The total is 9m, the same as the 3m default, so
        # the score is unchanged — only the checks land at
        # different times.
        scenario = get("fraud-rules-misfire")
        self.assertEqual(scenario.verify_delay_min_by_region,
                         {"us-east": 2, "us-west": 2, "eu-west": 5})
        self.assertEqual(scenario.verify_delay_min_for("us-east"), 2)
        self.assertEqual(scenario.verify_delay_min_for("eu-west"), 5)
        self.assertEqual(scenario.verify_delay_min_total(), 9)
        result = simulate(scenario, DEFAULT_POLICY,
                          purge_verification="per-region")
        self.assertEqual(result.score.verify_delay_min, 9)
        self.assertEqual(result.verify_delay_min_by_region,
                         {"us-east": 2, "us-west": 2, "eu-west": 5})
        # Primary misses at 5m, secondary acks 2m later (7m), so
        # the checks land at 9m, 11m and 16m; mitigation at 46m.
        verified = [event for event in result.timeline.events
                    if event.kind == "verified"]
        self.assertEqual(
            [(event.t_min, event.actor) for event in verified],
            [(9, "us-east"), (11, "us-west"), (16, "eu-west")])
        self.assertEqual(
            [event.detail for event in verified],
            ["fix confirmed in us-east (2m)",
             "fix confirmed in us-west (2m)",
             "fix confirmed in eu-west (5m)"])
        self.assertEqual(result.score.time_to_mitigate_min, 46)
        self.assertEqual(result.score.points, 68)

    def test_default_cost_is_three_minutes_per_region(self):
        # A scenario that gives no per-region costs verifies at
        # the 3m default everywhere, exactly as before.
        scenario = get("cdn-purge-failure")
        self.assertEqual(scenario.verify_delay_min_by_region, {})
        self.assertEqual(scenario.verify_delay_min_for("us-east"), 3)
        self.assertEqual(scenario.verify_delay_min_total(), 12)

    def _write_scenario_file(self, scenario):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"slow-edge": scenario}}, handle)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_scenario_file_per_region_costs(self):
        raw = {
            "service": "edge-cdn", "title": "Slow edge purge",
            "severity": "high", "description": "",
            "ack_delay_min": [2],
            "mitigate_delay_min": 15, "resolve_delay_min": 10,
            "regions": ["us-east", "eu-west"],
            "verify_delay_min_by_region": {"us-east": 2, "eu-west": 5},
        }
        loaded = load_scenario_file(self._write_scenario_file(raw))
        scenario = loaded["slow-edge"]
        self.assertEqual(scenario.verify_delay_min_for("us-east"), 2)
        self.assertEqual(scenario.verify_delay_min_for("eu-west"), 5)
        # The template round-trips the costs, so a saved scenario
        # file keeps them.
        self.assertEqual(scenario_to_dict(scenario)
                         ["verify_delay_min_by_region"],
                         {"us-east": 2, "eu-west": 5})
        self.assertEqual(load_scenario_file(
            self._write_scenario_file(scenario_to_dict(scenario))),
            loaded)
        # 2m + 5m of verification, not the 6m the default would
        # charge: mitigation 24m, so the score moves with it.
        result = simulate(scenario, DEFAULT_POLICY,
                          purge_verification="per-region")
        self.assertEqual(result.score.verify_delay_min, 7)
        self.assertEqual(result.score.mitigate_delay_min, 22)
        self.assertEqual(result.score.time_to_mitigate_min, 24)
        self.assertEqual(result.score.points, 96)

    def test_scenario_file_region_without_a_cost_uses_the_default(self):
        raw = {
            "service": "edge-cdn", "title": "Slow edge purge",
            "severity": "high", "description": "",
            "ack_delay_min": [2],
            "mitigate_delay_min": 15, "resolve_delay_min": 10,
            "regions": ["us-east", "eu-west"],
            "verify_delay_min_by_region": {"eu-west": 5},
        }
        scenario = load_scenario_file(
            self._write_scenario_file(raw))["slow-edge"]
        self.assertEqual(scenario.verify_delay_min_for("us-east"), 3)
        self.assertEqual(scenario.verify_delay_min_total(), 8)

    def test_scenario_file_rejects_bad_verify_costs(self):
        base = {
            "service": "edge-cdn", "title": "Slow edge purge",
            "severity": "high", "description": "",
            "ack_delay_min": [2],
            "mitigate_delay_min": 15, "resolve_delay_min": 10,
            "regions": ["us-east", "eu-west"],
        }
        bad_costs = [
            # A cost for a region the scenario never verifies.
            {"ap-south": 2},
            # Not a positive integer number of minutes.
            {"us-east": 0},
            {"us-east": -1},
            {"us-east": "5"},
            {"us-east": True},
            {"us-east": 2.5},
            # Not an object at all.
            ["us-east"],
            "us-east",
        ]
        for costs in bad_costs:
            raw = dict(base)
            raw["verify_delay_min_by_region"] = costs
            with self.assertRaises(ValueError):
                load_scenario_file(self._write_scenario_file(raw))

    def test_what_if_replays_per_region_costs(self):
        base = simulate(get("fraud-rules-misfire"), DEFAULT_POLICY)
        outcome = what_if(base, purge_verification="per-region")
        self.assertEqual(outcome.replayed.score.verify_delay_min, 9)
        self.assertEqual(outcome.replayed.score.points, 68)
        self.assertEqual(
            [(event.t_min, event.actor)
             for event in outcome.replayed.timeline.events
             if event.kind == "verified"],
            [(9, "us-east"), (11, "us-west"), (16, "eu-west")])
        # The same mode replays the run exactly, costs included.
        verified = simulate(get("fraud-rules-misfire"), DEFAULT_POLICY,
                            purge_verification="per-region")
        same = what_if(verified, purge_verification="per-region")
        self.assertEqual(same.overrides, [])
        self.assertEqual(same.replayed.to_dict(), verified.to_dict())

    def test_old_run_without_per_region_costs_replays_at_default(self):
        verified = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                            purge_verification="per-region")
        data = verified.to_dict()
        data.pop("verify_delay_min_by_region")
        loaded = load_result(self._write_json(data))
        self.assertEqual(loaded.verify_delay_min_by_region, {})
        # A run saved before 0.44.0 replays at the 3m default, which
        # is what 0.43.0 charged, so the replay still matches.
        outcome = what_if(loaded, purge_verification="per-region")
        self.assertEqual(outcome.replayed.score.verify_delay_min, 12)
        self.assertEqual(outcome.replayed.to_dict(), verified.to_dict())

    def test_json_round_trip_keeps_per_region_costs(self):
        result = simulate(get("fraud-rules-misfire"), DEFAULT_POLICY,
                          purge_verification="per-region")
        path = self._write_json(result.to_dict())
        self.assertEqual(load_result(path).to_dict(), result.to_dict())

    def test_drill_with_per_region_costs_matches_run(self):
        drill = run_drill(
            get("fraud-rules-misfire"), DEFAULT_POLICY,
            input_stream=io.StringIO(
                "wait\nack 2\nmitigate 30\nresolve 15\n"),
            prompt_stream=io.StringIO(),
            purge_verification="per-region")
        run = simulate(get("fraud-rules-misfire"), DEFAULT_POLICY,
                       purge_verification="per-region")
        self.assertEqual(drill.to_dict(), run.to_dict())
        self.assertEqual(drill.score.points, 68)

    def test_cli_run_shows_per_region_breakdown(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main([
                "run", "fraud-rules-misfire",
                "--verify-purge", "per-region"]), 0)
        out = buf.getvalue()
        self.assertIn(
            "verify delay:     9m (us-east 2m, us-west 2m, eu-west 5m)",
            out)
        self.assertIn("68/100", out)

    def test_debrief_names_per_region_costs(self):
        result = simulate(get("fraud-rules-misfire"), DEFAULT_POLICY,
                          purge_verification="per-region")
        debrief = generate_debrief(result)
        self.assertIn(
            "Purge verification: per-region "
            "(us-east 2m, us-west 2m, eu-west 5m, +9m)",
            debrief.render())
        self.assertEqual(
            debrief.to_dict()["summary"]["verify_delay_min_by_region"],
            {"us-east": 2, "us-west": 2, "eu-west": 5})


class TestDrill(unittest.TestCase):
    def _drill(self, scenario, policy=DEFAULT_POLICY, decisions="",
               **kwargs):
        return run_drill(
            scenario, policy,
            input_stream=io.StringIO(decisions),
            prompt_stream=io.StringIO(),
            **kwargs)

    def _write_decisions(self, decisions):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8")
        with handle:
            handle.write(decisions)
        return handle.name

    def _write_json(self, data):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(data, handle)
        return handle.name

    def test_scripted_drill_matches_run_for_same_decisions(self):
        # disk-full on run: ack 3m, mitigate 23m, resolve 35m, 96
        # points. A drill making the same decisions scores the same.
        drill = self._drill(get("disk-full"), decisions=(
            "ack 3\nmitigate 20\nresolve 12\n"))
        run = simulate(get("disk-full"), DEFAULT_POLICY)
        self.assertEqual(drill.to_dict(), run.to_dict())
        self.assertEqual(drill.score.points, 96)

    def test_drill_with_escalation_matches_run(self):
        # latency-spike: primary misses, secondary acks 4m after the
        # 5m timeout, mitigates 35m later, resolves 20m after that.
        drill = self._drill(get("latency-spike"), decisions=(
            "wait\nack 4\nmitigate 35\nresolve 20\n"))
        run = simulate(get("latency-spike"), DEFAULT_POLICY)
        self.assertEqual(drill.to_dict(), run.to_dict())
        self.assertEqual(drill.score.escalations, 1)
        self.assertEqual(drill.score.time_to_ack_min, 9)

    def test_drill_late_ack_matches_run(self):
        scenario = Scenario(
            name="slow", service="svc", title="slow", severity="low",
            description="", ack_delay_min=(7, None, None),
            mitigate_delay_min=20, resolve_delay_min=12,
        )
        drill = self._drill(scenario, decisions=(
            "ack 7\nmitigate 20\nresolve 12\n"))
        run = simulate(scenario, DEFAULT_POLICY)
        self.assertEqual(drill.to_dict(), run.to_dict())
        self.assertEqual(drill.score.ack_late_min, 2)
        self.assertEqual(drill.score.ack_credit, 0.6)
        self.assertEqual(drill.score.points, 55)

    def test_drill_on_hard_matches_run(self):
        # Hard scales the timeouts (5m -> 3m) and tightens the graces
        # (ack 3m, mitigate 10m): ack 3m is on time, mitigation at 23m
        # is 13m past the grace, so 100 - 13 // 2 = 94.
        policy = apply_difficulty(DEFAULT_POLICY, HARD)
        drill = self._drill(get("disk-full"), policy, difficulty=HARD,
                            decisions="ack 3\nmitigate 20\nresolve 12\n")
        run = simulate(get("disk-full"), policy, difficulty=HARD)
        self.assertEqual(drill.to_dict(), run.to_dict())
        self.assertEqual(drill.score.points, 94)

    def test_drill_hints_cost_points_like_run(self):
        # disk-full with one hint: 96 - 5 = 91, hint recorded.
        drill = self._drill(get("disk-full"), hints=1, decisions=(
            "ack 3\nmitigate 20\nresolve 12\n"))
        run = simulate(get("disk-full"), DEFAULT_POLICY, hints=1)
        self.assertEqual(drill.to_dict(), run.to_dict())
        self.assertEqual(drill.score.points, 91)
        self.assertEqual(drill.score.hints_used, 1)

    def test_drill_hint_mid_drill_costs_points_like_run(self):
        # A hint asked for at the ack prompt costs the same 5 points
        # as an up-front hint, and the drill matches a run that asked
        # for the same hint: 96 - 5 = 91.
        prompts = io.StringIO()
        drill = run_drill(
            get("disk-full"), DEFAULT_POLICY,
            input_stream=io.StringIO(
                "hint\nack 3\nmitigate 20\nresolve 12\n"),
            prompt_stream=prompts)
        run = simulate(get("disk-full"), DEFAULT_POLICY, hints=1)
        self.assertEqual(drill.to_dict(), run.to_dict())
        self.assertEqual(drill.score.points, 91)
        self.assertEqual(drill.score.hints_used, 1)
        self.assertEqual(drill.score.hint_cost_points, 5)
        self.assertEqual(drill.hints[0].text,
                         get("disk-full").hints[0])
        self.assertIn("Check WAL archiving", prompts.getvalue())
        self.assertIn("next milestone: ack", prompts.getvalue())

    def test_drill_hint_at_each_stage_draws_in_order(self):
        # One hint per prompt, drawn in order: 96 - 3 x 5 = 81.
        prompts = io.StringIO()
        drill = run_drill(
            get("disk-full"), DEFAULT_POLICY,
            input_stream=io.StringIO(
                "hint\nack 3\nhint\nmitigate 20\nhint\nresolve 12\n"),
            prompt_stream=prompts)
        self.assertEqual(drill.score.points, 81)
        self.assertEqual(drill.score.hints_used, 3)
        self.assertEqual(drill.score.hint_cost_points, 15)
        self.assertEqual(
            [hint.text for hint in drill.hints],
            list(get("disk-full").hints))
        self.assertIn("next milestone: ack", prompts.getvalue())
        self.assertIn("next milestone: mitigate", prompts.getvalue())
        self.assertIn("next milestone: resolve", prompts.getvalue())

    def test_drill_hint_continues_after_up_front_hints(self):
        # One hint up front, one mid-drill: the mid-drill hint is the
        # second scenario hint, not the first again.
        drill = self._drill(get("disk-full"), hints=1, decisions=(
            "hint\nack 3\nmitigate 20\nresolve 12\n"))
        self.assertEqual(drill.score.points, 86)
        self.assertEqual(drill.score.hints_used, 2)
        self.assertEqual(
            [hint.text for hint in drill.hints],
            list(get("disk-full").hints[:2]))

    def test_drill_hint_when_none_left_costs_nothing_more(self):
        # disk-full has 3 hints; the fourth `hint` says so, costs
        # nothing, and re-prompts instead of answering the decision.
        prompts = io.StringIO()
        drill = run_drill(
            get("disk-full"), DEFAULT_POLICY,
            input_stream=io.StringIO(
                "hint\nhint\nhint\nhint\n"
                "ack 3\nmitigate 20\nresolve 12\n"),
            prompt_stream=prompts)
        self.assertEqual(drill.score.points, 81)
        self.assertEqual(drill.score.hints_used, 3)
        self.assertEqual(drill.score.hint_cost_points, 15)
        self.assertIn("no hints left", prompts.getvalue())

    def test_drill_hint_in_partial_run_is_recorded(self):
        # A hint asked for before quitting is still recorded, and
        # still costs its 5 points: acked at 3m, inside the grace,
        # so the partial run keeps 100 - 5 = 95.
        drill = self._drill(get("disk-full"),
                            decisions="hint\nack 3\nquit\n")
        self.assertFalse(drill.score.resolved)
        self.assertEqual(drill.score.hints_used, 1)
        self.assertEqual(drill.score.hint_cost_points, 5)
        self.assertEqual(drill.score.points, 95)
        self.assertIn("abandoned", drill.timeline.kinds())

    def test_drill_hint_without_hints_is_unknown_answer(self):
        # A scenario with no hints offers no `hint` command: typing
        # it is an unknown answer and re-prompts, like any other.
        scenario = Scenario(
            name="plain", service="svc", title="plain", severity="low",
            description="", ack_delay_min=(3, None, None),
            mitigate_delay_min=20, resolve_delay_min=12,
        )
        prompts = io.StringIO()
        drill = run_drill(
            scenario, DEFAULT_POLICY,
            input_stream=io.StringIO(
                "hint\nack 3\nmitigate 20\nresolve 12\n"),
            prompt_stream=prompts)
        self.assertTrue(drill.score.resolved)
        self.assertEqual(drill.score.hints_used, 0)
        self.assertEqual(drill.score.points, 96)
        self.assertIn("unknown answer 'hint'", prompts.getvalue())

    def test_drill_hint_round_trips_through_replay(self):
        # A mid-drill hint is recorded in the run JSON like an
        # up-front hint, so replay/compare/stats see it.
        drill = self._drill(get("disk-full"), decisions=(
            "hint\nack 3\nmitigate 20\nresolve 12\n"))
        loaded = load_result(self._write_json(drill.to_dict()))
        self.assertEqual(loaded.to_dict(), drill.to_dict())
        self.assertEqual(loaded.score.hints_used, 1)
        self.assertEqual(loaded.score.points, 91)

    def test_drill_too_many_hints_fails(self):
        with self.assertRaises(ValueError):
            self._drill(get("disk-full"), hints=99, decisions="")

    def test_drill_wait_on_every_level_is_unacked(self):
        drill = self._drill(get("disk-full"),
                            decisions="wait\nwait\nwait\n")
        self.assertFalse(drill.score.resolved)
        self.assertEqual(drill.score.points, 0)
        self.assertEqual(drill.score.escalations, 2)
        self.assertIsNone(drill.score.time_to_ack_min)
        self.assertIn("unacked", drill.timeline.kinds())
        self.assertNotIn("abandoned", drill.timeline.kinds())

    def test_drill_quit_after_ack_records_partial_run(self):
        drill = self._drill(get("disk-full"),
                            decisions="ack 3\nquit\n")
        self.assertFalse(drill.score.resolved)
        self.assertEqual(drill.score.time_to_ack_min, 3)
        self.assertIsNone(drill.score.time_to_mitigate_min)
        self.assertIsNone(drill.score.time_to_resolve_min)
        self.assertIn("abandoned", drill.timeline.kinds())
        abandoned = [event for event in drill.timeline.events
                     if event.kind == "abandoned"][0]
        self.assertEqual(abandoned.t_min, 3)
        self.assertIn("ended by operator", abandoned.detail)
        # Scored on the milestones reached: acked at 3m, inside the
        # 5m grace, no escalations, so the partial run keeps 100.
        self.assertEqual(drill.score.points, 100)

    def test_drill_quit_after_mitigate_records_partial_run(self):
        drill = self._drill(get("disk-full"),
                            decisions="ack 3\nmitigate 20\nquit\n")
        self.assertFalse(drill.score.resolved)
        self.assertEqual(drill.score.time_to_ack_min, 3)
        self.assertEqual(drill.score.time_to_mitigate_min, 23)
        self.assertIsNone(drill.score.time_to_resolve_min)
        self.assertEqual(drill.score.mitigate_delay_min, 20)
        self.assertIsNone(drill.score.resolve_delay_min)
        self.assertIn("abandoned", drill.timeline.kinds())

    def test_drill_eof_records_partial_run(self):
        # Input ends mid-drill: same as quit, with the input ending
        # named as the reason.
        drill = self._drill(get("disk-full"), decisions="ack 3\n")
        self.assertFalse(drill.score.resolved)
        self.assertEqual(drill.score.time_to_ack_min, 3)
        self.assertIn("abandoned", drill.timeline.kinds())
        abandoned = [event for event in drill.timeline.events
                     if event.kind == "abandoned"][0]
        self.assertIn("input ended", abandoned.detail)

    def test_drill_eof_before_any_decision_is_unacked(self):
        drill = self._drill(get("disk-full"), decisions="")
        self.assertFalse(drill.score.resolved)
        self.assertEqual(drill.score.points, 0)
        self.assertIsNone(drill.score.time_to_ack_min)
        self.assertIn("abandoned", drill.timeline.kinds())
        self.assertNotIn("unacked", drill.timeline.kinds())

    def test_drill_invalid_answer_is_reprompted(self):
        prompts = io.StringIO()
        drill = run_drill(
            get("disk-full"), DEFAULT_POLICY,
            input_stream=io.StringIO(
                "bogus\nack notanumber\nack -1\n"
                "ack 3\nmitigate 20\nresolve 12\n"),
            prompt_stream=prompts)
        self.assertEqual(drill.score.points, 96)
        self.assertTrue(drill.score.resolved)
        self.assertIn("unknown answer 'bogus'", prompts.getvalue())
        self.assertIn("takes a number of minutes", prompts.getvalue())
        self.assertIn("non-negative", prompts.getvalue())

    def test_drill_bare_commands_mean_now(self):
        drill = self._drill(get("cert-expiry"),
                            decisions="ack\nmitigate\nresolve\n")
        self.assertEqual(drill.score.time_to_ack_min, 0)
        self.assertEqual(drill.score.time_to_mitigate_min, 0)
        self.assertEqual(drill.score.time_to_resolve_min, 0)
        self.assertEqual(drill.score.points, 100)

    def test_drill_result_round_trips_through_replay(self):
        drill = self._drill(get("disk-full"), decisions=(
            "ack 3\nmitigate 20\nresolve 12\n"))
        loaded = load_result(self._write_json(drill.to_dict()))
        self.assertEqual(loaded.to_dict(), drill.to_dict())

    def test_drill_run_works_with_compare_stats_and_debrief(self):
        drill = self._drill(get("disk-full"), decisions=(
            "ack 3\nmitigate 20\nresolve 12\n"))
        run = simulate(get("disk-full"), DEFAULT_POLICY)
        drill_path = self._write_json(drill.to_dict())
        run_path = self._write_json(run.to_dict())
        loaded = load_result(drill_path)
        comparison = compare_results(loaded, run)
        self.assertTrue(comparison.timelines_identical)
        self.assertEqual(
            comparison.score_rows[0]["delta"], 0)
        summary = aggregate_stats(
            [loaded, run], labels=[drill_path, run_path])
        self.assertEqual(summary.count, 2)
        self.assertEqual(summary.points_mean, 96)
        self.assertEqual(summary.resolved, 2)
        debrief = generate_debrief(loaded)
        self.assertEqual(debrief.points, 96)
        self.assertIn("Incident resolved in 35m", debrief.went_well)

    def test_debrief_of_partial_drill_run(self):
        drill = self._drill(get("disk-full"),
                            decisions="ack 3\nquit\n")
        debrief = generate_debrief(load_result(
            self._write_json(drill.to_dict())))
        self.assertEqual(debrief.time_to_ack_min, 3)
        self.assertFalse(debrief.resolved)
        self.assertIn("never mitigated",
                      " ".join(debrief.went_badly))
        self.assertNotIn("Nobody acked", debrief.render())
        self.assertIn("Re-run the drill",
                      " ".join(item["action"]
                               for item in debrief.action_items))

    def test_debrief_of_partial_drill_run_after_mitigate(self):
        drill = self._drill(get("disk-full"),
                            decisions="ack 3\nmitigate 20\nquit\n")
        debrief = generate_debrief(drill)
        self.assertEqual(debrief.time_to_mitigate_min, 23)
        self.assertFalse(debrief.resolved)
        self.assertIn("mitigated but never resolved",
                      " ".join(debrief.went_badly))

    def test_cli_drill_scripted_json(self):
        path = self._write_decisions("ack 3\nmitigate 20\nresolve 12\n")
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(
                main(["drill", "disk-full", "--input", path, "--json"]), 0)
        # Prompts go to stderr, so stdout is clean JSON.
        data = json.loads(out.getvalue())
        self.assertEqual(data["score"]["points"], 96)
        self.assertEqual(data["score"]["time_to_ack_min"], 3)
        self.assertIn("paged ana", err.getvalue())

    def test_cli_drill_scripted_human_output(self):
        path = self._write_decisions("wait\nack 4\nmitigate 35\nresolve 20\n")
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["drill", "latency-spike", "--input", path]), 0)
        self.assertIn("acked", out.getvalue())
        self.assertIn("escalations:      1", out.getvalue())

    def test_cli_drill_reads_piped_stdin(self):
        from unittest import mock
        out = io.StringIO()
        with mock.patch.object(
                sys, "stdin",
                io.StringIO("ack 3\nmitigate 20\nresolve 12\n")), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["drill", "disk-full"]), 0)
        self.assertIn("96/100", out.getvalue())

    def test_cli_drill_out_writes_file(self):
        decisions = self._write_decisions("ack 3\nmitigate 20\nresolve 12\n")
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        handle.close()
        with redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["drill", "disk-full", "--input", decisions,
                      "--out", handle.name]), 0)
        with open(handle.name, encoding="utf-8") as saved:
            data = json.load(saved)
        self.assertEqual(data["score"]["points"], 96)

    def test_cli_drill_unknown_scenario_fails(self):
        self.assertEqual(main(["drill", "no-such-scenario"]), 2)

    def test_cli_drill_missing_input_file_fails(self):
        self.assertEqual(
            main(["drill", "disk-full", "--input", "/no-such-file.txt"]), 2)

    def test_cli_drill_unwritable_out_fails(self):
        decisions = self._write_decisions("ack 3\nmitigate 20\nresolve 12\n")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["drill", "disk-full", "--input", decisions,
                      "--out", "/no-such-dir/out.json"]), 2)

    def test_cli_drill_too_many_hints_fails(self):
        self.assertEqual(
            main(["drill", "disk-full", "--hints", "99"]), 2)

    def test_cli_drill_difficulty_changes_scoring(self):
        path = self._write_decisions("ack 3\nmitigate 20\nresolve 12\n")
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["drill", "disk-full", "--input", path,
                      "--difficulty", "hard", "--json"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["difficulty"], "hard")
        self.assertEqual(data["score"]["points"], 94)

    def test_cli_drill_hint_records_hint_in_json(self):
        # A mid-drill hint is recorded in the drill --json output
        # like an up-front hint, and the prompt text (with the hint
        # itself) stays on stderr, so stdout is clean JSON.
        path = self._write_decisions(
            "hint\nack 3\nmitigate 20\nresolve 12\n")
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(
                main(["drill", "disk-full", "--input", path, "--json"]),
                0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["score"]["points"], 91)
        self.assertEqual(data["score"]["hints_used"], 1)
        self.assertEqual(data["score"]["hint_cost_points"], 5)
        self.assertEqual(len(data["hints"]), 1)
        self.assertIn("Check WAL archiving", data["hints"][0]["text"])
        self.assertIn("Check WAL archiving", err.getvalue())


class TestHistory(unittest.TestCase):
    def _write_decisions(self, decisions):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8")
        with handle:
            handle.write(decisions)
        return handle.name

    def _drill_result(self, decisions, scenario="disk-full", **kwargs):
        stream = io.StringIO(decisions)
        return run_drill(get(scenario), DEFAULT_POLICY,
                         input_stream=stream,
                         prompt_stream=io.StringIO(), **kwargs)

    def _write_history(self, data):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        handle.write(json.dumps(data))
        handle.close()
        return handle.name

    def test_entry_from_finished_drill(self):
        result = self._drill_result("ack 3\nmitigate 20\nresolve 12\n")
        entry = entry_from_result(result, "ana",
                                  recorded_at="2026-11-23T12:00:00+00:00")
        self.assertEqual(entry.operator, "ana")
        self.assertEqual(entry.scenario, "disk-full")
        self.assertEqual(entry.difficulty, "normal")
        self.assertEqual(entry.policy, "standard")
        self.assertEqual(entry.points, 96)
        self.assertEqual(entry.grade, "A")
        self.assertEqual(entry.time_to_ack_min, 3)
        self.assertEqual(entry.time_to_mitigate_min, 23)
        self.assertEqual(entry.time_to_resolve_min, 35)
        self.assertEqual(entry.escalations, 0)
        self.assertTrue(entry.resolved)
        self.assertFalse(entry.abandoned)
        self.assertEqual(entry.hints_used, 0)

    def test_entry_from_abandoned_drill_is_flagged(self):
        result = self._drill_result("ack 3\nquit\n")
        entry = entry_from_result(result, "ana")
        self.assertTrue(entry.abandoned)
        self.assertFalse(entry.resolved)
        self.assertEqual(entry.time_to_ack_min, 3)
        self.assertIsNone(entry.time_to_mitigate_min)

    def test_entry_from_unacked_drill_scores_zero(self):
        result = self._drill_result("wait\nwait\nwait\n")
        entry = entry_from_result(result, "ana")
        self.assertEqual(entry.points, 0)
        self.assertEqual(entry.grade, "D")
        self.assertIsNone(entry.time_to_ack_min)
        self.assertFalse(entry.abandoned)

    def test_record_and_load_round_trips(self):
        path = os.path.join(tempfile.mkdtemp(), "history.json")
        entry = entry_from_result(
            self._drill_result("ack 3\nmitigate 20\nresolve 12\n"), "ana",
            recorded_at="2026-11-23T12:00:00+00:00")
        record_run(path, entry)
        record_run(path, entry_from_result(
            self._drill_result("wait\nack 4\nmitigate 35\nresolve 20\n",
                               scenario="latency-spike"), "ben",
            recorded_at="2026-11-23T13:00:00+00:00"))
        entries = load_history(path)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], entry)
        self.assertEqual(entries[1].operator, "ben")
        self.assertEqual(entries[1].scenario, "latency-spike")

    def test_load_missing_history_is_empty(self):
        self.assertEqual(
            load_history(os.path.join(tempfile.mkdtemp(), "nope.json")),
            [])

    def test_load_bad_json_fails(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        handle.write("{not json")
        handle.close()
        with self.assertRaises(json.JSONDecodeError):
            load_history(handle.name)

    def test_load_wrong_version_fails(self):
        path = self._write_history({"version": 99, "runs": []})
        with self.assertRaises(ValueError):
            load_history(path)

    def test_load_invalid_entry_fails(self):
        entry = entry_from_result(
            self._drill_result("ack 3\nmitigate 20\nresolve 12\n"), "ana")
        data = {"version": 1, "runs": [entry.to_dict()]}
        data["runs"][0]["points"] = "ninety-six"
        with self.assertRaises(ValueError):
            load_history(self._write_history(data))

    def test_summarize_operator_tracks_improvement(self):
        # Scores 50, 70, 90: first half mean 50, second half mean 80.
        entries = []
        for index, points in enumerate((50, 70, 90)):
            base = entry_from_result(
                self._drill_result("ack 3\nmitigate 20\nresolve 12\n"),
                "ana", recorded_at=f"2026-11-2{index}T12:00:00+00:00")
            entries.append(replace(base, points=points,
                                   grade=grade(points)))
        summary = summarize_operator(entries, "ana")
        self.assertEqual(summary.runs, 3)
        self.assertEqual(summary.points_mean, 70)
        self.assertEqual(summary.points_best, 90)
        self.assertEqual(summary.points_first, 50)
        self.assertEqual(summary.points_latest, 90)
        self.assertEqual(summary.points_delta, 40)
        self.assertEqual(summary.trend, "improving")
        self.assertEqual(summary.mean_time_to_ack_min, 3)
        self.assertEqual(summary.resolved, 3)
        self.assertEqual(summary.abandoned, 0)
        rendered = summary.render(entries)
        self.assertIn("ana (3 drill(s))", rendered)
        self.assertIn("first -> latest: 50 -> 90 (+40 pts)", rendered)
        self.assertIn("trend: improving", rendered)

    def test_summarize_operator_declining_and_steady(self):
        def _entries(points_list):
            entries = []
            for index, points in enumerate(points_list):
                base = entry_from_result(
                    self._drill_result("ack 3\nmitigate 20\nresolve 12\n"),
                    "ana")
                entries.append(replace(base, points=points,
                                       grade=grade(points)))
            return entries
        self.assertEqual(
            summarize_operator(_entries((90, 70, 50)), "ana").trend,
            "declining")
        self.assertEqual(
            summarize_operator(_entries((80, 80, 80)), "ana").trend,
            "steady")

    def test_summarize_single_run_has_no_trend(self):
        entry = entry_from_result(
            self._drill_result("ack 3\nmitigate 20\nresolve 12\n"), "ana")
        summary = summarize_operator([entry], "ana")
        self.assertEqual(summary.trend, "not enough runs yet")
        self.assertEqual(summary.points_delta, 0)

    def test_summarize_unknown_operator_fails(self):
        entry = entry_from_result(
            self._drill_result("ack 3\nmitigate 20\nresolve 12\n"), "ana")
        with self.assertRaises(ValueError):
            summarize_operator([entry], "ben")

    def test_summarize_all_lists_operators_in_first_run_order(self):
        entries = [
            entry_from_result(
                self._drill_result("ack 3\nmitigate 20\nresolve 12\n"),
                "ana"),
            entry_from_result(
                self._drill_result("ack 3\nmitigate 20\nresolve 12\n"),
                "ben"),
            entry_from_result(
                self._drill_result("ack 3\nmitigate 20\nresolve 12\n"),
                "ana"),
        ]
        summaries = summarize_all(entries)
        self.assertEqual([s.operator for s in summaries], ["ana", "ben"])
        self.assertEqual(summaries[0].runs, 2)
        self.assertEqual(summaries[1].runs, 1)
        overview = render_all_operators(summaries)
        self.assertIn("ana", overview)
        self.assertIn("2 drill(s)", overview)
        self.assertIn("ben", overview)

    def test_cli_drill_with_operator_records_history(self):
        decisions = self._write_decisions(
            "ack 3\nmitigate 20\nresolve 12\n")
        history_path = os.path.join(tempfile.mkdtemp(), "history.json")
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(
                main(["drill", "disk-full", "--input", decisions,
                      "--operator", "ana", "--history-file", history_path,
                      "--json"]), 0)
        # Stdout stays clean JSON; the recording note goes to stderr.
        self.assertEqual(json.loads(out.getvalue())["score"]["points"], 96)
        self.assertIn("recorded drill for operator 'ana'", err.getvalue())
        entries = load_history(history_path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].operator, "ana")
        self.assertEqual(entries[0].points, 96)

    def test_cli_drill_without_operator_records_nothing(self):
        from unittest import mock
        decisions = self._write_decisions(
            "ack 3\nmitigate 20\nresolve 12\n")
        history_path = os.path.join(tempfile.mkdtemp(), "history.json")
        with mock.patch("incident_lab.cli.DEFAULT_HISTORY_PATH",
                        history_path), \
                redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["drill", "disk-full", "--input", decisions]), 0)
        self.assertFalse(os.path.exists(history_path))

    def test_cli_drill_history_file_without_operator_fails(self):
        decisions = self._write_decisions(
            "ack 3\nmitigate 20\nresolve 12\n")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["drill", "disk-full", "--input", decisions,
                      "--history-file", "/tmp/history.json"]), 2)

    def test_cli_history_operator_view(self):
        decisions = self._write_decisions(
            "ack 3\nmitigate 20\nresolve 12\n")
        history_path = os.path.join(tempfile.mkdtemp(), "history.json")
        for _ in range(2):
            with redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(
                    main(["drill", "disk-full", "--input", decisions,
                          "--operator", "ana",
                          "--history-file", history_path]), 0)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["history", "--operator", "ana",
                      "--history-file", history_path]), 0)
        self.assertIn("ana (2 drill(s))", out.getvalue())
        self.assertIn("96/100", out.getvalue())
        self.assertIn("trend: steady", out.getvalue())

    def test_cli_history_overview_and_json(self):
        decisions = self._write_decisions(
            "ack 3\nmitigate 20\nresolve 12\n")
        history_path = os.path.join(tempfile.mkdtemp(), "history.json")
        for operator in ("ana", "ben"):
            with redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(
                    main(["drill", "disk-full", "--input", decisions,
                          "--operator", operator,
                          "--history-file", history_path]), 0)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["history", "--history-file", history_path]), 0)
        self.assertIn("ana", out.getvalue())
        self.assertIn("ben", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["history", "--operator", "ana",
                      "--history-file", history_path, "--json"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["summary"]["operator"], "ana")
        self.assertEqual(len(data["runs"]), 1)

    def test_cli_history_empty_file_reports_no_drills(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["history", "--history-file",
                      os.path.join(tempfile.mkdtemp(), "history.json")]),
                0)
        self.assertIn("no drills recorded yet", out.getvalue())

    def test_cli_history_unknown_operator_fails(self):
        decisions = self._write_decisions(
            "ack 3\nmitigate 20\nresolve 12\n")
        history_path = os.path.join(tempfile.mkdtemp(), "history.json")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["drill", "disk-full", "--input", decisions,
                      "--operator", "ana",
                      "--history-file", history_path]), 0)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["history", "--operator", "ben",
                      "--history-file", history_path]), 2)


class TestSev1EscalationChain(unittest.TestCase):
    def _run(self, scenario="payments-outage", policy=None, **kwargs):
        if policy is None:
            policy = get_policy("sev1")
        return simulate(get(scenario), policy, **kwargs)

    def _write_run(self, result):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(result.to_dict(), handle)
        return handle.name

    def test_sev1_policy_chain(self):
        policy = get_policy("sev1")
        self.assertEqual(
            [level.responder.role for level in policy.levels],
            ["on-call", "senior", "incident-commander"])
        self.assertEqual(
            [level.ack_timeout_min for level in policy.levels],
            [3, 5, 10])

    def test_payments_outage_scenario(self):
        scenario = get("payments-outage")
        self.assertEqual(scenario.severity, "critical")
        self.assertEqual(scenario.ack_delay_min, (None, None, 4))
        self.assertEqual(len(scenario.impact_stages), 3)
        self.assertGreaterEqual(len(scenario.hints), 1)

    def test_full_run_walks_the_whole_chain(self):
        result = self._run()
        score = result.score
        self.assertEqual(score.escalations, 2)
        # 3m on-call timeout + 5m senior timeout + 4m commander ack.
        self.assertEqual(score.time_to_ack_min, 12)
        self.assertEqual(score.time_to_mitigate_min, 62)
        self.assertEqual(score.time_to_resolve_min, 92)
        self.assertTrue(score.resolved)
        # 100 - 30 (two escalations) - 7 (ack past the 5m grace) -
        # (62-15)//2 = 23 -> 40.
        self.assertEqual(score.points, 40)
        chain = result.escalation_chain
        self.assertEqual(len(chain), 3)
        self.assertEqual([step.outcome for step in chain],
                         ["missed", "missed", "acked"])
        self.assertEqual(
            [step.responder_role for step in chain],
            ["on-call", "senior", "incident-commander"])
        self.assertEqual([step.paged_at_min for step in chain], [0, 3, 8])
        self.assertEqual(chain[2].acked_at_min, 12)
        self.assertEqual(chain[2].ack_late_min, 0)
        self.assertIsNone(chain[0].acked_at_min)
        self.assertIsNone(chain[0].ack_late_min)
        self.assertEqual(
            [step.impact for step in chain],
            list(get("payments-outage").impact_stages))

    def test_impact_grows_on_escalation_events(self):
        result = self._run()
        escalated = [event for event in result.timeline.events
                     if event.kind == "escalated"]
        self.assertEqual(len(escalated), 2)
        self.assertIn("impact now: Checkout down:",
                      escalated[0].detail)
        self.assertIn("impact now: Payments fully down:",
                      escalated[1].detail)

    def test_chain_in_json_and_replay_round_trip(self):
        result = self._run()
        data = result.to_dict()
        self.assertEqual(len(data["escalation_chain"]), 3)
        self.assertEqual(data["escalation_chain"][0]["outcome"],
                         "missed")
        self.assertEqual(data["escalation_chain"][2]["acked_at_min"], 12)
        self.assertEqual(
            data["escalation_chain"][2]["impact"],
            "Payments fully down: checkout and refunds failing, "
            "revenue impact growing")
        path = self._write_run(result)
        loaded = load_result(path)
        self.assertEqual(loaded.escalation_chain,
                         result.escalation_chain)
        self.assertEqual(loaded.to_dict(), data)

    def test_old_run_without_chain_loads_as_empty(self):
        data = self._run().to_dict()
        del data["escalation_chain"]
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(data, handle)
        self.assertEqual(load_result(handle.name).escalation_chain, [])

    def test_bad_chain_in_replay_file_raises(self):
        data = self._run().to_dict()
        data["escalation_chain"] = [{"level": 1, "outcome": "maybe"}]
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(data, handle)
        with self.assertRaises(ValueError):
            load_result(handle.name)

    def test_chain_printed_in_human_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "payments-outage", "--policy", "sev1"]), 0)
        out = buf.getvalue()
        self.assertIn("escalation chain:", out)
        self.assertIn("L1 ana (on-call) paged 0m, missed after 3m", out)
        self.assertIn("L3 carla (incident-commander) paged 8m, acked 12m",
                      out)

    def test_scenarios_without_stages_have_empty_impact(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        self.assertEqual(len(result.escalation_chain), 1)
        self.assertEqual(result.escalation_chain[0].outcome, "acked")
        self.assertEqual(result.escalation_chain[0].impact, "")
        self.assertNotIn("impact now:", result.timeline.render())

    def test_unacked_run_records_every_level_missed(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost",
            severity="critical", description="",
            ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
            impact_stages=("bad", "worse", "worst"))
        result = simulate(scenario, get_policy("sev1"))
        self.assertEqual([step.outcome for step in result.escalation_chain],
                         ["missed"] * 3)
        self.assertEqual([step.impact for step in result.escalation_chain],
                         ["bad", "worse", "worst"])
        self.assertEqual(result.score.points, 0)

    def test_impact_clamps_to_last_stage(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost",
            severity="critical", description="",
            ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
            impact_stages=("only-stage",))
        result = simulate(scenario, get_policy("sev1"))
        self.assertEqual([step.impact for step in result.escalation_chain],
                         ["only-stage"] * 3)
        # One stage means no growth, so no "impact now" on the events.
        self.assertNotIn("impact now:", result.timeline.render())

    def test_debrief_reports_impact_growth(self):
        debrief = generate_debrief(self._run())
        self.assertIn("Impact grew while the page went unanswered",
                      " ".join(debrief.went_badly))
        self.assertTrue(any("Escalate sooner" in item["action"]
                            for item in debrief.action_items))
        # A clean run reports no impact growth.
        clean = generate_debrief(simulate(get("disk-full"),
                                          DEFAULT_POLICY))
        self.assertNotIn("Impact grew", " ".join(clean.went_badly))

    def test_compare_diffs_chains(self):
        a = self._run()  # sev1: 3/5/10, acked 12m
        b = self._run(policy=DEFAULT_POLICY)  # standard: 5/5/10, acked 14m
        comparison = compare_results(a, b)
        self.assertFalse(comparison.chains_identical)
        self.assertIn("escalation chain:", comparison.render())
        self.assertIn("acked 12m", comparison.render())
        self.assertIn("acked 14m", comparison.render())
        data = comparison.to_dict()
        self.assertEqual(len(data["escalation_chain"]["a"]), 3)
        self.assertFalse(data["escalation_chain"]["identical"])
        # The same run compared with itself has an identical chain.
        same = compare_results(a, a)
        self.assertTrue(same.chains_identical)
        self.assertNotIn("escalation chain:", same.render())

    def test_stats_records_final_impact_and_growth(self):
        sev1 = self._run()
        clean = simulate(get("disk-full"), DEFAULT_POLICY)
        stats = aggregate_stats([sev1, clean])
        self.assertEqual(stats.impact_growth_runs, 1)
        self.assertEqual(
            stats.runs[0]["impact"],
            "Payments fully down: checkout and refunds failing, "
            "revenue impact growing")
        self.assertIsNone(stats.runs[1]["impact"])
        self.assertIn("impact grew while paging: 1 run(s)",
                      stats.render())
        self.assertEqual(stats.to_dict()["impact_growth_runs"], 1)

    def test_what_if_keeps_impact_stages(self):
        original = self._run()
        outcome = what_if(original, ack_timeout=5)
        replayed = outcome.replayed
        self.assertEqual(
            [step.impact for step in replayed.escalation_chain],
            [step.impact for step in original.escalation_chain])
        self.assertEqual(
            [step.outcome for step in replayed.escalation_chain],
            [step.outcome for step in original.escalation_chain])
        # The wider timeouts move the ack later (5+5+4=14m), but the
        # impact growth is the same.
        self.assertEqual(replayed.score.time_to_ack_min, 14)
        self.assertEqual(
            replayed.escalation_chain[2].impact,
            "Payments fully down: checkout and refunds failing, "
            "revenue impact growing")

    def test_drill_records_the_chain(self):
        decisions = io.StringIO(
            "wait\nwait\nack 4\nmitigate 50\nresolve 30\n")
        result = run_drill(get("payments-outage"), get_policy("sev1"),
                           input_stream=decisions,
                           prompt_stream=io.StringIO())
        self.assertEqual([step.outcome for step in result.escalation_chain],
                         ["missed", "missed", "acked"])
        self.assertEqual(result.score.points, 40)
        self.assertEqual(
            [step.impact for step in result.escalation_chain],
            list(get("payments-outage").impact_stages))

    def test_drill_quit_before_decision_records_no_step(self):
        decisions = io.StringIO("quit\n")
        result = run_drill(get("payments-outage"), get_policy("sev1"),
                           input_stream=decisions,
                           prompt_stream=io.StringIO())
        self.assertEqual(result.escalation_chain, [])

    def test_template_round_trips_impact_stages(self):
        data = scenario_to_dict(get("payments-outage"))
        self.assertEqual(data["impact_stages"],
                         list(get("payments-outage").impact_stages))
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"payments-outage": data}}, handle)
        loaded = load_scenario_file(handle.name)
        self.assertEqual(loaded["payments-outage"].impact_stages,
                         get("payments-outage").impact_stages)

    def test_scenario_file_rejects_bad_impact_stages(self):
        base = scenario_to_dict(get("payments-outage"))
        for bad in ("not-a-list", ["", "ok"], [3]):
            data = dict(base, impact_stages=bad)
            handle = tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8")
            with handle:
                json.dump({"scenarios": {"bad": data}}, handle)
            with self.assertRaises(ValueError):
                load_scenario_file(handle.name)

    def test_named_timeout_override_by_sev1_role(self):
        # --ack-timeouts on-call=5 names the sev1 role.
        policy = parse_ack_timeout_overrides(["on-call=5"],
                                             get_policy("sev1"))
        self.assertEqual([level.ack_timeout_min for level in policy.levels],
                         [5, 5, 10])


class TestSsoOutageScenario(unittest.TestCase):
    """sso-outage: the first built-in scenario nobody acks.

    The pages go to a stale on-call rotation, so every level misses
    and the incident ends unacked with a score of 0 — the library's
    only built-in run of the unacked path.
    """

    def test_sso_outage_scenario(self):
        scenario = get("sso-outage")
        self.assertEqual(scenario.severity, "critical")
        self.assertEqual(scenario.service, "identity-sso")
        self.assertEqual(scenario.ack_delay_min, (None, None, None))
        self.assertEqual(scenario.slo_percent, 99.95)
        self.assertEqual(len(scenario.impact_stages), 3)
        self.assertGreaterEqual(len(scenario.hints), 1)

    def test_unacked_run_walks_the_whole_chain_and_scores_zero(self):
        result = simulate(get("sso-outage"), DEFAULT_POLICY)
        score = result.score
        self.assertIsNone(score.time_to_ack_min)
        self.assertIsNone(score.time_to_mitigate_min)
        self.assertIsNone(score.time_to_resolve_min)
        self.assertFalse(score.resolved)
        self.assertEqual(score.points, 0)
        # Two escalations: primary -> secondary -> manager, then the
        # manager's miss ends the incident unacked.
        self.assertEqual(score.escalations, 2)
        self.assertIsNone(score.downtime_min)
        self.assertIsNone(score.error_budget_burn_pct)
        self.assertIn("unacked", result.timeline.kinds())
        chain = result.escalation_chain
        self.assertEqual(len(chain), 3)
        self.assertEqual([step.outcome for step in chain],
                         ["missed", "missed", "missed"])
        # Standard timeouts 5m, 5m, 10m: paged at 0m, 5m, 10m.
        self.assertEqual([step.paged_at_min for step in chain], [0, 5, 10])
        self.assertEqual(
            [step.impact for step in chain],
            list(get("sso-outage").impact_stages))

    def test_impact_grows_on_escalation_events(self):
        result = simulate(get("sso-outage"), DEFAULT_POLICY)
        escalated = [event for event in result.timeline.events
                     if event.kind == "escalated"]
        self.assertEqual(len(escalated), 2)
        self.assertIn("impact now: Logins down:", escalated[0].detail)
        self.assertIn("impact now: Sessions expiring:", escalated[1].detail)

    def test_debrief_of_unacked_run_adds_fallback_page_action(self):
        result = simulate(get("sso-outage"), DEFAULT_POLICY)
        text = generate_debrief(result).render()
        self.assertIn("nobody acked", text)
        self.assertIn("fallback", text)
        for name in ("ana", "ben", "carla"):
            self.assertIn(name, text)

    def test_template_round_trips_sso_outage(self):
        data = scenario_to_dict(get("sso-outage"))
        self.assertEqual(data["ack_delay_min"], [None, None, None])
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"sso-outage": data}}, handle)
        loaded = load_scenario_file(handle.name)
        self.assertEqual(loaded["sso-outage"], get("sso-outage"))

    def test_cli_runs_sso_outage(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "sso-outage"]), 0)
        out = buf.getvalue()
        self.assertIn("unacked", out)
        self.assertIn("0/100", out)


class TestConfigPushScenario(unittest.TestCase):
    """config-push: a bad config push, not a code deploy.

    The primary misses the page and the secondary acks 3m late, so
    this is the library's first built-in run that exercises a miss
    and a late ack together — the impact grows while the page goes
    unanswered, and the late ack keeps only 40% of the score.
    """

    def test_config_push_scenario(self):
        scenario = get("config-push")
        self.assertEqual(scenario.severity, "critical")
        self.assertEqual(scenario.service, "edge-config")
        self.assertEqual(scenario.ack_delay_min, (None, 8, None))
        self.assertEqual(scenario.slo_percent, 99.9)
        self.assertEqual(len(scenario.impact_stages), 2)
        self.assertGreaterEqual(len(scenario.hints), 1)

    def test_miss_then_late_ack_scores_with_partial_credit(self):
        result = simulate(get("config-push"), DEFAULT_POLICY)
        score = result.score
        # Standard timeouts 5m, 5m, 10m: the primary misses at 5m and
        # the secondary acks 8m after being paged, at 13m — 3m late.
        self.assertEqual(score.time_to_ack_min, 13)
        self.assertEqual(score.time_to_mitigate_min, 43)
        self.assertEqual(score.time_to_resolve_min, 63)
        self.assertTrue(score.resolved)
        self.assertEqual(score.escalations, 1)
        self.assertEqual(score.ack_late_min, 3)
        self.assertAlmostEqual(score.ack_credit, 0.4)
        # 100 - 15 (one escalation) - 8 (13m to ack past the 5m
        # grace) - 14 (43m to mitigate past the 15m grace, halved)
        # = 63, scaled by the 40% late-ack credit.
        self.assertEqual(score.points, 25)
        # Downtime 43m against a 99.9% budget of 43.2m.
        self.assertEqual(score.downtime_min, 43)
        self.assertEqual(score.error_budget_burn_pct, 99.5)
        chain = result.escalation_chain
        self.assertEqual(len(chain), 2)
        self.assertEqual([step.outcome for step in chain],
                         ["missed", "acked"])
        self.assertEqual([step.paged_at_min for step in chain], [0, 5])
        self.assertEqual(
            [step.impact for step in chain],
            list(get("config-push").impact_stages))

    def test_impact_grows_on_escalation_event(self):
        result = simulate(get("config-push"), DEFAULT_POLICY)
        escalated = [event for event in result.timeline.events
                     if event.kind == "escalated"]
        self.assertEqual(len(escalated), 1)
        self.assertIn("impact now: Checkout down in all regions:",
                      escalated[0].detail)

    def test_debrief_of_late_ack_names_the_lateness_and_credit(self):
        result = simulate(get("config-push"), DEFAULT_POLICY)
        text = generate_debrief(result).render()
        self.assertIn("3m late", text)
        self.assertIn("40%", text)
        self.assertIn("ben", text)

    def test_template_round_trips_config_push(self):
        data = scenario_to_dict(get("config-push"))
        self.assertEqual(data["ack_delay_min"], [None, 8, None])
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"config-push": data}}, handle)
        loaded = load_scenario_file(handle.name)
        self.assertEqual(loaded["config-push"], get("config-push"))

    def test_cli_runs_config_push(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "config-push"]), 0)
        out = buf.getvalue()
        self.assertIn("late by 3m", out)
        self.assertIn("25/100", out)


class TestDbFailoverScenario(unittest.TestCase):
    """db-failover: the primary database fails over to a replica.

    The primary and the secondary both miss the page and the manager
    acks 2m late, so this is the library's first built-in run that
    exercises two misses and a late ack together — the impact grows
    across all three stages while the page goes unanswered, and the
    late ack keeps 80% of the score.
    """

    def test_db_failover_scenario(self):
        scenario = get("db-failover")
        self.assertEqual(scenario.severity, "critical")
        self.assertEqual(scenario.service, "orders-db")
        self.assertEqual(scenario.ack_delay_min, (None, None, 12))
        self.assertEqual(scenario.slo_percent, 99.95)
        self.assertEqual(len(scenario.impact_stages), 3)
        self.assertGreaterEqual(len(scenario.hints), 1)

    def test_two_misses_then_late_ack_scores_with_partial_credit(self):
        result = simulate(get("db-failover"), DEFAULT_POLICY)
        score = result.score
        # Standard timeouts 5m, 5m, 10m: the primary misses at 5m,
        # the secondary misses at 10m and the manager acks 12m after
        # being paged, at 22m — 2m late.
        self.assertEqual(score.time_to_ack_min, 22)
        self.assertEqual(score.time_to_mitigate_min, 62)
        self.assertEqual(score.time_to_resolve_min, 87)
        self.assertTrue(score.resolved)
        self.assertEqual(score.escalations, 2)
        self.assertEqual(score.ack_late_min, 2)
        self.assertAlmostEqual(score.ack_credit, 0.8)
        # 100 - 30 (two escalations) - 17 (22m to ack past the 5m
        # grace) - 23 (62m to mitigate past the 15m grace, halved)
        # = 30, scaled by the 80% late-ack credit.
        self.assertEqual(score.points, 24)
        # Downtime 62m against a 99.95% budget of 21.6m.
        self.assertEqual(score.downtime_min, 62)
        self.assertEqual(score.error_budget_burn_pct, 287.0)
        chain = result.escalation_chain
        self.assertEqual(len(chain), 3)
        self.assertEqual([step.outcome for step in chain],
                         ["missed", "missed", "acked"])
        self.assertEqual([step.paged_at_min for step in chain], [0, 5, 10])
        self.assertEqual(
            [step.impact for step in chain],
            list(get("db-failover").impact_stages))

    def test_impact_grows_on_each_escalation_event(self):
        result = simulate(get("db-failover"), DEFAULT_POLICY)
        escalated = [event for event in result.timeline.events
                     if event.kind == "escalated"]
        self.assertEqual(len(escalated), 2)
        self.assertIn("impact now: Orders failing: replica promotion "
                      "started, cache expiring", escalated[0].detail)
        self.assertIn("impact now: Failover in progress: replica "
                      "catching up, reads degrading as the cache "
                      "drains", escalated[1].detail)

    def test_debrief_of_late_ack_names_the_lateness_and_credit(self):
        result = simulate(get("db-failover"), DEFAULT_POLICY)
        text = generate_debrief(result).render()
        self.assertIn("2m late", text)
        self.assertIn("80%", text)
        self.assertIn("carla", text)

    def test_template_round_trips_db_failover(self):
        data = scenario_to_dict(get("db-failover"))
        self.assertEqual(data["ack_delay_min"], [None, None, 12])
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"db-failover": data}}, handle)
        loaded = load_scenario_file(handle.name)
        self.assertEqual(loaded["db-failover"], get("db-failover"))

    def test_cli_runs_db_failover(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "db-failover"]), 0)
        out = buf.getvalue()
        self.assertIn("late by 2m", out)
        self.assertIn("24/100", out)


class TestNetworkPartitionScenario(unittest.TestCase):
    """network-partition: the datacenters cannot reach each other.

    The primary and the secondary both miss the page — their paging
    tooling sits on the unreachable side — and the manager acks on
    time, so this is the library's first built-in run that exercises
    two misses with an on-time ack: the impact grows across all three
    stages while the page goes unanswered, and the full ack credit
    is kept.
    """

    def test_network_partition_scenario(self):
        scenario = get("network-partition")
        self.assertEqual(scenario.severity, "critical")
        self.assertEqual(scenario.service, "checkout-mesh")
        self.assertEqual(scenario.ack_delay_min, (None, None, 8))
        self.assertEqual(scenario.slo_percent, 99.9)
        self.assertEqual(len(scenario.impact_stages), 3)
        self.assertEqual(len(scenario.hints), 3)

    def test_two_misses_then_on_time_ack_scores_with_full_credit(self):
        result = simulate(get("network-partition"), DEFAULT_POLICY)
        score = result.score
        # Standard timeouts 5m, 5m, 10m: the primary misses at 5m,
        # the secondary misses at 10m and the manager acks 8m after
        # being paged, at 18m — on time against the 10m timeout.
        self.assertEqual(score.time_to_ack_min, 18)
        self.assertEqual(score.time_to_mitigate_min, 63)
        self.assertEqual(score.time_to_resolve_min, 88)
        self.assertTrue(score.resolved)
        self.assertEqual(score.escalations, 2)
        self.assertEqual(score.ack_late_min, 0)
        self.assertAlmostEqual(score.ack_credit, 1.0)
        # 100 - 30 (two escalations) - 13 (18m to ack past the 5m
        # grace) - 24 (63m to mitigate past the 15m grace, halved)
        # = 33, with the full ack credit kept.
        self.assertEqual(score.points, 33)
        # Downtime 63m against a 99.9% budget of 43.2m.
        self.assertEqual(score.downtime_min, 63)
        self.assertEqual(score.error_budget_burn_pct, 145.8)
        chain = result.escalation_chain
        self.assertEqual(len(chain), 3)
        self.assertEqual([step.outcome for step in chain],
                         ["missed", "missed", "acked"])
        self.assertEqual([step.paged_at_min for step in chain], [0, 5, 10])
        self.assertEqual(
            [step.impact for step in chain],
            list(get("network-partition").impact_stages))

    def test_impact_grows_on_each_escalation_event(self):
        result = simulate(get("network-partition"), DEFAULT_POLICY)
        escalated = [event for event in result.timeline.events
                     if event.kind == "escalated"]
        self.assertEqual(len(escalated), 2)
        self.assertIn("impact now: Orders stuck: writes cannot reach "
                      "the primary datacenter, checkout queue growing",
                      escalated[0].detail)
        self.assertIn("impact now: Split-brain risk: both sides still "
                      "serving reads, reconciliation needed once the "
                      "link returns", escalated[1].detail)

    def test_debrief_of_unanswered_pages_names_the_missed_responders(self):
        result = simulate(get("network-partition"), DEFAULT_POLICY)
        text = generate_debrief(result).render()
        self.assertIn("ana", text)
        self.assertIn("ben", text)
        self.assertIn("Split-brain", text)

    def test_template_round_trips_network_partition(self):
        data = scenario_to_dict(get("network-partition"))
        self.assertEqual(data["ack_delay_min"], [None, None, 8])
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"network-partition": data}}, handle)
        loaded = load_scenario_file(handle.name)
        self.assertEqual(loaded["network-partition"],
                         get("network-partition"))

    def test_cli_runs_network_partition(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "network-partition"]), 0)
        out = buf.getvalue()
        self.assertIn("33/100", out)

    def test_cli_runs_network_partition_with_hints(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "network-partition", "--hints", "3"]), 0)
        out = buf.getvalue()
        self.assertIn("inter-datacenter link", out)
        self.assertIn("18/100", out)


class TestPoisonPillScenario(unittest.TestCase):
    """poison-pill: one bad message crash-loops the consumers.

    The primary misses the page — the alert storm buries it — and
    the secondary acks on time, so this is the library's first
    built-in run that exercises a single miss with an on-time ack
    on a high-severity (not critical) service with a 99.5% SLO:
    the impact grows across both stages while the queue backs up,
    and the full ack credit is kept.
    """

    def test_poison_pill_scenario(self):
        scenario = get("poison-pill")
        self.assertEqual(scenario.severity, "high")
        self.assertEqual(scenario.service, "billing-queue")
        self.assertEqual(scenario.ack_delay_min, (None, 4))
        self.assertEqual(scenario.slo_percent, 99.5)
        self.assertEqual(len(scenario.impact_stages), 2)
        self.assertEqual(len(scenario.hints), 3)

    def test_miss_then_on_time_ack_scores_with_full_credit(self):
        result = simulate(get("poison-pill"), DEFAULT_POLICY)
        score = result.score
        # Standard timeouts 5m, 5m, 10m: the primary misses at 5m
        # and the secondary acks 4m after being paged, at 9m — on
        # time against the 5m timeout.
        self.assertEqual(score.time_to_ack_min, 9)
        self.assertEqual(score.time_to_mitigate_min, 59)
        self.assertEqual(score.time_to_resolve_min, 79)
        self.assertTrue(score.resolved)
        self.assertEqual(score.escalations, 1)
        self.assertEqual(score.ack_late_min, 0)
        self.assertAlmostEqual(score.ack_credit, 1.0)
        # 100 - 15 (one escalation) - 4 (9m to ack past the 5m
        # grace) - 22 (59m to mitigate past the 15m grace, halved)
        # = 59, with the full ack credit kept.
        self.assertEqual(score.points, 59)
        # Downtime 59m against a 99.5% budget of 216m.
        self.assertEqual(score.downtime_min, 59)
        self.assertEqual(score.error_budget_burn_pct, 27.3)
        chain = result.escalation_chain
        self.assertEqual(len(chain), 2)
        self.assertEqual([step.outcome for step in chain],
                         ["missed", "acked"])
        self.assertEqual([step.paged_at_min for step in chain], [0, 5])
        self.assertEqual(
            [step.impact for step in chain],
            list(get("poison-pill").impact_stages))

    def test_impact_grows_on_the_escalation_event(self):
        result = simulate(get("poison-pill"), DEFAULT_POLICY)
        escalated = [event for event in result.timeline.events
                     if event.kind == "escalated"]
        self.assertEqual(len(escalated), 1)
        self.assertIn("impact now: Billing events stuck: orders placed "
                      "but not billed, invoices delayed",
                      escalated[0].detail)

    def test_debrief_of_missed_page_names_the_missed_responder(self):
        result = simulate(get("poison-pill"), DEFAULT_POLICY)
        text = generate_debrief(result).render()
        self.assertIn("ana", text)
        self.assertIn("ben", text)
        self.assertIn("Billing events", text)

    def test_template_round_trips_poison_pill(self):
        data = scenario_to_dict(get("poison-pill"))
        self.assertEqual(data["ack_delay_min"], [None, 4])
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"poison-pill": data}}, handle)
        loaded = load_scenario_file(handle.name)
        self.assertEqual(loaded["poison-pill"], get("poison-pill"))

    def test_cli_runs_poison_pill(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "poison-pill"]), 0)
        out = buf.getvalue()
        self.assertIn("59/100", out)

    def test_cli_runs_poison_pill_with_hints(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "poison-pill", "--hints", "3"]), 0)
        out = buf.getvalue()
        self.assertIn("dead-letter queue", out)
        self.assertIn("44/100", out)


class TestCdnPurgeFailureScenario(unittest.TestCase):
    """cdn-purge-failure: a purge that silently fails, stale assets
    keep serving.

    The primary acks late — the purge dashboard says the purge
    worked, so the primary does not believe the page at first and
    takes 8m against a 5m timeout — so this is the library's first
    built-in run that exercises a late ack by the primary, with no
    miss at all: no escalation, a single chain step carrying the
    first impact stage, and the late ack keeping only 40% of the
    score. The impact stages spell out the stale-content spread
    while the primary is slow to ack.
    """

    def test_cdn_purge_failure_scenario(self):
        scenario = get("cdn-purge-failure")
        self.assertEqual(scenario.severity, "high")
        self.assertEqual(scenario.service, "edge-cdn")
        self.assertEqual(scenario.ack_delay_min, (8, None, None))
        self.assertEqual(scenario.slo_percent, 99.9)
        self.assertEqual(len(scenario.impact_stages), 3)
        self.assertEqual(len(scenario.hints), 3)

    def test_primary_late_ack_scores_with_partial_credit(self):
        result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY)
        score = result.score
        # Standard timeouts 5m, 5m, 10m: the primary acks 8m after
        # being paged — 3m late against the 5m timeout — so nobody
        # misses and the incident never escalates.
        self.assertEqual(score.time_to_ack_min, 8)
        self.assertEqual(score.time_to_mitigate_min, 38)
        self.assertEqual(score.time_to_resolve_min, 53)
        self.assertTrue(score.resolved)
        self.assertEqual(score.escalations, 0)
        self.assertEqual(score.ack_late_min, 3)
        self.assertAlmostEqual(score.ack_credit, 0.4)
        # 100 - 3 (8m to ack past the 5m grace) - 11 (38m to
        # mitigate past the 15m grace, halved) = 86, scaled by the
        # 0.4 late-ack credit = 34.
        self.assertEqual(score.points, 34)
        # Downtime 38m against a 99.9% budget of 43.2m.
        self.assertEqual(score.downtime_min, 38)
        self.assertEqual(score.error_budget_burn_pct, 88.0)
        chain = result.escalation_chain
        self.assertEqual(len(chain), 1)
        self.assertEqual([step.outcome for step in chain], ["acked"])
        self.assertEqual([step.paged_at_min for step in chain], [0])
        # No miss means only the first impact stage ever applies.
        self.assertEqual(chain[0].impact,
                         get("cdn-purge-failure").impact_stages[0])

    def test_debrief_names_the_late_primary(self):
        result = simulate(get("cdn-purge-failure"), DEFAULT_POLICY)
        text = generate_debrief(result).render()
        self.assertIn("ana", text)
        self.assertIn("stale assets", text)
        self.assertIn("late", text)

    def test_template_round_trips_cdn_purge_failure(self):
        data = scenario_to_dict(get("cdn-purge-failure"))
        self.assertEqual(data["ack_delay_min"], [8, None, None])
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"cdn-purge-failure": data}}, handle)
        loaded = load_scenario_file(handle.name)
        self.assertEqual(loaded["cdn-purge-failure"],
                         get("cdn-purge-failure"))

    def test_cli_runs_cdn_purge_failure(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "cdn-purge-failure"]), 0)
        out = buf.getvalue()
        self.assertIn("34/100", out)

    def test_cli_runs_cdn_purge_failure_with_hints(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "cdn-purge-failure", "--hints", "3"]), 0)
        out = buf.getvalue()
        self.assertIn("purge dashboard", out)
        self.assertIn("19/100", out)


class TestFraudRulesMisfireScenario(unittest.TestCase):
    """fraud-rules-misfire: a fraud-rules update declines every payment.

    The primary misses the page — phone on silent in the rules-deploy
    review — and the secondary acks in 2m, inside the fast policy's 2m
    window, so this is the library's first built-in run designed for
    the fast policy: the chain fires after just 2m, the incident is
    acked in 4m with the full credit kept, and the impact grows across
    both stages while payments are declined.
    """

    def test_fraud_rules_misfire_scenario(self):
        scenario = get("fraud-rules-misfire")
        self.assertEqual(scenario.severity, "critical")
        self.assertEqual(scenario.service, "payments-fraud")
        self.assertEqual(scenario.ack_delay_min, (None, 2))
        self.assertEqual(scenario.slo_percent, 99.95)
        self.assertEqual(len(scenario.impact_stages), 2)
        self.assertEqual(len(scenario.hints), 3)

    def test_miss_then_fast_on_time_ack_scores_with_full_credit(self):
        result = simulate(get("fraud-rules-misfire"), get_policy("fast"))
        score = result.score
        # Fast timeouts 2m, 2m, 5m: the primary misses at 2m and the
        # secondary acks 2m after being paged, at 4m — on time against
        # the 2m timeout.
        self.assertEqual(score.time_to_ack_min, 4)
        self.assertEqual(score.time_to_mitigate_min, 34)
        self.assertEqual(score.time_to_resolve_min, 49)
        self.assertTrue(score.resolved)
        self.assertEqual(score.escalations, 1)
        self.assertEqual(score.ack_late_min, 0)
        self.assertAlmostEqual(score.ack_credit, 1.0)
        # 100 - 15 (one escalation) - 0 (4m to ack inside the 5m
        # grace) - 9 (34m to mitigate past the 15m grace, halved)
        # = 76, with the full ack credit kept.
        self.assertEqual(score.points, 76)
        # Downtime 34m against a 99.95% budget of 21.6m.
        self.assertEqual(score.downtime_min, 34)
        self.assertEqual(score.error_budget_burn_pct, 157.4)
        chain = result.escalation_chain
        self.assertEqual(len(chain), 2)
        self.assertEqual([step.outcome for step in chain],
                         ["missed", "acked"])
        self.assertEqual([step.paged_at_min for step in chain], [0, 2])
        self.assertEqual(
            [step.impact for step in chain],
            list(get("fraud-rules-misfire").impact_stages))

    def test_impact_grows_on_the_escalation_event(self):
        result = simulate(get("fraud-rules-misfire"), get_policy("fast"))
        escalated = [event for event in result.timeline.events
                     if event.kind == "escalated"]
        self.assertEqual(len(escalated), 1)
        self.assertIn("impact now: Checkout blocked: most payments "
                      "declined, orders failing, revenue stopped",
                      escalated[0].detail)

    def test_debrief_of_missed_page_names_the_missed_responder(self):
        result = simulate(get("fraud-rules-misfire"), get_policy("fast"))
        text = generate_debrief(result).render()
        self.assertIn("ana", text)
        self.assertIn("ben", text)
        self.assertIn("Checkout blocked", text)

    def test_template_round_trips_fraud_rules_misfire(self):
        data = scenario_to_dict(get("fraud-rules-misfire"))
        self.assertEqual(data["ack_delay_min"], [None, 2])
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"fraud-rules-misfire": data}}, handle)
        loaded = load_scenario_file(handle.name)
        self.assertEqual(loaded["fraud-rules-misfire"],
                         get("fraud-rules-misfire"))

    def test_drill_matches_run_for_same_decisions(self):
        drill = run_drill(
            get("fraud-rules-misfire"), get_policy("fast"),
            input_stream=io.StringIO(
                "wait\nack 2\nmitigate 30\nresolve 15\n"),
            prompt_stream=io.StringIO())
        run = simulate(get("fraud-rules-misfire"), get_policy("fast"))
        self.assertEqual(drill.to_dict(), run.to_dict())
        self.assertEqual(drill.score.points, 76)

    def test_cli_runs_fraud_rules_misfire_fast(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "fraud-rules-misfire", "--policy", "fast"]),
                0)
        out = buf.getvalue()
        self.assertIn("76/100", out)

    def test_cli_runs_fraud_rules_misfire_fast_with_hints(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "fraud-rules-misfire", "--policy", "fast",
                      "--hints", "3"]), 0)
        out = buf.getvalue()
        self.assertIn("fraud-rules deploy", out)
        self.assertIn("61/100", out)


class TestWafRulesBlockScenario(unittest.TestCase):
    """waf-rules-block: a WAF rules update blocks legitimate checkout.

    The primary acks in 2m, on time, and the rollback itself takes
    13m, so without verification the incident mitigates in 15m —
    exactly inside the 15m grace — and scores 100 (grade A). But the
    rollback is only real once it is confirmed in every region, and
    the far region is slow: ap-southeast takes 14m to confirm,
    because its edge PoP only pulls WAF rules every 15 minutes, so
    verifying in every region costs 22m, mitigation lands at 37m
    and the score drops to 89 (grade B) — the slow far region costs
    a whole grade relative to the same incident mitigated on time.
    """

    def test_waf_rules_block_scenario(self):
        scenario = get("waf-rules-block")
        self.assertEqual(scenario.severity, "critical")
        self.assertEqual(scenario.service, "edge-waf")
        self.assertEqual(scenario.ack_delay_min, (2, None, None))
        self.assertEqual(scenario.slo_percent, 99.9)
        self.assertEqual(scenario.regions,
                         ("us-east", "eu-west", "ap-southeast"))
        self.assertEqual(scenario.verify_delay_min_by_region,
                         {"us-east": 3, "eu-west": 5, "ap-southeast": 14})
        self.assertEqual(scenario.verify_delay_min_total(), 22)
        self.assertEqual(len(scenario.impact_stages), 2)
        self.assertEqual(len(scenario.hints), 3)

    def test_unverified_mitigates_inside_the_grace_and_scores_100(self):
        result = simulate(get("waf-rules-block"), DEFAULT_POLICY)
        score = result.score
        self.assertEqual(score.time_to_ack_min, 2)
        self.assertEqual(score.time_to_mitigate_min, 15)
        self.assertEqual(score.time_to_resolve_min, 25)
        self.assertTrue(score.resolved)
        self.assertEqual(score.escalations, 0)
        self.assertEqual(score.ack_late_min, 0)
        self.assertAlmostEqual(score.ack_credit, 1.0)
        self.assertEqual(score.verify_delay_min, 0)
        # 100 - 0 - 0 (2m to ack inside the 5m grace) - 0 (15m to
        # mitigate inside the 15m grace) = 100, grade A.
        self.assertEqual(score.points, 100)
        self.assertEqual(grade(score.points), "A")
        # Downtime 15m against a 99.9% budget of 43.2m.
        self.assertEqual(score.downtime_min, 15)
        self.assertEqual(score.error_budget_burn_pct, 34.7)

    def test_verified_per_region_costs_a_grade(self):
        result = simulate(get("waf-rules-block"), DEFAULT_POLICY,
                          purge_verification="per-region")
        score = result.score
        self.assertEqual(score.verify_delay_min, 22)
        # The checks land at 5m, 10m and 24m: the 14m ap-southeast
        # check is what pushes mitigation past the grace.
        verified = [event for event in result.timeline.events
                    if event.kind == "verified"]
        self.assertEqual(
            [(event.t_min, event.actor) for event in verified],
            [(5, "us-east"), (10, "eu-west"), (24, "ap-southeast")])
        self.assertEqual(
            [event.detail for event in verified],
            ["fix confirmed in us-east (3m)",
             "fix confirmed in eu-west (5m)",
             "fix confirmed in ap-southeast (14m)"])
        self.assertEqual(score.time_to_mitigate_min, 37)
        self.assertEqual(score.time_to_resolve_min, 47)
        # 100 - 11 (37m to mitigate past the 15m grace, halved)
        # = 89, grade B — one grade below the unverified run.
        self.assertEqual(score.points, 89)
        self.assertEqual(grade(score.points), "B")
        self.assertEqual(score.downtime_min, 37)
        self.assertEqual(score.error_budget_burn_pct, 85.6)

    def test_what_if_turning_verification_on_costs_a_grade(self):
        base = simulate(get("waf-rules-block"), DEFAULT_POLICY)
        outcome = what_if(base, purge_verification="per-region")
        self.assertEqual(outcome.replayed.score.points, 89)
        self.assertEqual(outcome.points_delta, -11)
        self.assertEqual(grade(outcome.replayed.score.points), "B")

    def test_drill_matches_run_for_same_decisions(self):
        drill = run_drill(
            get("waf-rules-block"), DEFAULT_POLICY,
            input_stream=io.StringIO("ack 2\nmitigate 13\nresolve 10\n"),
            prompt_stream=io.StringIO(),
            purge_verification="per-region")
        run = simulate(get("waf-rules-block"), DEFAULT_POLICY,
                       purge_verification="per-region")
        self.assertEqual(drill.to_dict(), run.to_dict())
        self.assertEqual(drill.score.points, 89)
        plain_drill = run_drill(
            get("waf-rules-block"), DEFAULT_POLICY,
            input_stream=io.StringIO("ack 2\nmitigate 13\nresolve 10\n"),
            prompt_stream=io.StringIO())
        plain_run = simulate(get("waf-rules-block"), DEFAULT_POLICY)
        self.assertEqual(plain_drill.to_dict(), plain_run.to_dict())
        self.assertEqual(plain_drill.score.points, 100)

    def test_template_round_trips_waf_rules_block(self):
        data = scenario_to_dict(get("waf-rules-block"))
        self.assertEqual(data["ack_delay_min"], [2, None, None])
        self.assertEqual(data["regions"],
                         ["us-east", "eu-west", "ap-southeast"])
        self.assertEqual(data["verify_delay_min_by_region"],
                         {"us-east": 3, "eu-west": 5, "ap-southeast": 14})
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"waf-rules-block": data}}, handle)
        loaded = load_scenario_file(handle.name)
        self.assertEqual(loaded["waf-rules-block"],
                         get("waf-rules-block"))

    def test_shift_runs_waf_rules_block_like_a_run(self):
        shift = simulate_shift(
            [get("waf-rules-block"), get("disk-full")], DEFAULT_POLICY,
            slo_percent=99.9)
        self.assertEqual(
            [run.score.points for run in shift.runs], [100, 96])
        verified = simulate_shift(
            [get("waf-rules-block")], DEFAULT_POLICY,
            slo_percent=99.9, purge_verification="per-region")
        self.assertEqual(
            [run.score.points for run in verified.runs], [89])
        self.assertEqual(verified.total_burn_pct, 85.6)

    def test_cli_runs_waf_rules_block(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "waf-rules-block"]), 0)
        self.assertIn("100/100", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main([
                "run", "waf-rules-block",
                "--verify-purge", "per-region"]), 0)
        out = buf.getvalue()
        self.assertIn(
            "verify delay:     22m "
            "(us-east 3m, eu-west 5m, ap-southeast 14m)", out)
        self.assertIn("89/100", out)


class TestKeyRotationScenario(unittest.TestCase):
    """key-rotation: a KMS key rotation breaks saved-card decryption.

    The on-call acks 2m late against the sev1 policy's 3m window, so
    this is the library's first built-in run designed for the sev1
    policy with a late ack by the first level: no miss, no escalation,
    but the late ack keeps only a third of the score, and the chain
    records the first impact stage while the stages spell out the
    token-decryption failure spreading.
    """

    def test_key_rotation_scenario(self):
        scenario = get("key-rotation")
        self.assertEqual(scenario.severity, "critical")
        self.assertEqual(scenario.service, "payments-tokens")
        self.assertEqual(scenario.ack_delay_min, (5,))
        self.assertEqual(scenario.slo_percent, 99.95)
        self.assertEqual(len(scenario.impact_stages), 3)
        self.assertEqual(len(scenario.hints), 3)

    def test_late_ack_on_sev1_keeps_a_third_of_the_score(self):
        result = simulate(get("key-rotation"), get_policy("sev1"))
        score = result.score
        # Sev1 timeouts 3m, 5m, 10m: the on-call acks at 5m, 2m late
        # against the 3m window, with no escalation.
        self.assertEqual(score.time_to_ack_min, 5)
        self.assertEqual(score.time_to_mitigate_min, 40)
        self.assertEqual(score.time_to_resolve_min, 60)
        self.assertTrue(score.resolved)
        self.assertEqual(score.escalations, 0)
        self.assertEqual(score.ack_late_min, 2)
        self.assertAlmostEqual(score.ack_credit, 1 / 3)
        # 100 - 0 (no escalations) - 0 (5m to ack inside the 5m
        # grace) - 12 (40m to mitigate past the 15m grace, halved)
        # = 88, scaled by the 1/3 ack credit: round(88 / 3) = 29.
        self.assertEqual(score.points, 29)
        # Downtime 40m against a 99.95% budget of 21.6m.
        self.assertEqual(score.downtime_min, 40)
        self.assertEqual(score.error_budget_burn_pct, 185.2)
        chain = result.escalation_chain
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0].outcome, "acked")
        self.assertEqual(chain[0].paged_at_min, 0)
        self.assertEqual(chain[0].acked_at_min, 5)
        self.assertEqual(chain[0].ack_late_min, 2)
        self.assertEqual(
            chain[0].impact,
            "Saved-card checkouts failing: stored tokens cannot be "
            "decrypted, new cards still work")

    def test_late_ack_is_on_time_on_the_standard_policy(self):
        # The same 5m ack is on time against the standard policy's
        # 5m primary timeout, so the full credit is kept there: the
        # lateness is a property of the sev1 policy, not the incident.
        result = simulate(get("key-rotation"), DEFAULT_POLICY)
        score = result.score
        self.assertEqual(score.ack_late_min, 0)
        self.assertAlmostEqual(score.ack_credit, 1.0)
        self.assertEqual(score.points, 88)

    def test_debrief_of_late_ack_names_the_lateness(self):
        result = simulate(get("key-rotation"), get_policy("sev1"))
        text = generate_debrief(result).render()
        self.assertIn("ana", text)
        self.assertIn("2m late", text)
        self.assertIn("33% of the score", text)

    def test_template_round_trips_key_rotation(self):
        data = scenario_to_dict(get("key-rotation"))
        self.assertEqual(data["ack_delay_min"], [5])
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"key-rotation": data}}, handle)
        loaded = load_scenario_file(handle.name)
        self.assertEqual(loaded["key-rotation"], get("key-rotation"))

    def test_drill_matches_run_for_same_decisions(self):
        drill = run_drill(
            get("key-rotation"), get_policy("sev1"),
            input_stream=io.StringIO(
                "ack 5\nmitigate 35\nresolve 20\n"),
            prompt_stream=io.StringIO())
        run = simulate(get("key-rotation"), get_policy("sev1"))
        self.assertEqual(drill.to_dict(), run.to_dict())
        self.assertEqual(drill.score.points, 29)

    def test_cli_runs_key_rotation_sev1(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "key-rotation", "--policy", "sev1"]),
                0)
        out = buf.getvalue()
        self.assertIn("29/100", out)

    def test_cli_runs_key_rotation_sev1_with_hints(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "key-rotation", "--policy", "sev1",
                      "--hints", "3"]), 0)
        out = buf.getvalue()
        self.assertIn("key rotation", out)
        self.assertIn("14/100", out)


class TestRetryStormScenario(unittest.TestCase):
    """retry-storm: a retry storm rate-limits the push provider.

    The primary acks 1m late against the fast policy's 2m window, so
    this is the library's first built-in run exercising a late ack on
    the fast policy: no miss, no escalation, but the late ack keeps
    only half the score, and the chain records the first impact stage
    while the stages spell out the retry flood spreading to
    notifications fully down.
    """

    def test_retry_storm_scenario(self):
        scenario = get("retry-storm")
        self.assertEqual(scenario.severity, "critical")
        self.assertEqual(scenario.service, "notifications-push")
        self.assertEqual(scenario.ack_delay_min, (3,))
        self.assertEqual(scenario.slo_percent, 99.95)
        self.assertEqual(len(scenario.impact_stages), 3)
        self.assertEqual(len(scenario.hints), 3)

    def test_late_ack_on_fast_keeps_half_the_score(self):
        result = simulate(get("retry-storm"), get_policy("fast"))
        score = result.score
        # Fast timeouts 2m, 2m, 5m: the primary acks at 3m, 1m late
        # against the 2m window, with no escalation.
        self.assertEqual(score.time_to_ack_min, 3)
        self.assertEqual(score.time_to_mitigate_min, 28)
        self.assertEqual(score.time_to_resolve_min, 43)
        self.assertTrue(score.resolved)
        self.assertEqual(score.escalations, 0)
        self.assertEqual(score.ack_late_min, 1)
        self.assertAlmostEqual(score.ack_credit, 0.5)
        # 100 - 0 (no escalations) - 0 (3m to ack inside the 5m
        # grace) - 6 (28m to mitigate past the 15m grace, halved)
        # = 94, scaled by the 1/2 ack credit: round(94 / 2) = 47.
        self.assertEqual(score.points, 47)
        # Downtime 28m against a 99.95% budget of 21.6m.
        self.assertEqual(score.downtime_min, 28)
        self.assertEqual(score.error_budget_burn_pct, 129.6)
        chain = result.escalation_chain
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0].outcome, "acked")
        self.assertEqual(chain[0].paged_at_min, 0)
        self.assertEqual(chain[0].acked_at_min, 3)
        self.assertEqual(chain[0].ack_late_min, 1)
        self.assertEqual(
            chain[0].impact,
            "Retries flooding the provider: delivery attempts spiking, "
            "push latency climbing")

    def test_late_ack_is_on_time_on_the_standard_policy(self):
        # The same 3m ack is on time against the standard policy's
        # 5m primary timeout, so the full credit is kept there: the
        # lateness is a property of the fast policy, not the incident.
        result = simulate(get("retry-storm"), DEFAULT_POLICY)
        score = result.score
        self.assertEqual(score.ack_late_min, 0)
        self.assertAlmostEqual(score.ack_credit, 1.0)
        self.assertEqual(score.points, 94)

    def test_debrief_of_late_ack_names_the_lateness(self):
        result = simulate(get("retry-storm"), get_policy("fast"))
        text = generate_debrief(result).render()
        self.assertIn("ana", text)
        self.assertIn("1m late", text)
        self.assertIn("50% of the score", text)

    def test_template_round_trips_retry_storm(self):
        data = scenario_to_dict(get("retry-storm"))
        self.assertEqual(data["ack_delay_min"], [3])
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump({"scenarios": {"retry-storm": data}}, handle)
        loaded = load_scenario_file(handle.name)
        self.assertEqual(loaded["retry-storm"], get("retry-storm"))

    def test_drill_matches_run_for_same_decisions(self):
        drill = run_drill(
            get("retry-storm"), get_policy("fast"),
            input_stream=io.StringIO(
                "ack 3\nmitigate 25\nresolve 15\n"),
            prompt_stream=io.StringIO())
        run = simulate(get("retry-storm"), get_policy("fast"))
        self.assertEqual(drill.to_dict(), run.to_dict())
        self.assertEqual(drill.score.points, 47)

    def test_cli_runs_retry_storm_fast(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "retry-storm", "--policy", "fast"]),
                0)
        out = buf.getvalue()
        self.assertIn("47/100", out)

    def test_cli_runs_retry_storm_fast_with_hints(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["run", "retry-storm", "--policy", "fast",
                      "--hints", "3"]), 0)
        out = buf.getvalue()
        self.assertIn("retry loop", out)
        self.assertIn("32/100", out)


class TestErrorBudgetBurn(unittest.TestCase):
    def _write_scenario_file(self, data):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(data, handle)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_error_budget_minutes(self):
        # A 30-day month has 43,200 minutes; the budget is the allowed
        # share of it: 99.9% allows 43.2m, 99.99% allows 4.32m.
        self.assertAlmostEqual(error_budget_minutes(99.9), 43.2)
        self.assertAlmostEqual(error_budget_minutes(99.99), 4.32)
        self.assertAlmostEqual(error_budget_minutes(99.5), 216.0)

    def test_error_budget_burn_pct(self):
        self.assertEqual(error_budget_burn_pct(99.9, 43.2), 100.0)
        self.assertEqual(error_budget_burn_pct(99.9, 50), 115.7)
        self.assertIsNone(error_budget_burn_pct(None, 50))
        self.assertIsNone(error_budget_burn_pct(99.9, None))

    def test_every_builtin_scenario_has_an_slo(self):
        for name in list_scenarios():
            slo = get(name).slo_percent
            self.assertIsInstance(slo, float, name)
            self.assertTrue(90 <= slo < 100, name)

    def test_disk_full_burn(self):
        # disk-full: ack 3m, mitigate 23m, so 23m of downtime against
        # a 99.9% SLO (43.2m budget) burns 53.2%.
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        self.assertEqual(result.score.slo_percent, 99.9)
        self.assertEqual(result.score.downtime_min, 23)
        self.assertEqual(result.score.error_budget_burn_pct, 53.2)

    def test_payments_outage_burns_over_the_full_budget(self):
        # SEV-1 on the sev1 policy: ack 12m, mitigate 62m, so 62m of
        # downtime against a 99.9% SLO burns 143.5% — one incident
        # spends the whole month's budget.
        result = simulate(get("payments-outage"), get_policy("sev1"))
        self.assertEqual(result.score.downtime_min, 62)
        self.assertEqual(result.score.error_budget_burn_pct, 143.5)

    def test_scenario_without_slo_reports_no_burn(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(2, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        result = simulate(scenario, DEFAULT_POLICY)
        self.assertIsNone(result.score.slo_percent)
        self.assertEqual(result.score.downtime_min, 12)
        self.assertIsNone(result.score.error_budget_burn_pct)

    def test_unacked_run_has_no_downtime_or_burn(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        result = simulate(scenario, DEFAULT_POLICY)
        self.assertIsNone(result.score.downtime_min)
        self.assertIsNone(result.score.error_budget_burn_pct)

    def test_scenario_file_validates_slo(self):
        base = scenario_to_dict(get("disk-full"))
        for bad in (100, 89.9, "99.9", True, [99.9]):
            data = dict(base, slo_percent=bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                load_scenario_file(self._write_scenario_file(
                    {"scenarios": {"bad": data}}))
        # A valid SLO loads, and a missing one means no SLO.
        data = dict(base, slo_percent=99.95)
        loaded = load_scenario_file(self._write_scenario_file(
            {"scenarios": {"ok": data}}))
        self.assertEqual(loaded["ok"].slo_percent, 99.95)
        data = dict(base)
        del data["slo_percent"]
        loaded = load_scenario_file(self._write_scenario_file(
            {"scenarios": {"ok": data}}))
        self.assertIsNone(loaded["ok"].slo_percent)

    def test_template_round_trips_slo(self):
        data = scenario_to_dict(get("disk-full"))
        self.assertEqual(data["slo_percent"], 99.9)
        loaded = load_scenario_file(self._write_scenario_file(
            {"scenarios": {"disk-full": data}}))
        self.assertEqual(loaded["disk-full"].slo_percent, 99.9)

    def test_replay_round_trips_burn(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(result.to_dict(), handle)
        self.addCleanup(os.unlink, handle.name)
        loaded = load_result(handle.name)
        self.assertEqual(loaded.score.slo_percent, 99.9)
        self.assertEqual(loaded.score.downtime_min, 23)
        self.assertEqual(loaded.score.error_budget_burn_pct, 53.2)

    def test_replay_of_old_run_has_no_burn_fields(self):
        data = simulate(get("disk-full"), DEFAULT_POLICY).to_dict()
        for key in ("slo_percent", "downtime_min",
                    "error_budget_burn_pct"):
            del data["score"][key]
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            json.dump(data, handle)
        self.addCleanup(os.unlink, handle.name)
        loaded = load_result(handle.name)
        self.assertIsNone(loaded.score.slo_percent)
        self.assertIsNone(loaded.score.downtime_min)
        self.assertIsNone(loaded.score.error_budget_burn_pct)

    def test_run_prints_downtime_and_burn(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["run", "disk-full"]), 0)
        out = buf.getvalue()
        self.assertIn("downtime:         23m", out)
        self.assertIn("error budget:     burned 53.2% of the monthly "
                      "budget (SLO 99.9%)", out)

    def test_debrief_flags_burn_over_100_only(self):
        over = generate_debrief(
            simulate(get("payments-outage"), get_policy("sev1")))
        self.assertTrue(any("error budget" in item
                            for item in over.went_badly))
        self.assertTrue(any("error budget" in item["action"]
                            for item in over.action_items))
        self.assertIn("143.5%", over.render())
        # disk-full burns 53.2%: the budget held, so no finding.
        under = generate_debrief(simulate(get("disk-full"),
                                          DEFAULT_POLICY))
        self.assertFalse(any("error budget" in item
                             for item in under.went_badly))
        self.assertIn("53.2%", under.render())

    def test_compare_shows_burn_delta(self):
        a = simulate(get("disk-full"), DEFAULT_POLICY)
        b = simulate(get("latency-spike"), DEFAULT_POLICY)
        comparison = compare_results(a, b)
        rows = {row["metric"]: row for row in comparison.score_rows}
        self.assertEqual(rows["error_budget_burn_pct"]["a"], 53.2)
        self.assertEqual(rows["error_budget_burn_pct"]["b"], 203.7)
        self.assertAlmostEqual(
            rows["error_budget_burn_pct"]["delta"], 150.5)
        self.assertIn("error budget burn", comparison.render())
        self.assertIn("203.7%", comparison.render())

    def test_stats_mean_burn(self):
        runs = [simulate(get("disk-full"), DEFAULT_POLICY),
                simulate(get("latency-spike"), DEFAULT_POLICY)]
        stats = aggregate_stats(runs)
        self.assertAlmostEqual(
            stats.mean_error_budget_burn_pct, (53.2 + 203.7) / 2)
        self.assertEqual(stats.to_dict()["mean_error_budget_burn_pct"],
                         stats.mean_error_budget_burn_pct)
        self.assertIn("error budget burn",
                      stats.render())

    def test_drill_records_burn(self):
        drill = run_drill(
            get("disk-full"), DEFAULT_POLICY,
            input_stream=io.StringIO("ack 3\nmitigate 20\nresolve 12\n"),
            prompt_stream=io.StringIO())
        self.assertEqual(drill.score.downtime_min, 23)
        self.assertEqual(drill.score.error_budget_burn_pct, 53.2)

    def test_drill_abandoned_after_mitigate_still_reports_burn(self):
        drill = run_drill(
            get("disk-full"), DEFAULT_POLICY,
            input_stream=io.StringIO("ack 3\nmitigate 20\nquit\n"),
            prompt_stream=io.StringIO())
        self.assertFalse(drill.score.resolved)
        self.assertEqual(drill.score.downtime_min, 23)
        self.assertEqual(drill.score.error_budget_burn_pct, 53.2)

    def test_what_if_keeps_slo_and_recomputes_burn(self):
        # latency-spike: ack 9m, downtime 44m, burn 203.7% on a 99.95%
        # SLO. A 10m ack timeout waits out the primary's miss longer,
        # so the ack lands at 14m and the burn grows.
        original = simulate(get("latency-spike"), DEFAULT_POLICY)
        outcome = what_if(original, ack_timeout=10)
        self.assertEqual(outcome.replayed.score.slo_percent, 99.95)
        self.assertEqual(outcome.replayed.score.downtime_min, 49)
        self.assertEqual(outcome.replayed.score.error_budget_burn_pct,
                         226.9)
        self.assertIn("226.9%", outcome.render())

    def test_batch_runs_carry_burn(self):
        batch = simulate_batch(get("disk-full"), DEFAULT_POLICY,
                               seeds=[1, 2])
        for run in batch.runs:
            self.assertEqual(run.score.slo_percent, 99.9)
            self.assertIsNotNone(run.score.error_budget_burn_pct)


class TestHtmlPostmortem(unittest.TestCase):
    def _save_run(self, result):
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(result.to_dict(), path)
        self.addCleanup(os.unlink, path.name)
        return path.name

    def _postmortem_html(self, path, *extra_args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["postmortem", path, *extra_args]), 0)
        return buf.getvalue()

    def test_html_contains_key_facts(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        html_text = render_postmortem_html(result)
        self.assertIn("<!DOCTYPE html>", html_text)
        self.assertIn("Disk 97% full on payments-db", html_text)
        self.assertIn("payments-db", html_text)
        self.assertIn("96/100 (grade A)", html_text)
        self.assertIn("ana", html_text)
        self.assertIn("acked", html_text)
        self.assertIn("Time-to-ack", html_text)
        self.assertIn("3m", html_text)
        self.assertIn("Error budget", html_text)
        self.assertIn("53.2%", html_text)
        # Self-contained: inline CSS, no scripts, no external resources.
        self.assertIn("<style>", html_text)
        self.assertNotIn("<script", html_text)
        self.assertNotIn("http://", html_text)
        self.assertNotIn("https://", html_text)

    def test_html_lists_hints_used_with_cost(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY, hints=1)
        html_text = render_postmortem_html(result)
        self.assertIn("Check WAL archiving", html_text)
        self.assertIn("Total hint cost: 5 points", html_text)

    def test_html_without_hints_says_none_used(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        self.assertIn("No hints used.",
                      render_postmortem_html(result))

    def test_html_shows_responder_timings_from_chain(self):
        result = simulate(get("payments-outage"), get_policy("sev1"))
        html_text = render_postmortem_html(result)
        self.assertIn("Responder timings", html_text)
        self.assertIn("on-call", html_text)
        self.assertIn("incident commander", html_text)
        self.assertIn("missed", html_text)
        self.assertIn("Checkout down: all payments failing", html_text)
        self.assertIn("no ack within 3m", html_text)

    def test_html_marks_unacked_run(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        html_text = render_postmortem_html(simulate(scenario,
                                                    DEFAULT_POLICY))
        self.assertIn("nobody acked", html_text)
        self.assertIn("0/100 (grade D)", html_text)

    def test_html_escapes_scenario_markup(self):
        scenario = Scenario(
            name="xss", service="svc",
            title="Title with <b>bold</b> markup", severity="low",
            description="Detail with <script>alert(1)</script> inside.",
            ack_delay_min=(2, None, None),
            mitigate_delay_min=1, resolve_delay_min=1,
            hints=("Hint with <img src=x onerror=alert(1)> in it.",),
        )
        html_text = render_postmortem_html(
            simulate(scenario, DEFAULT_POLICY, hints=1))
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", html_text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_text)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html_text)
        self.assertNotIn("<b>bold</b>", html_text)
        self.assertNotIn("<script>alert(1)</script>", html_text)
        self.assertNotIn("<img src=x", html_text)

    def test_cli_prints_html_and_out_writes_file(self):
        path = self._save_run(simulate(get("disk-full"), DEFAULT_POLICY))
        html_text = self._postmortem_html(path)
        self.assertIn("Disk 97% full on payments-db", html_text)
        out_path = tempfile.NamedTemporaryFile(
            suffix=".html", delete=False)
        out_path.close()
        self.addCleanup(os.unlink, out_path.name)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["postmortem", path, "--out", out_path.name]), 0)
        self.assertIn("wrote postmortem", buf.getvalue())
        with open(out_path.name, encoding="utf-8") as handle:
            # print() adds one trailing newline to the stdout version.
            self.assertEqual(handle.read(), html_text.rstrip("\n") + "\n")

    def test_cli_what_if_file_carries_provenance(self):
        run_path = self._save_run(
            simulate(get("disk-full"), get_policy("fast")))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["what-if", run_path, "--ack-timeout", "5",
                      "--json"]), 0)
        whatif_path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with whatif_path:
            whatif_path.write(buf.getvalue())
        self.addCleanup(os.unlink, whatif_path.name)
        html_text = self._postmortem_html(whatif_path.name)
        self.assertIn("What-if replay", html_text)
        self.assertIn("ack timeout: every level 5m", html_text)
        self.assertIn("+48 points", html_text)
        self.assertIn("base run scored 48/100", html_text)

    def test_cli_missing_or_bad_file_fails(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["postmortem", "/no/such/run.json"]), 2)
        bad = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with bad:
            bad.write("{not json")
        self.addCleanup(os.unlink, bad.name)
        with redirect_stderr(err):
            self.assertEqual(main(["postmortem", bad.name]), 2)


class TestPdfPostmortem(unittest.TestCase):
    def _save_run(self, result):
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(result.to_dict(), path)
        self.addCleanup(os.unlink, path.name)
        return path.name

    def _pdf_text(self, data: bytes) -> str:
        """The readable text of a PDF: the literal strings of its
        uncompressed content streams, with PDF escapes undone,
        joined in order. This is how the tests read the PDF back."""
        raw = data.decode("latin-1")
        parts = []
        for match in re.finditer(r"\((?:\\.|[^\\()])*\)", raw):
            literal = match.group(0)[1:-1]
            parts.append(literal.replace("\\\\", "\\")
                                .replace("\\(", "(")
                                .replace("\\)", ")"))
        return "\n".join(parts)

    def _pdf_page_count(self, data: bytes) -> int:
        """Count page objects: \"/Type /Page\" that is not \"/Pages\"."""
        return len(re.findall(rb"/Type /Page(?![a-zA-Z])", data))

    def test_pdf_contains_key_facts(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        data = render_postmortem_pdf(result)
        self.assertTrue(data.startswith(b"%PDF-1.4\n"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        self.assertEqual(self._pdf_page_count(data), 1)
        text = self._pdf_text(data)
        self.assertIn("Postmortem: Disk 97% full on payments-db", text)
        self.assertIn("payments-db", text)
        self.assertIn("96/100 (grade A)", text)
        self.assertIn("ana", text)
        self.assertIn("acked", text)
        self.assertIn("Time-to-ack", text)
        self.assertIn("3m", text)
        self.assertIn("Error budget", text)
        self.assertIn("53.2%", text)
        self.assertIn("Generated by incident-lab 0.45.0", text)
        self.assertIn("Page 1 of 1", text)

    def test_pdf_lists_hints_used_with_cost(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY, hints=1)
        text = self._pdf_text(render_postmortem_pdf(result))
        self.assertIn("Check WAL archiving", text)
        self.assertIn("Total hint cost: 5 points", text)

    def test_pdf_without_hints_says_none_used(self):
        result = simulate(get("disk-full"), DEFAULT_POLICY)
        self.assertIn("No hints used.",
                      self._pdf_text(render_postmortem_pdf(result)))

    def test_pdf_shows_responder_timings_from_chain(self):
        result = simulate(get("payments-outage"), get_policy("sev1"))
        text = self._pdf_text(render_postmortem_pdf(result))
        self.assertIn("Responder timings", text)
        self.assertIn("on-call", text)
        self.assertIn("incident commander", text)
        self.assertIn("missed", text)
        self.assertIn("Checkout down", text)
        self.assertIn("missed after 3m", text)

    def test_pdf_marks_unacked_run(self):
        scenario = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
        )
        text = self._pdf_text(render_postmortem_pdf(
            simulate(scenario, DEFAULT_POLICY)))
        self.assertIn("nobody acked", text)
        self.assertIn("0/100 (grade D)", text)

    def test_pdf_escapes_parens_backslash_and_non_ascii(self):
        scenario = Scenario(
            name="pdf-esc", service="svc",
            title="Title (with parens) and a \\ backslash",
            severity="low",
            description="Detail \u2014 with an em dash \u2014 and "
                        "a caf\u00e9 note.",
            ack_delay_min=(2, None, None),
            mitigate_delay_min=1, resolve_delay_min=1,
            hints=("Hint (with parens) in it.",),
        )
        data = render_postmortem_pdf(
            simulate(scenario, DEFAULT_POLICY, hints=1))
        # The raw bytes carry the parens escaped, so the string never
        # breaks the PDF syntax.
        self.assertIn(b"\\(with parens\\)", data)
        self.assertIn(b"\\\\ backslash", data)
        text = self._pdf_text(data)
        self.assertIn("Title (with parens) and a \\ backslash", text)
        self.assertIn("Hint (with parens) in it.", text)
        # The em dash folds to a plain hyphen; latin-1 text survives.
        self.assertIn("Detail - with an em dash - and a caf\u00e9 note.",
                      text)

    def test_pdf_spans_multiple_pages(self):
        scenario = Scenario(
            name="long", service="svc", title="long incident",
            severity="low",
            description="Detail. " * 800,
            ack_delay_min=(2, None, None),
            mitigate_delay_min=1, resolve_delay_min=1,
        )
        data = render_postmortem_pdf(
            simulate(scenario, DEFAULT_POLICY))
        pages = self._pdf_page_count(data)
        self.assertGreaterEqual(pages, 2)
        text = self._pdf_text(data)
        self.assertIn("Postmortem: long incident", text)
        self.assertIn(f"Page 2 of {pages}", text)
        self.assertIn(f"Page {pages} of {pages}", text)
        self.assertIn("100/100 (grade A)", text)

    def test_pdf_what_if_carries_provenance(self):
        run_path = self._save_run(
            simulate(get("disk-full"), get_policy("fast")))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["what-if", run_path, "--ack-timeout", "5",
                      "--json"]), 0)
        whatif_path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with whatif_path:
            whatif_path.write(buf.getvalue())
        self.addCleanup(os.unlink, whatif_path.name)
        loaded = load_saved(whatif_path.name)
        text = self._pdf_text(render_postmortem_pdf(
            loaded.result, what_if=loaded.what_if))
        self.assertIn("What-if replay", text)
        self.assertIn("ack timeout: every level 5m", text)
        self.assertIn("+48 points", text)
        self.assertIn("base run scored 48/100", text)

    def test_cli_pdf_writes_file_and_requires_out(self):
        path = self._save_run(simulate(get("disk-full"), DEFAULT_POLICY))
        out_path = tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False)
        out_path.close()
        self.addCleanup(os.unlink, out_path.name)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["postmortem", path, "--format", "pdf",
                      "--out", out_path.name]), 0)
        self.assertIn("wrote PDF postmortem", buf.getvalue())
        with open(out_path.name, "rb") as handle:
            data = handle.read()
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertEqual(self._pdf_page_count(data), 1)
        self.assertIn("Disk 97% full on payments-db",
                      self._pdf_text(data))

    def test_cli_pdf_without_out_fails(self):
        path = self._save_run(simulate(get("disk-full"), DEFAULT_POLICY))
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(
                main(["postmortem", path, "--format", "pdf"]), 2)
        self.assertIn("needs --out", err.getvalue())

    def test_cli_pdf_unknown_format_fails(self):
        path = self._save_run(simulate(get("disk-full"), DEFAULT_POLICY))
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            main(["postmortem", path, "--format", "docx"])

    def test_cli_pdf_missing_or_bad_file_fails(self):
        err = io.StringIO()
        out_path = tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False)
        out_path.close()
        self.addCleanup(os.unlink, out_path.name)
        with redirect_stderr(err):
            self.assertEqual(
                main(["postmortem", "/no/such/run.json", "--format",
                      "pdf", "--out", out_path.name]), 2)
        # Nothing was written to the output path.
        self.assertEqual(os.path.getsize(out_path.name), 0)


class TestBundle(unittest.TestCase):
    def _save_run(self, result):
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(result.to_dict(), path)
        self.addCleanup(os.unlink, path.name)
        return path.name

    def _save_whatif(self, run_path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["what-if", run_path, "--ack-timeout", "5",
                      "--json"]), 0)
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            path.write(buf.getvalue())
        self.addCleanup(os.unlink, path.name)
        return path.name

    def _bundle_path(self):
        path = tempfile.NamedTemporaryFile(
            suffix=".zip", delete=False)
        path.close()
        os.unlink(path.name)
        self.addCleanup(
            lambda: os.path.exists(path.name) and os.unlink(path.name))
        return path.name

    def _tamper(self, bundle_path, member, new_data):
        """Rewrite a bundle with one member's bytes replaced."""
        with zipfile.ZipFile(bundle_path) as bundle:
            members = {name: bundle.read(name)
                       for name in bundle.namelist()}
        members[member] = new_data
        tampered = tempfile.NamedTemporaryFile(
            suffix=".zip", delete=False)
        tampered.close()
        self.addCleanup(os.unlink, tampered.name)
        with zipfile.ZipFile(tampered.name, "w",
                             compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, data in members.items():
                bundle.writestr(name, data)
        return tampered.name

    def test_bundle_contains_run_html_and_manifest(self):
        run_path = self._save_run(
            simulate(get("disk-full"), DEFAULT_POLICY))
        out = self._bundle_path()
        manifest = create_bundle(run_path, out)
        self.assertEqual(manifest["format"], "incident-lab-bundle")
        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["generator"],
                         "incident-lab 0.45.0")
        self.assertEqual(manifest["incident"]["id"], "disk-full")
        self.assertEqual(manifest["score"]["points"], 96)
        self.assertEqual(manifest["score"]["grade"], "A")
        self.assertIsNone(manifest["what_if"])
        with zipfile.ZipFile(out) as bundle:
            self.assertEqual(sorted(bundle.namelist()),
                             ["manifest.json", "postmortem.html",
                              "run.json"])
            # The stored run is the saved file verbatim.
            with open(run_path, "rb") as handle:
                self.assertEqual(bundle.read("run.json"),
                                 handle.read())
            html_text = bundle.read("postmortem.html").decode("utf-8")
            self.assertIn("<!DOCTYPE html>", html_text)
            self.assertIn("Disk 97% full on payments-db", html_text)
            self.assertIn("96/100 (grade A)", html_text)
            stored_manifest = json.loads(
                bundle.read("manifest.json").decode("utf-8"))
            self.assertEqual(stored_manifest, manifest)

    def test_bundled_run_reimports(self):
        run_path = self._save_run(
            simulate(get("disk-full"), DEFAULT_POLICY))
        out = self._bundle_path()
        create_bundle(run_path, out)
        with zipfile.ZipFile(out) as bundle:
            data = bundle.read("run.json")
        extracted = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".json", delete=False)
        with extracted:
            extracted.write(data)
        self.addCleanup(os.unlink, extracted.name)
        self.assertEqual(load_result(extracted.name).score.points, 96)

    def test_bundle_what_if_carries_base_run_and_provenance(self):
        run_path = self._save_run(
            simulate(get("disk-full"), get_policy("fast")))
        whatif_path = self._save_whatif(run_path)
        out = self._bundle_path()
        manifest = create_bundle(whatif_path, out)
        self.assertIsNotNone(manifest["what_if"])
        self.assertEqual(manifest["what_if"]["points_delta"], 48)
        self.assertEqual(manifest["what_if"]["base_points"], 48)
        self.assertIn("ack timeout: every level 5m",
                      manifest["what_if"]["overrides"])
        with zipfile.ZipFile(out) as bundle:
            self.assertEqual(sorted(bundle.namelist()),
                             ["base-run.json", "manifest.json",
                              "postmortem.html", "run.json"])
            replayed = json.loads(
                bundle.read("run.json").decode("utf-8"))
            base = json.loads(
                bundle.read("base-run.json").decode("utf-8"))
            self.assertEqual(replayed["score"]["points"], 96)
            self.assertEqual(base["score"]["points"], 48)
            html_text = bundle.read("postmortem.html").decode("utf-8")
            self.assertIn("What-if replay", html_text)

    def test_validation_catches_tampered_run(self):
        run_path = self._save_run(
            simulate(get("disk-full"), DEFAULT_POLICY))
        out = self._bundle_path()
        create_bundle(run_path, out)
        with zipfile.ZipFile(out) as bundle:
            run_data = json.loads(bundle.read("run.json").decode("utf-8"))
        run_data["score"]["points"] = 100
        tampered = self._tamper(
            out, "run.json",
            (json.dumps(run_data, indent=2) + "\n").encode("utf-8"))
        with self.assertRaises(ValueError):
            validate_bundle(tampered)

    def test_validation_catches_missing_member(self):
        run_path = self._save_run(
            simulate(get("disk-full"), DEFAULT_POLICY))
        out = self._bundle_path()
        create_bundle(run_path, out)
        with zipfile.ZipFile(out) as bundle:
            members = {name: bundle.read(name)
                       for name in bundle.namelist()
                       if name != "postmortem.html"}
        trimmed = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        trimmed.close()
        self.addCleanup(os.unlink, trimmed.name)
        with zipfile.ZipFile(trimmed.name, "w",
                             compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, data in members.items():
                bundle.writestr(name, data)
        with self.assertRaises(ValueError):
            validate_bundle(trimmed)

    def test_validation_rejects_non_bundle_zip(self):
        path = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        path.close()
        self.addCleanup(os.unlink, path.name)
        with zipfile.ZipFile(path.name, "w") as bundle:
            bundle.writestr("hello.txt", "hello")
        with self.assertRaises(ValueError):
            validate_bundle(path.name)

    def test_create_bundle_rejects_invalid_run_and_leaves_no_file(self):
        bad = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with bad:
            bad.write('{"not": "a run"}')
        self.addCleanup(os.unlink, bad.name)
        out = self._bundle_path()
        with self.assertRaises(ValueError):
            create_bundle(bad.name, out)
        self.assertFalse(os.path.exists(out))

    def test_default_bundle_path(self):
        self.assertEqual(default_bundle_path("/tmp/latency-spike.json"),
                         "latency-spike-bundle.zip")
        self.assertEqual(default_bundle_path("disk-full.json"),
                         "disk-full-bundle.zip")

    def test_cli_bundle_and_check(self):
        run_path = self._save_run(
            simulate(get("disk-full"), DEFAULT_POLICY))
        out = self._bundle_path()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["bundle", run_path, "--out", out]), 0)
        self.assertIn("wrote bundle", buf.getvalue())
        self.assertIn("Disk 97% full on payments-db", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["bundle", "--check", out]), 0)
        self.assertIn("bundle OK", buf.getvalue())
        self.assertIn("96/100 (grade A)", buf.getvalue())

    def test_cli_bundle_missing_or_bad_file_fails(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["bundle", "/no/such/run.json"]), 2)
        bad = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with bad:
            bad.write("{not json")
        self.addCleanup(os.unlink, bad.name)
        with redirect_stderr(err):
            self.assertEqual(main(["bundle", bad.name]), 2)

    def test_cli_check_tampered_bundle_fails(self):
        run_path = self._save_run(
            simulate(get("disk-full"), DEFAULT_POLICY))
        out = self._bundle_path()
        create_bundle(run_path, out)
        with zipfile.ZipFile(out) as bundle:
            run_data = json.loads(bundle.read("run.json").decode("utf-8"))
        run_data["score"]["points"] = 100
        tampered = self._tamper(
            out, "run.json",
            (json.dumps(run_data, indent=2) + "\n").encode("utf-8"))
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["bundle", "--check", tampered]), 2)
        self.assertIn("invalid bundle", err.getvalue())
class TestShift(unittest.TestCase):
    def _save_shift(self, shift):
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(shift.to_dict(), path)
        self.addCleanup(os.unlink, path.name)
        return path.name

    def _save_run(self, result):
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(result.to_dict(), path)
        self.addCleanup(os.unlink, path.name)
        return path.name

    def test_burn_accumulates_across_incidents(self):
        # disk-full: 23m downtime, latency-spike: 44m. Against a
        # 99.5% SLO (216m monthly budget) that is 10.6% then 20.4%,
        # 31.0% in total, and the budget holds.
        shift = simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.5)
        self.assertEqual(shift.slo_percent, 99.5)
        self.assertEqual(shift.budget_minutes, 216.0)
        self.assertFalse(shift.budget_exhausted)
        self.assertEqual(shift.skipped, [])
        self.assertEqual(shift.total_burn_pct, 31.0)
        self.assertEqual(shift.shift_points, 81.5)
        self.assertEqual(
            [(entry["scenario"], entry["points"],
              entry["downtime_min"], entry["burn_pct"],
              entry["cumulative_burn_pct"])
             for entry in shift.incidents],
            [("disk-full", 96, 23, 10.6, 10.6),
             ("latency-spike", 67, 44, 20.4, 31.0)])
        # Each incident is still an ordinary run: same points as
        # `run` would give it on its own.
        self.assertEqual(
            [run.score.points for run in shift.runs], [96, 67])
        self.assertEqual(
            [run.incident.id for run in shift.runs],
            ["disk-full", "latency-spike"])

    def test_default_slo_comes_from_first_scenario(self):
        # disk-full carries 99.9%: 43.2m monthly budget.
        shift = simulate_shift(
            [get("disk-full"), get("cert-expiry")], DEFAULT_POLICY)
        self.assertEqual(shift.slo_percent, 99.9)
        self.assertEqual(shift.budget_minutes, 43.2)
        # 23m burns 53.2%, cert-expiry's 10m burns 23.1%: 76.4%.
        self.assertEqual(shift.total_burn_pct, 76.4)
        self.assertFalse(shift.budget_exhausted)

    def test_shift_ends_early_when_budget_exhausted(self):
        # Against the default 99.9% SLO, disk-full burns 53.2% and
        # latency-spike burns 101.9%: 155.1% in total, so the budget
        # is spent after the second incident and cert-expiry never
        # runs — it scores 0 in the shift mean.
        shift = simulate_shift(
            [get("disk-full"), get("latency-spike"),
             get("cert-expiry")],
            DEFAULT_POLICY)
        self.assertTrue(shift.budget_exhausted)
        self.assertEqual([run.incident.id for run in shift.runs],
                         ["disk-full", "latency-spike"])
        self.assertEqual(shift.skipped, ["cert-expiry"])
        self.assertEqual(shift.total_burn_pct, 155.1)
        self.assertEqual(shift.shift_points, 54.3)
        self.assertEqual(shift.scheduled,
                         ["disk-full", "latency-spike", "cert-expiry"])

    def test_unacked_incident_burns_nothing(self):
        ghost = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(None, None, None),
            mitigate_delay_min=10, resolve_delay_min=10,
            slo_percent=99.9)
        shift = simulate_shift(
            [ghost, get("disk-full")], DEFAULT_POLICY,
            slo_percent=99.9)
        self.assertEqual(
            [(entry["scenario"], entry["burn_pct"],
              entry["downtime_min"]) for entry in shift.incidents],
            [("ghost", 0.0, None), ("disk-full", 53.2, 23)])
        self.assertEqual(shift.total_burn_pct, 53.2)
        self.assertFalse(shift.budget_exhausted)
        # The ghost scores 0 and drags the shift mean down.
        self.assertEqual(shift.shift_points, 48.0)

    def test_shift_needs_at_least_one_scenario(self):
        with self.assertRaises(ValueError):
            simulate_shift([], DEFAULT_POLICY, slo_percent=99.9)

    def test_shift_needs_an_slo(self):
        ghost = Scenario(
            name="ghost", service="svc", title="ghost", severity="high",
            description="", ack_delay_min=(2, None, None),
            mitigate_delay_min=10, resolve_delay_min=10)
        with self.assertRaises(ValueError):
            simulate_shift([ghost], DEFAULT_POLICY)

    def test_shift_rejects_bad_slo(self):
        with self.assertRaises(ValueError):
            simulate_shift([get("disk-full")], DEFAULT_POLICY,
                           slo_percent=50)
        with self.assertRaises(ValueError):
            simulate_shift([get("disk-full")], DEFAULT_POLICY,
                           slo_percent=100)

    def test_shift_seeded_reproducible_with_per_incident_seeds(self):
        first = simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.5, seed=7)
        second = simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.5, seed=7)
        self.assertEqual(first.to_dict(), second.to_dict())
        # Incident i uses seed + i, so the runs are reproducible
        # and still vary between incidents.
        self.assertEqual([run.seed for run in first.runs], [7, 8])

    def test_shift_json_round_trips(self):
        shift = simulate_shift(
            [get("disk-full"), get("latency-spike"),
             get("cert-expiry")],
            DEFAULT_POLICY)
        path = self._save_shift(shift)
        loaded = load_shift(path)
        self.assertEqual(loaded.to_dict(), shift.to_dict())
        self.assertTrue(loaded.budget_exhausted)
        self.assertEqual(loaded.skipped, ["cert-expiry"])
        kind, loaded_any = load_shift_or_result(path)
        self.assertEqual(kind, "shift")
        self.assertEqual(loaded_any.to_dict(), shift.to_dict())

    def test_shift_json_tampered_total_rejected(self):
        shift = simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.5)
        data = shift.to_dict()
        data["shift"]["total_burn_pct"] = 1.0
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(data, path)
        self.addCleanup(os.unlink, path.name)
        with self.assertRaises(ValueError):
            load_shift(path.name)

    def test_load_shift_or_result_distinguishes_run_and_shift(self):
        run_path = self._save_run(
            simulate(get("disk-full"), DEFAULT_POLICY))
        kind, result = load_shift_or_result(run_path)
        self.assertEqual(kind, "run")
        self.assertEqual(result.incident.id, "disk-full")
        shift_path = self._save_shift(simulate_shift(
            [get("disk-full")], DEFAULT_POLICY, slo_percent=99.5))
        kind, shift = load_shift_or_result(shift_path)
        self.assertEqual(kind, "shift")
        self.assertEqual(shift.shift_points, 96.0)

    def test_shift_debrief_flags_exhausted_budget(self):
        shift = simulate_shift(
            [get("disk-full"), get("latency-spike"),
             get("cert-expiry")],
            DEFAULT_POLICY)
        debrief = generate_shift_debrief(shift)
        self.assertEqual(debrief.shift_points, 54.3)
        self.assertEqual(debrief.grade, "C")
        self.assertTrue(debrief.budget_exhausted)
        self.assertEqual(debrief.skipped, ["cert-expiry"])
        self.assertEqual(
            [(entry["scenario"], entry["points"], entry["grade"])
             for entry in debrief.incidents],
            [("disk-full", 96, "A"), ("latency-spike", 67, "C")])
        text = debrief.render()
        self.assertIn("# Shift debrief", text)
        self.assertIn("budget ran out", text)
        self.assertIn("cert-expiry never ran", text)
        data = debrief.to_dict()
        self.assertEqual(data["shift"]["total_burn_pct"], 155.1)
        self.assertEqual(data["incidents"][1]["burn_pct"], 101.9)

    def test_shift_debrief_when_budget_holds(self):
        shift = simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.5)
        debrief = generate_shift_debrief(shift)
        self.assertFalse(debrief.budget_exhausted)
        self.assertIn("budget held", debrief.render())
        self.assertEqual(debrief.action_items, [])

    def test_compare_shifts(self):
        held = simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.5)
        spent = simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.9)
        comparison = compare_shifts(held, spent, label_a="A",
                                    label_b="B")
        self.assertEqual(comparison.shift_points_a, 81.5)
        self.assertEqual(comparison.shift_points_b, 81.5)
        self.assertEqual(comparison.total_burn_a, 31.0)
        self.assertEqual(comparison.total_burn_b, 155.1)
        self.assertTrue(comparison.exhausted_b)
        self.assertFalse(comparison.exhausted_a)
        self.assertEqual(
            comparison.per_incident,
            [{"scenario": "disk-full", "a": 96, "b": 96, "delta": 0},
             {"scenario": "latency-spike", "a": 67, "b": 67,
              "delta": 0}])
        data = comparison.to_dict()
        self.assertEqual(data["shift_points_delta"], 0.0)
        self.assertEqual(data["total_burn_delta"], 124.1)
        self.assertFalse(data["same_slo"])
        self.assertIn("different shift SLOs", comparison.render())

    def test_cli_shift_prints_summary(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["shift", "disk-full", "latency-spike",
                      "--slo", "99.5"]), 0)
        out = buf.getvalue()
        self.assertIn("shift: 2 incident(s) ran, 0 skipped", out)
        self.assertIn("shift points: 81.5/100", out)
        self.assertIn("burned 31.0% of the monthly budget", out)

    def test_cli_shift_exhausted_skips_rest(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["shift", "disk-full", "latency-spike",
                      "cert-expiry"]), 0)
        out = buf.getvalue()
        self.assertIn("shift: 2 incident(s) ran, 1 skipped", out)
        self.assertIn("skipped (error budget exhausted): cert-expiry",
                      out)
        self.assertIn("error budget: EXHAUSTED", out)
        self.assertIn("shift points: 54.3/100", out)

    def test_cli_shift_json_records_shift_and_burn(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["shift", "disk-full", "latency-spike",
                      "--slo", "99.5", "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["shift"]["slo_percent"], 99.5)
        self.assertEqual(data["shift"]["budget_minutes"], 216.0)
        self.assertEqual(data["shift"]["total_burn_pct"], 31.0)
        self.assertEqual(data["shift"]["shift_points"], 81.5)
        self.assertEqual(data["shift"]["skipped"], [])
        self.assertEqual(
            [entry["cumulative_burn_pct"]
             for entry in data["shift"]["incidents"]],
            [10.6, 31.0])
        self.assertEqual(len(data["runs"]), 2)
        self.assertEqual(data["runs"][0]["incident"]["id"], "disk-full")

    def test_cli_shift_unknown_scenario_and_bad_slo_fail(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["shift", "no-such-scenario"]), 2)
        self.assertIn("unknown scenario", err.getvalue())
        with redirect_stderr(err):
            self.assertEqual(
                main(["shift", "disk-full", "--slo", "50"]), 2)
        self.assertIn("--slo", err.getvalue())

    def test_cli_shift_seed_reproducible(self):
        first = io.StringIO()
        with redirect_stdout(first):
            self.assertEqual(
                main(["shift", "disk-full", "latency-spike",
                      "--slo", "99.5", "--seed", "7", "--json"]), 0)
        second = io.StringIO()
        with redirect_stdout(second):
            self.assertEqual(
                main(["shift", "disk-full", "latency-spike",
                      "--slo", "99.5", "--seed", "7", "--json"]), 0)
        self.assertEqual(first.getvalue(), second.getvalue())
        data = json.loads(first.getvalue())
        self.assertEqual(
            [run["seed"] for run in data["runs"]], [7, 8])

    def test_cli_replay_and_debrief_shift(self):
        shift_path = self._save_shift(simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.5))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["replay", shift_path]), 0)
        self.assertIn("shift points: 81.5/100", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["debrief", shift_path]), 0)
        self.assertIn("# Shift debrief", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["debrief", shift_path, "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["shift"]["shift_points"], 81.5)
        self.assertEqual(data["incidents"][0]["scenario"], "disk-full")

    def test_cli_compare_shifts(self):
        held_path = self._save_shift(simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.5))
        spent_path = self._save_shift(simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.9))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["compare", held_path, spent_path]), 0)
        out = buf.getvalue()
        self.assertIn("shift points", out)
        self.assertIn("total burn", out)
        self.assertIn("budget exhausted", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["compare", held_path, spent_path, "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["total_burn_delta"], 124.1)
        self.assertEqual(len(data["per_incident"]), 2)

    def test_cli_compare_shift_with_run_fails(self):
        shift_path = self._save_shift(simulate_shift(
            [get("disk-full")], DEFAULT_POLICY, slo_percent=99.5))
        run_path = self._save_run(
            simulate(get("disk-full"), DEFAULT_POLICY))
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(
                main(["compare", shift_path, run_path]), 2)
        self.assertIn("cannot compare a shift with a single run",
                      err.getvalue())

    def test_cli_stats_shift_counts_each_incident(self):
        shift_path = self._save_shift(simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.5))
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["stats", shift_path]), 0)
        out = buf.getvalue()
        self.assertIn("runs: 2", out)
        self.assertIn("shift files: 1", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["stats", shift_path, "--json"]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["shift_files"], 1)
        self.assertEqual(
            [row["scenario"] for row in data["runs"]],
            ["disk-full", "latency-spike"])


class TestShiftPostmortem(unittest.TestCase):
    def _save_shift(self, shift):
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(shift.to_dict(), path)
        self.addCleanup(os.unlink, path.name)
        return path.name

    def _shift(self, exhausted=False):
        if exhausted:
            return simulate_shift(
                [get("disk-full"), get("latency-spike"),
                 get("cert-expiry")],
                DEFAULT_POLICY)
        return simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.5)

    def test_shift_html_contains_key_facts(self):
        html_text = render_shift_postmortem_html(self._shift())
        self.assertIn("<!DOCTYPE html>", html_text)
        self.assertIn("Shift postmortem", html_text)
        self.assertIn("81.5/100 (grade B)", html_text)
        self.assertIn("disk-full", html_text)
        self.assertIn("latency-spike", html_text)
        self.assertIn("31.0%", html_text)
        self.assertIn("SLO 99.5%", html_text)
        # Per-incident sections carry each incident's own write-up.
        self.assertIn("Disk 97% full on payments-db", html_text)
        self.assertIn("p99 latency over 2s on checkout-api", html_text)
        # Self-contained: inline CSS, no scripts, no external resources.
        self.assertIn("<style>", html_text)
        self.assertNotIn("<script", html_text)
        self.assertNotIn("http://", html_text)
        self.assertNotIn("https://", html_text)

    def test_shift_html_marks_skipped_incidents(self):
        html_text = render_shift_postmortem_html(
            self._shift(exhausted=True))
        self.assertIn("54.3/100", html_text)
        self.assertIn("155.1%", html_text)
        self.assertIn("cert-expiry", html_text)
        self.assertIn("skipped (0/100)", html_text)
        self.assertIn("never ran", html_text)

    def test_shift_html_escapes_scenario_markup(self):
        ghost = Scenario(
            name="xss", service="svc",
            title="Title with <b>bold</b> markup", severity="low",
            description="", ack_delay_min=(2, None, None),
            mitigate_delay_min=1, resolve_delay_min=1,
            slo_percent=99.9)
        shift = simulate_shift([ghost], DEFAULT_POLICY,
                               slo_percent=99.9)
        html_text = render_shift_postmortem_html(shift)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", html_text)
        self.assertNotIn("<b>bold</b>", html_text)

    def test_shift_pdf_is_readable_and_names_incidents(self):
        data = render_shift_postmortem_pdf(self._shift())
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        text = data.decode("latin-1")
        self.assertIn("Shift postmortem", text)
        self.assertIn("disk-full", text)
        self.assertIn("latency-spike", text)
        self.assertIn("81.5/100", text)
        self.assertIn("Page 1 of ", text)

    def test_cli_postmortem_shift_prints_html_and_writes_files(self):
        path = self._save_shift(self._shift())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["postmortem", path]), 0)
        self.assertIn("Shift postmortem", buf.getvalue())
        self.assertIn("81.5/100 (grade B)", buf.getvalue())
        out_path = tempfile.NamedTemporaryFile(
            suffix=".html", delete=False)
        out_path.close()
        self.addCleanup(os.unlink, out_path.name)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["postmortem", path, "--out", out_path.name]), 0)
        self.assertIn("wrote postmortem for shift postmortem",
                      buf.getvalue())
        with open(out_path.name, encoding="utf-8") as handle:
            self.assertIn("Shift postmortem", handle.read())
        pdf_path = tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False)
        pdf_path.close()
        self.addCleanup(os.unlink, pdf_path.name)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["postmortem", path, "--format", "pdf",
                      "--out", pdf_path.name]), 0)
        self.assertIn("wrote PDF postmortem for shift postmortem",
                      buf.getvalue())
        with open(pdf_path.name, "rb") as handle:
            self.assertTrue(handle.read().startswith(b"%PDF-1.4"))

    def test_cli_shift_pdf_without_out_fails(self):
        path = self._save_shift(self._shift())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(
                main(["postmortem", path, "--format", "pdf"]), 2)
        self.assertIn("--format pdf needs --out", err.getvalue())


class TestShiftBundle(unittest.TestCase):
    def _save_shift(self, shift):
        path = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        with path:
            json.dump(shift.to_dict(), path)
        self.addCleanup(os.unlink, path.name)
        return path.name

    def _bundle_path(self):
        path = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        path.close()
        self.addCleanup(os.unlink, path.name)
        return path.name

    def _shift(self):
        return simulate_shift(
            [get("disk-full"), get("latency-spike")], DEFAULT_POLICY,
            slo_percent=99.5)

    def test_shift_bundle_contains_shift_html_and_manifest(self):
        shift_path = self._save_shift(self._shift())
        out = self._bundle_path()
        manifest = create_bundle(shift_path, out)
        self.assertEqual(manifest["format"], "incident-lab-bundle")
        self.assertEqual(manifest["generator"], "incident-lab 0.45.0")
        self.assertEqual(manifest["shift"]["shift_points"], 81.5)
        self.assertEqual(manifest["shift"]["grade"], "B")
        self.assertEqual(manifest["shift"]["total_burn_pct"], 31.0)
        self.assertFalse(manifest["shift"]["budget_exhausted"])
        self.assertEqual(manifest["shift"]["incidents"],
                         ["disk-full", "latency-spike"])
        self.assertEqual(manifest["shift"]["skipped"], [])
        self.assertIsNone(manifest["what_if"])
        with zipfile.ZipFile(out) as bundle:
            self.assertEqual(sorted(bundle.namelist()),
                             ["manifest.json", "postmortem.html",
                              "run.json"])
            # The stored shift is the saved file verbatim.
            with open(shift_path, "rb") as handle:
                self.assertEqual(bundle.read("run.json"),
                                 handle.read())
            html_text = bundle.read("postmortem.html").decode("utf-8")
            self.assertIn("<!DOCTYPE html>", html_text)
            self.assertIn("Shift postmortem", html_text)
            self.assertIn("81.5/100 (grade B)", html_text)

    def test_bundled_shift_reimports(self):
        shift_path = self._save_shift(self._shift())
        out = self._bundle_path()
        create_bundle(shift_path, out)
        with zipfile.ZipFile(out) as bundle:
            data = bundle.read("run.json")
        extracted = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".json", delete=False)
        with extracted:
            extracted.write(data)
        self.addCleanup(os.unlink, extracted.name)
        loaded = load_shift(extracted.name)
        self.assertEqual(loaded.shift_points, 81.5)
        self.assertEqual(loaded.total_burn_pct, 31.0)

    def test_shift_bundle_validation_catches_tampered_shift(self):
        shift_path = self._save_shift(self._shift())
        out = self._bundle_path()
        create_bundle(shift_path, out)
        with zipfile.ZipFile(out) as bundle:
            shift_data = json.loads(
                bundle.read("run.json").decode("utf-8"))
        shift_data["shift"]["total_burn_pct"] = 1.0
        tampered = tempfile.NamedTemporaryFile(
            suffix=".zip", delete=False)
        tampered.close()
        self.addCleanup(os.unlink, tampered.name)
        with zipfile.ZipFile(out) as bundle:
            members = {name: bundle.read(name)
                       for name in bundle.namelist()}
        members["run.json"] = (
            json.dumps(shift_data, indent=2) + "\n").encode("utf-8")
        with zipfile.ZipFile(tampered.name, "w",
                             compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, data in members.items():
                bundle.writestr(name, data)
        with self.assertRaises(ValueError):
            validate_bundle(tampered.name)

    def test_cli_bundle_and_check_shift(self):
        shift_path = self._save_shift(self._shift())
        out = self._bundle_path()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                main(["bundle", shift_path, "--out", out]), 0)
        self.assertIn("wrote bundle for shift of 2 incident(s)",
                      buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["bundle", "--check", out]), 0)
        self.assertIn("shift: 2 incident(s) ran", buf.getvalue())
        self.assertIn("81.5/100 (grade B)", buf.getvalue())
        self.assertIn(f"bundle OK: {out}", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
