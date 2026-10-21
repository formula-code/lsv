"""SQLite database of benchmark dependencies, always_affected marks and baseline timings."""

import logging
import sqlite3
from array import array
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple


@dataclass(frozen=True)
class BenchmarkId:
    """An asv_runner benchmark name and its parameter combination index (-1 when not parameterized)."""
    name: str
    param_idx: int = -1

    def __str__(self) -> str:
        if self.param_idx == -1:
            return self.name
        return f"{self.name}-{self.param_idx}"

log = logging.getLogger("asv.lightspeed")

COMPILED_SUFFIXES = (
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".c.src", ".h.src",
    ".pyx", ".pxd", ".pxi", ".rs", ".cu", ".cuh", ".f", ".f90",
)

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
        """Every BenchmarkId in the meta and dep tables."""
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
        """Benchmarks to re-run for the changed files; the selection rules are in lightspeed_docs.md."""
        affected: Set[str] = set()

        rows = self._con.execute(
            "SELECT benchmark_id FROM benchmark_meta WHERE always_affected = 1"
        ).fetchall()
        affected.update(r[0] for r in rows)

        for filename, (current_checksums, current_fsha) in changed_files.items():
            deps = self._con.execute(
                """
                SELECT benchmark_id, method_checksums, fsha
                FROM   benchmark_dep
                WHERE  filename = ?
                """,
                (filename,),
            ).fetchall()
            if not deps:
                if filename.endswith(COMPILED_SUFFIXES):
                    fallback = self._compiled_source_benchmarks() or {
                        str(b) for b in self.get_all_benchmark_ids()
                    }
                    log.warning(
                        f"lightspeed: compiled source {filename} has no dependency rows; "
                        f"selecting {len(fallback)} benchmarks"
                    )
                    affected.update(fallback)
                continue
            current_set = set(current_checksums)
            file_affected: Set[str] = set()
            all_dep_bids: Set[str] = set()
            for row in deps:
                stored_checksums = _blob_to_checksums(row["method_checksums"])
                stored_fsha = row["fsha"]
                all_dep_bids.add(row["benchmark_id"])
                if stored_fsha == current_fsha:
                    continue
                # If any stored fingerprint block is absent from the current checksums, the code the benchmark touched has changed.
                if set(stored_checksums) - current_set:
                    file_affected.add(row["benchmark_id"])

            if file_affected:
                affected.update(file_affected)
            else:
                # The file changed but no covered block did; select all its dependents so the change is not missed.
                affected.update(all_dep_bids)

        return [_parse_bid(bid_str) for bid_str in affected]

    def _compiled_source_benchmarks(self) -> Set[str]:
        rows = self._con.execute("SELECT DISTINCT benchmark_id, filename FROM benchmark_dep").fetchall()
        return {r[0] for r in rows if r[1].endswith(COMPILED_SUFFIXES)}

    def get_stored_fshas(self) -> Dict[str, str]:
        """filename -> fsha from the dep table; the rows of one file agree at survey time."""
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
