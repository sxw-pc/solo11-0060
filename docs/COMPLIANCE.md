# 合规与边界说明

本文对照项目需求逐项说明实现位置、验证方式与已知残留风险。所有结论均可在仓库内检查或复现。

## 1. 功能范围

| 编号 | 要求 | 实现 | 验证 |
| --- | --- | --- | --- |
| F1 | 配置与模型校验 | `config.py`、`models.py`、`timeutil.py` | `test_config.py`、`test_timeutil.py` |
| F2 | manifest 与文件扫描 | `manifest.py`、`scanner.py` | `test_manifest.py`、`test_scanner.py` |
| F3 | CSV 完整性检查 | `csvcheck.py` | `test_csvcheck.py` |
| F4 | 水位、迟到、未来时间 | `watermark.py` | `test_watermark.py` |
| F5 | 重复、幂等与记录收敛 | `duplicates.py`、`convergence.py` | `test_duplicates.py`、`test_convergence.py` |
| F6 | 序列缺口与链检查 | `sequence.py` | `test_sequence.py` |
| F7 | 确定性报告与 SQLite 台账 | `report_*.py`、`ledger*.py` | `test_reports.py`、`test_ledger.py` |
| F8 | 只读本地查看器 | `server.py`、`templates/`、`static/` | `test_server.py` |

CLI 集成由 `test_cli.py` 覆盖。故障 fixture 提供跨规则的端到端证据。

## 2. 完全离线

- 运行期代码不包含 HTTP 客户端、云 SDK、对象存储客户端、遥测或远程资源加载。
- Python 依赖仅用于本地解析与本地 HTTP 服务：PyYAML、Flask、pytest。
- 查看器只在显式回环地址上监听；这不是对外网络服务。
- 报告 HTML 内联 `static/style.css`，不引用 CDN、远程字体、远程 JavaScript、远程图片或任何外部 URL。
- 查看器模板只引用本地 `/static/style.css`，不加载脚本。
- fixture 与文档不包含真实域名、公司、个人数据、凭据或外部目标地址。

安装依赖可能需要访问包索引，这只是部署准备；项目运行本身不需要互联网。

## 3. 回环地址强制

`lakeinlet/server.py`：

```python
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
```

`serve_viewer()` 在创建 Flask 应用和绑定端口**之前**调用 `validate_host(host)`。任何其他值（包括 `0.0.0.0`、`::`、局域网 IP、主机名）都会抛出 `ServeError`，CLI 以退出码 `2` 结束，不会启动监听。

端口必须是 1–65535 的整数，布尔值被拒绝。

## 4. 只读数据与只读台账

### 湖数据

- manifest 通过 `read_text` 读取；数据文件通过 `open("rb")` 计算摘要、通过 `open("r", ...)` 解析 CSV。
- 不存在对 `lake_root` 下 manifest 或数据文件的写入、删除、重命名、压缩、合并、回填、迁移代码。
- 路径边界由 `resolve_lake_path()` 强制执行：拒绝绝对路径与 `..`，并验证解析后仍位于湖根内。
- 审计只报告问题，不自动修复、不回写 manifest、不生成替代数据文件。

### SQLite

- 审计仅向用户指定的报告目录/台账路径写入。
- 查看器通过 URI `mode=ro` 打开数据库，并执行 `PRAGMA query_only = ON`。
- 查看器启动前校验 `PRAGMA user_version = 2` 与五张必需表。
- 查看器只注册 GET 路由：`/`、`/run/<run_id>`、`/healthz`；不存在写入、删除、上传或命令执行端点。

## 5. 无凭据与无外部地址

- 源码、配置、manifest、CSV、测试和文档中没有硬编码的令牌、密码、私钥、访问密钥、真实主机或真实外部 URL。
- 源码与 fixture 中没有 URL。文档只包含本机示例 `http://127.0.0.1:8088/`；测试中的 `"http://"` / `"https://"` 字符串仅用于断言渲染报告不包含外部链接。
- 配置加载使用 `yaml.safe_load`，不会实例化任意 Python 对象。
- 应用不读取环境变量中的凭据；不存在云认证路径。

## 6. 合成 fixture

`fixtures/demo/` 与 `fixtures/broken/` 全部为仓库内置合成数据：

- 名称如 `orders`、`clicks`、`eu`、`us` 为通用业务标签；
- 记录标识与事件时间人工构造；
- 不含真实公司、真实域名、真实个人数据、真实主机、真实密钥；
- 所有 SHA-256、字节数与行数均由合成文件计算得出；
- 故障场景覆盖迟到、未来事件、序列缺口、链断裂、各类重复、文件缺失、摘要/字节/行数/事件范围不符、坏表头、坏行、非法时间、空记录标识、分区不一致与非法 manifest。

## 7. 确定性

- 所有时间判断以配置 `as_of` 为基准；源码与测试不读取真实时钟。
- 批次按 `(declared_at, sequence, manifest_relpath)` 排序。
- 文件按路径排序；发现项按固定元组排序并去重。
- JSON 使用排序键、固定缩进与末尾换行。
- `run_id` 是内容寻址摘要；相同输入重复运行会得到相同结果并幂等替换台账记录。
- 报告中的 `recorded_at` 等于 `as_of`，不使用墙钟时间。
- 测试使用固定 `as_of`、临时目录和 Flask 测试客户端，不依赖网络。

## 8. 安全处理

| 面 | 措施 |
| --- | --- |
| YAML | `yaml.safe_load`；字段白名单；类型/边界校验 |
| 路径 | 绝对路径与 `..` 拒绝；解析后边界检查 |
| SQL | 全部参数化查询；查看器只读连接 |
| HTML 报告 | 所有动态值 `html.escape`；无 `<script>`；内联本地 CSS |
| 查看器模板 | Jinja 自动转义；CSP `script-src 'none'` |
| 响应头 | CSP、`X-Frame-Options: DENY`、`nosniff`、`no-referrer`、`no-store` |
| 文件规模 | 摘要按 64 KiB 分块读取，不整体载入大文件 |
| 证据膨胀 | CSV 样例最多 3 条；重复记录的收敛账完整保留被覆盖条目；序列缺失样例最多 100 个 |

## 9. 仓库卫生

`.gitignore` 覆盖：

```text
out/
__pycache__/
*.py[cod]
.pytest_cache/
.benchmarks/
.mypy_cache/
.ruff_cache/
*.sqlite
*.sqlite-wal
*.sqlite-shm
*.sqlite-journal
*.db
.venv/  venv/
.coverage  htmlcov/
.DS_Store  .idea/  .vscode/
```

生成报告与台账位于 `out/`，不纳入版本控制。

## 10. 规模约束

截至本次交付：

| 范围 | 行数 |
| --- | ---: |
| `lakeinlet/` + `tests/` 的 Python 代码（含测试） | 5486 |
| 其中测试 | 约 1848 |
| `static/style.css` | 288 |
| Jinja 模板 | 228 |
| Python 单文件最大行数 | 300（`lakeinlet/convergence.py`、`lakeinlet/ledger.py`） |

- Python 总量处于 2500–4500 区间。
- 所有 Python、CSS、模板与文档单文件均不超过 300 行。
- 文档与 fixture 数据是独立交付物，不计入上述 Python 代码量。

## 11. 测试与验收

```bash
pytest -q
python -m lakeinlet audit --config fixtures/demo/config.yaml --out out/demo
python -m lakeinlet audit --config fixtures/broken/config.yaml --out out/broken
python -m lakeinlet serve --ledger out/demo/ledger.sqlite --host 127.0.0.1 --port 8088
```

验收基线：

- 测试全部通过（当前 126 项）；
- 干净 fixture 状态 `passed`，0 发现项，`record_count` 与 `raw_record_count` 同为 17；
- 故障 fixture 状态 `failed`，24 个 error，默认退出码 1；有效记录 8 条、原始记录 13 条；
- 故障 fixture 的 4 组重复 `record_id` 均给出保留位置、被覆盖条目与比较依据；
- 查看器只绑定回环地址，非回环地址在启动前被拒绝；
- 报告与台账可重复生成。

## 12. 已知残留风险

- **无认证**：查看器面向可信本机。同机其他进程可访问该端口，因此不要在不可信多用户主机上运行。
- **本地路径可见**：台账保存报告的绝对路径，查看器会显示；这是本机运维信息，不是远程暴露面。
- **证据可能含业务值**：CSV 样例、记录标识、分区值和文件路径会进入报告与台账。当前版本不做脱敏。使用真实数据时应控制 `out/` 的访问权限。
- **非流式 manifest 处理**：manifest 列表、扫描结果与记录标识集合驻留内存；面向单机、小规模批次，未针对海量数据做流式优化。
- **批次级迟到语义**：以批次最大事件时间判断迟到，不计算逐记录迟到比例；无有效事件时间的批次不推进水位。
- **收敛仅按记录标识**：同一 `record_id` 视为同一逻辑记录；若上游存在同一 ID 多版本需要并存的场景，需要改用复合幂等键。
- **收敛账规模**：被覆盖条目完整写入报告与台账，不做截断；极端重复度下证据体积会随重复行数增长。
- **序列作用域**：序列连续性与重复按 source 检查，不按 partition 独立编号；这要求上游采用 source 级序列。
- **SQLite 并发**：写入使用单事务；没有多写者协调或服务化锁。查看器只读，但长时间运行不会自动刷新已打开页面的数据。
- **依赖安装**：首次安装 Python 包可能需要网络；运行期不需要。
