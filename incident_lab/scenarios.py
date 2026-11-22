"""Built-in incident scenarios.

A scenario fixes the service, severity and responder behaviour: how long
each responder takes to ack (minutes), or None if they never ack, plus the
mitigation and resolution delays once someone acks. The simulator walks the
escalation chain in order, so the behaviour list lines up with the policy
levels: index 0 is the primary, 1 the secondary, 2 the manager.

All times are simulated minutes. Nothing here pages a real person.
"""

from dataclasses import dataclass


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
