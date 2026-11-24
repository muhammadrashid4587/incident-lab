"""Compare two saved incident-lab runs side by side.

Given two SimulationResults (in memory, or rebuilt from saved
`run --json` files by replay.load_result), this diffs the scores and
the timelines: which run scored higher and by how much on each
metric, which timeline events the runs share (and how their times
shifted), and which events only one run has. A comparison only means
much when both runs played the same scenario — comparing different
scenarios still works, but the report says so plainly instead of
pretending the scores are apples to apples.
"""

from dataclasses import dataclass

from .simulator import SimulationResult

# (score attribute, display label, unit) for the score rows, in the
# order the report prints them.
_SCORE_FIELDS = [
    ("points", "points", "/100"),
    ("time_to_ack_min", "time-to-ack", "m"),
    ("time_to_mitigate_min", "time-to-mitigate", "m"),
    ("time_to_resolve_min", "time-to-resolve", "m"),
    ("escalations", "escalations", ""),
    ("ack_late_min", "late by", "m"),
    ("ack_credit", "ack credit", ""),
    # Error-budget burn is a percent number (115.7), not a fraction,
    # so it formats as "115.7%" rather than like ack_credit.
    ("error_budget_burn_pct", "error budget burn", "%"),
]


def _event_key(event) -> tuple:
    """Identity of an event ignoring when it happened: kind, actor, detail."""
    return (event.kind, event.actor, event.detail)


@dataclass
class Comparison:
    label_a: str
    label_b: str
    incident_a: str  # scenario id of run A
    incident_b: str  # scenario id of run B
    same_scenario: bool
    same_policy: bool
    # Score rows: {"metric", "label", "unit", "a", "b", "delta"}; a
    # value is None when that run never reached the milestone, and the
    # delta is None when either side is None. ack_credit deltas are
    # rounded to two decimals so --json stays readable.
    score_rows: list
    # Shared timeline events: {"kind", "actor", "detail", "t_a", "t_b",
    # "shift"} where shift is t_b - t_a in minutes.
    timeline_matched: list
    # Events only one run has, as {"t_min", "kind", "actor", "detail"}.
    timeline_only_a: list
    timeline_only_b: list
    # What-if provenance for each side, or None: a WhatIfInfo from
    # replay.load_saved, when that side came from a `what-if --json`
    # file instead of a `run --json` file.
    whatif_a: object = None
    whatif_b: object = None
    # Each side's escalation chain, as a list of step dicts (the same
    # shape as `run --json`'s "escalation_chain"); an empty list when
    # that side's run predates chains (before 0.26.0).
    chain_a: list = None
    chain_b: list = None

    @property
    def chains_identical(self) -> bool:
        """True when both runs walked the same escalation chain."""
        return self.chain_a == self.chain_b

    @property
    def timelines_identical(self) -> bool:
        """True when both runs have the same events at the same times."""
        return (not self.timeline_only_a and not self.timeline_only_b
                and all(item["shift"] == 0
                        for item in self.timeline_matched))

    def to_dict(self) -> dict:
        """JSON-ready dict: runs, score rows, timeline diff."""
        return {
            "run_a": {
                "label": self.label_a,
                "scenario": self.incident_a,
                "what_if": (self.whatif_a.to_dict()
                            if self.whatif_a is not None else None),
            },
            "run_b": {
                "label": self.label_b,
                "scenario": self.incident_b,
                "what_if": (self.whatif_b.to_dict()
                            if self.whatif_b is not None else None),
            },
            "same_scenario": self.same_scenario,
            "same_policy": self.same_policy,
            "score": [
                {
                    "metric": row["metric"],
                    "a": row["a"],
                    "b": row["b"],
                    "delta": row["delta"],
                }
                for row in self.score_rows
            ],
            "timeline": {
                "matched": list(self.timeline_matched),
                "only_in_a": list(self.timeline_only_a),
                "only_in_b": list(self.timeline_only_b),
                "identical": self.timelines_identical,
            },
            "escalation_chain": {
                "a": list(self.chain_a or []),
                "b": list(self.chain_b or []),
                "identical": self.chains_identical,
            },
        }

    def render(self) -> str:
        """Human-readable side-by-side comparison."""
        lines = [
            f"A: {self.label_a} ({self.incident_a})",
            f"B: {self.label_b} ({self.incident_b})",
        ]
        for label, whatif in (("A", self.whatif_a),
                              ("B", self.whatif_b)):
            if whatif is not None:
                delta = whatif.points_delta
                lines.append(
                    f"note: {label} is a what-if replay of its base run "
                    f"({whatif.base_points} points) with overrides: "
                    f"{'; '.join(whatif.overrides)}; "
                    f"{'+' if delta >= 0 else ''}{delta} points vs base")
        if not self.same_scenario:
            lines.append("note: different scenarios — scores are not "
                         "directly comparable")
        if not self.same_policy:
            lines.append("note: different policies")
        lines.append("")
        lines.append("score:")
        for row in self.score_rows:
            a = _fmt_value(row["a"], row["unit"], row["metric"])
            b = _fmt_value(row["b"], row["unit"], row["metric"])
            delta = _fmt_delta(row["delta"], row["unit"], row["metric"])
            lines.append(f"  {row['label']:<17} A: {a:<8} B: {b:<8} "
                         f"Δ: {delta}")
        lines.append("")
        lines.append("timeline:")
        if self.timelines_identical:
            lines.append("  identical")
        else:
            if self.timeline_matched:
                lines.append(
                    f"  shared events ({len(self.timeline_matched)}):")
                for item in self.timeline_matched:
                    shift = _fmt_shift(item["shift"])
                    line = (f"    [{item['t_a']:>4}m -> {item['t_b']:>4}m] "
                            f"{item['kind']:<10} {item['actor']}")
                    if item["detail"]:
                        line += f"  {item['detail']}"
                    if shift:
                        line += f"  ({shift})"
                    lines.append(line)
            if self.timeline_only_a:
                lines.append(
                    f"  only in A ({len(self.timeline_only_a)}):")
                for event in self.timeline_only_a:
                    lines.append(f"    {_fmt_event(event)}")
            if self.timeline_only_b:
                lines.append(
                    f"  only in B ({len(self.timeline_only_b)}):")
                for event in self.timeline_only_b:
                    lines.append(f"    {_fmt_event(event)}")
        if not self.chains_identical:
            lines.append("")
            lines.append("escalation chain:")
            summary_a = _chain_summary_dicts(self.chain_a)
            summary_b = _chain_summary_dicts(self.chain_b)
            lines.append(f"  A: {summary_a or 'no chain recorded'}")
            lines.append(f"  B: {summary_b or 'no chain recorded'}")
        return "\n".join(lines)


def _fmt_value(value, unit: str, metric: str = "") -> str:
    if value is None:
        return "-"
    if metric == "error_budget_burn_pct":
        return f"{value:.1f}%"
    if isinstance(value, float):
        return f"{value:.0%}"
    return f"{value}{unit}"


def _fmt_delta(delta, unit: str, metric: str = "") -> str:
    if delta is None:
        return "-"
    if metric == "error_budget_burn_pct":
        return f"{delta:+.1f}%"
    if isinstance(delta, float):
        return f"{delta:+.0%}"
    return f"{delta:+d}{unit}"


def _fmt_shift(shift: int) -> str:
    if shift == 0:
        return ""
    return f"{shift:+d}m"


def _fmt_event(event: dict) -> str:
    line = (f"[{event['t_min']:>4}m] {event['kind']:<10} "
            f"{event['actor']}")
    if event["detail"]:
        line += f"  {event['detail']}"
    return line


def _score_rows(a: SimulationResult, b: SimulationResult) -> list:
    rows = []
    for metric, label, unit in _SCORE_FIELDS:
        value_a = getattr(a.score, metric)
        value_b = getattr(b.score, metric)
        if value_a is None or value_b is None:
            delta = None
        elif isinstance(value_a, float) or isinstance(value_b, float):
            delta = round(value_b - value_a, 2)
        else:
            delta = value_b - value_a
        rows.append({
            "metric": metric,
            "label": label,
            "unit": unit,
            "a": value_a,
            "b": value_b,
            "delta": delta,
        })
    return rows


def _timeline_diff(a: SimulationResult, b: SimulationResult) -> tuple:
    """Match events across the two timelines by kind/actor/detail.

    Events match greedily in order, so a run that escalates twice
    still pairs the first escalation with the first one in the other
    run. Matched events keep both times so the shift shows; unmatched
    events are listed per run.
    """
    remaining_b = list(b.timeline.events)
    matched = []
    only_a = []
    for event in a.timeline.events:
        key = _event_key(event)
        match = next((other for other in remaining_b
                      if _event_key(other) == key), None)
        if match is None:
            only_a.append(event.to_dict())
        else:
            remaining_b.remove(match)
            matched.append({
                "kind": event.kind,
                "actor": event.actor,
                "detail": event.detail,
                "t_a": event.t_min,
                "t_b": match.t_min,
                "shift": match.t_min - event.t_min,
            })
    only_b = [event.to_dict() for event in remaining_b]
    return matched, only_a, only_b


def _chain_summary_dicts(chain) -> str:
    """One-line summary of an escalation chain (a list of step dicts),
    or None when the run recorded no chain (saved before 0.26.0)."""
    if not chain:
        return None
    parts = []
    for step in chain:
        responder = step["responder"]
        name = f"{responder['name']} ({responder['role']})"
        if step["outcome"] == "acked":
            parts.append(f"{name} acked {step['acked_at_min']}m")
        else:
            parts.append(f"{name} missed {step['ack_timeout_min']}m")
    return " → ".join(parts)


def _same_policy(a: SimulationResult, b: SimulationResult) -> bool:
    if a.policy.name != b.policy.name:
        return False
    return [(level.responder.name, level.responder.role,
             level.ack_timeout_min) for level in a.policy.levels] == [
        (level.responder.name, level.responder.role,
         level.ack_timeout_min) for level in b.policy.levels]


def compare_results(a: SimulationResult, b: SimulationResult,
                    label_a: str = "A", label_b: str = "B",
                    whatif_a=None, whatif_b=None) -> Comparison:
    """Diff two runs: score rows with deltas and a timeline diff.

    ``whatif_a`` / ``whatif_b`` are WhatIfInfo objects (from
    replay.load_saved) when that side came from a `what-if --json`
    file; the comparison then uses the replayed run and flags it.
    """
    matched, only_a, only_b = _timeline_diff(a, b)
    return Comparison(
        label_a=label_a,
        label_b=label_b,
        incident_a=a.incident.id,
        incident_b=b.incident.id,
        same_scenario=a.incident.id == b.incident.id,
        same_policy=_same_policy(a, b),
        score_rows=_score_rows(a, b),
        timeline_matched=matched,
        timeline_only_a=only_a,
        timeline_only_b=only_b,
        whatif_a=whatif_a,
        whatif_b=whatif_b,
        chain_a=[step.to_dict() for step in a.escalation_chain],
        chain_b=[step.to_dict() for step in b.escalation_chain],
    )


@dataclass
class ShiftComparison:
    """Side-by-side diff of two saved shifts.

    Shifts are compared at shift level — shift points and total burn
    against the shared budget, how many incidents each ran, whether
    each ran out of budget — plus a per-incident points table for the
    scenarios both shifts ran. Timelines are not diffed: a shift
    holds several timelines, and the per-incident runs are what
    `compare` is for.
    """
    label_a: str
    label_b: str
    shift_points_a: float
    shift_points_b: float
    total_burn_a: float
    total_burn_b: float
    ran_a: int
    ran_b: int
    skipped_a: list
    skipped_b: list
    exhausted_a: bool
    exhausted_b: bool
    slo_a: float
    slo_b: float
    # Per-incident points for scenarios both shifts ran:
    # {"scenario", "a", "b", "delta"}.
    per_incident: list

    def to_dict(self) -> dict:
        """JSON-ready dict: shift rows and the per-incident table."""
        return {
            "shift_a": {
                "label": self.label_a,
                "shift_points": self.shift_points_a,
                "total_burn_pct": self.total_burn_a,
                "incidents_ran": self.ran_a,
                "skipped": list(self.skipped_a),
                "budget_exhausted": self.exhausted_a,
                "slo_percent": self.slo_a,
            },
            "shift_b": {
                "label": self.label_b,
                "shift_points": self.shift_points_b,
                "total_burn_pct": self.total_burn_b,
                "incidents_ran": self.ran_b,
                "skipped": list(self.skipped_b),
                "budget_exhausted": self.exhausted_b,
                "slo_percent": self.slo_b,
            },
            "same_slo": self.slo_a == self.slo_b,
            "shift_points_delta": round(
                self.shift_points_b - self.shift_points_a, 1),
            "total_burn_delta": round(
                self.total_burn_b - self.total_burn_a, 1),
            "per_incident": [dict(row) for row in self.per_incident],
        }

    def render(self) -> str:
        """Human-readable side-by-side shift comparison."""
        lines = [
            f"A: {self.label_a} ({self.ran_a} ran, "
            f"{len(self.skipped_a)} skipped)",
            f"B: {self.label_b} ({self.ran_b} ran, "
            f"{len(self.skipped_b)} skipped)",
        ]
        if self.slo_a != self.slo_b:
            lines.append("note: different shift SLOs "
                         f"({self.slo_a}% vs {self.slo_b}%) — burn is "
                         "not directly comparable")
        lines.append("")
        lines.append("shift:")
        lines.append(
            f"  {'shift points':<17} A: {self.shift_points_a:<8} "
            f"B: {self.shift_points_b:<8} "
            f"Δ: {self.shift_points_b - self.shift_points_a:+.1f}")
        lines.append(
            f"  {'total burn':<17} A: {self.total_burn_a:.1f}%"
            f"    B: {self.total_burn_b:.1f}%"
            f"    Δ: {self.total_burn_b - self.total_burn_a:+.1f}%")
        lines.append(
            f"  {'budget exhausted':<17} A: "
            f"{'yes' if self.exhausted_a else 'no':<8} B: "
            f"{'yes' if self.exhausted_b else 'no'}")
        if self.per_incident:
            lines.append("")
            lines.append("per incident (both shifts ran it):")
            for row in self.per_incident:
                lines.append(
                    f"  {row['scenario']:<20} A: {row['a']:>3}/100  "
                    f"B: {row['b']:>3}/100  Δ: {row['delta']:+d}")
        return "\n".join(lines)


def compare_shifts(a, b, label_a: str = "A",
                   label_b: str = "B") -> ShiftComparison:
    """Diff two ShiftResults at shift level.

    ``a`` and ``b`` are ShiftResults (from shift.simulate_shift or
    replay.load_shift). The per-incident table covers only the
    scenarios both shifts actually ran.
    """
    points_a = {entry["scenario"]: entry["points"]
                for entry in a.incidents}
    points_b = {entry["scenario"]: entry["points"]
                for entry in b.incidents}
    per_incident = [
        {"scenario": name, "a": points_a[name], "b": points_b[name],
         "delta": points_b[name] - points_a[name]}
        for name in points_a if name in points_b
    ]
    return ShiftComparison(
        label_a=label_a,
        label_b=label_b,
        shift_points_a=a.shift_points,
        shift_points_b=b.shift_points,
        total_burn_a=a.total_burn_pct,
        total_burn_b=b.total_burn_pct,
        ran_a=len(a.runs),
        ran_b=len(b.runs),
        skipped_a=list(a.skipped),
        skipped_b=list(b.skipped),
        exhausted_a=a.budget_exhausted,
        exhausted_b=b.budget_exhausted,
        slo_a=a.slo_percent,
        slo_b=b.slo_percent,
        per_incident=per_incident,
    )
