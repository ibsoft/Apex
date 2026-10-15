"use client";

/* AlarmOverlay - the reminder alarm banner.
 *
 * A fired reminder keeps ringing until the operator stops it, either here (the
 * STOP button or the X) or by voice/text ("stop", "σταμάτα"). It is a banner
 * rather than a full-screen modal: the alarm must be impossible to miss but the
 * rest of APEX stays usable, so a dismiss is a deliberate click rather than
 * something that happens by clicking away. The sound itself lives in
 * ApexProvider; this component only asks it to stop. */

import { useApex } from "./ApexProvider";

const C = {
  amber: "#ffb020",
  red: "#ff6a56",
};

export default function AlarmOverlay() {
  const a = useApex();
  if (!a.alarms.length) return null;

  const many = a.alarms.length > 1;

  return (
    <div
      role="alertdialog"
      aria-modal="false"
      aria-label="Reminder alarm"
      style={{
        position: "fixed",
        top: "calc(var(--safe-top, 0px) + 18px)",
        left: "50%",
        transform: "translateX(-50%)",
        zIndex: 300,
        width: "min(520px, 94vw)",
        padding: "16px 18px",
        borderRadius: 16,
        background: "linear-gradient(160deg, rgba(40,16,8,0.97) 0%, rgba(10,8,14,0.97) 100%)",
        border: `1px solid rgba(255,106,86,0.55)`,
        boxShadow: "0 0 50px rgba(255,106,86,0.35), 0 18px 50px rgba(0,0,0,0.6)",
        fontFamily: "var(--font-mono)",
      }}
    >
      <style>{`
        @keyframes apexAlarmPulse {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.45; transform: scale(0.82); }
        }
        @keyframes apexAlarmGlow {
          0%, 100% { box-shadow: 0 0 0 0 rgba(255,106,86,0.5); }
          50% { box-shadow: 0 0 0 8px rgba(255,106,86,0.0); }
        }
      `}</style>

      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <span
          aria-hidden
          style={{
            width: 12, height: 12, borderRadius: "50%", flex: "0 0 auto",
            background: C.red, boxShadow: `0 0 18px ${C.red}`,
            animation: "apexAlarmPulse 0.9s ease-in-out infinite",
          }}
        />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 11, letterSpacing: "0.16em", color: C.amber, marginBottom: 2 }}>
            REMINDER
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {a.alarms.map((alarm) => (
              <div
                key={alarm.id}
                style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}
              >
                <span style={{
                  color: "#f4f8ff", fontSize: 15, fontWeight: 600,
                  overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                }}>
                  {alarm.name}
                </span>
                {many && (
                  <button
                    type="button"
                    onClick={() => a.stopAlarm(alarm.id)}
                    aria-label={`Stop reminder ${alarm.name}`}
                    style={{
                      flex: "0 0 auto", width: 22, height: 22, lineHeight: "20px", padding: 0,
                      borderRadius: 6, cursor: "pointer", fontSize: 13,
                      background: "rgba(255,255,255,0.06)", border: "1px solid rgba(255,255,255,0.16)",
                      color: "rgba(200,214,230,0.85)",
                    }}
                  >
                    ×
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>
        <button
          type="button"
          onClick={() => a.stopAlarm()}
          aria-label="Stop alarm"
          style={{
            flex: "0 0 auto", padding: "10px 16px", borderRadius: 10, cursor: "pointer",
            border: "1px solid rgba(255,106,86,0.6)",
            background: "linear-gradient(135deg, #ff8a70 0%, #ff6a56 60%, #e04836 100%)",
            color: "#1a0603", fontWeight: 800, letterSpacing: "0.1em", fontSize: 12,
            animation: "apexAlarmGlow 1.6s ease-out infinite",
          }}
        >
          STOP
        </button>
        <button
          type="button"
          onClick={() => a.stopAlarm()}
          aria-label="Close alarm"
          style={{
            flex: "0 0 auto", width: 28, height: 28, lineHeight: "26px", padding: 0,
            borderRadius: 8, cursor: "pointer", fontSize: 16,
            background: "rgba(255,255,255,0.05)", border: "1px solid rgba(255,255,255,0.16)",
            color: "rgba(200,214,230,0.85)",
          }}
        >
          ×
        </button>
      </div>
    </div>
  );
}
