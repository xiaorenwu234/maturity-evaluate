# 成熟度评估 · 模块一：数据收集与知识建模

> Maturity-Evaluation **Module 1** — Data Collection & Unified System Knowledge Graph

本模块从**四类信息源**（源代码 / 文档 / 部署环境 / 运行时）收集系统信息，构建一张**统一的系统知识图谱（Unified System Knowledge Graph）**。
这张图谱是本模块**唯一产出物**，也是后续两个模块**唯一的数据入口**：

- 模块二：成熟度评估与问题发现
- 模块三：问题修复

---

## 1. 设计概览

```
                     ┌─────────────────── 采集器 Collectors ───────────────────┐
  源代码/仓库  ──▶   │ CodeCollector   (AST / Tree-sitter + Semgrep 框架规则)   │
  架构/接口/配置 ─▶   │ DocumentCollector (Markdown / YAML / JSON / TOML / INI)  │
  云端/K8s 部署 ──▶   │ DeploymentCollector  (STUB — 接口已对齐，待接入)          │
  指标/日志/链路 ──▶   │ RuntimeCollector     (STUB — 接口已对齐，待接入)          │
                     └───────────────────────────────┬──────────────────────────┘
                                                      │  CollectorResult(节点+关系+来源证据)
                                                      ▼
                                        BuildPipeline（合并：节点先、关系后）
                                                      │
                                                      ▼
                                   SemanticEnricher（纯 LLM：OpenAI 兼容 / LangChain）
                                                      │
                                                      ▼
                              Unified System Knowledge Graph  (JSON + Mermaid + Cypher)
```

核心思想（对齐需求）：

- **代码侧**用 `AST / Tree-sitter + 静态调用分析` 得到结构事实；**框架/资源/配置识别**交给开源 SAST 工具 [Semgrep](https://semgrep.dev)（本仓库自带 YAML 规则），LLM 只做静态分析拿不到的语义（是否核心业务链路、模块职责、异常处理含义、配置项控制什么行为）。
- **部署侧 / 运行时侧**直接访问真实环境（K8s API / Prometheus 等），本模块**先置空为 stub**，但节点/关系映射与证据链逻辑已写好并有测试覆盖，后续接入真实客户端即为即插即用。
- **图谱不仅保存关系，还保存信息来源（provenance）**，从而支持模块二给出完整证据链，例如：

  ```
  Payment ──replica──▶ 1     source: Kubernetes
  Order   ──calls───▶ Payment source: source code
  ```

### 本体（Ontology）

| 节点 NodeType | Service / Module / API / Function / Config / Database / Deployment / Metric / Document / ExternalDependency |
|---|---|
| **关系 RelationType** | `CALLS` `DEPENDS_ON` `DEPLOYED_AS` `CONFIGURED_BY` `READS` `WRITES` `EXPOSES` `OBSERVED_BY` |
| **来源 SourceType** | `source_code` `document` `deployment` `runtime` `llm` `manual` |

---

## 2. 目录结构

```
src/kg_builder/
├── config.py                     # 构建设置（含 settings_for + 环境变量加载）
├── cli.py                        # 命令行入口：build / inspect / evidence / export-neo4j
├── graph/                        # 统一知识图谱核心（模块二/三消费的数据契约）
│   ├── ontology.py               # 节点/关系/来源 词表
│   ├── model.py                  # Provenance / KGNode / KGEdge（携带来源证据）
│   ├── knowledge_graph.py        # 图容器：跨来源合并
│   ├── serialization.py          # JSON / Mermaid / Neo4j Cypher
│   └── neo4j_import.py           # 将图谱参数化导入实时 Neo4j
├── collectors/
│   ├── base.py                   # BaseCollector + CollectorResult 协议
│   ├── code/                     # 源代码采集（AST + Tree-sitter + 静态调用分析 + Semgrep 规则引擎）
│   │   ├── code_collector.py
│   │   ├── languages.py          # 语言识别 + Tree-sitter 可用性探测
│   │   ├── artifact.py           # 语言中立中间表示(IR)
│   │   ├── frameworks.py         # 消费 Semgrep findings → RouteDef/ResourceRef/ConfigRef
│   │   ├── semgrep_runner.py     # Semgrep CLI 子进程封装（kgb-json payload 契约）
│   │   ├── semgrep_rules/        # 项目自带的 YAML 规则：routes / resources / configs
│   │   ├── static_analysis.py    # 符号索引 + 调用点解析（调用图）
│   │   └── extractors/           # python(ast) / tree-sitter 提取器（无正则回退）
│   ├── document/                 # 文档采集（架构/接口/配置）
│   ├── deployment/               # K8s/云采集（STUB，映射逻辑已就绪）
│   └── runtime/                  # 指标/日志/链路采集（STUB）
├── enrichment/
│   ├── llm_client.py             # LangChain OpenAI 兼容客户端（未配置即报错，无 Null 回退）
│   └── semantic_enricher.py      # 语义增强：完全由 LLM 产出（核心链路/职责/配置语义）
└── pipeline/
    └── build_pipeline.py         # 编排：采集 → 合并 → 增强 → 落盘
```

---

## 3. 环境准备

项目使用 conda 环境 **CMMeter**（Python 3.12）。

```bash
# 以可编辑模式安装（LLM 语义增强 + Semgrep 框架识别为核心依赖；tree-sitter 为多语言可选）
conda run -n CMMeter pip install -e ".[treesitter,semgrep,dev]"
```

> 语义增强**只用 LLM**，没有离线启发式回退：`llm.model` 与 `llm.api_key` 必须在配置文件/环境变量中提供，否则构建会明确报错（可用 `--skip-enrichment` 仅构建结构图）。
> tree-sitter 为**多语言解析所需**（JS/TS/Java/Go），无回退——缺失某语言语法时该语言文件被跳过并告警；Python 使用标准库 `ast`。kubernetes 为 stub，可选。
> Semgrep 驱动 HTTP 路由 / 数据库 / 外部服务 / 配置读取的**框架语义识别**（取代原来的正则启发式），若未安装则相应 enrichment 步骤被跳过并告警；项目自带规则位于 `src/kg_builder/collectors/code/semgrep_rules/`，新增框架只需再写一份 YAML。

---

## 4. 使用

### 命令行

```bash
# 1) 准备 LLM 配置（OpenAI 兼容），复制示例并按需填写
#    cp config.example.yaml config.yaml
cat > config.yaml <<'YAML'
enrich: true
llm:
  model: gpt-4o-mini
  base_url: https://api.openai.com/v1   # 或任意 OpenAI 兼容网关
  api_key: sk-xxxx
YAML

# 2) 对仓库构建知识图谱（结构 + LLM 语义增强）
conda run -n CMMeter kgb build examples/sample_repo --config config.yaml -o out/sample.kg.json

# 也可用环境变量代替配置文件：KGB_LLM__MODEL / KGB_LLM__API_KEY / KGB_LLM__BASE_URL

# 仅构建结构图（不调用 LLM）
conda run -n CMMeter kgb build examples/sample_repo --skip-enrichment -o out/sample.structural.json

# 查看图谱统计 / 按类型或名称检索节点
conda run -n CMMeter kgb inspect out/sample.kg.json --type Service

# 查看某个实体的完整证据链（体现多来源 provenance）
conda run -n CMMeter kgb evidence out/sample.kg.json PaymentService
```

### 导入 Neo4j（`kgb export-neo4j`）

```bash
# 首次需安装官方驱动
conda run -n CMMeter pip install -e ".[neo4j]"

# 直接把已构建的图谱灌进本地 Neo4j：幂等 MERGE、按标签自动建 id 索引、保留 provenance 证据链
conda run -n CMMeter kgb export-neo4j out/sample.kg.json --uri bolt://localhost:7687 --user neo4j
#   密码：--password 或 env KGB_NEO4J__PASSWORD / NEO4J_PASSWORD（都没有则交互式提示）
#   先清空目标库再导入：加 --clear
```

导入后节点按类型打标签（`:Service`/`:API`/…），`attributes`、`provenance` 以 JSON 字符串存储，`sources` 为字符串数组；例如：
```cypher
MATCH (a:Service)-[r:CALLS]->(b:Service) RETURN a, r, b;
MATCH (n:API) WHERE n.attributes CONTAINS '"is_core_business_link": true' RETURN n.name;
```

### Python API

```python
from kg_builder import build

# 需要 LLM：配置文件/环境变量已配好，或直接注入一个 client
report = build("examples/sample_repo", "out/kg.json")            # 需已配置 llm
# report = build("examples/sample_repo", "out/kg.json", enrich=False)  # 仅结构

graph = report.graph
g = graph.to_networkx()  # 交给模块二跑图算法
```

`kgb build` 会产出三份文件（主 JSON + 两种可视化导出）：

- `out/kg.json` — 统一知识图谱（模块二/三的数据入口）
- `out/kg.mmd` — Mermaid 图，便于快速查看
- `out/kg.cypher` — Neo4j `MERGE` 语句，便于导入真实图库

---

## 5. 关于开源复用

- **tree-sitter**（`tree-sitter` + `tree-sitter-language-pack`）作为多语言 AST 解析引擎；Python 额外使用标准库 `ast`（对调用/导入解析最准确、零依赖）。
- **Semgrep**（`semgrep>=1.60`）驱动框架/资源/配置识别：项目自带的 YAML 规则位于 `src/kg_builder/collectors/code/semgrep_rules/`，`CodeCollector` 每次构建会 fork 一次 `semgrep --config ... --json` 扫描全仓，`semgrep_runner` 把每条 finding 的 `kgb-json` payload 反解成结构化字段，`frameworks.enrich_all` 再分配回各自的 `FileArtifact`。命中不了的调用保持为普通调用/导入，不误判；新增框架只需再补 YAML。
- 图容器基于 **networkx**（`to_networkx()` 直接输出给模块二）。
- LLM 语义增强通过 **LangChain** 调用 **OpenAI 兼容** 接口（`langchain` + `langchain-openai`，可在 `config.yaml` 的 `llm:` 配置 model/base_url/api_key）；**无离线启发式回退**。

如需接入其它专用调用图工具（例如针对特定语言的静态分析器），只要在 `collectors/code/extractors/` 新增一个提取器并产出 `FileArtifact` IR 即可；如需增加框架识别能力，只需往 `semgrep_rules/` 里再丢一份 YAML 规则，`frameworks.enrich_all` 自动消费——两者都不需要改动流水线。

---

## 6. 测试

```bash
conda run -n CMMeter pytest -q
```

覆盖：图谱合并与来源证据链、序列化往返、代码采集（`Order→Payment` 服务级调用、`/order/create` 暴露、DB 读写、外部依赖、配置关系）、部署/运行时 stub 映射、语义增强、端到端流水线与跨来源合并。

---

## 7. 后续（TODO）

- `DeploymentCollector._fetch_workloads()`：接入 Kubernetes API（`kubernetes` 客户端），产出 replica / CPU-Mem Limit / HPA / Readiness-Liveness Probe / Region-AZ。
- `RuntimeCollector._fetch_metrics()`：接入 Prometheus / 日志 / 链路，产出 `Metric` 节点与 `OBSERVED_BY`。
- 跨语言调用命名规范化 / 更强的类型推断（提升调用图召回）。
