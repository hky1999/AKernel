# Copyright (c) 2026 Ant Group Corporation.
# SPDX-License-Identifier: Apache-2.0
"""Validate shared checkpoints while preserving node-local runtime storage."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


def shared_mount(root: Path, mountinfo: str) -> None:
    """Reject a missing, covered, or local substitute before creating files."""
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("shared mount root must be an absolute path without symlinks")
    mounts = []
    for line in mountinfo.splitlines():
        left, separator, right = line.partition(" - ")
        fields, filesystem = left.split(), right.split()
        if not separator or len(fields) < 6 or len(filesystem) < 3:
            continue
        path = Path(
            re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
        )
        mounts.append((path, fields[5], filesystem))
    candidates = [item for item in mounts if item[0] == root]
    if len(candidates) != 1:
        raise ValueError("shared storage mount is missing, covered, or ambiguous")
    _, options, filesystem = candidates[0]
    kind, source = filesystem[:2]
    afs = (kind == "fuse" or kind.startswith("fuse.")) and source == "afs-dfs"
    juicefs = (
        kind == "fuse.juicefs" and source.startswith("JuiceFS:") and len(source) > 8
    )
    if (
        "rw" not in options.split(",")
        or "rw" not in filesystem[2].split(",")
        or not (afs or juicefs)
    ):
        raise ValueError(
            "shared storage requires a writable AFS or JuiceFS mount at mount-root"
        )
    if any(path != root and root in path.parents for path, _, _ in mounts):
        raise ValueError("shared storage contains a covering mount")


def validate_runtime_storage(source: str, mount_root: Path) -> None:
    """Keep runtime state and private writes on their configured local paths."""
    config = tomllib.loads(source)
    runtime = config.get("plugin", {}).get("runtime", {})
    paths = [
        config.get("rootDir"),
        config.get("storeDir"),
        runtime.get("filestore_dir"),
    ]
    firecracker_storage = runtime.get("firecracker", {}).get("storage_dir")
    if firecracker_storage not in (None, ""):
        paths.append(firecracker_storage)
    for value in paths:
        if not isinstance(value, str):
            raise TypeError(
                "sandboxd runtime storage paths must be strings; "
                "rootDir, storeDir, and filestore_dir are required"
            )
        directory = Path(value)
        if not directory.is_absolute() or ".." in directory.parts:
            raise ValueError(
                "sandboxd runtime paths must be absolute without traversal"
            )
        for path in (directory, directory.resolve()):
            if path == mount_root or mount_root in path.parents:
                raise ValueError(
                    "sandboxd runtime storage must remain outside "
                    "the shared checkpoint mount"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mount-root", type=Path, required=True)
    args = parser.parse_args()
    shared_mount(args.mount_root, Path("/proc/self/mountinfo").read_text())
    validate_runtime_storage(args.config.read_text(), args.mount_root)


if __name__ == "__main__":
    main()
