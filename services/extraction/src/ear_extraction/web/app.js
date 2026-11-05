let state = null;
let busy = false;
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (s) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
const labels = {pending:'待审核',approved:'已通过',rejected:'已拒绝',executed:'已执行',failed:'执行失败'};
const kindNames = {rule_create:'载入提取规则',extract:'活动提取',reevaluate:'产物重判',review_suggestion:'建议审核',action_result:'执行回执'};
const empty = '<div class="empty"><div class="empty-symbol">⌁</div><h3>建议将在这里出现</h3><p>提取完成后，可通过或拒绝提醒建议。</p></div>';
function textNames(text) {
  return String(text).replace(/\[\[(person|place):([^\]]+)\]\]/g, (_, type, id) => state.person.find(p => p.id === id)?.payload.name || '未命名对象');
}
function timeLabel(value) { return value ? value.replace('T', ' ').replace('Z', ' UTC') : '未指定时间'; }
function render() {
  const hasResult = state.activity.length > 0;
  const allExecuted = state.executed.length > 0;
  $('speaker').textContent = state.person.find(p => p.id === state.speaker_id)?.payload.name || '未命名人物';
  $('extract').disabled = busy || hasResult;
  $('extract').textContent = hasResult ? '✓ 已生成活动与待办' : '生成活动与待办 →';
  $('correct').disabled = busy || !hasResult || state.corrected;
  $('correct').textContent = state.corrected ? '✓ 已纠正为乙' : '纠正为乙 ↗';
  $('result-badge').textContent = hasResult ? '已提取' : '等待提取';
  $('progress').innerHTML = ['准备数据','生成活动与待办','纠正人物归属','审核与模拟执行'].map((title, i) => `<div class="progress-item ${[true,hasResult,state.corrected,allExecuted][i] ? 'done' : ''}"><b>0${i+1}</b>${title}</div>`).join('');
  if (hasResult) {
    const activity = state.activity[0];
    const artifact = state.artifact[0];
    const old = state.artifact_history.find(a => a.id === artifact?.id && a.version === 1);
    $('results').innerHTML = `<div class="activity"><strong>${escapeHtml(activity.payload.title_md)}</strong><p>09:00—10:00 UTC · 活动版本 ${activity.version}</p></div>${artifact ? `<div class="note"><div class="note-top"><span>MARKDOWN / 待办</span><span>版本 ${artifact.version}</span></div><div class="markdown">${escapeHtml(textNames(artifact.payload.body_md))}</div></div>` : ''}${old && artifact.version > 1 ? `<div class="previous">更新前 · 版本 1<p>${escapeHtml(textNames(old.payload.body_md))}</p><span>产物已更新，活动 ID 保持不变。</span></div>` : ''}`;
  } else {
    $('results').innerHTML = '<div class="empty"><div class="empty-symbol">≋</div><h3>让片段有条理</h3><p>点击左侧“生成活动与待办”，<br>在这里查看活动及提取出的 Markdown。</p></div>';
  }
  $('suggestion-count').textContent = `${state.action_suggestion.length} 条`;
  $('suggestions').innerHTML = state.action_suggestion.length ? state.action_suggestion.map(s => {
    const p = s.payload;
    const target = escapeHtml(s.id);
    return `<div class="suggestion"><span class="status ${escapeHtml(p.state)}">${escapeHtml(labels[p.state] || p.state)}</span><h3>${escapeHtml(textNames(p.action.title))}</h3><p class="notes">${escapeHtml(textNames(p.action.notes))}</p><div class="date">截止 ${escapeHtml(timeLabel(p.action.due_at))}</div><div class="muted small">来自产物版本 ${p.artifact_version}</div><div class="buttons">${p.state === 'pending' ? `<button class="button primary" data-action="approve" data-target="${target}">审核通过</button><button class="button reject" data-action="reject" data-target="${target}">拒绝</button>` : p.state === 'approved' ? `<button class="button primary" data-action="execute" data-target="${target}">模拟执行提醒 →</button>` : ''}</div></div>`;
  }).join('') : empty;
  const superseded = state.suggestion_history.filter(s => s.status === 'superseded').length;
  if (superseded) $('suggestions').insertAdjacentHTML('beforeend', `<p class="superseded">↻ ${superseded} 条旧的未审核建议已被替代。</p>`);
  $('executed').innerHTML = state.executed.map(a => `<div class="execution"><strong>✓ 模拟提醒已创建</strong><p>${escapeHtml(textNames(a.title))}</p><p>${escapeHtml(textNames(a.notes))}</p><p>仅演示，不会发送通知。</p></div>`).join('');
  $('steps').innerHTML = state.steps.map(s => `<li><strong>${escapeHtml(s.title)}</strong><p>${escapeHtml(s.detail)}</p></li>`).join('');
  $('run-count').textContent = `${state.runs.length} 项任务`;
  $('runs').innerHTML = state.runs.map(r => `<div class="run">${escapeHtml(kindNames[r.kind] || r.kind)}<span>${r.status === 'succeeded' ? '已完成' : escapeHtml(r.status)} · ${r.calls} 次模拟模型调用</span></div>`).join('');
  $('raw').textContent = JSON.stringify({activities:state.activity,artifacts:state.artifact,suggestions:state.suggestion_history,runs:state.runs}, null, 2);
  document.querySelectorAll('button[data-action]').forEach(b => { if (!['extract','correct'].includes(b.dataset.action)) b.disabled = busy; });
}
async function load() {
  const response = await fetch('/api/state');
  if (!response.ok) throw new Error('无法加载演示，请确认服务正在运行。');
  state = await response.json(); render();
}
document.addEventListener('click', async event => {
  const button = event.target.closest('button[data-action]');
  if (!button || busy || !state) return;
  busy = true; render();
  $('message').className = ''; $('message').textContent = '正在处理…';
  try {
    const response = await fetch('/api/action', {method:'POST',headers:{'Content-Type':'application/json','X-Ear-Demo':'1'},body:JSON.stringify({action:button.dataset.action,target:button.dataset.target,revision:state.revision})});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || '操作失败，请重试。');
    state = body;
    $('message').textContent = button.dataset.action === 'reset' ? '场景已重置，可以重新开始演示。' : '操作已完成。';
  } catch (error) {
    $('message').className = 'error'; $('message').textContent = error.message;
    try { await load(); } catch { /* Keep the previous scene and visible error. */ }
  } finally { busy = false; render(); }
});
load().catch(error => { $('message').className = 'error'; $('message').textContent = error.message; });
