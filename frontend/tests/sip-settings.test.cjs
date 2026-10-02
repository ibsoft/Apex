/* The SIP settings tab: a write-only password and a real dial gate.
 *
 * The server never returns the stored SIP password (it deletes the key and
 * adds a `<key>_set` flag), so the tab cannot render it and must not try. These
 * assertions are textual, like csrf-clients.test.cjs: the failure is "the box
 * quietly shows a placeholder that then gets posted back over the real secret",
 * which a mock would happily accept.
 */
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

const panel = read("components/SipSettings.tsx");
const api = read("lib/api.ts");

test("the password box is write-only and emptied after every save", () => {
  assert.match(panel, /type=["']password["']/, "the SIP password input must be a password field");
  /* The stored value is never fetched, so nothing may seed the box from it. */
  assert.ok(
    !/value=\{[^}]*password_set[^}]*\}/.test(panel),
    "the password box must not be seeded from the server; the server never sends it",
  );
  assert.ok(!/defaultValue=\{[^}]*password/i.test(panel), "no server value as a default either");
  /* A saved secret must not be left on screen looking like a stored credential. */
  const save = panel.slice(panel.indexOf("const save = "), panel.indexOf("const saveAll"));
  assert.match(save, /setPassword\(""\)/, "the box must be cleared once the save is done");
});

test("an empty password is omitted from the payload, not sent as empty", () => {
  /* Sending "" would be ambiguous with "clear it". The server ignores an empty
   * value and clearing is a separate endpoint; the tab must not ask to clear by
   * saving some other field. */
  assert.match(panel, /password\.trim\(\)\s*\?\s*\{\s*sip_password/, "send the password only when typed");
  assert.match(panel, /api\.sip\.clearPassword/, "clearing is explicit");
});

test("the tab routes its mutating calls through lib/api.ts", () => {
  for (const call of ["api.sip.status()", "api.sip.clearPassword()", "a.updateSettings("]) {
    assert.ok(panel.includes(call), `expected the tab to use ${call}`);
  }
  const raw = [...panel.matchAll(/fetch\(/g)];
  assert.equal(raw.length, 0, "the settings tab must not hand-roll fetch()");
});

test("lib/api.ts exposes the SIP status and clear calls", () => {
  assert.match(api, /status:\s*\(\)\s*=>\s*json<SipStatus>\(\s*["']\/api\/sip\/status["']/);
  assert.match(api, /clearPassword:\s*\(\)\s*=>\s*json<[^>]+>\(\s*["']\/api\/sip\/clear-password["'],\s*\{\s*method:\s*["']POST["']/);
});

test("the SIP config summary is typed for both consumers", () => {
  /* AppConfig and the provider's inline config type are separate declarations;
   * adding SIP to one and not the other fails the build only at the use site. */
  for (const [file, label] of [["lib/api.ts", "api.ts"], ["components/ApexProvider.tsx", "ApexProvider.tsx"]]) {
    const src = read(file);
    for (const key of ["sip_enabled", "sip_server", "sip_user", "sip_password_set",
                       "sip_transport", "sip_port", "sip_display_name", "sip_domain",
                       "sip_outbound_proxy", "sip_configured"]) {
      assert.ok(src.includes(`${key}:`), `${label} is missing sip config key ${key}`);
    }
  }
});

test("the settings tab is actually rendered in the tab list", () => {
  const chat = read("components/ChatUI.tsx");
  assert.match(chat, /import SipSettings from "\.\/SipSettings"/);
  assert.match(chat, /<SipSettings \/>/);
});
