"use client";

/* SudoPromptModal - centered security popup for the VAPT skill.
   Shown whenever a VAPT tool needs root and no session credential is stored.
   "Save for this session" keeps the credential in server memory until logout
   or backend restart; otherwise it is single-use for ~2 minutes. */

import { useEffect, useRef, useState } from "react";
import { useApex } from "./ApexProvider";

const C = {
  cyan: "#00e5ff",
  red: "#ff6a56",
  line: "rgba(0,229,255,0.16)",
};

export default function SudoPromptModal() {
  const a = useApex();
  const [password, setPassword] = useState("");
  const [save, setSave] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (a.sudoPrompt) {
      setPassword("");
      setSave(false);
      setError("");
      setBusy(false);
      const t = setTimeout(() => inputRef.current?.focus(), 60);
      return () => clearTimeout(t);
    }
  }, [a.sudoPrompt]);

  const reason = a.sudoPrompt?.reason?.trim();

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password || busy) return;
    setBusy(true);
    setError("");
    try {
      await a.setSudoPassword(password, save);
      a.closeSudoPrompt();
    } catch (err: any) {
      setError(err?.message ?? "Could not save the sudo password.");
      setBusy(false);
      inputRef.current?.focus();
    }
  };

  if (!a.sudoPrompt) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Sudo password required"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) a.closeSudoPrompt();
      }}
      style={{
        position: "fixed", inset: 0, zIndex: 200,
        display: "flex", alignItems: "center", justifyContent: "center",
        background: "radial-gradient(ellipse 90% 80% at 50% 45%, rgba(4,8,15,0.55) 0%, rgba(4,8,15,0.9) 100%)",
        backdropFilter: "blur(4px)",
      }}
    >
      <form
        onSubmit={onSubmit}
        style={{
          width: "min(440px, 92vw)", padding: "28px 26px 24px", borderRadius: 18,
          background: "rgba(6,10,20,0.96)", border: `1px solid ${C.line}`,
          boxShadow: "0 0 60px rgba(0,229,255,0.14), 0 18px 50px rgba(0,0,0,0.65)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 6 }}>
          <span style={{
            width: 12, height: 12, borderRadius: "50%", flex: "0 0 auto",
            background: C.red, boxShadow: `0 0 18px ${C.red}`,
          }} />
          <div style={{ fontSize: 15, fontWeight: 700, letterSpacing: "0.1em", color: "#f0f6ff", fontFamily: "var(--font-mono)" }}>
            SUDO PASSWORD REQUIRED
          </div>
        </div>
        <div style={{ fontSize: 11, color: "rgba(170,192,215,0.75)", lineHeight: 1.7, marginBottom: 16, fontFamily: "var(--font-mono)" }}>
          VAPT needs root to run:&nbsp;
          <span style={{ color: C.cyan }}>{reason ? `"${reason}"` : "a privileged command"}</span>
          <br />
          Sent to the server in memory only — never stored on disk, never logged.
        </div>

        <label style={{ display: "block", fontSize: 11, color: "rgba(170,192,215,0.8)", marginBottom: 6, fontFamily: "var(--font-mono)" }}>
          PASSWORD
        </label>
        <input
          ref={inputRef}
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="••••••••••••"
          autoComplete="off"
          spellCheck={false}
          style={{
            width: "100%", padding: "12px 14px", borderRadius: 10, marginBottom: 12,
            background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.14)",
            color: "#f0f6ff", fontSize: 14, outline: "none", fontFamily: "var(--font-mono)",
          }}
        />

        <label style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 16, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={save}
            onChange={(e) => setSave(e.target.checked)}
            style={{ width: 15, height: 15, accentColor: C.cyan }}
          />
          <span style={{ fontSize: 12, color: "rgba(170,192,215,0.9)", fontFamily: "var(--font-mono)" }}>
            Save for this session
            <span style={{ color: "rgba(170,192,215,0.55)" }}> (until logout / backend restart)</span>
          </span>
        </label>

        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <button
            type="submit"
            disabled={busy || !password}
            style={{
              flex: 1, padding: "12px 0", borderRadius: 10, border: "none", cursor: busy || !password ? "default" : "pointer",
              color: "#04121a", fontWeight: 700, letterSpacing: "0.12em", fontFamily: "var(--font-mono)", fontSize: 12,
              background: busy ? "rgba(0,229,255,0.35)" : "linear-gradient(135deg, #7df3ff 0%, #00e5ff 55%, #00b8d4 100%)",
              boxShadow: "0 0 22px rgba(0,229,255,0.35)",
              opacity: !password ? 0.55 : 1,
            }}
          >
            {busy ? "SAVING…" : "SAVE"}
          </button>
          <button
            type="button"
            onClick={() => a.closeSudoPrompt()}
            style={{
              padding: "12px 18px", borderRadius: 10, cursor: "pointer",
              background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.14)",
              color: "rgba(170,192,215,0.85)", fontFamily: "var(--font-mono)", fontSize: 11,
            }}
          >
            CANCEL
          </button>
        </div>

        {error && (
          <div style={{ marginTop: 12, fontSize: 11, color: C.red, fontFamily: "var(--font-mono)" }}>
            {error}
          </div>
        )}
      </form>
    </div>
  );
}