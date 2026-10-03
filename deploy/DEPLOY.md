# 服务器部署指南（国内轻量云）

把本机（Mac）的 Clinical Trial Monitor 搬到国内云服务器，变成自己/小团队
随时可访问的网站。爬取规则（collectors/、core/waf_guard.py、爬取节奏）全部
留在服务端，浏览器侧只有一个纯只读的 SPA——**仓库保持 private 即可，对外
不暴露任何爬取经验**。

架构：

```
浏览器（任意设备）
   │  http://服务器IP/   ← Basic 认证弹窗（CT_AUTH_USER / CT_AUTH_PASSWORD）
   ▼
nginx :80（唯一对公网开放的端口）
   ▼ proxy
uvicorn 127.0.0.1:8000  ←  systemd: ct-monitor-api（run_monitor.py serve）
   ▼
SQLite /opt/ct-monitor/db/ct_monitor.db
   ▲
   └── systemd timers：每4h ChiCTR/CTR 增量 · 每日9:00 pipeline ·
       周六03:00 ICTRP 对账 · 每15min 监控 tick
```

## 0. 买什么

- 阿里云/腾讯云**轻量应用服务器**，2核4G 起（headless Chromium 过 WAF 吃内存），
  系统镜像 **Ubuntu 24.04 LTS**（自带 Python 3.12，`run_monitor.py serve`
  要求 ≥3.12）。流量套餐按默认即可。
- 地域选离你近的国内节点（出口 IP 在国内，爬 ChiCTR/CTR 最稳）。
- 控制台防火墙只放行 **22（SSH）和 80（HTTP）**，**8000 绝不放行**。

## 1. 服务器初始化（服务器上，一次性）

```bash
sudo timedatectl set-timezone Asia/Shanghai      # 定时器按本地时间跑
sudo apt update
sudo apt install -y python3.12-venv build-essential nginx rsync sqlite3 jq

sudo useradd -m -s /bin/bash ctmon || true       # 或直接用你自己的用户
sudo mkdir -p /opt/ct-monitor
sudo chown ctmon /opt/ct-monitor
```

## 2. 打包上传（Mac 上）

前端在本地构建好，服务器**不需要装 Node**：

```bash
cd /path/to/clinical-trial-monitor
(cd web && npm run build)          # 产出 web/dist，随代码一起上传

# ⚠️ --delete 会删掉服务器上被 .gitignore 排除的 deploy/ct-monitor.env
#    （真实口令文件只存在于服务器），所以必须显式 exclude 它。
rsync -av --delete \
  --exclude .git --exclude venv --exclude web/node_modules \
  --exclude 'db' --exclude 'data' --exclude logs --exclude backups \
  --exclude reports --exclude release --exclude design-proof \
  --exclude heart_valve --exclude m2_muscarinic_ab --exclude myocarditis \
  --exclude '__pycache__' --exclude '*.db*' \
  --exclude 'deploy/ct-monitor.env' \
  ./ ctmon@服务器IP:/opt/ct-monitor/
```

> 想改用 git：私有仓库 + deploy key，服务器 `git pull` 即可。GitHub 直连
> 间歇性超时是常态，失败重试即可；上面这条 rsync 是最稳的通道。

## 3. 数据库迁移（Mac → 服务器）

用项目自带的在线备份（WAL 安全、自包含单文件），不要直接拷
`ct_monitor.db` 本体（可能拷到写了一半的 WAL）：

```bash
# Mac 上
venv/bin/python run_monitor.py backup            # 打印出 backups/ 里的文件路径
rsync -av backups/最新那个文件.ctmon@服务器IP:/tmp/

# 服务器上
mkdir -p /opt/ct-monitor/db
cp /tmp/那个文件 /opt/ct-monitor/db/ct_monitor.db
```

## 4. Python 环境 + 过 WAF 浏览器（服务器上）

```bash
cd /opt/ct-monitor
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/playwright install --with-deps chromium
```

## 5. 配置口令（服务器上）

```bash
cp deploy/ct-monitor.env.example deploy/ct-monitor.env
chmod 600 deploy/ct-monitor.env
nano deploy/ct-monitor.env        # 填 CT_AUTH_PASSWORD（openssl rand -base64 18 生成）
```

**公网免登录演示站（可选）**：把 `CT_AUTH_USER` / `CT_AUTH_PASSWORD` 注释掉，
并设 `CT_PUBLIC_READONLY=1`，重启 `ct-monitor-api` 后任何人无需登录即可浏览；
所有写接口（含会触发爬取的 sync / retry / live-check）一律 403，仅放行
`/api/search/interpret` 和 `/api/saved-searches/{id}/open` 两个浏览辅助端点。
自用/内网部署不要开只读模式，保持 Basic 认证。

## 6. 启动（服务器上）

```bash
sudo bash deploy/install_systemd.sh
```

这一步会装 5 组 systemd 单元并全部启动：API 服务 + 4 个定时器
（每4h 增量爬取 / 每日 pipeline / 周度 ICTRP / 15min 监控 tick）。
排期与 Mac 上的 launchd 作业一一对应；卸载：`sudo deploy/install_systemd.sh --unload`。

## 7. Nginx 与防火墙（服务器上）

```bash
sudo cp deploy/nginx-ct-monitor.conf /etc/nginx/sites-available/ct-monitor.conf
sudo ln -sf /etc/nginx/sites-available/ct-monitor.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

云控制台确认：入方向只有 22/80。

## 8. 上线验证清单

```bash
# 服务器本机：
systemctl --no-pager status ct-monitor-api          # active (running)
curl -s http://127.0.0.1:8000/api/health | jq .status   # "ok"
curl -si http://127.0.0.1/api/health | head -1          # HTTP/1.1 200（走 nginx）
curl -si http://127.0.0.1/api/profiles | head -1        # HTTP/1.1 401（未带凭证被拦）
journalctl -u ct-monitor-crawl --no-pager | tail        # 定时器注册成功
```

浏览器（你的电脑上）：

1. 打开 `http://服务器IP/` → 弹出账号密码框 → 输入 env 里那组 → 看板正常出数据。
2. 新开无痕窗口直接访问 `http://服务器IP:8000/` → **应当连不上**（8000 未对公网开放）。
3. 首轮爬取预热：新 IP 对 WAF 是陌生访客，先小批量手动跑一次，
   确认熔断器不跳、记录正常入库，再交给每4小时的定时增量：

   ```bash
   cd /opt/ct-monitor && venv/bin/python scripts/crawl_waf_batch.py --source chictr --incremental --batch 5
   ```

## 9. 日常运维

- **看日志**：`journalctl -u ct-monitor-api -f`、`journalctl -u ct-monitor-crawl -f`。
- **更新代码**：Mac 上重新 rsync（第 2 步）→ 服务器 `sudo systemctl restart ct-monitor-api`。
  `run_monitor.py serve` 启动时会自动做迁移前备份 + 完整性审计。
- **备份**：每日 pipeline 自带数据库备份到 `backups/`；偶尔拉回 Mac 一份异地：
  `rsync -av ctmon@服务器IP:/opt/ct-monitor/backups/ ./server-backups/`。
- **爬取纪律不变**：单一出口 IP、小批量、脚本内置 flock 与熔断。想调节奏，
  改的是 `deploy/install_systemd.sh` 里 timer 的 OnCalendar 和 `--batch`，
  改完重跑安装脚本即可。

## 10. Mac 端收尾（重要）

服务器接管后，**本机的定时任务要停**，否则 Mac 和服务器各爬各的库、互相打架：

```bash
scripts/install_launchd.sh --unload      # 卸载 launchd 三个作业
```

另外把本机的每4小时增量自动化（ZCode 定时任务）暂停或删除。
Mac 回归纯开发机：本地跑测试、构建前端、改代码。
