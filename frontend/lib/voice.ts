"use client";

/* Voice engine: always-on wake-word listening + spoken replies.

   - SpeechRecognition runs continuously and auto-restarts on end.
   - Hearing the wake word (default "apex") arms the next utterance as a
     command; text after the wake word in the SAME utterance is also used.
   - Replies are spoken aloud with the Web Speech synthesis API. During a
     reply the mic stays live: saying the wake word cuts the speech off and
     starts a new command (barge-in).
   - After answering, a follow-up window (no wake word needed) stays open for
     `follow_up_seconds`. "Stop / sleep / goodbye / that's all" closes it.
*/

import { useEffect, useRef, useState } from "react";
import { isSleepCommand, isWakeOnlyText, isWakeWordFragment, recognitionLanguage, wakePattern } from "./voiceCommands";

export type VoicePhase = "standby" | "awake" | "thinking" | "speaking";

export type VoiceEngine = {
  supported: boolean;
  active: boolean;
  error: string | null;
  speak: (text: string) => void;
  cancelSpeech: () => void;
  isSpeaking: boolean;
  forceAwake: () => void;
  lastHeard: string;
};

type SpeechRecognitionLike = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onstart: ((e: any) => void) | null;
  onaudiostart: ((e: any) => void) | null;
  onresult: ((e: any) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: any) => void) | null;
  start: () => void;
  abort: () => void;
};

const WAKE_BEEP_FREQ = 1180;

// Auto-recovery knobs: browsers (Chromium especially) can reject a restart
// issued too soon after an abort, or start a session that silently never
// produces results. These limits back off retries so the wake word always
// comes back without requiring the user to toggle the microphone.
const MAX_START_ATTEMPTS = 4;
const START_VERIFY_MS = 5000; // a started session must fire onstart within this
const STALE_RESULTS_MS = 120000; // only case a session is wedged if it is this old

const DEBUG_VOICE =
  typeof window !== "undefined" &&
  typeof localStorage !== "undefined" &&
  localStorage.getItem("apex:debug:voice") === "1";
function vlog(...args: any[]) {
  if (DEBUG_VOICE) console.log("[voice]", ...args);
}

function SRClassAvailable(): boolean {
  if (typeof window === "undefined") return false;
  const w = window as any;
  return !!(w.SpeechRecognition || w.webkitSpeechRecognition);
}

function speechSupportMessage(): string {
  if (typeof navigator !== "undefined" && /firefox/i.test(navigator.userAgent)) {
    return "Firefox does not support browser speech recognition. Use Chrome or Edge for wake words.";
  }
  return "Speech recognition is not supported by this browser.";
}

export function useVoiceEngine(opts: {
  enabled: boolean;
  wakeWord: string;
  followUpSeconds: number;
  voiceName: string;
  responseLanguage: string;
  onPhase: (p: VoicePhase) => void;
  onWake?: () => void;
  onCommand: (text: string) => void;
}): VoiceEngine {
  const { enabled, wakeWord, followUpSeconds, voiceName, responseLanguage, onPhase, onWake, onCommand } = opts;

  const cfgRef = useRef({ wakeWord, followUpSeconds, voiceName, responseLanguage, onPhase, onWake, onCommand });
  cfgRef.current = { wakeWord, followUpSeconds, voiceName, responseLanguage, onPhase, onWake, onCommand };

  const recRef = useRef<SpeechRecognitionLike | null>(null);
  const phaseRef = useRef<VoicePhase>("standby");
  const armedRef = useRef(false); // next utterance = command
  const stoppingRef = useRef(false);
  const recStartedRef = useRef(false); // current session fired onstart/onaudiostart
  const startAttemptsRef = useRef(0); // consecutive failed starts (backoff)
  const startVerifyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const followUpUntilRef = useRef(0);
  const postSpeechDeafUntilRef = useRef(0); // ignore mic echo after TTS finishes
  const lastFireRef = useRef<{ text: string; at: number }>({ text: "", at: 0 });
  const lastResultAtRef = useRef<number>(0);
  const healthTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const [active, setActive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const activeRef = useRef(active);
  activeRef.current = active;

  const [segmentsLeft, setSegmentsLeft] = useState(0);
  const queueRef = useRef<string[]>([]);
  const restartTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const commandTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const followUpLangTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingCommand = useRef("");
  const idleAwaitedRef = useRef(false);
  const [lastHeard, setLastHeard] = useState<string>("");

  const recognitionLang = (): string => recognitionLanguage(
    cfgRef.current.responseLanguage,
    cfgRef.current.wakeWord,
    phaseRef.current,
    armedRef.current,
  );

  const syncRecognitionLanguage = () => {
    if (recRef.current && recRef.current.lang !== recognitionLang()) {
      try {
        recRef.current.abort();
      } catch {}
    }
  };

  const clearFollowUpLangTimer = () => {
    if (followUpLangTimer.current) {
      clearTimeout(followUpLangTimer.current);
      followUpLangTimer.current = null;
    }
  };

  /* ---------- speech (TTS) ---------- */

  const setPhase = (p: VoicePhase) => {
    phaseRef.current = p;
    vlog("phase ->", p, "armed:", armedRef.current, "followUpUntil:", followUpUntilRef.current ? followUpUntilRef.current - Date.now() : 0);
    cfgRef.current.onPhase(p);
  };

  const sleepVoice = () => {
    armedRef.current = false;
    followUpUntilRef.current = 0;
    pendingCommand.current = "";
    if (commandTimer.current) clearTimeout(commandTimer.current);
    if (idleTimer.current) clearTimeout(idleTimer.current);
    clearFollowUpLangTimer();
    vlog("sleep command, disarming");
    setPhase("standby");
    syncRecognitionLanguage();
  };

  const finishSpeaking = () => {
    queueRef.current = [];
    setSegmentsLeft(0);
    if (commandTimer.current) clearTimeout(commandTimer.current);
    pendingCommand.current = "";
    const fu = cfgRef.current.followUpSeconds;
    if (fu > 0) {
      followUpUntilRef.current = Date.now() + fu * 1000;
      armedRef.current = true;
      // Briefly ignore the mic after TTS stops so the assistant's own voice
      // (speaker echo) is not re-recognized as a user command.
      postSpeechDeafUntilRef.current = Date.now() + 600;
      // A custom English wake word may need a different recognizer language
      // during its follow-up window.
      clearFollowUpLangTimer();
      followUpLangTimer.current = setTimeout(() => {
        if (!armedRef.current) return;
        armedRef.current = false;
        vlog("follow-up window closed, returning to wake-word listening");
        syncRecognitionLanguage();
      }, fu * 1000);
      vlog("follow-up armed for", fu, "s");
    } else {
      vlog("follow-up disabled (fu=", fu, ")");
    }
    setPhase("standby");
    syncRecognitionLanguage();
  };

  const cancelSpeech = () => {
    queueRef.current = [];
    setSegmentsLeft(0);
    try {
      window.speechSynthesis?.cancel();
    } catch {}
    setPhase("standby");
  };

  const forceAwake = () => {
    if (!activeRef.current) return;
    vlog("force awake via tap/shortcut");
    cancelSpeech();
    // Build a fake transcript that is just the wake word so wakeSlow enters the
    // armed awake state and beeps.
    wakeSlow(cfgRef.current.wakeWord, cfgRef.current.wakeWord);
  };

  const speak = (text: string) => {
    try {
      window.speechSynthesis?.cancel();
    } catch {}
    const clean = (text || "").replace(/\s+/g, " ").trim();
    if (!clean || !window.speechSynthesis) {
      finishSpeaking();
      return;
    }
    queueRef.current = splitIntoChunks(clean);
    setSegmentsLeft(queueRef.current.length);
    setPhase("speaking");
    spinQueue();
  };

  const spinQueue = () => {
    const synth = window.speechSynthesis;
    if (!synth) return finishSpeaking();
    if (synth.speaking || synth.pending) return;
    const next = queueRef.current.shift();
    if (next === undefined) {
      finishSpeaking();
      return;
    }
    setSegmentsLeft(queueRef.current.length);
    const utt = new SpeechSynthesisUtterance(next);
    utt.rate = 1.02;
    utt.pitch = 1.0;
    const want = cfgRef.current.voiceName.toLowerCase();
    const wantsGreek = cfgRef.current.responseLanguage === "el";
    const voices = synth.getVoices();
    const picked =
      (wantsGreek && voices.find((v: any) => /^el(?:[-_]|$)/i.test(v.lang) || /greek|ελλην/i.test(v.name))) ??
      (!wantsGreek && voices.find((v: any) => v.name.toLowerCase() === want)) ??
      voices.find((v: any) => /en[-_]/i.test(v.lang) && /female|google/i.test(v.name)) ??
      voices.find((v: any) => /en[-_]/i.test(v.lang)) ??
      voices[0];
    if (picked) {
      utt.voice = picked;
      utt.lang = picked.lang;
    }
    utt.onend = () => {
      const left = queueRef.current.length;
      setSegmentsLeft(left);
      if (left === 0) finishSpeaking();
      else spinQueue();
    };
    utt.onerror = (e: any) => {
      if (e?.error && e.error !== "interrupted" && e.error !== "canceled") {
        finishSpeaking();
      } else if (queueRef.current.length === 0) {
        finishSpeaking();
      } else if (!synth.speaking) {
        spinQueue();
      }
    };
    synth.speak(utt);
  };

  /* warm the voice list on load (some engines load async) */
  useEffect(() => {
    if (typeof window === "undefined" || !window.speechSynthesis) return;
    const warm = () => window.speechSynthesis.getVoices();
    warm();
    if (typeof window !== "undefined") {
      const w = window as any;
      if (w.speechSynthesis && w.speechSynthesis.addEventListener) {
        w.speechSynthesis.addEventListener?.("voiceschanged", warm);
      }
    }
  }, []);

  /* ---------- recognition ---------- */

  const utteranceText = (e: any): string => {
    const last = e.results?.[e.results.length - 1]?.[0];
    return last?.transcript?.trim() ?? "";
  };

  const isFinal = (e: any): boolean => !!e.results?.[e.results.length - 1]?.[0]?.isFinal;

  const beep = () => {
    try {
      const ctxClass = (window as any).AudioContext ?? (window as any).webkitAudioContext;
      if (!ctxClass) return;
      const ctx: AudioContext = new ctxClass();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = WAKE_BEEP_FREQ;
      const t = ctx.currentTime;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.12, t + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
      osc.connect(gain).connect(ctx.destination);
      osc.start();
      osc.stop(t + 0.24);
      osc.onended = () => ctx.close().catch(() => {});
    } catch {
      /* audio blocked - skip */
    }
  };

  const fireCommand = (raw: string) => {
    const text = raw.replace(/\s+/g, " ").trim();
    if (!text) return;
    if (isSleepCommand(text, cfgRef.current.responseLanguage)) {
      sleepVoice();
      return;
    }
    const now = Date.now();
    if (lastFireRef.current.text === text && now - lastFireRef.current.at < 2500) return; // dup frame
    lastFireRef.current = { text, at: now };
    armedRef.current = false;
    vlog("fireCommand:", text);
    setPhase("thinking");
    // End the current question-language session. The onend handler normally
    // opens a fresh session, but browsers occasionally swallow the onend event
    // after abort(); schedule a guaranteed restart as a backstop.
    try {
      recRef.current?.abort();
    } catch {}
    scheduleRestart(400);
    cfgRef.current.onCommand(text);
  };

  const sweepIdleAwait = () => {
    if (idleTimer.current) clearTimeout(idleTimer.current);
    idleTimer.current = setTimeout(() => {
      if (armedRef.current && phaseRef.current === "awake") {
        armedRef.current = false;
        setPhase("standby");
        syncRecognitionLanguage();
      }
    }, 15000);
  };

  const queueAwakeCommand = (text: string, re: RegExp, ww: string, final: boolean) => {
    const wakeMatch = text.match(re);
    const command = (wakeMatch && wakeMatch.index !== undefined
      ? text.slice(wakeMatch.index + wakeMatch[0].length)
      : text).replace(/^[\s,.;:!?··;]+/g, "").replace(/\s+/g, " ").trim();
    if (!command) return;
    // Edge may emit fragments of the wake word while it is still listening.
    // Never send those fragments as commands (for example "a" or "ape").
    if (isWakeWordFragment(command, ww, cfgRef.current.responseLanguage)) return;
    pendingCommand.current = command;
    if (commandTimer.current) clearTimeout(commandTimer.current);
    if (final) {
      pendingCommand.current = "";
      fireCommand(command);
      return;
    }
    // Some Edge speech-service sessions never mark the second utterance final.
    // Treat a short period of silence after interim text as the command end.
    commandTimer.current = setTimeout(() => {
      if (phaseRef.current !== "awake") return;
      const pending = pendingCommand.current;
      pendingCommand.current = "";
      fireCommand(pending);
    }, 900);
  };

  const queueFollowUpCommand = (text: string, final: boolean) => {
    const command = text.replace(/\s+/g, " ").trim();
    if (!command) return;
    const normalized = command.toLowerCase().replace(/[.!?,;:]+/g, "").trim();
    if (normalized.length < 2) return;
    pendingCommand.current = command;
    if (commandTimer.current) clearTimeout(commandTimer.current);
    if (final) {
      pendingCommand.current = "";
      fireCommand(command);
      return;
    }
    // Treat a short silence during the follow-up window as the end of the command,
    // even if the browser never marks the result final.
    commandTimer.current = setTimeout(() => {
      if (phaseRef.current !== "standby" || !armedRef.current) return;
      const pending = pendingCommand.current;
      pendingCommand.current = "";
      if (pending && Date.now() <= followUpUntilRef.current) {
        fireCommand(pending);
      }
    }, 900);
  };

  const wakeSlow = (text: string, ww: string) => {
    vlog("wakeSlow:", text);
    cfgRef.current.onPhase("awake");
    setPhase("awake");
    armedRef.current = true;
    clearFollowUpLangTimer();
    beep();
    cfgRef.current.onWake?.();
    syncRecognitionLanguage();
    const language = cfgRef.current.responseLanguage;
    const re = wakePattern(ww, language);
    const m = text.match(re);
    const rest = m && m.index !== undefined ? text.slice(m.index + m[0].length).replace(/^[\s,.;:!?··;]+/g, "").trim() : "";
    if (rest && !isWakeOnlyText(rest, ww, language) && !re.test(rest)) {
      fireCommand(rest);
      return;
    }
    idleAwaitedRef.current = true;
    sweepIdleAwait();
  };

  const onResult = (e: any) => {
    lastResultAtRef.current = Date.now();
    const text = utteranceText(e);
    const final = isFinal(e);
    setLastHeard(`${final ? "✓" : "…"} ${text}`);
    const ww = cfgRef.current.wakeWord.toLowerCase();
    const language = cfgRef.current.responseLanguage;
    const re = wakePattern(ww, language);
    const low = text.toLowerCase();
    const phase = phaseRef.current;

    // barge-in: wake word cuts off speech / thinking
    if ((phase === "speaking" || phase === "thinking") && re.test(low)) {
      cancelSpeech();
      wakeSlow(text, ww);
      return;
    }

    if (phase === "awake") {
      if (isWakeOnlyText(text, ww, language)) return;
      if (isSleepCommand(text, language)) {
        sleepVoice();
        return;
      }
      queueAwakeCommand(text, re, ww, final);
      return;
    }

    if (phase !== "standby") {
      // Thinking is owned by the chat request; only a wake word can interrupt it.
      return;
    }

    // Ignore mic echo for a short moment after the assistant finishes speaking.
    if (Date.now() < postSpeechDeafUntilRef.current) {
      vlog("ignoring during post-TTS deaf window:", text);
      return;
    }

    // standby: wake word detection
    if (re.test(low)) {
      vlog("wake word detected:", text);
      wakeSlow(text, ww);
      return;
    }

    // follow-up window (armed, no wake word)
    if (armedRef.current) {
      vlog("follow-up candidate:", text, "final:", final, "ms left:", followUpUntilRef.current - Date.now());
      if (isWakeOnlyText(text, ww, language)) {
        wakeSlow(text, ww);
        return;
      }
      if (isSleepCommand(text, language)) {
        sleepVoice();
        return;
      }
      if (Date.now() <= followUpUntilRef.current) {
        queueFollowUpCommand(text, final);
        return;
      }
      vlog("follow-up window expired");
      armedRef.current = false;
      clearFollowUpLangTimer();
      syncRecognitionLanguage();
    }
  };

  /** Abort and drop the current recognizer without permanently stopping voice. */
  const teardownRecognition = () => {
    if (recRef.current) {
      try {
        recRef.current.abort();
      } catch {}
      recRef.current = null;
    }
    if (startVerifyTimer.current) {
      clearTimeout(startVerifyTimer.current);
      startVerifyTimer.current = null;
    }
    recStartedRef.current = false;
  };

  /**
   * Schedule a fresh recognition session. Replaces any pending restart so the
   * onend handler, the post-command backstop and the health check can never
   * double-spawn recognizers. A short delay lets the browser release the mic
   * from the previous session (Chromium rejects eager restarts).
   */
  const scheduleRestart = (delayMs = 350) => {
    if (!activeRef.current || stoppingRef.current) return;
    if (restartTimer.current) clearTimeout(restartTimer.current);
    restartTimer.current = setTimeout(() => {
      restartTimer.current = null;
      if (!activeRef.current || stoppingRef.current) return;
      teardownRecognition();
      startRecognition();
    }, delayMs);
  };

  /** A recognizer that never fires onstart is dead; force a fresh session. */
  const startVerify = (rec: SpeechRecognitionLike) => {
    if (startVerifyTimer.current) clearTimeout(startVerifyTimer.current);
    startVerifyTimer.current = setTimeout(() => {
      startVerifyTimer.current = null;
      if (!activeRef.current || stoppingRef.current) return;
      if (recRef.current !== rec || recStartedRef.current) return;
      vlog("start-verify: recognizer never started, restarting");
      scheduleRestart(500);
    }, START_VERIFY_MS);
  };

  const startRecognition = () => {
    const W = window as any;
    const SRClass = W.SpeechRecognition || W.webkitSpeechRecognition;
    if (!SRClass) {
      setError("Speech recognition is not supported by this browser.");
      return;
    }
    setError(null);
    teardownRecognition();
    let rec: SpeechRecognitionLike;
    try {
      rec = new SRClass();
    } catch (err) {
      stoppingRef.current = true;
      setError(`Could not start voice recognition: ${err instanceof Error ? err.message : String(err)}`);
      setActive(false);
      return;
    }
    rec.lang = recognitionLang();
    // Keep the microphone session alive across the wake word and the command.
    // The newest-result parser below prevents Edge's cumulative results from
    // replaying older speech.
    rec.continuous = true;
    rec.interimResults = true;
    rec.onstart = () => {
      recStartedRef.current = true;
      startAttemptsRef.current = 0;
      vlog("recognition session started");
    };
    rec.onaudiostart = () => {
      recStartedRef.current = true;
      startAttemptsRef.current = 0;
    };
    rec.onresult = (e) => onResult(e);
    rec.onend = () => {
      // Ignore onend from a session we have already replaced or aborted for
      // restart; only the current session may restart the engine.
      if (recRef.current !== rec) return;
      recRef.current = null;
      recStartedRef.current = false;
      if (!stoppingRef.current && activeRef.current) {
        scheduleRestart(300);
      }
    };
    rec.onerror = (e: any) => {
      const code = e?.error || "unknown";
      if (code === "aborted" || code === "canceled") return;
      if (code === "no-speech") return; // not fatal for a continuous session
      if (code === "not-allowed" || code === "service-not-allowed") {
        stoppingRef.current = true;
        setError(code === "not-allowed"
          ? "Microphone access was denied. Allow microphone access for this page."
          : "This browser does not allow its speech service. Open Apex in Chrome or Edge directly.");
        setActive(false);
        return;
      }
      if (code === "network") {
        // Chromium reports a transient network error when its remote speech
        // service drops the recognition session. Restart without showing a
        // stale error beside the microphone control.
        setError(null);
        scheduleRestart(600);
        return;
      }
      if (startAttemptsRef.current >= MAX_START_ATTEMPTS) {
        // Persistent audio trouble (e.g. mic in use elsewhere): stop retrying
        // rather than loop forever; the user can toggle the mic back on.
        stoppingRef.current = true;
        setError(`Voice recognition error: ${code}`);
        setActive(false);
        return;
      }
      startAttemptsRef.current += 1;
      setError(`Voice recognition error: ${code}`);
      scheduleRestart(400 * startAttemptsRef.current);
    };
    recRef.current = rec;
    lastResultAtRef.current = Date.now();
    recStartedRef.current = false;
    try {
      rec.start();
    } catch (err) {
      // Chromium rejects a start issued right after the previous session
      // ended. Retry with a growing delay instead of disabling voice.
      startAttemptsRef.current += 1;
      const detail = err instanceof Error ? err.message : String(err);
      if (/(not allowed|permission|denied)/i.test(detail)) {
        stoppingRef.current = true;
        setError(`Could not start voice recognition: ${detail}`);
        setActive(false);
      } else if (startAttemptsRef.current >= MAX_START_ATTEMPTS) {
        setError(`Could not start voice recognition: ${detail}`);
        setActive(false);
      } else {
        vlog("start rejected, retrying", startAttemptsRef.current);
        recRef.current = null;
        scheduleRestart(400 * startAttemptsRef.current);
        return;
      }
    }
    startVerify(rec);
  };

  const stopRecognition = () => {
    stoppingRef.current = true;
    try {
      recRef.current?.abort();
    } catch {}
    recRef.current = null;
    recStartedRef.current = false;
    if (startVerifyTimer.current) {
      clearTimeout(startVerifyTimer.current);
      startVerifyTimer.current = null;
    }
    if (restartTimer.current) clearTimeout(restartTimer.current);
    if (idleTimer.current) clearTimeout(idleTimer.current);
    if (commandTimer.current) clearTimeout(commandTimer.current);
    if (healthTimer.current) clearInterval(healthTimer.current);
    clearFollowUpLangTimer();
    pendingCommand.current = "";
  };

  const startHealthCheck = () => {
    if (healthTimer.current) clearInterval(healthTimer.current);
    lastResultAtRef.current = Date.now();
    healthTimer.current = setInterval(() => {
      if (!activeRef.current || stoppingRef.current) return;
      const now = Date.now();
      const msSinceResult = now - lastResultAtRef.current;
      // In a quiet room there are no results, so only restart if the session
      // is either missing or very old. This catches browser sessions that
      // silently die without firing onend. The start-verify watchdog in
      // startRecognition handles sessions that never fire onstart at all.
      const hasRec = !!recRef.current;
      const phase = phaseRef.current;
      // Only force-restart while wake-word listening; during thinking/speaking
      // the existing onend handler will restart after the assistant finishes.
      const canRestart = phase === "standby" || phase === "awake";
      const isStuck = hasRec && recStartedRef.current && msSinceResult > STALE_RESULTS_MS && canRestart;
      if ((!hasRec && canRestart) || isStuck) {
        vlog("health check: recognition", hasRec ? "stuck" : "missing", "restarting");
        setError(null);
        scheduleRestart(isStuck ? 1000 : 200);
      }
    }, 10000);
  };

  /* start/stop with the user toggle */
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (enabled && !active && !SRClassAvailable()) {
      setError(speechSupportMessage());
      setPhase("standby");
    } else if (enabled && !active && SRClassAvailable()) {
      stoppingRef.current = false;
      setActive(true);
      setPhase("standby");
      // SpeechRecognition must start directly from the toggle gesture in some
      // Chromium builds. A separate getUserMedia check can remain pending, so
      // it is diagnostic only and must not gate recognition startup.
      startRecognition();
      startHealthCheck();
      void navigator.mediaDevices?.getUserMedia({ audio: true })
        .then((stream) => stream.getTracks().forEach((track) => track.stop()))
        .catch(() => {
          if (activeRef.current) setError("Microphone access was denied. Allow microphone access for this page.");
        });
    } else if (!enabled && active) {
      stopRecognition();
      cancelSpeech();
      setActive(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  // Self-heal: if voice stays enabled but the recognizer died (or a restart
  // was rejected) without firing the enable path, bring it back automatically
  // so the wake word keeps working — instead of needing a manual mic toggle.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!enabled || active) return;
    if (stoppingRef.current) return;
    const t = setTimeout(() => {
      if (!enabled || stoppingRef.current) return;
      if (activeRef.current) return; // recovered on its own
      vlog("self-heal: voice enabled but recognition inactive, restarting");
      stoppingRef.current = false;
      setActive(true);
      setPhase("standby");
      startRecognition();
      startHealthCheck();
    }, 800);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, active]);

  // Apply language/wake-word setting changes to an already active microphone.
  useEffect(() => {
    if (activeRef.current && !stoppingRef.current) syncRecognitionLanguage();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [responseLanguage, wakeWord]);

  return {
    supported: SRClassAvailable(),
    active,
    error,
    speak,
    cancelSpeech,
    isSpeaking: segmentsLeft > 0,
    forceAwake,
    lastHeard,
  };
}

/* ---------- helpers ---------- */

function splitIntoChunks(text: string): string[] {
  const sentences = text.match(/[^.!?]+[.!?]+|[^.!?]+$/g) ?? [text];
  const out: string[] = [];
  let buf = "";
  for (const s of sentences) {
    if ((buf + " " + s).trim().length > 220) {
      if (buf.trim()) out.push(buf.trim());
      buf = s;
    } else {
      buf += " " + s;
    }
  }
  if (buf.trim()) out.push(buf.trim());
  return out.filter(Boolean).map((s) => s.trim());
}
