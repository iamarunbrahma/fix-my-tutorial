import pytest

from fixmytutorial.sandbox.docker_backend import DockerBackend
from tests.sandbox_contract import ALL_CHECKS


@pytest.mark.docker
@pytest.mark.parametrize("check", ALL_CHECKS, ids=lambda c: c.__name__)
async def test_docker_contract(check, settings):
    await check(DockerBackend(settings.model_copy(update={"sandbox_backend": "docker"})))
