"""Debrief: a structured postmortem generated from one simulated run.

Given a SimulationResult (in memory, or rebuilt from a saved
`run --json` file by replay.load_result), this builds the postmortem a
team would write after the incident: a short summary, what went well,
what went badly, and action items. Every finding is derived from the
run itself — the timeline, the policy levels, and the score — so the
debrief never invents a cause the simulation did not show.

Action items carry an owner: the responder whose page was missed or
late, or "on-call lead" for the systemic ones (slow mitigation, nobody
acking at all).
"""

from dataclasses import dataclass

from .simulator import (
    ACK_GRACE_MIN, MITIGATE_GRACE_MIN, SimulationResult,
)


def grade(points: int) -> str:
    """Letter grade for a run's score: A at 90, B at 75, C at 50, else D."""
    if points >= 90:
        return "A"
    if points >= 75:
        return "B"
    if points >= 50:
        return "C"
    return "D"


@dataclass
class Debrief:
    incident_id: str
    service: str
    title: str
    severity: str
    points: int
    grade: str
    time_to_ack_min: object  # int, or None if nobody acked
    time_to_mitigate_min: object  # int, or None if never mitigated
    time_to_resolve_min: object  # int, or None if never resolved
    escalations: int
    resolved: bool
    went_well: list  # list[str]
    went_badly: list  # list[str]
    # list[{"action": str, "owner": str}]
    action_items: list
    timeline: str  # rendered timeline, as replay prints it
    # Difficulty the run was played at, or None for a saved run from
    # before difficulties existed.
    difficulty: object = None

    def to_dict(self) -> dict:
        """JSON-ready dict: summary, findings, action items, timeline."""
        return {
            "incident": {
                "id": self.incident_id,
                "service": self.service,
                "title": self.title,
                "severity": self.severity,
            },
            "summary": {
                "points": self.points,
                "grade": self.grade,
                "difficulty": (self.difficulty.name
                               if self.difficulty is not None else None),
                "time_to_ack_min": self.time_to_ack_min,
                "time_to_mitigate_min": self.time_to_mitigate_min,
                "time_to_resolve_min": self.time_to_resolve_min,
                "escalations": self.escalations,
                "resolved": self.resolved,
            },
            "went_well": list(self.went_well),
            "went_badly": list(self.went_badly),
            "action_items": [
                {"action": item["action"], "owner": item["owner"]}
                for item in self.action_items
            ],
            "timeline": self.timeline,
        }

    def render(self) -> str:
        """The postmortem as Markdown: summary, timeline, findings."""
        lines = [
            f"# Postmortem: {self.title}",
            "",
            f"Service: {self.service} [{self.severity}]",
            f"Score: {self.points}/100 (grade {self.grade})",
        ]
        if self.difficulty is not None:
            lines.append(f"Difficulty: {self.difficulty.name}")
        lines += [
            ("Time-to-ack: "
             f"{self.time_to_ack_min}m" if self.time_to_ack_min is not None
             else "Time-to-ack: nobody acked"),
            ("Time-to-mitigate: "
             f"{self.time_to_mitigate_min}m"
             if self.time_to_mitigate_min is not None
             else "Time-to-mitigate: never mitigated"),
            ("Time-to-resolve: "
             f"{self.time_to_resolve_min}m"
             if self.time_to_resolve_min is not None
             else "Time-to-resolve: never resolved"),
            f"Escalations: {self.escalations}",
            "",
            "## Timeline",
            "",
            self.timeline,
            "",
            "## What went well",
            "",
        ]
        if self.went_well:
            lines.extend(f"- {item}" for item in self.went_well)
        else:
            lines.append("- none")
        lines += ["", "## What went badly", ""]
        if self.went_badly:
            lines.extend(f"- {item}" for item in self.went_badly)
        else:
            lines.append("- none")
        lines += ["", "## Action items", ""]
        if self.action_items:
            lines.extend(
                f"- [ ] {item['action']} (owner: {item['owner']})"
                for item in self.action_items
            )
        else:
            lines.append("- none")
        return "\n".join(lines)


def _impact_growth(result: SimulationResult) -> tuple:
    """Findings for impact that grew while the page went unanswered.

    Returns ``(went_badly, action_items)``: one bad finding per stage
    the impact grew to, plus a single action to escalate sooner,
    naming the worst impact reached. A run whose impact never grew
    (no impact stages, or an ack on the first page) reports nothing.
    The chain records the impact in effect when each level was paged,
    so a step whose impact differs from the previous step's is a
    stage the incident grew to while paging.
    """
    chain = result.escalation_chain or []
    went_badly = []
    action_items = []
    previous = None
    worst = None
    for step in chain:
        if not step.impact:
            continue
        if previous is not None and step.impact != previous:
            went_badly.append(
                "Impact grew while the page went unanswered: "
                f"{step.impact}")
        previous = step.impact
        worst = step.impact
    if went_badly:
        action_items.append({
            "action": (
                "Escalate sooner: the impact grew to "
                f"{worst!r} before anyone acked — tighten the early "
                "ack timeouts or page the next level sooner"),
            "owner": "on-call lead",
        })
    return went_badly, action_items


def _partial_debrief(result: SimulationResult, ack_grace_min: int,
                     mitigate_grace_min: int) -> Debrief:
    """Postmortem for a run that acked but never resolved.

    Only a drill ended early (quit or EOF) produces this shape: the
    ack happened, so the missed pages and the ack are judged the same
    way as in a resolved run, and the missing mitigation / resolution
    are findings of their own, with an action to finish the drill.
    """
    score = result.score
    incident = result.incident
    went_well = []
    went_badly = []
    action_items = []
    # The first `escalations` levels missed; the next level acked.
    missed = result.policy.levels[:score.escalations]
    acking = result.policy.levels[score.escalations]
    # Minutes from when the acking level was paged to its ack: the
    # time-to-ack minus the timeouts waited out on the missed levels.
    ack_delay_min = (score.time_to_ack_min
                     - sum(level.ack_timeout_min for level in missed))
    for level in missed:
        went_badly.append(
            f"{level.responder.name} ({level.responder.role}) missed "
            f"the page — no ack within {level.ack_timeout_min}m")
        action_items.append({
            "action": (
                f"Check {level.responder.name}'s paging delivery and "
                f"alert routing — the page went unanswered for "
                f"{level.ack_timeout_min}m"),
            "owner": level.responder.name,
        })
    # The impact may have grown while the page went unanswered.
    badly, actions = _impact_growth(result)
    went_badly.extend(badly)
    action_items.extend(actions)
    if score.escalations == 0:
        went_well.append(
            f"{acking.responder.name} ({acking.responder.role}) acked "
            "first time, with no escalation")
    if score.ack_late_min == 0:
        went_well.append(
            f"Ack arrived on time ({acking.responder.name} acked "
            f"{ack_delay_min}m after being paged, within the "
            f"{acking.ack_timeout_min}m timeout)")
    else:
        went_badly.append(
            f"Ack was {score.ack_late_min}m late, keeping only "
            f"{score.ack_credit:.0%} of the score")
        action_items.append({
            "action": (
                f"Review why {acking.responder.name} acked "
                f"{score.ack_late_min}m late and whether the "
                f"{acking.ack_timeout_min}m {acking.responder.role} "
                "timeout still fits"),
            "owner": acking.responder.name,
        })
    if score.time_to_ack_min <= ack_grace_min:
        went_well.append(
            f"Time-to-ack ({score.time_to_ack_min}m) was inside the "
            f"{ack_grace_min}m target")
    if score.time_to_mitigate_min is not None:
        if score.time_to_mitigate_min <= mitigate_grace_min:
            went_well.append(
                f"Mitigated within the {mitigate_grace_min}m target "
                f"({score.time_to_mitigate_min}m)")
        else:
            over = score.time_to_mitigate_min - mitigate_grace_min
            went_badly.append(
                f"Mitigation took {score.time_to_mitigate_min}m, {over}m "
                f"past the {mitigate_grace_min}m target")
            action_items.append({
                "action": (
                    f"Write or rehearse a runbook for {incident.title} on "
                    f"{incident.service} — mitigation took "
                    f"{score.time_to_mitigate_min}m against a "
                    f"{mitigate_grace_min}m target"),
                "owner": "on-call lead",
            })
        went_badly.append(
            "The incident was mitigated but never resolved — the drill "
            "ended before the resolve step")
    else:
        went_badly.append(
            "The incident was never mitigated — the drill ended after "
            "the ack, before mitigation")
    action_items.append({
        "action": (
            "Re-run the drill to completion: a drill ended early "
            "leaves the incident unmitigated and unresolved, so the "
            "response was never practised end to end"),
        "owner": "on-call lead",
    })
    return Debrief(
        incident_id=incident.id,
        service=incident.service,
        title=incident.title,
        severity=incident.severity,
        points=score.points,
        grade=grade(score.points),
        time_to_ack_min=score.time_to_ack_min,
        time_to_mitigate_min=score.time_to_mitigate_min,
        time_to_resolve_min=None,
        escalations=score.escalations,
        resolved=False,
        went_well=went_well,
        went_badly=went_badly,
        action_items=action_items,
        timeline=result.timeline.render(),
        difficulty=result.difficulty,
    )


def generate_debrief(result: SimulationResult) -> Debrief:
    """Build the postmortem for one simulated run.

    The acking level is the first level that did not miss: with N
    escalations, the first N levels missed and level N acked (or, when
    nobody acked, every level missed). Findings follow from that and
    from the score's lateness, mitigation time, and resolution.
    """
    score = result.score
    incident = result.incident
    # Scoring targets come from the run's difficulty (normal's 5m / 15m
    # when the run predates difficulties), so the findings judge the run
    # against the targets it was actually played at.
    if result.difficulty is not None:
        ack_grace_min = result.difficulty.ack_grace_min
        mitigate_grace_min = result.difficulty.mitigate_grace_min
    else:
        ack_grace_min = ACK_GRACE_MIN
        mitigate_grace_min = MITIGATE_GRACE_MIN
    went_well = []
    went_badly = []
    action_items = []

    if not score.resolved:
        if score.time_to_ack_min is not None:
            # A drill ended early: someone acked, but the incident was
            # never mitigated or resolved. Judge what happened — the
            # missed pages and the ack — and say plainly that the rest
            # never happened, instead of claiming nobody acked.
            return _partial_debrief(result, ack_grace_min,
                                    mitigate_grace_min)
        # Nobody acked: every level in the chain missed the page.
        for level in result.policy.levels:
            went_badly.append(
                f"{level.responder.name} ({level.responder.role}) missed "
                f"the page — no ack within "
                f"{level.ack_timeout_min}m")
            action_items.append({
                "action": (
                    f"Check {level.responder.name}'s paging delivery and "
                    f"alert routing — the page went unanswered for "
                    f"{level.ack_timeout_min}m"),
                "owner": level.responder.name,
            })
        # The impact may have grown while the page went unanswered.
        badly, actions = _impact_growth(result)
        went_badly.extend(badly)
        action_items.extend(actions)
        went_badly.append(
            "Nobody acked the page; the incident was never mitigated "
            "or resolved")
        action_items.append({
            "action": (
                "Add a fallback page outside the chain (for example the "
                "on-call manager directly) for when nobody acks"),
            "owner": "on-call lead",
        })
        return Debrief(
            incident_id=incident.id,
            service=incident.service,
            title=incident.title,
            severity=incident.severity,
            points=score.points,
            grade=grade(score.points),
            time_to_ack_min=None,
            time_to_mitigate_min=None,
            time_to_resolve_min=None,
            escalations=score.escalations,
            resolved=False,
            went_well=went_well,
            went_badly=went_badly,
            action_items=action_items,
            timeline=result.timeline.render(),
            difficulty=result.difficulty,
        )

    # The first `escalations` levels missed; the next level acked.
    missed = result.policy.levels[:score.escalations]
    acking = result.policy.levels[score.escalations]
    # Minutes from when the acking level was paged to its ack: the
    # time-to-ack minus the timeouts waited out on the missed levels.
    ack_delay_min = (score.time_to_ack_min
                     - sum(level.ack_timeout_min for level in missed))
    for level in missed:
        went_badly.append(
            f"{level.responder.name} ({level.responder.role}) missed "
            f"the page — no ack within {level.ack_timeout_min}m")
        action_items.append({
            "action": (
                f"Check {level.responder.name}'s paging delivery and "
                f"alert routing — the page went unanswered for "
                f"{level.ack_timeout_min}m"),
            "owner": level.responder.name,
        })

    # The impact may have grown while the page went unanswered.
    badly, actions = _impact_growth(result)
    went_badly.extend(badly)
    action_items.extend(actions)

    if score.escalations == 0:
        went_well.append(
            f"{acking.responder.name} ({acking.responder.role}) acked "
            "first time, with no escalation")

    if score.ack_late_min == 0:
        went_well.append(
            f"Ack arrived on time ({acking.responder.name} acked "
            f"{ack_delay_min}m after being paged, within the "
            f"{acking.ack_timeout_min}m timeout)")
    else:
        went_badly.append(
            f"Ack was {score.ack_late_min}m late, keeping only "
            f"{score.ack_credit:.0%} of the score")
        action_items.append({
            "action": (
                f"Review why {acking.responder.name} acked "
                f"{score.ack_late_min}m late and whether the "
                f"{acking.ack_timeout_min}m {acking.responder.role} "
                "timeout still fits"),
            "owner": acking.responder.name,
        })

    if score.time_to_mitigate_min <= mitigate_grace_min:
        went_well.append(
            f"Mitigated within the {mitigate_grace_min}m target "
            f"({score.time_to_mitigate_min}m)")
    else:
        over = score.time_to_mitigate_min - mitigate_grace_min
        went_badly.append(
            f"Mitigation took {score.time_to_mitigate_min}m, {over}m "
            f"past the {mitigate_grace_min}m target")
        action_items.append({
            "action": (
                f"Write or rehearse a runbook for {incident.title} on "
                f"{incident.service} — mitigation took "
                f"{score.time_to_mitigate_min}m against a "
                f"{mitigate_grace_min}m target"),
            "owner": "on-call lead",
        })

    if score.time_to_ack_min <= ack_grace_min:
        went_well.append(
            f"Time-to-ack ({score.time_to_ack_min}m) was inside the "
            f"{ack_grace_min}m target")

    went_well.append(
        f"Incident resolved in {score.time_to_resolve_min}m")

    return Debrief(
        incident_id=incident.id,
        service=incident.service,
        title=incident.title,
        severity=incident.severity,
        points=score.points,
        grade=grade(score.points),
        time_to_ack_min=score.time_to_ack_min,
        time_to_mitigate_min=score.time_to_mitigate_min,
        time_to_resolve_min=score.time_to_resolve_min,
        escalations=score.escalations,
        resolved=True,
        went_well=went_well,
        went_badly=went_badly,
        action_items=action_items,
        timeline=result.timeline.render(),
        difficulty=result.difficulty,
    )
