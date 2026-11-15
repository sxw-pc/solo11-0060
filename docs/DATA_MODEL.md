# 数据模型

本文描述配置、manifest、CSV、审计报告与 SQLite 台账之间的数据契约。所有时间均为 UTC，报告中的规范表示形如 `2025-03-01T04:00:00Z`。

## 1. 对象关系

```text
AuditConfig 1 ── * SourceConfig
     │
     └── lake_root ── * BatchManifest 1 ── * FileRef
                              │
                              └── BatchScan 1 ── * FileScan 1 ── * Record
                                        │
                                        ▼
                              收敛（convergence）
                                        │
                                        ▼
                                   AuditResult
                        ├── SourceSummary[]
                        ├── BatchSummary[]
                        ├── Watermark[]
                        ├── RecordConvergence[]
                        └── Finding[]
                                        │
                                        ▼
                         report.{json,md,html} + ledger.sqlite
```

`source + partition` 是水位的唯一状态键；`source` 也是记录标识重复检查与记录收敛的作用域。`convergence.py` 负责收敛账，`duplicates.py` 把它转成发现项。

## 2. 配置模型

顶层字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | 整数 | 否 | 仅允许 `1`；省略视为 1 |
| `project` | 非空字符串 | 是 | 项目名，进入报告与台账 |
| `lake_root` | 非空字符串 | 是 | 湖根目录；相对路径以配置文件目录为基准 |
| `as_of` | UTC ISO-8601 | 是 | 审计基准时间；所有未来判断的锚点 |
| `sources` | 非空列表 | 是 | 数据源，名称不得重复 |

每个 source：

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `name` | 标识符 | 必填 | 匹配 `[A-Za-z0-9][A-Za-z0-9_.-]*` |
| `partition_keys` | 非空字符串列表 | 必填 | 无重复；决定分区身份 |
| `allowed_lateness_seconds` | 非负整数 | 必填 | 安全水位回退窗口 |
| `future_tolerance_seconds` | 非负整数 | 必填 | 相对 `as_of` 允许的未来偏移 |
| `sequence_policy` | 枚举 | `contiguous` | `contiguous` 或 `gapped` |
| `manifest_dir` | 相对路径 | `_manifests/<name>` | 相对 `lake_root` |
| `manifest_glob` | glob | `*.yaml` | 仅在该目录内匹配 |
| `event_time_field` | 标识符 | `event_time` | CSV 事件时间列 |
| `record_id_field` | 标识符 | `record_id` | CSV 记录标识列 |

布尔值不能充当整数；未知字段会被拒绝。完整校验规则见 [CONFIG.md](CONFIG.md)。

## 3. Manifest

一个 YAML 文件表示一个入湖批次。顶层字段：

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `manifest_version` | 整数 | 否 | 仅允许 `1`，省略视为 1 |
| `batch_id` | 非空字符串 | 是 | 全局重复会被检测 |
| `source` | 非空字符串 | 是 | 必须等于当前审计 source |
| `sequence` | 非负整数 | 是 | 连续性/重复检查依据 |
| `partition` | 映射 | 是 | 键集合必须与配置完全一致 |
| `declared_at` | UTC ISO-8601 | 是 | 批次声明到达时间 |
| `files` | 非空列表 | 是 | 文件引用，同一路径不得重复 |
| `prev_batch_id` | 非空字符串 | 否 | 声明前驱批次，用于链检查 |

分区值允许字符串、整数或浮点数，最终统一转为字符串；布尔、空值、空字符串和容器被拒绝。

文件引用：

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `path` | 相对路径 | 是 | 必须位于 `lake_root` 内，禁止绝对路径与 `..` |
| `sha256` | 64 位十六进制 | 是 | 大小写不限，内部规范为小写 |
| `bytes` | 非负整数 | 否 | 声明字节数 |
| `rows` | 非负整数 | 否 | 声明物理数据行数 |
| `event_time_min` | UTC ISO-8601 | 否 | 不得晚于 `event_time_max` |
| `event_time_max` | UTC ISO-8601 | 否 | 与解析结果比较 |

当前实现不会拒绝 manifest 中的额外键；审计所需字段缺失或类型错误才会形成发现项。manifest 解析失败只影响该文件，其余批次继续审计。

## 4. CSV 契约

- UTF-8（允许 BOM），必须有表头；
- 至少包含 `event_time_field`、`record_id_field` 与全部 partition key 列；
- 表头名去除首尾空白；重复表头会报告；
- 每行事件时间必须是带 UTC 偏移的 ISO-8601；
- 每行记录标识必须非空；
- 每个 partition 列的值必须与 manifest 分区完全一致（按字符串比较）；
- 列数多于或少于表头的行会被视为 malformed，不参与记录级检查。

`record_count` 表示**收敛后的有效记录数**：同一 source 内同一 `record_id` 只在收敛集里保留一条。收敛前的记录身份由两步得到：

1. **原始记录数**（`raw_record_count`）：记录标识非空的物理行，包括事件时间无法解析的行；
2. **有效记录数**（`record_count`）：其中事件时间可解析、且在重复收敛中未被他条覆盖的行。

物理行数（含空 `record_id`、malformed 行）由 `FILE_ROW_COUNT_MISMATCH` 单独核对 manifest 的 `rows`。缺少关键列时仍统计物理行数，以便行数检查仍然有意义。

### 4.1 记录收敛规则

同一 `source` 内同一 `record_id` 有多条时，按以下次序确定唯一保留条目：

1. `event_time` 更晚者保留；
2. 并列时，所属批次 `declared_at` 更晚者保留；
3. 仍并列时，按文件路径升序、行号升序排稳定次序，取**先者**保留。

关键约束：

- `event_time` 无法解析的重复条目**参与分组**，但**不参与比较**，始终作为被覆盖方留证；它们不会因批次声明更晚等原因夺走合法记录的保留位。
- 一组内若没有任何可解析条目，则该组不保留任何记录，`effective_count` 为 0。
- 唯一且可解析的记录直接进入有效集；唯一但事件时间非法的行不进入有效集（仍由 `CSV_INVALID_EVENT_TIME` 报告）。

收敛账随报告 JSON 的 `record_convergences`、每条 `DUPLICATE_RECORD_ID` 的 `evidence`、Markdown/HTML 报告的「记录收敛账」章节与 SQLite 的 `record_convergences` 表输出，包含每条被覆盖条目的原定位、所属批次、事件时间（或原始文本）、最终保留位置与比较依据。

### 4.2 收敛集的下游使用

`BatchScan` 在收敛后同时保留：

- `raw_record_count`：具身份物理行数（含不可解析）；
- `record_count`：收敛后的有效记录数；
- `superseded_record_count`：被本批其他记录覆盖的条目数；
- `effective_records`：进入水位与摘要的有效记录。

批次 `event_time_min/max` 仅由 `effective_records` 计算。水位、`LATE_BATCH`、`FUTURE_EVENT_TIME` 与批次汇总因此全部基于收敛后的有效记录集，而不是原始行集。汇总中的 `record_count` 是有效记录数，`raw_record_count` 是具身份原始记录数，两者并列保留、互不割裂。

## 5. 内部派生模型

`FileScan` 保存声明引用、实际摘要/字节数、表头、物理行数、带身份记录（含不可解析事件时间）、文件级有效事件范围与文件级发现项。

`BatchScan` 聚合一个 manifest 的所有 `FileScan`；收敛前保存全部具身份记录，收敛后保存有效记录、有效记录数、原始记录数、被覆盖数与事件时间最小/最大值。

`BatchSummary` 是报告与台账中的批次视图，包含：

```text
source, batch_id, sequence, partition, declared_at, manifest_path,
file_count, record_count, raw_record_count, superseded_record_count,
event_time_min, event_time_max,
watermark_before, watermark_after, late
```

`Watermark`：

```text
source, partition, max_event_time, safe_watermark,
batch_count, late_batch_count
```

`Finding`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `severity` | `error` / `warning` / `info` | 决定状态与退出码 |
| `code` | 大写标识符 | 稳定机器可读代码 |
| `message` | 字符串 | 面向人的说明 |
| `source` | 字符串或 null | 可为空（例如跨 source 的批次 ID 重复） |
| `partition` | 映射或 null | 证据中的规范排序副本 |
| `batch_id` | 字符串或 null | 相关批次 |
| `evidence` | JSON 对象 | 计数、路径、行号、期望/实际值等 |

发现项先按完整内容去重，再按 `(severity, source, code, batch_id, partition, message, canonical evidence)` 排序。

分区标签规范化为 `key=value`，多个键按字母序并以逗号连接，例如 `dt=2025-03-01,region=eu`。

## 6. 报告 JSON

`schema_version` 为字符串 `"1.0"`。顶层结构：

```json
{
  "schema_version": "1.0",
  "project": "lakeinlet-demo",
  "as_of": "2025-03-01T04:00:00Z",
  "run_id": "0c3034a6e34bff83",
  "status": "passed",
  "config_digest": "64-hex",
  "summary": {
    "manifest_count": 6,
    "batch_count": 6,
    "file_count": 6,
    "record_count": 17,
    "raw_record_count": 17,
    "finding_count": 0,
    "error_count": 0,
    "warning_count": 0,
    "info_count": 0
  },
  "sources": [],
  "watermarks": [],
  "batches": [],
  "record_convergences": [],
  "findings": []
}
```

- `run_id`：16 位十六进制内容寻址标识，由规范配置、manifest、实际文件摘要/字节/行数、扫描结果与发现项共同决定。
- `config_digest`：仅配置语义（不含本机绝对路径）的 SHA-256。
- 空值统一为 JSON `null`；所有键排序，缩进 2 空格，文件以换行结束。
- `summary.batch_count` 等于有效 manifest 数（即 `batches` 长度），不是 manifest 文件总数；无法解析的 manifest 只贡献发现项。

`sources[]` 字段：`name`, `partition_keys`, `allowed_lateness_seconds`, `future_tolerance_seconds`, `sequence_policy`, `manifest_count`, `file_count`, `record_count`, `raw_record_count`, `error_count`, `warning_count`。

`watermarks[]` 字段：`source`, `partition`, `max_event_time`, `safe_watermark`, `batch_count`, `late_batch_count`。

`batches[]` 字段：`source`, `batch_id`, `sequence`, `partition`, `declared_at`, `manifest_path`, `file_count`, `record_count`, `raw_record_count`, `superseded_record_count`, `event_time_min`, `event_time_max`, `watermark_before`, `watermark_after`, `late`。

`record_convergences[]` 字段：`source`, `record_id`, `raw_count`, `effective_count`, `retained`（保留位置证据或 null）, `basis`（决定保留位的比较依据）, `superseded[]`（被覆盖条目：原定位、`declared_at`、`event_time`、`raw_event_time`、`event_time_parseable`、`basis`、`detail`、`retained` 最终保留位置）。

`findings[]` 字段：`severity`, `code`, `source`, `partition`, `batch_id`, `message`, `evidence`。

## 7. SQLite 台账

数据库 `PRAGMA user_version = 2`，五张表：

### `runs`

主键 `run_id`。列：`project`, `status`, `as_of`, `recorded_at`, `config_digest`, `manifest_count`, `batch_count`, `file_count`, `record_count`, `raw_record_count`, `finding_count`, `error_count`, `warning_count`, `info_count`, `report_json`, `report_md`, `report_html`。

`recorded_at` 被有意设置为 `as_of`，而非真实墙钟时间，以保持确定性。报告路径是本机绝对路径。

### `findings`

`id` 自增主键；`run_id` 外键；`ordinal`（从 1 开始，与报告顺序一致）；`severity`, `code`, `source`, `partition_label`, `batch_id`, `message`, `evidence_json`。`evidence_json` 是排序键、紧凑分隔符的 JSON 文本。`(run_id, ordinal)` 唯一。

### `watermarks`

`run_id`, `ordinal`, `source`, `partition_label`, `max_event_time`, `safe_watermark`, `batch_count`, `late_batch_count`。`(run_id, ordinal)` 唯一。

### `batches`

`run_id`, `ordinal`, `manifest_path`, `source`, `batch_id`, `sequence`, `partition_label`, `declared_at`, `file_count`, `record_count`, `raw_record_count`, `superseded_record_count`, `event_time_min`, `event_time_max`, `watermark_before`, `watermark_after`, `late`（0/1）。`(run_id, ordinal)` 唯一。

### `record_convergences`

`run_id`, `ordinal`, `source`, `record_id`, `raw_count`, `effective_count`, `basis`, `retained_json`（保留位置证据，或 NULL）, `superseded_json`（被覆盖条目数组）。`(run_id, ordinal)` 唯一。该表使收敛账可直接 SQL 查询。

写入以 `run_id` 幂等：先删除该运行的四类子表记录，再 upsert `runs` 并插入子记录，整个过程处于一个事务中。读取使用 SQLite `mode=ro` 和 `PRAGMA query_only=ON`。除上述审计元数据外，不复制原始 CSV 内容到台账。打开 v1 台账时会原地迁移到 v2（新增计数列与收敛账表）。
