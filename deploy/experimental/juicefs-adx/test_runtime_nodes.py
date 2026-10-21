from pathlib import Path
import unittest

from lab import LabError
from runtime_nodes import (check_cgroup_mounts, check_view, entered_command,
                           require_controllers, scope, sysfs_commands)


ROOT = Path("/mnt/test/cn-a0-adx-juicefs-test-jpa2cd")


class RuntimeNodeTests(unittest.TestCase):
    def test_removes_inherited_sysfs_before_mounting_task_views(self):
        mounts = "10 1 0:1 / /sys rw - sysfs sysfs rw\n11 10 0:2 /.. /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n"
        commands = sysfs_commands(100, 101, mounts)
        self.assertEqual(commands[0], ["umount", "--recursive", "/sys"])
        self.assertEqual(commands[1][2], "sysfs")
        self.assertEqual(commands[2][2], "cgroup2")
        self.assertEqual(commands[2][-1], "/sys/fs/cgroup")
        self.assertFalse(any("--lazy" in command or "--force" in command for command in commands))

    def test_sysfs_replacement_rejects_parent_namespace(self):
        mounts = "10 1 0:1 / /sys rw - sysfs sysfs rw\n"
        for parent, current in ((100, 100), (0, 100), (100, 0)):
            with self.assertRaises(LabError):
                sysfs_commands(parent, current, mounts)

    def test_sysfs_replacement_rejects_propagating_or_missing_mounts(self):
        for mounts in ("10 1 0:1 / /sys rw shared:3 - sysfs sysfs rw\n",
                       "10 1 0:1 / /sys rw master:3 - sysfs sysfs rw\n",
                       "10 1 0:1 / / rw - ext4 disk rw\n"):
            with self.assertRaises(LabError):
                sysfs_commands(100, 101, mounts)

    def test_accepts_one_task_rooted_cgroup_mount(self):
        check_cgroup_mounts("10 1 0:1 / /sys rw - sysfs sysfs rw\n"
                            "11 10 0:2 / /sys/fs/cgroup rw - cgroup2 none rw\n")

    def test_rejects_native04_hidden_mount_in_either_order(self):
        old = "11 10 0:2 /.. /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n"
        new = "12 10 0:2 / /sys/fs/cgroup rw - cgroup2 none rw\n"
        for mounts in (old + new, new + old):
            with self.assertRaises(LabError):
                check_cgroup_mounts(mounts)

    def test_rejects_missing_foreign_or_legacy_cgroup_mount(self):
        for mounts in ("", "11 10 0:2 /.. /sys/fs/cgroup rw - cgroup2 none rw\n",
                       "11 10 0:2 / /other rw - cgroup2 none rw\n",
                       "11 10 0:2 / /sys/fs/cgroup rw - cgroup none rw\n"):
            with self.assertRaises(LabError):
                check_cgroup_mounts(mounts)

    def test_scope_is_unique_per_task_generation_and_node(self):
        a = scope(ROOT, "native03", "source")
        b = scope(ROOT, "native03", "target")
        c = scope(ROOT, "native04", "source")
        self.assertEqual(a.parent, Path("/sys/fs/cgroup"))
        self.assertEqual(len({a, b, c}), 3)
        self.assertIn(ROOT.name, a.name)

    def test_rejects_path_injection(self):
        for generation, node in [("../native03", "source"), ("native03", "../other")]:
            with self.assertRaises(LabError):
                scope(ROOT, generation, node)

    def test_requires_preexisting_root_delegation(self):
        self.assertEqual(require_controllers("cpu memory pids io", "cpu memory pids"),
                         ["cpu", "memory", "pids"])
        with self.assertRaises(LabError):
            require_controllers("cpu memory pids", "cpu pids")

    def test_exec_enters_all_three_namespaces_without_ip_netns_exec(self):
        args = entered_command(1234, Path("/task/runtime_nodes.py"), ROOT,
                               "native03", "source", ["/task/adxctl", "run"])
        for flag in ("--mount", "--net", "--cgroup", "_exec"):
            self.assertIn(flag, args)
        self.assertNotIn("ip", args)
        self.assertEqual(args[-2:], ["/task/adxctl", "run"])

    def test_rejects_unsafe_pid_and_empty_exec(self):
        for pid, command in [(1, ["true"]), (10, [])]:
            with self.assertRaises(LabError):
                entered_command(pid, Path("helper"), ROOT, "native03", "source", command)

    def test_preflight_rejects_host_root_sysfs_and_outside_membership(self):
        check_view(70, 70, "63677270\n", "0::/control\n")
        for actual, expected, magic, membership in [
            (1, 70, "63677270", "0::/control"),
            (70, 70, "62656572", "0::/control"),
            (70, 70, "63677270", "0::/../.."),
        ]:
            with self.assertRaises(LabError):
                check_view(actual, expected, magic, membership)
