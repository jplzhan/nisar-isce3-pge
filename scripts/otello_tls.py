#!/usr/bin/env python3
"""Proper TLS verification for otello (HySDS/Mozart) calls, without patching otello.

Problem
-------
otello disables SSL verification by default (``Base.__init__`` sets
``self._session.verify = ssl_verify`` with ``ssl_verify`` defaulting to ``False``,
see ~/otello/otello/base.py). Talking to Mozart/Jenkins over that session accepts
any certificate, exposing the HySDS Basic-Auth credentials to a MITM.

requests' ``verify`` accepts a *CA bundle path*, not only a bool -- so we can turn
on real verification by handing the shared otello session a trust bundle. Rather
than requiring an operator to pre-distribute a CA file for the self-signed internal
cluster, this module can auto-download the server's *complete* certificate chain
and use that as the trust bundle.

Trust model (honest caveat)
---------------------------
Auto-downloading the chain from the same host we then talk to is trust-on-first-use:
it defeats a *passive* eavesdropper and catches accidental cert mismatches, but it
does NOT stop an *active* MITM present at download time. For strong guarantees pass
an out-of-band ``--ca-cert``; that path is strict (no insecure fallback).

Resolution priority (see ``resolve_verify``)
-------------------------------------------
1. explicit ``--ca-cert PATH``          (mode "explicit", strict)
2. env REQUESTS_CA_BUNDLE / SSL_CERT_FILE (mode "env", strict)
3. auto-downloaded full chain            (mode "autochain")
4. nothing usable                        (mode "none" -> caller may fall back to
                                          insecure verify=False)

Environment
-----------
Python 3.12 here lacks ``ssl.SSLSocket.get_unverified_chain`` (3.13+), and neither
``cryptography`` nor ``pyOpenSSL`` is installed -- but the ``openssl`` 3.x CLI is.
So chain capture and validation are done by shelling out to ``openssl``.
"""

import os
import re
import shutil
import ssl
import subprocess
import tempfile
import urllib.request

import requests
from requests.adapters import HTTPAdapter

try:
    import certifi
except Exception:  # noqa: BLE001 -- certifi should be present, but degrade cleanly
    certifi = None

# One PEM certificate block.
_PEM_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)
# AIA "CA Issuers - URI:" line emitted by `openssl x509 -text`.
_AIA_RE = re.compile(r"CA Issuers - URI:(\S+)")

# Cap AIA walk so a pathological/looping issuer set cannot spin forever.
_MAX_AIA_HOPS = 8


def _openssl() -> str:
    """Absolute path to the openssl CLI, or raise if unavailable."""
    exe = shutil.which("openssl")
    if not exe:
        raise RuntimeError("openssl CLI not found on PATH; cannot auto-fetch chain")
    return exe


def _run(cmd, input_bytes=b"", timeout=30):
    """Run a command, return (returncode, stdout_bytes, stderr_text). Never raises
    on non-zero exit; raises only on timeout / missing binary."""
    proc = subprocess.run(
        cmd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace")


def _pem_blocks(text: str):
    """Return the list of PEM certificate blocks (as strings) found in text."""
    return _PEM_RE.findall(text or "")


def _cert_text(pem: str) -> str:
    """`openssl x509 -text -noout` for a single PEM cert (for AIA/subject/issuer)."""
    _, out, _ = _run([_openssl(), "x509", "-noout", "-text"], input_bytes=pem.encode())
    return out.decode("utf-8", "replace")


def _subject_issuer(pem: str):
    """Return (subject, issuer) one-line strings for a PEM cert."""
    _, out, _ = _run(
        [_openssl(), "x509", "-noout", "-subject", "-issuer", "-nameopt", "oneline"],
        input_bytes=pem.encode(),
    )
    subject = issuer = ""
    for line in out.decode("utf-8", "replace").splitlines():
        if line.startswith("subject="):
            subject = line[len("subject="):].strip()
        elif line.startswith("issuer="):
            issuer = line[len("issuer="):].strip()
    return subject, issuer


def _is_self_signed(pem: str) -> bool:
    """A cert whose subject == issuer is a (self-signed) root."""
    subject, issuer = _subject_issuer(pem)
    return bool(subject) and subject == issuer


def _aia_issuer_uri(pem: str):
    """Return the AIA 'CA Issuers' URI for a cert, or None."""
    m = _AIA_RE.search(_cert_text(pem))
    return m.group(1) if m else None


def _fetch_issuer_pem(uri: str):
    """Download an issuer cert from an AIA URI and normalize to PEM. None on failure.

    AIA commonly serves DER (.cer/.crt). Convert whatever we get to PEM via openssl.
    """
    try:
        with urllib.request.urlopen(uri, timeout=20) as resp:  # noqa: S310 -- AIA URL from cert
            data = resp.read()
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    # Already PEM?
    if b"-----BEGIN CERTIFICATE-----" in data:
        blocks = _pem_blocks(data.decode("utf-8", "replace"))
        return blocks[0] if blocks else None
    # Assume DER -> PEM.
    rc, out, _ = _run(
        [_openssl(), "x509", "-inform", "DER", "-outform", "PEM"],
        input_bytes=data,
    )
    if rc != 0 or not out:
        return None
    blocks = _pem_blocks(out.decode("utf-8", "replace"))
    return blocks[0] if blocks else None


def _grab_presented_chain(host: str, port: int, timeout: int = 30):
    """Return the list of PEM blocks the server presents (leaf + any intermediates).

    Uses ``openssl s_client -showcerts`` with SNI (-servername) so a fronting
    proxy / ELB returns the correct certificate even when the connect host is an
    autogenerated name. We capture whatever chain terminates TLS at the connected
    host -- correct for a proxy -- and do NOT filter on hostname here.
    """
    cmd = [
        _openssl(), "s_client",
        "-connect", f"{host}:{port}",
        "-servername", host,      # SNI: correct cert through proxy/ELB
        "-showcerts",
        "-verify", "10",
    ]
    try:
        _, out, _ = _run(cmd, input_bytes=b"", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"openssl s_client timed out connecting to {host}:{port}")
    blocks = _pem_blocks(out.decode("utf-8", "replace"))
    if not blocks:
        raise RuntimeError(
            f"no certificates presented by {host}:{port} (host unreachable or not TLS?)"
        )
    return blocks


def _complete_chain(blocks):
    """Ensure the chain reaches a self-signed root, completing via AIA if needed.

    Starting from the presented blocks, if the top cert is not self-signed, follow
    its AIA 'CA Issuers' URI to download the next issuer up, appending until we hit
    a self-signed root, run out of AIA info, or hit the hop cap. Returns the
    (possibly extended) list of PEM blocks. De-dupes while preserving order.
    """
    chain = list(blocks)
    seen = set(chain)
    hops = 0
    while hops < _MAX_AIA_HOPS:
        top = chain[-1]
        if _is_self_signed(top):
            break
        uri = _aia_issuer_uri(top)
        if not uri:
            # No way to fetch the parent; stop. Completeness is validated later by
            # openssl verify against certifi roots (a public root may already be
            # trusted even though we couldn't append it).
            break
        issuer_pem = _fetch_issuer_pem(uri)
        if not issuer_pem or issuer_pem in seen:
            break
        chain.append(issuer_pem)
        seen.add(issuer_pem)
        hops += 1
    return chain


def _verify_bundle(leaf_pem: str, bundle_path: str) -> bool:
    """Return True if ``leaf_pem`` verifies against the CA bundle at ``bundle_path``.

    ``openssl verify -CAfile <bundle>`` builds and checks the path from the leaf to
    a trust anchor in the bundle. The bundle includes the downloaded chain PLUS
    certifi roots, so a chain terminating at either a private root (we downloaded)
    or a public root (in certifi) verifies.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as lf:
        lf.write(leaf_pem)
        leaf_file = lf.name
    try:
        rc, out, err = _run(
            [_openssl(), "verify", "-CAfile", bundle_path, leaf_file],
        )
        return rc == 0 and b"OK" in out
    finally:
        try:
            os.unlink(leaf_file)
        except OSError:
            pass


def fetch_full_chain(host: str, port: int = 443, cache_dir: str = None) -> str:
    """Download the complete cert chain for ``host`` and return a CA bundle file path.

    Steps:
      1. Capture the presented chain via ``openssl s_client -showcerts`` (SNI on).
      2. Complete it to a self-signed root via AIA if intermediates are missing.
      3. Write <downloaded chain> + <certifi roots> to a bundle file.
      4. Validate the leaf verifies against that bundle; raise if it does not
         (never return an unverifiable / partial bundle).

    The bundle is written under ``cache_dir`` (default: a per-host file in the
    system temp dir) and its path returned for use as requests' ``verify``.

    Raises RuntimeError on any failure (unreachable host, no chain, fails to verify).
    """
    blocks = _grab_presented_chain(host, port)
    leaf = blocks[0]
    chain = _complete_chain(blocks)

    # Assemble bundle: downloaded chain first, then certifi public roots so a chain
    # ending at a public root also validates.
    parts = list(chain)
    if certifi is not None:
        try:
            with open(certifi.where(), "r") as cf:
                parts.append(cf.read())
        except OSError:
            pass

    cache_dir = cache_dir or tempfile.gettempdir()
    os.makedirs(cache_dir, exist_ok=True)
    safe_host = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    bundle_path = os.path.join(cache_dir, f"otello-ca-{safe_host}-{port}.pem")
    with open(bundle_path, "w") as bf:
        bf.write("\n".join(p.strip() for p in parts if p.strip()) + "\n")

    if not _verify_bundle(leaf, bundle_path):
        raise RuntimeError(
            f"assembled chain for {host}:{port} did not verify against its own "
            f"CA bundle (incomplete chain and AIA completion failed); refusing to "
            f"use a partial bundle"
        )
    return bundle_path


def _host_from_url(url_or_host: str) -> str:
    """Extract the bare host from a URL or host[:port] string."""
    s = url_or_host
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0]
    # strip userinfo + port
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    if ":" in s:
        s = s.rsplit(":", 1)[0]
    return s


def resolve_verify(cli_ca_cert: str, host_or_url: str, *,
                   allow_autodownload: bool = True, no_verify: bool = False,
                   log=print):
    """Resolve the requests ``verify`` value for talking to ``host_or_url``.

    Returns ``(verify, mode)`` where ``verify`` is a CA bundle path (str), or None
    when nothing usable was resolved, and ``mode`` is one of:
      "forced_insecure" (no_verify), "explicit", "env", "autochain", "none".

    Never raises: auto-download failure downgrades to ("none") so the caller can
    decide whether to fall back to insecure verify=False.
    """
    if no_verify:
        return None, "forced_insecure"

    # 1. explicit CLI path
    if cli_ca_cert:
        if not os.path.exists(cli_ca_cert):
            raise SystemExit(f"--ca-cert not found: {cli_ca_cert}")
        return cli_ca_cert, "explicit"

    # 2. environment bundles
    for env_key in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        path = os.environ.get(env_key)
        if path and os.path.exists(path):
            log(f"TLS: using {env_key}={path}")
            return path, "env"

    # 3. auto-download the full chain
    if allow_autodownload:
        host = _host_from_url(host_or_url)
        try:
            bundle = fetch_full_chain(host)
            log(f"TLS: auto-downloaded verified chain for {host} -> {bundle}")
            return bundle, "autochain"
        except Exception as exc:  # noqa: BLE001 -- degrade to caller-decided fallback
            log(f"TLS: WARNING auto-download of CA chain for {host} failed: {exc}")

    return None, "none"


class _ChainVerifyHostnameTolerantAdapter(HTTPAdapter):
    """A requests HTTPAdapter that verifies the cert CHAIN but NOT the hostname.

    The HySDS cluster is fronted by an AWS ELB addressed by its autogenerated DNS
    name (``internal-...elb.amazonaws.com``), while its certificate is issued for
    the internal name ``nisar-st-internal.jpl.nasa.gov`` (chaining to JPLICA Root).
    Normal verification (``session.verify=<bundle>``) enforces BOTH chain trust and
    hostname match, so the name mismatch raises SSLError even though the chain is
    valid. This adapter keeps chain enforcement (protects against a forged cert
    that does not chain to the trusted root) but disables the hostname assertion,
    which is unavoidable when addressing the load balancer by its AWS name.

    urllib3 2.x asserts the hostname at the connection-pool level, so disabling
    ``ctx.check_hostname`` is not enough -- the pool manager must also be created
    with ``assert_hostname=False``. Both are set here.
    """

    def __init__(self, ca_bundle=None, *args, **kwargs):
        self._ca_bundle = ca_bundle  # path to CA PEM, or None -> certifi default
        super().__init__(*args, **kwargs)

    def _build_context(self):
        ctx = ssl.create_default_context(cafile=self._ca_bundle)
        ctx.check_hostname = False           # tolerate ELB-name vs cert-SAN mismatch
        ctx.verify_mode = ssl.CERT_REQUIRED  # but STILL require a trusted chain
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._build_context()
        kwargs["assert_hostname"] = False    # urllib3-level hostname assertion off
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._build_context()
        kwargs["assert_hostname"] = False
        return super().proxy_manager_for(*args, **kwargs)


def apply_tls(obj, verify) -> None:
    """Configure TLS on an otello object's shared requests Session.

    Works uniformly for any otello object (Mozart, CI, Job, ...) since all share a
    requests Session on ``_session``. Behavior by ``verify`` value:

    - ``None``  (mode "none"): leave the session as otello configured it (insecure,
      verify=False) -- the caller handles the insecure fallback explicitly.
    - ``False`` (explicit --no-verify): force verify=False.
    - a bundle path (str): mount a chain-verifying, hostname-TOLERANT adapter so
      EVERY request through this session (mozart, grq, CI, job endpoints) verifies
      the cert chain against the bundle while tolerating the ELB/cert name
      mismatch. ``session.verify`` is also set to the bundle (harmless; the mounted
      adapter's SSL context governs the actual handshake).
    """
    session = getattr(obj, "_session", None)
    if session is None:
        raise RuntimeError(f"{obj!r} has no _session to configure TLS on")

    if verify is None:
        return
    if verify is False:
        session.verify = False
        return

    # A real CA bundle path -> chain-verify, hostname-tolerant, session-wide.
    session.verify = verify
    session.mount("https://", _ChainVerifyHostnameTolerantAdapter(ca_bundle=verify))


# --------------------------------------------------------------------------- #
# Standalone test: python3 otello_tls.py <host-or-url> [port]
# --------------------------------------------------------------------------- #
def _main(argv) -> int:
    if not argv:
        print("usage: otello_tls.py <host-or-url> [port]")
        return 2
    host = _host_from_url(argv[0])
    port = int(argv[1]) if len(argv) > 1 else 443
    try:
        bundle = fetch_full_chain(host, port)
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}")
        return 1
    with open(bundle) as f:
        text = f.read()
    n_certs = len(_pem_blocks(text))
    print(f"bundle: {bundle}")
    print(f"certificates in bundle (incl. certifi roots): {n_certs}")
    # Report the downloaded (non-certifi) chain length by re-grabbing presented certs.
    try:
        presented = _grab_presented_chain(host, port)
        completed = _complete_chain(presented)
        print(f"presented chain: {len(presented)} cert(s); "
              f"after AIA completion: {len(completed)} cert(s)")
        print(f"top cert self-signed (root reached): {_is_self_signed(completed[-1])}")
    except Exception as exc:  # noqa: BLE001
        print(f"(could not re-summarize chain: {exc})")
    print("openssl verify: OK (bundle validated during fetch)")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
