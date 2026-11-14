# 发现项参考

## 1. 状态与严重级别

| 严重级别 | 含义 | 对状态的影响 |
| --- | --- | --- |
| `error` | 契约、完整性、时间或幂等性被破坏 | 状态 `failed` |
| `warning` | 需要关注，但不构成硬失败 | 无 error 时为 `passed_with_warnings` |
| `info` | 保留的提示级别 | 不影响状态 |

状态判定顺序：任意 `error` → `failed`；否则任意 `warning` → `passed_with_warnings`；否则 `passed`。

当前版本中，除 `NO_MANIFESTS` 为 warning 外，所有规则产生的发现项均为 error。`info` 是模型与报告支持、但当前规则未使用的级别。

## 2. 退出码

```bash
lakeinlet audit ...                 # 默认 --fail-on error
lakeinlet audit ... --fail-on warning
lakeinlet audit ... --fail-on never
```

| 参数 | 退出码 1 的条件 |
| --- | --- |
| `--fail-on error`（默认） | 存在至少一个 error |
| `--fail-on warning` | 存在至少一个 error 或 warning |
| `--fail-on never` | 永不因发现项返回 1 |

退出码 `2` 始终表示操作错误（配置、路径、台账、查看器参数），与发现项无关。

## 3. 发现项代码总表

### Manifest 发现与解析

| 代码 | 严重级别 | 检测逻辑 | 常见修复 |
| --- | --- | --- | --- |
| `MANIFEST_DIR_INVALID` | error | `manifest_dir` 解析后不在 `lake_root` 内，或路径非法 | 改成湖根内的相对目录 |
| `MANIFEST_DIR_MISSING` | error | 配置的 manifest 目录不存在 | 创建目录或修正 `manifest_dir` |
| `NO_MANIFESTS` | warning | glob 没有匹配到任何普通文件 | 放置 manifest 或修正 glob |
| `MANIFEST_PARSE_ERROR` | error | manifest 不可读、非 UTF-8 或 YAML 语法错误 | 修正文件编码与 YAML |
| `MANIFEST_INVALID` | error | YAML 根节点不是映射 | 改为键值映射 |
| `MANIFEST_INVALID_FIELD` | error | 必填字段缺失、类型错误、版本非 1、时间非 UTC、序列为负、文件列表为空、文件内事件范围倒置等 | 按错误消息修正具体字段 |
| `MANIFEST_SOURCE_MISMATCH` | error | manifest 的 `source` 与正在审计的 source 不同 | 将文件放到正确目录并修正 `source` |
| `PARTITION_KEYS_INVALID` | error | partition 不是映射、键集合不匹配、值为空或为容器 | 提供与配置完全一致的标量分区值 |
| `INVALID_FILE_PATH` | error | 数据文件路径为绝对路径、含 `..`、解析后越界；或 manifest 自身位于湖根外 | 使用湖根内的相对路径 |
| `DUPLICATE_FILE_REF` | error | 同一 manifest 重复引用相同 `path` | 合并或删除重复文件引用 |

manifest 解析失败只丢弃该 manifest，其他 manifest、文件和数据仍会继续审计。

### 文件完整性与 CSV

| 代码 | 严重级别 | 检测逻辑 | 常见修复 |
| --- | --- | --- | --- |
| `MISSING_DATA_FILE` | error | 声明的数据文件不存在 | 按 manifest 路径补齐文件或修正引用 |
| `FILE_READ_ERROR` | error | 文件存在但无法读取或计算摘要 | 检查权限、占用与文件损坏 |
| `FILE_CHECKSUM_MISMATCH` | error | 实际 SHA-256 与 manifest 不一致 | 核对文件内容与声明的摘要 |
| `FILE_BYTES_MISMATCH` | error | 声明了 `bytes` 且实际字节数不同 | 修正 manifest 或文件 |
| `FILE_ROW_COUNT_MISMATCH` | error | 声明了 `rows` 且 CSV 物理数据行数不同 | 核对物理行数；注意有效记录数是另一个指标 |
| `MANIFEST_EVENT_RANGE_MISMATCH` | error | 声明的事件时间最小/最大值与可解析记录不同 | 修正声明边界或数据 |
| `CSV_READ_ERROR` | error | 无法打开、解析表头或解析中途出现 CSV 错误 | 检查编码、引号与分隔符 |
| `CSV_EMPTY` | error | 没有表头行 | 提供带表头的 CSV |
| `CSV_DUPLICATE_HEADER` | error | 表头出现重复列名 | 去重列名 |
| `CSV_MISSING_COLUMNS` | error | 缺少事件时间、记录标识或任一分区列 | 补齐列；仍会统计物理行数以核对行数 |
| `CSV_MALFORMED_ROWS` | error | 某行列数与表头不一致 | 修正引号、分隔符或补齐字段 |
| `CSV_INVALID_EVENT_TIME` | error | 事件时间为空、非 ISO-8601、无时区或非 UTC | 改为 `...Z` 或 `+00:00` |
| `CSV_MISSING_RECORD_ID` | error | 记录标识为空字符串 | 提供稳定且非空的记录标识 |
| `PARTITION_MISMATCH` | error | CSV 行的分区列值与 manifest 不同 | 修正行分区值或 manifest 分区 |

文件摘要、字节数、行数、事件范围和 CSV 内容问题可以同时出现；审计不会在第一个问题处停止。

### 水位与时间

| 代码 | 严重级别 | 检测逻辑 | 常见修复 |
| --- | --- | --- | --- |
| `LATE_BATCH` | error | 在到达前已存在安全水位，且本批 `event_time_max` 严格低于它 | 检查上游延迟、重放顺序或迟到窗口配置 |
| `FUTURE_EVENT_TIME` | error | 批次 `event_time_max > as_of + future_tolerance_seconds` | 修正未来时间戳；该批不会推进水位 |
| `FUTURE_DECLARED_AT` | error | `declared_at > as_of + future_tolerance_seconds` | 修正声明到达时间或 `as_of` |

水位按 `(declared_at, sequence, manifest_relpath)` 稳定排序推进，每个 `source + partition` 独立维护。边界规则：

- 事件时间最大值**等于**安全水位时不算迟到；
- 无有效事件时间的批次不参与迟到判断，也不推进水位；
- 未来事件批次不参与迟到判断、不推进水位，避免损坏时间戳抬高水位；
- `FUTURE_DECLARED_AT` 不阻止本批的其他检查。

### 幂等性

| 代码 | 严重级别 | 检测逻辑 | 常见修复 |
| --- | --- | --- | --- |
| `DUPLICATE_BATCH_ID` | error | 多个有效 manifest 使用同一 `batch_id`（跨全部被审计 source） | 为每个批次分配唯一 ID |
| `DUPLICATE_SEQUENCE` | error | 同一 source 内多个 manifest 使用同一非负 `sequence` | 修正序列号或删除重复声明 |
| `DUPLICATE_FILE_DIGEST` | error | 多个实际存在且可读的数据文件共享同一 SHA-256 | 确认是否为有意的重放；避免重复入湖 |
| `DUPLICATE_RECORD_ID` | error | 同一 source 内同一 `record_id` 出现多次 | 确认幂等键；移除重复记录或改用稳定唯一键 |

`DUPLICATE_FILE_DIGEST` 使用实际摘要，而不是声明摘要；即使某文件同时存在校验和不符，只要可读就参与该检查。`DUPLICATE_RECORD_ID` 只统计同时具有非空记录标识和合法 UTC 事件时间的记录。

### 序列与链

| 代码 | 严重级别 | 检测逻辑 | 常见修复 |
| --- | --- | --- | --- |
| `SEQUENCE_GAP` | error | `contiguous` 策略下，source 内最小到最大序列号之间存在缺失 | 补充缺失批次或切换为 `gapped` |
| `CHAIN_BROKEN` | error | `prev_batch_id` 指向的批次在同 source 有效 manifest 中不存在 | 修正前驱 ID 或补入前驱 manifest |
| `CHAIN_ORDER_INVALID` | error | 唯一匹配的前驱批次 `sequence` 大于或等于当前批次 | 修正序列或前驱关系 |

序列检查以 source 为作用域；`gapped` 仅关闭 `SEQUENCE_GAP`，不关闭链检查。缺口证据最多列出前 100 个缺失值，并标记 `samples_truncated`。

## 4. 证据字段

所有发现项都包含非空 `evidence`。常用字段：

| 字段 | 含义 |
| --- | --- |
| `path` / `manifest_path` | manifest 内相对路径或 manifest 相对湖根路径 |
| `expected` / `actual` | 声明值与实际值 |
| `count` | 重复或问题数量 |
| `samples` | 最多 3 条行级样例（CSV 问题） |
| `locations` | 重复文件/记录所在 manifest、批次、文件与行号 |
| `sequence` / `range` / `missing_count` | 序列相关证据 |
| `sha256` | 实际文件摘要 |
| `event_time_max` / `safe_watermark_before` / `as_of` | 时间判断依据 |
| `policy` | 触发缺口检查的序列策略 |

CSV 聚合证据中的 `count` 是总数，`samples` 只保留前三条，避免报告无限膨胀。重复记录位置最多保留 10 条；分组总数仍完整。

## 5. 顺序与去重

1. 每个规则产生发现项；
2. 按完整 JSON 内容去重；
3. 按严重级别（error、warning、info）、source、code、batch_id、partition 标签、message、规范证据排序；
4. 报告、台账 `ordinal` 与查看器使用同一顺序。

因此相同输入会得到相同的发现项集合、顺序、`run_id` 与字节级稳定的 JSON/Markdown/HTML。

## 6. 定位建议

1. 先用 `batch_id` 或 `manifest_path` 在报告中定位批次；
2. 查看该批 `sources[]` 与 `batches[]` 中的记录数和事件范围；
3. 对照 `watermarks[]` 的 `watermark_before`、`watermark_after` 与 `late`；
4. 对文件完整性问题，核对 `expected` 与 `actual`；
5. 对序列问题，检查 source 内全部有效 manifest，而不是只看当前分区；
6. 对记录重复，查看 `locations` 中的文件与行号，确认是否为同一幂等键。

工具只报告问题，不执行修复；修复应由数据拥有者在湖外流程中完成并重新生成 manifest。
