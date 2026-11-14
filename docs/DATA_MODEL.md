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
                                   AuditResult
                        ├── SourceSummary[]
                        ├── BatchSummary[]
                        ├── Watermark[]
                        └── Finding[]
                                        │
                                        ▼
                         report.{json,md,html} + ledger.sqlite
```

`source + partition` 是水位的唯一状态键；`source` 也是记录标识重复检查的作用域。

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

`record_count` 表示**成功解析的记录数**：只有记录标识非空且事件时间合法的行才计入。它不等于物理行数；物理行数由 `FILE_ROW_COUNT_MISMATCH` 单独核对 manifest 的 `rows`。缺少关键列时仍统计物理行数，以便行数检查仍然有意义。

## 5. 内部派生模型

`FileScan` 保存声明引用、实际摘要/字节数、表头、物理行数、有效记录、有效事件范围与文件级发现项。

`BatchScan` 聚合一个 manifest 的所有 `FileScan`，计算有效记录数及事件时间最小/最大值。

`BatchSummary` 是报告与台账中的批次视图，包含：

```text
source, batch_id, sequence, partition, declared_at, manifest_path,
file_count, record_count, event_time_min, event_time_max,
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
    "finding_count": 0,
    "error_count": 0,
    "warning_count": 0,
    "info_count": 0
  },
  "sources": [],
  "watermarks": [],
  "batches": [],
  "findings": []
}
```

- `run_id`：16 位十六进制内容寻址标识，由规范配置、manifest、实际文件摘要/字节/行数、扫描结果与发现项共同决定。
- `config_digest`：仅配置语义（不含本机绝对路径）的 SHA-256。
- 空值统一为 JSON `null`；所有键排序，缩进 2 空格，文件以换行结束。
- `summary.batch_count` 等于有效 manifest 数（即 `batches` 长度），不是 manifest 文件总数；无法解析的 manifest 只贡献发现项。

`sources[]` 字段：`name`, `partition_keys`, `allowed_lateness_seconds`, `future_tolerance_seconds`, `sequence_policy`, `manifest_count`, `file_count`, `record_count`, `error_count`, `warning_count`。

`watermarks[]` 字段：`source`, `partition`, `max_event_time`, `safe_watermark`, `batch_count`, `late_batch_count`。

`batches[]` 字段：`source`, `batch_id`, `sequence`, `partition`, `declared_at`, `manifest_path`, `file_count`, `record_count`, `event_time_min`, `event_time_max`, `watermark_before`, `watermark_after`, `late`。

`findings[]` 字段：`severity`, `code`, `source`, `partition`, `batch_id`, `message`, `evidence`。

## 7. SQLite 台账

数据库 `PRAGMA user_version = 1`，四张表：

### `runs`

主键 `run_id`。列：`project`, `status`, `as_of`, `recorded_at`, `config_digest`, `manifest_count`, `batch_count`, `file_count`, `record_count`, `finding_count`, `error_count`, `warning_count`, `info_count`, `report_json`, `report_md`, `report_html`。

`recorded_at` 被有意设置为 `as_of`，而非真实墙钟时间，以保持确定性。报告路径是本机绝对路径。

### `findings`

`id` 自增主键；`run_id` 外键；`ordinal`（从 1 开始，与报告顺序一致）；`severity`, `code`, `source`, `partition_label`, `batch_id`, `message`, `evidence_json`。`evidence_json` 是排序键、紧凑分隔符的 JSON 文本。`(run_id, ordinal)` 唯一。

### `watermarks`

`run_id`, `ordinal`, `source`, `partition_label`, `max_event_time`, `safe_watermark`, `batch_count`, `late_batch_count`。`(run_id, ordinal)` 唯一。

### `batches`

`run_id`, `ordinal`, `manifest_path`, `source`, `batch_id`, `sequence`, `partition_label`, `declared_at`, `file_count`, `record_count`, `event_time_min`, `event_time_max`, `watermark_before`, `watermark_after`, `late`（0/1）。`(run_id, ordinal)` 唯一。

写入以 `run_id` 幂等：先删除该运行的三类子表记录，再 upsert `runs` 并插入子记录，整个过程处于一个事务中。读取使用 SQLite `mode=ro` 和 `PRAGMA query_only=ON`。除上述审计元数据外，不复制原始 CSV 内容到台账。
