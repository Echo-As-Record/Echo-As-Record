/* Real Chromium smoke test of standalone flat assets under production CSP.
 * Synthetic API responses ONLY. This is not live-storage / production acceptance.
 * Enable with EAR_WORKSPACE_BROWSER=1; optional EAR_CHROME points to headless-shell.
 */
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');
const os = require('node:os');
const STATIC = path.resolve(__dirname, '../../src/ear_api/static');
const mock = (() => {
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

const sleep = ms => new Promise(r=>setTimeout(r,ms));
const HEADERS = {
  'Cache-Control':'no-store','Pragma':'no-cache','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
  'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
};
function cdp(url) {
  const socket = new WebSocket(url); let seq=0;
  const pending=new Map(), events=[];
  const ready=new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
  socket.addEventListener('message',event=>{
    const data=JSON.parse(event.data);
    if(data.id && pending.has(data.id)){
      const p=pending.get(data.id);pending.delete(data.id);clearTimeout(p.timer);
      data.error?p.reject(Error(data.error.message)):p.resolve(data.result);
    }else events.forEach(f=>f(data));
  });
  return {ready, on:f=>events.push(f), close:()=>socket.close(), send(method,params={}){
    return new Promise((resolve,reject)=>{const id=++seq;const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout: '+method));},10000);pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));});
  }};
}

test('flat browser: actual DOM/CSP/mobile/session/history/evidence/get-only flows',
  {skip:process.env.EAR_WORKSPACE_BROWSER!=='1', timeout:60000}, async t=>{
  const token='B'.repeat(43), world=mock.createWorld(), handler=mock.createHandler(world);
  const requests=[], assets=[], errors=[], offOrigin=[];
  let denyPeople=false, unauthorized=false;
  const server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://fixture.invalid');
    const send=(status,body,type='application/json')=>{res.writeHead(status,{'Content-Type':type,...HEADERS});res.end(body);};
    const files={'/workspace/':'workspace.html','/workspace/workspace.js':'workspace.js','/workspace/workspace.css':'workspace.css'};
    if(files[url.pathname]){assets.push(url.pathname);const file=files[url.pathname];return send(200,fs.readFileSync(path.join(STATIC,file)),file.endsWith('.html')?'text/html; charset=utf-8':file.endsWith('.js')?'text/javascript; charset=utf-8':'text/css; charset=utf-8');}
    if(!url.pathname.startsWith('/v1/')&&!url.pathname.startsWith('/health/'))return send(404,'{"code":"not_found"}');
    requests.push({method:req.method,url:req.url,hasAuth:!!req.headers.authorization});
    assert.equal(req.method,'GET');assert.ok(!req.url.includes(token));
    if(url.pathname==='/v1/session')return send(200,JSON.stringify({test_no_auth:false}));
    if(!url.pathname.startsWith('/health/')&&(unauthorized||req.headers.authorization!=='Bearer '+token))return send(401,'{"code":"unauthenticated"}');
    if(denyPeople&&url.pathname==='/v1/records/person')return send(403,'{"code":"forbidden"}');
    const result=handler({url:req.url});send(result.status||200,JSON.stringify(result.body));
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  t.after(()=>server.close());
  const origin='http://127.0.0.1:'+server.address().port;
  const cache=path.join(os.homedir(),'.cache/ms-playwright');
  const chrome=process.env.EAR_CHROME || path.join(cache,fs.readdirSync(cache).filter(n=>n.startsWith('chromium_headless_shell')).sort().pop(),'chrome-headless-shell-linux64/chrome-headless-shell');
  assert.ok(fs.existsSync(chrome),'set EAR_CHROME to an existing Chromium executable; no downloads');
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'ear-flat-browser-'));
  t.after(()=>fs.rmSync(profile,{recursive:true,force:true}));
  const proc=spawn(chrome,['--no-sandbox','--disable-gpu','--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:['ignore','ignore','pipe']});
  t.after(()=>proc.kill());
  const debug=await new Promise((resolve,reject)=>{
    const timeout=setTimeout(()=>reject(Error('Chromium startup timeout')),10000);let output='';
    proc.once('error',e=>{clearTimeout(timeout);reject(e);});
    proc.stderr.on('data',b=>{output+=b;const m=/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/.exec(output);if(m){clearTimeout(timeout);resolve(m[1]);}});
  });
  const targets=await (await fetch('http://127.0.0.1:'+debug+'/json')).json();
  const c=cdp(targets.find(x=>x.type==='page').webSocketDebuggerUrl);await c.ready;t.after(()=>c.close());
  c.on(d=>{
    if(d.method==='Runtime.exceptionThrown')errors.push(d.params.exceptionDetails.text);
    if(d.method==='Log.entryAdded' && /Content Security Policy|Refused to/.test(d.params.entry.text))errors.push(d.params.entry.text);
    // Native date inputs may emit a browser-internal data: calendar icon (not a network request).
    if(d.method==='Network.requestWillBeSent'&&!d.params.request.url.startsWith('data:')&&!d.params.request.url.startsWith(origin+'/'))offOrigin.push(d.params.request.url);
  });
  for(const domain of ['Page','Runtime','Log','Network'])await c.send(domain+'.enable');
  const evaluate=async expression=>{const r=await c.send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});assert.equal(r.exceptionDetails,undefined,JSON.stringify(r.exceptionDetails));return r.result.value;};
  const until=async(expression,label)=>{for(let n=0;n<100;n++){if(await evaluate(expression))return;await sleep(40);}throw Error('Timed out: '+label);};
  const text=()=>evaluate('document.getElementById("view-root").textContent');
  const visit=async(hash,needle)=>{await evaluate('location.hash='+JSON.stringify(hash));await until('document.getElementById("view-root").textContent.includes('+JSON.stringify(needle)+')',hash);};
  await c.send('Emulation.setDeviceMetricsOverride',{width:1280,height:900,deviceScaleFactor:1,mobile:false});
  await c.send('Page.navigate',{url:origin+'/workspace/'});
  await until('!!document.getElementById("view-title") && document.getElementById("view-root").textContent.includes("请先连接")','boot');
  assert.ok((await text()).includes('请先连接'));assert.deepEqual(requests.map(r=>r.url),['/v1/session']);
  assert.deepEqual(assets.sort(),['/workspace/','/workspace/workspace.css','/workspace/workspace.js']);
  await evaluate(`document.getElementById('token-input').value=${JSON.stringify(token)};document.getElementById('connect-form').dispatchEvent(new Event('submit',{cancelable:true}));`);
  await until('document.getElementById("session-badge").textContent.includes("tester")','connect');
  assert.equal(await evaluate('document.getElementById("token-input").value'),'');
  await until('document.getElementById("view-root").textContent.includes("有数据")','overview probes');
  for(const [hash,needle] of [
    ['#/timeline','预算会议'],['#/activities/'+mock.id('activity',1),'规则选择'],
    ['#/artifacts/'+mock.id('artifact',1)+'/v/1','正在查看历史版本'],
    ['#/people/'+mock.id('person',1),'合成人物甲'],['#/places/'+mock.id('place',1),'合成示例路'],
    ['#/rules/'+mock.id('rule',1),'提取待办事项'],['#/actions/'+mock.id('suggestion',1),'准备预算表'],
    ['#/trace/person/'+mock.id('person',1),'版本历史'],['#/trace/gps_point','39.99900, 116.32600']]) await visit(hash,needle);
  await visit('#/trace/person/'+mock.id('person',1),'写入时有效');
  assert.ok((await text()).includes('无法展示真实依赖图'));
  await visit('#/artifacts/'+mock.id('artifact',1)+'/v/1','图片已屏蔽');
  const danger=await evaluate(`(()=>{const r=document.getElementById('view-root');return {scripts:r.querySelectorAll('script').length,images:r.querySelectorAll('img').length,external:[...r.querySelectorAll('a')].filter(a=>!a.getAttribute('href').startsWith('#/')).length,inline:r.querySelectorAll('[style],[onclick],[onerror]').length};})()`);
  assert.deepEqual(danger,{scripts:0,images:0,external:0,inline:0});
  await visit('#/timeline','预算会议');
  await evaluate(`const f=document.querySelector('.search-form');f.querySelector('input').value='下周三';f.dispatchEvent(new Event('submit',{cancelable:true}));`);
  await until('document.querySelector(".evidence-hits")?.textContent.includes("合成人物甲")','evidence search');
  assert.ok((await text()).includes('实时尽力而为'));
  await c.send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  assert.notEqual(await evaluate('getComputedStyle(document.getElementById("nav-toggle")).display'),'none', 'flex layout may blockify the visible button');
  await evaluate('document.getElementById("nav-toggle").click()');
  assert.equal(await evaluate('document.getElementById("nav-toggle").getAttribute("aria-expanded")'),'true');
  await evaluate('document.getElementById("nav-people").click()');
  await until('document.title.includes("人物")','mobile navigation');
  assert.equal(await evaluate('document.getElementById("nav-toggle").getAttribute("aria-expanded")'),'false');
  assert.equal(await evaluate('document.activeElement.id'),'view-title');
  assert.ok(await evaluate('document.documentElement.scrollWidth <= innerWidth + 1'),'mobile document should not overflow');
  denyPeople=true;
  await visit('#/rules','规则列表');await visit('#/people','没有读取');
  assert.ok(!(await text()).includes('合成人物甲'),'403 replaces data, not stale cache');
  denyPeople=false;unauthorized=true;
  await visit('#/activities','请先连接');
  assert.ok(!(await text()).includes('预算会议'));assert.equal(await evaluate('document.getElementById("session-badge").textContent'),'未连接');
  assert.equal(await evaluate('localStorage.length+sessionStorage.length'),0);
  assert.equal(requests.filter(r=>r.method!=='GET').length,0);
  assert.ok(requests.filter(r=>r.url.startsWith('/v1/')&&r.url!=='/v1/session').every(r=>r.hasAuth));
  assert.deepEqual(offOrigin,[]);assert.deepEqual(errors,[]);
  t.diagnostic('Real Chromium + synthetic same-origin API; '+requests.length+' GET requests; no external requests/CSP violations; desktop and 390px mobile checks passed.');
  // Close before removing the isolated browser profile, including on assertion failures.
  c.close();proc.kill();await new Promise(r=>proc.once('exit',r));
});

test('results browser: compact dates, aligned reading, animated sidebar, compact source timeline with automatic paging and GET only',
  {skip: process.env.EAR_WORKSPACE_BROWSER !== '1', timeout: 60000}, async t => {
  const world = mock.createWorld(), id = mock.id('activity', 1);
  const o = {start: '2026-10-29T09:00:00Z', end: '2026-10-29T09:30:00Z'};
  const v1 = world.records.find(r => r.id === id);
  world.records.push(mock.sem('activity', 1, 2, {...v1.payload, title_md: '新版预算会议'}, o));
  world.records.push(mock.sem('activity', 7, 1, {...v1.payload, title_md: '已替代活动'}, {...o, status: 'superseded'}));
  world.records.push(mock.sem('activity', 8, 1, {...v1.payload, title_md: '重叠但不同活动'}, o));
  world.records.push(mock.sem('artifact', 2, 1, {activity_id: id, body_md: '# 第二份笔记\n\n行内 $a^2+b^2=c^2$。\n\n![外部图片](https://tracker.example/pixel.png) [链接](https://example.test) <img src=x onerror=alert(1)>'}, o));
  world.records.push(mock.sem('artifact', 3, 1, {activity_id: mock.id('activity', 8), body_md: '不应串入的笔记'}, o));
  const late = {start:'2026-10-29T22:00:00Z', end:'2026-10-29T22:30:00Z'}, lateId = mock.id('activity', 9);
  world.records.push(mock.sem('activity', 9, 1, {title_md: '傍晚记录', description_md:'这是当天最后一项。'}, late));
  world.records.push(mock.sem('artifact', 9, 1, {activity_id: lateId, body_md: '下午的独立笔记'}, late));
  const handler = mock.createHandler(world), reqs = [], errors = [], external = [];
  let denyNotes = true, denyCoverage = true, noAuth = true;
  const token = 'A'.repeat(43);
  const event = (n, kind, source, hour, text='', title='', role=null) => ({id:mock.id('audio',100+n),kind,source,
    start:`2026-10-29T${hour}:00:00Z`,end:`2026-10-29T${hour}:00:00Z`,title,text,role,truncated:false});
  const formulas = '\n\n' + String.raw`行内 **\(x_i^2\)**、$\sqrt{2}$，金额 $5 和 $10、转义 \$x$、` + '`$not_math$`' + '\n\n' + String.raw`\[
\begin{aligned}
 f(x)&=\frac{1}{1+x^2}\\
 g(x)&=2
\end{aligned}
\]

$$\sum_{i=1}^{n} i$$

不可读：$\notarealcommand{x}$。不可信：$\includegraphics{https://tracker.example/math.svg}$。`;
  const sourceItems = [event(1,'audio_ref','legacy-ear','08'),event(2,'transcript_segment','legacy-ear','10','合成录音'),
    event(3,'chat_session','librechat','09','','合成会话'),event(4,'chat_message','librechat','12','**消息正文**\n\n- 第一条\n- 第二条\n\n```js\nlet n = 1;\n```\n\n<img src=https://tracker.example/evil.png> ![图](https://tracker.example/p.png) [链接](https://tracker.example)' + formulas,'','assistant'),
    event(5,'chat_attachment','librechat','13','','合成附件.txt'),event(6,'audio_ref','native-ear','14')];
  const longDay = '2026-10-30', sparseDay = '2026-10-31';
  const streamEvents = Array.from({length:52}, (_, i) => {
    const stamp = `${longDay}T09:${String(i).padStart(2,'0')}:00Z`;
    return {...event(20+i,'audio_ref','legacy-ear','09'),start:stamp,end:stamp};
  });
  let pauseNext = true;
  const server = http.createServer((req, res) => {
    const {pathname, search} = new URL(req.url, 'http://fixture.invalid');
    const send = (code, body, kind = 'application/json') => { res.writeHead(code, {'Content-Type': kind, ...HEADERS}); res.end(body); };
    const files = {'/results/': 'results.html', '/results/results.js': 'results.js', '/results/results.css': 'results.css', '/results/katex.min.js': 'katex.min.js', '/workspace/workspace.js': 'workspace.js'};
    const file = files[pathname];
    if (file) return send(200, fs.readFileSync(path.join(STATIC, file)), file.endsWith('.html') ? 'text/html; charset=utf-8' : file.endsWith('.js') ? 'text/javascript; charset=utf-8' : 'text/css');
    const q = new URLSearchParams(search);
    reqs.push({method: req.method, path: pathname, start: q.get('start'), source:q.get('source'), kind:q.get('kind'), cursor:q.get('cursor'), auth: req.headers.authorization === 'Bearer ' + token});
    if (pathname === '/v1/session') return send(200, JSON.stringify({test_no_auth:noAuth,chat_evidence_read:true,recording_upload:true}));
    if (!noAuth && req.headers.authorization !== 'Bearer ' + token) return send(401, '{"code":"unauthenticated"}');
    if (pathname === '/v1/source-timeline') {
      const kind = q.get('kind'), source = q.get('source'), cursor = q.get('cursor');
      if (q.get('start') === longDay+'T04:00:00Z') {
        if (kind !== 'audio_ref') return send(200, JSON.stringify({items:[],next_cursor:null}));
        if (cursor === '32' && pauseNext) return send(503, '{"code":"upstream_unavailable"}');
        const offset = Number(cursor || 0);
        return send(200, JSON.stringify({items:streamEvents.slice(offset, offset+16),next_cursor:offset+16<streamEvents.length?String(offset+16):null}));
      }
      if (q.get('start') === sparseDay+'T04:00:00Z') {
        const offset = Number(cursor || 0);
        if (kind !== 'audio_ref') return send(200, JSON.stringify({items:[],next_cursor:null}));
        if (offset < 4) return send(200, JSON.stringify({items:[],next_cursor:String(offset+1)}));
        if (offset === 5) return send(200, JSON.stringify({items:[],next_cursor:null}));
        const stamp = `${sparseDay}T10:00:00Z`;
        return send(200, JSON.stringify({items:[{...event(85,'audio_ref','legacy-ear','10'),start:stamp,end:stamp}],next_cursor:'5'}));
      }
      if (q.get('start') !== '2026-10-29T04:00:00Z') return send(200, JSON.stringify({items:[],next_cursor:null}));
      if (kind === 'audio_ref' && !cursor) return send(200, JSON.stringify({items:[],next_cursor:'audio-next'}));
      return send(200, JSON.stringify({items:sourceItems.filter(r=>r.kind===kind && (source==='all'||r.source===source)),next_cursor:null}));
    }
    if (pathname === '/v1/week-coverage') {
      if (denyCoverage) return send(503, '{"code":"upstream_unavailable"}');
      const days = q.get('days').split(',');
      const makeTrack = (track) => ({state:'ready',days:days.map(pair=>{
        const [start,end] = pair.split('/'), count = Math.ceil((Date.parse(end)-Date.parse(start))/300000);
        const bits = Array(count).fill('0');
        if (start.startsWith('2026-10-29')) { bits[60] = '1'; if (track==='timeline') bits[108] = '1'; }
        if (start.startsWith('2026-10-31') && track==='timeline') bits[86] = '1';
        return bits.join('');
      })});
      return send(200, JSON.stringify({slot_seconds:300,timeline:makeTrack('timeline'),activity:makeTrack('activity')}));
    }
    if (pathname.endsWith('/history')) return send(413, '{"code":"resource_limit"}');
    if (denyNotes && pathname === '/v1/records/artifact') return send(503, '{"code":"upstream_unavailable"}');
    const r = handler({url: (pathname === '/v1/timeline' ? '/v1/records/activity' : pathname) + search});
    send(r.status || 200, JSON.stringify(r.body));
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(() => { server.close(); server.closeAllConnections(); });
  const origin = 'http://127.0.0.1:' + server.address().port;
  const cache = path.join(os.homedir(), '.cache/ms-playwright');
  const chrome = process.env.EAR_CHROME || path.join(cache, fs.readdirSync(cache).filter(n => n.startsWith('chromium_headless_shell')).sort().pop(), 'chrome-headless-shell-linux64/chrome-headless-shell');
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'ear-results-browser-'));
  const proc = spawn(chrome, ['--no-sandbox','--disable-gpu','--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--user-data-dir=' + profile,'about:blank'], {stdio:['ignore','ignore','pipe']});
  const exited = new Promise(r => { proc.once('exit', r); proc.once('error', r); });
  t.after(async () => { proc.kill(); await exited; fs.rmSync(profile, {recursive:true,force:true}); });
  const debug = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(Error('Chromium startup timeout')), 10000);
    proc.once('error', e => { clearTimeout(timeout); reject(e); });
    proc.stderr.on('data', b => { const m = /DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/.exec(String(b)); if (m) { clearTimeout(timeout); resolve(m[1]); } });
  });
  const targets = await (await fetch('http://127.0.0.1:' + debug + '/json')).json();
  const c = cdp(targets.find(x => x.type === 'page').webSocketDebuggerUrl); await c.ready; t.after(() => c.close());
  c.on(d => {
    if (d.method === 'Runtime.exceptionThrown') errors.push(d.params.exceptionDetails.text);
    if (d.method === 'Log.entryAdded' && /Content Security Policy|Refused to/.test(d.params.entry.text)) errors.push(d.params.entry.text);
    if (d.method === 'Network.requestWillBeSent' && !d.params.request.url.startsWith(origin + '/') && !d.params.request.url.startsWith('data:')) external.push(d.params.request.url);
  });
  for (const domain of ['Page','Runtime','Log','Network']) await c.send(domain + '.enable');
  const evaluate = async expression => {
    const r = await c.send('Runtime.evaluate', {expression,awaitPromise:true,returnByValue:true});
    assert.equal(r.exceptionDetails, undefined, JSON.stringify(r.exceptionDetails)); return r.result.value;
  };
  const until = async (expression, label) => {
    for (let n = 0; n < 120; n++) { if (await evaluate(expression)) return; await sleep(35); }
    throw Error('Timed out: ' + label + ' ' + JSON.stringify({requests: reqs.slice(-7), ui: await evaluate('document.getElementById("connection-status")?.textContent'), search: await evaluate('location.search')}));
  };
  let scheme = 'light', motion = 'no-preference';
  const media = () => c.send('Emulation.setEmulatedMedia', {features: [
    {name:'prefers-color-scheme',value:scheme},{name:'prefers-reduced-motion',value:motion}]});
  const setScheme = value => { scheme = value; return media(); };
  const setMotion = value => { motion = value; return media(); };
  const appearance = () => evaluate(`(() => {
    const style = (sel, property) => getComputedStyle(document.querySelector(sel))[property];
    return {page: style('body', 'backgroundColor'), panel: style('.reader-pane', 'backgroundColor'),
      sidebar: style('.list-panel', 'backgroundColor'), ink: style('body', 'color'),
      muted: style('.axis-marker', 'color'), scheme: style('#previous', 'colorScheme')};
  })()`);
  const contrast = (a, b) => {
    const light = (rgb) => rgb.match(/\d+/g).reduce((sum, n, i) => {
      const v = Number(n) / 255;
      return sum + [0.2126, 0.7152, 0.0722][i] * (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
    }, 0);
    const x = light(a), y = light(b);
    return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
  };
  const day = '2026-10-29';
  const first = `.feed-activity[data-activity="${id}"]`;
  const later = `.feed-activity[data-activity="${lateId}"]`;
  const textIn = (sel, str) => `document.querySelector(${JSON.stringify(sel)})?.textContent.includes(${JSON.stringify(str)})`;
  const marker = `.axis-marker[data-activity="${lateId}"]`;
  const toggle = 'document.getElementById("sidebar-toggle").click()';
  const go = async (target) => c.send('Page.navigate', {url: origin + target});
  const click = (selector) => evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
  await setScheme('light');
  await c.send('Emulation.setTimezoneOverride', {timezoneId: 'America/New_York'});
  await c.send('Emulation.setDeviceMetricsOverride', {width:1280,height:500,deviceScaleFactor:1,mobile:false});
  await go('/results/?date=' + day);
  await until(textIn(first,'读取笔记失败'), 'first note failure');
  await until('!document.querySelector(".coverage-mini [data-state=pending]")', 'weekly previews settle');
  assert.ok(await evaluate('!document.querySelector("body > header, #week-range, #zone, .day details, .day ul, .back-link, .panel-fold")'));
  assert.equal(await evaluate('document.querySelector("#list-title").textContent'), '活动与笔记');
  assert.deepEqual(await evaluate('[...document.querySelectorAll(".list-head nav button")].map(x=>x.id)'), ['previous','next']);
  assert.equal(await evaluate('document.querySelector(".list-panel footer a").pathname'), '/workspace/');
  assert.equal(await evaluate('document.querySelector("#recording-upload-link:not([hidden])")?.pathname'), '/recording-upload/');
  assert.deepEqual(await evaluate('[...document.querySelectorAll(".icon-rail .rail-icon")].map(x=>x.id)'), ['sidebar-toggle','results-view','timeline-view']);
  assert.deepEqual(await evaluate('[...document.querySelectorAll(".day")].map(x=>x.dataset.date)'),
    ['2026-10-26','2026-10-27','2026-10-28','2026-10-29','2026-10-30','2026-10-31','2026-11-01']);
  assert.equal(await evaluate('document.querySelector(".day[aria-current=date]").dataset.date'), day);
  assert.equal(await evaluate('document.querySelectorAll(".coverage-mini svg").length'), 14);
  assert.ok(await evaluate('!document.querySelector(".coverage-mini :is(text,title,line)")'));
  assert.ok(await evaluate('!document.getElementById("reader").textContent.includes("已替代活动")'));
  const timelineLayout = () => evaluate(`(() => {
    const rect = e => e.getBoundingClientRect();
    const cards = [...document.querySelectorAll('.timeline-list > .feed-activity')];
    const flat = ['#reading-date','.reading-head','.feed-activity','.axis-marker','.timeline-list'].every(sel => {
      const css = getComputedStyle(document.querySelector(sel));
      return css.backgroundColor === 'rgba(0, 0, 0, 0)' && css.boxShadow === 'none' &&
        ['borderTopWidth','borderRightWidth','borderBottomWidth','borderLeftWidth'].every(side => css[side] === '0px');
    });
    return {flat, date:rect(document.getElementById('reading-date')).top, scroll:document.getElementById('reader').scrollTop,
      rows:cards.map(card => {
        const title = card.querySelector('h3'), marker = card.querySelector('.axis-marker'), height = parseFloat(getComputedStyle(title).lineHeight);
        const a = rect(marker), b = rect(title);
        return {aligned:Math.abs(a.top+a.height/2-(b.top+height/2)) < 1, left:a.right <= b.left+1,
          distinct:marker.dataset.activity === card.dataset.activity, top:a.top, time:marker.textContent};
      })};
  })()`);
  const assertAligned = layout => {
    assert.ok(layout.flat, 'date, activity and axis share an unboxed surface');
    assert.ok(layout.rows.length === 4 && layout.rows.every(row => row.aligned && row.left && row.distinct));
    assert.deepEqual(layout.rows.map(row=>row.time), ['05:00','05:00','06:00','18:00']);
  };
  assertAligned(await timelineLayout());
  assert.ok(!reqs.some(r => r.path === '/v1/records/artifact' && r.start === late.start), 'offscreen activity does not prefetch notes');
  const light = await appearance();
  assert.equal(light.scheme, 'light');
  assert.ok(contrast(light.ink, light.panel) >= 4.5 && contrast(light.muted, light.sidebar) >= 4.5);
  await until('!!document.querySelector(".axis-retry")', 'weekly preview error is separate');
  assert.equal(reqs.filter(r=>r.path==='/v1/week-coverage').length, 1);
  denyCoverage = false;
  await click('.axis-retry');
  await until('document.querySelectorAll("#list .coverage-mini [data-state=ready]").length === 14', 'one response paints the entire week');
  assert.equal(reqs.filter(r=>r.path==='/v1/week-coverage').length, 2);
  assert.equal(reqs.filter(r=>r.path==='/v1/timeline').length, 1, 'only the selected day loads activity bodies');
  assert.ok(!reqs.some(r=>r.path==='/v1/recording-intervals'));
  const chatOnly = '.day[data-date="2026-10-31"]';
  assert.ok(await evaluate(`!!document.querySelector('${chatOnly} .coverage-timeline .coverage-span') && !document.querySelector('${chatOnly} .coverage-activity .coverage-span')`));
  denyNotes = false;
  await click(first + ' .inline-retry');
  await until(`document.querySelectorAll(${JSON.stringify(first+' .note')}).length === 2`, 'two notes after retry');
  assert.ok(!await evaluate(textIn(first,'不应串入的笔记')));
  assert.ok(await evaluate(`!!document.querySelector(${JSON.stringify(first+' .note math msup')})`));
  assert.equal(await evaluate('document.querySelectorAll(".note :is(img,script,a[href],[contenteditable])").length'), 0);
  const beforeScroll = await timelineLayout();
  assertAligned(beforeScroll);
  const pointer = await evaluate(`(() => {
    const mark = document.querySelector('.timeline-list .axis-marker').getBoundingClientRect();
    return {x: mark.left + mark.width/2, y: mark.top + mark.height/2};
  })()`);
  await c.send('Input.dispatchMouseEvent', {type:'mouseWheel', ...pointer, deltaX:0, deltaY:5000});
  await until(textIn(later,'下午的独立笔记'), 'native vertical scrolling reveals the last activity and loads its note');
  await until(`document.querySelector(${JSON.stringify(marker)})?.getAttribute('aria-current') === 'true'`, 'scroll highlights the active marker');
  const afterScroll = await timelineLayout();
  assertAligned(afterScroll);
  const delta = afterScroll.scroll - beforeScroll.scroll;
  assert.ok(delta > 100);
  assert.ok(Math.abs(beforeScroll.date - afterScroll.date - delta) < 1);
  assert.ok(afterScroll.rows.every((row,i) => Math.abs(beforeScroll.rows[i].top-row.top-delta) < 1));
  assert.equal(await evaluate('scrollY'), 0);
  await evaluate('document.getElementById("reader").scrollTop = 0');
  await evaluate(`document.querySelector(${JSON.stringify(marker)}).dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,button:0}))`);
  await until(`document.activeElement.matches(${JSON.stringify(later)}) && location.search.includes('activity=${lateId}')`, 'marker links to its activity');
  await until('!document.querySelector("#reader .note + [role=status]")', 'notes settle');
  const reading = () => evaluate('({width:document.getElementById("reader-pane").offsetWidth, scroll:document.getElementById("reader").scrollTop})');
  const before = await reading(), count = reqs.length;
  await setScheme('dark');
  await until('getComputedStyle(document.querySelector("#previous")).colorScheme === "dark"', 'live dark appearance');
  const dark = await appearance();
  assert.notEqual(dark.panel, light.panel);
  assert.ok(contrast(dark.ink, dark.panel) >= 4.5 && contrast(dark.muted, dark.sidebar) >= 4.5);
  const address = await evaluate('location.href');
  const sidebar = 'document.getElementById("week-sidebar")';
  const sidebarEnd = (open) => `getComputedStyle(${sidebar}.parentElement).visibility === '${open?'visible':'hidden'}' && ${sidebar}.getAnimations().length === 0`;
  const animated = (control, property, owner) => evaluate(`(() => {
    const node = document.querySelector(${JSON.stringify(owner)});
    getComputedStyle(node).getPropertyValue(${JSON.stringify(property)});
    document.querySelector(${JSON.stringify(control)}).click();
    getComputedStyle(node).getPropertyValue(${JSON.stringify(property)});
    const transition = node.getAnimations().find(a=>a.transitionProperty===${JSON.stringify(property)});
    return !!transition && transition.effect.getTiming().duration > 0;
  })()`);
  const weekSettled = '!document.querySelector(".week-outgoing") && !document.getElementById("list").getAnimations().length';
  const weekMotion = (control) => evaluate(`(() => {
    const button = document.getElementById(${JSON.stringify(control)});
    const list = document.getElementById('list');
    const source = document.getElementById('source-filter');
    const title = document.getElementById('list-title');
    button.focus();
    button.click();
    const old = document.querySelector('.week-outgoing');
    const frames = n => n?.getAnimations()[0]?.effect.getKeyframes().map(f=>f.transform) || [];
    return {enter:frames(list), exit:frames(old), hidden:old ? old.inert && old.getAttribute('aria-hidden')==='true' && !old.querySelector('[id]') : false,
      days:list.children.length, oldDays:old?.children.length ?? 0, focus:document.activeElement.id,
      controls:source===document.getElementById('source-filter') && title===document.getElementById('list-title')};
  })()`);
  const assertWeekMotion = async (control, direction) => {
    assert.deepEqual(await weekMotion(control), {enter:[`translateX(${direction*100}%)`,'translateX(0%)'],
      exit:['translateX(0%)',`translateX(${-direction*100}%)`],hidden:true,days:7,oldDays:7,focus:control,controls:true});
  };
  await evaluate(`${sidebar}.querySelector('button').focus()`);
  assert.ok(await animated('#sidebar-toggle','grid-template-columns','#board'));
  assert.ok(await evaluate(`${sidebar}.inert && document.activeElement.id==='sidebar-toggle'`));
  await until(sidebarEnd(false), 'desktop sidebar slides closed');
  assert.ok((await reading()).width > before.width + 190);
  assert.ok(await animated('#results-view','grid-template-columns','#board'));
  await until(sidebarEnd(true), 'desktop sidebar slides open');
  await until(`document.getElementById('reader-pane').offsetWidth === ${before.width}`, 'desktop reading width restored');
  assert.equal(await evaluate('location.href'), address);
  assert.equal(reqs.length, count);
  await setMotion('reduce');
  await until('matchMedia("(prefers-reduced-motion: reduce)").matches', 'reduced motion');
  assert.equal(await animated('#sidebar-toggle','grid-template-columns','#board'), false);
  assert.ok(await evaluate(sidebarEnd(false)));
  assert.equal(await animated('#results-view','transform','#week-sidebar'), false);
  assert.ok(await evaluate(sidebarEnd(true)));
  assert.deepEqual(await weekMotion('next'), {enter:[],exit:[],hidden:false,days:7,oldDays:0,focus:'next',controls:true});
  await until('location.search === "?week=2026-11-02"', 'week switches instantly with reduced motion');
  await setMotion('no-preference');
  await assertWeekMotion('previous', -1);
  await until(weekSettled, 'desktop previous-week animation settles');
  await go(`/results/?date=${day}&activity=${id}&version=1`);
  await until(textIn('.linked-activity','正在阅读历史版本'), 'old deep link still works');
  assert.ok(await evaluate('!!document.querySelector(".linked-activity .warning a") && !document.querySelector(".linked-activity .note")'));
  await c.send('Emulation.setDeviceMetricsOverride', {width:390,height:500,deviceScaleFactor:1,mobile:true});
  await until('document.getElementById("week-sidebar").inert && document.documentElement.scrollWidth <= innerWidth', 'mobile reading area');
  await until(sidebarEnd(false), 'sidebar settles after changing viewport');
  await until('document.querySelectorAll(".timeline-list .axis-marker").length === 4', 'mobile timeline loaded');
  assertAligned(await timelineLayout());
  assert.ok(await animated('#sidebar-toggle','transform','#week-sidebar'));
  assert.ok(await evaluate('!document.getElementById("week-sidebar").inert && document.getElementById("reader-pane").inert'));
  await until(sidebarEnd(true), 'mobile drawer slides open');
  await c.send('Input.dispatchKeyEvent', {type:'keyDown', key:'Escape', code:'Escape', windowsVirtualKeyCode:27});
  await until('document.getElementById("week-sidebar").inert && !document.getElementById("reader-pane").inert && document.activeElement.id === "sidebar-toggle"', 'Escape returns to reading');
  await until(sidebarEnd(false), 'mobile drawer slides closed');
  await evaluate(toggle);
  await until(sidebarEnd(true), 'mobile drawer ready to change weeks');
  await assertWeekMotion('next', 1);
  await assertWeekMotion('previous', -1);
  assert.ok(await evaluate('document.querySelectorAll(".week-outgoing").length === 1'));
  await assertWeekMotion('next', 1);
  await until(`location.search === "?week=2026-11-02" && (${weekSettled})`, 'rapid mobile week changes settle on the latest week');
  assert.equal(await evaluate('document.querySelector(".day[aria-current=date]").dataset.date'), '2026-11-02');
  await assertWeekMotion('previous', -1);
  await until('location.search === "?week=2026-10-26"', 'previous week');
  await evaluate(`document.querySelector('.day[data-date="2026-10-30"]').focus()`);
  await c.send('Input.dispatchKeyEvent', {type:'keyDown', key:'Enter', code:'Enter', windowsVirtualKeyCode:13});
  await until(`location.search === '?date=2026-10-30' && document.getElementById('week-sidebar').inert && ${textIn('#reader','该日暂无可见活动')}`, 'keyboard opens empty date');
  assert.equal(await evaluate('document.activeElement.id'), 'reader');
  await setScheme('light');
  await until('getComputedStyle(document.querySelector("#previous")).colorScheme === "light"', 'mobile returns to light');
  await go('/results/?date=' + day);
  await until(textIn(first,'新版预算会议'), 'activity tab before switching');
  await click('#timeline-view');
  const renderedEvents = () => evaluate(`[...document.querySelectorAll('.source-event')].map(e=>[e.dataset.source,e.querySelector('time').textContent])`);
  await until('document.querySelectorAll(".source-event").length === 6', 'all-source chronological merge');
  assert.deepEqual(await renderedEvents(), [['legacy-ear','04:00'],['librechat','05:00'],['legacy-ear','06:00'],['librechat','08:00'],['librechat','09:00'],['native-ear','10:00']]);
  assert.ok(await evaluate('document.querySelector("#timeline-view").getAttribute("aria-current") === "page" && document.getElementById("list-title").textContent === "时间线"'));
  assert.equal(await evaluate('document.querySelector("[data-kind=chat_message] .md strong").textContent'), '消息正文');
  const mathSummary = await evaluate(`(() => {
    const node = document.querySelector('[data-kind=chat_message] .md');
    return {inline:node.querySelectorAll('math[display=inline],math:not([display])').length,
      display:node.querySelectorAll('.md-math-block math[display=block]').length,
      square:!!node.querySelector('strong math msubsup'), fraction:!!node.querySelector('math mtable mfrac'),
      sum:!!node.querySelector('math munderover'), root:!!node.querySelector('math msqrt'),
      safe:!node.querySelector('math [href],math [src],math [style],math annotation-xml,math img,math svg'),
      code:[...node.querySelectorAll('code')].some(c=>c.textContent==='$not_math$'&&!c.querySelector('math')),
      text:node.textContent};
  })()`);
  assert.equal(mathSummary.inline, 2);
  assert.equal(mathSummary.display, 2);
  assert.ok(mathSummary.square && mathSummary.fraction && mathSummary.sum && mathSummary.root && mathSummary.safe && mathSummary.code);
  assert.ok(mathSummary.text.includes('$5 和 $10') && mathSummary.text.includes('$x$'));
  assert.ok(mathSummary.text.includes(String.raw`$\notarealcommand{x}$`) && mathSummary.text.includes(String.raw`$\includegraphics{https://tracker.example/math.svg}$`));
  assert.equal(await evaluate('document.querySelectorAll("[data-kind=chat_message] .md li").length'), 2);
  assert.ok(await evaluate('!!document.querySelector("[data-kind=chat_message] .md pre code") && !document.querySelector(".source-event :is(img,a[href],script)")'));
  assert.equal(await evaluate('document.querySelector("[data-kind=transcript_segment] .event-text").tagName'), 'P');
  const density = () => evaluate(`(() => {
    const event = document.querySelector('.source-event[data-kind=audio_ref]');
    const {top} = event.querySelector('h3').getBoundingClientRect();
    const meta = event.querySelector('.event-meta').getBoundingClientRect();
    return {height:event.offsetHeight, inline:Math.abs(meta.top-top)<8, noOverflow:document.documentElement.scrollWidth<=innerWidth};
  })()`);
  const compactMobile = await density();
  assert.ok(compactMobile.height <= 42 && compactMobile.inline && compactMobile.noOverflow);
  await c.send('Emulation.setDeviceMetricsOverride', {width:1280,height:580,deviceScaleFactor:1,mobile:false});
  assert.deepEqual(await density(), compactMobile);
  await c.send('Emulation.setDeviceMetricsOverride', {width:390,height:500,deviceScaleFactor:1,mobile:true});
  assert.ok(reqs.some(r=>r.cursor==='audio-next'));
  await evaluate(toggle);
  await until('!document.getElementById("source-filters").hidden && !document.getElementById("week-sidebar").inert', 'source selector in mobile sidebar');
  const filter = source => evaluate(`(() => { const s=document.getElementById('source-filter'); s.value=${JSON.stringify(source)}; s.dispatchEvent(new Event('change',{bubbles:true})); })()`);
  const beforeChat = reqs.length;
  await filter('librechat');
  await until('document.querySelectorAll(".source-event").length === 3 && document.getElementById("week-sidebar").inert', 'LibreChat only');
  assert.ok((await renderedEvents()).every(r=>r[0]==='librechat'));
  assert.ok(reqs.slice(beforeChat).filter(r=>r.path==='/v1/source-timeline').every(r=>r.source==='librechat' && r.kind.startsWith('chat_')));
  await evaluate(toggle);
  const beforeOld = reqs.length;
  await filter('legacy-ear');
  await until('document.querySelectorAll(".source-event").length === 2 && document.getElementById("week-sidebar").inert', 'legacy Ear only');
  assert.ok((await renderedEvents()).every(r=>r[0]==='legacy-ear'));
  assert.ok(reqs.slice(beforeOld).filter(r=>r.path==='/v1/source-timeline').every(r=>r.source==='legacy-ear' && !r.kind.startsWith('chat_')));
  await evaluate(toggle);
  await until(sidebarEnd(true), 'timeline drawer ready');
  await assertWeekMotion('next', 1);
  await until('location.search.includes("week=2026-11-02") && location.search.includes("source=legacy-ear")', 'timeline week navigation keeps filter');
  await assertWeekMotion('previous', -1);
  await click('.day[data-date="2026-10-29"]');
  await until('document.querySelectorAll(".source-event").length === 2', 'filtered date opens by click');
  await click('#results-view');
  await until(textIn(first,'新版预算会议'), 'return to activity view preserves date');
  await click('#timeline-view');
  await until('document.querySelectorAll(".source-event[data-source=legacy-ear]").length === 2', 'filter survives switching back');
  const countEvents = () => evaluate('document.querySelectorAll(".source-event").length');
  const timelineRequests = (day, cursor) => reqs.filter(r => r.path === '/v1/source-timeline' && r.start.startsWith(day) && (cursor === undefined || r.cursor === cursor));
  const tailButtons = '.event-list + .daily-state button';
  const noLoadButton = async () => {
    assert.ok(!await evaluate(`document.querySelector(${JSON.stringify(tailButtons)})`));
  };
  await go(`/results/?date=${longDay}&view=timeline&source=legacy-ear`);
  await until('document.querySelectorAll(".source-event").length === 24 && !document.querySelector(".event-list + .daily-state [role=status]")', 'first screen loaded');
  await noLoadButton();
  assert.equal(timelineRequests(longDay, '32').length, 0, 'do not fetch the next batch before reaching the bottom');
  const toBottom = () => evaluate('document.getElementById("reader").scrollTop = document.getElementById("reader").scrollHeight');
  await toBottom();
  await until(textIn('.event-list + .daily-state','读取时间线失败'), 'scroll fetches the next page and surfaces the simulated error');
  const atFailure = await countEvents();
  assert.equal(atFailure, 32);
  assert.equal(timelineRequests(longDay, '32').length, 1);
  await toBottom(); await sleep(100);
  assert.equal(timelineRequests(longDay, '32').length, 1, 'failure is not automatically retried');
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(tailButtons)}).textContent`), '重试读取');
  pauseNext = false;
  await click(tailButtons);
  await until('document.querySelectorAll(".source-event").length === 52', 'retry preserves position and adds the remaining rows');
  await toBottom(); await sleep(100);
  assert.equal(timelineRequests(longDay, '32').length, 2);
  assert.equal(timelineRequests(longDay, '48').length, 1);
  await noLoadButton();
  const stamps = await evaluate('[...document.querySelectorAll(".source-event time")].map(t=>t.dateTime)');
  assert.deepEqual(stamps, streamEvents.map(e=>e.start));
  await go(`/results/?date=${sparseDay}&view=timeline&source=legacy-ear`);
  await until(`location.search.includes('${sparseDay}') && document.querySelectorAll('.source-event').length===1 && !document.querySelector('.event-list + .daily-state [role=status]')`, 'auto-fill continues through filtered empty batches and a short screen');
  await noLoadButton();
  assert.deepEqual(timelineRequests(sparseDay).filter(r=>r.kind==='audio_ref').map(r=>r.cursor), [null,'1','2','3','4','5']);
  noAuth = false;
  await go('/results/?date='+day+'&activity='+id);
  await until('document.querySelector("#connect-form")?.hidden === false && !document.querySelector("#connection").hidden', 'authenticated-mode form');
  await evaluate(`document.querySelector('#token').value=${JSON.stringify(token)};document.querySelector('#connect-form').requestSubmit()`);
  await until(`document.querySelectorAll(${JSON.stringify(first+' .note')}).length === 2`, 'Bearer-mode deep link');
  assert.ok(reqs.some(r => r.auth && r.path === '/v1/timeline'));
  assert.ok(reqs.every(r => r.method === 'GET' && !/transcript_segment|source-evidence|\/history/.test(r.path)));
  assert.equal(await evaluate('localStorage.length + sessionStorage.length'), 0);
  assert.deepEqual(external, []);
  assert.deepEqual(errors, []);
  t.diagnostic(`Actual Chromium, synthetic data, desktop and mobile 390px; ${reqs.length} GET requests; no external requests or browser errors.`);
});

// Real Chromium and the packaged page, with a synthetic loopback HTTP API.
test('recording browser: requires capture time, uploads raw bytes, and displays job progress',
  {skip: process.env.EAR_WORKSPACE_BROWSER !== '1', timeout: 30000}, async t => {
  const token = 'W'.repeat(43);
  const job = '0199f0a0-0000-7aaa-9bbb-000000000001';
  const uploads = [], reads = [], violations = [];
  const route = '/v1/recording-uploads';
  let noAuth = false;
  const server = http.createServer(async (req, res) => {
    const send = (code, data, type = 'application/json') => {
      res.writeHead(code, {'Content-Type': type, ...HEADERS});
      res.end(data);
    };
    if (req.url === '/v1/session') {
      return send(200, JSON.stringify({test_no_auth: noAuth, recording_upload: true}));
    }
    if (req.url === route && req.method === 'POST') {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      uploads.push({headers: req.headers, body: Buffer.concat(chunks).toString()});
      return send(202, JSON.stringify({id: job, duplicate: false, state: 'queued'}));
    }
    if (req.url === route + '/' + job && req.method === 'GET') {
      reads.push(req.headers.authorization);
      return send(200, JSON.stringify({id: job, state: 'blocked', attempts: 1, error: 'commit_unknown'}));
    }
    const files = {'/recording-upload/': ['html', 'text/html'],
      '/recording-upload/recording-upload.js': ['js', 'text/javascript'],
      '/recording-upload/recording-upload.css': ['css', 'text/css']};
    if (!(req.url in files)) return send(404, '{}');
    const [ext, type] = files[req.url];
    return send(200, fs.readFileSync(path.resolve(STATIC, '../recording_page/recording-upload.' + ext)), type + '; charset=utf-8');
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(() => { server.close(); server.closeAllConnections(); });
  const origin = 'http://127.0.0.1:' + server.address().port;
  const cache = path.join(os.homedir(), '.cache/ms-playwright');
  const chrome = process.env.EAR_CHROME || path.join(cache, fs.readdirSync(cache).filter(n => n.startsWith('chromium_headless_shell')).sort().pop(), 'chrome-headless-shell-linux64/chrome-headless-shell');
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'ear-recording-browser-'));
  const proc = spawn(chrome, ['--no-sandbox', '--disable-gpu', '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], {stdio:['ignore','ignore','pipe']});
  const exited = new Promise(r => { proc.once('exit', r); proc.once('error', r); });
  t.after(async () => { proc.kill(); await exited; fs.rmSync(profile, {recursive:true,force:true}); });
  const debug = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(Error('Chromium startup timeout')), 10000);
    proc.once('error', e => { clearTimeout(timeout); reject(e); });
    proc.stderr.on('data', b => {
      const m = /DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/.exec(String(b));
      if (m) { clearTimeout(timeout); resolve(m[1]); }
    });
  });
  const targets = await (await fetch('http://127.0.0.1:' + debug + '/json')).json();
  const c = cdp(targets.find(x => x.type === 'page').webSocketDebuggerUrl);
  await c.ready;
  t.after(() => c.close());
  c.on(event => {
    if (event.method === 'Runtime.exceptionThrown') violations.push(event.params.exceptionDetails.text);
    if (event.method === 'Network.requestWillBeSent') {
      const url = event.params.request.url;
      if ((!url.startsWith(origin + '/') && !url.startsWith('data:')) || url.includes(token)) violations.push(url);
    }
  });
  for (const domain of ['Page', 'Runtime', 'Network']) await c.send(domain + '.enable');
  const run = async expression => {
    const r = await c.send('Runtime.evaluate', {expression, returnByValue: true});
    assert.equal(r.exceptionDetails, undefined, JSON.stringify(r.exceptionDetails));
    return r.result.value;
  };
  const until = async expression => {
    for (let i = 0; i < 150; i++) { if (await run(expression)) return; await sleep(35); }
    throw Error('Timed out: ' + expression);
  };
  await c.send('Page.navigate', {url: origin + '/recording-upload/'});
  await until('document.getElementById("connect-form")?.hidden === false');
  await run(`window.el = id => document.getElementById(id); el('token-input').value = '${token}'; el('connect-form').requestSubmit();`);
  assert.equal(await run("el('token-input').value"), '');
  const pick = `const dt = new DataTransfer(); dt.items.add(new File(['RIFF0000WAVE'], 'sample.wav')); el('audio-file').files = dt.files;`;
  await run(`(() => { ${pick} el('upload-form').requestSubmit(); })()`);
  assert.deepEqual(uploads, []);
  await run("el('started-at').value = '2026-11-20T18:01:02'; el('upload-form').requestSubmit();");
  assert.deepEqual(uploads, []);
  await run("el('offset').value = '-07:00'; el('upload-form').requestSubmit();");
  await until(`el('job-id').value === '${job}' && el('status').textContent.includes('已排队')`);
  assert.equal(uploads.length, 1);
  const [first] = uploads;
  assert.equal(first.body, 'RIFF0000WAVE');
  assert.equal(first.headers['content-type'], 'audio/wav');
  assert.equal(first.headers['x-recording-started-at'], '2026-11-20T18:01:02-07:00');
  assert.equal(first.headers.authorization, 'Bearer ' + token);
  assert.equal(first.headers.origin, origin);
  await until("el('status').textContent.includes('入库结果未知')");
  assert.deepEqual(reads, ['Bearer ' + token]);
  assert.equal(await run('sessionStorage.length + localStorage.length'), 0);
  noAuth = true;
  await c.send('Page.reload');
  await until('document.getElementById("recordings")?.hidden === false');
  assert.equal(await run('document.getElementById("connect-form").hidden'), true);
  await run(`(() => { const el = id => document.getElementById(id); el('job-id').value = '${job}'; el('lookup-form').requestSubmit(); })()`);
  await until("document.getElementById('status').textContent.includes('入库结果未知')");
  assert.deepEqual(reads, ['Bearer ' + token, undefined]);
  assert.deepEqual(violations, []);
});
