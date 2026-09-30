import type { ReactNode } from "react";

/** Shared grouping for every section in the settings panel. */
export default function SettingsCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <fieldset style={{
      margin: 0, minWidth: 0, flexShrink: 0,
      border: "1px solid #284359", borderRadius: 8,
      padding: 12, display: "grid", gap: 12,
      color: "#dbeafa", fontSize: 12,
      background: "rgba(8,14,26,0.35)",
    }}>
      <legend style={{ padding: "0 6px", fontWeight: 500 }}>{title}</legend>
      {children}
    </fieldset>
  );
}
