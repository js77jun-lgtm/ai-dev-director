import os
import re
from pathlib import Path, PurePosixPath
from typing import List, Tuple, Optional, Dict
from schemas.order import OrderSchema

class ScopeViolationError(Exception):
    pass

class SafetyPolicy:
    """Enforces Deny-All policy and Scope Guard validation."""

    # Sensitive dependency / package management files
    # ---- Director-owned control plane: the Builder may never write/delete/rename these, at any layer
    # (Antigravity hook, Antigravity permission settings, Director pre-write guard, post-build Scope Guard).
    CONTROL_PLANE_DIRS = {".git", ".agents", ".director_worktrees"}          # any path segment
    CONTROL_PLANE_FILES = {"gemini.md"}                                     # repo-root files (case-insensitive)
    CONTROL_PLANE_PATTERNS = (".git/**", ".agents/**", ".director_worktrees/**", "GEMINI.md")
    # ---- Runtime artifacts the Director/tooling generates; excluded from post-build scope accounting only
    RUNTIME_ARTIFACT_DIRS = {".pytest_cache", "__pycache__"}
    # ---- ENTRY approval allowlist (DEFAULT DENY): only these APPROVAL_REQUIRED codes may be approved by a human.
    # PROTECTED_PATH_*, GIT_INTERNALS_IN_SCOPE, CONTROL_PLANE_IN_SCOPE, INVALID_PATH, DESTRUCTIVE_CHANGE and any
    # unknown/new code are non-approvable.
    APPROVABLE_ENTRY = {"SENSITIVE_FILE_IN_SCOPE", "AMBIGUOUS_SCOPE", "HIGH_RISK", "EXPLICIT_HUMAN_APPROVAL_REQUESTED",
                        "USER_DECISION_REQUIRED"}   # HEAD proxy escalated a money/credential/irreversible decision

    @staticmethod
    def _norm_control(path_str) -> str:
        """posix, lower-case, leading './' and '/' removed WITHOUT eating the leading dot of '.agents'."""
        norm = str(path_str or "").replace("\\", "/").strip()
        while norm.startswith("./"):
            norm = norm[2:]
        return norm.lstrip("/").lower()

    @classmethod
    def is_control_plane_path(cls, path_str: str) -> bool:
        """True when a repo-relative path touches the Director control plane (.git/.agents/.director_worktrees
        anywhere in the path, or the root GEMINI.md)."""
        norm = cls._norm_control(path_str)
        if not norm:
            return False
        parts = [p for p in norm.split("/") if p not in ("", ".")]
        if any(p in cls.CONTROL_PLANE_DIRS for p in parts):
            return True
        return len(parts) == 1 and parts[0] in cls.CONTROL_PLANE_FILES

    @classmethod
    def is_runtime_artifact(cls, path_str: str) -> bool:
        norm = cls._norm_control(path_str)
        parts = [p for p in norm.split("/") if p not in ("", ".")]
        return any(p in cls.RUNTIME_ARTIFACT_DIRS for p in parts)

    DEPENDENCY_FILES = {
        "requirements.txt", "setup.py", "pyproject.toml", "pipfile", "pipfile.lock",
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "pom.xml", "build.gradle", "build.gradle.kts", "go.mod", "go.sum",
        "gemfile", "gemfile.lock", "cargo.toml", "cargo.lock"
    }

    # Permission and sensitive system files
    PERMISSION_FILES = {
        "androidmanifest.xml", "info.plist", "entitlements.plist", "privileges.json"
    }

    # Build and architecture configs
    CRITICAL_CONFIG_FILES = {
        "makefile", "cmakelists.txt", "dockerfile", "docker-compose.yml", "docker-compose.yaml",
        "tsconfig.json", "webpack.config.js", "vite.config.ts", "vite.config.js"
    }

    # Forbidden command patterns (DENY ALL except approved build/test)
    FORBIDDEN_COMMAND_PATTERNS = [
        r"\bgit\s+(push|reset|clean|branch\s+-d|branch\s+-D|checkout\s+-b)\b",
        r"\b(rm\s+-rf\s+/|del\s+/f|format\b|dd\s+if=)\b",
        r"\b(pip\s+install|npm\s+install|yarn\s+add|cargo\s+install|apt\s+install|brew\s+install)\b",
        r"\b(curl|wget|nc|ncat|netcat|ssh|ftp|scp)\b",
        r"\b(chmod|chown|attrib|icacls)\b",
        r"\b(skip-permissions|bypass-permissions)\b"
    ]

    # ------------------------------------------------------------------ v0.2.3.3 canonical path safety
    @staticmethod
    def is_within_root(root, target) -> bool:
        """Canonical containment: commonpath(root.resolve(), target.resolve()) == root. Follows symlinks/junctions."""
        try:
            r = Path(root).resolve()
            t = Path(target).resolve()
            return os.path.commonpath([str(r), str(t)]) == str(r)
        except Exception:
            return False

    GLOB_CHARS = "*?["

    @classmethod
    def has_glob(cls, path_str: str) -> bool:
        return any(ch in path_str for ch in cls.GLOB_CHARS)

    @classmethod
    def static_prefix(cls, pattern: str) -> str:
        """Static (wildcard-free) prefix of a repo-relative pattern, posix-normalized.
        'src/__pycache__/**' -> 'src/__pycache__', 'tests/**/*.py' -> 'tests', '**/*.py' -> '' (worktree root).
        Only the segments BEFORE the first wildcard segment are kept; a pattern is never resolved as a path."""
        parts = [seg for seg in str(pattern).replace("\\", "/").split("/") if seg not in ("", ".")]
        static = []
        for seg in parts:
            if cls.has_glob(seg):
                break
            static.append(seg)
        return "/".join(static)

    @classmethod
    def canonical_within_worktree(cls, root, rel_path: str) -> bool:
        """Canonical containment for an ORDER path entry. Lexical safety (no '..', absolute, UNC, '~', control
        chars) is checked by the caller first. A glob pattern is never passed to Path.resolve() (Windows raises
        OSError for '**'); only its static prefix is resolved. Actual matched files are resolved individually
        by validate_scope_post_build, so symlink/junction escapes below the prefix are still caught there."""
        target_rel = cls.static_prefix(rel_path) if cls.has_glob(rel_path) else rel_path
        root_p = Path(root)
        target = root_p / target_rel if target_rel else root_p
        return cls.is_within_root(root_p, target)

    @classmethod
    def is_repo_relative_path(cls, path_str) -> Tuple[bool, Optional[str]]:
        """Only plain repo-relative paths are acceptable in an ORDER. Rejects '..' segments, absolute paths
        (POSIX or Windows drive), UNC paths, home expansion and control characters."""
        if path_str is None or not isinstance(path_str, str):
            return False, "path is not a string"
        s = path_str.strip()
        if not s:
            return False, "empty path"
        if "\x00" in s or "\n" in s or "\r" in s:
            return False, "control characters in path"
        if s.startswith("~"):
            return False, f"home expansion not allowed: {path_str}"
        norm = s.replace("\\", "/")
        if norm.startswith("//") or norm.startswith("\\\\"):
            return False, f"UNC path not allowed: {path_str}"
        if norm.startswith("/"):
            return False, f"absolute path not allowed: {path_str}"
        if re.match(r"^[A-Za-z]:", norm):
            return False, f"drive-absolute path not allowed: {path_str}"
        if os.path.isabs(s):
            return False, f"absolute path not allowed: {path_str}"
        segments = [seg for seg in norm.split("/") if seg not in ("", ".")]
        if any(seg == ".." for seg in segments):
            return False, f"parent traversal not allowed: {path_str}"
        return True, None

    @classmethod
    def validate_order_paths(cls, order: OrderSchema, worktree_path=None) -> List[str]:
        """Fail-closed validation of every path in an ORDER (allowed_paths, protected_paths, allowed_deletions).
        When a worktree is given, each path must also resolve canonically inside it (symlink/junction escapes)."""
        violations: List[str] = []
        groups = (
            ("allowed_paths", list(order.allowed_paths or [])),
            ("protected_paths", list(order.protected_paths or [])),
            ("allowed_deletions", list(order.allowed_deletions or [])),
        )
        root = Path(worktree_path).resolve() if worktree_path else None
        for group, items in groups:
            for p in items:
                ok, reason = cls.is_repo_relative_path(p)
                if not ok:
                    violations.append(f"{group}: {reason}")
                    continue
                if root is not None:
                    # allowed_deletions may name symbols (def:foo) rather than files; only check path-like entries
                    if group == "allowed_deletions" and ":" in p and "/" not in p and "\\" not in p:
                        continue
                    if not cls.canonical_within_worktree(root, p):
                        violations.append(f"{group}: resolves outside worktree: {p}")
        return violations

    @classmethod
    def normalize_order_paths(cls, order: OrderSchema, project_path: str) -> OrderSchema:
        """Best-effort, safety-preserving normalization of a freshly HEAD-generated chat ORDER's paths,
        run once BEFORE SafetyPolicy.assess_order_for_auto_run()/validate_order_paths() ever sees it.

        This never loosens is_repo_relative_path/validate_order_paths themselves - those remain the
        real, unmodified, fail-closed check every ORDER (chat-originated or not) still goes through
        after this. It only handles two purely representational cases a chat-generated ORDER commonly
        gets wrong:
          (a) an absolute path that resolves INSIDE project_path is rewritten to its repo-relative form
              (e.g. "E:/.../the trading sample project/src" -> "src") - same authority the ORDER already implied, just
              spelled correctly.
          (b) a protected_paths entry that resolves OUTSIDE project_path (absolute-outside, or a '..'
              traversal) is dropped - a worktree-isolated Builder cannot reach anything outside the
              project root anyway, so "protecting" it is a no-op, never a grant of anything.
        allowed_paths and allowed_deletions are never dropped or widened here: an entry that cannot be
        resolved to somewhere inside project_path is passed through untouched, so a genuine attempt to
        write outside the project still fails validate_order_paths exactly as before.
        """
        try:
            root = Path(project_path).resolve()
        except Exception:
            return order

        def to_relative_or_none(raw: str) -> Optional[str]:
            """Returns the repo-relative form if raw resolves inside root; None if it resolves outside
            (or can't be resolved at all) so the caller can decide whether to drop or pass through."""
            s = str(raw or "").strip()
            if not s:
                return s
            norm = s.replace("\\", "/")
            looks_absolute = bool(re.match(r"^[A-Za-z]:", norm)) or norm.startswith("/") or norm.startswith("\\\\")
            has_parent_traversal = any(
                seg == ".." for seg in norm.split("/") if seg not in ("", ".")
            )
            if not looks_absolute and not has_parent_traversal:
                return s  # already relative and lexically clean; let the real check accept/reject it
            try:
                resolved = Path(s).resolve() if looks_absolute else (root / s).resolve()
            except Exception:
                return None
            if not cls.is_within_root(root, resolved):
                return None
            try:
                rel = resolved.relative_to(root)
            except Exception:
                return None
            rel_posix = rel.as_posix()
            return rel_posix if rel_posix != "." else s

        def normalize_list(items, *, drop_if_outside: bool) -> list:
            result = []
            for item in items or []:
                fixed = to_relative_or_none(item)
                if fixed is None:
                    if not drop_if_outside:
                        result.append(item)  # unresolved/outside: pass through, real check still rejects it
                    continue
                result.append(fixed)
            return result

        data = order.model_dump()
        data["allowed_paths"] = normalize_list(order.allowed_paths, drop_if_outside=False)
        data["protected_paths"] = normalize_list(order.protected_paths, drop_if_outside=True)
        data["allowed_deletions"] = normalize_list(order.allowed_deletions, drop_if_outside=False)
        try:
            return OrderSchema(**data)
        except Exception:
            return order  # normalization must never be why an otherwise-valid order breaks

    @classmethod
    def normalize_rel_path(cls, path_str: str) -> str:
        """Normalizes path into relative posix format."""
        clean = str(path_str or "").replace("\\", "/").strip()
        while clean.startswith("./"):
            clean = clean[2:]
        return clean.strip("/").lower()

    @classmethod
    def _matches_path_rule(cls, target_norm: str, rule_norm: str) -> bool:
        """Match one normalized repo-relative path rule without broad string-prefix shortcuts.

        ``dir/**`` is the ORDER convention for a whole subtree (including direct
        children). Other wildcard forms use pathlib's segment-aware matching so
        ``src/*.py`` cannot accidentally authorize ``src/deep/x.py``.
        """
        if cls.has_glob(rule_norm):
            if rule_norm.endswith("/**"):
                subtree = rule_norm[:-3].rstrip("/")
                if not subtree:
                    return True
                return target_norm == subtree or target_norm.startswith(subtree + "/")
            try:
                return PurePosixPath(target_norm).match(rule_norm)
            except (TypeError, ValueError):
                return False
        return target_norm == rule_norm or target_norm.startswith(rule_norm.rstrip("/") + "/")

    @classmethod
    def is_path_allowed(cls, target_path: str, allowed_paths: List[str]) -> bool:
        """Checks if target_path matches any allowed path prefix or glob."""
        if not allowed_paths:
            # If no allowed_paths specified, DENY ALL writes
            return False
        
        target_norm = cls.normalize_rel_path(target_path)
        for allowed in allowed_paths:
            allowed_norm = cls.normalize_rel_path(allowed)
            if cls._matches_path_rule(target_norm, allowed_norm):
                return True
        return False

    @classmethod
    def is_path_protected(cls, target_path: str, protected_paths: List[str]) -> bool:
        """Checks if target_path matches any protected path."""
        target_norm = cls.normalize_rel_path(target_path)
        for protected in protected_paths:
            protected_norm = cls.normalize_rel_path(protected)
            if cls._matches_path_rule(target_norm, protected_norm):
                return True
        return False

    @classmethod
    def check_pre_tool_file_write(
        cls,
        file_path: str,
        workspace_root: str,
        allowed_paths: List[str],
        protected_paths: List[str]
    ) -> Tuple[bool, Optional[str]]:
        """
        PreToolUse hook for file modification.
        Deny all writes unless explicitly in allowed_paths and outside protected_paths.
        """
        try:
            ws_root = Path(workspace_root).resolve()
            target = Path(file_path)
            if not target.is_absolute():
                target = (ws_root / target).resolve()
            else:
                target = target.resolve()

            # Must stay inside workspace (canonical containment; follows symlinks/junctions)
            if not cls.is_within_root(ws_root, target):
                return False, f"Forbidden: Attempted to write outside workspace: {file_path}"
            try:
                rel_path = target.relative_to(ws_root).as_posix()
            except ValueError:
                return False, f"Forbidden: Attempted to write outside workspace: {file_path}"
            ok_rel, why = cls.is_repo_relative_path(file_path) if not Path(file_path).is_absolute() else (True, None)
            if not ok_rel:
                return False, f"Forbidden: {why}"

            # Director control plane is never writable by the Builder (hard deny, regardless of the ORDER)
            if cls.is_control_plane_path(rel_path):
                return False, f"Forbidden: Director control-plane path is never writable: {rel_path}"

            # Check protected paths
            if cls.is_path_protected(rel_path, protected_paths):
                return False, f"Forbidden: Path is in protected_paths: {rel_path}"

            # Check allowed paths (DENY ALL default)
            if not cls.is_path_allowed(rel_path, allowed_paths):
                return False, f"Forbidden: Path is not in allowed_paths: {rel_path}"

            # Check sensitive files
            fname = target.name.lower()
            if fname in cls.DEPENDENCY_FILES and not cls.is_path_allowed(rel_path, allowed_paths):
                return False, f"Forbidden: Modifying dependency file {fname}"
            if fname in cls.PERMISSION_FILES:
                return False, f"Forbidden: Modifying permission file {fname}"

            return True, None
        except Exception as e:
            return False, f"Error validating path: {str(e)}"

    @classmethod
    def check_pre_tool_command(cls, command: str, approved_commands: Optional[List[str]] = None) -> Tuple[bool, Optional[str]]:
        """
        PreToolUse hook for shell command execution.
        Denies forbidden commands and destructive actions.
        """
        if not command:
            return True, None

        cmd_clean = command.strip()

        # Check explicit forbidden patterns
        for pattern in cls.FORBIDDEN_COMMAND_PATTERNS:
            if re.search(pattern, cmd_clean, re.IGNORECASE):
                return False, f"Forbidden command pattern detected: {pattern}"

        # If an approved commands whitelist is provided, check membership
        if approved_commands is not None:
            # Check if command starts with any approved prefix (e.g. pytest, python, npm test)
            matched = any(cmd_clean.startswith(appr) for appr in approved_commands)
            if not matched:
                return False, f"Command not in approved command list: {command}"

        return True, None

    # Post-build violations a human may explicitly waive, per file. Everything else is deterministic and final:
    # traversal / canonical / symlink escapes, files outside allowed_paths, protected paths, permission files,
    # critical config, deletions, regression, missing evidence, reviewer/final gate failures.
    APPROVABLE_POST_BUILD = {
        "DEPENDENCY_CONFIGURATION_MODIFIED": "Dependency configuration modified",
        "LIVE_MONEY_CODE": "Live money code",   # order_send / payment SDK added: only the user may allow executing it
    }

    @classmethod
    def classify_post_build_violations(cls, violations: List[str]) -> Dict[str, list]:
        """Splits Scope Guard violations into human-approvable items (gate + file) and blocking ones."""
        approvable, blocking = [], []
        for v in violations or []:
            matched = False
            for gate, prefix in cls.APPROVABLE_POST_BUILD.items():
                if v.startswith(prefix + ": "):
                    approvable.append({"gate": gate, "file": v[len(prefix) + 2:].strip(), "text": v})
                    matched = True
                    break
            if not matched:
                blocking.append(v)
        return {"approvable": approvable, "blocking": blocking}

    @classmethod
    def apply_waivers(cls, violations: List[str], waivers: Optional[List[dict]]) -> Tuple[List[str], List[str]]:
        """Removes ONLY violations that exactly match an explicit human waiver (approvable gate + same file).
        Returns (remaining_violations, waived_violations)."""
        if not waivers:
            return list(violations), []
        allowed = set()
        for w in waivers:
            gate = str(w.get("gate", "")); prefix = cls.APPROVABLE_POST_BUILD.get(gate)
            if prefix and w.get("file"):
                allowed.add(f"{prefix}: {cls.normalize_rel_path(str(w['file']))}")
        remaining, waived = [], []
        for v in violations:
            key = v
            for prefix in cls.APPROVABLE_POST_BUILD.values():
                if v.startswith(prefix + ": "):
                    key = f"{prefix}: {cls.normalize_rel_path(v[len(prefix) + 2:])}"
            (waived if key in allowed else remaining).append(v)
        return remaining, waived

    @classmethod
    def validate_scope_post_build(
        cls,
        order: OrderSchema,
        numstat: List[Tuple[int, int, str]],
        untracked_files: List[str],
        raw_git_diff: Optional[str] = None,
        deleted_files: Optional[List[str]] = None,
        worktree_path: Optional[str] = None,
        approved_waivers: Optional[List[dict]] = None
    ) -> Tuple[bool, List[str]]:
        """
        Post-build Scope Guard: Analyzes actual git diff changes.
        Protects existing behavior:
        - Prevents unauthorized deletion of existing functions, classes, tests, or files
        - Enforces allowed_paths, protected_paths, max_files_changed, max_diff_lines
        Returns (is_valid, list_of_violations).
        If any violation is detected, Orchestrator triggers HUMAN_REQUIRED.
        """
        violations = []
        
        all_changed_files = set()
        total_diff_lines = 0

        # Runtime artifacts (caches) are excluded from accounting. Director control-plane files are NOT excluded:
        # any tracked/untracked change there is a hard, non-approvable violation.
        control_plane_hits = []
        for added, deleted, fname in numstat:
            if cls.is_runtime_artifact(fname):
                continue
            if cls.is_control_plane_path(fname):
                control_plane_hits.append(fname)
                continue
            all_changed_files.add(fname)
            total_diff_lines += (added + deleted)

        for ufname in untracked_files:
            if cls.is_runtime_artifact(ufname):
                continue
            if cls.is_control_plane_path(ufname):
                control_plane_hits.append(ufname)
                continue
            all_changed_files.add(ufname)
        for f in sorted(set(control_plane_hits)):
            violations.append(f"Control plane modified: {f}")

        # 1. Check max files changed
        if len(all_changed_files) > order.max_files_changed:
            violations.append(
                f"Max files changed exceeded: {len(all_changed_files)} > {order.max_files_changed}"
            )

        # 2. Check max diff lines
        if total_diff_lines > order.max_diff_lines:
            violations.append(
                f"Max diff lines exceeded: {total_diff_lines} > {order.max_diff_lines}"
            )

        # 2.5 Canonical containment: a changed path that resolves outside the worktree (symlink/junction escape)
        if worktree_path:
            root = Path(worktree_path).resolve()
            for f in sorted(all_changed_files):
                if not cls.is_within_root(root, root / f):
                    violations.append(f"Path escapes worktree (symlink/junction or traversal): {f}")

        # 3. Check allowed / protected / sensitive paths for every changed file
        for f in all_changed_files:
            # Check allowed paths
            if not cls.is_path_allowed(f, order.allowed_paths):
                violations.append(f"File modified outside allowed_paths: {f}")

            # Check protected paths
            if cls.is_path_protected(f, order.protected_paths):
                violations.append(f"Protected path modified: {f}")

            # Check dependency files changed unexpectedly
            fname = Path(f).name.lower()
            if fname in cls.DEPENDENCY_FILES:
                violations.append(f"Dependency configuration modified: {f}")

            # Check permission files
            if fname in cls.PERMISSION_FILES:
                violations.append(f"Permission configuration modified: {f}")

            # Check critical config files if not explicitly allowed
            if fname in cls.CRITICAL_CONFIG_FILES and not cls.is_path_allowed(f, order.allowed_paths):
                violations.append(f"Critical build/architecture configuration modified: {f}")

        # 4. Existing Behavior Protection: Check file deletions
        allowed_deletions = set(getattr(order, "allowed_deletions", []))
        if deleted_files:
            for df in deleted_files:
                if cls.is_control_plane_path(df):
                    violations.append(f"Control plane modified: {df}")
                elif df not in allowed_deletions:
                    violations.append(f"Unauthorized file deletion: {df}")

        # 5. Existing Behavior Protection: Check function/class/test deletions in diff.
        # A symbol whose definition line was only edited or moved (same kind+name re-added anywhere in the diff,
        # e.g. a new parameter) still exists and is not a deletion.
        if raw_git_diff:
            removed_pattern = re.compile(r"^\-\s*(?:async\s+)?(def|class)\s+([a-zA-Z0-9_]+)")
            added_pattern = re.compile(r"^\+\s*(?:async\s+)?(def|class)\s+([a-zA-Z0-9_]+)")
            removed, readded = [], set()
            for line in raw_git_diff.splitlines():
                if line.startswith("---") or line.startswith("+++"):
                    continue
                match = removed_pattern.match(line)
                if match:
                    removed.append((match.group(1), match.group(2)))
                    continue
                match = added_pattern.match(line)
                if match:
                    readded.add((match.group(1), match.group(2)))
            for kind, sym_name in dict.fromkeys(removed):
                if (kind, sym_name) in readded:
                    continue
                # Check if permitted in allowed_deletions
                if sym_name not in allowed_deletions and f"{kind}:{sym_name}" not in allowed_deletions:
                    violations.append(
                        f"Unauthorized deletion of existing {kind}: '{sym_name}' without explicit allowed_deletions permission"
                    )

        violations, _waived = cls.apply_waivers(violations, approved_waivers)
        is_valid = len(violations) == 0
        return is_valid, violations

    # ------------------------------------------------------------------ v0.2.3.1
    @classmethod
    def assess_order_for_auto_run(cls, order: OrderSchema) -> List[str]:
        """
        Pre-flight approval policy for chat-originated ORDERs.
        Returns an empty list when the ORDER may execute autonomously (LOW/MEDIUM risk, valid,
        clearly scoped). Otherwise returns the reasons that require HUMAN_REQUIRED.
        Runtime gates (Scope Guard, Regression Gate, Same-Family guard, CONFIG_ERROR) are still
        enforced inside Orchestrator.run(); this is only the entry gate.
        """
        reasons: List[str] = []

        for v in cls.validate_order_paths(order):
            reasons.append(f"INVALID_PATH ({v})")

        if getattr(order, "requires_human_approval", False):
            reasons.append("EXPLICIT_HUMAN_APPROVAL_REQUESTED")
        if str(getattr(order, "risk_level", "LOW")).upper() == "HIGH":
            reasons.append("HIGH_RISK")
        if getattr(order, "allowed_deletions", None):
            reasons.append("DESTRUCTIVE_CHANGE (allowed_deletions present)")

        allowed = [p for p in (order.allowed_paths or []) if p is not None]
        # A zero write-budget ORDER cannot change a single file regardless of how its allowed_paths
        # reads - the post-build cap (validate_scope_post_build) enforces this independently of ENTRY
        # approval, so an ambiguous/empty scope is not actually a risk here and does not need a human
        # to bless it. Every other gate (HIGH_RISK, protected/sensitive paths, destructive change) is
        # unaffected - this narrows AMBIGUOUS_SCOPE only.
        provably_read_only = (order.max_files_changed == 0 and order.max_diff_lines == 0
                               and not (order.allowed_deletions or []))
        if not provably_read_only and (not allowed or any(cls.normalize_rel_path(p) in ("", ".", "/") for p in allowed)):
            reasons.append("AMBIGUOUS_SCOPE (allowed_paths empty or whole-repo)")

        for p in allowed:
            if cls.is_path_protected(p, order.protected_paths or []):
                reasons.append(f"PROTECTED_PATH_IN_SCOPE ({p})")
            for prot in (order.protected_paths or []):
                if cls.is_path_allowed(prot, [p]) and cls.normalize_rel_path(prot) != cls.normalize_rel_path(p):
                    reasons.append(f"PROTECTED_PATH_INSIDE_ALLOWED ({prot} under {p})")
            fname = Path(p).name.lower()
            if fname in cls.DEPENDENCY_FILES or fname in cls.PERMISSION_FILES or fname in cls.CRITICAL_CONFIG_FILES:
                reasons.append(f"SENSITIVE_FILE_IN_SCOPE ({p})")
            if ".git" in [seg.lower() for seg in Path(p).parts]:
                reasons.append(f"GIT_INTERNALS_IN_SCOPE ({p})")
            elif cls.is_control_plane_path(p):
                reasons.append(f"CONTROL_PLANE_IN_SCOPE ({p})")

        # de-duplicate, keep order
        seen = set()
        out = []
        for r in reasons:
            if r not in seen:
                seen.add(r)
                out.append(r)
        return out
