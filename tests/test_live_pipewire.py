"""Opt-in end-to-end test against a real PipeWire session.

    CLEAN_MIC_LIVE=1 LADSPA_PATH=/dir/with/librnnoise_ladspa.so python3 -m unittest tests.test_live_pipewire -v

Needs pipewire, wireplumber, pipewire-pulse (pactl) running for this user and at least one
Audio/Source. It runs the real backend (no mocks): spawns the child, checks the node, the live
VAD update, the default-mic switch and the restore on disable and on a killed child.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import _fake_decky  # noqa: E402
_fake_decky.install()
import main  # noqa: E402
import cleanmic as cm  # noqa: E402

LIVE = os.environ.get("CLEAN_MIC_LIVE") == "1"


def dump():
    return json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True, env=main._user_env()).stdout)


def default_source():
    return subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True,
                          env=main._user_env()).stdout.strip()


@unittest.skipUnless(LIVE, "set CLEAN_MIC_LIVE=1 to run against a real PipeWire session")
class Live(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        main.shutil.rmtree(main.SETTINGS_DIR, ignore_errors=True)
        self.p = main.Plugin()
        self.before = default_source()

    def tearDown(self):
        self.loop.run_until_complete(self.p._stop_supervisor())
        self.loop.close()

    def wait(self, cond, timeout=10):
        async def w():
            for _ in range(int(timeout / 0.1)):
                if cond():
                    return True
                await asyncio.sleep(0.1)
            return False
        return self.loop.run_until_complete(w())

    def test_engine(self):
        for engine in ("rnnoise", "webrtc"):
            with self.subTest(engine=engine):
                r = self.loop.run_until_complete
                r(self.p.set_engine(engine))
                r(self.p.set_make_default(True))
                r(self.p.set_enabled(True))
                self.assertTrue(self.wait(lambda: self.p.status == "active"), self.p.message)
                self.assertEqual(self.p.engine, engine)
                self.assertIsNotNone(cm.find_node(dump(), cm.NODE_NAME))
                self.assertTrue(self.wait(lambda: default_source() == cm.NODE_NAME), default_source())
                if engine == "rnnoise":
                    res = r(self.p.set_strength(20))
                    self.assertTrue(res["live"])
                    d = dump()
                    nid = cm.control_node_id(d)
                    vals = [p["params"] for o in d if o["id"] == nid
                            for p in o["info"]["params"]["Props"] if "rnnoise:VAD Threshold (%)" in (p.get("params") or [])]
                    self.assertEqual(vals[0][vals[0].index("rnnoise:VAD Threshold (%)") + 1], 19)
                    # a killed child is respawned and the default is still Clean Mic
                    pid = self.p.proc.pid
                    os.kill(pid, signal.SIGKILL)
                    self.assertTrue(self.wait(lambda: self.p.proc is not None and self.p.proc.pid != pid
                                              and self.p.status == "active"))
                    self.assertTrue(self.wait(lambda: default_source() == cm.NODE_NAME))
                    st = r(self.p.test_record(2))
                    self.assertTrue(st["ok"], st)
                r(self.p.set_enabled(False))
                self.assertIsNone(cm.find_node(dump(), cm.NODE_NAME))
                self.assertTrue(self.wait(lambda: default_source() == self.before), default_source())
                self.assertEqual(main._find_orphans(main.CONF_FILE), [])


if __name__ == "__main__":
    unittest.main()
