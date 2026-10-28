"""Shared lower integrity, private upper restore config and one-time startup."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from akernel_sdk import DockerRuntime, ImageMountPlan, Sandbox
from akernel_sdk import _image_resolver as resolver
from akernel_sdk import sandbox as public


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class DockerRuntimeTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def plan(self):
        blobs = []
        for kind in ("config", "manifest"):
            payload = json.dumps({"kind": kind}).encode()
            name = kind + ".json"
            (self.directory / name).write_bytes(payload)
            blobs.append(
                {
                    "file": name,
                    "descriptor": {"digest": digest(payload), "size": len(payload)},
                }
            )
        target = blobs[1]["descriptor"]
        reference = "registry.example/test@" + target["digest"]
        image = {
            "reference": reference,
            "root": "/opt/akernel-images/" + target["digest"][7:],
            "target": target,
            "config": blobs[0]["descriptor"],
            "blobs": blobs,
            "chain_id": "sha256:" + "a" * 64,
        }
        path = self.directory / "plan.json"
        path.write_text(
            json.dumps({"images": [image], "services": {"main": reference}})
        )
        return path

    def test_integrity_and_path_escape_rejected_before_sandbox_creation(self):
        for defect in ("corrupt", "escape", "root"):
            with self.subTest(defect=defect):
                path = self.plan()
                plan = json.loads(path.read_text())
                if defect == "corrupt":
                    (self.directory / "config.json").write_bytes(b"changed")
                elif defect == "escape":
                    plan["images"][0]["blobs"][0]["file"] = "../private"
                else:
                    plan["images"][0]["root"] += "different"
                path.write_text(json.dumps(plan))
                with self.assertRaises(ValueError):
                    ImageMountPlan.load(path)

    def test_restore_options_retain_lower_and_upper_without_startup(self):
        runtime = DockerRuntime(
            images=ImageMountPlan.load(self.plan()), writable_mb=2048
        )
        backend = MagicMock()
        backend.from_snapshot.return_value.id = "restored"
        with patch.object(public, "load_backend", return_value=backend):
            for factory in (Sandbox.from_snapshot, Sandbox.create):
                options = runtime.sandbox_options()
                sb = factory("snapshot", **options)
                spec = backend.from_snapshot.call_args.args[0]
                reference = runtime.images.images[0]["reference"]
                self.assertEqual(spec.mounts[0].image_url, reference)
                upper = options["extra_config"]["fileBackedTmpfsMounts"][0]
                self.assertEqual(
                    spec.extra_config["fileBackedTmpfsMounts"][0]["sizeMB"], 2048
                )
                upper["sizeMB"] = 999
                self.assertEqual(
                    spec.extra_config["fileBackedTmpfsMounts"][0]["sizeMB"], 2048
                )
                backend.create.assert_not_called()
                backend.from_snapshot.return_value.commands.run.assert_not_called()
                sb.kill()

    def test_start_checks_return_codes_and_refuses_existing_daemon(self):
        sandbox = MagicMock()
        sandbox.commands.run.side_effect = RuntimeError("existing daemon")
        with self.assertRaisesRegex(RuntimeError, "existing daemon"):
            DockerRuntime().start(sandbox)
        self.assertEqual(sandbox.commands.run.call_count, 1)
        self.assertTrue(sandbox.commands.run.call_args.kwargs["check"])
        sandbox.files.copy_from_local.assert_not_called()

    def test_bridge_upload_is_sdk_owned_and_precedes_dockerd(self):
        sandbox = MagicMock()
        sandbox.commands.run.return_value = SimpleNamespace(
            stdout='["--config-file", "/bridge/docker.json"]', stderr=""
        )
        binary = self.directory / "bridge"
        binary.write_bytes(b"fixture")
        runtime = DockerRuntime(
            images=ImageMountPlan.load(self.plan()), bridge_binary=binary
        )
        runtime.start(sandbox, network="host")
        commands = [c.args[0] for c in sandbox.commands.run.call_args_list]
        start = next(i for i, c in enumerate(commands) if "start.py --root" in c)
        docker = next(i for i, c in enumerate(commands) if "dockerd --config-file" in c)
        self.assertLess(start, docker)
        uploads = [c.args[0] for c in sandbox.files.copy_from_local.call_args_list]
        self.assertTrue(
            any(p.endswith("akernel_sdk/_image_bridge_guest.py") for p in uploads)
        )
        self.assertFalse(any("harbor" in p for p in uploads))

    def test_registry_auth_pins_metadata_and_never_downloads_layers(self):
        config = json.dumps(
            {
                "os": "linux",
                "architecture": "amd64",
                "rootfs": {"diff_ids": ["sha256:" + "a" * 64]},
            }
        ).encode()
        manifest = json.dumps(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": digest(config),
                    "size": len(config),
                },
                "layers": [{"digest": "sha256:" + "b" * 64}],
            }
        ).encode()
        calls = []

        def handle(request):
            calls.append(request)
            if request.url.host == "auth.example":
                return httpx.Response(200, json={"token": "test-only"})
            if request.headers.get("Authorization") != "Bearer test-only":
                return httpx.Response(
                    401,
                    headers={
                        "www-authenticate": 'Bearer realm="https://auth.example/token",'
                        'scope="repository:library/test:pull"'
                    },
                )
            if "/manifests/" in request.url.path:
                return httpx.Response(
                    200,
                    content=manifest,
                    headers={"docker-content-digest": digest(manifest)},
                )
            if request.url.path.endswith(digest(config)):
                return httpx.Response(200, content=config)
            raise AssertionError("must never request a layer")

        with patch.dict("os.environ", {"DOCKER_CONFIG": str(self.directory)}):
            with httpx.Client(transport=httpx.MockTransport(handle)) as client:
                image = resolver.RegistryMetadata(client, self.directory).prepare(
                    "docker.io/library/test:latest"
                )
        self.assertEqual(
            image["reference"], "docker.io/library/test@" + digest(manifest)
        )
        self.assertEqual(len(image["blobs"]), 2)
        self.assertEqual(sum(r.url.host == "auth.example" for r in calls), 1)
