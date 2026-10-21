# Copyright (c) 2026 Ant Group Corporation.
# Licensed under the Apache License, Version 2.0.
"""Public, backend-neutral shared volume permission management."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._backends.errors import UnsupportedBackendFeatureError
from ._backends.registry import load_backend


@dataclass(frozen=True)
class SharedVolumeGrant:
    """Permission for new admissions; revocation does not stop a live mount."""

    tenant_id: str
    subpath: str
    access: str


@dataclass(frozen=True)
class SharedVolumeInfo:
    """Persistent identity and ACL, not evidence of filesystem/mount readiness."""

    id: str
    owner_tenant_id: str
    permission_revision: int
    grants: tuple[SharedVolumeGrant, ...]

    @classmethod
    def _from_native(cls, value: Any) -> SharedVolumeInfo:
        return cls(
            id=str(value["id"]),
            owner_tenant_id=str(value["ownerTenantId"]),
            permission_revision=int(value["permissionRevision"]),
            grants=tuple(
                SharedVolumeGrant(
                    tenant_id=str(grant["tenantId"]),
                    subpath=str(grant["subpath"]),
                    access=str(grant["access"]),
                )
                for grant in value["grants"]
            ),
        )


class SharedVolumes:
    """Manage authenticated volume metadata without exposing host paths.

    Closing the client does not delete a volume. Mounting and filesystem
    readiness are separate node operations, not implied by create().
    """

    def __init__(self) -> None:
        factory = getattr(load_backend(), "shared_volumes", None)
        if not callable(factory):
            raise UnsupportedBackendFeatureError(
                "The selected backend does not support shared volume management."
            )
        self._native = factory()

    def create(self, *, request_id: str | None = None) -> SharedVolumeInfo:
        """Use the same UUID to retry an uncertain creation outcome."""
        return SharedVolumeInfo._from_native(self._native.create(request_id=request_id))

    def get(self, volume_id: str) -> SharedVolumeInfo:
        return SharedVolumeInfo._from_native(self._native.get(volume_id))

    def grant(
        self,
        volume_id: str,
        tenant_id: str,
        *,
        expected_permission_revision: int,
        access: str = "read_only",
        subpath: str = "",
    ) -> SharedVolumeInfo:
        return SharedVolumeInfo._from_native(
            self._native.grant(
                volume_id,
                tenant_id,
                expected_permission_revision=expected_permission_revision,
                access=access,
                subpath=subpath,
            )
        )

    def revoke(
        self,
        volume_id: str,
        tenant_id: str,
        *,
        expected_permission_revision: int,
        subpath: str = "",
    ) -> SharedVolumeInfo:
        return SharedVolumeInfo._from_native(
            self._native.revoke(
                volume_id,
                tenant_id,
                expected_permission_revision=expected_permission_revision,
                subpath=subpath,
            )
        )

    def close(self) -> None:
        self._native.close()

    def __enter__(self) -> SharedVolumes:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
