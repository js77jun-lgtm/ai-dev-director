"""Antigravity planning/review using the shared isolated official CLI runner."""
import json
import tempfile
from agents.claude_head import ClaudeHead
from core.bootstrap import prepare_project
from core.env_utils import resolve_cli
from core.research_runner import TriadResearchRunner
from schemas.review_result import ReviewResultSchema

class GoogleHead(ClaudeHead):
    agent_family = 'antigravity'
    def __init__(self, **kwargs):
        kwargs['executable'] = kwargs.get('executable') or resolve_cli('agy')
        super().__init__(**kwargs)
        self.provider = 'google'

    def _invoke_cli(self, prompt, cwd=None):
        from core import usage
        usage.set_context(role=getattr(self, "usage_role", "HEAD"))
        runner = TriadResearchRunner(executables={'GOOGLE': self.executable}, timeout=self.timeout,
                                    no_output_timeout=self.no_progress_timeout)
        with tempfile.TemporaryDirectory(prefix='director-google-role-') as tmp:
            prepare_project(tmp)
            result = runner.invoke('GOOGLE', 'Do not use tools. Use supplied evidence only.\n' + prompt, tmp, 'HEAD')
        self.last_execution_info = {'invocation_command': result['invocation'], 'exit_code': result['exit_code'],
                                    'execution_duration_sec': result['duration_sec']}
        return result['output']

    def get_provenance(self, exit_code=0):
        return super().get_provenance(exit_code).model_copy(update={
            'provider': 'google', 'runtime': 'antigravity-cli',
            'agent': 'REVIEWER' if isinstance(self, GoogleReviewer) else 'HEAD'})

class GoogleReviewer(GoogleHead):
    usage_role = 'REVIEWER'

    def review(self, order, git_diff, verification, project_context=None, worktree_path=None):
        prompt = ('Independently review the ORDER against the code diff and test evidence. '
                  'Never claim tests were run by you. Identify concrete defects and requirement violations. '
                  'File-count and diff-size limits are enforced by the Director: never report them. '
                  'Return only JSON: {"verdict":"pass or changes_required","p0":[],"p1":[],"p2":[],"summary":"..."}.\n'
                  + json.dumps({'order':order.model_dump(), 'diff':git_diff,
                                'verification':{k:v for k,v in verification.items() if k != 'git_diff'}}, ensure_ascii=False))
        return ReviewResultSchema(**self._extract_json(self._invoke_cli(prompt)))
