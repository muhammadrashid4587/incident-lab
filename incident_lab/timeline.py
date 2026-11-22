"""Append-only timeline of incident events.

Times are minutes since the incident was declared. Events must be recorded
in non-decreasing time order; the timeline refuses out-of-order writes, so
a replay always reads as a real sequence of events.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Event:
    t_min: int
    kind: str  # declared, paged, acked, escalated, mitigated, resolved, unacked
    actor: str
    detail: str = ""


class Timeline:
    """An ordered log of events for one incident."""

    def __init__(self) -> None:
        self.events: list = []

    def record(self, t_min: int, kind: str, actor: str, detail: str = "") -> Event:
        if self.events and t_min < self.events[-1].t_min:
            raise ValueError(
                f"events must be recorded in time order: {t_min} < "
                f"{self.events[-1].t_min}"
            )
        event = Event(t_min, kind, actor, detail)
        self.events.append(event)
        return event

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self):
        return iter(self.events)

    def kinds(self) -> list:
        return [event.kind for event in self.events]

    def render(self) -> str:
        """Human-readable replay, one event per line."""
        lines = []
        for event in self.events:
            line = f"[{event.t_min:>4}m] {event.kind:<10} {event.actor}"
            if event.detail:
                line += f"  {event.detail}"
            lines.append(line)
        return "\n".join(lines)
