"""验证显式内存策略配置，不启动容器或修改物理资源。"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


entrypoint = module("memory_entrypoint", "scheduler-dev-entrypoint.py")
launcher = module("memory_launcher", "scheduler-dev.py")


class MemoryConfigurationTest(unittest.TestCase):
    def test_launcher_opt_in_is_explicit(self):
        for enabled in (False, True):
            commands = launcher.commands("sha256:" + "a" * 64, elastic_memory=enabled)
            run = commands[1]
            self.assertEqual("AKERNEL_SCHEDULER_ELASTIC_MEMORY=true" in run, enabled)
            self.assertEqual(run[-1], "sha256:" + "a" * 64)

    def test_environment_flag_rejects_ambiguous_values(self):
        for value, expected in ((None, False), ("false", False), ("true", True)):
            with patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["AKERNEL_SCHEDULER_ELASTIC_MEMORY"] = value
                self.assertEqual(entrypoint.elastic_memory_enabled(), expected)
        for value in ("", "1", "TRUE", " true "):
            with patch.dict(os.environ, AKERNEL_SCHEDULER_ELASTIC_MEMORY=value), \
                    self.assertRaises(RuntimeError):
                entrypoint.elastic_memory_enabled()

    def test_prepared_config_matches_daemon_and_keeps_host_budget(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp) / "state"
                state.mkdir()
                output = Path(tmp) / "deployment.json"
                expanded = {"services": [
                    {"role": "coordinator", "config": {}},
                    {"role": "adxlet", "config": {"node_id": "memory-node"}},
                ]}
                with patch.object(entrypoint, "STATE", state), \
                        patch.object(entrypoint.subprocess, "check_output", return_value=json.dumps(expanded)), \
                        patch.object(entrypoint.subprocess, "run") as validate:
                    entrypoint.prepare_config(Path(__file__).parent / "config", output,
                                              "adxctl-test", elastic_memory=enabled)
                prepared = json.loads(output.read_text())["services"][0]["config"]
                resource = tomllib.loads((state / "sandboxd/config.toml").read_text())["plugin"]["resource"]
                self.assertEqual(prepared["host_budgets"]["BareMetal1"]["capacity"],
                                 entrypoint.fixed_capacity())
                self.assertEqual(prepared["host_budgets"]["BareMetal1"]["nodes"], ["memory-node"])
                if enabled:
                    self.assertEqual(resource["memory_high_ratio"], 0.75)
                    self.assertIs(resource["cgroup_io_accounting"], True)
                    policy = prepared["memory_control"]
                    self.assertEqual(policy["grow_per_mille"], 650)
                    self.assertEqual(policy["shrink_per_mille"], 300)
                    self.assertEqual(policy["low_usage_millis"], 30000)
                    self.assertEqual(policy["operation_timeout_millis"], 10000)
                else:
                    self.assertNotIn("memory_high_ratio", resource)
                    self.assertNotIn("cgroup_io_accounting", resource)
                    self.assertNotIn("memory_control", prepared)
                validate.assert_called_once()

    def test_conflicting_policy_or_daemon_template_is_rejected(self):
        for enabled in (False, True):
            expanded = {"services": [{"role": "coordinator", "config": {
                "memory_control": {"grow_per_mille": 999}}}]}
            with self.subTest(enabled=enabled), self.assertRaises(RuntimeError):
                entrypoint.configure_memory_control(expanded, "[plugin.resource]\n", enabled)
        for text in ("[plugin.resource]\nmemory_high_ratio=0.9\n",
                     "[plugin.resource]\ncgroup_io_accounting=false\n", "[other]\n"):
            expanded = {"services": [{"role": "coordinator", "config": {}}]}
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                entrypoint.configure_memory_control(expanded, text, True)
