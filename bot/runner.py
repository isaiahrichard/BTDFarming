"""Brain: look at the screen, decide the next move, act, check it worked.

States:   MENU --new_game--> IN_GAME --+--> VICTORY --go_home (Next, Home, event screens)--> MENU (next game)
                                       +--> DEFEAT --retry (max N)--> IN_GAME
                                                   +--retries used up--> stop the bot
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import cv2
import numpy as np

from .input import Input
from .strategy import Action, Strategy
from .vision import Vision

log = logging.getLogger(__name__)



class BotStopped(Exception):
    """Raised when the bot must stop and hand control back to the human."""


class GameEnded(Exception):
    """Raised mid-action when a Victory/Defeat screen appears; the main loop handles it."""


class Runner:
    def __init__(self, cfg: dict, inp: Input, vision: Vision, make_strategy, logs_dir: Path,
                 stop_file: Path):
        self.cfg = cfg
        self.inp = inp
        self.vision = vision
        self.make_strategy = make_strategy  # fresh Strategy per game
        self.logs_dir = logs_dir
        self.stop_file = stop_file
        self.from_step = 0                  # resume point for the first game (--from-step)
        self.tier_overrides: dict[str, str] = {}   # tower id -> crosspath (--set-tiers)
        self.selected: str | None = None    # tower whose upgrade panel we believe is open
        self.wins = 0                       # games won this run (for the summary line)
        self.losses = 0                     # games lost after all retries (--forever only)
        self.max_losses_in_a_row = cfg["loss_policy"].get("max_losses_in_a_row", 3)
        self.started_at: float | None = None
        self.fail_count: dict[Action, int] = {}   # consecutive failures per action
        self.min_cash: dict[Action, int] = {}     # don't retry an action until cash exceeds this
        self.clearly_red_min = 45.0         # above this a probe is red beyond doubt: don't even click
        self.click_unclear = True           # try a click on unclear probes (real games; the layout tool turns it off)
        self.placed_by_click = False
        self.valid_redness_max = -12.0      # placement_redness below this = valid (sandbox: valid <= -32, red >= +8)
        self.round: int | None = None       # latest round read off the HUD (for logs)
        self.poll = cfg["vision"]["poll_interval_s"]
        self.keys = cfg["hotkeys"]

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def wait_until(check, timeout: float, interval: float = 0.05):
        """Call check() until it returns something truthy (returned) or timeout (None).

        With ~50 ms screenshots it's cheaper to look again than to sleep a fixed, worst-case
        amount; most game reactions take 1-3 frames.
        """
        deadline = time.monotonic() + timeout
        while True:
            result = check()
            # Not `if result:` -- check() may return a numpy image, whose truth value is
            # ambiguous. None/False mean "not yet"; anything else is the answer.
            if (result is not None and result is not False) or time.monotonic() >= deadline:
                return result
            time.sleep(interval)

    def check_stop(self) -> None:
        if self.stop_file.exists():
            raise BotStopped(f"{self.stop_file} exists — stopping as requested")

    def save_debug(self, img: np.ndarray, label: str) -> Path:
        self.logs_dir.mkdir(exist_ok=True)
        path = self.logs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{label}.png"
        cv2.imwrite(str(path), img)
        return path

    def click_template(self, name: str, timeout: float) -> None:
        x, y = self.vision.wait_for(name, timeout=timeout)
        self.inp.click(x, y)

    def run_flow(self, flow_name: str) -> None:
        """Click through a template sequence from config.yaml `menu_flow`."""
        for step in self.cfg["menu_flow"][flow_name]:
            name, optional = step["template"], step.get("optional", False)
            self.check_stop()
            if not self.vision.has_template(name):
                if optional:
                    continue
                raise BotStopped(f"menu flow '{flow_name}' needs template '{name}', which hasn't been "
                                 f"captured yet (see `python -m bot.tools --help`)")
            if "page_with" in step:
                # The template may be on a later page (Dark Castle is on page 2 of the Expert
                # maps): click the page arrow until it shows up.
                for page in range(step.get("max_pages", 10)):
                    hit = self.vision.find(name)
                    if hit:
                        break
                    arrow = self.vision.find(step["page_with"])
                    if arrow is None:
                        raise BotStopped(f"menu flow '{flow_name}': '{name}' not found and no page arrow")
                    self.inp.click(*arrow)
                    time.sleep(1.0)
                else:
                    raise BotStopped(f"menu flow '{flow_name}': '{name}' not found after paging")
                self.inp.click(*hit)
                log.info("menu: clicked %s (page %d)", name, page + 1)
                time.sleep(1.0)
                continue
            try:
                self.click_template(name, timeout=step.get("timeout", 4 if optional else 30))
                log.info("menu: clicked %s", name)
            except TimeoutError:
                if not optional:
                    raise
            time.sleep(1.0)  # menu transition animations

    # Screens that can appear between a win and the home screen, and what to click on each.
    # Order matters: popups first (they cover everything), then buttons. Captured live 2026-11-08.
    # The Collection Event is seasonal and the Insta reveal only shows for some rewards, so this
    # reacts to whatever is on screen instead of following a fixed sequence.
    HOME_ROUTE = [
        ("popup_level_up", "centre"),         # player LEVEL UP! popup: click anywhere
        ("popup_knowledge_point", "centre"),  # "1 Monkey Knowledge Point!" after a level-up
        ("insta_monkey_banner", "centre"),    # full-screen INSTA MONKEY! reveal: click anywhere
        ("victory_next", "it"),               # Victory stats card
        ("victory_home", "it"),               # rewards card: Home / Review Map / Freeplay
        ("event_collect", "it"),              # Collection Event chest ready
        ("event_continue", "it"),             # all mystery instas revealed
        ("event_mystery_insta", "it"),        # an unrevealed mystery insta (one per click)
        ("event_back", "it"),                 # Collection Event screen: back arrow -> home
        ("defeat_home", "it"),                # Defeat screen after a lost game (--forever keeps going)
    ]

    def go_home(self, timeout: float = 90) -> None:
        """Click through post-game screens until the home screen's Play button shows."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_stop()
            img = self.vision.screenshot()
            if self.vision.find("home_play", img):
                log.info("home screen")
                return
            for name, where in self.HOME_ROUTE:
                if not self.vision.has_template(name):
                    continue
                hit = self.vision.find(name, img)
                if hit:
                    log.info("going home: %s", name)
                    self.inp.click(*((1920, 1300) if where == "centre" else hit))
                    time.sleep(2.5)   # reveal / transition animations swallow clicks made too soon
                    break
            else:
                time.sleep(1.0)       # nothing recognised yet (loading, animation): look again
        img = self.vision.screenshot()
        raise BotStopped(f"couldn't get back to the home screen; screenshot: {self.save_debug(img, 'not_home')}")

    # ---------------------------------------------------------------- top level
    def run(self, games: int | None, start_in: str) -> None:
        """Play `games` games, or keep going until stopped when games is None (--forever)."""
        # Keypresses go to the focused window, which is the terminal that launched us.
        # Click a spot where nothing can be placed (castle stone) to focus the game first.
        # In game the castle-area safe spot is fine; on the home screen that spot sits next to
        # Play Social, so focus by clicking empty sand in front of the statue instead.
        self.inp.click(*(self.cfg["game"]["home_focus_px"] if start_in == "menu" else self.cfg["game"]["safe_click_px"]))
        time.sleep(0.5)
        self.started_at = time.monotonic()
        game = 0
        losses_in_a_row = 0
        while games is None or game < games:
            game += 1
            log.info("=== game %d%s ===", game, "" if games is None else f"/{games}")
            if start_in == "menu" or game > 1:
                self.go_home()
                self.run_flow("new_game")
            won = self.play_game()
            if not won:
                self.losses += 1
                losses_in_a_row += 1
                # One loss in --forever is bad luck (round 63 is close); several in a row means
                # something is broken, so stop instead of failing all night.
                if games is not None or losses_in_a_row >= self.max_losses_in_a_row:
                    raise BotStopped(f"game lost after using all retries ({losses_in_a_row} in a row)")
                log.warning("game lost; starting a new one (%d loss(es) in a row)", losses_in_a_row)
                log.info("SUMMARY: %s", self.summary())
                continue   # next loop: go_home() clicks Home on the Defeat screen, then a new game
            losses_in_a_row = 0
            self.wins += 1
            log.info("SUMMARY: %s", self.summary())
            if games is None or game < games:
                self.go_home()      # Victory -> rewards -> any event screens -> home
        log.info("finished %d game(s)", game)

    def summary(self) -> str:
        """One line for the log: wins, time, pace, monkey money (Dark Castle CHIMPS = $240/win)."""
        if self.started_at is None:
            return "no games played"
        mins = (time.monotonic() - self.started_at) / 60
        pace = f", {mins / self.wins:.1f} min/win" if self.wins else ""
        lost = f", {self.losses} loss(es)" if self.losses else ""
        return f"{self.wins} win(s){lost} in {int(mins // 60)}h{int(mins % 60):02d}m{pace}, ~${240 * self.wins} monkey money"

    # ---------------------------------------------------------------- one game
    def play_game(self) -> bool:
        strat: Strategy = self.make_strategy()
        if self.from_step:
            strat.skip_to(self.from_step)
            log.info("resuming at step %d", self.from_step)
            self.from_step = 0              # later games start from the beginning
        for tower_id, crosspath in self.tier_overrides.items():
            strat.set_tiers(tower_id, crosspath)
            log.info("set %s to %s", tower_id, crosspath)
        self.tier_overrides = {}
        retries_left = self.cfg["loss_policy"]["max_retries_per_game"]
        checkpoints: dict[int, dict] = {}   # round -> strategy state at the start of that round
        last_round = None
        unreadable = 0                      # consecutive frames where cash could not be read

        while True:
            self.check_stop()
            img = self.vision.screenshot()

            # --- end-of-game screens first: they cover the HUD
            # A CHIMPS win first shows a full-screen "Insta Monkey!" reward popup that stays up
            # until clicked (seen in the user's recording), then the Victory card with Next.
            if self.vision.find("popup_level_up", img) or self.vision.find("popup_knowledge_point", img):
                # Player level-ups pop up mid-game (seen at round 60) and pause everything; the
                # "1 Monkey Knowledge Point!" screen follows some of them.
                log.info("Level Up / Knowledge popup: clicking to dismiss")
                self.inp.click(1920, 1300)
                time.sleep(1.5)
                continue
            if self.vision.find("insta_monkey_banner", img):
                log.info("Insta Monkey reward popup: clicking to dismiss")
                self.inp.click(1920, 1300)
                time.sleep(1.5)
                continue
            if self.vision.find("victory_text", img) or self.vision.find("victory_next", img):
                log.info("VICTORY (last round seen: %s)", self.round)
                return True
            if self.vision.find("defeat_text", img):
                self.save_debug(img, f"defeat_round{last_round}")
                if retries_left <= 0:
                    log.error("defeat on round %s and no retries left", last_round)
                    return False
                self.selected = None
                if not self.retry_last_round(img):
                    return False
                retries_left -= 1
                if last_round in checkpoints:
                    strat.restore(checkpoints[last_round])
                    log.warning("retrying round %s (%d retries left); strategy rewound to step %d",
                                last_round, retries_left, strat.step_index)
                continue   # the retried round waits on its play button, so we re-buy first

            # --- in game
            rnd = self.vision.read_round(img)
            if rnd is not None and rnd != last_round:
                if rnd not in checkpoints:
                    checkpoints[rnd] = strat.snapshot()
                log.info("round %d", rnd)
                last_round = rnd
                self.round = rnd

            action = strat.next_action()
            cash = self.vision.read_cash(img)
            unreadable = 0 if cash is not None else unreadable + 1
            if unreadable and unreadable % 20 == 0:
                # ~30 s with no HUD, no Victory/Defeat, no known popup: something unknown is
                # covering the game (a level-up stalled us 13 min before this existed). Keep a
                # picture for later, then try the usual dismissal.
                path = self.save_debug(img, f"unknown_screen_round{last_round}")
                log.warning("HUD unreadable for %d frames; clicking to dismiss an unknown popup (%s)",
                            unreadable, path)
                self.inp.click(1920, 1300)
                time.sleep(1.5)
                continue
            if (last_round or 0) >= 100 and unreadable and unreadable % 4 == 0:
                # Past the last round with no HUD and no Victory/Defeat recognised: some other
                # popup is covering the screen. Click the middle to dismiss it.
                log.warning("round 100 done but the screen is unrecognised; clicking to dismiss a popup")
                self.inp.click(1920, 1300)
                time.sleep(1.5)
                continue
            if action is not None:
                if cash is None:
                    log.debug("couldn't read cash this frame (%d in a row)", unreadable)
                elif cash >= action.cost and cash > self.min_cash.get(action, -1):
                    try:
                        self.execute(strat, action, cash)
                    except GameEnded as e:
                        log.warning("game ended (%s) during %s", e, action.describe())
                        self.inp.park()   # drop anything still held
                    continue  # act again straight away, or handle the end screen next loop
            # Between rounds the play button shows (always at game start; every round when
            # auto-start is off). Like a human: collect the end-of-round bonus, buy everything
            # affordable (handled above), and only then press play.
            play_btn = self.vision.find("ingame_play", img)
            # After round 100 the play button is just the game ending: pressing it would
            # start freeplay. Wait for the reward popup / Victory card instead.
            if play_btn and (last_round or 0) < 100 and (cash is not None or unreadable >= 3):
                if cash is None:
                    log.warning("cash unreadable for %d frames; starting the round anyway", unreadable)
                self.start_round(play_btn)
                continue
            time.sleep(self.poll)

    def start_round(self, play_btn: tuple[int, int]) -> None:
        """Press play, then make sure fast-forward is ON by looking at the button.

        Once a round runs, the same button toggles fast-forward. Whether the game keeps the
        setting (between rounds, after a retry) varies, so read the button (glowing = on)
        rather than count clicks.
        """
        self.inp.click(*play_btn)
        if not self.cfg["game"]["fast_forward"]:
            log.info("round started")
            return
        for attempt in range(3):
            state = self.wait_until(lambda: self.vision.fast_forward_state(self.vision.screenshot()), timeout=1.0)
            if state == "on":
                break
            if state == "off":
                self.inp.click(*play_btn)
                log.info("fast-forward was off: turned it on")
        else:
            log.warning("couldn't confirm fast-forward is on")
        log.info("round started")

    def retry_last_round(self, img: np.ndarray) -> bool:
        btn = self.vision.find("defeat_retry_last_round", img)
        if btn is None:
            log.error("no 'Retry last round' button (lost on the very first round?)")
            return False
        self.inp.click(*btn)
        # The game asked "are you sure?" on the first retry but not the second, so the
        # confirm dialog is optional: click it if it shows up within a few seconds.
        if self.vision.has_template("retry_confirm_ok"):
            try:
                self.click_template("retry_confirm_ok", timeout=4)
            except TimeoutError:
                log.info("no retry confirmation dialog this time")
        time.sleep(2.0)
        return True

    # ---------------------------------------------------------------- actions
    def execute(self, strat: Strategy, action: Action, cash_before: int) -> None:
        log.info("round %s, step %d: %s (cash $%d)", self.round, action.step_index, action.describe(), cash_before)
        if action.kind == "place":
            self.place(strat, action, cash_before)
        elif action.kind == "upgrade":
            self.upgrade(strat, action, cash_before)
        else:
            self.set_target(strat, action)

    def pick_up(self, kind: str, target: tuple[float, float]):
        """Pick up a tower with the pointer at `target`. Returns the background screenshot
        (taken before pickup, for the redness comparison), or None if nothing got held."""
        key = self.keys["towers"]["hero" if kind.startswith("hero_") else kind]
        # Order matters: be at the spot *before* picking the tower up. Any move_to() while
        # holding a tower re-homes over the shop panel, which cancels the placement.
        self.selected = None              # picking a tower up closes any upgrade panel
        self.inp.move_to(*target)
        before_img = self.vision.screenshot(park=False)  # background, nothing held yet
        self.inp.key(key)
        if kind.startswith("hero_"):
            # Heroes are different: the hotkey only scrolls the shop to the hero tile and
            # highlights it. Clicking the tile picks the hero up; then carry it to the spot.
            tx, ty = self.cfg["game"]["hero_tile_px"]
            time.sleep(0.15)              # let the shop scroll to the hero tile
            self.inp.move_to(tx, ty)      # homing over the shop is fine: nothing held yet
            self.inp.click_here()
            time.sleep(0.1)
            self.inp.move_by(target[0] - tx, target[1] - ty)
        # The red "X" cancel button only shows while something is held. Without this check a
        # pickup that silently failed would look "valid" (no red circle) and we'd click an
        # empty map.
        if not self.wait_until(self.is_holding, timeout=0.8):
            log.warning("pressed %r but nothing is held", key)
            self.inp.park()
            return None
        return before_img

    def is_holding(self) -> bool:
        return self.vision.find("holding_cancel_x", self.vision.screenshot(park=False)) is not None

    def drop_here(self) -> bool:
        """Click to place the held tower. True if it left the cursor (the red X vanished)."""
        self.inp.click_here()
        if not self.wait_until(lambda: not self.is_holding(), timeout=0.6):
            self.inp.park()   # still held = the spot was refused; drop it over the shop panel
            return False
        return True

    def place(self, strat: Strategy, action: Action, cash_before: int) -> None:
        ox, oy = self.cfg["game"]["placement_pointer_offset_px"]
        target = (action.pos[0] + ox, action.pos[1] + oy)   # pointer goes below where the tower lands
        for attempt in range(3):
            before_img = self.pick_up(action.tower_kind, target)
            if before_img is None:
                continue
            self.placed_by_click = False
            pointer = self.find_valid_pointer(target, (ox, oy), before_img)
            if pointer is None:
                self.inp.park()   # over the shop panel = drop the held tower (Esc would pause)
                break
            landed = (round(pointer[0] - ox), round(pointer[1] - oy))
            if not self.placed_by_click and not self.drop_here():
                log.warning("click at %s didn't place %s (still held)", pointer, action.tower_id)
                continue
            # The red X vanishing already proves the game took it (cash checks were noisy).
            if landed != action.pos:
                log.info("%s landed at %s (strategy said %s)", action.tower_id, landed, action.pos)
            strat.complete(action, landed)   # later upgrade clicks aim at where it really is
            return
        img = self.vision.screenshot()
        # A Defeat/Victory screen eats the hotkey, so every pickup above fails. That's a lost
        # game, not a placement bug: report it as GameEnded so the main loop retries / moves on.
        screen = self.end_screen(img)
        if screen is not None:
            raise GameEnded(screen)
        raise BotStopped(f"round {self.round}: couldn't place {action.tower_id}; screenshot: {self.save_debug(img, 'place_failed')}")

    def find_valid_pointer(self, target, offset, before_img, max_probes: int = 60,
                           radius: int = 120, step: int = 20, refine: bool = False, accept=None):
        """With a tower held, move the pointer around `target` until its range circle isn't red.

        Probes closest positions first (a `step` px grid out to `radius`). With refine=True,
        once a valid spot is found it binary-searches back toward the target for the valid
        spot *closest* to it, then backs off a few px from the edge so replays aren't
        borderline. Returns the pointer position (cursor left there, still holding), or None.
        """
        ox, oy = offset
        cur = list(target)
        probe_shots: list[np.ndarray] = []

        def probe(x: float, y: float) -> bool:
            mx, my = self.inp.move_by(x - cur[0], y - cur[1])
            cur[0] += mx
            cur[1] += my
            time.sleep(0.1)   # let the held tower follow the cursor (a few frames)
            tower = (cur[0] - ox, cur[1] - oy)
            img = self.vision.screenshot(park=False)
            if not probe_shots:
                probe_shots.append(img)   # the probe at the target itself: evidence if all fail
            delta = self.vision.placement_redness(before_img, img, tower)
            log.debug("probe pointer (%d, %d): redness %s", cur[0], cur[1],
                      "unknown (ring covered)" if delta is None else f"{delta:+.0f}")
            if delta is not None and delta < self.valid_redness_max:
                return True
            if self.click_unclear and (delta is None or delta < self.clearly_red_min):
                # Late-game effects (moving bloons, lightning) muddy the colour reading, so an
                # unclear spot gets the definitive test: click. An invalid spot just refuses
                # (the tower stays held); a valid one places it and the red X disappears.
                self.inp.click_here()
                if self.wait_until(lambda: not self.is_holding(), timeout=0.4):
                    log.debug("click at unclear spot (%d, %d) placed it", cur[0], cur[1])
                    self.placed_by_click = True
                    return True
            return False

        ok = accept or (lambda tower_xy: True)   # extra rule on where the tower may land
        grid = [(dx, dy) for dx in range(-radius, radius + 1, step) for dy in range(-radius, radius + 1, step)
                if ok((target[0] + dx - ox, target[1] + dy - oy))]
        grid.sort(key=lambda d: (d[0] ** 2 + d[1] ** 2, abs(d[0])))  # nearest first, prefer vertical
        for dx, dy in grid[:max_probes]:
            if probe(target[0] + dx, target[1] + dy):
                break
        else:
            # Save what the bot saw: the background before pickup and the held tower at the
            # target (range ring visible), so a red ring can be told apart from a misread.
            log.warning("no valid spot within %d px of %s; background: %s, held at target: %s",
                        radius, target, self.save_debug(before_img, "no_spot_before"),
                        self.save_debug(probe_shots[0], "no_spot_held") if probe_shots else "none")
            return None
        if not refine or (dx, dy) == (0, 0):
            return tuple(cur)

        good, bad = (cur[0], cur[1]), tuple(target)       # invariant: good valid, bad invalid
        for _ in range(5):                               # 5 halvings: within ~1/32 of the gap
            mid = ((good[0] + bad[0]) / 2, (good[1] + bad[1]) / 2)
            # The `accept` rule applies here too: a midpoint outside it counts as "bad", so
            # tightening toward the target can never carry the tower out of bounds.
            if ok((mid[0] - ox, mid[1] - oy)) and probe(*mid):
                good = (cur[0], cur[1])
            else:
                bad = mid
        # Back off ~3 px from the boundary, away from the invalid side.
        vx, vy = good[0] - bad[0], good[1] - bad[1]
        norm = max(1e-6, (vx * vx + vy * vy) ** 0.5)
        safe = (good[0] + 3 * vx / norm, good[1] + 3 * vy / norm)
        if ok((safe[0] - ox, safe[1] - oy)) and probe(*safe):
            return tuple(cur)
        probe(*good)
        return tuple(cur)

    def select_tower(self, pos: tuple[int, int]):
        """Open a tower's upgrade panel, reliably. Returns a screenshot showing it (or None).

        Clicking a tower that is *already* selected deselects it, so first click empty
        ground to clear any selection, then click the tower.
        """
        self.selected = None
        self.inp.click(*self.cfg["game"]["safe_click_px"])
        time.sleep(0.1)
        dx, dy = self.cfg["game"]["select_offset_px"]
        self.inp.click(pos[0] + dx, pos[1] + dy)   # the tower's real centre, below its recorded spot

        def panel_shot():
            img = self.vision.screenshot()
            return img if self.vision.panel_side(img) else None
        return self.wait_until(panel_shot, timeout=1.0)

    def end_screen(self, img) -> str | None:
        """Name of the end-of-game screen showing in `img` ("defeat_text", ...), or None."""
        for screen in ("defeat_text", "victory_text", "insta_monkey_banner"):
            if self.vision.find(screen, img):
                return screen
        return None

    def fresh_cash(self) -> int | None:
        """Read cash from a brand-new screenshot (raises GameEnded if an end screen shows)."""
        img = self.vision.screenshot()
        cash = self.vision.read_cash(img)
        if cash is None:
            screen = self.end_screen(img)
            if screen is not None:
                raise GameEnded(screen)
        return cash

    def upgrade(self, strat: Strategy, action: Action, cash_before: int) -> None:
        """Buy one upgrade tier, verified by the panel's green tier boxes, not by cash.

        Late in the game income arrives faster than a cheap upgrade lowers cash, so a
        cash-based check once called a successful buy a failure and pressed again,
        buying the NEXT tier too. The panel shows the real tiers, so:
          1. select the tower and read its tiers: if they differ from what we believe,
             adopt the game's numbers and re-plan (never buy on a wrong assumption);
          2. press the key ONCE and read the tiers again;
          3. if the tier didn't go up, we couldn't afford it: remember the cash we had and
             wait for more. Never press twice in one go, never stop the bot over it.
        """
        tower = strat.towers[action.tower_id]
        key = self.keys["upgrades"][action.path]
        img, real, fresh = None, None, False
        if self.selected == action.tower_id:
            # Panel should still be open from the previous upgrade on this tower: skip the
            # close-and-reopen. Only trust it if the tiers are exactly what we expect.
            img = self.vision.screenshot()
            real = self.vision.read_panel_tiers(img)
            if real is None or list(real) != tower.tiers:
                img, real = None, None
        if img is None:
            img = self.select_tower(action.pos)
            fresh = True
            real = self.vision.read_panel_tiers(img) if img is not None else None
        if real is None:
            self.fail_count[action] = self.fail_count.get(action, 0) + 1
            log.warning("%s: upgrade panel didn't open (%d)", action.tower_id, self.fail_count[action])
            if self.fail_count[action] >= 8:
                shot = img if img is not None else self.vision.screenshot()
                raise BotStopped(f"round {self.round}: can't open {action.tower_id}'s panel; screenshot: "
                                 f"{self.save_debug(shot, 'no_panel')}")
            return
        if fresh and list(real) != tower.tiers:
            log.warning("%s: panel shows %s but expected %s; syncing to the game",
                        action.tower_id, "-".join(map(str, real)), "-".join(map(str, tower.tiers)))
            strat.set_tiers(action.tower_id, "-".join(map(str, real)))
            self.selected = action.tower_id
            return                         # main loop re-plans from the true state
        self.selected = action.tower_id
        pre = self.vision.read_cash(img)
        if pre is None or pre < action.cost:
            return
        self.inp.key(key)
        want = tower.tiers[action.path] + 1

        def tiers_now():
            t = self.vision.read_panel_tiers(self.vision.screenshot())
            return t if t is not None and t[action.path] == want else None
        after = self.wait_until(tiers_now, timeout=0.7)
        if after is None:
            after = self.vision.read_panel_tiers(self.vision.screenshot())
        if after is not None and after[action.path] == want:
            strat.complete(action)
            self.fail_count.pop(action, None)
            return
        if after is not None and list(after) != tower.tiers:
            # Something else changed (e.g. a different path went up); adopt it and re-plan.
            strat.set_tiers(action.tower_id, "-".join(map(str, after)))
            return
        # Not bought: the real price must be above the cash we had.
        full = strat.tier_price(tower.kind, action.path, action.tier)
        if pre < full and action.tower_id not in strat.no_discount and action.cost < full:
            strat.no_discount.add(action.tower_id)
            log.info("%s: assuming no village discount; waiting for $%d", action.tower_id, full)
        else:
            self.min_cash[action] = int(pre * 1.05)   # retry once cash has grown a bit
            log.info("%s: couldn't afford %s tier %d with $%d; waiting for more cash",
                     action.tower_id, ("top", "middle", "bottom")[action.path], action.tier, pre)

    def set_target(self, strat: Strategy, action: Action) -> None:
        step = strat.steps[action.step_index]["target"]
        if self.select_tower(action.pos) is None:
            log.warning("%s: panel didn't open for targeting; will retry", action.tower_id)
            return
        self.inp.key(self.keys["target_next"], times=action.tab_presses)
        if step.get("camo_priority"):
            self.ensure_camo_priority(action.tower_id)
        self.selected = action.tower_id
        strat.complete(action)   # targeting itself isn't verifiable from the HUD; trust the keys

    def ensure_camo_priority(self, tower_id: str) -> None:
        """Turn on the Sniper's camo-priority toggle (the button beside its portrait).

        It's a toggle, so look before clicking: only click when it shows OFF (plain camo
        bloon), then confirm it shows ON (red crosshair). If neither matches, don't guess.
        The tower's panel must already be open.
        """
        img = self.vision.screenshot()            # parking doesn't close the panel
        if self.vision.find("sniper_camo_toggle_on", img):
            log.info("%s camo priority already on", tower_id)
            return
        off = self.vision.find("sniper_camo_toggle_off", img)
        if off is None:
            log.warning("%s: camo toggle not found (panel closed?), skipping", tower_id)
            return
        self.inp.click(*off)
        time.sleep(0.3)
        if self.vision.find("sniper_camo_toggle_on", self.vision.screenshot()):
            log.info("%s camo priority on", tower_id)
        else:
            log.warning("%s: clicked the camo toggle but it doesn't show ON", tower_id)
