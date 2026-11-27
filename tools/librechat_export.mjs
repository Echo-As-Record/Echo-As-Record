#!/usr/bin/env node
// Read-only, version-checked LibreChat exporter for one personally scoped account.
//
// Emits the NDJSON stream `ear-librechat-export/1` on stdout; the Ear adapter
// (`librechat-adapter`) is the only intended consumer. Nothing here writes to LibreChat.
//
// Safety properties (see services/adapters/ai-docs/librechat-source.md):
//  * every query carries an equality filter on the configured account id; there is no
//    "all users" mode. Hidden fields (privateText, subagent*) are never projected.
//  * find() only, small batches, maxTimeMS, optional pause between batches. No aggregation,
//    no expensive sample/group pipeline.
//  * attachment bytes come only from the local `/uploads/<account>/<name>` and
//    `/images/<account>/<name>` stores, opened with O_NOFOLLOW under a fixed root. URLs and any
//    other storage source are never fetched; they are reported as unavailable.
//  * the LibreChat version is read from the application directory and must be on the tested list.
//
// Usage (inside the same image as the running LibreChat API, see tools/librechat_export_docker.sh):
//   EAR_EXPORT_MONGO_URI=... node librechat_export.mjs --user-id <24 hex> --request-stdin
//   node librechat_export.mjs --print-setup --user-id <24 hex> # creates no account itself
//
// Exit codes: 0 complete stream; 2 usage/config; 3 version or schema mismatch; 4 runtime failure.

import { createHash } from 'node:crypto';
import { constants as fsc } from 'node:fs';
import fsp from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

export const FORMAT = 'ear-librechat-export/1';
export const REQUEST_FORMAT = 'ear-librechat-export-request/1';
export const EXPORTER_VERSION = '1';
/** LibreChat releases whose schema this exporter was checked against. */
export const SUPPORTED_LIBRECHAT_VERSIONS = ['0.8.9'];

const OID = /^[0-9a-f]{24}$/;
const MEDIA_TYPE = /^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}\/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$/i;
const LOCAL_PATH = /^\/(uploads|images)\/([0-9a-f]{24})\/([^/\\\0]{1,255})$/;
const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$/;
const ROOT_PARENT = '00000000-0000-0000-0000-000000000000';

export const DEFAULTS = Object.freeze({
  maxFileBytes: 64 * 1024 * 1024,
  maxTextChars: 8_000_000,
  maxAuxChars: 64_000,
  maxToolChars: 2_000,
  maxToolCalls: 50,
  maxAttachmentTextChars: 20_000,
  chunkBytes: 1024 * 1024,
  batchSize: 20,
  maxTimeMs: 30_000,
  pauseMs: 25,
  overlapSeconds: 120,
});

export class ExportError extends Error {
  constructor(code, message, exitCode = 4) {
    super(message);
    this.code = code;
    this.exitCode = exitCode;
  }
}

// ---------------------------------------------------------------------------------------------
// small helpers

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
const str = (v) => (typeof v === 'string' ? v : null);

export function iso(value) {
  if (value === null || value === undefined) return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export function bound(text, limit) {
  if (typeof text !== 'string') return ['', false];
  if (text.length <= limit) return [text, false];
  let cut = text.slice(0, limit);
  const last = cut.charCodeAt(cut.length - 1);
  if (last >= 0xd800 && last <= 0xdbff) cut = cut.slice(0, -1); // never split a surrogate pair
  return [cut, true];
}

export function semver(version) {
  const m = /^v?(\d+\.\d+\.\d+)(?:[-+].*)?$/.exec(String(version ?? '').trim());
  return m ? m[1] : null;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function asText(value) {
  if (typeof value === 'string') return value;
  if (value === null || value === undefined) return '';
  return JSON.stringify(value) ?? '';
}

export function accountRef(userId) {
  return createHash('sha256').update(`ear-librechat-account:${userId}`).digest('hex').slice(0, 16);
}

// ---------------------------------------------------------------------------------------------
// message normalisation (pure; unit-testable)

function textOf(part) {
  if (typeof part.text === 'string') return part.text;
  if (isObj(part.text) && typeof part.text.value === 'string') return part.text.value;
  return null;
}

/** Returns a local `/uploads|/images` path referenced by an image_url part, never other URLs. */
function localImagePath(part) {
  const raw = isObj(part.image_url) ? part.image_url.url : part.image_url;
  if (typeof raw !== 'string') return null;
  const clean = raw.split(/[?#]/, 1)[0];
  return LOCAL_PATH.test(clean) ? clean : null;
}

export function normalizeMessage(doc, limits = DEFAULTS) {
  const parts = Array.isArray(doc.content) ? doc.content.filter(isObj) : [];
  const visible = [];
  const reasoning = [];
  const toolCalls = [];
  const partTypes = {};
  const imageFileIds = [];
  const imagePaths = [];
  for (const part of parts) {
    const type = typeof part.type === 'string' ? part.type : 'unknown';
    partTypes[type] = (partTypes[type] ?? 0) + 1;
    if (type === 'text') {
      const t = textOf(part);
      if (t !== null && t !== '') visible.push(t);
    } else if (type === 'think') {
      const t = str(part.think) ?? textOf(part);
      if (t) reasoning.push(t);
    } else if (type === 'tool_call' && isObj(part.tool_call)) {
      if (toolCalls.length < limits.maxToolCalls) {
        const [args, argsCut] = bound(asText(part.tool_call.args), limits.maxToolChars);
        const [output, outCut] = bound(asText(part.tool_call.output), limits.maxToolChars);
        toolCalls.push({
          name: bound(str(part.tool_call.name) ?? '', 200)[0],
          args,
          output,
          truncated: argsCut || outCut,
        });
      }
    } else if (type === 'image_file' && isObj(part.image_file) && str(part.image_file.file_id)) {
      imageFileIds.push(part.image_file.file_id);
    } else if (type === 'image_url') {
      const p = localImagePath(part);
      if (p) imagePaths.push(p);
    }
  }
  let text;
  let textSource;
  if (visible.length > 0) {
    text = visible.join('\n\n');
    textSource = 'content';
  } else {
    text = str(doc.text) ?? '';
    textSource = 'text';
  }
  const [boundedText, textTruncated] = bound(text, limits.maxTextChars);
  const [reasoningText, reasoningTruncated] = bound(reasoning.join('\n\n'), limits.maxAuxChars);
  const refs = [];
  const addRef = (fileId, kind, filename, filepath) => {
    if (typeof fileId === 'string' && fileId && !refs.some((r) => r.file_id === fileId)) {
      refs.push({ file_id: fileId, kind, filename: str(filename), filepath: str(filepath) });
    }
  };
  for (const f of Array.isArray(doc.files) ? doc.files : []) {
    if (isObj(f)) addRef(f.file_id, 'upload', f.filename, f.filepath);
  }
  for (const f of Array.isArray(doc.attachments) ? doc.attachments : []) {
    if (isObj(f)) addRef(f.file_id, 'generated', f.filename, f.filepath);
  }
  for (const id of imageFileIds) addRef(id, 'inline', null, null);
  const parent = str(doc.parentMessageId);
  return {
    conversation_id: doc.conversationId,
    message_id: doc.messageId,
    parent_message_id: parent && parent !== ROOT_PARENT ? parent : null,
    role: doc.isCreatedByUser === true ? 'user' : 'assistant',
    sender: str(doc.sender) === null ? null : bound(doc.sender, 200)[0],
    model: str(doc.model),
    endpoint: str(doc.endpoint),
    created_at: iso(doc.createdAt),
    updated_at: iso(doc.updatedAt ?? doc.createdAt),
    text: boundedText,
    text_source: textSource,
    text_truncated: textTruncated,
    reasoning: reasoningText,
    reasoning_truncated: reasoningTruncated,
    tool_calls: toolCalls,
    part_types: partTypes,
    refs,
    image_paths: imagePaths,
    error: doc.error === true,
    unfinished: doc.unfinished === true,
    finish_reason: str(doc.finish_reason),
    is_temporary: doc.isTemporary === true,
  };
}

export function validMessageDoc(doc, userId) {
  return (
    isObj(doc) &&
    typeof doc.messageId === 'string' &&
    doc.messageId !== '' &&
    typeof doc.conversationId === 'string' &&
    doc.conversationId !== '' &&
    doc.user === userId &&
    iso(doc.createdAt) !== null
  );
}

export function normalizeConversation(doc) {
  const tags = (Array.isArray(doc.tags) ? doc.tags : [])
    .filter((t) => typeof t === 'string')
    .slice(0, 50)
    .map((t) => bound(t, 200)[0]);
  return {
    conversation_id: doc.conversationId,
    title: bound(str(doc.title) ?? '', 2000)[0],
    endpoint: str(doc.endpoint),
    model: str(doc.model),
    agent_id: str(doc.agent_id),
    tags,
    created_at: iso(doc.createdAt),
    updated_at: iso(doc.updatedAt ?? doc.createdAt),
    is_temporary: doc.isTemporary === true,
    is_archived: doc.isArchived === true,
    expired_at: iso(doc.expiredAt),
  };
}

export function validConversationDoc(doc, userId) {
  return (
    isObj(doc) &&
    typeof doc.conversationId === 'string' &&
    doc.conversationId !== '' &&
    doc.user === userId &&
    iso(doc.createdAt) !== null
  );
}

// ---------------------------------------------------------------------------------------------
// local file access

/** `/uploads|/images/<account>/<name>` (query/hash stripped) for exactly this account, else null. */
export function parseLocalPath(filepath, userId) {
  if (typeof filepath !== 'string') return null;
  const m = LOCAL_PATH.exec(filepath.split(/[?#]/, 1)[0]);
  if (!m || m[2] !== userId || m[3] === '.' || m[3] === '..') return null;
  return { store: m[1], name: m[3] };
}

async function plainDirectory(p) {
  const st = await fsp.lstat(p);
  if (st.isSymbolicLink() || !st.isDirectory()) throw new ExportError('bad_root', 'not a directory');
}

export class FileStore {
  constructor({ uploadsRoot, imagesRoot, userId, maxBytes }) {
    this.roots = { uploads: uploadsRoot, images: imagesRoot };
    this.userId = userId;
    this.maxBytes = maxBytes;
  }

  /** Streams one file; returns {ok:true,size,sha256} or {ok:false,reason,started?}. */
  async stream(parsed, onChunk, chunkBytes = DEFAULTS.chunkBytes) {
    const root = this.roots[parsed.store];
    if (!root) return { ok: false, reason: 'bad_path' };
    let handle = null;
    let started = false;
    try {
      const dir = path.join(root, this.userId);
      await plainDirectory(root);
      await plainDirectory(dir);
      handle = await fsp.open(path.join(dir, parsed.name), fsc.O_RDONLY | fsc.O_NOFOLLOW | fsc.O_NONBLOCK);
      const before = await handle.stat();
      if (!before.isFile() || before.nlink > 1) return { ok: false, reason: 'not_regular' };
      if (before.size > this.maxBytes) return { ok: false, reason: 'too_large' };
      const hash = createHash('sha256');
      const buf = Buffer.allocUnsafe(chunkBytes);
      let total = 0;
      for (;;) {
        const { bytesRead } = await handle.read(buf, 0, buf.length, null);
        if (bytesRead === 0) break;
        total += bytesRead;
        if (total > this.maxBytes) return { ok: false, reason: 'too_large', started };
        const chunk = Buffer.from(buf.subarray(0, bytesRead));
        hash.update(chunk);
        started = true;
        await onChunk(chunk);
      }
      const after = await handle.stat();
      if (after.size !== total || after.mtimeMs !== before.mtimeMs) {
        return { ok: false, reason: 'changed_during_read', started };
      }
      return { ok: true, size: total, sha256: hash.digest('hex') };
    } catch (err) {
      if (err instanceof ExportError) return { ok: false, reason: 'bad_path', started };
      if (err && err.code === 'ENOENT') return { ok: false, reason: 'missing_on_disk', started };
      if (err && (err.code === 'ELOOP' || err.code === 'EMLINK')) {
        return { ok: false, reason: 'not_regular', started };
      }
      return { ok: false, reason: 'read_error', started };
    } finally {
      if (handle) await handle.close().catch(() => {});
    }
  }
}

// ---------------------------------------------------------------------------------------------
// database abstraction: real MongoDB, or a JSON fixture used by the tests

const VIEWS = Object.freeze({
  conversations: "ear_export_conversations",
  messages: "ear_export_messages",
  files: "ear_export_files",
});

export class MongoDb {
  constructor(client, db, ObjectId) {
    this.client = client;
    this.db = db;
    this.ObjectId = ObjectId;
  }

  oid(hex) {
    return new this.ObjectId(hex);
  }

  async *find(collection, filter, { projection, sort, batchSize, maxTimeMS, limit } = {}) {
    // Only pass options that are actually set; the driver treats `undefined` inconsistently.
    const options = Object.fromEntries(
      Object.entries({ projection, sort, batchSize, maxTimeMS, limit }).filter(([, v]) => v !== undefined),
    );
    if (!Object.hasOwn(VIEWS, collection)) throw new Error("unsupported export view");
    const cursor = this.db.collection(VIEWS[collection]).find(filter, options);
    try {
      for await (const doc of cursor) yield doc;
    } finally {
      await cursor.close().catch(() => {});
    }
  }

  async close() {
    await this.client.close();
  }
}

function tsOf(v) {
  return v instanceof Date ? v.getTime() : new Date(v).getTime();
}

function matches(doc, filter) {
  for (const [key, cond] of Object.entries(filter)) {
    if (key === '$or') {
      if (!cond.some((f) => matches(doc, f))) return false;
      continue;
    }
    const value = doc[key];
    if (isObj(cond) && !(cond instanceof Date)) {
      for (const [op, arg] of Object.entries(cond)) {
        if (op === '$gte' && !(tsOf(value) >= tsOf(arg))) return false;
        else if (op === '$gt' && !(tsOf(value) > tsOf(arg))) return false;
        else if (op === '$in' && !arg.includes(value)) return false;
        else if (!['$gte', '$gt', '$in'].includes(op)) throw new Error(`fixture: operator ${op}`);
      }
    } else if (String(value) !== String(cond)) {
      return false;
    }
  }
  return true;
}

export class FixtureDb {
  constructor(data) {
    this.data = data;
    this.queries = [];
  }

  oid(hex) {
    return hex;
  }

  async *find(collection, filter, { sort } = {}) {
    if (!('user' in filter)) throw new Error('fixture: query without account filter');
    this.queries.push({ collection, filter });
    let docs = (this.data[collection] ?? []).filter((d) => matches(d, filter));
    if (sort) {
      const keys = Object.entries(sort);
      const val = (d, k) => (k === 'createdAt' || k === 'updatedAt' ? tsOf(d[k]) : String(d[k] ?? ''));
      docs = [...docs].sort((a, b) => {
        for (const [k, dir] of keys) {
          if (val(a, k) < val(b, k)) return -dir;
          if (val(a, k) > val(b, k)) return dir;
        }
        return 0;
      });
    }
    for (const d of docs) yield structuredClone(d);
  }

  async close() {}
}

export async function openMongo(uri, appDir) {
  const req = createRequire(path.join(appDir, 'package.json'));
  const { MongoClient, ObjectId } = req('mongodb');
  const client = new MongoClient(uri, {
    appName: 'ear-librechat-export',
    maxPoolSize: 2,
    serverSelectionTimeoutMS: 10_000,
    retryReads: false,
  });
  await client.connect();
  return new MongoDb(client, client.db(), ObjectId);
}

// ---------------------------------------------------------------------------------------------
// the export

const MESSAGE_PROJECTION = {
  _id: 0,
  messageId: 1,
  conversationId: 1,
  user: 1,
  parentMessageId: 1,
  sender: 1,
  text: 1,
  isCreatedByUser: 1,
  model: 1,
  endpoint: 1,
  error: 1,
  unfinished: 1,
  finish_reason: 1,
  createdAt: 1,
  updatedAt: 1,
  isTemporary: 1,
  'content.type': 1,
  'content.text': 1,
  'content.think': 1,
  'content.tool_call.name': 1,
  'content.tool_call.args': 1,
  'content.tool_call.output': 1,
  'content.image_file.file_id': 1,
  'content.image_url.url': 1,
  'files.file_id': 1,
  'files.filename': 1,
  'files.filepath': 1,
  'attachments.file_id': 1,
  'attachments.filename': 1,
  'attachments.filepath': 1,
};
const CONVERSATION_PROJECTION = {
  _id: 0,
  conversationId: 1,
  user: 1,
  title: 1,
  endpoint: 1,
  model: 1,
  agent_id: 1,
  tags: 1,
  createdAt: 1,
  updatedAt: 1,
  isTemporary: 1,
  isArchived: 1,
  expiredAt: 1,
};
const FILE_PROJECTION = {
  _id: 0,
  user: 1,
  file_id: 1,
  conversationId: 1,
  messageId: 1,
  filename: 1,
  filepath: 1,
  bytes: 1,
  type: 1,
  text: 1,
  textFormat: 1,
  status: 1,
  source: 1,
  context: 1,
  createdAt: 1,
  updatedAt: 1,
};
const OWNER_PROJECTION = { _id: 0, messageId: 1, conversationId: 1, user: 1, createdAt: 1 };

export async function readAppVersions(appDir) {
  const read = async (p) => JSON.parse(await fsp.readFile(p, 'utf8'));
  let app;
  try {
    app = await read(path.join(appDir, 'package.json'));
  } catch {
    throw new ExportError('version_unreadable', 'cannot read the LibreChat package.json', 3);
  }
  let schemas = null;
  try {
    schemas = (await read(path.join(appDir, 'packages/data-schemas/package.json'))).version ?? null;
  } catch {
    // optional; informational only
  }
  return { app: semver(app.version), dataSchemas: schemas };
}

export function checkVersion(found, allowed) {
  if (!found || !allowed.includes(found)) {
    throw new ExportError(
      'unsupported_version',
      `LibreChat ${found ?? 'unknown'} is not on the tested list [${allowed.join(', ')}]`,
      3,
    );
  }
}

export async function runExport({ db, files, write, options }) {
  const o = { ...DEFAULTS, ...options };
  const userId = o.userId;
  if (!OID.test(userId ?? '')) throw new ExportError('bad_user', 'user id must be 24 hex digits', 2);
  const since = o.since ? new Date(o.since) : null;
  if (o.mode === 'incremental' && (!since || Number.isNaN(since.getTime()))) {
    throw new ExportError('bad_request', 'incremental export needs a valid since', 2);
  }
  const startedAt = new Date();
  const find = (collection, filter, extra) =>
    db.find(collection, filter, { batchSize: o.batchSize, maxTimeMS: o.maxTimeMs, ...extra });
  const pace = () => (o.pauseMs > 0 ? sleep(o.pauseMs) : Promise.resolve());

  // The `user` of conversations/messages is a string; files.user is an ObjectId.
  const fileUser = db.oid(userId);

  const counts = {
    conversations: 0,
    messages: 0,
    attachments: 0,
    blobs: 0,
    blob_bytes: 0,
    skipped: {},
  };
  const skip = (reason) => {
    counts.skipped[reason] = (counts.skipped[reason] ?? 0) + 1;
  };

  await write({
    t: 'header',
    format: FORMAT,
    exporter_version: EXPORTER_VERSION,
    librechat_version: o.librechatVersion,
    data_schemas_version: o.dataSchemasVersion ?? null,
    mode: o.mode,
    since: since ? since.toISOString() : null,
    started_at: startedAt.toISOString(),
    account_ref: accountRef(userId),

  });

  let blobSeq = 0;
  /** file key -> {blob_id,size,sha256} | {unavailable} for this run */
  const fileCache = new Map();
  const attachedFiles = new Set();

  async function resolveFile(doc) {
    const key = `${doc.file_id}|${iso(doc.updatedAt) ?? ''}|${doc.bytes ?? ''}`;
    const cached = fileCache.get(key);
    if (cached) return cached;
    let result;
    const source = str(doc.source);
    if (source !== null && source !== 'local') {
      result = { unavailable: 'unsupported_source' };
    } else {
      const parsed = parseLocalPath(doc.filepath, userId);
      if (!parsed) {
        result = { unavailable: 'bad_path' };
      } else {
        const blobId = ++blobSeq;
        let seq = 0;
        const outcome = await files.stream(parsed, async (chunk) => {
          await write({ t: 'blob', blob_id: blobId, seq: seq++, b64: chunk.toString('base64') });
        });
        if (outcome.ok) {
          await write({ t: 'blob_end', blob_id: blobId, size: outcome.size, sha256: outcome.sha256 });
          counts.blobs += 1;
          counts.blob_bytes += outcome.size;
          result = { blob_id: blobId, size: outcome.size, sha256: outcome.sha256 };
        } else {
          if (outcome.started) await write({ t: 'blob_abort', blob_id: blobId });
          result = { unavailable: outcome.reason };
        }
      }
    }
    fileCache.set(key, result);
    return result;
  }

  function fileMeta(doc, ref) {
    // `text/plain; charset=utf-8` -> `text/plain`
    const type = str(doc?.type)?.split(';', 1)[0].trim() ?? null;
    const usable = doc && typeof doc.text === 'string' && doc.textFormat !== 'html';
    const [text, cut] = usable ? bound(doc.text, o.maxAttachmentTextChars) : [null, false];
    return {
      file_id: doc?.file_id ?? ref.file_id,
      filename: bound(str(doc?.filename) ?? ref.filename ?? '', 500)[0],
      media_type: type && MEDIA_TYPE.test(type) ? type.toLowerCase() : 'application/octet-stream',
      declared_bytes: Number.isFinite(doc?.bytes) ? doc.bytes : null,
      source: str(doc?.source),
      context: str(doc?.context) === null ? null : bound(doc.context, 100)[0],
      status: str(doc?.status),
      created_at: iso(doc?.createdAt),
      updated_at: iso(doc?.updatedAt ?? doc?.createdAt),
      text,
      text_truncated: cut,
      text_format: str(doc?.textFormat),
    };
  }

  async function emitAttachment(message, ref, doc) {
    counts.attachments += 1;
    const base = {
      t: 'attachment',
      conversation_id: message.conversation_id,
      message_id: message.message_id,
      message_created_at: message.created_at,
      ref_kind: ref.kind,
      ...fileMeta(doc, ref),
    };
    if (!doc) {
      await write({ ...base, blob_id: null, unavailable: 'no_file_record' });
      return;
    }
    attachedFiles.add(doc.file_id);
    const resolved = await resolveFile(doc);
    await write({
      ...base,
      blob_id: resolved.blob_id ?? null,
      unavailable: resolved.unavailable ?? null,
    });
  }

  /** Resolve the file documents of one message (account-scoped), in reference order. */
  async function lookupFiles(message) {
    const ids = message.refs.map((r) => r.file_id);
    const paths = [...new Set(message.image_paths)];
    const byId = new Map();
    if (ids.length > 0 || paths.length > 0) {
      const or = [];
      if (ids.length > 0) or.push({ file_id: { $in: ids } });
      if (paths.length > 0) or.push({ filepath: { $in: paths } });
      const filter = { user: fileUser, ...(or.length === 1 ? or[0] : { $or: or }) };
      for await (const doc of find('files', filter, { projection: FILE_PROJECTION })) {
        if (!isObj(doc) || typeof doc.file_id !== 'string' || String(doc.user) !== userId) continue;
        const known = byId.get(doc.file_id);
        if (known) skip('duplicate_file_record');
        if (!known || tsOf(doc.updatedAt ?? 0) >= tsOf(known.updatedAt ?? 0)) {
          byId.set(doc.file_id, doc);
        }
      }
    }
    // inline image paths resolve to file ids through their stored filepath
    const refs = [...message.refs];
    for (const p of paths) {
      const doc = [...byId.values()].find((d) => str(d.filepath)?.split(/[?#]/, 1)[0] === p);
      if (doc && !refs.some((r) => r.file_id === doc.file_id)) {
        refs.push({ file_id: doc.file_id, kind: 'inline', filename: null, filepath: p });
      }
    }
    return { refs, byId };
  }

  async function inlineImages(doc, message) {
    const ids = [];
    const parts = Array.isArray(doc.content) ? doc.content : [];
    for (const [position, part] of parts.entries()) {
      if (!isObj(part) || part.type !== 'image_url') continue;
      const raw = isObj(part.image_url) ? part.image_url.url : part.image_url;
      if (typeof raw !== 'string' || !raw.startsWith('data:')) continue;
      const i = raw.indexOf(',');
      const mime = /^data:(image\/(?:png|jpeg|gif|webp));base64$/i.exec(raw.slice(0, i));
      if (i < 0 || !mime || raw.length - i - 1 > Math.ceil(o.maxFileBytes / 3) * 4) {
        skip('unsupported_inline_image');
        continue;
      }
      const encoded = raw.slice(i + 1);
      const body = Buffer.from(encoded, 'base64');
      if (!body.length || body.length > o.maxFileBytes || body.toString('base64') !== encoded) {
        skip('invalid_inline_image');
        continue;
      }
      const sha256 = createHash('sha256').update(body).digest('hex');
      const fileId = `ear-inline-${position}-${sha256}`;
      const blobId = ++blobSeq;
      for (let offset = 0; offset < body.length; offset += o.chunkBytes) {
        await write({ t: 'blob', blob_id: blobId, seq: offset / o.chunkBytes,
          b64: body.subarray(offset, offset + o.chunkBytes).toString('base64') });
      }
      await write({ t: 'blob_end', blob_id: blobId, size: body.length, sha256 });
      counts.blobs += 1;
      counts.blob_bytes += body.length;
      counts.attachments += 1;
      ids.push(fileId);
      await write({ t: 'attachment', conversation_id: message.conversation_id,
        message_id: message.message_id, message_created_at: message.created_at,
        ref_kind: message.role === 'user' ? 'upload' : 'inline', file_id: fileId,
        media_type: mime[1].toLowerCase(), declared_bytes: body.length, source: 'inline_data',
        filename: `inline.${mime[1].split('/')[1].toLowerCase()}`, blob_id: blobId, unavailable: null });
    }
    return ids;
  }

  async function exportMessage(doc) {
    if (!validMessageDoc(doc, userId)) {
      skip('invalid_message');
      return;
    }
    const message = normalizeMessage(doc, o);
    const { refs, byId } = await lookupFiles(message);
    for (const ref of refs) await emitAttachment(message, ref, byId.get(ref.file_id) ?? null);
    const inline = await inlineImages(doc, message);
    const { refs: _refs, image_paths: _paths, ...wire } = message;
    await write({ t: 'message', ...wire, attachment_file_ids: [...refs.map((r) => r.file_id), ...inline] });
    counts.messages += 1;
  }

  // Pass 1: conversations and their messages -------------------------------------------------
  for await (const conv of find(
    'conversations',
    { user: userId },
    { projection: CONVERSATION_PROJECTION, sort: { _id: 1 } },
  )) {
    if (!validConversationDoc(conv, userId)) {
      skip('invalid_conversation');
      continue;
    }
    const metaChanged =
      o.mode === 'full' || tsOf(conv.updatedAt ?? conv.createdAt) >= since.getTime();
    let announced = false;
    const announce = async () => {
      if (announced) return;
      announced = true;
      await write({ t: 'conversation', ...normalizeConversation(conv) });
      counts.conversations += 1;
    };
    if (metaChanged) await announce();
    const messageFilter = {
      conversationId: conv.conversationId,
      user: userId,
      ...(o.mode === 'incremental' ? { updatedAt: { $gte: since } } : {}),
    };
    let exported = 0;
    for await (const doc of find('messages', messageFilter, {
      projection: MESSAGE_PROJECTION,
    })) {
      await announce();
      await exportMessage(doc);
      exported += 1;
      await pace();
    }
    if (announced) {
      await write({ t: 'conversation_end', conversation_id: conv.conversationId, messages: exported });
    }
    await pace();
  }

  // Archive files even when they have never been attached to a message. The synthetic
  // labels for those files are explicitly marked unlinked; they are not source sessions.
  for await (const doc of find(
    'files',
    { user: fileUser, ...(o.mode === 'incremental' ? { updatedAt: { $gte: since } } : {}) },
    { projection: FILE_PROJECTION },
  )) {
    if (!isObj(doc) || typeof doc.file_id !== 'string' || !doc.file_id ||
        String(doc.user) !== userId) {
      skip('invalid_file');
      continue;
    }
    if (attachedFiles.has(doc.file_id)) continue;
    let owner = null;
    if (typeof doc.messageId === 'string' && doc.messageId) {
      for await (const m of find(
        'messages',
        { messageId: doc.messageId, user: userId },
        { projection: OWNER_PROJECTION, limit: 1 },
      )) {
        if (validMessageDoc(m, userId)) owner = m;
      }
    }
    const fileTime = iso(doc.createdAt ?? doc.updatedAt);
    if (!owner && !fileTime) {
      skip('file_time_missing');
      continue;
    }
    const stub = {
      conversation_id: owner?.conversationId ?? (str(doc.conversationId) || 'ear-unlinked-files'),
      message_id: owner?.messageId ?? 'ear-unlinked-file',
      created_at: owner ? iso(owner.createdAt) : fileTime,
    };
    await emitAttachment(stub, { file_id: doc.file_id, kind: owner ? 'generated' : 'unlinked', filename: null }, doc);
    await pace();
  }

  await write({
    t: 'end',
    complete: true,
    counts,
    // With account-only views, never inspect other accounts to distinguish an empty
    // account from an owner binding mistake. In either case do not advance a full cursor.
    suspect_empty: o.mode === 'full' && counts.conversations === 0 && counts.attachments === 0,
    watermark: new Date(startedAt.getTime() - o.overlapSeconds * 1000).toISOString(),
    finished_at: new Date().toISOString(),
  });
}

// ---------------------------------------------------------------------------------------------
// CLI

export function setupText(userId) {
  if (!OID.test(userId ?? '')) throw new ExportError('bad_user', 'set the fixed 24-hex account ID', 2);
  const projections = { conversations: CONVERSATION_PROJECTION, messages: MESSAGE_PROJECTION, files: FILE_PROJECTION };
  const views = Object.entries(VIEWS).map(([source, view]) => {
    const owner = source === 'files' ? `ObjectId(${JSON.stringify(userId)})` : JSON.stringify(userId);
    // _id remains private on the wire but is required for the query's stable sorting.
    const { _id, ...projection } = projections[source];
    return `lc.createView(${JSON.stringify(view)}, ${JSON.stringify(source)}, [{$match:{user:${owner}}}, {$project:${JSON.stringify(projection)}}]);`;
  });
  return `// Run as a MongoDB administrator. Views bind this credential to one owner.
const lc = db.getSiblingDB('LibreChat');
${views.join('\n')}
lc.createRole({role:'earLibreChatExport', privileges:${JSON.stringify(Object.values(VIEWS))}.map(collection => ({resource:{db:'LibreChat',collection},actions:['find']})), roles:[]});
lc.createUser({user:'ear_export',pwd:passwordPrompt(),roles:[{role:'earLibreChatExport',db:'LibreChat'}]});
// Store the connection URI in the private exporter configuration, never in git.
`;
}

function parseArgs(argv) {
  const opts = {};
  const valued = new Set([
    '--user-id',
    '--since',
    '--app-dir',
    '--uploads-root',
    '--images-root',
    '--fixture',
    '--allow-version',
    '--max-file-bytes',
    '--batch-size',
    '--pause-ms',
    '--max-time-ms',
    '--overlap-seconds',
    '--mongo-uri-env',
  ]);
  const flags = new Set(['--full', '--request-stdin', '--print-setup', '--help']);
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (flags.has(a)) {
      opts[a] = true;
    } else if (valued.has(a) && i + 1 < argv.length) {
      i += 1;
      if (a === '--allow-version') (opts[a] ??= []).push(argv[i]);
      else opts[a] = argv[i];
    } else {
      throw new ExportError('usage', 'unknown or incomplete argument', 2);
    }
  }
  return opts;
}

async function readStdinJson(stdin) {
  const chunks = [];
  let size = 0;
  for await (const chunk of stdin) {
    size += chunk.length;
    if (size > 4096) throw new ExportError('bad_request', 'request too large', 2);
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    throw new ExportError('bad_request', 'request is not JSON', 2);
  }
}

function integer(name, value, fallback, { min = 1, max = Number.MAX_SAFE_INTEGER } = {}) {
  if (value === undefined) return fallback;
  const n = Number(value);
  if (!Number.isInteger(n) || n < min || n > max) {
    throw new ExportError('usage', `${name} out of range`, 2);
  }
  return n;
}

export async function main(
  argv,
  { stdout = process.stdout, stdin = process.stdin, env = process.env } = {},
) {
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (err) {
    process.stderr.write(`${err.code ?? 'error'}: ${err.message}\n`);
    return 2;
  }
  if (opts['--help'] || opts['--print-setup']) {
    if (opts['--print-setup']) {
      try { stdout.write(setupText(opts['--user-id'] ?? env.EAR_EXPORT_USER_ID)); }
      catch (e) { return e instanceof ExportError ? e.exitCode : 2; }
    } else stdout.write(`${FORMAT} exporter; see the file header.\n`);
    return 0;
  }
  const write = async (obj) => {
    const line = `${JSON.stringify(obj)}\n`;
    if (!stdout.write(line)) await new Promise((resolve) => stdout.once('drain', resolve));
  };
  let db = null;
  try {
    const appDir = opts['--app-dir'] ?? '/app';
    const versions = await readAppVersions(appDir);
    checkVersion(versions.app, [
      ...SUPPORTED_LIBRECHAT_VERSIONS,
      ...(opts['--allow-version'] ?? []),
    ]);

    let request = { mode: opts['--full'] ? 'full' : 'incremental', since: opts['--since'] ?? null };
    if (opts['--request-stdin']) {
      const body = await readStdinJson(stdin);
      if (!isObj(body) || body.format !== REQUEST_FORMAT) {
        throw new ExportError('bad_request', 'wrong request format', 2);
      }
      request = { mode: body.mode, since: body.since ?? null };
    }
    if (!['full', 'incremental'].includes(request.mode)) {
      throw new ExportError('bad_request', 'mode must be full or incremental', 2);
    }
    if (request.since !== null && !(typeof request.since === 'string' && ISO.test(request.since))) {
      throw new ExportError('bad_request', 'since must be an RFC 3339 UTC timestamp', 2);
    }
    const userId = opts['--user-id'] ?? env.EAR_EXPORT_USER_ID;
    if (!OID.test(userId ?? '')) throw new ExportError('bad_user', 'user id must be 24 hex digits', 2);

    if (opts['--fixture']) {
      db = new FixtureDb(JSON.parse(await fsp.readFile(opts['--fixture'], 'utf8')));
    } else {
      const uri = env[opts['--mongo-uri-env'] ?? 'EAR_EXPORT_MONGO_URI'];
      if (!uri) throw new ExportError('usage', 'EAR_EXPORT_MONGO_URI is not set', 2);
      db = await openMongo(uri, appDir);
    }
    const files = new FileStore({
      uploadsRoot: opts['--uploads-root'] ?? path.join(appDir, 'uploads'),
      imagesRoot: opts['--images-root'] ?? path.join(appDir, 'client/public/images'),
      userId,
      maxBytes: integer('--max-file-bytes', opts['--max-file-bytes'], DEFAULTS.maxFileBytes),
    });
    await runExport({
      db,
      files,
      write,
      options: {
        userId,
        mode: request.mode,
        since: request.since,
        librechatVersion: versions.app,
        dataSchemasVersion: versions.dataSchemas,
        batchSize: integer('--batch-size', opts['--batch-size'], DEFAULTS.batchSize, { max: 200 }),
        pauseMs: integer('--pause-ms', opts['--pause-ms'], DEFAULTS.pauseMs, { min: 0, max: 5000 }),
        maxTimeMs: integer('--max-time-ms', opts['--max-time-ms'], DEFAULTS.maxTimeMs),
        overlapSeconds: integer(
          '--overlap-seconds',
          opts['--overlap-seconds'],
          DEFAULTS.overlapSeconds,
          { min: 0, max: 86400 },
        ),
      },
    });
    return 0;
  } catch (err) {
    const known = err instanceof ExportError;
    // Fixed vocabulary only: never echo driver or document text.
    await write({
      t: 'error',
      code: known ? err.code : 'runtime_error',
      message: known ? err.message : 'export failed',
    }).catch(() => {});
    return known ? err.exitCode : 4;
  } finally {
    if (db) await db.close().catch(() => {});
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).then((code) => {
    process.exitCode = code;
  });
}
