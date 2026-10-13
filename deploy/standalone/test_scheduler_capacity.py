"""Verify bounded renewal and refusal of changed development capacity sources."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "scheduler_entrypoint", Path(__file__).with_name("scheduler-dev-entrypoint.py")
)
entrypoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entrypoint)


class FixedCapacityLeaseTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        (self.state / "adx").mkdir(parents=True)
        self.group = self.root / "cgroup"
        (self.group / "services").mkdir(parents=True)
        self.bounds()
        self.addCleanup(patch.stopall)
        patch.object(entrypoint, "STATE", self.state).start()
        patch.object(entrypoint, "CGROUP", self.group).start()
        patch.object(entrypoint.shutil, "disk_usage", return_value=(
            type("Disk", (), {"free": 32 * 1024**3})()
        )).start()
        self.clock = patch.object(entrypoint.time, "time", return_value=1000).start()
        entrypoint.write_capacity(self.state / "adx")
        self.lease = entrypoint.FixedCapacityLease()
        self.addCleanup(self.lease.close)

    def bounds(self):
        (self.group / "cpu.max").write_text("400000 100000\n")
        (self.group / "memory.max").write_text(str(16 * 1024**3))
        (self.group / "services/memory.max").write_text(str(4 * 1024**3))

    def test_renewal_keeps_fixed_capacity_and_expires_after_provider_stops(self):
        original = json.loads((self.state / "adx/capacity.json").read_text())
        self.clock.return_value = 2000
        self.lease.renew()
        path = self.state / "adx/capacity.json"
        renewed = json.loads(path.read_text())
        self.assertEqual(renewed["capacity"], original["capacity"])
        self.assertEqual(renewed["valid_until_unix_seconds"], 2060)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.clock.return_value = 2061
        self.assertLess(renewed["valid_until_unix_seconds"], entrypoint.time.time())

    def test_changed_limit_does_not_publish_a_new_lease(self):
        path = self.state / "adx/capacity.json"
        original = path.read_bytes()
        (self.group / "memory.max").write_text(str(32 * 1024**3))
        with self.assertRaises(RuntimeError):
            self.lease.renew()
        self.assertEqual(path.read_bytes(), original)

    def test_replaced_group_with_equal_numbers_is_refused(self):
        path = self.state / "adx/capacity.json"
        original = path.read_bytes()
        self.group.rename(self.root / "original-cgroup")
        (self.group / "services").mkdir(parents=True)
        self.bounds()
        with self.assertRaises(RuntimeError):
            self.lease.renew()
        self.assertEqual(path.read_bytes(), original)

    def test_other_writer_and_symlink_are_not_overwritten(self):
        path = self.state / "adx/capacity.json"
        path.write_text("another writer")
        with self.assertRaises(RuntimeError):
            self.lease.renew()
        self.assertEqual(path.read_text(), "another writer")
        path.unlink()
        other = self.root / "other.json"
        other.write_text("preserve foreign file")
        path.symlink_to(other)
        with self.assertRaises(RuntimeError):
            entrypoint.write_capacity(self.state / "adx")
        self.assertEqual(other.read_text(), "preserve foreign file")

    def test_second_provider_cannot_acquire_the_same_lease(self):
        with self.assertRaises(BlockingIOError):
            entrypoint.FixedCapacityLease()

    def test_initial_publication_cannot_overwrite_an_active_provider(self):
        path = self.state / "adx/capacity.json"
        original = path.read_bytes()
        self.clock.return_value = 3000
        with self.assertRaises(BlockingIOError):
            entrypoint.write_capacity(self.state / "adx")
        self.assertEqual(path.read_bytes(), original)


class HostBudgetConfigurationTest(unittest.TestCase):
    def test_budget_binding_rejects_ambiguous_identity_and_configuration(self):
        def expanded(node):
            return {"services": [
                {"role": "coordinator", "config": {}},
                {"role": "adxlet", "config": {"node_id": node}},
            ]}
        for node in (None, "", " node ", "${ADX_NODE_ID}"):
            with self.subTest(node=node), self.assertRaises(RuntimeError):
                entrypoint.configure_host_budget(expanded(node))
        for role in ("coordinator", "adxlet"):
            value = expanded("node")
            value["services"].append({"role": role, "config": {"node_id": "another"}})
            with self.subTest(role=role), self.assertRaises(RuntimeError):
                entrypoint.configure_host_budget(value)
        value = expanded("node")
        value["services"][0]["config"]["host_budgets"] = {"other-host": {}}
        with self.assertRaises(RuntimeError):
            entrypoint.configure_host_budget(value)
        self.assertEqual(value["services"][0]["config"]["host_budgets"], {"other-host": {}})
        value = expanded("node")
        entrypoint.configure_host_budget(value)
        original = json.dumps(value, sort_keys=True)
        entrypoint.configure_host_budget(value)
        self.assertEqual(json.dumps(value, sort_keys=True), original)

    def test_prepared_config_binds_fixed_budget_to_expanded_node_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            state.mkdir()
            output = Path(temporary) / "deployment.json"
            expanded = {
                "services": [
                    {"role": "coordinator", "config": {}},
                    {"role": "adxlet", "config": {"node_id": "operator-selected-node"}},
                ]
            }
            with patch.object(entrypoint, "STATE", state), \
                    patch.object(entrypoint.subprocess, "check_output", return_value=json.dumps(expanded)), \
                    patch.object(entrypoint.subprocess, "run") as validate:
                entrypoint.prepare_config(Path(__file__).parent / "config", output, "adxctl-test")
            prepared = json.loads(output.read_text())
            coordinator = prepared["services"][0]["config"]
            observed = json.loads((state / "adx/capacity.json").read_text())
            self.assertEqual(coordinator.get("host_budgets"), {
                "BareMetal1": {"capacity": observed["capacity"], "nodes": ["operator-selected-node"]}
            })
            self.assertEqual(observed["capacity"], {
                "cpu_millis": 3000, "memory_bytes": 12 * 1024**3, "disk_bytes": 16 * 1024**3
            })
            validate.assert_called_once_with(["adxctl-test", "--config", str(output), "validate"], check=True)


if __name__ == "__main__":
    unittest.main()
