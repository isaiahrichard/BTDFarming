from pathlib import Path

import pytest

from bot import strategy as strategy_mod
from bot.strategy import Strategy, Tower, upgrade_cost

ROOT = Path(__file__).resolve().parent.parent
COSTS = ROOT / "strategies" / "costs_hard.yaml"


# ---------------------------------------------------------------- your TODO(me)
class TestUpgradeCost:
    def test_no_discount_is_full_price(self):
        assert upgrade_cost(270, 1, 0) == 270

    def test_fifteen_percent_rounds_up_to_five(self):
        assert upgrade_cost(270, 1, 15) == 230   # 229.5 -> 230
        assert upgrade_cost(380, 2, 15) == 325   # 323   -> 325
        assert upgrade_cost(110, 1, 15) == 95    # 93.5  -> 95

    def test_exact_multiple_of_five_is_unchanged(self):
        assert upgrade_cost(1000, 3, 10) == 900

    def test_tier_three_still_discounted(self):
        assert upgrade_cost(650, 3, 15) == 555   # 552.5 -> 555

    def test_tiers_four_and_five_never_discounted(self):
        assert upgrade_cost(2540, 4, 15) == 2540
        assert upgrade_cost(48600, 5, 25) == 48600


# ---------------------------------------------------------------- planning (independent of the TODO)
@pytest.fixture
def full_price(monkeypatch):
    """Stand-in for upgrade_cost so planning tests don't depend on the TODO."""
    monkeypatch.setattr(strategy_mod, "upgrade_cost", lambda price, tier, pct: price)


def make(steps, px_per_unit=10.0):
    import yaml
    return Strategy({"steps": steps}, yaml.safe_load(COSTS.read_text()), px_per_unit)


def run_all(s):
    actions = []
    while (a := s.next_action()) is not None:
        actions.append(a)
        s.complete(a)
    return actions


def test_placement_uses_full_base_price():
    s = make([{"place": {"id": "d1", "tower": "druid", "at_px": [100, 100]}}])
    a = s.next_action()
    assert (a.kind, a.cost, a.pos) == ("place", 430, (100, 100))


def test_hero_price_comes_from_hero_table():
    s = make([{"place": {"id": "obyn", "tower": "hero_obyn", "at_px": [1, 1]}}])
    assert s.next_action().cost == 700


def test_multi_path_upgrade_expands_one_tier_at_a_time(full_price):
    s = make([
        {"place": {"id": "a1", "tower": "alchemist", "at_px": [0, 0]}},
        {"upgrade": {"id": "a1", "to": "3-0-1"}},
    ])
    ups = [(a.path, a.tier) for a in run_all(s) if a.kind == "upgrade"]
    assert ups == [(0, 1), (0, 2), (0, 3), (2, 1)]
    assert s.towers["a1"].tiers == [3, 0, 1]


def test_targeting_counts_tab_presses_around_the_cycle():
    s = make([
        {"place": {"id": "s1", "tower": "sniper_monkey", "at_px": [0, 0]}},
        {"target": {"id": "s1", "mode": "strong"}},   # first -> strong = 3 presses
        {"target": {"id": "s1", "mode": "strong"}},   # already strong = skipped
        {"target": {"id": "s1", "mode": "first"}},    # strong -> first wraps = 1 press
    ])
    presses = [a.tab_presses for a in run_all(s) if a.kind == "target"]
    assert presses == [3, 1]


def test_village_discount_needs_range_and_bottom_path():
    s = make([], px_per_unit=10.0)                    # radius 40 units = 400 px
    village = Tower("v", "monkey_village", (0, 0), tiers=[0, 0, 0])
    near, far = Tower("n", "druid", (300, 0)), Tower("f", "druid", (450, 0))
    s.towers = {"v": village, "n": near, "f": far}
    assert s.discount_pct(near) == 0                  # no Monkey Business yet
    village.tiers = [0, 0, 2]
    assert s.discount_pct(near) == 15                 # business 10 + commerce 5
    assert s.discount_pct(far) == 0                   # 450 px > 400 px
    village.tiers = [1, 0, 2]                         # Bigger Radius: 48 units = 480 px
    assert s.discount_pct(far) == 15
    assert s.discount_pct(village) == 0               # villages don't discount themselves


def test_snapshot_restore_rewinds_progress(full_price):
    s = make([
        {"place": {"id": "d1", "tower": "druid", "at_px": [0, 0]}},
        {"upgrade": {"id": "d1", "to": "0-0-2"}},
    ])
    s.complete(s.next_action())                       # placed
    snap = s.snapshot()
    s.complete(s.next_action())                       # tier 1 bought
    assert s.towers["d1"].tiers == [0, 0, 1]
    s.restore(snap)
    assert s.towers["d1"].tiers == [0, 0, 0]
    assert s.next_action().tier == 1


def test_real_strategy_file_plans_end_to_end(full_price):
    s = Strategy.load(ROOT / "strategies" / "dark_castle_chimps.yaml", COSTS, 11.5)
    actions = run_all(s)
    assert sum(a.kind == "place" for a in actions) == 15
    assert s.towers["druid1"].tiers == [0, 1, 5]
    assert s.towers["glue1"].tiers == [0, 2, 4]


def test_skip_to_resumes_mid_game(full_price):
    s = Strategy.load(ROOT / "strategies" / "dark_castle_chimps.yaml", COSTS, 11.5)
    s.skip_to(1)
    assert "skywarden1" in s.towers
    a = s.next_action()
    assert (a.kind, a.tower_id, a.step_index) == ("place", "sub1", 1)


def test_set_tiers_resumes_a_half_done_upgrade(full_price):
    s = Strategy.load(ROOT / "strategies" / "dark_castle_chimps.yaml", COSTS, 11.5)
    s.skip_to(8)                      # step 8 = sniper1 -> 0-1-1
    s.set_tiers("sniper1", "0-1-0")   # middle tier already bought in-game
    a = s.next_action()
    assert (a.kind, a.tower_id, a.path, a.tier) == ("upgrade", "sniper1", 2, 1)   # bottom tier 1 next


def test_village_range_matches_in_game_prices():
    # Measured 2026-11-08: with the village at 0-0-2, druid2 (373 px away) showed discounted
    # prices and druid3 (402 px away) full prices. config px_per_unit must reproduce that.
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    s = make([], px_per_unit=cfg["village"]["px_per_unit"])
    village = Tower("village1", "monkey_village", (1665, 450), tiers=[0, 0, 2])
    d2, d3 = Tower("druid2", "druid", (1428, 738)), Tower("druid3", "druid", (1898, 777))
    s.towers = {t.id: t for t in (village, d2, d3)}
    assert s.discount_pct(d2) == 15
    assert s.discount_pct(d3) == 0
    village.tiers = [2, 0, 2]          # Bigger Radius: 48 units, should now reach druid3
    assert s.discount_pct(d3) == 15


def test_layout_targets_cover_every_placement():
    import yaml
    from bot.tools import RECORDED_TARGETS
    steps = yaml.safe_load((ROOT / "strategies" / "dark_castle_chimps.yaml").read_text())["steps"]
    placed = {st["place"]["id"] for st in steps if "place" in st}
    assert set(RECORDED_TARGETS) == placed


def test_wait_until_accepts_numpy_results():
    # Regression (2026-11-08): `if result:` on a returned screenshot raised ValueError mid-game.
    import numpy as np
    from bot.runner import Runner
    assert Runner.wait_until(lambda: np.zeros((4, 4, 3)), timeout=0.1) is not None
    assert Runner.wait_until(lambda: None, timeout=0.1) is None
    assert Runner.wait_until(lambda: False, timeout=0.1) is False
    assert Runner.wait_until(lambda: (0, 1, 3), timeout=0.1) == (0, 1, 3)
