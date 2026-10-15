from fixmytutorial.config import ROLES, Settings


def test_defaults_have_model_and_price_for_every_role():
    s = Settings(_env_file=None)
    for role in ROLES:
        assert s.model_for(role)
        pin, pout = s.price_for(role)
        assert pin > 0 and pout > 0


def test_caps_match_spec():
    s = Settings(_env_file=None)
    assert (s.cap_lifetime_usd, s.cap_daily_usd, s.cap_run_usd) == (11.0, 3.0, 0.5)
    assert (s.cap_tavily_run_credits, s.cap_tavily_lifetime_credits) == (30, 5000)


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "contree")
    monkeypatch.setenv("MODEL_REPAIR", "nvidia/custom-ultra")
    s = Settings(_env_file=None)
    assert s.sandbox_backend == "contree"
    assert s.model_for("repair") == "nvidia/custom-ultra"


def test_json_object_roles_parse_from_env(monkeypatch):
    monkeypatch.setenv("JSON_OBJECT_ROLES", "triage,judge")
    s = Settings(_env_file=None)
    assert s.uses_json_object("triage") and not s.uses_json_object("planner")


def test_demo_and_laptop_have_separate_budgets_that_fit_the_credits():
    from fixmytutorial.config import ACCOUNT_CREDITS_USD
    laptop = Settings(_env_file=None)
    demo = Settings(_env_file=None, public_demo=True)
    assert demo.cap_lifetime_usd == demo.cap_lifetime_demo_usd == 15.0
    # in-flight runs lost on a demo restart are covered by the margin
    margin = demo.max_concurrent_runs * demo.cap_run_usd
    assert laptop.cap_lifetime_usd + demo.cap_lifetime_usd + margin <= ACCOUNT_CREDITS_USD


def test_the_container_image_is_a_public_demo():
    from pathlib import Path
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
    assert "PUBLIC_DEMO=true" in dockerfile


def test_one_visitor_cannot_fill_the_global_stream_cap():
    s = Settings(_env_file=None)
    # Per-visitor keys are IPv6 /64s, and a home connection often holds a /56 (256 of them). The global cap is a
    # memory safety net, so a single visitor must not be able to reach it and lock everyone else out.
    assert s.max_streams_total > 4 * 256 * s.max_streams_per_client
