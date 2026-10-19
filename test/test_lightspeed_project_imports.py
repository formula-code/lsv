"""The import probe reports where a benchmark process imports the project from."""

import sys

import pytest

from asv import config, environment
from asv.contrib.lightspeed import ProjectShadowed, check_project_imports
from asv.contrib.lightspeed.project_imports import import_packages


@pytest.fixture
def layout(tmp_path):
    for d in ("repo/pkg", "shadow/pkg", "repo/benchmarks"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "__init__.py").write_text("")
    conf = config.Config.from_json(
        {
            "repo": str(tmp_path / "repo"),
            "benchmark_dir": "benchmarks",
            "env_dir": "env",
            "results_dir": "results",
            "html_dir": "html",
        }
    )
    return tmp_path, environment.ExistingEnvironment(conf, sys.executable, {}, {})


def _check(tmp_path, env, launch):
    return check_project_imports(
        env, tmp_path / "repo/benchmarks", launch, ["pkg"], tmp_path / "repo"
    )


@pytest.mark.parametrize("launch", ["spawn", "forkserver"])
def test_package_in_repo_passes(layout, monkeypatch, launch):
    tmp_path, env = layout
    monkeypatch.setenv("ASV_PYTHONPATH", str(tmp_path / "repo"))
    assert _check(tmp_path, env, launch) == {"pkg": str(tmp_path / "repo/pkg/__init__.py")}


@pytest.mark.parametrize("launch", ["spawn", "forkserver"])
def test_shadow_copy_earlier_on_path_is_reported(layout, monkeypatch, launch):
    tmp_path, env = layout
    monkeypatch.setenv("ASV_PYTHONPATH", f"{tmp_path / 'shadow'}:{tmp_path / 'repo'}")
    with pytest.raises(ProjectShadowed) as exc:
        _check(tmp_path, env, launch)
    assert exc.value.paths == {"pkg": str(tmp_path / "shadow/pkg/__init__.py")}


def test_import_packages_from_changed_files(tmp_path):
    for d in ("numpy/core", "src/skimage/filters", "benchmarks"):
        (tmp_path / d).mkdir(parents=True)
    for f in ("numpy/__init__.py", "numpy/core/__init__.py", "src/skimage/__init__.py"):
        (tmp_path / f).write_text("")
    files = ["numpy/core/x.c", "src/skimage/filters/a.py", "benchmarks/b.py", "README.rst"]
    assert import_packages([str(tmp_path / f) for f in files], str(tmp_path)) == [
        "numpy",
        "skimage",
    ]
