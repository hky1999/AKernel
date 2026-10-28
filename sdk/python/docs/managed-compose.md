# Managed Compose integration

The SDK manages Docker and Compose inside one outer AKernel sandbox. Callers can keep original Dockerfiles and Compose definitions. It is an opt-in Linux/amd64 runsc profile, separate from the lightweight Dockerfile direct-launch parser.

## Component boundary

| Layer | Responsibility |
| --- | --- |
| Evaluation framework such as Harbor | Translate task resources, bind paths and verifier isolation; run the model proxy; choose checkpoint boundaries; persist aligned conversation state and fence the previous execution before recovery. |
| AKernel SDK | Discover startup-declared images, validate immutable OCI metadata, prepare mounts, bootstrap Docker/containerd/bridge once, build/up projects, expose service I/O and process identities, reconnect client tunnels and owned network rules. |
| Agent DX | Existing environment creation, exec/files/tunnel transport and reusable snapshot control. No Compose-aware server API is introduced. |
| Sandboxd | Existing shared ROFS image provider plus private, quota-accounted file-backed tmpfs mounts. Validate and clone managed filestores during checkpoint/restore. |
| runsc | Capture the entire outer sandbox, including dockerd, services, process memory, filesystem metadata and sockets. Reflink shares file-backed data; it does not turn RAM capture into a dirty-page incremental snapshot. |

The image bridge downloads only OCI manifests/configs on the client. Sandboxd's existing OCI/ROFS provider supplies shared lower roots and owns layer distribution/cache. Each sandbox has a separate writable upper. Client metadata credentials and node image-pull credentials are separate. There is no second shared layer cache in the SDK.

## Integration order and prerequisites

This implementation is based on the reusable snapshot/lifecycle SDK already merged into `develop`. It retains that implementation's creation ownership checks, atomic command identity, cancellation and rollback semantics. It does not include the older preview's metadata/lifetime/callback API extensions.

1. Review and merge Sandboxd's `fileBackedTmpfsMounts` support ([Sandboxd #83](https://github.com/inclusionAI/sandboxd/pull/83)). The AKernel consumer must then pin the merged Sandboxd revision and qualify a normal build before offering the file-backed profile. This SDK patch does not replace the existing gitlink or runtime bundle.
2. Configure runsc reflink mode, durable output sync, sufficient writable quota and a compatible backing/checkpoint filesystem. On Linux 5.10 these paths must use the same mount object. The separate [AKernel #126](https://github.com/inclusionAI/AKernel/pull/126) adds an explicit Helm profile without changing defaults. Docker 28 also requires operator-enabled `--net-raw=true` and `--allow-packet-socket-write=true`.
3. Build the matching [image bridge](../../../builder/image-bridge/README.md) and provide an outer image containing Docker 28.3.3, containerd/ctr, Python 3, Bash, mount/findmnt, iproute and legacy iptables tools. No user task Dockerfile changes are needed.
4. Install the SDK revision containing `ComposeProject`, then use [the maintained example](../examples/compose_checkpoint.py). An independent verifier prepares its own project and outer sandbox; it does not attach to the agent sandbox.

The SDK never updates node configuration or deployments. The snapshot/restore API has no client switch that can enable reflink on an unconfigured backend.

## Fresh creation and recovery

`ComposeProject.prepare()` creates a frozen local context and resolves image inputs before sandbox creation. Pass `project.sandbox_options()` into creation, then call `start_runtime()`, `build()`, `pull()` and `up()` at explicit boundaries. A partial startup is not replayable on that project object; the owner must clean up its newly created sandbox.

For checkpointing, quiesce tools/model requests, stop the real process tree using `ServiceProcessHandle`, and call `sandbox.create_snapshot()`. Persist the snapshot ID together with the project descriptor, process descriptor and application state. Resume the source tree if the source continues.

To recover, use `Sandbox.from_snapshot(..., **project.sandbox_options())` with the original upper geometry, mounts, current network policy and reverse tunnel. Fence the old execution before continuing the restored process. `ComposeProject.attach()` reconstructs local bindings only; `reconnect_network()` updates SDK-owned forwarding/NAT for the new outer TAP. `ServiceProcessHandle.attach()` checks Docker container ID/start time and Linux PID/start time. Continue the captured process without rerunning `build`, `up` or its launch command. Unknown process-launch outcomes retain a token for observation and must not be blindly resubmitted.

Harbor's Python memory and local model proxy are outside the remote snapshot. Their aligned state remains the framework's responsibility. A service-specific controlled TCP entry makes the existing outer reverse tunnel reachable from a bridge container without changing HTTP, auth headers or SSE.

## Supported scope and verification

The default profile retains native default/custom IPv4 bridges, service DNS/aliases and TCP/UDP userland port mappings. The opt-in `tests/integration/test_compose_network.py` checks existing inner and published-port TCP sockets across two delete/restore cycles, as well as peer-restricted model HTTP/auth/SSE and new egress. It requires a dedicated compatible test endpoint and is not run against a shared production deployment by CI. External TCP address migration, IPv6-only egress, non-bridge drivers, remote build contexts, arbitrary runtime-created images and complete Docker content-store/export compatibility are not claimed. Managed process handles require Python 3 in that service; ordinary service exec does not.

The earlier Harbor prototype passed original-process Claude C/R and an independent verifier on unchanged kv-live-surgery inputs. Those measurements predate this migration onto the current `develop` SDK and do not qualify the new revision or its newly pinned upstream bundle. One earlier prototype checkpoint returned no result before its 300-second deadline despite completed serialization; the later successful cycles did not establish a root cause. Keep fresh isolated end-to-end qualification as a merge/release requirement.

Local review checks are `make sdk-check` and `CGO_ENABLED=0 go test ./...` under `builder/image-bridge`; CI also vets and builds the bridge and validates clean wheel/sdist installs. Existing upstream ownership/fork/rollback tests stay in place.
