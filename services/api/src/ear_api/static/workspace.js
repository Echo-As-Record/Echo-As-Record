/* Ear 只读中文工作台 — standalone source, no build step or external assets.
 * Entry: GET /workspace/ -> workspace.html; sibling workspace.js/workspace.css.
 * Integration: app.py public /v1/records/{type}, /v1/me, GET status/preview endpoints.
 * Evidence assumption: GET /v1/evidence/search?start=UTC-Z&end=UTC-Z&q=phrase&limit=10&cursor=opaque.
 * See evidence.py search(); <=24h; live_best_effort; 404 is unavailable, never fake results.
 * No public dependency API exists: record-carried refs are explicitly NOT a dependency graph.
 * Recovered module implementation consolidated here; edit this file directly. The previous
 * static/workspace/ prototype and plugin UI remain untouched and are NOT runtime dependencies.
 */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else { root.EarWorkspace = api; api.app.boot(root, root.document); }
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';
  const modules = Object.create(null);
  function define(name, factory) {
    const module = { exports: {} };
    factory(module, function (key) {
      if (!Object.prototype.hasOwnProperty.call(modules, key)) throw new Error('Unknown internal module');
      return modules[key];
    });
    modules[name] = module.exports;
  }

// ===== CORE =====
define('./ws-core.js', function (module, require) {
/*
 * Ear 业务工作台：无 DOM 的纯逻辑（路由、时间与时区、请求路径、错误说明、记录投影、脱敏、版本差异）。
 *
 * 浏览器里挂在 self.EarWsCore；Node 测试用 require 加载。
 * 本文件不访问网络、存储、URL 或 DOM，也不输出日志。它只整理“当前认证 API 已经返回的数据”，
 * 从不编造数据：缺失、畸形的字段一律显示为缺失，不用占位值冒充真实内容。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module !== null && module.exports) {
    module.exports = api;
  } else {
    root.EarWsCore = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // ---------------------------------------------------------------- 常量

  var ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  var UTC_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z$/;
  var DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
  var LOCAL_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;

  // 证据检索接口由主 agent 在后端注册；路径集中在这里，改名只需改此处。
  var EVIDENCE_PATH = '/v1/evidence/search';
  var MAX_EVIDENCE_WINDOW_MS = 24 * 3600 * 1000;
  var MAX_CURSOR = 512;

  // 17 个可浏览业务类型（与 ear_api.business.BUSINESS_TYPES 一致）。view 为专属页面，null 则落到“追溯”页。
  var TYPES = {
    person: { label: '人物', kind: 'semantic', view: 'people', time: false },
    place: { label: '地点', kind: 'semantic', view: 'places', time: false },
    attribution: { label: '归属关系', kind: 'semantic', view: null, time: true },
    activity: { label: '活动', kind: 'semantic', view: 'activities', time: true },
    artifact: { label: '产物', kind: 'semantic', view: 'artifacts', time: true },
    action_suggestion: { label: '动作建议', kind: 'semantic', view: 'actions', time: true },
    rule: { label: '规则', kind: 'rule', view: 'rules', time: false },
    rule_selection: { label: '规则选择', kind: 'semantic', view: null, time: true },
    transcript_segment: { label: '转写段', kind: 'raw_record', view: null, time: true },
    audio_ref: { label: '录音引用', kind: 'raw_record', view: null, time: true },
    local_speaker: { label: '局部说话人', kind: 'raw_record', view: null, time: true },
    voiceprint: { label: '声纹', kind: 'raw_record', view: null, time: true },
    ambient_sound: { label: '环境声', kind: 'raw_record', view: null, time: true },
    gps_point: { label: 'GPS 点', kind: 'raw_record', view: null, time: true },
    gps_geocode: { label: 'GPS 逆地理编码', kind: 'raw_record', view: null, time: true },
    phone_info: { label: '手机信息', kind: 'raw_record', view: null, time: true },
    feedback: { label: '用户反馈', kind: 'raw_record', view: null, time: true }
  };
  var TYPE_ORDER = [
    'activity', 'artifact', 'action_suggestion', 'rule', 'rule_selection', 'person', 'place', 'attribution',
    'transcript_segment', 'audio_ref', 'local_speaker', 'voiceprint', 'ambient_sound',
    'gps_point', 'gps_geocode', 'phone_info', 'feedback'
  ];
  var KIND_LABELS = { raw_record: '原始数据', semantic: '语义信息', rule: '规则' };

  var VIEWS = [
    { id: 'overview', label: '概览' },
    { id: 'timeline', label: '证据时间线' },
    { id: 'activities', label: '活动' },
    { id: 'artifacts', label: '产物' },
    { id: 'people', label: '人物' },
    { id: 'places', label: '地点' },
    { id: 'rules', label: '规则' },
    { id: 'actions', label: '动作建议' },
    { id: 'trace', label: '追溯' }
  ];
  var VIEW_TYPE = {
    people: 'person', places: 'place', activities: 'activity', artifacts: 'artifact',
    rules: 'rule', actions: 'action_suggestion'
  };

  var STATE_LABELS = {
    pending: '待审核', approved: '已批准', rejected: '已拒绝', executing: '执行中',
    executed: '已执行', failed: '执行失败', unknown: '结果未知（需人工核对）'
  };
  var SOURCE_LABELS = { automatic: '自动选择', manual: '手动应用' };
  var ACTION_KIND_LABELS = { calendar: '日历', reminder: '提醒事项' };
  var FEEDBACK_STATUS_LABELS = {
    pending: '处理中（尚无最终结果）', queued: '已排队', running: '运行中', retry_wait: '等待重试',
    waiting_attribution: '等待归属', succeeded: '已成功', blocked: '已阻塞', failed: '已失败',
    rejected: '被拒绝', accepted: '已受理'
  };
  var ATTRIBUTION_STATUS_LABELS = {
    resolved: '已识别为人物', no_voiceprint: '该转写段没有声纹', unavailable: '暂时没有可用的归属',
    ambiguous: '归属有歧义，未显示', person_superseded: '该人物已被整体替代', invalid_name: '人物名称格式异常'
  };
  var PLAYBACK_STATUS_LABELS = {
    available: '可定位到录音位置（本页不提供音频播放）', unavailable: '没有可用的录音引用',
    invalid_offsets: '录音偏移与转写段不一致，已忽略'
  };
  var STOP_REASON_LABELS = {
    exhausted: '该时间窗内已读完', result_limit: '已达到本次条数上限', segment_limit: '已达到单次扫描段数上限',
    deadline: '读取超时，结果不完整', row_bytes: '已达到数据量上限，结果不完整', call_budget: '已达到读取次数上限，结果不完整',
    page_budget: '已达到分页上限，结果不完整', cursor_stalled: '翻页没有进展，已停止', response_bytes: '响应体积达到上限，结果被截断'
  };

  // ---------------------------------------------------------------- 基础

  function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }
  function has(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function isId(v) { return typeof v === 'string' && ID_RE.test(v); }
  function isInt(v) { return typeof v === 'number' && isFinite(v) && Math.floor(v) === v; }
  function str(v, max) {
    if (typeof v !== 'string') return null;
    return max && v.length > max ? v.slice(0, max) : v;
  }
  function pad(n, w) { var s = String(n); while (s.length < (w || 2)) s = '0' + s; return s; }
  function idList(v) {
    if (!Array.isArray(v)) return [];
    return v.filter(isId);
  }
  function typeLabel(t) { return has(TYPES, t) ? TYPES[t].label : (typeof t === 'string' ? t : '未知类型'); }
  function labelOf(map, key, fallback) {
    return typeof key === 'string' && has(map, key) ? map[key] : fallback;
  }
  function shortId(id) { return isId(id) ? id.slice(0, 8) + '…' + id.slice(-4) : '（无效 ID）'; }

  // ---------------------------------------------------------------- 时间与时区

  function parseUtc(iso) {
    if (typeof iso !== 'string' || iso.length > 32) return null;
    var m = UTC_RE.exec(iso);
    if (!m) return null;
    var ms = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6], m[7] ? +(m[7] + '00').slice(0, 3) : 0);
    var d = new Date(ms);
    if (isNaN(ms) || d.getUTCFullYear() !== +m[1] || d.getUTCMonth() !== +m[2] - 1 || d.getUTCDate() !== +m[3] ||
        d.getUTCHours() !== +m[4] || d.getUTCMinutes() !== +m[5] || d.getUTCSeconds() !== +m[6]) return null;
    return ms;
  }
  function toIso(ms) {
    if (typeof ms !== 'number' || !isFinite(ms)) return null;
    var d = new Date(ms);
    if (isNaN(d.getTime())) return null;
    return pad(d.getUTCFullYear(), 4) + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + 'T' +
      pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds()) + 'Z';
  }

  /**
   * 时区：'utc' 或 'local'。offsetAt(ms) 返回该时刻的“东偏分钟数”（UTC+8 为 480）。
   * 默认用浏览器的本地时区；测试可注入固定偏移。
   */
  function makeZone(id, offsetAt) {
    var local = id === 'local';
    var fn = local
      ? (typeof offsetAt === 'function' ? offsetAt : function (ms) { return -new Date(ms).getTimezoneOffset(); })
      : function () { return 0; };
    return {
      id: local ? 'local' : 'utc',
      offsetAt: fn,
      label: function (ms) {
        if (!local) return 'UTC';
        var o = fn(typeof ms === 'number' ? ms : Date.now());
        if (!isFinite(o)) return 'UTC';
        var sign = o < 0 ? '-' : '+';
        var a = Math.abs(o);
        return 'UTC' + sign + Math.floor(a / 60) + (a % 60 ? ':' + pad(a % 60) : '');
      }
    };
  }
  function shifted(ms, zone) { return new Date(ms + zone.offsetAt(ms) * 60000); }

  function formatClock(iso, zone, seconds) {
    var ms = parseUtc(iso);
    if (ms === null) return null;
    var z = zone || makeZone('utc');
    var d = shifted(ms, z);
    var s = pad(d.getUTCFullYear(), 4) + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + ' ' +
      pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes());
    return seconds === false ? s : s + ':' + pad(d.getUTCSeconds());
  }
  function formatTimeOnly(iso, zone) {
    var s = formatClock(iso, zone, true);
    return s === null ? null : s.slice(11);
  }
  function formatDuration(ms) {
    if (typeof ms !== 'number' || !isFinite(ms) || ms < 0) return null;
    var s = Math.round(ms / 1000);
    if (s < 60) return s + ' 秒';
    var m = Math.floor(s / 60);
    if (m < 60) return m + ' 分' + (s % 60 ? ' ' + (s % 60) + ' 秒' : '');
    var h = Math.floor(m / 60);
    return h + ' 小时' + (m % 60 ? ' ' + (m % 60) + ' 分' : '');
  }
  function formatOffsetSeconds(sec) {
    if (typeof sec !== 'number' || !isFinite(sec) || sec < 0) return null;
    var t = Math.floor(sec);
    var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    return (h ? h + ':' + pad(m) : m) + ':' + pad(s);
  }

  // 本地墙钟（无时区）→ UTC 毫秒；两步修正以处理夏令时边界。
  function wallToMs(y, mo, d, h, mi, s, zone) {
    var guess = Date.UTC(y, mo - 1, d, h, mi, s || 0);
    var o1 = zone.offsetAt(guess);
    var ms = guess - o1 * 60000;
    var o2 = zone.offsetAt(ms);
    if (o2 !== o1) ms = guess - o2 * 60000;
    return ms;
  }
  function localToUtcIso(local, zone) {
    if (typeof local !== 'string') return null;
    var m = LOCAL_RE.exec(local);
    if (!m) return null;
    var ms = wallToMs(+m[1], +m[2], +m[3], +m[4], +m[5], m[6] ? +m[6] : 0, zone);
    var back = shifted(ms, zone);
    // 往返校验：拒绝 2026-02-31 这类不存在的日期。
    if (back.getUTCFullYear() !== +m[1] || back.getUTCMonth() !== +m[2] - 1 || back.getUTCDate() !== +m[3] ||
        back.getUTCHours() !== +m[4] || back.getUTCMinutes() !== +m[5] ||
        back.getUTCSeconds() !== (m[6] ? +m[6] : 0)) return null;
    return toIso(ms);
  }
  function utcToLocalInput(iso, zone) {
    var s = formatClock(iso, zone, false);
    return s === null ? null : s.replace(' ', 'T');
  }
  function dateOf(iso, zone) {
    var s = formatClock(iso, zone, false);
    return s === null ? null : s.slice(0, 10);
  }
  function shiftDay(dateStr, n) {
    var m = DATE_RE.exec(dateStr || '');
    if (!m) return null;
    var d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3] + n));
    return pad(d.getUTCFullYear(), 4) + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate());
  }
  function dayBounds(dateStr, zone) {
    var m = DATE_RE.exec(dateStr || '');
    if (!m) return null;
    var start = localToUtcIso(dateStr + 'T00:00', zone);
    var next = shiftDay(dateStr, 1);
    var end = next === null ? null : localToUtcIso(next + 'T00:00', zone);
    if (start === null || end === null) return null;
    return { start: start, end: end };
  }

  /**
   * 时间范围控件的状态 → 请求用的 UTC 区间。后端要求 start 与 end 同时给出或同时缺省，
   * 所以这里只返回完整区间或 null（全部时间）；半填写直接报错，不替用户补默认值。
   */
  function resolveRange(state, zone) {
    var s = state || { mode: 'all' };
    if (s.mode === 'all') return { ok: true, range: null };
    if (s.mode === 'day') {
      var b = dayBounds(s.date, zone);
      if (b === null) return { ok: false, error: '请选择有效的日期。' };
      return { ok: true, range: b };
    }
    if (s.mode === 'custom') {
      if (!s.start || !s.end) return { ok: false, error: '请同时填写开始和结束时间；本页不会替你补默认值。' };
      var a = localToUtcIso(s.start, zone), e = localToUtcIso(s.end, zone);
      if (a === null || e === null) return { ok: false, error: '时间格式无效。' };
      if (parseUtc(e) <= parseUtc(a)) return { ok: false, error: '结束时间必须晚于开始时间（区间为 [开始, 结束)）。' };
      return { ok: true, range: { start: a, end: e } };
    }
    return { ok: false, error: '未知的范围模式。' };
  }

  // ---------------------------------------------------------------- 请求路径

  // encodeURIComponent 不转义 ! ' ( ) *；游标是不透明字符串，这里一并转义，保证路径只含安全字符。
  function enc(v) {
    return encodeURIComponent(String(v)).replace(/[!'()*]/g, function (c) { return '%' + c.charCodeAt(0).toString(16).toUpperCase(); });
  }
  function qs(params) {
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v !== undefined && v !== null && v !== '') parts.push(enc(k) + '=' + enc(v));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }
  function validCursor(c) {
    return typeof c === 'string' && c.length >= 1 && c.length <= MAX_CURSOR && !/[\u0000-\u001f\u007f]/.test(c);
  }
  function rangeOk(range) {
    return isObj(range) && parseUtc(range.start) !== null && parseUtc(range.end) !== null &&
      parseUtc(range.end) >= parseUtc(range.start);
  }
  function listPath(type, o) {
    o = o || {};
    if (!has(TYPES, type)) return null;
    var limit = o.limit === undefined ? 50 : o.limit;
    if (!isInt(limit) || limit < 1 || limit > 100) return null;
    if (o.cursor !== undefined && o.cursor !== null && !validCursor(o.cursor)) return null;
    var p = { limit: limit, cursor: o.cursor || undefined };
    if (o.range) {
      if (!rangeOk(o.range)) return null;
      p.start = o.range.start;
      p.end = o.range.end;
    }
    if (o.includeSuperseded) p.include_superseded = 'true';
    return '/v1/records/' + type + qs(p);
  }
  function recordPath(type, id, version) {
    if (!has(TYPES, type) || !isId(id)) return null;
    if (version !== undefined && version !== null && (!isInt(version) || version < 1 || version > 2147483647)) return null;
    return '/v1/records/' + type + '/' + id + (version ? '?version=' + version : '');
  }
  function historyPath(type, id, after, limit) {
    if (!has(TYPES, type) || !isId(id)) return null;
    var a = after === undefined || after === null ? 0 : after;
    var l = limit === undefined ? 50 : limit;
    if (!isInt(a) || a < 0 || a > 2147483647 || !isInt(l) || l < 1 || l > 100) return null;
    return '/v1/records/' + type + '/' + id + '/history' + qs({ after_version: a || undefined, limit: l });
  }
  function feedbackStatusPath(id) { return isId(id) ? '/v1/feedback/' + id + '/status' : null; }
  function personPreviewPath(id) { return isId(id) ? '/v1/previews/person/' + id : null; }
  function executionPath(id) { return isId(id) ? '/v1/actions/' + id + '/execution' : null; }
  function evidencePath(o) {
    o = o || {};
    if (!rangeOk(o) && !(isObj(o.range) && rangeOk(o.range))) return null;
    var range = o.range || o;
    var span = parseUtc(range.end) - parseUtc(range.start);
    if (span <= 0 || span > MAX_EVIDENCE_WINDOW_MS) return null;
    if (typeof o.q !== 'string' || o.q.length < 1 || o.q.length > 256) return null;
    var limit = o.limit === undefined ? 10 : o.limit;
    if (!isInt(limit) || limit < 1 || limit > 10) return null;
    if (o.cursor !== undefined && o.cursor !== null && !validCursor(o.cursor)) return null;
    return EVIDENCE_PATH + qs({ start: range.start, end: range.end, q: o.q, limit: limit, cursor: o.cursor || undefined });
  }
  // 证据窗口最长 24 小时：夏令时日可能是 25 小时，这里截到开始后的 24 小时并明示。
  function evidenceWindow(range) {
    if (!rangeOk(range)) return { ok: false, error: '请先选择有效的时间范围。' };
    var a = parseUtc(range.start), b = parseUtc(range.end);
    if (b - a <= 0) return { ok: false, error: '时间窗为空。' };
    if (b - a <= MAX_EVIDENCE_WINDOW_MS) return { ok: true, range: { start: range.start, end: range.end }, clipped: false };
    return { ok: true, range: { start: range.start, end: toIso(a + MAX_EVIDENCE_WINDOW_MS) }, clipped: true };
  }

  // ---------------------------------------------------------------- 错误说明

  var CODE_RE = /^[a-zA-Z0-9_]{1,100}$/;
  var CODE_MESSAGES = {
    unauthenticated: '令牌无效、已被撤销或格式不对，已断开连接。',
    forbidden: '没有读取这类数据的授权（由①判定）。',
    admin_required: '这个操作需要管理员令牌。',
    cross_origin_denied: '页面来源与服务配置的 public_origin 不一致，请求被拒绝。请用配置里的地址访问。',
    not_found: '没有找到：记录不存在，或你没有读取它的授权（①对无权读取的 ID 一律隐藏）。',
    invalid_input: '请求参数被服务端拒绝（例如时间范围只填了一端、游标过期或格式无效）。',
    request_rejected: '请求被服务端拒绝。',
    snapshot_unavailable: '读取一致性窗口已过期，请重新加载。',
    resource_limit: '请求超出了服务端的资源上限，请缩小时间范围后重试。',
    body_too_large: '请求体过大。',
    headers_too_large: '请求头过大。',
    request_target_too_large: '请求地址过长。',
    request_timeout: '服务端处理超时。',
    storage_unavailable: '存储服务暂时不可用，请稍后重试。',
    history_incomplete: '该记录的历史暂时不完整，请稍后重试。',
    internal_error: '服务端内部错误。',
    json_required: '服务端要求 JSON。',
    encoding_not_supported: '不支持的内容编码。',
    execution_journal_unavailable: '外部动作日志不可用。',
    extraction_execution_recovery_protocol_pending: '外部动作执行尚未启用。',
    preview_too_large: '预览范围过大。'
  };

  /**
   * 统一的错误描述。输入 {status, code, failure}：failure 为客户端本地失败原因
   * （network / timeout / parse / too_large / not_connected / bad_path）。
   */
  function describeError(err) {
    var e = err || {};
    var status = typeof e.status === 'number' ? e.status : 0;
    var code = typeof e.code === 'string' && CODE_RE.test(e.code) ? e.code : null;
    var kind = 'unknown', message = '请求失败。', retryable = false, authFailure = false;
    if (e.failure === 'not_connected') {
      kind = 'not_connected'; message = '尚未连接：请先粘贴 API 令牌并连接。';
    } else if (e.failure === 'timeout') {
      kind = 'timeout'; message = '请求超时，没有收到响应。'; retryable = true;
    } else if (e.failure === 'network') {
      kind = 'network'; message = '网络错误：无法联系服务。'; retryable = true;
    } else if (e.failure === 'too_large') {
      kind = 'too_large'; message = '响应过大，已丢弃；请缩小时间范围或减少每页数量。';
    } else if (e.failure === 'parse') {
      kind = 'parse'; message = '响应不是预期的 JSON，已丢弃。';
    } else if (e.failure === 'bad_path') {
      kind = 'bad_path'; message = '请求参数无效，没有发出请求。';
    } else if (status === 401) {
      kind = 'auth'; authFailure = true; message = CODE_MESSAGES.unauthenticated;
    } else if (status === 403) {
      kind = code === 'cross_origin_denied' ? 'origin' : 'forbidden';
      message = code && has(CODE_MESSAGES, code) ? CODE_MESSAGES[code] : CODE_MESSAGES.forbidden;
    } else if (status === 404) {
      kind = 'notfound'; message = CODE_MESSAGES.not_found;
    } else if (status === 410) {
      kind = 'gone'; message = CODE_MESSAGES.snapshot_unavailable; retryable = true;
    } else if (status === 413) {
      kind = 'limit'; message = CODE_MESSAGES.resource_limit;
    } else if (status === 422 || status === 400) {
      kind = 'invalid'; message = CODE_MESSAGES.invalid_input;
    } else if (status === 501) {
      kind = 'unsupported'; message = code && has(CODE_MESSAGES, code) ? CODE_MESSAGES[code] : '服务端尚未实现该功能。';
    } else if (status === 503 || status === 502 || status === 504) {
      kind = 'unavailable'; message = code && has(CODE_MESSAGES, code) ? CODE_MESSAGES[code] : CODE_MESSAGES.storage_unavailable; retryable = true;
    } else if (status >= 500) {
      kind = 'server'; message = CODE_MESSAGES.internal_error; retryable = true;
    } else if (status >= 400) {
      kind = 'rejected'; message = code && has(CODE_MESSAGES, code) ? CODE_MESSAGES[code] : CODE_MESSAGES.request_rejected;
    }
    return { kind: kind, status: status, code: code, message: message, retryable: retryable, authFailure: authFailure };
  }

  // ---------------------------------------------------------------- 记录规范化

  /**
   * 把一条服务端记录整理成统一形状。字段缺失/畸形返回 null（调用方计入“已忽略的异常记录”），
   * 绝不补默认值。原始数据版本固定为 1，状态恒为 active。
   */
  function normalizeRecord(rec) {
    if (!isObj(rec) || !isId(rec.id) || !has(TYPES, rec.data_type) || !isObj(rec.payload)) return null;
    var raw = typeof rec.ingested_at === 'string' || isObj(rec.source);
    var version = rec.version;
    if (raw && version !== 1) return null;
    if (!isInt(version) || version < 1) return null;
    var status = raw ? 'active' : rec.status;
    if (status !== 'active' && status !== 'superseded') return null;
    var start = typeof rec.event_start === 'string' ? rec.event_start : null;
    var end = typeof rec.event_end === 'string' ? rec.event_end : null;
    return {
      id: rec.id,
      type: rec.data_type,
      version: version,
      status: status,
      current: status === 'active',
      raw: raw,
      original: rec,
      start: start,
      end: end,
      payload: rec.payload,
      writer: str(rec.writer, 64),
      source: isObj(rec.source) ? { device: str(rec.source.device, 128), channel: str(rec.source.channel, 64) } : null,
      trace: {
        rootFeedbackId: str(rec.root_feedback_id, 64),
        depth: isInt(rec.depth) ? rec.depth : null,
        batchId: isId(rec.batch_id) ? rec.batch_id : null,
        requestEventId: isId(rec.request_event_id) ? rec.request_event_id : null,
        skipHistory: typeof rec.skip_history === 'boolean' ? rec.skip_history : null,
        createdAt: typeof rec.created_at === 'string' ? rec.created_at : (typeof rec.ingested_at === 'string' ? rec.ingested_at : null),
        adapterVersion: str(rec.adapter_version, 64),
        dedupKey: str(rec.dedup_key, 256)
      }
    };
  }
  /** 校验一页列表响应：items 必须是数组；畸形的条目被丢弃并计数。 */
  function readPage(data) {
    if (!isObj(data) || !Array.isArray(data.items)) return { ok: false };
    var items = [], dropped = 0;
    data.items.forEach(function (r) {
      var n = normalizeRecord(r);
      if (n) items.push(n); else dropped++;
    });
    if (data.next_cursor !== null && !validCursor(data.next_cursor)) return { ok: false };
    var next = data.next_cursor;
    return { ok: true, items: items, dropped: dropped, nextCursor: next, consistency: str(data.consistency, 32) };
  }
  /** 历史页：next_after_version 为整数表示还有更多。 */
  function readHistory(data) {
    if (!isObj(data) || !Array.isArray(data.items)) return { ok: false };
    var items = [], dropped = 0;
    data.items.forEach(function (r) {
      var n = normalizeRecord(r);
      if (n) items.push(n); else dropped++;
    });
    if (data.next_after_version !== null && (!isInt(data.next_after_version) || data.next_after_version < 1)) return { ok: false };
    var next = data.next_after_version;
    return { ok: true, items: items, dropped: dropped, nextAfter: next };
  }
  /** 合并分页结果：按 id+版本去重（实时读取下相邻页可能重复），并受总量上限约束。 */
  function mergeItems(existing, incoming, cap) {
    var seen = Object.create(null);
    existing.forEach(function (r) { seen[r.id + '@' + r.version] = true; });
    var out = existing.slice(), added = 0, capped = false;
    for (var i = 0; i < incoming.length; i++) {
      var key = incoming[i].id + '@' + incoming[i].version;
      if (seen[key]) continue;
      if (cap && out.length >= cap) { capped = true; break; }
      seen[key] = true; out.push(incoming[i]); added++;
    }
    return { items: out, added: added, capped: capped || !!(cap && out.length >= cap) };
  }

  // ---------------------------------------------------------------- 脱敏与差异

  var SENSITIVE_KEY = /^(action_key|action_hash|receipt|token|secret|password|authorization|credential|credentials|api_key|storage_token|claim_token|embedding|vector|values|external_id|external_identifier)$/i;
  var MAX_STR = 4000;
  var MAX_ITEMS = 200;
  var MAX_DEPTH = 8;

  function isNumberArray(a) {
    return Array.isArray(a) && a.length > 32 && a.every(function (x) { return typeof x === 'number'; });
  }
  /**
   * 生成可安全展示的深拷贝：隐藏声纹向量等大数值数组、令牌/回执/动作键/哈希、文件路径，
   * 并截断超长文本与过深嵌套。展示用，不用于回写。
   */
  function redact(value, depth, key) {
    var d = depth || 0;
    if (typeof key === 'string' && SENSITIVE_KEY.test(key)) return '[已隐藏]';
    if (value === null || typeof value === 'boolean' || typeof value === 'number') return value;
    if (typeof value === 'string') {
      return value.length > MAX_STR ? value.slice(0, MAX_STR) + '…[已截断，共 ' + value.length + ' 字符]' : value;
    }
    if (d >= MAX_DEPTH) return '[嵌套过深，已省略]';
    if (Array.isArray(value)) {
      if (isNumberArray(value)) return '[已隐藏 ' + value.length + ' 个数值]';
      var arr = value.slice(0, MAX_ITEMS).map(function (x) { return redact(x, d + 1); });
      if (value.length > MAX_ITEMS) arr.push('[另有 ' + (value.length - MAX_ITEMS) + ' 项未显示]');
      return arr;
    }
    if (isObj(value)) {
      var out = Object.create(null), keys = Object.keys(value);
      keys.slice(0, MAX_ITEMS).forEach(function (k) {
        if ((k === 'path') && typeof value[k] === 'string') { out[k] = '[路径已隐藏]'; return; }
        out[k] = redact(value[k], d + 1, k);
      });
      if (keys.length > MAX_ITEMS) out['…'] = '[另有 ' + (keys.length - MAX_ITEMS) + ' 个字段未显示]';
      return out;
    }
    return '[不支持的值]';
  }
  function stableJson(value, indent) {
    function sort(v) {
      if (Array.isArray(v)) return v.map(sort);
      if (isObj(v)) {
        var o = Object.create(null);
        Object.keys(v).sort().forEach(function (k) { o[k] = sort(v[k]); });
        return o;
      }
      return v;
    }
    return JSON.stringify(sort(value), null, indent);
  }
  function preview(v) {
    var s = typeof v === 'string' ? v : stableJson(v);
    if (s === undefined) s = '（无）';
    return s.length > 300 ? s.slice(0, 300) + '…' : s;
  }
  /** 两个版本 payload 的顶层字段差异（均先脱敏，所以不会泄露被隐藏的值）。 */
  function payloadDiff(before, after) {
    var a = isObj(before) ? before : {}, b = isObj(after) ? after : {};
    var keys = Object.keys(a).concat(Object.keys(b).filter(function (k) { return !has(a, k); })).sort();
    var out = [];
    keys.forEach(function (k) {
      var ra = has(a, k) ? redact(a[k], 0, k) : undefined;
      var rb = has(b, k) ? redact(b[k], 0, k) : undefined;
      if (!has(a, k)) out.push({ key: k, change: 'added', before: null, after: preview(rb) });
      else if (!has(b, k)) out.push({ key: k, change: 'removed', before: preview(ra), after: null });
      else if (stableJson(ra) !== stableJson(rb) || (SENSITIVE_KEY.test(k) && stableJson(a[k]) !== stableJson(b[k]))) {
        out.push({ key: k, change: 'changed', before: preview(ra), after: preview(rb) });
      }
    });
    return out;
  }
  /** 纯文本（转写、规则指令）的单行摘要：只折叠空白并截断，绝不删除或改写任何字符。 */
  function clip(text, max) {
    if (typeof text !== 'string') return '';
    var t = text.replace(/\s+/g, ' ').trim();
    return max && t.length > max ? t.slice(0, max) + '…' : t;
  }
  /** Markdown 的纯文本摘要（仅用于活动标题/产物正文这类确实是 Markdown 的字段）。 */
  function plainText(md, max) {
    if (typeof md !== 'string') return '';
    var t = md.replace(/```[\s\S]*?```/g, ' ').replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/[`*_~>#]/g, '').replace(/\s+/g, ' ').trim();
    return max && t.length > max ? t.slice(0, max) + '…' : t;
  }

  // ---------------------------------------------------------------- 各类型的展示描述

  function F(label, kind, value, extra) {
    var f = { label: label, kind: kind, value: value };
    if (extra) Object.keys(extra).forEach(function (k) { f[k] = extra[k]; });
    return f;
  }
  function ref(label, type, id, version) { return { label: label, type: type, id: id, version: version || null }; }
  function nonEmpty(s) { return typeof s === 'string' && s.trim() !== ''; }

  /**
   * 把规范化记录描述为 {headline, fields, markdown, refs, warnings}，供 DOM 层原样渲染。
   * 字段缺失 → 不出现（或显示“未提供”），畸形 → 记入 warnings。
   */
  function describeRecord(n) {
    var p = n.payload;
    var out = { headline: typeLabel(n.type), fields: [], markdown: [], refs: [], warnings: [] };
    function warn(msg) { out.warnings.push(msg); }
    function evidence(list, label) {
      var ids = idList(list);
      if (Array.isArray(list) && ids.length !== list.length) warn(label + '里有 ' + (list.length - ids.length) + ' 个格式无效的 ID，已忽略。');
      ids.forEach(function (id) { out.refs.push(ref(label, null, id)); });
      return ids.length;
    }
    switch (n.type) {
      case 'person': {
        var name = nonEmpty(p.name) ? p.name : null;
        out.headline = name || (isInt(p.display_number) ? '未命名人物 #' + p.display_number : '未命名人物');
        out.fields.push(F('名称', name ? 'text' : 'missing', name));
        if (isInt(p.display_number)) out.fields.push(F('编号', 'text', '#' + p.display_number));
        var refs = idList(p.references);
        out.fields.push(F('参考声纹数', 'text', String(refs.length)));
        refs.forEach(function (id) { out.refs.push(ref('参考声纹', 'voiceprint', id)); });
        break;
      }
      case 'place': {
        var pn = nonEmpty(p.name) ? p.name : null;
        out.headline = pn || (p.is_unknown === true ? '未知地点' : '未命名地点');
        out.fields.push(F('名称', pn ? 'text' : 'missing', pn));
        if (typeof p.is_unknown === 'boolean') out.fields.push(F('是否“未知地点”', 'text', p.is_unknown ? '是（兜底地点，无参考）' : '否'));
        var gps = idList(p.references);
        out.fields.push(F('参考 GPS 点数', 'text', String(gps.length)));
        gps.forEach(function (id) { out.refs.push(ref('参考 GPS 点', 'gps_point', id)); });
        break;
      }
      case 'attribution': {
        var st = str(p.subject_type, 32), tt = str(p.target_type, 32);
        out.headline = '归属：' + typeLabel(st) + ' → ' + typeLabel(tt);
        if (isId(p.subject_id)) out.refs.push(ref('归属主体', has(TYPES, st) ? st : null, p.subject_id)); else warn('缺少有效的 subject_id。');
        if (isId(p.target_id)) out.refs.push(ref('归属目标', has(TYPES, tt) ? tt : null, p.target_id)); else warn('缺少有效的 target_id。');
        if (typeof p.user_protected === 'boolean') out.fields.push(F('用户确认保护', 'text', p.user_protected ? '是（用户纠正或确认过，重判不得改变）' : '否'));
        else out.fields.push(F('用户确认保护', 'text', '未知（旧数据未记录）'));
        break;
      }
      case 'activity': {
        out.headline = plainText(p.title_md, 80) || '（无标题活动）';
        if (typeof p.title_md === 'string') out.markdown.push({ label: '标题', text: p.title_md, inline: true });
        if (typeof p.description_md === 'string' && p.description_md !== '') out.markdown.push({ label: '描述', text: p.description_md });
        var s = parseUtc(n.start), e = parseUtc(n.end);
        if (s !== null && e !== null) out.fields.push(F('时长', 'text', formatDuration(e - s) || '—'));
        out.fields.push(F('证据条数', 'text', String(evidence(p.evidence_ids, '证据'))));
        break;
      }
      case 'artifact': {
        out.headline = plainText(p.body_md, 60) || '（空产物）';
        if (typeof p.body_md === 'string') out.markdown.push({ label: '正文', text: p.body_md });
        else warn('产物没有 Markdown 正文。');
        out.fields.push(F('来源', 'text', labelOf(SOURCE_LABELS, p.manual === true ? 'manual' : p.source, '未记录')));
        if (isInt(p.rule_version)) out.fields.push(F('生成时的规则版本', 'text', 'v' + p.rule_version));
        if (nonEmpty(p.action_reason)) out.fields.push(F('动作理由', 'text', p.action_reason));
        if (nonEmpty(p.run_id)) out.fields.push(F('提取运行', 'code', str(p.run_id, 80)));
        if (isId(p.activity_id)) out.refs.push(ref('所属活动', 'activity', p.activity_id));
        if (isId(p.rule_id)) out.refs.push(ref('生成所用规则', 'rule', p.rule_id, isInt(p.rule_version) ? p.rule_version : null));
        evidence(p.evidence_ids, '证据');
        break;
      }
      case 'action_suggestion': {
        var a = isObj(p.action) ? p.action : null;
        out.headline = a && nonEmpty(a.title) ? a.title : '（无标题动作）';
        out.fields.push(F('状态', 'badge', labelOf(STATE_LABELS, p.state, '未知状态'), { code: str(p.state, 32), tone: stateTone(p.state) }));
        if (a) {
          out.fields.push(F('动作类型', 'text', labelOf(ACTION_KIND_LABELS, a.kind, '未知类型')));
          if (typeof a.start === 'string') out.fields.push(F('开始', 'time', a.start));
          if (typeof a.end === 'string') out.fields.push(F('结束', 'time', a.end));
          if (typeof a.due_at === 'string') out.fields.push(F('截止', 'time', a.due_at));
          if (nonEmpty(a.notes)) out.fields.push(F('备注', 'text', a.notes));
        } else warn('建议里没有有效的 action。');
        if (typeof p.receipt === 'string') out.fields.push(F('执行回执', 'text', '已记录（内容已隐藏）'));
        if (isObj(p.execution)) {
          var ex = p.execution, bits = [];
          if (isInt(ex.attempt)) bits.push('第 ' + ex.attempt + ' 次尝试');
          if (isInt(ex.approved_version)) bits.push('批准版本 v' + ex.approved_version);
          out.fields.push(F('执行记录', 'text', '存在（' + (bits.join('，') || '细节已隐藏') + '）'));
        }
        if (isId(p.artifact_id)) out.refs.push(ref('来源产物', 'artifact', p.artifact_id, isInt(p.artifact_version) ? p.artifact_version : null));
        break;
      }
      case 'rule': {
        out.headline = clip(p.instruction, 40) || '（空规则）';
        out.fields.push(F('适用范围', 'text', nonEmpty(p.scope) ? p.scope : null));
        out.fields.push(F('提取指令', 'text', nonEmpty(p.instruction) ? p.instruction : null));
        out.fields.push(F('可选动作', 'text', p.action === null || p.action === undefined ? '无' : labelOf(ACTION_KIND_LABELS, p.action, '未知')));
        out.fields.push(F('启用', 'text', p.enabled === false ? '已停用' : p.enabled === true ? '启用中' : '未记录'));
        if (isId(p.activity_id)) { out.fields.push(F('仅限单个活动（“只改这一次”）', 'text', '是')); out.refs.push(ref('限定的活动', 'activity', p.activity_id)); }
        break;
      }
      case 'rule_selection': {
        out.headline = '规则选择';
        var ids = idList(p.rule_ids);
        out.fields.push(F('选中的规则数', 'text', String(ids.length)));
        if (isId(p.activity_id)) out.refs.push(ref('所属活动', 'activity', p.activity_id)); else warn('缺少有效的 activity_id。');
        ids.forEach(function (id) { out.refs.push(ref('选中的规则', 'rule', id)); });
        break;
      }
      case 'transcript_segment': {
        out.headline = clip(p.text, 60) || '（空转写段）';
        out.fields.push(F('文本', 'text', typeof p.text === 'string' ? p.text : null));
        if (nonEmpty(p.language)) out.fields.push(F('语言', 'text', p.language));
        if (typeof p.overlap === 'boolean') out.fields.push(F('与他人说话重叠', 'text', p.overlap ? '是' : '否'));
        if (isId(p.voiceprint_id)) out.refs.push(ref('声纹', 'voiceprint', p.voiceprint_id));
        if (isId(p.local_speaker_id)) out.refs.push(ref('局部说话人', 'local_speaker', p.local_speaker_id));
        if (isId(p.audio_ref_id)) out.refs.push(ref('录音', 'audio_ref', p.audio_ref_id));
        break;
      }
      case 'voiceprint': {
        out.headline = '声纹';
        var emb = isObj(p.embedding) ? p.embedding : null;
        var dim = emb && Array.isArray(emb.values) ? emb.values.length : null;
        out.fields.push(F('声纹向量', 'text', dim === null ? '未提供' : '已隐藏（' + dim + ' 维，属生物特征，不在页面显示）'));
        if (typeof p.sample_quality === 'number') out.fields.push(F('样本质量', 'text', String(p.sample_quality)));
        if (nonEmpty(p.model_version)) out.fields.push(F('模型版本', 'code', p.model_version));
        if (isId(p.local_speaker_id)) out.refs.push(ref('局部说话人', 'local_speaker', p.local_speaker_id));
        if (isId(p.audio_ref_id)) out.refs.push(ref('录音', 'audio_ref', p.audio_ref_id));
        idList(p.transcript_segment_ids).forEach(function (id) { out.refs.push(ref('转写段', 'transcript_segment', id)); });
        break;
      }
      case 'local_speaker': {
        out.headline = nonEmpty(p.label) ? p.label : '局部说话人';
        if (nonEmpty(p.label)) out.fields.push(F('标签', 'text', p.label));
        if (isId(p.audio_ref_id)) out.refs.push(ref('录音', 'audio_ref', p.audio_ref_id));
        break;
      }
      case 'audio_ref': {
        out.headline = '录音引用';
        if (nonEmpty(p.media_type)) out.fields.push(F('媒体类型', 'text', p.media_type));
        var fr = isObj(p.file_ref) ? p.file_ref : (isObj(p.file) ? p.file : null);
        if (fr) {
          if (isInt(fr.size)) out.fields.push(F('文件大小', 'text', formatBytes(fr.size)));
          if (typeof fr.sha256 === 'string') out.fields.push(F('SHA-256（前 12 位）', 'code', fr.sha256.slice(0, 12)));
          out.fields.push(F('文件路径', 'text', '已隐藏；本页不提供音频下载或播放'));
        }
        var as = parseUtc(n.start), ae = parseUtc(n.end);
        if (as !== null && ae !== null) out.fields.push(F('录音时长', 'text', formatDuration(ae - as) || '—'));
        break;
      }
      case 'ambient_sound': {
        out.headline = nonEmpty(p.label) ? p.label : '环境声';
        if (nonEmpty(p.label)) out.fields.push(F('标签', 'text', p.label));
        if (nonEmpty(p.taxonomy)) out.fields.push(F('分类体系', 'text', p.taxonomy));
        if (isId(p.audio_ref_id)) out.refs.push(ref('录音', 'audio_ref', p.audio_ref_id));
        break;
      }
      case 'gps_point': {
        var lat = p.latitude, lon = p.longitude;
        var okc = typeof lat === 'number' && typeof lon === 'number' && isFinite(lat) && isFinite(lon);
        out.headline = okc ? lat.toFixed(5) + ', ' + lon.toFixed(5) : '（坐标缺失）';
        if (okc) out.fields.push(F('纬度 / 经度', 'text', lat + ', ' + lon));
        else warn('坐标缺失或格式异常。');
        if (typeof p.accuracy_m === 'number') out.fields.push(F('精度', 'text', '± ' + p.accuracy_m + ' 米'));
        break;
      }
      case 'gps_geocode': {
        out.headline = nonEmpty(p.formatted_address) ? p.formatted_address : '逆地理编码';
        out.fields.push(F('状态', 'text', p.status === 'ok' ? '成功' : p.status === 'no_result' ? '无结果' : p.status === 'failed' ? '失败' : '未知'));
        if (nonEmpty(p.provider)) out.fields.push(F('服务商', 'text', p.provider));
        ['country', 'province', 'city', 'district', 'township', 'street', 'building_name'].forEach(function (k) {
          var lbl = { country: '国家', province: '省', city: '市', district: '区', township: '乡镇', street: '街道', building_name: '建筑' }[k];
          if (nonEmpty(p[k])) out.fields.push(F(lbl, 'text', p[k]));
        });
        if (isObj(p.query_coordinate) && typeof p.query_coordinate.latitude === 'number') {
          out.fields.push(F('查询坐标', 'text', p.query_coordinate.latitude + ', ' + p.query_coordinate.longitude + '（' + str(p.query_coordinate.crs, 16) + '）'));
        }
        if (isId(p.gps_point_id)) out.refs.push(ref('对应 GPS 点', 'gps_point', p.gps_point_id));
        break;
      }
      case 'phone_info': {
        out.headline = p.kind === 'event' ? '手机事件' : p.kind === 'snapshot' ? '手机状态快照' : '手机信息';
        if (nonEmpty(p.kind)) out.fields.push(F('类别', 'text', p.kind));
        if (nonEmpty(p.event_type)) out.fields.push(F('事件类型', 'text', p.event_type));
        if (nonEmpty(p.state)) out.fields.push(F('状态', 'text', p.state));
        break;
      }
      case 'feedback': {
        var cmd = isObj(p.command) ? p.command : null;
        out.headline = '反馈：' + (nonEmpty(p.module) ? p.module : '未知模块') + (cmd && nonEmpty(cmd.kind) ? ' / ' + cmd.kind : '');
        if (nonEmpty(p.module)) out.fields.push(F('目标模块', 'text', p.module));
        if (cmd && nonEmpty(cmd.kind)) out.fields.push(F('命令', 'code', cmd.kind));
        if (cmd && isId(cmd.target_id)) out.refs.push(ref('命令目标', null, cmd.target_id));
        break;
      }
      default:
        out.headline = typeLabel(n.type);
    }
    return out;
  }
  function stateTone(state) {
    return state === 'executed' || state === 'approved' ? 'ok'
      : state === 'failed' || state === 'rejected' ? 'bad'
      : state === 'unknown' ? 'warn'
      : state === 'executing' ? 'info' : 'muted';
  }
  function formatBytes(n) {
    if (!isInt(n) || n < 0) return null;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KiB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MiB';
    return (n / 1073741824).toFixed(2) + ' GiB';
  }

  // ---------------------------------------------------------------- 路由（只含视图、类型、ID、版本；绝不含令牌）

  function viewIds() { return VIEWS.map(function (v) { return v.id; }); }
  function parseHash(hash) {
    var bad = { view: 'overview', type: null, id: null, version: null, valid: false };
    var h = typeof hash === 'string' ? hash : '';
    if (h === '' || h === '#' || h === '#/') return { view: 'overview', type: null, id: null, version: null, valid: true };
    if (h.length > 160 || h.indexOf('#/') !== 0) return bad;
    var seg = h.slice(2).split('/');
    var view = seg[0];
    if (viewIds().indexOf(view) < 0) return bad;
    var r = { view: view, type: null, id: null, version: null, valid: true };
    var rest = seg.slice(1);
    if (rest.length && rest[rest.length - 1] === '') rest.pop(); // 允许结尾斜杠
    if (view === 'trace' && rest.length && has(TYPES, rest[0])) { r.type = rest[0]; rest = rest.slice(1); }
    if (rest.length) {
      if (!isId(rest[0])) return bad;
      if (view !== 'trace' && !has(VIEW_TYPE, view)) return bad;
      r.id = rest[0];
      rest = rest.slice(1);
    }
    if (rest.length) {
      if (!r.id || rest.length !== 2 || rest[0] !== 'v' || !/^[1-9][0-9]{0,9}$/.test(rest[1]) || +rest[1] > 2147483647) return bad;
      r.version = +rest[1];
    }
    return r;
  }
  function formatHash(route) {
    var r = route || {};
    var s = '#/' + (viewIds().indexOf(r.view) >= 0 ? r.view : 'overview');
    if (r.view === 'trace' && r.type && has(TYPES, r.type)) s += '/' + r.type;
    if (isId(r.id)) {
      s += '/' + r.id;
      if (isInt(r.version) && r.version >= 1) s += '/v/' + r.version;
    }
    return s;
  }
  /** 指向某条记录的站内链接：有专属页面的类型去专属页，否则去“追溯”页；类型未知时让追溯页自动识别。 */
  function recordHash(type, id, version) {
    if (!isId(id)) return null;
    var tail = isInt(version) && version >= 1 ? '/v/' + version : '';
    if (!has(TYPES, type)) return '#/trace/' + id + tail;
    var v = TYPES[type].view;
    return v ? '#/' + v + '/' + id + tail : '#/trace/' + type + '/' + id + tail;
  }

  // ---------------------------------------------------------------- 时间线与地理示意（纯数学）

  /** 把区间贪心分配到泳道；返回 0..1 的横向比例，超出窗口的只计数。 */
  function layoutTimeline(items, startMs, endMs, minWidth) {
    var span = endMs - startMs;
    var min = typeof minWidth === 'number' ? minWidth : 0.004;
    var bars = [], outside = 0, laneEnds = [];
    if (!(span > 0)) return { bars: bars, lanes: 0, outside: items.length };
    var list = items.map(function (it) {
      return { id: it.id, s: parseUtc(it.start), e: parseUtc(it.end), ref: it };
    }).filter(function (x) { return x.s !== null && x.e !== null; });
    outside += items.length - list.length;
    list.sort(function (a, b) { return a.s - b.s || a.e - b.e; });
    list.forEach(function (x) {
      if (x.e < startMs || x.s >= endMs) { outside++; return; }
      var s = Math.max(x.s, startMs), e = Math.min(Math.max(x.e, x.s), endMs);
      var fx = (s - startMs) / span, fw = Math.max((e - s) / span, min);
      if (fx + fw > 1) fx = Math.max(0, 1 - fw);
      var lane = 0;
      while (lane < laneEnds.length && laneEnds[lane] > fx + 1e-9) lane++;
      laneEnds[lane] = fx + fw;
      bars.push({ id: x.id, x: fx, w: fw, lane: lane, clippedStart: x.s < startMs, clippedEnd: x.e > endMs, ref: x.ref });
    });
    return { bars: bars, lanes: laneEnds.length, outside: outside };
  }
  function hourTicks(startMs, endMs, zone) {
    var span = endMs - startMs;
    if (!(span > 0)) return [];
    var steps = [1, 2, 3, 6, 12, 24, 48, 168], step = 168;
    for (var i = 0; i < steps.length; i++) {
      if (span / (steps[i] * 3600000) <= 12) { step = steps[i]; break; }
    }
    var off = zone.offsetAt(startMs) * 60000;
    var unit = step * 3600000;
    var tick = Math.ceil((startMs + off) / unit) * unit - off;
    var ticks = [];
    while (tick <= endMs && ticks.length < 40) {
      var d = shifted(tick, zone);
      ticks.push({
        x: (tick - startMs) / span,
        label: step < 24 ? pad(d.getUTCHours()) + ':00' : pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate())
      });
      tick += unit;
    }
    return ticks;
  }
  /** 把经纬度等距投影到 size×size 的画布（仅相对位置示意，不是地图）。 */
  function projectPoints(points, size, pad_) {
    var sz = size || 300, pd = typeof pad_ === 'number' ? pad_ : 20;
    var valid = points.filter(function (p) {
      return typeof p.lat === 'number' && typeof p.lon === 'number' && isFinite(p.lat) && isFinite(p.lon) &&
        Math.abs(p.lat) <= 90 && Math.abs(p.lon) <= 180;
    });
    if (!valid.length) return { points: [], spanMeters: 0, skipped: points.length };
    var minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
    valid.forEach(function (p) {
      minLat = Math.min(minLat, p.lat); maxLat = Math.max(maxLat, p.lat);
      minLon = Math.min(minLon, p.lon); maxLon = Math.max(maxLon, p.lon);
    });
    var kx = Math.cos(((minLat + maxLat) / 2) * Math.PI / 180);
    var spanX = (maxLon - minLon) * kx, spanY = maxLat - minLat;
    var span = Math.max(spanX, spanY);
    var inner = sz - 2 * pd;
    var scale = span > 1e-9 ? inner / span : 0;
    var offX = pd + (inner - spanX * scale) / 2, offY = pd + (inner - spanY * scale) / 2;
    return {
      points: valid.map(function (p) {
        return {
          id: p.id,
          x: scale ? offX + (p.lon - minLon) * kx * scale : sz / 2,
          y: scale ? sz - (offY + (p.lat - minLat) * scale) : sz / 2
        };
      }),
      spanMeters: Math.round(span * 111320),
      skipped: points.length - valid.length
    };
  }

  // ---------------------------------------------------------------- 其他响应的读取

  function readEvidence(data) {
    if (!isObj(data) || !Array.isArray(data.items)) return { ok: false };
    var items = [], dropped = 0;
    data.items.forEach(function (it) {
      if (!isObj(it) || !isId(it.transcript_segment_id) || !isInt(it.transcript_segment_version) ||
          parseUtc(it.start) === null || parseUtc(it.end) === null || typeof it.text !== 'string') { dropped++; return; }
      var pb = isObj(it.playback) && isId(it.playback.audio_ref_id) && typeof it.playback.start_seconds === 'number' &&
        typeof it.playback.end_seconds === 'number' ? it.playback : null;
      var at = isObj(it.attribution) && isId(it.attribution.person_id) ? it.attribution : null;
      items.push({
        id: it.transcript_segment_id,
        version: it.transcript_segment_version,
        start: it.start,
        end: it.end,
        text: it.text.slice(0, 600),
        localSpeakerId: isId(it.local_speaker_id) ? it.local_speaker_id : null,
        voiceprintId: isId(it.voiceprint_id) ? it.voiceprint_id : null,
        audioRefId: isId(it.audio_ref_id) ? it.audio_ref_id : null,
        playback: pb ? { audioRefId: pb.audio_ref_id, startSeconds: pb.start_seconds, endSeconds: pb.end_seconds } : null,
        playbackStatus: str(it.playback_status, 32),
        attributionStatus: str(it.attribution_status, 32),
        attribution: at ? {
          id: isId(at.id) ? at.id : null,
          personId: at.person_id,
          personVersion: isInt(at.person_version) ? at.person_version : null,
          personStatus: str(at.person_status, 32),
          name: typeof at.name === 'string' ? at.name.slice(0, 256) : null
        } : null
      });
    });
    if (data.next_cursor !== null && !validCursor(data.next_cursor)) return { ok: false };
    var rep = isObj(data.report) ? data.report : {};
    return {
      ok: true,
      items: items,
      dropped: dropped,
      nextCursor: validCursor(data.next_cursor) ? data.next_cursor : null,
      truncated: data.truncated === true,
      scanned: isInt(data.scanned) ? data.scanned : null,
      consistency: str(data.consistency, 32),
      report: {
        stopReason: str(rep.stop_reason, 32),
        continuation: str(rep.continuation, 32),
        pages: isInt(rep.pages) ? rep.pages : null,
        calls: isInt(rep.calls) ? rep.calls : null,
        readRows: isInt(rep.read_rows) ? rep.read_rows : null
      }
    };
  }

  var STATUS_LABELS = {
    suggestion_id: '建议 ID', suggestion_version: '建议版本', suggestion_state: '建议状态', suggestion_status: '版本状态',
    executor_state: '本地执行状态', journal_revision: '日志修订号', execution_enabled: '外部执行已启用',
    verification_enabled: '只读查证可用', recovery_protocol: '恢复协议', blocker: '阻塞原因',
    expected_version: '预期建议版本', expected_revision: '预期日志修订号', status_scope: '状态范围',
    feedback_id: '反馈 ID', status: '状态', completion_source: '结果来源', integration: '集成方式', run_id: '提取运行 ID',
    business_status: '业务状态'
  };
  /** 只读取标量字段做“键值表”；嵌套对象不展开（避免泄露执行键、哈希、外部标识）。 */
  function readStatus(data) {
    if (!isObj(data)) return { ok: false };
    var rows = [];
    Object.keys(data).slice(0, 40).forEach(function (k) {
      var v = data[k];
      if (SENSITIVE_KEY.test(k)) return;
      if (v === null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
        var text = v === null ? '（空）' : typeof v === 'boolean' ? (v ? '是' : '否') : String(v).slice(0, 300);
        if (k === 'suggestion_state' || k === 'status') text = labelOf(STATE_LABELS, v, labelOf(FEEDBACK_STATUS_LABELS, v, text));
        rows.push({ key: k, label: labelOf(STATUS_LABELS, k, k), text: text });
      }
    });
    return {
      ok: true,
      rows: rows,
      executionEnabled: typeof data.execution_enabled === 'boolean' ? data.execution_enabled : null,
      nested: Object.keys(data).filter(function (k) { return isObj(data[k]) || Array.isArray(data[k]); }).length
    };
  }
  /** 概览探测：用 limit=1 的一页判断“有数据 / 无数据 / 无授权 / 读取失败”，不计数、不编造总数。 */
  function classifyProbe(resp, page) {
    if (resp && resp.ok && page && page.ok) {
      if (!page.items.length) return page.nextCursor || page.dropped
        ? { state: 'error', message: '当前页无有效条目，但还有游标或异常记录；不能判断为没有数据。' }
        : { state: 'empty' };
      var first = page.items[0];
      return { state: 'has', earliest: first.start, more: page.nextCursor !== null, firstId: first.id };
    }
    var e = resp && resp.error ? resp.error : { kind: 'unknown' };
    if (e.kind === 'forbidden' || e.kind === 'notfound') return { state: 'denied', message: e.message };
    if (resp && resp.ok) return { state: 'error', message: '响应格式异常。' };
    return { state: 'error', message: e.message };
  }

  return {
    ID_RE: ID_RE, EVIDENCE_PATH: EVIDENCE_PATH, MAX_EVIDENCE_WINDOW_MS: MAX_EVIDENCE_WINDOW_MS,
    TYPES: TYPES, TYPE_ORDER: TYPE_ORDER, KIND_LABELS: KIND_LABELS, VIEWS: VIEWS, VIEW_TYPE: VIEW_TYPE,
    STATE_LABELS: STATE_LABELS, SOURCE_LABELS: SOURCE_LABELS, ACTION_KIND_LABELS: ACTION_KIND_LABELS,
    FEEDBACK_STATUS_LABELS: FEEDBACK_STATUS_LABELS, ATTRIBUTION_STATUS_LABELS: ATTRIBUTION_STATUS_LABELS,
    PLAYBACK_STATUS_LABELS: PLAYBACK_STATUS_LABELS, STOP_REASON_LABELS: STOP_REASON_LABELS,
    isObj: isObj, isId: isId, isInt: isInt, str: str, idList: idList, typeLabel: typeLabel, labelOf: labelOf, shortId: shortId,
    parseUtc: parseUtc, toIso: toIso, makeZone: makeZone, formatClock: formatClock, formatTimeOnly: formatTimeOnly,
    formatDuration: formatDuration, formatOffsetSeconds: formatOffsetSeconds, formatBytes: formatBytes,
    localToUtcIso: localToUtcIso, utcToLocalInput: utcToLocalInput, dateOf: dateOf, shiftDay: shiftDay,
    dayBounds: dayBounds, resolveRange: resolveRange,
    listPath: listPath, recordPath: recordPath, historyPath: historyPath, feedbackStatusPath: feedbackStatusPath,
    personPreviewPath: personPreviewPath, executionPath: executionPath, evidencePath: evidencePath,
    evidenceWindow: evidenceWindow, validCursor: validCursor,
    describeError: describeError,
    normalizeRecord: normalizeRecord, readPage: readPage, readHistory: readHistory, mergeItems: mergeItems,
    redact: redact, stableJson: stableJson, payloadDiff: payloadDiff, plainText: plainText, clip: clip, describeRecord: describeRecord,
    stateTone: stateTone,
    parseHash: parseHash, formatHash: formatHash, recordHash: recordHash,
    layoutTimeline: layoutTimeline, hourTicks: hourTicks, projectPoints: projectPoints,
    readEvidence: readEvidence, readStatus: readStatus, classifyProbe: classifyProbe
  };
});

});

// ===== MD =====
define('./ws-md.js', function (module, require) {
/*
 * Ear 业务工作台：安全的 Markdown 渲染（纯 DOM 构造，不解析 HTML）。
 *
 * 产物正文、活动描述来自大模型，按“不可信文本”处理：
 *  - 只用 createElement / createTextNode / textContent 构建 DOM，从不使用 innerHTML；
 *  - 原始 HTML 标签原样显示为文字，不会被解析；
 *  - 链接不可点击：只显示链接文字和目标地址（避免点击即外泄、避免 javascript: 等协议）；
 *  - 图片不加载：显示“已屏蔽”占位和地址文字，绝不发出任何远程请求；
 *  - 只设置固定的 class、aria-*、role、start、scope 属性，从不设置 href/src/style/on*；
 *  - 长度、块数、嵌套层数和节点数都有上限，超限时明示“已截断”。
 *
 * 浏览器里挂在 self.EarWsMd；Node 测试用 require 加载。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module !== null && module.exports) {
    module.exports = api;
  } else {
    root.EarWsMd = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var LIMITS = { maxChars: 200000, maxBlocks: 3000, maxDepth: 6, maxInlineDepth: 5, maxNodes: 30000, maxUrl: 300 };
  var ESCAPABLE = '\\`*_{}[]()#+-.!|~<>"\'&';
  var ENTITIES = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'", '&#39;': "'", '&nbsp;': '\u00a0' };

  function decodeEntities(s) {
    return s.replace(/&(?:amp|lt|gt|quot|apos|nbsp|#39);/g, function (m) { return ENTITIES[m]; });
  }
  function isSpace(c) { return c === undefined || c === ' ' || c === '\t' || c === '\n'; }
  function isWordChar(c) { return c !== undefined && /[A-Za-z0-9]/.test(c); }

  // ---------------------------------------------------------------- 行内

  function countRun(s, i, ch) { var k = i; while (s[k] === ch) k++; return k - i; }
  function findRun(s, from, ch, len) {
    var k = from;
    while (k < s.length) {
      if (s[k] === ch) {
        var r = countRun(s, k, ch);
        if (r === len) return k;
        k += r;
      } else k++;
    }
    return -1;
  }
  function backslashes(s, i) { var c = 0; while (i - 1 - c >= 0 && s[i - 1 - c] === '\\') c++; return c; }

  function findClose(s, from, delim) {
    var k = from;
    var ch = delim[0];
    while (k < s.length) {
      var c = s[k];
      if (c === '`') { var r = countRun(s, k, '`'); var e = findRun(s, k + r, '`', r); k = e >= 0 ? e + r : k + r; continue; }
      if (c === '\\') { k += 2; continue; }
      if (s.substr(k, delim.length) === delim) {
        var after = s[k + delim.length];
        var before = s[k - 1];
        var single = delim.length === 1;
        if (single && (after === ch || before === ch)) { k++; continue; }
        if (!isSpace(before) && !(ch === '_' && isWordChar(after))) return k;
      }
      k++;
    }
    return -1;
  }

  function parseLink(s, i) {
    // s[i] === '['
    var depth = 0, k = i, close = -1;
    while (k < s.length && k - i < 2000) {
      var c = s[k];
      if (c === '\\') { k += 2; continue; }
      if (c === '`') { var r = countRun(s, k, '`'); var e = findRun(s, k + r, '`', r); k = e >= 0 ? e + r : k + r; continue; }
      if (c === '[') depth++;
      else if (c === ']') { depth--; if (depth === 0) { close = k; break; } }
      k++;
    }
    if (close < 0 || s[close + 1] !== '(') return null;
    var p = close + 2;
    while (s[p] === ' ') p++;
    var url = '';
    if (s[p] === '<') {
      var gt = s.indexOf('>', p + 1);
      if (gt < 0 || s.slice(p + 1, gt).indexOf('\n') >= 0) return null;
      url = s.slice(p + 1, gt); p = gt + 1;
    } else {
      var paren = 0, st = p;
      while (p < s.length && p - st < 2000) {
        var ch = s[p];
        if (ch === '\\') { p += 2; continue; }
        if (ch === '(') paren++;
        else if (ch === ')') { if (paren === 0) break; paren--; }
        else if (ch === ' ' || ch === '\n') break;
        p++;
      }
      url = s.slice(st, p);
    }
    while (s[p] === ' ') p++;
    if (s[p] === '"' || s[p] === "'") {
      var q = s[p], endq = s.indexOf(q, p + 1);
      if (endq < 0) return null;
      p = endq + 1;
      while (s[p] === ' ') p++;
    }
    if (s[p] !== ')') return null;
    return { text: s.slice(i + 1, close), url: url, end: p + 1 };
  }

  function parseInline(src, depth) {
    var out = [], buf = '', i = 0, n = src.length;
    function flush() { if (buf) { out.push({ t: 'text', v: decodeEntities(buf) }); buf = ''; } }
    while (i < n) {
      var ch = src[i];
      if (ch === '\\' && i + 1 < n) {
        var nx = src[i + 1];
        if (nx === '\n') { flush(); out.push({ t: 'br' }); i += 2; continue; }
        if (ESCAPABLE.indexOf(nx) >= 0) { buf += nx; i += 2; continue; }
        buf += ch; i++; continue;
      }
      if (ch === '\n') {
        buf = buf.replace(/ +$/, '');
        flush(); out.push({ t: 'br' }); i++;
        while (src[i] === ' ') i++;
        continue;
      }
      if (ch === '`') {
        var run = countRun(src, i, '`'), close = findRun(src, i + run, '`', run);
        if (close >= 0) {
          flush();
          var code = src.slice(i + run, close).replace(/\n/g, ' ');
          if (code.length > 2 && code[0] === ' ' && code[code.length - 1] === ' ' && code.trim() !== '') code = code.slice(1, -1);
          out.push({ t: 'code', v: code });
          i = close + run; continue;
        }
        buf += src.slice(i, i + run); i += run; continue;
      }
      if (ch === '!' && src[i + 1] === '[') {
        var im = parseLink(src, i + 1);
        if (im) { flush(); out.push({ t: 'img', alt: im.text, url: im.url }); i = im.end; continue; }
      }
      if (ch === '[') {
        var lk = parseLink(src, i);
        if (lk) {
          flush();
          out.push({ t: 'link', url: lk.url, c: depth < LIMITS.maxInlineDepth ? parseInline(lk.text, depth + 1) : [{ t: 'text', v: lk.text }] });
          i = lk.end; continue;
        }
      }
      if (ch === '<') {
        var m = /^<((?:https?|mailto|ftp):[^\s<>]{1,2000})>/i.exec(src.slice(i, i + 2100));
        if (m) { flush(); out.push({ t: 'url', v: m[1] }); i += m[0].length; continue; }
      }
      if (ch === 'h' && (src.substr(i, 7) === 'http://' || src.substr(i, 8) === 'https://') && !isWordChar(src[i - 1])) {
        var um = /^https?:\/\/[^\s<>"'`]{1,2000}/.exec(src.slice(i, i + 2100));
        if (um) {
          var url = um[0].replace(/[.,;:!?)\]}，。；：！？）】」』]+$/, '');
          flush(); out.push({ t: 'url', v: url }); i += url.length; continue;
        }
      }
      if ((ch === '*' || ch === '_' || ch === '~') && depth < LIMITS.maxInlineDepth) {
        var r = countRun(src, i, ch);
        var delim = null;
        if (ch === '~') { if (r >= 2) delim = '~~'; }
        else if (r >= 3) delim = ch + ch + ch;
        else if (r === 2) delim = ch + ch;
        else delim = ch;
        var canOpen = delim !== null && !isSpace(src[i + delim.length]) && !(ch === '_' && isWordChar(src[i - 1]));
        if (canOpen) {
          var cl = findClose(src, i + delim.length, delim);
          if (cl > i + delim.length) {
            var inner = parseInline(src.slice(i + delim.length, cl), depth + 1);
            flush();
            if (delim === '~~') out.push({ t: 'del', c: inner });
            else if (delim.length === 3) out.push({ t: 'em', c: [{ t: 'strong', c: inner }] });
            else if (delim.length === 2) out.push({ t: 'strong', c: inner });
            else out.push({ t: 'em', c: inner });
            i = cl + delim.length; continue;
          }
        }
        buf += src.slice(i, i + r); i += r; continue;
      }
      buf += ch; i++;
    }
    flush();
    return out;
  }

  // ---------------------------------------------------------------- 块

  var RE = {
    fence: /^ {0,3}(`{3,}|~{3,})[ \t]*([^\s`]*)[^`]*$/,
    atx: /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/,
    hr: /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/,
    quote: /^ {0,3}>/,
    list: /^( {0,3})([-+*]|\d{1,9}[.)])(?:[ \t]+(.*)|[ \t]*)$/,
    setext: /^ {0,3}(=+|-+)[ \t]*$/,
    tableSep: /^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/
  };
  function indentOf(line) { var m = /^ */.exec(line); return m[0].length; }
  function isBlank(line) { return /^[ \t]*$/.test(line); }

  function splitRow(line) {
    var t = line.trim();
    if (t[0] === '|') t = t.slice(1);
    if (t[t.length - 1] === '|' && t[t.length - 2] !== '\\') t = t.slice(0, -1);
    var cells = [], cur = '';
    for (var i = 0; i < t.length; i++) {
      if (t[i] === '\\' && t[i + 1] === '|') { cur += '|'; i++; }
      else if (t[i] === '|') { cells.push(cur.trim()); cur = ''; }
      else cur += t[i];
    }
    cells.push(cur.trim());
    return cells;
  }
  function startsBlock(line, afterPara) {
    if (RE.fence.test(line) || RE.atx.test(line) || RE.hr.test(line) || RE.quote.test(line)) return true;
    var m = RE.list.exec(line);
    if (m) {
      // 只有“空行之后”或“-/*/+ 与 1.”才能打断段落，避免把“2024. 年”之类误判成列表。
      if (!afterPara) return true;
      if (m[3] === undefined || m[3] === '') return false;
      return /^[-+*]$/.test(m[2]) || /^1[.)]$/.test(m[2]);
    }
    return false;
  }

  function parseBlocks(lines, depth, ctx) {
    var blocks = [], i = 0;
    while (i < lines.length) {
      if (ctx.count >= LIMITS.maxBlocks) { ctx.truncated = true; break; }
      var line = lines[i], m;
      if (isBlank(line)) { i++; continue; }

      if ((m = RE.fence.exec(line))) {
        var fence = m[1], lang = /^[A-Za-z0-9_+#.-]{1,20}$/.test(m[2]) ? m[2] : '';
        var body = [], j = i + 1, closed = false;
        var indent = indentOf(line);
        while (j < lines.length) {
          var cl = lines[j].trim();
          if (cl.length >= fence.length && cl[0] === fence[0] && /^(`+|~+)$/.test(cl) && cl[0] === fence[0]) { closed = true; break; }
          body.push(lines[j].replace(new RegExp('^ {0,' + indent + '}'), ''));
          j++;
        }
        blocks.push({ t: 'code', lang: lang, text: body.join('\n') });
        ctx.count++;
        i = closed ? j + 1 : j;
        continue;
      }
      if ((m = RE.atx.exec(line))) {
        blocks.push({ t: 'h', level: m[1].length, text: (m[2] || '').trim() });
        ctx.count++; i++; continue;
      }
      if (RE.hr.test(line)) { blocks.push({ t: 'hr' }); ctx.count++; i++; continue; }

      if (RE.quote.test(line)) {
        var q = [];
        while (i < lines.length && !isBlank(lines[i]) && (RE.quote.test(lines[i]) || (q.length && !startsBlock(lines[i], true)))) {
          q.push(lines[i].replace(/^ {0,3}> ?/, ''));
          i++;
        }
        ctx.count++;
        blocks.push({ t: 'quote', c: depth < LIMITS.maxDepth ? parseBlocks(q, depth + 1, ctx) : [{ t: 'p', text: q.join('\n') }] });
        continue;
      }

      if ((m = RE.list.exec(line))) {
        var ordered = !/^[-+*]$/.test(m[2]);
        var start = ordered ? parseInt(m[2], 10) : 1;
        var items = [];
        var marker = ordered ? (m[2].slice(-1)) : m[2];
        while (i < lines.length) {
          var mm = RE.list.exec(lines[i]);
          if (!mm || (ordered ? mm[2].slice(-1) : mm[2]) !== marker || (ordered !== !/^[-+*]$/.test(mm[2])) || indentOf(lines[i]) > 3) break;
          var contentIndent = mm[1].length + mm[2].length + 1;
          var first = mm[3] === undefined ? '' : mm[3];
          var itemLines = [first];
          var k = i + 1;
          while (k < lines.length) {
            var ln = lines[k];
            if (isBlank(ln)) {
              var nx = k + 1;
              while (nx < lines.length && isBlank(lines[nx])) nx++;
              if (nx < lines.length && indentOf(lines[nx]) >= contentIndent) { itemLines.push(''); k++; continue; }
              break;
            }
            if (indentOf(ln) >= contentIndent) { itemLines.push(ln.slice(contentIndent)); k++; continue; }
            if (RE.list.test(ln) || startsBlock(ln, true)) break;
            if (!isBlank(itemLines[itemLines.length - 1])) { itemLines.push(ln.trim()); k++; continue; }
            break;
          }
          var task = null;
          var tm = /^\[([ xX])\][ \t]+/.exec(itemLines[0]);
          if (tm) { task = tm[1] !== ' '; itemLines[0] = itemLines[0].slice(tm[0].length); }
          ctx.count++;
          items.push({ task: task, c: depth < LIMITS.maxDepth ? parseBlocks(itemLines, depth + 1, ctx) : [{ t: 'p', text: itemLines.join('\n') }] });
          i = k;
          if (ctx.count >= LIMITS.maxBlocks) break;
          while (i < lines.length && isBlank(lines[i]) && i + 1 < lines.length && RE.list.test(lines[i + 1]) && !isBlank(lines[i + 1])) i++;
        }
        blocks.push({ t: 'list', ordered: ordered, start: start, items: items });
        continue;
      }

      if (line.indexOf('|') >= 0 && i + 1 < lines.length && RE.tableSep.test(lines[i + 1]) && lines[i + 1].indexOf('-') >= 0) {
        var head = splitRow(line), sep = splitRow(lines[i + 1]);
        if (head.length === sep.length && head.length <= 24) {
          var align = sep.map(function (c) { return /^:-+:$/.test(c) ? 'center' : /-:$/.test(c) ? 'right' : /^:-/.test(c) ? 'left' : ''; });
          var rows = [], r = i + 2;
          while (r < lines.length && !isBlank(lines[r]) && lines[r].indexOf('|') >= 0 && rows.length < 500) {
            var cells = splitRow(lines[r]);
            while (cells.length < head.length) cells.push('');
            rows.push(cells.slice(0, head.length));
            r++;
          }
          blocks.push({ t: 'table', head: head, align: align, rows: rows });
          ctx.count++; i = r; continue;
        }
      }

      if (indentOf(line) >= 4) {
        var code = [];
        while (i < lines.length && (indentOf(lines[i]) >= 4 || isBlank(lines[i]))) { code.push(lines[i].slice(4)); i++; }
        while (code.length && isBlank(code[code.length - 1])) code.pop();
        blocks.push({ t: 'code', lang: '', text: code.join('\n') });
        ctx.count++; continue;
      }

      // 段落（含 setext 标题）
      var para = [line.replace(/^ +/, '')];
      i++;
      while (i < lines.length && !isBlank(lines[i])) {
        var sm = RE.setext.exec(lines[i]);
        if (sm) {
          blocks.push({ t: 'h', level: sm[1][0] === '=' ? 1 : 2, text: para.join('\n') });
          ctx.count++; i++; para = null; break;
        }
        if (startsBlock(lines[i], true)) break;
        if (lines[i].indexOf('|') >= 0 && i + 1 < lines.length && RE.tableSep.test(lines[i + 1]) && lines[i + 1].indexOf('-') >= 0) break;
        para.push(lines[i].replace(/^ +/, ''));
        i++;
      }
      if (para) { blocks.push({ t: 'p', text: para.join('\n') }); ctx.count++; }
    }
    return blocks;
  }

  function parse(text) {
    var src = typeof text === 'string' ? text : '';
    var truncated = false;
    if (src.length > LIMITS.maxChars) { src = src.slice(0, LIMITS.maxChars); truncated = true; }
    src = src.replace(/\r\n?/g, '\n').replace(/\t/g, '    ').replace(/\u0000/g, '');
    var ctx = { count: 0, truncated: truncated };
    var blocks = parseBlocks(src.split('\n'), 0, ctx);
    return { blocks: blocks, truncated: ctx.truncated, totalChars: typeof text === 'string' ? text.length : 0 };
  }

  // ---------------------------------------------------------------- 渲染（只用安全的 DOM API）

  function makeCtx(doc) { return { doc: doc, nodes: 0, stats: { images: 0, links: 0, truncated: false } }; }
  function allocate(ctx) {
    if (ctx.nodes >= LIMITS.maxNodes) { ctx.stats.truncated = true; throw new RangeError('md_node_limit'); }
    ctx.nodes++;
  }
  function el(ctx, tag, cls) {
    allocate(ctx);
    var n = ctx.doc.createElement(tag);
    if (cls) n.setAttribute('class', cls);
    return n;
  }
  function txt(ctx, s) { allocate(ctx); return ctx.doc.createTextNode(s); }
  function shortUrl(u) { return u.length > LIMITS.maxUrl ? u.slice(0, LIMITS.maxUrl) + '…' : u; }

  function renderInlineNodes(ctx, nodes, parent) {
    nodes.forEach(function (n) {
      switch (n.t) {
        case 'text': parent.appendChild(txt(ctx, n.v)); break;
        case 'br': parent.appendChild(el(ctx, 'br')); break;
        case 'code': { var c = el(ctx, 'code', 'md-code-inline'); c.appendChild(txt(ctx, n.v)); parent.appendChild(c); break; }
        case 'strong': { var s = el(ctx, 'strong'); renderInlineNodes(ctx, n.c, s); parent.appendChild(s); break; }
        case 'em': { var e = el(ctx, 'em'); renderInlineNodes(ctx, n.c, e); parent.appendChild(e); break; }
        case 'del': { var d = el(ctx, 'del'); renderInlineNodes(ctx, n.c, d); parent.appendChild(d); break; }
        case 'link': {
          // 不可点击：文字 + 地址文本。
          ctx.stats.links++;
          var w = el(ctx, 'span', 'md-link');
          renderInlineNodes(ctx, n.c, w);
          parent.appendChild(w);
          var u = el(ctx, 'code', 'md-url');
          u.appendChild(txt(ctx, ' （链接未启用：' + shortUrl(n.url) + '）'));
          parent.appendChild(u);
          break;
        }
        case 'url': {
          ctx.stats.links++;
          var au = el(ctx, 'code', 'md-url');
          au.appendChild(txt(ctx, shortUrl(n.v)));
          parent.appendChild(au);
          break;
        }
        case 'img': {
          ctx.stats.images++;
          var ph = el(ctx, 'span', 'md-image-blocked');
          ph.appendChild(txt(ctx, '[图片已屏蔽，未加载' + (n.alt ? '：' + n.alt : '') + ']'));
          parent.appendChild(ph);
          if (n.url) {
            var iu = el(ctx, 'code', 'md-url');
            iu.appendChild(txt(ctx, ' ' + shortUrl(n.url)));
            parent.appendChild(iu);
          }
          break;
        }
        default: break;
      }
    });
  }
  function inlineInto(ctx, text, parent) { renderInlineNodes(ctx, parseInline(text, 0), parent); }

  function renderBlocks(ctx, blocks, parent) {
    blocks.forEach(function (b) {
      switch (b.t) {
        case 'h': {
          var h = el(ctx, 'h' + Math.min(6, b.level + 2), 'md-h');
          inlineInto(ctx, b.text, h); parent.appendChild(h); break;
        }
        case 'p': { var p = el(ctx, 'p'); inlineInto(ctx, b.text, p); parent.appendChild(p); break; }
        case 'hr': parent.appendChild(el(ctx, 'hr')); break;
        case 'code': {
          var pre = el(ctx, 'pre', 'md-code');
          var code = el(ctx, 'code');
          code.appendChild(txt(ctx, b.text));
          pre.appendChild(code);
          if (b.lang) pre.setAttribute('aria-label', '代码块（' + b.lang + '）');
          parent.appendChild(pre); break;
        }
        case 'quote': { var q = el(ctx, 'blockquote'); renderBlocks(ctx, b.c, q); parent.appendChild(q); break; }
        case 'list': {
          var list = el(ctx, b.ordered ? 'ol' : 'ul');
          if (b.ordered && b.start !== 1) list.setAttribute('start', String(b.start));
          b.items.forEach(function (it) {
            var li = el(ctx, 'li', it.task === null ? '' : 'md-task');
            if (it.task !== null) {
              var box = el(ctx, 'span', 'md-checkbox');
              box.setAttribute('role', 'img');
              box.setAttribute('aria-label', it.task ? '已完成' : '未完成');
              box.appendChild(txt(ctx, it.task ? '☑ ' : '☐ '));
              li.appendChild(box);
            }
            renderBlocks(ctx, it.c, li);
            list.appendChild(li);
          });
          parent.appendChild(list); break;
        }
        case 'table': {
          var wrap = el(ctx, 'div', 'md-table-wrap');
          var table = el(ctx, 'table', 'md-table');
          var thead = el(ctx, 'thead'), tr = el(ctx, 'tr');
          b.head.forEach(function (c, idx) {
            var th = el(ctx, 'th', b.align[idx] ? 'md-align-' + b.align[idx] : '');
            th.setAttribute('scope', 'col');
            inlineInto(ctx, c, th); tr.appendChild(th);
          });
          thead.appendChild(tr); table.appendChild(thead);
          var tbody = el(ctx, 'tbody');
          b.rows.forEach(function (row) {
            var r = el(ctx, 'tr');
            row.forEach(function (c, idx) {
              var td = el(ctx, 'td', b.align[idx] ? 'md-align-' + b.align[idx] : '');
              inlineInto(ctx, c, td); r.appendChild(td);
            });
            tbody.appendChild(r);
          });
          table.appendChild(tbody); wrap.appendChild(table); parent.appendChild(wrap); break;
        }
        default: break;
      }
    });
  }

  /** 把 Markdown 渲染为一个 div.md；返回 {node, stats}。stats 供界面提示“已屏蔽 N 张图片、N 个链接未启用”。 */
  function renderMarkdown(doc, text) {
    var ctx = makeCtx(doc);
    var box = el(ctx, 'div', 'md');
    var ast = parse(text);
    try {
      renderBlocks(ctx, ast.blocks, box);
    } catch (e) {
      if (!(e instanceof RangeError) || !ctx.stats.truncated || e.message !== 'md_node_limit') throw e;
    }
    if (ast.truncated || ctx.stats.truncated) {
      // Fixed two-node reserve, independent of the exhausted content allocator.
      var note = doc.createElement('p');
      note.setAttribute('class', 'md-truncated');
      note.appendChild(doc.createTextNode('[内容过长，已截断显示；完整文本请用“原始 JSON”查看]'));
      box.appendChild(note);
      ctx.stats.truncated = true;
    }
    return { node: box, stats: ctx.stats };
  }
  /** 行内渲染（用于活动标题等单行场景），返回 span.md-inline。 */
  function renderInline(doc, text) {
    var ctx = makeCtx(doc);
    var span = el(ctx, 'span', 'md-inline');
    try {
      inlineInto(ctx, typeof text === 'string' ? text.slice(0, 2000).replace(/\n+/g, ' ') : '', span);
    } catch (e) {
      if (!(e instanceof RangeError) || !ctx.stats.truncated || e.message !== 'md_node_limit') throw e;
    }
    return { node: span, stats: ctx.stats };
  }

  return { LIMITS: LIMITS, parse: parse, parseInline: parseInline, renderMarkdown: renderMarkdown, renderInline: renderInline };
});

});

// ===== CLIENT =====
define('./ws-client.js', function (module, require) {
/*
 * Ear 业务工作台：只读网络层。
 *
 * 约束（均有测试覆盖）：
 *  - 只有 GET：这个客户端没有 post/put/delete，页面因此不可能写入任何数据；
 *  - 令牌只存在于 createClient 的闭包变量：不写入任何存储、地址栏、日志或 DOM，只出现在 Authorization 头；
 *  - 只允许同源的 /v1/ 与 /health/ 路径，拒绝带协议、双斜杠、反斜杠、.. 或控制字符的路径；
 *  - 并发上限、超时、响应体积上限；断开后返回的响应一律标记 stale，调用方直接丢弃；
 *  - request 永不抛异常，统一返回 {ok, status, data} 或 {ok:false, error, stale, notConnected}。
 *
 * 浏览器里挂在 self.EarWsClient；Node 测试用 require 加载，并注入假的 fetch / 计时器。
 */
(function (root, factory) {
  'use strict';
  var isNode = typeof module === 'object' && module !== null && module.exports;
  var core = isNode ? require('./ws-core.js') : root.EarWsCore;
  var api = factory(core);
  if (isNode) module.exports = api; else root.EarWsClient = api;
})(typeof self !== 'undefined' ? self : this, function (core) {
  'use strict';

  var LIMITS = { requestTimeoutMs: 15000, maxResponseChars: 4000000, maxConcurrent: 4, maxPathChars: 2200 };
  var TOKEN_RE = /^[A-Za-z0-9_-]{43}$/;
  var CODE_RE = /^[a-zA-Z0-9_]{1,100}$/;

  function pathOk(path) {
    if (typeof path !== 'string' || path.length > LIMITS.maxPathChars) return false;
    if (!/^\/(v1|health)\/[A-Za-z0-9_\-/]*(\?[A-Za-z0-9_\-.~%=&]*)?$/.test(path)) return false;
    if (path.indexOf('//') >= 0 || path.indexOf('..') >= 0) return false;
    var pathname = path.split('?')[0];
    var uuid = '[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}';
    var records = new RegExp('^/v1/records/([a-z_]+)(?:/' + uuid + '(?:/history)?)?$').exec(pathname);
    if (records) return Object.prototype.hasOwnProperty.call(core.TYPES, records[1]);
    return pathname === '/v1/me' || pathname === core.EVIDENCE_PATH || pathname === '/health/ready' ||
      new RegExp('^/v1/(?:previews/person/' + uuid + '|actions/' + uuid + '/execution|feedback/' + uuid + '/status)$').test(pathname);
  }

  function createClient(opts) {
    var fetchFn = opts.fetch;
    var Abort = opts.AbortController;
    var setT = opts.setTimeout;
    var clearT = opts.clearTimeout;
    var timeoutMs = opts.timeoutMs || LIMITS.requestTimeoutMs;
    var maxConcurrent = opts.maxConcurrent || LIMITS.maxConcurrent;
    var onAuthFailure = typeof opts.onAuthFailure === 'function' ? opts.onAuthFailure : function () {};
    var token = null;
    var generation = 0;
    var authReported = false;
    var inflight = 0;
    var queue = [];
    var controllers = [];
    var cancelers = [];

    function fail(err) {
      var desc = core.describeError(err);
      return { ok: false, status: desc.status, error: desc, notConnected: desc.kind === 'not_connected', stale: false };
    }
    function stale() { return { ok: false, stale: true, status: 0, error: null, notConnected: false }; }

    function run(path, gen, resolve) {
      inflight++;
      var headers = { Accept: 'application/json' };
      if (path.indexOf('/health/') !== 0) headers.Authorization = 'Bearer ' + token;
      var ctrl = Abort ? new Abort() : null;
      var completed = false, timer;
      var init = {
        method: 'GET', headers: headers, cache: 'no-store', credentials: 'omit',
        redirect: 'error', referrerPolicy: 'no-referrer', mode: 'same-origin'
      };
      if (ctrl) { init.signal = ctrl.signal; controllers.push(ctrl); }
      function done(result) {
        if (completed) return;
        completed = true; clearT(timer);
        var cancelIndex = cancelers.indexOf(cancel);
        if (cancelIndex >= 0) cancelers.splice(cancelIndex, 1);
        if (ctrl) { var i = controllers.indexOf(ctrl); if (i >= 0) controllers.splice(i, 1); }
        inflight--;
        var out = gen !== generation ? stale() : result;
        if (!out.ok && out.error && out.error.authFailure && !authReported && gen === generation) {
          authReported = true; onAuthFailure();
        }
        resolve(out); pump();
      }
      function cancel() { if (ctrl) ctrl.abort(); done(stale()); }
      cancelers.push(cancel);
      timer = setT(function () { if (ctrl) ctrl.abort(); done(fail({ failure: 'timeout' })); }, timeoutMs);
      async function readText(res) {
        if (res.body && typeof res.body.getReader === 'function' && typeof TextDecoder !== 'undefined') {
          var reader = res.body.getReader(), decoder = new TextDecoder(), bytes = 0, text = '';
          try {
            while (true) {
              var chunk = await reader.read();
              if (completed || gen !== generation) { await reader.cancel(); return ''; }
              if (chunk.done) return text + decoder.decode();
              bytes += chunk.value.byteLength;
              if (bytes > LIMITS.maxResponseChars) { await reader.cancel(); throw new Error('response_limit'); }
              text += decoder.decode(chunk.value, { stream: true });
            }
          } finally { reader.releaseLock(); }
        }
        var text = await res.text();
        if (text.length > LIMITS.maxResponseChars) throw new Error('response_limit');
        return text;
      }
      Promise.resolve().then(function () {
        if (completed || gen !== generation) return null;
        return fetchFn(path, init);
      }).then(async function (res) {
        if (!res || completed || gen !== generation) return stale();
        // Authentication errors invalidate even when the response body is malformed or too large.
        if (res.status === 401) { if (ctrl) ctrl.abort(); return fail({ status: 401 }); }
        var text = await readText(res), data = null;
        try { data = JSON.parse(text); } catch (_) { /* Never echo error bodies. */ }
        if (res.ok) return core.isObj(data) ? { ok: true, status: res.status, data: data } : fail({ status: res.status, failure: 'parse' });
        var code = core.isObj(data) && typeof data.code === 'string' && CODE_RE.test(data.code) ? data.code : null;
        return fail({ status: res.status, code: code });
      }).catch(function (e) {
        return fail({ failure: e && e.message === 'response_limit' ? 'too_large' : 'network' });
      }).then(done);
    }

    function pump() {
      while (inflight < maxConcurrent && queue.length) {
        var job = queue.shift();
        if (job.gen !== generation) { job.resolve(stale()); continue; }
        run(job.path, job.gen, job.resolve);
      }
    }

    function invalidate() {
      token = null; generation++;
      var q = queue; queue = [];
      q.forEach(function (j) { j.resolve(stale()); });
      cancelers.slice().forEach(function (cancel) { cancel(); });
    }

    function get(path) {
      if (!pathOk(path)) return Promise.resolve(fail({ failure: 'bad_path' }));
      var isHealth = path.indexOf('/health/') === 0;
      if (!isHealth && token === null) return Promise.resolve(fail({ failure: 'not_connected' }));
      return new Promise(function (resolve) {
        queue.push({ path: path, gen: generation, resolve: resolve });
        pump();
      });
    }

    return {
      get: get,
      setToken: function (t) {
        if (typeof t !== 'string' || !TOKEN_RE.test(t)) return false;
        invalidate();
        token = t; authReported = false;
        return true;
      },
      clear: invalidate,
      connected: function () { return token !== null; },
      generation: function () { return generation; },
      inflight: function () { return inflight; },
      queued: function () { return queue.length; }
    };
  }

  return { LIMITS: LIMITS, createClient: createClient, pathOk: pathOk };
});

});

// ===== VIEWS =====
define('./ws-views.js', function (module, require) {
/*
 * Ear 业务工作台：可复用的 DOM 组件。
 *
 * 所有文字只经 textContent / createTextNode 写入；属性只能来自白名单；站内链接只能是
 * 形如 #/视图/ID 的哈希路由（不带令牌）。组件不访问网络，也不保存任何状态到存储。
 * 浏览器里挂在 self.EarWsViews；Node 测试用 require 加载，并配合假 DOM 运行。
 */
(function (root, factory) {
  'use strict';
  var isNode = typeof module === 'object' && module !== null && module.exports;
  var core = isNode ? require('./ws-core.js') : root.EarWsCore;
  var md = isNode ? require('./ws-md.js') : root.EarWsMd;
  var api = factory(core, md);
  if (isNode) module.exports = api; else root.EarWsViews = api;
})(typeof self !== 'undefined' ? self : this, function (core, md) {
  'use strict';

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const ATTR_OK = /^(class|id|role|type|for|name|value|scope|title|lang|datetime|tabindex|placeholder|maxlength|autocomplete|spellcheck|autocapitalize|min|max|step|rows|cols|colspan|rowspan|open|checked|disabled|hidden|selected|readonly|required|aria-[a-z-]+)$/;
  const SVG_ATTR_OK = /^(class|viewBox|x|y|x1|x2|y1|y2|cx|cy|r|rx|ry|width|height|role|focusable|preserveAspectRatio|text-anchor|tabindex|aria-[a-z-]+)$/;
  const BOOL = { disabled: 1, hidden: 1, checked: 1, open: 1, selected: 1, readonly: 1, required: 1 };
  const LINK_RE = /^#\/[a-z]+(?:\/[a-z_]+)?(?:\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?(?:\/v\/[1-9][0-9]{0,9})?$/;
  const TONES = ['ok', 'warn', 'bad', 'info', 'muted'];

  function createUi(doc, deps) {
    const zone = deps.zone;
    let seq = 0;

    function apply(n, props, okRe) {
      if (!props) return;
      Object.keys(props).forEach(function (key) {
        let k = key;
        const v = props[k];
        if (v === undefined || v === null || v === false) return;
        if (k === 'text') { n.textContent = String(v); return; }
        if (k === 'on') { Object.keys(v).forEach(function (ev) { n.addEventListener(ev, v[ev]); }); return; }
        if (k === 'cls') k = 'class';
        if (!okRe.test(k)) throw new Error('attribute not allowed: ' + k);
        if (BOOL[k]) { n[k] = true; return; }
        if (k === 'value') { n.value = String(v); return; }
        n.setAttribute(k, String(v));
      });
    }
    function append(n, kids) {
      if (kids === undefined || kids === null || kids === false) return;
      if (Array.isArray(kids)) { kids.forEach(function (c) { append(n, c); }); return; }
      if (typeof kids === 'string' || typeof kids === 'number') { n.appendChild(doc.createTextNode(String(kids))); return; }
      n.appendChild(kids);
    }
    function h(tag, props, kids) {
      const n = doc.createElement(tag);
      apply(n, props, ATTR_OK);
      append(n, kids);
      return n;
    }
    function svg(tag, props, kids) {
      const n = doc.createElementNS(SVG_NS, tag);
      apply(n, props, SVG_ATTR_OK);
      append(n, kids);
      return n;
    }
    function nextId(prefix) { seq++; return prefix + '-' + seq; }
    function clear(n) { n.replaceChildren(); return n; }
    function fill(n, kids) { n.replaceChildren(); append(n, kids); return n; }

    /** 站内链接：只接受 #/… 哈希路由，其余一律抛错（所以不可能由不可信文本生成链接）。 */
    function a(hash, kids, props) {
      if (typeof hash !== 'string' || !LINK_RE.test(hash)) throw new Error('internal link only');
      const n = doc.createElement('a');
      n.setAttribute('href', hash);
      apply(n, props, ATTR_OK);
      append(n, kids);
      return n;
    }
    function badge(text, tone) {
      return h('span', { class: 'badge tone-' + (TONES.indexOf(tone) >= 0 ? tone : 'muted'), text: text });
    }
    function timeEl(iso) {
      const t = core.formatClock(iso, zone());
      if (t === null) return h('span', { class: 'tone-muted', text: typeof iso === 'string' ? '时间格式异常' : '（无时间）' });
      return h('time', { datetime: iso, title: 'UTC ' + iso, text: t });
    }
    function timeSpan(start, end) {
      const s = core.parseUtc(start), e = core.parseUtc(end);
      if (s === null || e === null) return h('span', { class: 'tone-muted', text: '（无时间范围）' });
      const z = zone();
      const sText = core.formatClock(start, z), eText = core.formatClock(end, z);
      const sameDay = sText.slice(0, 10) === eText.slice(0, 10);
      const dur = core.formatDuration(e - s);
      return h('span', { class: 'time-span', title: 'UTC [' + start + ', ' + end + ')' }, [
        sText, e === s ? '' : ' – ' + (sameDay ? eText.slice(11) : eText), dur && e !== s ? '（' + dur + '）' : ''
      ]);
    }
    function code(text) { return h('code', { text: text }); }
    function chip(type, id, version, opts) {
      const hash = core.recordHash(type, id, version);
      const label = (opts && opts.label) || core.shortId(id);
      const title = (type ? core.typeLabel(type) + ' ' : '') + id + (version ? ' v' + version : '');
      if (hash === null) return h('span', { class: 'chip tone-bad', text: '（无效 ID）' });
      return a(hash, [label, version ? ' v' + version : ''], { class: 'chip', title: title });
    }

    // ------------------------------------------------------------ 状态块

    function stateBox(kind, message, opts) {
      const o = opts || {};
      const cls = 'state state-' + kind;
      if (kind === 'loading') return h('p', { class: cls, role: 'status', text: message || '加载中…' });
      if (kind === 'empty') return h('p', { class: cls, text: message });
      const box = h('div', { class: cls, role: kind === 'denied' ? 'status' : 'alert' }, [h('p', { text: message })]);
      if (o.detail) box.appendChild(h('p', { class: 'meta', text: o.detail }));
      if (o.onRetry) box.appendChild(h('button', { type: 'button', text: '重试', on: { click: o.onRetry } }));
      return box;
    }
    /** 把客户端错误渲染成合适的状态块：无授权 → denied；其余 → error。 */
    function errorBox(err, onRetry) {
      const e = err || { kind: 'unknown', message: '请求失败。' };
      const detail = (e.status ? 'HTTP ' + e.status : '') + (e.code ? (e.status ? ' · ' : '') + e.code : '');
      const kind = e.kind === 'forbidden' || e.kind === 'notfound' ? 'denied' : 'error';
      return stateBox(kind, e.message, { detail: detail || null, onRetry: e.retryable || kind === 'error' ? onRetry : null });
    }
    /** 一个带标题的区块：{node, body, loading(), empty(msg), error(err, retry), set(kids)}。 */
    function section(title, opts) {
      const o = opts || {};
      const hid = nextId('sec');
      const body = h('div', { class: 'section-body' });
      const head = h('div', { class: 'section-head' }, [h('h' + (o.level || 3), { id: hid, text: title })]);
      if (o.actions) append(head, o.actions);
      const node = h('section', { class: 'panel' + (o.cls ? ' ' + o.cls : ''), 'aria-labelledby': hid }, [head]);
      if (o.intro) node.appendChild(h('p', { class: 'meta', text: o.intro }));
      node.appendChild(body);
      return {
        node: node, body: body, head: head,
        loading: function (msg) { fill(body, stateBox('loading', msg)); },
        empty: function (msg) { fill(body, stateBox('empty', msg)); },
        error: function (err, retry) { fill(body, errorBox(err, retry)); },
        set: function (kids) { fill(body, kids); }
      };
    }

    // ------------------------------------------------------------ 事实表、引用、Markdown、JSON

    function facts(fields) {
      const dl = h('dl', { class: 'facts' });
      fields.forEach(function (f) {
        dl.appendChild(h('dt', { text: f.label }));
        let dd;
        if (f.kind === 'missing' || f.value === null || f.value === undefined) dd = h('dd', { class: 'tone-muted', text: '未提供' });
        else if (f.kind === 'code') dd = h('dd', {}, code(String(f.value)));
        else if (f.kind === 'time') dd = h('dd', {}, timeEl(f.value));
        else if (f.kind === 'badge') dd = h('dd', {}, badge(String(f.value), f.tone));
        else dd = h('dd', { class: 'pre-wrap', text: String(f.value) });
        dl.appendChild(dd);
      });
      return dl;
    }
    function refList(refs, opts) {
      if (!refs.length) return null;
      const ul = h('ul', { class: 'refs' });
      refs.forEach(function (r) {
        ul.appendChild(h('li', {}, [
          h('span', { class: 'ref-label', text: r.label + (r.type ? '（' + core.typeLabel(r.type) + '）' : '') + '：' }),
          chip(r.type, r.id, r.version),
          opts && opts.extra ? opts.extra(r) : null
        ]));
      });
      return ul;
    }
    function markdown(text) {
      const r = md.renderMarkdown(doc, text);
      const box = h('div', { class: 'md-wrap' }, r.node);
      if (r.stats.images || r.stats.links) {
        box.appendChild(h('p', { class: 'meta', text: '为保护隐私，已屏蔽 ' + r.stats.images + ' 张图片、' + r.stats.links + ' 个链接（只显示文字与地址，不会加载或跳转）。' }));
      }
      return box;
    }
    function markdownInline(text) { return md.renderInline(doc, text).node; }
    function jsonDetails(value, summary) {
      let text;
      try { text = core.stableJson(core.redact(value), 2); } catch (e) { text = '（无法序列化）'; }
      return h('details', { class: 'json' }, [
        h('summary', { text: summary || '原始 JSON（已脱敏：隐藏向量、回执、令牌、文件路径）' }),
        h('pre', { class: 'json-pre' }, h('code', { text: text }))
      ]);
    }
    function warnings(list) {
      if (!list || !list.length) return null;
      return h('ul', { class: 'warn-list' }, list.map(function (w) { return h('li', { text: w }); }));
    }

    // ------------------------------------------------------------ 分页条

    function pager(opts) {
      const o = opts || {};
      const info = h('span', { class: 'meta', role: 'status' });
      const btn = h('button', { type: 'button', text: '加载更多', hidden: true, on: { click: function () { if (o.onMore) o.onMore(); } } });
      const node = h('div', { class: 'pager' }, [info, btn]);
      return {
        node: node, button: btn, info: info,
        update: function (s) {
          const noun = o.noun || '条';
          let text = '已加载 ' + s.loaded + ' ' + noun;
          if (s.dropped) text += '（另有 ' + s.dropped + ' 条格式异常已忽略）';
          if (s.capped) text += '；已达到页面加载上限或游标未前进，请缩小范围并刷新';
          else if (s.hasMore) text += '；还有更多';
          else text += '；已到末尾（实时读取，不是快照）';
          info.textContent = text;
          btn.hidden = !(s.hasMore && !s.capped);
          btn.disabled = !!s.loading;
          btn.textContent = s.loading ? '加载中…' : '加载更多';
        }
      };
    }

    // ------------------------------------------------------------ 时间范围控件

    /**
     * state: {mode:'all'|'day'|'custom', date, start, end}（本地墙钟，按当前时区解释）。
     * kinds：允许的模式。onApply(resolved) 只在范围有效时调用；无效时显示错误，不替用户补默认值。
     */
    function rangeControls(state, o) {
      const kinds = o.kinds || ['all', 'day', 'custom'];
      const idMode = nextId('range-mode'), idDate = nextId('range-date'), idStart = nextId('range-start'), idEnd = nextId('range-end');
      const labels = { all: '全部时间', day: '单日', custom: '自定义区间' };
      const mode = h('select', { id: idMode }, kinds.map(function (k) {
        const op = h('option', { value: k, text: labels[k] });
        if (state.mode === k) op.selected = true;
        return op;
      }));
      mode.value = state.mode;
      const date = h('input', { id: idDate, type: 'date' }); date.value = state.date || '';
      const start = h('input', { id: idStart, type: 'datetime-local' }); start.value = state.start || '';
      const end = h('input', { id: idEnd, type: 'datetime-local' }); end.value = state.end || '';
      const note = h('p', { class: 'meta range-note', role: 'status' });
      const err = h('p', { class: 'tone-bad range-error', role: 'alert', hidden: true });
      const dayBox = h('div', { class: 'row range-day' }, [
        h('button', { type: 'button', text: '前一天', on: { click: function () { shift(-1); } } }),
        h('label', { for: idDate, class: 'inline', text: '日期' }), date,
        h('button', { type: 'button', text: '后一天', on: { click: function () { shift(1); } } })
      ]);
      const customBox = h('div', { class: 'row range-custom' }, [
        h('label', { for: idStart, class: 'inline', text: '开始' }), start,
        h('label', { for: idEnd, class: 'inline', text: '结束' }), end
      ]);
      const form = h('form', { class: 'range-form', 'aria-label': '时间范围', autocomplete: 'off' }, [
        h('div', { class: 'row' }, [h('label', { for: idMode, class: 'inline', text: '范围' }), mode,
          h('button', { type: 'submit', class: 'primary', text: '应用范围' })]),
        dayBox, customBox, err, note
      ]);

      function sync() {
        state.mode = mode.value;
        state.date = date.value; state.start = start.value; state.end = end.value;
        dayBox.hidden = state.mode !== 'day';
        customBox.hidden = state.mode !== 'custom';
      }
      function describe() {
        const z = zone();
        const r = core.resolveRange(state, z);
        if (!r.ok) { err.hidden = false; err.textContent = r.error; note.textContent = ''; return r; }
        err.hidden = true; err.textContent = '';
        note.textContent = r.range
          ? '时区 ' + z.label(core.parseUtc(r.range.start)) + '；查询区间（UTC，半开）：[' + r.range.start + ', ' + r.range.end + ')'
          : '不限时间：按时间升序从最早的数据开始读取。';
        return r;
      }
      function shift(n) {
        sync();
        const next = core.shiftDay(state.date, n);
        if (next === null) { describe(); return; }
        date.value = next; state.date = next;
        apply_();
      }
      function apply_() {
        sync();
        const r = describe();
        if (r.ok && o.onApply) o.onApply(r);
      }
      mode.addEventListener('change', function () { sync(); describe(); });
      [date, start, end].forEach(function (i) { i.addEventListener('change', function () { sync(); describe(); }); });
      form.addEventListener('submit', function (ev) { if (ev && ev.preventDefault) ev.preventDefault(); apply_(); });
      sync(); describe();
      return {
        node: form, mode: mode, date: date, start: start, end: end, note: note, error: err,
        read: function () { sync(); return core.resolveRange(state, zone()); },
        refresh: describe
      };
    }

    // ------------------------------------------------------------ SVG：活动时间条与 GPS 相对位置

    function timelineSvg(layout, ticks, opts) {
      const W = 1000, laneH = 24, gap = 6, top = 24;
      const H = top + Math.max(1, layout.lanes) * (laneH + gap) + 4;
      const root = svg('svg', {
        viewBox: '0 0 ' + W + ' ' + H, role: 'img', class: 'tl-svg', focusable: 'false',
        'aria-label': opts.label
      });
      ticks.forEach(function (t) {
        const x = Math.round(t.x * W * 10) / 10;
        root.appendChild(svg('line', { x1: x, x2: x, y1: top - 4, y2: H, class: 'tl-grid' }));
        const lab = svg('text', { x: x, y: 14, class: 'tl-tick', 'text-anchor': t.x > 0.97 ? 'end' : t.x < 0.03 ? 'start' : 'middle' });
        lab.textContent = t.label;
        root.appendChild(lab);
      });
      layout.bars.forEach(function (b) {
        const x = Math.round(b.x * W * 10) / 10, w = Math.max(3, Math.round(b.w * W * 10) / 10);
        const y = top + b.lane * (laneH + gap);
        const rect = svg('rect', { x: x, y: y, width: w, height: laneH, rx: 4, class: 'tl-bar' + (b.clippedStart || b.clippedEnd ? ' tl-clipped' : '') });
        const title = svg('title');
        title.textContent = opts.titleOf(b.ref);
        rect.appendChild(title);
        if (opts.onSelect) rect.addEventListener('click', function () { opts.onSelect(b.ref); });
        root.appendChild(rect);
      });
      return root;
    }
    function scatterSvg(projected, opts) {
      const S = 300;
      const root = svg('svg', { viewBox: '0 0 ' + S + ' ' + S, role: 'img', class: 'geo-svg', focusable: 'false', 'aria-label': opts.label });
      root.appendChild(svg('rect', { x: 0.5, y: 0.5, width: S - 1, height: S - 1, class: 'geo-frame' }));
      projected.points.forEach(function (p) {
        const c = svg('circle', { cx: Math.round(p.x * 10) / 10, cy: Math.round(p.y * 10) / 10, r: 5, class: 'geo-dot' });
        const t = svg('title');
        t.textContent = opts.titleOf(p.id);
        c.appendChild(t);
        root.appendChild(c);
      });
      return root;
    }

    return {
      h: h, svg: svg, a: a, append: append, fill: fill, clear: clear, nextId: nextId,
      badge: badge, timeEl: timeEl, timeSpan: timeSpan, code: code, chip: chip,
      stateBox: stateBox, errorBox: errorBox, section: section,
      facts: facts, refList: refList, markdown: markdown, markdownInline: markdownInline,
      jsonDetails: jsonDetails, warnings: warnings, pager: pager, rangeControls: rangeControls,
      timelineSvg: timelineSvg, scatterSvg: scatterSvg
    };
  }

  return { createUi: createUi, LINK_RE: LINK_RE, ATTR_OK: ATTR_OK, SVG_ATTR_OK: SVG_ATTR_OK };
});

});

// ===== PAGES =====
define('./ws-pages.js', function (module, require) {
/*
 * Ear 业务工作台：数据层与各个页面（概览、证据时间线、人物、地点、活动、产物、规则、动作建议、追溯）。
 *
 * 全部只读：数据层只调用 client.get；页面不提供审核、执行、改名、纠正等任何写入入口。
 * 数据层与页面都只展示“当前认证 API 实际返回的内容”：空、无授权、读取失败各有明确状态，
 * 计数只写“已加载 N 条”，从不编造总数；实时读取不是快照，页面会如实说明。
 *
 * 浏览器里挂在 self.EarWsPages；Node 测试用 require 加载，并配合假 DOM 运行。
 */
(function (root, factory) {
  'use strict';
  var isNode = typeof module === 'object' && module !== null && module.exports;
  var core = isNode ? require('./ws-core.js') : root.EarWsCore;
  var api = factory(core);
  if (isNode) module.exports = api; else root.EarWsPages = api;
})(typeof self !== 'undefined' ? self : this, function (core) {
  'use strict';

  const LOAD_CAP = 500;          // 单个列表最多加载的条数
  const SCAN_PAGES = 5;          // 为“关联对象”做的有界扫描页数（每页 100）
  const PROBE_ORDER = ['transcript_segment', 'gps_point', 'phone_info', 'voiceprint', 'ambient_sound', 'audio_ref', 'local_speaker',
    'gps_geocode', 'activity', 'artifact', 'action_suggestion', 'person', 'place', 'attribution', 'rule', 'rule_selection', 'feedback'];

  // ================================================================ 数据层（只读）

  function createData(client) {
    const parseErr = () => core.describeError({ failure: 'parse' });
    const badPath = () => core.describeError({ failure: 'bad_path' });

    async function list(type, o) {
      const path = core.listPath(type, o);
      if (path === null) return { ok: false, error: badPath() };
      const r = await client.get(path);
      if (r.stale) return { stale: true };
      if (!r.ok) return { ok: false, error: r.error };
      const page = core.readPage(r.data);
      if (!page.ok || page.items.length + page.dropped > (o?.limit || 50) || page.items.some((x) => x.type !== type) ||
          (page.nextCursor && page.nextCursor === o?.cursor)) return { ok: false, error: parseErr() };
      return { ok: true, page: page };
    }
    async function record(type, id, version, opts) {
      const path = core.recordPath(type, id, version);
      if (path === null) return { ok: false, error: badPath() };
      const r = await client.get(path);
      if (r.stale) return { stale: true };
      if (!r.ok) return { ok: false, error: r.error };
      const rec = core.normalizeRecord(r.data);
      if (!rec || rec.id !== id || rec.type !== type || (version && rec.version !== version)) return { ok: false, error: parseErr() };
      return { ok: true, rec: rec };
    }
    async function history(type, id, after) {
      const path = core.historyPath(type, id, after, 50);
      if (path === null) return { ok: false, error: badPath() };
      const r = await client.get(path);
      if (r.stale) return { stale: true };
      if (!r.ok) return { ok: false, error: r.error };
      const h = core.readHistory(r.data);
      if (!h.ok || h.dropped || h.items.some((x, i) => x.id !== id || x.type !== type ||
          x.version !== (after || 0) + i + 1) ||
          (h.nextAfter !== null && (!h.items.length || h.nextAfter !== h.items[h.items.length - 1].version))) {
        return { ok: false, error: parseErr() };
      }
      return { ok: true, history: h };
    }
    async function json(path, reader) {
      if (path === null) return { ok: false, error: badPath() };
      const r = await client.get(path);
      if (r.stale) return { stale: true };
      if (!r.ok) return { ok: false, error: r.error };
      const v = reader(r.data);
      return v && v.ok ? { ok: true, value: v } : { ok: false, error: parseErr() };
    }
    /** 类型未知的 ID：按固定顺序逐个类型读取（最多 17 次），404 继续，其他错误停止。 */
    async function resolveAny(id) {
      let denied = 0;
      for (const t of PROBE_ORDER) {
        const r = await record(t, id);
        if (r.stale) return r;
        if (r.ok) return { ok: true, type: t, rec: r.rec };
        if (r.error.kind === 'notfound') continue;
        if (r.error.kind === 'forbidden') { denied++; continue; }
        return r;
      }
      return { ok: false, error: core.describeError({ status: 404 }), denied: denied };
    }
    return {
      list: list, record: record, history: history, json: json, resolveAny: resolveAny,
      health: async function () {
        const r = await client.get('/health/ready');
        if (r.stale) return { stale: true };
        if (r.ok && core.isObj(r.data)) return { ok: true, status: String(r.data.status || ''), scope: r.data.scope || null };
        if (r.status === 503) return { ok: true, status: 'unavailable', scope: null };
        return { ok: false, error: r.error };
      },
      clear: function () { /* No cross-request data cache; every read is authorized. */ }
    };
  }

  /** 有界扫描：最多 maxPages 页，筛出满足条件的记录；明确告知“是否已读完”。 */
  async function scan(ctx, type, range, pred, maxPages) {
    let cursor = null, pages = 0, scanned = 0, complete = false;
    const found = [];
    while (pages < (maxPages || SCAN_PAGES)) {
      const r = await ctx.data.list(type, { range: range, limit: 100, cursor: cursor });
      if (r.stale || !ctx.alive()) return { stale: true };
      if (!r.ok) return { error: r.error, found: found, scanned: scanned, complete: false };
      pages++;
      scanned += r.page.items.length;
      r.page.items.forEach(function (x) { if (pred(x)) found.push(x); });
      cursor = r.page.nextCursor;
      if (!cursor) { complete = true; break; }
    }
    return { found: found, scanned: scanned, complete: complete, pages: pages };
  }
  /** 活动时间范围查询：端点相同（单点）的区间是空区间，所以至少放宽 1 秒。 */
  function spanRange(rec) {
    const s = core.parseUtc(rec.start), e = core.parseUtc(rec.end);
    if (s === null || e === null) return null;
    return { start: rec.start, end: core.toIso(Math.max(e, s + 1000)) };
  }

  function rememberCursor(state, first, next) {
    if (first || !state.cursors) state.cursors = new Set();
    if (next && state.cursors.has(next)) { state.capped = true; return null; }
    if (next) state.cursors.add(next);
    return next;
  }

  // ================================================================ 页面

  function createPages(ctx0) {
    const ui = ctx0.ui, h = ui.h, doc = ctx0.doc;
    const zoneNow = ctx0.zone;

    function titleEl(text) { return h('h2', { id: 'view-title', tabindex: '-1', text: text }); }
    function recMeta(rec, desc) {
      const bits = [];
      if (rec.start) bits.push(ui.timeSpan(rec.start, rec.end || rec.start));
      bits.push(ui.badge('v' + rec.version, 'muted'));
      if (!rec.current) bits.push(ui.badge('已被整体替代', 'warn'));
      return bits.map(function (b, i) { return i ? [' ', b] : b; });
    }
    function extraMeta(rec) {
      const p = rec.payload;
      switch (rec.type) {
        case 'person': return (core.isInt(p.display_number) ? '编号 #' + p.display_number + ' · ' : '') + '参考声纹 ' + core.idList(p.references).length;
        case 'place': return '参考 GPS ' + core.idList(p.references).length + (p.is_unknown === true ? ' · 兜底“未知地点”' : '');
        case 'action_suggestion': return core.labelOf(core.STATE_LABELS, p.state, '未知状态');
        case 'rule': return (p.enabled === false ? '已停用' : p.enabled === true ? '启用中' : '启用状态未记录') + (core.isId(p.activity_id) ? ' · 仅限单个活动' : '');
        case 'artifact': return core.labelOf(core.SOURCE_LABELS, p.manual === true ? 'manual' : p.source, '来源未记录');
        default: return '';
      }
    }
    function recordRow(rec, hash, selected) {
      const desc = core.describeRecord(rec);
      const extra = extraMeta(rec);
      const link = ui.a(hash, [
        h('span', { class: 'row-title', text: desc.headline }),
        h('span', { class: 'row-meta' }, [recMeta(rec, desc), extra ? [' · ', extra] : null])
      ], { class: 'row-link', 'aria-current': selected ? 'true' : null });
      return h('li', { class: 'record-row' + (selected ? ' is-selected' : '') }, link);
    }

    // ------------------------------------------------------------ 概览

    function overviewPage(ctx) {
      const root = ctx.root;
      root.appendChild(titleEl('概览'));
      root.appendChild(h('p', { class: 'lead-text', text: '这是 Ear 的只读工作台：用当前令牌读取①里的真实数据，不写入、不审核、不执行。下面逐类显示“你是否能读到数据”，不显示总数（接口不提供计数）。' }));

      const conn = ui.section('连接与服务');
      root.appendChild(conn.node);
      const me = ctx.session();
      const facts = [
        { label: '当前主体', kind: 'text', value: me ? me.subject : null },
        { label: '角色（来自网关配置，授权由①逐次复核）', kind: 'text', value: me ? (me.role === 'admin' ? '管理员 admin' : '普通用户 user') : null },
        { label: '读取方式', kind: 'text', value: '实时读取（live），不是快照；翻页期间有新写入时可能出现重复或遗漏。' }
      ];
      conn.set(ui.facts(facts));
      const healthLine = h('p', { class: 'meta', role: 'status', text: '正在检查服务就绪状态…' });
      conn.body.appendChild(healthLine);
      ctx.data.health().then(function (r) {
        if (r.stale || !ctx.alive()) return;
        healthLine.textContent = !r.ok ? '服务就绪检查失败：' + r.error.message
          : r.status === 'ready' ? '服务就绪：⑥ 到 ① 的连通正常（仅表示连通，不代表各模块都在运行）。'
          : '服务未就绪：存储暂时不可用，下面的读取可能失败。';
      });
      if (me && me.role === 'admin') {
        conn.body.appendChild(h('p', { class: 'meta' }, ['你是管理员：插件登记、授权与凭据请到 ', h('strong', { text: '插件管理页' }), '（侧边栏链接）；本工作台不提供这些操作。']));
      }

      const probe = ui.section('各类数据的可读性', {
        intro: '每类数据只读取 1 条用于判断：有数据 / 没有数据 / 没有读取授权 / 读取失败。“最早”是按时间升序的第一条，不是总范围。',
        actions: [h('button', { type: 'button', text: '重新检测', on: { click: function () { run(); } } })]
      });
      root.appendChild(probe.node);
      const table = h('table', { class: 'data-table' });
      table.appendChild(h('caption', { class: 'sr-only', text: '各类数据的可读性' }));
      table.appendChild(h('thead', {}, h('tr', {}, ['数据类型', '分类', '状态', '最早一条', '打开'].map(function (t) { return h('th', { scope: 'col', text: t }); }))));
      const tbody = h('tbody');
      table.appendChild(tbody);
      probe.set(h('div', { class: 'table-wrap' }, table));
      const cells = {};
      core.TYPE_ORDER.forEach(function (t) {
        const status = h('td', {}, ui.badge('检测中…', 'muted'));
        const earliest = h('td', { text: '—' });
        const open = h('td');
        cells[t] = { status: status, earliest: earliest, open: open };
        tbody.appendChild(h('tr', {}, [
          h('th', { scope: 'row' }, [core.TYPES[t].label + ' ', ui.code(t)]),
          h('td', { text: core.KIND_LABELS[core.TYPES[t].kind] }),
          status, earliest, open
        ]));
      });

      function paint(t, res) {
        const c = cells[t];
        const spec = core.TYPES[t];
        ui.fill(c.status, res.state === 'has' ? ui.badge(res.more ? '有数据（多于 1 条）' : '有数据（至少 1 条）', 'ok')
          : res.state === 'empty' ? ui.badge('没有数据', 'muted')
          : res.state === 'denied' ? ui.badge('无读取授权或不可见', 'warn')
          : ui.badge('读取失败', 'bad'));
        if (res.state === 'denied' || res.state === 'error') c.status.appendChild(h('div', { class: 'meta', text: res.message }));
        ui.fill(c.earliest, res.state === 'has' && res.earliest ? ui.timeEl(res.earliest) : '—');
        const links = [];
        if (res.state === 'has') {
          const view = spec.view;
          links.push(ui.a(view ? '#/' + view : '#/trace/' + t, view ? '打开' + spec.label : '在追溯页查看'));
          if (res.earliest && core.parseUtc(res.earliest) !== null && spec.time) {
            links.push(' ', h('button', { type: 'button', text: '到时间线看这一天', on: { click: function () {
              const d = core.dateOf(res.earliest, zoneNow());
              ctx.shared.timeline = { range: { mode: 'day', date: d, start: '', end: '' }, tab: 'transcript_segment' };
              ctx.go('#/timeline');
            } } }));
          }
        }
        ui.fill(c.open, links);
      }
      let runId = 0;
      function run() {
        const mine = ++runId;
        core.TYPE_ORDER.forEach(function (t) {
          ui.fill(cells[t].status, ui.badge('检测中…', 'muted'));
          ctx.data.list(t, { limit: 1 }).then(function (r) {
            if (r.stale || !ctx.alive() || mine !== runId) return;
            paint(t, core.classifyProbe(r.ok ? { ok: true } : r, r.ok ? r.page : null));
          });
        });
      }
      run();
      return { update: function () {}, dispose: function () { runId++; } };
    }

    // ------------------------------------------------------------ 证据解析（时间线与活动详情共用）

    async function resolveEvidence(ctx, activity, ids, box) {
      const MAX = 50, PROBE_BUDGET = 16;
      const shown = ids.slice(0, MAX);
      if (!shown.length) { ui.fill(box, ui.stateBox('empty', '这个活动没有登记证据 ID。')); return; }
      ui.fill(box, ui.stateBox('loading', '正在解析 ' + shown.length + ' 条证据…'));
      const want = new Set(shown), found = new Map(), notes = [];
      const range = spanRange(activity);
      if (range) {
        for (const t of ['transcript_segment', 'gps_point', 'phone_info', 'ambient_sound']) {
          if (found.size === want.size) break;
          const s = await scan(ctx, t, range, function (x) { return want.has(x.id); }, 3);
          if (s.stale || !ctx.alive()) return;
          if (s.error) { notes.push(core.typeLabel(t) + ' 按时间读取失败：' + s.error.message); continue; }
          s.found.forEach(function (x) { found.set(x.id, x); });
        }
      }
      let probes = 0;
      for (const id of shown) {
        if (found.has(id) || probes >= PROBE_BUDGET) continue;
        for (const t of ['voiceprint', 'audio_ref', 'local_speaker', 'gps_geocode']) {
          if (probes >= PROBE_BUDGET) break;
          probes++;
          const r = await ctx.data.record(t, id);
          if (r.stale || !ctx.alive()) return;
          if (r.ok) { found.set(id, r.rec); break; }
          if (r.error.kind !== 'notfound' && r.error.kind !== 'forbidden') { notes.push('逐条读取失败：' + r.error.message); probes = PROBE_BUDGET; break; }
        }
      }
      const ul = h('ul', { class: 'evidence-list' });
      shown.forEach(function (id) {
        const rec = found.get(id);
        if (!rec) {
          ul.appendChild(h('li', {}, [ui.chip(null, id), h('span', { class: 'tone-muted', text: ' 未能解析：可能超出本次有界扫描、活动时间范围，没有读取授权或已不存在。' })]));
          return;
        }
        const d = core.describeRecord(rec);
        ul.appendChild(h('li', {}, [
          ui.badge(core.typeLabel(rec.type), 'info'), ' ',
          rec.start ? [ui.timeSpan(rec.start, rec.end || rec.start), ' '] : null,
          h('span', { class: 'pre-wrap', text: d.headline }), ' ',
          ui.chip(rec.type, rec.id)
        ]));
      });
      ui.fill(box, [
        h('p', { class: 'meta', text: '证据 ID 共 ' + ids.length + ' 个' + (ids.length > MAX ? '（仅解析前 ' + MAX + ' 个）' : '') + '；已解析 ' + found.size + ' 个。' }),
        ul,
        notes.length ? h('ul', { class: 'warn-list' }, notes.map(function (n) { return h('li', { text: n }); })) : null
      ]);
    }

    function highlight(text, phrase) {
      const out = [];
      let i = 0, marks = 0;
      while (marks < 20) {
        const j = phrase ? text.indexOf(phrase, i) : -1;
        if (j < 0) break;
        if (j > i) out.push(text.slice(i, j));
        out.push(h('mark', { text: phrase }));
        i = j + phrase.length; marks++;
      }
      out.push(text.slice(i));
      return out;
    }

    // ------------------------------------------------------------ 证据时间线

    function timelinePage(ctx) {
      const root = ctx.root;
      const st = ctx.shared.timeline || (ctx.shared.timeline = { range: { mode: 'day', date: null, start: '', end: '' }, tab: 'transcript_segment' });
      let load = 0, rawLoad = 0, searchLoad = 0;
      root.appendChild(titleEl('证据时间线'));
      root.appendChild(h('p', { class: 'lead-text', text: '按天（或自定义区间）查看活动、原始证据（转写、GPS、手机信息等），并按短语检索转写。时间条只是活动的示意；下方列表才是完整、可用键盘操作的内容。' }));
      const rangeHost = h('div', { class: 'panel' });
      const actsSec = ui.section('活动', { cls: 'wide' });
      const rawSec = ui.section('原始证据流');
      const searchSec = ui.section('证据检索（按短语）');
      [rangeHost, actsSec.node, rawSec.node, searchSec.node].forEach(function (n) { root.appendChild(n); });

      let controls = null, win = null;

      async function locate() {
        if (st.range.mode === 'day' && !st.range.date) {
          ui.fill(rangeHost, ui.stateBox('loading', '正在定位有数据的日期…'));
          const r = await ctx.data.list('activity', { limit: 1 });
          if (r.stale || !ctx.alive()) return false;
          const first = r.ok && r.page.items[0];
          const d = first ? core.dateOf(first.start, zoneNow()) : null;
          st.range.date = d || core.dateOf(core.toIso(ctx.now()), zoneNow());
          st.note = first && d ? '已自动定位到最早一条活动所在的日期，可用下方控件更改。'
            : r.ok ? '没有读到任何活动，先显示今天；可在概览查看哪些类型有数据。' : '无法读取活动（' + r.error.message + '），先显示今天。';
        }
        return true;
      }
      function buildControls() {
        controls = ui.rangeControls(st.range, { kinds: ['day', 'custom'], onApply: function () { reload(); } });
        ui.fill(rangeHost, [h('h3', { text: '时间范围' }), controls.node, st.note ? h('p', { class: 'meta', text: st.note }) : null]);
      }

      // ---- 活动
      const acts = { items: [], next: null, dropped: 0, loading: false, capped: false };
      const actsPager = ui.pager({ noun: '个活动', onMore: function () { loadActs(false); } });
      const actsHost = h('div');
      actsSec.set([actsHost, actsPager.node]);
      const rowFor = new Map();

      async function loadActs(first) {
        if (!win) return;
        const mine = load;
        if (first) { acts.items = []; acts.next = null; acts.dropped = 0; acts.capped = false; rowFor.clear(); ui.fill(actsHost, ui.stateBox('loading', '正在读取活动…')); }
        acts.loading = true; actsPager.update({ loaded: acts.items.length, hasMore: !!acts.next, loading: true, dropped: acts.dropped, capped: acts.capped });
        const r = await ctx.data.list('activity', { range: win, limit: 100, cursor: first ? null : acts.next });
        if (r.stale || !ctx.alive() || mine !== load) return;
        acts.loading = false;
        if (!r.ok) { ui.fill(actsHost, ui.errorBox(r.error, function () { loadActs(true); })); actsPager.node.hidden = true; return; }
        actsPager.node.hidden = false;
        const m = core.mergeItems(acts.items, r.page.items, LOAD_CAP);
        acts.items = m.items; acts.capped = m.capped; acts.dropped += r.page.dropped; acts.next = rememberCursor(acts, first, r.page.nextCursor);
        renderActs();
        ctx.announce('已加载 ' + acts.items.length + ' 个活动');
      }
      function renderActs() {
        actsPager.update({ loaded: acts.items.length, hasMore: !!acts.next, loading: false, dropped: acts.dropped, capped: acts.capped });
        if (!acts.items.length) {
          ui.fill(actsHost, ui.stateBox('empty', '这个时间范围内没有读到活动。活动由④在提取后写入；如果刚导入数据，请确认提取已运行，或到概览查看有数据的日期。'));
          return;
        }
        const s = core.parseUtc(win.start), e = core.parseUtc(win.end);
        const layout = core.layoutTimeline(acts.items.map(function (x) { return { id: x.id, start: x.start, end: x.end, rec: x }; }), s, e);
        const zone = zoneNow();
        const ticks = core.hourTicks(s, e, zone);
        const svg = ui.timelineSvg(layout, ticks, {
          label: '活动时间条：' + acts.items.length + ' 个活动，窗口 ' + (core.formatClock(win.start, zone, false) || '') + ' 至 ' + (core.formatClock(win.end, zone, false) || '') + '。完整内容见下方列表。',
          titleOf: function (it) { return core.describeRecord(it.rec).headline + '（' + (core.formatClock(it.start, zone, false) || '') + ' – ' + (core.formatTimeOnly(it.end, zone) || '') + '）'; },
          onSelect: function (it) { const row = rowFor.get(it.id); if (row) { row.scrollIntoView(); row.focus(); } }
        });
        const list = h('ol', { class: 'activity-list' });
        acts.items.forEach(function (rec) { list.appendChild(activityItem(rec)); });
        ui.fill(actsHost, [
          h('div', { class: 'tl-wrap' }, svg),
          layout.outside ? h('p', { class: 'meta', text: layout.outside + ' 个活动不在时间条窗口内或时间异常，仍在下方列表中。' }) : null,
          list
        ]);
      }
      function activityItem(rec) {
        const evBox = h('div', { class: 'evidence-box', hidden: true });
        const ids = core.idList(rec.payload.evidence_ids);
        let opened = false;
        const evBtn = h('button', { type: 'button', 'aria-expanded': 'false', text: '证据（' + ids.length + '）', on: { click: function () {
          opened = !opened;
          evBtn.setAttribute('aria-expanded', opened ? 'true' : 'false');
          evBox.hidden = !opened;
          if (opened && !evBox.childNodes.length) resolveEvidence(ctx, rec, ids, evBox);
        } } });
        const titleLink = ui.a('#/activities/' + rec.id, ui.markdownInline(typeof rec.payload.title_md === 'string' ? rec.payload.title_md : '（无标题活动）'));
        const li = h('li', { class: 'activity-item' }, [
          h('div', { class: 'activity-head' }, [
            h('strong', {}, ui.timeSpan(rec.start, rec.end)),
            rec.current ? null : ui.badge('已被整体替代', 'warn')
          ]),
          h('h4', { class: 'activity-title' }, titleLink),
          rec.payload.description_md ? h('p', { class: 'meta', text: core.plainText(rec.payload.description_md, 160) }) : null,
          h('div', { class: 'row' }, [evBtn, ui.a('#/activities/' + rec.id, '详情与版本'), ui.a('#/trace/activity/' + rec.id, '追溯')]),
          evBox
        ]);
        rowFor.set(rec.id, titleLink);
        return li;
      }

      // ---- 原始证据流
      const raw = { items: [], next: null, dropped: 0, loading: false, capped: false };
      const rawPager = ui.pager({ noun: '条', onMore: function () { loadRaw(false); } });
      const rawHost = h('div');
      const TABS = [['transcript_segment', '转写段'], ['gps_point', 'GPS 点'], ['phone_info', '手机信息'], ['ambient_sound', '环境声'], ['audio_ref', '录音引用']];
      const tabBar = h('div', { class: 'row tabs', role: 'group', 'aria-label': '证据类型' });
      const tabBtns = TABS.map(function (t) {
        const b = h('button', { type: 'button', text: t[1], on: { click: function () { st.tab = t[0]; paintTabs(); loadRaw(true); } } });
        tabBar.appendChild(b);
        return b;
      });
      function paintTabs() { TABS.forEach(function (t, i) { tabBtns[i].setAttribute('aria-pressed', st.tab === t[0] ? 'true' : 'false'); }); }
      rawSec.set([tabBar, rawHost, rawPager.node]);
      paintTabs();

      async function loadRaw(first) {
        if (!win) return;
        const mine = load, rawMine = ++rawLoad, type = st.tab;
        if (first) { raw.items = []; raw.next = null; raw.dropped = 0; raw.capped = false; ui.fill(rawHost, ui.stateBox('loading', '正在读取' + core.typeLabel(type) + '…')); }
        rawPager.update({ loaded: raw.items.length, hasMore: !!raw.next, loading: true, dropped: raw.dropped, capped: raw.capped });
        const r = await ctx.data.list(type, { range: win, limit: 50, cursor: first ? null : raw.next });
        if (r.stale || !ctx.alive() || mine !== load || rawMine !== rawLoad || type !== st.tab) return;
        if (!r.ok) { ui.fill(rawHost, ui.errorBox(r.error, function () { loadRaw(true); })); rawPager.node.hidden = true; return; }
        rawPager.node.hidden = false;
        const m = core.mergeItems(raw.items, r.page.items, LOAD_CAP);
        raw.items = m.items; raw.capped = m.capped; raw.dropped += r.page.dropped; raw.next = rememberCursor(raw, first, r.page.nextCursor);
        rawPager.update({ loaded: raw.items.length, hasMore: !!raw.next, loading: false, dropped: raw.dropped, capped: raw.capped });
        if (!raw.items.length) { ui.fill(rawHost, ui.stateBox('empty', '这个时间范围内没有读到' + core.typeLabel(type) + '。')); return; }
        const ul = h('ol', { class: 'raw-list' });
        raw.items.forEach(function (rec) {
          const d = core.describeRecord(rec);
          ul.appendChild(h('li', {}, [
            h('div', { class: 'meta' }, [ui.timeSpan(rec.start, rec.end || rec.start), ' · ', ui.chip(rec.type, rec.id)]),
            h('div', { class: 'pre-wrap', text: d.headline })
          ]));
        });
        ui.fill(rawHost, ul);
      }

      // ---- 证据检索
      const se = { q: '', next: null, items: [], loading: false };
      const qId = ui.nextId('evidence-q');
      const qInput = h('input', { id: qId, type: 'text', maxlength: '256', autocomplete: 'off', spellcheck: 'false', placeholder: '输入要在转写里查找的短语' });
      const searchBtn = h('button', { type: 'submit', class: 'primary', text: '检索' });
      const searchNote = h('p', { class: 'meta', role: 'status' });
      const searchOut = h('div');
      const searchForm = h('form', { class: 'row search-form', autocomplete: 'off' }, [h('label', { for: qId, class: 'inline', text: '短语' }), qInput, searchBtn]);
      searchSec.set([
        h('p', { class: 'meta', text: '在当前时间范围内（最长 24 小时）查找转写文本包含该短语的片段，并显示说话人归属与录音位置。读取是实时、尽力而为的：不保证完整，也不保证翻页稳定。' }),
        searchForm, searchNote, searchOut
      ]);
      searchForm.addEventListener('submit', function (ev) { if (ev && ev.preventDefault) ev.preventDefault(); runSearch(true); });

      async function runSearch(first) {
        if (!win) return;
        const q = first ? qInput.value : se.q;
        if (first && q.trim() === '') { searchNote.textContent = '请输入要检索的短语。'; return; }
        if (q.length > 256) { searchNote.textContent = '短语最长 256 个字符。'; return; }
        const w = core.evidenceWindow(win);
        if (!w.ok) { searchNote.textContent = w.error; return; }
        const mine = load, searchMine = ++searchLoad;
        if (first) { se.q = q; se.next = null; se.items = []; ui.fill(searchOut, ui.stateBox('loading', '正在检索…')); }
        searchNote.textContent = w.clipped ? '所选范围超过 24 小时，只检索开始后的 24 小时。' : '';
        searchBtn.disabled = true;
        const r = await ctx.data.json(core.evidencePath({ range: w.range, q: se.q, limit: 10, cursor: first ? null : se.next }), core.readEvidence);
        if (r.stale || !ctx.alive() || mine !== load || searchMine !== searchLoad) return;
        searchBtn.disabled = false;
        if (!r.ok) {
          if (r.error.kind === 'notfound') {
            ui.fill(searchOut, ui.stateBox('denied', '证据检索接口不可用或无权访问（' + core.EVIDENCE_PATH + '）；请确认部署已启用。其他功能不受影响。', { detail: 'HTTP 404' }));
          } else ui.fill(searchOut, ui.errorBox(r.error, function () { runSearch(first); }));
          return;
        }
        const v = r.value;
        const seen = new Set(se.items.map((x) => x.id + '@' + x.version));
        const additions = v.items.filter((x) => { const k = x.id + '@' + x.version; if (seen.has(k)) return false; seen.add(k); return true; });
        if (first || !se.cursors) se.cursors = new Set();
        const repeated = v.nextCursor && se.cursors.has(v.nextCursor);
        if (v.nextCursor) se.cursors.add(v.nextCursor);
        const oldCursor = se.next;
        se.items = se.items.concat(additions).slice(0, LOAD_CAP); se.next = v.nextCursor;
        if (repeated || (se.next && se.next === oldCursor) || se.items.length >= LOAD_CAP) {
          se.next = null; searchNote.textContent += ' 已达到页面上限或游标未前进，请缩小范围重新检索。';
        }
        const ul = h('ol', { class: 'evidence-hits' });
        se.items.forEach(function (it) {
          const who = it.attribution
            ? (it.attribution.name ? '人物：' + it.attribution.name : '人物：未命名') + '（' + core.labelOf(core.ATTRIBUTION_STATUS_LABELS, it.attributionStatus, it.attributionStatus || '') + '）'
            : core.labelOf(core.ATTRIBUTION_STATUS_LABELS, it.attributionStatus, '归属状态未知');
          const play = it.playback
            ? '录音 ' + core.shortId(it.playback.audioRefId) + ' 的 ' + core.formatOffsetSeconds(it.playback.startSeconds) + ' – ' + core.formatOffsetSeconds(it.playback.endSeconds) + '（' + core.labelOf(core.PLAYBACK_STATUS_LABELS, it.playbackStatus, '') + '）'
            : core.labelOf(core.PLAYBACK_STATUS_LABELS, it.playbackStatus, '录音位置未知');
          ul.appendChild(h('li', { class: 'hit' }, [
            h('div', { class: 'meta' }, [ui.timeSpan(it.start, it.end), ' · ', ui.chip('transcript_segment', it.id, it.version)]),
            h('p', { class: 'pre-wrap' }, highlight(it.text, se.q)),
            h('p', { class: 'meta', text: who + ' · ' + play }),
            it.attribution ? h('p', { class: 'meta' }, ['查看人物：', ui.chip('person', it.attribution.personId, it.attribution.personVersion)]) : null
          ]));
        });
        const rep = v.report;
        const status = [
          '已显示 ' + se.items.length + ' 条命中',
          v.dropped ? '（' + v.dropped + ' 条格式异常已忽略）' : '',
          '；扫描 ' + (v.scanned === null ? '?' : v.scanned) + ' 段',
          '；停止原因：' + core.labelOf(core.STOP_REASON_LABELS, rep.stopReason, rep.stopReason || '未知'),
          v.truncated ? '；结果可能不完整' : '',
          '；一致性：' + (v.consistency === 'live_best_effort' ? '实时尽力而为' : (v.consistency || '未说明'))
        ].join('');
        ui.fill(searchOut, [
          se.items.length ? ul : ui.stateBox('empty', '该时间窗内没有找到包含这个短语的转写段' + (v.truncated ? '（但扫描被截断，不代表不存在）' : '') + '。'),
          h('p', { class: 'meta', role: 'status', text: status }),
          se.next ? h('button', { type: 'button', text: '继续检索', on: { click: function (ev) { ev.currentTarget.disabled = true; runSearch(false); } } }) : null
        ]);
      }

      async function reload() {
        const mine = ++load;
        const r = controls ? controls.read() : core.resolveRange(st.range, zoneNow());
        if (!r.ok) { ui.fill(actsHost, ui.stateBox('error', r.error)); return; }
        win = r.range;
        searchLoad++; searchBtn.disabled = false; se.next = null; se.items = [];
        ui.fill(searchOut, []); searchNote.textContent = '';
        if (mine !== load) return;
        loadActs(true); loadRaw(true);
      }
      (async function init() {
        if (!(await locate()) || !ctx.alive()) return;
        buildControls();
        reload();
      })();
      return { update: function () {}, dispose: function () { load++; } };
    }

    // ------------------------------------------------------------ 详情组件（各实体页与追溯页共用）

    async function loadFeedbackStatus(ctx, fid, box) {
      ui.fill(box, ui.stateBox('loading', '正在查询反馈处理状态…'));
      const r = await ctx.data.json(core.feedbackStatusPath(fid), core.readStatus);
      if (r.stale || !ctx.alive()) return;
      if (!r.ok) { ui.fill(box, ui.errorBox(r.error, function () { loadFeedbackStatus(ctx, fid, box); })); return; }
      const rows = r.value.rows.map(function (x) { return { label: x.label, kind: 'text', value: x.text }; });
      ui.fill(box, [
        ui.facts(rows),
        h('p', { class: 'meta', text: '“已受理”不等于“已完成”：最终结果以来源模块写回的回执为准；这里只读，不会重试或推进任何处理。' })
      ]);
    }

    function historySection(ctx, rec, cur, open) {
      const sec = ui.section('版本历史', { level: 4, intro: '修改会追加新版本，旧版本不可变；版本号最大的是当前版本。点“查看”可读取某个历史版本；“对比”显示相对上一版的字段差异（已脱敏）。' });
      const items = [];
      let nextAfter = 0, loading = false;
      const host = h('div');
      const more = h('button', { type: 'button', text: '加载更多版本', hidden: true, on: { click: function () { load(); } } });
      sec.set([host, more]);
      async function load() {
        if (loading) return;
        loading = true; more.disabled = true;
        if (!items.length) ui.fill(host, ui.stateBox('loading', '正在读取版本历史…'));
        const r = await ctx.data.history(rec.type, rec.id, nextAfter);
        loading = false; more.disabled = false;
        if (r.stale || !ctx.alive()) return;
        if (!r.ok) { ui.fill(host, ui.errorBox(r.error, function () { load(); })); return; }
        r.history.items.forEach(function (x) { items.push(x); });
        nextAfter = r.history.nextAfter;
        more.hidden = nextAfter === null || items.length >= LOAD_CAP;
        if (items.length >= LOAD_CAP && nextAfter !== null) sec.body.appendChild(h('p', { class: 'meta', text: '已达到 500 个版本的页面上限；可通过版本链接查看指定历史版本。' }));
        render(r.history.dropped);
      }
      function render(dropped) {
        if (!items.length) { ui.fill(host, ui.stateBox('empty', '没有读到版本历史。')); return; }
        const tbody = h('tbody');
        items.forEach(function (x, i) {
          const prev = i > 0 ? items[i - 1] : null;
          const diff = prev ? core.payloadDiff(prev.payload, x.payload) : null;
          const change = x.version === 1 ? '初始版本' : !prev ? '—（上一版未加载）'
            : diff.length ? diff.map(function (d) { return d.key; }).join('、') : (prev.status !== x.status ? '仅状态变化' : 'payload 无变化');
          const hasDiff = !!(diff && diff.length);
          const detailRow = hasDiff ? h('tr', { class: 'diff-row', hidden: true }, h('td', { colspan: '6' }, diffTable(diff))) : null;
          const diffBtn = hasDiff ? h('button', { type: 'button', 'aria-expanded': 'false', text: '对比', on: { click: function () {
            detailRow.hidden = !detailRow.hidden;
            diffBtn.setAttribute('aria-expanded', detailRow.hidden ? 'false' : 'true');
          } } }) : null;
          tbody.appendChild(h('tr', { class: x.version === rec.version ? 'is-selected' : '' }, [
            h('th', { scope: 'row' }, ['v' + x.version, x.version === cur.version ? ' ' : null, x.version === cur.version ? ui.badge('当前', 'ok') : null]),
            h('td', { text: x.current ? '写入时有效（不代表当前）' : '终止（已被整体替代）' }),
            h('td', {}, ui.timeEl(x.trace.createdAt)),
            h('td', { text: x.writer || '—' }),
            h('td', { text: change }),
            h('td', {}, [ui.a(core.recordHash(x.type, x.id, x.version === cur.version ? null : x.version), '查看', { 'aria-current': x.version === rec.version ? 'true' : null }), ' ', diffBtn])
          ]));
          if (detailRow) tbody.appendChild(detailRow);
        });
        ui.fill(host, [
          h('div', { class: 'table-wrap' }, h('table', { class: 'data-table' }, [
            h('caption', { class: 'sr-only', text: '版本历史' }),
            h('thead', {}, h('tr', {}, ['版本', '状态', '创建时间', '写入方', '相对上一版的变化', '操作'].map(function (t) { return h('th', { scope: 'col', text: t }); }))),
            tbody
          ])),
          dropped ? h('p', { class: 'meta', text: dropped + ' 个版本格式异常，已忽略。' }) : null
        ]);
      }
      if (open) load(); else {
        const btn = h('button', { type: 'button', text: '加载版本历史', on: { click: function () { btn.hidden = true; load(); } } });
        ui.fill(host, btn);
      }
      return sec;
    }
    function diffTable(diff) {
      if (!diff || !diff.length) return h('p', { class: 'meta', text: '没有字段差异。' });
      const label = { added: '新增', removed: '删除', changed: '修改' };
      return h('table', { class: 'data-table diff-table' }, [
        h('thead', {}, h('tr', {}, ['字段', '变化', '旧值', '新值'].map(function (t) { return h('th', { scope: 'col', text: t }); }))),
        h('tbody', {}, diff.map(function (d) {
          return h('tr', {}, [h('th', { scope: 'row' }, ui.code(d.key)), h('td', { text: label[d.change] }),
            h('td', { class: 'pre-wrap', text: d.before === null ? '—' : d.before }), h('td', { class: 'pre-wrap', text: d.after === null ? '—' : d.after })]);
        }))
      ]);
    }

    function traceSection(ctx, rec, desc, trace) {
      const sec = ui.section('追溯字段与依赖边界', { level: 4, intro: '这里只显示记录携带的追溯字段与关联引用，不把引用当成已登记依赖。当前公共 API 未暴露依赖登记、字段依赖、新鲜度或反向依赖，无法展示真实依赖图。' });
      const t = rec.trace;
      const root = t.rootFeedbackId;
      const rows = [
        { label: '写入方模块', kind: 'text', value: rec.writer },
        { label: '来源', kind: 'text', value: rec.source ? (rec.source.device || '') + ' / ' + (rec.source.channel || '') : null },
        { label: '适配器版本', kind: 'text', value: t.adapterVersion },
        { label: '去重键', kind: 'code', value: t.dedupKey },
        { label: '创建/入库时间', kind: 'time', value: t.createdAt },
        { label: '触发的根反馈', kind: root === 'system' ? 'text' : 'code', value: root === 'system' ? 'system（系统发起，不是用户反馈）' : root },
        { label: '重判深度 depth', kind: 'text', value: t.depth === null ? null : String(t.depth) },
        { label: '写入批次 batch_id', kind: 'code', value: t.batchId },
        { label: '请求事件 request_event_id', kind: 'code', value: t.requestEventId },
        { label: '重放时跳过历史', kind: 'text', value: t.skipHistory === null ? null : (t.skipHistory ? '是' : '否') }
      ].filter(function (r) { return r.value !== null && r.value !== undefined; });
      const box = h('div', { class: 'status-box' });
      const kids = [ui.facts(rows)];
      if (core.isId(root)) {
        kids.push(h('div', { class: 'row' }, [
          ui.chip('feedback', root), ' ',
          h('button', { type: 'button', text: '查询根反馈的处理状态', on: { click: function () { loadFeedbackStatus(ctx, root, box); } } })
        ]), box);
      }
      sec.set(kids);
      if (trace && desc.refs.length) {
        const preview = h('div');
        const btn = h('button', { type: 'button', text: '预览关联对象（最多 12 个）', on: { click: function () { btn.hidden = true; previewRefs(ctx, desc.refs.slice(0, 12), preview); } } });
        sec.body.appendChild(h('div', {}, [h('h5', { text: '关联对象' }), btn, preview]));
      }
      return sec;
    }
    async function previewRefs(ctx, refs, box) {
      ui.fill(box, ui.stateBox('loading', '正在读取关联对象…'));
      const rows = [];
      for (const r of refs) {
        let res;
        if (r.type) res = await ctx.data.record(r.type, r.id, r.version || null).then(function (x) { return x.ok ? { ok: true, type: r.type, rec: x.rec } : x; });
        else res = await ctx.data.resolveAny(r.id);
        if (res.stale || !ctx.alive()) return;
        rows.push(h('li', {}, [
          h('span', { class: 'ref-label', text: r.label + '：' }),
          res.ok ? [ui.badge(core.typeLabel(res.type), 'info'), ' ', h('span', { class: 'pre-wrap', text: core.describeRecord(res.rec).headline }), ' ', ui.chip(res.type, r.id, r.version)]
            : [ui.chip(r.type, r.id, r.version), h('span', { class: 'tone-muted', text: ' ' + res.error.message })]
        ]));
      }
      ui.fill(box, h('ul', { class: 'refs' }, rows));
    }

    // ---- 各类型的关联面板（异步、只读）

    async function relatedPerson(ctx, host, rec) {
      const sec = ui.section('归属与参考声纹', { level: 4 });
      host.appendChild(sec.node); sec.loading();
      const ids = core.idList(rec.payload.references);
      const r = await ctx.data.json(core.personPreviewPath(rec.id), function (d) { return core.isObj(d) && core.isInt(d.attributed_records) ? { ok: true, count: d.attributed_records } : { ok: false }; });
      if (r.stale || !ctx.alive()) return;
      const refsBox = h('div');
      const btn = ids.length ? h('button', { type: 'button', text: '查看参考声纹（最多 20 个）', on: { click: function () {
        btn.hidden = true;
        loadVoiceprints(ctx, ids.slice(0, 20), refsBox);
      } } }) : null;
      sec.set([
        r.ok ? ui.facts([{ label: '已归属到此人物的记录数', kind: 'text', value: r.value.count + '（来自预览接口：当前有效的归属对象数）' }])
          : ui.errorBox(r.error, function () { host.removeChild(sec.node); relatedPerson(ctx, host, rec); }),
        ui.facts([{ label: '参考声纹数', kind: 'text', value: String(ids.length) }]),
        ids.length ? [btn, refsBox] : h('p', { class: 'meta', text: '这个人物目前没有参考声纹（可能已被全部移走）。' }),
        h('p', { class: 'meta', text: '声纹向量属于生物特征，本页不显示。改名、纠正归属等写操作不在本只读页面提供。' })
      ]);
    }
    async function loadVoiceprints(ctx, ids, box) {
      ui.fill(box, ui.stateBox('loading', '正在读取参考声纹…'));
      const res = await Promise.all(ids.map(function (id) { return ctx.data.record('voiceprint', id); }));
      if (!ctx.alive() || res.some(function (x) { return x.stale; })) return;
      const ul = h('ul', { class: 'refs' });
      res.forEach(function (x, i) {
        if (!x.ok) { ul.appendChild(h('li', {}, [ui.chip('voiceprint', ids[i]), h('span', { class: 'tone-muted', text: ' ' + x.error.message })])); return; }
        const n = core.idList(x.rec.payload.transcript_segment_ids).length;
        ul.appendChild(h('li', {}, [ui.chip('voiceprint', x.rec.id), ' ', ui.timeSpan(x.rec.start, x.rec.end || x.rec.start), ' · 关联转写段 ' + n + ' 段']));
      });
      ui.fill(box, ul);
    }

    async function relatedPlace(ctx, host, rec) {
      const sec = ui.section('参考 GPS 点', { level: 4 });
      host.appendChild(sec.node);
      const ids = core.idList(rec.payload.references).slice(0, 60);
      if (!ids.length) {
        sec.empty(rec.payload.is_unknown === true ? '这是兜底的“未知地点”：它没有参考 GPS 点，所有未能匹配到具体地点的 GPS 点都归到这里。' : '这个地点没有参考 GPS 点。');
        return;
      }
      sec.loading('正在读取 ' + ids.length + ' 个 GPS 点…');
      const res = await Promise.all(ids.map(function (id) { return ctx.data.record('gps_point', id); }));
      if (!ctx.alive() || res.some(function (x) { return x.stale; })) return;
      const pts = res.filter(function (x) { return x.ok; }).map(function (x) { return x.rec; });
      const failed = res.length - pts.length;
      const times = pts.map(function (p) { return core.parseUtc(p.start); }).filter(function (v) { return v !== null; });
      const geo = new Map();
      let geoNote = '';
      if (times.length) {
        const range = { start: core.toIso(Math.min.apply(null, times)), end: core.toIso(Math.max.apply(null, times) + 1000) };
        const want = new Set(ids);
        const s = await scan(ctx, 'gps_geocode', range, function (g) { return want.has(g.payload.gps_point_id); }, 3);
        if (s.stale || !ctx.alive()) return;
        if (s.error) geoNote = '逆地理编码读取失败：' + s.error.message;
        else {
          s.found.forEach(function (g) { geo.set(g.payload.gps_point_id, g); });
          if (!s.complete) geoNote = '逆地理编码只扫描了前 ' + s.scanned + ' 条，部分地址可能未显示。';
        }
      }
      const proj = core.projectPoints(pts.map(function (p) { return { id: p.id, lat: p.payload.latitude, lon: p.payload.longitude }; }), 300);
      const tbody = h('tbody');
      pts.forEach(function (p) {
        const g = geo.get(p.id);
        const addr = !g ? '没有读到逆地理编码' : g.payload.status === 'ok' ? (g.payload.formatted_address || '（无地址文本）') : g.payload.status === 'no_result' ? '逆地理编码无结果' : '逆地理编码失败';
        tbody.appendChild(h('tr', {}, [
          h('td', {}, ui.timeEl(p.start)),
          h('td', { text: core.describeRecord(p).headline }),
          h('td', { text: typeof p.payload.accuracy_m === 'number' ? '± ' + p.payload.accuracy_m + ' 米' : '—' }),
          h('td', { class: 'pre-wrap', text: addr }),
          h('td', {}, ui.chip('gps_point', p.id))
        ]));
      });
      sec.set([
        pts.length ? [
          proj.points.length ? h('figure', { class: 'geo-fig' }, [
            ui.scatterSvg(proj, { label: 'GPS 点相对位置示意：' + proj.points.length + ' 个点，跨度约 ' + proj.spanMeters + ' 米', titleOf: function (id) { return core.shortId(id); } }),
            h('figcaption', { class: 'meta', text: '仅为相对位置示意（北在上、跨度约 ' + proj.spanMeters + ' 米），不含底图，页面不会向任何地图服务发请求。' })
          ]) : null,
          h('div', { class: 'table-wrap' }, h('table', { class: 'data-table' }, [
            h('caption', { class: 'sr-only', text: '参考 GPS 点' }),
            h('thead', {}, h('tr', {}, ['时间', '坐标（纬度, 经度）', '精度', '地址', 'ID'].map(function (t) { return h('th', { scope: 'col', text: t }); }))),
            tbody
          ]))
        ] : ui.stateBox('empty', '没有读到任何参考 GPS 点。'),
        failed ? h('p', { class: 'tone-warn', text: failed + ' 个参考 GPS 点读取失败（无授权或已不存在）。' }) : null,
        rec.payload.references && rec.payload.references.length > 60 ? h('p', { class: 'meta', text: '只显示前 60 个参考点。' }) : null,
        geoNote ? h('p', { class: 'meta', text: geoNote }) : null
      ]);
    }

    function completenessNote(s) {
      return s.complete ? null : h('p', { class: 'meta', text: '只扫描了前 ' + s.scanned + ' 条记录（最多 ' + SCAN_PAGES * 100 + ' 条），结果可能不完整。' });
    }
    async function relatedActivity(ctx, host, rec, cur, desc) {
      const range = spanRange(rec);
      const ids = core.idList(rec.payload.evidence_ids);
      const evSec = ui.section('证据', { level: 4 });
      const evBox = h('div');
      const evBtn = h('button', { type: 'button', text: '解析证据（' + ids.length + '）', on: { click: function () { evBtn.hidden = true; resolveEvidence(ctx, rec, ids, evBox); } } });
      evSec.set(ids.length ? [evBtn, evBox] : h('p', { class: 'meta', text: '这个活动没有登记证据 ID。' }));
      host.appendChild(evSec.node);
      if (!range) { return; }
      const artSec = ui.section('该活动的产物与动作建议', { level: 4 });
      const selSec = ui.section('规则选择', { level: 4 });
      host.appendChild(artSec.node); host.appendChild(selSec.node);
      artSec.loading(); selSec.loading();
      const a = await scan(ctx, 'artifact', range, function (x) { return x.payload.activity_id === rec.id; });
      if (a.stale || !ctx.alive()) return;
      if (a.error) artSec.error(a.error, function () { relatedActivity(ctx, host, rec, cur, desc); });
      else {
        const artIds = new Set(a.found.map(function (x) { return x.id; }));
        const sg = await scan(ctx, 'action_suggestion', range, function (x) { return artIds.has(x.payload.artifact_id); });
        if (sg.stale || !ctx.alive()) return;
        const ul = h('ul', { class: 'record-list' });
        a.found.forEach(function (x) { ul.appendChild(recordRow(x, core.recordHash('artifact', x.id), false)); });
        const sl = h('ul', { class: 'record-list' });
        (sg.found || []).forEach(function (x) { sl.appendChild(recordRow(x, core.recordHash('action_suggestion', x.id), false)); });
        artSec.set([
          a.found.length ? ul : ui.stateBox('empty', '没有读到属于这个活动的产物（可能还没有适用的规则，或产物尚未生成）。'),
          completenessNote(a),
          a.found.length ? [h('h5', { text: '动作建议' }), sg.error ? ui.errorBox(sg.error) : (sg.found.length ? sl : h('p', { class: 'meta', text: '这些产物没有动作建议。' })), sg.error ? null : completenessNote(sg)] : null
        ]);
      }
      const s = await scan(ctx, 'rule_selection', range, function (x) { return x.payload.activity_id === rec.id; });
      if (s.stale || !ctx.alive()) return;
      if (s.error) { selSec.error(s.error, function () { relatedActivity(ctx, host, rec, cur, desc); }); return; }
      if (!s.found.length) { selSec.empty('没有读到这个活动的规则选择记录。'); return; }
      const list = h('div');
      s.found.forEach(function (x) {
        const ruleIds = core.idList(x.payload.rule_ids);
        list.appendChild(h('div', { class: 'card' }, [
          h('p', { class: 'meta' }, [ui.chip('rule_selection', x.id), ' ', ui.badge('v' + x.version, 'muted'), x.current ? null : [' ', ui.badge('已被整体替代', 'warn')]]),
          ruleIds.length ? h('ul', { class: 'refs' }, ruleIds.map(function (rid) { return h('li', {}, [h('span', { class: 'ref-label', text: '选中的规则：' }), ui.chip('rule', rid)]); })) : h('p', { class: 'meta', text: '没有选中任何规则。' })
        ]));
      });
      selSec.set([list, completenessNote(s)]);
    }

    async function relatedArtifact(ctx, host, rec) {
      const range = spanRange(rec);
      const sec = ui.section('这个产物的动作建议', { level: 4 });
      host.appendChild(sec.node);
      if (!range) { sec.empty('产物没有有效的时间范围，无法查找动作建议。'); return; }
      sec.loading();
      const s = await scan(ctx, 'action_suggestion', range, function (x) { return x.payload.artifact_id === rec.id; });
      if (s.stale || !ctx.alive()) return;
      if (s.error) { sec.error(s.error, function () { host.removeChild(sec.node); relatedArtifact(ctx, host, rec); }); return; }
      if (!s.found.length) { sec.set([ui.stateBox('empty', '没有读到针对这个产物的动作建议。'), completenessNote(s)]); return; }
      const ul = h('ul', { class: 'record-list' });
      s.found.forEach(function (x) {
        const v = x.payload.artifact_version;
        const row = recordRow(x, core.recordHash('action_suggestion', x.id), false);
        if (core.isInt(v) && v !== rec.version) row.appendChild(h('p', { class: 'meta tone-warn', text: '基于产物 v' + v + '（当前查看的是 v' + rec.version + '）。' }));
        ul.appendChild(row);
      });
      sec.set([ul, completenessNote(s)]);
    }

    async function relatedSuggestion(ctx, host, rec) {
      const sec = ui.section('外部动作状态（只读）', { level: 4 });
      host.appendChild(sec.node);
      const box = h('div');
      const btn = h('button', { type: 'button', text: '查询执行状态（只读）', on: { click: async function () {
        btn.disabled = true;
        ui.fill(box, ui.stateBox('loading', '正在查询…'));
        const r = await ctx.data.json(core.executionPath(rec.id), core.readStatus);
        btn.disabled = false;
        if (r.stale || !ctx.alive()) return;
        if (!r.ok) { ui.fill(box, ui.errorBox(r.error)); return; }
        ui.fill(box, [
          r.value.executionEnabled === true ? h('p', { class: 'banner warn', text: '服务端报告外部执行已启用；本页仍然只读，不提供审核、执行或重试。' })
            : h('p', { class: 'banner info', text: r.value.executionEnabled === false ? '此接口报告外部执行未启用；不代表其他执行器或过去的外部动作状态。' : '接口未报告执行开关，状态未知；本页不提供执行操作。' }),
          ui.facts(r.value.rows.map(function (x) { return { label: x.label, kind: 'text', value: x.text }; })),
          h('p', { class: 'meta', text: '这是只读查询：不查证、不写反馈、不推进执行；动作正文、键、哈希与外部标识不会显示。' })
        ]);
      } } });
      sec.set([
        h('p', { class: 'meta', text: '设计规定：建议 → 用户审核 → 执行。本工作台不提供审核和执行；结果未知时需要人工核对，不会自动重试。' }),
        btn, box
      ]);
    }

    const RELATED = {
      person: relatedPerson, place: relatedPlace, activity: relatedActivity,
      artifact: relatedArtifact, action_suggestion: relatedSuggestion
    };

    function renderDetail(parentCtx, host, type, id, version, opts) {
      const o = opts || {};
      let token = 0, disposed = false;
      const ctx = Object.assign({}, parentCtx, { alive: () => !disposed && parentCtx.alive() });
      async function render(fresh) {
        const mine = ++token;
        host.setAttribute('aria-busy', 'true');
        try {
          ui.fill(host, ui.stateBox('loading', '正在读取…'));
          const curR = o.prefetched && !fresh ? { ok: true, rec: o.prefetched } : await ctx.data.record(type, id, null, { fresh: fresh });
          if (curR.stale || !ctx.alive() || mine !== token) return;
          if (!curR.ok) { ui.fill(host, ui.errorBox(curR.error, function () { render(true); })); return; }
          const cur = curR.rec;
          let rec = cur;
          if (version && version !== cur.version) {
            const vr = await ctx.data.record(type, id, version, { fresh: fresh });
            if (vr.stale || !ctx.alive() || mine !== token) return;
            if (!vr.ok) { ui.fill(host, [ui.errorBox(vr.error, function () { render(true); }), ui.a(core.recordHash(type, id), '回到当前版本')]); return; }
            rec = vr.rec;
          }
          const desc = core.describeRecord(rec);
          const old = rec.version !== cur.version;
          const kids = [];
          kids.push(h('div', { class: 'detail-head' }, [
            h('h3', { class: 'pre-wrap', text: desc.headline }),
            h('div', { class: 'row' }, [
              ui.badge(core.typeLabel(type), 'info'), ui.badge('v' + rec.version, 'muted'),
              old ? ui.badge('历史版本', 'warn') : rec.current ? ui.badge('当前版本', 'ok') : ui.badge('已被整体替代（终止版本）', 'warn'),
              ui.code(rec.id),
              h('button', { type: 'button', text: '刷新', on: { click: function () { ctx.data.clear(); render(true); } } })
            ])
          ]));
          if (old) kids.push(h('p', { class: 'banner info', role: 'status' }, ['正在查看历史版本 v' + rec.version + '；当前版本是 v' + cur.version + '。', ' ', ui.a(core.recordHash(type, id), '回到当前版本')]));
          else if (!rec.current) kids.push(h('p', { class: 'banner warn', role: 'status', text: '这个对象已被整体替代：下面是它的终止版本，不会再有新版本。' }));
          const overview = ui.section('内容', { level: 4 });
          const ov = [];
          const facts = desc.fields.slice();
          if (rec.start) {
            ov.push(h('p', { class: 'meta' }, ['时间：', ui.timeSpan(rec.start, rec.end || rec.start)]));
            facts.unshift({ label: 'UTC 区间 [开始, 结束)', kind: 'code', value: '[' + rec.start + ', ' + (rec.end || rec.start) + ')' });
          }
          ov.push(ui.facts(facts));
          desc.markdown.forEach(function (m) {
            ov.push(h('h5', { text: m.label }));
            ov.push(m.inline ? h('p', {}, ui.markdownInline(m.text)) : ui.markdown(m.text));
          });
          const w = ui.warnings(desc.warnings);
          if (w) ov.push(w);
          const rl = ui.refList(desc.refs);
          if (rl) ov.push(h('h5', { text: '关联对象' }), rl);
          overview.set(ov);
          kids.push(overview.node);
          const related = h('div', { class: 'related' });
          if (old) related.appendChild(h('p', { class: 'banner info', text: '关联面板按当前授权实时查找，不是这个历史版本的依赖快照；只有明确带版本号的引用指向指定历史版本。' }));
          kids.push(related);
          kids.push(historySection(ctx, rec, cur, o.trace === true).node);
          kids.push(traceSection(ctx, rec, desc, o.trace === true).node);
          kids.push(ui.jsonDetails(rec.original || rec.payload));
          ui.fill(host, kids);
          if (RELATED[type]) await RELATED[type](ctx, related, rec, cur, desc);
        } catch (_) {
          if (!ctx.alive() || mine !== token) return;
          // Never expose payloads or exception text, and never strand the loading view.
          ui.fill(host, ui.stateBox('error', '详情显示失败，请重试或选择其他记录。', { onRetry: function () { render(true); } }));
        } finally {
          if (ctx.alive() && mine === token) host.setAttribute('aria-busy', 'false');
        }
      }
      render(false);
      return { dispose: function () { disposed = true; token++; } };
    }

    // ------------------------------------------------------------ 实体页（列表 + 详情）

    const ENTITY = {
      people: { view: 'people', type: 'person', title: '人物', timeBased: false,
        intro: '③归属出的人物（含未命名人物）。详情显示名称、参考声纹、已归属记录数与完整版本历史；改名、纠正等写操作不在本页提供。' },
      places: { view: 'places', type: 'place', title: '地点', timeBased: false,
        intro: '③归属出的地点。详情显示参考 GPS 点、逆地理编码地址和相对位置示意（无底图、不请求外部地图）。' },
      activities: { view: 'activities', type: 'activity', title: '活动', timeBased: true,
        intro: '④划分出的活动。详情显示标题与描述（安全渲染的 Markdown）、证据、产物、动作建议与规则选择。' },
      artifacts: { view: 'artifacts', type: 'artifact', title: '产物', timeBased: true,
        intro: '④按规则生成的 Markdown 产物。正文按不可信文本安全渲染：图片与链接不会加载或跳转。可查看每个历史版本与差异。' },
      rules: { view: 'rules', type: 'rule', title: '规则', timeBased: false,
        intro: '自然语言规则（适用范围、提取指令、可选动作）。规则编辑本身不会主动重算历史，这里只读显示当前与历史版本。' },
      actions: { view: 'actions', type: 'action_suggestion', title: '动作建议', timeBased: true,
        intro: '④生成的日历/提醒事项建议。外部动作须“建议→用户审核→执行”；本页只读，不审核、不执行、不重试。' }
    };

    function todayDate(ctx) { return core.dateOf(core.toIso(ctx.now()), zoneNow()); }

    function entityPage(ctx, route0, cfg) {
      let route = route0, loadTok = 0, detail = null;
      const root = ctx.root;
      root.appendChild(titleEl(cfg.title));
      root.appendChild(h('p', { class: 'lead-text', text: cfg.intro }));
      const listSec = ui.section(cfg.title + '列表', { cls: 'list-pane' });
      const detailHost = h('section', { class: 'detail-pane', 'aria-label': cfg.title + '详情' });
      root.appendChild(h('div', { class: 'split' }, [listSec.node, detailHost]));

      const rs = ctx.shared.ranges[cfg.view] || (ctx.shared.ranges[cfg.view] = { mode: 'all', date: todayDate(ctx), start: '', end: '' });
      const st = { items: [], next: null, dropped: 0, capped: false, includeSuperseded: false, appliedRange: null, loading: false, error: null };
      const idSup = ui.nextId('incl-sup'), idFilter = ui.nextId('filter');
      const sup = h('input', { id: idSup, type: 'checkbox' });
      const filter = h('input', { id: idFilter, type: 'text', autocomplete: 'off', spellcheck: 'false', placeholder: '在已加载的记录中筛选' });
      const controls = cfg.timeBased ? ui.rangeControls(rs, { kinds: ['all', 'day', 'custom'], onApply: function () { loadList(true); } }) : null;
      const pager = ui.pager({ noun: '条', onMore: function () { loadList(false); } });
      const listHost = h('div');
      listSec.set([
        controls ? controls.node : h('p', { class: 'meta', text: '这类数据没有事件时间，按 ID（大致创建顺序）读取，不支持时间范围。' }),
        h('div', { class: 'row' }, [
          h('label', { for: idSup, class: 'inline' }, [sup, ' 包含已被整体替代的对象']),
          h('label', { for: idFilter, class: 'inline', text: '筛选' }), filter,
          h('button', { type: 'button', text: '刷新', on: { click: function () { ctx.data.clear(); loadList(true); } } })
        ]),
        listHost, pager.node
      ]);
      sup.addEventListener('change', function () { st.includeSuperseded = !!sup.checked; loadList(true); });
      filter.addEventListener('input', function () { renderList(); });

      async function loadList(first) {
        const mine = ++loadTok;
        if (first) {
          const r = controls ? controls.read() : { ok: true, range: null };
          if (!r.ok) { st.loading = false; st.error = { kind: 'input', message: r.error }; renderList(); return; }
          st.appliedRange = r.range;
        }
        const range = st.appliedRange;
        if (first) { st.items = []; st.next = null; st.dropped = 0; st.capped = false; }
        st.loading = true;
        renderList();
        const r = await ctx.data.list(cfg.type, { range: range, limit: 50, cursor: first ? null : st.next, includeSuperseded: st.includeSuperseded });
        if (r.stale || !ctx.alive() || mine !== loadTok) return;
        st.loading = false;
        if (!r.ok) { st.error = r.error; renderList(); return; }
        st.error = null;
        const m = core.mergeItems(st.items, r.page.items, LOAD_CAP);
        st.items = m.items; st.capped = m.capped; st.dropped += r.page.dropped; st.next = rememberCursor(st, first, r.page.nextCursor);
        renderList();
        ctx.announce('已加载 ' + st.items.length + ' 条' + cfg.title);
      }
      function renderList() {
        pager.update({ loaded: st.items.length, hasMore: !!st.next, loading: st.loading, dropped: st.dropped, capped: st.capped });
        // A failed/pending read is not proof of an empty collection or its end.
        pager.node.hidden = !!st.error || (st.loading && !st.items.length);
        if (pager.node.hidden) pager.info.textContent = '';
        const notices = [];
        if (st.error) {
          notices.push(ui.errorBox(st.error, st.loading ? null : function () { loadList(true); }));
          if (st.items.length) notices.push(h('p', { class: 'banner warn', text: '读取未完成；以下仅为此前已加载的记录，可能过时。请刷新后确认。' }));
        }
        if (st.loading) notices.push(ui.stateBox('loading', '正在读取' + cfg.title + '…'));
        if (!st.items.length) {
          if (!notices.length) notices.push(ui.stateBox('empty', '没有读到' + cfg.title + (controls ? '（可以换一个时间范围，或选“全部时间”）' : '') + '。如果刚导入数据，请确认相应模块已经处理。'));
          ui.fill(listHost, notices);
          return;
        }
        const needle = filter.value.trim();
        const shown = needle ? st.items.filter(function (r) { return core.describeRecord(r).headline.indexOf(needle) >= 0; }) : st.items;
        if (!shown.length) { ui.fill(listHost, notices.concat(ui.stateBox('empty', '已加载的 ' + st.items.length + ' 条里没有匹配“' + needle + '”的记录；清空筛选或加载更多。'))); return; }
        const ul = h('ul', { class: 'record-list' });
        shown.forEach(function (rec) { ul.appendChild(recordRow(rec, core.recordHash(cfg.type, rec.id), rec.id === route.id)); });
        ui.fill(listHost, notices.concat([needle ? h('p', { class: 'meta', text: '筛选只作用于已加载的 ' + st.items.length + ' 条，显示 ' + shown.length + ' 条。' }) : null, ul]));
      }
      function showDetail() {
        if (detail) detail.dispose();
        detail = null;
        if (!route.id) { ui.fill(detailHost, h('p', { class: 'state state-empty', text: '在左侧选择一条记录，查看详情、历史版本与追溯。' })); return; }
        detail = renderDetail(ctx, detailHost, cfg.type, route.id, route.version, {});
      }
      loadList(true);
      showDetail();
      return {
        update: function (next) { route = next; renderList(); showDetail(); },
        dispose: function () { loadTok++; if (detail) detail.dispose(); }
      };
    }

    // ------------------------------------------------------------ 追溯

    function tracePage(ctx, route0) {
      let route = route0, detail = null, resolveTok = 0, loadTok = 0;
      const root = ctx.root;
      root.appendChild(titleEl('追溯'));
      root.appendChild(h('p', { class: 'lead-text', text: '输入任意记录的 ID，查看它的全部版本、写入方、触发它的反馈与批次，以及它引用的其他对象。追溯只基于当前账号可读的记录；没有全局因果图或依赖图（依赖登记目前没有只读接口）。' }));

      const lookup = ui.section('按 ID 追溯');
      const selId = ui.nextId('trace-type'), idInId = ui.nextId('trace-id');
      const sel = h('select', { id: selId }, [h('option', { value: '', text: '自动识别类型' })].concat(core.TYPE_ORDER.map(function (t) { return h('option', { value: t, text: core.typeLabel(t) + '（' + t + '）' }); })));
      const idIn = h('input', { id: idInId, type: 'text', maxlength: '40', autocomplete: 'off', spellcheck: 'false', placeholder: '记录 ID（UUIDv7，小写）' });
      const msg = h('p', { class: 'tone-bad', role: 'alert', hidden: true });
      const form = h('form', { class: 'row', autocomplete: 'off' }, [
        h('label', { for: selId, class: 'inline', text: '类型' }), sel,
        h('label', { for: idInId, class: 'inline', text: 'ID' }), idIn,
        h('button', { type: 'submit', class: 'primary', text: '追溯' })
      ]);
      lookup.set([form, msg]);
      form.addEventListener('submit', function (ev) {
        if (ev && ev.preventDefault) ev.preventDefault();
        const id = idIn.value.trim();
        if (!core.isId(id)) { msg.hidden = false; msg.textContent = 'ID 必须是小写 UUIDv7，请从已有记录的详情复制。'; return; }
        msg.hidden = true; msg.textContent = '';
        const type = sel.value;
        ctx.go(core.formatHash({ view: 'trace', type: type || null, id: id }));
      });
      root.appendChild(lookup.node);

      const detailHost = h('section', { class: 'detail-pane panel', 'aria-label': '追溯详情', hidden: true });
      root.appendChild(detailHost);

      // 反馈列表（无 ID 时显示）
      const fb = ui.section('记录列表 · 用户反馈记录与其他类型', { intro: '默认显示用户反馈；从概览或下方类型入口浏览其他类型。这里只读；“已受理”不等于业务完成。' });
      root.appendChild(h('div', { class: 'row', role: 'navigation', 'aria-label': '按类型浏览记录' }, core.TYPE_ORDER.map((t) => ui.a('#/trace/' + t, core.typeLabel(t)))));
      const rs = ctx.shared.ranges.trace || (ctx.shared.ranges.trace = { mode: 'all', date: todayDate(ctx), start: '', end: '' });
      const fbState = { items: [], next: null, dropped: 0, capped: false, appliedRange: null };
      const fbControls = ui.rangeControls(rs, { kinds: ['all', 'day', 'custom'], onApply: function () { loadFeedback(true); } });
      const fbPager = ui.pager({ noun: '条反馈', onMore: function () { loadFeedback(false); } });
      const fbHost = h('div');
      fb.set([fbControls.node, fbHost, fbPager.node]);
      root.appendChild(fb.node);

      async function loadFeedback(first) {
        const mine = ++loadTok;
        if (first) {
          const r0 = core.TYPES[route.type || 'feedback'].time ? fbControls.read() : { ok: true, range: null };
          if (!r0.ok) { ui.fill(fbHost, ui.stateBox('error', r0.error)); return; }
          fbState.appliedRange = r0.range;
        }
        if (first) { fbState.items = []; fbState.next = null; fbState.dropped = 0; fbState.capped = false; ui.fill(fbHost, ui.stateBox('loading', '正在读取反馈…')); }
        fbPager.update({ loaded: fbState.items.length, hasMore: !!fbState.next, loading: true, dropped: fbState.dropped, capped: fbState.capped });
        const browseType = route.type || 'feedback';
        const r = await ctx.data.list(browseType, { range: fbState.appliedRange, limit: 50, cursor: first ? null : fbState.next });
        if (r.stale || !ctx.alive() || mine !== loadTok) return;
        if (!r.ok) { ui.fill(fbHost, ui.errorBox(r.error, function () { loadFeedback(true); })); fbPager.node.hidden = true; return; }
        fbPager.node.hidden = false;
        const m = core.mergeItems(fbState.items, r.page.items, LOAD_CAP);
        fbState.items = m.items; fbState.capped = m.capped; fbState.dropped += r.page.dropped; fbState.next = rememberCursor(fbState, first, r.page.nextCursor);
        fbPager.update({ loaded: fbState.items.length, hasMore: !!fbState.next, loading: false, dropped: fbState.dropped, capped: fbState.capped });
        if (!fbState.items.length) { ui.fill(fbHost, ui.stateBox('empty', '没有读到' + core.typeLabel(browseType) + '。可换时间范围；空列表不代表其他类型没有数据。')); return; }
        const ul = h('ul', { class: 'record-list' });
        fbState.items.forEach(function (rec) {
          const d = core.describeRecord(rec);
          const box = h('div', { class: 'status-box' });
          ul.appendChild(h('li', { class: 'record-row' }, [
            h('div', { class: 'row-link' }, [
              h('span', { class: 'row-title', text: d.headline }),
              h('span', { class: 'row-meta' }, [ui.timeEl(rec.start), ' · ', ui.chip(rec.type, rec.id)])
            ]),
            h('div', { class: 'row' }, [
              ui.a(core.recordHash(rec.type, rec.id), '追溯'),
              rec.type === 'feedback' ? h('button', { type: 'button', text: '查询处理状态', on: { click: function () { loadFeedbackStatus(ctx, rec.id, box); } } }) : null
            ]),
            box
          ]));
        });
        ui.fill(fbHost, ul);
      }

      async function show() {
        if (detail) detail.dispose();
        detail = null;
        const mine = ++resolveTok;
        if (!route.id) {
          detailHost.hidden = true; fb.node.hidden = false;
          fbControls.node.hidden = !core.TYPES[route.type || 'feedback'].time;
          sel.value = route.type || '';
          loadFeedback(true); return;
        }
        fb.node.hidden = true; detailHost.hidden = false;
        if (route.type) { detail = renderDetail(ctx, detailHost, route.type, route.id, route.version, { trace: true }); return; }
        ui.fill(detailHost, ui.stateBox('loading', '正在识别记录类型（最多尝试 17 种类型）…'));
        const r = await ctx.data.resolveAny(route.id);
        if (r.stale || !ctx.alive() || mine !== resolveTok) return;
        if (!r.ok) {
          ui.fill(detailHost, ui.errorBox(r.error));
          if (r.denied) detailHost.appendChild(h('p', { class: 'meta', text: '有 ' + r.denied + ' 种类型返回无读取授权，该 ID 可能属于你无权读取的类型。' }));
          return;
        }
        detail = renderDetail(ctx, detailHost, r.type, route.id, route.version, { trace: true, prefetched: r.rec });
      }
      show();
      return {
        update: function (next) { route = next; show(); },
        dispose: function () { loadTok++; resolveTok++; if (detail) detail.dispose(); }
      };
    }

    return {
      overview: overviewPage,
      timeline: timelinePage,
      people: function (ctx, route) { return entityPage(ctx, route, ENTITY.people); },
      places: function (ctx, route) { return entityPage(ctx, route, ENTITY.places); },
      activities: function (ctx, route) { return entityPage(ctx, route, ENTITY.activities); },
      artifacts: function (ctx, route) { return entityPage(ctx, route, ENTITY.artifacts); },
      rules: function (ctx, route) { return entityPage(ctx, route, ENTITY.rules); },
      actions: function (ctx, route) { return entityPage(ctx, route, ENTITY.actions); },
      trace: tracePage
    };
  }

  return { createData: createData, createPages: createPages, scan: scan, spanRange: spanRange, LOAD_CAP: LOAD_CAP, PROBE_ORDER: PROBE_ORDER };
});

});

// ===== APP =====
define('./ws-app.js', function (module, require) {
/*
 * Ear 业务工作台：启动、会话（令牌只在内存）、哈希路由、侧边栏导航与页面生命周期。
 *
 * 约束（均有测试覆盖）：
 *  - 令牌只存在于 EarWsClient 的闭包：不写入任何存储、地址栏、日志或 DOM（输入框连接后立即清空）；
 *  - 路由只含视图/类型/ID/版本，不含令牌；所有请求都是同源只读 GET；
 *  - 断开、401、令牌被撤销都会清空页面数据、取消排队请求，迟到的响应被丢弃；
 *  - 页面内容只经 textContent / createTextNode 写入。
 *
 * 浏览器里挂在 self.EarWsApp 并自动启动；Node 测试用 require 加载，并注入假的 fetch / 计时器 / DOM。
 */
(function (root, factory) {
  'use strict';
  var isNode = typeof module === 'object' && module !== null && module.exports;
  var deps = isNode
    ? { core: require('./ws-core.js'), client: require('./ws-client.js'), views: require('./ws-views.js'), pages: require('./ws-pages.js') }
    : { core: root.EarWsCore, client: root.EarWsClient, views: root.EarWsViews, pages: root.EarWsPages };
  var api = factory(deps);
  if (isNode) {
    module.exports = api;
  } else {
    root.EarWsApp = api;
    if (typeof document !== 'undefined') api.boot(root, document);
  }
})(typeof self !== 'undefined' ? self : this, function (mods) {
  'use strict';

  const core = mods.core;

  function boot(win, doc, injected) {
    const inj = injected || {};
    const fetchFn = inj.fetch || (win.fetch ? win.fetch.bind(win) : null);
    const timers = {
      setTimeout: inj.setTimeout || win.setTimeout.bind(win),
      clearTimeout: inj.clearTimeout || win.clearTimeout.bind(win)
    };
    const now = inj.now || function () { return Date.now(); };
    const $ = function (id) { return doc.getElementById(id); };

    let zone = core.makeZone('local', inj.offsetAt);
    let session = null;
    let route = core.parseHash('');
    let current = null;
    let pageToken = 0;
    const shared = { ranges: {}, timeline: null };

    const ui = mods.views.createUi(doc, { zone: function () { return zone; } });
    const h = ui.h;
    const client = mods.client.createClient({
      fetch: fetchFn,
      AbortController: inj.AbortController || win.AbortController,
      setTimeout: timers.setTimeout,
      clearTimeout: timers.clearTimeout,
      onAuthFailure: function () { disconnect('令牌无效、已被撤销或格式不对，已断开连接并清除令牌。', 'error'); }
    });
    const data = mods.pages.createData(client);

    // ------------------------------------------------------------ 公共小工具

    function announce(text) { $('live-status').textContent = text || ''; }
    function banner(kind, text) {
      const b = $('banner');
      b.hidden = !text;
      b.textContent = text || '';
      b.className = 'banner' + (kind ? ' ' + kind : '');
    }
    function go(hash) { win.location.hash = hash; }
    function viewLabel(id) {
      const v = core.VIEWS.find(function (x) { return x.id === id; });
      return v ? v.label : '';
    }

    // ------------------------------------------------------------ 导航

    function buildNav() {
      const list = $('nav-list');
      ui.fill(list, core.VIEWS.map(function (v) {
        return h('li', {}, ui.a('#/' + v.id, v.label, { id: 'nav-' + v.id, class: 'nav-link' }));
      }));
    }
    function paintNav(view) {
      core.VIEWS.forEach(function (v) {
        const link = $('nav-' + v.id);
        if (!link) return;
        if (v.id === view) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
      });
    }
    function setNavOpen(open) {
      const side = $('sidebar');
      side.classList.toggle('open', open);
      $('nav-toggle').setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    // ------------------------------------------------------------ 页面生命周期

    function makeCtx(viewEl, token) {
      const scopedData = {};
      Object.keys(data).forEach(function (k) {
        scopedData[k] = function (...args) {
          if (token !== pageToken || !client.connected()) return Promise.resolve({ stale: true });
          return data[k](...args);
        };
      });
      return {
        doc: doc, core: core, ui: ui, data: scopedData, root: viewEl, shared: shared, now: now,
        zone: function () { return zone; },
        session: function () { return session; },
        alive: function () { return token === pageToken && client.connected(); },
        announce: announce,
        go: go
      };
    }
    function disposeCurrent() {
      pageToken++;
      if (current && current.instance && current.instance.dispose) current.instance.dispose();
      current = null;
    }
    function focusTitle() {
      const t = $('view-title') || $('main');
      if (t && t.focus) t.focus();
    }
    function renderDisconnected() {
      disposeCurrent();
      const view = h('div', { class: 'view' }, [
        h('h2', { id: 'view-title', tabindex: '-1', text: '请先连接' }),
        h('p', { class: 'lead-text', text: '在左侧粘贴 API 令牌并点“连接”。令牌只保存在本页内存里，刷新或关闭页面即丢失；本工作台只读取数据，不会修改任何内容。' }),
        h('ul', { class: 'notes' }, [
          h('li', { text: '令牌由部署方在网关配置里给出；普通用户令牌即可浏览，管理员令牌另可使用“插件管理页”。' }),
          h('li', { text: '所有读取都由①按你的身份逐次授权；没有授权的数据会明确显示“无读取授权”，而不是空白。' })
        ])
      ]);
      ui.fill($('view-root'), view);
    }
    function renderRoute() {
      const r = core.parseHash(win.location.hash);
      if (!r.valid) {
        banner('warn', '地址无法识别，已回到概览。');
        route = core.parseHash('');
        if (win.location.hash !== '#/overview') { go('#/overview'); return; }
      }
      route = r.valid ? r : core.parseHash('');
      paintNav(route.view);
      doc.title = 'Ear 工作台 · ' + viewLabel(route.view);
      setNavOpen(false);
      if (!client.connected()) { renderDisconnected(); return; }
      const reusable = current && current.view === route.view && current.instance && current.instance.update &&
        (route.view === 'trace' || core.VIEW_TYPE[route.view]);
      if (reusable) { current.instance.update(route); focusTitle(); return; }
      disposeCurrent();
      const token = pageToken;
      const view = h('div', { class: 'view' });
      ui.fill($('view-root'), view);
      const page = mods.pages.createPages({ ui: ui, doc: doc, zone: function () { return zone; } });
      const instance = page[route.view](makeCtx(view, token), route);
      current = { view: route.view, instance: instance };
      focusTitle();
    }

    // ------------------------------------------------------------ 连接与断开

    function paintSession() {
      const badge = $('session-badge');
      if (session) {
        badge.textContent = '已连接：' + session.subject + '（' + (session.role === 'admin' ? '管理员' : '用户') + '）';
        badge.className = 'badge tone-ok';
        $('session-info').textContent = '已连接。授权由①逐次复核；页面显示的角色来自网关部署配置。';
      } else {
        badge.textContent = '未连接';
        badge.className = 'badge tone-muted';
        $('session-info').textContent = '未连接。';
      }
      $('disconnect-btn').hidden = !session;
      $('connect-btn').disabled = false;
      $('token-input').disabled = !!session;
    }
    function disconnect(message, kind) {
      client.clear();
      data.clear();
      session = null;
      shared.ranges = {};
      shared.timeline = null;
      $('token-input').value = '';
      disposeCurrent();
      paintSession();
      banner(message ? (kind || 'info') : '', message || '');
      announce(message || '已断开并清除令牌。');
      renderDisconnected();
    }
    async function connect() {
      const input = $('token-input');
      const token = input.value;
      input.value = '';
      if (!client.setToken(token)) {
        banner('error', '令牌格式不对：应为 43 位的 URL 安全字符（字母、数字、- 和 _）。');
        return;
      }
      input.value = ''; // 令牌之后只存在于客户端闭包里
      $('connect-btn').disabled = true;
      banner('', '');
      announce('正在连接…');
      const r = await client.get('/v1/me');
      if (r.stale) return;
      if (!r.ok) {
        const msg = r.error ? r.error.message : '连接失败。';
        client.clear();
        paintSession();
        banner('error', '连接失败：' + msg);
        announce('连接失败。');
        return;
      }
      const d = r.data;
      if (!core.isObj(d) || typeof d.subject !== 'string' || (d.role !== 'user' && d.role !== 'admin')) {
        client.clear();
        paintSession();
        banner('error', '连接失败：服务返回的身份信息格式异常。');
        return;
      }
      session = { subject: d.subject.slice(0, 64), role: d.role };
      paintSession();
      announce('已连接。');
      renderRoute();
    }

    // ------------------------------------------------------------ 事件与启动

    buildNav();
    paintSession();
    $('connect-form').addEventListener('submit', function (ev) { if (ev && ev.preventDefault) ev.preventDefault(); connect(); });
    $('disconnect-btn').addEventListener('click', function () { disconnect('', ''); });
    $('zone-select').addEventListener('change', function () {
      zone = core.makeZone($('zone-select').value === 'utc' ? 'utc' : 'local', inj.offsetAt);
      announce('时区已切换为 ' + zone.label(now()) + '；日期与时间输入会按新时区解释。');
      disposeCurrent();
      renderRoute();
    });
    $('nav-toggle').addEventListener('click', function () { setNavOpen($('nav-toggle').getAttribute('aria-expanded') !== 'true'); });
    $('skip-link').addEventListener('click', function () { $('main').focus(); });
    win.addEventListener('hashchange', renderRoute);
    win.addEventListener('pagehide', function () { disconnect('', ''); });
    win.addEventListener('keydown', function (ev) { if (ev && ev.key === 'Escape') setNavOpen(false); });
    renderRoute();

    return {
      client: client, data: data, ui: ui,
      state: function () { return { connected: client.connected(), session: session, route: route, zone: zone.id }; },
      connect: connect,
      disconnect: function () { disconnect('', ''); },
      navigate: go
    };
  }

  return { boot: boot };
});

});
return { core: modules['./ws-core.js'], md: modules['./ws-md.js'], client: modules['./ws-client.js'], views: modules['./ws-views.js'], pages: modules['./ws-pages.js'], app: modules['./ws-app.js'] };
});
