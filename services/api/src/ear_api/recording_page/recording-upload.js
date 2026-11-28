'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const tokenInput = $('token-input');
  const fileInput = $('audio-file');
  const jobInput = $('job-id');
  const status = (text) => { $('status').textContent = text; };
  const endpoint = '/v1/recording-uploads';
  const validJob = (id) => typeof id === 'string' && id.length === 36 &&
    /^[a-f0-9]{8}-[a-f0-9]{4}-7[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(id);
  const labels = new Map([
    ['queued', '已排队，等待处理。'], ['processing', '正在处理录音。'],
    ['committing', '正在确认入库结果。'], ['succeeded', '处理完成，已入库。'],
    ['failed', '处理失败。请联系部署者检查。'], ['blocked', '入库结果未知，已停止自动处理。请联系部署者核对，不要重新上传。']
  ]);
  const active = new Set(['queued', 'processing', 'committing']);
  const errors = new Map([
    [401, '令牌无效或授权已撤销。'], [403, '当前录音所有者授权或同源校验未通过。'],
    [404, '未找到任务，或此部署未启用录音上传。'], [408, '接收录音超时。'],
    [409, '同一录音已用不同的开始时间接收，请核对录音时间。'],
    [413, '文件超过此部署的录音大小上限。'], [415, '只支持 WAV 或 M4A 文件。'],
    [422, '录音格式、任务编号或开始时间无效。'], [429, '已有上传正在接收，请稍后重试。']
  ]);
  let noAuth = false;
  let token = null;
  let pending = null;
  let timer = null;

  function cancel() {
    clearTimeout(timer);
    timer = null;
    pending?.abort();
    pending = null;
    $('upload-fields').disabled = $('lookup-fields').disabled = false;
  }

  function disconnect(message) {
    cancel();
    token = null;
    tokenInput.value = '';
    $('upload-form').reset();
    $('lookup-form').reset();
    $('recordings').hidden = !noAuth;
    $('connect-form').hidden = noAuth;
    status(message);
  }

  async function request(path, { file, media, start } = {}, signal) {
    const headers = { Accept: 'application/json' };
    if (token !== null) headers.Authorization = 'Bearer ' + token;
    if (file) {
      headers['Content-Type'] = media;
      headers['X-Recording-Started-At'] = start;
    }
    const res = await fetch(path, {
      method: file ? 'POST' : 'GET', body: file, headers, signal,
      mode: 'same-origin', redirect: 'error', credentials: 'omit',
      cache: 'no-store', referrerPolicy: 'no-referrer'
    });
    if (!res.ok) return { error: res.status };
    return { data: await res.json() };
  }

  async function exchange(path, upload, show) {
    const controller = new AbortController();
    pending = controller;
    // Allow the server's configurable body budget plus local admission/verification time.
    const timeout = setTimeout(() => controller.abort(), upload ? 2000000 : 20000);
    try {
      const reply = await request(path, upload, controller.signal);
      if (pending !== controller) return;
      if (reply.error) {
        const message = errors.get(reply.error) ?? (upload ?
          '无法确认是否已接收。可用同一文件和同一录音时间再次提交，以取回原任务。' :
          '暂时无法读取任务，稍后可手动查询。');
        if (!noAuth && (reply.error === 401 || reply.error === 403)) disconnect(message);
        else status(message);
      } else {
        show(reply.data);
      }
    } catch (_) {
      if (pending === controller) status(upload ?
        '上传中断，未收到回执。可用同一文件和同一录音时间再次提交。' :
        '无法读取处理状态，请手动查询。');
    } finally {
      clearTimeout(timeout);
      if (pending === controller) {
        pending = null;
        $('upload-fields').disabled = $('lookup-fields').disabled = false;
      }
    }
  }

  function showJob(job, remaining, duplicate = false) {
    if (!job || !validJob(job.id) || !labels.has(job.state)) throw new Error();
    jobInput.value = job.id;
    const again = active.has(job.state);
    status((duplicate ? '此录音已接收。' : '') + labels.get(job.state) +
      (again && remaining === 0 ? ' 自动查询已暂停，可手动查询。' : ''));
    if (again && remaining > 0) timer = setTimeout(() => lookup(job.id, remaining - 1), 3000);
  }

  function lookup(id, remaining) {
    cancel();
    $('lookup-fields').disabled = true;
    void exchange(endpoint + '/' + id, undefined, (job) => {
      if (job?.id !== id) throw new Error();
      showJob(job, remaining);
    });
  }

  $('connect-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const candidate = tokenInput.value;
    tokenInput.value = '';
    if (noAuth || candidate.length !== 43 || !/^[A-Za-z0-9_-]{43}$/.test(candidate)) {
      status('请输入 43 位 Ear API 令牌。');
      return;
    }
    token = candidate;
    $('connect-form').hidden = true;
    $('recordings').hidden = false;
    $('disconnect').hidden = false;
    status('请选择文件并填写录音时间，或查询已有任务。');
  });
  $('disconnect').addEventListener('click', () => disconnect('已清除令牌。'));
  window.addEventListener('pagehide', () => disconnect('请重新连接或查询任务。'));

  $('upload-form').addEventListener('submit', (event) => {
    event.preventDefault();
    if ((!noAuth && !token) || $('upload-fields').disabled) return;
    const file = fileInput.files[0];
    const media = /\.m4a$/i.test(file?.name ?? '') ? 'audio/mp4' :
      (/\.wav$/i.test(file?.name ?? '') ? 'audio/wav' : null);
    if (!media || !file?.size) { status('请选择非空 WAV 或 M4A 文件。'); return; }
    const time = $('started-at').value;
    const offset = $('offset').value;
    if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?$/.test(time) ||
        !/^[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(offset) || offset === '-00:00') {
      status('填写录音的开始时间及明确的 UTC 偏移。');
      return;
    }
    const start = time + (time.length === 16 ? ':00' : '') + offset;
    cancel();
    $('upload-fields').disabled = $('lookup-fields').disabled = true;
    status('正在上传录音，请保持页面打开…');
    void exchange(endpoint, { file, media, start }, (job) => {
      if (typeof job?.duplicate !== 'boolean') throw new Error();
      showJob(job, 100, job.duplicate);
      $('upload-form').reset();
    });
  });
  $('lookup-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const id = jobInput.value.trim();
    if ((!noAuth && !token) || $('lookup-fields').disabled) return;
    if (!validJob(id)) { status('请输入完整任务编号。'); return; }
    status('正在读取处理状态…');
    lookup(id, 100);
  });

  void (async () => {
    try {
      const { data } = await request('/v1/session', {}, AbortSignal.timeout(15000));
      if (typeof data?.test_no_auth !== 'boolean' || data.recording_upload !== true) {
        status('此部署未开启录音上传。');
        return;
      }
      noAuth = data.test_no_auth;
      disconnect(noAuth ? '请选择文件并填写录音时间，或查询已有任务。' : '请输入录音所有者的 API 令牌。');
    } catch (_) { status('无法检查上传功能，请刷新页面后重试。'); }
  })();
})();
