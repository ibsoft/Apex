const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

// lib/media.ts is deliberately DOM-free and import-free, so it transpiles on
// its own the way commands.ts does.
const source = fs.readFileSync(path.join(__dirname, '../lib/media.ts'), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const moduleExports = {};
new Function('exports', compiled)(moduleExports);
const {
  normalizeMediaAction, asMediaApp, isGreekLanguage, localize, formatMediaDuration,
  nextIndex, prevIndex, currentItem, thumbUrl, mediaContextBlock, vuLevels,
  asMediaItem, stateFromPlayEvent, watchUrl, youtubeErrorKind, isTrackError,
  nextPlayableIndex, mediaErrorSentence, availableMediaIndex, rejectMediaTrack,
  isLocalMedia, asMediaDownload, streamHref,
} = moduleExports;

const item = (over = {}) => ({ id: 'dQw4w9WgXcQ', title: 'A track', ...over });
const state = (over = {}) => ({
  app: 'music', items: [item(), item({ id: 'abcdefghijk', title: 'Second' })],
  index: 0, paused: false, source: 'youtube', seq: 0, ...over,
});

/* ---------- untrusted events ---------- */

test('an action from an event is one of the nine controls or nothing', () => {
  for (const action of ['stop', 'close', 'pause', 'resume', 'next', 'previous', 'maximize', 'minimize', 'restore']) {
    assert.equal(normalizeMediaAction(action), action, action);
  }
  // "select" is a click the operator makes in the window: a tool event must not
  // be able to aim the player at an arbitrary queue position.
  assert.equal(normalizeMediaAction('select'), null);
  assert.equal(normalizeMediaAction('PLAY'), null);
  assert.equal(normalizeMediaAction(''), null);
  assert.equal(normalizeMediaAction(null), null);
  assert.equal(normalizeMediaAction(4), null);
  assert.equal(normalizeMediaAction({ action: 'stop' }), null);
});

test('only the two player names are an app', () => {
  assert.equal(asMediaApp('music'), 'music');
  assert.equal(asMediaApp('video'), 'video');
  assert.equal(asMediaApp('Music'), null);
  assert.equal(asMediaApp('radio'), null);
  assert.equal(asMediaApp(undefined), null);
});

test('an item without a well-formed id never reaches the queue', () => {
  assert.equal(asMediaItem(null), null);
  assert.equal(asMediaItem('abc'), null);
  assert.equal(asMediaItem({ title: 'no id' }), null);
  assert.equal(asMediaItem({ id: '<script>x</script>', title: 'x' }), null);
  assert.equal(asMediaItem({ id: 'quo"te', title: 'x' }), null);
  assert.equal(asMediaItem({ id: 'back\\slash', title: 'x' }), null);
  assert.equal(asMediaItem({ id: 'new\nline', title: 'x' }), null);
  // Spaces are allowed: a local file may legitimately be "Artist - Song.mp3".
  assert.equal(asMediaItem({ id: 'Artist - Song.mp3', title: 'x' }).id, 'Artist - Song.mp3');
  // A local library id is not a YouTube id and must be accepted: the fallback
  // to the local player is exactly what these ids are for.
  assert.equal(asMediaItem({ id: 'own:Artist - Song.mp3', title: 'Song' }).id, 'own:Artist - Song.mp3');
  assert.equal(asMediaItem({ id: 'local:Albums/x.mp3', title: 'x' }).id, 'local:Albums/x.mp3');
  // The title falls back to the id rather than to an empty string: a nameless
  // row in the queue would be unclickable.
  assert.equal(asMediaItem({ id: 'dQw4w9WgXcQ' }).title, 'dQw4w9WgXcQ');
  assert.equal(asMediaItem({ id: 'dQw4w9WgXcQ', title: '   ' }).title, 'dQw4w9WgXcQ');
  // A hostile payload can carry megabytes of text; it is clipped on arrival.
  assert.equal(asMediaItem({ id: 'dQw4w9WgXcQ', title: 'x'.repeat(5000) }).title.length, 300);
  assert.equal(asMediaItem({ id: 'dQw4w9WgXcQ', channel: 'y'.repeat(500) }).channel.length, 200);
  assert.equal(asMediaItem({ id: 'dQw4w9WgXcQ', duration: -5 }).duration, undefined);
  assert.equal(asMediaItem({ id: 'dQw4w9WgXcQ', duration: '10' }).duration, undefined);
});

test('a play event becomes state, and an unusable one is dropped', () => {
  const ok = stateFromPlayEvent({
    app: 'music',
    items: [{ id: 'dQw4w9WgXcQ', title: 'One' }, 'junk', { id: 'zzzzzzzzzzz', title: 'Two' }],
    index: 1,
  });
  assert.equal(ok.app, 'music');
  assert.deepEqual(ok.items.map((entry) => entry.title), ['One', 'Two']);
  assert.equal(ok.items[0].id, 'dQw4w9WgXcQ');
  // The bogus middle entry was dropped, so index 1 names the last real item.
  assert.equal(ok.index, 1);
  assert.equal(ok.paused, false);
  assert.equal(ok.seq, 0);
  assert.equal(stateFromPlayEvent({ app: 'radio', items: [{ id: 'dQw4w9WgXcQ' }] }), null);
  assert.equal(stateFromPlayEvent({ app: 'music', items: [] }), null);
  assert.equal(stateFromPlayEvent({ app: 'music', items: [{ nope: 1 }] }), null);
  assert.equal(stateFromPlayEvent({ app: 'video', items: [{ id: 'dQw4w9WgXcQ' }], index: 9 }).index, 0);
  assert.equal(stateFromPlayEvent({ app: 'video', items: [{ id: 'dQw4w9WgXcQ' }], index: 'x' }).index, 0);
});

test('a local play event is marked local, and a mixed one keeps its source', () => {
  const local = stateFromPlayEvent({
    app: 'music',
    source: 'local',
    items: [{ id: 'own:Song.mp3', title: 'Song' }],
  });
  assert.equal(local.source, 'local');
  assert.equal(isLocalMedia(local), true);
  // The event source wins when present, even if an item disagrees.
  const youtube = stateFromPlayEvent({
    app: 'music',
    source: 'youtube',
    items: [{ id: 'own:Song.mp3', title: 'Song', source: 'local' }],
  });
  assert.equal(youtube.source, 'youtube');
  // Without an explicit event source, the queue follows the first item.
  const inferred = stateFromPlayEvent({ app: 'music', items: [{ id: 'own:x.mp3', title: 'x', source: 'local' }] });
  assert.equal(inferred.source, 'local');
  assert.equal(isLocalMedia(state({ source: 'youtube' })), false);
  assert.equal(isLocalMedia(null), false);
});

test('a download bar parses only real percentages and clamps them', () => {
  assert.equal(asMediaDownload(null), null);
  assert.equal(asMediaDownload({}), null);
  assert.equal(asMediaDownload({ percent: 'x' }), null);
  assert.equal(asMediaDownload({ percent: 150 }).percent, 100);
  assert.equal(asMediaDownload({ percent: -3 }).percent, 0);
  const bar = asMediaDownload({ percent: 42, title: 'Song', id: 'own:x', stage: 'converting' });
  assert.equal(bar.percent, 42);
  assert.equal(bar.stage, 'converting');
  // An unknown stage reads as downloading, not as a missing value.
  assert.equal(asMediaDownload({ percent: 1, stage: 'weird' }).stage, 'downloading');
  assert.equal(asMediaDownload({ percent: 100, stage: 'done' }).stage, 'done');
  assert.equal(asMediaDownload({ percent: 1, title: 'y'.repeat(500) }).title.length, 300);
});

/* ---------- the queue ---------- */

test('next and previous wrap, and a queue of one lands on itself', () => {
  assert.equal(nextIndex(0, 3), 1);
  assert.equal(nextIndex(2, 3), 0);
  assert.equal(nextIndex(5, 3), 0);
  assert.equal(prevIndex(0, 3), 2);
  assert.equal(prevIndex(2, 3), 1);
  assert.equal(nextIndex(0, 1), 0);
  assert.equal(prevIndex(0, 1), 0);
  assert.equal(nextIndex(0, 0), 0);
  assert.equal(prevIndex(0, 0), 0);
});

test('the current item is clamped rather than rendering as nothing', () => {
  assert.equal(currentItem(null), null);
  assert.equal(currentItem(state({ items: [] })), null);
  assert.equal(currentItem(state()).title, 'A track');
  // A queue that shrank under playback must still show its last item.
  assert.equal(currentItem(state({ index: 99 })).title, 'Second');
  assert.equal(currentItem(state({ index: -4 })).title, 'A track');
});

test('a thumbnail is a URL for a well-formed id and nothing otherwise', () => {
  assert.equal(thumbUrl(item({ thumbnail: 'https://i.ytimg.com/vi/abc/hq.jpg' })), 'https://i.ytimg.com/vi/abc/hq.jpg');
  assert.equal(thumbUrl(item()), 'https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg');
  assert.equal(thumbUrl(item({ id: '<img src=x>' })), '');
  assert.equal(thumbUrl(null), '');
});

/* ---------- the local stream URL rewrite ---------- */

test('a backend audio URL is rewritten onto the proxied same-origin path', () => {
  // The backend signs an absolute BASE_URL URL, but Next only proxies /be/api.
  // Left absolute it is cross-origin, cookie-less and 404/401 - nothing plays.
  assert.equal(
    streamHref('https://192.168.1.218:3001/api/music/stream/abc.def-_'),
    '/be/api/music/stream/abc.def-_',
  );
  assert.equal(streamHref('/api/music/stream/tok123'), '/be/api/music/stream/tok123');
  // Already-proxied paths are not double-prefixed.
  assert.equal(streamHref('/be/api/music/stream/tok123'), '/be/api/music/stream/tok123');
  // A non-stream URL is untouched, and junk is returned as-is rather than
  // throwing inside a render.
  assert.equal(streamHref('https://example.com/song.mp3'), 'https://example.com/song.mp3');
  assert.equal(streamHref(''), '');
  assert.equal(streamHref(undefined), '');
  assert.equal(streamHref(null), '');
});

/* ---------- the prompt block ---------- */

test('the media block is English, labelled and includes the queue', () => {
  assert.equal(mediaContextBlock(null), '');
  const playing = mediaContextBlock(state({ index: 1 }));
  assert.match(playing, /^\[Media player: music · source youtube · playing "Second"/);
  assert.match(playing, /position 2\/2/);
  assert.match(playing, /queue:/);
  assert.equal(mediaContextBlock(state({ paused: true })).includes('paused "A track"'), true);
  // A local queue reports its source so the model knows playback is local.
  assert.match(mediaContextBlock(state({ source: 'local' })), /source local library/);
  const solo = mediaContextBlock(state({ items: [item()], index: 0 }));
  // One item is not a queue; naming it as one would invite the model to talk
  // about a list the operator cannot see.
  assert.equal(solo.includes('queue:'), false);
  assert.match(solo, /position 1\/1/);
  assert.match(mediaContextBlock(state({ items: [], index: 0 })), /nothing playing/);
  // A long queue is capped: the block is prompt context, not a full listing.
  const long = mediaContextBlock(state({
    items: Array.from({ length: 30 }, (_, i) => item({ id: `id${i}0000000`, title: `T${i}` })),
  }));
  assert.match(long, /\(\+22 more\)/);
});

/* ---------- the decorative level meter ---------- */

test('vuLevels is one value per band, in range, and moves with the phase', () => {
  assert.equal(vuLevels(0).length, 24);
  assert.equal(vuLevels(0, 32).length, 32);
  assert.equal(vuLevels(0, 0.5).length, 1);
  const a = vuLevels(1.3, 40);
  const b = vuLevels(1.3, 40);
  assert.deepEqual(a, b, 'the meter is a pure function of its phase');
  for (const v of a) assert.ok(v >= 0.04 && v <= 1, `out of range: ${v}`);
  assert.notDeepEqual(a, vuLevels(4.7, 40), 'the phase drives it');
});

/* ---------- the two languages ---------- */

test('Greek is recognised the way the parser reads it', () => {
  assert.equal(isGreekLanguage('el'), true);
  assert.equal(isGreekLanguage('el-GR'), true);
  assert.equal(isGreekLanguage('EL'), true);
  assert.equal(isGreekLanguage(' en '), false);
  assert.equal(isGreekLanguage('en-US'), false);
  assert.equal(isGreekLanguage(null), false);
  assert.equal(isGreekLanguage(undefined), false);
  assert.equal(localize('el', 'Play', 'Αναπαραγωγή'), 'Αναπαραγωγή');
  assert.equal(localize('el-GR', 'Play', 'Αναπαραγωγή'), 'Αναπαραγωγή');
  assert.equal(localize('en', 'Play', 'Αναπαραγωγή'), 'Play');
  assert.equal(localize(undefined, 'Play', 'Αναπαραγωγή'), 'Play');
  assert.equal(localize(null, 7, 8), 7);
});

test('durations read as a clock, and unknown length is empty', () => {
  assert.equal(formatMediaDuration(0), '');
  assert.equal(formatMediaDuration(null), '');
  assert.equal(formatMediaDuration(-3), '');
  assert.equal(formatMediaDuration(undefined), '');
  assert.equal(formatMediaDuration(5), '0:05');
  assert.equal(formatMediaDuration(65), '1:05');
  assert.equal(formatMediaDuration(3671), '1:01:11');
  assert.equal(formatMediaDuration('90'), '1:30');
});

/* ---------- refusals: what comes back when the embed says no ---------- */

test('YouTube error codes are classified by whether the video is the problem', () => {
  assert.equal(youtubeErrorKind(100), 'not-found');
  assert.equal(youtubeErrorKind(101), 'no-embed');
  assert.equal(youtubeErrorKind(150), 'no-embed');
  assert.equal(youtubeErrorKind(2), 'invalid');
  assert.equal(youtubeErrorKind(5), 'html5');
  assert.equal(youtubeErrorKind(153), 'no-referrer');
  assert.equal(youtubeErrorKind(null), 'unknown');
  assert.equal(youtubeErrorKind(undefined), 'unknown');
  assert.equal(youtubeErrorKind(999), 'unknown');
  // Only a per-video refusal is worth skipping past: the rest would refuse the
  // whole queue the same way, so chasing them lapses through it in silence.
  assert.equal(isTrackError('no-embed'), true);
  assert.equal(isTrackError('not-found'), true);
  assert.equal(isTrackError('no-referrer'), false);
  assert.equal(isTrackError('invalid'), false);
  assert.equal(isTrackError('html5'), false);
  assert.equal(isTrackError('unknown'), false);
});

test('the skip lands on the next item that has not already failed', () => {
  const items = [item({ id: 'aaaaaaaaaaa' }), item({ id: 'bbbbbbbbbbb' }), item({ id: 'ccccccccccc' })];
  assert.equal(nextPlayableIndex(items, 0, []), 1);
  // The second item also failed: the third is the one to play.
  assert.equal(nextPlayableIndex(items, 0, ['bbbbbbbbbbb']), 2);
  // Wrapping, so a failure at the end does not stop the queue.
  assert.equal(nextPlayableIndex(items, 2, ['aaaaaaaaaaa']), 1);
  // Everything else has failed: there is nothing left to try, and returning an
  // index here would lap the queue forever.
  assert.equal(nextPlayableIndex(items, 0, ['aaaaaaaaaaa', 'bbbbbbbbbbb', 'ccccccccccc']), null);
  assert.equal(nextPlayableIndex(items, 0, ['bbbbbbbbbbb', 'ccccccccccc']), null);
  // A single-item queue has nowhere to go: the error card is the answer.
  assert.equal(nextPlayableIndex([item()], 0, []), null);
  assert.equal(nextPlayableIndex([], 0, []), null);
});

test('the failure sentence is localized and never a raw code', () => {
  assert.match(mediaErrorSentence('en', 'no-embed'), /embedding/i);
  assert.match(mediaErrorSentence('el', 'no-embed'), /ενσωμάτωση/);
  assert.match(mediaErrorSentence('en', 'not-found'), /unavailable/i);
  assert.match(mediaErrorSentence('el', 'no-referrer'), /YouTube/);
  for (const kind of ['not-found', 'no-embed', 'invalid', 'html5', 'no-referrer', 'unknown']) {
    const sentence = mediaErrorSentence('en', kind);
    assert.equal(typeof sentence, 'string');
    assert.ok(sentence.length > 0);
    assert.doesNotMatch(sentence, /^error \d+/);
  }
});

test('a refused item still has somewhere to be watched', () => {
  assert.equal(watchUrl(item()), 'https://www.youtube.com/watch?v=dQw4w9WgXcQ');
  // The tool ships the canonical watch URL; it wins over the built one.
  assert.equal(watchUrl(item({ url: 'https://www.youtube.com/watch?v=X&t=9' })), 'https://www.youtube.com/watch?v=X&t=9');
  assert.equal(watchUrl(null), 'https://www.youtube.com');
});

test('denied tracks are skipped by forward/back navigation and queue wrap', () => {
  let queue = state({ items: [item({ id: 'a' }), item({ id: 'b' }), item({ id: 'c' })], index: 0, seq: 0 });
  queue = rejectMediaTrack(queue, 0);
  assert.equal(queue.index, 1);
  assert.equal(queue.paused, false);
  queue = rejectMediaTrack(queue, 1);
  assert.equal(queue.index, 2);
  assert.equal(availableMediaIndex(queue, 1), 2);
  assert.equal(availableMediaIndex(queue, -1), 2);
  assert.deepEqual(queue.unavailableIds, ['a', 'b']);
  queue = rejectMediaTrack(queue, 2);
  assert.equal(queue.paused, true);
  assert.equal(availableMediaIndex(queue, 1), null);
  assert.equal(availableMediaIndex(queue, -1), null);
});

test('a stale rejection cannot skip the current song', () => {
  const queue = state({ index: 1 });
  assert.equal(rejectMediaTrack(queue, 0), queue);
});
