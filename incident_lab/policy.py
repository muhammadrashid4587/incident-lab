"""Escalation policies for incident-lab.

A policy is an ordered chain of responders. Each level has an ack timeout
in minutes: if the paged responder does not acknowledge the incident within
that window, the incident escalates to the next level.
"""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Responder:
    """One person on the escalation chain."""

    name: str
    role: str  # e.g. "primary", "secondary", "manager"


@dataclass(frozen=True)
class Level:
    """One step of the escalation chain."""

    responder: Responder
    ack_timeout_min: int

    def __post_init__(self) -> None:
        if self.ack_timeout_min <= 0:
            raise ValueError("ack timeout must be a positive number of minutes")


@dataclass(frozen=True)
class EscalationPolicy:
    """The full escalation chain for an incident."""

    name: str
    levels: tuple

    def __post_init__(self) -> None:
        if not self.levels:
            raise ValueError("a policy needs at least one escalation level")


#: The default chain: primary gets 5 minutes, secondary 5, manager 10.
DEFAULT_POLICY = EscalationPolicy(
    name="standard",
    levels=(
        Level(Responder("ana", "primary"), 5),
        Level(Responder("ben", "secondary"), 5),
        Level(Responder("carla", "manager"), 10),
    ),
)

#: Same responders, tighter windows: for critical services where every
#: minute of paging delay costs more than the risk of a hasty escalation.
FAST_POLICY = EscalationPolicy(
    name="fast",
    levels=(
        Level(Responder("ana", "primary"), 2),
        Level(Responder("ben", "secondary"), 2),
        Level(Responder("carla", "manager"), 5),
    ),
)

POLICIES = {
    DEFAULT_POLICY.name: DEFAULT_POLICY,
    FAST_POLICY.name: FAST_POLICY,
}


def get_policy(name: str) -> EscalationPolicy:
    """Return a built-in policy by name, or raise KeyError."""
    return POLICIES[name]


def list_policies() -> list:
    """Built-in policy names, in definition order."""
    return list(POLICIES)


def _policy_from_dict(name: str, data) -> EscalationPolicy:
    """Build one policy from its JSON mapping, validating as we go."""
    if not isinstance(data, dict):
        raise ValueError(f"policy {name!r}: expected an object")
    raw_levels = data.get("levels")
    if not isinstance(raw_levels, list) or not raw_levels:
        raise ValueError(
            f"policy {name!r}: 'levels' must be a non-empty list")
    levels = []
    for index, raw in enumerate(raw_levels):
        if not isinstance(raw, dict):
            raise ValueError(
                f"policy {name!r} level {index}: expected an object")
        responder_name = raw.get("name")
        role = raw.get("role")
        timeout = raw.get("ack_timeout_min")
        if not isinstance(responder_name, str) or not responder_name:
            raise ValueError(
                f"policy {name!r} level {index}: 'name' must be a "
                "non-empty string")
        if not isinstance(role, str) or not role:
            raise ValueError(
                f"policy {name!r} level {index}: 'role' must be a "
                "non-empty string")
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ValueError(
                f"policy {name!r} level {index}: 'ack_timeout_min' must "
                "be an integer number of minutes")
        levels.append(Level(Responder(responder_name, role), timeout))
    return EscalationPolicy(name=name, levels=tuple(levels))


def load_policy_file(path: str) -> dict:
    """Load named policies from a JSON file.

    The file is an object with a "policies" mapping, each entry a policy
    with a non-empty "levels" list of {"name", "role", "ack_timeout_min"}
    objects::

        {"policies": {"relaxed": {"levels": [
            {"name": "ana", "role": "primary", "ack_timeout_min": 10},
            {"name": "ben", "role": "secondary", "ack_timeout_min": 10},
            {"name": "carla", "role": "manager", "ack_timeout_min": 20}
        ]}}}

    Returns a dict of name -> EscalationPolicy. Raises FileNotFoundError
    if the file is missing, json.JSONDecodeError on bad JSON, and
    ValueError on a wrong shape or an invalid level.
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("policy file: expected a JSON object")
    raw_policies = data.get("policies")
    if not isinstance(raw_policies, dict) or not raw_policies:
        raise ValueError(
            "policy file: 'policies' must be a non-empty object mapping "
            "policy names to policies")
    policies = {}
    for name, raw in raw_policies.items():
        if not isinstance(name, str) or not name:
            raise ValueError("policy file: policy names must be non-empty")
        policies[name] = _policy_from_dict(name, raw)
    return policies
