# Environment templates

Templates register an existing reusable snapshot in the cluster catalog. They
are tenant-owned, immutable records with their own ID, snapshot ID and display
name. Names are not aliases: multiple records may share a name, and creation
always uses a template ID. This MVP does not build Dockerfiles or import images.

The service must advertise `environment-templates-v1` at
`GET /api/environment/v1/capabilities`. Template startup checks this before
submitting a create, so an older service cannot ignore `templateId` and create
an ordinary fresh environment. Existing image and snapshot APIs are unchanged.

## Python

Install the SDK from the feature branch/commit and use a service built with the
matching Environment template changes. Installing only the new SDK does not
add a server catalog. The connection remains `AKERNEL_SERVER_ADDRESS`,
`AKERNEL_TOKEN`, optional `AKERNEL_GATEWAY_ADDRESS`, and the existing TLS policy.

```python
from akernel_sdk import Sandbox, Template

snapshot = None
template = None
try:
    with Sandbox(image="python:3.12-slim") as source:
        source.files.write("/tmp/template-marker", "initialized")
        snapshot = source.create_snapshot()

    template = Template.import_snapshot(snapshot, name="python-tools")
    print(Template.get(template.template_id))
    pages = Template.list(name="python-tools", limit=20)
    while pages.has_next:
        for item in pages.next_items():
            print(item.template_id, item.snapshot_id, item.name)

    with Sandbox.create(template=template.template_id) as child:
        assert child.files.read("/tmp/template-marker") == "initialized"
    # Sandbox.from_template(template) is equivalent.
finally:
    if template is not None:
        Template.delete(template)
    if snapshot is not None:
        Sandbox.delete_snapshot(snapshot)
```

`TemplateInfo(template_id, snapshot_id, name)` is backend-neutral. `get`,
`delete` and `Sandbox.create(template=...)` accept either the descriptor or ID.
`Client(...).Template` and `Client(...).Sandbox` bind both operations to the
same explicit cluster without changing process environment variables.

`Template.import_snapshot(snapshot_id, *, name, template_id=None)` accepts an
optional stable caller-generated ID (ASCII letters, digits, `_` and `-`, up to
128 characters). Otherwise the SDK allocates `tpl-<UUID>`. A transport failure
retains that ID in `BackendOperationError.template_id`. Query it to reconcile;
if absent, retry the same import with that explicit ID and unchanged contents.
There is no automatic import retry. Reusing an ID for different contents, or
reusing a deleted ID, fails. Names are limited to 256 UTF-8 bytes.

`Template.list(name=None, limit=100, next_token=None)` returns a lazy
`TemplatePaginator` with `has_next`, `next_token`, and `next_items()`. `limit`
is the page size (1..1000), and name filtering is exact. Failed reads leave the
cursor intact. These are live keyset pages, not a point-in-time catalog view.

## Creation and ownership

`Sandbox.create(template=...)` and `Sandbox.from_template(...)` use the snapshot
restore options: idle/scheduling/create budgets, name, command cwd, mounts,
ports, reverse tunnel, detached mode, failover and node selection. Runtime,
image and CPU/memory geometry are inherited. Fresh setup/geometry overrides
are rejected. `timeout` defaults to `schedule_timeout + 60`; it is a logical
create budget, not the running lifetime. Template IDs are not accepted as the
position argument: `Sandbox.create(snapshot_id)` and `from_snapshot()` retain
their existing snapshot semantics.

For a newly created Environment, the server reapplies the snapshot's captured
network policy. A different nonempty creation policy is rejected; omitted/empty
policy uses the captured policy. Direct snapshot restore keeps its existing
explicit-policy contract. Because the MVP does not return the effective policy
to the client, a template-created handle with omitted/empty policy must
successfully reapply its intended policy before calling `fork()`. It fails
with `NETWORK_POLICY_UNCONFIRMED` rather than silently forking without restrictions.

Named creation keeps AKernel's existing reuse semantics: if the logical sandbox
name already exists, it can return that existing environment without applying
the requested template or policy. Omit name or use a unique name for a distinct
child; use update_network_policy() explicitly to change an existing environment.
Ownership/rollback remains conservative on reuse.

Source sandbox, snapshot, registration and child sandbox lifetimes are separate:

- Import atomically creates the catalog record and its durable snapshot pin.
- `Template.delete()` prevents new template admission and releases only that
  registration's pin. The original snapshot and already admitted/running
  children survive. Published create retries may return their existing child.
- The snapshot remains caller-owned. `delete_snapshot()` rejects active
  template/restore users with `SNAPSHOT_IN_USE`; delete it explicitly after its
  users finish. Unregistering a template is not revocation of direct snapshot
  access for the owning tenant.
- Existing `kill()`/`close()`/detached behavior applies. `close()` disconnects
  locally. Do not use it as proof that remote resources were deleted.
- Snapshot locality and runtime compatibility remain backend-owned. A local
  snapshot remains source-node constrained; registration does not distribute it.

External mounts/services and local harness state are not captured automatically.
Use a clean initialization environment when registering templates intended for
reuse. Task-progress checkpoints remain separate from shared template seeds.

## CLI

```sh
ak template import-snapshot <snapshot-id> --name python-tools
ak template list --name python-tools --limit 20
ak template get <template-id>
ak template start <template-id> --idle-timeout 600
ak exec <returned-sandbox-id> -- cat /tmp/template-marker
ak delete <returned-sandbox-id>
ak template delete <template-id>
```

Import/get/list print JSON. List returns one page and its `next_token`; resume
with `--next-token`. Start prints the new sandbox ID, disconnects locally and
leaves the sandbox running until explicit `ak delete`, idle policy or platform
lifecycle action. Use the existing global TLS flags as needed. There is no
`ak template build`/alias management/E2B HTTP compatibility layer in this MVP.

## Shared storage and cross-node startup

Template registration preserves the snapshot's storage location. New reusable
snapshots are published through the source node's configured checkpoint store;
`import_snapshot()` neither uploads nor migrates an existing local artifact.
Use a source node configured with a shared store when publishing a template
intended for cross-node startup. Existing local snapshots require a new shared
capture or a separate controlled migration; register a new immutable template ID.

ADX implements `s3` and `distributed_fs`. AKernel's maintained Helm shared-storage
profile currently exposes `distributed_fs`; see the deployment-owned
[JuiceFS/AFS guide](../../../deploy/shared-storage/README.md). Configure execution
nodes and Coordinator's reclaimer with the same alias and actual storage
namespace. This backend checks real JuiceFS/AFS mount identity; a local directory
or an arbitrary RWX mount is not automatically accepted. Redis catalog data and
checkpoint data need independent persistent storage and recovery procedures.

Shared startup still requires compatible runtime/architecture and captured
resource geometry, access to the base image, and the matching storage alias on
the target node. By default shared restores may prefer the source node. Pass
`node_id=target_node` and inspect the committed placement to prove a cross-node
test. A successful create alone does not prove a second physical host was used.

The opt-in staged acceptance driver is
[`test_templates_shared.py`](../tests/integration/test_templates_shared.py).
It saves memory/PID/FD and file witnesses in a private receipt, restores two
independent children on a second host, and can verify that the same registration
survives source loss and deployment-owned restarts from a fresh client:

```sh
export AKERNEL_RUN_INTEGRATION=1
export AKERNEL_TEST_IMAGE='<python3-image>'
export AKERNEL_TEST_TEMPLATE_SOURCE_NODE='<source-node-id>'
export AKERNEL_TEST_TEMPLATE_TARGET_NODE='<target-node-id>'
export AKERNEL_TEST_TEMPLATE_STORAGE_ALIAS='<shared-alias>'
export AKERNEL_TEST_TEMPLATE_RECEIPT=/absolute/private/path/template-receipt.json
export AKERNEL_TEST_TEMPLATE_INSPECT_COMMAND='["/absolute/path/read-only-inspector"]'

python sdk/python/tests/integration/test_templates_shared.py --capture-receipt
python sdk/python/tests/integration/test_templates_shared.py \
  SharedTemplateIntegrationTest.test_cross_node_state_and_independent_writes -v

# With deployment-owned tools, retire only the dedicated source execution node.
# Recreate target-local runtime/cache, catalog Redis, storage processes and the
# control/API processes while retaining their intended persistent data volumes.
# Reconnect using the same service identity from a fresh client process.
export AKERNEL_TEST_TEMPLATE_SOURCE_OFFLINE=1
export AKERNEL_TEST_TEMPLATE_MIN_AGE_SECONDS=1800
python sdk/python/tests/integration/test_templates_shared.py \
  SharedTemplateIntegrationTest.test_retained_template_after_source_loss_and_restarts -v
```

The read-only inspector receives `environment|snapshot|node` and an ID as argv.
Environment results include committed `node_id` plus deployment-mapped
`physical_host`; snapshot results include `storage` and `source_node_id`; node
results include deployment-observed `online`. ADX's `adx-inspect` can provide the
committed metadata; the deployment supplies actual host mapping and current
node presence. Do not infer physical placement or online state from a stale
catalog record. The driver never shuts down hosts or mutates the deployment.
For a Kubernetes fixture, check node Pod/IP capacity and let the scheduler place
control/storage Pods using selectors or affinity. Binding every fixture Pod with
`nodeName` can bypass scheduling checks and cause replacement failures.

Keep an independent fault/UID/volume receipt for each restarted process. Report
the actual retention interval and whether a real host or only its isolated ADX
Pod was stopped. A bounded persistence/restart run does not establish multi-day
reliability, storage HA or cold-disk performance. Local FUSE/cache recreation
does not clear a shared host's global page cache.

The capture intentionally retains its template and snapshot between phases,
including on failure. After all consumers finish, explicitly delete the source
by its recorded ID, unregister the recorded template, and delete its snapshot.
Keep the receipt if cleanup is uncertain. Remove only test-owned deployment
resources and data; never retire an existing Harbor node to run this test.

After consumer cleanup, the recorded IDs can be released with:

```python
Sandbox.kill_by_id(receipt["source_id"])
Template.delete(receipt["template_id"])
Sandbox.delete_snapshot(receipt["snapshot_id"])
```

## Validation

Unit coverage exercises public API compatibility, explicit template wire fields,
capability rejection, pagination, connection isolation and unknown import
outcomes. Backend Redis tests exercise atomic pins, immutable IDs, tenant
authorization, deletion races and epoch/restart fencing. Real cluster tests:

```sh
AKERNEL_RUN_INTEGRATION=1 AKERNEL_TEST_IMAGE=<python3-image> \
python sdk/python/tests/integration/test_templates.py -v
```

Set `AKERNEL_TEST_NETWORK_URL` to a reachable test HTTP endpoint returning
`template-mvp-ready` to enable the network-policy case. Tests create only their
own registrations/snapshots/sandboxes and remove them on completion. The memory
test verifies process state, FD offsets, independent writes and a separate
verifier sandbox; it is not a Claude/DSH/Codex harness run or a performance claim.
