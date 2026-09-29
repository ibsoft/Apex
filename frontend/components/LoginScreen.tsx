/* Sign-in and lock screens.

   Two rules shape this screen:

   * **The microphone is off here.** Voice is a continuous, always-listening
     capability; letting it run before a password has been entered, or while the
     screen is locked, would mean an unauthenticated - or a locked - machine
     that still accepts spoken commands. The caller only mounts this component
     when voice is disabled, and `lockEnabled` is passed down so the state is
     obvious at the call site.
   * **Nothing is pre-filled from the URL.** The next destination is read from
     the router, not from a query parameter, so a phishing link cannot bounce a
     user who just authenticated into another origin.

   The password is sent to the backend, which hands it to libpam. It is never
   stored, never logged, and never rendered back.
*/
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, auth, type SystemUserLite } from "../lib/api";

/* Same palette the rest of the shell uses, so the sign-in screen reads as part
   of APEX rather than a separate page. */
const C = {
  cyan: "#00e5ff",
  gold: "#f5a623",
  line: "rgba(0,229,255,0.16)",
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  padding: "12px 14px",
  borderRadius: 11,
  background: "rgba(2,6,14,0.75)",
  border: `1px solid ${C.line}`,
  color: "#eaf4ff",
  fontSize: 14,
  fontFamily: "var(--font-mono)",
  outline: "none",
};

function Shell({ children, dim }: { children: React.ReactNode; dim?: boolean }) {
  return (
    <div style={{
      position: "absolute", inset: 0, zIndex: 70,
      display: "flex", alignItems: "center", justifyContent: "center",
      background: "radial-gradient(ellipse 90% 80% at 50% 45%, rgba(4,8,15,0.55) 0%, rgba(4,8,15,0.9) 100%)",
      backdropFilter: "blur(6px)",
      pointerEvents: "auto",
      overflow: "auto",
    }}>
      <div style={{
        width: "min(430px, 92vw)", textAlign: "center", margin: "auto",
        padding: "34px 30px 30px", borderRadius: 18,
        background: "rgba(6,10,20,0.92)", border: `1px solid ${C.line}`,
        boxShadow: "0 0 60px rgba(0,229,255,0.12), 0 18px 50px rgba(0,0,0,0.6)",
        opacity: dim ? 0.97 : 1,
      }}>
        <div style={{ width: 64, height: 64, margin: "0 auto 18px", borderRadius: "50%", display: "flex", alignItems: "center", justifyContent: "center",
          background: "radial-gradient(circle, rgba(0,229,255,0.25) 0%, rgba(0,229,255,0.05) 60%)",
          border: `1px solid ${C.line}` }}>
          <div style={{ width: 26, height: 26, borderRadius: "50%", background: C.cyan, boxShadow: "0 0 22px rgba(0,229,255,0.9)" }} />
        </div>
        {children}
      </div>
    </div>
  );
}

function Avatar({ initials, size = 34 }: { initials: string; size?: number }) {
  return (
    <div style={{
      width: size, height: size, borderRadius: "50%", flex: "0 0 auto",
      display: "flex", alignItems: "center", justifyContent: "center",
      fontSize: size * 0.38, fontWeight: 700, letterSpacing: "0.04em",
      fontFamily: "var(--font-mono)",
      color: "#04121a",
      background: "linear-gradient(135deg, #7df3ff 0%, #00e5ff 55%, #00b8d4 100%)",
      boxShadow: "0 0 18px rgba(0,229,255,0.35)",
    }}>
      {initials}
    </div>
  );
}

function Message({ tone, children }: { tone: "error" | "info"; children: React.ReactNode }) {
  const color = tone === "error" ? "#ff8f8f" : "rgba(170,192,215,0.72)";
  return (
    <div role={tone === "error" ? "alert" : "status"} style={{
      marginTop: 14, fontSize: 11, lineHeight: 1.6, fontFamily: "var(--font-mono)",
      color, letterSpacing: "0.04em", wordBreak: "break-word",
    }}>
      {children}
    </div>
  );
}

/** A seconds-remaining notice that does not need a timer to stay honest. */
function RetryNotice({ seconds }: { seconds: number }) {
  const [left, setLeft] = useState(seconds);
  useEffect(() => {
    setLeft(seconds);
    if (seconds <= 0) return;
    const t = setInterval(() => setLeft((v) => Math.max(0, v - 1)), 1000);
    return () => clearInterval(t);
  }, [seconds]);
  if (left <= 0) return null;
  return <Message tone="info">Locked out for another {left}s.</Message>;
}

export function LoginScreen({ onSignedIn, oauthAvailable }: {
  onSignedIn: () => void;
  oauthAvailable?: boolean;
}) {
  const [users, setUsers] = useState<SystemUserLite[]>([]);
  const [picked, setPicked] = useState<string>("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [retryAfter, setRetryAfter] = useState(0);
  const pwRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let alive = true;
    auth.systemUsers()
      .then((data) => {
        if (!alive) return;
        setUsers(data.users ?? []);
      })
      .catch(() => { /* the plain field still works without the list */ });
    return () => { alive = false; };
  }, []);

  const effective = picked || username.trim();
  const canSubmit = effective.length > 0 && password.length > 0 && !busy;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError("");
    setRetryAfter(0);
    try {
      await auth.login(effective, password);
      // Clear the secret from component state the moment it is not needed.
      setPassword("");
      onSignedIn();
    } catch (err) {
      setPassword("");
      if (err instanceof ApiError) {
        setError(err.message || "Incorrect username or password.");
        if (typeof (err as any).retryAfter === "number") setRetryAfter((err as any).retryAfter);
      } else {
        setError("Could not reach the APEX server.");
      }
    } finally {
      setBusy(false);
      pwRef.current?.focus();
    }
  }

  const fieldLabel = useMemo(
    () => (users.length ? "USER" : "USERNAME"),
    [users.length],
  );

  return (
    <Shell>
      <div style={{ fontSize: 26, fontWeight: 700, letterSpacing: "0.24em", color: "#f0f6ff", fontFamily: "var(--font-mono)" }}>APEX</div>
      <div style={{ fontSize: 11, color: "rgba(170,192,215,0.7)", marginTop: 10, lineHeight: 1.8, fontFamily: "var(--font-mono)" }}>
        SIGN IN TO CONTINUE<br />VOICE · TOOLS · SKILLS · MEMORY
      </div>

      {users.length > 0 && (
        <div style={{ marginTop: 22, display: "flex", flexDirection: "column", gap: 6 }}>
          {users.map((u) => {
            const active = (picked || username) === u.username;
            return (
              <button
                key={u.username}
                type="button"
                onClick={() => { setPicked(u.username); setUsername(u.username); setError(""); pwRef.current?.focus(); }}
                style={{
                  display: "flex", alignItems: "center", gap: 12, textAlign: "left",
                  padding: "9px 12px", borderRadius: 11, cursor: "pointer",
                  background: active ? "rgba(0,229,255,0.10)" : "rgba(2,6,14,0.55)",
                  border: `1px solid ${active ? "rgba(0,229,255,0.55)" : C.line}`,
                  color: "#eaf4ff",
                }}
              >
                <Avatar initials={u.initials} />
                <span style={{ minWidth: 0 }}>
                  <span style={{ display: "block", fontSize: 13, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {u.name || u.username}
                  </span>
                  <span style={{ display: "block", fontSize: 10.5, color: "rgba(170,192,215,0.6)", fontFamily: "var(--font-mono)" }}>
                    @{u.username}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      )}

      <form onSubmit={submit} style={{ marginTop: 20, display: "flex", flexDirection: "column", gap: 10, textAlign: "left" }}>
        <label htmlFor="apex-username" style={{ fontSize: 9.5, letterSpacing: "0.16em", color: "rgba(170,192,215,0.6)", fontFamily: "var(--font-mono)" }}>
          {fieldLabel}
        </label>
        <input
          id="apex-username"
          name="username"
          autoComplete="username"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          style={inputStyle}
          value={username}
          onChange={(e) => { setUsername(e.target.value); if (picked) setPicked(""); setError(""); }}
          placeholder="username"
        />
        <label htmlFor="apex-password" style={{ fontSize: 9.5, letterSpacing: "0.16em", color: "rgba(170,192,215,0.6)", fontFamily: "var(--font-mono)", marginTop: 4 }}>
          PASSWORD
        </label>
        <input
          id="apex-password"
          ref={pwRef}
          name="password"
          type="password"
          autoComplete="current-password"
          style={inputStyle}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="••••••••"
        />
        <button
          type="submit"
          disabled={!canSubmit}
          style={{
            marginTop: 8, padding: "13px 18px", borderRadius: 12, cursor: canSubmit ? "pointer" : "not-allowed",
            letterSpacing: "0.14em", fontFamily: "var(--font-mono)", fontSize: 12, fontWeight: 600,
            color: "#04121a", border: "none",
            background: canSubmit
              ? "linear-gradient(135deg, #7df3ff 0%, #00e5ff 55%, #00b8d4 100%)"
              : "rgba(0,229,255,0.18)",
            boxShadow: canSubmit ? "0 0 24px rgba(0,229,255,0.45)" : "none",
            opacity: busy ? 0.7 : 1,
          }}
        >
          {busy ? "CHECKING…" : "SIGN IN"}
        </button>
      </form>

      {error && <Message tone="error">{error}</Message>}
      {retryAfter > 0 && <RetryNotice seconds={retryAfter} />}

      {oauthAvailable && (
        <a
          href="/api/oauth/start"
          style={{
            marginTop: 16, display: "inline-block", fontSize: 10.5, letterSpacing: "0.1em",
            color: "rgba(170,192,215,0.72)", fontFamily: "var(--font-mono)", textDecoration: "none",
            borderBottom: "1px dotted rgba(170,192,215,0.4)",
          }}
        >
          OR CONTINUE WITH OPENAI
        </a>
      )}

      <div style={{ marginTop: 16, fontSize: 9, color: "rgba(170,192,215,0.42)", fontFamily: "var(--font-mono)", letterSpacing: "0.06em", lineHeight: 1.7 }}>
        MICROPHONE IS OFF UNTIL YOU SIGN IN
      </div>
    </Shell>
  );
}

export function LockScreen({ userName, onUnlock }: { userName: string; onUnlock: (password: string) => Promise<boolean> }) {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [retryAfter, setRetryAfter] = useState(0);
  const pwRef = useRef<HTMLInputElement>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!password || busy) return;
    setBusy(true);
    setError("");
    setRetryAfter(0);
    try {
      /* The provider owns this call, not the screen. Unlocking has to clear
         `locked` and restore the microphone the operator had chosen, and only
         ApexProvider knows what that preference was. The screen used to call
         auth.unlock() itself and then just refresh the user, which left `locked`
         true: the server was unlocked and every request worked, but the lock
         screen never went away. */
      await onUnlock(password);
      setPassword("");
    } catch (err) {
      setPassword("");
      if (err instanceof ApiError) {
        setError(err.message || "Incorrect password.");
        if (typeof (err as any).retryAfter === "number") setRetryAfter((err as any).retryAfter);
      } else {
        setError("Could not reach the APEX server.");
      }
    } finally {
      setBusy(false);
      pwRef.current?.focus();
    }
  }

  return (
    <Shell dim>
      <div style={{ fontSize: 22, fontWeight: 700, letterSpacing: "0.2em", color: "#f0f6ff", fontFamily: "var(--font-mono)" }}>
        LOCKED
      </div>
      <div style={{ marginTop: 16, display: "flex", alignItems: "center", justifyContent: "center", gap: 12 }}>
        <Avatar initials={(userName || "A").slice(0, 2).toUpperCase()} size={40} />
        <span style={{ fontSize: 13, color: "#eaf4ff", textAlign: "left" }}>{userName || "Signed in"}</span>
      </div>

      <form onSubmit={submit} style={{ marginTop: 22, display: "flex", flexDirection: "column", gap: 10, textAlign: "left" }}>
        <label htmlFor="apex-unlock" style={{ fontSize: 9.5, letterSpacing: "0.16em", color: "rgba(170,192,215,0.6)", fontFamily: "var(--font-mono)" }}>
          PASSWORD TO UNLOCK
        </label>
        <input
          id="apex-unlock"
          ref={pwRef}
          type="password"
          autoComplete="current-password"
          autoFocus
          style={inputStyle}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="••••••••"
        />
        <button
          type="submit"
          disabled={!password || busy}
          style={{
            marginTop: 8, padding: "13px 18px", borderRadius: 12, cursor: password && !busy ? "pointer" : "not-allowed",
            letterSpacing: "0.14em", fontFamily: "var(--font-mono)", fontSize: 12, fontWeight: 600,
            color: "#04121a", border: "none",
            background: password && !busy
              ? "linear-gradient(135deg, #7df3ff 0%, #00e5ff 55%, #00b8d4 100%)"
              : "rgba(0,229,255,0.18)",
            boxShadow: password && !busy ? "0 0 24px rgba(0,229,255,0.45)" : "none",
            opacity: busy ? 0.7 : 1,
          }}
        >
          {busy ? "CHECKING…" : "UNLOCK"}
        </button>
      </form>

      {error && <Message tone="error">{error}</Message>}
      {retryAfter > 0 && <RetryNotice seconds={retryAfter} />}

      <div style={{ marginTop: 16, fontSize: 9, color: "rgba(170,192,215,0.42)", fontFamily: "var(--font-mono)", letterSpacing: "0.06em", lineHeight: 1.7 }}>
        MICROPHONE IS OFF WHILE LOCKED<br />TYPE YOUR PASSWORD TO CONTINUE
      </div>
    </Shell>
  );
}
