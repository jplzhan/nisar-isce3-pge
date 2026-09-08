#!/usr/bin/env python3
"""Locate NISAR ISCE3 output products in S3 by granule-name prefix.

Shared by submit_isce3_job.py (pre-submit collision check + product-counter bump)
and check_isce3_job.py (locate a completed job's outputs).

Output layout (verified against a real completed job):
    s3://nisar-<venue>-rs-ondemand/products/<LEVEL>_L_<TYPE>/YYYY/MM/DD/<granule>/
LEVEL/TYPE come from the granule id (NISAR_L2_PR_GUNW_... -> L2, GUNW); the ``_L_``
is the LSAR band; YYYY/MM/DD is the granule's first (reference-start) timestamp.

The granule name is only finalized by the SAS -- its {MODE}/{PO}/timestamp tokens
and the trailing product counter are unknown up front. But the static prefix and
the product family ARE known from the runconfig's partial_granule_id, so we can
list the S3 output tree for that family and match filled granule names against a
template regex.
"""

import re

from job_records import setup_logging

logger = setup_logging("products")

PRODUCTS_BUCKET_FMT = "nisar-{venue}-rs-ondemand"
# Venues to probe when one is not supplied (matches submit_isce3_job.py).
PRODUCT_VENUES = ("st", "adt")

# granule: NISAR_<level>_<procType>_<type>_..._<YYYYMMDD>T<HHMMSS>_...
_GRANULE_RE = re.compile(
    r"^NISAR_(?P<level>L\d)_[A-Z]{2}_(?P<type>[A-Z0-9]+)_.*?_(?P<date>\d{8})T\d{6}_"
)
# Level/type from just the static prefix (no timestamp needed).
_LEVEL_TYPE_RE = re.compile(r"^NISAR_(?P<level>L\d)_[A-Z]{2}_(?P<type>[A-Z0-9]+)_")
# Trailing product counter (the _001 at the end of a granule / template).
_COUNTER_RE = re.compile(r"_(\d+)$")


# --------------------------------------------------------------------------- #
# Path / name derivation
# --------------------------------------------------------------------------- #
def expected_s3_prefix(granule_id: str, venue: str) -> str:
    """Derive the canonical S3 output prefix for a fully-resolved granule.

    Returns ``s3://nisar-<venue>-rs-ondemand/products/<LEVEL>_L_<TYPE>/YYYY/MM/DD/
    <granule>/`` or None if the granule id does not parse.
    """
    m = _GRANULE_RE.match(granule_id or "")
    if not m:
        return None
    level, ptype, date = m.group("level"), m.group("type"), m.group("date")
    bucket = PRODUCTS_BUCKET_FMT.format(venue=venue)
    return (f"s3://{bucket}/products/{level}_L_{ptype}/"
            f"{date[0:4]}/{date[4:6]}/{date[6:8]}/{granule_id}/")


def _granule_date_path(granule_id: str) -> str:
    """Return the ``YYYY/MM/DD/`` segment parsed from the granule's first timestamp."""
    m = _GRANULE_RE.match(granule_id)
    if not m:
        return ""
    d = m.group("date")
    return f"{d[0:4]}/{d[4:6]}/{d[6:8]}/"


def _level_type(partial_granule_id: str):
    """Return (level, type) parsed from a granule id or partial_granule_id."""
    m = _LEVEL_TYPE_RE.match(partial_granule_id or "")
    if not m:
        return None, None
    return m.group("level"), m.group("type")


# --------------------------------------------------------------------------- #
# Template -> regex
# --------------------------------------------------------------------------- #
def granule_regex_from_template(partial_granule_id: str) -> "re.Pattern":
    """Full-granule match regex from a template (product counter matched literally).

    Each ``{token}`` (e.g. {MODE}, {PO}, {RefStartDateTime}) is one
    underscore-delimited field, so it becomes ``[^_]+``; static text is literal.
    Anchored at both ends (matches a complete granule id, no trailing ``.h5``).
    """
    parts = re.split(r"\{[^}]+\}", partial_granule_id)
    pattern = "[^_]+".join(re.escape(p) for p in parts)
    return re.compile("^" + pattern + "$")


def counter_agnostic_regex(partial_granule_id: str) -> "re.Pattern":
    """Like granule_regex_from_template but the trailing counter is a wildcard.

    Matches every version (counter) of a granule so existing counters can be
    collected before submission. The counter is captured as group ``counter``.
    """
    base = _COUNTER_RE.sub("", partial_granule_id)
    parts = re.split(r"\{[^}]+\}", base)
    pattern = "[^_]+".join(re.escape(p) for p in parts)
    return re.compile("^" + pattern + r"_(?P<counter>\d+)$")


def extract_counter(granule_id: str):
    """Return the trailing product counter as an int, or None."""
    m = _COUNTER_RE.search(granule_id or "")
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# S3 listing
# --------------------------------------------------------------------------- #
def _s3_client():
    try:
        import boto3
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"boto3 unavailable, cannot search S3: {exc}")
        return None
    return boto3.client("s3")


def _list_granule_dirs(s3, bucket: str, prefix: str) -> list:
    """Return the <granule> directory names under products/<LEVEL>_L_<TYPE>/.

    Walks YYYY/ -> MM/ -> DD/ -> <granule>/ via Delimiter='/' + CommonPrefixes
    paging. Never raises -- logs and returns [] on failure.
    """
    def _children(pfx):
        out, token = [], None
        while True:
            kw = {"Bucket": bucket, "Prefix": pfx, "Delimiter": "/"}
            if token:
                kw["ContinuationToken"] = token
            resp = s3.list_objects_v2(**kw)
            out.extend(cp["Prefix"] for cp in resp.get("CommonPrefixes", []))
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break
        return out

    try:
        level = [prefix]
        for _ in range(4):  # YYYY, MM, DD, granule
            nxt = []
            for pfx in level:
                nxt.extend(_children(pfx))
            level = nxt
            if not level:
                break
        return [p.rstrip("/").split("/")[-1] for p in level]
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"S3 listing failed under s3://{bucket}/{prefix}: {exc}")
        return []


def _iter_matching(partial_granule_id: str, regex, venue: str = None) -> list:
    """Return [(venue, bucket, granule)] whose granule dir matches ``regex``."""
    level, ptype = _level_type(partial_granule_id.split("{", 1)[0])
    if not level:
        logger.warning(f"cannot parse level/type from {partial_granule_id!r}")
        return []
    s3 = _s3_client()
    if s3 is None:
        return []
    venues = [venue] if venue else list(PRODUCT_VENUES)
    hits = []
    for v in venues:
        bucket = PRODUCTS_BUCKET_FMT.format(venue=v)
        prefix = f"products/{level}_L_{ptype}/"
        for granule in _list_granule_dirs(s3, bucket, prefix):
            if regex.match(granule):
                hits.append((v, bucket, granule))
    return hits


# --------------------------------------------------------------------------- #
# Public search helpers
# --------------------------------------------------------------------------- #
def find_products_by_prefix(partial_granule_id: str, venue: str = None) -> list:
    """Find staged products matching a template (product counter matched literally).

    Returns ``s3://<bucket>/products/<LEVEL>_L_<TYPE>/YYYY/MM/DD/<granule>/`` URLs.
    """
    if not partial_granule_id:
        return []
    regex = granule_regex_from_template(partial_granule_id)
    urls = []
    for _v, bucket, granule in _iter_matching(partial_granule_id, regex, venue):
        urls.append(f"s3://{bucket}/products/"
                    + f"{'_'.join(_level_type(granule))}/"
                    + _granule_date_path(granule) + f"{granule}/")
    return urls


def find_existing_counters(partial_granule_id: str, venue: str = None) -> list:
    """Return the sorted product counters already present for this granule family.

    Uses the counter-agnostic regex so every version of a matching granule is
    considered. Empty list means nothing collides yet.
    """
    if not partial_granule_id:
        return []
    regex = counter_agnostic_regex(partial_granule_id)
    counters = set()
    for _v, _bucket, granule in _iter_matching(partial_granule_id, regex, venue):
        c = extract_counter(granule)
        if c is not None:
            counters.add(c)
    return sorted(counters)


# --------------------------------------------------------------------------- #
# Product-counter rewrite (raw-text, to avoid reserializing the YAML)
# --------------------------------------------------------------------------- #
def set_product_counter(runconfig_text: str, counter: int) -> str:
    """Set the product counter in both places it appears in the runconfig.

    - ``partial_granule_id``: the trailing ``_NNN`` (zero-padded to >= 3 digits),
      which becomes the counter in the output product name.
    - ``product_counter``: the integer field under product_path_group.

    Operates on the raw text (line-anchored regex) so surrounding YAML is untouched.
    """
    cstr = f"{counter:03d}"
    text = re.sub(
        r"(?m)^(?P<pre>\s*partial_granule_id:\s*.*_)\d+\s*$",
        lambda m: m.group("pre") + cstr,
        runconfig_text,
    )
    text = re.sub(
        r"(?m)^(?P<pre>\s*product_counter:\s*)\d+\s*$",
        lambda m: m.group("pre") + str(counter),
        text,
    )
    return text
