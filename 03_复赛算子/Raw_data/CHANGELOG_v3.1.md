# RefSync-QA v3.1 整改对照（依据《v3.0 独立复核 2026-09-22》）

每一项给出：改动位置 → 验证方式 → 本机结果。“本机”指云端沙箱，只有参考集 10 条、测试集 8 条（13,17,25,29,48,73,74,80）；完整 89 条与 20 条留一需在本地复跑（命令见文末）。

## P0-1 输入输出重叠

| 改动 | 验证 |
|---|---|
| `pipeline.guard_paths`：真实路径（含软链接）比较，拒绝 output 与 input/reference 相同或互相包含，CLI 退出码 2 | 探针 `output_input_overlap_guard`：复核报告的反例（input=`<sandbox>/governed`，output=`<sandbox>`）及另两种组合均被拒绝，源文件 SHA-256 不变 |
| 写入前核对目标不是任何输入文件（`Repairer.write(forbidden=…)`） | 同上 |
| 输出标记 `.refsync_qa_output.json`；`dataset.discover` 与 CLI 多数据集发现跳过带标记目录 | 探针 `marked_output_never_rediscovered_as_input` |
| 旧 `report/`、`governed/` 移到 `_previous_runs/<时间>/`；`--clean-previous` 才删除 | 探针 `previous_run_archived_not_overwritten` |
| 临时文件写入 → 回读逐单元格校验 → `os.replace` | 探针 `post_write_verify_catches_unaudited_change` |

## P1-2 去重过度剔除

| 改动 | 验证 |
|---|---|
| 删除并查集传递闭包；关系按双向覆盖率 + 顺序一致性 + state/actions 一致性 + 任务是否相同分为 整轨等价 / 同源副本 / 包含 / 部分重合（`pipeline.relations`） | 探针 `dedup_non_transitive_keeps_disjoint_episode`：A–B–C 反例中 A、C 各保留 100 帧，A–C 无关系 |
| 帧级剔除：某行仅当相同的全模态行已在更高优先级轨迹的**保留片段**里才剔除，随后重算片段（`report.governance`） | 探针 `dedup_containment_keeps_superset`、`dedup_equivalent_pair_drops_one_copy` |
| 图像相同但本体不同 → 同源副本，只提示不剔除 | 探针 `dedup_same_images_different_state_not_dropped` |
| 去重后在数据集级统一重算价值分（被剔除帧占比计入冗余扣分） | `report._rescore`；探针 `dedup_and_task_coverage_in_reports` |

## P1-3 容错

| 反例 | v3.0 | v3.1 |
|---|---|---|
| 5 帧、max lag 12 的互相关 | ValueError | lag 上限按有效长度裁剪，返回空；短轨迹记“视觉—本体时滞不可评估”（探针 `short_xcorr_5_rows`、`short_episode_analyse`） |
| state 中不可解析字符串 | ValueError | 逐分量解析，`J_VALUE_UNPARSEABLE`（探针 `numeric_string_corruption`） |
| 标量字段 `[1,99]` | 静默取 1 | 置 NaN 并报 `C_FIELD_INVALID`（探针 `scalar_wrong_shape_reported`） |
| worker 异常 | 整批终止 | 每条轨迹 try/except + future 级兜底与进程内重试，记为 `C_PROCESSING_ERROR`，策略“处理失败”；`--strict` 退出码 3（探针 `worker_exception_becomes_record`、`processing_error_recorded_batch_continues`） |

## P1-4 数值修复门禁

| 问题 | v3.1 |
|---|---|
| 行号比例插值、跨缺帧 | 按 frame_index 插值，并要求缺口两侧帧号连续、邻居无数值/帧序问题码；反例帧号 `[49,150,151]` 直接拒绝（探针 `sparse_repair_rejects_frame_gap`、`sparse_repair_rejects_untrusted_neighbour`） |
| index 偏移众数无支持度 | 检测：众数须唯一且支持度 ≥60%，否则整条标“偏移不唯一”；修复：≥90% 且唯一才执行，否则写入候选（探针 `index_offset_tie_not_repaired`、`index_offset_dominant_repaired_minimally`） |
| float64 行被整行转 float32 | 单元格级写回、保持列 dtype；反例中有限分量改动 0 个、审计 1 条、写后校验无差异（探针 `float64_sparse_repair_preservation`） |
| 实际改动与审计逐值匹配 | 写后逐单元格校验（未审计改动或审计未发生即丢弃副本） |
| （新发现）ep25 timestamp 为 int64 且单位 10 ms、含 ±1 截断，v3.0 写回时被 int64 截断 | 新增 `C_SCHEMA_DTYPE_MISMATCH`；标量列按 info.json 无损转换后再规整；ep25 复检通过（探针 `int64_timestamp_schema_and_unit_repair`） |

## P1-5 自测可复现性

| 问题 | v3.1 |
|---|---|
| 停滞长度按差分数、阈值按另一口径 | 检测与标定共用 `detect.frozen_runs`，长度统一按**帧数**（低运动差分数 + 1）；阈值 = max(10, ceil(1.5 × 参考集最长停滞帧数)) |
| 单底本、单一时长 | 3 条底本（12,16,0）× 39 类，停滞 8/15/30 帧三档，期望按当次标定的检测下限自动判定 |
| 命中不等于 P/R | 帧级 TP/FP/FN（63 个注入行已知的用例）：P 1.000、R 0.988；意外缺陷码计为失败；干净对照连价值提示都不允许 |
| 失败仍退出 0 | 任一失败退出码 1；回归探针同理 |
| 删除输出目录 | 只删除带 `.refsync_selftest` 标记的目录 |
| 标定出处不明 | 阈值文件 `_provenance`：参考 ID、逐文件 SHA-256、阈值哈希；`tools/calibrate_reference.py` 可复现 |

## 第四节其余事项

| 事项 | v3.1 |
|---|---|
| 同步可观测性 | summary 分“索引/覆盖（确定性）”“统计时滞/停滞（阈值型）”“硬件时钟（不可评估）”三层；缺图码改称“疑似晚启动/早停止/断流，仅证明缺数据” |
| 视觉—本体时滞过度阻断 | 阻断码 `S_VISUAL_KINEMATIC_LAG` 仅在两路腕部**一致**偏离（≥2 帧、r≥0.6、增益≥0.1，偏差相差 ≤1）时报；单路偏离改为不阻断的 `S_CAMERA_LAG_SUSPECT`（≥3 帧、r≥0.7、增益≥0.15）；path 错位已解释的时滞不重复报；< 60 帧不评估 |
| 相机自动移位前提 | 需 path 完整 + 帧号连续 + 至少一项与 k 一致的独立内容证据且无更强矛盾；证据强度写入审计；否则进 `repair_candidates.csv` |
| 时间戳规整 | 无损变换（单位换算、基准归零）与“按标称周期规整”分开标注，原值全部在审计；`--timestamp-policy conservative/off` |
| 位置/夹爪范围 | 描述改为“固定工程上界，非学习包络/物理边界”，夹爪降为阈值型；阈值文件记录参考集观测范围（\|x\|≤0.47，夹爪 [0,1]） |
| 价值不可评估 | 运动与每路相机分别判断；任一不可评估时 `value_score` 为空，另给部分分与覆盖范围 |
| 任务覆盖 | 用 tasks.jsonl 全部声明任务（缺失计 0）；少于 2 个任务为不可评估；另给可训练帧的任务分布 |
| 治理元数据 | governed/meta/info.json 去掉误导的 splits，增加 `refsync_overlay`（episode_ids、说明）；新增 `tools/load_governed.py` 并校验 |
| 掩膜与片段 | 掩膜分 `frame_valid` / `dedup_drop` / `clip_id` / `train_keep`，`train_keep` 只取片段成员，隔离轨迹不再有散点 |
| 评分多效应 | summary 注明“一码一维，不宣称因果去重”；自测输出每个单缺陷用例被扣分的维度（`score_dims_below_100`） |
| 口径统一 | headline 与 summary 分开写 Parquet 副本、仅元数据补丁、复检不再需复核（按两类拆分）、frame_valid、train_frames |
| 效率 | 计时覆盖全部 CSV/Parquet/掩膜写出；记录实际解码图像数、硬件信息；注明是诊断日志 |

## 本地复跑（以本地结果为准）

```powershell
# 1) 完整 89 条：默认 10 条标定 与 20 条现场标定 两种口径分别输出
python 03_复赛算子/Raw_data/run.py --input <测试集> --output <新目录A> --workers 4
python 03_复赛算子/Raw_data/run.py --input <测试集> --output <新目录B> --reference <参考集> --workers 4
# 2) 20 条留一 + 合成注入（专用新目录）
python 03_复赛算子/Raw_data/tools/selftest.py --reference <参考集> --output <新目录C> --workers 4
# 3) 边界反例回归
python 03_复赛算子/Raw_data/tools/regression_probes.py --reference <参考集> --output <新目录D>
# 4) 训练端读取校验
python 03_复赛算子/Raw_data/tools/load_governed.py --dataset <测试集> --output <新目录A>
```
