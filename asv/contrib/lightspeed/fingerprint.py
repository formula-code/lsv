"""
File fingerprinting adapted from testmon's process_code.py.

"""

from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .process_code import (
    Module,
    create_fingerprint,
    read_source_sha,
)


def fsha(path: str) -> Optional[str]:
    """git-style SHA1 of *path*"""
    _, sha = read_source_sha(path)
    return sha


def file_method_checksums(path: str) -> Tuple[List[int], Optional[str]]:
    """
    Return (all_method_checksums, fsha) for a path.

    ``all_method_checksums``: full list of CRC32 block checksums for
    the file. 
    This is what we store in the DB for each benchmark's deps, 
    and what we compare against at runtime to determine if a benchmark 
    is affected.

    ``fsha``: git-style SHA1.
    """
    source, sha = read_source_sha(path)
    if source is None:
        return [], None
    ext = "py" if path.endswith(".py") else "c"
    module = Module(source_code=source, ext=ext)
    return module.method_checksums, sha


def coverage_fingerprint(
    path: str, covered_lines: Set[int]
) -> Tuple[List[int], Optional[str]]:
    """
    Return (fingerprint, fsha) where fingerprint contains CRC32 checksums
    only for the code blocks that were actually executed (covered_lines).

    This is the core of testmon's dependency tracking: we store only the
    blocks a benchmark actually ran, not all blocks in the file.
    """
    source, sha = read_source_sha(path)
    if source is None:
        return [], None
    module = Module(source_code=source)
    checksums = create_fingerprint(module, covered_lines)
    return checksums, sha


def changed_files_with_fingerprints(
    paths: List[str],
    stored_fshas: Dict[str, str],
) -> Dict[str, Tuple[List[int], str]]:
    """
    Given a list of file paths and their previously-stored fshas, return a
    dict of  { filename: (current_method_checksums, current_fsha) }
    for every file whose fsha has changed (or that has no stored fsha).

    This is called at the start of each step to compute the input for
    LightspeedDB.get_affected_benchmark_ids().
    """
    result: Dict[str, Tuple[List[int], str]] = {}
    for p in paths:
        current_sha = fsha(p)
        if current_sha is None:
            continue
        if stored_fshas.get(p) == current_sha:
            continue
        checksums, sha = file_method_checksums(p)
        if sha is not None:
            result[p] = (checksums, sha)
    return result
