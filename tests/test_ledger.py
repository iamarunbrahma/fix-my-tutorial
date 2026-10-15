import asyncio

import pytest

from fixmytutorial.ledger import BudgetExceeded, Ledger


def make(settings, tmp_path, now=1_792_000_000.0, **caps):
    s = settings.model_copy(update=caps)
    clock = {"t": now}
    led = Ledger(tmp_path / "l.db", s, clock=lambda: clock["t"])
    return led, clock


def test_price_math(settings, tmp_path):
    led, _ = make(settings, tmp_path)
    # Ultra: 1M in = $1, 1M out = $3
    assert led.price_usd("repair", 1_000_000, 1_000_000) == pytest.approx(4.0)


def test_commit_records_actual_usage(settings, tmp_path):
    led, _ = make(settings, tmp_path)
    r = led.reserve_llm("run1", True, "planner", 10_000, 2_000)
    usd = led.commit_llm(r, "planner", "m", 10_000, 1_000)
    assert usd == pytest.approx(10_000 / 1e6 * 0.30 + 1_000 / 1e6 * 0.90)
    t = led.totals("run1")
    assert t.run_usd == pytest.approx(usd) and t.lifetime_usd == pytest.approx(usd)


def test_run_cap_blocks_reservation(settings, tmp_path):
    led, _ = make(settings, tmp_path, cap_run_usd=0.01)
    with pytest.raises(BudgetExceeded) as e:
        led.reserve_llm("run1", True, "repair", 10_000, 4_000)  # ~$0.022
    assert e.value.scope == "run"


def test_daily_cap_applies_to_public_only(settings, tmp_path):
    led, _ = make(settings, tmp_path, cap_daily_usd=0.001, cap_run_usd=10)
    with pytest.raises(BudgetExceeded) as e:
        led.reserve_llm("pub", True, "planner", 10_000, 2_000)
    assert e.value.scope == "daily"
    led.reserve_llm("batch", False, "planner", 10_000, 2_000)  # does not raise


def test_lifetime_cap_counts_pending_reservations(settings, tmp_path):
    led, _ = make(settings, tmp_path, cap_lifetime_usd=0.05, cap_run_usd=10, cap_daily_usd=10)
    led.reserve_llm("a", False, "repair", 10_000, 4_000)  # ~0.022 pending
    led.reserve_llm("b", False, "repair", 10_000, 4_000)  # ~0.044 pending
    with pytest.raises(BudgetExceeded):
        led.reserve_llm("c", False, "repair", 10_000, 4_000)


def test_release_frees_reservation(settings, tmp_path):
    led, _ = make(settings, tmp_path, cap_run_usd=0.03)
    r = led.reserve_llm("run1", True, "repair", 10_000, 4_000)
    led.release(r)
    led.reserve_llm("run1", True, "repair", 10_000, 4_000)


def test_daily_rollover(settings, tmp_path):
    led, clock = make(settings, tmp_path, cap_daily_usd=0.01, cap_run_usd=10)
    r = led.reserve_llm("x", True, "planner", 20_000, 2_000)
    led.commit_llm(r, "planner", "m", 20_000, 2_000)  # ~0.0078
    with pytest.raises(BudgetExceeded):
        led.reserve_llm("y", True, "planner", 20_000, 2_000)
    clock["t"] += 86_400
    led.reserve_llm("y", True, "planner", 20_000, 2_000)


def test_tavily_caps(settings, tmp_path):
    led, _ = make(settings, tmp_path, cap_tavily_run_credits=3)
    r = led.reserve_tavily("run1", True, 2)
    led.commit_tavily(r, 2)
    with pytest.raises(BudgetExceeded) as e:
        led.reserve_tavily("run1", True, 2)
    assert e.value.scope == "tavily_run"


def test_public_runs_open_flips_when_daily_spent(settings, tmp_path):
    led, _ = make(settings, tmp_path, cap_daily_usd=0.005, cap_run_usd=10)
    assert led.public_runs_open()[0]
    r = led.reserve_llm("x", True, "planner", 10_000, 2_000)
    led.commit_llm(r, "planner", "m", 10_000, 5_000)
    ok, why = led.public_runs_open()
    assert not ok and "today" in why


def test_export_import_round_trip(settings, tmp_path):
    led, _ = make(settings, tmp_path)
    r = led.reserve_llm("x", True, "planner", 1000, 100)
    led.commit_llm(r, "planner", "m", 1000, 100)
    rows = led.export_rows()
    other = Ledger(tmp_path / "other.db", settings)
    assert other.import_rows(rows) == 1
    assert other.import_rows(rows) == 0
    assert other.totals().lifetime_usd == pytest.approx(led.totals().lifetime_usd)


async def test_concurrent_reservations_never_exceed_cap(settings, tmp_path):
    led, _ = make(settings, tmp_path, cap_lifetime_usd=0.1, cap_run_usd=10, cap_daily_usd=10)
    ok = 0

    async def one(i):
        nonlocal ok
        try:
            led.reserve_llm(f"r{i}", False, "repair", 10_000, 4_000)
            ok += 1
        except BudgetExceeded:
            pass

    await asyncio.gather(*(one(i) for i in range(20)))
    assert ok == 4  # 4 * 0.022 = 0.088 <= 0.1; a 5th would exceed


def test_charge_reserved_records_full_estimate(settings, tmp_path):
    led, _ = make(settings, tmp_path)
    r = led.reserve_llm("run1", True, "repair", 10_000, 4_000)
    usd = led.charge_reserved(r, "repair", "m")
    assert usd == pytest.approx(r.amount) and led.totals("run1").run_usd == pytest.approx(r.amount)


def test_commit_never_records_less_than_zero(settings, tmp_path):
    led, _ = make(settings, tmp_path)
    r = led.reserve_llm("run1", True, "planner", 100, 100)
    assert led.commit_llm(r, "planner", "m", 0, 0) == 0
