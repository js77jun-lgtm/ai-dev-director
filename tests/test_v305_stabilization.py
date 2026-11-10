import json
import sys
import zipfile
import pytest
from agents.proposal_builder import ProposalBuilder
from core.builder_context import referenced_documents
from core.research_runner import ResearchProviderCallError, TriadResearchRunner
from schemas.order import OrderSchema


def test_protected_baseline_is_readable_but_not_writable(tmp_path, monkeypatch):
    (tmp_path / 'src').mkdir()
    baseline = tmp_path / 'src/base.py'
    baseline.write_text('BASELINE = 42')
    def invoke(self, domain, prompt, cwd, stage):
        assert 'BASELINE = 42' in prompt
        return dict(output=json.dumps({'files': [{'path': 'src/v2.py', 'content': 'NEW = 1'}]}),
                    invocation='mock', exit_code=0, duration_sec=0)
    monkeypatch.setattr(TriadResearchRunner, 'invoke', invoke)
    order = OrderSchema(goal='fork', allowed_paths=['src/v2.py'], protected_paths=['src/base.py'])
    ProposalBuilder(executable=sys.executable).run(order, str(tmp_path))
    assert baseline.read_text() == 'BASELINE = 42'


def test_failure_replaces_stale_success_evidence(tmp_path, monkeypatch):
    def invoke(*args):
        err = ResearchProviderCallError('GOOGLE_STREAM_EMPTY_RESPONSE', meta={'exit_code': 0})
        err.attempts = [{'error': 'GOOGLE_STREAM_EMPTY_RESPONSE', 'duration_sec': 2}] * 2
        raise err
    monkeypatch.setattr(TriadResearchRunner, 'invoke', invoke)
    builder = ProposalBuilder(executable=sys.executable)
    builder.last_execution_info = {'output': 'old success'}
    with pytest.raises(ResearchProviderCallError):
        builder.run(OrderSchema(goal='test'), str(tmp_path))
    assert builder.last_execution_info['output'] == ''
    assert len(builder.last_execution_info['attempts']) == 2
    assert builder.last_execution_info['execution_duration_sec'] == 4


def test_docx_reference_extracted_and_missing_reference_fails(tmp_path):
    doc = tmp_path / 'research.docx'
    with zipfile.ZipFile(doc, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Evidence 42</w:t></w:r></w:p></w:body></w:document>')
    assert 'Evidence 42' in referenced_documents(OrderSchema(goal='read', reference_paths=[str(doc)]))[str(doc)]
    with pytest.raises(ValueError, match='REFERENCE_INPUT_UNAVAILABLE'):
        referenced_documents(OrderSchema(goal='read', reference_paths=[str(tmp_path / 'missing.docx')]))


def test_output_limit_retries_compact_complete_proposal(tmp_path, monkeypatch):
    prompts = []
    def once(self, domain, prompt, cwd, stage):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise ResearchProviderCallError('GOOGLE_STREAM_FAILED: exceeded the output token limit')
        return {'output': '{}'}
    monkeypatch.setattr(TriadResearchRunner, '_invoke_once', once)
    result = TriadResearchRunner().invoke('GOOGLE', 'original', str(tmp_path), 'BUILDING')
    assert len(prompts) == 2 and 'complete, compact JSON' in prompts[1]
    assert len(result['attempts']) == 1


def test_retry_preserves_failure_history_and_rejects_scope_failure(tmp_path):
    from core.state_machine import StateMachine, State
    sm = StateMachine(str(tmp_path / 'state.db'))
    sm.create_run('transport', str(tmp_path), 'test', str(tmp_path / 'worktree'))
    sm.transition_to('transport', State.FAIL, 'Builder failed: GOOGLE_STREAM_EMPTY_RESPONSE')
    assert sm.reopen_transport_failure('transport')
    assert sm.get_run('transport')['current_state'] == State.BUILDING
    assert not sm.reopen_transport_failure('transport')
    sm.create_run('scope', str(tmp_path), 'test')
    sm.transition_to('scope', State.HUMAN_REQUIRED, 'Scope violation: protected file')
    assert not sm.reopen_transport_failure('scope')
