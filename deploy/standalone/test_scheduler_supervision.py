"""以真实自有子进程验证开发入口的守护进程恢复边界。"""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ENTRY = Path(__file__).with_name("scheduler-dev-entrypoint.py")
spec = importlib.util.spec_from_file_location("supervision_entry", ENTRY)
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)

DAEMON = r"""
import json,os,signal,socket,sys,time
from pathlib import Path
root=Path(sys.argv[1]);mode=sys.argv[2];listener=None;stopped=False
if mode!='never-listen':
 p=root/'daemon.sock';p.unlink(missing_ok=True)
 listener=socket.socket(socket.AF_UNIX);listener.bind(str(p));listener.listen();listener.settimeout(.02)
with (root/'daemon.jsonl').open('a') as out:out.write(json.dumps({'pid':os.getpid(),'ppid':os.getppid(),'monotonic':time.monotonic()})+'\n')
def stop(*args):
 global stopped
 stopped=True
signal.signal(signal.SIGTERM,stop)
while not stopped:
 if (root/('exit-'+str(os.getpid()))).exists():os._exit(9)
 if listener:
  try:c,_=listener.accept();c.close()
  except TimeoutError:pass
 else:time.sleep(.02)
if listener:listener.close()
"""
CONTROL = r"""
import json,os,signal,sys,time
from pathlib import Path
root=Path(sys.argv[1]);mode=sys.argv[2];stopped=False
with (root/'control.jsonl').open('a') as out:out.write(json.dumps({'pid':os.getpid(),'ppid':os.getppid()})+'\n')
if mode=='exit':sys.exit(7)
def stop(*args):
 global stopped
 stopped=True
signal.signal(signal.SIGTERM,stop)
count=0
while not stopped:
 count+=1;(root/'heartbeat').write_text(str(count));time.sleep(.02)
"""
RUNNER = r"""
import importlib.util,json,signal,sys,time
from pathlib import Path
entry_path,root,mode,daemon,control=sys.argv[1:];root=Path(root)
spec=importlib.util.spec_from_file_location('entry',entry_path);entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
def event(value):
 with (root/'lease.jsonl').open('a') as out:out.write(json.dumps(value)+'\n')
class Lease:
 def renew(self):
  fail=(root/'capacity-fails').exists();event({'event':'renew','failed':fail,'monotonic':time.monotonic()})
  if fail:raise OSError('自有测试的容量来源暂不可读')
 def close(self):event({'event':'close'})
def stop(*args):(root/'stop').touch()
signal.signal(signal.SIGTERM,stop)
if mode=='main':
 import os,threading
 entry.STATE=root;entry.prepare_cgroup=lambda:None;entry.prepare_config=lambda **kw:None;entry.FixedCapacityLease=Lease;entry.CAPACITY_REFRESH_SECONDS=.15
 original_path=entry.Path
 entry.Path=lambda value:root/'daemon.sock' if str(value)=='/run/sandboxd/sandboxd.sock' else original_path(value)
 original_spawn=entry.subprocess.Popen
 def spawn(argv,**kw):
  if argv[0]=='/usr/local/bin/sandboxd':argv=[sys.executable,'-I','-B','-c',daemon,str(root),mode]
  elif argv==['/usr/local/bin/adx-service','run']:argv=[sys.executable,'-I','-B','-c',control,str(root),mode]
  else:raise AssertionError('测试入口出现未绑定的子进程')
  return original_spawn(argv,**kw)
 entry.subprocess.Popen=spawn;entry.subprocess.run=lambda *a,**kw:None
 def request_stop():
  while not (root/'stop').exists():time.sleep(.02)
  os.kill(os.getpid(),signal.SIGTERM)
 threading.Thread(target=request_stop,daemon=True).start()
 entry.main()
else:
 entry.run_services([sys.executable,'-I','-B','-c',daemon,str(root),mode],dict(),Lease(),lambda:(root/'stop').exists(),service_argv=[sys.executable,'-I','-B','-c',control,str(root),mode],socket_path=root/'daemon.sock',startup_timeout=.4 if mode=='never-listen' else 5,capacity_refresh_seconds=.15,poll_seconds=.02)
"""


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def identity(pid):
    fields = Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
    return int(fields[1]), int(fields[19])


class SupervisionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runner = None
        self.log = (self.root / 'runner.log').open('w')

    def tearDown(self):
        try:
            if self.runner and self.runner.poll() is None:
                (self.root / 'stop').touch()
                self.runner.wait(timeout=8)
        finally:
            self.log.close()
            evidence = os.environ.get('AKERNEL_SUPERVISION_EVIDENCE')
            if evidence:
                target = Path(evidence) / self._testMethodName
                target.mkdir(parents=True, exist_ok=False)
                for path in self.root.iterdir():
                    if path.is_file() and not path.is_symlink():
                        shutil.copyfile(path, target / path.name)
            self.temporary.cleanup()

    def launch(self, mode='serve'):
        self.runner = subprocess.Popen([sys.executable, '-I', '-B', '-c', RUNNER,
                                       str(ENTRY), str(self.root), mode, DAEMON, CONTROL],
                                      stdout=self.log, stderr=self.log)
        return self.runner

    def wait_for(self, predicate, timeout=7):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            if self.runner and self.runner.poll() is not None:
                self.fail('自有监督进程提前退出：' + (self.root / 'runner.log').read_text())
            time.sleep(.02)
        self.fail('自有进程没有在期限内达到观察条件')

    def test_main_restart_preserves_control_process_and_renewal(self):
        self.launch('main')
        original = self.wait_for(lambda: rows(self.root / 'daemon.jsonl'))[0]
        control = self.wait_for(lambda: rows(self.root / 'control.jsonl'))[0]
        old_identity = identity(original['pid'])
        control_identity = identity(control['pid'])
        self.assertEqual(old_identity[0], self.runner.pid)
        self.assertEqual(control_identity[0], self.runner.pid)
        self.wait_for(lambda: (self.root / 'heartbeat').exists())
        heartbeat = int((self.root / 'heartbeat').read_text())
        (self.root / 'durable-journal').write_text('原操作及预算不得删除')
        (self.root / 'capacity-fails').touch()
        handle = os.pidfd_open(original['pid'])
        try:
            self.assertEqual(identity(original['pid']), old_identity)
            executable = Path('/proc', str(original['pid']), 'exe')
            executable_sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
            self.assertEqual(executable_sha256,
                             hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest())
            self.assertEqual(identity(original['pid']), old_identity)
            (self.root / ('exit-' + str(original['pid']))).touch()
            poller = select.poll();poller.register(handle, select.POLLIN)
            self.assertTrue(poller.poll(3000))
            after = self.wait_for(lambda: rows(self.root / 'daemon.jsonl')
                                  if len(rows(self.root / 'daemon.jsonl')) >= 2 else None)[1]
            self.assertNotEqual(after['pid'], original['pid'])
            self.assertEqual(identity(after['pid'])[0], self.runner.pid)
            control_after = identity(control['pid'])
            self.assertEqual(control_after, control_identity)
            self.wait_for(lambda: int((self.root / 'heartbeat').read_text() or '0') > heartbeat)
            (self.root / 'restart-observation.json').write_text(json.dumps({
                'old_daemon': original, 'old_identity': old_identity,
                'old_pidfd_exited': True, 'old_exe_sha256': executable_sha256,
                'exit_method': '自有子进程收到绑定自身 PID 的文件请求后执行 os._exit(9)，没有外部 SIGKILL',
                'new_daemon': after,
                'new_identity': identity(after['pid']), 'control': control,
                'control_before': control_identity, 'control_after': control_after,
                'heartbeat_before': heartbeat,
                'heartbeat_after': int((self.root / 'heartbeat').read_text() or '0'),
                'parent_pid': self.runner.pid,
            }, ensure_ascii=False, indent=2))
            self.assertEqual((self.root / 'durable-journal').read_text(), '原操作及预算不得删除')
            renewals = rows(self.root / 'lease.jsonl')
            self.assertGreaterEqual(sum(row.get('failed', False) for row in renewals), 2)
            (self.root / 'capacity-fails').unlink()
            self.wait_for(lambda: rows(self.root / 'lease.jsonl')[-1].get('failed') is False)
            new_handle = os.pidfd_open(after['pid'])
            control_handle = os.pidfd_open(control['pid'])
            try:
                (self.root / 'stop').touch()
                self.assertEqual(self.runner.wait(timeout=8), 0)
                for fd in (new_handle, control_handle):
                    p = select.poll();p.register(fd, select.POLLIN)
                    self.assertTrue(p.poll(1000))
                self.assertEqual(rows(self.root / 'lease.jsonl')[-1], {'event': 'close'})
                self.assertEqual(len(rows(self.root / 'daemon.jsonl')), 2)
            finally:
                os.close(new_handle);os.close(control_handle)
        finally:
            os.close(handle)

    def test_stale_socket_does_not_start_control_service(self):
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(self.root / 'daemon.sock'))
        self.launch('never-listen')
        self.assertNotEqual(self.runner.wait(timeout=8), 0)
        self.assertEqual(rows(self.root / 'control.jsonl'), [])
        self.assertEqual(rows(self.root / 'lease.jsonl')[-1], {'event': 'close'})
        self.assertIn('sandboxd 未在期限内提供服务', (self.root / 'runner.log').read_text())
        daemon = rows(self.root / 'daemon.jsonl')[0]
        self.assertFalse(Path('/proc', str(daemon['pid'])).exists())

    def test_control_failure_remains_fatal_and_reaps_daemon(self):
        self.launch('exit')
        self.assertNotEqual(self.runner.wait(timeout=8), 0)
        self.assertIn('调度服务意外退出', (self.root / 'runner.log').read_text())
        daemon = rows(self.root / 'daemon.jsonl')[0]
        self.assertFalse(Path('/proc', str(daemon['pid'])).exists())
        self.assertEqual(len(rows(self.root / 'daemon.jsonl')), 1)
        self.assertEqual(rows(self.root / 'lease.jsonl')[-1], {'event': 'close'})

    def test_stop_requested_before_start_does_not_spawn_children(self):
        (self.root / 'stop').touch()
        self.launch()
        self.assertEqual(self.runner.wait(timeout=8), 0)
        self.assertEqual(rows(self.root / 'daemon.jsonl'), [])
        self.assertEqual(rows(self.root / 'control.jsonl'), [])
        self.assertEqual(rows(self.root / 'lease.jsonl'), [{'event': 'close'}])

    def test_spawn_error_retries_are_bounded_and_close_prevents_retry(self):
        daemon = entry.SandboxdSupervisor([str(self.root / 'missing')], {})
        with patch.object(entry.subprocess, 'Popen', wraps=subprocess.Popen) as spawn:
            try:
                for _ in range(100):
                    daemon.tick()
                self.assertEqual(spawn.call_count, 1)
                time.sleep(1.05)
                daemon.tick()
                self.assertEqual(spawn.call_count, 2)
                daemon.close()
                for _ in range(100):
                    daemon.tick()
                self.assertEqual(spawn.call_count, 2)
            finally:
                daemon.close()


if __name__ == '__main__':
    unittest.main()
