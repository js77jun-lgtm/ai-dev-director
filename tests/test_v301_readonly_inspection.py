import hashlib
from pathlib import Path
from core.git_manager import GitManager


def test_inspection_does_not_stage_runtime_scaffolding(tmp_path):
    GitManager._run_git(['init'], tmp_path)
    GitManager._run_git(['-c', 'user.name=test', '-c', 'user.email=test@localhost',
                         'commit', '--allow-empty', '-m', 'baseline'], tmp_path)
    (tmp_path / 'GEMINI.md').write_text('permission scaffold', encoding='utf-8')
    (tmp_path / '.agents').mkdir()
    (tmp_path / '.agents/hooks.json').write_text('{}', encoding='utf-8')
    (tmp_path / 'feature.py').write_text('x = 1\n', encoding='utf-8')
    before = GitManager._run_git(['ls-files', '--stage'], tmp_path)
    diff, stats, untracked = GitManager.inspect_changes(str(tmp_path))
    assert not diff and not stats
    assert 'GEMINI.md' in untracked and 'feature.py' in untracked
    assert GitManager._run_git(['ls-files', '--stage'], tmp_path) == before
    (tmp_path / 'GEMINI.md').unlink()
    (tmp_path / '.agents/hooks.json').unlink()
    assert not GitManager.get_deleted_files(str(tmp_path))


def test_real_tracked_control_deletion_still_visible(tmp_path):
    GitManager._run_git(['init'], tmp_path)
    (tmp_path / 'GEMINI.md').write_text('existing', encoding='utf-8')
    GitManager._run_git(['add', 'GEMINI.md'], tmp_path)
    GitManager._run_git(['-c', 'user.name=test', '-c', 'user.email=test@localhost', 'commit', '-m', 'baseline'], tmp_path)
    (tmp_path / 'GEMINI.md').unlink()
    _, stats, _ = GitManager.inspect_changes(str(tmp_path))
    assert stats[0][2] == 'GEMINI.md'
    assert 'GEMINI.md' in GitManager.get_deleted_files(str(tmp_path))
