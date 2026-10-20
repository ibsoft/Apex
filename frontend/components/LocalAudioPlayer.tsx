"use client";

/* A local MP3 player with the same imperative handle as `YoutubePlayer`.
 *
 * The music window's transport code (play/pause/seek/mute/sound) was written
 * against that handle, so a local track is played by an `<audio>` element that
 * answers the same calls rather than by a second transport implementation. The
 * only real difference is the source of the bytes: a signed APEX stream URL,
 * not the YouTube iframe.
 */

import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { streamHref } from "../lib/media";

export type LocalAudioPlayerHandle = {
  playVideo: () => void;
  pauseVideo: () => void;
  seekTo: (seconds: number, allowSeekAhead: boolean) => void;
  mute: () => void;
  unMute: () => void;
  isMuted: () => boolean;
  setVolume: (volume: number) => void;
  getPlayerState: () => number;
};

type Props = {
  src: string;
  seq: number;
  playing: boolean;
  controls?: boolean;
  onEnded?: () => void;
  onPause?: () => void;
  onResume?: () => void;
  onTime?: (current: number, total: number) => void;
  onPlayer?: (player: LocalAudioPlayerHandle) => void;
  onFail?: (info: { code: number | null; detail: string }) => void;
};

const LocalAudioPlayer = forwardRef<LocalAudioPlayerHandle, Props>(function LocalAudioPlayer(
  { src, seq, playing, controls = true, onEnded, onPause, onResume, onTime, onPlayer, onFail },
  ref,
) {
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const handle: LocalAudioPlayerHandle = {
    playVideo: () => { void audioRef.current?.play().catch(() => {}); },
    pauseVideo: () => audioRef.current?.pause(),
    seekTo: (seconds) => { if (audioRef.current) audioRef.current.currentTime = seconds; },
    mute: () => { if (audioRef.current) audioRef.current.muted = true; },
    unMute: () => { if (audioRef.current) audioRef.current.muted = false; },
    isMuted: () => audioRef.current?.muted ?? false,
    setVolume: (volume) => { if (audioRef.current) audioRef.current.volume = Math.min(1, Math.max(0, volume / 100)); },
    getPlayerState: () => (audioRef.current && !audioRef.current.paused ? 1 : 2),
  };

  useImperativeHandle(ref, () => handle, [src]);

  useEffect(() => {
    onPlayer?.(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src, seq]);

  /* Drive the element from state the same way the iframe player is driven:
     `playing` is the source of truth, and the element follows it. */
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (playing) {
      void audio.play().catch(() => {
        // Autoplay refused: the window's blocked-gesture path handles the user's
        // next click, so this is not a failure to report.
      });
    } else {
      audio.pause();
    }
  }, [playing, src, seq]);

  return (
    <audio
      ref={audioRef}
      src={streamHref(src)}
      controls={controls}
      preload="metadata"
      onEnded={onEnded}
      onPause={() => onPause?.()}
      onPlay={() => onResume?.()}
      onTimeUpdate={(event) => {
        const audio = event.currentTarget;
        onTime?.(audio.currentTime, Number.isFinite(audio.duration) ? audio.duration : 0);
      }}
      onLoadedMetadata={(event) => {
        const audio = event.currentTarget;
        onTime?.(audio.currentTime, Number.isFinite(audio.duration) ? audio.duration : 0);
      }}
      onError={() => onFail?.({ code: null, detail: "This local track could not be played." })}
      style={{ width: "100%", height: 54 }}
    />
  );
});

export default LocalAudioPlayer;
