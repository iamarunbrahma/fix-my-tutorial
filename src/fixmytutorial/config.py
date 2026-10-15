from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ACCOUNT_CREDITS_USD = 30.50  # $29.50 AI Builder Program credits + $1 trial on the Token Factory account
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

    # Exact IDs from GET /v1/models on 2026-10-15.
    model_planner: str = "nvidia/nemotron-3-super-120b-a12b"
    model_summary: str = "nvidia/nemotron-3-super-120b-a12b"
    model_triage: str = "nvidia/Nemotron-3_5-Lightning"
    model_judge: str = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
    model_repair: str = "nvidia/Nemotron-3-Ultra-550b-a55b"
    json_object_roles: str = ""  # comma list of roles that need json_object instead of json_schema

    # The laptop (evals, Rot Index batch) and the public demo each keep their own ledger and budget. The budgets
    # plus a margin for in-flight runs lost on a demo restart add up to less than ACCOUNT_CREDITS_USD, so neither
    # process needs to know what the other spent. The container image sets PUBLIC_DEMO=true wherever it runs
    # (the Space or a fallback VM): the demo budget applies and spend must persist to the Hub dataset.
    public_demo: bool = False
    cap_lifetime_usd: float = 11.0  # laptop budget; replaced by cap_lifetime_demo_usd in the public demo
    cap_lifetime_demo_usd: float = 15.0
    cap_daily_usd: float = 3.0  # public runs per day
    cap_daily_all_usd: float = 3.0  # all runs per day (local evals and batches too), so credits last to judging
    cap_run_usd: float = 0.50
    cap_tavily_run_credits: int = 30
    cap_tavily_lifetime_credits: int = 5000  # 9,000 Tavily credits available (free plan + BBDEVPOST coupon)

    rate_limit_runs_per_hour: int = 3
    max_streams_per_client: int = 8  # open live-progress pages per visitor (several tabs)
    max_streams_total: int = 10_000  # memory safety net only: idle streams are cheap, and a low cap is a lockout lever
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

    snapshot_heal_s: int = 300  # while public runs are paused by a Hub error, retry loading or saving this often

    @model_validator(mode="after")
    def _demo_budget(self):
        if self.public_demo:
            self.cap_lifetime_usd = self.cap_lifetime_demo_usd
        return self

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
