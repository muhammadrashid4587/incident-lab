"""Built-in incident scenarios.

A scenario fixes the service, severity and responder behaviour: how long
each responder takes to ack (minutes), or None if they never ack, plus the
mitigation and resolution delays once someone acks. The simulator walks the
escalation chain in order, so the behaviour list lines up with the policy
levels: index 0 is the primary, 1 the secondary, 2 the manager.

All times are simulated minutes. Nothing here pages a real person.
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
    )


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
