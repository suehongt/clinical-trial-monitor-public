#!/bin/bash
# 一键启动局域网演示服务:局域网内任何设备可访问。
# 默认免认证(服务端仅 CT_AUTH_USER+CT_AUTH_PASSWORD 同时设置才开 Basic 认证)。
# 需要认证时,启动前导出: CT_AUTH_USER=账号 CT_AUTH_PASSWORD=密码 ./scripts/start_demo.sh
# 用法: ./scripts/start_demo.sh   (Ctrl+C 停止)
# 注意: 8000 是日常本地服务,本脚本用 8080,互不影响;8765 是纯静态旧服务,无 API,勿用于演示。
set -e
cd "$(dirname "$0")/.."

IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 || echo 127.0.0.1)

echo "──────────────────────────────────────────────"
echo "  演示地址(同一 WiFi 的手机/电脑可开):"
echo "    http://${IP}:8080"
echo "  本机演示:"
echo "    http://localhost:8080"
if [ -n "$CT_AUTH_USER" ] && [ -n "$CT_AUTH_PASSWORD" ]; then
    echo "  认证: 已开启(账号 ${CT_AUTH_USER})"
    AUTH_ARGS=(CT_AUTH_USER="$CT_AUTH_USER" CT_AUTH_PASSWORD="$CT_AUTH_PASSWORD")
else
    echo "  认证: 免认证(未设置 CT_AUTH_USER/CT_AUTH_PASSWORD)"
    AUTH_ARGS=()
fi
echo "──────────────────────────────────────────────"

exec env CT_HOST=0.0.0.0 CT_PORT=8080 "${AUTH_ARGS[@]}" \
    ./venv/bin/python -m server
