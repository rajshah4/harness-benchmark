import subprocess

from run_suite import diff_metrics


def git(workspace, *args):
    subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


def test_diff_metrics_uses_root_commit_when_agent_commits(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Benchmark Test")
    git(tmp_path, "config", "user.email", "benchmark@example.invalid")
    source = tmp_path / "app.py"
    source.write_text("one\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "baseline")

    source.write_text("one\ntwo\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "agent implementation")

    result = diff_metrics(tmp_path)
    assert result["additions"] == 1
    assert result["deletions"] == 0
    assert result["changed_files"] == ["M\tapp.py"]
