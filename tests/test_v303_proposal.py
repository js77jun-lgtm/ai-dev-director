import json
import sys
from pathlib import Path
import pytest
from agents.proposal_builder import ProposalBuilder
from core.research_runner import TriadResearchRunner
from schemas.order import OrderSchema

def result(files):
    return dict(output=json.dumps({'files': files}), invocation='test', exit_code=0, duration_sec=0)

def test_invalid_whitespace_escape_is_recovered_without_changing_structure():
    raw='{"files":[{"path":"tests/x.py","content":"a=1\\n\\    b=2\\n"}],"summary":"ok"}'
    parsed,repairs=ProposalBuilder.parse_proposal(raw)
    assert parsed['files'][0]['content']=='a=1\n    b=2\n'
    assert len(repairs)==1

def test_valid_backslashes_are_untouched():
    raw=json.dumps({'files':[{'path':'x.py','content':r'pattern="\\d+"'}]})
    parsed,repairs=ProposalBuilder.parse_proposal(raw)
    assert parsed['files'][0]['content']==r'pattern="\\d+"' and repairs==[]

def test_structurally_broken_json_is_not_guessed():
    with pytest.raises(json.JSONDecodeError):
        ProposalBuilder.parse_proposal('{"files": [}')

def test_invalid_regex_escape_after_escaped_slash_is_recovered():
    raw='{"files":[{"path":"x.py","content":"assert re.search(r\\"x\\\\\\s+\\", text)"}]}'
    parsed,repairs=ProposalBuilder.parse_proposal(raw)
    assert parsed['files'][0]['content']=='assert re.search(r"x\\s+", text)'
    assert len(repairs)==1

def test_glob_protection_does_not_block_normal_implementation(tmp_path, monkeypatch):
    monkeypatch.setattr(TriadResearchRunner, 'invoke', lambda *a: result([{'path':'src/tool.py','content':'x = 1\n'}]))
    order=OrderSchema(goal='test',allowed_paths=['src/**'],protected_paths=['**/credentials*','**/secrets*'])
    outcome=ProposalBuilder(executable=sys.executable).run(order,str(tmp_path))
    assert outcome.status=='COMPLETED'
    assert (tmp_path/'src/tool.py').read_text()=='x = 1\n'

@pytest.mark.parametrize('path',['src/credentials.json','src/nested/secrets.txt','../escape.py'])
def test_whole_batch_checked_before_any_write(tmp_path, monkeypatch, path):
    monkeypatch.setattr(TriadResearchRunner, 'invoke', lambda *a: result([
        {'path':'src/tool.py','content':'x=1'}, {'path':path,'content':'bad'}]))
    order=OrderSchema(goal='test',allowed_paths=['src/**'],protected_paths=['**/credentials*','**/secrets*'])
    with pytest.raises(ValueError): ProposalBuilder(executable=sys.executable).run(order,str(tmp_path))
    assert not (tmp_path/'src/tool.py').exists()

@pytest.mark.parametrize('head_stop,always_wrong,review_only,expected,builds',[
    (False,False,False,'PASS',2),(True,True,False,'HUMAN_REQUIRED',1),
    (False,True,False,'FAIL',3),(False,False,True,'PASS',2)])
def test_failed_tests_return_to_head_and_builder(tmp_path, monkeypatch,head_stop,always_wrong,review_only,expected,builds):
    from tests.test_v0234_resume_parity import _mock_orch
    from schemas.builder_result import BuilderResultSchema
    from schemas.review_result import ReviewResultSchema
    from schemas.decision import DecisionSchema
    orch=_mock_orch(bootstrap={'enabled':True}, autonomy={'enabled':True,'max_repairs':2},
                    verification={'test_command':'python -m pytest tests/'})
    calls=[]
    def build(order, worktree_path, builder_fn=None):
        root=Path(worktree_path)
        calls.append(order.context)
        (root/'feature.py').write_text('def double(x): return x * '+('3' if always_wrong or (len(calls)==1 and not review_only) else '2')+'\n')
        (root/'tests/test_feature.py').write_text('from feature import double\ndef test_double(): assert double(4)==8\n')
        return BuilderResultSchema(status='COMPLETED',files_modified=['feature.py','tests/test_feature.py'],summary='built')
    monkeypatch.setattr(orch.builder,'run',build)
    monkeypatch.setattr(orch,'_claim_trial_for',lambda *a:None)
    monkeypatch.setattr(orch,'_record_provider_success',lambda *a:None)
    monkeypatch.setattr(orch,'run_reviewer_with_failover',lambda rid,o,v,*a:(ReviewResultSchema(
        verdict='pass' if v['passed'] and not (review_only and len(calls)==1) else 'changes_required',
        p1=[] if v['passed'] else ['double is wrong'],
        p2=['Improve edge cases'] if review_only and len(calls)==1 else [],summary='review'),None))
    monkeypatch.setattr(orch,'run_decision_with_failover',lambda rid,o,b,v,*a:(DecisionSchema(
        decision='HUMAN_REQUIRED' if head_stop else ('PASS' if v['passed'] else 'FAIL'),rationale='check',next_steps=['Multiply by 2; preserve tests']),None))
    order=OrderSchema(goal='double',allowed_paths=['feature.py','tests/**'],context='original')
    outcome=orch.run(str(tmp_path/'project'),order.goal,order=order)
    assert outcome['final_state']==expected
    assert len(calls)==builds
    if builds>1: assert 'Multiply by 2' in calls[1]
    assert len(orch._load_json(outcome['run_id'],'repair_history.json') or [])==builds-1
