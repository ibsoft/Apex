"use client";

/* PwaManager - everything the service worker and the install prompt need from a
   browser: registration, the update handshake, the install button and a small
   offline chip.

   It is mounted once from app/page.tsx and holds no state that anything else
   cares about. APEX has no other source of truth for "am I installed" - the
   worker is invisible to React, and getting this wrong only costs a stray
   install button, so the logic lives in lib/pwa.ts and is unit tested. */

import { useCallback, useEffect, useState } from "react";
import {
  INSTALL_AVAILABLE_EVENT,
  SW_URL,
  isStandalone,
  type InstallPromptEvent,
} from "../lib/pwa";

const C = {
  cyan: "#00e5ff",
  gold: "#f5a623",
};

export default function PwaManager() {
  const [installEvent, setInstallEvent] = useState<InstallPromptEvent | null>(null);
  const [standalone, setStandalone] = useState(false);
  const [waiting, setWaiting] = useState<ServiceWorker | null>(null);
  const [offline, setOffline] = useState(false);

  /* Display mode. Checked on mount and on every change, because the user can
     install the app while the tab is open and the same window then becomes
     standalone. */
  useEffect(() => {
    const read = () => setStandalone(isStandalone(window.navigator));
    read();
    const queries = (["standalone", "minimal-ui", "fullscreen", "window-controls-overlay"] as const).map(
      (mode) => window.matchMedia(`(display-mode: ${mode})`),
    );
    queries.forEach((query) => query.addEventListener?.("change", read));
    return () => queries.forEach((query) => query.removeEventListener?.("change", read));
  }, []);

  useEffect(() => {
    const goOnline = () => setOffline(false);
    const goOffline = () => setOffline(true);
    setOffline(!navigator.onLine);
    window.addEventListener("online", goOnline);
    window.addEventListener("offline", goOffline);
    return () => {
      window.removeEventListener("online", goOnline);
      window.removeEventListener("offline", goOffline);
    };
  }, []);

  /* Registration.

     An "update found" is only interesting when a worker is already in charge:
     the very first registration has nothing to replace, and treating it as an
     update would offer the user a reload for no reason.

     skipWaiting is never called here either. public/sw.js deliberately does not
     self-skip, because a worker that takes over mid-session swaps its cache
     set out from under a page that may be streaming a chat reply. The handover
     waits for the operator to ask for it. */
  useEffect(() => {
    if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
    let cancelled = false;

    // Read the controller *before* registering. A freshly installed worker calls
    // clients.claim() in activate, which fires controllerchange on this page -
    // so a handler that always reloads turns a first visit into a reload loop.
    const hadController = !!navigator.serviceWorker.controller;

    const track = (worker: ServiceWorker) => {
      worker.addEventListener("statechange", () => {
        if (cancelled) return;
        if (worker.state === "installed" && navigator.serviceWorker.controller) setWaiting(worker);
      });
    };

    void navigator.serviceWorker
      .register(SW_URL, { scope: "/" })
      .then((registration) => {
        if (cancelled) return;
        if (registration.waiting && navigator.serviceWorker.controller) setWaiting(registration.waiting);
        // Only an installing worker can report a new update. `active` is
        // already activated and will not fire statechange again.
        if (registration.installing) track(registration.installing);
        registration.addEventListener("updatefound", () => {
          const next = registration.installing;
          if (next) track(next);
        });
      })
      .catch(() => {
        /* An unavailable or blocked worker costs the offline shell and the
           install button. Nothing else in the app depends on it. */
      });

    const onControllerChange = () => {
      if (cancelled || !hadController) return;
      /* A handover we asked for. Reload so the document and the cache set come
         from the same build - a stale HTML shell pointing at chunks the previous
         generation cached is the one state that is actually broken. */
      window.location.reload();
    };
    navigator.serviceWorker.addEventListener("controllerchange", onControllerChange);

    return () => {
      cancelled = true;
      navigator.serviceWorker.removeEventListener("controllerchange", onControllerChange);
    };
  }, []);

  const applyUpdate = useCallback(() => {
    waiting?.postMessage("SKIP_WAITING");
    setWaiting(null);
  }, [waiting]);

  const install = useCallback(async () => {
    if (!installEvent) return;
    // prompt() rejects when the gesture was consumed by something else first.
    await installEvent.prompt().catch(() => {});
    const choice = await installEvent.userChoice.catch(() => null);
    // Either way the event is single-use; a dismissed prompt must not leave a
    // button behind that can only fail. Clear the shared store too, or a remount
    // resurrects the spent event.
    window.__apexInstallPrompt = null;
    setInstallEvent(null);
    if (choice?.outcome === "accepted") setStandalone(true);
  }, [installEvent]);

  const dismissInstall = useCallback(() => {
    // Dismissing is a decision for the session: drop the captured event so the
    // chip does not return on the next remount or display-mode change.
    window.__apexInstallPrompt = null;
    setInstallEvent(null);
  }, []);

  /* The beforeinstallprompt event is captured by the beforeInteractive script in
     app/layout.tsx, which stores it and forwards it as INSTALL_AVAILABLE_EVENT.
     Read the store first: the event may already have fired before this component
     mounted, and it is not re-emitted. */
  useEffect(() => {
    const sync = () => setInstallEvent(window.__apexInstallPrompt ?? null);
    sync();
    window.addEventListener(INSTALL_AVAILABLE_EVENT, sync);
    return () => window.removeEventListener(INSTALL_AVAILABLE_EVENT, sync);
  }, []);

  const showInstall = !!installEvent && !standalone;

  return (
    <>
      {showInstall && (
        <div style={{
          position: "fixed", left: 14, bottom: 14, zIndex: 85,
          display: "flex", alignItems: "center", gap: 10,
          padding: "9px 12px", borderRadius: 10,
          background: "rgba(6,12,22,0.92)", border: `1px solid ${C.cyan}44`,
          boxShadow: "0 8px 30px rgba(0,0,0,0.5)",
          fontFamily: "var(--font-mono)", fontSize: 10.5, letterSpacing: "0.08em",
          color: "rgba(240,246,255,0.9)",
        }}>
          <button onClick={install} style={{
            padding: "6px 12px", borderRadius: 8, cursor: "pointer",
            letterSpacing: "0.14em", fontSize: 10, fontWeight: 600,
            color: "#04121a", background: `linear-gradient(135deg, #7df3ff 0%, ${C.cyan} 100%)`,
            border: "none",
          }}>
            INSTALL APEX
          </button>
          <span style={{ color: "rgba(170,192,215,0.6)" }}>
            Run it in its own window, with its own icon.
          </span>
          <button onClick={dismissInstall} aria-label="Dismiss" style={{
            background: "none", border: "none", cursor: "pointer",
            color: "rgba(170,192,215,0.7)", fontSize: 14, lineHeight: 1,
          }}>
            ×
          </button>
        </div>
      )}

      {/* The offline chip shares the bottom-left corner with the error toast in
          AppShell, so it sits above it when both are up. */}
      {offline && (
        <div style={{
          position: "fixed", left: 14, bottom: showInstall ? 62 : 14, zIndex: 86,
          padding: "6px 11px", borderRadius: 8,
          background: "rgba(40,26,6,0.92)", border: `1px solid ${C.gold}55`,
          color: C.gold, fontSize: 10, letterSpacing: "0.16em",
          fontFamily: "var(--font-mono)", fontWeight: 600,
        }}>
          OFFLINE — THE BACKEND IS UNREACHABLE
        </div>
      )}

      {waiting && (
        <div style={{
          position: "fixed", right: 14, bottom: 14, zIndex: 85,
          display: "flex", alignItems: "center", gap: 10,
          padding: "9px 13px", borderRadius: 10,
          background: "rgba(6,12,22,0.92)", border: `1px solid ${C.cyan}44`,
          fontFamily: "var(--font-mono)", fontSize: 10.5, letterSpacing: "0.08em",
          color: "rgba(240,246,255,0.9)",
        }}>
          <span>An update is ready.</span>
          <button onClick={applyUpdate} style={{
            padding: "6px 12px", borderRadius: 8, cursor: "pointer",
            letterSpacing: "0.14em", fontSize: 10, fontWeight: 600,
            color: "#04121a", background: `linear-gradient(135deg, #7df3ff 0%, ${C.cyan} 100%)`,
            border: "none",
          }}>
            RELOAD
          </button>
        </div>
      )}
    </>
  );
}