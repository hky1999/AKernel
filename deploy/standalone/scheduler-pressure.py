#!/usr/bin/env python3
"""为一个物理宿主发布有身份、有效期和滞回的只读压力准入观测。"""
import argparse
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
import time


def identity_digest(pins):
    return hashlib.sha256(json.dumps(pins, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def percentage(used, total):
    if not isinstance(used, int) or not isinstance(total, int) or used < 0 or total <= 0 or used > total:
        raise ValueError("压力计数必须处于有限总量内")
    return (used * 1000 + total - 1) // total


def psi_per_mille(text):
    lines = [line for line in text.splitlines() if line.startswith("some ")]
    if len(lines) != 1:
        raise ValueError("缺少唯一的PSI some观测")
    values = [field.removeprefix("avg10=") for field in lines[0].split() if field.startswith("avg10=")]
    if len(values) != 1:
        raise ValueError("缺少唯一的PSI avg10观测")
    try:
        value = Decimal(values[0])
        if not value.is_finite() or not 0 <= value <= 100:
            raise ValueError("PSI观测超出范围")
        return int((value * 10).to_integral_value(rounding="ROUND_CEILING"))
    except InvalidOperation as error:
        raise ValueError("PSI观测不是数值") from error


class PressureGate:
    def __init__(self, policy):
        if set(policy) != {"memory_high_per_mille", "memory_low_per_mille", "psi_high_per_mille", "psi_low_per_mille", "disk_high_per_mille", "disk_low_per_mille", "recovery_millis", "minimum_disk_available_bytes"}:
            raise ValueError("压力策略字段不完整")
        if any(type(value) is not int for value in policy.values()):
            raise ValueError("压力策略要求整数")
        for name in ("memory", "psi", "disk"):
            if not 0 <= policy[name + "_low_per_mille"] < policy[name + "_high_per_mille"] <= 1000:
                raise ValueError("压力水位要求0 <= low < high <= 1000")
        if not 1000 <= policy["recovery_millis"] <= 60000 or policy["minimum_disk_available_bytes"] <= 0:
            raise ValueError("恢复窗口和磁盘余量必须为正且有界")
        self.policy = dict(policy)
        self.accepting = False
        self.low_since = None

    def fail(self):
        self.accepting = False
        self.low_since = None

    def update(self, metrics, monotonic_millis):
        p = self.policy
        memory = max(metrics["host_memory_per_mille"], metrics["parent_memory_per_mille"])
        psi = max(metrics["host_memory_psi_per_mille"], metrics["parent_memory_psi_per_mille"])
        disk = metrics["disk_used_per_mille"]
        values = {"memory": memory, "psi": psi, "disk": disk}
        if metrics["disk_available_bytes"] < p["minimum_disk_available_bytes"] or any(
                value >= p[name + "_high_per_mille"] for name, value in values.items()):
            self.fail()
            return False, "high_watermark"
        low = all(value <= p[name + "_low_per_mille"] for name, value in values.items())
        if low:
            if self.low_since is None or monotonic_millis < self.low_since:
                self.low_since = monotonic_millis
            if monotonic_millis - self.low_since >= p["recovery_millis"]:
                self.accepting = True
        else:
            self.low_since = None
        return self.accepting, "low_watermark" if self.accepting else "warming"



class RuntimeAdmission:
    """Independent subpool hysteresis and an operator-owned, expiring veto."""
    def __init__(self, config, host_identity, recovery_millis):
        if not isinstance(config, dict) or set(config) != {"pool", "guard"}:
            raise ValueError("整链压力配置字段不完整")
        self.pool, self.guard = config["pool"], config["guard"]
        if set(self.pool) != {"path", "device", "inode", "memory_max_bytes", "high_bytes", "low_bytes", "psi_high_per_mille", "psi_low_per_mille"} or set(self.guard) != {"path", "identity", "max_age_millis"}:
            raise ValueError("整链子池或准入观察字段不完整")
        if any(type(self.pool[k]) is not int for k in self.pool if k != "path") or not 0 < self.pool["low_bytes"] < self.pool["high_bytes"] < self.pool["memory_max_bytes"] or not 0 <= self.pool["psi_low_per_mille"] < self.pool["psi_high_per_mille"] <= 1000 or self.pool["inode"] <= 0:
            raise ValueError("整链子池边界无效")
        identity = self.guard["identity"]
        if not isinstance(identity, dict) or set(identity) != {"host_id", "boot_id", "parent_device", "parent_inode", "profile_sha256", "pool_device", "pool_inode"}:
            raise ValueError("整链身份字段不完整")
        if any(identity[k] != host_identity[k] for k in ("host_id", "boot_id", "parent_device", "parent_inode")) or identity["pool_device"] != self.pool["device"] or identity["pool_inode"] != self.pool["inode"]:
            raise ValueError("整链身份不属于当前宿主与子池")
        profile = identity["profile_sha256"]
        if not isinstance(profile, str) or len(profile) != 64 or any(c not in "0123456789abcdef" for c in profile):
            raise ValueError("整链策略摘要无效")
        if type(self.guard["max_age_millis"]) is not int or not 100 <= self.guard["max_age_millis"] <= 10000:
            raise ValueError("整链观察有效期无效")
        for name in (self.pool["path"], self.guard["path"]):
            if not isinstance(name, str) or not Path(name).is_absolute() or Path(name).resolve() != Path(name):
                raise ValueError("整链路径必须是规范绝对路径")
        self.recovery_millis = recovery_millis
        self.fail()

    def fail(self):
        self.accepting, self.low_since = False, None

    def update(self, monotonic_millis):
        p = Path(self.pool["path"])
        st = p.lstat()
        if p.is_symlink() or not p.is_dir() or (st.st_dev, st.st_ino) != (self.pool["device"], self.pool["inode"]):
            raise RuntimeError("工作负载子池身份变化")
        if int((p/"memory.max").read_text()) != self.pool["memory_max_bytes"]:
            raise RuntimeError("工作负载子池上限变化")
        current = int((p/"memory.current").read_text())
        if not 0 <= current <= self.pool["memory_max_bytes"]:
            raise ValueError("工作负载子池计数无效")
        psi = psi_per_mille((p/"memory.pressure").read_text())
        path = Path(self.guard["path"])
        before = path.lstat()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or opened.st_mode & 0o022:
                raise ValueError("整链观察文件类型或权限无效")
            data = stream.read(8193)
        if len(data) > 8192:
            raise ValueError("整链观察过大")
        value = json.loads(data)
        fields = {"schema", "identity", "sequence", "control_session", "observed_unix_millis", "valid_until_unix_millis", "admission_open", "reason"}
        now = time.time_ns()//1000000
        if not isinstance(value, dict) or set(value) != fields or value["schema"] != 1 or value["identity"] != self.guard["identity"] or type(value["sequence"]) is not int or value["sequence"] <= 0 or any(type(value[k]) is not int for k in ("observed_unix_millis", "valid_until_unix_millis")) or not value["observed_unix_millis"] <= now < value["valid_until_unix_millis"] or not 0 < value["valid_until_unix_millis"]-value["observed_unix_millis"] <= self.guard["max_age_millis"] or type(value["admission_open"]) is not bool:
            raise ValueError("整链观察过期、缺失或身份不符")
        session = value["control_session"]
        if not isinstance(session, str) or len(session) != 32 or any(c not in "0123456789abcdef" for c in session) or not isinstance(value["reason"], str) or not 1 <= len(value["reason"]) <= 64 or (value["admission_open"] and value["reason"] != "ready"):
            raise ValueError("整链会话或准入原因无效")
        if current >= self.pool["high_bytes"] or psi >= self.pool["psi_high_per_mille"]:
            self.fail()
            return False, "high_watermark"
        if not value["admission_open"]:
            self.fail()
            return False, "runtime_guard_closed"
        if current <= self.pool["low_bytes"] and psi <= self.pool["psi_low_per_mille"]:
            if self.low_since is None or monotonic_millis < self.low_since:
                self.low_since = monotonic_millis
            if monotonic_millis-self.low_since >= self.recovery_millis:
                self.accepting = True
        else:
            self.low_since = None
        return self.accepting, "low_watermark" if self.accepting else "warming"


class HostObserver:
    def __init__(self, config):
        required = {"identity", "parent_path", "parent_memory_max_bytes", "parent_cpu_max", "storage_pins", "disk_path", "output", "valid_for_millis", "sample_interval_millis", "policy"}
        if set(config) not in (required, required | {"runtime"}):
            raise ValueError("宿主观测配置字段不完整")
        self.config = config
        self.gate = PressureGate(config["policy"])
        self.runtime_gate = RuntimeAdmission(config["runtime"], config["identity"], config["policy"]["recovery_millis"]) if "runtime" in config else None
        if (type(config["valid_for_millis"]) is not int or type(config["sample_interval_millis"]) is not int
                or not 100 <= config["sample_interval_millis"] <= 1000
                or not 2 * config["sample_interval_millis"] <= config["valid_for_millis"] <= 10000):
            raise ValueError("采样间隔和有效期不在允许范围内")
        for key in ("parent_path", "disk_path", "output"):
            if not Path(config[key]).is_absolute():
                raise ValueError("观测路径必须是绝对路径")
        if config["identity"]["storage_identity_sha256"] != identity_digest(config["storage_pins"]):
            raise ValueError("持久目录身份摘要不匹配")
        self.output = Path(config["output"])
        st = self.output.parent.lstat()
        if not self.output.parent.is_dir() or self.output.parent.is_symlink():
            raise ValueError("观测目录必须是真实目录")
        self.output_directory_identity = (st.st_dev, st.st_ino)
        self.lock = os.open(str(self.output) + ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.lock)
            raise
        self.sequence = 0
        self.last_sample_monotonic = None
        self.last_metrics = {"host_memory_per_mille": 1000, "parent_memory_per_mille": 1000,
                             "host_memory_psi_per_mille": 1000, "parent_memory_psi_per_mille": 1000,
                             "disk_used_per_mille": 1000, "parent_memory_current_bytes": 0,
                             "parent_memory_max_bytes": config["parent_memory_max_bytes"],
                             "disk_available_bytes": 0}

    def close(self):
        os.close(self.lock)

    def verify(self):
        c = self.config
        identity = c["identity"]
        if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != identity["boot_id"]:
            raise RuntimeError("宿主启动身份变化，需要隔离恢复")
        parent = Path(c["parent_path"])
        st = parent.lstat()
        if parent.is_symlink() or (st.st_dev, st.st_ino) != (identity["parent_device"], identity["parent_inode"]):
            raise RuntimeError("实验父cgroup身份变化")
        if int((parent / "memory.max").read_text().strip()) != c["parent_memory_max_bytes"]:
            raise RuntimeError("实验父cgroup内存上限变化")
        if (parent / "cpu.max").read_text().strip() != c["parent_cpu_max"]:
            raise RuntimeError("实验父cgroup CPU上限变化")
        for pin in c["storage_pins"]:
            p = Path(pin["path"]); st = p.lstat()
            if p.is_symlink() or (st.st_dev, st.st_ino) != (pin["device"], pin["inode"]):
                raise RuntimeError("持久目录或SQLite身份变化，需要隔离恢复")

    def sample(self):
        self.verify()
        c = self.config; parent = Path(c["parent_path"])
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            fields = line.split()
            if fields[0] in ("MemTotal:", "MemAvailable:"):
                if len(fields) != 3 or fields[2] != "kB" or fields[0] in info:
                    raise ValueError("宿主内存观测格式无效")
                info[fields[0]] = int(fields[1]) * 1024
        total, available = info["MemTotal:"], info["MemAvailable:"]
        current = int((parent / "memory.current").read_text().strip())
        disk = os.statvfs(c["disk_path"])
        disk_total = disk.f_blocks * disk.f_frsize
        disk_free = disk.f_bavail * disk.f_frsize
        return {"host_memory_per_mille": percentage(total - available, total),
                "parent_memory_per_mille": percentage(current, c["parent_memory_max_bytes"]),
                "host_memory_psi_per_mille": psi_per_mille(Path("/proc/pressure/memory").read_text()),
                "parent_memory_psi_per_mille": psi_per_mille((parent / "memory.pressure").read_text()),
                "disk_used_per_mille": percentage(disk_total - disk_free, disk_total),
                "parent_memory_current_bytes": current,
                "parent_memory_max_bytes": c["parent_memory_max_bytes"],
                "disk_available_bytes": disk_free}

    def publish(self, accepting, reason, metrics, observed_unix_millis=None):
        st = self.output.parent.lstat()
        if self.output.parent.is_symlink() or (st.st_dev, st.st_ino) != self.output_directory_identity or self.output.is_symlink():
            raise RuntimeError("观测输出目录或文件被替换")
        now = time.time_ns() // 1000000 if observed_unix_millis is None else observed_unix_millis
        self.sequence += 1
        value = {"schema": 1, "identity": self.config["identity"], "sequence": self.sequence,
                 "observed_unix_millis": now, "valid_until_unix_millis": now + self.config["valid_for_millis"],
                 "accepting": accepting, "reason": reason, "metrics": metrics}
        fd, temporary = tempfile.mkstemp(prefix=".pressure-", dir=self.output.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, separators=(",", ":")); stream.write("\n")
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.output)
            directory = os.open(self.output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return value

    def step(self):
        started = time.monotonic_ns() // 1000000
        observed = time.time_ns() // 1000000
        if self.last_sample_monotonic is not None and not 0 <= started - self.last_sample_monotonic < self.config["valid_for_millis"]:
            self.gate.fail()
            if self.runtime_gate is not None:
                self.runtime_gate.fail()
        self.last_sample_monotonic = started
        try:
            metrics = self.sample()
            accepting, reason = self.gate.update(metrics, started)
            if self.runtime_gate is not None:
                runtime_accepting, runtime_reason = self.runtime_gate.update(started)
                if not runtime_accepting:
                    accepting = False
                    if reason != "high_watermark":
                        reason = runtime_reason
            finished = time.monotonic_ns() // 1000000
            if not 0 <= finished - started <= self.config["sample_interval_millis"]:
                raise RuntimeError("单次压力采集超过有界窗口")
            self.last_metrics = metrics
        except (OSError, ValueError, KeyError, RuntimeError):
            self.gate.fail()
            if self.runtime_gate is not None:
                self.runtime_gate.fail()
            accepting, reason, metrics = False, "observation_failed", self.last_metrics
        return self.publish(accepting, reason, metrics, observed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    observer = HostObserver(json.loads(args.config.read_text()))
    stopped = False
    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try:
        observer.publish(False, "starting", observer.last_metrics)
        while not stopped:
            value = observer.step()
            if args.once:
                print(json.dumps(value)); return
            time.sleep(observer.config["sample_interval_millis"] / 1000)
    finally:
        try:
            observer.publish(False, "stopped", observer.last_metrics)
        finally:
            observer.close()


if __name__ == "__main__":
    main()
