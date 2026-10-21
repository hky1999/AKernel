#!/usr/bin/env python3
"""Generate private ADX/sandboxd configuration for the task's two real nodes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import uuid

from lab import Lab, LabError, checked_root, private_json
from node_network import namespace_matches
from private_pki import ensure_pki


def deployment(root: Path, generation: str, node: str, volume: str,
               password: str, runtime: str) -> dict:
    directory = root / generation / node
    address = "10.243.251.2" if node == "source" else "10.243.251.3"
    guest_range = "10.244.208.0/20" if node == "source" else "10.244.224.0/20"
    key = root / "secrets" / "native-api-key"
    node_config = {
        "node_id": "a0-" + node,
        "listen": "0.0.0.0:27401",
        "advertised_address": address + ":27401",
        "proxy_address": address + ":27402",
        "proxy_socket": str(directory / "route.sock"),
        "sandboxd_socket": str(directory / "sandboxd.sock"),
        "runtime_logs": {"directory": str(directory / "runtime-logs")},
        "checkpoint_dir": None,
        "checkpoint_storage": {
            "kind": "shared_fs", "root": str(root / "mount" / "checkpoints"),
            "local_root": str(directory / "local-checkpoints"),
            "volume_uuid": volume, "mount_root": str(root / "mount"),
        },
        "checkpoint_gc": {"enabled": False},
        "resource_source": {"kind": "auto", "disk_path": str(root), "valid_for_seconds": 30},
        "degradation_journal": str(directory / "degraded.sqlite"),
        "metrics_listen": "127.0.0.1:27491",
        "report_interval_seconds": 2,
        "rpc_timeout_seconds": 120,
        "execd_port": 50090,
        "execd_command": ["/usr/local/bin/adx-execd"],
        "execd_env": {},
    }
    overrides = {
        "adxlet": {"config": node_config, "env": {
            "ADX_DATA_PLANE_RELAY_BIND": "0.0.0.0:27402",
            "ADX_DATA_PLANE_RELAY_ADVERTISE_ADDRESS": address + ":27402",
            "ADX_DATA_PLANE_RELAY_ACTIVITY_UDS_DIR": str(directory),
            "ADX_DATA_PLANE_ALLOWED_TARGET_CIDRS": guest_range,
            "ADX_DATA_PLANE_ALLOWED_INGRESS_CIDRS": "10.243.251.0/24,127.0.0.1/32",
            "RUST_LOG": "info",
        }},
    }
    if node == "source":
        overrides.update({
            "redis": {"config": {"bind": "0.0.0.0", "port": 27479,
                "data_dir": str(root / generation / "redis"), "appendfsync": "always",
                "password_file": str(root / "secrets" / "native-redis-key")}},
            "coordinator": {"config": {
                "listen": "0.0.0.0:27400", "advertised_address": "http://10.243.251.2:27400",
                "placement": "spread", "rpc_timeout_seconds": 120,
                "metrics_listen": "127.0.0.1:27490", "autoscaling_listen": "127.0.0.1:27492",
                "bootstrap_credentials": [{"key_file": str(key), "tenant_id": "a0",
                    "administrator": True, "expires_at_unix_seconds": 0}],
            }},
            "apiserver": {"config": {"listen": "127.0.0.1:27488", "loopback_http": True,
                "rpc_timeout_seconds": 120}},
            "ingress": {"env": {
                "ADX_DATA_PLANE_INGRESS_TLS_BIND": "0.0.0.0:27443",
                "ADX_DATA_PLANE_INGRESS_TLS_CERT": str(root / "secrets" / "native-pki" / "ingress.pem"),
                "ADX_DATA_PLANE_INGRESS_TLS_KEY": str(root / "secrets" / "native-pki" / "ingress.key"),
                "ADX_DATA_PLANE_INGRESS_PLAIN_BIND": "0.0.0.0:27480",
                "ADX_DATA_PLANE_INGRESS_HEALTH_BIND": "127.0.0.1:27489",
                "ADX_DATA_PLANE_INGRESS_ALLOWED_CLIENT_CIDRS": "10.243.251.0/24,127.0.0.1/32",
                "ADX_DATA_PLANE_INGRESS_CONTROL_PLANE_ADDRESS": "127.0.0.1:27488",
            }},
        })
    image = str(root / "runtime" / "rootfs.erofs")
    return {
        "schema_version": 1, "profile": "standalone" if node == "source" else "node",
        "internal_security": "network", "package_dir": str(root / "package"),
        "state_dir": str(directory / "state"),
        "redis_url": "redis://:" + password + "@10.243.251.2:27479/",
        "namespace": "juicefs-a0-" + generation,
        "restart_delay_ms": 1000, "stop_timeout_seconds": 60,
        "service_overrides": overrides,
        "runtime_profile": {
            "rootfs": {"runtime_class": runtime, "type": "local", "path": image, "readonly": False},
            "bootstrap": {"type": "erofs", "root": image, "target": "/__adx",
                "entrypoint": ["/__adx/usr/local/bin/adx-execd"],
                "image_process_config": "/etc/adx-image-process.json"},
            "env": {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        },
    }


def sandboxd_config(root: Path, generation: str, node: str, runtime: str) -> str:
    directory = root / generation / node
    subnet = "10.244.208.1/20" if node == "source" else "10.244.224.1/20"
    binary = (root / "runtime" / "gvisor" / "runsc" if runtime == "runsc" else
              root / "runtime" / "fc" / "release-v1.16.1-akernel.3-x86_64" / "firecracker")
    text = f'''rootDir = "{directory}/sandboxd-root"
storeDir = "{directory}/sandboxd-store"
[plugin.network]
ip_range = "{subnet}"
nat_backend = "iptables"
enable_local_dnat = true
enable_network_acl = false
[plugin.resource]
disable_cgroup = false
cpu_limit_mode = "quota"
cgroup_root_name = "/{root.name}-{generation}-{node}"
cgroup_cache_size = 1
interface_cache_size = 2
max_instance_num = 6
pids_max = 1024
[plugin.runtime]
filestore_dir = "{root}/mount/runtime/{generation}/{node}/filestore"
filestore_dir_size = ""
loop_device_dir = "/dev"
overlay_tmpfs_size = "256M"
[plugin.runtime.checkpoint_sync]
sync_output = true
[plugin.runtime.runsc]
platform = "systrap"
background_restore = true
[plugin.runtime.basic_spec]
{runtime} = ""
[plugin.runtime.runtime_binary]
{runtime} = "{binary}"
[plugin.image]
root = "{directory}/images"
distill_fs_bin = "{root}/bin/distill_fs"
oss_template = "{directory}/oss.json"
nydus_template = "{directory}/registry.json"
oss_auths_path = "{directory}/oss_auths.json"
registry_auths_path = "{directory}/registry_auths.json"
cgroup_memory_limit = "0"
'''
    if runtime == "firecracker":
        text += f'''[plugin.runtime.firecracker]
kernel_image_path = "{root}/runtime/fc/release-v1.16.1-akernel.3-x86_64/vmlinux"
initrd_path = "{root}/bin/initrd-a0.img"
kernel_args = "console=ttyS0 reboot=k panic=1 pci=off init=/init random.trust_cpu=on"
kvm_device = "/dev/kvm"
default_vcpu_count = 1
default_memory_mib = 512
default_overlay_size_bytes = 536870912
checkpoint_mode = "full"
writable_io_engine = "Async"
virtiofs_enabled = false
'''
    return text


def prepare(root: Path, generation: str, volume: str, runtime: str) -> None:
    lab = Lab(root)
    network = json.loads((root / "node-network.json").read_text())
    if network.get("task") != root.name or not network.get("ready"):
        raise LabError("ready task-owned node network required")
    for node, record in network["nodes"].items():
        if not namespace_matches(Path("/run/netns", record["name"]), record):
            raise LabError("node network namespace identity mismatch")
        code = ("import json; from pathlib import Path; "
                "print(json.loads(Path(" + repr(str(root / "mount" / ".config")) +
                ").read_text())['Format']['UUID'])")
        actual = lab.run([sys.executable, str(root / "profile" / "lab.py"), "--root", str(root),
            "--node", node, "exec", "--", sys.executable, "-c", code], "native-volume-" + node, timeout=10).strip()
        if actual != volume:
            raise LabError("mounted volume does not match the pinned UUID")
    destination = root / generation
    if destination.exists():
        raise LabError("native generation already exists; preserve the previous configuration")
    ensure_pki(root / "secrets" / "native-pki", root.name)
    destination.mkdir(mode=0o700)
    for name in ("native-api-key", "native-redis-key"):
        key = root / "secrets" / name
        if not key.exists():
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_hex(32))
        if key.is_symlink() or key.stat().st_mode & 0o077:
            raise LabError("private native key identity or permissions invalid")
    password = (root / "secrets" / "native-redis-key").read_text().strip()
    package = root / "package"
    if not package.exists():
        package.mkdir(mode=0o700)
        baseline = root / "runtime" / "adx"
        for entry in baseline.iterdir():
            if entry.name != "bin":
                (package / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
        (package / "bin").mkdir()
        for entry in (baseline / "bin").iterdir():
            target = root / "bin" / "adxlet" if entry.name == "adxlet" else entry
            (package / "bin" / entry.name).symlink_to(target)
    for node in ("source", "target"):
        directory = destination / node
        directory.mkdir(mode=0o700)
        for name, value in {
            "oss.json": {"type": "oss", "oss": {}},
            "registry.json": {"type": "registry", "registry": {"scheme": "https", "skip_verify": False}},
            "oss_auths.json": {}, "registry_auths.json": {"auths": {}},
        }.items():
            private_json(directory / name, value)
        private_json(directory / "deployment.yaml", deployment(root, generation, node, volume, password, runtime))
        config = directory / "sandboxd.toml"
        config.write_text(sandboxd_config(root, generation, node, runtime))
        config.chmod(0o600)
        lab.run([sys.executable, str(root / "profile" / "lab.py"), "--root", str(root),
            "--node", node, "exec", "--", "mkdir", "-p", str(root / "mount" / "checkpoints"),
            str(root / "mount" / "runtime" / generation / node / "filestore"),
            str(root / "mount" / "shares")], "native-layout-" + node, timeout=10)
    print("Created private " + runtime + " configuration: " + str(destination))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--volume-uuid", required=True)
    parser.add_argument("--runtime", choices=("runsc", "firecracker"), required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"native[0-9]{2}", args.generation):
        raise LabError("generation must be native followed by exactly two digits")
    prepare(checked_root(args.root), args.generation, str(uuid.UUID(args.volume_uuid)), args.runtime)


if __name__ == "__main__":
    try:
        main()
    except (LabError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("native profile error: " + str(error), file=sys.stderr)
        sys.exit(1)
