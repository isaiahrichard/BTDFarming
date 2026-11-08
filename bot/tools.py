"""Helpers for building the bot's assets.

  python -m bot.tools screenshot NAME          save the 4K screen to captures/NAME.png
  python -m bot.tools crop CAPTURE NAME x1 y1 x2 y2
                                               cut a template out of a capture and record its region
  python -m bot.tools glyphs IMAGE TEXT {cash,round}
                                               teach the digit reader: TEXT is what the cash/round
                                               display shows in IMAGE, e.g. '$1,450' or '37/100'
  python -m bot.tools ocr IMAGE                print what the bot reads as cash and round
  python -m bot.tools find IMAGE NAME          print a template's match score in IMAGE
  python -m bot.tools layout [--write]         in SANDBOX on Dark Castle: place every tower of the
                                               strategy in order, as close to its target as the game
                                               allows; results go to captures/layout_result.json
  python -m bot.tools layout-apply [FILE]      copy a layout result's landed spots into the strategy
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import yaml

from .input import Input
from .vision import GLYPH_NAMES, Vision
import json
import math
import re
import time
import logging

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())


def vision(park=lambda: None) -> Vision:
    return Vision(ROOT / "templates", CFG["vision"], park=park, screen_px=tuple(CFG["display"]["screen_px"]),
                  output=CFG["display"].get("output"))


def load(path: str):
    img = cv2.imread(path)
    if img is None:
        sys.exit(f"can't read {path}")
    return img


def cmd_screenshot(a):
    inp = Input(CFG["display"])
    img = vision(park=inp.park).screenshot()
    out = ROOT / "captures" / f"{a.name}.png"
    out.parent.mkdir(exist_ok=True)
    cv2.imwrite(str(out), img)
    print(out)


def cmd_crop(a):
    img = load(a.capture)
    x1, y1, x2, y2 = a.box
    cv2.imwrite(str(ROOT / "templates" / f"{a.name}.png"), img[y1:y2, x1:x2])
    regions_file = ROOT / "templates" / "regions.yaml"
    with regions_file.open("a") as f:
        f.write(f"{a.name}: [{x1}, {y1}, {x2}, {y2}]\n")
    print(f"saved templates/{a.name}.png and its region")


def cmd_glyphs(a):
    img = load(a.image)
    band = tuple(CFG["vision"][f"{a.which}_band"])
    glyphs = Vision.segment(img, band)
    if not glyphs:
        sys.exit("no white text found in the band")
    # Keep only glyphs on the main text line: same baseline as the tallest glyph
    # (commas are short but sit on the baseline too).
    top = max(glyphs, key=lambda g: g[0][3] - g[0][1])[0]
    h = top[3] - top[1]
    line = [(b, m) for b, m in glyphs if abs(b[3] - top[3]) < h * 0.6 and (b[3] - b[1]) > h * 0.2]
    # Split into contiguous runs (big gaps separate the number from map scenery) and take
    # the run whose length matches TEXT.
    runs = [line[:1]]
    for g in line[1:]:
        if g[0][0] - runs[-1][-1][0][2] > h * 0.8:
            runs.append([])
        runs[-1].append(g)
    text = a.text
    line = next((r for r in reversed(runs) if len(r) == len(text)), line)
    if len(line) != len(text):
        print(f"found {len(line)} glyphs but TEXT has {len(text)} chars:")
        for b, _ in line:
            print("  glyph box", b)
        sys.exit("adjust the band in config.yaml or crop the image so only the number line is inside")
    out = ROOT / "templates" / "glyphs"
    out.mkdir(exist_ok=True)
    for (b, m), ch in zip(line, text):
        name = GLYPH_NAMES.get(ch, ch)
        n = len(list(out.glob(f"{name}_*.png")))
        cv2.imwrite(str(out / f"{name}_{n}.png"), Vision.normalise(m).astype("uint8") * 255)
        print(f"  {ch!r} -> glyphs/{name}_{n}.png  (box {b})")


def cmd_ocr(a):
    v, img = vision(), load(a.image)
    print("cash :", v.read_cash(img))
    print("round:", v.read_round(img))


def cmd_find(a):
    v, img = vision(), load(a.image)
    score, centre = v.match(a.name, img)
    print(f"{a.name}: score {score:.3f} at {centre}")


# Where you placed each tower in your own winning run (measured from the recording), plus the
# Sub at the moat corner you chose. These are the targets; the layout pass finds the nearest
# valid spot to each, with all earlier towers already on the map.
RECORDED_TARGETS = {
    "skywarden1": (1800, 1225), "sub1": (2185, 740), "skywarden2": (1195, 890),
    "sniper1": (2935, 1050), "obyn": (1745, 805), "druid1": (1575, 775), "druid2": (1435, 750),
    "village1": (1665, 455), "druid3": (1885, 735), "druid4": (1375, 655), "druid5": (1530, 630),
    # druid3 moved up from (1895, 780) on 2026-11-08: it was outside the village's range.
    "alchemist1": (1655, 686), "druid6": (1450, 525), "druid7": (1818, 625), "glue1": (2012, 660),
}


def cmd_layout(a):
    from .runner import Runner
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    strat_path = ROOT / "strategies" / "dark_castle_chimps.yaml"
    steps = yaml.safe_load(strat_path.read_text())["steps"]
    places = [st["place"] for st in steps if "place" in st]
    inp = Input(CFG["display"])
    v = Vision(ROOT / "templates", CFG["vision"], park=inp.park,
               screen_px=tuple(CFG["display"]["screen_px"]),
               refocus=lambda: inp.click(*CFG["game"]["safe_click_px"]),
               output=CFG["display"].get("output"))
    r = Runner(CFG, inp, v, make_strategy=None, logs_dir=ROOT / "logs", stop_file=ROOT / "STOP")
    r.click_unclear = False   # the layout pass needs the exact closest spot, so no trial clicks
    ox, oy = CFG["game"]["placement_pointer_offset_px"]
    inp.click(*CFG["game"]["safe_click_px"]); time.sleep(0.5)

    results = {}
    for pl in places:
        tid, kind = pl["id"], pl["tower"]
        tx, ty = RECORDED_TARGETS.get(tid, tuple(pl["at_px"]))
        target = (tx + ox, ty + oy)
        landed = None
        for attempt in range(2):
            before = r.pick_up(kind, target)
            if before is None:
                continue
            vil = results.get("village1", (None, None, None))[2]
            accept = None
            if vil and kind in ("druid", "alchemist", "glue_gunner"):
                # Must sit inside the village's (measured) 40-unit range, with a 10 px margin,
                # so the Monkey Business/Commerce discount applies to every upgrade.
                limit = 40 * CFG["village"]["px_per_unit"] - 10
                accept = lambda xy, vil=vil, limit=limit: math.dist(xy, vil) <= limit
            ptr = r.find_valid_pointer(target, (ox, oy), before, max_probes=400, radius=250,
                                       refine=True, accept=accept)
            if ptr is None:
                inp.park()
                break
            if kind in ("alchemist", "monkey_village"):
                # Keep a picture of the range circle while still held: shows what it covers.
                cv2.imwrite(str(ROOT / "captures" / f"layout_range_{tid}.png"), v.screenshot(park=False))
            if r.drop_here():
                landed = (round(ptr[0] - ox), round(ptr[1] - oy))
                break
        results[tid] = (kind, (tx, ty), landed)
        off = "FAILED" if landed is None else f"{math.dist(landed, (tx, ty)):.0f} px off"
        print(f"{tid:11s} target {tx:4d},{ty:4d} -> landed {landed}  ({off})", flush=True)
        inp.click(*CFG["game"]["safe_click_px"]); time.sleep(0.3)   # deselect

    # Final picture with labels and the estimated village ranges.
    img = v.screenshot()
    cv2.imwrite(str(ROOT / "captures" / "layout_sandbox.png"), img)
    ann = img.copy()
    ppu = CFG["village"]["px_per_unit"]
    vil = results.get("village1", (None, None, None))[2]
    if vil:
        cv2.circle(ann, vil, round(40 * ppu), (0, 255, 255), 4)
        cv2.circle(ann, vil, round(48 * ppu), (0, 165, 255), 3)
    for tid, (kind, tgt, landed) in results.items():
        cv2.drawMarker(ann, tgt, (0, 0, 255), cv2.MARKER_TILTED_CROSS, 30, 4)
        if landed:
            cv2.circle(ann, landed, 12, (255, 0, 255), -1)
            cv2.putText(ann, tid, (landed[0] + 15, landed[1] - 15), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (255, 255, 255), 4)
            cv2.putText(ann, tid, (landed[0] + 15, landed[1] - 15), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2)
    cv2.imwrite(str(ROOT / "captures" / "layout_sandbox_annotated.png"), ann)

    if vil:
        print("\nDistance from village (estimate: 40 units = %d px, 48 = %d px):" % (40 * ppu, 48 * ppu))
        for tid, (kind, tgt, landed) in results.items():
            if landed and kind in ("druid", "alchemist", "glue_gunner"):
                d = math.dist(landed, vil)
                print(f"  {tid:11s} {d:5.0f} px  {'in range' if d <= 40 * ppu else ('in range after Bigger Radius' if d <= 48 * ppu else 'OUT of range')}")

    out = ROOT / "captures" / "layout_result.json"
    out.write_text(json.dumps({tid: {"kind": k, "target": list(t), "landed": list(l) if l else None}
                               for tid, (k, t, l) in results.items()}, indent=1))
    print(f"\nresults saved to {out}  (apply with: python -m bot.tools layout-apply)")
    if a.write:
        apply_layout(out)


def apply_layout(result_file: Path) -> None:
    """Copy landed positions from a layout result file into the strategy file."""
    strat_path = ROOT / "strategies" / "dark_castle_chimps.yaml"
    results = json.loads(Path(result_file).read_text())
    text = strat_path.read_text()
    for tid, r in results.items():
        if not r["landed"]:
            print(f"  {tid}: no landed position, left unchanged")
            continue
        (lx, ly), (tx, ty) = r["landed"], r["target"]
        pat = re.compile(r"(\{place: \{id: " + re.escape(tid) + r", tower: [a-z_]+, at_px: )\[[^\]]*\], at: \[[^\]]*\](\}[^\n]*?\})[^\n]*")
        text, n = pat.subn(lambda m: f"{m.group(1)}[{lx}, {ly}], at: [{lx / 3840:.4f}, {ly / 2160:.4f}]{m.group(2)}"
                                     f"   # sandbox layout 2026-11-08 (target {tx}, {ty})", text)
        if n != 1:
            print(f"  could not update {tid} in the strategy file")
    strat_path.write_text(text)
    print(f"wrote landed positions to {strat_path}")


def cmd_layout_apply(a):
    apply_layout(Path(a.file))


def main():
    ap = argparse.ArgumentParser(prog="python -m bot.tools", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(required=True)
    p = sub.add_parser("screenshot"); p.add_argument("name"); p.set_defaults(f=cmd_screenshot)
    p = sub.add_parser("crop"); p.add_argument("capture"); p.add_argument("name")
    p.add_argument("box", nargs=4, type=int); p.set_defaults(f=cmd_crop)
    p = sub.add_parser("glyphs"); p.add_argument("image"); p.add_argument("text")
    p.add_argument("which", choices=["cash", "round"]); p.set_defaults(f=cmd_glyphs)
    p = sub.add_parser("ocr"); p.add_argument("image"); p.set_defaults(f=cmd_ocr)
    p = sub.add_parser("find"); p.add_argument("image"); p.add_argument("name"); p.set_defaults(f=cmd_find)
    p = sub.add_parser("layout"); p.add_argument("--write", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true"); p.set_defaults(f=cmd_layout)
    p = sub.add_parser("layout-apply"); p.add_argument("file", nargs="?", default=str(ROOT / "captures" / "layout_result.json"))
    p.set_defaults(f=cmd_layout_apply)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
