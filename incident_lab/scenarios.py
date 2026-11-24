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
            "fails over to the backup DNS provider."
        ),
        ack_delay_min=(None, 3, None),
        mitigate_delay_min=40,
        resolve_delay_min=25,
        slo_percent=99.99,
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
        slo_percent=99.9,
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
            "timeouts to orders stuck to a split-brain risk."
        ),
        ack_delay_min=(None, None, 8),
        mitigate_delay_min=45,
        resolve_delay_min=25,
        slo_percent=99.9,
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
            "up to billing events stuck."
        ),
        ack_delay_min=(None, 4),
        mitigate_delay_min=50,
        resolve_delay_min=20,
        slo_percent=99.5,
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
