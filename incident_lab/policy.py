"""Escalation policies for incident-lab.

A policy is an ordered chain of responders. Each level has an ack timeout
in minutes: if the paged responder does not acknowledge the incident within
that window, the incident escalates to the next level.
"""

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
