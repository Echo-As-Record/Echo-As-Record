'use strict';
// 页面测试：网络层、有界轮询、请求 ID 复用、一次性领取、假 DOM 下的真实页面流程、静态卫生检查。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const STATIC = path.join(__dirname, '..', '..', 'src', 'ear_api', 'static');
const core = require(path.join(STATIC, 'core.js'));
const app = require(path.join(STATIC, 'app.js'));
const read = function (f) { return fs.readFileSync(path.join(STATIC, f), 'utf8'); };

const TOKEN = 'A'.repeat(43);
const CLAIM_TOKEN = 'C'.repeat(43);
const NOW = 1700000000000;
function idn(n) {
  return core.newRequestId(function (a) { a.fill(n); }, NOW + n);
}

// ------------------------------------------------------------ 测试替身

function makeTimers() {
  let seq = 0;
  const q = new Map();
  return {
    setTimeout: function (fn, ms) { const id = ++seq; q.set(id, { fn: fn, ms: ms }); return id; },
    clearTimeout: function (id) { q.delete(id); },
    pending: function () { return q.size; },
    // 只触发延迟不超过 limit 的计时器（轮询间隔 ≤5s；请求超时 15s、凭据清除 300s 不在其内）
    runDue: function (limit) {
      const lim = limit === undefined ? 10000 : limit;
      const due = Array.from(q.entries()).filter(function (e) { return e[1].ms <= lim; });
      due.forEach(function (e) { q.delete(e[0]); });
      due.forEach(function (e) { e[1].fn(); });
      return due.length;
    }
  };
}
const tick = function () { return new Promise(function (r) { setImmediate(r); }); };
async function settle(n) { for (let i = 0; i < (n || 12); i++) await tick(); }
async function pump(timers, n) { for (let i = 0; i < (n || 30); i++) { await tick(); timers.runDue(); } await settle(4); }

function makeFetch(handler) {
  const calls = [];
  async function fetchFn(url, init) {
    const call = { url: url, method: init.method, headers: init.headers, init: init, body: init.body === undefined ? undefined : JSON.parse(init.body) };
    calls.push(call);
    const r = (await handler(call)) || {};
    if (r.reject) throw new TypeError('network');
    if (r.hang) return new Promise(function (_, rej) { init.signal.addEventListener('abort', function () { rej(new Error('aborted')); }); });
    const status = r.status === undefined ? 200 : r.status;
    const text = r.text !== undefined ? r.text : JSON.stringify(r.body === undefined ? {} : r.body);
    return { ok: status >= 200 && status < 300, status: status, text: async function () { return text; } };
  }
  fetchFn.calls = calls;
  return fetchFn;
}

function newClient(f, t, extra) {
  const c = app.createClient(Object.assign({ fetch: f, AbortController: AbortController, setTimeout: t.setTimeout, clearTimeout: t.clearTimeout }, extra));
  c.setToken(TOKEN);
  return c;
}

function resultRaw(id, status, extra) {
  return { id: id, revision: 1, updated_at: '2026-01-01T00:00:00Z', payload: Object.assign({ module: 'demo', action: 'status', status: status, feedback_id: id }, extra) };
}

// ------------------------------------------------------------ 网络层

test('client: 令牌只出现在 Authorization 头，安全的 fetch 选项固定，断开后不再发请求', async () => {
  const f = makeFetch(function () { return { body: { ok: 1 } }; });
  const t = makeTimers();
  const c = newClient(f, t);
  assert.equal(c.setToken('short'), false);
  const r = await c.get('/v1/me');
  assert.equal(r.ok, true);
  const call = f.calls[0];
  assert.equal(call.headers.Authorization, 'Bearer ' + TOKEN);
  assert.ok(!call.url.includes(TOKEN));
  assert.equal(call.init.credentials, 'omit');
  assert.equal(call.init.cache, 'no-store');
  assert.equal(call.init.redirect, 'error');
  assert.equal(call.init.referrerPolicy, 'no-referrer');
  assert.equal(t.pending(), 0);
  c.clear();
  const r2 = await c.get('/v1/me');
  assert.equal(r2.notConnected, true);
  assert.equal(f.calls.length, 1);
});

test('client: 断开后才返回的响应被标记 stale', async () => {
  let release;
  const gate = new Promise(function (r) { release = r; });
  const f = makeFetch(async function () { await gate; return { body: {} }; });
  const c = newClient(f, makeTimers());
  const p = c.get('/v1/me');
  await tick();
  c.clear();
  release();
  const r = await p;
  assert.equal(r.stale, true);
  assert.equal(r.ok, false);
});

test('client: 401 触发一次断开回调；错误码只接受合法形状', async () => {
  let auth = 0;
  const t = makeTimers();
  const f = makeFetch(function (c) {
    if (c.url === '/a') return { status: 401, body: { code: 'unauthenticated' } };
    if (c.url === '/b') return { status: 403, body: { code: 'admin_required' } };
    if (c.url === '/c') return { status: 403, body: { code: 'bad code!' } };
    return { status: 500, text: '<html>oops</html>' };
  });
  const c = newClient(f, t, { onAuthFailure: function () { auth++; } });
  const a = await c.get('/a');
  assert.equal(a.error.authFailure, true);
  assert.equal(auth, 1);
  const b = await c.get('/b');
  assert.ok(b.error.message.includes('管理员'));
  assert.equal(auth, 1);
  assert.equal((await c.get('/c')).error.code, null);
  const d = await c.get('/d');
  assert.equal(d.error.code, null);
  assert.equal(d.status, 500);
});

test('client: 超时、断网、无法解析的成功响应，对 POST 都是“结果未知”，对 GET 不是', async () => {
  const t = makeTimers();
  const hang = makeFetch(function () { return { hang: true }; });
  const c1 = newClient(hang, t);
  const p = c1.post('/v1/management/requests', { a: 1 });
  await tick();
  t.runDue(20000);
  const r1 = await p;
  assert.equal(r1.error.outcomeUnknown, true);

  const c2 = newClient(makeFetch(function () { return { reject: true }; }), makeTimers());
  assert.equal((await c2.post('/x', {})).error.outcomeUnknown, true);
  assert.equal((await c2.get('/x')).error.outcomeUnknown, false);

  const c3 = newClient(makeFetch(function () { return { status: 202, text: 'not json' }; }), makeTimers());
  assert.equal((await c3.post('/x', {})).error.outcomeUnknown, true);
});

// ------------------------------------------------------------ 有界轮询

function pollerEnv(responses, opts) {
  const t = makeTimers();
  let i = 0;
  const f = makeFetch(function () { return responses[Math.min(i++, responses.length - 1)]; });
  const client = newClient(f, t);
  const events = { phases: [], stops: [] };
  const poller = app.createPoller(Object.assign({
    client: client, setTimeout: t.setTimeout, clearTimeout: t.clearTimeout,
    onUpdate: function (id, n, v) { events.phases.push(v.phase); },
    onStop: function (id, why) { events.stops.push([id, why]); }
  }, opts));
  return { t: t, f: f, client: client, poller: poller, events: events };
}

test('poller: 终态后停止，且只查询必要的次数', async () => {
  const id = idn(5);
  const env = pollerEnv([{ body: resultRaw(id, 'pending') }, { body: resultRaw(id, 'pending') }, { body: resultRaw(id, 'succeeded') }]);
  assert.equal(env.poller.track(id), true);
  await pump(env.t);
  assert.deepEqual(env.events.phases, ['processing', 'processing', 'effective']);
  assert.deepEqual(env.events.stops, [[id, 'terminal']]);
  assert.equal(env.f.calls.length, 3);
  assert.equal(env.poller.count(), 0);
});

test('poller: 永远不终结时在 maxAttempts 处停止，不再自动刷新', async () => {
  const id = idn(6);
  const env = pollerEnv([{ body: resultRaw(id, 'accepted') }], { maxAttempts: 5 });
  env.poller.track(id);
  await pump(env.t, 40);
  assert.equal(env.f.calls.length, 5);
  assert.deepEqual(env.events.stops, [[id, 'limit']]);
  assert.equal(env.t.pending(), 0);
});

test('poller: 并发上限；连续失败上限；错误 ID 的响应算失败', async () => {
  const a = idn(7), b = idn(8), c = idn(9);
  const env = pollerEnv([{ body: resultRaw(a, 'pending') }], { maxConcurrent: 2 });
  assert.equal(env.poller.track(a), true);
  assert.equal(env.poller.track(b), true);
  assert.equal(env.poller.track(c), false);
  assert.equal(env.poller.track('not-an-id'), false);
  env.poller.stopAll('x');

  const failing = pollerEnv([{ status: 500, body: { code: 'internal_error' } }], { failureLimit: 3 });
  failing.poller.track(a);
  await pump(failing.t, 30);
  assert.equal(failing.f.calls.length, 3);
  assert.deepEqual(failing.events.stops, [[a, 'errors']]);

  const wrong = pollerEnv([{ body: resultRaw(b, 'succeeded') }], { failureLimit: 2 });
  wrong.poller.track(a);
  await pump(wrong.t, 30);
  assert.deepEqual(wrong.events.phases, []);
  assert.deepEqual(wrong.events.stops, [[a, 'errors']]);
});

test('poller: 401 停止所有任务；stopAll 之后迟到的响应被忽略', async () => {
  const a = idn(7), b = idn(8);
  const auth = pollerEnv([{ status: 401, body: { code: 'unauthenticated' } }]);
  auth.poller.track(a);
  auth.poller.track(b);
  await pump(auth.t, 10);
  assert.deepEqual(auth.events.stops.map(function (s) { return s[1]; }).sort(), ['auth', 'auth']);

  let release;
  const gate = new Promise(function (r) { release = r; });
  const t = makeTimers();
  const f = makeFetch(async function () { await gate; return { body: resultRaw(a, 'succeeded') }; });
  const client = newClient(f, t);
  const events = { phases: [], stops: [] };
  const poller = app.createPoller({ client: client, setTimeout: t.setTimeout, clearTimeout: t.clearTimeout, onUpdate: function (i, n, v) { events.phases.push(v.phase); }, onStop: function (i, why) { events.stops.push(why); } });
  poller.track(a);
  await tick();
  t.runDue();
  await tick();
  poller.stopAll('disconnect');
  release();
  await settle();
  assert.deepEqual(events.phases, []);
  assert.deepEqual(events.stops, ['disconnect']);
  assert.ok(app.LIMITS.pollMaxAttempts * 5000 <= 300000);
  let total = 0;
  for (let i = 0; i < app.LIMITS.pollMaxAttempts; i++) total += core.backoffDelay(i);
  assert.ok(total < 300000);
});

// ------------------------------------------------------------ 请求 ID 与领取

test('submission: 结果未知后只有内容完全相同才复用请求 ID；明确结果后清除', () => {
  let now = NOW;
  const s = app.createSubmission(function (a) { a.fill(7); }, function () { return now++; });
  const p1 = { action: 'disable', module: 'demo' };
  const a = s.prepare(p1);
  assert.equal(a.reused, false);
  assert.ok(core.isId(a.requestId));
  assert.equal(a.body.request_id, a.requestId);
  assert.equal('request_id' in p1, false);
  s.settle(true);
  const b = s.prepare({ module: 'demo', action: 'disable' });
  assert.equal(b.reused, true);
  assert.equal(b.requestId, a.requestId);
  s.settle(true);
  const c = s.prepare({ action: 'enable', module: 'demo' });
  assert.equal(c.reused, false);
  assert.equal(c.supersededUnknown, true);
  assert.notEqual(c.requestId, a.requestId);
  s.settle(false);
  assert.equal(s.hasPending(), false);
  assert.equal(s.prepare(p1).supersededUnknown, false);
});

test('claim: 每个反馈 ID 只尝试一次；结果未知也绝不重试；POST 不带 body', async () => {
  const fid = idn(1);
  const f = makeFetch(function () { return { reject: true }; });
  const client = newClient(f, makeTimers());
  const guard = app.createClaimGuard();
  const r1 = await app.claimCredential(client, guard, fid);
  assert.equal(r1.ok, false);
  assert.equal(r1.error.outcomeUnknown, true);
  assert.equal(guard.state(fid), 'unknown');
  const r2 = await app.claimCredential(client, guard, fid);
  assert.equal(r2.blocked, true);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].body, undefined);
  assert.equal(f.calls[0].method, 'POST');
  assert.ok(f.calls[0].url.endsWith('/v1/management/credentials/' + fid + '/claim'));
  assert.equal((await app.claimCredential(client, guard, 'bad')).blocked, true);
});

test('claim: 成功返回令牌并阻止重复；403 视为已被拒绝；401 不消耗机会', async () => {
  const fid = idn(2);
  const ok = newClient(makeFetch(function () { return { body: { principal_id: idn(3), token: CLAIM_TOKEN } }; }), makeTimers());
  const guard = app.createClaimGuard();
  const r = await app.claimCredential(ok, guard, fid);
  assert.equal(r.ok, true);
  assert.equal(r.claim.token, CLAIM_TOKEN);
  assert.equal(guard.state(fid), 'delivered');
  assert.equal((await app.claimCredential(ok, guard, fid)).blocked, true);

  const g2 = app.createClaimGuard();
  const forbidden = newClient(makeFetch(function () { return { status: 403, body: { code: 'forbidden' } }; }), makeTimers());
  const r403 = await app.claimCredential(forbidden, g2, fid);
  assert.ok(r403.error.message.includes('无法领取'));
  assert.equal(g2.state(fid), 'rejected');

  const g3 = app.createClaimGuard();
  const unauth = newClient(makeFetch(function () { return { status: 401, body: { code: 'unauthenticated' } }; }), makeTimers());
  await app.claimCredential(unauth, g3, fid);
  assert.equal(g3.state(fid), null);

  const g4 = app.createClaimGuard();
  const badBody = newClient(makeFetch(function () { return { body: { principal_id: idn(3), token: 'short' } }; }), makeTimers());
  const rb = await app.claimCredential(badBody, g4, fid);
  assert.equal(rb.ok, false);
  assert.equal(g4.state(fid), 'unknown');
});

// ------------------------------------------------------------ 假 DOM 下的真实页面

class FakeNode {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.parentNode = null;
    this.attrs = {};
    this.listeners = {};
    this._text = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = '';
    this.className = '';
    this.open = false;
  }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; }
  get textContent() { return this._text + this.children.map(function (c) { return c.textContent; }).join(''); }
  set textContent(v) { this.children.forEach(function (c) { c.parentNode = null; }); this.children = []; this._text = String(v); }
  get innerHTML() { throw new Error('innerHTML is forbidden'); }
  set innerHTML(v) { throw new Error('innerHTML is forbidden'); }
  get outerHTML() { throw new Error('outerHTML is forbidden'); }
  set outerHTML(v) { throw new Error('outerHTML is forbidden'); }
  insertAdjacentHTML() { throw new Error('insertAdjacentHTML is forbidden'); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatch(t, extra) {
    const ev = Object.assign({ type: t, target: this, preventDefault: function () { this.defaultPrevented = true; } }, extra);
    (this.listeners[t] || []).forEach(function (fn) { fn(ev); });
    return ev;
  }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() {}
}

function walk(node, fn) { fn(node); node.children.forEach(function (c) { walk(c, fn); }); }
function findButtons(root, label) {
  const out = [];
  walk(root, function (n) { if (n.tagName === 'button' && n.textContent.includes(label)) out.push(n); });
  return out;
}

function makeDom() {
  const html = read('index.html');
  const nodes = new Map();
  const re = /<(\w+)([^>]*?)\sid="([^"]+)"([^>]*)>/g;
  let m;
  while ((m = re.exec(html))) {
    const n = new FakeNode(m[1]);
    const attrs = m[2] + ' ' + m[4];
    n.hidden = /(^|\s)hidden(\s|$|=)/.test(attrs);
    n.disabled = /(^|\s)disabled(\s|$|=)/.test(attrs);
    nodes.set(m[3], n);
  }
  const doc = {
    createElement: function (tag) { return new FakeNode(tag); },
    createTextNode: function (s) { const n = new FakeNode('#text'); n._text = String(s); return n; },
    getElementById: function (id) { return nodes.get(id) || null; }
  };
  const win = { addEventListener: function () {}, navigator: {} };
  const stored = [];
  Object.defineProperty(win, 'localStorage', { get: function () { stored.push('localStorage'); throw new Error('storage forbidden'); } });
  Object.defineProperty(win, 'sessionStorage', { get: function () { stored.push('sessionStorage'); throw new Error('storage forbidden'); } });
  return { doc: doc, win: win, nodes: nodes, stored: stored, n: function (id) { return nodes.get(id); } };
}
function allText(dom) {
  return Array.from(dom.nodes.values()).map(function (n) { return n.textContent + '|' + n.value; }).join('\n');
}
function bootPage(handler) {
  const dom = makeDom();
  const t = makeTimers();
  const f = makeFetch(handler);
  let clock = NOW;
  const page = app.boot(dom.win, dom.doc, {
    fetch: f, setTimeout: t.setTimeout, clearTimeout: t.clearTimeout, AbortController: AbortController,
    getRandomValues: function (a) { for (let i = 0; i < a.length; i++) a[i] = (i * 31 + 3) & 255; return a; },
    now: function () { return ++clock; }
  });
  return { dom: dom, t: t, f: f, page: page };
}
async function connectAs(env) {
  env.dom.n('token-input').value = TOKEN;
  env.dom.n('connect-form').dispatch('submit');
  await settle();
}
const PLUGIN_LIST = { items: [{ module: 'demo', types: [{ name: 'demo_note', kind: 'semantic', supports_reevaluation: false, requires_dependencies: false }], enabled: null, online: null, status_source: 'unavailable_submit_status_feedback' }], next_after: null };

async function confirmWithAck(env) {
  assert.equal(env.dom.n('confirm-dialog').open, true);
  assert.equal(env.dom.n('confirm-ok').disabled, true);
  env.dom.n('confirm-ack').checked = true;
  env.dom.n('confirm-ack').dispatch('change');
  assert.equal(env.dom.n('confirm-ok').disabled, false);
  env.dom.n('confirm-ok').dispatch('click');
  await settle();
}

test('page: 普通用户看不到管理界面，且不会发出任何管理请求；文字只经 textContent', async () => {
  const subject = '<img src=x onerror=alert(1)>';
  const env = bootPage(function (c) {
    return c.url === '/v1/me' ? { body: { subject: subject, role: 'user' } } : { status: 500 };
  });
  await connectAs(env);
  assert.deepEqual(env.f.calls.map(function (c) { return c.url; }), ['/v1/me']);
  assert.equal(env.dom.n('user-panel').hidden, false);
  assert.equal(env.dom.n('admin-area').hidden, true);
  assert.equal(env.dom.n('token-input').value, '');
  assert.ok(env.dom.n('session-info').textContent.includes(subject));
  assert.ok(!allText(env.dom).includes(TOKEN));
  await env.page.startSubmit({ action: 'status', module: 'demo' }, false);
  await env.page.loadTasks(true);
  await env.page.loadPlugins(true);
  assert.equal(env.f.calls.length, 1);
  assert.deepEqual(env.dom.stored, []);
});

test('page: 管理员——列表、已受理≠已生效、轮询至终态、令牌只在内存', async () => {
  const RID = idn(10), FID = idn(11);
  let polls = 0;
  const env = bootPage(function (c) {
    if (c.url === '/v1/me') return { body: { subject: 'root', role: 'admin' } };
    if (c.url.startsWith('/v1/plugins')) return { body: PLUGIN_LIST };
    if (c.method === 'POST' && c.url === '/v1/management/requests') return { status: 202, body: { status: 'accepted', feedback_id: FID, result_id: RID } };
    if (c.url === '/v1/management/results/' + RID) {
      polls++;
      if (polls === 1) return { body: resultRaw(RID, 'accepted', { feedback_id: FID }) };
      return { body: resultRaw(RID, 'succeeded', { feedback_id: FID, stage: 'complete', enabled: true, ever_enabled: true, resources_ready: true, revision: 3, need_recovery_decision: false, read_types: ['demo_note'], subscribe_types: [] }) };
    }
    if (c.url.startsWith('/v1/management/results')) return { body: { items: [], next_cursor: null } };
    return { status: 404, body: { code: 'not_found' } };
  });
  await connectAs(env);
  assert.equal(env.dom.n('admin-area').hidden, false);
  const cards = env.dom.n('plugins-list').textContent;
  assert.ok(cards.includes('demo'));
  assert.ok(cards.includes('配置状态：未知'));
  assert.ok(cards.includes('进程在线：未知'));

  env.dom.n('module-input').value = 'demo';
  env.dom.n('action-form').dispatch('submit');
  await settle();
  const post = env.f.calls.find(function (c) { return c.method === 'POST'; });
  assert.equal(post.body.action, 'status');
  assert.equal(post.body.module, 'demo');
  assert.ok(core.isId(post.body.request_id));
  assert.ok(!JSON.stringify(post.body).includes(TOKEN));
  assert.ok(env.dom.n('live-status').textContent.includes('已受理'));
  assert.ok(!env.dom.n('tasks-list').textContent.includes('已生效'));
  assert.ok(env.dom.n('banner').textContent.includes('不等于已生效'));

  await pump(env.t, 10);
  const tasks = env.dom.n('tasks-list').textContent;
  assert.ok(tasks.includes('状态快照已返回'));
  assert.ok(tasks.includes('已生效'));
  assert.ok(tasks.includes('进程在线：未知') || tasks.includes('进程在线未知'));
  assert.ok(env.dom.n('plugins-list').textContent.includes('配置状态：已启用'));
  assert.ok(env.dom.n('plugins-list').textContent.includes('进程在线：未知'));
  assert.equal(env.page.poller.count(), 0);
  assert.equal(polls, 2);
  assert.ok(!allText(env.dom).includes(TOKEN));
  assert.deepEqual(env.dom.stored, []);
  env.f.calls.forEach(function (c) { assert.ok(!c.url.includes(TOKEN)); });
});

test('page: 一次性领取——需要确认、只领一次、令牌显示后可清除', async () => {
  const RID = idn(12), FID = idn(13);
  const rotate = resultRaw(RID, 'succeeded', { action: 'rotate_credentials', stage: 'claim_authorized', feedback_id: FID, credential_claim_required: true, enabled: false });
  const env = bootPage(function (c) {
    if (c.url === '/v1/me') return { body: { subject: 'root', role: 'admin' } };
    if (c.url.startsWith('/v1/plugins')) return { body: PLUGIN_LIST };
    if (c.method === 'POST' && c.url === '/v1/management/credentials/' + FID + '/claim') return { body: { principal_id: idn(14), token: CLAIM_TOKEN } };
    if (c.url === '/v1/management/results/' + RID) return { body: rotate };
    if (c.url.startsWith('/v1/management/results')) return { body: { items: [rotate], next_cursor: null } };
    return { status: 404, body: { code: 'not_found' } };
  });
  await connectAs(env);
  const buttons = findButtons(env.dom.n('tasks-list'), '领取凭据');
  assert.equal(buttons.length, 1);
  assert.equal(env.f.calls.filter(function (c) { return c.method === 'POST'; }).length, 0);
  buttons[0].dispatch('click');
  await settle();
  assert.equal(env.f.calls.filter(function (c) { return c.method === 'POST'; }).length, 0);
  await confirmWithAck(env);
  const claims = env.f.calls.filter(function (c) { return c.method === 'POST'; });
  assert.equal(claims.length, 1);
  assert.equal(claims[0].body, undefined);
  assert.equal(env.dom.n('claim-panel').hidden, false);
  assert.equal(env.dom.n('claim-token').textContent, CLAIM_TOKEN);
  assert.equal(findButtons(env.dom.n('tasks-list'), '领取凭据').length, 0);
  assert.ok(env.dom.n('tasks-list').textContent.includes('只显示过一次'));
  assert.ok(!env.dom.n('tasks-list').textContent.includes(CLAIM_TOKEN));

  env.dom.n('claim-dismiss').dispatch('click');
  assert.equal(env.dom.n('claim-panel').hidden, true);
  assert.ok(!allText(env.dom).includes(CLAIM_TOKEN));
  await env.page.loadTasks(true);
  assert.equal(findButtons(env.dom.n('tasks-list'), '领取凭据').length, 0);
  assert.equal(env.f.calls.filter(function (c) { return c.method === 'POST'; }).length, 1);
});

test('page: 历史补处理没有默认范围；范围明确后以 UTC 半开区间提交', async () => {
  const env = bootPage(function (c) {
    if (c.url === '/v1/me') return { body: { subject: 'root', role: 'admin' } };
    if (c.url.startsWith('/v1/plugins')) return { body: PLUGIN_LIST };
    if (c.method === 'POST') return { status: 202, body: { status: 'accepted', feedback_id: idn(15), result_id: idn(16) } };
    return { body: { items: [], next_cursor: null } };
  });
  await connectAs(env);
  env.dom.n('action-select').value = 'history_backfill';
  env.dom.n('action-select').dispatch('change');
  assert.equal(env.dom.n('fs-history').hidden, false);
  assert.equal(env.dom.n('range-start').value, '');
  assert.equal(env.dom.n('range-end').value, '');
  assert.ok(env.dom.n('range-preview').textContent.includes('明确'));

  env.dom.n('module-input').value = 'demo';
  env.dom.n('input-types').value = 'demo_note';
  env.dom.n('action-form').dispatch('submit');
  await settle();
  assert.equal(env.dom.n('form-errors').hidden, false);
  assert.ok(env.dom.n('form-errors').textContent.includes('明确选择历史范围'));
  assert.equal(env.f.calls.filter(function (c) { return c.method === 'POST'; }).length, 0);

  env.dom.n('range-start').value = '2026-01-01T00:00';
  env.dom.n('range-end').value = '2026-01-02T00:00';
  env.dom.n('range-end').dispatch('input');
  assert.ok(env.dom.n('range-preview').textContent.includes('[2026-01-01T00:00:00Z, 2026-01-02T00:00:00Z)'));
  env.dom.n('action-form').dispatch('submit');
  await settle();
  assert.ok(env.dom.n('confirm-summary').textContent.includes('[2026-01-01T00:00:00Z, 2026-01-02T00:00:00Z)'));
  await confirmWithAck(env);
  const post = env.f.calls.find(function (c) { return c.method === 'POST'; });
  assert.equal(post.body.action, 'history_backfill');
  assert.equal(post.body.rangeStart, '2026-01-01T00:00:00Z');
  assert.equal(post.body.rangeEnd, '2026-01-02T00:00:00Z');
  assert.deepEqual(post.body.input_types, ['demo_note']);
  assert.ok(core.isId(post.body.request_id));
  assert.equal('range_start' in post.body, false);
});

test('page: 受理结果未知后，内容不变再次提交复用同一个请求 ID', async () => {
  let posts = 0;
  const env = bootPage(function (c) {
    if (c.url === '/v1/me') return { body: { subject: 'root', role: 'admin' } };
    if (c.url.startsWith('/v1/plugins')) return { body: PLUGIN_LIST };
    if (c.method === 'POST') {
      posts++;
      if (posts === 1) return { reject: true };
      return { status: 202, body: { status: 'accepted', feedback_id: idn(17), result_id: idn(18) } };
    }
    return { body: { items: [], next_cursor: null } };
  });
  await connectAs(env);
  env.dom.n('action-select').value = 'disable';
  env.dom.n('action-select').dispatch('change');
  env.dom.n('module-input').value = 'demo';
  env.dom.n('action-form').dispatch('submit');
  await settle();
  await confirmWithAck(env);
  assert.equal(posts, 1);
  assert.ok(env.dom.n('banner').textContent.includes('请求 ID'));
  assert.equal(env.dom.n('submit-btn').disabled, false);
  assert.ok(env.dom.n('submit-note').textContent.includes('同一个请求 ID'));

  env.dom.n('action-form').dispatch('submit');
  await settle();
  await confirmWithAck(env);
  assert.equal(posts, 2);
  const bodies = env.f.calls.filter(function (c) { return c.method === 'POST'; }).map(function (c) { return c.body; });
  assert.equal(bodies[0].request_id, bodies[1].request_id);
  assert.equal(env.dom.n('submit-note').textContent, '');
});

test('page: 启用时附带恢复选择，必须明确选择并带修订号', async () => {
  const env = bootPage(function (c) {
    if (c.url === '/v1/me') return { body: { subject: 'root', role: 'admin' } };
    if (c.url.startsWith('/v1/plugins')) return { body: PLUGIN_LIST };
    return { body: { items: [], next_cursor: null } };
  });
  await connectAs(env);
  env.dom.n('action-select').value = 'enable';
  env.dom.n('action-select').dispatch('change');
  env.dom.n('module-input').value = 'demo';
  env.dom.n('resume-include').checked = true;
  env.dom.n('resume-include').dispatch('change');
  env.dom.n('action-form').dispatch('submit');
  await settle();
  assert.ok(env.dom.n('form-errors').textContent.includes('明确选择'));
  env.dom.n('resume-catch-up').checked = true;
  env.dom.n('resume-revision').value = '5';
  env.dom.n('action-form').dispatch('submit');
  await settle();
  assert.equal(env.dom.n('confirm-dialog').open, true);
  assert.ok(env.dom.n('confirm-summary').textContent.includes('补处理（catch_up）'));
  assert.ok(env.dom.n('confirm-notes').textContent.includes('修订号 5'));
  env.dom.n('confirm-cancel').dispatch('click');
  await settle();
  assert.equal(env.dom.n('confirm-dialog').open, false);
  assert.equal(env.f.calls.filter(function (c) { return c.method === 'POST'; }).length, 0);
});

test('page: 断开会清除令牌显示、停止轮询并回到未连接', async () => {
  const RID = idn(19);
  const env = bootPage(function (c) {
    if (c.url === '/v1/me') return { body: { subject: 'root', role: 'admin' } };
    if (c.url.startsWith('/v1/plugins')) return { body: PLUGIN_LIST };
    if (c.url.startsWith('/v1/management/results')) return { body: { items: [resultRaw(RID, 'accepted')], next_cursor: null } };
    return { status: 404, body: { code: 'not_found' } };
  });
  await connectAs(env);
  assert.equal(env.page.poller.count(), 1);
  env.dom.n('disconnect-btn').dispatch('click');
  await settle();
  assert.equal(env.page.poller.count(), 0);
  assert.equal(env.page.client.connected(), false);
  assert.equal(env.dom.n('admin-area').hidden, true);
  assert.equal(env.dom.n('token-input').disabled, false);
  assert.equal(env.dom.n('tasks-list').children.length, 0);
});

// ------------------------------------------------------------ 静态卫生

test('static: core.js 与 app.js 不使用解析 HTML、持久存储、日志或其他通道的 API', () => {
  const FORBIDDEN = /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new Function|localStorage|sessionStorage|indexedDB|document\.cookie|console\.|location\.(hash|search|href)|history\.(push|replace)State|sendBeacon|XMLHttpRequest|WebSocket|EventSource|importScripts/;
  ['core.js', 'app.js'].forEach(function (f) { assert.doesNotMatch(read(f), FORBIDDEN, f); });
});

test('static: index.html 符合 CSP（无内联脚本/样式/事件属性/外部资源），资源用相对路径', () => {
  const html = read('index.html');
  assert.doesNotMatch(html, /<script(?![^>]*\ssrc=)/i);
  assert.doesNotMatch(html, /<style/i);
  assert.doesNotMatch(html, /\son[a-z]+\s*=/i);
  assert.doesNotMatch(html, /\sstyle\s*=/i);
  assert.doesNotMatch(html, /https?:\/\//i);
  assert.ok(html.indexOf('src="core.js"') > 0 && html.indexOf('src="core.js"') < html.indexOf('src="app.js"'));
  assert.ok(/<link rel="stylesheet" href="styles\.css">/.test(html));
  assert.ok(/<input id="token-input" type="password" autocomplete="off"/.test(html));
  const ids = Array.from(html.matchAll(/\sid="([^"]+)"/g)).map(function (m) { return m[1]; });
  assert.equal(new Set(ids).size, ids.length);
  const used = Array.from(read('app.js').matchAll(/\$\('([a-z0-9-]+)'\)/g)).map(function (m) { return m[1]; });
  assert.ok(used.length > 40);
  used.forEach(function (id) { assert.ok(ids.includes(id), 'app.js 引用了 index.html 里没有的 #' + id); });
});

test('static: styles.css 无外部引用，并保证 hidden 属性生效', () => {
  const css = read('styles.css');
  assert.doesNotMatch(css, /@import|url\(|expression\(/i);
  assert.ok(/\[hidden\]\s*\{\s*display:\s*none\s*!important/.test(css));
});

test('page: 历史补处理只填了一端时仍被拒绝，不会替用户补默认值', async () => {
  const env = bootPage(function (c) {
    if (c.url === '/v1/me') return { body: { subject: 'root', role: 'admin' } };
    if (c.url.startsWith('/v1/plugins')) return { body: PLUGIN_LIST };
    return { body: { items: [], next_cursor: null } };
  });
  await connectAs(env);
  env.dom.n('action-select').value = 'history_backfill';
  env.dom.n('action-select').dispatch('change');
  env.dom.n('module-input').value = 'demo';
  env.dom.n('input-types').value = 'demo_note';
  const posts = function () { return env.f.calls.filter(function (c) { return c.method === 'POST'; }).length; };

  env.dom.n('range-start').value = '';
  env.dom.n('range-end').value = '2026-01-02T00:00';
  env.dom.n('action-form').dispatch('submit');
  await settle();
  assert.ok(env.dom.n('form-errors').textContent.includes('明确选择历史范围'));
  assert.equal(env.dom.n('confirm-dialog').open, false);
  assert.equal(posts(), 0);
  assert.equal(env.dom.n('range-start').value, '');

  env.dom.n('range-start').value = '2026-01-01T00:00';
  env.dom.n('range-end').value = '';
  env.dom.n('action-form').dispatch('submit');
  await settle();
  assert.ok(env.dom.n('form-errors').textContent.includes('明确选择历史范围'));
  assert.equal(env.dom.n('confirm-dialog').open, false);
  assert.equal(posts(), 0);
  assert.equal(env.dom.n('range-end').value, '');
});
