"""Build submission and opaque runtime artifact handles; no Dockerfile parser."""

from __future__ import annotations

import json
import re
import shlex
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ._backends.errors import BackendOperationError, retain_failure_context

if TYPE_CHECKING:
    from .sandbox import Sandbox


@dataclass(frozen=True)
class BuiltImage:
    """Immutable image and public process defaults returned by the runtime."""

    reference: str
    envs: dict[str, str]
    cwd: str | None = None
    user: str | None = None


def upload_context(sandbox: Sandbox, source: Path, destination: str) -> None:
    """Transfer a directory as ordinary files/links, retaining native build inputs."""
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Build context must be a directory")
    archive = destination + ".upload.tar.gz"
    with tempfile.TemporaryDirectory(prefix="akernel-context-") as temporary:
        local = Path(temporary) / "context.tar.gz"
        with tarfile.open(local, "w:gz", dereference=False) as output:
            output.add(source, arcname=".")
        sandbox.files.copy_from_local(str(local), archive)
    sandbox.commands.run(
        shlex.join(["mkdir", "-p", destination]), timeout=20, check=True
    )
    sandbox.commands.run(
        shlex.join(["tar", "-xzf", archive, "-C", destination]), timeout=120, check=True
    )
    sandbox.commands.run(shlex.join(["rm", "-f", archive]), timeout=20, check=True)


def build_image(
    context: str | Path,
    *,
    profile: str,
    no_cache: bool = False,
    timeout: int = 1800,
    schedule_timeout: int = 180,
    log_path: str | Path | None = None,
) -> BuiltImage:
    """Submit an original context through an authorized platform build profile.

    The platform supplies the builder image, endpoint, publication credentials
    and image provider. No host Docker daemon, helper binary or Registry route
    is needed on this client. The temporary preparation Sandbox is deleted on
    success/failure; unknown outcomes are not replayed. Cleanup errors preserve
    the preparation Sandbox identity. The returned image lifetime follows the
    platform profile's retention contract. Timeouts are seconds.
    """
    from ._docker_runtime import DockerRuntime
    from .sandbox import Sandbox

    options = DockerRuntime(profile=profile).sandbox_options()
    source = Path(context).resolve(strict=True)
    if not source.is_dir() or not (source / "Dockerfile").is_file():
        raise ValueError("Native build requires a context containing Dockerfile")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ValueError("Build timeout must be a positive integer")
    failure: BaseException | None = None
    builder = Sandbox(
        cpu=1000,
        memory=2048,
        storage_mb=8192,
        idle_timeout=max(300, timeout + 60),
        schedule_timeout=schedule_timeout,
        **options,
    )
    try:
        upload_context(builder, source, "/work/context")
        result = builder.commands.run(
            shlex.join(
                [
                    "python3",
                    "/usr/lib/akernel/compose.py",
                    "image-build",
                    "--context",
                    "/work/context",
                    "--result",
                    "/work/artifact.json",
                    *(["--no-cache"] if no_cache else []),
                ]
            ),
            timeout=timeout,
        )
        if log_path is not None:
            target = Path(log_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(result.stdout + result.stderr)
        from .commands import _checked_result

        _checked_result(result, True)
        handle = json.loads(
            builder.commands.run(
                "cat /work/artifact.json", timeout=20, check=True
            ).stdout
        )
        reference = handle["reference"]
        if (
            not isinstance(reference, str)
            or re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", reference) is None
        ):
            raise ValueError("Runtime returned an invalid artifact reference")
        process = handle.get("image_process", {})
        return BuiltImage(
            reference=reference,
            envs=dict(
                value.split("=", 1) for value in process.get("Env", []) if "=" in value
            ),
            cwd=process.get("WorkingDir") or None,
            user=process.get("User") or None,
        )
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            builder.kill()
        except BaseException as cleanup:
            if failure is not None:
                retain_failure_context(failure, builder.id, cleanup)
            else:
                cleanup_failure = BackendOperationError(
                    "Managed build preparation cleanup failed",
                    environment_id=builder.id,
                )
                retain_failure_context(cleanup_failure, builder.id, cleanup)
                raise cleanup_failure from cleanup
