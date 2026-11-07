/*
 * Ear 插件管理页：网络层、有界轮询、请求 ID 复用、一次性领取与 DOM 渲染。
 *
 * 约束（均有测试覆盖）：
 *  - 令牌只存在于 createClient 的闭包变量：不写入任何存储、地址栏、日志或 DOM。
 *  - 页面文字只经 textContent / createTextNode 写入，不解析 HTML。
 *  - 凭据只在管理员点击确认后领取一次；领取不自动触发、不重试。
 *  - 已受理 ≠ 已生效，已启用 ≠ 进程在线，补处理已授权 ≠ 已完成。
 *  - 状态轮询有次数、并发、连续失败三重上限；到限后只提示手动刷新。
 *  - 普通用户令牌不会向任何管理接口发请求。
 *
 * 浏览器里挂在 self.EarApp 并自动启动；Node 测试用 require 加载，并注入假的 fetch / 计时器 / DOM。
 */
(function (root, factory) {
  'use strict';
  var isNode = typeof module === 'object' && module !== null && module.exports;
  var core = isNode ? require('./core.js') : root.EarCore;
  var api = factory(core);
  if (isNode) {
    module.exports = api;
  } else {
    root.EarApp = api;
    if (typeof document !== 'undefined') api.boot(root, document);
  }
})(typeof self !== 'undefined' ? self : this, function (core) {
  'use strict';

  var LIMITS = {
    requestTimeoutMs: 15000,
    maxResponseChars: 262144,
    maxPlugins: 500,
    maxTasks: 200,
    pageSize: 20,
    pollMaxAttempts: 40,
    pollConcurrent: 4,
    pollFailureLimit: 6,
    claimClearMs: 300000,
    maxTypeRows: 128
  };

  var FORBIDDEN_ATTR = /^(on|style|srcdoc|src|href|action|formaction|xlink)/i;
  var CODE_RE = /^[a-zA-Z0-9_]{1,100}$/;

  function has(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }
  function labelOf(map, key, fallback) {
    return typeof key === 'string' && has(map, key) ? map[key] : fallback;
  }
  function qs(params) {
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v !== undefined && v !== null && v !== '') {
        parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(String(v)));
      }
    });
    return parts.length ? '?' + parts.join('&') : '';
  }
  function fmt(iso) {
    var s = iso ? core.formatClock(iso, 'utc') : '';
    return s ? s + ' UTC' : '时间未知';
  }

  // ------------------------------------------------------------ 网络层

  /**
   * 令牌只保存在这个闭包里。request 永不抛异常，统一返回
   * {ok, status, data} 或 {ok:false, error:describeApiError(...), stale, notConnected}。
   * 断开后才返回的响应一律标记 stale，调用方直接丢弃。
   */
  function createClient(opts) {
    var fetchFn = opts.fetch;
    var Abort = opts.AbortController;
    var setT = opts.setTimeout;
    var clearT = opts.clearTimeout;
    var timeoutMs = opts.timeoutMs || LIMITS.requestTimeoutMs;
    var onAuthFailure = typeof opts.onAuthFailure === 'function' ? opts.onAuthFailure : function () {};
    var token = null;
    var generation = 0;

    function fail(err, ctx) {
      var desc = core.describeApiError(err, ctx);
      return { ok: false, status: desc.status, error: desc, notConnected: err.kind === 'not_connected', stale: false };
    }

    function request(method, path, o) {
      o = o || {};
      var ctx = { method: method, claim: o.claim === true };
      if (token === null) return Promise.resolve(fail({ kind: 'not_connected' }, ctx));
      var gen = generation;
      var headers = { Accept: 'application/json', Authorization: 'Bearer ' + token };
      var init = {
        method: method,
        headers: headers,
        cache: 'no-store',
        credentials: 'omit',
        redirect: 'error',
        referrerPolicy: 'no-referrer'
      };
      if (o.body !== undefined) {
        headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(o.body);
      }
      var ctrl = Abort ? new Abort() : null;
      var timedOut = false;
      if (ctrl) init.signal = ctrl.signal;
      var timer = setT(function () {
        timedOut = true;
        if (ctrl) ctrl.abort();
      }, timeoutMs);

      function settle(result) {
        clearT(timer);
        if (gen !== generation) return { ok: false, stale: true, status: 0, error: null };
        if (!result.ok && result.error && result.error.authFailure) onAuthFailure();
        return result;
      }

      return Promise.resolve()
        .then(function () { return fetchFn(path, init); })
        .then(function (res) {
          return res.text().then(function (text) { return { res: res, text: text }; });
        })
        .then(function (r) {
          var data = null;
          var parsed = true;
          if (r.text.length > LIMITS.maxResponseChars) {
            parsed = false;
          } else if (r.text) {
            try { data = JSON.parse(r.text); } catch (e) { parsed = false; }
          }
          if (r.res.ok) {
            if (!parsed || !core.isObj(data)) return settle(fail({ kind: 'parse' }, ctx));
            return settle({ ok: true, status: r.res.status, data: data, stale: false });
          }
          var code = parsed && core.isObj(data) && typeof data.code === 'string' && CODE_RE.test(data.code) ? data.code : null;
          return settle(fail({ kind: 'http', status: r.res.status, code: code }, ctx));
        })
        .catch(function () {
          return settle(fail({ kind: timedOut ? 'timeout' : 'network' }, ctx));
        });
    }

    return {
      setToken: function (value) {
        if (!core.isToken(value)) return false;
        token = value;
        generation++;
        return true;
      },
      clear: function () {
        token = null;
        generation++;
      },
      connected: function () { return token !== null; },
      get: function (path) { return request('GET', path); },
      post: function (path, body) { return request('POST', path, { body: body }); },
      claim: function (feedbackId) {
        if (!core.isId(feedbackId)) {
          return Promise.resolve({ ok: false, stale: false, status: 0, error: core.describeApiError({ kind: 'http', status: 422, code: 'invalid_input' }, { method: 'POST', claim: true }) });
        }
        return request('POST', '/v1/management/credentials/' + feedbackId + '/claim', { claim: true });
      }
    };
  }

  // ------------------------------------------------------------ 有界轮询

  /**
   * 对管理结果做有限次数的状态查询。上限：每个任务最多 maxAttempts 次、
   * 同时最多 maxConcurrent 个任务、连续失败 failureLimit 次即停。
   * 到限不再自动刷新，由调用方提示手动刷新。
   */
  function createPoller(o) {
    var client = o.client;
    var setT = o.setTimeout;
    var clearT = o.clearTimeout;
    var maxAttempts = o.maxAttempts || LIMITS.pollMaxAttempts;
    var maxConcurrent = o.maxConcurrent || LIMITS.pollConcurrent;
    var failureLimit = o.failureLimit || LIMITS.pollFailureLimit;
    var jobs = Object.create(null);
    var epoch = 0;
    var noop = function () {};
    var onUpdate = o.onUpdate || noop;
    var onStop = o.onStop || noop;
    var onError = o.onError || noop;

    function count() { return Object.keys(jobs).length; }

    function finish(id, reason) {
      var job = jobs[id];
      if (!job) return;
      if (job.timer !== null) clearT(job.timer);
      delete jobs[id];
      onStop(id, reason);
    }

    function schedule(id, delay) {
      var job = jobs[id];
      if (!job) return;
      job.timer = setT(function () { tick(id); }, delay);
    }

    function tick(id) {
      var job = jobs[id];
      if (!job || job.inflight) return;
      job.timer = null;
      job.inflight = true;
      var mine = job.epoch;
      client.get('/v1/management/results/' + id).then(function (r) {
        var cur = jobs[id];
        if (!cur || cur.epoch !== mine || r.stale) return;
        cur.inflight = false;
        cur.attempts++;
        if (r.ok) {
          var n = core.normalizeResult(r.data);
          if (!n || n.id !== id) {
            cur.failures++;
          } else {
            cur.failures = 0;
            var view = core.interpretResult(n);
            onUpdate(id, n, view);
            if (view.terminal) return finish(id, 'terminal');
          }
        } else {
          if (r.error && r.error.authFailure) return stopAll('auth');
          cur.failures++;
          onError(id, r.error);
          if (r.status === 403 || r.status === 422) return finish(id, 'rejected');
        }
        if (cur.failures >= failureLimit) return finish(id, 'errors');
        if (cur.attempts >= maxAttempts) return finish(id, 'limit');
        schedule(id, core.backoffDelay(cur.attempts));
      });
    }

    function stopAll(reason) {
      epoch++;
      Object.keys(jobs).forEach(function (id) { finish(id, reason); });
    }

    return {
      track: function (id) {
        if (!core.isId(id)) return false;
        if (jobs[id]) return true;
        if (count() >= maxConcurrent) return false;
        jobs[id] = { attempts: 0, failures: 0, timer: null, inflight: false, epoch: epoch };
        schedule(id, core.backoffDelay(0));
        return true;
      },
      stop: function (id) { finish(id, 'stopped'); },
      stopAll: stopAll,
      isActive: function (id) { return !!jobs[id]; },
      count: count
    };
  }

  // ------------------------------------------------------------ 请求 ID 复用

  /**
   * 结果未知（超时、断网、5xx）之后，只有“内容完全相同”的再次提交才复用同一个
   * request_id（服务端幂等）；内容一改就换新 ID，并提醒上一次可能已被受理。
   */
  function createSubmission(randomFn, nowFn) {
    var pending = null;
    function withId(payload, id) {
      var body = {};
      Object.keys(payload).forEach(function (k) { body[k] = payload[k]; });
      body.request_id = id;
      return body;
    }
    return {
      prepare: function (payload) {
        var fp = core.fingerprint(payload);
        if (pending && pending.fp === fp) {
          return { requestId: pending.requestId, reused: true, supersededUnknown: false, body: withId(payload, pending.requestId) };
        }
        var superseded = pending !== null;
        var id = core.newRequestId(randomFn, nowFn());
        pending = { fp: fp, requestId: id };
        return { requestId: id, reused: false, supersededUnknown: superseded, body: withId(payload, id) };
      },
      settle: function (outcomeUnknown) {
        if (!outcomeUnknown) pending = null;
      },
      reset: function () { pending = null; },
      hasPending: function () { return pending !== null; }
    };
  }

  // ------------------------------------------------------------ 一次性领取

  /** 同一个反馈 ID 在本页最多尝试领取一次；结果未知也不再放开，避免重复消耗。 */
  function createClaimGuard() {
    var attempted = Object.create(null);
    return {
      begin: function (fid) {
        if (!core.isId(fid) || attempted[fid]) return false;
        attempted[fid] = 'inflight';
        return true;
      },
      done: function (fid, outcome) { attempted[fid] = outcome; },
      release: function (fid) { delete attempted[fid]; },
      state: function (fid) { return typeof fid === 'string' && attempted[fid] ? attempted[fid] : null; }
    };
  }

  function claimCredential(client, guard, feedbackId) {
    if (!guard.begin(feedbackId)) return Promise.resolve({ ok: false, blocked: true, error: null });
    return client.claim(feedbackId).then(function (r) {
      if (r.ok) {
        var c = core.normalizeClaim(r.data);
        if (c) {
          guard.done(feedbackId, 'delivered');
          return { ok: true, claim: c };
        }
        guard.done(feedbackId, 'unknown');
        return { ok: false, error: core.describeApiError({ kind: 'parse' }, { method: 'POST', claim: true }) };
      }
      if (r.stale || r.notConnected || (r.error && r.error.authFailure)) {
        // 请求没有到达授权之后的阶段，没有消耗凭据。
        guard.release(feedbackId);
      } else {
        guard.done(feedbackId, r.error && r.error.outcomeUnknown ? 'unknown' : 'rejected');
      }
      return { ok: false, error: r.error, stale: r.stale === true };
    });
  }

  // ------------------------------------------------------------ 页面

  var ACTION_HELP = {
    status: '查询：让①记录并返回该模块当前的配置状态快照。只读，不改变任何东西。',
    register_plugin: '登记：声明插件的类型与初始权限。登记不等于启用，也不会安装或启动插件。',
    enable: '启用：配置上允许访问与事件投递。不代表进程在线。需要先更换并领取过凭据。',
    disable: '停用：阻止新的访问与投递，数据保留，不是删除。',
    set_permissions: '设置权限：整体替换读取/订阅集合，不是追加。',
    rotate_credentials: '更换凭据：旧凭据立即失效；新令牌需要你在任务出现“等待领取”后手动领取一次。',
    revoke_credentials: '撤销凭据：该插件身份立即不能访问；不停用、不删除数据。',
    retry_task: '重试任务：只让因投递租约耗尽或死信而“需要恢复”的原任务重新排队。',
    history_backfill: '历史补处理：只补你明确选择的 UTC 半开区间与输入类型；授权不等于完成。'
  };
  var CLAIM_STATE_TEXT = {
    inflight: '正在领取，请不要关闭页面。',
    delivered: '凭据已领取，令牌只显示过一次。',
    unknown: '领取结果未知：如果没有保存到令牌，只能重新提交“更换凭据”。本页不会自动重试。',
    rejected: '无法领取：凭据已被领取、已过期，或不是你提交的请求。若没有保存令牌，只能重新“更换凭据”。'
  };
  var POLL_STOP_TEXT = {
    limit: '自动刷新已达上限（' + LIMITS.pollMaxAttempts + ' 次），已停止。请点“刷新”手动查询。',
    errors: '连续查询失败，已停止自动刷新。请点“刷新”重试。',
    rejected: '查询被拒绝，已停止自动刷新。'
  };

  function boot(win, doc, deps) {
    deps = deps || {};
    var fetchFn = deps.fetch || function (u, i) { return win.fetch(u, i); };
    var setT = deps.setTimeout || function (f, ms) { return win.setTimeout(f, ms); };
    var clearT = deps.clearTimeout || function (id) { win.clearTimeout(id); };
    var Abort = deps.AbortController || win.AbortController;
    var rand = deps.getRandomValues || function (a) { return win.crypto.getRandomValues(a); };
    var nowFn = deps.now || function () { return Date.now(); };

    function $(id) {
      var node = doc.getElementById(id);
      if (!node) throw new Error('missing element #' + id);
      return node;
    }

    // ---- DOM 小工具：只写 textContent / 文本节点
    function h(tag, attrs, kids) {
      var node = doc.createElement(tag);
      Object.keys(attrs || {}).forEach(function (k) {
        var v = attrs[k];
        if (k === 'class') node.className = v;
        else if (k === 'text') node.textContent = v;
        else if (k === 'disabled' || k === 'hidden' || k === 'checked' || k === 'value' || k === 'readOnly') node[k] = v;
        else if (FORBIDDEN_ATTR.test(k)) throw new Error('attribute not allowed: ' + k);
        else node.setAttribute(k, v);
      });
      (kids || []).forEach(function (c) {
        if (c) node.appendChild(typeof c === 'string' ? doc.createTextNode(c) : c);
      });
      return node;
    }
    function clearNode(node) {
      while (node.firstChild) node.removeChild(node.firstChild);
    }
    function show(node, visible) { node.hidden = !visible; }
    function button(label, onClick, cls) {
      var b = h('button', { type: 'button', class: cls || '', text: label });
      b.addEventListener('click', onClick);
      return b;
    }
    function fillList(ul, items) {
      clearNode(ul);
      items.forEach(function (t) { ul.appendChild(h('li', { text: t })); });
      show(ul, items.length > 0);
    }
    function factList(dl, rows) {
      clearNode(dl);
      rows.forEach(function (r) {
        dl.appendChild(h('dt', { text: r[0] }));
        dl.appendChild(h('dd', { text: r[1] }));
      });
    }

    // ---- 状态
    var state = {
      me: null,
      plugins: [],
      pluginsAfter: null,
      taskMap: Object.create(null),
      tasksCursor: null,
      snapshots: Object.create(null),
      jobs: Object.create(null),
      submitting: false,
      claiming: false
    };
    var revealed = null;
    var revealTimer = null;
    var typeRows = [];
    var dialogResolve = null;
    var dialogProfile = null;
    var dialogModule = '';

    var submission = createSubmission(rand, nowFn);
    var claimGuard = createClaimGuard();
    var client = createClient({
      fetch: fetchFn,
      AbortController: Abort,
      setTimeout: setT,
      clearTimeout: clearT,
      onAuthFailure: function () { disconnect('令牌无效、已被撤销或缺失，已断开。请重新连接。'); }
    });
    var poller = createPoller({
      client: client,
      setTimeout: setT,
      clearTimeout: clearT,
      onUpdate: function (id, n) {
        mergeTask(n);
        refreshViews();
      },
      onStop: function (id, reason) {
        var t = state.taskMap[id];
        if (t && has(POLL_STOP_TEXT, reason)) {
          t.pollNote = POLL_STOP_TEXT[reason];
          renderTasks();
        } else if (!t && has(POLL_STOP_TEXT, reason)) {
          live('没有读到这个任务的结果：' + POLL_STOP_TEXT[reason]);
        }
      },
      onError: function (id, err) {
        if (err && err.message) live('查询进度暂时失败：' + err.message);
      }
    });

    // ---- 提示
    function banner(kind, message) {
      var b = $('banner');
      b.textContent = message || '';
      b.className = 'banner' + (kind ? ' ' + kind : '');
      b.setAttribute('role', kind === 'error' ? 'alert' : 'status');
      show(b, !!message);
    }
    function live(message) { $('live-status').textContent = message || ''; }
    function showError(desc) {
      if (!desc) return;
      banner(desc.outcomeUnknown ? 'warn' : 'error', desc.message);
    }

    // ---- 会话
    function renderSession() {
      var me = state.me;
      $('session-info').textContent = me
        ? '已连接：' + me.subject + '（' + (me.role === 'admin' ? '管理员' : '普通用户') + '）'
        : '未连接。';
      $('token-input').disabled = !!me;
      show($('connect-btn'), !me);
      show($('disconnect-btn'), !!me);
      $('disconnect-btn').disabled = state.claiming;
    }

    function connect(raw) {
      var t = typeof raw === 'string' ? raw.trim() : '';
      $('token-input').value = '';
      if (!core.isToken(t)) {
        banner('error', '令牌格式不对：应是 43 位字母、数字、下划线或连字符。');
        return Promise.resolve();
      }
      client.setToken(t);
      t = null;
      banner('', '');
      live('正在连接…');
      return client.get('/v1/me').then(function (r) {
        live('');
        if (r.stale) return;
        if (!r.ok) {
          client.clear();
          showError(r.error);
          return;
        }
        var me = core.normalizeMe(r.data);
        if (!me) {
          client.clear();
          banner('error', '身份响应格式异常，已断开。');
          return;
        }
        state.me = me;
        renderSession();
        if (me.role === 'admin') {
          show($('admin-area'), true);
          loadPlugins(true);
          loadTasks(true);
        } else {
          show($('user-panel'), true);
        }
      });
    }

    function disconnect(message) {
      poller.stopAll('disconnect');
      client.clear();
      submission.reset();
      dismissToken();
      closeConfirm(false);
      state.me = null;
      state.plugins = [];
      state.pluginsAfter = null;
      state.taskMap = Object.create(null);
      state.tasksCursor = null;
      state.snapshots = Object.create(null);
      state.jobs = Object.create(null);
      state.submitting = false;
      state.claiming = false;
      show($('admin-area'), false);
      show($('user-panel'), false);
      clearNode($('plugins-list'));
      clearNode($('tasks-list'));
      $('submit-btn').disabled = false;
      renderSession();
      live('');
      renderSubmitNote();
      banner(message ? 'info' : '', message || '');
    }

    // ---- 插件列表
    function snapshotLine(snap) {
      if (!snap) return '配置状态：未知（点“查询状态”取一次快照）';
      var word = snap.enabled ? '已启用' : (snap.everEnabled === false ? '未启用（从未启用）' : '已停用');
      return '配置状态：' + word + '（' + fmt(snap.updatedAt) + ' 的结果快照，不是实时）';
    }

    function pluginCard(p) {
      var snap = state.snapshots[p.module];
      var card = h('article', { class: 'card plugin' });
      card.appendChild(h('h3', { text: p.module }));
      var types = h('div', { class: 'meta' });
      p.types.forEach(function (t) {
        var parts = [t.name];
        if (t.kind) parts.push(t.kind);
        if (t.supportsReevaluation) parts.push('可重判');
        if (t.requiresDependencies) parts.push('需依赖');
        types.appendChild(h('span', { class: 'chip', text: parts.join(' · ') }));
      });
      card.appendChild(types);
      card.appendChild(h('p', { class: 'note', text: snapshotLine(snap) }));
      card.appendChild(h('p', { class: 'note', text: '进程在线：未知（没有可信来源；“已启用”不代表进程在线）' }));
      var row = h('div', { class: 'row' });
      row.appendChild(button('填入表单', function () {
        $('module-input').value = p.module;
        updateResumeHint();
        $('module-input').focus();
      }));
      row.appendChild(button('查询状态', function () {
        startSubmit({ action: 'status', module: p.module }, false);
      }));
      card.appendChild(row);
      return card;
    }

    function renderPlugins() {
      var list = $('plugins-list');
      clearNode(list);
      state.plugins.forEach(function (p) { list.appendChild(pluginCard(p)); });
      show($('plugins-empty'), state.plugins.length === 0);
      show($('plugins-more'), !!state.pluginsAfter && state.plugins.length < LIMITS.maxPlugins);
    }

    function loadPlugins(reset) {
      if (!state.me || state.me.role !== 'admin') return Promise.resolve();
      var after = reset ? '' : (state.pluginsAfter || '');
      return client.get('/v1/plugins' + qs({ limit: 50, after: after })).then(function (r) {
        if (r.stale) return;
        if (!r.ok) { showError(r.error); return; }
        var page = core.normalizePlugins(r.data);
        if (reset) state.plugins = [];
        page.items.forEach(function (p) {
          var seen = state.plugins.some(function (q) { return q.module === p.module; });
          if (!seen && state.plugins.length < LIMITS.maxPlugins) state.plugins.push(p);
        });
        state.pluginsAfter = page.nextAfter;
        renderPlugins();
      });
    }

    // ---- 任务列表
    function mergeTask(n) {
      var view = core.interpretResult(n);
      state.taskMap[n.id] = { n: n, view: view, pollNote: '' };
      var snap = core.snapshotFromResult(n);
      if (snap && core.newerSnapshot(state.snapshots[snap.module], snap)) state.snapshots[snap.module] = snap;
      var ids = Object.keys(state.taskMap);
      if (ids.length > LIMITS.maxTasks) {
        sortIds(ids).slice(LIMITS.maxTasks).forEach(function (id) { delete state.taskMap[id]; });
      }
      return view;
    }
    function sortIds(ids) {
      function time(id) {
        var t = core.parseTime(state.taskMap[id].n.updatedAt);
        return isNaN(t) ? -Infinity : t;
      }
      return ids.slice().sort(function (a, b) { return time(b) - time(a); });
    }
    function refreshViews() {
      renderTasks();
      renderPlugins();
      updateResumeHint();
    }

    function factsFor(n, v) {
      var p = n.payload;
      var rows = [];
      if (v.stageLabel) rows.push(['阶段', v.stageLabel]);
      if (p.error_code) rows.push(['错误', v.errorLabel + '（' + p.error_code + '）']);
      if (p.enabled !== null) {
        rows.push(['配置状态', p.enabled ? '已启用（配置）' : (p.ever_enabled === false ? '未启用（从未启用）' : '已停用（配置）')]);
      }
      rows.push(['进程在线', '未知（没有可信来源）']);
      if (p.resources_ready !== null) rows.push(['资源就绪', p.resources_ready ? '是' : '否']);
      if (p.revision !== null) rows.push(['模块修订号', String(p.revision)]);
      if (p.need_recovery_decision === true) rows.push(['恢复选择', '需要：请带修订号选择补处理或跳过']);
      if (p.read_types) rows.push(['批准读取', p.read_types.length ? p.read_types.join('、') : '（无）']);
      if (p.subscribe_types) rows.push(['批准订阅', p.subscribe_types.length ? p.subscribe_types.join('、') : '（无）']);
      if (p.history_execution_status) rows.push(['补处理执行状态', p.history_execution_status]);
      rows.push(['反馈 ID', p.feedback_id || '—']);
      rows.push(['结果 ID', n.id]);
      return rows;
    }

    function jobBlock(jobId) {
      var j = state.jobs[jobId];
      var box = h('div', { class: 'note' });
      if (!j) return null;
      if (j.loading) { box.textContent = '正在查询历史任务…'; return box; }
      if (j.error) { box.textContent = '历史任务查询失败：' + j.error; return box; }
      var job = j.job;
      var dl = h('dl', { class: 'facts' });
      var rows = [
        ['历史任务 ID', job.jobId],
        ['状态', core.jobStateLabel(job.state)],
        ['范围（UTC，半开）', '[' + (job.rangeStart || '未知') + ', ' + (job.rangeEnd || '未知') + ')'],
        ['输入类型', job.inputTypes.length ? job.inputTypes.join('、') : '（无）']
      ];
      Object.keys(job.items).forEach(function (k) { rows.push(['计数 ' + k, String(job.items[k])]); });
      rows.push(['可执行', job.executionAvailable === true ? '是' : (job.executionAvailable === false ? '否：当前没有可用的执行条件，补处理尚未执行' : '未知')]);
      if (job.requiresScheduler === true) rows.push(['说明', '需要调度器执行，不是管理请求本身会完成']);
      if (job.errorCode) rows.push(['错误', core.errorLabel(job.errorCode) + '（' + job.errorCode + '）']);
      factList(dl, rows);
      box.appendChild(h('p', { text: '提醒：补处理“已授权”不等于“已完成”，以这里的状态与计数为准。' }));
      box.appendChild(dl);
      return box;
    }

    function taskCard(id) {
      var t = state.taskMap[id];
      var n = t.n;
      var v = t.view;
      var p = n.payload;
      var card = h('article', { class: 'card task' });
      card.appendChild(h('header', { class: 'task-head' }, [
        h('span', { class: 'badge tone-' + v.tone, text: v.phaseLabel }),
        h('strong', { text: labelOf(core.ACTION_LABELS, p.action, '未知操作') + (p.module ? ' · ' + p.module : '') }),
        h('span', { class: 'meta', text: fmt(n.updatedAt) })
      ]));
      card.appendChild(h('p', { class: 'headline', text: v.headline }));
      if (v.detail) card.appendChild(h('p', { text: v.detail }));
      if (v.notes.length) {
        var notes = h('ul', { class: 'notes' });
        v.notes.forEach(function (x) { notes.appendChild(h('li', { text: x })); });
        card.appendChild(notes);
      }
      var dl = h('dl', { class: 'facts' });
      factList(dl, factsFor(n, v));
      card.appendChild(dl);

      if (!v.terminal && poller.isActive(id)) card.appendChild(h('p', { class: 'note', text: '自动刷新中（有次数上限）…' }));
      else if (t.pollNote) card.appendChild(h('p', { class: 'note', text: t.pollNote }));

      var row = h('div', { class: 'row' });
      row.appendChild(button('刷新', function () { refreshTask(id); }));
      var fid = p.feedback_id;
      if (v.needsClaim && p.credential_claim_required !== false && core.isId(fid)) {
        var cs = claimGuard.state(fid);
        if (cs === null) {
          row.appendChild(button('领取凭据（只能一次）', function () { onClaim(id); }, 'primary'));
          var dead = core.claimDeadline(n);
          if (dead) card.appendChild(h('p', { class: 'note', text: '领取窗口约至 ' + fmt(dead) + '（以服务端为准）。只有提交这次更换的管理员能领取。' }));
        } else {
          card.appendChild(h('p', { class: 'note', text: CLAIM_STATE_TEXT[cs] || '' }));
        }
      }
      if (v.canRetry && p.module && core.isId(fid)) {
        row.appendChild(button('重试任务', function () {
          startSubmit({ action: 'retry_task', module: p.module, target_feedback_id: fid }, false);
        }));
      }
      if (v.historyJobId) {
        row.appendChild(button('查看历史任务', function () { loadJob(v.historyJobId); }));
      }
      card.appendChild(row);
      if (v.historyJobId) {
        var jb = jobBlock(v.historyJobId);
        if (jb) card.appendChild(jb);
      }
      return card;
    }

    function renderTasks() {
      var list = $('tasks-list');
      clearNode(list);
      var ids = sortIds(Object.keys(state.taskMap));
      ids.forEach(function (id) { list.appendChild(taskCard(id)); });
      show($('tasks-empty'), ids.length === 0);
      show($('tasks-more'), !!state.tasksCursor);
    }

    function loadTasks(reset) {
      if (!state.me || state.me.role !== 'admin') return Promise.resolve();
      var module = $('tasks-module').value.trim();
      if (module && !core.isModule(module)) {
        banner('error', '过滤用的模块名不合法。');
        return Promise.resolve();
      }
      var cursor = reset ? '' : (state.tasksCursor || '');
      return client.get('/v1/management/results' + qs({ limit: LIMITS.pageSize, cursor: cursor, module: module })).then(function (r) {
        if (r.stale) return;
        if (!r.ok) { showError(r.error); return; }
        if (reset) state.taskMap = Object.create(null);
        var items = Array.isArray(r.data.items) ? r.data.items.slice(0, 100) : [];
        items.forEach(function (raw) {
          var n = core.normalizeResult(raw);
          if (!n) return;
          var view = mergeTask(n);
          if (!view.terminal) poller.track(n.id);
        });
        var next = r.data.next_cursor;
        state.tasksCursor = typeof next === 'string' && next.length > 0 && next.length <= 512 ? next : null;
        refreshViews();
      });
    }

    function refreshTask(id) {
      return client.get('/v1/management/results/' + id).then(function (r) {
        if (r.stale) return;
        if (!r.ok) { showError(r.error); return; }
        var n = core.normalizeResult(r.data);
        if (!n || n.id !== id) { banner('error', '结果响应格式异常。'); return; }
        var view = mergeTask(n);
        refreshViews();
        if (!view.terminal) poller.track(id);
      });
    }

    function loadJob(jobId) {
      state.jobs[jobId] = { loading: true };
      renderTasks();
      return client.get('/v1/management/history/jobs/' + jobId).then(function (r) {
        if (r.stale) return;
        if (!r.ok) {
          state.jobs[jobId] = { error: r.error.message };
        } else {
          var job = core.normalizeHistoryJob(r.data);
          state.jobs[jobId] = job ? { job: job } : { error: '历史任务响应格式异常。' };
        }
        renderTasks();
      });
    }

    // ---- 确认对话框
    function updateConfirmOk() {
      var p = dialogProfile;
      var okAck = !p || !p.ack || $('confirm-ack').checked;
      var okType = !p || !p.typeWord || $('confirm-type').value === p.typeWord;
      $('confirm-ok').disabled = !(okAck && okType);
    }
    function confirmDialog(profile, rows, moduleName) {
      closeConfirm(false);
      return new Promise(function (resolve) {
        dialogResolve = resolve;
        dialogProfile = profile;
        dialogModule = moduleName;
        $('confirm-title').textContent = profile.title;
        factList($('confirm-summary'), rows.map(function (r) { return [r.label, r.value]; }));
        fillList($('confirm-notes'), profile.notes);
        show($('confirm-ack-row'), !!profile.ack);
        $('confirm-ack').checked = false;
        $('confirm-ack-text').textContent = profile.ack || '';
        show($('confirm-type-row'), !!profile.typeWord);
        $('confirm-type').value = '';
        $('confirm-type-label').textContent = profile.typeWord ? '请输入模块名“' + profile.typeWord + '”以确认' : '';
        var ok = $('confirm-ok');
        ok.textContent = profile.confirmLabel;
        ok.className = profile.danger ? 'danger' : 'primary';
        updateConfirmOk();
        var dlg = $('confirm-dialog');
        if (typeof dlg.showModal === 'function') dlg.showModal();
        else dlg.setAttribute('open', '');
      });
    }
    function closeConfirm(result) {
      var r = dialogResolve;
      dialogResolve = null;
      dialogProfile = null;
      var dlg = $('confirm-dialog');
      if (r) {
        if (typeof dlg.close === 'function') dlg.close();
        else dlg.removeAttribute('open');
        r(result);
      }
    }

    // ---- 凭据领取（只在点击并确认后发生一次）
    function onClaim(id) {
      var t = state.taskMap[id];
      if (!t || state.claiming) return;
      var fid = t.n.payload.feedback_id;
      var moduleName = t.n.payload.module || '';
      if (claimGuard.state(fid) !== null) return;
      var profile = {
        title: '领取新凭据（只能一次）',
        danger: false,
        ack: '我已准备好立刻保存令牌；我明白这是唯一一次机会，丢失后只能重新更换凭据。',
        typeWord: null,
        confirmLabel: '领取',
        notes: [
          '点击后本页向网关发出一次领取请求；令牌只显示一次，只有提交这次更换的管理员能领。',
          '如果响应在途中丢失，令牌无法找回，只能重新“更换凭据”。本页不会自动重试领取。'
        ]
      };
      confirmDialog(profile, [
        { label: '模块', value: moduleName },
        { label: '来自任务（反馈 ID）', value: fid }
      ], moduleName).then(function (ok) {
        if (!ok) return;
        state.claiming = true;
        renderSession();
        renderTasks();
        claimCredential(client, claimGuard, fid).then(function (res) {
          state.claiming = false;
          renderSession();
          if (res.stale) return;
          if (res.ok) {
            revealToken(res.claim);
            banner('info', '凭据已领取。令牌只显示这一次，请立即保存。');
          } else if (res.error) {
            showError(res.error);
          }
          renderTasks();
          refreshTask(id);
        });
      });
    }

    function revealToken(c) {
      revealed = c;
      $('claim-principal').textContent = c.principalId;
      $('claim-token').textContent = c.token;
      $('claim-copy-note').textContent = '';
      show($('claim-panel'), true);
      if (revealTimer !== null) clearT(revealTimer);
      revealTimer = setT(dismissToken, LIMITS.claimClearMs);
    }
    function dismissToken() {
      revealed = null;
      if (revealTimer !== null) {
        clearT(revealTimer);
        revealTimer = null;
      }
      $('claim-principal').textContent = '';
      $('claim-token').textContent = '';
      $('claim-copy-note').textContent = '';
      show($('claim-panel'), false);
    }
    function copyToken() {
      var note = $('claim-copy-note');
      var nav = win.navigator;
      if (!revealed) return;
      if (nav && nav.clipboard && typeof nav.clipboard.writeText === 'function') {
        nav.clipboard.writeText(revealed.token).then(
          function () { note.textContent = '已复制到剪贴板。用完后请清空剪贴板。'; },
          function () { note.textContent = '浏览器不允许自动复制，请手动选中上面的令牌复制。'; }
        );
      } else {
        note.textContent = '这个浏览器不支持自动复制，请手动选中上面的令牌复制。';
      }
    }

    // ---- 表单
    function currentAction() { return $('action-select').value; }

    function addTypeRow() {
      if (typeRows.length >= LIMITS.maxTypeRows) return;
      var row = {};
      row.name = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', maxlength: '128', placeholder: '模块名_类型名' });
      row.kind = h('select', {}, [h('option', { value: '', text: '请选择…' })].concat(
        core.KINDS.map(function (k) { return h('option', { value: k, text: core.KIND_LABELS[k] }); })
      ));
      row.deps = h('input', { type: 'checkbox' });
      row.reeval = h('input', { type: 'checkbox' });
      row.completion = h('input', { type: 'checkbox' });
      row.unique = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false' });
      row.exclusive = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false' });
      row.timeFrom = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false' });
      var remove = button('删除此类型', function () {
        if (typeRows.length <= 1) return;
        typeRows.splice(typeRows.indexOf(row), 1);
        if (row.root.parentNode) row.root.parentNode.removeChild(row.root);
        $('add-type').disabled = false;
      });
      row.root = h('div', { class: 'type-row' }, [
        h('div', { class: 'grid' }, [
          h('div', {}, [h('label', { text: '类型名' }), row.name]),
          h('div', {}, [h('label', { text: '数据种类' }), row.kind])
        ]),
        h('label', { class: 'inline' }, [row.deps, '需要依赖（仅 semantic/rule）']),
        h('label', { class: 'inline' }, [row.reeval, '支持重判（仅 semantic/rule）']),
        h('label', { class: 'inline' }, [row.completion, '这是重判完成记录（模块名_completion，system_record）']),
        h('div', { class: 'grid' }, [
          h('div', {}, [h('label', { text: 'unique_fields（可空）' }), row.unique]),
          h('div', {}, [h('label', { text: 'exclusive_members（可空）' }), row.exclusive]),
          h('div', {}, [h('label', { text: 'time_from（可空）' }), row.timeFrom])
        ]),
        remove
      ]);
      typeRows.push(row);
      $('types-rows').appendChild(row.root);
      $('add-type').disabled = typeRows.length >= LIMITS.maxTypeRows;
    }
    function readRow(row) {
      return {
        name: row.name.value,
        kind: row.kind.value,
        requires_dependencies: row.deps.checked === true,
        supports_reevaluation: row.reeval.checked === true,
        reevaluation_result: row.completion.checked === true,
        unique_fields: row.unique.value,
        exclusive_members: row.exclusive.value,
        time_from: row.timeFrom.value
      };
    }

    function updateForm() {
      var a = currentAction();
      show($('fs-register'), a === 'register_plugin');
      show($('fs-permissions'), a === 'register_plugin' || a === 'set_permissions');
      show($('fs-resume'), a === 'enable' || a === 'set_permissions');
      show($('fs-history'), a === 'history_backfill');
      show($('fs-retry'), a === 'retry_task');
      $('action-help').textContent = labelOf(ACTION_HELP, a, '');
      updateResumeHint();
      updateRangePreview();
    }

    function updateResumeHint() {
      var snap = state.snapshots[$('module-input').value.trim()];
      var hint = $('resume-hint');
      if (!snap) {
        hint.textContent = '还没有该模块的状态快照。若①提示“需要恢复选择”，请先“查询状态”，修订号以结果为准。';
        return;
      }
      var need = snap.needRecoveryDecision === true ? '是（建议勾选“附带恢复选择”）' : (snap.needRecoveryDecision === false ? '否' : '未知');
      hint.textContent = '最近结果（' + fmt(snap.updatedAt) + '）记录的模块修订号：' +
        (snap.revision === null ? '未知' : snap.revision) + '；需要恢复选择：' + need +
        '。修订号过期会被拒绝，不会误执行；你仍需明确选择补处理或跳过。';
    }
    function onResumeToggle() {
      var snap = state.snapshots[$('module-input').value.trim()];
      var rev = $('resume-revision');
      if ($('resume-include').checked && !rev.value.trim() && snap && snap.revision !== null) {
        rev.value = String(snap.revision);
      }
    }

    function updateRangePreview() {
      var zone = $('range-zone').value === 'local' ? 'local' : 'utc';
      var s = core.toUtcIso($('range-start').value, zone);
      var e = core.toUtcIso($('range-end').value, zone);
      var out = $('range-preview');
      if (!s || !e) out.textContent = '请明确填写开始与结束时间；没有默认范围。';
      else if (core.parseTime(s) >= core.parseTime(e)) out.textContent = '开始必须早于结束（半开区间，结束不含）。';
      else out.textContent = '将提交 UTC 半开区间 [' + s + ', ' + e + ')';
    }

    function collectSpec() {
      var a = currentAction();
      var spec = { action: a, module: $('module-input').value.trim() };
      var invalid = [];
      if (a === 'register_plugin') spec.types = typeRows.map(readRow);
      if (a === 'register_plugin' || a === 'set_permissions') {
        var r = core.parseNames($('read-types').value);
        var s = core.parseNames($('subscribe-types').value);
        spec.read_types = r.names;
        spec.subscribe_types = s.names;
        invalid = invalid.concat(r.invalid, s.invalid);
      }
      if (a === 'enable' || a === 'set_permissions') {
        spec.resume = {
          include: $('resume-include').checked === true,
          choice: $('resume-catch-up').checked ? 'catch_up' : ($('resume-skip').checked ? 'skip' : null),
          expected_revision: $('resume-revision').value
        };
      }
      if (a === 'history_backfill') {
        var zone = $('range-zone').value === 'local' ? 'local' : 'utc';
        spec.rangeStart = core.toUtcIso($('range-start').value, zone) || '';
        spec.rangeEnd = core.toUtcIso($('range-end').value, zone) || '';
        var t = core.parseNames($('input-types').value);
        spec.input_types = t.names;
        invalid = invalid.concat(t.invalid);
      }
      if (a === 'retry_task') spec.target_feedback_id = $('retry-target').value.trim();
      spec.invalid_names = invalid;
      return spec;
    }

    function renderSubmitNote() {
      $('submit-note').textContent = submission.hasPending()
        ? '上一次提交的结果未知。内容不变直接再次提交，会复用同一个请求 ID（服务端幂等，不会重复生效）；改动内容会换新 ID。'
        : '';
    }
    function setSubmitting(on) {
      state.submitting = on;
      $('submit-btn').disabled = on;
    }

    function startSubmit(spec, fromForm) {
      if (state.submitting) return Promise.resolve();
      if (!state.me || state.me.role !== 'admin') {
        banner('error', '没有管理权限，未发送请求。');
        return Promise.resolve();
      }
      var built = core.buildRequest(spec);
      if (fromForm) {
        fillList($('form-errors'), built.errors);
        fillList($('form-warnings'), built.warnings);
      }
      if (!built.ok) {
        banner('error', fromForm ? '请求没有通过检查，请看表单里的提示。' : built.errors.join(' '));
        return Promise.resolve();
      }
      banner('', '');
      var profile = core.riskProfile(built.payload);
      var go = profile.confirm
        ? confirmDialog(profile, core.summarize(built.payload), built.payload.module)
        : Promise.resolve(true);
      return go.then(function (ok) {
        if (ok) return send(built.payload);
      });
    }

    function send(payload) {
      if (state.submitting) return Promise.resolve();
      setSubmitting(true);
      var prep = submission.prepare(payload);
      if (prep.supersededUnknown) {
        live('提醒：上一次提交的结果未知，现在内容已改动，将使用新的请求 ID；上一次可能已被受理，请在任务列表核对。');
      }
      return client.post('/v1/management/requests', prep.body).then(function (r) {
        setSubmitting(false);
        if (r.stale) return;
        if (!r.ok) {
          submission.settle(r.error.outcomeUnknown === true);
          showError(r.error);
          renderSubmitNote();
          return;
        }
        var acc = core.normalizeAccepted(r.data);
        if (!acc) {
          submission.settle(true);
          showError(core.describeApiError({ kind: 'parse' }, { method: 'POST' }));
          renderSubmitNote();
          return;
        }
        submission.settle(false);
        renderSubmitNote();
        banner('info', '已受理——这不等于已生效。请在下面的任务卡片里等到“已生效”再下结论。');
        live('已受理：反馈 ' + acc.feedbackId + '，结果 ' + acc.resultId + '。正在查询进度（最多自动刷新 ' + LIMITS.pollMaxAttempts + ' 次）。');
        if (!poller.track(acc.resultId)) {
          live('已受理：反馈 ' + acc.feedbackId + '，结果 ' + acc.resultId + '。同时自动刷新的任务已满，请稍后在下方点“刷新任务”。');
        }
      });
    }

    // ---- 事件
    $('connect-form').addEventListener('submit', function (e) {
      e.preventDefault();
      connect($('token-input').value);
    });
    $('disconnect-btn').addEventListener('click', function () {
      if (state.claiming) return;
      disconnect('已断开，令牌已从本页清除。');
    });
    $('action-select').addEventListener('change', updateForm);
    $('module-input').addEventListener('input', updateResumeHint);
    $('resume-include').addEventListener('change', onResumeToggle);
    $('range-zone').addEventListener('change', updateRangePreview);
    $('range-start').addEventListener('input', updateRangePreview);
    $('range-end').addEventListener('input', updateRangePreview);
    $('add-type').addEventListener('click', addTypeRow);
    $('fill-input-types').addEventListener('click', function () {
      var snap = state.snapshots[$('module-input').value.trim()];
      if (!snap || !snap.readTypes.length) {
        banner('error', '没有该模块已知的读取权限快照，请先“查询状态”，或手动填写输入类型。');
        return;
      }
      $('input-types').value = snap.readTypes.join(' ');
    });
    $('action-form').addEventListener('submit', function (e) {
      e.preventDefault();
      startSubmit(collectSpec(), true);
    });
    $('plugins-refresh').addEventListener('click', function () { loadPlugins(true); });
    $('plugins-more').addEventListener('click', function () { loadPlugins(false); });
    $('tasks-refresh').addEventListener('click', function () { loadTasks(true); });
    $('tasks-more').addEventListener('click', function () { loadTasks(false); });
    $('confirm-ack').addEventListener('change', updateConfirmOk);
    $('confirm-type').addEventListener('input', updateConfirmOk);
    $('confirm-ok').addEventListener('click', function () {
      if ($('confirm-ok').disabled) return;
      closeConfirm(true);
    });
    $('confirm-cancel').addEventListener('click', function () { closeConfirm(false); });
    $('confirm-dialog').addEventListener('cancel', function (e) {
      if (e && typeof e.preventDefault === 'function') e.preventDefault();
      closeConfirm(false);
    });
    $('claim-copy').addEventListener('click', copyToken);
    $('claim-dismiss').addEventListener('click', dismissToken);
    if (win.addEventListener) {
      win.addEventListener('pagehide', function () {
        poller.stopAll('disconnect');
        dismissToken();
      });
    }

    // ---- 初始化
    var select = $('action-select');
    core.ACTIONS.forEach(function (a) {
      select.appendChild(h('option', { value: a, text: core.ACTION_LABELS[a] + '（' + a + '）' }));
    });
    select.value = 'status';
    addTypeRow();
    renderSession();
    renderSubmitNote();
    updateForm();

    return {
      state: state,
      client: client,
      poller: poller,
      connect: connect,
      disconnect: disconnect,
      startSubmit: startSubmit,
      collectSpec: collectSpec,
      loadTasks: loadTasks,
      loadPlugins: loadPlugins
    };
  }

  return {
    LIMITS: LIMITS,
    createClient: createClient,
    createPoller: createPoller,
    createSubmission: createSubmission,
    createClaimGuard: createClaimGuard,
    claimCredential: claimCredential,
    boot: boot
  };
});
