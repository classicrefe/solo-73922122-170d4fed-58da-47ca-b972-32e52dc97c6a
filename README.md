# BOM 叶件用量数仓

制造企业成本数仓的多级 BOM 叶件用量表：浏览器维护父子关系与用量，整批提交后
沿路径乘用量、多路径相加，把结果**落库**；查看与下载只读已落库的快照。

另提供**离线修订合并页**：下载完整 `parent,child,qty` CSV（服务端同时保存不可变
基准并返回修订单 ID），工艺员离线改完后上传同一份完整文件（缺行＝删除），
按基准/上传/当前三方比较后应用；结构与物化用量一起生效。

## 快速启动

```bash
docker compose up --build
```

- 网页 A：<http://localhost:8000>（进程 `web-a`）
- 网页 B：<http://localhost:8001>（进程 `web-b`，与 A 共用同一个 SQLite 文件）
- 离线合并页：<http://localhost:8000/offline>（B 上同样可访问）
- `seed` 一次性任务会通过 HTTP 载入演示结构，并把叶件 `L-4` 提升为装配件
  （`L-4 → L-6/L-7`），两个样例都可直接看到。

离线合并的浏览器操作：

1. 「下载完整 CSV（生成修订单）」——浏览器拿到完整文件，页面显示修订单 ID；
2. 用任意表格工具离线修改（删掉的行即删除该父子关系），另存为 CSV；
3. 「选择文件」→「预览三方合并」——逐项显示基准/上传/当前与采用结果；
4. 双方改成不同值（含删除 vs 改用量）的冲突行，逐项选「用上传 / 用当前」，
   再点「按选择重新预览」；
5. 无未解决冲突时「确认应用合并」——结构与物化叶件用量在同一事务生效。
   若预览后库里又有提交，旧确认返回 409 并要求重新预览，新修改不会被覆盖。


可选：在两个真实容器之间验证交错修改：

```bash
docker compose --profile demo run --rm interleave
```

本地（不使用 Docker）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000            # 第二个进程换端口即可
python scripts/concurrency_demo.py           # 起两个真实进程做交错验证
pytest -q                                    # 90 项测试
```

## 功能与约束

| 要求 | 实现 |
| --- | --- |
| 最多 100 个料号、200 条父子关系 | `validate_limits`（超限返回 409，整批不写） |
| 浏览器成批增改删、正整数用量 | 单条操作先进入「批改清单」，「提交整批」一次提交；用量 1..1e9 |
| 每个装配件展开到叶件、沿路径乘用量、多路径相加 | `BomStore.expand`（按子装配记忆化，共享子装配不会指数爆炸） |
| 结果必须落库 | `leaf_usage` 表中按版本物化，启动/查看/下载都不再展开 |
| 以整批修改后的结构判环，显示路径，遇环整批不改 | `find_cycles` 基于最终结构；409 返回 `paths` 如 `A → B → C → A`，事务回滚 |
| 两个 Web 进程共库 | 同一 `BOM_DB_PATH`，WAL + `busy_timeout`；`docker-compose.yml` 起两份 |
| 递归展开不占写锁，其他修改可先提交 | 展开与判环都在内存快照上完成，之后才 `BEGIN IMMEDIATE` |
| 迟到的展开不能覆盖新结构/漏掉新依赖 | 提交时对 `bom_head.version_seq` 做 CAS；失败则按最新结构**重新规划并重新判环** |
| 分别无环的并发修改不能合成环 | 复核在最新结构上重跑，合成环会被拒绝 |
| 关系与受影响用量表同时更新，失败保留旧版 | 新版本写入 `bom_edge` + `leaf_usage` + 切换 `bom_head` 全在同一事务 |
| 查看/下载不混两个提交、不临时展开 | 每次请求在其读事务内解析一次 `bom_head`，只 SELECT 物化行 |
| 重启保留结果 | 数据在卷 `bom-data` 的 SQLite 文件中；版本与用量行都在 |
| 离线下载保存不可变基准 | `POST /api/offline/revisions` 把当前结构快照写入 `offline_revision`（只插入，不更新），返回修订单 ID |
| 上传完整 CSV，缺行＝删除 | `parse_edges_csv` 把整份文件作为完整结构；未出现的父/子关系视为删除 |
| 仅一侧变化采用、双方相同合并、双方不同逐项选 | `three_way_merge` 按父子关系比较基准/上传/当前；冲突行给出三方值与 `current/upload` 选择 |
| 删除参与比较 | 每一侧的值可为 `null`；删除 vs 改用量同样进入冲突展示 |
| 未解决冲突不得应用 | 预览标记 `resolved=false`，`apply` 返回 409 `UnresolvedConflictError`；预览阶段即可按选择重新预览 |
| 预览后库又有提交则旧确认拒绝 | 预览冻结 `current_version`；应用时严格 CAS，版本不符返回 409 `StalePreviewError`，要求重新预览 |
| 最终合并成环显示路径、整批不改 | 预览时 `find_cycles` 返回 `paths`（如 `A → B → C → A`），应用前再次判环；成环整批不写 |
| 成功时结构与物化用量一起生效 | `_commit_exact` 在同一个 `BEGIN IMMEDIATE` 中写 `bom_edge` + `leaf_usage` + 切换 `bom_head`，失败回滚保留旧版 |
| 沿用双 Web 共库，合并/展开不持写锁 | 比较与展开都在内存中完成，仅在最后短事务持锁；`X-Demo-Pause` 测试证明其他进程可同时提交 |
| 共享子装配 / 叶件转装配件 / 可暂停计算的交错修改样例 | 见下文 |

## 并发模型

```
读快照 ──► 应用批改 ──► 判环 ──► 展开（全部在内存，无写锁）
                                        │
                                        ▼
                        BEGIN IMMEDIATE ──► CAS 版本号
                           成功：写新版本 + 用量 + 切 head + 清理旧行 → COMMIT
                           失败：ROLLBACK，回到第一步按最新结构重来
```

- 写锁只在最后一段纯 SQL 事务内持有，毫秒级；期间别的进程可以正常提交。
- CAS 失败说明期间有新提交，此时**旧计划作废**：重新读取结构、重新应用批改、
  重新判环、重新展开。因此迟到的展开不会覆盖新结构，也不会漏掉新依赖。
- 两次分别无环的修改若合起来成环，后提交的一方会在重规划后的结构上发现环而
  整批失败（返回路径）。测试 `test_two_acyclic_edits_cannot_combine_into_cycle`
  与 `scripts/concurrency_demo.py` 场景 2 覆盖此点。
- 只保留当前发布版本的边与用量行（同事务内清理），因此重启后读到的是同一份
  结果，且存储量有界。

### 离线合并的并发规则

```
下载基准（只读快照 → 写入不可变 offline_revision）
        │
上传 + 三方比较（纯内存，无写锁）──► 冻结 merge_preview（记录 current_version）
        │
确认应用 ──► 判环 ──► 展开（无写锁）
                              │
                              ▼
                 BEGIN IMMEDIATE ──► 严格 CAS current_version
                    相符：写结构 + 用量 + 切 head → COMMIT
                    不符：ROLLBACK → 409 StalePreviewError，要求重新预览
```

- 预览与展开都不持写锁，普通编辑照常提交（`X-Demo-Pause` 头可复现）。
- 旧确认只在其预览版本仍为当前版本时有效；期间任何提交都会让旧确认被拒绝，
  新修改绝不被覆盖。
- 应用路径不做「重规划」：合并结果是用户逐个冲突确认过的，库已变化时重新比较
  可能得出不同结果，因此要求重新预览而不是静默采用旧决定。
- 应用成功时 `bom_edge` 与 `leaf_usage` 同一事务写入；写入或展开失败则回滚，
  旧版本与其用量保持发布状态。

## 样例

1. **共享子装配**：`FG-1 → SA-1(2) → L-3(5)` 与 `FG-2 → SA-1(3) → L-3(5)`。
   用量表里 `(FG-1,L-3)=10`、`(FG-2,L-3)=15`，同时 `(SA-1,L-3)=5`
   作为子装配自身的展开行。
2. **叶件转装配件**：`POST /api/demo/promote-leaf`（UI 上「叶件转装配件」按钮）
   在一批中把 `L-4` 改为由 `L-6(3)`、`L-7(1)` 组成；`FG-1` 的 `L-4` 行消失，
   `L-6/L-7` 行出现，全部来自同一次提交。
3. **可暂停计算的交错修改**：演示模式下 `X-Demo-Pause` 头会让进程在展开阶段暂停
   （写锁尚未获取）。此时另一个进程可以提交；暂停方醒来后因 CAS 失败而重规划。
   可用 UI 的「写 A 侧 / 写 B 侧」按钮、`/api/demo/interleave/{a|b}`，或
   `scripts/concurrency_demo.py` 复现。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 当前版本的边、用量、版本号、上限 |
| POST | `/api/batch` | `{"ops":[...]}` 顺序执行；`replace` 为整体替换；`{"reset":true,"upserts":[...]}` 直接重建 |
| GET | `/api/leaf-usage?top=&leaf=` | 已落库用量（单快照） |
| GET | `/api/leaf-usage.csv?top=&leaf=` | 下载 CSV，带 `X-Bom-Version` |
| GET | `/api/usage-summary` | 只读汇总 |
| POST | `/api/offline/revisions` | 下载完整 `parent,child,qty` CSV，保存不可变基准，返回 `X-Bom-Revision` / `X-Bom-Version` |
| GET | `/api/offline/revisions/{id}` · `/api/offline/revisions/{id}.csv` | 修订单信息 / 重新下载原始基准 |
| POST | `/api/offline/preview` | multipart（`file` + `revision_id`，可选 `resolutions` JSON）或 JSON（`csv`/`rows`）；返回三方比较、冲突、最终结构与 `preview_id` |
| GET | `/api/offline/preview/{preview_id}` | 读取已冻结的预览 |
| POST | `/api/offline/apply/{preview_id}` | 应用已解决冲突的预览；过期确认返回 409 `StalePreviewError` |
| POST | `/api/demo/seed` / `/api/demo/promote-leaf` / `/api/demo/interleave/{a,b}` | 演示样例 |
| GET | `/api/demo/scenarios` | 样例说明 |

离线合并示例：

```bash
curl -sX POST localhost:8000/api/offline/revisions -D - -o base.csv
# 修改 base.csv（删行=删除关系），然后用响应头里的 X-Bom-Revision 上传
curl -sX POST localhost:8000/api/offline/preview \
  -F revision_id=<修订单ID> -F file=@edited.csv
# 若返回 conflicts，带选择再预览一次
curl -sX POST localhost:8000/api/offline/preview \
  -F revision_id=<修订单ID> -F file=@edited.csv \
  -F 'resolutions=[{"parent":"FG-1","child":"SA-1","choice":"upload"}]'
curl -sX POST localhost:8000/api/offline/apply/<preview_id>
```

批改示例：

```json
{"ops": [
  {"op": "upsert", "parent": "FG-1", "child": "SA-9", "qty": 2},
  {"op": "delete", "parent": "FG-1", "child": "L-1"},
  {"op": "upsert", "parent": "SA-9", "child": "L-8", "qty": 3}
]}
```

错误：422 校验失败（非正整数、非法料号、超限）；409 环路（含 `paths`）或容量超限。

## 文件

```
app/store.py        SQLite 版本化存储、展开、判环、并发提交、离线基准与三方合并
app/main.py         FastAPI 应用工厂、API 与页面
app/demo.py         内置样例数据与交错修改用例
app/templates/      单页 UI（含 /offline 合并页）
app/static/         前端脚本与样式
scripts/            两进程演示、HTTP 交错验证、种子任务、worker
tests/              pytest（90 项：展开、判环、原子性、并发、HTTP、合并、UI）
docker-compose.yml  双 Web + 种子任务（+ 可选 interleave）
```

## 测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

覆盖：多路径求和、共享子装配、叶件转装配件、深 DAG 记忆化、整批判环与路径、
`replace`/`reset`/顺序语义、容量上限、展开失败与写入中途失败的回滚、跨实例持久化、
展开期间无写锁、迟到展开重规划、并发合成环、读写快照一致性、CSV 单快照、
重启保留、两 Web 应用共库，以及模板/JS 元素引用一致性。离线合并另覆盖：
CSV 往返与坏文件拒绝、仅一侧变化/双方相同/双方不同/删除参与比较的规则、
未解决冲突阻止应用、过期确认拒绝且不覆盖新修改、合并后成环显示路径且整批不改、
应用时结构与物化用量同事务、以及合并展开期间其他进程照常提交。
