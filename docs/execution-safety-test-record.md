# Execution safety verification record

Runtime implementation commit: `91d5b094ffea02f40ffdc2a9a6d8e37dfcb8c25f`.

The bounded patch was applied to `cc65741959eaf048a5be9ce625202e980eb124d3`, syntax-checked, and tested before publishing the resulting source commit. Exact source SHA-256 values and the patch are retained with the run artifacts.

- Successful run: https://github.com/ecofree/hv-studio/actions/runs/43609428623
- Windows runner, Python 3.13.17, repository `requirements.core.lock.txt`.
- Dependency consistency check and `compileall app bin tests`: passed.
- Focused execution/recovery/security suite: **93 passed, 0 failed, 0 skipped** (JUnit: 18.698 seconds).
- Includes actual separate-process kill/reacquire of the kernel episode lease, plus thread exclusion, nested ownership, queue contention, Studio worker admission, original-budget preservation, and API HTTP-boundary tests.
- First run https://github.com/ecofree/hv-studio/actions/runs/43609063946 had 91 passes and two new test-harness failures (existing temporary directory; lazy router enumeration). Both were corrected without relaxing security expectations; the failed evidence remains available.

The temporary write-capable publication workflow and encoded updater have been removed after publication. The retained offline workflow has read-only permissions and also executes the complete production suite. Its latest JUnit report is authoritative for that broader suite; the 93 focused passes above do not imply the full suite or real production acceptance passed.

No real TTS/image provider, local model worker, historical media modification, or content approval was performed. This is offline engineering evidence, not a production-ready certificate. Main remains unchanged and PR #1 remains Draft.
