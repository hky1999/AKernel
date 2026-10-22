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


class HostObserver:
    def __init__(self, config):
        required = {"identity", "parent_path", "parent_memory_max_bytes", "parent_cpu_max", "storage_pins", "disk_path", "output", "valid_for_millis", "sample_interval_millis", "policy"}
        if set(config) != required:
            raise ValueError("宿主观测配置字段不完整")
        self.config = config
        self.gate = PressureGate(config["policy"])
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
        self.last_sample_monotonic = started
        try:
            metrics = self.sample()
            finished = time.monotonic_ns() // 1000000
            if not 0 <= finished - started <= self.config["sample_interval_millis"]:
                raise RuntimeError("单次压力采集超过有界窗口")
            accepting, reason = self.gate.update(metrics, started)
            self.last_metrics = metrics
        except (OSError, ValueError, KeyError, RuntimeError):
            self.gate.fail()
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
