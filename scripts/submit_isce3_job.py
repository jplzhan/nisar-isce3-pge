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
"""

import argparse
import os
import sys

import yaml

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

# Default Mozart private IP is read from an on-demand config object in S3. The
# bucket is venue-suffixed; try "st" first, then "adt". The object is JSON with a
# "MOZART_PVT_IP" key. Overridden by --mozart-pvt-ip.
MOZART_CONFIG_VENUES = ("st", "adt")
MOZART_CONFIG_KEY = "ondemand-test/mozart_config.json"


def log(msg: str) -> None:
    print(f"[submit_isce3_job] {msg}", flush=True)


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
def resolve_and_build(mozart, version: str) -> dict:
    """Resolve VERSION + build/register the ISCE3 PGE image via the Mozart endpoint.

    The endpoint resolves VERSION (branch/tag/commit) to the ISCE3 hash, forwards
    it to the Jenkins CI machine, and reports registration/build status (or
    triggers a rebuild). Reuses otello's session so host, auth, and TLS settings
    come from ~/.config/otello/config.yml. Returns the parsed JSON response, which
    includes `short_hash` (the branch of the registered job) and `status`.
    """
    host = mozart._cfg["host"].rstrip("/")
    url = f"{host}/{PGE_ISCE3_ENDPOINT}"
    log(f"resolving + build/register for VERSION={version} via {url}")
    resp = mozart._session.post(url, data={"VERSION": version}, timeout=120)
    # 200 = already built/registered; 202 = build triggered (Accepted).
    if resp.status_code not in (200, 202):
        raise SystemExit(f"PGE build/register failed [{resp.status_code}]: {resp.text}")
    body = resp.json()
    log(f"status={body.get('status')} triggered={body.get('triggered')} "
        f"hash={body.get('short_hash')}")
    if not body.get("success"):
        raise SystemExit(f"PGE build/register unsuccessful: {body.get('message')}")
    return body


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
               mozart_pvt_ip: str = None):
    """Submit job-run_isce3:<branch> to Mozart with an inline runconfig.

    Queue precedence: --queue (explicit_queue) -> workflow-matched queue derived
    from the runconfig. The workflow queues always exist (guaranteed by the
    deployment); Mozart only *lists* queues it has exercised at least once, so we
    do not validate against that list -- submitting exercises the queue.

    ``mozart_pvt_ip`` is passed to the worker so it can mint short-term DAAC S3
    credentials directly from the Mozart gunicorn API on :8888 (bypassing the
    httpd basic-auth proxy). It has no public internet, so this is how it gets
    DAAC access. Empty -> the notebook skips the credential fetch.
    """
    job_name = f"job-run_isce3:{branch}"
    log(f"getting job type {job_name}")
    jt = mozart.get_job_type(job_name)
    jt.initialize()

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

    log(f"submitting to queue {queue} (priority {priority})")
    job = jt.submit_job(queue=queue, priority=priority)
    job_id = getattr(job, "job_id", None) or getattr(job, "_id", None)
    log(f"submitted. job id: {job_id}")

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
    parser.add_argument("--priority", type=int, default=1,
                        help="Mozart job priority 1-9 (default: 1)")
    parser.add_argument("--wait", action="store_true",
                        help="wait for the submitted Mozart job to complete")
    parser.add_argument("--only-build", action="store_true",
                        help="resolve + build/register the image, then exit "
                             "(do not submit a Mozart job)")
    parser.add_argument("--skip-build", action="store_true",
                        help="skip the build/register endpoint call; requires --branch")
    parser.add_argument("--branch",
                        help="job branch/short-hash to submit against when --skip-build "
                             "is set (bypasses version resolution)")
    args = parser.parse_args()

    cfg = load_config(args.config)

    otello = _import_otello()
    mozart = otello.Mozart()

    # 1. Resolve VERSION + ensure the image is built / CI job registered.
    if args.skip_build:
        if not args.branch:
            raise SystemExit("--skip-build requires --branch <short-hash>")
        branch = args.branch
    else:
        info = resolve_and_build(mozart, args.version)
        branch = info.get("short_hash")
        if not branch:
            raise SystemExit(f"endpoint returned no short_hash: {info}")
        if args.only_build:
            log(f"--only-build set; version {args.version} -> {branch} "
                f"({info.get('status')}); done")
            return 0
        if info.get("status") != "already_built":
            raise SystemExit(
                f"image for {args.version} ({branch}) not ready yet "
                f"(status={info.get('status')}). Re-run once the build completes, "
                f"or use --skip-build --branch {branch} to submit anyway."
            )

    # 2. Submit the PGE job with the runconfig inline.
    with open(args.runconfig) as f:
        runconfig_text = f.read()
    log(f"runconfig: {args.runconfig} ({len(runconfig_text)} bytes)")

    netrc_text = ""
    if os.path.exists(args.netrc):
        with open(args.netrc) as f:
            netrc_text = f.read()
        log(f"netrc: {args.netrc} ({len(netrc_text)} bytes)")
    else:
        log(f"netrc not found at {args.netrc}; submitting empty netrc_content")

    submit_job(mozart, branch, runconfig_text, netrc_text,
               args.queue, cfg, args.priority, args.wait,
               mozart_pvt_ip=args.mozart_pvt_ip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
