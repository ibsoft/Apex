"use client";

/* TerminalWindow - an xterm host terminal inside a desktop window.
 *
 * Connects to the backend PTY session over a few tiny routes:
 *   POST /api/terminal/session           (created by ApexProvider)
 *   POST /api/terminal/session/<id>/input   base64 bytes, queued
 *   POST /api/terminal/session/<id>/resize  {cols, rows}
 *   GET  /api/terminal/session/<id>/drain   incremental output poll
 *   DELETE /api/terminal/session/<id>       released on unmount
 *
 * Output is pulled with a lightweight byte cursor; input is flushed in order so
 * fast typing is never reordered. The session is also killed server-side when
 * it goes idle, so closing the tab can never leak a shell.
 */

import React, { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { shouldReleaseTerminal } from "../lib/windows";
import { apiFetch } from "../lib/api";

type Props = {
  sessionId: string;
  base: string;
  focused: boolean;
  onClosed?: () => void;
};

export default function TerminalWindow({ sessionId, base, focused, onClosed }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const cursorRef = useRef(0);
  const mountedAtRef = useRef(Date.now());
  const pollIdRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const inputQueueRef = useRef<Promise<unknown>>(Promise.resolve());
  const disconnectedRef = useRef(false);
  // One drain in flight at a time: two overlapping polls can land out of order,
  // which interleaves a command and its output in the wrong sequence.
  const inFlightRef = useRef(false);
  const focusedRef = useRef(focused);
  focusedRef.current = focused;

  const apiPath = (path: string) =>
    `/api/terminal/session/${encodeURIComponent(sessionId)}${path}`;
  const api = (path: string) => `${base.replace(/\/$/, "")}${apiPath(path)}`;

  /* Input, resize and release all change state, so they need the CSRF token and
     the retry after a stale one. `base` is always lib/api's BASE, so going
     through apiFetch addresses the same origin - it just adds the token, which
     these three used to omit and the server answered with invalid_csrf_token. */
  const apiPost = (path: string, body: unknown) =>
    apiFetch(apiPath(path), { method: "POST", body: JSON.stringify(body) });

  const onClosedRef = useRef(onClosed);
  onClosedRef.current = onClosed;
  const closedOnceRef = useRef(false);
  const closeWindow = () => {
    if (closedOnceRef.current) return;
    closedOnceRef.current = true;
    onClosedRef.current?.();
  };

  useEffect(() => {
    if (typeof window === "undefined") return;
    const host = hostRef.current;
    if (!host) return;

    const term = new Terminal({
      cursorBlink: true,
      fontFamily: "var(--font-mono), monospace",
      fontSize: 12.5,
      lineHeight: 1.25,
      scrollback: 5000,
      theme: { background: "#070b14", foreground: "#dbe7f5", cursor: "#00e5ff" },
      allowTransparency: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);
    fit.fit();
    termRef.current = term;
    fitRef.current = fit;
    if (focused) term.focus();

    const flush = (chunk: string) => {
      if (!chunk || disconnectedRef.current) return;
      inputQueueRef.current = inputQueueRef.current
        .then(async () => {
          const res = await apiPost("/input", { data: btoa(chunk) });
          if (!res.ok && res.status === 410) {
            disconnectedRef.current = true;
            closeWindow();
          }
        })
        .catch(() => undefined);
    };
    term.onData((data) => flush(data));

    let disposed = false;
    const poll = async () => {
      if (disposed || disconnectedRef.current) return;
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      try {
        const res = await fetch(api(`/drain?from=${cursorRef.current}`), { credentials: "include" });
        if (!res.ok) {
          if (res.status === 404 || res.status === 410) {
            disconnectedRef.current = true;
            closeWindow();
          }
          return;
        }
        const payload = await res.json();
        // If this effect was torn down mid-flight, drop the response entirely:
        // the cursor must only ever advance past bytes that were actually
        // written to the terminal, or the bytes in between are lost for good
        // and the window stays blank. drain() is not consuming, so re-reading
        // the same cursor is free and the remounted effect repaints them.
        if (disposed) return;
        if (payload.data) term.write(Uint8Array.from(atob(payload.data), (c) => c.charCodeAt(0)));
        cursorRef.current = payload.from;
        if (payload.closed) {
          disconnectedRef.current = true;
          closeWindow();
        }
      } catch {
        /* transient network error; keep polling */
      } finally {
        inFlightRef.current = false;
      }
    };
    void poll();
    pollIdRef.current = setInterval(() => void poll(), 160);

    const postResize = () => {
      const ft = fitRef.current;
      const instance = termRef.current;
      if (!ft || !instance) return;
      try {
        ft.fit();
        void apiPost("/resize", { cols: instance.cols, rows: instance.rows }).catch(
          () => undefined
        );
      } catch {
        /* container not measurable yet */
      }
    };

    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => {
        if (!disconnectedRef.current) postResize();
      });
      observer.observe(host);
    }

    const onKeydown = (e: KeyboardEvent) => {
      // Never stop propagation: xterm's own keydown listener on its hidden
      // textarea must receive every key to translate it into the escape
      // sequence the shell expects (function keys for mc, arrows for shell
      // history, Ctrl+C, ...). The window layer already ignores keys while a
      // terminal window is focused, so nothing needs shielding up here.
      // The browser would otherwise steal some of these — F1 help, F5 reload,
      // F11 fullscreen, F12 devtools. Cancel the default action for plain
      // function keys so they reach the shell instead; propagation continues so
      // the xterm textarea handler still runs and maps F-keys to sequences.
      if (focusedRef.current && /^F\d{1,2}$/.test(e.key) && !e.ctrlKey && !e.altKey && !e.metaKey) {
        e.preventDefault();
      }
    };
    host.addEventListener("keydown", onKeydown, true);

    return () => {
      disposed = true;
      if (pollIdRef.current) clearInterval(pollIdRef.current);
      if (observer) observer.disconnect();
      host.removeEventListener("keydown", onKeydown, true);
      // Release the host shell (best effort; the server reaps idle sessions too).
      // React StrictMode double-mounts effects, so a spurious unmount right after
      // mount must not DELETE a fresh session (the second mount reuses it).
      if (shouldReleaseTerminal(mountedAtRef.current)) {
        void apiFetch(apiPath(""), { method: "DELETE" }).catch(() => undefined);
      }
      try {
        term.dispose();
      } catch {
        /* already disposed */
      }
      termRef.current = null;
      fitRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, base]);

  useEffect(() => {
    if (focused && termRef.current) termRef.current.focus();
  }, [focused]);

  if (typeof window === "undefined") return null;

  return (
    <div
      ref={hostRef}
      data-terminal-session={sessionId}
      style={{
        flex: 1, minWidth: 0, minHeight: 0, width: "100%",
        padding: 6, background: "#070b14", overflow: "hidden",
      }}
    />
  );
}