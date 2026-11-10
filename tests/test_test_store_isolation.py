"""Pytest must not implicitly use the application's persistent review store."""
import os
import subprocess
import sys
from pathlib import Path


def _bootstrap_store_path(env):
    script = "import os, tests.conftest; print(os.environ['STORE_DB_PATH'])"
    result = subprocess.run([sys.executable, "-c", script], env=env, check=True,
                            capture_output=True, text=True)
    return Path(result.stdout.strip())


def test_missing_store_path_gets_unique_temporary_database():
    env = os.environ.copy()
    env.pop("STORE_DB_PATH", None)
    first = _bootstrap_store_path(env)
    second = _bootstrap_store_path(env)
    assert first != second
    assert first.name == second.name == "reviews.sqlite"
    assert first.parent.name.startswith("agent-t-pytest-")
    assert second.parent.name.startswith("agent-t-pytest-")


def test_explicit_store_path_is_preserved(tmp_path):
    env = os.environ.copy()
    expected = tmp_path / "explicit.sqlite"
    env["STORE_DB_PATH"] = str(expected)
    assert _bootstrap_store_path(env) == expected


def test_inherited_generated_store_is_not_shared_between_workers(tmp_path):
    env = os.environ.copy()
    inherited = str(tmp_path / "parent.sqlite")
    env["STORE_DB_PATH"] = inherited
    env["_AGENT_T_PYTEST_GENERATED_STORE"] = inherited
    first = _bootstrap_store_path(env)
    second = _bootstrap_store_path(env)
    assert first != second and first != Path(inherited) and second != Path(inherited)
