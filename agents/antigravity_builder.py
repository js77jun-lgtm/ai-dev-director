import os
import sys
import json
import time
import shutil
import subprocess
import logging
import re
from pathlib import Path, PurePosixPath
from datetime import datetime
from typing import Dict, Any, List, Optional, Callable

from core.env_utils import resolve_cli, get_sanitized_env
from core.process_runner import ProcessTreeRunner
from core.safety import SafetyPolicy
from schemas.order import OrderSchema
from schemas.builder_result import BuilderResultSchema
from schemas.provenance import AgentProvenanceRecord

import threading

# Only one Antigravity CLI build may run per process: the builder rewrites the user-global
# ~/.gemini/antigravity-cli/settings.json for the duration of the run, so concurrent builds
# would race on that file.
_AGY_GLOBAL_LOCK = threading.Lock()


class AntigravityBuilder:
    """
    Antigravity Builder Agent using the official Google Antigravity CLI ('agy') via ProcessTreeRunner.
    Direct API calls and PAYG API keys are strictly forbidden.
    """
    provider: str = "google"

    def __init__(
        self,
        executable: Optional[str] = None,
        approved_commands: Optional[List[str]] = None,
        model: str = "antigravity-cli",
        timeout: int = 600,
        no_progress_timeout: int = 180,
        heartbeat_interval: int = 30,
        **kwargs
    ):
        self.provider = "google"
        # Discover executable strictly from config -> env -> resolve_cli("agy")
        self.executable = executable or os.environ.get("ANTIGRAVITY_BIN") or resolve_cli("agy")
        if self.executable:
            self.executable = str(Path(self.executable).resolve())

        self.approved_commands = approved_commands
        self.model = model
        self.timeout = timeout
        self.no_progress_timeout = no_progress_timeout
        self.heartbeat_interval = heartbeat_interval
        self.logger = logging.getLogger("director.antigravity_builder")
        self.last_execution_info = {}

    def pre_tool_write_file(
        self,
        file_path: str,
        content: str,
        worktree_path: str,
        order: OrderSchema
    ) -> str:
        """
        Pre-tool safety verification before writing a file.
        Throws ScopeViolationError if the file violates allowed_paths or protected_paths.
        """
        from core.safety import SafetyPolicy, ScopeViolationError
        allowed, reason = SafetyPolicy.check_pre_tool_file_write(
            file_path=file_path,
            workspace_root=worktree_path,
            allowed_paths=order.allowed_paths,
            protected_paths=order.protected_paths
        )
        if not allowed:
            raise ScopeViolationError(reason)
        dest = Path(worktree_path) / file_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        return str(dest)

    def pre_tool_run_command(
        self,
        command: str,
        worktree_path: str,
        order: OrderSchema
    ) -> None:
        """
        Pre-tool safety verification before executing a shell command.
        """
        from core.safety import SafetyPolicy, ScopeViolationError
        allowed, reason = SafetyPolicy.check_pre_tool_command(
            command=command,
            approved_commands=self.approved_commands
        )
        if not allowed:
            raise ScopeViolationError(reason)

    def get_version(self) -> str:
        if not self.executable or not Path(self.executable).exists():
            return "unknown"
        from core.env_utils import probe_version
        v = probe_version(self.executable, timeout=10)
        return v if v != "UNKNOWN" else "unknown"

    def get_provenance(self, exit_code: int = 0) -> AgentProvenanceRecord:
        exec_path = str(Path(self.executable).resolve()) if self.executable else "agy"
        invocation = self.last_execution_info.get("invocation_command", "agy -p <order> --mode accept-edits --sandbox")
        version = self.get_version()
        duration = self.last_execution_info.get("execution_duration_sec")
        actual_exit_code = self.last_execution_info.get("exit_code")   # None when no process ran (never a default)

        return AgentProvenanceRecord(
            agent="BUILDER",
            provider="google",
            runtime="antigravity-cli",
            model="antigravity-cli",
            executable=exec_path,
            invocation_command=invocation,
            version=version,
            exit_code=actual_exit_code,
            duration_sec=duration,
            authentication_mode="subscription",
            api_key_used=False,
            timestamp=datetime.utcnow().isoformat()
        )

    @staticmethod
    def _control_plane_snapshot(worktree_path: Path) -> Dict[str, str]:
        """sha256 of every Director control-plane file inside the worktree (.agents/**, GEMINI.md, any
        .director_worktrees/** that should not exist). Compared before/after the CLI run: any difference means the
        Builder touched the control plane -> hard, non-approvable failure."""
        import hashlib
        wt = Path(worktree_path)
        snap: Dict[str, str] = {}
        for rel in ("GEMINI.md",):
            p = wt / rel
            if p.is_file():
                snap[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        for d in (".agents", ".director_worktrees"):
            base = wt / d
            if base.exists():
                for p in sorted(x for x in base.rglob("*") if x.is_file()):
                    snap[p.relative_to(wt).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        return snap

    @staticmethod
    def _permission_rel_path(path_rule: str) -> str:
        """Convert an ORDER path into an exact AGY permission target.

        Antigravity treats a directory target recursively, so ``src/**`` is
        represented as ``src``. Other wildcard shapes cannot be represented
        without broadening authority and therefore fail closed.
        """
        clean = str(path_rule or "").strip().replace("\\", "/").strip("/")
        if clean.endswith("/**"):
            clean = clean[:-3].rstrip("/")
        if not clean or SafetyPolicy.has_glob(clean):
            raise ValueError(f"unsupported scoped permission path pattern: {path_rule}")
        return clean

    def _setup_scoped_permissions(self, worktree_path: Path, order: OrderSchema) -> Dict[str, List[str]]:
        """
        Dynamically generates and provisions scoped permission rules for this run.
        Creates worktree rule files (GEMINI.md and .agents/hooks.json).
        """
        wt_posix = worktree_path.as_posix()
        wt_native = str(worktree_path)
        control_dirs = sorted(SafetyPolicy.CONTROL_PLANE_DIRS)
        control_files = sorted(SafetyPolicy.CONTROL_PLANE_FILES)
        allowed_permission_paths = [self._permission_rel_path(p) for p in order.allowed_paths]
        protected_permission_paths = []
        for rule in order.protected_paths:
            try:
                protected_permission_paths.append(self._permission_rel_path(rule))
            except ValueError:
                # A deny glob entirely outside every granted path needs no native rule:
                # deny-by-default already forbids it. Keep it in ORDER for Scope Guard.
                # Any potential overlap remains fail-closed, never broaden a write grant.
                # When no write path was granted at all (allowed_permission_paths is empty),
                # deny-by-default already blocks every write regardless of what this
                # unrepresentable pattern (e.g. a bare "**") might otherwise match, so it is
                # always safe to skip the native rule in that case - there is no grant left
                # for it to fail to cover.
                prefix = re.split(r'[\*?\[]', rule.replace('\\', '/'), maxsplit=1)[0].lower()
                grants = [p.lower().rstrip('/') for p in allowed_permission_paths]
                if grants and (not prefix or any(prefix.startswith(p.rstrip('/') + '/') or p.startswith(prefix) or prefix.rstrip('/') == p for p in grants)):
                    raise
        self._native_protected_permission_paths = protected_permission_paths
        allow_writes = [f"write_file({wt_posix}/{p})" for p in allowed_permission_paths]
        # The Director performs Git inspection and deterministic verification after the Builder. Headless AGY on
        # Windows may request an interactive admin escalation even for read-only shell commands, so the Builder
        # gets no terminal permission at all; it may only use scoped file tools.
        allow_commands = []

        deny_writes = [f"write_file({pat.replace('/**', '').replace('/*', '')})"
                       for pat in SafetyPolicy.CONTROL_PLANE_PATTERNS] + [
            f"write_file({wt_posix}/{p})" for p in protected_permission_paths
        ] + [
            f"write_file(.. outside {wt_posix} ..)"
        ]
        deny_commands = [
            "command(*)",
            "unsandboxed(*)",
            "command(regex:git push.*)",
            "command(regex:git reset.*)",
            "command(regex:git clean.*)",
            "command(regex:git checkout.*)",
            "command(regex:git branch -D.*)",
            "command(regex:rm -rf.*)",
            "command(regex:del /s.*)",
            "command(regex:format.*)"
        ]

        # 1. Provision GEMINI.md in worktree
        gemini_md_content = (
            "# SCOPED PERMISSION RULES\n\n"
            "## ALLOW\n"
            + "\n".join(f"- {item}" for item in (allow_writes + allow_commands)) + "\n\n"
            "## DENY\n"
            + "\n".join(f"- {item}" for item in (deny_writes + deny_commands)) + "\n"
        )
        (worktree_path / "GEMINI.md").write_text(gemini_md_content, encoding="utf-8")

        # 2. Provision .agents/hooks.json and permission_guard.py in worktree
        agents_dir = worktree_path / ".agents"
        agents_dir.mkdir(exist_ok=True)

        guard_script = agents_dir / "permission_guard.py"
        guard_code = f"""import sys, json, re
from pathlib import Path

try:
    payload = json.load(sys.stdin)
    tool = payload.get("toolCall", {{}})
    name = tool.get("name", "")
    args = tool.get("args", {{}})

    # Check file write tools
    if name in ("write_to_file", "replace_file_content", "write_file", "edit_file"):
        import os
        target = args.get("TargetFile") or args.get("file_path") or args.get("path") or ""
        root = os.path.realpath(r"{wt_native}")
        raw = str(target)
        if not raw or raw.startswith("~") or raw.startswith(chr(92) * 2) or raw.startswith("//"):
            print(json.dumps({{"decision": "deny", "reason": "Scoped permission DENY: invalid target path"}}))
            sys.exit(0)
        cand = raw if os.path.isabs(raw) else os.path.join(root, raw)
        real = os.path.realpath(cand)
        try:
            inside = os.path.commonpath([root, real]) == root
        except ValueError:
            inside = False
        rel_parts = [p.lower() for p in os.path.relpath(real, root).replace(os.sep, "/").split("/")] if inside else []
        if not inside:
            print(json.dumps({{"decision": "deny", "reason": "Scoped permission DENY: write outside allowed worktree (canonical containment)"}}))
            sys.exit(0)
        control_dirs = {control_dirs!r}
        control_files = {control_files!r}
        if any(p in control_dirs for p in rel_parts) or (len(rel_parts) == 1 and rel_parts[0] in control_files):
            print(json.dumps({{"decision": "deny", "reason": "Scoped permission DENY: Director control-plane path is never writable"}}))
            sys.exit(0)

    # Check shell execution tools
    if name in ("run_command", "execute_command", "bash", "terminal"):
        cmd = args.get("CommandLine") or args.get("command") or ""
        for pattern in [r"\\bgit\\s+(push|reset|clean|checkout|branch\\s+-D)\\b", r"\\b(rm\\s+-rf|del\\s+/s|format)\\b"]:
            if re.search(pattern, cmd):
                print(json.dumps({{"decision": "deny", "reason": f"Scoped permission DENY: command '{{cmd}}' is forbidden"}}))
                sys.exit(0)

    print(json.dumps({{"decision": "allow"}}))
except Exception as e:
    print(json.dumps({{"decision": "deny", "reason": f"Permission guard hook error: {{e}}"}}))
    sys.exit(0)
"""
        guard_script.write_text(guard_code, encoding="utf-8")

        hooks_config = {
            "scoped-permission-guard": {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "command": f"python {guard_script.as_posix()}"
                            }
                        ]
                    }
                ]
            }
        }
        (agents_dir / "hooks.json").write_text(json.dumps(hooks_config, indent=2), encoding="utf-8")

        return {
            "allow_writes": allow_writes,
            "allow_commands": allow_commands,
            "deny_writes": deny_writes,
            "deny_commands": deny_commands
        }

    SAFE_DEFAULT_SETTINGS = {
        "permissions": {
            "allow": [],
            "deny": [
                "write_file(.git)",
                "write_file(.agents)",
                "write_file(.director_worktrees)",
                "write_file(GEMINI.md)",
                "command(regex:git push.*)",
                "command(regex:git reset.*)",
                "command(regex:git clean.*)",
                "command(regex:git checkout.*)",
                "command(regex:git branch -D.*)",
                "command(regex:rm -rf.*)",
                "command(regex:del /s.*)",
                "command(regex:format.*)"
            ]
        }
    }

    def _restore_settings(self, settings_file: Path, backup_file: Path, orig_settings_content: Optional[str]) -> bool:
        """Puts the user's original settings.json back (wildcards stripped) using an atomic replace.
        Returns True on success (snapshot removed). On failure the snapshot is PRESERVED and False is returned;
        the caller must surface PERMISSION_RESTORE_FAILED. Called on normal completion, exceptions and
        permission-setup failure."""
        try:
            if orig_settings_content:
                try:
                    data = json.loads(orig_settings_content)
                except Exception:
                    data = dict(self.SAFE_DEFAULT_SETTINGS)
                perms = data.get("permissions", {}) if isinstance(data, dict) else {}
                allow_list = perms.get("allow", []) if isinstance(perms, dict) else []
                perms["allow"] = [a for a in allow_list
                                  if a not in ("write_file(*)", "write_file(**)", "command(*)", "unsandboxed(*)",
                                               "read_file(*)", "list_dir(*)")]
                data["permissions"] = perms
                data.pop("_director_scoped", None)
                restored_text = json.dumps(data, indent=2)
            else:
                restored_text = json.dumps(self.SAFE_DEFAULT_SETTINGS, indent=2)
            tmp = settings_file.with_name(settings_file.name + ".director-restore")
            tmp.write_text(restored_text, encoding="utf-8")
            os.replace(str(tmp), str(settings_file))
            # verify: no scoped marker may remain
            check = json.loads(settings_file.read_text(encoding="utf-8"))
            if isinstance(check, dict) and "_director_scoped" in check:
                raise RuntimeError("scoped marker still present after restore")
        except Exception as e:
            self.logger.error(f"[Antigravity Builder] PERMISSION_RESTORE_FAILED: settings.json restore failed: {e}; "
                              f"original snapshot preserved at {backup_file}")
            return False
        try:
            if backup_file.exists():
                backup_file.unlink()
        except Exception:
            pass
        return True

    def run(
        self,
        order: OrderSchema,
        worktree_path: str,
        builder_fn: Optional[Callable[["AntigravityBuilder", OrderSchema, str], List[str]]] = None
    ) -> BuilderResultSchema:
        """
        Executes Antigravity CLI subprocess inside the RUN Git worktree with scoped permissions.
        """
        wt = Path(worktree_path).resolve()
        start_time = time.time()

        # If custom/test function provided for unit test suite
        if builder_fn is not None:
            self.logger.info("[Antigravity Builder] Executing custom/test builder function")
            modified = builder_fn(self, order, worktree_path) or []
            return BuilderResultSchema(
                status="COMPLETED",
                files_modified=modified,
                commands_run=[],
                summary=f"Custom builder executed on {len(modified)} files.",
                error=None
            )

        if not self.executable or not Path(self.executable).exists():
            err_msg = f"CONFIG_ERROR: Antigravity CLI executable not found: {self.executable}"
            self.logger.error(err_msg)
            return BuilderResultSchema(
                status="FAILED",
                files_modified=[],
                commands_run=[],
                summary="Antigravity CLI missing",
                error=err_msg
            )

        # 0. Global Antigravity execution lock: in-process (threads) + cross-process file lock
        from core.file_lock import FileLock
        from core.db_path import get_lock_dir
        if not _AGY_GLOBAL_LOCK.acquire(blocking=False):
            return BuilderResultSchema(
                status="FAILED", files_modified=[], commands_run=[],
                summary="Antigravity busy (in-process)",
                error="BUILDER_BUSY: another Antigravity build is running in this process"
            )
        file_lock = FileLock(get_lock_dir() / "antigravity.lock", max_age_sec=self.timeout + 300, note=str(wt))
        if not file_lock.try_acquire():
            owner = file_lock.read_owner() or {}
            _AGY_GLOBAL_LOCK.release()
            return BuilderResultSchema(
                status="FAILED", files_modified=[], commands_run=[],
                summary="Antigravity busy (another process)",
                error=f"BUILDER_BUSY: Antigravity lock held by PID {owner.get('pid', '?')} ({owner.get('note', '')})"
            )
        self._file_lock = file_lock
        try:
            # 1. Provision Scoped Permissions for this worktree
            scoped_perms = self._setup_scoped_permissions(wt, order)
            self._cp_before = self._control_plane_snapshot(wt)
        except Exception as e:
            file_lock.release()
            _AGY_GLOBAL_LOCK.release()
            return BuilderResultSchema(
                status="FAILED",
                files_modified=[],
                commands_run=[],
                summary="Scoped permission setup failed",
                error=f"PERMISSION_SETUP_FAILED: scoped permission provisioning failed: {e}"
            )

        # Dynamic management of ~/.gemini/antigravity-cli/settings.json
        # Crash recovery: the original is snapshotted to settings.json.director-original before any change.
        # A settings.json carrying the "_director_scoped" marker is a leftover from an interrupted run and is
        # never mistaken for the user's original.
        settings_file = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        backup_file = settings_file.with_name(settings_file.name + ".director-original")
        orig_settings_content = None
        try:
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            leftover = None
            if settings_file.exists():
                try:
                    leftover = json.loads(settings_file.read_text(encoding="utf-8"))
                except Exception:
                    leftover = None
            if isinstance(leftover, dict) and "_director_scoped" in leftover:
                # previous run died (or failed to restore) while scoped permissions were active:
                # the original MUST be recovered before any build; the snapshot is never overwritten with scoped content
                if backup_file.exists():
                    os.replace(str(backup_file), str(settings_file))
                    self.logger.warning("[Antigravity Builder] Restored original settings.json left behind by an interrupted run")
                else:
                    settings_file.unlink()
                    self.logger.warning("[Antigravity Builder] Removed leftover scoped settings.json (no original snapshot existed)")
                if settings_file.exists():
                    verify = json.loads(settings_file.read_text(encoding="utf-8"))
                    if isinstance(verify, dict) and "_director_scoped" in verify:
                        raise RuntimeError("scoped settings still active after recovery attempt")
            if settings_file.exists():
                orig_settings_content = settings_file.read_text(encoding="utf-8")
                if "_director_scoped" in orig_settings_content:
                    raise RuntimeError("refusing to snapshot scoped settings as original")
                tmp_backup = backup_file.with_name(backup_file.name + ".tmp")
                tmp_backup.write_text(orig_settings_content, encoding="utf-8")
                os.replace(str(tmp_backup), str(backup_file))
            else:
                if backup_file.exists():
                    backup_file.unlink()
        except Exception as e:
            try:
                shutil.rmtree(wt / ".agents", ignore_errors=True)
                (wt / "GEMINI.md").unlink(missing_ok=True)
            except Exception:
                pass
            file_lock.release()
            _AGY_GLOBAL_LOCK.release()
            return BuilderResultSchema(
                status="FAILED", files_modified=[], commands_run=[],
                summary="Permission snapshot failed",
                error=f"PERMISSION_SETUP_FAILED: could not snapshot global settings.json: {e}"
            )

        try:
            # Build strict DENY-BY-DEFAULT scoped permissions configuration
            # No open wildcards (*): write_file(*), command(*), unsandboxed(*), read_file(*), list_dir(*) are
            # strictly forbidden
            wt_posix = wt.as_posix()
            dynamic_allow = [
                f"read_file({wt_posix})",
                f"list_dir({wt_posix})",
                # Antigravity reads its own injected context/hook files at session start (GEMINI.md,
                # .agents/hooks.json) and lists the worktree root; write to both stays denied via
                # CONTROL_PLANE_PATTERNS below.
                f"read_file({wt_posix}/GEMINI.md)",
                f"read_file({wt_posix}/.agents/hooks.json)",
            ]
            listed_dirs = set()
            for clean_p in (self._permission_rel_path(p) for p in order.allowed_paths):
                dynamic_allow.append(f"write_file({wt_posix}/{clean_p})")
                dynamic_allow.append(f"read_file({wt_posix}/{clean_p})")
                # Antigravity lists an allowed path's ancestor directories (e.g. "src/", "tests/") before
                # writing into them, especially on a GREENFIELD worktree where they do not exist yet - grant
                # list_dir on every ancestor, not just the worktree root, so that lookup is never auto-denied.
                ancestor = PurePosixPath(clean_p).parent
                while str(ancestor) not in (".", ""):
                    listed_dirs.add(str(ancestor))
                    ancestor = ancestor.parent
            for rel_dir in sorted(listed_dirs):
                dynamic_allow.append(f"list_dir({wt_posix}/{rel_dir})")

            dynamic_deny = [
                f"write_file({wt_posix}/.git)",
            ] + [
                f"write_file({wt_posix}/{pat.replace('/**', '').replace('/*', '')})"
                for pat in SafetyPolicy.CONTROL_PLANE_PATTERNS
            ] + [
                f"write_file({wt_posix}/{p})"
                for p in self._native_protected_permission_paths
            ] + [
                "command(*)",
                "unsandboxed(*)",
                "command(regex:git push.*)",
                "command(regex:git reset.*)",
                "command(regex:git clean.*)",
                "command(regex:git checkout.*)",
                "command(regex:git branch -D.*)",
                "command(regex:rm -rf.*)",
                "command(regex:del /s.*)",
                "command(regex:format.*)",
                "command(regex:Get-PSDrive.*)"
            ]

            settings_data = {
                "permissions": {
                    "allow": dynamic_allow,
                    "deny": dynamic_deny
                },
                "_director_scoped": {"pid": os.getpid(), "worktree": wt_posix, "created_at": datetime.utcnow().isoformat()}
            }
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            scoped_text = json.dumps(settings_data, indent=2)
            tmp_settings = settings_file.with_name(settings_file.name + ".director-tmp")
            tmp_settings.write_text(scoped_text, encoding="utf-8")
            os.replace(str(tmp_settings), str(settings_file))
            # Evidence: read back and verify the scoped permissions are exactly what will govern the run
            applied = json.loads(settings_file.read_text(encoding="utf-8"))
            if applied.get("permissions") != settings_data["permissions"] or "_director_scoped" not in applied:
                raise RuntimeError("settings.json read-back does not match the scoped permissions written")
            for a in applied["permissions"]["allow"]:
                if a in ("write_file(*)", "write_file(**)", "command(*)", "unsandboxed(*)",
                         "read_file(*)", "list_dir(*)"):
                    raise RuntimeError(f"wildcard permission present after write: {a}")
            self.logger.info("[Antigravity Builder] Dynamically applied strict Deny-by-Default scoped permissions (verified by read-back)")
        except Exception as e:
            # FAIL-CLOSED: without verified permission setup Antigravity must not run
            self.logger.error(f"[Antigravity Builder] PERMISSION_SETUP_FAILED: {e}")
            self._restore_settings(settings_file, backup_file, orig_settings_content)
            try:
                shutil.rmtree(wt / ".agents", ignore_errors=True)
                (wt / "GEMINI.md").unlink(missing_ok=True)
            except Exception:
                pass
            file_lock.release()
            _AGY_GLOBAL_LOCK.release()
            return BuilderResultSchema(
                status="FAILED", files_modified=[], commands_run=[],
                summary="Permission setup failed; Antigravity not executed",
                error=f"PERMISSION_SETUP_FAILED: {e}"
            )

        prompt_text = (
            f"GOAL: {order.goal}\n\n"
            f"ORDER CONTEXT (does not expand allowed paths or permissions): {order.context or ''}\n\n"
            f"ALLOWED PATHS: {json.dumps(order.allowed_paths)}\n"
            f"PROTECTED PATHS: {json.dumps(order.protected_paths)}\n"
            f"ALLOWED DELETIONS: {json.dumps(order.allowed_deletions)}\n"
            f"ACCEPTANCE CRITERIA: {json.dumps(order.acceptance_criteria)}\n\n"
            "EXISTING TESTS ARE PROTECTED: never delete, rename, skip, weaken or replace an existing test file or "
            "test function unless ALLOWED DELETIONS lists that exact path. Add new tests next to the existing ones. "
            "If the goal seems to require changing or removing an existing test, do NOT do it: finish the rest, "
            "keep the test as it is, and explain the conflict in your final summary so the Director can ask the operator. "
            "A result that removed or rewrote existing tests is rejected by the Reviewer as an unauthorised deletion.\n\n"
            f"HARD CHANGE CAPS: at most {order.max_files_changed} changed files and at most "
            f"{order.max_diff_lines} total added/deleted lines. These are fail-closed limits; implement the "
            "smallest complete solution that stays below both caps.\n\n"
            "QUALITY GATE BEFORE FINAL RESPONSE: Re-read every acceptance criterion and inspect both code and "
            "tests for falsy-value defaulting bugs (especially None versus explicit empty containers), invalid "
            "or incomplete states comparing equal, and missing boundary assertions. Do not report success until "
            "the implementation and tests explicitly preserve those distinctions where applicable.\n\n"
            "SCOPED PERMISSION RULES (ENFORCED):\n"
            "ALLOW:\n"
            + "\n".join(f"- {p}" for p in (scoped_perms["allow_writes"] + scoped_perms["allow_commands"])) + "\n\n"
            "DENY:\n"
            + "\n".join(f"- {p}" for p in (scoped_perms["deny_writes"] + scoped_perms["deny_commands"])) + "\n\n"
            "PRESERVATION MANDATE: Preserve all existing functions, classes, and tests verbatim without deletion or replacement!\n"
            "Do not invoke run_command, Bash, terminal, or any sandbox bypass. The Director runs Git checks and "
            "deterministic verification after you finish. Use only file tools to write the required code and tests "
            "directly into the allowed paths."
        )

        invocation_cmd = (
            "agy --input-format stream-json --output-format stream-json "
            "--mode accept-edits --sandbox <stdin>"
        )
        self.logger.info(f"[Antigravity Builder] Subprocess execution: {self.executable} in {wt}")

        def _execute_agy() -> BuilderResultSchema:
            # Antigravity's official stream protocol keeps long prompts off the Windows
            # command line and provides an explicit terminal result event.  The same
            # protocol is used by the read-only TRIAD runner; Builder remains writable
            # only through the scoped settings installed above.
            cmd = [
                self.executable,
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--mode", "accept-edits",
                "--sandbox"
            ]
            active_prompt = prompt_text
            attempts = []
            final_event = None
            res = None
            for attempt_no in range(1, 3):
                message = {
                    "event": "user",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": active_prompt}],
                    },
                }
                res = ProcessTreeRunner.run(
                    argv=cmd,
                    cwd=str(wt),
                    env=get_sanitized_env(),
                    timeout=self.timeout,
                    no_output_timeout=self.no_progress_timeout,
                    heartbeat_interval=self.heartbeat_interval,
                    stdin_data=json.dumps(message, ensure_ascii=False) + "\n",
                )
                attempts.append({
                    "attempt": attempt_no,
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                    "exit_code": res.exit_code,
                    "duration_sec": res.duration_sec,
                })
                final_event = None
                for line in (res.stdout or "").splitlines():
                    try:
                        event = json.loads(line)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if event.get("event") == "result" and isinstance(event.get("result"), dict):
                        final_event = event["result"]

                detail = str((final_event or {}).get("error") or "")
                output_limited = (
                    (final_event or {}).get("status") != "SUCCESS"
                    and "output token limit" in detail.lower()
                )
                if not output_limited or attempt_no == 2:
                    break

                self.logger.warning(
                    "[Antigravity Builder] Output token limit reached; preserving edits and retrying once with a compact continuation"
                )
                active_prompt = (
                    "CONTINUE THE CURRENT REPAIR IN THIS WORKTREE. Inspect the files already changed by your prior "
                    "attempt, finish only the unresolved acceptance/review items, and use file-edit tools directly. "
                    "Do not repeat code or explanations in the response. Keep the final response under 500 characters. "
                    "Do not run commands. Preserve existing functions and tests."
                )

            assert res is not None

            self.last_execution_info = {
                "executable": self.executable,
                "version": self.get_version(),
                "invocation_command": invocation_cmd,
                "stdout": res.stdout,
                "stderr": res.stderr,
                "exit_code": res.exit_code,
                "execution_duration_sec": sum(a["duration_sec"] for a in attempts),
                "attempts": attempts,
            }

            self.logger.info(f"[Antigravity Builder] agy finished with code {res.exit_code} in {res.duration_sec:.2f}s")

            if res.timed_out:
                return BuilderResultSchema(
                    status="FAILED",
                    files_modified=[],
                    commands_run=[invocation_cmd],
                    summary="Antigravity CLI timed out",
                    error=f"TIMEOUT: {res.timeout_reason}"
                )

            if res.exit_code != 0:
                return BuilderResultSchema(
                    status="FAILED",
                    files_modified=[],
                    commands_run=[invocation_cmd],
                    summary="Antigravity CLI process returned non-zero exit code",
                    error=res.stderr or res.stdout
                )

            if not final_event:
                return BuilderResultSchema(
                    status="FAILED",
                    files_modified=[],
                    commands_run=[invocation_cmd],
                    summary="Antigravity stream ended without a terminal result",
                    error="AGY_STREAM_RESULT_MISSING",
                )
            if final_event.get("status") != "SUCCESS":
                detail = str(final_event.get("error") or final_event.get("status") or "UNKNOWN")[:500]
                return BuilderResultSchema(
                    status="FAILED",
                    files_modified=[],
                    commands_run=[invocation_cmd],
                    summary="Antigravity stream returned a failed terminal result",
                    error="AGY_STREAM_FAILED: " + detail,
                )
            provider_response = str(final_event.get("response") or "")

            # Control-plane integrity: the Builder must not have touched .agents/**, GEMINI.md or created
            # .director_worktrees/** (checked BEFORE cleanup so the evidence is compared, never assumed)
            cp_after = self._control_plane_snapshot(wt)
            cp_before = getattr(self, "_cp_before", {}) or {}
            changed = sorted(set(k for k in set(cp_before) | set(cp_after) if cp_before.get(k) != cp_after.get(k)))
            if changed:
                self.logger.error(f"[Antigravity Builder] CONTROL_PLANE_MODIFIED: {changed}")
                return BuilderResultSchema(
                    status="FAILED",
                    files_modified=[],
                    commands_run=[invocation_cmd],
                    summary="Builder modified Director control-plane files (kept in worktree for inspection)",
                    error="CONTROL_PLANE_MODIFIED: " + "; ".join(f"Control plane modified: {c}" for c in changed)
                )

            # Clean up temporary scoped permission rules and hooks before checking modified files
            try:
                if (wt / "GEMINI.md").exists():
                    (wt / "GEMINI.md").unlink()
                if (wt / ".agents").exists():
                    shutil.rmtree(wt / ".agents", ignore_errors=True)
            except Exception:
                pass

            # Discover modified files from git diff in worktree
            diff_res = subprocess.run(
                ["git", "-c", "core.quotepath=false", "status", "--porcelain", "--untracked-files=all"],
                cwd=str(wt),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8"
            )
            if diff_res.returncode != 0:
                return BuilderResultSchema(
                    status="FAILED",
                    files_modified=[],
                    commands_run=[invocation_cmd],
                    summary="Builder output could not be inspected",
                    error="BUILDER_GIT_STATUS_FAILED: " + (diff_res.stderr or "git status failed")[:500]
                )
            raw_modified = [line[3:].strip() for line in diff_res.stdout.splitlines() if line.strip()]
            # Filter internal director scaffolding
            files_modified = [
                f for f in raw_modified
                if not (f.startswith(".agents") or f == "GEMINI.md" or "__pycache__" in f or ".pytest_cache" in f)
            ]

            if not files_modified:
                provider_output = (provider_response or res.stderr or "").strip().replace("\x00", "")
                detail = f"; provider output: {provider_output[:500]}" if provider_output else ""
                return BuilderResultSchema(
                    status="FAILED",
                    files_modified=[],
                    commands_run=[invocation_cmd],
                    summary="Antigravity exited successfully but made no task changes",
                    error="BUILDER_NO_CHANGES: exit_code=0 and git status contained no task files" + detail
                )

            return BuilderResultSchema(
                status="COMPLETED",
                files_modified=files_modified,
                commands_run=[invocation_cmd],
                summary=f"Antigravity CLI successfully applied changes in {res.duration_sec:.2f}s.",
                error=None
            )
        result: Optional[BuilderResultSchema] = None
        restore_ok = True
        try:
            result = _execute_agy()
        except Exception as e:
            result = BuilderResultSchema(
                status="FAILED",
                files_modified=[],
                commands_run=[invocation_cmd],
                summary="Antigravity CLI failed to launch",
                error=str(e)
            )
        finally:
            # Clean up temporary scoped permission rules and hooks before git status
            try:
                if (wt / "GEMINI.md").exists():
                    (wt / "GEMINI.md").unlink()
                if (wt / ".agents").exists():
                    shutil.rmtree(wt / ".agents", ignore_errors=True)
            except Exception:
                pass

            # Restore the original settings.json (atomic replace from the snapshot; never restore open wildcards)
            restore_ok = self._restore_settings(settings_file, backup_file, orig_settings_content)
            try:
                file_lock.release()
            except Exception:
                pass
            try:
                _AGY_GLOBAL_LOCK.release()
            except RuntimeError:
                pass

        if not restore_ok:
            # The user's original permissions are NOT back in place: fail closed, keep the snapshot, escalate.
            return BuilderResultSchema(
                status="FAILED",
                files_modified=list(result.files_modified) if result else [],
                commands_run=[invocation_cmd],
                summary="Global settings restore failed; scoped permissions may still be active",
                error=(f"PERMISSION_RESTORE_FAILED: could not restore {settings_file}; original snapshot preserved at "
                       f"{backup_file}; scoped settings still active; build result was {result.status if result else 'n/a'}")
            )
        return result
