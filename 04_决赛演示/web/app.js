'use strict';
/* RefSync-QA 决赛演示页面。所有数字来自本地服务读取的算子真实输出文件，页面不写死任何结果。 */

// ------------------------------------------------------------ utils
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (n, d = 0) => (n == null || Number.isNaN(n) ? '—' : Number(n).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }));
const pct = (x, d = 1) => (x == null ? '—' : (x * 100).toFixed(d) + '%');
const fmtBytes = (b) => (b == null ? '—' : b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(1) + ' MB' : b >= 1e3 ? (b / 1e3).toFixed(1) + ' KB' : b + ' B');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const cssv = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const epName = (e) => 'ep' + String(e).padStart(2, '0');
async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}

const CATS = ['时序', '同步', '内容结构', '数据价值'];
const CAT_VAR = { 时序: '--c-t', 同步: '--c-s', 内容结构: '--c-c', 数据价值: '--c-v' };
const CAT_BIT = { 时序: 1, 同步: 2, 内容结构: 4, 数据价值: 8 };
const catOf = (code) => ({ T: '时序', S: '同步', C: '内容结构', J: '内容结构', V: '数据价值' }[code[0]] || '内容结构');
const STATUS_CLS = { 通过: 'ok', 价值提示: 'hint', 需复核: 'bad' };
const POLICY_VAR = {
  整段可用: '--ok', 修复后整段可用: '--fix', 切段使用: '--c-t', '部分修复+切段使用': '--c-c',
  隔离回采: '--bad', 文件不可用: '--ink-3', 处理失败: '--ink-3', 重复剔除: '--warn', '部分重复剔除+切段使用': '--c-v',
};
const DIM_CN = { structure: '结构', temporal: '时序', sync: '同步', content: '内容', value: '价值' };
const DIM_W = { structure: '25%', temporal: '20%', sync: '20%', content: '20%', value: '15%' };

// 演示四步：1 检测 → 2 修复 → 3 对比 → 4 导入测试
const STEP = { detect: 1, govern: 2, compare: 3, import: 4 };
const S = {
  step: STEP.detect,
  mode: 'live',
  info: null,
  res: {},
  panels: {},
  cmpEp: null,
};

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
document.addEventListener('click', (e) => { if (e.target.closest('[data-close]')) closeOverlays(); });

// ------------------------------------------------------------ theme / steps / mode
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('rsqa-theme', t); } catch (e) { /* ignore */ }
  // redraw canvases that read CSS colors
  $$('canvas[data-pano]').forEach((c) => c._redraw && c._redraw());
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
  if (n === STEP.compare) renderCompare();
  window.scrollTo(0, 0);
}
$$('#steps button').forEach((b) => b.addEventListener('click', () => setStep(+b.dataset.step)));
function markDone() {
  $$('#steps button').forEach((b) => {
    const st = +b.dataset.step;
    b.classList.toggle('done', (st === STEP.import && !!S.impDone) || (st === STEP.detect && !!S.res.detect) || (st === STEP.govern && !!S.res.govern));
  });
}

$$('#modeSeg button').forEach((b) => b.addEventListener('click', () => {
  S.mode = b.dataset.mode;
  $$('#modeSeg button').forEach((x) => { x.classList.toggle('on', x === b); x.setAttribute('aria-checked', x === b); });
  toast(S.mode === 'replay' ? '回放模式：按原始时间节奏重放上一次实测记录' : '实时模式：现场调用算子真实运行');
}));

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
    if (p && p.state !== 'running') { e.preventDefault(); p.start(); }
    if (S.step === STEP.import && !$('#impRun').disabled) { e.preventDefault(); impRun(); }
  }
});

function hwLabel(t) {
  if (!t || !t.hardware) return '';
  const p = t.hardware.platform || '';
  const os = p.startsWith('macOS') ? 'macOS' : p.startsWith('Linux') ? 'Linux' : p.startsWith('Windows') ? 'Windows' : p.split('-')[0];
  const arch = /arm64|aarch64/.test(p) ? 'ARM' : /x86_64|AMD64/.test(p) ? 'x86' : '';
  return `${os} ${arch} · ${t.hardware.cpu_count} 核 · ${t.workers} 进程`;
}

// ------------------------------------------------------------ run panel
const STAGE_CFG = {
  detect: {
    name: '一、检测',
    desc: '逐轨迹、逐帧定位时序、同步、内容结构、数据价值四类问题，按证据强弱分级，给出问题区间。只读运行，不生成修复副本。',
    btn: '开始检测', rerun: '重新检测',
    phases: [['episodes', '逐轨迹特征提取与三层证据检测'], ['dedup', '跨轨迹重复关系'], ['outputs', '掩膜与报告写出'], ['summary', '汇总报告']],
  },
  govern: {
    name: '二、修复',
    desc: '证据充分才修：改写只写副本、原始数据只读，副本写后逐单元格回读核验，修复后重新检测；证据不足则拒修隔离。最后切出连续可训练片段。',
    btn: '开始修复', rerun: '重新修复',
    phases: [['episodes', '检测 → 修复 → 回读核验 → 复检'], ['dedup', '跨轨迹去重'], ['outputs', '修复决策、切段与审计写出'], ['summary', '汇总报告']],
  },
};

// 实时面板：左边计时与阶段；右边是逐帧全景（每完成一条轨迹，铺开一行）和逐条发现流
const LIVE_LEGEND = {
  detect: () => CATS.map((c) => [`var(${CAT_VAR[c]})`, c]).concat([['var(--cell)', '未命中'], ['var(--cell-empty)', '待检测']]),
  govern: () => [['var(--fix)', '本行被修复改写']].concat(CATS.map((c) => [`var(${CAT_VAR[c]})`, `复检仍有：${c}`]), [['var(--cell)', '干净'], ['var(--cell-empty)', '待处理']]),
};
const bitsOf = (s) => { const o = {}; for (const ch of s || '') { const b = parseInt(ch, 16) || 0; for (const c of CATS) if (b & CAT_BIT[c]) o[c] = (o[c] || 0) + 1; } return o; };
const runsToSet = (runs) => { const s = new Set(); for (const [a, b] of runs || []) for (let i = +a; i <= +b; i++) s.add(i); return s; };

class RunPanel {
  constructor(stage, host, opts = {}) {
    this.stage = stage;
    this.kind = opts.kind || stage;
    this.opts = opts;
    this.cfg = opts.cfg || STAGE_CFG[stage];
    host.appendChild($('#runTpl').content.cloneNode(true));
    this.el = host;
    this.state = 'idle';
    $('.rt-name', host).textContent = this.cfg.name;
    $('.rt-desc', host).textContent = this.cfg.desc;
    this.btn = $('.run-btn', host);
    this.btn.textContent = this.cfg.btn;
    this.btn.addEventListener('click', () => this.start());
    this.canvas = $('canvas[data-live]', host);
    this.bindCanvas();
    this.setKind(this.kind);
    this.raf = null;
    if (opts.import) {
      host.classList.add('imp-panel');
      this.buildRows([]);
      return;
    }
    const n = Math.max(S.info.dataset.episodes_files || 0, S.info.dataset.meta_episodes || 0) || 89;
    this.buildRows(Array.from({ length: n }, (_, i) => i));
    const brief = S.info.runs[stage];
    if (brief) {
      const a = document.createElement('button');
      a.className = 'ghost sm';
      a.textContent = `查看上次结果（${brief.created}）`;
      a.onclick = () => this.showSaved();
      $('.run-actions', host).appendChild(a);
      this.savedBtn = a;
    }
  }

  setKind(kind) {
    this.kind = kind;
    this.setPhases((STAGE_CFG[kind] || this.cfg).phases);
    $('.bh-title', this.el).textContent = kind === 'govern' ? '逐帧修复全景' : '逐帧检测全景';
    $('.bh-sub', this.el).textContent = kind === 'govern'
      ? '每行一条轨迹、每格一帧：紫色是被修复改写的行，彩色是复检后仍有问题的行'
      : '每行一条轨迹、每格一帧：一条轨迹查完，它的那一行就铺开，颜色是问题类别';
    $('.live-legend', this.el).innerHTML = LIVE_LEGEND[kind === 'govern' ? 'govern' : 'detect']()
      .map(([c, l]) => `<span><i style="background:${c};border:1px solid var(--line-2)"></i>${l}</span>`).join('');
    $('.feed-h', this.el).textContent = kind === 'govern' ? '逐条修复' : '逐条发现';
  }

  setPhases(phases) {
    $('.phases', this.el).innerHTML = phases.map(([k, l]) => `<li data-k="${k}"><span>${l}</span><span></span></li>`).join('');
  }

  buildRows(ids) {
    this.ids = [...ids];
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
      showTip(`<b>${epName(h.ep)}</b> · ${esc(this.kind === 'govern' ? R.final_status : R.status)} · ${fmt(R.rows)} 行`
        + (this.kind === 'govern' && R.fix.size ? ` · 改写 ${fmt(R.fix.size)} 行` : '')
        + '<br>' + (codes.length ? codes.map((x) => `<div class="tt-row"><i style="background:var(${CAT_VAR[catOf(x)]})"></i>${esc(x)}</div>`).join('') : '未命中问题码'), ev.clientX, ev.clientY);
    });
    c.addEventListener('mouseleave', hideTip);
    c.addEventListener('click', (ev) => { const h = hit(ev); if (h && h.row && this.state === 'done') openEpisode(this.stage, h.ep); });
    new ResizeObserver(() => this.drawLive()).observe(c.parentElement);
  }

  drawLive() {
    const c = this.canvas;
    if (!c || !c.parentElement) return;
    const n = this.ids.length;
    const W = c.parentElement.clientWidth || 700;
    const rowH = n <= 3 ? 30 : n <= 12 ? 16 : n <= 40 ? 12 : 5, gap = n <= 12 ? 4 : 1, gutter = 44;
    const H = Math.max(n, 1) * (rowH + gap) + 4;
    const dpr = devicePixelRatio || 1;
    c.width = W * dpr; c.height = H * dpr; c.style.height = H + 'px';
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const done = Object.values(this.rows);
    const maxRows = Math.max(240, ...done.map((r) => r.rows || 0));
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
    $('.lp-n', this.el).textContent = `${done.length} / ${n} 条已${this.kind === 'govern' ? '处理' : '检测'}`;
  }

  reset() {
    this.events = [];
    this.k = 0;
    this.anchorT = 0;
    this.anchorAt = performance.now();
    this.lastT = 0;
    this.images = 0;
    this.rows = {};
    this.lastEp = null;
    this.workers = null;
    $('.console', this.el).innerHTML = '';
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
    this.setLiveCaption(null, true);
    this.renderLive();
    this.drawLive();
  }

  setLiveCaption(ev, reset = false) {
    const text = $('.lc-text', this.el);
    const meta = $('.lc-meta', this.el);
    if (!text) return;
    if (reset || !ev) {
      text.textContent = '运行开始，逐条处理中…';
      meta.textContent = this.kind === 'govern'
        ? '每条轨迹修了什么、为什么拒修，会在这里实时出现'
        : '每条轨迹查出了什么、在第几行，会在这里实时出现';
      return;
    }
    const c = captionFor(ev, this.kind);
    text.textContent = c.text;
    meta.textContent = c.meta;
    const box = $('.live-caption', this.el);
    box.classList.remove('pulse');
    void box.offsetWidth;
    box.classList.add('pulse');
  }

  setFinalCaption() {
    const L = this.live;
    const text = $('.lc-text', this.el);
    const meta = $('.lc-meta', this.el);
    if (!text) return;
    if (this.kind === 'govern') {
      text.textContent = `${this.n || L.k} 条跑完：${L.fixed} 条生成修复副本或元数据补丁，改写 ${fmt(L.cells)} 格；${L.rejected} 项证据不足、拒修`;
      meta.textContent = `复检后问题行 ${fmt(L.flagged)} → ${fmt(L.fflagged)}；需复核轨迹 ${L.st['需复核']} → ${L.fst['需复核']}`;
    } else {
      text.textContent = `${this.n || L.k} 条跑完：${L.st['需复核'] || 0} 条需复核，${fmt(L.flagged)} 行被逐帧定位`;
      meta.textContent = '蓝色时序、橙色同步、绿色内容结构、黄色数据价值；结果页可点任一行看画面';
    }
    $('.live-caption', this.el).classList.remove('pulse');
  }

  renderLive() {
    const L = this.live;
    const n = this.n || 89;
    const cell = (k, v, sub = '', cls = '') => `<div class="ls ${cls}"><span>${k}</span><b>${v}</b>${sub ? `<small>${sub}</small>` : ''}</div>`;
    const arrow = (a, b) => `${fmt(a)}<em>→</em>${fmt(b)}`;
    const stats = this.kind === 'govern'
      ? [
        cell('修复副本', fmt(L.fixed), '含元数据补丁', 'fix'),
        cell('改写单元格', fmt(L.cells), `${fmt(L.fixRows)} 行有改动`),
        cell('证据不足 · 拒修', fmt(L.rejected), '保持原样'),
        cell('问题行：修复前 → 复检后', arrow(L.flagged, L.fflagged), '逐行重新检测', 'wide'),
        cell('需复核轨迹', arrow(L.st['需复核'], L.fst['需复核']), `通过 ${L.st['通过']} → ${L.fst['通过']}`),
      ]
      : [
        cell('需复核', `<i style="background:var(--bad)"></i>${L.st['需复核']}`),
        cell('通过', `<i style="background:var(--ok)"></i>${L.st['通过']}`),
        cell('仅价值提示', `<i style="background:var(--warn)"></i>${L.st['价值提示']}`),
        cell('逐帧定位', L.flaggedKnown ? fmt(L.flagged) : '—', '行'),
        cell('命中问题码', fmt(L.codes.size), '种'),
        cell('平均每条', L.k ? (L.secs / L.k).toFixed(2) : '—', '秒'),
      ];
    $('.live-status', this.el).innerHTML = stats.join('');
    const maxFr = Math.max(1, ...CATS.map((c) => Math.max(L.catFr[c], L.fcatFr[c])));
    $('.live-cats', this.el).innerHTML = this.kind === 'govern'
      ? '<div class="mini-h">四类问题的问题行：修复前 → 复检后</div>' + CATS.map((c) => `<div class="hb2"><span><i style="background:var(${CAT_VAR[c]})"></i>${c}</span>
          <div class="t2"><i class="b" style="width:${(L.catFr[c] / maxFr) * 100}%"></i><i class="a" style="width:${(L.fcatFr[c] / maxFr) * 100}%;background:var(${CAT_VAR[c]})"></i></div>
          <span class="v">${fmt(L.catFr[c])}<em>→</em><b>${fmt(L.fcatFr[c])}</b></span></div>`).join('')
      : '<div class="mini-h">四类检测器命中（实时累计）</div>' + CATS.map((c) => `<div class="hb"><span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:var(${CAT_VAR[c]});margin-right:6px"></i>${c}</span><div class="t"><i style="width:${(L.catEp[c] / n) * 100}%;background:var(${CAT_VAR[c]})"></i></div><span class="v">${L.catEp[c]}<small> 条</small> · ${L.catFr[c] ? fmt(L.catFr[c]) : '—'}<small> 帧</small></span></div>`).join('');
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

  log(msg, cls = '') {
    const c = $('.console', this.el);
    const d = document.createElement('div');
    d.textContent = msg;
    if (cls) d.className = cls;
    c.appendChild(d);
    while (c.children.length > 9) c.firstChild.remove();
  }

  tick() {
    if (this.state !== 'running') return;
    const t = this.anchorT + (performance.now() - this.anchorAt) / 1000;
    $('.clock-v', this.el).textContent = t.toFixed(2);
    this.raf = requestAnimationFrame(() => this.tick());
  }

  async start() {
    if (this.state === 'running') return;
    if (this.opts.import) return this.opts.onStart && this.opts.onStart();
    if (Object.values(S.panels).some((p) => p !== this && p.state === 'running')) return toast('已有运行在进行中');
    const r = await api(`/api/run?stage=${this.stage}&mode=${S.mode}`, { method: 'POST' });
    if (!r.ok) return toast(r.error || '无法启动');
    await this.follow();
  }

  async runImport(r) {
    this.stage = r.id;
    this.setKind(r.repair ? 'govern' : 'detect');
    this.buildRows(r.ids || []);
    await this.follow();
  }

  async follow() {
    this.reset();
    this.state = 'running';
    this.btn.disabled = true;
    this.btn.textContent = '运行中…';
    if (this.savedBtn) this.savedBtn.hidden = true;
    this.anchorAt = performance.now();
    this.tick();
    let from = 0, done = false, ok = false;
    while (!done) {
      let j;
      try { j = await api(`/api/job?stage=${this.stage}&from=${from}`); } catch (e) { await sleep(400); continue; }
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
      const b = document.createElement('div');
      b.className = 'banner';
      b.innerHTML = `<span>运行没有完成：${esc(this.err || '未知原因')}。上一次成功的结果仍然保留。</span><button class="ghost sm">改用回放</button>`;
      if (this.opts.import) b.querySelector('button').remove();
      else b.querySelector('button').onclick = () => { $('#modeSeg [data-mode=replay]').click(); this.start(); };
      $('.run-head', this.el).after(b);
      if (this.opts.onDone) this.opts.onDone(false);
      return;
    }
    await sleep(1200);
    await refreshInfo();
    await this.loadResult(true);
    if (this.opts.onDone) this.opts.onDone(true);
  }

  onEvent(ev) {
    this.events.push(ev);
    if (typeof ev.t === 'number') { this.anchorT = ev.t; this.anchorAt = performance.now(); this.lastT = ev.t; }
    switch (ev.type) {
      case 'replay':
        this.log(`回放 ${ev.recorded} 的实测记录`);
        break;
      case 'start':
        this.workers = ev.workers;
        $('.c-w', this.el).textContent = `${ev.workers}`;
        $('.cli', this.el).textContent = '$ ' + ev.cli;
        $('.cli', this.el).title = ev.cli;
        this.log(`${ev.operator} · ${ev.cpu} 核 CPU · ${ev.workers} 个并行进程`);
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
        if (this.kind === 'govern' && (ev.repaired || ev.meta_patch)) {
          this.log(`${epName(ev.ep)} ${ev.repaired ? '修复副本已写出并回读核验' : '元数据补丁'} · 改写 ${ev.cells} 格 · 复检 ${fmt(ev.score, 1)} → ${fmt(ev.final_score, 1)} 分`);
        } else if (ev.error) {
          this.log(`${epName(ev.ep)} 处理异常，已记录并继续批次`, 'err');
        }
        break;
      }
      case 'final': {
        const L = this.live;
        const z = { 需复核: 0, 通过: 0, 价值提示: 0 };
        const st = { ...z, ...(ev.st || {}) }, fst = { ...z, ...(ev.fst || {}) };
        const moved = ['需复核', '通过', '价值提示'].filter((k) => st[k] !== L.st[k]);
        if (moved.length) this.log(`跨轨迹去重后按最终报告校正状态：${moved.map((k) => `${k} ${L.st[k]}→${st[k]}`).join('，')}`);
        L.st = st; L.fst = fst;
        this.renderLive();
        break;
      }
      case 'log':
        if (/^\[detect\]/.test(ev.msg)) break;
        this.log(ev.msg.replace(/^\[done\] /, '完成：'));
        break;
      case 'done': {
        this.timing = ev.timing;
        $('.clock-v', this.el).textContent = (ev.timing?.total_seconds_incl_all_csv_parquet_outputs ?? ev.t).toFixed(2);
        $('.clock-sub', this.el).textContent = `算子计时（读取 + 检测${this.kind === 'govern' ? ' + 修复 + 复检' : ''} + 全部文件写出）· ${hwLabel(ev.timing)}`;
        $$('.phases li', this.el).forEach((li) => { if (li.className === 'run') li.className = 'end'; });
        this.lastEp = null;
        this.drawLive();
        this.setFinalCaption();
        break;
      }
      case 'error':
        this.err = ev.msg;
        this.log(ev.msg, 'err');
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

  async showSaved() {
    this.reset();
    await this.loadResult(false);
  }

  async showRun(id, repair, ids) {
    this.stage = id;
    this.setKind(repair ? 'govern' : 'detect');
    this.buildRows(ids || []);
    this.events = [];
    this.reset();
    await this.loadResult(false);
  }

  async loadResult(animate) {
    const res = await api(`/api/result?stage=${this.stage}`);
    if (!res.ok) return toast(res.error);
    S.res[this.stage] = res;
    res._phases = this.events?.length ? this.phaseTimes() : [];
    markDone();
    this.state = 'done';
    this.btn.textContent = this.cfg.rerun;
    if (this.savedBtn) this.savedBtn.hidden = true;
    const t = res.timing;
    $('.clock-v', this.el).textContent = (t?.total_seconds_incl_all_csv_parquet_outputs ?? 0).toFixed(2);
    if (!this.events?.length) {
      $('.pk', this.el).textContent = t?.episodes;
      $('.prog-bar i', this.el).style.width = '100%';
      $('.c-rows', this.el).textContent = fmt(t?.rows);
      $('.c-img', this.el).textContent = fmt(t?.images_decoded);
      $('.c-ips', this.el).textContent = fmt(t?.decoded_images_per_second) + ' 张/秒';
      $('.c-w', this.el).textContent = t?.workers;
      $('.clock-sub', this.el).textContent = `算子计时 · ${hwLabel(t)} · ${res.created}`;
    }
    const box = $('.result', this.el);
    box.innerHTML = '';
    if (this.opts.import) renderImportResult(res, box, this);
    else if (this.kind === 'detect') renderDetectResult(res, box, this);
    else renderGovernResult(res, box, this);
    box.hidden = false;
    $('.run-body', this.el).hidden = true;
    if (animate) box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function feedFor(ev, kind) {
  const c = captionFor(ev, kind);
  const text = c.text.replace(/^第 \d+ 条 · /, '');
  const codes = (kind === 'govern' ? ev.final_codes : ev.codes) || [];
  const cats = [...new Set((ev.codes || []).map(catOf))];
  const chips = cats.map((x) => `<i class="fi-c" style="background:var(${CAT_VAR[x]})" title="${x}"></i>`).join('');
  if (ev.error) return { tag: '异常', cls: 'err', text, meta: c.meta, chips: '' };
  if (kind === 'govern') {
    if (ev.repaired || ev.meta_patch) return { tag: '修复', cls: 'fix', text, meta: c.meta, chips };
    if (ev.rejected) return { tag: '拒修', cls: 'bad', text, meta: c.meta, chips };
    if (!codes.length) return { tag: '通过', cls: 'ok', text: '没有需要修复的问题', meta: `${fmt(ev.rows)} 行`, chips: '' };
    return { tag: '保持', cls: 'keep', text, meta: c.meta, chips };
  }
  if (!codes.length) return { tag: '通过', cls: 'ok', text: '未命中问题', meta: `${fmt(ev.rows)} 行 · ${fmt(ev.seconds, 2)} 秒`, chips: '' };
  return { tag: ev.status === '价值提示' ? '提示' : '需复核', cls: ev.status === '价值提示' ? 'hint' : 'bad', text, meta: c.meta, chips };
}

// ------------------------------------------------------------ result pieces
function kpi(label, value, sub, lead) {
  return `<div class="kpi panel ${lead ? 'lead' : ''}"><div class="k-l">${label}</div><div class="k-v">${value}</div><div class="k-s">${sub || ''}</div></div>`;
}

const PHASE_SHORT = { episodes: '逐条处理', dedup: '去重', outputs: '写出', summary: '汇总' };
function speedKpi(res) {
  const t = res.timing || {};
  const ph = (res._phases || []).filter(([k]) => PHASE_SHORT[k]).map(([k, v]) => `${PHASE_SHORT[k]} ${v}`).join(' · ');
  return kpi('总用时', `${fmt(t.total_seconds_incl_all_csv_parquet_outputs, 2)}<small>秒</small>`,
    `${fmt(t.episodes_per_second, 2)} 条/秒 · ${fmt(t.decoded_images_per_second)} 张图像/秒<br>${ph ? ph + '<br>' : ''}${hwLabel(t)}`, true);
}

function filesKpi(res, prefix) {
  const items = res.artifacts.filter((a) => !prefix || prefix.some((p) => a.path.startsWith(p)));
  const n = items.reduce((s, a) => s + (a.dir ? a.files : 1), 0);
  const b = items.reduce((s, a) => s + a.bytes, 0);
  return kpi('生成文件', `${fmt(n)}<small>个</small>`, `${fmtBytes(b)} · 报告、掩膜${prefix && prefix.includes('governed/') ? '、修复副本与审计' : '与问题区间'}`);
}

function catCard(res) {
  const d = res.detection;
  const total = res.scope.rows;
  const maxE = res.scope.episodes;
  const eRows = CATS.map((c) => `<div class="hb"><span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:var(${CAT_VAR[c]});margin-right:6px"></i>${c}</span><div class="t"><i style="width:${((d.episodes_by_category[c] || 0) / maxE) * 100}%;background:var(${CAT_VAR[c]})"></i></div><span class="v">${d.episodes_by_category[c] || 0}<small> 条</small></span></div>`).join('');
  const fRows = CATS.map((c) => {
    const v = res.cat_frames[c] || 0;
    return `<div class="hb"><span>${c}</span><div class="t"><i style="width:${(v / total) * 100}%;background:var(${CAT_VAR[c]})"></i></div><span class="v">${v ? fmt(v) : '<small>轨迹级</small>'}</span></div>`;
  }).join('');
  const tiers = d.flagged_by_strongest_evidence || {};
  const tsum = Object.values(tiers).reduce((a, b) => a + b, 0) || 1;
  const tierColors = ['var(--ink)', 'var(--ink-2)', 'var(--ink-3)'];
  const tk = Object.keys(tiers);
  return `<div class="card panel">
    <div class="card-h"><h3>四类问题覆盖</h3><button class="ghost sm" data-act="codes">全部 ${res.codes.length} 种问题码</button></div>
    <div class="two"><div><div class="mini-h">涉及轨迹数</div><div class="hbars">${eRows}</div></div>
    <div><div class="mini-h">帧级定位行数</div><div class="hbars">${fRows}</div></div></div>
    <div class="sect"><div class="mini-h">需复核轨迹按最强证据分级（${tsum} 条）</div>
      <div class="stack">${tk.map((k, i) => `<i style="width:${(tiers[k] / tsum) * 100}%;background:${tierColors[i]}"></i>`).join('')}</div>
      <div class="stack-legend">${tk.map((k, i) => `<span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${tierColors[i]};margin-right:5px"></i>${k} <b>${tiers[k]}</b></span>`).join('')}</div>
    </div></div>`;
}

function scoreCard(res) {
  const sc = res.scores;
  return `<div class="card panel"><div class="card-h"><h3>五维质量评分</h3><span class="sub">均分 ${fmt(sc.mean, 2)} · 中位 ${fmt(sc.median, 1)}</span></div>
    <div class="dims">${Object.entries(sc.dimension_means).map(([k, v]) => `<div class="dim"><span>${DIM_CN[k]} <small class="muted">${DIM_W[k]}</small></span><div class="t"><i style="width:${v}%"></i></div><span class="v">${fmt(v, 1)}</span></div>`).join('')}</div>
    <p class="note" style="margin-top:10px">加权合成 0–100 分；确定性阻断问题触发硬门禁封顶。</p></div>`;
}

function fileType(a) {
  if (a.dir) return 'DIR';
  const ext = a.path.split('.').pop().toUpperCase();
  return { PARQUET: 'PQ', JSONL: 'JSON' }[ext] || ext;
}

function filesCard(res, stage, prefixes, title, collapsed = []) {
  const items = res.artifacts.filter((a) => prefixes.some((p) => a.path.startsWith(p)));
  for (const c of collapsed) {
    const sub = res.artifacts.filter((a) => a.path.startsWith(c + '/'));
    items.push({ path: c, dir: true, files: sub.length, bytes: sub.reduce((s, a) => s + a.bytes, 0), note: c === 'report' ? '复检后的检测报告：逐轨迹、逐帧、问题区间、汇总' : '' });
  }
  const total = items.reduce((s, a) => s + a.bytes, 0);
  return `<div class="card panel"><div class="card-h"><h3>${title}</h3><span class="sub">${esc(prefixes.join(' + '))}</span></div>
    <div class="files">${items.map((a) => `<button class="file" data-file="${esc(a.path)}" data-stage="${stage}">
      <span class="ft">${fileType(a)}</span>
      <span class="fn"><b>${esc(a.path.split('/').pop())}${a.dir ? '/' : ''}</b><span>${esc(a.note || a.path)}</span></span>
      <span class="fs">${a.dir ? fmt(a.files) + ' 个文件<br>' : a.rows != null ? fmt(a.rows) + ' 行<br>' : ''}${fmtBytes(a.bytes)}</span></button>`).join('')}</div>
    <div class="files-foot"><span>合计 ${fmtBytes(total)}</span><button class="ghost sm" data-open="${stage}">打开输出目录</button></div></div>`;
}

function bindCommon(box, res, stage) {
  $$('[data-file]', box).forEach((b) => b.addEventListener('click', () => previewFile(b.dataset.stage, b.dataset.file)));
  $$('[data-open]', box).forEach((b) => b.addEventListener('click', async () => {
    const r = await api(`/api/open?stage=${b.dataset.open}&path=`, { method: 'POST' });
    toast(r.ok ? '已在访达中打开：' + r.path : r.error + (r.path ? '：' + r.path : ''));
  }));
  $$('[data-act=codes]', box).forEach((b) => b.addEventListener('click', () => showCodes(res)));
  $$('[data-act=eps]', box).forEach((b) => b.addEventListener('click', () => showEpisodes(res, stage)));
}

function renderDetectResult(res, box, panel) {
  const d = res.detection;
  const cats = Object.entries(d.episodes_by_category || {}).sort((a, b) => b[1] - a[1]);
  const topCat = cats[0] && cats[0][1] ? cats[0][0] : null;
  box.innerHTML = `<div class="finding panel">
      <span class="f-k">检测结论</span>
      <h2>${fmt(d.flagged_episodes)} / ${fmt(res.scope.episodes)} 条需要复核${topCat ? `；问题最集中在${topCat}` : ''}。</h2>
      <p>每行一条轨迹，每格一帧；颜色是问题类别。点任一行看画面与问题区间。</p>
    </div>
    <div class="kpis">
      ${speedKpi(res)}
      ${kpi('需复核轨迹', `${d.flagged_episodes}<small>/ ${res.scope.episodes}</small>`, `${pct(d.flagged_rate)} · 另有 ${d.value_hint_only_episodes} 条仅价值提示`)}
      ${kpi('帧级定位', `${fmt(d.flagged_frames)}<small>行</small>`, `占全部 ${fmt(res.scope.rows)} 行的 ${pct(d.flagged_frame_rate)}`)}
      ${kpi('问题区间', `${fmt(d.issue_intervals)}<small>段</small>`, '连续异常帧聚合，精确到起止行号与数据流')}
      ${filesKpi(res, ['report/'])}
    </div>
    <div class="res-grid">
      <div class="col">
        <div class="card panel">
          <div class="card-h"><h3>帧级质量全景：每行一条轨迹，每格一帧</h3><div class="pano-tools"><button class="ghost sm" data-act="eps">轨迹清单</button></div></div>
          <div class="legend" style="margin-bottom:10px">${CATS.map((c) => `<span><i style="background:var(${CAT_VAR[c]})"></i>${c}</span>`).join('')}<span><i style="background:var(--cell)"></i>未命中</span><span class="muted">浅色 = 整条轨迹级问题；点击任一行查看细节</span></div>
          <div class="pano-wrap"><canvas data-pano></canvas></div>
        </div>
      </div>
      <div class="col">${catCard(res)}${scoreCard(res)}${filesCard(res, res.stage, ['report/'], '生成物：检测报告')}</div>
    </div>
    ${res.stage === 'detect' ? '<div class="cta-row"><button class="cta" data-goto="2">下一步：修复</button><span class="hint">检测只读，原始数据没有被改动；修复只写副本</span></div>' : ''}`;
  drawPanorama($('canvas[data-pano]', box), res, 'before', res.stage);
  $$('[data-goto]', box).forEach((b) => b.addEventListener('click', () => setStep(+b.dataset.goto)));
  bindCommon(box, res, res.stage);
}

function renderGovernResult(res, box, panel) {
  const g = res.governance;
  const tf = g.train_frames;
  const base = tf['原本可用'] || 0;
  const detectOnly = res.detect_only_train_frames;
  const total = tf.total;
  const delta = total - base;
  const maxV = Math.max(total, detectOnly || 0, base) || 1;
  const tiers = [
    ['全盘丢弃', '只保留整条无阻断问题的轨迹', base, 'var(--ink-3)'],
    ...(detectOnly != null ? [['只检测 + 切段', '不修复，只切出连续可用片段', detectOnly, 'var(--ink-2)']] : []),
    ['检测 + 安全修复 + 切段', 'RefSync-QA 默认策略', total, 'var(--ok)'],
  ];
  const reasons = {};
  for (const m of res.manifest || []) {
    for (const r of (m.rejected || '').split('；').filter(Boolean)) {
      const k = reasonLabel(r);
      reasons[k] = (reasons[k] || 0) + 1;
    }
  }
  const rs = Object.entries(reasons).sort((a, b) => b[1] - a[1]);
  const rmax = rs[0]?.[1] || 1;
  const comp = [['原本可用', 'var(--ink-3)'], ['切段新增', 'var(--c-t)'], ['修复恢复', 'var(--fix)'], ['修复+切段', 'var(--c-c)']];
  box.innerHTML = `<section class="gov-hero panel">
      <div class="gh-copy">
        <p class="eyebrow">同一批 ${res.scope.episodes} 条轨迹 · ${fmt(res.scope.rows)} 行 · 本次运行</p>
        <h2><b>+${fmt(delta)}</b><small>帧</small><br>被安全救回。</h2>
        <p class="gh-line">不修，只能留下 <b>${fmt(base)}</b> 帧；安全修复后留下 <b>${fmt(total)}</b> 帧。多找回的 <b>${fmt(delta)}</b> 帧（占最终可训练帧的 ${total ? ((delta / total) * 100).toFixed(1) : '—'}%）每一帧都重新过了同一套检测，带阻断问题的不进训练。</p>
        ${res.compare ? `<div class="gh-ba"><span>复检 · 需复核轨迹 ${fmt(res.compare.status_before['需复核'] || 0)} → <b>${fmt(res.compare.status_after['需复核'] || 0)}</b></span><span>带问题码的帧 ${fmt(res.compare.flag_rows_before)} → <b>${fmt(res.compare.flag_rows_after)}</b></span><span>平均质量分 ${fmt(res.compare.score_mean_before, 2)} → <b>${fmt(res.compare.score_mean_after, 2)}</b></span></div>` : ''}
      </div>
      <div class="gh-chart">
        ${tiers.map(([n, s, v, c], i) => `<div class="gh-row ${i === tiers.length - 1 ? 'best' : ''}">
          <div class="lab"><b>${n}</b><span>${s}</span></div>
          <div class="t"><i data-w="${(v / maxV) * 100}" style="width:0;background:${c}"></i></div>
          <div class="v">${fmt(v)}<small>${i === 0 ? '基线' : '<span class="up">+' + fmt(v - base) + '</span>'}</small></div>
        </div>`).join('')}
        <div class="gh-legend"><span><i style="background:var(--ink-3)"></i>不修</span><span><i style="background:var(--ink-2)"></i>只检测 + 切段</span><span><i style="background:var(--ok)"></i>检测 + 安全修复 + 切段</span></div>
      </div>
      <div class="gh-foot"><span>可训练帧 = 连续 ≥ ${g.min_clip_frames || 30} 帧、且复检后不含阻断问题的片段</span><button class="ghost sm" data-scroll="govDetails">看检测与治理细节 ↓</button></div>
    </section>
    <div class="gov-details" id="govDetails">
      <div class="gov-details-h"><span>细节：检测结论、逐条决策与生成物</span><span class="muted">主结论在上面；这里给需要核对的人</span></div>
      <div class="kpis">
        ${speedKpi(res)}
        ${kpi('修复副本', `${g.parquet_repaired_copies}<small>+ ${g.meta_patch_only}</small>`, `${g.parquet_repaired_copies} 个 Parquet 副本 + ${g.meta_patch_only} 个元数据补丁；${g.flagged_resolved?.total ?? 0} 条复检后不再需复核`)}
        ${kpi('改写单元格', `${fmt(g.values_changed)}<small>格</small>`, '每一格写后回读核验，并登记原值 → 新值')}
        ${kpi('证据不足 · 拒修', `${fmt(g.repairs_rejected)}<small>项</small>`, `另有 ${g.repair_candidates} 项候选修复未执行，全部写明原因`)}
        ${kpi('可训练帧', `${fmt(total)}<small>帧</small>`, base ? `比全盘丢弃多 ${fmt(total - base)} 帧（+${((total / base - 1) * 100).toFixed(1)}%）` : '')}
      </div>
      <div class="res-grid">
        <div class="col">
          <div class="card panel">
            <div class="card-h"><h3>帧级全景：治理后哪些帧进入训练</h3><div class="pano-tools seg" data-pseg><button data-p="before">治理前</button><button data-p="after" class="on">治理后</button></div></div>
            <div class="legend pano-legend" style="margin-bottom:10px"></div>
            <div class="pano-wrap"><canvas data-pano></canvas></div>
          </div>
        </div>
        <div class="col">
          <div class="card panel"><div class="card-h"><h3>治理决策流向</h3><span class="sub">原始检测状态 → 每条轨迹的去向</span></div><div class="sankey" id="sankey-${panel.stage}"></div></div>
          <div class="card panel"><div class="card-h"><h3>证据不足，主动拒修</h3><span class="sub">${fmt(g.repairs_rejected)} 项 · 按原因</span></div>
            <div class="reasons">${rs.slice(0, 7).map(([k, v]) => `<div class="reason"><div>${esc(k)}<div class="t"><i style="width:${(v / rmax) * 100}%"></i></div></div><b>${v}</b></div>`).join('')}</div>
            <div class="files-foot"><span>拒修的轨迹保持原样，交由隔离回采或人工复核</span>${res.stage === 'govern' ? '<button class="ghost sm" data-goto="3">看修复说明</button>' : ''}</div>
          </div>
          ${filesCard(res, res.stage, ['governed/'], '生成物：治理叠加层', ['report'])}
        </div>
      </div>
      ${res.stage === 'govern' ? '<div class="cta-row"><button class="cta" data-goto="3">下一步：修复前后对比</button><span class="hint">先看全量对比，再逐条看 ep29 修复前后的画面、ep74 为什么不修</span></div>' : ''}
    </div>`;
  requestAnimationFrame(() => {
    $$('.gh-row .t i', box).forEach((i) => (i.style.width = i.dataset.w + '%'));
    $$('.tier-row .t i', box).forEach((i) => (i.style.width = i.dataset.w + '%'));
  });
  $$('[data-scroll]', box).forEach((b) => b.addEventListener('click', () => $('#' + b.dataset.scroll, box)?.scrollIntoView({ behavior: 'smooth', block: 'start' })));
  const canvas = $('canvas[data-pano]', box);
  const legend = $('.pano-legend', box);
  const setP = (p) => {
    $$('[data-pseg] button', box).forEach((b) => b.classList.toggle('on', b.dataset.p === p));
    legend.innerHTML = p === 'after'
      ? `<span><i style="background:var(--keep)"></i>进入训练片段</span><span><i style="background:var(--fix)"></i>修复后进入训练</span><span><i style="background:var(--scatter)"></i>有效但不足 30 帧连续</span><span><i style="background:var(--blocked);border:1px solid var(--line-2)"></i>阻断 / 隔离</span><span><i style="background:var(--warn)"></i>重复剔除</span>`
      : CATS.map((c) => `<span><i style="background:var(${CAT_VAR[c]})"></i>${c}</span>`).join('') + '<span><i style="background:var(--cell)"></i>未命中</span>';
    drawPanorama(canvas, res, p, res.stage);
  };
  $$('[data-pseg] button', box).forEach((b) => b.addEventListener('click', () => setP(b.dataset.p)));
  setP('after');
  drawSankey($(`#sankey-${panel.stage}`, box), res);
  $$('[data-goto]', box).forEach((b) => b.addEventListener('click', () => setStep(+b.dataset.goto)));
  bindCommon(box, res, res.stage);
}

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

function showEpisodes(res, stage) {
  openModal(`<h2 style="font-size:22px">轨迹清单</h2><p class="sub" style="margin:4px 0 14px">点击任一行查看该条轨迹的画面、信号与问题区间</p>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>轨迹</th><th>行数</th><th>状态</th><th>评分</th><th>${stage === 'govern' ? '复检评分' : '证据等级'}</th><th>治理决策</th><th>训练帧</th><th>问题码</th></tr></thead><tbody>
    ${res.episodes.map((e) => `<tr class="click" data-ep="${e.id}"><td><b>${epName(e.id)}</b></td><td>${e.rows}</td><td><span class="badge b-${STATUS_CLS[e.status] || 'bad'}"><i></i>${esc(e.status)}</span></td><td>${fmt(e.score, 1)}</td><td>${stage === 'govern' ? fmt(e.final_score, 1) : esc(e.tier || '—')}</td><td>${esc(e.policy)}</td><td>${fmt(e.train_frames)}</td><td class="mono" style="max-width:420px">${e.codes.map(esc).join(' ')}</td></tr>`).join('')}
    </tbody></table></div>`);
  $$('#modalContent tr.click').forEach((tr) => tr.addEventListener('click', () => { $('#modal').hidden = true; openEpisode(stage, +tr.dataset.ep); }));
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
const pathNum = (p) => { const m = /(\d+)\.\w+$/.exec(p || ''); return m ? +m[1] : null; };
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
  const repaired = !!r.repaired_file;
  const shownStatus = repaired ? r.final_status : r.status;
  const shownScore = repaired ? r.final_overall_score : r.overall_score;
  const head = `<div class="ep-head"><h2 id="drawerTitle">${epName(ep)}</h2>
    <span class="badge b-${STATUS_CLS[shownStatus] || 'bad'}"><i></i>${esc(shownStatus)}</span>
    <span class="badge">${fmt(+shownScore, 1)} 分${repaired ? ' · 复检后' : ''}</span>
    <span class="badge">${fmt(+r.rows)} 行</span>
    <span class="badge">${res?.kind === 'detect' ? '不修复时：' : ''}${esc(r.train_policy)} · 训练帧 ${fmt(+r.train_frames)}</span>
    ${res?.kind === 'govern' && repaired ? '<span class="badge b-fix"><i></i>已生成修复副本</span>' : ''}</div>`;
  if (!raw.ok || !n) {
    return openDrawer(`${head}<div class="sect"><p class="banner" style="border-color:var(--line-2)">原始文件无法读取或没有数据行：${esc(raw.error || r.issue_details)}</p></div>${codeListHtml(codes, dict, r)}`);
  }
  // interval lanes
  const byCode = {};
  for (const it of d.intervals) (byCode[it.issue_code] = byCode[it.issue_code] || []).push(it);
  const lanes = Object.entries(byCode).map(([c, its]) => `<div class="lane"><span>${esc(c)}</span><div class="lt">${its.map((it) => `<i style="left:${(+it.start_row / n) * 100}%;width:${Math.max(((+it.end_row - +it.start_row + 1) / n) * 100, 0.4)}%;background:var(${CAT_VAR[catOf(c)]})" title="${esc(it.description)}：第 ${it.start_row}–${it.end_row} 行（${it.frames} 帧）${it.streams ? ' · ' + esc(it.streams) : ''}"></i>`).join('')}<b class="ph"></b></div></div>`).join('');
  const mask = d.mask;
  const keepLane = mask ? `<div class="lane"><span>进入训练片段</span><div class="lt">${runsOf(mask.map((m) => m.keep)).map(([a, b]) => `<i style="left:${(a / n) * 100}%;width:${((b - a + 1) / n) * 100}%;background:var(--keep)"></i>`).join('')}<b class="ph"></b></div></div>` : '';
  const dims = ['structure', 'temporal', 'sync', 'content', 'value'];
  openDrawer(`${head}
    <div class="ep-grid">
      <div>
        <div class="cams" id="dCams">${['image', 'left_wrist_image', 'right_wrist_image'].map((c, i) => `<div class="cam" data-cam="${c}"><img alt=""><span>${['场景', '左腕', '右腕'][i]}</span><em></em></div>`).join('')}</div>
        <div class="scrub"><button class="ghost sm" id="dPlay">播放</button><input type="range" id="dRow" min="0" max="${n - 1}" value="${Math.min(row, n - 1)}"><span class="rowlab" id="dRowLab"></span></div>
        <div class="sect"><h4>问题区间（逐行定位）</h4><div class="lanes" id="dLanes">${lanes || '<span class="muted">没有帧级问题区间</span>'}${keepLane}</div></div>
        <div class="sect"><div class="tabs" id="dTabs"></div><div id="dChart"></div></div>
      </div>
      <div>
        <div class="sect" style="margin-top:0"><h4>${repaired ? '修复前五维评分' : '五维评分'}</h4><div class="dims">${dims.map((k) => { const v = +r[k + '_score']; return `<div class="dim"><span>${DIM_CN[k]}</span><div class="t"><i style="width:${Number.isFinite(v) ? v : 0}%"></i></div><span class="v">${Number.isFinite(v) ? fmt(v, 1) : '—'}</span></div>`; }).join('')}</div></div>
        ${codeListHtml(codes, dict, r)}
        <div class="sect"><h4>处理决策</h4><dl class="kv">
          <dt>治理决策</dt><dd>${esc(r.train_policy)}</dd>
          <dt>有效帧 / 训练帧</dt><dd>${fmt(+r.frame_valid)} / ${fmt(+r.train_frames)}（${r.clip_count} 段）</dd>
          <dt>样本权重</dt><dd>${esc(r.sample_weight)}</dd>
          ${r.repair_applied ? `<dt>已修复</dt><dd>${esc(r.repair_applied)}</dd>` : ''}
          ${r.repair_rejected ? `<dt>拒修原因</dt><dd>${esc(r.repair_rejected)}</dd>` : ''}
          <dt>单条耗时</dt><dd>${esc(r.seconds)} s</dd>
        </dl></div>
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

function codeListHtml(codes, dict, r) {
  const details = {};
  for (const m of (r.issue_details || '').matchAll(/([A-Z]_[A-Z0-9_]+)\(([^()]*(?:\([^()]*\)[^()]*)*)\)/g)) details[m[1]] = m[2];
  return `<div class="sect"><h4>问题码（${codes.length}）</h4><div class="code-list">${codes.length ? codes.map((c) => `<div class="code-item"><span class="code"><i style="background:var(${CAT_VAR[catOf(c)]})"></i>${esc(c)}</span>${esc(details[c] || dict[c]?.description || '')}</div>`).join('') : '<span class="muted">未命中任何问题码</span>'}</div></div>`;
}

const CAMS = ['image', 'left_wrist_image', 'right_wrist_image'];
const CAM_CN = { image: '场景相机', left_wrist_image: '左腕相机', right_wrist_image: '右腕相机' };
const CODE_CN = {
  J_SPIKE_JUMP: 'state / actions 相邻帧突跳',
  J_STATE_ACTION_MISMATCH: 'state 与 actions 对不上',
  J_ROT6D_INVALID: 'Rotation-6D 不满足正交约束',
  J_GRIPPER_RANGE: '夹爪开度越界',
  J_STATE_NONFINITE: 'state 有 NaN/Inf 空值',
  J_ACTION_NONFINITE: 'actions 有 NaN/Inf 空值',
  J_POSITION_ENVELOPE: '末端位置出现孤立越界值',
  T_TIMESTAMP_GAP: '时间戳有掉帧缺口',
  T_FRAME_INDEX_GAP: '帧号跳号，中间缺帧',
  T_TIMESTAMP_NON_MONOTONIC: '时间戳重复或倒退',
  T_TIMESTAMP_JITTER: '采样间隔抖动',
  T_FRAME_ORDER_ERROR: '帧号重复或倒序',
  T_INDEX_DISCONTINUITY: '全局 index 与帧号不同步',
  T_TIMESTAMP_OFFSET: '时间戳基准不为 0',
  T_TIMESTAMP_UNIT_MISMATCH: '时间戳单位标错',
  T_TIMESTAMP_INVALID: '时间戳含非法值',
  T_METADATA_LENGTH_MISMATCH: '元数据记录长度与实际不符',
  C_IMAGE_SCREEN: '画面黑屏、白屏或低对比度',
  C_IMAGE_BLUR: '画面模糊',
  C_IMAGE_DUPLICATE: '相邻帧画面重复',
  C_IMAGE_SHAPE: '图像分辨率或通道数不符',
  C_FILE_UNREADABLE: '文件无法读取',
  C_SCHEMA_MISSING_FIELD: '必需字段缺失',
  C_SCHEMA_DTYPE_MISMATCH: '字段类型与声明不符',
  S_IMAGE_PATH_MISMATCH: '图像路径与数据行局部不一致',
  S_STREAM_MISSING: '某路相机整条没有图像',
  S_SENSOR_LATE_START: '相机开头缺图',
  S_SENSOR_EARLY_STOP: '相机结尾缺图',
  S_SENSOR_DROPOUT: '相机中段断流',
  S_STREAM_SHIFTED: '相机流整体错位',
  S_TASK_INDEX_INVALID: '任务标签是未定义值',
  S_TASK_SWITCH_WITHIN_EPISODE: '一条轨迹里任务标签中途变了',
  S_TASK_META_MISMATCH: '任务标签与元数据不一致',
  V_LOW_VISUAL_DIVERSITY: '画面多样性偏低',
  V_LOW_INTERACTION: '有效交互帧偏少',
  V_EPISODE_OVERLAP: '与其他轨迹重复',
};
function peakLag(values, L) {
  let k = -1, best = -2;
  (values || []).forEach((v, i) => { if (v != null && v > best) { best = v; k = i; } });
  return k < 0 ? null : { lag: k - L, r: best };
}
function fixLabel(m) {
  const a = m.applied || '';
  const mm = /^(\w+) 图像整体移回 ([+-]?\d+) 帧/.exec(a);
  if (mm) return `${CAM_CN[mm[1]] || mm[1]}整体错位 ${mm[2]} 帧，移回真实画面`;
  if (/task_index/.test(a)) return '任务标签与元数据不一致，按元数据改正';
  if (/存储类型/.test(a)) return 'timestamp 存储类型无损转换，数值不变';
  if (/越界/.test(a)) return '孤立单帧越界，只改越界分量';
  if (/非有限/.test(a)) return 'NaN 稀疏补值，Rotation-6D 约束解出';
  if (/length/.test(a)) return '元数据记录长度补丁，不改 Parquet';
  return a.slice(0, 40);
}
function reasonLabel(r) {
  r = (r || '').trim();
  if (/^时间戳规整/.test(r)) return '缺帧不能凭空重建时间戳';
  if (/^index 重建/.test(r)) return '帧号不连续，索引无法唯一确定';
  if (/^时间戳/.test(r)) return '非单位类时间问题，不按标称周期伪造时钟';
  if (/稀疏补值/.test(r)) return 'NaN 比例超过上限，整段不臆造';
  if (/越界/.test(r)) return '连续越界不是孤立突跳，不插值';
  if (/必需字段缺失/.test(r)) return '必需字段缺失，文件级隔离';
  if (/重对齐/.test(r)) return '相机错位缺少内容证据，不移动画面';
  if (/写后逐单元格校验失败/.test(r)) return '写后回读校验失败，副本丢弃';
  return r.split('：')[0];
}

const signed = (n) => (n == null ? '—' : (n > 0 ? '+' : '') + n);
function shiftFromText(text) {
  const t = String(text || '');
  const m = /(image|left_wrist_image|right_wrist_image)\s*图像整体移回\s*([+-]?\d+)\s*帧/.exec(t)
    || /(image|left_wrist_image|right_wrist_image)\s*重对齐/.exec(t);
  if (!m) return null;
  const off = /图像整体移回\s*([+-]?\d+)\s*帧/.exec(t) || /path\s*偏移\s*([+-]?\d+)/.exec(t);
  return { cam: m[1], frames: off ? +off[1] : null };
}
function crossR(text) {
  const m = /跨相机运动相关[^；]*?r=([0-9.]+)/.exec(text || '');
  return m ? +m[1] : null;
}
function armR(text) {
  const m = /本臂运动[^；]*?r=([0-9.]+)/.exec(text || '');
  return m ? +m[1] : null;
}
function codeLine(ev, codes, shift) {
  const c = (codes || [])[0] || '';
  if (shift) return `${CAM_CN[shift.cam] || shift.cam}画面整体错位 ${signed(shift.frames)} 帧`;
  if (c === 'J_STATE_NONFINITE' && ev.flagged) return `state 有 ${fmt(ev.flagged)} 行空值（NaN/Inf）`;
  if (c === 'J_POSITION_ENVELOPE' && ev.flagged) return `末端位置有 ${fmt(ev.flagged)} 行孤立越界`;
  if (codes.length > 1) return `${CODE_CN[c] || c} · 另有 ${codes.length - 1} 类问题`;
  return CODE_CN[c] || c;
}
function captionFor(ev, kind) {
  const ep = `第 ${ev.ep} 条`;
  const b = ev.brief || {};
  const shift = b.shift || shiftFromText(b.applied || b.rejected);
  const codes = ev.codes || [];
  const finalCodes = ev.final_codes || [];
  if (ev.error) return { text: `${ep} · 文件处理异常，已记录并继续批次`, meta: '其余轨迹照常处理' };
  if (kind === 'govern') {
    if (ev.repaired || ev.meta_patch) {
      let what;
      if (shift) what = `${CAM_CN[shift.cam] || shift.cam}画面整体错位 ${signed(shift.frames)} 帧 → 已移回`;
      else if (ev.meta_patch) what = '元数据记录长度不符 → 只更新元数据';
      else what = fixLabel({ applied: b.applied || '' });
      const score = ev.score != null && ev.final_score != null ? `复检 ${fmt(ev.score, 2)} → ${fmt(ev.final_score, 2)} 分` : '';
      return { text: `${ep} · ${what}`, meta: [ev.cells ? `改写 ${fmt(ev.cells)} 格` : '', score].filter(Boolean).join(' · ') };
    }
    if (b.rejected || ev.rejected) {
      const r = crossR(b.rejected);
      const why = reasonLabel((b.rejected || '').split('；')[0]);
      const s = shift ? `${CAM_CN[shift.cam] || shift.cam}疑似错位 ${signed(shift.frames)} 帧` : why;
      const evidence = shift ? (r != null ? `跨相机相关只有 ${r.toFixed(2)}，低于 0.80` : '画面内容证据不足') : '证据不足';
      return { text: `${ep} · ${s} → 拒修`, meta: `${evidence} · 保持原样，问题段不进入训练` };
    }
    if (!finalCodes.length) return { text: `${ep} · 通过`, meta: `${fmt(ev.rows)} 行全部可进入训练` };
    return { text: `${ep} · ${codeLine(ev, finalCodes, shift)}`, meta: '这类问题没有可用的修复方法，保持原样；问题帧不进入训练' };
  }
  if (!codes.length) return { text: `${ep} · 未命中阻断问题`, meta: `${fmt(ev.rows)} 行 · ${fmt(ev.seconds, 2)} 秒` };
  const marked = ev.flagged ? `${fmt(ev.flagged)} 行被标记` : '整条轨迹级问题';
  return { text: `${ep} · ${codeLine(ev, codes, shift)}`, meta: `${marked} · 共 ${fmt(ev.rows)} 行 · ${fmt(ev.seconds, 2)} 秒` };
}

function runsText(rows, max = 4) {
  const xs = [...new Set(rows.map(Number).filter((v) => Number.isFinite(v)))].sort((a, b) => a - b);
  if (!xs.length) return '—';
  const out = [];
  let a = xs[0], b = xs[0];
  for (const x of xs.slice(1)) {
    if (x === b + 1) b = x;
    else { out.push([a, b]); a = b = x; }
  }
  out.push([a, b]);
  return out.slice(0, max).map(([x, y]) => (x === y ? `第 ${x} 行` : `第 ${x}–${y} 行`)).join('、') + (out.length > max ? ' 等' : '');
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
async function ensureGovern() {
  if (S.res.govern) return S.res.govern;
  if (!S.info.runs.govern) return null;
  const r = await api('/api/result?stage=govern');
  if (r.ok) { S.res.govern = r; markDone(); }
  return r.ok ? r : null;
}

function needGovernHtml(what) {
  return `<div class="panel empty"><h3>${what}需要先完成修复</h3><p>回到第 2 步运行一次修复，或切换到回放模式。</p><button class="cta" data-goto="2">去修复</button></div>`;
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
  const rTxt = r == null ? '—' : r.toFixed(2);
  return `<button class="pair-case ${kind} ${activeEp === ep ? 'on' : ''}" data-ep="${ep}">
    <span class="pc-kicker">相机疑似错位</span>
    <div class="pc-top"><b>${epName(ep)}</b><span class="pc-r">相关度 <b>${rTxt}</b>${r != null ? ` ${kind === 'fixed' ? '≥' : '<'} 0.80` : ''}</span></div>
    <div class="pc-verdict">${kind === 'fixed' ? `修了 · 移回 ${Math.abs(sh?.frames ?? 0)} 帧` : '没修 · 保持隔离'}</div>
    <div class="pc-foot">${fmt(+m.score_before, 2)} ${kind === 'fixed' ? '→ ' + fmt(+m.score_after, 2) : '不变'} · ${fmt(e.train_frames)} 帧可训练</div>
  </button>`;
}

function pairBannerHtml(res, activeEp) {
  const pair = shiftPairs(res);
  if (!pair.fixed && !pair.refused) return '';
  return `<div class="panel pair">
    <div class="pair-head">
      <div><h3>同样的现象，为什么一个修、一个不修</h3><p>两张卡片用同一句话开头：「相机疑似错位」。区别只在内容证据够不够。</p></div>
    </div>
    <div class="pair-cases">${pairCard(pair.fixed, 'fixed', res, activeEp)}${pairCard(pair.refused, 'refused', res, activeEp)}</div>
  </div>`;
}

// ------------------------------------------------------------ before / after: whole dataset, same detector re-run on the repaired copies
function baPair(label, b, a, opt = {}) {
  const d = b != null && a != null ? a - b : null;
  const good = d == null || d === 0 ? '' : (opt.lower ? d < 0 : d > 0) ? 'good' : 'worse';
  const f = (v) => (v == null ? '—' : opt.dec ? fmt(v, opt.dec) : fmt(v));
  const pctTxt = d && b ? ` · ${d > 0 ? '+' : ''}${((d / b) * 100).toFixed(1)}%` : '';
  return `<div class="bap ${good}">
    <span class="bap-l">${label}</span>
    <div class="bap-v"><s>${f(b)}</s><em>→</em><b>${f(a)}</b></div>
    <span class="bap-d">${d == null ? '' : d === 0 ? '不变' : `${d > 0 ? '+' : ''}${opt.dec ? fmt(d, opt.dec) : fmt(d)}${pctTxt}`}${opt.sub ? ` · ${opt.sub}` : ''}</span>
  </div>`;
}

function overallHtml(res, opts = {}) {
  const C = res.compare;
  if (!C) return '';
  const sb = C.status_before || {}, sa = C.status_after || {};
  const maxCat = Math.max(1, ...CATS.map((c) => Math.max(C.cat_rows_before[c] || 0, C.cat_rows_after[c] || 0)));
  const catRows = CATS.map((c) => {
    const b = C.cat_rows_before[c] || 0, a = C.cat_rows_after[c] || 0;
    const eb = C.cat_eps_before[c] || 0, ea = C.cat_eps_after[c] || 0;
    return `<div class="bac">
      <span class="bac-l"><i style="background:var(${CAT_VAR[c]})"></i>${c}</span>
      <div class="bac-t"><i class="b" style="width:${(b / maxCat) * 100}%"></i><i class="a" style="width:${(a / maxCat) * 100}%;background:var(${CAT_VAR[c]})"></i></div>
      <span class="bac-v">${b || a ? `${fmt(b)}<em>→</em><b>${fmt(a)}</b> 帧` : '<small>只有轨迹级问题</small>'}</span>
      <span class="bac-e">${eb}<em>→</em>${ea} 条</span>
    </div>`;
  }).join('');
  const moved = C.codes.filter((x) => x.episodes_before !== x.episodes_after || x.frames_before !== x.frames_after);
  const same = C.codes.length - moved.length;
  const codeRow = (x) => `<tr class="${x.episodes_after === 0 ? 'gone' : ''}">
      <td><span class="code"><i style="background:var(${CAT_VAR[x.category]})"></i>${esc(x.code)}</span></td>
      <td>${esc(CODE_CN[x.code] || x.description)}</td>
      <td class="num">${x.episodes_before}<em>→</em><b>${x.episodes_after}</b></td>
      <td class="num">${x.frames_before || x.frames_after ? `${fmt(x.frames_before)}<em>→</em><b>${fmt(x.frames_after)}</b>` : '<span class="muted">轨迹级</span>'}</td>
    </tr>`;
  const ch = (C.changed || []).map((x) => `<tr class="click" data-ep="${x.ep}">
      <td><b>${epName(x.ep)}</b></td>
      <td><span class="badge b-${STATUS_CLS[x.status_before] || 'bad'}"><i></i>${esc(x.status_before)}</span> → <span class="badge b-${STATUS_CLS[x.status_after] || 'bad'}"><i></i>${esc(x.status_after)}</span></td>
      <td class="num">${fmt(x.score_before, 2)} → <b>${fmt(x.score_after, 2)}</b></td>
      <td>${x.resolved.map((c) => `<span class="code gone"><i style="background:var(${CAT_VAR[catOf(c)]})"></i>${esc(c)}</span>`).join(' ')}${x.new.map((c) => `<span class="code new"><i style="background:var(${CAT_VAR[catOf(c)]})"></i>${esc(c)}</span>`).join(' ')}${x.remaining.length ? `<span class="muted"> 仍有 ${x.remaining.length} 种</span>` : ''}</td>
      <td class="num">${fmt(x.train_frames)}</td>
      <td>${esc(x.policy || '')}</td>
    </tr>`).join('');
  return `<div class="ov">
    <section class="panel ov-head">
      <div class="ov-copy">
        <p class="eyebrow">同一批 ${fmt(C.episodes)} 条轨迹 · ${fmt(C.rows)} 行 · 修复后对副本用同一套检测重新跑一遍</p>
        <h2>修复前 <em>vs</em> 修复后</h2>
        <p class="ov-lead">左边是原始数据的检测结果，右边是修复副本的复检结果。数字全部来自算子这次写出的报告，不依赖任何人工标注。</p>
      </div>
      <div class="bap-grid">
        ${baPair('需复核轨迹', sb['需复核'] || 0, sa['需复核'] || 0, { lower: true })}
        ${baPair('通过的轨迹', sb['通过'] || 0, sa['通过'] || 0)}
        ${baPair('带问题码的帧', C.flag_rows_before, C.flag_rows_after, { lower: true })}
        ${baPair('平均质量分', C.score_mean_before, C.score_mean_after, { dec: 2 })}
        ${baPair('可训练帧', C.train_base, C.train_total, { sub: C.train_detect_only != null ? `只检测+切段 ${fmt(C.train_detect_only)}` : '含切段' })}
      </div>
    </section>
    <section class="panel card">
      <div class="card-h"><h3>逐帧全景：修复前 / 修复后（复检）</h3><span class="sub">每行一条轨迹，每格一帧；两边同一行是同一条轨迹</span></div>
      <div class="legend" style="margin-bottom:10px">${CATS.map((c) => `<span><i style="background:var(${CAT_VAR[c]})"></i>${c}</span>`).join('')}<span><i style="background:var(--fix)"></i>被修复改写的行</span><span><i style="background:var(--cell)"></i>未命中</span><span class="muted">浅色 = 整条轨迹级问题</span></div>
      <div class="ov-panos">
        <div><div class="ov-pt"><span class="dot ov-b"></span>修复前 · 原始检测<b>${fmt(C.flag_rows_before)}</b><small>帧带问题码</small></div><div class="pano-wrap"><canvas data-pano="before"></canvas></div></div>
        <div><div class="ov-pt"><span class="dot ov-f"></span>修复后 · 复检<b>${fmt(C.flag_rows_after)}</b><small>帧带问题码</small></div><div class="pano-wrap"><canvas data-pano="final"></canvas></div></div>
      </div>
    </section>
    <div class="ov-two">
      <section class="panel card">
        <div class="card-h"><h3>四类问题：修复前 → 修复后</h3><span class="sub">上条灰 = 修复前，下条彩色 = 复检后</span></div>
        <div class="bacs">${catRows}</div>
        <p class="note" style="margin-top:12px">修复只动证据充分的那一部分：同步类（相机错位、任务标签）下降最明显；证据不足的问题保持原样、不进入训练。</p>
      </section>
      <section class="panel card">
        <div class="card-h"><h3>问题码变化</h3><span class="sub">${moved.length} 种有变化${same ? ` · 其余 ${same} 种不变` : ''}</span></div>
        <div class="tbl-wrap ov-codes"><table class="tbl"><thead><tr><th>问题码</th><th>说明</th><th>轨迹</th><th>帧</th></tr></thead><tbody>${moved.map(codeRow).join('') || '<tr><td colspan="4" class="muted">没有问题码发生变化</td></tr>'}</tbody></table></div>
        ${same ? `<details class="ov-more"><summary>展开其余 ${same} 种不变的问题码</summary><div class="tbl-wrap ov-codes"><table class="tbl"><tbody>${C.codes.filter((x) => !moved.includes(x)).map(codeRow).join('')}</tbody></table></div></details>` : ''}
      </section>
    </div>
    <section class="panel card">
      <div class="card-h"><h3>有变化的轨迹（${(C.changed || []).length} 条）</h3><span class="sub">删除线 = 复检已消除；带框 = 修复后如实标出的新问题（如相机移回后开头缺图）；${opts.compareHint || '点任一行看修复前后画面'}</span></div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>轨迹</th><th>状态</th><th>质量分</th><th>问题码变化</th><th>训练帧</th><th>去向</th></tr></thead><tbody>${ch || '<tr><td colspan="6" class="muted">这次没有轨迹发生变化</td></tr>'}</tbody></table></div>
    </section>
  </div>`;
}

function bindOverall(host, res, onEp) {
  $$('canvas[data-pano]', host).forEach((c) => drawPanorama(c, res, c.dataset.pano, res.stage));
  $$('tr.click[data-ep]', host).forEach((tr) => tr.addEventListener('click', () => onEp(+tr.dataset.ep)));
}

function showOverallInStep3() {
  const res = S.res.govern;
  S.cmpEp = 'overall';
  if (S.cmpTimer) { clearInterval(S.cmpTimer); S.cmpTimer = null; }
  $$('#cmpList .case').forEach((b) => b.classList.toggle('on', b.dataset.ep === 'overall'));
  const main = $('#cmpMain');
  main.innerHTML = overallHtml(res) + `<div class="cta-row"><button class="cta" data-ep-go="${shiftPairs(res).fixed?.episode_index ?? ''}">逐条看：同一现象，为什么一个修、一个不修</button><span class="hint">修复前后同一行画面并排对比</span></div>`;
  bindOverall(main, res, (ep) => { selectCase(ep); window.scrollTo({ top: 0, behavior: 'smooth' }); });
  $$('[data-ep-go]', main).forEach((b) => b.addEventListener('click', () => { if (b.dataset.epGo !== '') { selectCase(+b.dataset.epGo); window.scrollTo({ top: 0, behavior: 'smooth' }); } }));
}

async function renderCompare() {
  const list = $('#cmpList'), main = $('#cmpMain');
  const res = await ensureGovern();
  if (!res) {
    list.hidden = true;
    main.innerHTML = needGovernHtml('修复前后对比');
    $$('[data-goto]', main).forEach((b) => b.addEventListener('click', () => setStep(STEP.govern)));
    return;
  }
  list.hidden = false;
  if (S.cmpFor === res.created && S.cmpEp != null) return;
  S.cmpFor = res.created;
  const pair = shiftPairs(res);
  const pairIds = new Set([pair.fixed, pair.refused].filter(Boolean).map((m) => +m.episode_index));
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
    const sb = +m.score_before, sa = +m.score_after;
    const desc = kind === 'fix' ? fixLabel(m) : reasonLabel((m.rejected || '').split('；')[0]);
    return `<button class="case" data-ep="${m.episode_index}"><b>${epName(+m.episode_index)}</b><span class="sc">${kind === 'fix' ? `<s>${fmt(sb, 2)}</s> → ${fmt(sa, 2)}` : fmt(sb, 2)}</span><span class="d">${esc(desc)}</span></button>`;
  };
  const mini = (m, kind) => {
    if (!m) return '';
    const ep = +m.episode_index;
    const text = kind === 'fixed' ? m.applied : m.rejected;
    const r = crossR(text);
    return `<button class="case mini-case ${kind}" data-ep="${ep}">
      <span class="mc-kicker">相机疑似错位</span>
      <b>${epName(ep)}</b>
      <em>${r == null ? '' : '相关度 ' + r.toFixed(2) + '，'}${kind === 'fixed' ? '修了' : '没修'}</em>
    </button>`;
  };
  const otherFixed = fixed.filter((m) => !pairIds.has(+m.episode_index));
  const otherRefused = refused.filter((m) => !pairIds.has(+m.episode_index));
  list.innerHTML = `<button class="case case-overall" data-ep="overall"><b>全量对比</b><span class="sc">默认</span><span class="d">修复前 vs 修复后：需复核轨迹、带问题的帧、问题码、可训练帧</span></button>
    <div class="mini-pair">
      <h4>同样的现象，为什么一个修、一个不修</h4>
      <div class="mini-cases">${mini(pair.fixed, 'fixed')}${mini(pair.refused, 'refused')}</div>
    </div>
    <button class="case case-notes" data-ep="notes"><b>修复说明</b><span class="sc">点开看</span><span class="d">每类问题怎么修、必须满足什么条件、哪些坚决不修</span></button>
    <h4>其他已修复 · ${otherFixed.length} 条</h4>${otherFixed.map((m) => item(m, 'fix')).join('')}
    <h4>其他拒修示例</h4>${otherRefused.map((m) => item(m, 'ref')).join('')}`;
  $$('.case', list).forEach((b) => b.addEventListener('click', () => (b.dataset.ep === 'notes' ? showNotesInStep4() : b.dataset.ep === 'overall' ? showOverallInStep3() : selectCase(+b.dataset.ep))));
  if (res.compare) showOverallInStep3();
  else if (pair.fixed) selectCase(+pair.fixed.episode_index);
  else showNotesInStep4();
}

function showNotesInStep4() {
  S.cmpEp = 'notes';
  $$('#cmpList .case').forEach((b) => b.classList.toggle('on', b.dataset.ep === 'notes'));
  renderRepairNotes('govern', $('#cmpMain'), (ep) => { selectCase(ep); window.scrollTo({ top: 0, behavior: 'smooth' }); });
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
        <span class="sep">改写 <b class="num">${fmt(f.cells)}</b> 格</span>
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
    <div class="panel card notes-head">
      <h2>修复说明</h2>
      <p class="lead">本次治理修复 <b>${T.fixed}</b> 条轨迹（${T.parquet} 个 Parquet 副本 + ${T.meta} 个元数据补丁），共改写 <b>${fmt(T.cells)}</b> 个单元格；证据不足、主动不修 <b>${nRef}</b> 项；提出方案但不自动执行 <b>${T.candidates}</b> 项。最终可训练 <b>${fmt(T.train_frames)}</b> 帧。</p>
      <ul class="rules">${n.safety.map((s) => `<li>${esc(s)}</li>`).join('')}</ul>
    </div>
    <div class="panel card"><div class="card-h"><h3>一、每类问题怎么修、什么条件下才修</h3><span class="sub">条件全部满足才动手，任何一条不满足就不修</span></div>
      <div class="tbl-wrap notes-tbl"><table class="tbl"><thead><tr><th>问题类型</th><th>问题是什么</th><th>怎么修</th><th>必须满足</th><th>不满足时</th><th>本次</th></tr></thead><tbody>${methods}</tbody></table></div>
    </div>
    <div class="panel card"><div class="card-h"><h3>二、逐条修复说明（${n.fixed.length} 条）</h3><span class="sub">点「看对比」查看修复前后的数据</span></div>
      <div class="fixes">${fixed || '<p class="muted">本次没有执行修复</p>'}</div>
    </div>
    <div class="panel card"><div class="card-h"><h3>三、没有修复的项目（${nRef} 项）</h3><span class="sub">保持原样，问题段不进入训练</span></div>
      <div class="refs">${refused || '<p class="muted">没有拒修项</p>'}</div>
    </div>
    <div class="panel card"><div class="card-h"><h3>四、提出了方案、但不自动执行（${T.candidates} 项）</h3><span class="sub">repair_candidates.csv · 交人工确认</span></div>
      ${cands ? `<div class="tbl-wrap notes-tbl"><table class="tbl"><thead><tr><th>问题码</th><th>候选方案</th><th>不自动执行的原因</th><th>涉及轨迹</th></tr></thead><tbody>${cands}</tbody></table></div>` : '<p class="muted">没有候选方案</p>'}
    </div>
  </div>`;
  $$('[data-ep]', host).forEach((b) => b.addEventListener('click', () => onOpen(+b.dataset.ep)));
}

async function selectCase(ep, stage = 'govern', main = $('#cmpMain')) {
  const inStep4 = main.id === 'cmpMain';
  if (S.cmpTimer) { clearInterval(S.cmpTimer); S.cmpTimer = null; }
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
  const fixed = m.kind && m.kind !== '未修改';
  const before = (m.codes_before || r.issue_codes || '').split(';').filter(Boolean);
  const after = (m.codes_after || r.final_issue_codes || '').split(';').filter(Boolean);
  const gone = before.filter((c) => !after.includes(c));
  const audit = d.audit || [];
  const audCodes = [...new Set(audit.map((a) => a.repair_code))];
  const isShift = audCodes.includes('S_STREAM_SHIFTED')
    || (fixed ? /图像整体移回/.test(m.applied || '') : /重对齐/.test(m.rejected || ''));
  const res = S.res[stage] || {};
  const pair = shiftPairs(res);
  let head;
  if (isShift) {
    const text = fixed ? m.applied : m.rejected;
    const sh = shiftFromText(text);
    const cr = crossR(text);
    const ar = armR(text);
    const gate = cr != null ? { r: cr, thr: 0.8, label: '跨相机运动相关' } : ar != null ? { r: ar, thr: 0.6, label: '本臂运动相关' } : null;
    const epInfo = (res.episodes || []).find((x) => x.id === ep) || {};
    const cam = sh?.cam ? CAM_CN[sh.cam] : '相机';
    const frames = Math.abs(sh?.frames ?? 0);
    head = `<div class="panel cmp-head shift-head">
      <div class="cmp-title">
        <h2>${epName(ep)}</h2>
        <span class="badge ${fixed ? 'b-fix' : 'b-hint'}"><i></i>${fixed ? '修了' : '没修'}</span>
        <span class="verdict-line">${cam}${fixed ? '整体错位' : '疑似错位'} ${signed(sh?.frames)} 帧${fixed ? ' · 已移回' : ' · 保持原样'}</span>
      </div>
      <p class="cmp-method">${fixed
        ? `同一行的画面和机械臂状态错开 ${frames} 帧。${gate ? `${gate.label} r=${gate.r.toFixed(2)}（门槛 ${gate.thr.toFixed(2)}），` : ''}对端恰好缺 ${frames} 帧；修复只移动 ${cam} 这一列。`
        : `path 偏移 ${signed(sh?.frames)} 帧，但${gate ? `${gate.label}只有 ${gate.r.toFixed(2)}（门槛 ${gate.thr.toFixed(2)}）` : '内容证据不足'}。偏移整齐不等于画面真的错了，证据不足就不动。`}</p>
      <div class="cmp-foot">
        <span class="badge">训练帧 ${fmt(epInfo.train_frames)}</span>
        <span class="badge">${fixed ? `改写 ${fmt(audit.length)} 格` : '0 格改写'}</span>
        <span class="footnote">${fixed ? `复检 ${fmt(+m.score_before, 2)} → ${fmt(+m.score_after, 2)} 分` : `复检 ${fmt(+m.score_before, 2)} 分 · 保持原样`}</span>
        ${stage === 'govern' ? '<button class="ghost sm" data-notes>修复说明</button>' : ''}
      </div>
    </div>${stage === 'govern' ? pairBannerHtml(res, ep) : ''}`;
  } else {
    const epInfo = (res.episodes || []).find((x) => x.id === ep) || {};
    const stateDim = d.raw.state?.[0]?.length || 0;
    const actionDim = d.raw.actions?.[0]?.length || 0;
    const totalCells = d.raw.n * (stateDim + actionDim);
    let headline = fixed ? `修了：${fmt(audit.length)} 格改动` : '拒修：保持原样';
    let detail = fixed ? (m.applied || '') : (m.rejected || '');
    let count = fixed ? `改写 ${fmt(audit.length)} 格` : '0 格改写';
    if (!fixed) {
      headline = `拒修：${reasonLabel((m.rejected || '').split('；')[0])}`;
      detail = m.rejected || '';
    } else if (before.includes('J_STATE_NONFINITE')) {
      headline = `state 里 ${fmt(audit.length)} 个空值，只补这 ${fmt(audit.length)} 格`;
      detail = '只补非有限分量；Rotation-6D 按正交约束解出，其余分量逐位不变。';
      count = `改写 ${fmt(audit.length)} / ${fmt(totalCells)} 格`;
    } else if (before.includes('J_POSITION_ENVELOPE')) {
      headline = `末端位置 ${fmt(audit.length)} 个越界分量，只改越界的那几格`;
      detail = '孤立单帧越界才修：取前后帧均值；连续越界保持原样。';
      count = `改写 ${fmt(audit.length)} / ${fmt(totalCells)} 格`;
    } else if (before.some((c) => /^S_TASK/.test(c))) {
      headline = '任务标签和元数据不一致，只改 task_index 这一列';
      detail = '按 episodes.jsonl 唯一登记的任务统一整条；画面、动作、时间戳都不动。';
      count = `改写 ${fmt(audit.length)} 行 task_index`;
    } else if (before.includes('C_SCHEMA_DTYPE_MISMATCH')) {
      headline = '字段类型和声明不符，无损转换';
      detail = '按 info.json 声明转换存储类型，每个数值逐位不变。';
      count = '0 个数值变化';
    } else if (m.kind === '仅元数据补丁') {
      headline = '只改元数据记录，Parquet 不动';
      detail = 'episodes.jsonl 的记录长度与实际行数对齐；数据文件一个字节不改。';
      count = 'Parquet 0 改动';
    }
    head = `<div class="panel cmp-head shift-head">
      <div class="cmp-title"><h2>${epName(ep)}</h2><span class="badge ${fixed ? 'b-fix' : 'b-hint'}"><i></i>${fixed ? '修了' : '没修'}</span><span class="verdict-line">${esc(headline)}</span></div>
      <p class="cmp-method">${esc(detail)}</p>
      <div class="cmp-foot">
        <span class="badge">训练帧 ${fmt(epInfo.train_frames ?? r.train_frames)}</span>
        <span class="badge">${esc(count)}</span>
        <span class="footnote">${fixed ? `复检 ${fmt(+m.score_before, 2)} → ${fmt(+m.score_after, 2)} 分` : `复检 ${fmt(+m.score_before, 2)} 分 · 保持原样`}</span>
        ${stage === 'govern' ? '<button class="ghost sm" data-notes>修复说明</button>' : ''}
      </div>
    </div>`;
  }
  main.innerHTML = head + `<div class="panel card" id="cmpViz"><div class="sub"><span class="spin"></span> 生成对比图</div></div>${fixed ? auditHtml(audit, m) : ''}`;
  $$('[data-notes]', main).forEach((b) => b.addEventListener('click', () => showNotesInStep4()));
  $$('.pair-case', main).forEach((b) => b.addEventListener('click', () => { selectCase(+b.dataset.ep); window.scrollTo({ top: 0, behavior: 'smooth' }); }));
  const viz = $('#cmpViz', main);
  try {
    if (isShift) await vizShift(viz, d, ep, fixed);
    else if (audCodes.some((c) => /NONFINITE/.test(c))) vizCells(viz, d, 'NaN 稀疏补值：只补非有限分量，其余分量逐位不变');
    else if (audCodes.includes('J_POSITION_ENVELOPE')) vizCells(viz, d, '孤立单帧越界：只改越界分量');
    else if (audCodes.some((c) => /^S_TASK/.test(c))) vizTask(viz, d);
    else if (audCodes.includes('C_SCHEMA_DTYPE_MISMATCH')) vizDtype(viz, d);
    else if (m.kind === '仅元数据补丁') vizMeta(viz, d);
    else vizRefused(viz, d);
  } catch (err) {
    viz.innerHTML = `<p class="banner">${esc(String(err))}</p>`;
  }
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
  return `<details class="audit-fold panel card"><summary><b>逐单元格修复审计</b><span>共 ${fmt(audit.length)} 格 · repair_audit.csv · 点开看原值 → 新值</span></summary>
    <div class="tbl-wrap" style="max-height:340px;margin-top:12px"><table class="tbl"><thead><tr><th>行</th><th>字段</th><th>原值</th><th>新值</th><th>方法</th></tr></thead><tbody>
    ${rows.map((a) => `<tr><td>${esc(a.row)}</td><td class="mono">${esc(a.field)}</td><td class="mono del">− ${esc(auditVal(a.original_value))}</td><td class="mono add">+ ${esc(auditVal(a.repaired_value))}</td><td>${esc(a.method)}</td></tr>`).join('')}
    </tbody></table></div>
    <dl class="kv" style="margin-top:12px"><dt>源文件 SHA256</dt><dd class="mono">${esc((m.source_sha256 || '').slice(0, 24))}… 原始文件只读未改</dd>
    ${m.repaired_sha256 ? `<dt>副本 SHA256</dt><dd class="mono">${esc(m.repaired_sha256.slice(0, 24))}… 写后逐格回读核验通过（未通过的副本会被直接丢弃）</dd>` : ''}</dl></details>`;
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
  viz.innerHTML = `<div class="card-h"><h3>同一行，修复前 vs 修复后</h3><span class="sub">左右两侧始终停在同一数据行；拖动或播放</span></div>
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
               <p>如果强行移回，就是按一个没有内容证据的偏移去改画面。</p>
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
          <div class="peak-txt">${fixed
            ? `移回前，画面运动的峰值落在 ${signed(beforePk?.lag)} 帧；驱动修复的${gate ? gate.label : ''}门控相关 r=${gate ? gate.r.toFixed(2) : '—'}（门槛 ${gate ? gate.thr.toFixed(2) : '0.80'}）；移回后峰值回到 ${signed(afterPk?.lag)} 帧。`
            : `峰值落在 ${signed(beforePk?.lag)} 帧；${gate ? gate.label : ''}门控相关只有 r=${gate ? gate.r.toFixed(2) : '—'}（门槛 ${gate ? gate.thr.toFixed(2) : '0.80'}）。偏移整齐，但内容证据不足，所以不移动。`}<br><span class="muted">曲线是相邻帧灰度差能量的互相关，用来展示时滞位置；门控数值取自 repair_manifest。</span></div>
        </div>
        <div id="vzLag"></div>
      </div>
      <div>
        <div class="mini-h">修复门控：不是看偏移猜，是看画面内容</div>
        <div class="evidence" id="vzGate"></div>
        <p class="note">必须同时满足：path 帧号恒定偏移、对端恰好缺图、本臂相关 r ≥ 0.60 或跨相机相关 r ≥ 0.80，且没有反向证据。</p>
      </div>
    </div>
    <div class="changed">
      <div class="chg-main ${fixed ? '' : 'bad'}"><b>${fmt(auditLen)}</b><span>格改写<br><small>${fixed ? `${fmt(moved)} 行移回 + ${fmt(blanked)} 行显式置空` : '没有生成修复副本'}</small></span></div>
      <div class="chg-keep"><b>${fixed ? fmt(otherDiffTotal) : '—'}</b><span>格改动<br><small>${fixed ? otherTxt : '整条保持原样'}</small></span></div>
      <div class="chg-verdict">${fixed ? `复检 ${fmt(+d.manifest.score_before, 2)} → <b>${fmt(+d.manifest.score_after, 2)}</b><small>分数只是注脚</small>` : `<b>0</b> 帧进入训练<small>${esc(d.report.train_policy || '')}</small>`}</div>
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
      : `<b>${camCn}</b>：仍显示第 ${b ? pathNum(b) : '—'} 帧。没有生成修复副本，整条保持隔离。`;
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
      <div class="chg-verdict">${sb != null ? `复检 ${fmt(sb, 2)} → <b>${fmt(sa, 2)}</b><small>分数只是注脚</small>` : ''}</div>
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
      legendExtra: '<span class="muted">底部刻度 = 原值缺失（NaN）的行；圆点 = 被改写的格</span>',
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
  viz.innerHTML = `<div class="card-h"><h3>任务标签与元数据契约不一致：按 episodes.jsonl 唯一任务重写</h3><span class="sub">${d.audit.length} 行 task_index</span></div>
    <div id="vzTask"></div>
    <dl class="kv sect">${tasks.map((t) => `<dt>任务 ${t.task_index}</dt><dd>${esc(t.task)}</dd>`).join('')}<dt>元数据记录</dt><dd>本条轨迹属于任务 ${esc(exp)}</dd></dl>`;
  lineChart($('#vzTask', viz), { n: raw.n, height: 170, series: [{ name: '修复前 task_index', values: raw.task_index, color: 'var(--bad)', width: 3 }, { name: '修复后 task_index', values: after?.task_index || [], color: 'var(--fix)' }] });
}

function vizDtype(viz, d) {
  const raw = d.raw, after = d.after;
  const same = (raw.timestamp || []).every((v, i) => v === after?.timestamp?.[i]);
  viz.innerHTML = `<div class="card-h"><h3>字段存储类型与声明不符：无损转换，数值不变</h3></div>
    <dl class="kv"><dt>info.json 声明</dt><dd>timestamp: float32</dd><dt>修复前存储</dt><dd class="mono">timestamp: ${esc(raw.dtypes?.timestamp)}</dd><dt>修复后存储</dt><dd class="mono">timestamp: ${esc(after?.dtypes?.timestamp)}</dd><dt>逐行数值</dt><dd>${same ? `${raw.n} 行全部相同` : '存在差异'}</dd></dl>
    <p class="note sect">同一轨迹的单位疑似错误（T_TIMESTAMP_UNIT_MISMATCH）没有被改写：conservative 策略不按标称周期重建时间戳，保留给下游判断。</p>
    <div id="vzTs" class="sect"></div>`;
  lineChart($('#vzTs', viz), { n: raw.n, height: 150, series: [{ name: 'timestamp', values: raw.timestamp, color: 'var(--c-t)' }] });
}

function vizMeta(viz, d) {
  const p = ((S.res[d.stage] || {}).manifest || []).find((m) => +m.episode_index === d.ep);
  viz.innerHTML = `<div class="card-h"><h3>元数据补丁：不改 Parquet，只修正记录长度</h3></div>
    <dl class="kv"><dt>实际数据行</dt><dd>${d.raw.n}</dd><dt>episodes.jsonl length</dt><dd>${esc(d.audit[0]?.original_value)} → ${esc(d.audit[0]?.repaired_value)}</dd><dt>写入位置</dt><dd class="mono">governed/meta_patches.csv · governed/meta/</dd></dl>
    <p class="note sect">${esc(p?.applied || '')}</p>`;
}

function vizRefused(viz, d) {
  const raw = d.raw;
  const rej = d.manifest?.rejected || d.report.repair_rejected || '';
  const n = raw.n || 0;
  let spec = null, title = '';
  if (/时间戳|index/.test(rej)) {
    title = '时间戳 / 帧号：缺帧不能凭空重建，不按标称周期伪造时钟';
    spec = { series: [{ name: 'Δt（秒）', values: diff(raw.timestamp), color: SER[0] }], thresholds: [{ y: 1 / (S.info.dataset.fps || 10), label: '标称周期' }] };
  } else if (/稀疏补值/.test(rej)) {
    title = '大面积 NaN：超过补值比例上限，整段不臆造';
    const bad = (raw.state || []).map((r) => (r ? r.filter((v) => v == null).length : 20));
    spec = { series: [{ name: '每行 NaN 分量数', values: bad, color: 'var(--bad)' }] };
  } else if (/越界/.test(rej)) {
    title = '位置越界：连续多帧越界不是孤立突跳，不插值';
    spec = { series: [0, 1, 2].map((k) => ({ name: `左臂 ${'xyz'[k]}`, values: col(raw.state, k), color: SER[k] })).concat([0, 1, 2].map((k) => ({ name: `右臂 ${'xyz'[k]}`, values: col(raw.state, 10 + k), color: SER[k], dash: true }))) };
  } else {
    title = '拒修';
  }
  const bands = (d.intervals || []).map((it) => ({ from: +it.start_row, to: +it.end_row, color: `var(${CAT_VAR[catOf(it.issue_code)]})`, label: it.issue_code }));
  viz.innerHTML = `<div class="card-h"><h3>${esc(title)}</h3><span class="sub">保持原样 · ${esc(d.report.train_policy)}</span></div><div id="vzRef"></div><p class="note sect">${esc(rej)}</p>`;
  if (spec && n) lineChart($('#vzRef', viz), { n, height: 200, bands, ...spec });
}

// ------------------------------------------------------------ import testing
const IMP = { sel: null, mode: 'govern', panel: null, current: null };
const isMac = () => /^macOS/.test(S.info?.host?.platform || '');

function initImport() {
  const shell = $('.run-shell[data-stage=import]');
  IMP.panel = new RunPanel('import', shell, {
    import: true, kind: 'govern',
    cfg: { name: '导入测试', desc: '', btn: '开始测试', rerun: '开始测试', phases: STAGE_CFG.govern.phases },
    onStart: impRun, onDone: () => loadHist(),
  });
  S.panels.import = IMP.panel;
  $('#impPickFile').addEventListener('click', () => (isMac() ? pickNative('file') : $('#impFileInput').click()));
  $('#impPickDir').addEventListener('click', () => (isMac() ? pickNative('folder') : $('#impDirInput').click()));
  $('#impFileInput').addEventListener('change', (e) => uploadItems([...e.target.files].map((f) => ({ file: f, rel: f.name }))));
  $('#impDirInput').addEventListener('change', (e) => uploadItems([...e.target.files].map((f) => ({ file: f, rel: f.webkitRelativePath || f.name }))));
  const drop = $('#impDrop');
  drop.addEventListener('click', () => $('#impDirInput').click());
  drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('#impDirInput').click(); } });
  ['dragenter', 'dragover'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, () => drop.classList.remove('over')));
  drop.addEventListener('drop', async (e) => {
    e.preventDefault();
    const items = await collectDrop(e.dataTransfer);
    uploadItems(items);
  });
  $('#impPath').addEventListener('keydown', (e) => { if (e.key === 'Enter') inspectPath($('#impPath').value); });
  $('#impCheck').addEventListener('click', () => inspectPath($('#impPath').value));
  $$('#impMode button').forEach((b) => b.addEventListener('click', () => {
    IMP.mode = b.dataset.m;
    $$('#impMode button').forEach((x) => x.classList.toggle('on', x === b));
  }));
  $('#impRun').addEventListener('click', impRun);
  loadHist();
}

function selMsg(html, cls = '') { $('#impSel').innerHTML = `<div class="sel-card ${cls}">${html}</div>`; }

async function pickNative(kind) {
  const prev = $('#impSel').innerHTML;
  selMsg(`<span><span class="spin"></span> 在弹出的系统窗口里选择${kind === 'file' ? '一个 Parquet 文件' : '一个文件夹'}…</span>`);
  let r;
  try { r = await api(`/api/import/pick?kind=${kind}`, { method: 'POST' }); } catch (e) { r = { ok: false, error: String(e) }; }
  if (r.canceled) { $('#impSel').innerHTML = prev; return; }
  if (!r.ok) {
    $('#impSel').innerHTML = prev;
    toast(r.error || '无法打开选择框');
    (kind === 'file' ? $('#impFileInput') : $('#impDirInput')).click();
    return;
  }
  $('#impPath').value = r.path;
  inspectPath(r.path);
}

async function inspectPath(path, extra = {}) {
  if (!path || !path.trim()) return toast('请先选择或输入路径');
  selMsg('<span><span class="spin"></span> 读取中</span>');
  const r = await api(`/api/import/inspect?path=${encodeURIComponent(path.trim())}`);
  IMP.sel = r.ok ? { ...r, ...extra } : null;
  renderSel(r);
}

function renderSel(r) {
  const run = $('#impRun');
  if (!r.ok) { run.disabled = true; return selMsg(`<b>无法测试</b><span class="warn">${esc(r.error)}</span>`, 'err'); }
  const metaLine = (m) => (m && m.episodes != null ? `${fmt(m.episodes)} 条 · ${fmt(m.frames)} 帧（元数据声明）· ${m.fps} fps · ${m.cameras?.length || 0} 路相机 · ${esc(m.format || '')}` : '');
  const up = r.uploaded ? '<span class="warn">已复制到本次测试目录</span>' : '';
  if (r.kind === 'multi') {
    run.disabled = true;
    selMsg(`<span class="k">文件夹</span><span class="big">${esc(r.name)}</span><span class="warn">这里面有 ${r.choices.length} 个数据集，选一个来测试：</span>
      ${r.choices.map((c, i) => `<button class="choice" data-i="${i}"><b>${esc(c.name)}</b><span>${fmt(c.episodes)} 条 · ${fmtBytes(c.bytes)}</span></button>`).join('')}
      <span class="p">${esc(r.path)}</span>`);
    $$('#impSel .choice').forEach((b) => b.addEventListener('click', () => inspectPath(r.choices[+b.dataset.i].path)));
    return;
  }
  run.disabled = false;
  if (r.kind === 'file') {
    selMsg(`<span class="k">单个文件</span><span class="big">${esc(r.name)}</span>
      <span>${r.readable ? `${fmt(r.rows)} 行 · ${fmtBytes(r.bytes)} · ${epName(r.ids[0])}` : `<b>文件无法读取</b>：${esc(r.read_error)}（照样可以测，算子会报告 C_FILE_UNREADABLE）`}</span>
      <span class="warn">${r.has_meta ? `所属数据集：${esc(r.root.split('/').pop())}（有 meta/，可做元数据一致性检查）` : '没有找到 meta/：元数据相关检查记为不可评估'}</span>${up}
      <span class="p">${esc(r.path)}</span>`);
  } else {
    selMsg(`<span class="k">${r.kind === 'dataset' ? 'LeRobot 数据集' : '文件夹'} · 批量测试</span><span class="big">${esc(r.name)}</span>
      <span>${fmt(r.episodes)} 个 Parquet · ${fmtBytes(r.bytes)}</span>
      ${r.has_meta ? `<span class="warn">${metaLine(r.meta)}</span>` : '<span class="warn">没有 meta/：元数据相关检查记为不可评估</span>'}${up}
      <span class="p">${esc(r.path)}</span>`);
  }
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
    selMsg(`<span><span class="spin"></span> 复制到本次测试目录 ${i + 1} / ${keep.length} · ${fmtBytes(sent)} / ${fmtBytes(total)}</span>`);
    const r = await fetch(`/api/import/upload?token=${token}&path=${encodeURIComponent(keep[i].rel)}`, { method: 'PUT', body: keep[i].file }).then((x) => x.json());
    if (!r.ok) return selMsg(`<b>复制失败</b><span class="warn">${esc(r.error)}</span>`, 'err');
    sent += keep[i].file.size;
    dir = r.dir;
    file = r.file;
  }
  const single = keep.length === 1;
  $('#impPath').value = single ? file : dir;
  inspectPath(single ? file : dir, { uploaded: true, source: single ? keep[0].rel : keep[0].rel.split('/')[0] + '/' });
}

async function impRun() {
  if (!IMP.sel || IMP.sel.kind === 'multi') return toast('请先选择要测试的数据');
  if (Object.values(S.panels).some((p) => p.state === 'running')) return toast('已有运行在进行中');
  const body = { path: IMP.sel.path, repair: IMP.mode === 'govern', source: IMP.sel.source || IMP.sel.picked || IMP.sel.path, uploaded: !!IMP.sel.uploaded };
  const r = await api('/api/import/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) {
    toast(r.error || '无法开始测试');
    if (r.inspect) { IMP.sel = r.inspect; renderSel(r.inspect); }
    return;
  }
  $('#impEmpty').hidden = true;
  $('.run-shell[data-stage=import]').hidden = false;
  $('#impRun').disabled = true;
  IMP.current = r.id;
  loadHist();
  try { await IMP.panel.runImport(r); S.impDone = true; markDone(); } finally { $('#impRun').disabled = false; }
}

async function loadHist() {
  const r = await api('/api/import/list');
  const items = r.items || [];
  const host = $('#impHist');
  if (!items.length) { host.innerHTML = '<p class="muted">还没有测试记录</p>'; return; }
  host.innerHTML = items.map((it) => {
    const tag = it.kind === 'file' ? '单个文件' : '批量';
    const mode = it.repair ? '检测 + 修复' : '只检测';
    const line = it.status === 'ok'
      ? `${fmt(it.episodes)} 条 · ${fmt(it.seconds, 2)} 秒 · 需复核 ${fmt(it.flagged)} · 训练帧 ${fmt(it.train_frames)}`
      : it.status === 'running' ? '运行中…' : `<span class="st-failed">失败：${esc(it.error || '')}</span>`;
    return `<button class="hist ${it.id === IMP.current ? 'on' : ''}" data-id="${it.id}" data-repair="${it.repair ? 1 : 0}" data-ok="${it.status === 'ok' ? 1 : 0}">
      <span class="h1"><b>${esc(it.name)}</b><span class="muted">${tag} · ${mode}</span></span>
      <span class="h2">${esc(it.created.slice(5, 16))} · ${line}</span></button>`;
  }).join('');
  $$('.hist', host).forEach((b) => b.addEventListener('click', async () => {
    if (b.dataset.ok !== '1') return;
    if (IMP.panel.state === 'running') return toast('当前测试还在运行');
    IMP.current = b.dataset.id;
    $$('.hist', host).forEach((x) => x.classList.toggle('on', x === b));
    $('#impEmpty').hidden = true;
    $('.run-shell[data-stage=import]').hidden = false;
    await IMP.panel.showRun(b.dataset.id, b.dataset.repair === '1', []);
  }));
}

// ------------------------------------------------------------ import result: one story, not a dashboard
function renderImportResult(res, box, panel) {
  const imp = res.import || {};
  const eps = res.episodes || [];
  S.impStoryToken = (S.impStoryToken || 0) + 1;
  if (!eps.length) {
    box.innerHTML = `<div class="panel empty"><h3>没有读到可分析的轨迹</h3><p>检查路径或换一个 Parquet 文件再试。</p></div>`;
    return;
  }
  if (eps.length === 1) renderImportStory(res, box, eps[0], imp, S.impStoryToken);
  else renderImportBatch(res, box, imp);
}

function importType(d, e, dict = {}) {
  const m = d.manifest || {};
  const fixed = m.kind && m.kind !== '未修改';
  const text = fixed ? (m.applied || '') : (m.rejected || '');
  const shift = shiftFromText(text);
  if (shift) return fixed ? 'shift-fixed' : 'shift-refused';
  if (fixed && m.kind === '仅元数据补丁') return 'meta';
  if (fixed) return 'cells';
  if (m.rejected) return 'refused';
  // 没有修复、也没有拒修记录：要么真的干净，要么是"没有修复手段"的问题（黑屏、模糊、马赛克…），不能当成干净
  const codes = e.final_codes?.length ? e.final_codes : (e.codes || []);
  if (codes.some((c) => dict[c]?.blocks !== false)) return 'norepair';
  if (codes.length) return 'hint';
  return 'clean';
}

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
    let cls = 'x', label = '阻断 / 缺图';
    if (m) {
      if (m.keep) { cls = m.est ? 'r' : 'k'; label = m.est ? '修复后进入训练' : '进入训练'; }
      else if (m.valid) { cls = 'v'; label = '有效但未成段'; }
    } else {
      cls = i < keep ? 'k' : 'x';
    }
    cells += `<i class="${cls}" title="第 ${i} 行 · ${label}"></i>`;
  }
  return `<div class="story-strip" aria-hidden="true">${cells}</div>`;
}

function importCellChart(host, d, audit) {
  const comps = [...new Set(audit.map((a) => a.field))].filter((f) => /^state\[\d+\]$|^actions\[\d+\]$/.test(f)).slice(0, 4);
  if (!comps.length) {
    host.innerHTML = '<p class="muted" style="padding:18px 0">这条修复不涉及 state / actions 曲线：只改标签或存储类型，其余数值逐位不变。</p>';
    return;
  }
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
      legendExtra: '<span class="muted">底部刻度 = 原值缺失的行；圆点 = 实际改写的格</span>',
    });
  }
}

async function renderImportStory(res, box, e, imp, token) {
  if (S.impTimer) { clearInterval(S.impTimer); S.impTimer = null; }
  box.innerHTML = `<div class="story">
    <div class="story-head panel">
      <div class="story-kicker">导入测试 · 单条轨迹 · ${esc(imp.created || '')}</div>
      <h2>${epName(e.id)}<span>${fmt(e.rows)} 帧${imp.name ? ' · ' + esc(imp.name) : ''}</span></h2>
      <p class="story-src">${esc(imp.source || imp.input || '')}</p>
    </div>
    <div class="panel card story-loading"><span class="spin"></span> 读取三路画面与逐帧定位…</div>
  </div>`;
  const d = await api(`/api/episode?stage=${res.stage}&ep=${e.id}`);
  if (token != null && token !== S.impStoryToken) return;
  if (!d.ok || !d.raw?.ok || !d.raw.n) {
    box.innerHTML = `<div class="story"><div class="panel empty"><h3>${epName(e.id)} 原始文件读不出画面</h3><p>${esc(d.error || d.raw?.error || '没有数据行')}</p></div></div>`;
    return;
  }
  const raw = d.raw, after = d.after, n = raw.n;
  const m = d.manifest || {};
  const fixed = m.kind && m.kind !== '未修改';
  const audit = d.audit || [];
  const codes = e.codes || [];
  const detectOnly = imp.repair === false;
  const text = fixed ? (m.applied || '') : (m.rejected || '');
  const cov = String(d.report?.sync_evidence_index_coverage || '');
  const cm = /(image|left_wrist_image|right_wrist_image):path偏移([+-]?\d+)/.exec(cov);
  const shift = shiftFromText(text) || (detectOnly && cm ? { cam: cm[1], frames: +cm[2] } : null);
  const rr = crossR(text);
  const arm = armR(text);
  const keep = Math.max(0, Math.min(n, e.train_frames || 0));
  let type = importType(d, e, res.code_dict || {});
  if (detectOnly) type = 'detect-only';
  if (type === 'cells' && !audit.length) type = 'fixed';
  const camCn = shift ? (CAM_CN[shift.cam] || shift.cam) : '';
  const off = shift?.frames ?? 0;
  const ints = d.intervals || [];
  let problem = '', action = '', evidence = [];
  if (type === 'shift-fixed') {
    const moved = (after?.paths?.[shift.cam] || []).filter(Boolean).length;
    const blank = n - moved;
    problem = `同一行里，${camCn}的画面和机械臂状态差了 ${Math.abs(off)} 帧：夹爪闭合的瞬间对不上。`;
    action = `移回 ${fmt(moved)} 帧画面；开头 ${fmt(blank)} 帧相机没拍到，显式置空，不合成任何画面。`;
    evidence = [
      ['path 偏移', `${signed(off)} 帧，整条恒定`, true],
      ['跨相机相关', `r = ${rr == null ? '—' : rr.toFixed(2)} ≥ 0.80`, rr != null && rr >= 0.8],
      ['对端缺图', `${Math.abs(off)} 帧，正好对上`, true],
    ];
  } else if (type === 'shift-refused') {
    problem = `${camCn}的画面疑似整体错了 ${Math.abs(off)} 帧，但画面内容没有给出足够的对应证据。`;
    action = '没修。整条保持原样隔离，不让伪同步画面进入训练。';
    evidence = [
      ['path 偏移', `${signed(off)} 帧（只是索引，不是内容证据）`, null],
      ['跨相机相关', `r = ${rr == null ? '—' : rr.toFixed(2)} < 0.80`, false],
      ...(arm != null ? [['本臂相关', `r = ${arm.toFixed(2)} < 0.60`, arm >= 0.6]] : []),
    ];
  } else if (type === 'detect-only') {
    if (!codes.length && !shift) {
      problem = '这条数据没有检测到阻断问题。';
      action = '本次只检测；没有需要修复的地方，整条可进入训练。';
      evidence = [['检测结果', '未命中阻断问题', true]];
    } else {
      problem = shift
        ? `同一行里，${camCn}的画面和机械臂状态差了 ${Math.abs(off)} 帧。`
        : `${codeLine({ flagged: e.flagged_frames, rows: n }, codes)}。`;
      action = '本次选择只检测，没有生成修复副本；问题段不进入训练。';
      evidence = [
        ['检测结论', codes.length ? codes.join('、') : '未命中阻断问题', !codes.length],
        ['修复', '未执行（只检测）', null],
        ...(shift ? [['path 偏移', `${signed(off)} 帧`, true]] : []),
      ];
    }
  } else if (type === 'cells') {
    const rows = [...new Set(audit.map((a) => +a.row))];
    const isVec = audit.some((a) => /^(state|actions)\[/.test(a.field));
    const dims = (raw.state?.[0]?.length || 20) + (raw.actions?.[0]?.length || 20);
    const unchanged = isVec ? n * dims - audit.length : null;
    if (codes.includes('J_STATE_NONFINITE')) {
      problem = `state 里有 ${fmt(audit.length)} 个空值（NaN/Inf）。这些格直接进训练会让损失变成 NaN。`;
      action = `只补这 ${fmt(audit.length)} 格：Rotation-6D 按正交约束解出，其余分量逐位不变。`;
    } else if (codes.includes('J_POSITION_ENVELOPE')) {
      problem = `末端位置有 ${fmt(audit.length)} 个孤立越界值。`;
      action = '只把越界分量改成前后帧均值；其余分量逐位不变。';
    } else if (codes.some((c) => /^S_TASK/.test(c))) {
      problem = '任务标签和元数据登记的不一致。';
      action = '整条统一为元数据登记的任务，只改 task_index 这一列；画面、动作、时间戳不动。';
    } else if (codes.includes('C_SCHEMA_DTYPE_MISMATCH')) {
      problem = '字段存储类型和 info.json 声明不符。';
      action = '按声明做无损转换，每个数值逐位不变。';
    } else {
      problem = `检测到 ${codes.length} 类可以安全修复的问题。`;
      action = fixLabel(m) || m.applied || '只改写检测到的单元格，其余逐位不变。';
    }
    evidence = [
      ['改写', `${fmt(audit.length)} 格（${runsText(rows)}）`, true],
      ['写后核验', '逐格回读一致', true],
      ...(unchanged != null ? [['没有动的格', `${fmt(unchanged)} 格逐位不变`, true]] : []),
    ];
  } else if (type === 'fixed') {
    problem = `检测到 ${codes.length || 1} 类问题，可以直接修复。`;
    action = fixLabel(m) || m.applied || '按审计记录逐格改写。';
    evidence = [['处理方式', m.applied || '', true]];
  } else if (type === 'meta') {
    problem = 'episodes.jsonl 记录的长度和实际行数不一致。';
    action = '只更新元数据记录，Parquet 文件不改。';
    evidence = [
      ['实际行数', `${fmt(n)} 行`, true],
      ['Parquet', '未改动', true],
    ];
  } else if (type === 'norepair') {
    const dict = res.code_dict || {};
    const blocking = codes.filter((c) => dict[c]?.blocks !== false);
    problem = `${codeLine({ flagged: e.flagged_frames, rows: n }, blocking.length ? blocking : codes)}：${fmt(e.flagged_frames)} / ${fmt(n)} 帧被标记。`;
    action = '这类问题没有安全的修复方法（不能凭空生成画面或数据），不修；被标记的帧不进入训练。';
    evidence = [
      ['检测结论', (blocking.length ? blocking : codes).join('、'), false],
      ['被标记帧', `${fmt(e.flagged_frames)} / ${fmt(n)} 帧`, false],
      ['修复', '不在可修复范围，保持原样', null],
    ];
  } else if (type === 'hint') {
    problem = `只有数据价值提示（不阻断训练）：${codes.map((c) => CODE_CN[c] || c).join('、')}。`;
    action = '数据价值提示不影响训练，不需要修复。';
    evidence = [['检测结论', codes.join('、'), null]];
  } else if (type === 'refused') {
    problem = `检测到问题，但证据不足：${reasonLabel((m.rejected || '').split('；')[0])}。`;
    action = '没修。问题段不进入训练，整条交由人工复核或隔离回采。';
    evidence = splitClausesJs(m.rejected || '').slice(0, 4).map((s) => [reasonLabel(s), s.slice(0, 140), null]);
  } else {
    problem = '这条数据没有检测到阻断问题。';
    action = '不需要修复，整条进入训练。';
    evidence = [['检测结果', '未命中阻断问题', true]];
  }
  let row = Math.min(n - 1, 25);
  if (shift) row = Math.min(n - 1, Math.max(25, Math.abs(off) + 20));
  else if (audit.length) row = Math.min(n - 1, Math.max(0, +audit[0].row));
  else if (ints.length) row = Math.min(n - 1, Math.max(0, +ints[0].start_row));
  const camFig = (c) => `<figure class="story-cam ${c === shift?.cam ? 'affected' : ''}" data-cam="${c}"><img alt=""><figcaption>${CAM_CN[c]}<em class="frame-no">—</em></figcaption></figure>`;
  const intervalHtml = ints.length ? `<div class="story-intervals">${ints.slice(0, 6).map((it) => `<div class="si"><span>${esc(it.issue_code)}</span><div class="si-bar"><i style="left:${(+it.start_row / n) * 100}%;width:${Math.max(((+it.end_row - +it.start_row + 1) / n) * 100, 1)}%;background:var(${CAT_VAR[catOf(it.issue_code)]})"></i></div><b>第 ${it.start_row}–${it.end_row} 行</b></div>`).join('')}</div>` : '';
  const evHtml = evidence.map(([k, v, pass]) => `<div class="story-ev ${pass === true ? 'pass' : pass === false ? 'fail' : ''}"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('');
  const notKeep = n - keep;
  let keepText;
  if (keep === n) keepText = `全部 ${fmt(n)} 帧都能进入训练。`;
  else if (keep === 0) keepText = '0 帧进入训练；整条交给人工复核或隔离回采。';
  else if (type === 'shift-fixed') keepText = `开头 ${fmt(n - keep)} 帧修复后没有画面，不进入训练；其余 ${fmt(keep)} 帧全部可用。`;
  else keepText = `${fmt(notKeep)} 帧不进入训练：复检后仍有阻断问题，或不足 30 帧连续。`;
  const sb = m.score_before != null ? +m.score_before : e.score;
  const sa = m.score_after != null ? +m.score_after : e.final_score;
  const scoreTxt = detectOnly ? `检测评分 ${fmt(sb, 2)} 分 · 未修复` : fixed ? `复检 ${fmt(sb, 2)} → <b>${fmt(sa, 2)}</b> 分` : `复检 ${fmt(sb, 2)} 分 · 保持原样`;
  box.innerHTML = `<div class="story">
    <header class="story-head panel">
      <div class="story-kicker">导入测试 · 单条轨迹 · ${esc(imp.created || '')}</div>
      <h2>${epName(e.id)}<span>${fmt(n)} 帧 · ${fmt(ints.length || (codes.length ? 1 : 0))} 段问题</span></h2>
      <p class="story-src">${esc(imp.source || imp.input || '')}</p>
      <div class="story-head-right"><span class="badge ${e.final_status === '通过' ? 'b-ok' : e.final_status === '价值提示' ? 'b-hint' : 'b-bad'}"><i></i>${esc(e.final_status || e.status || '')}</span><span class="badge">${esc(e.policy || '')}</span></div>
    </header>
    <section class="story-sec panel">
      <div class="story-rail">01</div>
      <div class="story-content">
        <h3>这条数据有什么病</h3>
        <p class="story-lead">${esc(problem)}</p>
        <div class="story-cams">${CAMS.map(camFig).join('')}</div>
        <div class="story-scrub"><button class="ghost sm" id="impPlay">▶ 播放</button><input type="range" id="impRow" min="0" max="${n - 1}" value="${row}"><span class="rowlab" id="impRowLab"></span></div>
        <p class="story-note" id="impCamNote"></p>
        ${intervalHtml}
      </div>
    </section>
    <section class="story-sec panel">
      <div class="story-rail">02</div>
      <div class="story-content">
        <h3>修了，还是没修？</h3>
        <p class="story-verdict ${fixed ? 'fixed' : type === 'clean' || type === 'hint' ? 'ok' : 'refused'}">${esc(action)}</p>
        ${type === 'shift-fixed' ? `<div class="sameframe">
          <figure class="sameframe-item"><img data-side="raw" alt=""><figcaption>修复前 · 第 <em class="frame-no">—</em> 帧</figcaption></figure>
          <div class="sameframe-arrow">移回 ${Math.abs(off)} 帧<br><span>同一行 →</span></div>
          <figure class="sameframe-item"><img data-side="after" alt=""><figcaption>修复后 · 第 <em class="frame-no">—</em> 帧</figcaption></figure>
        </div>` : (type === 'shift-refused' || (type === 'detect-only' && shift)) ? `<div class="sameframe one">
          <figure class="sameframe-item"><img data-side="raw" alt=""><figcaption>仍保持原样 · 第 <em class="frame-no">—</em> 帧</figcaption></figure>
          <div class="refuse-body"><div class="refuse-stamp">${type === 'detect-only' ? '只检测' : '没修'}</div><p>${type === 'detect-only' ? '没有生成修复副本。' : '证据不足，不移动画面。'}</p></div>
        </div>` : ''}
        ${type === 'cells' ? '<div class="cell-proof"><div id="impCells"></div><div class="cell-facts"><b>' + fmt(audit.length) + '</b><span>格改写</span><small>圆点与竖线 = 实际改写的格；其余曲线逐位不变</small></div></div>' : ''}
        <div class="story-evs">${evHtml}</div>
      </div>
    </section>
    <section class="story-sec panel">
      <div class="story-rail">03</div>
      <div class="story-content">
        <h3>${detectOnly ? '不修复时，能训练多少帧' : '修了哪几帧，还剩多少帧能训练'}</h3>
        <div class="story-keep">
          <div class="keep-big"><b>${fmt(keep)}</b><span>/ ${fmt(n)} 帧可训练</span></div>
          <p>${esc(keepText)}</p>
        </div>
        ${storyStrip(d, n, keep)}
        <div class="story-legend"><span><i class="k"></i>进入训练</span><span><i class="r"></i>修复后进入训练</span><span><i class="v"></i>有效但未成段</span><span><i class="x"></i>阻断 / 缺图</span></div>
        <div class="story-facts"><span>${scoreTxt}</span>${detectOnly ? '' : `<span>问题码 ${fmt(codes.length)} → <b>${fmt((e.final_codes || []).length)}</b> 种</span><span>带问题码的帧 ${fmt(e.flagged_frames)} → <b>${fmt(res.compare?.flag_rows_after ?? '—')}</b></span>`}<span>改写 ${fmt(audit.length)} 格</span><span>处理 ${fmt(e.seconds, 2)} 秒</span><span>${esc(e.policy || '')}</span></div>
        <div class="story-actions">
          ${!detectOnly && (fixed || m.rejected) ? '<button class="ghost sm" data-case>看修复前后完整对比</button>' : ''}
          ${audit.length ? '<button class="ghost sm" data-preview="governed/repair_audit.csv">查看逐格审计</button>' : ''}
          <button class="ghost sm" data-full="ep">查看完整轨迹（三路相机 + 信号曲线）</button>
        </div>
      </div>
    </section>
  </div>`;
  if (type === 'cells') importCellChart($('#impCells', box), d, audit);
  const setRow = (i) => {
    i = Math.max(0, Math.min(n - 1, i));
    $('#impRow', box).value = i;
    $$('.story-cam', box).forEach((fig) => {
      const cam = fig.dataset.cam, p = raw.paths?.[cam]?.[i];
      const img = $('img', fig);
      if (!p) { img.removeAttribute('src'); img.style.visibility = 'hidden'; }
      else { img.style.visibility = ''; img.src = `/api/frame?stage=${res.stage}&ep=${e.id}&row=${i}&cam=${cam}&src=raw`; }
      $('.frame-no', fig).textContent = p ? '第 ' + pathNum(p) + ' 帧' : '缺图';
    });
    if (type === 'shift-fixed') {
      const rfig = $('.sameframe-item img[data-side=raw]', box);
      const afig = $('.sameframe-item img[data-side=after]', box);
      const pr = raw.paths?.[shift.cam]?.[i], pa = after?.paths?.[shift.cam]?.[i];
      if (pr) rfig.src = `/api/frame?stage=${res.stage}&ep=${e.id}&row=${i}&cam=${shift.cam}&src=raw`;
      else { rfig.removeAttribute('src'); rfig.style.visibility = 'hidden'; }
      if (pa) { afig.style.visibility = ''; afig.src = `/api/frame?stage=${res.stage}&ep=${e.id}&row=${i}&cam=${shift.cam}&src=after`; }
      else { afig.removeAttribute('src'); afig.style.visibility = 'hidden'; }
      $('.sameframe-item img[data-side=raw]', box).parentElement.querySelector('.frame-no').textContent = pr ? pathNum(pr) : '缺图';
      $('.sameframe-item img[data-side=after]', box).parentElement.querySelector('.frame-no').textContent = pa ? pathNum(pa) : '缺图';
    }
    if (type === 'shift-refused' || (type === 'detect-only' && shift)) {
      const rfig = $('.sameframe-item img[data-side=raw]', box);
      const pr = raw.paths?.[shift.cam]?.[i];
      if (pr) rfig.src = `/api/frame?stage=${res.stage}&ep=${e.id}&row=${i}&cam=${shift.cam}&src=raw`;
      $('.sameframe-item .frame-no', box).textContent = pr ? pathNum(pr) : '—';
    }
    $('#impRowLab', box).textContent = `第 ${i} 行 · frame ${raw.frame_index?.[i] ?? '—'}`;
    $('#impCamNote', box).textContent = shift
      ? `橙色框 = ${CAM_CN[shift.cam]}。修复前这一行显示第 ${raw.paths?.[shift.cam]?.[i] ? pathNum(raw.paths[shift.cam][i]) : '—'} 帧${fixed ? '，修复后回到第 ' + (after?.paths?.[shift.cam]?.[i] ? pathNum(after.paths[shift.cam][i]) : '缺图') + ' 帧' : ''}。`
      : '三路相机取自同一数据行；拖动游标看问题发生的前后。';
  };
  const play = $('#impPlay', box);
  const stop = () => { if (S.impTimer) clearInterval(S.impTimer); S.impTimer = null; play.textContent = '▶ 播放'; };
  play.addEventListener('click', () => {
    if (S.impTimer) return stop();
    let i = +$('#impRow', box).value;
    if (i >= n - 1) i = 0;
    play.textContent = '❚❚ 暂停';
    S.impTimer = setInterval(() => { i += 1; if (i >= n) return stop(); setRow(i); }, 1000 / (S.info.dataset.fps || 10));
  });
  $('#impRow', box).addEventListener('input', (ev) => { stop(); setRow(+ev.target.value); });
  $$('[data-preview]', box).forEach((b) => b.addEventListener('click', () => previewFile(res.stage, b.dataset.preview)));
  $$('[data-full]', box).forEach((b) => b.addEventListener('click', () => openEpisode(res.stage, e.id, row)));
  $$('[data-case]', box).forEach((b) => b.addEventListener('click', () => openCaseModal(res.stage, e.id)));
  setRow(row);
}

function renderImportBatch(res, box, imp) {
  const eps = res.episodes || [];
  const man = res.manifest || [];
  const repair = res.kind === 'govern';
  const C = res.compare;
  const manOf = (id) => man.find((x) => +x.episode_index === id);
  const review = eps.filter((e) => e.status === '需复核').length;
  const reviewAfter = eps.filter((e) => e.final_status === '需复核').length;
  const fixed = eps.filter((e) => { const m = manOf(e.id); return m && m.kind && m.kind !== '未修改'; });
  const keep = eps.reduce((s, e) => s + (e.train_frames || 0), 0);
  const rows = eps.reduce((s, e) => s + (e.rows || 0), 0);
  const flagRows = res.detection?.flagged_frames;
  const desc = (e) => {
    const m = manOf(e.id);
    if (m) {
      if (m.kind !== '未修改') return fixLabel(m);
      if (m.rejected) return '拒修：' + reasonLabel(m.rejected.split('；')[0]);
    }
    if (!(e.codes || []).length) return '未命中问题';
    return codeLine({ flagged: e.flagged_frames, rows: e.rows }, e.codes);
  };
  const what = (e) => {
    const m = manOf(e.id);
    if (!m || m.kind === '未修改') return '';
    if (+m.values_changed > 0) return ` · 改写 ${fmt(+m.values_changed)} 格`;
    return m.kind === '仅元数据补丁' ? ' · 只改元数据' : ' · 类型转换，数值不变';
  };
  const openEp = (id) => {
    const m = manOf(id);
    if (repair && m && (m.kind !== '未修改' || m.rejected)) openCaseModal(res.stage, id);
    else openEpisode(res.stage, id);
  };
  const head = repair
    ? `${fmt(eps.length)} 条轨迹：检测出 ${fmt(review)} 条需复核，修了 ${fmt(fixed.length)} 条，复检后还剩 ${fmt(reviewAfter)} 条需复核。`
    : `${fmt(eps.length)} 条轨迹：${fmt(review)} 条需复核，${fmt(flagRows)} 行被逐帧定位。`;
  box.innerHTML = `<div class="story">
    <header class="story-head panel">
      <div class="story-kicker">导入测试 · 文件夹 · ${repair ? '检测 + 修复' : '只检测'} · ${esc(imp.created || '')}</div>
      <h2>${head}</h2>
      <p class="story-lead">${repair ? `最终 ${fmt(keep)} / ${fmt(rows)} 帧可训练。下面先看修复前后全量对比，再看每条一句话；点开看三路画面和修复前后。` : '下面是逐帧检测全景和每条一句话；点开看三路画面、信号曲线和问题区间。'}</p>
      <p class="story-src">${esc(imp.source || imp.input || '')}</p>
      ${repair ? '<div class="story-head-right"><button class="ghost sm" data-notes>本次修复说明</button></div>' : ''}
    </header>
    ${repair && C ? overallHtml(res, { compareHint: '点任一行看修复前后画面' }) : `<div class="res-grid">
        <div class="card panel"><div class="card-h"><h3>帧级质量全景：每行一条轨迹，每格一帧</h3></div>
          <div class="legend" style="margin-bottom:10px">${CATS.map((c) => `<span><i style="background:var(${CAT_VAR[c]})"></i>${c}</span>`).join('')}<span><i style="background:var(--cell)"></i>未命中</span></div>
          <div class="pano-wrap"><canvas data-pano="before"></canvas></div></div>
        <div class="col">${catCard(res)}</div>
      </div>`}
    <h3 class="batch-h">逐条结果（${fmt(eps.length)} 条）</h3>
    <div class="batch-list">${eps.map((e) => {
      const m = manOf(e.id);
      const tag = !repair ? '' : m && m.kind !== '未修改' ? '<span class="badge b-fix"><i></i>已修复</span>' : m && m.rejected ? '<span class="badge b-hint"><i></i>拒修</span>' : '';
      return `<button class="batch-case panel" data-ep="${e.id}">
        <div class="bc-head"><b>${epName(e.id)}</b><span class="badge b-${STATUS_CLS[e.status] || 'bad'}"><i></i>${esc(e.status)}</span>${repair ? `<span class="muted">→</span><span class="badge b-${STATUS_CLS[e.final_status] || 'bad'}"><i></i>${esc(e.final_status)}</span>` : ''}${tag}<span class="bc-keep">${fmt(e.train_frames)} / ${fmt(e.rows)} 帧可训练</span></div>
        <p>${esc(desc(e))}</p>
        <small>${esc(e.policy || '')}${what(e)}${repair ? ` · 质量分 ${fmt(e.score, 2)} → ${fmt(e.final_score, 2)}` : ` · 质量分 ${fmt(e.score, 2)}`}</small>
      </button>`;
    }).join('')}</div>
  </div>`;
  if (repair && C) bindOverall(box, res, openEp);
  else $$('canvas[data-pano]', box).forEach((c) => drawPanorama(c, res, 'before', res.stage));
  $$('[data-act=codes]', box).forEach((b) => b.addEventListener('click', () => showCodes(res)));
  $$('[data-notes]', box).forEach((b) => b.addEventListener('click', () => {
    openModal('<div id="mNotes"></div>');
    renderRepairNotes(res.stage, $('#mNotes'), (ep) => openCaseModal(res.stage, ep));
  }));
  $$('.batch-case', box).forEach((b) => b.addEventListener('click', () => openEp(+b.dataset.ep)));
}

function splitClausesJs(text) {
  const out = [];
  let depth = 0, cur = '';
  for (const ch of String(text || '')) {
    if (ch === '（' || ch === '(') depth++;
    if (ch === '）' || ch === ')') depth = Math.max(0, depth - 1);
    if (ch === '；' && depth === 0) { if (cur.trim()) out.push(cur.trim()); cur = ''; }
    else cur += ch;
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}

// ------------------------------------------------------------ boot
async function refreshInfo() {
  S.info = await api('/api/info');
  const rep = $('#modeSeg [data-mode=replay]');
  const any = Object.values(S.info.runs).some((r) => r && r.replayable);
  rep.disabled = !any;
  rep.title = any ? '' : '还没有实测记录，先实时运行一次';
}

(async function boot() {
  try {
    await refreshInfo();
  } catch (e) {
    document.body.innerHTML = '<p style="padding:40px">无法连接本地演示服务。请用 start_demo.command 启动，或在 demo_system 目录运行 python server.py。</p>';
    return;
  }
  for (const st of ['detect', 'govern']) S.panels[st] = new RunPanel(st, $(`.run-shell[data-stage=${st}]`));
  initImport();
  setStep(STEP.detect);
})();
