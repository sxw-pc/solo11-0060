# 湖入水位台（lakeinlet）

湖入水位台是一个**纯本地、完全离线、配置驱动**的数据湖入湖批次审计工具。它扫描本地 manifest 与 CSV 数据文件，重建每个 `source + partition` 的事件时间水位，并把“批次是否迟到、是否重复、是否有缺口、文件是否完整、当前安全水位是多少”变成可复现、可追溯、可归档的本地审计结果。

一句话定位：

> 把入湖批次的水位、迟到、幂等、序列与文件完整性检查，收敛为一条命令、一份稳定报告和一个本地 SQLite 台账。

## 适用范围

它适合：

- 小团队在没有云服务、调度平台或真实数据湖服务的环境里做入湖批次审计；
- 对事件时间 watermark、允许迟到窗口、批次序列与幂等性做离线回归；
- 为演示、验收、事故复盘生成确定性证据；
- 在本地只读查看审计运行、水位、批次与发现项。

它明确不做：

- 小文件合并、快照过期、孤儿文件清理；
- 版本账本、时间旅行查询、历史快照；
- 冷热分层、生命周期迁移或归档；
- SQL 血缘、指标口径、数据契约、字段脱敏或抽样闸门；
- 分布式调度、实时流处理、爬虫、RPA 或通用数据质量平台；
- 重写、删除、压缩、合并、回填或迁移入湖数据。

审计全程只读；唯一写入的位置是用户指定的报告目录与 SQLite 台账。

## 快速开始

要求 Python 3.11+，并安装仓库依赖：

```bash
python -m pip install -r requirements.txt
```

一条命令跑通干净场景审计：

```bash
python -m lakeinlet audit --config fixtures/demo/config.yaml --out out/demo
```

一条命令跑测试：

```bash
pytest -q
```

一条命令启动只读本地查看器：

```bash
python -m lakeinlet serve --ledger out/demo/ledger.sqlite --host 127.0.0.1 --port 8088
```

打开 <http://127.0.0.1:8088/> 查看运行列表；点击 run id 查看水位、批次和发现项。查看器只允许绑定 `127.0.0.1`、`localhost` 或 `::1`。

故障场景：

```bash
python -m lakeinlet audit --config fixtures/broken/config.yaml --out out/broken
```

故障场景默认以退出码 `1` 结束，并生成 20+ 条可定位的发现项。若只想生成报告、不把发现项当作命令失败，可加 `--fail-on never`。

## 命令

```text
python -m lakeinlet audit --config CONFIG --out OUT [--ledger PATH] [--fail-on error|warning|never] [--quiet]
python -m lakeinlet validate --config CONFIG
python -m lakeinlet serve --ledger LEDGER --host 127.0.0.1 --port 8088
```

- `audit`：运行全部检查，写出 `report.json`、`report.md`、`report.html` 和默认位于 `<out>/ledger.sqlite` 的台账。
- `validate`：只做配置静态校验，不读取数据文件。
- `serve`：从 SQLite 台账启动只读 Flask 查看器。

`audit` 的退出码：

| 退出码 | 含义 |
| ---: | --- |
| `0` | 未达到 `--fail-on` 阈值 |
| `1` | 存在达到阈值的发现项 |
| `2` | 配置、台账、路径或查看器启动参数错误 |

## 输出

每次审计产生：

```text
out/demo/
├── report.json       # 稳定 JSON，排序键、统一时间格式
├── report.md         # Markdown 报告，包含发现项证据
├── report.html       # 自包含 HTML，内联 CSS，无脚本和远程资源
└── ledger.sqlite     # SQLite 审计台账
```

报告不包含真实当前时间；所有时间判断以配置中的 `as_of` 为锚点。相同输入重复审计会得到相同 `run_id`、相同发现项顺序和字节级稳定的报告。台账对同一 `run_id` 做幂等替换，而不是无限追加重复运行。

### 水位定义

对每个 `source + 规范 partition`（分区值经 `partition_templates` 归一）：

```text
safe_watermark = max(observed event_time) - allowed_lateness_seconds
```

批次按 `declared_at`、`sequence`、manifest 路径稳定排序后依次处理。一个批次的 `event_time_max` 低于其到达前已存在的水位时，记为 `LATE_BATCH`。超过 `as_of + future_tolerance_seconds` 的未来事件会记为 `FUTURE_EVENT_TIME`，并且**不会推进水位**，避免损坏的未来时间戳把后续正常批次误判为迟到。

## 配置示例

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
  - name: clicks
    partition_keys: [dt]
    allowed_lateness_seconds: 1800
    future_tolerance_seconds: 300
    sequence_policy: contiguous
```

- `as_of` 必须是带 UTC 偏移（`Z` 或 `+00:00`）的 ISO-8601 时间。
- 相对路径以配置文件所在目录为基准解析。
- `sequence_policy` 支持 `contiguous`（检查序列缺口）和 `gapped`（允许缺口，但仍检查重复和链）。
- 每个 source 默认从 `<_manifest_dir>/*.yaml` 读取 manifest，默认目录为 `_manifests/<source>`。
- 可选的 `partition_templates` 用 `case`（`none/upper/lower`）与 `aliases` 声明每个分区键的规范取值；配置后 manifest 声明、CSV 行分区、水位分组、重复判定与台账 `partition_label` 全部归一到同一规范值，原始拼写保留在证据中。例如：

```yaml
partition_templates:
  region:
    case: upper
    aliases:
      EU: [emea]   # eu、EU、emea 都归一为 EU
```

完整字段与静态校验规则见 [docs/CONFIG.md](docs/CONFIG.md)。

## Manifest 与 CSV

一个 manifest 表示一个入湖批次。示例：

```yaml
manifest_version: 1
batch_id: orders-eu-0001
source: orders
sequence: 1
partition:
  dt: "2025-03-01"
  region: eu
declared_at: "2025-03-01T00:30:00Z"
files:
  - path: data/orders/orders_eu_0001.csv
    sha256: "<64 lowercase/uppercase hex characters>"
    bytes: 182
    rows: 3
    event_time_min: "2025-03-01T00:05:00Z"
    event_time_max: "2025-03-01T00:10:00Z"
```

数据文件是带表头的 UTF-8 CSV，至少包含：

- `record_id`（可配置）；
- `event_time`（可配置，必须为 UTC ISO-8601）；
- 该 source 配置中的全部 partition key 列。

审计会比较实际 SHA-256、字节数、行数、事件时间范围、表头、字段合法性、归一后的分区一致性，并检查规范分区内的重复记录标识。所有路径必须相对于 `lake_root`，绝对路径与 `..` 会被拒绝。仅大小写或别名差异（如 `eu` 对 `EU`）不再报 `PARTITION_MISMATCH`；同一 `batch_id` 的分区身份不自洽会报 `PARTITION_ALIAS_COLLISION`。

报告中的 `record_count` 是成功解析的有效记录数；manifest 声明的物理行数校验由 `FILE_ROW_COUNT_MISMATCH` 单独负责。

## Fixtures

仓库包含两套合成数据，不含真实公司、域名、个人数据或凭据：

- `fixtures/demo/`：6 个批次、两个 source、三个 partition 的干净场景，审计状态为 `passed`。
- `fixtures/broken/`：迟到批次、未来事件、序列缺口、重复批次/序列/文件摘要/记录、缺失文件、校验和不符、行数不符、坏表头、坏行、分区不一致、分区大小写/别名归一与跨分区 `batch_id` 冲突、非法 manifest，审计状态为 `failed`。

故障场景是回归规则覆盖的主要证据集。

## 项目结构

```text
lakeinlet/
├── cli.py              # argparse 命令入口
├── config.py           # YAML 配置读取与静态校验
├── models.py           # 配置、manifest、扫描、发现项等模型
├── audit_result.py     # 审计结果与 JSON 序列化
├── manifest.py         # manifest 发现、解析、路径安全
├── scanner.py          # 文件摘要、大小、行数与 CSV 扫描
├── csvcheck.py         # CSV 表头、字段、行、分区检查
├── watermark.py        # 水位推进、迟到、未来时间
├── duplicates.py       # 批次/序列/文件摘要/记录标识幂等检查
├── sequence.py         # 序列缺口与 manifest 链检查
├── rules.py            # 审计编排、状态、内容寻址 run_id
├── ledger.py           # SQLite 写入与只读查询
├── ledger_schema.py    # 台账 schema 与只读连接
├── report_{json,md,html}.py
├── server.py           # 只读回环 Flask 查看器
├── templates/          # 本地 Jinja 模板
└── static/style.css    # 本地样式，无 CDN/远程字体
```

更细的设计见：

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/DATA_MODEL.md](docs/DATA_MODEL.md)
- [docs/CONFIG.md](docs/CONFIG.md)
- [docs/FINDINGS.md](docs/FINDINGS.md)
- [docs/COMPLIANCE.md](docs/COMPLIANCE.md)

## 发现项与状态

严重级别为 `error`、`warning`、`info`。

- 有任意 `error`：状态 `failed`；
- 无 error、有 warning：状态 `passed_with_warnings`；
- 全部干净：状态 `passed`。

完整代码表、检测逻辑与修复建议见 [docs/FINDINGS.md](docs/FINDINGS.md)。

## 安全与离线边界

- 运行期不访问互联网，不调用外部 API，不读取云凭据。
- 不写入、不删除、不移动任何 lake 数据文件。
- 报告 HTML 自包含；不使用 CDN、远程字体、远程 JavaScript 或远程图片。
- 查看器强制只绑定回环地址；使用 SQLite `mode=ro` 与 `PRAGMA query_only`。
- 配置与 manifest 使用 `yaml.safe_load`；SQL 全部参数化；Jinja 自动转义动态内容；服务端设置 CSP。
- manifest 与数据文件路径被约束在 `lake_root` 内，拒绝绝对路径和 `..`。
- 测试不依赖网络或真实时钟。

## 已知边界

- 面向单机、小规模批次的本地审计；不是分布式调度器，不承诺海量 manifest 的流式内存上限。
- manifest 的 `sequence` 连续性按 source 维度检查，不按 partition 维度重排。
- 迟到判断使用批次事件时间最大值，不用逐记录迟到计数。
- 未来事件批次不推进水位，但其他低时间戳记录仍参与重复、完整性等检查。
- 查看器是无认证的本地只读工具，只应在可信本机回环地址上运行。
