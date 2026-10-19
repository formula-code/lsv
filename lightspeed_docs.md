# Lightspeed

Lightspeed is an extension to asv that re-runs only some benchmarks. You give it the changed source files. It runs only the benchmarks that executed code in those files, and it reports the change in time against a stored baseline.

The main use is a training loop for code agents. After each step you measure the effect of the agent's patch without running the full benchmark suite.

The code is in this fork of asv:

```
asv/contrib/lightspeed/
├── session.py          LightspeedSession: the Python API used by both commands
├── survey.py           coverage pass: which files and blocks each benchmark runs
├── fingerprint.py      file SHA and CRC32 block checksums
├── process_code.py     block parser, copied from pytest-testmon
├── deps_db.py          SQLite database: dependencies, failures, baseline
└── project_imports.py  finds where benchmark processes import the project from
asv/commands/
├── initialize_diffcheck.py   asv initialize_diffcheck
└── measure_impacted.py       asv measure_impacted
```

You need an `asv.conf.json` and a benchmark suite that asv can discover. Lightspeed needs no other configuration file. The benchmarks run in the current Python interpreter (`existing:same`), so the project must already be installed there.

---

## How it works

### Two levels of change detection

The dependency tracking comes from [pytest-testmon](https://github.com/tarpas/pytest-testmon).

1. **File SHA.** Each source file is hashed (git-style SHA1 of its contents). If the SHA of a file is the same as at survey time, the file is not checked further.
2. **Block checksums.** If the SHA changed, Lightspeed compares CRC32 checksums of code blocks (function bodies and module-level segments, from the AST). For each benchmark, the database stores the checksums of only the blocks that the benchmark executed. A benchmark is selected when at least one of its stored checksums is not in the current file.

Example: a file has `foo()`, `bar()` and `baz()`. A benchmark calls only `foo()`. An edit to `bar()` does not select the benchmark. An edit to `foo()` does.

C, C++ and Cython files (`.c`, `.cpp`, `.pyx`, `.pxd`, `.h`) are stored as one block for the whole file, so any edit to such a file selects all benchmarks that depend on it.

### Selection rule

`LightspeedDB.get_affected_benchmark_ids` selects a benchmark when one of these is true:

1. It is marked `always_affected` (the survey could not measure it, see below).
2. It depends on a changed file, and one of its stored block checksums is not in the current file.
3. It depends on a changed file, and rule 2 selected no benchmark for that file. Then all benchmarks that depend on the file are selected, because the change can be in code that no benchmark covered directly.

A changed file that no benchmark depends on selects nothing.

### Two phases

1. **Survey and baseline** (`initialize_diffcheck`). Each benchmark runs once under Coverage.py. The executed lines are converted to block checksums and stored in `{results_dir}/.lightspeed_deps.db`. Then asv times all benchmarks and stores the baseline in the same database.
2. **Selective measurement** (`measure_impacted`). Lightspeed fingerprints the changed files, selects benchmarks with the rule above, checks where the benchmark processes import the project from, runs only the selected benchmarks, and compares them with the baseline.

### What the survey records

- **Only executed files.** A file is a dependency of a benchmark only when the benchmark executed at least one line in it. Coverage.py with `source=` also lists files that never ran; these are skipped. Files outside `--source-root` and files under `site-packages` or `dist-packages` are not recorded. (lsv#5)
- **Child Python processes.** During the survey, a child Python process started by the benchmark is also measured. Lightspeed writes a temporary coverage config and a `sitecustomize.py`, sets `COVERAGE_PROCESS_START` and puts the folder first on `PYTHONPATH`. The child starts coverage and then runs the environment's own `sitecustomize`, if there is one. The lines from the child data files are added to the lines of the parent. The environment is restored and the folder is deleted before the timing pass. (lsv#9)
- **C extensions.** After the coverage step, Lightspeed reads a copy of `sys.modules` and finds the loaded `.so`/`.pyd` modules. For each one it adds every C, C++ and Cython source file under `{source_root}/{top-level package}` as a dependency. When that folder does not exist, it scans all of `--source-root`. The scan uses a copy of `sys.modules` because a module with a lazy `__getattr__` can import other modules during the scan. (lsv#10)

### Benchmarks that are always run

When the survey cannot measure a benchmark, the benchmark is marked `always_affected` and every `measure_impacted` call runs it. The reason is stored in `benchmark_meta.coverage_failure_reason`:

| Reason | Cause |
|---|---|
| `load_error: ...` | asv_runner could not load the benchmark |
| `_thread import detected (static)` | the benchmark file imports `_thread` or `thread` |
| `setup_error: ...` | `setup` raised an exception |
| `benchmark_skipped` | `setup` skipped the benchmark |
| `runtime_error: ...` | the benchmark body raised an exception |
| `sys.settrace clobbered` | the benchmark changed the trace function |
| `sys.setprofile clobbered` | the benchmark changed the profile function |
| `coverage_read_error: ...` | the coverage data could not be read |
| `uncomputable fingerprint: FILE` | a covered file could not be read |
| `no_source_root_coverage` | the benchmark executed no file under `--source-root` |

### Project shadow check

A task image can contain two copies of the project: the repository as an editable install, and a wheel that another package pulled in. The benchmark processes can import the wheel, and then the timings measure the wrong code.

Before timing, `measure_impacted` finds the top-level import names of the changed files (the first folder with an `__init__.py`: `numpy/core/x.c` gives `numpy`, `src/skimage/a.py` gives `skimage`). It runs a small probe benchmark with the same spawner and launch method as the real benchmarks. The probe first imports the benchmark suite package, because its `__init__.py` can change `sys.path`, and then imports each name. If a package resolves to a file outside the repository root, `measure_impacted` raises `ProjectShadowed` with the paths and times nothing. The repository root is the git top level of the first changed file, or the `repo` setting when that is not a git repository. (lsv#8)

### Limits

- Selection only notices a stored checksum that is no longer in the file. New code that adds a block and changes no existing block that a benchmark ran does not select by rule 2; rule 3 then selects all dependents of the file.
- The database is never cleared. A new survey replaces rows for the same benchmark and file, but rows for files that are no longer covered stay, and so do `always_affected` marks. To rebuild the database, delete `.lightspeed_deps.db` and run `initialize_diffcheck` again.

---

## `asv initialize_diffcheck`

Builds the dependency database and records the baseline.

```bash
asv initialize_diffcheck --source-root /workspace/repo/shapely
```

| Option | Default | Effect |
|---|---|---|
| `--source-root PATH` | required | Root of the source package to track. Only files under this folder are recorded. Give the package, not the benchmark folder. |
| `--force` | off | Run the survey and the timing again even when every benchmark has a baseline. |
| `--rounds N` | benchmark setting (typically 2) | Timing rounds per benchmark. |
| `--repeat N` | auto (1 to 10) | Samples per round. |
| `--warmup-time SECS` | auto | Warmup before timing (about 1 s with several rounds, about 5 s with one). |
| `--launch-method {auto,spawn,forkserver}` | `asv.conf.json`, else `auto` | How asv starts benchmark processes. |
| `-E`, `--environment`, `--python` | `existing:same` | Accepted, but not used: the benchmarks always run in the current interpreter. |
| `-b`, `--bench REGEX` | all | Accepted, but not used: all benchmarks are surveyed and timed. |

The timing options change only the baseline timing, not the survey.

Without `--force`, the command does nothing when every benchmark already has a baseline row. When one or more baselines are missing, it runs both passes for all benchmarks.

```bash
# More accurate baseline
asv initialize_diffcheck --source-root src/mypkg --rounds 3

# Fast baseline
asv initialize_diffcheck --source-root src/mypkg --rounds 1 --warmup-time 0.5
```

---

## `asv measure_impacted`

Runs only the affected benchmarks and prints the change against the baseline.

```bash
# Explicit file list
asv measure_impacted --changed-files src/mypkg/foo.py src/mypkg/bar.py

# Files from the git working tree
asv measure_impacted --from-git-diff
```

| Option | Default | Effect |
|---|---|---|
| `--changed-files FILE [FILE ...]` | | Changed source files. Paths are made absolute from the current folder. |
| `--from-git-diff` | | Changed files from `git diff HEAD --name-only`, run in the current folder. Includes all uncommitted changes to tracked files. |
| `--step-id ID` | none | A label passed to `MeasureImpacted._on_step_results()`. That method does nothing; a subclass can override it to store results. |
| `--rounds N` | benchmark setting (typically 2) | Timing rounds. One round is usually enough for a per-step measurement. |
| `--repeat N` | auto (1 to 10) | Samples per round. |
| `--warmup-time SECS` | auto | Warmup before timing. |
| `--launch-method {auto,spawn,forkserver}` | `asv.conf.json`, else `auto` | How asv starts benchmark processes. |
| `-E`, `--environment`, `--python` | `existing:same` | Accepted, but not used. |
| `-b`, `--bench REGEX` | all | Accepted, but not used. |

Exactly one of `--changed-files` and `--from-git-diff` is required. `initialize_diffcheck` must have run first.

The command exits 0 after a run. It does not compare the change against a threshold. Output:

```
benchmark                            baseline      current      delta
---------------------------------------------------------------------
benchmarks.TimeSuite.time_add_arr     1.230ms      1.450ms     +18.0%
benchmarks.TimeSuite.time_mul_arr     2.100ms      2.098ms      -0.1%
```

Selection is by benchmark name. When one parameter combination of a parameterized benchmark is selected, all its combinations run.

---

## Python API

```python
from asv.contrib.lightspeed import LightspeedSession
```

### `LightspeedSession`

```python
session = LightspeedSession(
    config_path,          # path to asv.conf.json
    overrides={           # optional config overrides, applied in memory
        "results_dir": "/output/results",
        "repo": "/workspace/repo",
    },
    machine="ci",         # machine name (default: hostname)
    python="same",        # interpreter for existing:<python> (default: current)
)
```

`overrides` accepts any key that is valid in `asv.conf.json`. Relative `benchmark_dir`, `results_dir`, `html_dir` and `env_dir` are taken from the folder of the config file, not the current folder. The session creates `{results_dir}/{machine}/machine.json` if it does not exist.

Properties: `config_path`, `benchmark_dir`, `results_dir`, `env_dir`, `repo`, `machine`, `python`, `deps_db_path`.

### `session.initialize_diffcheck()`

```python
result = session.initialize_diffcheck(
    source_root,          # str or Path
    force=False,
    rounds=None,
    repeat=None,
    warmup_time=None,
    base_commit=None,     # measure at this commit instead of the current checkout
)
```

With `base_commit`, the survey and the timing run with the tracked files reset to that commit, and the checkout is restored afterwards. Untracked files, such as new `benchmark_*.py` files, stay in place. `base_commit` does not work with `launch_method="forkserver"`, because that method imports the suite only once. This option is not on the command line.

Returns `InitResult`:

```python
result.benchmarks_discovered   # list[str]: all benchmark IDs
result.benchmarks_impactable   # list[str]: benchmarks with dependency rows
result.source_files_covered    # int: distinct files in the dependency table
result.deps_db_path            # Path to .lightspeed_deps.db
result.timing.total_s          # float: seconds
result.timing.phases           # {"coverage": s, "benchmarking": s}, or None when nothing ran
```

### `session.measure_impacted()`

```python
result = session.measure_impacted(
    from_git_diff=False,
    changed_files=None,   # list[str | Path]; used instead of from_git_diff when given
    rounds=None,
    repeat=None,
    warmup_time=None,
    packages=None,        # import names for the shadow check; None derives them from the changed files
)
```

Returns `MeasureResult`:

```python
result.benchmarks              # dict[str, BenchmarkDelta]
result.selected_count          # int: benchmarks run
result.total_count             # int: benchmarks in the suite
result.skipped_count           # int: benchmarks not run
result.timing.total_s          # float

delta = result.benchmarks["benchmarks.TimeSuite.time_add_arr"]
delta.baseline                 # float | None: baseline median in seconds
delta.current                  # float | None: current median in seconds
delta.delta_pct                # float | None: percent change, positive is slower
delta.baseline_str             # str, e.g. "1.230ms"
delta.current_str              # str, e.g. "1.450ms"
delta.params                   # dict | None: {"p0": ..., "p1": ...} for parameterized benchmarks
```

Parameterized benchmarks have the key `name-index`, where `index` is the position in the product of the parameter lists.

### Baselines

```python
session.get_results()                  # {benchmark_id: baseline median in seconds}
payload = session.export_baselines()   # {benchmark_id: {"median", "ci_99_a", "ci_99_b", "q_25", "q_75", "repeat", "number"}}
session.load_baselines(payload)        # writes the rows; returns the count
```

`load_baselines` needs an existing database. After it, `initialize_diffcheck(force=False)` does nothing when every benchmark has a baseline. Use this to reuse a baseline measured on the same hardware.

### Exceptions

```python
from asv.contrib.lightspeed import ASVError, ConfigError, BenchmarkError, NoBenchmarksError, ProjectShadowed
```

- `ASVError`: base class. Also raised when the database is missing, `source_root` is not a folder, or a git command fails.
- `ConfigError`: the config file cannot be loaded.
- `NoBenchmarksError`: no benchmarks were found.
- `ProjectShadowed`: a project package is imported from outside the repository. `.paths` maps each package to its file.
- `BenchmarkError`: exported, but no code path raises it at present.

A benchmark that fails to run gives `None` values in the result, not an exception.

`check_project_imports(env, benchmark_dir, launch_method, packages, repo_root)` runs the shadow check alone. It returns `{package: file}` or raises `ProjectShadowed`.

### Example

```python
from asv.contrib.lightspeed import LightspeedSession

session = LightspeedSession(
    "/workspace/repo/benchmarks/asv.conf.json",
    overrides={"results_dir": "/output/results", "repo": "/workspace/repo"},
    machine="dockertest",
)

init = session.initialize_diffcheck(source_root="/workspace/repo/shapely", rounds=3)
print(f"Baseline recorded for {len(init.benchmarks_discovered)} benchmarks")

# ... the agent changes source files ...

result = session.measure_impacted(changed_files=modified_files, rounds=1)
for name, delta in result.benchmarks.items():
    print(f"{name}: {delta.baseline_str} -> {delta.current_str} ({delta.delta_pct:+.1f}%)")
print(f"Ran {result.selected_count}/{result.total_count} benchmarks in {result.timing.total_s:.1f}s")
```

---

## Database

All data is in `{results_dir}/.lightspeed_deps.db` (SQLite). No asv JSON result files are written, and no commit hashes are stored.

```sql
-- Blocks of each file that each benchmark executed
CREATE TABLE benchmark_dep (
    benchmark_id     TEXT NOT NULL,
    filename         TEXT NOT NULL,   -- absolute path
    method_checksums BLOB NOT NULL,   -- array('i') of signed CRC32 values, as raw bytes
    fsha             TEXT NOT NULL,   -- git-style SHA1 of the file at survey time
    PRIMARY KEY (benchmark_id, filename)
);

-- Benchmarks the survey could not measure; always run
CREATE TABLE benchmark_meta (
    benchmark_id            TEXT PRIMARY KEY,
    always_affected         INTEGER NOT NULL DEFAULT 0,
    coverage_failure_reason TEXT
);

-- Baseline timing from initialize_diffcheck
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

To read the checksums:

```python
from array import array
checksums = array("i")
checksums.frombytes(row["method_checksums"])
```
