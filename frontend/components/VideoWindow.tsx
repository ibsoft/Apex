"use client";

/* The video player window: the YouTube iframe with our own transport under it.
 *
 * Unlike the music player the picture and YouTube's native controls are shown
 * (the operator said "watch", so the browser-shaped controls are the right
 * ones); our bar adds what the iframe has no idea about - the queue, the window
 * maximize, and full screen driven from the parent frame.
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

const APP: MediaApp = "video";

const C = {
  cyan: "#00e5ff",
  gold: "#f5a623",
  line: "rgba(0,229,255,0.16)",
  lineGold: "rgba(245,166,35,0.28)",
  text: "rgba(235,244,255,0.92)",
  dim: "rgba(170,192,215,0.55)",
};

const MONO = "var(--font-mono, monospace)";

export default function VideoWindow({ focused: _focused }: { windowId: string; focused: boolean }) {
  const a = useApex();
  const language = a.settings?.response_language ?? a.config?.response_language ?? "en";
  const media = a.media?.app === APP ? a.media : null;
  const item = currentItem(media);
  const playerRef = useRef<YoutubePlayerHandle | null>(null);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [queueOpen, setQueueOpen] = useState(false);
  const [failure, setFailure] = useState<MediaFailure | null>(null);
  const [blocked, setBlocked] = useState(false);
  const [retryKey, setRetryKey] = useState(0);

  const act = (action: Parameters<typeof a.mediaAction>[1], index?: number) => a.mediaAction(APP, action, index);

  /* A refusal means the state is no longer the truth about what is on screen,
     so playback follows the player, not the queue. */
  const playing = Boolean(media && item && !media.paused && !failure);

  /* A new video clears the last one's refusal. */
  useEffect(() => {
    setFailure(null);
    setBlocked(false);
  }, [item?.id, media?.seq]);

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
    } catch { setRetryKey((key) => key + 1); }
  };

  const audible = playing && !blocked;

  /* Autoplay was blocked, so the video is waiting for a gesture. Unmuted playback needs
     the page to have been interacted with, and a click or keypress anywhere in
     APEX is exactly that - so the sound comes back on the operator's very next
     gesture, wherever it lands. */
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

  const onTime = (current: number, total: number) => {
    const player = playerRef.current;
    if (player) {
      try {
        if (player.getPlayerState() === 1 && !player.isMuted()) setBlocked(false);
      } catch { /* not ready yet */ }
    }
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

  const fullScreen = () => {
    const iframe = playerRef.current?.getIframe();
    if (!iframe) return;
    const request = iframe.requestFullscreen?.bind(iframe)
      ?? (iframe as HTMLIFrameElement & { webkitRequestFullscreen?: () => void }).webkitRequestFullscreen?.bind(iframe);
    if (!request) return;
    try {
      // Not every engine returns a promise here, and a refusal (fullscreen needs
      // a gesture, which this click is) must not surface as an error anywhere.
      const result = request() as unknown;
      if (result && typeof (result as Promise<void>).catch === "function") {
        (result as Promise<void>).catch(() => { /* refused */ });
      }
    } catch { /* refused */ }
  };

  if (!media || !item) {
    return (
      <div style={{
        flex: 1, display: "flex", flexDirection: "column", gap: 10, minHeight: 0,
        alignItems: "center", justifyContent: "center", textAlign: "center",
        padding: 18, background: "rgba(6,10,20,0.35)",
      }}>
        <div style={{ fontSize: 38, lineHeight: 1, opacity: 0.7 }}>▶</div>
        <div style={{ fontFamily: MONO, fontSize: 11, letterSpacing: "0.22em", color: C.gold }}>
          {localize(language, "NOTHING PLAYING", "ΚΑΜΙΑ ΑΝΑΠΑΡΑΓΩΓΗ")}
        </div>
        <div style={{ fontFamily: MONO, fontSize: 11, color: C.dim, maxWidth: 340 }}>
          {localize(
            language,
            "Say “watch …” or ask for a search first, then pick a result.",
            "Πείτε «δες …» ή ζητήστε αναζήτηση και επιλέξτε αποτέλεσμα.",
          )}
        </div>
      </div>
    );
  }

  const exhausted = media.items.every(entry => media.unavailableIds?.includes(entry.id));
  const total = duration || item.duration || 0;
  const percent = total > 0 ? Math.min(100, Math.max(0, (time / total) * 100)) : 0;

  /* A refusal is reported, not swallowed. A per-video one (embedding disabled,
     removed) moves on to the next item that has not already failed; an identity
     failure keeps the card, because skipping would only fail again. */
  const onFail = (info: MediaFailure) => {
    setFailure(info);
    setBlocked(false);
    if (isTrackError(youtubeErrorKind(info.code))) {
      act("reject", media.index);
    }
  };

  return (
    <div style={{
      flex: 1, display: "flex", flexDirection: "column", gap: 0, minHeight: 0,
      background: "#000",
    }}>
      {/* The player is a 16:9 box fitted into whatever the window is, not a
          full-bleed iframe. YouTube letterboxes its own picture, so a stretched
          area leaves the video floating with its native control bar stranded at
          the container edge; an aspect-ratio box makes the picture and its
          controls hug each other and the leftover space is our backdrop. */}
      <div style={{
        flex: 1, minHeight: 0, display: "flex", alignItems: "center",
        justifyContent: "center", background: "#000", overflow: "hidden",
      }}>
        <div style={{
          position: "relative", width: "100%", aspectRatio: "16 / 9",
          maxWidth: "100%", maxHeight: "100%",
        }}>
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
      </div>

      <div style={{
        display: "flex", flexDirection: "column", gap: 8, padding: "8px 12px 10px",
        background: "rgba(6,10,20,0.92)", borderTop: `1px solid ${C.line}`,
      }}>
        <MediaFailureBanner
          failure={failure}
          blocked={blocked}
          item={item}
          language={language}
          onRetry={retry}
        />

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

        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", rowGap: 8 }}>
          <button onClick={() => act("previous")} aria-label={localize(language, "Previous", "Προηγούμενο")} style={btnStyle}>
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
              if (blocked && !media.paused) { playNow(); return; }
              act(playing ? "pause" : "resume");
            }}
            disabled={exhausted}
          aria-label={localize(language, audible ? "Pause" : "Play", audible ? "Παύση" : "Αναπαραγωγή")}
            style={{ ...btnStyle, ...primaryBtnStyle }}
          >
            {audible ? "⏸" : "▶"}
          </button>
          <button onClick={() => act("next")} aria-label={localize(language, "Next", "Επόμενο")} style={btnStyle}>
            ⏭
          </button>
          <span style={{
            fontFamily: MONO, fontSize: 10, color: C.dim, minWidth: 84, textAlign: "center",
          }}>
            {formatMediaDuration(time) || "0:00"} / {formatMediaDuration(total)}
          </span>
          <span style={{
            flex: "1 1 140px", minWidth: 0, fontFamily: MONO, fontSize: 11, color: C.text,
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>
            {item.title}
          </span>
          <button
            onClick={() => setQueueOpen((open) => !open)}
            aria-label={localize(language, "Toggle queue", "Εναλλαγή ουράς")}
            title={localize(language, "Queue", "Ουρά")}
            style={{ ...btnStyle, fontSize: 13, opacity: queueOpen ? 1 : 0.65 }}
          >
            ☰
          </button>
          <button
            onClick={fullScreen}
            aria-label={localize(language, "Full screen", "Πλήρης οθόνη")}
            title={localize(language, "Full screen", "Πλήρης οθόνη")}
            style={{ ...btnStyle, fontSize: 14 }}
          >
            ⛶
          </button>
          <button
            onClick={() => act("maximize")}
            aria-label={localize(language, "Maximize window", "Μεγιστοποίηση παραθύρου")}
            title={localize(language, "Maximize window", "Μεγιστοποίηση παραθύρου")}
            style={{ ...btnStyle, fontSize: 13 }}
          >
            ⤢
          </button>
        </div>

        {queueOpen && (
          <div style={{
            maxHeight: 132, overflowY: "auto", borderTop: `1px solid ${C.line}`,
            paddingTop: 6, display: "flex", flexDirection: "column", gap: 2,
          }}>
            <span style={{
              fontFamily: MONO, fontSize: 9, letterSpacing: "0.22em", color: C.dim, marginBottom: 2,
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
    </div>
  );
}

const btnStyle: CSSProperties = {
  width: 36, height: 30, borderRadius: 7, cursor: "pointer",
  background: "rgba(0,229,255,0.07)", border: `1px solid ${C.line}`,
  color: C.text, fontSize: 14, lineHeight: 1, padding: 0,
  display: "inline-flex", alignItems: "center", justifyContent: "center",
  flexShrink: 0,
};

const primaryBtnStyle: CSSProperties = {
  width: 46, background: "rgba(245,166,35,0.14)", borderColor: C.lineGold,
  color: C.gold, fontSize: 16,
};
