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


def test_space_and_laptop_have_separate_budgets_that_fit_the_credits():
    from fixmytutorial.config import ACCOUNT_CREDITS_USD
    laptop = Settings(_env_file=None)
    space = Settings(_env_file=None, space_id="heliosbrahma/fix-my-tutorial")
    assert space.cap_lifetime_usd == space.cap_lifetime_space_usd == 15.0
    # in-flight runs lost on a Space restart are covered by the margin
    margin = space.max_concurrent_runs * space.cap_run_usd
    assert laptop.cap_lifetime_usd + space.cap_lifetime_usd + margin <= ACCOUNT_CREDITS_USD
