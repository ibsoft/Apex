"use client";

/* Voice engine: always-on wake-word listening + spoken replies.

   - SpeechRecognition runs continuously and auto-restarts on end.
   - Hearing the wake word (default "apex") arms the next utterance as a
      command; text after the wake word in the SAME utterance is also used. A
      short random acknowledgement ("I'm listening.", «Σε ακούω», …) is spoken
      right after the beep to confirm the mic is live, without leaving the awake
      state, so a command can start while it is still talking.
   - A command is sent only after a stable quiet period (ENDPOINT_SILENCE_MS)
     with no new recognition results. All finalized chunks of one sentence are
     merged with the live interim, so pausing mid-sentence (or a browser that
     finalizes chunks at each pause) never cuts the command, English or Greek.
   - Replies are spoken aloud with the Web Speech synthesis API. During a
     reply the mic stays live: saying the wake word cuts the speech off and
     starts a new command (barge-in).
   - After answering, a follow-up window (no wake word needed) stays open for
     `follow_up_seconds`. "Stop / sleep / goodbye / that's all" closes it.
*/

import { useEffect, useRef, useState } from "react";
import { isAlarmStopPhrase } from "./commands";
import { settleVoiceCommand } from "./voiceLifecycle";
import {
  accumulateResults,
  commandText,
  emptyResultSnapshot,
  initialVadState,
  isResumeCommand,
  isSleepCommand,
  isWakeOnlyText,
  isWakeWordFragment,
  pickWakeAck,
  recognitionLanguage,
  sensitivityToThreshold,
  sliceAfterLastWake,
  speechSummary,
  stripAckEcho,
  vadStep,
  wakePattern,
  type ResultSnapshot,
  type VadState,
} from "./voiceCommands";

export type VoicePhase = "standby" | "awake" | "thinking" | "speaking";

/** Health of the second mic stream that powers talk-over interruption. */
export type VoiceVadStatus = "off" | "starting" | "live" | "blocked" | "error";

export type VoiceEngine = {
  supported: boolean;
  active: boolean;
  error: string | null;
  speak: (text: string) => void;
  cancelSpeech: () => void;
  isSpeaking: boolean;
  forceAwake: () => void;
  lastHeard: string;
  vadStatus: VoiceVadStatus;
  vadNote: string;
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

/* The acknowledgement is short and the recognizer keeps running while it is
 * spoken, so the user can start talking straight away. The echo guard only has
 * to outlast the utterance itself: it stops filtering shortly after the
 * acknowledgement ends, so a later command that happens to contain the same
 * words is never truncated. */
const ACK_ECHO_TTL_MS = 6000; // while the acknowledgement may still be playing
const ACK_ECHO_TAIL_MS = 2500; // extra window after it stops
const ACK_ECHO_MAX_HITS = 3; // same interim repeated: ignore instead of re-arming

// Auto-recovery knobs: browsers (Chromium especially) can reject a restart
// issued too soon after an abort, or start a session that silently never
// produces results. These limits back off retries so the wake word always
// comes back without requiring the user to toggle the microphone.
const MAX_START_ATTEMPTS = 4;
const START_VERIFY_MS = 5000; // a started session must fire onstart within this
const STALE_RESULTS_MS = 120000; // only case a session is wedged if it is this old
// A command is sent only after this much quiet following the LAST result update
// (interim results keep flowing while the user is still talking). This is the
// real "user stopped talking" signal instead of a browser isFinal flag, so a
// mid-sentence pause never cuts the transcript.
const ENDPOINT_SILENCE_MS = 1600;

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
  /** Let talking over the reply cut the speech off (and "continue" resume it). */
  interruptEnabled: boolean;
  /** 1..10 slider; higher = interrupt more eagerly. */
  interruptSensitivity: number;
  /** True while a reminder alarm is ringing. */
  alarmActive?: boolean;
  /** Called when a "stop" phrase is heard while `alarmActive`. */
  onAlarmStop?: () => void;
  onPhase: (p: VoicePhase) => void;
  onWake?: () => void;
  onCommand: (text: string) => void | Promise<void>;
}): VoiceEngine {
  const { enabled, wakeWord, followUpSeconds, voiceName, responseLanguage, interruptEnabled, interruptSensitivity, alarmActive, onAlarmStop, onPhase, onWake, onCommand } = opts;

  const cfgRef = useRef({ wakeWord, followUpSeconds, voiceName, responseLanguage, interruptEnabled, interruptSensitivity, alarmActive, onAlarmStop, onPhase, onWake, onCommand });
  cfgRef.current = { wakeWord, followUpSeconds, voiceName, responseLanguage, interruptEnabled, interruptSensitivity, alarmActive, onAlarmStop, onPhase, onWake, onCommand };

  const recRef = useRef<SpeechRecognitionLike | null>(null);
  const phaseRef = useRef<VoicePhase>("standby");
  const commandEpoch = useRef(0);
  const armedRef = useRef(false); // next utterance = command
  const stoppingRef = useRef(false);
  const recStartedRef = useRef(false); // current session fired onstart/onaudiostart
  const startAttemptsRef = useRef(0); // consecutive failed starts (backoff)
  const startVerifyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const followUpUntilRef = useRef(0);
  const postSpeechDeafUntilRef = useRef(0); // ignore mic echo after TTS finishes
  const lastFireRef = useRef<{ text: string; at: number }>({ text: "", at: 0 });
  const lastResultAtRef = useRef<number>(0);
  const lastResultLenRef = useRef(0); // results length seen by the last onresult
  const accRef = useRef<ResultSnapshot>(emptyResultSnapshot(0)); // utterance collector
  const needsWakeSliceRef = useRef(false); // awake-collection mode vs follow-up mode
  const healthTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const ackRef = useRef<{ text: string; until: number; hits: number } | null>(null);
  const lastWakeAckRef = useRef<string | null>(null); // avoid repeating the last phrase

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
  const [lastHeard, setLastHeard] = useState<string>("");

  // Interruption health, surfaced in settings: a silently dead VAD looks
  // exactly like a working one, which is how "does not stop talking" becomes
  // unreportable. "live" means the analyser is running; "blocked"/"error"
  // carry a note the operator can act on.
  const [vadStatus, setVadStatus] = useState<VoiceVadStatus>("off");
  const [vadNote, setVadNote] = useState<string>("");
  const reportVad = (s: VoiceVadStatus, note = "") => {
    setVadStatus(s);
    setVadNote(note);
  };

  // Interruption + resume state. resumeRef holds the untold remainder of a
  // reply cut off by voice activity, so "continue" can speak on from there;
  // inProgressRef tracks how much of the current chunk was already spoken
  // (via utterance boundary events). speakSinceRef gates the VAD for the first
  // moments of a reply so the assistant's own opening words cannot cut it.
  const resumeRef = useRef<string[] | null>(null);
  const inProgressRef = useRef<{ text: string; spoken: number } | null>(null);
  const speakSinceRef = useRef(0);
  const vadRef = useRef<{ stop: () => void } | null>(null);
  const vadStateRef = useRef<VadState>(initialVadState());

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
    commandEpoch.current += 1;
    armedRef.current = false;
    followUpUntilRef.current = 0;
    pendingCommand.current = "";
    needsWakeSliceRef.current = false;
    lastWakeAckRef.current = null;
    resumeRef.current = null;
    forgetAck(true);
    accRef.current = emptyResultSnapshot(lastResultLenRef.current);
    if (commandTimer.current) clearTimeout(commandTimer.current);
    if (idleTimer.current) clearTimeout(idleTimer.current);
    clearFollowUpLangTimer();
    vlog("sleep command, disarming");
    setPhase("standby");
    syncRecognitionLanguage();
  };

  const finishSpeaking = () => {
    queueRef.current = [];
    resumeRef.current = null; // nothing left to resume
    inProgressRef.current = null;
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
      // Follow-up commands are not preceded by a wake word, so no slicing.
      needsWakeSliceRef.current = false;
      // Start fresh: exclude the assistant's reply that was captured while
      // the recognizer stayed live for barge-in.
      accRef.current = emptyResultSnapshot(lastResultLenRef.current);
      pendingCommand.current = "";
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
    commandEpoch.current += 1;
    queueRef.current = [];
    inProgressRef.current = null;
    setSegmentsLeft(0);
    forgetAck(true);
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
    wakeSlow({ results: [{ isFinal: true, 0: { transcript: cfgRef.current.wakeWord } }] }, cfgRef.current.wakeWord);
  };

  const speak = (text: string) => {
    try {
      window.speechSynthesis?.cancel();
    } catch {}
    // A real response replaces the wake acknowledgement; its echo guard would
    // otherwise strip those same words from a later command.
    forgetAck();
    const clean = speechSummary(text || "").replace(/\s+/g, " ").trim();
    if (!clean || !window.speechSynthesis) {
      finishSpeaking();
      return;
    }
    // A fresh reply supersedes any saved "continue" remainder.
    resumeRef.current = null;
    queueRef.current = splitIntoChunks(clean);
    setSegmentsLeft(queueRef.current.length);
    speakSinceRef.current = Date.now();
    setPhase("speaking");
    spinQueue();
  };

  /** Resume a reply cut off by an accidental voice interruption. */
  const resumeSpeaking = () => {
    const chunks = resumeRef.current;
    if (!chunks?.length) return;
    resumeRef.current = null;
    commandEpoch.current += 1;
    try {
      window.speechSynthesis?.cancel();
    } catch {}
    queueRef.current = chunks;
    setSegmentsLeft(chunks.length);
    speakSinceRef.current = Date.now();
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
    // Track how far this chunk got, so an interruption can resume from the
    // current word instead of repeating the whole sentence. Engines that never
    // fire boundary events keep spoken=0 and the chunk replays in full - a
    // small repeat, never a lost tail.
    inProgressRef.current = { text: next, spoken: 0 };
    utt.onboundary = (e: any) => {
      if (inProgressRef.current?.text === next) {
        const idx = typeof e?.charIndex === "number" ? e.charIndex : inProgressRef.current.spoken;
        inProgressRef.current.spoken = Math.max(0, Math.min(next.length, idx));
      }
    };
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
      if (inProgressRef.current?.text === next) inProgressRef.current = null;
      const left = queueRef.current.length;
      setSegmentsLeft(left);
      if (left === 0) finishSpeaking();
      else spinQueue();
    };
    utt.onerror = (e: any) => {
      if (inProgressRef.current?.text === next) inProgressRef.current = null;
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

  /** Forget a pending acknowledgement, its echo guard and its audible utterance. */
  const forgetAck = (cancelSpeechToo = false) => {
    ackRef.current = null;
    if (cancelSpeechToo) {
      try {
        window.speechSynthesis?.cancel();
      } catch {}
    }
  };

  /**
   * Speak the random wake acknowledgement. It deliberately bypasses `speak()`:
   * the phase must stay "awake" so the command that follows is still collected
   * and the orb keeps showing LISTENING. A Greek reply language gets Greek
   * phrases, otherwise the Greek TTS voice would read English.
   */
  const speakAck = () => {
    const language = cfgRef.current.responseLanguage;
    const text = pickWakeAck(language, lastWakeAckRef.current);
    if (!text) return;
    lastWakeAckRef.current = text;
    const synth = typeof window === "undefined" ? undefined : window.speechSynthesis;
    if (!synth) return;
    // Any response the assistant is still giving is obsolete now.
    forgetAck(true);
    ackRef.current = { text, until: Date.now() + ACK_ECHO_TTL_MS, hits: 0 };
    try {
      const utt = new SpeechSynthesisUtterance(text);
      utt.rate = 1.02;
      utt.pitch = 1.0;
      const voices = synth.getVoices();
      const wantsGreek = /^el(?:-|$)/i.test(language || "en");
      const picked =
        (wantsGreek && voices.find((v: any) => /^el(?:[-_]|$)/i.test(v.lang) || /greek|ελλην/i.test(v.name))) ??
        (!wantsGreek && voices.find((v: any) => v.name.toLowerCase() === cfgRef.current.voiceName.toLowerCase())) ??
        voices.find((v: any) => (wantsGreek ? /^el[-_]/i.test(v.lang) : /en[-_]/i.test(v.lang))) ??
        voices[0];
      if (picked) {
        utt.voice = picked;
        utt.lang = picked.lang;
      }
      // Only the explicit cancel() must drop it: barge-in and disabling the mic
      // rely on that, a stale/errored utterance must not swallow a real command.
      // The echo guard is narrowed to a short tail once the audio is over.
      const settleAck = () => {
        if (ackRef.current?.text !== text) return;
        ackRef.current = { ...ackRef.current, until: Date.now() + ACK_ECHO_TAIL_MS };
      };
      utt.onend = settleAck;
      utt.onerror = settleAck;
      synth.speak(utt);
    } catch {
      ackRef.current = null;
    }
  };

  /**
   * Remove the acknowledgement from text the recognizer heard back through the
   * speakers, before the wake word is sliced off. Returns null when the echo
   * took over the utterance and the result must be ignored entirely.
   */
  const applyAckEcho = (text: string): string | null => {
    const ack = ackRef.current;
    if (!ack) return text;
    if (Date.now() > ack.until) {
      ackRef.current = null;
      return text;
    }
    if (text.toLowerCase() === ack.text.toLowerCase() && ack.hits < ACK_ECHO_MAX_HITS) {
      ack.hits += 1;
      return null;
    }
    return stripAckEcho(text, ack.text);
  };

  const fireCommand = (raw: string) => {
    const text = raw.replace(/\s+/g, " ").trim();
    if (!text) return;
    // "continue" after an accidental interruption resumes the reply from where
    // it was cut off - consumed here so it never reaches the agent. Without a
    // saved remainder it falls through and the agent answers normally.
    if (isResumeCommand(text, cfgRef.current.responseLanguage) && resumeRef.current?.length) {
      vlog("resume after interruption:", text);
      resumeSpeaking();
      return;
    }
    if (isSleepCommand(text, cfgRef.current.responseLanguage)) {
      sleepVoice();
      return;
    }
    const now = Date.now();
    if (lastFireRef.current.text === text && now - lastFireRef.current.at < 2500) return; // dup frame
    lastFireRef.current = { text, at: now };
    armedRef.current = false;
    needsWakeSliceRef.current = false;
    lastWakeAckRef.current = null;
    forgetAck();
    accRef.current = emptyResultSnapshot(lastResultLenRef.current);
    if (commandTimer.current) clearTimeout(commandTimer.current);
    commandTimer.current = null;
    vlog("fireCommand:", text);
    setPhase("thinking");
    // End the current question-language session. The onend handler normally
    // opens a fresh session, but browsers occasionally swallow the onend event
    // after abort(); schedule a guaranteed restart as a backstop.
    try {
      recRef.current?.abort();
    } catch {}
    scheduleRestart(400);
    const epoch = ++commandEpoch.current;
    void settleVoiceCommand(
      () => cfgRef.current.onCommand(text),
      () => commandEpoch.current === epoch && phaseRef.current === "thinking" && !stoppingRef.current,
      finishSpeaking,
      (error) => setError(error instanceof Error ? error.message : String(error)),
    );
  };

  /**
   * Voice-activity interruption: the user spoke over the reply, so cut the
   * speech off. The untold remainder is saved for "continue", and the
   * microphone is re-armed so whatever the user is saying becomes the next
   * command. This is distinct from a wake-word barge-in (a deliberate new
   * command: no remainder is kept).
   */
  const voiceInterrupt = () => {
    if (phaseRef.current !== "speaking") return;
    vlog("voice activity: interrupting the reply");
    const partial = inProgressRef.current;
    const remainder: string[] = [];
    if (partial?.text) {
      const cut = Math.min(Math.max(partial.spoken, 0), partial.text.length);
      const rest = partial.text.slice(cut).trim();
      if (rest) remainder.push(rest);
    }
    remainder.push(...queueRef.current);
    resumeRef.current = remainder.length ? remainder : null;
    cancelSpeech();
    // Keep the mic warm for the sentence the user started: arm a follow-up
    // window without a wake word so their words are collected right away.
    const fu = Math.max(cfgRef.current.followUpSeconds, 8);
    followUpUntilRef.current = Date.now() + fu * 1000;
    armedRef.current = true;
    needsWakeSliceRef.current = false;
    postSpeechDeafUntilRef.current = 0; // the user is talking NOW; do not deafen
    accRef.current = emptyResultSnapshot(lastResultLenRef.current); // drop TTS echo
    pendingCommand.current = "";
    clearFollowUpLangTimer();
    followUpLangTimer.current = setTimeout(() => {
      if (!armedRef.current) return;
      armedRef.current = false;
      vlog("interrupt follow-up window closed");
      syncRecognitionLanguage();
    }, fu * 1000);
    setPhase("awake");
    syncRecognitionLanguage();
    sweepIdleAwait();
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

  /* Collect one chunk of a spoken command. Every onresult while we are in a
     collecting state runs here: it merges finalized chunks + the live interim,
     re-slices the wake word for awake-mode commands, refreshes the pending
     text and (re-)arms the quiet-period endpoint. */
  const collectCommand = (e: any) => {
    accRef.current = accumulateResults(e.results, accRef.current);
    let text = commandText(accRef.current);
    // The acknowledgement is spoken while the recognizer is live, so drop it
    // from what the microphone heard back before slicing off the wake word.
    const heard = applyAckEcho(text);
    if (heard === null) {
      setLastHeard(`… ${text}`);
      return;
    }
    text = heard;
    if (needsWakeSliceRef.current) {
      text = sliceAfterLastWake(text, cfgRef.current.wakeWord, cfgRef.current.responseLanguage);
    } else if (!text) {
      // A silently-restarted recognition session has an empty result list;
      // keep whatever we collected before the restart so it can still fire.
      text = pendingCommand.current;
    }
    setLastHeard(`… ${text}`);
    if (!text) {
      pendingCommand.current = "";
      return;
    }
    if (isWakeWordFragment(text, cfgRef.current.wakeWord, cfgRef.current.responseLanguage)) {
      return; // still a partial wake word; wait for a real command
    }
    pendingCommand.current = text;
    if (commandTimer.current) clearTimeout(commandTimer.current);
    commandTimer.current = setTimeout(() => {
      commandTimer.current = null;
      firePendingCommand();
    }, ENDPOINT_SILENCE_MS);
  };

  const firePendingCommand = () => {
    const text = pendingCommand.current;
    pendingCommand.current = "";
    if (!text) return;
    if (phaseRef.current === "awake") {
      fireCommand(text);
      return;
    }
    if (
      phaseRef.current === "standby" &&
      armedRef.current &&
      Date.now() <= followUpUntilRef.current
    ) {
      fireCommand(text);
    }
  };

  const wakeSlow = (e: any, ww: string) => {
    commandEpoch.current += 1;
    vlog("wakeSlow:", utteranceText(e));
    cfgRef.current.onPhase("awake");
    setPhase("awake");
    armedRef.current = true;
    clearFollowUpLangTimer();
    beep();
    speakAck();
    cfgRef.current.onWake?.();
    syncRecognitionLanguage();
    // Start collecting from the result that contains the wake word; later
    // chunks (final or interim) merge in. The wake word itself is removed by
    // sliceAfterLastWake when the command is assembled.
    const len = e?.results?.length ?? 0;
    needsWakeSliceRef.current = true;
    accRef.current = emptyResultSnapshot(Math.max(0, len - 1));
    lastResultLenRef.current = len;
    collectCommand(e);
    sweepIdleAwait();
  };

  const onResult = (e: any) => {
    lastResultAtRef.current = Date.now();
    lastResultLenRef.current = e?.results?.length ?? 0;
    const text = utteranceText(e);
    const final = isFinal(e);
    setLastHeard(`${final ? "✓" : "…"} ${text}`);
    const ww = cfgRef.current.wakeWord.toLowerCase();
    const language = cfgRef.current.responseLanguage;
    const re = wakePattern(ww, language);
    const low = text.toLowerCase();
    const phase = phaseRef.current;

    /* A ringing reminder alarm answers to a stop phrase directly, with or
       without the wake word ("Apex stop"), in any phase. It is checked before
       the phase machine because the alarm must be stoppable at any moment and
       because the alarm audio itself can make wake-word detection less
       reliable. The caller only enables this while an alarm is actually
       ringing, so a bare "stop" is otherwise an ordinary command. */
    if (cfgRef.current.alarmActive && text) {
      const withoutWake = sliceAfterLastWake(text, ww, language);
      if (isAlarmStopPhrase(text) || isAlarmStopPhrase(withoutWake)) {
        vlog("alarm stop phrase heard:", text);
        cfgRef.current.onAlarmStop?.();
        accRef.current = emptyResultSnapshot(lastResultLenRef.current);
        pendingCommand.current = "";
        if (commandTimer.current) {
          clearTimeout(commandTimer.current);
          commandTimer.current = null;
        }
        return;
      }
    }

    // barge-in: wake word cuts off speech / thinking
    if ((phase === "speaking" || phase === "thinking") && re.test(low)) {
      cancelSpeech();
      wakeSlow(e, ww);
      return;
    }

    if (phase === "awake") {
      if (isSleepCommand(text, language)) {
        sleepVoice();
        return;
      }
      collectCommand(e);
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
      wakeSlow(e, ww);
      return;
    }

    // follow-up window (armed, no wake word)
    if (armedRef.current) {
      vlog("follow-up candidate:", text, "final:", final, "ms left:", followUpUntilRef.current - Date.now());
      if (isWakeOnlyText(text, ww, language)) {
        wakeSlow(e, ww);
        return;
      }
      if (isSleepCommand(text, language)) {
        sleepVoice();
        return;
      }
      if (Date.now() <= followUpUntilRef.current) {
        collectCommand(e);
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
    lastResultLenRef.current = 0;
    accRef.current = emptyResultSnapshot(0);
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
    forgetAck(true);
    lastWakeAckRef.current = null;
    pendingCommand.current = "";
    resumeRef.current = null;
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

  /*
   * Interruption detection: while a reply is being spoken, a second mic stream
   * (with echo cancellation, so the assistant's own voice is largely removed)
   * feeds an analyser. When the RMS level stays above the sensitivity-derived
   * threshold for a few frames the speech is cut. The loop runs whenever voice
   * is on AND interruptions are enabled, but only ever acts in the "speaking"
   * phase; the analyser's own stream is closed on disable.
   */
  useEffect(() => {
    if (typeof window === "undefined" || typeof navigator === "undefined") return;
    if (!active || !cfgRef.current.interruptEnabled) {
      vadRef.current?.stop?.();
      vadRef.current = null;
      vadStateRef.current = initialVadState();
      reportVad("off");
      return;
    }
    if (vadRef.current) return; // already running

    let ctx: AudioContext | null = null;
    let analyser: AnalyserNode | null = null;
    let stream: MediaStream | null = null;
    let raf = 0;
    let poll: ReturnType<typeof setTimeout> | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;
    let buffer: Float32Array<ArrayBuffer> | null = null;
    let attempts = 0;
    let resumedAt = 0;
    let startedAt = 0;
    let live = false;
    let rmsLogAt = 0;
    let warned = false;
    let handle: { stop: () => void } | null = null;

    const warn = (msg: string) => {
      if (warned) return;
      warned = true;
      // Deliberately not behind the debug flag: a dead interrupt is otherwise
      // indistinguishable from a working one from the outside.
      console.warn("[voice] interruption:", msg);
    };

    // A context created outside a user gesture (page load, background tab) or
    // suspended by the browser stays suspended, and a suspended analyser reads
    // digital silence: the RMS never reaches the threshold and the reply is
    // never cut. Re-arm it on creation, on every gesture, on focus/visibility
    // and on statechange until it is really running.
    const ensureRunning = () => {
      if (stopped || !ctx || ctx.state === "running") return;
      const now = Date.now();
      if (now - resumedAt < 300) return;
      resumedAt = now;
      void ctx.resume().catch(() => {});
    };
    const onWake = () => ensureRunning();
    const detach = () => {
      window.removeEventListener("pointerdown", onWake);
      window.removeEventListener("keydown", onWake);
      window.removeEventListener("focus", onWake);
      document.removeEventListener("visibilitychange", onWake);
    };

    const stop = () => {
      stopped = true;
      if (raf) cancelAnimationFrame(raf);
      if (poll) clearTimeout(poll);
      if (retry) clearTimeout(retry);
      detach();
      try {
        stream?.getTracks().forEach((t) => t.stop());
      } catch {}
      try {
        void ctx?.close();
      } catch {}
      if (handle && vadRef.current === handle) vadRef.current = null;
    };

    const tick = () => {
      if (stopped || !ctx || !analyser || !buffer) return;
      if (ctx.state !== "running") {
        ensureRunning();
        if (startedAt && Date.now() - startedAt > 1500) {
          reportVad("blocked", "audio blocked - click the page once");
        }
        if (!stopped) poll = setTimeout(tick, 200);
        return;
      }
      if (!live) {
        live = true;
        reportVad("live");
      }
      analyser.getFloatTimeDomainData(buffer);
      let sum = 0;
      for (let i = 0; i < buffer.length; i++) sum += buffer[i] * buffer[i];
      const rms = Math.sqrt(sum / buffer.length);
      if (phaseRef.current === "speaking") {
        // Give echo cancellation a moment at the start of the reply so the
        // assistant's opening words cannot cut its own speech off.
        if (Date.now() - speakSinceRef.current > 400) {
          const threshold = sensitivityToThreshold(cfgRef.current.interruptSensitivity);
          if (Date.now() - rmsLogAt > 1000) {
            rmsLogAt = Date.now();
            console.log("[voice] interruption rms", rms.toFixed(3), "threshold", threshold);
          }
          const { state, fire } = vadStep(vadStateRef.current, rms, threshold);
          vadStateRef.current = state;
          if (fire) {
            vlog("interrupt fired, rms=", rms.toFixed(3), "threshold=", threshold);
            vadStateRef.current = initialVadState();
            voiceInterrupt();
          }
        }
      } else {
        vadStateRef.current = initialVadState();
      }
      if (!stopped) raf = requestAnimationFrame(tick);
    };

    const start = () => {
      const AC = (window as any).AudioContext ?? (window as any).webkitAudioContext;
      const media = typeof navigator !== "undefined" ? navigator.mediaDevices : undefined;
      if (!AC || !media?.getUserMedia) {
        reportVad("error", "this browser does not expose the microphone");
        warn("AudioContext/getUserMedia unavailable");
        stop();
        return;
      }
      reportVad("starting", attempts ? `microphone busy - retry ${attempts} of 3` : "");
      void media
        // AEC is what separates the user's voice from the assistant's voice
        // coming out of the same speakers.
        .getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
        .then((m: MediaStream) => {
          if (stopped) {
            m.getTracks().forEach((t) => t.stop());
            return;
          }
          stream = m;
          const audioCtx: AudioContext = new AC();
          ctx = audioCtx;
          audioCtx.onstatechange = () => ensureRunning();
          const src = audioCtx.createMediaStreamSource(m);
          const node = audioCtx.createAnalyser();
          analyser = node;
          node.fftSize = 2048;
          node.smoothingTimeConstant = 0.1;
          src.connect(node);
          buffer = new Float32Array(node.fftSize);
          startedAt = Date.now();
          ensureRunning();
          if (audioCtx.state === "running" && !live) {
            live = true;
            reportVad("live");
          }
          if (!stopped) raf = requestAnimationFrame(tick);
        })
        .catch((err: unknown) => {
          const message = err instanceof Error ? err.message : String(err);
          warn(`microphone unavailable (${message})`);
          if (stopped) return;
          attempts += 1;
          if (attempts <= 3) {
            reportVad("starting", `microphone busy - retry ${attempts} of 3`);
            retry = setTimeout(start, 750 * attempts * attempts);
          } else {
            reportVad("error", `microphone unavailable (${message})`);
            stop();
          }
        });
    };

    handle = { stop };
    vadRef.current = handle;
    window.addEventListener("pointerdown", onWake);
    window.addEventListener("keydown", onWake);
    window.addEventListener("focus", onWake);
    document.addEventListener("visibilitychange", onWake);
    start();
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, interruptEnabled]);

  return {
    supported: SRClassAvailable(),
    active,
    error,
    speak,
    cancelSpeech,
    isSpeaking: segmentsLeft > 0,
    forceAwake,
    lastHeard,
    vadStatus,
    vadNote,
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
