/* App launcher registry for the APPS tab (desktop-style icons).
 *
 * Built-in apps are seeded here. Any new component can register itself later:
 *
 *   import { registerApp } from "../lib/apps";
 *   registerApp({
 *     id: "my-app",
 *     name: "My App",
 *     icon: "🧩",                       // emoji glyph
 *     open: (ctx) => ctx.windowOpenNew([{ url: "myapp:", title: "My App", kind: "other" }]),
 *   });
 *
 * `open` is called at click time with a `LauncherCtx` of provider actions so a
 * launcher always opens a *brand new* window (no focus-of-existing behaviour).
 */

import type { WindowItem, WindowKind } from "./windows";

export interface LauncherCtx {
  /** Start a fresh host terminal session and open a window. Returns an error message or null. */
  openTerminal: () => Promise<string | null>;
  /** Open a brand-new window for these items (never focuses an existing one). */
  windowOpenNew: (items: WindowItem[], opts?: { title?: string; kind?: WindowKind; maximize?: boolean }) => void;
}

export interface LauncherApp {
  id: string;
  name: string;
  icon: string;
  open: (ctx: LauncherCtx) => void | Promise<void>;
}

const APPS: LauncherApp[] = [];

/** Register a launcher app. Ids are unique; a duplicate id is ignored. */
export function registerApp(app: LauncherApp): void {
  if (!app?.id || APPS.some((existing) => existing.id === app.id)) return;
  APPS.push(app);
}

/** Snapshot of the current app registry. */
export function getApps(): LauncherApp[] {
  return [...APPS];
}

function seedBuiltins(): void {
  registerApp({
    id: "terminal",
    name: "Terminal",
    icon: "🖥️",
    open: (ctx) => {
      void ctx.openTerminal();
    },
  });
  registerApp({
    id: "files",
    name: "File Manager",
    icon: "📁",
    open: (ctx) => ctx.windowOpenNew([{ url: "files:", title: "File Manager", kind: "files" }], { kind: "files" }),
  });
  registerApp({
    id: "notepad",
    name: "Notepad",
    icon: "📝",
    open: (ctx) => ctx.windowOpenNew(
      [{ url: `notepad:${Date.now()}`, title: "Untitled", kind: "notepad" }],
      { kind: "notepad" },
    ),
  });
}

seedBuiltins();