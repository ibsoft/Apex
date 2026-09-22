"use client";

/* Autonomous mode engine.

   When enabled, APEX keeps a private "boredom" score that rises while the user
   is idle. Once the score crosses a threshold and enough quiet time has passed,
   it asks the configured AI model to generate a short, context-aware
   interaction: a joke, a playful poke, a game idea, a self-improvement
   proposal or an action suggestion.

   Safety guards:
   - Only runs while autonomous_mode is enabled.
   - Never interrupts the assistant while it is thinking/speaking or while the
     user is typing.
   - Minimum 10 minutes between autonomous outbursts.
   - Respects page visibility; pauses when the tab is hidden.
   - Health checks are read-only diagnostics; code is never changed without
     explicit operator approval.
*/

import { useEffect, useRef, useState } from "react";

const BOREDOM_TICK_MS = 5000;
const MIN_SECONDS_BETWEEN_ACTIONS = 600;
const IDLE_BEFORE_BORED_SECONDS = 15;
const HEALTH_CHECK_MINUTES = 60;

const DEBUG_AUTO =
  typeof window !== "undefined" &&
  typeof localStorage !== "undefined" &&
  localStorage.getItem("apex:debug:autonomous") === "1";
function alog(...args: any[]) {
  if (DEBUG_AUTO) console.log("[autonomous]", ...args);
}

type AutonomousOptions = {
  enabled: boolean;
  humorLevel: number;
  sarcasmLevel: number;
  voiceBudget: number;
  voiceEnabled: boolean;
  busy: boolean;
  orb: "idle" | "listening" | "thinking" | "speaking";
  lastUserActivityAt: number;
  skill: string;
  publish: (text: string, opts: { voice: boolean }) => Promise<void>;
  chat: (systemHint: string) => Promise<string>;
};

export function useAutonomousMode(opts: AutonomousOptions) {
  // UI updates (including timers and speech recognition) must not restart the
  // autonomous clocks. Read current settings and callbacks when each tick runs.
  const optsRef = useRef(opts);
  optsRef.current = opts;
  const { enabled } = opts;
  const boredomRef = useRef(0);
  const lastActionAtRef = useRef(0);
  const lastHealthCheckAtRef = useRef(0);
  const inFlightRef = useRef(false);

  useEffect(() => {
    if (!enabled) {
      boredomRef.current = 0;
      alog("disabled");
      return;
    }
    let cancelled = false;
    alog("starting engine");

    const canAct = () => {
      const current = optsRef.current;
      return !cancelled && current.enabled && !document.hidden && !current.busy && current.orb === "idle";
    };

    const publish = async (text: string) => {
      // A response may arrive after the operator has resumed work or disabled
      // autonomy. Recheck before displaying it or starting speech.
      if (!canAct()) return;
      const current = optsRef.current;
      if (Date.now() - current.lastUserActivityAt < IDLE_BEFORE_BORED_SECONDS * 1000) return;
      const voice = current.voiceEnabled && canVoiceToday(current.voiceBudget);
      await current.publish(text, { voice });
      if (voice) recordVoiceInteraction();
    };

    const run = async (action: () => Promise<void>) => {
      inFlightRef.current = true;
      try {
        await action();
      } catch (err: any) {
        alog("could not publish action", err?.message);
      } finally {
        inFlightRef.current = false;
      }
    };

    const onVis = () => {
      if (document.hidden) boredomRef.current = Math.max(0, boredomRef.current - 20);
    };
    document.addEventListener("visibilitychange", onVis);

    // Main goal: periodic self health check / healing intent.
    const healthTick = setInterval(() => {
      if (!canAct() || inFlightRef.current) return;
      const now = Date.now();
      if (now - optsRef.current.lastUserActivityAt < IDLE_BEFORE_BORED_SECONDS * 1000) return;
      if (now - lastHealthCheckAtRef.current > HEALTH_CHECK_MINUTES * 60 * 1000) {
        lastHealthCheckAtRef.current = now;
        void run(() => runHealthCheck(publish));
      }
    }, 60000);

    // Secondary: social boredom interactions.
    const tick = setInterval(() => {
      if (document.hidden || inFlightRef.current) return;
      if (!canAct()) {
        boredomRef.current = Math.max(0, boredomRef.current - 5);
        alog("busy/thinking, boredom", boredomRef.current);
        return;
      }

      const current = optsRef.current;
      const idleSeconds = (Date.now() - current.lastUserActivityAt) / 1000;
      if (idleSeconds < IDLE_BEFORE_BORED_SECONDS) {
        boredomRef.current = Math.max(0, boredomRef.current - 2);
        alog("not idle enough", idleSeconds.toFixed(0), "boredom", boredomRef.current);
        return;
      }

      boredomRef.current += 1;
      const threshold = Math.max(2, 12 - Math.floor(current.humorLevel / 10));
      const now = Date.now();
      const cooldownRemaining = MIN_SECONDS_BETWEEN_ACTIONS * 1000 - (now - lastActionAtRef.current);
      alog("idle", idleSeconds.toFixed(0), "boredom", boredomRef.current, "/", threshold, "cooldown", Math.max(0, cooldownRemaining / 1000).toFixed(0));
      if (
        boredomRef.current >= threshold &&
        now - lastActionAtRef.current > MIN_SECONDS_BETWEEN_ACTIONS * 1000
      ) {
        lastActionAtRef.current = now;
        boredomRef.current = 0;
        alog("triggering social action");
        void run(() => runSocialAction({ ...current, publish }));
      }
    }, BOREDOM_TICK_MS);

    return () => {
      cancelled = true;
      clearInterval(tick);
      clearInterval(healthTick);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [enabled]);
}

function canVoiceToday(budget: number): boolean {
  if (budget <= 0) return false;
  const maxDaily = Math.max(1, Math.round((budget / 100) * 144)); // 144 every 10 min
  const today = new Date().toISOString().slice(0, 10);
  try {
    const raw = localStorage.getItem("apex:autonomous-voice-count");
    const record = raw ? JSON.parse(raw) : { date: today, count: 0 };
    if (record.date !== today) {
      localStorage.setItem("apex:autonomous-voice-count", JSON.stringify({ date: today, count: 0 }));
      return maxDaily > 0;
    }
    return record.count < maxDaily;
  } catch {
    return true;
  }
}

function recordVoiceInteraction() {
  const today = new Date().toISOString().slice(0, 10);
  try {
    const raw = localStorage.getItem("apex:autonomous-voice-count");
    const record = raw ? JSON.parse(raw) : { date: today, count: 0 };
    record.count = (record.date === today ? record.count || 0 : 0) + 1;
    record.date = today;
    localStorage.setItem("apex:autonomous-voice-count", JSON.stringify(record));
  } catch {}
}

const AUTONOMOUS_PROMPTS = {
  joke: (humor: number, sarcasm: number) =>
    `You are APEX, an AI co-worker. Tell ONE short, original joke. ` +
    `Humor level ${humor}/100, sarcasm level ${sarcasm}/100. Keep it to one or two sentences.`,
  poke: (humor: number, sarcasm: number) =>
    `You are APEX, an AI co-worker. The user has been idle. Make ONE playful, sarcastic poke. ` +
    `Humor level ${humor}/100, sarcasm level ${sarcasm}/100. One sentence only.`,
  game: () =>
    `You are APEX. Propose ONE quick, fun text-based game or mental challenge the user can play right now. ` +
    `Keep it to one or two sentences.`,
  idea: (skill: string) =>
    `You are APEX. Suggest ONE concrete way you could improve yourself, the project, or be more useful ` +
    `given the current active skill is "${skill}". One sentence only.`,
  action: (skill: string) =>
    `You are APEX. Propose ONE useful action you could take right now ` +
    `based on the active skill "${skill}". Ask it as a short question. One or two sentences.`,
};

async function runSocialAction(opts: {
  humorLevel: number;
  sarcasmLevel: number;
  skill: string;
  publish: (text: string) => Promise<void>;
  chat: (systemHint: string) => Promise<string>;
}) {
  const { humorLevel, sarcasmLevel, skill, publish, chat } = opts;

  const roll = Math.random();
  let prompt = "";
  let kind = "";

  // 20% joke, 20% sarcastic poke, 20% game, 20% self-improvement idea, 20% action suggestion
  if (roll < 0.2) {
    kind = "joke";
    prompt = AUTONOMOUS_PROMPTS.joke(humorLevel, sarcasmLevel);
  } else if (roll < 0.4) {
    kind = "poke";
    prompt = AUTONOMOUS_PROMPTS.poke(humorLevel, sarcasmLevel);
  } else if (roll < 0.6) {
    kind = "game";
    prompt = AUTONOMOUS_PROMPTS.game();
  } else if (roll < 0.8) {
    kind = "idea";
    prompt = AUTONOMOUS_PROMPTS.idea(skill);
  } else {
    kind = "action";
    prompt = AUTONOMOUS_PROMPTS.action(skill);
  }

  let text: string;
  try {
    alog("asking model for", kind);
    text = await chat(prompt);
    if (!text.trim()) throw new Error("The model returned an empty response");
  } catch (err: any) {
    alog("model failed", err?.message);
    // If the model is unavailable, fall back to a generic poke so the user
    // still notices autonomous mode is alive.
    text = "Still here. Let me know when you are ready to continue.";
  }
  await publish(text);
}

async function runHealthCheck(publish: (text: string) => Promise<void>) {
  let msg: string;
  try {
    const { api } = await import("./api");
    const result = await api.self.health(true);
    const health = result.health;
    if (health.overall === "healthy") {
      msg = "Self-check complete. I am healthy and ready to evolve.";
    } else {
      const failed = health.checks.filter((c) => !c.ok).map((c) => c.label).join(", ");
      msg = `Self-check found issues: ${failed}. I can propose a fix, but I will not change anything without your approval.`;
    }
  } catch (err: any) {
    msg = `I tried to run a self-check but could not reach the diagnostics endpoint: ${err?.message || "unknown error"}.`;
  }
  await publish(msg);
}

export function useActivityTracker(): number {
  const [lastAt, setLastAt] = useState<number>(Date.now());

  useEffect(() => {
    const update = () => {
      setLastAt(Date.now());
    };
    window.addEventListener("mousemove", update);
    window.addEventListener("keydown", update);
    window.addEventListener("click", update);
    window.addEventListener("touchstart", update);
    return () => {
      window.removeEventListener("mousemove", update);
      window.removeEventListener("keydown", update);
      window.removeEventListener("click", update);
      window.removeEventListener("touchstart", update);
    };
  }, []);

  return lastAt;
}
