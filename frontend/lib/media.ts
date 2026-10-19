/** Shared model + pure helpers for the YouTube music and video players.
 *
 * The backend owns the search and the tool; the browser owns the queue. This
 * module is deliberately DOM-free so the unit tests can exercise it the way
 * `windows.ts` is exercised - the IFrame API loader and every component live
 * in `components/`, never here.
 */

export type MediaApp = "music" | "video";

export type MediaItem = {
  id: string;
  title: string;
  channel?: string;
  duration?: number;
  url?: string;
  thumbnail?: string;
};

export type MediaState = {
  app: MediaApp;
  items: MediaItem[];
  /** Index of the item playing (or that would play) in `items`. */
  index: number;
  paused: boolean;
  /** Tracks refused in this queue; every transport path skips them. */
  unavailableIds?: string[];
  /** Bumped on every track change (and replay of the current one) so a queue
   *  of one item can restart it: the player keys off `(videoId, seq)`, and an
   *  unchanged video id with a new seq is "play this again". */
  seq: number;
};

/** Every control the window transport buttons and the tool events share.
 *
 *  `select` is deliberately *not* in {@link normalizeMediaAction}'s set: jumping
 *  to a queue position is a click the operator makes in the window, never
 *  something a tool event asks for, so an untrusted payload cannot aim the
 *  player at an arbitrary index. */
export type MediaAction =
  | "stop"
  | "close"
  | "pause"
  | "resume"
  | "next"
  | "previous"
  | "maximize"
  | "minimize"
  | "restore"
  | "select"
  | "reject";

const MEDIA_ACTIONS = new Set<string>([
  "stop", "close", "pause", "resume", "next", "previous",
  "maximize", "minimize", "restore",
]);

/** Read an action out of an untrusted event: anything that is not one of the
 *  nine above is a no-op rather than a cast. */
export function normalizeMediaAction(raw: unknown): MediaAction | null {
  return typeof raw === "string" && MEDIA_ACTIONS.has(raw) ? (raw as MediaAction) : null;
}

/** Which player an event belongs to, or null for anything else. */
export function asMediaApp(raw: unknown): MediaApp | null {
  return raw === "music" || raw === "video" ? raw : null;
}

/** True when the operator's reply language is Greek. Mirrors `isGreek` in
 *  `commands.ts`, which is module-private, so the windows can label themselves
 *  the same way the parser reads an utterance. */
export function isGreekLanguage(language: string | null | undefined): boolean {
  return /^el(?:-|$)/i.test((language ?? "").trim());
}

/** Pick the Greek string when the operator speaks Greek, English otherwise. */
export function localize<T>(language: string | null | undefined, en: T, el: T): T {
  return isGreekLanguage(language) ? el : en;
}

/** `m:ss` (or `h:mm:ss` past an hour); empty for unknown/zero lengths. */
export function formatMediaDuration(seconds: number | null | undefined): string {
  const total = Math.floor(Number(seconds) || 0);
  if (total <= 0) return "";
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  const ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Wrap forward onto the following position; a queue of any length plays
 *  forever, and a queue of one wraps onto itself (a replay, which the seq bump
 *  turns into a restart). Negative indices normalize rather than escape. */
export function nextIndex(index: number, length: number): number {
  if (!length) return 0;
  return (((index + 1) % length) + length) % length;
}

/** Wrap backward onto the preceding position, the same way. */
export function prevIndex(index: number, length: number): number {
  if (!length) return 0;
  return (((index - 1) % length) + length) % length;
}

/** The item a state is on, clamped - an index past the end (a queue that shrank
 *  under playback) must fall back rather than render as "nothing playing". */
export function currentItem(state: MediaState | null | undefined): MediaItem | null {
  if (!state || !state.items.length) return null;
  const item = state.items[Math.min(Math.max(state.index, 0), state.items.length - 1)];
  return item ?? null;
}

/** Where the operator can watch an item when the embed refuses to play it. */
export function watchUrl(item: MediaItem | null | undefined): string {
  if (!item) return "https://www.youtube.com";
  if (item.url) return item.url;
  return `https://www.youtube.com/watch?v=${item.id}`;
}

/** Why the embedded player refused. The YouTube error codes are not
 *  interchangeable: `no-embed` and `not-found` are about *this* video (skip it
 *  and try the next), while `no-referrer` would refuse every item the same way,
 *  and `invalid` means we handed the player a bad id. */
export type MediaErrorKind = "not-found" | "no-embed" | "invalid" | "html5" | "no-referrer" | "unknown";

export function youtubeErrorKind(code: number | null | undefined): MediaErrorKind {
  switch (code) {
    case 2: return "invalid";
    case 5: return "html5";
    case 100: return "not-found";
    case 101:
    case 150: return "no-embed";
    case 153: return "no-referrer";
    default: return "unknown";
  }
}

/** Only a per-video refusal is worth skipping past. An identity or parameter
 *  failure is the same for every item, so burning the queue on it would just
 *  end in silence a few seconds later. */
export function isTrackError(kind: MediaErrorKind): boolean {
  return kind === "not-found" || kind === "no-embed";
}

/** The next item that has not already failed, or null when everything else in
 *  the queue is a known failure - then chasing it would lap forever and the
 *  error card is the honest answer. */
export function nextPlayableIndex(
  items: MediaItem[],
  index: number,
  failedIds: Iterable<string>,
): number | null {
  if (items.length < 2) return null;
  const failed = failedIds instanceof Set ? failedIds : new Set(failedIds);
  for (let step = 1; step < items.length; step++) {
    const candidate = (((index + step) % items.length) + items.length) % items.length;
    if (!failed.has(items[candidate].id)) return candidate;
  }
  return null;
}

/** Shared navigation for buttons, voice, tool events and automatic track end. */
export function availableMediaIndex(state: MediaState, direction: 1 | -1): number | null {
  const unavailable = new Set(state.unavailableIds ?? []);
  const count = state.items.length;
  for (let step = 1; step <= count; step++) {
    const index = ((state.index + direction * step) % count + count) % count;
    if (!unavailable.has(state.items[index].id)) return index;
  }
  return null;
}

/** Remember a refusal and advance once; stop if the entire queue was refused. */
export function rejectMediaTrack(state: MediaState, index: number): MediaState {
  if (index !== state.index || !state.items[index]) return state;
  const unavailableIds = [...new Set([...(state.unavailableIds ?? []), state.items[index].id])];
  const rejected = { ...state, unavailableIds };
  const next = availableMediaIndex(rejected, 1);
  return next === null
    ? { ...rejected, paused: true }
    : { ...rejected, index: next, seq: state.seq + 1 };
}

/** Operator-facing sentence for a refusal - the English text the embed shows is
 *  a code, and "error 150" is not an explanation anyone should have to read. */
export function mediaErrorSentence(language: string | null | undefined, kind: MediaErrorKind): string {
  switch (kind) {
    case "not-found":
      return localize(language, "This video is unavailable.", "Το βίντεο δεν είναι διαθέσιμο.");
    case "no-embed":
      return localize(language, "The owner does not allow embedding.", "Ο ιδιοκτήτης δεν επιτρέπει την ενσωμάτωση.");
    case "invalid":
      return localize(language, "That video id is not valid.", "Μη έγκυρο αναγνωριστικό βίντεο.");
    case "html5":
      return localize(language, "The browser cannot play this video.", "Ο περιηγητής δεν μπορεί να αναπαράξει το βίντεο.");
    case "no-referrer":
      return localize(language, "YouTube refused the embed.", "Το YouTube απέρριψε την ενσωμάτωση.");
    default:
      return localize(language, "The player failed.", "Ο αναπαραγωγός απέτυχε.");
  }
}

/** Thumbnail URL for a result; the tool already ships one, this is the
 *  fallback for anything that does not (the id shape is validated server-side). */
export function thumbUrl(item: MediaItem | null | undefined): string {
  if (!item) return "";
  if (item.thumbnail) return item.thumbnail;
  if (item.id && /^[A-Za-z0-9_-]{6,24}$/.test(item.id)) {
    return `https://i.ytimg.com/vi/${item.id}/hqdefault.jpg`;
  }
  return "";
}

/** Build-time state block for the model's prompt: which player is open, what it
 *  is playing and whether it is paused. Stays English on purpose - it is read
 *  by the model, and the tool answers in the operator's language - and is
 *  labelled as live state the same way `windowContextBlock` labels windows. */
export function mediaContextBlock(state: MediaState | null | undefined): string {
  if (!state) return "";
  const item = currentItem(state);
  if (!item) return `[Media player: ${state.app} window open, nothing playing.]`;
  const position = `${Math.min(Math.max(state.index, 0), state.items.length - 1) + 1}/${state.items.length}`;
  const status = state.paused ? "paused" : "playing";
  const by = item.channel ? ` by ${item.channel}` : "";
  const queue = state.items
    .slice(0, 8)
    .map((entry, i) => `${i + 1}. ${entry.title}`)
    .join(" · ");
  const more = state.items.length > 8 ? ` (+${state.items.length - 8} more)` : "";
  return `[Media player: ${state.app} · ${status} "${item.title}"${by} · position ${position}`
    + `${state.items.length > 1 ? `, queue: ${queue}${more}` : ""}]`;
}

/** Decorative VU levels, one 0..1 bar per band.
 *
 *  Cross-origin YouTube iframes expose no audio graph, so nothing here can be
 *  a measurement - it is the look of a level meter, driven by a phase, and it
 *  is a pure function so the shape (length, range, determinism, the bass-to-
 *  treble tilt) can be asserted without a canvas. */
export function vuLevels(phase: number, bands = 24): number[] {
  const n = Math.max(1, Math.floor(bands));
  const p = Number(phase) || 0;
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    const t = p + i * 0.37;
    const slow = Math.sin(t * 2.1) * 0.5 + 0.5;
    const fast = Math.sin(t * 5.3 + i) * 0.5 + 0.5;
    const drift = Math.sin(t * 1.1 + i * 0.5) * 0.5 + 0.5;
    // The left of the meter is the bass end; it moves harder than the treble.
    const tilt = 1 - (i / n) * 0.35;
    const raw = (slow * 0.5 + fast * 0.3 + drift * 0.2) * tilt;
    out.push(Math.min(1, Math.max(0.04, raw)));
  }
  return out;
}

/** Validate one item out of a tool event before it reaches state. */
export function asMediaItem(raw: unknown): MediaItem | null {
  if (!raw || typeof raw !== "object") return null;
  const entry = raw as Record<string, unknown>;
  const id = typeof entry.id === "string" ? entry.id : "";
  if (!/^[A-Za-z0-9_-]{6,24}$/.test(id)) return null;
  const title = typeof entry.title === "string" && entry.title.trim() ? entry.title.trim().slice(0, 300) : id;
  return {
    id,
    title,
    channel: typeof entry.channel === "string" ? entry.channel.slice(0, 200) : undefined,
    duration: typeof entry.duration === "number" && entry.duration > 0 ? entry.duration : undefined,
    url: typeof entry.url === "string" ? entry.url : undefined,
    thumbnail: typeof entry.thumbnail === "string" ? entry.thumbnail : undefined,
  };
}

/** Normalize an untrusted `media_play` payload into a state, or null when the
 *  event carries nothing that could play (a drop is the safe outcome: an empty
 *  player window would be worse than no change). */
export function stateFromPlayEvent(raw: {
  app?: unknown;
  items?: unknown;
  index?: unknown;
}): MediaState | null {
  const app = asMediaApp(raw.app);
  if (!app) return null;
  const items = (Array.isArray(raw.items) ? raw.items : [])
    .map(asMediaItem)
    .filter((item): item is MediaItem => item !== null);
  if (!items.length) return null;
  const index = typeof raw.index === "number" && Number.isFinite(raw.index)
    ? Math.min(Math.max(Math.trunc(raw.index), 0), items.length - 1)
    : 0;
  return { app, items, index, paused: false, seq: 0 };
}
