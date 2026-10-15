import pytest

from fixmytutorial.executor import ExecState, Executor
from fixmytutorial.judge import Judge
from fixmytutorial.models import RunPlan, Step
from fixmytutorial.sandbox.docker_backend import DockerBackend
from tests.sandbox_contract import ALL_CHECKS


@pytest.mark.docker
@pytest.mark.parametrize("check", ALL_CHECKS, ids=lambda c: c.__name__)
async def test_docker_contract(check, settings):
    await check(DockerBackend(settings.model_copy(update={"sandbox_backend": "docker"})))


@pytest.mark.docker
async def test_executor_on_docker_runs_real_cells(settings):
    s = settings.model_copy(update={"sandbox_backend": "docker"})
    backend = DockerBackend(s)
    plan = RunPlan(language="python", runtime="3.12", steps=[
        Step(id="s1", title="a", kind="run_code", content="x = 20"),
        Step(id="s2", title="b", kind="shell", content="echo between"),
        Step(id="s3", title="c", kind="run_code", content="print(x + 22)"),
    ])
    base = await backend.base_image("python")
    t = await Executor(backend, s, Judge(None)).execute(plan, [ExecState(base)], run_id="d", public=False)
    assert t.failed_unit is None and "42" in t.results["s3"].stdout
