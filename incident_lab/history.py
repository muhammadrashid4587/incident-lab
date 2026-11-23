"""Per-operator drill history: record practice runs, show improvement.

A drill is practice, and practice only pays off if the operator can
see whether they are getting better. ``drill --operator NAME`` records
the finished drill (or a drill ended early with quit/EOF — a partial
run is still practice) in a history file, and ``history`` reads it
back: ``history --operator NAME`` lists that operator's runs in order
with their scores and a summary — runs, mean and best score, how the
latest score compares to the first, and whether the second half of
their runs scores better than the first — while plain ``history``
summarises every operator the file knows about.

The history file is plain JSON, one file per operator set, so it can
be shared, backed up, or pointed elsewhere with ``--history-file``:

    {"version": 1, "runs": [{"recorded_at": "...", "operator": "ana",
                            "scenario": "disk-full", "difficulty": "normal",
                            "policy": "standard", "points": 96,
                            "grade": "A", "time_to_ack_min": 3, ...}]}

The default file is ``~/.incident-lab/history.json``. Only drills
record runs — ``run`` plays a scenario's fixed behaviour, not an
operator's decisions, so there is nothing to learn from attributing
it. A drill without ``--operator`` records nothing.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics

from .debrief import grade
from .simulator import SimulationResult

HISTORY_VERSION = 1
# Where drills are recorded when --history-file is not given.
DEFAULT_HISTORY_PATH = str(
    Path.home() / ".incident-lab" / "history.json")


@dataclass(frozen=True)
class HistoryEntry:
    """One recorded drill run, as stored in the history file.

    ``recorded_at`` is an ISO 8601 UTC timestamp; ``abandoned`` is
    true when the drill ended early (quit or EOF) instead of resolving.
    The milestone fields are None when the drill never reached them.
    """
    recorded_at: str
    operator: str
    scenario: str
    difficulty: object  # str, or None for a run played before difficulties
    policy: str
    points: int
    grade: str
    time_to_ack_min: object
    time_to_mitigate_min: object
    time_to_resolve_min: object
    escalations: int
    resolved: bool
    hints_used: int
    abandoned: bool

    def to_dict(self) -> dict:
        """JSON-ready dict, in file order."""
        return {
            "recorded_at": self.recorded_at,
            "operator": self.operator,
            "scenario": self.scenario,
            "difficulty": self.difficulty,
            "policy": self.policy,
            "points": self.points,
            "grade": self.grade,
            "time_to_ack_min": self.time_to_ack_min,
            "time_to_mitigate_min": self.time_to_mitigate_min,
            "time_to_resolve_min": self.time_to_resolve_min,
            "escalations": self.escalations,
            "resolved": self.resolved,
            "hints_used": self.hints_used,
            "abandoned": self.abandoned,
        }


def entry_from_result(result: SimulationResult, operator: str,
                      recorded_at: str = None) -> HistoryEntry:
    """Build a history entry from a finished (or partial) drill result.

    ``recorded_at`` defaults to now, in UTC; pass it explicitly to
    record a run at a known time (tests do). The operator name is
    stored as given, so "ana" and "Ana" are different operators —
    pick one spelling and stick to it.
    """
    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc).isoformat()
    score = result.score
    return HistoryEntry(
        recorded_at=recorded_at,
        operator=operator,
        scenario=result.incident.id,
        difficulty=(result.difficulty.name
                    if result.difficulty is not None else None),
        policy=result.policy.name,
        points=score.points,
        grade=grade(score.points),
        time_to_ack_min=score.time_to_ack_min,
        time_to_mitigate_min=score.time_to_mitigate_min,
        time_to_resolve_min=score.time_to_resolve_min,
        escalations=score.escalations,
        resolved=score.resolved,
        hints_used=score.hints_used,
        # A drill that ended early records an "abandoned" timeline
        # event; a finished drill never does.
        abandoned="abandoned" in result.timeline.kinds(),
    )


def _require_str(data: dict, key: str, what: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"history file: {what} {key!r} must be a non-empty string")
    return value


def _require_int_or_none(data: dict, key: str, what: str):
    value = data.get(key)
    if value is not None and (isinstance(value, bool)
                              or not isinstance(value, int)):
        raise ValueError(f"history file: {what} {key!r} must be an "
                         "integer or null")
    return value


def _entry_from_dict(raw) -> HistoryEntry:
    """Rebuild one history entry from its stored dict; raises
    ValueError on a wrong shape or an invalid value."""
    if not isinstance(raw, dict):
        raise ValueError("history file: each run must be an object")
    points = raw.get("points")
    if isinstance(points, bool) or not isinstance(points, int):
        raise ValueError("history file: run 'points' must be an integer")
    escalations = raw.get("escalations")
    if isinstance(escalations, bool) or not isinstance(escalations, int):
        raise ValueError(
            "history file: run 'escalations' must be an integer")
    resolved = raw.get("resolved")
    if not isinstance(resolved, bool):
        raise ValueError("history file: run 'resolved' must be a boolean")
    abandoned = raw.get("abandoned")
    if not isinstance(abandoned, bool):
        raise ValueError("history file: run 'abandoned' must be a boolean")
    hints_used = raw.get("hints_used")
    if (isinstance(hints_used, bool) or not isinstance(hints_used, int)
            or hints_used < 0):
        raise ValueError("history file: run 'hints_used' must be a "
                         "non-negative integer")
    difficulty = raw.get("difficulty")
    if difficulty is not None and not isinstance(difficulty, str):
        raise ValueError(
            "history file: run 'difficulty' must be a string or null")
    return HistoryEntry(
        recorded_at=_require_str(raw, "recorded_at", "run"),
        operator=_require_str(raw, "operator", "run"),
        scenario=_require_str(raw, "scenario", "run"),
        difficulty=difficulty,
        policy=_require_str(raw, "policy", "run"),
        points=points,
        grade=_require_str(raw, "grade", "run"),
        time_to_ack_min=_require_int_or_none(raw, "time_to_ack_min",
                                             "run"),
        time_to_mitigate_min=_require_int_or_none(
            raw, "time_to_mitigate_min", "run"),
        time_to_resolve_min=_require_int_or_none(
            raw, "time_to_resolve_min", "run"),
        escalations=escalations,
        resolved=resolved,
        hints_used=hints_used,
        abandoned=abandoned,
    )


def load_history(path: str) -> list:
    """Load the recorded runs from a history file, in file order.

    A missing file means no drills recorded yet, so it loads as an
    empty list. Raises json.JSONDecodeError on bad JSON and
    ValueError on a wrong shape or an invalid entry.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    if not isinstance(data, dict):
        raise ValueError("history file: expected a JSON object")
    version = data.get("version")
    if version != HISTORY_VERSION:
        raise ValueError(
            f"history file: unsupported version {version!r} "
            f"(expected {HISTORY_VERSION})")
    raw_runs = data.get("runs")
    if not isinstance(raw_runs, list):
        raise ValueError("history file: 'runs' must be a list")
    return [_entry_from_dict(raw) for raw in raw_runs]


def record_run(path: str, entry: HistoryEntry) -> None:
    """Append one entry to a history file, creating it (and its parent
    directory) when needed. The existing runs are kept in file order,
    so the newest run is always last.
    """
    entries = load_history(path)
    entries.append(entry)
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(
            {"version": HISTORY_VERSION,
             "runs": [item.to_dict() for item in entries]},
            indent=2) + "\n")


def entries_for_operator(entries: list, operator: str) -> list:
    """The entries for one operator, in the order they were recorded."""
    return [entry for entry in entries if entry.operator == operator]


@dataclass
class OperatorSummary:
    """One operator's record: what they have done and how it is trending.

    ``points_delta`` is the latest score minus the first — the
    headline improvement — and ``trend`` compares the mean of the
    second half of their runs with the mean of the first half:
    improving when the second half scores at least 1 point better,
    declining when it scores at least 1 point worse, steady otherwise.
    With fewer than two runs there is nothing to compare, so the
    trend is "not enough runs yet".
    """
    operator: str
    runs: int
    points_mean: float
    points_best: int
    points_first: int
    points_latest: int
    points_delta: int
    mean_time_to_ack_min: object  # None when no run was ever acked
    resolved: int
    abandoned: int
    trend: str

    def to_dict(self) -> dict:
        """JSON-ready dict."""
        return {
            "operator": self.operator,
            "runs": self.runs,
            "points_mean": self.points_mean,
            "points_best": self.points_best,
            "points_first": self.points_first,
            "points_latest": self.points_latest,
            "points_delta": self.points_delta,
            "mean_time_to_ack_min": self.mean_time_to_ack_min,
            "resolved": self.resolved,
            "abandoned": self.abandoned,
            "trend": self.trend,
        }

    def render(self, entries: list) -> str:
        """Human-readable view: every run in order, then the summary."""
        lines = [f"operator: {self.operator} ({self.runs} drill(s))",
                 ""]
        lines.append("runs:")
        for index, entry in enumerate(entries, start=1):
            ack = (f"{entry.time_to_ack_min}m"
                   if entry.time_to_ack_min is not None else "-")
            mitigate = (f"{entry.time_to_mitigate_min}m"
                        if entry.time_to_mitigate_min is not None else "-")
            resolve = (f"{entry.time_to_resolve_min}m"
                       if entry.time_to_resolve_min is not None else "-")
            status = ("abandoned" if entry.abandoned
                      else "resolved" if entry.resolved else "unresolved")
            lines.append(
                f"  {index}. {entry.recorded_at}  {entry.scenario} "
                f"[{entry.difficulty or '-'}]  {entry.points}/100 "
                f"(grade {entry.grade})  ack {ack}  mitigate {mitigate}  "
                f"resolve {resolve}  {entry.escalations} esc.  {status}")
        lines.append("")
        lines.append("summary:")
        lines.append(f"  mean score: {self.points_mean:.1f}/100")
        lines.append(f"  best score: {self.points_best}/100")
        lines.append(f"  first -> latest: {self.points_first} -> "
                     f"{self.points_latest} "
                     f"({self.points_delta:+d} pts)")
        lines.append(f"  trend: {self.trend}")
        lines.append(f"  mean time-to-ack: "
                     f"{self.mean_time_to_ack_min:.1f}m"
                     if self.mean_time_to_ack_min is not None
                     else "  mean time-to-ack: -")
        lines.append(f"  resolved: {self.resolved}/{self.runs}")
        if self.abandoned:
            lines.append(f"  abandoned: {self.abandoned}")
        return "\n".join(lines)


def summarize_operator(entries: list, operator: str) -> OperatorSummary:
    """Summarise one operator's runs. Raises ValueError when the
    operator has no recorded runs."""
    runs = entries_for_operator(entries, operator)
    if not runs:
        raise ValueError(f"no drills recorded for operator {operator!r}")
    points = [entry.points for entry in runs]
    trend = "not enough runs yet"
    if len(runs) >= 2:
        half = len(runs) // 2
        first_half = statistics.mean(points[:half])
        second_half = statistics.mean(points[half:])
        if second_half - first_half >= 1:
            trend = "improving"
        elif first_half - second_half >= 1:
            trend = "declining"
        else:
            trend = "steady"
    acked = [entry.time_to_ack_min for entry in runs
             if entry.time_to_ack_min is not None]
    return OperatorSummary(
        operator=operator,
        runs=len(runs),
        points_mean=statistics.mean(points),
        points_best=max(points),
        points_first=points[0],
        points_latest=points[-1],
        points_delta=points[-1] - points[0],
        mean_time_to_ack_min=(statistics.mean(acked) if acked else None),
        resolved=sum(1 for entry in runs if entry.resolved),
        abandoned=sum(1 for entry in runs if entry.abandoned),
        trend=trend,
    )


def summarize_all(entries: list) -> list:
    """One summary per operator in the history, in first-run order."""
    operators = []
    for entry in entries:
        if entry.operator not in operators:
            operators.append(entry.operator)
    return [summarize_operator(entries, operator)
            for operator in operators]


def render_all_operators(summaries: list) -> str:
    """Human-readable overview: one line per operator."""
    lines = ["operators:"]
    for summary in summaries:
        lines.append(
            f"  {summary.operator:<16} {summary.runs} drill(s)  "
            f"mean {summary.points_mean:.1f}/100  "
            f"best {summary.points_best}/100  "
            f"latest {summary.points_latest}/100  "
            f"{summary.trend}")
    return "\n".join(lines)
