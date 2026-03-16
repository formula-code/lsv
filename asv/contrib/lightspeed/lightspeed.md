
## Database

A single SQLite file at `{results_dir}/.lightspeed_deps.db` containing:

```sql
-- Which (file, method fingerprints) does each benchmark touch?
CREATE TABLE benchmark_dep (
    benchmark_id     TEXT NOT NULL,
    filename         TEXT NOT NULL,
    method_checksums BLOB NOT NULL,
    fsha             TEXT NOT NULL,
    PRIMARY KEY (benchmark_id, filename)
);

-- Benchmarks for which coverage failed, always run
CREATE TABLE benchmark_meta (
    benchmark_id              TEXT PRIMARY KEY,
    always_affected           INTEGER NOT NULL DEFAULT 0,
    coverage_failure_reason   TEXT
);

-- Baseline timing written by initialize_diffcheck
CREATE TABLE baseline (
    benchmark_id  TEXT PRIMARY KEY,
    median        REAL NOT NULL,
    ci_99_a       REAL NOT NULL,
    ci_99_b       REAL NOT NULL,
    q_25          REAL NOT NULL,
    q_75          REAL NOT NULL,
    repeat        INTEGER NOT NULL,
    number        INTEGER NOT NULL
);
```

No ASV JSON result files are written. No commit hashes are used as identifiers.

---

## Command 1: `asv initialize_diffcheck`

**Args**:
- `--python` / `-E` (reuse `common_args.add_environment()` with `env_default_same=True`)
- `--force` — re-run even if dep DB already exists
- `--verbose`

**Execution sequence**:
1. Load `conf` from `asv.conf.json`
2. Create `ExistingEnvironment` via `get_environments(conf, ["existing:same"])`
3. Discover benchmarks via `Benchmarks.load(conf)` (uses cached `benchmarks.json`) or `Benchmarks.discover(conf, ...)` if not cached
4. **Coverage survey pass**: for each benchmark, use `asv_runner` Python API + `coverage.Coverage` to call `bench.func()` once and record file→fingerprint dependencies. Store in `.lightspeed_deps.db`. (Reuse `fingerprint.py` and survey logic from lightspeed.)
5. **Baseline timing pass**: call `run_benchmarks(benchmarks, env, ...)` from `asv/runner.py` with the full benchmark set. Write timing results directly to the `baseline` table in `.lightspeed_deps.db`. No JSON files, no commit hashes.
6. Print summary.

---

## Command 2: `asv measure_impacted`

**File**: `asv/commands/measure_impacted.py`
**Class**: `MeasureImpacted(Command)`

**Arguments**:
- `--changed-files` — one or more file paths (explicit, for RL agent integration)
- `--from-git-diff` — alternative: auto-detect changed files via `git diff HEAD`
- `--step-id` — optional label for this run (e.g. trajectory counter, timestamp, UUID); ignored in the PR version, used by fork-only persistence extension
- `--python` / `-E` (same as above)
- `--bench` (pass-through for additional filtering on top of dep-based selection)
- `--verbose`

**Execution sequence**:
1. Load `conf`, create `ExistingEnvironment`
2. Load `Benchmarks` from `benchmarks.json`
3. Open `.lightspeed_deps.db`; read `baseline` table into memory
4. Resolve `changed_files` to absolute paths; compute current fsha + method checksums for each changed file
5. Call `db.get_affected_benchmark_ids(changed_files_with_fingerprints)` → set of affected `BenchmarkId`s
6. Build affected name set; call `benchmarks.filter_out(all_names - affected_names)` to get a filtered `Benchmarks` object
7. Call `run_benchmarks(filtered_benchmarks, env, ...)` — returns timing results in memory; nothing written to disk in the PR version
8. Compute per-benchmark deltas by comparing run results against the `baseline` table rows
9. Call `_on_step_results(results, deltas, step_id)` — no-op in the PR version (overridden by fork subclass)
10. Print per-benchmark delta table; exit non-zero if any regression exceeds `--factor`

**Output format** (stdout, machine-readable option):
```
benchmark                           baseline    current     delta
benchmarks.TimeSuite.time_add_arr   1.23ms      1.45ms     +18.0%
```

---