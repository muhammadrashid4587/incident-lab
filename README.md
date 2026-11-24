# incident-lab

An SRE incident simulator with escalation policies, timeline replay,
and postmortem debriefs.
Pure Python 3, standard library only, no dependencies.

## What it is

A training and planning tool for on-call incident response. You define a
service and an escalation policy (primary, secondary, manager, each with an
ack timeout), pick a scenario from a small library of realistic incidents,
and the simulator plays the whole incident out on a simulated clock:

- **page** the primary, **ack** (or time out and **escalate**),
- **mitigate**, then **resolve**.

Every event is recorded with a timestamp into a timeline you can replay and
print, and the response is scored on time-to-ack, time-to-mitigate and
escalations.

## What it is not

Not a real pager. Nothing here pages a real person, sends a real alert, or
talks to PagerDuty, Opsgenie or any incident tool. The clock is simulated
(the whole incident runs instantly), and responder behaviour is fixed by
the scenario, not by real humans.

## Quickstart

```sh
python3 -m incident_lab list
python3 -m incident_lab run disk-full
python3 -m incident_lab run latency-spike
python3 -m incident_lab run disk-full --json
python3 -m incident_lab run latency-spike --policy fast
python3 -m incident_lab run disk-full --difficulty easy
python3 -m incident_lab run disk-full --difficulty hard
python3 -m incident_lab run latency-spike --difficulty 3 --seed 7 --json
python3 -m incident_lab run latency-spike --ack-timeouts 120,300,600
python3 -m incident_lab run latency-spike --ack-timeouts primary=120 L2=300
python3 -m incident_lab run disk-full --policy-file examples/custom-policy.json
python3 -m incident_lab policy-template > my-policy.json
python3 -m incident_lab policy-template fast --out my-fast-policy.json
python3 -m incident_lab run disk-full --policy-file my-policy.json
python3 -m incident_lab run disk-full --rota-file examples/rota.json
python3 -m incident_lab run disk-full --rota-file examples/rota.json --rota-week 1
python3 -m incident_lab run dns-outage --json > /tmp/dns-outage.json
python3 -m incident_lab run disk-full --seed 7 --json
python3 -m incident_lab run disk-full --seed 7 --jitter 0-3 --json
python3 -m incident_lab run disk-full --seeds 1,2,3
python3 -m incident_lab run disk-full --seeds 1-5 --json
python3 -m incident_lab replay /tmp/dns-outage.json
python3 -m incident_lab run latency-spike --json > /tmp/latency-spike.json
python3 -m incident_lab debrief /tmp/latency-spike.json
python3 -m incident_lab debrief /tmp/latency-spike.json --json
python3 -m incident_lab postmortem /tmp/latency-spike.json
python3 -m incident_lab postmortem /tmp/latency-spike.json --out postmortem.html
python3 -m incident_lab postmortem /tmp/latency-spike.json --format pdf --out postmortem.pdf
python3 -m incident_lab run disk-full --seed 1 --json > /tmp/run-seed-1.json
python3 -m incident_lab run disk-full --seed 7 --json > /tmp/run-seed-7.json
python3 -m incident_lab compare /tmp/run-seed-1.json /tmp/run-seed-7.json
python3 -m incident_lab compare /tmp/run-seed-1.json /tmp/run-seed-7.json --json
python3 -m incident_lab stats /tmp/run-seed-1.json /tmp/run-seed-7.json
python3 -m incident_lab stats /tmp/run-seed-1.json /tmp/run-seed-7.json --json
python3 -m incident_lab run payments-outage --policy sev1
python3 -m incident_lab run payments-outage --policy sev1 --json
python3 -m incident_lab run sso-outage
python3 -m incident_lab run sso-outage --json
python3 -m incident_lab run payments-outage --policy sev1 --json > /tmp/payments-outage.json
python3 -m incident_lab replay /tmp/payments-outage.json
python3 -m incident_lab debrief /tmp/payments-outage.json
python3 -m incident_lab template disk-full > my-disk-full.json
python3 -m incident_lab run disk-full --scenario-file my-disk-full.json
python3 -m incident_lab run cache-down --scenario-file examples/custom-scenario.json
python3 -m incident_lab run disk-full --json > /tmp/disk-full.json
python3 -m incident_lab what-if /tmp/disk-full.json --ack-timeout 10
python3 -m incident_lab what-if /tmp/disk-full.json --difficulty hard
python3 -m incident_lab what-if /tmp/disk-full.json --ack-timeouts primary=2 --json
python3 -m incident_lab what-if /tmp/disk-full.json --policy standard
python3 -m incident_lab what-if /tmp/disk-full.json --policy fast --difficulty hard --json
python3 -m incident_lab what-if /tmp/disk-full.json --ack-timeout 5 --json > /tmp/disk-full-whatif.json
python3 -m incident_lab compare /tmp/disk-full.json /tmp/disk-full-whatif.json
python3 -m incident_lab stats /tmp/run-seed-1.json /tmp/disk-full-whatif.json
python3 -m incident_lab drill disk-full
printf 'ack 3\nmitigate 20\nresolve 12\n' | python3 -m incident_lab drill disk-full --json > /tmp/drill.json
python3 -m incident_lab drill latency-spike --difficulty hard
python3 -m incident_lab replay /tmp/drill.json
python3 -m incident_lab debrief /tmp/drill.json
printf 'ack 3\nmitigate 20\nresolve 12\n' | python3 -m incident_lab drill disk-full --operator ana
python3 -m incident_lab history --operator ana
python3 -m incident_lab history
python3 -m incident_lab bundle /tmp/latency-spike.json
python3 -m incident_lab bundle --check latency-spike-bundle.zip
python3 -m incident_lab shift disk-full latency-spike
python3 -m incident_lab shift disk-full latency-spike --slo 99.5
python3 -m incident_lab shift disk-full latency-spike cert-expiry --json > /tmp/shift.json
python3 -m incident_lab replay /tmp/shift.json
python3 -m incident_lab debrief /tmp/shift.json
python3 -m incident_lab postmortem /tmp/shift.json --out shift-postmortem.html
python3 -m incident_lab postmortem /tmp/shift.json --format pdf --out shift-postmortem.pdf
python3 -m incident_lab bundle /tmp/shift.json
python3 -m incident_lab compare /tmp/shift.json /tmp/shift.json
python3 -m incident_lab stats /tmp/shift.json
python3 scripts/demo.py
```

## JSON output

`run --json` prints the whole result as machine-readable JSON instead of
the human replay: incident, policy (levels with responders and ack
timeouts), the timeline in time order, the score, and any hints asked for
(each with its text and cost). It also carries the escalation chain
(`escalation_chain`, one step per level walked — responder, ack
timeout, acked or missed, impact in effect when paged; see Escalation
chain). The score carries
the acking level's lateness (`ack_late_min`, 0 for an on-time ack) and
the partial credit it kept (`ack_credit`, 1.0 on time). The score also
carries the effective mitigate and resolve delays
(`mitigate_delay_min`, `resolve_delay_min`, `null` when the incident
was never mitigated or resolved), which vary with `--seed`. The score
also carries the error-budget burn (see Error budget): the service's
SLO target (`slo_percent`), the downtime in minutes from declaration
to mitigated (`downtime_min`), and the share of the monthly error
budget that downtime burned (`error_budget_burn_pct`, over 100 when
the incident alone spent the whole month's budget). Unreached milestones
serialize as `null` (for example, an incident nobody acks has
`"time_to_ack_min": null` and `"points": 0`). The top-level `seed` records
the `--seed` value used for the run, or `null` for an unseeded run, and the
top-level `jitter` records the `--jitter` range as `[lo, hi]`, or `null`
for an unseeded run. The top-level `difficulty` records the difficulty
the run was played at (`easy`, `normal` or `hard`; see below), and the
top-level `rota_week` records the rota week the run was played for,
or `null` when no rota file was used (see On-call rota).

A batch run (`--seeds`, below) prints a different JSON shape: `seeds`
(the seed list), `jitter`, `difficulty`, the shared `incident` and
`policy`, every run in full under `runs` (each one a single-run result
as above), and `score_distribution` with the mean, min, max and median
points plus a `per_seed` list of `{"seed": N, "points": P}` entries.

A shift run (`shift --json`, see Shifts) prints a third shape: a
`shift` summary (`slo_percent`, `budget_minutes`, `budget_exhausted`,
`total_burn_pct`, `shift_points`, one `incidents` entry per incident
that ran with its scenario, points, downtime, burn against the shared
budget and cumulative burn, plus `skipped`, `difficulty`, `seed`,
`jitter` and `rota_week`), the shared `policy`, and every incident's
full run under `runs` (each one a single-run result as above, with
its own SLO burn in its own score — the shared-budget burn lives
only in the `shift` summary).

## Shifts

A shift runs several incidents back-to-back against one shared error
budget, instead of each incident's own SLO: `shift disk-full
latency-spike` plays the two incidents in order, and the burn
accumulates — the second incident starts with whatever budget the
first one left. The shared SLO comes from `--slo` (a number from 90
up to but not including 100), or from the first scenario's SLO when
no `--slo` is given; the shift fails when no scenario carries an SLO.
When the cumulative burn reaches 100% the budget is exhausted and
the shift ends early: the remaining incidents are skipped, and the
shift score counts them as 0, so a shift that spends its whole
month's budget part-way through scores down for the incidents it
never got to run. The shift score is the mean points over every
scheduled incident. Each incident is simulated exactly as `run`
would simulate it — same policy, difficulty, seed and rota, with
`--seed N` giving incident `i` the seed `N+i` so the shift replays
exactly — and `replay`, `debrief`, `compare` and `stats` all accept
a saved shift file: `replay` re-prints the shift summary, `debrief`
writes a shift postmortem (per-incident rows plus shift-level
findings about the budget), `compare` diffs two shifts (shift points,
total burn, incidents ran, whether each ran out of budget, and a
per-incident points table for the scenarios both ran; a shift and a
single run cannot be compared), and `stats` aggregates each incident
in a shift as one run, saying how many of its files were shifts.
`postmortem` and `bundle` accept shift files too: `postmortem`
renders the shift postmortem — the shift summary, one row per
incident that ran and one per skipped incident, the shift-level
findings and action items, then one section per incident that ran
with that incident's own debrief findings — as HTML or PDF, and
`bundle` packages the shift JSON with that HTML postmortem and a
manifest carrying the shift's score, total burn and the incidents
that ran and were skipped, validated the same way a run bundle is.

## Replay

`replay <path>` reads a JSON file saved from `run --json` and re-prints
the timeline and score in the same human-readable format `run` uses, so
you can review a past incident without re-running it:

```sh
python3 -m incident_lab run dns-outage --json > dns-outage.json
python3 -m incident_lab replay dns-outage.json
```

A missing file, bad JSON, or a file that is not a saved run fails with
exit code 2 and an error message.

## Debrief

`debrief <path>` reads a JSON file saved from `run --json` and writes
the postmortem for the incident as Markdown: a summary (score and
grade — A at 90, B at 75, C at 50, else D — plus time-to-ack,
time-to-mitigate and time-to-resolve), the timeline, what went well,
what went badly, and action items. Every finding is derived from the
run itself, never invented: a missed page names the responder and
becomes an action to check their paging delivery, a late ack names the
lateness and the credit it kept, and slow mitigation becomes an action
to write or rehearse a runbook. An incident nobody acked lists every
missed level and adds a fallback-page action. Action items carry an
owner — the responder whose page was missed or late, or the on-call
lead for the systemic ones. With `--json` the same postmortem prints
as JSON instead of Markdown.

```sh
python3 -m incident_lab run latency-spike --json > latency-spike.json
python3 -m incident_lab debrief latency-spike.json
```

## HTML postmortem

`postmortem <path>` renders the same debrief as a self-contained HTML
page — inline CSS, no JavaScript, no external resources — so it can be
opened anywhere, attached to a ticket, or pasted into a wiki. It
carries the score and grade, the timeline, what went well and badly,
and action items with owners, plus two things the Markdown debrief does
not show: a per-responder timings table built from the escalation chain
(when each level was paged, when they acked or missed, how late they
were, and the impact in effect when they were paged), and the hints the
run used, with what each one cost. A saved `what-if --json` file renders
its replay with a provenance box naming the overrides, the points delta
against the base run, and the base run's score, so a hypothetical replay
never passes as a run that happened. A saved `shift --json` file renders
the shift postmortem instead: the shift summary, one row per incident
that ran and one per skipped incident, the shift-level findings and
action items, then one section per incident that ran with that
incident's own debrief findings. With `--out PATH` the HTML is
written to a file instead of printed; without it, it prints to stdout.
Every string from the run is escaped, so scenario text can never break
out of the page. A missing file, bad JSON, or a file that is not a saved
run fails with exit code 2 and an error message.

```sh
python3 -m incident_lab run latency-spike --json > latency-spike.json
python3 -m incident_lab postmortem latency-spike.json --out postmortem.html
python3 -m incident_lab what-if latency-spike.json --ack-timeout 5 --json > latency-spike-whatif.json
python3 -m incident_lab postmortem latency-spike-whatif.json
```

## PDF postmortem

`postmortem <path> --format pdf --out file.pdf` renders the same
postmortem as a readable multi-page PDF — scenario, score, timeline,
per-responder timings, hints with cost, findings and action items,
plus the what-if provenance box for a saved `what-if --json` file —
so it can be attached to a ticket or filed where HTML is not wanted.
A saved `shift --json` file renders the shift postmortem as a PDF
instead, with the same sections the HTML shift postmortem carries.
The writer is hand-rolled and standard library only (Letter pages,
Helvetica, no embedded fonts, no new dependency); text is wrapped
to the page width and flows across pages, each page footered with
the generator and the page number. A PDF is binary, so `--out` is
required — `--format pdf` without it fails with exit code 2, as does
a missing file, bad JSON, or a file that is not a saved run. Text
outside Latin-1 is folded to ASCII lookalikes (an em dash becomes
`-`), and parentheses and backslashes in scenario text are escaped,
so run text can never break the file.

```sh
python3 -m incident_lab run latency-spike --json > latency-spike.json
python3 -m incident_lab postmortem latency-spike.json --format pdf --out postmortem.pdf
```

## Postmortem bundle

`bundle <run.json>` packages a saved run into one shareable zip so
the run can be sent to someone else and re-imported: `run.json` (the
saved run, verbatim), `postmortem.html` (the HTML postmortem), and
`manifest.json` (incident-lab version, incident and score, seed and
difficulty, what-if overrides and points delta when the source is a
what-if replay, plus a SHA-256 hash and size for every other member).
A what-if file bundles its replay as `run.json` and its base run as
`base-run.json`, so the comparison stays replayable. The bundle is
validated as it is created — the written zip is re-opened, every hash
is re-checked, and `run.json` is loaded back through `replay` to
prove it re-imports — and a bundle that fails validation is deleted,
never left behind. `bundle --check <bundle.zip>` runs the same
validation on a bundle someone shared. After unzipping, the run is a
plain saved run, so `replay`, `debrief` and `postmortem` all work on
it. With `--out PATH` the zip is written there instead of
`<run>-bundle.zip`; a missing file, bad JSON, or a file that is not a
saved run fails with exit code 2 and an error message.

```sh
python3 -m incident_lab run latency-spike --json > latency-spike.json
python3 -m incident_lab bundle latency-spike.json
python3 -m incident_lab bundle --check latency-spike-bundle.zip
unzip latency-spike-bundle.zip -d shared-run
python3 -m incident_lab replay shared-run/run.json
python3 -m incident_lab shift disk-full latency-spike --json > shift.json
python3 -m incident_lab bundle shift.json
python3 -m incident_lab bundle --check shift-bundle.zip
```

A shift bundle carries the same three members — `run.json` (the
shift JSON, verbatim), `postmortem.html` (the shift HTML postmortem)
and `manifest.json` — with the manifest holding the shift's points
and grade, total burn, and the incidents that ran and were skipped
instead of one incident's score; validation re-loads `run.json` as
a shift and checks the postmortem covers every incident in it.

## Compare

`compare <run-a.json> <run-b.json>` diffs two saved `run --json` runs
side by side: a score table with the delta on each metric (points,
time-to-ack, time-to-mitigate, time-to-resolve, escalations, lateness,
ack credit) and a timeline diff — the events both runs share, with
how each event's time shifted, plus the events only one run has. With
`--json` the same comparison prints as JSON instead. A comparison is
most useful for two runs of the same scenario (for example, two seeds,
or the same incident before and after a policy change); comparing
different scenarios still works, but the report says plainly that the
scores are not directly comparable, and a different policy is flagged
the same way. A milestone one run never reached (nobody acked, never
mitigated) shows as `-` with no delta.

Either side may also be a saved `what-if --json` file: the comparison
then uses the what-if replay and says so plainly, with the overrides
that produced it and how many points the replay gained or lost
against its base run — so comparing a base run against its own
what-if shows exactly what the override was worth, and comparing two
what-if replays shows which override helped more. With `--json` each
side carries a `what_if` entry (`null` for a real run, or the
overrides, points delta and base score for a replay).

```sh
python3 -m incident_lab run disk-full --seed 1 --json > run-seed-1.json
python3 -m incident_lab run disk-full --seed 7 --json > run-seed-7.json
python3 -m incident_lab compare run-seed-1.json run-seed-7.json
```

A missing file, bad JSON, or a file that is not a saved run fails with
exit code 2 and an error message.

## What-if replay

`what-if <path> --ack-timeout N | --ack-timeouts ... | --difficulty X |
--policy NAME` replays a saved `run --json` run with overridden
parameters and shows how the score and grade would have changed —
what if the primary had had 10 minutes to ack, or the same incident
had been played on hard, or under the standard policy instead of
fast? The original run file is never modified; the what-if result
lives only in the output.

`--policy` swaps in a different named escalation policy (built-in, or
from `--policy-file`, the same way `run` loads one; a file policy
with the same name as a built-in one wins, and a file with exactly
one policy needs no `--policy`). The new policy's timeouts are scaled
to the original run's difficulty, so the replay stays at the
difficulty the run was played at unless `--difficulty` is also given.
Behaviour is replayed by chain position — a miss stays a miss — so
if the new chain is shorter than the level that acked, nobody acks.
An unknown policy name fails with exit code 2 and an error message.

The replay rebuilds the incident's actual behaviour from the saved
run: the acking responder's delay is the time-to-ack minus the
timeouts of the levels that missed before it (a miss stays a miss —
a timeout only decides how long the page waits, never whether the
responder acks), and the mitigate and resolve delays are the gaps
between the saved milestones. The recorded delays are replayed
exactly as they happened, so a seeded run's jitter is already baked
in and is not drawn again — with no overrides the what-if reproduces
the original run exactly. The original run's hints are kept, at the
same cost.

`--difficulty` scales the recorded ack timeouts relative to the
original run's difficulty (the saved timeouts already carry the
original difficulty's scaling) and scores against the new
difficulty's targets, so a normal run replayed on hard escalates
faster and is judged against the hard graces. An explicit
`--ack-timeout` or `--ack-timeouts` still wins for the timeouts it
sets, exactly as in `run`. At least one override is required; with
`--json` the output carries both runs in full, the overrides, the
points delta and both grades.

```sh
python3 -m incident_lab run disk-full --policy fast --json > disk-full.json
python3 -m incident_lab what-if disk-full.json --ack-timeout 5
python3 -m incident_lab what-if disk-full.json --difficulty hard --json
python3 -m incident_lab what-if disk-full.json --policy standard
```

Example: `disk-full` on the fast policy scores 48 (grade D) because
the primary's 3m ack is 1m late against the 2m timeout and the late-ack
credit halves the score. Replayed with 5m timeouts it scores 96
(grade A) — the same incident, the same responder, just a policy that
gives them time to answer. Replayed under the standard policy it
scores 96 too: the same ack is on time against the standard 5m
timeout.

## Stats

`stats <run.json>...` aggregates any number of saved `run --json` runs
into a summary: the score spread (mean, median, min, max), the mean
time-to-ack, time-to-mitigate and time-to-resolve over the runs that
reached each milestone, how many runs were acked and resolved, total
and per-run escalations, total hints used, and the grade counts
(A at 90, B at 75, C at 50, else D). With `--json` the same summary
prints as JSON instead, including one row per run. Runs spanning
several scenarios are still aggregated, but the report says plainly
that the averages mix different incidents, and it breaks the runs down
by scenario with a per-scenario mean score.

A saved `what-if --json` file may be passed too: it is aggregated as
its replayed run, and it is flagged as a what-if — the report counts
how many of the runs are hypothetical replays rather than runs that
happened, and each what-if row in `--json` carries its overrides, its
points delta against its base run, and the base run's score — so a
what-if never slips silently into an average of real runs.

```sh
python3 -m incident_lab run disk-full --seed 1 --json > run-seed-1.json
python3 -m incident_lab run disk-full --seed 7 --json > run-seed-7.json
python3 -m incident_lab stats run-seed-1.json run-seed-7.json
```

A missing file, bad JSON, or a file that is not a saved run fails with
exit code 2 and an error message. A batch file from `run --seeds --json`
is not a single run, so it is rejected the same way; pass the runs
one by one instead.

## Incident templates

`template <scenario>` prints a scenario as a shareable JSON template —
the same shape a scenario file uses — so you can save it, edit it, and
hand it to someone else:

```sh
python3 -m incident_lab template disk-full > my-disk-full.json
```

`run <name> --scenario-file <path>` loads named scenarios from a JSON
file, the same way `--policy-file` loads policies. A file scenario with
the same name as a built-in one wins. The file holds a `"scenarios"`
mapping, each entry with `service`, `title`, `severity` (`low`,
`medium`, `high` or `critical`), `description`, `ack_delay_min` (a list
of minutes per escalation level, `null` where a responder never acks),
and `mitigate_delay_min` / `resolve_delay_min`. An entry can also carry
an optional `hints` list of known-good next actions, asked for with
`run --hints N` (see Hints), and an optional `impact_stages` list —
the impact in effect at each escalation count (stage 0 when the
incident is declared, stage 1 after the first missed level, and so
on), so a SEV-1 that nobody acks quickly gets worse while the chain
walks (see Escalation chain):

```json
{"scenarios": {"cache-down": {
  "service": "sessions-cache",
  "title": "Cache cluster unreachable",
  "severity": "high",
  "description": "The sessions cache stopped accepting writes.",
  "ack_delay_min": [2, null, null],
  "mitigate_delay_min": 15,
  "resolve_delay_min": 10
}}}
```

See `examples/custom-scenario.json`. A missing file, bad JSON, or an
invalid scenario fails the run with exit code 2 and an error message.

## Scenarios

| name | service | severity | SLO | what happens |
| ---- | ------- | -------- | --- | ------------ |
| disk-full | payments-db | high | 99.9% | primary acks in 3m, mitigates in 20m |
| latency-spike | checkout-api | critical | 99.95% | primary misses the page, secondary acks in 4m |
| cert-expiry | edge-tls | high | 99.9% | primary acks in 2m, quick fix |
| deploy-regression | search-api | critical | 99.9% | primary and secondary miss, manager acks in 6m |
| queue-backlog | orders-queue | high | 99.5% | primary acks in 4m, scales consumers to drain backlog |
| dns-outage | edge-dns | critical | 99.99% | primary misses the page, secondary acks in 3m, fails over |
| payments-outage | payments-api | critical | 99.9% | SEV-1: on-call and senior miss, incident commander acks in 4m; impact grows while paging |
| sso-outage | identity-sso | critical | 99.95% | SSO logins failing, pages go to a stale rotation, nobody acks; impact grows while paging |

## Escalation policy

The default policy (`standard`) is: primary, 5 minute ack timeout;
secondary, 5 minutes; manager, 10 minutes. Pick a different built-in
policy with `run <scenario> --policy <name>`:

| policy | level 1 | level 2 | level 3 | for |
| ------ | ------- | ------- | ------- | --- |
| standard | primary 5m | secondary 5m | manager 10m | the default chain |
| fast | primary 2m | secondary 2m | manager 5m | critical services where paging delay costs more than a hasty escalation |
| sev1 | on-call 3m | senior 5m | incident commander 10m | SEV-1 incidents: short timeouts, and the impact grows with every missed level |

Override the timeout for every level with `run <scenario> --ack-timeout <minutes>`.
`--ack-timeout` applies on top of whichever policy you picked.

Override timeouts per level with `run <scenario> --ack-timeouts <spec>...`,
either as a list in level order (`--ack-timeouts 120,300,600`) or by name
(`--ack-timeouts primary=120 L2=300`, naming a role or a 1-based level
number). Named overrides may cover any subset and leave the rest of the
policy alone; a positional list must cover every level. Values must be
positive integers in minutes, and a bad value, an unknown level, or the
wrong number of timeouts fails the run with exit code 2 and an error
message. `--ack-timeout` and `--ack-timeouts` cannot be combined. The
effective timeouts are printed in the run output and in the policy
section of `run --json`.

Define your own named policies in a JSON file and load them with
`run <scenario> --policy-file <path> --policy <name>`. A file policy with
the same name as a built-in one wins. If the file holds exactly one
policy, `--policy` is optional. See `examples/custom-policy.json`:

```json
{"policies": {"relaxed": {"levels": [
  {"name": "ana", "role": "primary", "ack_timeout_min": 10},
  {"name": "ben", "role": "secondary", "ack_timeout_min": 10},
  {"name": "carla", "role": "manager", "ack_timeout_min": 20}
]}}}
```

Levels are validated on load: each needs a non-empty `name` and `role`
and a positive integer `ack_timeout_min`, and a policy needs at least
one level. A missing file, bad JSON, or an invalid policy fails the run
with exit code 2 and an error message.

## On-call rota

In real life the person holding the pager changes every week, and the
simulator's fixed names (`ana`, `ben`, `carla`) are just one week's
rotation. A rota file maps each role to a weekly rotation list of
responder names, in rotation order — one name per week — and `run` /
`drill` page whoever is on call for that week:

```json
{"rota": {"primary": ["ana", "dave", "erin"],
          "secondary": ["ben", "fay"],
          "manager": ["carla"]}}
```

```sh
python3 -m incident_lab run disk-full --rota-file examples/rota.json
python3 -m incident_lab run disk-full --rota-file examples/rota.json --rota-week 1
printf 'ack 3\nmitigate 20\nresolve 12\n' | python3 -m incident_lab drill disk-full --rota-file examples/rota.json --rota-week 2
```

`--rota-week N` picks the name at position `N % len(names)` in each
role's list, so the pager moves to the next person each week and wraps
around at the end of the list: week 0 pages `ana`, week 1 pages `dave`,
week 2 pages `erin`, week 3 pages `ana` again. With no `--rota-week`
the run uses the current ISO calendar week, so the same command run
next week pages the next person on the rotation. A role the rota does
not cover keeps the policy's responder, and a rota entry for a role no
level uses changes nothing — only the names change, never the roles,
the order or the ack timeouts, so a rota run scores exactly like the
same run without one. The week is printed in the human-readable output
(`rota week: 1`) and recorded in the `run --json` output (top-level
`rota_week`, `null` without a rota file), and `replay` re-prints it
from a saved run. A `what-if` replay keeps the original run's rota
week when it keeps the original policy, and drops it when the policy
is swapped for a different one (whose responders are that policy's
own). A missing file, bad JSON, an empty rotation list, or a negative
week fails the run with exit code 2 and an error message, as does
`--rota-week` without `--rota-file`.

## Escalation chain

Every run records the escalation chain as it was actually walked, in
the `run --json` output as a top-level `escalation_chain` list — one
step per level paged, with the responder, the ack timeout, whether
they acked or missed, when they were paged and acked, and the impact
in effect when they were paged. The human-readable output prints the
same chain after the timeline. A scenario can carry `impact_stages`
(see Incident templates): the impact in effect at each escalation
count, so a SEV-1 that nobody acks quickly gets worse while the chain
walks — each missed page moves to the next stage, and the escalation
event in the timeline says so (`impact now: ...`). The built-in
`payments-outage` scenario is the SEV-1 for this: run it with the
`sev1` policy (on-call 3m, senior 5m, incident commander 10m), where
the on-call and the senior both miss and the incident commander acks
in 4m, while the impact grows from degraded checkout to checkout down
to payments fully down. It scores 40/100 on the `sev1` policy:
100 - 30 (two escalations) - 7 (12m to ack, past the 5m grace) -
23 (62m to mitigate, past the 15m grace, halved).

`replay` re-prints the chain from a saved run (runs saved before
0.26.0 have no chain and show none), `debrief` adds a finding and an
action item when the impact grew while paging, `compare` diffs the
two runs' chains when they differ, and `stats` records the impact
each run ended at and counts the runs whose impact grew. A `what-if`
replay keeps the original run's impact stages, so the replayed chain
shows the same growth; a drill records the chain the same way `run`
does, one step per level the operator actually decided on.

## Policy templates

`policy-template [name]` prints a built-in policy as a starter JSON
policy file — the same shape `--policy-file` loads — so you can save
it, edit it, and pass it back:

```sh
python3 -m incident_lab policy-template > my-policy.json
python3 -m incident_lab policy-template fast --out my-fast-policy.json
python3 -m incident_lab run disk-full --policy-file my-policy.json
```

With no name it templates the `standard` policy; `fast` templates the
tighter chain. `--out PATH` writes the file directly instead of
printing it. The template is validated through the same loader a
policy file goes through, so it always loads cleanly — what you edit
is up to you:

- **timeouts**: change any level's `ack_timeout_min`, or the
  responders' names and roles. The template's timeouts are the
  normal-difficulty values; `--difficulty` still scales them
  (×1.5 on easy, ×0.6 on hard), exactly as for a built-in policy.
- **escalation rules**: the level order is the escalation order —
  page the first level, wait out its timeout, escalate to the next.
  Add a level to page someone else before the manager, or drop one
  to shorten the chain (a policy needs at least one level).
- **name**: rename the policy in the file to whatever you want to
  call it, and pick it with `run --policy <name>` (or leave
  `--policy` off when the file holds exactly one policy).

An unknown policy name, or an unwritable `--out` path, fails with
exit code 2 and an error message. A file you edited into an invalid
shape fails the same way when a run loads it.

## Scoring

Points start at 100 and are subtracted:

- 15 points per escalation
- 1 point per minute of time-to-ack past the ack grace (5 minutes on
  normal; the difficulty sets it)
- 1 point per 2 minutes of time-to-mitigate past the mitigate grace
  (15 minutes on normal; the difficulty sets it)
- an incident nobody acks scores 0
- 5 points per hint asked for (`run --hints N`)

A late ack still counts, but it only keeps part of the score. The ack
credit is 1.0 up to the acking level's timeout, then decays linearly to
0.0 at twice the timeout, and the effective score is the base score
above scaled by that credit. The acking level's lateness
(`ack_late_min`) and credit (`ack_credit`) are shown in the run output
and in the score section of `run --json`.

Example: `disk-full` acks in 3m and mitigates in 23m with no escalations,
so 100 - 0 - (23-15)//2 = 96. On the `fast` policy the primary's 2m
timeout makes that a 1m-late ack, so the credit is 1 - 1/2 = 0.5 and the
effective score is 96 × 0.5 = 48.

## Error budget

Every scenario carries an SLO target (`slo_percent`, for example 99.9
for `payments-db`), and the run reports how much of the monthly error
budget the incident burned. The budget is the downtime the SLO allows
in a 30-day month — (100 - SLO)% of 43,200 minutes, so 99.9% allows
43.2m and 99.99% allows 4.32m — and the incident's downtime is the
minutes from declaration to mitigated. The burn is downtime / budget
as a percent, rounded to one decimal: `disk-full` is down 23m, so it
burns 53.2% of its 99.9% budget, while `payments-outage` on the `sev1`
policy is down 62m and burns 143.5% — one incident spending the whole
month's budget. The run output shows the downtime and the burn, and
the score section of `run --json` carries `slo_percent`,
`downtime_min` and `error_budget_burn_pct` (all `null` for a scenario
with no SLO, and `downtime_min` / `error_budget_burn_pct` `null` when
the incident was never mitigated). `debrief` flags a burn over 100%
as a finding with an action to review the SLO and the response,
`compare` diffs the burn between two runs, `stats` reports the mean
burn across runs, `what-if` recomputes it from the replayed delays,
and drills record it too. Scenario files can set their own
`slo_percent` (a number from 90 up to but not including 100);
`template` prints it, and a scenario without one reports no burn.

## Hints

`run <scenario> --hints N` asks for N hints during the run: the operator
is stuck while triaging and asks for help. Each hint is one of the
scenario's known-good next actions, drawn in order (the first hint is
the most useful thing to check first), and each one costs 5 points off
the final score, after the late-ack credit scaling, floored at 0. The
hints used are printed after the score, recorded in the `run --json`
output (a top-level `hints` list of `{"text", "cost_points"}` entries,
plus `score.hints_used` and `score.hint_cost_points`), and shown again
by `replay`. Asking for more hints than the scenario has fails the run
with exit code 2 and an error message, as does a negative count;
`--hints` cannot be combined with `--seeds`, since hints are asked for
during one run. Custom scenarios loaded with `--scenario-file` can
carry their own `hints` list; `template` prints a scenario's hints
with it.

```sh
python3 -m incident_lab run disk-full --hints 1
python3 -m incident_lab run disk-full --hints 2 --json
```

Example: `disk-full` with one hint scores 96 - 5 = 91, and the hint
("Check WAL archiving...") is printed with the run.

In a drill the same hints are available mid-drill, when you are
stuck: type `hint` at any prompt to ask for the scenario's next
known-good action, in order, continuing after any hints asked for
up front with `drill --hints N`. Each one costs the same 5 points,
is printed with the milestone that is next and what the scoring
rewards at this point, and is recorded in the drill's JSON exactly
like an up-front hint, so `replay`, `debrief`, `compare` and `stats`
all see it. When the scenario's hints run out, `hint` says so and
costs nothing.

## Interactive drills

`drill <scenario>` plays an incident step by step with you as the
responder, instead of playing the scenario's fixed behaviour the way
`run` does. At each page you decide when to ack (or let the timeout
lapse and escalate), then when to mitigate and resolve, one decision
per line:

```sh
$ python3 -m incident_lab drill disk-full
drill: Disk 97% full on payments-db [high]
service:  payments-db
policy:   standard (ana (primary) 5m, ben (secondary) 5m, carla (manager) 10m)
difficulty: normal
The primary database disk is 97% full and growing. WAL archiving stalled after a bad deploy.

[t=0m] paged ana (primary) — ack timeout 5m.
  ack in how many minutes?
  [ack N, wait] > ack 3
  ana acked at 3m. mitigate in how many minutes?
  [mitigate N] > mitigate 20
  mitigated at 23m. resolve in how many minutes?
  [resolve N] > resolve 12
```

The answers are:

- `ack N` — ack N minutes after the page (`ack` alone means now).
  A delay past the level's timeout is a late ack, with the same
  partial credit `run` gives (see Scoring).
- `wait` — let the timeout lapse and escalate to the next level (on
  the last level the incident goes unacked, exactly as in `run`).
- `mitigate N` / `resolve N` — minutes after the ack / mitigation
  (`mitigate` / `resolve` alone mean now).
- `hint` — ask for the scenario's next known-good action when stuck
  (5 points, like `run --hints`): the hint is printed with the
  milestone that is next and what the scoring rewards at this point,
  and the same prompt is shown again, so a hint never answers the
  decision itself. Hints are drawn in order, continuing after any
  asked for up front with `drill --hints N`; when none are left,
  `hint` says so and costs nothing. A scenario with no hints offers
  no `hint` command.
- `quit` — end the drill cleanly at any prompt.
- `help` — list the answers the current prompt accepts.

The timeouts, the late-ack credit and the scoring are the same as
`run`: a drill that makes the same decisions as a `run` scores the
same. `disk-full` drilled as `ack 3`, `mitigate 20`, `resolve 12`
scores 96, the same as `run disk-full`. The scenario's fixed
ack/mitigate/resolve delays are the behaviour `run` plays, not the
drill's — the drill's delays are your decisions. `drill --operator
NAME` records the drill in the operator's history — see Drill
history below. `drill` takes the
same `--policy`, `--policy-file`, `--scenario-file`,
`--ack-timeout(s)`, `--difficulty` and `--hints N` as `run` (hints
are asked for up front, in order, 5 points each — or mid-drill with
`hint` at any prompt, at the same cost); it takes no
`--seed`, because there is nothing to jitter — your decisions are
the delays.

A drill needs no TTY. Read the decisions from a file with
`--input PATH`, or pipe them in on stdin — a scripted drill is just
a list of decisions, which is how drills are tested:

```sh
printf 'ack 3\nmitigate 20\nresolve 12\n' | python3 -m incident_lab drill disk-full --json > drill.json
python3 -m incident_lab drill disk-full --input decisions.txt --out drill.json
```

Prompts always go to stderr, never to stdout, so `drill --json`
output stays parseable JSON. `--out PATH` writes the result as JSON
to a file as well as printing it.

Quitting, or running out of input (EOF), ends the drill cleanly and
records a partial run: the timeline so far, plus an `abandoned`
event, scored on the milestones it reached (a milestone never
reached costs nothing, because it never happened). The partial run
is the same JSON shape as `run --json`, so `replay`, `debrief`,
`compare` and `stats` all work on drill runs — including partial
ones, whose debrief says plainly that the incident was never
mitigated or resolved instead of claiming nobody acked.

```sh
python3 -m incident_lab drill disk-full --input decisions.txt --json > drill.json
python3 -m incident_lab replay drill.json
python3 -m incident_lab debrief drill.json
python3 -m incident_lab compare drill.json run.json
```

## Drill history

`drill --operator NAME` records the finished drill — or a drill
ended early with `quit` or EOF, flagged as abandoned — in a history
file, so an operator can see whether their practice is paying off.
`history --operator NAME` lists that operator's runs in order with
their scores, grades and response times, plus a summary: runs, mean
and best score, first score vs latest score, and a trend — improving,
declining or steady, from the mean of the second half of their runs
against the mean of the first half (with fewer than two runs there is
nothing to compare yet). Plain `history` summarises every operator
the file knows about, one line each. With `--json` either view prints
as JSON instead.

The history file is `~/.incident-lab/history.json` by default; point
at a different file with `--history-file PATH` on both `drill` and
`history` (for example, one file per team or per training cohort).
A drill without `--operator` records nothing, and `--history-file`
without `--operator` fails with exit code 2 — history is only kept
for a named operator. A missing history file just means no drills
recorded yet; a corrupt one fails with exit code 2 and an error
message. Only drills are recorded: `run` plays a scenario's fixed
behaviour, not an operator's decisions, so there is nothing to learn
from attributing it.

```sh
printf 'ack 3\nmitigate 20\nresolve 12\n' | python3 -m incident_lab drill disk-full --operator ana
python3 -m incident_lab history --operator ana
python3 -m incident_lab history
python3 -m incident_lab history --operator ana --json
```

Example: ana drills `disk-full` three times, scoring 50, then 70,
then 90 — `history --operator ana` shows all three runs and
summarises them as mean 70/100, best 90/100, first → latest 50 → 90
(+40 pts), trend improving.

## Difficulty ladder

`run <scenario> --difficulty <level>` plays the same scenario harder or
easier. Levels are `easy`, `normal` (the default) and `hard`, or the
numbers `1`, `2`, `3`; an unknown level fails the run with exit code 2
and an error message. A difficulty scales three things at once:

| | easy | normal | hard |
| - | ---- | ------ | ---- |
| ack timeouts | ×1.5 (5m → 8m) | ×1.0 | ×0.6 (5m → 3m) |
| ack grace | 8m | 5m | 3m |
| mitigate grace | 20m | 15m | 10m |
| default seeded jitter | 0-0 | 0-1 | 0-3 |

Tighter timeouts mean faster escalation on hard (the secondary is paged
sooner), and the stricter graces mean the same incident scores lower:
`disk-full` scores 99 on easy, 96 on normal and 94 on hard. The
difficulty is printed in the human-readable output, recorded in the
`run --json` output (top-level `difficulty`), and shown again by
`replay` and `debrief` — a debrief also judges mitigation against the
difficulty's own target, so a hard run is not marked against the
normal 15m target. An explicit `--ack-timeout` or `--ack-timeouts`
still wins for the timeouts it sets, but the difficulty's scoring
targets keep applying. With `--seeds` the same difficulty applies to
every run in the batch.

## Layout

```
incident_lab/
  policy.py      escalation chain: responders, ack timeouts; on-call
                 rota files and per-week responder resolution
  incident.py    incident model
  timeline.py    append-only, time-ordered event log
  scenarios.py   built-in incident scenarios + scenario files/templates
  simulator.py   deterministic simulation + scoring
  difficulty.py  difficulty ladder: easy / normal / hard scaling
  replay.py      rebuild a saved --json run for re-printing; also loads
                 a saved what-if file as its replayed run, with the
                 base run and overrides kept as provenance
  debrief.py     structured postmortem from a saved run
  export_html.py self-contained HTML postmortem from a saved run:
                 score and grade, timeline, per-responder timings
                 from the escalation chain, hints used, findings and
                 action items; what-if files render with provenance;
                 shift files render the shift postmortem
  export_pdf.py  same postmortem as a multi-page PDF (stdlib-only,
                 hand-rolled writer: Letter pages, Helvetica, no
                 embedded fonts, text wrapped and flowed across
                 pages); what-if files render with provenance; shift
                 files render the shift postmortem
  bundle.py      shareable postmortem bundle: one zip holding the run
                 JSON, the HTML postmortem and a manifest with
                 provenance and SHA-256 hashes, validated on creation
                 and on `--check`; shift files bundle the same way,
                 with the shift's score and burn in the manifest
  compare.py     side-by-side diff of two saved runs: score deltas,
                 shared-event time shifts, events only one run has
  stats.py       aggregate saved runs: score spread, mean response
                 times, grade counts, per-scenario means
  whatif.py      what-if replay: re-simulate a saved run with
                 overridden ack timeouts, difficulty or policy, show
                 how the score and grade would have changed
  drill.py       interactive drill: the operator decides when to ack,
                 mitigate and resolve, one decision per line; same
                 timeouts, late-ack credit and scoring as run, with
                 scripted input (--input / piped stdin) and a clean
                 quit/EOF path that records a partial run
  history.py     per-operator drill history: record a drill run under
                 an operator name (`drill --operator NAME`), read it
                 back with `history` — one operator's runs and
                 improvement over time, or an overview of all
                 operators — as human text or --json
  cli.py         list / template / policy-template / run / drill /
                 history / replay / debrief / postmortem / bundle /
                 compare / stats / what-if
tests/
  test_incident_lab.py
scripts/
  demo.py        run every scenario, one-line summary each
examples/
  custom-policy.json   sample file for --policy-file
  custom-scenario.json sample file for --scenario-file
```

## Tests

```sh
make test
```

## Seeded runs

`run <scenario> --seed N` jitters each ack delay by up to 1 minute either
way, drawn from a random generator seeded with `N`, so the same seed
replays the same run exactly (same timeline, same score, byte-identical
`--json` output) while different seeds vary the response times. A miss
stays a miss, so the seed never changes who acks or how many escalations
happen — only how quickly the acking responder answers. The same seeded
jitter also applies to the mitigate and resolve delays once someone
acks, drawn from the same generator after the ack draws, so the whole
incident varies with the seed while staying reproducible; the effective
delays are recorded in the `run --json` output as
`score.mitigate_delay_min` and `score.resolve_delay_min` (or `null` for
an incident nobody acks). The seed is recorded in the `run --json`
output (top-level `seed`) and shown in the human-readable output, and
`replay` re-prints it from a saved run.

`run <scenario> --seed N --jitter LO-HI` widens or narrows that jitter:
each ack, mitigate and resolve delay shifts by a magnitude drawn from
`LO` to `HI` minutes, with a random sign, from the same seeded
generator, so reproducibility holds for any range. `--jitter 0-0` means
no jitter at all. The default is `0-1`, the same as `--seed` alone.
`--jitter` requires `--seed` or `--seeds`, and a bad range (negative bounds, or `LO`
greater than `HI`) fails the run with exit code 2 and an error
message. The range is recorded in the `run --json` output (top-level
`jitter`, `[lo, hi]`) and shown in the human-readable output, and
`replay` re-prints it from a saved run.

## Multi-seed batch mode

`run <scenario> --seeds 1,2,3` (or `--seeds 1-5` for an inclusive
range) runs the same scenario once per seed and reports the score
distribution instead of a single timeline: the mean, min, max and
median points across runs, plus the score for each seed. `--seeds`
cannot be combined with `--seed`; `--jitter` works with `--seeds`
and applies the same range to every run. A bad spec (empty entries,
non-integer or negative seeds, a reversed range) fails the run with
exit code 2 and an error message. With `--json` the batch prints
`seeds`, the shared incident and policy, every run in full under
`runs`, and `score_distribution` (mean/min/max/median and a
`per_seed` list); without it the same distribution is printed in
human-readable form.

## Version

0.34.0 — New scenario `sso-outage`: the identity provider fails
every login, but the pages go to a stale on-call rotation, so every
level misses and the incident ends unacked with a score of 0 — the
first built-in scenario where nobody acks, so the library now
exercises the unacked path (whole chain missed, impact growing from
logins failing to sessions expiring, debrief naming every missed
responder and adding a fallback-page action) with a real scenario
instead of only through scenario files.

0.33.0 — Postmortem and bundle support for shifts: `postmortem
<saved-shift.json>` renders the shift postmortem — the shift
summary, one row per incident that ran and one per skipped incident,
the shift-level findings and action items, then one section per
incident that ran with that incident's own debrief findings — as
HTML or PDF, and `bundle <saved-shift.json>` packages the shift JSON
with that HTML postmortem and a manifest carrying the shift's score,
total burn and the incidents that ran and were skipped, validated the
same way a run bundle is (the shift re-loads, the hashes match, and
the postmortem covers every incident in the shift).

0.32.0 — Multi-incident shift with a shared error budget: `shift
<scenario>...` runs several incidents back-to-back against one shared
budget from a single SLO (`--slo`, or the first scenario's SLO by
default) — burn accumulates across incidents, and when the cumulative
burn reaches 100% the shift ends early, the remaining incidents are
skipped and score 0, so the shift score (mean points over every
scheduled incident) scores down for a spent budget. The shift JSON
records the shared SLO, budget minutes, total burn, shift points and
per-incident burn/cumulative burn, and `replay`, `debrief`, `compare`
and `stats` all accept saved shift files.

0.31.0 — On-call rota: `run` / `drill` take `--rota-file PATH` (a
JSON file mapping each role to a weekly rotation list of responder
names, see `examples/rota.json`) and `--rota-week N`, and page
whoever is on call for that week — the name at position `N %
len(names)` in each role's list, wrapping around, so the pager
moves to the next person each week. With no `--rota-week` the run
uses the current ISO calendar week. A role the rota does not cover
keeps the policy's responder; only names change, never roles, order
or ack timeouts, so a rota run scores like the same run without one.
The week is printed in the human-readable output and recorded in
`run --json` output (top-level `rota_week`, `null` without a rota
file), `replay` re-prints it, and a `what-if` replay keeps it when
it keeps the original policy and drops it when the policy is swapped.

0.30.0 — PDF postmortem export: `postmortem <saved-run.json>
--format pdf --out file.pdf` renders the same postmortem the HTML
export carries (score and grade, timeline, per-responder timings
from the escalation chain, hints used with what each one cost,
findings and action items with owners, plus the what-if provenance
box for a saved `what-if --json` file) as a readable multi-page PDF.
The writer is hand-rolled and standard library only — Letter pages,
Helvetica and Helvetica-Bold, no embedded fonts, no new dependency
— with text wrapped to the page width and flowed across pages, each
page footered with the generator and the page number; `--out` is
required, since a PDF cannot be printed as text. Text outside
Latin-1 is folded to ASCII lookalikes and parentheses and
backslashes are escaped, so scenario text can never break the file.

0.29.0 — Shareable postmortem bundle: `bundle <saved-run.json>`
packages a saved run into one zip — `run.json` (the saved run,
verbatim), `postmortem.html` (the HTML postmortem) and `manifest.json`
(incident-lab version, incident and score, seed and difficulty, and
the what-if overrides and points delta when the source is a what-if
replay, plus a SHA-256 hash and size for every other member). A
what-if file bundles its replay as `run.json` and its base run as
`base-run.json`, so the comparison stays replayable. The bundle is
validated as it is created (the written zip is re-opened, every hash
is re-checked, `run.json` is loaded back through `replay` to prove it
re-imports) and a failing bundle is deleted, never left behind;
`bundle --check <bundle.zip>` runs the same validation on a bundle
someone shared. After unzipping, the run is a plain saved run, so
`replay`, `debrief` and `postmortem` all work on it.

0.28.0 — HTML postmortem export: `postmortem <saved-run.json>` renders
the debrief as a self-contained HTML page (inline CSS, no JavaScript,
no external resources) with the score and grade, the timeline, a
per-responder timings table from the escalation chain, the hints used
with what each one cost, findings and action items with owners; a saved
`what-if --json` file renders its replay with a provenance box (the
overrides, the points delta and the base run's score), `--out PATH`
writes the page to a file instead of printing it, and every string from
the run is escaped.

0.27.0 — error-budget burn: every scenario carries an SLO target
(`slo_percent`, for example 99.9% for `payments-db`), and the run
reports the downtime (declaration to mitigated) as a percentage of
the monthly error budget that SLO allows in a 30-day month — over
100% means one incident spent the whole month's budget (`disk-full`
burns 53.2%, `payments-outage` on the `sev1` policy burns 143.5%).
The run output shows the downtime and the burn, `run --json` carries
`slo_percent`, `downtime_min` and `error_budget_burn_pct` on the
score, `debrief` flags a burn over 100% with an action to review the
SLO and the response, `compare` diffs the burn between two runs,
`stats` reports the mean burn, `what-if` recomputes it from the
replayed delays, and drills record it too. Scenario files can set
their own `slo_percent` (90 up to but not including 100); `template`
prints it, and a scenario without one reports no burn.

0.26.0 — SEV-1 scenario with a multi-level escalation chain:
`payments-outage` (payments API fully down, on-call and senior miss,
incident commander acks in 4m) with a new `sev1` policy (on-call 3m,
senior 5m, incident commander 10m), and the escalation chain recorded
in `run --json` (one step per level walked: responder, ack timeout,
acked or missed, impact in effect when paged) — scenarios can carry
`impact_stages`, so the impact grows with every missed level and the
escalation event says so (`impact now: ...`); `replay` re-prints the
chain, `debrief` adds a finding and an action when the impact grew,
`compare` diffs the two runs' chains, `stats` records the impact each
run ended at and counts the runs whose impact grew, `what-if` keeps
the original run's impact stages, and a drill records the chain the
same way `run` does.

0.25.0 — per-operator drill history: `drill --operator NAME` records
the drill (finished or abandoned) in a history file
(`~/.incident-lab/history.json` by default, `--history-file PATH` to
point elsewhere), and `history --operator NAME` shows that operator's
runs in order with their scores, grades and response times, plus a
summary — runs, mean and best score, first score vs latest, and a
trend (improving / declining / steady, from the second half of their
runs against the first); plain `history` summarises every operator,
one line each, and `--json` prints either view as JSON. A drill
without `--operator` records nothing; only drills are recorded, never
`run`, because `run` plays fixed behaviour, not an operator's
decisions.

0.24.0 — interactive `hint` during a drill: type `hint` at any drill
prompt to ask for the scenario's next known-good action when stuck,
drawn in order and continuing after any hints asked for up front
with `drill --hints N`, each costing 5 points off the final score
exactly like `run --hints`; the hint is printed on stderr with the
milestone that is next and what the scoring rewards at this point,
and recorded in the drill's JSON like an up-front hint, so `replay`,
`debrief`, `compare` and `stats` all see it — a drill that asks for
a hint scores the same as a `run` that asked for the same hint.
When the scenario's hints run out, `hint` says so and costs nothing;
a scenario with no hints offers no `hint` command.

0.23.0 — interactive drill mode: `incident-lab drill <scenario>`
plays an incident step by step with the operator deciding when to
ack, mitigate and resolve (`ack N` / `wait`, `mitigate N`,
`resolve N`, `quit`), with the same timeouts, late-ack credit and
scoring as `run` — a drill making the same decisions as a `run`
scores the same; scripted input via `--input PATH` or piped stdin,
prompts on stderr so `drill --json` stays parseable, `--out PATH`
writes the result as JSON, and quit/EOF ends the drill cleanly with
a partial run (timeline plus an `abandoned` event, scored on the
milestones reached) in the same JSON shape as `run`, so `replay`,
`debrief`, `compare` and `stats` all work on drill runs, and a
debrief of a partial drill says the incident was never mitigated or
resolved instead of claiming nobody acked.

## Roadmap

- [x] Escalation policies and timeline replay (0.1.0)
- [x] `run --json` prints the full result as JSON (0.2.0)
- [x] Named policies: `--policy fast` with tighter ack timeouts (0.3.0)
- [x] Custom policies from a JSON file (`--policy-file`) (0.4.0)
- [x] More scenarios (queue backlog, DNS outage) (0.5.0)
- [x] `replay` command: re-print a timeline from a saved `--json` run (0.6.0)
- [x] Per-level ack timeouts on the command line (0.7.0)
- [x] Late-ack partial credit: linear decay to zero at 2x the timeout (0.8.0)
- [x] `--seed N` for randomized ack delays: reproducible runs (0.9.0)
- [x] `--jitter LO-HI` for a configurable seeded ack-delay jitter range (0.10.0)
- [x] Seeded jitter for mitigate and resolve delays, recorded in JSON, replay round-trips (0.11.0)
- [x] Multi-seed batch mode: `--seeds 1,2,3` or `--seeds 1-5` runs the same scenario per seed and reports the score distribution (mean/min/max/median, per-seed scores) in human output and `--json` (0.12.0)
- [x] Debrief/postmortem report generator: `debrief <saved-run.json>` writes a structured postmortem (summary with score and grade, timeline, what went well / badly, action items with owners) as Markdown or `--json`, every finding derived from the run (0.13.0)
- [x] Shareable incident templates: `template <scenario>` prints a scenario as JSON, `run --scenario-file` loads named scenarios from a JSON file (file wins over built-in names), validated like `--policy-file` (0.14.0)
- [x] Difficulty ladder: `run --difficulty easy|normal|hard` (or 1/2/3) scales ack timeouts (×1.5 / ×1.0 / ×0.6), scoring targets (ack grace 8/5/3m, mitigate grace 20/15/10m) and the default seeded jitter (0-0 / 0-1 / 0-3), records the difficulty in `run --json`, and `replay`/`debrief` show it (0.15.0)
- [x] Run comparison: `compare <run-a.json> <run-b.json>` diffs two saved runs side by side (score deltas per metric, timeline diff of shared-event time shifts plus events only one run has), human form or `--json`, flags different scenarios/policies, unreached milestones show as `-` (0.16.0)
- [x] Hints: `run --hints N` asks for N hints during a run, drawn in order from the scenario's known-good next actions (each built-in scenario carries 2-3, scenario files can add their own, `template` prints them); each hint costs 5 points off the final score and is recorded in `run --json` and shown again by `replay` (0.17.0)
- [x] Stats: `stats <run.json>...` aggregates saved runs (score spread, mean response times, acked/resolved counts, escalations, hints used, grade counts, per-scenario means), human form or `--json`, flags mixed scenarios (0.18.0)
- [x] What-if replay: `what-if <saved-run.json> --ack-timeout N | --ack-timeouts ... | --difficulty X` re-simulates a saved run with overridden parameters and shows how the score and grade would have changed (human form or `--json`); the incident's behaviour is rebuilt from the saved run's own recorded delays, a seeded run's jitter is not drawn again, hints are kept, and the original run file is never modified (0.19.0)
- [x] What-if replay against a different named policy: `what-if <saved-run.json> --policy <name>` (built-in, or from `--policy-file`) re-simulates the saved run under that policy and shows how the score and grade would have changed; the new policy's timeouts are scaled to the original run's difficulty unless `--difficulty` is also given, an unknown policy name fails with exit code 2, and the original run file is never modified (0.20.0)
- [x] Compare and stats accept what-if replay outputs: `compare` and `stats` take saved `what-if --json` files alongside normal runs, compare what-if replays against each other and against their base runs, and aggregate what-if runs in stats, flagging them as what-if with their overrides and points delta recorded; a what-if file whose points delta does not match its runs' scores is rejected (0.21.0)
- [x] Policy template command: `policy-template [name]` prints a built-in policy as a starter JSON policy file (or writes it with `--out PATH`), ready to edit and pass back with `--policy-file`; the template is validated through the policy loader so it always loads cleanly (0.22.0)
- [x] Interactive drill mode: `drill <scenario>` plays an incident step by step with the operator deciding when to ack, mitigate and resolve (`ack N` / `wait`, `mitigate N`, `resolve N`, `quit`), same timeouts, late-ack credit and scoring as `run`; scripted input via `--input PATH` or piped stdin, prompts on stderr so `drill --json` stays parseable, `--out PATH` writes the JSON result, and quit/EOF ends the drill cleanly with a partial run (timeline plus an `abandoned` event, scored on the milestones reached) in the same JSON shape as `run`, so `replay`, `debrief`, `compare` and `stats` all work on drill runs (0.23.0)
- [x] Interactive `hint` during a drill: type `hint` at any drill prompt to ask for the scenario's next known-good action when stuck (costs 5 points, like `run --hints`), drawn in order and continuing after any up-front `--hints`, printed with the next milestone and what the scoring rewards at that point, and recorded in the drill's JSON exactly like an up-front hint, so `replay`, `debrief`, `compare` and `stats` all work on drill runs with mid-drill hints (0.24.0)
- [x] Per-operator drill history: `drill --operator NAME` records the drill (finished or abandoned) in a history file (`~/.incident-lab/history.json` by default, `--history-file PATH` to point elsewhere), and `history --operator NAME` shows that operator's runs in order with scores, grades and response times plus a summary (runs, mean and best score, first vs latest, trend from the second half of runs against the first); plain `history` summarises every operator, `--json` prints either view as JSON, and a drill without `--operator` records nothing (0.25.0)
- [x] SEV-1 scenario with a multi-level escalation chain: `payments-outage` with a `sev1` policy (on-call 3m, senior 5m, incident commander 10m), impact stages that grow with every missed level, and the escalation chain recorded in `run --json` and shown by `replay`, `debrief`, `compare` and `stats`; `what-if` keeps the original run's impact stages and drills record the chain too (0.26.0)
- [x] Error-budget burn: every scenario carries an SLO target (`slo_percent`), and the run reports the downtime (declaration to mitigated) as a percentage of the monthly error budget that SLO allows in a 30-day month — over 100% means one incident spent the whole month's budget; shown in `run` output and `run --json`, flagged by `debrief` when over 100%, diffed by `compare`, averaged by `stats`, recomputed by `what-if`, recorded by drills, and settable per scenario in scenario files (0.27.0)
- [x] HTML postmortem export: `postmortem <saved-run.json>` renders the debrief as a self-contained HTML page (inline CSS, no JavaScript, no external resources) with the score and grade, the timeline, a per-responder timings table from the escalation chain, the hints used with what each one cost, findings and action items with owners; a saved `what-if --json` file renders its replay with a provenance box (the overrides, the points delta and the base run's score), `--out PATH` writes the page to a file, and every string from the run is escaped (0.28.0)
- [x] Shareable postmortem bundle: `bundle <saved-run.json>` packages the run JSON (verbatim), the HTML postmortem and a manifest with provenance (incident-lab version, incident and score, seed and difficulty, what-if overrides and points delta when the source is a what-if replay) and a SHA-256 hash of every other member into one zip, validated as it is created (hashes re-checked, `run.json` loaded back through `replay` to prove it re-imports, a failing bundle deleted); a what-if file bundles its replay as `run.json` and its base run as `base-run.json`; `bundle --check <bundle.zip>` validates a shared bundle, and the unzipped run works with `replay`, `debrief` and `postmortem` (0.29.0)
- [x] PDF postmortem export: `postmortem <saved-run.json> --format pdf --out file.pdf` renders the same postmortem as the HTML export (score and grade, timeline, per-responder timings, hints with cost, findings and action items, what-if provenance) as a readable multi-page PDF, hand-rolled and standard library only (Letter pages, Helvetica, no embedded fonts, no new dependency), text wrapped and flowed across pages with a footer on each; `--out` is required, non-Latin-1 text folds to ASCII lookalikes, and parentheses and backslashes are escaped (0.30.0)
- [x] On-call rota: `run` / `drill` take `--rota-file PATH` (a JSON file mapping each role to a weekly rotation list of responder names) and `--rota-week N`, and page whoever is on call for that week — the name at position `N % len(names)` per role, wrapping around; with no `--rota-week` the run uses the current ISO calendar week; a role the rota does not cover keeps the policy's responder, only names change, never roles, order or timeouts; the week is printed in the human-readable output and recorded in `run --json` (top-level `rota_week`), `replay` re-prints it, and a `what-if` replay keeps it when it keeps the original policy and drops it when the policy is swapped (0.31.0)
- [x] Multi-incident shift with a shared error budget: `shift <scenario>...` runs several incidents back-to-back against one shared budget from a single SLO (`--slo`, or the first scenario's SLO by default) — burn accumulates across incidents in the shift, and when the cumulative burn reaches 100% the shift ends early, the remaining incidents are skipped and score 0, so the shift score (mean points over every scheduled incident) scores down for a spent budget; the shift JSON records the shared SLO, budget minutes, total burn, shift points and per-incident burn/cumulative burn, each incident's run is an ordinary single-run result, and `replay`, `debrief`, `compare` and `stats` all accept saved shift files (debrief writes a shift postmortem, compare diffs two shifts at shift level, stats counts each incident in a shift as a run) (0.32.0)
- [x] Unacked-incident scenario: `sso-outage` — the identity provider fails every login while the pages go to a stale on-call rotation, so every level misses, the impact grows from logins failing to sessions expiring, and the incident ends unacked with a score of 0; the first built-in scenario where nobody acks, so the unacked path (whole chain missed, debrief naming every missed responder plus a fallback-page action) is exercised by a real scenario, with hints and an SLO like the rest of the library (0.34.0)
