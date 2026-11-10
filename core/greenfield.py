"""Fail-closed GREENFIELD project classification for TRIAD research.

GREENFIELD is an evidence-backed project mode, not an interpretation of a
test runner exit code.  In particular, pytest exit code 5 (no tests found) is
never converted to PASS.  The normal Verifier and normal baseline pipeline do
not import this module.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

from core.git_manager import GitManager


PROJECT_TYPE_EXISTING = "EXISTING"
PROJECT_TYPE_GREENFIELD = "GREENFIELD"
BASELINE_REQUIRED = "REQUIRED"
BASELINE_NOT_APPLICABLE_GREENFIELD = "NOT_APPLICABLE_GREENFIELD"


_IGNORED_PARTS = {
    ".git", ".director_worktrees", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", ".venv", "venv",
}
_BENIGN_ROOT_FILES = {
    ".gitignore", ".gitattributes", ".editorconfig", "license", "license.md",
    "license.txt", "readme", "readme.md", "readme.txt",
}
_SOURCE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts",
    ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".cxx", ".cs", ".fs",
    ".rb", ".php", ".swift", ".scala", ".dart", ".lua", ".r", ".jl",
    ".sql", ".vue", ".svelte", ".html", ".css", ".scss", ".sh", ".ps1",
}
_PRODUCT_MANIFESTS = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "pipfile", "poetry.lock", "cargo.toml", "go.mod", "go.sum", "pom.xml",
    "build.gradle", "build.gradle.kts", "composer.json", "gemfile", "makefile",
    "cmakelists.txt", "dockerfile", "docker-compose.yml", "docker-compose.yaml",
}


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath([str(root), str(candidate)]) == str(root)
    except (OSError, ValueError):
        return False


def _is_test_path(relative: Path) -> bool:
    lowered = [part.lower() for part in relative.parts]
    name = relative.name.lower()
    stem = relative.stem.lower()
    return (
        any(part in {"test", "tests", "spec", "specs", "__tests__"} for part in lowered[:-1])
        or name.startswith("test_")
        or stem.endswith("_test")
        or name.endswith(".spec.js")
        or name.endswith(".spec.ts")
        or name.endswith(".test.js")
        or name.endswith(".test.ts")
    )


def _inventory(project: Path) -> Dict[str, List[str]]:
    meaningful: List[str] = []
    tests: List[str] = []
    unsafe_entries: List[str] = []
    other: List[str] = []

    for path in project.rglob("*"):
        try:
            relative = path.relative_to(project)
        except ValueError:
            unsafe_entries.append(str(path))
            continue
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        rel = relative.as_posix()
        try:
            if path.is_symlink():
                resolved = path.resolve(strict=True)
                if not _is_within(project, resolved):
                    unsafe_entries.append(rel)
                    continue
            if not path.is_file():
                continue
        except (OSError, RuntimeError):
            unsafe_entries.append(rel)
            continue

        lower_name = path.name.lower()
        if _is_test_path(relative):
            tests.append(rel)
        elif path.suffix.lower() in _SOURCE_SUFFIXES or lower_name in _PRODUCT_MANIFESTS:
            meaningful.append(rel)
        elif len(relative.parts) == 1 and lower_name in _BENIGN_ROOT_FILES:
            continue
        else:
            other.append(rel)

    return {
        "meaningful_source_files": sorted(set(meaningful)),
        "test_files": sorted(set(tests)),
        "unsafe_entries": sorted(set(unsafe_entries)),
        "other_files": sorted(set(other)),
    }


def assess_project_type(
    project_path: str,
    *,
    user_explicit_new_project: bool,
    existing_product_modification_request: bool = False,
) -> Dict[str, Any]:
    """Return deterministic evidence for EXISTING vs GREENFIELD.

    Every required GREENFIELD predicate must be positively observed.  Missing,
    unreadable, or unsafe evidence classifies as EXISTING and therefore keeps
    the normal baseline requirement.
    """
    requested_path = str(project_path)
    project = Path(project_path).resolve()
    exists = project.exists() and project.is_dir()
    valid_git_repo = False
    head_exists = False
    head_commit = None
    inventory = {
        "meaningful_source_files": [], "test_files": [],
        "unsafe_entries": [], "other_files": [],
    }
    errors: List[str] = []

    if exists:
        try:
            # `git worktree` checkouts contain a .git *file*, not a directory.  Ask Git itself so both a
            # primary checkout and an isolated worktree are valid local repositories.
            inside = GitManager._run_git(["rev-parse", "--is-inside-work-tree"], project)
            valid_git_repo = inside.strip().lower() == "true"
            head_commit = GitManager._run_git(["rev-parse", "HEAD"], project) if valid_git_repo else None
            head_exists = bool(head_commit)
        except Exception as exc:
            errors.append(f"GIT_EVIDENCE_UNAVAILABLE: {type(exc).__name__}: {exc}")
        try:
            inventory = _inventory(project)
        except Exception as exc:
            errors.append(f"PROJECT_INVENTORY_UNAVAILABLE: {type(exc).__name__}: {exc}")
            inventory["unsafe_entries"].append("<inventory-unavailable>")
    else:
        errors.append("PROJECT_DIRECTORY_MISSING")

    predicates = {
        "project_directory_exists": exists,
        "valid_local_git_repo": valid_git_repo,
        "head_exists": head_exists,
        "user_explicit_new_project": bool(user_explicit_new_project),
        "meaningful_source_absent": not inventory["meaningful_source_files"],
        "existing_tests_absent": not inventory["test_files"],
        "unclassified_project_files_absent": not inventory["other_files"],
        "existing_product_modification_request_absent": not bool(existing_product_modification_request),
        "unsafe_entries_absent": not inventory["unsafe_entries"],
    }
    qualified = all(predicates.values())
    project_type = PROJECT_TYPE_GREENFIELD if qualified else PROJECT_TYPE_EXISTING
    baseline_state = BASELINE_NOT_APPLICABLE_GREENFIELD if qualified else BASELINE_REQUIRED
    failed_predicates = sorted(name for name, value in predicates.items() if not value)

    evidence: Dict[str, Any] = {
        "schema_version": 1,
        "requested_path": requested_path,
        "canonical_project_path": str(project),
        "project_type": project_type,
        "baseline_state": baseline_state,
        "greenfield_qualified": qualified,
        "predicates": predicates,
        "failed_predicates": failed_predicates,
        "head_commit": head_commit,
        **inventory,
        "errors": errors,
        "observed_at": datetime.utcnow().isoformat(),
    }
    digest_payload = {key: value for key, value in evidence.items() if key not in {"observed_at", "evidence_sha256"}}
    encoded = json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    evidence["evidence_sha256"] = hashlib.sha256(encoded).hexdigest()
    return evidence


def build_greenfield_baseline_evidence(project_evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Build the explicit non-PASS baseline artifact for a qualified GREENFIELD project."""
    if (
        project_evidence.get("project_type") != PROJECT_TYPE_GREENFIELD
        or project_evidence.get("baseline_state") != BASELINE_NOT_APPLICABLE_GREENFIELD
        or not project_evidence.get("greenfield_qualified")
        or not project_evidence.get("evidence_sha256")
    ):
        raise ValueError("GREENFIELD_QUALIFICATION_FAILED")
    return {
        "schema_version": 1,
        "baseline_state": BASELINE_NOT_APPLICABLE_GREENFIELD,
        "applicable": False,
        "passed": None,
        "summary": "Baseline verification is not applicable to a qualified new project; this is not PASS.",
        "test_counts": None,
        "project_evidence_sha256": project_evidence["evidence_sha256"],
        "recorded_at": datetime.utcnow().isoformat(),
    }
