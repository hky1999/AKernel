"""压力观测、滞回、身份与发布边界的确定性检查。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("pressure", Path(__file__).with_name("scheduler-pressure.py"))
pressure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pressure)


def policy():
    return {"memory_high_per_mille": 750, "memory_low_per_mille": 650,
            "psi_high_per_mille": 20, "psi_low_per_mille": 5,
            "disk_high_per_mille": 900, "disk_low_per_mille": 800,
            "recovery_millis": 5000, "minimum_disk_available_bytes": 16 * 1024**3}


def metrics(**values):
    return dict({"host_memory_per_mille": 100, "parent_memory_per_mille": 100,
                 "host_memory_psi_per_mille": 0, "parent_memory_psi_per_mille": 0,
                 "disk_used_per_mille": 100, "parent_memory_current_bytes": 100,
                 "parent_memory_max_bytes": 1000, "disk_available_bytes": 32 * 1024**3}, **values)


class GateTests(unittest.TestCase):
    def test_initial_closed_hysteresis_and_continuous_low_window(self):
        gate = pressure.PressureGate(policy())
        self.assertFalse(gate.update(metrics(parent_memory_per_mille=700), 0)[0])
        self.assertFalse(gate.update(metrics(), 1)[0])
        self.assertFalse(gate.update(metrics(), 5000)[0])
        self.assertTrue(gate.update(metrics(), 5001)[0])
        self.assertTrue(gate.update(metrics(parent_memory_per_mille=700), 6000)[0])
        self.assertFalse(gate.update(metrics(parent_memory_per_mille=750), 6001)[0])
        self.assertFalse(gate.update(metrics(), 7000)[0])
        self.assertFalse(gate.update(metrics(parent_memory_per_mille=700), 11000)[0])
        self.assertFalse(gate.update(metrics(), 12000)[0])
        self.assertTrue(gate.update(metrics(), 17000)[0])
        gate.fail()
        self.assertFalse(gate.update(metrics(), 20000)[0])

    def test_each_high_signal_and_disk_headroom_close_independently(self):
        for name, value in [("host_memory_per_mille", 750), ("parent_memory_per_mille", 750),
                            ("host_memory_psi_per_mille", 20), ("parent_memory_psi_per_mille", 20),
                            ("disk_used_per_mille", 900), ("disk_available_bytes", 16 * 1024**3 - 1)]:
            with self.subTest(name=name):
                gate = pressure.PressureGate(policy()); gate.update(metrics(), 0)
                self.assertTrue(gate.update(metrics(), 5000)[0])
                self.assertEqual(gate.update(metrics(**{name: value}), 5001), (False, "high_watermark"))

    def test_invalid_policy_and_observations_are_rejected(self):
        for key, value in [("memory_low_per_mille", 750), ("psi_high_per_mille", 1001),
                           ("recovery_millis", 0), ("disk_low_per_mille", True)]:
            config = policy(); config[key] = value
            with self.assertRaises(ValueError): pressure.PressureGate(config)
        for text in ["", "full avg10=0.00", "some avg10=NaN", "some avg10=101", "some avg10=-1", "some avg10=1 avg10=2"]:
            with self.assertRaises(ValueError): pressure.psi_per_mille(text)
        self.assertEqual(pressure.psi_per_mille("some avg10=1.01 avg60=0.00 total=1\nfull avg10=0.00"), 11)
        for used, total in [(1, 0), (-1, 2), (3, 2)]:
            with self.assertRaises(ValueError): pressure.percentage(used, total)


class ObserverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); parent = self.root / "parent"; parent.mkdir()
        (parent / "memory.max").write_text("1000"); (parent / "cpu.max").write_text("400000 100000")
        state = self.root / "state"; state.mkdir(); st = state.stat()
        pins = [{"path": str(state), "device": st.st_dev, "inode": st.st_ino}]
        st = parent.stat()
        self.config = {"identity": {"host_id": "host", "authority": "authority", "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                                    "parent_device": st.st_dev, "parent_inode": st.st_ino, "storage_identity_sha256": pressure.identity_digest(pins)},
                       "parent_path": str(parent), "parent_memory_max_bytes": 1000, "parent_cpu_max": "400000 100000",
                       "storage_pins": pins, "disk_path": str(state), "output": str(self.root / "pressure.json"),
                       "valid_for_millis": 3000, "sample_interval_millis": 1000, "policy": policy()}
        self.observer = pressure.HostObserver(self.config); self.addCleanup(self.observer.close)

    def test_identity_bounds_and_storage_replacement_require_recovery(self):
        self.observer.verify()
        parent = Path(self.config["parent_path"])
        (parent / "memory.max").write_text("2000")
        with self.assertRaises(RuntimeError): self.observer.verify()
        (parent / "memory.max").write_text("1000")
        state = self.root / "state"; state.rename(self.root / "original"); state.mkdir()
        with self.assertRaises(RuntimeError): self.observer.verify()

    def test_atomic_publication_single_writer_and_failure_closes(self):
        with self.assertRaises(BlockingIOError): pressure.HostObserver(self.config)
        with patch.object(self.observer, "sample", return_value=metrics()), patch.object(pressure.time, "monotonic_ns", return_value=0):
            first = self.observer.step(); self.assertFalse(first["accepting"])
        for second in range(1, 6):
            with patch.object(self.observer, "sample", return_value=metrics()), patch.object(pressure.time, "monotonic_ns", return_value=second * 1000000000):
                self.assertEqual(self.observer.step()["accepting"], second == 5)
        with patch.object(self.observer, "sample", side_effect=OSError("lost")):
            closed = self.observer.step()
        self.assertFalse(closed["accepting"]); self.assertEqual(closed["reason"], "observation_failed")
        output = Path(self.config["output"])
        self.assertEqual(json.loads(output.read_text()), closed)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob(".pressure-*")), [])

    def test_sampling_gap_and_slow_sample_close_before_recovery(self):
        with patch.object(self.observer, "sample", return_value=metrics()):
            for second in range(6):
                with patch.object(pressure.time, "monotonic_ns", return_value=second * 1000000000):
                    self.observer.step()
            self.assertTrue(self.observer.gate.accepting)
            with patch.object(pressure.time, "monotonic_ns", return_value=9000000000):
                self.assertFalse(self.observer.step()["accepting"])
            with patch.object(pressure.time, "monotonic_ns", side_effect=[10000000000, 12000000000]):
                value = self.observer.step()
                self.assertFalse(value["accepting"])
                self.assertEqual(value["reason"], "observation_failed")

    def test_symlink_and_failed_rename_preserve_original(self):
        original = self.observer.publish(False, "starting", metrics())
        with patch.object(pressure.os, "replace", side_effect=OSError("failed rename")):
            with self.assertRaises(OSError): self.observer.publish(True, "low_watermark", metrics())
        output = Path(self.config["output"])
        self.assertEqual(json.loads(output.read_text()), original)
        self.assertEqual(list(self.root.glob(".pressure-*")), [])
        output.unlink(); other = self.root / "other"; other.write_text("preserve"); output.symlink_to(other)
        with self.assertRaises(RuntimeError): self.observer.publish(False, "starting", metrics())
        self.assertEqual(other.read_text(), "preserve")




class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.entry = self.load("entry", "scheduler-dev-entrypoint.py")
        self.launcher = self.load("launcher", "scheduler-dev.py")

    def load(self, name, file):
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_source_is_shared_and_cannot_be_silently_disabled(self):
        expanded = {"services": [{"role": "coordinator", "config": {}}, {"role": "adxlet", "config": {}}]}
        self.entry.configure_host_pressure(expanded, None)
        self.assertNotIn("host_pressure", expanded["services"][1]["config"])
        source = {"path": "/run/akernel-pressure/BareMetal1.json", "max_age_millis": 3000,
                  "identity": {"host_id": "BareMetal1"}}
        self.entry.configure_host_pressure(expanded, source)
        self.assertEqual(expanded["services"][0]["config"]["host_pressure_admission"], {"BareMetal1": source})
        self.assertEqual(expanded["services"][1]["config"]["host_pressure"], source)
        with self.assertRaises(RuntimeError): self.entry.configure_host_pressure(expanded, None)

    def test_launcher_requires_explicit_source_and_read_only_directory(self):
        image = "sha256:" + "a" * 64
        self.assertNotIn("AKERNEL_SCHEDULER_PRESSURE_SOURCE", str(self.launcher.commands(image)))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            source = {"path": "/run/akernel-pressure/BareMetal1.json", "max_age_millis": 3000,
                      "identity": {"host_id": "BareMetal1"}}
            path.write_text(json.dumps(source))
            command = self.launcher.commands(image, pressure_source=path)[1]
            self.assertIn(f"type=bind,source={path.parent},target=/run/akernel-pressure,readonly", command)
            self.assertIn("AKERNEL_SCHEDULER_PRESSURE_SOURCE=/run/akernel-pressure/source.json", command)
            source["identity"]["host_id"] = "other"; path.write_text(json.dumps(source))
            with self.assertRaises(ValueError): self.launcher.commands(image, pressure_source=path)

if __name__ == "__main__":
    unittest.main()
