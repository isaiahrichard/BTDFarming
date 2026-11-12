"""Entry point: `python -m bot [--games N] [--start ingame|menu] [--dry-run]`.

Stopping the bot (it controls your mouse, so you need more than one way):
  * Ctrl+C in the terminal running it (run that terminal on the 1080p monitor)
  * `touch STOP` in the project folder; the bot checks for it every loop
  * emergency: `systemctl --user stop ydotool` cuts off all fake input instantly
"""
from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

from .input import Input
from .runner import BotStopped, Runner
from .strategy import Strategy
from .vision import Vision

ROOT = Path(__file__).resolve().parent.parent


def setup_logging(verbose: bool) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "logs" / "bot.log")],
    )


def notify(text: str) -> None:
    """Desktop notification, so you know the bot let go of the mouse."""
    subprocess.run(["notify-send", "-a", "BTD bot", text], capture_output=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", default=ROOT / "strategies" / "dark_castle_chimps.yaml", type=Path)
    ap.add_argument("--games", type=int, default=1, help="games to play before stopping")
    ap.add_argument("--forever", action="store_true",
                    help="keep playing games until stopped (hotkey / Ctrl+C / STOP file)")
    ap.add_argument("--start", choices=["ingame", "menu"], default="ingame",
                    help="ingame = a fresh CHIMPS game is already open at round 6 (default)")
    ap.add_argument("--sample-until-round", type=int, metavar="N",
                    help="test mode: leave each game once round N is cleared (no retries), and "
                         "log where games are lost. Use with --forever for a sample of N-round runs")
    ap.add_argument("--from-step", type=int, default=0,
                    help="resume an in-progress game: treat strategy steps before N as done")
    ap.add_argument("--set-tiers", nargs="*", default=[], metavar="ID=T-M-B",
                    help="after --from-step, correct towers' real tiers, e.g. sniper1=0-1-0")
    ap.add_argument("--record", action="store_true",
                    help="search for a valid spot near each strategy position instead of stopping, "
                         "and save where towers landed to logs/placements_*.json "
                         "(apply with: python -m bot.tools layout-apply FILE --label 'real game')")
    ap.add_argument("--delay", type=int, default=10, metavar="SECONDS",
                    help="wait this long before touching the mouse, so you can set up your "
                         "screens (default 10; 0 to start at once)")
    ap.add_argument("--dry-run", action="store_true", help="log input instead of sending it")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    log = logging.getLogger("bot")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())

    stop_file = ROOT / "STOP"
    stop_file.unlink(missing_ok=True)

    inp = Input(cfg["display"], dry_run=args.dry_run)
    vision = Vision(ROOT / "templates", cfg["vision"], park=inp.park,
                    screen_px=tuple(cfg["display"]["screen_px"]),
                    refocus=lambda: inp.click(*cfg["game"]["safe_click_px"]),
                    output=cfg["display"].get("output"))
    make_strategy = lambda: Strategy.load(args.strategy, ROOT / "strategies" / "costs_hard.yaml",
                                          cfg["village"]["px_per_unit"])
    runner = Runner(cfg, inp, vision, make_strategy, ROOT / "logs", stop_file)
    runner.from_step = args.from_step
    runner.sample_until = args.sample_until_round
    runner.tier_overrides = dict(s.split("=", 1) for s in args.set_tiers)
    if args.record:
        runner.record = True
        # Colour readings are unreliable in crowded spots (that's why this mode exists), so
        # test every probe that isn't clearly valid with a real click: the game is the judge.
        runner.clearly_red_min = float("inf")

    # The stop hotkey (scripts/stop_bot.sh) sends SIGINT, which Python turns into
    # KeyboardInterrupt, exactly like Ctrl+C. Treat SIGTERM (plain `pkill`) the same way.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    # A process started in the background (`&`, scripts, launchers) inherits "ignore SIGINT"
    # from the shell, and Python keeps that, so the hotkey would do nothing. Re-enable it.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        for left in range(args.delay, 0, -1):
            log.info("starting in %d s…", left)
            time.sleep(1)
        runner.run(games=None if args.forever else args.games, start_in=args.start)
    except BotStopped as e:
        log.error("STOPPED at round %s: %s", runner.round, e)
        log.info("SUMMARY: %s", runner.summary())
        notify(f"BTD bot stopped: {runner.summary()}")
        return 1
    except KeyboardInterrupt:
        log.warning("stopped by hotkey / Ctrl+C at round %s", runner.round)
        log.info("SUMMARY: %s", runner.summary())
        notify(f"BTD bot stopped at round {runner.round}: {runner.summary()}")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
