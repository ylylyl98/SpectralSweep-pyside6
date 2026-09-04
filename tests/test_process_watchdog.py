import json, tempfile, types, unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import app.process_watchdog as pw
from app.process_watchdog import WatchdogMonitor, WatchdogSession, _atomic_write

class TestWatchdog(unittest.TestCase):
 def path(self): return Path(tempfile.gettempdir()) / "sw-test-watchdog.json"
 def write(self,p,state="running",hb=100,token="t",pid=4): _atomic_write(p,{"token":token,"pid":pid,"state":state,"heartbeat":hb})
 def test_dead_crash_once(self):
  p=self.path(); self.write(p); out=[]; m=WatchdogMonitor(p,"t",4,lambda t,x: out.append(t),lambda _:False,lambda:100); self.assertEqual(m.step()[0],"Spectra Sweep CRASH"); self.assertIsNone(m.step()); self.assertEqual(out,["Spectra Sweep CRASH"])
 def test_stale_live(self):
  p=self.path(); self.write(p); out=[]; m=WatchdogMonitor(p,"t",4,lambda t,x: out.append(t),lambda _:True,lambda:131); self.assertEqual(m.step()[0],"Spectra Sweep UNRESPONSIVE")
 def test_windows_liveness_still_active_and_exited(self):
  kernel=MagicMock(); kernel.OpenProcess.return_value=object()
  def exit_code(_handle, code): code._obj.value = 259; return 1
  kernel.GetExitCodeProcess.side_effect=exit_code
  with patch.object(pw.os,"name","nt"), patch.object(pw.ctypes,"windll",types.SimpleNamespace(kernel32=kernel)):
   self.assertTrue(pw.pid_alive(4)); kernel.GetExitCodeProcess.side_effect=lambda _h, code: setattr(code._obj,"value",1) or 1
   self.assertFalse(pw.pid_alive(4)); kernel.CloseHandle.assert_called()
 def test_windows_liveness_access_denied_is_alive(self):
  kernel=MagicMock(); kernel.OpenProcess.return_value=0; kernel.GetLastError.return_value=5
  with patch.object(pw.os,"name","nt"), patch.object(pw.ctypes,"windll",types.SimpleNamespace(kernel32=kernel)): self.assertTrue(pw.pid_alive(4))
 def test_windows_liveness_query_failure_is_alive(self):
  kernel=MagicMock(); kernel.OpenProcess.return_value=object(); kernel.GetExitCodeProcess.return_value=0
  with patch.object(pw.os,"name","nt"), patch.object(pw.ctypes,"windll",types.SimpleNamespace(kernel32=kernel)): self.assertTrue(pw.pid_alive(4))
 def test_dead_clean_marker_race_suppresses_alert(self):
  p=self.path(); self.write(p); out=[]
  def dead(_): self.write(p,state="normal_exit"); return False
  m=WatchdogMonitor(p,"t",4,lambda t,x: out.append(t),dead,lambda:100)
  self.assertIsNone(m.step()); self.assertEqual(out,[])
 def test_fresh_and_malformed(self):
  p=self.path(); self.write(p,hb=100); self.assertIsNone(WatchdogMonitor(p,"t",4,liveness=lambda _:True,now=lambda:101).step()); p.write_text("bad"); self.assertIsNone(WatchdogMonitor(p,"t",4).step())
 def test_stale_then_dead_one(self):
  p=self.path(); self.write(p); out=[]; alive=[True]; m=WatchdogMonitor(p,"t",4,lambda t,x:out.append(t),lambda _:alive[0],lambda:131); m.step(); alive[0]=False; m.step(); self.assertEqual(len(out),1)
 def test_bounded_failures(self):
  p=self.path(); self.write(p); n=[]; m=WatchdogMonitor(p,"t",4,lambda t,x:n.append(1) or False,lambda _:True,lambda:131); [m.step() for _ in range(8)]; self.assertEqual(len(n),3)
 def test_normal_close(self):
  s=WatchdogSession("x"); s.start(); s.beat(); s.close(True); self.assertFalse(s.path.exists())
 def test_default_sender_two_args(self):
  p=self.path(); self.write(p)
  with patch("urllib.request.urlopen") as u:
   u.return_value.__enter__.return_value=None
   self.assertEqual(WatchdogMonitor(p,"t",4,liveness=lambda _:False,now=lambda:100).step()[0],"Spectra Sweep CRASH")
 def test_boundary_and_pid_token_mismatch(self):
  p=self.path(); self.write(p,hb=100)
  self.assertIsNone(WatchdogMonitor(p,"t",4,lambda *_:1,lambda _:True,lambda:130).step())
  self.assertEqual(WatchdogMonitor(p,"t",4,lambda *_:1,lambda _:True,lambda:130.1).step()[0],"Spectra Sweep UNRESPONSIVE")
  self.write(p,token="wrong"); self.assertIsNone(WatchdogMonitor(p,"t",4).step())
  self.write(p,pid=9); self.assertIsNone(WatchdogMonitor(p,"t",4).step())
 def test_crash_close_preserves_running_state(self):
  s=WatchdogSession("x"); s.start(); path=s.path; s.close(False); self.assertTrue(path.exists()); self.assertEqual(json.loads(path.read_text())["state"],"running"); self.assertIsNone(s.process.poll()); s.process.terminate(); s.process.wait()
 def test_launch_arguments(self):
  s=WatchdogSession("url")
  with patch.object(pw.subprocess,"Popen",return_value=MagicMock()) as pop:
   s.start(); args,kw=pop.call_args; self.assertIn("app.process_watchdog",args[0]); self.assertIn(str(s.pid),args[0]); self.assertIn(s.token,args[0]); self.assertIn("url",args[0]); self.assertTrue(kw["close_fds"]); s.close()
 def test_module_isolated(self):
  text=Path(pw.__file__).read_text(); self.assertNotRegex(text,r"import .*\b(ui|controllers|instruments)\b")

if __name__ == "__main__": unittest.main()
