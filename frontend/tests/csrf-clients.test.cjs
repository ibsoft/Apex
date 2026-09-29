/* Every state-changing browser API must carry the CSRF token.
 *
 * These were separate hand-rolled fetch() calls that never sent X-APEX-CSRF, so
 * once the server started requiring it (which it must, now that every session
 * has a sid) they were all answered with invalid_csrf_token: the file manager
 * could not open a picture or a document, and the terminal could not take
 * input, resize or be closed.
 *
 * The test is deliberately textual. It reads the sources rather than driving a
 * browser, because the failure mode is "a client quietly stopped going through
 * apiFetch", which is exactly what a mock would paper over.
 */
const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

/* Clients that mutate server state. Each must reach lib/api.ts, either by
 * calling apiFetch/requestHeaders or, for XHR, by setting the header itself. */
const MUTATING_CLIENTS = [
  { file: "lib/fm.ts", label: "file manager" },
  { file: "components/TerminalWindow.tsx", label: "terminal window" },
  { file: "components/ApexProvider.tsx", label: "provider (terminal open)" },
];

test("no mutating client hand-rolls fetch without the token", () => {
  for (const { file, label } of MUTATING_CLIENTS) {
    const src = read(file);
    const usesShared = /apiFetch|requestHeaders/.test(src);
    assert.ok(usesShared, `${label} (${file}) must route through apiFetch/requestHeaders`);

    /* Any raw fetch() left in these files is a state change that forgot the
     * token, so it has to be a GET with no method override. */
    const rawFetches = [...src.matchAll(/fetch\(/g)];
    for (const match of rawFetches) {
      const window = src.slice(match.index, match.index + 220);
      /* The first call after the match is the one under test. A GET is safe. */
      if (/method:\s*["'](POST|PUT|PATCH|DELETE)["']/.test(window)) {
        assert.fail(`${label} (${file}) has a raw fetch() with an unsafe method: ${window.slice(0, 90)}`);
      }
    }
  }
});

test("the file manager sends the token on its XHR upload", () => {
  const src = read("lib/fm.ts");
  assert.match(src, /setRequestHeader\(\s*["']X-APEX-CSRF["']/, "upload XHR must set the token");
  /* A multipart body must not be given a Content-Type by hand. */
  const around = src.slice(src.indexOf("X-APEX-CSRF") - 200, src.indexOf("X-APEX-CSRF") + 120);
  assert.ok(!/setRequestHeader\(\s*["']Content-Type["']/.test(around), "must not set Content-Type on the upload");
});

test("apiFetch retries a stale token once, then gives up", () => {
  const src = read("lib/api.ts");
  assert.match(src, /refreshCsrfToken/, "a stale token must be re-minted, not just cleared");
  assert.match(src, /invalid_csrf_token/, "apiFetch must recognise the rejection");
  /* The retry has to be a single re-send, not recursion: exactly one mention of
   * apiFetch in the body, which is the declaration itself. */
  const fn = src.slice(src.indexOf("export async function apiFetch"));
  const body = fn.slice(0, fn.indexOf("\n}\n"));
  const mentions = body.match(/apiFetch\(/g) || [];
  assert.equal(mentions.length, 1, "apiFetch must not call itself");
});

test("every unsafe method is covered by the token", () => {
  const src = read("lib/api.ts");
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    assert.match(src, new RegExp(`UNSAFE_METHODS[^\\n]*${method}|${method}[^\\n]*UNSAFE_METHODS`), method);
  }
});
