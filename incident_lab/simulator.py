"""Deterministic incident-response simulation over a simulated clock.

The clock runs in minutes and never sleeps: the whole incident plays out
instantly. The engine pages each responder in policy order, waits for an
ack up to that level's timeout, escalates on a miss, and once someone acks
it walks through mitigate and resolve.

Scoring (0-100, documented in README.md):
- start at 100
- minus 15 per escalation
- minus 1 per minute of time-to-ack past 5 minutes
- minus 1 per 2 minutes of time-to-mitigate past 15 minutes
- an incident nobody acks scores 0
"""

from dataclasses import dataclass

from .incident import Incident
from .policy import EscalationPolicy
from .scenarios import Scenario
from .timeline import Timeline

ACK_GRACE_MIN = 5
MITIGATE_GRACE_MIN = 15
ESCALATION_PENALTY = 15


@dataclass(frozen=True)
class Score:
    time_to_ack_min: object  # int, or None if nobody acked
    time_to_mitigate_min: object  # int, or None if never mitigated
    time_to_resolve_min: object  # int, or None if never resolved
    escalations: int
    resolved: bool
    points: int  # 0..100


@dataclass
class SimulationResult:
    incident: Incident
    policy: EscalationPolicy
    timeline: Timeline
    score: Score


def simulate(
    scenario: Scenario,
    policy: EscalationPolicy,
    max_duration_min: int = 240,
) -> SimulationResult:
    """Run one scenario against one policy and return the full result."""
    incident = Incident(
        id=scenario.name,
        service=scenario.service,
        title=scenario.title,
        severity=scenario.severity,
        description=scenario.description,
    )
    timeline = Timeline()
    timeline.record(0, "declared", "monitor", scenario.title)

    t = 0
    escalations = 0
    acked_at = None
    acked_by = None

    for i, level in enumerate(policy.levels):
        timeline.record(t, "paged", level.responder.name, level.responder.role)
        delay = scenario.ack_delay_min[i] if i < len(scenario.ack_delay_min) else None
        if delay is not None and delay <= level.ack_timeout_min:
            acked_at = t + delay
            acked_by = level.responder.name
            timeline.record(acked_at, "acked", acked_by)
            break
        # Missed ack: wait out the timeout, then escalate (or give up).
        t += level.ack_timeout_min
        if i + 1 < len(policy.levels):
            escalations += 1
            timeline.record(
                t, "escalated", policy.levels[i + 1].responder.name,
                f"no ack from {level.responder.name} in "
                f"{level.ack_timeout_min}m",
            )
        else:
            timeline.record(t, "unacked", "monitor", "nobody acked the page")
            return SimulationResult(
                incident, policy, timeline,
                Score(None, None, None, escalations, False, 0),
            )
        if t > max_duration_min:
            timeline.record(t, "unacked", "monitor", "max duration reached")
            return SimulationResult(
                incident, policy, timeline,
                Score(None, None, None, escalations, False, 0),
            )

    mitigated_at = acked_at + scenario.mitigate_delay_min
    resolved_at = mitigated_at + scenario.resolve_delay_min
    timeline.record(mitigated_at, "mitigated", acked_by)
    timeline.record(resolved_at, "resolved", acked_by)

    points = 100
    points -= ESCALATION_PENALTY * escalations
    points -= max(0, acked_at - ACK_GRACE_MIN)
    points -= max(0, mitigated_at - MITIGATE_GRACE_MIN) // 2
    score = Score(
        time_to_ack_min=acked_at,
        time_to_mitigate_min=mitigated_at,
        time_to_resolve_min=resolved_at,
        escalations=escalations,
        resolved=True,
        points=max(0, points),
    )
    return SimulationResult(incident, policy, timeline, score)
