#!/bin/bash
# Install launchd schedules that replace the ZCode workspace automations
# (the project design notes §7 — 智能体退场).
#
# Three user agents are installed:
#   com.ct-monitor.pipeline   daily 09:00       pipeline --profiles all --english --full
#   com.ct-monitor.ictrp      weekly Sat 03:00  scripts/ictrp_weekly.py
#   com.ct-monitor.monitors   every 15 min      run_monitor.py tick-monitors
#
# Deliberately NOT scheduled (WAF red line): ChiCTR/CTR
# `crawl --refresh-only` stays a manual off-peak routine — one round per
# source per night, run by hand.
#
# Usage:
#   scripts/install_launchd.sh          install + load all jobs
#   scripts/install_launchd.sh --unload unload and remove all jobs
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_ROOT/venv/bin/python"
LABELS=(com.ct-monitor.pipeline com.ct-monitor.ictrp com.ct-monitor.monitors)

if [[ ! -x "$PYTHON" ]]; then
  echo "error: $PYTHON not found (create the venv first)" >&2
  exit 1
fi

# TCC: launchd agents have no Terminal grant for ~/Downloads, so the venv
# stub (venv/bin/python -> CommandLineTools python3) dies with EPERM on
# venv/pyvenv.cfg. Launch the resolved framework interpreter directly —
# the same binary the production server already runs under launchd — with
# the venv site-packages on PYTHONPATH, so pyvenv.cfg is never touched.
PYSITE="$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
PYBIN="$("$PYTHON" -c '
import os, sys
p = os.path.realpath(sys.executable)
app = os.path.join(os.path.dirname(p), os.pardir, "Resources", "Python.app", "Contents", "MacOS", "Python")
print(os.path.normpath(app) if os.path.exists(app) else p)')"
if [[ ! -x "$PYBIN" ]]; then
  echo "error: resolved interpreter $PYBIN not executable" >&2
  exit 1
fi

unload() {
  for label in "${LABELS[@]}"; do
    local plist="$HOME/Library/LaunchAgents/$label.plist"
    if launchctl unload "$plist" 2>/dev/null; then
      echo "unloaded $label"
    fi
    rm -f "$plist"
  done
}

[[ "${1:-}" == "--unload" ]] && { unload; echo "launchd jobs removed."; exit 0; }

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_ROOT/logs"

unload  # idempotent reinstalls: clear old jobs BEFORE writing fresh plists

cat > "$HOME/Library/LaunchAgents/com.ct-monitor.pipeline.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.ct-monitor.pipeline</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYBIN</string>
        <string>$PROJECT_ROOT/run_monitor.py</string>
        <string>pipeline</string>
        <string>--profiles</string><string>all</string>
        <string>--english</string>
        <string>--full</string>
    </array>
    <key>WorkingDirectory</key><string>$PROJECT_ROOT</string>
    <key>EnvironmentVariables</key>
    <dict><key>PYTHONPATH</key><string>$PYSITE</string></dict>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <key>StandardOutPath</key><string>$PROJECT_ROOT/logs/launchd_pipeline.log</string>
    <key>StandardErrorPath</key><string>$PROJECT_ROOT/logs/launchd_pipeline.log</string>
</dict>
</plist>
EOF

cat > "$HOME/Library/LaunchAgents/com.ct-monitor.ictrp.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.ct-monitor.ictrp</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYBIN</string>
        <string>$PROJECT_ROOT/scripts/ictrp_weekly.py</string>
        <string>--apply</string>
    </array>
    <key>WorkingDirectory</key><string>$PROJECT_ROOT</string>
    <key>EnvironmentVariables</key>
    <dict><key>PYTHONPATH</key><string>$PYSITE</string></dict>
    <key>StartCalendarInterval</key>
    <dict><key>Weekday</key><integer>6</integer><key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer></dict>
    <key>StandardOutPath</key><string>$PROJECT_ROOT/logs/launchd_ictrp.log</string>
    <key>StandardErrorPath</key><string>$PROJECT_ROOT/logs/launchd_ictrp.log</string>
</dict>
</plist>
EOF

# Weekly job is a no-op dry-run without --apply (see scripts/ictrp_weekly.py),
# hence the explicit --apply argument above.

# Topic-monitor scheduler tick: cheap no-op when nothing is due (the tick
# claims due occurrences durably, so overlapping/missed ticks are safe).
# RunAtLoad covers "just enabled a schedule" right after install/boot;
# launchd coalesces StartInterval fires missed while asleep into one.
cat > "$HOME/Library/LaunchAgents/com.ct-monitor.monitors.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.ct-monitor.monitors</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYBIN</string>
        <string>$PROJECT_ROOT/run_monitor.py</string>
        <string>tick-monitors</string>
    </array>
    <key>WorkingDirectory</key><string>$PROJECT_ROOT</string>
    <key>EnvironmentVariables</key>
    <dict><key>PYTHONPATH</key><string>$PYSITE</string></dict>
    <key>RunAtLoad</key><true/>
    <key>StartInterval</key><integer>900</integer>
    <key>StandardOutPath</key><string>$PROJECT_ROOT/logs/launchd_monitors.log</string>
    <key>StandardErrorPath</key><string>$PROJECT_ROOT/logs/launchd_monitors.log</string>
</dict>
</plist>
EOF

for label in "${LABELS[@]}"; do
  launchctl load "$HOME/Library/LaunchAgents/$label.plist"
  echo "loaded $label"
done

echo
echo "Scheduled (replace the ZCode workspace automation):"
echo "  com.ct-monitor.pipeline   daily 09:00  pipeline --profiles all --english --full"
echo "  com.ct-monitor.ictrp      Sat 03:00    scripts/ictrp_weekly.py"
echo "  com.ct-monitor.monitors   every 15 min run_monitor.py tick-monitors"
echo "Manual-only (WAF red line): crawl --refresh-only (ChiCTR/CTR, off-peak)"
echo
echo "Kickstart a job manually:  launchctl start com.ct-monitor.pipeline"
