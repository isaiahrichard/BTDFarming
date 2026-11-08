#!/bin/bash
# Stop the BTD bot from anywhere. Bind this to a global keyboard shortcut in KDE
# (System Settings -> Keyboard -> Shortcuts -> Add New -> Command or Script).
# SIGINT = the same signal Ctrl+C sends: the bot stops at once and logs where it was.
# Anchored pattern: only the bot process itself, not shells/editors whose command line
# merely mentions it.
if pkill -INT -f "^[^ ]*python[0-9.]* -m bot( |$)"; then
    notify-send -a "BTD bot" "Stopping the bot…"
else
    notify-send -a "BTD bot" "No bot is running"
fi
