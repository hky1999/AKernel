import tempfile
import unittest
from pathlib import Path

from node_network import link_owned, namespace_matches, route_collision


class NetworkOwnershipTests(unittest.TestCase):
    def test_default_route_is_not_an_overlap(self):
        self.assertFalse(route_collision([{"dst": "default"}, {"dst": "172.25.16.0/20"}], "10.243.251.0/24"))

    def test_broader_or_narrower_existing_route_is_rejected(self):
        for route in ("10.243.0.0/16", "10.243.251.2/32", "10.243.251.0/24"):
            self.assertTrue(route_collision([{"dst": route}], "10.243.251.0/24"))

    def test_interface_requires_both_name_and_task_alias(self):
        self.assertTrue(link_owned({"ifname": "a0s", "ifalias": "task"}, "a0s", "task"))
        self.assertFalse(link_owned({"ifname": "a0s", "ifalias": "other"}, "a0s", "task"))
        self.assertFalse(link_owned({"ifname": "other", "ifalias": "task"}, "a0s", "task"))

    def test_namespace_identity_is_not_just_a_reused_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "namespace"
            path.touch()
            stat = path.stat()
            record = {"dev": stat.st_dev, "ino": stat.st_ino}
            self.assertTrue(namespace_matches(path, record))
            self.assertFalse(namespace_matches(path, {"dev": stat.st_dev, "ino": stat.st_ino + 1}))
            path.unlink()
            self.assertFalse(namespace_matches(path, record))


if __name__ == "__main__":
    unittest.main()
