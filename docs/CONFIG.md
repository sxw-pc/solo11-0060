# 配置参考

## 1. 加载方式

```bash
python -m lakeinlet validate --config fixtures/demo/config.yaml
python -m lakeinlet audit --config fixtures/demo/config.yaml --out out/demo
```

配置使用 `yaml.safe_load` 读取，必须是顶层映射。未知字段、类型错误、非法枚举或路径越界会在读取 manifest 之前以配置错误终止，CLI 退出码为 `2`。

配置文件路径相对当前工作目录解析；`lake_root` 与 `manifest_dir` 的语义见下文。

## 2. 完整示例

```yaml
schema_version: 1
project: lakeinlet-demo
lake_root: lake
as_of: "2025-03-01T04:00:00Z"
sources:
  - name: orders
    partition_keys: [dt, region]
    allowed_lateness_seconds: 3600
    future_tolerance_seconds: 300
    sequence_policy: contiguous
    manifest_dir: _manifests/orders
    manifest_glob: "*.yaml"
    event_time_field: event_time
    record_id_field: record_id

  - name: clicks
    partition_keys: [dt]
    allowed_lateness_seconds: 1800
    future_tolerance_seconds: 300
    sequence_policy: gapped
```

带分区归一模板的写法：

```yaml
sources:
  - name: orders
    partition_keys: [dt, region]
    partition_templates:
      dt:
        case: none
      region:
        case: upper
        aliases:
          EU: [emea]
    allowed_lateness_seconds: 3600
    future_tolerance_seconds: 300
```

所有字段名区分大小写。缩进必须合法；建议时间值加引号，避免 YAML 隐式类型带来的歧义。

## 3. 顶层字段

### `schema_version`

- 可选，默认按 `1` 处理。
- 只接受整数 `1` 或字符串 `"1"`。
- 布尔值、`2`、`"2"` 等均被拒绝。

### `project`

- 必填，非空字符串，首尾空白会被去除。
- 进入报告标题、CLI 输出和台账。
- 不参与路径解析。

### `lake_root`

- 必填，非空字符串。
- 相对值以**配置文件所在目录**为基准解析；也接受绝对路径。
- 最终必须指向已存在的目录，否则配置错误。
- 所有 manifest 与数据文件都必须实际位于该目录之内；符号链接解析后越界同样被拒绝。
- 审计不会在该目录内写入任何内容。

### `as_of`

- 必填。
- 必须是 ISO-8601 且带显式 UTC 偏移；接受 `Z`、`z` 与 `+00:00`。
- 无时区、非零偏移（如 `+08:00`）均被拒绝。
- 这是唯一的“当前时间”来源。测试、报告与未来容忍判断都不读取真实时钟。
- 推荐写法：`"2025-03-01T04:00:00Z"`。

### `sources`

- 必填，非空列表。
- 每一项必须是映射。
- `name` 不得重复；否则配置错误。
- source 顺序保留在报告的 `sources[]` 中；批次与发现项另有确定性排序。

## 4. Source 字段

### `name`

- 必填，非空。
- 必须匹配 `[A-Za-z0-9][A-Za-z0-9_.-]*`。
- 必须与 manifest 中的 `source` 完全一致。

### `partition_keys`

- 必填，非空字符串列表。
- 每一项必须匹配同一标识符规则，且不得重复。
- manifest 的 `partition` 键集合必须与它**完全相等**：不能缺、不能多。
- CSV 必须包含所有这些列，且每行值与 manifest 相同。
- 水位的状态键是 `source + 全部 partition key 的值`。

### `partition_templates`

- 可选。声明每个分区键的**规范取值规则**；未配置时归一化是恒等函数，历史行为完全不变。
- 一旦出现该字段，必须覆盖 `partition_keys` 中的**每一个键**，不能缺、不能多；否则配置错误。
- 每个键对应一个映射，仅允许两个字段：

| 字段 | 含义 |
| --- | --- |
| `case` | 可选，`none`（默认，保持原样）、`upper`（大写）、`lower`（小写）。先对原始值做大小写折叠，再查别名。 |
| `aliases` | 可选，映射 `规范值: [额外原始拼写...]`。规范值自身（按 `case` 折叠后）始终是合法拼写。 |

示例 `{region: upper, alias: {emea: [eu, EU]}}` 对应的 YAML 为：

```yaml
partition_templates:
  region:
    case: upper
    aliases:
      EMEA: [eu, EU]
```

此时 `emea`、`EMEA`、`eu`、`EU` 全部归一到同一规范分区 `EMEA`。

加载配置时的静态校验（任一不满足即配置错误，退出码 2）：

- 模板键集合与 `partition_keys` 不完全相等；
- `case` 不在 `none/upper/lower` 内，或存在未知模板字段；
- 别名列表不是字符串列表、规范值或别名不是非空字符串；
- 同一键内，两个原始拼写（含规范值自身）经 `case` 折叠后落到同一规范值——别名键不得与自身或同键其它别名冲突；
- 两个规范值经 `case` 折叠后相互冲突（例如 `case: lower` 下同时声明 `eu` 与 `EU`）。

归一化是全链路**唯一**的分区身份来源：manifest 声明、CSV 每行分区列、水位分组键、重复记录分组、台账 `partition_label` 都经过同一函数得到同一规范值。报告的 `partition` 字段只展示规范值；原始拼写与模板命名保留在 finding 的 `evidence`（如 `raw_actual`、`raw_expected`、`raw_partition_values`、`raw_value_mapping`）中供追溯。普通别名归并不产生 finding；只有 `PARTITION_ALIAS_COLLISION` 描述的冲突才报错（见 FINDINGS.md）。

### `allowed_lateness_seconds`

- 必填，非负整数；布尔值不算整数。
- 用于：

```text
safe_watermark = max_observed_event_time - allowed_lateness_seconds
```

- `0` 表示不额外容忍迟到，水位等于最大观测事件时间。

### `future_tolerance_seconds`

- 必填，非负整数；布尔值不算整数。
- 判断 `event_time_max > as_of + future_tolerance_seconds` 是否成立。
- 同样用于判断 `declared_at` 是否过未来。
- `0` 表示只允许不晚于 `as_of` 的声明和事件。

### `sequence_policy`

允许值：

| 值 | 行为 |
| --- | --- |
| `contiguous` | 在 source 内检查最小到最大序列号之间的缺口 |
| `gapped` | 允许序列缺口，不产生 `SEQUENCE_GAP` |
| `allow_gaps` | `gapped` 的兼容别名 |
| `non_contiguous` | `gapped` 的兼容别名 |

无论策略如何，`DUPLICATE_SEQUENCE`、`CHAIN_BROKEN` 与 `CHAIN_ORDER_INVALID` 始终检查。输出中策略会规范化为 `contiguous` 或 `gapped`。

### `manifest_dir`

- 可选，默认 `_manifests/<source>`。
- 必须是相对路径，不能是绝对路径，也不能包含 `..`。
- 相对 `lake_root`，不是相对配置文件。
- 必须实际存在，否则审计产生 `MANIFEST_DIR_MISSING` 错误。
- 目录内容不会被递归匹配，除非 `manifest_glob` 自身包含递归模式。

### `manifest_glob`

- 可选，默认 `*.yaml`。
- 必须是非空字符串。
- 不允许绝对路径或 `..` 路径段。
- 只在 `manifest_dir` 内匹配普通文件；结果按路径排序。
- 如果要包含 `.yml`，可写 `"*.yml"` 或使用多个目录。

### `event_time_field`

- 可选，默认 `event_time`。
- 必须是标识符。
- 指定 CSV 中的事件时间列名。

### `record_id_field`

- 可选，默认 `record_id`。
- 必须是标识符。
- 指定 CSV 中的记录标识列名，用于跨文件重复检查。
- 不需要全局唯一，但同一 source 内重复会产生 `DUPLICATE_RECORD_ID`。

## 5. 路径与安全规则

```text
config directory
└── lake_root/
    ├── _manifests/<source>/*.yaml   ← manifest_dir + manifest_glob
    └── data/...                     ← manifest 文件引用路径
```

- manifest 文件自身必须位于 `lake_root` 内；发现过程从解析后的 manifest 目录开始。
- 数据文件路径必须是相对路径，不含 `..`，解析后仍须位于 `lake_root` 内。
- manifest 绝对路径与越界路径会产生 `INVALID_FILE_PATH`。
- 允许符号链接，但以真实解析后的位置执行边界检查。
- 绝对路径或包含 `..` 的 `manifest_dir` 会在配置加载阶段被拒绝；解析后仍越界（例如经由符号链接）则在审计阶段形成 `MANIFEST_DIR_INVALID`。

## 6. 校验时机对照

| 问题 | `validate` | `audit` |
| --- | --- | --- |
| 未知顶层/source 键 | 退出码 2 | 退出码 2，不生成报告 |
| 非 UTC `as_of` | 退出码 2 | 退出码 2 |
| `lake_root` 不存在 | 退出码 2 | 退出码 2 |
| `manifest_dir` 不存在 | 不检查 | `MANIFEST_DIR_MISSING` |
| manifest YAML 语法错误 | 不检查 | `MANIFEST_PARSE_ERROR` |
| 数据文件缺失 | 不检查 | `MISSING_DATA_FILE` |
| CSV 内容问题 | 不检查 | 对应 CSV 发现项 |

`validate` 只验证配置和 `lake_root` 目录存在，不扫描数据，也不写任何输出。

## 7. 常见错误

- `config.as_of must include a UTC timezone offset`：时间缺少 `Z`/`+00:00`。
- `config.lake_root is not an existing directory`：相对路径不是相对于配置文件目录，或目录不存在。
- `config.sources contains duplicate source names`：两个 source 同名。
- `sources[0].partition_keys contains duplicates`：分区键重复。
- `sources[0] contains unknown keys: ...`：字段拼写错误或使用了不支持键。
- `sources[0].manifest_dir must be a relative path without '..'`：把 manifest 目录写成了绝对路径或试图越界。
- `sources[0].allowed_lateness_seconds must be a non-negative integer`：使用了小数、负数、布尔或字符串。
- `sources[0].partition_templates must cover every partition key; missing: ...`：模板没有覆盖全部分区键（或出现了不属于 `partition_keys` 的键）。
- `... aliases: raw value ... already resolves to ...`：别名与同键其它拼写（含规范值）在大小写折叠后冲突。
- `... aliases: canonical values ... collide after case=... normalization`：两个规范值在所选 `case` 下不可区分。

修正后运行 `python -m lakeinlet validate --config <path>` 可快速确认。配置错误属于操作错误，不会生成部分报告。
