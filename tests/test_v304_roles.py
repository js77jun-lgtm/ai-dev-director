import itertools
import json
import sys
import pytest
from core import role_settings as settings
from core.orchestrator import Orchestrator
from core.research_runner import TriadResearchRunner
from agents.proposal_builder import ProposalBuilder
from schemas.order import OrderSchema

@pytest.mark.parametrize('providers', list(itertools.permutations(settings.DOMAINS)))
def test_all_six_role_assignments(tmp_path, monkeypatch, providers):
    monkeypatch.setattr(settings, 'PATH', tmp_path/'roles.json')
    roles = dict(zip(('head','builder','reviewer'), providers))
    settings.save(roles)
    cfg = settings.apply({})
    assert settings.current(cfg) == roles
    orch = Orchestrator(cfg)
    assert [orch._get_agent_domain(a) for a in (orch.head,orch.builder,orch.reviewer)] == [settings.DOMAINS[p] for p in providers]
    assigned = orch.pm.resolve_roles(*providers, strict=True)
    assert assigned['can_proceed']
    assert [assigned[r+'_provider'] for r in roles] == list(providers)
    for role,provider in roles.items():
        with orch.pm._get_conn() as conn:
            row=conn.execute('SELECT current_role FROM provider_health WHERE provider=?',(settings.DOMAINS[provider],)).fetchone()
            assert row[0] == role.upper()

@pytest.mark.parametrize('roles', [{}, {'head':'bad','builder':'claude','reviewer':'openai'},
    {'head':'openai','builder':'openai','reviewer':'claude'}])
def test_invalid_roles_do_not_replace_saved_file(tmp_path,monkeypatch,roles):
    monkeypatch.setattr(settings,'PATH',tmp_path/'roles.json')
    valid={'head':'claude','builder':'antigravity','reviewer':'openai'}
    settings.save(valid)
    with pytest.raises(ValueError): settings.save(roles)
    assert json.loads(settings.PATH.read_text())==valid

@pytest.mark.parametrize('provider',list(settings.DOMAINS))
def test_builder_routes_to_selected_cli(tmp_path,monkeypatch,provider):
    calls=[]
    def invoke(self,domain,prompt,cwd,stage):
        calls.append(domain)
        return {'output':json.dumps({'files':[{'path':'src/a.py','content':'x=1\n'}]}),
                'invocation':'test','exit_code':0,'duration_sec':0}
    monkeypatch.setattr(TriadResearchRunner,'invoke',invoke)
    builder=ProposalBuilder(provider=provider,executable=sys.executable)
    builder.run(OrderSchema(goal='test',allowed_paths=['src/**']),str(tmp_path))
    assert calls==[settings.DOMAINS[provider]]
    assert (tmp_path/'src/a.py').read_text()=='x=1\n'
    assert builder.get_provenance().provider == settings.DOMAINS[provider].lower()

def test_existing_orchestrator_keeps_role_snapshot(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'PATH',tmp_path/'roles.json')
    settings.save({'head':'claude','builder':'antigravity','reviewer':'openai'})
    old=Orchestrator(settings.apply({}))
    settings.save({'head':'antigravity','builder':'openai','reviewer':'claude'})
    new=Orchestrator(settings.apply({}))
    assert old.head.provider=='anthropic'
    assert new.head.provider=='google'

def test_claude_builder_result_envelope(tmp_path,monkeypatch):
    inner=json.dumps({'files':[{'path':'src/a.py','content':'x=1\n'}]})
    monkeypatch.setattr(TriadResearchRunner,'invoke',lambda *a:{
        'output':json.dumps({'type':'result','is_error':False,'result':'```json\n'+inner+'\n```'}),
        'invocation':'test','exit_code':0,'duration_sec':0})
    builder=ProposalBuilder(provider='claude',executable=sys.executable)
    assert builder.run(OrderSchema(goal='test',allowed_paths=['src/**']),str(tmp_path)).status=='COMPLETED'
