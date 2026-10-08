"""Integration tests using the real Helm and Flux Operator template engines."""
import copy
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts/branch-preview"


def helm(*overrides):
    args = ["helm", "template", "sample", str(CHART), "--namespace", "preview-test",
            "--set", "repository.url=https://github.com/example/my-app"]
    for override in overrides:
        args.extend(["--set", override])
    return list(yaml.safe_load_all(subprocess.check_output(args, text=True)))


def render(branches, sha="a" * 40, *overrides):
    resources = helm(*overrides)
    template = copy.deepcopy(next(x for x in resources if x["kind"] == "ResourceSet"))
    del template["spec"]["inputsFrom"]
    template["spec"]["inputs"] = [{"id": str(i + 1), "branch": b, "sha": sha} for i, b in enumerate(branches)]
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "rset.yaml"
        source.write_text(yaml.safe_dump(template, sort_keys=False))
        out = subprocess.check_output(["flux-operator", "build", "resourceset", "-f", str(source)], text=True)
    docs = [d for d in yaml.safe_load_all(out) if d]
    return [item for d in docs for item in (d["items"] if d.get("kind") == "List" else [d])]


class ChartTests(unittest.TestCase):
    def test_provider_and_async_reconciliation(self):
        docs = helm()
        provider = next(d for d in docs if d["kind"] == "ResourceSetInputProvider")
        self.assertEqual(provider["spec"]["type"], "GitHubBranch")
        self.assertEqual(provider["spec"]["filter"]["limit"], 10000)
        self.assertFalse(next(d for d in docs if d["kind"] == "ResourceSet")["spec"]["wait"])

    def test_all_branch_resources_names_and_dns(self):
        branches = ["main", "feature/a", "feature-a", "Feature/A", "true", "123", "a" * 250,
                    'feature/quote"here', "main--reserved", "feature/under_score"]
        docs = render(branches)
        self.assertEqual(len(docs), len(branches) * 9)
        routes = [d for d in docs if d["kind"] == "HTTPRoute"]
        hosts = [d["spec"]["hostnames"][0] for d in routes]
        self.assertEqual(len(set(hosts)), len(branches))
        self.assertIn("main.my-app.test.internal", hosts)
        for d in docs:
            self.assertLessEqual(len(d["metadata"]["name"]), 63)
            self.assertRegex(d["metadata"]["name"], r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
            self.assertEqual(d["metadata"]["namespace"], "preview-test")
        for host in hosts:
            label = host.split(".")[0]
            self.assertLessEqual(len(label), 63)
            self.assertRegex(label, r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
        for d in docs:
            if d["kind"] == "Job":
                self.assertNotIn("ttlSecondsAfterFinished", d["spec"])
                pod = d["spec"]["template"]["spec"]
                self.assertEqual(pod["initContainers"][0]["restartPolicy"], "Always")
                self.assertFalse(pod["automountServiceAccountToken"])
                self.assertNotIn("/kube", [v["mountPath"] for v in pod["initContainers"][0]["volumeMounts"]])
            if d["kind"] == "Lease":
                self.assertNotIn("spec", d)
                self.assertEqual(d["metadata"]["annotations"]["fluxcd.controlplane.io/prune"], "disabled")

    def test_numeric_hash_labels_are_strings(self):
        branch = next(str(i) for i in range(100000) if re.fullmatch(r"[0-9]{16}", hashlib.sha256(str(i).encode()).hexdigest()[:16]))
        docs = render([branch])
        job = next(d for d in docs if d["kind"] == "Job")
        for metadata in [job["metadata"], job["spec"]["template"]["metadata"]]:
            self.assertIsInstance(metadata["labels"]["elektrokube.dev/branch-id"], str)

    def test_hash_looking_literal_branch_does_not_collide(self):
        docs = render(["feature/a"])
        route = next(d for d in docs if d["kind"] == "HTTPRoute")
        literal = route["spec"]["hostnames"][0].split(".")[0]
        both = render(["feature/a", literal])
        hosts = [d["spec"]["hostnames"][0] for d in both if d["kind"] == "HTTPRoute"]
        self.assertEqual(len(set(hosts)), 2)

    def test_new_sha_changes_job_and_image_not_database(self):
        old = {d["kind"]: d for d in render(["main"], "a" * 40)}
        new = {d["kind"]: d for d in render(["main"], "b" * 40)}
        for kind in ["Cluster", "Deployment", "Lease", "Service", "HTTPRoute"]:
            self.assertEqual(old[kind]["metadata"]["name"], new[kind]["metadata"]["name"])
        self.assertNotEqual(old["Job"]["metadata"]["name"], new["Job"]["metadata"]["name"])
        deployment = new["Deployment"]["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("b" * 40, deployment["image"])
        env = new["Job"]["spec"]["template"]["spec"]["containers"][0]["env"]
        commit_tag = next(v["value"] for v in env if v["name"] == "COMMIT_TAG")
        self.assertTrue(deployment["image"].endswith(":" + commit_tag))
        self.assertEqual(deployment["env"][0]["valueFrom"]["secretKeyRef"]["name"], new["Cluster"]["metadata"]["name"] + "-app")

    def test_public_repository_and_explicit_database_cleanup(self):
        docs = render(["main"], "a" * 40, "repository.credentialsSecret=", "database.retainOnDelete=false")
        db = next(d for d in docs if d["kind"] == "Cluster")
        self.assertNotIn("fluxcd.controlplane.io/prune", db["metadata"]["annotations"])
        job = next(d for d in docs if d["kind"] == "Job")
        self.assertNotIn("github", [v["name"] for v in job["spec"]["template"]["spec"]["volumes"]])

    def test_values_schema_rejects_bad_input(self):
        for value in ["repository.url=http://github.com/owner/repo", "application.port=0", "repository.branchLimit=10001"]:
            with self.assertRaises(subprocess.CalledProcessError):
                helm(value)


if __name__ == "__main__":
    unittest.main()
