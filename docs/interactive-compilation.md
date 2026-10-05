# ARC 编译与交互命令

ARC 统一使用 `arc compile`。首次编译不需要 `--resume`；所有交互操作都在已生成的工作区上执行，因此必须使用 `--resume`。

## 完整编译与恢复

```powershell
arc compile requirements.yaml -o workspace/demo --type web
arc compile requirements.yaml -o workspace/demo --resume
```

`--clean` 会重建输出目录，不能与 `--resume` 同用。

逐节点 DESIGN/TDD 之前，ARC 自动执行全局 `DATABASE_PREPARE`：先通过普通 LLM JSON 问答确定全局实体命名，再分析根需求及各一级需求子树。每批只返回发生变化的完整表定义和新增预置行，系统校验并合并；草案冲突必须显式修正，已落地记录仍仅允许追加。批间允许暂未解析的外键，最终统一核对所有引用和预置数据，再确定性生成代码并初始化真实数据库。不使用智能体工具，不生成 schema 文档。

实体目录、已接受批次的增量记录、累计草案和最终核对结果保存在 `.arc/database/analysis.json`；最终建库记录和阶段状态保存在 `.arc/database/state.json`。数据库准备失败时不会执行节点；修正原因后使用 `--resume` 恢复。同一需求版本和数据库基线下跳过成功批次，版本或基线改变则重新分析。数据库初始化可重复执行而不覆盖已有业务数据。Web 运行库和 E2E 隔离库使用同一套生成代码。

节点 DESIGN 直接完善现有页面/组件、挂载新增 UI、实现前端请求函数及事件/状态逻辑，再生成并注册该节点独立的后端 API → FUNC → DB 操作函数调用骨架。接口清单只记录后端契约，DB 指对共享数据库的操作函数；UI 和前端请求函数不建模为接口或节点。

所有模型调用只输出工具调用数组，每项包含 `tool` 和必要参数，不输出包装对象、解释、summary、状态或空字段。DESIGN 仅提供 add_file/edit_file/delete_file/read_file。add_file 携带 layer（frontend/API/FUNC/DB/shared），创建时自动加入文件追踪；edit_file 保留已有归属，新接入且无法按路径分类的文件需携带 layer；删除时移除该节点的文件记录，不需要额外登记调用。TestGenerator 用文件操作及 `register_test(test_id,type,file_path)` 登记测试。系统校验实际文件并生成稳定追踪记录，需求归属由系统填写。节点会话仍保存 `file_groups` 和 `materialized_files`，供测试生成、TDD 按路径读取源码。父节点或纯前端需求不登记后端分组。TDD 完成后端业务并修复前端接通；骨架阶段不得伪造成功结果。

TDD 从 DESIGN 的文件分组确定 `implementation_scope`，直接实现已登记的 API/FUNC/DB 文件。后端只允许修改这些文件和明确登记的 shared 接入文件，禁止全库搜索或新建替代后端模块；当前节点测试文件可修复。前端仍允许在前端范围内定位页面/组件/请求逻辑。其他依赖可以按精确路径读取。旧工作区从 traceability 恢复后端位置；骨架文件缺失时应重跑 DESIGN。

## 重试节点

节点设计、测试生成、TDD 及共享能力处理使用简易 ReAct 文件循环。初始仅提供当前登记目标、失败涉及的文件及少量入口，不预加载整个前端或递归依赖。模型返回 `[{"tool":"read_file","path":"单个具体路径"}]`，可同时请求多个文件；下一步收到完整内容或路径错误。后续修复重新读取最新快照。有足够信息后返回最终的 `add_file(path,content)`、`edit_file(path,old_text,new_text)`、`delete_file(path)` 及该阶段必要的登记调用。读取不能与写入或登记混在同一批次，最终写入统一校验并落盘，失败则回滚，包括删除操作。

共享读取使用 `read_shared(name)`、`read_shared_group(id)`；缺失能力使用单独的 `request_shared(name,need)`，共享发现使用 `register_shared(name,files,reuse_files,contract)` 或单独的 `report_database_gap(need)`。数据库实体分析使用 `define_entity`；表和预置数据使用 `define_table`、`seed_rows`，草案冲突时使用 `replace_table(name)`、`replace_seed_rows(name)`。这些调用只产生经校验的内部记录，数据库仍由系统确定性生成代码。无操作返回 `[]`。

每次 ReAct 循环默认最多 12 步，每步最多读取 10 项（包括契约/索引组）；可通过 `ARC_AGENT_MAX_STEPS` 设置 2～50 步。最后一步必须结束读取并给出最终批次。JSON/清单修复以及系统构建/测试反馈仍使用原有外层有限修复轮次；没有恢复 Deep Agent、shell 或自由搜索。读取保持单文件 60 KB、总源码 300 KB 限制，不授予写权限。删除只允许阶段内可移除文件：DESIGN 可移除旧前端文件并从最终分组排除；TDD 不删除登记目标；TestGenerator 不删除登记测试、资产或运行器配置；共享核心/数据库文件保持受保护。模型返回对象、旧协议字段、额外参数或不可用工具会被明确拒绝，进入原有有限修复流程。

```powershell
arc compile requirements.yaml -o workspace/demo --resume --retry REQ-2.1
arc compile requirements.yaml -o workspace/demo --resume --retry-failed
```

DESIGN 失败会重跑该节点的 DESIGN 与 IMPLEMENT；仅 IMPLEMENT 失败时保留已有接口与测试清单。

## 选中测试重新执行 TDD

```powershell
arc compile requirements.yaml -o workspace/demo --resume `
  --rerun-tdd REQ-2.1 --test TEST-2.1-search --test TEST-2.1-empty
```

测试必须归属于该节点。ARC 只执行所选测试，并按 `Unit -> Integration -> E2E` 分层运行；不会改写节点最后一次完整编译的状态。

## 追加测试

```powershell
arc compile requirements.yaml -o workspace/demo --resume `
  --add-tests REQ-2.1 --intent "日期非法时禁止提交"
```

`--intent` 是本次 TestGenerator 调用的测试目标。该命令只生成并登记新测试，不运行 TDD，也不重新设计接口。

## 按 test ID 修改测试

```powershell
arc compile requirements.yaml -o workspace/demo --resume `
  --regenerate-tests REQ-2.1 --test TEST-2.1-invalid-date `
  --intent "日期非法时禁止提交"
```

该命令修改指定的已登记 test ID。`--intent` 只提供本次修改的测试目标，不写入 Traceability，也不作为替换匹配键。生成结果必须只返回该 test ID，并保持其原测试文件路径；同一文件内的其他测试必须保留。

## 新增或修改需求后的增量编译

先编辑原始需求文件，再运行：

```powershell
arc compile requirements.yaml -o workspace/demo --resume --sync-requirements
```

ARC 会追加新增/变更节点、其祖先及显式反向依赖节点的标准 DESIGN 和 IMPLEMENT 任务。删除节点、移动节点、更改父节点或更改节点 ID 暂不支持增量处理，应使用完整编译。

同步需求时会重新分析全局数据库记录，并将变化的共享表消费者及其反向依赖、祖先纳入重编译。数据库只允许保留数据的追加式演进；删除/重命名表字段、修改已有键或预置行等不兼容变化会阻止编译。节点智能体复用全局 DB 契约，不自行修改生成的建库与预置数据代码。

## 规则

模型输入去重：视觉分析只放在顶层 `requirement.visual_reference`，不再次注入 context；文件位置、归属、读取状态及明确写入权限合并为 `file_inventory`，源码正文仍单独完整提供，系统内部继续使用原始范围记录校验落盘。共享/数据库所有权、按需读取、真实行为及输出规则集中在公共 system policy。测试示例按应用类型和当前测试层注入：Web E2E 使用 Playwright 示例，Unit/Integration 使用 Vitest 示例；首次生成尚未选择测试层时仅提供简短约定，后续修复依据候选或登记测试层提供示例，CLI/Android 不注入 Web 示例。

- 每次调用只能使用一种交互动作。
- `--test` 仅用于 `--rerun-tdd` 或 `--regenerate-tests`；后者要求恰好一个 test ID。
- `--intent` 仅用于 `--add-tests` 或 `--regenerate-tests`。
- 不要手动编辑 `.arc` 下的队列、Traceability 或节点会话文件。
