# Controlled shared volumes with ADX

Install the matching source `adx-environment` SDK before this source `akernel-sdk`. The published package with the same SDK version string may not include these experimental extensions. Keep the source commit manifest with an experiment.

`SharedVolumes` creates a persistent logical volume and manages owner/grantee permissions. `SharedVolumeMount` admits a directory into a sandbox. Neither is a public checkpoint catalog or an unrestricted host-path mount API.

```python
import uuid
from akernel_sdk import SharedVolumes

with SharedVolumes() as volumes:
    volume = volumes.create(request_id=str(uuid.uuid4()))
    volume = volumes.grant(
        volume.id,
        "YOUR-READER-TENANT",
        expected_permission_revision=volume.permission_revision,
        access="read_only",
    )
```

The authenticated API key determines the owner. Requests cannot select a different owner. Use the same request ID when resolving an uncertain create result; do not blindly retry with a new ID. Permission revisions use integer semantics, and stale updates fail closed.

In the owner's SDK process:

```python
from akernel_sdk import Sandbox, SharedVolumeMount

writer = Sandbox(
    runtime="runsc",
    failover=True,
    shared_volumes=[SharedVolumeMount(
        volume_id=volume.id, target="/mnt/shared", access="read_write"
    )],
)
writer.files.write("/mnt/shared/message.txt", "message from the writer")
writer.commands.run("sync")
```

Run the independent reader in another process with its own `AKERNEL_TOKEN` and the same verified TLS endpoint. Its mount uses `access="read_only"`; for cross-node acceptance explicitly select distinct source/target node IDs and verify actual placement. A successful file read is not by itself proof of placement.

The accepted policy is one RW writer with RO readers. It is not an accepted general multi-writer concurrent-update protocol. Basic close-write/new-reader visibility and explicit EROFS refusal were tested; instantaneous propagation to a reader that already cached a file must not be inferred from that test.

Shared volume contents are live application data, not the sandbox's private writable layer. A checkpoint does not roll them back. Restore admission rechecks the required storage identity and permissions; it must not silently substitute a local directory after denial.

Revocation blocks new admission. It does not prove that an existing running mount has stopped. Normal SDK delete is not independently sufficient evidence to collect backing files: runtime execution-bound retirement must complete first. Closing `SharedVolumes` only releases the client, not the persistent volume.

The same public mount contract is used for gVisor and Firecracker. Their private writable root filesystems and memory restoration mechanisms remain runtime-specific. See the CrossNode delivery manifest and acceptance scope for exactly which combinations were tested.
