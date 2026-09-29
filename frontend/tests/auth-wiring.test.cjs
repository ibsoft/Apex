/* Wiring checks for the sign-in and lock screens.

   These are source-level on purpose. The bug they guard was not a wrong
   algorithm but a wrong callback: the lock screen called `auth.unlock()`
   itself and then handed control to a function that only refreshed the user,
   so the server was unlocked, every request worked, and the screen never went
   away. A behavioural test would need a DOM and a real provider; asserting
   that the screen is wired to the function that clears `locked` is what
   actually matters and it fails the moment someone passes the wrong prop.
*/
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

test("the lock screen is dismissed by the call that unlocks it", () => {
  const appShell = read("components/AppShell.tsx");

  // `a.unlock` is the one that clears `locked` and restores the microphone.
  assert.match(
    appShell,
    /<LockScreen[\s\S]*?onUnlock=\{a\.unlock\}/,
    "LockScreen must be handed a.unlock, which clears the locked state",
  );
  // `afterAuth` only re-reads the user. Passing it left `locked` true, so the
  // screen stayed up over a working, unlocked session.
  assert.doesNotMatch(
    appShell,
    /onUnlocked=\{a\.afterAuth\}/,
    "afterAuth does not clear the lock; the screen would never dismiss",
  );
});

test("the unlock screen takes the unlock call, not a bare refresh", () => {
  const login = read("components/LoginScreen.tsx");
  const lockScreen = login.slice(login.indexOf("export function LockScreen"));
  assert.match(lockScreen, /onUnlock: \(password: string\) => Promise<boolean>/);
  assert.match(lockScreen, /await onUnlock\(password\)/);
  // Calling auth.unlock() from here would bypass the state change again.
  assert.doesNotMatch(lockScreen, /await auth\.unlock\(/);
});

test("the provider's unlock clears the lock and restores the microphone", () => {
  const provider = read("components/ApexProvider.tsx");
  const body = provider.slice(provider.indexOf("const unlock = useCallback"));
  const end = body.indexOf("}, []);");
  const unlock = body.slice(0, end > 0 ? end : 400);

  assert.match(unlock, /auth\.unlock\(password\)/, "must verify the password");
  assert.match(unlock, /setLocked\(false\)/, "must clear the locked state");
  assert.match(
    unlock,
    /setVoiceEnabledState\(voiceWantedRef\.current\)/,
    "must restore the microphone the operator had chosen",
  );
});

test("locking stops the microphone before any network call", () => {
  const provider = read("components/ApexProvider.tsx");
  const apply = provider.slice(provider.indexOf("const applyLocked = useCallback"));
  const stopMic = apply.indexOf("setVoiceEnabledState(false)");
  const network = apply.indexOf("await auth.lock");
  assert.ok(stopMic > -1, "applyLocked must switch the microphone off");
  // `applyLocked` runs first and `auth.lock` is awaited afterwards: a locked
  // screen must never still be listening while the request is in flight.
  assert.ok(
    network > stopMic,
    "the microphone has to be off before the lock request is sent",
  );
});

test("the CSRF token is not cleared before a request that needs it", () => {
  const api = read("lib/api.ts");
  const lock = api.slice(api.indexOf("lock: async ()"));
  const lockEnd = lock.indexOf("logout: async");
  const lockBody = lock.slice(0, lockEnd > 0 ? lockEnd : 600);

  const post = lockBody.indexOf('"/api/auth/lock"');
  const clear = lockBody.indexOf("setCsrfToken(\"\")");
  // Clearing first turns the lock POST into a 403, and the rotated session id
  // then makes the unlock that follows impossible.
  assert.ok(post > -1, "lock must post to /api/auth/lock");
  assert.ok(
    clear === -1 || clear > post,
    "the cached token must be sent with the request, not dropped first",
  );
  assert.match(lockBody, /adoptToken\(data\.csrf_token\)/, "must adopt the new token");

  const logout = api.slice(api.indexOf("logout: async ()"));
  const logoutEnd = logout.indexOf("};", logout.indexOf("api/auth/logout"));
  const logoutBody = logout.slice(0, logoutEnd > 0 ? logoutEnd : 500);
  const clearIdx = logoutBody.indexOf("setCsrfToken(\"\")");
  const postIdx = logoutBody.indexOf('"/api/auth/logout"');
  assert.ok(
    clearIdx === -1 || clearIdx > postIdx,
    "logout must send the token before forgetting it",
  );
});
