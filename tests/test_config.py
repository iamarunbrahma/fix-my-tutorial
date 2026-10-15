from fixmytutorial.config import ROLES, Settings


def test_defaults_have_model_and_price_for_every_role():
    s = Settings(_env_file=None)
    for role in ROLES:
        assert s.model_for(role)
        pin, pout = s.price_for(role)
        assert pin > 0 and pout > 0


def test_caps_match_spec():
    s = Settings(_env_file=None)
    assert (s.cap_lifetime_usd, s.cap_daily_usd, s.cap_run_usd) == (28.0, 3.0, 0.5)
    assert (s.cap_tavily_run_credits, s.cap_tavily_lifetime_credits) == (30, 1500)


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
