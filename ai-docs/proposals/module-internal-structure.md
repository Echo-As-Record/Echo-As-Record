---
generated-by: Claude (AI)，应 prts 要求
generated-at: 2026-10-31
based-on: README.md (reviewed-at 2026-10-30), docs/design/design-v1.1.md (reviewed-at 2026-10-30), docs/design/signals-supplement.md (reviewed-at 2026-10-29), docs/design/work-breakdown-v1.1.md (reviewed-at 2026-10-30), docs/glossary.md (reviewed-at 2026-10-30), contracts/events/*.schema.json, commit 0a8b67d；课程「类型安全」lec3–lec5 与 backend-sample（Scala）
---

# 提案：模块内部的代码组织（纯计划 + 脏边缘）

> 未审定的工作文档。与 `README.md`、`docs/` 冲突时以它们为准。设计文档 0.2 节规定模块内部实现由负责人决定，本文只是给负责人的**可选参考**，不是约束。

## 1. 来源与核心思路

思路来自一门课的 Scala 后端样例（cats-effect + http4s）。那个样例把每个微服务写成：

- **请求即计划**：每个对外功能是一个不可变对象，字段就是入参，方法 `plan(conn)` 返回一个**还没执行**的 `IO[Response]`。计划本身是纯的，同样的输入总是得到同样的计划。
- **副作用推到边缘**：真正读写数据库、发网络请求的代码只在两处出现——表访问层，以及程序入口/框架。业务逻辑里没有全局变量、没有隐式状态。
- **可见性就是边界**：一个服务的表访问函数对别的服务不可见（Scala 的 `private[service]`），别的服务只能调它公开的功能。

样例中一个服务的目录：

| 目录 | 作用 |
|---|---|
| `objects/` | 领域类型：id、记录、枚举、编解码。纯数据，无行为 |
| `api/` | 每个文件一个「请求即计划」，业务判断都写在这里 |
| `routes/` | 注册表：本服务对外暴露哪些功能，各自的输入输出类型。不写逻辑 |
| `tables/` | 每张表一个子目录：读写 SQL + 建表初始化。唯一碰数据库的地方 |
| `utils/` | 服务内部复用、但不对外暴露的计划或纯函数 |

服务外面还有一层 `system/`（框架：统一入口、事务、错误映射）和 `Main`（唯一执行副作用的地方）。

## 2. Ear 已经定下的、和这个思路同向的约束

Ear 的硬约束（README 第 6 节）本身就是同一种思想在系统层面的版本，下表把对应关系列出来，便于理解为什么值得在模块内部也这样做：

| 课程思路 | Ear 已有约束 |
|---|---|
| 服务只能碰自己的表 | 每类数据只有一个模块负责写（设计 5.1） |
| 服务间只调公开功能 | 模块之间不互相下指令，只写 ①、只收 ① 的事件（例外：⑤ 的「请求重新判断」） |
| 计划纯、执行在边缘 | 事件只带 id 和版本，接收方查**当前状态**再决定；同一事件送达多次结果相同 |
| 不可变对象 | 原始数据只追加；语义信息修改即新增版本 |
| 注册表而非硬编码 | ⑤ 按登记表分发，不写死接收方 |

所以本提案只把这套约束**往模块内部延伸一层**，不新增系统级规则。

## 3. 建议的模块内部结构（Python 版）

本节先给所有模块共用的骨架和「计划」类型（3.1、3.2），再逐个写出 `services/` 下七个模块的预期结构（3.3～3.9）。各模块采用的程度不同：

| 模块 | 采用程度 | 原因 |
|---|---|---|
| ① `storage` | 不用 handler / registry，只取「纯判断 + 边缘执行」 | 不接收事件，是被调用的基础设施 |
| ② `adapter` | 轻量：解析是纯函数，扫描和写入在边缘 | 由扫描到的新文件驱动，不接收事件 |
| ③ `attribution` | **完整采用，建议作为试点** | 输入种类清楚，逻辑大多确定，一条反馈要写多条 |
| ④ `extraction` | 部分采用 | 主体是多轮大模型调用，只在调用前后和规则计算处用纯函数 |
| ⑤ `dependency` | **完整采用** | 处理条件、深度、去重、修改描述都是纯规则 |
| ⑥ `api` | 部分采用 | 主体是 HTTP 读写；监听事件的只有「执行外部动作」一处 |
| ⑦ `llm_gateway` | 不需要 | 无状态代理加调用记录 |

### 3.1 通用骨架

```
services/<目录>/
  README.md                 # 已审定，模块说明
  ai-docs/                  # 模块工作文档
  pyproject.toml            # uv workspace 成员
  src/ear_<模块名>/         # 模块名见术语表：storage、adapter、attribution……
    domain/                 # 本模块的类型（不可变）
    handlers/               # 每个文件处理一类输入，返回「计划」
    registry.py             # 本模块处理哪些事件、反馈、重新判断
    store/                  # 对 ① 接口的薄封装
    logic/                  # 模块内部复用的纯函数
    runtime.py              # 唯一执行副作用的地方
  tests/
```

各目录的规则：

- **`domain/`**：`@dataclass(frozen=True, slots=True)`；id、版本号用 `NewType` 区分（`PersonId`、`PlaceId`……），让 pyright 拦住「把地点 id 传给人物」；可能的结果用联合类型（`Matched | Unnamed | Skipped`），不用 `None` 或魔法字符串。和 `contracts/` 对应的事件类型不在各模块手写，统一放在 `libs/ear_contracts/`（见 3.2）。
- **`handlers/`**：输入是触发它的事件或反馈，加上 runtime 事先从 ① 读好的 `snapshot`；输出是一个「计划」（3.2）。handler 里不读写 ①、不调用 ⑦、不取当前时间、不生成 UUID，需要时由 runtime 放进 `snapshot` 或作为参数传入。需要多轮读写时（先读、调 ⑦、再读），拆成几个纯步骤，由 runtime 串起来。
- **`registry.py`**：只做声明，读一个模块先看它。审代码时对照设计 5.1 表和补充章节第 4 节，检查有没有处理不归自己的反馈。
- **`store/`**：把计划里的条目翻译成 ① 的接口调用，把 ① 的返回翻译成 `domain` 类型。**范围是「本模块负责写的数据」加上「本模块要写的系统记录」**：③、④ 都要登记依赖，④ 还要写提取记录，⑤ 要写传播记录。读取不受限制，任何数据都能读。
- **`logic/`**：纯函数。模块之间要共享的纯函数放 `libs/`，不跨模块 import 对方的 `logic/`。
- **`runtime.py`**：订阅总线 → 按 `registry` 找 handler → 读 ① 得到 `snapshot` → 调 handler 得到计划 → 通过 `store/` **整批提交** → 处理冲突（3.2）。README 第 7 节的公共事项也在这里统一做：结构化日志（`module`、`event_id`、`root_feedback_id`、`depth`）、调 ⑦ 时带模块名和 `root_feedback_id`、写入时原样带上 `root_feedback_id` 和 `depth`。

### 3.2 「计划」类型与幂等

handler 返回的不是单纯的「写入列表」，而是一个联合类型的元组：

```python
Effect = WriteData | RegisterDependency | WriteSystemRecord | EmitReevaluation
Plan = tuple[Effect, ...]

@dataclass(frozen=True, slots=True)
class WriteData:
    data_type: DataType
    data_id: DataId
    payload: Mapping[str, object]
    expected_version: int | None   # 修改：写入前应有的版本；新增：None
    dedup_key: str | None          # 新增：确定的去重键，例如 "unnamed_person:<voiceprint_id>"
    skip_history: bool = False     # 补充章节 3.4
```

- `root_feedback_id`、`depth` 不放在 `Effect` 里，由 runtime 从触发它的事件或请求统一填写，handler 不会漏带。
- `EmitReevaluation` 只有 ⑤ 会产生；`WriteSystemRecord` 用于依赖登记以外的系统记录（提取记录、传播记录等）。

**纯函数测得到什么、测不到什么。** 纯 handler 只保证「同样的事件 + 同样的状态，得到同样的计划」。真实系统里的幂等还取决于两件事，必须由 ① 的写入接口支持：

1. **整批原子提交。** 一条反馈常常要写多条：③ 执行纠正时，要从原对象的参考中移除、加入新对象的参考、修改归属关系。整个计划作为一批提交给 ①，要么全部成功，要么全部不写；成功时每条写入各自发出「数据库被修改」。
2. **冲突检测。** 修改带 `expected_version`，新增带 `dedup_key`。有任何一条冲突（版本已变、去重键已存在），① 拒绝整批，runtime **重新读取状态、重新调用 handler**（有次数上限）。

例：同一个「新增声纹 V」事件被几乎同时处理两次。两次读到的状态里 V 都还没有归属对象，两次都计划「新建未命名人物（`dedup_key = unnamed_person:V`）+ 写归属关系」。先提交的成功；后提交的因去重键冲突被拒，重读后看到 V 已有归属对象，handler 返回空计划。只靠「先查当前状态」做不到这一点。

**「内容不变就不写」以 ① 为准。** 工作分解规定这条规则只在 ① 实现一次。handler 可以提前判断、返回空计划，省一次调用，但这只是优化；验收看 ① 的行为。① 对每条写入返回结果（已写入 / 内容未变 / 已去重 / 冲突），runtime 据此记录日志。

### 3.3 ① 存储与事件 `storage`

① 不接收事件，所以没有 `handlers/` 和 `registry.py`。它最值得抽成纯函数的，是「一次写入应该怎样落库、发不发事件」这个判断。

```
src/ear_storage/
  domain/            # 记录、版本、写入请求、写入结果
  logic/
    decide_write.py  # (当前记录 | None, 写入请求) → Deduped | Unchanged | Conflict | Insert
    content_equal.py # 规范化后比较内容（md 产物逐字比较也用它）
    staleness.py     # 依赖登记的版本是否已过时
    permission.py    # 调用方模块能否写这个数据类型
  ownership.py       # 数据类型 → 负责模块（见 5.2 第 5 条）
  store/             # 表与 SQL：数据及其版本、依赖登记、系统记录、发件箱
  api/               # 对外读写接口
  relay.py           # 发件箱 → 总线
  runtime.py         # 启动 api 与 relay
```

- **`decide_write`** 是 ① 的核心，覆盖工作分解 ① 的大部分验收：同一原始数据写多次只存一条、内容没变不产生新版本、新版本把旧版本标记为 `superseded`。它是纯函数，可以表驱动测试。
- **写入成功就发事件、失败就不发**：建议用发件箱表（outbox）。一批写入和它们要发出的事件在同一个数据库事务里落库；`relay.py` 把发件箱里的事件转发到总线，至少送达一次。接收方本来就要求幂等，所以重复送达没有问题。
- **写入权限**：写入请求带调用方模块名，① 按 `ownership.py` 检查。这是「每类数据只有一个模块写」最可靠的一道防线，比在仓库里禁止跨模块 import 更直接（各模块本来就是独立进程）。
- 数据库、总线、事务的具体选型仍由 ① 负责人决定（设计 0.2）。

### 3.4 ② 适配器 `adapter`

② 由扫描到的新文件驱动，不接收事件。目录名是 `services/adapters/`，包名用模块名 `ear_adapter`。

```
src/ear_adapter/
  domain/          # 统一输入格式（RawRecord）
  scanner.py       # 发现 Octopus 目录中的新文件、确认已写完（副作用）
  parsers/
    audio.py       # 基础处理模块的输出 → 录音引用、转写段、局部说话人、声纹、环境声
    gps.py         # → gps_point
    phone_info.py  # → phone_info
  logic/           # 去重键、时间对齐、事件时间区间
  store/           # 按文件整批写入原始数据
  runtime.py       # 扫描 → 调用基础处理模块 → 解析 → 写入
```

- `parsers/` 都是纯函数：输入文件内容（或基础处理模块的输出），输出 `RawRecord` 列表。去重键、时间区间可以不碰文件系统直接测。
- 调用基础处理模块（外部仓库、固定版本）是副作用，放在 runtime。
- 一个文件产生的所有原始数据作为一批提交；扫描服务重启后重扫，靠 `dedup_key` 保证不重复。
- 用户反馈适配器属于 ⑥（设计 3.5），不在这里。

### 3.5 ③ 归属模块 `attribution`（建议的试点）

```
src/ear_attribution/
  domain/            # Person、Place、Attribution、Reference、匹配结果
  handlers/
    new_voiceprint.py          # 新增声纹且还没有归属对象
    new_gps_point.py           # 新增 GPS 点且还没有归属对象
    reevaluate_attribution.py  # 「请求重新判断」，目标是归属关系
    feedback/
      correct_attribution.py
      confirm_attribution.py
      edit_references.py
      create_place.py
      rename.py
      reattribute_range.py
  registry.py
  logic/
    match_voiceprint.py   # 声纹 → 人物；匹配不上 → 新建未命名人物
    match_gps.py          # GPS 点 → 地点；匹配不上 → UNKNOWN_PLACE_ID
    references.py         # 一条数据最多只作为一个对象的参考
    skip_history.py       # 补充章节 3.4 的标记规则
    unnamed_person.py     # 参考全部移走后的处理、「#N 还有 M 段声音」的数据
  store/
  runtime.py
```

`registry.py` 对应补充章节 4.1：

```python
HANDLERS: Mapping[Trigger, type[Handler]] = {
    Trigger.data_changed("voiceprint", "created"): NewVoiceprint,
    Trigger.data_changed("gps_point", "created"): NewGpsPoint,
    Trigger.feedback("correct_attribution"): CorrectAttribution,
    Trigger.feedback("confirm_attribution"): ConfirmAttribution,
    Trigger.feedback("edit_references"): EditReferences,
    Trigger.feedback("create_place"): CreatePlace,
    Trigger.feedback("rename"): Rename,
    Trigger.feedback("reattribute_range"): ReattributeRange,
    Trigger.reevaluate("attribution"): ReevaluateAttribution,
}
```

- **为什么适合试点**：大部分逻辑是确定的（匹配算法、参考规则、标记规则）；纠正、新建未命名人物都要一次写多条，3.2 的整批提交和冲突处理会最先在这里用上；3.2 那个「同一声纹被处理两次」的并发问题也会最先在这里暴露。
- **测试重点**：设计 6.2 的验收（纠正或确认过的数据，任何重新匹配都不改变结果）、一条数据最多作为一个参考、改名不引起重新归属，都可以在纯 handler 上表驱动测试。
- 依赖登记（归属关系依赖哪些候选对象的哪个版本）也作为计划里的 `RegisterDependency` 返回；登记哪些候选由负责人决定（设计 6.1）。

### 3.6 ④ 活动提取模块 `extraction`

④ 的主体是多轮大模型调用。整个提取流程写成一个 handler 会拆得很碎，所以只在三类地方用纯函数：**大模型调用前构造输入、调用后解析输出、以及不需要大模型的规则计算**。

```
src/ear_extraction/
  domain/            # Activity、Artifact、ActionSuggestion、Rule、ExtractionRun
  handlers/
    reevaluate_artifact.py     # 「请求重新判断」，目标是 md 产物
    feedback/
      extract_range.py
      edit_rule.py
      one_off_extraction.py
      apply_rule.py
      review_suggestion.py
      action_result.py
  registry.py
  pipeline/          # 提取流程的各步，每步 = build_*（纯）+ parse_*（纯）
    precheck.py      # 需要归属的数据是否都已有归属对象
    segment.py       # 划分活动
    select_rules.py  # 选规则
    extract.py       # 按规则生成 md 产物
    suggest.py       # 生成外部动作建议
  logic/
    scheduled_range.py   # 定时提取范围：去掉已成功提取的时间段（半开区间运算）
    suggestions.py       # md 产物有新版本时，作废未审核的旧建议、重新生成
    markup.py            # 检查 [[person:<id>]]、[[place:<id>]] 引用的 id 存在
  scheduler.py       # 内部定时器（副作用）
  store/
  runtime.py         # 串起 pipeline：读 → build → 调 ⑦ → parse → 写；前置检查的延后重试
```

- **大模型调用放在 runtime**，`pipeline/` 里只有调用前后的纯函数。CI 按 README 第 8 节用录制好的回复，测试整条流程也不调用真实模型。
- **`scheduled_range`** 直接对应工作分解 ④ 的验收「定时提取不漏时间段，也不覆盖已提取过的时间段」，是纯的区间运算，最适合先写测试。
- 重新判断时「结果没变就不写入」以 ① 的内容比较为准（3.2）。大模型输出能否做到逐字不变，是提示词和设计的问题，不是代码组织能解决的。

### 3.7 ⑤ 依赖分析模块 `dependency`

```
src/ear_dependency/
  domain/            # 依赖登记、传播记录、修改描述
  handlers/
    semantic_changed.py        # 语义信息被修改或标记为已被替代
    feedback/
      reevaluate_related.py    # 「同时重新判断其他相关记录」
  registry.py        # 本模块收哪些事件、执行哪些反馈
  logic/
    should_handle.py       # 补充章节 4.3 的处理条件表
    depth.py               # 深度计算（补充章节 3.3）与深度上限
    dedupe.py              # 避免同一条记录被重复请求重新判断
    change_description.py  # 由新旧两个版本生成「局部说话人 C：丙 → 丁」
    propagation.py         # 构造传播记录
  store/
  runtime.py
```

- handler 的计划由 `EmitReevaluation`（发给负责模块）和 `WriteSystemRecord`（传播记录）组成。
- **分发登记表是数据，不是代码**：「数据类型 → 负责模块」从配置读取，不在 handler 里写死接收方（工作分解 ⑤ 的验收）。它和 ① 的写入权限表可以是同一张，见 5.2 第 5 条。
- **测试重点**：`should_handle` 可以把补充章节 4.3 的表逐行写成测试用例；深度、去重、修改描述也都是纯函数。⑤ 的这部分不依赖 ① 的接口定稿，**现在就可以开始写**。
- 传播记录要求写明「结果有没有变化、在哪里停下」，目前还缺一个信号渠道，见第 6 节。

### 3.8 ⑥ 客户端与 API `api`

MVP 只做服务端 API（README 第 8 节）。

```
src/ear_api/
  domain/
  routes/
    read.py          # 时间线、活动、md 产物、人物、地点、规则、建议
    feedback.py      # 写入用户反馈
  feedback/
    build.py         # 界面操作 → 结构化用户反馈（纯），按 kind 校验
  handlers/
    suggestion_approved.py   # 建议变为「已通过」且尚未执行 → 计划执行外部动作
  executors/
    calendar.py      # 写入日历（副作用）
    reminder.py      # 写入提醒事项（副作用）
  logic/
    render_markup.py # 把引用标记换成当前的名字（纯）
  auth.py            # 单一访问令牌
  store/
  runtime.py
```

- 监听事件的 handler 只有 `suggestion_approved`（补充章节 4.4 的「必需」项）；其余都是 HTTP 请求处理。
- **外部动作不能执行两次**：「先查当前状态」防不住两次并发送达。⑥ 不能写建议（建议归 ④），所以 MVP 可以让同一条建议的事件在 ⑥ 内部串行处理；执行结果反馈的 `dedup_key` 用「建议 id + 版本」。一人一套部署，这样就够了。
- 反馈的格式由 ⑥ 与 ③、④ 共同约定（设计 5.2），`feedback/build.py` 跟着约定走。

### 3.9 ⑦ 大模型入口 `llm_gateway`

⑦ 是无状态代理加调用记录，不需要 handler 和 registry。

```
src/ear_llm_gateway/
  api.py             # 调用接口
  logic/
    validate.py      # 拒绝没带调用方模块的调用
    usage.py         # 按模块、按根源反馈统计
  providers/         # 对接反代平台；换模型、限流、重试
  recording/         # 录制与回放，供 CI 使用（README 第 8 节）
  store/             # 调用记录
  runtime.py
```

- 调用记录除了设计第 9 节要求的字段，建议再加一个**触发它的事件或请求 id**（例如 `trigger_event_id`）。只靠 `root_feedback_id`，只能把调用对应到「哪次用户操作」，对应不到传播记录里的「哪一步」，见第 6 节。

## 4. 好处与限度

**好处**

- **确定性可以直接单测**：「同样的事件 + 同样的状态，得到同样的计划」「内容没变时返回空计划」都在纯 handler 上测，不需要数据库和总线。
- **审代码有固定的看点**：先看 `registry`（处理了什么），再看 `store`（写了什么），对照设计 5.1 和补充章节第 4 节即可。
- **对 AI 友好**：目录名固定，靠名字就能定位；每个文件一个职责，改动范围小，便于审核。
- **便于并行开发**：handler 只依赖 `domain` 和 `snapshot`，① 的接口定稿前，可以用手工构造的 `snapshot` 先写先测。

**限度**

- 纯函数测试**不能替代**真实环境下的幂等测试。并发送达、整批提交失败、冲突后重算，都要在接上 ① 之后做集成测试（3.2）。
- 「内容不变就不写」「写入成功就发事件」的验收在 ①，不在各模块的 handler。
- ④ 的大模型输出是否稳定，不因为代码组织而改变。

## 5. 需要人决定的问题

### 5.1 要等 ① 定稿的接口

3.2 的「计划」类型要等这些接口定下来再对齐，建议 ① 的接口说明里明确回答：

1. 能否**整批原子写入**，成功时每条写入各自发事件。
2. 修改是否支持 `expected_version`、新增是否支持 `dedup_key`；冲突时是否**整批拒绝**。
3. 写入请求是否带**调用方模块名**，由 ① 检查写入权限。
4. 写入的返回值是否区分「已写入 / 内容未变 / 已去重 / 冲突」。
5. 依赖登记，以及查询「依赖某对象、且登记版本已过时」的接口。
6. 一次读取能否拿到**同一时刻**的状态（`snapshot` 的一致性）。
7. ⑤ 发出「请求重新判断」时，是直接发到总线，还是经由 ① 的发件箱，和传播记录在同一事务里（见第 6 节）。

### 5.2 起草人的倾向（供负责人参考）

1. **是否采用**：作为参考，不强制（设计 0.2）。③、⑤ 完整采用；其余模块至少做到「纯计算和副作用分开」，按 3.3～3.9 的程度取用。
2. **类型**：模块内部用 frozen dataclass。事件类型由 `contracts/` 的 JSON Schema 生成，放在 `libs/ear_contracts/` 共用；pydantic 只在需要的边界上用（例如 ⑥ 的 HTTP 入参）。
3. **静态检查**：主要防线放在 ① 的运行时写入权限检查（3.3）。禁止跨模块 import 只是辅助，如果要加，只改 ruff 配置，不需要改 README。
4. **包布局**：`services/<目录>/src/ear_<模块名>/`；workspace 成员按 `pyproject.toml` 注释里已有的 `libs/*`、`services/*`，建第一个模块时落地。
5. **登记表**：① 的写入权限表和 ⑤ 的分发登记表可以合成一张「数据类型 → 负责模块，能否重新判断」的表，由 ① 维护。MVP 里能重新判断的数据类型（归属关系 → ③，md 产物 → ④），负责重新判断的模块就是负责写它的模块；以后扩展模块接入也只需登记一次。需要 ①、⑤ 负责人决定。
6. **是否升格为 ADR**：先不升格。③ 试点跑通、团队同意定为约定后，再写成 `docs/adr/`。

### 5.3 建议的顺序

1. **现在**：⑤ 的 `logic/`（处理条件、深度、去重、修改描述）和 ④ 的 `scheduled_range`，都不依赖 ① 的接口，可以先写并配齐测试。
2. **① 回答 5.1 之后**：定下 `libs/ear_contracts/` 和 `Effect` 类型，在 ③ 试点完整结构，包括冲突重算的集成测试。
3. **③ 跑通后**：⑤、④、⑥ 按各自的程度跟进。

## 6. 与已审定文档的冲突检查

逐条对照了 README 第 6、7 节，设计 3.2、4.2、5.1、8.1、8.3、10.1，补充章节第 3、4 节，以及术语表。**本提案与已审定文档没有冲突**，没有修改任何已审定文档。

起草时发现以下几处已审定文档本身的空缺或不一致，只报告，不修改：

| 位置 | 问题 | 建议由谁决定 |
|---|---|---|
| 设计 8.1 第 5 步 | 传播记录要写明「结果有没有变化」，但 ③、④ 重新判断后结果没变时不写入、不发事件，⑤ 收不到任何信号。可选做法：负责模块在结果不变时写一条系统记录（例如「重新判断完成，结果不变」），⑤ 去读 | ⑤ 负责人与审定人；要改设计文档或 `contracts/` |
| 设计 8.3、第 9 节 | 要求传播记录能和它引起的大模型调用关联起来，但 ⑦ 的调用记录只要求带 `root_feedback_id`，只能对应到用户操作，对应不到传播的某一步（见 3.9） | ⑤、⑦ 负责人 |
| 补充章节第 2 节 | ⑤ 发出「请求重新判断」和写传播记录是两个动作，没有规定是否需要原子；若 ⑤ 在两者之间崩溃，传播记录会和实际发出的请求对不上（见 5.1 第 7 条） | ①、⑤ 负责人 |
| 术语表「待讨论」 | 写着「目前事件里原始数据的版本号为 `null`」，而设计 3.2 已定为原始数据版本号固定为 1，`data-changed.schema.json` 也要求 `new_version` ≥ 1。术语表这一条已过时 | 术语表审定人 |
