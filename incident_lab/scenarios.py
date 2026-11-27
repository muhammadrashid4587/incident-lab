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

A scenario may also carry ``slo_percent``: the service's SLO target
(99.9, say). The incident's downtime — declaration to mitigated —
is reported against the monthly error budget that SLO allows, as
error-budget burn in percent; a scenario without an SLO reports no
burn.

A scenario may also carry ``verify_delay_min_by_region``: the
minutes verifying the fix costs in one region, by region. A region
listed there costs what it says instead of the 3m default — a far
or slow region costs more to confirm than a near one — and a region
left out costs the default, so a scenario that gives no costs at
all verifies at 3m per region, exactly as before.
"""

from dataclasses import dataclass, field
import json

from .incident import SEVERITIES


# Minutes to verify a remediation in one region when the scenario
# gives no per-region cost for it: fetch the asset (or run the
# check) from a cold edge node there and confirm the fix actually
# landed, instead of trusting the dashboard. A scenario can give a
# region its own cost with ``verify_delay_min_by_region`` — a far or
# slow region costs more to check than a near one.
DEFAULT_VERIFY_MIN_PER_REGION = 3


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
    # The service's SLO target in percent (99.9, say), or None when the
    # scenario carries no SLO. The monthly error budget is
    # (100 - SLO)% of a 30-day month; the incident's downtime is
    # reported against it as error-budget burn.
    slo_percent: object = None
    # The regions the remediation must reach: with per-region purge
    # verification the fix is confirmed from a cold edge node in each
    # one before the incident is declared mitigated. Empty means the
    # scenario has no regions and per-region verification does not
    # apply to it.
    regions: tuple = ()
    # Minutes to verify the fix in one region, by region: a region
    # listed here costs that many minutes to check instead of the
    # default (``DEFAULT_VERIFY_MIN_PER_REGION``) — a far or slow
    # region costs more to confirm than a near one. A region not
    # listed here costs the default, so a scenario that gives no
    # costs at all verifies at 3m per region, exactly as before.
    verify_delay_min_by_region: dict = field(default_factory=dict)

    def verify_delay_min_for(self, region: str) -> int:
        """Minutes to verify the fix in ``region``: the scenario's own
        per-region cost when it gives one, else the default."""
        return self.verify_delay_min_by_region.get(
            region, DEFAULT_VERIFY_MIN_PER_REGION)

    def verify_delay_min_total(self) -> int:
        """Minutes to verify the fix in every region, in order: the
        sum of each region's own cost (or the default where the
        scenario gives none)."""
        return sum(self.verify_delay_min_for(region)
                   for region in self.regions)


SCENARIOS = {
    "disk-full": Scenario(
        name="disk-full",
        service="payments-db",
        title="Disk 97% full on payments-db",
        severity="high",
        description=(
            "The primary database disk is 97% full and growing. "
            "WAL archiving stalled after a bad deploy. The reclaimed "
            "space is only real once the storage team confirms it in "
            "every region: the volume check takes 3m in us-east, "
            "where the fleet is smallest, 5m in us-west and 9m in "
            "eu-west, where the fleet is largest and the team "
            "confirms each host's volume before signing off."
        ),
        ack_delay_min=(3, None, None),
        mitigate_delay_min=20,
        resolve_delay_min=12,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: the
        # storage team confirms the reclaimed volume in 3m in
        # us-east, 5m in us-west and 9m in eu-west, where the fleet
        # is largest and each host's volume is checked before the
        # team signs off. The total is 17m, 8m more than the 3m
        # default, so the verified run scores 88/100 where the
        # default-cost run scored 92.
        verify_delay_min_by_region={
            "us-east": 3, "us-west": 5, "eu-west": 9,
        },
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
        slo_percent=99.95,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # canary checkout has to complete inside the p99 threshold
        # in every region before the incident counts as mitigated
        # — 3m in us-east, where the checkout fleet is smallest
        # and the canary clears fastest, 6m in us-west and 10m in
        # eu-west, where the slow downstream dependency takes
        # longest to drain its backlog before a clean checkout can
        # be measured. The total is 19m, 10m more than the 3m
        # default, so the verified run scores 57/100 where the
        # default-cost run scored 62.
        verify_delay_min_by_region={
            "us-east": 3, "us-west": 6, "eu-west": 10,
        },
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
        slo_percent=99.9,
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
        slo_percent=99.9,
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
        slo_percent=99.5,
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
            "fails over to the backup DNS provider. The failover is only "
            "real once DNS resolves in every region: a test lookup clears "
            "in 2m in us-east, where the backup provider's nameservers "
            "answer fastest, 5m in us-west and 8m in eu-west, where "
            "resolvers hold the old SERVFAIL longest on their TTLs."
        ),
        ack_delay_min=(None, 3, None),
        mitigate_delay_min=40,
        resolve_delay_min=25,
        slo_percent=99.99,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the failover is not the same price everywhere:
        # a test lookup resolves in 2m in us-east, 5m in us-west
        # and 8m in eu-west, where resolvers hold the old SERVFAIL
        # longest on their TTLs. The total is 15m, 6m more than the
        # 3m default, so the verified run scores 58/100 where the
        # default-cost run scored 61.
        verify_delay_min_by_region={
            "us-east": 2, "us-west": 5, "eu-west": 8,
        },
        hints=(
            "Fail over to the backup DNS provider first; DNS is "
            "foundational, and every minute down blocks everything "
            "else.",
            "Check whether the SERVFAIL is everywhere or one region: "
            "a regional failure points at one nameserver, not the "
            "whole provider.",
            "Verify the failover actually resolves in every region "
            "before declaring the incident mitigated; a bad failover "
            "looks fixed until the TTLs expire, and the resolvers in "
            "eu-west hold the old SERVFAIL longest.",
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
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # test payment clears region by region at the price each
        # region's standby payments cluster charges — 4m in
        # us-east, where the standby cluster is already warm, 7m
        # in us-west, where the standby has to re-register its
        # payment-provider credentials before it can take a test
        # charge, and 11m in eu-west, where the provider's
        # European gateway re-authenticates every queued payment
        # before it clears a fresh one. The total is 22m, 13m
        # more than the 3m default, so the verified run scores
        # 26/100 where the default-cost run scored 32.
        verify_delay_min_by_region={
            "us-east": 4, "us-west": 7, "eu-west": 11,
        },
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
    "sso-outage": Scenario(
        name="sso-outage",
        service="identity-sso",
        title="SSO logins failing, pages going unanswered",
        severity="critical",
        description=(
            "The identity provider is failing every login attempt. "
            "The alert fired, but the pages go to a stale on-call "
            "rotation: the three responders paged have all rotated "
            "off the team, so nobody answers while the impact grows."
        ),
        ack_delay_min=(None, None, None),
        mitigate_delay_min=30,
        resolve_delay_min=20,
        slo_percent=99.95,
        hints=(
            "Fail over to the backup identity provider first; logins "
            "are down and every minute locks out more users.",
            "Check the pager rotation: pages to a stale group explain "
            "why nobody acked, so fix the rotation before the next "
            "incident.",
            "Force a session refresh once the failover is up; sessions "
            "that expired during the outage will not recover on "
            "their own.",
        ),
        impact_stages=(
            "Logins failing: new sign-ins erroring, existing sessions "
            "still work",
            "Logins down: no new sign-ins, support queue growing",
            "Sessions expiring: existing sessions timing out, users "
            "locked out entirely",
        ),
    ),
    "config-push": Scenario(
        name="config-push",
        service="edge-config",
        title="Bad config push routes checkout to a dead pool",
        severity="critical",
        description=(
            "A config push, not a code deploy, flipped the checkout "
            "traffic split to a decommissioned pool. The primary "
            "misses the page; the secondary acks 3m late and rolls "
            "the config back, while the impact grows from checkout "
            "errors rising to checkout down in every region. The "
            "rollback is only real once it is confirmed in every "
            "region: the edge-config agents pull the new config at "
            "different speeds, so the rollback lands in 2m in "
            "us-east, where the config service lives, 4m in us-west "
            "and 7m in eu-west, where the agents poll on a slower "
            "schedule and hold the bad traffic split in cache "
            "longest."
        ),
        ack_delay_min=(None, 8, None),
        mitigate_delay_min=30,
        resolve_delay_min=20,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the rollback is not the same price everywhere:
        # the edge-config agents pull the new config in 2m in
        # us-east, 4m in us-west and 7m in eu-west, where the agents
        # poll on a slower schedule and hold the bad split longest.
        # The total is 13m, 4m more than the 3m default, so the
        # verified run scores 23/100 where the default-cost run
        # scored 24.
        verify_delay_min_by_region={
            "us-east": 2, "us-west": 4, "eu-west": 7,
        },
        hints=(
            "Check the last config change before the code deploys: "
            "a bad config push looks exactly like a bad deploy but "
            "rolls back differently.",
            "Roll back the config push first: push the previous "
            "known-good config, then verify the traffic split is "
            "back before declaring the incident mitigated.",
            "Verify the config actually propagated to every region; "
            "a rollback that only reached one region leaves the "
            "others broken.",
        ),
        impact_stages=(
            "Checkout errors rising: 15% of checkouts failing",
            "Checkout down in all regions: orders failing, customers "
            "cannot pay",
        ),
    ),
    "db-failover": Scenario(
        name="db-failover",
        service="orders-db",
        title="Primary database down, failing over to replica",
        severity="critical",
        description=(
            "The primary database stopped accepting writes and the "
            "automatic failover did not fire. The primary and the "
            "secondary both miss the page; the manager picks it up "
            "2m late and starts the manual failover to the replica, "
            "which completes partway through mitigation while the "
            "impact grows from writes failing to orders down. The "
            "failover is only real once the storage team confirms "
            "the reclaimed volume in every region: the volume check "
            "takes 3m in us-east, where the fleet is smallest, 5m "
            "in us-west and 9m in eu-west, where the fleet is "
            "largest and the team confirms each host's volume "
            "before signing off."
        ),
        ack_delay_min=(None, None, 12),
        mitigate_delay_min=40,
        resolve_delay_min=25,
        slo_percent=99.95,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the failover is not the same price everywhere:
        # the storage team confirms the reclaimed volume in 3m in
        # us-east, 5m in us-west and 9m in eu-west, where the fleet
        # is largest and each host's volume is checked before the
        # team signs off. The total is 17m, 8m more than the 3m
        # default, so the verified run scores 17/100 where the
        # default-cost run scored 20.
        verify_delay_min_by_region={
            "us-east": 3, "us-west": 5, "eu-west": 9,
        },
        hints=(
            "Promote the replica first: writes are down until the "
            "failover completes, and every minute costs orders.",
            "Check the replica's replication lag before promoting: "
            "a replica hours behind will lose orders when it "
            "becomes the primary.",
            "Verify writes are landing on the new primary before "
            "declaring the incident mitigated; a failover that only "
            "half-completed looks fixed until the next write.",
        ),
        impact_stages=(
            "Writes failing: primary down, reads still serving "
            "from cache",
            "Orders failing: replica promotion started, cache "
            "expiring",
            "Failover in progress: replica catching up, reads "
            "degrading as the cache drains",
        ),
    ),
    "network-partition": Scenario(
        name="network-partition",
        service="checkout-mesh",
        title="Network partition between datacenters",
        severity="critical",
        description=(
            "The link between the us-east and us-west datacenters "
            "dropped, cutting the services on each side off from "
            "each other. The pages go to responders whose paging "
            "tooling and bastion sit on the unreachable side, so "
            "the primary and the secondary both miss; the manager "
            "picks it up on time and fails traffic over to one "
            "datacenter while the impact grows from cross-region "
            "timeouts to orders stuck to a split-brain risk. The "
            "failover is only real once a test checkout completes "
            "in every region: the check takes 2m in us-east, where "
            "traffic already routes, 6m in us-west, across the "
            "healed link, and 9m in eu-west, where the partition "
            "spans the most AZs and a clean checkout takes longest "
            "to confirm."
        ),
        ack_delay_min=(None, None, 8),
        mitigate_delay_min=45,
        resolve_delay_min=25,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the failover is not the same price everywhere:
        # a test checkout completes in 2m in us-east, 6m in
        # us-west and 9m in eu-west, where the partition spans the
        # most AZs. The total is 17m, 8m more than the 3m default,
        # so the verified run scores 25/100 where the default-cost
        # run scored 29.
        verify_delay_min_by_region={
            "us-east": 2, "us-west": 6, "eu-west": 9,
        },
        hints=(
            "Check the inter-datacenter link first: a partition "
            "looks like a service outage on each side, but the "
            "services are fine — the network between them is not.",
            "Fail traffic over to one side before repairing the "
            "link: pick the datacenter with the freshest writes "
            "and route everything there, then fix the link.",
            "Check for split-brain writes before declaring the "
            "incident resolved: both sides accepting writes will "
            "need reconciliation once the partition heals.",
        ),
        impact_stages=(
            "Cross-datacenter traffic failing: us-east cannot "
            "reach us-west, requests timing out",
            "Orders stuck: writes cannot reach the primary "
            "datacenter, checkout queue growing",
            "Split-brain risk: both sides still serving reads, "
            "reconciliation needed once the link returns",
        ),
    ),
    "poison-pill": Scenario(
        name="poison-pill",
        service="billing-queue",
        title="Poison pill message crashing consumers",
        severity="high",
        description=(
            "A single malformed billing event keeps crashing every "
            "consumer that reads it; each crash redelivers the same "
            "message, so the queue depth grows while throughput is "
            "zero. The alert storm buries the primary's page, so the "
            "primary misses; the secondary sees the queue depth "
            "climbing and acks in 4m, then finds the bad offset, "
            "moves it to the dead-letter queue and restarts the "
            "consumers while the impact grows from messages backing "
            "up to billing events stuck. The fix is only real once "
            "the queue is confirmed draining in every region: the "
            "check takes 2m in us-east, 4m in us-west and 8m in "
            "eu-west, where the bad deploy rolled out widest and "
            "the most consumers have to be confirmed clear of the "
            "bad offset."
        ),
        ack_delay_min=(None, 4),
        mitigate_delay_min=50,
        resolve_delay_min=20,
        slo_percent=99.5,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # test billing event processes cleanly in 2m in us-east,
        # 4m in us-west and 8m in eu-west, where the bad deploy
        # rolled out widest. The total is 14m, 5m more than the 3m
        # default, so the verified run scores 52/100 where the
        # default-cost run scored 55.
        verify_delay_min_by_region={
            "us-east": 2, "us-west": 4, "eu-west": 8,
        },
        hints=(
            "Check the consumer logs for the same message offset "
            "crashing on every retry: a poison pill looks like a "
            "traffic spike, but the queue depth grows while "
            "throughput is zero.",
            "Move the bad message to the dead-letter queue before "
            "restarting the consumers: restarting without removing "
            "it just crashes them again on the same offset.",
            "Add a max-delivery-count guard so one bad message can "
            "never block the queue again, and alert on dead-letter "
            "arrivals.",
        ),
        impact_stages=(
            "Messages backing up: consumers crashing on the same "
            "offset, queue depth rising",
            "Billing events stuck: orders placed but not billed, "
            "invoices delayed",
        ),
    ),
    "cdn-purge-failure": Scenario(
        name="cdn-purge-failure",
        service="edge-cdn",
        title="CDN purge silently fails, stale assets keep serving",
        severity="high",
        description=(
            "A bad deploy shipped new asset references, and the CDN "
            "purge that was supposed to clear the old ones silently "
            "failed: the purge API returned 200 and the dashboard "
            "says purged, but nothing was actually invalidated, so "
            "users see mixed old and new content and support tickets "
            "spike. The primary does not believe the page at first — "
            "the dashboard says the purge worked — and acks 3m late, "
            "then forces a full invalidation and ships versioned "
            "asset URLs while the stale content spreads. Confirming "
            "the fix costs more the farther the region: a cold edge "
            "node in us-east fetches the new asset in 2m, us-west in "
            "4m, eu-west in 6m, and ap-southeast — where the cold "
            "edge node has to pull the new asset across the Pacific "
            "before the check can confirm it — in 8m."
        ),
        ack_delay_min=(8, None, None),
        mitigate_delay_min=30,
        resolve_delay_min=15,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west", "ap-southeast"),
        # Verifying the fix is not the same price everywhere: a
        # cold edge node fetches the new asset in 2m in us-east,
        # 4m in us-west and 6m in eu-west, but 8m in ap-southeast,
        # where the cold edge node has to pull the new asset across
        # the Pacific before the check can confirm it. The total is
        # 20m, 8m more than the 3m default, so the verified run
        # scores 30/100 where the default-cost run scored 32.
        verify_delay_min_by_region={
            "us-east": 2, "us-west": 4, "eu-west": 6, "ap-southeast": 8,
        },
        hints=(
            "Check the purge by fetching the asset yourself, not by "
            "trusting the purge dashboard: a purge API that returns "
            "200 without purging is the classic silent failure.",
            "Force a full invalidation and ship versioned asset "
            "URLs: purging the same paths again will fail the same "
            "silent way, and versioned URLs stop the old bundle "
            "from being served at all.",
            "Verify the fix from a cold edge node in every region: "
            "a purge that reached one region leaves the others "
            "serving stale assets.",
        ),
        impact_stages=(
            "Some users seeing stale assets: mixed old and new "
            "content after the deploy",
            "Stale checkout assets: users on the old checkout flow, "
            "support tickets spiking",
            "Widespread stale content: most users on the old "
            "bundle, checkout errors rising",
        ),
    ),
    "fraud-rules-misfire": Scenario(
        name="fraud-rules-misfire",
        service="payments-fraud",
        title="Fraud rules update declining every legitimate payment",
        severity="critical",
        description=(
            "A fraud-rules update shipped to the payments fraud service "
            "starts declining every legitimate card payment as suspected "
            "fraud. Checkout itself is up, but no payment goes through, "
            "so revenue stops. The primary is in the rules-deploy review "
            "with their phone on silent and misses the page; the secondary "
            "sees payments being declined and acks in 2m — inside the "
            "fast policy's 2m window — then rolls back the rules update "
            "while the impact grows from payments declining to checkout "
            "blocked."
        ),
        ack_delay_min=(None, 2),
        mitigate_delay_min=30,
        resolve_delay_min=15,
        slo_percent=99.95,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the rollback is not the same price everywhere:
        # a test payment clears fast in the two US regions (2m each),
        # but eu-west takes 5m — the test card must clear 3-D Secure
        # with a European issuer before the rollback is confirmed.
        # The total is 9m, the same as the 3m default, so the score
        # is unchanged; only the checks land at different times.
        verify_delay_min_by_region={
            "us-east": 2, "us-west": 2, "eu-west": 5,
        },
        hints=(
            "Check the fraud-rules deploy first: payments declining "
            "right after a rules update is the rules until proven "
            "otherwise.",
            "Roll back the rules update before digging into the decline "
            "codes: a rollback is fast, and every minute of declines "
            "costs revenue.",
            "Verify a test payment goes through after the rollback "
            "before declaring the incident mitigated; a rollback that "
            "only reached one region leaves the others declining.",
        ),
        impact_stages=(
            "Payments declining: legitimate cards flagged as fraud, "
            "checkout success rate falling",
            "Checkout blocked: most payments declined, orders failing, "
            "revenue stopped",
        ),
    ),
    "key-rotation": Scenario(
        name="key-rotation",
        service="payments-tokens",
        title="Key rotation breaks saved-card decryption",
        severity="critical",
        description=(
            "A scheduled KMS key rotation retired the old data key "
            "before the new one had propagated to every payments "
            "worker, so stored card tokens can no longer be decrypted "
            "and saved-card checkouts fail. The on-call is mid-handover "
            "on another incident bridge and acks 2m late against the "
            "sev1 policy's 3m window, then rolls the rotation back "
            "while the impact grows from saved-card checkouts failing "
            "to payments blocked. Confirming the rollback costs more "
            "the farther the region: workers in us-east pick up the "
            "restored key in 2m, us-west in 4m, and eu-west — where "
            "workers cache the key version longest — in 8m, so the "
            "last saved-card test payment only decrypts once the key "
            "has crossed the Atlantic."
        ),
        ack_delay_min=(5,),
        mitigate_delay_min=35,
        resolve_delay_min=20,
        slo_percent=99.95,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the rollback is not the same price everywhere:
        # a saved-card test payment decrypts in 2m in us-east, where
        # workers pick up the restored key almost at once, in 4m in
        # us-west, and in 8m in eu-west, where workers cache the key
        # version longest. The total is 14m, 5m more than the 3m
        # default, so the verified run scores 27/100 where the
        # default-cost run scored 28.
        verify_delay_min_by_region={
            "us-east": 2, "us-west": 4, "eu-west": 8,
        },
        hints=(
            "Roll back the key rotation first: the old key still "
            "decrypts the stored tokens, and a rollback is faster "
            "than re-encrypting every token with the new key.",
            "Check which workers got the new key: a rotation that "
            "only reached some workers looks like a partial outage, "
            "not a key problem, until you compare the key versions.",
            "Verify a saved-card test payment decrypts after the "
            "rollback before declaring the incident mitigated; "
            "tokens that still fail will keep checkout down.",
        ),
        impact_stages=(
            "Saved-card checkouts failing: stored tokens cannot be "
            "decrypted, new cards still work",
            "Checkout failing: token cache expiring, saved cards and "
            "one-click checkout down",
            "Payments blocked: tokens unusable everywhere, customers "
            "cannot pay at all",
        ),
    ),
    "retry-storm": Scenario(
        name="retry-storm",
        service="notifications-push",
        title="Retry storm rate-limits the push provider",
        severity="critical",
        description=(
            "A bad deploy to the push notification service dropped the "
            "backoff between delivery retries, so every failed push is "
            "retried immediately; the retry loop floods the push "
            "provider, which rate-limits the account, and no push "
            "notifications go out at all. The primary is in the deploy "
            "review and assumes the page is noise from the deploy's "
            "own alerts, so they ack 1m late against the fast "
            "policy's 2m window, then kill the retry loop and drain "
            "the queue while the impact grows from retries flooding "
            "the provider to notifications fully down. The fix only "
            "counts once a test push actually delivers in every "
            "region: the provider's regional gateways drain the "
            "retry backlog at different speeds, so a test push "
            "clears in 2m in us-east, 5m in us-west, and 8m in "
            "eu-west — where the provider's European gateway is "
            "still draining the backlog of retries before it "
            "accepts a fresh test push."
        ),
        ack_delay_min=(3,),
        mitigate_delay_min=25,
        resolve_delay_min=15,
        slo_percent=99.95,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # test push clears in 2m in us-east, 5m in us-west, and 8m
        # in eu-west, where the provider's European gateway is
        # still draining the retry backlog before it accepts a
        # fresh test push. The total is 15m, 6m more than the 3m
        # default, so the verified run scores 43/100 where the
        # default-cost run scored 44.
        verify_delay_min_by_region={
            "us-east": 2, "us-west": 5, "eu-west": 8,
        },
        hints=(
            "Kill the retry loop first: retries without backoff are "
            "the flood, and every retry costs provider quota while "
            "the loop runs.",
            "Check the push provider's rate-limit status: a 429 storm "
            "right after a deploy is the deploy, not the provider.",
            "Drain the retry queue before re-enabling delivery: "
            "re-enabling with a full queue just restarts the flood, "
            "and verify a test push delivers in every region before "
            "declaring the incident mitigated — a gateway still "
            "draining its backlog will drop it.",
        ),
        impact_stages=(
            "Retries flooding the provider: delivery attempts spiking, "
            "push latency climbing",
            "Provider rate-limiting the account: pushes throttled, "
            "notifications delayed",
            "Notifications fully down: no pushes going out, users "
            "missing alerts",
        ),
    ),
    "waf-rules-block": Scenario(
        name="waf-rules-block",
        service="edge-waf",
        title="WAF rules update blocking legitimate checkout traffic",
        severity="critical",
        description=(
            "A WAF rules update shipped to the edge WAF starts blocking "
            "legitimate checkout requests as suspected bots: checkout "
            "itself is up, but real customers cannot pay, so revenue "
            "stops. The primary sees the block rate spike and acks in "
            "2m — on time — then rolls the rules update back while the "
            "impact grows from checkout requests blocked to checkout "
            "down. The rollback is only real once it is confirmed in "
            "every region, and the far region is slow to confirm: the "
            "ap-southeast edge PoP only pulls WAF rules every 15 "
            "minutes, so checking the fix there waits out the poll."
        ),
        ack_delay_min=(2, None, None),
        mitigate_delay_min=13,
        resolve_delay_min=10,
        slo_percent=99.9,
        regions=("us-east", "eu-west", "ap-southeast"),
        # Verifying the rollback is not the same price everywhere:
        # a test request clears in 3m in us-east and 5m in eu-west,
        # but ap-southeast takes 14m — the edge PoP there only pulls
        # WAF rules every 15 minutes, so the check waits out the
        # poll. The total is 22m, and that is the point: without
        # verification the incident mitigates in 15m, inside the
        # grace, and scores 100 (grade A); verified in every region
        # it mitigates in 37m and scores 89 (grade B) — the slow far
        # region costs a whole grade.
        verify_delay_min_by_region={
            "us-east": 3, "eu-west": 5, "ap-southeast": 14,
        },
        hints=(
            "Check the WAF rules deploy first: legitimate checkouts "
            "blocked right after a rules update is the rules until "
            "proven otherwise.",
            "Roll back the rules update before digging into the block "
            "reasons: a rollback is fast, and every minute of blocks "
            "costs revenue.",
            "Verify a test checkout goes through after the rollback "
            "before declaring the incident mitigated; a rollback that "
            "only reached one region leaves the others blocking.",
        ),
        impact_stages=(
            "Checkout requests blocked: legitimate customers flagged "
            "as bots, checkout success rate falling",
            "Checkout down: most checkouts blocked, orders failing, "
            "revenue stopped",
        ),
    ),
    "cache-poisoning": Scenario(
        name="cache-poisoning",
        service="storefront-cache",
        title="Cache poisoning serves stale prices across stores",
        severity="critical",
        description=(
            "A deploy to the pricing service starts writing cache "
            "entries with a key format that drops the tenant segment, "
            "so entries collide across stores: shoppers see another "
            "store's prices and cart contents. The primary's page is "
            "buried under a storm of cache-eviction alerts, so they "
            "miss; the secondary acks 2m late, then flushes the "
            "poisoned keyspace and re-warms the cache while the impact "
            "grows from stale prices to wrong charges at checkout. "
            "The fix is only real once a test request returns fresh "
            "prices in every region: the flush propagates in 4m in "
            "us-east, 6m in us-west and 9m in eu-west, where edge "
            "caches hold the poisoned entries longest on their TTLs."
        ),
        ack_delay_min=(None, 7),
        mitigate_delay_min=30,
        resolve_delay_min=15,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # test request returns fresh prices in 4m in us-east, 6m
        # in us-west and 9m in eu-west, where edge caches hold the
        # poisoned entries longest on their TTLs. The total is 19m,
        # 10m more than the 3m default, so the verified run scores
        # 33/100 where the default-cost run scored 36.
        verify_delay_min_by_region={
            "us-east": 4, "us-west": 6, "eu-west": 9,
        },
        hints=(
            "Check the cache keys first: entries written after the "
            "deploy that are missing the tenant segment are the "
            "poison — a key collision looks like stale data, not a "
            "cache outage.",
            "Flush the poisoned keyspace before re-warming: "
            "re-warming over poisoned entries just re-serves them, "
            "and flush the whole prefix, not one key.",
            "Verify a test request returns fresh prices in every "
            "region before declaring the incident mitigated; edge "
            "caches in eu-west hold the poisoned entries longest on "
            "their TTLs.",
        ),
        impact_stages=(
            "Stale prices served: product pages showing pre-change "
            "prices, customers seeing the wrong amounts",
            "Checkout charging wrong prices: carts priced from "
            "poisoned entries, orders placed at the wrong amount",
        ),
    ),
    "webhook-outage": Scenario(
        name="webhook-outage",
        service="webhooks-outbound",
        title="Webhook provider down, outbound event deliveries failing",
        severity="critical",
        description=(
            "The third-party webhook provider went down, so outbound "
            "event deliveries are failing and queueing up: order "
            "confirmations, shipping updates and payment receipts "
            "never reach the customers who subscribed to them, and "
            "the retries back off while the queue grows. The "
            "primary's page is buried under a storm of "
            "delivery-failure alerts, so they miss; the secondary "
            "acks 1m late, then fails over to the backup provider "
            "and drains the queued events while the impact grows "
            "from deliveries failing to customers missing events. "
            "The fix is only real once a test webhook actually "
            "delivers in every region: the provider's gateway "
            "recovers first in us-east, where the queue is "
            "shallowest, in 3m; us-west takes 5m, where the backlog "
            "of queued events has to drain before a fresh test "
            "delivery is accepted; and eu-west takes 8m, where the "
            "provider's EU endpoint comes back last and each queued "
            "payload has to be re-signed before it can be delivered."
        ),
        ack_delay_min=(None, 6),
        mitigate_delay_min=35,
        resolve_delay_min=20,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # test webhook delivers in 3m in us-east, where the
        # provider's gateway recovers first and the queue is
        # shallowest, in 5m in us-west, where the backlog of queued
        # events has to drain before a fresh test delivery is
        # accepted, and in 8m in eu-west, where the provider's EU
        # endpoint comes back last and each queued payload has to
        # be re-signed before it can be delivered. The total is
        # 16m, 7m more than the 3m default, so the verified run
        # scores 45/100 where the default-cost run scored 47.
        verify_delay_min_by_region={
            "us-east": 3, "us-west": 5, "eu-west": 8,
        },
        hints=(
            "Check the webhook provider's status first: deliveries "
            "failing everywhere at once is the provider, not your "
            "queue — and fail over to the backup provider before "
            "digging into the retries.",
            "Drain the queued events after the failover: the queue "
            "holds deliveries customers are still waiting on, and "
            "re-enabling delivery with a full queue just restarts "
            "the backlog.",
            "Verify a test webhook actually delivers in every region "
            "before declaring the incident mitigated; a gateway "
            "still draining its backlog will drop it, and eu-west "
            "re-signs every queued payload before delivery.",
        ),
        impact_stages=(
            "Deliveries failing: outbound webhooks erroring, events "
            "queueing up instead of reaching subscribers",
            "Customers missing events: order confirmations and "
            "shipping updates never delivered, support tickets "
            "rising",
        ),
    ),
    "feature-flag-misfire": Scenario(
        name="feature-flag-misfire",
        service="checkout-flags",
        title="Feature flag misfire enables unfinished checkout for everyone",
        severity="critical",
        description=(
            "A feature-flag rollout meant to enable the new checkout "
            "for 5% of users instead enables it for everyone: the "
            "flag evaluation service misreads the rollout percentage "
            "after a bad deploy, so every shopper lands on the "
            "unfinished checkout, which errors on payment and drops "
            "carts. The primary's page is buried under a storm of "
            "flag-evaluation error alerts, so they miss; the "
            "secondary acks in 4m, on time, then kills the flag and "
            "rolls the evaluation service back while the impact "
            "grows from shoppers hitting the unfinished checkout to "
            "carts being dropped. The fix is only real once the flag "
            "is confirmed off in every region: the flag evaluation "
            "cache holds the bad value on its TTL, so a test checkout "
            "clears in 3m in us-east, 5m in us-west, 7m in eu-west "
            "and 10m in ap-southeast, where the cache holds the "
            "flag longest before it re-evaluates."
        ),
        ack_delay_min=(None, 4),
        mitigate_delay_min=30,
        resolve_delay_min=15,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west", "ap-southeast"),
        # Verifying the fix is not the same price everywhere: the
        # flag evaluation cache holds the bad value on its TTL, so
        # a test checkout clears in 3m in us-east, 5m in us-west,
        # 7m in eu-west and 10m in ap-southeast, where the cache
        # holds the flag longest before it re-evaluates. The total
        # is 25m, 13m more than the 3m default, so the verified run
        # scores 57/100 where the default-cost run scored 63.
        verify_delay_min_by_region={
            "us-east": 3, "us-west": 5, "eu-west": 7, "ap-southeast": 10,
        },
        hints=(
            "Check the flag evaluation service first: an unfinished "
            "checkout appearing for everyone right after a flag "
            "rollout is the flag, not the checkout code.",
            "Kill the flag before rolling anything back: turning "
            "the flag off stops the bleeding in seconds, and the "
            "evaluation-service rollback can follow.",
            "Verify a test checkout goes through the old flow in "
            "every region before declaring the incident mitigated; "
            "the flag cache in ap-southeast holds the bad value "
            "longest on its TTL.",
        ),
        impact_stages=(
            "Shoppers hitting the unfinished checkout: the new "
            "flow erroring on payment, carts being dropped",
            "Checkout down for everyone: the unfinished flow is "
            "the only flow, orders failing, revenue stopped",
        ),
    ),
    "payment-processor-outage": Scenario(
        name="payment-processor-outage",
        service="checkout-payments",
        title="Card processor down, checkout cannot take payments",
        severity="critical",
        description=(
            "The third-party card processor is down: every card "
            "authorization errors, so checkout cannot take payments "
            "at all. The team has to decide whether to wait for the "
            "vendor to recover, fail over to the secondary processor "
            "— cards only, no wallets, a degraded path — or shed "
            "load by queueing checkouts until payments work again. "
            "The primary's page is buried under a storm of "
            "checkout-error alerts, so they miss; the secondary "
            "acks 2m late, then fails over to the secondary "
            "processor and starts queueing checkouts while the "
            "impact grows from card payments failing to checkouts "
            "queueing up. The fix is only real once a test charge "
            "actually clears in every region: the secondary "
            "processor settles in 3m in us-east, 6m in us-west and "
            "9m in eu-west, where its EU endpoint settles slowest."
        ),
        ack_delay_min=(None, 7),
        mitigate_delay_min=28,
        resolve_delay_min=18,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # test charge clears in 3m in us-east, 6m in us-west and
        # 9m in eu-west, where the secondary processor's EU
        # endpoint settles slowest. The total is 18m, 9m more
        # than the 3m default, so the verified run scores 34/100
        # where the default-cost run scored 37.
        verify_delay_min_by_region={
            "us-east": 3, "us-west": 6, "eu-west": 9,
        },
        hints=(
            "Check the processor's status page first: card "
            "failures everywhere at once with no deploy is the "
            "vendor, not checkout — and start the failover to the "
            "secondary processor instead of waiting on the vendor.",
            "Shed load while the failover runs: queue checkouts "
            "instead of letting them fail, so a customer retries "
            "once instead of hammering a dead processor.",
            "Verify a test charge actually clears in every region "
            "before declaring the incident mitigated; the "
            "secondary processor's EU endpoint settles slowest.",
        ),
        impact_stages=(
            "Card payments failing: authorizations erroring, "
            "customers retrying and failing",
            "Checkouts queueing: card payments down everywhere, "
            "orders backing up while the team fails over",
        ),
    ),
    "vault-outage": Scenario(
        name="vault-outage",
        service="secrets-vault",
        title="Secrets manager down, services cannot fetch secrets",
        severity="critical",
        description=(
            "The secrets manager is down after its storage backend "
            "lost quorum: every secret fetch errors, so any service "
            "that restarts crash-loops on startup and no deploy can "
            "ship. The team has to fail over to the secondary vault "
            "cluster — and stop restarting services until it is up, "
            "because a restart while Vault is down turns a running "
            "service into a crash-looping one. The primary's page is "
            "buried under a storm of crash-loop alerts, so they "
            "miss; the secondary is in the storage-backend review "
            "and misses too; the manager is in an incident-review "
            "meeting, phone on silent, and acks 3m late against the "
            "fast policy's 5m window, then starts the failover while "
            "the impact grows from secret fetches failing to "
            "services restarting without secrets. The fix is only "
            "real once a test secret actually fetches in every "
            "region: the replica lags in 2m in us-east, 5m in "
            "us-west and 9m in eu-west, where it lags the primary "
            "longest."
        ),
        ack_delay_min=(None, None, 8),
        mitigate_delay_min=30,
        resolve_delay_min=20,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # test secret fetches in 2m in us-east, 5m in us-west and
        # 9m in eu-west, where the replica lags the primary
        # longest. The total is 16m, 7m more than the 3m default,
        # so the verified run scores 17/100 where the
        # default-cost run scored 18.
        verify_delay_min_by_region={
            "us-east": 2, "us-west": 5, "eu-west": 9,
        },
        hints=(
            "Fail over to the secondary vault cluster first: "
            "nothing can start until secret fetches work again, "
            "and every restart without secrets crash-loops.",
            "Stop the deploy pipeline and freeze restarts while "
            "Vault is down: a restart turns a running service "
            "into a crash-looping one.",
            "Verify a test secret actually fetches in every region "
            "before declaring the incident mitigated; the eu-west "
            "replica lags the primary longest.",
        ),
        impact_stages=(
            "Secret fetches failing: new pods crash-looping on "
            "startup, deploys blocked",
            "Services restarting without secrets: checkout and "
            "payments shedding traffic",
            "Vault failover in progress: secondary cluster "
            "catching up, leases expiring",
        ),
    ),
    "schema-migration": Scenario(
        name="schema-migration",
        service="orders-api",
        title="Schema migration locks the orders table",
        severity="critical",
        description=(
            "A deploy runs a schema migration that adds a NOT NULL "
            "column with no default, and the ALTER TABLE takes an "
            "exclusive lock on the orders table: every write queues "
            "behind the migration and times out, so orders stop "
            "being placed. The primary's page is buried under a "
            "storm of slow-query alerts, so they miss; the senior "
            "is mid-deploy-review and acks 2m late against the "
            "sev1 policy's 5m window, then kills the migration "
            "and rolls it back while the impact grows from writes "
            "queueing to orders failing. The fix is only real once "
            "a test order actually writes in every region: the "
            "rollback has to reach each region's replica, so the "
            "check takes 3m in us-east, 6m in us-west and 8m in "
            "eu-west, where the replica applies the rollback "
            "slowest."
        ),
        ack_delay_min=(None, 7),
        mitigate_delay_min=40,
        resolve_delay_min=20,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # test order writes in 3m in us-east, 6m in us-west and
        # 8m in eu-west, where the replica applies the rollback
        # slowest. The total is 17m, 8m more than the 3m default,
        # so the verified run scores 32/100 where the
        # default-cost run scored 35.
        verify_delay_min_by_region={
            "us-east": 3, "us-west": 6, "eu-west": 8,
        },
        hints=(
            "Kill the migration first: the ALTER TABLE holds an "
            "exclusive lock, and no write gets through until it "
            "is gone — the lock is the incident, not the slow "
            "queries.",
            "Roll the migration back before re-running it: a "
            "migration that adds a NOT NULL column with no default "
            "will lock the table the same way a second time.",
            "Verify a test order actually writes in every region "
            "before declaring the incident mitigated; the eu-west "
            "replica applies the rollback slowest, and a region "
            "still locked keeps failing writes.",
        ),
        impact_stages=(
            "Writes queueing: inserts timing out behind the "
            "migration lock, orders backing up",
            "Orders failing: the write queue full, checkouts "
            "timing out, orders lost",
        ),
    ),
    "email-provider-outage": Scenario(
        name="email-provider-outage",
        service="comms-email",
        title="Transactional email provider down, mail never sends",
        severity="critical",
        description=(
            "The transactional email provider is down: password "
            "resets, order confirmations and receipts never send — "
            "the provider accepts the messages and drops them, so "
            "the queue grows while nothing reaches a customer. The "
            "team has to fail over to the backup provider and replay "
            "the queued mail, because a failover without the replay "
            "leaves every customer who already asked for a reset "
            "locked out. The primary's page is buried under a storm "
            "of delivery-failure alerts, so they miss; the senior "
            "is on the vendor status call and misses too; the "
            "incident commander is in an exec review, phone on "
            "silent, and acks 3m late against the sev1 policy's "
            "10m window, then starts the failover while the impact "
            "grows from password resets failing to order "
            "confirmations not sending to transactional email fully "
            "down. The fix is only real once a test email actually "
            "delivers in every region: the backup provider's "
            "regional relays clear the queue at different speeds, "
            "so the check takes 3m in us-east, 5m in us-west and "
            "8m in eu-west, where the relay re-authenticates every "
            "queued message before it sends."
        ),
        ack_delay_min=(None, None, 13),
        mitigate_delay_min=40,
        resolve_delay_min=20,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # test email delivers in 3m in us-east, 5m in us-west and
        # 8m in eu-west, where the relay re-authenticates every
        # queued message before it sends. The total is 16m, 7m
        # more than the 3m default, so the verified run scores
        # 16/100 where the default-cost run scored 19.
        verify_delay_min_by_region={
            "us-east": 3, "us-west": 5, "eu-west": 8,
        },
        hints=(
            "Fail over to the backup email provider first: "
            "transactional mail is down and every minute leaves "
            "more customers locked out of their accounts.",
            "Replay the queued mail after the failover: the queue "
            "holds password resets customers are still waiting on, "
            "and skipping the replay leaves them locked out even "
            "after the provider is back.",
            "Verify a test email actually delivers in every region "
            "before declaring the incident mitigated; the eu-west "
            "relay re-authenticates every queued message before it "
            "sends, and a region still queueing keeps customers "
            "waiting.",
        ),
        impact_stages=(
            "Password resets failing: reset emails never arriving, "
            "customers locked out of their accounts",
            "Order confirmations not sending: receipts and shipping "
            "updates queued, support tickets rising",
            "Transactional email fully down: no resets, "
            "confirmations or receipts going out, accounts "
            "unrecoverable",
        ),
    ),
    "clock-skew": Scenario(
        name="clock-skew",
        service="checkout-api",
        title="Clock skew breaks leases and ordering across regions",
        severity="critical",
        description=(
            "A bad config push kills the NTP daemon on part of the "
            "fleet, and the affected hosts drift minutes apart: "
            "leases expire early, so idempotency locks are released "
            "while requests are still in flight and duplicate "
            "charges appear; events are timestamped out of order, "
            "so checkout state disagrees across regions; and "
            "leader election flaps, because nodes that disagree "
            "about the time disagree about who holds the lock. The "
            "team has to freeze the election, restart NTP and force "
            "a step resync on the drifted hosts — a slow slew would "
            "take hours to close a minutes-wide gap — and then "
            "verify the clocks agree before trusting any lease "
            "again. The primary's page is buried under a storm of "
            "duplicate-charge alerts, so they miss; the senior is "
            "chasing a payments discrepancy and acks 4m late "
            "against the sev1 policy's 5m window, then freezes the "
            "election while the impact grows from leases expiring "
            "early to ordering breaking to leader election "
            "flapping. The fix is only real once every region's "
            "clocks agree within tolerance: the check takes 4m in "
            "us-east, 6m in us-west and 9m in eu-west, where the "
            "hosts drifted furthest and take longest to resync."
        ),
        ack_delay_min=(None, 9),
        mitigate_delay_min=40,
        resolve_delay_min=20,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: the
        # clocks are checked region by region at the price each
        # region's drift charges — 4m in us-east, 6m in us-west
        # and 9m in eu-west, where the hosts drifted furthest.
        # The total is 19m, 16m more than the 3m default, so the
        # verified run scores 10/100 where the default-cost run
        # scored 11.
        verify_delay_min_by_region={
            "us-east": 4, "us-west": 6, "eu-west": 9,
        },
        hints=(
            "Freeze the leader election first: no decision made on "
            "a skewed clock can be trusted, and every election "
            "held while the clocks disagree makes the flapping "
            "worse.",
            "Restart NTP and force a step resync on the drifted "
            "hosts: a slow slew takes hours to close a "
            "minutes-wide gap, and the leases stay broken until "
            "the clocks agree.",
            "Verify every region's clocks agree within tolerance "
            "before declaring the incident mitigated; the eu-west "
            "hosts drifted furthest and take longest to resync, "
            "and a region still skewed keeps expiring leases "
            "early.",
        ),
        impact_stages=(
            "Leases expiring early: idempotency locks released "
            "while requests are still in flight, duplicate charges "
            "appearing",
            "Ordering broken: events timestamped out of order, "
            "checkout state disagreeing across regions",
            "Leader election flapping: nodes disagreeing about who "
            "holds the lock, writes failing",
        ),
    ),
    "search-index-corruption": Scenario(
        name="search-index-corruption",
        service="search-index",
        title="Search index corrupted after a bad deploy",
        severity="critical",
        description=(
            "A bad deploy ships a new analyzer that tokenizes "
            "product names wrong, and a disk fault on one shard "
            "host corrupts the segments it holds: queries return "
            "stale listings for products that changed days ago, "
            "new products never appear in results at all, and "
            "some searches return products that have nothing to "
            "do with the query. There is no clean alert — the "
            "search error rate stays flat, because the queries "
            "succeed, they just return the wrong answers — so the "
            "team only finds out from user reports and a slow "
            "climb in zero-result searches, and then has to freeze "
            "writes to the index, rebuild it from the source of "
            "truth instead of the corrupted segments, and verify "
            "a sample of known queries returns the right results "
            "before trusting search again. The primary's page is "
            "buried under a storm of zero-result-search alerts, "
            "so they miss; the senior is chasing a checkout "
            "discrepancy and acks 2m late against the sev1 "
            "policy's 5m window, then freezes index writes while "
            "the impact grows from results going stale to results "
            "coming back wrong to search becoming unusable. The "
            "fix is only real once a sample of known queries "
            "returns the right results in every region: the check "
            "takes 5m in us-east, 7m in us-west and 10m in "
            "eu-west, where the index shard is largest and takes "
            "longest to rebuild."
        ),
        ack_delay_min=(None, 7),
        mitigate_delay_min=45,
        resolve_delay_min=15,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # sample of known queries is checked region by region at
        # the price each region's rebuild charges — 5m in
        # us-east, 7m in us-west and 10m in eu-west, where the
        # index shard is largest and takes longest to rebuild.
        # The total is 22m, 13m more than the 3m default, so the
        # verified run scores 29/100 where the default-cost run
        # scored 34.
        verify_delay_min_by_region={
            "us-east": 5, "us-west": 7, "eu-west": 10,
        },
        hints=(
            "Freeze writes to the index first: every document "
            "indexed against the corrupted postings makes the "
            "rebuild harder to verify.",
            "Rebuild the index from the source of truth, not from "
            "the corrupted segments: reindexing bad data just "
            "reproduces the corruption.",
            "Verify a sample of known queries returns the right "
            "results in every region before declaring the incident "
            "mitigated; the eu-west shard is largest and takes "
            "longest to rebuild, and a region still serving stale "
            "results keeps customers from finding what they "
            "searched for.",
        ),
        impact_stages=(
            "Search results stale: listings days out of date, new "
            "products missing from results",
            "Search results wrong: corrupted postings returning "
            "unrelated products, conversion dropping",
            "Search unusable: index rebuild in progress, queries "
            "timing out or returning nothing",
        ),
    ),
    "object-storage-outage": Scenario(
        name="object-storage-outage",
        service="media-store",
        title="Object storage outage breaks uploads and images",
        severity="critical",
        description=(
            "A bad deploy to the storage gateway corrupts the "
            "metadata index the object store uses to address its "
            "objects: the bytes are still on disk, but reads cannot "
            "find them, so product images 404, uploads return 500s, "
            "and receipts cannot be fetched. There is no clean "
            "alert — the store's health check only probes the "
            "gateway, which still answers, so the team only finds "
            "out from a climb in broken-image reports and failed "
            "uploads, and then has to fail over reads to the "
            "secondary region's replicated bucket and replay the "
            "queued uploads, because a failover without the replay "
            "leaves every upload from the outage window lost. The "
            "primary acks in 3m, on time against the standard "
            "policy's 5m window, then fails over reads while the "
            "impact grows from uploads failing to images breaking "
            "to the store becoming unusable. The fix is only real "
            "once a sample of known objects returns the right bytes "
            "in every region: the check takes 4m in us-east, 6m in "
            "us-west and 9m in eu-west, where the replica lags the "
            "primary longest and takes longest to catch up."
        ),
        ack_delay_min=(3,),
        mitigate_delay_min=40,
        resolve_delay_min=15,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # sample of known objects is checked region by region at
        # the price each region's replica charges — 4m in us-east,
        # 6m in us-west and 9m in eu-west, where the replica lags
        # the primary longest and takes longest to catch up.
        # The total is 19m, 10m more than the 3m default, so the
        # verified run scores 77/100 where the default-cost run
        # scored 82.
        verify_delay_min_by_region={
            "us-east": 4, "us-west": 6, "eu-west": 9,
        },
        hints=(
            "Fail over reads to the secondary region's replicated "
            "bucket first: the objects are still on disk there, and "
            "reads recover while the primary index is repaired.",
            "Replay the queued uploads after the failover: a "
            "failover without the replay leaves every upload from "
            "the outage window lost.",
            "Verify a sample of known objects — an upload, a "
            "product image and a receipt — returns the right bytes "
            "in every region before declaring the incident "
            "mitigated; the eu-west replica lags longest, and a "
            "region still serving 404s keeps customers from seeing "
            "what they bought.",
        ),
        impact_stages=(
            "Uploads failing: new images and receipts cannot be "
            "stored, uploads returning 500s",
            "Images breaking: product pages serving broken images "
            "as cached copies expire",
            "Store unusable: reads failing across regions, uploads "
            "queued, receipts unreachable",
        ),
    ),
    "container-registry-outage": Scenario(
        name="container-registry-outage",
        service="image-registry",
        title="Container registry outage blocks deploys and image pulls",
        severity="critical",
        description=(
            "The container registry stops serving image pulls: the "
            "registry is unreachable, and when it does answer it "
            "serves stale or corrupt manifests, so new deploys fail "
            "at the pull step, rollbacks are blocked for the same "
            "reason, and any node that restarts cannot pull its "
            "image and stays down. Running workloads are unaffected "
            "until they restart, which is what makes the incident "
            "sneaky: the service looks healthy while the fleet "
            "quietly loses the ability to change. There is no clean "
            "alert — the registry's health check probes the load "
            "balancer, which still answers, so the team only finds "
            "out from a climb in failed deploys and image-pull "
            "errors, and then has to fail over pulls to the mirrored "
            "registry and promote the cached images, because a "
            "failover without the promotion leaves every node "
            "pulling from a mirror that never held the images it "
            "needs. The primary acks in 4m, on time against the "
            "standard policy's 5m window, then fails over pulls "
            "while the impact grows from deploys failing to nodes "
            "unable to restart to rollbacks blocked. The fix is "
            "only real once a known image pulls and starts in every "
            "region: the check takes 3m in us-east, 6m in us-west "
            "and 11m in eu-west, where the mirror is coldest and "
            "the largest base images take longest to re-pull."
        ),
        ack_delay_min=(4,),
        mitigate_delay_min=35,
        resolve_delay_min=15,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # known image is pulled and started region by region at
        # the price each region's mirror charges — 3m in us-east,
        # 6m in us-west and 11m in eu-west, where the mirror is
        # coldest and the largest base images take longest to
        # re-pull. The total is 20m, 11m more than the 3m
        # default, so the verified run scores 78/100 where the
        # default-cost run scored 84.
        verify_delay_min_by_region={
            "us-east": 3, "us-west": 6, "eu-west": 11,
        },
        hints=(
            "Fail over pulls to the mirrored registry first: the "
            "images are still cached there, and deploys recover "
            "while the primary registry is repaired.",
            "Promote the cached images after the failover, and pin "
            "deploys to image digests: a failover without the "
            "promotion leaves nodes pulling from a mirror that "
            "never held the images they need, and the latest tag "
            "cannot be trusted while the registry serves stale "
            "manifests.",
            "Verify a known image pulls and starts in every region "
            "before declaring the incident mitigated; the eu-west "
            "mirror is coldest and takes longest to warm, and a "
            "region still failing pulls keeps its nodes from "
            "restarting.",
        ),
        impact_stages=(
            "Deploys failing: new deploys failing at the image "
            "pull step, rollbacks blocked for the same reason",
            "Nodes unable to restart: restarted nodes cannot pull "
            "their image and stay down, fleet slowly shrinking",
            "Fleet frozen: no deploy, no rollback and no restart "
            "can pull an image anywhere",
        ),
    ),
    "kafka-outage": Scenario(
        name="kafka-outage",
        service="orders-events",
        title="Message broker outage stalls order events",
        severity="critical",
        description=(
            "The message broker cluster loses quorum: a bad broker "
            "deploy and a disk fault on two brokers take the "
            "cluster down, so producers cannot publish and "
            "consumers stall, and order events stop flowing — "
            "orders are still placed, but fulfilment, email and "
            "inventory never hear about them, so placed orders "
            "sit invisible downstream. There is no clean alert — "
            "the producers buffer and retry silently, and consumer "
            "lag climbs slowly, so the team only finds out from "
            "customers asking where their order confirmation is, "
            "and then has to fail over producers and consumers to "
            "the standby cluster and replay from the last "
            "committed offsets, because a failover without the "
            "replay loses every event from the outage window, and "
            "replaying from the start double-processes orders. "
            "The primary acks in 3m, on time against the standard "
            "policy's 5m window, then fails over to the standby "
            "cluster while the impact grows from events stalling "
            "to consumers lagging to orders stuck. The fix is only "
            "real once a test event publishes and is consumed in "
            "every region: the check takes 2m in us-east, 5m in "
            "us-west and 9m in eu-west, where the consumers lag "
            "longest and take longest to catch up."
        ),
        ack_delay_min=(3,),
        mitigate_delay_min=30,
        resolve_delay_min=12,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # test event is published and consumed region by region
        # at the price each region's consumers charge — 2m in
        # us-east, 5m in us-west and 9m in eu-west, where the
        # consumers lag longest and take longest to catch up.
        # The total is 16m, 7m more than the 3m default, so the
        # verified run scores 83/100 where the default-cost run
        # scored 87.
        verify_delay_min_by_region={
            "us-east": 2, "us-west": 5, "eu-west": 9,
        },
        hints=(
            "Fail over producers and consumers to the standby "
            "cluster first: events flow again while the primary "
            "brokers are repaired.",
            "Replay from the last committed offsets after the "
            "failover: a failover without the replay loses every "
            "event from the outage window, and replaying from the "
            "start double-processes orders.",
            "Verify a test event publishes and is consumed in "
            "every region before declaring the incident mitigated; "
            "the eu-west consumers lag longest and take longest "
            "to catch up, and a region still not consuming keeps "
            "its orders stuck.",
        ),
        impact_stages=(
            "Events stalling: producers buffering and retrying, "
            "order events not reaching fulfilment or email",
            "Consumers lagging: consumer lag climbing across "
            "topics, downstream services working from stale state",
            "Orders stuck: placed orders invisible to fulfilment, "
            "inventory and shipping",
        ),
    ),
    "kubernetes-control-plane-outage": Scenario(
        name="kubernetes-control-plane-outage",
        service="kubernetes-control-plane",
        title="Kubernetes control plane outage blocks deploys and scaling",
        severity="critical",
        description=(
            "The Kubernetes control plane goes down: a bad "
            "control-plane upgrade and a disk fault on two etcd "
            "members take the API servers offline, so kubectl "
            "commands fail, rollouts stick and no new pods can be "
            "scheduled — deploys and scaling are blocked, while "
            "running workloads keep serving until they need a "
            "restart or a new replica, which is what makes the "
            "incident sneaky: the platform looks healthy while "
            "the fleet quietly loses the ability to change. There "
            "is no clean alert — the kubelets keep the running "
            "pods alive and their health checks pass, so the team "
            "only finds out from a climb in failed kubectl "
            "commands and stuck rollouts, and then has to fail "
            "over to the standby control plane and restore etcd "
            "from snapshot, because a failover without the "
            "restore loses every deployment, service and config "
            "the cluster held since the last snapshot. The "
            "primary acks in 3m, on time against the standard "
            "policy's 5m window, then fails over to the standby "
            "control plane while the impact grows from deploys "
            "blocked to scaling frozen to cluster state at risk. "
            "The fix is only real once a test deploy rolls out "
            "and scales in every region: the check takes 3m in "
            "us-east, 6m in us-west and 10m in eu-west, where the "
            "standby control plane is coldest and the etcd "
            "snapshot takes longest to restore."
        ),
        ack_delay_min=(3,),
        mitigate_delay_min=38,
        resolve_delay_min=12,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # test deploy is rolled out and scaled region by region
        # at the price each region's standby control plane
        # charges — 3m in us-east, 6m in us-west and 10m in
        # eu-west, where the standby is coldest and the etcd
        # snapshot takes longest to restore. The total is 19m,
        # 10m more than the 3m default, so the verified run
        # scores 78/100 where the default-cost run scored 83.
        verify_delay_min_by_region={
            "us-east": 3, "us-west": 6, "eu-west": 10,
        },
        hints=(
            "Fail over to the standby control plane first: "
            "deploys and scaling recover while the primary "
            "control plane is repaired.",
            "Restore etcd from snapshot after the failover: a "
            "failover without the restore loses every deployment, "
            "service and config the cluster held since the last "
            "snapshot, and a stale snapshot rolls the cluster "
            "back further than the outage.",
            "Verify a test deploy rolls out and scales in every "
            "region before declaring the incident mitigated; the "
            "eu-west standby is coldest and takes longest to "
            "restore, and a region still unable to schedule "
            "keeps its workloads frozen.",
        ),
        impact_stages=(
            "Deploys blocked: kubectl commands failing, rollouts "
            "stuck, new pods unschedulable",
            "Scaling frozen: autoscalers unable to act, traffic "
            "growth with nowhere to go, nodes unable to be "
            "replaced",
            "Cluster state at risk: etcd quorum lost, deployments "
            "and services exist only in the snapshot",
        ),
    ),
    "load-balancer-ingress-outage": Scenario(
        name="load-balancer-ingress-outage",
        service="edge-ingress",
        title="Load balancer / ingress outage drops external traffic",
        severity="critical",
        description=(
            "A bad ingress config deploy pushes a routing rule that "
            "sends all external traffic to a decommissioned backend "
            "pool: the load balancer still answers, but every "
            "request 502s or times out at the edge, while the "
            "services behind it stay healthy — internal health "
            "checks pass and the dashboards stay green, which is "
            "what makes the incident sneaky: the platform looks "
            "fine while no customer can reach it. There is no "
            "clean alert — the load balancer's health check probes "
            "the control plane, which still answers, so the team "
            "only finds out from a climb in 5xx errors at the edge "
            "and customer reports that the site will not load, and "
            "then has to fail over to the standby ingress and roll "
            "the bad config back, because a failover without the "
            "rollback leaves the standby serving the same bad "
            "routing rule. The primary acks in 3m, on time against "
            "the standard policy's 5m window, then fails over to "
            "the standby ingress while the impact grows from "
            "external traffic erroring to checkout unreachable to "
            "the site fully down. The fix is only real once a "
            "staged test request through the load balancer succeeds "
            "in every region: the check takes 3m in us-east, where "
            "the standby ingress is already warm, 7m in us-west, "
            "where the new config has to propagate across the load "
            "balancer control plane, and 11m in eu-west, where the "
            "standby has to drain the bad config from its cache "
            "and re-register its backends."
        ),
        ack_delay_min=(3,),
        mitigate_delay_min=34,
        resolve_delay_min=14,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # staged test request through the load balancer succeeds
        # region by region at the price each region's standby
        # ingress charges — 3m in us-east, 7m in us-west and 11m
        # in eu-west, where the standby has to drain the bad
        # config from its cache and re-register its backends.
        # The total is 21m, 12m more than the 3m default, so the
        # verified run scores 79/100 where the default-cost run
        # scored 85.
        verify_delay_min_by_region={
            "us-east": 3, "us-west": 7, "eu-west": 11,
        },
        hints=(
            "Check the ingress config deploy first: 5xx errors at "
            "the edge with healthy services behind it is the load "
            "balancer's routing, not the services — compare the "
            "edge 5xx rate with the internal success rate before "
            "digging into any one service.",
            "Fail over to the standby ingress before rolling the "
            "config back: traffic recovers while the bad config "
            "is unwound, and roll the config back too — a failover "
            "without the rollback leaves the standby serving the "
            "same bad routing rule.",
            "Verify a staged test request through the load balancer "
            "succeeds in every region before declaring the incident "
            "mitigated; the eu-west standby drains the bad config "
            "from its cache slowest, and a region still serving "
            "502s keeps its customers locked out.",
        ),
        impact_stages=(
            "External traffic erroring: 5xx errors at the edge, "
            "customers seeing errors while the services behind the "
            "load balancer stay healthy",
            "Checkout unreachable: external requests timing out, "
            "orders failing at the edge",
            "Site fully down: no external traffic reaching any "
            "service, customers cannot load the site",
        ),
    ),
    "cicd-pipeline-outage": Scenario(
        name="cicd-pipeline-outage",
        service="ci-cd-pipeline",
        title="CI/CD pipeline outage blocks builds and deploys",
        severity="critical",
        description=(
            "A bad runner-fleet upgrade takes the CI/CD runners "
            "offline and corrupts the job queue's scheduler state: "
            "builds queue and never start, deploys block at the "
            "pipeline step, and rollbacks are unavailable because "
            "the rollback path runs through the same pipeline, "
            "while the services already running stay healthy — "
            "production looks fine while the team quietly loses "
            "the ability to ship anything, which is what makes "
            "the incident sneaky. There is no clean alert — the "
            "pipeline dashboard shows jobs queued, not failed, so "
            "the team only finds out from a climb in queued builds "
            "and engineers reporting that deploys are stuck, and "
            "then has to fail over to the standby runner fleet "
            "and drain the queued jobs through it, because a "
            "failover without the drain leaves every queued build "
            "and deploy stranded, and the manual deploy path is "
            "the only way to ship a hotfix while the queue drains. "
            "The primary acks in 3m, on time against the standard "
            "policy's 5m window, then fails over to the standby "
            "runners while the impact grows from builds queueing "
            "to deploys blocked to delivery fully stalled. The fix "
            "is only real once a test pipeline runs green in every "
            "region: the check takes 4m in us-east, where the "
            "standby fleet is already warm, 6m in us-west, where "
            "the new runners have to register before they can take "
            "jobs, and 10m in eu-west, where the standby fleet has "
            "to rebuild its image cache before a build can run."
        ),
        ack_delay_min=(3,),
        mitigate_delay_min=36,
        resolve_delay_min=15,
        slo_percent=99.9,
        regions=("us-east", "us-west", "eu-west"),
        # Verifying the fix is not the same price everywhere: a
        # test pipeline runs green region by region at the price
        # each region's standby runner fleet charges — 4m in
        # us-east, 6m in us-west and 10m in eu-west, where the
        # standby fleet has to rebuild its image cache before a
        # build can run. The total is 20m, 11m more than the 3m
        # default, so the verified run scores 78/100 where the
        # default-cost run scored 84.
        verify_delay_min_by_region={
            "us-east": 4, "us-west": 6, "eu-west": 10,
        },
        hints=(
            "Check the runner fleet first: builds queueing with no "
            "failures is the pipeline, not the code — compare the "
            "queued-build count with the runner heartbeat before "
            "digging into any one repo's failing tests.",
            "Fail over to the standby runner fleet before draining "
            "the queue: jobs start moving while the bad scheduler "
            "state is unwound, and drain the queue too — a failover "
            "without the drain leaves every queued build and "
            "deploy stranded.",
            "Verify a test pipeline runs green in every region "
            "before declaring the incident mitigated; the eu-west "
            "standby fleet rebuilds its image cache slowest, and "
            "a region still queueing builds keeps its deploys "
            "blocked.",
        ),
        impact_stages=(
            "Builds queueing: jobs piling up in the queue, no "
            "builds starting, engineers waiting on CI",
            "Deploys blocked: no release can ship, rollbacks "
            "unavailable, hotfixes stuck in the queue",
            "Delivery fully stalled: the pipeline cannot run "
            "anything, the team cannot ship or roll back",
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
        "slo_percent": scenario.slo_percent,
        "regions": list(scenario.regions),
        "verify_delay_min_by_region": dict(
            scenario.verify_delay_min_by_region),
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
    regions = _require_regions(data, name)
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
        slo_percent=_require_slo(data, name),
        regions=tuple(regions),
        verify_delay_min_by_region=_require_verify_delay_by_region(
            data, name, regions),
    )


def _require_verify_delay_by_region(data: dict, name: str,
                                    regions: list) -> dict:
    """Validate a scenario file's optional ``verify_delay_min_by_region``.

    A mapping from region name to the minutes verifying the fix in
    that region costs. Absent means every region costs the default
    (``DEFAULT_VERIFY_MIN_PER_REGION``); a region listed here costs
    what it says, and a region left out keeps the default. Every key
    must be one of the scenario's ``regions`` — a cost for a region
    the scenario never verifies is a typo — and every value must be
    a positive integer number of minutes.
    """
    raw = data.get("verify_delay_min_by_region", {})
    if not isinstance(raw, dict):
        raise ValueError(
            f"scenario {name!r}: 'verify_delay_min_by_region' must be "
            "an object mapping region names to minutes")
    for region, minutes in raw.items():
        if region not in regions:
            raise ValueError(
                f"scenario {name!r}: 'verify_delay_min_by_region' names "
                f"{region!r}, which is not one of the scenario's "
                f"regions {regions}")
        if (isinstance(minutes, bool) or not isinstance(minutes, int)
                or minutes <= 0):
            raise ValueError(
                f"scenario {name!r}: 'verify_delay_min_by_region' for "
                f"{region!r} must be a positive integer number of "
                "minutes")
    return dict(raw)


def _require_regions(data: dict, name: str) -> list:
    """Validate a scenario file's optional ``regions`` list.

    Regions are where the remediation must be confirmed: with
    per-region purge verification the fix is checked from a cold
    edge node in each one before the incident is declared
    mitigated. Absent means the scenario has no regions and
    per-region verification does not apply to it; present it must
    be a list of non-empty strings, with no region listed twice.
    """
    raw = data.get("regions", [])
    if not isinstance(raw, list):
        raise ValueError(
            f"scenario {name!r}: 'regions' must be a list of strings")
    for index, region in enumerate(raw):
        if not isinstance(region, str) or not region:
            raise ValueError(
                f"scenario {name!r}: 'regions' entry {index} must be "
                "a non-empty string")
    if len(set(raw)) != len(raw):
        raise ValueError(
            f"scenario {name!r}: 'regions' must not list a region twice")
    return raw


def _require_slo(data: dict, name: str) -> object:
    """Validate a scenario file's optional ``slo_percent``.

    Absent (or null) means the scenario carries no SLO and no
    error-budget burn is reported. Present it must be a number
    between 90 (inclusive) and 100 (exclusive): an SLO of 100% allows
    no downtime at all, so there is no budget to burn against, and
    anything below 90% is not an SLO worth tracking.
    """
    raw = data.get("slo_percent")
    if raw is None:
        return None
    if (isinstance(raw, bool) or not isinstance(raw, (int, float))
            or not 90 <= raw < 100):
        raise ValueError(
            f"scenario {name!r}: 'slo_percent' must be a number from "
            f"90 up to (but not including) 100, got {raw!r}")
    return raw


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
