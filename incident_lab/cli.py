"""CLI for incident-lab: list scenarios, run one, print timeline + score."""

import argparse
import json
import sys

from . import __version__
from .policy import (
    POLICIES, list_policies, load_policy_file, parse_ack_timeout_overrides,
)
from .replay import load_result
from .scenarios import get, list_scenarios
from .simulator import simulate


def _print_result(result) -> None:
    incident = result.incident
    score = result.score
    print(f"incident: {incident.title} [{incident.severity}]")
    print(f"service:  {incident.service}")
    print(f"policy:   {result.policy.name}")
    if result.seed is not None:
        print(f"seed:     {result.seed}")
    print("timeouts: " + ", ".join(
        f"{level.responder.name} ({level.responder.role}) "
        f"{level.ack_timeout_min}m" for level in result.policy.levels))
    print()
    print(result.timeline.render())
    print()
    print("score:")
    print(f"  time-to-ack:      {score.time_to_ack_min}m"
          if score.time_to_ack_min is not None else "  time-to-ack:      -")
    print(f"  late by:          {score.ack_late_min}m"
          if score.ack_late_min is not None else "  late by:          -")
    print(f"  ack credit:       {score.ack_credit:.0%}"
          if score.ack_credit is not None else "  ack credit:       -")
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
    run.add_argument("--ack-timeouts", nargs="+", default=None,
                     metavar="TIMEOUTS",
                     help="override ack timeouts per level, in level order "
                          "(120,300,600) or by name (primary=120 L2=300)")
    run.add_argument("--policy", default=None,
                     help="escalation policy to use "
                          f"({', '.join(list_policies())})")
    run.add_argument("--policy-file", default=None, metavar="PATH",
                     help="load named policies from a JSON file; use one "
                          "with --policy, or the file's only policy if it "
                          "has exactly one")
    run.add_argument("--seed", type=int, default=None, metavar="N",
                     help="seed the RNG that jitters ack delays by up to "
                          "1 minute, so the same seed replays the same run "
                          "exactly; recorded in --json output")
    run.add_argument("--json", action="store_true",
                     help="print the result as JSON instead of the replay")

    replay = sub.add_parser(
        "replay",
        help="re-print a timeline from a saved --json run")
    replay.add_argument("path", metavar="PATH",
                        help="path to a JSON file saved from run --json")

    args = parser.parse_args(argv)

    if args.command == "list":
        for name in list_scenarios():
            scenario = get(name)
            print(f"{name:<20} {scenario.service:<15} "
                  f"[{scenario.severity}] {scenario.title}")
        return 0

    if args.command == "replay":
        try:
            result = load_result(args.path)
        except OSError as exc:
            print(f"cannot read replay file: {exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"invalid replay file: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"invalid replay file: {exc}", file=sys.stderr)
            return 2
        _print_result(result)
        return 0

    if args.command == "run":
        try:
            scenario = get(args.scenario)
        except KeyError:
            print(f"unknown scenario: {args.scenario}", file=sys.stderr)
            print(f"known scenarios: {', '.join(list_scenarios())}",
                  file=sys.stderr)
            return 2
        policies = dict(POLICIES)
        if args.policy_file is not None:
            try:
                file_policies = load_policy_file(args.policy_file)
            except OSError as exc:
                print(f"cannot read policy file: {exc}", file=sys.stderr)
                return 2
            except ValueError as exc:
                print(f"invalid policy file: {exc}", file=sys.stderr)
                return 2
            policies.update(file_policies)
        policy_name = args.policy
        if policy_name is None:
            if args.policy_file is not None and len(file_policies) == 1:
                policy_name = next(iter(file_policies))
            elif args.policy_file is not None:
                print("policy file has more than one policy; pick one with "
                      "--policy", file=sys.stderr)
                print(f"policies in file: {', '.join(file_policies)}",
                      file=sys.stderr)
                return 2
            else:
                policy_name = "standard"
        try:
            policy = policies[policy_name]
        except KeyError:
            print(f"unknown policy: {policy_name}", file=sys.stderr)
            print(f"known policies: {', '.join(policies)}",
                  file=sys.stderr)
            return 2
        if args.ack_timeout is not None and args.ack_timeouts is not None:
            print("use only one of --ack-timeout and --ack-timeouts",
                  file=sys.stderr)
            return 2
        if args.ack_timeout is not None:
            from dataclasses import replace
            levels = tuple(
                replace(level, ack_timeout_min=args.ack_timeout)
                for level in policy.levels
            )
            policy = replace(policy, levels=levels)
        elif args.ack_timeouts is not None:
            try:
                policy = parse_ack_timeout_overrides(args.ack_timeouts,
                                                     policy)
            except ValueError as exc:
                print(f"invalid --ack-timeouts: {exc}", file=sys.stderr)
                return 2
        result = simulate(scenario, policy, seed=args.seed)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            _print_result(result)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
