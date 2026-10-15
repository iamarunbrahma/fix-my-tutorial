"""Create or update the Hugging Face Space and its secrets, then upload the app.

Usage: uv run python scripts/deploy_space.py
"""
import shutil
import tempfile
from pathlib import Path

from dotenv import dotenv_values
from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parents[1]
SPACE = "heliosbrahma/fix-my-tutorial"
URL = "https://heliosbrahma-fix-my-tutorial.hf.space"
FRONT_MATTER = """---
title: Fix My Tutorial
emoji: 🔧
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Run any coding tutorial, find what broke, fix it with sources
---

"""
SECRETS = ["NEBIUS_API_KEY", "NEBIUS_PROJECT_ID", "TAVILY_API_KEY", "ADMIN_TOKEN",
           "CONTREE_PYTHON_IMAGE", "CONTREE_NODE_IMAGE"]


def main() -> None:
    env = dotenv_values(ROOT / ".env")
    api = HfApi(token=env["HF_TOKEN"])
    api.create_repo(SPACE, repo_type="space", space_sdk="docker", exist_ok=True, private=False)
    for key in SECRETS:
        if env.get(key):
            api.add_space_secret(SPACE, key, env[key])
    # The Space only gets a fine-grained token that can write to the private dataset, never the deploy token.
    api.add_space_secret(SPACE, "HF_TOKEN", env["SPACE_HF_TOKEN"])
    api.add_space_variable(SPACE, "SANDBOX_BACKEND", env.get("PROD_SANDBOX_BACKEND", "contree"))
    api.add_space_variable(SPACE, "PUBLIC_BASE_URL", URL)
    with tempfile.TemporaryDirectory() as d:
        for item in ["Dockerfile", "pyproject.toml", "uv.lock", "LICENSE", "src", "rot_index", ".dockerignore"]:
            src = ROOT / item
            (shutil.copytree if src.is_dir() else shutil.copy)(src, Path(d) / item)
        (Path(d) / "README.md").write_text(FRONT_MATTER + (ROOT / "README.md").read_text())
        api.upload_folder(folder_path=d, repo_id=SPACE, repo_type="space", commit_message="Deploy Fix My Tutorial")
    print("deployed:", URL)


if __name__ == "__main__":
    main()
