# Lightspeed

Lightspeed is an extension to ASV that enables selective benchmark re-running. Given a set of changed source files, it runs only the benchmarks whose execution paths touch those files and reports the performance delta against a stored baseline.

The primary use case is RL training loops for code generation agents: after each trajectory step, you want to measure the performance impact of the agent's changes without running the entire benchmark suite.

---

## How It Works

### Two-level change detection

Lightspeed borrows its dependency tracking mechanism from [testmon](https://github.com/tarpas/pytest-testmon).

**Level 1 — file SHA (fast gate)**: Each source file is hashed (git-style SHA1 of contents). If the hash hasn't changed since the last survey, the file is skipped entirely.

**Level 2 — method fingerprints (precision)**: If a file's SHA has changed, Lightspeed doesn't automatically flag every benchmark that touches it. Instead, it compares the CRC32 checksums of individual code blocks (AST-level function bodies and branch segments) against what was recorded for each benchmark during the survey. A benchmark is only flagged if at least one of the specific blocks it executed has a changed checksum.

**Example**: A file has functions `foo()`, `bar()`, `baz()`. A benchmark only calls `foo()`. Editing `bar()` changes `bar()`'s checksum — but the benchmark never recorded `bar()`'s checksum, so it is not selected. Only if `foo()`'s checksum changes does the benchmark get selected.

### Two phases

**Phase 1 — coverage survey** (`initialize_diffcheck`): Each benchmark is run once under `coverage.Coverage()`. The covered file→line mapping is converted to method-level CRC32 fingerprints and stored in a SQLite database (`.lightspeed_deps.db`). Baseline timing is also recorded in the same pass.

**Phase 2 — selective measurement** (`measure_impacted`): The changed files are fingerprinted, compared against stored checksums, and the affected benchmark set is computed. Only those benchmarks are re-run, and their results are compared against the stored baseline.

### Failure modes

Coverage instrumentation fails silently in some cases. Any benchmark where coverage cannot be reliably measured is marked `always_affected` and will always be included in `measure_impacted` runs, regardless of what changed. The cases that trigger this:

| Failure mode | Trigger |
|---|---|
| `execv` / process replacement | Benchmark calls `os.execv` or similar |
| Low-level threads | Benchmark imports `_thread` (not `threading`) |
| Tracer clobbering | Benchmark installs its own `sys.settrace` |
| Profiler clobbering | Benchmark installs its own `sys.setprofile` |

### False negatives

The dependency check only flags benchmarks when a stored fingerprint *disappears* from the current set. If code is added that creates an entirely new block without modifying any existing block, no stored checksum goes missing and the benchmark is not flagged.

In practice, adding code inside a function body changes the surrounding block's checksum and is correctly detected. The pathological case — appending a new isolated function to a file that the benchmark never calls — is also not a concern, since a benchmark that doesn't call the new function isn't affected by it.

The `always_affected` fallback and the `hotfile-threshold` mechanism (see below) are the two main tools for managing false positives. There is no automated recall guarantee in production; it was validated during development using a full-sweep vs selective-sweep comparison.

---

## Setup

Lightspeed lives entirely inside the ASV fork:

```
asv/contrib/lightspeed/   — library code (fingerprint, dep DB, survey, session)
asv/commands/             — CLI commands (initialize_diffcheck, measure_impacted)
```

It requires an `asv.conf.json` and a benchmark suite discoverable by ASV. No additional configuration files are needed.

---

## Phase 1: `asv initialize_diffcheck`

Run once to build the dependency database and record baseline timing.

```bash
asv initialize_diffcheck --source-root <path>
```

**`--source-root PATH`** (required)
Root of the source package to track. Only files within this directory are recorded in the dependency table. Pass the root of the package you are benchmarking, not the benchmark directory.

```bash
asv initialize_diffcheck --source-root /workspace/repo/shapely
```

**`--force`**
Re-run both the coverage survey and baseline timing passes even if a baseline already exists. Use this after significant changes to the source package or benchmark suite.

**Timing controls** — these affect only the baseline timing pass, not the coverage survey:

| Flag | Default | Effect |
|---|---|---|
| `--rounds N` | 2 (benchmark default) | Number of timing rounds. More rounds = more accurate baseline, longer runtime. |
| `--repeat N` | auto (1–10) | Samples collected per round. Auto-scales based on round count. |
| `--warmup-time SECS` | auto | Warmup time before timing begins (≈1 s for multi-round, ≈5 s for single-round). |

```bash
# Thorough baseline: 3 rounds
asv initialize_diffcheck --source-root src/ --rounds 3

# Fast baseline: 1 round, minimal warmup
asv initialize_diffcheck --source-root src/ --rounds 1 --warmup-time 0.5
```

**Output**: A `.lightspeed_deps.db` SQLite file at `{results_dir}/.lightspeed_deps.db` containing three tables:
- `benchmark_dep` — file→method fingerprint mapping per benchmark
- `benchmark_meta` — `always_affected` flag and failure reason per benchmark
- `baseline` — median, CI, quartiles per benchmark

---

## Phase 2: `asv measure_impacted`

Run after each code change to measure only the affected benchmarks.

```bash
# Explicit file list (for RL agents or CI pipelines)
asv measure_impacted --changed-files src/foo.py src/bar.py

# Auto-detect from git working tree
asv measure_impacted --from-git-diff
```

Exactly one of `--changed-files` or `--from-git-diff` is required.

**`--changed-files FILE [FILE ...]`**
Explicit list of changed source files. Paths are resolved to absolute before lookup. This is the recommended mode for RL agent integration where the agent knows exactly which files it modified.

**`--from-git-diff`**
Detect changed files via `git diff HEAD --name-only`. Includes all uncommitted working-tree changes against the last commit. Does not require committing between steps.

**`--step-id ID`**
An optional label for this run (e.g. trajectory counter, timestamp, UUID). Has no effect in the base version — it is passed to the `_on_step_results()` hook, which is used by the fork-only persistence extension.

**Timing controls** — same flags as `initialize_diffcheck`:

| Flag | Recommended for RL | Notes |
|---|---|---|
| `--rounds N` | `--rounds 1` | 1 round is usually sufficient for delta measurement |
| `--repeat N` | default | |
| `--warmup-time SECS` | default | |

```bash
# Fast per-step measurement
asv measure_impacted --changed-files src/foo.py --rounds 1
```

**Hotfile exclusion — `--hotfile-threshold FRAC`** (default `0.5`)

Infrastructure files (decorators, base classes, utilities) are often covered by a large fraction of benchmarks. When such a file changes, nearly every benchmark would be selected — a false positive rate that defeats the purpose of selective running.

Files covered by more than `FRAC` of the total benchmark population are treated as "hotfiles" and excluded from dependency matching. The threshold applies at query time and can be tuned without re-running `initialize_diffcheck`.

```bash
# Default: files covered by >50% of benchmarks are excluded
asv measure_impacted --changed-files src/decorators.py

# Disable hotfile exclusion (always run matching benchmarks)
asv measure_impacted --changed-files src/decorators.py --hotfile-threshold 1.0

# More aggressive: exclude files covered by >25% of benchmarks
asv measure_impacted --changed-files src/foo.py --hotfile-threshold 0.25
```

**Tradeoff**: A genuine performance regression in a hotfile will not be detected unless `--hotfile-threshold 1.0` is set or the file is listed explicitly. For infrastructure files this is usually acceptable.

**Output**:
```
benchmark                           baseline      current       delta
benchmarks.TimeSuite.time_add_arr     1.230ms      1.450ms    +18.0%
benchmarks.TimeSuite.time_mul_arr     2.100ms      2.098ms     -0.1%
```

---

## Python API

All functionality is available as importable Python objects, without shelling out to the CLI.

```python
from asv.contrib.lightspeed import LightspeedSession
```

### `LightspeedSession`

```python
session = LightspeedSession(
    config_path,          # path to asv.conf.json
    overrides={           # optional in-memory config overrides
        "results_dir": "/output/results",
        "repo": "/workspace/repo",
    },
    machine="ci",         # machine name for result files (default: hostname)
    python="same",        # python spec (default: current interpreter)
)
```

`overrides` accepts any key valid in `asv.conf.json`. Common uses: `results_dir`, `html_dir`, `repo`, `branches`, `environment_type`. Relative paths in the config are resolved against the config file's parent directory, not the caller's cwd.

**Properties**: `config_path`, `benchmark_dir`, `results_dir`, `env_dir`, `repo`, `machine`, `python`, `deps_db_path`

### `session.initialize_diffcheck()`

```python
result = session.initialize_diffcheck(
    source_root,          # str or Path — source package root
    force=False,          # re-run even if baseline exists
    rounds=None,          # int — timing rounds (None = benchmark default)
    repeat=None,          # int — samples per round (None = auto)
    warmup_time=None,     # float — warmup seconds (None = auto)
)
```

Returns `InitResult`:

```python
result.benchmarks_discovered   # list[str] — all benchmark ID strings
result.benchmarks_impactable   # list[str] — benchmarks with dep data
result.source_files_covered    # int — distinct source files recorded
result.deps_db_path            # Path — absolute path to .lightspeed_deps.db
result.timing.total_s          # float — wall-clock seconds
result.timing.phases           # dict — {"coverage": s, "benchmarking": s}
```

### `session.measure_impacted()`

```python
result = session.measure_impacted(
    from_git_diff=False,      # detect changes via git diff HEAD
    changed_files=None,       # list[str | Path] — explicit file list
    rounds=None,
    repeat=None,
    warmup_time=None,
)
```

Exactly one of `from_git_diff=True` or `changed_files=[...]` must be provided.

Returns `MeasureResult`:

```python
result.benchmarks              # dict[str, BenchmarkDelta]
result.selected_count          # int — benchmarks re-run
result.total_count             # int — total benchmarks in suite
result.skipped_count           # int — benchmarks skipped (unaffected)
result.timing.total_s          # float

# Per-benchmark:
delta = result.benchmarks["benchmarks.TimeSuite.time_add_arr"]
delta.baseline                 # float | None — baseline median in seconds
delta.current                  # float | None — current median in seconds
delta.delta_pct                # float | None — % change (positive = slower)
delta.baseline_str             # str — e.g. "1.230ms"
delta.current_str              # str — e.g. "1.450ms"
delta.params                   # dict | None — param dict for parameterised benchmarks
```

### `session.get_results()`

```python
results = session.get_results()
# dict[str, float | None] — benchmark ID → baseline median in seconds
```

### Exceptions

```python
from asv.contrib.lightspeed import ASVError, ConfigError, BenchmarkError, NoBenchmarksError
```

Partial failures (some benchmarks failed to run) are reported as `None` values in the result dicts, not as exceptions. `BenchmarkError` is raised only when the entire operation cannot proceed.

### Full pipeline example

```python
from asv.contrib.lightspeed import LightspeedSession

session = LightspeedSession(
    "/workspace/repo/benchmarks/asv.conf.json",
    overrides={
        "results_dir": "/output/results",
        "repo": "/workspace/repo",
    },
    machine="dockertest",
)

# Phase 1: run once at trajectory start
init = session.initialize_diffcheck(
    source_root="/workspace/repo/shapely",
    rounds=3,
)
print(f"Baseline recorded for {len(init.benchmarks_discovered)} benchmarks")

# ... agent modifies source files ...

# Phase 2: run after each step
result = session.measure_impacted(
    changed_files=agent.modified_files,
    rounds=1,
)

for name, delta in result.benchmarks.items():
    print(f"{name}: {delta.baseline_str} -> {delta.current_str} ({delta.delta_pct:+.1f}%)")

print(f"Ran {result.selected_count}/{result.total_count} benchmarks in {result.timing.total_s:.1f}s")
```

---

## Database Schema

Everything is stored in `{results_dir}/.lightspeed_deps.db` (SQLite).

```sql
-- Which method-level fingerprints does each benchmark touch?
CREATE TABLE benchmark_dep (
    benchmark_id     TEXT NOT NULL,
    filename         TEXT NOT NULL,
    method_checksums BLOB NOT NULL,  -- pickled dict of {block_id: crc32}
    fsha             TEXT NOT NULL   -- SHA1 of file at survey time
);

-- Benchmarks for which coverage failed → always included in measure_impacted
CREATE TABLE benchmark_meta (
    benchmark_id            TEXT PRIMARY KEY,
    always_affected         INTEGER NOT NULL DEFAULT 0,
    coverage_failure_reason TEXT
);

-- Baseline timing recorded by initialize_diffcheck
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

No ASV JSON result files are written. No commit hashes are used.

---

## Fork-Only: Step Result Persistence

The base commands are designed to be PR-able to upstream ASV. Step timing results are ephemeral — computed, printed, then discarded.

The fork includes a persistence extension in `asv/contrib/lightspeed/` that stores step results in the same SQLite database, queryable directly by the RL training loop.

**Additional table** (added by `step_db.py`):

```sql
CREATE TABLE IF NOT EXISTS step_result (
    step_id       TEXT NOT NULL,   -- caller-supplied label (counter, UUID, timestamp)
    benchmark_id  TEXT NOT NULL,
    median        REAL NOT NULL,
    ci_99_a       REAL NOT NULL,
    ci_99_b       REAL NOT NULL,
    q_25          REAL NOT NULL,
    q_75          REAL NOT NULL,
    repeat        INTEGER NOT NULL,
    number        INTEGER NOT NULL,
    delta         REAL NOT NULL,   -- fractional change from baseline
    PRIMARY KEY (step_id, benchmark_id)
);
```

**`PersistentMeasureImpacted`** (`persistent_measure.py`) subclasses `MeasureImpacted` and overrides `_on_step_results(result, step_id)` to write to this table.

**Usage** — pass `--step-id` to name each step:

```bash
asv measure_impacted --changed-files src/foo.py --step-id step_001
```

**Querying from the RL loop**:

```python
import sqlite3
con = sqlite3.connect(".lightspeed_deps.db")
rows = con.execute(
    "SELECT benchmark_id, delta FROM step_result WHERE step_id = ?",
    ("step_001",),
).fetchall()
reward = -sum(r["delta"] for r in rows if r["delta"] > 0)
```

The `step_id` is any caller-supplied string — trajectory counter, wall-clock timestamp, UUID, or git-style label. No git history is involved.
