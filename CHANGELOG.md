# Changelog

## 0.1.0 (unreleased, awaiting device testing)
- First version.
- **Clean Mic** toggle: adds a virtual microphone, "Clean Mic (noise-cancelled)" (`clean_mic`), that any app can pick.
- Runs RNNoise through the system's `librnnoise_ladspa.so` (label `noise_suppressor_mono`) in a PipeWire filter-chain hosted by its own `pipewire -c ~/homebrew/settings/clean-mic/clean-mic.conf` process. No binaries are bundled, no root is needed, and nothing is written to `/etc`, `/usr` or `~/.config/pipewire`.
- **WebRTC fallback**: if RNNoise isn't installed, or keeps failing to start, PipeWire's echo-cancel module with WebRTC noise suppression is used instead (monitor mode, so no extra output device appears).
- **Strength** slider (0–100%) maps to RNNoise's voice-activity gate (0–95%) and applies live, with no restart.
- **Input mic** picker (system default or a specific device). It follows the default mic when that changes and never feeds on its own output.
- **Use as default mic** option: remembers your previous default and restores it when you turn the option off, turn Clean Mic off, uninstall it, or the filter can't run.
- Crash handling:
  - the filter dies with the plugin (PDEATHSIG);
  - it respawns with backoff (1/3/10 s);
  - after three quick failures it falls back to WebRTC, then stops cleanly with a Retry button;
  - orphans are cleaned up on start;
  - it waits for the audio session after boot or a Game/Desktop Mode switch.
- Status readout: state, engine, input device, whether the output device is present and default, CPU % of the filter process, uptime and restarts. Warns when the chosen mic is already filtered by the system.
- 5-second A/B test recording (original vs cleaned).
- `install.sh` / `uninstall.sh` one-liners. sudo is only used when Decky's plugin folder needs it and to restart Decky. The installer validates zip paths before extracting. The uninstaller restores the default mic first, and `PURGE=1` removes settings.
