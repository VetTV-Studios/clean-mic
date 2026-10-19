"""Backend behaviour tests with a simulated PipeWire session (no audio stack needed)."""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import _fake_decky  # noqa: E402
_fake_decky.install()
import main  # noqa: E402
import cleanmic as cm  # noqa: E402


class FakeProc:
    def __init__(self, world, engine, die_at_once):
        self.world, self.engine = world, engine
        self.pid = os.getpid()
        self.returncode = None
        self._done = asyncio.Event()
        if die_at_once:
            self._exit(1)
        else:
            world["clean_node"] = True

    def _exit(self, rc):
        if self.returncode is None:
            self.returncode = rc
            self.world["clean_node"] = False
            self._done.set()

    def terminate(self):
        self._exit(-15)

    def kill(self):
        self._exit(-9)

    async def wait(self):
        await self._done.wait()
        return self.returncode


class World:
    """A tiny fake PipeWire graph + default-source metadata."""

    def __init__(self):
        self.w = {"clean_node": False, "configured": "alsa_input.internal", "effective": "alsa_input.internal",
                  "mics": ["alsa_input.internal", "alsa_input.usb"], "session": True,
                  "fail_engines": set(), "spawned": [], "set_default_calls": [], "cleared": 0, "runs": []}

    def dump(self):
        w = self.w
        if not w["session"]:
            return None
        d = [{"id": i + 10, "type": "PipeWire:Interface:Node",
              "info": {"props": {"node.name": n, "media.class": "Audio/Source", "node.description": n.upper()}}}
             for i, n in enumerate(w["mics"])]
        if w["clean_node"]:
            d.append({"id": 51, "type": "PipeWire:Interface:Node",
                      "info": {"props": {"node.name": cm.CAPTURE_NAME, "media.class": "Stream/Input/Audio"},
                               "params": {"Props": [{"params": ["rnnoise:VAD Threshold (%)", 66]}]}}})
            d.append({"id": 52, "type": "PipeWire:Interface:Node",
                      "info": {"props": {"node.name": cm.NODE_NAME, "media.class": "Audio/Source",
                                         "clean_mic.managed": "true"}}})
        md = []
        if w["configured"]:
            md.append({"key": "default.configured.audio.source", "value": {"name": w["configured"]}})
        eff = w["effective"]
        if eff == cm.NODE_NAME and not w["clean_node"]:
            eff = w["mics"][0]
        if eff:
            md.append({"key": "default.audio.source", "value": {"name": eff}})
        d.append({"id": 99, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
                  "metadata": md})
        return d


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class Base(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.world = World()
        w = self.world.w
        self.rn_path = "/usr/lib64/ladspa/librnnoise_ladspa.so"
        self.wr_path = "/usr/lib64/spa-0.2/aec/libspa-aec-webrtc.so"
        self.have_rn, self.have_wr = True, True

        async def pw_dump():
            return self.world.dump()

        async def set_default(name):
            w["set_default_calls"].append(name)
            w["configured"] = name
            w["effective"] = name
            return True

        async def clear_default():
            w["cleared"] += 1
            w["configured"] = None
            w["effective"] = w["mics"][0]
            return True

        async def fake_run(argv, timeout=5.0):
            w["runs"].append(argv)
            return 0, ""

        async def spawn(plugin, engine, target, rnnoise_path):
            w["spawned"].append((engine, target))
            main._write_text(main.CONF_FILE, cm.build_config(engine, target=target, strength=plugin.settings["strength"],
                                                             rnnoise_path=rnnoise_path))
            plugin.proc = FakeProc(w, engine, engine in w["fail_engines"])
            plugin.started_at = main.time.monotonic()

        patches = [
            mock.patch.object(main, "_pw_dump", pw_dump),
            mock.patch.object(main, "_set_default_source", set_default),
            mock.patch.object(main, "_clear_default_source", clear_default),
            mock.patch.object(main, "_run", fake_run),
            mock.patch.object(main.Plugin, "_spawn", spawn),
            mock.patch.object(main, "_find_orphans", lambda conf: []),
            mock.patch.object(main, "_pw_socket", lambda: "/"),
            mock.patch.object(main.shutil, "which", lambda *a, **k: "/usr/bin/pipewire"),
            mock.patch.object(main.cm, "find_rnnoise", lambda **k: self.rn_path if self.have_rn else None),
            mock.patch.object(main.cm, "find_webrtc", lambda **k: self.wr_path if self.have_wr else None),
            mock.patch.object(main, "BACKOFF", (0, 0, 0)),
            mock.patch.object(main, "NODE_WAIT_S", 0.3),
            mock.patch.object(main, "RETARGET_EVERY_S", 0.05),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        main.shutil.rmtree(main.SETTINGS_DIR, ignore_errors=True)
        self.p = main.Plugin()
        self.p._ready = False

    def tearDown(self):
        async def stop():
            await self.p._stop_supervisor()
        self.loop.run_until_complete(stop())
        self.loop.close()

    async def until(self, cond, timeout=3.0):
        end = self.loop.time() + timeout
        while self.loop.time() < end:
            if cond():
                return True
            await asyncio.sleep(0.02)
        return False


class DefaultMic(Base):
    def test_apply_records_previous_and_restore_puts_it_back(self):
        async def go():
            self.p._init()
            self.world.w["clean_node"] = True
            await self.p._apply_default()
            self.assertEqual(self.world.w["configured"], cm.NODE_NAME)
            self.assertEqual(self.p.state, {"we_set_default": True, "prev_default": "alsa_input.internal"})
            with open(main.STATE_FILE) as f:
                self.assertTrue(json.load(f)["we_set_default"])          # persisted for crash recovery
            await self.p._restore_default()
            self.assertEqual(self.world.w["configured"], "alsa_input.internal")
            self.assertFalse(self.p.state["we_set_default"])
        run(go())

    def test_restore_clears_when_previous_mic_is_gone(self):
        async def go():
            self.p._init()
            self.world.w["clean_node"] = True
            await self.p._apply_default()
            self.world.w["mics"] = ["alsa_input.usb"]
            await self.p._restore_default()
            self.assertEqual(self.world.w["cleared"], 1)
        run(go())

    def test_restore_leaves_a_user_choice_alone(self):
        async def go():
            self.p._init()
            self.world.w["clean_node"] = True
            await self.p._apply_default()
            self.world.w["configured"] = "alsa_input.usb"      # user picked another mic meanwhile
            n = len(self.world.w["set_default_calls"])
            await self.p._restore_default()
            self.assertEqual(len(self.world.w["set_default_calls"]), n)
            self.assertEqual(self.world.w["configured"], "alsa_input.usb")
            self.assertFalse(self.p.state["we_set_default"])
        run(go())

    def test_restore_waits_for_a_session(self):
        async def go():
            self.p._init()
            self.p.state = {"we_set_default": True, "prev_default": "alsa_input.internal"}
            self.world.w["session"] = False
            await self.p._restore_default()
            self.assertTrue(self.p.state["we_set_default"])         # kept, retried later
        run(go())

    def test_crash_recovery_on_start(self):
        async def go():
            self.p._init()
            self.p.state = {"we_set_default": True, "prev_default": "alsa_input.internal"}
            self.world.w["configured"] = cm.NODE_NAME
            await self.p._main()
            self.assertEqual(self.world.w["configured"], "alsa_input.internal")
        run(go())


class Supervisor(Base):
    def test_enable_rnnoise_then_disable_restores(self):
        async def go():
            self.p._init()
            await self.p.set_make_default(True)
            await self.p.set_enabled(True)
            self.assertTrue(await self.until(lambda: self.p.status == "active"))
            self.assertEqual(self.p.engine, "rnnoise")
            self.assertEqual(self.world.w["spawned"][0], ("rnnoise", "alsa_input.internal"))
            self.assertTrue(await self.until(lambda: self.world.w["configured"] == cm.NODE_NAME))
            with open(main.CONF_FILE) as f:
                self.assertIn(self.rn_path, f.read())
            st = await self.p.get_status()
            self.assertTrue(st["node_present"])
            self.assertTrue(st["is_default"])
            await self.p.set_enabled(False)
            self.assertEqual(self.p.status, "off")
            self.assertFalse(self.world.w["clean_node"])
            self.assertEqual(self.world.w["configured"], "alsa_input.internal")
        run(go())

    def test_rnnoise_failing_falls_back_to_webrtc(self):
        async def go():
            self.world.w["fail_engines"] = {"rnnoise"}
            self.p._init()
            await self.p.set_enabled(True)
            self.assertTrue(await self.until(lambda: self.p.status == "active"))
            self.assertEqual(self.p.engine, "webrtc")
            self.assertEqual([e for e, _ in self.world.w["spawned"]], ["rnnoise"] * 3 + ["webrtc"])
        run(go())

    def test_everything_failing_gives_up_and_restores(self):
        async def go():
            self.world.w["fail_engines"] = {"rnnoise", "webrtc"}
            self.p._init()
            self.p.settings["make_default"] = True
            self.p.state = {"we_set_default": True, "prev_default": "alsa_input.usb"}
            self.world.w["configured"] = cm.NODE_NAME
            await self.p.set_enabled(True)
            self.assertTrue(await self.until(lambda: self.p.status == "error"))
            self.assertIn("failed to start", self.p.message)
            self.assertEqual(self.world.w["configured"], "alsa_input.usb")
            # Retry starts over from RNNoise
            self.world.w["fail_engines"] = set()
            await self.p.retry()
            self.assertTrue(await self.until(lambda: self.p.status == "active"))
            self.assertEqual(self.p.engine, "rnnoise")
        run(go())

    def test_no_engine_gives_clear_message(self):
        async def go():
            self.have_rn = self.have_wr = False
            self.p._init()
            await self.p.set_enabled(True)
            self.assertTrue(await self.until(lambda: self.p.status == "error"))
            self.assertIn("librnnoise_ladspa.so", self.p.message)
            self.assertEqual(self.world.w["spawned"], [])
        run(go())

    def test_missing_rnnoise_uses_webrtc_with_reason(self):
        async def go():
            self.have_rn = False
            self.p._init()
            await self.p.set_enabled(True)
            self.assertTrue(await self.until(lambda: self.p.status == "active"))
            self.assertEqual(self.p.engine, "webrtc")
            self.assertIn("not installed", self.p.engine_reason)
        run(go())

    def test_crash_mid_session_respawns(self):
        async def go():
            self.p._init()
            await self.p.set_enabled(True)
            self.assertTrue(await self.until(lambda: self.p.status == "active"))
            self.p.proc._exit(1)            # the child dies
            self.assertTrue(await self.until(lambda: len(self.world.w["spawned"]) == 2 and self.p.status == "active"))
            self.assertEqual(self.p.restarts, 1)
        run(go())

    def test_waits_for_session(self):
        async def go():
            self.world.w["session"] = False
            self.p._init()
            await self.p.set_enabled(True)
            self.assertTrue(await self.until(lambda: self.p.status == "waiting"))
            self.world.w["session"] = True
            self.assertTrue(await self.until(lambda: self.p.status == "active", timeout=6))
        run(go())

    def test_input_change_restarts_on_new_target(self):
        async def go():
            self.p._init()
            await self.p.set_enabled(True)
            self.assertTrue(await self.until(lambda: self.p.status == "active"))
            await self.p.set_input("alsa_input.usb")
            self.assertTrue(await self.until(lambda: self.world.w["spawned"][-1] == ("rnnoise", "alsa_input.usb")
                                             and self.p.status == "active"))
            self.assertEqual(self.p.restarts, 0)     # a planned restart is not a crash
        run(go())

    def test_default_mic_moves_follow(self):
        async def go():
            self.p._init()
            await self.p.set_enabled(True)
            self.assertTrue(await self.until(lambda: self.p.status == "active"))
            self.world.w["configured"] = self.world.w["effective"] = "alsa_input.usb"   # user plugs in a USB mic
            self.assertTrue(await self.until(lambda: self.world.w["spawned"][-1] == ("rnnoise", "alsa_input.usb")))
        run(go())

    def test_strength_is_live(self):
        async def go():
            self.p._init()
            await self.p.set_enabled(True)
            self.assertTrue(await self.until(lambda: self.p.status == "active"))
            r = await self.p.set_strength(30)
            self.assertTrue(r["live"])
            self.assertEqual(r["vad"], 28)
            self.assertIn(cm.set_param_args(51, 30), self.world.w["runs"])
            self.assertEqual(len(self.world.w["spawned"]), 1)    # no restart needed
        run(go())

    def test_unload_restores_but_keeps_enabled(self):
        async def go():
            self.p._init()
            await self.p.set_make_default(True)
            await self.p.set_enabled(True)
            self.assertTrue(await self.until(lambda: self.world.w["configured"] == cm.NODE_NAME))
            await self.p._unload()
            self.assertFalse(self.world.w["clean_node"])
            self.assertEqual(self.world.w["configured"], "alsa_input.internal")
            with open(main.SETTINGS_FILE) as f:
                self.assertTrue(json.load(f)["enabled"])            # comes back after reboot
        run(go())

    def test_uninstall_removes_settings(self):
        async def go():
            self.p._init()
            await self.p.set_enabled(True)
            self.assertTrue(await self.until(lambda: self.p.status == "active"))
            self.assertTrue(os.path.isdir(main.SETTINGS_DIR))
            await self.p._uninstall()
            self.assertFalse(os.path.exists(main.SETTINGS_DIR))
            self.assertFalse(self.world.w["clean_node"])
        run(go())


class Paths(unittest.TestCase):
    def test_settings_live_under_homebrew_settings_clean_mic(self):
        self.assertTrue(main.SETTINGS_DIR.endswith(os.path.join("homebrew", "settings", "clean-mic")))
        self.assertEqual(os.path.basename(main.CONF_FILE), "clean-mic.conf")

    def test_user_env_strips_decky_libs(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/decky/lib", "PYTHONPATH": "/x"}):
            env = main._user_env()
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertTrue(env["XDG_RUNTIME_DIR"].startswith("/run/user/"))


if __name__ == "__main__":
    unittest.main()
