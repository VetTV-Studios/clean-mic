"""Clean Mic core logic: engine discovery, PipeWire config generation and parsing.

Pure functions only (no Decky imports), so everything here is unit-testable on any machine.
The plugin never ships a denoiser binary. It uses the RNNoise LADSPA plugin that SteamOS and
Bazzite already install (werman/noise-suppression-for-voice, GPL-3.0, loaded by PipeWire in a
separate process) or, if that is missing, PipeWire's own WebRTC audio-processing module.
"""
import json
import os
import re

NODE_NAME = "clean_mic"                       # the virtual source apps pick
NODE_DESC = "Clean Mic (noise-cancelled)"
CAPTURE_NAME = "clean_mic.capture"           # the stream that reads the real mic
CONF_NAME = "clean-mic.conf"
ECHO_REF_NAME = "clean_mic.echo-reference"   # webrtc fallback: reads the speakers' monitor

RNNOISE_FILE = "librnnoise_ladspa.so"
RNNOISE_LABEL = "noise_suppressor_mono"
RNNOISE_NODE = "rnnoise"                      # node name inside filter.graph
VAD_KEY = "VAD Threshold (%)"
VAD_GRACE_KEY = "VAD Grace Period (ms)"
RETRO_KEY = "Retroactive VAD Grace (ms)"
VAD_GRACE_MS = 200                            # spec 08: fixed
RETRO_MS = 0                                  # spec 08: 0 = no added latency

WEBRTC_LIB = "libspa-aec-webrtc.so"

ENGINES = ("rnnoise", "webrtc")
ENGINE_LABELS = {"rnnoise": "RNNoise", "webrtc": "WebRTC (fallback)"}

DEFAULT_SETTINGS = {
    "enabled": False,
    "strength": 70,          # 0..100
    "input": "",             # node.name of the real mic; "" = the system default mic
    "make_default": False,   # make Clean Mic the system default mic (all apps)
    "engine": "auto",        # auto | rnnoise | webrtc
}

# Sources that already run a denoiser (SteamOS's Deck mic chain, Bazzite profiles, Easy Effects...).
ALREADY_DENOISED_RE = re.compile(r"rnnoise|denois|noise.?cancel|noise.?suppress|filter.?chain|echo.?cancel|easyeffects",
                                 re.IGNORECASE)


# ---------------------------------------------------------------- settings
def clamp_strength(v):
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        v = DEFAULT_SETTINGS["strength"]
    return max(0, min(100, v))


def normalize_settings(raw):
    s = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        for k in DEFAULT_SETTINGS:
            if k in raw:
                s[k] = raw[k]
    s["enabled"] = bool(s["enabled"])
    s["make_default"] = bool(s["make_default"])
    s["strength"] = clamp_strength(s["strength"])
    s["input"] = s["input"] if isinstance(s["input"], str) and is_safe_node_name(s["input"]) else ""
    if s["input"] in (NODE_NAME, CAPTURE_NAME):
        s["input"] = ""
    s["engine"] = s["engine"] if s["engine"] in ("auto",) + ENGINES else "auto"
    return s


def vad_threshold(strength):
    """Strength 0..100 -> RNNoise VAD threshold 0..95 (integer port, range 0..99).

    0 disables the voice gate (pure RNNoise denoise), 70 -> 66, 100 -> 95 (werman recommends
    85-95 for aggressive gating)."""
    return int(round(clamp_strength(strength) * 0.95))


_NODE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:\-@+]{1,200}$")


def is_safe_node_name(name):
    """Node names go into a PipeWire SPA-JSON config and an argv; only allow plain tokens."""
    return bool(name) and bool(_NODE_NAME_RE.match(name))


# ---------------------------------------------------------------- engine discovery
def ladspa_dirs(env=None, home=None):
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    dirs = []
    for d in (env.get("LADSPA_PATH") or "").split(":"):
        if d:
            dirs.append(d)
    dirs += [
        "/usr/lib64/ladspa",                    # Fedora / Bazzite (steamdeck-dsp)
        "/usr/lib/ladspa",                      # Arch / SteamOS
        "/usr/lib/x86_64-linux-gnu/ladspa",     # Debian / Ubuntu
        "/usr/local/lib/ladspa",
        "/usr/local/lib64/ladspa",
        os.path.join(home, ".ladspa"),
        os.path.join(home, "homebrew", "data", "clean-mic", "ladspa"),   # user-supplied copy
    ]
    out = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return out


def find_rnnoise(env=None, home=None, exists=os.path.isfile):
    for d in ladspa_dirs(env, home):
        p = os.path.join(d, RNNOISE_FILE)
        if exists(p):
            return p
    return None


def spa_dirs(env=None):
    env = os.environ if env is None else env
    dirs = []
    if env.get("SPA_PLUGIN_DIR"):
        dirs += env["SPA_PLUGIN_DIR"].split(":")
    dirs += ["/usr/lib64/spa-0.2", "/usr/lib/spa-0.2", "/usr/lib/x86_64-linux-gnu/spa-0.2",
             "/usr/local/lib64/spa-0.2", "/usr/local/lib/spa-0.2"]
    return dirs


def find_webrtc(env=None, exists=os.path.isfile):
    for d in spa_dirs(env):
        p = os.path.join(d, "aec", WEBRTC_LIB)
        if exists(p):
            return p
    return None


def pick_engine(pref, rnnoise_path, webrtc_path, rnnoise_failed=False):
    """Return (engine or None, reason)."""
    if pref == "webrtc":
        if webrtc_path:
            return "webrtc", "WebRTC selected"
        return (("rnnoise", "WebRTC module not found, using RNNoise") if rnnoise_path and not rnnoise_failed
                else (None, "No noise-suppression engine found"))
    if rnnoise_path and not rnnoise_failed:
        return "rnnoise", "RNNoise found at " + rnnoise_path
    if webrtc_path:
        why = "RNNoise failed to start" if rnnoise_failed else "RNNoise plugin not installed"
        return "webrtc", why + ", using PipeWire's WebRTC noise suppression"
    return None, ("RNNoise failed to start and the WebRTC module is missing" if rnnoise_failed
                  else "RNNoise plugin (librnnoise_ladspa.so) not found and the WebRTC module is missing")


# ---------------------------------------------------------------- config generation
def _q(s):
    """Quote a string for PipeWire SPA-JSON."""
    return json.dumps(str(s))


_HEADER = """# Generated by the Clean Mic Decky plugin. Do not edit: it is rewritten on every start.
# Runs as its own `pipewire -c` client process; nothing is installed into the system config.
context.properties = {
    log.level = 2
}
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt
        args = { nice.level = -11 }
        flags = [ ifexists nofail ]
    }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
"""


def _capture_props(target):
    lines = [
        f"node.name = {_q(CAPTURE_NAME)}",
        f"node.description = {_q('Clean Mic input')}",
        "node.passive = true",
        "audio.rate = 48000",
        "audio.channels = 1",
        "audio.position = [ MONO ]",
        "stream.dont-remix = false",
    ]
    if target:
        lines.append(f"target.object = {_q(target)}")
        lines.append(f"node.target = {_q(target)}")   # PipeWire < 0.3.64 spelling
    return lines


def _source_props():
    return [
        f"node.name = {_q(NODE_NAME)}",
        f"node.description = {_q(NODE_DESC)}",
        "media.class = Audio/Source",
        "audio.rate = 48000",
        "audio.channels = 1",
        "audio.position = [ MONO ]",
        "clean_mic.managed = true",
    ]


def _block(name, lines, indent):
    pad = " " * indent
    inner = "\n".join(pad + "    " + l for l in lines)
    return f"{pad}{name} = {{\n{inner}\n{pad}}}"


def build_config(engine, *, target, strength=70, rnnoise_path=None):
    """Return the text of a standalone PipeWire client config for the chosen engine."""
    if target and not is_safe_node_name(target):
        raise ValueError("unsafe input node name")
    if engine == "rnnoise":
        if not rnnoise_path:
            raise ValueError("rnnoise_path required")
        body = f"""    {{ name = libpipewire-module-filter-chain
        args = {{
            node.description = {_q(NODE_DESC)}
            media.name = {_q(NODE_DESC)}
            node.latency = 480/48000
            filter.graph = {{
                nodes = [
                    {{
                        type = ladspa
                        name = {RNNOISE_NODE}
                        plugin = {_q(rnnoise_path)}
                        label = {RNNOISE_LABEL}
                        control = {{
                            {_q(VAD_KEY)} = {vad_threshold(strength)}
                            {_q(VAD_GRACE_KEY)} = {VAD_GRACE_MS}
                            {_q(RETRO_KEY)} = {RETRO_MS}
                        }}
                    }}
                ]
            }}
            audio.rate = 48000
            audio.channels = 1
            audio.position = [ MONO ]
{_block("capture.props", _capture_props(target), 12)}
{_block("playback.props", _source_props(), 12)}
        }}
    }}
"""
    elif engine == "webrtc":
        # monitor.mode: no extra sink is created; the default sink's monitor is the echo
        # reference, so game audio leaking from the speakers into the mic is also removed.
        body = f"""    {{ name = libpipewire-module-echo-cancel
        args = {{
            library.name = aec/libspa-aec-webrtc
            monitor.mode = true
            node.latency = 480/48000
            audio.rate = 48000
            audio.channels = 1
            audio.position = [ MONO ]
            aec.args = {{
                webrtc.noise_suppression = true
                webrtc.high_pass_filter = true
                webrtc.gain_control = false
                webrtc.voice_detection = false
                webrtc.extended_filter = true
                webrtc.delay_agnostic = true
            }}
{_block("capture.props", _capture_props(target), 12)}
{_block("source.props", _source_props(), 12)}
{_block("sink.props", ["node.name = " + _q(ECHO_REF_NAME), "node.description = " + _q("Clean Mic echo reference")], 12)}
        }}
    }}
"""
    else:
        raise ValueError(f"unknown engine {engine!r}")
    return _HEADER + body + "]\n"


def set_param_args(node_id, strength):
    """argv for a live VAD update without restarting the filter (pw-cli set-param)."""
    pod = '{ params = [ ' + _q(f"{RNNOISE_NODE}:{VAD_KEY}") + f' {float(vad_threshold(strength))} ] }}'
    return ["pw-cli", "set-param", str(int(node_id)), "Props", pod]


# ---------------------------------------------------------------- pw-dump parsing
def _nodes(dump):
    for o in dump or []:
        if isinstance(o, dict) and o.get("type", "").endswith(":Node"):
            props = (o.get("info") or {}).get("props") or {}
            yield o, props


def list_mics(dump):
    """Real microphones (Audio/Source nodes), excluding Clean Mic itself and monitors."""
    out = []
    for o, p in _nodes(dump):
        mc = p.get("media.class", "")
        name = p.get("node.name", "")
        if mc not in ("Audio/Source", "Audio/Source/Virtual", "Audio/Duplex"):
            continue
        if not name or name.startswith(NODE_NAME) or name.endswith(".monitor"):
            continue
        if str(p.get("clean_mic.managed", "")).lower() == "true":
            continue
        desc = p.get("node.description") or p.get("node.nick") or name
        out.append({"id": o.get("id"), "name": name, "desc": desc,
                    "denoised": bool(ALREADY_DENOISED_RE.search(name + " " + desc))})
    return out


def find_node(dump, name):
    for o, p in _nodes(dump):
        if p.get("node.name") == name:
            return {"id": o.get("id"), "props": p,
                    "state": (o.get("info") or {}).get("state")}
    return None


def node_has_param(dump, node_id, key):
    """True if the node's Props params expose `key` (filter-chain control)."""
    for o in dump or []:
        if o.get("id") != node_id:
            continue
        for prop in ((o.get("info") or {}).get("params") or {}).get("Props") or []:
            params = prop.get("params") if isinstance(prop, dict) else None
            if isinstance(params, list) and key in params[0::2]:
                return True
    return False


def control_node_id(dump):
    """The node that accepts live control updates (the filter-chain capture/input node)."""
    key = f"{RNNOISE_NODE}:{VAD_KEY}"
    for name in (CAPTURE_NAME, NODE_NAME):
        n = find_node(dump, name)
        if n and node_has_param(dump, n["id"], key):
            return n["id"]
    n = find_node(dump, CAPTURE_NAME)
    return n["id"] if n else None


def _meta_value(v):
    if isinstance(v, dict):
        return v.get("name")
    if isinstance(v, str):
        try:
            j = json.loads(v)
            return j.get("name") if isinstance(j, dict) else None
        except ValueError:
            return v or None
    return None


def default_sources(dump):
    """(configured, effective) default source names from the 'default' metadata object."""
    conf = eff = None
    for o in dump or []:
        if o.get("type", "").endswith(":Metadata") and ((o.get("props") or {}).get("metadata.name") == "default"):
            for m in o.get("metadata") or []:
                if m.get("key") == "default.configured.audio.source":
                    conf = _meta_value(m.get("value"))
                elif m.get("key") == "default.audio.source":
                    eff = _meta_value(m.get("value"))
    return conf, eff


def resolve_target(setting_input, dump):
    """Concrete mic node.name the filter should read.

    Never returns Clean Mic itself: if the system default is (or is configured to be) Clean Mic,
    we fall back to the first real mic so the filter can't feed on its own output."""
    mics = list_mics(dump)
    names = [m["name"] for m in mics]
    if setting_input and setting_input in names:
        return setting_input
    conf, eff = default_sources(dump)
    for cand in (eff, conf):
        if cand and cand in names:
            return cand
    real = [m for m in mics if not m["denoised"]] or mics
    return real[0]["name"] if real else None


# ---------------------------------------------------------------- misc helpers
def proc_cpu_ticks(pid, read=None):
    """utime+stime ticks of a process from /proc/<pid>/stat, or None."""
    try:
        if read is None:
            with open(f"/proc/{pid}/stat") as f:
                data = f.read()
        else:
            data = read(pid)
        rest = data[data.rindex(")") + 2:].split()
        return int(rest[11]) + int(rest[12])
    except Exception:
        return None


def is_our_child(cmdline, conf_path):
    """True if a /proc cmdline (NUL-separated) is a pipewire process running our config."""
    parts = [p for p in cmdline.split("\0") if p]
    return bool(parts) and os.path.basename(parts[0]).startswith("pipewire") and conf_path in parts
