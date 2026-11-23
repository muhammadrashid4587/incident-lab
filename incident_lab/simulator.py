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
- a late ack (after its level's timeout) still counts, but the score is
  scaled by partial credit: full credit up to the timeout, then linear
  decay to zero at twice the timeout
"""

from dataclasses import dataclass
import random

from .incident import Incident
from .policy import EscalationPolicy
from .scenarios import Scenario
from .timeline import Timeline

ACK_GRACE_MIN = 5
MITIGATE_GRACE_MIN = 15
ESCALATION_PENALTY = 15
# A late ack earns partial credit that decays linearly to zero when the
# ack delay reaches this many times the level's ack timeout.
LATE_ACK_CREDIT_WINDOW = 2
# With --seed, each ack delay is jittered by this many minutes either way
# (drawn from a seeded RNG), so the same seed replays the same run while
# different seeds vary the response times. The jitter is small enough
# that a scenario's structure (who acks, who misses) never changes.
SEED_JITTER_MIN = 1


def late_ack_credit(ack_delay_min: int, ack_timeout_min: int) -> float:
    """Partial credit for an ack that arrives after its level's timeout.

    Full credit (1.0) up to the timeout, then linear decay to zero at
    ``LATE_ACK_CREDIT_WINDOW`` times the timeout; an ack later than that
    still counts as an ack but earns no credit.
    """
    if ack_delay_min <= ack_timeout_min:
        return 1.0
    late_min = ack_delay_min - ack_timeout_min
    return max(0.0, 1.0 - late_min / ack_timeout_min)


@dataclass(frozen=True)
class Score:
    time_to_ack_min: object  # int, or None if nobody acked
    time_to_mitigate_min: object  # int, or None if never mitigated
    time_to_resolve_min: object  # int, or None if never resolved
    escalations: int
    resolved: bool
    points: int  # 0..100
    # Minutes past the acking level's timeout (0 for an on-time ack),
    # or None if nobody acked.
    ack_late_min: object = None
    # Fraction of the score a late ack keeps: 1.0 on time, decaying
    # linearly to 0.0 at twice the timeout; None if nobody acked.
    ack_credit: object = None

    def to_dict(self) -> dict:
        """JSON-ready dict; unreached milestones serialize as null."""
        return {
            "time_to_ack_min": self.time_to_ack_min,
            "time_to_mitigate_min": self.time_to_mitigate_min,
            "time_to_resolve_min": self.time_to_resolve_min,
            "escalations": self.escalations,
            "resolved": self.resolved,
            "points": self.points,
            "ack_late_min": self.ack_late_min,
            "ack_credit": self.ack_credit,
        }


@dataclass
class SimulationResult:
    incident: Incident
    policy: EscalationPolicy
    timeline: Timeline
    score: Score
    # RNG seed used to jitter ack delays, or None for the fixed scenario
    # delays. Recorded so a seeded run can be reproduced exactly.
    seed: object = None

    def to_dict(self) -> dict:
        """JSON-ready dict of the whole result: incident, policy, timeline, score."""
        return {
            "seed": self.seed,
            "incident": {
                "id": self.incident.id,
                "service": self.incident.service,
                "title": self.incident.title,
                "severity": self.incident.severity,
                "description": self.incident.description,
            },
            "policy": {
                "name": self.policy.name,
                "levels": [
                    {
                        "responder": {
                            "name": level.responder.name,
                            "role": level.responder.role,
                        },
                        "ack_timeout_min": level.ack_timeout_min,
                    }
                    for level in self.policy.levels
                ],
            },
            "timeline": self.timeline.to_dict(),
            "score": self.score.to_dict(),
        }


def simulate(
    scenario: Scenario,
    policy: EscalationPolicy,
    max_duration_min: int = 240,
    seed: object = None,
) -> SimulationResult:
    """Run one scenario against one policy and return the full result.

    With ``seed`` given, each ack delay is jittered by up to
    ``SEED_JITTER_MIN`` minutes either way, drawn from a ``random.Random``
    seeded with ``seed`` — so the same seed replays the same run exactly
    while different seeds vary the response times. A miss (``None``
    delay) stays a miss, so the scenario's escalation structure is
    unchanged. Without a seed the scenario's fixed delays are used.
    """
    incident = Incident(
        id=scenario.name,
        service=scenario.service,
        title=scenario.title,
        severity=scenario.severity,
        description=scenario.description,
    )
    timeline = Timeline()
    timeline.record(0, "declared", "monitor", scenario.title)
    rng = random.Random(seed) if seed is not None else None

    t = 0
    escalations = 0
    acked_at = None
    acked_by = None

    for i, level in enumerate(policy.levels):
        timeline.record(t, "paged", level.responder.name, level.responder.role)
        delay = scenario.ack_delay_min[i] if i < len(scenario.ack_delay_min) else None
        if delay is not None and rng is not None:
            # Jitter the ack delay, never below 1 minute.
            delay = max(1, delay + rng.randint(-SEED_JITTER_MIN,
                                               SEED_JITTER_MIN))
        if delay is not None:
            # The responder acks, on time or late. A late ack still counts:
            # the incident proceeds, but the score is scaled by partial
            # credit that decays linearly to zero at twice the timeout.
            ack_late_min = max(0, delay - level.ack_timeout_min)
            ack_credit = late_ack_credit(delay, level.ack_timeout_min)
            acked_at = t + delay
            acked_by = level.responder.name
            detail = ""
            if ack_late_min:
                detail = (f"late by {ack_late_min}m "
                          f"(ack credit {ack_credit:.0%})")
            timeline.record(acked_at, "acked", acked_by, detail)
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
                seed=seed,
            )
        if t > max_duration_min:
            timeline.record(t, "unacked", "monitor", "max duration reached")
            return SimulationResult(
                incident, policy, timeline,
                Score(None, None, None, escalations, False, 0),
                seed=seed,
            )

    mitigated_at = acked_at + scenario.mitigate_delay_min
    resolved_at = mitigated_at + scenario.resolve_delay_min
    timeline.record(mitigated_at, "mitigated", acked_by)
    timeline.record(resolved_at, "resolved", acked_by)

    points = 100
    points -= ESCALATION_PENALTY * escalations
    points -= max(0, acked_at - ACK_GRACE_MIN)
    points -= max(0, mitigated_at - MITIGATE_GRACE_MIN) // 2
    # A late ack keeps only part of the score: the effective score is the
    # base score scaled by the ack credit (1.0 for an on-time ack).
    points = max(0, round(points * ack_credit))
    score = Score(
        time_to_ack_min=acked_at,
        time_to_mitigate_min=mitigated_at,
        time_to_resolve_min=resolved_at,
        escalations=escalations,
        resolved=True,
        points=points,
        ack_late_min=ack_late_min,
        ack_credit=ack_credit,
    )
    return SimulationResult(incident, policy, timeline, score, seed=seed)
