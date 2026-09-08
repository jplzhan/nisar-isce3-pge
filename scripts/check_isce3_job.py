#!/usr/bin/env python3
"""Check the status of a NISAR ISCE3 PGE job submitted with submit_isce3_job.py.

The submit script writes a record JSON (tag, job id, queue, runconfig, ...) into
~/.nisar-isce3-jobs (override with $NISAR_ISCE3_JOBS_DIR) and prints a readable tag.
This script resolves that job and reports its status, and -- when the job has
completed -- lists the final output products (the staged <granuleId>.h5 and its
sidecars) with their URLs / S3 paths.

Selecting a job (first match wins):
  --job-id <uuid>     : check this Mozart job id directly
  --tag <tag>         : look up the record by tag (falls back to Mozart get_jobs)
  --record <path>     : read a specific record JSON
  (nothing)           : use the most recent record in the records dir

Modes:
  one-shot (default)  : print status once and exit
  --watch             : poll until the job reaches a terminal state, then report

Finding output products:
When a job completes, GRQ reports each product's S3 location directly. The granule
name is only finalized by the SAS, but the submit record keeps the
partial_granule_id template, so --search can also LOCATE the products by listing
the S3 output tree for the product family and matching the filled granule name
against the template. --search works even before GRQ has published, and falls back
automatically when GRQ lists nothing. --venue narrows the search to one venue.

Exit codes (handy for scripting/CI):
  0  completed
  2  failed / offline
  3  still queued / running (one-shot only; --watch waits instead)
  4  could not resolve a job
"""

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from job_records import (  # noqa: E402
    latest_record,
    list_records,
    load_record,
    setup_logging,
)

from product_search import (  # noqa: E402
    expected_s3_prefix,
    find_products_by_prefix,
)

OTELLO_PKG = os.path.expanduser("~/otello")

POLL_SECONDS = 30

TERMINAL = {"job-completed", "job-failed", "job-deduped", "job-offline"}
FRIENDLY = {
    "job-queued": "queued",
    "job-started": "running",
    "job-completed": "completed",
    "job-failed": "FAILED",
    "job-deduped": "deduped (identical job already ran)",
    "job-offline": "offline (worker lost)",
}

logger = setup_logging("check")


def _import_otello():
    if OTELLO_PKG not in sys.path:
        sys.path.insert(0, OTELLO_PKG)
    import otello  # noqa: E402
    return otello


# --------------------------------------------------------------------------- #
# Job resolution
# --------------------------------------------------------------------------- #
def resolve_job(otello, mozart, args):
    """Return (Job, record_or_None) for the requested selection, or (None, None)."""
    Job = otello.mozart.Job

    def _job(job_id, tags=None):
        return Job(job_id=job_id, tags=tags, cfg=mozart._cfg, session=mozart._session)

    # 1. Explicit job id.
    if args.job_id:
        return _job(args.job_id), load_record(args.job_id)

    # 2. Explicit record path or tag.
    record = None
    if args.record:
        record = load_record(args.record)
        if record is None:
            logger.error(f"no record found at {args.record}")
            return None, None
    elif args.tag:
        record = load_record(args.tag)
        if record is None:
            # No local record: fall back to a Mozart tag lookup.
            logger.info(f"no local record for tag {args.tag}; querying Mozart by tag")
            jobs = mozart.get_jobs(tag=args.tag)
            if not jobs:
                logger.error(f"no Mozart jobs found for tag {args.tag}")
                return None, None
            if len(jobs) > 1:
                logger.warning(f"{len(jobs)} jobs share tag {args.tag}; using the first")
            return jobs[0], None
    else:
        # 3. Nothing specified -> most recent record.
        record = latest_record()
        if record is None:
            logger.error(
                "no job selected and no records found in the records dir. "
                "Pass --job-id / --tag / --record, or submit a job first."
            )
            return None, None
        logger.info(f"using most recent record: {record.get('tag')} "
                    f"({record.get('_record_path')})")

    job_id = record.get("job_id")
    if not job_id:
        logger.error(f"record for tag {record.get('tag')} has no job_id")
        return None, None
    return _job(job_id, tags=record.get("tag")), record


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _clean_s3_url(url: str) -> str:
    """Normalize HySDS's endpoint-style S3 URL to plain s3://<bucket>/<key>.

    GRQ returns e.g. ``s3://s3-us-west-2.amazonaws.com:80/<bucket>/<key>``; the host
    segment is the S3 endpoint, not the bucket. Strip it so the first path segment
    (the real bucket) leads. Non-S3 or already-clean URLs are returned unchanged.
    """
    if not url.startswith("s3://"):
        return url
    rest = url[len("s3://"):]
    host, _, tail = rest.partition("/")
    # host looks like an S3 endpoint (contains amazonaws.com or a :port) -> drop it.
    if tail and ("amazonaws.com" in host or ":" in host):
        return "s3://" + tail
    return url


def _s3_urls(product: dict) -> list:
    """Return the cleaned s3:// URLs from a product doc's urls (product data only)."""
    out = []
    vals = product.get("urls") or []
    if isinstance(vals, str):
        vals = [vals]
    for v in vals:
        if isinstance(v, str) and v.startswith("s3://"):
            out.append(_clean_s3_url(v))
    return out


def _venue_from_bucket(s3_url: str) -> str:
    """Extract <venue> from an s3://nisar-<venue>-rs-ondemand/... URL, or None."""
    m = re.match(r"s3://nisar-([a-z0-9]+)-rs-ondemand/", s3_url or "")
    return m.group(1) if m else None


def report_products(job):
    """Log the generated products (identifier + normalized S3 / browse URLs)."""
    try:
        products = job.get_generated_products()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"could not fetch generated products: {exc}")
        return
    if not products:
        logger.info("no generated products reported")
        return
    logger.info(f"{len(products)} product(s):")
    for p in products:
        pid = p.get("id") or p.get("_id") or "(unknown id)"
        dataset = p.get("dataset") or p.get("ipath") or ""
        logger.info(f"  - {pid}" + (f"  [{dataset}]" if dataset else ""))
        # The s3:// output location reported by GRQ (authoritative).
        s3 = _s3_urls(p)
        for u in s3:
            logger.info(f"      output: {u}")
        if not s3:
            urls = p.get("urls") or []
            if isinstance(urls, str):
                urls = [urls]
            for u in urls:
                logger.info(f"      {u}")
        # Cross-check against the granule-derived canonical prefix. Venue comes
        # from the reported bucket; if GRQ gave no s3 url we cannot know the venue.
        venue = _venue_from_bucket(s3[0]) if s3 else None
        if venue:
            derived = expected_s3_prefix(pid, venue)
            if derived:
                logger.info(f"      expected prefix: {derived}")
    return len(products)


def report_products_by_search(record, venue=None):
    """Locate staged products in S3 by matching the record's partial_granule_id.

    Fallback for when GRQ has not listed products yet (or no local record maps to a
    job): we know the granule prefix + product family from the submitted
    partial_granule_id, so list the S3 output tree and match the filled name.
    Returns the number of matches found.
    """
    if not record:
        logger.info("no local record; cannot search S3 by granule prefix")
        return 0
    template = record.get("partial_granule_id")
    if not template:
        logger.info("record has no partial_granule_id; cannot search S3 by prefix")
        return 0
    logger.info(f"searching S3 output for granule prefix "
                f"{template.split('{', 1)[0]!r}...")
    hits = find_products_by_prefix(template, venue=venue)
    if not hits:
        logger.info("no matching products found in S3 (job may not have published yet)")
        return 0
    logger.info(f"{len(hits)} matching product(s) in S3:")
    for h in hits:
        logger.info(f"  - {h}")
    return len(hits)


def report_failure(job, dump_traceback):
    try:
        exc = job.get_exception()
        logger.error(f"job exception: {exc}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"could not fetch exception detail: {e}")
    if dump_traceback:
        try:
            logger.error("traceback:\n" + str(job.get_traceback()))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"could not fetch traceback: {e}")
    else:
        logger.info("re-run with --traceback for the full worker traceback")


def report(job, status, args, record=None):
    """Log a status line + products/failure detail. Returns an exit code."""
    friendly = FRIENDLY.get(status, status)
    logger.info(f"status: {friendly} [{status}]  job_id={job.job_id}")

    if status in ("job-completed", "job-deduped"):
        n = report_products(job)
        # If GRQ listed nothing (or --search forced), fall back to an S3 prefix
        # search using the submitted partial_granule_id template.
        if (not n or args.search) and record:
            report_products_by_search(record, venue=args.venue)
        return 0
    if status in ("job-failed", "job-offline"):
        report_failure(job, args.traceback)
        return 2
    # queued / started
    if args.products or args.search:
        report_products_by_search(record, venue=args.venue)
    return 3


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sel = parser.add_mutually_exclusive_group()
    sel.add_argument("--job-id", help="Mozart job id (UUID) to check directly")
    sel.add_argument("--tag", help="submission tag to look up")
    sel.add_argument("--record", help="path to a specific record JSON")
    parser.add_argument("--watch", action="store_true",
                        help=f"poll every {POLL_SECONDS}s until the job is terminal")
    parser.add_argument("--products", action="store_true",
                        help="also list products even if the job is not yet complete")
    parser.add_argument("--search", action="store_true",
                        help="find output products in S3 by matching the submitted "
                             "partial_granule_id prefix (uses the local record). "
                             "Works before GRQ lists products; can run standalone "
                             "with --tag/--record and no Mozart status check.")
    parser.add_argument("--venue", help="limit the S3 product search to one venue "
                                        "(e.g. st, adt); default: probe all")
    parser.add_argument("--traceback", action="store_true",
                        help="on failure, dump the full worker traceback")
    parser.add_argument("--list", action="store_true",
                        help="list known job records (newest first) and exit")
    args = parser.parse_args()

    if args.list:
        recs = list_records()
        if not recs:
            logger.info("no records found")
            return 0
        for r in recs:
            logger.info(f"{r.get('tag')}  job_id={r.get('job_id')}  "
                        f"queue={r.get('queue')}  submitted={r.get('submitted_at')}")
        return 0

    otello = _import_otello()
    mozart = otello.Mozart()

    job, record = resolve_job(otello, mozart, args)
    if job is None:
        return 4
    if record:
        logger.info(f"tag={record.get('tag')} product_type={record.get('product_type')} "
                    f"queue={record.get('queue')} submitted={record.get('submitted_at')}")

    if not args.watch:
        try:
            status = job.get_status()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"could not get status for job {job.job_id}: {exc}")
            return 4
        return report(job, status, args, record=record)

    # Watch until terminal.
    logger.info(f"watching job {job.job_id} (poll every {POLL_SECONDS}s)...")
    while True:
        try:
            status = job.get_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"status check failed, retrying: {exc}")
            time.sleep(POLL_SECONDS)
            continue
        if status in TERMINAL:
            return report(job, status, args, record=record)
        logger.info(f"status: {FRIENDLY.get(status, status)} [{status}]")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
