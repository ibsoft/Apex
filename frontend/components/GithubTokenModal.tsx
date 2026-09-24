"use client";

/* GithubTokenModal - centered popup for the CODE skill.
   Shown when a code_push needs a GitHub Personal Access Token and none is
   stored. The token is validated against the GitHub API and stored per-user
   (chmod 600, gitignored) so future pushes do not need it again. */

import { useEffect, useRef, useState } from "react";
import { useApex } from "./ApexProvider";

const C = {
  green: "#5ef2ab",
  cyan: "#00e5ff",
  red: "#ff6a56",
  line: "rgba(0,229,255,0.16)",
};

export default function GithubTokenModal() {
  const a = useApex();
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [login, setLogin] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (a.githubPrompt) {
      setToken("");
      setError("");
      setLogin("");
      setBusy(false);
      const t = setTimeout(() => inputRef.current?.focus(), 60);
      return () => clearTimeout(t);
    }
  }, [a.githubPrompt]);

  const reason = a.githubPrompt?.reason?.trim();

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!token || busy) return;
    setBusy(true);
    setError("");
    try {
      await a.setGithubToken(token.trim());
      setLogin("connected");
    } catch (err: any) {
      setError(err?.message ?? "Could not save the GitHub token (was it accepted by GitHub?).");
      setBusy(false);
      inputRef.current?.focus();
    }
  };

  if (!a.githubPrompt) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="GitHub token required"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) a.closeGithubPrompt();
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
          width: "min(480px, 92vw)", padding: "28px 26px 24px", borderRadius: 18,
          background: "rgba(6,10,20,0.96)", border: `1px solid ${C.line}`,
          boxShadow: "0 0 60px rgba(0,229,255,0.14), 0 18px 50px rgba(0,0,0,0.65)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 6 }}>
          <span style={{
            width: 12, height: 12, borderRadius: "50%", flex: "0 0 auto",
            background: C.green, boxShadow: `0 0 18px ${C.green}`,
          }} />
          <div style={{ fontSize: 15, fontWeight: 700, letterSpacing: "0.1em", color: "#f0f6ff", fontFamily: "var(--font-mono)" }}>
            GITHUB TOKEN REQUIRED
          </div>
        </div>
        <div style={{ fontSize: 11, color: "rgba(170,192,215,0.75)", lineHeight: 1.7, marginBottom: 16, fontFamily: "var(--font-mono)" }}>
          APEX needs a GitHub Personal Access Token to push:{' '}
          <span style={{ color: C.cyan }}>{reason ? `"${reason}"` : "to publish a repository"}</span>
          <br />
          Create one at{" "}
          <a href="https://github.com/settings/tokens" target="_blank" rel="noreferrer" style={{ color: C.cyan }}>
            github.com/settings/tokens
          </a>{" "}
          (scope: <b>repo</b>), paste it below. It is validated against the GitHub API and
          stored per-user on this server for future pushes — never in git config or chat.
        </div>

        <label style={{ display: "block", fontSize: 11, color: "rgba(170,192,215,0.8)", marginBottom: 6, fontFamily: "var(--font-mono)" }}>
          PERSONAL ACCESS TOKEN
        </label>
        <input
          ref={inputRef}
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="ghp_••••••••••••••••••••••"
          autoComplete="off"
          spellCheck={false}
          style={{
            width: "100%", padding: "12px 14px", borderRadius: 10, marginBottom: 12,
            background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.14)",
            color: "#f0f6ff", fontSize: 14, outline: "none", fontFamily: "var(--font-mono)",
          }}
        />

        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <button
            type="submit"
            disabled={busy || !token}
            style={{
              flex: 1, padding: "12px 0", borderRadius: 10, border: "none", cursor: busy || !token ? "default" : "pointer",
              color: "#04121a", fontWeight: 700, letterSpacing: "0.12em", fontFamily: "var(--font-mono)", fontSize: 12,
              background: busy ? "rgba(0,229,255,0.35)" : "linear-gradient(135deg, #7df3ff 0%, #00e5ff 55%, #00b8d4 100%)",
              boxShadow: "0 0 22px rgba(0,229,255,0.35)",
              opacity: !token ? 0.55 : 1,
            }}
          >
            {busy ? "VALIDATING…" : login ? "SAVED ✓" : "SAVE & CONNECT"}
          </button>
          <button
            type="button"
            onClick={() => a.closeGithubPrompt()}
            style={{
              padding: "12px 18px", borderRadius: 10, cursor: "pointer",
              background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.14)",
              color: "rgba(170,192,215,0.85)", fontFamily: "var(--font-mono)", fontSize: 11,
            }}
          >
            CANCEL
          </button>
        </div>

        {login && !busy && (
          <div style={{ marginTop: 12, fontSize: 11, color: C.green, fontFamily: "var(--font-mono)" }}>
            Connected to GitHub. Now say <b>&quot;continue&quot;</b> so the push can run.
          </div>
        )}
        {error && (
          <div style={{ marginTop: 12, fontSize: 11, color: C.red, fontFamily: "var(--font-mono)" }}>
            {error}
          </div>
        )}
      </form>
    </div>
  );
}