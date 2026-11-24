"""Run every built-in scenario and print a one-line summary each."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from incident_lab.policy import DEFAULT_POLICY
from incident_lab.scenarios import get, list_scenarios
from incident_lab.simulator import simulate


def main() -> None:
    for name in list_scenarios():
        result = simulate(get(name), DEFAULT_POLICY)
        score = result.score
        print(
            f"{name:<20} ack={score.time_to_ack_min}m "
            f"mitigate={score.time_to_mitigate_min}m "
            f"escalations={score.escalations} "
            f"resolved={score.resolved} points={score.points}/100"
        )
    # The purge-verification what-if dimension, shown on the
    # scenario it was built for: verifying the purge costs more
    # the farther the region — 2m in us-east, 4m in us-west, 6m in
    # eu-west and 8m in ap-southeast — before the incident is
    # mitigated.
    verified = simulate(get("cdn-purge-failure"), DEFAULT_POLICY,
                        purge_verification="per-region")
    score = verified.score
    print(
        f"{'cdn-purge-failure':<20} ack={score.time_to_ack_min}m "
        f"mitigate={score.time_to_mitigate_min}m "
        f"escalations={score.escalations} "
        f"resolved={score.resolved} points={score.points}/100 "
        f"(verified per region, +{score.verify_delay_min}m)"
    )
    # The same dimension on the scenario it costs a grade for:
    # waf-rules-block mitigates in 15m unverified (100 points, grade
    # A) but in 37m verified per region (89 points, grade B) — the
    # 14m check in ap-southeast is what pushes it past the grace.
    verified = simulate(get("waf-rules-block"), DEFAULT_POLICY,
                        purge_verification="per-region")
    score = verified.score
    print(
        f"{'waf-rules-block':<20} ack={score.time_to_ack_min}m "
        f"mitigate={score.time_to_mitigate_min}m "
        f"escalations={score.escalations} "
        f"resolved={score.resolved} points={score.points}/100 "
        f"(verified per region, +{score.verify_delay_min}m)"
    )


if __name__ == "__main__":
    main()
