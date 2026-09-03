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

Exit codes (handy for scripting/CI):
  0  completed
  2  failed / offline
  3  still queued / running (one-shot only; --watch waits instead)
  4  could not resolve a job
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from job_records import (  # noqa: E402
    latest_record,
    list_records,
    load_record,
    setup_logging,
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
def report_products(job):
    """Log the generated products (identifier + URLs / S3 paths)."""
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
        logger.info(f"  - {pid}")
        urls = p.get("urls") or []
        if isinstance(urls, str):
            urls = [urls]
        for u in urls:
            logger.info(f"      {u}")


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


def report(job, status, args):
    """Log a status line + products/failure detail. Returns an exit code."""
    friendly = FRIENDLY.get(status, status)
    logger.info(f"status: {friendly} [{status}]  job_id={job.job_id}")

    if status == "job-completed":
        report_products(job)
        return 0
    if status in ("job-failed", "job-offline"):
        report_failure(job, args.traceback)
        return 2
    if status == "job-deduped":
        # Deduped: an identical job already produced the products.
        report_products(job)
        return 0
    # queued / started
    if args.products:
        report_products(job)
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
        return report(job, status, args)

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
            return report(job, status, args)
        logger.info(f"status: {FRIENDLY.get(status, status)} [{status}]")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
