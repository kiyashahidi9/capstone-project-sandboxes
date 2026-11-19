import pytest

import host_manager
import uploads


HOST_TEST_MODULES = {"test_host_manager"}   # these drive real Firecracker, so they need the real /var/sbx


@pytest.fixture(autouse=True)
def keep_unit_tests_off_the_real_host(request, tmp_path, monkeypatch):
    """Point unit tests at a temporary sandbox folder, so the API's startup cleanup can't destroy real sandboxes."""
    if request.module.__name__.rsplit(".", 1)[-1] in HOST_TEST_MODULES:
        return
    monkeypatch.setattr(host_manager, "SANDBOXES_ROOT", str(tmp_path / "sbx"))


@pytest.fixture(autouse=True)
def keep_tests_off_the_real_bucket(monkeypatch):
    """Turn uploads off in every test, even on a host whose .env names a real bucket; test_uploads uses a fake one."""
    monkeypatch.delenv(uploads.BUCKET_ENV, raising=False)
