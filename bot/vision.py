"""Eyes: screenshots, template matching, and reading numbers off the screen.

Vision reports facts ("the Victory text is at (1900, 600)", "cash is 1450"). It never decides
what to do about them; that's the runner's job.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
import yaml

log = logging.getLogger(__name__)

Box = tuple[int, int, int, int]  # x1, y1, x2, y2 in 4K pixels

# Glyph files are named by character; these characters can't be file names.
GLYPH_NAMES = {"$": "dollar", "/": "slash", ",": "comma"}
GLYPH_SIZE = (24, 36)  # every glyph is resized to this before comparing


class Vision:
    def __init__(self, templates_dir: Path, cfg: dict, park: Callable[[], None],
                 screen_px: tuple[int, int] = (3840, 2160),
                 refocus: Callable[[], None] | None = None, output: str | None = None):
        self.dir = Path(templates_dir)
        self.cfg = cfg
        self.park = park  # input.park, so the cursor is never in our screenshots
        self.refocus = refocus  # clicks the game if another window stole focus
        self.kwin = None
        if output:
            try:
                from .kwin_capture import KWinCapture
                self.kwin = KWinCapture(output)
            except Exception as e:
                log.warning("KWin capture unavailable (%s); using spectacle", e)
        self.screen_px = tuple(screen_px)
        self.scale = cfg["match_scale"]
        regions_file = self.dir / "regions.yaml"
        self.regions: dict[str, list[int]] = yaml.safe_load(regions_file.read_text()) or {}
        self._templates: dict[str, np.ndarray] = {}
        self.glyphs = self._load_glyphs()

    # ---------------------------------------------------------------- screenshots
    def screenshot(self, park: bool = True) -> np.ndarray:
        """Capture the game monitor as a BGR numpy array (OpenCV's channel order).

        park=False keeps the cursor where it is (needed while holding a tower: parking moves
        over the shop panel, which drops it). Uses KWin's D-Bus capture (~50 ms) and falls
        back to spectacle (~800 ms) if that ever fails.
        """
        if park:
            self.park()
        if self.kwin is not None:
            try:
                img = self.kwin.grab()
                if (img.shape[1], img.shape[0]) == self.screen_px:
                    return img
                log.warning("KWin capture is %sx%s, expected %s", img.shape[1], img.shape[0], self.screen_px)
            except Exception as e:      # D-Bus hiccup: use the slow path this once
                log.warning("KWin capture failed (%s); using spectacle", e)
        return self._spectacle_screenshot(park)

    def _spectacle_screenshot(self, park: bool) -> np.ndarray:
        """Slow fallback. `spectacle -m` captures the monitor of the focused window."""
        got = None
        for attempt in range(4):
            if park:
                self.park()
            fd, path = tempfile.mkstemp(suffix=".png", prefix="btd_")
            os.close(fd)
            try:
                subprocess.run(["spectacle", "-b", "-n", "-m", "-o", path],
                               capture_output=True, timeout=10)
                img = cv2.imread(path)
            except subprocess.TimeoutExpired:
                img = None
            finally:
                os.remove(path)
            if img is not None and (img.shape[1], img.shape[0]) == self.screen_px:
                return img
            # Two known failure modes: spectacle occasionally exits 0 without writing a file
            # (img is None, just retry), or another window took focus so we captured the
            # wrong monitor (wrong size: click the game to take focus back).
            got = None if img is None else (img.shape[1], img.shape[0])
            log.warning("screenshot attempt %d gave %s, retrying", attempt + 1, got)
            if got is not None and self.refocus is not None:
                self.refocus()
            time.sleep(0.3)
        raise RuntimeError(f"screenshot is {got}, expected {self.screen_px} after 4 attempts")

    # ---------------------------------------------------------------- templates
    def has_template(self, name: str) -> bool:
        return (self.dir / f"{name}.png").exists()

    def _template(self, name: str) -> np.ndarray:
        if name not in self._templates:
            path = self.dir / f"{name}.png"
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is None:
                raise FileNotFoundError(f"template {path} missing — capture it with `python -m bot.tools crop`")
            self._templates[name] = cv2.resize(img, None, fx=self.scale, fy=self.scale,
                                               interpolation=cv2.INTER_AREA)
        return self._templates[name]

    def _search_box(self, name: str) -> Box:
        w, h = self.screen_px
        if name not in self.regions:
            return 0, 0, w, h
        m = self.cfg["region_margin_px"]
        x1, y1, x2, y2 = self.regions[name]
        return max(0, x1 - m), max(0, y1 - m), min(w, x2 + m), min(h, y2 + m)

    def match(self, name: str, img: np.ndarray) -> tuple[float, tuple[int, int]]:
        """Best match score (0..1) and its centre in 4K pixels."""
        tpl = self._template(name)
        x1, y1, x2, y2 = self._search_box(name)
        crop = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        # Grayscale + half size: ~8x less work, and buttons are still unmistakable.
        crop = cv2.resize(crop, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA)
        if crop.shape[0] < tpl.shape[0] or crop.shape[1] < tpl.shape[1]:
            return 0.0, (0, 0)
        scores = cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED)
        _, best, _, (bx, by) = cv2.minMaxLoc(scores)
        cx = x1 + (bx + tpl.shape[1] / 2) / self.scale
        cy = y1 + (by + tpl.shape[0] / 2) / self.scale
        return float(best), (round(cx), round(cy))

    def find(self, name: str, img: np.ndarray | None = None,
             threshold: float | None = None) -> tuple[int, int] | None:
        """Centre of `name` on screen, or None if it isn't there."""
        img = self.screenshot() if img is None else img
        score, centre = self.match(name, img)
        found = score >= (threshold or self.cfg["match_threshold"])
        log.debug("find %-26s score=%.3f %s", name, score, "FOUND" if found else "")
        return centre if found else None

    def wait_for(self, name: str, timeout: float, interval: float | None = None,
                 threshold: float | None = None) -> tuple[int, int]:
        """Poll the screen until template `name` appears; return its centre.

        Raises TimeoutError after `timeout` seconds, saving the last screenshot to logs/.
        """
        # Step 1: fill in the default. `is None` (not `or`) so an explicit interval=0 is respected.
        interval = self.cfg["poll_interval_s"] if interval is None else interval

        # Step 2: compute the deadline once. monotonic() only ever moves forward, unlike
        # time.time(), which jumps if the system clock is adjusted (NTP sync, DST, manual change).
        deadline = time.monotonic() + timeout

        while True:
            # Step 3: look once. Remember when this look started so we can pace the loop.
            started = time.monotonic()
            img = self.screenshot()
            centre = self.find(name, img, threshold)
            if centre is not None:
                return centre                      # success: the caller gets (x, y)

            # Step 4: give up past the deadline, leaving evidence behind for debugging.
            if time.monotonic() >= deadline:
                score, _ = self.match(name, img)   # how close did it get? helps tune thresholds
                logs = self.dir.parent / "logs"
                logs.mkdir(exist_ok=True)
                path = logs / f"{time.strftime('%Y%m%d-%H%M%S')}_timeout_{name}.png"
                cv2.imwrite(str(path), img)
                raise TimeoutError(f"'{name}' not seen within {timeout:.0f}s "
                                   f"(best score {score:.2f}); last screenshot: {path}")

            # Step 5: sleep only for what's left of the interval (the screenshot already used
            # some of it), and never sleep past the deadline.
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, min(interval - elapsed, deadline - time.monotonic())))

    # ---------------------------------------------------------------- placement validity
    @staticmethod
    def ring_redness(img: np.ndarray, centre: tuple[float, float], r1: int = 120, r2: int = 200) -> float:
        """Mean (red - green) in a ring around `centre`.

        While a tower is held, BTD6 draws its range circle grey on valid spots and red on
        invalid ones. Comparing this against the same ring *before* picking the tower up
        cancels out the background (brown dirt is reddish too).
        """
        cx, cy = round(centre[0]), round(centre[1])
        h, w = img.shape[:2]
        x1, x2, y1, y2 = max(0, cx - r2), min(w, cx + r2), max(0, cy - r2), min(h, cy + r2)
        patch = img[y1:y2, x1:x2].astype(np.int32)
        yy, xx = np.mgrid[y1:y2, x1:x2]
        d = np.hypot(xx - cx, yy - cy)
        ring = (d > r1) & (d < r2)
        return float(patch[..., 2][ring].mean() - patch[..., 1][ring].mean())

    @staticmethod
    def placement_redness(before: np.ndarray, held: np.ndarray, centre: tuple[float, float],
                          r1: int = 120, r2: int = 200, min_pixels: int = 300) -> float | None:
        """How red (vs bluish-grey) the range-circle overlay made the ring around `centre`.

        The game draws the range circle *under* tower sprites, so in a crowded cluster most
        of the ring is covered by towers whose pixels don't change. Averaging over the whole
        ring then hides a red circle (a false "valid"). So only pixels that actually changed
        between `before` (nothing held) and `held` count: those are the overlay. Returns None
        when too few pixels changed to tell (callers should treat that as invalid).
        """
        cx, cy = round(centre[0]), round(centre[1])
        h, w = held.shape[:2]
        x1, x2, y1, y2 = max(0, cx - r2), min(w, cx + r2), max(0, cy - r2), min(h, cy + r2)
        a = before[y1:y2, x1:x2].astype(np.int32)
        b = held[y1:y2, x1:x2].astype(np.int32)
        yy, xx = np.mgrid[y1:y2, x1:x2]
        d = np.hypot(xx - cx, yy - cy)
        changed = (d > r1) & (d < r2) & (np.abs(b - a).sum(axis=2) > 25)
        if changed.sum() < min_pixels:
            return None
        # Valid circles are bluish grey (blue goes up), invalid ones red (red up, blue down),
        # so (change in R) - (change in B) separates them. Calibrated in sandbox 2026-11-08:
        # red spots +8 (crowded) .. +46 (open track); valid spots -32 .. -37.
        d_red = b[..., 2] - a[..., 2]
        d_blue = b[..., 0] - a[..., 0]
        return float((d_red - d_blue)[changed].mean())



    def fast_forward_state(self, img: np.ndarray) -> str | None:
        """'on' / 'off' for the fast-forward button during a round, None if it isn't showing.

        The two states differ only by a glow (ON: glowing arrows, white rim), so the
        templates score 1.00 vs 0.94 against each other. A threshold can't separate that,
        but comparing which of the two matches *better*, at full resolution, can.
        """
        x1, y1, x2, y2 = self._search_box("ingame_ff_on")
        crop = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        scores = {}
        for state in ("on", "off"):
            tpl = cv2.imread(str(self.dir / f"ingame_ff_{state}.png"), cv2.IMREAD_GRAYSCALE)
            scores[state] = cv2.minMaxLoc(cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED))[1]
        best = max(scores, key=scores.get)
        log.debug("fast-forward scores on=%.3f off=%.3f", scores["on"], scores["off"])
        if scores[best] < self.cfg["match_threshold"] or abs(scores["on"] - scores["off"]) < 0.01:
            return None
        return best

    # ---------------------------------------------------------------- upgrade panel
    # Measured 2026-11-08 at 4K. A tower's panel opens on the side away from it; each path
    # row has a column of 5 boxes (tier 1 at the bottom) that turn bright green when owned.
    PANEL_PIP_X = {"left": 113, "right": 2555}
    PANEL_ROW_T1_Y = (1084, 1383, 1682)      # top, middle, bottom path: tier-1 box centre
    PANEL_PIP_DY = 49                        # spacing between tiers, upward
    PANEL_SELL_BOX = {"left": (520, 1740, 780, 1890), "right": (2950, 1740, 3210, 1890)}

    def panel_side(self, img: np.ndarray) -> str | None:
        """Which side an upgrade panel is open on (found by its Sell button), or None."""
        tpl = self._template("panel_sell")
        best, side = 0.0, None
        for name, (x1, y1, x2, y2) in self.PANEL_SELL_BOX.items():
            crop = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
            crop = cv2.resize(crop, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA)
            score = cv2.minMaxLoc(cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED))[1]
            if score > best:
                best, side = score, name
        return side if best >= self.cfg["match_threshold"] else None

    def read_panel_tiers(self, img: np.ndarray) -> tuple[int, int, int] | None:
        """The selected tower's crosspath, read from the green boxes in its upgrade panel.

        Unlike cash, this can't be fooled by income arriving mid-round, and it also shows
        whether the RIGHT tower is selected (a wrong tower has different tiers).
        Returns None if no panel is open.
        """
        side = self.panel_side(img)
        if side is None:
            return None
        x = self.PANEL_PIP_X[side]
        tiers = []
        for t1 in self.PANEL_ROW_T1_Y:
            n = 0
            for t in range(5):
                y = t1 - self.PANEL_PIP_DY * t
                p = img[y - 8:y + 8, x - 8:x + 8].astype(np.int32)
                green = ((p[..., 1] > 170) & (p[..., 2] < 170) & (p[..., 0] < 90)).mean()
                if green < 0.5:
                    break                 # owned boxes are contiguous from tier 1
                n += 1
            tiers.append(n)
        return tuple(tiers)

    # ---------------------------------------------------------------- reading numbers
    def _load_glyphs(self) -> list[tuple[str, np.ndarray]]:
        """templates/glyphs/<char>_<n>.png -> [(char, normalised mask), ...]."""
        names = {v: k for k, v in GLYPH_NAMES.items()}
        out = []
        for p in sorted((self.dir / "glyphs").glob("*.png")):
            char = p.stem.rsplit("_", 1)[0]
            out.append((names.get(char, char), cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) > 127))
        return out

    @staticmethod
    def segment(img: np.ndarray, band: Box) -> list[tuple[Box, np.ndarray]]:
        """Split the white, black-outlined game text in `band` into glyphs, left to right.

        BTD6 numbers are pure white with a thick black outline, so "very bright in all three
        channels" isolates the glyph interiors from the busy map behind them.
        """
        x1, y1, x2, y2 = band
        crop = img[y1:y2, x1:x2]
        mask = np.all(crop > 225, axis=2).astype(np.uint8)
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        glyphs = []
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            if area < 25 or h < 8:  # specks
                continue
            g = (labels[y:y + h, x:x + w] == i)
            glyphs.append(((x1 + x, y1 + y, x1 + x + w, y1 + y + h), g))
        glyphs.sort(key=lambda t: t[0][0])
        return glyphs

    @staticmethod
    def normalise(mask: np.ndarray) -> np.ndarray:
        return cv2.resize(mask.astype(np.uint8) * 255, GLYPH_SIZE, interpolation=cv2.INTER_AREA) > 127

    def classify(self, mask: np.ndarray) -> tuple[str, float]:
        """Closest known glyph by IoU (overlap / union of the two shapes)."""
        if not self.glyphs:
            return "?", 0.0
        g = self.normalise(mask)
        best, best_iou = "?", 0.0
        for char, ref in self.glyphs:
            iou = (g & ref).sum() / max(1, (g | ref).sum())
            if iou > best_iou:
                best, best_iou = char, iou
        return (best, best_iou) if best_iou >= 0.6 else ("?", best_iou)

    def _text_line(self, img: np.ndarray, band: Box, anchor: str) -> str | None:
        """Read the line of text containing glyph `anchor` ('$' or '/').

        Upgrade panels shift the HUD sideways, so instead of a fixed position we find the
        anchor glyph and collect neighbours of similar height on the same baseline.
        """
        glyphs = self.segment(img, band)
        labelled = [(box, self.classify(m)[0]) for box, m in glyphs]
        anchors = [box for box, c in labelled if c == anchor]
        if not anchors:
            return None
        ax1, ay1, ax2, ay2 = anchors[0]
        ah = ay2 - ay1
        line = []
        for (x1, y1, x2, y2), c in labelled:
            h = y2 - y1
            same_line = abs(y2 - ay2) < ah * 0.35 or (c == "," and abs(y2 - ay2) < ah * 0.6)
            if same_line and (h > ah * 0.5 or c == ","):
                line.append(((x1, x2), c))
        # Keep the contiguous run around the anchor (stop at gaps wider than ~one glyph).
        line.sort()
        idx = next(i for i, ((x1, _), _) in enumerate(line) if x1 == ax1)
        run = [line[idx]]
        for step in (-1, 1):
            i = idx
            while 0 <= i + step < len(line):
                a, b = (line[i], line[i + step]) if step > 0 else (line[i + step], line[i])
                if b[0][0] - a[0][1] > ah * 0.8:
                    break
                run.insert(0 if step < 0 else len(run), line[i + step])
                i += step
        return "".join(c for _, c in run)

    def _find_coins(self, img: np.ndarray) -> tuple[int, int, int] | None:
        """Locate the HUD's gold-coin icon in the cash band: (right edge x, top y, bottom y).

        The coins sit immediately left of the cash and move with it when an upgrade panel
        opens. Being big and colourful, they match far more reliably than the thin "$" glyph.
        """
        x1, y1, x2, y2 = self.cfg["cash_band"]
        tpl = self._template("hud_coins")
        crop = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        crop = cv2.resize(crop, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA)
        if crop.shape[0] < tpl.shape[0] or crop.shape[1] < tpl.shape[1]:
            return None
        _, score, _, (bx, by) = cv2.minMaxLoc(cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED))
        if score < 0.6:
            log.debug("coins not found (score %.2f)", score)
            return None
        left, top = x1 + bx / self.scale, y1 + by / self.scale
        return round(left + tpl.shape[1] / self.scale), round(top), round(top + tpl.shape[0] / self.scale)

    def read_cash(self, img: np.ndarray) -> int | None:
        coins = self._find_coins(img)
        if coins is None:
            return None
        right, top, bottom = coins
        # Glyphs that start just right of the coins and overlap them vertically.
        glyphs = [(b, m) for b, m in self.segment(img, tuple(self.cfg["cash_band"]))
                  if b[0] >= right - 15 and b[1] < bottom and b[3] > top]
        if len(glyphs) < 2:
            return None
        # Contiguous run from the first glyph (stop at a gap wider than ~a glyph).
        height = max(b[3] - b[1] for b, _ in glyphs[:3])
        run = [glyphs[0]]
        for g in glyphs[1:]:
            if g[0][0] - run[-1][0][2] > height * 0.8:
                break
            run.append(g)
        # Digits share a top line; use the tallest early glyph's top as the reference.
        ref_top = min(b[1] for b, _ in run[1:3]) if len(run) > 1 else run[0][0][1]
        digits = ""
        for box, mask in run[1:]:                 # run[0] is the "$": skip it, whatever it looks like
            h = box[3] - box[1]
            if h < height * 0.5 and box[1] > ref_top + height * 0.4:
                continue                          # comma: short and sitting low
            if abs(box[1] - ref_top) > height * 0.25:
                break                             # doesn't line up: scenery after the number (e.g. a cobweb)
            char, _ = self.classify(mask)
            if not char.isdigit():
                if digits:
                    break                         # number already read; the rest is scenery
                log.debug("cash glyph at x=%d read as %r", box[0], char)
                return None
            digits += char
        return int(digits) if digits else None

    def read_round(self, img: np.ndarray) -> int | None:
        text = self._text_line(img, tuple(self.cfg["round_band"]), "/")
        if not text or "/" not in text:
            return None
        current = text.split("/", 1)[0]
        return int(current) if current.isdigit() else None
