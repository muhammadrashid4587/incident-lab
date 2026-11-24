"""Aggregate saved incident-lab runs into summary statistics.

Given several SimulationResults (rebuilt from saved `run --json` files
by replay.load_result), this summarises the set: how the scores
spread, how long acks and mitigations took on average, how often the
incident went unacked or unresolved, how many hints the operators
leaned on, and a grade distribution. A batch of runs is only
meaningful when they played the same scenario — runs spanning several
scenarios are still aggregated, but the report says so plainly
instead of pretending the average is apples to apples.
"""

from dataclasses import dataclass
import statistics

from .debrief import grade
from .simulator import SimulationResult


def _mean(values: list):
    """Mean of a list of numbers, or None when the list is empty."""
    if not values:
        return None
    return statistics.mean(values)


@dataclass
class RunStats:
    # One row per run: {"label", "scenario", "points", "grade",
    # "time_to_ack_min", "time_to_mitigate_min", "time_to_resolve_min",
    # "escalations", "resolved", "hints_used", "impact", "what_if",
    # "error_budget_burn_pct"}; a milestone a run never reached is
    # None in its row, "impact" is the impact the run ended at from
    # its escalation chain (None when the run recorded no chain or the
    # scenario has no impact stages), "error_budget_burn_pct" is the
    # share of the monthly error budget the run burned (None when the
    # run has no SLO or was never mitigated), and "what_if" is None for
    # a real run or {"overrides", "points_delta", "base_points"} for a
    # what-if replay.
    runs: list
    count: int
    # Points spread across all runs.
    points_mean: float
    points_min: int
    points_max: int
    points_median: float
    # Mean minutes to each milestone, over the runs that reached it;
    # None when no run reached that milestone.
    mean_time_to_ack_min: object
    mean_time_to_mitigate_min: object
    mean_time_to_resolve_min: object
    # Mean error-budget burn in percent, over the runs that reported
    # one (a run with no SLO or no downtime reports none); None when
    # no run reported a burn.
    mean_error_budget_burn_pct: object
    # How many runs were acked and resolved at all.
    acked: int
    resolved: int
    # Escalations across all runs, and the mean per run.
    escalations_total: int
    escalations_mean: float
    # Hints asked for across all runs.
    hints_total: int
    # Grade counts: {"A": n, "B": n, "C": n, "D": n}.
    grade_counts: dict
    # Per-scenario counts and mean points: {"scenario": {"runs": n,
    # "points_mean": x}}.
    by_scenario: dict
    # True when every run played the same scenario.
    same_scenario: bool
    # How many of the runs are what-if replays rather than runs that
    # happened; a what-if replay is a hypothetical, so it is flagged
    # in its row and counted here instead of being silently mixed in.
    whatif_count: int
    # How many runs saw their impact grow while the page went
    # unanswered (the chain's impact changed between levels).
    impact_growth_runs: int
    # How many of the input files were shifts rather than single
    # runs: a shift contributes one row per incident it ran, so this
    # says how many of the rows came from shifts.
    shift_count: int = 0

    def to_dict(self) -> dict:
        """JSON-ready dict: per-run rows and the aggregates."""
        return {
            "count": self.count,
            "same_scenario": self.same_scenario,
            "points": {
                "mean": self.points_mean,
                "min": self.points_min,
                "max": self.points_max,
                "median": self.points_median,
            },
            "mean_time_to_ack_min": self.mean_time_to_ack_min,
            "mean_time_to_mitigate_min": self.mean_time_to_mitigate_min,
            "mean_time_to_resolve_min": self.mean_time_to_resolve_min,
            "mean_error_budget_burn_pct": self.mean_error_budget_burn_pct,
            "acked": self.acked,
            "resolved": self.resolved,
            "escalations": {
                "total": self.escalations_total,
                "mean": self.escalations_mean,
            },
            "hints_total": self.hints_total,
            "impact_growth_runs": self.impact_growth_runs,
            "grades": dict(self.grade_counts),
            "what_if_runs": self.whatif_count,
            "shift_files": self.shift_count,
            "by_scenario": {
                name: dict(entry)
                for name, entry in self.by_scenario.items()
            },
            "runs": [dict(row) for row in self.runs],
        }

    def render(self) -> str:
        """Human-readable summary of the runs."""
        lines = [f"runs: {self.count}"]
        if not self.same_scenario:
            names = sorted(self.by_scenario)
            lines.append("note: multiple scenarios ("
                         + ", ".join(names)
                         + ") — averages mix different incidents")
        lines.append("")
        lines.append("points:")
        lines.append(f"  mean:   {self.points_mean:.1f}/100")
        lines.append(f"  median: {self.points_median}/100")
        lines.append(f"  min:    {self.points_min}/100")
        lines.append(f"  max:    {self.points_max}/100")
        lines.append("")
        lines.append("averages (runs that reached each milestone):")
        lines.append(f"  time-to-ack:      {_fmt_mean(self.mean_time_to_ack_min)}")
        lines.append(f"  time-to-mitigate: {_fmt_mean(self.mean_time_to_mitigate_min)}")
        lines.append(f"  time-to-resolve:  {_fmt_mean(self.mean_time_to_resolve_min)}")
        if self.mean_error_budget_burn_pct is not None:
            lines.append(
                f"  error budget burn: "
                f"{self.mean_error_budget_burn_pct:.1f}% of the monthly "
                f"budget (mean)")
        lines.append("")
        lines.append(f"acked:      {self.acked}/{self.count}")
        lines.append(f"resolved:   {self.resolved}/{self.count}")
        lines.append(f"escalations: {self.escalations_total} total, "
                     f"{self.escalations_mean:.1f} per run")
        lines.append(f"hints used: {self.hints_total}")
        if self.impact_growth_runs:
            lines.append(
                f"impact grew while paging: {self.impact_growth_runs} "
                f"run(s)")
        if self.whatif_count:
            lines.append(
                f"what-if runs: {self.whatif_count} "
                f"(hypothetical replays, flagged per run in --json)")
        if self.shift_count:
            lines.append(
                f"shift files: {self.shift_count} (each incident in a "
                f"shift counted as a run; the shift's shared-budget "
                f"burn is in the shift file, not these rows)")
        lines.append("")
        lines.append("grades:")
        for letter in ("A", "B", "C", "D"):
            lines.append(f"  {letter}: {self.grade_counts[letter]}")
        if not self.same_scenario:
            lines.append("")
            lines.append("by scenario:")
            for name in sorted(self.by_scenario):
                entry = self.by_scenario[name]
                lines.append(f"  {name}: {entry['runs']} run(s), "
                             f"mean {entry['points_mean']:.1f}/100")
        return "\n".join(lines)


def _fmt_mean(value) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}m"


def _final_impact(result) -> object:
    """The impact in effect at the end of a run's escalation chain.

    The last step's impact — the stage the incident had reached when
    someone acked (or when the chain ran out). None when the run
    recorded no chain or the scenario has no impact stages.
    """
    chain = result.escalation_chain or []
    if not chain:
        return None
    impact = chain[-1].impact
    return impact or None


def _impact_grew(result) -> bool:
    """True when a run's impact changed between chain levels — the
    incident got worse while the page went unanswered."""
    chain = result.escalation_chain or []
    seen = []
    for step in chain:
        if step.impact and step.impact not in seen:
            seen.append(step.impact)
    return len(seen) > 1


def aggregate_stats(results: list, labels: list = None,
                    whatifs: list = None, shift_count: int = 0) -> RunStats:
    """Summarise a list of saved runs.

    ``labels`` names each run in the per-run rows (defaults to
    "run 1", "run 2", ...). ``whatifs`` is an optional list aligned
    with ``results`` of WhatIfInfo objects (from replay.load_saved,
    or None) for runs that came from a `what-if --json` file; those
    rows are flagged as what-if replays with their overrides and
    points delta recorded. ``shift_count`` is how many of the input
    files were shifts (each incident in a shift is one run here), so
    the report can say the rows came from shifts. Raises ValueError
    on an empty list or a labels / whatifs list of the wrong length.
    """
    results = list(results)
    if not results:
        raise ValueError("stats needs at least one run")
    if labels is None:
        labels = [f"run {index + 1}" for index in range(len(results))]
    if len(labels) != len(results):
        raise ValueError("stats needs one label per run")
    if whatifs is None:
        whatifs = [None] * len(results)
    if len(whatifs) != len(results):
        raise ValueError("stats needs one what-if entry per run")
    rows = []
    for result, label, whatif in zip(results, labels, whatifs):
        score = result.score
        rows.append({
            "label": label,
            "scenario": result.incident.id,
            "points": score.points,
            "grade": grade(score.points),
            "time_to_ack_min": score.time_to_ack_min,
            "time_to_mitigate_min": score.time_to_mitigate_min,
            "time_to_resolve_min": score.time_to_resolve_min,
            "escalations": score.escalations,
            "resolved": score.resolved,
            "hints_used": score.hints_used,
            # The share of the monthly error budget the run burned
            # (None when the run has no SLO or was never mitigated).
            "error_budget_burn_pct": score.error_budget_burn_pct,
            # The impact the run ended at, from its escalation chain
            # (None when the run recorded no chain or the scenario has
            # no impact stages).
            "impact": _final_impact(result),
            "what_if": (whatif.to_dict()
                        if whatif is not None else None),
        })
    points = [row["points"] for row in rows]
    grade_counts = {letter: 0 for letter in ("A", "B", "C", "D")}
    for row in rows:
        grade_counts[row["grade"]] += 1
    by_scenario = {}
    for row in rows:
        entry = by_scenario.setdefault(row["scenario"],
                                       {"runs": 0, "points": []})
        entry["runs"] += 1
        entry["points"].append(row["points"])
    by_scenario = {
        name: {"runs": entry["runs"],
               "points_mean": statistics.mean(entry["points"])}
        for name, entry in by_scenario.items()
    }
    return RunStats(
        runs=rows,
        count=len(rows),
        points_mean=statistics.mean(points),
        points_min=min(points),
        points_max=max(points),
        points_median=statistics.median(points),
        mean_time_to_ack_min=_mean(
            [row["time_to_ack_min"] for row in rows
             if row["time_to_ack_min"] is not None]),
        mean_time_to_mitigate_min=_mean(
            [row["time_to_mitigate_min"] for row in rows
             if row["time_to_mitigate_min"] is not None]),
        mean_time_to_resolve_min=_mean(
            [row["time_to_resolve_min"] for row in rows
             if row["time_to_resolve_min"] is not None]),
        mean_error_budget_burn_pct=_mean(
            [row["error_budget_burn_pct"] for row in rows
             if row["error_budget_burn_pct"] is not None]),
        acked=sum(1 for row in rows
                  if row["time_to_ack_min"] is not None),
        resolved=sum(1 for row in rows if row["resolved"]),
        escalations_total=sum(row["escalations"] for row in rows),
        escalations_mean=statistics.mean(
            [row["escalations"] for row in rows]),
        hints_total=sum(row["hints_used"] for row in rows),
        grade_counts=grade_counts,
        by_scenario=by_scenario,
        same_scenario=len(by_scenario) == 1,
        whatif_count=sum(1 for whatif in whatifs
                         if whatif is not None),
        impact_growth_runs=sum(1 for result in results
                               if _impact_grew(result)),
        shift_count=shift_count,
    )
