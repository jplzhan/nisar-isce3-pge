# Submitting ISCE3 PGE Jobs

`scripts/submit_isce3_job.py` submits a NISAR ISCE3 PGE job to Mozart in one
step. You give it a **runconfig** and an ISCE3 **version** (a branch, tag, or
commit — e.g. `develop`); it does everything else:

1. Calls the Mozart endpoint `/mozart/api/v0.1/pge/isce3` with your version. That
   endpoint resolves the version to the ISCE3 commit hash, forwards it to the
   Jenkins CI machine, and reports whether the image is already built + the job
   registered (or triggers a build). It returns the resolved short hash.
2. Submits `job-run_isce3:<short_hash>` to Mozart, passing your runconfig inline
   as YAML text and your `~/.netrc` (for DAAC S3 access).

There is nothing else to configure per-run: host, credentials, and TLS all come
from otello's own config.

## One-time setup

You need a working otello config at `~/.config/otello/config.yml` (host,
username, auth). This is the same config otello uses everywhere; if you can run
otello you already have it. Verify:

```bash
cat ~/.config/otello/config.yml     # should show host + username
```

`~/.netrc` should hold your Earthdata login so the worker can fetch the `s3://`
inputs referenced in the runconfig:

```
machine urs.earthdata.nasa.gov
    login <your-earthdata-user>
    password <your-earthdata-pass>
```

No Jenkins credentials, tokens, or extra config files are required.

## Basic usage

Submit the default GUNW example runconfig on `develop`:

```bash
python3 scripts/submit_isce3_job.py
```

Specify your own runconfig and version — the two things you normally change:

```bash
python3 scripts/submit_isce3_job.py \
    --runconfig /path/to/my_runconfig.yaml \
    --version develop
```

`--version` accepts any ISCE3 ref the endpoint can resolve: a branch
(`develop`), a tag (`v0.20.0`), or a commit hash. The runconfig can be any NISAR
runconfig YAML whose `input_file_group` / `dynamic_ancillary_file_group` entries
are `s3://` links; the PGE localizes them and stages the DEM/water mask itself.

On success you get a Mozart job id:

```
[submit_isce3_job] status=already_built triggered=False hash=23f99329d715
[submit_isce3_job] runconfig: /path/to/my_runconfig.yaml (10807 bytes)
[submit_isce3_job] submitting to queue factotum-job_worker-small (priority 1)
[submit_isce3_job] submitted. job id: d1df4ce0-1f01-418e-9d3f-85bc03e5e0e9
```

Track that id in Mozart / Figaro.

## Common flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--version VERSION` | `develop` | ISCE3 branch / tag / commit to run. |
| `--runconfig PATH` | GUNW example | Runconfig YAML to submit (sent inline). |
| `--netrc PATH` | `~/.netrc` | Earthdata creds file for DAAC S3 access. |
| `--queue NAME` | auto (from runconfig) | Mozart queue. Overrides auto-selection. |
| `--priority N` | `1` | Job priority 1–9. |
| `--wait` | off | Block until the submitted job finishes. |
| `--only-build` | off | Resolve + build/register the image, then exit (no job submitted). |
| `--skip-build --branch HASH` | off | Skip the resolve/build step and submit directly against a known short hash. |

## Typical scenarios

**Run a job and wait for it:**

```bash
python3 scripts/submit_isce3_job.py --runconfig my.yaml --version develop --wait
```

**Just kick off / warm the image build for a version** (e.g. a fresh branch),
then submit later:

```bash
python3 scripts/submit_isce3_job.py --version my-feature-branch --only-build
```

If the image is not built yet, the endpoint triggers a build and the script
stops with the resolved hash. Re-run the same command once the build finishes;
it will report `already_built` and proceed. (Re-running is always safe — the
endpoint is idempotent.)

**Submit against a hash directly**, skipping resolution (useful if you already
know the short hash and the image is built):

```bash
python3 scripts/submit_isce3_job.py --skip-build --branch 23f99329d715 --runconfig my.yaml
```

## Queue selection

The ISCE3 notebook can run every workflow, so the script picks the queue that
matches the runconfig — the same convention as alos-to-insar (RSLC jobs to the
RSLC queue, GCOV to the GCOV queue, etc.). The workflow is read from the
runconfig's `primary_executable.product_type`:

| product_type | workflow | queue |
|--------------|----------|-------|
| `RSLC` | focus | `nisar-job_worker-sciflo-rslc` |
| `GSLC` | gslc | `nisar-job_worker-sciflo-gslc` |
| `GCOV` | gcov | `nisar-job_worker-sciflo-gcov` |
| `SME2` | sme2 | `nisar-job_worker-sciflo-sme2` |
| `RIFG`/`RUNW`/`ROFF`/`GUNW`/`GOFF` (or combos like `RIFG_RUNW_GUNW`) | insar | `nisar-job_worker-sciflo-insar-{gpu,cpu}` |

For INSAR the `-gpu`/`-cpu` suffix is chosen from the runconfig's
`worker.gpu_enabled` flag. You do not set the queue yourself — it follows from
the runconfig.

Overrides:

- `--queue NAME` forces a specific queue for this run.
- A `queues:` mapping in the config file overrides per-workflow (see below).

These workflow queues always exist (guaranteed by the deployment). Mozart's
queue-list query only reports queues that have been exercised at least once, so a
freshly deployed queue may not appear there yet — the script submits to the
mapped queue regardless, which exercises it.

## How the runconfig is delivered

The runconfig is passed **inline** as YAML text (the `runconfig_s3` job param is
a HySDS `context` value, so the submitted string is stored verbatim). The PGE
notebook writes it to a file on the worker, then localizes the `s3://` inputs and
regenerates the DEM/water mask from the runconfig geometry. You therefore do not
need to upload the runconfig anywhere — just point `--runconfig` at a local file.

## Optional: default queue override

Host/auth/TLS come from `~/.config/otello/config.yml`. The only thing you can
override with a separate config is the default queue. Copy the example if you
want a non-default queue without passing `--queue` each time:

```bash
mkdir -p ~/.config/nisar-isce3-submit
cp config/submit_config.example.yml ~/.config/nisar-isce3-submit/config.yml
# edit `queue:` in that file
```

Resolution order: `--config <path>` → `$NISAR_ISCE3_SUBMIT_CONFIG` →
`~/.config/nisar-isce3-submit/config.yml`. A missing file is fine.
