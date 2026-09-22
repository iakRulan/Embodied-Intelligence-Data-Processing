# RefSync-QA v3.2 Deployment Guide

## 1. Submission files

- `RefSync_QA_v3_2_Submission.zip`: self-contained operator, documentation and non-sensitive validation summaries.
- `Algorithm_Description_v3_2.pdf`: reviewer-facing algorithm description.
- `Algorithm_Description_v3_2.md`: editable source of the description.
- `Submission_SHA256.txt`: archive integrity hash (not a proof of algorithm accuracy).

Upload these files to the organizer-designated `Raw_data` directory. Preserve older submissions. ZIP extraction yields one English-named directory, `refsync_qa_v3_2/`. File names, archive paths and deployment examples are ASCII. Chinese explanations and issue labels in UTF-8 files are intentional.

## 2. Runtime

The observed local environment is Python 3.13.5, NumPy 2.5.2, PyArrow 25.0.1 and Pillow 12.3.0. No GPU or model weights are required. `requirements.txt` declares compatible dependency ranges; only the documented local combination has full-data evidence. Optional pandas/openpyxl produce an XLSX summary. The supplied Dockerfile is an unvalidated deployment example, not proof of a platform image build.

```bash
cd /opt/refsync_qa_v3_2
python -m pip install -r requirements.txt
python tools/test_safety.py
python -O tools/test_safety.py
python run.py --input /data/test --output /data/result --workers 4 --strict
python tools/load_governed.py --dataset /data/test --output /data/result
```

To calibrate from a known-clean reference, append `--reference /data/reference`. Without it, the shipped complete-20-reference calibration is used. Keep input/reference read-only and output in a separate writable directory. A fresh English output path per run is recommended.

For argument-free platform execution set `RSQA_INPUT=/data/test`, `RSQA_OUTPUT=/data/result`, `RSQA_WORKERS=4` and optionally `RSQA_REFERENCE=/data/reference`. The CLI takes explicit arguments first. Windows examples: `H:/refsync_data/test`, `H:/refsync_data/reference`, `H:/refsync_results/run_001`. Do not hardcode another user's workspace paths.

## 3. Integration contract

This release exposes `python run.py` and `from refsync_qa import run`. It does not invent a proprietary platform SDK entry point. If the platform requires a callable, argument schema, mounted resource URI, dependencies image or result registration API, the administrator must provide that contract and test execution. File-resource upload alone is not operator deployment.

Use `--no-repair` for initial format review. Production default is `--timestamp-policy conservative`; `nominal` is an explicit estimate policy and is not recommended without a nominal-time contract. Do not use `--clean-previous` during review; marked previous outputs are archived by default. Limit workers on low-memory hosts; one episode, including embedded image payloads, is held per worker.

Exit 0: batch completed, possibly with quality findings. Exit 2: invalid input/options/path protection. Exit 3: at least one processing error with `--strict`. Review `report/summary.json` in all cases; scores do not replace blocking issue codes.

## 4. Outputs and acceptance

`report/` contains complete episode/frame/interval results, five-dimensional scores, issue dictionary and actual thresholds. `governed/` contains repaired copies only, audit, manifest, masks and clips; it is an overlay, not a standalone dataset. Train through `tools/load_governed.py`, and never cross clip boundaries.

```bash
python tools/acceptance.py --input /data/test --reference /data/reference --output /data/acceptance --workers 4
python tools/regression_probes.py --reference /data/reference --output /data/regression
python tools/selftest.py --reference /data/reference --output /data/selftest --workers 4
```

The acceptance tool hashes all existing input/reference files before and after, re-reads every repair audit, then actually loads all retained clips. Synthetic selftests contain nominal-time cases explicitly; they are not real-data precision/recall and do not change the conservative default.

The included `validation/full89_episode_report.csv` and `validation/v3.2_acceptance.json` describe the local official first-round data only. They are not measurements on the platform's second-round dataset. No raw/repair/synthetic Parquet, pictures, videos, personal contacts, credentials or cached outputs are bundled.

## 5. Review limitations

24 unflagged / 1 value-only / 64 review-needed episodes are algorithm decisions, not labeled truth. Default governance yields 8 Parquet copies and 1 metadata patch, but only 6 previously review-needed episodes stop needing defect review. 49 episodes provide 61 loadable clips and 8,881 frames. Independent clocks, semantic scene coverage, dedicated occlusion/corruption benchmarks and downstream training improvement remain unverified. See `Algorithm_Description_v3_2.md` for full methods and boundaries.
