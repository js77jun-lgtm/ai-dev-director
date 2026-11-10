import os
import subprocess
from pathlib import Path
from typing import Tuple, Optional

class GitManagerError(Exception):
    pass

class GitManager:
    """Manages isolated Git worktrees and branches for runs."""

    @staticmethod
    def _run_git(cmd: list, cwd: Path) -> str:
        res = subprocess.run(
            # Non-ASCII file names verbatim (UTF-8), never as quoted octal escapes: a Korean file name once reached the
            # Scope Guard as "docs/\353\246..." and was reported as a path escaping the worktree.
            ["git", "-c", "core.quotepath=false"] + cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        if res.returncode != 0:
            raise GitManagerError(f"Git command failed: {' '.join(cmd)}\nError: {res.stderr.strip()}")
        return res.stdout.strip()

    @classmethod
    def ensure_git_repo(cls, project_path: str) -> None:
        proj = Path(project_path).resolve()
        if not proj.exists() or not (proj / ".git").exists():
            raise GitManagerError(f"Directory is not a valid Git repository: {project_path}")

    @classmethod
    def get_current_head(cls, project_path: str) -> str:
        cls.ensure_git_repo(project_path)
        proj = Path(project_path).resolve()
        return cls._run_git(["rev-parse", "HEAD"], proj)

    @classmethod
    def create_worktree(cls, project_path: str, run_id: str, base_ref: str = "HEAD") -> Tuple[str, str]:
        """
        Creates an isolated git worktree and branch for the run.
        Branch: RUN-xxxxx
        Worktree Directory: project_path/.director_worktrees/RUN-xxxxx
        """
        cls.ensure_git_repo(project_path)
        proj = Path(project_path).resolve()
        
        branch_name = run_id
        worktree_dir = proj / ".director_worktrees" / run_id
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        if worktree_dir.exists():
            raise GitManagerError(f"Worktree directory already exists: {worktree_dir}")

        # Ensure .director_worktrees is excluded in main repo without modifying .gitignore
        exclude_file = proj / ".git" / "info" / "exclude"
        if exclude_file.exists():
            content = exclude_file.read_text(encoding="utf-8")
            if ".director_worktrees" not in content:
                exclude_file.write_text(content.rstrip() + "\n.director_worktrees/\n", encoding="utf-8")

        # Create branch & worktree
        cls._run_git(["worktree", "add", "-b", branch_name, str(worktree_dir), base_ref], proj)

        return str(worktree_dir), branch_name

    @classmethod
    def sync_intent_to_add(cls, worktree_path: str) -> None:
        """Marks valid untracked files with intent-to-add (-N) so git diff detects new files."""
        wt = Path(worktree_path).resolve()
        untracked = cls.get_untracked_files(str(wt))
        for uf in untracked:
            try:
                cls._run_git(["add", "-N", uf], wt)
            except Exception:
                pass

    @classmethod
    def get_diff(cls, worktree_path: str) -> str:
        """Returns the current unstaged and staged git diff against HEAD in the worktree."""
        wt = Path(worktree_path).resolve()
        cls.sync_intent_to_add(str(wt))
        diff = cls._run_git(["diff", "HEAD"], wt)
        return diff

    @classmethod
    def get_status(cls, worktree_path: str) -> str:
        """Returns git status --porcelain for the worktree."""
        wt = Path(worktree_path).resolve()
        return cls._run_git(["status", "--porcelain"], wt)

    @classmethod
    def inspect_changes(cls, worktree_path: str):
        """Dashboard-only observation. Never stage intent-to-add during a live build.

        New files remain in `untracked`; only the Orchestrator's verification methods
        may add intent-to-add entries. Optional Git index refresh locks are disabled.
        """
        wt = Path(worktree_path).resolve()
        diff = cls._run_git(['--no-optional-locks', 'diff', 'HEAD'], wt)
        raw = cls._run_git(['--no-optional-locks', 'diff', 'HEAD', '--numstat'], wt)
        stats = []
        for line in raw.splitlines():
            parts = line.split('\t')
            if len(parts) == 3:
                stats.append((int(parts[0]) if parts[0] != '-' else 0,
                              int(parts[1]) if parts[1] != '-' else 0, parts[2]))
        return diff, stats, cls.get_untracked_files(str(wt))

    @classmethod
    def get_numstat(cls, worktree_path: str) -> list:
        """Returns lines added/deleted and filenames from git diff HEAD --numstat."""
        wt = Path(worktree_path).resolve()
        cls.sync_intent_to_add(str(wt))
        output = cls._run_git(["diff", "HEAD", "--numstat"], wt)
        stats = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) == 3:
                added = int(parts[0]) if parts[0] != "-" else 0
                deleted = int(parts[1]) if parts[1] != "-" else 0
                filename = parts[2]
                stats.append((added, deleted, filename))
        return stats

    @staticmethod
    def _unquote(path: str) -> str:
        """git still wraps a path in double quotes when it contains a space or a quote; the name inside is the real one."""
        if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
            return path[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        return path

    IGNORED_UNTRACKED_PATTERNS = {
        "__pycache__", ".pytest_cache", ".coverage", ".mypy_cache", ".ruff_cache", ".DS_Store"
    }

    @classmethod
    def get_untracked_files(cls, worktree_path: str) -> list:
        """Returns list of untracked files, excluding common test/runtime caches."""
        wt = Path(worktree_path).resolve()
        output = cls._run_git(["status", "--porcelain"], wt)
        untracked = []
        for line in output.splitlines():
            if line.startswith("??"):
                raw_path = cls._unquote(line[3:].strip())
                # Check if path or any parent matches ignored patterns
                parts = Path(raw_path).parts
                if any(p in cls.IGNORED_UNTRACKED_PATTERNS or p.endswith(".pyc") for p in parts):
                    continue
                untracked.append(raw_path)
        return untracked

    @classmethod
    def get_deleted_files(cls, worktree_path: str) -> list:
        """Returns list of files deleted according to git status."""
        wt = Path(worktree_path).resolve()
        output = cls._run_git(["status", "--porcelain"], wt)
        deleted = []
        for line in output.splitlines():
            # Matches ' D ' or 'D '
            if line.strip().startswith("D "):
                deleted.append(cls._unquote(line.strip()[2:].strip()))
        return deleted

    @classmethod
    def remove_worktree(cls, project_path: str, worktree_path: str, branch_name: Optional[str] = None, force: bool = False) -> None:
        """Removes the worktree. Note: In v0.1 this is only called on explicit cleanup."""
        proj = Path(project_path).resolve()
        try:
            cmd = ["worktree", "remove"]
            if force:
                cmd.append("--force")
            cmd.append(worktree_path)
            cls._run_git(cmd, proj)
        except Exception:
            pass
        if branch_name:
            try:
                cls._run_git(["branch", "-D", branch_name], proj)
            except Exception:
                pass
