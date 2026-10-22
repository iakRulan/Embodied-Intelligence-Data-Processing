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

const S = {
  step: 1,
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
  $$('.view').forEach((v) => v.classList.toggle('on', +v.dataset.view === n));
  $$('#steps button').forEach((b) => b.classList.toggle('on', +b.dataset.step === n));
  closeOverlays();
  if (n === 4) renderCompare();
  window.scrollTo(0, 0);
}
$$('#steps button').forEach((b) => b.addEventListener('click', () => setStep(+b.dataset.step)));
function markDone() {
  $$('#steps button').forEach((b) => {
    const st = +b.dataset.step;
    b.classList.toggle('done', (st === 2 && !!S.res.detect) || (st === 3 && !!S.res.govern));
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
  if (e.key === 'ArrowRight') setStep(Math.min(5, S.step + 1));
  else if (e.key === 'ArrowLeft') setStep(Math.max(1, S.step - 1));
  else if (/^[1-5]$/.test(e.key)) setStep(+e.key);
  else if (e.key === 't' || e.key === 'T') $('#themeBtn').click();
  else if (e.key === 'f' || e.key === 'F') { if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen?.(); }
  else if (e.key === ' ' || e.key === 'Enter') {
    const p = S.step === 2 ? S.panels.detect : S.step === 3 ? S.panels.govern : null;
    if (p && p.state !== 'running') { e.preventDefault(); p.start(); }
    if (S.step === 1) { e.preventDefault(); $('#goDetect').click(); }
    if (S.step === 5 && !$('#impRun').disabled) { e.preventDefault(); impRun(); }
  }
});

function hwLabel(t) {
  if (!t || !t.hardware) return '';
  const p = t.hardware.platform || '';
  const os = p.startsWith('macOS') ? 'macOS' : p.startsWith('Linux') ? 'Linux' : p.startsWith('Windows') ? 'Windows' : p.split('-')[0];
  const arch = /arm64|aarch64/.test(p) ? 'ARM' : /x86_64|AMD64/.test(p) ? 'x86' : '';
  return `${os} ${arch} · ${t.hardware.cpu_count} 核 · ${t.workers} 进程`;
}

// ------------------------------------------------------------ overview
function renderOverview() {
  const { dataset: ds, official: of, platform: pf, hero } = S.info;
  const ep = hero?.episode ?? 6, row = hero?.row ?? 80;
  $$('#hero img').forEach((img) => { img.src = `/api/frame?stage=detect&ep=${ep}&row=${row}&cam=${img.dataset.cam}&src=raw`; });
  const task = ds.tasks?.length ? ds.tasks.map((t) => t.task.split(':')[0]).join(' / ') : '';
  $('#heroCap').textContent = `${epName(ep)} 第 ${row} 帧的三路同步画面 · 双臂人形机器人光伏板搬运装配任务（${task}）`;
  $('#dsTitle').textContent = ds.label;
  $('#dsFigs').innerHTML = [
    [fmt(ds.episodes_files), '条轨迹'],
    [fmt(ds.meta_frames), '帧（元数据声明）'],
    [fmtBytes(ds.bytes), '原始数据'],
  ].map(([v, l]) => `<div class="fig"><b>${v}</b><span>${l}</span></div>`).join('');
  const cams = ds.cameras.map((c) => c.shape?.slice(0, 2).join('×')).filter(Boolean)[0] || '';
  $('#dsSpec').textContent = `${ds.cameras.length} 路相机 ${cams} · state / actions 各 ${ds.state_dim} 维（双臂末端位置、Rotation-6D 姿态、夹爪）· ${ds.fps} fps · ${ds.tasks.length} 个任务 · ${ds.format}`;
  const flow = [
    ['读取', '只读挂载'], ['特征提取', '图像只解码一次'], ['三层证据检测', '确定性 / 高置信 / 阈值'], ['五维评分', '0–100 分'],
    ['安全修复', '写副本 + 回读核验', 1], ['复检', '修复后重新检测', 1], ['切段交付', '连续 ≥30 帧', 1],
  ];
  $('#flow').innerHTML = flow.map(([a, b, g]) => `<li class="${g ? 'gov' : ''}">${a}<em>${b}</em></li>`).join('');
  $('#goDetect').onclick = () => { setStep(2); S.panels.detect.start(); };

  let html = `<div class="off-head"><h3>${esc(of.title)}</h3><span class="muted">${esc(of.team)}</span></div>
    <div class="off-total"><b class="num">${of.total.toFixed(2)}</b><span>总分</span></div>
    <div class="off-acc"><div><b class="num">${pct(of.accuracy, 2)}</b><span>检测准确率</span></div><div><b class="num">${of.correct} / ${of.count}</b><span>正确 / 总数</span></div></div>
    <div class="score-rows">${of.items.map(([n, v, m]) => `<div class="sr"><span>${esc(n)}</span><span class="v">${v.toFixed(2)}<small> / ${m}</small></span><div class="bar"><i style="width:${(v / m) * 100}%"></i></div></div>`).join('')}</div>
    <p class="quote">${esc(of.comment)}</p>`;
  if (pf) {
    html += `<div class="plat"><span>大衍平台统一测试实跑：<b>${fmt(pf.timing?.total_seconds_incl_all_csv_parquet_outputs, 2)} 秒</b>处理 ${pf.timing?.episodes} 条 / ${fmt(pf.timing?.rows)} 行，处理异常 <b>${pf.errors}</b> 项</span>
      <span>平台数据：需复核 ${pf.detection?.flagged_episodes} 条，帧级定位 ${fmt(pf.detection?.flagged_frames)} 行，可训练帧 ${fmt(pf.governance?.train_frames?.total)}</span>
      <span class="muted">阈值指纹 ${esc((pf.thresholds_sha256 || '').slice(0, 8))}…，与现场运行一致；平台数据为复赛数据集，与本机公开测试集不是同一份</span></div>`;
  }
  $('#official').innerHTML = `<div style="display:grid;gap:16px">${html}</div><div class="muted" style="font-size:12px">${esc(of.source)}</div>`;
}

// ------------------------------------------------------------ run panel
const STAGE_CFG = {
  detect: {
    name: '质量检测',
    desc: '逐轨迹、逐帧定位时序、同步、内容结构、数据价值四类问题，按证据强弱分级，给出 0–100 五维评分与问题区间。只读运行，不生成修复副本。',
    btn: '开始检测', rerun: '重新检测',
    phases: [['episodes', '逐轨迹特征提取与三层证据检测'], ['dedup', '跨轨迹重复关系'], ['outputs', '切段、掩膜与报告写出'], ['summary', '汇总报告']],
  },
  govern: {
    name: '安全治理',
    desc: '证据充分才修：改写只写副本、原始数据只读，副本写后逐单元格回读核验，修复后重新检测；证据不足则拒修隔离。最后切出连续可训练片段。',
    btn: '开始治理', rerun: '重新治理',
    phases: [['episodes', '检测 → 修复 → 回读核验 → 复检'], ['dedup', '跨轨迹去重'], ['outputs', '治理决策、切段与审计写出'], ['summary', '汇总报告']],
  },
};

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
    $('.tiles-legend', host).innerHTML = [['--ok', '通过'], ['--warn', '仅价值提示'], ['--bad', '需复核'], ['--cell-empty', '待处理']]
      .map(([v, l]) => `<span><i style="background:var(${v});border:1px solid var(--line-2)"></i>${l}</span>`).join('')
      + (this.kind === 'govern' || opts.import ? '<span><i class="dot" style="background:var(--fix)"></i>已生成修复副本</span>' : '');
    this.setPhases(this.cfg.phases);
    this.raf = null;
    if (opts.import) {
      host.classList.add('imp-panel');
      this.buildTiles([]);
      return;
    }
    this.buildTiles(Math.max(S.info.dataset.episodes_files || 0, S.info.dataset.meta_episodes || 0) || 89);
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

  setPhases(phases) {
    $('.phases', this.el).innerHTML = phases.map(([k, l]) => `<li data-k="${k}"><span>${l}</span><span></span></li>`).join('');
  }

  buildTiles(n) {
    const ids = Array.isArray(n) ? n : Array.from({ length: n }, (_, i) => i);
    this.n = ids.length;
    $('.pn', this.el).textContent = ids.length;
    $('.board-head h3', this.el).textContent = `${ids.length} 条轨迹处理状态`;
    $('.tiles', this.el).classList.toggle('few', ids.length <= 6);
    $('.tiles', this.el).innerHTML = ids.map((i) => `<div class="tile" data-ep="${i}">${i}</div>`).join('');
    $$('.tile', this.el).forEach((t) => {
      t.addEventListener('mousemove', (e) => { if (t._tip) showTip(t._tip, e.clientX, e.clientY); });
      t.addEventListener('mouseleave', hideTip);
      t.addEventListener('click', () => { if (this.state === 'done') openEpisode(this.stage, +t.dataset.ep); });
    });
  }

  reset() {
    this.events = [];
    this.k = 0;
    this.anchorT = 0;
    this.anchorAt = performance.now();
    this.lastT = 0;
    this.images = 0;
    this.rows = 0;
    this.workers = null;
    $$('.tile', this.el).forEach((t) => { t.className = 'tile'; t._tip = ''; t.innerHTML = t.dataset.ep; });
    $('.console', this.el).innerHTML = '';
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
    this.live = { st: { 需复核: 0, 通过: 0, 价值提示: 0 }, cats: { 时序: 0, 同步: 0, 内容结构: 0, 数据价值: 0 }, fixed: 0, cells: 0, rejected: 0, flagged: 0, codes: new Set(), secs: 0 };
    this.renderLive();
  }

  renderLive() {
    const L = this.live;
    const n = this.n || 89;
    const st = [['需复核', '--bad'], ['通过', '--ok'], ['价值提示', '--warn']].map(([k, v]) => `<div><span>${k}</span><b><i style="background:var(${v})"></i>${L.st[k]}</b></div>`).join('');
    const extra = this.kind === 'govern'
      ? [['修复副本', L.fixed], ['改写单元格', fmt(L.cells)], ['证据不足拒修', L.rejected]]
      : [['帧级定位行', L.flaggedKnown === false ? '—' : fmt(L.flagged)], ['命中问题码种类', L.codes.size], ['平均每条耗时', L.k ? (L.secs / L.k).toFixed(2) + ' s' : '—']];
    $('.live-status', this.el).innerHTML = st + extra.map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join('');
    $('.live-cats', this.el).innerHTML = '<div class="mini-h">四类问题涉及轨迹（实时累计）</div>' + CATS.map((c) => `<div class="hb"><span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:var(${CAT_VAR[c]});margin-right:6px"></i>${c}</span><div class="t"><i style="width:${(L.cats[c] / n) * 100}%;background:var(${CAT_VAR[c]})"></i></div><span class="v">${L.cats[c]}<small> 条</small></span></div>`).join('');
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
    this.kind = r.repair ? 'govern' : 'detect';
    this.setPhases(STAGE_CFG[this.kind].phases);
    this.buildTiles(r.ids || []);
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
    await sleep(700);
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
        this.images = ev.images_total;
        this.rows = ev.rows_total;
        $('.pk', this.el).textContent = ev.k;
        $('.prog-bar i', this.el).style.width = (ev.k / ev.total) * 100 + '%';
        $('.c-rows', this.el).textContent = fmt(ev.rows_total);
        $('.c-img', this.el).textContent = fmt(ev.images_total);
        if (ev.t > 0.5) $('.c-ips', this.el).textContent = fmt(ev.images_total / ev.t) + ' 张/秒';
        const st = this.kind === 'govern' ? ev.final_status : ev.status;
        this.paintTile(ev.ep, st, ev.repaired || ev.meta_patch, ev.error, true, ev);
        const L = this.live;
        L.k = ev.k;
        L.st[st] = (L.st[st] || 0) + 1;
        for (const c of new Set((ev.codes || []).map(catOf))) L.cats[c] += 1;
        (ev.codes || []).forEach((c) => L.codes.add(c));
        L.fixed += ev.repaired || ev.meta_patch ? 1 : 0;
        L.cells += ev.cells || 0;
        L.rejected += ev.rejected || 0;
        if (ev.flagged == null) L.flaggedKnown = false;
        L.flagged += ev.flagged || 0;
        L.secs += ev.seconds || 0;
        this.renderLive();
        if (this.kind === 'govern' && (ev.repaired || ev.meta_patch)) {
          this.log(`${epName(ev.ep)} ${ev.repaired ? '修复副本已写出并回读核验' : '元数据补丁'} · 改写 ${ev.cells} 格 · 复检 ${fmt(ev.score, 1)} → ${fmt(ev.final_score, 1)} 分`);
        } else if (ev.error) {
          this.log(`${epName(ev.ep)} 处理异常，已记录并继续批次`, 'err');
        }
        break;
      }
      case 'log':
        if (/^\[detect\]/.test(ev.msg)) break;
        this.log(ev.msg.replace(/^\[done\] /, '完成：'));
        break;
      case 'done':
        this.timing = ev.timing;
        $('.clock-v', this.el).textContent = (ev.timing?.total_seconds_incl_all_csv_parquet_outputs ?? ev.t).toFixed(2);
        $('.clock-sub', this.el).textContent = `算子计时（读取 + 检测${this.kind === 'govern' ? ' + 修复 + 复检' : ''} + 全部文件写出）· ${hwLabel(ev.timing)}`;
        $$('.phases li', this.el).forEach((li) => { if (li.className === 'run') li.className = 'end'; });
        break;
      case 'error':
        this.err = ev.msg;
        this.log(ev.msg, 'err');
        break;
      default:
    }
  }

  paintTile(ep, status, fixed, error, flash, ev) {
    const t = $(`.tile[data-ep="${ep}"]`, this.el);
    if (!t) return;
    t.className = 'tile s-' + (error ? 'err' : STATUS_CLS[status] || 'bad') + (fixed ? ' fixed' : '') + (flash ? ' flash' : '');
    if (ev) {
      const codes = (this.kind === 'govern' ? ev.final_codes : ev.codes) || [];
      t.innerHTML = `${ep}<small>${fmt(ev.rows)} 行</small>`;
      t._tip = `<b>${epName(ep)}</b> · ${esc(status)} · ${fmt(this.kind === 'govern' ? ev.final_score : ev.score, 1)} 分 · ${fmt(ev.seconds, 2)} s<br>`
        + (codes.length ? codes.map((c) => `<div class="tt-row"><i style="background:var(${CAT_VAR[catOf(c)]})"></i>${esc(c)}</div>`).join('') : '未命中问题码');
    }
  }

  async showSaved() {
    this.reset();
    await this.loadResult(false);
  }

  async showRun(id, repair, ids) {
    this.stage = id;
    this.kind = repair ? 'govern' : 'detect';
    this.setPhases(STAGE_CFG[this.kind].phases);
    this.buildTiles(ids || []);
    this.events = [];
    this.reset();
    await this.loadResult(false);
  }

  async loadResult(animate) {
    const res = await api(`/api/result?stage=${this.stage}`);
    if (!res.ok) return toast(res.error);
    S.res[this.stage] = res;
    if (this.opts.import && (!this.n || this.n !== res.episodes.length)) this.buildTiles(res.episodes.map((e) => e.id));
    markDone();
    this.state = 'done';
    this.btn.textContent = this.cfg.rerun;
    if (this.savedBtn) this.savedBtn.hidden = true;
    // tiles reflect the saved result
    for (const e of res.episodes) {
      const st = this.kind === 'govern' ? e.final_status : e.status;
      this.paintTile(e.id, st, e.repaired || (this.kind === 'govern' && (res.manifest || []).some((m) => +m.episode_index === e.id && m.kind === '仅元数据补丁')), false, false,
        { codes: e.codes, final_codes: e.final_codes, score: e.score, final_score: e.final_score, rows: e.rows, seconds: e.seconds });
    }
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
    if (this.kind === 'detect') renderDetectResult(res, box, this);
    else renderGovernResult(res, box, this);
    if (this.opts.onResult) this.opts.onResult(res, box);
    box.hidden = false;
    $('.run-body', this.el).hidden = true;
    if (animate) box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

// ------------------------------------------------------------ result pieces
function kpi(label, value, sub, lead) {
  return `<div class="kpi panel ${lead ? 'lead' : ''}"><div class="k-l">${label}</div><div class="k-v">${value}</div><div class="k-s">${sub || ''}</div></div>`;
}

function speedKpi(res) {
  const t = res.timing || {};
  return kpi('总用时', `${fmt(t.total_seconds_incl_all_csv_parquet_outputs, 2)}<small>秒</small>`,
    `${fmt(t.episodes_per_second, 2)} 条/秒 · ${fmt(t.decoded_images_per_second)} 张图像/秒<br>${hwLabel(t)}`, true);
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
  box.innerHTML = `<div class="kpis">
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
    ${res.stage === 'detect' ? '<div class="cta-row"><button class="cta" data-goto="3">下一步：安全治理</button><span class="hint">检测只读，原始数据未被改动；治理会在此基础上写修复副本</span></div>' : ''}`;
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
  box.innerHTML = `<div class="kpis">
      ${speedKpi(res)}
      ${kpi('修复副本', `${g.parquet_repaired_copies}<small>+ ${g.meta_patch_only}</small>`, `${g.parquet_repaired_copies} 个 Parquet 副本 + ${g.meta_patch_only} 个元数据补丁；${g.flagged_resolved?.total ?? 0} 条复检后不再需复核`)}
      ${kpi('改写单元格', `${fmt(g.values_changed)}<small>格</small>`, '每一格写后回读核验，并登记原值 → 新值')}
      ${kpi('证据不足 · 拒修', `${fmt(g.repairs_rejected)}<small>项</small>`, `另有 ${g.repair_candidates} 项候选修复未执行，全部写明原因`)}
      ${kpi('可训练帧', `${fmt(total)}<small>帧</small>`, base ? `比全盘丢弃多 ${fmt(total - base)} 帧（+${((total / base - 1) * 100).toFixed(1)}%）` : '')}
    </div>
    <div class="res-grid">
      <div class="col">
        <div class="card panel"><div class="card-h"><h3>可训练数据：三种处理方式对比</h3><span class="sub">同一批 ${res.scope.episodes} 条轨迹 · ${fmt(res.scope.rows)} 行</span></div>
          <div class="tier">${tiers.map(([n, s, v, c]) => `<div class="tier-row"><div class="lab"><b>${n}</b>${s}</div><div class="t"><i data-w="${(v / maxV) * 100}" style="width:0;background:${c}"></i></div><div class="v">${fmt(v)}<small>${base && v !== base ? '+' + ((v / base - 1) * 100).toFixed(1) + '%' : '基线'}</small></div></div>`).join('')}</div>
          <div class="sect"><div class="mini-h">最终 ${fmt(total)} 帧的来源</div>
            <div class="stack">${comp.map(([k, c]) => `<i style="width:${((tf[k] || 0) / total) * 100}%;background:${c}"></i>`).join('')}</div>
            <div class="stack-legend">${comp.map(([k, c]) => `<span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${c};margin-right:5px"></i>${k} <b>${fmt(tf[k] || 0)}</b></span>`).join('')}</div>
          </div>
        </div>
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
          <div class="files-foot"><span>拒修的轨迹保持原样，交由隔离回采或人工复核</span>${res.stage === 'govern' ? '<button class="ghost sm" data-goto="4">看修复说明</button>' : ''}</div>
        </div>
        ${filesCard(res, res.stage, ['governed/'], '生成物：治理叠加层', ['report'])}
      </div>
    </div>
    ${res.stage === 'govern' ? '<div class="cta-row"><button class="cta" data-goto="4">下一步：修复说明与前后对比</button><span class="hint">每一条修复的问题、做法、依据和注意事项都集中在一页</span></div>' : ''}`;
  requestAnimationFrame(() => $$('.tier-row .t i', box).forEach((i) => (i.style.width = i.dataset.w + '%')));
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
    for (const c of e.codes) {
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
      const s = (mode === 'after' ? res.mask_after : res.mask_before)[e.id] || '';
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
      let html = `<b>${epName(e.id)}</b> · ${fmt(e.rows)} 行 · ${esc(canvas._mode === 'after' ? e.policy : e.status)} · ${fmt(canvas._mode === 'after' ? e.final_score : e.score, 1)} 分`;
      if (inRow && e.rows) {
        html += `<br>第 ${row} 行`;
        const s = (canvas._mode === 'after' ? canvas._res.mask_after : canvas._res.mask_before)[e.id] || '';
        if (canvas._mode === 'after') html += ' · ' + ({ k: '进入训练', r: '修复后进入训练', v: '有效但未成段', x: '阻断 / 隔离', d: '重复剔除' }[s[row]] || '');
        const codes = e.codes.filter((c) => {
          const b = parseInt(s[row], 16);
          return canvas._mode !== 'after' && !Number.isNaN(b) && (b & CAT_BIT[catOf(c)]);
        });
        if (codes.length) html += '<br>' + codes.map((c) => `<div class="tt-row"><i style="background:var(${CAT_VAR[catOf(c)]})"></i>${esc(c)}</div>`).join('');
      }
      const lvl = e.codes.filter((c) => canvas._res.code_dict?.[c]?.scope === '轨迹级');
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
  const head = `<div class="ep-head"><h2 id="drawerTitle">${epName(ep)}</h2>
    <span class="badge b-${STATUS_CLS[r.status] || 'bad'}"><i></i>${esc(r.status)}</span>
    <span class="badge">${fmt(+r.overall_score, 1)} 分</span>
    <span class="badge">${fmt(+r.rows)} 行</span>
    <span class="badge">${res?.kind === 'detect' ? '不修复时：' : ''}${esc(r.train_policy)} · 训练帧 ${fmt(+r.train_frames)}</span>
    ${res?.kind === 'govern' && r.repaired_file ? '<span class="badge b-fix"><i></i>已生成修复副本</span>' : ''}</div>`;
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
        <div class="sect" style="margin-top:0"><h4>五维评分</h4><div class="dims">${dims.map((k) => { const v = +r[k + '_score']; return `<div class="dim"><span>${DIM_CN[k]}</span><div class="t"><i style="width:${Number.isFinite(v) ? v : 0}%"></i></div><span class="v">${Number.isFinite(v) ? fmt(v, 1) : '—'}</span></div>`; }).join('')}</div></div>
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

const CAM_CN = { image: '场景相机', left_wrist_image: '左腕相机', right_wrist_image: '右腕相机' };
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
  return `<div class="panel empty"><h3>${what}需要先完成安全治理</h3><p>回到第 3 步运行一次治理，或切换到回放模式。</p><button class="cta" data-goto="3">去安全治理</button></div>`;
}

async function renderCompare() {
  const list = $('#cmpList'), main = $('#cmpMain');
  const res = await ensureGovern();
  if (!res) {
    list.hidden = true;
    main.innerHTML = needGovernHtml('修复前后对比');
    $$('[data-goto]', main).forEach((b) => b.addEventListener('click', () => setStep(3)));
    return;
  }
  list.hidden = false;
  if (S.cmpFor === res.created && S.cmpEp != null) return;
  S.cmpFor = res.created;
  const man = res.manifest || [];
  const fixed = man.filter((m) => m.kind !== '未修改');
  const refusedAll = man.filter((m) => m.kind === '未修改' && m.rejected);
  // 拒修：把图像重对齐类排在前面，其余按原因去重展示
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
  list.innerHTML = `<button class="case case-notes" data-ep="notes"><b>修复说明</b><span class="sc">${fixed.length} 条</span><span class="d">每一条修复的问题、做法、依据与注意事项，以及所有未修复的原因</span></button>`
    + `<h4>已修复 · ${fixed.length} 条</h4>${fixed.map((m) => item(m, 'fix')).join('')}<h4>证据不足 · 拒修示例</h4>${refused.map((m) => item(m, 'ref')).join('')}`;
  $$('.case', list).forEach((b) => b.addEventListener('click', () => (b.dataset.ep === 'notes' ? showNotesInStep4() : selectCase(+b.dataset.ep))));
  showNotesInStep4();
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
  const kindBadge = fixed ? `<span class="badge b-fix"><i></i>${esc(m.kind)}</span>` : '<span class="badge b-hint"><i></i>拒修 · 保持原样</span>';
  main.innerHTML = `<div class="panel cmp-head">
      <div class="cmp-title"><h2>${epName(ep)}</h2>${kindBadge}<span class="badge">${esc(r.train_policy)}</span><span class="badge">训练帧 ${fmt(+r.train_frames)}</span></div>
      <p class="cmp-method">${esc(fixed ? m.applied : m.rejected)}</p>
      <div class="ba">
        <div class="ba-side"><div class="l">修复前 · ${esc(m.status_before || r.status)}</div><div class="s">${fmt(+m.score_before, 2)}<small class="muted" style="font-size:15px"> 分</small></div><div class="codes">${before.map((c) => `<span class="code ${gone.includes(c) ? 'gone' : ''}"><i style="background:var(${CAT_VAR[catOf(c)]})"></i>${esc(c)}</span>`).join('') || '<span class="muted">无</span>'}</div></div>
        <div class="ba-arrow">→</div>
        <div class="ba-side"><div class="l">${fixed ? '修复后复检' : '保持原样'} · ${esc(m.status_after || r.final_status)}</div><div class="s">${fmt(+m.score_after, 2)}<small class="muted" style="font-size:15px"> 分</small></div><div class="codes">${after.map((c) => `<span class="code ${before.includes(c) ? '' : 'new'}"><i style="background:var(${CAT_VAR[catOf(c)]})"></i>${esc(c)}</span>`).join('') || '<span class="muted">问题码全部消除</span>'}</div></div>
      </div>
    </div>
    <div class="panel card" id="cmpViz"><div class="sub"><span class="spin"></span> 生成对比图</div></div>
    ${fixed ? auditHtml(audit, m) : ''}`;
  const viz = $('#cmpViz', main);
  try {
    if (audCodes.includes('S_STREAM_SHIFTED') || (!fixed && /image 重对齐|图像/.test(m.rejected || ''))) await vizShift(viz, d, ep, fixed);
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
  return `<div class="panel card"><div class="card-h"><h3>逐单元格修复审计</h3><span class="sub">共 ${fmt(audit.length)} 格 · repair_audit.csv</span></div>
    <div class="tbl-wrap" style="max-height:340px"><table class="tbl"><thead><tr><th>行</th><th>字段</th><th>原值</th><th>新值</th><th>方法</th></tr></thead><tbody>
    ${rows.map((a) => `<tr><td>${esc(a.row)}</td><td class="mono">${esc(a.field)}</td><td class="mono del">− ${esc(auditVal(a.original_value))}</td><td class="mono add">+ ${esc(auditVal(a.repaired_value))}</td><td>${esc(a.method)}</td></tr>`).join('')}
    </tbody></table></div>
    <dl class="kv" style="margin-top:12px"><dt>源文件 SHA256</dt><dd class="mono">${esc((m.source_sha256 || '').slice(0, 24))}… 原始文件只读未改</dd>
    ${m.repaired_sha256 ? `<dt>副本 SHA256</dt><dd class="mono">${esc(m.repaired_sha256.slice(0, 24))}… 写后逐格回读核验通过（未通过的副本会被直接丢弃）</dd>` : ''}</dl></div>`;
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
  if (!cam) { const mm = /(image|left_wrist_image|right_wrist_image) 重对齐/.exec(d.manifest?.rejected || ''); cam = mm ? mm[1] : 'image'; }
  const camCn = CAM_CN[cam];
  const refs = cam === 'image' ? ['left_wrist_image', 'right_wrist_image'] : [cam === 'left_wrist_image' ? 'right_wrist_image' : 'left_wrist_image'];
  const gates = parseGates(fixed ? d.manifest?.applied : d.manifest?.rejected);
  const pnBefore = (raw.paths?.[cam] || []).map(pathNum);
  const pnAfter = after ? (after.paths?.[cam] || []).map(pathNum) : null;
  const offB = pnBefore.map((p, i) => (p == null ? null : p - i));
  const offA = pnAfter ? pnAfter.map((p, i) => (p == null ? null : p - i)) : null;
  const fin = offB.filter((v) => v != null).sort((a, b) => a - b);
  const off = fin.length ? fin[Math.floor(fin.length / 2)] : 0;
  const L = Math.max(12, Math.abs(off) + 4);
  const eRaw = mo.raw?.energy || {}, eAft = mo.after?.energy || {};
  const curves = [];
  const peakTxt = [];
  const peak = (arr) => { let k = -1, best = -2; arr.forEach((v, i) => { if (v != null && v > best) { best = v; k = i; } }); return k < 0 ? null : { lag: k - L, r: best }; };
  refs.forEach((rc, i) => {
    const b = lagCorr(eRaw[cam] || [], eRaw[rc] || [], L);
    curves.push({ name: `修复前 · 参照${CAM_CN[rc]}`, values: b, color: 'var(--bad)', dash: i > 0 });
    const pb = peak(b);
    if (pb) peakTxt.push(`修复前 vs ${CAM_CN[rc]}：峰值在 ${pb.lag > 0 ? '+' : ''}${pb.lag} 帧（r = ${pb.r.toFixed(2)}）`);
    if (fixed && eAft[cam]) {
      const a = lagCorr(eAft[cam], eAft[rc] || [], L);
      curves.push({ name: `修复后 · 参照${CAM_CN[rc]}`, values: a, color: 'var(--fix)', dash: i > 0 });
      const pa = peak(a);
      if (pa) peakTxt.push(`修复后：峰值在 ${pa.lag > 0 ? '+' : ''}${pa.lag} 帧（r = ${pa.r.toFixed(2)}）`);
    }
  });
  viz.innerHTML = `<div class="card-h"><h3>${camCn}${fixed ? '整体错位：证据充分，移回真实画面' : '疑似错位：内容证据不足，拒绝移动画面'}</h3><span class="sub">只移动已有图像字节，空出的端点显式置空</span></div>
    <div class="two">
      <div><div class="mini-h">① 图像文件帧号 − 数据行号：path 记录的偏移为 ${off >= 0 ? '+' : ''}${off} 帧</div><div id="vzPath"></div></div>
      <div><div class="mini-h">② 门控证据（算子统计量，来自 repair_manifest）</div><div class="evidence" id="vzGate"></div></div>
    </div>
    <div class="two sect">
      <div><div class="mini-h">③ 跨相机画面运动互相关 vs 时滞</div><div id="vzLag"></div><p class="note">${peakTxt.map(esc).join('<br>')}<br>演示可视化：相邻帧灰度差能量的相关系数，仅用于直观展示时滞位置。</p></div>
      <div><div class="mini-h">④ ${fixed ? '修复后' : '原始数据'}：${camCn}与${CAM_CN[refs[0]]}的画面运动（归一化）</div><div id="vzTS"></div>${fixed ? '' : `<p class="note">整条轨迹进入「${esc(d.report.train_policy)}」，不让伪同步数据进入训练。</p>`}</div>
    </div>
    <div class="sect"><div class="mini-h">⑤ 同一数据行的画面：${fixed ? '修复前 / 修复后' : '原始数据'}（拖动选择行，右下角为图像文件帧号）</div>
      <div class="scrub"><input type="range" id="vzRow" min="0" max="${n - 1}" value="${Math.min(Math.abs(off) + 20, n - 1)}"><span class="rowlab" id="vzRowLab"></span></div>
      <div class="strip" id="vzStrip"></div></div>`;
  lineChart($('#vzPath', viz), {
    n, height: 170, yMin: Math.min(-1, off - 2), yMax: Math.max(1, off + 2),
    series: [{ name: '修复前', values: offB, color: 'var(--bad)' }, ...(offA ? [{ name: '修复后', values: offA, color: 'var(--fix)' }] : [])],
    thresholds: [{ y: 0, label: '对齐 = 0' }],
    legendExtra: offA ? '<span class="muted">修复后开头缺失的几行 = 显式置空的缺图</span>' : '',
  });
  $('#vzGate', viz).innerHTML = gates.length ? gates.map((g) => {
    const pass = g.r >= g.thr;
    return `<div class="gate"><span>${esc(g.label)}</span><b>r = ${g.r.toFixed(3)} ${pass ? '≥' : '<'} ${g.thr}${g.gain != null ? ` · 增益 ${g.gain.toFixed(3)}` : ''}</b>
      <div class="meter2"><i style="width:${Math.min(Math.max(g.r, 0), 1) * 100}%;background:var(${pass ? '--ok' : '--bad'})"></i><u style="left:${g.thr * 100}%"></u></div></div>`;
  }).join('') + '<p class="note">移动画面需同时满足：path 帧号恒定偏移 k、对端恰好缺 |k| 帧、本臂运动相关 r ≥ 0.6 或跨相机相关 r ≥ 0.8，且无反向矛盾证据。</p>'
    : '<p class="note">门控证据见上方说明。</p>';
  lineChart($('#vzLag', viz), {
    n: 2 * L + 1, height: 170, xFmt: (i) => { const v = i - L; return (v > 0 ? '+' : '') + v; }, xName: '时滞',
    xTicks: Array.from({ length: 2 * L + 1 }, (_, i) => i).filter((i) => (i - L) % 5 === 0),
    series: curves, vlines: [{ x: L, label: '0' }, ...(off ? [{ x: L + off, label: `path ${off > 0 ? '+' : ''}${off}` }] : [])],
  });
  const src = fixed && eAft[cam] ? eAft : eRaw;
  lineChart($('#vzTS', viz), { n, height: 170, yMin: 0, yMax: 1, series: [{ name: camCn, values: norm01(src[cam] || []), color: 'var(--c-s)' }, { name: CAM_CN[refs[0]], values: norm01(src[refs[0]] || []), color: 'var(--c-t)' }] });
  const strip = $('#vzStrip', viz);
  const setRow = (i) => {
    const rows = [0, 1, 2, 3, 4, 5].map((k) => Math.min(n - 1, Math.max(0, i - 2 + k)));
    $('#vzRowLab', viz).textContent = `第 ${rows[0]}–${rows[5]} 行`;
    const cell = (s, c, r) => {
      const p = (s === 'after' ? after?.paths?.[c]?.[r] : raw.paths?.[c]?.[r]);
      return `<div class="cam ${p ? '' : 'missing'}">${p ? `<img src="/api/frame?stage=${d.stage}&ep=${ep}&row=${r}&cam=${c}&src=${s}" alt="">` : ''}<span>第 ${r} 行</span><em>${p ? esc(String(pathNum(p))) : '空'}</em></div>`;
    };
    const refCam = refs[0];
    strip.innerHTML = `<span class="rl">参照：${CAM_CN[refCam]}</span>${rows.map((r) => cell('raw', refCam, r)).join('')}`
      + `<span class="rl">${fixed ? '修复前' : '原始'}：${camCn}</span>${rows.map((r) => cell('raw', cam, r)).join('')}`
      + (fixed ? `<span class="rl">修复后：${camCn}</span>${rows.map((r) => cell('after', cam, r)).join('')}` : '');
  };
  $('#vzRow', viz).addEventListener('input', (e) => setRow(+e.target.value));
  setRow(+$('#vzRow', viz).value);
}

function vizCells(viz, d, title) {
  const raw = d.raw, after = d.after;
  const n = raw.n;
  const comps = [...new Set(d.audit.map((a) => a.field).filter((f) => /^state\[\d+\]$|^actions\[\d+\]$/.test(f)))];
  const rowsBy = {};
  for (const a of d.audit) (rowsBy[a.field] = rowsBy[a.field] || []).push(+a.row);
  const label = (f) => {
    const k = +/\[(\d+)\]/.exec(f)[1];
    const arm = k < 10 ? '左臂' : '右臂', j = k % 10;
    const part = j < 3 ? `末端位置 ${'xyz'[j]}` : j < 9 ? `Rotation-6D 第 ${j - 2} 分量` : '夹爪';
    return `${f.split('[')[0]}[${k}] ${arm}${part}`;
  };
  const rotArms = [...new Set(comps.map((f) => +/\[(\d+)\]/.exec(f)[1]).filter((k) => k % 10 >= 3 && k % 10 < 9).map((k) => (k < 10 ? 'left' : 'right')))];
  viz.innerHTML = `<div class="card-h"><h3>${esc(title)}</h3><span class="sub">${comps.length} 个分量 · ${d.audit.length} 格</span></div>
    <div class="two" id="vzCells"></div>
    ${rotArms.length ? '<div class="sect"><div class="mini-h">修复后 Rotation-6D 正交误差（对数坐标）：解出的分量严格满足单位正交约束</div><div id="vzRot"></div></div>' : ''}`;
  const host = $('#vzCells', viz);
  comps.slice(0, 6).forEach((f, idx) => {
    const k = +/\[(\d+)\]/.exec(f)[1];
    const fld = f.split('[')[0] === 'actions' ? 'actions' : 'state';
    const div = document.createElement('div');
    div.innerHTML = `<div class="mini-h">${esc(label(f))}</div><div></div>`;
    host.appendChild(div);
    const B = col(raw[fld], k), A = after ? col(after[fld], k) : B;
    const rr = rowsBy[f] || [];
    lineChart(div.lastElementChild, {
      n, height: 150,
      series: [
        { name: '修复前', values: B, color: 'var(--ink-3)', width: 3, opacity: 0.8, xmarks: rr.filter((i) => B[i] == null) },
        { name: '修复后', values: A, color: 'var(--fix)', markers: rr, width: 1.5 },
      ],
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
    onStart: impRun, onDone: () => loadHist(), onResult: impExtras,
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
  try { await IMP.panel.runImport(r); } finally { $('#impRun').disabled = false; }
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

function impExtras(res, box) {
  const imp = res.import || {};
  const extra = document.createElement('div');
  extra.className = 'imp-extra';
  const one = res.episodes.length === 1 ? res.episodes[0] : null;
  const man = one && (res.manifest || []).find((m) => +m.episode_index === one.id);
  const hasCase = man && (man.kind !== '未修改' || man.rejected);
  extra.innerHTML = `<div class="panel card"><div class="src-line">
      <span>测试来源</span><code>${esc(imp.source || imp.input || '')}</code>
      <span>${imp.kind === 'file' ? '单个文件' : '批量'} · ${imp.repair ? '检测 + 修复' : '只检测'} · ${esc(imp.created || '')}</span>
      ${one ? `<button class="ghost sm" data-one="ep">查看这条轨迹的画面与问题区间</button>` : ''}
      ${hasCase ? `<button class="ghost sm" data-one="case">查看修复前后</button>` : ''}
    </div></div>
    ${res.kind === 'govern' ? '<div class="imp-notes"></div>' : ''}`;
  box.appendChild(extra);
  $('[data-one=ep]', extra)?.addEventListener('click', () => openEpisode(res.stage, one.id));
  $('[data-one=case]', extra)?.addEventListener('click', () => openCaseModal(res.stage, one.id));
  if (res.kind === 'govern') renderRepairNotes(res.stage, $('.imp-notes', extra), (ep) => openCaseModal(res.stage, ep));
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
  renderOverview();
  for (const st of ['detect', 'govern']) S.panels[st] = new RunPanel(st, $(`.run-shell[data-stage=${st}]`));
  initImport();
  setStep(1);
})();
