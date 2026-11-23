"""Replay a saved incident-lab run.

`run --json` writes the whole result as machine-readable JSON. This module
reads that JSON back and rebuilds the SimulationResult, so the CLI can
re-print the timeline and score in the same human-readable format `run`
uses — no re-simulation, just a faithful re-render of the saved run.

Raises FileNotFoundError if the file is missing, json.JSONDecodeError on
bad JSON, and ValueError on a wrong shape or an invalid value.
"""

import json
from dataclasses import dataclass

from .difficulty import parse_difficulty
from .incident import Incident
from .policy import EscalationPolicy, Level, Responder
from .simulator import EscalationStep, Hint, Score, SimulationResult
from .timeline import Timeline


def _require_mapping(data, what: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError(f"replay file: {what} must be an object")
    return data


def _require_str(data: dict, key: str, what: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"replay file: {what} {key!r} must be a "
                         "non-empty string")
    return value


def _require_int_or_none(data: dict, key: str, what: str):
    value = data.get(key)
    if value is not None and (isinstance(value, bool)
                              or not isinstance(value, int)):
        raise ValueError(f"replay file: {what} {key!r} must be an integer "
                         "or null")
    return value


def _incident_from_dict(raw) -> Incident:
    data = _require_mapping(raw, "incident")
    return Incident(
        id=_require_str(data, "id", "incident"),
        service=_require_str(data, "service", "incident"),
        title=_require_str(data, "title", "incident"),
        severity=_require_str(data, "severity", "incident"),
        description=data.get("description", "") or "",
    )


def _policy_from_dict(raw) -> EscalationPolicy:
    data = _require_mapping(raw, "policy")
    name = _require_str(data, "name", "policy")
    raw_levels = data.get("levels")
    if not isinstance(raw_levels, list) or not raw_levels:
        raise ValueError("replay file: policy 'levels' must be a "
                         "non-empty list")
    levels = []
    for index, raw_level in enumerate(raw_levels):
        level_data = _require_mapping(raw_level, f"policy level {index}")
        responder_data = _require_mapping(
            level_data.get("responder"), f"policy level {index} responder")
        responder = Responder(
            name=_require_str(responder_data, "name",
                              f"policy level {index} responder"),
            role=_require_str(responder_data, "role",
                              f"policy level {index} responder"),
        )
        timeout = level_data.get("ack_timeout_min")
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ValueError(
                f"replay file: policy level {index} 'ack_timeout_min' "
                "must be an integer number of minutes")
        levels.append(Level(responder, timeout))
    return EscalationPolicy(name=name, levels=tuple(levels))


def _timeline_from_dict(raw) -> Timeline:
    data = _require_mapping(raw, "timeline")
    raw_events = data.get("events")
    if not isinstance(raw_events, list):
        raise ValueError("replay file: timeline 'events' must be a list")
    timeline = Timeline()
    for index, raw_event in enumerate(raw_events):
        event = _require_mapping(raw_event, f"timeline event {index}")
        t_min = event.get("t_min")
        if isinstance(t_min, bool) or not isinstance(t_min, int):
            raise ValueError(f"replay file: timeline event {index} "
                             "'t_min' must be an integer number of minutes")
        kind = _require_str(event, "kind", f"timeline event {index}")
        actor = _require_str(event, "actor", f"timeline event {index}")
        detail = event.get("detail", "") or ""
        if not isinstance(detail, str):
            raise ValueError(f"replay file: timeline event {index} "
                             "'detail' must be a string")
        try:
            timeline.record(t_min, kind, actor, detail)
        except ValueError as exc:
            raise ValueError(f"replay file: timeline event {index}: {exc}")
    return timeline


def _require_number_or_none(data: dict, key: str, what: str):
    value = data.get(key)
    if value is not None and (isinstance(value, bool)
                              or not isinstance(value, (int, float))):
        raise ValueError(f"replay file: {what} {key!r} must be a number "
                         "or null")
    if value is not None and not 0.0 <= value <= 1.0:
        raise ValueError(f"replay file: {what} {key!r} must be between "
                         "0 and 1")
    return value


def _score_from_dict(raw) -> Score:
    data = _require_mapping(raw, "score")
    escalations = data.get("escalations")
    if isinstance(escalations, bool) or not isinstance(escalations, int):
        raise ValueError("replay file: score 'escalations' must be an "
                         "integer")
    resolved = data.get("resolved")
    if not isinstance(resolved, bool):
        raise ValueError("replay file: score 'resolved' must be a boolean")
    points = data.get("points")
    if isinstance(points, bool) or not isinstance(points, int):
        raise ValueError("replay file: score 'points' must be an integer")
    ack_late_min = _require_int_or_none(data, "ack_late_min", "score")
    if ack_late_min is not None and ack_late_min < 0:
        raise ValueError("replay file: score 'ack_late_min' must not be "
                         "negative")
    return Score(
        time_to_ack_min=_require_int_or_none(data, "time_to_ack_min",
                                             "score"),
        time_to_mitigate_min=_require_int_or_none(
            data, "time_to_mitigate_min", "score"),
        time_to_resolve_min=_require_int_or_none(
            data, "time_to_resolve_min", "score"),
        escalations=escalations,
        resolved=resolved,
        points=points,
        ack_late_min=ack_late_min,
        ack_credit=_require_number_or_none(data, "ack_credit", "score"),
        # Saved runs from before 0.11.0 have no delay fields; they
        # round-trip as null.
        mitigate_delay_min=_require_int_or_none(
            data, "mitigate_delay_min", "score"),
        resolve_delay_min=_require_int_or_none(
            data, "resolve_delay_min", "score"),
        # Saved runs from before 0.17.0 have no hint fields; they
        # round-trip as zero hints at zero cost.
        hints_used=_require_hint_count(data, "hints_used"),
        hint_cost_points=_require_hint_count(data, "hint_cost_points"),
    )


def _require_hint_count(data: dict, key: str) -> int:
    """Parse an optional score hint count; missing means no hints."""
    value = data.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f"replay file: score {key!r} must be a non-negative integer")
    return value


def _hints_from_dict(raw) -> list:
    """Parse an optional top-level ``hints`` list of used hints.

    Saved runs from before 0.17.0 have no hints key; they round-trip
    as no hints.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("replay file: 'hints' must be a list or null")
    hints = []
    for index, raw_hint in enumerate(raw):
        hint_data = _require_mapping(raw_hint, f"hint {index}")
        text = _require_str(hint_data, "text", f"hint {index}")
        cost = hint_data.get("cost_points")
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
            raise ValueError(
                f"replay file: hint {index} 'cost_points' must be a "
                "non-negative integer")
        hints.append(Hint(text=text, cost_points=cost))
    return hints


def _escalation_chain_from_dict(raw) -> list:
    """Parse an optional top-level ``escalation_chain`` list.

    Saved runs from before 0.26.0 have no chain key; they round-trip
    as an empty chain. Each step records the level walked, the
    responder, the ack timeout, whether they acked or missed, and the
    impact in effect when they were paged.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            "replay file: 'escalation_chain' must be a list or null")
    chain = []
    for index, raw_step in enumerate(raw):
        step = _require_mapping(raw_step, f"escalation step {index}")
        level = step.get("level")
        if isinstance(level, bool) or not isinstance(level, int) \
                or level < 1:
            raise ValueError(
                f"replay file: escalation step {index} 'level' must be "
                "a positive integer")
        responder = _require_mapping(
            step.get("responder"), f"escalation step {index} responder")
        timeout = step.get("ack_timeout_min")
        if isinstance(timeout, bool) or not isinstance(timeout, int) \
                or timeout <= 0:
            raise ValueError(
                f"replay file: escalation step {index} 'ack_timeout_min' "
                "must be a positive integer number of minutes")
        paged_at = step.get("paged_at_min")
        if isinstance(paged_at, bool) or not isinstance(paged_at, int) \
                or paged_at < 0:
            raise ValueError(
                f"replay file: escalation step {index} 'paged_at_min' "
                "must be a non-negative integer number of minutes")
        outcome = step.get("outcome")
        if outcome not in ("acked", "missed"):
            raise ValueError(
                f"replay file: escalation step {index} 'outcome' must "
                "be 'acked' or 'missed'")
        impact = step.get("impact", "")
        if not isinstance(impact, str):
            raise ValueError(
                f"replay file: escalation step {index} 'impact' must be "
                "a string")
        chain.append(EscalationStep(
            level=level,
            responder_name=_require_str(
                responder, "name", f"escalation step {index} responder"),
            responder_role=_require_str(
                responder, "role", f"escalation step {index} responder"),
            ack_timeout_min=timeout,
            paged_at_min=paged_at,
            outcome=outcome,
            acked_at_min=_require_int_or_none(
                step, "acked_at_min", f"escalation step {index}"),
            ack_late_min=_require_int_or_none(
                step, "ack_late_min", f"escalation step {index}"),
            impact=impact,
        ))
    return chain


def load_result(path: str) -> SimulationResult:
    """Load a saved `run --json` result and rebuild it for re-printing."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return _result_from_dict(data)


@dataclass
class WhatIfInfo:
    """Provenance of a what-if replay loaded from a saved file.

    ``overrides`` are the human-readable override descriptions, in
    the order applied; ``points_delta`` is how the replay scored
    against its base run, and ``base_points`` is the base run's
    score. ``base_result`` is the base run itself, for callers that
    want to compare the replay against the run it came from.
    """
    overrides: list
    points_delta: int
    base_points: int
    base_result: object  # SimulationResult, the run that was replayed

    def to_dict(self) -> dict:
        """JSON-ready dict: overrides, delta and base score."""
        return {
            "overrides": list(self.overrides),
            "points_delta": self.points_delta,
            "base_points": self.base_points,
        }


@dataclass
class LoadedRun:
    """A saved run file: the run to use, plus what-if provenance.

    ``result`` is the run itself for a normal `run --json` file, or
    the replayed run for a `what-if --json` file; ``what_if`` is
    None for a normal run.
    """
    result: object  # SimulationResult
    what_if: object = None  # WhatIfInfo, or None


def load_saved(path: str) -> LoadedRun:
    """Load a saved run or a saved what-if result.

    A `what-if --json` file carries both runs under "original" and
    "what_if" plus the overrides and the points delta; it loads as
    the replayed run, with the base run kept as provenance. A normal
    `run --json` file loads as itself, with no provenance. Raises
    FileNotFoundError if the file is missing, json.JSONDecodeError
    on bad JSON, and ValueError on a wrong shape or an invalid value
    (including a what-if file whose delta does not match its runs).
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("replay file: expected a JSON object")
    if "what_if" in data or "original" in data:
        return _load_whatif(data)
    return LoadedRun(result=_result_from_dict(data))


def _load_whatif(data: dict) -> LoadedRun:
    """Load a `what-if --json` file as its replayed run."""
    original = data.get("original")
    replayed = data.get("what_if")
    if not isinstance(original, dict) or not isinstance(replayed, dict):
        raise ValueError("replay file: a what-if file must carry "
                         "'original' and 'what_if' runs")
    overrides = data.get("overrides")
    if (not isinstance(overrides, list)
            or any(not isinstance(item, str) for item in overrides)):
        raise ValueError("replay file: a what-if file's 'overrides' "
                         "must be a list of strings")
    points_delta = data.get("points_delta")
    if isinstance(points_delta, bool) or not isinstance(points_delta, int):
        raise ValueError("replay file: a what-if file's 'points_delta' "
                         "must be an integer")
    base = _result_from_dict(original)
    result = _result_from_dict(replayed)
    if base.incident.id != result.incident.id:
        raise ValueError("replay file: a what-if file's replay must be "
                         "of the same incident as its base run")
    if result.score.points - base.score.points != points_delta:
        raise ValueError("replay file: a what-if file's 'points_delta' "
                         "does not match its runs' scores")
    return LoadedRun(
        result=result,
        what_if=WhatIfInfo(
            overrides=list(overrides),
            points_delta=points_delta,
            base_points=base.score.points,
            base_result=base))


def _result_from_dict(data: dict) -> SimulationResult:
    """Rebuild a SimulationResult from one saved run's JSON dict."""
    if not isinstance(data, dict):
        raise ValueError("replay file: expected a JSON object")
    seed = data.get("seed")
    if seed is not None and (isinstance(seed, bool)
                             or not isinstance(seed, int)):
        raise ValueError("replay file: 'seed' must be an integer or null")
    raw_jitter = data.get("jitter")
    jitter = None
    if raw_jitter is not None:
        if (not isinstance(raw_jitter, list) or len(raw_jitter) != 2
                or any(isinstance(v, bool) or not isinstance(v, int)
                       for v in raw_jitter)
                or raw_jitter[0] < 0 or raw_jitter[1] < raw_jitter[0]):
            raise ValueError(
                "replay file: 'jitter' must be [lo, hi] minutes or null")
        jitter = tuple(raw_jitter)
    raw_difficulty = data.get("difficulty")
    difficulty = None
    if raw_difficulty is not None:
        if not isinstance(raw_difficulty, str):
            raise ValueError(
                "replay file: 'difficulty' must be a string or null")
        try:
            difficulty = parse_difficulty(raw_difficulty)
        except ValueError as exc:
            raise ValueError(f"replay file: {exc}")
    return SimulationResult(
        incident=_incident_from_dict(data.get("incident")),
        policy=_policy_from_dict(data.get("policy")),
        timeline=_timeline_from_dict(data.get("timeline")),
        score=_score_from_dict(data.get("score")),
        seed=seed,
        jitter=jitter,
        difficulty=difficulty,
        hints=_hints_from_dict(data.get("hints")),
        escalation_chain=_escalation_chain_from_dict(
            data.get("escalation_chain")),
    )
