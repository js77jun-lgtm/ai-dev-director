import tempfile
import subprocess
from pathlib import Path
from core.git_manager import GitManager

def _init_sample_git_repo(path: Path):
    subprocess.run(["git", "init"], cwd=str(path), check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(path), check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), check=True, stdout=subprocess.PIPE)
    
    # Create initial commit
    hello_file = path / "README.md"
    hello_file.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(path), check=True, stdout=subprocess.PIPE)

def test_git_manager_worktree_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "sample_repo"
        repo_path.mkdir()
        _init_sample_git_repo(repo_path)

        run_id = "RUN-TEST-009"
        wt_path, branch = GitManager.create_worktree(str(repo_path), run_id)

        assert Path(wt_path).exists()
        assert branch == run_id

        # Make change inside worktree
        new_file = Path(wt_path) / "feature.py"
        new_file.write_text("print('hello')", encoding="utf-8")

        # Main repo must remain untouched
        assert not (repo_path / "feature.py").exists()

        status = GitManager.get_status(wt_path)
        assert "feature.py" in status

        # Cleanup
        GitManager.remove_worktree(str(repo_path), wt_path, branch, force=True)
        assert not Path(wt_path).exists()
