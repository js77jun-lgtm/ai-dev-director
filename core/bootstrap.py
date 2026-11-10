"""Prepare local projects without staging or committing the user's working copy."""
import os
import subprocess
import tempfile
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
# What a milestone hands to the next one. A missing extension is not harmless: a web game's '.cjs' tests and new
# 'data/*.json' files were dropped between milestones and every following step failed on "the tests disappeared".
SUFFIXES = {'.py', '.pyi', '.md', '.txt', '.toml', '.yaml', '.yml', '.json', '.js', '.cjs', '.mjs', '.ts', '.tsx', '.jsx', '.vue',
            '.html', '.css', '.scss', '.sql', '.rs', '.go', '.java', '.kt', '.swift', '.c', '.h', '.cpp', '.cs', '.gradle', '.xml',
            '.ini', '.cfg', '.csv', '.bat', '.cmd', '.sh', '.ps1', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.ico',
            '.mq5', '.mqh', '.mq4', '.set'}

def git(root, *args, env=None):
    p = subprocess.run(['git', *args], cwd=str(root), env=env, input='', capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=30)
    if p.returncode: raise ValueError(p.stderr.strip())
    return p.stdout.strip()

def identity():
    env = os.environ.copy()
    env.update(GIT_AUTHOR_NAME='AI DEV Bootstrap', GIT_AUTHOR_EMAIL='bootstrap@localhost',
               GIT_COMMITTER_NAME='AI DEV Bootstrap', GIT_COMMITTER_EMAIL='bootstrap@localhost')
    return env

def prepare_project(path):
    if not str(path).strip(): raise ValueError('프로젝트 경로가 필요합니다')
    root = Path(path).resolve()
    if root == Path(root.anchor) or root == Path.home() or root == SOURCE_ROOT or SOURCE_ROOT in root.parents or root in SOURCE_ROOT.parents:
        raise ValueError('프로젝트 루트로 사용할 수 없는 광범위/제어 경로입니다')
    root.mkdir(parents=True, exist_ok=True)
    if not (root / '.git').exists(): git(root, 'init')
    try: git(root, 'rev-parse', '--verify', 'HEAD')
    except ValueError:
        tree = git(root, 'mktree')
        commit = git(root, 'commit-tree', tree, '-m', 'AI DEV empty starting point', env=identity())
        git(root, 'update-ref', 'HEAD', commit)
    return str(root)

def snapshot(root):
    root = Path(prepare_project(root)); head = git(root, 'rev-parse', 'HEAD')
    others = git(root, '-c', 'core.quotepath=false', 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
    # Newly staged files are not HEAD-tracked and are omitted by ls-files --others.
    # Include their working-copy version in the private index without changing the real index.
    others += git(root, '-c', 'core.quotepath=false', 'diff', '--cached', '--name-only', '--diff-filter=A', '-z').split('\0')
    # Intent-to-add entries (git add -N, which the Director uses so run diffs show new files) are neither untracked
    # nor cached-added; without this a milestone's new files would be lost when the next milestone builds on it.
    others += git(root, '-c', 'core.quotepath=false', 'diff', 'HEAD', '--name-only', '--diff-filter=A', '-z').split('\0')
    safe = []
    for rel in dict.fromkeys(filter(None, others)):
        p = Path(rel)
        if any(x.lower() in {'.director_worktrees', '.venv', 'venv', 'node_modules', '__pycache__', '.claude', '.agents', '.git'} for x in p.parts): continue
        if any(any(x in part.lower() for x in ('secret', 'credential', 'token', '.env', 'private_key')) for part in p.parts): continue
        target = root / p
        if not target.exists(): continue
        if target.is_symlink() or root not in target.resolve().parents: raise ValueError('Snapshot escapes project: ' + rel)
        if p.suffix.lower() in SUFFIXES and target.stat().st_size <= 2_000_000: safe.append(rel)
    with tempfile.TemporaryDirectory(prefix='director-index-') as tmp:
        env = identity(); env['GIT_INDEX_FILE'] = str(Path(tmp) / 'index')
        git(root, 'read-tree', head, env=env)
        git(root, 'add', '-u', env=env)
        for rel in safe: git(root, 'add', '--', rel, env=env)
        tree = git(root, 'write-tree', env=env)
        commit = git(root, 'commit-tree', tree, '-p', head, '-m', 'AI DEV private working-copy snapshot', env=env)
    return commit

def bootstrap_tests(worktree):
    from core.greenfield import _inventory
    root = Path(worktree).resolve(); inventory = _inventory(root)
    if inventory['test_files'] or inventory['unsafe_entries']: return None
    if any(Path(p).suffix.lower() not in {'.py', '.pyi'} and Path(p).name != 'requirements.txt'
           for p in inventory['meaningful_source_files']): return None
    rel = 'tests/test_director_bootstrap.py'; target = root / rel
    target.parent.mkdir(exist_ok=True)
    target.write_text('''"""Initial syntax smoke test; NOT product behavior acceptance."""
import ast
from pathlib import Path

def test_existing_python_source_parses():
    root = Path(__file__).resolve().parents[1]
    for path in root.rglob('*.py'):
        if any(part in {'.git', '.agents', '.venv', 'venv', '.director_worktrees', '__pycache__'} for part in path.relative_to(root).parts):
            continue
        ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
''', encoding='utf-8')
    git(root, 'add', '--', rel)
    git(root, '-c', 'user.name=AI DEV Bootstrap', '-c', 'user.email=bootstrap@localhost',
        'commit', '-m', 'Initial syntax test, product tests still required')
    return rel

def configure_runtime(config, project):
    import copy
    updated = copy.deepcopy(config)
    # Per-project verification lives in the Director's own config (never in the project, where a Builder could
    # weaken it), keyed by the project path.
    for key, override in (updated.get('projects') or {}).items():
        try:
            same = Path(key).resolve() == Path(project).resolve()
        except (OSError, ValueError):
            same = False
        if same and isinstance(override, dict):
            updated.setdefault('verification', {}).update(override.get('verification') or {})
    command = updated.get('verification', {}).get('test_command')
    if command == 'python -m pytest tests/':
        for rel in ('.venv/Scripts/python.exe', '.venv/bin/python'):
            exe = Path(project) / rel
            if exe.is_file():
                updated['verification']['test_command'] = [str(exe.resolve()), '-m', 'pytest', 'tests/']
                break
    return updated
