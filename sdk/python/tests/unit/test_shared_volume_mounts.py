"""Public shared volume requests, not real runtime/FS acceptance."""

import unittest
from unittest.mock import MagicMock, patch

from akernel_sdk import Sandbox, SharedVolumeMount
from akernel_sdk._backends.base import Capability
from akernel_sdk._backends.errors import UnsupportedBackendFeatureError
from akernel_sdk._shared_volume_mounts import normalize_shared_mounts

ID = "00000000-0000-4000-8000-000000000001"


class SharedVolumeMountTests(unittest.TestCase):
    def test_closed_wire_shape_and_default_read_only(self):
        mount = SharedVolumeMount(ID, "/shared")
        self.assertEqual(
            mount.to_dict(),
            {"volumeId": ID, "target": "/shared", "access": "read_only", "subpath": ""},
        )
        self.assertNotIn("hostPath", mount.to_dict())

    def test_bad_identity_path_or_access_is_rejected(self):
        for kwargs in (
            {"volume_id": "bad"},
            {"volume_id": "00000000-0000-0000-0000-000000000000"},
            {"target": "/dev/private"},
            {"target": "/shared/../outside"},
            {"subpath": "../outside"},
            {"subpath": "a//b"},
            {"access": "unknown"},
        ):
            values = dict(volume_id=ID, target="/shared")
            values.update(kwargs)
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaises((TypeError, ValueError)),
            ):
                SharedVolumeMount(**values)

    def test_duplicate_overlap_and_ordinary_alias_are_rejected(self):
        mount = SharedVolumeMount(ID, "/shared")
        with self.assertRaises(ValueError):
            normalize_shared_mounts([mount, SharedVolumeMount(ID, "/another")])
        with self.assertRaises(ValueError):
            normalize_shared_mounts([mount], ["//shared/./nested"])
        with self.assertRaises(TypeError):
            normalize_shared_mounts([{"hostPath": "/etc"}])
        self.assertEqual(normalize_shared_mounts(None), ())

    def test_sandbox_passes_immutable_request_to_the_backend(self):
        backend = MagicMock()
        backend.capabilities = frozenset({Capability.SHARED_VOLUMES})
        backend.create.return_value.id = "sandbox"
        mount = SharedVolumeMount(ID, "/shared", access="read_write")
        with patch("akernel_sdk.sandbox.load_backend", return_value=backend):
            sandbox = Sandbox(image="example/image:v1", shared_volumes=[mount])
            try:
                self.assertEqual(
                    backend.create.call_args.args[0].shared_volumes, (mount,)
                )
            finally:
                sandbox.kill()

    def test_unsupported_backend_never_silently_drops_the_mount(self):
        backend = MagicMock()
        backend.capabilities = frozenset()
        with patch("akernel_sdk.sandbox.load_backend", return_value=backend):
            with self.assertRaises(UnsupportedBackendFeatureError):
                Sandbox(
                    image="example/image:v1",
                    shared_volumes=[SharedVolumeMount(ID, "/shared")],
                )
        backend.create.assert_not_called()
