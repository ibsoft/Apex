"use client";

/* AppsPanel - desktop-style launcher for the APPS tab.
 *
 * Renders every entry in the app registry (see ../lib/apps.ts) as a big
 * Windows-desktop-style icon with its name underneath. Clicking always opens a
 * *brand new* window for that app; there is no focus-existing behaviour.
 */

import { useMemo } from "react";
import { getApps, type LauncherCtx } from "../lib/apps";
import { useApex } from "./ApexProvider";

const C = {
  cyan: "#00e5ff",
  line: "rgba(0,229,255,0.16)",
  text: "rgba(235,244,255,0.92)",
  dim: "rgba(170,192,215,0.5)",
};

export default function AppsPanel() {
  const a = useApex();
  const launch: LauncherCtx = useMemo<LauncherCtx>(() => ({
    openTerminal: () => a.openTerminal(),
    windowOpenNew: a.windowOpenNew,
  }), [a]);
  const apps = useMemo(() => getApps(), []);

  return (
    <div style={{ padding: 12, overflowY: "auto", flex: 1 }}>
      {apps.length === 0 && (
        <div style={{ padding: 22, textAlign: "center", color: C.dim, fontSize: 10, fontFamily: "var(--font-mono)", letterSpacing: "0.12em" }}>
          NO APPS REGISTERED
        </div>
      )}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignContent: "flex-start" }}>
        {apps.map((app) => (
          <button key={app.id} onClick={() => void app.open(launch)} title={app.name}
            style={{
              width: 76, padding: "10px 4px", borderRadius: 10, cursor: "pointer",
              display: "flex", flexDirection: "column", alignItems: "center", gap: 7,
              background: "rgba(255,255,255,0.03)", border: `1px solid ${C.line}`, color: C.text,
            }}>
            <span style={{ fontSize: 34, lineHeight: 1 }}>{app.icon}</span>
            <span style={{ fontSize: 9, fontFamily: "var(--font-mono)", letterSpacing: "0.04em", textAlign: "center" }}>
              {app.name}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}