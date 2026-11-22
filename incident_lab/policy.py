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
