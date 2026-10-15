"""Local Docker implementation of SandboxBackend (dev, tests, and production fallback)."""
from __future__ import annotations

import asyncio
import hashlib
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
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out, err


# Untrusted tutorial code: no Linux capabilities, no privilege escalation, bounded processes, memory and logs.
HARDENING = ["--memory", "4g", "--memory-swap", "4g", "--cpus", "2", "--pids-limit", "512", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--log-opt", "max-size=10m"]


class DockerBackend:
    name = "docker"

    def __init__(self, settings):
        self.s = settings
        self._ready: set[str] = set()
        self._build_lock = asyncio.Lock()

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
        return ImageRef(backend="docker", ref=tag)

    async def run(self, image: ImageRef, script: str, *, files: dict[str, bytes] | None = None,
                  timeout_s: int = 180, save: bool = True, env: dict[str, str] | None = None) -> StepResult:
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
            if save:
                rc, img, _ = await self._docker("commit", cid)
                if rc == 0:
                    new_image = ImageRef(backend="docker", ref=img.strip())
            return StepResult(exit_code=exit_code, stdout=sout, stderr=serr,
                              timed_out=timed_out, duration_s=duration, image=new_image)
        finally:
            await self._docker("rm", "-f", cid)
