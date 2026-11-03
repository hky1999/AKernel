"""Runtime-side Compose/Buildx adapter. Docker owns Dockerfile/Compose semantics.

The client uploads a context and invokes this tool; image metadata and ROFS
registration remain on the runtime side. This preview uses an operator-issued
session capability and pins its complete collection until session cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

# Platform-owned harness dependency, resolved by native BuildKit.
PYTHON_HELPER_IMAGE = "docker.io/library/python@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8"
PYTHON_HELPER_ROOT = "/opt/akernel/harness-python"


def helper_targets(
    targets: dict, exports: dict[str, str], helper_image: str
) -> dict[str, str]:
    """Append one reusable tools layer through native linked Bake targets."""

    def fresh(base: str) -> str:
        name = base
        while name in targets:
            name += "_"
        return name

    helper = fresh("akernel_harness_python")
    targets[helper] = {
        "platforms": ["linux/amd64"],
        "dockerfile-inline": "FROM " + helper_image + " AS prepare\n"
        "RUN apk add --no-cache patchelf\n"
        "RUN set -eu; root=" + PYTHON_HELPER_ROOT + "; "
        'mkdir -p "$root/usr"; cp -a /lib "$root/lib"; '
        'cp -a /usr/lib "$root/usr/lib"; cp -a /usr/local "$root/usr/local"; '
        'binary=$(readlink -f "$root/usr/local/bin/python3"); '
        'patchelf --set-interpreter "$root/lib/ld-musl-x86_64.so.1" '
        "--force-rpath --set-rpath '$ORIGIN/../lib:$ORIGIN/../../../lib:$ORIGIN/../../lib' "
        '"$binary"; '
        '"$root/usr/local/bin/python3" -I -S -c "import json,ssl,sqlite3,ctypes,subprocess,sys; '
        "subprocess.run([sys.executable,'-I','-S','-c','import json'],check=True)\"\n"
        "FROM scratch\nCOPY --from=prepare "
        + PYTHON_HELPER_ROOT
        + " "
        + PYTHON_HELPER_ROOT
        + "\n",
    }
    result = {}
    for source in exports:
        name = fresh("akernel_export_" + source)
        original = targets[source]
        output = {
            k: original.pop(k) for k in ["tags", "output", "attest"] if k in original
        }
        targets[name] = {
            "platforms": ["linux/amd64"],
            "dockerfile-inline": "FROM task\nCOPY --link --from=harness "
            + PYTHON_HELPER_ROOT
            + " "
            + PYTHON_HELPER_ROOT
            + '\nENV PATH="$PATH:'
            + PYTHON_HELPER_ROOT
            + '/usr/local/bin"\n',
            "contexts": {"task": "target:" + source, "harness": "target:" + helper},
            **output,
        }
        result[source] = name
    return result


def execute(args: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(
        args, check=True, text=True, stdout=subprocess.PIPE if capture else None
    )
    return result.stdout if capture else ""


def compose_command(descriptor: dict) -> list[str]:
    command = [
        "docker",
        "compose",
        "-p",
        descriptor["project_name"],
        "--project-directory",
        descriptor["directory"],
    ]
    for filename in descriptor["files"]:
        command.extend(["-f", filename])
    return command


def describe(descriptor: dict) -> list[str]:
    """Resolve service identities natively, without exposing interpolated secrets."""
    os.environ.update(descriptor.get("environment", {}))
    config = json.loads(
        execute(
            [*compose_command(descriptor), "config", "--format", "json"], capture=True
        )
    )
    services = config.get("services", {})
    if not services:
        raise ValueError("Compose project requires at least one active service")
    return list(services)


def build_plan(
    descriptor: dict,
    config: dict,
    bake: dict,
    registry: str,
    prefix: str,
    no_cache: bool = False,
    registry_insecure: bool = False,
    helper_image: str | None = None,
) -> tuple[dict, dict[str, str]]:
    """Adapt native Bake targets' output only; do not interpret Dockerfiles."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9/_-]*", prefix) or ".." in prefix:
        raise ValueError("Invalid session repository prefix")
    targets = bake.setdefault("target", {})
    references = {}
    for name, service in config["services"].items():
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
            raise ValueError("Unsupported Compose service name")
        target = targets.get(name)
        if target is None:
            # Image-only inputs use the official Dockerfile frontend for format
            # conversion, retaining OCI config. No Python layer reconstruction.
            image = service.get("image")
            if not isinstance(image, str) or any(c.isspace() for c in image):
                raise ValueError("Service requires an image or native build target")
            target = {"dockerfile-inline": "FROM " + image + "\n"}
            targets[name] = target
        reference = registry + "/" + prefix.rstrip("/") + "/" + name.lower() + ":built"
        references[name] = reference
        target.update(
            {
                "tags": [reference],
                "platforms": ["linux/amd64"],
                "attest": [],
                "output": [
                    "type=image,push=true,compression=nydus,force-compression=true,"
                    "oci-mediatypes=true,fs-version=5"
                    + (",registry.insecure=true" if registry_insecure else "")
                ],
            }
        )
        if no_cache:
            target["no-cache"] = True
    selected = (
        helper_targets(targets, references, helper_image)
        if helper_image
        else {name: name for name in references}
    )
    bake["group"] = {"default": {"targets": list(selected.values())}}
    return bake, references


def build(descriptor: dict, profile: dict, *, no_cache: bool = False) -> dict:
    os.environ.setdefault("BUILDX_NO_DEFAULT_ATTESTATIONS", "1")
    os.environ.update(descriptor.get("environment", {}))
    command = compose_command(descriptor)
    config = json.loads(execute([*command, "config", "--format", "json"], capture=True))
    bake = json.loads(execute([*command, "build", "--print"], capture=True))
    bake, targets = build_plan(
        descriptor,
        config,
        bake,
        profile["registry"],
        profile["prefix"],
        no_cache,
        profile.get("registry_insecure", False),
        PYTHON_HELPER_IMAGE,
    )
    root = Path(descriptor["directory"] + ".build")
    root.mkdir(mode=0o700, exist_ok=True)
    plan_path = root / "bake.json"
    plan_path.write_text(json.dumps(bake))
    metadata_path = root / "metadata.json"
    execute(
        [
            "docker",
            "buildx",
            "bake",
            "--builder",
            profile["builder"],
            "--file",
            str(plan_path),
            "--metadata-file",
            str(metadata_path),
            "--progress",
            "plain",
            "default",
        ]
    )
    metadata = json.loads(metadata_path.read_text())
    references = {}
    for name, tagged in targets.items():
        exported = next(
            key
            for key, value in bake["target"].items()
            if value.get("tags") == [tagged]
        )
        digest = metadata[exported]["containerimage.digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("BuildKit did not return an immutable image digest")
        references[name] = tagged.rsplit(":", 1)[0] + "@" + digest
    (root / "artifacts.json").write_text(json.dumps(references, indent=2))
    return references


def prepare(descriptor: dict, profile: dict) -> dict:
    root = Path(descriptor["directory"] + ".build")
    references = json.loads((root / "artifacts.json").read_text())
    token = Path(profile["token_file"]).read_text().strip()
    opener = build_opener(ProxyHandler({}))
    prepared = []
    for reference in dict.fromkeys(references.values()):
        request = Request(
            profile["provider"] + "/prepare",
            data=json.dumps({"reference": reference}).encode(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        with opener.open(request, timeout=180) as response:
            prepared.append(json.loads(response.read(16 * 1024)))
    execute(
        [
            "/opt/akernel-image-bridge",
            "--mode",
            "register",
            "--plan",
            profile["plan"],
            "--containerd",
            "/run/akernel-image-bridge/containerd.sock",
        ]
    )
    # Generated override; never mutate the supplied Compose or Dockerfiles.
    lines = ["services:"]
    for service, reference in references.items():
        lines.extend(
            [
                "  " + json.dumps(service) + ":",
                "    build: !reset null",
                "    image: " + json.dumps(reference),
                "    pull_policy: never",
            ]
        )
    overlay = str(root / "runtime-images.yaml")
    Path(overlay).write_text("\n".join(lines) + "\n")
    attached = dict(descriptor)
    attached["files"] = [*descriptor["files"], overlay]
    attached["managed_artifacts"] = references
    (root / "attached.json").write_text(json.dumps(attached, indent=2))
    return {"artifacts": references, "prepared": prepared, "descriptor": attached}


def prepare_image(profile: dict, reference: str) -> dict:
    token = Path(profile["token_file"]).read_text().strip()
    request = Request(
        profile["provider"] + "/prepare",
        data=json.dumps({"reference": reference}).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
        return json.loads(response.read(1024 * 1024))


def start_builder(profile: dict) -> None:
    execute(
        [
            "docker",
            "buildx",
            "create",
            "--name",
            profile["builder"],
            "--driver",
            "remote",
            profile["endpoint"],
            "--use",
        ]
    )


def image_build(context: str, profile: dict, *, no_cache: bool = False) -> dict:
    os.environ.setdefault("BUILDX_NO_DEFAULT_ATTESTATIONS", "1")
    start_builder(profile)
    target = profile["registry"] + "/" + profile["prefix"] + "/single:built"
    metadata = context + ".metadata.json"
    plan, _ = build_plan(
        {},
        {"services": {"single": {"build": {}}}},
        {"target": {"single": {"context": context}}},
        profile["registry"],
        profile["prefix"],
        no_cache,
        profile.get("registry_insecure", False),
        PYTHON_HELPER_IMAGE,
    )
    plan_path = context + ".bake.json"
    Path(plan_path).write_text(json.dumps(plan))
    execute(
        [
            "docker",
            "buildx",
            "bake",
            "--builder",
            profile["builder"],
            "--file",
            plan_path,
            "--progress",
            "plain",
            "--metadata-file",
            metadata,
            "default",
        ]
    )
    values = json.loads(Path(metadata).read_text())
    exported = plan["group"]["default"]["targets"][0]
    digest = values[exported]["containerimage.digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("BuildKit did not return an immutable artifact")
    return prepare_image(profile, target.rsplit(":", 1)[0] + "@" + digest)


def expose_service(container: str, port: int) -> dict:
    import sys

    sys.path.insert(0, "/usr/lib/akernel")
    from docker_network_guest import expose

    data = json.loads(execute(["docker", "inspect", container], capture=True))[0]
    seen = set()
    for _ in range(8):
        mode = data["HostConfig"]["NetworkMode"]
        if not mode.startswith("container:"):
            break
        owner = mode.removeprefix("container:")
        if not owner or owner in seen:
            raise ValueError("Invalid network namespace ownership")
        seen.add(owner)
        data = json.loads(execute(["docker", "inspect", owner], capture=True))[0]
    else:
        raise ValueError("Network namespace ownership exceeds limit")
    if data["HostConfig"]["NetworkMode"] == "host":
        return {"url": f"http://127.0.0.1:{port}"}
    connected = []
    for name, network in (data["NetworkSettings"].get("Networks") or {}).items():
        if not network.get("IPAddress"):
            continue
        network = dict(network)
        if not network.get("Gateway"):
            inspected = json.loads(
                execute(["docker", "network", "inspect", name], capture=True)
            )[0]
            import ipaddress

            address = ipaddress.ip_address(network["IPAddress"])
            gateways = [
                v.get("Gateway")
                for v in inspected.get("IPAM", {}).get("Config", [])
                if v.get("Subnet")
                and address in ipaddress.ip_network(v["Subnet"], strict=False)
            ]
            network["Gateway"] = next((v for v in gateways if v), None)
        if network.get("Gateway"):
            connected.append(network)
    if not connected:
        raise ValueError("Managed service requires an IPv4 bridge")
    import uuid

    value = connected[0]
    # The runtime helper owns the relay process; return its opaque endpoint.
    return expose(
        Path("/opt/akernel-docker-runtime/tunnels") / uuid.uuid4().hex,
        {
            "address": value["Gateway"],
            "allowed": [value["IPAddress"]],
            "target_port": port,
        },
    )


def start_runtime(profile: dict) -> None:
    import sys

    sys.path.insert(0, "/usr/lib/akernel")
    from docker_network_guest import configure
    from image_bridge_guest import start_image_bridge

    if Path("/var/run/docker.sock").exists():
        raise RuntimeError("Runtime startup is fresh-only")
    runtime = Path("/opt/akernel-docker-runtime")
    runtime.mkdir(exist_ok=True)
    flags = start_image_bridge(
        runtime,
        Path(profile["plan"]),
        [],
        writable_tmpfs_mb=profile["writable_mb"],
        writable_backing="file",
    )
    configure()
    env = dict(os.environ)
    env["PATH"] = str(runtime / "network-bin") + ":" + env["PATH"]
    with Path("/var/log/akernel-dockerd.log").open("a") as logs:
        process = subprocess.Popen(
            [
                "dockerd",
                *flags,
                "--iptables=false",
                "--ip6tables=false",
                "--userland-proxy=true",
            ],
            stdout=logs,
            stderr=logs,
            env=env,
            start_new_session=True,
        )
    import time

    for _ in range(40):
        if process.poll() is not None:
            raise RuntimeError("Docker exited; inspect /var/log/akernel-dockerd.log")
        if (
            subprocess.run(
                ["docker", "info"], capture_output=True, check=False
            ).returncode
            == 0
        ):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Docker readiness timed out")
    start_builder(profile)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=[
            "start",
            "build",
            "prepare",
            "describe",
            "image-build",
            "reconnect",
            "expose",
        ],
    )
    parser.add_argument("--profile", default="/run/akernel-build/profile.json")
    parser.add_argument("--descriptor")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--context")
    parser.add_argument("--result")
    parser.add_argument("--container")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    profile = json.loads(Path(args.profile).read_text())
    if args.operation == "start":
        start_runtime(profile)
    elif args.operation == "image-build":
        if not args.context or not args.result:
            parser.error("image-build requires context and result")
        Path(args.result).write_text(
            json.dumps(image_build(args.context, profile, no_cache=args.no_cache))
        )
    elif args.operation == "reconnect":
        import sys

        sys.path.insert(0, "/usr/lib/akernel")
        from docker_network_guest import configure

        configure(reconnect=True)
    elif args.operation == "expose":
        if not args.container or not args.port or not 1 <= args.port <= 65535:
            parser.error("expose requires container and valid port")
        result = expose_service(args.container, args.port)
        if result:
            print(json.dumps(result))
    else:
        descriptor = json.loads(Path(args.descriptor).read_text())
        if args.operation == "describe":
            result = describe(descriptor)
        elif args.operation == "build":
            result = build(descriptor, profile, no_cache=args.no_cache)
        else:
            result = prepare(descriptor, profile)
        print(json.dumps(result))
