from pathlib import Path
import unittest

from native_profile import deployment, sandboxd_config


ROOT = Path("/mnt/test/cn-a0-adx-juicefs-test-jpa2cd")


class NativeProfileTests(unittest.TestCase):
    def test_shared_storage_explicitly_clears_legacy_template_directory(self):
        for node in ("source", "target"):
            for runtime in ("runsc", "firecracker"):
                config = deployment(ROOT, "native04", node, "volume", "password", runtime)
                node_config = config["service_overrides"]["adxlet"]["config"]
                self.assertIn("checkpoint_dir", node_config)
                self.assertIsNone(node_config["checkpoint_dir"])
                self.assertEqual(node_config["checkpoint_storage"]["kind"], "shared_fs")

    def test_same_shared_volume_but_independent_local_state(self):
        source = deployment(ROOT, "native01", "source", "volume", "password", "runsc")
        target = deployment(ROOT, "native01", "target", "volume", "password", "runsc")
        a = source["service_overrides"]["adxlet"]["config"]
        b = target["service_overrides"]["adxlet"]["config"]
        self.assertEqual(a["checkpoint_storage"]["root"], b["checkpoint_storage"]["root"])
        self.assertEqual(a["checkpoint_storage"]["volume_uuid"], b["checkpoint_storage"]["volume_uuid"])
        self.assertNotEqual(a["checkpoint_storage"]["local_root"], b["checkpoint_storage"]["local_root"])
        self.assertNotEqual(a["node_id"], b["node_id"])
        self.assertEqual(source["namespace"], target["namespace"])

    def test_gc_is_disabled_and_capture_is_direct_on_shared_volume(self):
        for node in ("source", "target"):
            d = deployment(ROOT, "native01", node, "volume", "password", "runsc")
            config = d["service_overrides"]["adxlet"]["config"]
            self.assertIs(config["checkpoint_gc"]["enabled"], False)
            self.assertEqual(config["checkpoint_storage"]["kind"], "shared_fs")
            self.assertEqual(config["checkpoint_storage"]["mount_root"], str(ROOT / "mount"))

    def test_storage_barrier_and_filestore_are_not_local_aliases(self):
        for runtime in ("runsc", "firecracker"):
            config = sandboxd_config(ROOT, "native01", "source", runtime)
            self.assertIn('filestore_dir = "' + str(ROOT / "mount" / "runtime" / "native01" / "source" / "filestore") + '"', config)
            self.assertIn('filestore_dir_size = ""', config)
            self.assertIn("sync_output = true", config)
            self.assertIn("disable_cgroup = false", config)

    def test_fc_explicitly_uses_buffered_async_and_paired_initrd(self):
        config = sandboxd_config(ROOT, "native02", "target", "firecracker")
        self.assertIn('writable_io_engine = "Async"', config)
        self.assertNotIn("AsyncDirect", config)
        self.assertIn(str(ROOT / "bin" / "initrd-a0.img"), config)
        self.assertIn("virtiofs_enabled = false", config)

    def test_embedded_ingress_uses_task_certificates_and_dedicated_tls_port(self):
        d = deployment(ROOT, "native03", "source", "volume", "password", "runsc")
        env = d["service_overrides"]["ingress"]["env"]
        self.assertEqual(env["ADX_DATA_PLANE_INGRESS_TLS_BIND"], "0.0.0.0:27443")
        self.assertEqual(env["ADX_DATA_PLANE_INGRESS_TLS_CERT"],
                         str(ROOT / "secrets" / "native-pki" / "ingress.pem"))
        self.assertEqual(env["ADX_DATA_PLANE_INGRESS_TLS_KEY"],
                         str(ROOT / "secrets" / "native-pki" / "ingress.key"))


if __name__ == "__main__":
    unittest.main()
