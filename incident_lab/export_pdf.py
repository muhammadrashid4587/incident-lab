"""PDF postmortem export: render a saved run as a readable multi-page PDF.

`postmortem <run.json> --format pdf --out file.pdf` renders the same
debrief the HTML export carries — score and grade, the timeline, a
per-responder timings table from the escalation chain, the hints used
with what each one cost, findings and action items with owners, plus
the what-if provenance box for a saved `what-if --json` file — as a
PDF, so it can be attached to a ticket or filed where HTML is not
wanted.

Pure Python 3, standard library only: the PDF writer is hand-rolled
(no new dependency), writing Letter pages with the PDF base-14 fonts
Helvetica and Helvetica-Bold, so no font embedding is needed, and
uncompressed content streams, so the text stays greppable in the
file. Text is wrapped to the page width and flows across pages; every
page carries a footer naming the generator and the page number.
"""

from . import __version__
from .debrief import generate_debrief, generate_shift_debrief

# Letter, in points, with a 3/4-inch margin all round.
PAGE_WIDTH = 612
PAGE_HEIGHT = 792
MARGIN = 54
FOOTER_Y = 36

_FONT_REGULAR = "F1"
_FONT_BOLD = "F2"

# Characters outside Latin-1, and the punctuation PDF readers render
# inconsistently, are replaced before encoding: the text stays
# readable ("--" reads as a dash) instead of raising or vanishing.
_REPLACEMENTS = {
    "\u2014": "-",  # em dash
    "\u2013": "-",  # en dash
    "\u201c": '"',  # left double quote
    "\u201d": '"',  # right double quote
    "\u2018": "'",  # left single quote
    "\u2019": "'",  # right single quote
    "\u00d7": "x",  # multiplication sign
    "\u2212": "-",  # minus sign
    "\u2026": "...",  # ellipsis
    "\u2022": "-",  # bullet
    "\u00a0": " ",  # non-breaking space
}


def _pdf_text(text) -> str:
    """Make run text safe for a PDF literal string: fold non-Latin-1
    characters to ASCII lookalikes, then drop anything still outside
    Latin-1 (``replace``) rather than raising on a scenario's text."""
    folded = "".join(_REPLACEMENTS.get(char, char) for char in str(text))
    return folded.encode("latin-1", "replace").decode("latin-1")


def _escape_literal(text: str) -> str:
    """Escape a string for a PDF literal string: backslash and
    parentheses are the only characters with a special meaning there.
    """
    return (text.replace("\\", "\\\\")
                .replace("(", "\\(")
                .replace(")", "\\)"))


def _chars_per_line(size: int) -> int:
    """How many average-width characters fit on one line at this size.

    Helvetica averages about 0.55 em per character, so this is a
    conservative estimate: text wraps a little early rather than
    running past the margin.
    """
    usable = PAGE_WIDTH - 2 * MARGIN
    return max(20, int(usable / (size * 0.55)))


def _wrap(text: str, size: int) -> list:
    """Word-wrap one logical line to the page width at this font size.

    Over-long words are split rather than overflowing the page; an
    empty line stays empty.
    """
    width = _chars_per_line(size)
    if not text:
        return [""]
    lines = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        current = ""
        for word in words:
            while len(word) > width:
                if current:
                    lines.append(current)
                    current = ""
                lines.append(word[:width])
                word = word[width:]
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current += " " + word
            else:
                lines.append(current)
                current = word
        if current or not lines:
            lines.append(current)
    return lines


# A content line is (text, font, size, gap-before in points).
def _content_lines(result, what_if) -> list:
    """The postmortem as a flat list of styled lines, in reading
    order, mirroring the HTML export section for section."""
    debrief = generate_debrief(result)
    incident = result.incident
    lines = []

    def add(text, font=_FONT_REGULAR, size=10, gap=0):
        for wrapped in _wrap(text, size):
            lines.append((wrapped, font, size, gap))
            gap = 0

    def heading(text):
        add(text, font=_FONT_BOLD, size=12, gap=14)

    def bullet(text):
        add(f"- {text}")

    add(f"Postmortem: {incident.title}", font=_FONT_BOLD, size=16)
    add(f"Service: {incident.service} [{incident.severity}]", gap=4)
    add(incident.description, gap=4)

    if what_if is not None:
        heading("What-if replay")
        add("This run is a hypothetical replay, not an incident that "
            "happened: it re-simulates a saved run with the overrides "
            "below.")
        for override in what_if.overrides:
            bullet(override)
        delta = what_if.points_delta
        sign = "+" if delta >= 0 else ""
        add(f"Score change: {sign}{delta} points (base run scored "
            f"{what_if.base_points}/100).")

    heading("Summary")
    score = debrief
    add(f"Score: {score.points}/100 (grade {score.grade})")
    add(f"Time-to-ack: {score.time_to_ack_min}m"
        if score.time_to_ack_min is not None else "Time-to-ack: "
        "nobody acked")
    add(f"Time-to-mitigate: {score.time_to_mitigate_min}m"
        if score.time_to_mitigate_min is not None
        else "Time-to-mitigate: never mitigated")
    add(f"Time-to-resolve: {score.time_to_resolve_min}m"
        if score.time_to_resolve_min is not None
        else "Time-to-resolve: never resolved")
    add(f"Escalations: {score.escalations}")
    add(f"Resolved: {'yes' if score.resolved else 'no'}")
    if score.difficulty is not None:
        add(f"Difficulty: {score.difficulty.name}")
    if score.verify_delay_min:
        add(f"Purge verification: per-region "
            f"({len(score.regions)} regions, "
            f"+{score.verify_delay_min}m)")
    if score.error_budget_burn_pct is not None:
        add(f"Error budget: burned {score.error_budget_burn_pct:.1f}% "
            f"of the monthly budget (SLO {score.slo_percent}%, "
            f"downtime {score.downtime_min}m)")

    heading("Timeline")
    for event in result.timeline.events:
        add(f"{event.t_min}m  {event.kind}  {event.actor}  "
            f"{event.detail}")

    heading("Responder timings")
    chain = result.escalation_chain or []
    if not chain:
        add("No escalation chain recorded for this run (runs saved "
            "before 0.26.0 have none).")
    for step in chain:
        if step.outcome == "acked":
            delay = step.acked_at_min - step.paged_at_min
            late = ("on time" if not step.ack_late_min
                    else f"{step.ack_late_min}m late")
            add(f"Level {step.level}: {step.responder_name} "
                f"({step.responder_role}) paged {step.paged_at_min}m, "
                f"acked {step.acked_at_min}m ({delay}m delay, {late}), "
                f"timeout {step.ack_timeout_min}m, impact: "
                f"{step.impact or '-'}")
        else:
            add(f"Level {step.level}: {step.responder_name} "
                f"({step.responder_role}) paged {step.paged_at_min}m, "
                f"missed after {step.ack_timeout_min}m, impact: "
                f"{step.impact or '-'}")

    heading("Hints used")
    if not result.hints:
        add("No hints used.")
    else:
        for hint in result.hints:
            bullet(f"{hint.text} (-{hint.cost_points} points)")
        add(f"Total hint cost: {result.score.hint_cost_points} points "
            f"({result.score.hints_used} hint(s)).")

    heading("What went well")
    if debrief.went_well:
        for item in debrief.went_well:
            bullet(item)
    else:
        add("none")

    heading("What went badly")
    if debrief.went_badly:
        for item in debrief.went_badly:
            bullet(item)
    else:
        add("none")

    heading("Action items")
    if debrief.action_items:
        for item in debrief.action_items:
            bullet(f"[ ] {item['action']} (owner: {item['owner']})")
    else:
        add("none")
    return lines


def _flow_pages(lines) -> list:
    """Flow the styled lines onto pages: each page is a list of
    (text, font, size, y) with y the baseline in points from the
    bottom of the page. A line that would run into the footer starts
    a new page instead."""
    pages = []
    current = []
    y = PAGE_HEIGHT - MARGIN
    for text, font, size, gap in lines:
        height = size * 1.4
        if gap:
            y -= gap
        if y - height < MARGIN + 14:
            pages.append(current)
            current = []
            y = PAGE_HEIGHT - MARGIN
        current.append((text, font, size, y - size))
        y -= height
    if current:
        pages.append(current)
    return pages or [[]]


def _page_stream(page_lines, page_number, page_count) -> bytes:
    """One page's content stream: the text lines plus the footer."""
    parts = []
    for text, font, size, y in page_lines:
        safe = _escape_literal(_pdf_text(text))
        parts.append(f"BT /{font} {size} Tf "
                     f"1 0 0 1 {MARGIN} {y:.1f} Tm ({safe}) Tj ET")
    footer = (f"Generated by incident-lab {__version__} - a simulated "
              f"incident, not a real pager event.")
    parts.append(f"BT /{_FONT_REGULAR} 8 Tf "
                 f"1 0 0 1 {MARGIN} {FOOTER_Y} Tm "
                 f"({_escape_literal(_pdf_text(footer))}) Tj ET")
    page_label = f"Page {page_number} of {page_count}"
    label_x = (PAGE_WIDTH - MARGIN
               - len(page_label) * 8 * 0.55)
    parts.append(f"BT /{_FONT_REGULAR} 8 Tf "
                 f"1 0 0 1 {label_x:.1f} {FOOTER_Y} Tm "
                 f"({page_label}) Tj ET")
    return ("\n".join(parts) + "\n").encode("latin-1")


def _build_pdf(pages) -> bytes:
    """Assemble the PDF: catalog, pages, two base-14 fonts, then one
    page object and one content stream per page, with a cross-reference
    table and trailer."""
    page_count = len(pages)
    # Object 1: catalog, 2: pages, 3: Helvetica, 4: Helvetica-Bold.
    # Each page then takes two objects: the page, then its stream.
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{5 + 2 * i} 0 R".encode()
                    for i in range(page_count))
        + b"] /Count " + str(page_count).encode() + b" >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ]
    for index, page_lines in enumerate(pages):
        page_obj = 5 + 2 * index
        stream = _page_stream(page_lines, index + 1, page_count)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /{_FONT_REGULAR} 3 0 R "
            f"/{_FONT_BOLD} 4 0 R >> >> "
            f"/Contents {page_obj + 1} 0 R >>".encode())
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\n"
            b"stream\n" + stream + b"endstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode()
        out += body + b"\nendobj\n"
    xref_at = len(out)
    total = len(objects) + 1
    out += f"xref\n0 {total}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {total} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()
    return bytes(out)


def _shift_content_lines(shift) -> list:
    """The shift postmortem as a flat list of styled lines, in
    reading order, mirroring the HTML shift export section for
    section: the shift summary, one row per incident, the
    shift-level findings and action items, then one section per
    incident that ran with its own debrief findings."""
    debrief = generate_shift_debrief(shift)
    lines = []

    def add(text, font=_FONT_REGULAR, size=10, gap=0):
        for wrapped in _wrap(text, size):
            lines.append((wrapped, font, size, gap))
            gap = 0

    def heading(text):
        add(text, font=_FONT_BOLD, size=12, gap=14)

    def bullet(text):
        add(f"- {text}")

    add("Shift postmortem", font=_FONT_BOLD, size=16)
    add(f"{len(shift.runs)} incident(s) ran, "
        f"{len(shift.skipped)} skipped", gap=4)

    heading("Summary")
    add(f"Shift score: {debrief.shift_points}/100 "
        f"(grade {debrief.grade})")
    add(f"Error budget: burned {debrief.total_burn_pct:.1f}% of the "
        f"monthly budget (SLO {debrief.slo_percent}%, "
        f"{debrief.budget_minutes:.1f}m)")
    if debrief.budget_exhausted:
        add("The budget ran out mid-shift: "
            + ", ".join(debrief.skipped) + " never ran")
    if debrief.difficulty is not None:
        add(f"Difficulty: {debrief.difficulty.name}")
    if shift.purge_verification != "off":
        add(f"Purge verification: {shift.purge_verification}")
    if shift.seed is not None:
        add(f"Seed: {shift.seed}")

    heading("Incidents")
    for entry in debrief.incidents:
        downtime = (f"{entry['downtime_min']}m"
                    if entry["downtime_min"] is not None else "-")
        add(f"{entry['scenario']}: {entry['points']}/100 "
            f"(grade {entry['grade']}), downtime {downtime}, burned "
            f"{entry['burn_pct']:.1f}% (cumulative "
            f"{entry['cumulative_burn_pct']:.1f}%)")
    for name in debrief.skipped:
        add(f"{name}: skipped (error budget exhausted)")

    heading("What went well")
    if debrief.went_well:
        for item in debrief.went_well:
            bullet(item)
    else:
        add("none")

    heading("What went badly")
    if debrief.went_badly:
        for item in debrief.went_badly:
            bullet(item)
    else:
        add("none")

    heading("Action items")
    if debrief.action_items:
        for item in debrief.action_items:
            bullet(f"[ ] {item['action']} (owner: {item['owner']})")
    else:
        add("none")

    for run in shift.runs:
        incident_debrief = generate_debrief(run)
        heading(run.incident.title)
        add(f"Service: {run.incident.service} "
            f"[{run.incident.severity}] — "
            f"{incident_debrief.points}/100 "
            f"(grade {incident_debrief.grade})")
        add("What went well:", font=_FONT_BOLD, size=10, gap=8)
        if incident_debrief.went_well:
            for item in incident_debrief.went_well:
                bullet(item)
        else:
            add("none")
        add("What went badly:", font=_FONT_BOLD, size=10, gap=8)
        if incident_debrief.went_badly:
            for item in incident_debrief.went_badly:
                bullet(item)
        else:
            add("none")
        add("Action items:", font=_FONT_BOLD, size=10, gap=8)
        if incident_debrief.action_items:
            for item in incident_debrief.action_items:
                bullet(f"[ ] {item['action']} "
                       f"(owner: {item['owner']})")
        else:
            add("none")
    return lines


def render_shift_postmortem_pdf(shift) -> bytes:
    """Render a saved shift as a PDF postmortem.

    ``shift`` is a ShiftResult (a saved `shift --json` file). The
    findings come from ``debrief.generate_shift_debrief`` and each
    incident's own ``generate_debrief``, so the PDF never invents
    a cause the simulation did not show.
    """
    return _build_pdf(_flow_pages(_shift_content_lines(shift)))


def render_postmortem_pdf(result, what_if=None) -> bytes:
    """Render one saved run as a PDF postmortem.

    ``result`` is a SimulationResult (a normal run, or the replayed
    run of a what-if file); ``what_if`` is the WhatIfInfo provenance
    for a what-if file, or None. The findings come from
    ``debrief.generate_debrief``, so the PDF never invents a cause
    the simulation did not show.
    """
    return _build_pdf(_flow_pages(_content_lines(result, what_if)))
