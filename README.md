# 企业级 Operational Ontology 实战项目（B2B 订单履约场景）

本项目以一家 **B2B 办公设备与耗材经销企业** 的 CRM、ERP、WMS 三套真实异构系统数据为蓝本，完整走通从 **场景与脏数据构造 → 本体概念建模 → Schema 驱动实体解析与物化 → CQ1–CQ4 查询验证 → 操作型动作与 AI Agent 工具闭环** 的五阶段工程落地流程，并按三大工业标准完成了 **MCP Server、交互式可视化控制台、W3C（OWL 2 / SHACL 1.0 / RDF Turtle / SPARQL 1.1）导出与验证**。

---

## 全流程文档导航

1. **阶段一｜场景定义与真实源数据构造**：[docs/01-scenario.md](docs/01-scenario.md) · [data/sources/README.md](data/sources/README.md)
2. **阶段二｜Ontology 概念设计与规范**：[docs/02-ontology-design.md](docs/02-ontology-design.md) · [ontology/schema.yaml](ontology/schema.yaml)
3. **阶段三｜语义映射、三级实体解析与物化**：[docs/03-mapping-and-resolution.md](docs/03-mapping-and-resolution.md) · [scripts/build_ontology.py](scripts/build_ontology.py)
4. **阶段四｜CQ1–CQ4 查询验证与裸查对照实验**：[docs/04-query-and-verification.md](docs/04-query-and-verification.md) · [ontology/client.py](ontology/client.py)
5. **阶段五｜操作型动作（Actions）与 AI Agent 语义工具层**：[docs/05-actions-and-agent.md](docs/05-actions-and-agent.md) · [ontology/actions.py](ontology/actions.py) · [ontology/agent_tools.py](ontology/agent_tools.py)
6. **三大标准集成｜MCP Server · 可视化图谱控制台 · W3C 语义网导出**：[docs/06-mcp-console-w3c.md](docs/06-mcp-console-w3c.md)
7. **实测架构对比｜Operational Ontology vs Text-to-SQL（真实 `Qwen3.8-27B` 四题同场 PK）**：[docs/07-ontology-vs-text2sql.md](docs/07-ontology-vs-text2sql.md)

---

## 目录结构

```text
.
├── config/
│   ├── llm.example.yaml                 # 真实模型接入配置模板（6 家厂商预设）
│   └── llm.local.yaml                   # 本地私有 API / Key 配置（预留待填，已 gitignore）
├── .env.example                         # 环境变量配置模板（LLM_PROVIDER / BASE_URL / MODEL / API_KEY）
├── docs/                                # 全流程工程方法论、验收与三大标准文档
├── ontology/
│   ├── schema.yaml                      # 单一可信源：词表、8类对象、7类关系、12条约束、3个动作
│   ├── crosswalk/customer_crosswalk.yaml             # L3 客户人工确认映射表
│   ├── overrides/product_category_overrides.yaml     # 产品分类治理覆盖表
│   ├── client.py                        # 统一语义查询与图谱邻域客户端
│   ├── actions.py                       # 前置条件守卫 + 审计账本动作引擎
│   ├── agent_tools.py                   # 9 个 AI Agent / MCP 标准语义工具
│   ├── llm_config.py                    # 模型与 API Key 多来源配置加载与安全掩码持久化
│   ├── llm_providers.py                 # 多协议真实模型客户端（OpenAI 兼容 / Anthropic / Gemini）
│   ├── agent_loop.py                    # 真实 LLM Agent Loop（MCP 通道 + 多工具 HITL 恢复 + Token 遥测）
│   ├── text2sql_arena.py                # Ontology vs Text-to-SQL 同模型同题 PK 引擎与原始 10 表沙箱
│   └── w3c/
│       ├── exporter.py                  # W3C OWL 2 / SHACL 1.0 / RDF 1.1 Turtle 导出器
│       ├── rdf_store.py                 # 轻量级 Turtle 1.1 解析器、SHACL 与 RDF-CQ 对账器
│       └── queries/cq[1-4]_*.rq         # 4 份 W3C SPARQL 1.1 标准查询
├── mcp_server/
│   ├── server.py                        # 标准 MCP Server（JSON-RPC 2.0 over stdio）
│   └── mcp_config.example.json          # MCP 客户端挂载配置示例
├── web/                                 # 零依赖交互式 Ontology 图谱与受控动作控制台前端
├── scripts/
│   ├── generate_source_data.py          # 确定性生成带真实脏数据的 CRM/ERP/WMS CSV
│   ├── build_ontology.py                # Schema 驱动的本体物化引擎（SQLite + JSON 图谱）
│   ├── run_cq_report.py                 # CQ1–CQ4 业务报告与“裸查 vs 本体”对照实验
│   ├── demo_agent_workflow.py           # 阶段五沙箱演示（巡检、拦截、催办、修复重建闭环）
│   ├── export_w3c.py                    # 导出并验证 W3C TBox / SHACL / ABox Turtle
│   ├── run_mcp_server.py                # 启动 MCP Server (stdio)
│   ├── run_real_agent.py                # 命令行真实大模型 Agent Loop 调试与连通性测试
│   ├── benchmark_ontology_vs_text2sql.py # 真实大模型 4 题 Ontology vs Text-to-SQL 批量对比评测
│   └── serve_console.py                 # 启动交互式 Web 控制台（默认 http://127.0.0.1:8765）
├── data/
│   ├── sources/{crm,erp,wms}/*.csv      # 不可变源系统数据快照（AS_OF=2026-11-23）
│   ├── ontology/                        # 物化产出：ontology.db + ontology_graph.json
│   └── w3c/                             # W3C 标准产出：ontology_tbox.ttl / shapes_shacl.ttl / instances_abox.ttl
└── tests/
    ├── test_ontology_cqs.py             # 阶段四：外键、图谱一致性、CQ1–CQ4 与边界陷阱测试
    ├── test_ontology_actions.py         # 阶段五：动作守卫、治理闭环重建与 Agent 工具测试
    ├── test_extensions_standards.py     # 三大标准集成测试：MCP stdio / Console HTTP / W3C RDF+SHACL
    ├── test_agent_loop.py               # Agent Loop 集成测试：守卫自愈、HITL 审批/驳回、重建闭环 API
    ├── test_real_llm_protocol.py        # 真实 HTTP LLM 协议测试（OpenAI 兼容 / Anthropic / 掩码 / Token）
    └── test_text2sql_arena.py           # Arena 对比测试：只读 SQL 安全、静默金额截断实证与 Arena API
```

---

## 一键复现与运行

```bash
# 1. 生成 CRM / ERP / WMS 源系统数据并构建本体库
python3 scripts/generate_source_data.py
python3 scripts/build_ontology.py

# 2. 导出并校验 W3C OWL 2 / SHACL 1.0 / RDF Turtle 三元组
python3 scripts/export_w3c.py

# 3. 配置你的真实模型 API 与 Key（任选其一，已 gitignore）
#    - 直接编辑 config/llm.local.yaml
#    - 或复制 .env.example 为 .env 并填入 LLM_API_KEY
#    - 或在 Web 控制台页面顶部面板填入并点击“保存到本地配置”
python3 scripts/run_real_agent.py --show-config
python3 scripts/run_real_agent.py --ping

# 4. 终端直跑真实大模型 Agent Loop（支持交互式 y/n HITL 审批）
python3 scripts/run_real_agent.py "请巡检近30天VIP逾期订单，并查清 SO-20261105-905 为何不能催办"

# 5. 启动交互式可视化图谱、受控动作与真实大模型 Agent Loop 控制台（安全沙箱模式）
python3 scripts/serve_console.py
# 然后浏览器打开 http://127.0.0.1:8765

# 6. 以 MCP Server (stdio) 方式启动，或按 mcp_server/mcp_config.example.json 挂载到外部 Agent
python3 -m mcp_server.server --mode sandbox

# 7. 运行全项目 26 组自动化单元测试与集成测试
python3 -m unittest discover -s tests -v
```
