"""Shareable postmortem bundle: one portable file per incident run.

`bundle <run.json> [--out bundle.zip]` packages a saved run into a
single zip that can be shared and re-imported: the source run as
``run.json`` (verbatim for a normal run; the replayed run plus the base
run as ``base-run.json`` for a saved ``what-if --json`` file), the HTML
postmortem from ``export_html`` as ``postmortem.html``, and a
``manifest.json`` with the provenance — incident-lab version, incident
and score, seed and difficulty, what-if overrides and points delta
when the source is a what-if replay — plus a SHA-256 hash of every
other member. The bundle is validated as it is created: the written
zip is re-opened, every hash is re-checked, and ``run.json`` is loaded
back through ``replay`` to prove it re-imports. A bundle that fails
validation is deleted, never left behind.

``bundle --check <bundle.zip>`` validates a bundle someone shared:
same checks, no re-creation. Either way the run inside a bundle is a
plain saved run, so ``replay``, ``debrief`` and ``postmortem`` all work
on it after unzipping.
"""

import hashlib
import html
import json
import os
import zipfile
from datetime import datetime, timezone

from . import __version__
from .debrief import grade
from .export_html import render_postmortem_html
from .replay import _result_from_dict, load_saved

BUNDLE_FORMAT = "incident-lab-bundle"
BUNDLE_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
RUN_NAME = "run.json"
BASE_RUN_NAME = "base-run.json"
POSTMORTEM_NAME = "postmortem.html"


def _sha256(data: bytes) -> str:
    """SHA-256 hex digest of one bundle member's bytes."""
    return hashlib.sha256(data).hexdigest()


def _file_entry(name: str, data: bytes) -> dict:
    """One manifest file entry: name, hash and size."""
    return {"name": name, "sha256": _sha256(data), "bytes": len(data)}


def _manifest(loaded, entries) -> dict:
    """The bundle manifest: provenance plus one entry per member."""
    result = loaded.result
    what_if = None
    if loaded.what_if is not None:
        what_if = {
            "overrides": list(loaded.what_if.overrides),
            "points_delta": loaded.what_if.points_delta,
            "base_points": loaded.what_if.base_points,
        }
    return {
        "format": BUNDLE_FORMAT,
        "format_version": BUNDLE_FORMAT_VERSION,
        "generator": f"incident-lab {__version__}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "incident": {
            "id": result.incident.id,
            "service": result.incident.service,
            "title": result.incident.title,
            "severity": result.incident.severity,
        },
        "score": {
            "points": result.score.points,
            "grade": grade(result.score.points),
        },
        "seed": result.seed,
        "difficulty": (result.difficulty.name
                       if result.difficulty is not None else None),
        "what_if": what_if,
        "files": entries,
    }


def create_bundle(run_path: str, out_path: str) -> dict:
    """Package a saved run as a bundle zip at ``out_path``.

    Returns the manifest. Raises FileNotFoundError if the run file is
    missing, json.JSONDecodeError on bad JSON, and ValueError on a
    run file that is not a saved run (or a what-if file whose delta
    does not match its runs), or on a bundle that fails its own
    validation — in which case the output file is removed.
    """
    loaded = load_saved(run_path)
    with open(run_path, "rb") as handle:
        source_bytes = handle.read()
    if loaded.what_if is None:
        run_data = source_bytes
        base_data = None
    else:
        run_data = (json.dumps(loaded.result.to_dict(), indent=2)
                    + "\n").encode("utf-8")
        base_data = (json.dumps(loaded.what_if.base_result.to_dict(),
                                indent=2) + "\n").encode("utf-8")
    html_text = render_postmortem_html(loaded.result,
                                       what_if=loaded.what_if)
    members = [(RUN_NAME, run_data)]
    if base_data is not None:
        members.append((BASE_RUN_NAME, base_data))
    members.append((POSTMORTEM_NAME, html_text.encode("utf-8")))
    entries = [_file_entry(name, data) for name, data in members]
    manifest = _manifest(loaded, entries)
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    try:
        with zipfile.ZipFile(out_path, "w",
                             compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(MANIFEST_NAME, manifest_bytes)
            for name, data in members:
                bundle.writestr(name, data)
        validate_bundle(out_path)
    except Exception:
        try:
            os.unlink(out_path)
        except OSError:
            pass
        raise
    return manifest


def validate_bundle(zip_path: str) -> dict:
    """Validate a bundle zip and return its manifest.

    Checks the zip opens, the manifest is present and well formed,
    every listed file is present with matching SHA-256 and size, no
    extra members are hiding in the zip, ``run.json`` loads back as a
    saved run, and — for a what-if bundle — the base run loads too
    and the points delta matches. Raises ValueError with the reason
    on the first failed check.
    """
    try:
        bundle = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"bundle: cannot open {zip_path}: {exc}")
    with bundle:
        names = set(bundle.namelist())
        if MANIFEST_NAME not in names:
            raise ValueError("bundle: missing manifest.json")
        try:
            manifest = json.loads(
                bundle.read(MANIFEST_NAME).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"bundle: manifest.json is not valid JSON: "
                             f"{exc}")
        if not isinstance(manifest, dict):
            raise ValueError("bundle: manifest.json must be a JSON object")
        if manifest.get("format") != BUNDLE_FORMAT:
            raise ValueError(
                f"bundle: manifest format must be {BUNDLE_FORMAT!r}")
        if manifest.get("format_version") != BUNDLE_FORMAT_VERSION:
            raise ValueError(
                "bundle: manifest format_version must be "
                f"{BUNDLE_FORMAT_VERSION}")
        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries:
            raise ValueError("bundle: manifest 'files' must be a "
                             "non-empty list")
        expected = {MANIFEST_NAME}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("bundle: manifest file entries must be "
                                 "objects")
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("bundle: manifest file entry needs a "
                                 "name")
            if name in expected:
                raise ValueError(f"bundle: duplicate file entry {name!r}")
            expected.add(name)
            if name not in names:
                raise ValueError(f"bundle: listed file {name!r} is "
                                 f"missing from the zip")
            data = bundle.read(name)
            if _sha256(data) != entry.get("sha256"):
                raise ValueError(f"bundle: {name!r} fails its SHA-256 "
                                 f"check")
            if len(data) != entry.get("bytes"):
                raise ValueError(f"bundle: {name!r} has the wrong size")
        if names != expected:
            extra = sorted(names - expected)
            raise ValueError(f"bundle: unexpected extra members: "
                             f"{', '.join(extra)}")
        if RUN_NAME not in names:
            raise ValueError(f"bundle: missing {RUN_NAME}")
        try:
            run_data = json.loads(bundle.read(RUN_NAME).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"bundle: {RUN_NAME} is not valid JSON: "
                             f"{exc}")
        try:
            result = _result_from_dict(run_data)
        except ValueError as exc:
            raise ValueError(f"bundle: {RUN_NAME} is not a saved run: "
                             f"{exc}")
        if POSTMORTEM_NAME not in names:
            raise ValueError(f"bundle: missing {POSTMORTEM_NAME}")
        html_bytes = bundle.read(POSTMORTEM_NAME)
        if b"<!DOCTYPE html>" not in html_bytes:
            raise ValueError(f"bundle: {POSTMORTEM_NAME} is not an HTML "
                             f"postmortem")
        if html.escape(result.incident.title).encode("utf-8") \
                not in html_bytes:
            raise ValueError(f"bundle: {POSTMORTEM_NAME} does not match "
                             f"the bundled run")
        what_if = manifest.get("what_if")
        if what_if is not None:
            if BASE_RUN_NAME not in names:
                raise ValueError(f"bundle: a what-if bundle needs "
                                 f"{BASE_RUN_NAME}")
            try:
                base_data = json.loads(
                    bundle.read(BASE_RUN_NAME).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError(f"bundle: {BASE_RUN_NAME} is not valid "
                                 f"JSON: {exc}")
            try:
                base = _result_from_dict(base_data)
            except ValueError as exc:
                raise ValueError(f"bundle: {BASE_RUN_NAME} is not a "
                                 f"saved run: {exc}")
            if base.incident.id != result.incident.id:
                raise ValueError("bundle: base run is of a different "
                                 "incident than the replay")
            if (result.score.points - base.score.points
                    != what_if.get("points_delta")):
                raise ValueError("bundle: what-if points delta does not "
                                 "match the bundled runs' scores")
    return manifest


def summarize_bundle(manifest: dict) -> str:
    """One short human-readable summary of a validated bundle."""
    incident = manifest["incident"]
    score = manifest["score"]
    names = ", ".join(entry["name"] for entry in manifest["files"])
    lines = [
        f"bundle: {manifest['format']} v{manifest['format_version']} "
        f"({manifest['generator']})",
        f"incident: {incident['title']} [{incident['id']}]",
        f"score: {score['points']}/100 (grade {score['grade']})",
        f"files: {names}",
    ]
    what_if = manifest.get("what_if")
    if what_if is not None:
        lines.append(
            f"what-if: {'; '.join(what_if['overrides'])} "
            f"({what_if['points_delta']:+d} points, base "
            f"{what_if['base_points']}/100)")
    return "\n".join(lines)


def default_bundle_path(run_path: str) -> str:
    """Default bundle path for a run file: ``<stem>-bundle.zip``."""
    stem = os.path.splitext(os.path.basename(run_path))[0]
    return f"{stem}-bundle.zip"
