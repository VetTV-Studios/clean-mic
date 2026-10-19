"""Unit tests for the pure Clean Mic logic. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cleanmic as cm  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "pw-dump-rnnoise.json")


def load_fixture():
    with open(FIX) as f:
        return json.load(f)


def node(nid, name, mclass, desc=None, **extra):
    props = {"node.name": name, "media.class": mclass}
    if desc:
        props["node.description"] = desc
    props.update(extra)
    return {"id": nid, "type": "PipeWire:Interface:Node", "info": {"props": props}}


def meta(configured=None, effective=None, as_string=False):
    md = []
    for key, val in (("default.configured.audio.source", configured), ("default.audio.source", effective)):
        if val is not None:
            v = {"name": val}
            md.append({"subject": 0, "key": key, "type": "Spa:String:JSON",
                       "value": json.dumps(v) if as_string else v})
    return {"id": 99, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"}, "metadata": md}


class Settings(unittest.TestCase):
    def test_defaults(self):
        s = cm.normalize_settings(None)
        self.assertEqual(s, cm.DEFAULT_SETTINGS)
        self.assertFalse(s["enabled"])

    def test_clamps_and_rejects_junk(self):
        s = cm.normalize_settings({"strength": "250", "engine": "magic", "input": 'evil" }\n{ x',
                                   "enabled": 1, "make_default": 0, "unknown": 5})
        self.assertEqual(s["strength"], 100)
        self.assertEqual(s["engine"], "auto")
        self.assertEqual(s["input"], "")
        self.assertTrue(s["enabled"])
        self.assertNotIn("unknown", s)

    def test_input_cannot_be_clean_mic_itself(self):
        self.assertEqual(cm.normalize_settings({"input": cm.NODE_NAME})["input"], "")
        self.assertEqual(cm.normalize_settings({"input": cm.CAPTURE_NAME})["input"], "")

    def test_vad_mapping(self):
        self.assertEqual(cm.vad_threshold(0), 0)
        self.assertEqual(cm.vad_threshold(70), 66)
        self.assertEqual(cm.vad_threshold(100), 95)
        self.assertEqual(cm.vad_threshold(-5), 0)
        self.assertEqual(cm.vad_threshold("x"), cm.vad_threshold(cm.DEFAULT_SETTINGS["strength"]))
        for v in range(0, 101):
            self.assertLessEqual(cm.vad_threshold(v), 99)   # the plugin's port range is 0..99

    def test_safe_node_names(self):
        for ok in ("alsa_input.pci-0000_04_00.6.analog-stereo", "bluez_input.AA:BB:CC:DD:EE:FF.0",
                   "alsa_input.usb-Blue_Yeti-00.mono-fallback"):
            self.assertTrue(cm.is_safe_node_name(ok), ok)
        for bad in ("", 'a"b', "a b", "a}b", "a\nb", "x" * 300):
            self.assertFalse(cm.is_safe_node_name(bad), bad)


class Discovery(unittest.TestCase):
    def test_rnnoise_search_order(self):
        seen = []

        def exists(p):
            seen.append(p)
            return p in ("/usr/lib/ladspa/librnnoise_ladspa.so", "/usr/lib64/ladspa/librnnoise_ladspa.so")
        self.assertEqual(cm.find_rnnoise(env={}, home="/h", exists=exists), "/usr/lib64/ladspa/librnnoise_ladspa.so")

    def test_ladspa_path_env_first_and_user_dir(self):
        dirs = cm.ladspa_dirs(env={"LADSPA_PATH": "/opt/l:/usr/lib/ladspa"}, home="/h")
        self.assertEqual(dirs[0], "/opt/l")
        self.assertEqual(dirs.count("/usr/lib/ladspa"), 1)
        self.assertIn("/h/homebrew/data/clean-mic/ladspa", dirs)

    def test_rnnoise_missing(self):
        self.assertIsNone(cm.find_rnnoise(env={}, home="/h", exists=lambda p: False))

    def test_webrtc_lookup(self):
        p = "/usr/lib/spa-0.2/aec/libspa-aec-webrtc.so"
        self.assertEqual(cm.find_webrtc(env={}, exists=lambda x: x == p), p)

    def test_pick_engine(self):
        rn, wr = "/r.so", "/w.so"
        self.assertEqual(cm.pick_engine("auto", rn, wr)[0], "rnnoise")
        self.assertEqual(cm.pick_engine("auto", None, wr)[0], "webrtc")
        self.assertIn("not installed", cm.pick_engine("auto", None, wr)[1])
        self.assertEqual(cm.pick_engine("auto", rn, wr, rnnoise_failed=True)[0], "webrtc")
        self.assertEqual(cm.pick_engine("webrtc", rn, wr)[0], "webrtc")
        self.assertEqual(cm.pick_engine("webrtc", rn, None)[0], "rnnoise")
        e, why = cm.pick_engine("auto", None, None)
        self.assertIsNone(e)
        self.assertIn("librnnoise_ladspa.so", why)


class Config(unittest.TestCase):
    def test_rnnoise_config(self):
        c = cm.build_config("rnnoise", target="alsa_input.mic", strength=70, rnnoise_path="/usr/lib64/ladspa/librnnoise_ladspa.so")
        self.assertIn("libpipewire-module-filter-chain", c)
        self.assertIn('plugin = "/usr/lib64/ladspa/librnnoise_ladspa.so"', c)
        self.assertIn("label = noise_suppressor_mono", c)
        self.assertIn('"VAD Threshold (%)" = 66', c)
        self.assertIn('"Retroactive VAD Grace (ms)" = 0', c)
        self.assertIn('target.object = "alsa_input.mic"', c)
        self.assertIn('node.name = "clean_mic"', c)
        self.assertIn("media.class = Audio/Source", c)
        self.assertIn("node.passive = true", c)
        self.assertIn("libpipewire-module-protocol-native", c)
        self.assertEqual(c.count("{"), c.count("}"))
        self.assertEqual(c.count("["), c.count("]"))

    def test_webrtc_config(self):
        c = cm.build_config("webrtc", target="alsa_input.mic")
        self.assertIn("libpipewire-module-echo-cancel", c)
        self.assertIn("monitor.mode = true", c)
        self.assertIn("webrtc.noise_suppression = true", c)
        self.assertIn("source.props", c)
        self.assertIn('node.name = "clean_mic"', c)
        self.assertNotIn("ladspa", c)
        self.assertEqual(c.count("{"), c.count("}"))

    def test_no_target_follows_nothing_explicit(self):
        c = cm.build_config("webrtc", target=None)
        self.assertNotIn("target.object", c)

    def test_rejects_injection(self):
        with self.assertRaises(ValueError):
            cm.build_config("rnnoise", target='x" } { evil', rnnoise_path="/r.so")
        with self.assertRaises(ValueError):
            cm.build_config("rnnoise", target="x")
        with self.assertRaises(ValueError):
            cm.build_config("nope", target="x")

    def test_plugin_path_is_quoted(self):
        c = cm.build_config("rnnoise", target="m", rnnoise_path='/weird "path"/lib.so')
        self.assertIn(r'plugin = "/weird \"path\"/lib.so"', c)

    def test_set_param_args(self):
        a = cm.set_param_args(51, 30)
        self.assertEqual(a[:4], ["pw-cli", "set-param", "51", "Props"])
        self.assertEqual(a[4], '{ params = [ "rnnoise:VAD Threshold (%)" 28.0 ] }')


class DumpParsing(unittest.TestCase):
    def test_fixture_from_real_pipewire(self):
        d = load_fixture()      # captured on PipeWire 1.4.2 with the real RNNoise child running
        self.assertIsNotNone(cm.find_node(d, cm.NODE_NAME))
        self.assertEqual(cm.control_node_id(d), cm.find_node(d, cm.CAPTURE_NAME)["id"])
        self.assertEqual([m["name"] for m in cm.list_mics(d)], ["fake_mic"])
        self.assertEqual(cm.resolve_target("", d), "fake_mic")

    def test_list_mics_filters(self):
        d = [node(1, "alsa_input.internal", "Audio/Source", "Internal Mic"),
             node(2, "clean_mic", "Audio/Source", "Clean Mic"),
             node(3, "alsa_output.speakers", "Audio/Sink"),
             node(4, "clean_mic.capture", "Stream/Input/Audio"),
             node(5, "other.thing", "Audio/Source", **{"clean_mic.managed": "true"}),
             node(6, "rnnoise_source", "Audio/Source", "Noise Canceling source"),
             node(7, "x.monitor", "Audio/Source")]
        mics = cm.list_mics(d)
        self.assertEqual([m["name"] for m in mics], ["alsa_input.internal", "rnnoise_source"])
        self.assertFalse(mics[0]["denoised"])
        self.assertTrue(mics[1]["denoised"])

    def test_default_sources_both_value_styles(self):
        for as_string in (False, True):
            d = [meta("clean_mic", "alsa_input.a", as_string=as_string)]
            self.assertEqual(cm.default_sources(d), ("clean_mic", "alsa_input.a"))
        self.assertEqual(cm.default_sources([]), (None, None))

    def test_resolve_target_never_self(self):
        d = [node(1, "alsa_input.a", "Audio/Source", "A"), node(2, "clean_mic", "Audio/Source"),
             meta("clean_mic", "clean_mic")]
        self.assertEqual(cm.resolve_target("", d), "alsa_input.a")

    def test_resolve_target_prefers_setting_then_default(self):
        d = [node(1, "a", "Audio/Source"), node(2, "b", "Audio/Source"), meta("b", "b")]
        self.assertEqual(cm.resolve_target("a", d), "a")
        self.assertEqual(cm.resolve_target("gone", d), "b")
        self.assertEqual(cm.resolve_target("", d), "b")

    def test_resolve_target_skips_already_denoised_when_guessing(self):
        d = [node(1, "rnnoise_source", "Audio/Source", "Noise Canceling source"),
             node(2, "alsa_input.raw", "Audio/Source", "Raw")]
        self.assertEqual(cm.resolve_target("", d), "alsa_input.raw")

    def test_resolve_target_none(self):
        self.assertIsNone(cm.resolve_target("", [node(1, "s", "Audio/Sink")]))

    def test_node_has_param(self):
        d = load_fixture()
        cap = cm.find_node(d, cm.CAPTURE_NAME)["id"]
        self.assertTrue(cm.node_has_param(d, cap, "rnnoise:VAD Threshold (%)"))
        self.assertFalse(cm.node_has_param(d, cap, "nope"))


class Misc(unittest.TestCase):
    def test_proc_cpu_ticks(self):
        stat = "1234 (pipe wire) S 1 2 3 4 5 6 7 8 9 10 111 222 0 0 20 0 1 0"
        self.assertEqual(cm.proc_cpu_ticks(1, read=lambda pid: stat), 333)
        self.assertIsNone(cm.proc_cpu_ticks(1, read=lambda pid: "garbage"))

    def test_is_our_child(self):
        conf = "/home/deck/homebrew/settings/clean-mic/clean-mic.conf"
        self.assertTrue(cm.is_our_child(f"/usr/bin/pipewire\0-c\0{conf}\0", conf))
        self.assertFalse(cm.is_our_child("/usr/bin/pipewire\0", conf))
        self.assertFalse(cm.is_our_child(f"/usr/bin/vim\0{conf}\0", conf))


if __name__ == "__main__":
    unittest.main()
