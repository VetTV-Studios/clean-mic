import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { addEventListener, callable, definePlugin, removeEventListener, toaster } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { CleanMicIcon } from "./icon";

type Mic = { id: number; name: string; desc: string; denoised: boolean };
type Status = {
  version: string;
  enabled: boolean;
  strength: number;
  vad: number;
  input: string;
  make_default: boolean;
  engine_pref: "auto" | "rnnoise" | "webrtc";
  status: "off" | "waiting" | "starting" | "active" | "error";
  message: string;
  engine: "rnnoise" | "webrtc" | null;
  engine_label: string;
  engine_reason: string;
  rnnoise_path: string | null;
  webrtc_available: boolean;
  target: string | null;
  target_desc: string | null;
  target_denoised: boolean;
  uptime: number;
  restarts: number;
  cpu: number | null;
  node_present: boolean | null;
  is_default: boolean | null;
  mics: Mic[] | null;
};

const getStatus = callable<[], Status>("get_status");
const setEnabled = callable<[enabled: boolean], Status>("set_enabled");
const setStrength = callable<[strength: number], { strength: number; vad: number; live: boolean }>("set_strength");
const setInput = callable<[name: string], Status>("set_input");
const setEngine = callable<[engine: string], Status>("set_engine");
const setMakeDefault = callable<[on: boolean], Status>("set_make_default");
const retry = callable<[], Status>("retry");
const testRecord = callable<[seconds: number], { ok: boolean; error?: string | null }>("test_record");
const testPlay = callable<[which: string], { ok: boolean; error?: string }>("test_play");

const DOT: Record<Status["status"], string> = {
  off: "#8b929a", waiting: "#e5b84b", starting: "#e5b84b", active: "#59bf40", error: "#e2483d",
};

function fmtUptime(s: number) {
  if (!s) return "—";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}

function Content() {
  const [st, setSt] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState<"" | "recording" | "ready">("");
  const strengthTimer = useRef<number | undefined>(undefined);

  const refresh = async () => {
    try { setSt(await getStatus()); } catch (e) { /* backend restarting */ }
  };

  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 2000);
    const l = addEventListener<[Status]>("clean_mic_status", (s) => setSt((old) => ({ ...(old ?? s), ...s,
      mics: s.mics ?? old?.mics ?? null, node_present: s.node_present ?? old?.node_present ?? null,
      is_default: s.is_default ?? old?.is_default ?? null })));
    return () => { window.clearInterval(t); removeEventListener("clean_mic_status", l); };
  }, []);

  if (!st) {
    return <PanelSection><PanelSectionRow><Field label="Loading…" /></PanelSectionRow></PanelSection>;
  }

  const run = async (f: () => Promise<Status>) => {
    setBusy(true);
    try { setSt(await f()); } finally { setBusy(false); }
  };

  const onStrength = (v: number) => {
    setSt({ ...st, strength: v, vad: Math.round(v * 0.95) });
    window.clearTimeout(strengthTimer.current);
    strengthTimer.current = window.setTimeout(() => { setStrength(v); }, 250);
  };

  const mics = st.mics ?? [];
  const inputOptions = [
    { data: "", label: "System default mic" },
    ...mics.map((m) => ({ data: m.name, label: m.desc + (m.denoised ? " (already filtered)" : "") })),
  ];
  if (st.input && !mics.some((m) => m.name === st.input)) {
    inputOptions.push({ data: st.input, label: `${st.input} (not connected)` });
  }
  const noEngine = !st.rnnoise_path && !st.webrtc_available;

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ToggleField label="Clean Mic" description="Noise-cancelled virtual microphone"
            checked={st.enabled} disabled={busy} onChange={(v) => run(() => setEnabled(v))} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Status" focusable={st.status === "error"} childrenContainerWidth="max">
            <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
              <span style={{ width: 8, height: 8, borderRadius: 4, background: DOT[st.status] }} />
              {st.message}
            </span>
          </Field>
        </PanelSectionRow>
        {st.status === "error" && (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={busy} onClick={() => run(retry)}>Retry</ButtonItem>
          </PanelSectionRow>
        )}
        {noEngine && (
          <PanelSectionRow>
            <Field label="RNNoise not found" description="Clean Mic uses the RNNoise plugin your OS provides
              (librnnoise_ladspa.so). SteamOS and Bazzite handheld images include it. See the README for
              other systems." />
          </PanelSectionRow>
        )}
        {!st.rnnoise_path && st.webrtc_available && (
          <PanelSectionRow>
            <Field label="Using WebRTC fallback" description="RNNoise isn't installed, so PipeWire's built-in
              WebRTC noise suppression is used. It's lighter but less effective. See the README to add RNNoise." />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Settings">
        <PanelSectionRow>
          <SliderField label="Strength" value={st.strength} min={0} max={100} step={5} showValue
            disabled={st.engine === "webrtc"}
            description={st.engine === "webrtc" ? "Not adjustable on the WebRTC fallback"
              : `Voice gate ${st.vad}% (0 = denoise only, higher = silence between words)`}
            onChange={onStrength} />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem label="Input mic" rgOptions={inputOptions} selectedOption={st.input}
            disabled={busy} onChange={(o) => run(() => setInput(o.data as string))} />
        </PanelSectionRow>
        {st.target_denoised && (
          <PanelSectionRow>
            <Field label="Heads up" description="This mic already runs a system noise filter. Use strength
              40% or less, or pick the raw device, to avoid a muffled voice." />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ToggleField label="Use as default mic"
            description="Steam voice chat, Discord, browsers and other apps that use the default mic get the clean
              mic. Your previous default comes back when this is turned off."
            checked={st.make_default} disabled={busy} onChange={(v) => run(() => setMakeDefault(v))} />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem label="Engine" selectedOption={st.engine_pref} disabled={busy}
            rgOptions={[
              { data: "auto", label: "Auto (RNNoise, WebRTC fallback)" },
              { data: "rnnoise", label: "RNNoise" },
              { data: "webrtc", label: "WebRTC (lighter)" },
            ]}
            onChange={(o) => run(() => setEngine(o.data as string))} />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Test">
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={st.status !== "active" || testing === "recording"}
            onClick={async () => {
              setTesting("recording");
              toaster.toast({ title: "Clean Mic", body: "Recording 5 s… talk with some noise around you" });
              const r = await testRecord(5);
              setTesting(r.ok ? "ready" : "");
              if (!r.ok) toaster.toast({ title: "Clean Mic", body: r.error ?? "Recording failed" });
            }}>
            {testing === "recording" ? "Recording…" : "Record 5 s test"}
          </ButtonItem>
        </PanelSectionRow>
        {testing === "ready" && (
          <>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => testPlay("raw")}>Play original</ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => testPlay("clean")}>Play cleaned</ButtonItem>
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      <PanelSection title="Details">
        <PanelSectionRow><Field label="Engine" childrenContainerWidth="max">{st.engine_label}</Field></PanelSectionRow>
        <PanelSectionRow><Field label="Input" childrenContainerWidth="max">{st.target_desc ?? "—"}</Field></PanelSectionRow>
        <PanelSectionRow><Field label="Output device" childrenContainerWidth="max">
          {st.node_present ? "Clean Mic (noise-cancelled)" : "not present"}{st.is_default ? " · default" : ""}
        </Field></PanelSectionRow>
        <PanelSectionRow><Field label="CPU / uptime" childrenContainerWidth="max">
          {st.cpu != null ? `${st.cpu}% of one core` : "—"} · {fmtUptime(st.uptime)}
          {st.restarts ? ` · ${st.restarts} restart${st.restarts > 1 ? "s" : ""}` : ""}
        </Field></PanelSectionRow>
        <PanelSectionRow><Field label="Version" childrenContainerWidth="max">{st.version}</Field></PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => ({
  name: "Clean Mic",
  titleView: <div className={staticClasses.Title}>Clean Mic</div>,
  content: <Content />,
  icon: <CleanMicIcon />,
  onDismount() {},
}));
