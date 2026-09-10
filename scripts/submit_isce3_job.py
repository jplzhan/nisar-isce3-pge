#!/usr/bin/env python3
"""Submit a NISAR ISCE3 PGE job to Mozart for a given ISCE3 version.

End-to-end flow
---------------
1. Call the Mozart endpoint `/mozart/api/v0.1/pge/isce3` with a VERSION (an ISCE3
   branch / tag / commit, default "develop"). That endpoint does all the heavy
   lifting: it resolves VERSION to the ISCE3 commit hash, forwards it to the
   Jenkins CI machine, and reports whether the job is already registered/built
   or triggers a rebuild. Re-running is safe. It returns the resolved short hash,
   which is the branch used for the registered HySDS job.
2. Submit the `job-run_isce3:<short_hash>` HySDS job to Mozart with the runconfig
   (default: the GUNW example) passed inline as YAML text.

Config
------
Host / auth / TLS all come from otello's own ~/.config/otello/config.yml (shared
by the endpoint call and the job submission). An optional submit config
(--config, then $NISAR_ISCE3_SUBMIT_CONFIG, then
~/.config/nisar-isce3-submit/config.yml) may override the default queue.

Modeled on ~/alos-to-insar/run.py and pcm.py (passing config file *contents* as
inline strings).

Tracking
--------
Each submission is given a readable tag (``nisar-isce3-<product>-<UTC timestamp>``)
and a record JSON in ~/.nisar-isce3-jobs (override with $NISAR_ISCE3_JOBS_DIR). The
end-of-run summary prints the exact ``check_isce3_job.py`` command to check status
and locate the final output products.

Product counter
---------------
Before submitting, the granule prefix is searched in the S3 output buckets. If
products for this granule already exist, the product counter (the trailing ``_NNN``
in the output name, plus the ``product_counter`` field) is bumped to the next value
so the rerun does not clobber a prior product and a later prefix search matches only
this job's output. The bump is confirmed interactively unless ``--yes``; use
``--no-counter-bump`` to submit at the runconfig's current counter regardless.
"""

import argparse
import os
import re
import sys
import time

import requests
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from job_records import make_tag, setup_logging, write_record  # noqa: E402
from otello_tls import apply_tls, resolve_verify  # noqa: E402
from product_search import (  # noqa: E402
    find_existing_counters,
    set_product_counter,
)

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_VERSION = "develop"
DEFAULT_RUNCONFIG = os.path.expanduser(
    "~/ondemand-resources/runconfigs/NISAR_L2_PR_GUNW_020_001_A_004_022_/"
    "NISAR_L2_PR_GUNW_020_001_A_004_022_.yaml"
)
DEFAULT_NETRC = os.path.expanduser("~/.netrc")
DEFAULT_CONFIG = os.path.expanduser("~/.config/nisar-isce3-submit/config.yml")
CONFIG_ENV = "NISAR_ISCE3_SUBMIT_CONFIG"

# Route each SAS workflow to the queue that matches it, mirroring the
# alos-to-insar convention (RSLC->...-rslc, GSLC->...-gslc, etc.). The workflow
# is derived from the runconfig's primary_executable.product_type. INSAR is
# gpu/cpu-suffixed based on the runconfig's worker.gpu_enabled flag. Any of these
# may be overridden per-workflow via the config file's `queues:` mapping.
SINGLE_MODULE = {"RSLC": "focus", "GSLC": "gslc", "GCOV": "gcov", "SME2": "sme2"}
INSAR_CODES = {"RIFG", "RUNW", "ROFF", "GUNW", "GOFF"}
WORKFLOW_QUEUES = {
    "focus": "nisar-job_worker-sciflo-rslc",
    "gslc": "nisar-job_worker-sciflo-gslc",
    "gcov": "nisar-job_worker-sciflo-gcov",
    "sme2": "nisar-job_worker-sciflo-sme2",
    "insar": "nisar-job_worker-sciflo-insar",  # -gpu/-cpu suffix added at runtime
}

OTELLO_PKG = os.path.expanduser("~/otello")

# Mozart endpoint that builds/registers the ISCE3 PGE image for a version.
PGE_ISCE3_ENDPOINT = "mozart/api/v0.1/pge/isce3"

# PGE repo whose branch (the resolved short_hash) is the Jenkins CI job. Used to
# query the real Jenkins build status via otello.CI.get_build_status().
PGE_REPO = "https://github.jpl.nasa.gov/NISAR-ODS/nisar-isce3-pge.git"

# Seconds between Jenkins build-status polls when watching an in-progress build.
BUILD_POLL_SECONDS = 30

# Jenkins build results that count as a completed, successful build.
BUILD_SUCCESS_RESULTS = {"SUCCESS"}

# Default Mozart private IP is read from an on-demand config object in S3. The
# bucket is venue-suffixed; try "st" first, then "adt". The object is JSON with a
# "MOZART_PVT_IP" key. Overridden by --mozart-pvt-ip.
MOZART_CONFIG_VENUES = ("st", "adt")
MOZART_CONFIG_KEY = "ondemand-test/mozart_config.json"


logger = setup_logging("submit")


def log(msg: str) -> None:
    """Thin INFO-level shim over the module logger (kept so call sites stay terse)."""
    logger.info(msg)


def product_name_from_cfg(cfg: dict) -> str:
    """Derive a readable product name for tagging from a parsed runconfig.

    Prefer the partial_granule_id prefix (up to the first token placeholder) so the
    tag reflects the actual granule family; fall back to the product_type.
    """
    try:
        primary = cfg["runconfig"]["groups"]["primary_executable"]
    except (KeyError, TypeError):
        return "job"
    pgid = primary.get("partial_granule_id")
    if isinstance(pgid, str) and pgid:
        prefix = pgid.split("{", 1)[0].strip("_")
        if prefix:
            return prefix
    return primary.get("product_type") or "job"


# --------------------------------------------------------------------------- #
# Config (optional; only overrides the default queue)
# --------------------------------------------------------------------------- #
def load_config(path: str = None) -> dict:
    """Load the optional submit config. Returns {} when none is present."""
    resolved = path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG
    if not os.path.exists(resolved):
        if path:  # explicitly requested but missing -> error
            raise SystemExit(f"config not found: {resolved}")
        return {}
    with open(resolved) as f:
        cfg = yaml.safe_load(f) or {}
    log(f"loaded config: {resolved}")
    return cfg


# --------------------------------------------------------------------------- #
# Composite release ID (CRID) letter normalization
# --------------------------------------------------------------------------- #
# The CRID is always 1 capital letter + 5 digits (e.g. P05023, X01000). Only S
# and A are allowed as the leading letter on the cluster; anything else must be
# rewritten before submission, including its embedded use in partial_granule_id
# (which becomes the output product name).
ALLOWED_CRID_LETTERS = ("S", "A")
CRID_RE = re.compile(r"^[A-Z][0-9]{5}$")

# Runconfig fields whose value carries the CRID and IS output-naming: the CRID
# field itself and the partial_granule_id (which becomes the output product
# name). ONLY these are rewritten -- never input paths (reference/secondary RSLC,
# orbit, TEC, troposphere, DEM ... s3:// URLs), whose embedded token, if it
# happens to match the CRID, must be preserved verbatim to locate the input.
CRID_OUTPUT_KEYS = ("composite_release_id", "partial_granule_id")


def normalize_crid(runconfig_text: str, allowed_letter: str) -> str:
    """Rewrite the runconfig CRID's leading letter to ``allowed_letter`` if needed.

    Reads composite_release_id from runconfig.groups.primary_executable, validates
    it as <1 capital letter><5 digits>, and if its leading letter is not S/A,
    replaces the token ONLY on the output-naming lines (composite_release_id and
    partial_granule_id -- see CRID_OUTPUT_KEYS). Input paths (reference/secondary
    RSLC, orbit, TEC, troposphere s3:// URLs) are left untouched even if their
    filename embeds the same token -- rewriting them would break input lookup.
    Operates on raw text (line-scoped) to avoid reserializing/reordering the YAML.
    Raises SystemExit on a missing or malformed CRID.
    """
    cfg = yaml.safe_load(runconfig_text) or {}
    try:
        primary = cfg["runconfig"]["groups"]["primary_executable"]
    except (KeyError, TypeError):
        raise SystemExit(
            "cannot normalize CRID: runconfig.groups.primary_executable not found"
        )
    old_crid = primary.get("composite_release_id")

    if not isinstance(old_crid, str) or not CRID_RE.match(old_crid):
        raise SystemExit(
            f"invalid composite_release_id {old_crid!r}: expected 1 capital letter "
            "followed by 5 digits (e.g. S05023). Fix composite_release_id in the "
            "runconfig under groups.primary_executable and resubmit."
        )

    if old_crid[0] in ALLOWED_CRID_LETTERS:
        log(f"composite_release_id {old_crid} already allowed; leaving unchanged")
        return runconfig_text

    new_crid = allowed_letter + old_crid[1:]
    # Line-scoped replace: only rewrite the token on the output-naming key lines,
    # NOT globally, so input s3:// URLs that embed the same token are preserved.
    key_re = re.compile(r"^(\s*)(" + "|".join(CRID_OUTPUT_KEYS) + r")(\s*:\s*)")
    out_lines = []
    count = 0
    for line in runconfig_text.splitlines(keepends=True):
        if key_re.match(line) and old_crid in line:
            count += line.count(old_crid)
            line = line.replace(old_crid, new_crid)
        out_lines.append(line)
    if count == 0:
        log(f"WARNING: composite_release_id {old_crid} not found on any of "
            f"{CRID_OUTPUT_KEYS}; nothing rewritten")
    else:
        log(f"composite_release_id {old_crid} -> {new_crid} "
            f"({count} occurrence(s) replaced on output-naming fields only)")
    return "".join(out_lines)


# --------------------------------------------------------------------------- #
# Product-counter collision check
# --------------------------------------------------------------------------- #
def _partial_granule_id(runconfig_text: str):
    """Return partial_granule_id from a runconfig, or None."""
    cfg = yaml.safe_load(runconfig_text) or {}
    try:
        return cfg["runconfig"]["groups"]["primary_executable"].get("partial_granule_id")
    except (KeyError, TypeError, AttributeError):
        return None


def resolve_product_counter(runconfig_text: str, assume_yes: bool,
                            no_bump: bool, venue: str = None) -> str:
    """Bump the product counter if outputs for this granule family already exist.

    The output granule name ends in a product counter (``..._001``); each rerun
    should increment it (002, 003, ...) so a new submission does not clobber a prior
    product and so a later prefix search matches exactly this job's output. Before
    submitting we list existing counters in S3 for the (CRID-normalized) granule
    prefix:

    - none exist -> submit unchanged.
    - some exist -> compute next = max(existing)+1 and (unless ``assume_yes``)
      prompt for confirmation. Declining, or ``no_bump``, submits at the runconfig's
      current counter (which may clobber / produce ambiguous matches -- warned).

    Returns the (possibly counter-rewritten) runconfig text.
    """
    pgid = _partial_granule_id(runconfig_text)
    if not pgid:
        log("no partial_granule_id in runconfig; skipping product-counter check")
        return runconfig_text

    existing = find_existing_counters(pgid, venue=venue)
    if not existing:
        log("no existing products match this granule prefix; product counter unchanged")
        return runconfig_text

    nxt = max(existing) + 1
    existing_str = ", ".join(f"{c:03d}" for c in existing)
    log(f"found existing product counter(s) [{existing_str}] for this granule prefix")

    if no_bump:
        log("WARNING: --no-counter-bump set; submitting at the runconfig's current "
            "counter -- this may clobber an existing product or match >1 result")
        return runconfig_text

    if not assume_yes:
        prompt = (f"Existing products found. Bump product counter to {nxt:03d} "
                  f"before submitting? [Y/n] ")
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            answer = ""  # non-interactive without --yes -> default to bumping
        if answer in ("n", "no"):
            log("WARNING: not bumping counter (user declined); submitting at the "
                "runconfig's current counter -- may clobber or match >1 result")
            return runconfig_text

    log(f"bumping product counter to {nxt:03d} (product_counter and granule suffix)")
    return set_product_counter(runconfig_text, nxt)


# --------------------------------------------------------------------------- #
# otello
# --------------------------------------------------------------------------- #
def _import_otello():
    if OTELLO_PKG not in sys.path:
        sys.path.insert(0, OTELLO_PKG)
    import otello  # noqa: E402
    return otello


# --------------------------------------------------------------------------- #
# Queue selection from the runconfig workflow
# --------------------------------------------------------------------------- #
def workflow_from_runconfig(cfg: dict) -> str:
    """Return the SAS workflow name (focus/gslc/gcov/sme2/insar) for a runconfig."""
    groups = cfg["runconfig"]["groups"]
    product_type = groups["primary_executable"]["product_type"]
    if product_type in SINGLE_MODULE:
        return SINGLE_MODULE[product_type]
    if set(product_type.split("_")) <= INSAR_CODES:
        return "insar"
    raise SystemExit(f"unsupported product_type {product_type!r} in runconfig")


def queue_for_runconfig(runconfig_text: str, config: dict) -> str:
    """Pick the queue matching the runconfig's workflow.

    Precedence: config `queues:<workflow>` override -> built-in WORKFLOW_QUEUES.
    For INSAR the queue is gpu/cpu-suffixed from the runconfig's
    worker.gpu_enabled flag (matching alos-to-insar).
    """
    cfg = yaml.safe_load(runconfig_text)
    workflow = workflow_from_runconfig(cfg)

    overrides = config.get("queues") or {}
    queue = overrides.get(workflow) or WORKFLOW_QUEUES.get(workflow)
    if queue is None:
        raise SystemExit(f"no queue mapping for workflow {workflow!r}")

    if workflow == "insar":
        gpu = bool(cfg["runconfig"]["groups"].get("worker", {}).get("gpu_enabled"))
        # Only auto-suffix the built-in name; a full override is used verbatim.
        if queue == WORKFLOW_QUEUES["insar"]:
            if gpu:
                queue = f"{queue}-gpu"
            else:
                # Route CPU InSAR to the GCOV queue: the instance type behind
                # nisar-job_worker-sciflo-insar-cpu appears insufficient (memory/
                # cores) for the InSAR SAS, whereas the GCOV queue's instances
                # handle it. Restore the dedicated CPU queue once it is resized.
                #queue = f"{queue}-cpu"
                queue = WORKFLOW_QUEUES["gcov"]
    log(f"workflow={workflow} -> queue={queue}")
    return queue


# --------------------------------------------------------------------------- #
# Build / register via the Mozart PGE endpoint
# --------------------------------------------------------------------------- #
def resolve_and_build(mozart, version: str, allow_insecure_fallback: bool = True) -> dict:
    """Resolve VERSION + build/register the ISCE3 PGE image via the Mozart endpoint.

    The endpoint resolves VERSION (branch/tag/commit) to the ISCE3 hash, forwards
    it to the Jenkins CI machine, and reports registration/build status (or
    triggers a rebuild). Reuses otello's session (its ``verify`` is set by
    ``apply_tls`` in ``main``) so host, auth, and TLS settings are shared.
    Returns the parsed JSON response, which includes `short_hash` and `status`.

    ``allow_insecure_fallback``: when True and no CA bundle was resolved (mode
    "none"), a TLS failure is retried once with ``verify=False`` (legacy behavior,
    logged as a WARNING). When False (a real bundle is in use), a TLS failure is a
    hard error -- we do NOT silently drop verification.
    """
    host = mozart._cfg["host"].rstrip("/")
    url = f"{host}/{PGE_ISCE3_ENDPOINT}"
    log(f"resolving + build/register for VERSION={version} via {url}")
    try:
        resp = mozart._session.post(url, data={"VERSION": version}, timeout=120)
    except requests.exceptions.SSLError as exc:
        if not allow_insecure_fallback:
            raise SystemExit(
                f"TLS verification of {url} failed against the configured CA "
                f"bundle: {exc}. Fix --ca-cert / the endpoint cert; refusing to "
                f"fall back to an unverified connection."
            )
        log(f"WARNING: SSL verification failed ({exc}); retrying INSECURELY with "
            f"verify=False (no CA bundle available)")
        resp = mozart._session.post(
            url, data={"VERSION": version}, timeout=120, verify=False)
    # 200 = already built/registered; 202 = build triggered (Accepted).
    if resp.status_code not in (200, 202):
        raise SystemExit(f"PGE build/register failed [{resp.status_code}]: {resp.text}")
    body = resp.json()
    log(f"status={body.get('status')} triggered={body.get('triggered')} "
        f"hash={body.get('short_hash')}")
    if not body.get("success"):
        raise SystemExit(f"PGE build/register unsuccessful: {body.get('message')}")
    return body


def classify_build(branch: str, verify=None) -> dict:
    """Query the Jenkins build status for ``branch`` ONCE and classify it.

    Uses ``otello.CI.get_build_status()`` (repo=PGE_REPO, branch=<short_hash>),
    which returns a dict with ``building`` (True while actively running) and
    ``result`` (SUCCESS/FAILURE/... once done), mirroring alos-to-insar/pcm.py.
    Collapses that into one of four states the PGE endpoint alone cannot report:

      - "building"   : a build is actively running right now.
      - "built"      : latest build finished with result SUCCESS.
      - "failed"     : latest build finished with a non-SUCCESS result.
      - "not_built"  : no build status available yet (never built / just registered).

    ``verify``: CA bundle path applied to the CI session for real TLS verification
    (CI does not accept ssl_verify in its constructor, so we set it after).

    Returns ``{"state": <one of the above>, "result": <str>, "building": <bool>,
    "url": <str>, "raw": <dict|None>}``. Never raises for a missing build (that is
    the ``not_built`` state); only unexpected import failures propagate.
    """
    otello = _import_otello()
    ci = otello.CI(repo=PGE_REPO, branch=branch)
    apply_tls(ci, verify)
    try:
        status = ci.get_build_status()
    except Exception as exc:  # noqa: BLE001 -- no build yet / transient
        log(f"no build status available for branch {branch} ({exc})")
        return {"state": "not_built", "result": "", "building": False,
                "url": "", "raw": None}

    result = str(status.get("result") or "").upper()
    building = bool(status.get("building"))
    url = status.get("url") or status.get("build_url") or ""
    if building:
        state = "building"
    elif result in BUILD_SUCCESS_RESULTS:
        state = "built"
    elif result:
        state = "failed"
    else:
        state = "not_built"
    return {"state": state, "result": result, "building": building,
            "url": url, "raw": status}


def report_build_status(branch: str, verify=None) -> int:
    """Print the current Jenkins build state for ``branch`` once and return an exit code.

    Exit codes (handy for scripting): 0 built, 1 failed, 2 building, 3 not_built.
    """
    info = classify_build(branch, verify=verify)
    state = info["state"]
    detail = f"result={info['result'] or '(none)'} building={info['building']}"
    if info["url"]:
        detail += f" url={info['url']}"
    log(f"image build status for branch {branch}: {state.upper()} ({detail})")
    return {"built": 0, "failed": 1, "building": 2, "not_built": 3}[state]


def watch_build(branch: str, poll_seconds: int = BUILD_POLL_SECONDS, verify=None) -> None:
    """Block until the Jenkins image build for ``branch`` reaches a terminal state.

    Polls ``classify_build`` every ``poll_seconds``:
      - "building" / "not_built" -> keep polling.
      - "built"                  -> return.
      - "failed"                 -> raise SystemExit (no infinite loop).

    ``verify``: CA bundle path forwarded to ``classify_build`` for TLS verification.

    Raises SystemExit if the build fails.
    """
    log(f"watching Jenkins build for branch {branch} (poll every {poll_seconds}s)...")
    while True:
        info = classify_build(branch, verify=verify)
        state = info["state"]
        log(f"build status: {state} "
            f"(result={info['result'] or '(none)'} building={info['building']})")
        if state == "built":
            log(f"image build SUCCESS for branch {branch}")
            return
        if state == "failed":
            url = info["url"]
            raise SystemExit(
                f"image build for branch {branch} did not succeed "
                f"(result={info['result'] or 'unknown'}). {('See ' + url) if url else ''}"
            )
        time.sleep(poll_seconds)


# --------------------------------------------------------------------------- #
# Mozart submission
# --------------------------------------------------------------------------- #
def resolve_mozart_pvt_ip() -> str:
    """Read the Mozart private IP from the on-demand config object in S3.

    Tries each venue bucket in ``MOZART_CONFIG_VENUES`` order
    (s3://nisar-<venue>-cc-ondemand/<MOZART_CONFIG_KEY>) and returns the first
    ``MOZART_PVT_IP`` found. Returns "" if none is readable (submit still works;
    the notebook just skips the DAAC credential fetch). Never raises.
    """
    import json

    try:
        import boto3
    except Exception as exc:  # noqa: BLE001
        log(f"WARNING: boto3 unavailable, cannot read Mozart config from S3: {exc}")
        return ""

    s3 = boto3.client("s3")
    for venue in MOZART_CONFIG_VENUES:
        bucket = f"nisar-{venue}-cc-ondemand"
        try:
            obj = s3.get_object(Bucket=bucket, Key=MOZART_CONFIG_KEY)
            body = json.loads(obj["Body"].read())
            ip = (body.get("MOZART_PVT_IP") or "").strip()
            if ip:
                log(f"resolved mozart_pvt_ip={ip} from s3://{bucket}/{MOZART_CONFIG_KEY}")
                return ip
            log(f"WARNING: s3://{bucket}/{MOZART_CONFIG_KEY} has no MOZART_PVT_IP")
        except Exception as exc:  # noqa: BLE001 -- 403/404/parse: try next venue
            log(f"mozart config not available at s3://{bucket}/{MOZART_CONFIG_KEY}: {exc}")
    log("WARNING: could not resolve mozart_pvt_ip from any venue; "
        "worker will skip the DAAC credential fetch")
    return ""


def submit_job(mozart, branch: str, runconfig_text: str, netrc_text: str,
               explicit_queue: str, config: dict, priority: int, wait: bool,
               mozart_pvt_ip: str = None, version: str = None,
               runconfig_path: str = None):
    """Submit job-run_isce3:<branch> to Mozart with an inline runconfig.

    Queue precedence: --queue (explicit_queue) -> workflow-matched queue derived
    from the runconfig. The workflow queues always exist (guaranteed by the
    deployment); Mozart only *lists* queues it has exercised at least once, so we
    do not validate against that list -- submitting exercises the queue.

    ``mozart_pvt_ip`` is passed to the worker so it can mint short-term DAAC S3
    credentials directly from the Mozart gunicorn API on :8888 (bypassing the
    httpd basic-auth proxy). It has no public internet, so this is how it gets
    DAAC access. Empty -> the notebook skips the credential fetch.

    Every submission is given a readable ``tag`` (nisar-isce3-<product>-<timestamp>)
    and a local record JSON so it can be found later with check_isce3_job.py.
    """
    job_name = f"job-run_isce3:{branch}"
    log(f"getting job type {job_name}")
    jt = mozart.get_job_type(job_name)
    jt.initialize()

    cfg = yaml.safe_load(runconfig_text) or {}
    queue = explicit_queue or queue_for_runconfig(runconfig_text, config)

    # --mozart-pvt-ip wins; otherwise read the default from the S3 config object.
    mozart_pvt_ip = (mozart_pvt_ip or "").strip() or resolve_mozart_pvt_ip()
    log(f"mozart DAAC-creds callback: pvt_ip={mozart_pvt_ip or '(none)'}")

    jt.set_input_params({
        "runconfig_s3": runconfig_text,     # inline YAML text (see notebook write-cell)
        "netrc_content": netrc_text,        # earthdata creds for DAAC S3 access
        "output_dir": "output",
        "scratch_dir": "scratch",
        "localized_runconfig": "runconfig_localized.yaml",
        "mozart_pvt_ip": mozart_pvt_ip,     # worker mints DAAC creds via :8888
    })

    tag = make_tag(product_name_from_cfg(cfg))
    log(f"submitting to queue {queue} (priority {priority}) tag={tag}")
    job = jt.submit_job(queue=queue, priority=priority, tag=tag)
    job_id = getattr(job, "job_id", None) or getattr(job, "_id", None)
    log(f"submitted. job id: {job_id}")

    # Persist a record so the job can be found again by check_isce3_job.py.
    try:
        primary = cfg.get("runconfig", {}).get("groups", {}).get("primary_executable", {})
    except AttributeError:
        primary = {}
    record = {
        "tag": tag,
        "job_id": job_id,
        "product_type": primary.get("product_type"),
        "composite_release_id": primary.get("composite_release_id"),
        "partial_granule_id": primary.get("partial_granule_id"),
        "queue": queue,
        "branch": branch,
        "version": version,
        "priority": priority,
        "runconfig_path": os.path.abspath(runconfig_path) if runconfig_path else None,
        "host": mozart._cfg.get("host"),
        "username": mozart._cfg.get("username"),
    }
    record_file = write_record(record)

    # Summary block: everything needed to track the job later, in one place.
    check = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_isce3_job.py")
    log("=" * 70)
    log("SUBMITTED")
    log(f"  tag        : {tag}")
    log(f"  job id     : {job_id}")
    log(f"  queue      : {queue}")
    log(f"  record     : {record_file}")
    log(f"  check with : python3 {check} --tag {tag}")
    log("=" * 70)

    if wait:
        log("waiting for job completion...")
        status = job.wait_for_completion()
        log(f"job finished: {status}")
    return job


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="optional submit config YAML (queue override)")
    parser.add_argument("--version", default=DEFAULT_VERSION,
                        help=f"ISCE3 branch/tag/commit to run (default: {DEFAULT_VERSION})")
    parser.add_argument("--runconfig", default=DEFAULT_RUNCONFIG,
                        help="runconfig YAML to submit (default: GUNW example)")
    parser.add_argument("--netrc", default=DEFAULT_NETRC,
                        help="netrc file with earthdata creds (default: ~/.netrc)")
    parser.add_argument("--queue",
                        help="Mozart queue (default: auto-selected from the "
                             "runconfig workflow, e.g. GUNW/INSAR -> "
                             "nisar-job_worker-sciflo-insar-{gpu,cpu})")
    parser.add_argument("--mozart-pvt-ip",
                        help="Mozart private IP the worker calls to mint DAAC S3 "
                             "creds, via https://<ip>:8888/api/v0.1/daac/s3credentials "
                             "(the worker has no public internet). Default: read "
                             "MOZART_PVT_IP from s3://nisar-{st,adt}-cc-ondemand/"
                             + MOZART_CONFIG_KEY + ".")
    parser.add_argument("--crid-letter", choices=list(ALLOWED_CRID_LETTERS),
                        default="S",
                        help="allowed leading letter for the composite release ID; "
                             "a runconfig CRID starting with any other letter (e.g. "
                             "P/X) is rewritten to this before submission, including "
                             "in the output product name (default: S)")
    parser.add_argument("--priority", type=int, default=1,
                        help="Mozart job priority 1-9 (default: 1)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="auto-confirm bumping the product counter when existing "
                             "products are found (no interactive prompt)")
    parser.add_argument("--no-counter-bump", action="store_true",
                        help="never bump the product counter, even if products for "
                             "this granule already exist (may clobber / match >1)")
    parser.add_argument("--venue",
                        help="limit the pre-submit product-collision search to one "
                             "venue (e.g. st, adt); default: probe all")
    parser.add_argument("--wait", action="store_true",
                        help="wait for the submitted Mozart job to complete")
    parser.add_argument("--only-build", action="store_true",
                        help="resolve + build/register the image, then exit "
                             "(do not submit a Mozart job)")
    parser.add_argument("--watch-build", action="store_true",
                        help="block until the Jenkins image build finishes, polling "
                             f"the real build status every {BUILD_POLL_SECONDS}s "
                             "(distinguishes running / success / failure); fails "
                             "if the build fails; then continue (or exit if "
                             "--only-build)")
    parser.add_argument("--build-status", action="store_true",
                        help="report the current Jenkins image build state ONCE "
                             "(building / built / failed / not_built) and exit "
                             "without submitting; exit code 0=built 1=failed "
                             "2=building 3=not_built")
    parser.add_argument("--skip-build", action="store_true",
                        help="skip the build/register endpoint call; requires --branch")
    parser.add_argument("--branch",
                        help="job branch/short-hash to submit against when --skip-build "
                             "is set (bypasses version resolution)")
    parser.add_argument("--ca-cert",
                        help="CA bundle (PEM) for TLS verification of the Mozart / "
                             "Jenkins endpoints. If omitted, uses REQUESTS_CA_BUNDLE/"
                             "SSL_CERT_FILE, else auto-downloads the server's full "
                             "cert chain and verifies against it. With --ca-cert a "
                             "TLS failure is a hard error (no insecure fallback).")
    parser.add_argument("--no-verify", action="store_true",
                        help="force INSECURE requests (verify=False), disabling all "
                             "TLS verification. Not recommended; exposes credentials.")
    args = parser.parse_args()

    cfg = load_config(args.config)

    otello = _import_otello()
    mozart = otello.Mozart()

    # Resolve the TLS verify value ONCE (explicit --ca-cert > env bundle >
    # auto-downloaded full chain > none) and apply it to the shared otello session.
    # mode "none" means nothing usable was resolved -> insecure fallback is allowed
    # in resolve_and_build; any real bundle makes TLS failures a hard error.
    verify, tls_mode = resolve_verify(
        args.ca_cert, mozart._cfg.get("host", ""),
        no_verify=args.no_verify, log=log)
    log(f"TLS verify mode: {tls_mode}")
    apply_tls(mozart, verify)
    allow_insecure_fallback = tls_mode in ("none", "forced_insecure")

    # One-shot build-status probe: resolve the branch (short-hash) then report the
    # current Jenkins state once and exit, without submitting a job. Uses --branch
    # verbatim if given; otherwise resolves VERSION via the endpoint (which also
    # registers/triggers a build if none exists).
    if args.build_status:
        if args.branch:
            branch = args.branch
        else:
            info = resolve_and_build(mozart, args.version,
                                     allow_insecure_fallback=allow_insecure_fallback)
            branch = info.get("short_hash")
            if not branch:
                raise SystemExit(f"endpoint returned no short_hash: {info}")
        return report_build_status(branch, verify=verify)

    # 1. Resolve VERSION + ensure the image is built / CI job registered.
    if args.skip_build:
        if not args.branch:
            raise SystemExit("--skip-build requires --branch <short-hash>")
        branch = args.branch
    else:
        info = resolve_and_build(mozart, args.version,
                                 allow_insecure_fallback=allow_insecure_fallback)
        branch = info.get("short_hash")
        if not branch:
            raise SystemExit(f"endpoint returned no short_hash: {info}")
        already = info.get("status") == "already_built"
        if args.watch_build and not already:
            # Endpoint triggered/registered the build; block on the real Jenkins
            # status (running -> success/failure) instead of re-polling the
            # endpoint's coarse built/not-built flag.
            watch_build(branch, verify=verify)
            already = True
        if args.only_build:
            log(f"--only-build set; version {args.version} -> {branch} "
                f"({'already_built' if already else info.get('status')}); done")
            return 0
        if not already:
            raise SystemExit(
                f"image for {args.version} ({branch}) not ready yet "
                f"(status={info.get('status')}). Re-run once the build completes, "
                f"use --watch-build to block until it finishes, or "
                f"--skip-build --branch {branch} to submit anyway."
            )

    # 2. Submit the PGE job with the runconfig inline.
    with open(args.runconfig) as f:
        runconfig_text = f.read()
    log(f"runconfig: {args.runconfig} ({len(runconfig_text)} bytes)")
    runconfig_text = normalize_crid(runconfig_text, args.crid_letter)

    # Bump the product counter if outputs for this granule already exist, so this
    # submission neither clobbers a prior product nor makes a later prefix search
    # match more than one result. Runs on the CRID-normalized text.
    runconfig_text = resolve_product_counter(
        runconfig_text, assume_yes=args.yes, no_bump=args.no_counter_bump,
        venue=args.venue)

    netrc_text = ""
    if os.path.exists(args.netrc):
        with open(args.netrc) as f:
            netrc_text = f.read()
        log(f"netrc: {args.netrc} ({len(netrc_text)} bytes)")
    else:
        log(f"netrc not found at {args.netrc}; submitting empty netrc_content")

    submit_job(mozart, branch, runconfig_text, netrc_text,
               args.queue, cfg, args.priority, args.wait,
               mozart_pvt_ip=args.mozart_pvt_ip,
               version=args.version, runconfig_path=args.runconfig)
    return 0


if __name__ == "__main__":
    sys.exit(main())
