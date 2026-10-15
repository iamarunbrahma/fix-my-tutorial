from types import SimpleNamespace

import pytest

from fixmytutorial.models import ImageRef
from fixmytutorial.sandbox.contree_backend import ContreeBackend
from tests.sandbox_contract import ALL_CHECKS


class FakeImage:
    def __init__(self, log, uuid):
        self.log, self.uuid = log, uuid

    async def run(self, **kw):
        self.log.append({"image": self.uuid, **kw})
        return SimpleNamespace(exit_code=0, stdout="out", stderr="", uuid=None if kw["disposable"] else "new-uuid")


class FakeContree:
    def __init__(self):
        self.log = []
        self.images = SimpleNamespace(use=self._use)

    async def _use(self, uuid, strict=True):
        return FakeImage(self.log, uuid)


async def test_run_maps_arguments_and_result(settings):
    fake = FakeContree()
    b = ContreeBackend(settings.model_copy(update={"contree_python_image": "py-uuid"}), client=fake)
    base = await b.base_image("python")
    r = await b.run(base, "echo 'hi there'", files={"/work/a": b"x"}, timeout_s=9, save=True)
    call = fake.log[0]
    assert call["image"] == "py-uuid" and call["disposable"] is False and call["timeout"] == 9
    assert call["shell"] == "bash -c 'echo '\"'\"'hi there'\"'\"''"
    assert call["files"] == {"/work/a": b"x"}
    assert r.image == ImageRef(backend="contree", ref="new-uuid") and r.stdout == "out"


async def test_disposable_run_returns_no_image(settings):
    b = ContreeBackend(settings.model_copy(update={"contree_python_image": "py"}), client=FakeContree())
    r = await b.run(await b.base_image("python"), "true", save=False)
    assert r.image is None


async def test_missing_base_image_is_a_clear_error(settings):
    b = ContreeBackend(settings, client=FakeContree())
    with pytest.raises(RuntimeError, match="prepare"):
        await b.base_image("javascript")


@pytest.mark.live
@pytest.mark.parametrize("check", ALL_CHECKS, ids=lambda c: c.__name__)
async def test_contree_contract(check):
    from fixmytutorial.config import Settings
    s = Settings(sandbox_backend="contree")
    assert s.contree_python_image and s.contree_node_image and s.nebius_project_id
    await check(ContreeBackend(s))
