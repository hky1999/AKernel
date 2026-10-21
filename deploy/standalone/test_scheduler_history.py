"""验证历史回收显式准入与持久配置，不启动容器或调用资源接口。"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entrypoint = load("history_entrypoint", "scheduler-dev-entrypoint.py")
launcher = load("history_launcher", "scheduler-dev.py")


class HistoryConfigurationTest(unittest.TestCase):
    def test_history_is_explicit_and_independent_of_memory(self):
        for memory in (False, True):
            for history in (False, True):
                command = launcher.commands("sha256:" + "a" * 64,
                                            elastic_memory=memory,
                                            history_reclamation=history)[1]
                self.assertEqual("AKERNEL_SCHEDULER_HISTORY_RECLAMATION=true" in command,
                                 history)
                self.assertEqual("AKERNEL_SCHEDULER_ELASTIC_MEMORY=true" in command,
                                 memory)

    def test_invalid_environment_cannot_silently_enable_or_disable_reclamation(self):
        for value, expected in ((None, False), ("false", False), ("true", True)):
            with patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["AKERNEL_SCHEDULER_HISTORY_RECLAMATION"] = value
                self.assertEqual(entrypoint.history_reclamation_enabled(), expected)
        for value in ("", "1", "TRUE", " true "):
            with patch.dict(os.environ, AKERNEL_SCHEDULER_HISTORY_RECLAMATION=value):
                with self.assertRaises(RuntimeError):
                    entrypoint.history_reclamation_enabled()

    def test_prepared_node_keeps_explicit_history_flag_and_fixed_budget(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp) / "state"
                state.mkdir()
                output = Path(tmp) / "deployment.json"
                expanded = {"services": [
                    {"role": "coordinator", "config": {}},
                    {"role": "adxlet", "config": {"node_id": "history-node"}},
                ]}
                with patch.object(entrypoint, "STATE", state), \
                        patch.object(entrypoint.subprocess, "check_output", return_value=json.dumps(expanded)), \
                        patch.object(entrypoint.subprocess, "run"):
                    entrypoint.prepare_config(Path(__file__).parent / "config", output,
                                              "adxctl-test", history_reclamation=enabled)
                prepared = json.loads(output.read_text())["services"]
                self.assertIs(prepared[1]["config"]["history_reclamation"], enabled)
                self.assertEqual(prepared[0]["config"]["host_budgets"]["BareMetal1"]["capacity"],
                                 entrypoint.fixed_capacity())
                self.assertNotIn("memory_control", prepared[0]["config"])

    def test_ambiguous_node_or_non_boolean_flag_is_refused(self):
        for services, flag in (([], True), ([{"role": "adxlet", "config": {}}], 1)):
            with self.assertRaises(RuntimeError):
                entrypoint.configure_history_reclamation({"services": services}, flag)
