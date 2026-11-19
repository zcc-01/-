# 农产品溯源与产地直供电商平台

> 以「一物一码」为数据主线，把溯源能力嵌入交易流程 —— 卖货的同时，把每件农产品的生产履历钉死。

[![CI](https://github.com/zcc-01/-/actions/workflows/ci.yml/badge.svg)](https://github.com/zcc-01/-/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11-3774AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-D71F00)
![Tests](https://img.shields.io/badge/tests-44%20passed-brightgreen)

## 项目简介

农产品"优质不优价、来源不可查、责任难追溯"的根因是**环节数据不可信**：纸质台账可补可改，事后无从核验。
本项目把生产、加工、质检、物流、销售五个环节的记录串成一条**哈希链**，消费者扫码即可看到从田间到餐桌的完整履历，
并且能判断这份履历是否被改过；同时以产地直供模式压缩中间环节，让品质投入转化为价格回报。

系统的关键差异在于**交易数据与溯源数据同源**：下单、发货、核销都要回查溯源码状态，
履历不是事后补录的展示页，而是交易流程的强制约束。

## 当前进度

| 业务域 | 负责人 | 用例 | 状态 |
|---|---|---|---|
| 溯源链域 + 平台公共基础 | 张××（组长） | UC-01～UC-05 | ✅ 已完成，含 44 个自动化测试 |
| 交易域 | 李×× | UC-06～UC-10 | 🟡 进行中（`feat/trade`） |
| 用户与运营域 | 王×× | UC-11～UC-15 | 🟡 进行中（`feat/identity-ops`） |

本仓库当前可直接运行的部分：**溯源链域全链路 + 登录鉴权 + 统一响应/异常 + 扫码页面 + CI/Codespaces**。

## 核心特性

| 特性 | 实现说明 |
|---|---|
| 哈希链存证 | `curr_hash = SHA-256(prev_hash｜batch_id｜seq_no｜stage｜operator_id｜occurred_at｜detail)` |
| 断链定位 | 校验返回 `broken_at`、`reason`（内容篡改／前序失配／序号缺失／尾部截断），不抛异常 |
| 并发安全 | 序号由 `batch.event_count` 原子自增分配，`(batch_id, seq_no)` 唯一索引兜底，冲突自动重试 |
| 双粒度赋码 | 批次码 + 单品码，码值由「批次号+类型+四位序号」派生，重复提交天然幂等 |
| 交易同源（预留） | 溯源码状态提供 `active/revoked`，交易域下单前回查 |
| 扫码日志 | 匿名可扫码，日志写入失败降级不阻断查询（UC-04 异常流程 6a） |
| 一键复现 | devcontainer + `scripts/bootstrap.sh`，克隆后一条命令启动 |

## 分层架构

```
表现层    Jinja2 + 原生 CSS（/trace/{code} 扫码页）
接口层    FastAPI Router + Pydantic v2（参数校验、鉴权、统一响应）
业务层    TraceService（用例编排、事务边界、业务规则）
数据层    SQLAlchemy 2.0 ORM（SQLite 开发 / MySQL 生产）
基础设施  HashChain 哈希链、JWT、雪花号、二维码导出、日志
```

横向按业务域切分为 `trace` / `trade` / `account` 三个包，域间只通过业务层接口调用；
公共能力（统一响应、异常、鉴权、主键、数据库会话）收在 `app/core` 与 `app/db`，由组长维护。

## 快速开始

### 方式一：GitHub Codespaces（推荐）

1. 仓库页 → `Code` → `Codespaces` → `Create codespace on main`
2. 容器建好后自动装依赖，执行：

```bash
bash scripts/bootstrap.sh
```

3. 打开 `http://127.0.0.1:8000`，接口文档在 `/docs`。

### 方式二：本地

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
python scripts/seed.py        # 建表 + 种子数据
python -m uvicorn app.main:app --reload
pytest -q                     # 44 个测试
```

### 演示账号（口令统一 `Passw0rd!`）

| 账号 | 角色 | 用途 |
|---|---|---|
| `farmer01` | 农户 | 建档、录入环节、赋码 |
| `operator01` | 平台运营 | 批次查询、链校验 |
| `regulator01` | 监管人员 | 全量批次与校验 |
| `consumer01` | 消费者 | 普通访问 |

演示溯源码：`PC-20251101-DEMO-ITM0001`（扫码页 `/trace/PC-20251101-DEMO-ITM0001`）

## 接口一览

统一响应：`{"code": 0, "message": "ok", "data": ...}`，错误码见 `app/core/errors.py`。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login` | 公开 | 口令登录，签发 JWT |
| POST | `/api/auth/dev-login` | 公开（仅 DEV_MODE） | 演示免密登录 |
| POST | `/api/batches` | 农户 | 创建生产批次（UC-01） |
| GET | `/api/batches` | 农户/运营/监管 | 批次列表（农户只见本人产地） |
| GET | `/api/batches/{id}` | 同上 | 批次详情 + 环节 + 码数量 |
| PATCH | `/api/batches/{id}/status` | 农户 | 状态单向流转 |
| POST | `/api/batches/{id}/events` | 农户 | 录入溯源环节（UC-02） |
| GET | `/api/batches/{id}/events` | 农户/运营/监管 | 环节记录 |
| POST | `/api/batches/{id}/codes` | 农户 | 生成溯源码（UC-03，≤5000） |
| GET | `/api/batches/{id}/codes` | 农户/运营 | 码列表 |
| GET | `/api/batches/{id}/qrcodes.zip` | 农户/运营 | 二维码压缩包 + manifest.csv |
| POST | `/api/codes/{code}/revoke` | 农户 | 作废溯源码 |
| GET | `/api/trace/{code}` | 公开 | 扫码溯源（UC-04） |
| GET | `/trace/{code}` | 公开 | 扫码落地页（时间轴 + 断链告警） |
| GET | `/api/batches/{id}/verify` | 运营/监管 | 链完整性校验（UC-05） |
| GET | `/api/batches/{id}/scans` | 运营/监管 | 扫码日志 |
| GET | `/api/health` | 公开 | 健康检查 |

完整入参/出参与调用示例见 [`docs/接口文档.md`](docs/接口文档.md)。

## 哈希链设计要点

**为什么要链式摘要**：单条记录自带摘要只能证明"这条没被改"，不能证明"没有记录被删掉或插队"。
把前一条的摘要写进当前记录，任何位置被改动都会让后续所有链接失效，且能定位到第一条出问题的记录。

**序号分配（并发关键）**：不用 `SELECT MAX(seq_no)+1`（并发下会取到同一个值），
而是对批次行做 `UPDATE batch SET event_count = event_count + 1`，在 MySQL/PG 上即行级锁，
在 SQLite 上即库级写锁，天然串行；`(batch_id, seq_no)` 唯一索引作为最终兜底，冲突回滚重试。

**尾部截断检测**：仅逐条重算无法发现"删掉最后几条同时改小计数"。
因此 `batch` 表维护 `event_count` 与 `last_event_hash` 两个锚点，校验时与链尾比对，命中则报 `tail_truncated` / `count_mismatch`。

**边界与取舍**（诚实记录，答辩会被问）：
- 创世摘要为 `SHA-256("GENESIS"|batch_id)`，可重现但**可被攻击者重算整条链**——真正的防篡改需要外部锚定（如定期把链尾摘要写入第三方存证/区块链），当前为课程范围内的单机方案；
- `location` 字段未参与摘要（与报告表 4-4 的公式一致），修改地点不会被检测到，属已知缺口，已在 `docs/数据库设计.md` 列为演进项；
- SQLite 并发上限较低，生产切 MySQL 后压测数据见 `docs/测试说明.md`。

## 目录结构

```
.
├── .devcontainer/          # Codespaces 一键开发环境
├── .github/
│   ├── workflows/ci.yml    # ruff + pytest + 启动冒烟
│   ├── CODEOWNERS          # 按业务域自动指派评审
│   ├── ISSUE_TEMPLATE/     # 任务 / 缺陷模板
│   └── PULL_REQUEST_TEMPLATE.md
├── app/
│   ├── core/               # 公共基础：配置、异常、响应、鉴权、雪花号
│   ├── db/                 # 引擎/会话、建表、种子数据
│   ├── common/             # 跨域最小契约：User、Farm、Role
│   ├── trace/              # 溯源链域：models/schemas/service/chain/router/二维码
│   ├── account/            # 用户与运营域（登录部分，其余由王×× 扩展）
│   ├── web/                # 扫码页模板与静态样式
│   └── main.py             # 应用入口、中间件、异常处理器、页面路由
├── docs/
│   ├── use-cases/          # UC-01～UC-05 用例规格
│   ├── 接口文档.md
│   ├── 数据库设计.md
│   ├── 设计说明.md          # 类图/顺序图（PlantUML）
│   ├── 分工与协作说明.md
│   ├── issues-清单.md      # 可直接粘贴到 GitHub 的任务清单
│   └── 测试说明.md
├── sql/schema_mysql.sql    # 生产 DDL + 只追加权限示例
├── scripts/                # bootstrap / seed / smoke
└── tests/                  # 44 个用例：链路、并发、鉴权、赋码、扫码
```

## 测试与质量

```bash
ruff check .            # 静态检查
pytest -q               # 44 passed
bash scripts/smoke.sh   # 对已启动服务跑一遍端到端流程
```

覆盖重点：

- 链完整性：正常链、内容篡改、前序失配、中间记录删除、尾部截断；
- 并发：6 线程 × 2 次追加，序号连续且链不断裂（`tests/test_concurrency.py`）；
- 业务规则：重复建档 409、越权 403、状态机禁止回退、赋码幂等与上限、作废码 410；
- 页面：扫码页正常渲染、断链告警条、码不存在 404。

## 团队与纵向分工

三人各认领一个完整业务域，独立走完「业务分析 → 需求建模 → 系统设计 → 编码 → 测试 → 文档」，
不做前后端横切；域之间靠统一架构规范、接口契约与主键约定解耦。

| 成员 | 业务域 | 用例 | 代码目录 | 分支 |
|---|---|---|---|---|
| 张××（组长） | 溯源链域 + 公共基础 | UC-01～UC-05 | `app/trace`、`app/core` | `feat/trace-chain` |
| 李×× | 交易域 | UC-06～UC-10 | `app/trade` | `feat/trade` |
| 王×× | 用户与运营域 | UC-11～UC-15 | `app/account` | `feat/identity-ops` |

详细阶段产出物与报告章节对应见 [`docs/分工与协作说明.md`](docs/分工与协作说明.md)。

## GitHub 协作规范

**分支**：`main` 受保护，只允许通过 PR 合并；功能分支 `feat/<域>`；修复分支 `fix/<问题>`。

**提交信息**：Conventional Commits，例如

```
feat(trace): 批次建档与创世记录同事务落库
fix(trace): 并发追加时序号冲突改为原子自增 + 唯一索引兜底
test(trace): 补齐尾部截断检测用例
docs(trace): 补充 UC-01~UC-05 用例规格
```

**流程**：Issue 认领 → 建分支 → 小步提交 → 开 PR（自动带模板与 CODEOWNERS 评审）→ CI 通过 → Squash 合并。

**Issue 清单**：见 [`docs/issues-清单.md`](docs/issues-清单.md)，已按 UC 拆好，可直接批量建 issue。

## 已知边界与后续计划

| 项 | 现状 | 计划 |
|---|---|---|
| 链条外部锚定 | 单机哈希链 | 引入定期摘要快照 / 第三方存证 |
| `location` 参与摘要 | 未参与（对齐报告公式） | v1.1 纳入哈希口径并做数据迁移 |
| 异步扫码日志 | 同会话尽力写入 | 换消息队列异步补写 |
| 交易域联动 | 仅预留码状态 | 李×× 在 `feat/trade` 回查码状态 |
| 预警工单 | 仅返回 warning 提示 | 王×× 在运营域落 `alert` 表与规则引擎 |

## 许可

课程作业用途，未授权商用。
