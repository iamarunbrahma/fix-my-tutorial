from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROLES = ("planner", "summary", "triage", "judge", "repair")

# Token Factory list prices per 1M tokens (input, output), checked 2026-10-15.
PRICES: dict[str, tuple[float, float]] = {
    "planner": (0.30, 0.90),
    "summary": (0.30, 0.90),
    "triage": (0.06, 0.24),
    "judge": (0.06, 0.24),
    "repair": (1.00, 3.00),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    nebius_api_key: str = ""
    nebius_project_id: str = ""
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    tavily_api_key: str = ""
    hf_token: str = ""
    hf_dataset_repo: str = "heliosbrahma/fixmytutorial-data"
    admin_token: str = ""
    public_base_url: str = "http://localhost:8000"

    sandbox_backend: Literal["contree", "docker", "fake"] = "docker"
    data_dir: Path = Path("data")

    model_planner: str = "nvidia/Nemotron-3-Super-120b-a12b"
    model_summary: str = "nvidia/Nemotron-3-Super-120b-a12b"
    model_triage: str = "nvidia/Nemotron-3.5-Lightning"
    model_judge: str = "nvidia/Nemotron-3-Nano-30B-A3B"
    model_repair: str = "nvidia/Nemotron-3-Ultra-550b-a55b"
    json_object_roles: str = ""  # comma list of roles that need json_object instead of json_schema

    cap_lifetime_usd: float = 35.0
    cap_daily_usd: float = 3.0
    cap_run_usd: float = 0.50
    cap_tavily_run_credits: int = 30
    cap_tavily_lifetime_credits: int = 1500

    rate_limit_runs_per_hour: int = 3
    max_concurrent_runs: int = 4
    max_sandbox_ops: int = 12
    cache_ttl_s: int = 24 * 3600

    step_timeout_s: int = 180
    install_timeout_s: int = 300
    run_wall_clock_s: int = 720
    repair_rounds: int = 3
    max_repaired_steps: int = 4
    candidates_per_round: int = 3
    max_steps: int = 40
    max_tutorial_chars: int = 60_000

    python_base_image: str = "docker.io/library/python:3.12-slim"
    node_base_image: str = "docker.io/library/node:22"
    # Dedicated network for the Docker sandbox. Before the first run the backend probes it from inside a container
    # and refuses to run tutorials if private addresses or the cloud metadata IP are reachable, unless
    # docker_allow_private_egress is set (only for our own trusted fixture tutorials on a dev laptop).
    docker_network: str = "fmt-sandbox"
    docker_allow_private_egress: bool = False
    rot_index_path: Path = Path("rot_index/results.json")
    contree_python_image: str = ""  # UUID from `python -m fixmytutorial.sandbox.prepare`
    contree_node_image: str = ""

    def model_for(self, role: str) -> str:
        return getattr(self, f"model_{role}")

    def price_for(self, role: str) -> tuple[float, float]:
        return PRICES[role]

    def uses_json_object(self, role: str) -> bool:
        return role in {r.strip() for r in self.json_object_roles.split(",") if r.strip()}

    @property
    def db_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / "fixmytutorial.sqlite3"

    @property
    def ledger_db_path(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir / "ledger.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()
