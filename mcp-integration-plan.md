# 网页系统查询 + Excel 信息归集 MCP 方案

> 版本：v0.3
> 目标：用 2 个 MCP Server，把「固定网页系统查询 → 信息提取 → Excel 归集」串成可被 AI 客户端直接调用的工具链。
>
> 当前状态：`excel-mcp` 与 `web-mcp` 均已完成，18 个测试通过；下一步是把真实站点接进流程（P3）。

---

## 1. 结论先行

| # | MCP Server | 职责 | 外部依赖 | 状态 |
|---|---|---|---|---|
| 1 | `web-mcp` | 在固定网页系统上执行查询类操作，抽取结构化结果 | Playwright + 各站点登录态 | 已完成（P2） |
| 2 | `excel-mcp` | Excel 结构理解、查询、写入、格式保留 | 无（纯本地） | **已完成** |
| — | `mcp-common` | 配置、日志、工具包装 | 共享库，不单独启动 | 已完成 |

不再建钉钉、企微两个 MCP，原因见第 2 节。

**这套方案的成败不取决于代码量，而取决于一件事**：网页操作细节未知时，怎么做到「用起来再定、定了不用改代码」。答案是把所有操作写成 YAML 流程文件，代码只提供原子能力（第 4.3、4.6 节）。

---

## 2. 范围变更（v0.2 → v0.3）

| 项目 | 原计划 | 现状 | 处理 |
|---|---|---|---|
| `dingtalk-mcp` | 应用 API + 会话存档读消息 | 应用/存档权限均申请不到 | **移除** |
| `wecom-mcp` | 会话存档拉取解密 | 同上，且企微无网页版可兜底 | **移除** |
| 聊天记录获取 | 自动采集 | 改为**人工筛选** | 不建 MCP，由 `excel-mcp` 承接结构化落地（第 6 节） |
| `web-mcp` | 4 个模块之一 | 升级为**唯一新建模块**，也是核心 | 重点设计，见第 4 节 |
| `mcp-common` | 含消息模型 + SQLite 存储 | 消息采集取消后无引用方 | 见下方说明 |

**已清理的死代码**：聊天采集取消后，`src/mcp_common/models.py`（统一消息模型）、`store.py`（消息 SQLite 存储与同步游标）、`credentials.py`（API 凭据读取）都已无调用方，已删除；仅被 `credentials.py` 使用的 `keyring` 依赖同时移除。三个文件都在回收站，需要时可恢复。

---

## 3. 总体架构

```
┌────────────────────────────────────────────────┐
│ MCP Client（Trae / Claude Desktop / 自研 Agent） │
│ 职责：理解意图、选流程、LLM 信息抽取、编排调用     │
└──────────┬──────────────────────┬──────────────┘
           │ stdio                 │
┌──────────▼──────────┐  ┌────────▼─────────────┐
│      web-mcp        │  │      excel-mcp       │
│                     │  │                      │
│ ·原子操作（探索）    │  │ ·读结构 / 查询        │
│ ·YAML 流程（固化）  │  │ ·追加 / upsert        │
│ ·表格抽取 / 分页     │  │ ·模板填充 / 备份      │
│ ·只读护栏 / 人工接管 │  │ ·格式丢失预警          │
└──────────┬──────────┘  └────────┬─────────────┘
           └───────────┬──────────┘
              ┌────────▼─────────┐
              │    mcp-common    │
              │ 配置 / 日志 / 脱敏 │
              │ 工具错误包装       │
              └──────────────────┘
```

**分层原则**

- MCP 只负责「操作网页 + 取数 + 规整 + 落地」，**不做业务语义判断**。
- 「提取哪些信息、怎么归类」由客户端 LLM 完成。
- 好处：筛选规则天天变也不用改 MCP 代码、不用重启服务。

---

## 4. `web-mcp` 设计（核心）

### 4.1 三个设计前提

因为「具体操作后续使用时再确认」，设计必须满足三条：

1. **代码只给能力，不给具体流程**。新增一个查询场景 = 新增一个 YAML 文件，零代码改动。
2. **原子工具用于探索，流程用于固化**。第一次摸页面靠原子工具边试边看；摸清后写成 YAML，以后一键跑。
3. **默认只读**。你的需求主要是查询，所以所有会改变数据的动作（提交、审批、删除、发布）在流程未显式声明 `mode: mutate` 时一律拦截。

### 4.2 原子工具（探索用）

| 工具 | 关键参数 | 说明 |
|---|---|---|
| `web_open` | `url`, `profile?` | 打开页面，`profile` 复用该站点的登录态 |
| `web_snapshot` | `mode: outline/markdown/table/html`, `selector?`, `max_chars` | 看页面结构。`outline` 返回带 `ref` 的元素列表（省 token），`table` 直接把表格转成 JSON |
| `web_click` | `ref` 或 `selector` 或 `text` | 点击。命中危险词会被拦截，见 4.7 |
| `web_fill` | `ref`/`selector`, `value`, `press_enter?` | 填输入框，可顺便回车触发查询 |
| `web_select` | `ref`/`selector`, `value`/`label` | 下拉框 |
| `web_set_date` | `ref`/`selector`, `value` | 日期控件（很多系统是假的 input，需特殊处理） |
| `web_press` | `key` | 按键，如 Enter、Tab |
| `web_wait_for` | `selector?`, `text?`, `timeout_ms` | 等元素、等文本、等加载完成 |
| `web_extract` | `selector`, `fields: {name: selector}` | 按选择器抽字段，返回对象 |
| `web_extract_table` | `selector`, `header_row?`, `as: name` | 抽表格为对象数组（主流查询系统的核心工具） |
| `web_paginate` | `next_selector`, `max_pages`, `stop_when` | 自动翻页并累计结果，见 4.4 |
| `web_screenshot` | `path?`, `full_page?` | 截图存档，排查用 |
| `web_download` | `trigger_ref`, `dest?` | 触发导出按钮并把文件落到指定目录 |
| `web_save_state` | `profile`, `url` | 人工登录后保存登录态，只需做一次 |
| `web_list_profiles` | — | 看哪些站点已有登录态、最后保存时间 |

### 4.3 流程 DSL

流程文件放在 `flows/`，启动时自动加载。结构如下（这个例子是最常见的「查询页」形态）：

```yaml
# flows/erp_order_query.yaml
name: erp_order_query
description: 按日期区间查询订单列表
site: erp                       # 对应 data/profiles/erp.json
mode: readonly                  # readonly / mutate；默认 readonly
base_url: https://erp.internal.example.com

params:
  - name: start_date
    type: date
    required: true
  - name: end_date
    type: date
    required: true
  - name: status
    type: str
    default: "全部"

steps:
  - action: goto
    url: /order/list

  - action: fill
    selector: "#startDate"
    value: "{{ start_date }}"

  - action: fill
    selector: "#endDate"
    value: "{{ end_date }}"

  - action: select
    selector: "#status"
    label: "{{ status }}"

  - action: click
    text: "查询"

  - action: wait_for
    selector: "#orderTable tbody tr"
    timeout_ms: 10000

paginate:
  next_selector: ".pagination .next:not(.disabled)"
  max_pages: 20
  dedupe_by: ["订单号"]

output:
  table: "#orderTable"

on_error:
  screenshot: true
  save_html: true
```

**支持的 action**

| action | 必填 | 说明 |
|---|---|---|
| `goto` | `url` | 支持相对路径，自动拼 `base_url` |
| `fill` | `selector`, `value` | 输入框赋值，支持 `{{ param }}` |
| `click` | `ref` / `selector` / `text` | 三选一 |
| `select` | `selector`, `value` 或 `label` | 下拉框 |
| `press` | `key` | 按键 |
| `wait_for` | `selector` / `text` / `idle` | 等待条件 |
| `extract` | `selector`, `fields`, `as` | 抽取单个对象 |
| `extract_table` | `selector`, `as` | 抽表格为数组 |
| `download` | `trigger`, `dest` | 触发下载 |
| `sleep` | `ms` | 最后手段，优先用 `wait_for` |
| `assert` | `selector` / `text` | 校验，失败即中止并截图 |

**流程文件的三条约定**

- 变量只来自 `params`，用 `{{ name }}` 引用，不做表达式运算（避免 DSL 变成第二套编程语言）。
- `output` 必须产出纯 JSON（对象或数组），方便直接传给 `excel-mcp` 或给 LLM 读。
- 未知 action、引用了不存在的 param、`mode` 拼错，都在加载阶段报错并指明文件与行号，不会等到运行时才炸。

### 4.4 分页与去重（查询场景的高频需求）

`paginate` 是独立段落，可选：

- `next_selector`：下一页按钮；按钮变成 disabled 时自动停。
- `max_pages`：硬上限，默认 20，防死循环。
- `dedupe_by`：按指定列去重，避免「翻页过程中数据变动导致重复」。
- `stop_when`：如 `selector: ".empty"`，出现即停。

跑完返回 `{rows, page_count, truncated}`，`truncated` 为 true 时说明撞到 `max_pages`，需要调大或缩范围。

### 4.5 登录态与人工接管

- 每个站点一个 profile：`data/profiles/<site>.json`，存 Playwright 的 `storage_state`（cookie + localStorage）。
- 首次接入某站点：`web_open(url=..., profile="erp")` 以**有头模式**打开，你手动登录/过验证码，然后 `web_save_state(profile="erp")`，之后全自动。
- 默认有头（能看到浏览器窗口），这是刻意的：你随时能接管。批量跑定稿后才考虑 `headless=true`。
- 遇到验证码、短信验证、二次确认：工具返回 `{ok: false, code: "NEED_HUMAN", screenshot: "..."}` 并暂停，不会硬闯。

### 4.6 新增一个查询场景的五步（不用写代码）

1. `web_open` 打开目标页，`profile` 指定站点。
2. `web_snapshot(mode="outline")` 看页面有哪些元素、每个元素拿到 `ref`；需要看数据就 `mode="table"`。
3. 用 `web_fill` / `web_click` / `web_extract_table` 手工跑一遍完整查询，确认：
   - 每个控件的选择器稳定（优先 `#id`，其次输入框的 `name`，最后才 `nth-child`）；
   - 结果表格的选择器能稳定拿到数据行；
   - 有没有分页、多少页。
4. 把第 3 步的动作序列抄成 `flows/<site>_<场景>.yaml`。
5. `web_flow_validate` 校验选择器 → `web_flow_dry_run` 跑到「查询」前停住 → `web_flow_run` 完整跑通 → 定稿。

选择器不稳定的页面（纯动态生成的 class 名），改用 `text=` 或表头文字定位，把脆弱点集中在一处，改版时只改 YAML。

### 4.7 只读护栏（针对「只要查询」的需求）

这是本方案的安全底线，规则具体如下：

- 流程默认 `mode: readonly`。
- `readonly` 模式下，`web_click` 命中以下文本时**直接拦截**并返回 `BLOCKED_MUTATION`：提交、保存、删除、确定、审批通过、驳回、发布、支付、下单、退订、作废。
- 需要写操作时，流程必须显式写 `mode: mutate`，且首次运行强制 `dry_run`（走到最后一个写动作前停下，报告「将要点击哪个按钮」）。
- 拦截名单可在 `flows/_policy.yaml` 里调整。

### 4.8 返回值契约

所有工具统一返回：

```jsonc
{
  "ok": true,
  "data": [ ... ],              // 查询结果，数组或对象
  "meta": {
    "flow": "erp_order_query",  // 原子工具时为 null
    "site": "erp",
    "page_count": 3,
    "truncated": false,
    "elapsed_ms": 4210
  },
  "warnings": [ "..." ],        // 如「第 2 页解析为空」
  "artifacts": {                // 排查用文件
    "screenshot": "data/runs/20261112-0931-erp/shot.png"
  }
}
```

失败时 `{ok: false, code, error, artifacts}`，`code` 用固定枚举：`NEED_HUMAN` / `BLOCKED_MUTATION` / `SELECTOR_NOT_FOUND` / `TIMEOUT` / `FLOW_INVALID` / `BROWSER_ERROR`。这样 Agent 能区分「该叫我接管」和「该改 YAML」。

**每次运行都会落一份运行记录**到 `data/runs/<时间>-<flow>/`：结果 JSON、截图、HTML 快照。出问题不用复现，直接看现场。

---

## 5. `excel-mcp`（已完成）

10 个工具已实现并通过测试：

| 工具 | 用途 |
|---|---|
| `xlsx_describe` | 结构探查：sheet、表头行（自动识别）、列类型、合并单元格、公式 |
| `xlsx_read_range` | 读取区域，可按表头返回对象数组 |
| `xlsx_query` | `where` 等值 + `contains` 包含过滤 |
| `xlsx_append_rows` | 追加，按 `dedup_key` 去重 |
| `xlsx_upsert_rows` | 按主键更新/插入，返回单元格前后值 |
| `xlsx_write_cells` | 精确写单元格 |
| `xlsx_write_table` | 从起始单元格写整张表 |
| `xlsx_create_from_template` | 模板填充，格式全保留 |
| `xlsx_check` | 扫 `#REF!` 等错误值 + 格式丢失风险 |
| `xlsx_export` | 导出 CSV / JSONL |

统一约束：写操作默认 `dry_run=true` 先出变更预览；真实写入前自动备份到 `backups/`；检测到图片/图表/透视表/外部链接会返回 `warnings`。

**尚未实现**：原计划里的 `xlsx_format`（列宽、数字格式、条件格式）。当前不做，等实际报表需要时再按具体样式加，避免做一堆用不上的样式 API。

---

## 6. 人工筛选聊天记录怎么落地

聊天记录改人工后，MCP 不需要碰消息，`excel-mcp` 负责落地。推荐流程：

```
你在钉钉/企微里人工浏览 → 挑出有价值的几条 → 复制原文贴给 Agent
→ Agent 按你的规则抽字段 → xlsx_append_rows 写入 Excel
```

**推荐的 sheet 结构**（关键是「可追溯」列，方便回头核对原文）：

| 列名 | 说明 |
|---|---|
| 日期 | 消息日期，`YYYY-MM-DD` |
| 来源群 | 群名，人工填写 |
| 发言人 | 昵称/姓名 |
| 原文 | 消息原文，保留，不要只留摘要 |
| 分类 | 你的分类，如 风险/待办/客户诉求/决策 |
| 承接人 | 谁负责跟进 |
| 承诺时间 | 有承诺时填 |
| 状态 | 待处理/处理中/已关闭 |
| 录入时间 | 自动，用于排查重复录入 |

**去重**：`dedup_key=["来源群", "日期", "原文"]`。同一条消息重复贴不会产生重复行。

**如果以后想省事**：钉钉/企微客户端可以导出部分聊天记录为 txt/html，届时再加一个「批量解析导出文件 → 结构化 → 写入 Excel」的流程即可，仍然不需要新的 MCP，只是 `excel-mcp` 多一个导入工具。当前不做，因为不知道导出文件的实际格式。

---

## 7. 端到端典型场景

**场景 A：每日信息汇总**

```
① web-mcp 跑 erp_order_query，拿到订单列表（分页已处理）
② web-mcp 跑 crm_customer_query，拿到客户状态
③ Agent 侧把两部分 + 你人工贴的聊天要点合成一张表
④ excel-mcp xlsx_upsert_rows 写入《日报.xlsx》，主键 [日期, 订单号]
⑤ 返回变更摘要：新增 N 条、更新 M 条、跳过重复 K 条
```

**场景 B：按需查询**

你直接说「查一下上周华东区未发货的订单，导到表里」，Agent 选流程、传参数、取结果、写入。

---

## 8. 技术选型

| 维度 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.12 | 已装好，环境已就绪 |
| MCP 框架 | 官方 `mcp` 2.3（`MCPServer`） | 已跑通；注意 2.x 把 `FastMCP` 改名了 |
| 浏览器 | `playwright` + Chromium | 持久化登录态、下载、截图齐全 |
| 表格 | `openpyxl` + `pandas` | 已装好 |
| 配置 | `pydantic-settings` + `.env` | 已用 |
| 重试 | `tenacity` | 网页偶发超时时用 |
| 流程校验 | 启动时 pydantic 校验 YAML | 让流程错误在加载期暴露 |
| 测试 | `pytest` | 流程引擎用本地 HTML fixture 测，不依赖真站点 |

**新增依赖**：`playwright`（含 `playwright install chromium`）、`tenacity`、`pyyaml`。

---

## 9. 目录结构（相对当前已落地的结构）

```
D:\AIAssistant\
├─ pyproject.toml
├─ .env.example
├─ mcp-config.example.json
├─ mcp-integration-plan.md
├─ src/
│  ├─ mcp_common/       # 已实现
│  │  └─ config.py  logging.py  errors.py  utils.py  tools.py  server.py
│  ├─ mcp_excel/        # 已实现
│  │  ├─ guard.py  reader.py  writer.py  server.py
│  └─ mcp_web/          # 已实现（P2）
│     ├─ server.py      # 19 个工具（15 原子 + 4 流程）
│     ├─ browser.py     # Playwright 会话 / profile 管理
│     ├─ extract.py     # 快照三模式、表格/字段抽取
│     ├─ flow.py        # YAML 加载 / 校验 / 执行 / 翻页
│     └─ policy.py      # 只读护栏与危险词表
├─ flows/
│  ├─ _policy.yaml      # 拦截名单
│  └─ <site>_<scene>.yaml
├─ data/
│  ├─ profiles/         # 各站点登录态（勿提交）
│  ├─ runs/             # 每次运行的结果 + 截图 + HTML
│  └─ exports/
├─ backups/             # Excel 写入前备份
└─ tests/
   ├─ fixtures/         # 本地 HTML 样本 + 样例 xlsx
   └─ test_*.py
```

`mcp-config.example.json` 后续会加一段 `web` 的启动配置，与 `excel` 并列。

---

## 10. 分阶段实施

### P0 · 决策确认 —— 已完成
技术栈 Python、权限不可得、聊天改人工、MCP 只做网页查询 + Excel。

### P1 · 工程骨架 + `excel-mcp` —— 已完成
8 个测试通过，10 个工具注册正常，真实文件端到端冒烟通过。

### P2 · `web-mcp` 基础设施（不含具体站点）—— 已完成
- 19 个工具：15 个原子工具 + 4 个流程工具。
- 快照三种模式（outline / markdown / table）、表格抽取、翻页去重。
- 只读护栏 + 危险词拦截，词表在 `flows/_policy.yaml`。
- 浏览器 profile 持久化（`data/profiles/<site>.json`）。
- 流程 YAML 用 pydantic 校验，未知 action / 未知字段在加载期报错并指出字段名。
- 失败现场落盘（截图 + HTML），流程结果写 `data/runs/<时间>-<流程>/result.json`。
- **实测**：18 个测试通过；本地 HTML 上端到端流程（goto → fill → wait_for → extract_table）输出正确。
- **未验证**（需要真实站点才能测）：翻页超过 1 页、登录态跨进程复用、验证码触发 `NEED_HUMAN`。

### P3 · 流程引擎 + 第一个真实站点
- YAML 加载、pydantic 校验、变量替换、10 个 action。
- 4 个流程工具（list / validate / dry_run / run）。
- 选**一个**你高频、结构简单的查询页打通。
- **验收**：
  - `web_flow_validate` 能指出错误行号和原因。
  - 完整流程稳定输出 JSON，字段类型正确（数字不是字符串）。
  - 改版模拟：改掉一个选择器后，只改 YAML 即可恢复，代码零改动。
  - 流程跑失败时，`data/runs/` 里能看到截图和 HTML。

### P4 · 多站点铺开 + 日常化
- 按你实际使用情况，逐个补 `flows/*.yaml`（每个场景 30 分钟量级）。
- 与 Excel 串联跑通场景 A。
- 可选：定时任务（每日自动查询并写入表）。
- **验收**：
  - 至少 2 个站点、3 个查询场景可一键跑。
  - 同一份数据跑两次，Excel 无重复行、不覆盖人工填写列。
  - 某个站点登录态过期时，返回 `NEED_HUMAN` 而不是超时卡死。

---

## 11. 安全与合规

1. **账号使用**：只用你自己的账号登录，不共享、不借用他人账号；不绕过任何权限控制。
2. **只读优先**：默认 `readonly`；写操作需显式声明并先 dry-run。
3. **登录态保护**：`data/profiles/` 含 cookie，等同密码，必须 gitignore、不外传。
4. **日志脱敏**：手机号、邮箱、身份证默认脱敏（`mcp_common/logging.py` 已实现）。
5. **数据留存**：`data/runs/` 会累积截图和 HTML，建议加保留期（如 30 天）。
6. **抓取边界**：遵守目标系统的使用条款；不高频轮询，查询类操作加合理间隔。
7. **人工聊天记录**：只录入工作必需信息，原文列保留但注意表的分发范围。

---

## 12. 决策记录

| # | 决策 | 状态 | 说明 |
|---|---|---|---|
| 1 | 钉钉/企微 API | 已定 | 权限拿不到，移除两个 MCP |
| 2 | 聊天记录 | 已定 | 人工筛选，`excel-mcp` 承接 |
| 3 | MCP 范围 | 已定 | 仅网页查询 + Excel |
| 4 | 具体网页操作 | 待定 | 用起来再定；靠 YAML 流程，无需改代码 |
| 5 | 技术栈 | 已定 | Python 3.12 |
| 6 | 目标站点清单 | 待提供 | 第一个站点需要你给出 URL 和一个典型查询 |
| 7 | 是否允许网页写操作 | 待定 | 当前按「不允许」设计，护栏可调 |
| 8 | 是否需要定时自动跑 | 待定 | 影响 P4 是否加调度 |

---

## 13. 风险清单

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| 页面选择器不稳定、频繁改版 | 高 | 流程失效 | 选择器全在 YAML；失败留截图+HTML；优先 id/name |
| 登录态过期或触发风控 | 高 | 查询中断 | `NEED_HUMAN` 暂停接管，不重试硬闯 |
| 页面是前端动态渲染，等不到元素 | 中 | 超时 | `wait_for` 支持 selector/text/idle 三种条件 |
| 结果表格结构复杂（合并单元格、嵌套表头） | 中 | 抽取错列 | `header_row` 可指定；必要时 `web_snapshot(mode="html")` 人工定选择器 |
| 分页是无限滚动而非按钮 | 中 | 取不全 | 先用 `run` 探明；若为滚动，P3 加一个 `scroll` action |
| Agent 一次返回数据过大 | 中 | 上下文爆炸 | 工具侧限制 `max_rows`，超限返回 `truncated` |
| Excel 复杂格式被破坏 | 中 | 报表损坏 | 模板填充 + 写前备份 + 格式告警（已实现） |
| 流程 DSL 被写成第二套编程语言 | 低 | 维护成本高 | 只允许变量替换，不引入表达式/循环 |

---

## 14. 下一步

1. **你给我一个目标站点**：URL + 一个典型查询（查什么、按什么条件、要哪些字段）。这决定 P2/P3 的验收用例。
2. 装 Playwright 与 Chromium（`pip install -e ".[web]"` + `playwright install chromium`）。
3. 按 P2 实现 `web-mcp` 基础设施，用本地 HTML fixture 验收，不依赖你的内网系统。
4. P3 用真实站点跑通第一个流程，同时验证「改版只改 YAML」。
