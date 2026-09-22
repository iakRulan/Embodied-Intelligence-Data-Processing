"""v3.2 safety regressions; no competition data required. Run with and without -O."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refsync_qa import config, dataset, detect, features
from refsync_qa.detect import Result
from refsync_qa.pipeline import PathGuardError, _prepare_output, run, validate_options
from refsync_qa.repair import Repairer, _fmt, verify_copy
from refsync_qa.report import _w, build_outputs, governance
from tools.regression_probes import fake_result
from tools.load_governed import iter_clips


def opts(**extra):
    return {**config.DEFAULT_OPTIONS, "layout": config.DEFAULT_LAYOUT, **extra}


def audit(field, old, new, row=0):
    return {"field": field, "row": row, "original_value": _fmt(old), "repaired_value": _fmt(new)}


class SafetyTests(unittest.TestCase):
    def test_audit_original_value(self):
        a, b = pa.table({"x": [1.]}), pa.table({"x": [2.]})
        self.assertFalse(verify_copy(a, b, [audit("x", 1., 2.)]))
        self.assertTrue(verify_copy(a, b, [audit("x", 99., 2.)]))

    def test_image_payload_hash_verified(self):
        x, y = {"bytes": b"old", "path": "a"}, {"bytes": b"new", "path": "b"}
        a, b = pa.table({"image": [x]}), pa.table({"image": [y]})
        self.assertFalse(verify_copy(a, b, [audit("image", x, y)]))
        self.assertTrue(verify_copy(a, b, [audit("image", x, {**y, "bytes": b"tampered"})]))

    def test_int64_no_float_rounding(self):
        a, b = pa.table({"x": [2**53]}), pa.table({"x": [2**53 + 1]})
        self.assertTrue(verify_copy(a, b, []))
        self.assertFalse(verify_copy(a, b, [audit("x", 2**53, 2**53 + 1)]))

    def test_schema_change_needs_audit(self):
        a, b = pa.table({"x": [1]}), pa.table({"x": [1.]})
        self.assertTrue(verify_copy(a, b, []))
        self.assertFalse(verify_copy(a, b, [audit("x.dtype", "int64", "double", -1)]))

    def test_audit_chain(self):
        a, b = pa.table({"x": [1]}), pa.table({"x": [3]})
        self.assertFalse(verify_copy(a, b, [audit("x", 1, 2), audit("x", 2, 3)]))
        self.assertTrue(verify_copy(a, b, [audit("x", 1, 2), audit("x", 9, 3)]))

    def test_timestamp_preserves_residual(self):
        fi = np.arange(40.)
        ts = (fi / 10 + np.sin(fi) * 0.0001) * 1000
        f = {"frame_index": fi, "timestamp": ts, "field_names": {"timestamp": "timestamp"}}
        r = Result(40)
        r.ep("T_TIMESTAMP_UNIT_MISMATCH", "synthetic")
        r.metrics["timestamp_unit_scale"] = 1000
        rep = Repairer(f, r, 0, dataset.DatasetInfo(Path("."), []), config.FALLBACK_THRESHOLDS, opts())
        rep.table = rep.source = pa.table({"timestamp": ts})
        rep.timestamps()
        after = np.array(rep.table.column("timestamp").to_pylist())
        np.testing.assert_array_equal(after, ts / 1000)
        self.assertGreater(float(np.max(np.abs(after - fi / 10))), 0)
        self.assertFalse(verify_copy(rep.source, rep.table, rep.audit))

    def test_conservative_does_not_normalize_jitter(self):
        f = {"frame_index": np.arange(40.), "timestamp": np.arange(40.) / 10,
             "field_names": {"timestamp": "timestamp"}}
        r = Result(40)
        r.row(np.ones(40, bool), "T_TIMESTAMP_JITTER")
        rep = Repairer(f, r, 0, dataset.DatasetInfo(Path("."), []), config.FALLBACK_THRESHOLDS, opts())
        rep.timestamps()
        self.assertIsNone(rep.table)
        self.assertTrue(rep.candidates)

    def test_weak_shift_not_applied(self):
        r = Result(40)
        r.metrics["streams"] = {"image": {"shift_frames": 3, "shift_path_complete": True,
            "shift_frames_consecutive": True, "shift_evidence": [{"method": "双臂运动时滞(场景相机)",
                "tier": "弱", "implied": 3, "r": .335, "gain": .18, "qualifies": True, "agrees": True}]}}
        rep = Repairer({}, r, 0, None, config.FALLBACK_THRESHOLDS, opts())
        rep.stream_shift()
        self.assertIsNone(rep.table)
        self.assertTrue(rep.candidates)

    def test_strong_contradiction_blocks_shift(self):
        r = Result(40)
        ev = [{"method": "本臂运动时滞", "tier": "中", "implied": 3, "r": .9, "gain": .2, "qualifies": True, "agrees": True},
              {"method": "跨相机运动相关(other)", "tier": "弱", "implied": -5, "r": .95, "gain": .3, "qualifies": False, "agrees": False}]
        r.metrics["streams"] = {"image": {"shift_frames": 3, "shift_path_complete": True, "shift_frames_consecutive": True, "shift_evidence": ev}}
        rep = Repairer({}, r, 0, None, config.FALLBACK_THRESHOLDS, opts())
        rep.stream_shift()
        self.assertIsNone(rep.table)
        self.assertIn("矛盾", rep.rejected[0])

    def dedup(self, first, second, **kw):
        a = fake_result(1, len(first), first, first)
        b = fake_result(2, len(second), second, second, task=kw.get("task", 0))
        if "times" in kw:
            a["timestamp_final"] = np.arange(len(first)).tolist()
            b["timestamp_final"] = kw["times"]
        return governance([a, b], opts())

    def test_dedup_different_task_kept(self):
        seq = list(map(str, range(50)))
        self.assertEqual(int(self.dedup(seq, seq, task=1)[2]["train"].sum()), 50)

    def test_dedup_reversed_order_kept(self):
        seq = list(map(str, range(50)))
        self.assertEqual(int(self.dedup(seq, seq[::-1])[2]["train"].sum()), 50)

    def test_partial_overlap_unique_context_kept(self):
        seq = list(map(str, range(100)))
        # Old row-set removal discarded these ten unique frames after clipping.
        second = seq[:40] + ["unique" + str(i) for i in range(10)]
        self.assertEqual(int(self.dedup(seq, second)[2]["train"].sum()), 50)

    def test_dedup_timing_different_kept(self):
        seq = list(map(str, range(50)))
        self.assertEqual(int(self.dedup(seq, seq, times=(np.arange(50)*2).tolist())[2]["train"].sum()), 50)

    def test_dedup_unknown_task_kept(self):
        seq = list(map(str, range(50)))
        self.assertEqual(int(self.dedup(seq, seq, task=None)[2]["train"].sum()), 50)

    def test_invalid_options_rejected(self):
        for kw in ({"min_clip_frames": 0}, {"min_clip_frames": -1}, {"low_value_sample_weight": float("nan")},
                   {"typo": True}, {"timestamp_policy": "unsafe"}, {"expected_image_shape": [0, 224, 3]}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                validate_options(opts(**kw))
        for workers in (-1, 0, True):
            with self.assertRaises(ValueError):
                validate_options(opts(), workers=workers)

    def test_empty_input_does_not_touch_output(self):
        with tempfile.TemporaryDirectory(prefix="refsync-safety-") as tmp:
            root = Path(tmp)
            (root / "empty").mkdir()
            with self.assertRaises(ValueError):
                run(root / "empty", root / "out", workers=1, log=lambda *_: None)
            self.assertFalse((root / "out").exists())

    def test_dataset_fps_and_camera_shapes(self):
        with tempfile.TemporaryDirectory(prefix="refsync-safety-") as tmp:
            root = Path(tmp)
            (root / "meta").mkdir()
            info = {"fps": 10, "features": {"image": {"dtype": "image", "shape": [224,224,3]},
                                             "wrist": {"dtype": "image", "shape": [112,112,3]}}}
            (root / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
            ds = dataset.discover(root)
            self.assertEqual(ds.image_shapes["image"], (224,224,3))
            self.assertEqual(ds.image_shapes["wrist"], (112,112,3))
            for fps in (0, -1, float("nan")):
                with self.assertRaises(ValueError):
                    dataset.discover(root, {"fps": fps})

    def test_unmarked_output_not_moved(self):
        with tempfile.TemporaryDirectory(prefix="refsync-safety-") as tmp:
            root = Path(tmp)
            (root / "report").mkdir()
            (root / "report" / "user.txt").write_text("preserve", encoding="utf-8")
            with self.assertRaises(PathGuardError):
                _prepare_output(root, root / "input", opts(), lambda *_: None)
            self.assertEqual((root / "report" / "user.txt").read_text(), "preserve")
            self.assertFalse((root / "_previous_runs").exists())

    def test_timestamp_keeps_field_metadata(self):
        n = 40
        f = {"frame_index": np.arange(n, dtype=float), "timestamp": np.arange(n)*100., "field_names": {"timestamp": "timestamp"}}
        r = Result(n)
        r.ep("T_TIMESTAMP_UNIT_MISMATCH", "synthetic")
        r.metrics["timestamp_unit_scale"] = 1000
        rep = Repairer(f, r, 0, dataset.DatasetInfo(Path("."), []), config.FALLBACK_THRESHOLDS, opts())
        field = pa.field("timestamp", pa.float64(), metadata={b"unit": b"recorded"})
        rep.table = rep.source = pa.Table.from_arrays([pa.array(f["timestamp"])], schema=pa.schema([field]))
        rep.timestamps()
        self.assertFalse(verify_copy(rep.source, rep.table, rep.audit))

    def test_video_never_training_approved(self):
        with tempfile.TemporaryDirectory(prefix="refsync-safety-") as tmp:
            root = Path(tmp)
            state = [0.,0.,0.,1.,0.,0.,0.,1.,0.,.5]*2
            t = pa.table({"frame_index": list(range(40)), "timestamp": (np.arange(40)/10).tolist(), "index": list(range(40)),
                          "episode_index": [0]*40, "task_index": [0]*40, "state": [state]*40, "actions": [state]*40})
            p = root / "episode_000000.parquet"
            pq.write_table(t, p)
            ds = dataset.DatasetInfo(root, [], image_columns=[], video_columns=["camera"])
            f = features.extract(p, {"image_columns": [], "state_dim": 20, "action_dim": 20})
            r = detect.analyse(f, 0, ds, config.FALLBACK_THRESHOLDS, opts())
            self.assertIn("C_MODALITY_UNSUPPORTED", r.ep_codes)
            self.assertTrue(r.blocked_rows().all())
            self.assertFalse(r.metrics["value_evaluable"])
            self.assertEqual(r.metrics["value_coverage"], "1/2")

    def test_training_task_uses_final_metadata(self):
        with tempfile.TemporaryDirectory(prefix="refsync-safety-") as tmp:
            root = Path(tmp)
            r = fake_result(0, 40, list(map(str, range(40))), list(map(str, range(40))), task=0)
            r["final"]["metrics"]["task_index"] = 1
            ds = dataset.DatasetInfo(root, [], tasks={0: "A", 1: "B"})
            s = build_outputs([r], [], ds, config.FALLBACK_THRESHOLDS, opts(), root, root / "governed", "synthetic")
            self.assertEqual(s["dataset_value"]["task_trainable_frames"], {"0": 0, "1": 40})

    def loader_fixture(self, root):
        ds, out = root / "dataset", root / "out"
        (ds / "data").mkdir(parents=True)
        pq.write_table(pa.table({"frame_index": [0, 1], "timestamp": [0., .1]}), ds / "data" / "ep.parquet")
        clip = {"episode_index": 0, "clip_id": "c", "start_row": 0, "end_row": 1, "start_frame": 0,
                "end_frame": 1, "frames": 2, "source": "data/ep.parquet", "origin": "original", "sample_weight": 1}
        _w(out / "governed" / "clips.csv", [clip])
        _w(out / "governed" / "mask_index.csv", [{"episode_index": 0, "mask_file": "mask.csv", "source_file": "data/ep.parquet", "rows": 2}])
        masks = [{"row": i, "frame_index": i, "train_keep": True, "frame_valid": True, "clip_id": "c", "sample_weight": 1} for i in range(2)]
        _w(out / "governed" / "mask.csv", masks)
        return ds, out, clip, masks

    def test_loader_original_path_and_optimized_checks(self):
        with tempfile.TemporaryDirectory(prefix="refsync-safety-") as tmp:
            ds, out, clip, masks = self.loader_fixture(Path(tmp))
            self.assertEqual(sum(c["table"].num_rows for c in iter_clips(ds, out)), 2)
            masks[0]["frame_valid"] = False
            _w(out / "governed" / "mask.csv", masks)
            with self.assertRaises(ValueError):
                list(iter_clips(ds, out))

    def test_loader_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory(prefix="refsync-safety-") as tmp:
            ds, out, clip, _ = self.loader_fixture(Path(tmp))
            clip["source"] = "../outside.parquet"
            _w(out / "governed" / "clips.csv", [clip])
            with self.assertRaises(ValueError):
                list(iter_clips(ds, out))

    def test_loader_truncated_clip_rejected(self):
        with tempfile.TemporaryDirectory(prefix="refsync-safety-") as tmp:
            ds, out, clip, _ = self.loader_fixture(Path(tmp))
            clip["end_row"], clip["frames"] = 2, 3
            _w(out / "governed" / "clips.csv", [clip])
            with self.assertRaises(ValueError):
                list(iter_clips(ds, out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
