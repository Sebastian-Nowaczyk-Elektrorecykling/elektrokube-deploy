# elektrokube-deploy

A Helm chart that turns **every branch of a GitHub repository** into a preview environment: a Kubernetes image-build Job, GHCR image tags, Deployment, Service, HTTPRoute, and an independent CloudNativePG PostgreSQL cluster.

The actual Flux CRD is **`ResourceSetInputProvider`**, with `type: GitHubBranch`. This and `ResourceSet` are supplied by **Flux Operator**, not by the four standard Flux controllers alone.

## Layout

- `charts/branch-preview/`: reusable chart, values schema, namespace-scoped reconciliation RBAC, and the Python build/publish runner.
- `build-tools/`: non-root Python/git/buildctl image. GitHub Actions validates the repository and publishes it to GHCR for amd64 and arm64.
- `bootstrap/`, `clusters/elektrokube/`, `infrastructure/`: opt-in GitOps registration, Flux Operator installation, and a shared Cilium Gateway.
- `applications/`: initially empty. Add one configured HelmRelease per application repository.
- `examples/`: Helm values and a complete Namespace/HelmRelease example; nothing here is automatically deployed.
- `tests/`: publication/locking unit tests and real Helm + Flux Operator rendering tests.

## How it works

```text
GitHub branches (polled every 30 seconds)
  -> ResourceSetInputProvider
  -> ResourceSet (one group per branch)
       |- Job: exact SHA checkout -> rootless BuildKit -> unique staging image
       |        -> retained, non-expiring Lease -> commit tag -> branch tag
       |- Deployment -> commit-specific image tag
       |- Service -> HTTPRoute -> <branch>.<repository>.test.internal
       `- CNPG Cluster -> generated application Secret -> DATABASE_URL
```

A new SHA changes both the Job name and Deployment pod template. The previous Job is pruned, and its runner independently checks GitHub every 10 seconds while cloning/building, terminating subprocesses when superseded. BuildKit is a Kubernetes **native sidecar**, so it terminates when the runner exits instead of keeping completed Jobs alive. Completed desired Jobs have no TTL, avoiding rebuild loops caused by Flux recreating them.

The new Deployment may temporarily show `ImagePullBackOff` until its image is published, or wait for CNPG's generated Secret. With `maxUnavailable: 0`, an existing ready replica keeps serving until the new replica becomes ready. A failed build does not roll the application back to an older commit or remove the last ready replica. The ResourceSet does not wait for workloads, so one broken branch does not block processing new branch inputs.

### Latest-only publication: the precise guarantee

BuildKit can only publish a unique `build-<pod-uid>` staging tag through this workflow. The runner publishes final tags while holding a **per-branch Lease**, acquired using Kubernetes `resourceVersion` compare-and-swap:

1. Acquire the retained, non-expiring Lease. There is no clock-based expiry or automatic lock stealing.
2. Check the live GitHub branch HEAD **inside the lock**, not just before the build.
3. Publish `sha-<full-commit>-<branch-id>-<build-config-id>`, preserving that tag if it already exists.
4. Check HEAD again, then point `branch-<dns-branch-label>` at the same manifest. Verify the result before releasing the lock.

Manifest PUTs use a single HTTP request, **without automatic retries**. An ambiguous registry write or a crash in the critical section leaves the Lease held. New writers stop rather than risk a partitioned/late publisher overwriting a newer tag. See recovery below. Leases are not garbage-collected when a branch or release is deleted; recreating a branch cannot bypass an old holder.

**Guarantee scope:** one chart release owns each source repository/destination image; no other CI, user, or release writes these tags or clears its locks. Within that scope, an older build finishing late cannot overwrite a newer publisher. GitHub ref updates and GHCR manifest updates do not share a transaction: a push can occur after the final HEAD check. The tag therefore converges to the newest **successfully built** HEAD; it cannot point to an image that has not built yet. Build/API failures or an intentionally retained lock can delay convergence. Deployments use commit-specific tags, never the mutable branch alias.

Changing `build.revision` forces a rebuild of the same SHA. Relevant chart/configuration changes also change the build ID. Previously published commit tags are reused, not overwritten, on retries or manual Job recreation. Use a new revision to rebuild with updated mutable base images or dependencies.

### Branch names and URLs

`main` in `my-app` is exposed at `http://main.my-app.test.internal` and gets image alias `branch-main`.

DNS-incompatible branch names, case-sensitive names, names longer than 63 characters, and names containing the reserved `--` separator become `<normalized-prefix>--<16-character-sha256>`. Thus `feature/login` and `feature-login` cannot collide, and a literal branch resembling an encoded name is encoded again. Kubernetes identities always include a branch hash and remain stable across commits. Original names are stored in `elektrokube.dev/branch` annotations. Repository names are normalized for DNS; `routing.repositoryLabel` can override that label.

All branches, including `main`, match by default. The provider limit is explicitly **10,000**, Flux Operator's API ceiling, instead of silently accepting its default of 100. Repositories exceeding that ceiling need multiple non-overlapping providers/releases with distinct destinations. Filters and polling intervals are configurable. Thousands of branches also mean thousands of databases and substantial GitHub API traffic: size capacity and token rate limits accordingly.

## Cluster integration

The supplied manifests use the existing infrastructure contracts:

| Existing owner | Reused configuration |
| --- | --- |
| `elektrokube-cilium-and-flux` | Standard Flux controllers, `flux` and `cilium` Kustomizations, Gateway API, Cilium GatewayClass, host-network ingress |
| `elektrokube-storage` | `storage-cnpg`, `storage-classes`, `storage-cnpg-policy`, and `longhorn-cnpg` |
| This repository | Flux Operator **0.61.0**, `gateway-system/test-internal`, and opt-in application releases |

No `FluxInstance` is created, and this repository does not adopt/reinstall the existing Flux controllers, Cilium, Longhorn, or CNPG operator. Nothing modifies the two existing repositories automatically.

Requirements: Kubernetes **1.33+** for stable native sidecars; the existing storage admission policy separately requires the cluster version described in `elektrokube-storage`. Worker nodes must support unprivileged user namespaces for RootlessKit. Rootless BuildKit needs unconfined seccomp/AppArmor and setuid mapping helpers, so the example uses an explicitly privileged Pod Security admission namespace. It does **not** use privileged containers, a Docker socket, hostPath mounts, or host networking.

The shared Gateway has one HTTP listener on port 80 and accepts routes from other namespaces. No listener hostname restriction is used. DNS is external to this chart, as requested. With Cilium host-network ingress, do not create a second Gateway competing for port 80 on the same nodes: reuse an existing Gateway by adjusting chart values and omitting `deploy-gateway` from this bootstrap. TLS is not configured by default; attach routes to an existing HTTPS listener when required.

### Register this repository

From a checkout, after reviewing the Gateway ownership above:

```sh
kubectl apply -k bootstrap
```

Alternatively, copy `bootstrap/source.yaml` and `bootstrap/sync.yaml` into the already watched `clusters/elektrokube` directory of the root GitOps repository and add them to its `kustomization.yaml`. That makes the initial registration Git-managed too.

The root creates three dependency-ordered Kustomizations. Application reconciliation waits for Flux Operator, the Gateway, CNPG, the storage classes and the CNPG policy. The application directory is deliberately empty, so registration does not build an arbitrary application or require placeholder credentials.

## Deploy an application

### 1. Publish / make the build-tools image accessible

The `Validate and publish build tools` GitHub Actions workflow publishes:

```text
ghcr.io/sebastian-nowaczyk-elektrorecykling/elektrokube-deploy/build-tools:0.1.0
ghcr.io/sebastian-nowaczyk-elektrorecykling/elektrokube-deploy/build-tools:sha-<repository-commit>
```

The version alias is refreshed by successful main builds. For controlled upgrades, override `build.toolsImage` with the immutable commit tag or, preferably, its image digest. The package may initially be private: make it public or grant `ghcr-pull` access to it. GitHub Actions needs package-write permission; cluster credentials are not needed by this workflow.

### 2. Create credentials in the release namespace

Create the namespace shown in your configured example, then provision these **existing** Secrets. Do not commit plaintext credentials or put them in Helm values.

| Secret | Type / keys | Access |
| --- | --- | --- |
| `github-auth` | `kubernetes.io/basic-auth`: `username`, `password` | Read-only GitHub token with repository contents access |
| `ghcr-push` | `kubernetes.io/dockerconfigjson`: `.dockerconfigjson` | GHCR package push/pull access for the destination |
| `ghcr-pull` | `kubernetes.io/dockerconfigjson`: `.dockerconfigjson` | Read-only access to application images and build tools |

For example, with credentials already stored in permission-restricted local files:

```sh
kubectl create namespace preview-my-app
kubectl label namespace preview-my-app pod-security.kubernetes.io/enforce=privileged
kubectl -n preview-my-app create secret generic github-auth \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=x-access-token --from-file=password=/secure/github-read-token
kubectl -n preview-my-app create secret generic ghcr-push \
  --type=kubernetes.io/dockerconfigjson --from-file=.dockerconfigjson=/secure/ghcr-push-config.json
kubectl -n preview-my-app create secret generic ghcr-pull \
  --type=kubernetes.io/dockerconfigjson --from-file=.dockerconfigjson=/secure/ghcr-pull-config.json
```

Use ordinary Docker `auths.ghcr.io.auth` credentials, not an external credential-helper configuration. GHCR personal-token authentication uses a classic PAT with the required package scopes and organization SSO authorization where applicable. Prefer SOPS/External Secrets or your existing secret manager for sustained operation. CNPG independently generates each database password and the `<cluster>-app` Secret; the chart injects its `uri` as `DATABASE_URL`.

A public source repository may use `repository.credentialsSecret: ""`, but authenticated polling is strongly recommended for rate limits. The build runner supports PAT/basic-auth credentials, not GitHub App private-key credentials; the upstream Flux provider's broader authentication options do not change that runner contract.

### 3. Add the release

Copy `examples/application.yaml` to `applications/my-app.yaml`. Replace the source URL, namespace, release name and application port. Add `my-app.yaml` to `applications/kustomization.yaml`, then commit. The application only needs a Dockerfile, an HTTP server on the configured port, and support for the configured database environment variable. Override `application.env`, `envFrom`, command, arguments and probes as necessary.

A direct Helm install is also supported once Flux Operator, CNPG and the Gateway exist:

```sh
helm upgrade --install my-app ./charts/branch-preview \
  --namespace preview-my-app \
  --set repository.url=https://github.com/OWNER/REPO \
  --set application.port=8080
```

The example HelmRelease uses `reconcileStrategy: Revision`, so Git chart edits are picked up even before a chart version bump. Build context and Dockerfile paths are relative to the source repository. Plain build arguments are supported; never put secrets in build arguments. Submodules, Git LFS, migrations, Docker Compose and automatic application-specific configuration are not implemented. Default builds target linux/amd64; for arm64 change `build.platform` and both build/application node selectors. Cross-platform emulation inside the cluster is not automatically installed.

## Data lifecycle and security

Each branch gets a separate CNPG cluster, user, password and database; commits on the same branch retain the same database. Defaults are one instance and a 5 GiB `longhorn-cnpg` volume. These are preview defaults, not a highly available or backed-up production database.

**`database.retainOnDelete: true` is the default.** Deleting a branch removes its Job, Deployment, Service, HTTPRoute and builder RBAC, but retains its CNPG cluster and publish Lease. Helm uninstall also leaves those generated objects intact. Deleting the namespace still deletes everything in it. Set `retainOnDelete: false` only for intentionally disposable data; deleting a branch can then remove the database and its storage. Retained databases and GHCR staging/old commit images consume space until explicitly cleaned up. No registry garbage-collection credentials are granted to the runner.

Use only **trusted repository branches**. A Dockerfile is executable code. Rootless BuildKit is not a hostile multi-tenant sandbox, and build sessions have registry push capability; an adversarial source author is outside the publication guarantee. Branches share a release namespace and network, although their databases and runtime Secrets are distinct. BuildKit receives neither the Kubernetes token mount nor the GitHub token mount; the runner can only get/update its one branch Lease, and application pods do not automount Kubernetes credentials. The ResourceSet reconciles through a namespace-scoped service account rather than cluster-admin.

## Operations

```sh
kubectl -n preview-my-app get resourcesetinputproviders,resourcesets
kubectl -n preview-my-app get jobs,deployments,httproutes,clusters.postgresql.cnpg.io
kubectl -n preview-my-app logs job/JOB_NAME -c runner
kubectl -n preview-my-app get leases -o yaml
kubectl -n gateway-system get gateway test-internal
```

`ResourceSet Ready` means manifests were reconciled, not that all builds or applications are healthy. Check Job conditions, Deployment availability, HTTPRoute `Accepted`/`ResolvedRefs`, and CNPG readiness separately. A transient failed Job retries twice; after its retry/deadline budget is exhausted, delete that failed Job to retry the same commit. A new source SHA or build revision creates a new Job automatically.

### Recover a retained publish lock

Do **not** configure lease expiry or clear a lock merely because a Pod disappeared from the Kubernetes API. A partitioned node may still run it.

1. Suspend the release's input provider and ResourceSet with `flux-operator -n NAMESPACE suspend rsip NAME` and `suspend rset NAME`.
2. Inspect `LEASE.spec.holderIdentity` (the publisher Pod UID), its Job/logs, node state and GHCR tags. Stop the branch's Jobs and **verify actual process termination**. Fence an unreachable node. Resolve any in-flight/uncertain registry operation before proceeding; API-level force deletion alone is not proof.
3. Only after all former publishers are unable to write, clear the holder with a compare-and-swap patch (replace the placeholders):

   ```sh
   kubectl -n NAMESPACE patch lease LEASE --type=json -p \
     '[{"op":"test","path":"/spec/holderIdentity","value":"OLD_POD_UID"},{"op":"replace","path":"/spec/holderIdentity","value":""}]'
   ```

4. Delete the failed/superseded branch Job if it still exists, resume the input provider, then resume/reconcile the ResourceSet. The new Job checks current GitHub HEAD before doing any work.

This deliberately trades automatic failure recovery for stale-writer safety. Keep the Lease object itself; never solve this by deleting it and allowing overlapping publishers.

## Validation

```sh
python -m pip install -r tests/requirements.txt
make unit  # stdlib-only build/publish and concurrency tests
make test  # additionally requires Helm 3.19.0 and flux-operator CLI 0.61.0
```

CI runs Helm lint, renders the actual ResourceSet templates using the Flux Operator CLI, tests hostile/long/colliding branch names and new-SHA rollouts, builds the Kustomize roots, and packages the chart before publishing build tools. Rendering/unit tests are not a live cluster smoke test. Before broad rollout, test one disposable repository, rapid successive pushes, a failed Dockerfile, branch deletion/recreation, private GHCR pulls, and CNPG persistence on the real cluster.

## References

- [Flux Operator branch providers](https://fluxoperator.dev/docs/resourcesets/feature-branches/) and [ResourceSet API](https://fluxoperator.dev/docs/crd/resourceset/)
- [BuildKit rootless requirements](https://github.com/moby/buildkit/blob/master/docs/rootless.md)
- [OCI Distribution manifest operations](https://github.com/opencontainers/distribution-spec/blob/main/spec.md)
- [CNPG application connections and generated Secrets](https://cloudnative-pg.io/documentation/1.26/applications/)
- [GHCR authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
