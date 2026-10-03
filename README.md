# Global Clinical Trial Monitor | 全球临床试验持续监测系统

[![CI](https://github.com/suehongt/clinical-trial-monitor-public/actions/workflows/ci.yml/badge.svg)](https://github.com/suehongt/clinical-trial-monitor-public/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-All%20Rights%20Reserved-gray)](#license)

**English** | [中文](#中文文档)

---

## Overview

A continuous monitoring system for global clinical trial registries. It supports seven registry sources, resolves the same trial across platforms into a single *master trial*, detects field-level changes between crawl cycles, and produces incremental cross-source reports.

- **Multi-registry collection** — ClinicalTrials.gov, EU CTIS and ISRCTN (public APIs), legacy EUCTR (historical backfill), ChiCTR & CTR/NMPA (browser automation), WHO ICTRP (XML snapshot)
- **Cross-source entity resolution** — identifier matching (AUTO_CONFIRMED) + FTS-assisted title similarity (review queue)
- **Change detection** — immutable version chains, field-level diff, event-hash deduplication
- **Disease profiles** — switchable retrieval scope per disease (built-in: myocardial infarction, heart failure, cardiomyopathy, multiple myeloma) with isolated incremental baselines
- **Three output surfaces** — self-contained HTML report, static web viewer (GitHub Pages), and the self-hosted web platform (live API + SPA, `python -m server`)
- **One research workspace UI (1.10.0)** — every route shares one design system, routes load on demand, bilingual labels, light/dark themes, WCAG-AA-verified contrast

## Data Sources

| Registry | Code | Method | Notes |
|:---------|:-----|:-------|:------|
| ClinicalTrials.gov | NCT | REST API v2 | condition-scoped full & incremental retrieval |
| 中国药物临床试验登记与信息公示平台 (CDE/NMPA) | CTR | Playwright + HTML parsing | Wangsu WAF handled per fresh browser context |
| 中国临床试验注册中心 (ChiCTR) | ChiCTR | Playwright + HTML parsing | Alibaba Cloud WAF, same approach |
| WHO ICTRP | ICTRP | XML snapshot import | AGGREGATOR — never counted as independent discoveries |
| EU Clinical Trials Information System | CTIS | Public JSON API | Direct primary records; disabled by default until explicitly enabled |
| ISRCTN Registry | ISRCTN | Public XML API | Direct primary records; disabled by default until explicitly enabled |
| EU Clinical Trials Register | EUCTR | Public text endpoints | Historical/manual backfill only; one preferred country protocol per trial |

## Installation

Requires **Python 3.12+**. Playwright's Chromium is only needed for the ChiCTR / CTR collectors. The prebuilt release zip (see [Releases](https://github.com/suehongt/clinical-trial-monitor-public/releases)) already contains built frontend assets, so Node.js is only required when building from source.

### macOS / Linux

```bash
# 0. Enter the project directory — the folder that contains requirements.txt
cd path/to/clinical-trial-monitor

# 1. Virtual environment + Python dependencies
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 2. Browser kernel — only needed for the ChiCTR / CTR collectors
venv/bin/playwright install chromium
```

### Windows (PowerShell)

```powershell
# 0. Enter the project directory — the folder that contains requirements.txt.
#    A fresh PowerShell starts in your home folder (C:\Users\<you>);
#    without cd first, pip fails with "No such file or directory: requirements.txt"
cd C:\path\to\clinical-trial-monitor

# 1. Virtual environment + Python dependencies
python -m venv venv
venv\Scripts\pip install -r requirements.txt

# 2. Browser kernel — only needed for the ChiCTR / CTR collectors
venv\Scripts\playwright install chromium
```

> **Platform notes.** The only path difference is `venv/bin/` (macOS/Linux) vs `venv\Scripts\` (Windows) — substitute accordingly in every command below. `./start.sh` is a bash script; on Windows either use Git Bash or run `cd web && npm run build` once and then `venv\Scripts\python -m server`. Scheduled ingestion uses launchd on macOS (`scripts/install_launchd.sh`) and systemd on Linux (`deploy/install_systemd.sh`); on Windows run the crawl/monitor commands manually or register them in Task Scheduler. `scripts/crawl_waf_batch.py` relies on POSIX file locking (`fcntl`) and must run on macOS/Linux. See [docs/INSTALL.md](docs/INSTALL.md) for the production install flow and [deploy/DEPLOY.md](deploy/DEPLOY.md) for the server (Linux + nginx + Basic-auth) deployment.

## Demo

![Demo preview](docs/assets/demo-teaser.gif)

A ~1 minute walkthrough (fixture data): dashboard attention view → bilingual search → trial change timeline → monitors & runs → project notebook → research briefing → data sources → dark mode / 中文. Watch the full video: [docs/assets/demo-video.mp4](docs/assets/demo-video.mp4) (also attached to the [Releases](releases)).

## Quick Start

```bash
# 1. Initialise the database
python run_monitor.py init

# 2. Enable a source
python run_monitor.py enable clinicaltrials_gov

# 3. Crawl
python run_monitor.py crawl                            # all enabled sources
python run_monitor.py crawl --source clinicaltrials_gov
python run_monitor.py crawl --incremental              # records updated since last sync

# 4. Entity resolution — link records to master trials
python run_monitor.py resolve

# 5. Inspect unacknowledged changes / DB stats
python run_monitor.py changes
python run_monitor.py stats

# 6. Daily / weekly summary reports
python run_monitor.py report --type daily
```

## Disease Profiles

Retrieval scope is defined per disease in `config.DISEASE_PROFILES` and selected with the `CT_DISEASE_PROFILE` environment variable. Each profile keeps its own incremental report baseline, so diseases never overwrite each other's state.

```bash
# Myocardial infarction (default profile)
python3 scripts/trial_report.py --english            # incremental: new/changed only
python3 scripts/trial_report.py --english --force    # full rebuild

# Multiple myeloma
CT_DISEASE_PROFILE=mm venv/bin/python run_monitor.py crawl --source clinicaltrials_gov
CT_DISEASE_PROFILE=mm venv/bin/python scripts/trial_report.py --english --force

# Chinese report (DeepSeek translation; set DEEPSEEK_API_KEY,
# ANTHROPIC_AUTH_TOKEN still accepted as a fallback)
python3 scripts/trial_report.py

# Ad-hoc keywords without switching profile
python3 scripts/trial_report.py --english --force -k "breast cancer" 乳腺癌
```

## Web Viewer & Web Platform

A React 18 + TypeScript + Vite single-page app under `web/`, redesigned (release 1.10.0) into one research-workspace system: unified shell and components, on-demand route loading, paired English/Chinese labels, light/dark themes, and WCAG-AA-verified contrast. Cards open a detail modal (purpose, investigator, endpoints, inclusion/exclusion criteria):

```bash
# 1. Export the current disease profile's data
CT_DISEASE_PROFILE=mm venv/bin/python scripts/export_report_json.py --profile mm

# 2. Build / preview locally
cd web && npm install && npm run build && npm run preview

# 3. Push to main — CI builds and deploys GitHub Pages automatically
```

### Self-hosted web platform (Phase 7 P1 — live queries, no static export)

**One supported full-stack launch command (Phase 3E):**

```bash
./start.sh              # builds the SPA if needed, serves API + app on http://127.0.0.1:8000
./start.sh --dev        # HMR dev server on :5173 (proxied) + API on :8000
./start.sh --rebuild    # force a fresh frontend build
```

Both API and frontend are served from the same origin, so every page — Dashboard, Trials, Monitors, Updates — reaches the monitoring backend. The SPA verifies backend health explicitly (`GET /api/health`, including database state and schema version); when the backend is offline trial browsing still works over the static export and monitoring pages show a recovery banner instead of a dead end. Manual path (equivalent):

```bash
cd web && npm run build                       # build the SPA once
venv/bin/python -m server                     # serve API + SPA on http://127.0.0.1:8000
CT_PORT=9000 venv/bin/python -m server        # custom port
```

- Navigation (Phase 3E): **Dashboard · Trials · Monitors · Updates** — the landing dashboard summarizes new/updated trials, important changes and watched-trial activity (`GET /api/dashboard`); watched trials live under Trials → Watched; changes/notifications/digest live under Updates; cross-source mirrors moved into Trial detail → Sources
- Trial detail (`/trials/{source}/{id}`) has Overview / Changes / History / Sources tabs: change history with human-readable labels, old → new values and severity; version timeline with any-pair comparison; structured site table; deep links are directly reloadable
- Endpoints: `/api/trials`, `/api/trials/{source}/{id}` (detail + change history), `/api/trials/{source}/{id}/mirrors`, `/api/mirrors`, `/api/events`, `/api/dashboard`, `/api/stats`, `/api/profiles`, `/api/health`; interactive docs at `/api/docs`
- ChiCTR local adapter (unofficial, read-only): `GET /api/chictr/search?q=heart&page=1` and `GET /api/chictr/studies/ChiCTR2600128415`. It reuses the collector's HTML parser, serializes outbound access, waits at least two seconds between uncached requests, caches responses for five minutes, and stops on the shared WAF circuit breaker. It does not write search hits to the discovery queue or bypass CAPTCHA/access controls.
- China Drug Trials/CTR local adapter (unofficial, read-only): `GET /api/ctr/search?q=heart+failure&page=1` and `GET /api/ctr/studies/CTR20262758`. It reuses the CDE HTML parser, supports `keywords`/`indication` field routing, serializes outbound access with a five-second minimum interval, caches responses for five minutes, and shares the WAF circuit breaker. Full live-check ingestion remains the write path that preserves the complete source HTML.
- WHO ICTRP adapters (read-only): `GET /api/ictrp/search?q=troponin&page=1` drives the public ASP.NET search form and returns current lightweight list metadata; `GET /api/ictrp/studies/{registry_id}` reads the latest record from the local mirror without contacting WHO, returns the preserved XML payload, and explicitly labels portal-only placeholders as incomplete. The weekly XML snapshot remains the full-detail ingestion source.
- Every response carries `data_as_of` (per-source last successful sync) — the UI stamps it as "Data as of"
- Browser E2E suite (real Chromium + FastAPI + built SPA): `CT_SKIP_WEB_BUILD=1 venv/bin/python -m pytest tests/test_e2e_browser.py -m e2e`
- Scheduled runs replace agent automations: `scripts/install_launchd.sh` installs the daily pipeline + weekly ICTRP jobs (ChiCTR/CTR refresh stays manual, off-peak)

## Core Design Principles

1. **Raw records are immutable** — original registry payloads are never modified or deleted
2. **Deduplication by linkage, not deletion** — cross-registry mirrors attach to the same `master_trial` via `record_master_map`
3. **ICTRP is an aggregator** — its records link back to primary sources and are excluded from discovery counts
4. **Version chains** — every detected change creates a new record version (`is_latest` flips); change events are deduplicated by `event_hash`
5. **No repeated reporting** — acknowledged, unchanged events never reappear in reports

## Project Layout

```
clinical-trial-monitor/
├── run_monitor.py            # CLI entry point
├── config.py                 # Configuration + disease profiles
├── db/                       # Schema (19 tables incl. FTS5 index), connections
├── collectors/               # BaseCollector + 4 registry collectors + WAF browser base
├── core/                     # Entity resolution, change detection, quality, provenance
├── ct_report/                # HTML report package (query/diffing/translate/render)
├── scripts/                  # Report CLI, JSON exporter, ICTRP fetch/import, migrations
├── web/                      # React + TypeScript web viewer
├── tests/                    # pytest suite (180+ tests)
└── data/ · reports/ · logs/  # Runtime artifacts (gitignored)
```

## Operations

```bash
# Triage the entity-resolution review queue (31k+ backlog friendly)
python run_monitor.py review_queue stats                       # counts by confidence bucket
python run_monitor.py review_queue batch --min-confidence 0.95 # dry-run preview (default)
python run_monitor.py review_queue batch --min-confidence 0.95 --yes  # actually apply
python run_monitor.py review_queue export --output queue.csv   # Excel-friendly CSV

# One-off: migrate legacy list fields to canonical JSON arrays (dry-run first)
python3 scripts/migrate_json_fields.py
python3 scripts/migrate_json_fields.py --apply

# Rebuild the FTS index (normally automatic via the v6 migration)
python3 scripts/rebuild_fts.py

# Download & import a WHO ICTRP XML snapshot
python3 scripts/fetch_ictrp_xml.py --query "multiple myeloma" --output data/ictrp_mm_export.xml
python3 scripts/import_ictrp_xml.py --xml data/ictrp_mm_export.xml

# Cross-source data quality report (incl. raw_payload storage share)
python3 scripts/quality_report.py

# Re-visit already-enriched ChiCTR/CTR records so field updates (e.g.
# enrollment) get detected — plan 200 stalest records, consume 50/round
python run_monitor.py crawl --refresh-only --source chictr
python run_monitor.py crawl --refresh-only --source chinadrugtrials

# Back up the database (online backup API, WAL-safe, keeps the newest 7)
python run_monitor.py backup
python run_monitor.py backup --out /path/to/dir --keep 14
```

### Daily pipeline & alerts

`python run_monitor.py pipeline --profiles all` runs crawl → resolve → quality
gate → JSON export → reports → **digest push** → database backup. Each step
runs in its own subprocess; the R4 quality checks run right after entity
resolution, and the backup runs after every DB writer has finished.

The **digest push** sends a daily summary of new trials and field-level
changes (old → new values, grouped by disease profile) to every configured
channel. Delivery is watermark-gated: with no channels configured it is a
no-op (nothing lost); if at least one channel succeeds the watermark
advances; if all channels fail it retries on the next run. Preview without
sending via `python run_monitor.py digest --dry-run`.

Failure alerts are sent to every configured channel (opt-in success brief with
`--notify-success`):

```bash
CT_NOTIFY_WEWORK_WEBHOOK=...    # 企业微信群机器人
CT_NOTIFY_DINGTALK_WEBHOOK=...  # 钉钉群机器人
CT_NOTIFY_FEISHU_WEBHOOK=...    # 飞书群机器人
CT_NOTIFY_WEBHOOK_URL=...       # generic JSON POST
CT_NOTIFY_SMTP_HOST=... CT_NOTIFY_SMTP_PORT=... CT_NOTIFY_SMTP_USER=... \
CT_NOTIFY_SMTP_PASSWORD=... CT_NOTIFY_TO=a@b.com,c@d.com
```

## License

All rights reserved. Contact the repository owner if you wish to use this code.

---

# 中文文档

## 项目简介

全球临床试验持续监测系统：支持七个临床试验注册数据源，将同一试验跨平台对齐为唯一的「主试验（master trial）」，在采集周期间检测字段级变更，并生成增量式跨源报告。

- **多平台采集** — ClinicalTrials.gov、欧盟 CTIS 与 ISRCTN（公开 API）、旧版 EUCTR（历史回填）、ChiCTR 与 CTR/药审中心（浏览器自动化）、WHO ICTRP（XML 快照）
- **跨源实体解析** — 标识符匹配（自动确认）+ FTS 辅助的标题相似度（进入人工审核队列）
- **变更检测** — 不可变版本链、字段级比对、事件哈希去重
- **疾病 profile** — 每种疾病一套可切换的检索口径（内置心肌梗死、多发性骨髓瘤），增量基线相互隔离
- **双产出** — 自包含 HTML 报告 + 自动部署到 GitHub Pages 的 React + TypeScript 网页查看器
- **统一研究工作台界面（1.10.0）** — 全部页面共享一套设计系统、路由按需加载、中英双语、深浅主题，对比度通过 WCAG AA 验证

## 安装

需要 **Python 3.12+**。Playwright 的 Chromium 只有 ChiCTR / CTR 采集器需要。预构建的 release zip(见 [Releases](https://github.com/suehongt/clinical-trial-monitor-public/releases))已包含前端构建产物,只有从源码构建前端才需要 Node.js。

### macOS / Linux

```bash
# 0. 进入项目目录(即含有 requirements.txt 的文件夹)
cd path/to/clinical-trial-monitor

# 1. 创建虚拟环境并安装依赖
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 2. 浏览器内核(仅 ChiCTR / CTR 采集器需要)
venv/bin/playwright install chromium
```

### Windows(PowerShell)

```powershell
# 0. 进入项目目录(即含有 requirements.txt 的文件夹,解压或克隆出来的那一层)。
#    PowerShell 刚打开时停在用户主目录(C:\Users\<用户名>),不先 cd 进项目目录,
#    pip 会报 "No such file or directory: requirements.txt"
cd C:\path\to\clinical-trial-monitor

# 1. 创建虚拟环境并安装依赖
python -m venv venv
venv\Scripts\pip install -r requirements.txt

# 2. 浏览器内核(仅 ChiCTR / CTR 采集器需要)
venv\Scripts\playwright install chromium
```

> **平台差异说明。** 唯一的路径区别是 macOS/Linux 用 `venv/bin/`、Windows 用 `venv\Scripts\`,下文所有命令按平台替换即可。`./start.sh` 是 bash 脚本,Windows 下可用 Git Bash 运行,或先 `cd web && npm run build` 再直接 `venv\Scripts\python -m server`。定时采集:macOS 用 launchd(`scripts/install_launchd.sh`),Linux 用 systemd(`deploy/install_systemd.sh`),Windows 可手动执行 crawl/monitor 命令或注册到任务计划程序。`scripts/crawl_waf_batch.py` 依赖 POSIX 文件锁(`fcntl`),只能在 macOS/Linux 运行。生产部署流程见 [docs/INSTALL.md](docs/INSTALL.md),服务器(Linux + nginx + Basic Auth)部署见 [deploy/DEPLOY.md](deploy/DEPLOY.md)。

## 功能演示

![功能演示预览](docs/assets/demo-teaser.gif)

约 1 分钟的功能走录(演示数据):仪表盘注意力视图 → 双语检索 → 试验变更时间线 → 监测主题与运行历史 → 研究项目笔记本 → 研究简报 → 数据源看板 → 深色模式/中文。完整视频:[docs/assets/demo-video.mp4](docs/assets/demo-video.mp4)(亦附于 [Releases](releases))。

## 快速开始

```bash
python run_monitor.py init                          # 初始化数据库
python run_monitor.py enable clinicaltrials_gov     # 启用数据源
python run_monitor.py crawl --incremental           # 增量采集
python run_monitor.py resolve                       # 实体解析
python run_monitor.py changes                       # 查看待确认变更
python run_monitor.py stats                         # 数据库统计
python run_monitor.py report --type daily           # 日报
```

## 疾病 Profile

检索口径按疾病定义于 `config.DISEASE_PROFILES`，用环境变量 `CT_DISEASE_PROFILE` 切换（默认 `mi` 心肌梗死，内置 `mm` 多发性骨髓瘤）。每个疾病使用独立的增量报告基线，互不覆盖。

```bash
# 心肌梗死（默认）
python3 scripts/trial_report.py --english            # 增量：只渲染新增/变更
python3 scripts/trial_report.py --english --force    # 全量重建

# 多发性骨髓瘤
CT_DISEASE_PROFILE=mm venv/bin/python run_monitor.py crawl --source clinicaltrials_gov
CT_DISEASE_PROFILE=mm venv/bin/python scripts/trial_report.py --english --force

# 中文报告（DeepSeek 翻译，设置 DEEPSEEK_API_KEY；兼容 ANTHROPIC_AUTH_TOKEN）
python3 scripts/trial_report.py

# 临时自定义关键词（不切换 profile）
python3 scripts/trial_report.py --english --force -k "breast cancer" 乳腺癌
```

## 网页查看器与网页版平台

`web/` 目录为 React 18 + TypeScript + Vite 单页应用，交互场景下替代 25MB 单文件 HTML。点击卡片打开详情弹窗（研究目的、研究者、终点、入选/排除标准）：

```bash
CT_DISEASE_PROFILE=mm venv/bin/python scripts/export_report_json.py --profile mm  # 导出数据
cd web && npm install && npm run build && npm run preview                          # 构建/预览
```

推送到 main 后由 CI 自动构建并部署 GitHub Pages。

### 自托管网页版（Phase 7 P1 — 实时查询，无需静态导出）

界面已按研究工作台设计全面重构（1.10.0）：统一组件与信息层级、路由级按需
加载、中英双语与深浅主题全量适配，并通过发布候选审计（0 严重无障碍违规、
三引擎浏览器矩阵、恢复演练与性能预算，判定见 release gate 记录）。

**一条命令启动完整应用（Phase 3E）：**

```bash
./start.sh              # 需要时自动构建前端，http://127.0.0.1:8000 同时服务 API + 应用
./start.sh --dev        # 前端 HMR 开发服务 :5173（已代理）+ API :8000
./start.sh --rebuild    # 强制重新构建前端
```

API 与前端同源服务，仪表盘、试验、监测主题、动态每个页面都直连监测后端。前端通过 `GET /api/health` 显式检查后端健康（含数据库状态与 schema 版本）；后端离线时试验浏览仍走静态导出，监测页面显示可重试的服务状态横幅，不再出现死路提示。手动方式（等价）：

```bash
cd web && npm run build        # 构建前端（一次）
venv/bin/python -m server      # http://127.0.0.1:8000 同时服务 API + 前端
```

- 导航（Phase 3E）：**仪表盘 · 试验 · 监测主题 · 动态** — 落地仪表盘汇总新增/更新试验、重要变更与已监测试验动态（`GET /api/dashboard`）；已监测试验归入「试验 → 已监测」；变更/通知/每日摘要归入「动态」；跨源镜像移入试验详情「来源」页签
- 试验详情（`/trials/{source}/{id}`）分概览 / 变更 / 历史 / 来源四个页签：变更历史带可读字段标签、旧值 → 新值与严重程度；版本时间线支持任意两版比较；研究地点为结构化表格；所有深链接可直接刷新
- 端点：`/api/trials`、`/api/trials/{source}/{id}`（详情 + 变更历史）、`/api/mirrors`、`/api/events`、`/api/dashboard`、`/api/stats`、`/api/profiles`、`/api/health`；交互文档见 `/api/docs`
- ChiCTR 本地适配接口（非官方、只读）：`GET /api/chictr/search?q=心力衰竭&page=1` 与 `GET /api/chictr/studies/ChiCTR2600128415`。它复用采集器的 HTML 解析，未缓存请求串行执行且至少间隔 2 秒，响应缓存 5 分钟，并受共享 WAF 熔断器保护；不会把查询结果写入发现队列，也不会绕过验证码或访问控制。
- China Drug Trials/CTR 本地适配接口（非官方、只读）：`GET /api/ctr/search?q=心力衰竭&page=1` 与 `GET /api/ctr/studies/CTR20262758`。它复用 CDE HTML 解析器，支持 `keywords`/`indication` 字段路由，未缓存请求串行执行且至少间隔 5 秒，响应缓存 5 分钟，并受共享 WAF 熔断器保护。真正保存完整原始 HTML 仍由 full live-check 入库路径负责。
- WHO ICTRP 只读接口：`GET /api/ictrp/search?q=肌钙蛋白&page=1` 驱动公开 ASP.NET 搜索表单，仅返回当前轻量列表字段；`GET /api/ictrp/studies/{registry_id}` 只读本地最新镜像，不访问 WHO，返回保留的 XML 原始记录，并明确把仅来自门户列表的占位记录标为不完整。完整字段仍由每周 XML 快照入库。
- 每个响应带 `data_as_of`（各源最近成功同步时间），页面如实标注「数据截止」
- 浏览器 E2E 测试（真实 Chromium + FastAPI + 构建产物）：`CT_SKIP_WEB_BUILD=1 venv/bin/python -m pytest tests/test_e2e_browser.py -m e2e`
- 定时任务去智能体化：`scripts/install_launchd.sh` 安装每日 pipeline + 每周 ICTRP 任务（ChiCTR/CTR refresh 保持人工错峰）

## 核心设计原则

1. **原始记录不可变** — 各平台原始数据永不修改、永不删除
2. **以关联去重，而非删除** — 跨平台镜像通过 `record_master_map` 挂到同一 `master_trial`
3. **ICTRP 是聚合源** — 其记录链接回原始源，不计入独立发现数
4. **版本链** — 每次检测到变化生成新版本（`is_latest` 翻转），变更事件按 `event_hash` 去重
5. **不重复报告** — 已确认且无新变化的事件不会重复出现在报告中

## 目录结构

```
clinical-trial-monitor/
├── run_monitor.py            # CLI 统一入口
├── config.py                 # 配置 + 疾病 profile
├── db/                       # 数据库 Schema（19 张表，含 FTS5 全文索引）
├── collectors/               # 采集器基类 + 四个注册平台采集器 + WAF 浏览器基类
├── core/                     # 实体解析 / 变更检测 / 质量检查 / 溯源
├── ct_report/                # HTML 报告包（查询/增量/翻译/渲染）
├── scripts/                  # 报告 CLI、JSON 导出、ICTRP 抓取导入、迁移脚本
├── web/                      # React + TypeScript 网页查看器
├── tests/                    # pytest 测试套件（180+ 用例）
└── data/ · reports/ · logs/  # 运行产物（不入库）
```

## 运维脚本

```bash
# 审核队列分流（适应 3 万+ 积压）
python run_monitor.py review_queue stats                        # 按置信度分档统计
python run_monitor.py review_queue batch --min-confidence 0.95  # 默认 dry-run 预览
python run_monitor.py review_queue batch --min-confidence 0.95 --yes  # 真正执行
python run_monitor.py review_queue export --output queue.csv    # Excel 友好 CSV

# 一次性：旧库列表字段统一为 JSON 数组（先 dry-run，确认后 --apply）
python3 scripts/migrate_json_fields.py
python3 scripts/migrate_json_fields.py --apply

# 重建 FTS 全文索引（正常由 v6 迁移自动完成，仅索引漂移时需要）
python3 scripts/rebuild_fts.py

# 下载并导入 WHO ICTRP XML 快照
python3 scripts/fetch_ictrp_xml.py --query "multiple myeloma" --output data/ictrp_mm_export.xml
python3 scripts/import_ictrp_xml.py --xml data/ictrp_mm_export.xml

# 跨源数据质量报告（含 raw_payload 存储占比）
python3 scripts/quality_report.py

# 复查已入库的 ChiCTR/CTR 记录，让源端字段变化（如入组人数）能被检测到
# ——入队最久未验证的 200 条，每轮每源预算 50 条详情页
python run_monitor.py crawl --refresh-only --source chictr
python run_monitor.py crawl --refresh-only --source chinadrugtrials

# 备份数据库（在线备份 API，WAL 安全，默认保留最近 7 份）
python run_monitor.py backup
python run_monitor.py backup --out /path/to/dir --keep 14
```

### 每日流水线与告警

`python run_monitor.py pipeline --profiles all` 依次运行 抓取 → 实体解析 → 质量门 →
JSON 导出 → 报告 → **每日推送** → 数据库备份。每步独立子进程；R4 质量检查紧跟实体解析之后，
备份在所有数据库写入方完成后执行。

**每日推送（digest）**把当天新增试验与字段级变更（含 old → new 值、按疾病
profile 分组）推送到所有已配置渠道。水位线门控：未配置渠道时静默跳过
（不丢历史）；任一渠道成功即推进水位；全部失败则下次运行重推。
`python run_monitor.py digest --dry-run` 可只看不发。

失败告警推送到所有已配置渠道（`--notify-success` 可选开启成功简报）：

```bash
CT_NOTIFY_WEWORK_WEBHOOK=...    # 企业微信群机器人
CT_NOTIFY_DINGTALK_WEBHOOK=...  # 钉钉群机器人
CT_NOTIFY_FEISHU_WEBHOOK=...    # 飞书群机器人
CT_NOTIFY_WEBHOOK_URL=...       # 通用 JSON POST
CT_NOTIFY_SMTP_HOST=... CT_NOTIFY_SMTP_PORT=... CT_NOTIFY_SMTP_USER=... \
CT_NOTIFY_SMTP_PASSWORD=... CT_NOTIFY_TO=a@b.com,c@d.com
```

## 许可

保留所有权利。如需使用本代码，请联系仓库所有者。
