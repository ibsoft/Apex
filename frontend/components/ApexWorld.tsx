"use client";

/** Main screen: animated backdrop, interactive Apex core, and status bar. */

import { useEffect, useRef, useState } from "react";
import ApexHeroOrb, { type OrbState } from "./ApexHeroOrb";
import ShaderBackgroundJs from "./ShaderBackground";
import OrbStatusBar from "./OrbStatusBar";

const ShaderBackground = ShaderBackgroundJs as unknown as React.ComponentType<{
  opacity?: number; voiceActive?: boolean; gold?: boolean;
}>;

/* ── The world ── */
export default function ApexWorld({
  state: controlled,
  onTap,
}: { state?: OrbState; onTap?: () => void } = {}) {
  const [reduced, setReduced] = useState(false);

  // Uncontrolled: a tap cycles idle → thinking → speaking → idle on its own
  // timer (the original site behavior). Controlled: the parent (chat/voice
  // provider) drives the state and receives taps.
  const [showState, setShowState] = useState<OrbState>("idle");
  const showTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const orbState: OrbState = controlled ?? showState;

  const boost = () => {
    if (onTap) {
      onTap();
      return;
    }
    const next: OrbState = showState === "idle" ? "thinking" : showState === "thinking" ? "speaking" : "idle";
    setShowState(next);
    if (showTimer.current) clearTimeout(showTimer.current);
    showTimer.current = setTimeout(() => setShowState("idle"), 8000);
  };
  useEffect(() => () => { if (showTimer.current) clearTimeout(showTimer.current); }, []);

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const apply = () => setReduced(mq.matches);
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);

  return (
    <div style={{ position: "absolute", inset: 0, overflow: "hidden", userSelect: "none" }}>
      {/* backdrop - the app's EXACT stack (Chat.jsx dark mode): base radial page
          gradient, waves at 0.12, the cyan breathing glow behind the orb, and the
          dark moat disc directly behind the particle cloud that makes it pop. */}
      <div aria-hidden="true" style={{
        position: "absolute", inset: 0,
        background: "radial-gradient(ellipse 95% 88% at 50% 42%, #122c43 0%, #0c1d30 38%, #07111f 72%, #050b14 100%)",
      }} />

      {/* background waves - the app's WebGL shader at the app's opacity */}
      {!reduced && (
        <div aria-hidden="true" style={{ position: "absolute", inset: 0, zIndex: 0 }}>
          <ShaderBackground opacity={0.12} voiceActive={orbState === "speaking"} gold={false} />
        </div>
      )}

      {/* cyan LIGHT-CAST - app copy exactly: mixBlendMode screen (only ever LIFTS the
          navy, never darkens), brightens while speaking. The app has NO dark moat disc
          in dark mode - that layer is its light-theme "reactor well" only. */}
      <div aria-hidden="true" style={{
        position: "absolute", inset: 0, zIndex: 1, pointerEvents: "none", mixBlendMode: "screen",
        background: `radial-gradient(circle at 50% 42%, rgba(13,210,255,${orbState === "speaking" ? 0.30 : 0.18}) 0%, rgba(13,170,228,0.08) 30%, rgba(8,17,31,0) 62%)`,
        transition: "background 0.6s ease",
      }} />

      {/* Central orb with a separate tap target. */}
      <div style={{ position: "absolute", left: "50%", top: "50%", width: "min(560px, 58vw)", height: "min(500px, 56vw, 70vh)", transform: "translate(-50%, -50%)", zIndex: 3, pointerEvents: "none" }}>
        <ApexHeroOrb state={orbState} interactive={false} />
      </div>

      {/* Central tap disc */}
      <div
        role="button"
        tabIndex={0}
        aria-label="Apex core - tap to energize"
        onClick={boost}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); boost(); } }}
        onMouseDown={(e) => e.preventDefault()}
        style={{
          position: "absolute", left: "50%", top: "50%", transform: "translate(-50%, -50%)",
          width: "min(340px, 36vw)", height: "min(340px, 36vw)", borderRadius: "50%",
          zIndex: 4, cursor: "pointer", background: "transparent", border: "none", userSelect: "none",
        }}
      />

      {/* equalizer + STANDBY cluster */}
      <OrbStatusBar state={orbState} />
    </div>
  );
}
