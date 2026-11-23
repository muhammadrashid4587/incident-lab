"""Built-in incident scenarios.

A scenario fixes the service, severity and responder behaviour: how long
each responder takes to ack (minutes), or None if they never ack, plus the
mitigation and resolution delays once someone acks. The simulator walks the
escalation chain in order, so the behaviour list lines up with the policy
levels: index 0 is the primary, 1 the secondary, 2 the manager.

All times are simulated minutes. Nothing here pages a real person.

A scenario may also carry ``impact_stages``: the impact in effect at
each escalation count — stage 0 when the incident is declared, stage 1
after the first missed level, and so on. When a level misses its page
the impact grows to the next stage, so a SEV-1 that nobody acks
quickly gets worse while the chain walks. Scenarios without stages
have no impact progression; the chain records an empty impact for
them.
"""

from dataclasses import dataclass
import json

from .incident import SEVERITIES


@dataclass(frozen=True)
class Scenario:
    name: str
    service: str
    title: str
    severity: str
    description: str
    # Per escalation level: minutes until that responder acks, or None.
    ack_delay_min: tuple
    # Minutes from ack to mitigated, and from mitigated to resolved.
    mitigate_delay_min: int
    resolve_delay_min: int
    # Known-good next actions for this incident, in the order they help:
    # what an operator who is stuck should check or do first. Asked for
    # with `run --hints N`; each hint costs score, so they are guidance,
    # not a free answer.
    hints: tuple = ()
    # Impact in effect at each escalation count: index 0 when the
    # incident is declared, index 1 after the first missed level, and
    # so on. Empty means the scenario has no impact progression.
    impact_stages: tuple = ()


SCENARIOS = {
    "disk-full": Scenario(
        name="disk-full",
        service="payments-db",
        title="Disk 97% full on payments-db",
        severity="high",
        description=(
            "The primary database disk is 97% full and growing. "
            "WAL archiving stalled after a bad deploy."
        ),
        ack_delay_min=(3, None, None),
        mitigate_delay_min=20,
        resolve_delay_min=12,
        hints=(
            "Check WAL archiving: a stalled archiver is the usual cause "
            "of disk growth after a deploy.",
            "Free space now by rotating old WAL segments, then confirm "
            "the bad deploy is rolled back before adding disk.",
            "Watch the disk-free metric for 10m after the fix; if it "
            "keeps falling, the leak is still running.",
        ),
    ),
    "latency-spike": Scenario(
        name="latency-spike",
        service="checkout-api",
        title="p99 latency over 2s on checkout-api",
        severity="critical",
        description=(
            "Checkout p99 latency jumped from 180ms to over 2s. "
            "The primary is in a meeting and misses the page."
        ),
        ack_delay_min=(None, 4, None),
        mitigate_delay_min=35,
        resolve_delay_min=20,
        hints=(
            "Check the downstream dependency first: a latency spike "
            "with no deploys is usually a slow database or queue.",
            "Compare p99 by endpoint: if only checkout is slow, the "
            "problem is in checkout, not the shared stack.",
            "Shed load before tuning: enable the circuit breaker or "
            "rate-limit non-critical traffic while you dig.",
        ),
    ),
    "cert-expiry": Scenario(
        name="cert-expiry",
        service="edge-tls",
        title="TLS certificate expires in 6 hours",
        severity="high",
        description=(
            "The edge TLS certificate expires in 6 hours and the renewal "
            "job has been failing silently for a week."
        ),
        ack_delay_min=(2, None, None),
        mitigate_delay_min=8,
        resolve_delay_min=5,
        hints=(
            "Renew the certificate by hand first; fix the silent renewal "
            "job after the incident is over.",
            "Check the renewal job's logs: a week of silent failures "
            "means its alerting is broken too.",
        ),
    ),
    "deploy-regression": Scenario(
        name="deploy-regression",
        service="search-api",
        title="Error rate 18% after deploy",
        severity="critical",
        description=(
            "Error rate jumped to 18% right after a deploy. Both on-call "
            "engineers miss the page; the manager picks it up."
        ),
        ack_delay_min=(None, None, 6),
        mitigate_delay_min=45,
        resolve_delay_min=30,
        hints=(
            "Roll back the deploy first: an 18% error rate starting "
            "right after a deploy is the deploy until proven otherwise.",
            "Compare the error rate by version: if only the new version "
            "errors, the rollback will confirm it.",
            "Freeze further deploys until the bad one is out; a second "
            "deploy on top of a regression hides the cause.",
        ),
    ),
    "queue-backlog": Scenario(
        name="queue-backlog",
        service="orders-queue",
        title="Consumer lag over 2M messages on orders-queue",
        severity="high",
        description=(
            "Order processing is falling behind: consumer lag passed 2M "
            "messages after a downstream slowdown. The primary acks in "
            "4m and scales up consumers to drain the backlog."
        ),
        ack_delay_min=(4, None, None),
        mitigate_delay_min=25,
        resolve_delay_min=15,
        hints=(
            "Scale the consumers up before anything else: lag drains "
            "only as fast as consumption, and the backlog is the "
            "incident.",
            "Check the downstream first: a slow downstream caps how "
            "fast extra consumers can drain the queue.",
            "Confirm the lag is actually falling after scaling; if it "
            "is flat, the consumers are stuck, not short-handed.",
        ),
    ),
    "dns-outage": Scenario(
        name="dns-outage",
        service="edge-dns",
        title="DNS resolution failing for edge-dns",
        severity="critical",
        description=(
            "Authoritative DNS is returning SERVFAIL for customer domains. "
            "The primary misses the page; the secondary acks in 3m and "
            "fails over to the backup DNS provider."
        ),
        ack_delay_min=(None, 3, None),
        mitigate_delay_min=40,
        resolve_delay_min=25,
        hints=(
            "Fail over to the backup DNS provider first; DNS is "
            "foundational, and every minute down blocks everything "
            "else.",
            "Check whether the SERVFAIL is everywhere or one region: "
            "a regional failure points at one nameserver, not the "
            "whole provider.",
            "Verify the failover actually resolves before declaring "
            "the incident mitigated; a bad failover looks fixed "
            "until the TTLs expire.",
        ),
    ),
    "payments-outage": Scenario(
        name="payments-outage",
        service="payments-api",
        title="Payments API fully down — SEV-1",
        severity="critical",
        description=(
            "The payments API is returning 500s on every request and "
            "checkout is down. The on-call and the senior both miss "
            "the page; the incident commander picks it up. While the "
            "page goes unanswered the impact grows: degraded checkout, "
            "then checkout down, then payments fully down."
        ),
        ack_delay_min=(None, None, 4),
        mitigate_delay_min=50,
        resolve_delay_min=30,
        hints=(
            "Fail over to the standby payments cluster first; "
            "checkout is down and every minute costs revenue.",
            "Check the payment provider's status page: a full outage "
            "with no deploys is usually the provider, not us.",
            "Freeze refunds until checkout is back; a second failure "
            "mode on top of the outage hides the cause.",
        ),
        impact_stages=(
            "Checkout degraded: 5% of payments failing",
            "Checkout down: all payments failing, customers cannot "
            "check out",
            "Payments fully down: checkout and refunds failing, "
            "revenue impact growing",
        ),
    ),
}


def get(name: str) -> Scenario:
    """Return a scenario by name, or raise KeyError."""
    return SCENARIOS[name]


def list_scenarios() -> list:
    """Scenario names, in definition order."""
    return list(SCENARIOS)


def scenario_to_dict(scenario: Scenario) -> dict:
    """JSON-ready dict of a scenario (its name is the key it sits under).

    The shape matches one entry of a scenario file, so the output of
    ``template`` can be saved and used with ``--scenario-file`` as-is.
    """
    return {
        "service": scenario.service,
        "title": scenario.title,
        "severity": scenario.severity,
        "description": scenario.description,
        "ack_delay_min": list(scenario.ack_delay_min),
        "mitigate_delay_min": scenario.mitigate_delay_min,
        "resolve_delay_min": scenario.resolve_delay_min,
        "hints": list(scenario.hints),
        "impact_stages": list(scenario.impact_stages),
    }


def _require_string(data: dict, field: str, name: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"scenario {name!r}: {field!r} must be a non-empty string")
    return value


def _require_positive_int(data: dict, field: str, name: str) -> int:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            f"scenario {name!r}: {field!r} must be a positive integer "
            "number of minutes")
    return value


def _scenario_from_dict(name: str, data) -> Scenario:
    """Build one scenario from its JSON mapping, validating as we go."""
    if not isinstance(data, dict):
        raise ValueError(f"scenario {name!r}: expected an object")
    severity = _require_string(data, "severity", name)
    if severity not in SEVERITIES:
        raise ValueError(
            f"scenario {name!r}: 'severity' must be one of {SEVERITIES}, "
            f"got {severity!r}")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ValueError(
            f"scenario {name!r}: 'description' must be a string")
    raw_delays = data.get("ack_delay_min")
    if not isinstance(raw_delays, list) or not raw_delays:
        raise ValueError(
            f"scenario {name!r}: 'ack_delay_min' must be a non-empty list "
            "of minutes (or null for a responder who never acks)")
    delays = []
    for index, raw in enumerate(raw_delays):
        if raw is None:
            delays.append(None)
        elif isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise ValueError(
                f"scenario {name!r}: 'ack_delay_min' entry {index} must "
                "be a positive integer number of minutes or null")
        else:
            delays.append(raw)
    return Scenario(
        name=name,
        service=_require_string(data, "service", name),
        title=_require_string(data, "title", name),
        severity=severity,
        description=description,
        ack_delay_min=tuple(delays),
        mitigate_delay_min=_require_positive_int(
            data, "mitigate_delay_min", name),
        resolve_delay_min=_require_positive_int(
            data, "resolve_delay_min", name),
        hints=tuple(_require_hints(data, name)),
        impact_stages=tuple(_require_impact_stages(data, name)),
    )


def _require_impact_stages(data: dict, name: str) -> list:
    """Validate a scenario file's optional ``impact_stages`` list.

    Impact stages are the impact in effect at each escalation count
    (stage 0 when the incident is declared, stage 1 after the first
    missed level, and so on), so a SEV-1 that nobody acks quickly
    gets worse while the chain walks. Absent means no impact
    progression; present it must be a list of non-empty strings.
    """
    raw = data.get("impact_stages", [])
    if not isinstance(raw, list):
        raise ValueError(
            f"scenario {name!r}: 'impact_stages' must be a list of "
            "strings")
    for index, stage in enumerate(raw):
        if not isinstance(stage, str) or not stage:
            raise ValueError(
                f"scenario {name!r}: 'impact_stages' entry {index} must "
                "be a non-empty string")
    return raw


def _require_hints(data: dict, name: str) -> list:
    """Validate a scenario file's optional ``hints`` list.

    Hints are the scenario's known-good next actions, asked for with
    ``run --hints N``. Absent means no hints; present it must be a list
    of non-empty strings.
    """
    raw = data.get("hints", [])
    if not isinstance(raw, list):
        raise ValueError(
            f"scenario {name!r}: 'hints' must be a list of strings")
    for index, hint in enumerate(raw):
        if not isinstance(hint, str) or not hint:
            raise ValueError(
                f"scenario {name!r}: 'hints' entry {index} must be a "
                "non-empty string")
    return raw


def load_scenario_file(path: str) -> dict:
    """Load named scenarios from a JSON file.

    The file is an object with a "scenarios" mapping, each entry a
    scenario with "service", "title", "severity", "description",
    "ack_delay_min" (a list of minutes, or null where a responder never
    acks) and "mitigate_delay_min" / "resolve_delay_min"::

        {"scenarios": {"cache-down": {
            "service": "sessions-cache",
            "title": "Cache cluster unreachable",
            "severity": "high",
            "description": "Redis cluster stopped accepting writes.",
            "ack_delay_min": [2, null, null],
            "mitigate_delay_min": 15,
            "resolve_delay_min": 10
        }}}

    Returns a dict of name -> Scenario. Raises FileNotFoundError if the
    file is missing, json.JSONDecodeError on bad JSON, and ValueError on
    a wrong shape or an invalid scenario.
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("scenario file: expected a JSON object")
    raw_scenarios = data.get("scenarios")
    if not isinstance(raw_scenarios, dict) or not raw_scenarios:
        raise ValueError(
            "scenario file: 'scenarios' must be a non-empty object "
            "mapping scenario names to scenarios")
    scenarios = {}
    for name, raw in raw_scenarios.items():
        if not isinstance(name, str) or not name:
            raise ValueError("scenario file: scenario names must be "
                             "non-empty")
        scenarios[name] = _scenario_from_dict(name, raw)
    return scenarios
