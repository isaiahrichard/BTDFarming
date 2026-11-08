# Template candidates (4K, cropped from Screencast_20261107_203223.webm)

Bounding boxes are in the 3838x2158 capture frame: (x1, y1, x2, y2). Frame index = keyframe sample k (t ≈ 3.15 + 3k s).

| file | screen | box | frame | notes |
|---|---|---|---|---|
| home_play.png | main menu Play button | (1740, 1900, 2060, 2158) | k=210 | bottom edge cut by screen edge |
| mapselect_expert.png | map select, Expert tab | (2500, 1780, 2860, 2130) | k=211 | |
| mapselect_dark_castle.png | Dark Castle tile on Expert page | (1600, 840, 2320, 1480) | k=211 | tile shows save/medal badges — may differ with progress; consider matching only the "DARK CASTLE" title strip |
| chimps_intro_banner.png | "C.H.I.M.P.S." intro popup | (1700, 700, 2140, 850) | k=214 | shown on resume/start |
| chimps_intro_ok.png | OK on that popup | (1680, 1430, 2160, 1610) | k=214 | **cursor hand overlaps** — recrop from a clean screenshot |
| ingame_play.png | start-round button (single arrow), between rounds | (3565, 1935, 3765, 2095) | live 2026-11-07, game fullscreen | clean |
| ingame_fastforward.png | double-arrow button shown while a round is running | (3565, 1935, 3765, 2095) | live 2026-11-07 | clean — on/off state not yet distinguished |
| defeat_text.png | Defeat screen title | (1440, 560, 2400, 800) | k=170 | |
| defeat_retry_last_round.png | Defeat: Retry Last Round | (2440, 1490, 2720, 1880) | k=170 | |
| defeat_restart.png | Defeat: Restart | (1560, 1490, 1824, 1830) | k=170 | |
| defeat_home.png | Defeat: Home | (1120, 1490, 1376, 1830) | k=170 | |
| retry_confirm_ok.png | "Retry last round?" OK | (2024, 1370, 2536, 1540) | k=174 | **cursor hand overlaps** |
| victory_text.png | Victory title | (1000, 430, 2800, 820) | k=748 | |
| victory_next.png | Victory: Next button | (1680, 1715, 2160, 1910) | k=748 | same spot at k=749 |
| insta_monkey_banner.png | "Insta Monkey!" reward popup | (1340, 450, 2500, 610) | k=745 | appears after round 100 before Victory — click to dismiss |

Not in the video (user resumed a save): difficulty select (Hard), mode select (CHIMPS), "overwrite save?" dialog,
post-Victory Home/collect screen. Capture these with `spectacle -b -n -m` next time through the menus.
Note: the screencast includes the mouse cursor, so crops marked above should be retaken from cursor-free screenshots.
| ref_cash_220.png | cash display reading $220 (reference for digit OCR) | (690, 40, 920, 120) | live screenshot | clean |
