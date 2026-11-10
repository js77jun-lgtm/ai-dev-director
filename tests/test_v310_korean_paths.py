"""
v3.1.0: a project with Korean file names stopped a mission with "Path escapes worktree" - git printed the names as
quoted octal escapes and the Scope Guard read the backslashes as path traversal. git output is now verbatim UTF-8.
"""
import subprocess

from core.git_manager import GitManager
from core.safety import SafetyPolicy
from schemas.order import OrderSchema


def repo(tmp_path):
    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "기존 문서.md").write_text("old", encoding="utf-8")
    (tmp_path / "docs" / "삭제될.md").write_text("bye", encoding="utf-8")
    git("add", "-A"); git("commit", "-qm", "base")
    return tmp_path


def test_korean_and_spaced_file_names_come_back_verbatim(tmp_path):
    wt = repo(tmp_path)
    (wt / "docs" / "리서치_요약.md").write_text("new", encoding="utf-8")
    (wt / "docs" / "기존 문서.md").write_text("changed", encoding="utf-8")
    (wt / "docs" / "새 파일 이름.md").write_text("x", encoding="utf-8")
    (wt / "docs" / "삭제될.md").unlink()
    assert sorted(GitManager.get_untracked_files(str(wt))) == ["docs/리서치_요약.md", "docs/새 파일 이름.md"]
    assert GitManager.get_deleted_files(str(wt)) == ["docs/삭제될.md"]
    names = {n[2] for n in GitManager.get_numstat(str(wt))}
    assert {"docs/기존 문서.md", "docs/삭제될.md"} <= names and not any("\\" in n or '"' in n for n in names)


def test_korean_file_inside_allowed_paths_is_not_a_scope_violation(tmp_path):
    wt = repo(tmp_path)
    (wt / "docs" / "리서치_요약.md").write_text("new", encoding="utf-8")
    order = OrderSchema(goal="g", allowed_paths=["docs/**"])
    for rel in GitManager.get_untracked_files(str(wt)):
        ok, reason = SafetyPolicy.check_pre_tool_file_write(rel, str(wt), order.allowed_paths, order.protected_paths)
        assert ok, reason


def test_a_sentence_that_starts_with_a_folder_is_not_read_as_a_document_path():
    from core.builder_context import referenced_documents
    order = OrderSchema(goal="g", allowed_paths=["docs/**"], context=(
        "E:/work/dog_walker = 개 산책 게임 '오늘도, 산책'. 수치는 data/*.json 에 있고 계획은 docs/v3/plan.md 에 쓴다."))
    assert referenced_documents(order) == {}


def test_a_project_root_sentence_before_a_file_name_is_not_one_document_path():
    from core.builder_context import referenced_documents
    order = OrderSchema(goal="g", allowed_paths=["src/**"], context=(
        "Project root: C:\\Users\\My\\Desktop\\AI_DEV\\director_smoke. The existing files are README.md "
        "and tests/test_baseline.py."))
    assert referenced_documents(order) == {}


def test_snapshot_carries_cjs_tests_data_files_images_and_korean_names(tmp_path):
    from core.bootstrap import snapshot
    wt = repo(tmp_path)
    for rel, body in (("tests/missions.test.cjs", "ok"), ("data/missions.json", "{}"), ("docs/계획서.md", "plan"),
                      ("assets/dog.png", "png"), ("data/secret_token.json", "no")):
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(body, encoding="utf-8")
    commit = snapshot(str(wt))
    listed = subprocess.run(["git", "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", commit], cwd=wt,
                            capture_output=True, text=True, encoding="utf-8").stdout.split()
    assert {"tests/missions.test.cjs", "data/missions.json", "docs/계획서.md", "assets/dog.png"} <= set(listed)
    assert "data/secret_token.json" not in listed
