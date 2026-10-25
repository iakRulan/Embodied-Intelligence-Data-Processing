'use strict';
/* RefSync-QA 演示 · 纯逻辑（不碰 DOM）：页面上所有自动生成的结论文字都在这里，
   浏览器里由 index.html 先于 app.js 加载；tools/check_text.js 在 Node 里加载它，对全部轨迹逐条校验文字与算子结果一致。 */

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// 非数字（null、''、'—'、NaN、Infinity）一律显示 —，不会出现 NaN
const fmt = (n, d = 0) => { const v = typeof n === 'number' ? n : n == null || n === '' ? NaN : Number(n); return Number.isFinite(v) ? v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }) : '—'; };

const pct = (x, d = 1) => (x == null || !Number.isFinite(+x) ? '—' : (x * 100).toFixed(d) + '%');

const epName = (e) => 'ep' + String(e).padStart(2, '0');

const CATS = ['时序', '同步', '内容结构', '数据价值'];

const CAT_VAR = { 时序: '--c-t', 同步: '--c-s', 内容结构: '--c-c', 数据价值: '--c-v' };

const CAT_BIT = { 时序: 1, 同步: 2, 内容结构: 4, 数据价值: 8 };

const catOf = (code) => ({ T: '时序', S: '同步', C: '内容结构', J: '内容结构', V: '数据价值' }[code[0]] || '内容结构');

const STATUS_CLS = { 通过: 'ok', 价值提示: 'hint', 需复核: 'bad' };

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
    if (!codes.some((x) => blocksTraining(x))) return { tag: '提示', cls: 'hint', text, meta: c.meta, chips };
    return { tag: '保持', cls: 'keep', text, meta: c.meta, chips };
  }
  if (!codes.length) return { tag: '通过', cls: 'ok', text: '未命中问题', meta: `${fmt(ev.rows)} 行 · ${fmt(ev.seconds, 2)} 秒`, chips: '' };
  return { tag: ev.status === '价值提示' ? '提示' : '需复核', cls: ev.status === '价值提示' ? 'hint' : 'bad', text, meta: c.meta, chips };
}

const pathNum = (p) => { const m = /(\d+)\.\w+$/.exec(p || ''); return m ? +m[1] : null; };

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
  C_IMAGE_MOSAIC: '画面成块马赛克（有效分辨率过低）',
  C_IMAGE_NOISE: '画面噪声 / 花屏',
  C_IMAGE_EXPOSURE: '曝光异常（过曝或欠曝）',
  C_IMAGE_COLOR: '色彩通道异常（疑似红蓝互换）',
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
  if (/^时间戳/.test(r)) return '时间戳偏差不能无损还原';
  if (/稀疏补值/.test(r)) return '空值比例超过补值上限';
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
      const done = ev.meta_patch && !ev.repaired ? 'Parquet 不改' : ev.cells ? `改写 ${fmt(ev.cells)} 格` : '数值逐位不变';
      const left = finalCodes.filter((c) => blocksTraining(c)).filter((c) => !(shift && BLANK_CODES.has(c)));
      return { text: `${ep} · ${what}`, meta: [done, left.length ? `仍有 ${left.length} 类问题没修，相关帧不进入训练` : ''].filter(Boolean).join(' · ') };
    }
    if (b.rejected || ev.rejected) {
      const r = crossR(b.rejected);
      const why = reasonLabel((b.rejected || '').split('；')[0]);
      const s = shift ? `${CAM_CN[shift.cam] || shift.cam}疑似错位 ${signed(shift.frames)} 帧` : why;
      const evidence = shift ? (r != null ? `跨相机相关只有 ${r.toFixed(2)}，低于 0.80` : '画面内容证据不足') : '证据不足';
      return { text: `${ep} · ${s} → 拒修`, meta: `${evidence} · 问题段不进入训练` };
    }
    if (!finalCodes.length) return { text: `${ep} · 通过`, meta: `${fmt(ev.rows)} 帧，无问题` };
    if (!finalCodes.some((c) => blocksTraining(c))) return { text: `${ep} · ${codeLine(ev, finalCodes, shift)}`, meta: '只有提示，不影响训练' };
    return { text: `${ep} · ${codeLine(ev, finalCodes, shift)}`, meta: '没有修复方法 · 问题帧不进入训练' };
  }
  if (!codes.length) return { text: `${ep} · 未命中问题`, meta: `${fmt(ev.rows)} 帧 · ${fmt(ev.seconds, 2)} 秒` };
  const marked = ev.flagged ? `${fmt(ev.flagged)} / ${fmt(ev.rows)} 帧有问题` : '整条级问题';
  return { text: `${ep} · ${codeLine(ev, codes, shift)}`, meta: `${marked} · ${fmt(ev.seconds, 2)} 秒` };
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


// 不阻断训练的问题码（与算子 config.CODES 中 blocks_training=False 一致）；有问题码字典时以字典为准
const NONBLOCK = new Set(['C_SCHEMA_DTYPE_MISMATCH', 'T_HEAD_FRAMES_MISSING', 'T_METADATA_LENGTH_MISMATCH', 'S_CAMERA_LAG_SUSPECT',
  'V_STATE_FREEZE', 'V_ACTION_FREEZE', 'V_LOW_INFORMATION', 'V_HIGH_IDLE', 'V_LOW_INTERACTION', 'V_LOW_VISUAL_DIVERSITY',
  'V_EPISODE_DUPLICATE', 'V_EPISODE_OVERLAP']);
function blocksTraining(code, dict) {
  const m = dict && dict[code];
  if (m && typeof m.blocks === 'boolean') return m.blocks;
  return !NONBLOCK.has(code);
}

const codeCn = (c, dict) => CODE_CN[c] || dict?.[c]?.description || c;
// 叙事页用：把问题逐个叫出名字（最多 3 个），而不是"另有 N 类问题"
function codesText(codes, dict, max = 3) {
  const xs = [...new Set(codes || [])];
  const names = xs.slice(0, max).map((c) => codeCn(c, dict));
  return names.join('、') + (xs.length > max ? ` 等 ${xs.length} 类问题` : '');
}

// 修复之后仍然阻断训练、而且不是"修复本身置空的缺图行"造成的问题码：页面必须说清楚它们没修
const BLANK_CODES = new Set(['S_SENSOR_LATE_START', 'S_SENSOR_EARLY_STOP']);
function remainAfterFix({ finalCodes, rowFinal, blankRows, dict, shift }) {
  const blank = new Set(blankRows || []);
  const rowsOf = {};
  (rowFinal || []).forEach((s, i) => String(s || '').split(';').filter(Boolean).forEach((c) => (rowsOf[c] = rowsOf[c] || []).push(i)));
  return (finalCodes || []).filter((c) => blocksTraining(c, dict)).filter((c) => {
    if (!shift || !BLANK_CODES.has(c)) return true;
    const rs = rowsOf[c] || [];
    return !(rs.length && rs.every((i) => blank.has(i)));
  });
}

// 画面类问题：落在哪一路相机、有多严重。模糊给出实测清晰度与判定下界（下界 = 参考集 0.2% 分位 × 0.5，见算子 calibrate.py）
const IMAGE_CODE_CN = { C_IMAGE_BLUR: '模糊', C_IMAGE_MOSAIC: '马赛克', C_IMAGE_SCREEN: '黑屏 / 白屏 / 低对比度', C_IMAGE_DUPLICATE: '相邻帧画面重复', C_IMAGE_SHAPE: '分辨率或通道数不符',
  C_IMAGE_NOISE: '噪声 / 花屏', C_IMAGE_EXPOSURE: '曝光异常', C_IMAGE_COLOR: '色彩通道异常' };
// 算子在 issue_details 里按相机写出的实测值（噪声强度 / 平均亮度 / 偏暖占比 与参考集界限），页面直接引用
const MEASURED_CODES = new Set(['C_IMAGE_NOISE', 'C_IMAGE_EXPOSURE', 'C_IMAGE_COLOR']);
function detailsByCode(text) {
  const out = {};
  for (const m of String(text || '').matchAll(/([A-Z]_[A-Z0-9_]+)\(([^()]*(?:\([^()]*\)[^()]*)*)\)/g)) out[m[1]] = m[2];
  return out;
}
function camMeasurements(details, code) {
  return String(details[code] || '').split('；').map((s) => s.trim())
    .map((s) => /^(image|left_wrist_image|right_wrist_image) (.+)$/.exec(s)).filter(Boolean).map((m) => [m[1], m[2]]);
}
function imageNotes(d, codes, dict) {
  const ie = d.image_evidence || {};
  const cams = new Set(), sents = [], evidence = [];
  const det = detailsByCode(d.report?.issue_details);
  let mild = false;
  for (const c of codes || []) {
    if (MEASURED_CODES.has(c)) for (const [k, v] of camMeasurements(det, c)) evidence.push([`${CAM_CN[k] || k} · ${IMAGE_CODE_CN[c]}`, v, false]);
    const x = ie[c];
    if (!x || !x.cams || !x.cams.length) continue;
    x.cams.forEach((k) => cams.add(k));
    const parts = x.cams.length === 1 ? CAM_CN[x.cams[0]] : x.cams.map((k) => `${CAM_CN[k] || k}（${fmt(x.frames?.[k] || 0)} 帧）`).join('、');
    const others = CAMS.filter((k) => !x.cams.includes(k)).map((k) => CAM_CN[k]);
    const only = (codes || []).filter((y) => ie[y]?.cams?.length).length === 1 && (codes || []).length === 1;
    sents.push(`${only ? '' : IMAGE_CODE_CN[c] || codeCn(c, dict)}${others.length ? `只出现在${parts}，${others.join('、')}没有这个问题` : `三路相机都有：${parts}`}`);
    if (c !== 'C_IMAGE_BLUR' || !x.sharp) continue;
    for (const k of x.cams) {
      const sp = x.sharp[k];
      if (!sp || !sp.lap_min) continue;
      const cam = CAM_CN[k] || k;
      if (sp.hit_bytes > 0 && sp.hit_bytes >= Math.max(sp.hit_both, sp.hit_lap4)) {
        evidence.push([`${cam}图像文件`, `中位 ${fmt(sp.bytes / 1024, 1)} KB，不到参考集下界的 1/5（${fmt((0.2 * sp.byte_min) / 1024, 1)} KB）：压缩过度、细节丢失`, false]);
        continue;
      }
      const q = sp.lap_min / 0.5;
      const r = sp.lap / q;
      evidence.push([`${cam}清晰度`, `中位 ${fmt(sp.lap)} < 判定下界 ${fmt(sp.lap_min)}（参考集 99.8% 的帧 ≥ ${fmt(q)}，下界取一半）`, false]);
      const lvl = r >= 0.25 ? '轻度：整体看得清，放大后边缘发虚' : r >= 0.1 ? '中度：细节明显变软' : '重度：画面糊成一片';
      if (r >= 0.25) mild = true;
      evidence.push(['模糊程度', `${lvl}（清晰度约为参考集最不清晰那 0.2% 帧的 ${Math.round(r * 100)}%）`, null]);
    }
  }
  return { cams: [...cams], sentence: sents.join('；'), evidence, mild };
}

// 没有自动修复的阻断问题：画面类大多无法修；红蓝通道互换能换回来，但原始通道顺序无法确认，算子只写候选修复（repair.py）
function noRepairNote(blocking, dict) {
  const color = (blocking || []).includes('C_IMAGE_COLOR');
  const others = (blocking || []).filter((c) => c !== 'C_IMAGE_COLOR');
  if (!color) return { line: '没有安全的修复方法', action: '没有安全的修复方法（不能凭空生成画面或数据），不修；被标记的帧不进入训练。', cand: '' };
  const swap = '色彩通道可以整路交换红蓝换回来，但原始通道顺序无法确认，只列为候选修复、不自动改';
  return {
    line: others.length ? '没有自动修复' : '只列为候选修复',
    action: `${others.length ? `${codesText(others, dict)}没有安全的修复方法；` : ''}${swap}；被标记的帧不进入训练。`,
    cand: '整路交换红蓝通道（R↔B）后复检，待人工确认',
  };
}

function gateOf(text) {
  const cr = crossR(text), ar = armR(text);
  if (cr != null) return { r: cr, thr: 0.8, label: '跨相机运动相关' };
  if (ar != null) return { r: ar, thr: 0.6, label: '本臂运动相关' };
  return null;
}

// 单条轨迹叙事页的全部文字：问题、修没修、证据、还剩多少帧能训练
function storyModel({ e, d, imp = {}, dict = {} }) {
  const raw = d.raw || {}, after = d.after, n = raw.n || 0;
  const m = d.manifest || {};
  const fixed = !!(m.kind && m.kind !== '未修改');
  const audit = d.audit || [];
  const codes = e.codes || [];
  const detectOnly = imp.repair === false;
  const text = fixed ? (m.applied || '') : (m.rejected || '');
  const cov = String(d.report?.sync_evidence_index_coverage || '');
  const cm = /(image|left_wrist_image|right_wrist_image):path偏移([+-]?\d+)/.exec(cov);
  const shift = shiftFromText(text) || (detectOnly && cm ? { cam: cm[1], frames: +cm[2] } : null);
  const keep = Math.max(0, Math.min(n, e.train_frames || 0));
  let type = importType(d, e, dict);
  if (detectOnly) type = 'detect-only';
  if (type === 'cells' && !audit.length) type = 'fixed';
  if (type === 'cells' && audit.every((a) => +a.row < 0) && audit.some((a) => /\.dtype$/.test(a.field))) type = 'dtype';
  const camCn = shift ? (CAM_CN[shift.cam] || shift.cam) : '';
  const off = shift?.frames ?? 0;
  // 按影响帧数排序：一句话先讲覆盖面最大的问题
  const codeFrames = {};
  for (const it of d.intervals || []) codeFrames[it.issue_code] = (codeFrames[it.issue_code] || 0) + (+it.frames || (+it.end_row - +it.start_row + 1) || 0);
  const byFrames = (arr) => [...arr].sort((a, b) => (codeFrames[b] || 0) - (codeFrames[a] || 0));
  const blocking = byFrames(codes.filter((c) => blocksTraining(c, dict)));
  const mask = d.mask || [];
  let problem = '', action = '', evidence = [], blankRows = [];
  if (type === 'shift-fixed') {
    const ap = after?.paths?.[shift.cam];
    if (ap && ap.length) ap.forEach((p, i) => { if (!p) blankRows.push(i); });
    const moved = n - blankRows.length;
    const k = blankRows.length;
    const where = !k ? '' : blankRows[k - 1] === k - 1 ? '开头' : blankRows[0] === n - k ? '结尾' : runsText(blankRows);
    const gate = gateOf(text);
    problem = `同一行里，${camCn}的画面和机械臂状态差了 ${Math.abs(off)} 帧（整条恒定）。`;
    action = `把 ${fmt(moved)} 帧画面移回对应的行；${k ? `${where} ${fmt(k)} 帧这路相机没拍到，显式置空。` : '没有需要置空的行。'}`;
    evidence = [
      ['path 偏移', `${signed(off)} 帧，整条恒定`, true],
      gate ? [gate.label, `r = ${gate.r.toFixed(2)} ${gate.r >= gate.thr ? '≥' : '<'} ${gate.thr.toFixed(2)}`, gate.r >= gate.thr] : ['内容证据', '见修复说明', null],
      ['对端缺图', `${Math.abs(off)} 帧，正好对上`, true],
    ];
  } else if (type === 'shift-refused') {
    const cr = crossR(text), ar = armR(text);
    problem = `${camCn}的画面疑似整体错了 ${Math.abs(off)} 帧，但画面内容没有给出足够的对应证据。`;
    action = '没修：内容证据不足，带问题的帧不进入训练。';
    evidence = [['path 偏移', `${signed(off)} 帧（只是索引，不是内容证据）`, null]];
    if (cr != null) evidence.push(['跨相机运动相关', `r = ${cr.toFixed(2)} ${cr >= 0.8 ? '≥' : '<'} 0.80`, cr >= 0.8]);
    if (ar != null) evidence.push(['本臂运动相关', `r = ${ar.toFixed(2)} ${ar >= 0.6 ? '≥' : '<'} 0.60`, ar >= 0.6]);
    if (cr == null && ar == null) evidence.push(['内容证据', '不足', false]);
  } else if (type === 'detect-only') {
    if (!codes.length && !shift) {
      problem = '这条数据没有检测到问题。';
      action = '不需要修复。';
      evidence = [['检测结果', '无问题', true]];
    } else if (!blocking.length && !shift) {
      problem = `只有不阻断训练的提示：${codes.map((c) => CODE_CN[c] || c).join('、')}。`;
      action = '这些提示不阻断训练。';
      evidence = [['提示', codesText(codes, dict), null]];
    } else {
      problem = shift ? `同一行里，${camCn}的画面和机械臂状态差了 ${Math.abs(off)} 帧。`
        : `${codesText(blocking.length ? blocking : codes, dict)}。`;
      action = '不修复时，带问题的帧不进入训练。';
      evidence = [['问题', codesText(blocking.length ? blocking : codes, dict), false],
        ['问题帧', e.flagged_frames ? `${fmt(e.flagged_frames)} / ${fmt(n)} 帧` : '整条', false],
        ['不修复时可训练', `${fmt(keep)} 帧`, null],
        ...(shift ? [['path 偏移', `${signed(off)} 帧`, null]] : [])];
    }
  } else if (type === 'cells') {
    const rows = [...new Set(audit.map((a) => +a.row))];
    const isVec = audit.some((a) => /^(state|actions)\[/.test(a.field));
    const dims = (raw.state?.[0]?.length || 20) + (raw.actions?.[0]?.length || 20);
    const unchanged = isVec ? n * dims - audit.length : null;
    if (codes.includes('J_STATE_NONFINITE')) {
      problem = `state 里有 ${fmt(audit.length)} 个空值（NaN/Inf）。`;
      action = `只补这 ${fmt(audit.length)} 格：Rotation-6D 按正交约束解出，其余分量逐位不变。`;
    } else if (codes.includes('J_POSITION_ENVELOPE')) {
      problem = `末端位置有 ${fmt(audit.filter((a) => /^state\[/.test(a.field)).length || audit.length)} 个孤立越界值。`;
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
  } else if (type === 'dtype') {
    const a = audit.find((x) => /\.dtype$/.test(x.field)) || {};
    const f = String(a.field || 'timestamp').replace(/\.dtype$/, '');
    const decl = d.declared?.[f];
    problem = `${f} 列存成了 ${a.original_value || '—'}，和 info.json 声明的 ${decl || '类型'} 不符。`;
    action = `按声明做无损类型转换（${a.original_value || '—'} → ${a.repaired_value || '—'}），每个数值逐位不变，没有改写任何数值。`;
    evidence = [['存储类型', `${a.original_value || '—'} → ${a.repaired_value || '—'}${decl ? `（声明 ${decl}）` : ''}`, true], ['数值变化', '0 个，逐位不变', true], ['写后核验', '逐行回读一致', true]];
  } else if (type === 'fixed') {
    problem = `检测到 ${codes.length || 1} 类问题，可以直接修复。`;
    action = fixLabel(m) || m.applied || '按审计记录逐格改写。';
    evidence = [['处理方式', m.applied || '', true]];
  } else if (type === 'meta') {
    problem = 'episodes.jsonl 记录的长度和实际行数不一致。';
    action = '只更新元数据记录，Parquet 文件不改。';
    evidence = [['实际行数', `${fmt(n)} 行`, true], ['Parquet', '未改动', true]];
  } else if (type === 'norepair') {
    const show = blocking.length ? blocking : codes;
    problem = `${codesText(show, dict)}：${fmt(e.flagged_frames)} / ${fmt(n)} 帧被标记。`;
    const NR = noRepairNote(show, dict);
    action = NR.action;
    evidence = [['问题', codesText(show, dict), false], ['被标记帧', `${fmt(e.flagged_frames)} / ${fmt(n)} 帧`, false]];
    if (NR.cand) evidence.push(['候选修复', NR.cand, null]);
  } else if (type === 'hint') {
    problem = `只有不阻断训练的提示：${codes.map((c) => CODE_CN[c] || c).join('、')}。`;
    action = '这些提示不影响训练，不需要修复。';
    evidence = [['提示', codesText(codes, dict), null]];
  } else if (type === 'refused') {
    const show = blocking.length ? blocking : codes;
    problem = `${codesText(show, dict)}${e.flagged_frames ? `：${fmt(e.flagged_frames)} / ${fmt(n)} 帧被标记` : '（整条轨迹级问题）'}。`;
    const whys = [...new Set(splitClausesJs(m.rejected || '').map(reasonLabel).filter(Boolean))].slice(0, 3);
    action = `没修：${whys.join('；') || '证据不足'}。问题段不进入训练，${keep ? '不受影响的连续片段照常进入训练' : '整条交由人工复核或隔离回采'}。`;
    evidence = splitClausesJs(m.rejected || '').slice(0, 4).map((s) => [reasonLabel(s), s.slice(0, 140), null]);
  } else {
    problem = '这条数据没有检测到问题。';
    action = '不需要修复。';
    evidence = [['检测结果', '无问题', true]];
  }
  // 画面类问题：点名是哪一路相机、有多严重（不点名的话，观众看另外两路清楚的画面会以为判错了）
  let cams = [];
  if (['norepair', 'refused', 'detect-only'].includes(type) && !shift) {
    const IMG = imageNotes(d, blocking.length ? blocking : codes, dict);
    cams = IMG.cams;
    if (IMG.sentence) {
      problem = problem.replace(/。$/, '') + `；${IMG.sentence}。`;
      evidence.push(...IMG.evidence);
      if (IMG.mild) action += '程度轻，建议人工复核。';
    }
  }
  // 修了一部分、另一部分没修：必须讲出来，否则"修复"两个字会让人以为整条能用了
  let remain = [];
  if (fixed && !detectOnly) {
    remain = remainAfterFix({ finalCodes: e.final_codes, rowFinal: d.row_final_codes, blankRows, dict, shift });
    if (remain.length) {
      const why = m.rejected ? reasonLabel(splitClausesJs(m.rejected)[0] || '') : '没有可用的修复方法';
      action += `但${remain.map((c) => codeCn(c, dict)).join('、')}没有修（${why}），${keep === 0 ? '整条仍然不能进入训练' : '带这些问题的帧不进入训练'}。`;
      evidence.push(['仍未修', remain.join('、'), false]);
    }
  }
  // 还剩多少帧能训练
  const notKeep = n - keep;
  let keepText;
  if (keep === n) keepText = `全部 ${fmt(n)} 帧都能进入训练。`;
  else if (keep === 0) keepText = '0 帧进入训练；整条交给人工复核或隔离回采。';
  else if (type === 'shift-fixed' && blankRows.length) {
    const blankOut = blankRows.filter((i) => !(mask[i] && mask[i].keep)).length;
    const other = Math.max(0, notKeep - blankOut);
    keepText = `${fmt(blankOut)} 帧修复后这路相机没有画面，不进入训练${other ? `；另有 ${fmt(other)} 帧复检后仍有阻断问题，或不足 30 帧连续` : ''}；其余 ${fmt(keep)} 帧可用。`;
  } else keepText = `${fmt(notKeep)} 帧不进入训练：${detectOnly ? '带阻断问题' : '复检后仍有阻断问题'}，或不足 30 帧连续。`;
  const sb = m.score_before != null && m.score_before !== '' ? +m.score_before : e.score;
  const sa = m.score_after != null && m.score_after !== '' ? +m.score_after : e.final_score;
  const scoreTxt = detectOnly ? `检测评分 ${fmt(sb, 2)} 分 · 未修复` : fixed ? `复检 ${fmt(sb, 2)} → <b>${fmt(sa, 2)}</b> 分` : `复检 ${fmt(sb, 2)} 分 · 保持原样`;
  const ints = d.intervals || [];
  let row = Math.min(n - 1, 25);
  if (shift) row = Math.min(n - 1, Math.max(25, Math.abs(off) + 20));
  else if (audit.length) row = Math.min(n - 1, Math.max(0, +audit[0].row));
  else if (ints.length) row = Math.min(n - 1, Math.max(0, +ints[0].start_row));
  const okType = type === 'clean' || type === 'hint' || (type === 'detect-only' && !blocking.length && !shift);
  const verdictCls = fixed && !detectOnly ? (remain.length ? 'partial' : 'fixed') : okType ? 'ok' : 'refused';
  return { type, fixed, detectOnly, shift, camCn, off, keep, n, problem, action, evidence, keepText, scoreTxt, row, verdictCls, blankRows, blocking, codeFrames, remain, cams };
}

// 批量导入：每条一句话
function batchDesc(e, m) {
  if (m) {
    if (m.kind && m.kind !== '未修改') return fixLabel(m);
    if (m.rejected) return '拒修：' + reasonLabel(m.rejected.split('；')[0]);
  }
  if (!(e.codes || []).length) return '未命中问题';
  return codeLine({ flagged: e.flagged_frames, rows: e.rows }, e.codes);
}

if (typeof module !== 'undefined') module.exports = { noRepairNote, storyModel, captionFor, feedFor, batchDesc, importType, blocksTraining, codeLine, fixLabel, reasonLabel, remainAfterFix, codeCn, codesText, imageNotes, gateOf, fmt };
