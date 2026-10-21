#!/usr/bin/env python3
"""启动有独立持久目录和固定容量的单节点开发环境。"""

import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time

import yaml

STATE = Path("/home/akernel")
CGROUP = Path("/sys/fs/cgroup")
CAPACITY_VALID_SECONDS = 60
CAPACITY_REFRESH_SECONDS = 10


def elastic_memory_enabled():
    """只接受明确的布尔配置，避免拼写错误意外启用或关闭策略。"""
    value = os.environ.get("AKERNEL_SCHEDULER_ELASTIC_MEMORY", "false")
    if value not in ("true", "false"):
        raise RuntimeError("AKERNEL_SCHEDULER_ELASTIC_MEMORY 只能是 true 或 false")
    return value == "true"


def history_reclamation_enabled():
    value = os.environ.get("AKERNEL_SCHEDULER_HISTORY_RECLAMATION", "false")
    if value not in ("true", "false"):
        raise RuntimeError("AKERNEL_SCHEDULER_HISTORY_RECLAMATION 只能是 true 或 false")
    return value == "true"


def configure_history_reclamation(expanded, enabled):
    nodes = [service for service in expanded["services"] if service["role"] == "adxlet"]
    if len(nodes) != 1 or not isinstance(enabled, bool):
        raise RuntimeError("历史回收要求单个明确节点和布尔开关")
    nodes[0]["config"]["history_reclamation"] = enabled


def configure_memory_control(expanded, sandboxd_text, enabled):
    """为单节点实验同时配置中心策略和守护进程的软节流比例。"""
    coordinators = [service for service in expanded["services"]
                    if service["role"] == "coordinator"]
    if len(coordinators) != 1 or not isinstance(enabled, bool):
        raise RuntimeError("自动内存策略要求单个 Coordinator 和明确的布尔开关")
    config = coordinators[0]["config"]
    policy = {
        "grow_per_mille": 650, "shrink_per_mille": 300, "target_per_mille": 500,
        "cooldown_millis": 5000, "low_usage_millis": 30000, "low_usage_samples": 5,
        "max_sample_age_millis": 10000, "operation_timeout_millis": 10000,
    } if enabled else None
    if config.get("memory_control") not in (None, policy):
        raise RuntimeError("展开配置已有不同的自动内存策略")
    if not enabled:
        return sandboxd_text
    marker = "[plugin.resource]\n"
    if sandboxd_text.count(marker) != 1 or any(
            field in sandboxd_text for field in ("memory_high_ratio", "cgroup_io_accounting")):
        raise RuntimeError("守护进程模板的内存软节流配置与预期不一致")
    config["memory_control"] = policy
    return sandboxd_text.replace(
        marker, marker + "memory_high_ratio=0.75\ncgroup_io_accounting=true\n")


def fixed_capacity():
    """返回本实验明确预留的容量，不使用主机瞬时空闲量。"""
    return {"cpu_millis": 3000, "memory_bytes": 12 * 1024**3,
            "disk_bytes": 16 * 1024**3}


def configure_host_budget(expanded):
    """为展开后的单节点配置绑定独立持久宿主预算。"""
    coordinators = [service for service in expanded["services"]
                    if service["role"] == "coordinator"]
    nodes = [service["config"].get("node_id") for service in expanded["services"]
             if service["role"] == "adxlet"]
    if len(coordinators) != 1 or len(nodes) != 1:
        raise RuntimeError("本开发模板要求恰好一个 Coordinator 和一个节点")
    node = nodes[0]
    if not isinstance(node, str) or not node or node.strip() != node or "${" in node:
        raise RuntimeError("宿主预算需要展开后的明确节点身份")
    budget = {"BareMetal1": {"capacity": fixed_capacity(), "nodes": nodes}}
    config = coordinators[0]["config"]
    if "host_budgets" in config and config["host_budgets"] != budget:
        raise RuntimeError("展开配置已有不同的宿主预算")
    config["host_budgets"] = budget


def write_capacity(adx, owned_lock=None):
    """Publish the fixed experiment allocation without exposing a partial file."""
    if (adx / "capacity.json").is_symlink():
        raise RuntimeError("容量文件不能是软链接")
    capacity = {
        "capacity": fixed_capacity(),
        "devices": [],
        "valid_until_unix_seconds": int(time.time()) + CAPACITY_VALID_SECONDS,
    }
    lock = owned_lock
    if lock is None:
        lock = os.open(adx / "capacity.lock",
                       os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(lock)
            raise
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix=".capacity-", dir=adx)
        with os.fdopen(fd, "w") as stream:
            json.dump(capacity, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, adx / "capacity.json")
        directory = os.open(adx, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)
        if owned_lock is None:
            os.close(lock)


class FixedCapacityLease:
    """Renew only the original, bounded single-node development allocation."""

    def __init__(self):
        self.directories = (CGROUP, CGROUP / "services", STATE / "adx")
        self.identities = [(path.stat().st_dev, path.stat().st_ino)
                           for path in self.directories]
        self.previous = (STATE / "adx/capacity.json").read_bytes()
        self.lock = os.open(STATE / "adx/capacity.lock",
                            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.lock)
            raise

    def close(self):
        os.close(self.lock)

    def renew(self):
        for path, original in zip(self.directories, self.identities):
            current = path.lstat()
            if path.is_symlink() or (current.st_dev, current.st_ino) != original:
                raise RuntimeError("容量来源目录已被替换")
        if (CGROUP / "cpu.max").read_text().split() != ["400000", "100000"]:
            raise RuntimeError("开发环境的固定 CPU 上限发生变化")
        if (CGROUP / "memory.max").read_text().strip() != str(16 * 1024**3):
            raise RuntimeError("开发环境的固定内存上限发生变化")
        if (CGROUP / "services/memory.max").read_text().strip() != str(4 * 1024**3):
            raise RuntimeError("控制服务的内存预留发生变化")
        if shutil.disk_usage(STATE).free < 16 * 1024**3:
            raise RuntimeError("磁盘剩余空间不足以维持固定开发容量")
        path = STATE / "adx/capacity.json"
        if path.is_symlink() or path.read_bytes() != self.previous:
            raise RuntimeError("容量文件已被其他写入者修改")
        write_capacity(STATE / "adx", owned_lock=self.lock)
        self.previous = path.read_bytes()


def prepare_cgroup(*, io_accounting=False):
    if not isinstance(io_accounting, bool):
        raise RuntimeError("I/O统计开关必须是明确的布尔值")
    # 此文件系统必须来自启动器声明的私有 cgroup namespace。
    if Path("/proc/self/ns/cgroup").readlink() != Path("/proc/1/ns/cgroup").readlink():
        raise RuntimeError("开发服务必须与容器 PID 1 共用私有 cgroup namespace")
    available = set((CGROUP / "cgroup.controllers").read_text().split())
    required = {"cpu", "memory", "pids"}
    if io_accounting:
        required.add("io")
    if not required.issubset(available):
        missing = ", ".join(sorted(required - available))
        raise RuntimeError("开发容器缺少控制器：" + missing)
    services = CGROUP / "services"
    services.mkdir(exist_ok=True)
    for pid in (CGROUP / "cgroup.procs").read_text().split():
        (services / "cgroup.procs").write_text(pid)
    controls = "+cpu +memory +pids" + (" +io" if io_accounting else "")
    (CGROUP / "cgroup.subtree_control").write_text(controls)
    (services / "memory.max").write_text(str(4 * 1024**3))


def prepare_config(templates=Path("/etc/akernel/dev-templates"),
                   output=Path("/etc/akernel/adx-standalone.yaml"),
                   adxctl="/opt/adx/current/bin/adxctl", *, elastic_memory=False,
                   history_reclamation=False):
    config = STATE / "sandboxd/config"
    config.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "oss.json", "registry.json", "oss_auths.json"):
        target = config / name
        if not target.exists():
            shutil.copyfile(templates / name, target)
    auths = config / "registry_auths.json"
    if not auths.exists():
        auths.write_text('{"auths": {}}\n')
    auths.chmod(0o600)
    text = (templates / "sandboxd_config.toml").read_text()
    for old, new in (
        ('10.88.0.1/16', '10.93.0.1/16'),
        ('cgroup_cache_size=800', 'cgroup_cache_size=8'),
        ('netns_cache_size=1000', 'netns_cache_size=8'),
        ('interface_cache_size=1000', 'interface_cache_size=8'),
        ('max_instance_num=1000', 'max_instance_num=16'),
        ('cgroup_root_name="/akernel"', 'cgroup_root_name="/akernel-agent-dx"'),
        ('filestore_dir_size="50G"', 'filestore_dir_size="20G"'),
        ('overlay_tmpfs_size="10G"', 'overlay_tmpfs_size="1G"'),
        ('cgroup_memory_limit="64GiB"', 'cgroup_memory_limit="1GiB"'),
        ('# AKERNEL_CHUNK_DB_SIZE', 'chunk_db_size="4GiB"'),
        ('kata="/usr/local/bin/containerd-shim-kata-v2"', 'kata=""'),
        ('firecracker="/usr/local/bin/firecracker"', 'firecracker=""'),
    ):
        if old not in text:
            raise RuntimeError("开发模板与预期配置不一致")
        text = text.replace(old, new)
    images = STATE / "images"
    images.mkdir(exist_ok=True)
    shutil.copyfile(config / "config.json", images / "config.json")
    adx = STATE / "adx"
    (adx / "secrets").mkdir(parents=True, exist_ok=True, mode=0o700)
    key = adx / "secrets/admin-key"
    if not key.exists():
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(48) + "\n")
    key.chmod(0o600)
    # 预留 4 GiB 给控制服务、运行时及缓存；节点观测与持久宿主预算分开。
    write_capacity(adx)
    deployment = {
        "schema_version": 1, "profile": "standalone", "internal_security": "network",
        "namespace": "akernel-agent-dx-dev",
        "logging": {"max_file_bytes": 16777216, "max_files": 3,
                    "max_total_bytes": 268435456},
        "service_overrides": {
            "adxlet": {
                "config": {
                    "execd_env": {"ADX_EXECD_CONTROL_SOCKET_PATH": "/run/akernel"},
                },
                "env": {"ADX_DATA_PLANE_ALLOWED_TARGET_CIDRS": "10.93.0.0/16"},
            },
            "ingress": {"env": {
                "ADX_DATA_PLANE_INGRESS_PLAIN_BIND": "0.0.0.0:8080",
                "ADX_DATA_PLANE_INGRESS_ALLOWED_CLIENT_CIDRS": "0.0.0.0/0",
                "ADX_DATA_PLANE_INGRESS_PORT_HOST_DOMAIN": "localhost",
            }},
        },
    }
    output.write_text(json.dumps(deployment))
    # 配置覆盖采用深合并。先展开，再整体替换资源来源，避免把 auto 的
    # disk_path、valid_for_seconds 留进拒绝额外字段的 file 配置。
    expanded = yaml.safe_load(subprocess.check_output(
        [adxctl, "--config", str(output), "config", "dump"], text=True))
    configure_host_budget(expanded)
    configure_history_reclamation(expanded, history_reclamation)
    text = configure_memory_control(expanded, text, elastic_memory)
    (STATE / "sandboxd/config.toml").write_text(text)
    for service in expanded["services"]:
        if service["role"] == "adxlet":
            service["config"]["resource_source"] = {
                "kind": "file", "path": str(adx / "capacity.json"),
            }
            service["config"]["checkpoint_dir"] = str(
                STATE / "sandboxd/root/checkpoints/adx")
            service["config"]["sandboxd_operation_journal"] = str(
                adx / "operations/sandboxd.sqlite")
    output.write_text(json.dumps(expanded))
    subprocess.run([adxctl, "--config", str(output), "validate"], check=True)


class SandboxdSupervisor:
    """只在原有子进程已退出后重启 sandboxd，保持其他服务及持久目录。"""

    def __init__(self, argv, env):
        self.argv = list(argv)
        self.env = dict(env)
        self.process = None
        self.next_start = 0
        self.started_at = 0
        self.restart_delay = 1.0
        self.closed = False

    def tick(self):
        if self.closed:
            return
        now = time.monotonic()
        if self.process is not None:
            code = self.process.poll()
            if code is None:
                if now - self.started_at >= 60:
                    self.restart_delay = 1.0
                return
            print(f"sandboxd 已退出，退出码为 {code}，将按退避间隔重启。",
                  file=sys.stderr, flush=True)
            self.process = None
            self.next_start = now + self.restart_delay
            self.restart_delay = min(30.0, self.restart_delay * 2)
        if now < self.next_start:
            return
        try:
            self.process = subprocess.Popen(self.argv, env=self.env)
        except OSError:
            print("sandboxd 暂时无法启动，保留其他服务并等待下次重试。",
                  file=sys.stderr, flush=True)
            self.next_start = now + self.restart_delay
            self.restart_delay = min(30.0, self.restart_delay * 2)
        else:
            self.started_at = time.monotonic()

    def close(self):
        self.closed = True
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=100)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("sandboxd 未正常结束，保留环境用于检查") from error


def socket_available(path):
    """拒绝残留的 socket 文件，只接受当前可连接的 Unix 监听端点。"""
    try:
        if not stat.S_ISSOCK(path.lstat().st_mode):
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.1)
            probe.connect(str(path))
        return True
    except OSError:
        return False


def run_services(daemon_argv, daemon_env, capacity_lease, stop_requested, *,
                 service_argv=None, socket_path=None, startup_timeout=90,
                 capacity_refresh_seconds=CAPACITY_REFRESH_SECONDS,
                 poll_seconds=0.1):
    """让容量续期与守护进程恢复独立推进，不清除任何物理操作账本。"""
    if service_argv is None:
        service_argv = ["/usr/local/bin/adx-service", "run"]
    if socket_path is None:
        socket_path = Path("/run/sandboxd/sandboxd.sock")
    daemon = SandboxdSupervisor(daemon_argv, daemon_env)
    service = None
    deadline = time.monotonic() + startup_timeout
    next_capacity_refresh = 0
    try:
        while not stop_requested():
            if service is not None and service.poll() is not None:
                raise RuntimeError("调度服务意外退出，保留环境用于检查")
            daemon.tick()
            now = time.monotonic()
            if now >= next_capacity_refresh:
                try:
                    capacity_lease.renew()
                except (OSError, RuntimeError):
                    print("固定容量无法续期，等待原观测过期并关闭新任务准入。",
                          file=sys.stderr, flush=True)
                next_capacity_refresh = now + capacity_refresh_seconds
            if service is None:
                if (daemon.process is not None and daemon.process.poll() is None
                        and socket_available(socket_path)):
                    service = subprocess.Popen(service_argv)
                elif now >= deadline:
                    raise RuntimeError("sandboxd 未在期限内提供服务")
            time.sleep(poll_seconds)
    finally:
        # 每次只管理自己创建的当前子进程；先停止调度服务，再关闭守护进程。
        try:
            if service is not None and service.poll() is None:
                service.terminate()
                try:
                    service.wait(timeout=100)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError("调度服务未正常结束，保留环境用于检查") from error
        finally:
            try:
                daemon.close()
            finally:
                capacity_lease.close()


def main():
    elastic_memory = elastic_memory_enabled()
    history_reclamation = history_reclamation_enabled()
    prepare_cgroup(io_accounting=elastic_memory)
    prepare_config(elastic_memory=elastic_memory, history_reclamation=history_reclamation)
    capacity_lease = FixedCapacityLease()
    try:
        subprocess.run(["/usr/local/bin/sandboxd-network-prepare"], check=True)
        (STATE / "logs/sandboxd").mkdir(parents=True, exist_ok=True)
    except BaseException:
        capacity_lease.close()
        raise
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run_services([
        "/usr/local/bin/sandboxd", "-config", str(STATE / "sandboxd/config.toml"),
        "-socket", "/run/sandboxd/sandboxd.sock", "-log-level", "info",
        "-log-file", str(STATE / "logs/sandboxd/sandboxd.log"),
    ], dict(os.environ, GOMEMLIMIT="1GiB", GOGC="100"), capacity_lease,
        lambda: stopped, capacity_refresh_seconds=CAPACITY_REFRESH_SECONDS)


if __name__ == "__main__":
    main()
