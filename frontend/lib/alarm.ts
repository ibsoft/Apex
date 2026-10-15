/** The reminder alarm is a short bell/marimba arpeggio rather than a single
 * sweep: a plain beep is easy to sleep through and hard to tell apart from the
 * timer chime. The notes are a pure description - `ApexProvider` only turns
 * them into oscillators - so the shape and its loop length can be unit tested
 * without a Web Audio implementation. */

export type AlarmNote = {
  /** Frequency in Hz. */
  freq: number;
  /** Seconds from the start of the cycle. */
  at: number;
  /** Seconds the note sounds for. */
  dur: number;
  /** Peak gain, 0..1. Kept low because the cycle repeats. */
  gain: number;
  type: OscillatorType;
};

/* E5 - A5 - C6 - E6 - C6 - E6: an ascending major arpeggio that resolves up,
 * with the top two notes on a softer triangle for a bell-like shimmer. */
export const FANCY_ALARM_NOTES: readonly AlarmNote[] = [
  { freq: 659.25, at: 0.0, dur: 0.28, gain: 0.16, type: "sine" },
  { freq: 880.0, at: 0.14, dur: 0.28, gain: 0.15, type: "sine" },
  { freq: 1046.5, at: 0.28, dur: 0.3, gain: 0.14, type: "sine" },
  { freq: 1318.51, at: 0.42, dur: 0.42, gain: 0.13, type: "sine" },
  { freq: 1046.5, at: 0.72, dur: 0.22, gain: 0.1, type: "triangle" },
  { freq: 1318.51, at: 0.88, dur: 0.6, gain: 0.12, type: "triangle" },
];

/** Seconds one full cycle occupies, including a short breath before the repeat. */
export function alarmLoopSeconds(notes: readonly AlarmNote[] = FANCY_ALARM_NOTES): number {
  const end = notes.reduce((max, n) => Math.max(max, n.at + n.dur), 0);
  return end + 0.6;
}
