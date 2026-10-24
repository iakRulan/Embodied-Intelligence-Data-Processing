#!/usr/bin/env node
'use strict';
/* 演示文字一致性校验：页面上每一句自动生成的结论，都要和算子这次写出的结果对得上。
   用法：先启动 server.py，然后
     node tools/check_text.js [http://127.0.0.1:8765] [--stages detect,govern] [--runs runs]
   做的事：
     1. 对每个阶段、每条轨迹，拿 /api/result + /api/episode 的真实数据，跑一遍 web/logic.js 里的叙事函数
        （单条导入页 storyModel、批量页 batchDesc、实时流水 captionFor/feedFor），逐条核对：
        - 有阻断问题时，不能说"没有检测到问题 / 不需要修复 / 全部帧进入训练"，判定色不能是绿色
        - "全部 N 帧 / 0 帧 / X 帧不进入训练"这些数字和 train_frames、quality_masks 逐帧一致
        - "被标记的帧不进入训练"：所有带阻断问题码的行在 quality_masks 里确实 train_keep=False
        - 相机错位修复：置空的行数 = 偏移帧数，内容证据 r 过了门槛；拒修时没过门槛
        - 分数、改写格数和 repair_manifest / repair_audit 一致
     2. 回放事件 demo_events.jsonl：流水标签（通过 / 修复 / 拒修 / 保持 / 提示）和最终报告一致
     3. 全量对比（/api/result 的 compare）：每个数字独立重算一遍
   有问题时逐条列出并以非 0 退出。 */

const path = require('path');
const fs = require('fs');
const L = require(path.join(__dirname, '..', 'web', 'logic.js'));

const args = process.argv.slice(2);
const base = (args.find((a) => /^https?:/.test(a)) || 'http://127.0.0.1:8765').replace(/\/$/, '');
const opt = (k, d) => { const i = args.indexOf('--' + k); return i >= 0 ? args[i + 1] : d; };
const stages = opt('stages', 'detect,govern').split(',');
const runsDir = path.resolve(__dirname, '..', opt('runs', 'runs'));

const problems = [];
const bad = (stage, ep, what, detail) => problems.push({ stage, ep, what, detail });
const get = async (p) => { const r = await fetch(base + p); return r.json(); };
const nums = (s) => (String(s).replace(/<[^>]+>/g, '').match(/\d[\d,]*/g) || []).map((x) => +x.replace(/,/g, ''));
const split = (s) => String(s || '').split(';').map((x) => x.trim()).filter(Boolean);

async function checkStage(stage) {
  const res = await get(`/api/result?stage=${stage}`);
  if (!res || !res.episodes) { bad(stage, '-', '读不到结果', JSON.stringify(res).slice(0, 200)); return; }
  const dict = res.code_dict || {};
  const blocks = (c) => L.blocksTraining(c, dict);
  const govern = res.kind === 'govern';
  const man = Object.fromEntries((res.manifest || []).map((m) => [+m.episode_index, m]));
  let n_ep = 0, unreadable = 0;
  const flagRowsAfter = { n: 0 }, flagRowsBefore = { n: 0 };
  for (const e of res.episodes) {
    const d = await get(`/api/episode?stage=${stage}&ep=${e.id}`);
    if (!d.ok || !d.raw?.ok) {
      unreadable++;
      // 文件本身损坏时算子会判 C_FILE_UNREADABLE，页面显示"原始文件读不出画面"，这是预期行为
      if (!(e.codes || []).includes('C_FILE_UNREADABLE')) bad(stage, e.id, '读不到轨迹', d.error || d.raw?.error);
      continue;
    }
    n_ep++;
    const n = d.raw.n;
    const B = (what, detail) => bad(stage, e.id, what, detail);
    if (n !== e.rows) B('行数不一致', `raw.n=${n} episode_report.rows=${e.rows}`);
    const rowCodes = (d.row_codes || []).map(split);
    const rowFinal = (d.row_final_codes || []).map(split);
    const nowRows = govern ? rowFinal : rowCodes;
    flagRowsBefore.n += rowCodes.filter((x) => x.length).length;
    flagRowsAfter.n += rowFinal.filter((x) => x.length).length;
    const codesNow = govern ? (e.final_codes || []) : (e.codes || []);
    const blockingNow = codesNow.filter(blocks);
    const mask = d.mask || null;
    const keepN = mask ? mask.filter((m) => m.keep).length : null;
    if (mask && mask.length !== n) B('quality_mask 行数不对', `${mask.length} vs ${n}`);
    if (mask && keepN !== e.train_frames) B('train_frames 与 quality_mask 不一致', `report=${e.train_frames} mask=${keepN}`);
    // 带阻断码的行不可训练（算子不变量，文字里多处依赖它）
    const leak = [];
    nowRows.forEach((cs, i) => { if (cs.some(blocks) && mask && mask[i]?.keep) leak.push(i); });
    if (leak.length) B('带阻断问题码的帧进了训练', `行 ${leak.slice(0, 8).join(',')}${leak.length > 8 ? '…' : ''}（共 ${leak.length}）`);
    // 整条级阻断码（没有落到具体行）：整条都不该训练
    const rowLevel = new Set(nowRows.flat());
    const epLevelBlock = blockingNow.filter((c) => !rowLevel.has(c) && dict[c]?.scope !== 'row');
    if (epLevelBlock.length && e.train_frames > 0) B('整条级阻断问题但仍有帧进训练', `${epLevelBlock.join(',')} train=${e.train_frames}`);

    // ---------- 单条叙事（导入页）
    const imp = { repair: govern };
    let M;
    try { M = L.storyModel({ e, d, imp, dict }); } catch (err) { B('storyModel 抛错', String(err.stack || err).slice(0, 300)); continue; }
    const all = [M.problem, M.action, M.keepText, ...M.evidence.map((x) => x.join(' '))].join(' | ');
    const m = d.manifest || {};
    const fixed = !!(m.kind && m.kind !== '未修改');
    if (blockingNow.length && !fixed) {
      if (/没有检测到问题|不需要修复|不影响训练|整条进入训练/.test(M.problem + M.action)) B('有阻断问题却说没问题', `${blockingNow.join(',')} → ${M.problem} ${M.action}`);
      if (M.verdictCls === 'ok') B('有阻断问题判定色却是绿色', `${M.type} ${blockingNow.join(',')}`);
    }
    if (!codesNow.length && !(e.codes || []).length && M.verdictCls !== 'ok' && !fixed) B('没有问题码判定色却不是绿色', `${M.type}`);
    if (fixed && !govern) B('只检测阶段出现修复', m.kind);
    // 还剩多少帧
    const keep = e.train_frames;
    if (/^全部/.test(M.keepText) && keep !== n) B('说全部进训练但不是', `train=${keep}/${n} · ${M.keepText}`);
    if (/^0 帧/.test(M.keepText) && keep !== 0) B('说 0 帧进训练但不是', `train=${keep}`);
    if (!/^全部|^0 帧/.test(M.keepText)) {
      const xs = nums(M.keepText);
      if (M.type === 'shift-fixed' && M.blankRows.length) {
        const sum = xs.reduce((a, b) => a + b, 0);
        if (sum !== n) B('相机修复后帧数加起来不等于总帧数', `${xs.join('+')}=${sum} ≠ ${n} · ${M.keepText}`);
        if (xs[xs.length - 1] !== keep) B('可用帧数不对', `${M.keepText} vs train=${keep}`);
        const blankOut = M.blankRows.filter((i) => !(mask && mask[i]?.keep)).length;
        if (xs[0] !== blankOut) B('置空帧数不对', `${xs[0]} vs ${blankOut}`);
      } else if (xs[0] !== n - keep) B('不进训练的帧数不对', `${M.keepText} vs ${n - keep}`);
    }
    if (/被标记的帧不进入训练|带阻断问题的帧不进入训练|问题段不进入训练/.test(all) && leak.length) B('文字说问题帧不进训练，实际进了', `${leak.length} 帧`);
    // 被标记帧数
    if (M.type === 'norepair') {
      const flagged = nowRows.filter((x) => x.length).length;
      const said = nums(M.problem);
      if (!said.includes(e.flagged_frames)) B('norepair 被标记帧数没写对', M.problem);
      if (govern && flagged !== e.flagged_frames) B('修复后被标记帧数与修复前不同，但页面说没修', `${e.flagged_frames} → ${flagged}`);
      if (!blockingNow.length) B('norepair 却没有阻断码', codesNow.join(','));
    }
    if (M.type === 'hint' && blockingNow.length) B('说只有提示，其实有阻断码', blockingNow.join(','));
    // 相机错位
    if (M.type === 'shift-fixed') {
      const k = M.blankRows.length;
      if (k !== Math.abs(M.off)) B('置空行数 ≠ 偏移帧数', `blank=${k} off=${M.off}`);
      const g = M.evidence[1];
      if (g && g[2] === false) B('相机修复了，但门槛证据显示未通过', g.join(' '));
      if (!d.after) B('相机修复了，但没有修复副本', '');
    }
    if (M.type === 'shift-refused') {
      const fails = M.evidence.filter((x) => x[2] === true && /相关/.test(x[0]));
      const cr = M.evidence.find((x) => /跨相机/.test(x[0]));
      if (cr && cr[2] === true) B('拒修了，但跨相机证据显示通过', cr.join(' '));
      void fails;
    }
    if (M.type === 'cells' || M.type === 'fixed') {
      const changed = +m.values_changed || 0;
      const cellRows = d.audit.filter((a) => +a.row >= 0).length;
      if (M.type === 'cells' && cellRows !== changed) B('审计格数与 manifest 不一致', `audit=${cellRows} manifest=${changed}`);
      const said = nums(M.evidence[0]?.[1] || '')[0];
      if (M.type === 'cells' && said !== changed) B('改写格数写错', `${M.evidence[0]?.[1]} vs ${changed}`);
    }
    if (M.type === 'dtype' && +m.values_changed) B('类型转换说数值不变，但 manifest 有数值改动', m.values_changed);
    // 修了一部分：没修的部分必须讲出来
    if (fixed && govern) {
      const left = L.remainAfterFix({ finalCodes: e.final_codes, rowFinal: d.row_final_codes, blankRows: M.blankRows, dict, shift: M.shift });
      if (left.length && !/没有修/.test(M.action)) B('修了一部分，没修的部分没讲', `${left.join(',')} · ${M.action}`);
      if (left.length && M.verdictCls !== 'partial') B('修了一部分，判定色却是"已修复"', left.join(','));
      if (!left.length && keep < n && M.type !== 'shift-fixed') B('修复后没有阻断问题，却有帧不进训练', `train=${keep}/${n} final=${(e.final_codes || []).join(',')}`);
    }
    // 分数
    if (govern) {
      const sb = m.score_before !== '' && m.score_before != null ? +m.score_before : e.score;
      const sa = m.score_after !== '' && m.score_after != null ? +m.score_after : e.final_score;
      const xs = String(M.scoreTxt).replace(/<[^>]+>/g, '').match(/\d+\.\d+/g) || [];
      if (Math.abs(+xs[0] - sb) > 0.006) B('修复前分数写错', `${M.scoreTxt} vs ${sb}`);
      if (fixed && Math.abs(+xs[1] - sa) > 0.006) B('修复后分数写错', `${M.scoreTxt} vs ${sa}`);
      if (Math.abs(sb - e.score) > 0.006) B('manifest 修复前分数 ≠ episode_report', `${sb} vs ${e.score}`);
      if (Math.abs(sa - e.final_score) > 0.006) B('manifest 修复后分数 ≠ episode_report', `${sa} vs ${e.final_score}`);
    }
    // ---------- 批量页一句话
    const desc = L.batchDesc(e, govern ? man[e.id] : null);
    if (/未命中问题/.test(desc) && (e.codes || []).length) B('批量页说未命中，其实有问题码', `${desc} · ${(e.codes || []).join(',')}`);
    if (govern && man[e.id] && !fixed && !man[e.id].rejected && blockingNow.length && /修复|移回|改正|补值/.test(desc)) B('批量页说修了，其实没修', desc);
    e._d = { leak: leak.length, n, mask: !!mask };
  }
  // ---------- 回放流水
  const evFile = /^imp-/.test(stage) ? path.join(runsDir, 'imports', stage, 'out', 'demo_events.jsonl') : path.join(runsDir, stage, 'demo_events.jsonl');
  if (fs.existsSync(evFile)) {
    const byId = Object.fromEntries(res.episodes.map((e) => [e.id, e]));
    const evs = fs.readFileSync(evFile, 'utf-8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
    const epEvs = evs.filter((x) => x.type === 'episode');
    if (epEvs.length !== res.episodes.length) bad(stage, '-', '回放事件条数 ≠ 报告条数', `${epEvs.length} vs ${res.episodes.length}`);
    const kind = govern ? 'govern' : 'detect';
    for (const ev of epEvs) {
      const e = byId[ev.ep];
      const B = (what, detail) => bad(stage, ev.ep, '[流水] ' + what, detail);
      if (!e) { B('报告里没有这条', ''); continue; }
      const f = L.feedFor(ev, kind);
      const c = L.captionFor(ev, kind);
      const m = man[ev.ep] || {};
      const fixed = !!(m.kind && m.kind !== '未修改');
      // 回放事件是算子逐条处理时发出的；数据集级关系（重复/重叠）在全部处理完后才加上，所以只核对阻断问题
      const evNow = govern ? (ev.final_codes || []) : (ev.codes || []);
      const repNow = govern ? (e.final_codes || []) : (e.codes || []);
      const evB = evNow.filter(blocks).sort().join(','), repB = repNow.filter(blocks).sort().join(',');
      if (evB !== repB) B('事件里的阻断码 ≠ 最终报告', `${evB || '无'} vs ${repB || '无'}`);
      if (f.tag === '通过' && repNow.some(blocks)) B('流水显示通过，最终有阻断问题', repNow.filter(blocks).join(','));
      if (f.tag === '通过' && !govern && (e.codes || []).length) {
        const extra = (e.codes || []).filter((x) => !(ev.codes || []).includes(x));
        if (extra.some(blocks)) B('流水显示未命中，最终有阻断问题', extra.join(','));
      }
      if (govern) {
        if (f.tag === '修复' && !fixed) B('流水显示修复，manifest 未修改', m.kind);
        if (fixed && f.tag !== '修复') B('manifest 修复了，流水没显示修复', f.tag);
        if (f.tag === '拒修' && !m.rejected) B('流水显示拒修，manifest 没有拒修记录', '');
        if (/问题帧不进入训练|问题段不进入训练/.test(c.meta) && e._d?.leak) B('流水说问题帧不进训练，实际进了', `${e._d.leak} 帧`);
        if (f.tag === '保持' && !repNow.some(blocks)) B('流水显示保持（有阻断），其实没有阻断码', repNow.join(','));
        if (fixed && +ev.cells !== +(m.values_changed || 0)) B('流水改写格数 ≠ manifest', `${ev.cells} vs ${m.values_changed}`);
        if (fixed) {
          const left = (e.final_codes || []).filter(blocks).filter((x) => !/^S_SENSOR_(LATE_START|EARLY_STOP)$/.test(x) || !L.captionFor(ev, kind).text.includes('移回'));
          if (left.length && !/仍有/.test(c.meta)) B('流水只说修了，没说还有阻断问题没修', `${left.join(',')} · ${c.meta}`);
          if (/改写 \d+ 格/.test(c.meta) && !+(m.values_changed || 0)) B('流水说改写了单元格，实际 0 个数值改动', c.meta);
        }
      }
      if (ev.status !== e.status && !(e.codes || []).some((x) => /^V_EPISODE_(DUPLICATE|OVERLAP)/.test(x))) B('流水状态 ≠ 最终状态', `${ev.status} vs ${e.status}`);
    }
    const fin = evs.find((x) => x.type === 'final');
    if (!fin) { if (!/^imp-/.test(stage)) bad(stage, '-', '回放缺少 final 事件（实时计数会和最终报告对不上）', ''); }
    else {
      const st = {};
      for (const e of res.episodes) st[e.status] = (st[e.status] || 0) + 1;
      const fst = {};
      for (const e of res.episodes) fst[e.final_status] = (fst[e.final_status] || 0) + 1;
      if (JSON.stringify(sortObj(fin.st)) !== JSON.stringify(sortObj(st))) bad(stage, '-', 'final 事件状态计数 ≠ 报告', `${JSON.stringify(fin.st)} vs ${JSON.stringify(st)}`);
      if (govern && JSON.stringify(sortObj(fin.fst)) !== JSON.stringify(sortObj(fst))) bad(stage, '-', 'final 事件复检状态计数 ≠ 报告', `${JSON.stringify(fin.fst)} vs ${JSON.stringify(fst)}`);
    }
  } else bad(stage, '-', '没有回放事件文件', evFile);
  // ---------- 全量对比
  const C = res.compare;
  if (govern) {
    if (!C) bad(stage, '-', '修复阶段没有全量对比数据', '');
    else {
      const cnt = (k) => res.episodes.reduce((o, e) => ((o[e[k]] = (o[e[k]] || 0) + 1), o), {});
      const eq = (what, a, b) => { if (JSON.stringify(a) !== JSON.stringify(b)) bad(stage, '-', '[全量对比] ' + what, `${JSON.stringify(a)} vs 重算 ${JSON.stringify(b)}`); };
      eq('修复前状态', sortObj(C.status_before), sortObj(cnt('status')));
      eq('修复后状态', sortObj(C.status_after), sortObj(cnt('final_status')));
      if (!unreadable || unreadable === res.episodes.filter((e) => (e.codes || []).includes('C_FILE_UNREADABLE')).length) {
        if (unreadable < res.episodes.length) {
          eq('修复前带码帧', C.flag_rows_before, flagRowsBefore.n);
          eq('修复后带码帧', C.flag_rows_after, flagRowsAfter.n);
        }
      }
      eq('可训练帧（修复后）', C.train_total, res.episodes.reduce((s, e) => s + e.train_frames, 0));
      const mean = (k) => +(res.episodes.reduce((s, e) => s + (e[k] || 0), 0) / res.episodes.length).toFixed(2);
      if (Math.abs(C.score_mean_before - mean('score')) > 0.01) eq('修复前平均分', C.score_mean_before, mean('score'));
      if (Math.abs(C.score_mean_after - mean('final_score')) > 0.01) eq('修复后平均分', C.score_mean_after, mean('final_score'));
      const changed = res.episodes.filter((e) => { const m = man[e.id]; return m && m.kind && m.kind !== '未修改'; }).map((e) => e.id).sort((a, b) => a - b);
      const shown = (C.changed || []).map((x) => x.ep).sort((a, b) => a - b);
      eq('有变化的轨迹', shown, changed);
      for (const x of C.changed || []) {
        const e = res.episodes.find((y) => y.id === x.ep);
        if (x.train_frames !== e.train_frames) eq(`ep${x.ep} 训练帧`, x.train_frames, e.train_frames);
        const gone = (e.codes || []).filter((c) => !(e.final_codes || []).includes(c)).sort();
        eq(`ep${x.ep} 已消除问题码`, [...x.resolved].sort(), gone);
      }
      if (C.train_base != null && C.train_detect_only != null && C.train_detect_only < C.train_base) bad(stage, '-', '[全量对比] 只检测+切段 少于 原本可用', `${C.train_detect_only} < ${C.train_base}`);
      if (C.train_total < (C.train_detect_only ?? 0)) bad(stage, '-', '[全量对比] 修复后可训练帧比只检测还少', `${C.train_total} < ${C.train_detect_only}`);
    }
  }
  console.log(`· ${stage}: 核对 ${n_ep} 条轨迹${unreadable ? `（${unreadable} 条原始文件读不出，跳过画面相关检查）` : ''}`);
}

function sortObj(o) { return Object.fromEntries(Object.entries(o || {}).sort()); }

(async () => {
  const list = [...stages];
  if (args.includes('--imports')) {
    const r = await get('/api/import/list');
    for (const x of r.items || r.imports || r || []) if (x && x.id && x.ok !== false) list.push(x.id);
  }
  for (const s of list) await checkStage(s);
  if (!problems.length) { console.log('✓ 全部一致'); return; }
  const groups = {};
  for (const p of problems) (groups[p.what] = groups[p.what] || []).push(p);
  for (const [w, ps] of Object.entries(groups)) {
    console.log(`\n✗ ${w}（${ps.length}）`);
    for (const p of ps.slice(0, 12)) console.log(`   ${p.stage} ep${p.ep}: ${p.detail}`);
    if (ps.length > 12) console.log(`   … 另有 ${ps.length - 12} 条`);
  }
  process.exitCode = 1;
})();
