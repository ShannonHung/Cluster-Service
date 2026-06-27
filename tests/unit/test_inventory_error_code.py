from app.core.exceptions import ErrorCode


def test_inventory_not_found_code_exists():
    assert ErrorCode.INVENTORY_NOT_FOUND == "INVENTORY_NOT_FOUND"
