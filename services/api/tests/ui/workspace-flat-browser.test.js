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
  await until('!!document.getElementById("view-title")','boot');
  assert.ok((await text()).includes('请先连接'));assert.equal(requests.length,0);
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
  assert.ok(requests.filter(r=>r.url.startsWith('/v1/')).every(r=>r.hasAuth));
  assert.deepEqual(offOrigin,[]);assert.deepEqual(errors,[]);
  t.diagnostic('Real Chromium + synthetic same-origin API; '+requests.length+' GET requests; no external requests/CSP violations; desktop and 390px mobile checks passed.');
  // Close before removing the isolated browser profile, including on assertion failures.
  c.close();proc.kill();await new Promise(r=>proc.once('exit',r));
});
