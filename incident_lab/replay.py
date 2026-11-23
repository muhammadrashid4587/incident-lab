"""Replay a saved incident-lab run.

`run --json` writes the whole result as machine-readable JSON. This module
reads that JSON back and rebuilds the SimulationResult, so the CLI can
re-print the timeline and score in the same human-readable format `run`
uses — no re-simulation, just a faithful re-render of the saved run.

Raises FileNotFoundError if the file is missing, json.JSONDecodeError on
bad JSON, and ValueError on a wrong shape or an invalid value.
"""

import json

from .incident import Incident
from .policy import EscalationPolicy, Level, Responder
from .simulator import Score, SimulationResult
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
    )


def load_result(path: str) -> SimulationResult:
    """Load a saved `run --json` result and rebuild it for re-printing."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("replay file: expected a JSON object")
    seed = data.get("seed")
    if seed is not None and (isinstance(seed, bool)
                             or not isinstance(seed, int)):
        raise ValueError("replay file: 'seed' must be an integer or null")
    return SimulationResult(
        incident=_incident_from_dict(data.get("incident")),
        policy=_policy_from_dict(data.get("policy")),
        timeline=_timeline_from_dict(data.get("timeline")),
        score=_score_from_dict(data.get("score")),
        seed=seed,
    )
