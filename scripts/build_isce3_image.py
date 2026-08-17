#!/usr/bin/env python3
"""Build a NISAR ISCE3 PGE docker image pinned to a specific ISCE3 commit.

Intended to run as a parameterized Jenkins job on the same Mozart/Jenkins machine
that will register and build the resulting CI job (via otello at ~/otello).

Flow
----
1. Resolve the requested ISCE3 ref (tag / branch / short or full hash) to its unique
   full commit hash on github.com/isce-framework/isce3.
2. If a tag of THIS PGE repo already has that hash as its name, the image already
   exists -> report and exit 0 (nothing to do).
3. Otherwise, on a branch named after the ISCE3 hash, rewrite docker/Dockerfile's
   `--commit-hash <hash>` line (based on master) and push the branch to origin.
   If the branch already exists on the remote, reuse it as-is (no re-push).
4. Register the CI job with otello only if it is not already registered, and submit
   a build only if the branch has not already had a successful build.

Auth
----
GitHub token is read from an environment variable (default GITHUB_TOKEN, override with
--token-env). The PGE repo remote host (github.com vs github.jpl.nasa.gov) is detected
from --pge-repo and the token is embedded in the push URL accordingly.

otello reads its own config from ~/.config/otello/config.yml (host / username / auth).
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlparse, urlunparse

ISCE3_REPO = "https://github.com/isce-framework/isce3.git"
DEFAULT_PGE_REPO = "https://github.jpl.nasa.gov/NISAR-ODS/nisar-isce3-pge.git"
DOCKERFILE_REL = "docker/Dockerfile"
# Matches the pinned ISCE3 commit in the Dockerfile, e.g.
#   --commit-hash 3383442fee8d2003f5066fffe37bda9de63a8ed2 \
COMMIT_HASH_RE = re.compile(r"(--commit-hash\s+)([0-9a-fA-F]{7,40})")

# Jenkins build results that count as "already successfully built".
SUCCESS_RESULTS = {"SUCCESS"}


def log(msg: str) -> None:
    print(f"[build_isce3_image] {msg}", flush=True)


def _redact(cmd):
    """Hide any embedded token (user:token@host) before logging a command."""
    return [re.sub(r"://[^@/]+@", "://***@", part) for part in cmd]


def run(cmd, cwd=None, capture=True, check=True):
    """Run a command (list form, no shell). Returns stdout stripped when capture=True."""
    log("$ " + " ".join(_redact(cmd)))
    res = subprocess.run(
        cmd, cwd=cwd, check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )
    return res.stdout.strip() if capture and res.stdout is not None else ""


# --------------------------------------------------------------------------- #
# ISCE3 ref -> commit hash
# --------------------------------------------------------------------------- #
def resolve_isce3_commit(ref: str) -> str:
    """Translate a tag / branch / (short|full) commit of ISCE3 to its full commit hash.

    Uses a shallow bare mirror so this works for tags, branches, and arbitrary commits
    without a full clone.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "isce3.git")
        # Full clone is the robust way to resolve an arbitrary commit hash (not just
        # a ref): ls-remote only sees refs, so a bare commit wouldn't resolve.
        run(["git", "clone", "--bare", "--filter=blob:none", ISCE3_REPO, repo])
        # Try as a tree-ish (tag/branch/hash) -> full commit hash.
        try:
            full = run(["git", "-C", repo, "rev-parse", "--verify", f"{ref}^{{commit}}"])
        except subprocess.CalledProcessError:
            # Maybe a remote branch that rev-parse can't see directly.
            full = run(["git", "-C", repo, "rev-parse", "--verify",
                        f"refs/remotes/origin/{ref}^{{commit}}"], check=True)
        if not re.fullmatch(r"[0-9a-f]{40}", full):
            raise ValueError(f"could not resolve ISCE3 ref {ref!r} to a commit hash (got {full!r})")
        return full


# --------------------------------------------------------------------------- #
# remote helpers
# --------------------------------------------------------------------------- #
def remote_tag_exists(repo_url: str, tag: str) -> bool:
    """True if a tag named exactly `tag` exists on the PGE remote."""
    out = run(["git", "ls-remote", "--tags", repo_url, f"refs/tags/{tag}"], check=False)
    return bool(out.strip())


def remote_branch_exists(repo_url: str, branch: str) -> bool:
    out = run(["git", "ls-remote", "--heads", repo_url, f"refs/heads/{branch}"], check=False)
    return bool(out.strip())


def authed_url(repo_url: str, token: str) -> str:
    """Embed the token in an https git URL. Works for github.com and GHE hosts."""
    parts = urlparse(repo_url)
    if parts.scheme != "https":
        raise ValueError(f"expected https repo URL, got {repo_url!r}")
    # x-access-token is accepted by github.com and GitHub Enterprise.
    netloc = f"x-access-token:{token}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunparse((parts.scheme, netloc, parts.path, "", "", ""))


# --------------------------------------------------------------------------- #
# Dockerfile edit + branch push
# --------------------------------------------------------------------------- #
def prepare_and_push_branch(repo_url: str, token: str, branch: str, isce3_hash: str,
                            git_user_name: str, git_user_email: str) -> None:
    """Clone master, rewrite the Dockerfile commit-hash, commit on `branch`, push."""
    push_url = authed_url(repo_url, token)
    with tempfile.TemporaryDirectory() as tmp:
        work = os.path.join(tmp, "pge")
        run(["git", "clone", "--branch", "master", push_url, work])
        run(["git", "-C", work, "config", "user.name", git_user_name])
        run(["git", "-C", work, "config", "user.email", git_user_email])
        run(["git", "-C", work, "checkout", "-b", branch])

        dockerfile = os.path.join(work, DOCKERFILE_REL)
        with open(dockerfile) as f:
            content = f.read()
        new_content, n = COMMIT_HASH_RE.subn(rf"\g<1>{isce3_hash}", content)
        if n == 0:
            raise RuntimeError(f"no --commit-hash line found in {DOCKERFILE_REL}")
        if n > 1:
            raise RuntimeError(f"multiple --commit-hash lines in {DOCKERFILE_REL}; refusing to guess")
        if new_content == content:
            log("Dockerfile already pins this commit; committing anyway to create the branch")
        with open(dockerfile, "w") as f:
            f.write(new_content)

        run(["git", "-C", work, "add", DOCKERFILE_REL])
        run(["git", "-C", work, "commit", "-m",
             f"Pin ISCE3 to {isce3_hash}"])
        # branch name == hash; do not force (branch reuse is handled by the caller).
        run(["git", "-C", work, "push", "origin", branch])
    log(f"pushed branch {branch}")


# --------------------------------------------------------------------------- #
# otello register + build
# --------------------------------------------------------------------------- #
def build_already_successful(ci) -> bool:
    """Best-effort check whether the branch already has a successful Jenkins build."""
    try:
        status = ci.get_build_status()
    except Exception as e:  # no build yet / job just registered
        log(f"no build status available ({e}); treating as not-yet-built")
        return False
    # Jenkins-style payloads vary; check the common fields defensively.
    result = str(status.get("result") or status.get("status") or "").upper()
    building = status.get("building")
    log(f"latest build status: result={result!r} building={building!r}")
    if building:
        return False
    return result in SUCCESS_RESULTS


def register_and_build(repo_url: str, branch: str) -> None:
    # Import here so the git-only path (tag exists) doesn't require otello installed.
    otello_pkg = os.path.expanduser("~/otello")
    if otello_pkg not in sys.path:
        sys.path.insert(0, otello_pkg)
    from otello import CI

    ci = CI(repo=repo_url, branch=branch)

    if ci.check_job_exists():
        log("CI job already registered; skipping register()")
    else:
        log("registering CI job")
        ci.register()

    if build_already_successful(ci):
        log("branch already has a successful build; skipping submit_build()")
        return

    log("submitting build")
    result = ci.submit_build()
    log(f"submit_build result: {result}")


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ref",
                        help="ISCE3 tag, branch, or commit hash to build an image for")
    parser.add_argument("--pge-repo", default=DEFAULT_PGE_REPO,
                        help=f"PGE repo https URL (default: {DEFAULT_PGE_REPO})")
    parser.add_argument("--token-env", default="GITHUB_TOKEN",
                        help="env var holding the GitHub token (default: GITHUB_TOKEN)")
    parser.add_argument("--git-user-name", default="jenkins",
                        help="git author/committer name for the pushed commit")
    parser.add_argument("--git-user-email", default="jenkins@jpl.nasa.gov",
                        help="git author/committer email for the pushed commit")
    parser.add_argument("--skip-build", action="store_true",
                        help="push the branch but do not register/build via otello")
    args = parser.parse_args()

    token = os.environ.get(args.token_env)
    if not token:
        log(f"ERROR: token env var {args.token_env!r} is empty or unset")
        return 2

    # 1. ISCE3 ref -> unique commit hash. Branch/tag name == this hash.
    isce3_hash = resolve_isce3_commit(args.ref)
    log(f"resolved ISCE3 ref {args.ref!r} -> {isce3_hash}")
    branch = isce3_hash

    # 2. If a tag of the PGE repo already carries this hash as its name, the image
    #    for this commit already exists -> nothing to do.
    if remote_tag_exists(args.pge_repo, isce3_hash):
        log(f"tag {isce3_hash} already exists on {args.pge_repo}; image already built. Done.")
        return 0

    # 3. Prepare + push the branch (reuse if it already exists on the remote).
    if remote_branch_exists(args.pge_repo, branch):
        log(f"branch {branch} already exists on remote; reusing without re-push")
    else:
        prepare_and_push_branch(args.pge_repo, token, branch, isce3_hash,
                                args.git_user_name, args.git_user_email)

    # 4. Register + build via otello (idempotent on both).
    if args.skip_build:
        log("--skip-build set; not registering/building")
        return 0
    register_and_build(args.pge_repo, branch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
