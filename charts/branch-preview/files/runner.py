"""Build one exact commit; serialize GHCR publication without expiring locks.

Only this process publishes final tags. BuildKit pushes a unique staging tag.
Registry manifest PUTs are NEVER retried: an ambiguous failure retains the Lease.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler

LOG = logging.getLogger("builder")
STOP = threading.Event()
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


class Superseded(Exception):
    """The requested commit is no longer the branch HEAD, or termination began."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected HTTP redirect; refusing to forward credentials")


def request(url, *, headers=None, data=None, method="GET", context=None):
    """Exactly one HTTP request, no retries or redirects (including manifest PUT)."""
    opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
    with opener.open(Request(url, data=data, headers=headers or {}, method=method), timeout=25) as response:
        return response.read(), response.headers


def github_head(repo: str, branch: str) -> str | None:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "elektrokube-deploy",
               "X-GitHub-Api-Version": "2022-11-28", "Cache-Control": "no-cache"}
    token_file = Path("/github/password")
    if token_file.exists():
        headers["Authorization"] = "Bearer " + token_file.read_text().strip()
    url = f"https://api.github.com/repos/{repo}/git/ref/heads/{quote(branch, safe='')}"
    try:
        body, _ = request(url, headers=headers)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return json.loads(body)["object"]["sha"]


class Lease:
    """A non-expiring, resourceVersion-CAS mutex. Never steal a dead holder."""
    def __init__(self, namespace: str, name: str, holder: str, wait_seconds: int):
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        host = f"[{host}]" if ":" in host else host
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        self.url = f"https://{host}:{port}/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases/{name}"
        self.context = ssl.create_default_context(cafile="/kube/ca.crt")
        self.holder, self.wait_seconds = holder, wait_seconds

    def api(self, method="GET", obj=None):
        headers = {"Authorization": "Bearer " + Path("/kube/token").read_text().strip(),
                   "Content-Type": "application/json"}
        data = json.dumps(obj).encode() if obj is not None else None
        body, _ = request(self.url, headers=headers, data=data, method=method, context=self.context)
        return json.loads(body)

    def acquire(self, check_current):
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() < deadline:
            check_current()
            obj = self.api()
            if obj["metadata"].get("deletionTimestamp"):
                raise RuntimeError("Publish Lease is being deleted; refusing publication")
            holder = obj.get("spec", {}).get("holderIdentity", "")
            if not holder:
                obj.setdefault("spec", {}).update(holderIdentity=self.holder,
                    acquireTime=datetime.now(timezone.utc).isoformat())
                try:
                    # PUT includes the resourceVersion just read: competing writers get 409.
                    self.api("PUT", obj)
                    return
                except HTTPError as exc:
                    if exc.code != 409:
                        raise
            else:
                LOG.info("Publish lock held by %s; no expiry/automatic takeover", holder)
            time.sleep(2)
        raise RuntimeError("Publish lock timeout. Inspect the holder; see README recovery procedure.")

    def release(self):
        obj = self.api()
        if obj.get("spec", {}).get("holderIdentity") != self.holder:
            raise RuntimeError("Publish lock ownership changed unexpectedly")
        obj["spec"]["holderIdentity"] = ""
        self.api("PUT", obj)


@dataclass(frozen=True)
class Manifest:
    body: bytes
    media_type: str

    @property
    def digest(self):
        return "sha256:" + hashlib.sha256(self.body).hexdigest()


class Registry:
    def __init__(self, image: str):
        if not image.startswith("ghcr.io/"):
            raise ValueError("Only ghcr.io is supported")
        self.path = image.removeprefix("ghcr.io/")
        config = json.loads(Path(os.environ["DOCKER_CONFIG"], "config.json").read_text())
        auths = config.get("auths", {})
        auth = next((auths[k] for k in ("ghcr.io", "https://ghcr.io", "https://ghcr.io/v1/") if k in auths), None)
        if not auth:
            raise ValueError("Push Secret must contain basic GHCR credentials")
        basic = auth.get("auth")
        if not basic:
            basic = base64.b64encode(f'{auth["username"]}:{auth["password"]}'.encode()).decode()
        self.basic = basic

    def headers(self):
        # Obtain a fresh token before each operation; do not retry a manifest PUT on 401.
        query = urlencode({"service": "ghcr.io", "scope": f"repository:{self.path}:pull,push"})
        body, _ = request("https://ghcr.io/token?" + query,
                          headers={"Authorization": "Basic " + self.basic})
        data = json.loads(body)
        token = data.get("token") or data["access_token"]
        return {"Authorization": "Bearer " + token, "Accept": ACCEPT}

    def lookup(self, ref: str) -> Manifest | None:
        try:
            body, headers = request(f"https://ghcr.io/v2/{self.path}/manifests/{quote(ref, safe=':')}",
                                    headers=self.headers())
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return Manifest(body, headers["Content-Type"])

    def tag(self, manifest: Manifest, tag: str):
        headers = self.headers()
        headers["Content-Type"] = manifest.media_type
        request(f"https://ghcr.io/v2/{self.path}/manifests/{tag}",
                method="PUT", data=manifest.body, headers=headers)
        actual = self.lookup(tag)
        if actual is None or actual.digest != manifest.digest:
            raise RuntimeError("Registry tag verification failed; retaining publish lock")


def promote(lease, registry, check_current, digest, commit_tag, branch_tag):
    """No older build can overtake a newer publisher using this same retained Lease."""
    lease.acquire(check_current)
    safe_to_release = True
    try:
        check_current()  # Deliberately INSIDE the critical section.
        manifest = registry.lookup(commit_tag)
        if manifest is None:
            manifest = registry.lookup(digest)
            if manifest is None or manifest.digest != digest:
                raise RuntimeError("Staging image is missing or has an unexpected digest")
            safe_to_release = False
            registry.tag(manifest, commit_tag)
            safe_to_release = True
        # Preserve an already published commit tag, including on retries/recreated Jobs.
        check_current()
        safe_to_release = False
        registry.tag(manifest, branch_tag)
        safe_to_release = True
        LOG.info("Published %s -> %s", branch_tag, manifest.digest)
    finally:
        if safe_to_release:
            lease.release()
        else:
            LOG.error("Publication outcome uncertain. Lease intentionally retained; manual recovery required.")


def run_monitored(args: list[str], check_current, interval: int, *, cwd=None, env=None):
    check_current()
    process = subprocess.Popen(args, cwd=cwd, env=env, start_new_session=True)
    try:
        next_check = time.monotonic() + interval
        while process.poll() is None:
            if STOP.is_set():
                raise Superseded("Termination requested")
            if time.monotonic() >= next_check:
                check_current()
                next_check = time.monotonic() + interval
            time.sleep(0.5)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, args)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def contained_path(root: Path, relative: str) -> Path:
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("Build paths must stay within the checked-out repository")
    return result


def main():
    repo, branch, sha = (os.environ[k] for k in ("REPOSITORY", "BRANCH", "COMMIT_SHA"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid GitHub repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected a full 40-character commit SHA")
    interval = int(os.environ["CHECK_INTERVAL"])

    def check_current():
        if STOP.is_set() or github_head(repo, branch) != sha:
            raise Superseded("Branch moved/deleted or termination requested")

    check_current()
    root = Path("/workspace/source")
    root.mkdir(parents=True, exist_ok=True)
    askpass = Path("/tmp/git-askpass")
    askpass.write_text('#!/usr/local/bin/python\nimport pathlib,sys\n'
                      'print("x-access-token" if "Username" in sys.argv[1] else '
                      'pathlib.Path("/github/password").read_text().strip())\n')
    askpass.chmod(0o700)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=str(askpass))
    git = ["git", "-c", "credential.helper="]
    for args in (["init", "-q"], ["remote", "add", "origin", f"https://github.com/{repo}.git"],
                 ["fetch", "--depth=1", "origin", sha], ["checkout", "--detach", "FETCH_HEAD"]):
        run_monitored(git + args, check_current, interval, cwd=root, env=env)
    actual = subprocess.check_output(git + ["rev-parse", "HEAD"], cwd=root, text=True, timeout=10).strip()
    if actual != sha:
        raise RuntimeError("Checkout SHA mismatch")
    context = contained_path(root, os.environ["BUILD_CONTEXT"])
    dockerfile = contained_path(root, os.environ["DOCKERFILE"])
    if not context.is_dir() or not dockerfile.is_file():
        raise ValueError("Build context directory or Dockerfile does not exist")
    image = os.environ["IMAGE"]
    stage_tag = "build-" + os.environ["POD_UID"]
    args = ["buildctl", "--addr", "unix:///run/buildkit/buildkitd.sock", "build",
            "--frontend", "dockerfile.v0", "--local", f"context={context}",
            "--local", f"dockerfile={dockerfile.parent}", "--opt", f"filename={dockerfile.name}",
            "--opt", "platform=" + os.environ["BUILD_PLATFORM"],
            "--output", f"type=image,name={image}:{stage_tag},push=true",
            "--metadata-file", "/workspace/metadata.json"]
    if os.environ.get("BUILD_TARGET"):
        args += ["--opt", "target=" + os.environ["BUILD_TARGET"]]
    for key, value in json.loads(os.environ["BUILD_ARGS"]).items():
        args += ["--opt", f"build-arg:{key}={value}"]
    run_monitored(args, check_current, interval)
    digest = json.loads(Path("/workspace/metadata.json").read_text())["containerimage.digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RuntimeError("BuildKit returned an invalid manifest digest")
    lease = Lease(os.environ["POD_NAMESPACE"], os.environ["LEASE_NAME"],
                  os.environ["POD_UID"], int(os.environ["LOCK_WAIT"]))
    promote(lease, Registry(image), check_current, digest,
            os.environ["COMMIT_TAG"], os.environ["BRANCH_TAG"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    try:
        main()
    except Superseded as exc:
        LOG.info("Skipping obsolete build: %s", exc)
    except Exception:
        LOG.exception("Build failed")
        raise SystemExit(1)
