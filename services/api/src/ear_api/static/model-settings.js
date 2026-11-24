'use strict';

(function () {
  const $ = (id) => document.getElementById(id);
  const connectForm = $('connect-form');
  const tokenInput = $('token-input');
  const editArea = $('edit-area');
  const modelForm = $('model-form');
  const modelInput = $('model-input');
  const status = (message) => { $('status').textContent = message; };
  const endpoint = '/v1/settings/model';
  let token = null;
  let revision = null;
  let pending = null;

  function validModel(value) {
    return typeof value === 'string' &&
      /^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,127}$/.test(value) &&
      !/[^A-Za-z0-9_.:/@+-]/.test(value) && !value.includes('://');
  }

  function clearSelection() {
    revision = null;
    modelForm.hidden = true;
    modelInput.value = '';
    $('saved-model').textContent = '—';
    $('saved-revision').textContent = '—';
  }

  function disconnect(message) {
    if (pending !== null) pending.abort();
    pending = null;
    token = null;
    tokenInput.value = '';
    clearSelection();
    editArea.hidden = true;
    connectForm.hidden = false;
    status(message);
  }

  function options(method, signal) {
    return {
      method, signal, mode: 'same-origin', redirect: 'error', credentials: 'omit',
      cache: 'no-store', referrerPolicy: 'no-referrer', headers: { Accept: 'application/json' }
    };
  }

  async function start() {
    const signal = AbortSignal.timeout(15000);
    try {
      const res = await fetch('/v1/session', options('GET', signal));
      if (!res.ok) throw new Error();
      const { test_no_auth: noAuth } = await res.json();
      if (noAuth === false) {
        connectForm.hidden = false;
        status('请使用管理员 API 令牌连接。');
      } else if (noAuth === true) {
        status('免登录测试模式不开放大模型设置。');
      } else {
        throw new Error();
      }
    } catch (_) {
      status('无法检查登录方式，请刷新页面后重试。');
    }
  }

  async function request(method, model) {
    if (token === null || pending !== null) return;
    const controller = new AbortController();
    pending = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    const init = options(method, controller.signal);
    init.headers.Authorization = 'Bearer ' + token;
    if (method === 'PUT') {
      init.headers['Content-Type'] = 'application/json';
      // The server's revision is an integer, possibly beyond JS's safe number range.
      init.body = '{"model":' + JSON.stringify(model) + ',"revision":' + revision + '}';
    }
    clearSelection();
    $('reload-btn').disabled = true;
    status(method === 'PUT' ? '正在保存…' : '正在读取…');
    try {
      const res = await fetch(endpoint, init);
      if (pending !== controller) return;
      if (res.status === 401 || res.status === 403) {
        disconnect(res.status === 401 ? '令牌无效或已撤销。' : '没有当前管理员权限，或请求未获允许。');
      } else if (res.status === 404) {
        disconnect('此部署未启用大模型设置。');
      } else if (res.status === 409) {
        status('修订已变化，未保存。请重新读取并核对后再编辑。');
      } else if (!res.ok) {
        status('设置不可用或结果无法确认。请重新读取，不要直接重试保存。');
      } else {
        const text = await res.text();
        if (pending !== controller) return;
        if (text.length > 4096) throw new Error();
        const data = JSON.parse(text);
        const rawRevision = text.match(/"revision"\s*:\s*([1-9][0-9]{0,18})(?=\s*[,}])/);
        if (!data || Object.keys(data).length !== 2 || !validModel(data.model) ||
            !rawRevision || typeof data.revision !== 'number' || BigInt(rawRevision[1]) > 9223372036854775807n) {
          throw new Error();
        }
        revision = rawRevision[1];
        $('saved-model').textContent = data.model;
        $('saved-revision').textContent = revision;
        modelInput.value = data.model;
        modelForm.hidden = false;
        status(method === 'PUT' ? '已保存。网关下次启动时读取；本次未调用模型。' : '已读取保存的设置（不是网关运行状态）。');
      }
    } catch (_) {
      if (pending === controller) status('连接失败或结果无法确认。请重新读取设置。');
    } finally {
      clearTimeout(timeout);
      if (pending === controller) {
        pending = null;
        $('reload-btn').disabled = false;
      }
    }
  }

  connectForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (connectForm.hidden || pending !== null) return;
    const candidate = tokenInput.value;
    tokenInput.value = '';
    if (!/^[A-Za-z0-9_-]{43}$/.test(candidate) || candidate.length !== 43) {
      status('请输入 43 位管理员 API 令牌。');
      return;
    }
    token = candidate;
    connectForm.hidden = true;
    editArea.hidden = false;
    request('GET');
  });

  modelForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (revision === null) return;
    if (!validModel(modelInput.value)) {
      status('请输入 1–128 位有效模型标识，不要输入供应商地址。');
      return;
    }
    request('PUT', modelInput.value);
  });

  $('reload-btn').addEventListener('click', () => request('GET'));
  $('disconnect-btn').addEventListener('click', () => disconnect('已断开。'));
  window.addEventListener('pagehide', () => {
    // Without this, back/forward cache could restore an already authenticated page.
    if (token !== null) disconnect('已断开。');
    tokenInput.value = '';
  });
  start();
})();
