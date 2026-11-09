"""Plan: turn the strategy YAML into concrete, one-tier-at-a-time actions with prices.

Pure logic: no screenshots, no input. That's what makes it unit-testable (see tests/).
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PATHS = ("top", "middle", "bottom")
TARGET_CYCLE = ("first", "last", "close", "strong")  # order the Tab key cycles through


def upgrade_cost(full_price: int, tier: int, discount_pct: int) -> int:
    """Price the bot should wait for before buying one upgrade tier."""
    
    if tier > 3:
        return full_price
    else:
        discounted_price = (full_price * (1 - discount_pct/100))
        return 5 * math.ceil(discounted_price / 5)


@dataclass
class Tower:
    id: str
    kind: str                      # key into costs_hard.yaml towers / config hotkeys
    pos: tuple[int, int]           # 4K pixels
    tiers: list[int] = field(default_factory=lambda: [0, 0, 0])
    target: str = "first"


@dataclass(frozen=True)
class Action:
    """One atomic thing to do in the game."""
    kind: str                      # "place" | "upgrade" | "target" | "ability"
    tower_id: str
    pos: tuple[int, int]
    cost: int = 0
    tower_kind: str = ""           # place
    path: int = -1                 # upgrade: 0/1/2
    tier: int = 0                  # upgrade: tier being bought
    target: str = ""               # target: desired mode
    tab_presses: int = 0           # target: how many Tab presses get there
    key: str = ""                  # ability: hotkey to press
    min_round: int = 0             # ability: don't use it before this round
    step_index: int = -1           # which YAML step this belongs to

    def describe(self) -> str:
        if self.kind == "place":
            return f"place {self.tower_id} ({self.tower_kind}) at {self.pos} for ${self.cost}"
        if self.kind == "upgrade":
            return f"upgrade {self.tower_id} {PATHS[self.path]} -> tier {self.tier} for ${self.cost}"
        if self.kind == "ability":
            return f"ability {self.tower_id} (key {self.key}) from round {self.min_round}"
        return f"target {self.tower_id} -> {self.target} ({self.tab_presses}x tab)"


def parse_crosspath(s: str) -> list[int]:
    tiers = [int(t) for t in s.split("-")]
    if len(tiers) != 3 or not all(0 <= t <= 5 for t in tiers):
        raise ValueError(f"bad crosspath {s!r}")
    return tiers


class Strategy:
    def __init__(self, strategy: dict, costs: dict, px_per_unit: float):
        self.steps = strategy["steps"]
        self.costs = costs
        self.px_per_unit = px_per_unit
        self.towers: dict[str, Tower] = {}
        self.step_index = 0
        self.no_discount: set[str] = set()   # towers the game showed to be out of village range
        self.abilities_used: set[int] = set()   # step indexes of ability steps already pressed

    @classmethod
    def load(cls, strategy_path: Path, costs_path: Path, px_per_unit: float) -> "Strategy":
        return cls(yaml.safe_load(Path(strategy_path).read_text()),
                   yaml.safe_load(Path(costs_path).read_text()), px_per_unit)

    # ---------------------------------------------------------------- prices
    def base_cost(self, kind: str) -> int:
        if kind.startswith("hero_"):
            return self.costs["hero"][kind.removeprefix("hero_")]["cost"]
        return self.costs["towers"][kind]["base"]

    def tier_price(self, kind: str, path: int, tier: int) -> int:
        return self.costs["towers"][kind]["paths"][PATHS[path]][tier - 1]["cost"]

    def discount_pct(self, tower: Tower) -> int:
        """Total village discount for purchases made by `tower` right now.

        Wiki rules: Monkey Business (bottom tier >= 1) gives 10% and doesn't stack;
        each Monkey Commerce village (bottom tier >= 2) adds 5%, at most 3 of them.
        Only villages whose radius covers the tower's centre count. Villages don't
        discount themselves here (unknown on the wiki, so we assume the safe answer).
        """
        if tower.id in self.no_discount:
            return 0
        business, commerce = False, 0
        for v in self.towers.values():
            if v.kind != "monkey_village" or v.id == tower.id or v.tiers[2] < 1:
                continue
            radius_units = 48 if v.tiers[0] >= 1 else 40   # Bigger Radius: 40 -> 48
            if math.dist(v.pos, tower.pos) <= radius_units * self.px_per_unit:
                business = True
                commerce += v.tiers[2] >= 2
        return (10 if business else 0) + 5 * min(commerce, 3)

    # ---------------------------------------------------------------- planning
    def next_action(self) -> Action | None:
        """The next single action to perform, or None when the strategy is finished.

        One YAML step can expand into several actions (0-0-0 -> 3-0-1 is four upgrade
        tiers). Because towers' tiers are updated as each action completes, calling this
        again after complete() naturally moves on to the next tier or the next step.
        """
        while self.step_index < len(self.steps):
            step = self.steps[self.step_index]
            action = self._action_for(step)
            if action is not None:
                return action
            self.step_index += 1  # this step is fully done
        return None

    def _action_for(self, step: dict) -> Action | None:
        i = self.step_index
        if "place" in step:
            p = step["place"]
            if p["id"] in self.towers:
                return None
            # Placements are paid in full: the village's 10-15% comes back as a refund *after*
            # placing, so the bot still needs the full price in hand.
            return Action("place", p["id"], tuple(p["at_px"]), cost=self.base_cost(p["tower"]),
                          tower_kind=p["tower"], step_index=i)
        if "upgrade" in step:
            u = step["upgrade"]
            t = self.towers[u["id"]]
            goal = parse_crosspath(u["to"])
            for path in range(3):  # top, then middle, then bottom; lowest tier first
                if t.tiers[path] < goal[path]:
                    tier = t.tiers[path] + 1
                    cost = upgrade_cost(self.tier_price(t.kind, path, tier), tier, self.discount_pct(t))
                    return Action("upgrade", t.id, t.pos, cost=cost, path=path, tier=tier, step_index=i)
            return None
        if "target" in step:
            g = step["target"]
            t = self.towers[g["id"]]
            if t.target == g["mode"]:
                return None
            presses = (TARGET_CYCLE.index(g["mode"]) - TARGET_CYCLE.index(t.target)) % len(TARGET_CYCLE)
            return Action("target", t.id, t.pos, target=g["mode"], tab_presses=presses, step_index=i)
        if "ability" in step:
            # One press of an activated ability (e.g. Obyn's), held back until `round`.
            a = step["ability"]
            if i in self.abilities_used:
                return None
            return Action("ability", a["id"], self.towers[a["id"]].pos, key=str(a["key"]),
                          min_round=a.get("round", 0), step_index=i)
        raise ValueError(f"step {i} has no place/upgrade/target/ability: {step}")

    def complete(self, action: Action, pos: tuple[int, int] | None = None) -> None:
        """Record that `action` succeeded. `pos` = where a tower actually landed (after nudging)."""
        if action.kind == "place":
            self.towers[action.tower_id] = Tower(action.tower_id, action.tower_kind, pos or action.pos)
        elif action.kind == "upgrade":
            self.towers[action.tower_id].tiers[action.path] = action.tier
        elif action.kind == "target":
            self.towers[action.tower_id].target = action.target
        elif action.kind == "ability":
            self.abilities_used.add(action.step_index)

    def skip_to(self, step: int) -> None:
        """Pretend steps before `step` are done (towers at their strategy positions).

        For resuming a game that's already in progress, e.g. `python -m bot --from-step 2`.
        """
        while (a := self.next_action()) is not None and a.step_index < step:
            self.complete(a)

    def set_tiers(self, tower_id: str, crosspath: str) -> None:
        """Correct a tower's tiers to what the game really shows (e.g. after a crash)."""
        self.towers[tower_id].tiers = parse_crosspath(crosspath)

    # ---------------------------------------------------------------- retry support
    def snapshot(self) -> dict:
        """Everything needed to rewind to this moment (used for 'Retry last round')."""
        return {"step_index": self.step_index, "towers": copy.deepcopy(self.towers),
                "no_discount": set(self.no_discount), "abilities_used": set(self.abilities_used)}

    def restore(self, snap: dict) -> None:
        self.step_index = snap["step_index"]
        self.towers = copy.deepcopy(snap["towers"])
        self.no_discount = set(snap.get("no_discount", ()))
        self.abilities_used = set(snap.get("abilities_used", ()))

    @property
    def finished(self) -> bool:
        return self.next_action() is None
