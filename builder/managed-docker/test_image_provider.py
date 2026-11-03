import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

module_spec = importlib.util.spec_from_file_location(
    "provider", Path(__file__).with_name("image_provider.py")
)
provider = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(provider)


class ImageProviderTests(unittest.TestCase):
    def test_native_docker_and_flat_credentials_match_node_parser(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "auth.json"
            for config in (
                {"registry.test": {"Auth": "test-value"}},
                {"auths": {"registry.test": {"auth": "test-value"}}},
                {"auths": {"https://registry.test/v1/": {"auth": "test-value"}}},
            ):
                path.write_text(json.dumps(config))
                self.assertEqual(provider.registry_auth(path, "registry.test"), "test-value")
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    provider.registry_auth(path, "other.test")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        auth = self.root / "auth.json"
        auth.write_text(json.dumps({"registry.test": {"Auth": "dGVzdDp0ZXN0"}}))
        self.provider = provider.ImageProvider(
            self.root / "collection", "registry.test", auth
        )

    def test_only_authorized_digest_references_are_accepted(self):
        digest = provider.digest(b"artifact")
        self.assertEqual(
            self.provider.validate_reference("registry.test/poc/main@" + digest),
            ("poc/main", digest),
        )
        for value in [
            "registry.test/poc/main:latest",
            "foreign.test/poc/main@" + digest,
            "registry.test/private/main@" + digest,
            "registry.test/poc/../other@" + digest,
            "registry.test/poc/main@sha256:bad",
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.provider.validate_reference(value)

    def test_invalid_reference_cannot_start_mount(self):
        self.provider.mount_image = lambda _: self.fail(
            "invalid request reached privileged mount"
        )
        with self.assertRaises(ValueError):
            self.provider.prepare("registry.test/poc/main:latest")

    def test_digest_chain_matches_oci_definition(self):
        first, second = provider.digest(b"one"), provider.digest(b"two")
        self.assertEqual(provider.chain_id([first]), first)
        self.assertEqual(
            provider.chain_id([first, second]),
            provider.digest((first + " " + second).encode()),
        )
        with self.assertRaises(ValueError):
            provider.chain_id([])

    def test_plan_is_published_atomically_and_read_only(self):
        plan = {"images": [{"reference": "kept"}], "automatic": True}
        provider.atomic_json(self.provider.plan, plan)
        self.assertEqual(json.loads(self.provider.plan.read_text()), plan)
        self.assertFalse(self.provider.plan.with_name("plan.json.new").exists())
        self.assertEqual(self.provider.plan.stat().st_mode & 0o222, 0)

    def test_prepared_reference_reuses_existing_read_only_root(self):
        reference = "registry.test/poc/main@" + provider.digest(b"artifact")
        provider.atomic_json(
            self.provider.plan,
            {"images": [{"reference": reference}], "automatic": True},
        )
        self.provider.mount_image = lambda _: self.fail(
            "existing registration was mounted twice"
        )
        self.provider.validate_mount = lambda _: None
        self.assertTrue(self.provider.prepare(reference)["reused"])

    def test_same_digest_alias_does_not_stack_bind_mounts(self):
        identity = provider.digest(b"artifact")
        existing = {
            "reference": "registry.test/poc/one@" + identity,
            "root": "/opt/akernel-images/" + identity[7:],
            "chain_id": "chain",
            "target": {},
            "config": {},
            "blobs": [],
        }
        provider.atomic_json(
            self.provider.plan, {"images": [existing], "automatic": True}
        )
        reference = "registry.test/poc/two@" + identity
        self.provider.describe = lambda _: dict(existing, reference=reference)
        self.provider.validate_mount = lambda _: None
        self.provider.mount_image = lambda _: self.fail(
            "same immutable root was mounted twice"
        )
        self.assertTrue(self.provider.prepare(reference)["reused"])
        self.assertEqual(len(json.loads(self.provider.plan.read_text())["images"]), 2)

    def test_missing_registered_mount_fails_without_replacing_live_root(self):
        reference = "registry.test/poc/main@" + provider.digest(b"artifact")
        provider.atomic_json(
            self.provider.plan,
            {"images": [{"reference": reference}], "automatic": True},
        )

        def unavailable(_):
            raise RuntimeError("missing root")

        self.provider.validate_mount = unavailable
        self.provider.mount_image = lambda _: self.fail(
            "missing live root was silently replaced"
        )
        with self.assertRaisesRegex(RuntimeError, "missing root"):
            self.provider.prepare(reference)


if __name__ == "__main__":
    unittest.main()
