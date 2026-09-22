# RefSync-QA v3.2 复赛算子（MoMo_Lab）

面向具身智能多模态训练数据（LeRobot v2.1：三路相机 PNG + 20 维 state/actions + 时间与索引字段）的**质量检测 + 安全治理**算子。
输入一个数据集目录，输出轨迹级/帧级/区间级检测报告、五维评分卡、逐帧质量掩膜、训练片段、可逆修复副本、逐单元格审计与待人工决定的候选修复。

v3.2 在 v3.1 基础上继续收紧修复与训练准入：保留时间戳残余抖动、拒绝弱相关驱动的图像移位、同任务有序片段去重、图像载荷/原值/schema 审计、修复估计降权和可执行的全量验收。正式说明见 [Algorithm Description](docs/Algorithm_Description_v3_2.md)，平台交付方式见 [Deployment Guide](docs/Deployment_Guide_v3_2.md)。交付包仅使用英文文件名和相对路径，不包含原始数据、凭据或旧版摘要。

## 1. 快速运行

```bash
pip install -r requirements.txt            # numpy / pyarrow / pillow；pandas+openpyxl 仅用于可选 xlsx
python run.py --input /data/test --output /data/result --workers 4 --strict
```

| 参数 | 说明 |
|---|---|
| `--input` / `-i` | 数据集根目录（`data/chunk-*/episode_*.parquet` + `meta/`），也接受任意含 parquet 的目录或单个 parquet |
| `--output` / `-o` | 输出目录。**不得与输入/参考集相同或互相包含**（否则拒绝运行，退出码 2） |
| `--reference` / `-r` | 可选：正常参考集目录，提供时现场重新标定全部统计阈值；不提供时使用 `calibration/default_thresholds.json` |
| `--workers` / `-w` | 并行进程数，默认 `min(8, CPU-1)`；以 episode 为最小任务单元 |
| `--no-repair` / `--no-dedup` | 只检测 / 关闭跨轨迹冗余检测 |
| `--min-clip` | 训练片段最短帧数，默认 30 |
| `--timestamp-policy` | `conservative`（默认，单位/基准变换并保留残余抖动）/ `nominal`（显式选择按 `frame_index/fps` 规整，非实测时刻恢复）/ `off` |
| `--clean-previous` | 删除（而非归档）同一输出目录上次运行的 `report/`、`governed/`，仅当目录带输出标记时生效 |
| `--strict` | 任一轨迹处理异常时退出码 3（异常轨迹仍记录为 `C_PROCESSING_ERROR`，其余轨迹照常输出） |
| `--options` | JSON 覆盖字段别名、20 维布局、fps、相机—机械臂对应关系等 |

平台无参数启动时可用环境变量：`RSQA_INPUT`（或 `INPUT_DIR`/`DATA_DIR`）、`RSQA_OUTPUT`（或 `OUTPUT_DIR`/`RESULT_DIR`）、`RSQA_REFERENCE`、`RSQA_WORKERS`。
平台部署请使用英文路径，并显式指定 `--input /data/test`、`--reference /data/reference`（可选）。多数据集自动发现支持 `reference/clean/ref` 及历史数据集名称别名；其他数据集按原相对目录分别输出，避免同名目录覆盖。历史名称兼容不代表部署依赖中文路径。

```python
from refsync_qa import run
summary = run("/data/test_set", "/data/result", reference_dir=None, workers=8)
```

退出码：0 完成（不代表无缺陷）；2 空输入、非法参数/配置或路径防护拒绝；3 `--strict` 下存在处理异常。空参考集/不可读参考轨迹不静默回退为部分标定。

## 2. 数据安全保证

- **只读输入**：启动时解析真实路径（含软链接），拒绝输出目录与输入/参考集相同或互相包含；每个修复副本写入前再核对“目标不是任何输入文件”。
- **输出标记**：输出目录写入 `.refsync_qa_output.json`；数据发现会跳过任何带标记的目录，上次的修复副本不会被当成下一次输入。同一目录再次运行时，旧 `report/`、`governed/` 移到 `_previous_runs/<run_id>/`（`--clean-previous` 才删除）。
- **原子写 + 写后校验**：修复副本先写临时文件，再回读核对每项改动的原值、新值、审计链和 schema；图像审计含 payload SHA-256、长度与 path，int64 不经 float64 比较。未审计改动、伪造原值/新值、未发生的审计均拒绝落盘。SHA-256 证明内容完整性，不证明修复恢复了真实采样状态。
- **输出保护**：已有 report/governed 必须带算子输出标记才允许归档；拒绝链接子目录；归档名带随机后缀，避免同秒重复运行相互覆盖。
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

训练端读取：`python tools/load_governed.py --dataset /data/test --output /data/result`（或 `iter_clips()`），按 `clips.csv` 叠加读取原文件/修复副本，验证路径边界、行数、帧号连续、时间单调、掩膜来源和权重。使用显式异常，`python -O` 不会关闭检查。训练窗口不可跨 clip，`min_clip` 应按下游动作窗口长度配置。

## 4. 检测内容（问题码见 `issue_code_dictionary.csv`）

| 类别 | 检测项 | 证据强度 |
|---|---|---|
| 时序 | timestamp/frame_index/index 为空或非整数；时间戳倒退、掉帧间隔、抖动（>2% 周期）、疑似单位错误、整体偏移；frame_index 跳号/乱序；index 与 frame_index 不同步（偏移众数需唯一且支持度 ≥60%，否则整条标记为“偏移不唯一”）；首段缺帧；元数据长度不符 | 确定性为主，抖动为阈值型 |
| 同步 | **索引/覆盖层**：episode_index、task_index 非法或与元数据不符、轨迹内任务切换；相机头部/尾部/中段连续缺图（疑似晚启动/早停止/断流，仅证明缺数据）、整路缺失、path 帧号恒定偏移且对端缺图（整体错位）、局部 path 不一致。**统计时滞层**：两路腕部相机与各自机械臂运动的时滞**一致**偏离参考（疑似本体与图像整体错位，阻断）；单路明显偏离（`S_CAMERA_LAG_SUSPECT`，提示不阻断）；画面静止而机械臂在动（停滞，按**帧数**计长度）。**硬件时钟层**：不可评估（数据无逐传感器独立时间戳） | 确定性 / 阈值型 |
| 内容结构 | 文件损坏、必需字段缺失、**字段存储类型与 info.json 不符**、标量字段形状非法、数值无法解析；图像无法解码、分辨率不符；黑屏/白屏/低对比；字节级重复帧；模糊；state/actions 维度错误、NaN/Inf；Rotation-6D 单位正交约束（容差 1e-4，参考集最大偏差 4e-8）；夹爪越界（[0,1] 加容差）；位置超工程上界（固定 1.0，非学习包络；参考集观测 \|x\|≤0.47）；相邻帧突跳；轨迹级固定时移下 state/actions 分组残差 | 确定性 / 阈值型 |
| 数据价值 | 冻结/低信息、空闲帧占比高、交互运动不足、画面多样性低（aHash 代理）；跨轨迹冗余（整轨等价/同源副本/包含/部分重合）；任务覆盖（按 tasks.jsonl 全部声明任务，缺失任务计 0） | 只提示与降权，不直接删帧 |

**五维评分（0–100）**：结构完整性 25%、时序 20%、同步 20%、内容 20%、训练价值 15%。每个问题码只归一个维度（同一物理缺陷仍可能触发不同维度的多个码，属于多效应，不宣称因果去重）。
价值分：运动与每一路相机分别判断可评估性，任一部分不可评估时 `value_score` 为空（另给部分分与可评估范围）。v3.2 修复前后总分均不含数据集级去重扣分，去重损失单独列出；避免把治理后的决策用于修复前评分。不能与 v3.1 总分直接比较，也不能把总分当作训练成功概率。
硬门禁：文件不可读/处理异常 0 分，必需字段缺失 ≤25，整路相机缺失 ≤40。
无官方标签时，所有计数都是“算法标记、需复核”，“未被标记”不等于“确认正常”。

## 5. 治理与修复

原则：确定性元数据修正、统计证据门控重对齐、估计补值分开说明；改写只写副本，未改分量保持数值与类型；所有改动进审计并复检。满足约束的插值仍非真实值恢复，质量掩膜增加 `estimated_repair`，涉及估计的轨迹默认权重降至 0.5。弱证据进入 `repair_candidates.csv`，不自动执行。

| 修复 | 自动执行的前提 | 做法 | 不做 / 转候选 |
|---|---|---|---|
| 字段类型 | 标量列存储类型与 info.json 不符且转换无损 | 按声明类型转换（如 int64 → float32） | 有损转换（如 float64 列表 → float32）不做 |
| 时间戳 | frame_index 完整连续且单位/基准变换后偏差在容差内 | 默认保留变换后残余抖动；只有显式选择 nominal 才按标称周期重建 | 缺口/乱序不重建；默认策略对缺失、抖动、非唯一变换仅给候选 |
| index | 偏移众数唯一且支持度 ≥90%、frame_index 连续 | 只改偏离众数的行 | 偏移不唯一（如 50/50）转候选 |
| episode_index / task_index | 文件编号、episodes.jsonl、tasks.jsonl 唯一确定 | 统一为唯一值 | 元数据不唯一时不改 |
| 相机流重对齐 | path 完整、恒定偏移 k、对端恰缺 k 帧且帧号连续；本臂 r≥0.6 或跨相机 r≥0.8，增益≥0.1、偏移一致且无强证据矛盾 | 移动真实 payload，缺失端置空，记录前后 payload 哈希 | 弱场景相关只给候选；阈值为工程安全门槛，不是正确率或硬件时间证明 |
| 稀疏数值补值 | 非有限行 ≤10%、缺口 ≤2 帧、缺口两侧帧号连续、两侧邻居无数值/帧序问题码 | 只补非有限分量：Rotation-6D 分量由约束解出（补后须满足 1e-4 容差），其余按 frame_index 线性插值 | 跨缺帧、乱序、不可信邻居、首尾缺口一律拒绝 |
| 孤立越界 | 单帧越界、两侧帧号连续且邻居可信 | 仅越界分量取前后帧均值 | 连续越界不插值 |
| 元数据 | episodes.jsonl 长度与实际不符 | 元数据补丁（不改 Parquet） | — |
| 只给候选 | 两路腕部一致时滞、单路相机时滞、Rotation-6D 整条噪声 | 写入 `repair_candidates.csv` | 不自动平移本体/重新正交化 |

**训练策略（互斥）**：整段可用 / 修复后整段可用 / 切段使用 / 部分修复+切段使用 / 隔离回采 / 文件不可用 / 处理失败 / 重复剔除。
**跨轨迹去重（非传递、整片段）**：按可用帧数、分数、编号排优先级。只有同一已知任务、全模态签名与顺序完全一致、相对时间一致（容差 1e-5s）、被更高优先级已保留片段连续完整覆盖时才删除该片段；不拼接多个覆盖来源。部分重合保留整片段及独有上下文；不同任务、逆序、不同本体或不同时间间隔不自动去重。去重少一些是保留证据不足的样本，不是漏掉确定的重复。
`frame_valid`（复检后无阻断码）与 `train_keep`（属于 ≥min_clip 的连续片段）分开给出，训练端只读 `train_keep`。低价值提示轨迹 `sample_weight=0.5`。

## 6. 当前标定与验证

- 默认阈值已由完整 20 条参考轨迹（ID 0–19）标定，保存逐文件 SHA-256；阈值哈希为 `4b70fc4db785da7888fa76b7d5ccf9b9fc3bf3a9e9b5a1c8f75b4383ee25d4b4`。
- 完整 89 条：原始检测 24 条未标记、1 条仅价值提示、64 条需复核。默认保守治理生成 8 条 Parquet 副本、1 条元数据补丁；6 条原需复核轨迹复检后不再需复核；61 个片段共 8,881 帧实际可加载。
- 20 条参考集留一：缺陷误报 0/20、价值提示 0/20，仅为小样本观测，不证明泛化误报率为零。
- 三条底本排除后用其余 17 条标定，117 个合成用例满足预设期望；指定注入行的 TP=510、FP=0、FN=6，P=1.0000、R=0.9884、F1=0.9942。包括干净/低于阈值对照，不是 117 个真实异常全检出。
- 23 个历史探针与 24 个 v3.2 安全单测通过；24 个单测在普通及 -O 模式分别运行。
- 两种时间策略的全量验收都回读全部修复副本、实际加载片段，并确认 115 个输入文件哈希未改变。nominal 产生 10,290 帧，但额外数据依赖明确标称时间契约，不作为默认推荐。
- 历史 `validation/real_subset_*`、未带版本前缀的旧摘要仍属 v3.1；以 `validation/v3.2_acceptance.json` 和完整 v3.2 说明为准。

## 7. v3.2 复现与验收入口

```bash
python tools/test_safety.py
python -O tools/test_safety.py
python tools/regression_probes.py --reference /data/reference --output /data/regression
python tools/selftest.py --reference /data/reference --output /data/selftest --workers 4
python tools/acceptance.py --input /data/test --reference /data/reference --output /data/acceptance --workers 4
```

`acceptance.py` 在运行前后核对输入文件 SHA-256，逐个回读修复副本验证审计，并实际加载所有训练片段核对分母。selftest 的缺失时间/抖动恢复用例显式选择 `nominal`，用于验证契约规整功能；默认 conservative 的拒修边界由 `test_safety.py` 单独验证，二者不能混称同一策略。

## 8. 效率

每次特征提取单次读取 Parquet、每张图解码一次；修复复检需再次提取并计入解码工作量。数值列走 Arrow 向量化解析，episode 级多进程。v3.2 首轮全量保守日志：Windows 11、Ryzen 7 5800H、31.86 GiB RAM、4 workers，54.74 秒、55,204 次实际解码（含复检）。耗时包含 CSV/Parquet/掩膜写出，不含最后 summary/xlsx 序列化；同期有其他测试且未区分冷/热缓存，只作诊断，不宣称线性扩展或独立吞吐基准。

## 9. 未覆盖 / 不可评估

硬件时间同步与时钟漂移（无独立时间戳）；花屏/遮挡的专门正样本评测；任务成功率、碰撞等下游验证；位置/夹爪的物理边界（当前为固定工程值）。这些在 `summary.json.coverage_limits` 中逐条列出。
