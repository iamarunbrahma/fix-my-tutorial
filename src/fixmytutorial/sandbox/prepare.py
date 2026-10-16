"""Build the Contree base images (official image + /opt/fmt helpers) and print their UUIDs for .env."""
import asyncio
import os

from ..config import get_settings
from . import ASSETS_DIR, TOOLS_SETUP


async def prepare() -> dict[str, str]:
    from contree_sdk import Contree
    s = get_settings()
    os.environ.setdefault("NEBIUS_API_KEY", s.nebius_api_key)
    os.environ.setdefault("NEBIUS_PROJECT_ID", s.nebius_project_id)
    client = Contree()
    files = {f"/opt/fmt/{p.name}": p.read_bytes() for p in ASSETS_DIR.iterdir() if p.is_file()}
    ids = {}
    for lang, ref in (("python", s.python_base_image), ("javascript", s.node_base_image)):
        base = await client.images.oci(ref)
        res = await base.run(shell=f"{TOOLS_SETUP} && mkdir -p /work && python3 --version && (node --version || true)",
                             files=files, disposable=False, timeout=600)
        if res.exit_code != 0 or res.uuid is None:
            raise RuntimeError(f"Preparing {lang} image failed: {res.stderr}")
        ids[lang] = str(res.uuid)
    return ids


if __name__ == "__main__":
    out = asyncio.run(prepare())
    print(f"CONTREE_PYTHON_IMAGE={out['python']}\nCONTREE_NODE_IMAGE={out['javascript']}")
