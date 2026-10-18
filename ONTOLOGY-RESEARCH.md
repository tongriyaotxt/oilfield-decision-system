# Ontology 研究笔记

> 面向"要真的用它干活"的读者：为什么需要本体、它和数据库/知识图谱差在哪、
> 哪些东西必须写进 OWL、哪些必须写进 SHACL、以及 2025 年前后 LLM 带来的变化。
> 文末附本目录里一个**可运行**的最小工程（342 条断言 → 1182 条物化三元组）逐文件对照。

---

## 0. 先给结论

1. **本体的价值不在"存知识"，而在"把判定规则从代码里搬到模型里"。**
   分类、传递、互斥、继承这些逻辑一旦形式化，就只写一次、被 N 个系统复用；
   而贴在数据上的 `type` 标签，永远活在各自的 ETL 脚本里。
2. **OWL 和 SHACL 不是竞品，是分工。** OWL 是开放世界（能推出什么），SHACL 是闭世界
   （必须填什么）。只用 OWL → 字段缺失永远不报错；只用 SHACL → 拿不到任何推理收益。
   **流水线顺序必须是"先物化，再校验"**，否则针对推理类型的约束全打空。
3. **选 profile 比选语言重要。** 绝大多数生产系统落在 OWL 2 RL（三元组可物化、能落图库、
   规则引擎可解释），而不是 OWL 2 DL 全量（要挂 HermiT/ELK，且推理结果不是三元组）。
4. **本体工程的真实瓶颈是治理，不是建模。** IRI 命名、版本、谁有权改类、冲突怎么裁，
   决定这套东西三个月后是资产还是垃圾。
5. **LLM 改变了本体的"生产成本"，没改变"判定权"。** LLM 适合抽取候选类/关系、
   写标签和定义、做自然语言到 SPARQL；不适合当一致性裁判——那件事必须交给推理器。

---

## 1. 两条血脉：哲学本体论 vs 计算本体

| | 哲学 ontology | 计算 ontology |
|---|---|---|
| 问题 | 存在什么？（What is there?） | 某个领域里，我们约定用哪些范畴和关系来描述世界？ |
| 判定 | 真假（形而上学承诺） | 有用性 / 一致性 / 可复用性 |
| 产物 | 范畴体系（亚里士多德十范畴、康德十二范畴） | 一个形式化的词汇表 + 公理集（Turtle/OWL 文件） |
| 典型争议 | 共相是否实在、本质主义 | 该不该用 `partOf` 表达"装配"、材料算不算 continuant |

两条线在 **Quine 的"ontological commitment"**（一个理论承诺了哪些实体存在）处交汇：
一个形式语言量化到什么对象上，就承诺了什么。这也是为什么"给类起名"在本体工程里
是一件严肃的事——名字背后是承诺，不是标签。

计算方向公认的起点是 Gruber (1993) 的定义：

> **an ontology is an explicit specification of a conceptualization**
> （本体是对某一概念化的显式规范说明）

后来的修正值得记住两条：

- **Studer, Benjamins & Fensel (1998)**：补上 *formal* 和 *shared*——
  必须是形式化的（机器可推），且是共享的（不是某个人脑子里的模型）。
- **Guarino (1998)**：`conceptualization` 应是 *intensional* 的，即
  描述概念的内涵（什么条件下算这个类的实例），而不是枚举外延（现在有哪些实例）。
  **这条区分直接决定了"分类走推理、而不是走枚举"的工程写法。**

---

## 2. 语义阶梯：别把词表叫成本体

工程上按"形式化程度"分层，越往下越贵、也越值钱：

```
词表 Term list            →  术语清单，无结构
术语表 Glossary           →  + 自然语言定义
分类树 Taxonomy           →  + 上下位（rdfs:subClassOf）
叙词表 Thesaurus          →  + 同义/相关（SKOS: altLabel / related）
概念模型 Conceptual model →  + 属性、域值、基数（UML/ER 级）
形式本体 Formal ontology  →  + 公理：互斥、传递、逆、属性链、定义类（OWL/DL）
```

判断一个项目处在哪一层的硬指标：**有没有"定义类"（defined class）**。
如果所有 `rdf:type` 都是人手工贴上去的，那它只是"带 URI 的 ER 图"，
不是本体——因为系统里不存在任何"由规则得出的类型"。

本仓库 demo 就是冲着这一点写的：`Assembly / System / Actuator / SafetyEquipment /
SafetyRelevantPart / DocumentedPart` 这 6 个类，**数据文件里一次都没写过**，
全部由公理推出（见 §12 实测）。

---

## 3. 形式骨架：DL、OWL 2 与三个 profile

OWL 2 的语义基础是描述逻辑 **SROIQ(D)**。工程上不需要会写公式，但必须知道三件事：
表达力、计算复杂度、以及"推理结果是三元组还是分类树"。

| Profile | 表达力 | 推理复杂度 | 典型用途 | 能不能物化成三元组 |
|---|---|---|---|---|
| **OWL 2 EL** | 交集、存在量词、传递属性 | PTime | 生物医学大本体（SNOMED、GO）、超大规模 | 否（分类为主，ELK 秒级） |
| **OWL 2 QL** | 受限，可rewrite成 SQL | AC⁰（查询重写） | 直接架在关系库上做 OBDA | 否（重写成 SQL） |
| **OWL 2 RL** | 规则可实现子集 | PTime，可增量 | 图数据库、规则引擎、CI 物化 | **是**（owlrl / GraphDB / Neptune） |
| **OWL 2 DL** | 全量（含基数、否定、析取） | N2ExpTime（最坏） | 精细建模、一致性审计 | 否（HermiT/Openllet 给分类） |

**选型口诀**：数据量大 + 要落库 + 要解释 → RL；本体巨大且以分类为主 → EL；
要挂在既有 RDBMS 上 → QL；只有"必须精确捕捉细微约束"时才付 DL 的代价。

⚠️ **RL 的边界要背下来**（本 demo 踩过）：
RL 不支持用 `minCardinality / maxCardinality(≥2)` 做分类。规则表里只有
`maxCardinality 0/1` 的类规则。所以 `System ≡ PhysicalObject ⊓ ≥2 hasPart.Part`
这种写法在 RL 下**不会**把实例分类成 `System`，必须改写成
`∃hasPart.Assembly` 这类存在量词形式（本仓库正是这么写的）。

其他常用构造在本 demo 里的落点：

| 构造 | 语义 | 本项目用法 |
|---|---|---|
| `owl:inverseOf` | 逆属性 | 只断言 `partOf`，自动得到 `hasPart` |
| `owl:TransitiveProperty` | 传递 | `partOf` 传递 → 螺栓属于整机 |
| `owl:propertyChainAxiom` | 属性链 | `partOf ∘ locatedIn ⊑ locatedIn`：位置沿分解树下传 |
| `owl:equivalentClass` + 交集 + `someValuesFrom` | 定义类 | 6 个类的分类全靠它 |
| `owl:disjointWith` | 互斥 | `Equipment ⊓ Part = ⊥`：抓错分类 |
| `owl:FunctionalProperty` | 函数性 | 序列号唯一 |

---

## 4. 三个最容易翻车的世界假设

这三条是"从 SQL 思维切到本体思维"的真正门槛。

### 4.1 开放世界假设（OWA）

数据库里"没填 = 空"。OWL 里"没写 ≠ 不存在"，只是"还不知道"。
所以：

- `maxCardinality 0` 不能把"没有任何 hasPart 断言"的个体推成"叶子零件"——
  它只是"目前没证据说有"，不等于"证明没有"。
- **"必填字段"在 OWL 里根本无法表达。** 想强制，只有两条路：
  ① SHACL（推荐）；② 闭包公理 / 关闭世界的一阶改写（贵且难维护）。

本 demo 的 `Motor_M` 没写材质，OWL 物化后一句不吭；`pyshacl` 立刻报
"零件必须声明材质"。**这就是 SHACL 存在的全部理由。**

### 4.2 无唯一命名假设（no UNA）

`eq:Press01` 和 `eq:Press_01` 在 OWL 里**可能指同一个东西**，除非你写
`owl:differentFrom`。合并靠 `owl:sameAs`（推理器还可能自己推出来）。
工程后果：跨源对齐时，重复实体不会自动暴露，必须显式做 entity resolution，
或者用 SHACL/SPARQL 做唯一性断言（本 demo 的 `SerialUniquenessShape` 就是干这个的）。

### 4.3 Punning 与"类 vs 个体"

OWL 2 DL 允许同名同时作为类与个体（punning），但**不要用**。
一个概念要么是类（`Material`），要么是个体（`mat:Steel`）。
混用会让约束、查询、推理全部变形。本 demo 严格分层：`ind:Material` 是类，
`mat:Steel` 是它的个体。

### 4.4 域/值域是"类型推土机"，会凭空造节点

`rdfs:domain` / `rdfs:range` **不是**校验规则，而是**推理规则**：
断言 `x p y` 且 `p range C`，推理器就直接得出 `y a C`——哪怕 `y` 这个 IRI
从来没被声明过、甚至根本是拼错的。

本仓库实测踩到：把 `cad:Frame-Drawing` 误写成 `cad:Frame_Drawing` 并断言
`specifiedIn` 后，物化图里凭空出现一个 `ind:CADModel` 实例，导致 SHACL 报
"CAD 模型必须且只能有一个版本号"——**报错位置看着莫名其妙，根因是拼写**。
修正 IRI 后 `conforms = True`。

三条工程结论：

1. 别用 domain/range 当"必填/类型检查"，它做不到；要校验请用 SHACL 的
   `sh:class` / `sh:nodeKind`；
2. IRI 拼写错误在本体里是**静默新增实体**，必须有 IRI 规范 + 引用完整性检查；
3. 这也是"先物化再校验"的额外收益：凭空节点的缺陷会在校验环节显形。

---

## 5. 上层本体：别从白纸开始

| 上层本体 | 特点 | 适合谁 |
|---|---|---|
| **BFO**（ISO/IEC 21838-2） | 极简、严格，continuant/occurrent 二分；OBO Foundry 生态标配 | 生物医学、需要跨本体对齐的严肃场景 |
| **DOLCE** | 认知取向，强调"自然语言概念" | 语言/认知相关、需要精细区分 |
| **SUMO** | 体量大、公理多，带丰富中层词汇 | 通用推理、教学 |
| **ISO/IEC 21838-1/3** | 上层本体的标准化框架（含 BFO、ISO 15926 等） | 需要"有标准可引"的合规场景 |
| **gIST / IDEAS** | 商业/企业取向，落地快 | 企业级 KG 起步 |
| **Schema.org** | 轻量、SEO 生态、无公理 | Web 数据交换（不要指望它推理） |

**实践建议**：不要一上来就 import 整个 BFO。做法是"**借骨架、不借全身**"——
本 demo 只借了 continuant / occurrent / information entity 三分（约 15 行），
就足以避免最常见的建模事故（把"功能""过程""文档"当成物理对象）。
需要严格对齐时再上完整 BFO + RO（Relation Ontology）。

领域标准同样能省大量工作：

- 工业/制造：**IEC 61360 / ECLASS**（属性字典）、**ISO 15926**（流程工业）、
  **IFC**（BIM）、**AAS / IDTA 子模型**（工业 4.0 资产管理壳）、**OPC UA 信息模型**
- 生物医学：**OBO Foundry**、SNOMED CT、Gene Ontology、Uberon
- 金融：**FIBO**
- Web：**schema.org**、**SKOS**（词表/叙词表，不是本体）

选标准 vs 自建的判断：**跨组织交换 → 用标准；内部判定逻辑 → 自建 + 借骨架。**

---

## 6. 方法论与质量：让本体可评审

### 6.1 需求侧：Competency Questions（CQ）

先写 20–50 条"系统必须能回答的问题"，每条配一个 SPARQL。
本体能不能交付，看 CQ 覆盖率，而不是看类有多少个。
本 demo 的 `queries/q1–q4` 就是 4 条 CQ：能回答 = 通过。

### 6.2 建模侧：OntoClean

用四个元属性给类"体检"，专治 `subClassOf` 乱挂：

| 元属性 | 问什么 | 反面案例 |
|---|---|---|
| **Rigidity（刚性）** | 是不是"必然如此"？ | 把 `Employee`（角色，anti-rigid）挂在 `Person` 下当子类 → 人离职就不是 Employee，分类崩了 |
| **Identity（同一性）** | 靠什么判定"同一个"？ | `AmountOfMaterial` 与 `Car` 混在一条继承链上 |
| **Unity** | 有没有"整体性"判据？ | 把 `Steel`（部分-整体界限模糊）当 `PhysicalObject` |
| **Dependence** | 是否必须依附别的东西存在？ | `Marriage` 不能独立存在 → 不该是 continuant |

**最实用的一条**：抗刚性概念（角色、状态、阶段）**不要**放进分类树的主干，
要用"角色模式"（Role pattern）单独建模。本 demo 里 `SafetyEquipment` 就是典型：
它不是一个"出厂就定"的类别，而是"是否被安全要求约束"的角色，
所以做成**定义类**（依据 `governedBy`），而不是让人手工贴标签。

### 6.3 评审侧：自动化检查

- **OOPS! (OntOlogy Pitfall Scanner)**：在线扫 40+ 类常见陷阱（缺注释、缺逆属性、
  循环继承、把 `is-a` 用成 `part-of` 等）。适合作为 PR 门禁的第一道。
- **ROBOT report**：ODK 生态里的本体 CI 工具（一致性、废弃类、标签完整性）。
- **自建规则**：本仓库 `scripts/consistency.py` 示范了两条最要紧的：
  互斥类冲突、函数属性取值冲突。
- **SHACL 测试套件**：把"已知的正确/错误数据"固化成回归用例。

---

## 7. 工具栈（按职责分，不要混）

| 层 | 选项 | 备注 |
|---|---|---|
| 编辑器 | **Protégé / WebProtégé**、Cellfie（表格→本体）、Excel 模板 + ROBOT | 业务专家参与靠 WebProtégé |
| 序列化 | **Turtle**（人读）、JSON-LD（Web）、RDF/XML（工具间）、N-Triples（流） | 版本控制里用 Turtle，diff 友好 |
| 推理 | **HermiT / Openllet**（DL，需 JVM）、**ELK**（EL，极快）、**owlrl**（RL，纯 Python）、GraphDB/Stardog 内置 | 生产多用"RL 物化 + 必要时离线 DL 审计" |
| 查询 | **SPARQL 1.1**；库内 Cypher/Gremlin 只作补充 | 属性路径 `+`/`*` 是本体查询的主力 |
| 约束 | **SHACL**（W3C Rec）、ShEx | pyshacl / Apache Jena SHACL |
| 存储 | GraphDB、Stardog、Apache Jena Fuseki、Amazon Neptune、Oxigraph、Virtuoso | 选型看"是否自带 RL 推理 + 是否有 SHACL" |
| 版本/CI | Git + ROBOT + 自建 SHACL 回归 + OOPS! | 本体必须像代码一样 review |

> 注意：**Neo4j 不是 RDF 存储**。它能存"图"，但没有 OWL 语义、没有 SPARQL、
> 没有标准推理。用它当本体库 = 自己重写一遍推理器。除非只要拓扑查询。

---

## 8. 知识图谱 ≠ 本体

这是最常见的预算错配来源：

| | 本体（Ontology / TBox） | 知识图谱（KG / ABox + 数据） |
|---|---|---|
| 内容 | 类、属性、公理 | 实例、关系、字面值 |
| 变化频率 | 低（受控、需评审） | 高（持续写入） |
| 谁负责 | 领域专家 + 语义工程师 | 数据工程 |
| 失败模式 | 建模错 → 全链路错 | 脏数据 → 局部错 |
| 度量 | CQ 覆盖率、一致性、复用率 | 覆盖度、新鲜度、查询延迟 |

工程含义：**本体是 schema，KG 是数据**。把它们混成一个"大图"里随便写，
三个月后没人能回答"这个类到底什么意思"。正确做法是 TBox/ABox 分文件、
分仓库、分发布节奏（本 demo：`ind-core.ttl` / `ind-data.ttl` 分离）。

---

## 9. 落地架构（四层，可直接照抄）

```
┌─ 数据层 ───────────────────────────────────────────────┐
│  MES / PLM / CAD / IoT 时序 / 文档   →  mapping (RML/R2RML 或代码) │
├─ 本体层（TBox，受控发布）──────────────────────────────┤
│  上层骨架 + 领域类 + 定义类 + 互斥/传递/属性链            │
├─ 约束层（SHACL，闭世界）───────────────────────────────┤
│  必填、正则、值域、唯一性、跨字段一致性                    │
├─ 服务层 ───────────────────────────────────────────────┤
│  SPARQL 端点 / GraphQL / 向量+图混合检索 / LLM 工具调用     │
└────────────────────────────────────────────────────────┘
   CI 门禁：语法 → 一致性(RL+规则) → SHACL 回归 → OOPS! → CQ 通过率
```

**顺序铁律**：`解析 → 物化(OWL RL) → SHACL 校验 → 入库`。
反过来（先校验后物化）会让针对推理类型的约束静默失效——
本 demo 的 `SafetyRelevantPart` 约束就是只有物化后才命中 3 个实例的。

---

## 10. 2024–2026 的前沿：LLM × 本体

四个方向，成熟度差别很大：

1. **本体作为 LLM 的"约束/校验器"（成熟，可直接用）**
   LLM 输出 → 映射到本体 → 用 SPARQL/SHACL/推理器校验 → 不合规就让它重写。
   这是把"幻觉"变成"可检出的结构化错误"最省力的办法。
2. **本体作为检索结构（GraphRAG 方向，工程化中）**
   用本体/KG 做实体归一和子图检索，弥补纯向量检索在"多跳、聚合、可解释"上的短板。
   关键仍在本体质量：schema 烂，子图检索只会更快地给出错答案。
3. **LLM 辅助建模（半自动，需人工把关）**
   从文档/表格抽候选类与关系、生成 `rdfs:label`/`skos:definition`、
   起草 CQ、生成 SPARQL、解释不一致报告。**不要**让它直接决定公理。
   已有系统性综述（含生物本体方向）总结这类"LLM 参与本体工程"的模式与风险。
4. **神经符号 / 可验证推理（研究为主）**
   LLM 负责"提议"，DL/规则负责"裁决"。评价指标从"回答像不像"转向
   "结论是否可被形式系统证明"。

一句提醒：**LLM 让本体的边际成本下降，但也让"没人维护的伪本体"数量暴涨。**
判断标准没变——能否被推理器消费、能否支撑 CQ。

---

## 11. 90 天落地路线（可直接排期）

**第 1–2 周｜对齐**
- 写 20–50 条 CQ（"谁能回答什么问题"），每条落成 SPARQL 骨架
- 划边界：哪些概念**不**进本体（留在业务库）

**第 3–4 周｜骨架**
- 借上层三分（continuant / occurrent / information），画 50–150 个类的初稿
- 用 OntoClean 体检一遍主干继承链；抗刚性概念一律做成角色/定义类

**第 5–8 周｜公理与约束**
- 补 5 类关键公理：互斥、传递、逆、属性链、定义类
- SHACL 只写"不许错"的（10–30 条），别把 BI 校验全搬进来

**第 9–10 周｜工具链**
- Git + CI：语法 → 一致性 → SHACL 回归 → OOPS! → CQ 通过率
- 出一个只读物化结果的 SPARQL 端点给业务试跑

**第 11–13 周｜接入与度量**
- 接 1–2 个真实源（映射脚本 + 幂等重跑）
- 度量：CQ 通过率、一致性告警数、类复用率（被几个系统 import）、
  人工补标签的比例（应逐月下降）

---

## 12. 本目录的 demo：逐文件地图与实测结果

```
ontology/
  ind-core.ttl        TBox：上层三分 + 27 个类（含 6 个定义类）+ 12 个对象属性 + 4 个数据属性
  ind-data.ttl        ABox：1 台冲压机 / 1 台泵 / 4 个部件 / 3 个模型（故意留 3 类缺陷）
  ind-shapes.ttl      SHACL：序列号格式与唯一性、材质必填、安全件必建档、闭世界模型
  ind-inconsistent.ttl 故意互斥的数据，给一致性检查用
queries/
  q1_types.rq         CQ1：哪些分类是推理出来的，数据里一条没写
  q2_bom_count.rq     CQ2：按设备统计 BOM（依赖传递闭包）
  q3_gaps.rq          CQ3：安全设备上哪些零件缺模型
  q4_integrity.rq     CQ4：完整性诊断（缺质量/缺模型），非强制
scripts/
  build.py            物化 + 计数 + 跑全部 CQ + 导出 build/inferred.ttl
  validate.py         SHACL 校验（跑在物化图上）
  consistency.py      三层不一致检查：RL / SPARQL 规则 / SHACL
```

### 实测（`python scripts/build.py`）

```
断言图 (TBox+ABox)        342 三元组
OWL 2 RL 物化后           1182 三元组  (+840)
新增类型断言               66 条
hasPart 边  6 → 9         （逆属性 + 传递）
partOf  边  6 → 9
locatedIn 边              6   （1 条断言经属性链传播成 6 条）
```

推理得到的分类（**数据文件里 0 次出现**）：

| 定义类 | 数量 | 依据 |
|---|---|---|
| `Assembly` | 2（Frame、Drive） | `Part ⊓ ∃hasPart.Part` |
| `System` | 1（Press01） | `PhysicalObject ⊓ ∃hasPart.Assembly`（两级传递） |
| `Actuator` | 1（Motor_M） | `Part ⊓ ∃realizesFunction.Actuation` |
| `SafetyEquipment` | 1（Press01） | `Equipment ⊓ ∃governedBy.SafetyRequirement` |
| `SafetyRelevantPart` | 5 | `Part ⊓ ∃partOf.SafetyEquipment`（靠 `partOf` 传递扩散） |
| `DocumentedPart` | 3 | `PhysicalObject ⊓ ∃specifiedIn.CADModel` |

`PartShape` 之外，SHACL 报出 5 条违规，正好覆盖 3 类缺陷：
`Pump07` 缺序列号、`Motor_M` 缺材质、`Drive/Bolt_M8/Weld_01` 未建档（安全件）。

一致性检查的结论值得单独记一笔：

```
[1] OWL 2 RL 物化后 owl:Nothing = 空   ← RL 只做正向蕴含，不判互斥冲突
[2] SPARQL 互斥规则 → 抓到 Ghost 同时是 Equipment 和 Part
[3] SHACL sh:not + sh:class → 用自然语言复述同一问题
```

**要真正的 DL 一致性判定（基数、不可满足类、存在性蕴含），得挂 HermiT/ELK（需 JVM）。**
本机无 Java，所以走的是"RL 物化 + 显式规则 + SHACL"的工程路线——
这也正是大多数生产系统的实际选择。

### 想亲手验一遍

```powershell
cd D:\project\ontology
python -m pip install rdflib owlrl pyshacl
python scripts\build.py        # 推理 + CQ + 导出 build/inferred.ttl
python scripts\validate.py     # SHACL：预期 5 条违规，退出码 1
python scripts\consistency.py  # 预期：RL 不报、SPARQL/SHACL 抓到
```

把 `ind-data.ttl` 里补上 `ind:hasSerialNumber`（Pump07）、`ind:hasMaterial`（Motor_M）、
`ind:specifiedIn`（Drive / Bolt_M8 / Weld_01），`validate.py` 就应当转绿、退出码 0
（已实测：`conforms = True`）。
**这就是本体的回归测试长什么样。**

---

## 13. 参考文献与入口

**标准与规范**
- OWL 2 Overview / Profiles / New Features — <https://www.w3.org/TR/owl2-overview/> 、<https://www.w3.org/TR/owl2-profiles/>
- SHACL（Shapes Constraint Language, W3C Rec）— <https://www.w3.org/TR/shacl/>
- SPARQL 1.1 Query Language — <https://www.w3.org/TR/sparql11-query/>
- SKOS Reference — <https://www.w3.org/TR/skos-reference/>
- ISO/IEC 21838-2:2021（Top-level ontologies, Part 2: BFO）— [标准条目](https://cssn.net.cn/cssn/productDetail/f9b77bb81e870d9aba00d2a51f444816)

**定义与方法论**
- Gruber, T. (1993) *A Translation Approach to Portable Ontology Specifications*
- Studer, Benjamins & Fensel (1998) *Knowledge Engineering: Principles and Methods*
- Guarino, N. (1998) *Formal Ontology and Information Systems*
- OntoClean（rigidity / identity / unity / dependence）— [Wikipedia 词条](https://en.m.wikipedia.org/wiki/OntoClean)
- OOPS! 陷阱目录与用户手册 — <http://oops.linkeddata.es/OOPSUserGuidev1.pdf> 、<https://oops.linkeddata.es/>

**生态与工具**
- OBO Foundry — <https://obofoundry.org/>
- Protégé — <https://protege.stanford.edu/>
- ROBOT（本体 CI）— <https://github.com/ontodev/robot>
- RDFLib / owlrl / pySHACL — <https://rdflib.readthedocs.io/> 、<https://github.com/RDFLib/OWL-RL> 、<https://github.com/RDFLib/pySHACL>

**LLM × 本体（近期综述）**
- *Large Language Models in Bio-Ontology Research: A Review* — [PubMed](https://pubmed.ncbi.nlm.nih.gov/41301216/)
- *Ontology Engineering for Trustworthy AI: A Production-Grounded Survey of Knowledge Graphs in the LLM Era* — [PDF](https://www.researchgate.net/profile/Dheeraj-Kumar-151/publication/405935402_Ontology_Engineering_for_Trustworthy_AI_A_Production-Grounded_Survey_of_Knowledge_Graphs_in_the_LLM_Era)
- GraphRAG（微软研究院项目页）— <https://www.microsoft.com/en-us/research/project/graphrag/>
