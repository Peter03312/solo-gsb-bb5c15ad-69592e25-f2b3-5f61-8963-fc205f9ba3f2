# Live Subtitle Finalizer — 直播字幕定稿服务

基于 FastAPI + SQLite 的直播字幕定稿服务。客户端按频道提交带**序号 / 修订号 / 文本**的字幕片段，服务将乱序到达的片段放入缓冲区，只从序号 1 起按**连续前缀**推进水位线并生成**不可再修改**的定稿字幕。操作员可对确认永久丢失的序号区间发起**缺口裁决**，服务在同一事务中记录缺口并释放紧随其后的连续缓冲片段。定稿之后如需改字，可通过**追加式更正层**提交不可变的更正版本——原始定稿永远保留，下游可在原始视图与生效视图之间选择；需要一次改多处时还可调用**批量更正接口**：单事务内逐项核对、全部通过才按输入顺序追加，任一项失败整批零写入，并以频道范围内的幂等键支持安全重试。需要让下游分批拉取**同一时刻的稳定字幕**时，可对频道**发布快照**：单事务锁定水位线并冻结 1..watermark 的原始 / 生效结果、缺口标记与各条更正版本，此后任何定稿、更正或缺口都不会改变既有快照；任意两个同频道快照还可经**快照差异接口**按 seq 合并比较，只返回新增 / 移除 / 内容变更的条目。快照内容如需合规输出，可调用**快照敏感词遮盖**：基于冻结生效文本在整个快照上按码点精确匹配词表，命中码点替换为 `*` 后分页返回，全程只读。需要把冻结字幕套用到剪辑时间轴时，可调用**剪辑字幕导出**：提交全部字幕的源区间 `timings` 与有序剪辑片段 `clips`，服务把每条字幕与每个片段求交集、平移到成片累计时间后返回 cue 列表或直接输出 WebVTT 文件，全程只读。需要把旧快照里定位过的一段引用迁移到新快照时，可调用**快照字幕引用迁移**：传入源 / 目标快照及源引用的起止字幕序号与 Unicode 码点偏移（终点不含），服务按源快照冻结生效文本提取非空引用（可跨相邻字幕、不得越过缺口），再在同频道目标快照的连续字幕文本中查找完整引用——恰好一次返回对应起止序号 / 偏移，零次返回 `missing`，多次返回 `ambiguous`，全程只读。拿到定稿与稿件后需要逐行校稿时，可调用**稿件校稿对齐**：提交 1～100 条有序稿件行及每行最多合并的字幕数（1～3），服务只读快照冻结的生效文本，以 Unicode 码点编辑距离为匹配代价，用动态规划求稿件行与字幕的全局最小代价顺序对齐（每行可匹连续 1～3 条、可跳过行或字幕、不跨缺口不复用），返回每行起止序号与距离、未匹配的稿件行与字幕序号，全程只读。下游也可以注册**命名消费者拉取游标**：每个命名消费者在频道内持有独立的已确认游标，领取接口在事务中返回游标后的连续批次（含缺口标记）并签发带过期时间的租约令牌，确认后游标才推进；租约过期未确认的批次可被重新领取，实现至少一次交付。

## 语义规则

- 片段可乱序到达；同一序号只接受**更高修订号**（更低或相等的修订号被拒绝）。
- 水位线 = 已定稿的最大连续序号（定稿字幕与永久缺口共同构成连续前缀）。遇到缺口即停，**不会越过缺口**定稿。
- 缺口被补齐的提交会在**同一事务**内一次释放整段连续片段。
- 序号 ≤ 当前水位线的提交一律不生效（定稿不可变，已裁决为缺口的序号同样不可再提交）。
- 所有提交 / 裁决在 `BEGIN IMMEDIATE` 事务内串行执行，并发下不会重复定稿或跳过缺口。

### 永久缺口裁决规则

- 区间 `[start_seq, end_seq]`（含两端）的**起点必须等于当前水位线下一位**；
- 区间内**不得存在已缓冲片段**（有缓冲说明片段只是迟到，应等待定稿而不是裁决丢失）；
- 任一校验失败返回 `applied=false` 且**不产生任何写入**；
- 裁决成功：同一事务内记录被跳过序号、推进水位线到区间终点，并继续释放紧随其后的连续缓冲片段；
- 重复提交**已完整裁决的同一区间**幂等返回当前结果（`idempotent=true`）；与既有裁决**部分重叠**则拒绝（`gap_overlap`）。

### 追加式更正规则

- 定稿不可改写；需要修改已定稿字幕时只能**追加**一条更正，原始文本永久保留。
- 仅**已定稿字幕**可被更正；未定稿序号与永久缺口序号一律拒绝（409）且不写入。
- 每个序号的更正版本从 1 起单调递增；提交时必须携带 `expected_version`，无更正时为 `0`，否则须等于当前最新版本。
- 版本不匹配（含并发竞争失败）返回 409 `version_conflict`；校验与追加在同一 `BEGIN IMMEDIATE` 事务内原子完成。
- 定稿查询默认 `view=original`（响应结构不变）；`view=effective` 合并最新更正文本并返回每条字幕的 `correction_version`。
- 更正历史可通过审计接口按版本升序查询。

### 批量更正规则

- 接口 `POST /channels/{channel}/corrections/batch` 一次接收 **1～100 条**更正，每项含 `seq`、`expected_version`、`text`、`reason`，请求顶层另带频道范围内的 `idempotency_key`。
- **同一批内 `seq` 不得重复**；出现重复时整批拒绝（409 `batch_conflict`，重复项标记 `duplicate_seq`）。
- 「逐项核对（序号确为已定稿字幕、不是永久缺口、`expected_version` 等于该序号当前版本）→ 按输入顺序追加各序号的新更正版本 → 记录批次」全部在**单个 `BEGIN IMMEDIATE` 事务**内完成；**任一项失败，409 响应的 `conflicts` 都按输入顺序给出各项冲突原因，整批零写入**（即使批次中其他项本身合法也不追加）。
- 全部通过时按**输入顺序**追加，返回批次号 `batch_id` 与逐项 `results`（含每项的下标、新 `correction_version` 等）。
- **幂等**：相同频道、相同 `idempotency_key` 且请求完全相同（逐项字段与顺序一致）的重试原样返回原批次结果（同一 `batch_id`、`idempotent=true`），不会重复追加版本；键相同而**内容不同**返回 409 `idempotency_key_conflict`。幂等键按频道隔离；被拒绝的失败批次不占用幂等键。
- 批量与**单条更正接口**、定稿原文（`view=original`）及更正审计互不影响，可混用：同一序号经两种方式追加的版本仍连续递增。

### 发布快照规则

- 创建快照在**单个 `BEGIN IMMEDIATE` 事务**内完成「锁定频道当前 watermark → 固化 `1..watermark` 的字幕（原始文本 + 创建时刻的生效文本）与缺口标记 → 冻结各字幕序号当时存在的全部更正版本」，冻结结果与创建时刻的定稿状态严格一致。
- 快照**整体不可变**：创建之后新定稿的字幕、新追加的更正或新裁决的缺口只写入原表，任何既有快照的内容（含分页与更正历史）都不随之变化。
- 同一频道携带相同 `idempotency_key` 重试始终返回**同一个 `snapshot_id`**（响应 `idempotent=true`），不重新冻结；幂等键按频道隔离（不同频道可复用同一字符串）。
- 不存在的频道也可创建 `watermark=0` 的空快照（不创建频道行）；之后可分页拉到空列表。
- 快照表只追加，存储层触发器禁止任何 `UPDATE` / `DELETE`。

### 快照差异规则

- 只允许比较**同一频道**的两个既有快照：任一快照不存在返回 404，频道不一致返回 409，比较本身为只读操作，不产生任何写入。
- 差异基于快照**冻结的生效内容**：字幕以生效文本与 `correction_version` 判定，缺口以裁决区间端点判定；两侧完全一致的序号不返回。
- 两份快照按 `seq` 升序合并，差异分 `added` / `removed` / `changed` 三类；`limit` 只计差异条目，`next_after` 指向本页最后一条差异，稀疏差异不会漏项。

### 快照敏感词遮盖规则

- `POST /snapshots/{snapshot_id}/redactions` 接收 `words`（敏感词表）、`after`（序号游标，默认 0）与 `limit`（1~100，默认 100）；词表按精确相等**去重后须有 1~20 项**，每项 **1~128 个 Unicode 码点**，非法参数返回 422，快照不存在返回 404。
- 遮盖基于快照**冻结的生效文本**（与 `view=effective` 一致），是**纯只读**计算：快照与当前定稿 / 更正均不被修改。
- 匹配在**整个快照**上进行：相邻字幕（中间没有缺口）按序号**无分隔拼接**，跨字幕（含跨页）的词语完整命中；**缺口阻断拼接**，跨缺口的词语绝不命中。
- 匹配**区分大小写、逐码点精确、不做归一化**；所有词的重叠命中都参与，覆盖区间取并集，**每个被覆盖码点替换为一个 `*`**（遮盖后码点数不变），再按各字幕原码点长度拆回原序号；未命中文字与缺口标记原样保留。
- 先算好完整快照的遮盖，再返回 `after` 之后至多 `limit` 条字幕或缺口；还有后续条目时 `next_after` 为本页末序号，否则为 `null`。

### 剪辑字幕导出规则

- `POST /snapshots/{snapshot_id}/edits` 接收 `timings`（每条字幕的源时间轴区间：`seq` / `start_ms` / `end_ms`）与有序 `clips`（剪辑片段：`start_ms` / `end_ms`）；所有时间均为**非负整数毫秒**、**左闭右开**且**起点小于终点**，非法输入返回 422，快照不存在返回 404。
- `timings` 须**恰好覆盖快照内全部字幕 seq**：缺字幕序号、序号重复或包含快照中不存在的序号（缺口序号 / 超界序号）都返回 422 `timings_mismatch`；空快照须传空 `timings`。
- `clips` 可**重排、重复或重叠**，按输入顺序首尾拼接为成片时间轴（第 i 个片段的成片偏移 = 此前所有片段时长之和）。
- 每条字幕与每个 clip 取**左闭右开交集**，非空才生成 cue，再平移到成片累计时间：一条字幕横跨多个 clip 时**按交集拆分**为多条 cue；重复 clip 在不同偏移处**保留副本**；时间重叠的字幕各自生成 cue，**不合并**。
- 导出基于快照**冻结的生效文本**（与 `view=effective` 一致），是**纯只读**计算：快照与当前定稿 / 更正均不被修改。
- 默认 `format=json` 返回结构化 cue 列表（`seq`、`clip_index`、`text`、映射后的 `start_ms` / `end_ms`），按 **(start_ms, clip_index, seq) 升序**；`format=vtt` 直接输出**合法 WebVTT** 文档（`text/vtt`），正文转义 `&` `<` `>`。

### 快照字幕引用迁移规则

- `POST /snapshots/{snapshot_id}/quote-migrations` 请求体携带 `target_snapshot_id` 及源引用的 `start_seq` / `start_offset` / `end_seq` / `end_offset`（序号区间含两端，偏移为序号内 **Unicode 码点**偏移、**左闭右开**，`end_offset` 允许等于末条字幕的码点长度）；任一快照不存在返回 404，两个快照分属不同频道返回 409。
- 服务按**源快照冻结的生效文本**（与 `view=effective` 一致）提取引用：引用可跨越**相邻字幕**（中间没有缺口，按序号无分隔拼接），但**不得越过缺口**；源区间内出现缺口标记、序号超出源快照水位线、偏移超出对应字幕文本长度，均返回 422 `invalid_quote_range`。
- 提取结果必须**非空**：同一字幕内 `start_offset` 必须小于 `end_offset`；跨字幕区间在拼接后为空（起止偏移都落在字幕边界等零宽情形）同样返回 422。中间字幕为空时引用仍可正常跨越（空字幕属于同一段）。
- 提取出的引用在**目标快照的连续字幕文本**中逐码点精确查找（相邻字幕无分隔拼接、**缺口阻断**、区分大小写、不做归一化），命中可跨字幕，重叠出现也分别计数：
  - 恰好一次命中：`status="found"`，`match` 返回引用在目标快照中的起止序号与码点偏移（终点不含；终点恰落在字幕边界时归属引用最后一个码点所在字幕）；
  - 零次命中：`status="missing"`，`match=null`；
  - 两次及以上命中（含跨缺口分段的重复）：`status="ambiguous"`，`match=null`，**绝不任选一个位置**。
- 迁移是**纯只读**计算：两个快照与当前定稿 / 更正均不被修改，重复调用结果稳定。

### 稿件校稿对齐规则

- `POST /snapshots/{snapshot_id}/script-alignments` 请求体含 1～100 条**有序、非空**稿件行 `lines`，每行带 `text`（去除首尾空白后不得为空）与 `max_subtitles`（本行最多合并的连续字幕数，**1～3**）；参数非法返回 422，快照不存在返回 404。
- 对齐**只读快照冻结的生效文本**（与 `view=effective` 一致），是纯只读计算：快照与当前定稿 / 更正均不被修改。
- 每行可与连续 **1～`max_subtitles` 条**字幕匹配，字幕块按序号**无分隔拼接**（中间不得有缺口 / 空洞，故**不得跨缺口**）；字幕块之间严格按稿件行序与字幕序对齐，**字幕不得跨块复用**。
- 匹配代价为稿件行与字幕块拼接文本之间的 **Unicode 码点编辑距离（Levenshtein，逐码点、不做归一化）**；稿件行与字幕都可被跳过，**跳过代价为相应文本的码点数**。
- 服务用动态规划求**全局最小代价顺序对齐**；总代价相同时依次按以下规则择优：① **跳过项更少**（跳过稿件行与跳过字幕合计）；② **合并字幕更少**（Σ(合并数−1) 更小，即能 1:1 就不合并）；③ **匹配序号字典序更小**（把每个匹配块的 `(start_seq,end_seq)` 依次按序列比较）。
- 响应逐行给出 `matched`、`start_seq` / `end_seq`、`subtitles` 与 `distance`，另以 `unmatched_script_lines`（被跳过稿件行下标，从 0 起）与 `unmatched_subtitle_seqs`（被跳过字幕序号）列出未匹配项；空快照（无字幕）时所有稿件行被跳过，代价为各行码点数之和。


### 命名消费者拉取游标规则

- 每个 `(channel, consumer)` 维护独立的已确认游标 `cursor`（初始 0）与至多一个未过期租约；消费者之间互不影响。
- **领取（claim）** 在单个 `BEGIN IMMEDIATE` 事务内完成「读取游标与现有租约 → 校验 → 截取连续批次 → 写入新租约」：从 `cursor` 后返回至多 `limit` 条的连续批次（字幕与缺口标记按序混合），并签发带过期时间的 `lease_token`；同一消费者持有未过期租约时重复领取返回 409 `lease_active`。
- **确认（ack）** 必须同时携带 `lease_token` 与 `expected_cursor`，且 `new_cursor` 必须落在本批次序号范围内（允许部分确认）；过期（`lease_expired`）、旧令牌（`lease_token_mismatch`）、越界（`cursor_out_of_range`）、并发落后（`cursor_conflict`）或无租约（`no_active_lease`）一律返回 409 且**不改写任何状态**。
- 确认成功后游标推进到 `new_cursor`、租约清空，下一次领取从新游标后继续；租约过期未确认时同一批次可被重新领取（内容相同、签发新令牌，旧令牌随之失效），实现至少一次交付。
- 无新内容时领取返回空批次且不签发租约（`lease_token=null`）。

## 快速启动（单条命令）

```bash
docker compose up --build
```

服务监听 **8000** 端口（容器内 `uvicorn --host 0.0.0.0 --port 8000`，Compose 映射 `8000:8000`），启动时自动初始化数据库（建表幂等，包括更正层、快照层与消费者游标表及触发器，旧实例重启自动补建）。本机访问入口：

- 服务根 / 交互文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health （或 http://localhost:8000 ）
- 提交片段：`POST /channels/{channel}/fragments`
- 缺口裁决：`POST /channels/{channel}/gaps`
- 定稿查询（`view=original|effective`）：`GET /channels/{channel}/finalized`
- 追加更正：`POST /channels/{channel}/finalized/{seq}/corrections`
- 批量更正（幂等）：`POST /channels/{channel}/corrections/batch`
- 更正审计（版本升序）：`GET /channels/{channel}/finalized/{seq}/corrections`
- 创建发布快照：`POST /channels/{channel}/snapshots`
- 分页拉取快照（`view=original|effective`）：`GET /snapshots/{snapshot_id}/items`
- 快照内更正历史（版本升序）：`GET /snapshots/{snapshot_id}/items/{seq}/corrections`
- 比较两个快照的差异：`GET /snapshots/{snapshot_id}/diff?other={other_id}`
- 快照敏感词遮盖：`POST /snapshots/{snapshot_id}/redactions`
- 剪辑字幕导出（`format=json|vtt`）：`POST /snapshots/{snapshot_id}/edits`
- 快照字幕引用迁移：`POST /snapshots/{snapshot_id}/quote-migrations`
- 稿件校稿对齐：`POST /snapshots/{snapshot_id}/script-alignments`
- 领取消费者批次：`POST /channels/{channel}/consumers/{consumer}/claims`
- 确认消费者批次：`POST /channels/{channel}/consumers/{consumer}/acks`
- 查询消费者状态：`GET /channels/{channel}/consumers/{consumer}`

## 配置

| 环境变量   | 默认值                 | 说明               |
| ---------- | ---------------------- | ------------------ |
| `DB_PATH`  | `/data/subtitles.db`   | SQLite 数据库文件路径 |

Compose 默认将 `/data` 挂载到命名卷 `subtitles-data`，容器重启数据不丢失。端口映射可在 `docker-compose.yml` 的 `ports` 中调整。

依赖除 FastAPI / Uvicorn / Pydantic 外，默认安装 [`rapidfuzz`](https://github.com/maxbachmann/RapidFuzz)（C 实现的 Levenshtein，稿件校稿对齐用其 `score_cutoff` 带状早停加速）。运行环境缺少该库时服务仍可正常启动，对齐接口自动回退到等价的纯 Python 带状动态规划（结果完全一致，仅在大规模输入下更慢）。

## API

### 提交字幕片段

```bash
curl -X POST http://localhost:8000/channels/news/fragments \
  -H 'Content-Type: application/json' \
  -d '{"seq": 1, "revision": 1, "text": "各位观众晚上好"}'
```

响应（`applied` 表示本次提交是否生效，`watermark` 为处理后的最新水位线，`finalized` 为本次新定稿的片段）：

```json
{
  "channel": "news",
  "seq": 1,
  "applied": true,
  "reason": null,
  "watermark": 1,
  "finalized": [{"seq": 1, "revision": 1, "text": "各位观众晚上好"}]
}
```

`applied=false` 时 `reason` 取值：

- `stale_revision` — 修订号不高于已缓冲的同序号片段；
- `already_finalized` — 该序号已定稿（或已裁决为永久缺口），不可再修改。

### 裁决永久缺口

当确认某些序号永久丢失（例如推流中断），操作员可对缺失区间发起裁决：

```bash
curl -X POST http://localhost:8000/channels/news/gaps \
  -H 'Content-Type: application/json' \
  -d '{"start_seq": 3, "end_seq": 4}'
```

裁决成功的响应（`watermark` 为裁决后的新水位线，`skipped` 为本次跳过区间，`finalized` 为随裁决一并释放的连续缓冲片段）：

```json
{
  "channel": "news",
  "applied": true,
  "idempotent": false,
  "reason": null,
  "watermark": 6,
  "skipped": {"start_seq": 3, "end_seq": 4},
  "finalized": [
    {"kind": "subtitle", "seq": 5, "revision": 1, "text": "第五条字幕", "start_seq": null, "end_seq": null},
    {"kind": "subtitle", "seq": 6, "revision": 1, "text": "第六条字幕", "start_seq": null, "end_seq": null}
  ]
}
```

重复提交**完全相同**的已裁决区间时，幂等返回原裁决结果，`idempotent` 为 `true`。`applied=false` 时 `reason` 取值：

- `start_not_at_watermark` — 区间起点不是当前水位线下一位（不能跳过未裁决的序号）；
- `buffered_fragments` — 区间内存在已缓冲片段，应等待其定稿；
- `already_finalized` — 区间已完全定稿且不匹配任何缺口；
- `gap_overlap` — 与既有缺口裁决部分重叠（完全相同的区间才允许幂等提交）。

请求体校验（如 `start_seq < 1`、`end_seq < start_seq`）返回 HTTP 422。

### 查询频道定稿

定稿查询按序号升序**混合返回字幕与缺口标记**，`kind` 字段供下游区分丢失内容。通过 `view` 参数在原始文本与更正后文本之间选择，**默认 `original`，响应结构与旧版完全一致**：

```bash
curl 'http://localhost:8000/channels/news/finalized?after=0&limit=1000&view=original'
```

```json
{
  "channel": "news",
  "watermark": 6,
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕", "start_seq": null, "end_seq": null},
    {"kind": "subtitle", "seq": 2, "revision": 1, "text": "第二条字幕", "start_seq": null, "end_seq": null},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4},
    {"kind": "gap",      "seq": 4, "revision": null, "text": null, "start_seq": 3, "end_seq": 4},
    {"kind": "subtitle", "seq": 5, "revision": 1, "text": "第五条字幕", "start_seq": null, "end_seq": null},
    {"kind": "subtitle", "seq": 6, "revision": 1, "text": "第六条字幕", "start_seq": null, "end_seq": null}
  ]
}
```

- `kind=subtitle`：定稿字幕，取 `revision` / `text`；
- `kind=gap`：永久缺口标记，`start_seq` / `end_seq` 标明所属裁决区间（同一区间内每个被跳过序号各占一条标记）；
- `after` 用于增量拉取，对字幕与缺口标记统一按序号计数。

`view=effective` 时，每个字幕条目合并其**最新更正文本**，并附带该序号的当前 `correction_version`（`0` = 未更正；缺口条目为 `null`）；顶层 `correction_version` 是本页返回范围内出现过的最大更正版本。原始定稿不受任何影响：

```bash
curl 'http://localhost:8000/channels/news/finalized?view=effective'
```

```json
{
  "channel": "news",
  "watermark": 6,
  "view": "effective",
  "correction_version": 1,
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕（已更正）", "start_seq": null, "end_seq": null, "correction_version": 1},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4, "correction_version": null}
  ]
}
```

非法的 `view` 值返回 HTTP 422。

### 追加更正（不可变更正层）

定稿字幕如有错字 / 翻译问题，可对**单个已定稿序号**追加更正。更正不修改任何旧数据，只向 `corrections` 表追加一个新版本：

```bash
curl -X POST http://localhost:8000/channels/news/finalized/1/corrections \
  -H 'Content-Type: application/json' \
  -d '{"expected_version": 0, "text": "各位观众晚上好", "reason": "原稿漏字"}'
```

请求字段：

- `expected_version`（≥0，必填）：客户端认为的该序号**当前更正版本**。没有更正时当前版本为 `0`，首个更正必须提交 `0`；此后必须等于最新版本号；
- `text`（非空字符串）：更正后的字幕文本；
- `reason`（非空字符串）：更正原因，随审计记录永久保留。

成功响应（原子追加下一版本，返回该序号新的更正版本）：

```json
{
  "channel": "news",
  "seq": 1,
  "correction_version": 1,
  "text": "各位观众晚上好",
  "reason": "原稿漏字",
  "created_at": "2026-09-20T12:00:00.000Z"
}
```

规则：

- **仅允许更正已定稿字幕**：未定稿序号（尚未进入连续前缀）或已裁决为永久缺口的序号一律拒绝，返回 HTTP 409 且**不产生任何写入**；
- **乐观版本控制**：`expected_version` 与当前版本不一致时返回 HTTP 409 `version_conflict`，客户端应重新拉取生效视图 / 审计记录后携带新版本重试；
- 「读取当前版本 → 校验 → 追加下一版本」在单个 `BEGIN IMMEDIATE` 事务内完成，并发竞争请求中只有一个成功，其余全部得到版本冲突；
- 更正只追加：每个 `(channel, seq)` 的版本号从 1 起严格递增、无空洞；存储层触发器禁止 `UPDATE` / `DELETE`，**原始定稿文本永远不被改写**，随时可用 `view=original` 取回。

409 响应体（更正接口）：

| `error`             | 含义                                                    |
| ------------------- | ------------------------------------------------------- |
| `not_finalized`     | 序号未定稿（频道不存在 / 序号还在缓冲区或水位线之外）   |
| `seq_is_gap`        | 序号已裁决为永久缺口，缺口不可更正                      |
| `version_conflict`  | `expected_version` 与当前更正版本不一致（竞争失败）     |

```json
{"error": "version_conflict", "message": "expected_version=0 与当前更正版本 1 不一致"}
```

### 查询更正审计

按更正版本**升序**返回某定稿序号的全部更正记录（不可变、可追溯）：

```bash
curl http://localhost:8000/channels/news/finalized/1/corrections
```

```json
{
  "channel": "news",
  "seq": 1,
  "current_version": 2,
  "items": [
    {"channel": "news", "seq": 1, "version": 1, "text": "各位观众晚上好", "reason": "原稿漏字", "created_at": "2026-09-20T12:00:00.000Z"},
    {"channel": "news", "seq": 1, "version": 2, "text": "各位观众朋友们晚上好", "reason": "主持人改口", "created_at": "2026-09-20T12:05:00.000Z"}
  ]
}
```

没有更正的定稿序号返回 `current_version=0` 与空 `items`；缺口 / 未定稿 / 不存在的序号返回 HTTP 404。

### 批量更正（单事务、可幂等重试）

需要一次性更正多条已定稿字幕时，调用批量接口。每批 1～100 条，服务在**单个写事务**内逐项核对：任一项不满足条件都整批拒绝、零写入；全部通过才按输入顺序追加各序号的新版本。

```bash
curl -X POST http://localhost:8000/channels/news/corrections/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "idempotency_key": "fix-2026-09-26T20:00",
    "corrections": [
      {"seq": 3, "expected_version": 0, "text": "第三条（批量更正）", "reason": "错字"},
      {"seq": 1, "expected_version": 0, "text": "第一条（批量更正）", "reason": "译名统一"},
      {"seq": 2, "expected_version": 0, "text": "第二条（批量更正）", "reason": "标点"}
    ]
  }'
```

成功响应（`results` 顺序与请求数组一致，`index` 为项下标；本例按输入顺序处理，与序号大小无关）：

```json
{
  "batch_id": "6f2a1c9e-....",
  "channel": "news",
  "idempotency_key": "fix-2026-09-26T20:00",
  "idempotent": false,
  "created_at": "2026-09-26T20:00:00.000Z",
  "results": [
    {"index": 0, "seq": 3, "correction_version": 1, "text": "第三条（批量更正）", "reason": "错字", "created_at": "2026-09-26T20:00:00.000Z"},
    {"index": 1, "seq": 1, "correction_version": 1, "text": "第一条（批量更正）", "reason": "译名统一", "created_at": "2026-09-26T20:00:00.000Z"},
    {"index": 2, "seq": 2, "correction_version": 1, "text": "第二条（批量更正）", "reason": "标点", "created_at": "2026-09-26T20:00:00.000Z"}
  ]
}
```

**幂等重试**：相同频道、相同 `idempotency_key` 且请求体逐项字段与顺序完全相同时，重发始终返回同一批次结果（`batch_id` 不变、`idempotent=true`），即使相关序号的更正版本此后又被推进也不会误判冲突、不会重复追加：

```json
{
  "batch_id": "6f2a1c9e-....",
  "channel": "news",
  "idempotency_key": "fix-2026-09-26T20:00",
  "idempotent": true,
  "created_at": "2026-09-26T20:00:00.000Z",
  "results": [ /* 与首次完全一致的逐项结果 */ ]
}
```

相同频道复用同一 `idempotency_key` 但**内容不同**（任何一项的 seq / expected_version / text / reason 或输入顺序不同）返回 409：

```json
{"error": "idempotency_key_conflict", "message": "频道 news 的幂等键 fix-2026-09-26T20:00 已被内容不同的请求占用，请更换幂等键后重试"}
```

**整批拒绝**：同批出现重复 `seq`，或任何一项未定稿 / 为缺口 / 版本不符时，返回 409 `batch_conflict`，`conflicts` 按输入顺序列出**所有**冲突项，且整批零写入（合法项也不会落库）：

```bash
curl -X POST http://localhost:8000/channels/news/corrections/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "idempotency_key": "fix-bad",
    "corrections": [
      {"seq": 2, "expected_version": 0, "text": "通过项但不会写入", "reason": "r"},
      {"seq": 99, "expected_version": 0, "text": "未定稿", "reason": "r"},
      {"seq": 1, "expected_version": 0, "text": "版本已落后", "reason": "r"},
      {"seq": 3, "expected_version": 0, "text": "缺口序号", "reason": "r"},
      {"seq": 2, "expected_version": 0, "text": "批内重复 seq", "reason": "r"}
    ]
  }'
```

```json
{
  "error": "batch_conflict",
  "message": "4 个更正项未通过校验，整批拒绝且无任何写入",
  "conflicts": [
    {"index": 1, "seq": 99, "error": "not_finalized", "message": "序号 99 尚未定稿，只有已定稿字幕允许更正"},
    {"index": 2, "seq": 1, "error": "version_conflict", "message": "第 2 项 expected_version=0 与序号 1 的当前更正版本 1 不一致"},
    {"index": 3, "seq": 3, "error": "seq_is_gap", "message": "序号 3 已裁决为永久缺口，不能更正"},
    {"index": 4, "seq": 2, "error": "duplicate_seq", "message": "序号 2 在本批中重复：与第 0 项（下标从 0 起）序号相同"}
  ]
}
```

`conflicts[].error` 取值：

| `error`             | 含义                                                              |
| ------------------- | ----------------------------------------------------------------- |
| `not_finalized`     | 序号未定稿（频道不存在 / 序号还在缓冲区或水位线之外）             |
| `seq_is_gap`        | 序号已裁决为永久缺口，缺口不可更正                                |
| `version_conflict`  | `expected_version` 与该序号当前更正版本不一致                     |
| `duplicate_seq`     | 与本批中更靠前的某项序号重复                                      |

请求体校验失败（批次数为 0 或超过 100、`seq<1`、`expected_version<0`、`text`/`reason`/`idempotency_key` 为空白等）返回 HTTP 422。被拒绝的批次不占用幂等键，可用原键提交修正后的请求。批量与单条更正可混用，版本链在每个序号上保持连续；原始定稿仍只经 `view=original` 获取。

### 创建发布快照

对频道当前定稿状态做一次不可变冻结，供下游按 `snapshot_id` 分批拉取同一时刻的稳定字幕：

```bash
curl -X POST http://localhost:8000/channels/news/snapshots \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key": "run-2026-09-20T12:00"}'
```

```json
{
  "snapshot_id": "b1f3c2a4-....",
  "channel": "news",
  "watermark": 6,
  "idempotency_key": "run-2026-09-20T12:00",
  "idempotent": false,
  "created_at": "2026-09-20T12:00:00.000Z"
}
```

- 请求只需 `idempotency_key`（非空字符串，去除首尾空白后不得为空）；
- `idempotent=false` 表示本次新建；相同频道携带相同 key 重试时不重新冻结，返回原快照且 `idempotent=true`（即使频道水位线已推进，`watermark` 仍是创建时刻锁定的值）；
- 不存在的频道可直接创建快照，得到 `watermark=0` 的空快照。

### 分页拉取快照

按 `snapshot_id` + `after`（序号游标，字幕与缺口标记统一计数）+ `limit`（默认 1000，1..10000）分页，按序号升序混合返回冻结的字幕与缺口标记。默认 `view=original`，响应结构与定稿查询一致（不带 `view` / `correction_version` 字段）：

```bash
curl 'http://localhost:8000/snapshots/b1f3c2a4-..../items?after=0&limit=1000'
```

```json
{
  "snapshot_id": "b1f3c2a4-....",
  "channel": "news",
  "watermark": 6,
  "view": "original",
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕", "start_seq": null, "end_seq": null},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4}
  ]
}
```

`view=effective` 返回**快照创建时刻冻结**的最新更正文本（不是查询时刻的最新更正），并附带 `correction_version`（字幕 0=快照时未更正；缺口为 null），顶层 `correction_version` 为本页最大值：

```bash
curl 'http://localhost:8000/snapshots/b1f3c2a4-..../items?view=effective&after=0&limit=2'
```

```json
{
  "snapshot_id": "b1f3c2a4-....",
  "channel": "news",
  "watermark": 6,
  "view": "effective",
  "correction_version": 1,
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕（快照时的更正）", "start_seq": null, "end_seq": null, "correction_version": 1},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4, "correction_version": null}
  ]
}
```

快照不存在返回 HTTP 404 `snapshot_not_found`；`after<0`、`limit` 越界、非法 `view` 返回 HTTP 422。

### 查询快照内更正历史

返回某字幕序号在**快照时刻冻结**的全部更正版本（按 `version` 升序），与实时审计接口结构对应：

```bash
curl http://localhost:8000/snapshots/b1f3c2a4-..../items/1/corrections
```

```json
{
  "snapshot_id": "b1f3c2a4-....",
  "channel": "news",
  "seq": 1,
  "current_version": 1,
  "items": [
    {"seq": 1, "version": 1, "text": "第一条字幕（快照时的更正）", "reason": "原稿漏字", "created_at": "2026-09-20T11:00:00.000Z"}
  ]
}
```

| 状态码 | `error`                | 含义                                                       |
| ------ | ---------------------- | ---------------------------------------------------------- |
| 404    | `snapshot_not_found`   | 快照不存在                                                 |
| 404    | `seq_out_of_snapshot`  | 序号小于 1 或超出快照锁定的 watermark（空快照全部越界）    |
| 409    | `seq_is_gap`           | 该序号在快照中是永久缺口，缺口没有更正历史                 |

### 比较两个快照（差异）

比较**同一频道**任意两个快照的冻结生效内容。以路径快照为基准（base）、`other` 查询参数为对方，按 `seq` 升序合并两份快照条目，只返回有差异的条目：

```bash
curl 'http://localhost:8000/snapshots/b1f3c2a4-..../diff?other=c2e4d5b6-....&after=0&limit=1000'
```

```json
{
  "snapshot_id": "b1f3c2a4-....",
  "other_snapshot_id": "c2e4d5b6-....",
  "channel": "news",
  "watermark": 5,
  "other_watermark": 9,
  "items": [
    {
      "change": "changed",
      "seq": 1,
      "base":  {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕", "start_seq": null, "end_seq": null, "correction_version": 2},
      "other": {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕（再改）", "start_seq": null, "end_seq": null, "correction_version": 3}
    },
    {
      "change": "added",
      "seq": 7,
      "base": null,
      "other": {"kind": "gap", "seq": 7, "revision": null, "text": null, "start_seq": 7, "end_seq": 8, "correction_version": null}
    }
  ],
  "next_after": 7
}
```

差异类型与判定规则：

- `added`：序号仅存在于 `other` 快照（`base` 为 null）；`removed`：序号仅存在于基准快照（`other` 为 null，对方是更早的快照时才会出现）；`changed`：两侧都有但冻结内容不同；
- **字幕**按快照冻结的**生效文本**（`view=effective` 同款）与 `correction_version` 判定——即使文本相同，只要更正版本不同也算变更；**缺口**按裁决区间端点 `start_seq` / `end_seq` 判定；
- 两侧完全一致的序号**不返回**；与自身比较或期间无任何变化时返回空 `items`；
- 每条差异的 `base` / `other` 携带该侧快照冻结的生效条目（结构同快照 `view=effective` 条目）。

分页规则：

- `after`（默认 0）为序号游标，`limit`（默认 1000，1..10000）**只计差异条目**——未变化的条目不占额度，差异再稀疏也不会被漏掉；
- `next_after` 指向本页最后一条差异的 `seq`，把它作为下一页的 `after` 续拉即可逐页拉全；本页为空（无更多差异）时 `next_after=null`。

错误：

| 状态码 | `error`                      | 含义                                       |
| ------ | ---------------------------- | ------------------------------------------ |
| 404    | `snapshot_not_found`         | 基准快照或 `other` 快照不存在              |
| 409    | `snapshot_channel_mismatch`  | 两个快照分属不同频道，不能比较             |
| 422    | —                            | 缺少 `other`、`after<0` 或 `limit` 越界    |

### 快照敏感词遮盖

对快照冻结的生效文本做敏感词遮盖，只读、不落库。请求体携带词表与分页参数：

```bash
curl -X POST http://localhost:8000/snapshots/b1f3c2a4-..../redactions \
  -H 'Content-Type: application/json' \
  -d '{"words": ["敏感词", "违禁"], "after": 0, "limit": 100}'
```

```json
{
  "snapshot_id": "b1f3c2a4-....",
  "channel": "news",
  "watermark": 6,
  "words": ["敏感词", "违禁"],
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条***字幕", "start_seq": null, "end_seq": null, "correction_version": 1},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4, "correction_version": null}
  ],
  "next_after": null
}
```

- `words`：敏感词表，按精确相等去重后须有 1~20 项，每项 1~128 个 Unicode 码点；响应中的 `words` 为实际生效的去重词表（保持首次出现顺序）；
- 相邻字幕按序号无分隔拼接后匹配，跨字幕词语完整遮盖；缺口阻断拼接，跨缺口词语不命中；缺口标记原样返回；
- 匹配区分大小写、逐码点精确、不做归一化；所有词的重叠命中取并集，每个被覆盖码点替换为一个 `*`；
- 遮盖在整个快照上一次算好再分页：`after`（默认 0）为序号游标，`limit`（默认 100，1~100）只限制返回条数，跨页词语也完整遮盖；还有后续条目时 `next_after` 为本页末序号，否则为 `null`；
- 快照不存在返回 404 `snapshot_not_found`；词表去重后数量越界、词长越界、`after<0`、`limit` 越界返回 422。

### 剪辑字幕导出（json / WebVTT）

把快照冻结的生效字幕套用到剪辑时间轴：提交每条字幕的源区间 `timings` 与有序剪辑片段 `clips`，服务逐对求交集、平移到成片累计时间后返回 cue 列表，或直接输出 WebVTT 文件。只读，不修改快照。

```bash
curl -X POST 'http://localhost:8000/snapshots/b1f3c2a4-..../edits' \
  -H 'Content-Type: application/json' \
  -d '{
    "timings": [
      {"seq": 1, "start_ms": 0, "end_ms": 3000},
      {"seq": 2, "start_ms": 3000, "end_ms": 5000}
    ],
    "clips": [
      {"start_ms": 1000, "end_ms": 4000},
      {"start_ms": 0, "end_ms": 1000}
    ]
  }'
```

```json
{
  "snapshot_id": "b1f3c2a4-....",
  "channel": "news",
  "format": "json",
  "duration_ms": 4000,
  "cues": [
    {"seq": 1, "clip_index": 0, "text": "第一条字幕", "start_ms": 0, "end_ms": 2000},
    {"seq": 2, "clip_index": 0, "text": "第二条字幕", "start_ms": 2000, "end_ms": 3000},
    {"seq": 1, "clip_index": 1, "text": "第一条字幕", "start_ms": 3000, "end_ms": 4000}
  ]
}
```

- `timings`：每条字幕在源时间轴上的左闭右开区间（毫秒），须**恰好覆盖快照内全部字幕 seq**——上例字幕 1 横跨两个 clip 被拆分为两条 cue，第二个 clip 重排了源区间 `[0,1000)` 故字幕 1 在其后再次出现；
- `clips`：有序剪辑片段，可重排 / 重复 / 重叠，按输入顺序首尾拼接；`duration_ms` 为全部片段时长之和；
- cue 按 `(start_ms, clip_index, seq)` 升序；`clip_index` 为片段在请求 `clips` 数组中的下标（从 0 起）；
- 所有时间须为非负整数且起点小于终点；`timings` 缺序号 / 重复 / 含多余序号（缺口或超界）返回 422 `timings_mismatch`，快照不存在返回 404 `snapshot_not_found`。

`format=vtt` 时直接返回 `text/vtt` 的 WebVTT 文档（cue 按同一顺序输出，正文转义 `&` `<` `>`）：

```bash
curl -X POST 'http://localhost:8000/snapshots/b1f3c2a4-..../edits?format=vtt' \
  -H 'Content-Type: application/json' \
  -d '{"timings": [{"seq": 1, "start_ms": 0, "end_ms": 1500}], "clips": [{"start_ms": 0, "end_ms": 1500}]}'
```

```vtt
WEBVTT

1
00:00:00.000 --> 00:00:01.500
第一条字幕

```

### 快照字幕引用迁移

把旧快照（源）中已经定位过的一段引用，按内容重新定位到同一频道的另一个快照（目标）——例如快照后字幕被更正、切分点变化，需要换算引用在新快照中的序号 / 偏移。接口只读，不修改任何快照或当前字幕。

假设源快照中 seq1 的冻结生效文本为 `"hello "`、seq2 为 `"world!"`，引用取 seq1 后 3 码点 + seq2 前 5 码点（即 `"lo world"`）：

```bash
curl -X POST http://localhost:8000/snapshots/b1f3c2a4-..../quote-migrations \
  -H 'Content-Type: application/json' \
  -d '{
    "target_snapshot_id": "c2e4d5b6-....",
    "start_seq": 1, "start_offset": 3,
    "end_seq": 2, "end_offset": 5
  }'
```

请求字段（偏移均为 **Unicode 码点**偏移、按左闭右开计算）：

- `target_snapshot_id`：目标快照 ID，必须与路径上的源快照同频道；
- `start_seq` / `start_offset`：引用起点（含）的字幕序号与序号内码点偏移（从 0 起）；
- `end_seq` / `end_offset`：引用终点（不含）的字幕序号与序号内码点偏移，可等于该字幕文本的码点长度；`start_seq` 与 `end_seq` 可相同（引用位于单条字幕内）。

目标快照恰好命中一次时，`status="found"`，`match` 给出引用在目标快照中的起止序号 / 偏移（命中同样可跨相邻字幕）；`quote` 是按源快照冻结生效文本提取出的引用文本，`source` 原样回显请求区间：

```json
{
  "source_snapshot_id": "b1f3c2a4-....",
  "target_snapshot_id": "c2e4d5b6-....",
  "channel": "news",
  "status": "found",
  "source": {"start_seq": 1, "start_offset": 3, "end_seq": 2, "end_offset": 5},
  "quote": "lo world",
  "match": {"start_seq": 1, "start_offset": 3, "end_seq": 2, "end_offset": 2}
}
```

目标快照中零次命中返回 `missing`，多次命中（含重叠出现、跨缺口分段的重复）返回 `ambiguous`，两种情况下 `match` 均为 `null`，服务不会任选一个位置：

```json
{
  "source_snapshot_id": "b1f3c2a4-....",
  "target_snapshot_id": "c2e4d5b6-....",
  "channel": "news",
  "status": "ambiguous",
  "source": {"start_seq": 1, "start_offset": 0, "end_seq": 1, "end_offset": 4},
  "quote": "echo",
  "match": null
}
```

规则与错误：

- 源引用按源快照冻结的生效文本提取：可跨相邻字幕（无分隔拼接），但区间内存在缺口标记、序号超出源快照水位线、偏移超出对应字幕码点长度、提取结果为空，均返回 422 `invalid_quote_range`；
- 目标快照匹配同样按“相邻字幕无分隔拼接、缺口阻断”分段，跨缺口的引用不会命中；匹配区分大小写、逐码点精确、不做归一化；
- 任一快照不存在返回 404 `snapshot_not_found`，两个快照分属不同频道返回 409 `snapshot_channel_mismatch`；请求体基本校验（序号 `<1`、偏移 `<0`、`end_seq < start_seq` 等）返回 422。

### 稿件校稿对齐

把外部稿件按行与快照冻结字幕做**全局最小代价顺序对齐**，用于校稿定位。只读快照，不修改任何数据：

```bash
curl -X POST http://localhost:8000/snapshots/b1f3c2a4-..../script-alignments \
  -H 'Content-Type: application/json' \
  -d '{
    "lines": [
      {"text": "各位观众晚上好，欢迎收看新闻联播", "max_subtitles": 2},
      {"text": "今天的主要内容有……", "max_subtitles": 3}
    ]
  }'
```

```json
{
  "snapshot_id": "b1f3c2a4-....",
  "channel": "news",
  "watermark": 3,
  "total_cost": 1,
  "alignments": [
    {"index": 0, "matched": true, "start_seq": 1, "end_seq": 2, "subtitles": 2, "distance": 1},
    {"index": 1, "matched": false, "start_seq": null, "end_seq": null, "subtitles": null, "distance": null}
  ],
  "unmatched_script_lines": [1],
  "unmatched_subtitle_seqs": [3]
}
```

- `lines`：1～100 条有序稿件行；`text` 去除首尾空白后不得为空，`max_subtitles` 取 1～3；
- 每行匹配连续 1～`max_subtitles` 条同段字幕（**无分隔拼接、不跨缺口、字幕不复用**），`distance` 为稿件行与该字幕块的码点编辑距离；未匹配行 `matched=false`、序号与距离字段为 `null`；
- 稿件行与字幕均可跳过（跳过代价 = 相应文本码点数）；全局总代价相同依次按**跳过项更少、合并字幕更少、匹配序号字典序更小**裁决；
- 快照不存在返回 404 `snapshot_not_found`；行数越界（0 或 >100）、行为空白、`max_subtitles` 不在 1～3、缺字段等返回 422。

### 领取消费者批次（claim）

命名消费者从自己的已确认游标后领取连续批次，服务在同一事务内签发带过期时间的租约令牌：

```bash
curl -X POST http://localhost:8000/channels/news/consumers/worker-1/claims \
  -H 'Content-Type: application/json' \
  -d '{"limit": 100, "lease_seconds": 30}'
```

```json
{
  "channel": "news",
  "consumer": "worker-1",
  "cursor": 0,
  "watermark": 6,
  "lease_token": "7c9e2f1a-....",
  "lease_expires_at": "2026-09-21T12:00:30.000Z",
  "items": [
    {"kind": "subtitle", "seq": 1, "revision": 1, "text": "第一条字幕", "start_seq": null, "end_seq": null},
    {"kind": "gap",      "seq": 3, "revision": null, "text": null, "start_seq": 3, "end_seq": 4}
  ]
}
```

- 请求体可整体省略（默认 `limit=100`、`lease_seconds=30`）；`limit` 取值 1..10000，`lease_seconds` 取值 (0, 3600]；
- 批次为该消费者已确认游标后的连续前缀（`cursor+1` 起，字幕与缺口标记按序混合，最多 `limit` 条），只包含已定稿内容，不含缓冲区中未到连续前缀的片段；
- 同一消费者持有**未过期租约**时重复领取返回 409 `lease_active`；租约过期后可重新领取同一未确认批次（返回相同内容、签发新令牌）；
- 无新内容时返回 `"items": []`、`"lease_token": null`，不签发租约；首次领取即注册消费者（之后可查状态）。

### 确认消费者批次（ack）

确认已处理的批次并把已确认游标推进到 `new_cursor`（允许部分确认，但必须落在本批次范围内）：

```bash
curl -X POST http://localhost:8000/channels/news/consumers/worker-1/acks \
  -H 'Content-Type: application/json' \
  -d '{"lease_token": "7c9e2f1a-....", "expected_cursor": 0, "new_cursor": 3}'
```

```json
{"channel": "news", "consumer": "worker-1", "previous_cursor": 0, "cursor": 3, "watermark": 6}
```

- `lease_token` 必须等于当前租约令牌；`expected_cursor` 必须等于当前已确认游标（乐观并发控制）；`new_cursor` 必须落在本批次 `[cursor+1, 批次末序号]` 范围内；
- 成功后游标推进、租约清空，下一次领取从新游标后继续；
- 失败一律返回 409 且**不改写任何状态**：

| `error`                 | 含义                                                     |
| ----------------------- | -------------------------------------------------------- |
| `lease_expired`         | 租约已过期，需重新领取后再确认                           |
| `lease_token_mismatch`  | 令牌与当前租约不匹配（旧令牌或他人令牌）                 |
| `cursor_out_of_range`   | `new_cursor` 不在本批次序号范围内                        |
| `cursor_conflict`       | `expected_cursor` 与当前游标不一致（并发确认已推进游标） |
| `no_active_lease`       | 消费者不存在，或当前没有待确认的租约                     |

### 查询消费者状态

```bash
curl http://localhost:8000/channels/news/consumers/worker-1
```

```json
{
  "channel": "news",
  "consumer": "worker-1",
  "cursor": 3,
  "watermark": 6,
  "lag": 3,
  "lease": {"from_seq": 4, "to_seq": 6, "expires_at": "2026-09-21T12:00:30.000Z", "expired": false}
}
```

- `lag` = 未确认条目数（`watermark - cursor`）；`lease` 为当前租约（含批次范围、过期时刻与查询时刻是否已过期），无租约时为 `null`；
- 消费者不存在（从未领取过批次）返回 404 `consumer_not_found`。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest                     # 含乱序、修订竞争、缺口裁决、幂等/重叠、单条/批量更正、批量整批拒绝/幂等重试/并发、版本冲突、视图合并、审计、发布快照冻结/幂等/分页/不可变、快照差异合并/分页/错误、快照敏感词遮盖/跨字幕拼接/缺口阻断/分页/校验、剪辑字幕导出交集平移/拆分/副本/排序/VTT/校验、快照字幕引用迁移提取/跨字幕/缺口阻断/found/missing/ambiguous/校验、稿件校稿对齐合并/缺口阻断/跳过/并列裁决/码点距离/校验、消费者领取/确认/租约过期/并发、并发事务等用例
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

服务默认监听 **8000** 端口；本地开发时 uvicorn 绑定 `0.0.0.0:8000`，从本机访问 http://localhost:8000/docs 即可打开交互式文档。

## 实现要点

- **事务化提交 / 裁决 / 更正 / 快照**：`app/service.py` 中「校验 → 写入」在单个 `BEGIN IMMEDIATE` 事务内完成；缺口裁决、单条 / 批量更正与快照发布的所有校验先于任何写入执行，失败即只读返回（批量更正随事务回滚），不产生副作用。`finalized` / `gaps` 表主键 `(channel, seq)`、`corrections` 表主键 `(channel, seq, version)`、`correction_batches` 的 `UNIQUE(channel, idempotency_key)`、`snapshots` 的 `UNIQUE(channel, idempotency_key)` 分别作为防重复定稿 / 裁决 / 版本号重复 / 批量幂等键重复 / 快照幂等键重复的最后防线。
- **连续前缀推进**：`_consume_contiguous` 从给定水位线的下一位起逐条消费缓冲区，遇到第一个缺失即停止；片段补齐与缺口裁决都复用该逻辑。
- **缺口存储**：`gap_decisions` 记录每次裁决区间及其当时释放到的序号（用于幂等返回原结果），`gaps` 按被跳过序号逐行落表，定稿查询用 `UNION ALL` 按序合并字幕与缺口标记。
- **追加式更正层**：`corrections(channel, seq, version, text, reason, created_at)` 仅通过 `INSERT` 追加，`(channel, seq)` 外键引用 `finalized`——缺口或未定稿序号在存储层即无法插入；版本号按序号从 1 递增，`expected_version` 乐观并发控制 + `BEGIN IMMEDIATE` 保证竞争下唯一成功；`corrections_no_update` / `corrections_no_delete` 触发器在存储层禁止任何改写与删除，原始定稿保持不可变。生效视图用相关子查询连接每个序号的最新版本（`MAX(version)`）合并文本，审计查询按 `version` 升序返回完整历史。
- **批量更正**：`correction_batches(batch_id, channel, idempotency_key, request_fingerprint, created_at)` 以 `UNIQUE(channel, idempotency_key)` 约束频道内幂等键；`correction_batch_items(batch_id, item_index, seq, expected_version, correction_version, text, reason, created_at)` 按输入顺序（`item_index` 从 0 起）冻结逐项请求与结果，供重试逐字重放。`submit_correction_batch` 在单个 `BEGIN IMMEDIATE` 事务内先查幂等键——命中且指纹（逐项 seq/expected_version/text/reason 及顺序的 SHA-256）一致则只读重放原批次，键相同指纹不同抛 `idempotency_key_conflict`；未命中时逐项核对批内重复 seq（重复出现项标 `duplicate_seq`）、定稿存在性（`not_finalized` / `seq_is_gap`）与当前版本（`version_conflict`），冲突按输入顺序全部收集后一次性抛 `batch_conflict` 并随事务回滚，做到**零写入**；全部通过才先插入批次行（唯一约束是并发同键重试的最后防线），再按输入顺序逐项 `INSERT ... RETURNING created_at` 追加 `corrections` 版本并写入冻结记录，与单条更正共用同一版本链。两张批次表均配 `no_update` / `no_delete` 触发器，幂等记录在存储层不可变。
- **发布快照**：`snapshots(snapshot_id, channel, watermark, idempotency_key, created_at)` 记录冻结时刻，`snapshot_items(snapshot_id, seq, …)` 对 `1..watermark` 每个序号落一行——字幕行同时存 `original_text`、创建时刻经 `MAX(version)` 连接冻结的 `effective_text` 及其 `correction_version`，缺口行存裁决区间端点；`snapshot_corrections(snapshot_id, seq, version, …)` 逐版本复制快照时刻已有的全部更正记录。创建先按 `(channel, idempotency_key)` 查重，命中即幂等返回；全部插入在一个写事务内完成，故冻结视图与锁定水位线严格一致。三张快照表各自配备 `no_update` / `no_delete` 触发器，快照一旦生成在存储层即不可变；分页与快照内审计只读取快照表，后续定稿、更正、缺口对其零影响。不存在的频道不写 `channels` 行，直接生成 `watermark=0` 的空快照。
- **快照差异**：`GET /snapshots/{snapshot_id}/diff?other=…` 在单个只读事务内校验两快照存在且同频道（不存在 404、跨频道 409），随后用两段 `LEFT JOIN` 的 `UNION ALL` 模拟全外连接，把两份 `snapshot_items` 按 `seq` 合并：第一段覆盖“基准有 / 双方都有且有差异”，第二段补齐“仅对方有”。差异判定用 SQLite 的 NULL 安全不等号 `IS NOT` 比较冻结的 `effective_text`、`correction_version`（字幕）与 `start_seq` / `end_seq`（缺口），完全一致的行在 SQL 层即被过滤，`ORDER BY seq LIMIT` 只作用于差异行——因此 `limit` 天然只计差异条目，稀疏差异也不会漏项；`next_after` 取本页最后一条差异的 `seq` 供续拉。
- **快照敏感词遮盖**：`POST /snapshots/{snapshot_id}/redactions` 在只读事务内取出快照 `1..watermark` 冻结的生效文本，按序号把相邻字幕（中间无缺口）无分隔拼接成分段，缺口截断分段；在每段上对去重词表（1~20 项、每项 1~128 码点）用 `str.find` 逐码点枚举全部重叠命中（每次命中后仅前进一位），覆盖区间取并集后把每个被覆盖码点替换为一个 `*`（长度不变），再按各字幕原码点长度拆回原序号。遮盖先于分页完成，`after` / `limit`（1~100）只截取结果页，`next_after` 在还有后续条目时指向本页末序号；全程不写入，快照与当前字幕不受影响。
- **剪辑字幕导出**：`POST /snapshots/{snapshot_id}/edits` 在只读事务内取出快照内全部字幕行（`kind='subtitle'`）冻结的生效文本，先在内存中校验 `timings` 恰好覆盖其 seq 集合（重复 / 缺失 / 多余均 422 `timings_mismatch`，快照不存在 404），再按输入顺序遍历 `clips` 并维护成片累计偏移：每条字幕与当前 clip 取左闭右开交集 `[max(start), min(end))`，非空才生成 cue 并平移 `交集 - clip.start + 偏移`；跨片段自然拆分、重复 clip 在不同偏移处保留副本、重叠字幕各自成 cue 不合并，最后按 `(start_ms, clip_index, seq)` 排序返回。`format=vtt` 时把排序后的 cue 渲染为 WebVTT（`HH:MM:SS.mmm` 时间戳，正文转义 `&` `<` `>`），以 `text/vtt` 直接输出；全程不写入，快照与当前字幕不受影响。
- **快照字幕引用迁移**：`POST /snapshots/{snapshot_id}/quote-migrations` 在单个只读事务内取出源 / 目标两个快照的元数据与全部 `snapshot_items` 冻结生效文本：任一快照不存在 404、频道不一致 409。源引用提取先校验序号区间落在源快照水位线内且逐序号都是字幕行（缺口 / 空洞即 422 `invalid_quote_range`），再按 Unicode 码点（Python `str` 切片）校验起止偏移不越界——首条取 `text[start_offset:]`、末条取 `text[:end_offset]`、中间整条（空字幕照常），拼接结果为空一律 422。目标匹配复用与遮盖相同的“分段”模型：相邻字幕（中间无缺口）无分隔拼接、缺口截断分段；在每段上以 `str.find` 逐码点枚举引用的全部重叠出现（每次只前进一位），并把拼接下标映射回序号 / 偏移——起点取最后一个起点不大于该下标的字幕（落在边界归属后一条），终点取最后一个起点严格小于该下标的字幕（落在边界归属引用末字符所在的前一条）。最多枚举到 2 个命中即可判定：1 个为 `found`、0 个为 `missing`、≥2 个为 `ambiguous`（不任选位置）；全程不写入，两个快照与当前定稿 / 更正均不受影响。
- **命名消费者拉取游标**：`consumers(channel, consumer, cursor, lease_token, lease_from, lease_to, lease_expires_at, …)` 每消费者一行。领取在单个 `BEGIN IMMEDIATE` 事务内「注册消费者 → 校验无未过期租约 → 复用定稿查询截取 `cursor` 后的连续批次（含缺口标记）→ 写入新租约（UUID 令牌 + 过期时刻 epoch 秒）」，并发领取同一消费者只有一个能拿到租约；确认同样单事务完成全部校验（游标一致 → 有租约 → 令牌匹配 → 未过期 → `new_cursor` 在批次范围内）后推进游标并清空租约，任何一步失败都不改写状态。租约过期由应用层按 `lease_expires_at` 判定，过期后同一未确认批次可被重新领取并签发新令牌（旧令牌随之失效），确认成功即清空租约、下一批从新游标继续，实现至少一次交付。
- **稿件校稿对齐**：`POST /snapshots/{snapshot_id}/script-alignments` 在只读事务内取出快照全部字幕行（`kind='subtitle'`）冻结的生效文本，按序号升序与 1～100 条稿件行做二维动态规划：状态 `(i,j)` 的入边为“跳过字幕（代价为其码点数）/ 跳过稿件行（代价为其码点数）/ 行 i 匹配连续 1～max 条同段字幕（块无分隔拼接，序号不连续即跨缺口、不可行，代价为码点 Levenshtein 距离）”。每个单元除最小总代价外还携带跳过项数、合并字幕数（Σ(k−1)）与匹配块 `(start_seq,end_seq)` 序列，并列时依次按这三项 + 序号序列字典序取小；字典序比较用持久化 Trie + LCP 记忆化、节点只在候选胜出时实体化。编辑距离优先调用 `rapidfuzz` 的 C 实现并以紧上界 `score_cutoff` 带状早停，无该依赖时回退到等价的纯 Python 带状 DP。回溯码表重建逐行起止序号 / 距离与未匹配稿件行下标、未匹配字幕序号；全程不写入，快照与当前字幕不受影响。
- **存储**：`channels`（水位线）、`fragments`（待定稿缓冲）、`finalized`（定稿，不可变）、`gap_decisions` / `gaps`（永久缺口）、`corrections`（追加式更正）、`correction_batches` / `correction_batch_items`（批量更正批次与逐项冻结记录，不可变）、`snapshots` / `snapshot_items` / `snapshot_corrections`（不可变发布快照）、`consumers`（命名消费者拉取游标）十二张表；WAL 模式 + `busy_timeout`。建表语句与触发器均幂等（`CREATE TABLE/TRIGGER IF NOT EXISTS`），已部署实例重启后自动补建新表与触发器，原有数据与接口行为不受影响。
- **容器化**：`docker compose up --build` 一键启动，数据库落在命名卷 `subtitles-data`；服务固定监听 **8000** 端口。
