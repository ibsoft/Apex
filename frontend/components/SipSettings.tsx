"use client";

import { useEffect, useState } from "react";
import { api, type SipStatus } from "../lib/api";
import { useApex } from "./ApexProvider";
import SettingsCard from "./SettingsCard";

const control: React.CSSProperties = { background: "#0b1728", color: "#dbeafa", border: "1px solid #284359", borderRadius: 8, padding: "7px 10px", width: "100%" };
const label: React.CSSProperties = { display: "grid", gap: 5 };

/**
 * SIP · outbound phone calls.
 *
 * Every field here is saved to the settings table *and* mirrored into the
 * project's env file, so a backend started from a shell sees the same account as
 * the systemd one.
 *
 * The password field is write-only and deliberately blank every time. The
 * server never sends the stored value back (`password_set` only), so rendering
 * it is impossible; the field shows whether one is stored and the note explains
 * that saving an empty box keeps the stored one. Clearing it is a separate,
 * explicit button, because a stray empty save must never destroy a working
 * account.
 */
export default function SipSettings() {
  const a = useApex();
  const enabled = !!(a.settings.sip_enabled ?? a.config?.sip_enabled);
  const saved = {
    server: String(a.settings.sip_server ?? a.config?.sip_server ?? ""),
    user: String(a.settings.sip_user ?? a.config?.sip_user ?? ""),
    transport: String(a.settings.sip_transport ?? a.config?.sip_transport ?? "udp"),
    port: String(a.settings.sip_port ?? a.config?.sip_port ?? ""),
    displayName: String(a.settings.sip_display_name ?? a.config?.sip_display_name ?? "APEX"),
    domain: String(a.settings.sip_domain ?? a.config?.sip_domain ?? ""),
    proxy: String(a.settings.sip_outbound_proxy ?? a.config?.sip_outbound_proxy ?? ""),
    ttsEngine: String(a.settings.sip_tts_engine ?? a.config?.sip_tts_engine ?? "espeak"),
    ttsVoice: String(a.settings.sip_tts_voice ?? a.config?.sip_tts_voice ?? ""),
  };

  const [form, setForm] = useState(saved);
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<SipStatus | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  // Adopt server state whenever it changes, so a save (or another operator's
  // save) is reflected instead of leaving a stale local edit on screen.
  useEffect(() => setForm(saved), [
    saved.server, saved.user, saved.transport, saved.port,
    saved.displayName, saved.domain, saved.proxy,
    saved.ttsEngine, saved.ttsVoice,
  ]);
  useEffect(() => setPassword(""), [saved.user, saved.server]);

  const refresh = async () => {
    try {
      setStatus(await api.sip.status());
    } catch (e) {
      setNote(e instanceof Error ? e.message : "Could not read the SIP status.");
    }
  };
  useEffect(() => {
    void refresh();
  }, [enabled, saved.user, saved.server, saved.transport]);

  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm(f => ({ ...f, [key]: e.target.value }));

  const save = async (patch: Record<string, unknown>) => {
    setNote("");
    await a.updateSettings(patch as any);
    // The password is write-only, so the box is emptied after every save: it
    // must never be left on screen looking like a saved credential.
    setPassword("");
    await refresh();
  };

  const saveAll = () => save({
    sip_enabled: enabled,
    sip_server: form.server.trim(),
    sip_user: form.user.trim(),
    sip_transport: form.transport,
    sip_port: form.port.trim(),
    sip_display_name: form.displayName.trim() || "APEX",
    sip_domain: form.domain.trim(),
    sip_outbound_proxy: form.proxy.trim(),
    sip_tts_engine: form.ttsEngine,
    sip_tts_voice: form.ttsVoice.trim(),
    // Omitted when blank: the server ignores an empty password, which is what
    // keeps an unrelated save from wiping a stored one.
    ...(password.trim() ? { sip_password: password.trim() } : {}),
  });

  const clearPassword = async () => {
    setBusy(true);
    setNote("");
    try {
      await api.sip.clearPassword();
      setPassword("");
      await a.refresh();
      await refresh();
      setNote("Password cleared. Save the account again to store a new one.");
    } catch (e) {
      setNote(e instanceof Error ? e.message : "Could not clear the password.");
    } finally {
      setBusy(false);
    }
  };

  const passwordSet = !!(status?.password_set ?? a.settings.sip_password_set ?? a.config?.sip_password_set);
  const problems: string[] = [];
  if (status) {
    for (const m of status.missing_on_host) problems.push(`Not installed: ${m}`);
    if (!status.ffmpeg_available) problems.push("Not installed: ffmpeg (needed to convert call audio)");
    if (status.tts_engine === "none") problems.push("No SIP speech engine is available. Install Edge TTS or espeak-ng.");
    if (!status.audio_loopback_ok) problems.push(status.audio_loopback_note);
  }

  return (
    <SettingsCard title="SIP · phone calls">
      <label style={label}>
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => void save({ sip_enabled: e.target.checked })}
        />
        Allow APEX to place outbound phone calls
      </label>

      <label style={label}>SIP server
        <input style={control} autoComplete="off" spellCheck={false} placeholder="pbx.example.org"
          value={form.server} onChange={set("server")} />
      </label>

      <label style={label}>SIP user
        <input style={control} autoComplete="off" spellCheck={false} placeholder="1001"
          value={form.user} onChange={set("user")} />
      </label>

      <label style={label}>SIP password
        <input style={control} type="password" autoComplete="new-password"
          placeholder={passwordSet ? "Stored — type to replace" : "Not set"}
          value={password} onChange={(e) => setPassword(e.target.value)} />
      </label>

      <label style={label}>Transport
        <select style={control} value={form.transport} onChange={set("transport")}>
          {(status?.allowed_transports ?? ["udp", "tcp", "tls"]).map(t => <option key={t} value={t}>{t}</option>)}
        </select>
      </label>

      <label style={label}>Port
        <input style={control} inputMode="numeric" placeholder="server default"
          value={form.port} onChange={set("port")} />
      </label>

      <label style={label}>Display name
        <input style={control} placeholder="APEX"
          value={form.displayName} onChange={set("displayName")} />
      </label>

      <label style={label}>Domain
        <input style={control} placeholder="defaults to the SIP server"
          value={form.domain} onChange={set("domain")} />
      </label>

      <label style={label}>Outbound proxy
        <input style={control} placeholder="none"
          value={form.proxy} onChange={set("proxy")} />
      </label>

      <label style={label}>Call voice engine
        <select style={control} value={form.ttsEngine} onChange={set("ttsEngine")}>
          <option value="espeak">eSpeak, local</option>
          <option value="edge">Edge TTS, online</option>
        </select>
      </label>

      {form.ttsEngine === "edge" && (
        <>
          <label style={label}>Edge voice
            <input style={control} autoComplete="off" spellCheck={false}
              placeholder="Default Edge voice"
              value={form.ttsVoice} onChange={set("ttsVoice")} />
          </label>
          <span style={{ fontSize: 11, lineHeight: 1.5 }}>
            Requires internet access. Utterance text is sent to Microsoft Edge TTS for synthesis.
          </span>
        </>
      )}

      <button type="button" style={control} onClick={() => void saveAll()}>Save SIP account</button>
      <button type="button" style={control} disabled={busy || !passwordSet} onClick={() => void clearPassword()}>
        {busy ? "Clearing…" : "Forget stored password"}
      </button>

      {note && <span role="status" style={{ fontSize: 11 }}>{note}</span>}

      {problems.length > 0 && (
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 11, lineHeight: 1.5, color: "#ffb4a2" }}>
          {problems.map(p => <li key={p}>{p}</li>)}
        </ul>
      )}

      {status && !status.whisper_ready && (
        <span style={{ fontSize: 11, lineHeight: 1.5 }}>
          Speech recognition is not installed, so APEX would not be able to hear the
          reply. Set it up once with
          {" "}<code>python3 -m venv .venv/sip-whisper</code>{" "}and
          {" "}<code>pip install -r backend/tools/sip-whisper-requirements.txt</code>.
        </span>
      )}

      {status && (
        <span style={{ fontSize: 11, lineHeight: 1.5 }}>
          Say “call me” and APEX will print a plan before dialling anything; it only
          dials after you approve. Speak with {status.tts_engine === "none" ? "no engine" : status.tts_engine},
          up to {status.max_duration_seconds}s per call, one call at a time
          {status.max_concurrent_calls === 1 ? "" : ` (max ${status.max_concurrent_calls})`}.
          Saved values are mirrored to <code>{status.env_file}</code>.
        </span>
      )}
    </SettingsCard>
  );
}