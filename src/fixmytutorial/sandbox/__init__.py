from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import ImageRef, StepResult

ASSETS_DIR = Path(__file__).parent / "assets"
PRELUDE = "source /opt/fmt/prelude.sh"
# Tools tutorials reach for to download data or code. The slim Python image lacks them; Node's image has them.
TOOLS_SETUP = ("(command -v curl && command -v wget && command -v git) >/dev/null 2>&1 || (apt-get update -qq && "
               "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends curl wget git "
               "ca-certificates >/dev/null && rm -rf /var/lib/apt/lists/*)")
OUTPUT_CAP = 64 * 1024


def cap_output(text: str) -> str:
    if len(text) <= OUTPUT_CAP:
        return text
    return "[output truncated]\n" + text[-OUTPUT_CAP:]


class SandboxBackend(Protocol):
    name: str

    async def base_image(self, language: str) -> ImageRef: ...

    async def run(self, image: ImageRef, script: str, *, files: dict[str, bytes] | None = None,
                  timeout_s: int = 180, save: bool = True, env: dict[str, str] | None = None) -> StepResult: ...


def make_backend(settings) -> SandboxBackend:
    if settings.sandbox_backend == "docker":
        from .docker_backend import DockerBackend
        return DockerBackend(settings)
    if settings.sandbox_backend == "contree":
        from .contree_backend import ContreeBackend
        return ContreeBackend(settings)
    raise ValueError(f"No production backend for {settings.sandbox_backend!r}")
