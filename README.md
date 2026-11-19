# 农产品溯源与产地直供电商平台 · 用户与运营域（feat/identity-ops）

> 分支说明：本分支交付**用户与运营域**（UC-11～UC-15）与平台公共基础。
> 溯源链域在 `feat/trace-chain`，交易域在 `feat/trade`，三者通过统一的用户身份、
> 统一响应结构与统一审计日志解耦，可单独 checkout 也可合并运行。

## 一、这个分支做了什么

| 用例 | 名称 | 关键实现 | 代码位置 |
|---|---|---|---|
| UC-11 | 注册登录与鉴权 | 口令 + 验证码双登录、JWT、四角色 RBAC 矩阵、越权 403 且留痕审计、失败锁定 | `app/account/`、`app/core/{security,rbac,deps,audit}.py` |
| UC-12 | 产地认证审核 | 材料缺项存草稿、在途申请唯一、**审核结论单向不可改**、检测报告过期禁止通过、12 个月有效期 + 到期提醒 | `app/ops/cert_service.py` |
| UC-13 | 经营数据看板 | 平台视角与农户视角双看板；跨域指标「不可用即显示 —」而非 0 | `app/ops/dashboard_service.py` |
| UC-14 | 溯源异常预警 | **可配置规则表**（R-01/R-02/R-03）、30 分钟调度、工单去重、待处置→处置中→已关闭流转 | `app/ops/alert_service.py` |
| UC-15 | 监管回溯与处置 | 按产地/批次/时间回溯、单批次链路明细、CSV 导出（列表与导出同源） | `app/ops/regulator_service.py` |

## 二、快速启动

```bash
bash scripts/bootstrap.sh          # 装依赖 → 建库 → 写种子数据 → 启动
# 或手动：
pip install -r requirements-dev.txt
python -m app.db.seed_cli
uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000>，接口文档在 `/docs`。

### 演示账号（口令均为 `Passw0rd!`）

| 账号 | 角色 | 建议演示路径 |
|---|---|---|
| `farmer01` | 农户 | `/farmer`：入驻 → 提交认证 → 我的申请 → 看板 |
| `operator01` | 平台运营 | `/ops`：审核队列 → 预警工单 → 平台看板 → 规则配置 |
| `regulator01` | 监管人员 | `/regulator`：条件回溯 → 链路明细 → 导出 CSV |
| `consumer01` | 消费者 | 用于越权测试：访问 `/api/ops/**` 应返回 403 |

## 三、目录结构

```
app/
├── core/            公共基础：配置、统一响应/异常、雪花 ID、JWT、RBAC、审计
├── common/models.py 三人共用契约：sys_user、farm（本分支不修改其既有字段）
├── db/              引擎与会话、种子数据、seed_cli
├── account/         UC-11 账号与入驻：router / service / schemas
├── ops/             UC-12～UC-15：cert / alert / dashboard / regulator 四个 service
└── web/             Jinja2 页面 + 原生 JS（无前端构建链）
tests/               43 条 pytest 用例（单元 + 接口 + 越权）
```

## 四、几条刻意为之的设计取舍

1. **审核结论不可修改靠状态机，不靠接口自觉。**
   `review` 只接受 `submitted`，终态不再进入状态机；数据库层 `alert.dedupe_key`、
   `cert_application.cert_no` 各有唯一约束兜底。

2. **跨域依赖一律「探测 + 降级」。**
   看板、预警、回溯都要读交易域/溯源域的表。用 `has_table()` 探测，表不在就返回
   `available=false` 并注明原因（页面显示「—」），绝不把「没有数据」显示成 0，
   也绝不因为对方分支没合入就 500。

3. **预警规则表化。**
   AI 初版给的是硬编码 `if-else`（见报告 5.2.4 的人工修改点），改为 `alert_rule`
   配置表后，运营改阈值不需要改代码，启停也不需要发版。

4. **跨省预警按设备聚合。**
   同一设备在多省扫码视为正常移动，其记录整体剔除后再统计省份数，
   这是把误报从 41 条压到 6 条的关键一步（BUG-15）。

5. **审计写失败不影响主流程。**
   越权请求的首要诉求是「返回 403 并留痕」，日志库异常时 403 仍必须返回，
   因此 `write_audit` 吞异常并降级到标准日志。

## 五、质量门

```bash
ruff check .        # 静态检查，0 问题
pytest -q           # 43 passed
```

CI 配置见 `.github/workflows/ci.yml`：push 到 `feat/**` 或 `main` 触发 ruff + pytest。
