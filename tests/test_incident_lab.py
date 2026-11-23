"""Tests for incident-lab."""

import io
import json
import os
import statistics
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace

from incident_lab.cli import main, parse_jitter_range, parse_seed_spec
from incident_lab.debrief import generate_debrief, grade
from incident_lab.policy import (
    DEFAULT_POLICY, get_policy, list_policies, load_policy_file,
    parse_ack_timeout_overrides,
)
from incident_lab.replay import load_result
from incident_lab.scenarios import Scenario, get, list_scenarios
from incident_lab.simulator import simulate, simulate_batch
from incident_lab.timeline import Timeline


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
    def test_registry_lists_both_policies(self):
        self.assertEqual(list_policies(), ["standard", "fast"])
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
        self.assertEqual(len(data["action_items"]), 2)
        self.assertEqual(data["action_items"][0]["owner"], "ana")

    def test_cli_debrief_missing_file_fails(self):
        self.assertEqual(main(["debrief", "/tmp/no-such-run.json"]), 2)


if __name__ == "__main__":
    unittest.main()
