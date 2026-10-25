'use strict';
/* RefSync-QA 决赛演示页面：导入数据 → 检测 → 修复 → 对比。所有数字来自本地服务读取的算子真实输出文件。 */

// ------------------------------------------------------------ utils
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const fmtBytes = (b) => (b == null ? '—' : b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(1) + ' MB' : b >= 1e3 ? (b / 1e3).toFixed(1) + ' KB' : b + ' B');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const cssv = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}
const post = (path, body) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });

// 每条轨迹的去向：用处理状态色（绿 = 进入训练、紫 = 修复后进入），不借用问题类别色
const POLICY_VAR = {
  整段可用: '--keep', 修复后整段可用: '--fix', 切段使用: '--keep-2', '部分修复+切段使用': '--fix-2',
  隔离回采: '--bad', 文件不可用: '--ink-3', 处理失败: '--ink-3', 重复剔除: '--warn', '部分重复剔除+切段使用': '--warn',
};
const DIM_CN = { structure: '结构', temporal: '时序', sync: '同步', content: '内容', value: '价值' };

// 演示四步：1 导入数据 → 2 检测 → 3 修复 → 4 对比；后三步都只处理第 1 步选中的那份数据
const STEP = { data: 1, detect: 2, govern: 3, compare: 4 };
const S = {
  step: STEP.data,
  info: null,
  cur: null,                        // 当前数据：{ path, kind, name, label, n, ids, rows, bytes, example, key }
  runs: { detect: null, govern: null }, // 当前数据的运行：{ id, mode: live|replay|history, created, version }
  res: {},
  panels: {},
  cmpEp: null,
};
const isSingle = () => !!S.cur && S.cur.n === 1;

// ------------------------------------------------------------ tooltip / toast / modal / drawer
const tip = $('#tip');
function showTip(html, x, y) {
  tip.innerHTML = html;
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  let left = x + 14, top = y + 14;
  if (left + r.width > innerWidth - 8) left = x - r.width - 14;
  if (top + r.height > innerHeight - 8) top = y - r.height - 14;
  tip.style.left = left + 'px';
  tip.style.top = top + 'px';
}
const hideTip = () => { tip.hidden = true; };
let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3200);
}
function openModal(html) {
  $('#modalContent').innerHTML = html;
  $('#modal').hidden = false;
}
function openDrawer(html) {
  $('#drawerContent').innerHTML = html;
  $('#drawer').hidden = false;
  $('#drawer .drawer-body').scrollTop = 0;
}
function closeOverlays() {
  $('#modal').hidden = true;
  $('#drawer').hidden = true;
  if (S.drawerTimer) clearInterval(S.drawerTimer);
  S.drawerTimer = null;
  hideTip();
}
document.addEventListener('click', (e) => {
  if (e.target.closest('[data-close]')) closeOverlays();
  const g = e.target.closest('[data-goto]');
  if (g) setStep(+g.dataset.goto);
});

// ------------------------------------------------------------ theme / steps
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('rsqa-theme', t); } catch (e) { /* ignore */ }
  $$('canvas[data-pano]').forEach((c) => c._redraw && c._redraw());
  Object.values(S.panels).forEach((p) => p.drawLive && p.drawLive());
}
$('#themeBtn').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light'));
try { setTheme(localStorage.getItem('rsqa-theme') || 'dark'); } catch (e) { setTheme('dark'); }

function setStep(n) {
  S.step = n;
  if (S.cmpTimer) { clearInterval(S.cmpTimer); S.cmpTimer = null; }
  if (S.impTimer) { clearInterval(S.impTimer); S.impTimer = null; }
  $$('.view').forEach((v) => v.classList.toggle('on', +v.dataset.view === n));
  $$('#steps button').forEach((b) => b.classList.toggle('on', +b.dataset.step === n));
  closeOverlays();
  if (n === STEP.detect) S.panels.detect.sync();
  if (n === STEP.govern) S.panels.govern.sync();
  if (n === STEP.compare) renderCompare();
  if (n === STEP.data) renderSel();
  updateCurChip();
  window.scrollTo(0, 0);
}
$$('#steps button').forEach((b) => b.addEventListener('click', () => setStep(+b.dataset.step)));
function markDone() {
  $$('#steps button').forEach((b) => {
    const st = +b.dataset.step;
    b.classList.toggle('done', (st === STEP.data && !!S.cur) || (st === STEP.detect && !!S.runs.detect) || (st === STEP.govern && !!S.runs.govern) || (st === STEP.compare && !!S.runs.govern));
  });
}

document.addEventListener('keydown', (e) => {
  if (e.target.matches('input, textarea')) return;
  if (e.key === 'Escape') return closeOverlays();
  if (!$('#drawer').hidden || !$('#modal').hidden) return;
  if (e.key === 'ArrowRight') setStep(Math.min(4, S.step + 1));
  else if (e.key === 'ArrowLeft') setStep(Math.max(1, S.step - 1));
  else if (/^[1-4]$/.test(e.key)) setStep(+e.key);
  else if (e.key === 't' || e.key === 'T') $('#themeBtn').click();
  else if (e.key === 'f' || e.key === 'F') { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen?.(); }
  else if (e.key === ' ' || e.key === 'Enter') {
    const p = S.step === STEP.detect ? S.panels.detect : S.step === STEP.govern ? S.panels.govern : null;
    if (p && p.state !== 'running' && S.cur) { e.preventDefault(); p.start(); }
    if (S.step === STEP.data && S.cur) { e.preventDefault(); startFromData(); }
  }
});

function hwLabel(t) {
  if (!t || !t.hardware) return '';
  const p = t.hardware.platform || '';
  const os = p.startsWith('macOS') ? 'macOS' : p.startsWith('Linux') ? 'Linux' : p.startsWith('Windows') ? 'Windows' : p.split('-')[0];
  const arch = /arm64|aarch64/.test(p) ? 'ARM' : /x86_64|AMD64/.test(p) ? 'x86' : '';
  return `${os} ${arch} · ${t.hardware.cpu_count} 核 · ${t.workers} 进程`;
}

// ------------------------------------------------------------ 当前数据 / 结果来源（顶栏常驻）
const MODE_CN = { live: '现场运行', replay: '回放', history: '历史结果' };
const verShort = (v) => (v ? String(v).replace(/^RefSync-QA-/, '') : '');
function dataLabel(cur = S.cur) {
  if (!cur) return '';
  if (cur.n === 1) return `${cur.example ? '示例 ' : ''}${cur.ids?.length ? epName(cur.ids[0]) : cur.name}`;
  return `${cur.example ? '示例数据' : cur.name} · ${fmt(cur.n)} 条`;
}
function runLabel(run) {
  if (!run) return '';
  return [MODE_CN[run.mode] || '', run.created ? run.created.slice(5, 16) : '', verShort(run.version)].filter(Boolean).join(' · ');
}
function updateCurChip() {
  const chip = $('#curChip');
  if (!S.cur) { chip.hidden = true; return; }
  const run = S.step === STEP.detect ? S.runs.detect : S.step >= STEP.govern ? S.runs.govern : null;
  chip.hidden = false;
  chip.innerHTML = `<b>${esc(dataLabel())}</b>${run ? `<span class="cur-run m-${run.mode}">${esc(runLabel(run))}</span>` : ''}`;
}
// ------------------------------------------------------------ run panel
const STAGE_CFG = {
  detect: {
    name: '检测', btn: '开始检测', rerun: '重新检测',
    phases: [['episodes', '逐条检测'], ['dedup', '跨轨迹去重'], ['outputs', '写出报告'], ['summary', '汇总']],
  },
  govern: {
    name: '修复', btn: '开始修复', rerun: '重新修复',
    phases: [['episodes', '检测 · 修复 · 复检'], ['dedup', '跨轨迹去重'], ['outputs', '写出副本与审计'], ['summary', '汇总']],
  },
};

// 实时面板：左边计时与阶段；右边是逐帧全景（每完成一条轨迹，铺开一行）和逐条发现流
const LIVE_LEGEND = {
  detect: () => CATS.map((c) => [`var(${CAT_VAR[c]})`, c]).concat([['var(--cell)', '无问题']]),
  govern: () => [['var(--fix)', '修复改写']].concat(CATS.map((c) => [`var(${CAT_VAR[c]})`, c]), [['var(--cell)', '无问题']]),
};
const bitsOf = (s) => { const o = {}; for (const ch of s || '') { const b = parseInt(ch, 16) || 0; for (const c of CATS) if (b & CAT_BIT[c]) o[c] = (o[c] || 0) + 1; } return o; };
const runsToSet = (runs) => { const s = new Set(); for (const [a, b] of runs || []) for (let i = +a; i <= +b; i++) s.add(i); return s; };

class RunPanel {
  constructor(kind, host) {
    this.kind = kind;
    this.cfg = STAGE_CFG[kind];
    host.appendChild($('#runTpl').content.cloneNode(true));
    this.el = host;
    this.state = 'idle';
    this.stage = null;
    this.forKey = null;
    $('.rt-name', host).textContent = this.cfg.name;
    this.btn = $('.run-btn', host);
    this.btn.textContent = this.cfg.btn;
    this.btn.addEventListener('click', () => this.start());
    this.canvas = $('canvas[data-live]', host);
    this.bindCanvas();
    this.setKind(kind);
    this.raf = null;
    this.buildRows([]);
  }

  setKind(kind) {
    this.setPhases(STAGE_CFG[kind].phases);
    $('.bh-title', this.el).textContent = kind === 'govern' ? '逐帧修复全景' : '逐帧检测全景';
    $('.live-legend', this.el).innerHTML = LIVE_LEGEND[kind]()
      .map(([c, l]) => `<span><i style="background:${c};border:1px solid var(--line-2)"></i>${l}</span>`).join('');
    $('.feed-h', this.el).textContent = kind === 'govern' ? '逐条修复' : '逐条发现';
  }

  setPhases(phases) {
    $('.phases', this.el).innerHTML = phases.map(([k, l]) => `<li data-k="${k}"><span>${l}</span><span></span></li>`).join('');
  }

  // 进入这一步时调用：数据换了就清空，有结果就显示结果，否则等待开始
  sync() {
    const empty = !S.cur;
    $('.run-empty', this.el).hidden = !empty;
    $('.run-actions', this.el).hidden = empty;
    $('.rt-data', this.el).textContent = empty ? '' : dataLabel();
    if (empty) {
      $('.run-body', this.el).hidden = true;
      $('.result', this.el).hidden = true;
      return;
    }
    if (this.state === 'running') return;
    const run = S.runs[this.kind];
    if (this.forKey !== S.cur.key) {
      this.forKey = S.cur.key;
      this.stage = null;
      this.reset();
      this.buildRows(S.cur.ids || []);
      this.btn.textContent = this.cfg.btn;
    }
    if (run && run.id !== this.stage) this.loadResult(run.id, false);
    else if (!run) {
      $('.result', this.el).hidden = true;
      $('.run-body', this.el).hidden = false;
      this.btn.textContent = this.cfg.btn;
    }
  }

  buildRows(ids) {
    this.ids = [...ids].sort((a, b) => a - b);
    this.n = this.ids.length;
    this.rows = {};
    this.lastEp = null;
    $('.pn', this.el).textContent = this.n;
    this.drawLive();
  }

  bindCanvas() {
    const c = this.canvas;
    const hit = (ev) => {
      const g = c._geo;
      if (!g) return null;
      const r = c.getBoundingClientRect();
      const i = Math.floor((ev.clientY - r.top - 2) / (g.rowH + g.gap));
      const ep = this.ids[i];
      return ep == null ? null : { ep, row: this.rows[ep] };
    };
    c.addEventListener('mousemove', (ev) => {
      const h = hit(ev);
      if (!h || !h.row) return hideTip();
      const R = h.row;
      const codes = (this.kind === 'govern' ? R.final_codes : R.codes) || [];
      showTip(`<b>${epName(h.ep)}</b> · ${esc(this.kind === 'govern' ? R.final_status : R.status)} · ${fmt(R.rows)} 帧`
        + (this.kind === 'govern' && R.fix.size ? ` · 改写 ${fmt(R.fix.size)} 行` : '')
        + '<br>' + (codes.length ? codes.map((x) => `<div class="tt-row"><i style="background:var(${CAT_VAR[catOf(x)]})"></i>${esc(codeCn(x))}</div>`).join('') : '无问题'), ev.clientX, ev.clientY);
    });
    c.addEventListener('mouseleave', hideTip);
    c.addEventListener('click', (ev) => { const h = hit(ev); if (h && h.row && this.state === 'done' && this.stage) openEpisode(this.stage, h.ep); });
    new ResizeObserver(() => this.drawLive()).observe(c.parentElement);
  }

  drawLive() {
    const c = this.canvas;
    if (!c || !c.parentElement || !this.ids) return;
    const n = this.ids.length;
    const W = c.parentElement.clientWidth || 700;
    const rowH = n <= 1 ? 46 : n <= 3 ? 30 : n <= 12 ? 16 : n <= 40 ? 12 : 5, gap = n <= 12 ? 4 : 1, gutter = 44;
    const H = Math.max(n, 1) * (rowH + gap) + 4;
    const dpr = devicePixelRatio || 1;
    c.width = W * dpr; c.height = H * dpr; c.style.height = H + 'px';
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const done = Object.values(this.rows);
    const maxRows = Math.max(n <= 3 ? 1 : 240, ...done.map((r) => r.rows || 0));
    const cw = (W - gutter - 4) / maxRows;
    const col = { cell: cssv('--cell'), empty: cssv('--cell-empty'), ink: cssv('--ink'), ink3: cssv('--ink-3'), fix: cssv('--fix'), bad: cssv('--bad'), ok: cssv('--ok'), warn: cssv('--warn') };
    const catCol = CATS.map((x) => cssv(CAT_VAR[x]));
    const stCol = { 通过: col.ok, 价值提示: col.warn, 需复核: col.bad };
    ctx.font = '10px ' + cssv('--font');
    ctx.textBaseline = 'middle';
    this.ids.forEach((ep, i) => {
      const y = i * (rowH + gap) + 2;
      const R = this.rows[ep];
      if (n <= 40 || ep % 10 === 0) { ctx.fillStyle = col.ink3; ctx.fillText(epName(ep), 0, y + rowH / 2); }
      if (!R) {
        ctx.fillStyle = col.empty;
        ctx.fillRect(gutter, y, W - gutter - 4, rowH);
        return;
      }
      ctx.fillStyle = stCol[this.kind === 'govern' ? R.final_status : R.status] || col.bad;
      ctx.fillRect(gutter - 6, y, 3, rowH);
      if (ep === this.lastEp && this.state === 'running') {
        ctx.fillStyle = col.ink; ctx.globalAlpha = 0.14;
        ctx.fillRect(gutter - 2, y - 1, W - gutter, rowH + 2);
        ctx.globalAlpha = 1;
      }
      const s = this.kind === 'govern' ? (R.fmask || R.mask) : R.mask;
      if (!s.length) {
        ctx.fillStyle = col.bad; ctx.globalAlpha = 0.35;
        for (let x = gutter; x < W - 4; x += 6) ctx.fillRect(x, y, 3, rowH);
        ctx.globalAlpha = 1;
        return;
      }
      const tint = R.epbits;
      for (let r = 0; r < s.length; r++) {
        let fill, alpha = 1;
        if (this.kind === 'govern' && R.fix.has(r)) fill = col.fix;
        else {
          const b = parseInt(s[r], 16) || 0;
          if (b) fill = catCol[[1, 2, 4, 8].findIndex((m) => b & m)];
          else if (tint) { fill = catCol[[1, 2, 4, 8].findIndex((m) => tint & m)]; alpha = 0.3; }
          else fill = col.cell;
        }
        ctx.globalAlpha = alpha;
        ctx.fillStyle = fill;
        ctx.fillRect(gutter + r * cw, y, Math.max(cw - (cw > 3 ? 0.5 : 0), 0.8), rowH);
      }
      ctx.globalAlpha = 1;
    });
    c._geo = { rowH, gap, gutter };
    const feed = $('.feed', this.el);
    if (feed) feed.style.maxHeight = Math.max(H + 22, 260) + 'px';
    $('.lp-n', this.el).textContent = `${done.length} / ${n}`;
  }

  reset() {
    this.events = [];
    this.k = 0;
    this.anchorT = 0;
    this.anchorAt = performance.now();
    this.lastT = 0;
    this.rows = {};
    this.lastEp = null;
    this.err = null;
    $('.feed', this.el).innerHTML = '';
    $$('.phases li', this.el).forEach((li) => { li.className = ''; li.lastElementChild.textContent = ''; });
    $('.clock-v', this.el).textContent = '0.00';
    $('.clock-sub', this.el).textContent = '算子计时';
    $('.prog-bar i', this.el).style.width = '0';
    $('.pk', this.el).textContent = '0';
    ['.c-rows', '.c-img'].forEach((s) => ($(s, this.el).textContent = '0'));
    $('.c-ips', this.el).textContent = '—';
    $('.c-w', this.el).textContent = '—';
    $('.result', this.el).hidden = true;
    $('.run-body', this.el).hidden = false;
    $('.banner', this.el)?.remove();
    const z = () => ({ 时序: 0, 同步: 0, 内容结构: 0, 数据价值: 0 });
    this.live = {
      k: 0, st: { 需复核: 0, 通过: 0, 价值提示: 0 }, fst: { 需复核: 0, 通过: 0, 价值提示: 0 },
      catEp: z(), catFr: z(), fcatFr: z(), flagged: 0, fflagged: 0, flaggedKnown: true,
      fixed: 0, cells: 0, fixRows: 0, rejected: 0, codes: new Set(), secs: 0,
    };
    $('.lc-text', this.el).textContent = '等待开始';
    $('.lc-meta', this.el).textContent = '';
    this.renderLive();
    this.drawLive();
  }

  setLiveCaption(ev) {
    const c = captionFor(ev, this.kind);
    $('.lc-text', this.el).textContent = c.text;
    $('.lc-meta', this.el).textContent = c.meta;
    const box = $('.live-caption', this.el);
    box.classList.remove('pulse');
    void box.offsetWidth;
    box.classList.add('pulse');
  }

  setFinalCaption() {
    const L = this.live;
    const n = this.n || L.k;
    if (this.kind === 'govern') {
      $('.lc-text', this.el).textContent = `${n} 条完成：修复 ${L.fixed} 条，拒修 ${L.rejected} 项`;
      $('.lc-meta', this.el).textContent = `问题帧 ${fmt(L.flagged)} → ${fmt(L.fflagged)} · 需复核 ${L.st['需复核']} → ${L.fst['需复核']} 条`;
    } else {
      $('.lc-text', this.el).textContent = `${n} 条完成：${L.st['需复核'] || 0} 条需复核`;
      $('.lc-meta', this.el).textContent = `${fmt(L.flagged)} 帧被逐帧定位`;
    }
    $('.live-caption', this.el).classList.remove('pulse');
  }

  renderLive() {
    const L = this.live;
    const n = this.n || 1;
    const cell = (k, v, sub = '', cls = '') => `<div class="ls ${cls}"><span>${k}</span><b>${v}</b>${sub ? `<small>${sub}</small>` : ''}</div>`;
    const arrow = (a, b) => `${fmt(a)}<em>→</em>${fmt(b)}`;
    const stats = this.kind === 'govern'
      ? [
        cell('修复', fmt(L.fixed), '条', 'fix'),
        cell('改写', fmt(L.cells), '格'),
        cell('拒修', fmt(L.rejected), '项'),
        cell('问题帧：修复前 → 复检', arrow(L.flagged, L.fflagged), '', 'wide'),
        cell('需复核', arrow(L.st['需复核'], L.fst['需复核']), '条'),
      ]
      : [
        cell('需复核', `<i style="background:var(--bad)"></i>${L.st['需复核']}`),
        cell('通过', `<i style="background:var(--ok)"></i>${L.st['通过']}`),
        cell('仅价值提示', `<i style="background:var(--warn)"></i>${L.st['价值提示']}`),
        cell('逐帧定位', L.flaggedKnown ? fmt(L.flagged) : '—', '帧'),
        cell('问题类型', fmt(L.codes.size), '种'),
        cell('平均每条', L.k ? (L.secs / L.k).toFixed(2) : '—', '秒'),
      ];
    $('.live-status', this.el).innerHTML = stats.join('');
    const maxFr = Math.max(1, ...CATS.map((c) => Math.max(L.catFr[c], L.fcatFr[c])));
    $('.live-cats', this.el).innerHTML = this.kind === 'govern'
      ? '<div class="mini-h">问题帧：修复前 → 复检</div>' + CATS.map((c) => `<div class="hb2"><span><i style="background:var(${CAT_VAR[c]})"></i>${c}</span>
          <div class="t2"><i class="b" style="width:${(L.catFr[c] / maxFr) * 100}%"></i><i class="a" style="width:${(L.fcatFr[c] / maxFr) * 100}%;background:var(${CAT_VAR[c]})"></i></div>
          <span class="v">${fmt(L.catFr[c])}<em>→</em><b>${fmt(L.fcatFr[c])}</b></span></div>`).join('')
      : '<div class="mini-h">四类问题（实时）</div>' + CATS.map((c) => `<div class="hb"><span><i class="sw" style="background:var(${CAT_VAR[c]})"></i>${c}</span><div class="t"><i style="width:${(L.catEp[c] / n) * 100}%;background:var(${CAT_VAR[c]})"></i></div><span class="v">${L.catEp[c]}<small> 条</small></span></div>`).join('');
  }

  pushFeed(ev) {
    const f = feedFor(ev, this.kind);
    const li = document.createElement('li');
    li.className = 'fi new fi--' + f.cls;
    li.innerHTML = `<b class="fi-ep">${epName(ev.ep)}</b><span class="fi-tag">${esc(f.tag)}</span>
      <span class="fi-main"><span class="fi-t">${esc(f.text)}</span><span class="fi-m">${f.chips}${esc(f.meta)}</span></span>`;
    const feed = $('.feed', this.el);
    feed.prepend(li);
    requestAnimationFrame(() => li.classList.remove('new'));
    while (feed.children.length > 30) feed.lastElementChild.remove();
  }

  tick() {
    if (this.state !== 'running') return;
    const t = this.anchorT + (performance.now() - this.anchorAt) / 1000;
    $('.clock-v', this.el).textContent = t.toFixed(2);
    this.raf = requestAnimationFrame(() => this.tick());
  }

  async start() {
    if (this.state === 'running' || !S.cur) return;
    if (Object.values(S.panels).some((p) => p !== this && p.state === 'running')) return toast('已有运行在进行中');
    if (this.kind === 'govern' && S.runs.govern) S.runs.govern = null;
    const r = await post('/api/import/run', { path: S.cur.path, repair: this.kind === 'govern', source: S.cur.source || S.cur.path });
    if (!r.ok) return toast(r.error || '无法开始');
    if (this.kind === 'detect') { S.runs.detect = null; S.runs.govern = null; }
    this.buildRows(r.ids || S.cur.ids || []);
    await this.follow(r.id, 'live');
  }

  // 现场运行失败时：同一份数据上一次的记录按原节奏回放
  async replay(id) {
    const r = await api(`/api/run?stage=${id}&mode=replay`, { method: 'POST' });
    if (!r.ok) return toast(r.error || '无法回放');
    await this.follow(id, 'replay');
  }

  async follow(id, mode) {
    this.stage = id;
    this.forKey = S.cur.key;
    this.reset();
    this.state = 'running';
    this.btn.disabled = true;
    this.btn.textContent = '运行中…';
    this.anchorAt = performance.now();
    this.tick();
    let from = 0, done = false, ok = false;
    while (!done) {
      let j;
      try { j = await api(`/api/job?stage=${id}&from=${from}`); } catch (e) { await sleep(400); continue; }
      if (!j.exists) break;
      for (const ev of j.events) this.onEvent(ev);
      from = j.next;
      done = j.done;
      ok = j.ok;
    }
    cancelAnimationFrame(this.raf);
    this.state = ok ? 'done' : 'idle';
    this.drawLive();
    this.btn.disabled = false;
    this.btn.textContent = this.cfg.rerun;
    if (!ok) {
      const prev = mode === 'live' ? await previousRun(this.kind) : null;
      const b = document.createElement('div');
      b.className = 'banner';
      b.innerHTML = `<span>运行没有完成：${esc(this.err || '未知原因')}</span>${prev ? `<button class="ghost sm">回放上次运行（${esc(prev.created.slice(5))}）</button>` : ''}`;
      if (prev) b.querySelector('button').onclick = () => this.replay(prev.id);
      $('.run-head', this.el).after(b);
      return;
    }
    S.runs[this.kind] = { id, mode };
    markDone();
    await sleep(900);
    await this.loadResult(id, true);
    refreshExample();
    loadHist();
  }

  onEvent(ev) {
    this.events.push(ev);
    if (typeof ev.t === 'number') { this.anchorT = ev.t; this.anchorAt = performance.now(); this.lastT = ev.t; }
    switch (ev.type) {
      case 'start':
        $('.c-w', this.el).textContent = `${ev.workers}`;
        $('.clock', this.el).title = `${ev.operator} · ${ev.cli}`;
        if (S.runs[this.kind] == null) { this.liveVersion = ev.operator; }
        break;
      case 'stage': {
        const li = $(`.phases li[data-k="${ev.stage}"]`, this.el);
        if (!li) break;
        if (ev.state === 'start') {
          $$('.phases li.run', this.el).forEach((x) => { if (x !== li) { x.className = 'end'; } });
          li.className = 'run';
          li._t0 = ev.t;
        } else {
          li.className = 'end';
          li.lastElementChild.textContent = ((ev.t - (li._t0 ?? ev.t)) || 0).toFixed(2) + ' s';
        }
        if (ev.stage === 'dedup' && ev.state === 'start') {
          const ep = $('.phases li[data-k="episodes"]', this.el);
          ep.className = 'end';
          ep.lastElementChild.textContent = ((ev.t - (ep._t0 ?? 0)) || 0).toFixed(2) + ' s';
        }
        break;
      }
      case 'episode': {
        this.k = ev.k;
        $('.pk', this.el).textContent = ev.k;
        $('.prog-bar i', this.el).style.width = (ev.k / ev.total) * 100 + '%';
        $('.c-rows', this.el).textContent = fmt(ev.rows_total);
        $('.c-img', this.el).textContent = fmt(ev.images_total);
        if (ev.t > 0.5) $('.c-ips', this.el).textContent = fmt(ev.images_total / ev.t) + ' 张/秒';
        if (!this.ids.includes(ev.ep)) { this.ids.push(ev.ep); this.ids.sort((a, b) => a - b); this.n = this.ids.length; $('.pn', this.el).textContent = this.n; }
        let epbits = 0;
        for (const c of ev.epcodes || []) if (catOf(c) !== '数据价值') epbits |= CAT_BIT[catOf(c)];
        const fix = runsToSet(ev.fixruns);
        this.rows[ev.ep] = {
          rows: ev.rows, mask: ev.mask || '', fmask: ev.fmask || '', fix, epbits,
          status: ev.status, final_status: ev.final_status, codes: ev.codes || [], final_codes: ev.final_codes || [],
        };
        this.lastEp = ev.ep;
        this.drawLive();
        this.setLiveCaption(ev);
        this.pushFeed(ev);
        const L = this.live;
        L.k = ev.k;
        L.st[ev.status] = (L.st[ev.status] || 0) + 1;
        L.fst[ev.final_status] = (L.fst[ev.final_status] || 0) + 1;
        for (const c of new Set((ev.codes || []).map(catOf))) L.catEp[c] += 1;
        (ev.codes || []).forEach((c) => L.codes.add(c));
        const bb = bitsOf(ev.mask), ab = bitsOf(this.kind === 'govern' ? ev.fmask : ev.mask);
        for (const c of CATS) { L.catFr[c] += bb[c] || 0; L.fcatFr[c] += ab[c] || 0; }
        L.fixed += ev.repaired || ev.meta_patch ? 1 : 0;
        L.cells += ev.cells || 0;
        L.fixRows += fix.size;
        L.rejected += ev.rejected || 0;
        if (ev.flagged == null) L.flaggedKnown = false;
        L.flagged += ev.flagged || 0;
        L.fflagged += ev.fflagged ?? ev.flagged ?? 0;
        L.secs += ev.seconds || 0;
        this.renderLive();
        break;
      }
      case 'final': {
        const z = { 需复核: 0, 通过: 0, 价值提示: 0 };
        this.live.st = { ...z, ...(ev.st || {}) };
        this.live.fst = { ...z, ...(ev.fst || {}) };
        this.renderLive();
        break;
      }
      case 'done': {
        $('.clock-v', this.el).textContent = (ev.timing?.total_seconds_incl_all_csv_parquet_outputs ?? ev.t).toFixed(2);
        $('.clock-sub', this.el).textContent = hwLabel(ev.timing);
        $$('.phases li', this.el).forEach((li) => { if (li.className === 'run') li.className = 'end'; });
        this.lastEp = null;
        this.drawLive();
        this.setFinalCaption();
        break;
      }
      case 'error':
        this.err = ev.msg;
        break;
      default:
    }
  }

  phaseTimes() {
    const out = [];
    for (const li of $$('.phases li', this.el)) {
      const t = li.lastElementChild.textContent;
      if (t) out.push([li.dataset.k, t]);
    }
    return out;
  }

  async loadResult(id, animate) {
    const res = await api(`/api/result?stage=${id}`);
    if (!res.ok) { toast(res.error); return; }
    S.res[id] = res;
    this.stage = id;
    res._phases = this.events?.length ? this.phaseTimes() : [];
    const run = S.runs[this.kind];
    if (run && run.id === id) Object.assign(run, { created: res.run?.created, version: res.run?.version });
    markDone();
    updateCurChip();
    this.state = 'done';
    this.btn.textContent = this.cfg.rerun;
    const box = $('.result', this.el);
    box.innerHTML = '';
    const single = res.episodes.length === 1;
    if (this.kind === 'detect') (single ? renderDetectSingle : renderDetectResult)(res, box);
    else (single ? renderGovernSingle : renderGovernResult)(res, box);
    box.hidden = false;
    $('.run-body', this.el).hidden = true;
    if (animate) box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

async function previousRun(kind) {
  if (!S.cur) return null;
  const r = await api('/api/sessions');
  const g = (r.items || []).find((x) => x.key === S.cur.key);
  const run = g && g[kind];
  return run && run.replayable ? run : null;
}

// ------------------------------------------------------------ result pieces
function kpi(label, value, sub, lead) {
  return `<div class="kpi panel ${lead ? 'lead' : ''}"><div class="k-l">${label}</div><div class="k-v">${value}</div><div class="k-s">${sub || ''}</div></div>`;
}

function speedKpi(res) {
  const t = res.timing || {};
  return kpi('用时', `${fmt(t.total_seconds_incl_all_csv_parquet_outputs, 2)}<small>秒</small>`,
    `${fmt(t.decoded_images_per_second)} 张图像/秒<br>${hwLabel(t)}`, true);
}

function catCard(res) {
  const d = res.detection;
  const maxE = Math.max(1, res.scope.episodes);
  const maxF = Math.max(1, ...CATS.map((c) => res.cat_frames[c] || 0));
  const rows = CATS.map((c) => {
    const e = d.episodes_by_category?.[c] || 0, f = res.cat_frames[c] || 0;
    return `<div class="cat-row"><span class="cr-l"><i class="sw" style="background:var(${CAT_VAR[c]})"></i>${c}</span>
      <div class="t"><i style="width:${(e / maxE) * 100}%;background:var(${CAT_VAR[c]})"></i></div><span class="v">${e}<small> 条</small></span>
      <div class="t"><i style="width:${(f / maxF) * 100}%;background:var(${CAT_VAR[c]});opacity:.7"></i></div><span class="v">${f ? `${fmt(f)}<small> 帧</small>` : '<small>整条级</small>'}</span></div>`;
  }).join('');
  return `<div class="card panel">
    <div class="card-h"><h3>四类问题</h3><button class="ghost sm" data-act="codes">问题明细</button></div>
    <div class="cat-head"><span></span><span>涉及轨迹</span><span>逐帧定位</span></div>
    <div class="cat-rows">${rows}</div></div>`;
}

function bindCommon(box, res) {
  $$('[data-act=codes]', box).forEach((b) => b.addEventListener('click', () => showCodes(res)));
  $$('[data-act=eps]', box).forEach((b) => b.addEventListener('click', () => showEpisodes(res)));
}

const nextCta = (label, fn) => `<div class="cta-row"><button class="cta" data-next="${fn}">${label}</button></div>`;
function bindNext(box) {
  $$('[data-next]', box).forEach((b) => b.addEventListener('click', () => {
    if (b.dataset.next === 'govern') { setStep(STEP.govern); if (!S.runs.govern) S.panels.govern.start(); }
    else if (b.dataset.next === 'compare') setStep(STEP.compare);
  }));
}

function renderDetectResult(res, box) {
  const d = res.detection;
  const cats = Object.entries(d.episodes_by_category || {}).sort((a, b) => b[1] - a[1]);
  const topCat = cats[0] && cats[0][1] ? cats[0][0] : null;
  box.innerHTML = `<div class="finding panel">
      <h2>${fmt(res.scope.episodes)} 条中 <b class="hl-bad">${fmt(d.flagged_episodes)}</b> 条需复核${topCat ? `，最多的是${topCat}问题` : ''}</h2>
    </div>
    <div class="kpis">
      ${speedKpi(res)}
      ${kpi('需复核', `${d.flagged_episodes}<small>/ ${res.scope.episodes} 条</small>`, d.value_hint_only_episodes ? `另有 ${d.value_hint_only_episodes} 条仅价值提示` : '')}
      ${kpi('逐帧定位', `${fmt(d.flagged_frames)}<small>帧</small>`, `占 ${pct(d.flagged_frame_rate)}`)}
      ${kpi('问题区间', `${fmt(d.issue_intervals)}<small>段</small>`, '起止行号 + 数据流')}
    </div>
    <div class="res-grid">
      <div class="card panel">
        <div class="card-h"><h3>逐帧全景</h3><button class="ghost sm" data-act="eps">需复核清单</button></div>
        <div class="legend" style="margin-bottom:10px">${CATS.map((c) => `<span><i style="background:var(${CAT_VAR[c]})"></i>${c}</span>`).join('')}<span><i style="background:var(--cell)"></i>无问题</span><span class="muted">浅色＝整条级</span></div>
        <div class="pano-wrap"><canvas data-pano></canvas></div>
      </div>
      <div class="col">${catCard(res)}</div>
    </div>
    ${nextCta('下一步：修复 →', 'govern')}`;
  drawPanorama($('canvas[data-pano]', box), res, 'before', res.stage);
  bindCommon(box, res);
  bindNext(box);
}

// 可训练帧三档：不修不切（只检测运行的整段可用）→ 只切段（只检测运行）→ 切段 + 修复（本次）
function trainTiers(res) {
  const g = res.governance || {};
  const total = g.train_frames?.total || 0;
  const detectOnly = res.detect_only_train_frames;
  const base = res.detect_only_base ?? g.train_frames?.['原本可用'] ?? 0;
  return { total, detectOnly, base, clip: detectOnly != null ? detectOnly - base : null, fix: detectOnly != null ? total - detectOnly : null };
}

function renderGovernResult(res, box) {
  const g = res.governance;
  const T = trainTiers(res);
  const maxV = Math.max(T.total, T.detectOnly || 0, T.base) || 1;
  const tiers = [
    ['不修不切', T.base, 'var(--ink-3)', ''],
    ...(T.detectOnly != null ? [['只切段', T.detectOnly, 'var(--ink-2)', `+${fmt(T.clip)}`]] : []),
    ['切段 + 修复', T.total, 'var(--ok)', T.detectOnly != null ? `+${fmt(T.fix)}` : `+${fmt(T.total - T.base)}`],
  ];
  const reasons = {};
  for (const m of res.manifest || []) {
    for (const k of new Set(splitClausesJs(m.rejected || '').map(reasonLabel).filter(Boolean))) reasons[k] = (reasons[k] || 0) + 1;
  }
  const rs = Object.entries(reasons).sort((a, b) => b[1] - a[1]);
  const rmax = rs[0]?.[1] || 1;
  const line = T.detectOnly != null
    ? `切段多保留 <b>${fmt(T.clip)}</b> 帧，修复再增加 <b>${fmt(T.fix)}</b> 帧`
    : `比不修不切多 <b>${fmt(T.total - T.base)}</b> 帧`;
  box.innerHTML = `<section class="gov-hero panel">
      <div class="gh-copy">
        <h2>可训练 <b>${fmt(T.total)}</b><small>帧</small></h2>
        <p class="gh-line">${line}</p>
        <p class="gh-def">可训练＝复检无阻断问题、且连续 ≥ ${g.min_clip_frames || 30} 帧</p>
      </div>
      <div class="gh-chart">
        ${tiers.map(([n, v, c, d], i) => `<div class="gh-row ${i === tiers.length - 1 ? 'best' : ''}">
          <div class="lab"><b>${n}</b></div>
          <div class="t"><i data-w="${(v / maxV) * 100}" style="width:0;background:${c}"></i></div>
          <div class="v">${fmt(v)}<small>${d ? `<span class="up">${d}</span>` : ''}</small></div>
        </div>`).join('')}
      </div>
    </section>
    <div class="kpis">
      ${speedKpi(res)}
      ${kpi('修复', `${g.parquet_repaired_copies + g.meta_patch_only}<small>条</small>`, `${g.parquet_repaired_copies} 个副本 + ${g.meta_patch_only} 个元数据补丁`)}
      ${kpi('改写', `${fmt(g.values_changed)}<small>格</small>`, '逐格回读核验')}
      ${kpi('拒修', `${fmt(g.repairs_rejected)}<small>项</small>`, '证据不足，保持原样')}
    </div>
    <div class="res-grid">
      <div class="card panel">
        <div class="card-h"><h3>逐帧全景</h3><div class="pano-tools seg" data-pseg><button data-p="before">修复前</button><button data-p="after" class="on">修复后去向</button></div></div>
        <div class="legend pano-legend" style="margin-bottom:10px"></div>
        <div class="pano-wrap"><canvas data-pano></canvas></div>
      </div>
      <div class="col">
        <div class="card panel"><div class="card-h"><h3>每条轨迹的去向</h3></div><div class="sankey" id="sankey-${res.stage}"></div></div>
        ${rs.length ? `<div class="card panel"><div class="card-h"><h3>拒修原因</h3><span class="sub">${fmt(g.repairs_rejected)} 项</span></div>
          <div class="reasons">${rs.slice(0, 6).map(([k, v]) => `<div class="reason"><div>${esc(k)}<div class="t"><i style="width:${(v / rmax) * 100}%"></i></div></div><b>${v}</b></div>`).join('')}</div></div>` : ''}
      </div>
    </div>
    ${nextCta('下一步：对比 →', 'compare')}`;
  requestAnimationFrame(() => $$('.gh-row .t i', box).forEach((i) => (i.style.width = i.dataset.w + '%')));
  const canvas = $('canvas[data-pano]', box);
  const legend = $('.pano-legend', box);
  const setP = (p) => {
    $$('[data-pseg] button', box).forEach((b) => b.classList.toggle('on', b.dataset.p === p));
    legend.innerHTML = p === 'after' ? FATE_LEGEND : CATS.map((c) => `<span><i style="background:var(${CAT_VAR[c]})"></i>${c}</span>`).join('') + '<span><i style="background:var(--cell)"></i>无问题</span>';
    drawPanorama(canvas, res, p, res.stage);
  };
  $$('[data-pseg] button', box).forEach((b) => b.addEventListener('click', () => setP(b.dataset.p)));
  setP('after');
  drawSankey($(`#sankey-${res.stage}`, box), res);
  bindCommon(box, res);
  bindNext(box);
}

// 处理去向的颜色（与问题类别色分开：绿色只表示"进入训练"）
const FATE_LEGEND = `<span><i style="background:var(--keep)"></i>进入训练</span><span><i style="background:var(--fix)"></i>修复后进入训练</span><span><i style="background:var(--scatter)"></i>可用但不足 30 帧</span><span><i style="background:var(--blocked);border:1px solid var(--line-2)"></i>不进入训练</span><span><i style="background:var(--warn)"></i>重复剔除</span>`;

// ------------------------------------------------------------ panorama (canvas)
function drawPanorama(canvas, res, mode, stage) {
  const eps = res.episodes;
  const maxRows = Math.max(...eps.map((e) => e.rows), 1);
  const rowH = eps.length <= 3 ? 30 : eps.length <= 12 ? 14 : eps.length <= 40 ? 9 : 6, gap = eps.length <= 12 ? 3 : 1, gutter = 40;
  const codeScope = res.code_dict || {};
  const epBits = {};
  for (const e of eps) {
    let b = 0;
    for (const c of (mode === 'final' ? e.final_codes : e.codes) || []) {
      const m = codeScope[c];
      if (m && m.scope === '轨迹级' && catOf(c) !== '数据价值') b |= CAT_BIT[catOf(c)];
    }
    epBits[e.id] = b;
  }
  const draw = () => {
    const W = canvas.parentElement.clientWidth || 900;
    const H = eps.length * (rowH + gap) + 4;
    const dpr = devicePixelRatio || 1;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.height = H + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const cw = (W - gutter) / maxRows;
    const col = {
      cell: cssv('--cell'), empty: cssv('--cell-empty'), ink3: cssv('--ink-3'), bad: cssv('--bad'),
      keep: cssv('--keep'), fix: cssv('--fix'), scatter: cssv('--scatter'), blocked: cssv('--blocked'), warn: cssv('--warn'),
    };
    const catCol = CATS.map((c) => cssv(CAT_VAR[c]));
    ctx.font = '11px ' + cssv('--font');
    ctx.textBaseline = 'middle';
    eps.forEach((e, i) => {
      const y = i * (rowH + gap) + 2;
      if (eps.length <= 40 || e.id % 10 === 0) { ctx.fillStyle = col.ink3; ctx.fillText(epName(e.id), 0, y + rowH / 2); }
      const s = (mode === 'after' ? res.mask_after : mode === 'final' ? (res.mask_final || res.mask_before) : res.mask_before)[e.id] || '';
      if (!s.length) {
        ctx.fillStyle = col.bad;
        ctx.globalAlpha = 0.35;
        for (let x = gutter; x < W; x += 6) ctx.fillRect(x, y, 3, rowH);
        ctx.globalAlpha = 1;
        ctx.fillStyle = col.ink3;
        ctx.fillText(e.rows ? '' : '文件不可读', gutter + 6, y + rowH / 2);
        return;
      }
      for (let r = 0; r < s.length; r++) {
        const ch = s[r];
        let fill, alpha = 1;
        if (mode === 'after') {
          fill = { k: col.keep, r: col.fix, v: col.scatter, x: col.blocked, d: col.warn }[ch] || col.blocked;
        } else if (ch === 'r') {
          fill = col.fix;
        } else {
          const b = parseInt(ch, 16) || 0;
          if (b) fill = catCol[[1, 2, 4, 8].findIndex((m) => b & m)];
          else if (epBits[e.id]) { fill = catCol[[1, 2, 4, 8].findIndex((m) => epBits[e.id] & m)]; alpha = 0.32; }
          else fill = col.cell;
        }
        ctx.globalAlpha = alpha;
        ctx.fillStyle = fill;
        ctx.fillRect(gutter + r * cw, y, Math.max(cw - (cw > 3 ? 0.6 : 0), 0.8), rowH);
      }
      ctx.globalAlpha = 1;
    });
    canvas._geo = { gutter, cw, rowH, gap, W };
  };
  canvas._redraw = draw;
  draw();
  if (!canvas._bound) {
    canvas._bound = true;
    let ro = new ResizeObserver(() => canvas._redraw && canvas._redraw());
    ro.observe(canvas.parentElement);
    const hit = (ev) => {
      const g = canvas._geo;
      const rect = canvas.getBoundingClientRect();
      const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
      const i = Math.floor((y - 2) / (g.rowH + g.gap));
      const e = canvas._res.episodes[i];
      if (!e || x < g.gutter) return null;
      const row = Math.floor((x - g.gutter) / g.cw);
      return { e, row: Math.min(row, Math.max(e.rows - 1, 0)), inRow: row < e.rows };
    };
    canvas.addEventListener('mousemove', (ev) => {
      const h = hit(ev);
      if (!h) return hideTip();
      const { e, row, inRow } = h;
      const ints = (canvas._res.intervals_by_ep?.[e.id] || []);
      const md = canvas._mode;
      const fin = md === 'after' || md === 'final';
      let html = `<b>${epName(e.id)}</b> · ${fmt(e.rows)} 行 · ${esc(md === 'after' ? e.policy : md === 'final' ? e.final_status : e.status)} · ${fmt(fin ? e.final_score : e.score, 1)} 分`;
      const eCodes = (md === 'final' ? e.final_codes : e.codes) || [];
      if (inRow && e.rows) {
        html += `<br>第 ${row} 行`;
        const s = (md === 'after' ? canvas._res.mask_after : md === 'final' ? canvas._res.mask_final : canvas._res.mask_before)[e.id] || '';
        if (md === 'after') html += ' · ' + ({ k: '进入训练', r: '修复后进入训练', v: '有效但未成段', x: '阻断 / 隔离', d: '重复剔除' }[s[row]] || '');
        if (md === 'final' && s[row] === 'r') html += ' · 本行被修复改写';
        const codes = eCodes.filter((c) => {
          const b = parseInt(s[row], 16);
          return md !== 'after' && !Number.isNaN(b) && (b & CAT_BIT[catOf(c)]);
        });
        if (codes.length) html += '<br>' + codes.map((c) => `<div class="tt-row"><i style="background:var(${CAT_VAR[catOf(c)]})"></i>${esc(c)}</div>`).join('');
      }
      const lvl = eCodes.filter((c) => canvas._res.code_dict?.[c]?.scope === '轨迹级');
      if (lvl.length && canvas._mode !== 'after') html += `<div class="muted">轨迹级：${lvl.map(esc).join('、')}</div>`;
      showTip(html, ev.clientX, ev.clientY);
    });
    canvas.addEventListener('mouseleave', hideTip);
    canvas.addEventListener('click', (ev) => { const h = hit(ev); if (h) openEpisode(canvas._stage, h.e.id, h.inRow ? h.row : 0); });
  }
  canvas._res = res;
  canvas._mode = mode;
  canvas._stage = stage;
}

// ------------------------------------------------------------ sankey
function drawSankey(host, res) {
  const eps = res.episodes;
  const L = ['通过', '价值提示', '需复核'];
  const Rorder = ['整段可用', '修复后整段可用', '切段使用', '部分修复+切段使用', '重复剔除', '部分重复剔除+切段使用', '隔离回采', '文件不可用', '处理失败'];
  const flows = {};
  const lc = {}, rc = {};
  for (const e of eps) {
    const a = e.status, b = e.policy;
    flows[a + '|' + b] = (flows[a + '|' + b] || 0) + 1;
    lc[a] = (lc[a] || 0) + 1;
    rc[b] = (rc[b] || 0) + 1;
  }
  const R = Rorder.filter((x) => rc[x]).concat(Object.keys(rc).filter((x) => !Rorder.includes(x)));
  const W = 560, H = 330, nodeW = 10, padL = 86, padR = 150, gapN = 8;
  const total = eps.length;
  const scaleL = (H - gapN * (L.filter((x) => lc[x]).length - 1)) / total;
  const scaleR = (H - gapN * (R.length - 1)) / total;
  const sc = Math.min(scaleL, scaleR);
  const pos = (keys, counts) => {
    let y = 0;
    const o = {};
    for (const k of keys) { if (!counts[k]) continue; o[k] = { y, h: counts[k] * sc, used: 0 }; y += counts[k] * sc + gapN; }
    return o;
  };
  const lp = pos(L, lc), rp = pos(R, rc);
  const x0 = padL, x1 = W - padR;
  let paths = '';
  for (const a of L) {
    for (const b of R) {
      const v = flows[a + '|' + b];
      if (!v) continue;
      const h = v * sc;
      const ya = lp[a].y + lp[a].used, yb = rp[b].y + rp[b].used;
      lp[a].used += h; rp[b].used += h;
      const mx = (x0 + nodeW + x1) / 2;
      paths += `<path d="M${x0 + nodeW},${ya} C${mx},${ya} ${mx},${yb} ${x1},${yb} L${x1},${yb + h} C${mx},${yb + h} ${mx},${ya + h} ${x0 + nodeW},${ya + h} Z" fill="var(${POLICY_VAR[b] || '--ink-3'})" opacity=".32"><title>${a} → ${b}：${v} 条</title></path>`;
    }
  }
  let nodes = '';
  for (const a of L) {
    if (!lp[a]) continue;
    const c = { 通过: '--ok', 价值提示: '--warn', 需复核: '--bad' }[a];
    nodes += `<rect x="${x0}" y="${lp[a].y}" width="${nodeW}" height="${Math.max(lp[a].h, 1.5)}" fill="var(${c})" rx="2"/>`;
    nodes += `<text x="${x0 - 8}" y="${lp[a].y + lp[a].h / 2}" text-anchor="end" dominant-baseline="middle" class="n">${a} ${lc[a]}</text>`;
  }
  for (const b of R) {
    nodes += `<rect x="${x1}" y="${rp[b].y}" width="${nodeW}" height="${Math.max(rp[b].h, 1.5)}" fill="var(${POLICY_VAR[b] || '--ink-3'})" rx="2"/>`;
    nodes += `<text x="${x1 + nodeW + 8}" y="${rp[b].y + rp[b].h / 2}" dominant-baseline="middle"><tspan class="n">${rc[b]}</tspan> ${b}</text>`;
  }
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="治理决策流向">${paths}${nodes}</svg>`;
}

// ------------------------------------------------------------ modals: codes / episodes / files
function showCodes(res) {
  const rows = [...res.codes].sort((a, b) => CATS.indexOf(a.category) - CATS.indexOf(b.category) || b.episodes - a.episodes);
  openModal(`<h2 style="font-size:22px">问题码明细</h2><p class="sub" style="margin:4px 0 14px">本次检测命中 ${rows.length} 种问题码（字典共 ${Object.keys(res.code_dict).length} 种）；证据等级决定是否阻断训练</p>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>类别</th><th>问题码</th><th>说明</th><th>证据等级</th><th>粒度</th><th>轨迹</th><th>帧</th></tr></thead><tbody>
    ${rows.map((c) => `<tr><td><span class="code"><i style="background:var(${CAT_VAR[c.category]})"></i>${c.category}</span></td><td class="mono">${esc(c.code)}</td><td>${esc(c.description)}</td><td>${esc(c.tier)}</td><td>${esc(c.scope || '')}</td><td>${c.episodes}</td><td>${c.frames ? fmt(c.frames) : '—'}</td></tr>`).join('')}
    </tbody></table></div>`);
}

// 需复核清单：先看哪条——按证据强弱、再按问题帧数排；可按问题类别筛选
function showEpisodes(res) {
  const govern = res.kind === 'govern';
  const tierOrder = Object.keys(res.detection?.flagged_by_strongest_evidence || {});
  const st = (e) => (govern ? e.final_status : e.status);
  const codesOf = (e) => (govern ? e.final_codes : e.codes) || [];
  const list = res.episodes.filter((e) => st(e) === '需复核')
    .sort((a, b) => ((tierOrder.indexOf(a.tier) + 99) % 99) - ((tierOrder.indexOf(b.tier) + 99) % 99) || b.flagged_frames - a.flagged_frames || a.id - b.id);
  const dict = res.code_dict || {};
  const row = (e) => {
    const cs = codesOf(e);
    const blocking = cs.filter((c) => blocksTraining(c, dict));
    const cats = [...new Set(cs.map(catOf))];
    return `<tr class="click" data-ep="${e.id}" data-cats="${cats.join(' ')}">
      <td><b>${epName(e.id)}</b></td>
      <td>${cats.map((c) => `<i class="dot-c" style="background:var(${CAT_VAR[c]})" title="${c}"></i>`).join('')}</td>
      <td>${esc(codesText(blocking.length ? blocking : cs, dict))}</td>
      <td class="num">${e.flagged_frames ? fmt(e.flagged_frames) : '整条'}</td>
      <td class="num">${fmt(e.train_frames)} / ${fmt(e.rows)}</td>
      <td>${esc(e.policy || '')}</td></tr>`;
  };
  const counts = Object.fromEntries(CATS.map((c) => [c, list.filter((e) => codesOf(e).some((x) => catOf(x) === c)).length]));
  openModal(`<h2 style="font-size:22px">需复核清单 · ${list.length} 条</h2>
    <div class="filters" id="epFilt"><button class="on" data-c="">全部</button>${CATS.filter((c) => counts[c]).map((c) => `<button data-c="${c}"><i class="dot-c" style="background:var(${CAT_VAR[c]})"></i>${c} ${counts[c]}</button>`).join('')}</div>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>轨迹</th><th>类别</th><th>问题</th><th>问题帧</th><th>可训练</th><th>去向</th></tr></thead><tbody>
    ${list.map(row).join('') || '<tr><td colspan="6" class="muted">没有需复核的轨迹</td></tr>'}
    </tbody></table></div>`);
  $$('#epFilt button').forEach((b) => b.addEventListener('click', () => {
    $$('#epFilt button').forEach((x) => x.classList.toggle('on', x === b));
    $$('#modalContent tr.click').forEach((tr) => { tr.hidden = !!b.dataset.c && !tr.dataset.cats.split(' ').includes(b.dataset.c); });
  }));
  $$('#modalContent tr.click').forEach((tr) => tr.addEventListener('click', () => { $('#modal').hidden = true; openEpisode(res.stage, +tr.dataset.ep); }));
}

async function previewFile(stage, path) {
  openModal(`<h2 style="font-size:20px" class="mono">${esc(path)}</h2><p class="sub" style="margin:6px 0 14px"><span class="spin"></span> 读取中</p>`);
  const p = await api(`/api/preview?stage=${stage}&path=${encodeURIComponent(path)}`);
  let body = '';
  const note = (S.res[stage]?.artifacts || []).find((a) => a.path === path)?.note || '';
  if (!p.ok) body = `<p>${esc(p.error)}</p>`;
  else if (p.kind === 'table') {
    body = `<p class="sub" style="margin:0 0 10px">${esc(note)} · 共 ${fmt(p.total)} 行，显示前 ${p.rows.length} 行</p>`
      + (p.schema ? `<pre class="txt" style="max-height:120px;margin:0 0 10px">${esc(p.schema.join('\n'))}</pre>` : '')
      + `<div class="tbl-wrap"><table class="tbl"><thead><tr>${p.columns.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${p.rows.map((r) => `<tr>${p.columns.map((c) => `<td class="mono">${esc(String(r[c] ?? '')).slice(0, 160)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  } else if (p.kind === 'text') {
    body = `<p class="sub" style="margin:0 0 10px">${esc(note)}</p><pre class="txt">${esc(p.text)}${p.truncated ? '\n…' : ''}</pre>`;
  } else if (p.kind === 'dir') {
    body = `<p class="sub" style="margin:0 0 10px">${esc(note)} · ${p.count} 个文件</p><div class="files">${p.files.map((f) => `<button class="file" data-sub="${esc(f.path)}"><span class="ft">${f.path.split('.').pop().toUpperCase().slice(0, 4)}</span><span class="fn"><b>${esc(f.path)}</b></span><span class="fs">${fmtBytes(f.bytes)}</span></button>`).join('')}</div>`;
  } else body = `<p>二进制文件 · ${fmtBytes(p.bytes)}</p>`;
  $('#modalContent').innerHTML = `<h2 style="font-size:20px" class="mono">${esc(path)}</h2><div style="margin-top:8px">${body}</div>`;
  $$('#modalContent [data-sub]').forEach((b) => b.addEventListener('click', () => previewFile(stage, b.dataset.sub)));
}

// ------------------------------------------------------------ line chart (SVG)
function niceTicks(min, max, n = 4) {
  if (!(max > min)) { max = min + 1; }
  const span = max - min;
  const step0 = span / n;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-12; v += step) out.push(+v.toFixed(12));
  return out;
}
const tickFmt = (v) => (Math.abs(v) >= 1000 ? fmt(v) : Math.abs(v) < 0.01 && v !== 0 ? v.toExponential(0) : +v.toFixed(3) + '');

function lineChart(el, o) {
  const n = o.n;
  const H = o.height || 170;
  const W = Math.max(el.clientWidth || 700, 320);
  const m = { l: 52, r: 12, t: 10, b: 24 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const vals = o.series.flatMap((s) => s.values).filter((v) => v != null && Number.isFinite(v) && (!o.log || v > 0));
  let ymin = o.yMin ?? Math.min(...vals), ymax = o.yMax ?? Math.max(...vals);
  if (!vals.length) { ymin = 0; ymax = 1; }
  (o.thresholds || []).forEach((t) => { ymin = Math.min(ymin, t.y); ymax = Math.max(ymax, t.y); });
  let ty;
  let yticks;
  if (o.log) {
    ymin = Math.max(ymin, 1e-12);
    const a = Math.floor(Math.log10(ymin)), b = Math.ceil(Math.log10(ymax));
    ymin = 10 ** a; ymax = 10 ** Math.max(b, a + 1);
    ty = (v) => m.t + ih - ((Math.log10(Math.max(v, ymin)) - Math.log10(ymin)) / (Math.log10(ymax) - Math.log10(ymin))) * ih;
    const st = Math.max(1, Math.ceil((Math.log10(ymax) - Math.log10(ymin)) / 5));
    yticks = [];
    for (let e = Math.log10(ymin); e <= Math.log10(ymax); e += st) yticks.push(10 ** e);
  } else {
    if (ymax === ymin) { ymax += 1; ymin -= 1; }
    const pad = (ymax - ymin) * 0.06;
    ymin -= pad; ymax += pad;
    yticks = niceTicks(ymin, ymax, 4);
    ty = (v) => m.t + ih - ((v - ymin) / (ymax - ymin)) * ih;
  }
  const tx = (i) => m.l + (n <= 1 ? 0 : (i / (n - 1)) * iw);
  let svg = `<svg viewBox="0 0 ${W} ${H}" height="${H}">`;
  // bands
  for (const b of o.bands || []) {
    svg += `<rect class="band" x="${tx(b.from) - 1}" y="${m.t}" width="${Math.max(tx(b.to) - tx(b.from) + 2, 2)}" height="${ih}" fill="${b.color}"><title>${esc(b.label || '')}</title></rect>`;
  }
  svg += '<g class="grid">' + yticks.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${ty(v)}" y2="${ty(v)}"/>`).join('') + '</g>';
  svg += '<g class="ax">' + yticks.map((v) => `<text x="${m.l - 6}" y="${ty(v)}" text-anchor="end" dominant-baseline="middle">${o.log ? '1e' + Math.round(Math.log10(v)) : tickFmt(v)}</text>`).join('');
  const xt = o.xTicks || niceTicks(0, Math.max(n - 1, 1), 6).filter((v) => v <= n - 1 && Number.isInteger(v));
  svg += xt.map((v) => `<text x="${tx(v)}" y="${H - 6}" text-anchor="middle">${o.xFmt ? o.xFmt(v) : v}</text>`).join('') + '</g>';
  for (const v of o.vlines || []) {
    svg += `<line class="thr" x1="${tx(v.x)}" x2="${tx(v.x)}" y1="${m.t}" y2="${m.t + ih}"/><text x="${tx(v.x) + 4}" y="${m.t + 10}" style="font-size:11px;fill:var(--ink-3)">${esc(v.label)}</text>`;
  }
  for (const t of o.thresholds || []) {
    svg += `<line class="thr" x1="${m.l}" x2="${W - m.r}" y1="${ty(t.y)}" y2="${ty(t.y)}"/><text x="${W - m.r - 2}" y="${ty(t.y) - 4}" text-anchor="end" style="font-size:11px;fill:var(--ink-3)">${esc(t.label)}</text>`;
  }
  for (const s of o.series) {
    let d = '', pen = false;
    s.values.forEach((v, i) => {
      if (v == null || !Number.isFinite(v) || (o.log && v <= 0)) { pen = false; return; }
      d += (pen ? 'L' : 'M') + tx(i).toFixed(1) + ',' + ty(v).toFixed(1);
      pen = true;
    });
    svg += `<path class="ser" d="${d}" stroke="${s.color}" ${s.dash ? 'stroke-dasharray="5 4"' : ''} ${s.width ? `stroke-width="${s.width}"` : ''} ${s.opacity ? `opacity="${s.opacity}"` : ''}/>`;
    if (s.step) { /* drawn as polyline already */ }
    for (const i of s.markers || []) {
      const v = s.values[i];
      if (v == null) continue;
      svg += `<circle cx="${tx(i)}" cy="${ty(v)}" r="4.5" fill="${s.color}" stroke="var(--panel)" stroke-width="2"/>`;
    }
    for (const i of s.xmarks || []) {
      svg += `<line x1="${tx(i)}" x2="${tx(i)}" y1="${m.t + ih - 10}" y2="${m.t + ih}" stroke="${s.color}" stroke-width="2"/>`;
    }
  }
  svg += `<line class="ph" x1="0" x2="0" y1="${m.t}" y2="${m.t + ih}" style="display:none"/>`;
  svg += `<line class="hov" x1="0" x2="0" y1="${m.t}" y2="${m.t + ih}" style="display:none"/>`;
  svg += `<rect x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent" class="hit"/></svg>`;
  el.innerHTML = `<div class="chart">${svg}</div>` + (o.series.length > 1 || o.legend ? `<div class="chart-legend">${o.series.filter((s) => s.name).map((s) => `<span><i class="${s.dash ? 'dash' : ''}" style="background:${s.color};color:${s.color}"></i>${esc(s.name)}</span>`).join('')}${o.legendExtra || ''}</div>` : '');
  const svgEl = $('svg', el);
  const hov = $('.hov', el), ph = $('.ph', el);
  const hitEl = $('.hit', el);
  hitEl.addEventListener('mousemove', (ev) => {
    const r = svgEl.getBoundingClientRect();
    const sx = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round(((sx - m.l) / iw) * (n - 1))));
    hov.setAttribute('x1', tx(i)); hov.setAttribute('x2', tx(i)); hov.style.display = '';
    const lines = o.series.filter((s) => s.name).map((s) => {
      const v = s.values[i];
      return `<div class="tt-row"><i style="background:${s.color}"></i>${esc(s.name)}：${v == null || !Number.isFinite(v) ? '<b>缺失</b>' : o.log ? v.toExponential(2) : (+v.toFixed(5)).toString()}</div>`;
    }).join('');
    showTip(`<b>${o.xFmt ? (o.xName || '') + ' ' + o.xFmt(i) : `第 ${i} 行`}</b>${o.tipExtra ? o.tipExtra(i) : ''}${lines}`, ev.clientX, ev.clientY);
    if (o.onHover) o.onHover(i);
  });
  hitEl.addEventListener('mouseleave', () => { hov.style.display = 'none'; hideTip(); });
  hitEl.addEventListener('click', (ev) => {
    const r = svgEl.getBoundingClientRect();
    const sx = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round(((sx - m.l) / iw) * (n - 1))));
    if (o.onClick) o.onClick(i);
  });
  return {
    setPlayhead(i) {
      if (i == null) { ph.style.display = 'none'; return; }
      ph.setAttribute('x1', tx(i)); ph.setAttribute('x2', tx(i)); ph.style.display = '';
    },
  };
}

// ------------------------------------------------------------ signal derivations
const col = (M, j) => (M || []).map((r) => (r && r[j] != null && Number.isFinite(r[j]) ? r[j] : null));
function rotErr(M, off) {
  return (M || []).map((r) => {
    if (!r) return null;
    const a = r.slice(off, off + 3), b = r.slice(off + 3, off + 6);
    if ([...a, ...b].some((v) => v == null || !Number.isFinite(v))) return null;
    const na = Math.hypot(...a), nb = Math.hypot(...b), dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    return Math.max(Math.abs(na - 1), Math.abs(nb - 1), Math.abs(dot), 1e-9);
  });
}
const diff = (a) => (a || []).map((v, i) => (i === 0 || v == null || a[i - 1] == null ? null : v - a[i - 1]));
function corr(a, b) {
  const xs = [], ys = [];
  for (let i = 0; i < Math.min(a.length, b.length); i++) if (a[i] != null && b[i] != null && Number.isFinite(a[i]) && Number.isFinite(b[i])) { xs.push(a[i]); ys.push(b[i]); }
  if (xs.length < 5) return null;
  const mx = xs.reduce((s, v) => s + v, 0) / xs.length, my = ys.reduce((s, v) => s + v, 0) / ys.length;
  let sxy = 0, sxx = 0, syy = 0;
  for (let i = 0; i < xs.length; i++) { sxy += (xs[i] - mx) * (ys[i] - my); sxx += (xs[i] - mx) ** 2; syy += (ys[i] - my) ** 2; }
  return sxx && syy ? sxy / Math.sqrt(sxx * syy) : null;
}
function norm01(a) {
  const f = a.filter((v) => v != null && Number.isFinite(v));
  if (!f.length) return a;
  const lo = Math.min(...f), hi = Math.max(...f);
  return a.map((v) => (v == null ? null : hi > lo ? (v - lo) / (hi - lo) : 0));
}
const SER = ['var(--c-t)', 'var(--c-s)', 'var(--c-c)', 'var(--c-v)'];
const ARM = { left: { pos: 0, rot: 3, grip: 9, cn: '左臂' }, right: { pos: 10, rot: 13, grip: 19, cn: '右臂' } };

// ------------------------------------------------------------ episode drawer
async function openEpisode(stage, ep, row = 0) {
  openDrawer(`<div class="ep-head"><h2>${epName(ep)}</h2><span class="sub"><span class="spin"></span> 读取原始数据</span></div>`);
  const d = await api(`/api/episode?stage=${stage}&ep=${ep}`);
  if (!d.ok) return openDrawer(`<div class="ep-head"><h2>${epName(ep)}</h2></div><p class="banner">${esc(d.error)}</p>`);
  const r = d.report;
  const res = S.res[stage];
  const e = res?.episodes.find((x) => x.id === ep) || {};
  const codes = e.codes || [];
  const dict = res?.code_dict || {};
  const raw = d.raw;
  const n = raw.ok ? raw.n : 0;
  const govern = res?.kind === 'govern';
  const shownStatus = govern ? r.final_status : r.status;
  const head = `<div class="ep-head"><h2 id="drawerTitle">${epName(ep)}</h2>
    <span class="badge b-${STATUS_CLS[shownStatus] || 'bad'}"><i></i>${esc(shownStatus)}</span>
    <span class="badge">${govern ? '' : '不修复时 '}可训练 ${fmt(+r.train_frames)} / ${fmt(+r.rows)} 帧</span>
    <span class="badge">${esc(r.train_policy)}</span></div>`;
  if (!raw.ok || !n) {
    return openDrawer(`${head}<div class="sect"><p class="banner" style="border-color:var(--line-2)">原始文件读不出来：${esc(raw.error || r.issue_details)}</p></div>${codeListHtml(codes, dict, r)}`);
  }
  let M = null;
  try { M = storyModel({ e, d, imp: { repair: govern }, dict }); } catch (err) { M = null; }
  // interval lanes
  const byCode = {};
  for (const it of d.intervals) (byCode[it.issue_code] = byCode[it.issue_code] || []).push(it);
  const lanes = Object.entries(byCode).map(([c, its]) => `<div class="lane"><span title="${esc(c)}">${esc(codeCn(c, dict))}</span><div class="lt">${its.map((it) => `<i style="left:${(+it.start_row / n) * 100}%;width:${Math.max(((+it.end_row - +it.start_row + 1) / n) * 100, 0.4)}%;background:var(${CAT_VAR[catOf(c)]})" title="第 ${it.start_row}–${it.end_row} 行（${it.frames} 帧）${it.streams ? ' · ' + esc(it.streams) : ''}"></i>`).join('')}<b class="ph"></b></div></div>`).join('');
  const mask = d.mask;
  const keepLane = mask ? `<div class="lane"><span>进入训练</span><div class="lt">${runsOf(mask.map((m) => m.keep)).map(([a, b]) => `<i style="left:${(a / n) * 100}%;width:${((b - a + 1) / n) * 100}%;background:var(--keep)"></i>`).join('')}<b class="ph"></b></div></div>` : '';
  const dims = ['structure', 'temporal', 'sync', 'content', 'value'];
  openDrawer(`${head}
    ${M ? `<div class="ep-lead"><p>${esc(M.problem)}</p><p class="muted">${esc(M.keepText)}</p></div>` : ''}
    <div class="ep-grid">
      <div>
        <div class="cams" id="dCams">${['image', 'left_wrist_image', 'right_wrist_image'].map((c, i) => `<div class="cam" data-cam="${c}"><img alt=""><span>${['场景', '左腕', '右腕'][i]}</span><em></em></div>`).join('')}</div>
        <div class="scrub"><button class="ghost sm" id="dPlay">播放</button><input type="range" id="dRow" min="0" max="${n - 1}" value="${Math.min(row, n - 1)}"><span class="rowlab" id="dRowLab"></span></div>
        <div class="sect"><h4>问题区间</h4><div class="lanes" id="dLanes">${lanes || '<span class="muted">没有帧级问题区间</span>'}${keepLane}</div></div>
        <div class="sect"><div class="tabs" id="dTabs"></div><div id="dChart"></div></div>
      </div>
      <div>
        ${codeListHtml(govern ? (e.final_codes || []) : codes, dict, r, d.image_evidence)}
        <div class="sect"><h4>去向</h4><dl class="kv">
          <dt>决策</dt><dd>${esc(r.train_policy)}</dd>
          <dt>可训练</dt><dd>${fmt(+r.train_frames)} 帧（${r.clip_count} 段）</dd>
          ${govern && r.repair_applied ? `<dt>已修复</dt><dd>${esc(r.repair_applied)}</dd>` : ''}
          ${govern && r.repair_rejected ? `<dt>未修原因</dt><dd>${esc(r.repair_rejected)}</dd>` : ''}
        </dl></div>
        <details class="sect dims-fold"><summary>质量分 ${fmt(+r.overall_score, 1)}<span class="muted"> · 五维加权，和可训练帧数是两回事</span></summary><div class="dims">${dims.map((k) => { const v = +r[k + '_score']; return `<div class="dim"><span>${DIM_CN[k]}</span><div class="t"><i style="width:${Number.isFinite(v) ? v : 0}%"></i></div><span class="v">${Number.isFinite(v) ? fmt(v, 1) : '—'}</span></div>`; }).join('')}</div></details>
      </div>
    </div>`);
  // charts
  const bands = d.intervals.map((it) => ({ from: +it.start_row, to: +it.end_row, color: `var(${CAT_VAR[catOf(it.issue_code)]})`, label: it.issue_code }));
  const st = raw.state, ts = raw.timestamp;
  const tabs = [
    ['末端位置', () => ({ series: ['left', 'right'].flatMap((a) => [0, 1, 2].map((k) => ({ name: `${ARM[a].cn} ${'xyz'[k]}`, values: col(st, ARM[a].pos + k), color: SER[k], dash: a === 'right' }))) })],
    ['夹爪', () => ({ series: ['left', 'right'].map((a, i) => ({ name: `${ARM[a].cn}夹爪`, values: col(st, ARM[a].grip), color: SER[i] })) })],
    ['Rotation-6D 约束', () => ({ log: true, series: ['left', 'right'].map((a, i) => ({ name: `${ARM[a].cn}正交误差`, values: rotErr(st, ARM[a].rot), color: SER[i] })), thresholds: [{ y: 1e-4, label: '容差 1e-4' }] })],
    ['时间戳间隔', () => ({ series: [{ name: 'Δt（秒）', values: diff(ts), color: SER[0] }], thresholds: [{ y: 1 / (S.info.dataset.fps || 10), label: '标称周期' }] })],
    ['帧号与索引', () => ({ series: [{ name: 'frame_index', values: raw.frame_index, color: SER[0] }, { name: 'index − 首值', values: (raw.index || []).map((v) => (v == null || raw.index[0] == null ? null : v - raw.index[0])), color: SER[1], dash: true }] })],
  ];
  const defaultTab = codes.some((c) => c.startsWith('J_ROT6D')) ? 2 : codes.some((c) => /^T_/.test(c)) ? 3 : codes.some((c) => c.startsWith('J_GRIP')) ? 1 : 0;
  let chart;
  const drawTab = (k) => {
    $$('#dTabs button').forEach((b, i) => b.classList.toggle('on', i === k));
    const spec = tabs[k][1]();
    chart = lineChart($('#dChart'), { n, height: 200, bands, ...spec, onClick: (i) => setRow(i) });
    chart.setPlayhead(+$('#dRow').value);
  };
  $('#dTabs').innerHTML = tabs.map(([l]) => `<button>${l}</button>`).join('');
  $$('#dTabs button').forEach((b, i) => b.addEventListener('click', () => drawTab(i)));
  const setRow = (i) => {
    i = Math.max(0, Math.min(n - 1, i));
    $('#dRow').value = i;
    const rc = d.row_codes[i] || '';
    $('#dRowLab').textContent = `第 ${i} 行 · ${raw.frame_index?.[i] ?? '—'} 帧`;
    for (const c of ['image', 'left_wrist_image', 'right_wrist_image']) {
      const box = $(`#dCams [data-cam=${c}]`);
      const p = raw.paths?.[c]?.[i];
      const img = $('img', box);
      img.onerror = () => { box.classList.add('missing'); img.style.visibility = 'hidden'; };
      img.onload = () => { box.classList.remove('missing'); img.style.visibility = ''; };
      img.src = `/api/frame?stage=${stage}&ep=${ep}&row=${i}&cam=${c}&src=raw`;
      $('em', box).textContent = p || '无 path';
    }
    $$('#dLanes .ph').forEach((ph) => { ph.style.left = (i / n) * 100 + '%'; });
    chart && chart.setPlayhead(i);
    $('#dRowLab').title = rc;
  };
  $('#dRow').addEventListener('input', (ev) => setRow(+ev.target.value));
  $('#dPlay').addEventListener('click', () => {
    if (S.drawerTimer) { clearInterval(S.drawerTimer); S.drawerTimer = null; $('#dPlay').textContent = '播放'; return; }
    $('#dPlay').textContent = '暂停';
    S.drawerTimer = setInterval(() => { const v = +$('#dRow').value + 1; if (v >= n) { clearInterval(S.drawerTimer); S.drawerTimer = null; $('#dPlay').textContent = '播放'; return; } setRow(v); }, 1000 / (S.info.dataset.fps || 10));
  });
  drawTab(defaultTab);
  setRow(Math.min(row, n - 1));
}

function runsOf(bools) {
  const out = [];
  let s = null;
  bools.forEach((b, i) => {
    if (b && s == null) s = i;
    if (!b && s != null) { out.push([s, i - 1]); s = null; }
  });
  if (s != null) out.push([s, bools.length - 1]);
  return out;
}

const camText = (s) => String(s || '').replace(/\b(left_wrist_image|right_wrist_image|image)\b/g, (m) => CAM_CN[m] || m);
function codeListHtml(codes, dict, r, ie = {}) {
  const details = {};
  for (const m of (r.issue_details || '').matchAll(/([A-Z]_[A-Z0-9_]+)\(([^()]*(?:\([^()]*\)[^()]*)*)\)/g)) details[m[1]] = m[2];
  for (const [c, x] of Object.entries(ie || {})) if (x && x.cams && details[c] != null) details[c] += ` · ${x.cams.map((k) => `${CAM_CN[k]} ${fmt(x.frames?.[k] || 0)} 帧`).join('、')}`;
  return `<div class="sect"><h4>问题（${codes.length}）</h4><div class="code-list">${codes.length ? codes.map((c) => `<div class="code-item"><span class="code"><i style="background:var(${CAT_VAR[catOf(c)]})"></i>${esc(codeCn(c, dict))}</span><small class="mono muted">${esc(c)}</small>${details[c] ? `<div class="ci-d">${esc(camText(details[c]))}</div>` : ''}</div>`).join('') : '<span class="muted">无问题</span>'}</div></div>`;
}

function peakLag(values, L) {
  let k = -1, best = -2;
  (values || []).forEach((v, i) => { if (v != null && v > best) { best = v; k = i; } });
  return k < 0 ? null : { lag: k - L, r: best };
}


function lagCorr(a, b, L = 12) {
  // 返回 lag ∈ [-L, L] 的相关系数；lag = +k 表示 a 第 t 行的画面对应 b 第 t+k 行（与 path 帧号偏移同号）
  const out = [];
  for (let lag = -L; lag <= L; lag++) {
    const x = [], y = [];
    for (let t = 0; t < b.length; t++) {
      const u = t - lag;
      if (u < 0 || u >= a.length) continue;
      x.push(a[u]); y.push(b[t]);
    }
    out.push(corr(x, y));
  }
  return out;
}

// ------------------------------------------------------------ compare (before / after)
async function governRes() {
  const run = S.runs.govern;
  if (!run) return null;
  if (S.res[run.id]) return S.res[run.id];
  const r = await api(`/api/result?stage=${run.id}`);
  if (!r.ok) return null;
  S.res[run.id] = r;
  Object.assign(run, { created: r.run?.created, version: r.run?.version });
  updateCurChip();
  return r;
}

function shiftPairs(res) {
  const man = res.manifest || [];
  const fixed = man.filter((m) => m.kind && m.kind !== '未修改' && /图像整体移回/.test(m.applied || ''));
  const refused = man.filter((m) => m.kind === '未修改' && /重对齐/.test(m.rejected || ''));
  const cfg = S.info?.compare_pair || {};
  const pick = (arr, id) => arr.find((m) => +m.episode_index === +id) || arr[0] || null;
  return { fixed: pick(fixed, cfg.fixed), refused: pick(refused, cfg.refused) };
}

function pairCard(m, kind, res, activeEp) {
  if (!m) return '';
  const ep = +m.episode_index;
  const text = kind === 'fixed' ? m.applied : m.rejected;
  const sh = shiftFromText(text);
  const r = crossR(text);
  const e = (res.episodes || []).find((x) => x.id === ep) || {};
  return `<button class="pair-case ${kind} ${activeEp === ep ? 'on' : ''}" data-ep="${ep}">
    <span class="pc-kicker">相机疑似错位</span>
    <div class="pc-top"><b>${epName(ep)}</b><span class="pc-r">相关度 <b>${r == null ? '—' : r.toFixed(2)}</b>${r != null ? ` ${kind === 'fixed' ? '≥' : '<'} 0.80` : ''}</span></div>
    <div class="pc-verdict">${kind === 'fixed' ? `修了 · 移回 ${Math.abs(sh?.frames ?? 0)} 帧` : '没修 · 保持原样'}</div>
    <div class="pc-foot">${fmt(e.train_frames)} 帧可训练</div>
  </button>`;
}

function pairBannerHtml(res, activeEp) {
  const pair = shiftPairs(res);
  if (!pair.fixed || !pair.refused) return '';
  return `<div class="panel pair">
    <div class="pair-head"><h3>同样的现象，一个修、一个不修</h3></div>
    <div class="pair-cases">${pairCard(pair.fixed, 'fixed', res, activeEp)}${pairCard(pair.refused, 'refused', res, activeEp)}</div>
  </div>`;
}

// ------------------------------------------------------------ before / after: whole batch, same detector re-run on the repaired copies
function baPair(label, b, a, opt = {}) {
  const d = Number.isFinite(b) && Number.isFinite(a) ? a - b : null;
  const good = d == null || d === 0 ? '' : (opt.lower ? d < 0 : d > 0) ? 'good' : 'worse';
  const f = (v) => (opt.dec ? fmt(v, opt.dec) : fmt(v));
  const pctTxt = d && b ? ` · ${d > 0 ? '+' : ''}${((d / b) * 100).toFixed(1)}%` : '';
  return `<div class="bap ${good}">
    <span class="bap-l">${label}</span>
    <div class="bap-v"><s>${f(b)}</s><em>→</em><b>${f(a)}</b></div>
    <span class="bap-d">${d == null ? '' : d === 0 ? '不变' : `${d > 0 ? '+' : ''}${opt.dec ? fmt(d, opt.dec) : fmt(d)}${pctTxt}`}</span>
  </div>`;
}

function overallHtml(res) {
  const C = res.compare;
  if (!C) return '<div class="panel empty"><h3>这次运行没有修复前后数据</h3></div>';
  const sb = C.status_before || {}, sa = C.status_after || {};
  const maxCat = Math.max(1, ...CATS.map((c) => Math.max(C.cat_rows_before[c] || 0, C.cat_rows_after[c] || 0)));
  const catRows = CATS.map((c) => {
    const b = C.cat_rows_before[c] || 0, a = C.cat_rows_after[c] || 0;
    const eb = C.cat_eps_before[c] || 0, ea = C.cat_eps_after[c] || 0;
    return `<div class="bac">
      <span class="bac-l"><i style="background:var(${CAT_VAR[c]})"></i>${c}</span>
      <div class="bac-t"><i class="b" style="width:${(b / maxCat) * 100}%"></i><i class="a" style="width:${(a / maxCat) * 100}%;background:var(${CAT_VAR[c]})"></i></div>
      <span class="bac-v">${b || a ? `${fmt(b)}<em>→</em><b>${fmt(a)}</b> 帧` : '<small>整条级</small>'}</span>
      <span class="bac-e">${eb}<em>→</em>${ea} 条</span>
    </div>`;
  }).join('');
  const T = trainTiers(res);
  const ch = (C.changed || []).map((x) => `<tr class="click" data-ep="${x.ep}">
      <td><b>${epName(x.ep)}</b></td>
      <td class="st2"><span class="st st-${STATUS_CLS[x.status_before] || 'bad'}">${esc(x.status_before)}</span><em>→</em><span class="st st-${STATUS_CLS[x.status_after] || 'bad'}">${esc(x.status_after)}</span></td>
      <td>${x.resolved.map((c) => `<span class="code gone" title="${esc(c)}"><i style="background:var(${CAT_VAR[catOf(c)]})"></i>${esc(codeCn(c, res.code_dict))}</span>`).join(' ')}${x.new.map((c) => `<span class="code new" title="${esc(c)}"><i style="background:var(${CAT_VAR[catOf(c)]})"></i>${esc(codeCn(c, res.code_dict))}</span>`).join(' ')}${x.remaining.length ? `<span class="muted"> 仍有 ${x.remaining.length} 类</span>` : ''}</td>
      <td class="num">${fmt(x.train_frames)}</td>
      <td class="nw">${esc(x.policy || '')}</td>
    </tr>`).join('');
  return `<div class="ov">
    <section class="panel ov-head">
      <h2>修复前 <em>vs</em> 修复后</h2>
      <div class="bap-grid">
        ${baPair('需复核', sb['需复核'] || 0, sa['需复核'] || 0, { lower: true })}
        ${baPair('通过', sb['通过'] || 0, sa['通过'] || 0)}
        ${baPair('问题帧', C.flag_rows_before, C.flag_rows_after, { lower: true })}
        ${baPair('可训练帧', T.detectOnly ?? T.base, T.total)}
      </div>
    </section>
    <section class="panel card">
      <div class="card-h"><h3>逐帧全景</h3><div class="legend">${CATS.map((c) => `<span><i style="background:var(${CAT_VAR[c]})"></i>${c}</span>`).join('')}<span><i style="background:var(--fix)"></i>修复改写</span><span><i style="background:var(--cell)"></i>无问题</span></div></div>
      <div class="ov-panos">
        <div><div class="ov-pt"><span class="dot ov-b"></span>修复前<b>${fmt(C.flag_rows_before)}</b><small>问题帧</small></div><div class="pano-wrap"><canvas data-pano="before"></canvas></div></div>
        <div><div class="ov-pt"><span class="dot ov-f"></span>修复后复检<b>${fmt(C.flag_rows_after)}</b><small>问题帧</small></div><div class="pano-wrap"><canvas data-pano="final"></canvas></div></div>
      </div>
    </section>
    <div class="ov-two">
      <section class="panel card">
        <div class="card-h"><h3>四类问题</h3><span class="sub">灰 = 修复前 · 彩 = 复检</span></div>
        <div class="bacs">${catRows}</div>
      </section>
      <section class="panel card">
        <div class="card-h"><h3>有变化的轨迹 · ${(C.changed || []).length} 条</h3></div>
        <div class="tbl-wrap ov-changed"><table class="tbl"><thead><tr><th>轨迹</th><th>状态</th><th>问题变化</th><th>可训练</th><th>去向</th></tr></thead><tbody>${ch || '<tr><td colspan="5" class="muted">没有轨迹发生变化</td></tr>'}</tbody></table></div>
      </section>
    </div>
  </div>`;
}

function bindOverall(host, res, onEp) {
  $$('canvas[data-pano]', host).forEach((c) => drawPanorama(c, res, c.dataset.pano, res.stage));
  $$('tr.click[data-ep]', host).forEach((tr) => tr.addEventListener('click', () => onEp(+tr.dataset.ep)));
}

// 第 4 步的出口：拿去训练的、交给别人看的、要人工处理的
function exportHtml(res) {
  const g = res.governance || {};
  const review = res.episodes.filter((e) => e.final_status === '需复核' || ['隔离回采', '文件不可用', '处理失败'].includes(e.policy)).length;
  const href = (w) => `/api/export?stage=${encodeURIComponent(res.stage)}&what=${w}`;
  return `<section class="panel exports">
    <h3>导出</h3>
    <a class="exp ${g.train_frames?.total ? '' : 'off'}" href="${href('train')}" download><b>可用数据包</b><span>${fmt(g.train_frames?.total)} 帧 · 片段清单 + 掩膜 + 修复副本</span></a>
    <a class="exp" href="${href('report')}" download><b>质检报告</b><span>逐条 / 逐帧 / 问题区间 + Excel</span></a>
    <a class="exp ${review ? '' : 'off'}" href="${href('review')}" download><b>复核清单</b><span>${review ? `${review} 条待人工复核或回采` : '没有待复核的轨迹'}</span></a>
  </section>`;
}

function showOverallInStep4(res) {
  S.cmpEp = 'overall';
  if (S.cmpTimer) { clearInterval(S.cmpTimer); S.cmpTimer = null; }
  $$('#cmpList .case').forEach((b) => b.classList.toggle('on', b.dataset.ep === 'overall'));
  const main = $('#cmpMain');
  main.innerHTML = overallHtml(res) + exportHtml(res);
  bindOverall(main, res, (ep) => { selectCase(ep); window.scrollTo({ top: 0, behavior: 'smooth' }); });
}

async function renderCompare() {
  const list = $('#cmpList'), main = $('#cmpMain');
  const res = await governRes();
  if (!res) {
    list.hidden = true;
    main.innerHTML = `<div class="panel empty"><h3>${S.cur ? '先完成第 3 步修复' : '先选择要处理的数据'}</h3><button class="cta" data-goto="${S.cur ? STEP.govern : STEP.data}">${S.cur ? '去修复' : '去导入数据'}</button></div>`;
    S.cmpFor = null;
    return;
  }
  if (S.cmpFor === res.stage && S.cmpEp != null && main.childElementCount) return;
  S.cmpFor = res.stage;
  if (res.episodes.length === 1) {
    list.hidden = true;
    $('.cmp-grid').classList.add('single');
    await selectCase(res.episodes[0].id, res.stage);
    main.insertAdjacentHTML('beforeend', exportHtml(res));
    return;
  }
  $('.cmp-grid').classList.remove('single');
  list.hidden = false;
  const pair = shiftPairs(res);
  const hasPair = pair.fixed && pair.refused;
  const pairIds = new Set(hasPair ? [+pair.fixed.episode_index, +pair.refused.episode_index] : []);
  const man = res.manifest || [];
  const fixed = man.filter((m) => m.kind !== '未修改');
  const refusedAll = man.filter((m) => m.kind === '未修改' && m.rejected);
  const seen = new Set();
  const refused = [];
  for (const m of [...refusedAll].sort((a, b) => (/image|图像/.test(b.rejected) ? 1 : 0) - (/image|图像/.test(a.rejected) ? 1 : 0))) {
    const k = reasonLabel(m.rejected.split('；')[0]);
    if (seen.has(k) && refused.length > 5) continue;
    seen.add(k);
    refused.push(m);
  }
  const item = (m, kind) => {
    const desc = kind === 'fix' ? fixLabel(m) : reasonLabel((m.rejected || '').split('；')[0]);
    return `<button class="case" data-ep="${m.episode_index}"><b>${epName(+m.episode_index)}</b><span class="d">${esc(desc)}</span></button>`;
  };
  const mini = (m, kind) => {
    const ep = +m.episode_index;
    const r = crossR(kind === 'fixed' ? m.applied : m.rejected);
    return `<button class="case mini-case ${kind}" data-ep="${ep}"><b>${epName(ep)}</b><em>${r == null ? '' : 'r=' + r.toFixed(2) + ' · '}${kind === 'fixed' ? '修了' : '没修'}</em></button>`;
  };
  const otherFixed = fixed.filter((m) => !pairIds.has(+m.episode_index));
  const otherRefused = refused.filter((m) => !pairIds.has(+m.episode_index));
  list.innerHTML = `<button class="case case-overall" data-ep="overall"><b>全量对比</b><span class="d">${fmt(res.episodes.length)} 条修复前 vs 修复后</span></button>
    ${hasPair ? `<div class="mini-pair"><h4>同样的现象，一个修、一个不修</h4><div class="mini-cases">${mini(pair.fixed, 'fixed')}${mini(pair.refused, 'refused')}</div></div>` : ''}
    ${otherFixed.length ? `<h4>已修复 · ${otherFixed.length}</h4>${otherFixed.map((m) => item(m, 'fix')).join('')}` : ''}
    ${otherRefused.length ? `<h4>拒修示例</h4>${otherRefused.map((m) => item(m, 'ref')).join('')}` : ''}
    <button class="case case-notes" data-ep="notes"><b>修复规则</b><span class="d">每类问题怎么修、什么条件下不修</span></button>`;
  $$('.case', list).forEach((b) => b.addEventListener('click', () => (b.dataset.ep === 'notes' ? showNotesInStep4() : b.dataset.ep === 'overall' ? showOverallInStep4(res) : selectCase(+b.dataset.ep))));
  showOverallInStep4(res);
}

function showNotesInStep4() {
  S.cmpEp = 'notes';
  $$('#cmpList .case').forEach((b) => b.classList.toggle('on', b.dataset.ep === 'notes'));
  renderRepairNotes(S.runs.govern.id, $('#cmpMain'), (ep) => { selectCase(ep); window.scrollTo({ top: 0, behavior: 'smooth' }); });
}

function openCaseModal(stage, ep) {
  openModal('<div class="cmp-main" id="mCmp"></div>');
  selectCase(ep, stage, $('#mCmp'));
}

// ------------------------------------------------------------ repair notes (everything in one place)
async function renderRepairNotes(stage, host, onOpen) {
  host.innerHTML = '<div class="panel empty"><span class="spin"></span><p>整理修复说明</p></div>';
  const n = await api(`/api/repairs?stage=${stage}`);
  if (!n.ok) { host.innerHTML = `<div class="panel empty"><p>${esc(n.error)}</p></div>`; return; }
  const T = n.totals;
  const chip = (c, cls = '') => `<span class="code ${cls}"><i style="background:var(${CAT_VAR[catOf(c)]})"></i>${esc(c)}</span>`;
  const epBtn = (ep, extra = '') => `<button class="ep-chip" data-ep="${ep}">${epName(ep)}${extra}</button>`;
  const methods = n.methods.map((m) => `<tr><td><b>${esc(m.title)}</b><div class="muted">${m.codes.map(esc).join(' ')}</div></td><td>${esc(m.problem)}</td><td>${esc(m.action)}${m.note ? `<div class="muted">${esc(m.note)}</div>` : ''}</td><td>${esc(m.conditions)}</td><td>${esc(m.otherwise)}</td><td class="num"><b>${m.fixed}</b></td></tr>`).join('');
  const fixed = n.fixed.map((f) => `
    <article class="fix">
      <header>
        <b class="fix-ep">${epName(f.ep)}</b>
        <span class="badge b-fix"><i></i>${esc(f.kind)}</span>
        <span class="fix-title">${f.sections.map((s) => esc(s.title)).join('；')}</span>
        <span class="fix-score num">${fmt(f.score_before, 2)} → <b>${fmt(f.score_after, 2)}</b></span>
        <button class="ghost sm" data-ep="${f.ep}">看对比</button>
      </header>
      ${f.sections.map((s) => `<dl class="fix-dl">
        <dt>问题</dt><dd>${esc(s.problem)}</dd>
        <dt>怎么修</dt><dd>${esc(s.action)}</dd>
        ${s.why ? `<dt>依据</dt><dd>${esc(s.why)}</dd>` : ''}
        ${s.note ? `<dt>注意</dt><dd>${esc(s.note)}</dd>` : ''}
      </dl>`).join('')}
      <div class="fix-res">
        <span>问题码</span>${f.resolved.map((c) => chip(c, 'gone')).join('')}${f.remaining.map((c) => chip(c)).join('')}${f.new.map((c) => chip(c, 'new')).join('')}${!f.resolved.length && !f.remaining.length && !f.new.length ? '<span class="muted">无</span>' : ''}
        <span class="sep">治理决策 <b>${esc(f.policy)}</b></span>
        <span class="sep">训练帧 <b class="num">${fmt(f.train_frames)}</b>${f.clips.length ? `（第 ${f.clips.join('、')} 行）` : ''}</span>
        <span class="sep">${f.cells ? `改写 <b class="num">${fmt(f.cells)}</b> 格` : f.kind === '仅元数据补丁' ? 'Parquet <b class="num">0</b> 改动' : '数值改动 <b class="num">0</b>'}</span>
      </div>
      ${f.rejected_here ? `<p class="note">同一条轨迹里没有改的部分：${esc(f.rejected_here)}</p>` : ''}
    </article>`).join('');
  const refused = n.refused.map((g) => `<div class="ref">
      <div class="ref-h"><b>${esc(g.label)}</b><span class="muted num">${g.items.length} 项</span></div>
      ${g.why ? `<p>${esc(g.why)}</p>` : ''}
      <div class="ep-chips">${g.items.map((it) => epBtn(it.ep, it.repaired_elsewhere ? '<small>部分已修</small>' : '')).join('')}</div>
    </div>`).join('');
  const cands = n.candidates.map((c) => `<tr><td class="mono">${esc(c.code)}</td><td>${esc(c.action)}</td><td>${esc(c.because)}</td><td><div class="ep-chips">${c.eps.map((e) => epBtn(e)).join('')}</div></td></tr>`).join('');
  const nRef = n.refused.reduce((s, g) => s + g.items.length, 0);
  host.innerHTML = `<div class="notes">
    <div class="panel card"><div class="card-h"><h2>修复规则</h2><span class="sub">修了 ${T.fixed} 条 · 拒修 ${nRef} 项 · 待人工确认 ${T.candidates} 项</span></div>
      <div class="tbl-wrap notes-tbl"><table class="tbl"><thead><tr><th>问题类型</th><th>问题是什么</th><th>怎么修</th><th>必须满足</th><th>不满足时</th><th>本次</th></tr></thead><tbody>${methods}</tbody></table></div>
    </div>
    <div class="panel card"><div class="card-h"><h3>逐条修复（${n.fixed.length} 条）</h3></div>
      <div class="fixes">${fixed || '<p class="muted">本次没有执行修复</p>'}</div>
    </div>
    <div class="panel card"><div class="card-h"><h3>没有修的（${nRef} 项）</h3></div>
      <div class="refs">${refused || '<p class="muted">没有拒修项</p>'}</div>
    </div>
    <div class="panel card"><div class="card-h"><h3>有方案、不自动执行（${T.candidates} 项）</h3></div>
      ${cands ? `<div class="tbl-wrap notes-tbl"><table class="tbl"><thead><tr><th>问题码</th><th>候选方案</th><th>不自动执行的原因</th><th>涉及轨迹</th></tr></thead><tbody>${cands}</tbody></table></div>` : '<p class="muted">没有候选方案</p>'}
    </div>
  </div>`;
  $$('[data-ep]', host).forEach((b) => b.addEventListener('click', () => onOpen(+b.dataset.ep)));
}

async function selectCase(ep, stage = S.runs.govern?.id, main = $('#cmpMain')) {
  const inStep4 = main.id === 'cmpMain';
  if (S.cmpTimer) { clearInterval(S.cmpTimer); S.cmpTimer = null; }
  if (S.impTimer) { clearInterval(S.impTimer); S.impTimer = null; }
  if (inStep4) {
    S.cmpEp = ep;
    $$('#cmpList .case').forEach((b) => b.classList.toggle('on', b.dataset.ep === String(ep)));
  }
  main.innerHTML = `<div class="panel cmp-head"><div class="cmp-title"><h2>${epName(ep)}</h2><span class="sub"><span class="spin"></span> 读取修复前后数据</span></div></div>`;
  const d = await api(`/api/episode?stage=${stage}&ep=${ep}`);
  if (inStep4 && S.cmpEp !== ep) return;
  if (!d.ok) { main.innerHTML = `<p class="banner">${esc(d.error)}</p>`; return; }
  const m = d.manifest || {};
  const r = d.report;
  const res = S.res[stage] || {};
  const dict = res.code_dict || {};
  const e = (res.episodes || []).find((x) => x.id === ep) || {};
  const single = (res.episodes || []).length === 1;
  const fixed = !!(m.kind && m.kind !== '未修改');
  const before = (m.codes_before || r.issue_codes || '').split(';').filter(Boolean);
  const after = (m.codes_after || r.final_issue_codes || '').split(';').filter(Boolean);
  const audit = d.audit || [];
  const audCodes = [...new Set(audit.map((a) => a.repair_code))];
  const isShift = audCodes.includes('S_STREAM_SHIFTED')
    || (fixed ? /图像整体移回/.test(m.applied || '') : /重对齐/.test(m.rejected || ''));
  const keepN = e.train_frames ?? +r.train_frames;
  const notesBtn = !single ? '<button class="ghost sm" data-notes>修复规则</button>' : '';
  const foot = (count) => `<div class="cmp-foot"><span class="badge">可训练 ${fmt(keepN)} / ${fmt(d.raw.n)} 帧</span><span class="badge">${esc(count)}</span>${notesBtn}</div>`;
  let head;
  let kind = 'refused';
  if (isShift) {
    const text = fixed ? m.applied : m.rejected;
    const sh = shiftFromText(text);
    const gate = gateOf(text);
    const cam = sh?.cam ? CAM_CN[sh.cam] : '相机';
    const frames = Math.abs(sh?.frames ?? 0);
    head = `<div class="panel cmp-head shift-head">
      <div class="cmp-title">
        <h2>${epName(ep)}</h2>
        <span class="badge ${fixed ? 'b-fix' : 'b-hint'}"><i></i>${fixed ? '修了' : '没修'}</span>
        <span class="verdict-line">${cam}${fixed ? '整体错位' : '疑似错位'} ${signed(sh?.frames)} 帧${fixed ? ' · 已移回' : ' · 保持原样'}</span>
      </div>
      <p class="cmp-method">${fixed
        ? `${gate ? `${gate.label} r=${gate.r.toFixed(2)} ≥ ${gate.thr.toFixed(2)}，` : ''}对端恰好缺 ${frames} 帧：只移动${cam}这一列。`
        : `${gate ? `${gate.label}只有 r=${gate.r.toFixed(2)}，低于 ${gate.thr.toFixed(2)}` : '内容证据不足'}：偏移整齐不等于画面真的错了，不动。`}</p>
      ${foot(fixed ? `改写 ${fmt(audit.length)} 格` : '0 格改写')}
    </div>`;
  } else if (!fixed && !m.rejected) {
    // 没有修复、也没有拒修记录：要么干净 / 只有提示，要么是没有修复手段的问题（黑屏、模糊、马赛克…）
    kind = importType(d, e, dict);
    const blocking = after.filter((c) => blocksTraining(c, dict));
    const NR = noRepairNote(blocking, dict);
    const line = kind === 'norepair' ? `${codesText(blocking, dict)}：${NR.line}`
      : kind === 'hint' ? '只有不阻断训练的提示' : '没有问题';
    const detail = kind === 'norepair' ? (blocking.includes('C_IMAGE_COLOR') ? NR.action : '不能凭空生成画面或数据，不修；被标记的帧不进入训练。') : '不需要修复，修复前后一致。';
    head = `<div class="panel cmp-head shift-head">
      <div class="cmp-title"><h2>${epName(ep)}</h2><span class="badge ${kind === 'norepair' ? 'b-hint' : 'b-ok'}"><i></i>${kind === 'norepair' ? '没修' : '无需修复'}</span><span class="verdict-line">${esc(line)}</span></div>
      <p class="cmp-method">${esc(detail)}</p>
      ${foot('0 格改写')}
    </div>`;
  } else {
    const stateDim = d.raw.state?.[0]?.length || 0;
    const actionDim = d.raw.actions?.[0]?.length || 0;
    const totalCells = d.raw.n * (stateDim + actionDim);
    const nCells = audit.filter((a) => +a.row >= 0).length;
    let headline = fixed ? `修了：${fmt(nCells)} 格改动` : '拒修：保持原样';
    let detail = fixed ? (m.applied || '') : (m.rejected || '');
    let count = fixed ? `改写 ${fmt(nCells)} 格` : '0 格改写';
    if (!fixed) {
      headline = `拒修：${reasonLabel((m.rejected || '').split('；')[0])}`;
      detail = '';
    } else if (before.includes('J_STATE_NONFINITE')) {
      headline = `state 里 ${fmt(audit.length)} 个空值，只补这 ${fmt(audit.length)} 格`;
      detail = 'Rotation-6D 按正交约束解出，其余分量逐位不变。';
      count = `改写 ${fmt(audit.length)} / ${fmt(totalCells)} 格`;
    } else if (before.includes('J_POSITION_ENVELOPE')) {
      headline = `末端位置 ${fmt(audit.length)} 个越界分量，只改这几格`;
      detail = '孤立单帧越界取前后帧均值；连续越界不改。';
      count = `改写 ${fmt(audit.length)} / ${fmt(totalCells)} 格`;
    } else if (before.some((c) => /^S_TASK/.test(c))) {
      headline = '任务标签和元数据不一致，只改 task_index';
      detail = '按 episodes.jsonl 登记的任务统一整条；画面、动作、时间戳不动。';
      count = `改写 ${fmt(audit.length)} 行 task_index`;
    } else if (before.includes('C_SCHEMA_DTYPE_MISMATCH')) {
      headline = '字段类型和声明不符，无损转换';
      detail = '每个数值逐位不变。';
      count = '0 个数值变化';
    } else if (m.kind === '仅元数据补丁') {
      headline = '只改元数据记录，Parquet 不动';
      detail = 'episodes.jsonl 的长度与实际行数对齐。';
      count = 'Parquet 0 改动';
    }
    // 修了一部分、另一部分没修：讲清楚没修的是什么
    const remain = fixed ? remainAfterFix({ finalCodes: after, rowFinal: d.row_final_codes, dict }) : [];
    if (remain.length) detail += `但${remain.map((c) => codeCn(c, dict)).join('、')}没有修，${keepN === 0 ? '整条仍不能进入训练' : '带这些问题的帧不进入训练'}。`;
    head = `<div class="panel cmp-head shift-head">
      <div class="cmp-title"><h2>${epName(ep)}</h2><span class="badge ${fixed ? (remain.length ? 'b-hint' : 'b-fix') : 'b-hint'}"><i></i>${fixed ? (remain.length ? '修了一部分' : '修了') : '没修'}</span><span class="verdict-line">${esc(headline)}</span></div>
      ${detail ? `<p class="cmp-method">${esc(detail)}</p>` : ''}
      ${foot(count)}
    </div>`;
  }
  main.innerHTML = head + `<div class="panel card" id="cmpViz"><div class="sub"><span class="spin"></span> 生成对比图</div></div>${fixed ? auditHtml(audit, m) : ''}`;
  $$('[data-notes]', main).forEach((b) => b.addEventListener('click', () => showNotesInStep4()));
  $$('.pair-case', main).forEach((b) => b.addEventListener('click', () => { selectCase(+b.dataset.ep); window.scrollTo({ top: 0, behavior: 'smooth' }); }));
  const viz = $('#cmpViz', main);
  try {
    if (isShift) await vizShift(viz, d, ep, fixed);
    else if (audCodes.some((c) => /NONFINITE/.test(c))) vizCells(viz, d, '只补空值分量，其余逐位不变');
    else if (audCodes.includes('J_POSITION_ENVELOPE')) vizCells(viz, d, '只改越界分量');
    else if (audCodes.some((c) => /^S_TASK/.test(c))) vizTask(viz, d);
    else if (audCodes.includes('C_SCHEMA_DTYPE_MISMATCH')) vizDtype(viz, d);
    else if (m.kind === '仅元数据补丁') vizMeta(viz, d);
    else if (m.rejected) vizRefused(viz, d);
    else vizKeep(viz, d, e, res);
  } catch (err) {
    viz.innerHTML = `<p class="banner">${esc(String(err))}</p>`;
  }
}

// 没有修复动作的轨迹：画面、问题区间、哪些帧进入训练
function vizKeep(viz, d, e, res) {
  const n = d.raw.n;
  const M = storyModel({ e, d, imp: { repair: true }, dict: res.code_dict || {} });
  viz.innerHTML = `<div class="card-h"><h3>画面与去向</h3></div><div class="story-content">${camsHtml(M, n, d.intervals || [])}
    <div>${storyStrip(d, n, e.train_frames || 0)}${STRIP_LEGEND}</div></div>`;
  mountCams(viz, res, e, d, M);
}

function auditVal(v) {
  if (!v) return '';
  if (v.startsWith('{')) {
    try {
      const o = JSON.parse(v);
      if (o.path === null && o.bytes === null) return '空（显式缺图）';
      const sha = o.bytes?.bytes_sha256 ? ' · sha ' + o.bytes.bytes_sha256.slice(0, 8) : '';
      return (o.path || '') + sha;
    } catch (e) { return v; }
  }
  return v;
}

function auditHtml(audit, m) {
  const rows = audit.slice(0, 40);
  const nCell = audit.filter((a) => +a.row >= 0).length, nCol = audit.length - nCell;
  const cnt = [nCell ? `${fmt(nCell)} 格` : '', nCol ? `${fmt(nCol)} 条整列 / 元数据记录` : ''].filter(Boolean).join(' + ');
  return `<details class="audit-fold panel card"><summary><b>逐单元格修复审计</b><span>共 ${cnt} · repair_audit.csv · 点开看原值 → 新值</span></summary>
    <div class="tbl-wrap" style="max-height:340px;margin-top:12px"><table class="tbl"><thead><tr><th>行</th><th>字段</th><th>原值</th><th>新值</th><th>方法</th></tr></thead><tbody>
    ${rows.map((a) => `<tr><td>${+a.row < 0 ? '整列' : esc(a.row)}</td><td class="mono">${esc(a.field)}</td><td class="mono del">− ${esc(auditVal(a.original_value))}</td><td class="mono add">+ ${esc(auditVal(a.repaired_value))}</td><td>${esc(a.method)}</td></tr>`).join('')}
    </tbody></table></div>
    <dl class="kv" style="margin-top:12px"><dt>源文件 SHA256</dt><dd class="mono">${esc((m.source_sha256 || '').slice(0, 24))}…</dd>
    ${m.repaired_sha256 ? `<dt>副本 SHA256</dt><dd class="mono">${esc(m.repaired_sha256.slice(0, 24))}…</dd>` : ''}</dl></details>`;
}

function parseGates(text) {
  const out = [];
  const re = /(本臂运动时滞|双臂运动时滞\(场景相机\)|跨相机运动相关\(([a-z_]+)\))[^r]*?([+-]?\d+)?\s*\(?r=([0-9.]+)(?:[,，]\s*增益=([0-9.]+))?/g;
  let mm;
  while ((mm = re.exec(text || ''))) {
    const cross = mm[1].startsWith('跨相机');
    out.push({ label: cross ? `跨相机运动相关（参照${CAM_CN[mm[2]] || mm[2]}）` : mm[1].replace('(场景相机)', '（场景相机）'), r: +mm[4], gain: mm[5] != null ? +mm[5] : null, thr: cross ? 0.8 : 0.6 });
  }
  return out;
}

async function vizShift(viz, d, ep, fixed) {
  const mo = await api(`/api/motion?stage=${d.stage}&ep=${ep}`);
  const raw = d.raw, after = d.after;
  const n = raw.n;
  let cam = ['left_wrist_image', 'right_wrist_image', 'image'].find((c) => (d.audit || []).some((a) => a.field === c));
  if (!cam) {
    const mm = /(image|left_wrist_image|right_wrist_image)\s*重对齐/.exec(d.manifest?.rejected || '');
    cam = mm ? mm[1] : 'image';
  }
  const camCn = CAM_CN[cam] || cam;
  const refs = cam === 'image' ? ['left_wrist_image', 'right_wrist_image'] : [cam === 'left_wrist_image' ? 'right_wrist_image' : 'left_wrist_image'];
  const text = fixed ? d.manifest?.applied : d.manifest?.rejected;
  const gates = parseGates(text);
  const rr = crossR(text);
  const ar = armR(text);
  const gate = rr != null ? { r: rr, thr: 0.8, label: '跨相机' } : ar != null ? { r: ar, thr: 0.6, label: '本臂' } : null;
  const pn = (raw.paths?.[cam] || []).map(pathNum);
  const offB = pn.map((p, i) => (p == null ? null : p - i));
  const fin = offB.filter((v) => v != null).sort((a, b) => a - b);
  const off = fin.length ? fin[Math.floor(fin.length / 2)] : 0;
  const L = Math.max(12, Math.abs(off) + 6);
  const eRaw = mo.raw?.energy || {}, eAft = mo.after?.energy || {};
  let best = null;
  for (const rc of refs) {
    const c = lagCorr(eRaw[cam] || [], eRaw[rc] || [], L);
    const pk = peakLag(c, L);
    if (pk && (!best || pk.r > best.pk.r)) best = { rc, c, pk };
  }
  const ref = best?.rc || refs[0];
  const beforeCurve = best?.c || [];
  const beforePk = best?.pk || null;
  let afterCurve = null, afterPk = null;
  if (fixed && eAft[cam]) {
    afterCurve = lagCorr(eAft[cam], eAft[ref] || [], L);
    afterPk = peakLag(afterCurve, L);
  }
  const row0 = (() => {
    const e = (mo.raw?.energy?.[cam] || []).map((v, i) => ({ v, i }))
      .filter((x) => x.v != null && x.i > 2 && x.i < n - 3)
      .sort((a, b) => b.v - a.v);
    return e[0] ? e[0].i : Math.min(n - 1, Math.max(25, Math.abs(off) + 20));
  })();
  const auditLen = (d.audit || []).length;
  const moved = fixed ? (after?.paths?.[cam] || []).filter(Boolean).length : 0;
  const blanked = fixed ? n - moved : 0;
  const pathDiff = (c) => {
    const a = raw.paths?.[c] || [], b = after?.paths?.[c];
    return a.reduce((s, p, i) => s + (p !== (b?.[i] ?? p) ? 1 : 0), 0);
  };
  const otherTxt = CAMS.filter((c) => c !== cam).map((c) => {
    const d = pathDiff(c);
    return d ? `${CAM_CN[c]} ${fmt(d)} 格改动` : `${CAM_CN[c]} 未动`;
  }).join(' · ');
  const otherDiffTotal = CAMS.filter((c) => c !== cam).reduce((s, c) => s + pathDiff(c), 0);
  const camCell = (side, c, cls = '') => `<figure class="split-cam ${cls}" data-side="${side}" data-cam="${c}"><img alt=""><figcaption>${CAM_CN[c]}<em class="frame-no">—</em></figcaption></figure>`;
  const otherCams = CAMS.filter((c) => c !== cam);
  const hero = (side) => `<figure class="split-hero" data-side="${side}" data-cam="${cam}"><img alt=""><figcaption>${CAM_CN[cam]} · 错位的那一路<em class="frame-no">—</em></figcaption></figure>`;
  const thumbs = (side) => `<div class="split-thumbs">${otherCams.map((c) => camCell(side, c)).join('')}</div>`;
  const vals = [...beforeCurve, ...(afterCurve || [])].filter((v) => v != null && Number.isFinite(v));
  viz.innerHTML = `<div class="card-h"><h3>同一行，修复前 vs 修复后</h3></div>
    <div class="split ${fixed ? '' : 'one-sided'}">
      <div class="split-side">
        <div class="split-h"><span class="dot bad"></span><b>修复前</b><small>原始图像</small></div>
        ${hero('raw')}${thumbs('raw')}
      </div>
      <div class="split-mid"><span>同一行</span><b>→</b><small id="vzRowMid"></small></div>
      <div class="split-side ${fixed ? '' : 'refused'}">
        <div class="split-h"><span class="dot ${fixed ? 'fix' : 'bad'}"></span><b>${fixed ? '修复后' : '没有修复'}</b><small>${fixed ? 'Parquet 副本' : '保持原样'}</small></div>
        ${fixed
          ? `${hero('after')}${thumbs('after')}`
          : `<div class="refuse-body">
               <div class="refuse-stamp">没修</div>
               <p>${camCn} path 偏移 ${signed(off)} 帧，但门控统计只有 r=${gate ? gate.r.toFixed(2) : '—'}，低于 ${gate ? gate.thr.toFixed(2) : '0.80'}。</p>
             </div>`}
      </div>
    </div>
    <div class="split-scrub">
      <button class="ghost sm" id="vzPlay">▶ 播放</button>
      <input type="range" id="vzRow" min="0" max="${n - 1}" value="${row0}">
      <span class="rowlab" id="vzRowLab"></span>
    </div>
    <p class="split-note" id="vzAlign"></p>
    <div class="proof">
      <div>
        <div class="proof-h">
          <div>
            <div class="mini-h">画面运动互相关 vs 时滞 · 参照${CAM_CN[ref]}</div>
            <div class="peak-move ${fixed && afterPk ? '' : 'bad'}">${fixed && afterPk
              ? `<b>${signed(beforePk?.lag)}</b><span>→</span><b>${signed(afterPk.lag)}</b><small>峰值时滞</small>`
              : `<b>${signed(beforePk?.lag)}</b><small>峰值时滞 · 门控 r=${gate ? gate.r.toFixed(2) : '—'}</small>`}</div>
          </div>
          
        </div>
        <div id="vzLag"></div>
      </div>
      <div>
        <div class="mini-h">修复门槛：看画面内容，不看偏移</div>
        <div class="evidence" id="vzGate"></div>
      </div>
    </div>
    <div class="changed">
      <div class="chg-main ${fixed ? '' : 'bad'}"><b>${fmt(auditLen)}</b><span>格改写<br><small>${fixed ? `${fmt(moved)} 行移回 + ${fmt(blanked)} 行显式置空` : '没有生成修复副本'}</small></span></div>
      <div class="chg-keep"><b>${fixed ? fmt(otherDiffTotal) : '—'}</b><span>格改动<br><small>${fixed ? otherTxt : '整条保持原样'}</small></span></div>
      <div class="chg-verdict">${fixed ? `复检 ${fmt(+d.manifest.score_before, 2)} → <b>${fmt(+d.manifest.score_after, 2)}</b><small>质量分</small>` : `<b>${fmt(+d.report.train_frames || 0)}</b> 帧进入训练<small>${esc(d.report.train_policy || '')}</small>`}</div>
    </div>`;
  const setImg = (fig, src, row, path) => {
    const img = $('img', fig);
    const missing = !path;
    fig.classList.toggle('missing', missing);
    if (missing) { img.removeAttribute('src'); img.style.visibility = 'hidden'; }
    else { img.style.visibility = ''; img.src = `/api/frame?stage=${d.stage}&ep=${ep}&row=${row}&cam=${fig.dataset.cam}&src=${src}`; }
    $('.frame-no', fig).textContent = missing ? '缺图' : '第 ' + pathNum(path) + ' 帧';
  };
  const setRow = (i) => {
    i = Math.max(0, Math.min(n - 1, i));
    $('#vzRow', viz).value = i;
    $$('figure[data-side=raw]', viz).forEach((fig) => setImg(fig, 'raw', i, raw.paths?.[fig.dataset.cam]?.[i]));
    if (fixed) $$('figure[data-side=after]', viz).forEach((fig) => setImg(fig, 'after', i, after?.paths?.[fig.dataset.cam]?.[i]));
    const b = raw.paths?.[cam]?.[i];
    const a = after?.paths?.[cam]?.[i];
    $('#vzAlign').innerHTML = fixed
      ? `<b>${camCn}</b>：修复前显示第 ${b ? pathNum(b) : '—'} 帧，修复后显示第 ${a ? pathNum(a) : '缺图'} 帧。${otherTxt}。`
      : `<b>${camCn}</b>：仍显示第 ${b ? pathNum(b) : '—'} 帧。没有生成修复副本，${+d.report.train_frames ? `只有 ${fmt(+d.report.train_frames)} 帧不受影响的片段进入训练` : '整条不进入训练'}。`;
    $('#vzRowLab').textContent = `第 ${i} 行 · frame ${raw.frame_index?.[i] ?? '—'}`;
    $('#vzRowMid').textContent = `第 ${i} 行`;
  };
  const btn = $('#vzPlay', viz);
  const stop = () => { if (S.cmpTimer) clearInterval(S.cmpTimer); S.cmpTimer = null; btn.textContent = '▶ 播放'; };
  btn.addEventListener('click', () => {
    if (S.cmpTimer) return stop();
    let i = +$('#vzRow', viz).value;
    if (i >= n - 1) i = 0;
    btn.textContent = '❚❚ 暂停';
    S.cmpTimer = setInterval(() => { i += 1; if (i >= n) return stop(); setRow(i); }, 1000 / (S.info.dataset.fps || 10));
  });
  $('#vzRow', viz).addEventListener('input', (e) => { stop(); setRow(+e.target.value); });
  lineChart($('#vzLag', viz), {
    n: 2 * L + 1, height: 240, yMin: Math.min(-0.2, ...vals), yMax: 1,
    xFmt: (i) => signed(i - L), xName: '时滞',
    xTicks: Array.from({ length: 2 * L + 1 }, (_, i) => i).filter((i) => (i - L) % 5 === 0),
    series: [
      { name: `修复前 · 参照${CAM_CN[ref]}`, values: beforeCurve, color: 'var(--bad)', width: 2.6 },
      ...(afterCurve ? [{ name: `修复后 · 参照${CAM_CN[ref]}`, values: afterCurve, color: 'var(--fix)', width: 2.6 }] : []),
    ],
    thresholds: [{ y: gate ? gate.thr : 0.8, label: `门控 ${gate ? gate.thr.toFixed(2) : '0.80'}` }],
    vlines: [{ x: L, label: '0' }, ...(beforePk ? [{ x: L + beforePk.lag, label: '修复前峰值' }] : []), ...(afterPk ? [{ x: L + afterPk.lag, label: '修复后峰值' }] : [])],
  });
  $('#vzGate', viz).innerHTML = gates.length ? gates.map((g) => {
    const pass = g.r >= g.thr;
    return `<div class="gate ${pass ? 'pass' : 'fail'}"><span>${esc(g.label)}</span><b>r = ${g.r.toFixed(3)} ${pass ? '≥' : '<'} ${g.thr}${g.gain != null ? ` · 增益 ${g.gain.toFixed(3)}` : ''}</b>
      <div class="meter2"><i style="width:${Math.min(Math.max(g.r, 0), 1) * 100}%;background:var(${pass ? '--ok' : '--bad'})"></i><u style="left:${g.thr * 100}%"></u></div></div>`;
  }).join('') : '<p class="note">门控证据见上方说明。</p>';
  setRow(row0);
}

function vizCells(viz, d, title) {
  const raw = d.raw, after = d.after;
  const n = raw.n;
  const comps = [...new Set(d.audit.map((a) => a.field).filter((f) => /^state\[\d+\]$|^actions\[\d+\]$/.test(f)))];
  const rowsBy = {};
  for (const a of d.audit) (rowsBy[a.field] = rowsBy[a.field] || []).push(+a.row);
  const rotArms = [...new Set(comps.map((f) => +/\[(\d+)\]/.exec(f)[1]).filter((k) => k % 10 >= 3 && k % 10 < 9).map((k) => (k < 10 ? 'left' : 'right')))];
  const stateDim = raw.state?.[0]?.length || 0;
  const actionDim = raw.actions?.[0]?.length || 0;
  const totalCells = n * (stateDim + actionDim);
  const pctCells = totalCells ? ((d.audit.length / totalCells) * 100).toFixed(1) : null;
  const sb = d.manifest?.score_before, sa = d.manifest?.score_after;
  viz.innerHTML = `<div class="card-h"><h3>${esc(title)}</h3><span class="sub">${comps.length} 个分量 · ${d.audit.length} 格</span></div>
    <div class="changed">
      <div class="chg-main"><b>${fmt(d.audit.length)}</b><span>格改写<br><small>${fmt(comps.length)} 个分量${pctCells ? ' · 占 ' + pctCells + '%' : ''}</small></span></div>
      <div class="chg-keep"><b>${totalCells ? fmt(totalCells - d.audit.length) : '—'}</b><span>格逐位不变<br><small>${totalCells ? `${fmt(stateDim + actionDim)} 维 × ${fmt(n)} 行` : '其余列未动'}</small></span></div>
      <div class="chg-verdict">${sb != null ? `复检 ${fmt(sb, 2)} → <b>${fmt(sa, 2)}</b><small>质量分</small>` : ''}</div>
    </div>
    <div class="two sect" id="vzCells"></div>
    ${rotArms.length ? '<div class="sect"><div class="mini-h">修复后 Rotation-6D 正交误差（对数坐标）：解出的分量严格满足单位正交约束</div><div id="vzRot"></div></div>' : ''}`;
  const host = $('#vzCells', viz);
  comps.slice(0, 6).forEach((f) => {
    const k = +/\[(\d+)\]/.exec(f)[1];
    const fld = f.split('[')[0] === 'actions' ? 'actions' : 'state';
    const div = document.createElement('div');
    div.innerHTML = `<div class="mini-h">${esc(cellLabel(f))}</div><div></div>`;
    host.appendChild(div);
    const B = col(raw[fld], k), A = after ? col(after[fld], k) : B;
    const rr = rowsBy[f] || [];
    lineChart(div.lastElementChild, {
      n, height: 150,
      series: [
        { name: '修复前', values: B, color: 'var(--ink-3)', width: 3, opacity: 0.8, xmarks: rr.filter((i) => B[i] == null) },
        { name: '修复后', values: A, color: 'var(--fix)', markers: rr, width: 1.5 },
      ],
      bands: rr.map((r) => ({ from: r, to: r, color: 'var(--fix)', label: `第 ${r} 行` })),
    });
  });
  if (rotArms.length) {
    const series = [];
    rotArms.forEach((a, i) => {
      series.push({ name: `${ARM[a].cn} 修复前`, values: rotErr(raw.state, ARM[a].rot), color: 'var(--ink-3)', width: 3 });
      series.push({ name: `${ARM[a].cn} 修复后`, values: rotErr(after?.state, ARM[a].rot), color: SER[i], width: 1.5, markers: [...new Set(d.audit.map((x) => +x.row))] });
    });
    lineChart($('#vzRot', viz), { n, height: 160, log: true, series, thresholds: [{ y: 1e-4, label: '容差 1e-4' }] });
  }
}

function vizTask(viz, d) {
  const raw = d.raw, after = d.after;
  const tasks = S.info.dataset.tasks || [];
  const exp = d.report.expected_task_index;
  viz.innerHTML = `<div class="card-h"><h3>任务标签按元数据改正</h3><span class="sub">${d.audit.length} 行 task_index</span></div>
    <div id="vzTask"></div>
    <dl class="kv sect">${tasks.map((t) => `<dt>任务 ${t.task_index}</dt><dd>${esc(t.task)}</dd>`).join('')}<dt>元数据记录</dt><dd>本条轨迹属于任务 ${esc(exp)}</dd></dl>`;
  lineChart($('#vzTask', viz), { n: raw.n, height: 170, series: [{ name: '修复前 task_index', values: raw.task_index, color: 'var(--bad)', width: 3 }, { name: '修复后 task_index', values: after?.task_index || [], color: 'var(--fix)' }] });
}

function vizDtype(viz, d) {
  const raw = d.raw, after = d.after;
  const same = (raw.timestamp || []).every((v, i) => v === after?.timestamp?.[i]);
  const unitLeft = String(d.report.final_issue_codes || '').split(';').includes('T_TIMESTAMP_UNIT_MISMATCH');
  viz.innerHTML = `<div class="card-h"><h3>存储类型无损转换，数值不变</h3></div>
    <dl class="kv"><dt>info.json 声明</dt><dd class="mono">timestamp: ${esc(d.declared?.timestamp || '—')}</dd><dt>修复前存储</dt><dd class="mono">timestamp: ${esc(raw.dtypes?.timestamp)}</dd><dt>修复后存储</dt><dd class="mono">timestamp: ${esc(after?.dtypes?.timestamp)}</dd><dt>逐行数值</dt><dd>${same ? `${raw.n} 行全部相同` : '存在差异'}</dd></dl>
    ${unitLeft ? `<p class="note sect">时间戳单位疑似标错没有改（不按标称周期重建时间戳），所以修复后仍是 ${fmt(+d.report.train_frames || 0)} 帧可训练。</p>` : ''}
    <div id="vzTs" class="sect"></div>`;
  lineChart($('#vzTs', viz), { n: raw.n, height: 150, series: [{ name: 'timestamp', values: raw.timestamp, color: 'var(--c-t)' }] });
}

function vizMeta(viz, d) {
  const p = ((S.res[d.stage] || {}).manifest || []).find((m) => +m.episode_index === d.ep);
  viz.innerHTML = `<div class="card-h"><h3>只改元数据记录长度，Parquet 不动</h3></div>
    <dl class="kv"><dt>实际数据行</dt><dd>${d.raw.n}</dd><dt>episodes.jsonl length</dt><dd>${esc(d.audit[0]?.original_value)} → ${esc(d.audit[0]?.repaired_value)}</dd><dt>写入位置</dt><dd class="mono">governed/meta_patches.csv · governed/meta/</dd></dl>
    <p class="note sect">${esc(p?.applied || '')}</p>`;
}

function vizRefused(viz, d) {
  const raw = d.raw;
  const rej = d.manifest?.rejected || d.report.repair_rejected || '';
  const n = raw.n || 0;
  let spec = null, title = '';
  if (/时间戳|index/.test(rej)) {
    title = '缺帧不能凭空重建时间戳';
    spec = { series: [{ name: 'Δt（秒）', values: diff(raw.timestamp), color: SER[0] }], thresholds: [{ y: 1 / (S.info.dataset.fps || 10), label: '标称周期' }] };
  } else if (/稀疏补值/.test(rej)) {
    title = '空值太多，超过补值上限';
    const bad = (raw.state || []).map((r) => (r ? r.filter((v) => v == null).length : 20));
    spec = { series: [{ name: '每行 NaN 分量数', values: bad, color: 'var(--bad)' }] };
  } else if (/越界/.test(rej)) {
    title = '连续多帧越界，不是孤立突跳';
    spec = { series: [0, 1, 2].map((k) => ({ name: `左臂 ${'xyz'[k]}`, values: col(raw.state, k), color: SER[k] })).concat([0, 1, 2].map((k) => ({ name: `右臂 ${'xyz'[k]}`, values: col(raw.state, 10 + k), color: SER[k], dash: true }))) };
  }
  if (!spec || !n) {
    const res = S.res[d.stage] || {};
    const e = (res.episodes || []).find((x) => x.id === d.ep) || {};
    vizKeep(viz, d, e, res);
    viz.insertAdjacentHTML('beforeend', `<details class="sect"><summary class="muted">未修原因（算子原文）</summary><p class="note">${esc(rej)}</p></details>`);
    return;
  }
  const bands = (d.intervals || []).map((it) => ({ from: +it.start_row, to: +it.end_row, color: `var(${CAT_VAR[catOf(it.issue_code)]})`, label: it.issue_code }));
  viz.innerHTML = `<div class="card-h"><h3>${esc(title)}</h3><span class="sub">保持原样 · ${esc(d.report.train_policy)}</span></div><div id="vzRef"></div><details class="sect"><summary class="muted">未修原因（算子原文）</summary><p class="note">${esc(rej)}</p></details>`;
  lineChart($('#vzRef', viz), { n, height: 200, bands, ...spec });
}

// ------------------------------------------------------------ 单条数据：检测页 / 修复页
function cellLabel(f) {
  const k = +/\[(\d+)\]/.exec(f)[1];
  const arm = k < 10 ? '左臂' : '右臂', j = k % 10;
  const part = j < 3 ? `末端位置 ${'xyz'[j]}` : j < 9 ? `Rotation-6D 第 ${j - 2} 分量` : '夹爪';
  return `${f.split('[')[0]}[${k}] ${arm}${part}`;
}

function storyStrip(d, n, keep) {
  const mask = d.mask || [];
  let cells = '';
  for (let i = 0; i < n; i++) {
    const m = mask[i];
    let cls = 'x', label = '不进入训练';
    if (m) {
      if (m.keep) { cls = m.est ? 'r' : 'k'; label = m.est ? '修复后进入训练' : '进入训练'; }
      else if (m.valid) { cls = 'v'; label = '可用但不足 30 帧'; }
    } else {
      cls = i < keep ? 'k' : 'x';
    }
    cells += `<i class="${cls}" title="第 ${i} 行 · ${label}"></i>`;
  }
  return `<div class="story-strip" aria-hidden="true">${cells}</div>`;
}
const STRIP_LEGEND = '<div class="story-legend"><span><i class="k"></i>进入训练</span><span><i class="r"></i>修复后进入训练</span><span><i class="v"></i>可用但不足 30 帧</span><span><i class="x"></i>不进入训练</span></div>';

function importCellChart(host, d, audit) {
  const comps = [...new Set(audit.map((a) => a.field))].filter((f) => /^state\[\d+\]$|^actions\[\d+\]$/.test(f)).slice(0, 4);
  if (!comps.length) { host.innerHTML = ''; return; }
  const rowsBy = {};
  for (const a of audit) (rowsBy[a.field] = rowsBy[a.field] || []).push(+a.row);
  host.innerHTML = `<div class="cell-charts">${comps.map((f) => `<div><div class="mini-h">${esc(cellLabel(f))}</div><div class="cc" data-f="${esc(f)}"></div></div>`).join('')}</div>`;
  for (const f of comps) {
    const k = +/\[(\d+)\]/.exec(f)[1];
    const fld = f.split('[')[0] === 'actions' ? 'actions' : 'state';
    const B = col(d.raw[fld], k), A = d.after ? col(d.after[fld], k) : B;
    const rr = [...new Set(rowsBy[f] || [])];
    const miss = rr.filter((i) => B[i] == null);
    lineChart($(`.cc[data-f="${CSS.escape(f)}"]`, host), {
      n: d.raw.n, height: 150,
      series: [
        { name: '修复前', values: B, color: 'var(--ink-3)', width: 3, opacity: 0.8, xmarks: miss },
        { name: '修复后', values: A, color: 'var(--fix)', markers: rr, width: 1.6 },
      ],
      bands: rr.map((r) => ({ from: r, to: r, color: 'var(--fix)', label: `第 ${r} 行` })),
    });
  }
}

const frameUrl = (stage, ep, row, cam, src) => `/api/frame?stage=${stage}&ep=${ep}&row=${row}&cam=${cam}&src=${src}`;
const statusBadge = (s) => `<span class="badge b-${STATUS_CLS[s] || 'bad'}"><i></i>${esc(s || '')}</span>`;

// 三路相机 + 游标 + 问题区间：检测页、修复页共用
function camsHtml(M, n, ints, withAfter = false) {
  const camFig = (c) => `<figure class="story-cam ${M.shift?.cam === c || M.cams.includes(c) ? 'affected' : ''}" data-cam="${c}"><img alt=""><figcaption>${CAM_CN[c]}${M.shift?.cam === c || M.cams.includes(c) ? ' · 问题在这一路' : ''}<em class="frame-no">—</em></figcaption></figure>`;
  const byCode = {};
  for (const it of ints) (byCode[it.issue_code] = byCode[it.issue_code] || []).push(it);
  const codeRows = Object.entries(byCode).sort((a, b) => (M.codeFrames[b[0]] || 0) - (M.codeFrames[a[0]] || 0)).slice(0, 6);
  const intervals = codeRows.length ? `<div class="story-intervals">${codeRows.map(([c, its]) => `<div class="si"><span title="${esc(c)}">${esc(codeCn(c))}</span><div class="si-bar">${its.map((it) => `<i style="left:${(+it.start_row / n) * 100}%;width:${Math.max(((+it.end_row - +it.start_row + 1) / n) * 100, 0.6)}%;background:var(${CAT_VAR[catOf(c)]})"></i>`).join('')}<b class="ph"></b></div><b>${its.length === 1 ? `第 ${its[0].start_row}–${its[0].end_row} 行` : `${fmt(M.codeFrames[c] || 0)} 帧 · ${its.length} 段`}</b></div>`).join('')}</div>` : '';
  return `<div class="story-cams">${CAMS.map(camFig).join('')}</div>
    <div class="story-scrub"><button class="ghost sm" data-play>▶ 播放</button><input type="range" data-row min="0" max="${n - 1}" value="${M.row}"><span class="rowlab" data-rowlab></span></div>
    ${intervals}`;
}

function mountCams(box, res, e, d, M, extra) {
  const raw = d.raw, n = raw.n;
  const range = $('[data-row]', box), play = $('[data-play]', box);
  const setRow = (i) => {
    i = Math.max(0, Math.min(n - 1, i));
    range.value = i;
    $$('.story-cam', box).forEach((fig) => {
      const cam = fig.dataset.cam, p = raw.paths?.[cam]?.[i];
      const img = $('img', fig);
      if (!p) { img.removeAttribute('src'); img.style.visibility = 'hidden'; }
      else { img.style.visibility = ''; img.src = frameUrl(res.stage, e.id, i, cam, 'raw'); }
      $('.frame-no', fig).textContent = p ? '第 ' + pathNum(p) + ' 帧' : '缺图';
    });
    $$('.si-bar .ph', box).forEach((ph) => { ph.style.left = (i / n) * 100 + '%'; });
    $('[data-rowlab]', box).textContent = `第 ${i} 行`;
    if (extra) extra(i);
  };
  const stop = () => { if (S.impTimer) clearInterval(S.impTimer); S.impTimer = null; play.textContent = '▶ 播放'; };
  play.addEventListener('click', () => {
    if (S.impTimer) return stop();
    let i = +range.value;
    if (i >= n - 1) i = 0;
    play.textContent = '❚❚ 暂停';
    S.impTimer = setInterval(() => { i += 1; if (i >= n) return stop(); setRow(i); }, 1000 / (S.info.dataset.fps || 10));
  });
  range.addEventListener('input', (ev) => { stop(); setRow(+ev.target.value); });
  setRow(M.row);
}

async function loadSingle(res, box, title) {
  if (S.impTimer) { clearInterval(S.impTimer); S.impTimer = null; }
  const e = res.episodes[0];
  box.innerHTML = `<div class="story"><div class="panel card story-loading"><span class="spin"></span> ${title}</div></div>`;
  const d = await api(`/api/episode?stage=${res.stage}&ep=${e.id}`);
  if (!d.ok || !d.raw?.ok || !d.raw.n) {
    const gone = /文件不存在/.test(d.error || d.raw?.error || '');
    box.innerHTML = `<div class="story"><div class="panel empty"><h3>${epName(e.id)} 读不出画面</h3><p>${(e.codes || []).includes('C_FILE_UNREADABLE') ? `文件损坏（C_FILE_UNREADABLE），整条不进入训练。` : gone ? '原始文件已不在原来的位置。' : esc(d.error || d.raw?.error || '没有数据行')}</p></div></div>`;
    return null;
  }
  return { e, d };
}

async function renderDetectSingle(res, box) {
  const got = await loadSingle(res, box, '读取三路画面与逐帧定位');
  if (!got) return;
  const { e, d } = got;
  const n = d.raw.n;
  const M = storyModel({ e, d, imp: { repair: false }, dict: res.code_dict || {} });
  const evHtml = M.evidence.map(([k, v, pass]) => `<div class="story-ev ${pass === true ? 'pass' : pass === false ? 'fail' : ''}"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
  box.innerHTML = `<div class="story">
    <header class="story-head panel">
      <h2>${epName(e.id)}<span>${fmt(n)} 帧</span></h2>
      <div class="story-head-right">${statusBadge(e.status)}<span class="badge">${fmt(res.timing?.total_seconds_incl_all_csv_parquet_outputs, 2)} 秒</span></div>
    </header>
    <section class="story-sec panel">
      <div class="story-content">
        <p class="story-lead">${esc(M.problem)}</p>
        ${camsHtml(M, n, d.intervals || [])}
        <div class="story-evs">${evHtml}</div>
        <div class="story-actions"><button class="ghost sm" data-full>信号曲线与问题码</button></div>
      </div>
    </section>
    ${nextCta('下一步：修复 →', 'govern')}
  </div>`;
  mountCams(box, res, e, d, M);
  $('[data-full]', box).addEventListener('click', () => openEpisode(res.stage, e.id, +$('[data-row]', box).value));
  bindNext(box);
}

const VERDICT_CN = { fixed: '修了', partial: '修了一部分', refused: '没修', ok: '无需修复' };

async function renderGovernSingle(res, box) {
  const got = await loadSingle(res, box, '读取修复前后数据');
  if (!got) return;
  const { e, d } = got;
  const raw = d.raw, after = d.after, n = raw.n;
  const audit = d.audit || [];
  const M = storyModel({ e, d, imp: { repair: true }, dict: res.code_dict || {} });
  const { type, shift, off, keep, verdictCls } = M;
  const before = res.detect_ep_train?.[String(e.id)];
  const evHtml = M.evidence.map(([k, v, pass]) => `<div class="story-ev ${pass === true ? 'pass' : pass === false ? 'fail' : ''}"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
  const same = type === 'shift-fixed' ? `<div class="sameframe">
      <figure class="sameframe-item"><img data-side="raw" alt=""><figcaption>修复前 · 第 <em class="frame-no">—</em> 帧</figcaption></figure>
      <div class="sameframe-arrow">移回 ${Math.abs(off)} 帧<br><span>同一行 →</span></div>
      <figure class="sameframe-item"><img data-side="after" alt=""><figcaption>修复后 · 第 <em class="frame-no">—</em> 帧</figcaption></figure>
    </div>` : type === 'shift-refused' ? `<div class="sameframe one">
      <figure class="sameframe-item"><img data-side="raw" alt=""><figcaption>保持原样 · 第 <em class="frame-no">—</em> 帧</figcaption></figure>
      <div class="refuse-body"><div class="refuse-stamp">没修</div><p>内容证据不足，不移动画面。</p></div>
    </div>` : '';
  box.innerHTML = `<div class="story">
    <header class="story-head panel">
      <h2>${epName(e.id)}<span>${fmt(n)} 帧</span></h2>
      <div class="story-head-right"><span class="badge v-${verdictCls}"><i></i>${VERDICT_CN[verdictCls] || ''}</span>${audit.length ? `<span class="badge">改写 ${fmt(audit.filter((a) => +a.row >= 0).length)} 格</span>` : ''}</div>
    </header>
    <section class="story-sec panel">
      <div class="story-content">
        <p class="story-sub">${esc(M.problem)}</p>
        <p class="story-verdict ${verdictCls}">${esc(M.action)}</p>
        ${same}
        ${type === 'cells' ? `<div class="cell-proof"><div id="impCells"></div><div class="cell-facts"><b>${fmt(audit.length)}</b><span>格改写</span></div></div>` : ''}
        <div class="story-evs">${evHtml}</div>
        ${same ? `<div class="story-scrub"><button class="ghost sm" data-play>▶ 播放</button><input type="range" data-row min="0" max="${n - 1}" value="${M.row}"><span class="rowlab" data-rowlab></span></div>` : ''}
      </div>
    </section>
    <section class="story-sec panel">
      <div class="story-content">
        <div class="story-keep">
          <div class="keep-big"><b>${fmt(keep)}</b><span>/ ${fmt(n)} 帧可训练</span></div>
          ${before != null ? `<div class="keep-before">不修复时 <b>${fmt(before)}</b> 帧${keep > before ? `<span class="up">+${fmt(keep - before)}</span>` : ''}</div>` : ''}
          <p>${esc(M.keepText)}</p>
        </div>
        ${storyStrip(d, n, keep)}
        ${STRIP_LEGEND}
      </div>
    </section>
    ${nextCta('下一步：对比 →', 'compare')}
  </div>`;
  if (type === 'cells') importCellChart($('#impCells', box), d, audit);
  if (same) {
    const range = $('[data-row]', box), play = $('[data-play]', box);
    const setRow = (i) => {
      i = Math.max(0, Math.min(n - 1, i));
      range.value = i;
      const rimg = $('.sameframe-item img[data-side=raw]', box), aimg = $('.sameframe-item img[data-side=after]', box);
      const pr = raw.paths?.[shift.cam]?.[i], pa = after?.paths?.[shift.cam]?.[i];
      if (pr) { rimg.style.visibility = ''; rimg.src = frameUrl(res.stage, e.id, i, shift.cam, 'raw'); } else { rimg.removeAttribute('src'); rimg.style.visibility = 'hidden'; }
      rimg.parentElement.querySelector('.frame-no').textContent = pr ? pathNum(pr) : '缺图';
      if (aimg) {
        if (pa) { aimg.style.visibility = ''; aimg.src = frameUrl(res.stage, e.id, i, shift.cam, 'after'); } else { aimg.removeAttribute('src'); aimg.style.visibility = 'hidden'; }
        aimg.parentElement.querySelector('.frame-no').textContent = pa ? pathNum(pa) : '缺图';
      }
      $('[data-rowlab]', box).textContent = `第 ${i} 行 · ${CAM_CN[shift.cam]}`;
    };
    const stop = () => { if (S.impTimer) clearInterval(S.impTimer); S.impTimer = null; play.textContent = '▶ 播放'; };
    play.addEventListener('click', () => {
      if (S.impTimer) return stop();
      let i = +range.value;
      if (i >= n - 1) i = 0;
      play.textContent = '❚❚ 暂停';
      S.impTimer = setInterval(() => { i += 1; if (i >= n) return stop(); setRow(i); }, 1000 / (S.info.dataset.fps || 10));
    });
    range.addEventListener('input', (ev) => { stop(); setRow(+ev.target.value); });
    setRow(M.row);
  }
  bindNext(box);
}

// ------------------------------------------------------------ 第 1 步：导入数据
const isMac = () => /^macOS/.test(S.info?.host?.platform || '');
const STATUS_DOT = { 需复核: 'bad', 通过: 'ok', 价值提示: 'hint' };

function renderExample() {
  const ex = S.info.example || {};
  const eps = ex.episodes || [];
  const ds = S.info.dataset || {};
  $('#exCard').hidden = !eps.length;
  if (!eps.length) return;
  $('#exTitle').textContent = ex.label || '示例数据';
  $('#exFigs').textContent = [`${fmt(eps.length)} 条`, `${fmt(ex.rows)} 帧`, ds.cameras?.length ? `${ds.cameras.length} 路相机` : '', ds.fps ? `${ds.fps} fps` : '', fmtBytes(ex.bytes)].filter(Boolean).join(' · ');
  $('#exAll').innerHTML = `<b>全部 ${fmt(eps.length)} 条</b><span>批量检测与修复</span>`;
  const anyStatus = eps.some((e) => e.status);
  $('#exLegend').innerHTML = anyStatus ? '<span><i class="d-bad"></i>需复核</span><span><i class="d-hint"></i>价值提示</span><span><i class="d-ok"></i>通过</span><span><i class="d-fix"></i>有修复</span>' : '';
  const sel = S.cur && S.cur.example && S.cur.n === 1 ? S.cur.ids?.[0] : null;
  const all = S.cur && S.cur.example && S.cur.n > 1;
  $('#exAll').classList.toggle('on', !!all);
  $('#exGrid').innerHTML = eps.map((e) => `<button class="epc ${e.status ? 's-' + STATUS_DOT[e.status] : ''} ${e.fixed ? 'fixed' : ''} ${sel === e.id ? 'on' : ''}" data-path="${esc(e.path)}" title="${epName(e.id)} · ${fmt(e.rows)} 帧${e.status ? ' · ' + e.status : ''}${e.fixed ? ' · 有修复' : ''}">${String(e.id).padStart(2, '0')}</button>`).join('');
}

async function refreshExample() {
  try {
    const info = await api('/api/info');
    S.info.example = info.example;
    renderExample();
  } catch (e) { /* ignore */ }
}

function msg(html, cls = '') { $('#impMsg').innerHTML = html ? `<div class="sel-card ${cls}">${html}</div>` : ''; }

async function inspectAndSelect(path, extra = {}) {
  if (!path || !path.trim()) return toast('请先选择或输入路径');
  msg('<span><span class="spin"></span> 读取中</span>');
  const r = await api(`/api/import/inspect?path=${encodeURIComponent(path.trim())}`);
  if (!r.ok) return msg(`<b>无法读取</b><span class="warn">${esc(r.error)}</span>`, 'err');
  if (r.kind === 'multi') {
    msg(`<span class="warn">这里有 ${r.choices.length} 个数据集，选一个：</span>
      ${r.choices.map((c, i) => `<button class="choice" data-i="${i}"><b>${esc(c.name)}</b><span>${fmt(c.episodes)} 条 · ${fmtBytes(c.bytes)}</span></button>`).join('')}`);
    $$('#impMsg .choice').forEach((b) => b.addEventListener('click', () => inspectAndSelect(r.choices[+b.dataset.i].path, extra)));
    return;
  }
  msg('');
  selectData(r, extra);
}

function selectData(r, extra = {}) {
  const exPath = S.info.example?.path || '';
  const example = !!exPath && (r.path === exPath || r.path.startsWith(exPath + '/'));
  const cur = {
    path: r.path, key: r.path, kind: r.kind, name: r.name, n: r.episodes || (r.kind === 'file' ? 1 : 0), ids: r.ids || [],
    rows: r.rows ?? null, bytes: r.bytes, example, source: extra.source || r.picked || r.path,
  };
  if (!S.cur || S.cur.key !== cur.key) S.runs = { detect: null, govern: null };
  S.cur = cur;
  S.cmpFor = null;
  renderSel();
  renderExample();
  markDone();
  updateCurChip();
}

function renderSel() {
  const c = S.cur;
  $('#selBar').hidden = !c;
  if (!c) return;
  const figs = [c.rows ? `${fmt(c.rows)} 帧` : '', c.bytes ? fmtBytes(c.bytes) : ''].filter(Boolean).join(' · ');
  $('#selInfo').innerHTML = `<span class="kicker">已选</span><b>${esc(dataLabel())}</b><span class="muted">${esc(figs)}</span>${!c.example ? `<span class="p">${esc(c.source || c.path)}</span>` : ''}`;
  $('#selGo').textContent = S.runs.detect ? '查看检测结果 →' : '开始检测 →';
}

function startFromData() {
  if (!S.cur) return;
  setStep(STEP.detect);
  if (!S.runs.detect) S.panels.detect.start();
}

async function pickNative(kind) {
  msg(`<span><span class="spin"></span> 在系统窗口里选择${kind === 'file' ? '文件' : '文件夹'}…</span>`);
  let r;
  try { r = await api(`/api/import/pick?kind=${kind}`, { method: 'POST' }); } catch (e) { r = { ok: false, error: String(e) }; }
  if (r.canceled) return msg('');
  if (!r.ok) {
    msg('');
    (kind === 'file' ? $('#impFileInput') : $('#impDirInput')).click();
    return;
  }
  $('#impPath').value = r.path;
  inspectAndSelect(r.path);
}

async function collectDrop(dt) {
  const out = [];
  const entries = [...(dt.items || [])].map((it) => it.webkitGetAsEntry && it.webkitGetAsEntry()).filter(Boolean);
  const walk = async (entry, prefix) => {
    if (entry.isFile) {
      const f = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ file: f, rel: prefix + f.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      let batch;
      do {
        batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        for (const e of batch) await walk(e, prefix + entry.name + '/');
      } while (batch.length);
    }
  };
  if (entries.length) for (const e of entries) await walk(e, '');
  else for (const f of dt.files || []) out.push({ file: f, rel: f.name });
  return out;
}

async function uploadItems(items) {
  const keep = items.filter(({ file, rel }) => /\.(parquet|jsonl?|json)$/i.test(file.name) && !file.name.startsWith('._') && !/__MACOSX/.test(rel));
  if (!keep.some((x) => /\.parquet$/i.test(x.file.name))) return toast('没有找到 .parquet 文件');
  const token = [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, '0')).join('');
  const total = keep.reduce((s, x) => s + x.file.size, 0);
  let sent = 0, dir = '', file = '';
  for (let i = 0; i < keep.length; i++) {
    msg(`<span><span class="spin"></span> 复制 ${i + 1} / ${keep.length} · ${fmtBytes(sent)} / ${fmtBytes(total)}</span>`);
    const r = await fetch(`/api/import/upload?token=${token}&path=${encodeURIComponent(keep[i].rel)}`, { method: 'PUT', body: keep[i].file }).then((x) => x.json());
    if (!r.ok) return msg(`<b>复制失败</b><span class="warn">${esc(r.error)}</span>`, 'err');
    sent += keep[i].file.size;
    dir = r.dir;
    file = r.file;
  }
  const single = keep.filter((x) => /\.parquet$/i.test(x.file.name)).length === 1 && keep.length === 1;
  inspectAndSelect(single ? file : dir, { source: single ? keep[0].rel : keep[0].rel.split('/')[0] + '/' });
}

function sessionLabel(g) {
  const m = /episode_(\d+)/.exec(g.name || '');
  if (g.kind === 'file') return `${g.example ? '示例 ' : ''}${m ? epName(+m[1]) : g.name}`;
  return `${g.example ? '示例数据' : g.name} · ${fmt(g.episodes)} 条`;
}

async function loadHist() {
  const r = await api('/api/sessions');
  const items = (r.items || []).slice(0, 12);
  S.sessions = items;
  const host = $('#histList');
  if (!items.length) { host.innerHTML = '<p class="muted">还没有运行记录</p>'; return; }
  host.innerHTML = items.map((g, i) => `<button class="hist ${S.cur && S.cur.key === g.key && (S.runs.detect?.mode === 'history' || S.runs.govern?.mode === 'history') ? 'on' : ''}" data-i="${i}">
      <span class="h1"><b>${esc(sessionLabel(g))}</b><span class="muted">${esc(g.created.slice(5))}</span></span>
      <span class="h2"><span class="tagc ${g.detect ? 'y' : ''}">检测</span><span class="tagc ${g.govern ? 'y' : ''}">修复</span><span class="muted">${esc(verShort(g.govern?.version || g.detect?.version))}</span></span></button>`).join('');
  $$('.hist', host).forEach((b) => b.addEventListener('click', () => loadSession(items[+b.dataset.i])));
}

function loadSession(g) {
  if (Object.values(S.panels).some((p) => p.state === 'running')) return toast('还有运行在进行中');
  const m = /episode_(\d+)/.exec(g.name || '');
  S.cur = { path: g.key, key: g.key, kind: g.kind, name: g.name, n: g.kind === 'file' ? 1 : g.episodes, ids: g.kind === 'file' && m ? [+m[1]] : [],
    rows: null, bytes: null, example: g.example, source: g.key };
  S.runs = {
    detect: g.detect ? { id: g.detect.id, mode: 'history', created: g.detect.created, version: g.detect.version } : null,
    govern: g.govern ? { id: g.govern.id, mode: 'history', created: g.govern.created, version: g.govern.version } : null,
  };
  S.cmpFor = null;
  renderSel();
  renderExample();
  markDone();
  setStep(g.govern ? STEP.compare : STEP.detect);
}

function initData() {
  renderExample();
  $('#exAll').addEventListener('click', () => inspectAndSelect(S.info.example.path));
  $('#exGrid').addEventListener('click', (e) => { const b = e.target.closest('.epc'); if (b) inspectAndSelect(b.dataset.path); });
  $('#impPickFile').addEventListener('click', () => (isMac() ? pickNative('file') : $('#impFileInput').click()));
  $('#impPickDir').addEventListener('click', () => (isMac() ? pickNative('folder') : $('#impDirInput').click()));
  $('#impFileInput').addEventListener('change', (e) => uploadItems([...e.target.files].map((f) => ({ file: f, rel: f.name }))));
  $('#impDirInput').addEventListener('change', (e) => uploadItems([...e.target.files].map((f) => ({ file: f, rel: f.webkitRelativePath || f.name }))));
  const drop = $('#impDrop');
  drop.addEventListener('click', () => $('#impDirInput').click());
  drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('#impDirInput').click(); } });
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, () => drop.classList.remove('over')));
  drop.addEventListener('drop', async (e) => { e.preventDefault(); uploadItems(await collectDrop(e.dataTransfer)); });
  $('#impPath').addEventListener('keydown', (e) => { if (e.key === 'Enter') inspectAndSelect($('#impPath').value); });
  $('#selGo').addEventListener('click', startFromData);
  loadHist();
}

// ------------------------------------------------------------ boot
(async function boot() {
  try {
    S.info = await api('/api/info');
  } catch (e) {
    document.body.innerHTML = '<p style="padding:40px">无法连接本地演示服务。请用 start_demo.command 启动，或在 demo_system 目录运行 python server.py。</p>';
    return;
  }
  for (const st of ['detect', 'govern']) S.panels[st] = new RunPanel(st, $(`.run-shell[data-stage=${st}]`));
  initData();
  setStep(STEP.data);
})();
