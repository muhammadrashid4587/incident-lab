"""CLI for incident-lab: list scenarios, run one, print timeline + score."""

import argparse
import json
import sys

from . import __version__
from .compare import compare_results
from .debrief import generate_debrief
from .difficulty import (
    apply_difficulty, list_difficulties, parse_difficulty,
)
from .policy import (
    POLICIES, list_policies, load_policy_file, parse_ack_timeout_overrides,
)
from .replay import load_result
from .scenarios import (
    SCENARIOS, get, list_scenarios, load_scenario_file, scenario_to_dict,
)
from .simulator import simulate, simulate_batch
from .stats import aggregate_stats


def parse_seed_spec(spec: str) -> list:
    """Parse a --seeds spec into a list of seed integers.

    Either a comma-separated list ("1,2,3"), an inclusive range
    ("1-5"), or a single seed ("7"). Seeds must be non-negative
    integers; duplicates are dropped, order is kept. Raises
    ValueError on a bad spec.
    """
    spec = spec.strip()
    if not spec:
        raise ValueError("seed spec must not be empty")
    if "," in spec:
        parts = [part.strip() for part in spec.split(",")]
        if any(part == "" for part in parts):
            raise ValueError(
                f"seed list must not have empty entries (got {spec!r})")
        seeds = [_parse_seed_int(part, spec) for part in parts]
    elif "-" in spec:
        parts = [part.strip() for part in spec.split("-")]
        if len(parts) != 2:
            raise ValueError(
                f"seed range must look like LO-HI (got {spec!r})")
        lo = _parse_seed_int(parts[0], spec)
        hi = _parse_seed_int(parts[1], spec)
        if lo > hi:
            raise ValueError(
                f"seed range must have LO <= HI (got {spec!r})")
        seeds = list(range(lo, hi + 1))
    else:
        seeds = [_parse_seed_int(spec, spec)]
    seen = set()
    out = []
    for seed in seeds:
        if seed not in seen:
            seen.add(seed)
            out.append(seed)
    return out


def _parse_seed_int(text: str, spec: str) -> int:
    """Parse one seed from a --seeds spec; non-negative integers only."""
    try:
        seed = int(text)
    except ValueError:
        raise ValueError(
            f"seeds must be integers (got {spec!r})")
    if seed < 0:
        raise ValueError(
            f"seeds must be non-negative integers (got {spec!r})")
    return seed


def parse_jitter_range(spec: str) -> tuple:
    """Parse a --jitter LO-HI spec into a (lo, hi) pair of minutes.

    Both bounds must be non-negative integers with lo <= hi. Raises
    ValueError on a bad spec.
    """
    parts = spec.split("-")
    if len(parts) != 2:
        raise ValueError(
            f"jitter range must look like LO-HI (got {spec!r})")
    try:
        lo, hi = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(
            f"jitter range bounds must be integers (got {spec!r})")
    if lo < 0 or hi < 0 or lo > hi:
        raise ValueError(
            f"jitter range must be non-negative minutes with LO <= HI "
            f"(got {spec!r})")
    return (lo, hi)


def _print_result(result) -> None:
    incident = result.incident
    score = result.score
    print(f"incident: {incident.title} [{incident.severity}]")
    print(f"service:  {incident.service}")
    print(f"policy:   {result.policy.name}")
    if result.difficulty is not None:
        print(f"difficulty: {result.difficulty.name}")
    if result.seed is not None:
        print(f"seed:     {result.seed}")
    if result.jitter is not None:
        print(f"jitter:   {result.jitter[0]}-{result.jitter[1]}m")
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
    if score.hints_used:
        print(f"  hint cost:        -{score.hint_cost_points} pts "
              f"({score.hints_used} hint(s))")
    print(f"  points:           {score.points}/100")
    if result.hints:
        print()
        print(f"hints used ({len(result.hints)} × "
              f"{result.hints[0].cost_points} pts):")
        for hint in result.hints:
            print(f"  - {hint.text}")


def _print_batch_result(batch) -> None:
    incident = batch.incident
    dist = batch.score_distribution()
    print(f"incident: {incident.title} [{incident.severity}]")
    print(f"service:  {incident.service}")
    print(f"policy:   {batch.policy.name}")
    if batch.difficulty is not None:
        print(f"difficulty: {batch.difficulty.name}")
    print(f"seeds:    {', '.join(str(seed) for seed in batch.seeds)}")
    if batch.jitter is not None:
        print(f"jitter:   {batch.jitter[0]}-{batch.jitter[1]}m")
    print("timeouts: " + ", ".join(
        f"{level.responder.name} ({level.responder.role}) "
        f"{level.ack_timeout_min}m" for level in batch.policy.levels))
    print()
    print(f"score distribution ({len(batch.runs)} runs):")
    print(f"  mean:   {dist['mean']:.1f}/100")
    print(f"  median: {dist['median']}/100")
    print(f"  min:    {dist['min']}/100")
    print(f"  max:    {dist['max']}/100")
    print("per-seed scores:")
    for entry in dist["per_seed"]:
        print(f"  seed {entry['seed']}: {entry['points']}/100")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="incident-lab",
        description="Simulate on-call incident response and replay the timeline.",
    )
    parser.add_argument("--version", action="version",
                        version=f"incident-lab {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list the built-in scenarios")

    template = sub.add_parser(
        "template",
        help="print a scenario as a shareable JSON template, ready to "
             "save and use with run --scenario-file")
    template.add_argument("scenario",
                          help="scenario name (see: incident-lab list)")

    run = sub.add_parser("run", help="run one scenario and print the replay")
    run.add_argument("scenario", help="scenario name (see: incident-lab list)")
    run.add_argument("--scenario-file", default=None, metavar="PATH",
                     help="load named scenarios from a JSON file; a file "
                          "scenario with the same name as a built-in one "
                          "wins (see: incident-lab template)")
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
    run.add_argument("--difficulty", default=None,
                     help="difficulty level: "
                          f"{', '.join(list_difficulties())} (or 1, 2, 3); "
                          "scales ack timeouts, scoring targets and the "
                          "default seeded jitter; default normal")
    run.add_argument("--seed", type=int, default=None, metavar="N",
                     help="seed the RNG that jitters ack delays by up to "
                          "1 minute, so the same seed replays the same run "
                          "exactly; recorded in --json output")
    run.add_argument("--seeds", default=None, metavar="SEEDS",
                     help="run the scenario once per seed and report the "
                          "score distribution, as a comma list (1,2,3) or "
                          "an inclusive range (1-5); cannot be combined "
                          "with --seed")
    run.add_argument("--jitter", default=None, metavar="LO-HI",
                     help="jitter range in minutes for seeded ack delays, "
                          "e.g. 0-3: each ack delay shifts by a magnitude "
                          "in that range, random sign; requires --seed or "
                          "--seeds, default 0-1; recorded in --json output")
    run.add_argument("--hints", type=int, default=0, metavar="N",
                     help="ask for N hints during the run, drawn in order "
                          "from the scenario's known-good next actions; "
                          "each hint costs 5 points off the final score "
                          "and is recorded in --json output; cannot be "
                          "combined with --seeds")
    run.add_argument("--json", action="store_true",
                     help="print the result as JSON instead of the replay")

    replay = sub.add_parser(
        "replay",
        help="re-print a timeline from a saved --json run")
    replay.add_argument("path", metavar="PATH",
                        help="path to a JSON file saved from run --json")

    debrief = sub.add_parser(
        "debrief",
        help="write a postmortem from a saved --json run")
    debrief.add_argument("path", metavar="PATH",
                         help="path to a JSON file saved from run --json")
    debrief.add_argument("--json", action="store_true",
                         help="print the postmortem as JSON instead of "
                              "Markdown")

    compare = sub.add_parser(
        "compare",
        help="diff two saved --json runs side by side: score deltas "
             "and timeline differences")
    compare.add_argument("path_a", metavar="RUN-A",
                         help="path to a JSON file saved from run --json")
    compare.add_argument("path_b", metavar="RUN-B",
                         help="path to a JSON file saved from run --json")
    compare.add_argument("--json", action="store_true",
                         help="print the comparison as JSON instead of "
                              "the human-readable diff")

    stats = sub.add_parser(
        "stats",
        help="aggregate saved --json runs: score spread, mean response "
             "times, grade counts, per-scenario means")
    stats.add_argument("paths", nargs="+", metavar="RUN",
                       help="paths to JSON files saved from run --json")
    stats.add_argument("--json", action="store_true",
                       help="print the summary as JSON instead of the "
                            "human-readable form")

    args = parser.parse_args(argv)

    if args.command == "list":
        for name in list_scenarios():
            scenario = get(name)
            print(f"{name:<20} {scenario.service:<15} "
                  f"[{scenario.severity}] {scenario.title}")
        return 0

    if args.command == "template":
        try:
            scenario = get(args.scenario)
        except KeyError:
            print(f"unknown scenario: {args.scenario}", file=sys.stderr)
            print(f"known scenarios: {', '.join(list_scenarios())}",
                  file=sys.stderr)
            return 2
        print(json.dumps(
            {"scenarios": {scenario.name: scenario_to_dict(scenario)}},
            indent=2))
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

    if args.command == "debrief":
        try:
            result = load_result(args.path)
        except OSError as exc:
            print(f"cannot read debrief file: {exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"invalid debrief file: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"invalid debrief file: {exc}", file=sys.stderr)
            return 2
        debrief = generate_debrief(result)
        if args.json:
            print(json.dumps(debrief.to_dict(), indent=2))
        else:
            print(debrief.render())
        return 0

    if args.command == "compare":
        results = []
        for path in (args.path_a, args.path_b):
            try:
                results.append(load_result(path))
            except OSError as exc:
                print(f"cannot read compare file: {exc}", file=sys.stderr)
                return 2
            except json.JSONDecodeError as exc:
                print(f"invalid compare file: {exc}", file=sys.stderr)
                return 2
            except ValueError as exc:
                print(f"invalid compare file: {exc}", file=sys.stderr)
                return 2
        comparison = compare_results(results[0], results[1],
                                     label_a=args.path_a,
                                     label_b=args.path_b)
        if args.json:
            print(json.dumps(comparison.to_dict(), indent=2))
        else:
            print(comparison.render())
        return 0

    if args.command == "stats":
        results = []
        for path in args.paths:
            try:
                results.append(load_result(path))
            except OSError as exc:
                print(f"cannot read stats file: {exc}", file=sys.stderr)
                return 2
            except json.JSONDecodeError as exc:
                print(f"invalid stats file: {exc}", file=sys.stderr)
                return 2
            except ValueError as exc:
                print(f"invalid stats file: {exc}", file=sys.stderr)
                return 2
        summary = aggregate_stats(results, labels=list(args.paths))
        if args.json:
            print(json.dumps(summary.to_dict(), indent=2))
        else:
            print(summary.render())
        return 0

    if args.command == "run":
        file_scenarios = {}
        if args.scenario_file is not None:
            try:
                file_scenarios = load_scenario_file(args.scenario_file)
            except OSError as exc:
                print(f"cannot read scenario file: {exc}", file=sys.stderr)
                return 2
            except json.JSONDecodeError as exc:
                print(f"invalid scenario file: {exc}", file=sys.stderr)
                return 2
            except ValueError as exc:
                print(f"invalid scenario file: {exc}", file=sys.stderr)
                return 2
        if args.scenario in file_scenarios:
            scenario = file_scenarios[args.scenario]
        else:
            try:
                scenario = get(args.scenario)
            except KeyError:
                print(f"unknown scenario: {args.scenario}", file=sys.stderr)
                known = list_scenarios() + sorted(
                    name for name in file_scenarios
                    if name not in SCENARIOS)
                print(f"known scenarios: {', '.join(known)}",
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
        difficulty = None
        if args.difficulty is not None:
            try:
                difficulty = parse_difficulty(args.difficulty)
            except ValueError as exc:
                print(f"invalid --difficulty: {exc}", file=sys.stderr)
                return 2
            # Difficulty scales the policy's timeouts first; an explicit
            # --ack-timeout(s) override below still wins for its levels.
            policy = apply_difficulty(policy, difficulty)
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
        if args.seed is not None and args.seeds is not None:
            print("use only one of --seed and --seeds", file=sys.stderr)
            return 2
        if args.hints < 0:
            print("--hints must be a non-negative number of hints",
                  file=sys.stderr)
            return 2
        if args.hints and args.seeds is not None:
            print("--hints cannot be combined with --seeds: hints are "
                  "asked for during one run", file=sys.stderr)
            return 2
        if args.hints > len(scenario.hints):
            print(f"scenario {scenario.name!r} has only "
                  f"{len(scenario.hints)} hint(s), asked for {args.hints}",
                  file=sys.stderr)
            return 2
        jitter = None
        if args.jitter is not None:
            if args.seed is None and args.seeds is None:
                print("--jitter requires --seed or --seeds", file=sys.stderr)
                return 2
            try:
                jitter = parse_jitter_range(args.jitter)
            except ValueError as exc:
                print(f"invalid --jitter: {exc}", file=sys.stderr)
                return 2
        if args.seeds is not None:
            try:
                seeds = parse_seed_spec(args.seeds)
            except ValueError as exc:
                print(f"invalid --seeds: {exc}", file=sys.stderr)
                return 2
            batch = simulate_batch(scenario, policy, seeds, jitter=jitter,
                                   difficulty=difficulty)
            if args.json:
                print(json.dumps(batch.to_dict(), indent=2))
            else:
                _print_batch_result(batch)
            return 0
        result = simulate(scenario, policy, seed=args.seed, jitter=jitter,
                          difficulty=difficulty, hints=args.hints)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            _print_result(result)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
