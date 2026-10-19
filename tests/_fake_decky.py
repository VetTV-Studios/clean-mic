"""A stand-in for Decky's `decky` module so the backend can be imported in tests."""
import logging
import sys
import tempfile
import types

HOME = tempfile.mkdtemp(prefix="cm-home-")
events = []


async def _emit(event, *args):
    events.append((event, args))


def install():
    if "decky" in sys.modules and getattr(sys.modules["decky"], "_clean_mic_fake", False):
        return sys.modules["decky"]
    mod = types.SimpleNamespace(
        _clean_mic_fake=True,
        logger=logging.getLogger("clean-mic-test"),
        DECKY_HOME=HOME + "/homebrew",
        DECKY_USER="nobody-clean-mic-test",
        DECKY_USER_HOME=HOME,
        DECKY_PLUGIN_LOG_DIR=tempfile.mkdtemp(prefix="cm-log-"),
        DECKY_PLUGIN_RUNTIME_DIR=tempfile.mkdtemp(prefix="cm-run-"),
        emit=_emit,
    )
    sys.modules["decky"] = mod
    return mod
