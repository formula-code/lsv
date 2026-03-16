"""
SQLite database for benchmark dependency tracking and baseline timing.

Tables:
  benchmark_dep  — which (file, method fingerprints) each benchmark touches
  benchmark_meta — always_affected flag and coverage failure reason
  baseline       — step-0 timing measurements for every benchmark
"""

import sqlite3
from array import array
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple


@dataclass(frozen=True)
class BenchmarkId:
    """Uniquely identifies one (benchmark, parameter-combination) pair.

    ``name`` is the asv_runner fully-qualified name, e.g.
    ``benchmarks.suite.MyClass.time_sort``.
    ``param_idx`` is the Cartesian-product index (-1 for non-parameterised).
    """
    name: str
    param_idx: int = -1

    def __str__(self) -> str:
        if self.param_idx == -1:
            return self.name
        return f"{self.name}-{self.param_idx}"

_ARRAY_TYPE = "i"   # signed 32-bit int, same as testmon


def _checksums_to_blob(checksums: List[int]) -> sqlite3.Binary:
    arr = array(_ARRAY_TYPE, checksums)
    return sqlite3.Binary(arr.tobytes())


def _blob_to_checksums(blob) -> List[int]:
    arr = array(_ARRAY_TYPE)
    arr.frombytes(blob)
    return arr.tolist()


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=OFF;

CREATE TABLE IF NOT EXISTS benchmark_dep (
    -- Which (file, method fingerprints) does each benchmark touch?
    benchmark_id    TEXT    NOT NULL,
    filename        TEXT    NOT NULL,
    method_checksums BLOB   NOT NULL,   -- array of CRC32 signed ints
    fsha            TEXT    NOT NULL,   -- git-style SHA1 at survey time
    PRIMARY KEY (benchmark_id, filename)
);

CREATE TABLE IF NOT EXISTS benchmark_meta (
    -- Metadata per benchmark: always_affected flag, survey coverage status.
    benchmark_id    TEXT    PRIMARY KEY,
    always_affected INTEGER NOT NULL DEFAULT 0,
    coverage_failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS baseline (
    -- Step-0 measurements for every benchmark.
    benchmark_id    TEXT    PRIMARY KEY,
    median          REAL    NOT NULL,
    ci_99_a         REAL    NOT NULL,
    ci_99_b         REAL    NOT NULL,
    q_25            REAL    NOT NULL,
    q_75            REAL    NOT NULL,
    repeat          INTEGER NOT NULL,
    number          INTEGER NOT NULL
);
"""

class LightspeedDB:
    def __init__(self, path: str):
        self._path = path
        self._con = sqlite3.connect(path)
        self._con.row_factory = sqlite3.Row
        self._con.executescript(_SCHEMA)
        self._con.commit()

    def close(self):
        self._con.close()
    def store_deps_batch(
        self,
        bid: BenchmarkId,
        file_deps: Dict[str, Tuple[List[int], str]],  # filename -> (checksums, fsha)
    ):
        rows = [
            (str(bid), fname, _checksums_to_blob(chk), fsha)
            for fname, (chk, fsha) in file_deps.items()
        ]
        self._con.executemany(
            """
            INSERT OR REPLACE INTO benchmark_dep
                (benchmark_id, filename, method_checksums, fsha)
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )
        self._con.commit()

    def set_always_affected(self, bid: BenchmarkId, reason: str):
        self._con.execute(
            """
            INSERT OR REPLACE INTO benchmark_meta
                (benchmark_id, always_affected, coverage_failure_reason)
            VALUES (?, 1, ?)
            """,
            (str(bid), reason),
        )
        self._con.commit()

    def get_all_benchmark_ids(self) -> List[BenchmarkId]:
        """Return every known BenchmarkId (from meta + dep tables)."""
        rows = self._con.execute(
            """
            SELECT DISTINCT benchmark_id FROM benchmark_meta
            UNION
            SELECT DISTINCT benchmark_id FROM benchmark_dep
            """
        ).fetchall()
        return [_parse_bid(r[0]) for r in rows]

    def get_affected_benchmark_ids(
        self,
        changed_files: Dict[str, Tuple[List[int], str]],
    ) -> List[BenchmarkId]:
        """
        Return benchmarks that must re-run given a set of changed files.

        A benchmark is affected if:
          (a) it is marked always_affected, OR
          (b) it has a dep on a changed file AND the stored fingerprint
              is NOT a subset of the current method_checksums for that file
              (i.e. some code block it previously touched has changed).
        """
        affected: Set[str] = set()

        rows = self._con.execute(
            "SELECT benchmark_id FROM benchmark_meta WHERE always_affected = 1"
        ).fetchall()
        affected.update(r[0] for r in rows)

        # fingerprint mismatch on changed files
        for filename, (current_checksums, current_fsha) in changed_files.items():
            deps = self._con.execute(
                """
                SELECT benchmark_id, method_checksums, fsha
                FROM   benchmark_dep
                WHERE  filename = ?
                """,
                (filename,),
            ).fetchall()
            current_set = set(current_checksums)
            for row in deps:
                stored_checksums = _blob_to_checksums(row["method_checksums"])
                stored_fsha = row["fsha"]
                if stored_fsha == current_fsha:
                    continue
                # If any stored fingerprint block is absent from the current checksums, the code the benchmark touched has changed.
                if set(stored_checksums) - current_set:
                    affected.add(row["benchmark_id"])

        return [_parse_bid(bid_str) for bid_str in affected]

    def get_stored_fshas(self) -> Dict[str, str]:
        """
        Return a mapping of filename -> fsha as stored in the dep table.
        When a file has multiple rows (one per benchmark), we take any one
        (they all agree at survey time).
        """
        rows = self._con.execute(
            "SELECT DISTINCT filename, fsha FROM benchmark_dep"
        ).fetchall()
        return {r["filename"]: r["fsha"] for r in rows}

    def store_baseline(self, bid: BenchmarkId, median: float, stats: dict):
        self._con.execute(
            """
            INSERT OR REPLACE INTO baseline
                (benchmark_id, median, ci_99_a, ci_99_b, q_25, q_75, repeat, number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(bid),
                median,
                stats["ci_99_a"],
                stats["ci_99_b"],
                stats["q_25"],
                stats["q_75"],
                stats["repeat"],
                stats["number"],
            ),
        )
        self._con.commit()

    def get_baseline(self, bid: BenchmarkId) -> Optional[dict]:
        row = self._con.execute(
            "SELECT * FROM baseline WHERE benchmark_id = ?", (str(bid),)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def has_baseline(self, bid: BenchmarkId) -> bool:
        return self.get_baseline(bid) is not None
def _parse_bid(bid_str: str) -> BenchmarkId:
    if "-" in bid_str:
        name, param_str = bid_str.rsplit("-", 1)
        try:
            return BenchmarkId(name=name, param_idx=int(param_str))
        except ValueError:
            pass
    return BenchmarkId(name=bid_str, param_idx=-1)
