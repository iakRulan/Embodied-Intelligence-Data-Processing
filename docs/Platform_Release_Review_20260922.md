# RefSync-QA v3.2 Platform Release Review

Date: 2026-09-22. This record covers local delivery preparation, not platform approval.

## Changes and scope

- Added the formal 10-page algorithm description and editable Markdown, covering schema provenance, detection, calibration, score formulas, repair gates, training overlay, real cases, evaluation and limitations.
- Added an English deployment guide and repeatable PDF/package/review tools. Updated README links and runtime examples to be self-contained and English-named.
- Replaced Chinese path placeholders in six Python docstrings. Compared all 19 delivered Python files to commit `42bced4a0d108a88f46801b62a7a91583ce1d000`: executable ASTs are identical after removing docstrings. This is a delivery/documentation revision, not a new algorithm-effect claim.
- Package excludes old-version summaries, raw/repaired/synthetic data, images, videos and credentials. One original absolute local path embedded in the corrupt-file exception was rebased to `/data/test` in the report attachment. No episode findings or metrics were changed.

## Verification

- 22 current Python files parsed successfully (19 operator/test files, 3 documentation/release utilities).
- 24 unique safety tests passed in normal mode and in `python -O`; repeated successfully from the final English release directory.
- ZIP CRC check passed; all 31 archive entry names are ASCII. The manifest verifies SHA-256 for 30 payload files; the manifest itself is protected by the ZIP hash, not self-hashed.
- Rendered and visually inspected all 10 PDF pages; no clipping, broken tables or missing Chinese characters observed.
- Ran full acceptance from a ZIP-extracted copy in an English directory: 89 test episodes, 20 reference episodes, 115 input files unchanged, 8 repaired copies independently reread, 61 clips actually loaded, 8,881 training frames, 49 contributing episodes, zero acceptance failures.
- Compared the rerun with the prior shipped-calibration report: 89 rows x 47 non-timing fields, zero differences. Final delivered Python hashes match the tested extracted source for all 19 files.

## Final artifact identity

Archive: `RefSync_QA_v3_2_Submission.zip`.

SHA-256: `233e3ddea3cd962d028fc5fe433ed1f674a3bb89091575f30a5b2ed2dd7e2233`.

Size: 348,194 bytes. Internal root: `refsync_qa_v3_2/`.

Files alongside the archive: `Algorithm_Description_v3_2.pdf`, `Algorithm_Description_v3_2.md`, `Submission_SHA256.txt`.

The public repository holds source and Markdown only; the submission archive and PDF are local artifacts intended for the organizer's `Raw_data` file resource directory.

## Platform status and remaining boundary

The specified platform login page was opened. It requires a graphical CAPTCHA; action-time permission was requested and has not been received when this record was written. No completed login, inspection of the existing remote submission, upload, overwrite or platform execution has been verified. The login page is retained for user handoff. Browser credentials are not saved in this repository or submission.

File upload is separate from operator deployment and the organizer's effectiveness review. The platform SDK/entry-point contract, actual second-round data effects, independent-clock synchronization, semantic value coverage and downstream training benefit remain unverified. Local tests do not guarantee correctness on all unknown data.
