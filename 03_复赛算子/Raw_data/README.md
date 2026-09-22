# RefSync-QA v3.0 复赛算子（MoMo_Lab）

具身智能多模态训练数据（LeRobot v2.1：三路相机 PNG + 20 维 state/actions + 时间与索引字段）的**质量检测 + 安全治理**算子。
输入一个数据集目录，输出轨迹级/帧级/区间级检测报告、0–100 五维评分卡、逐帧训练掩膜、训练片段、可逆修复副本与逐值审计。

## 1. 快速运行

```bash
pip install -r requirements.txt            # numpy / pyarrow / pillow；pandas+openpyxl 仅用于可选 xlsx
python run.py --input <数据集目录> --output <输出目录>
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--input` / `-i` | 数据集根目录（含 `data/chunk-*/episode_*.parquet` 与 `meta/`），也接受任意含 parquet 的目录或单个 parquet |
| `--output` / `-o` | 输出目录（不存在会自动创建；原始数据只读，从不覆盖） |
| `--reference` / `-r` | 可选：正常参考集目录。提供时现场重新标定全部统计阈值；不提供时使用 `calibration/default_thresholds.json` |
| `--workers` / `-w` | 并行进程数，默认 `min(8, CPU-1)`；以 episode 为最小任务单元，无共享状态 |
| `--no-repair` | 只检测，不生成修复副本 |
| `--no-dedup` | 关闭跨轨迹重复检测 |
| `--min-clip` | 训练片段最短帧数，默认 30 |
| `--options` | JSON 文件覆盖字段别名、20 维布局、fps、相机与机械臂对应关系等 |

平台若以无参数方式启动，可用环境变量：`RSQA_INPUT`（或 `INPUT_DIR`/`DATA_DIR`）、`RSQA_OUTPUT`（或 `OUTPUT_DIR`/`RESULT_DIR`）、`RSQA_REFERENCE`、`RSQA_WORKERS`。
若 `--input` 下同时有多个 LeRobot 数据集（例如赛题包里的 `参考集/` 与 `测试集/`），名称含“参考/reference/clean”的一个自动用于标定，其余逐个检测，结果写到 `--output/<数据集名>/`。

Python 调用：

```python
from refsync_qa import run
summary = run("/data/test_set", "/data/result", reference_dir=None, workers=8)
```

退出码：0 = 正常完成（无论是否发现缺陷）；2 = 输入路径不存在。数据缺陷不会让算子崩溃：损坏文件、缺字段、NaN 字段都被记录为问题码。

## 2. 输出

```
<output>/
  report/
    summary.json / summary.md      总览：检出率、四类分布、证据强度、问题码统计、评分、治理与效率
    episode_report.csv             每条轨迹：问题码+中文说明、帧数、五维分+总分、证据强度、修复动作/拒绝理由、修复后复检、训练策略
    frame_flags.csv                每帧：问题码、涉及的相机/字段、train_keep
    issue_intervals.csv            连续问题区间（按问题码聚合，含起止行/帧与相机）
    duplicate_pairs.csv            跨轨迹重复/部分重合对
    issue_code_dictionary.csv      问题码字典（类别、证据强度、范围、是否阻断训练）
    thresholds_used.json           本次使用的全部阈值与来源
    quality_report.xlsx            以上主要表格的工作簿（安装 pandas+openpyxl 时生成）
  governed/
    data/chunk-000/*.parquet       修复副本（仅发生改写的轨迹；schema 与原始一致）
    meta/                          修复副本的 info.json / episodes.jsonl / tasks.jsonl（按实际行数）
    quality_masks/episode_*.csv    每条可读轨迹的逐帧 train_keep 与 sample_weight（读修复副本或原文件，见 mask_index.csv）
    clips.csv                      训练片段：帧号连续、全部 train_keep、≥ min_clip 帧；标注来源（原本可用/修复恢复/切段新增）
    train_policy.csv               互斥训练策略
    repair_audit.csv               每一个被改写的值：原值、新值、方法、问题码（逐值精确比较）
    repair_manifest.csv            源文件/修复副本 SHA-256、修复前后问题码与分数
    meta_patches.csv               元数据补丁（如 episodes.jsonl length）
```

## 3. 检测内容（问题码详见 `issue_code_dictionary.csv`）

| 类别 | 检测项 | 证据强度 |
|---|---|---|
| 时序 | timestamp/frame_index/index 为空或非整数；时间戳倒退、掉帧间隔、抖动（>2% 周期）、单位错误、整体偏移；frame_index 跳号/乱序；index 与 frame_index 不同步；首段缺帧；元数据长度不符 | 确定性为主，抖动为阈值型 |
| 同步 | episode_index 为空/与文件和元数据不符；task_index 非法、轨迹内切换、与 episodes.jsonl 任务不符；相机**晚启动/早停止/中途断流/整路缺失**（按缺图位置分型）；**相机流整体错位**（path 帧号恒定偏移且对端缺图）；局部 path 不一致；**腕部相机运动与同侧机械臂运动的时滞偏离参考集**；非纯色画面持续静止而机械臂在动（视觉流停滞） | 确定性 / 阈值型 |
| 内容结构 | 文件损坏、必需字段缺失；图像无法解码、分辨率不符；黑屏/白屏/低对比；字节级重复帧；模糊；state/actions 维度错误、NaN/Inf；**Rotation-6D 单位正交约束**（容差 1e-4，参考集最大偏差 4e-8）；夹爪越界；位置超出经验包络；相邻帧突跳；按**轨迹固定时移**对齐后 state/actions 残差（位置/姿态/夹爪分组阈值） | 确定性 / 高置信 / 阈值型 |
| 数据价值 | 冻结/低信息轨迹、空闲帧占比高、交互运动不足、画面多样性低；**跨轨迹重复（非纯色图像字节级重合 ≥80%）与部分重合**；任务分布均衡度与熵（数据集级） | 确定性 / 阈值型，只给提示与降权，不删帧 |

五维评分（0–100）：结构完整性 25%、时序 20%、同步 20%、内容 20%、训练价值 15%，证据互斥归属；价值无法评估时记“不可评估”并按剩余权重归一；硬门禁：文件不可读 0 分，必需字段缺失 ≤25，整路相机缺失 ≤40。
无官方标签时，所有计数都是“算法标记、需复核”，不等同于真实异常数。

## 4. 治理原则与修复

只在证据唯一、可逆时改写数据，且改写写到副本；每个改写值进审计表；修复副本用同一检测器复检，训练掩膜与片段**只从最终副本生成**。

| 修复 | 触发条件 | 做法 | 不做的事 |
|---|---|---|---|
| 时间戳规整 | frame_index 完整连续 | `timestamp = frame_index / fps`（参考集时间戳严格满足此关系） | frame_index 有缺口/乱序时不重建 |
| index / episode_index / task_index | 由 frame_index 恒定偏移、文件编号与 episodes.jsonl、tasks.jsonl 唯一确定 | 统一为唯一值 | 元数据不能唯一确定时不改 |
| 相机流重对齐 | 该路 path 帧号恒定偏移 k、且对端正好缺 k 帧；腕部相机需与本臂运动时滞一致 | 按 path 帧号把图像移回对应行，缺失端置空（记为晚启动缺口） | 不改名掩盖错位、不生成图像 |
| 稀疏数值补值 | 非有限行 ≤10%、每个缺口 ≤2 帧、两侧有可信邻帧 | **只补非有限分量**：Rotation-6D 单分量用正交/单位范数约束解出，其余分量按时间线性插值；其余分量逐值不变 | 大段缺失不臆造 |
| 孤立越界 | 单帧位置越界、前后帧一致 | 仅越界分量取前后帧均值 | 连续越界不插值 |
| 元数据 | episodes.jsonl 长度与实际不符 | 在治理元数据中更新 | — |

训练策略（互斥）：整段可用 / 修复后整段可用 / 切段使用 / 部分修复+切段使用 / 隔离回采 / 文件不可用 / 重复剔除（重复组只保留可用帧最多的一条）。低价值提示的轨迹 `sample_weight=0.5`。

## 5. 标定与验证

默认阈值 `calibration/default_thresholds.json` 由初赛参考集 10 条正常轨迹标定（逐项来源写在文件 `_source` 中）；平台若提供参考集，用 `--reference` 现场重标定。硬约束（NaN、索引规则、Rotation-6D）不依赖标定。

`python tools/selftest.py --reference <参考集> --output <dir>` 复现以下结果：

- **参考集留一**：每次用其余 9 条标定、检测留出的 1 条，缺陷误报 0/10，价值提示 0/10（样本小，只说明没有观察到误报）。
- **故障注入**：以一条参考轨迹为底本注入 32 类故障 + 1 条干净对照（标定时排除底本），33/33 检出期望问题码；可修复项全部通过复检；相机流重对齐后第 7 行起图像字节与 path 与原始**逐字节一致**；稀疏 NaN 与单帧突跳修复后与真值最大误差 < 1e-4。
- **初赛真实数据（7 条抽样）**：ep29/48/74 检出左腕/右腕/主相机整体错位 +5/+10/+3 帧，重对齐后仅剩晚启动缺口；ep80 只补 12 个 NaN 分量后复检通过；ep13/73 时间戳规整后复检通过；ep17 NaN 行占 25%，拒绝补值并隔离。

## 6. 效率

单次读取 parquet，每张图只解码一次；episode 级多进程。2 核环境约 2 s/条（含三路 224×224 解码与复检），8 核机器建议 `--workers 8`。

## 7. 相对初赛 v2.2 的主要变更

- 不再改写 path 掩盖错位：改为按真实帧号重对齐；新增“相机流整体错位 / 晚启动 / 早停止 / 断流 / 整路缺失”分型。
- 数值修复只改非有限分量（v2.2 会整行插值，改动原本有效的值）。
- 新增基础字段门禁（timestamp/index/episode_index/frame_index 空值、任务与元数据不符、时间戳整体偏移）。
- 新增跨轨迹重复检测；Rotation-6D 约束容差由 0.08 收紧到 1e-4（参考集最大偏差 4e-8）。
- 视觉停滞排除纯色帧（纯色帧归黑屏/白屏）；视觉—本体时滞改为腕部相机与同侧机械臂逐路比较。
- state/actions 残差改为轨迹级固定时移 + 分组阈值；缺 payload 不再误报分辨率异常。
- 数据价值缺数据时记“不可评估”；关节数值问题按赛题归入“内容/结构”。
- 检测、复检、掩膜、片段、策略同一入口生成；不再锁定检出数量；无硬编码轨迹数与路径。
