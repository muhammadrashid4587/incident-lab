"""Incident model for incident-lab."""

from dataclasses import dataclass

SEVERITIES = ("low", "medium", "high", "critical")


@dataclass(frozen=True)
class Incident:
    """One incident, as declared by monitoring."""

    id: str
    service: str
    title: str
    severity: str
    description: str = ""

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"severity must be one of {SEVERITIES}, got {self.severity!r}"
            )
