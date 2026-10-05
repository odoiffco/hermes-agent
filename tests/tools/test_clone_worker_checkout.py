"""Real Git exercises for the worker clone wrapper; no network or live checkout writes."""

import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "clone_worker_checkout.sh"


def git(*args, cwd=None):
    return subprocess.run(["git", *map(str, args)], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    git("init", "-q", "-b", "main", root)
    (root / "file.txt").write_text("source\n")
    git("add", "file.txt", cwd=root)
    git("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "base", cwd=root)
    git("branch", "other-base", cwd=root)
    return root


@pytest.mark.parametrize("branch,subdir", [("main", "worker"), ("other-base", "nested/another-worker")])
def test_borrows_objects_for_any_base_and_destination(source, tmp_path, branch, subdir):
    destination = tmp_path / subdir
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["bash", str(SCRIPT), str(source), branch, str(destination)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert git("branch", "--show-current", cwd=destination) == branch
    alternates = (destination / ".git/objects/info/alternates").read_text().strip()
    assert Path(alternates).resolve() == (source / ".git/objects").resolve()
    assert (destination / "file.txt").read_text() == "source\n"
    assert not list((destination / ".git/objects/pack").glob("*.pack"))


def test_refuses_existing_destination_and_missing_source(source, tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    for src in (source, tmp_path / "not-a-repo"):
        result = subprocess.run(["bash", str(SCRIPT), str(src), "main", str(destination)],
                                capture_output=True, text=True)
        assert result.returncode == 2


def test_refuses_promisor_source(source, tmp_path):
    git("config", "remote.origin.promisor", "true", cwd=source)
    destination = tmp_path / "worker"
    result = subprocess.run(["bash", str(SCRIPT), str(source), "main", str(destination)],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "promisor" in result.stderr
    assert not destination.exists()
