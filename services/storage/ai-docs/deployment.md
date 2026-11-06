---
audience: ai
doc-type: guide
based-on: 本分支（基于合并 84f20fe 后的代码；部署切片 deploy/ 与 health.py、deployment.py 等已在本分支实现，提交状态以提交历史为准）的 deploy/compose.yaml、deploy/.env.example、deploy/Dockerfile、deploy/runtime.py、deploy/postgres-init.sh、services/storage/src/ear_storage 的 cli.py、health.py、http.py、deployment.py，以及备份工具 tools/storage_backup.py；容器冒烟 /tmp/ear-storage-final-smoke.log（.exit 为 0，16 项 PASS）；备份新版真实往返与五种真实竞争 /tmp/ear-final-race-84lx9T/real.log（1 passed in 100.61s）及汇总 /tmp/ear-final-race-close.json（历史失败记录 /tmp/ear-backup-main-final.log 早于新版，已被取代，不代表当前状态）；三开启本地完整运行 /tmp/ear-repro-suite-ykxiUWmy（exit 为 0，末行 1503 passed, 5 skipped, 1 warning in 890.20s；同目录 static.exit 为 0）；项目负责人授权①功能分支实现，合并前由负责人兼 I 审核
---

# ①存储切片：隔离部署、初始化与健康检查

只部署①存储切片（`postgres`、`nats`、`storage`、`relay`、`manage-worker`），**不是** Ear 全部模块，不启动②③④⑤⑥⑦。本文是工程操作说明，不是生产运行手册：

- **没有任何生产启动、未做生产验收**；部署切片已在本分支实现（提交状态以提交历史为准），**未合并**；本地验证见第 0 节，远程 CI 与负责人兼 I 的合并前人工审核均尚待。
- AI 只写说明，**不代替用户执行真实部署**；以下命令由操作者在自己的隔离环境中执行。
- 备份/恢复新版已通过隔离环境的真实往返与五种竞争验证（见第 0 节），但**不是**生产运行手册：本文只在第 8 节给出运维交接要点，不提供逐步命令。

## 0. 当前证据（只对应所标状态）

| 证据 | 结果 | 说明 |
|---|---|---|
| 容器冒烟 `/tmp/ear-storage-final-smoke.log` | `.exit` 为 0，16 项 PASS，结束时清理了随机名 Compose 项目 | 覆盖：Compose 校验不输出解析结果；显式 migrate→bootstrap→身份链；首个管理反馈早于首个 consumer 仍由真实后台 worker 处理；五个服务启动且 storage/worker 就绪；storage/relay/worker 非 root；运行时 DB 角色非超级用户；DB 与 broker 无主机端口；身份文件 0600、幂等、不重签；真实 HTTP 反馈→Relay→worker 注册/领取/启用并写自有系统记录；二次启动保留业务数据与 token；schema 不匹配通用 503 且不自动升级；worker 停止时 storage 健康不冒充其在线；broker、数据库宕机 503 与恢复；容器 CLI 有界 snapshot 清理 |
| 插件链 `services/storage/tests/test_plugin_chain_e2e.py` | 2 passed | 使用**固定的⑤测试驱动**，不是⑤真实调度 |
| 备份 `tools/storage_backup.py`（真实测试 `tests/test_storage_backup_real.py`） | 新版真实备份→恢复往返与五种真实竞争通过：`/tmp/ear-final-race-84lx9T/real.log` 为 **1 passed in 100.61 s**；汇总 `/tmp/ear-final-race-close.json`（`roundtrip.phase=passed`、`production_touched=false`、`cleanupFailures=[]`；`exits` 中 real、unit、suite、pyright、ruff、format、docs 均为 0，其中 `suite` 是该证据目录自己的检查，不是下一行的完整运行） | 五种竞争：`fresh`、`volume-create`、`compose-create`、`db-import`、`stopped`；每种均数据库未变、未被重启、清理由测试方持有。前四种中，其他项目的容器在拒绝后仍在运行；`stopped` 中，其他项目的容器本就停止并保持停止。校验通过：PostgreSQL 全部表哈希一致、NATS、身份 token 有效、运行时属主 `ear_storage` 且非超级用户、已存在目标与损坏 hash 均被拒绝、Relay 暂停与源服务状态保留。历史失败（`/tmp/ear-backup-main-final.log` exit 1，早于新版）已被取代，不是最新结果：定位为 Compose `create --no-recreate` 停止其他项目的容器（`systemd-repro` 复现项 exit 1：创建前 Running，创建后 kill/stop/die），现不再使用该命令。**仅隔离环境验证，不是生产运行手册**，交接要点见第 8 节 |
| 三开启本地完整运行 `/tmp/ear-repro-suite-ykxiUWmy/{pytest.log,exit}` | **已结束**：`exit` 为 0，`pytest.log` 末行 **1503 passed, 5 skipped, 1 warning in 890.20s (0:14:50)**；同目录 `static.exit` 为 0，`static.log` 为 pyright 0 errors 与文档检查通过（32 个 Markdown 文件，不代表人工审定） | `EAR_RUN_POSTGRES_TESTS`、`EAR_RUN_NATS_TESTS`、`EAR_RUN_BACKUP_TESTS` 均为 1（运行者记录，日志不含环境变量），`uv run pytest -q`。5 个 skip 均为 `memory` 参数、原因 `requires authenticated PostgreSQL`，与既有 `test_causality_context_guard.py` 的三处 skip 一致（`test_association_authorization_and_replay_without_finish` 的 `[memory-target]`、`[memory-primary]`、`[memory-secondary]`，`test_new_context_origin_permission_and_source_metadata_precedence[memory]`，`test_dependency_can_read_foreign_upstream_and_publish[memory]`）；日志只有汇总行，未逐项列出。对应运行时本分支的工作区（运行开始后无非 Markdown 文件被修改，已按修改时间核对）。**这是本地一次运行，有 5 个 skip，不是生产验收**；远程 CI 与人工审核尚待 |
| 提交与全量证据 | P5 已提交 9705b61；②适配器合并 84f20fe；部署切片已在本分支实现，提交状态以提交历史为准 | P5 之前完整运行中的唯一失败已修复，并被上一行的三开启本地完整运行（无失败）覆盖；远程 CI **尚未运行**，合并前人工审核（负责人兼 I）**尚未进行** |

## 1. 不变量

- **普通启动不迁移**：`up` 只启动五个运行服务；`migrate`、`bootstrap`、`init-identities` 只在 `init` profile 中、由操作者显式执行。`serve` 不自动迁移；schema 不匹配时 `/health/ready` 返回通用 503，不自动升级。
- **凭据不打印、不入库**：密钥只在 `deploy/.env`（不进仓库、0600），经 Compose secrets 挂入容器；[`deploy/runtime.py`](../../../deploy/runtime.py) 在容器内组装 DSN，失败只输出 `storage container configuration unavailable`；CLI 异常只输出类型与帧位置，不含消息。身份 token 只在 `identities` 卷的 `/run/secrets/ear/identities.env`（0600），`init-identities` 拒绝向 stdout 交付。
- 禁止：不带 `--quiet` 的 `docker compose config`、`config --environment`；`cat`/`docker cp` 身份文件或 `deploy/.env` 到终端；把 token 放进命令行参数、日志、issue、PR 或仓库。
- **`deploy/.env` 只许新建**：本文命令一律在 `noclobber` 下写入，文件已存在即失败并停止，不覆盖、不截断；本文不提供改写已有 `deploy/.env` 的命令。
- 只有 `storage` 发布 `127.0.0.1:${EAR_STORAGE_PORT}`；PostgreSQL、NATS 在 `internal` 网络，不映射主机端口。容器以 10001 运行、`cap_drop: [ALL]`、`no-new-privileges`。运行时 DB 角色 `ear_storage` 非超级用户，由 [`postgres-init.sh`](../../../deploy/postgres-init.sh) **只在空卷上**创建。

## 2. 首次隔离部署（bash）

**2.1 隔离命名**。每套部署用唯一项目名（小写字母、数字、`-`、`_`），卷、网络、容器都以它为前缀；不要用默认项目名，不要与已有项目重名。

```bash
cd /home/aytony/projects/Echo-As-Record
EAR_PROJECT=ear-storage-mytest            # 自选、唯一
dc() { docker compose -p "$EAR_PROJECT" --env-file deploy/.env -f deploy/compose.yaml "$@"; }
docker compose ls -a | grep -w "$EAR_PROJECT" && echo "项目名已存在，停止"
docker volume ls -q --filter "label=com.docker.compose.project=$EAR_PROJECT"   # 应为空
```

**2.2 私密生成 `deploy/.env`**。值经 bash 内建 `printf` 写入，不出现在进程参数或终端；镜像标签按项目区分，避免 `build` 覆盖其他部署的 `ear-storage:local`。同一检出只能有一个 `deploy/.env`；并行多套部署请用不同检出。**只许新建**：下面的命令在 `noclobber` 下写入，`deploy/.env` 已存在即失败、绝不覆盖或截断；不要改用 `>|`、`tee`、`cp`、`mv`、`sed -i` 或编辑器去改写已有文件。

```bash
git check-ignore -q deploy/.env || echo "deploy/.env 未被忽略，停止"
test -e deploy/.env && echo "已存在，先确认它属于哪套部署，停止"
(
  set -o noclobber                         # 只许新建：deploy/.env 已存在时重定向失败，不覆盖、不截断
  umask 077
  pg=$(openssl rand -hex 32); svc=$(openssl rand -hex 32); nt=n$(openssl rand -hex 32)
  printf 'EAR_POSTGRES_PASSWORD=%s\nEAR_SERVICE_DB_PASSWORD=%s\nEAR_NATS_TOKEN=%s\nEAR_STORAGE_IMAGE=ear-storage:%s\nEAR_STORAGE_PORT=18000\n' \
    "$pg" "$svc" "$nt" "$EAR_PROJECT" > deploy/.env
) || echo "未写入 deploy/.env（已存在或写入失败）：原文件保持不变，停止"
stat -c %a deploy/.env                     # 应为 600
ss -ltn | grep -q ':18000 ' && echo "端口占用，改 EAR_STORAGE_PORT"
```

NATS 2.11 的 token 须字母开头、其余为 URL 安全字符（上面用 `n` + hex）。PostgreSQL 卷初始化后再改 `EAR_SERVICE_DB_PASSWORD` 不会改库内角色密码（见第 4 节）。

**2.3 校验与构建**

```bash
dc config --quiet && dc build
```

**2.4 显式初始化（新卷上一次）**。`init-identities` 经 `depends_on` 依次执行 `migrate` → `bootstrap`（均须成功退出）→ 签发身份：

```bash
dc --profile init run --rm init-identities     # 只输出 {"status":"created"} 或 {"status":"unchanged"}
dc --profile init ps -a                        # migrate、bootstrap 应为 Exited (0)
```

- 重复执行对已有完整身份文件返回 `unchanged`，不替换、不重签、不轮换。
- `migrate` / `bootstrap` 的依赖容器以退出状态留在本项目中，可用 `dc --profile init rm -f migrate bootstrap` 清掉（不影响卷）。
- 签发两个身份：`EAR_ADMIN_TOKEN`（`api` 可信管理员，供未来⑥使用；本切片不启动 `api`）与 `EAR_MANAGEMENT_WORKER_TOKEN`（`manage-worker` 启动时由 `runtime.py` 从卷内读取）。把管理员 token 交付给操作者客户端的流程**本切片未定义、未验证**：不要读出；需要时按负责人兼 I 确定的方式经 stdin/环境传给客户端，不经命令行参数或日志。

**2.5 启动**

```bash
dc up -d          # 不含 init profile，不迁移、不签发
dc ps
```

## 3. 健康检查：API ready 与 worker 就绪不同

| 探针 | 200 的含义 | 不代表 |
|---|---|---|
| storage `GET /health/live` | 进程在响应 | 数据库、broker 可用 |
| storage `GET /health/ready` | 只读短事务内 schema revision 匹配、核心类型已登记（bootstrap 完成）、写入模块已存在，**之后**（关闭 DB 连接后）broker 校验成功；失败统一 503，不给原因 | relay、manage-worker 或其他 Ear 模块在线 |
| manage-worker `GET /health/ready`（容器内 8001，未发布） | 最近 5 秒内有一次成功轮询，且管理 consumer `ready AND enabled` | 仅仅进程存在；`/health/live` 只表示 HTTP 线程在 |
| relay | 无 Compose healthcheck；`relay-health` 只读数据库（`broker_probed=false`） | broker 可达 |

worker 停止时 storage 的 ready 仍可能是 200（冒烟已验证不会冒充 worker 在线）。判断“管理反馈能否被处理”看 worker 健康；管理动作 `status` 报告存储的生命周期状态，**不等于插件在线**。

```bash
PORT=$(sed -n 's/^EAR_STORAGE_PORT=//p' deploy/.env)       # 端口不是密钥
curl -fsS "http://127.0.0.1:$PORT/health/live"; echo
curl -sS -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:$PORT/health/ready"
for s in postgres nats storage manage-worker; do
  docker inspect -f "$s {{.State.Health.Status}}" "$(dc ps -q "$s")"
done
dc ps relay
dc run --rm --no-deps relay relay-health      # 只读数据库；输出字段未逐项核对，只在本机查看
```

## 4. 故障检查

| 现象 | 可能原因 | 检查与处理 |
|---|---|---|
| 容器立即退出，日志只有 `storage container configuration unavailable` | secret 为空/缺失；`manage-worker` 读不到有效身份文件（未初始化、项目名错导致卷不同、卷丢失） | `dc ps -a`；按项目标签列卷。新部署补做 2.4；**已有数据的部署不要在新卷上重新初始化**，见第 5 节 |
| live 200、ready 503 | 数据库或 NATS 不健康；schema 与镜像不匹配；未 bootstrap | `dc ps`、`dc logs --tail=50 postgres nats`。依赖恢复后 ready 自行恢复。schema 不匹配是预期的失败关闭：对要保留的数据**不要**仅为消除 503 去迁移，升级迁移需可用备份与负责人确认，目前不具备 |
| storage 日志为 `{"error": ..., "frames": [...]}` | CLI 异常（只含类型与位置） | 在一次性隔离环境复现；不要为排障临时打印密钥或 DSN |
| 改了 `EAR_SERVICE_DB_PASSWORD` 后连接失败 | `postgres-init.sh` 只在空卷执行，库内角色密码未变 | 恢复原值；改角色密码是显式维护，本文不提供 |
| `init-identities` 报 `operator recovery required; refusing automatic reissue` | 库内已有身份但文件缺失/不一致、文件非 0600、授权漂移，或文件来自另一套数据库 | 停止，保留全部卷，不删 principal、不删文件、不换项目名重试；交负责人恢复（无自动轮换） |
| NATS 不启动或不健康 | token 为空或格式不符 | 按 2.2 的格式（`n` + hex）准备新值；**不得覆盖已有 `deploy/.env`**（2.2 的命令在 noclobber 下会拒绝），本文不提供改写已有文件的命令，确需更换由操作者另行手工处理并先确认它属于哪套部署；NATS 卷已有数据时换 token 后需同步重启依赖服务 |
| storage ready 而 worker `unhealthy` | worker 崩溃/停止、管理 consumer 未就绪或未启用、broker 问题 | `dc ps manage-worker`、`dc logs --tail=50 manage-worker`、`dc restart manage-worker` |
| Outbox 积压 | 流满拒收（无清理，已知限制）或 broker 故障 | `relay-health`；容量与保留策略待与 I 确定 |
| 端口冲突 | `EAR_STORAGE_PORT` 已占用 | 改端口后 `dc up -d` |

## 5. 三套卷：何时必须一起保留

卷名为 `<项目名>_postgres_data`、`<项目名>_nats_data`、`<项目名>_identities`，三者是一套一致状态：PostgreSQL 有身份摘要、principal、Outbox、Relay fence 与 consumer 投递状态；NATS 有 JetStream 流与 durable consumer；`identities` 有与库内摘要对应的明文 token。

- **只要还需要这套部署的数据或身份，就三套一起保留**：`stop`/`restart`、重建镜像后 `up -d`、不带 `-v` 的 `down` 都保留卷（冒烟验证二次启动保留业务数据与 token）。
- 只丢 `identities`、保留数据库：worker 无法启动，`init-identities` 拒绝重签，需要人工恢复（本切片未提供流程）。
- 保留 `identities`、丢数据库：新库中找不到对应身份，`init-identities` 失败关闭。
- 只丢或回退 NATS 卷、或混用不同时点/不同项目的卷：与数据库中的投递状态不一致，**未验证、视为不支持**。
- 只有一次性测试部署才三套一起丢弃；不要按名称模式 `docker volume rm` / `prune`。

## 6. 停止与清理

停止并保留三套卷（可重复执行，之后 `dc up -d` 即可恢复）：

```bash
dc down                                          # 停止并移除容器，保留三套卷（不删卷）
```

**永久删除不提供可粘贴的命令块。** 删除卷、镜像或 `deploy/.env` 不可恢复，只适用于确认无需保留数据与身份的一次性测试部署，由操作者核对下列事项后**手工**选择并逐条自行输入，一次只做一条：

- `$EAR_PROJECT` 确是要销毁的那一套（用 `docker compose ls -a` 与按项目标签列卷核对）；三套卷一起处理，不按名称模式批量删除，不用 `prune`。
- 镜像 `ear-storage:<项目名>` 与 `deploy/.env` 只在对应卷已确认全部销毁、且不被其他部署使用之后才可手工删除。
- 有保留价值的部署（包括已做备份但尚未验证恢复的）一律不删。

## 7. 未做与待定

- 无生产启动、未做生产验收；部署切片已在本分支实现（提交状态以提交历史为准）但未合并；负责人兼 I 的合并前人工审核与远程 CI 均尚待。
- ⑥管理页对接；⑤实际调度对接（插件链测试用固定⑤驱动）。
- 保留/清理：JetStream 流容量与保留、幂等回执、`snapshot-sweep` 定时调度（目前仅手动 CLI）、重配置退役资源；NATS 按模块账户隔离。
- 备份文件的保留与删除（工具不提供）、备份/恢复的任何生产使用、升级迁移流程、管理员 token 交付流程、远程 CI 实跑与合并前人工审核（本地三开启完整运行已通过，见第 0 节，不能替代二者）。

## 8. 运维交接：备份与恢复（隔离环境验证，非生产手册）

交接给操作者的要点。工具 `tools/storage_backup.py` 与本节已在本分支实现（提交状态以提交历史为准），**未合并、无生产使用**；参数以 `--help` 为准，本节不提供逐步命令。

- **证据**：新版真实备份→恢复往返与五种真实同名竞争通过（第 0 节；`real.log` 为 1 passed in 100.61 s），覆盖 PostgreSQL、NATS、身份与数据库数据。只证明隔离测试环境，不证明生产。
- **历史故障（早于新版，已被取代）**：已定位为 Compose `create --no-recreate` 会停止其他项目的容器（不属于本工具）；现不再使用该命令（工具源码中已无该参数）。
- **备份**：必须 `--project <源项目>` 加 `--allow-downtime`，会停止源的应用与 NATS；加 `--resume` 才在持久化后重启原先运行的服务。`--bundle` 须是尚不存在的绝对路径，`--env-file` 须 0600；bundle 含真实身份密钥，按机密保管，不入仓库。
- **恢复**：只进入独立的新项目：`--new-project` 须是未使用的名字（无同名容器、卷、网络），`--confirm-new-project` 须与之完全相同。备份必须可信：须显式 `--trust-backup`（归档可执行 SQL，信任的是产出者而不是哈希）；manifest 的 SHA-256 经**独立渠道**取得并传给 `--trusted-manifest-sha256`，不从 bundle 里读，也不与 bundle 走同一渠道。
- **成功判定与启动**：成功必须读取命令输出的 JSON `.project`，并**手动**以该项目名启动 NATS 与应用（启动前自行核对所用 compose 与 env 文件）；`--start` 不接受。**不在原目标项目执行 `up`**。失败或中断时只保留本工具创建的资源、不触碰其他项目的资源，由操作者检查后处理。
- **`deploy/.env`**：任何写入都只许新建（noclobber），已存在即停止；不要为备份或恢复覆盖已有文件。
- **不要夸大**：无生产启动、未合并、负责人兼 I 审核未进行；原部署冒烟 16 项 PASS 与插件链 2 passed 结果不变；保留/清理（含备份文件保留与删除，工具不提供）、⑥管理页与⑤实际调度对接、升级迁移与管理员 token 交付流程仍未做。
- **三开启本地完整运行**（`/tmp/ear-repro-suite-ykxiUWmy/{pytest.log,exit}`）：已结束，`exit` 为 0，1503 passed, 5 skipped；5 个 skip 的说明见第 0 节。它是本地一次运行，不替代远程 CI 与人工审核，不是生产验收。

相关：[实现状态](progress.md)、[模块入口](../README.md)、[Compose](../../../deploy/compose.yaml)、[环境模板](../../../deploy/.env.example)、[健康探针](../src/ear_storage/health.py)、[身份初始化](../src/ear_storage/deployment.py)、[CLI](../src/ear_storage/cli.py)。
