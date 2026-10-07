# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""Find where the benchmark processes import the project packages from."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ...runner import get_spawner

_PROBE = '''import importlib, json, os
from asv_runner._aux import update_sys_path

def track_import_probe():
    try:  # the suite's own __init__ can change sys.path, as in a real run
        update_sys_path({bench!r})
        importlib.import_module(os.path.basename({bench!r}))
    except Exception:
        pass
    out = {{}}
    for name in {names!r}:
        try:
            mod = importlib.import_module(name)
            out[name] = getattr(mod, "__file__", None) or next(iter(getattr(mod, "__path__", [])), None)
        except Exception:
            out[name] = None
    with open({out!r}, "w") as f:
        json.dump(out, f)
    return 0
'''


def import_packages(paths: Iterable[str], repo_root: str) -> List[str]:
    """Top-level import names of the files: numpy/core/x.c -> numpy, src/skimage/a.py -> skimage."""
    root = Path(repo_root).resolve()
    names = set()
    for p in paths:
        try:
            parts = Path(p).resolve().relative_to(root).parts
        except ValueError:
            continue
        for i in range(1, len(parts)):
            if (root.joinpath(*parts[:i]) / "__init__.py").is_file():
                names.add(parts[i - 1])
                break
    return sorted(names)


def probe_imports(env, benchmark_dir, launch_method, packages) -> Dict[str, Optional[str]]:
    """Import ``packages`` in a process started by the benchmark spawner; map each to its file, None if not importable."""
    tmp = tempfile.mkdtemp(prefix="lsv-import-probe-")
    try:
        bench = os.path.join(tmp, "lsv_import_probe")
        os.mkdir(bench)
        Path(bench, "__init__.py").write_text("")
        out = os.path.join(tmp, "out.json")
        Path(bench, "probe.py").write_text(
            _PROBE.format(bench=os.path.abspath(str(benchmark_dir)), names=list(packages), out=out)
        )
        spawner = get_spawner(env, bench, launch_method)
        try:
            log, code = spawner.run(
                name="probe.track_import_probe",
                params_str="{}",
                profile_path="None",
                result_file_name=os.path.join(tmp, "res.json"),
                timeout=300,
                cwd=tmp,
            )
        finally:
            spawner.close()
        if code != 0 or not os.path.exists(out):
            raise RuntimeError(f"import probe failed (exit {code}): {log[-2000:]}")
        return json.loads(Path(out).read_text())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def outside_root(found: Dict[str, Optional[str]], repo_root) -> Dict[str, str]:
    """The packages that resolve to a file outside ``repo_root``."""
    root = os.path.realpath(str(repo_root))
    return {
        name: path
        for name, path in found.items()
        if path and os.path.commonpath([root, os.path.realpath(path)]) != root
    }
