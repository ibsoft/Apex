"use client";

/* The YouTube IFrame player that both media windows sit on.
 *
 * Three things here are load-bearing and easy to get wrong:
 *
 * 1. The API script is loaded once per page, through one module-scoped
 *    promise. `onYouTubeIframeAPIReady` is a single global callback, so two
 *    components each installing their own resolver would fight over it, and a
 *    failed load must clear the promise or every later attempt inherits the
 *    rejection.
 * 2. The mount node is created imperatively and owned by this component.
 *    `new YT.Player(node)` *replaces* the element it is given with an iframe,
 *    so a React-managed node is gone by the time React unmounts and the
 *    removeChild throws. React owns the container, we own what goes in it.
 * 3. Player state is never mirrored twice: `playing` comes from the provider's
 *    media state, and the only things read back from the iframe are what the
 *    operator does *inside* it (the native controls in video mode) and the end
 *    of a track.
 */

import { useEffect, useRef, useState } from "react";

/** The slice of the YT.Player API this file uses, typed so the rest of the
 *  app never sees `any` coming back out of the loader. */
export type YoutubePlayerHandle = {
  destroy: () => void;
  loadVideoById: (id: string) => void;
  playVideo: () => void;
  pauseVideo: () => void;
  seekTo: (seconds: number, allowSeekAhead: boolean) => void;
  getCurrentTime: () => number;
  getDuration: () => number;
  getPlayerState: () => number;
  getIframe: () => HTMLIFrameElement;
  /* Sound. A browser blocks autoplay *with sound* until the page has been
     interacted with, and YouTube's answer is to start the track muted. In the
     music window the iframe is invisible, so YouTube's own "tap to unmute"
     overlay is a button nobody can see or click - these are how we bring the
     sound back, and the volume is always set explicitly so "muted" can never
     be the silent default. */
  unMute: () => void;
  mute: () => void;
  setVolume: (volume: number) => void;
  isMuted: () => boolean;
};

const DEFAULT_VOLUME = 100;

/* Sound is not a setting applied once: `autoplay` on a page that has not been
 * clicked yet makes YouTube start muted, and the only unmute control inside a
 * hidden iframe is unreachable. So this runs on ready and on every play - the
 * operator's next click anywhere in APEX is what makes it stick, and until
 * then the media windows show a sound button that is a gesture of its own. */
function applySound(player: YoutubePlayerHandle, volume: number) {
  try {
    player.setVolume(volume);
    player.unMute();
  } catch { /* not ready yet, or a player already destroyed */ }
}

/* The API builds the iframe with its own `allow` list, but without the autoplay
 * token an unmute is simply ignored. Assert it rather than trust the embed. */
function assertAutoplayAllowed(player: YoutubePlayerHandle) {
  try {
    const iframe = player.getIframe?.();
    const allow = iframe?.getAttribute("allow") ?? "";
    if (iframe && !/autoplay/.test(allow)) {
      iframe.setAttribute("allow", `${allow} autoplay; encrypted-media; picture-in-picture`.trim());
    }
  } catch { /* older API shape */ }
}

type PlayerStateEvent = { data: number; target: YoutubePlayerHandle };

/** What the player refused, in the only form that can be acted on: the code.
 *  `null` means the failure was ours (the script never loaded), not the video's. */
export type MediaFailure = { code: number | null; detail: string };

const ENDED = 0;
const PLAYING = 1;
const PAUSED = 2;

/** YouTube's own error numbers, spelled out for the console and the card - the
 *  operator should never have to read "151" and guess. */
function errorDetail(code: number): string {
  switch (code) {
    case 2: return "error 2: invalid video id";
    case 5: return "error 5: not playable in an HTML5 player";
    case 100: return "error 100: video not found or private";
    case 101: return "error 101: embedding disabled by the owner";
    case 150: return "error 150: embedding disabled by the owner (101)";
    case 153: return "error 153: YouTube refused the request (missing referrer)";
    default: return `error ${code}: the player reported a failure`;
  }
}

let apiPromise: Promise<any> | null = null;

function loadYoutubeApi(): Promise<any> {
  if (typeof window === "undefined") return Promise.reject(new Error("no window"));
  const w = window as any;
  if (w.YT && typeof w.YT.Player === "function") return Promise.resolve(w.YT);
  if (apiPromise) return apiPromise;
  apiPromise = new Promise((resolve, reject) => {
    const previous = w.onYouTubeIframeAPIReady;
    const timer = window.setTimeout(() => {
      apiPromise = null;
      reject(new Error("YouTube player timed out"));
    }, 25000);
    w.onYouTubeIframeAPIReady = () => {
      window.clearTimeout(timer);
      resolve(w.YT);
      if (typeof previous === "function") previous();
    };
    const script = document.createElement("script");
    script.src = "https://www.youtube.com/iframe_api";
    script.async = true;
    script.onerror = () => {
      window.clearTimeout(timer);
      apiPromise = null;
      script.remove();
      reject(new Error("YouTube player failed to load"));
    };
    document.head.appendChild(script);
  });
  return apiPromise;
}

type Props = {
  videoId: string;
  /** Bumped by next/previous, so a one-item queue can replay the same id. */
  seq: number;
  playing: boolean;
  /** Native controls: off for the music player (the picture is the artwork),
   *  on for the video player, where the operator expects them. */
  controls: 0 | 1;
  /** 0..100, applied whenever playback starts. Defaults to full volume. */
  volume?: number;
  onEnded: () => void;
  /** The operator paused/played through the iframe's own controls. */
  onPause: () => void;
  onResume: () => void;
  /** The player refused, and why. Without the code a window cannot tell "this
   *  video is not embeddable, play the next one" from "the embed itself is
   *  broken", so the failure is reported, not swallowed. */
  onFail?: (failure: MediaFailure) => void;
  /** Autoplay was blocked because the page has no gesture yet: the track is
   *  cued but silent, and a click on our own play button is the way out. */
  onBlocked?: () => void;
  /** Progress while playing, for the transport bar. */
  onTime?: (current: number, duration: number) => void;
  /** The raw handle (the video window's full-screen button needs the iframe). */
  onPlayer?: (player: YoutubePlayerHandle | null) => void;
  /** Shown when the script or the embed is refused (offline, blocked). */
  failText: string;
};

export default function YoutubePlayer(props: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const playerRef = useRef<YoutubePlayerHandle | null>(null);
  const mountRef = useRef<HTMLElement | null>(null);
  const readyRef = useRef(false);
  const failedRef = useRef(false);
  /* Set when *we* load a track while the state says paused: the first PLAYING
     that follows is YouTube starting on its own and must be undone, while a
     later one is the operator pressing play in the iframe and must be adopted. */
  const forcePauseRef = useRef(false);
  const [failed, setFailed] = useState(false);
  const [detail, setDetail] = useState("");

  /* The props the player's callbacks close over live in a ref: the callbacks
     are installed once, when the player is created, and a stale closure there
     would report track-end against a queue that has already moved on. */
  const live = useRef(props);
  live.current = props;
  const playingRef = useRef(props.playing);
  playingRef.current = props.playing;

  const fail = (message: string, code: number | null = null) => {
    if (failedRef.current) return;
    failedRef.current = true;
    setFailed(true);
    setDetail(message);
    live.current.onFail?.({ code, detail: message });
    // eslint-disable-next-line no-console
    console.warn("[apex media]", message);
  };

  /* Create once. The first video id is read from the live ref, and later
     changes travel through the effects below rather than by rebuilding - a
     rebuild is a visible reload on every track. */
  useEffect(() => {
    let disposed = false;
    let readyTimer: number | undefined;
    const container = containerRef.current;
    if (!container) return;
    const mount = document.createElement("div");
    mount.style.width = "100%";
    mount.style.height = "100%";
    container.appendChild(mount);
    mountRef.current = mount;

    loadYoutubeApi()
      .then((YT: any) => {
        if (disposed || !mountRef.current) return;
        readyTimer = window.setTimeout(() => {
          if (!disposed && !readyRef.current) fail("YouTube did not initialize. Check the player or retry.");
        }, 20000);
        const player = new YT.Player(mount, {
          width: "100%",
          height: "100%",
          videoId: live.current.videoId,
          playerVars: {
            autoplay: live.current.playing ? 1 : 0,
            controls: live.current.controls,
            playsinline: 1,
            rel: 0,
            modestbranding: 1,
            origin: window.location.origin,
          },
          events: {
            onReady: (event: { target: YoutubePlayerHandle }) => {
              if (disposed) return;
              window.clearTimeout(readyTimer);
              readyRef.current = true;
              playerRef.current = event.target;
              live.current.onPlayer?.(event.target);
              assertAutoplayAllowed(event.target);
              // `autoplay: 0` leaves the video cued and paused, so there is no
              // PLAYING to undo here - only the loads below can autoplay. When
              // the state already wants playback the player may have started
              // muted (no gesture yet), so claim the sound and start it: the
              // play effect below cannot help, it only runs on a change.
              if (playingRef.current) {
                applySound(event.target, live.current.volume ?? DEFAULT_VOLUME);
                try { event.target.playVideo(); } catch { /* gone */ }
              }
            },
            onStateChange: (event: PlayerStateEvent) => {
              if (disposed) return;
              if (event.data === ENDED) {
                live.current.onEnded();
                return;
              }
              if (event.data === PLAYING) {
                if (forcePauseRef.current) {
                  forcePauseRef.current = false;
                  event.target.pauseVideo();
                  return;
                }
                // Pressed play inside the iframe while the state says paused:
                // adopt it instead of fighting the click.
                if (!playingRef.current) live.current.onResume();
                return;
              }
              if (event.data === PAUSED && playingRef.current) live.current.onPause();
            },
            onError: (event: { data: number }) => {
              if (disposed) return;
              window.clearTimeout(readyTimer);
              const code = typeof event?.data === "number" ? event.data : null;
              fail(code === null ? "the video could not be played" : errorDetail(code), code);
            },
            // Keep the track cued when sound is refused. Starting it muted
            // hides the refusal and can consume the song without audible audio.
            onAutoplayBlocked: () => {
              if (disposed) return;
              live.current.onBlocked?.();
            },
          },
        });
        if (disposed) {
          try { player.destroy(); } catch { /* already gone */ }
        }
      })
      .catch((error: Error) => { if (!disposed) fail(error.message); });

    return () => {
      disposed = true;
      window.clearTimeout(readyTimer);
      readyRef.current = false;
      const player = playerRef.current;
      playerRef.current = null;
      live.current.onPlayer?.(null);
      try { player?.destroy(); } catch { /* the iframe may already be gone */ }
      mountRef.current?.remove();
      mountRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* A new track - or a replay of the same one: `loadVideoById` restarts the id
     it is given, which is exactly what "next" on a one-item queue needs. A load
     while paused must come back paused, so say so before YouTube starts. The
     playing flag is read from the ref, not the prop list: putting it in deps
     would reload the video on every pause/resume and restart the track. */
  useEffect(() => {
    forcePauseRef.current = !playingRef.current;
    const player = playerRef.current;
    if (!readyRef.current || !player) return;
    // A failed song must not hide the next iframe or suppress its error.
    // Queue fallback reuses this player, so reset the per-track failure latch.
    failedRef.current = false;
    setFailed(false);
    setDetail("");
    // A track change is a fresh autoplay: unmute before the load, or the next
    // song starts as silently as a muted first one.
    if (playingRef.current) applySound(player, live.current.volume ?? DEFAULT_VOLUME);
    try {
      player.loadVideoById(props.videoId);
    } catch { /* a player destroyed mid-switch */ }
  }, [props.videoId, props.seq]);

  /* Play / pause follows the media state. Loading is handled above; this is the
     pause/resume button, the voice "pause the music" and the router's. */
  useEffect(() => {
    const player = playerRef.current;
    if (!readyRef.current || !player) return;
    if (props.playing) {
      // The state wants playback: any earlier "this load must come back paused"
      // is void, and the operator's resume must not be undone by it.
      forcePauseRef.current = false;
      // Sound first: a click on our own play button is the gesture the browser
      // was waiting for, and this is the only play path a track load does not
      // go through (loading calls loadVideoById, which uses the player's
      // autoplay, not this effect).
      applySound(player, props.volume ?? DEFAULT_VOLUME);
      try { player.playVideo(); } catch { /* torn down between renders */ }
      return;
    }
    try { player.pauseVideo(); } catch { /* torn down between renders */ }
  }, [props.playing, props.volume]);

  /* Progress for the transport bar; only while playing, so a paused window
     stops polling the iframe. The callback is read through the live ref so an
     inline parent callback cannot churn the interval every render. */
  useEffect(() => {
    if (!props.playing) return;
    const id = window.setInterval(() => {
      const player = playerRef.current;
      if (!readyRef.current || !player || !live.current.onTime) return;
      try {
        live.current.onTime(player.getCurrentTime() || 0, player.getDuration() || 0);
      } catch { /* not ready yet */ }
    }, 500);
    return () => window.clearInterval(id);
  }, [props.playing]);

  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <div
        ref={containerRef}
        style={{
          width: "100%",
          height: "100%",
          display: failed ? "none" : "block",
          background: "#000",
        }}
      />
      {failed && (
        <div style={{
          position: "absolute", inset: 0, display: "flex", flexDirection: "column", gap: 6,
          alignItems: "center", justifyContent: "center", padding: 20, textAlign: "center",
          font: "11px var(--font-mono, monospace)", letterSpacing: "0.08em",
          color: "rgba(170,192,215,0.7)", background: "rgba(6,10,20,0.9)",
        }}>
          <span>{props.failText}</span>
          {detail && (
            <span style={{ fontSize: 10, color: "rgba(245,166,35,0.75)", letterSpacing: 0 }}>
              {detail}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
