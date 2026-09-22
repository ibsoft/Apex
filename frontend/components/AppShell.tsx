"use client";

/* AppShell - client assembler for the assistant experience:
   controlled APEX world (orb state driven by chat/voice), login overlay,
   chat/settings/memory panel and error toasts. */

import { useEffect, useRef } from "react";
import ApexWorld from "./ApexWorld";
import ChatUI from "./ChatUI";
import { useApex } from "./ApexProvider";

const C = {
  cyan: "#00e5ff",
  gold: "#f5a623",
  line: "rgba(0,229,255,0.16)",
};

function LoginOverlay() {
  const a = useApex();
  const oauthReady = a.config?.oauth_configured ?? true;
  return (
    <div style={{
      position: "absolute", inset: 0, zIndex: 70,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "radial-gradient(ellipse 90% 80% at 50% 45%, rgba(4,8,15,0.55) 0%, rgba(4,8,15,0.88) 100%)",
      backdropFilter: "blur(4px)",
      pointerEvents: "auto",
    }}>
      <div style={{
        width: "min(420px, 92vw)", textAlign: "center",
        padding: "34px 30px 30px", borderRadius: 18,
        background: "rgba(6,10,20,0.9)", border: `1px solid ${C.line}`,
        boxShadow: "0 0 60px rgba(0,229,255,0.12), 0 18px 50px rgba(0,0,0,0.6)",
      }}>
        <div style={{ width: 64, height: 64, margin: "0 auto 18px", borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center",
          background: "radial-gradient(circle, rgba(0,229,255,0.25) 0%, rgba(0,229,255,0.05) 60%)",
          border: `1px solid ${C.line}` }}>
          <div style={{ width: 26, height: 26, borderRadius: "50%", background: C.cyan, boxShadow: "0 0 22px rgba(0,229,255,0.9)" }} />
        </div>
        <div style={{ fontSize: 26, fontWeight: 700, letterSpacing: "0.24em", color: "#f0f6ff", fontFamily: "var(--font-mono)" }}>APEX</div>
        <div style={{ fontSize: 11, color: "rgba(170,192,215,0.7)", marginTop: 10, lineHeight: 1.8, fontFamily: "var(--font-mono)" }}>
          YOUR AI CO-WORKER<br />VOICE · TOOLS · SKILLS · MEMORY
        </div>
        <div style={{ marginTop: 26, display: "flex", flexDirection: "column", gap: 10 }}>
          {oauthReady ? (
            <button onClick={() => a.login()}
              style={{
                padding: "13px 18px", borderRadius: 12, cursor: "pointer",
                letterSpacing: "0.14em", fontFamily: "var(--font-mono)", fontSize: 12, fontWeight: 600,
                color: "#04121a", background: "linear-gradient(135deg, #7df3ff 0%, #00e5ff 55%, #00b8d4 100%)",
                border: "none", boxShadow: "0 0 24px rgba(0,229,255,0.45)",
              }}>
              SIGN IN WITH OPENAI
            </button>
          ) : (
            <div style={{ fontSize: 10.5, color: C.gold, fontFamily: "var(--font-mono)", letterSpacing: "0.06em", lineHeight: 1.7 }}>
              BACKEND OAUTH NOT CONFIGURED —<br />SET OPENAI_CLIENT_ID / SECRET ON THE SERVER
            </div>
          )}
          <div style={{ fontSize: 9.5, color: "rgba(170,192,215,0.5)", fontFamily: "var(--font-mono)", letterSpacing: "0.08em", marginTop: 4 }}>
            ALSO WORKS WITH LOCALLY-RUN MODELS — OLLAMA / GPU
          </div>
        </div>
      </div>
    </div>
  );
}

function ErrorToast() {
  const a = useApex();
  const err = a.error;
  useEffect(() => {
    if (!err) return;
    const t = setTimeout(a.clearError, 7000);
    return () => clearTimeout(t);
  }, [err, a]);
  if (!err) return null;
  return (
    <div style={{
      position: "fixed", left: 14, bottom: 14, zIndex: 80, maxWidth: "min(360px, 86vw)",
      padding: "10px 14px", borderRadius: 10,
      background: "rgba(40,10,8,0.92)", border: "1px solid rgba(255,110,90,0.4)",
      color: "#ffb4a0", fontSize: 11, fontFamily: "var(--font-mono)", letterSpacing: "0.03em",
      boxShadow: "0 8px 30px rgba(0,0,0,0.5)",
    }}>
      <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
        <span>{err}</span>
        <button onClick={a.clearError} aria-label="Dismiss" style={{ background: "none", border: "none", color: "#ffb4a0", cursor: "pointer", fontSize: 13, lineHeight: 1 }}>×</button>
      </div>
    </div>
  );
}

function LoadingSplash() {
  return (
    <div style={{
      position: "absolute", inset: 0, zIndex: 69,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "rgba(4,8,15,0.94)",
    }}>
      <span className="apex-blink" style={{ color: C.cyan, fontSize: 13, letterSpacing: "0.3em", fontFamily: "var(--font-mono)" }}>INITIALIZING…</span>
    </div>
  );
}

export default function AppShell() {
  const a = useApex();

  const lastTapRef = useRef<number>(0);
  const handleTap = () => {
    if (!a.user) {
      a.login();
      return;
    }
    const now = Date.now();
    const doubleTap = now - lastTapRef.current < 350;
    lastTapRef.current = now;
    if (doubleTap && a.voiceEnabled) {
      a.forceVoiceAwake();
      return;
    }
    a.setVoiceEnabled(!a.voiceEnabled);
  };

  return (
    <div style={{ position: "relative", height: "100vh", minHeight: 620 }}>
      <ApexWorld state={a.orb} onTap={handleTap} />

      {a.loading ? <LoadingSplash /> : !a.user ? <LoginOverlay /> : <ChatUI />}

      <ErrorToast />
    </div>
  );
}