# 架构说明

## 1. 目标

`lakeinlet` 在单机、完全离线条件下，对本地数据湖入湖批次执行一次确定性的只读审计：

1. 读取声明式配置与批次 manifest；
2. 核验数据文件完整性；
3. 重建 `source + partition` 的事件时间水位；
4. 检查迟到、未来时间、序列缺口和 manifest 链；
5. 检查批次、序列、文件摘要与记录标识的重复；
6. 生成 JSON/Markdown/自包含 HTML 报告；
7. 将运行、发现项、水位和批次写入 SQLite；
8. 通过仅绑定回环地址的只读查看器浏览台账。

## 2. 非目标

本工具不修改湖数据，不做表维护、快照管理、版本时间旅行、冷热分层、血缘、指标口径、契约校验、脱敏、抽样、调度或流处理。它只回答审计问题，不执行数据工程侧的修复动作。

## 3. 运行边界

```text
config.yaml
   │  safe_load + static validation
   ▼
manifest discovery (source manifest_dir + glob)
   │  schema validation + safe path resolution
   ▼
file scanner ──► CSV parser ──► integrity and row findings
   │
   ├──► duplicates (batch / sequence / digest / record id)
   ├──► sequence continuity + chain
   └──► watermark engine (arrival order, late/future)
              │
              ▼
        AuditResult (sorted findings, source summaries)
          │        │             │
          │        │             └──► SQLite ledger (idempotent by run_id)
          │        └──► JSON / Markdown / inline-CSS HTML
          └──► CLI exit code
```

审计阶段对 manifest 与数据文件只读，不执行任何写入、删除、移动或维护操作。唯一的持久化写入点是用户指定的报告目录和台账；即使该位置位于 `lake_root` 内，也只会新增报告/数据库文件，不会改动湖数据。

## 4. 模块职责

| 模块 | 职责 |
| --- | --- |
| `cli.py` | 参数解析、退出码、报告与台账写出顺序 |
| `config.py` | YAML 读取、字段白名单、UTC 校验、路径约束、source 策略归一化 |
| `models.py` | 配置、manifest、扫描、发现项、批次、水位等强类型模型 |
| `audit_result.py` | 顶层结果、计数与稳定 JSON 序列化结构 |
| `manifest.py` | 发现 manifest、安全解析、schema 与路径校验、逐文件错误降级 |
| `scanner.py` | SHA-256、字节数、CSV 扫描聚合、行数/事件范围对比 |
| `csvcheck.py` | 表头、必填列、行结构、UTC 事件时间、记录标识、分区一致性 |
| `watermark.py` | 按声明到达顺序推进分区水位、迟到、未来事件、未来声明 |
| `duplicates.py` | 批次 ID、source 序列、实际文件摘要、记录 ID 跨文件重复 |
| `sequence.py` | `contiguous` 序列缺口、`prev_batch_id` 链存在性与前后关系 |
| `rules.py` | 编排、发现项去重排序、source 汇总、状态、内容寻址 run id |
| `ledger_schema.py` | 台账 schema、初始化、只读连接与兼容性检查 |
| `ledger.py` | 幂等写入与只读查询 |
| `report_json.py` | 排序键 JSON、UTF-8、末尾换行 |
| `report_md.py` | 稳定 Markdown 与证据附录 |
| `report_html.py` | 转义所有动态值、内联本地 CSS、无脚本自包含页面 |
| `server.py` | Flask 只读应用、回环地址白名单、安全响应头 |
| `templates/`, `static/` | 本地服务端渲染资源，无外部依赖 |

## 5. 确定性设计

确定性是本项目的核心约束，具体措施：

- **固定审计基准**：所有“未来”判断都使用配置 `as_of`，绝不读取 `datetime.now()`。
- **显式排序**：文件按路径排序；批次按 `(declared_at, sequence, manifest_relpath)` 排序；发现项按严重级别、source、代码、batch、partition、消息、证据排序。
- **稳定序列化**：JSON 使用 `sort_keys=True`、固定缩进、末尾换行；证据字典递归排序。
- **稳定 ID**：`run_id` 是配置语义、manifest 内容、实际文件摘要、扫描结果和发现项的 SHA-256 内容摘要，取前 16 个十六进制字符。
- **幂等台账**：相同 `run_id` 重跑会先删除该运行的四类子记录再写回，`runs` 表使用 upsert，最终只保留一条运行。
- **无真实时间戳**：报告中的 `as_of` 与 `recorded_at` 都取配置基准时间，测试和归档结果不受机器时钟影响。
- **无外部资源**：HTML 报告内联 CSS；查看器只提供本地 CSS 和 Jinja 模板。

报告内容变化意味着 `run_id` 变化；相同报告要求相同 run id。这个性质使结果容易归档和比较。

## 6. 水位算法

对每个 `source + partition` 独立维护：

```text
max_observed_event_time
safe_watermark = max_observed_event_time - allowed_lateness_seconds
batch_count
late_batch_count
```

处理顺序是 manifest 的 `declared_at` 升序，`sequence` 与路径作为稳定的并列排序键。

每个批次：

1. 记录到达前的 `safe_watermark`；
2. 如果 `event_time_max` 低于该水位，产生 `LATE_BATCH`；
3. 如果 `event_time_max` 超过 `as_of + future_tolerance_seconds`，产生 `FUTURE_EVENT_TIME`；
4. 超过未来容忍度的批次**不推进水位**，防止损坏时间戳污染后续判断；
5. 否则以本批 `event_time_max` 更新最大观测事件时间，并重算安全水位。

批次级迟到判断使用该批最大事件时间；这是有意选择的简单、可解释语义。逐记录迟到统计不在本工具范围内。

`declared_at` 超过未来容忍度会产生 `FUTURE_DECLARED_AT`，但不会阻断文件扫描或其他规则。

## 7. 失败隔离

- 单个 manifest 解析失败会形成 `MANIFEST_*` 错误；其他 manifest 继续审计。
- 单个文件不存在、可读性失败或摘要不符仍会继续扫描可读内容，以便一次报告暴露尽可能多的问题。
- CSV 缺少关键列时仍统计物理行数，但不做字段级解析。
- 台账写入在一个事务中替换运行及其子项，失败时回滚。
- CLI 将预期领域错误转换为退出码 2，不输出 traceback。

## 8. 只读查看器

`server.create_app()`：

- 启动前验证台账存在、schema 版本正确；
- 使用 SQLite URI `mode=ro` 与 `PRAGMA query_only=ON` 打开数据库；
- 只注册 GET 路由：运行列表、运行详情、健康检查；
- 不提供任何写入、删除、下载任意文件或命令执行接口；
- 添加 CSP、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`no-referrer` 与 `no-store`；
- `validate_host()` 只接受 `127.0.0.1`、`localhost`、`::1`，在绑定前拒绝通配地址和任意主机。

未配置认证，因此只应在可信本机使用。

## 9. 扩展点

扩展时保持以下不变量：

- 新发现项必须带确定性的代码、严重级别和可 JSON 序列化证据；
- 新批次或水位字段必须同时进入报告 schema、SQLite schema 和测试；
- 任何网络访问、远程资源、凭据读取或湖数据写入都必须被拒绝；
- 时间逻辑必须接受 `as_of`，不能退化到真实时钟；
- 新路径读取必须经过 `resolve_lake_path` 边界校验。
