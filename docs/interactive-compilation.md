# ARC 编译与交互命令

ARC 统一使用 `arc compile`。首次编译不需要 `--resume`；所有交互操作都在已生成的工作区上执行，因此必须使用 `--resume`。

## 完整编译与恢复

```powershell
arc compile requirements.yaml -o workspace/demo --type web
arc compile requirements.yaml -o workspace/demo --resume
```

`--clean` 会重建输出目录，不能与 `--resume` 同用。

逐节点 DESIGN/TDD 之前，ARC 自动执行全局 `DATABASE_PREPARE`：分析完整需求树，返回表、字段、约束、索引、预置行及需求来源的 JSON 中间记录，系统确定性生成数据库代码并初始化数据库，不生成 schema 文档。

中间记录和阶段状态保存在 `.arc/database/state.json`。数据库准备失败时不会执行节点；修正原因后使用 `--resume` 恢复。已通过校验的分析记录会被复用，数据库初始化可重复执行而不覆盖已有业务数据。Web 运行库和 E2E 隔离库使用同一套生成代码。

节点 DESIGN 直接完善现有页面/组件、挂载新增 UI、实现前端请求函数及事件/状态逻辑，再生成并注册该节点独立的后端 API → FUNC → DB 操作函数调用骨架。接口清单只记录后端契约，DB 指对共享数据库的操作函数；UI 和前端请求函数不建模为接口或节点。

前端代码位置通过 `files_written` 保存为节点会话的 `materialized_files`，并提供给后续测试生成、TDD 和相关子节点。父节点有视觉参考时直接完善布局并返回空接口清单。纯前端叶节点也可返回空接口清单，仍按场景生成测试。TDD 完成后端业务并修复前端接通；骨架阶段不得伪造成功结果。

## 重试节点

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

- 每次调用只能使用一种交互动作。
- `--test` 仅用于 `--rerun-tdd` 或 `--regenerate-tests`；后者要求恰好一个 test ID。
- `--intent` 仅用于 `--add-tests` 或 `--regenerate-tests`。
- 不要手动编辑 `.arc` 下的队列、Traceability 或节点会话文件。
