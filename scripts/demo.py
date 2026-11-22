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


if __name__ == "__main__":
    main()
