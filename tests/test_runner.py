import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.dont_write_bytecode = True
PATH = Path(__file__).resolve().parents[1] / "charts/branch-preview/files/runner.py"
spec = importlib.util.spec_from_file_location("runner", PATH)
r = importlib.util.module_from_spec(spec)
sys.modules["runner"] = r
spec.loader.exec_module(r)


class FakeLease:
    def __init__(self):
        self.held = False
        self.releases = 0
        self.after_acquire = lambda: None

    def acquire(self, check):
        if self.held:
            raise RuntimeError("locked")
        self.held = True
        self.after_acquire()

    def release(self):
        self.held = False
        self.releases += 1


class FakeRegistry:
    def __init__(self, manifest):
        self.images = {manifest.digest: manifest}
        self.writes = []
        self.fail_tag = None
        self.after_tag = lambda: None

    def lookup(self, ref):
        return self.images.get(ref)

    def tag(self, manifest, tag):
        # Model a timeout AFTER the server applied the write.
        self.images[tag] = manifest
        self.writes.append(tag)
        if tag == self.fail_tag:
            raise TimeoutError("response lost after mutation")
        self.after_tag()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.manifest = r.Manifest(b'{"schemaVersion":2}', 'application/vnd.oci.image.manifest.v1+json')
        self.lease = FakeLease()
        self.registry = FakeRegistry(self.manifest)
        self.head = "new"

    def publish(self, wanted="new"):
        def check():
            self.assertTrue(self.lease.held, "HEAD must be checked inside the lock")
            if self.head != wanted:
                raise r.Superseded()
        r.promote(self.lease, self.registry, check, self.manifest.digest, "sha-new", "branch-main")

    def test_success(self):
        self.publish()
        self.assertEqual(self.registry.writes, ["sha-new", "branch-main"])
        self.assertEqual(self.lease.releases, 1)

    def test_late_old_build_cannot_overwrite_new(self):
        self.publish()
        with self.assertRaises(r.Superseded):
            self.publish("old")
        self.assertEqual(self.registry.writes, ["sha-new", "branch-main"])
        self.assertFalse(self.lease.held)

    def test_branch_moves_during_lock_acquisition(self):
        self.lease.after_acquire = lambda: setattr(self, "head", "newer")
        with self.assertRaises(r.Superseded):
            self.publish()
        self.assertEqual(self.registry.writes, [])
        self.assertFalse(self.lease.held)

    def test_branch_moves_after_commit_tag_before_branch_tag(self):
        self.registry.after_tag = lambda: setattr(self, "head", "newer")
        with self.assertRaises(r.Superseded):
            self.publish()
        self.assertEqual(self.registry.writes, ["sha-new"])
        self.assertFalse(self.lease.held)

    def test_deleted_branch(self):
        self.head = None
        with self.assertRaises(r.Superseded):
            self.publish()
        self.assertEqual(self.registry.writes, [])

    def test_ambiguous_branch_put_retains_lock_and_blocks_next_publisher(self):
        self.registry.fail_tag = "branch-main"
        with self.assertRaises(TimeoutError):
            self.publish()
        self.assertTrue(self.lease.held)
        with self.assertRaisesRegex(RuntimeError, "locked"):
            self.publish()
        self.assertEqual(self.lease.releases, 0)

    def test_ambiguous_commit_put_also_retains_lock(self):
        self.registry.fail_tag = "sha-new"
        with self.assertRaises(TimeoutError):
            self.publish()
        self.assertTrue(self.lease.held)
        self.assertNotIn("branch-main", self.registry.images)

    def test_existing_commit_tag_is_not_overwritten(self):
        previous = r.Manifest(b"previous-build-of-the-same-commit", "application/json")
        self.registry.images["sha-new"] = previous
        self.publish()
        self.assertEqual(self.registry.writes, ["branch-main"])
        self.assertEqual(self.registry.images["branch-main"], previous)

    def test_missing_staging_image_releases_lock_without_writes(self):
        self.registry.images.clear()
        with self.assertRaisesRegex(RuntimeError, "Staging"):
            self.publish()
        self.assertFalse(self.lease.held)
        self.assertEqual(self.registry.writes, [])


class LeaseTests(unittest.TestCase):
    def lease(self, holder=""):
        lease = object.__new__(r.Lease)
        lease.holder, lease.wait_seconds = "pod-new", 1
        state = {"metadata": {"resourceVersion": "1"}, "spec": {"holderIdentity": holder}}
        def api(method="GET", obj=None):
            if method == "PUT":
                if obj["metadata"]["resourceVersion"] != state["metadata"]["resourceVersion"]:
                    raise HTTPError("https://api", 409, "conflict", {}, None)
                state.update(copy.deepcopy(obj))
                state["metadata"]["resourceVersion"] = str(int(state["metadata"]["resourceVersion"]) + 1)
            return copy.deepcopy(state)
        lease.api = api
        return lease, state

    def test_cas_acquire_and_release(self):
        lease, state = self.lease()
        lease.acquire(lambda: None)
        self.assertEqual(state["spec"]["holderIdentity"], "pod-new")
        self.assertNotIn("leaseDurationSeconds", state["spec"])
        lease.release()
        self.assertEqual(state["spec"]["holderIdentity"], "")

    def test_never_steals_old_holder(self):
        lease, state = self.lease("dead-pod")
        with patch.object(r.time, "monotonic", side_effect=[0, 0, 2]), patch.object(r.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                lease.acquire(lambda: None)
        self.assertEqual(state["spec"]["holderIdentity"], "dead-pod")

    def test_cannot_release_another_holders_lock(self):
        lease, _ = self.lease("somebody-else")
        with self.assertRaisesRegex(RuntimeError, "ownership"):
            lease.release()

    def test_deleting_lease_is_not_acquired(self):
        lease, state = self.lease()
        state["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        with self.assertRaisesRegex(RuntimeError, "deleted"):
            lease.acquire(lambda: None)

    def test_conflict_retries_acquisition_not_registry_writes(self):
        lease, _ = self.lease()
        original, calls = lease.api, []
        def api(method="GET", obj=None):
            if method == "PUT" and not calls:
                calls.append(method)
                raise HTTPError("https://api", 409, "conflict", {}, None)
            return original(method, obj)
        lease.api = api
        with patch.object(r.time, "sleep"):
            lease.acquire(lambda: None)
        self.assertEqual(calls, ["PUT"])


class InputTests(unittest.TestCase):
    def test_paths_cannot_escape_via_parent_or_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            (root / "escape").symlink_to(Path(tmp))
            for path in ("../Dockerfile", "escape/Dockerfile", "/etc/passwd"):
                with self.assertRaises(ValueError):
                    r.contained_path(root, path)
            self.assertEqual(r.contained_path(root, "."), root)

    def test_branch_is_url_encoded_not_interpreted(self):
        with patch.object(r, "request", return_value=(b'{"object":{"sha":"abc"}}', {})) as call:
            self.assertEqual(r.github_head("owner/repo", "feature/a#b"), "abc")
            self.assertTrue(call.call_args.args[0].endswith("feature%2Fa%23b"))

    def test_redirects_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "redirect"):
            r.NoRedirect().redirect_request(None, None, 307, "redirect", {}, "https://other")

    def test_cancelled_build_never_starts_process(self):
        def stale():
            raise r.Superseded()
        with patch.object(r.subprocess, "Popen") as popen:
            with self.assertRaises(r.Superseded):
                r.run_monitored(["false"], stale, 10)
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
