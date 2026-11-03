import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location(
    "runtime_compose", Path(__file__).with_name("compose.py")
)
compose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compose)


class NativeBuildContract(unittest.TestCase):
    def test_service_discovery_uses_official_compose_and_returns_only_names(self):
        descriptor = {"project_name": "test", "directory": "/work", "files": ["/work/base.yaml", "/work/override.yaml"]}
        config = {"services": {"main": {"environment": {"SECRET": "private"}}}}
        with patch.object(compose, "execute", return_value=json.dumps(config)) as execute:
            self.assertEqual(compose.describe(descriptor), ["main"])
        self.assertEqual(execute.call_args.args[0], ["docker", "compose", "-p", "test", "--project-directory", "/work", "-f", "/work/base.yaml", "-f", "/work/override.yaml", "config", "--format", "json"])

    def test_native_build_definition_and_cache_inputs_are_preserved(self):
        native = {
            "context": "/work/app",
            "dockerfile": "Dockerfile",
            "target": "release",
            "args": {"MODE": "prod"},
            "secret": ["id=private,src=/run/session"],
            "cache-from": ["type=registry,ref=registry.test/cache:step"],
        }
        plan, refs = compose.build_plan(
            {},
            {"services": {"main": {"build": {}}}},
            {"target": {"main": dict(native)}},
            "registry.test",
            "poc/job",
        )
        actual = plan["target"]["main"]
        for key, value in native.items():
            self.assertEqual(actual[key], value)
        self.assertNotIn("no-cache", actual)
        self.assertNotIn("registry.insecure", actual["output"][0])
        self.assertIn("compression=nydus", actual["output"][0])
        self.assertEqual(refs, {"main": "registry.test/poc/job/main:built"})

    def test_image_only_conversion_uses_native_dockerfile_frontend(self):
        image = "registry.test/base@sha256:" + "a" * 64
        plan, _ = compose.build_plan(
            {},
            {"services": {"loadgen": {"image": image}}},
            {},
            "registry.test",
            "poc/job",
            True,
            True,
        )
        target = plan["target"]["loadgen"]
        self.assertEqual(target["dockerfile-inline"], "FROM " + image + "\n")
        self.assertTrue(target["no-cache"])
        self.assertIn("registry.insecure=true", target["output"][0])

    def test_namespace_owner_and_internal_network_use_native_inspection(self):
        relay = MagicMock(return_value={"url": "http://172.28.1.1:23456"})
        values = [
            [{"HostConfig": {"NetworkMode": "container:api-id"}}],
            [
                {
                    "HostConfig": {"NetworkMode": "project-private"},
                    "NetworkSettings": {
                        "Networks": {
                            "private": {"IPAddress": "172.28.1.2", "Gateway": ""}
                        }
                    },
                }
            ],
            [
                {
                    "IPAM": {
                        "Config": [{"Subnet": "172.28.1.0/24", "Gateway": "172.28.1.1"}]
                    }
                }
            ],
        ]
        with (
            patch.object(
                compose, "execute", side_effect=[json.dumps(v) for v in values]
            ) as execute,
            patch.dict(
                sys.modules, {"docker_network_guest": SimpleNamespace(expose=relay)}
            ),
        ):
            self.assertEqual(
                compose.expose_service("main-id", 8766), relay.return_value
            )
        self.assertEqual(
            execute.call_args_list[1].args[0], ["docker", "inspect", "api-id"]
        )
        self.assertEqual(
            relay.call_args.args[1],
            {"address": "172.28.1.1", "allowed": ["172.28.1.2"], "target_port": 8766},
        )

    def test_unsafe_repository_and_image_input_fail_before_build(self):
        for prefix, image in [
            ("poc/../other", "alpine:3"),
            ("poc/job", "ubuntu\nRUN bad"),
        ]:
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                compose.build_plan(
                    {},
                    {"services": {"main": {"image": image}}},
                    {},
                    "registry.test",
                    prefix,
                )


if __name__ == "__main__":
    unittest.main()
