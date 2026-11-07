'use strict';
// 纯逻辑测试：node --test services/api/tests/ui/
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const core = require(path.join(__dirname, '..', '..', 'src', 'ear_api', 'static', 'core.js'));

const NOW = 1700000000000;
function idn(n) {
  return core.newRequestId(function (a) { a.fill(n); }, NOW + n);
}
function raw(payload, extra) {
  return Object.assign({ id: idn(1), revision: 1, updated_at: '2026-01-01T00:00:00Z', payload: payload }, extra);
}
function norm(payload) {
  return core.normalizeResult(raw(payload));
}

test('newRequestId: UUIDv7 格式，时间戳在前，随机部分来自注入的 RNG', () => {
  const id = core.newRequestId(function (a) { for (let i = 0; i < a.length; i++) a[i] = (i * 29 + 5) & 255; }, NOW);
  assert.ok(core.isId(id));
  assert.equal(id[14], '7');
  assert.equal(id.replace(/-/g, '').slice(0, 12), NOW.toString(16).padStart(12, '0'));
  assert.notEqual(idn(1), idn(2));
});

test('buildRequest: 登记——命名空间前缀、重判需完成记录、订阅不得超读取', () => {
  const ok = core.buildRequest({
    action: 'register_plugin', module: 'demo',
    types: [
      { name: 'demo_note', kind: 'semantic', supports_reevaluation: true },
      { name: 'demo_completion', kind: 'system_record' }
    ],
    read_types: ['demo_note'], subscribe_types: ['demo_note']
  });
  assert.equal(ok.ok, true);
  assert.equal(ok.payload.types.length, 2);
  assert.deepEqual(ok.payload.read_types, ['demo_note']);

  const prefix = core.buildRequest({ action: 'register_plugin', module: 'demo', types: [{ name: 'other_note', kind: 'semantic' }], read_types: [], subscribe_types: [] });
  assert.equal(prefix.ok, false);
  assert.ok(prefix.errors.join('').includes('demo_'));

  const noCompletion = core.buildRequest({ action: 'register_plugin', module: 'demo', types: [{ name: 'demo_note', kind: 'rule', supports_reevaluation: true }], read_types: [], subscribe_types: [] });
  assert.equal(noCompletion.ok, false);
  assert.ok(noCompletion.errors.join('').includes('demo_completion'));

  const badKind = core.buildRequest({ action: 'register_plugin', module: 'demo', types: [{ name: 'demo_raw', kind: 'raw_record', supports_reevaluation: true }], read_types: [], subscribe_types: [] });
  assert.equal(badKind.ok, false);

  const sub = core.buildRequest({ action: 'set_permissions', module: 'demo', read_types: ['demo_note'], subscribe_types: ['demo_other'] });
  assert.equal(sub.ok, false);
  assert.ok(sub.errors.join('').includes('订阅不得超出读取范围'));

  const reserved = core.buildRequest({ action: 'register_plugin', module: 'core', types: [{ name: 'core_x', kind: 'semantic' }], read_types: [], subscribe_types: [] });
  assert.ok(reserved.warnings.join('').includes('namespace_unavailable'));

  assert.equal(core.buildRequest({ action: 'status', module: 'Demo' }).ok, false);
  assert.equal(core.buildRequest({ action: 'nope', module: 'demo' }).ok, false);
});

test('buildRequest: 权限整体替换时读取为空要警告', () => {
  const r = core.buildRequest({ action: 'set_permissions', module: 'demo', read_types: [], subscribe_types: [] });
  assert.equal(r.ok, true);
  assert.ok(r.warnings.join('').includes('读取权限为空'));
});

test('buildRequest: 恢复选择必须明确选择且带修订号', () => {
  const none = core.buildRequest({ action: 'enable', module: 'demo', resume: { include: true, choice: null, expected_revision: '5' } });
  assert.equal(none.ok, false);
  assert.ok(none.errors.join('').includes('明确选择'));

  const noRev = core.buildRequest({ action: 'enable', module: 'demo', resume: { include: true, choice: 'skip', expected_revision: '' } });
  assert.equal(noRev.ok, false);
  assert.ok(noRev.errors.join('').includes('expected_revision'));

  ['0', '01', '2147483648', '1.5', 'abc'].forEach(function (bad) {
    assert.equal(core.buildRequest({ action: 'enable', module: 'demo', resume: { include: true, choice: 'catch_up', expected_revision: bad } }).ok, false, bad);
  });

  const ok = core.buildRequest({ action: 'enable', module: 'demo', resume: { include: true, choice: 'catch_up', expected_revision: '12' } });
  assert.equal(ok.ok, true);
  assert.equal(ok.payload.resume_choice, 'catch_up');
  assert.equal(ok.payload.expected_revision, 12);

  const plain = core.buildRequest({ action: 'enable', module: 'demo', resume: { include: false } });
  assert.equal(plain.ok, true);
  assert.equal('resume_choice' in plain.payload, false);
  assert.equal(core.parseRevision(2147483647), 2147483647);
  assert.equal(core.parseRevision(''), null);
});

test('buildRequest: 历史补处理必须有明确范围、半开区间和输入类型，字段名与服务端别名一致', () => {
  const missing = core.buildRequest({ action: 'history_backfill', module: 'demo', rangeStart: '', rangeEnd: '', input_types: ['demo_note'] });
  assert.equal(missing.ok, false);
  assert.ok(missing.errors.join('').includes('明确选择历史范围'));

  const reversed = core.buildRequest({ action: 'history_backfill', module: 'demo', rangeStart: '2026-01-02T00:00:00Z', rangeEnd: '2026-01-02T00:00:00Z', input_types: ['demo_note'] });
  assert.equal(reversed.ok, false);

  const noTypes = core.buildRequest({ action: 'history_backfill', module: 'demo', rangeStart: '2026-01-01T00:00:00Z', rangeEnd: '2026-01-02T00:00:00Z', input_types: [] });
  assert.equal(noTypes.ok, false);

  const ok = core.buildRequest({ action: 'history_backfill', module: 'demo', rangeStart: '2026-01-01T00:00:00Z', rangeEnd: '2026-01-02T00:00:00Z', input_types: ['demo_note'] });
  assert.equal(ok.ok, true);
  assert.deepEqual(Object.keys(ok.payload).sort(), ['action', 'input_types', 'module', 'rangeEnd', 'rangeStart']);
});

test('buildRequest: 重试任务需要原任务反馈 ID；状态查询无额外字段', () => {
  assert.equal(core.buildRequest({ action: 'retry_task', module: 'demo', target_feedback_id: 'x' }).ok, false);
  const ok = core.buildRequest({ action: 'retry_task', module: 'demo', target_feedback_id: idn(3) });
  assert.equal(ok.ok, true);
  assert.equal(ok.payload.target_feedback_id, idn(3));
  assert.deepEqual(core.buildRequest({ action: 'status', module: 'demo' }).payload, { action: 'status', module: 'demo' });
});

test('toUtcIso: UTC 与本地解释，非法日期返回 null', () => {
  assert.equal(core.toUtcIso('2026-01-01T00:00', 'utc'), '2026-01-01T00:00:00Z');
  assert.equal(core.toUtcIso('2026-01-01T00:00:30', 'utc'), '2026-01-01T00:00:30Z');
  assert.equal(core.toUtcIso('2026-02-30T00:00', 'utc'), null);
  assert.equal(core.toUtcIso('', 'utc'), null);
  assert.equal(core.toUtcIso('2026-01-01', 'utc'), null);
  assert.equal(core.toUtcIso('2026-06-15T12:30', 'local'), new Date(2026, 5, 15, 12, 30, 0).toISOString().replace('.000Z', 'Z'));
});

test('fingerprint 与 parseNames', () => {
  assert.equal(core.fingerprint({ a: 1, b: [1, 2], request_id: 'x' }), core.fingerprint({ b: [1, 2], a: 1, request_id: 'y' }));
  assert.notEqual(core.fingerprint({ a: 1 }), core.fingerprint({ a: 2 }));
  const p = core.parseNames('a_b, c;d  a_b BAD');
  assert.deepEqual(p.names, ['a_b', 'c', 'd']);
  assert.deepEqual(p.invalid, ['BAD']);
});

test('interpretResult: 已受理≠已生效，已启用≠进程在线，补处理已授权≠已完成', () => {
  const accepted = core.interpretResult(norm({ module: 'demo', action: 'enable', status: 'accepted' }));
  assert.equal(accepted.phase, 'accepted');
  assert.equal(accepted.terminal, false);
  assert.ok(accepted.headline.includes('尚未'));

  const pending = core.interpretResult(norm({ module: 'demo', action: 'enable', status: 'pending', stage: 'resources' }));
  assert.equal(pending.phase, 'processing');
  assert.equal(pending.terminal, false);

  const enabled = core.interpretResult(norm({ module: 'demo', action: 'enable', status: 'succeeded', enabled: true }));
  assert.equal(enabled.phase, 'effective');
  assert.equal(enabled.terminal, true);
  assert.ok(enabled.notes.join('').includes('进程在线'));

  const backfill = core.interpretResult(norm({ module: 'demo', action: 'history_backfill', status: 'succeeded' }));
  assert.ok(backfill.headline.includes('尚未完成'));
  assert.ok(backfill.notes.join('').includes('不表示补处理已完成'));

  const rotate = core.interpretResult(norm({ module: 'demo', action: 'rotate_credentials', status: 'succeeded', stage: 'claim_authorized' }));
  assert.equal(rotate.needsClaim, true);
  assert.equal(core.interpretResult(norm({ module: 'demo', action: 'rotate_credentials', status: 'pending', stage: 'claim_authorized' })).needsClaim, false);

  const dead = core.interpretResult(norm({ module: 'demo', action: 'enable', status: 'failed', stage: 'recovery_required', error_code: 'delivery_dead' }));
  assert.equal(dead.canRetry, true);
  assert.equal(dead.errorLabel, core.ERROR_LABELS.delivery_dead);
  assert.equal(core.interpretResult(norm({ module: 'demo', action: 'enable', status: 'failed', stage: 'validation', error_code: 'namespace_unavailable' })).canRetry, false);

  const weird = core.interpretResult(norm({ module: 'demo', action: 'enable', status: 'weird' }));
  assert.equal(weird.phase, 'unknown');
  assert.equal(weird.terminal, false);
});

test('normalizeResult: 只保留白名单字段，非法错误码被替换，非法 ID 被拒绝', () => {
  const n = norm({ module: 'demo', action: 'status', status: 'failed', error_code: 'bad code!', token: 'SECRET', extra: { a: 1 } });
  assert.equal('token' in n.payload, false);
  assert.equal('extra' in n.payload, false);
  assert.equal(n.payload.error_code, 'management_failed');
  assert.equal(core.normalizeResult({ id: 'not-an-id', payload: {} }), null);
  assert.equal(core.errorLabel('totally_new_code'), '未收录的错误码');
});

test('normalizePlugins: enabled/online 为 null 时保持“未知”，不被当成 false', () => {
  const out = core.normalizePlugins({
    items: [
      { module: 'demo', types: [{ name: 'demo_note', kind: 'semantic' }], enabled: null, online: null },
      { module: 'Bad Module', types: [] }
    ],
    next_after: 'demo'
  });
  assert.equal(out.items.length, 1);
  assert.equal(out.items[0].enabled, null);
  assert.equal(out.items[0].online, null);
  assert.equal(out.nextAfter, 'demo');
});

test('normalizeClaim: 严格校验，且不复制多余字段', () => {
  const ok = core.normalizeClaim({ principal_id: idn(4), token: 'T'.repeat(43), extra: 'x' });
  assert.deepEqual(Object.keys(ok).sort(), ['principalId', 'token']);
  assert.equal(core.normalizeClaim({ principal_id: idn(4), token: 'short' }), null);
  assert.equal(core.normalizeClaim({ principal_id: 'x', token: 'T'.repeat(43) }), null);
});

test('describeApiError: 结果未知与明确失败要区分', () => {
  const postNet = core.describeApiError({ kind: 'network' }, { method: 'POST' });
  assert.equal(postNet.outcomeUnknown, true);
  assert.ok(postNet.message.includes('请求 ID'));
  const claimNet = core.describeApiError({ kind: 'timeout' }, { method: 'POST', claim: true });
  assert.equal(claimNet.outcomeUnknown, true);
  assert.ok(claimNet.message.includes('不要重复领取'));
  assert.equal(core.describeApiError({ kind: 'network' }, { method: 'GET' }).outcomeUnknown, false);
  assert.equal(core.describeApiError({ kind: 'http', status: 401 }, { method: 'GET' }).authFailure, true);
  assert.ok(core.describeApiError({ kind: 'http', status: 403, code: 'admin_required' }, { method: 'GET' }).message.includes('管理员'));
  assert.ok(core.describeApiError({ kind: 'http', status: 403, code: 'cross_origin_denied' }, { method: 'POST' }).message.includes('public_origin'));
  assert.equal(core.describeApiError({ kind: 'http', status: 503, code: 'management_acceptance_unknown' }, { method: 'POST' }).outcomeUnknown, true);
  assert.equal(core.describeApiError({ kind: 'http', status: 503, code: 'acceptance_unknown_replay_identical_request' }, { method: 'POST' }).outcomeUnknown, true);
  assert.equal(core.describeApiError({ kind: 'http', status: 500 }, { method: 'POST' }).outcomeUnknown, true);
  assert.equal(core.describeApiError({ kind: 'http', status: 404, code: 'not_found' }, { method: 'GET' }).outcomeUnknown, false);
  assert.ok(core.describeApiError({ kind: 'http', status: 403, code: 'forbidden' }, { method: 'POST', claim: true }).message.includes('无法领取'));
});

test('riskProfile: 危险操作要求输入模块名；状态查询不需要确认；补处理说明授权不等于完成', () => {
  const rotate = core.riskProfile({ action: 'rotate_credentials', module: 'demo' });
  assert.equal(rotate.typeWord, 'demo');
  assert.equal(rotate.danger, true);
  assert.equal(core.riskProfile({ action: 'revoke_credentials', module: 'demo' }).typeWord, 'demo');
  assert.equal(core.riskProfile({ action: 'status', module: 'demo' }).confirm, false);
  const disable = core.riskProfile({ action: 'disable', module: 'demo' });
  assert.equal(disable.danger, true);
  assert.ok(disable.ack);
  assert.ok(core.riskProfile({ action: 'history_backfill', module: 'demo' }).notes.join('').includes('不等于已完成'));
  const rows = core.summarize({ action: 'history_backfill', module: 'demo', rangeStart: '2026-01-01T00:00:00Z', rangeEnd: '2026-01-02T00:00:00Z', input_types: ['demo_note'] });
  assert.ok(rows.some(function (r) { return r.value === '[2026-01-01T00:00:00Z, 2026-01-02T00:00:00Z)'; }));
});

test('newerSnapshot 不让旧结果覆盖新状态；backoffDelay 有上限', () => {
  const a = { updatedAt: '2026-01-02T00:00:00Z' };
  assert.equal(core.newerSnapshot(a, { updatedAt: '2026-01-01T00:00:00Z' }), false);
  assert.equal(core.newerSnapshot(a, { updatedAt: '2026-01-02T00:00:00Z' }), true);
  assert.equal(core.newerSnapshot(null, { updatedAt: '2026-01-01T00:00:00Z' }), true);
  assert.equal(core.newerSnapshot(a, null), false);
  assert.equal(core.backoffDelay(-3), 1000);
  assert.equal(core.backoffDelay(500), 5000);
  const snap = core.snapshotFromResult(norm({ module: 'demo', action: 'status', status: 'succeeded', enabled: true, revision: 4, read_types: ['demo_note'] }));
  assert.equal(snap.enabled, true);
  assert.equal(snap.revision, 4);
  assert.equal(core.snapshotFromResult(norm({ module: 'demo', action: 'status', status: 'pending' })), null);
});
