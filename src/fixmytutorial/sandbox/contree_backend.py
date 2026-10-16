"""Token Factory Sandboxes (Contree) implementation of SandboxBackend."""
from __future__ import annotations

import asyncio
import os
import shlex
import time

from contree_sdk.sdk.exceptions.operation import OperationTimedOutError

from ..models import ImageRef, StepResult
from . import OUTPUT_CAP, cap_output

# The SDK uses one timeout both for the operation and for how long it waits, so start-up and polling would count
# against the step. The step's limit is enforced inside the sandbox instead (exit 124, like Docker), and the SDK gets
# this much headroom on top.
SDK_HEADROOM_S = 60


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


class ContreeBackend:
    name = "contree"

    def __init__(self, settings, client=None):
        self.s = settings
        if client is None:
            from contree_sdk import Contree
            if settings.nebius_api_key:
                os.environ.setdefault("NEBIUS_API_KEY", settings.nebius_api_key)
            if settings.nebius_project_id:
                os.environ.setdefault("NEBIUS_PROJECT_ID", settings.nebius_project_id)
            client = Contree()
        self.client = client
        self._sem = asyncio.Semaphore(settings.max_sandbox_ops)

    async def base_image(self, language: str) -> ImageRef:
        ref = self.s.contree_python_image if language == "python" else self.s.contree_node_image
        if not ref:
            raise RuntimeError("Contree base image is not set. Run: python -m fixmytutorial.sandbox.prepare")
        return ImageRef(backend="contree", ref=ref)

    async def run(self, image: ImageRef, script: str, *, files: dict[str, bytes] | None = None,
                  timeout_s: int = 180, save: bool = True, env: dict[str, str] | None = None) -> StepResult:
        t0 = time.monotonic()
        async with self._sem:
            img = await self.client.images.use(image.ref, strict=True)
            try:
                res = await img.run(shell=f"timeout --kill-after=5 {int(timeout_s)} bash -c {shlex.quote(script)}",
                                    files=files or None, env=env or None, timeout=timeout_s + SDK_HEADROOM_S,
                                    disposable=not save, truncate_output_at=OUTPUT_CAP)
            except OperationTimedOutError:  # the SDK gave up waiting (and cancelled the operation)
                return StepResult(exit_code=124, stderr="", timed_out=True,
                                  duration_s=round(time.monotonic() - t0, 2))
        duration = round(time.monotonic() - t0, 2)
        exit_code = int(res.exit_code) if res.exit_code is not None else 1
        result_obj = getattr(res, "result", None)
        timed_out = bool(getattr(result_obj, "timed_out", False)) or (
            exit_code in (124, 137, -9) and duration >= timeout_s * 0.95)
        uuid = getattr(res, "uuid", None)
        return StepResult(exit_code=exit_code, stdout=cap_output(_text(res.stdout)),
                          stderr=cap_output(_text(res.stderr)), timed_out=timed_out, duration_s=duration,
                          image=ImageRef(backend="contree", ref=str(uuid)) if save and uuid else None)
