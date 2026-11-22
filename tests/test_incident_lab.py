"""Tests for incident-lab."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace

from incident_lab.cli import main
from incident_lab.policy import (
    DEFAULT_POLICY, get_policy, list_policies, load_policy_file,
)
from incident_lab.scenarios import Scenario, get, list_scenarios
from incident_lab.simulator import simulate
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

    def test_fast_policy_escalates_past_slow_primary(self):
        # disk-full: primary takes 3m, past the fast 2m timeout; nobody
        # else acks, so the incident goes unacked and scores 0.
        result = simulate(get("disk-full"), get_policy("fast"))
        self.assertEqual(result.score.escalations, 2)
        self.assertFalse(result.score.resolved)
        self.assertEqual(result.score.points, 0)


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
        score = simulate(get("disk-full"), DEFAULT_POLICY).score.to_dict()
        self.assertEqual(score, {
            "time_to_ack_min": 3,
            "time_to_mitigate_min": 23,
            "time_to_resolve_min": 35,
            "escalations": 0,
            "resolved": True,
            "points": 96,
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


if __name__ == "__main__":
    unittest.main()
