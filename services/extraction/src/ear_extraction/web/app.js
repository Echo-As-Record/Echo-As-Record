let state = null;
let busy = false;
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (s) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
const labels = {pending:'待确认',approved:'已确认',rejected:'已忽略',executed:'已保存至本页',failed:'执行失败'};
const empty = '<div class="empty"><div class="empty-symbol">⌁</div><h3>建议将在这里出现</h3><p>记录整理后，在这里确认需要跟进的事项。</p></div>';
function textNames(text) {
  return String(text).replace(/\[\[(person|place):([^\]]+)\]\]/g, (_, type, id) => personName(state.person.find(p => p.id === id)?.payload.name));
}
function personName(name) { return ({'合成人物甲':'林舟','合成人物乙':'陈默'})[name] || name || '未命名人物'; }
function timeLabel(value) { return value ? value.replace('T', ' ').replace('Z', ' UTC') : '未指定时间'; }
function render() {
  const hasResult = state.activity.length > 0;

  $('speaker').textContent = personName(state.person.find(p => p.id === state.speaker_id)?.payload.name);
  $('extract').disabled = busy || hasResult;
  $('extract').textContent = hasResult ? '✓ 已整理' : '整理这段记录 →';
  $('correct').disabled = busy || !hasResult || state.corrected;
  $('correct').textContent = state.corrected ? '✓ 已更新为陈默' : '修正说话人';
  $('result-badge').textContent = hasResult ? '已整理' : '待整理';
  $('progress').innerHTML = [['记录',state.evidence.length],['活动',state.activity.length],['待确认',state.action_suggestion.filter(s=>s.payload.state==='pending').length],['已保存提醒',state.executed.length]].map(([title,count])=>`<div class="overview-item"><span>${title}</span><strong>${count}</strong></div>`).join('');
  if (hasResult) {
    const activity = state.activity[0];
    const artifact = state.artifact[0];
    const old = state.artifact_history.find(a => a.id === artifact?.id && a.version === 1);
    $('results').innerHTML = `<div class="activity"><strong>${escapeHtml(activity.payload.title_md.replace('合成组会','实验组会'))}</strong><p>09:00—10:00 UTC · 组会</p></div>${artifact ? `<div class="note"><div class="note-top"><span>待办</span><span>${artifact.version > 1 ? '已更新' : '从记录整理'}</span></div><div class="markdown">${escapeHtml(textNames(artifact.payload.body_md))}</div></div>` : ''}${old && artifact.version > 1 ? `<div class="previous">更新前<p>${escapeHtml(textNames(old.payload.body_md))}</p><span>负责人已更新，原活动保持不变。</span></div>` : ''}`;
  } else {
    $('results').innerHTML = '<div class="empty"><div class="empty-symbol">≋</div><h3>让片段有条理</h3><p>整理这段记录，查看活动要点和待办。</p></div>';
  }
  $('suggestion-count').textContent = `${state.action_suggestion.length} 条`;
  $('suggestions').innerHTML = state.action_suggestion.length ? state.action_suggestion.map(s => {
    const p = s.payload;
    const target = escapeHtml(s.id);
    return `<div class="suggestion"><span class="status ${escapeHtml(p.state)}">${escapeHtml(labels[p.state] || p.state)}</span><h3>${escapeHtml(textNames(p.action.title))}</h3><p class="notes">${escapeHtml(textNames(p.action.notes))}</p><div class="date">截止 ${escapeHtml(timeLabel(p.action.due_at))}</div><div class="muted small">来自：实验组会</div><div class="buttons">${p.state === 'pending' ? `<button class="button primary" data-action="approve" data-target="${target}">确认提醒</button><button class="button reject" data-action="reject" data-target="${target}">忽略</button>` : p.state === 'approved' ? `<button class="button primary" data-action="execute" data-target="${target}">保存到本页 →</button>` : ''}</div></div>`;
  }).join('') : empty;
  const superseded = state.suggestion_history.filter(s => s.status === 'superseded').length;
  if (superseded) $('suggestions').insertAdjacentHTML('beforeend', `<p class="superseded">↻ ${superseded} 条未确认提醒已随笔记更新。</p>`);
  $('executed').innerHTML = state.executed.map(a => `<div class="execution"><strong>✓ 已保存到本页</strong><p>${escapeHtml(textNames(a.title))}</p><p>${escapeHtml(textNames(a.notes))}</p><p>尚未连接手机，不发送通知。</p></div>`).join('');
  const updates = {
    '演示数据已就绪':['记录已载入','组会转写与提取规则已就绪。'],
    '活动提取完成':['组会已整理','已生成活动笔记及待确认提醒。'],
    '归属纠正并完成重判':['参与人物已修正','说话人已更新为陈默，相关笔记已同步更新。'],
    '建议已通过':['提醒已确认','可以将这条事项保存到本页。'],
    '建议已拒绝':['提醒已忽略','这条事项不会加入提醒。'],
    '模拟执行成功':['提醒已保存','该事项已保存在当前页面。'],
  };
  $('steps').innerHTML = [...state.steps].reverse().map(s => {const [title, detail] = updates[s.title] || [s.title,s.detail];return `<li><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></li>`;}).join('');
  document.querySelectorAll('button[data-action]').forEach(b => { if (!['extract','correct'].includes(b.dataset.action)) b.disabled = busy; });
}
async function load() {
  const response = await fetch('/api/state');
  if (!response.ok) throw new Error('暂时无法加载记录，请稍后刷新。');
  state = await response.json(); render();
}
$('correct').addEventListener('click', () => $('person-dialog').showModal());
document.addEventListener('click', async event => {
  const button = event.target.closest('button[data-action]');
  if (!button || busy || !state) return;
  if (button.dataset.action === 'correct') $('person-dialog').close();
  if (button.dataset.action === 'reset' && !confirm('重新开始会清空当前会话的笔记、提醒和修改记录。继续吗？')) return;
  busy = true; render();
  $('message').className = ''; $('message').textContent = '正在处理…';
  try {
    const response = await fetch('/api/action', {method:'POST',headers:{'Content-Type':'application/json','X-Ear-Demo':'1'},body:JSON.stringify({action:button.dataset.action,target:button.dataset.target,revision:state.revision})});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || '操作失败，请重试。');
    state = body;
    $('message').textContent = button.dataset.action === 'reset' ? '记录已恢复，可以重新整理。' : '操作已完成。';
  } catch (error) {
    $('message').className = 'error'; $('message').textContent = error.message;
    try { await load(); } catch { /* Keep the previous scene and visible error. */ }
  } finally { busy = false; render(); }
});
load().catch(error => { $('message').className = 'error'; $('message').textContent = error.message; });
