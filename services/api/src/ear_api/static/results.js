/* Ear results viewer. Same-origin GET only. No data persisted outside this document. */
(function (root, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./workspace.js'));
  else factory(root.EarWorkspace).boot(root);
})(typeof window !== 'undefined' ? window : globalThis, function (workspace) {
  'use strict';
  const { core, md, client, pages } = workspace;
  const ROOT = '/results/';
  const MIN_DATE = '1900-01-01';
  const MAX_DATE = '9999-12-26'; // Last Sunday of a full, four-digit-year week.
  const PAGE_SIZE = 30;
  const EVENT_PAGE_SIZE = 16;
  const RECORDING_EVENTS = ['audio_ref','transcript_segment'];
  const CHAT_EVENTS = ['chat_session','chat_message','chat_attachment'];
  const SOURCE_LABELS = {'legacy-ear':'旧 Ear 导入','native-ear':'新 Ear 采集',librechat:'LibreChat',other:'其他 / 来源未确认'};
  const EVENT_FILTERS = ['all','legacy-ear','librechat'];
  const COVERAGE_SLOT_MS = 300000;
  const TRACKS = { timeline: '时间线事件分布', activity: '活动覆盖' };
  const MAX_ACTIVITY_ROWS = 1000;
  const MAX_ARTIFACT_ROWS = 1000;
  const allowed = [/^\/v1\/(session|timeline|week-coverage|source-timeline)$/, /^\/v1\/records\/artifact$/, /^\/v1\/records\/activity\/[a-f0-9-]{36}$/];
  const invalid = { message: '返回的数据不完整或格式异常。' };

  function validDate(s) {
    if (typeof s !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(s) || s < MIN_DATE || s > MAX_DATE) return false;
    return core.shiftDay(s, 0) === s;
  }
  function weekStart(date) {
    if (!validDate(date)) return null;
    const dayOfWeek = new Date(date + 'T00:00:00Z').getUTCDay();
    return core.shiftDay(date, -((dayOfWeek + 6) % 7));
  }
  function datesFor(anchor) {
    const monday = weekStart(anchor);
    return monday ? Array.from({ length: 7 }, (_, i) => core.shiftDay(monday, i)) : [];
  }
  function routeOf(query) {
    const p = new URLSearchParams(query);
    const d = p.get('date'), id = p.get('activity'), v = p.get('version');
    const date = validDate(d) ? d : null;
    const version = /^(?:[1-9]\d{0,9})$/.test(v || '') && Number(v) <= 2147483647 ? Number(v) : null;
    const timeline = p.get('view') === 'timeline', source = p.get('source');
    return { date, week: date ? null : weekStart(p.get('week')), id: !timeline && core.isId(id) ? id : null,
      version: !timeline && core.isId(id) ? version : null, timeline,
      source: timeline && EVENT_FILTERS.includes(source) ? source : 'all' };
  }
  function href({ date, week, id, version, timeline, source }) {
    const p = new URLSearchParams();
    if (date) p.set('date', date);
    else if (week) p.set('week', week);
    if (timeline) {
      p.set('view', 'timeline');
      if (EVENT_FILTERS.includes(source) && source !== 'all') p.set('source', source);
    } else {
      if (id) p.set('activity', id);
      if (id && version) p.set('version', String(version));
    }
    return ROOT + (p.size ? '?' + p.toString() : '');
  }
  function validSpan(item) {
    const s = core.parseUtc(item.start), e = core.parseUtc(item.end);
    return s !== null && e !== null && e >= s;
  }
  // Assume no link between two activities solely because their intervals overlap.
  function notesFor(items, id) {
    return items.filter((row) => row.current && row.payload.activity_id === id);
  }
  function chronological(a, b) {
    // Preserve Storage's order when multiple records fall in the same millisecond.
    const micros = stamp => Number((stamp.slice(19, -1).slice(1) + '000000').slice(0, 6));
    return core.parseUtc(a.start) - core.parseUtc(b.start) || micros(a.start) - micros(b.start) || a.id.localeCompare(b.id);
  }
  function coverageSlots(range, bits) {
    const start = core.parseUtc(range.start), end = core.parseUtc(range.end);
    if (start === null || end === null || end <= start || typeof bits !== 'string' ||
        bits.length !== Math.ceil((end - start) / COVERAGE_SLOT_MS) || /[^01]/.test(bits)) return null;
    const spans = [];
    for (const run of bits.matchAll(/1+/g)) {
      spans.push([start + run.index * COVERAGE_SLOT_MS,
        Math.min(end, start + (run.index + run[0].length) * COVERAGE_SLOT_MS)]);
    }
    return spans;
  }
  function weekTracks(data, days) {
    if (!core.isObj(data) || data.slot_seconds !== COVERAGE_SLOT_MS / 1000) return null;
    const tracks = days.map(() => []);
    for (const [key, title] of Object.entries(TRACKS)) {
      const track = data[key];
      if (!core.isObj(track) || !['ready','partial','unavailable'].includes(track.state) ||
          !Array.isArray(track.days) || track.days.length !== days.length) return null;
      for (const [i, range] of days.entries()) {
        const bits = track.days[i];
        const spans = track.state === 'unavailable' ? (bits === '' ? [] : null) : coverageSlots(range, bits);
        if (!spans) return null;
        tracks[i].push({key, title, spans, state:track.state});
      }
    }
    return tracks;
  }
  function pageCursor(seen, next) {
    if (next !== null && seen.has(next)) return false;
    if (next !== null) seen.add(next);
    return true;
  }

  function validEvent(row, kind, filter) {
    return core.isObj(row) && core.isId(row.id) && row.kind === kind && validSpan(row) &&
      Object.prototype.hasOwnProperty.call(SOURCE_LABELS, row.source) &&
      (CHAT_EVENTS.includes(kind) ? row.source === 'librechat' : row.source !== 'librechat') &&
      (filter === 'all' || row.source === filter) && typeof row.title === 'string' && row.title.length <= 480 &&
      typeof row.text === 'string' && row.text.length <= 4000 && typeof row.truncated === 'boolean' &&
      [null,'user','assistant'].includes(row.role);
  }

  function boot(win) {
    const doc = win.document;
    const $ = (id) => doc.getElementById(id);
    const zone = core.makeZone('local');
    const today = () => core.dateOf(core.toIso(Date.now()), zone);
    const currentWeek = () => weekStart(route.date || route.week || today());
    let route = routeOf(win.location.search), groups = null, listKey = null;
    let view = null, eventsView = null, chatEnabled = false, lastSource = 'all';
    let connected = false, noAuth = null, routeStamp = 0, scrollFrame = null;
    const narrow = win.matchMedia('(max-width: 720px)');
    const reducedMotion = win.matchMedia('(prefers-reduced-motion: reduce)');
    let weekSlide = null, weekCoverageRequest = null;
    let wideSidebar = true, narrowSidebar = false;

    function syncSidebar() {
      const open = narrow.matches ? narrowSidebar : wideSidebar;
      const panel = $('week-sidebar'), reader = $('reader-pane'), toggle = $('sidebar-toggle');
      const focusWasHidden = (open && narrow.matches && reader.contains(doc.activeElement)) ||
        (!open && panel.contains(doc.activeElement));
      const label = open ? '隐藏侧边栏' : '显示侧边栏';
      if (focusWasHidden) toggle.focus({ preventScroll: true });
      // Animate the layout, but remove the obscured pane from interaction immediately.
      panel.inert = !open;
      if (!open) stopWeekSlide();
      const readerWasInert = reader.inert;
      reader.inert = narrow.matches && open;
      if (!reader.inert) {
        if (readerWasInert && view) resumeNotes(view);
        queueEventLoad();
      }
      $('board').classList.toggle('sidebar-collapsed', !open);
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', label);
      toggle.title = label;
      if (!open || !narrow.matches) queueActive();
    }
    function setSidebar(open) {
      if (narrow.matches) narrowSidebar = open;
      else wideSidebar = open;
      syncSidebar();
    }

    const net = client.createClient({ fetch: win.fetch.bind(win), AbortController: win.AbortController,
      setTimeout: win.setTimeout.bind(win), clearTimeout: win.clearTimeout.bind(win), maxConcurrent: 4, resultsPaths: true,
      onAuthFailure: () => disconnect(noAuth ? '连接失效，请重新连接 Ear。' : '令牌无效或已失效，请重新输入。') });
    const gateway = { get(path) {
      if (path.startsWith('/v1/records/activity?')) path = '/v1/timeline' + path.slice('/v1/records/activity'.length);
      if (!allowed.some((pattern) => pattern.test(path.split('?')[0]))) return Promise.resolve({ ok: false, error: invalid });
      return net.get(path);
    } };
    const api = pages.createData(gateway);

    function el(tag, cls, text) {
      const node = doc.createElement(tag);
      if (cls) node.className = cls;
      if (typeof text === 'string') node.textContent = text;
      return node;
    }
    function p(text, cls = 'subtle') { return el('p', cls, text); }
    function link(text, target, cls, callback) {
      const a = el('a', cls, text);
      a.href = target;
      if (callback) a.addEventListener('click', (e) => {
        if (!plainClick(e)) return;
        e.preventDefault(); callback();
      });
      return a;
    }
    function button(text, fn, cls) {
      const b = el('button', cls, text); b.type = 'button'; b.addEventListener('click', fn); return b;
    }
    function status(text, cls) {
      const node = p(text, cls); node.setAttribute('role', 'status'); return node;
    }
    function retry(box, text, fn) {
      box.append(status(text, 'warning'), button('重试读取', fn, 'inline-retry'));
    }
    function errText(error) { return (error || invalid).message; }
    function dayName(date) {
      const [year, month, day] = date.split('-').map(Number);
      return new Intl.DateTimeFormat('zh-CN', { timeZone: 'UTC', year: 'numeric', month: 'long', day: 'numeric', weekday: 'short' })
        .format(new Date(Date.UTC(year, month - 1, day)));
    }
    function clockSpan(row) {
      const start = core.formatClock(row.start, zone, false), end = core.formatClock(row.end, zone, false);
      if (!start || !end) return '时间未记录';
      if (start.slice(0, 10) === end.slice(0, 10)) return `${start.slice(11)}–${end.slice(11)}`;
      return `${start.slice(5)} → ${end.slice(5)}`;
    }
    function section(parent, title) {
      const node = el('section', 'reading-section'); node.append(el('h4', '', title)); parent.append(node); return node;
    }
    const mathNS = 'http://www.w3.org/1998/Math/MathML';
    const mathTags = new Set(('math semantics annotation mrow mi mo mn mtext mspace mfrac msqrt mroot ' +
      'msub msup msubsup munder mover munderover mtable mtr mtd mstyle mpadded mphantom menclose ' +
      'mmultiscripts mprescripts none').split(' '));
    const mathAttrs = new Set(('display encoding mathvariant mathcolor mathbackground mathsize stretchy ' +
      'symmetric fence separator accent accentunder largeop movablelimits form minsize maxsize lspace rspace ' +
      'linethickness bevelled scriptlevel displaystyle width height depth voffset rowspacing columnspacing ' +
      'rowlines columnlines rowalign columnalign columnspan rowspan notation').split(' '));
    function renderMath(tex, display) {
      try {
        const markup = win.katex.renderToString(tex, {
          displayMode:display, output:'mathml', strict:'ignore', throwOnError:true,
          maxExpand:200, maxSize:16, macros:Object.create(null),
          trust:() => { throw new Error('untrusted_math_command'); },
        });
        if (markup.length > 64000) return null;
        // Parse detached XML; never insert the generated HTML or arbitrary attributes.
        const tree = new win.DOMParser().parseFromString(markup, 'application/xml');
        const shell = tree.documentElement, math = shell.firstElementChild;
        if (shell.localName !== 'span' || shell.childElementCount !== 1 || math?.localName !== 'math') return null;
        let nodes = 0;
        function copy(node, depth = 0) {
          if (++nodes > 1500 || depth > 50) throw new Error('math_render_limit');
          if (node.nodeType === 3) return doc.createTextNode(node.nodeValue);
          if (node.namespaceURI !== mathNS || !mathTags.has(node.localName)) throw new Error('invalid_math_node');
          const result = doc.createElementNS(mathNS, node.localName);
          for (const a of node.attributes) {
            if (a.namespaceURI === null && mathAttrs.has(a.name) && a.value.length <= 200) result.setAttribute(a.name, a.value);
          }
          for (const child of node.childNodes) result.appendChild(copy(child, depth + 1));
          return result;
        }
        return copy(math);
      } catch (_) {
        // Invalid/unsupported or truncated TeX stays readable as its original text.
        return null;
      }
    }
    function markdown(text, truncated = '内容较长，此处只显示前一部分；可在高级工作台查看原始文本。') {
      const out = md.renderMarkdown(doc, text, win.katex ? renderMath : null);
      if (out.stats.truncated) {
        const msg = out.node.querySelector('.md-truncated');
        if (msg) msg.textContent = truncated;
      }
      return out.node;
    }
    function svg(tag, attrs) {
      const node = doc.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
      return node;
    }
    function paintMini(g, state = g.range ? 'pending' : 'unavailable') {
      const metrics = g.tracks ?? Object.entries(TRACKS).map(([key, title]) => ({key,title,spans:[],state}));
      const start = g.range && core.parseUtc(g.range.start), end = g.range && core.parseUtc(g.range.end);
      const percent = ms => 100 * (ms - start) / (end - start);
      const desc = {pending:'读取中', error:'读取失败', partial:'部分来源不可读取', unavailable:'不可用'};
      g.mini.setAttribute('aria-label', metrics.map(m => `${m.title}：${desc[m.state] || (m.spans.length ? '有记录' : '无记录')}`).join('；'));
      g.mini.replaceChildren(...metrics.map(m => {
        const bar = svg('svg', {class:`coverage-bar coverage-${m.key}`, 'aria-hidden':'true', 'data-state':m.state, width:'100%', height:4});
        bar.append(svg('rect', {x:0,y:0,width:'100%',height:'100%',class:'coverage-track'}));
        for (const [first, last] of m.spans) {
          bar.append(svg('rect', {x:`${percent(first)}%`, y:0, width:`${percent(last)-percent(first)}%`, height:4, class:'coverage-span'}));
        }
        return bar;
      }));
    }
    function paintCoverageStatus() {
      const box = $('day-coverage'), focused = box.contains(doc.activeElement);
      const g = groups?.find(item => item.day === selectedDate());
      box.replaceChildren();
      if (g?.coverageError) {
        box.append(p('日期缩略读取失败', 'axis-status'), button('重试日期缩略', () => loadWeekCoverage(groups), 'axis-retry'));
      }
      if (focused) (box.querySelector('button') || $('reader')).focus({preventScroll:true});
    }
    async function loadWeekCoverage(days) {
      if (!days || groups !== days || weekCoverageRequest === days) return;
      const valid = days.filter(g => g.range);
      if (!valid.length) return;
      weekCoverageRequest = days;
      for (const g of valid) { g.coverageError = false; paintMini(g); }
      paintCoverageStatus();
      const query = new URLSearchParams({days:valid.map(g => `${g.range.start}/${g.range.end}`).join(',')});
      const result = await gateway.get('/v1/week-coverage?' + query);
      if (weekCoverageRequest === days) weekCoverageRequest = null;
      if (groups !== days) return;
      const tracks = result.ok ? weekTracks(result.data, valid.map(g => g.range)) : null;
      valid.forEach((g,i) => { g.tracks = tracks?.[i]; g.coverageError = !tracks; paintMini(g, 'error'); });
      paintCoverageStatus();
    }
    function markActive(v, id) {
      if (v.active === id) return;
      v.cards.get(v.active)?.querySelector('.axis-marker')?.removeAttribute('aria-current');
      v.active = id;
      v.cards.get(id)?.querySelector('.axis-marker')?.setAttribute('aria-current', 'true');
    }
    function plainClick(e) { return e.button === 0 && !e.ctrlKey && !e.shiftKey && !e.metaKey && !e.altKey && !e.defaultPrevented; }
    function paintDay(group) {
      if (view?.group === group) paintFeed(view);
    }
    async function loadDay(g) {
      if (!groups?.includes(g) || g.busy) return;
      const range = g.range;
      if (!range) { g.error = { message: '此日期在当前时区无法换算。' }; paintDay(g); return; }
      g.busy = true; g.error = null; paintDay(g);
      const r = await api.list('activity', { range, limit: PAGE_SIZE, cursor: g.cursor });
      if (!groups?.includes(g)) return;
      g.busy = false; g.activityLoaded = true;
      if (!r.ok) g.error = r.error || invalid;
      else if (!pageCursor(g.seen, r.page.nextCursor)) g.error = invalid;
      else {
        g.examined += r.page.items.length + r.page.dropped;
        for (const row of r.page.items) {
          if (!validSpan(row)) { g.incomplete = true; continue; }
          if (row.current) g.rows.set(row.id, row);
        }
        g.cursor = r.page.nextCursor;
        if (g.cursor && (g.examined >= MAX_ACTIVITY_ROWS || g.seen.size >= Math.ceil(MAX_ACTIVITY_ROWS / PAGE_SIZE))) {
          g.incomplete = true; g.cursor = null;
        }
        g.incomplete ||= !!r.page.dropped;
      }
      paintDay(g);
    }
    function loadDayOnce(g) {
      if (groups?.includes(g) && !g.activities) g.activities = loadDay(g);
    }
    function stopWeekSlide() {
      if (!weekSlide) return;
      const {outgoing, enter, exit} = weekSlide;
      weekSlide = null;
      enter.cancel(); exit.cancel(); outgoing.remove();
    }
    function animateWeek(items, outgoing, step) {
      const time = {duration:230, easing:'cubic-bezier(.24,.65,.3,1)'};
      const slide = (node, from, to) => node.animate(
        [{transform:`translateX(${from}%)`}, {transform:`translateX(${to}%)`}], time);
      $('week-pages').append(outgoing);
      const enter = slide(items, step * 100, 0), exit = slide(outgoing, 0, -step * 100);
      const motion = weekSlide = {outgoing, enter, exit};
      const cleanup = () => { if (weekSlide === motion) stopWeekSlide(); };
      enter.onfinish = cleanup;
      enter.oncancel = cleanup;
    }
    function paintList(weekStep) {
      const key = currentWeek();
      if (listKey === key && groups) return;
      stopWeekSlide();
      const items = $('list');
      let outgoing;
      if (weekStep && listKey && items.childElementCount && !$('week-sidebar').inert &&
          !reducedMotion.matches && typeof items.animate === 'function') {
        // Keep only one interactive week. The outgoing copy is for the transition alone.
        outgoing = items.cloneNode(true);
        outgoing.removeAttribute('id');
        outgoing.classList.add('week-outgoing');
        outgoing.inert = true;
        outgoing.setAttribute('aria-hidden', 'true');
        for (const node of outgoing.querySelectorAll('[id],[aria-describedby]')) {
          node.removeAttribute('id'); node.removeAttribute('aria-describedby');
        }
      }
      listKey = key;
      if (weekStep && items.contains(doc.activeElement)) {
        $(weekStep < 0 ? 'previous' : 'next').focus({preventScroll:true});
      }
      items.replaceChildren();
      groups = datesFor(key).map((day) => {
        const card = link(dayName(day), href(dateRoute({date:day})), 'day', () => navigate(dateRoute({date:day}), true, true));
        const mini = el('span', 'coverage-mini'), label = `coverage-${day}`;
        mini.id = label; mini.setAttribute('role', 'img'); card.append(mini);
        card.setAttribute('aria-describedby', label);
        card.dataset.date = day;
        items.append(card);
        const g = { day, card, mini, range: core.dayBounds(day, zone),
          rows: new Map(), seen: new Set(), examined: 0, cursor: null, busy: false, error: null, activityLoaded: false,
          incomplete: false, tracks: null, coverageError: false };
        paintMini(g);
        return g;
      });
      if (outgoing) animateWeek(items, outgoing, weekStep);
      void loadWeekCoverage(groups);
    }
    function paintControls() {
      const start = currentWeek();
      $('previous').disabled = !weekStart(core.shiftDay(start, -7));
      $('next').disabled = !weekStart(core.shiftDay(start, 7));
    }
    function moveWeek(step) {
      const week = weekStart(core.shiftDay(currentWeek(), step * 7));
      if (week) { $('week-sidebar').scrollTop = 0; navigate(dateRoute({week}), false, false, step); }
    }

    async function notePage(d) {
      if (view !== d.view || !d.noteBox || d.noteBusy) return;
      d.noteStarted = true;
      d.noteBusy = true; paintNotes(d);
      const { start, end } = d.activity;
      const range = { start, end: core.parseUtc(start) === core.parseUtc(end) ? core.toIso(core.parseUtc(end) + 1000) : end };
      const r = await api.list('artifact', { range, limit: PAGE_SIZE, cursor: d.noteCursor });
      if (view !== d.view) return;
      d.noteBusy = false;
      if (!r.ok || !pageCursor(d.noteSeen, r.page.nextCursor)) d.noteError = r.error || invalid;
      else {
        d.noteError = null;
        d.noteExamined += r.page.items.length + r.page.dropped;
        for (const note of notesFor(r.page.items, d.activity.id)) d.notes.set(note.id, note);
        d.noteCursor = r.page.nextCursor;
        if (d.noteCursor && (d.noteExamined >= MAX_ARTIFACT_ROWS || d.noteSeen.size >= Math.ceil(MAX_ARTIFACT_ROWS / PAGE_SIZE))) {
          d.noteIncomplete = true; d.noteCursor = null;
        }
        d.noteIncomplete ||= !!r.page.dropped;
      }
      paintNotes(d);
    }
    function paintNotes(d) {
      const box = d.noteBox, focus = box.contains(doc.activeElement);
      box.replaceChildren();
      [...d.notes.values()].sort(chronological).forEach((item, index) => {
        const node = el('article', 'note');
        if (d.notes.size > 1) node.append(p(`笔记 ${index + 1}`, 'note-count'));
        if (typeof item.payload.body_md === 'string' && item.payload.body_md.trim()) node.append(markdown(item.payload.body_md));
        else node.append(p('此笔记没有可读正文。'));
        box.append(node);
      });
      if (d.noteBusy) box.append(status('正在读取关联笔记…'));
      else if (d.noteError) retry(box, `读取笔记失败：${errText(d.noteError)}`, () => notePage(d));
      else if (d.noteCursor) box.append(status('关联结果尚未加载完整。'), button('继续查找关联笔记', () => notePage(d)));
      else if (d.noteIncomplete) box.append(status('未能完整核对关联笔记；不能据此判断没有笔记。', 'warning'));
      else if (!d.notes.size) box.append(status('暂未查到这项活动的关联笔记。'));
      if (focus) (box.querySelector('button') || box.closest('.feed-activity')).focus({preventScroll:true});
      queueActive();
    }
    function activityCard(v, row, historical = null, target = null) {
      const pane = el('article', 'feed-activity');
      pane.tabIndex = -1;
      pane.dataset.activity = row.id;
      const heading = el('h3');
      if (core.plainText(row.payload.title_md, 200)) heading.append(md.renderInline(doc, row.payload.title_md).node);
      else heading.textContent = '未命名活动';
      const head = el('header', 'reading-head');
      const stamp = core.formatClock(row.start, zone, false);
      if (stamp) {
        const time = el('time', '', stamp.slice(11));
        time.dateTime = row.start;
        // The marker and the title occupy the same header row, regardless of note height.
        const to = { date: v.group.day, id: row.id };
        const marker = !historical && v.group.rows.has(row.id)
          ? link('', href(to), 'axis-marker', () => navigate(to, true)) : el('span', 'axis-marker');
        marker.dataset.activity = row.id;
        if (marker.tagName === 'A') marker.setAttribute('aria-label', `${clockSpan(row)} ${heading.textContent}`);
        marker.append(time);
        head.append(marker);
      }
      head.append(heading, p(clockSpan(row), 'time'));
      if (historical) {
        const hint = p(historical, 'warning');
        if (target) hint.append(' ', link('查看当前版本', href(target), '', () => navigate(target, true)));
        head.append(hint);
      }
      pane.append(head);
      if (typeof row.payload.description_md === 'string' && row.payload.description_md.trim()) {
        section(pane, '活动概述').append(markdown(row.payload.description_md));
      }
      const notes = section(pane, '笔记');
      if (historical) notes.append(p('历史版本不显示现行笔记。'), link('在高级工作台追溯', '/workspace/' + core.recordHash('activity', row.id, row.version)));
      else if (!validSpan(row)) notes.append(p('活动缺少有效时间，暂不能查询关联笔记。', 'warning'));
      else {
        const noteBox = el('div'); noteBox.append(p('滚动至此时读取笔记…'));
        notes.append(noteBox);
        const state = {view:v, activity:row, noteBox, notes:new Map(), noteSeen:new Set(), noteCursor:null, noteExamined:0};
        v.lazy.set(pane, state);
        v.observer.observe(pane);
      }
      return pane;
    }
    function paintFeed(v) {
      const {group:g, timeline, message, cards} = v;
      let before = null;
      for (const row of [...g.rows.values()].sort(chronological).reverse()) {
        if (!cards.has(row.id)) cards.set(row.id, activityCard(v, row));
        const card = cards.get(row.id);
        if (card.parentNode !== timeline || card.nextSibling !== before) timeline.insertBefore(card, before);
        before = card;
      }
      const restore = message.contains(doc.activeElement);
      message.replaceChildren();
      if (g.busy || !g.activityLoaded && !g.error) message.append(status('正在读取当天活动…'));
      else if (g.error) retry(message, `读取活动失败：${errText(g.error)}`, () => loadDay(g));
      else if (g.cursor) message.append(button('加载更多活动', () => loadDay(g)));
      else if (g.incomplete) message.append(status('部分结果未能显示。', 'warning'));
      else if (!cards.size && !v.extra.firstChild) message.append(status('该日暂无可见活动。'));
      if (restore) (message.querySelector('button') || $('reader')).focus({preventScroll:true});
      queueActive();
    }
    function queueActive() {
      if (scrollFrame !== null) return;
      scrollFrame = win.requestAnimationFrame(() => {
        scrollFrame = null;
        if (!view || $('reader-pane').inert || $('reader-pane').getClientRects().length === 0) return;
        const reader = $('reader'), edge = reader.getBoundingClientRect().top + Math.min(90, reader.clientHeight * .25);
        let id = null;
        if (view.extra.offsetHeight && view.extra.getBoundingClientRect().bottom > edge) { markActive(view, null); return; }
        for (const card of view.timeline.children) {
          if (card.getBoundingClientRect().top > edge && id) break;
          id = card.dataset.activity;
        }
        markActive(view, id);
      });
    }
    function jump(node, focus) {
      if (focus) node.focus({preventScroll:true});
      const reader = $('reader');
      reader.scrollTop += node.getBoundingClientRect().top - reader.getBoundingClientRect().top;
      queueActive();
    }
    function selectedDate() {
      if (route.date) return route.date;
      const now = today();
      return !route.week || route.week === weekStart(now) ? now : route.week;
    }
    function clearView() {
      view?.observer.disconnect(); view = null;
    }
    function dateRoute(date) { return {...date, timeline:route.timeline, source:route.source}; }
    function selectDay(day) {
      $('reading-date').textContent = dayName(day);
      for (const g of groups) {
        g.card.href = href(dateRoute({date:g.day}));
        if (g.day === day) g.card.setAttribute('aria-current', 'date');
        else g.card.removeAttribute('aria-current');
      }
    }
    function resumeNotes(v) {
      for (const card of [...v.timeline.children, ...v.extra.children]) {
        const note = v.lazy.get(card);
        if (note && !note.noteStarted) {
          v.observer.unobserve(card);
          v.observer.observe(card);
        }
      }
    }
    function showDay(day) {
      const group = groups?.find((g) => g.day === day);
      if (!group || view?.group === group) return view;
      clearView();
      const reader = $('reader'), timeline = el('div', 'timeline-list'), extra = el('div', 'linked-activity'), message = el('div', 'daily-state');
      const v = {group, timeline, extra, message, cards:new Map(), lazy:new WeakMap(), active:null, extraKey:null};
      const readerPane = $('reader-pane');
      v.observer = new win.IntersectionObserver((entries) => {
        if (view !== v || readerPane.inert) return;
        for (const {target, isIntersecting} of entries) {
          const d = v.lazy.get(target);
          if (isIntersecting && d && !d.noteStarted) { v.observer.unobserve(target); void notePage(d); }
        }
      }, {root:reader, rootMargin:'12% 0px'});
      view = v;
      selectDay(day);
      $('day-content').replaceChildren(extra, timeline, message); reader.scrollTop = 0;
      paintCoverageStatus();
      paintDay(group);
      void loadDayOnce(group);
      return v;
    }
    async function showLinkedActivity(v, stamp, focus, prefetched) {
      const {id, version} = route;
      if (!id) {
        if (v.extraKey) { v.extra.replaceChildren(); v.extraKey = null; paintFeed(v); }
        return;
      }
      const key = `${id}:${version || ''}`;
      if (v.extraKey && v.extraKey !== key) { v.extra.replaceChildren(); v.extraKey = null; }
      if (!version) {
        if (!v.cards.has(id)) await v.group.activities;
        if (stamp !== routeStamp || view !== v) return;
        const card = v.cards.get(id);
        if (card) { jump(card, focus); return; }
      }
      if (v.extraKey === key && v.extra.firstChild?.matches('.feed-activity')) { jump(v.extra.firstChild, focus); return; }
      v.extraKey = key;
      v.extra.replaceChildren(status('正在读取指定活动…', 'daily-state'));
      const [r, current] = await Promise.all([prefetched || api.record('activity', id, version), version ? api.record('activity', id) : null]);
      if (stamp !== routeStamp || view !== v) return;
      v.extra.replaceChildren();
      if (!r.ok) {
        if (r.error?.kind === 'notfound') v.extra.append(status('该活动或版本不存在，或当前不可读取。', 'warning'));
        else retry(v.extra, `读取活动失败：${errText(r.error)}`, () => showLinkedActivity(v, stamp, focus));
        return;
      }
      const historical = !r.rec.current ? '这项活动已被整体替代。' : version && !current?.ok ? '这是指定版本；无法确认是否仍为当前版本。' :
        version && (current.rec.version !== r.rec.version || !current.rec.current) ? '你正在阅读历史版本。' : null;
      if (!historical && v.cards.has(id)) { v.extraKey = null; jump(v.cards.get(id), focus); return; }
      const target = historical && current?.ok && current.rec.current ? {date:v.group.day, id} : null;
      const card = activityCard(v, r.rec, historical, target);
      v.extra.append(card); jump(card, focus);
    }
    function eventCard(row) {
      const pane = el('article', 'feed-activity source-event');
      pane.dataset.source = row.source;
      pane.dataset.kind = row.kind;
      const head = el('header', 'reading-head event-head');
      const marker = el('span', 'axis-marker'), stamp = core.formatClock(row.start, zone, false);
      const time = el('time', '', stamp.slice(11)); time.dateTime = row.start; marker.append(time);
      const labels = { audio_ref:'录音', transcript_segment:'语音转写', chat_session:'会话', chat_message:'消息', chat_attachment:'附件' };
      const title = row.title ? `${labels[row.kind]} · ${row.title}` : row.kind === 'chat_message' && row.role
        ? (row.role === 'user' ? '用户消息' : '助手消息') : labels[row.kind];
      head.append(marker, el('h3', '', title));
      const meta = el('span', 'event-meta'); meta.append(el('span', 'event-source', SOURCE_LABELS[row.source]));
      if (row.start !== row.end) meta.append(` · ${clockSpan(row)}`);
      if (core.dateOf(row.start, zone) !== selectedDate()) meta.append(` · 始于 ${stamp.slice(0,10)}`);
      head.append(meta); pane.append(head);
      if (row.text || row.truncated) {
        const renderMd = row.kind === 'chat_message' && row.source === 'librechat';
        const text = renderMd ? el('div', 'event-text event-markdown') : p(row.text, 'event-text');
        if (renderMd && row.text) text.append(markdown(row.text, '部分内容因排版限制未显示。'));
        if (row.truncated) text.append(row.text ? ' ' : '', el('span', 'event-cut', '（仅显示摘录）'));
        pane.append(text);
      }
      return pane;
    }
    function paintEventState(v) {
      const message = v.message, restore = message.contains(doc.activeElement);
      message.replaceChildren();
      if (v.unavailable.size) message.append(status(`部分事件类型不可读取：${[...v.unavailable].map(kind => ({audio_ref:'录音',transcript_segment:'转写',chat_session:'会话',chat_message:'消息',chat_attachment:'附件'})[kind]).join('、')}。`));
      if (v.busy) message.append(status('正在读取时间线…'));
      else if (v.error) retry(message, `读取时间线失败：${errText(v.error)}`, () => loadEvents(v));
      else if (v.done && !v.list.firstChild && !v.unavailable.size) message.append(status(v.disabled ? '当前实例未启用 LibreChat 浏览。' : '当天没有符合条件的事件。'));
      if (restore) (message.querySelector('button') || $('reader')).focus({preventScroll:true});
    }
    function queueEventLoad() {
      const v = eventsView;
      if (!v?.range || v.busy || v.done || v.error || v.loadQueued || $('reader-pane').inert) return;
      v.loadQueued = true;
      win.requestAnimationFrame(() => {
        v.loadQueued = false;
        if (eventsView !== v || v.busy || v.done || v.error || $('reader-pane').inert) return;
        const reader = $('reader');
        if (reader.clientHeight > 0 && reader.scrollHeight - reader.clientHeight - reader.scrollTop <= 96) {
          void loadEvents(v);
        }
      });
    }
    async function fillStream(v, stream) {
      if (stream.buffer.length || stream.done) return null;
      const query = new URLSearchParams({...v.range, source:v.source, kind:stream.kind, limit:String(EVENT_PAGE_SIZE)});
      if (stream.cursor) query.set('cursor', stream.cursor);
      const result = await gateway.get('/v1/source-timeline?' + query);
      if (eventsView !== v) return null;
      if (result.error?.kind === 'forbidden') {
        stream.done = true; v.unavailable.add(stream.kind); return null;
      }
      if (!result.ok) return result.error || invalid;
      const data = result.data;
      if (!core.isObj(data) || !Array.isArray(data.items) || data.items.length > EVENT_PAGE_SIZE ||
          (data.next_cursor !== null && !core.validCursor(data.next_cursor)) ||
          data.items.some(row => !validEvent(row, stream.kind, v.source)) ||
          !pageCursor(stream.seen, data.next_cursor)) return invalid;
      stream.cursor = data.next_cursor; stream.done = stream.cursor === null;
      stream.buffer.push(...data.items.sort(chronological));
      return null;
    }
    async function loadEvents(v) {
      if (eventsView !== v || v.busy || v.done || !v.range) return;
      v.busy = true; v.error = null; paintEventState(v);
      let shown = 0, rounds = 0;
      while (shown < 24 && eventsView === v) {
        const needed = v.streams.filter(s => !s.done && !s.buffer.length);
        if (needed.length) {
          if (rounds++ >= 3) break;
          const errors = await Promise.all(needed.map(s => fillStream(v, s)));
          if (eventsView !== v) return;
          v.error = errors.find(Boolean) || null;
          if (v.error) break;
          // A filtered storage page may be empty before the end. Do not emit another
          // stream's later rows until every stream has a head or is exhausted.
          if (v.streams.some(s => !s.done && !s.buffer.length)) continue;
        }
        const next = v.streams.filter(s => s.buffer.length).sort((a,b) => chronological(a.buffer[0],b.buffer[0]))[0];
        if (!next) { v.done = true; break; }
        const row = next.buffer.shift();
        if (!v.seen.has(row.id)) { v.seen.add(row.id); v.list.append(eventCard(row)); ++shown; }
      }
      if (eventsView !== v) return;
      v.done ||= v.streams.every(s => s.done && !s.buffer.length);
      v.busy = false; paintEventState(v);
      // The viewport may still have room, including after a batch of filtered-out pages.
      queueEventLoad();
    }
    function showEvents(day, source) {
      if (eventsView?.day === day && eventsView.source === source) return;
      clearView();
      const range = core.dayBounds(day, zone), kinds = source === 'librechat' ? [] : [...RECORDING_EVENTS];
      if (chatEnabled && source !== 'legacy-ear') kinds.push(...CHAT_EVENTS);
      const list = el('div', 'timeline-list event-list'), message = el('div', 'daily-state');
      const v = {day, source, range, list, message, streams:kinds.map(kind => ({kind, cursor:null, buffer:[], seen:new Set(), done:false})),
        seen:new Set(), unavailable:new Set(), done:!kinds.length, disabled:!kinds.length, busy:false, loadQueued:false, error:null};
      eventsView = v;
      const reader = $('reader');
      $('day-content').replaceChildren(list, message);
      selectDay(day); paintCoverageStatus(); reader.scrollTop = 0;
      if (!range) message.append(status('无法换算当天时间。', 'warning'));
      else { paintEventState(v); void loadEvents(v); }
    }
    function stopData() {
      ++routeStamp; stopWeekSlide(); clearView(); eventsView = null; groups = null; listKey = null; weekCoverageRequest = null;
      for (const id of ['day-content','list','day-coverage','reading-date']) $(id).replaceChildren();
    }
    function disconnect(message) {
      net.clear(); connected = false; stopData();
      $('board').hidden = true; $('connection').hidden = false;
      $('token').value = '';
      $('connect-form').hidden = noAuth !== false;
      $('reconnect').hidden = noAuth === false;
      $('connection-status').textContent = message;
    }
    async function discover() {
      noAuth = null; disconnect('正在连接 Ear…'); $('reconnect').hidden = true;
      const r = await gateway.get('/v1/session');
      if (r.stale) return;
      if (!r.ok || typeof r.data.test_no_auth !== 'boolean') { disconnect(`无法连接：${errText(r.error)}`); return; }
      noAuth = r.data.test_no_auth;
      chatEnabled = r.data.chat_evidence_read === true;
      $('recording-upload-link').hidden = r.data.recording_upload !== true;
      if (!noAuth) { disconnect('输入 API 令牌以查看结果。令牌只在当前页面内存中保留。'); return; }
      net.setNoAuth(); connect();
    }
    function connect() {
      connected = true; $('connection').hidden = true; $('board').hidden = false;
      void renderRoute();
    }
    async function renderRoute(focus = false, weekStep = 0) {
      route = routeOf(win.location.search);
      if (!connected) return;
      const stamp = ++routeStamp;
      let prefetched;
      if (route.id && !route.date && !route.week) {
        prefetched = await api.record('activity', route.id, route.version);
        if (stamp !== routeStamp || !connected) return;
        const day = prefetched.ok && core.dateOf(prefetched.rec.start, zone);
        if (validDate(day)) route = {...route, date: day};
      }
      syncSidebar(); paintControls(); paintList(weekStep);
      paintTabs();
      if (route.timeline) showEvents(selectedDate(), route.source);
      else {
        eventsView = null;
        const v = showDay(selectedDate());
        if (v) void showLinkedActivity(v, stamp, focus, prefetched);
      }
      if (focus && !route.id) {
        $('reader').scrollTop = 0;
        $('reader').focus({preventScroll:true});
      }
    }
    function navigate(to, focus = false, fromDates = false, weekStep = 0) {
      if (fromDates) narrowSidebar = false;
      const target = href(to);
      if (target !== win.location.pathname + win.location.search) win.history.pushState({}, '', target);
      void renderRoute(focus, weekStep);
    }
    $('reader').addEventListener('scroll', () => { queueActive(); queueEventLoad(); }, {passive:true});
    win.addEventListener('resize', queueEventLoad);
    win.history.scrollRestoration = 'manual';
    win.addEventListener('popstate', () => { narrowSidebar = false; void renderRoute(true); });
    win.addEventListener('pagehide', () => { net.clear(); connected = false; stopData(); });
    win.addEventListener('pageshow', (e) => { if (e.persisted) { route = routeOf(win.location.search); void discover(); } });
    $('connect-form').addEventListener('submit', (e) => {
      e.preventDefault(); const t = $('token').value; $('token').value = '';
      if (!net.setToken(t)) { disconnect('请输入有效的 API 令牌。'); $('token').focus(); return; }
      connect();
    });
    $('reconnect').addEventListener('click', discover);
    $('sidebar-toggle').addEventListener('click', () => setSidebar(!(narrow.matches ? narrowSidebar : wideSidebar)));
    function paintTabs() {
      if (route.timeline) lastSource = route.source;
      $('board').classList.toggle('timeline-mode', route.timeline);
      $('list-title').textContent = route.timeline ? '时间线' : '活动与笔记';
      $('source-filters').hidden = !route.timeline;
      $('librechat-filter').disabled = !chatEnabled;
      $('source-filter').value = route.source;
      for (const [id, timeline] of [['results-view',false],['timeline-view',true]]) {
        const tab = $(id);
        tab.href = href({date:selectedDate(), timeline, source:lastSource});
        if (route.timeline === timeline) tab.setAttribute('aria-current', 'page');
        else tab.removeAttribute('aria-current');
      }
    }
    for (const [id, timeline] of [['results-view',false],['timeline-view',true]]) {
      $(id).addEventListener('click', (e) => {
        if (!plainClick(e)) return;
        e.preventDefault();
        if (route.timeline === timeline) setSidebar(true);
        else navigate({date:selectedDate(), timeline, source:lastSource}, false, true);
      });
    }
    $('source-filter').addEventListener('change', (e) => {
      if (!route.timeline || !EVENT_FILTERS.includes(e.target.value)) return;
      navigate({date:selectedDate(), timeline:true, source:e.target.value}, narrow.matches, true);
    });
    narrow.addEventListener('change', () => { stopWeekSlide(); syncSidebar(); queueActive(); });
    reducedMotion.addEventListener('change', () => { if (reducedMotion.matches) stopWeekSlide(); });
    doc.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && narrow.matches && narrowSidebar && connected && !e.defaultPrevented) {
        setSidebar(false);
        $('sidebar-toggle').focus({ preventScroll: true });
      }
    });
    $('previous').addEventListener('click', () => moveWeek(-1));
    $('next').addEventListener('click', () => moveWeek(1));
    void discover();
  }
  return { validDate, routeOf, href, weekStart, datesFor, validSpan, notesFor, chronological, coverageSlots, weekTracks, pageCursor, boot };
});
