# Managed Docker remote-build preview

This isolated runtime-side proof of concept keeps the existing BuildKit HK deployment unchanged and delegates Dockerfile/Compose semantics to Docker's official Buildx remote driver and Compose/Bake frontend. `compose.py` changes publication targets and generates a separate image-reference override; it does not rewrite submitted task files or interpret Dockerfile instructions.

The runtime tool image contains Docker, Compose, Buildx and the AKernel snapshotter/registration helper. Session endpoints and credentials are injected at launch, outside the image. Buildx pushes native Nydus OCI artifacts directly to an internal CNCF Distribution Registry and returns digest references. No OCI archive passes through the Harbor client and no task artifact is published to ACR.

`image_provider.py` runs on the isolated runtime node. Its session capability accepts only immutable references in an operator-authorized Registry/repository prefix. It validates OCI metadata, calls sandboxd's private image-management Unix socket, and exports existing ROFS through a read-only collection. Dragonfly and the node's existing image manager own content distribution/caching; this adapter does not maintain a second layer cache. `akernel-image-bridge` imports checked metadata and committed roots into inner containerd; unknown extraction roots still fail closed. Private service uppers remain inside the outer sandbox's checkpoint boundary.

The Registry requires private routes from HK workers for push and from runtime nodes for pull. Harbor clients do not require direct Registry access or Kubernetes credentials. Registry credentials are separate from AKernel API keys. This experiment uses private HTTP and a disposable Registry account, matching the deployed Dragonfly HTTP blob profile; TLS trust-chain qualification remains separate. `registry.insecure` is an explicit experiment-profile option, not an unconditional default.

BuildKit owns cache validity. Existing worker-local caches reuse steps on the same worker, but the current HK workers use `emptyDir` and the load balancer does not promise affinity. Cross-worker registry cache import/export is a separate native Buildx policy; runtime Nydus artifacts must not be represented as BuildKit cache records. ROFS and Dragonfly cache final content independently from build-step caches.

`ComposeProject.bind_runtime(sandbox, descriptor)` binds a newly prepared runtime and permits initial peer-restricted tunnel creation. `ComposeProject.attach()` remains the restore binding: it must not start daemons, build, run Compose up or create replacement tunnels. `network_mode: service:...` is resolved using Docker's native namespace-owner inspection, preserving the task's network configuration.

The preview pins prepared roots until the owned runtime is shut down, including while sandbox checkpoints are retained. Sandboxd's opt-in management endpoint participates in the same shared OCI reference counter as ordinary rootfs consumers, preventing another sandbox's deletion from stopping a referenced ROFS daemon. Persistent snapshot leases, tenant authorization/catalog integration, node-restart recovery and Registry garbage collection require a separate lifecycle design before production release.

Run adapter regressions with `python3 -m unittest discover -s builder/managed-docker -p 'test_*.py'`. SDK changes additionally require `make sdk-check`; helper changes require `go test ./...` in `builder/image-bridge`. Use an owned namespace/data directory and delete sandbox/snapshot records before unmounting collections or deleting Registry storage.

## Platform-owned profile

`registry_auth_file` accepts Sandboxd's flat Basic-auth map or a standard Docker `auths` configuration. Both node admission and the runtime broker normalize root Registry URL keys consistently. Client credential helpers or dynamic token refresh are not implemented by this preview.

See [the operator-only Sandboxd configuration](profile.example.toml).

Harbor's maintained managed path sends `managed_runtime_profile="harbor-v1"`.
`DockerRuntime(profile=...)` forwards only that capability in `extra_config`.
Sandboxd authorizes the trusted ADX tenant label against the operator allowlist,
chooses the pinned tool image and creates a distinct collection for each fresh
environment, including independent verifiers. It injects readonly configuration,
credentials and collection mounts; client-selected reserved mounts are rejected.

Package `provider_service.py` and `image_provider.py` on the runtime node. The
all-in-one image includes the opt-in `akernel-managed-images.service`; operators
supply `/etc/akernel/managed-runtime.env` containing `MANAGED_RUNTIME_ROOT`,
`MANAGED_IMAGE_SOCKET`, `MANAGED_IMAGE_PORT`, configure matching Sandboxd
`[plugin.managed_runtime]` / `[plugin.managed_runtime.profiles.harbor-v1]`, then
run `systemctl enable --now akernel-managed-images.service`. No unit/profile is enabled by default. Keep the private broker
and Registry on reachable internal routes. Never expose the image Unix socket.

The Sandboxd profile owns `image` (digest pinned), `registry`, `repository_prefix`,
`registry_auth_file`, `registry_insecure` (explicit experiment only),
`buildkit_endpoint`, `provider_url`, `bootstrap_root`, `allowed_tenants`, `writable_mb`, and `minimum_root_mb` (defaults to 4096 MiB). The total writable-storage floor is the image upper plus this root-layer reservation. Before starting each worker, run `python3 builder/managed-docker/profile_metadata.py /path/to/sandboxd.toml` and merge the resulting labels into its ADXlet service `config.labels`. Labels contain only profile names and minimum MiB; regenerate them with every storage-geometry change. The SDK queries this authenticated resource metadata using its own connection before allocating a managed sandbox, reserves the default floor, and adds a hard placement condition for compatible workers. Explicit requests or limits below the floor fail before creation. Sandboxd checks the actual total quota again before any collection filesystem changes. Ordinary image sandboxes do not perform this preflight.

Managed Compose inputs are frozen and uploaded unchanged. The runtime's official `docker compose config` resolves services after startup, including native `!reset` and `!override` tags; the SDK does not parse or merge managed YAML. Call `start_runtime()` before obtaining service handles.

The snapshot compatibility fingerprint excludes `allowed_tenants`. Restore still checks the tenant against the current allowlist first: expanding or reordering authorization preserves compatibility; revocation denies restore. Tool, bootstrap, storage and other captured runtime configuration changes remain incompatible. This preview changes the fingerprint format; drain old preview snapshots before upgrading. It does not add service-restart recovery or cross-node portability.
The operator, rather than the Harbor user, builds and publishes the tool image
from this directory and supplies the matching image-bridge binary in its context.

Checkpoint output includes `managed-runtime.json` with an opaque collection ID,
tenant and profile fingerprint, without credentials or node paths. Restore checks
this server-created metadata and reuses the retained collection. Missing or changed
profile/collection fails closed. It must not rebuild, restart Docker or substitute
an empty collection. The profile currently retains roots until service drain;
restore is same-node only. Production multi-tenant Registry tokens, durable leases,
restart recovery and artifact GC remain separate release requirements.

## Source and release boundary

[Qualification sources](qualification-sources.json) records the default-branch
baselines and the same-source Agent DX candidate used by this preview. AKernel's
default `main` predates the Agent DX migration; this branch retains that migration
stack, which already contains `main`, rather than reverting to the old backend.
Sandboxd is rebased onto its current `main`; the gVisor and distill-fs components
use complete checksum-verified releases matching their current defaults.

The candidate Agent DX platform/Execd package is built and verified through its
formal `make platform-release` entry. This is an isolated source qualification,
not a released production bundle. The current default archive download URLs are
unchanged and are not that candidate. Operators must explicitly stage the matched
verified package for this POC. Before release, publish matching platform and
Execd component archives and update both URL/checksum pairs atomically with the
source pins; do not build a production bundle using mismatched default artifacts.

The source-preview gitlinks use the matching `hky1999` Sandboxd and Agent DX
forks so submodule initialization can fetch the unmerged candidate commits.
Restore organization URLs together with reviewed release pins after merge.
