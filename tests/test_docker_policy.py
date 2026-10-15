import json

import pytest

from fixmytutorial.sandbox.docker_backend import DockerBackend, SandboxPolicyError, private_egress_open


def test_private_egress_verdict():
    assert private_egress_open(json.dumps({"172.17.0.1": "refused", "169.254.169.254": "blocked"}))
    assert private_egress_open(json.dumps({"169.254.169.254": "open"}))
    assert not private_egress_open(json.dumps({"172.30.0.1": "blocked", "169.254.169.254": "blocked"}))
    assert private_egress_open("not json")  # unreadable probe counts as open (fail closed)


def backend_with(settings, probe_output):
    b = DockerBackend(settings)
    calls = []

    async def fake_docker(*args, timeout=None):
        calls.append(args)
        if args[:2] == ("image", "inspect") or args[:2] == ("network", "inspect"):
            return 0, "", ""
        if args[0] == "run":
            return 0, probe_output, ""
        return 0, "", ""

    b._docker = fake_docker
    return b, calls


async def test_open_private_egress_refused_by_default(settings):
    b, _ = backend_with(settings, json.dumps({"172.17.0.1": "refused"}))
    with pytest.raises(SandboxPolicyError, match="private"):
        await b.base_image("python")


async def test_open_private_egress_allowed_with_explicit_opt_in(settings):
    s = settings.model_copy(update={"docker_allow_private_egress": True})
    b, _ = backend_with(s, json.dumps({"172.17.0.1": "refused"}))
    assert (await b.base_image("python")).ref.startswith("fmt-python:")


async def test_firewalled_network_passes_and_probe_runs_on_that_network(settings):
    b, calls = backend_with(settings, json.dumps({"172.30.0.1": "blocked", "169.254.169.254": "blocked"}))
    await b.base_image("python")
    probe = next(c for c in calls if c[0] == "run")
    assert "--network" in probe and probe[probe.index("--network") + 1] == settings.docker_network


def test_default_network_is_not_the_shared_bridge(settings):
    assert settings.docker_network == "fmt-sandbox" and settings.docker_allow_private_egress is False
