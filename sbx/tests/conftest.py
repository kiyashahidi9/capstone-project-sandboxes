import pytest

import node_agent


HOST_TEST_MODULES = {"test_node_agent"}   # these drive real Firecracker, so they need the real /var/sbx


@pytest.fixture(autouse=True)
def keep_unit_tests_off_the_real_host(request, tmp_path, monkeypatch):
    """Point unit tests at a temporary sandbox folder, so the API's startup cleanup can't destroy real sandboxes."""
    if request.module.__name__.rsplit(".", 1)[-1] in HOST_TEST_MODULES:
        return
    monkeypatch.setattr(node_agent, "SANDBOXES_ROOT", str(tmp_path / "sbx"))
