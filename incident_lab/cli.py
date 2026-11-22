"""CLI for incident-lab: list scenarios, run one, print timeline + score."""

import argparse
import json
import sys

from . import __version__
from .policy import DEFAULT_POLICY
from .scenarios import get, list_scenarios
from .simulator import simulate


def _print_result(result) -> None:
    incident = result.incident
    score = result.score
    print(f"incident: {incident.title} [{incident.severity}]")
    print(f"service:  {incident.service}")
    print(f"policy:   {result.policy.name}")
    print()
    print(result.timeline.render())
    print()
    print("score:")
    print(f"  time-to-ack:      {score.time_to_ack_min}m"
          if score.time_to_ack_min is not None else "  time-to-ack:      -")
    print(f"  time-to-mitigate: {score.time_to_mitigate_min}m"
          if score.time_to_mitigate_min is not None
          else "  time-to-mitigate: -")
    print(f"  time-to-resolve:  {score.time_to_resolve_min}m"
          if score.time_to_resolve_min is not None
          else "  time-to-resolve:  -")
    print(f"  escalations:      {score.escalations}")
    print(f"  resolved:         {'yes' if score.resolved else 'no'}")
    print(f"  points:           {score.points}/100")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="incident-lab",
        description="Simulate on-call incident response and replay the timeline.",
    )
    parser.add_argument("--version", action="version",
                        version=f"incident-lab {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list the built-in scenarios")

    run = sub.add_parser("run", help="run one scenario and print the replay")
    run.add_argument("scenario", help="scenario name (see: incident-lab list)")
    run.add_argument("--ack-timeout", type=int, default=None,
                     help="override the ack timeout (minutes) for every level")
    run.add_argument("--json", action="store_true",
                     help="print the result as JSON instead of the replay")

    args = parser.parse_args(argv)

    if args.command == "list":
        for name in list_scenarios():
            scenario = get(name)
            print(f"{name:<20} {scenario.service:<15} "
                  f"[{scenario.severity}] {scenario.title}")
        return 0

    if args.command == "run":
        try:
            scenario = get(args.scenario)
        except KeyError:
            print(f"unknown scenario: {args.scenario}", file=sys.stderr)
            print(f"known scenarios: {', '.join(list_scenarios())}",
                  file=sys.stderr)
            return 2
        policy = DEFAULT_POLICY
        if args.ack_timeout is not None:
            from dataclasses import replace
            levels = tuple(
                replace(level, ack_timeout_min=args.ack_timeout)
                for level in policy.levels
            )
            policy = replace(policy, levels=levels)
        result = simulate(scenario, policy)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            _print_result(result)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
