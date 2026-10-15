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


def test_probe_covers_all_private_ranges():
    from fixmytutorial.sandbox.docker_backend import EGRESS_PROBE
    for target in ["169.254.169.254", "10.0.0.1", "172.16.0.1", "192.168.0.1", "192.168.1.1", "100.64.0.1",
                   "host.docker.internal"]:
        assert target in EGRESS_PROBE


async def test_created_network_blocks_container_to_container_traffic(settings):
    b = DockerBackend(settings)
    calls = []

    async def fake_docker(*args, timeout=None):
        calls.append(args)
        if args[:2] == ("network", "inspect"):
            return 1, "", "not found"
        if args[0] == "run":
            return 0, json.dumps({"10.0.0.1": "blocked"}), ""
        return 0, "", ""

    b._docker = fake_docker
    await b.base_image("python")
    create = next(c for c in calls if c[:2] == ("network", "create"))
    assert "com.docker.network.bridge.enable_icc=false" in create


def run_backend(settings, exit_code="0", start_delay=0.0, **overrides):
    import asyncio
    b = DockerBackend(settings.model_copy(update=overrides))
    calls, live = [], {"now": 0, "max": 0}

    async def fake_docker(*args, timeout=None):
        calls.append(args)
        if args[0] == "create":
            return 0, "cid123\n", ""
        if args[0] == "start":
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
            await asyncio.sleep(start_delay)
            live["now"] -= 1
            return 0, "out", ""
        if args[0] == "inspect":
            return 0, exit_code, ""
        if args[0] == "commit":
            return 0, "sha256:new\n", ""
        return 0, "", ""

    b._docker = fake_docker
    return b, calls, live


async def test_failed_steps_are_not_saved_as_checkpoints(settings):
    from fixmytutorial.models import ImageRef
    b, calls, _ = run_backend(settings, exit_code="1")
    r = await b.run(ImageRef(backend="docker", ref="base"), "false", save=True)
    assert r.image is None and not any(c[0] == "commit" for c in calls)


async def test_checkpoints_are_labelled_so_old_ones_can_be_pruned(settings):
    from fixmytutorial.models import ImageRef
    b, calls, _ = run_backend(settings)
    r = await b.run(ImageRef(backend="docker", ref="base"), "true", save=True)
    commit = next(c for c in calls if c[0] == "commit")
    assert "LABEL fmt.checkpoint=1" in commit and r.image.ref == "sha256:new"
    await b.prune_checkpoints()
    prune = next(c for c in calls if c[:2] == ("image", "prune"))
    assert "label=fmt.checkpoint=1" in prune and any(a.startswith("until=") for a in prune)


async def test_concurrent_sandbox_runs_are_bounded(settings):
    import asyncio
    from fixmytutorial.models import ImageRef
    b, _, live = run_backend(settings, start_delay=0.05, max_sandbox_ops=2)
    await asyncio.gather(*(b.run(ImageRef(backend="docker", ref="base"), "true", save=False) for _ in range(6)))
    assert live["max"] == 2


async def test_a_cancelled_docker_command_is_killed():
    import asyncio
    import time
    from fixmytutorial.sandbox.docker_backend import run_capped
    import random
    secs = f"{30 + random.random():.6f}"  # unique, so a stray process from elsewhere can't confuse the check
    task = asyncio.create_task(run_capped(["sleep", secs]))
    await asyncio.sleep(0.3)
    t0 = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - t0 < 5
    out = (await asyncio.create_subprocess_exec("pgrep", "-f", f"^sleep {secs}$", stdout=asyncio.subprocess.PIPE))
    stdout, _ = await out.communicate()
    assert stdout.strip() == b""
