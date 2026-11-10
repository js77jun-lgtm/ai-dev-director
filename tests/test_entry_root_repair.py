from unittest.mock import Mock
import pytest
from core.entry_repair import repair_root_scope
from schemas.order import OrderSchema

def test_root_replanned_without_changing_requirements(tmp_path):
    order=OrderSchema(goal='original',allowed_paths=[str(tmp_path)],protected_paths=['**/secrets*'],acceptance_criteria=['keep this'])
    head=Mock()
    head.create_order.return_value=OrderSchema(goal='different',allowed_paths=['src/**','tests/**'],acceptance_criteria=['wrong'])
    fixed,notes=repair_root_scope(order,str(tmp_path),head)
    assert fixed.allowed_paths==['src/**','tests/**'] and notes
    assert fixed.goal=='original' and fixed.acceptance_criteria==['keep this']
    assert fixed.protected_paths==order.protected_paths

@pytest.mark.parametrize('bad',['**','../escape',''])
def test_bad_head_repair_is_bounded(tmp_path,bad):
    order=OrderSchema(goal='original',allowed_paths=[str(tmp_path)])
    head=Mock()
    head.create_order.return_value=OrderSchema(goal='test',allowed_paths=[bad])
    fixed,notes=repair_root_scope(order,str(tmp_path),head)
    assert fixed.allowed_paths==order.allowed_paths and notes
    assert head.create_order.call_count==2

def test_outside_path_never_repaired_to_inside(tmp_path):
    order=OrderSchema(goal='test',allowed_paths=[str(tmp_path.parent/'outside')])
    head=Mock()
    fixed,_=repair_root_scope(order,str(tmp_path),head)
    head.create_order.assert_not_called()
    assert fixed.allowed_paths==order.allowed_paths
