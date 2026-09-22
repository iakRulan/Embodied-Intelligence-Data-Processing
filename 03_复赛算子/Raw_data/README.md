# RefSync-QA v3.1 复赛算子（MoMo_Lab）

面向具身智能多模态训练数据（LeRobot v2.1：三路相机 PNG + 20 维 state/actions + 时间与索引字段）的**质量检测 + 安全治理**算子。
输入一个数据集目录，输出轨迹级/帧级/区间级检测报告、五维评分卡、逐帧质量掩膜、训练片段、可逆修复副本、逐单元格审计与待人工决定的候选修复。

v3.1 按《v3.0 独立复核》逐项整改（对照表见 `CHANGELOG_v3.1.md`）：输入/输出路径防护、非传递的帧级去重、短轨迹/非法值/进程异常容错、保守数值修复门禁、同步证据分层、可复现的自测协议。

## 1. 快速运行

```bash
pip install -r requirements.txt            # numpy / pyarrow / pillow；pandas+openpyxl 仅用于可选 xlsx
python run.py --input <数据集目录> --output <输出目录>
```

| 参数 | 说明 |
|---|---|
| `--input` / `-i` | 数据集根目录（`data/chunk-*/episode_*.parquet` + `meta/`），也接受任意含 parquet 的目录或单个 parquet |
| `--output` / `-o` | 输出目录。**不得与输入/参考集相同或互相包含**（否则拒绝运行，退出码 2） |
| `--reference` / `-r` | 可选：正常参考集目录，提供时现场重新标定全部统计阈值；不提供时使用 `calibration/default_thresholds.json` |
| `--workers` / `-w` | 并行进程数，默认 `min(8, CPU-1)`；以 episode 为最小任务单元 |
| `--no-repair` / `--no-dedup` | 只检测 / 关闭跨轨迹冗余检测 |
| `--min-clip` | 训练片段最短帧数，默认 30 |
| `--timestamp-policy` | `nominal`（默认，按 LeRobot 契约 `frame_index/fps` 规整）/ `conservative`（只做单位换算、基准归零等无损变换，其余写入候选）/ `off` |
| `--clean-previous` | 删除（而非归档）同一输出目录上次运行的 `report/`、`governed/`，仅当目录带输出标记时生效 |
| `--strict` | 任一轨迹处理异常时退出码 3（异常轨迹仍记录为 `C_PROCESSING_ERROR`，其余轨迹照常输出） |
| `--options` | JSON 覆盖字段别名、20 维布局、fps、相机—机械臂对应关系等 |

平台无参数启动时可用环境变量：`RSQA_INPUT`（或 `INPUT_DIR`/`DATA_DIR`）、`RSQA_OUTPUT`（或 `OUTPUT_DIR`/`RESULT_DIR`）、`RSQA_REFERENCE`、`RSQA_WORKERS`。
若 `--input` 下有多个 LeRobot 数据集（如赛题包里的 `参考集/` 与 `测试集/`），名称含“参考/reference/clean”的一个自动用于标定，其余逐个检测，结果写到 `--output/<数据集名>/`。

```python
from refsync_qa import run
summary = run("/data/test_set", "/data/result", reference_dir=None, workers=8)
```

退出码：0 完成（无论是否发现缺陷）；2 输入不存在或输入/输出重叠被拒绝；3 `--strict` 下存在处理异常。

## 2. 数据安全保证

- **只读输入**：启动时解析真实路径（含软链接），拒绝输出目录与输入/参考集相同或互相包含；每个修复副本写入前再核对“目标不是任何输入文件”。
- **输出标记**：输出目录写入 `.refsync_qa_output.json`；数据发现会跳过任何带标记的目录，上次的修复副本不会被当成下一次输入。同一目录再次运行时，旧 `report/`、`governed/` 移到 `_previous_runs/<时间>/`（`--clean-previous` 才删除）。
- **原子写 + 写后校验**：修复副本先写临时文件，回读后与源文件**逐单元格**比较：任何未进审计表的改动、或审计记录了却未发生的改动，都会丢弃该副本并记录原因；通过后才 `os.replace` 落盘。
- **容错**：每条轨迹独立处理；损坏文件、缺字段、非法值、5 帧短轨迹都转为问题码；进程级异常转为 `C_PROCESSING_ERROR` 记录，批次继续。

## 3. 输出

```
<output>/
  .refsync_qa_output.json          输出标记（版本、时间、输入）
  report/
    summary.json / summary.md      总览：口径说明、四类分布、证据强度、同步证据分层、评分、治理、数据价值、未覆盖项、效率
    episode_report.csv             每条轨迹：问题码+说明、五维分（价值分不可评估时为空，另给部分分与可评估范围）、同步证据、修复/拒绝/候选、复检、训练策略
    frame_flags.csv                每帧问题码、涉及相机/字段、train_keep（= 属于最终训练片段）
    issue_intervals.csv            连续问题区间
    duplicate_pairs.csv            跨轨迹冗余关系：双向覆盖率、顺序一致性、本体一致性、任务是否相同、关系类型
    issue_code_dictionary.csv      问题码字典（类别、证据强度、范围、是否阻断训练）
    thresholds_used.json           本次阈值、来源、参考集观测范围、标定出处（参考 ID、文件 SHA-256、阈值哈希）
    quality_report.xlsx            主要表格工作簿（装有 pandas+openpyxl 时）
  governed/                        叠加层（overlay），不是可单独加载的完整数据集
    data/chunk-000/*.parquet       修复副本（仅发生改写的轨迹；列 dtype 按 info.json 契约）
    meta/                          修复副本的 info.json（refsync_overlay 字段列出 episode_ids）/ episodes.jsonl / tasks.jsonl
    quality_masks/episode_*.csv    逐帧 frame_valid / dedup_drop / clip_id / train_keep / sample_weight
    mask_index.csv                 每条轨迹读哪个文件（修复副本或原文件）及可用行数
    clips.csv                      训练片段：帧号连续、≥ min_clip 帧；来源（原本可用/修复恢复/切段新增/修复+切段）
    train_policy.csv               互斥训练策略（含去重前策略、去重剔除帧数与覆盖来源）
    repair_audit.csv               每个被改写的单元格：原值、新值、方法、问题码；schema 变更以 row=-1 记录
    repair_candidates.csv          证据不唯一、未自动执行的修复方案（供人工决定）
    repair_manifest.csv            源文件/副本 SHA-256、修复类型（Parquet 副本/仅元数据补丁）、前后问题码与分数
    meta_patches.csv               元数据补丁
```

训练端读取：`python tools/load_governed.py --dataset <原始数据集> --output <输出目录>`（或 `iter_clips()`），按 `clips.csv` 叠加读取原文件/修复副本，并校验帧号连续与掩膜一致。

## 4. 检测内容（问题码见 `issue_code_dictionary.csv`）

| 类别 | 检测项 | 证据强度 |
|---|---|---|
| 时序 | timestamp/frame_index/index 为空或非整数；时间戳倒退、掉帧间隔、抖动（>2% 周期）、疑似单位错误、整体偏移；frame_index 跳号/乱序；index 与 frame_index 不同步（偏移众数需唯一且支持度 ≥60%，否则整条标记为“偏移不唯一”）；首段缺帧；元数据长度不符 | 确定性为主，抖动为阈值型 |
| 同步 | **索引/覆盖层**：episode_index、task_index 非法或与元数据不符、轨迹内任务切换；相机头部/尾部/中段连续缺图（疑似晚启动/早停止/断流，仅证明缺数据）、整路缺失、path 帧号恒定偏移且对端缺图（整体错位）、局部 path 不一致。**统计时滞层**：两路腕部相机与各自机械臂运动的时滞**一致**偏离参考（疑似本体与图像整体错位，阻断）；单路明显偏离（`S_CAMERA_LAG_SUSPECT`，提示不阻断）；画面静止而机械臂在动（停滞，按**帧数**计长度）。**硬件时钟层**：不可评估（数据无逐传感器独立时间戳） | 确定性 / 阈值型 |
| 内容结构 | 文件损坏、必需字段缺失、**字段存储类型与 info.json 不符**、标量字段形状非法、数值无法解析；图像无法解码、分辨率不符；黑屏/白屏/低对比；字节级重复帧；模糊；state/actions 维度错误、NaN/Inf；Rotation-6D 单位正交约束（容差 1e-4，参考集最大偏差 4e-8）；夹爪越界（[0,1] 加容差）；位置超工程上界（固定 1.0，非学习包络；参考集观测 \|x\|≤0.47）；相邻帧突跳；轨迹级固定时移下 state/actions 分组残差 | 确定性 / 阈值型 |
| 数据价值 | 冻结/低信息、空闲帧占比高、交互运动不足、画面多样性低（aHash 代理）；跨轨迹冗余（整轨等价/同源副本/包含/部分重合）；任务覆盖（按 tasks.jsonl 全部声明任务，缺失任务计 0） | 只提示与降权，不直接删帧 |

**五维评分（0–100）**：结构完整性 25%、时序 20%、同步 20%、内容 20%、训练价值 15%。每个问题码只归一个维度（同一物理缺陷仍可能触发不同维度的多个码，属于多效应，不宣称因果去重）。
价值分：运动与每一路相机分别判断可评估性，任一部分不可评估时 `value_score` 为空（另给 `value_score_partial` 与 `value_coverage`），缺失的相机不会显示为满分；跨轨迹冗余在数据集级去重后计入（被去重剔除的帧占比越高扣分越多）。
硬门禁：文件不可读/处理异常 0 分，必需字段缺失 ≤25，整路相机缺失 ≤40。
无官方标签时，所有计数都是“算法标记、需复核”，“未被标记”不等于“确认正常”。

## 5. 治理与修复

原则：证据唯一才改写，改写只写副本，单元格级写回并保持列 dtype（float64 列仍为 float64，未改分量逐位不变）；每个改写进审计表并经写后校验；修复副本用同一检测器复检，掩膜与片段只从最终副本生成；证据不足的方案进入 `repair_candidates.csv`，不自动执行。

| 修复 | 自动执行的前提 | 做法 | 不做 / 转候选 |
|---|---|---|---|
| 字段类型 | 标量列存储类型与 info.json 不符且转换无损 | 按声明类型转换（如 int64 → float32） | 有损转换（如 float64 列表 → float32）不做 |
| 时间戳 | frame_index 完整连续 | 单位换算/基准归零能精确复现 `frame_index/fps` 时按无损变换标注；否则按标称周期规整（LeRobot 契约，非真实采样时刻恢复，原值全部在审计） | frame_index 缺口/乱序不重建；`conservative` 下非无损情形转候选 |
| index | 偏移众数唯一且支持度 ≥90%、frame_index 连续 | 只改偏离众数的行 | 偏移不唯一（如 50/50）转候选 |
| episode_index / task_index | 文件编号、episodes.jsonl、tasks.jsonl 唯一确定 | 统一为唯一值 | 元数据不唯一时不改 |
| 相机流重对齐 | path 帧号完整、恒定偏移 k 且对端恰缺 k 帧、frame_index 连续，**并且**至少一项独立内容证据与 k 一致（本臂运动时滞：r≥0.6、增益≥0.1；跨相机运动相关或场景相机双臂时滞：r≥0.1、增益≥0.1，标“弱”），且无更强证据矛盾 | 按 path 帧号把图像移回对应行，缺失端置空；审计标注证据强度 | 证据不足或矛盾转候选，保持隔离；从不生成图像 |
| 稀疏数值补值 | 非有限行 ≤10%、缺口 ≤2 帧、缺口两侧帧号连续、两侧邻居无数值/帧序问题码 | 只补非有限分量：Rotation-6D 分量由约束解出（补后须满足 1e-4 容差），其余按 frame_index 线性插值 | 跨缺帧、乱序、不可信邻居、首尾缺口一律拒绝 |
| 孤立越界 | 单帧越界、两侧帧号连续且邻居可信 | 仅越界分量取前后帧均值 | 连续越界不插值 |
| 元数据 | episodes.jsonl 长度与实际不符 | 元数据补丁（不改 Parquet） | — |
| 只给候选 | 两路腕部一致时滞、单路相机时滞、Rotation-6D 整条噪声 | 写入 `repair_candidates.csv` | 不自动平移本体/重新正交化 |

**训练策略（互斥）**：整段可用 / 修复后整段可用 / 切段使用 / 部分修复+切段使用 / 隔离回采 / 文件不可用 / 处理失败 / 重复剔除。
**跨轨迹去重（非传递）**：按可用帧数、分数、编号排定优先级；某行只有在“同样的全模态行（三路图像 + state + actions）已在更高优先级轨迹的**保留片段**中”时才剔除，然后重算片段。不做并查集传递闭包：与桥接轨迹各自重合、彼此不重合的两条轨迹都会保留；图像相同但本体不同的“同源副本”只提示、不剔除。
`frame_valid`（复检后无阻断码）与 `train_keep`（属于 ≥min_clip 的连续片段）分开给出，训练端只读 `train_keep`。低价值提示轨迹 `sample_weight=0.5`。

## 6. 标定与验证（口径与协议分开写）

- **默认阈值**：`calibration/default_thresholds.json` 由初赛参考集 **10 条**轨迹（ID 0,1,2,3,4,7,12,15,16,17）标定；文件 `_provenance` 记录每个参考文件 SHA-256 与阈值哈希（`c3a579ca…a313`），`_source` 写明每项来源。用 `tools/calibrate_reference.py` 可复现或按完整 20 条重标定；平台提供参考集时用 `--reference` 现场标定。
- **回归探针** `python tools/regression_probes.py --reference <参考集> --output <新目录>`：复核报告中的 9 个反例 + 14 个边界用例（短序列、非法字符串、标量形状、非传递去重、跨缺帧补值、float64 保真、index 偏移并列、输入输出重叠、输出标记、int64 时间戳、进程异常等），**23/23 通过**，任一失败退出码 1（`validation/regression_probe_results.json`）。
- **自测** `python tools/selftest.py --reference <参考集> --output <新目录> [--base-episodes 12,16,0]`（任一漏检/意外缺陷码/修复失败/对照被标记/留一误报都返回非零）：
  - 参考集留一（本机 10 条，每折用其余 9 条重标定）：缺陷误报 0/10，价值提示 0/10。样本小，只说明未观察到误报；完整 20 条请在本地复跑。
  - 故障注入：3 条底本 × 39 类（含干净对照、停滞 8/15/30 帧三档、单路相机时滞、本体整体错位、int64 时间戳、index 局部偏移），标定时排除全部底本（7 条标定）：**117/117 符合预期**（低于停滞检测下限 10 帧的 8 帧注入按预期不报），无意外缺陷码；帧级（注入行已知的 21×3=63 个帧级用例）Precision 1.000、Recall 0.988（漏检 6 行均为停滞区间首帧：首张陈旧帧与真实帧的差分不低于静止阈值）；重对齐后第 7 行起图像字节与 path 与原始逐字节一致；稀疏 NaN 与单帧突跳的补值误差 ≤7.3e-4（插值估计，非精确恢复，容差 0.25×典型帧间运动）。
  - 这些是合成注入结果，不等同于真实数据上的 Precision/Recall。
- **真实数据抽样（本机仅有测试集 8 条：13,17,25,29,48,73,74,80）**：ep25 检出 timestamp 为 int64 且单位为 10 ms（并含 ±1 截断），类型无损转换 + 按标称周期规整后复检通过；ep29/48/74 路径整体错位 +5/+10/+3，分别由跨相机运动相关（弱）/本臂运动时滞（中）/场景相机双臂时滞+跨相机（弱）确认后重对齐，仅剩头部缺图；ep80 只补 12 个 NaN 分量后复检通过；ep13/73 时间戳规整后复检通过；ep17 NaN 行占 25%，拒绝补值并隔离（`validation/real_subset_*`）。完整 89 条请在本地复跑并以其输出为准。

## 7. 效率

单次读取 Parquet，每张图只解码一次，数值列走 Arrow 向量化解析；episode 级多进程，异常不拖垮批次。`summary.json.timing` 记录读取→检测→修复→复检→去重→全部 CSV/Parquet/掩膜写出的总耗时、实际解码图像数与硬件信息（本机 2 核 cloud sandbox：8 条 / 9,120 张解码图约 17 s）。计时是诊断日志，不区分冷/热缓存，不作为独立基准。

## 8. 未覆盖 / 不可评估

硬件时间同步与时钟漂移（无独立时间戳）；花屏/遮挡的专门正样本评测；任务成功率、碰撞等下游验证；位置/夹爪的物理边界（当前为固定工程值）。这些在 `summary.json.coverage_limits` 中逐条列出。
