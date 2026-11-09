/*
 * Ear 插件管理页：无 DOM 的纯逻辑（校验、请求构造、结果解释、错误说明）。
 *
 * 浏览器里挂在 self.EarCore；Node 测试用 require 加载。
 * 本文件不访问网络、存储、URL 或 DOM，也不输出日志。校验只是为了尽早给出
 * 可读的错误，真正的权威校验在服务端（ManagementInput 与①）。
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module !== null && module.exports) {
    module.exports = api;
  } else {
    root.EarCore = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // ---------------------------------------------------------------- 常量

  var NAME_RE = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/;
  var ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  var TOKEN_RE = /^[A-Za-z0-9_-]{43}$/;
  var UTC_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;
  var CODE_RE = /^[a-zA-Z0-9_]{1,100}$/;
  var MODULE_MAX = 55;
  var NAME_MAX = 128;
  var MAX_REVISION = 2147483647;
  var CLAIM_WINDOW_SECONDS = 300;
  // 核心模块/保留名：服务端会以 namespace_unavailable 拒绝，页面只做提示。
  var RESERVED_MODULES = [
    'core', 'storage', 'adapter', 'adapters', 'attribution', 'extraction',
    'dependency', 'api', 'llm_gateway'
  ];

  var ACTIONS = [
    'register_plugin', 'enable', 'disable', 'set_permissions', 'status',
    'retry_task', 'rotate_credentials', 'revoke_credentials', 'history_backfill'
  ];
  var ACTION_LABELS = {
    register_plugin: '注册插件',
    enable: '启用',
    disable: '停用',
    set_permissions: '设置权限',
    status: '查询状态',
    retry_task: '重试任务',
    rotate_credentials: '更换凭据',
    revoke_credentials: '撤销凭据',
    history_backfill: '历史补处理'
  };
  var KINDS = ['semantic', 'rule', 'raw_record', 'system_record'];
  var VERSIONED_KINDS = ['semantic', 'rule'];
  var KIND_LABELS = {
    semantic: '语义记录 semantic',
    rule: '规则 rule',
    raw_record: '原始记录 raw_record',
    system_record: '系统记录 system_record'
  };

  var STAGE_LABELS = {
    accepted: '已受理',
    resources: '准备资源',
    authority: '复核授权',
    validation: '校验',
    complete: '完成',
    recovery_required: '需要恢复',
    recovery_queued: '已重新排队',
    claim_authorized: '等待领取凭据'
  };

  var ERROR_LABELS = {
    authority_revoked: '提交者的管理员授权已被撤销，任务没有执行',
    broker_unavailable: '消息服务暂不可用，①会在它恢复后继续处理',
    broker_not_configured: '①尚未配置消息服务，暂时无法就绪',
    reconfiguration_in_progress: '该插件的订阅重配置仍在进行，稍后会继续',
    need_recovery_decision: '该插件需要先做恢复选择（补处理 catch_up 或跳过 skip），并带上修订号重新提交',
    stale_recovery_revision: '提交的修订号已过期：请先刷新状态，再重新选择',
    history_plan_missing: '补处理计划尚未生成，①会继续处理',
    history_input_not_authorized: '补处理的输入类型超出该插件已批准的读取范围',
    plugin_not_registered: '插件尚未登记',
    unknown_permission_type: '权限里含有登记表中不存在的数据类型',
    namespace_unavailable: '模块名或类型名已被占用或属于保留名称',
    identity_not_ready: '插件身份还没有就绪（通常需要先“更换凭据”并领取一次）',
    identity_permissions_not_ready: '插件身份上的权限与已批准的权限尚不一致',
    resource_configuration_changed: '准备期间资源配置发生了变化，请重新提交',
    consumer_reconfiguration_required: '该插件需要先完成订阅重配置',
    consumer_owner_conflict: '消费者名称已被其他身份占用',
    consumer_configuration_changed: '消费者配置与已批准的权限不一致',
    credential_identity_not_ready: '凭据身份未就绪或存在歧义，没有执行更换/撤销',
    retry_target_unavailable: '要重试的原任务不存在',
    retry_target_mismatch: '要重试的原任务属于另一个插件',
    original_authority_revoked: '原任务提交者的管理员授权已撤销，不能重试',
    task_already_completed: '原任务已经完成，无需重试',
    task_not_retryable: '原任务当前不可重试',
    management_worker_required: '①的管理处理器尚未就绪',
    delivery_lease_exhausted: '处理租约多次耗尽，需要管理员确认后重试',
    delivery_dead: '投递已进入死信，需要管理员确认后重试',
    lease_expired: '处理租约已过期',
    management_failed: '执行失败（具体原因已被网关隐去，请查看①的运维记录）',
    input_unavailable: '部分输入已不可读（权限被撤销或数据不可用）',
    stale_inputs: '输入已过期，需要基于当前状态重新处理',
    write_conflict: '写入冲突',
    history_covered: '该范围已被覆盖',
    newrequest_needed: '需要重新提交重判请求'
  };

  var JOB_STATE_LABELS = {
    planned: '已计划',
    authorized: '已授权，等待调度',
    switched: '已切换',
    claimed: '已被所属方领取',
    running: '执行中',
    blocked: '受阻',
    completed: '已完成',
    failed: '失败'
  };

  var PHASES = {
    accepted: { label: '已受理', tone: 'info' },
    processing: { label: '处理中', tone: 'warn' },
    effective: { label: '已生效', tone: 'ok' },
    failed: { label: '失败', tone: 'bad' },
    unknown: { label: '状态未知', tone: 'muted' }
  };

  var SUCCESS_TEXT = {
    register_plugin: ['已登记（未启用）', '插件的类型、所有权和初始权限已登记。登记不等于启用，本页也不会安装或启动插件程序。'],
    enable: ['配置已启用', '①记录该插件为“已启用”：允许访问与事件投递。这不代表插件进程在线——本页没有可信的在线来源。'],
    disable: ['配置已停用', '已阻止新的访问与投递；已提交的写入和数据都保留，不是删除。'],
    set_permissions: ['权限已替换', '读取/订阅集合已整体替换为本次批准的内容。'],
    status: ['状态快照已返回', '这是①在该时刻记录的配置状态，不是实时在线信息。'],
    rotate_credentials: ['凭据已更换，等待领取', '旧凭据已失效；新令牌尚未交付，只能由提交者在约 5 分钟内一次性领取。'],
    revoke_credentials: ['凭据已撤销', '该插件身份不能继续访问；登记与数据保留，这不等于停用或删除。'],
    retry_task: ['重试已执行', '原任务已重新排队；请回到原任务卡片查看它自己的结果。'],
    history_backfill: ['补处理已授权（尚未完成）', '这只表示范围与输入类型已获授权。实际补处理由插件所属方与调度执行，请用“历史任务”查看进度。']
  };

  // ---------------------------------------------------------------- 基础

  function isObj(v) {
    return v !== null && typeof v === 'object' && !Array.isArray(v);
  }
  function isInt(v) {
    return typeof v === 'number' && isFinite(v) && Math.floor(v) === v;
  }
  function isName(v) {
    return typeof v === 'string' && v.length <= NAME_MAX && NAME_RE.test(v);
  }
  function isModule(v) {
    return typeof v === 'string' && v.length <= MODULE_MAX && NAME_RE.test(v);
  }
  function isId(v) {
    return typeof v === 'string' && ID_RE.test(v);
  }
  function isToken(v) {
    return typeof v === 'string' && TOKEN_RE.test(v);
  }
  function isUtc(v) {
    return typeof v === 'string' && UTC_RE.test(v) && !isNaN(Date.parse(v));
  }
  function clip(v, n) {
    var s = typeof v === 'string' ? v : '';
    return s.length > n ? s.slice(0, n) + '…' : s;
  }
  function pad(n, w) {
    var s = String(n);
    while (s.length < w) s = '0' + s;
    return s;
  }
  function has(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  /** UUIDv7（小写），与服务端 is_id 的格式一致。getRandomValues 由调用方注入。 */
  function newRequestId(getRandomValues, nowMs) {
    var r = new Uint8Array(10);
    getRandomValues(r);
    var ms = typeof nowMs === 'number' ? nowMs : Date.now();
    var b = new Uint8Array(16);
    for (var i = 0; i < 6; i++) {
      b[5 - i] = Math.floor(ms / Math.pow(2, 8 * i)) % 256;
    }
    b[6] = 0x70 | (r[0] & 0x0f);
    b[7] = r[1];
    b[8] = 0x80 | (r[2] & 0x3f);
    for (var j = 3; j < 10; j++) b[j + 6] = r[j];
    var hex = '';
    for (var k = 0; k < 16; k++) hex += pad(b[k].toString(16), 2);
    return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' +
      hex.slice(16, 20) + '-' + hex.slice(20);
  }

  function stableStringify(v) {
    if (Array.isArray(v)) return '[' + v.map(stableStringify).join(',') + ']';
    if (isObj(v)) {
      return '{' + Object.keys(v).filter(function (k) { return v[k] !== undefined; }).sort()
        .map(function (k) { return JSON.stringify(k) + ':' + stableStringify(v[k]); }).join(',') + '}';
    }
    return JSON.stringify(v);
  }
  /** 请求内容指纹（不含 request_id）：结果未知后，仅内容完全相同才复用同一请求 ID。 */
  function fingerprint(payload) {
    var copy = {};
    Object.keys(payload).forEach(function (k) {
      if (k !== 'request_id') copy[k] = payload[k];
    });
    return stableStringify(copy);
  }

  /** 以空白、逗号、分号分隔的类型名。返回去重后的合法名和不合法片段。 */
  function parseNames(text) {
    var seen = Object.create(null);
    var names = [];
    var invalid = [];
    String(text == null ? '' : text).split(/[\s,，、;；]+/).forEach(function (t) {
      if (!t) return;
      if (!isName(t)) {
        var shown = clip(t, 40);
        if (invalid.indexOf(shown) < 0) invalid.push(shown);
        return;
      }
      if (!seen[t]) {
        seen[t] = true;
        names.push(t);
      }
    });
    return { names: names, invalid: invalid };
  }
  function uniqueNames(list) {
    var seen = Object.create(null);
    var out = [];
    (Array.isArray(list) ? list : []).forEach(function (n) {
      if (typeof n === 'string' && !seen[n]) {
        seen[n] = true;
        out.push(n);
      }
    });
    return out;
  }
  function diffSets(before, after) {
    var b = uniqueNames(before);
    var a = uniqueNames(after);
    return {
      added: a.filter(function (n) { return b.indexOf(n) < 0; }),
      removed: b.filter(function (n) { return a.indexOf(n) < 0; }),
      kept: a.filter(function (n) { return b.indexOf(n) >= 0; })
    };
  }

  function parseRevision(v) {
    if (typeof v === 'number') return isInt(v) && v >= 1 && v <= MAX_REVISION ? v : null;
    if (typeof v === 'string' && /^[1-9][0-9]{0,9}$/.test(v.trim())) {
      var n = Number(v.trim());
      return n <= MAX_REVISION ? n : null;
    }
    return null;
  }

  // ---------------------------------------------------------------- 时间

  /** datetime-local 文本（YYYY-MM-DDTHH:MM[:SS]）按 UTC 或浏览器本地时区解释，输出 RFC 3339 的 Z 时间。 */
  function toUtcIso(text, zone) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(String(text == null ? '' : text));
    if (!m) return null;
    var y = +m[1], mo = +m[2], d = +m[3], h = +m[4], mi = +m[5], s = m[6] ? +m[6] : 0;
    var date;
    var ok;
    if (zone === 'local') {
      date = new Date(2000, 0, 1, 0, 0, 0, 0);
      date.setFullYear(y, mo - 1, d);
      date.setHours(h, mi, s, 0);
      ok = date.getFullYear() === y && date.getMonth() === mo - 1 && date.getDate() === d &&
        date.getHours() === h && date.getMinutes() === mi && date.getSeconds() === s;
    } else {
      date = new Date(0);
      date.setUTCFullYear(y, mo - 1, d);
      date.setUTCHours(h, mi, s, 0);
      ok = date.getUTCFullYear() === y && date.getUTCMonth() === mo - 1 && date.getUTCDate() === d &&
        date.getUTCHours() === h && date.getUTCMinutes() === mi && date.getUTCSeconds() === s;
    }
    if (!ok || isNaN(date.getTime())) return null;
    return date.toISOString().replace(/\.\d{3}Z$/, 'Z');
  }

  function parseTime(iso) {
    if (typeof iso !== 'string') return NaN;
    // 服务端是微秒精度；部分浏览器只认毫秒，先截断。
    return Date.parse(iso.replace(/(\.\d{3})\d+(Z|[+-]\d{2}:\d{2})$/, '$1$2'));
  }
  function validTime(iso) {
    return typeof iso === 'string' && iso.length <= 40 && !isNaN(parseTime(iso)) ? iso : '';
  }
  function formatClock(iso, zone) {
    var t = parseTime(iso);
    if (isNaN(t)) return '';
    var d = new Date(t);
    var u = zone === 'utc';
    return pad(u ? d.getUTCFullYear() : d.getFullYear(), 4) + '-' +
      pad((u ? d.getUTCMonth() : d.getMonth()) + 1, 2) + '-' +
      pad(u ? d.getUTCDate() : d.getDate(), 2) + ' ' +
      pad(u ? d.getUTCHours() : d.getHours(), 2) + ':' +
      pad(u ? d.getUTCMinutes() : d.getMinutes(), 2) + ':' +
      pad(u ? d.getUTCSeconds() : d.getSeconds(), 2);
  }

  function backoffDelay(attempt) {
    var steps = [1000, 1500, 2000, 3000, 4000, 5000];
    return steps[Math.min(Math.max(attempt, 0), steps.length - 1)];
  }

  // ---------------------------------------------------------------- 请求构造

  function fail(errors) {
    return { ok: false, payload: null, errors: errors, warnings: [] };
  }

  function listNames(value, label, errors) {
    var list = Array.isArray(value) ? value : [];
    var out = uniqueNames(list);
    out.forEach(function (n) {
      if (!isName(n)) errors.push(label + '含有不合法的类型名：' + clip(n, 40));
    });
    if (out.length > 512) errors.push(label + '最多 512 个。');
    return out;
  }

  function buildTypes(module, rows, errors, warnings) {
    var list = Array.isArray(rows) ? rows : [];
    var out = [];
    var seen = Object.create(null);
    if (!list.length) errors.push('至少声明一个数据类型。');
    if (list.length > 128) errors.push('类型声明最多 128 个。');
    var anyReeval = false;
    var hasCompletion = false;
    list.forEach(function (row, i) {
      var label = '第 ' + (i + 1) + ' 个类型';
      row = isObj(row) ? row : {};
      var name = typeof row.name === 'string' ? row.name.trim() : '';
      if (!isName(name)) {
        errors.push(label + '：名称只能用小写字母、数字和单个下划线，以字母开头。');
        return;
      }
      label = '类型 ' + name;
      if (seen[name]) errors.push(label + '：名称重复。');
      seen[name] = true;
      if (isModule(module) && name.indexOf(module + '_') !== 0) {
        errors.push(label + '：必须以“' + module + '_”开头（模块命名空间）。');
      }
      if (KINDS.indexOf(row.kind) < 0) {
        errors.push(label + '：请选择数据种类。');
        return;
      }
      var item = {
        name: name,
        kind: row.kind,
        requires_dependencies: row.requires_dependencies === true,
        supports_reevaluation: row.supports_reevaluation === true
      };
      var versioned = VERSIONED_KINDS.indexOf(row.kind) >= 0;
      if ((item.requires_dependencies || item.supports_reevaluation) && !versioned) {
        errors.push(label + '：“需要依赖/支持重判”只适用于 semantic 或 rule，不适用于 ' + row.kind + '。');
      }
      if (row.reevaluation_result === true) {
        if (!(isModule(module) && name === module + '_completion' && row.kind === 'system_record')) {
          errors.push(label + '：重判完成记录只能是 ' + (module || '<模块>') + '_completion 且为 system_record。');
        }
        item.reevaluation_result = true;
      }
      var unique = parseNames(row.unique_fields);
      var exclusive = parseNames(row.exclusive_members);
      if (unique.invalid.length) errors.push(label + '：unique_fields 含不合法字段名 ' + unique.invalid.join('、'));
      if (exclusive.invalid.length) errors.push(label + '：exclusive_members 含不合法字段名 ' + exclusive.invalid.join('、'));
      if (unique.names.length) item.unique_fields = unique.names;
      if (exclusive.names.length) item.exclusive_members = exclusive.names;
      var timeFrom = typeof row.time_from === 'string' ? row.time_from.trim() : '';
      if (timeFrom) {
        if (!isName(timeFrom)) errors.push(label + '：time_from 不是合法字段名。');
        else item.time_from = timeFrom;
      }
      if (item.supports_reevaluation) anyReeval = true;
      if (name === module + '_completion' && row.kind === 'system_record') hasCompletion = true;
      out.push(item);
    });
    if (anyReeval && !hasCompletion) {
      errors.push('有类型声明了支持重判，必须同时声明 ' + (module || '<模块>') + '_completion（system_record）作为完成记录。');
    }
    if (hasCompletion && !anyReeval) {
      warnings.push('声明了 ' + module + '_completion，但没有任何类型支持重判；它只是一个普通系统记录类型。');
    }
    return out;
  }

  function addPermissions(spec, payload, errors, warnings, replace) {
    var invalid = Array.isArray(spec.invalid_names) ? spec.invalid_names : [];
    if (invalid.length) errors.push('权限里有不合法的类型名：' + invalid.slice(0, 5).join('、'));
    var read = listNames(spec.read_types, '读取权限', errors);
    var sub = listNames(spec.subscribe_types, '订阅权限', errors);
    sub.forEach(function (n) {
      if (read.indexOf(n) < 0) errors.push('订阅不得超出读取范围：' + n + ' 没有读取权限。');
    });
    payload.read_types = read;
    payload.subscribe_types = sub;
    if (replace && !read.length) {
      warnings.push('读取权限为空：提交后该插件不再拥有任何读取与订阅权限。');
    }
  }

  function addResume(spec, payload, errors) {
    var r = spec.resume;
    if (!isObj(r) || r.include !== true) return;
    var ok = true;
    if (r.choice !== 'catch_up' && r.choice !== 'skip') {
      errors.push('请明确选择“补处理（catch_up）”或“跳过（skip）”，页面不会替你默认。');
      ok = false;
    }
    var rev = parseRevision(r.expected_revision);
    if (rev === null) {
      errors.push('恢复选择必须带修订号 expected_revision（1 到 2147483647 的整数）。');
      ok = false;
    }
    if (ok) {
      payload.resume_choice = r.choice;
      payload.expected_revision = rev;
    }
  }

  /**
   * 由表单取值构造管理请求体（不含 request_id）。
   * 返回 {ok, payload, errors, warnings}；ok 为 false 时 payload 为 null。
   */
  function buildRequest(spec) {
    spec = isObj(spec) ? spec : {};
    var action = spec.action;
    if (ACTIONS.indexOf(action) < 0) return fail(['未知操作。']);
    var errors = [];
    var warnings = [];
    var module = spec.module;
    if (!isModule(module)) {
      errors.push('模块名只能用小写字母、数字和单个下划线，以字母开头，最长 55 个字符。');
    }
    var payload = { action: action, module: module };

    if (action === 'register_plugin') {
      if (isModule(module) && RESERVED_MODULES.indexOf(module) >= 0) {
        warnings.push('“' + module + '”是核心或保留模块名，服务端很可能以 namespace_unavailable 拒绝。');
      }
      payload.types = buildTypes(module, spec.types, errors, warnings);
      addPermissions(spec, payload, errors, warnings, false);
    } else if (action === 'set_permissions') {
      addPermissions(spec, payload, errors, warnings, true);
    }
    if (action === 'enable' || action === 'set_permissions') {
      addResume(spec, payload, errors);
    }
    if (action === 'history_backfill') {
      var s = spec.rangeStart;
      var e = spec.rangeEnd;
      if (!isUtc(s) || !isUtc(e)) {
        errors.push('必须明确选择历史范围的开始与结束时间。');
      } else if (parseTime(s) >= parseTime(e)) {
        errors.push('范围是半开区间 [开始, 结束)，开始时间必须早于结束时间。');
      } else {
        payload.rangeStart = s;
        payload.rangeEnd = e;
      }
      var types = listNames(spec.input_types, '输入类型', errors);
      if (Array.isArray(spec.invalid_names) && spec.invalid_names.length) {
        errors.push('输入类型里有不合法的类型名：' + spec.invalid_names.slice(0, 5).join('、'));
      }
      if (!types.length) errors.push('必须至少选择一个输入类型。');
      payload.input_types = types;
    }
    if (action === 'retry_task') {
      if (!isId(spec.target_feedback_id)) errors.push('缺少要重试的原任务反馈 ID。');
      else payload.target_feedback_id = spec.target_feedback_id;
    }
    if (errors.length) return { ok: false, payload: null, errors: errors, warnings: warnings };
    return { ok: true, payload: payload, errors: [], warnings: warnings };
  }

  /** 供确认对话框使用的请求摘要：[{label, value}]。 */
  function summarize(payload) {
    var rows = [];
    rows.push({ label: '操作', value: ACTION_LABELS[payload.action] + '（' + payload.action + '）' });
    rows.push({ label: '模块', value: payload.module });
    if (payload.types) {
      rows.push({
        label: '类型声明',
        value: payload.types.map(function (t) {
          var flags = [];
          if (t.supports_reevaluation) flags.push('支持重判');
          if (t.requires_dependencies) flags.push('需依赖');
          if (t.reevaluation_result) flags.push('重判完成记录');
          return t.name + '（' + t.kind + (flags.length ? '，' + flags.join('、') : '') + '）';
        }).join('；')
      });
    }
    if (payload.read_types) {
      rows.push({ label: '批准读取', value: payload.read_types.length ? payload.read_types.join('、') : '（无）' });
      rows.push({ label: '批准订阅', value: payload.subscribe_types.length ? payload.subscribe_types.join('、') : '（无）' });
    }
    if (payload.resume_choice) {
      rows.push({
        label: '恢复选择',
        value: payload.resume_choice === 'catch_up' ? '补处理（catch_up）' : '跳过（skip）'
      });
      rows.push({ label: '依据修订号', value: String(payload.expected_revision) });
    }
    if (payload.rangeStart) {
      rows.push({ label: '历史范围（UTC，半开）', value: '[' + payload.rangeStart + ', ' + payload.rangeEnd + ')' });
      rows.push({ label: '输入类型', value: payload.input_types.join('、') });
    }
    if (payload.target_feedback_id) rows.push({ label: '原任务反馈 ID', value: payload.target_feedback_id });
    return rows;
  }

  /** 每种操作提交前要说清楚的后果与确认方式。 */
  function riskProfile(payload) {
    var a = payload.action;
    var p = {
      title: ACTION_LABELS[a],
      confirm: true,
      danger: false,
      ack: null,
      typeWord: null,
      confirmLabel: '确认提交',
      notes: []
    };
    var accepted = '提交后只表示“已受理”，请在任务卡片里等到“已生效”再下结论。';
    if (a === 'status') {
      p.confirm = false;
      return p;
    }
    if (a === 'register_plugin') {
      p.title = '确认注册插件声明与初始权限';
      p.notes = [
        '只登记名称、类型、所有权和上面批准的初始权限；不安装、不启动插件程序，也不启用。',
        '登记后，类型名与命名空间由①持久保存，插件无法自行更改或扩大。',
        accepted
      ];
      p.ack = '我已核对类型声明与权限；我明白登记不等于启用，“已受理”不等于“已生效”。';
    } else if (a === 'set_permissions') {
      p.title = '确认整体替换读取/订阅权限';
      p.notes = [
        '这是整体替换：以上批准内容取代该插件现有的全部读取/订阅集合，不是追加。',
        '订阅不超过读取；权限同时覆盖当前与历史版本。',
        '被撤销的读取权限会使依赖它的旧结果无法再更新（旧结果保留，不自动删除）。',
        accepted
      ];
      p.ack = '我已核对权限的增减；我明白这是整体替换。';
    } else if (a === 'enable') {
      p.title = '确认启用插件';
      p.notes = [
        '启用后，该插件被允许访问并接收事件投递；首次启用默认只处理启用之后的变化，不会批量生成历史。',
        '“已启用”只是配置状态，不代表插件进程在线。',
        '需要插件身份已就绪（通常先更换凭据并领取一次）；否则会以 identity_not_ready 失败。',
        accepted
      ];
      if (payload.resume_choice) {
        p.notes.push(payload.resume_choice === 'catch_up'
          ? '恢复选择：补处理——按当前状态处理停用期间的变化，不是盲目重放旧事件。'
          : '恢复选择：跳过——不处理停用期间的变化；旧结果保留但不会因此变成“最新”。');
        p.notes.push('修订号 ' + payload.expected_revision + ' 若已过期，请求会被拒绝而不是误执行。');
      }
      p.ack = '我已核对；我明白已启用不等于进程在线。';
    } else if (a === 'disable') {
      p.title = '确认停用插件';
      p.danger = true;
      p.notes = [
        '阻止新的访问与事件投递；已提交的写入和数据都保留，不会被删除。',
        '之后再启用时，系统会提示期间错过的变化，由你选择补处理或跳过。',
        accepted
      ];
      p.ack = '我确认要停用该插件。';
    } else if (a === 'rotate_credentials') {
      p.title = '确认更换凭据';
      p.danger = true;
      p.notes = [
        '旧凭据在阶段提交时立即失效；仍在使用它的插件进程会被拒绝。',
        '新令牌要在任务显示“等待领取”之后，由你点击“领取”才会交付：只显示一次、只有你能领、约 5 分钟内有效。',
        '领取响应若丢失就无法找回，只能重新更换。',
        accepted
      ];
      p.typeWord = payload.module;
      p.confirmLabel = '确认更换';
    } else if (a === 'revoke_credentials') {
      p.title = '确认撤销凭据';
      p.danger = true;
      p.notes = [
        '撤销后该插件身份立即不能访问；不会停用插件，也不会删除任何数据。',
        '恢复访问需要重新“更换凭据”并领取。',
        accepted
      ];
      p.typeWord = payload.module;
      p.confirmLabel = '确认撤销';
    } else if (a === 'retry_task') {
      p.title = '确认重试原任务';
      p.notes = [
        '只会让因投递租约耗尽或死信而停在“需要恢复”的原任务重新排队；保留原请求与资源，不会扩大任何权限。',
        '①在执行时会再次核对原提交者与你当前的管理员授权。',
        accepted
      ];
      p.ack = '我确认重试这个任务。';
    } else if (a === 'history_backfill') {
      p.title = '确认历史补处理范围';
      p.notes = [
        '范围是 UTC 半开区间 [开始, 结束)；输入类型必须在该插件已批准的读取范围内。',
        '补处理读取当前状态，不是重放旧事件；读取历史作上下文不等于批量生成历史产物，只有这里明确选择的范围才会被补。',
        '“已生效”只表示补处理已获授权，不等于已完成；请在任务卡片里查看历史任务进度。'
      ];
      p.ack = '我已核对范围与输入类型；我明白“已授权”不等于“已完成”。';
    }
    return p;
  }

  // ---------------------------------------------------------------- 响应整理（只保留白名单字段）

  function nameArray(v) {
    if (!Array.isArray(v)) return null;
    return uniqueNames(v.slice(0, 512).filter(isName));
  }
  function bool(v) {
    return typeof v === 'boolean' ? v : null;
  }

  function normalizeMe(raw) {
    if (!isObj(raw) || typeof raw.subject !== 'string' || (raw.role !== 'admin' && raw.role !== 'user')) return null;
    return { subject: clip(raw.subject, 64), role: raw.role };
  }

  function normalizeAccepted(raw) {
    if (!isObj(raw) || raw.status !== 'accepted' || !isId(raw.feedback_id) || !isId(raw.result_id)) return null;
    return { feedbackId: raw.feedback_id, resultId: raw.result_id };
  }

  /** 一次性领取响应。令牌只在这里被校验后原样交给调用方，不复制、不记录。 */
  function normalizeClaim(raw) {
    if (!isObj(raw) || !isId(raw.principal_id) || !isToken(raw.token)) return null;
    return { principalId: raw.principal_id, token: raw.token };
  }

  function normalizePlugins(raw) {
    var out = { items: [], nextAfter: null };
    if (!isObj(raw) || !Array.isArray(raw.items)) return out;
    raw.items.slice(0, 200).forEach(function (it) {
      if (!isObj(it) || !isModule(it.module)) return;
      var types = [];
      (Array.isArray(it.types) ? it.types.slice(0, 128) : []).forEach(function (t) {
        if (!isObj(t) || !isName(t.name)) return;
        types.push({
          name: t.name,
          kind: KINDS.indexOf(t.kind) >= 0 ? t.kind : null,
          supportsReevaluation: t.supports_reevaluation === true,
          requiresDependencies: t.requires_dependencies === true
        });
      });
      out.items.push({
        module: it.module,
        types: types,
        enabled: bool(it.enabled),
        online: bool(it.online)
      });
    });
    out.nextAfter = isModule(raw.next_after) ? raw.next_after : null;
    return out;
  }

  /** 管理结果。注意 payload.revision 是“模块修订号”（恢复选择要带它），record.revision 是记录自身的修订。 */
  function normalizeResult(raw) {
    if (!isObj(raw) || !isId(raw.id)) return null;
    var p = isObj(raw.payload) ? raw.payload : {};
    var o = {};
    ['feedback_id', 'request_id', 'history_job_id', 'recovery_feedback_id', 'target_feedback_id'].forEach(function (k) {
      o[k] = isId(p[k]) ? p[k] : null;
    });
    o.module = isModule(p.module) ? p.module : null;
    o.action = typeof p.action === 'string' ? clip(p.action, 40) : null;
    o.status = typeof p.status === 'string' ? clip(p.status, 40) : null;
    o.stage = typeof p.stage === 'string' ? clip(p.stage, 40) : null;
    if (p.error_code == null) o.error_code = null;
    else o.error_code = typeof p.error_code === 'string' && CODE_RE.test(p.error_code) ? p.error_code : 'management_failed';
    o.history_execution_status = typeof p.history_execution_status === 'string' ? clip(p.history_execution_status, 40) : null;
    ['enabled', 'resources_ready', 'ever_enabled', 'need_recovery_decision', 'credential_claim_required'].forEach(function (k) {
      o[k] = bool(p[k]);
    });
    o.revision = isInt(p.revision) && p.revision >= 0 && p.revision <= MAX_REVISION ? p.revision : null;
    o.read_types = nameArray(p.read_types);
    o.subscribe_types = nameArray(p.subscribe_types);
    o.online = bool(p.online);
    return {
      id: raw.id,
      recordRevision: isInt(raw.revision) ? raw.revision : null,
      updatedAt: validTime(raw.updated_at),
      payload: o
    };
  }

  function normalizeHistoryJob(raw) {
    if (!isObj(raw) || !isId(raw.job_id)) return null;
    var items = {};
    if (isObj(raw.items)) {
      Object.keys(raw.items).slice(0, 20).forEach(function (k) {
        if (isInt(raw.items[k]) && raw.items[k] >= 0) items[clip(k, 40)] = raw.items[k];
      });
    }
    return {
      jobId: raw.job_id,
      owner: isModule(raw.owner) ? raw.owner : null,
      mode: typeof raw.mode === 'string' ? clip(raw.mode, 40) : null,
      inputTypes: nameArray(raw.input_types) || [],
      rangeStart: validTime(raw.rangeStart),
      rangeEnd: validTime(raw.rangeEnd),
      state: typeof raw.state === 'string' ? clip(raw.state, 40) : null,
      items: items,
      executionAvailable: bool(raw.execution_available),
      requiresScheduler: bool(raw.requires_scheduler),
      errorCode: raw.error_code == null ? null : (typeof raw.error_code === 'string' && CODE_RE.test(raw.error_code) ? raw.error_code : 'management_failed')
    };
  }

  // ---------------------------------------------------------------- 结果解释

  function errorLabel(code) {
    if (!code) return '';
    return has(ERROR_LABELS, code) ? ERROR_LABELS[code] : '未收录的错误码';
  }
  function jobStateLabel(state) {
    if (!state) return '未知';
    return has(JOB_STATE_LABELS, state) ? JOB_STATE_LABELS[state] : '未收录的状态';
  }
  function stageLabel(stage) {
    if (!stage) return '';
    return has(STAGE_LABELS, stage) ? STAGE_LABELS[stage] : '未收录的阶段';
  }

  /**
   * 把一条管理结果解释成页面可展示的语义。
   * 关键区分：已受理≠已生效；已启用≠进程在线；补处理已授权≠已完成。
   */
  function interpretResult(n) {
    var p = n.payload;
    var phase = 'unknown';
    if (p.status === 'accepted') phase = 'accepted';
    else if (p.status === 'pending') phase = 'processing';
    else if (p.status === 'succeeded') phase = 'effective';
    else if (p.status === 'failed') phase = 'failed';
    var info = PHASES[phase];
    var out = {
      phase: phase,
      phaseLabel: info.label,
      tone: info.tone,
      headline: '',
      detail: '',
      notes: [],
      terminal: phase === 'effective' || phase === 'failed',
      needsClaim: false,
      canRetry: false,
      errorCode: p.error_code,
      errorLabel: errorLabel(p.error_code),
      stageLabel: stageLabel(p.stage),
      historyJobId: p.history_job_id
    };
    var action = p.action;
    if (phase === 'accepted') {
      out.headline = '已受理，尚未被处理';
      out.detail = '请求已交给①并排队，尚未执行——这不是“已生效”。若长时间停在这里，说明①的管理处理器或消息服务可能没有运行。';
    } else if (phase === 'processing') {
      out.headline = p.stage === 'recovery_queued' ? '已重新排队，等待处理' : '处理中';
      out.detail = out.errorLabel
        ? '①尚未完成：' + out.errorLabel + '。在出现“已生效”之前，不要当作已经生效。'
        : '①正在推进（' + (out.stageLabel || '处理') + '），未完成前不要当作已经生效。';
    } else if (phase === 'effective') {
      var text = has(SUCCESS_TEXT, action) ? SUCCESS_TEXT[action] : ['已生效', '①记录该管理请求已成功执行。'];
      out.headline = text[0];
      out.detail = text[1];
      if (action === 'rotate_credentials' && p.stage === 'claim_authorized') out.needsClaim = true;
      if (action === 'enable' || action === 'status' || action === 'register_plugin') {
        out.notes.push('已启用 ≠ 进程在线：本页没有可信的在线来源，“进程在线”始终显示为未知。');
      }
      if (action === 'history_backfill') {
        out.notes.push('管理成功只表示补处理已授权，不表示补处理已完成。');
      }
    } else if (phase === 'failed') {
      out.headline = '执行失败';
      out.detail = out.errorLabel || '①报告执行失败。';
      if (p.stage === 'recovery_required') {
        out.canRetry = true;
        out.notes.push('这是可恢复的失败：管理员可以用“重试任务”让原任务重新排队。');
      }
    } else {
      out.headline = '状态未知';
      out.detail = '收到了本页不认识的状态“' + clip(p.status || '', 40) + '”，请到①核实。';
    }
    return out;
  }

  /** 可信快照：只有结果里带了配置状态字段才算（任何阶段的结果都可能带）。 */
  function snapshotFromResult(n) {
    var p = n.payload;
    if (!p.module || typeof p.enabled !== 'boolean') return null;
    return {
      module: p.module,
      enabled: p.enabled,
      resourcesReady: p.resources_ready,
      everEnabled: p.ever_enabled,
      revision: p.revision,
      needRecoveryDecision: p.need_recovery_decision,
      readTypes: p.read_types || [],
      subscribeTypes: p.subscribe_types || [],
      updatedAt: n.updatedAt,
      resultId: n.id,
      action: p.action
    };
  }
  /** 只接受比现有快照更新的快照，避免旧任务的结果覆盖新状态。 */
  function newerSnapshot(current, candidate) {
    if (!candidate) return false;
    if (!current) return true;
    var a = parseTime(current.updatedAt);
    var b = parseTime(candidate.updatedAt);
    if (isNaN(b)) return false;
    if (isNaN(a)) return true;
    return b >= a;
  }

  /** 凭据领取窗口的大致截止（以服务端为准）。 */
  function claimDeadline(n) {
    var t = parseTime(n.updatedAt);
    return isNaN(t) ? null : new Date(t + CLAIM_WINDOW_SECONDS * 1000).toISOString();
  }

  // ---------------------------------------------------------------- 错误说明

  var SUBMIT_UNKNOWN = '受理结果未知：请求可能已经被①记录。请不要改动内容，直接再次提交——本页会复用同一个请求 ID，重复提交是幂等的。';
  var CLAIM_UNKNOWN = '凭据投递结果未知：令牌可能已被领取但响应丢失。不要重复领取；如果没有保存到令牌，只能重新提交“更换凭据”。';

  var CODE_MESSAGES = {
    unauthenticated: '令牌无效、已被撤销或缺失，请重新连接。',
    admin_required: '这个操作需要管理员令牌。',
    forbidden: '①拒绝了该请求：当前身份没有这项权限，或授权已被撤销。',
    cross_origin_denied: '请求被同源策略拒绝：部署需要把 public_origin 配置成与浏览器地址栏完全一致的来源。',
    invalid_input: '请求内容没有通过校验，请检查字段（修订号、范围、类型名等）。',
    json_required: '请求格式不正确（需要 JSON）。',
    encoding_not_supported: '请求编码不被支持。',
    body_too_large: '请求内容太大。',
    resource_limit: '请求超出了服务端的资源限制。',
    headers_too_large: '请求头太大。',
    request_target_too_large: '请求地址太长。',
    request_timeout: '请求超时，请稍后重试。',
    storage_unavailable: '①存储服务暂时不可用，请稍后重试。',
    snapshot_unavailable: '读取快照已失效，请重新加载。',
    not_found: '没有找到对应的记录。',
    feedback_conflict: '请求与已有记录冲突。',
    request_rejected: '请求被拒绝。',
    internal_error: '服务端内部错误，请稍后重试。'
  };

  /**
   * 把失败整理成可读说明。err: {kind: 'http'|'network'|'timeout'|'parse'|'not_connected', status, code}
   * ctx: {method: 'GET'|'POST', claim: bool}
   * outcomeUnknown 为 true 时，调用方必须保留请求 ID 以便原样重放，且不得自动重试领取。
   */
  function describeApiError(err, ctx) {
    ctx = ctx || {};
    err = isObj(err) ? err : { kind: 'network' };
    var out = { message: '', outcomeUnknown: false, authFailure: false, status: err.status || 0, code: err.code || null };
    if (err.kind === 'not_connected') {
      out.message = '尚未连接，请先输入令牌。';
      return out;
    }
    if (err.kind === 'network' || err.kind === 'timeout' || err.kind === 'parse') {
      if (ctx.claim) {
        out.outcomeUnknown = true;
        out.message = CLAIM_UNKNOWN;
      } else if (ctx.method === 'POST') {
        out.outcomeUnknown = true;
        out.message = SUBMIT_UNKNOWN;
      } else {
        out.message = err.kind === 'parse' ? '响应格式异常，请稍后重试。' : '网络请求失败或超时，请稍后重试。';
      }
      return out;
    }
    var code = typeof err.code === 'string' ? err.code : null;
    if (err.status === 401) out.authFailure = true;
    if (code === 'management_acceptance_unknown' || code === 'acceptance_unknown_replay_identical_request') {
      out.outcomeUnknown = true;
      out.message = SUBMIT_UNKNOWN;
    } else if (code === 'credential_delivery_unknown') {
      out.outcomeUnknown = true;
      out.message = CLAIM_UNKNOWN;
    } else if (ctx.claim && code === 'forbidden') {
      out.message = '无法领取：凭据已被领取或已过期，这不是你提交的更换请求，或授权已发生变化。若还没有保存令牌，只能重新提交“更换凭据”。';
    } else if (code && has(CODE_MESSAGES, code)) {
      out.message = CODE_MESSAGES[code];
    } else if (ctx.claim && err.status >= 500) {
      out.outcomeUnknown = true;
      out.message = CLAIM_UNKNOWN;
    } else if (ctx.method === 'POST' && err.status >= 500) {
      out.outcomeUnknown = true;
      out.message = SUBMIT_UNKNOWN;
    } else {
      out.message = '请求失败（HTTP ' + (err.status || '?') + '）。';
    }
    return out;
  }

  return {
    ACTIONS: ACTIONS,
    ACTION_LABELS: ACTION_LABELS,
    KINDS: KINDS,
    KIND_LABELS: KIND_LABELS,
    VERSIONED_KINDS: VERSIONED_KINDS,
    RESERVED_MODULES: RESERVED_MODULES,
    ERROR_LABELS: ERROR_LABELS,
    CLAIM_WINDOW_SECONDS: CLAIM_WINDOW_SECONDS,
    isObj: isObj,
    isName: isName,
    isModule: isModule,
    isId: isId,
    isToken: isToken,
    isUtc: isUtc,
    clip: clip,
    newRequestId: newRequestId,
    stableStringify: stableStringify,
    fingerprint: fingerprint,
    parseNames: parseNames,
    uniqueNames: uniqueNames,
    diffSets: diffSets,
    parseRevision: parseRevision,
    toUtcIso: toUtcIso,
    parseTime: parseTime,
    formatClock: formatClock,
    backoffDelay: backoffDelay,
    buildRequest: buildRequest,
    summarize: summarize,
    riskProfile: riskProfile,
    normalizeMe: normalizeMe,
    normalizeAccepted: normalizeAccepted,
    normalizeClaim: normalizeClaim,
    normalizePlugins: normalizePlugins,
    normalizeResult: normalizeResult,
    normalizeHistoryJob: normalizeHistoryJob,
    errorLabel: errorLabel,
    jobStateLabel: jobStateLabel,
    stageLabel: stageLabel,
    interpretResult: interpretResult,
    snapshotFromResult: snapshotFromResult,
    newerSnapshot: newerSnapshot,
    claimDeadline: claimDeadline,
    describeApiError: describeApiError
  };
});
