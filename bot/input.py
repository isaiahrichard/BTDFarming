"""Hands: the only module that talks to ydotool.

Coordinates everywhere else in the bot are *physical 4K pixels* (what a screenshot shows).
This module converts them to ydotool's world: relative moves in KDE logical pixels, starting
from a known anchor (the 4K monitor's top-right corner). See config.yaml `display`.
"""
from __future__ import annotations

import logging
import subprocess
import time

log = logging.getLogger(__name__)

# ydotool 1.x `key` takes Linux input keycodes (see /usr/include/linux/input-event-codes.h),
# not key names, so we keep a small lookup table.
KEYCODES = {
    "esc": 1, "tab": 15, "space": 57, "enter": 28,
    "q": 16, "w": 17, "e": 18, "r": 19, "t": 20, "y": 21, "u": 22, "i": 23, "o": 24, "p": 25,
    "a": 30, "s": 31, "d": 32, "f": 33, "g": 34, "h": 35, "j": 36, "k": 37, "l": 38,
    "z": 44, "x": 45, "c": 46, "v": 47, "b": 48, "n": 49, "m": 50,
}
LEFT_CLICK = "0xC0"  # ydotool: 0x40 = press, 0x80 = release, low bits 0 = left button


class Input:
    def __init__(self, display: dict, settle_s: float = 0.08, dry_run: bool = False):
        self.scale = display["scale"]
        self.logical_w = display["logical_size"][0]
        self.home_move = display["home_move"]
        self.settle_s = settle_s  # give the game a frame or two to notice each event
        self.dry_run = dry_run

    def _ydotool(self, *args: str) -> None:
        if self.dry_run:
            log.info("[dry-run] ydotool %s", " ".join(args))
            return
        subprocess.run(["ydotool", *args], check=True, capture_output=True)

    def home(self) -> None:
        """Pin the cursor to the 4K monitor's top-right corner (logical x=right edge, y=0)."""
        self._ydotool("mousemove", "-x", str(self.home_move[0]), "-y", str(self.home_move[1]))
        time.sleep(self.settle_s)

    def to_relative(self, px: float, py: float) -> tuple[int, int]:
        """Physical 4K pixel -> relative move from the home corner."""
        return round(px / self.scale - self.logical_w), round(py / self.scale)

    def move_to(self, px: float, py: float) -> None:
        # Re-homing before every move means errors never accumulate between moves.
        self.home()
        dx, dy = self.to_relative(px, py)
        self._ydotool("mousemove", "-x", str(dx), "-y", str(dy))
        time.sleep(self.settle_s)

    def click(self, px: float, py: float) -> None:
        log.debug("click (%d, %d)", px, py)
        self.move_to(px, py)
        self.click_here()

    def move_by(self, dx_px: float, dy_px: float) -> tuple[float, float]:
        """Relative move in physical pixels *without* re-homing (safe while holding a tower).

        Returns the physical distance actually moved (moves are whole logical pixels).
        """
        dx, dy = round(dx_px / self.scale), round(dy_px / self.scale)
        self._ydotool("mousemove", "-x", str(dx), "-y", str(dy))
        time.sleep(self.settle_s)
        return dx * self.scale, dy * self.scale

    def click_here(self) -> None:
        """Click wherever the cursor is, without re-homing.

        Needed for tower placement: homing passes over the shop panel (top-right), and in
        Drag & Drop mode a held tower dragged over the shop is cancelled.
        """
        self._ydotool("click", LEFT_CLICK)
        time.sleep(self.settle_s)

    def key(self, name: str, times: int = 1) -> None:
        code = KEYCODES[name.lower()]
        for _ in range(times):
            log.debug("key %s", name)
            self._ydotool("key", f"{code}:1", f"{code}:0")
            time.sleep(self.settle_s)

    def park(self) -> None:
        """Move the cursor out of the way before a screenshot.

        BTD6 draws its own hand cursor into the frame, so a cursor over a button would spoil
        template matches. The home corner is also on the 4K, which `spectacle -m` needs.
        """
        self.home()
