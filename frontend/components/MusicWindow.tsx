"use client";

/* The music player window: artwork, transport, queue and the level meter.
 *
 * The embedded player stays visible and interactive so browser permission,
 * sign-in and playback messages can be handled directly. The queue and
 * transport state live in the provider's media state.
 */

import { useEffect, useRef, useState, type CSSProperties, type MouseEvent } from "react";
import { useApex } from "./ApexProvider";
import {
  currentItem,
  formatMediaDuration,
  isTrackError,
  localize,
  youtubeErrorKind,
  type MediaApp,
} from "../lib/media";
import YoutubePlayer, { type MediaFailure, type YoutubePlayerHandle } from "./YoutubePlayer";
import MediaFailureBanner from "./MediaFailureBanner";
import VuMeter from "./VuMeter";

const APP: MediaApp = "music";

const C = {
  cyan: "#00e5ff",
  gold: "#f5a623",
  line: "rgba(0,229,255,0.16)",
  lineGold: "rgba(245,166,35,0.28)",
  text: "rgba(235,244,255,0.92)",
  dim: "rgba(170,192,215,0.55)",
};

const MONO = "var(--font-mono, monospace)";

export default function MusicWindow({ focused: _focused }: { windowId: string; focused: boolean }) {
  const a = useApex();
  const language = a.settings?.response_language ?? a.config?.response_language ?? "en";
  const media = a.media?.app === APP ? a.media : null;
  const item = currentItem(media);
  const playerRef = useRef<YoutubePlayerHandle | null>(null);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [queueOpen, setQueueOpen] = useState(true);
  const [muted, setMuted] = useState(false);
  const [actuallyPlaying, setActuallyPlaying] = useState(false);
  const [failure, setFailure] = useState<MediaFailure | null>(null);
  const [blocked, setBlocked] = useState(false);
  /* Bumping the key remounts the iframe: the only cure for a player that failed
     to load, and the retry button is the operator asking for exactly that. Ids
     that already failed stay remembered, so a skip cannot lap the queue. */
  const [retryKey, setRetryKey] = useState(0);

  /* Declared before the early return below so the sound effect can depend on it. */
  const playing = Boolean(media && item && !media.paused && !failure);
  const audible = playing && actuallyPlaying && !blocked && !muted;

  const act = (action: Parameters<typeof a.mediaAction>[1], index?: number) => a.mediaAction(APP, action, index);

  /* The browser can hold the iframe muted even though the state says playing -
     autoplay before any gesture starts silent, and this window hides the only
     unmute control YouTube offers. So read the player instead of assuming, and
     the sound button tells the truth about what is actually coming out. */
  useEffect(() => {
    if (!playing) return;
    const id = window.setInterval(() => {
      const player = playerRef.current;
      if (!player) return;
      try {
        const silent = player.isMuted();
        const active = player.getPlayerState() === 1;
        setMuted(silent);
        setActuallyPlaying(active);
        // A click in the iframe never bubbles to our document gesture handler.
        if (active && !silent) setBlocked(false);
      } catch { /* not ready yet */ }
    }, 700);
    return () => window.clearInterval(id);
  }, [playing, item?.id]);

  const toggleSound = () => {
    const player = playerRef.current;
    if (!player) return;
    try {
      if (player.isMuted()) {
        player.setVolume(100);
        player.unMute();
        setMuted(false);
      } else {
        player.mute();
        setMuted(true);
      }
    } catch { /* not ready yet */ }
  };

  /* A new track clears the refusal: whatever went wrong belonged to the last
     one, and leaving the banner up would blame the wrong song. */
  useEffect(() => {
    setFailure(null);
    setBlocked(false);
    setActuallyPlaying(false);
    setTime(0);
    setDuration(0);
  }, [item?.id, media?.seq]);

  /* Autoplay was blocked, so the track is waiting for a gesture. Unmuted playback needs
     the page to have been interacted with, and a click or keypress anywhere in
     APEX is exactly that - so the sound comes back on the operator's very next
     gesture, wherever it lands, without them having to find the 🔊 button. */
  useEffect(() => {
    if (!blocked) return;
    const release = () => { setBlocked(false); playNow(); };
    document.addEventListener("pointerdown", release, { once: true });
    document.addEventListener("keydown", release, { once: true });
    return () => {
      document.removeEventListener("pointerdown", release);
      document.removeEventListener("keydown", release);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blocked]);

  const retry = () => {
    setBlocked(false);
    // A refused *embed* needs a fresh iframe; a cued-but-blocked track does
    // not, and remounting one would autoplay into the same block. A click is
    // the gesture the browser asked for, so start what is already loaded.
    if (failure) {
      setFailure(null);
      setRetryKey((key) => key + 1);
      return;
    }
    playNow();
  };

  /* The operator's click IS the permission to make noise: drive the player
     that is already there rather than rebuilding it. */
  const playNow = () => {
    const player = playerRef.current;
    if (!player) { setRetryKey((key) => key + 1); return; }
    try {
      player.setVolume(100);
      player.unMute();
      player.playVideo();
      setMuted(false);
    } catch { setRetryKey((key) => key + 1); }
  };

  const onTime = (current: number, total: number) => {
    setTime(current);
    setDuration((previous) => (total > 0 && total !== previous ? total : previous));
  };

  const seek = (event: MouseEvent<HTMLDivElement>) => {
    const player = playerRef.current;
    const total = duration || 0;
    if (!player || !total) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    player.seekTo(ratio * total, true);
    setTime(ratio * total);
  };

  const idle = (
    <div style={{
      flex: 1, display: "flex", flexDirection: "column", gap: 10,
      alignItems: "center", justifyContent: "center", textAlign: "center", padding: 18,
    }}>
      <div style={{ fontSize: 40, lineHeight: 1, opacity: 0.7 }}>♪</div>
      <div style={{ fontFamily: MONO, fontSize: 11, letterSpacing: "0.22em", color: C.gold }}>
        {localize(language, "NOTHING PLAYING", "ΚΑΜΙΑ ΑΝΑΠΑΡΑΓΩΓΗ")}
      </div>
      <div style={{ fontFamily: MONO, fontSize: 11, color: C.dim, maxWidth: 320 }}>
        {localize(language, "Say “play some jazz” or “βάλε μουσική”.", "Πείτε «βάλε μουσική» ή «παίξε τζαζ».")}
      </div>
    </div>
  );

  if (!media || !item) {
    return (
      <div style={{ flex: 1, display: "flex", minHeight: 0, background: "rgba(6,10,20,0.35)" }}>
        {idle}
      </div>
    );
  }

  const exhausted = media.items.every(entry => media.unavailableIds?.includes(entry.id));
  const total = duration || item.duration || 0;
  const percent = total > 0 ? Math.min(100, Math.max(0, (time / total) * 100)) : 0;

  /* A refusal is reported, not swallowed: a per-video one (embedding disabled,
     video removed) moves on to the next item that has not already failed, while
     an identity failure keeps the card - skipping would only fail again. */
  const onFail = (info: MediaFailure) => {
    setFailure(info);
    setBlocked(false);
    if (isTrackError(youtubeErrorKind(info.code))) {
      act("reject", media.index);
    }
  };

  return (
    <div style={{
      flex: 1, display: "flex", flexDirection: "column", gap: 10, minHeight: 0,
      padding: "12px 14px", background: "rgba(6,10,20,0.35)", position: "relative",
    }}>
      <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
        {/* Keep native playback controls reachable when autoplay is refused. */}
        <div style={{ width: 240, height: 200, flexShrink: 0 }}>
          <YoutubePlayer
            key={retryKey}
            videoId={item.id}
            seq={media.seq}
            playing={playing}
            controls={1}
            onEnded={() => act("next")}
            onPause={() => act("pause")}
            onResume={() => act("resume")}
            onTime={onTime}
            onPlayer={(player) => { playerRef.current = player; }}
            onFail={onFail}
            onBlocked={() => setBlocked(true)}
            failText={localize(language, "PLAYER OFFLINE", "Ο ΑΝΑΠΑΡΑΓΩΓΟΣ ΔΕΝ ΕΙΝΑΙ ΔΙΑΘΕΣΙΜΟΣ")}
          />
        </div>

        <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 6 }}>
          <span style={{
            fontFamily: MONO, fontSize: 9.5, letterSpacing: "0.24em", color: C.gold,
          }}>
            {audible ? localize(language, "NOW PLAYING", "ΤΩΡΑ ΑΚΟΥΓΕΤΑΙ") : playing ? localize(language, muted ? "MUTED" : "WAITING FOR PLAYBACK", muted ? "ΣΕ ΣΙΓΑΣΗ" : "ΑΝΑΜΟΝΗ ΑΝΑΠΑΡΑΓΩΓΗΣ") : localize(language, "PAUSED", "ΣΕ ΠΑΥΣΗ")}
          </span>
          <div style={{
            fontFamily: MONO, fontSize: 14, lineHeight: 1.35, color: C.text,
            display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical",
            overflow: "hidden", wordBreak: "break-word",
          }}>
            {item.title}
          </div>
          <div style={{ fontFamily: MONO, fontSize: 11, color: C.dim, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {item.channel ?? ""}
          </div>
        </div>
      </div>

      {/* Seek bar: click to jump. It is the only place time is shown. */}
      <div>
        <div
          onClick={seek}
          role="presentation"
          style={{
            position: "relative", height: 6, borderRadius: 4, cursor: total ? "pointer" : "default",
            background: "rgba(0,229,255,0.12)", border: `1px solid ${C.line}`, overflow: "hidden",
          }}
        >
          <div style={{
            position: "absolute", inset: 0, width: `${percent}%`,
            background: `linear-gradient(90deg, ${C.cyan}55, ${C.gold})`,
          }} />
        </div>
        <div style={{
          display: "flex", justifyContent: "space-between", marginTop: 4,
          fontFamily: MONO, fontSize: 10, color: C.dim,
        }}>
          <span>{formatMediaDuration(time) || "0:00"}</span>
          <span>{formatMediaDuration(total)}</span>
        </div>
      </div>

      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 14 }}>
        <button onClick={() => act("previous")} aria-label={localize(language, "Previous track", "Προηγούμενο τραγούδι")} style={btnStyle}>
          ⏮
        </button>
        <button
          onClick={() => {
            setBlocked(false);
            // A refused player reads as playing in the state, so ▶ here means
            // "try again" rather than a resume that would change nothing; a
            // blocked one means "start what is cued", with this click as the
            // gesture the browser was waiting for.
            if (failure) { retry(); return; }
            if (!audible && !media.paused) { playNow(); return; }
            act(playing ? "pause" : "resume");
          }}
          disabled={exhausted}
          aria-label={localize(language, audible ? "Pause" : "Play", audible ? "Παύση" : "Αναπαραγωγή")}
          style={{ ...btnStyle, ...primaryBtnStyle }}
        >
          {audible ? "⏸" : "▶"}
        </button>
        <button onClick={() => act("next")} aria-label={localize(language, "Next track", "Επόμενο τραγούδι")} style={btnStyle}>
          ⏭
        </button>
        <button
          onClick={() => setQueueOpen((open) => !open)}
          aria-label={localize(language, "Toggle queue", "Εναλλαγή ουράς")}
          style={{ ...btnStyle, fontSize: 13, opacity: queueOpen ? 1 : 0.6 }}
        >
          ☰
        </button>
        {/* The reliable unmute: a click here is the gesture the browser asked
            for, so sound comes back even when autoplay started it muted. */}
        <button
          onClick={toggleSound}
          aria-label={localize(language, muted ? "Enable sound" : "Mute", muted ? "Ενεργοποίηση ήχου" : "Σίγαση")}
          title={localize(language, "Sound", "Ήχος")}
          style={{ ...btnStyle, fontSize: 13, opacity: muted ? 0.65 : 1 }}
        >
          {muted ? "🔇" : "🔊"}
        </button>
      </div>

      <MediaFailureBanner
        failure={failure}
        blocked={blocked}
        item={item}
        language={language}
        onRetry={retry}
      />

      <VuMeter running={audible} bands={34} height={44} />

      {queueOpen && media.items.length > 1 && (
        <div style={{
          flex: 1, minHeight: 0, overflowY: "auto", borderTop: `1px solid ${C.line}`,
          paddingTop: 6, display: "flex", flexDirection: "column", gap: 2,
        }}>
          <span style={{
            fontFamily: MONO, fontSize: 9, letterSpacing: "0.22em", color: C.dim,
            marginBottom: 2,
          }}>
            {localize(language, `QUEUE · ${media.items.length}`, `ΟΥΡΑ · ${media.items.length}`)}
          </span>
          {media.items.map((entry, i) => {
            const current = i === media.index;
            return (
              <button
                key={`${entry.id}:${i}`}
                disabled={media.unavailableIds?.includes(entry.id)}
                title={media.unavailableIds?.includes(entry.id) ? localize(language, "Skipped: unavailable in this player", "Παραλείφθηκε: μη διαθέσιμο") : undefined}
                onClick={() => act("select", i)}
                style={{
                  display: "flex", gap: 8, alignItems: "baseline", textAlign: "left",
                  background: current ? "rgba(245,166,35,0.10)" : "none",
                  border: "none", borderLeft: `2px solid ${current ? C.gold : "transparent"}`,
                  color: current ? C.text : C.dim, cursor: "pointer",
                  font: `11px ${MONO}`, padding: "4px 8px", borderRadius: 4,
                }}
              >
                <span style={{ color: current ? C.gold : "rgba(170,192,215,0.4)", minWidth: 18 }}>
                  {i + 1}
                </span>
                <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {media.unavailableIds?.includes(entry.id) ? "⊘ " : ""}{entry.title}
                </span>
                <span style={{ opacity: 0.7 }}>{formatMediaDuration(entry.duration)}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

const btnStyle: CSSProperties = {
  width: 40, height: 34, borderRadius: 8, cursor: "pointer",
  background: "rgba(0,229,255,0.07)", border: `1px solid ${C.line}`,
  color: C.text, fontSize: 15, lineHeight: 1, padding: 0,
  display: "inline-flex", alignItems: "center", justifyContent: "center",
};

const primaryBtnStyle: CSSProperties = {
  width: 52, background: "rgba(245,166,35,0.14)", borderColor: C.lineGold,
  color: C.gold, fontSize: 17,
};
