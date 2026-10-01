/* Progressive-web-app support for the APEX front end.

   Three separate jobs live here, deliberately split from the React component
   that drives them:

   * knowing whether we are running as an installed app (standalone) or in a
     browser tab, because the install button must disappear once it is used;
   * classifying a request the same way public/sw.js does, so a mismatch between
     the worker and this module shows up as a failing test rather than as a
     cached SSE stream in production;
   * the manifest and worker URLs, kept next to the version that has to match.

   Everything that can be decided without a DOM is a plain function. The
   component in components/PwaManager.tsx owns the browser wiring only.
 */

/* The worker must be served from the origin root, or its scope will not cover
   the whole app.

   The version query is deliberate. A service worker is only re-fetched when the
   browser decides to revalidate it, and that decision is allowed to be served
   from the HTTP cache. `next start` sends public/ files with `max-age=0`, which
   is enough today, but appending the version makes "the shipped JS changed, so
   ask the worker again" true regardless of what any proxy in between decides.
   Query strings do not affect the registration scope. */
export const SW_VERSION = "v1";
export const SW_URL = `/sw.js?v=${SW_VERSION}`;
export const MANIFEST_URL = "/manifest.webmanifest";

/* ---------- install prompt ---------- */

/* Chrome fires beforeinstallprompt once per page load, at most, and only when
   the app is installable. Firefox and Safari never fire it at all - iOS has no
   install prompt, the icon is added from the share sheet.

   The event is single-use. app/layout.tsx installs a beforeInteractive capture
   script that stores it on window before hydration, because it can fire before
   React attaches a listener and it is not emitted again. PwaManager reads the
   store on mount and listens for the forwarded event, so an offer that arrives
   either side of hydration is still shown. */
export type InstallPromptEvent = Event & {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
};

export const INSTALL_AVAILABLE_EVENT = "apex:install-available";

declare global {
  interface Window {
    __apexInstallPrompt?: InstallPromptEvent | null;
  }
}

/* Installed before hydration by app/layout.tsx with
   next/script strategy="beforeInteractive".

   The event fires at most once per page load and is not re-emitted if nothing is
   listening. React attaches its own listener only after hydration, so on a heavy
   first paint the offer can be gone before the component is alive - and there is
   no way to ask for it again short of a reload. This script holds it on window
   and forwards the shipped event name, so PwaManager sees it whether it arrives
   before or after mount.

   Kept here, not inline in the layout, so the test can execute the real script
   and assert the stored/forwarded contract instead of grepping a JSX string. */
export const INSTALL_CAPTURE_SCRIPT = `(function(){
  window.__apexInstallPrompt = null;
  window.addEventListener('beforeinstallprompt', function(e){
    e.preventDefault();
    window.__apexInstallPrompt = e;
    window.dispatchEvent(new Event('${INSTALL_AVAILABLE_EVENT}'));
  });
  window.addEventListener('appinstalled', function(){
    window.__apexInstallPrompt = null;
    window.dispatchEvent(new Event('${INSTALL_AVAILABLE_EVENT}'));
  });
})();`;

/* ---------- install / display mode ---------- */

/* `standalone` is what Android and desktop Chrome report; iOS Safari has no
   media query for it and only sets navigator.standalone. Reading just one of
   them means the install button comes back inside the installed app on one
   platform or the other. */
export const STANDALONE_DISPLAY_MODES = ["standalone", "minimal-ui", "fullscreen", "window-controls-overlay"] as const;

/* navigator.standalone is non-standard and missing from lib.dom, so this takes
   an untyped navigator and narrows it. `matchMedia` is called with its receiver
   intact - detached, it throws "Illegal invocation" in Chromium. */
export function isStandalone(nav: unknown): boolean {
  if (!nav || typeof nav !== "object") return false;
  const target = nav as { standalone?: unknown; matchMedia?: unknown };
  if (target.standalone === true) return true; // iOS Safari
  if (typeof target.matchMedia !== "function") return false;
  const matchMedia = target.matchMedia as (query: string) => { matches: boolean };
  return STANDALONE_DISPLAY_MODES.some((mode) => {
    try {
      return matchMedia.call(target, `(display-mode: ${mode})`).matches === true;
    } catch {
      return false;
    }
  });
}

/* ---------- request classification ---------- */

/* Mirrors NEVER_CACHE in public/sw.js. Anything not matched here is either a
   navigation (network-first, cached shell as the offline fallback) or a
   /_next/static asset (cache-first, content-hashed and immutable). */
const NEVER_CACHE = ["/be", "/api"];

export type RequestRoute = "passthrough" | "navigation" | "static-asset";

export type RouteInput = {
  method?: string;
  url: string;
  mode?: string;
  /** Range requests are media scrubbing: a cached 206 is worse than no cache. */
  range?: boolean;
};

export function classifyRequest(input: RouteInput): RequestRoute {
  if ((input.method ?? "GET").toUpperCase() !== "GET") return "passthrough";
  if (input.range) return "passthrough";

  const path = pathOf(input.url);
  // By path, not by origin: the worker only ever sees its own origin, and a
  // same-origin navigation to /be/api/... still has to reach the backend.
  for (const prefix of NEVER_CACHE) {
    if (path === prefix || path.startsWith(`${prefix}/`)) return "passthrough";
  }

  if (input.mode === "navigate") return "navigation";
  if (path.startsWith("/_next/static/")) return "static-asset";
  return "passthrough";
}

function pathOf(url: string): string {
  try {
    return new URL(url, "https://apex.invalid").pathname;
  } catch {
    return url.split("?")[0].split("#")[0];
  }
}

/** The endpoints whose behaviour a cache would silently corrupt. This is data
 *  rather than a comment so the test asserts against the real list. */
export const UNCACHEABLE_PATHS = [
  "/be/api/chat", // the SSE turn stream
  "/be/api/terminal/session/t1/drain?from=4", // 160ms non-consuming byte cursor
  "/be/api/terminal/session/t1/input",
  "/be/api/auth/status", // mints the CSRF token
  "/be/api/files/download/tok123", // short-lived signed URL
  "/be/api/editor/download/tok123",
  "/api/weather",
];