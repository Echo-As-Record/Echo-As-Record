/* Standalone flat-workspace regression tests. All records/credentials below are synthetic.
 * No dependency on the recovered static/workspace prototype or its test helpers.
 * Reuses the recovered pure-logic/page cases against the actual standalone bundle.
 * Run: node --test services/api/tests/ui/workspace-flat.test.js
 */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const bundle = require('../../src/ear_api/static/workspace.js');
const STATIC = path.join(__dirname, '../../src/ear_api/static');

const flatFake = (() => {
const module = {exports:{}};
'use strict';
/*
 * 工作台测试用的最小假 DOM（无第三方依赖）。
 *
 * 目的不是模拟浏览器，而是让页面逻辑在 Node 里可运行，并且“违规即抛错”：
 *  - innerHTML / outerHTML / insertAdjacentHTML 一律抛错；
 *  - 记录所有 setAttribute 调用（doc.attrLog），测试据此断言没有事件处理器、style、src 等危险属性，
 *    以及 href 只指向站内；
 *  - 记录对 style 的任何访问（doc.styleLog）——CSP 禁止内联样式，页面只能用 class 与 SVG 属性；
 *  - localStorage / sessionStorage 的访问会抛错并记录（win.storageLog）。
 */
const fs = require('node:fs');

class FakeNode {
  constructor(doc, tag, ns) {
    this.ownerDocument = doc;
    this.nodeType = tag === '#text' ? 3 : 1;
    this.localName = tag;
    this.tagName = tag === '#text' ? '#text' : tag.toUpperCase();
    this.namespaceURI = ns || null;
    this.childNodes = [];
    this.parentNode = null;
    this.attrs = {};
    this.listeners = {};
    this._text = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = '';
    this.open = false;
    this.tabIndex = -1;
  }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get id() { return this.attrs.id || ''; }
  set id(v) { this.attrs.id = String(v); }
  get className() { return this.attrs.class || ''; }
  set className(v) { this.attrs.class = String(v); }
  get classList() {
    const self = this;
    const list = () => (self.attrs.class || '').split(/\s+/).filter(Boolean);
    return {
      add(c) { const l = list(); if (!l.includes(c)) l.push(c); self.attrs.class = l.join(' '); },
      remove(c) { self.attrs.class = list().filter((x) => x !== c).join(' '); },
      contains(c) { return list().includes(c); },
      toggle(c, force) {
        const has = list().includes(c);
        const want = force === undefined ? !has : force;
        if (want && !has) this.add(c); else if (!want && has) this.remove(c);
        return want;
      }
    };
  }
  appendChild(c) {
    if (c.nodeType === 11) { c.childNodes.slice().forEach((x) => this.appendChild(x)); return c; }
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = this; this.childNodes.push(c); return c;
  }
  insertBefore(c, ref) {
    if (!ref) return this.appendChild(c);
    if (c.parentNode) c.parentNode.removeChild(c);
    const i = this.childNodes.indexOf(ref);
    c.parentNode = this; this.childNodes.splice(i < 0 ? this.childNodes.length : i, 0, c); return c;
  }
  removeChild(c) {
    const i = this.childNodes.indexOf(c);
    if (i >= 0) this.childNodes.splice(i, 1);
    c.parentNode = null; return c;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceChildren(...nodes) {
    this.childNodes.forEach((c) => { c.parentNode = null; });
    this.childNodes = []; this._text = '';
    nodes.forEach((n) => this.appendChild(n));
  }
  get textContent() {
    if (this.nodeType === 3) return this._text;
    return this._text + this.childNodes.map((c) => c.textContent).join('');
  }
  set textContent(v) {
    this.childNodes.forEach((c) => { c.parentNode = null; });
    this.childNodes = []; this._text = String(v);
  }
  get innerHTML() { throw new Error('innerHTML is forbidden'); }
  set innerHTML(_v) { throw new Error('innerHTML is forbidden'); }
  get outerHTML() { throw new Error('outerHTML is forbidden'); }
  set outerHTML(_v) { throw new Error('outerHTML is forbidden'); }
  insertAdjacentHTML() { throw new Error('insertAdjacentHTML is forbidden'); }
  get style() {
    this.ownerDocument.styleLog.push(this.localName);
    return new Proxy({}, { set: () => true, get: () => '' });
  }
  setAttribute(k, v) {
    const val = String(v);
    this.ownerDocument.attrLog.push({ tag: this.localName, name: k, value: val });
    if (k === 'hidden') { this.hidden = true; }
    this.attrs[k] = val;
  }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); }
  removeAttribute(k) { delete this.attrs[k]; if (k === 'hidden') this.hidden = false; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); }
  dispatch(t, extra) {
    const ev = Object.assign({
      type: t, target: this, currentTarget: this, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}
    }, extra);
    (this.listeners[t] || []).slice().forEach((fn) => fn(ev));
    return ev;
  }
  focus() { this.ownerDocument.activeElement = this; this.ownerDocument.focusLog.push(this); }
  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = null; }
  scrollIntoView() {}
  showModal() { this.open = true; }
  close() { this.open = false; }
}

function walk(node, fn) {
  fn(node);
  node.childNodes.forEach((c) => walk(c, fn));
}
function findAll(root, pred) {
  const out = [];
  walk(root, (n) => { if (n.nodeType === 1 && pred(n)) out.push(n); });
  return out;
}
function find(root, pred) { return findAll(root, pred)[0] || null; }
const byTag = (root, tag) => findAll(root, (n) => n.localName === tag);
const byClass = (root, cls) => findAll(root, (n) => (n.attrs.class || '').split(/\s+/).includes(cls));
const byText = (root, text, tag) => findAll(root, (n) => (!tag || n.localName === tag) && n.textContent.includes(text));
const buttons = (root, label) => findAll(root, (n) => n.localName === 'button' && (!label || n.textContent.includes(label)));
const links = (root) => findAll(root, (n) => n.localName === 'a');

function makeDom(htmlPath) {
  const html = fs.readFileSync(htmlPath, 'utf8');
  const doc = {
    attrLog: [], styleLog: [], focusLog: [], activeElement: null,
    createElement(tag) { return new FakeNode(doc, tag.toLowerCase()); },
    createElementNS(ns, tag) { return new FakeNode(doc, tag, ns); },
    createTextNode(s) { const n = new FakeNode(doc, '#text'); n._text = String(s); return n; },
    createDocumentFragment() { const n = new FakeNode(doc, '#fragment'); n.nodeType = 11; return n; },
    getElementById(id) {
      let found = null;
      walk(doc.body, (n) => { if (!found && n.nodeType === 1 && n.attrs.id === id) found = n; });
      return found;
    },
    title: ''
  };
  doc.body = new FakeNode(doc, 'body');
  doc.documentElement = new FakeNode(doc, 'html');
  const re = /<(\w+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*\/?>/g;
  let m;
  const staticIds = [];
  while ((m = re.exec(html))) {
    const attrText = m[2];
    const idm = /\sid="([^"]+)"/.exec(attrText);
    if (!idm) continue;
    const n = new FakeNode(doc, m[1].toLowerCase());
    const attrRe = /\s([a-zA-Z-]+)(?:="([^"]*)")?/g;
    let a;
    while ((a = attrRe.exec(attrText))) {
      if (a[1] === 'hidden') n.hidden = true;
      else if (a[1] === 'disabled') n.disabled = true;
      else n.attrs[a[1]] = a[2] === undefined ? '' : a[2];
    }
    if (n.attrs.value !== undefined) n.value = n.attrs.value;
    doc.body.appendChild(n);
    staticIds.push(idm[1]);
  }
  // 构造完成后清空日志：静态属性不算页面脚本写的。
  doc.attrLog.length = 0;
  doc.staticIds = staticIds;
  return doc;
}

function makeWindow() {
  const listeners = {};
  let hash = '';
  const storageLog = [];
  const win = {
    storageLog,
    navigator: {},
    addEventListener(t, fn) { (listeners[t] = listeners[t] || []).push(fn); },
    removeEventListener(t, fn) { listeners[t] = (listeners[t] || []).filter((f) => f !== fn); },
    fire(t, ev) { (listeners[t] || []).slice().forEach((fn) => fn(ev || { type: t })); },
    historyLog: []
  };
  win.location = {
    get hash() { return hash; },
    set hash(v) {
      const next = v === '' || String(v).startsWith('#') ? String(v) : '#' + v;
      if (next === hash) return;
      hash = next;
      win.fire('hashchange', { type: 'hashchange' });
    },
    get search() { return ''; },
    get href() { return 'http://127.0.0.1:8086/workspace/' + hash; }
  };
  win.history = {
    replaceState(_s, _t, url) { win.historyLog.push(['replace', url]); if (typeof url === 'string' && url.startsWith('#')) hash = url; },
    pushState(_s, _t, url) { win.historyLog.push(['push', url]); if (typeof url === 'string' && url.startsWith('#')) hash = url; }
  };
  ['localStorage', 'sessionStorage', 'indexedDB'].forEach((k) => {
    Object.defineProperty(win, k, { get() { storageLog.push(k); throw new Error('storage forbidden: ' + k); } });
  });
  return win;
}

module.exports = { FakeNode, makeDom, makeWindow, walk, find, findAll, byTag, byClass, byText, buttons, links };

return module.exports;
})();

const flatMock = (() => {
const module = {exports:{}};
'use strict';
/*
 * 工作台测试用的“合成世界”与模拟 API（只用于测试，页面里没有任何内置数据）。
 *
 * 所有数据都是合成的假数据（人物“合成人物甲”等），模拟的是当前 ear_api 的接口语义：
 *  - GET /v1/records/{type}：按 event_start 升序（无时间的排最后）再按 id；start/end 必须成对给出，否则 422；
 *    时间过滤沿用 TimeRange.overlaps 的规则；include_superseded 默认 false；游标不透明；
 *  - GET /v1/records/{type}/{id}[?version=]、/history?after_version&limit；
 *  - GET /v1/previews/person/{id}、/v1/actions/{id}/execution、/v1/feedback/{id}/status、/v1/me、/health/ready；
 *  - GET /v1/evidence/search：由 opts.evidence 决定是否“已启用”（否则 404，模拟主 agent 尚未注册路由）。
 */
const T = (s) => Date.parse(s);

function id(kind, n) {
  const k = { voiceprint: 1, transcript: 2, audio: 3, gps: 4, geocode: 5, person: 6, place: 7, attribution: 8, activity: 9, rule: 10, selection: 11, artifact: 12, suggestion: 13, feedback: 14, speaker: 15, phone: 16, batch: 17, root: 18 }[kind];
  return '0199f0a0-' + k.toString(16).padStart(4, '0') + '-7aaa-9bbb-' + String(n).padStart(12, '0');
}

const KIND_OF = { person: 'person', place: 'place', attribution: 'attribution', activity: 'activity', rule: 'rule', rule_selection: 'selection', artifact: 'artifact', action_suggestion: 'suggestion' };
function sem(type, n, version, payload, o) {
  const x = o || {};
  return {
    id: x.id || id(x.kind || KIND_OF[type], n), data_type: type, version: version, status: x.status || 'active',
    event_start: x.start === undefined ? null : x.start, event_end: x.end === undefined ? null : x.end,
    dedup_key: null, payload: payload, writer: x.writer || 'extraction', request_event_id: null,
    root_feedback_id: x.root || 'system', depth: x.depth || 0, skip_history: false,
    batch_id: id('batch', x.batch || version), created_at: x.created || '2026-10-29T07:00:00Z'
  };
}
function raw(type, kind, n, start, end, payload) {
  return {
    id: id(kind, n), data_type: type, version: 1, event_start: start, event_end: end, ingested_at: '2026-10-29T06:30:00Z',
    source: { device: 'phone-01', channel: 'octopus', user: 'default' }, adapter_version: 'test/0.1', dedup_key: type + ':' + n, payload: payload
  };
}

function createWorld() {
  const w = { records: [] };
  const add = (r) => { w.records.push(r); return r; };
  // 录音 09:00–09:30（UTC），一段转写在 09:05。
  add(raw('audio_ref', 'audio', 1, '2026-10-29T09:00:00Z', '2026-10-29T09:30:00Z', { media_type: 'audio/mp4', file_ref: { path: 'octopus/private/rec-0001.m4a', sha256: 'a'.repeat(64), size: 14680064 } }));
  add(raw('local_speaker', 'speaker', 1, '2026-10-29T09:00:00Z', '2026-10-29T09:30:00Z', { audio_ref_id: id('audio', 1), label: 'speaker_0' }));
  add(raw('voiceprint', 'voiceprint', 1, '2026-10-29T09:05:00Z', '2026-10-29T09:06:00Z', {
    local_speaker_id: id('speaker', 1), audio_ref_id: id('audio', 1), embedding: { values: Array.from({ length: 64 }, (_, i) => i / 100), shape: [64], dtype: 'float32' },
    sample_quality: 0.93, model_version: 'vp-1', transcript_segment_ids: [id('transcript', 1)]
  }));
  add(raw('transcript_segment', 'transcript', 1, '2026-10-29T09:05:00Z', '2026-10-29T09:05:20Z', {
    text: '我们下周三开会讨论预算，<b>不要</b>忘记', language: 'zh', overlap: false, local_speaker_id: id('speaker', 1), voiceprint_id: id('voiceprint', 1), audio_ref_id: id('audio', 1)
  }));
  add(raw('transcript_segment', 'transcript', 2, '2026-10-29T09:12:00Z', '2026-10-29T09:12:30Z', { text: '好的，下周三见', language: 'zh', overlap: false, local_speaker_id: id('speaker', 1), voiceprint_id: null, audio_ref_id: id('audio', 1) }));
  add(raw('gps_point', 'gps', 1, '2026-10-29T09:00:00Z', '2026-10-29T09:00:00Z', { latitude: 39.999, longitude: 116.326, accuracy_m: 12.5 }));
  add(raw('gps_point', 'gps', 2, '2026-10-29T09:10:00Z', '2026-10-29T09:10:00Z', { latitude: 40.001, longitude: 116.329, accuracy_m: 8 }));
  add(raw('gps_geocode', 'geocode', 1, '2026-10-29T09:00:00Z', '2026-10-29T09:00:00Z', {
    gps_point_id: id('gps', 1), status: 'ok', provider: 'nominatim', language: 'zh', formatted_address: '合成示例路 1 号', country: null, province: null, city: null, district: null, township: null, street: null, building_name: null, building_type: null, building_distance_m: null,
    query_coordinate: { latitude: 39.999, longitude: 116.326, crs: 'WGS84' }
  }));
  add(raw('phone_info', 'phone', 1, '2026-10-29T09:00:00Z', '2026-10-29T09:00:00Z', { kind: 'snapshot', state: { battery: 80 } }));
  // 人物：v1 未命名，v2 命名。
  add(sem('person', 1, 1, { name: null, display_number: 1, references: [id('voiceprint', 1)] }, { writer: 'attribution', created: '2026-10-29T07:00:00Z' }));
  add(sem('person', 1, 2, { name: '合成人物甲', display_number: 1, references: [id('voiceprint', 1)] }, { writer: 'attribution', created: '2026-10-29T08:00:00Z', root: id('feedback', 1), batch: 2 }));
  add(sem('place', 1, 1, { name: '合成会议室', references: [id('gps', 1), id('gps', 2)], is_unknown: false }, { writer: 'attribution' }));
  add(sem('place', 2, 1, { name: null, references: [], is_unknown: true }, { writer: 'attribution' }));
  add(sem('attribution', 1, 1, { subject_type: 'voiceprint', subject_id: id('voiceprint', 1), target_type: 'person', target_id: id('person', 1), user_protected: true }, { writer: 'attribution', start: '2026-10-29T09:05:00Z', end: '2026-10-29T09:06:00Z' }));
  add(sem('activity', 1, 1, { start: '2026-10-29T09:00:00Z', end: '2026-10-29T09:30:00Z', title_md: '**预算会议**', description_md: '讨论下周三的预算安排。\n\n- 议题一\n- 议题二', evidence_ids: [id('transcript', 1), id('transcript', 2), id('gps', 1), id('voiceprint', 1)] }, { start: '2026-10-29T09:00:00Z', end: '2026-10-29T09:30:00Z' }));
  add(sem('activity', 2, 1, { start: '2026-10-29T10:00:00Z', end: '2026-10-29T10:10:00Z', title_md: '午间散步', description_md: '', evidence_ids: [] }, { start: '2026-10-29T10:00:00Z', end: '2026-10-29T10:10:00Z' }));
  add(sem('rule', 1, 1, { scope: '会议类活动', instruction: '提取待办事项并给出提醒', action: 'reminder', activity_id: null, enabled: true }));
  add(sem('rule', 1, 2, { scope: '会议类活动', instruction: '提取待办事项和结论，并给出提醒', action: 'reminder', activity_id: null, enabled: true }, { batch: 2 }));
  add(sem('rule_selection', 1, 1, { activity_id: id('activity', 1), rule_ids: [id('rule', 1)] }, { start: '2026-10-29T09:00:00Z', end: '2026-10-29T09:30:00Z' }));
  add(sem('artifact', 1, 1, { activity_id: id('activity', 1), rule_id: id('rule', 1), rule_version: 1, body_md: '# 会议纪要\n\n- [ ] 准备预算表\n\n![像素](https://tracker.example/p.gif) [官网](javascript:alert(1)) <script>alert(1)</script>', evidence_ids: [id('transcript', 1)], action_reason: '会议里提到截止时间', run_id: 'run-1', manual: false, source: 'automatic' }, { start: '2026-10-29T09:00:00Z', end: '2026-10-29T09:30:00Z' }));
  add(sem('artifact', 1, 2, { activity_id: id('activity', 1), rule_id: id('rule', 1), rule_version: 2, body_md: '# 会议纪要（修订）\n\n- [x] 准备预算表\n- [ ] 发邮件', evidence_ids: [id('transcript', 1)], action_reason: '会议里提到截止时间', run_id: 'run-2', manual: false, source: 'automatic' }, { start: '2026-10-29T09:00:00Z', end: '2026-10-29T09:30:00Z', batch: 2 }));
  add(sem('action_suggestion', 1, 1, { artifact_id: id('artifact', 1), artifact_version: 1, action: { kind: 'reminder', title: '准备预算表', notes: '周三前', due_at: '2026-10-30T01:00:00Z' }, state: 'pending' }, { start: '2026-10-29T09:00:00Z', end: '2026-10-29T09:30:00Z' }));
  add(raw('feedback', 'feedback', 1, '2026-10-29T08:00:00Z', '2026-10-29T08:00:00Z', { module: 'attribution', command: { kind: 'rename', target_id: id('person', 1), name: '合成人物甲' }, integration: 'pending_contract' }));
  return w;
}

function overlaps(rs, re, s, e) {
  if (s === null || e === null) return false;
  const a = T(rs), b = T(re), x = T(s), y = T(e);
  if (a === b) return false;
  if (x === y) return a <= x && x < b;
  return x < b && y > a;
}

function createHandler(world, opts) {
  const o = Object.assign({ role: 'user', evidence: true, denied: [], fail: {}, evidenceItems: null, pageCap: 1000 }, opts);
  const log = [];
  function current(recs) {
    const byId = new Map();
    recs.forEach((r) => { const c = byId.get(r.id); if (!c || r.version > c.version) byId.set(r.id, r); });
    return Array.from(byId.values());
  }
  function handle(call) {
    log.push(call.url);
    const url = new URL(call.url, 'http://x');
    const p = url.pathname;
    const q = url.searchParams;
    const bad = (status, code) => ({ status: status, body: { code: code } });
    if (o.fail[p] !== undefined) return o.fail[p];
    if (p === '/health/ready') return { body: { status: 'ready', scope: 'api_storage_connectivity' } };
    if (p === '/v1/me') return { body: { subject: 'tester', role: o.role } };
    let m;
    if ((m = /^\/v1\/records\/([a-z_]+)\/?$/.exec(p))) {
      const type = m[1];
      if (o.denied.includes(type)) return bad(403, 'forbidden');
      if ((q.has('start') || q.has('end')) && !(q.has('start') && q.has('end'))) return bad(422, 'invalid_input');
      const limit = parseInt(q.get('limit') || '50', 10);
      let rows = world.records.filter((r) => r.data_type === type);
      // 原始数据没有 status 字段（版本恒为 1，永远有效）；只有语义记录会被整体替代。
      rows = q.get('include_superseded') === 'true' ? current(rows) : current(rows).filter((r) => r.status === undefined || r.status === 'active');
      if (q.has('start')) rows = rows.filter((r) => overlaps(q.get('start'), q.get('end'), r.event_start, r.event_end));
      rows.sort((a, b) => (a.event_start === null) - (b.event_start === null) || String(a.event_start).localeCompare(String(b.event_start)) || a.id.localeCompare(b.id));
      const off = q.has('cursor') ? parseInt(q.get('cursor').replace('off:', ''), 10) : 0;
      const page = rows.slice(off, off + limit);
      return { body: { items: page, next_cursor: off + limit < rows.length ? 'off:' + (off + limit) : null, consistency: 'live' } };
    }
    if ((m = /^\/v1\/records\/([a-z_]+)\/([0-9a-f-]{36})\/history$/.exec(p))) {
      if (o.denied.includes(m[1])) return bad(404, 'not_found');
      const vs = world.records.filter((r) => r.id === m[2] && r.data_type === m[1]).sort((a, b) => a.version - b.version);
      if (!vs.length) return bad(404, 'not_found');
      const after = parseInt(q.get('after_version') || '0', 10), limit = parseInt(q.get('limit') || '50', 10);
      const items = vs.filter((r) => r.version > after).slice(0, limit);
      const last = items.length ? items[items.length - 1].version : after;
      return { body: { items: items, next_after_version: last < vs[vs.length - 1].version ? last : null } };
    }
    if ((m = /^\/v1\/records\/([a-z_]+)\/([0-9a-f-]{36})$/.exec(p))) {
      if (o.denied.includes(m[1])) return bad(404, 'not_found');
      const vs = world.records.filter((r) => r.id === m[2] && r.data_type === m[1]).sort((a, b) => a.version - b.version);
      if (!vs.length) return bad(404, 'not_found');
      const want = q.has('version') ? parseInt(q.get('version'), 10) : vs[vs.length - 1].version;
      const hit = vs.find((r) => r.version === want);
      return hit ? { body: hit } : bad(404, 'not_found');
    }
    if ((m = /^\/v1\/previews\/person\/([0-9a-f-]{36})$/.exec(p))) {
      const n = world.records.filter((r) => r.data_type === 'attribution' && r.payload.target_id === m[1]).length;
      return { body: { person_id: m[1], attributed_records: n, candidate_identity: null } };
    }
    if ((m = /^\/v1\/actions\/([0-9a-f-]{36})\/execution$/.exec(p))) {
      return { body: { suggestion_id: m[1], suggestion_version: 1, suggestion_state: 'pending', suggestion_status: 'active', executor_state: 'disabled', journal_revision: null, execution_enabled: false, verification_enabled: false, recovery_protocol: 'pending_extraction_collaboration', blocker: 'extraction_execution_recovery_protocol_pending' } };
    }
    if ((m = /^\/v1\/feedback\/([0-9a-f-]{36})\/status$/.exec(p))) {
      return { body: { feedback_id: m[1], status: 'pending', completion_source: null, status_scope: 'feedback_processing_not_external_execution', integration: 'attribution_result_query' } };
    }
    if (p === '/v1/evidence/search') {
      if (!o.evidence) return bad(404, 'not_found');
      const phrase = q.get('q');
      const items = (o.evidenceItems || world.records.filter((r) => r.data_type === 'transcript_segment' && r.payload.text.includes(phrase) && overlaps(q.get('start'), q.get('end'), r.event_start, r.event_end)).map((r) => ({
        transcript_segment_id: r.id, transcript_segment_version: 1, start: r.event_start, end: r.event_end, text: r.payload.text, local_speaker_id: r.payload.local_speaker_id,
        voiceprint_id: r.payload.voiceprint_id, audio_ref_id: r.payload.audio_ref_id,
        playback: { audio_ref_id: r.payload.audio_ref_id, start_seconds: (T(r.event_start) - T('2026-10-29T09:00:00Z')) / 1000, end_seconds: (T(r.event_end) - T('2026-10-29T09:00:00Z')) / 1000 }, playback_status: 'available',
        attribution: r.payload.voiceprint_id ? { id: id('attribution', 1), version: 1, person_id: id('person', 1), person_version: 2, person_status: 'active', name: '合成人物甲' } : null,
        attribution_status: r.payload.voiceprint_id ? 'resolved' : 'no_voiceprint'
      })));
      return { body: { items: items, next_cursor: null, truncated: false, scanned: items.length, consistency: 'live_best_effort', report: { stop_reason: 'exhausted', continuation: 'exhausted', pages: 1, calls: 3, read_rows: items.length, row_bytes: 100, deadline_mode: 'cooperative' } } };
    }
    return bad(404, 'not_found');
  }
  handle.log = log;
  handle.options = o;
  return handle;
}

module.exports = { createWorld, createHandler, id, sem, raw };

return module.exports;
})();

const flatEnv = (() => {
const module = {exports:{}};
'use strict';
/*
 * 工作台页面测试的启动器：假 DOM + 假 window + 假 fetch（接模拟 API）+ 假计时器，真实运行 ws-app.js。
 */
const path = require('node:path');
const fake = flatFake;
const mock = flatMock;

const WS = STATIC;
const app = bundle.app;
const core = bundle.core;

const TOKEN = 'T'.repeat(43);
const NOW = Date.parse('2026-10-29T12:00:00Z');

const tick = () => new Promise((r) => setImmediate(r));
async function settle(n) { for (let i = 0; i < (n || 40); i++) await tick(); }

function makeTimers() {
  let seq = 0;
  const q = new Map();
  return {
    setTimeout(fn, ms) { const id = ++seq; q.set(id, { fn, ms }); return id; },
    clearTimeout(id) { q.delete(id); },
    pending: () => q.size,
    runDue(limit) {
      const due = Array.from(q.entries()).filter((e) => e[1].ms <= (limit === undefined ? 20000 : limit));
      due.forEach((e) => q.delete(e[0]));
      due.forEach((e) => e[1].fn());
      return due.length;
    }
  };
}

function makeFetch(handler) {
  const calls = [];
  async function fetchFn(url, init) {
    const call = { url, method: init.method, headers: init.headers, init };
    calls.push(call);
    const r = (await handler(call)) || { status: 404, body: { code: 'not_found' } };
    if (r.reject) throw new TypeError('network');
    if (r.hang) return new Promise((_, rej) => init.signal.addEventListener('abort', () => rej(new Error('aborted'))));
    if (r.gate) await r.gate;
    const status = r.status === undefined ? 200 : r.status;
    const text = r.text !== undefined ? r.text : JSON.stringify(r.body === undefined ? {} : r.body);
    return { ok: status >= 200 && status < 300, status, text: async () => text };
  }
  fetchFn.calls = calls;
  return fetchFn;
}

/** 启动页面；handler 默认是合成世界的模拟 API。 */
function bootWorkspace(options) {
  const o = options || {};
  const world = o.world || mock.createWorld();
  const handler = o.handler || mock.createHandler(world, o.api);
  const doc = fake.makeDom(path.join(WS, 'workspace.html'));
  const win = fake.makeWindow();
  const timers = makeTimers();
  const f = makeFetch(handler);
  const page = app.boot(win, doc, {
    fetch: f, setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout, AbortController,
    now: () => (o.now === undefined ? NOW : o.now), offsetAt: () => (o.offset === undefined ? 480 : o.offset)
  });
  const n = (id) => doc.getElementById(id);
  const env = {
    doc, win, page, f, timers, world, handler, n,
    root: () => n('view-root'),
    text: () => n('view-root').textContent,
    async connect(token) {
      n('token-input').value = token || TOKEN;
      n('connect-form').dispatch('submit');
      await settle();
    },
    async go(hash) { win.location.hash = hash; await settle(); },
    gets: () => f.calls.map((c) => c.url)
  };
  return env;
}

module.exports = { bootWorkspace, settle, tick, makeFetch, makeTimers, TOKEN, NOW, core, fake, mock, WS };

return module.exports;
})();

// ===== Recovered core regression cases, targeting flat bundle =====
{
'use strict';
// 工作台 · 纯逻辑：时间与时区、范围、请求路径、路由、错误说明、脱敏与差异、记录描述、布局数学。
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const WS = STATIC;
const core = bundle.core;
const { id, sem, raw } = flatMock;

const UTC = core.makeZone('utc');
const CST = core.makeZone('local', () => 480);          // UTC+8，无夏令时
const NEG = core.makeZone('local', () => -330);         // UTC-5:30
// 模拟一个会切换夏令时的时区：2026-03-08T10:00Z 之后偏移 -420（美国太平洋，PDT），之前 -480（PST）。
const DST_AT = Date.parse('2026-03-08T10:00:00Z');
const PACIFIC = core.makeZone('local', (ms) => (ms >= DST_AT ? -420 : -480));

test('time: parseUtc 只接受严格的 RFC3339 UTC Z，拒绝不存在的日期、偏移和超长输入', () => {
  assert.equal(core.parseUtc('2026-10-29T06:05:12Z'), Date.UTC(2026, 9, 29, 6, 5, 12));
  assert.equal(core.parseUtc('2026-10-29T06:05:12.5Z'), Date.UTC(2026, 9, 29, 6, 5, 12, 500));
  for (const bad of ['2026-10-29T06:05:12+08:00', '2026-10-29 06:05:12Z', '2026-02-30T00:00:00Z', '2026-13-01T00:00:00Z', '2026-10-29T25:00:00Z', null, 5, '', 'x'.repeat(40)]) {
    assert.equal(core.parseUtc(bad), null, String(bad));
  }
  assert.equal(core.toIso(Date.UTC(2026, 0, 2, 3, 4, 5)), '2026-01-02T03:04:05Z');
  assert.equal(core.toIso(NaN), null);
});

test('time: 格式化按所选时区显示，并标明时区；非法时间返回 null 而不是编造', () => {
  assert.equal(core.formatClock('2026-10-29T06:05:12Z', UTC), '2026-10-29 06:05:12');
  assert.equal(core.formatClock('2026-10-29T06:05:12Z', CST), '2026-10-29 14:05:12');
  assert.equal(core.formatClock('2026-10-29T06:05:12Z', NEG), '2026-10-29 00:35:12');
  assert.equal(core.formatClock('2026-10-29T06:05:12Z', CST, false), '2026-10-29 14:05');
  assert.equal(core.formatClock('garbage', CST), null);
  assert.equal(CST.label(0), 'UTC+8');
  assert.equal(NEG.label(0), 'UTC-5:30');
  assert.equal(UTC.label(0), 'UTC');
  assert.equal(core.formatTimeOnly('2026-10-29T23:30:00Z', CST), '07:30:00', '跨日显示的是本地时刻');
});

test('time: 本地墙钟 → UTC，含夏令时切换与不存在的日期', () => {
  assert.equal(core.localToUtcIso('2026-10-29T14:00', CST), '2026-10-29T06:00:00Z');
  assert.equal(core.localToUtcIso('2026-10-29T14:00:30', CST), '2026-10-29T06:00:30Z');
  assert.equal(core.localToUtcIso('2026-02-31T10:00', CST), null, '不存在的日期被拒绝');
  assert.equal(core.localToUtcIso('2026-10-29', CST), null, '缺少时间部分');
  assert.equal(core.localToUtcIso('2026-10-29T14:00Z', CST), null);
  // 夏令时之前（PST, -8）与之后（PDT, -7）：同一墙钟 12:00 对应的 UTC 不同。
  assert.equal(core.localToUtcIso('2026-03-07T12:00', PACIFIC), '2026-03-07T20:00:00Z');
  assert.equal(core.localToUtcIso('2026-03-09T12:00', PACIFIC), '2026-03-09T19:00:00Z');
  // 往返：UTC → 本地输入值 → UTC
  for (const iso of ['2026-10-29T06:00:00Z', '2026-03-09T19:00:00Z']) {
    const z = iso.startsWith('2026-03') ? PACIFIC : CST;
    assert.equal(core.localToUtcIso(core.utcToLocalInput(iso, z), z), iso);
  }
});

test('range: 单日在本地时区边界；夏令时日是 23 小时；自定义区间必须两端齐全且 start<end', () => {
  assert.deepEqual(core.dayBounds('2026-10-29', CST), { start: '2026-10-28T16:00:00Z', end: '2026-10-29T16:00:00Z' });
  assert.deepEqual(core.dayBounds('2026-10-29', UTC), { start: '2026-10-29T00:00:00Z', end: '2026-10-30T00:00:00Z' });
  const dst = core.dayBounds('2026-03-08', PACIFIC);
  assert.equal(core.parseUtc(dst.end) - core.parseUtc(dst.start), 23 * 3600 * 1000);
  assert.equal(core.dayBounds('2026-02-30', CST), null);
  assert.equal(core.shiftDay('2026-12-31', 1), '2027-01-01');
  assert.equal(core.shiftDay('2026-03-01', -1), '2026-02-28');
  assert.equal(core.dateOf('2026-10-28T20:00:00Z', CST), '2026-10-29');

  assert.deepEqual(core.resolveRange({ mode: 'all' }, CST), { ok: true, range: null });
  assert.equal(core.resolveRange({ mode: 'day', date: '' }, CST).ok, false);
  const half = core.resolveRange({ mode: 'custom', start: '2026-10-29T14:00', end: '' }, CST);
  assert.equal(half.ok, false);
  assert.ok(half.error.includes('同时填写'));
  assert.equal(core.resolveRange({ mode: 'custom', start: '2026-10-29T15:00', end: '2026-10-29T14:00' }, CST).ok, false);
  assert.equal(core.resolveRange({ mode: 'custom', start: '2026-10-29T14:00', end: '2026-10-29T14:00' }, CST).ok, false, '空区间 [t,t) 不是有效范围');
  assert.deepEqual(core.resolveRange({ mode: 'custom', start: '2026-10-29T14:00', end: '2026-10-29T15:30' }, CST).range, { start: '2026-10-29T06:00:00Z', end: '2026-10-29T07:30:00Z' });
  assert.equal(core.resolveRange({ mode: 'bogus' }, CST).ok, false);
});

test('format: 时长、偏移秒、字节数', () => {
  assert.equal(core.formatDuration(20000), '20 秒');
  assert.equal(core.formatDuration(90 * 1000), '1 分 30 秒');
  assert.equal(core.formatDuration(3600 * 1000), '1 小时');
  assert.equal(core.formatDuration(-1), null);
  assert.equal(core.formatOffsetSeconds(125.9), '2:05');
  assert.equal(core.formatOffsetSeconds(3725), '1:02:05');
  assert.equal(core.formatOffsetSeconds(-1), null);
  assert.equal(core.formatBytes(14680064), '14.0 MiB');
  assert.equal(core.formatBytes(-1), null);
});

test('paths: 列表/单条/历史/证据路径只生成受限参数；非法输入返回 null，不发请求', () => {
  const range = { start: '2026-10-29T00:00:00Z', end: '2026-10-30T00:00:00Z' };
  assert.equal(core.listPath('activity', { limit: 20, range }), '/v1/records/activity?limit=20&start=2026-10-29T00%3A00%3A00Z&end=2026-10-30T00%3A00%3A00Z');
  assert.equal(core.listPath('person'), '/v1/records/person?limit=50');
  assert.equal(core.listPath('person', { includeSuperseded: true }), '/v1/records/person?limit=50&include_superseded=true');
  assert.equal(core.listPath('plugin_management_feedback'), null, '管理类型不可浏览');
  assert.equal(core.listPath('activity', { limit: 0 }), null);
  assert.equal(core.listPath('activity', { limit: 101 }), null);
  assert.equal(core.listPath('activity', { range: { start: range.end, end: range.start } }), null);
  assert.equal(core.listPath('activity', { range: { start: range.start } }), null, '半个范围不会被发出');
  assert.equal(core.listPath('activity', { cursor: 'a\nb' }), null);
  assert.equal(core.listPath('activity', { cursor: 'x'.repeat(513) }), null);
  assert.ok(core.listPath('activity', { cursor: "o'(*)!" }).endsWith('cursor=o%27%28%2A%29%21'), '游标里的 ! \' ( ) * 也被转义');

  assert.equal(core.recordPath('person', id('person', 1)), '/v1/records/person/' + id('person', 1));
  assert.equal(core.recordPath('person', id('person', 1), 3), '/v1/records/person/' + id('person', 1) + '?version=3');
  assert.equal(core.recordPath('person', 'not-an-id'), null);
  assert.equal(core.recordPath('person', id('person', 1), 0), null);
  assert.equal(core.recordPath('person', id('person', 1), 2147483648), null);
  assert.equal(core.historyPath('rule', id('rule', 1), 0, 50), '/v1/records/rule/' + id('rule', 1) + '/history?limit=50');
  assert.equal(core.historyPath('rule', id('rule', 1), 2, 10), '/v1/records/rule/' + id('rule', 1) + '/history?after_version=2&limit=10');
  assert.equal(core.historyPath('rule', id('rule', 1), -1, 10), null);
  assert.equal(core.feedbackStatusPath(id('feedback', 1)), '/v1/feedback/' + id('feedback', 1) + '/status');
  assert.equal(core.executionPath('x'), null);
  assert.equal(core.personPreviewPath(id('person', 1)), '/v1/previews/person/' + id('person', 1));
});

test('paths: 证据检索窗口 ≤24h、短语 1–256、limit 1–10；超窗口会被截断并明示', () => {
  const r = { start: '2026-10-29T00:00:00Z', end: '2026-10-30T00:00:00Z' };
  assert.ok(core.evidencePath({ range: r, q: '预算' }).startsWith('/v1/evidence/search?start='));
  assert.equal(core.evidencePath({ range: { start: r.start, end: '2026-10-30T00:00:01Z' }, q: 'x' }), null);
  assert.equal(core.evidencePath({ range: r, q: '' }), null);
  assert.equal(core.evidencePath({ range: r, q: 'x'.repeat(257) }), null);
  assert.equal(core.evidencePath({ range: r, q: 'x', limit: 11 }), null);
  const w = core.evidenceWindow({ start: '2026-10-29T00:00:00Z', end: '2026-10-31T00:00:00Z' });
  assert.equal(w.clipped, true);
  assert.equal(w.range.end, '2026-10-30T00:00:00Z');
  assert.equal(core.evidenceWindow(r).clipped, false);
  assert.equal(core.evidenceWindow({ start: r.end, end: r.start }).ok, false);
});

test('route: 哈希只允许 视图/类型/ID/版本；畸形、路径穿越、令牌样式的哈希一律回到概览并标记无效', () => {
  const A = id('artifact', 1);
  assert.deepEqual(core.parseHash(''), { view: 'overview', type: null, id: null, version: null, valid: true });
  assert.deepEqual(core.parseHash('#/artifacts/' + A + '/v/3'), { view: 'artifacts', type: null, id: A, version: 3, valid: true });
  assert.deepEqual(core.parseHash('#/trace/person/' + A), { view: 'trace', type: 'person', id: A, version: null, valid: true });
  assert.equal(core.parseHash('#/trace/' + A).id, A);
  assert.equal(core.parseHash('#/people/').valid, true, '允许结尾斜杠');
  for (const bad of ['#/nope', '#/artifacts/../x', '#/artifacts/' + A + '/v/0', '#/artifacts/' + A + '/v/x', '#/artifacts/' + A + '/extra', '#/overview/' + A,
    '#/trace/not_a_type/' + A, '#?token=' + 'A'.repeat(43), '#/' + 'a'.repeat(200), '#/artifacts/' + A.toUpperCase(), '#/artifacts/' + A + '/v/99999999999']) {
    const r = core.parseHash(bad);
    assert.equal(r.valid, false, bad);
    assert.equal(r.view, 'overview');
  }
  assert.equal(core.formatHash({ view: 'trace', type: 'person', id: A, version: 2 }), '#/trace/person/' + A + '/v/2');
  assert.equal(core.formatHash({ view: 'nope', id: 'x' }), '#/overview');
  assert.equal(core.recordHash('person', A), '#/people/' + A);
  assert.equal(core.recordHash('voiceprint', A, 2), '#/trace/voiceprint/' + A + '/v/2');
  assert.equal(core.recordHash('???', A), '#/trace/' + A);
  assert.equal(core.recordHash('person', 'bad'), null);
  // 往返
  for (const h of ['#/rules/' + A, '#/trace/gps_point/' + A + '/v/4']) assert.equal(core.formatHash(core.parseHash(h)), h);
});

test('errors: 状态码 → 说明；只信任形状合法的错误码；401 标记断开', () => {
  assert.equal(core.describeError({ status: 401, code: 'unauthenticated' }).authFailure, true);
  assert.equal(core.describeError({ status: 403, code: 'cross_origin_denied' }).kind, 'origin');
  assert.equal(core.describeError({ status: 403, code: 'forbidden' }).kind, 'forbidden');
  assert.equal(core.describeError({ status: 404 }).kind, 'notfound');
  assert.ok(core.describeError({ status: 404 }).message.includes('隐藏'));
  assert.equal(core.describeError({ status: 422, code: 'invalid_input' }).kind, 'invalid');
  assert.equal(core.describeError({ status: 410 }).retryable, true);
  assert.equal(core.describeError({ status: 413 }).kind, 'limit');
  assert.equal(core.describeError({ status: 503, code: 'storage_unavailable' }).retryable, true);
  assert.equal(core.describeError({ status: 501 }).kind, 'unsupported');
  assert.equal(core.describeError({ status: 500 }).kind, 'server');
  assert.equal(core.describeError({ failure: 'timeout' }).retryable, true);
  assert.equal(core.describeError({ failure: 'network' }).kind, 'network');
  assert.equal(core.describeError({ failure: 'not_connected' }).kind, 'not_connected');
  assert.equal(core.describeError({ status: 403, code: '<script>alert(1)</script>' }).code, null, '形状非法的错误码被丢弃');
  assert.ok(!core.describeError({ status: 403, code: '<b>x</b>' }).message.includes('<'));
});

test('records: 规范化拒绝畸形记录，不补默认值；分页读取丢弃异常条目并计数', () => {
  const ok = sem('person', 1, 1, { name: 'x', references: [] });
  assert.equal(core.normalizeRecord(ok).version, 1);
  assert.equal(core.normalizeRecord(raw('gps_point', 'gps', 1, '2026-10-29T09:00:00Z', '2026-10-29T09:00:00Z', {})).status, 'active');
  assert.equal(core.normalizeRecord(raw('gps_point', 'gps', 1, '2026-10-29T09:00:00Z', '2026-10-29T09:00:00Z', {})).version, 1);
  assert.equal(core.normalizeRecord({ ...ok, id: 'nope' }), null);
  assert.equal(core.normalizeRecord({ ...ok, payload: null }), null);
  assert.equal(core.normalizeRecord({ ...ok, version: 0 }), null);
  assert.equal(core.normalizeRecord({ ...ok, status: 'weird' }), null);
  assert.equal(core.normalizeRecord(null), null);
  const page = core.readPage({ items: [ok, { junk: true }, null], next_cursor: 'abc', consistency: 'live' });
  assert.equal(page.ok, true);
  assert.equal(page.items.length, 1);
  assert.equal(page.dropped, 2);
  assert.equal(page.nextCursor, 'abc');
  assert.equal(core.readPage({ items: 'no' }).ok, false);
  assert.equal(core.readPage({ items: [], next_cursor: 5 }).ok, false, 'malformed cursor must not masquerade as exhausted');
  const hist = core.readHistory({ items: [ok], next_after_version: 3 });
  assert.equal(hist.nextAfter, 3);
  assert.equal(core.readHistory({ items: [], next_after_version: null }).nextAfter, null);
});

test('records: 合并分页按 id+版本去重并受上限约束', () => {
  const a = core.normalizeRecord(sem('person', 1, 1, {})), b = core.normalizeRecord(sem('person', 2, 1, {})), c = core.normalizeRecord(sem('person', 3, 1, {}));
  const m = core.mergeItems([a, b], [b, c], 0);
  assert.deepEqual(m.items.map((x) => x.id), [a.id, b.id, c.id]);
  assert.equal(m.added, 1);
  const capped = core.mergeItems([a], [b, c], 2);
  assert.equal(capped.items.length, 2);
  assert.equal(capped.capped, true);
});

test('redact: 隐藏声纹向量、回执、动作键/哈希、令牌与文件路径；截断超长文本与过深嵌套', () => {
  const out = core.redact({
    embedding: { values: new Array(64).fill(0.5), shape: [64] },
    receipt: 'secret-receipt', action_key: 'ear_action_' + 'a'.repeat(64), action_hash: 'b'.repeat(64), token: 'T'.repeat(43),
    file_ref: { path: 'octopus/private/a.m4a', sha256: 'c'.repeat(64), size: 1 },
    text: 'x'.repeat(5000), ok: '正常文本', small: [1, 2, 3]
  });
  assert.equal(out.embedding, '[已隐藏]', 'hide all embeddings, including short vectors');
  assert.equal(out.receipt, '[已隐藏]');
  assert.equal(out.action_key, '[已隐藏]');
  assert.equal(out.action_hash, '[已隐藏]');
  assert.equal(out.token, '[已隐藏]');
  assert.equal(out.file_ref.path, '[路径已隐藏]');
  assert.ok(out.text.includes('已截断') && out.text.length < 4100);
  assert.equal(out.ok, '正常文本');
  assert.deepEqual(out.small, [1, 2, 3], '短数组不会被误隐藏');
  let deep = 'leaf';
  for (let i = 0; i < 20; i++) deep = { n: deep };
  assert.ok(core.stableJson(core.redact(deep)).includes('嵌套过深'));
  assert.ok(!core.stableJson(out).includes('secret-receipt'));
});

test('diff: 版本差异按字段给出新增/删除/修改；被隐藏字段只显示“已变化”而不泄露值', () => {
  const d = core.payloadDiff({ name: null, references: ['a'], receipt: 'r1', keep: 1 }, { name: '甲', references: ['a', 'b'], receipt: 'r2', keep: 1, added: true });
  const by = Object.fromEntries(d.map((x) => [x.key, x]));
  assert.equal(by.name.change, 'changed');
  assert.equal(by.references.change, 'changed');
  assert.equal(by.added.change, 'added');
  assert.equal(by.receipt.change, 'changed');
  assert.equal(by.receipt.before, '[已隐藏]', '敏感字段变化时前后都只显示占位，但仍标为“修改”');
  assert.equal(by.receipt.after, '[已隐藏]');
  assert.ok(!JSON.stringify(d).includes('r1') && !JSON.stringify(d).includes('r2'));
  assert.equal(by.keep, undefined);
  assert.equal(core.payloadDiff({ a: 1 }, { a: 1 }).length, 0);
  assert.deepEqual(core.payloadDiff({ a: 1 }, {}).map((x) => x.change), ['removed']);
});

test('describe: 各类型的展示描述——缺失显示为缺失，畸形进入 warnings，不编造默认值', () => {
  const n = (rec) => core.describeRecord(core.normalizeRecord(rec));
  const unnamed = n(sem('person', 1, 1, { name: null, display_number: 3, references: [id('voiceprint', 1)] }));
  assert.equal(unnamed.headline, '未命名人物 #3');
  assert.equal(unnamed.fields[0].kind, 'missing');
  assert.equal(unnamed.refs[0].type, 'voiceprint');
  const unknownPlace = n(sem('place', 2, 1, { name: null, references: [], is_unknown: true }));
  assert.equal(unknownPlace.headline, '未知地点');
  const badAttr = n(sem('attribution', 1, 1, { subject_type: 'voiceprint', subject_id: 'bad', target_type: 'person', target_id: id('person', 1) }, { start: null, end: null }));
  assert.ok(badAttr.warnings.some((w) => w.includes('subject_id')));
  assert.ok(n(sem('attribution', 1, 1, { subject_type: 'voiceprint', subject_id: id('voiceprint', 1), target_type: 'person', target_id: id('person', 1) })).fields[0].value.includes('未知'));
  const art = n(sem('artifact', 1, 1, { activity_id: id('activity', 1), rule_id: id('rule', 1), rule_version: 2, body_md: '# 标题\n\n正文', manual: true, evidence_ids: [id('transcript', 1), 'bad'] }));
  assert.equal(art.markdown[0].text, '# 标题\n\n正文');
  assert.ok(art.fields.find((f) => f.label === '来源').value === '手动应用');
  assert.ok(art.warnings.some((w) => w.includes('格式无效')));
  assert.ok(art.refs.some((r) => r.type === 'rule' && r.version === 2));
  const noBody = n(sem('artifact', 1, 1, { activity_id: id('activity', 1) }));
  assert.ok(noBody.warnings.some((w) => w.includes('没有 Markdown 正文')));
  const sug = n(sem('action_suggestion', 1, 1, { artifact_id: id('artifact', 1), artifact_version: 1, action: { kind: 'calendar', title: '开会', start: '2026-10-30T01:00:00Z', end: '2026-10-30T02:00:00Z' }, state: 'unknown', receipt: 'SECRET', execution: { attempt: 2, action_key: 'ear_action_x'.padEnd(75, 'a') } }));
  assert.equal(sug.fields.find((f) => f.label === '状态').tone, 'warn');
  assert.ok(sug.fields.find((f) => f.label === '状态').value.includes('需人工核对'));
  assert.ok(!JSON.stringify(sug).includes('SECRET') && !JSON.stringify(sug).includes('ear_action_x'));
  assert.ok(n(sem('action_suggestion', 1, 1, { state: 'pending' })).warnings.some((w) => w.includes('action')));
  const vp = n(raw('voiceprint', 'voiceprint', 1, '2026-10-29T09:00:00Z', '2026-10-29T09:01:00Z', { embedding: { values: new Array(64).fill(1) } }));
  assert.ok(vp.fields[0].value.includes('已隐藏'));
  assert.ok(!JSON.stringify(vp).includes('"values"'));
  const audio = n(raw('audio_ref', 'audio', 1, '2026-10-29T09:00:00Z', '2026-10-29T09:30:00Z', { file_ref: { path: 'a/b.m4a', sha256: 'd'.repeat(64), size: 2048 } }));
  assert.ok(audio.fields.some((f) => f.value && String(f.value).includes('已隐藏')));
  assert.ok(!JSON.stringify(audio).includes('a/b.m4a'));
  const gps = n(raw('gps_point', 'gps', 1, '2026-10-29T09:00:00Z', '2026-10-29T09:00:00Z', { latitude: 'x' }));
  assert.ok(gps.warnings.length);
  assert.equal(gps.headline, '（坐标缺失）');
  assert.equal(n(sem('rule', 1, 1, { scope: 'a', instruction: 'b', enabled: false, activity_id: id('activity', 1) })).fields.find((f) => f.label === '启用').value, '已停用');
  assert.equal(n(raw('feedback', 'feedback', 1, '2026-10-29T08:00:00Z', '2026-10-29T08:00:00Z', { module: 'attribution', command: { kind: 'rename', target_id: id('person', 1), name: '甲' } })).headline, '反馈：attribution / rename');
});

test('layout: 时间条泳道分配、裁剪与窗口外计数；刻度随跨度变化；GPS 投影保持比例', () => {
  const s = Date.parse('2026-10-29T00:00:00Z'), e = Date.parse('2026-10-30T00:00:00Z');
  const mk = (i, a, b) => ({ id: 'i' + i, start: a, end: b });
  const items = [mk(1, '2026-10-29T09:00:00Z', '2026-10-29T10:00:00Z'), mk(2, '2026-10-29T09:30:00Z', '2026-10-29T10:30:00Z'), mk(3, '2026-10-29T11:00:00Z', '2026-10-29T11:00:00Z'),
    mk(4, '2026-10-28T20:00:00Z', '2026-10-29T01:00:00Z'), mk(5, '2026-11-02T00:00:00Z', '2026-11-02T01:00:00Z'), { id: 'bad', start: 'x', end: 'y' }];
  const l = core.layoutTimeline(items, s, e);
  const bar = (i) => l.bars.find((b) => b.id === 'i' + i);
  assert.equal(bar(1).lane, 0);
  assert.equal(bar(2).lane, 1, '重叠的活动分到不同泳道');
  assert.equal(bar(3).lane, 0, '点事件也有最小宽度并复用空闲泳道');
  assert.ok(bar(3).w > 0);
  assert.equal(bar(4).clippedStart, true);
  assert.equal(bar(4).x, 0);
  assert.equal(l.outside, 2, '窗口外与时间异常的被计数，而不是被丢弃或画错位置');
  assert.equal(l.bars.length, 4);
  assert.ok(l.bars.every((b) => b.x >= 0 && b.x + b.w <= 1 + 1e-9));
  assert.equal(core.layoutTimeline([], s, s).lanes, 0);
  assert.equal(core.hourTicks(s, e, UTC)[0].label, '00:00');
  assert.ok(core.hourTicks(s, e, UTC).length <= 13);
  // 窗口 00:00Z 在 UTC+8 是 08:00（整点），所以第一个刻度是 08:00；窗口从本地午夜开始时才是 00:00。
  assert.equal(core.hourTicks(s, e, CST)[0].label, '08:00', '刻度按所选时区的整点');
  assert.equal(core.hourTicks(Date.parse('2026-10-28T16:00:00Z'), Date.parse('2026-10-29T16:00:00Z'), CST)[0].label, '00:00');
  // 半小时偏移时区（UTC-5:30）：窗口 00:00Z 是本地 18:30，下一个 2 小时刻度是 20:00，且位置按比例落在窗口内。
  const half = core.hourTicks(s, e, NEG)[0];
  assert.equal(half.label, '20:00');
  assert.ok(Math.abs(half.x - 90 / 1440) < 1e-9, '20:00 本地 = 01:30Z，距窗口起点 90 分钟');
  assert.ok(core.hourTicks(s, s + 7 * 86400000, UTC)[0].label.includes('-'), '跨多日时刻度是日期');

  const p = core.projectPoints([{ id: 'a', lat: 39.999, lon: 116.326 }, { id: 'b', lat: 40.001, lon: 116.329 }, { id: 'bad', lat: 999, lon: 0 }, { id: 'nan', lat: NaN, lon: 1 }], 300, 20);
  assert.equal(p.skipped, 2);
  assert.equal(p.points.length, 2);
  assert.ok(p.points.every((q) => q.x >= 20 - 1e-6 && q.x <= 280 + 1e-6 && q.y >= 20 - 1e-6 && q.y <= 280 + 1e-6));
  assert.ok(p.points[1].y < p.points[0].y, '北在上：纬度更大的点 y 更小');
  assert.ok(p.spanMeters > 100 && p.spanMeters < 500);
  assert.equal(core.projectPoints([{ id: 'a', lat: 1, lon: 1 }], 300).points[0].x, 150, '只有一个点时居中');
  assert.deepEqual(core.projectPoints([], 300).points, []);
});

test('readers: 证据响应只取白名单字段并校验；状态响应不展开嵌套与敏感键；概览探测区分四种结果', () => {
  const ev = core.readEvidence({
    items: [
      { transcript_segment_id: id('transcript', 1), transcript_segment_version: 1, start: '2026-10-29T09:05:00Z', end: '2026-10-29T09:05:20Z', text: 'a'.repeat(900), voiceprint_id: id('voiceprint', 1), playback: { audio_ref_id: id('audio', 1), start_seconds: 1, end_seconds: 2 }, playback_status: 'available', attribution: { person_id: id('person', 1), name: '甲', person_status: 'active' }, attribution_status: 'resolved', evil: '<script>' },
      { transcript_segment_id: 'bad' }
    ],
    next_cursor: 'c', truncated: true, scanned: 5, consistency: 'live_best_effort', report: { stop_reason: 'deadline', pages: 2 }
  });
  assert.equal(ev.ok, true);
  assert.equal(ev.items.length, 1);
  assert.equal(ev.dropped, 1);
  assert.equal(ev.items[0].text.length, 600);
  assert.equal(ev.items[0].evil, undefined);
  assert.equal(ev.items[0].attribution.name, '甲');
  assert.equal(ev.truncated, true);
  assert.equal(ev.report.stopReason, 'deadline');
  assert.equal(core.readEvidence({ nope: 1 }).ok, false);

  const st = core.readStatus({ suggestion_id: id('suggestion', 1), suggestion_state: 'unknown', execution_enabled: false, receipt: 'S', nested: { action_key: 'K' }, journal_revision: null });
  assert.equal(st.ok, true);
  assert.equal(st.nested, 1);
  assert.equal(st.executionEnabled, false);
  assert.ok(st.rows.find((r) => r.key === 'suggestion_state').text.includes('需人工核对'));
  assert.ok(!JSON.stringify(st).includes('"S"') && !JSON.stringify(st).includes('"K"'));
  assert.equal(core.readStatus([1]).ok, false);

  const pg = { ok: true, items: [{ start: '2026-10-29T09:00:00Z', id: 'x' }], nextCursor: 'c' };
  assert.equal(core.classifyProbe({ ok: true }, pg).state, 'has');
  assert.equal(core.classifyProbe({ ok: true }, { ok: true, items: [], nextCursor: null }).state, 'empty');
  assert.equal(core.classifyProbe({ ok: false, error: { kind: 'forbidden', message: 'm' } }, null).state, 'denied');
  assert.equal(core.classifyProbe({ ok: false, error: { kind: 'unavailable', message: 'm' } }, null).state, 'error');
  assert.equal(core.classifyProbe({ ok: true }, { ok: false }).state, 'error');
});

}

// ===== Recovered client regression cases, targeting flat bundle =====
{
'use strict';
// 工作台 · 只读网络层：只有 GET、令牌只进 Authorization 头、路径白名单、并发/超时/体积上限、断开后迟到响应丢弃。
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { makeFetch, makeTimers, tick, settle, TOKEN } = flatEnv;

const WS = STATIC;
const clientMod = bundle.client;

function newClient(f, t, extra) {
  const c = clientMod.createClient(Object.assign({ fetch: f, AbortController, setTimeout: t.setTimeout, clearTimeout: t.clearTimeout }, extra));
  c.setToken(TOKEN);
  return c;
}

test('client: 没有任何写方法；所有请求都是 GET，选项固定为同源、无 cookie、不缓存、不跟随重定向', async () => {
  const c = newClient(makeFetch(() => ({ body: { ok: 1 } })), makeTimers());
  assert.deepEqual(Object.keys(c).sort(), ['clear', 'connected', 'generation', 'get', 'inflight', 'queued', 'setNoAuth', 'setToken']);
  for (const m of ['post', 'put', 'patch', 'delete', 'request']) assert.equal(c[m], undefined, m);
  const f = makeFetch(() => ({ body: { ok: 1 } }));
  const c2 = newClient(f, makeTimers());
  await c2.get('/v1/me');
  const call = f.calls[0];
  assert.equal(call.method, 'GET');
  assert.equal(call.init.body, undefined);
  assert.equal(call.init.credentials, 'omit');
  assert.equal(call.init.cache, 'no-store');
  assert.equal(call.init.redirect, 'error');
  assert.equal(call.init.referrerPolicy, 'no-referrer');
  assert.equal(call.init.mode, 'same-origin');
  assert.equal(call.headers.Authorization, 'Bearer ' + TOKEN);
  assert.ok(!call.url.includes(TOKEN), '令牌不会出现在 URL 里');
});

test('client: /health/ 不带 Authorization；其余路径未连接时不发请求', async () => {
  const f = makeFetch(() => ({ body: { status: 'ready' } }));
  const c = clientMod.createClient({ fetch: f, AbortController, setTimeout: makeTimers().setTimeout, clearTimeout: () => {} });
  const r = await c.get('/v1/me');
  assert.equal(r.notConnected, true);
  assert.equal(f.calls.length, 0);
  assert.equal(c.setToken('short'), false);
  assert.equal(c.setToken('x'.repeat(42) + '!'), false);
  const h = await c.get('/health/ready');
  assert.equal(h.ok, true);
  assert.equal(f.calls[0].headers.Authorization, undefined);
});

test('client: 路径白名单——拒绝外部地址、协议相对、反斜杠、..、控制字符与超长路径，且不发请求', async () => {
  const f = makeFetch(() => ({ body: {} }));
  const c = newClient(f, makeTimers());
  const bad = ['https://evil.example/v1/me', '//evil.example/v1/me', '/\\evil', '/v1/../etc', '/v1//me', '/admin', '/v1/me\n', '/v1/me#frag', '/v1/records/a b', 'v1/me', '', null, 5, '/v1/' + 'a'.repeat(2300), '/plugins/', '/v1/me?x=<script>'];
  for (const p of bad) {
    const r = await c.get(p);
    assert.equal(r.ok, false, String(p));
    assert.equal(r.error.kind, 'bad_path', String(p));
  }
  assert.equal(f.calls.length, 0);
  assert.equal(clientMod.pathOk('/v1/records/person?limit=5&cursor=a%2Fb'), true);
  assert.equal(clientMod.pathOk('/v1/evidence/search?q=%E4%BD%A0'), true);
});

test('client: 错误映射——401 触发一次断开回调；403/404/422/503 有明确说明；畸形错误体被忽略', async () => {
  let auth = 0;
  const f = makeFetch((c) => {
    if (c.url === '/v1/records/person') return { status: 401, body: { code: 'unauthenticated' } };
    if (c.url === '/v1/records/place') return { status: 403, body: { code: 'cross_origin_denied' } };
    if (c.url === '/v1/records/activity') return { status: 404, body: { code: 'not_found' } };
    if (c.url === '/v1/records/artifact') return { status: 422, body: { code: 'invalid_input' } };
    if (c.url === '/v1/records/rule') return { status: 503, body: { code: 'storage_unavailable' } };
    if (c.url === '/v1/records/feedback') return { status: 500, text: '<html>boom</html>' };
    if (c.url === '/v1/records/gps_point') return { status: 403, body: { code: '<img src=x onerror=1>' } };
    return {};
  });
  const c = newClient(f, makeTimers(), { onAuthFailure: () => { auth++; } });
  const a = await c.get('/v1/records/person');
  assert.equal(a.error.authFailure, true);
  assert.equal(auth, 1);
  await c.get('/v1/records/person');
  assert.equal(auth, 1, '同一连接只回调一次');
  assert.equal((await c.get('/v1/records/place')).error.kind, 'origin');
  assert.equal((await c.get('/v1/records/activity')).error.kind, 'notfound');
  assert.equal((await c.get('/v1/records/artifact')).error.kind, 'invalid');
  const e = await c.get('/v1/records/rule');
  assert.equal(e.error.kind, 'unavailable');
  assert.equal(e.error.retryable, true);
  const fr = await c.get('/v1/records/feedback');
  assert.equal(fr.error.kind, 'server');
  assert.equal(fr.error.code, null);
  assert.equal((await c.get('/v1/records/gps_point')).error.code, null);
  assert.ok(c.setToken(TOKEN));
  await c.get('/v1/records/person');
  assert.equal(auth, 2, '重新连接后可再次回调');
});

test('client: 超时、断网、非 JSON 的成功响应、超大响应——都是明确的失败而不是空成功', async () => {
  const t = makeTimers();
  const hang = newClient(makeFetch(() => ({ hang: true })), t);
  const p = hang.get('/v1/me');
  await tick();
  t.runDue(20000);
  const r1 = await p;
  assert.equal(r1.error.kind, 'timeout');
  assert.equal(r1.error.retryable, true);
  assert.equal((await newClient(makeFetch(() => ({ reject: true })), makeTimers()).get('/v1/me')).error.kind, 'network');
  assert.equal((await newClient(makeFetch(() => ({ text: 'not json' })), makeTimers()).get('/v1/me')).error.kind, 'parse');
  assert.equal((await newClient(makeFetch(() => ({ text: '' })), makeTimers()).get('/v1/me')).error.kind, 'parse');
  assert.equal((await newClient(makeFetch(() => ({ text: '"str"' })), makeTimers()).get('/v1/me')).error.kind, 'parse', '成功响应必须是对象或数组');
  const big = 'x'.repeat(clientMod.LIMITS.maxResponseChars + 1);
  assert.equal((await newClient(makeFetch(() => ({ text: JSON.stringify({ a: big }) })), makeTimers()).get('/v1/me')).error.kind, 'too_large');
});

test('client: 并发上限——同时进行的请求不超过上限，其余排队并按序完成', async () => {
  let active = 0, peak = 0;
  const gates = [];
  const f = makeFetch(() => new Promise((resolve) => {
    active++; peak = Math.max(peak, active);
    gates.push(() => { active--; resolve({ body: { n: 1 } }); });
  }));
  const c = newClient(f, makeTimers(), { maxConcurrent: 3 });
  const ps = Array.from({ length: 8 }, (_, i) => c.get('/v1/records/person?limit=' + (i + 1)));
  await settle(4);
  assert.equal(f.calls.length, 3);
  assert.equal(c.inflight(), 3);
  assert.equal(c.queued(), 5);
  while (gates.length) { gates.shift()(); await settle(4); }
  const rs = await Promise.all(ps);
  assert.ok(rs.every((r) => r.ok));
  assert.equal(peak, 3);
  assert.equal(f.calls.length, 8);
});

test('client: 断开会取消排队请求、中止在途请求，迟到的响应被标记 stale；重新连接后不受旧响应影响', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const f = makeFetch(async (c) => { if (c.url.includes('person')) await gate; return { body: { url: c.url } }; });
  const c = newClient(f, makeTimers(), { maxConcurrent: 1 });
  const slow = c.get('/v1/records/person');
  const queued = c.get('/v1/records/place');
  await tick();
  assert.equal(c.queued(), 1);
  c.clear();
  assert.equal(c.connected(), false);
  assert.equal((await queued).stale, true, '排队中的请求直接作废');
  assert.equal(f.calls.length, 1, '排队的请求从未发出');
  release();
  const s = await slow;
  assert.equal(s.stale, true);
  assert.equal(s.ok, false);
  const after = await c.get('/v1/me');
  assert.equal(after.notConnected, true);
  c.setToken(TOKEN);
  assert.equal((await c.get('/v1/records/rule')).ok, true);
});

}

// ===== Recovered md regression cases, targeting flat bundle =====
{
'use strict';
// 工作台 · 安全 Markdown：正确渲染、不可信文本不会变成链接/图片/脚本、资源上限。
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const dom = flatFake;

const WS = STATIC;
const md = bundle.md;

function render(text) {
  const doc = dom.makeDom(path.join(WS, 'workspace.html'));
  const r = md.renderMarkdown(doc, text);
  return { doc, node: r.node, stats: r.stats };
}
const tags = (root) => new Set(dom.findAll(root, () => true).map((n) => n.localName));

test('md: 标题、段落、强调、行内代码、删除线', () => {
  const { node } = render('# 一级\n\n## 二级\n\n正常 **加粗** 与 *斜体* 以及 `code` 与 ~~删除~~。\n第二行');
  assert.equal(dom.byTag(node, 'h3').length, 1);
  assert.equal(dom.byTag(node, 'h4').length, 1);
  assert.equal(dom.byTag(node, 'strong')[0].textContent, '加粗');
  assert.equal(dom.byTag(node, 'em')[0].textContent, '斜体');
  assert.equal(dom.byTag(node, 'code')[0].textContent, 'code');
  assert.equal(dom.byTag(node, 'del')[0].textContent, '删除');
  assert.equal(dom.byTag(node, 'br').length, 1, '软换行按硬换行显示，符合笔记式产物');
});

test('md: 列表（含嵌套、有序起始号、任务项）', () => {
  const { node } = render('- 甲\n- 乙\n  - 乙一\n  - 乙二\n- 丙\n\n3. 三\n4. 四\n\n- [ ] 待办\n- [x] 已办');
  const uls = dom.byTag(node, 'ul');
  assert.equal(uls.length, 3, '外层、嵌套、任务列表');
  assert.equal(dom.byTag(uls[0], 'li').length, 5, '外层 3 项 + 嵌套 2 项');
  const ol = dom.byTag(node, 'ol')[0];
  assert.equal(ol.attrs.start, '3');
  assert.equal(dom.byTag(ol, 'li').length, 2);
  const tasks = dom.byClass(node, 'md-checkbox');
  assert.deepEqual(tasks.map((t) => t.attrs['aria-label']), ['未完成', '已完成']);
});

test('md: 引用、分隔线、代码块、表格', () => {
  const { node } = render('> 引用 **粗**\n\n---\n\n```js\nconst a = "<b>";\n```\n\n| 名 | 值 |\n|:--|--:|\n| a | 1 |\n| b | 2 |');
  assert.equal(dom.byTag(node, 'blockquote').length, 1);
  assert.equal(dom.byTag(node, 'hr').length, 1);
  const pre = dom.byTag(node, 'pre')[0];
  assert.equal(pre.textContent, 'const a = "<b>";');
  assert.equal(pre.attrs['aria-label'], '代码块（js）');
  const table = dom.byTag(node, 'table')[0];
  assert.equal(dom.byTag(table, 'th').length, 2);
  assert.equal(dom.byTag(table, 'td').length, 4);
  assert.equal(dom.byTag(table, 'th')[1].attrs.class, 'md-align-right');
  assert.equal(dom.byTag(table, 'th')[0].attrs.scope, 'col');
});

test('md: 行内细节——下划线单词不变斜体、转义、实体、setext 标题', () => {
  const { node } = render('snake_case_word 与 2 * 3 * 4\n\n\\*不是斜体\\* &amp; &lt;b&gt;\n\n标题\n===');
  assert.equal(dom.byTag(node, 'em').length, 0);
  assert.ok(node.textContent.includes('snake_case_word'));
  assert.ok(node.textContent.includes('*不是斜体* & <b>'));
  assert.equal(dom.byTag(node, 'h3')[0].textContent, '标题');
});

test('md: 原始 HTML 只显示为文字，绝不生成对应元素', () => {
  const { node, doc } = render('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n<iframe src="https://evil.example"></iframe> <a href="javascript:alert(1)">点我</a>\n\n<style>body{display:none}</style>');
  const t = tags(node);
  for (const bad of ['script', 'img', 'iframe', 'a', 'style', 'link', 'object', 'embed', 'form', 'input', 'svg']) assert.equal(t.has(bad), false, bad);
  assert.ok(node.textContent.includes('<script>alert(1)</script>'));
  assert.ok(node.textContent.includes('<img src=x onerror=alert(1)>'));
  assert.deepEqual(doc.styleLog, []);
});

test('md: 链接不可点击，只显示文字与地址；图片不加载，只显示占位', () => {
  const { node, doc, stats } = render('看 [官网](https://example.com/a?token=SECRET#frag) 与 [脚本](javascript:alert(1)) 与 ![追踪像素](https://tracker.example/p.gif) 与 https://bare.example/x, 以及 <https://auto.example/y>。');
  assert.equal(dom.byTag(node, 'a').length, 0);
  assert.equal(dom.byTag(node, 'img').length, 0);
  assert.equal(stats.images, 1);
  assert.equal(stats.links, 4);
  const text = node.textContent;
  assert.ok(text.includes('官网'));
  assert.ok(text.includes('链接未启用：https://example.com/a?token=SECRET#frag'));
  assert.ok(text.includes('链接未启用：javascript:alert(1)'));
  assert.ok(text.includes('[图片已屏蔽，未加载：追踪像素]'));
  assert.ok(text.includes('https://bare.example/x'));
  assert.ok(text.includes('https://auto.example/y'));
  // 任何元素都不能带有会触发加载/导航/脚本的属性。
  const allowed = new Set(['class', 'aria-label', 'role', 'start', 'scope']);
  for (const rec of doc.attrLog) assert.ok(allowed.has(rec.name), rec.tag + '[' + rec.name + ']');
});

test('md: 超长输入被截断并明示；极深嵌套与大量块不会崩溃或卡死', () => {
  const huge = 'x'.repeat(md.LIMITS.maxChars + 5000);
  const a = render(huge);
  assert.ok(a.stats.truncated);
  assert.ok(a.node.textContent.includes('已截断'));

  const deepQuote = '>'.repeat(5000) + ' 深';
  const t0 = Date.now();
  const b = render(deepQuote);
  assert.ok(b.node.textContent.includes('深'));
  let nested = '';
  for (let i = 0; i < 400; i++) nested += ' '.repeat(i * 2) + '- 层 ' + i + '\n';
  const c = render(nested);
  assert.ok(c.node.textContent.includes('层 0'));

  const manyBlocks = Array.from({ length: md.LIMITS.maxBlocks + 500 }, (_, i) => '# H' + i).join('\n\n');
  const d = render(manyBlocks);
  assert.ok(d.stats.truncated);
  assert.ok(Date.now() - t0 < 5000, '病态输入应在数秒内处理完');
});

test('md: 非字符串输入渲染为空容器；行内渲染折叠换行并仍然只用文本节点', () => {
  const doc = dom.makeDom(path.join(WS, 'workspace.html'));
  const r = md.renderMarkdown(doc, null);
  assert.equal(r.node.textContent, '');
  const i = md.renderInline(doc, '**标题**\n第二行 [x](http://e.example)');
  assert.equal(dom.byTag(i.node, 'br').length, 0);
  assert.equal(dom.byTag(i.node, 'a').length, 0);
  assert.ok(i.node.textContent.includes('标题 第二行'));
});

}

// ===== Recovered app regression cases, targeting flat bundle =====
{
'use strict';
// 工作台 · 页面流程（一）：连接会话、令牌只在内存、权限/空/错误/加载状态、导航与键盘可达性、只读保证。
const test = require('node:test');
const assert = require('node:assert/strict');
const { bootWorkspace, settle, TOKEN, core, fake, mock } = flatEnv;

const { id } = mock;

test('session: 未连接时仅发现公开模式，不发业务请求；侧边栏导航含全部 9 个视图与插件管理链接', async () => {
  const env = bootWorkspace();
  await env.page.discovery;
  assert.ok(env.text().includes('请先连接'));
  assert.deepEqual(env.gets(), ['/v1/session']);
  const navLinks = fake.links(env.n('nav-list'));
  assert.deepEqual(navLinks.map((a) => a.textContent), ['概览', '证据时间线', '活动', '产物', '人物', '地点', '规则', '动作建议', '追溯']);
  assert.deepEqual(navLinks.map((a) => a.attrs.href), ['#/overview', '#/timeline', '#/activities', '#/artifacts', '#/people', '#/places', '#/rules', '#/actions', '#/trace']);
  assert.equal(env.n('plugins-link').attrs.href, '../plugins/', '链接到现有插件管理页，不替换它');
  assert.equal(env.n('session-badge').textContent, '未连接');
  assert.equal(env.n('disconnect-btn').hidden, true);
});

test('session: 令牌格式不对不会发请求；正确令牌连接后输入框立即清空，令牌只在 Authorization 头', async () => {
  const env = bootWorkspace();
  env.n('token-input').value = 'short';
  env.n('connect-form').dispatch('submit');
  await settle();
  assert.deepEqual(env.gets(), ['/v1/session']);
  assert.equal(env.n('banner').hidden, false);
  assert.ok(env.n('banner').textContent.includes('43 位'));

  await env.connect();
  assert.equal(env.n('token-input').value, '', '连接后输入框清空');
  assert.equal(env.n('token-input').disabled, true);
  assert.equal(env.n('disconnect-btn').hidden, false);
  assert.ok(env.n('session-badge').textContent.includes('tester'));
  assert.ok(env.n('session-badge').textContent.includes('用户'));
  for (const c of env.f.calls) {
    assert.equal(c.method, 'GET');
    assert.ok(!c.url.includes(TOKEN));
    if (c.url.startsWith('/v1/') && c.url !== '/v1/session') assert.equal(c.headers.Authorization, 'Bearer ' + TOKEN);
  }
  // 令牌不会出现在页面任何文字、属性或地址里
  const dump = [];
  fake.walk(env.doc.body, (n) => { dump.push(n.textContent, n.value, JSON.stringify(n.attrs)); });
  assert.ok(!dump.join('\n').includes(TOKEN));
  assert.ok(!env.win.location.href.includes(TOKEN));
  assert.deepEqual(env.win.storageLog, [], '从未访问 localStorage / sessionStorage / indexedDB');
  assert.deepEqual(env.win.historyLog, []);
});

test('session: 连接失败（401/403 跨源/503）显示明确原因，且不保留令牌、不进入已连接状态', async () => {
  for (const [status, code, expect] of [[401, 'unauthenticated', '令牌无效'], [403, 'cross_origin_denied', 'public_origin'], [503, 'storage_unavailable', '存储服务']]) {
    const env = bootWorkspace({ handler: () => ({ status, body: { code } }) });
    await env.connect();
    assert.equal(env.page.state().connected, false, String(status));
    assert.ok(env.n('banner').textContent.includes(expect), status + ': ' + env.n('banner').textContent);
    assert.equal(env.n('banner').hidden, false);
    assert.equal(env.n('token-input').disabled, false);
    assert.equal(env.n('token-input').value, '');
    assert.ok(env.text().includes('请先连接'));
  }
  const odd = bootWorkspace({ handler: (c) => (c.url === '/v1/me' ? { body: { subject: 5, role: 'root' } } : {}) });
  await odd.connect();
  assert.equal(odd.page.state().connected, false);
  assert.ok(odd.n('banner').textContent.includes('格式异常'));
});

test('session: 断开会清除令牌与全部页面数据、取消请求，之后不再发请求', async () => {
  const env = bootWorkspace();
  await env.connect();
  await env.go('#/people/' + id('person', 1));
  assert.ok(env.text().includes('合成人物甲'));
  const before = env.f.calls.length;
  env.n('disconnect-btn').dispatch('click');
  await settle();
  assert.equal(env.page.state().connected, false);
  assert.ok(env.text().includes('请先连接'));
  assert.ok(!env.text().includes('合成人物甲'), '断开后页面不残留业务数据');
  assert.equal(env.n('token-input').disabled, false);
  await env.go('#/timeline');
  assert.equal(env.f.calls.length, before, '断开后切换路由也不发请求');
});

test('session: 401（令牌被撤销）在任意页面都会自动断开并说明原因', async () => {
  let revoked = false;
  const world = mock.createWorld();
  const inner = mock.createHandler(world, {});
  const env = bootWorkspace({ world, handler: (c) => (revoked && c.url.startsWith('/v1/records') ? { status: 401, body: { code: 'unauthenticated' } } : inner(c)) });
  await env.connect();
  await env.go('#/people');
  assert.ok(env.text().includes('合成人物甲'));
  revoked = true;
  await env.go('#/rules');
  assert.equal(env.page.state().connected, false);
  assert.ok(env.n('banner').textContent.includes('已断开'));
  assert.ok(env.text().includes('请先连接'));
  assert.ok(!env.text().includes('提取待办事项'));
});

test('router: 非法哈希回到概览并提示；路由里没有令牌；标题与 aria-current 随视图变化', async () => {
  const env = bootWorkspace();
  await env.connect();
  await env.go('#/artifacts/../../etc');
  assert.equal(env.page.state().route.view, 'overview');
  assert.equal(env.n('banner').hidden, false);
  assert.ok(env.n('banner').textContent.includes('地址无法识别'));
  assert.equal(env.win.location.hash, '#/overview');
  await env.go('#/rules');
  assert.equal(env.n('nav-rules').attrs['aria-current'], 'page');
  assert.equal(env.n('nav-overview').attrs['aria-current'], undefined);
  assert.equal(env.doc.title, 'Ear 工作台 · 规则');
  assert.ok(!env.win.location.hash.includes(TOKEN));
});

test('a11y: 路由切换后焦点移到视图标题；跳转链接；菜单按钮 aria-expanded；每个区块有标签；表格有表头范围', async () => {
  const env = bootWorkspace();
  await env.connect();
  await env.go('#/people');
  const title = fake.find(env.root(), (n) => n.attrs.id === 'view-title');
  assert.ok(title && title.localName === 'h2');
  assert.equal(title.attrs.tabindex, '-1', '标题可程序聚焦但不进入 Tab 顺序');
  assert.equal(env.doc.activeElement, title);

  env.n('skip-link').dispatch('click');
  assert.equal(env.doc.activeElement, env.n('main'));
  assert.equal(env.n('nav-toggle').attrs['aria-expanded'], 'false');
  env.n('nav-toggle').dispatch('click');
  assert.equal(env.n('nav-toggle').attrs['aria-expanded'], 'true');
  assert.ok(env.n('sidebar').classList.contains('open'));
  env.win.fire('keydown', { key: 'Escape' });
  assert.equal(env.n('nav-toggle').attrs['aria-expanded'], 'false');
  await env.go('#/people');

  await env.go('#/overview');
  await settle(60);
  const sections = fake.byTag(env.root(), 'section');
  assert.ok(sections.length >= 2);
  for (const s of sections) {
    const labelled = s.attrs['aria-labelledby'];
    assert.ok(labelled, '区块必须有可访问名称');
    assert.ok(fake.find(env.root(), (n) => n.attrs.id === labelled), 'aria-labelledby 指向存在的标题');
  }
  const ths = fake.byTag(env.root(), 'th');
  assert.ok(ths.length > 5);
  for (const th of ths) assert.ok(th.attrs.scope === 'col' || th.attrs.scope === 'row', '表头必须声明 scope');
  assert.ok(fake.byTag(env.root(), 'caption').length >= 1);
  // 每个输入都有关联的 label（for → id）
  await env.go('#/timeline');
  await settle(60);
  const labelFor = new Set(fake.byTag(env.root(), 'label').map((l) => l.attrs.for).filter(Boolean));
  for (const input of fake.findAll(env.root(), (n) => n.localName === 'input' && n.attrs.type !== 'checkbox' || n.localName === 'select')) {
    assert.ok(labelFor.has(input.attrs.id) || input.attrs['aria-label'], '输入框缺少 label：' + input.attrs.id);
  }
});

test('overview: 逐类给出 有数据/无数据/无授权/读取失败，不显示总数；可跳转到对应页面', async () => {
  const env = bootWorkspace({ api: { denied: ['voiceprint'], fail: { '/v1/records/place': { status: 503, body: { code: 'storage_unavailable' } } } } });
  await env.connect();
  await settle(80);
  const rows = Object.fromEntries(fake.byTag(env.root(), 'tr').map((tr) => [tr.children[0].textContent, tr.textContent]).filter(([k]) => k));
  const row = (label) => Object.entries(rows).find(([k]) => k.startsWith(label))[1];
  assert.ok(row('人物').includes('有数据'));
  assert.ok(row('声纹').includes('无读取授权或不可见'), '403 显示为无授权，而不是“没有数据”');
  assert.ok(row('地点').includes('读取失败'));
  assert.ok(row('环境声').includes('没有数据'));
  assert.ok(!/共\s*\d+\s*(条|个)|总数\s*[:：]?\s*\d/.test(env.text().replace(/不显示总数/g, '')), '不编造总数');
  const open = fake.links(env.root()).map((a) => a.attrs.href);
  assert.ok(open.includes('#/people') && open.includes('#/activities'));
  assert.ok(open.includes('#/trace/gps_point'), '没有专属页面的类型落到追溯页');
  // 17 个类型各探测一次，且只读 1 条
  const probes = env.gets().filter((u) => /^\/v1\/records\/[a-z_]+\?limit=1$/.test(u));
  assert.equal(probes.length, 17);
  assert.equal(new Set(probes).size, 17);
});

test('overview: 管理员看到插件管理页提示，普通用户没有；服务未就绪会说明', async () => {
  const admin = bootWorkspace({ api: { role: 'admin' } });
  await admin.connect();
  await settle(60);
  assert.ok(admin.text().includes('你是管理员'));
  assert.ok(admin.n('session-badge').textContent.includes('管理员'));
  const user = bootWorkspace();
  await user.connect();
  await settle(60);
  assert.ok(!user.text().includes('你是管理员'));
  assert.ok(user.text().includes('服务就绪'));
  const down = bootWorkspace({ api: { fail: { '/health/ready': { status: 503, body: { status: 'unavailable' } } } } });
  await down.connect();
  await settle(60);
  assert.ok(down.text().includes('服务未就绪'));
});

test('states: 列表页的 加载中 / 空 / 无授权 / 失败可重试，且失败后重试成功', async () => {
  let fail = true;
  const world = mock.createWorld();
  const inner = mock.createHandler(world, {});
  const env = bootWorkspace({ world, handler: (c) => (fail && c.url.startsWith('/v1/records/person?') && !c.url.includes('limit=1&') && !c.url.endsWith('limit=1') ? { status: 503, body: { code: 'storage_unavailable' } } : inner(c)) });
  await env.connect();
  await env.go('#/people');
  assert.ok(env.text().includes('存储服务暂时不可用'));
  const retry = fake.buttons(env.root(), '重试')[0];
  assert.ok(retry, '可重试的错误提供重试按钮');
  fail = false;
  retry.dispatch('click');
  await settle(60);
  assert.ok(env.text().includes('合成人物甲'));

  const empty = bootWorkspace({ world: { records: [] } });
  await empty.connect();
  await empty.go('#/people');
  assert.ok(empty.text().includes('没有读到人物'));

  const denied = bootWorkspace({ api: { denied: ['person'] } });
  await denied.connect();
  await denied.go('#/people');
  assert.ok(denied.text().includes('没有读取这类数据的授权'));
  assert.equal(fake.byClass(denied.root(), 'state-denied').length >= 1, true);
});

test('read-only: 整个浏览过程只发 GET；页面没有写操作入口（审核/执行/改名/纠正/提交）', async () => {
  const env = bootWorkspace({ api: { role: 'admin' } });
  await env.connect();
  const A = id('artifact', 1);
  for (const h of ['#/overview', '#/timeline', '#/people/' + id('person', 1), '#/places/' + id('place', 1), '#/activities/' + id('activity', 1), '#/artifacts/' + A, '#/artifacts/' + A + '/v/1',
    '#/rules/' + id('rule', 1), '#/actions/' + id('suggestion', 1), '#/trace', '#/trace/' + id('person', 1)]) {
    await env.go(h);
    await settle(60);
    const labels = fake.findAll(env.doc.body, (n) => n.localName === 'button' || n.localName === 'a').map((n) => n.textContent).join('|');
    assert.ok(!/批准|通过审核|拒绝建议|执行动作|确认已执行|风险重试|提交反馈|改名|纠正|删除|合并|新建|保存|领取|更换凭据/.test(labels), h + ' 出现了写操作入口：' + labels.slice(0, 200));
  }
  assert.ok(env.f.calls.length > 40);
  assert.deepEqual(env.f.calls.filter((c) => c.method !== 'GET'), []);
  assert.ok(!env.gets().some((u) => /feedback$|\/prepare|\/pump|\/recovery|\/management|\/plugins|\/previews\/reextract/.test(u)), '没有调用任何写入或管理接口');
  assert.equal(env.doc.attrLog.filter((a) => /^on|^style$|^src$|^srcdoc$|^formaction$/i.test(a.name)).length, 0);
  assert.equal(env.doc.styleLog.length, 0, 'CSP 禁止内联样式：页面从不访问 style');
  assert.deepEqual(env.win.storageLog, []);
});

test('read-only: 所有 <a> 的 href 只能是站内哈希路由（不可能由业务文本生成外链）', async () => {
  const world = mock.createWorld();
  const p = world.records.find((r) => r.data_type === 'person' && r.version === 2);
  p.payload.name = '<a href="https://evil.example">点我</a> javascript:alert(1)';
  const env = bootWorkspace({ world });
  await env.connect();
  await env.go('#/people/' + id('person', 1));
  await settle(60);
  assert.ok(env.text().includes('<a href="https://evil.example">点我</a>'), '以文字显示');
  // 动态生成的链接（导航与视图）只能是站内哈希；整个文档里唯一的非哈希链接是 index.html 里写死的插件管理页链接。
  for (const root of [env.root(), env.n('nav-list')]) {
    for (const a of fake.links(root)) assert.match(a.attrs.href, /^#\/[a-z]+(\/|$)/, a.attrs.href);
  }
  const nonHash = fake.links(env.doc.body).filter((a) => !a.attrs.href.startsWith('#/'));
  assert.deepEqual(nonHash.map((a) => a.attrs.href), ['../plugins/']);
  assert.equal(nonHash[0].attrs.id, 'plugins-link', '它是静态链接，不是脚本生成的');
  assert.equal(fake.byTag(env.doc.body, 'img').length, 0);
  assert.equal(fake.byTag(env.doc.body, 'script').length, 0);
});

}

// ===== Recovered pages regression cases, targeting flat bundle =====
{
'use strict';
// 工作台 · 页面流程（二）：时间线与时间范围/时区、分页、人物/地点/活动/产物/规则/动作建议的当前/详情/版本、追溯、证据检索。
const test = require('node:test');
const assert = require('node:assert/strict');
const { bootWorkspace, settle, core, fake, mock } = flatEnv;

const { id, sem, raw } = mock;
const A1 = id('activity', 1);

async function ready(opts, hash) {
  const env = bootWorkspace(opts);
  await env.connect();
  if (hash) { await env.go(hash); await settle(80); }
  return env;
}
const clickText = async (env, label, root) => {
  const b = fake.buttons(root || env.root(), label)[0];
  assert.ok(b, '找不到按钮：' + label);
  b.dispatch('click');
  await settle(80);
  return b;
};
const listLinks = (env) => fake.links(env.root()).map((a) => a.attrs.href);

// ------------------------------------------------------------------ 时间线

test('timeline: 自动定位到最早活动所在的日期（按所选时区），请求带完整 UTC 半开区间；显示活动与 SVG 时间条', async () => {
  const env = await ready({}, '#/timeline');
  // 最早活动 09:00Z；本地时区是 UTC+8 → 2026-10-29 17:00，日期仍是 29 日，查询区间是本地日的 UTC 边界。
  const q = env.gets().find((u) => u.startsWith('/v1/records/activity?') && u.includes('start='));
  assert.ok(q, env.gets().join('\n'));
  const p = new URL(q, 'http://x').searchParams;
  assert.equal(p.get('start'), '2026-10-28T16:00:00Z');
  assert.equal(p.get('end'), '2026-10-29T16:00:00Z');
  assert.ok(env.text().includes('已自动定位到最早一条活动所在的日期'));
  assert.ok(env.text().includes('预算会议') && env.text().includes('午间散步'));
  const svg = fake.find(env.root(), (n) => n.localName === 'svg');
  assert.ok(svg && svg.attrs.role === 'img' && svg.attrs['aria-label'].includes('2 个活动'));
  assert.equal(fake.byTag(svg, 'rect').filter((r) => r.attrs.class.includes('tl-bar')).length, 2);
  assert.ok(fake.byTag(svg, 'title').length >= 2, '每个条都有文字标题');
  // 活动列表才是可键盘访问的完整内容：每个活动有链接
  assert.ok(listLinks(env).includes('#/activities/' + A1));
  assert.ok(fake.buttons(env.root(), '加载更多').every((b) => b.hidden), '没有更多页时按钮是隐藏的');
});

test('timeline: 时区切换后同一时刻显示不同的本地时间，并重新解释日期输入', async () => {
  const env = await ready({}, '#/timeline');
  assert.ok(env.text().includes('2026-10-29 17:00'), '本地（UTC+8）');
  env.n('zone-select').value = 'utc';
  env.n('zone-select').dispatch('change');
  assert.ok(env.n('live-status').textContent.includes('时区已切换为 UTC'), '切换时立即向读屏器播报（之后的“已加载”播报会替换它）');
  await settle(80);
  assert.equal(env.page.state().zone, 'utc');
  assert.ok(env.text().includes('2026-10-29 09:00'), 'UTC');
  assert.ok(!env.text().includes('2026-10-29 17:00'));
  const q = env.gets().filter((u) => u.startsWith('/v1/records/activity?') && u.includes('start=')).pop();
  assert.equal(new URL(q, 'http://x').searchParams.get('start'), '2026-10-29T00:00:00Z', '按 UTC 日界重新查询');
});

test('timeline: 自定义范围只填一端会被拒绝且不发请求；填完整后以 UTC 半开区间查询；前一天/后一天可翻日', async () => {
  const env = await ready({}, '#/timeline');
  const before = env.gets().length;
  const mode = fake.find(env.root(), (n) => n.localName === 'select' && n.attrs.id.startsWith('range-mode'));
  mode.value = 'custom';
  mode.dispatch('change');
  const start = fake.find(env.root(), (n) => n.attrs.id && n.attrs.id.startsWith('range-start'));
  const end = fake.find(env.root(), (n) => n.attrs.id && n.attrs.id.startsWith('range-end'));
  start.value = '2026-10-29T17:00';
  start.dispatch('change');
  fake.buttons(env.root(), '应用范围')[0].dispatch('click');
  const form = fake.find(env.root(), (n) => n.localName === 'form' && n.attrs['aria-label'] === '时间范围');
  form.dispatch('submit');
  await settle(40);
  assert.equal(env.gets().length, before, '半个范围不发请求');
  assert.ok(fake.byClass(env.root(), 'range-error')[0].textContent.includes('同时填写'));
  assert.equal(fake.byClass(env.root(), 'range-error')[0].hidden, false);
  assert.equal(end.value, '', '不替用户补默认值');

  end.value = '2026-10-29T18:00';
  end.dispatch('change');
  form.dispatch('submit');
  await settle(80);
  const q = env.gets().filter((u) => u.startsWith('/v1/records/activity?') && u.includes('start=')).pop();
  const sp = new URL(q, 'http://x').searchParams;
  assert.equal(sp.get('start'), '2026-10-29T09:00:00Z');
  assert.equal(sp.get('end'), '2026-10-29T10:00:00Z');
  assert.ok(env.text().includes('预算会议'));
  assert.ok(!env.text().includes('午间散步'), '10:00Z 的活动在半开区间 [09:00, 10:00) 之外');
  assert.ok(fake.byClass(env.root(), 'range-note')[0].textContent.includes('[2026-10-29T09:00:00Z, 2026-10-29T10:00:00Z)'));
});

test('timeline: 前一天/后一天翻日；空日显示明确的空状态而不是空白', async () => {
  const env = await ready({}, '#/timeline');
  await clickText(env, '后一天');
  const q = env.gets().filter((u) => u.startsWith('/v1/records/activity?') && u.includes('start=')).pop();
  assert.equal(new URL(q, 'http://x').searchParams.get('start'), '2026-10-29T16:00:00Z');
  assert.ok(env.text().includes('这个时间范围内没有读到活动'));
  assert.ok(env.text().includes('提取已运行'), '空状态说明可能的原因');
});

test('timeline: 点“证据”按活动时间范围解析证据 ID，显示类型、时间与文本；找不到的明确标注', async () => {
  const world = mock.createWorld();
  world.records.find((r) => r.id === A1).payload.evidence_ids.push(id('transcript', 99));
  const env = await ready({ world }, '#/timeline');
  await clickText(env, '证据（5）');
  const t = env.text();
  assert.ok(t.includes('我们下周三开会讨论预算'));
  assert.ok(t.includes('转写段') && t.includes('GPS 点') && t.includes('声纹'));
  assert.ok(t.includes('未能解析'), '不存在的证据明确标注，不假装解析成功');
  assert.ok(t.includes('已解析 4 个'));
  const btn = fake.buttons(env.root(), '证据（5）')[0];
  assert.equal(btn.attrs['aria-expanded'], 'true');
  btn.dispatch('click');
  assert.equal(btn.attrs['aria-expanded'], 'false');
});

test('timeline: 原始证据流分类型读取并分页（加载更多、按 id+版本去重、不重复追加）', async () => {
  const world = mock.createWorld();
  for (let i = 0; i < 70; i++) {
    world.records.push(raw('gps_point', 'gps', 100 + i, '2026-10-29T09:' + String(i % 60).padStart(2, '0') + ':30Z', '2026-10-29T09:' + String(i % 60).padStart(2, '0') + ':30Z', { latitude: 39.9 + i / 1000, longitude: 116.3 }));
  }
  const env = await ready({ world }, '#/timeline');
  await clickText(env, 'GPS 点');
  assert.ok(env.text().includes('已加载 50 条'), env.text().slice(-300));
  assert.ok(env.text().includes('还有更多'));
  const more = fake.buttons(env.root(), '加载更多').find((b) => !b.hidden);
  assert.ok(more);
  more.dispatch('click');
  await settle(80);
  assert.ok(env.text().includes('已加载 72 条'), '50 + 22（含原有 2 个）；没有重复');
  assert.ok(env.text().includes('已到末尾（实时读取，不是快照）'));
  assert.ok(fake.buttons(env.root(), '加载更多').every((b) => b.hidden));
  const cursors = env.gets().filter((u) => u.startsWith('/v1/records/gps_point?') && u.includes('cursor='));
  assert.equal(cursors.length, 1);
  assert.ok(cursors[0].includes('start=') && cursors[0].includes('end='), '翻页时范围参数保持不变');
});

test('timeline: 证据检索——正常命中显示说话人、录音位置与一致性说明；未启用路由时明确说明而不是报错', async () => {
  const env = await ready({}, '#/timeline');
  const input = fake.find(env.root(), (n) => n.localName === 'input' && n.attrs.placeholder && n.attrs.placeholder.includes('短语'));
  const form = fake.find(env.root(), (n) => n.attrs.class === 'row search-form');
  form.dispatch('submit');
  await settle(20);
  assert.ok(env.text().includes('请输入要检索的短语'), '空短语不发请求');
  assert.equal(env.gets().filter((u) => u.startsWith('/v1/evidence')).length, 0);
  input.value = '下周三';
  form.dispatch('submit');
  await settle(80);
  assert.ok(env.text().includes('人物：合成人物甲（已识别为人物）'));
  assert.ok(env.text().includes('0:00 – 0:20') === false);
  assert.ok(env.text().includes('5:00 – 5:20'), '录音内偏移');
  assert.ok(env.text().includes('本页不提供音频播放'), '不假装能播放');
  assert.ok(env.text().includes('实时尽力而为'));
  assert.ok(env.text().includes('停止原因：该时间窗内已读完'));
  assert.equal(fake.byTag(env.root(), 'mark').length, 2, '命中短语被高亮（文本节点，不是 HTML）');
  assert.equal(fake.byTag(env.root(), 'audio').length + fake.byTag(env.root(), 'video').length, 0);
  const url = env.gets().find((u) => u.startsWith('/v1/evidence/search'));
  const sp = new URL(url, 'http://x').searchParams;
  assert.equal(sp.get('q'), '下周三');
  assert.equal(sp.get('limit'), '10');
  assert.equal(sp.get('start'), '2026-10-28T16:00:00Z');

  const off = await ready({ api: { evidence: false } }, '#/timeline');
  const i2 = fake.find(off.root(), (n) => n.localName === 'input' && n.attrs.placeholder && n.attrs.placeholder.includes('短语'));
  i2.value = '预算';
  fake.find(off.root(), (n) => n.attrs.class === 'row search-form').dispatch('submit');
  await settle(80);
  assert.ok(off.text().includes('证据检索接口不可用或无权访问'));
  assert.ok(off.text().includes('其他功能不受影响'));
  assert.ok(off.text().includes('预算会议'), '时间线其余部分照常工作');
});

test('timeline: 证据检索结果里的不可信文本只以文字显示；被截断/不完整的结果会明示，不冒充完整', async () => {
  const items = [{
    transcript_segment_id: id('transcript', 1), transcript_segment_version: 1, start: '2026-10-29T09:05:00Z', end: '2026-10-29T09:05:20Z',
    text: '<img src=x onerror=alert(1)> 预算 <script>', local_speaker_id: null, voiceprint_id: null, audio_ref_id: null, playback: null, playback_status: 'unavailable',
    attribution: { id: id('attribution', 1), version: 1, person_id: id('person', 1), person_version: 1, person_status: 'active', name: '<b>甲</b>' }, attribution_status: 'resolved'
  }];
  const handler = (c) => {
    if (c.url.startsWith('/v1/evidence/search')) return { body: { items, next_cursor: 'nxt', truncated: true, scanned: 100, consistency: 'live_best_effort', report: { stop_reason: 'deadline', continuation: 'available', pages: 3, calls: 9, read_rows: 100 } } };
    return mock.createHandler(mock.createWorld(), {})(c);
  };
  const env = await ready({ handler }, '#/timeline');
  const input = fake.find(env.root(), (n) => n.localName === 'input' && n.attrs.placeholder && n.attrs.placeholder.includes('短语'));
  input.value = '预算';
  fake.find(env.root(), (n) => n.attrs.class === 'row search-form').dispatch('submit');
  await settle(80);
  assert.ok(env.text().includes('<img src=x onerror=alert(1)>'));
  assert.ok(env.text().includes('人物：<b>甲</b>'));
  assert.equal(fake.byTag(env.root(), 'img').length + fake.byTag(env.root(), 'script').length + fake.byTag(env.root(), 'b').length, 0);
  assert.ok(env.text().includes('读取超时，结果不完整'));
  assert.ok(env.text().includes('结果可能不完整'));
  assert.ok(fake.buttons(env.root(), '继续检索').length === 1, '有游标时可以继续');
  fake.buttons(env.root(), '继续检索')[0].dispatch('click');
  await settle(60);
  const cont = env.gets().filter((u) => u.startsWith('/v1/evidence/search')).pop();
  assert.equal(new URL(cont, 'http://x').searchParams.get('cursor'), 'nxt');
});

// ------------------------------------------------------------------ 人物 / 地点

test('people: 列表→详情；当前版本 v2 有名字，版本历史含 v1 未命名；历史版本可单独查看并回到当前', async () => {
  const P = id('person', 1);
  const env = await ready({}, '#/people');
  assert.ok(env.text().includes('合成人物甲') && env.text().includes('参考声纹 1'));
  assert.ok(listLinks(env).includes('#/people/' + P));
  await env.go('#/people/' + P);
  await settle(80);
  const t = env.text();
  assert.ok(t.includes('当前版本') && t.includes('v2'));
  assert.ok(t.includes('已归属到此人物的记录数') && t.includes('1（来自预览接口'));
  assert.ok(t.includes('声纹向量属于生物特征，本页不显示'));
  assert.ok(!t.includes('0.01') || !/"values"/.test(t));

  await clickText(env, '加载版本历史');
  assert.ok(env.text().includes('初始版本'), 'v1');
  assert.ok(env.text().includes('name'), 'v2 相对 v1 变化的字段');
  const diffBtn = fake.buttons(env.root(), '对比')[0];
  assert.equal(diffBtn.attrs['aria-expanded'], 'false');
  diffBtn.dispatch('click');
  assert.equal(diffBtn.attrs['aria-expanded'], 'true');
  const diffRow = fake.byClass(env.root(), 'diff-row')[0];
  assert.equal(diffRow.hidden, false);
  assert.ok(diffRow.textContent.includes('合成人物甲') && diffRow.textContent.includes('null'));

  const v1 = fake.links(env.root()).find((a) => a.attrs.href === '#/people/' + P + '/v/1');
  assert.ok(v1, '历史版本有链接');
  await env.go('#/people/' + P + '/v/1');
  await settle(80);
  assert.ok(env.text().includes('正在查看历史版本 v1；当前版本是 v2'));
  assert.ok(env.text().includes('未命名人物 #1'));
  assert.ok(fake.links(env.root()).some((a) => a.attrs.href === '#/people/' + P && a.textContent.includes('回到当前版本')));
  assert.ok(env.gets().includes('/v1/records/person/' + P + '?version=1'));
});

test('people: 未知版本号→明确的“没有找到/无授权”，并提供回到当前版本的入口；筛选只作用于已加载记录并如实说明', async () => {
  const P = id('person', 1);
  const env = await ready({}, '#/people/' + P + '/v/9');
  assert.ok(env.text().includes('没有找到'));
  assert.ok(fake.links(env.root()).some((a) => a.textContent.includes('回到当前版本')));
  const filter = fake.find(env.root(), (n) => n.localName === 'input' && n.attrs.placeholder && n.attrs.placeholder.includes('筛选'));
  filter.value = '不存在的名字';
  filter.dispatch('input');
  assert.ok(env.text().includes('已加载的 1 条里没有匹配'), '不说“没有这个人”，只说已加载的范围内');
});

test('people: “包含已被整体替代的对象”开关改变请求参数；终止版本有醒目标记', async () => {
  const world = mock.createWorld();
  world.records.push(sem('person', 5, 1, { name: '已替代的人', references: [] }, { writer: 'attribution' }));
  world.records.push(sem('person', 5, 2, { name: '已替代的人', references: [] }, { writer: 'attribution', status: 'superseded' }));
  const env = await ready({ world }, '#/people');
  assert.ok(!env.text().includes('已替代的人'));
  const cb = fake.find(env.root(), (n) => n.localName === 'input' && n.attrs.type === 'checkbox');
  cb.checked = true;
  cb.dispatch('change');
  await settle(80);
  assert.ok(env.gets().some((u) => u.startsWith('/v1/records/person?') && u.includes('include_superseded=true')));
  assert.ok(env.text().includes('已替代的人') && env.text().includes('已被整体替代'));
  await env.go('#/people/' + id('person', 5));
  await settle(80);
  assert.ok(env.text().includes('已被整体替代（终止版本）'));
  assert.ok(env.text().includes('不会再有新版本'));
});

test('places: 详情显示参考 GPS 的坐标、逆地理编码地址、相对位置示意（无底图、无外部请求）；“未知地点”说明其兜底性质', async () => {
  const env = await ready({}, '#/places/' + id('place', 1));
  const t = env.text();
  assert.ok(t.includes('39.99900, 116.32600') && t.includes('± 12.5 米'));
  assert.ok(t.includes('合成示例路 1 号'), '逆地理编码地址');
  assert.ok(t.includes('没有读到逆地理编码'), '第二个点没有地址时如实说明');
  assert.ok(t.includes('不含底图，页面不会向任何地图服务发请求'));
  const svg = fake.find(env.root(), (n) => n.localName === 'svg' && n.attrs.class === 'geo-svg');
  assert.ok(svg && svg.attrs['aria-label'].includes('2 个点'));
  assert.equal(fake.byTag(svg, 'circle').length, 2);
  assert.ok(env.gets().every((u) => u.startsWith('/v1/') || u.startsWith('/health/')), '所有请求都是同源 API');
  assert.equal(fake.byTag(env.root(), 'img').length, 0);

  await env.go('#/places/' + id('place', 2));
  await settle(80);
  assert.ok(env.text().includes('兜底的“未知地点”'));
});

test('places: 参考 GPS 读不到（无授权/已删除）时给出计数提示，不画不存在的点', async () => {
  const world = mock.createWorld();
  world.records = world.records.filter((r) => r.id !== id('gps', 2));
  const env = await ready({ world }, '#/places/' + id('place', 1));
  assert.ok(env.text().includes('1 个参考 GPS 点读取失败'));
  const svg = fake.find(env.root(), (n) => n.localName === 'svg' && n.attrs.class === 'geo-svg');
  assert.equal(fake.byTag(svg, 'circle').length, 1);
});

// ------------------------------------------------------------------ 活动 / 产物 / 规则 / 动作建议

test('activities: 详情显示安全渲染的标题与描述、证据解析、关联产物/动作建议/规则选择，并可跳转', async () => {
  const env = await ready({}, '#/activities/' + A1);
  const t = env.text();
  assert.ok(t.includes('预算会议'));
  assert.equal(fake.byTag(env.root(), 'strong').some((n) => n.textContent === '预算会议'), true, '**加粗** 被渲染为 <strong> 而不是原样显示');
  assert.ok(t.includes('议题一') && fake.byTag(env.root(), 'li').length >= 2);
  assert.ok(t.includes('证据') && t.includes('解析证据（4）'));
  assert.ok(t.includes('该活动的产物与动作建议'));
  assert.ok(t.includes('会议纪要'), '产物');
  assert.ok(t.includes('准备预算表'), '动作建议');
  assert.ok(t.includes('选中的规则'));
  const hrefs = listLinks(env);
  assert.ok(hrefs.includes('#/artifacts/' + id('artifact', 1)));
  assert.ok(hrefs.includes('#/actions/' + id('suggestion', 1)));
  assert.ok(hrefs.includes('#/rules/' + id('rule', 1)));
  await clickText(env, '解析证据（4）');
  assert.ok(env.text().includes('已解析 4 个'));
});

test('activities: 没有适用规则/产物的活动明确显示“没有读到产物”，并说明不代表出错', async () => {
  const env = await ready({}, '#/activities/' + id('activity', 2));
  assert.ok(env.text().includes('没有读到属于这个活动的产物'));
  assert.ok(env.text().includes('没有读到这个活动的规则选择记录'));
  assert.ok(env.text().includes('这个活动没有登记证据 ID'));
});

test('artifacts: Markdown 产物安全渲染（图片屏蔽/链接不可点/脚本显示为文字），并提示被屏蔽数量', async () => {
  const env = await ready({}, '#/artifacts/' + id('artifact', 1) + '/v/1');
  const md = fake.byClass(env.root(), 'md')[0];
  assert.ok(md, '正文被渲染');
  assert.equal(fake.byTag(md, 'img').length + fake.byTag(md, 'a').length + fake.byTag(md, 'script').length, 0);
  assert.ok(md.textContent.includes('[图片已屏蔽，未加载：像素]'));
  assert.ok(md.textContent.includes('链接未启用：javascript:alert(1)'));
  assert.ok(md.textContent.includes('<script>alert(1)</script>'));
  assert.ok(env.text().includes('已屏蔽 1 张图片、1 个链接'));
  assert.equal(fake.byTag(md, 'h3').length, 1, '# 标题 降级为 h3，不抢占页面的 h2');
  assert.equal(fake.byClass(md, 'md-checkbox').length, 1);
  assert.ok(env.text().includes('历史版本'));
  assert.ok(env.text().includes('生成时的规则版本') && env.text().includes('v1'));
  assert.ok(env.text().includes('原始 JSON（已脱敏'));
});

test('artifacts: 版本历史显示 v1→v2 的 body_md 差异与规则版本变化；当前版本是 v2；历史版本不伪装成当前', async () => {
  const A = id('artifact', 1);
  const env = await ready({}, '#/artifacts/' + A);
  assert.ok(env.text().includes('会议纪要（修订）'));
  assert.ok(env.text().includes('当前版本'));
  await clickText(env, '加载版本历史');
  const diff = fake.byClass(env.root(), 'diff-row')[0];
  assert.ok(diff.textContent.includes('body_md') && diff.textContent.includes('rule_version') && diff.textContent.includes('run_id'));
  assert.ok(env.text().includes('该产物的动作建议') || env.text().includes('这个产物的动作建议'));
  assert.ok(env.text().includes('基于产物 v1（当前查看的是 v2）'), '建议基于旧版产物时提示');
});

test('rules: 规则列表与详情，两个版本的指令差异可对比；停用/仅限单个活动如实显示', async () => {
  const R = id('rule', 1);
  const env = await ready({}, '#/rules/' + R);
  assert.ok(env.text().includes('提取待办事项和结论，并给出提醒'));
  assert.ok(env.text().includes('会议类活动') && env.text().includes('启用中'));
  assert.ok(env.text().includes('编辑本身不会主动重算历史'));
  await clickText(env, '加载版本历史');
  fake.buttons(env.root(), '对比')[0].dispatch('click');
  assert.ok(fake.byClass(env.root(), 'diff-row')[0].textContent.includes('instruction'));
});

test('actions: 动作建议只读展示；“查询执行状态”只发 GET，并明确说明外部执行未启用/不推进、不显示敏感字段', async () => {
  const S = id('suggestion', 1);
  const env = await ready({}, '#/actions/' + S);
  assert.ok(env.text().includes('待审核'));
  assert.ok(env.text().includes('提醒事项') && env.text().includes('准备预算表'));
  assert.ok(env.text().includes('本工作台不提供审核和执行'));
  assert.ok(env.gets().every((u) => !u.includes('/execution')), '不点就不查询');
  await clickText(env, '查询执行状态');
  assert.ok(env.text().includes('外部执行未启用'));
  assert.ok(env.text().includes('这是只读查询：不查证、不写反馈、不推进执行'));
  assert.ok(env.gets().includes('/v1/actions/' + S + '/execution'));
  assert.deepEqual(env.f.calls.filter((c) => c.method !== 'GET'), []);
  assert.ok(!fake.buttons(env.root()).some((b) => /审核|批准|执行动作|重试|确认已执行/.test(b.textContent)));
});

test('actions: 结果未知(unknown)状态高亮为需人工核对，不显示回执/动作键', async () => {
  const world = mock.createWorld();
  const s = world.records.find((r) => r.data_type === 'action_suggestion');
  s.payload.state = 'unknown';
  s.payload.receipt = 'SECRET-RECEIPT';
  s.payload.execution = { attempt: 1, approved_version: 1, action_key: 'ear_action_' + 'a'.repeat(64), action_hash: 'b'.repeat(64) };
  const env = await ready({ world }, '#/actions/' + id('suggestion', 1));
  assert.ok(env.text().includes('结果未知（需人工核对）'));
  assert.ok(fake.byClass(env.root(), 'tone-warn').length >= 1);
  assert.ok(env.text().includes('已记录（内容已隐藏）'));
  const all = env.text() + JSON.stringify(env.doc.attrLog);
  assert.ok(!all.includes('SECRET-RECEIPT') && !all.includes('ear_action_aaaa') && !all.includes('b'.repeat(64)));
  const json = fake.byClass(env.root(), 'json-pre')[0].textContent;
  assert.ok(!json.includes('SECRET-RECEIPT') && !json.includes('b'.repeat(64)), '原始 JSON 也已脱敏');
});

// ------------------------------------------------------------------ 追溯

test('trace: 输入 ID 自动识别类型（按固定顺序探测）；显示写入方/根反馈/批次；根反馈可查询处理状态（只读）', async () => {
  const P = id('person', 1);
  const env = await ready({}, '#/trace');
  assert.ok(env.text().includes('用户反馈记录') && env.text().includes('反馈：attribution / rename'));
  const idInput = fake.find(env.root(), (n) => n.localName === 'input' && n.attrs.placeholder && n.attrs.placeholder.includes('UUIDv7'));
  idInput.value = 'not-an-id';
  const form = fake.find(env.root(), (n) => n.localName === 'form' && fake.byTag(n, 'select').length === 1 && n.attrs.class === 'row');
  form.dispatch('submit');
  assert.ok(env.text().includes('ID 必须是小写 UUIDv7'));
  idInput.value = P;
  form.dispatch('submit');
  await settle(120);
  assert.equal(env.win.location.hash, '#/trace/' + P);
  const t = env.text();
  assert.ok(t.includes('追溯字段') && t.includes('写入方模块') && t.includes('attribution'));
  assert.ok(t.includes('触发的根反馈'));
  assert.ok(t.includes('依赖登记目前没有只读接口'), '如实说明没有依赖图');
  const probes = env.gets().filter((u) => /^\/v1\/records\/[a-z_]+\/0199f0a0-0006/.test(u));
  assert.ok(probes.length >= 3 && probes.length <= 17, '逐个类型探测直到命中：' + probes.length);
  assert.ok(env.gets().indexOf('/v1/records/person/' + P) >= 0);
  assert.ok(!fake.byClass(env.root(), 'detail-pane')[0].hidden);
  await clickText(env, '查询根反馈的处理状态');
  assert.ok(env.text().includes('“已受理”不等于“已完成”'));
  assert.ok(env.gets().includes('/v1/feedback/' + id('feedback', 1) + '/status'));
});

test('trace: 指定类型直接读取；关联对象预览逐个读取并显示类型；未知 ID → 404 说明；无授权类型被计数', async () => {
  const A = id('artifact', 1);
  const env = await ready({}, '#/trace/artifact/' + A);
  assert.equal(env.gets().filter((u) => /^\/v1\/records\/[a-z_]+\/0199f0a0-000c/.test(u) && !u.includes('/artifact/')).length, 0, '指定类型时不再逐个探测');
  await clickText(env, '预览关联对象');
  assert.ok(env.text().includes('所属活动') && env.text().includes('预算会议'));
  assert.ok(env.text().includes('生成所用规则'));

  const missing = await ready({}, '#/trace/' + id('person', 77));
  assert.ok(missing.text().includes('没有找到'));
  // 真实 API 对无权读取的 id 一律返回 404（①隐藏），所以页面不能、也不该声称区分“不存在”与“无授权”，而是如实说明两种可能。
  const hidden = await ready({ api: { denied: ['person', 'place'] } }, '#/trace/' + id('person', 1));
  assert.ok(hidden.text().includes('记录不存在，或你没有读取它的授权'));
  assert.ok(!hidden.text().includes('种类型返回无读取授权'), '404 隐藏语义下没有可计数的 403');
  // 如果服务端对 id 读取返回 403，则给出“有 N 种类型无授权”的线索。
  const inner = mock.createHandler(mock.createWorld(), {});
  const h403 = (c) => (/^\/v1\/records\/(person|place)\/0199f0a0/.test(c.url) ? { status: 403, body: { code: 'forbidden' } } : inner(c));
  const denied = await ready({ handler: h403 }, '#/trace/' + id('person', 1));
  assert.ok(denied.text().includes('有 1 种类型返回无读取授权') === false);
  assert.ok(denied.text().includes('种类型返回无读取授权'), '403 时给出线索');
});

test('trace: 反馈列表带时间范围与分页；根反馈为 system 的记录显示“系统发起”', async () => {
  const env = await ready({}, '#/trace');
  assert.ok(env.gets().some((u) => u.startsWith('/v1/records/feedback?limit=50')));
  await env.go('#/trace/person/' + id('person', 1));
  await settle(80);
  assert.ok(env.text().includes('system（系统发起，不是用户反馈）') || env.text().includes('触发的根反馈'));
  const v2 = fake.byClass(env.root(), 'facts').map((n) => n.textContent).join('|');
  assert.ok(v2.includes('0199f0a0-0012-7aaa-9bbb') || v2.includes('触发的根反馈'));
});

// ------------------------------------------------------------------ 竞态与韧性

test('race: 快速切换路由时，迟到的旧页面响应不会渲染到新页面', async () => {
  const world = mock.createWorld();
  const inner = mock.createHandler(world, {});
  let release;
  const gate = new Promise((r) => { release = r; });
  const env = bootWorkspace({ world, handler: async (c) => { if (c.url.startsWith('/v1/records/rule?') && !c.url.includes('limit=1')) await gate; return inner(c); } });
  await env.connect();
  await env.go('#/rules');
  await env.go('#/places');
  release();
  await settle(80);
  assert.ok(env.text().includes('合成会议室'));
  assert.ok(!env.text().includes('提取待办事项'), '规则页的迟到响应被丢弃');
  assert.equal(env.page.state().route.view, 'places');
});

test('resilience: 畸形记录被忽略并计数；整页响应畸形显示解析错误；超大列表受加载上限约束', async () => {
  const world = mock.createWorld();
  const handler = (c) => {
    if (/^\/v1\/records\/person\?limit=50$/.test(c.url)) return { body: { items: [world.records.find((r) => r.data_type === 'person'), { id: 'x' }, 7], next_cursor: null } };
    if (/^\/v1\/records\/place\?limit=50$/.test(c.url)) return { body: { nope: true } };
    return mock.createHandler(world, {})(c);
  };
  const env = await ready({ handler }, '#/people');
  assert.ok(env.text().includes('另有 2 条格式异常已忽略'));
  await env.go('#/places');
  await settle(80);
  assert.ok(env.text().includes('响应不是预期的 JSON'));
});

test('resilience: 加载上限——持续有下一页时最多加载 500 条并明示“请缩小范围”，不会无限翻页', async () => {
  const handler = (c) => {
    const u = new URL(c.url, 'http://x');
    if (u.pathname === '/v1/records/rule') {
      const off = u.searchParams.get('cursor') ? parseInt(u.searchParams.get('cursor'), 10) : 0;
      const items = Array.from({ length: 50 }, (_, i) => sem('rule', off + i + 1, 1, { scope: 's' + (off + i), instruction: 'i', enabled: true, activity_id: null }, { id: id('rule', off + i + 1) }));
      return { body: { items, next_cursor: String(off + 50) } };
    }
    return mock.createHandler(mock.createWorld(), {})(c);
  };
  const env = await ready({ handler }, '#/rules');
  for (let i = 0; i < 12; i++) {
    const more = fake.buttons(env.root(), '加载更多').find((b) => !b.hidden);
    if (!more) break;
    more.dispatch('click');
    await settle(60);
  }
  assert.ok(env.text().includes('已加载 500 条'));
  assert.ok(env.text().includes('已达到页面加载上限或游标未前进，请缩小范围'));
  assert.ok(fake.buttons(env.root(), '加载更多').every((b) => b.hidden));
});

test('verbatim: 转写与规则指令是纯文本，列表摘要与详情必须逐字显示（不得删除 > # * _ 等字符）；Markdown 字段才做 Markdown 渲染', async () => {
  const world = mock.createWorld();
  const seg = world.records.find((r) => r.id === id('transcript', 1));
  seg.payload.text = '成绩 5 > 3，C# 与 snake_case 以及 **不是加粗** 和 # 不是标题';
  const rule = world.records.find((r) => r.data_type === 'rule' && r.version === 2);
  rule.payload.instruction = '当 a > b 时提取 #待办 与 *重点*';
  const env = await ready({ world }, '#/timeline');
  await clickText(env, '转写段');
  assert.ok(env.text().includes('成绩 5 > 3，C# 与 snake_case 以及 **不是加粗** 和 # 不是标题'), '转写逐字显示');
  const rawList = fake.byClass(env.root(), 'raw-list')[0];
  assert.ok(rawList.textContent.includes('**不是加粗**'));
  assert.equal(fake.byTag(rawList, 'strong').length, 0, '转写不会被当作 Markdown（页面上其他位置的活动标题才是 Markdown，会有 <strong>）');
  assert.equal(fake.byTag(env.root(), 'strong').some((n) => n.textContent === '预算会议'), true, '对照：Markdown 字段确实被渲染');
  await env.go('#/rules');
  await settle(60);
  assert.ok(env.text().includes('当 a > b 时提取 #待办 与 *重点*'));
  const c = core.describeRecord(core.normalizeRecord(seg));
  assert.equal(c.headline, seg.payload.text, '摘要在 60 字内时与原文完全一致');
  assert.equal(core.clip('a  b\n c', 10), 'a b c');
  assert.equal(core.clip('x'.repeat(100), 10), 'x'.repeat(10) + '…');
  assert.equal(core.clip(null, 5), '');
});

}

// ===== New recovery/security regression cases for this delivery =====
{
const test = require('node:test');
const assert = require('node:assert/strict');
const {core, pages, client} = bundle;
const {bootWorkspace, settle, makeFetch, makeTimers, TOKEN, fake, mock} = flatEnv;
const {id, sem} = mock;
const clientWith = (f, extra = {}) => {
  const timers = makeTimers();
  const c = client.createClient({fetch:f, AbortController, setTimeout:timers.setTimeout, clearTimeout:timers.clearTimeout, ...extra});
  c.setToken(TOKEN);
  return {c, timers};
};

test('flat: standalone assets, CSP-safe DOM, no runtime dependency on prototype or external assets', () => {
  const html = fs.readFileSync(path.join(STATIC,'workspace.html'),'utf8');
  const js = fs.readFileSync(path.join(STATIC,'workspace.js'),'utf8');
  const css = fs.readFileSync(path.join(STATIC,'workspace.css'),'utf8');
  const code = js.replace(/\/\*[\s\S]*?\*\//g,'').split('\n').filter(l => !/^\s*\/\//.test(l)).join('\n');
  assert.deepEqual([...html.matchAll(/<script[^>]*src="([^"]+)"/g)].map(m=>m[1]), ['workspace.js']);
  assert.match(html, /href="workspace\.css"/);
  assert.match(html, /lang="zh-CN"/);
  assert.match(html, /<noscript>/);
  assert.doesNotMatch(html, /<style|\sstyle=|\son\w+=|https?:\/\//i);
  assert.doesNotMatch(code, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|localStorage|sessionStorage|indexedDB|document\.cookie|sendBeacon|XMLHttpRequest|new Function|\beval\s*\(|console\./);
  assert.doesNotMatch(code, /method:\s*['"](?:POST|PUT|PATCH|DELETE)/i);
  assert.doesNotMatch(css.replace(/\/\*[\s\S]*?\*\//g,''), /@import|url\(|@font-face/);
  assert.match(css, /:focus-visible/);
  assert.match(css, /max-width/);
  assert.match(css, /\[hidden\]/);
  assert.match(css, /prefers-reduced-motion/);
  assert.equal(js.includes('合成人物甲'),false, 'fixtures must never be bundled into runtime');
});

test('flat: every emitted endpoint is in app.py, except explicitly handed-off evidence search', () => {
  const api = fs.readFileSync(path.join(STATIC,'../app.py'),'utf8');
  for (const endpoint of ['/v1/me','/v1/records/{data_type}','/v1/records/{data_type}/{record_id}',
    '/v1/records/{data_type}/{record_id}/history','/v1/previews/person/{person_id}',
    '/v1/actions/{suggestion_id}/execution','/v1/feedback/{feedback_id}/status']) assert.ok(api.includes(endpoint), endpoint);
  assert.equal(core.EVIDENCE_PATH,'/v1/evidence/search');
  const evidence = fs.readFileSync(path.join(STATIC,'../evidence.py'),'utf8');
  for (const name of ['next_cursor','truncated','scanned','report','live_best_effort','transcript_segment_id']) assert.ok(evidence.includes(name));
});

test('flat: readonly endpoint allowlist blocks write/management and all off-origin paths before fetch', async () => {
  const f=makeFetch(()=>({body:{}})); const {c}=clientWith(f);
  for(const path of ['/v1/feedback','/v1/management/requests','/v1/plugins','/v1/previews/reextract',
    '/v1/actions/'+id('suggestion',1)+'/prepare','/v1/actions/'+id('suggestion',1)+'/pump',
    '/v1/actions/'+id('suggestion',1)+'/recovery','https://bad.invalid/v1/me','//bad.invalid/v1/me',
    '/v1/records/../me','/v1/records/%2e%2e/me','/v1/records/secret_type','/v1/me#x']) {
    assert.equal((await c.get(path)).error.kind,'bad_path',path);
  }
  assert.equal(f.calls.length,0);
});

test('flat: timeout completes even if transport ignores AbortSignal; releases concurrency slot', async () => {
  let first=true;
  const {c,timers}=clientWith(()=>{if(first){first=false; return new Promise(()=>{});} return Promise.resolve({ok:true,status:200,text:async()=>'{}'});},{maxConcurrent:1});
  const slow=c.get('/v1/me'); await settle(2); timers.runDue();
  assert.equal((await slow).error.kind,'timeout');
  assert.equal(c.inflight(),0);
  assert.equal((await c.get('/v1/me')).ok,true);
});

test('flat: streamed UTF-8 body is decoded and oversized stream is cancelled before full buffering', async () => {
  const encoder = new TextEncoder(); let cancelled=false, calls=0;
  const body = new ReadableStream({pull(controller){calls++; controller.enqueue(new Uint8Array(1024*1024));},cancel(){cancelled=true;}});
  const {c}=clientWith(async()=>({ok:true,status:200,body}));
  assert.equal((await c.get('/v1/me')).error.kind,'too_large');
  assert.equal(cancelled,true); assert.ok(calls <= 7);
  const bytes=encoder.encode('{"subject":"中文","role":"user"}');
  const {c:small}=clientWith(async()=>({ok:true,status:200,body:new ReadableStream({start(ctrl){for(const b of bytes)ctrl.enqueue(Uint8Array.of(b));ctrl.close();}})}));
  assert.equal((await small.get('/v1/me')).data.subject,'中文');
});

test('flat: 401 clears auth even when body is not JSON or unreadable', async () => {
  let auth=0;
  const {c}=clientWith(async()=>({ok:false,status:401,text:async()=>{throw Error('bad');}}),{onAuthFailure:()=>auth++});
  assert.equal((await c.get('/v1/me')).error.authFailure,true);assert.equal(auth,1);
});

test('flat: pagehide clears credentials/data and late responses cannot restore session', async () => {
  const env=bootWorkspace();await env.connect();await env.go('#/people/'+id('person',1));
  assert.ok(env.text().includes('合成人物甲'));
  env.win.fire('pagehide');await settle();
  assert.equal(env.page.state().connected,false);
  assert.equal(env.n('token-input').value,'');assert.ok(!env.text().includes('合成人物甲'));
  assert.equal((await env.page.client.get('/v1/me')).notConnected,true);
});

test('flat: record reads reauthorize even for a previously loaded immutable version', async () => {
  let count=0;
  const {c}=clientWith(makeFetch(()=>{count++;return count===1?{body:sem('person',1,1,{name:'合成'})}:{status:404,body:{code:'not_found'}};}));
  const data=pages.createData(c);
  assert.equal((await data.record('person',id('person',1),1)).ok,true);
  assert.equal((await data.record('person',id('person',1),1)).ok,false);
  assert.equal(count,2);
});

test('flat: raw normalization never silently supplies a missing or corrupt version',()=>{
  const r=mock.raw('gps_point','gps',1,'2026-10-29T09:00:00Z','2026-10-29T09:00:00Z',{});
  delete r.version; assert.equal(core.normalizeRecord(r),null);
  r.version=2; assert.equal(core.normalizeRecord(r),null);
  r.version=1; assert.equal(core.normalizeRecord(r).version,1);
});

test('flat: impossible local hours and DST spring gaps are rejected instead of shifted',()=>{
  const utc=core.makeZone('utc');
  assert.equal(core.localToUtcIso('2026-11-18T25:10',utc),null);
  const ny=core.makeZone('local',ms=>ms<Date.parse('2026-03-08T07:00:00Z')?-300:-240);
  assert.equal(core.localToUtcIso('2026-03-08T02:30',ny),null);
  assert.equal(core.localToUtcIso('2026-03-08T03:30',ny),'2026-03-08T07:30:00Z');
});

test('flat: malformed pages and stalled cursors fail explicitly, never become false empty success',async()=>{
  for(const body of [{items:[],next_cursor:5},{items:[],next_cursor:'same'},{items:[sem('place',1,1,{})],next_cursor:null}]){
    const {c}=clientWith(makeFetch(()=>({body})));const data=pages.createData(c);
    assert.equal((await data.list('person',{cursor:'same'})).ok,false);
  }
  assert.equal(core.classifyProbe({ok:true},core.readPage({items:[],next_cursor:'more'})).state,'error');
});

test('flat: history rejects wrong object, duplicate versions and stalled numeric cursor',async()=>{
  for(const body of [
    {items:[sem('person',2,1,{})],next_after_version:null},
    {items:[sem('person',1,1,{}),sem('person',1,1,{})],next_after_version:null},
    {items:[],next_after_version:1}]) {
    const {c}=clientWith(makeFetch(()=>({body}))); const data=pages.createData(c);
    assert.equal((await data.history('person',id('person',1),0)).ok,false);
  }
});

test('flat: embeddings of any length, credentials and file paths redacted; prototype pollution inert',()=>{
  const data=JSON.parse('{"__proto__":{"polluted":true},"embedding":{"values":[0.123]},"external_id":"private-external","api_key":"fake-secret","file_ref":{"path":"fake/private"}}');
  const text=core.stableJson(core.redact(data));
  assert.ok(!text.includes('0.123'));assert.ok(!text.includes('fake-secret'));assert.ok(!text.includes('private-external'));assert.ok(!text.includes('fake/private'));
  assert.equal({}.polluted,undefined);assert.ok(text.includes('__proto__'));
});

test('flat: overview typed links browse real raw type and can open its details',async()=>{
  const env=bootWorkspace();await env.connect();await env.go('#/trace/gps_point');
  assert.ok(env.gets().some(p=>p.startsWith('/v1/records/gps_point?')));
  assert.ok(env.text().includes('39.99900, 116.32600'));
  assert.equal(fake.buttons(env.root(),'查询处理状态').length,0);
  await env.go('#/trace/voiceprint');
  assert.ok(fake.links(env.root()).some(l=>l.attrs.href.includes(id('voiceprint',1))));
});

test('flat: unknown execution-enabled flag stays unknown; no execute buttons are rendered',async()=>{
  const handler=mock.createHandler(mock.createWorld());
  const env=bootWorkspace({handler:c=>c.url.endsWith('/execution')?{body:{suggestion_state:'unknown'}}:handler(c)});
  await env.connect();await env.go('#/actions/'+id('suggestion',1));
  fake.buttons(env.root(),'查询执行状态')[0].dispatch('click');await settle();
  assert.ok(env.text().includes('接口未报告执行开关，状态未知'));
  assert.ok(!env.text().includes('这个部署不会执行'));
  assert.ok(!fake.buttons(env.root()).some(b=>/^(执行|审核|批准|重试执行|确认已执行)$/.test(b.textContent)));
  assert.equal(env.f.calls.every(c=>c.method==='GET'),true);
});

test('flat: pending raw tab requests cannot overwrite the latest A → B → A selection',async()=>{
  const base=mock.createHandler(mock.createWorld());let slow=false, release, delayed=false;
  const gate=new Promise(r=>release=r);
  const env=bootWorkspace({handler:async c=>{
    const r=base(c);
    if(slow&&!delayed&&c.url.startsWith('/v1/records/transcript_segment?')){delayed=true;await gate;
      return {body:{items:[mock.raw('transcript_segment','transcript',99,'2026-10-29T09:00:00Z','2026-10-29T09:00:01Z',{text:'STALE-RAW-RESPONSE'})],next_cursor:null}};
    }return r;
  }});
  await env.connect();await env.go('#/timeline');slow=true;
  fake.buttons(env.root(),'转写段')[0].dispatch('click');await settle(3);
  fake.buttons(env.root(),'GPS 点')[0].dispatch('click');
  fake.buttons(env.root(),'转写段')[0].dispatch('click');await settle();release();await settle();
  assert.ok(!env.text().includes('STALE-RAW-RESPONSE'));
  assert.ok(env.text().includes('我们下周三开会讨论预算'));
});

test('flat: repeated opaque pagination cycles stop without claiming exhaustive results',async()=>{
  const base=mock.createHandler(mock.createWorld());let hits=0;
  const env=bootWorkspace({handler:c=>{
    if(c.url.startsWith('/v1/records/rule?')&&!c.url.includes('limit=1&')&&!c.url.endsWith('limit=1')){
      hits++;return {body:{items:[sem('rule',hits,1,{instruction:'规则'+hits})],next_cursor:hits%2?'cursor-a':'cursor-b'}};
    }return base(c);
  }});
  await env.connect();await env.go('#/rules');
  for(let i=0;i<3;i++){const b=fake.buttons(env.root(),'加载更多').find(b=>!b.hidden);if(b){b.dispatch('click');await settle();}}
  assert.equal(hits,3);assert.ok(env.text().includes('游标未前进'));
  assert.ok(fake.buttons(env.root(),'加载更多').every(b=>b.hidden));
});
}

{
const test = require('node:test');
const assert = require('node:assert/strict');
test('flat: disconnect releases all slots immediately even when transport ignores abort', async () => {
  const timers=flatEnv.makeTimers();let stall=true;
  const c=bundle.client.createClient({fetch:()=>stall?new Promise(()=>{}):Promise.resolve({ok:true,status:200,text:async()=>'{}'}),AbortController,setTimeout:timers.setTimeout,clearTimeout:timers.clearTimeout,maxConcurrent:1});
  c.setToken(flatEnv.TOKEN);const request=c.get('/v1/me');await flatEnv.settle(2);c.clear();
  assert.equal((await request).stale,true);assert.equal(c.inflight(),0);assert.equal(timers.pending(),0);
  stall=false;c.setToken(flatEnv.TOKEN);assert.equal((await c.get('/v1/me')).ok,true);
});

test('flat: older search response never overrides a later search in the same window',async()=>{
  const {bootWorkspace,settle,mock,fake}=flatEnv;
  const base=mock.createHandler(mock.createWorld());let release;
  const gate=new Promise(r=>release=r);
  const env=bootWorkspace({handler:async c=>{
    if(c.url.startsWith('/v1/evidence/search')){
      const old=new URL(c.url,'http://fixture.invalid').searchParams.get('q')==='old';
      if(old)await gate;
      return {body:{items:[{transcript_segment_id:mock.id('transcript',old?99:98),transcript_segment_version:1,start:'2026-10-29T09:00:00Z',end:'2026-10-29T09:00:01Z',text:old?'STALE-SEARCH':'LATEST-SEARCH',attribution:null,playback:null}],next_cursor:null,truncated:false,scanned:1,consistency:'live_best_effort',report:{stop_reason:'exhausted'}}};
    }return base(c);
  }});
  await env.connect();await env.go('#/timeline');
  const form=fake.byClass(env.root(),'search-form')[0], input=fake.byTag(form,'input')[0];
  input.value='old';form.dispatch('submit');await settle(3);
  input.value='new';form.dispatch('submit');await settle();release();await settle();
  assert.ok(env.text().includes('LATEST-SEARCH'));assert.ok(!env.text().includes('STALE-SEARCH'));
});
}

{
const test = require('node:test');
const assert = require('node:assert/strict');
test('flat: pagination binds to applied filters, never combines a cursor with unapplied date edits', async () => {
  const {bootWorkspace,settle,mock,fake}=flatEnv;
  const base=mock.createHandler(mock.createWorld());
  const env=bootWorkspace({handler:c=>{
    if(c.url.startsWith('/v1/records/activity?')&&!c.url.endsWith('limit=1')){
      const q=new URL(c.url,'http://fixture.invalid').searchParams;
      return {body:{items:[mock.sem('activity',q.has('cursor')?2:1,1,{title_md:'合成分页记录'})],next_cursor:q.has('cursor')?null:'opaque-original-filter'}};
    }return base(c);
  }});
  await env.connect();await env.go('#/activities');
  const mode=fake.find(env.root(),n=>n.localName==='select'&&n.id.startsWith('range-mode'));
  mode.value='day';mode.dispatch('change'); // draft only: no Apply
  const date=fake.find(env.root(),n=>n.localName==='input'&&n.id.startsWith('range-date'));
  date.value='2026-11-18';date.dispatch('change');
  fake.buttons(env.root(),'加载更多').find(b=>!b.hidden).dispatch('click');await settle();
  const next=new URL(env.gets().filter(u=>u.startsWith('/v1/records/activity?')&&u.includes('cursor=')).pop(),'http://fixture.invalid');
  assert.equal(next.searchParams.get('cursor'),'opaque-original-filter');
  assert.equal(next.searchParams.has('start'),false,'the applied first page was all-time');
  fake.find(env.root(),n=>n.localName==='form'&&n.attrs['aria-label']==='时间范围').dispatch('submit');await settle();
  const reset=new URL(env.gets().filter(u=>u.startsWith('/v1/records/activity?')).pop(),'http://fixture.invalid');
  assert.equal(reset.searchParams.has('cursor'),false);assert.equal(reset.searchParams.has('start'),true);
});
}

// Independent review 2026-11-18: failed reads and exhausted Markdown allocation.
{
const test = require('node:test');
const assert = require('node:assert/strict');
const { bootWorkspace, settle, mock, fake } = flatEnv;
const pane = env => fake.byClass(env.root(), 'list-pane')[0];
const detail = env => fake.byClass(env.root(), 'detail-pane')[0];
const filter = (env, value) => {
  const input = fake.find(pane(env), n => n.localName === 'input' && n.getAttribute('placeholder') === '在已加载的记录中筛选');
  input.value = value; input.dispatch('input');
};
const isPeopleList = c => new URL(c.url, 'http://fixture.invalid').pathname === '/v1/records/person';
const assertNotEmpty = node => assert.doesNotMatch(node.textContent, /没有读到人物|已到末尾/);

for (const status of [403, 503]) {
  test(`review: FAILED_LIST_TO_EMPTY_${status} retains failure through filter, route and pending retry`, async t => {
    const base = mock.createHandler(mock.createWorld());
    let mode = 'fail', release;
    const gate = new Promise(r => { release = r; });
    const env = bootWorkspace({ handler: async c => {
      if (!isPeopleList(c)) return base(c);
      if (mode === 'fail') return { status, body: { code: status === 403 ? 'forbidden' : 'storage_unavailable' } };
      if (mode === 'pending') await gate;
      return { body: { items: [], next_cursor: null } };
    } });
    t.after(() => { release(); env.page.disconnect(); });
    await env.connect(); await env.go('#/people');
    assert.match(pane(env).textContent, new RegExp('HTTP ' + status));
    const requests = env.gets().length;
    filter(env, 'synthetic'); await settle();
    assert.match(pane(env).textContent, new RegExp('HTTP ' + status));
    assertNotEmpty(pane(env)); assert.equal(env.gets().length, requests);
    const reads = env.f.calls.filter(isPeopleList).length;
    await env.go('#/people/' + mock.id('person', 1));
    assert.match(pane(env).textContent, new RegExp('HTTP ' + status));
    assertNotEmpty(pane(env)); assert.equal(env.f.calls.filter(isPeopleList).length, reads);
    mode = 'pending'; fake.buttons(pane(env), '刷新')[0].dispatch('click'); await settle();
    filter(env, 'pending'); await env.go('#/people');
    assert.match(pane(env).textContent, new RegExp('HTTP ' + status));
    assert.match(pane(env).textContent, /正在读取人物/); assertNotEmpty(pane(env));
    release(); await settle();
    assert.doesNotMatch(pane(env).textContent, /HTTP |正在读取人物/);
    assert.match(pane(env).textContent, /没有读到人物/);
    assert.ok(env.f.calls.every(c => c.method === 'GET'));
  });
}

test('review: initial loading survives filtering and same-view route, pager stays pending', async t => {
  const base = mock.createHandler(mock.createWorld()); let slow = false, release;
  const gate = new Promise(r => { release = r; });
  const env = bootWorkspace({ handler: async c => { if (slow && isPeopleList(c)) await gate; return base(c); } });
  t.after(() => { release(); env.page.disconnect(); });
  await env.connect(); slow = true; await env.go('#/people');
  filter(env, '甲'); await env.go('#/people/' + mock.id('person', 1));
  assert.match(pane(env).textContent, /正在读取人物/); assertNotEmpty(pane(env));
  assert.ok(fake.byClass(pane(env), 'pager')[0].hidden);
  release(); await settle();
  assert.match(pane(env).textContent, /合成人物甲/);
  assert.doesNotMatch(pane(env).textContent, /正在读取人物/);
});

for (const status of [403, 503]) {
  test(`review: pagination ${status} retains explicit failure while filtering previously loaded rows`, async t => {
    const world = mock.createWorld(), base = mock.createHandler(world); let more = false, release;
    const gate = new Promise(r => { release = r; });
    const env = bootWorkspace({ handler: async c => {
      if (!isPeopleList(c) || !more) return base(c);
      if (c.url.includes('cursor=')) { await gate; return { status, body: { code: status === 403 ? 'forbidden' : 'storage_unavailable' } }; }
      return { body: { items: [mock.sem('person', 1, 2, { name: '合成人物甲' })], next_cursor: 'synthetic-next' } };
    } });
    t.after(() => { release(); env.page.disconnect(); });
    await env.connect(); more = true; await env.go('#/people');
    fake.buttons(pane(env), '加载更多')[0].dispatch('click'); await settle();
    filter(env, '甲'); await env.go('#/people/' + mock.id('person', 1));
    const pending = fake.buttons(pane(env), '加载中')[0];
    assert.ok(pending && pending.disabled, 'local renders cannot enable an in-flight pager');
    release(); await settle();
    for (const needle of ['甲', 'no-match', '']) {
      filter(env, needle); await settle();
      assert.match(pane(env).textContent, new RegExp('HTTP ' + status));
      assert.match(pane(env).textContent, /未完成|过时|旧数据/);
      assert.doesNotMatch(pane(env).textContent, /没有读到人物/);
      if (needle === '甲') assert.match(pane(env).textContent, /合成人物甲/);
    }
    await env.go('#/people'); assert.match(pane(env).textContent, new RegExp('HTTP ' + status));
    more = false; fake.buttons(pane(env), '刷新')[0].dispatch('click'); await settle();
    assert.doesNotMatch(pane(env).textContent, /HTTP |未完成|过时|旧数据/);
    assert.match(pane(env).textContent, /合成人物甲/);
  });
}

for (const [label, response] of [
  ['invalid JSON', { text: '{' }], ['null envelope', { text: 'null' }],
  ['invalid items', { body: { items: {}, next_cursor: null } }],
  ['invalid cursor', { body: { items: [], next_cursor: {} } }]
]) {
  test(`review: malformed list ${label} stays an error across filter and same-view navigation`, async t => {
    const base = mock.createHandler(mock.createWorld());
    const env = bootWorkspace({ handler: c => isPeopleList(c) ? response : base(c) });
    t.after(() => env.page.disconnect());
    await env.connect(); await env.go('#/people');
    assert.ok(fake.byClass(pane(env), 'state-error').length);
    filter(env, 'anything'); await env.go('#/people/' + mock.id('person', 1));
    assert.ok(fake.byClass(pane(env), 'state-error').length); assertNotEmpty(pane(env));
  });
}

test('review: refresh with invalid draft range remains an error across local renders', async t => {
  const env = bootWorkspace(); t.after(() => env.page.disconnect());
  await env.connect(); await env.go('#/artifacts');
  const mode = fake.find(pane(env), n => n.localName === 'select' && n.id.startsWith('range-mode'));
  mode.value = 'custom'; mode.dispatch('change');
  fake.buttons(pane(env), '刷新')[0].dispatch('click');
  await settle(); assert.ok(fake.byClass(pane(env), 'state-error').length);
  filter(env, 'bad-range'); await env.go('#/artifacts/' + mock.id('artifact', 1));
  assert.ok(fake.byClass(pane(env), 'state-error').length);
});

function countedMarkdown(text) {
  const doc = fake.makeDom(path.join(STATIC, 'workspace.html')); let allocated = 0;
  for (const name of ['createElement', 'createTextNode']) {
    const original = doc[name]; doc[name] = (...args) => { allocated++; return original(...args); };
  }
  const result = bundle.md.renderMarkdown(doc, text);
  return { ...result, allocated, doc };
}

test('review: MD_NODE_LIMIT_THROW exact 40004-character probe returns a bounded truncation notice', () => {
  const text = '*x* '.repeat(10001); assert.equal(text.length, 40004);
  const result = countedMarkdown(text);
  assert.equal(result.stats.truncated, true);
  assert.equal(fake.byClass(result.node, 'md-truncated').length, 1);
  assert.match(result.node.textContent, /已截断/);
  assert.ok(result.allocated <= bundle.md.LIMITS.maxNodes + 2, 'fixed notice has its own two-node reserve');
});

test('review: Markdown exact node budget is allowed; element and text overflow are both bounded', () => {
  const prefix = '*x* '.repeat(9999); // root + paragraph + 9999 * (em + text + space) = 29999
  const exact = countedMarkdown(prefix + '\n\n---');
  assert.equal(exact.allocated, bundle.md.LIMITS.maxNodes);
  assert.equal(exact.stats.truncated, false);
  for (const text of [prefix + '\n\n---\n\nnext', prefix + '`y`']) {
    const over = countedMarkdown(text);
    assert.equal(over.stats.truncated, true);
    assert.equal(over.allocated, bundle.md.LIMITS.maxNodes + 2);
    assert.equal(fake.byClass(over.node, 'md-truncated').length, 1);
  }
});

test('review: malformed Markdown values and hostile text stay inert and bounded', () => {
  for (const input of [null, undefined, 42, false, {}, [], { toString() { throw Error('must not coerce'); } }, '\u0000\r\n```\n**unclosed', '> '.repeat(50) + '*x*']) {
    const result = countedMarkdown(input);
    assert.ok(result.allocated <= bundle.md.LIMITS.maxNodes + 2);
    if (typeof input !== 'string') assert.equal(result.node.textContent, '');
  }
  const hostile = countedMarkdown('<script>synthetic()</script> <img src="https://invalid.example/p" onerror="synthetic()">\n\n![pixel](https://invalid.example/p) [click](javascript:synthetic())');
  assert.equal(hostile.stats.images, 1); assert.equal(hostile.stats.links, 2);
  assert.ok(!hostile.doc.attrLog.some(a => /^(src|href|style|on)/i.test(a.name)));
});

test('review: unrelated Markdown RangeError is not silently swallowed as a successful render', () => {
  const doc = fake.makeDom(path.join(STATIC, 'workspace.html'));
  const create = doc.createElement;
  doc.createElement = tag => { if (tag === 'em') throw new RangeError('synthetic-render-failure'); return create(tag); };
  assert.throws(() => bundle.md.renderMarkdown(doc, '*x*'), /synthetic-render-failure/);
});

test('review: MD_NODE_LIMIT_DETAIL renders history and trace without unhandled rejection or stuck loading', async t => {
  const world = mock.createWorld();
  world.records.filter(r => r.data_type === 'artifact').forEach(r => { r.payload.body_md = '*x* '.repeat(10001); });
  const env = bootWorkspace({ world }); t.after(() => env.page.disconnect());
  // node:test also fails on any unhandled asynchronous rejection from the page.
  await env.connect(); await env.go('#/artifacts/' + mock.id('artifact', 1));
  assert.match(detail(env).textContent, /已截断/);
  assert.match(detail(env).textContent, /版本历史/); assert.match(detail(env).textContent, /追溯/);
  assert.notEqual(detail(env).textContent, '正在读取…');
  assert.equal(detail(env).getAttribute('aria-busy'), 'false');
  await env.go('#/artifacts/' + mock.id('artifact', 1) + '/v/1');
  assert.match(detail(env).textContent, /已截断/);
  await env.go('#/people'); assert.match(pane(env).textContent, /合成人物甲/);
});

for (const ErrorType of [Error, RangeError]) {
  test(`review: detail boundary catches unexpected ${ErrorType.name}, sanitizes and permits retry/navigation`, async t => {
    const env = bootWorkspace(); t.after(() => env.page.disconnect());
    await env.connect();
    const create = env.doc.createElement; let broken = true;
    env.doc.createElement = tag => { if (broken && tag === 'em') throw new ErrorType('SYNTHETIC-PRIVATE-EXCEPTION'); return create(tag); };
    env.world.records.filter(r => r.data_type === 'artifact').forEach(r => { r.payload.body_md = '*x*'; });
    await env.go('#/artifacts/' + mock.id('artifact', 1));
    assert.ok(fake.byClass(detail(env), 'state-error').length);
    assert.doesNotMatch(detail(env).textContent, /SYNTHETIC-PRIVATE-EXCEPTION|正在读取/);
    assert.equal(detail(env).getAttribute('aria-busy'), 'false');
    broken = false; fake.buttons(detail(env), '重试')[0].dispatch('click'); await settle();
    assert.match(detail(env).textContent, /版本历史/);
    await env.go('#/people'); assert.match(pane(env).textContent, /合成人物甲/);
  });
}
}

{
const test = require('node:test');
const assert = require('node:assert/strict');
test('test mode: auto-connects fixed user without bearer or login UI; failed internal identity stays unavailable', async () => {
  const env = flatEnv.bootWorkspace({ handler(c) {
    if (c.url === '/v1/session') return {body:{test_no_auth:true}};
    if (c.url === '/v1/me') return {body:{subject:'local-test',role:'user'}};
    if (c.url === '/health/ready') return {body:{status:'ready'}};
    return {body:{items:[],next_cursor:null,consistency:'live'}};
  }});
  await env.page.discovery; await flatEnv.settle();
  assert.equal(env.page.state().connected,true);
  assert.equal(env.page.state().session.role,'user');
  assert.equal(env.n('connect-form').hidden,true);
  assert.equal(env.n('auth-note').hidden,true);
  assert.equal(env.n('token-input').value,'');
  assert.ok(!env.text().includes('请先连接'));
  assert.ok(env.f.calls.length > 2);
  assert.ok(env.f.calls.every(c=>c.method==='GET' && !c.headers.Authorization && c.init.mode==='same-origin'));
  env.page.disconnect();
  assert.ok(!env.text().includes('API 令牌'));
  assert.equal(env.n('connect-form').hidden,true);
  const revoked=flatEnv.bootWorkspace({handler:c=>c.url==='/v1/session'?{body:{test_no_auth:true}}:{status:401}});
  await revoked.page.discovery; await flatEnv.settle();
  assert.equal(revoked.page.state().connected,false);
  assert.equal(revoked.n('connect-form').hidden,true);
  assert.ok(!revoked.text().includes('粘贴'));
});
test('test mode discovery is strict: truthy strings do not bypass default token requirement', async () => {
  const env=flatEnv.bootWorkspace({handler:()=>({body:{test_no_auth:'true'}})});
  await env.page.discovery;
  assert.equal(env.page.state().connected,false);
  assert.deepEqual(env.gets(),['/v1/session']);
  assert.equal(env.n('connect-form').hidden,false);
});
}
