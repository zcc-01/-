# 代码推送说明（feat/identity-ops）

## 为什么需要你自己执行一步

我尝试直接推送，被 GitHub 拒绝了：

```
POST /repos/zcc-01/-/git/trees  ->  403 Resource not accessible by integration
```

原因：当前会话连的 GitHub 账号是 `xing-kong-lang-ye`，不是仓库所有者 `zcc-01`，
对 `zcc-01/-` 只有读权限。另外本机网络也是白名单代理，`git`/`curl` 直连 GitHub 不通，
所以只能用你自己的凭据在你机器上推。

## 最省事的方式（推荐）

```bash
cd ~/Desktop/agri-identity-ops
git remote add origin https://github.com/zcc-01/-.git
git push -u origin feat/identity-ops --force
```

> 加 `--force` 的原因：远端 `feat/identity-ops` 现在只有一个 README 提交（与 main 同一个 sha），
> 与本地 11 个提交的历史不同源。若你希望保留远端历史，改用：
> ```bash
> git remote add origin https://github.com/zcc-01/-.git
> git fetch origin
> git rebase origin/feat/identity-ops
> git push -u origin feat/identity-ops
> ```

用 GitHub Desktop 也可以：`File → Add local repository → 选 Desktop/agri-identity-ops`，
然后 Publish/Push branch 即可，凭据是现成的。

## 已经提交好的 11 个 commit

```
d291113 test(identity-ops): 43 条单元与接口用例
fc9cc9f feat(web): 页面模板与服务入口
1959fa9 feat(ops): UC-13 经营看板与 UC-15 监管回溯
14e47e8 feat(ops): UC-14 溯源异常预警规则引擎
23eb33d feat(ops): UC-12 产地认证审核
7522549 feat(ops): 用户与运营域数据模型
aac5748 feat(identity): UC-11 注册登录与鉴权
45652cf chore: 统一换行符
11ff676 feat(core): 公共实体契约、数据库会话与演示种子数据
402da4e feat(core): 公共基础层
b8473a7 chore(project): 工程骨架、依赖与一键启动脚本
```

提交信息遵循 Conventional Commits，与报告附录 3 的规范一致。

## 本地跑起来（截图用）

```bash
cd ~/Desktop/agri-identity-ops
pip install -r requirements-dev.txt
python -m app.db.seed_cli
uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000>：

| 页面 | 用什么账号 | 对应报告图 |
|---|---|---|
| `/ops` → 认证待审 | operator01 / Passw0rd! | 图 5-7 产地认证审核工作台 |
| `/ops` → 预警工单 | operator01 | 图 5-8 溯源异常预警列表 |
| `/farmer` | farmer01 | 农户入驻与认证提交 |
| `/regulator` | regulator01 | 监管回溯与 CSV 导出 |

截图后把报告 5.2.4 里两处「［此处插入图片］」替换掉即可（图 5-7、图 5-8 的图题已经写好）。

## 质量门（已在本机跑过）

```
ruff check .   -> All checks passed
pytest -q      -> 43 passed
```
