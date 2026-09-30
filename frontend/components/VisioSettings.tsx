"use client";

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useApex } from "./ApexProvider";
import SettingsCard from "./SettingsCard";

const control = { background: "#0b1728", color: "#dbeafa", border: "1px solid #284359", borderRadius: 8, padding: "7px 10px", width: "100%" };

export default function VisioSettings() {
  const a = useApex();
  const enabled = !!(a.settings.visio_enabled ?? a.config?.visio_enabled);
  const provider = String(a.settings.visio_provider ?? a.config?.visio_provider ?? "ollama");
  const savedModel = String(a.settings.visio_model ?? a.config?.visio_model ?? "");
  const camera = String(a.settings.visio_camera ?? a.config?.visio_camera ?? "");
  const [model, setModel] = useState(savedModel);
  const [models, setModels] = useState<string[]>([]);
  const [cameras, setCameras] = useState<{ device: string; name: string; accessible: boolean }[]>([]);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => setModel(savedModel), [savedModel]);
  useEffect(() => {
    let disposed = false;
    setModels([]);
    api.models(provider).then(r => { if (!disposed) setModels(r.models); }).catch(() => {});
    return () => { disposed = true; };
  }, [provider]);
  const detect = async () => {
    setBusy(true);
    try {
      const result = await api.visio.cameras();
      setCameras(result.cameras);
      setStatus(!result.ffmpeg_available ? "FFmpeg is missing on the APEX host." : result.cameras.length ? `${result.cameras.length} video device(s) detected. Capture will verify availability.` : "No camera detected on the APEX host.");
    } catch (e) { setStatus(e instanceof Error ? e.message : "Camera detection failed. Retry below."); }
    finally { setBusy(false); }
  };
  return <SettingsCard title="VISIO · Camera vision">
    <label><input type="checkbox" checked={enabled} onChange={e => void a.updateSettings({ visio_enabled: e.target.checked })} /> Enable snapshots on request</label>
    <label>Vision provider
      <select aria-label="Vision provider" style={control} value={provider} onChange={e => void a.updateSettings({ visio_provider: e.target.value, visio_model: "" })}>
        <option value="ollama">Ollama</option><option value="openai">OpenAI (API key)</option>
      </select>
    </label>
    <label>Vision model
      <input aria-label="Vision model" style={control} list="visio-models" value={model} placeholder="Enter an image-capable model ID" onChange={e => setModel(e.target.value)} />
      <datalist id="visio-models">{models.map(m => <option key={m} value={m} />)}</datalist>
    </label>
    <button type="button" style={control} onClick={() => void a.updateSettings({ visio_model: model.trim() })}>Save vision model</button>
    <span style={{ fontSize: 11 }}>Choose a model that accepts images; the provider list also includes text-only models.</span>
    <label>Host camera
      <select aria-label="Host camera" style={control} value={camera} onChange={e => void a.updateSettings({ visio_camera: e.target.value })}>
        <option value="">Automatic (first accessible device)</option>
        {camera && !cameras.some(c => c.device === camera) && <option value={camera}>{camera} (not checked)</option>}
        {cameras.map(c => <option key={c.device} value={c.device}>{c.name} — {c.device}{c.accessible ? "" : " (no access)"}</option>)}
      </select>
    </label>
    <button type="button" style={control} disabled={busy} onClick={() => void detect()}>{busy ? "Detecting…" : "Detect cameras"}</button>
    {status && <span role="status">{status}</span>}
    <span style={{ fontSize: 11, lineHeight: 1.5 }}>Uses a camera attached to the APEX server. Ask “What do you see now?” to capture one frame and send it to the selected provider. APEX does not save snapshots. Face identification and face memory are not supported.</span>
  </SettingsCard>;
}
