"""Builder proposes complete file contents; Director validates and applies them."""
import json
import os
import sys
import tempfile
from pathlib import Path
from agents.antigravity_builder import AntigravityBuilder
from core.bootstrap import prepare_project
from core.research_runner import TriadResearchRunner, ResearchProviderCallError
from core.safety import SafetyPolicy
from core.autopilot import required_output_path as autopilot_output
from schemas.builder_result import BuilderResultSchema


class ProposalBuilder(AntigravityBuilder):
    @staticmethod
    def _repair_invalid_json_escapes(text):
        """Repair only JSON escapes that json.loads cannot legally interpret.

        AI code payloads occasionally contain a stray backslash before indentation
        (``\\n\\    code``).  Removing that one invalid marker recovers the intended
        source.  For a non-whitespace invalid escape, preserve the slash by escaping
        it.  Structure, quotes and all valid JSON escapes are left untouched.
        """
        out=[]; in_string=False; i=0; repairs=[]
        while i < len(text):
            ch=text[i]
            if ch == '"':
                # A quote is escaped when preceded by an odd slash run.
                slash_count=0; j=i-1
                while j >= 0 and text[j] == '\\': slash_count += 1; j -= 1
                if slash_count % 2 == 0: in_string = not in_string
                out.append(ch); i += 1; continue
            if in_string and ch == '\\' and i + 1 < len(text):
                nxt=text[i+1]
                if nxt not in '"\\/bfnrtu':
                    repairs.append({'offset':i, 'next':repr(nxt)})
                    if nxt.isspace():
                        i += 1; continue
                    out.append('\\\\')
                    i += 1; continue
            out.append(ch); i += 1
        return ''.join(out), repairs

    @classmethod
    def parse_proposal(cls, text):
        candidate=text; repairs=[]; original=None
        for _ in range(64):
            try:
                return json.loads(candidate), repairs
            except json.JSONDecodeError as error:
                original = original or error
                if error.msg.startswith('Extra data'):
                    # a complete JSON object followed by commentary: the object is the proposal
                    return json.JSONDecoder().raw_decode(candidate)[0], repairs + [{'offset': error.pos, 'next': 'trailing text dropped'}]
                if not error.msg.startswith('Invalid \\escape') or error.pos + 1 >= len(candidate):
                    raise original
                pos=error.pos; nxt=candidate[pos+1]
                slash_count=0; previous=pos-1
                while previous >= 0 and candidate[previous] == '\\':
                    slash_count += 1; previous -= 1
                repairs.append({'offset':pos, 'next':repr(nxt)})
                # Whitespace after a slash is always a stray formatting marker.
                # In an odd run such as JSON ``\\\\\\s`` the decoder points at
                # the final slash; dropping it preserves the already escaped slash.
                if nxt.isspace() or slash_count:
                    candidate=candidate[:pos]+candidate[pos+1:]
                else:
                    candidate=candidate[:pos]+'\\'+candidate[pos:]
        raise original or ValueError('Builder JSON repair limit exceeded')

    def __init__(self, provider='google', **kwargs):
        from core.role_settings import ALIASES, DOMAINS
        from core.env_utils import resolve_cli
        selected = ALIASES[provider.lower()]
        executable = kwargs.pop('executable', None) or resolve_cli({'openai':'codex','claude':'claude','antigravity':'agy'}[selected])
        super().__init__(executable=executable, **kwargs)
        self.provider = DOMAINS[selected].lower()
        self.agent_family = {'openai':'codex','claude':'claude','antigravity':'antigravity'}[selected]

    def get_provenance(self, exit_code=0):
        return super().get_provenance(exit_code).model_copy(update={
            'provider': self.provider, 'runtime': self.agent_family + '-cli'})

    def run(self, order, worktree_path, builder_fn=None):
        if builder_fn is not None:
            return super().run(order, worktree_path, builder_fn)
        from core import usage
        usage.set_context(role='BUILDER')
        self.last_execution_info = {'exit_code': None, 'attempts': [], 'output': ''}
        root = Path(worktree_path).resolve()
        from core.builder_context import referenced_documents
        documents = referenced_documents(order)
        sources = {}
        candidates = []
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.')
                             and d not in {'node_modules', '__pycache__', 'data'}
                             and not (Path(directory) / d).is_symlink())
            candidates.extend(Path(directory) / name for name in sorted(names))
        editable, context = [], []
        for path in candidates:
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            if any(p.startswith('.') or p in {'node_modules', '__pycache__', 'data'} for p in Path(rel).parts):
                continue
            allowed, _ = SafetyPolicy.check_pre_tool_file_write(rel, str(root), order.allowed_paths, order.protected_paths)
            # Write protection must not hide baseline source from a fork/upgrade.
            readable_source = path.suffix.lower() in {'.py', '.js', '.ts', '.tsx', '.jsx', '.md', '.toml'}
            sensitive = any(word in rel.lower() for word in ('secret', 'credential', 'token', 'private_key'))
            if sensitive or path.stat().st_size >= 100_000:
                continue
            if allowed:
                editable.append((rel, path))
            elif readable_source:
                context.append((rel, path))
        # The files the ORDER lets the Builder change come first (a budget spent on unrelated files once hid the only
        # file a milestone had to edit), ranked: required outputs, then files the ORDER/repair notes name, then the
        # rest smallest first. An oversized prompt is its own failure (empty responses, 600 s timeouts), so what does
        # not fit is listed by path only and the Builder is told not to rewrite it.
        order_text = '\n'.join([order.goal, order.context or '', *order.acceptance_criteria])
        wanted = {p for p in (autopilot_output(r) for r in order.required_outputs) if p}

        def rank(item):
            rel, path = item
            named = rel in order_text or Path(rel).name in order_text
            return (0 if rel in wanted else 1 if named else 2, path.stat().st_size, rel)
        editable.sort(key=rank)
        context.sort(key=lambda item: ('test' in Path(item[0]).name.lower() or '/tests/' in '/' + item[0], item[0]))
        omitted = []
        for budget, group in ((self.EDITABLE_BUDGET, editable), (self.CONTEXT_BUDGET, context)):
            used = 0
            for rel, path in group:
                text = path.read_text(encoding='utf-8', errors='replace')
                if used + len(text) <= budget:
                    sources[rel] = text
                    used += len(text)
                elif group is editable:
                    omitted.append(rel)
        self.last_context_files = list(sources)          # in priority order: editable first
        self.last_omitted_files = omitted
        prompt = ('You are the implementation Builder. Do not use tools. All source context is supplied below. '
                  'Return ONLY JSON: {"files":[{"path":"relative/file.py","content":"complete file contents"}],'
                  '"edits":[{"path":"existing/file.py","old":"exact text copied from that file","new":"replacement text"}],'
                  '"summary":"..."}. Prefer "edits" for changes to existing files - each "old" must appear EXACTLY ONCE in '
                  'the file as shown below (copy it verbatim with indentation, long enough to be unique); use "files" for '
                  'new files or complete rewrites. Keep the reply compact. '
                  'Implement the ORDER and real behavior tests. Preserve existing behavior. No placeholder implementations. '
                  'Return only changed files. No deletion operations. Stay within file and diff budgets. '
                  'HEAD feedback and outstanding reviewer findings in context must be resolved. '
                  'Director host Python is ' + sys.version.split()[0] + '; platform is ' + sys.platform +
                  '. Honor any project-specific runtime requirements in the ORDER.\nORDER:\n' + order.model_dump_json() +
                  '\nREFERENCE DOCUMENTS:\n' + json.dumps(documents, ensure_ascii=False) +
                  '\nREFERENCE SOURCE FILES (read-only unless ORDER allows writes; omitted files are not necessarily absent):\n' + json.dumps(sources, ensure_ascii=False)
                  + ('\nWRITABLE FILES NOT SHOWN (too large for this prompt; they exist - do NOT rewrite or edit them): '
                     + json.dumps(omitted) if omitted else ''))
        domain = self.provider.upper()
        runner = TriadResearchRunner(executables={domain: self.executable}, timeout=self.timeout,
                                     no_output_timeout=self.no_progress_timeout)
        with tempfile.TemporaryDirectory(prefix='director-builder-') as tmp:
            prepare_project(tmp)
            try:
                result = runner.invoke(domain, prompt, tmp, 'BUILDING')
            except ResearchProviderCallError as exc:
                self.last_execution_info = dict(exc.meta)
                self.last_execution_info.update(error=str(exc), output='', context_files=self.last_context_files,
                    attempts=getattr(exc, 'attempts', []), exit_code=exc.meta.get('exit_code'),
                    execution_duration_sec=sum(a.get('duration_sec', 0) for a in getattr(exc, 'attempts', [])))
                raise
        self.last_execution_info = {'invocation_command': result['invocation'], 'exit_code': result['exit_code'],
                                    'execution_duration_sec': result['duration_sec'], 'output': result['output'],
                                    'attempts': result.get('attempts', []), 'attempt_number': result.get('attempt_number', 1),
                                    'context_files': self.last_context_files, 'prompt_chars': len(prompt),
                                    'omitted_files': self.last_omitted_files}
        text = result['output'].strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        proposal, repairs = self.parse_proposal(text)
        if repairs:
            self.last_execution_info['json_repairs'] = repairs
        if self.provider == 'anthropic' and isinstance(proposal, dict) and proposal.get('type') == 'result':
            if proposal.get('is_error'):
                raise ValueError('Claude Builder returned an error result')
            text = proposal.get('result', '').strip()
            if text.startswith('```'):
                text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
            proposal, envelope_repairs = self.parse_proposal(text)
            if envelope_repairs:
                self.last_execution_info.setdefault('json_repairs', []).extend(envelope_repairs)
        if not isinstance(proposal, dict):
            raise ValueError('Builder must return a JSON object')
        files = proposal.get('files') or []
        edits = proposal.get('edits') or []
        if not isinstance(files, list) or not isinstance(edits, list):
            raise ValueError('Builder must return a bounded nonempty files list')
        if not files and not edits and 'HEAD REPAIR ORDER' in (order.context or ''):
            # In a correction round "nothing needs to change" is a legitimate answer: the Director re-verifies and the
            # Reviewer/HEAD judge the existing changes again (the repair budget bounds this).
            return BuilderResultSchema(status='COMPLETED', files_modified=[], commands_run=[result['invocation']],
                                       summary='No further changes proposed: ' + str(proposal.get('summary', ''))[:500])
        new_contents = self.resolve_changes(files, edits, root, order)
        if not new_contents or len(new_contents) > order.max_files_changed:
            raise ValueError('Builder must return a bounded nonempty files list')
        # The complete batch was validated (paths, exact edit anchors) before the first file is written.
        for rel, content in new_contents.items():
            self.pre_tool_write_file(rel, content, str(root), order)
        return BuilderResultSchema(status='COMPLETED', files_modified=list(new_contents),
                                   commands_run=[result['invocation']], summary=proposal.get('summary', 'Files implemented'))

    EDITABLE_BUDGET = 140_000
    CONTEXT_BUDGET = 60_000

    @staticmethod
    def resolve_changes(files, edits, root, order):
        """Full-file proposals and exact search/replace edits -> {path: new content}. Nothing is written here; any
        denied path, duplicate, or edit anchor that is missing or ambiguous rejects the whole batch."""
        root = Path(root)
        new_contents, seen = {}, set()

        def allowed(rel):
            ok, reason = SafetyPolicy.check_pre_tool_file_write(rel, str(root), order.allowed_paths, order.protected_paths)
            if not ok:
                raise ValueError('Proposed path denied: ' + reason)
        for item in files:
            rel, content = item.get('path'), item.get('content')
            if not isinstance(rel, str) or not isinstance(content, str) or len(content) > 300_000 or rel.casefold() in seen:
                raise ValueError('Invalid/duplicate proposed file')
            seen.add(rel.casefold())
            allowed(rel)
            new_contents[rel] = content
        for edit in edits:
            rel, old, new = edit.get('path'), edit.get('old'), edit.get('new')
            if not isinstance(rel, str) or not isinstance(old, str) or not old or not isinstance(new, str):
                raise ValueError('Invalid edit: path, non-empty old and new text are required')
            allowed(rel)
            if rel not in new_contents:
                target = root / rel
                if not target.is_file():
                    raise ValueError(f'Edit target does not exist: {rel}')
                new_contents[rel] = target.read_text(encoding='utf-8', errors='replace')
            count = new_contents[rel].count(old)
            if count != 1:
                raise ValueError(f'Edit for {rel} must match exactly once (found {count}); copy "old" verbatim and make it unique')
            new_contents[rel] = new_contents[rel].replace(old, new, 1)
        return new_contents
