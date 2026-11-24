"""CLI for incident-lab: list scenarios, run one, print timeline + score."""

import argparse
import json
import sys

from . import __version__
from .bundle import (
    create_bundle,
    default_bundle_path,
    summarize_bundle,
    validate_bundle,
)
from .compare import compare_results, compare_shifts
from .debrief import generate_debrief, generate_shift_debrief
from .difficulty import (
    apply_difficulty, list_difficulties, parse_difficulty,
)
from .drill import run_drill
from .export_html import render_postmortem_html, render_shift_postmortem_html
from .export_pdf import render_postmortem_pdf, render_shift_postmortem_pdf
from .history import (
    DEFAULT_HISTORY_PATH, entry_from_result, load_history, record_run,
    render_all_operators, summarize_all, summarize_operator,
)
from .policy import (
    POLICIES, apply_rota, current_rota_week, list_policies, load_policy_file,
    load_rota_file, parse_ack_timeout_overrides, policy_template,
)
from .replay import load_result, load_saved, load_shift, load_shift_or_result
from .scenarios import (
    SCENARIOS, get, list_scenarios, load_scenario_file, scenario_to_dict,
)
from .shift import simulate_shift
from .simulator import (
    PURGE_VERIFICATION_MODES, simulate, simulate_batch,
)
from .stats import aggregate_stats
from .whatif import what_if


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
    if result.purge_verification != "off":
        print(f"purge verification: {result.purge_verification} "
              f"({len(result.regions)} regions, "
              f"+{result.score.verify_delay_min}m)")
    if result.rota_week is not None:
        print(f"rota week: {result.rota_week}")
    if result.seed is not None:
        print(f"seed:     {result.seed}")
    if result.jitter is not None:
        print(f"jitter:   {result.jitter[0]}-{result.jitter[1]}m")
    print("timeouts: " + ", ".join(
        f"{level.responder.name} ({level.responder.role}) "
        f"{level.ack_timeout_min}m" for level in result.policy.levels))
    print()
    print(result.timeline.render())
    if result.escalation_chain:
        print()
        print("escalation chain:")
        for step in result.escalation_chain:
            if step.outcome == "acked":
                line = (f"  L{step.level} {step.responder_name} "
                        f"({step.responder_role}) paged "
                        f"{step.paged_at_min}m, "
                        f"acked {step.acked_at_min}m")
                if step.ack_late_min:
                    line += f" (late by {step.ack_late_min}m)"
            else:
                line = (f"  L{step.level} {step.responder_name} "
                        f"({step.responder_role}) paged "
                        f"{step.paged_at_min}m, "
                        f"missed after {step.ack_timeout_min}m")
            if step.impact:
                line += f" — impact: {step.impact}"
            print(line)
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
    if score.verify_delay_min:
        print(f"  verify delay:     {score.verify_delay_min}m "
              f"({len(result.regions)} regions)")
    if score.slo_percent is not None:
        print(f"  downtime:         {score.downtime_min}m"
              if score.downtime_min is not None else "  downtime:         -")
        if score.error_budget_burn_pct is not None:
            print(f"  error budget:     burned "
                  f"{score.error_budget_burn_pct:.1f}% of the monthly "
                  f"budget (SLO {score.slo_percent}%)")
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
    if batch.purge_verification != "off":
        print(f"purge verification: {batch.purge_verification}")
    if batch.rota_week is not None:
        print(f"rota week: {batch.rota_week}")
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


def _resolve_scenario_policy(args):
    """Load the scenario, policy and difficulty for `run` / `drill`.

    Shared by both commands: the scenario (built-in, or from
    ``--scenario-file``), the policy (built-in, or from
    ``--policy-file``), the difficulty scaling and the explicit ack
    timeout overrides, plus the ``--hints`` validation. Returns
    ``(scenario, policy, difficulty, rota_week)`` — ``difficulty``
    is None when no ``--difficulty`` was given, and ``rota_week``
    is None when no ``--rota-file`` was given — or None after
    printing the error, for the caller to fail with exit code 2.
    """
    file_scenarios = {}
    if args.scenario_file is not None:
        try:
            file_scenarios = load_scenario_file(args.scenario_file)
        except OSError as exc:
            print(f"cannot read scenario file: {exc}", file=sys.stderr)
            return None
        except json.JSONDecodeError as exc:
            print(f"invalid scenario file: {exc}", file=sys.stderr)
            return None
        except ValueError as exc:
            print(f"invalid scenario file: {exc}", file=sys.stderr)
            return None
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
            return None
    policies = dict(POLICIES)
    if args.policy_file is not None:
        try:
            file_policies = load_policy_file(args.policy_file)
        except OSError as exc:
            print(f"cannot read policy file: {exc}", file=sys.stderr)
            return None
        except ValueError as exc:
            print(f"invalid policy file: {exc}", file=sys.stderr)
            return None
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
            return None
        else:
            policy_name = "standard"
    try:
        policy = policies[policy_name]
    except KeyError:
        print(f"unknown policy: {policy_name}", file=sys.stderr)
        print(f"known policies: {', '.join(policies)}",
              file=sys.stderr)
        return None
    # A rota file resolves the policy's responders to whoever is on
    # call for the rota week: the pager moves to the next person each
    # week. Only the names change — the roles, order and timeouts
    # are the policy's own.
    rota_week = None
    if getattr(args, "rota_week", None) is not None and \
            getattr(args, "rota_file", None) is None:
        print("--rota-week needs --rota-file: the week only means "
              "something against a rota", file=sys.stderr)
        return None
    if getattr(args, "rota_file", None) is not None:
        try:
            rota = load_rota_file(args.rota_file)
        except OSError as exc:
            print(f"cannot read rota file: {exc}", file=sys.stderr)
            return None
        except json.JSONDecodeError as exc:
            print(f"invalid rota file: {exc}", file=sys.stderr)
            return None
        except ValueError as exc:
            print(f"invalid rota file: {exc}", file=sys.stderr)
            return None
        rota_week = (args.rota_week if args.rota_week is not None
                     else current_rota_week())
        try:
            policy = apply_rota(policy, rota, rota_week)
        except ValueError as exc:
            print(f"invalid --rota-week: {exc}", file=sys.stderr)
            return None
    if args.ack_timeout is not None and args.ack_timeouts is not None:
        print("use only one of --ack-timeout and --ack-timeouts",
              file=sys.stderr)
        return None
    difficulty = None
    if args.difficulty is not None:
        try:
            difficulty = parse_difficulty(args.difficulty)
        except ValueError as exc:
            print(f"invalid --difficulty: {exc}", file=sys.stderr)
            return None
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
            return None
    if args.hints < 0:
        print("--hints must be a non-negative number of hints",
              file=sys.stderr)
        return None
    if args.hints > len(scenario.hints):
        print(f"scenario {scenario.name!r} has only "
              f"{len(scenario.hints)} hint(s), asked for {args.hints}",
              file=sys.stderr)
        return None
    if getattr(args, "verify_purge", None) == "per-region" \
            and not scenario.regions:
        print(f"scenario {scenario.name!r} has no regions to verify: "
              "--verify-purge per-region needs a scenario with "
              "regions", file=sys.stderr)
        return None
    return scenario, policy, difficulty, rota_week


def _resolve_shift(args):
    """Load the scenarios, policy and difficulty for `shift`.

    Every scenario name on the command line is resolved (built-in,
    or from ``--scenario-file``), and the policy / difficulty / rota
    are resolved once through `_resolve_scenario_policy` with the
    first scenario — the policy is the same for every incident in
    the shift. Returns ``(scenarios, policy, difficulty, rota_week)``
    — ``difficulty`` is None when no ``--difficulty`` was given, and
    ``rota_week`` is None when no ``--rota-file`` was given — or None
    after printing the error, for the caller to fail with exit code 2.
    """
    import types

    file_scenarios = {}
    if args.scenario_file is not None:
        try:
            file_scenarios = load_scenario_file(args.scenario_file)
        except OSError as exc:
            print(f"cannot read scenario file: {exc}", file=sys.stderr)
            return None
        except json.JSONDecodeError as exc:
            print(f"invalid scenario file: {exc}", file=sys.stderr)
            return None
        except ValueError as exc:
            print(f"invalid scenario file: {exc}", file=sys.stderr)
            return None
    scenarios = []
    for name in args.scenarios:
        if name in file_scenarios:
            scenarios.append(file_scenarios[name])
        else:
            try:
                scenarios.append(get(name))
            except KeyError:
                print(f"unknown scenario: {name}", file=sys.stderr)
                known = list_scenarios() + sorted(
                    scenario_name for scenario_name in file_scenarios
                    if scenario_name not in SCENARIOS)
                print(f"known scenarios: {', '.join(known)}",
                      file=sys.stderr)
                return None
    # Per-region verification needs every scheduled scenario to have
    # regions: the shared resolver above only sees the first one.
    if getattr(args, "verify_purge", None) == "per-region":
        regionless = [scenario.name for scenario in scenarios
                      if not scenario.regions]
        if regionless:
            print("--verify-purge per-region needs scenarios with "
                  "regions; no regions: " + ", ".join(regionless),
                  file=sys.stderr)
            return None
    # The policy, difficulty and rota are shared by the whole shift:
    # resolve them once, with the first scenario, and reuse them.
    # Shifts take no --hints and no per-level ack timeouts — those
    # belong to a single run, not a shift.
    shim = types.SimpleNamespace(**vars(args))
    shim.scenario = args.scenarios[0]
    shim.ack_timeout = None
    shim.ack_timeouts = None
    shim.hints = 0
    resolved = _resolve_scenario_policy(shim)
    if resolved is None:
        return None
    _, policy, difficulty, rota_week = resolved
    return scenarios, policy, difficulty, rota_week


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

    policy_template_parser = sub.add_parser(
        "policy-template",
        help="print a built-in policy as a starter JSON policy file, "
             "ready to edit and use with run --policy-file")
    policy_template_parser.add_argument(
        "policy", nargs="?", default="standard",
        help="policy name to base the template on "
             f"({', '.join(list_policies())}); default standard")
    policy_template_parser.add_argument(
        "--out", default=None, metavar="PATH",
        help="write the template to this file instead of printing it")

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
    run.add_argument("--rota-file", default=None, metavar="PATH",
                     help="load an on-call rota from a JSON file: each "
                          "role maps to a weekly rotation list of "
                          "responder names, and the run pages whoever is "
                          "on call for --rota-week (default: this week)")
    run.add_argument("--rota-week", type=int, default=None, metavar="N",
                     help="which week of the rota to page (0-based index "
                          "into each role's rotation list, wrapping "
                          "around); needs --rota-file, default: the "
                          "current ISO calendar week")
    run.add_argument("--difficulty", default=None,
                     help="difficulty level: "
                          f"{', '.join(list_difficulties())} (or 1, 2, 3); "
                          "scales ack timeouts, scoring targets and the "
                          "default seeded jitter; default normal")
    run.add_argument("--verify-purge", default=None,
                     choices=PURGE_VERIFICATION_MODES,
                     help="verify the fix in every region before the "
                          "incident is declared mitigated: off (default), "
                          "or per-region (3m per region, so a four-region "
                          "purge costs 12m); needs a scenario with "
                          "regions")
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

    shift = sub.add_parser(
        "shift",
        help="run several scenarios back-to-back against one shared "
             "error budget: burn accumulates across incidents, and the "
             "shift ends early (skipped incidents score 0) when the "
             "budget runs out")
    shift.add_argument("scenarios", nargs="+", metavar="SCENARIO",
                       help="scenario names in shift order "
                            "(see: incident-lab list)")
    shift.add_argument("--scenario-file", default=None, metavar="PATH",
                       help="load named scenarios from a JSON file; a file "
                            "scenario with the same name as a built-in one "
                            "wins (see: incident-lab template)")
    shift.add_argument("--slo", type=float, default=None, metavar="PCT",
                       help="the shared SLO target in percent (90 up to "
                            "but not including 100); default: the first "
                            "scenario's SLO, and the shift fails when no "
                            "scenario carries one")
    shift.add_argument("--policy", default=None,
                       help="escalation policy to use "
                            f"({', '.join(list_policies())})")
    shift.add_argument("--policy-file", default=None, metavar="PATH",
                       help="load named policies from a JSON file; use one "
                            "with --policy, or the file's only policy if it "
                            "has exactly one")
    shift.add_argument("--rota-file", default=None, metavar="PATH",
                       help="load an on-call rota from a JSON file: each "
                            "role maps to a weekly rotation list of "
                            "responder names, and the shift pages whoever "
                            "is on call for --rota-week (default: this "
                            "week)")
    shift.add_argument("--rota-week", type=int, default=None, metavar="N",
                       help="which week of the rota to page (0-based index "
                            "into each role's rotation list, wrapping "
                            "around); needs --rota-file, default: the "
                            "current ISO calendar week")
    shift.add_argument("--difficulty", default=None,
                       help="difficulty level: "
                            f"{', '.join(list_difficulties())} (or 1, 2, 3); "
                            "scales ack timeouts, scoring targets and the "
                            "default seeded jitter; default normal")
    shift.add_argument("--verify-purge", default=None,
                       choices=PURGE_VERIFICATION_MODES,
                       help="verify each incident's fix in every region "
                            "before it is declared mitigated: off "
                            "(default), or per-region (3m per region); "
                            "needs scenarios with regions")
    shift.add_argument("--seed", type=int, default=None, metavar="N",
                       help="seed the RNG that jitters ack delays: "
                            "incident i in the shift uses seed N+i, so the "
                            "shift replays exactly while the incidents "
                            "still vary; recorded in --json output")
    shift.add_argument("--jitter", default=None, metavar="LO-HI",
                       help="jitter range in minutes for seeded ack delays, "
                            "e.g. 0-3; requires --seed, default 0-1; "
                            "recorded in --json output")
    shift.add_argument("--json", action="store_true",
                       help="print the shift as JSON instead of the "
                            "human-readable summary")

    drill = sub.add_parser(
        "drill",
        help="play one scenario interactively, step by step: you "
             "decide when to ack, mitigate and resolve, and can ask "
             "for a hint when stuck, one decision "
             "per line (see README: Interactive drills)")
    drill.add_argument("scenario",
                       help="scenario name (see: incident-lab list)")
    drill.add_argument("--scenario-file", default=None, metavar="PATH",
                       help="load named scenarios from a JSON file; a file "
                            "scenario with the same name as a built-in one "
                            "wins (see: incident-lab template)")
    drill.add_argument("--ack-timeout", type=int, default=None,
                       help="override the ack timeout (minutes) for every "
                            "level")
    drill.add_argument("--ack-timeouts", nargs="+", default=None,
                       metavar="TIMEOUTS",
                       help="override ack timeouts per level, in level order "
                            "(120,300,600) or by name (primary=120 L2=300)")
    drill.add_argument("--policy", default=None,
                       help="escalation policy to use "
                            f"({', '.join(list_policies())})")
    drill.add_argument("--policy-file", default=None, metavar="PATH",
                       help="load named policies from a JSON file; use one "
                            "with --policy, or the file's only policy if it "
                            "has exactly one")
    drill.add_argument("--rota-file", default=None, metavar="PATH",
                       help="load an on-call rota from a JSON file: each "
                            "role maps to a weekly rotation list of "
                            "responder names, and the drill pages whoever "
                            "is on call for --rota-week (default: this "
                            "week)")
    drill.add_argument("--rota-week", type=int, default=None, metavar="N",
                       help="which week of the rota to page (0-based "
                            "index into each role's rotation list, "
                            "wrapping around); needs --rota-file, default: "
                            "the current ISO calendar week")
    drill.add_argument("--difficulty", default=None,
                       help="difficulty level: "
                            f"{', '.join(list_difficulties())} (or 1, 2, 3); "
                            "scales ack timeouts and scoring targets; "
                            "default normal")
    drill.add_argument("--verify-purge", default=None,
                       choices=PURGE_VERIFICATION_MODES,
                       help="verify the fix in every region before the "
                            "incident is declared mitigated: off (default), "
                            "or per-region (3m per region, so a four-region "
                            "purge costs 12m); needs a scenario with "
                            "regions")
    drill.add_argument("--hints", type=int, default=0, metavar="N",
                       help="ask for N hints up front, drawn in order "
                            "from the scenario's known-good next actions; "
                            "each hint costs 5 points off the final score "
                            "and is recorded in --json output")
    drill.add_argument("--input", default=None, metavar="PATH",
                       help="read decisions from this file instead of "
                            "stdin, one per line, so a drill can be "
                            "scripted without a TTY")
    drill.add_argument("--json", action="store_true",
                       help="print the result as JSON instead of the "
                            "replay (prompts go to stderr, so stdout "
                            "stays parseable JSON)")
    drill.add_argument("--out", default=None, metavar="PATH",
                       help="write the result as JSON to this file as "
                            "well as printing it")
    drill.add_argument("--operator", default=None, metavar="NAME",
                       help="record this drill in the operator history "
                            "under NAME, so `history --operator NAME` "
                            "can show the operator's runs and "
                            "improvement over time; without it the "
                            "drill is not recorded")
    drill.add_argument("--history-file", default=None, metavar="PATH",
                       help="history file to record this drill in "
                            "(default ~/.incident-lab/history.json); "
                            "needs --operator")

    history = sub.add_parser(
        "history",
        help="show recorded drill history: every operator's runs and "
             "improvement over time, or one operator's runs with "
             "`history --operator NAME` (drills are recorded with "
             "`drill --operator NAME`)")
    history.add_argument("--operator", default=None, metavar="NAME",
                         help="show one operator's runs and summary "
                              "instead of the overview of all operators")
    history.add_argument("--history-file", default=None, metavar="PATH",
                         help="history file to read "
                              "(default ~/.incident-lab/history.json)")
    history.add_argument("--json", action="store_true",
                         help="print the history as JSON instead of the "
                              "human-readable form")

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

    postmortem = sub.add_parser(
        "postmortem",
        help="render a saved --json run as a self-contained HTML "
             "postmortem: score and grade, timeline, per-responder "
             "timings, hints used, findings and action items; a saved "
             "shift --json file renders the shift postmortem instead")
    postmortem.add_argument("path", metavar="PATH",
                            help="path to a JSON file saved from run "
                                 "--json (or a saved what-if --json file, "
                                 "whose replay is rendered with its "
                                 "provenance), or from shift --json")
    postmortem.add_argument("--out", default=None, metavar="PATH",
                            help="write the postmortem to PATH instead of "
                                 "printing it (required with "
                                 "--format pdf)")
    postmortem.add_argument("--format", default="html",
                            choices=("html", "pdf"),
                            help="postmortem format: html (default, "
                                 "printed or written with --out) or pdf "
                                 "(a multi-page PDF, needs --out)")

    bundle = sub.add_parser(
        "bundle",
        help="package a saved --json run as a shareable zip: the run "
             "JSON, the HTML postmortem and a manifest with provenance "
             "and SHA-256 hashes, validated as it is created; "
             "--check validates a bundle someone shared; a saved "
             "shift --json file bundles the shift the same way")
    bundle.add_argument("path", metavar="PATH", nargs="?",
                        help="path to a JSON file saved from run --json "
                             "(or a saved what-if --json file, or a saved "
                             "shift --json file), or to a bundle zip with "
                             "--check")
    bundle.add_argument("--out", default=None, metavar="PATH",
                        help="write the bundle zip to PATH instead of "
                             "<run>-bundle.zip")
    bundle.add_argument("--check", action="store_true",
                        help="validate the bundle at PATH instead of "
                             "creating one")

    compare = sub.add_parser(
        "compare",
        help="diff two saved --json runs side by side: score deltas "
             "and timeline differences; each side may be a run or a "
             "what-if --json file (the replay is compared and flagged)")
    compare.add_argument("path_a", metavar="RUN-A",
                         help="path to a JSON file saved from run --json "
                              "or what-if --json")
    compare.add_argument("path_b", metavar="RUN-B",
                         help="path to a JSON file saved from run --json "
                              "or what-if --json")
    compare.add_argument("--json", action="store_true",
                         help="print the comparison as JSON instead of "
                              "the human-readable diff")

    stats = sub.add_parser(
        "stats",
        help="aggregate saved --json runs: score spread, mean response "
             "times, grade counts, per-scenario means; what-if --json "
             "files are aggregated as their replayed runs and flagged "
             "as what-if")
    stats.add_argument("paths", nargs="+", metavar="RUN",
                       help="paths to JSON files saved from run --json "
                            "or what-if --json")
    stats.add_argument("--json", action="store_true",
                       help="print the summary as JSON instead of the "
                            "human-readable form")

    whatif = sub.add_parser(
        "what-if",
        help="replay a saved --json run with overridden parameters "
             "(ack timeouts, difficulty, policy, purge verification) "
             "and show how the score and grade would have changed; "
             "the original run file is not modified")
    whatif.add_argument("path", metavar="PATH",
                        help="path to a JSON file saved from run --json")
    whatif.add_argument("--ack-timeout", type=int, default=None,
                        help="override the ack timeout (minutes) for "
                             "every level")
    whatif.add_argument("--ack-timeouts", nargs="+", default=None,
                        metavar="TIMEOUTS",
                        help="override ack timeouts per level, in level "
                             "order (120,300,600) or by name "
                             "(primary=120 L2=300)")
    whatif.add_argument("--difficulty", default=None,
                        help="replay the run at this difficulty level: "
                             f"{', '.join(list_difficulties())} "
                             "(or 1, 2, 3); scales ack timeouts relative "
                             "to the original run's difficulty and scores "
                             "against the new difficulty's targets")
    whatif.add_argument("--verify-purge", default=None,
                        choices=PURGE_VERIFICATION_MODES,
                        help="replay the run with the fix verified in "
                             "every region before it is declared mitigated "
                             "(per-region, 3m per region), or without that "
                             "verification (off); needs a scenario with "
                             "regions")
    whatif.add_argument("--policy", default=None,
                        help="replay the run under a different named "
                             "escalation policy "
                             f"({', '.join(list_policies())}); the new "
                             "policy's timeouts are scaled to the "
                             "original run's difficulty unless "
                             "--difficulty is also given")
    whatif.add_argument("--policy-file", default=None, metavar="PATH",
                        help="load named policies from a JSON file; use "
                             "one with --policy, or the file's only "
                             "policy if it has exactly one")
    whatif.add_argument("--json", action="store_true",
                        help="print the what-if result as JSON instead of "
                             "the human-readable report")

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

    if args.command == "policy-template":
        try:
            data = policy_template(args.policy)
        except KeyError:
            print(f"unknown policy: {args.policy}", file=sys.stderr)
            print(f"known policies: {', '.join(list_policies())}",
                  file=sys.stderr)
            return 2
        text = json.dumps(data, indent=2)
        if args.out is not None:
            try:
                with open(args.out, "w", encoding="utf-8") as handle:
                    handle.write(text + "\n")
            except OSError as exc:
                print(f"cannot write policy template: {exc}",
                      file=sys.stderr)
                return 2
            print(f"wrote policy template for {args.policy!r} "
                  f"to {args.out}")
            return 0
        print(text)
        return 0

    if args.command == "history":
        history_path = args.history_file or DEFAULT_HISTORY_PATH
        try:
            entries = load_history(history_path)
        except OSError as exc:
            print(f"cannot read history file: {exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"invalid history file: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"invalid history file: {exc}", file=sys.stderr)
            return 2
        if not entries:
            print("no drills recorded yet — record one with "
                  "`drill --operator NAME`")
            return 0
        if args.operator is not None:
            operator = args.operator.strip()
            if not operator:
                print("--operator must name an operator", file=sys.stderr)
                return 2
            try:
                summary = summarize_operator(entries, operator)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 2
            operator_entries = [entry for entry in entries
                                if entry.operator == operator]
            if args.json:
                print(json.dumps({
                    "summary": summary.to_dict(),
                    "runs": [entry.to_dict()
                             for entry in operator_entries],
                }, indent=2))
            else:
                print(summary.render(operator_entries))
            return 0
        summaries = summarize_all(entries)
        if args.json:
            print(json.dumps({
                "operators": [summary.to_dict()
                              for summary in summaries],
            }, indent=2))
        else:
            print(render_all_operators(summaries))
        return 0

    if args.command == "replay":
        try:
            kind, loaded = load_shift_or_result(args.path)
        except OSError as exc:
            print(f"cannot read replay file: {exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"invalid replay file: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"invalid replay file: {exc}", file=sys.stderr)
            return 2
        if kind == "shift":
            print(loaded.render())
        else:
            _print_result(loaded)
        return 0

    if args.command == "debrief":
        try:
            kind, loaded = load_shift_or_result(args.path)
        except OSError as exc:
            print(f"cannot read debrief file: {exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"invalid debrief file: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"invalid debrief file: {exc}", file=sys.stderr)
            return 2
        if kind == "shift":
            debrief = generate_shift_debrief(loaded)
        else:
            debrief = generate_debrief(loaded)
        if args.json:
            print(json.dumps(debrief.to_dict(), indent=2))
        else:
            print(debrief.render())
        return 0

    if args.command == "postmortem":
        try:
            loaded = load_saved(args.path)
            shift = None
        except OSError as exc:
            print(f"cannot read postmortem file: {exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"invalid postmortem file: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            # A shift file holds several runs, not one: render the
            # shift postmortem instead.
            if "shift file" not in str(exc):
                print(f"invalid postmortem file: {exc}", file=sys.stderr)
                return 2
            try:
                shift = load_shift(args.path)
            except ValueError as exc:
                print(f"invalid postmortem file: {exc}", file=sys.stderr)
                return 2
            loaded = None
        if args.format == "pdf":
            # A PDF is binary: it is only ever written to a file,
            # never printed to stdout.
            if args.out is None:
                print("postmortem --format pdf needs --out PATH: a PDF "
                      "cannot be printed as text", file=sys.stderr)
                return 2
            if shift is not None:
                data = render_shift_postmortem_pdf(shift)
                label = (f"shift postmortem "
                         f"({len(shift.runs)} incident(s) ran)")
            else:
                data = render_postmortem_pdf(loaded.result,
                                             what_if=loaded.what_if)
                label = f"{loaded.result.incident.title!r}"
            try:
                with open(args.out, "wb") as handle:
                    handle.write(data)
            except OSError as exc:
                print(f"cannot write postmortem file: {exc}",
                      file=sys.stderr)
                return 2
            print(f"wrote PDF postmortem for {label} to {args.out}")
            return 0
        if shift is not None:
            text = render_shift_postmortem_html(shift)
            label = (f"shift postmortem "
                     f"({len(shift.runs)} incident(s) ran)")
        else:
            text = render_postmortem_html(loaded.result,
                                          what_if=loaded.what_if)
            label = f"{loaded.result.incident.title!r}"
        if args.out is not None:
            try:
                with open(args.out, "w", encoding="utf-8") as handle:
                    handle.write(text)
            except OSError as exc:
                print(f"cannot write postmortem file: {exc}",
                      file=sys.stderr)
                return 2
            print(f"wrote postmortem for {label} to {args.out}")
            return 0
        print(text)
        return 0

    if args.command == "bundle":
        if args.check:
            if args.path is None:
                print("bundle --check needs a bundle zip path",
                      file=sys.stderr)
                return 2
            try:
                manifest = validate_bundle(args.path)
            except ValueError as exc:
                print(f"invalid bundle: {exc}", file=sys.stderr)
                return 2
            print(summarize_bundle(manifest))
            print(f"bundle OK: {args.path}")
            return 0
        if args.path is None:
            print("bundle needs a saved run path", file=sys.stderr)
            return 2
        out_path = args.out or default_bundle_path(args.path)
        try:
            manifest = create_bundle(args.path, out_path)
        except OSError as exc:
            print(f"cannot read bundle file: {exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"invalid bundle file: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"invalid bundle file: {exc}", file=sys.stderr)
            return 2
        names = ", ".join(entry["name"] for entry in manifest["files"])
        if manifest.get("shift") is not None:
            label = (f"shift of "
                     f"{len(manifest['shift']['incidents'])} incident(s)")
        else:
            label = f"{manifest['incident']['title']!r}"
        print(f"wrote bundle for {label} to {out_path} ({names})")
        return 0

    if args.command == "compare":
        loaded = []
        for path in (args.path_a, args.path_b):
            try:
                entry = load_saved(path)
            except OSError as exc:
                print(f"cannot read compare file: {exc}", file=sys.stderr)
                return 2
            except json.JSONDecodeError as exc:
                print(f"invalid compare file: {exc}", file=sys.stderr)
                return 2
            except ValueError as exc:
                # A shift file is not a single run: load it as a
                # shift instead, and compare shifts with shifts.
                if "shift file" not in str(exc):
                    print(f"invalid compare file: {exc}", file=sys.stderr)
                    return 2
                try:
                    _, shift = load_shift_or_result(path)
                except ValueError as exc:
                    print(f"invalid compare file: {exc}", file=sys.stderr)
                    return 2
                loaded.append(("shift", shift, None))
                continue
            loaded.append(("run", entry.result, entry.what_if))
        if loaded[0][0] == "shift" and loaded[1][0] == "shift":
            comparison = compare_shifts(loaded[0][1], loaded[1][1],
                                        label_a=args.path_a,
                                        label_b=args.path_b)
        elif loaded[0][0] != loaded[1][0]:
            print("cannot compare a shift with a single run: compare "
                  "two shifts, or two runs", file=sys.stderr)
            return 2
        else:
            comparison = compare_results(loaded[0][1], loaded[1][1],
                                         label_a=args.path_a,
                                         label_b=args.path_b,
                                         whatif_a=loaded[0][2],
                                         whatif_b=loaded[1][2])
        if args.json:
            print(json.dumps(comparison.to_dict(), indent=2))
        else:
            print(comparison.render())
        return 0

    if args.command == "stats":
        results = []
        labels = []
        whatifs = []
        shift_count = 0
        for path in args.paths:
            try:
                entry = load_saved(path)
            except OSError as exc:
                print(f"cannot read stats file: {exc}", file=sys.stderr)
                return 2
            except json.JSONDecodeError as exc:
                print(f"invalid stats file: {exc}", file=sys.stderr)
                return 2
            except ValueError as exc:
                # A shift file contributes one row per incident it
                # ran, labelled with its scenario.
                if "shift file" not in str(exc):
                    print(f"invalid stats file: {exc}", file=sys.stderr)
                    return 2
                try:
                    _, shift = load_shift_or_result(path)
                except ValueError as exc:
                    print(f"invalid stats file: {exc}", file=sys.stderr)
                    return 2
                shift_count += 1
                for run in shift.runs:
                    results.append(run)
                    labels.append(f"{path}:{run.incident.id}")
                    whatifs.append(None)
                continue
            results.append(entry.result)
            labels.append(path)
            whatifs.append(entry.what_if)
        summary = aggregate_stats(results, labels=labels, whatifs=whatifs,
                                  shift_count=shift_count)
        if args.json:
            print(json.dumps(summary.to_dict(), indent=2))
        else:
            print(summary.render())
        return 0

    if args.command == "what-if":
        try:
            result = load_result(args.path)
        except OSError as exc:
            print(f"cannot read what-if file: {exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"invalid what-if file: {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(f"invalid what-if file: {exc}", file=sys.stderr)
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
        policy = None
        if policy_name is not None:
            try:
                policy = policies[policy_name]
            except KeyError:
                print(f"unknown policy: {policy_name}", file=sys.stderr)
                print(f"known policies: {', '.join(policies)}",
                      file=sys.stderr)
                return 2
        if (args.ack_timeout is None and args.ack_timeouts is None
                and args.difficulty is None and policy is None
                and args.verify_purge is None):
            print("what-if needs an override: --ack-timeout, "
                  "--ack-timeouts, --difficulty, --policy or "
                  "--verify-purge",
                  file=sys.stderr)
            return 2
        difficulty = None
        if args.difficulty is not None:
            try:
                difficulty = parse_difficulty(args.difficulty)
            except ValueError as exc:
                print(f"invalid --difficulty: {exc}", file=sys.stderr)
                return 2
        try:
            outcome = what_if(result, ack_timeout=args.ack_timeout,
                              ack_timeouts=args.ack_timeouts,
                              difficulty=difficulty, policy=policy,
                              purge_verification=args.verify_purge)
        except ValueError as exc:
            print(f"invalid what-if: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(outcome.to_dict(), indent=2))
        else:
            print(outcome.render())
        return 0

    if args.command == "shift":
        resolved = _resolve_shift(args)
        if resolved is None:
            return 2
        scenarios, policy, difficulty, rota_week = resolved
        if args.slo is not None and not 90 <= args.slo < 100:
            print("--slo must be from 90 up to (but not including) 100",
                  file=sys.stderr)
            return 2
        jitter = None
        if args.jitter is not None:
            if args.seed is None:
                print("--jitter requires --seed", file=sys.stderr)
                return 2
            try:
                jitter = parse_jitter_range(args.jitter)
            except ValueError as exc:
                print(f"invalid --jitter: {exc}", file=sys.stderr)
                return 2
        try:
            shift = simulate_shift(scenarios, policy,
                                   slo_percent=args.slo,
                                   difficulty=difficulty,
                                   seed=args.seed, jitter=jitter,
                                   rota_week=rota_week,
                                   purge_verification=(
                                       args.verify_purge or "off"))
        except ValueError as exc:
            print(f"invalid shift: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(shift.to_dict(), indent=2))
        else:
            print(shift.render())
        return 0

    if args.command == "run":
        resolved = _resolve_scenario_policy(args)
        if resolved is None:
            return 2
        scenario, policy, difficulty, rota_week = resolved
        if args.seed is not None and args.seeds is not None:
            print("use only one of --seed and --seeds", file=sys.stderr)
            return 2
        if args.hints and args.seeds is not None:
            print("--hints cannot be combined with --seeds: hints are "
                  "asked for during one run", file=sys.stderr)
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
                                   difficulty=difficulty,
                                   rota_week=rota_week,
                                   purge_verification=(
                                       args.verify_purge or "off"))
            if args.json:
                print(json.dumps(batch.to_dict(), indent=2))
            else:
                _print_batch_result(batch)
            return 0
        result = simulate(scenario, policy, seed=args.seed, jitter=jitter,
                          difficulty=difficulty, hints=args.hints,
                          rota_week=rota_week,
                          purge_verification=args.verify_purge or "off")
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            _print_result(result)
        return 0

    if args.command == "drill":
        if args.operator is not None and not args.operator.strip():
            print("--operator must name an operator", file=sys.stderr)
            return 2
        if args.history_file is not None and args.operator is None:
            print("--history-file needs --operator: history is only "
                  "recorded for a named operator", file=sys.stderr)
            return 2
        resolved = _resolve_scenario_policy(args)
        if resolved is None:
            return 2
        scenario, policy, difficulty, rota_week = resolved
        input_stream = sys.stdin
        input_file = None
        if args.input is not None:
            try:
                input_file = open(args.input, encoding="utf-8")
            except OSError as exc:
                print(f"cannot read drill input file: {exc}",
                      file=sys.stderr)
                return 2
            input_stream = input_file
        try:
            result = run_drill(scenario, policy, difficulty=difficulty,
                               hints=args.hints,
                               input_stream=input_stream,
                               rota_week=rota_week,
                               purge_verification=(
                                   args.verify_purge or "off"))
        except ValueError as exc:
            print(f"invalid drill: {exc}", file=sys.stderr)
            return 2
        finally:
            if input_file is not None:
                input_file.close()
        if args.operator is not None:
            history_path = args.history_file or DEFAULT_HISTORY_PATH
            try:
                record_run(history_path,
                           entry_from_result(result, args.operator.strip()))
            except (OSError, ValueError) as exc:
                # The drill itself ran fine; a history write failure
                # is a warning, not a failed drill.
                print(f"warning: could not record drill history: {exc}",
                      file=sys.stderr)
            else:
                print(f"recorded drill for operator "
                      f"{args.operator.strip()!r} in {history_path}",
                      file=sys.stderr)
        if args.out is not None:
            try:
                with open(args.out, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(result.to_dict(), indent=2)
                                 + "\n")
            except OSError as exc:
                print(f"cannot write drill output file: {exc}",
                      file=sys.stderr)
                return 2
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            _print_result(result)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
