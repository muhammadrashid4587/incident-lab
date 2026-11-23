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

    @property
    def timelines_identical(self) -> bool:
        """True when both runs have the same events at the same times."""
        return (not self.timeline_only_a and not self.timeline_only_b
                and all(item["shift"] == 0
                        for item in self.timeline_matched))

    def to_dict(self) -> dict:
        """JSON-ready dict: runs, score rows, timeline diff."""
        return {
            "run_a": {"label": self.label_a, "scenario": self.incident_a},
            "run_b": {"label": self.label_b, "scenario": self.incident_b},
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
        }

    def render(self) -> str:
        """Human-readable side-by-side comparison."""
        lines = [
            f"A: {self.label_a} ({self.incident_a})",
            f"B: {self.label_b} ({self.incident_b})",
        ]
        if not self.same_scenario:
            lines.append("note: different scenarios — scores are not "
                         "directly comparable")
        if not self.same_policy:
            lines.append("note: different policies")
        lines.append("")
        lines.append("score:")
        for row in self.score_rows:
            a = _fmt_value(row["a"], row["unit"])
            b = _fmt_value(row["b"], row["unit"])
            delta = _fmt_delta(row["delta"], row["unit"])
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
        return "\n".join(lines)


def _fmt_value(value, unit: str) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.0%}"
    return f"{value}{unit}"


def _fmt_delta(delta, unit: str) -> str:
    if delta is None:
        return "-"
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


def _same_policy(a: SimulationResult, b: SimulationResult) -> bool:
    if a.policy.name != b.policy.name:
        return False
    return [(level.responder.name, level.responder.role,
             level.ack_timeout_min) for level in a.policy.levels] == [
        (level.responder.name, level.responder.role,
         level.ack_timeout_min) for level in b.policy.levels]


def compare_results(a: SimulationResult, b: SimulationResult,
                    label_a: str = "A", label_b: str = "B") -> Comparison:
    """Diff two runs: score rows with deltas and a timeline diff."""
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
    )
