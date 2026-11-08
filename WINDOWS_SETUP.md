# Porting the BTD6 bot to Windows

This bot was built and tested on **Fedora 44, KDE Plasma (Wayland), one 4K monitor at 1.65×
scaling**. This guide is for whoever sets it up on a **Windows** PC with a monitor of unknown
size. It explains what is Linux-specific, what can be reused as-is, and how to add Windows
support **without changing how the Linux version behaves**.

> Rule for the port: the Linux code path must keep working exactly as now. Add a Windows
> backend next to it and select it with `sys.platform`; don't rewrite the Linux one.

---

## 1. How the bot is laid out

```
config.yaml                     shared settings: hotkeys, thresholds, menu flow, display (Linux values)
strategies/dark_castle_chimps.yaml  build order + tower positions (4K pixels)
strategies/costs_hard.yaml      Hard prices + village discount rules
templates/                      ~35 button/text crops at 4K, regions.yaml, glyphs/ (cash digits)
captures/                       saved 4K screenshots used by the tests (keep these)
bot/input.py      HANDS   — the only file that sends mouse/keyboard input      ← Linux-specific
bot/vision.py     EYES    — screenshots + template matching + reading numbers  ← screenshot() is Linux-specific
bot/strategy.py   PLAN    — pure logic, no OS calls                            ← reuse as-is
bot/runner.py     BRAIN   — state machine, placement, upgrades, menus          ← reuse as-is
bot/__main__.py   entry point (`python -m bot`)                                ← tiny Linux bits (notify-send)
bot/tools.py      helpers: screenshot, crop, glyphs, ocr, layout (sandbox), layout-apply
scripts/stop_bot.sh  Linux stop hotkey script
tests/            27 pytest tests, run on saved screenshots — no game needed
```

**Everything in the bot works in 4K pixel coordinates (3840×2160).** Templates were cropped
at 4K; tower positions, the upgrade-panel tier boxes, the cash-reading band, placement and
selection offsets, the village range (9.7 px per game unit) are all 4K numbers.

## 2. What is Linux-only (and what replaces it)

| Piece | Linux implementation | Windows replacement |
|---|---|---|
| Mouse + keys | `ydotool` via a user systemd service, Linux keycodes (`KEYCODES` in `input.py`) | Win32 `SendInput` — e.g. the `pydirectinput` package (sends **scan codes**, which Unity games like BTD6 read reliably) |
| Mouse positioning | Wayland has no absolute positioning: the bot pins the cursor to the 4K monitor's top-right corner (`home_move`), then moves relatively, dividing by the 1.65 scale. Needs **Flat** pointer acceleration. | Not needed. Windows supports absolute positioning: move straight to the pixel. Make the process **DPI-aware** first (see §4) or coordinates get scaled wrongly. |
| Screenshots | `spectacle -b -n -m` (KDE), captures the monitor with the focused window; occasionally writes no file, hence the retry loop | `mss` (fast, multi-monitor). Capture the game monitor's rectangle directly. |
| Monitor info | read by hand from `kscreen-doctor` into `config.yaml: display` | `mss().monitors` gives each monitor's left/top/width/height in physical pixels (once DPI-aware) |
| Stop hotkey | `scripts/stop_bot.sh` bound to a KDE global shortcut → sends SIGINT | A global hotkey inside the bot, e.g. the `keyboard` package: `keyboard.add_hotkey("ctrl+shift+b", ...)` that sets a flag / raises in the main thread. On Windows, Ctrl+C only reaches the console when it has focus — same problem as on Linux. |
| Notification | `notify-send` | optional; `print` or skip (guard with `sys.platform`) |
| Cursor parking (`Input.park`) | moves to the top-right anchor so the game's hand cursor isn't in screenshots | move the cursor to a corner of the game monitor that has no UI (top-right edge works on BTD6). **Do not park over the shop panel while holding a tower** — `runner.py` relies on parking over the shop to *drop* a held tower. |

Packages to add on Windows (don't add them to the Linux `requirements.txt`; use a separate
`requirements-windows.txt`): `mss`, `pydirectinput`, `keyboard`.

## 3. Screen size: the "reference space" approach

Don't re-capture templates or re-measure positions. Instead, make 3840×2160 a **reference space**:

1. **Screenshots** (`Vision.screenshot` on Windows): capture the game monitor, then
   `cv2.resize(img, (3840, 2160))`. All templates, positions and constants then work unchanged.
2. **Clicks** (`Input.move_to` / `click` on Windows): convert reference → physical:
   `x = mon_left + px * mon_width / 3840`, `y = mon_top + py * mon_height / 2160`.
3. **Relative moves** (`Input.move_by`, used while holding a tower so the cursor never crosses
   the shop) take reference-pixel deltas: scale them the same way.

This only works if BTD6 lays out its screen identically, which it does for **16:9** monitors
(1920×1080, 2560×1440, 3840×2160, …): the whole UI scales uniformly.

**The monitor's aspect ratio is unknown, so check it first:**
- `width / height == 16/9` (± 0.01) → reference space is all you need.
- Anything else (16:10, 21:9 ultrawide): BTD6 places the map and HUD differently. You'll need to
  - re-run the **sandbox layout pass** (`python -m bot.tools layout`, then `layout-apply`) for tower positions,
  - re-capture the menu/HUD templates (`bot.tools screenshot` + `bot.tools crop`), and
  - re-measure the hard-coded panel/HUD coordinates listed in §6.
  Simplest alternative: run BTD6 **windowed or fullscreen at a 16:9 resolution** (e.g. 1920×1080)
  on that monitor, so the game image is 16:9 even if the monitor isn't.

Expect slightly lower template scores when upscaling from 1080p (blurrier). If something
stops matching, lower `vision.match_threshold` a little (0.85 → 0.8) **on that machine only**,
and re-check `tests/`.

## 4. Windows gotchas

- **DPI awareness first.** Call `ctypes.windll.shcore.SetProcessDpiAwareness(2)` before any
  screenshot or mouse call. Without it, at 125%/150% Windows scaling the screenshots and the
  click coordinates disagree — the Windows version of the 1.65 problem we fixed on Linux.
- **Fullscreen.** Use BTD6's fullscreen/borderless setting so nothing (taskbar, other windows)
  covers the game. On Linux the KDE panel once covered the play button.
- **Focus.** Input goes to the focused window. The bot starts with one "focus click" on empty
  ground (`game.safe_click_px`, or `game.home_focus_px` on the home screen).
- **Admin rights.** If BTD6 runs as administrator, input from a non-admin process is blocked
  (Windows UIPI). Run both normally.
- **`pydirectinput` needs scan codes,** not virtual keys — it handles that; just pass `"g"`, `"q"`, etc.
- Clicking: BTD6 placement needs press-and-release *at the same spot*; a 50–100 ms hold between
  `mouseDown` and `mouseUp` is safest.

## 5. Suggested implementation order

1. Copy the project folder **except** `.venv/`, `logs/` and the two `Screencast_*.webm` videos
   (≈10 GB, not needed). Keep `captures/` — the tests use it.
2. Install Python 3.12+ from python.org, then in the project folder:
   ```
   py -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\pip install mss pydirectinput keyboard
   .venv\Scripts\python -m pytest -q        # should pass: tests don't touch the OS
   ```
3. Add the Windows backends, selected by `sys.platform == "win32"`:
   - `bot/input.py` → a `WindowsInput` class with the same methods: `home`, `park`,
     `move_to`, `move_by` (returns the physical distance moved, in reference px),
     `click`, `click_here`, `key(name, times)`, and `to_relative` if anything still uses it.
   - `bot/vision.py` → make `screenshot(park=True)` use `mss` + resize on Windows.
   - `bot/__main__.py` → build the right input class; register the global stop hotkey;
     skip `notify-send`.
   - Put the machine's monitor values in a **machine-specific** file (e.g. `machine.yaml`,
     gitignored) rather than editing `config.yaml: display`, which holds the Linux values.
4. Write `bot/calibrate.py` (Windows): pick the monitor BTD6 is on, check 16:9, save
   `machine.yaml`, then **verify**: with BTD6 on the home screen, take a screenshot and check
   `vision.find("home_play")` matches; move the mouse to a known button and confirm it lands
   there (BTD6 draws its own hand cursor into screenshots, so you can find it by diffing a
   screenshot with the cursor moved away).
5. Write `setup.py` (Windows): venv + packages, run calibration, ask for the BTD6 hotkeys and
   write them to `machine.yaml`, then walk through the game-settings checklist (§7).
6. Test in this order: `pytest` → `python -m bot --dry-run` → one placement in **Sandbox**
   (`python -m bot.tools layout` on an empty sandbox board) → a full CHIMPS game.

## 6. Hard-coded 4K numbers to know about

With reference-space scaling these all keep working. They matter only for a non-16:9 layout.

| Where | What |
|---|---|
| `config.yaml: game` | `placement_pointer_offset_px` (tower drawn 110 px above the pointer while dragging), `select_offset_px` (click 65 px below a tower's saved spot), `safe_click_px`, `home_focus_px`, `hero_tile_px` |
| `config.yaml: vision` | `cash_band`, `round_band` (where the HUD numbers are) |
| `config.yaml: village.px_per_unit` | 9.7 — measured from in-game prices, not a guess |
| `templates/regions.yaml` | search box for every template |
| `bot/vision.py` | `PANEL_PIP_X / PANEL_ROW_T1_Y / PANEL_PIP_DY / PANEL_SELL_BOX` (upgrade panel tier boxes), `ring_redness` / `placement_redness` radii (120–200 px) |
| `bot/runner.py` | dismiss-popup click at (1920, 1300) |
| `strategies/dark_castle_chimps.yaml` | `at_px` for all 15 towers (from the sandbox layout pass) |

## 7. In-game settings checklist (both machines)

- **Hotkeys** (Settings → Hotkeys): towers — Sniper `z`, Sub `x`, Alchemist `f`, Druid `g`,
  Skywarden `s`, Village `k`, Glue Gunner `y`, Hero `u`; upgrades — top `q`, middle `w`,
  bottom `e`. Different keys are fine: put them in the machine config instead.
- **Auto Start: OFF** (gear icon in a game). The bot buys between rounds, then presses play.
- **Placement mode: Drag & Drop** (that's what the 110 px placement offset was measured with).
- **Fullscreen**, on the monitor you calibrated.
- **Hero: Obyn Greenfoot** selected.
- **Start the bot from the home screen** with no saved Dark Castle game. The "overwrite save?"
  dialog was never captured.

## 8. Lessons from the Linux build that will save time

- **Verify by reading the game, not by cash.** Late-game income hides purchases. Upgrades are
  verified by the green tier boxes in the upgrade panel; placements by the red ✕ cancel button
  disappearing. Fast-forward is verified by its glowing state.
- **Clicking a selected tower deselects it.** Always click empty ground first (`select_tower`).
- **The game draws the range circle under towers.** Placement validity compares a screenshot
  before and after picking the tower up, only on pixels that changed.
- **Expect popups:** Level Up, Monkey Knowledge Point, Insta Monkey, Collection Event. They're
  all in `Runner.HOME_ROUTE` / the main loop, and there's a 30 s "unknown screen" fallback.
- **Stopping must work while the bot owns the mouse.** Build the global stop hotkey first.

## 9. Running it

```
.venv\Scripts\python -m bot --start menu --games 10 -v
```
`-v` logs every template score. `logs\bot.log` keeps the history, and `logs\*.png` holds a
screenshot for every failure. Resume a stopped game with `--from-step N` (and `--set-tiers id=T-M-B`
if a tower's tiers differ).
