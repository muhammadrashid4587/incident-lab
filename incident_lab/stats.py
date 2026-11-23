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
    # "escalations", "resolved", "hints_used"}; a milestone a run
    # never reached is None in its row.
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
            "acked": self.acked,
            "resolved": self.resolved,
            "escalations": {
                "total": self.escalations_total,
                "mean": self.escalations_mean,
            },
            "hints_total": self.hints_total,
            "grades": dict(self.grade_counts),
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
        lines.append("")
        lines.append(f"acked:      {self.acked}/{self.count}")
        lines.append(f"resolved:   {self.resolved}/{self.count}")
        lines.append(f"escalations: {self.escalations_total} total, "
                     f"{self.escalations_mean:.1f} per run")
        lines.append(f"hints used: {self.hints_total}")
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


def aggregate_stats(results: list, labels: list = None) -> RunStats:
    """Summarise a list of saved runs.

    ``labels`` names each run in the per-run rows (defaults to
    "run 1", "run 2", ...). Raises ValueError on an empty list or a
    labels list of the wrong length.
    """
    results = list(results)
    if not results:
        raise ValueError("stats needs at least one run")
    if labels is None:
        labels = [f"run {index + 1}" for index in range(len(results))]
    if len(labels) != len(results):
        raise ValueError("stats needs one label per run")
    rows = []
    for result, label in zip(results, labels):
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
    )
