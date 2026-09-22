# -*- coding: utf-8 -*-
"""Static configuration: issue-code registry, schema defaults, fallback thresholds.

Every issue code has one category (the four competition categories), one
evidence tier and one scope:

* category: 时序 / 同步 / 内容结构 / 数据价值 (camera, joint-value and
  file/schema defects all belong to 内容结构, as the competition rules put
  "IMU/关节非法值/超标" and "字段缺失/损坏" under 内容/结构).
* tier: deterministic (fact, no statistical threshold), high (strong physical
  evidence), threshold (reference-calibrated statistic, review first).
* scope: row (localised to frames) or episode (applies to the whole episode;
  "defect" episode codes remove every row from training, "info" ones do not).
"""
from __future__ import annotations

VERSION = "RefSync-QA-v3.1"

CAT_T, CAT_S, CAT_C, CAT_V = "时序", "同步", "内容结构", "数据价值"
CATEGORIES = [CAT_T, CAT_S, CAT_C, CAT_V]

# code: (category, tier, scope, blocks_training, 中文说明)
#   scope "row"  -> reported per frame; blocks those frames when blocks_training
#   scope "ep"   -> episode level; blocks every frame when blocks_training
CODES: dict[str, tuple[str, str, str, bool, str]] = {
    # ---------------- 内容结构: file / schema ----------------
    "C_FILE_UNREADABLE": (CAT_C, "deterministic", "ep", True, "Parquet 文件无法读取/损坏"),
    "C_SCHEMA_MISSING_FIELD": (CAT_C, "deterministic", "ep", True, "必需字段缺失"),
    "C_EMPTY_EPISODE": (CAT_C, "deterministic", "ep", True, "轨迹行数为 0"),
    "C_PROCESSING_ERROR": (CAT_C, "deterministic", "ep", True, "处理异常：该轨迹未完成检测（批次继续），需人工检查"),
    "C_SCHEMA_DTYPE_MISMATCH": (CAT_C, "deterministic", "ep", False, "字段存储类型与 info.json 声明不一致（值可读，按契约转换后可用）"),
    "C_FIELD_INVALID": (CAT_C, "deterministic", "row", True, "标量字段形状/类型非法（如长度≠1 的列表、字符串），该值不被采信"),
    # ---------------- 时序 ----------------
    "T_TIMESTAMP_INVALID": (CAT_T, "deterministic", "row", True, "timestamp 为空/非有限值"),
    "T_FRAME_INDEX_INVALID": (CAT_T, "deterministic", "row", True, "frame_index 为空/非整数"),
    "T_INDEX_INVALID": (CAT_T, "deterministic", "row", True, "index 为空/非整数"),
    "T_TIMESTAMP_NON_MONOTONIC": (CAT_T, "deterministic", "row", True, "timestamp 重复/倒退"),
    "T_TIMESTAMP_GAP": (CAT_T, "high", "row", True, "timestamp 间隔大于 1.5 个采样周期（掉帧）"),
    "T_TIMESTAMP_JITTER": (CAT_T, "threshold", "row", True, "timestamp 间隔偏离标称周期（帧率抖动）"),
    "T_TIMESTAMP_UNIT_MISMATCH": (CAT_T, "high", "ep", True, "timestamp 采样间隔为标称值的 10/100/1000 倍（疑似单位错误）"),
    "T_TIMESTAMP_OFFSET": (CAT_T, "high", "ep", True, "timestamp 与 frame_index/fps 存在整体偏移（时间基准不为 0 或记录偏差）"),
    "T_FRAME_INDEX_GAP": (CAT_T, "deterministic", "row", True, "frame_index 跳号（缺帧）"),
    "T_FRAME_ORDER_ERROR": (CAT_T, "deterministic", "row", True, "frame_index 重复/倒序（乱序）"),
    "T_INDEX_DISCONTINUITY": (CAT_T, "deterministic", "row", True, "index 与 frame_index 不同步递增"),
    "T_HEAD_FRAMES_MISSING": (CAT_T, "deterministic", "ep", False, "frame_index 不从 0 开始（首段缺帧）"),
    "T_METADATA_LENGTH_MISMATCH": (CAT_T, "deterministic", "ep", False, "episodes.jsonl 记录长度与实际行数不一致"),
    # ---------------- 同步 ----------------
    "S_EPISODE_INDEX_INVALID": (CAT_S, "deterministic", "row", True, "episode_index 为空/非整数"),
    "S_EPISODE_INDEX_MISMATCH": (CAT_S, "deterministic", "row", True, "episode_index 与文件编号/元数据不一致"),
    "S_TASK_INDEX_INVALID": (CAT_S, "deterministic", "row", True, "task_index 为空或不在 tasks.jsonl 定义中"),
    "S_TASK_SWITCH_WITHIN_EPISODE": (CAT_S, "deterministic", "ep", True, "单条轨迹内 task_index 发生切换"),
    "S_TASK_META_MISMATCH": (CAT_S, "deterministic", "ep", True, "task_index 与 episodes.jsonl 记录的任务不一致"),
    "S_STREAM_MISSING": (CAT_S, "deterministic", "ep", True, "某路相机整条轨迹无图像"),
    "S_SENSOR_LATE_START": (CAT_S, "deterministic", "row", True, "相机头部连续缺图（疑似晚启动；仅能证明头部缺数据）"),
    "S_SENSOR_EARLY_STOP": (CAT_S, "deterministic", "row", True, "相机尾部连续缺图（疑似早停止；仅能证明尾部缺数据）"),
    "S_SENSOR_DROPOUT": (CAT_S, "deterministic", "row", True, "相机中段连续缺图（疑似断流）"),
    "S_STREAM_SHIFTED": (CAT_S, "deterministic", "row", True, "相机流整体错位：图像 path 帧号恒定偏移且对端缺图（疑似晚启动后前移）"),
    "S_IMAGE_PATH_MISMATCH": (CAT_S, "high", "row", True, "图像 path 帧号与 frame_index 局部不一致"),
    "S_VISUAL_KINEMATIC_LAG": (CAT_S, "threshold", "ep", True, "两路腕部相机与各自机械臂运动的时滞一致偏离参考（疑似本体数据与图像整体时间错位；统计时滞，非硬件时钟实测）"),
    "S_CAMERA_LAG_SUSPECT": (CAT_S, "threshold", "ep", False, "单路腕部相机与本臂运动的时滞明显偏离参考（疑似该路相机时间偏移，需复核；不阻断）"),
    "S_STREAM_FROZEN": (CAT_S, "threshold", "row", True, "非纯色画面持续静止而对应机械臂在运动（视觉流停滞）"),
    # ---------------- 内容结构: camera ----------------
    "C_IMAGE_DECODE": (CAT_C, "deterministic", "row", True, "图像 payload 存在但无法解码"),
    "C_IMAGE_SHAPE": (CAT_C, "deterministic", "row", True, "解码后分辨率/通道数与 schema 不符"),
    "C_IMAGE_SCREEN": (CAT_C, "high", "row", True, "黑屏/白屏/低对比度画面"),
    "C_IMAGE_DUPLICATE": (CAT_C, "deterministic", "row", True, "与上一帧字节级完全相同（非纯色）"),
    "C_IMAGE_BLUR": (CAT_C, "threshold", "row", True, "清晰度低于参考集下界（模糊）"),
    # ---------------- 内容结构: joint / numeric ----------------
    "J_DIM_MISMATCH": (CAT_C, "deterministic", "row", True, "state/actions 维度与 schema 不符"),
    "J_STATE_NONFINITE": (CAT_C, "deterministic", "row", True, "state 含 NaN/Inf"),
    "J_ACTION_NONFINITE": (CAT_C, "deterministic", "row", True, "actions 含 NaN/Inf"),
    "J_ROT6D_INVALID": (CAT_C, "deterministic", "row", True, "Rotation-6D 两列不再是单位正交向量（数学约束）"),
    "J_GRIPPER_RANGE": (CAT_C, "threshold", "row", True, "夹爪值超出 [0,1] 归一化区间加容差（归一化为赛题约定推断，非硬件实测边界）"),
    "J_POSITION_ENVELOPE": (CAT_C, "threshold", "row", True, "末端位置分量绝对值超出工程上界（固定工程值，非参考学习包络、非物理硬边界）"),
    "J_VALUE_UNPARSEABLE": (CAT_C, "deterministic", "row", True, "state/actions 分量无法解析为数值（字符串/嵌套等），该行不被采信"),
    "J_SPIKE_JUMP": (CAT_C, "threshold", "row", True, "state/actions 相邻帧突跳"),
    "J_STATE_ACTION_MISMATCH": (CAT_C, "threshold", "row", True, "按轨迹固定时移对齐后 state/actions 残差过大"),
    # ---------------- 数据价值 (training value, never blocks frames) ----------------
    "V_STATE_FREEZE": (CAT_V, "threshold", "ep", False, "state 几乎无运动而 actions 在变（疑似冻结）"),
    "V_ACTION_FREEZE": (CAT_V, "threshold", "ep", False, "actions 几乎无运动而 state 在变（疑似冻结）"),
    "V_LOW_INFORMATION": (CAT_V, "threshold", "ep", False, "state 与 actions 均几乎无运动，训练信息量低"),
    "V_HIGH_IDLE": (CAT_V, "threshold", "ep", False, "静止/空闲帧占比高于参考集"),
    "V_LOW_INTERACTION": (CAT_V, "threshold", "ep", False, "有效交互运动帧占比低于参考集（高价值片段不足）"),
    "V_LOW_VISUAL_DIVERSITY": (CAT_V, "threshold", "ep", False, "三路画面多样性低于参考集（场景覆盖代理偏低）"),
    "V_EPISODE_DUPLICATE": (CAT_V, "deterministic", "ep", False, "与另一条轨迹整轨等价：双向覆盖、顺序、本体与任务均一致（重复入库）"),
    "V_EPISODE_OVERLAP": (CAT_V, "deterministic", "ep", False, "与另一条轨迹存在包含/部分重合/同源副本关系（只剔除被直接覆盖的逐帧重复）"),
}

TIER_ORDER = {"deterministic": 0, "high": 1, "threshold": 2}
TIER_CN = {"deterministic": "确定性", "high": "高置信", "threshold": "阈值型需复核"}


def category(code: str) -> str:
    return CODES[code][0]


def tier(code: str) -> str:
    return CODES[code][1]


def blocks_training(code: str) -> bool:
    return CODES[code][3]


def describe(code: str) -> str:
    return CODES[code][4]


# LeRobot v2.1 field names (first match wins); overridable via --config
FIELD_ALIASES = {
    "timestamp": ["timestamp"],
    "frame_index": ["frame_index"],
    "episode_index": ["episode_index"],
    "index": ["index"],
    "task_index": ["task_index"],
    "state": ["state", "observation.state"],
    "actions": ["actions", "action"],
}

DEFAULT_IMAGE_COLUMNS = ["image", "left_wrist_image", "right_wrist_image"]

# 20-d layout given by the competition rules (per arm: pos3 + rot6d + gripper1)
DEFAULT_LAYOUT = {
    "dim": 20,
    "arms": {
        "left": {"pos": [0, 1, 2], "rot6d": [3, 4, 5, 6, 7, 8], "gripper": [9]},
        "right": {"pos": [10, 11, 12], "rot6d": [13, 14, 15, 16, 17, 18], "gripper": [19]},
    },
    # which arm each camera is mounted on (None = scene camera, uses both arms)
    "camera_arm": {"image": None, "left_wrist_image": "left", "right_wrist_image": "right"},
}

DEFAULT_OPTIONS = {
    "fps": None,  # None -> read meta/info.json, fallback 10
    "expected_image_shape": None,  # None -> read meta/info.json features, fallback [224,224,3]
    "min_clip_frames": 30,  # minimum contiguous clean frames for a training clip
    "sparse_repair_max_ratio": 0.10,  # sparse numeric repair only if <=10% rows affected
    "sparse_repair_max_gap": 2,  # at most 2 consecutive bad rows per hole
    "low_value_sample_weight": 0.5,
    "max_lag_search": 12,
    "equivalent_coverage": 0.95,  # 整轨等价: both directions' row coverage, order and kinematics >= this
    "partial_overlap": 0.3,  # relation reported when max(coverage) >= this
    "min_shared_images": 30,  # rows with shared image signature needed before a pair is reported
    "index_offset_detect_support": 0.6,  # index-frame_index offset mode must hold this share to localise bad rows
    "index_offset_repair_support": 0.9,  # ... and this share (unique mode) before index is rebuilt
    "timestamp_policy": "nominal",  # nominal | conservative | off (see README: 时间戳契约)
    "vk_min_frames": 60,  # visual–kinematic lag not evaluated on shorter episodes
    "clean_previous": False,  # True: delete previous report/governed of a marked output (default: archive)
}

# Fallback thresholds when no calibration file / reference set is provided.
# The shipped calibration/default_thresholds.json overrides these.
FALLBACK_THRESHOLDS = {
    "rot6d_tol": 1e-4,  # reference max deviation 4e-8 (float32 round-off)
    "gripper_low": -0.05,
    "gripper_high": 1.05,
    "position_envelope": 1.0,
    "timestamp_jitter_frac": 0.02,  # |dt - 1/fps| > 2% of the nominal period (reference max 0.002%)
    "timestamp_offset_frames": 0.5,
    "state_step_max": 0.5,
    "action_step_max": 0.5,
    "sa_residual_rmse": 0.15,
    "sa_residual_max_abs": 0.40,
    "sa_group_max_abs": {"pos": 0.08, "rot6d": 0.27, "gripper": 0.53},
    "screen_dark_fraction": 0.95,
    "screen_bright_fraction": 0.95,
    "screen_std_luma": 5.0,
    "blur": {},
    "freeze_motion": 0.3,
    "freeze_min_run": 10,
    "arm_moving_step": 0.01,
    "vk_ref_lag": {"left_wrist_image": -4, "right_wrist_image": -4},
    "vk_lag_tol": 2,  # both wrists deviate >= 2 frames, consistently -> S_VISUAL_KINEMATIC_LAG
    "vk_min_r": 0.6,
    "vk_min_gain": 0.1,
    "cam_lag_tol": 3,  # single wrist deviates >= 3 frames -> S_CAMERA_LAG_SUSPECT (non-blocking)
    "cam_lag_min_r": 0.7,
    "cam_lag_min_gain": 0.15,
    "shift_wrist_min_r": 0.6,  # automatic stream re-alignment evidence (wrist: own-arm motion)
    "shift_wrist_min_gain": 0.1,
    "shift_scene_min_r": 0.1,  # scene camera: cross-camera motion correlation (weak evidence)
    "shift_scene_min_gain": 0.1,
    "xcam_ref_lag": {},
    "low_motion_state_path": 0.5,
    "low_motion_action_path": 1.0,
    "idle_state_cut": 5e-4,
    "idle_action_cut": 1e-4,
    "interaction_state_cut": 0.017,
    "interaction_action_cut": 0.02,
    "idle_rate_high": 0.24,
    "interaction_rate_low": 0.35,
    "visual_diversity_low": 0.26,
}
