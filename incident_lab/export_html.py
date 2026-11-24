"""HTML postmortem export: render a saved run as one self-contained page.

`postmortem <run.json> [--out file.html]` renders the debrief for a saved
`run --json` run — or a saved `what-if --json` file, whose replay is
rendered with its provenance — as a single HTML file: inline CSS, no
JavaScript, no external resources, so the page opens anywhere and can be
attached to a ticket or pasted into a wiki. Every string that comes from
the run (titles, hints, timeline details, impact stages) is escaped, so
scenario text can never break out of the page.

The page carries the same facts as the Markdown debrief — score and
grade, the timeline, what went well and badly, action items with owners —
plus two things the Markdown form does not show: a per-responder timings
table built from the escalation chain (when each level was paged, when
they acked or missed, and how late they were), and the hints the run
used, with what each one cost.
"""

import html

from . import __version__
from .debrief import generate_debrief, generate_shift_debrief


def _esc(text) -> str:
    """Escape run text for HTML; None renders as an em dash."""
    if text is None:
        return "&mdash;"
    return html.escape(str(text), quote=True)


def _mins(value) -> str:
    """Minutes as \"3m\", or an em dash when the milestone was unreached."""
    if value is None:
        return "&mdash;"
    return f"{value}m"


def _score_rows(debrief) -> str:
    """Rows of the summary table: score, grade, times, error budget."""
    score = debrief
    rows = [
        ("Score", f"{score.points}/100 (grade {score.grade})"),
        ("Time-to-ack", _mins(score.time_to_ack_min)
         if score.time_to_ack_min is not None else "nobody acked"),
        ("Time-to-mitigate", _mins(score.time_to_mitigate_min)
         if score.time_to_mitigate_min is not None else "never mitigated"),
        ("Time-to-resolve", _mins(score.time_to_resolve_min)
         if score.time_to_resolve_min is not None else "never resolved"),
        ("Escalations", str(score.escalations)),
        ("Resolved", "yes" if score.resolved else "no"),
    ]
    if score.difficulty is not None:
        rows.append(("Difficulty", score.difficulty.name))
    if score.error_budget_burn_pct is not None:
        rows.append((
            "Error budget",
            f"burned {score.error_budget_burn_pct:.1f}% of the monthly "
            f"budget (SLO {score.slo_percent}%, downtime "
            f"{score.downtime_min}m)"))
    return "\n".join(
        f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>"
        for label, value in rows)


def _timeline_rows(result) -> str:
    """One table row per timeline event, in time order."""
    rows = []
    for event in result.timeline.events:
        rows.append(
            "<tr>"
            f"<td>{event.t_min}m</td>"
            f"<td>{_esc(event.kind)}</td>"
            f"<td>{_esc(event.actor)}</td>"
            f"<td>{_esc(event.detail)}</td>"
            "</tr>")
    return "\n".join(rows)


def _responder_rows(result) -> str:
    """One table row per escalation-chain step: who was paged, when, and
    whether they acked or missed.

    The ack delay is the minutes from page to ack; a missed level shows
    its timeout as the time the page went unanswered for. A run saved
    before 0.26.0 has no chain and reports that plainly instead.
    """
    chain = result.escalation_chain or []
    if not chain:
        return ("<p>No escalation chain recorded for this run "
                "(runs saved before 0.26.0 have none).</p>")
    rows = []
    for step in chain:
        if step.outcome == "acked":
            acked = f"{step.acked_at_min}m"
            delay = f"{step.acked_at_min - step.paged_at_min}m"
            late = ("on time" if not step.ack_late_min
                    else f"{step.ack_late_min}m late")
        else:
            acked = "missed"
            delay = "&mdash;"
            late = f"no ack within {step.ack_timeout_min}m"
        rows.append(
            "<tr>"
            f"<td>{step.level}</td>"
            f"<td>{_esc(step.responder_name)} "
            f"({_esc(step.responder_role)})</td>"
            f"<td>{step.paged_at_min}m</td>"
            f"<td>{acked}</td>"
            f"<td>{delay}</td>"
            f"<td>{step.ack_timeout_min}m</td>"
            f"<td>{_esc(late)}</td>"
            f"<td>{_esc(step.impact) if step.impact else '&mdash;'}</td>"
            "</tr>")
    return (
        "<table>\n"
        "<thead><tr><th>Level</th><th>Responder</th><th>Paged at</th>"
        "<th>Acked at</th><th>Ack delay</th><th>Timeout</th>"
        "<th>Lateness</th><th>Impact when paged</th></tr></thead>\n"
        "<tbody>\n" + "\n".join(rows) + "\n</tbody>\n</table>")


def _hints_section(result) -> str:
    """The hints the run used, with what each one cost."""
    if not result.hints:
        return "<p>No hints used.</p>"
    items = "".join(
        f"<li>{_esc(hint.text)} "
        f"(&minus;{hint.cost_points} points)</li>"
        for hint in result.hints)
    total = result.score.hint_cost_points
    return (f"<ul>\n{items}\n</ul>\n"
            f"<p>Total hint cost: {total} points "
            f"({result.score.hints_used} hint(s)).</p>")


def _findings_list(items) -> str:
    """A bullet list of findings, or an explicit \"none\"."""
    if not items:
        return "<ul><li>none</li></ul>"
    return "<ul>\n" + "\n".join(
        f"<li>{_esc(item)}</li>" for item in items) + "\n</ul>"


def _actions_list(items) -> str:
    """Action items as checkboxes with owners."""
    if not items:
        return "<ul><li>none</li></ul>"
    return "<ul>\n" + "\n".join(
        f"<li><input type=\"checkbox\" disabled> {_esc(item['action'])} "
        f"(owner: {_esc(item['owner'])})</li>"
        for item in items) + "\n</ul>"


def _what_if_section(what_if) -> str:
    """Provenance box for a what-if replay: the overrides, the points
    delta against the base run, and the base run's score."""
    if what_if is None:
        return ""
    delta = what_if.points_delta
    sign = "+" if delta >= 0 else ""
    overrides = "".join(
        f"<li>{_esc(override)}</li>" for override in what_if.overrides)
    return (
        "<section class=\"whatif\">\n"
        "<h2>What-if replay</h2>\n"
        "<p>This run is a hypothetical replay, not an incident that "
        "happened: it re-simulates a saved run with the overrides "
        "below.</p>\n"
        f"<ul>\n{overrides}\n</ul>\n"
        f"<p>Score change: {sign}{delta} points "
        f"(base run scored {what_if.base_points}/100).</p>\n"
        "</section>\n")


_CSS = """
body { font-family: system-ui, sans-serif; max-width: 900px;
       margin: 2em auto; padding: 0 1em; color: #111; line-height: 1.5; }
h1 { font-size: 1.6em; margin-bottom: 0.2em; }
h2 { font-size: 1.2em; margin-top: 1.5em; border-bottom: 1px solid #ccc;
     padding-bottom: 0.2em; }
h3 { font-size: 1.05em; margin-top: 1.2em; }
table { border-collapse: collapse; width: 100%; margin: 0.5em 0; }
th, td { border: 1px solid #ccc; padding: 0.3em 0.6em; text-align: left;
         vertical-align: top; }
th { background: #f0f0f0; }
.summary th { width: 12em; }
.whatif { background: #fff8e1; border: 1px solid #e6c200;
          padding: 0.5em 1em; margin-top: 1em; }
.meta { color: #555; }
footer { color: #555; font-size: 0.9em; margin-top: 2em;
         border-top: 1px solid #ccc; padding-top: 0.5em; }
"""


def _shift_incident_rows(debrief) -> str:
    """One table row per incident that ran, plus a row for the
    skipped ones: scenario, score and grade, downtime, and the
    share of the shared budget it burned."""
    rows = []
    for entry in debrief.incidents:
        downtime = (f"{entry['downtime_min']}m"
                    if entry["downtime_min"] is not None else "&mdash;")
        rows.append(
            "<tr>"
            f"<td>{_esc(entry['scenario'])}</td>"
            f"<td>{entry['points']}/100 (grade {entry['grade']})</td>"
            f"<td>{downtime}</td>"
            f"<td>{entry['burn_pct']:.1f}%</td>"
            f"<td>{entry['cumulative_burn_pct']:.1f}%</td>"
            "</tr>")
    for name in debrief.skipped:
        rows.append(
            "<tr>"
            f"<td>{_esc(name)}</td>"
            "<td>skipped (0/100)</td>"
            "<td>&mdash;</td>"
            "<td>&mdash;</td>"
            "<td>&mdash;</td>"
            "</tr>")
    return "\n".join(rows)


def render_shift_postmortem_html(shift) -> str:
    """Render a saved shift as a self-contained HTML postmortem.

    ``shift`` is a ShiftResult (a saved `shift --json` file). The
    page carries the shift summary — score and grade, the shared
    SLO and budget, one row per incident that ran and one per
    skipped incident — the shift-level findings from
    ``debrief.generate_shift_debrief``, and then one section per
    incident that ran with that incident's own debrief findings,
    so the page reads as the shift's postmortem with each
    incident's write-up attached.
    """
    debrief = generate_shift_debrief(shift)
    parts = [
        "<!DOCTYPE html>",
        "<html lang=\"en\">",
        "<head>",
        "<meta charset=\"utf-8\">",
        "<title>Shift postmortem</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        "<h1>Shift postmortem</h1>",
        "<p class=\"meta\">"
        f"{len(shift.runs)} incident(s) ran, "
        f"{len(shift.skipped)} skipped</p>",
        "<h2>Summary</h2>",
        "<table class=\"summary\">\n<tbody>\n",
        "<tr><th>Shift score</th>"
        f"<td>{debrief.shift_points}/100 "
        f"(grade {debrief.grade})</td></tr>",
        "<tr><th>Error budget</th>"
        f"<td>burned {debrief.total_burn_pct:.1f}% of the monthly "
        f"budget (SLO {debrief.slo_percent}%, "
        f"{debrief.budget_minutes:.1f}m)</td></tr>",
    ]
    if debrief.difficulty is not None:
        parts.append(
            "<tr><th>Difficulty</th>"
            f"<td>{debrief.difficulty.name}</td></tr>")
    if shift.seed is not None:
        parts.append(
            f"<tr><th>Seed</th><td>{shift.seed}</td></tr>")
    parts += [
        "</tbody>\n</table>",
        "<h2>Incidents</h2>",
        "<table>\n<thead><tr><th>Scenario</th><th>Score</th>"
        "<th>Downtime</th><th>Burn</th>"
        "<th>Cumulative burn</th></tr></thead>\n<tbody>\n"
        + _shift_incident_rows(debrief) + "\n</tbody>\n</table>",
        "<h2>What went well</h2>",
        _findings_list(debrief.went_well),
        "<h2>What went badly</h2>",
        _findings_list(debrief.went_badly),
        "<h2>Action items</h2>",
        _actions_list(debrief.action_items),
    ]
    for run in shift.runs:
        incident_debrief = generate_debrief(run)
        parts += [
            f"<h2>{_esc(run.incident.title)}</h2>",
            (f"<p class=\"meta\">Service: "
             f"{_esc(run.incident.service)} "
             f"[{_esc(run.incident.severity)}] — "
             f"{incident_debrief.points}/100 "
             f"(grade {incident_debrief.grade})</p>"),
            "<h3>What went well</h3>",
            _findings_list(incident_debrief.went_well),
            "<h3>What went badly</h3>",
            _findings_list(incident_debrief.went_badly),
            "<h3>Action items</h3>",
            _actions_list(incident_debrief.action_items),
        ]
    parts += [
        (f"<footer>Generated by incident-lab {__version__} — "
         "a simulated shift, not real pager events.</footer>"),
        "</body>",
        "</html>",
    ]
    return "\n".join(parts) + "\n"


def render_postmortem_html(result, what_if=None) -> str:
    """Render one saved run as a self-contained HTML postmortem.

    ``result`` is a SimulationResult (a normal run, or the replayed run
    of a what-if file); ``what_if`` is the WhatIfInfo provenance for a
    what-if file, or None. The findings come from
    ``debrief.generate_debrief``, so the page never invents a cause the
    simulation did not show.
    """
    debrief = generate_debrief(result)
    incident = result.incident
    title = f"Postmortem: {incident.title}"
    parts = [
        "<!DOCTYPE html>",
        "<html lang=\"en\">",
        "<head>",
        "<meta charset=\"utf-8\">",
        f"<title>{_esc(title)}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        f"<h1>{_esc(title)}</h1>",
        (f"<p class=\"meta\">Service: {_esc(incident.service)} "
         f"[{_esc(incident.severity)}]</p>"),
        f"<p>{_esc(incident.description)}</p>",
        _what_if_section(what_if),
        "<h2>Summary</h2>",
        "<table class=\"summary\">\n<tbody>\n"
        + _score_rows(debrief) + "\n</tbody>\n</table>",
        "<h2>Timeline</h2>",
        "<table>\n<thead><tr><th>Time</th><th>Event</th><th>Actor</th>"
        "<th>Detail</th></tr></thead>\n<tbody>\n"
        + _timeline_rows(result) + "\n</tbody>\n</table>",
        "<h2>Responder timings</h2>",
        _responder_rows(result),
        "<h2>Hints used</h2>",
        _hints_section(result),
        "<h2>What went well</h2>",
        _findings_list(debrief.went_well),
        "<h2>What went badly</h2>",
        _findings_list(debrief.went_badly),
        "<h2>Action items</h2>",
        _actions_list(debrief.action_items),
        (f"<footer>Generated by incident-lab {__version__} — "
         "a simulated incident, not a real pager event.</footer>"),
        "</body>",
        "</html>",
    ]
    return "\n".join(parts) + "\n"
