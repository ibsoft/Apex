"use client";

/* The level meter under the music player.
 *
 * It is decoration, and it says so: cross-origin YouTube iframes expose no
 * audio graph, so nothing here can be a measurement of what is playing. What it
 * does is look alive in the way a meter does - the level math is
 * `vuLevels(phase, bands)` in `lib/media.ts`, a pure function the tests cover;
 * this file only drives the phase off a clock and paints the bars.
 */

import { useEffect, useRef } from "react";
import { vuLevels } from "../lib/media";

type Props = {
  /** Animate (a track is playing). Paused and idle hold a still, low meter. */
  running: boolean;
  bands?: number;
  height?: number;
};

export default function VuMeter({ running, bands = 32, height = 46 }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const runningRef = useRef(running);
  runningRef.current = running;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    let raf = 0;
    let phase = 0;
    let last = performance.now();
    let alive = true;

    const draw = (now: number) => {
      if (!alive) return;
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      if (runningRef.current) phase += dt * 6.5;

      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const cssW = canvas.clientWidth || 320;
      const cssH = canvas.clientHeight || height;
      if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
        canvas.width = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssW, cssH);

      const levels = vuLevels(runningRef.current ? phase : 0, bands);
      const gap = 2;
      const barW = Math.max(2, (cssW - gap * (bands - 1)) / bands);
      for (let i = 0; i < bands; i++) {
        const level = levels[i];
        const h = Math.max(3, level * (cssH - 4));
        const x = i * (barW + gap);
        const y = cssH - h;
        const gradient = ctx.createLinearGradient(0, cssH, 0, y);
        gradient.addColorStop(0, "#00e5ff");
        gradient.addColorStop(0.62, "#46f2b4");
        gradient.addColorStop(0.85, "#f5a623");
        gradient.addColorStop(1, "#ff4d6d");
        ctx.fillStyle = gradient;
        ctx.globalAlpha = runningRef.current ? 0.92 : 0.4;
        ctx.fillRect(x, y, barW, h);
        // The cap that makes it read as hardware rather than a bar chart.
        ctx.globalAlpha = 1;
        ctx.fillStyle = "rgba(0,229,255,0.55)";
        ctx.fillRect(x, Math.max(1, y - 2), barW, 2);
      }
      raf = requestAnimationFrame(draw);
    };

    raf = requestAnimationFrame(draw);
    return () => {
      alive = false;
      cancelAnimationFrame(raf);
    };
  }, [bands, height]);

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      style={{ width: "100%", height, display: "block" }}
    />
  );
}
