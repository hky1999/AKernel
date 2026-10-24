"""Subpool and controller observations may only tighten admission."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from test_scheduler_pressure import policy,metrics
spec=importlib.util.spec_from_file_location('runtime_pressure',Path(__file__).with_name('scheduler-pressure.py'))
pressure=importlib.util.module_from_spec(spec);spec.loader.exec_module(pressure)
class RuntimePressureTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  parent=self.root/'parent';parent.mkdir();(parent/'memory.max').write_text('1000');(parent/'cpu.max').write_text('400000 100000');state=self.root/'state';state.mkdir();st=state.stat();pins=[dict(path=str(state),device=st.st_dev,inode=st.st_ino)];st=parent.stat();boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
  self.pool=self.root/'pool';self.pool.mkdir();self.pool.joinpath('memory.max').write_text('1024');self.pool.joinpath('memory.current').write_text('100');self.pool.joinpath('memory.pressure').write_text('some avg10=0.00 avg60=0.00 total=0\n');ps=self.pool.stat();self.guard=self.root/'guard.json';self.gid=dict(host_id='host',boot_id=boot,parent_device=st.st_dev,parent_inode=st.st_ino,profile_sha256='a'*64,pool_device=ps.st_dev,pool_inode=ps.st_ino)
  self.config=dict(identity=dict(host_id='host',authority='authority',boot_id=boot,parent_device=st.st_dev,parent_inode=st.st_ino,storage_identity_sha256=pressure.identity_digest(pins)),parent_path=str(parent),parent_memory_max_bytes=1000,parent_cpu_max='400000 100000',storage_pins=pins,disk_path=str(state),output=str(self.root/'pressure.json'),valid_for_millis=3000,sample_interval_millis=1000,policy=policy(),runtime=dict(pool=dict(path=str(self.pool),device=ps.st_dev,inode=ps.st_ino,memory_max_bytes=1024,high_bytes=640,low_bytes=512,psi_high_per_mille=20,psi_low_per_mille=5),guard=dict(path=str(self.guard),identity=self.gid,max_age_millis=3000)))
  self.publish();self.observer=pressure.HostObserver(self.config);self.addCleanup(self.observer.close)
 def publish(self,**extra):
  now=time.time_ns()//1000000;v=dict(schema=1,identity=self.gid,sequence=1,control_session='c'*32,observed_unix_millis=now,valid_until_unix_millis=now+3000,admission_open=True,reason='ready');v.update(extra);self.guard.write_text(json.dumps(v))
 def step(self,n):
  with patch.object(self.observer,'sample',return_value=metrics()),patch.object(pressure.time,'monotonic_ns',return_value=n*1000000000):return self.observer.step()
 def open(self):
  for n in range(6):v=self.step(n)
  self.assertTrue(v['accepting'])
 def test_pool_pressure_closes_without_falsifying_host_metrics(self):
  self.open();self.pool.joinpath('memory.current').write_text('640');v=self.step(6);self.assertFalse(v['accepting']);self.assertEqual('high_watermark',v['reason']);self.assertEqual(100,v['metrics']['parent_memory_per_mille']);self.assertEqual(100,v['metrics']['parent_memory_current_bytes'])
 def test_missing_stale_or_unavailable_emergency_guard_cannot_open(self):
  self.open();self.publish(admission_open=False,reason='emergency_unavailable');v=self.step(6);self.assertFalse(v['accepting']);self.assertEqual('runtime_guard_closed',v['reason']);self.publish(valid_until_unix_millis=1);self.assertEqual('observation_failed',self.step(7)['reason']);self.guard.write_text('{}');self.assertEqual('observation_failed',self.step(8)['reason'])
 def test_pool_or_guard_identity_change_fails_closed(self):
  self.open();self.pool.joinpath('memory.max').write_text('2048');self.assertEqual('observation_failed',self.step(6)['reason']);self.pool.joinpath('memory.max').write_text('1024');self.publish(identity=dict(self.gid,pool_inode=1));self.assertEqual('observation_failed',self.step(7)['reason'])
 def test_low_pool_window_is_required_after_a_high_event(self):
  self.open();self.pool.joinpath('memory.current').write_text('640');self.step(6);self.pool.joinpath('memory.current').write_text('600');self.assertFalse(self.step(7)['accepting']);self.pool.joinpath('memory.current').write_text('500');self.assertFalse(self.step(8)['accepting'])
  for second in range(9,13):self.assertFalse(self.step(second)['accepting'])
  self.assertTrue(self.step(13)['accepting'])
 def test_parent_pressure_cannot_be_overridden_by_open_runtime_guard(self):
  self.open()
  with patch.object(self.observer,'sample',return_value=metrics(parent_memory_per_mille=750)),patch.object(pressure.time,'monotonic_ns',return_value=6000000000):v=self.observer.step()
  self.assertFalse(v['accepting'])
if __name__=='__main__':unittest.main()
