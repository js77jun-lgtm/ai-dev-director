"""Opt-in real CLI smoke for newly enabled role adapters; no production project writes."""
import concurrent.futures
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agents.google_roles import GoogleHead, GoogleReviewer
from agents.proposal_builder import ProposalBuilder
from schemas.order import OrderSchema

def check(kind):
    with tempfile.TemporaryDirectory(prefix='director-role-smoke-') as tmp:
        order=OrderSchema(goal='Create src/add.py with def add(a,b): return a+b. No other files.',
                          allowed_paths=['src/**'],max_files_changed=1,max_diff_lines=20)
        if kind=='google-head':
            agent=GoogleHead(timeout=180)
            result=agent.create_order(order.goal,{'project_path':tmp,'existing_files':[]})
            assert result.allowed_paths
        elif kind=='google-reviewer':
            agent=GoogleReviewer(timeout=180)
            result=agent.review(order,'+def add(a,b): return a+b',{'passed':True,'summary':'smoke evidence only'})
            assert result.verdict in ('pass','changes_required')
        else:
            agent=ProposalBuilder(provider=kind,timeout=180)
            result=agent.run(order,tmp)
            assert result.status=='COMPLETED'
            scope={}
            exec(compile((Path(tmp)/'src/add.py').read_text(),'src/add.py','exec'),scope)
            assert scope['add'](2,3)==5
        return {'role':kind,'provider':agent.provider,'exit_code':agent.last_execution_info.get('exit_code'),'ok':True}

if __name__=='__main__':
    # Keep calls bounded; no same-provider concurrent invocations.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(check,['google-head','openai','claude']):
            print(json.dumps(result),flush=True)
    print(json.dumps(check('google-reviewer')),flush=True)
