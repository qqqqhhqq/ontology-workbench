# 扩展工程标准：MCP Server · 交互式可视化控制台 · W3C 语义网标准

在完成基础五阶段（源数据 → 本体规范 → 实体解析物化 → CQ1–CQ4 验证 → 受控动作引擎）之后，本项目进一步按三套工业标准完成了完整集成，且全部实现**零第三方重依赖**，开箱即用并通过 17 组集成测试。

---

## 标准一：Model Context Protocol (MCP) Server（JSON-RPC 2.0 over stdio）

将 `ontology/agent_tools.py` 封装为严格遵循 MCP 协议的独立服务端，可直接被 Claude Desktop、Cursor、Antigravity 等任何 MCP 客户端挂载。

### 核心产物
- MCP 服务端实现：[mcp_server/server.py](../mcp_server/server.py)
- 快捷启动脚本：[scripts/run_mcp_server.py](../scripts/run_mcp_server.py)
- 客户端标准挂载配置：[mcp_server/mcp_config.example.json](../mcp_server/mcp_config.example.json)

### 协议能力清单
1. **生命周期**：`initialize`（能力协商与指令注入）、`notifications/initialized`、`ping`。
2. **Tools（9 个）**：6 个只读语义工具 + 3 个前置守卫动作工具，遵循 camelCase `inputSchema` 与 MCP `annotations`（`readOnlyHint`、`destructiveHint`、`idempotentHint`）；当守卫拦截（`PreconditionError`）时按规范返回 `isError: true`。
3. **Resources（8 个）**：`ontology://schema`、`ontology://summary`、`ontology://cq/1` ~ `ontology://cq/4`、`ontology://w3c/tbox`、`ontology://w3c/shacl`。
4. **Prompts（2 个）**：`vip_fulfillment_patrol`（VIP 逾期巡检与先预演后催办流程）、`data_governance_audit`（高危异常审计与修复建议流程）。
5. **运行模式**：默认 `--mode sandbox`（隔离临时沙箱，放心试跑动作不污染基线），也支持 `--mode live` 直连工作区。

```bash
# 手动启动 stdio MCP Server（默认 sandbox 模式）
python3 -m mcp_server.server --mode sandbox
```

---

## 标准二：交互式可视化图谱与受控动作控制台（Web Workbench）

纯原生 HTML5 Canvas + CSS3 + ES6 前端（零 npm / 零外网 CDN）+ Python 标准库多线程 HTTP 服务端，提供企业级 Operational Ontology 交互式工作台。

### 核心产物
- 控制台服务端与 REST API：[scripts/serve_console.py](../scripts/serve_console.py)
- 前端界面与交互引擎：[web/index.html](../web/index.html)、[web/styles.css](../web/styles.css)、[web/app.js](../web/app.js)

### 五大工作台（默认接入真实大模型 Agent Loop，预留 API & Key）
0. **AI Agent 闭环中心（真实 LLM + MCP + HITL，默认首页）**：
   - 核心引擎：[ontology/agent_loop.py](../ontology/agent_loop.py)、多协议 HTTP 客户端：[ontology/llm_providers.py](../ontology/llm_providers.py)、配置管理：[ontology/llm_config.py](../ontology/llm_config.py)。
   - **默认真实模型模式（`real_llm`）**：所有工具调用经由 **MCP JSON-RPC 2.0（`tools/list`, `tools/call`）** 执行，原生支持 **OpenAI 兼容（OpenAI / DeepSeek / 通义千问 / Ollama）、Anthropic Claude（`/v1/messages`）、Google Gemini（`:generateContent`）** 三大协议，记录真实 `reasoning_content` / `thinking`、Token 用量与推理延迟；若 API Key 仍为预留空值，明确提示配置指引，绝不伪造模拟答案（离线规则演示需手动切换下拉框）。
   - **预留 API & Key 配置落盘**：[config/llm.example.yaml](../config/llm.example.yaml)、[config/llm.local.yaml](../config/llm.local.yaml)（已入 `.gitignore`）与 [.env.example](../.env.example)，界面支持一键填充 6 家主流厂商端点、掩码保存密钥与 **🔌 模型连通性探活**；另提供终端调试脚本 [scripts/run_real_agent.py](../scripts/run_real_agent.py)。
   - **Human-in-the-Loop（HITL）人工审批闸门**：完整支持单轮并发多 `tool_calls` 时的中途挂起、排空恢复与人工批准/驳回。
1. **业务驾驶舱（CQ1–CQ4）**：顶部 4 项实时 KPI；CQ1 逾期订单表支持一键 `Dry Run` / 提交催办；CQ2 客户 360° 支持更名前旧名（如“博思咨询”）实时检索；CQ3 堆叠条形图展示严格合规/非叶子上卷/异常挂账与 100% 金额守恒。
2. **交互式知识图谱（Graph Explorer）**：Canvas 力导向布局，支持拖拽节点、滚轮缩放、画布平移、关键词定位与 **5 个预设聚焦视图**（核心业务全览、VIP 逾期履约链路、客户实体对齐图、产品分类树与异常、数据质量冲突聚焦）；带质量异常节点高亮红/琥珀色预警环，点击节点在右侧 **Object Inspector** 查看属性、血缘、挂载异常并点击邻居跳转下钻。
3. **受控动作演练台（Action Playground）**：内置 **6 个一键案例预设**（3 个守卫拦截反例 + 3 个合法闭环正例），支持实时查看 `PreconditionError` 拦截详情、触发本体重建后 KPI/图谱/账本即时联动刷新，并支持顶部 **一键重置沙箱**。
4. **质量审计 & W3C / MCP 检视器**：支持按级别/规则码过滤 `DQ-001` ~ `DQ-012` 并直达图谱节点；内置代码浏览与复制面板查看 OWL TBox、SHACL Shapes、SPARQL 1.1 查询与 MCP 配置。

```bash
# 启动控制台（默认 http://127.0.0.1:8765，安全沙箱模式）
python3 scripts/serve_console.py
```

---

## 标准三：W3C 语义网标准导出（OWL 2 / SHACL 1.0 / RDF Turtle 1.1 / SPARQL 1.1）

严格按 W3C 规范将本体模式、业务约束与全部实例导出为标准 Turtle 1.1 与 SPARQL 1.1 查询，可直接导入 **Apache Jena Fuseki、GraphDB、Oxigraph、Blazegraph、Stardog 或 Python `rdflib` / `pyshacl`**。

### 核心产物
- 导出引擎：[ontology/w3c/exporter.py](../ontology/w3c/exporter.py) · 运行脚本：[scripts/export_w3c.py](../scripts/export_w3c.py)
- 轻量级 Turtle 1.1 解析器、内存三元组库与 SHACL / CQ 对账器：[ontology/w3c/rdf_store.py](../ontology/w3c/rdf_store.py)
- 导出的 3 份标准 Turtle 文件：
  - [data/w3c/ontology_tbox.ttl](../data/w3c/ontology_tbox.ttl)（462 triples — OWL 2 Classes、SKOS 受控词表、`owl:TransitiveProperty` 传递分类属性）
  - [data/w3c/shapes_shacl.ttl](../data/w3c/shapes_shacl.ttl)（110 triples — `sh:NodeShape` / `sh:PropertyShape` 与 `sh:SPARQLConstraint` 跨系统规则）
  - [data/w3c/instances_abox.ttl](../data/w3c/instances_abox.ttl)（7,188 triples — 全量 8 类对象、精确 XSD 类型字面量、`skos:altLabel` 别名与全部语义边）
- 4 份标准 W3C SPARQL 1.1 查询（[ontology/w3c/queries/](../ontology/w3c/queries/)）：
  - [cq1_vip_overdue.rq](../ontology/w3c/queries/cq1_vip_overdue.rq)
  - [cq2_customer_360.rq](../ontology/w3c/queries/cq2_customer_360.rq)
  - [cq3_category_sales.rq](../ontology/w3c/queries/cq3_category_sales.rq)（使用标准 SPARQL 1.1 Property Path `b2b:parentCategory*` 做分类树传递闭包聚合）
  - [cq4_dq_audit.rq](../ontology/w3c/queries/cq4_dq_audit.rq)

### 实跑交叉校验结论
- **语法 100% 合法**：3 份 `.ttl` 文件均通过严格 Turtle 1.1 递归下降解析器校验（共 7,760 条三元组）。
- **SHACL 校验精确对齐**：`sh:Violation=10`（精确对应 10 条 HIGH 级规则）、`sh:Warning=19`、`sh:Info=4`。
- **RDF 与 SQLite 比特级一致**：直接在解析后的 RDF 三元组上运行 CQ1 与 `b2b:parentCategory*` 传递闭包 CQ3，结果（4 张订单与 `¥1,959,142.90 / ¥3,498,978.40 / ¥414,185.30`）与 SQLite `OntologyClient` 100% 一致。
