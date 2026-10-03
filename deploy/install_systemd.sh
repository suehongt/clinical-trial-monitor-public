#!/bin/bash
# Install systemd schedules + API service that mirror the macOS launchd jobs
# (scripts/install_launchd.sh).  Linux counterpart for the server deployment
# in deploy/DEPLOY.md.
#
# Five units are installed under /etc/systemd/system:
#   ct-monitor-api         run_monitor.py serve        (always-on, :8000 → nginx)
#   ct-monitor-crawl       ChiCTR+CTR 增量 batch 20    (timer: every 4h, ±15min)
#   ct-monitor-pipeline    daily 09:00 full pipeline   (timer: Persistent)
#   ct-monitor-ictrp       weekly Sat 03:00 --apply    (timer: Persistent)
#   ct-monitor-monitors    tick-monitors               (timer: every 15 min)
#
# Usage:
#   sudo deploy/install_systemd.sh            install, enable, start
#   sudo deploy/install_systemd.sh --unload   stop, disable, remove all units
#
# Layout assumed (see deploy/DEPLOY.md): app at /opt/ct-monitor, config at
# /opt/ct-monitor/deploy/ct-monitor.env.  Override with CT_DEPLOY_DIR=...
set -euo pipefail

APP_DIR="${CT_DEPLOY_DIR:-/opt/ct-monitor}"
ENV_FILE="$APP_DIR/deploy/ct-monitor.env"
UNIT_DIR=/etc/systemd/system

if [[ "$(id -u)" -ne 0 ]]; then
  echo "error: run with sudo (system units need root)" >&2
  exit 1
fi
RUN_AS="${SUDO_USER:-root}"
if [[ "$RUN_AS" == "root" ]]; then
  echo "warning: units will run as root — prefer: sudo -u '#1000' ... or create a deploy user" >&2
fi
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  echo "error: $APP_DIR/venv/bin/python not found (run the DEPLOY.md install steps first)" >&2
  exit 1
fi

unload() {
  for name in ct-monitor-crawl ct-monitor-pipeline ct-monitor-ictrp ct-monitor-monitors; do
    systemctl disable --now "$name.timer" 2>/dev/null || true
    rm -f "$UNIT_DIR/$name.service" "$UNIT_DIR/$name.timer"
  done
  systemctl disable --now ct-monitor-api.service 2>/dev/null || true
  rm -f "$UNIT_DIR/ct-monitor-api.service"
  systemctl daemon-reload
  echo "ct-monitor units removed."
  exit 0
}

[[ "${1:-}" == "--unload" ]] && unload

common_env() {
  printf 'EnvironmentFile=-%s\nWorkingDirectory=%s\nUser=%s\nSyslogIdentifier=ct-monitor-%s\n' \
    "$ENV_FILE" "$APP_DIR" "$RUN_AS" "$1"
}

# ── API + SPA (nginx proxies 127.0.0.1:8000) ────────────────────────────
cat > "$UNIT_DIR/ct-monitor-api.service" <<EOF
[Unit]
Description=Clinical Trial Monitor API + SPA (run_monitor serve)
After=network-online.target
Wants=network-online.target

[Service]
$(common_env api)
ExecStart=$APP_DIR/venv/bin/python run_monitor.py serve
Restart=on-failure
RestartSec=5
TimeoutStartSec=300
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR/db $APP_DIR/data $APP_DIR/logs $APP_DIR/backups

[Install]
WantedBy=multi-user.target
EOF

# ── ChiCTR/CTR 增量爬取（每 4 小时，±15 分钟抖动更接近人节奏）────────────
# 红线提醒（项目内部 WAF 红线说明）：单一出口 IP、小批量、脚本内置 flock 防重入；
# 节奏参数改动前先读 scripts/crawl_waf_batch.py 头部注释。
cat > "$UNIT_DIR/ct-monitor-crawl.service" <<EOF
[Unit]
Description=Clinical Trial Monitor ChiCTR/CTR incremental WAF batch

[Service]
$(common_env crawl)
Type=oneshot
ExecStart=$APP_DIR/venv/bin/python scripts/crawl_waf_batch.py --source chictr --incremental --batch 20
ExecStart=$APP_DIR/venv/bin/python scripts/crawl_waf_batch.py --source ctr --incremental --batch 20
TimeoutStartSec=4h
PrivateTmp=true
EOF

cat > "$UNIT_DIR/ct-monitor-crawl.timer" <<EOF
[Unit]
Description=ChiCTR/CTR incremental crawl every 4 hours

[Timer]
OnCalendar=*-*-* 0/4:00:00
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── 每日全量 pipeline（与 launchd com.ct-monitor.pipeline 对齐）──────────
cat > "$UNIT_DIR/ct-monitor-pipeline.service" <<EOF
[Unit]
Description=Clinical Trial Monitor daily pipeline (all profiles, full)

[Service]
$(common_env pipeline)
Type=oneshot
ExecStart=$APP_DIR/venv/bin/python run_monitor.py pipeline --profiles all --english --full
TimeoutStartSec=6h
PrivateTmp=true
EOF

cat > "$UNIT_DIR/ct-monitor-pipeline.timer" <<EOF
[Unit]
Description=Daily pipeline at 09:00 local time

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── ICTRP 周对账（与 launchd com.ct-monitor.ictrp 对齐）─────────────────
cat > "$UNIT_DIR/ct-monitor-ictrp.service" <<EOF
[Unit]
Description=Clinical Trial Monitor weekly ICTRP reconciliation

[Service]
$(common_env ictrp)
Type=oneshot
ExecStart=$APP_DIR/venv/bin/python scripts/ictrp_weekly.py --apply
TimeoutStartSec=4h
PrivateTmp=true
EOF

cat > "$UNIT_DIR/ct-monitor-ictrp.timer" <<EOF
[Unit]
Description=Weekly ICTRP reconciliation, Saturdays 03:00

[Timer]
OnCalendar=Sat *-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── 监控计划任务 tick（与 launchd com.ct-monitor.monitors 对齐）──────────
cat > "$UNIT_DIR/ct-monitor-monitors.service" <<EOF
[Unit]
Description=Clinical Trial Monitor due-monitor tick

[Service]
$(common_env monitors)
Type=oneshot
ExecStart=$APP_DIR/venv/bin/python run_monitor.py tick-monitors
TimeoutStartSec=1h
PrivateTmp=true
EOF

cat > "$UNIT_DIR/ct-monitor-monitors.timer" <<EOF
[Unit]
Description=Tick due monitors every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now ct-monitor-api.service
for name in crawl pipeline ictrp monitors; do
  systemctl enable --now "ct-monitor-$name.timer"
done

echo "installed. status:"
systemctl --no-pager --lines=0 status ct-monitor-api.service || true
systemctl --no-pager list-timers 'ct-monitor-*'
