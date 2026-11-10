"""Bounded HEAD correction for a project-root scope, without granting whole-repo writes."""
from pathlib import Path
from core.safety import SafetyPolicy

def repair_root_scope(order, project_path, head):
    original = SafetyPolicy.normalize_order_paths(order, project_path)
    root = Path(project_path).resolve()
    def is_root(value):
        try:
            return Path(value).is_absolute() and Path(value).resolve() == root
        except (ValueError, OSError):
            return False
    if not any(is_root(p) for p in original.allowed_paths):
        return original, []
    # Only this representational error is eligible. Outside-project scopes remain blocked.
    if any(not is_root(p) and not SafetyPolicy.is_repo_relative_path(p)[0] for p in original.allowed_paths):
        return original, []
    notes=[]
    for attempt in range(2):
        proposal=head.create_order(original.goal, {
            'project_path':str(root), 'original_order':original.model_dump(),
            'instruction':'Repair allowed_paths ONLY: replace the project root with narrowly scoped repo-relative files/subtrees needed by this goal. No absolute paths, dot, whole-repo wildcard or outside paths. Keep original requirements; do not request user approval for path formatting.',
            'previous_errors':notes})
        candidate=original.model_copy(deep=True)
        candidate.allowed_paths=proposal.allowed_paths
        errors=SafetyPolicy.assess_order_for_auto_run(candidate)
        if any(p.strip() in ('*', '**', '**/*', '.', './') for p in candidate.allowed_paths):
            errors.append('Whole-project scope is not a path repair')
        if not errors:
            return candidate, ['HEAD corrected project-root scope to '+', '.join(candidate.allowed_paths)]
        notes=errors
    return original, notes
