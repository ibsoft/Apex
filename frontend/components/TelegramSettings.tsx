"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type TelegramStatus } from "../lib/api";
import { useApex } from "./ApexProvider";
import SettingsCard from "./SettingsCard";

const control: React.CSSProperties = {
  background: "#0b1728", color: "#dbeafa", border: "1px solid #284359",
  borderRadius: 8, padding: "7px 10px", width: "100%", fontFamily: "inherit",
};
const label: React.CSSProperties = { display: "grid", gap: 5 };
const brand: React.CSSProperties = {
  width: 12, height: 12, borderRadius: "50%", flex: "0 0 auto",
  background: "#37c1f0", boxShadow: "0 0 18px #37c1f0",
};

function backdrop(close: () => void): React.CSSProperties {
  return {
    position: "fixed", inset: 0, zIndex: 200,
    display: "flex", alignItems: "center", justifyContent: "center",
    background: "radial-gradient(ellipse 90% 80% at 50% 45%, rgba(4,8,15,0.55) 0%, rgba(4,8,15,0.9) 100%)",
    backdropFilter: "blur(4px)",
  };
}

function shell(): React.CSSProperties {
  return {
    width: "min(460px, 92vw)", padding: "24px 24px 20px", borderRadius: 18,
    background: "rgba(6,10,20,0.96)", border: "1px solid rgba(0,229,255,0.2)",
    boxShadow: "0 0 60px rgba(0,229,255,0.1), 0 18px 50px rgba(0,0,0,0.65)",
  };
}

const buttonPrimary: React.CSSProperties = {
  flex: 1, padding: "12px 0", borderRadius: 10, cursor: "pointer", color: "#02212c",
  fontWeight: 700, letterSpacing: "0.12em", fontFamily: "var(--font-mono)", fontSize: 12,
  background: "linear-gradient(135deg, #6ff0ff 0%, #00e5ff 55%, #0091a8 100%)",
  boxShadow: "0 0 22px rgba(0,229,255,0.3)", border: "none",
};
const buttonGhost: React.CSSProperties = {
  padding: "12px 18px", borderRadius: 10, cursor: "pointer",
  background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.14)",
  color: "rgba(170,192,215,0.85)", fontFamily: "var(--font-mono)", fontSize: 11,
};

/**
 * Telegram bot · talk to APEX from Telegram.
 *
 * Three-step join, all in this card: 1) paste the bot token from BotFather
 * (validated live with getMe, shown here as "Configure bot"), 2) generate a
 * pairing code and send it to the bot from your own Telegram account - only that
 * chat is ever accepted - 3) done: text messages in get fast text answers,
 * voice notes are read and answered with a spoken voice note.
 *
 * The token is a secret: it is stored through its own endpoint and only ever
 * surfaces here as a `token_set` flag, never back to the browser.
 */
export default function TelegramSettings() {
  const a = useApex();
  const enabled = !!(a.settings.telegram_enabled ?? a.config?.telegram_enabled);
  const [status, setStatus] = useState<TelegramStatus | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const [tokenModal, setTokenModal] = useState(false);
  const [tokenInput, setTokenInput] = useState("");
  const [tokenError, setTokenError] = useState("");

  const [pairModal, setPairModal] = useState(false);
  const [pairCode, setPairCode] = useState("");
  const [pairTtl, setPairTtl] = useState(600);
  const [pairError, setPairError] = useState("");
  const pairPoll = useRef<ReturnType<typeof setInterval> | null>(null);

  const [form, setForm] = useState({
    ttsEngine: String(a.settings.telegram_tts_engine ?? a.config?.telegram_tts_engine ?? "edge"),
    ttsVoice: String(a.settings.telegram_tts_voice ?? a.config?.telegram_tts_voice ?? ""),
    voiceReplies: !!(a.settings.telegram_voice_replies ?? a.config?.telegram_voice_replies ?? true),
  });

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.telegram.status());
    } catch (e) {
      setNote(e instanceof Error ? e.message : "Could not read the Telegram status.");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, enabled, form.ttsEngine, form.ttsVoice, form.voiceReplies]);

  const save = async (patch: Record<string, any>) => {
    setNote("");
    await a.updateSettings(patch as any);
    await refresh();
  };

  const openPair = async () => {
    setPairModal(true);
    setPairCode("");
    setPairError("");
    setBusy(true);
    try {
      const res = await api.telegram.pair();
      if (res?.code) {
        setPairCode(res.code);
        setPairTtl(res.ttl_seconds ?? 600);
      } else {
        setPairError("Could not generate a pairing code. Is a bot token stored?");
        setPairModal(false);
      }
      await refresh();
    } catch (e) {
      setPairError(e instanceof Error ? e.message : "Generation failed.");
      setPairModal(false);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!pairModal) return;
    pairPoll.current = setInterval(async () => {
      try {
        const s = await api.telegram.status();
        setStatus(s);
        if (s?.paired) {
          if (pairPoll.current) clearInterval(pairPoll.current);
        }
      } catch {
        /* keep polling */
      }
    }, 2000);
    return () => {
      if (pairPoll.current) clearInterval(pairPoll.current);
    };
  }, [pairModal]);

  const unpair = async () => {
    setBusy(true);
    setNote("");
    try {
      await api.telegram.unpair();
      setPairCode("");
      await refresh();
      setNote("Chat unpaired. The bot now ignores every message again.");
    } catch (e) {
      setNote(e instanceof Error ? e.message : "Could not unpair.");
    } finally {
      setBusy(false);
    }
  };

  const clearBot = async () => {
    setBusy(true);
    setNote("");
    try {
      await api.telegram.clear();
      setTokenModal(false);
      setPairCode("");
      await refresh();
      setNote("Bot token forgotten and chat unpaired.");
    } catch (e) {
      setNote(e instanceof Error ? e.message : "Could not clear the bot.");
    } finally {
      setBusy(false);
    }
  };

  const saveToken = async () => {
    setBusy(true);
    setTokenError("");
    try {
      const res = await api.telegram.setToken(tokenInput.trim());
      if (!res?.ok) {
        setTokenError("The backend refused that token.");
        return;
      }
      setTokenInput("");
      setTokenModal(false);
      await refresh();
    } catch (e) {
      setTokenError(e instanceof Error ? e.message : "Telegram rejected the token.");
    } finally {
      setBusy(false);
    }
  };

  const botLabel = status?.bot
    ? `@${status.bot.username ?? status.bot.id ?? "bot"}`
    : status?.token_set
      ? "checked with Telegram"
      : "not set";

  return (
    <SettingsCard title="Telegram bot">
      <label style={label}>
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => void save({ telegram_enabled: e.target.checked })}
        />
        Let APEX talk to you from your Telegram
      </label>
      {enabled && (
        <span style={{ fontSize: 11, lineHeight: 1.5 }}>
          Text messages to the bot run as the paired operator and are answered
          fast with plain text; voice notes are read aloud and answered with a
          spoken voice note.
        </span>
      )}

      <label style={label}>Bot (from BotFather)
        <button type="button" style={{ ...control, cursor: "pointer" }} onClick={() => { setTokenError(""); setTokenInput(""); setTokenModal(true); }}>
          {botLabel} · configure or remove
        </button>
      </label>

      <label style={label}>Paired chat
        <button type="button" style={{ ...control, cursor: "pointer" }} onClick={() => void openPair()} disabled={!!status && !status.token_set}>
          {status?.paired
            ? `@${status.paired_username || "paired chat"}` + (status.pairing_code_active ? " · a code is still valid" : "")
            : "not paired · set up a pairing code"}
        </button>
      </label>
      {status?.paired && (
        <button type="button" style={{ ...control, cursor: "pointer" }} disabled={busy} onClick={() => void unpair()}>
          Unpair this chat
        </button>
      )}

      <label style={label}>Reply to voice notes
        <label style={{ fontSize: 11.5, color: "rgba(235,244,255,0.92)", display: "flex", alignItems: "center", gap: 8 }}>
          <input
            type="checkbox"
            checked={form.voiceReplies}
            onChange={(e) => {
              const v = e.target.checked;
              setForm(f => ({ ...f, voiceReplies: v }));
              void save({ telegram_voice_replies: v });
            }}
          />
          Speak answers to voice notes (ogg/opus) and put the text under them
        </label>
      </label>

      <label style={label}>Reply voice engine
        <select style={control} value={form.ttsEngine}
          onChange={(e) => {
            const v = e.target.value;
            setForm(f => ({ ...f, ttsEngine: v }));
            void save({ telegram_tts_engine: v });
          }}>
          <option value="edge">Edge TTS, online (natural Greek & English)</option>
          <option value="espeak">eSpeak, local (offline)</option>
        </select>
      </label>

      {form.ttsEngine === "edge" && (
        <label style={label}>Edge voice
          <input style={control} autoComplete="off" spellCheck={false}
            placeholder={String(a.settings.response_language ?? a.config?.response_language ?? "en") === "el"
              ? "Automatic Greek neural voice" : "Default Edge voice"}
            value={form.ttsVoice}
            onChange={(e) => {
              const v = e.target.value;
              setForm(f => ({ ...f, ttsVoice: v }));
              void save({ telegram_tts_voice: v });
            }} />
        </label>
      )}

      <button type="button" style={{ ...control, cursor: "pointer", fontWeight: 700 }} disabled={busy} onClick={() => void clearBot()}>
        Forget bot token and unlink
      </button>

      {note && <span role="status" style={{ fontSize: 11 }}>{note}</span>}

      {status && (
        <div style={{ fontSize: 11, lineHeight: 1.6, color: "rgba(170,192,215,0.75)" }}>
          {!status.ffmpeg_available && (
            <div style={{ color: "#ffb4a2" }}>Not installed: ffmpeg (needed to make voice notes).</div>
          )}
          {!status.whisper_ready && (
            <div>
              Speech recognition is not installed, so incoming voice notes cannot be read back.
              {" "}Set up once: <code>python3 -m venv .venv/sip-whisper</code> and{" "}
              <code>pip install -r backend/tools/sip-whisper-requirements.txt</code>.
            </div>
          )}
          {status.running
            ? <div>Poller: listening {status.last_error ? `· last error: ${status.last_error}` : ""}</div>
            : <div style={{ color: "#ffb4a2" }}>Poller: not running{status.last_error ? ` · ${status.last_error}` : ""}</div>}
          <div>Replies: {status.response_language === "el" ? "Greek" : "English"} · {status.tts_engine}
            {status.tts_available ? "" : " (engine unavailable)"} · voice to voice notes {form.voiceReplies ? "on" : "off"}</div>
        </div>
      )}

      {tokenModal && (
        <div role="dialog" aria-modal="true" aria-label="Configure the Telegram bot"
          onMouseDown={(e) => { if (e.target === e.currentTarget) setTokenModal(false); }}
          style={backdrop(() => setTokenModal(false))}>
          <div style={shell()}>
            <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
              <span style={brand} />
              <div style={{ fontSize: 15, fontWeight: 700, letterSpacing: "0.1em", color: "#f0f6ff", fontFamily: "var(--font-mono)" }}>
                CONFIGURE THE BOT
              </div>
            </div>
            <div style={{ fontSize: 12, color: "rgba(170,192,215,0.85)", lineHeight: 1.7, marginBottom: 12, fontFamily: "var(--font-mono)" }}>
              Create a bot with @BotFather and paste its token here. APEX checks it live.
              Only this one bot answers, and only a chat that redeems the pairing code.
            </div>
            {!status?.token_set ? (
              <input
                style={{ ...control, marginBottom: 10, fontFamily: "var(--font-mono)", fontSize: 12 }}
                autoFocus spellCheck={false} autoComplete="off"
                placeholder="1234567890:AAH4…-token"
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
              />
            ) : (
              <div style={{ fontSize: 12, color: "rgba(170,192,215,0.8)", marginBottom: 10, fontFamily: "var(--font-mono)" }}>
                A token is stored. Paste a new one to replace it, or use “Forget bot token and unlink” below.
              </div>
            )}
            {tokenError && <div style={{ fontSize: 11, color: "#ffb4a2", marginBottom: 10 }}>{tokenError}</div>}
            <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <button type="button" disabled={busy || !tokenInput.trim()} style={buttonPrimary} onClick={() => void saveToken()}>
                SAVE & CHECK
              </button>
              <button type="button" style={buttonGhost} onClick={() => setTokenModal(false)}>CANCEL</button>
            </div>
          </div>
        </div>
      )}

      {pairModal && (
        <div role="dialog" aria-modal="true" aria-label="Pair Telegram with APEX"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) {
              if (pairPoll.current) clearInterval(pairPoll.current);
              setPairModal(false);
            }
          }}
          style={backdrop(() => {})}>
          <div style={shell()}>
            <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
              <span style={brand} />
              <div style={{ fontSize: 15, fontWeight: 700, letterSpacing: "0.1em", color: "#f0f6ff", fontFamily: "var(--font-mono)" }}>
                PAIR YOUR TELEGRAM
              </div>
            </div>

            {status?.paired ? (
              <>
                <div style={{ fontSize: 12, color: "rgba(170,192,215,0.85)", lineHeight: 1.7, marginBottom: 18, fontFamily: "var(--font-mono)" }}>
                  Paired with <span style={{ color: "#00e5ff" }}>@{status.paired_username || "your chat"}</span>.
                  Text or voice the bot from there and APEX answers.
                </div>
                <button type="button" style={buttonPrimary} onClick={() => { if (pairPoll.current) clearInterval(pairPoll.current); setPairModal(false); }}>
                  DONE
                </button>
              </>
            ) : pairCode ? (
              <>
                <div style={{ fontSize: 12, color: "rgba(170,192,215,0.9)", lineHeight: 1.7, marginBottom: 12, fontFamily: "var(--font-mono)" }}>
                  Open the bot <span style={{ color: "#00e5ff" }}>{status?.bot ? `@${status.bot.username}` : ""}</span> in
                  Telegram and send this code. It pairs that chat and expires in {Math.round(pairTtl / 60)} or so
                  minutes after it was generated:
                </div>
                <div style={{
                  textAlign: "center", padding: "16px 10px", margin: "0 0 14px", borderRadius: 12,
                  background: "rgba(0,229,255,0.06)", border: "1px dashed rgba(0,229,255,0.35)",
                  fontSize: 26, fontWeight: 800, letterSpacing: "0.35em", fontFamily: "var(--font-mono)",
                  color: "#00e5ff", textShadow: "0 0 18px rgba(0,229,255,0.5)",
                }}>
                  {pairCode}
                </div>
                <div style={{ fontSize: 11, color: "rgba(170,192,215,0.6)", marginBottom: 14, fontFamily: "var(--font-mono)" }}>
                  {status?.pairing_code_active || status?.paired
                    ? "Waiting for redemption… (only this chat will ever be served)"
                    : "The code expired. Close and press the pair button again."}
                </div>
                <button type="button" style={buttonGhost} onClick={() => { if (pairPoll.current) clearInterval(pairPoll.current); setPairModal(false); }}>
                  CLOSE
                </button>
              </>
            ) : (
              <>
                <div style={{ fontSize: 12, color: "#ffb4a2", lineHeight: 1.7, marginBottom: 14, fontFamily: "var(--font-mono)" }}>
                  {pairError || "Generating a pairing code…"}
                </div>
                <button type="button" style={buttonGhost} onClick={() => setPairModal(false)}>CLOSE</button>
              </>
            )}
          </div>
        </div>
      )}
    </SettingsCard>
  );
}