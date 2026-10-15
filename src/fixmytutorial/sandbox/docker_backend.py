"""Local Docker implementation of SandboxBackend (dev, tests, and production fallback)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import time
from asyncio.subprocess import DEVNULL, PIPE
from pathlib import Path

from ..models import ImageRef, StepResult
from . import ASSETS_DIR, OUTPUT_CAP


async def run_capped(args: list[str], timeout: float | None = None, cap: int = OUTPUT_CAP) -> tuple[int, str, str]:
    """Run a command, keeping only the last `cap` bytes of each stream so a noisy tutorial cannot exhaust memory."""
    proc = await asyncio.create_subprocess_exec(*args, stdin=DEVNULL, stdout=PIPE, stderr=PIPE)

    async def drain(stream) -> str:
        buf, truncated = bytearray(), False
        while chunk := await stream.read(65536):
            buf.extend(chunk)
            if len(buf) > cap:
                del buf[: len(buf) - cap]
                truncated = True
        text = buf.decode(errors="replace")
        return "[output truncated]\n" + text if truncated else text

    try:
        out, err = await asyncio.wait_for(asyncio.gather(drain(proc.stdout), drain(proc.stderr)), timeout)
        await proc.wait()
    except BaseException:  # timeout or cancellation: never leave the docker command running
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    return proc.returncode, out, err


class SandboxPolicyError(RuntimeError):
    pass


# Runs inside a sandbox container: can it reach the gateway (the host), the cloud metadata address, or sample
# addresses in every private range? "refused" means the packet arrived, so it counts as reachable. This is a
# defense-in-depth check, not a proof: production runs third-party tutorials on Token Factory Sandboxes, and the
# Docker fallback host must also firewall these ranges (see the deploy notes).
EGRESS_PROBE = """
import json, socket, struct
def gateway():
    for line in open("/proc/net/route").read().splitlines()[1:]:
        f = line.split()
        if f[1] == "00000000":
            return socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
def host_internal():
    try:
        return socket.gethostbyname("host.docker.internal")
    except OSError:
        return None
targets = [(gateway(), 1), (host_internal(), 1), ("169.254.169.254", 80), ("10.0.0.1", 1), ("172.16.0.1", 1),
           ("192.168.0.1", 1), ("192.168.1.1", 1), ("100.64.0.1", 1)]
res = {}
for host, port in targets:
    if not host:
        continue
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect((host, port))
        res[host] = "open"
    except ConnectionRefusedError:
        res[host] = "refused"
    except OSError:
        res[host] = "blocked"
    finally:
        s.close()
print(json.dumps(res))
"""


def private_egress_open(probe_output: str) -> bool:
    try:
        res = json.loads(probe_output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return True
    return not isinstance(res, dict) or any(v in ("open", "refused") for v in res.values())


# Untrusted tutorial code: no Linux capabilities, no privilege escalation, bounded processes, memory and logs.
HARDENING = ["--memory", "4g", "--memory-swap", "4g", "--cpus", "2", "--pids-limit", "512", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--log-opt", "max-size=10m"]
# Checkpoint images carry this label. No run lasts more than a few minutes, so older ones are safe to delete.
CHECKPOINT_LABEL = "fmt.checkpoint=1"
PRUNE_AFTER = "2h"
PRUNE_EVERY_S = 600


class DockerBackend:
    name = "docker"

    def __init__(self, settings):
        self.s = settings
        self._ready: set[str] = set()
        self._build_lock = asyncio.Lock()
        self._network_checked = False
        self._ops = asyncio.Semaphore(settings.max_sandbox_ops)  # bounds containers (memory) across all runs
        self._last_prune = 0.0

    async def _docker(self, *args: str, timeout: float | None = None) -> tuple[int, str, str]:
        return await run_capped(["docker", *args], timeout)

    @staticmethod
    def _assets_hash() -> str:
        h = hashlib.sha256()
        for p in sorted(ASSETS_DIR.iterdir()):
            h.update(p.name.encode())
            h.update(p.read_bytes())
        return h.hexdigest()[:12]

    async def base_image(self, language: str) -> ImageRef:
        base = self.s.python_base_image if language == "python" else self.s.node_base_image
        tag = f"fmt-{language}:{self._assets_hash()}"
        async with self._build_lock:
            if tag not in self._ready:
                rc, _, _ = await self._docker("image", "inspect", tag)
                if rc != 0:
                    with tempfile.TemporaryDirectory() as d:
                        shutil.copytree(ASSETS_DIR, Path(d) / "assets")
                        (Path(d) / "Dockerfile").write_text(
                            f"FROM {base}\nCOPY assets/ /opt/fmt/\nRUN mkdir -p /work\nWORKDIR /work\n")
                        rc, _, err = await self._docker("build", "-t", tag, d, timeout=1200)
                        if rc != 0:
                            raise RuntimeError(f"docker build failed: {err[-2000:]}")
                self._ready.add(tag)
            await self._check_network(tag)
        if time.monotonic() - self._last_prune > PRUNE_EVERY_S:
            await self.prune_checkpoints()
        return ImageRef(backend="docker", ref=tag)

    async def prune_checkpoints(self) -> None:
        """Delete checkpoint images left by finished runs, so the disk never fills up."""
        self._last_prune = time.monotonic()
        await self._docker("image", "prune", "-a", "-f", "--filter", f"label={CHECKPOINT_LABEL}",
                           "--filter", f"until={PRUNE_AFTER}", timeout=300)

    async def _check_network(self, image: str) -> None:
        if self._network_checked:
            return
        net = self.s.docker_network
        rc, _, _ = await self._docker("network", "inspect", net)
        if rc != 0:
            rc, _, err = await self._docker("network", "create", "--driver", "bridge", "--opt",
                                            "com.docker.network.bridge.enable_icc=false", net)
            if rc != 0:
                raise SandboxPolicyError(f"Could not create the sandbox network {net}: {err.strip()}")
        rc, out, _ = await self._docker("run", "--rm", *HARDENING, "--network", net, image, "python3", "-c",
                                        EGRESS_PROBE, timeout=90)
        if private_egress_open(out) and not self.s.docker_allow_private_egress:
            raise SandboxPolicyError(
                f"The Docker sandbox network '{net}' can reach private addresses ({out.strip()}). Run tutorials on "
                "Token Factory Sandboxes, or firewall this network (see the deploy notes). Set "
                "DOCKER_ALLOW_PRIVATE_EGRESS=true only for trusted local fixtures.")
        self._network_checked = True

    async def run(self, image: ImageRef, script: str, *, files: dict[str, bytes] | None = None,
                  timeout_s: int = 180, save: bool = True, env: dict[str, str] | None = None) -> StepResult:
        async with self._ops:
            return await self._run(image, script, files=files, timeout_s=timeout_s, save=save, env=env)

    async def _run(self, image: ImageRef, script: str, *, files: dict[str, bytes] | None, timeout_s: int,
                   save: bool, env: dict[str, str] | None) -> StepResult:
        args = ["create", *HARDENING, "--network", self.s.docker_network]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        rc, out, err = await self._docker(*args, image.ref, "bash", "-c", script)
        if rc != 0:
            raise RuntimeError(f"docker create failed: {err.strip()}")
        cid = out.strip()
        try:
            if files:
                with tempfile.TemporaryDirectory() as d:
                    for path, data in files.items():
                        p = Path(d) / path.lstrip("/")
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(data)
                    rc, _, err = await self._docker("cp", f"{d}/.", f"{cid}:/")
                    if rc != 0:
                        raise RuntimeError(f"docker cp failed: {err.strip()}")
            t0 = time.monotonic()
            timed_out = False
            try:
                _, sout, serr = await self._docker("start", "-a", cid, timeout=timeout_s)
            except TimeoutError:
                timed_out = True
                await self._docker("kill", cid)
                _, sout, serr = await self._docker("logs", cid)
            duration = round(time.monotonic() - t0, 2)
            _, code, _ = await self._docker("inspect", "-f", "{{.State.ExitCode}}", cid)
            exit_code = 124 if timed_out else int(code.strip() or 1)
            new_image = None
            if save and exit_code == 0:  # a failed step's filesystem is never resumed from
                rc, img, _ = await self._docker("commit", "--change", f"LABEL {CHECKPOINT_LABEL}", cid)
                if rc == 0:
                    new_image = ImageRef(backend="docker", ref=img.strip())
            return StepResult(exit_code=exit_code, stdout=sout, stderr=serr,
                              timed_out=timed_out, duration_s=duration, image=new_image)
        finally:
            await self._docker("rm", "-f", cid)
