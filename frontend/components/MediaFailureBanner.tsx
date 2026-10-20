"use client";

/* The strip under a player that says what it is actually doing.
 *
 * Both media windows hide the failure differently - the video one paints the
 * player's own black card, the music one hides the iframe altogether - so the
 * same "the state says playing but nothing is coming out" situation looks like
 * a broken app in one window and a silent track in the other. This is the one
 * place that admits it: the code YouTube sent, a link to watch it on YouTube
 * instead, and a retry that rebuilds the player (the only cure for a script
 * that never loaded).
 *
 * Two states share the strip because they are the same message to the
 * operator: "this is not playing, and here is why".
 */

import type { CSSProperties } from "react";
import {
  localize,
  isTrackError,
  mediaErrorSentence,
  watchUrl,
  youtubeErrorKind,
  type MediaItem,
} from "../lib/media";
import type { MediaFailure } from "./YoutubePlayer";

const MONO = "var(--font-mono, monospace)";

const C = {
  cyan: "#00e5ff",
  gold: "#f5a623",
  dim: "rgba(170,192,215,0.6)",
};

export default function MediaFailureBanner({
  failure,
  blocked,
  item,
  language,
  onRetry,
  onDownload,
}: {
  failure?: MediaFailure | null;
  /** Autoplay was blocked: the track is cued but silent until a gesture. */
  blocked?: boolean;
  item: MediaItem | null;
  language: string;
  onRetry: () => void;
  /** Present when "embedding disabled" (error 150/101) is the failure: the
   *  operator can have the track saved as a local MP3 and played from disk. */
  onDownload?: () => void;
}) {
  if (!failure && !blocked) return null;

  const kind = failure ? youtubeErrorKind(failure.code) : null;
  const exhausted = kind !== null && isTrackError(kind);

  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", rowGap: 4,
      padding: "6px 10px", fontFamily: MONO, fontSize: 10, letterSpacing: "0.04em",
      background: "rgba(245,166,35,0.07)",
      borderTop: `1px solid ${C.gold}44`,
      color: "rgba(245,166,35,0.92)",
    }}>
      {failure ? (
        <>
          <span>⚠ {mediaErrorSentence(language, youtubeErrorKind(failure.code))}</span>
          <span style={{ color: C.dim, letterSpacing: 0 }}>{failure.detail}</span>
          <a
            href={watchUrl(item)}
            target="_blank"
            rel="noreferrer"
            style={{ color: C.cyan, textDecoration: "none", marginLeft: "auto" }}
          >
            {localize(language, "WATCH ON YOUTUBE ↗", "ΠΡΟΒΟΛΗ ΣΤΟ YOUTUBE ↗")}
          </a>
          {/* Error 150/101 is fixable: the same song saved as a local MP3 plays
              from disk, no iframe. That is the whole point of the offer. */}
          {kind === "no-embed" && onDownload ? (
            <button onClick={onDownload} style={{ ...btn, background: "rgba(245,166,35,0.12)", borderColor: `${C.gold}66`, color: C.gold }}>
              {localize(language, "DOWNLOAD & PLAY OFFLINE", "ΛΗΨΗ & ΑΝΑΠΑΡΑΓΩΓΗ OFFLINE")}
            </button>
          ) : exhausted ? (
            <span>{localize(language, "No playable tracks remain. Try another search.", "Δεν υπάρχουν άλλα διαθέσιμα κομμάτια. Δοκιμάστε άλλη αναζήτηση.")}</span>
          ) : (
            <button onClick={onRetry} style={btn}>
              {localize(language, "RETRY", "ΞΑΝΑ")}
            </button>
          )}
        </>
      ) : (
        <>
          <span>
            {localize(
              language,
              "▶ Autoplay is blocked until you interact — press play to start.",
              "▶ Η αυτόματη αναπαραγωγή είναι μπλοκαρισμένη — πατήστε αναπαραγωγή.",
            )}
          </span>
          <button onClick={onRetry} style={{ ...btn, marginLeft: "auto" }}>
            {localize(language, "PLAY", "ΑΝΑΠΑΡΑΓΩΓΗ")}
          </button>
        </>
      )}
    </div>
  );
}

const btn: CSSProperties = {
  fontFamily: MONO, fontSize: 9, letterSpacing: "0.08em", cursor: "pointer",
  background: "rgba(0,229,255,0.08)", border: `1px solid ${C.cyan}44`,
  color: C.cyan, borderRadius: 5, padding: "3px 7px",
};
