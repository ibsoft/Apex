"use client";

/**
 * OVERVIEW lamp panel - same design as the Apex app's top-left HUD
 * (OverviewMode.jsx): glowing filament line + sliding node + "OVERVIEW"
 * label, live clock/date and the visitor's own local weather.
 *
 * The clock reads the visitor's location straight from the browser
 * (navigator.geolocation -> reverse geocode for the town name, open-meteo for
 * the weather) and shows the TOWN's local time via the timezone offset, so
 * it never guesses or shows "your town".
 *
 * Clicking the lamp opens the tiles. They are deliberately three different
 * kinds of thing, and each says which it is before it is pressed: "What is
 * Apex" opens the story overlay, "develop your own" goes to the guide, and the
 * social tiles leave the site (marked with an arrow, and they open in a new
 * tab so nobody loses the page they were on).
 */

import { useEffect, useState } from "react";
import { Sparkles, Linkedin, ArrowUpRight } from "lucide-react";

const ACCENT = "#00e5ff";
const WCODE: Record<number, string> = { 0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle", 61: "Rain", 63: "Rain", 65: "Heavy rain", 71: "Snow", 73: "Snow", 75: "Snow", 80: "Showers", 81: "Showers", 82: "Showers", 95: "Storm", 96: "Storm", 99: "Storm" };

type Tile = { key: string; icon: typeof Sparkles; label: string; href: string };

// Social links stay live — they point to public profiles. Swap them for your own.
const TILES: Tile[] = [
  { key: "linkedin",  icon: Linkedin,  label: "Follow us on LinkedIn",  href: "https://www.linkedin.com/in/insbhrs" },
];

function Clock() {
  const [now, setNow] = useState<Date | null>(null);
  const [tzOffset, setTzOffset] = useState<number | null>(null);
  const [wx, setWx] = useState<{ temp: number | null; code: number | null } | null>(null);
  const [city, setCity] = useState<string | null>(null);
  const [locOk, setLocOk] = useState(false);

  useEffect(() => {
    setNow(new Date());
    const id = setInterval(() => setNow(new Date()), 30000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    let ok = true;
    if (typeof navigator === "undefined" || !navigator.geolocation) return;
    // Ask the BROWSER where it is, then reverse-geocode the town and pull the
    // weather for those exact coordinates. open-meteo has no API key and the
    // reverse-geocode service is the browser-friendly BigDataCloud endpoint —
    // neither sees a secret, so we can call them straight from the client.
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        const lat = pos.coords.latitude;
        const lon = pos.coords.longitude;
        try {
          const [geo, meteo] = await Promise.all([
            fetch(
              `https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=${lat}&longitude=${lon}&localityLanguage=en`
            )
              .then((r) => r.json())
              .catch(() => null),
            fetch(
              `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}&current=temperature_2m,weather_code&timezone=auto`
            )
              .then((r) => r.json())
              .catch(() => null),
          ]);
          if (!ok) return;
          if (meteo?.utc_offset_seconds != null) setTzOffset(meteo.utc_offset_seconds);
          if (meteo?.current) setWx({ temp: meteo.current.temperature_2m ?? null, code: meteo.current.weather_code ?? null });
          setCity(geo?.city || geo?.locality || geo?.principalSubdivision || null);
          setLocOk(true);
        } catch {
          if (ok) setLocOk(false);
        }
      },
      () => {},
      { timeout: 8000, maximumAge: 300000 }
    );
    return () => { ok = false; };
  }, []);

  if (!now) return <div style={{ height: 48 }} />;
  // When geolocation succeeded, show the TOWN's own local time (UTC + the
  // open-meteo offset); otherwise fall back to the browser's local time.
  const ts = tzOffset != null ? new Date(Date.now() + tzOffset * 1000) : now;
  const useUTC = tzOffset != null ? "UTC" : undefined;
  const time = ts.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: useUTC });
  const date = ts.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: useUTC });
  const label = city ? city.toUpperCase() : null;

  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 26, flexWrap: "wrap" }}>
      <div>
        <div style={{ fontSize: 34, fontWeight: 300, letterSpacing: "0.04em", color: "#f0ede8", lineHeight: 1, textShadow: `0 0 22px ${ACCENT}33` }}>{time}</div>
        <div style={{ fontSize: 10.5, letterSpacing: "0.2em", color: "rgba(240,237,232,0.55)", marginTop: 4, textTransform: "uppercase" }}>{date}</div>
      </div>
      {locOk && wx && (
        <div>
          <div style={{ fontSize: 22, fontWeight: 300, color: `${ACCENT}e6` }}>{wx.temp != null ? Math.round(wx.temp) + "°C" : "-"}</div>
          <div style={{ fontSize: 9.5, letterSpacing: "0.12em", color: "rgba(240,237,232,0.5)", marginTop: 2, textTransform: "uppercase" }}>
            {label || ""}{label && wx.code != null ? " · " : ""}{wx.code != null ? (WCODE[wx.code] || "").toUpperCase() : ""}
          </div>
        </div>
      )}
    </div>
  );
}

export default function ApexOverviewPanel() {
  const [open, setOpen] = useState(false);
  const FIL = open ? 480 : 320; // filament width - grows when lit

  /**
   * How long the filament may actually get.
   *
   * The "Site home" pill sits at the top right of /apex, on the same line as
   * this lamp, and the filament used to run straight under it - and the node
   * that slides to the far end when the lamp opens parked underneath it. The
   * 184px is that pill's real estate: 8px lamp inset + up to 40px page inset +
   * ~124px of pill + a gap. On anything wider than a phone FIL wins and this
   * never applies; it only bites where the two would actually meet.
   */
  const FIL_CAP = "calc(100vw - 184px)";

  // One look for all five tiles: they sit in a list and a tile that changed
  // shape when pressed would read as a different kind of control.
  const tileStyle: React.CSSProperties = {
    display: "flex", alignItems: "center", gap: 11, padding: "11px 14px",
    background: "rgba(6,14,26,0.72)", border: `1px solid ${ACCENT}2a`,
    borderRadius: 10, cursor: "pointer", textAlign: "left",
    backdropFilter: "blur(8px)", transition: "all .2s",
    color: "rgba(240,237,232,0.9)", textDecoration: "none", width: "100%",
  };

  const inner = (Icon: typeof Sparkles, label: string, external?: boolean) => (
    <>
      <span style={{ display: "flex", color: ACCENT }}><Icon size={16} /></span>
      <span style={{ flex: 1, fontSize: 11.5, letterSpacing: "0.05em", lineHeight: 1.2 }}>{label}</span>
      {external && <ArrowUpRight size={12} style={{ color: `${ACCENT}88`, flex: "none" }} />}
    </>
  );

  return (
    <div className="apex-overview" style={{ pointerEvents: "none" }}>
      {/* downward glow cone */}
      <div style={{
        position: "absolute", top: 14, left: 8, width: "min(520px, 94vw)", height: 240, pointerEvents: "none",
        background: `radial-gradient(ellipse 48% 70% at 46% 0%, ${ACCENT}${open ? "55" : "28"}, ${ACCENT}0a 46%, transparent 72%)`,
        filter: "blur(10px)", transition: "all .5s ease",
      }} />

      {/* filament line + sliding node + click target */}
      <div
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen((o) => !o); } }}
        role="button" tabIndex={0} aria-label="Toggle overview panel" aria-expanded={open}
        style={{ position: "relative", height: 40, cursor: "pointer", pointerEvents: "auto", userSelect: "none" }}
      >
        <div style={{
          position: "absolute", top: 14, left: 8, width: `min(${FIL}px, ${FIL_CAP})`, height: 2, borderRadius: 2,
          background: "#a5f3fc",
          boxShadow: `0 0 10px ${ACCENT}, 0 0 26px ${ACCENT}${open ? ", 0 0 54px " + ACCENT : ""}`,
          transition: "all .5s ease",
        }} />
        {/* the node that slides to the right end when the panel opens */}
        <div style={{
          // Rides the end of the filament, so it has to obey the same cap or it
          // parks itself on top of the Site home button.
          position: "absolute", top: 11, left: open ? `min(${FIL}px, ${FIL_CAP})` : 6, width: 8, height: 8, borderRadius: "50%",
          background: "#e0fbff", boxShadow: `0 0 10px ${ACCENT}, 0 0 18px ${ACCENT}`,
          transition: "left .5s ease", pointerEvents: "none",
        }} />
        <span style={{
          position: "absolute", top: 20, left: 10, fontFamily: "var(--font-mono)", fontSize: 9, letterSpacing: "0.34em",
          color: `rgba(165,243,252,${open ? 0.9 : 0.55})`, transition: "color .4s",
        }}>OVERVIEW</span>
      </div>

      {/* clock + weather inside the lit cone */}
      <div style={{ paddingLeft: 14, paddingTop: 6, width: "fit-content", pointerEvents: "auto" }}>
        <Clock />

        {/* tiles - appear when the lamp is lit */}
        {open && (
          <div style={{ marginTop: 16, width: 250, display: "flex", flexDirection: "column", gap: 8 }}>
            {TILES.map(({ key, icon: Icon, label, href }) => (
              // rel on every external link: noopener is a security matter, not a
              // preference, once target is _blank.
              <a key={key} href={href} target="_blank" rel="noopener noreferrer" style={tileStyle}>
                {inner(Icon, label, true)}
              </a>
            ))}
          </div>
        )}
      </div>

    </div>
  );
}
