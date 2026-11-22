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


def _parse_timeout(text: str, what: str) -> int:
    """Parse one ack timeout in minutes, or raise ValueError."""
    if not text.isdigit():
        raise ValueError(
            f"ack timeout {what}: {text!r} is not a positive integer "
            "number of minutes")
    value = int(text)
    if value <= 0:
        raise ValueError(
            f"ack timeout {what}: {value} is not a positive number "
            "of minutes")
    return value


def parse_ack_timeout_overrides(specs, policy: EscalationPolicy) -> EscalationPolicy:
    """Apply per-level ack timeout overrides to a policy.

    ``specs`` is the list of tokens from ``--ack-timeouts``. Each token is
    either a comma-separated list of timeouts in level order
    (``"120,300,600"``) or a single named override (``"primary=120"``,
    ``"L2=300"``). A positional list must cover every level; named
    overrides may cover any subset and leave the rest alone. Timeouts are
    positive integers in minutes.

    Named levels are matched against the level's role (``primary``,
    ``secondary``, ``manager``) or its 1-based position (``L1``, ``L2``,
    ...). Raises ValueError with a clear message on a bad value, an
    unknown level or role, a wrong number of timeouts, or a mix of the
    two forms.
    """
    roles = [level.responder.role.lower() for level in policy.levels]
    positional = []
    named = {}
    named_seen = False
    positional_seen = False
    for spec in specs:
        if "=" in spec:
            named_seen = True
            if positional_seen:
                raise ValueError(
                    "cannot mix named overrides (role=minutes) with a "
                    "positional list of timeouts")
            key, _, raw_value = spec.partition("=")
            key = key.strip().lower()
            if not key:
                raise ValueError(
                    f"ack timeout override {spec!r}: missing level name")
            index = _override_level_index(key, roles)
            if key in named or index in named:
                raise ValueError(
                    f"ack timeout override {spec!r}: level {key!r} given "
                    "more than once")
            named[index] = _parse_timeout(raw_value.strip(), f"for {key!r}")
        else:
            positional_seen = True
            if named_seen:
                raise ValueError(
                    "cannot mix a positional list of timeouts with named "
                    "overrides (role=minutes)")
            for part in spec.split(","):
                part = part.strip()
                if not part:
                    raise ValueError(
                        f"ack timeout override {spec!r}: empty timeout")
                positional.append(_parse_timeout(
                    part, f"at position {len(positional) + 1}"))
    if positional_seen:
        if len(positional) != len(policy.levels):
            level_names = ", ".join(roles)
            raise ValueError(
                f"expected {len(policy.levels)} ack timeouts (one per "
                f"level: {level_names}), got {len(positional)}")
        timeouts = positional
    else:
        if not named:
            raise ValueError("no ack timeout overrides given")
        timeouts = [
            named.get(i, level.ack_timeout_min)
            for i, level in enumerate(policy.levels)
        ]
    from dataclasses import replace
    levels = tuple(
        replace(level, ack_timeout_min=timeout)
        for level, timeout in zip(policy.levels, timeouts)
    )
    return replace(policy, levels=levels)


def _override_level_index(key: str, roles: list) -> int:
    """Resolve a named override key to a 0-based level index."""
    if key.startswith("l") and key[1:].isdigit():
        index = int(key[1:]) - 1
        if 0 <= index < len(roles):
            return index
        raise ValueError(
            f"unknown escalation level {key!r}; this policy has "
            f"{len(roles)} level(s)")
    matches = [i for i, role in enumerate(roles) if role == key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"role {key!r} matches more than one level; use L1, L2, ... "
            "instead")
    level_names = ", ".join(roles)
    raise ValueError(
        f"unknown escalation level {key!r}; levels are {level_names} "
        f"(or L1..L{len(roles)})")


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
