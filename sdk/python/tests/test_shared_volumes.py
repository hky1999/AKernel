import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import Mock, patch

from akernel_sdk import SharedVolumeGrant, SharedVolumeInfo, SharedVolumes
from akernel_sdk._backends.errors import UnsupportedBackendFeatureError

ID = "00000000-0000-0000-0000-000000000001"


def value():
    return {
        "id": ID,
        "ownerTenantId": "owner",
        "permissionRevision": "2",
        "grants": [{"tenantId": "reader", "subpath": "project", "access": "read_only"}],
    }


class PublicVolumeTests(unittest.TestCase):
    def setUp(self):
        self.native = Mock()
        for name in ("create", "get", "grant", "revoke"):
            getattr(self.native, name).return_value = value()
        self.backend = Mock()
        self.backend.shared_volumes.return_value = self.native
        self.patch = patch(
            "akernel_sdk.shared_volumes.load_backend", return_value=self.backend
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = SharedVolumes()
        self.addCleanup(self.client.close)

    def test_public_values_do_not_leak_native_sdk_types(self):
        volume = self.client.get(ID)
        self.assertIsInstance(volume, SharedVolumeInfo)
        self.assertEqual(volume.permission_revision, 2)
        self.assertEqual(
            volume.grants, (SharedVolumeGrant("reader", "project", "read_only"),)
        )
        with self.assertRaises(FrozenInstanceError):
            volume.id = "other"

    def test_create_keeps_the_callers_retry_identity(self):
        self.client.create(request_id=ID)
        self.native.create.assert_called_once_with(request_id=ID)

    def test_grant_passes_explicit_revision_and_default_read_only(self):
        self.client.grant(ID, "reader", expected_permission_revision=1)
        self.native.grant.assert_called_once_with(
            ID, "reader", expected_permission_revision=1, access="read_only", subpath=""
        )

    def test_revoke_does_not_call_sandbox_delete(self):
        self.client.revoke(
            ID, "reader", expected_permission_revision=2, subpath="project"
        )
        self.native.revoke.assert_called_once_with(
            ID, "reader", expected_permission_revision=2, subpath="project"
        )
        self.native.delete.assert_not_called()

    def test_context_manager_only_closes_the_metadata_client(self):
        with SharedVolumes() as client:
            client.get(ID)
        self.native.close.assert_called_once()
        self.native.delete.assert_not_called()

    def test_unsupported_backend_is_explicit(self):
        with patch("akernel_sdk.shared_volumes.load_backend", return_value=object()):
            with self.assertRaises(UnsupportedBackendFeatureError):
                SharedVolumes()


if __name__ == "__main__":
    unittest.main()
