#!/usr/bin/env python3
"""Shared helpers for tracking NISAR ISCE3 PGE jobs.

Both ``submit_isce3_job.py`` and ``check_isce3_job.py`` use this module so a
submitted job can be found again later:

- Every submission gets a readable, sortable **tag**
  (``nisar-isce3-<product>-<UTC timestamp>``) and a small **record** JSON written to
  the records directory (default ``~/.nisar-isce3-jobs``, overridable with the
  ``NISAR_ISCE3_JOBS_DIR`` env var).
- The status script reads those records back (by tag, record path, job id, or the
  most recent record) to reconstruct the otello ``Job`` and report status/products.

Logging is timestamped and level-tagged (see ``setup_logging``), replacing the old
ad-hoc ``[submit_isce3_job] ...`` prints.
"""

import glob
import json
import logging
import os
import re
from datetime import datetime, timezone

# --------------------------------------------------------------------------- #
# Records directory
# --------------------------------------------------------------------------- #
RECORDS_DIR_ENV = "NISAR_ISCE3_JOBS_DIR"
DEFAULT_RECORDS_DIR = os.path.expanduser("~/.nisar-isce3-jobs")


def records_dir() -> str:
    """Return the records directory (env override wins), creating it if needed."""
    d = os.environ.get(RECORDS_DIR_ENV) or DEFAULT_RECORDS_DIR
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging(name: str) -> logging.Logger:
    """Return a timestamped, INFO-level logger streaming to stdout.

    Format: ``2026-09-03T18:22:01Z INFO submit: message``. Idempotent -- calling
    twice for the same name does not add duplicate handlers.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        fmt = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        fmt.converter = __import__("time").gmtime  # timestamps in UTC
        handler.setFormatter(fmt)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #
_TAG_SANITIZE_RE = re.compile(r"[^A-Za-z0-9]+")


def _sanitize(part: str) -> str:
    """Lowercase, collapse non-alphanumerics to single dashes, trim dashes."""
    return _TAG_SANITIZE_RE.sub("-", (part or "").strip()).strip("-").lower()


def make_tag(product_name: str, when: datetime = None) -> str:
    """Build a readable, sortable, unique-ish tag for a submission.

    ``nisar-isce3-<sanitized product_name>-<YYYYmmddTHHMMSSZ>``. ``product_name`` is
    whatever the caller derives from the runconfig (e.g. the partial_granule_id
    prefix or the product_type). ``when`` defaults to now (UTC).
    """
    when = when or datetime.now(timezone.utc)
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    name = _sanitize(product_name) or "job"
    return f"nisar-isce3-{name}-{stamp}"


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
def record_path_for_tag(tag: str) -> str:
    return os.path.join(records_dir(), f"{tag}.json")


def write_record(record: dict) -> str:
    """Write ``<records_dir>/<tag>.json`` and return its path.

    Requires a ``tag`` key. Stamps ``submitted_at`` (UTC ISO 8601) if absent.
    """
    tag = record.get("tag")
    if not tag:
        raise ValueError("record must include a 'tag'")
    record.setdefault(
        "submitted_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    path = record_path_for_tag(tag)
    with open(path, "w") as f:
        json.dump(record, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def _read_record_file(path: str) -> dict:
    with open(path) as f:
        rec = json.load(f)
    rec.setdefault("_record_path", path)
    return rec


def list_records() -> list:
    """Return all records, newest first (by file mtime)."""
    paths = glob.glob(os.path.join(records_dir(), "*.json"))
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    out = []
    for p in paths:
        try:
            out.append(_read_record_file(p))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def latest_record() -> dict:
    """Return the most recently written record, or None if none exist."""
    recs = list_records()
    return recs[0] if recs else None


def load_record(ref: str) -> dict:
    """Resolve a record from a tag, a record file path, or a bare job id.

    Returns the record dict, or None if nothing matches. A path is used directly; a
    tag maps to ``<records_dir>/<tag>.json``; otherwise ``ref`` is matched against
    every record's ``job_id``.
    """
    if not ref:
        return None
    # Explicit path.
    if os.path.sep in ref or ref.endswith(".json"):
        if os.path.exists(ref):
            return _read_record_file(ref)
        return None
    # Tag -> file.
    tag_path = record_path_for_tag(ref)
    if os.path.exists(tag_path):
        return _read_record_file(tag_path)
    # Fall back: match by job_id across all records.
    for rec in list_records():
        if rec.get("job_id") == ref:
            return rec
    return None
