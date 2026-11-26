// node --test tools/tests/librechat_export.test.mjs
import assert from 'node:assert/strict';
import fsp from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  FileStore,
  FixtureDb,
  bound,
  checkVersion,
  ExportError,
  main,
  normalizeMessage,
  parseLocalPath,
  runExport,
  semver,
} from '../librechat_export.mjs';

const UID = '111111111111111111111111';
const OTHER = '222222222222222222222222';

test('semver accepts the v-prefixed LibreChat version and rejects junk', () => {
  assert.equal(semver('v0.8.9'), '0.8.9');
  assert.equal(semver('0.8.9-rc.1'), '0.8.9');
  assert.equal(semver('latest'), null);
  assert.throws(() => checkVersion('0.9.0', ['0.8.9']), (e) => e instanceof ExportError && e.exitCode === 3);
  assert.throws(() => checkVersion(null, ['0.8.9']), ExportError);
  checkVersion('0.8.9', ['0.8.9']);
});

test('bound never splits a surrogate pair', () => {
  const [cut, truncated] = bound('a😀b', 2);
  assert.equal(truncated, true);
  assert.equal(cut, 'a');
  assert.deepEqual(bound('short', 10), ['short', false]);
});

test('parseLocalPath only accepts this account under /uploads or /images', () => {
  assert.deepEqual(parseLocalPath(`/uploads/${UID}/a b.txt`, UID), { store: 'uploads', name: 'a b.txt' });
  assert.deepEqual(parseLocalPath(`/images/${UID}/x.png?v=3#frag`, UID), { store: 'images', name: 'x.png' });
  for (const bad of [
    `/uploads/${OTHER}/a.txt`,
    `/uploads/${UID}/../${OTHER}/a.txt`,
    `/uploads/${UID}/..`,
    `/uploads/${UID}/a/b.txt`,
    `/uploads/${UID}/a\\b.txt`,
    `/other/${UID}/a.txt`,
    `https://example.com/uploads/${UID}/a.txt`,
    `s3://bucket/${UID}/a.txt`,
    `data:image/png;base64,AAAA`,
    `//uploads/${UID}/a.txt`,
    '',
    null,
    42,
  ]) {
    assert.equal(parseLocalPath(bad, UID), null, String(bad));
  }
});

test('normalizeMessage extracts visible text, bounded reasoning and tool calls', () => {
  const msg = normalizeMessage({
    messageId: 'm',
    conversationId: 'c',
    user: UID,
    createdAt: '2026-11-01T00:00:00Z',
    isCreatedByUser: false,
    parentMessageId: '00000000-0000-0000-0000-000000000000',
    content: [
      { type: 'think', think: 'why' },
      { type: 'text', text: { value: 'assistants api shape' } },
      { type: 'text', text: 'plain' },
      { type: 'tool_call', tool_call: { name: 'n'.repeat(500), args: { q: 1 }, output: 'o'.repeat(5000) } },
      { type: 'image_url', image_url: { url: `/images/${UID}/p.png?x=1` } },
      { type: 'image_url', image_url: { url: 'https://evil.example/p.png' } },
      { type: 'image_url', image_url: { url: 'data:image/png;base64,AAAA' } },
      { type: 'image_file', image_file: { file_id: 'f-img' } },
      'junk',
      null,
    ],
    text: 'ignored when content has text',
    privateText: 'SECRET',
  });
  assert.equal(msg.text, 'assistants api shape\n\nplain');
  assert.equal(msg.text_source, 'content');
  assert.equal(msg.parent_message_id, null);
  assert.equal(msg.role, 'assistant');
  assert.equal(msg.reasoning, 'why');
  assert.equal(msg.tool_calls[0].name.length, 200);
  assert.equal(msg.tool_calls[0].args, '{"q":1}');
  assert.equal(msg.tool_calls[0].output.length, 2000);
  assert.equal(msg.tool_calls[0].truncated, true);
  assert.deepEqual(msg.image_paths, [`/images/${UID}/p.png`]); // query string stripped
  assert.deepEqual(msg.refs.map((r) => r.file_id), ['f-img']);
  assert.equal(JSON.stringify(msg).includes('SECRET'), false);
  assert.equal(JSON.stringify(msg).includes('evil.example'), false);
});

test('normalizeMessage falls back to text and keeps user-message references', () => {
  const msg = normalizeMessage({
    messageId: 'm',
    conversationId: 'c',
    user: UID,
    createdAt: '2026-11-01T00:00:00Z',
    isCreatedByUser: true,
    parentMessageId: 'p1',
    text: 'hi',
    files: [{ file_id: 'a', filename: 'a.txt' }, { file_id: 'a' }, { nofile: true }],
    attachments: [{ file_id: 'b' }, { type: 'web_search' }],
  });
  assert.equal(msg.text, 'hi');
  assert.equal(msg.text_source, 'text');
  assert.equal(msg.parent_message_id, 'p1');
  assert.deepEqual(msg.refs.map((r) => [r.file_id, r.kind]), [['a', 'upload'], ['b', 'generated']]);
});

async function fixture() {
  const dir = await fsp.mkdtemp(path.join(os.tmpdir(), 'lc-export-'));
  for (const sub of ['uploads', 'images']) await fsp.mkdir(path.join(dir, sub, UID), { recursive: true });
  await fsp.writeFile(path.join(dir, 'uploads', UID, 'a.txt'), 'hello');
  await fsp.writeFile(path.join(dir, 'images', UID, 'p.png'), 'png-bytes');
  return dir;
}

function database(extra = {}) {
  const at = '2026-11-01T10:00:00.000Z';
  return new FixtureDb({
    conversations: [{ conversationId: 'c', user: UID, title: 't', createdAt: at, updatedAt: at }],
    messages: [
      {
        messageId: 'm',
        conversationId: 'c',
        user: UID,
        isCreatedByUser: true,
        text: 'hi',
        createdAt: at,
        updatedAt: at,
        files: [{ file_id: 'a' }],
        content: [{ type: 'image_url', image_url: { url: `/images/${UID}/p.png` } }],
      },
    ],
    files: [
      { user: UID, file_id: 'a', filename: 'a.txt', filepath: `/uploads/${UID}/a.txt`, bytes: 5, type: 'text/plain', createdAt: at, updatedAt: at },
      { user: UID, file_id: 'img', filename: 'p.png', filepath: `/images/${UID}/p.png`, bytes: 9, type: 'image/png', createdAt: at, updatedAt: at },
    ],
    ...extra,
  });
}

async function collect(db, root, options = {}) {
  const frames = [];
  const files = new FileStore({
    uploadsRoot: path.join(root, 'uploads'),
    imagesRoot: path.join(root, 'images'),
    userId: UID,
    maxBytes: 1 << 20,
  });
  await runExport({
    db,
    files,
    write: async (f) => {
      frames.push(f);
    },
    options: { userId: UID, mode: 'full', librechatVersion: '0.8.9', pauseMs: 0, ...options },
  });
  return frames;
}

test('every query is scoped to the account and frames are ordered blob → attachment → message', async () => {
  const root = await fixture();
  const db = database();
  const frames = await collect(db, root);
  assert.ok(db.queries.length > 0);
  for (const q of db.queries) assert.ok('user' in q.filter, JSON.stringify(q));
  const kinds = frames.map((f) => f.t);
  assert.equal(kinds[0], 'header');
  assert.equal(kinds.at(-1), 'end');
  const first = (t) => kinds.indexOf(t);
  assert.ok(first('blob') < first('attachment') && first('attachment') < first('message'));
  // image referenced only by its local path is resolved through the files collection
  const attachments = frames.filter((f) => f.t === 'attachment');
  assert.deepEqual(attachments.map((a) => a.file_id).sort(), ['a', 'img']);
  assert.ok(attachments.every((a) => a.blob_id !== null));
  const blobEnds = frames.filter((f) => f.t === 'blob_end');
  assert.equal(blobEnds.length, 2);
  const a = frames.find((f) => f.t === 'blob_end' && f.size === 5);
  assert.equal(a.sha256, '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'); // sha256("hello")
});

test('the same file referenced twice is streamed once per run', async () => {
  const root = await fixture();
  const at = '2026-11-01T10:00:01.000Z';
  const db = database();
  db.data.messages.push({
    messageId: 'm2', conversationId: 'c', user: UID, isCreatedByUser: true, text: 'again',
    createdAt: at, updatedAt: at, files: [{ file_id: 'a' }],
  });
  const frames = await collect(db, root);
  assert.equal(frames.filter((f) => f.t === 'blob_end').length, 2);
  assert.equal(frames.filter((f) => f.t === 'attachment' && f.file_id === 'a').length, 2);
});

test('a file that changes while it is read is aborted and reported', async () => {
  const root = await fixture();
  const big = path.join(root, 'uploads', UID, 'grow.bin');
  await fsp.writeFile(big, Buffer.alloc(3000, 1));
  const store = new FileStore({ uploadsRoot: path.join(root, 'uploads'), imagesRoot: path.join(root, 'images'), userId: UID, maxBytes: 1 << 20 });
  let calls = 0;
  const result = await store.stream({ store: 'uploads', name: 'grow.bin' }, async () => {
    calls += 1;
    if (calls === 1) await fsp.appendFile(big, 'more');
  }, 1000);
  assert.deepEqual([result.ok, result.reason, result.started], [false, 'changed_during_read', true]);
});

test('symlinks, hard links, directories and a swapped account directory are refused', async () => {
  const root = await fixture();
  const dir = path.join(root, 'uploads', UID);
  await fsp.symlink(path.join(dir, 'a.txt'), path.join(dir, 'sym.txt'));
  await fsp.link(path.join(dir, 'a.txt'), path.join(dir, 'hard.txt'));
  await fsp.mkdir(path.join(dir, 'sub'));
  const store = new FileStore({ uploadsRoot: path.join(root, 'uploads'), imagesRoot: path.join(root, 'images'), userId: UID, maxBytes: 1 << 20 });
  const reasons = {};
  for (const name of ['sym.txt', 'hard.txt', 'sub', 'absent.txt']) {
    reasons[name] = (await store.stream({ store: 'uploads', name }, async () => {})).reason;
  }
  assert.deepEqual(reasons, { 'sym.txt': 'not_regular', 'hard.txt': 'not_regular', sub: 'not_regular', 'absent.txt': 'missing_on_disk' });

  const swapped = await fixture();
  await fsp.rm(path.join(swapped, 'uploads', UID), { recursive: true });
  await fsp.symlink(path.join(swapped, 'images'), path.join(swapped, 'uploads', UID));
  const guarded = new FileStore({ uploadsRoot: path.join(swapped, 'uploads'), imagesRoot: path.join(swapped, 'images'), userId: UID, maxBytes: 1 << 20 });
  assert.equal((await guarded.stream({ store: 'uploads', name: 'p.png' }, async () => {})).reason, 'bad_path');
});

test('main refuses to run without a tested version or a valid account', async () => {
  const root = await fixture();
  const lines = [];
  const stdout = { write: (s) => (lines.push(s), true), once: () => {} };
  const stdin = (async function* () { yield Buffer.from('{"format":"ear-librechat-export-request/1","mode":"full","since":null}'); })();
  await fsp.writeFile(path.join(root, 'package.json'), JSON.stringify({ version: 'v9.9.9' }));
  const code = await main(['--request-stdin', '--user-id', UID, '--app-dir', root, '--fixture', path.join(root, 'none.json')], { stdout, stdin, env: {} });
  assert.equal(code, 3);
  assert.equal(JSON.parse(lines.join('')).code, 'unsupported_version');
});

test('print-setup grants find on three owner-bound views, not raw collections', async () => {
  const lines = [];
  assert.equal(await main(['--print-setup'], { env: {}, stdout: { write: () => true } }), 2);
  assert.equal(await main(['--print-setup', '--user-id', UID], { stdout: { write: (s) => (lines.push(s), true) } }), 0);
  const text = lines.join('');
  for (const name of ['conversations', 'messages', 'files']) assert.ok(text.includes(`"ear_export_${name}", "${name}"`));
  assert.ok(text.includes(`ObjectId("${UID}")`));
  assert.ok(!/collStats|readWrite|dbAdmin|\binsert\b|\bupdate\b|\bremove\b/.test(text));
});

test('the MongoDB path uses the app driver with find() only, account filters and whitelisted projections', async () => {
  const root = await fixture();
  await fsp.writeFile(path.join(root, 'package.json'), JSON.stringify({ name: 'LibreChat', version: 'v0.8.9' }));
  const driver = path.join(root, 'node_modules', 'mongodb');
  await fsp.mkdir(driver, { recursive: true });
  // A stand-in for the real driver: it only implements find(), so any
  // attempt to aggregate, update or delete would throw a TypeError and fail this test.
  await fsp.writeFile(
    path.join(driver, 'index.js'),
    `
class ObjectId { constructor(hex) { this.hex = hex; } toString() { return this.hex; } }
class MongoClient {
  constructor(uri, options) { globalThis.__driver = { uri, options, calls: [], closed: false }; }
  async connect() {}
  db() {
    return { collection: (name) => ({
      find: (filter, options) => {
        globalThis.__driver.calls.push({ name, filter, options });
        const docs = globalThis.__data[name] ?? [];
        return { async *[Symbol.asyncIterator]() { for (const d of docs) yield structuredClone(d); }, async close() {} };
      },
    }) };
  }
  async close() { globalThis.__driver.closed = true; }
}
module.exports = { MongoClient, ObjectId };
`,
  );
  globalThis.__data = Object.fromEntries(Object.entries(database().data).map(([k,v]) => ['ear_export_'+k,v]));
  const out = [];
  const stdout = { write: (s) => (out.push(s), true), once: () => {} };
  const stdin = (async function* () { yield Buffer.from('{"format":"ear-librechat-export-request/1","mode":"full","since":null}'); })();
  const code = await main(
    ['--request-stdin', '--user-id', UID, '--app-dir', root, '--uploads-root', path.join(root, 'uploads'), '--images-root', path.join(root, 'images'), '--pause-ms', '0'],
    { stdout, stdin, env: { EAR_EXPORT_MONGO_URI: 'mongodb://fake/LibreChat' } },
  );
  const frames = out.join('').split('\n').filter(Boolean).map((l) => JSON.parse(l));
  assert.equal(code, 0, JSON.stringify(frames.at(-1)));
  assert.equal(frames.at(-1).t, 'end');
  const { calls, closed } = globalThis.__driver;
  assert.equal(closed, true);
  assert.ok(calls.length > 0);
  for (const { name, filter, options } of calls) {
    assert.ok(['ear_export_conversations', 'ear_export_messages', 'ear_export_files'].includes(name), name);
    assert.ok('user' in filter, `${name} query without account filter`);
    // conversations/messages store the account as a string, files as an ObjectId
    if (name === 'ear_export_files') assert.equal(filter.user.constructor.name, 'ObjectId');
    else assert.equal(filter.user, UID);
    assert.equal(options.maxTimeMS, 30000);
    assert.ok(options.batchSize > 0 && options.batchSize <= 200);
    assert.ok(!Object.values(options).includes(undefined));
    assert.equal(options.projection._id, 0);
    for (const hidden of ['privateText', 'subagentTranscript', 'subagentContent', 'password', 'email']) {
      assert.ok(!(hidden in options.projection), hidden);
    }
  }
  const messageQueries = calls.filter((c) => c.name === 'ear_export_messages');
  assert.ok(messageQueries.every((c) => c.options.sort === undefined));
});

test('full export archives unlinked owned file bytes and safe inline images', async () => {
  const root = await fixture(), db = database();
  const at = '2026-11-22T10:00:00.000Z';
  await fsp.writeFile(path.join(root, 'uploads', UID, 'catalog.txt'), 'catalog');
  db.data.files.push({ user: UID, file_id: 'unlinked', filename: 'catalog.txt', filepath: `/uploads/${UID}/catalog.txt`, type:'text/plain', createdAt:at, updatedAt:at });
  db.data.messages[0].content.push({type:'image_url', image_url:{url:'data:image/png;base64,iVBORw0KGgo='}});
  const frames = await collect(db, root);
  const file = frames.find((row) => row.t === 'attachment' && row.file_id === 'unlinked');
  const inline = frames.find((row) => row.t === 'attachment' && row.source === 'inline_data');
  assert.equal(file.ref_kind, 'unlinked');
  assert.equal(file.message_created_at, at);
  assert.ok(frames.some((row) => row.t === 'blob_end' && row.blob_id === file.blob_id && row.size === 7));
  assert.ok(frames.some((row) => row.t === 'blob_end' && row.blob_id === inline.blob_id && row.size === 8));
  assert.ok(frames.some((row) => row.t === 'message' && row.attachment_file_ids.includes(inline.file_id)));
});
