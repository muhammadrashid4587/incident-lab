"""Tests for incident-lab."""

import io
import unittest
from contextlib import redirect_stdout
from dataclasses import replace

from incident_lab.cli import main
from incident_lab.policy import DEFAULT_POLICY
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


if __name__ == "__main__":
    unittest.main()
