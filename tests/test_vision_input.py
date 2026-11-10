"""Vision tests run on saved screenshots, so no game or mouse is needed."""
from pathlib import Path

import cv2
import pytest
import yaml

from bot.input import Input
from bot.vision import Vision

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
CAPTURES = ROOT / "captures"


@pytest.fixture(scope="module")
def vision():
    return Vision(ROOT / "templates", CFG["vision"], park=lambda: None)


def shot(name):
    path = CAPTURES / name
    if not path.exists():
        pytest.skip(f"{path} not present")
    return cv2.imread(str(path))


def test_reads_cash_and_round(vision):
    img = shot("ingame_r6_650.png")
    assert vision.read_cash(img) == 650
    assert vision.read_round(img) == 6


def test_finds_play_button_in_game_but_not_on_defeat(vision):
    assert vision.find("ingame_play", shot("ingame_r6_650.png")) is not None
    assert vision.find("defeat_text", shot("ingame_r6_650.png")) is None


def test_relative_move_matches_calibration():
    inp = Input(CFG["display"], dry_run=True)
    # Measured 2026-11-07: from the home corner, (-1000, +500) logical landed exactly there.
    px, py = (2328 - 1000) * 1.65, 500 * 1.65
    assert inp.to_relative(px, py) == (-1000, 500)


def test_reads_cash_when_upgrade_panel_shifts_hud(vision):
    # Upgrade panel open on the left pushes the cash ~800 px right (user screenshot, upscaled).
    assert vision.read_cash(shot("panel_open_270.png")) == 270


def test_short_cash_ignores_cobweb_after_number(vision):
    # "$25" leaves room for the gravestone's cobweb to sit right after the digits.
    assert vision.read_cash(shot("stuck_panel_25.png")) == 25


def test_sniper_camo_toggle_states_are_distinguished(vision):
    off, on = shot("sniper_panel_r16.png"), shot("sniper_panel_camo_on.png")
    assert vision.find("sniper_camo_toggle_off", off) and not vision.find("sniper_camo_toggle_on", off)
    assert vision.find("sniper_camo_toggle_on", on) and not vision.find("sniper_camo_toggle_off", on)


def test_reads_tower_tiers_from_upgrade_panel(vision):
    # Green tier boxes, panel on either side; None when no panel is open.
    assert vision.read_panel_tiers(shot("panel_left_druid7.png")) == (0, 2, 1)
    assert vision.read_panel_tiers(shot("panel_right_druid1.png")) == (0, 1, 4)
    assert vision.read_panel_tiers(shot("sniper_panel_r16.png")) == (0, 1, 0)
    assert vision.read_panel_tiers(shot("no_panel_r48.png")) is None
    assert vision.read_panel_tiers(shot("ingame_r6_650.png")) is None


def test_recognises_end_of_game_screens(vision):
    popup, card = shot("insta_popup_recording.png"), shot("menu_victory.png")
    assert vision.find("insta_monkey_banner", popup) is not None
    assert vision.find("victory_text", popup) is None          # popup first: not a win yet
    assert vision.find("victory_text", card) and vision.find("victory_next", card)
    assert vision.find("insta_monkey_banner", card) is None
    assert vision.find("victory_home", shot("menu_after_next.png")) is not None


def test_go_home_picks_the_right_click_on_each_post_game_screen(vision):
    from bot.runner import Runner

    def decide(img):
        if vision.find("home_play", img):
            return "home"
        for name, _ in Runner.HOME_ROUTE:
            if vision.has_template(name) and vision.find(name, img):
                return name
        return None

    expected = {"victory": "victory_next", "after_next": "victory_home", "rewards1": "event_collect",
                "rewards2": "event_mystery_insta", "rewards3": "event_mystery_insta",
                "rewards4": "insta_monkey_banner", "rewards5": "event_continue",
                "rewards6": "event_back", "rewards7": "home"}
    for cap, want in expected.items():
        assert decide(shot(f"menu_{cap}.png")) == want, cap


def test_level_up_popup_is_recognised_and_dismissed_on_the_way_home(vision):
    from bot.runner import Runner
    assert vision.find("popup_level_up", shot("menu_levelup.png")) is not None
    assert vision.find("popup_level_up", shot("ingame_r6_650.png")) is None
    assert ("popup_level_up", "centre") in Runner.HOME_ROUTE
    assert vision.find("popup_knowledge_point", shot("menu_knowledge.png")) is not None
    assert vision.find("popup_knowledge_point", shot("menu_levelup.png")) is None
    assert ("popup_knowledge_point", "centre") in Runner.HOME_ROUTE


def test_fast_forward_state_is_read_from_the_button(vision):
    assert vision.fast_forward_state(shot("ff_on_live.png")) == "on"
    assert vision.fast_forward_state(shot("ff_off_live.png")) == "off"
    assert vision.fast_forward_state(shot("ingame_r6_650.png")) is None   # between rounds: play button


@pytest.mark.parametrize("name", ["spent_12320.png", "spent_12320_panel_left.png", "spent_12320_panel_right.png"])
def test_reads_cash_spent_in_every_hud_layout(vision, name):
    # With the right-hand panel open the "$" alone reads as a 5 (512320); it must be dropped.
    assert vision.read_cash_spent(shot(name)) == 12320


def test_cash_spent_is_none_without_the_setting(vision):
    assert vision.read_cash_spent(shot("ingame_r6_650.png")) is None
