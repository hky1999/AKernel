#!/usr/bin/env python3
"""Isolated JuiceFS/PostgreSQL/Ceph functional lab. Run as root on its test host."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

LABEL = "io.akernel.juicefs-a0.task"
CEPH_IMAGE = "quay.io/ceph/ceph:v20.2.4"
PG_IMAGE = "docker.io/library/postgres:17"


class LabError(RuntimeError):
    pass


def task_s3_environment(credentials: dict, inherited: dict | None = None) -> dict:
    access = credentials.get("access_key")
    secret = credentials.get("secret_key")
    if not isinstance(access, str) or not access or not isinstance(secret, str) or not secret:
        raise LabError("task S3 credential record is incomplete")
    environment = dict(os.environ if inherited is None else inherited)
    for name in (
        "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    ):
        environment.pop(name, None)
    environment.update(AWS_ACCESS_KEY_ID=access, AWS_SECRET_ACCESS_KEY=secret,
                       AWS_EC2_METADATA_DISABLED="true")
    return environment


def private_log(path: Path, value: str) -> None:
    with path.open("w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(value)


def checked_root(value: str) -> Path:
    root = Path(value)
    if not root.is_absolute() or root.resolve() != root:
        raise LabError("lab root must be an absolute canonical path without symlinks")
    if not re.fullmatch(r"cn-a0-adx-juicefs-[A-Za-z0-9-]+", root.name):
        raise LabError("lab root must have the cn-a0-adx-juicefs- prefix")
    if root.is_dir() and root.stat().st_mode & 0o022:
        raise LabError("lab root must not be group/world writable")
    return root


def owned_container(info: dict, task: str, expected_id: str) -> bool:
    return (bool(re.fullmatch(r"[a-f0-9]{64}", expected_id))
            and info.get("Id") == expected_id
            and info.get("Config", {}).get("Labels", {}).get(LABEL) == task)


def process_start(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def private_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Lab:
    def __init__(self, root: Path):
        self.root = root
        self.state_path = root / "lab.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.podman = ["podman", "--root", str(root / "containers"),
                       "--runroot", str(root / "runroot"), "--storage-driver", "vfs"]

    def save(self) -> None:
        private_json(self.state_path, self.state)

    def run(self, args: list[str], label: str, timeout: int = 180, env: dict | None = None) -> str:
        try:
            result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            def decoded(value: bytes | str | None) -> str:
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")
            log = self.root / "logs" / f"{label}.log"
            private_log(log, decoded(exc.stdout) + decoded(exc.stderr))
            # TimeoutExpired includes argv, which can carry a metadata URI.
            # Preserve output privately, but never chain the raw command into
            # the user-visible exception.
            raise LabError(f"{label} timed out; scene retained; private log: {log}") from None
        if result.returncode:
            log = self.root / "logs" / f"{label}.log"
            private_log(log, result.stdout + result.stderr)
            raise LabError(f"{label} failed (exit {result.returncode}); private log: {log}")
        return result.stdout

    def init(self) -> None:
        if self.state:
            raise LabError("lab already initialized")
        self.root.mkdir(mode=0o700, parents=False, exist_ok=True)
        for name in ("secrets", "logs", "bin", "postgres", "ceph", "mount"):
            (self.root / name).mkdir(mode=0o700, exist_ok=True)
        ports = {"postgres": 16432, "mon": 19689, "rgw": 19633}
        for port in [*ports.values(), *range(19800, 19841)]:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
        password = secrets.token_hex(24)
        (self.root / "secrets" / "postgres.env").write_text(
            f"POSTGRES_USER=juicefs\nPOSTGRES_DB=juicefs\nPOSTGRES_PASSWORD={password}\n")
        os.chmod(self.root / "secrets" / "postgres.env", 0o600)
        private_json(self.root / "secrets" / "postgres.json", {"password": password})
        (self.root / "ceph" / "fsid").write_text(str(uuid.uuid4()))
        self.state = {"task": self.root.name, "ports": ports, "containers": {}, "mounts": {},
                      "bucket": "a0-" + secrets.token_hex(8)}
        shutil.copyfile(Path(__file__).with_name("ceph-entrypoint.sh"),
                        self.root / "bin" / "ceph-entrypoint.sh")
        self.save()

    def inspect(self, cid: str) -> dict:
        value = self.run(self.podman + ["inspect", cid], "inspect")
        info = json.loads(value)[0]
        if not owned_container(info, self.state["task"], cid):
            raise LabError("container identity/ownership mismatch; refusing mutation")
        return info

    def start(self) -> None:
        if not self.state:
            raise LabError("initialize lab first")
        ports = self.state["ports"]
        for role, image in (("postgres", PG_IMAGE), ("ceph", CEPH_IMAGE)):
            old = self.state["containers"].get(role)
            if old:
                info = self.inspect(old)
                if not info.get("State", {}).get("Running"):
                    self.run(self.podman + ["start", old], "start-" + role)
                continue
            args = self.podman + ["run", "-d", "--name", self.state["task"] + "-" + role,
                                 "--label", LABEL + "=" + self.state["task"], "--network", "host",
                                 "--log-driver", "k8s-file"]
            if role == "postgres":
                args += ["--env-file", str(self.root / "secrets" / "postgres.env"),
                         "-v", str(self.root / "postgres") + ":/var/lib/postgresql/data",
                         image, "postgres", "-p", str(ports["postgres"]),
                         "-c", "listen_addresses=127.0.0.1", "-c", "fsync=on",
                         "-c", "synchronous_commit=on"]
            else:
                args += ["--user", "0", "-v", str(self.root / "ceph") + ":/a0/ceph",
                         "-v", str(self.root / "secrets") + ":/a0/secrets",
                         "-v", str(self.root / "bin") + ":/a0/bin:ro",
                         "-e", "A0_MON_PORT=" + str(ports["mon"]),
                         "-e", "A0_RGW_PORT=" + str(ports["rgw"]),
                         "--entrypoint", "/bin/bash", image, "/a0/bin/ceph-entrypoint.sh"]
            cid = self.run(args, "create-" + role, timeout=600).strip()
            if not re.fullmatch(r"[a-f0-9]{64}", cid):
                raise LabError("unexpected container ID")
            self.state["containers"][role] = cid
            self.save()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            for cid in self.state["containers"].values():
                if not self.inspect(cid).get("State", {}).get("Running"):
                    raise LabError("lab container exited; scene retained")
            try:
                with socket.create_connection(("127.0.0.1", ports["rgw"]), timeout=1):
                    pass
                self.run(self.podman + ["exec", self.state["containers"]["postgres"],
                                       "pg_isready", "-p", str(ports["postgres"]),
                                       "-U", "juicefs"], "pg-ready", timeout=5)
                return
            except (OSError, LabError):
                time.sleep(1)
        raise LabError("lab readiness timed out; scene retained")

    def format_fs(self, binary: str) -> None:
        if not self.state:
            raise LabError("initialize lab first")
        copied = self.root / "bin" / "juicefs"
        if not copied.exists():
            shutil.copyfile(binary, copied)
            copied.chmod(0o755)
        self.state["juicefs_binary"] = str(copied)
        pg = json.loads((self.root / "secrets" / "postgres.json").read_text())
        meta = f"postgres://juicefs:{pg['password']}@127.0.0.1:{self.state['ports']['postgres']}/juicefs?sslmode=disable"
        private_json(self.root / "secrets" / "metadata.json", {"url": meta})
        if not self.state.get("formatted"):
            keys = json.loads((self.root / "secrets" / "s3-user.json").read_text())["keys"][0]
            mc = shutil.which("mc")
            if not mc:
                raise LabError("mc is required to provision the isolated RGW bucket")
            mc_dir = self.root / "secrets" / "mc"
            mc_dir.mkdir(mode=0o700, exist_ok=True)
            private_json(mc_dir / "config.json", {"version": "10", "aliases": {
                "a0": {"url": f"http://127.0.0.1:{self.state['ports']['rgw']}",
                       "accessKey": keys["access_key"], "secretKey": keys["secret_key"],
                       "api": "S3v4", "path": "on"}}})
            self.run([mc, "--config-dir", str(mc_dir), "mb", "--ignore-existing",
                      "--region", "default", "a0/" + self.state["bucket"]], "bucket-create")
            env = dict(os.environ, AWS_ACCESS_KEY_ID=keys["access_key"],
                       AWS_SECRET_ACCESS_KEY=keys["secret_key"])
            self.run([str(copied), "format", "--storage", "s3", "--bucket",
                      f"http://127.0.0.1:{self.state['ports']['rgw']}/{self.state['bucket']}",
                      "--trash-days", "0", meta, "a0adx"], "juicefs-format", env=env)
            self.state["formatted"] = True
        status = json.loads(self.run([str(copied), "status", meta], "juicefs-status"))
        volume_uuid = status.get("Setting", {}).get("UUID")
        if not isinstance(volume_uuid, str) or str(uuid.UUID(volume_uuid)) != volume_uuid.lower():
            raise LabError("JuiceFS status did not provide a valid volume UUID")
        self.state["volume_uuid"] = volume_uuid
        self.save()

    def mount(self, node: str) -> None:
        if self.state.get("mounts", {}).get(node):
            raise LabError("node mount already recorded; inspect/unmount before replacing")
        meta = json.loads((self.root / "secrets" / "metadata.json").read_text())["url"]
        keys = json.loads((self.root / "secrets" / "s3-user.json").read_text())["keys"]
        if not keys:
            raise LabError("task has no S3 key for its JuiceFS mount")
        environment = task_s3_environment(keys[0])
        self.state["data_plane_ready"] = False
        self.state.pop("data_plane_check", None)
        self.save()
        args = ["unshare", "--mount", "--propagation", "private",
                self.state["juicefs_binary"], "mount", "-f", "--cache-dir", "memory",
                "--cache-size", "0", "--writeback=false",
                "--buffer-size", "300", meta, str(self.root / "mount")]
        log = (self.root / "logs" / ("juicefs-" + node + ".log")).open("a")
        p = subprocess.Popen(args, stdout=log, stderr=log, start_new_session=True,
                             env=environment)
        log.close()
        start = process_start(p.pid)
        self.state["mounts"][node] = {"pid": p.pid, "start": start, "ready": False}
        self.save()
        for _ in range(60):
            if process_start(p.pid) != start or p.poll() is not None:
                raise LabError("JuiceFS client exited; dependencies retained")
            mountinfo = Path(f"/proc/{p.pid}/mountinfo").read_text()
            if " - fuse.juicefs " in mountinfo and str(self.root / "mount") in mountinfo:
                self.state["mounts"][node]["ready"] = True
                self.save()
                return
            time.sleep(0.5)
        raise LabError("JuiceFS mount timed out; scene retained")

    def namespace_command(self, node: str, command: list[str]) -> list[str]:
        record = self.state.get("mounts", {}).get(node)
        if not record or not record.get("ready") or process_start(record["pid"]) != record["start"]:
            raise LabError("ready task mount process identity required")
        return ["nsenter", "-t", str(record["pid"]), "-m", "--", *command]

    def check_data_plane(self) -> dict:
        # Mount presence only proves the metadata/FUSE path. Establish that
        # both clients can actually persist and fetch object-backed bytes.
        self.state["data_plane_ready"] = False
        self.state.pop("data_plane_check", None)
        self.save()
        for node in ("source", "target"):
            self.namespace_command(node, ["true"])
        probe_id = str(uuid.uuid4())
        host_directory = self.root / "data-probes" / probe_id
        host_directory.mkdir(mode=0o700, parents=True)
        shared_directory = self.root / "mount" / "a0-data-probes" / probe_id
        checks = []
        started = time.monotonic()
        for writer, reader in (("source", "target"), ("target", "source")):
            payload = host_directory / (writer + ".bin")
            with payload.open("wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(secrets.token_bytes(65536))
            target = shared_directory / (writer + ".bin")
            write_code = (
                "import hashlib,json,os,pathlib; "
                "source=pathlib.Path(" + repr(str(payload)) + "); "
                "dest=pathlib.Path(" + repr(str(target)) + "); "
                "dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700); "
                "temporary=dest.with_suffix('.tmp'); data=source.read_bytes(); "
                "fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
                "stream=os.fdopen(fd,'wb'); stream.write(data); stream.flush(); "
                "os.fsync(stream.fileno()); stream.close(); os.replace(temporary,dest); "
                "fd=os.open(dest.parent,os.O_RDONLY|os.O_DIRECTORY); os.fsync(fd); os.close(fd); "
                "print(json.dumps({'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}))"
            )
            written = json.loads(self.run(self.namespace_command(writer, [sys.executable, "-c", write_code]),
                                          "data-probe-write-" + writer, timeout=45))
            read_code = (
                "import hashlib,json,pathlib; data=pathlib.Path(" + repr(str(target)) + ").read_bytes(); "
                "print(json.dumps({'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}))"
            )
            observed = json.loads(self.run(self.namespace_command(reader, [sys.executable, "-c", read_code]),
                                           "data-probe-read-" + reader, timeout=45))
            if written != observed or written.get("bytes") != 65536:
                raise LabError("cross-client durable data probe did not match; scene retained")
            checks.append({"writer": writer, "reader": reader, "path": str(target), **observed})
        result = {"probe_id": probe_id, "volume_uuid": self.state.get("volume_uuid"),
                  "mounts": {node: dict(self.state["mounts"][node]) for node in ("source", "target")},
                  "checks": checks, "elapsed_ms": (time.monotonic() - started) * 1000}
        evidence = self.root / "evidence"
        evidence.mkdir(mode=0o700, exist_ok=True)
        private_json(evidence / ("data-plane-" + probe_id + ".json"), result)
        self.state["data_plane_check"] = result
        self.state["data_plane_ready"] = True
        self.save()
        return result

    def unmount(self, node: str) -> None:
        rec = self.state["mounts"].get(node)
        if not rec:
            return
        pid = rec["pid"]
        if process_start(pid) is None and not rec.get("ready", False):
            del self.state["mounts"][node]
            self.save()
            return
        if process_start(pid) != rec["start"]:
            raise LabError("mount process identity no longer matches; inspect dependents before stopping storage")
        self.state["data_plane_ready"] = False
        self.state.pop("data_plane_check", None)
        self.save()
        self.run(["nsenter", "-t", str(pid), "-m", "--", "fusermount3", "-u",
                  str(self.root / "mount")], "unmount-" + node, timeout=20)
        for _ in range(40):
            if process_start(pid) != rec["start"]:
                del self.state["mounts"][node]
                self.save()
                return
            time.sleep(0.25)
        raise LabError("client exit not confirmed; storage retained")

    def stop(self) -> None:
        for node in list(self.state.get("mounts", {})):
            self.unmount(node)
        for role in ("ceph", "postgres"):
            cid = self.state.get("containers", {}).get(role)
            if cid:
                info = self.inspect(cid)
                if info.get("State", {}).get("Running"):
                    self.run(self.podman + ["stop", "--time", "45", cid], "stop-" + role, timeout=60)
                if self.inspect(cid).get("State", {}).get("Running"):
                    raise LabError("container stop not confirmed")

    def status(self) -> dict:
        result = {"root": str(self.root), "task": self.state.get("task"),
                  "ports": self.state.get("ports"), "mounts": self.state.get("mounts", {}),
                  "data_plane_ready": bool(self.state.get("data_plane_ready", False)),
                  "containers": {}}
        for role, cid in self.state.get("containers", {}).items():
            result["containers"][role] = {
                "id": cid, "running": self.inspect(cid).get("State", {}).get("Running", False)}
        return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True)
    p.add_argument("action", choices=("init", "start", "format", "mount", "unmount", "stop", "status", "check-data", "exec"))
    p.add_argument("--juicefs-binary")
    p.add_argument("--node", choices=("source", "target"), default="source")
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args()
    if os.geteuid() != 0:
        raise LabError("run on the test host as root")
    lab = Lab(checked_root(args.root))
    if args.action == "init":
        lab.init()
    elif args.action == "start":
        lab.start()
    elif args.action == "format":
        if not args.juicefs_binary:
            raise LabError("--juicefs-binary is required")
        lab.format_fs(args.juicefs_binary)
    elif args.action == "mount":
        lab.mount(args.node)
    elif args.action == "unmount":
        lab.unmount(args.node)
    elif args.action == "stop":
        lab.stop()
    elif args.action == "status":
        print(json.dumps(lab.status(), indent=2))
    elif args.action == "check-data":
        print(json.dumps(lab.check_data_plane(), indent=2))
    else:
        rec = lab.state["mounts"].get(args.node)
        if not rec or not rec.get("ready", False) or process_start(rec["pid"]) != rec["start"]:
            raise LabError("node mount process identity mismatch")
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            raise LabError("exec requires a command after --")
        os.execvp("nsenter", ["nsenter", "-t", str(rec["pid"]), "-m", "--", *command])


if __name__ == "__main__":
    try:
        main()
    except (LabError, OSError, subprocess.TimeoutExpired, ValueError, KeyError) as e:
        print(f"lab error: {e}", file=sys.stderr)
        sys.exit(1)

