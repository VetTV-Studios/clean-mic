"""Clean Mic: a Decky Loader plugin that adds a noise-cancelled virtual microphone.

The backend runs as the normal user (no "_root" flag). It supervises one child process,
`pipewire -c ~/homebrew/settings/clean-mic/clean-mic.conf`, which joins the user's own
PipeWire session as a client and publishes the "clean_mic" Audio/Source. Nothing is written
to /etc, /usr or ~/.config/pipewire, and turning the feature off (or uninstalling, or a crash)
leaves the audio setup exactly as it was.
"""
import asyncio
import ctypes
import json
import os
import shutil
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import decky  # noqa: E402
import cleanmic as cm  # noqa: E402

VERSION = "0.1.0"
BACKOFF = (1, 3, 10)          # seconds between quick restarts
FAST_FAIL_S = 30              # a child that dies sooner than this counts as a failed start
NODE_WAIT_S = 6               # how long a fresh child gets to publish its node
RETARGET_EVERY_S = 10         # how often to check whether the default mic moved


def _decky_home():
    h = getattr(decky, "DECKY_HOME", None)
    if h:
        return h
    return os.path.join(getattr(decky, "DECKY_USER_HOME", None) or os.path.expanduser("~"), "homebrew")


SETTINGS_DIR = os.path.join(_decky_home(), "settings", "clean-mic")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
STATE_FILE = os.path.join(SETTINGS_DIR, "state.json")
CONF_FILE = os.path.join(SETTINGS_DIR, cm.CONF_NAME)


def _log_dir():
    d = getattr(decky, "DECKY_PLUGIN_LOG_DIR", None) or SETTINGS_DIR
    os.makedirs(d, exist_ok=True)
    return d


def _runtime_dir():
    d = getattr(decky, "DECKY_PLUGIN_RUNTIME_DIR", None) or SETTINGS_DIR
    os.makedirs(d, exist_ok=True)
    return d


# ------------------------------------------------------------------ files
def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ------------------------------------------------------------------ user session env
def _user_ids():
    try:
        import pwd
        pw = pwd.getpwnam(getattr(decky, "DECKY_USER", None) or "deck")
        return pw.pw_uid, pw.pw_gid, pw.pw_dir
    except Exception:
        return os.getuid(), os.getgid(), os.path.expanduser("~")


def _user_env():
    uid, _gid, home = _user_ids()
    env = dict(os.environ)
    rd = f"/run/user/{uid}"
    env["XDG_RUNTIME_DIR"] = rd
    env["PIPEWIRE_RUNTIME_DIR"] = rd
    env["PULSE_RUNTIME_PATH"] = f"{rd}/pulse"
    env["PULSE_SERVER"] = f"unix:{rd}/pulse/native"
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={rd}/bus")
    env["HOME"] = home
    for k in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONPATH", "PYTHONHOME", "PIPEWIRE_REMOTE"):
        env.pop(k, None)          # Decky's bundled libraries must not leak into system tools
    path = env.get("PATH") or ""
    for d in ("/usr/bin", "/bin", "/usr/local/bin"):
        if d not in path.split(":"):
            path = path + ":" + d if path else d
    env["PATH"] = path
    return env


def _pw_socket():
    uid, _gid, _home = _user_ids()
    return f"/run/user/{uid}/pipewire-0"


_PR_SET_PDEATHSIG = 1


def _preexec_child():
    """Runs in the forked child: die with the plugin, and never run audio tools as root."""
    try:
        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass
    _demote()


def _demote():
    if os.geteuid() != 0:
        return
    uid, gid, _home = _user_ids()
    if uid != 0:
        os.setgid(gid)
        os.setuid(uid)


async def _run(argv, timeout=5.0):
    """Run a helper (argv list, never a shell). Returns (rc, stdout); rc=None on failure."""
    if not shutil.which(argv[0], path=_user_env()["PATH"]):
        return None, ""
    try:
        p = await asyncio.create_subprocess_exec(
            *argv, env=_user_env(), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, stdin=asyncio.subprocess.DEVNULL, preexec_fn=_demote)
    except OSError:
        return None, ""
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        await p.wait()
        return None, ""
    return p.returncode, out.decode(errors="replace")


async def _pw_dump():
    rc, out = await _run(["pw-dump"], timeout=8)
    if rc != 0:
        return None
    try:
        return json.loads(out or "[]")
    except ValueError:
        return None


async def _set_default_source(name):
    rc, _ = await _run(["pactl", "set-default-source", name])
    if rc == 0:
        return True
    rc, _ = await _run(["pw-metadata", "0", "default.configured.audio.source",
                        json.dumps({"name": name}), "Spa:String:JSON"])
    return rc == 0


async def _clear_default_source():
    rc, _ = await _run(["pw-metadata", "-d", "0", "default.configured.audio.source"])
    return rc == 0


def _find_orphans(conf_path):
    """PIDs of leftover pipewire children running our config (e.g. after a Decky crash)."""
    pids = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for e in entries:
        if not e.isdigit() or int(e) == os.getpid():
            continue
        try:
            with open(f"/proc/{e}/cmdline", "rb") as f:
                cmd = f.read().decode(errors="replace")
        except OSError:
            continue
        if cm.is_our_child(cmd, conf_path):
            pids.append(int(e))
    return pids


# ------------------------------------------------------------------ plugin
class Plugin:
    settings = None
    state = None

    def _init(self):
        if getattr(self, "_ready", False):
            return
        self._ready = True
        self.settings = cm.normalize_settings(_read_json(SETTINGS_FILE, {}))
        st = _read_json(STATE_FILE, {})
        self.state = {"we_set_default": bool(st.get("we_set_default")),
                      "prev_default": st.get("prev_default") if isinstance(st.get("prev_default"), str) else None}
        self.proc = None
        self.task = None
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.status = "off"
        self.message = "Off"
        self.engine = None
        self.engine_reason = ""
        self.target = None
        self.target_desc = None
        self.target_denoised = False
        self.started_at = None
        self.restarts = 0
        self.fast_failures = 0
        self.rnnoise_failed = False
        self._cpu_sample = None
        self._last_cpu = None
        self._restart_requested = False

    # -------------------------------------------------------- persistence
    def _save_settings(self):
        _write_json(SETTINGS_FILE, self.settings)

    def _save_state(self):
        _write_json(STATE_FILE, self.state)

    # -------------------------------------------------------- status
    def _set(self, status, message):
        changed = (status, message) != (self.status, self.message)
        self.status, self.message = status, message
        if changed:
            decky.logger.info(f"status: {status}: {message}")
            asyncio.ensure_future(self._emit())

    async def _emit(self):
        try:
            await decky.emit("clean_mic_status", await self.get_status(light=True))
        except Exception:
            pass

    def _cpu_percent(self):
        if not self.proc or self.proc.returncode is not None:
            self._cpu_sample = None
            return None
        ticks = cm.proc_cpu_ticks(self.proc.pid)
        now = time.monotonic()
        if ticks is None:
            return None
        prev, self._cpu_sample = self._cpu_sample, (ticks, now)
        if prev and now - prev[1] >= 0.5:
            hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
            self._last_cpu = round(100.0 * (ticks - prev[0]) / hz / (now - prev[1]), 1)
        return self._last_cpu

    async def get_status(self, light=False):
        self._init()
        out = {
            "version": VERSION,
            "enabled": self.settings["enabled"],
            "strength": self.settings["strength"],
            "vad": cm.vad_threshold(self.settings["strength"]),
            "input": self.settings["input"],
            "make_default": self.settings["make_default"],
            "engine_pref": self.settings["engine"],
            "status": self.status,
            "message": self.message,
            "engine": self.engine,
            "engine_label": cm.ENGINE_LABELS.get(self.engine, "None"),
            "engine_reason": self.engine_reason,
            "rnnoise_path": cm.find_rnnoise(home=_user_ids()[2]),
            "webrtc_available": bool(cm.find_webrtc()),
            "target": self.target,
            "target_desc": self.target_desc,
            "target_denoised": self.target_denoised,
            "uptime": int(time.monotonic() - self.started_at) if self.started_at and self.status == "active" else 0,
            "restarts": self.restarts,
            "cpu": self._cpu_percent(),
            "node_present": None, "is_default": None, "mics": None,
        }
        if not light:
            dump = await _pw_dump()
            if dump is not None:
                out["node_present"] = cm.find_node(dump, cm.NODE_NAME) is not None
                conf, eff = cm.default_sources(dump)
                out["is_default"] = cm.NODE_NAME in (conf, eff)
                out["mics"] = cm.list_mics(dump)
        return out

    # -------------------------------------------------------- default mic
    async def _apply_default(self):
        dump = await _pw_dump()
        if dump is None:
            return
        conf, eff = cm.default_sources(dump)
        if not self.state["we_set_default"]:
            cur = conf or eff
            self.state["prev_default"] = cur if cur and not cur.startswith(cm.NODE_NAME) else ""
        if cm.NODE_NAME in (conf, eff) and self.state["we_set_default"]:
            return
        if await _set_default_source(cm.NODE_NAME):
            self.state["we_set_default"] = True
            self._save_state()
            decky.logger.info(f"default mic -> {cm.NODE_NAME} (was {self.state['prev_default']!r})")

    async def _restore_default(self):
        if not self.state["we_set_default"]:
            return
        dump = await _pw_dump()
        prev = self.state.get("prev_default") or ""
        if dump is not None:
            conf, eff = cm.default_sources(dump)
            still_ours = conf == cm.NODE_NAME or (conf is None and eff == cm.NODE_NAME)
            if still_ours:
                names = [m["name"] for m in cm.list_mics(dump)]
                if prev and prev in names:
                    await _set_default_source(prev)
                else:
                    await _clear_default_source()   # let WirePlumber pick the best mic again
                decky.logger.info(f"default mic restored to {prev or '(automatic)'}")
            else:
                decky.logger.info(f"default mic was changed by the user ({conf}); leaving it")
        else:
            return   # no session right now; keep the record and retry next time
        self.state = {"we_set_default": False, "prev_default": None}
        self._save_state()

    # -------------------------------------------------------- child process
    async def _spawn(self, engine, target, rnnoise_path):
        _write_text(CONF_FILE, cm.build_config(engine, target=target, strength=self.settings["strength"],
                                               rnnoise_path=rnnoise_path))
        log = open(os.path.join(_log_dir(), "pipewire-child.log"), "wb")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                "pipewire", "-c", CONF_FILE, env=_user_env(), stdin=asyncio.subprocess.DEVNULL,
                stdout=log, stderr=log, preexec_fn=_preexec_child)
        finally:
            log.close()
        _write_text(os.path.join(_runtime_dir(), "child.pid"), str(self.proc.pid))
        self.started_at = time.monotonic()
        self._cpu_sample = None
        decky.logger.info(f"spawned pipewire pid={self.proc.pid} engine={engine} target={target}")

    async def _kill_child(self):
        p, self.proc = self.proc, None
        if p and p.returncode is None:
            try:
                p.terminate()
                await asyncio.wait_for(p.wait(), 3)
            except asyncio.TimeoutError:
                p.kill()
                await p.wait()
            except ProcessLookupError:
                pass
        for pid in _find_orphans(CONF_FILE):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            os.unlink(os.path.join(_runtime_dir(), "child.pid"))
        except OSError:
            pass
        self.started_at = None

    async def _wait_node(self):
        deadline = time.monotonic() + NODE_WAIT_S
        while time.monotonic() < deadline:
            if self.proc is None or self.proc.returncode is not None:
                return False
            dump = await _pw_dump()
            if dump is not None and cm.find_node(dump, cm.NODE_NAME):
                return True
            await asyncio.sleep(0.4)
        return False

    async def _sleep_or_wake(self, seconds):
        self.wake.clear()
        try:
            await asyncio.wait_for(self.wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _supervise(self):
        """Keep the child running while enabled; respawn with backoff; fall back; give up cleanly."""
        try:
            while self.settings["enabled"]:
                if not shutil.which("pipewire", path=_user_env()["PATH"]):
                    self._set("error", "PipeWire is not installed on this system")
                    await self._sleep_or_wake(60)
                    continue
                if not os.path.exists(_pw_socket()):
                    self._set("waiting", "Waiting for the audio session (PipeWire)")
                    await self._sleep_or_wake(2)
                    continue
                dump = await _pw_dump()
                if dump is None:
                    self._set("waiting", "Waiting for the audio session (pw-dump failed)")
                    await self._sleep_or_wake(3)
                    continue
                rn = cm.find_rnnoise(home=_user_ids()[2])
                wr = cm.find_webrtc()
                engine, reason = cm.pick_engine(self.settings["engine"], rn, wr, self.rnnoise_failed)
                self.engine, self.engine_reason = engine, reason
                if engine is None:
                    await self._restore_default()
                    self._set("error", reason + ". Install RNNoise (see README) and press Retry.")
                    await self._sleep_or_wake(30)
                    continue
                target = cm.resolve_target(self.settings["input"], dump)
                if not target:
                    self._set("waiting", "No microphone found. Plug one in or pick an input.")
                    await self._sleep_or_wake(3)
                    continue
                mic = next((m for m in cm.list_mics(dump) if m["name"] == target), None)
                self.target = target
                self.target_desc = mic["desc"] if mic else target
                self.target_denoised = bool(mic and mic["denoised"])

                self._set("starting", f"Starting {cm.ENGINE_LABELS[engine]}")
                self._restart_requested = False
                await self._spawn(engine, target, rn)
                if await self._wait_node():
                    self._set("active", f"Active: {cm.ENGINE_LABELS[engine]} on {self.target_desc}")
                    if self.settings["make_default"]:
                        await self._apply_default()
                    # watch the child; re-target if the default mic moved
                    while self.proc and self.proc.returncode is None and self.settings["enabled"] \
                            and not self._restart_requested:
                        await self._wait_child(RETARGET_EVERY_S)
                        if self.proc and self.proc.returncode is None and not self.settings["input"] \
                                and not self._restart_requested:
                            d2 = await _pw_dump()
                            if d2 is not None and not cm.find_node(d2, cm.NODE_NAME):
                                decky.logger.warning("clean_mic node vanished; restarting child")
                                self._restart_requested = True
                            elif d2 is not None:
                                nt = cm.resolve_target("", d2)
                                if nt and nt != self.target:
                                    decky.logger.info(f"default mic moved {self.target} -> {nt}")
                                    self._restart_requested = True
                if not self.settings["enabled"]:
                    break
                ran = time.monotonic() - (self.started_at or time.monotonic())
                rc = self.proc.returncode if self.proc else None
                planned = self._restart_requested
                await self._kill_child()
                if planned:
                    continue
                self.restarts += 1
                self.fast_failures = self.fast_failures + 1 if ran < FAST_FAIL_S else 1
                decky.logger.warning(f"child exited rc={rc} after {ran:.1f}s (fast failures {self.fast_failures})")
                if self.fast_failures >= len(BACKOFF):
                    if engine == "rnnoise" and wr and self.settings["engine"] != "rnnoise":
                        self.rnnoise_failed = True
                        self.fast_failures = 0
                        self._set("starting", "RNNoise keeps failing; switching to WebRTC")
                        continue
                    await self._restore_default()
                    self._set("error", f"{cm.ENGINE_LABELS[engine]} failed to start "
                                       f"{self.fast_failures} times. See pipewire-child.log, then press Retry.")
                    self.fast_failures = 0
                    await self._sleep_or_wake(3600)
                    continue
                self._set("starting", f"Engine stopped; restarting in {BACKOFF[self.fast_failures - 1]} s")
                await self._sleep_or_wake(BACKOFF[self.fast_failures - 1])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never leave a half state behind
            decky.logger.exception(f"supervisor crashed: {e}")
            self._set("error", f"Internal error: {e}")
        finally:
            if not self.settings["enabled"]:
                await self._teardown()

    async def _wait_child(self, seconds):
        p = self.proc
        if not p:
            return
        self.wake.clear()
        waits = [asyncio.ensure_future(p.wait()), asyncio.ensure_future(self.wake.wait())]
        try:
            await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                if not w.done():
                    w.cancel()

    async def _teardown(self):
        await self._restore_default()
        await self._kill_child()
        self.engine = None
        self._set("off", "Off")

    def _start_supervisor(self):
        if self.task is None or self.task.done():
            self.fast_failures = 0
            self.task = asyncio.ensure_future(self._supervise())

    async def _stop_supervisor(self):
        t, self.task = self.task, None
        if t and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self._teardown()

    def _request_restart(self):
        self._restart_requested = True
        self.wake.set()

    # -------------------------------------------------------- callables
    async def set_enabled(self, enabled):
        self._init()
        async with self.lock:
            self.settings["enabled"] = bool(enabled)
            self._save_settings()
            if self.settings["enabled"]:
                self.rnnoise_failed = False
                self._start_supervisor()
            else:
                await self._stop_supervisor()
        return await self.get_status()

    async def set_strength(self, strength):
        self._init()
        self.settings["strength"] = cm.clamp_strength(strength)
        self._save_settings()
        live = False
        if self.status == "active" and self.engine == "rnnoise":
            dump = await _pw_dump()
            nid = cm.control_node_id(dump) if dump else None
            if nid is not None:
                rc, _ = await _run(cm.set_param_args(nid, self.settings["strength"]))
                live = rc == 0
            if not live:
                self._request_restart()
        return {"strength": self.settings["strength"], "vad": cm.vad_threshold(self.settings["strength"]),
                "live": live}

    async def set_input(self, name):
        self._init()
        name = name or ""
        if name and not cm.is_safe_node_name(name):
            return await self.get_status()
        self.settings["input"] = name
        self.settings = cm.normalize_settings(self.settings)
        self._save_settings()
        if self.settings["enabled"]:
            self._request_restart()
        return await self.get_status()

    async def set_engine(self, engine):
        self._init()
        self.settings["engine"] = engine if engine in ("auto",) + cm.ENGINES else "auto"
        self._save_settings()
        self.rnnoise_failed = False
        if self.settings["enabled"]:
            self._request_restart()
        return await self.get_status()

    async def set_make_default(self, on):
        self._init()
        async with self.lock:
            self.settings["make_default"] = bool(on)
            self._save_settings()
            if on and self.status == "active":
                await self._apply_default()
            elif not on:
                await self._restore_default()
        return await self.get_status()

    async def retry(self):
        self._init()
        self.rnnoise_failed = False
        self.fast_failures = 0
        if self.settings["enabled"]:
            if self.task is None or self.task.done():
                self._start_supervisor()
            else:
                self._request_restart()
        return await self.get_status()

    async def test_record(self, seconds=5):
        """Record raw and clean side by side for an A/B listen. Returns paths and levels."""
        self._init()
        if self.status != "active" or not self.target:
            return {"ok": False, "error": "Turn Clean Mic on first"}
        seconds = max(2, min(10, int(seconds)))
        rd = _runtime_dir()
        files = {"raw": os.path.join(rd, "test-raw.wav"), "clean": os.path.join(rd, "test-clean.wav")}
        procs = []
        for which, target in (("raw", self.target), ("clean", cm.NODE_NAME)):
            rc_argv = ["pw-record", "--target", target, "--rate", "48000", "--channels", "1",
                       "--format", "s16", files[which]]
            procs.append(asyncio.ensure_future(_run(rc_argv, timeout=seconds)))
        await asyncio.gather(*procs)
        ok = all(os.path.exists(f) and os.path.getsize(f) > 44 for f in files.values())
        return {"ok": ok, "files": files, "error": None if ok else "Recording failed"}

    async def test_play(self, which="clean"):
        self._init()
        f = os.path.join(_runtime_dir(), f"test-{'raw' if which == 'raw' else 'clean'}.wav")
        if not os.path.exists(f):
            return {"ok": False, "error": "Record a test first"}
        rc, _ = await _run(["pw-play", f], timeout=15)
        return {"ok": rc == 0}

    # -------------------------------------------------------- lifecycle
    async def _main(self):
        self._init()
        decky.logger.info(f"Clean Mic {VERSION} starting (uid {os.geteuid()}), settings in {SETTINGS_DIR}")
        for pid in _find_orphans(CONF_FILE):          # leftovers from a crashed Decky
            decky.logger.info(f"killing orphaned child {pid}")
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if self.settings["enabled"]:
            self._start_supervisor()
        elif self.state["we_set_default"]:
            await self._restore_default()             # crash recovery: we were off but still default

    async def _unload(self):
        self._init()
        decky.logger.info("unloading: stopping child and restoring the default mic")
        await self._stop_supervisor()

    async def _uninstall(self):
        self._init()
        decky.logger.info("uninstalling: restoring audio setup and removing settings")
        await self._stop_supervisor()
        shutil.rmtree(SETTINGS_DIR, ignore_errors=True)
