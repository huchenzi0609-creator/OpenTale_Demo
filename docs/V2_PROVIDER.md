# v2 反身程序：模型协议与输入证据

本文对应 `mode=reflexive_v2`、`ReflectionFrame.language_version=2`。经典模式与反身 v1 继续使用原有输出协议。程序类型与作用域执行以 [模块合同](CONTRACT.md) 为准。

## 1 职责与执行边界

Provider 完成模型输入投影、JSON/schema 校验、本次整数接口到持久引用的机械编译，以及安全错误和用量返回。确定性内核负责读取、检索、方法执行、采用、权限检查、版本保存和批次安装。

v2 沿用 `propose / realize / present` 三个调用阶段。`propose` 与 `present` 可以在原有响应中附带方法、采用和局部回应；`realize` 仍只返回正文。方法程序内的 `read / query / inspect / revise / apply` 均为本地确定性操作。当前没有由方法发起的动态子模型请求，也没有任意 Python、Shell、文件系统或网络执行权限。

一次方法提案在通过校验并被采用后，才能改变后续真实输入。`query` 的结果不是同一次模型响应内的工具回合；程序执行后形成下一阶段可读取的材料与视图。它读取当前实验作用域内已有对象，不自动导入磁盘上的小说或其他文件。

## 2 实际输入与候选预览

| 数据 | propose / present | realize |
| --- | --- | --- |
| `materials` | 实际选中的每个材料出现位置；长请求通过文本池呈现全文或有来源的摘录 | 同样保留全部出现位置与来源关系 |
| `instruction` | 完整的执行组织说明 | 完整的执行组织说明 |
| `view` | 嵌套分组、关系和材料出现位置 | 同样保留结构 |
| `objects` | 可操作对象的预览与当次整数接口 | 不提供候选目录 |
| `executed_definitions` | 实际检查、执行过的完整方法定义 | 不提供方法定义目录 |
| `full_objects` | 不进入模型输入 | 不进入模型输入 |
| 技术来源、私有作用域标记、输入指纹 | 不进入模型输入 | 不进入模型输入 |

原始 `materials` 快照完整保存，独立于候选预览长度。长请求的文本池优先共享相同正文；需要摘录时，`spans / segments / omitted_chars` 标明确切出处与省略范围，不能把摘录理解成已经读过全文。`objects` 中 `preview_truncated=true` 表示文字或其关联材料尚未完整展示；上游已经发生的截断始终保留。`full_length` 给出原文字数。对象可通过 `read` 读取全文或范围，通过 `query` 在权限允许的完整对象集合中寻找较早材料。具体算法见 [上下文压缩说明](CONTEXT_COMPACTION.md)。

紧凑格式中，`materials[].text_ref` 指向顶层 `text_pool[].text_index`。重复元信息有足够节省时，`materials[].metadata_ref` 指向顶层 `material_metadata[].metadata_index`，还原该次出现的 id、role、position_id、expression_id；材料出现次数和视图索引保持原值。文本池的来源路径对应冻结 frame 的材料或本条阅读历史，源哈希与字符范围可离线验证。

`view.groups` 保留树状子组，各组使用本次 `group_index`；关系使用 `source_group_index / target_group_index`。材料通过 `material_indices` 指向本次 `materials` 的出现位置。同一对象被读取两次时，这两个出现位置分别保留。候选视图只展示部分材料时，缺失的出现位置标记 `unavailable_material_count`，不会假装引用了其他出现位置。

`executed_definitions` 标明 `operation=inspect|apply`，保留完整 instruction 与程序。其对象引用继续转换为本次整数；没有可用整数的临时执行定义标明 `transient_definition=true`。模型可以理解其定义并提出新的程序，但不能把临时内部 ID 当作新权限。

初始私有资料可以进入拥有该权限的位置。`private_scope` 是服务端检查元数据，正文投影不会暴露这个字段；能否把资料经方法、视图或回应交给另一位置，仍由内核检查。

## 3 方法程序

新方法使用 `language_version:2`；v2 请求也接受旧式 `object_indices / order / limit` 方法。v1 请求不会接受新程序或回应字段。

```json
{
  "reflection": {
    "methods": [{
      "title": "让先前的回应参与继续",
      "language_version": 2,
      "instruction": "沿经历与回应之间的具体关系继续生活。",
      "revises_index": 1,
      "program": {
        "steps": [
          {"id": "prior", "op": "inspect", "ref": {"object_index": 1}},
          {
            "id": "changed",
            "op": "revise",
            "input": "prior",
            "patch": {
              "program": {
                "steps": [
                  {"id": "experience", "op": "read", "refs": [{"object_index": 0}]},
                  {"id": "response", "op": "read", "refs": [{"object_index": 3}]},
                  {
                    "id": "linked",
                    "op": "relate",
                    "inputs": ["experience", "response"],
                    "relations": [{"source": "experience", "target": "response", "label": "经验取得新的去处"}]
                  }
                ],
                "output": "linked"
              }
            }
          },
          {"id": "used", "op": "apply", "method": "changed"}
        ],
        "output": "used"
      }
    }],
    "adoptions": [{"scope_index": 0, "draft_index": 0}],
    "responses": [{
      "text": "归家的动作使先前的等待有了去处。",
      "about_indices": [1],
      "evidence_indices": [0],
      "visibility": "shared"
    }]
  }
}
```

这是响应中的 reflection 部分。正文仍放在各阶段既有的 `fragments` 或 `text` 中。示例假定本次对象 0 为内容、1 为方法、3 为可见回应，且作用域 0 可采用该方法；具体整数由当次请求决定。

| 操作 | 输入与作用 | 返回值 |
| --- | --- | --- |
| `read` | `refs` 指向可见对象，可指定文本范围 | 视图 |
| `query` | 检索词 `query`，可选 `kinds / related_to / limit` | 命中对象形成的视图 |
| `group` | `inputs` 中的已有视图，附自然语言 `label` | 保留内部层次的新视图 |
| `relate` | 组合已有视图；relations 连接本步骤 inputs 中的步骤名 | 带关系的新视图 |
| `inspect` | 指向已有 method 的对象引用 | 方法定义 |
| `revise` | 一个已检查或已改版的方法，加 `patch` | 新方法定义 |
| `apply` | 一个已检查或已改版的方法 | 该定义实际执行的视图 |

步骤 `id` 只在自身程序内有效，必须唯一；步骤只能使用前面已经定义且类型相容的结果。`program.output` 必须指向一个视图。查询 kinds 可选 `content / method / choice / view / response / effect`，空列表表示不按 kind 限定。

方法的 `revises_index` 表示它改版自哪个已有方法。采用新草稿时，替换规则保持局部作用域。程序内部的 `revise` 则产生可被 `apply` 执行的派生定义；它没有全局采用权限。检查定义、修改定义、采用方法和执行方法是不同操作。

`patch` 只改写实际给出的字段。省略字段表示保留；`limit:null` 表示取消该旧式材料截取。title、instruction、order、object_indices 和 program 在给出时须为相应类型，不能用 null 代替。这个区别在存档与恢复后保持。

## 4 引用、版本与局部回应

对象引用格式为：

```json
{"object_index": 0, "mode": "version", "start": 0, "end": 120}
```

- `mode=live` 为默认值，由执行器解析当前可见对象。
- `mode=version` 固定本次接口行的精确版本；`version` 可以省略，由编译器填入。显式版本必须与本次接口行一致。
- `start / end` 是文本字符范围，采用起点包含、终点不包含的切片语义。结构化视图通过完整读取保留内部关系。
- 原始持久 ID、错误类型、越界整数、虚构版本、未来步骤引用都会失败，错误只返回固定类别与可用量统计。
- 缺失候选引用会显示 `unavailable_*_count`，没有自动猜测、替换目标或静默删除无效操作。

局部回应使用 `about_indices` 指明理解对象，`evidence_indices` 指明依据。`visibility=scope` 为默认值；`shared` 仍受引用材料的隐私权限限制。回应属于生成出的局部陈述，不要求模型提供内部推理过程，也不成为统一文学评分。

`effect` 是可检索、可读取、可被回应引用的实际组织后果对象。候选投影提供 `changed`、阶段、前后选择的 `previous_choice_index / choice_index`、采用方法的 `method_indices`、新增与移除材料的 `selected_added_indices / selected_removed_indices`，以及已有回应和依据的整数引用。此前移除的材料若不在本次接口表中，保留 `unavailable_*_count`；前后选择对象仍保存其冻结材料。投影不暴露内部 ID、作用域来源或输入哈希。

内核同时检查前后选择及相关依赖的权限，隐藏任一侧不可见的后果记录。后果的文本只概括实际组织变化；对这种变化的意义，生成者可以继续形成不同局部理解。

普通人物承接的 v2 target 可以写 `action:rebind|withdraw`，默认 rebind。withdraw 只撤回本次提交者在指定目标、表达与 role 上的贡献。target 的 `interface_index` 仍只从本次 routing_table 选择。未运行来源的贡献由内核保留。旧模式没有这个新 action 字段。

## 5 精确请求投影与修复

`DeepSeekProvider.request_projection(frame, method, repair=None)` 是同步、无网络的方法，返回与实际 HTTP 请求体完全一致的 JSON 对象，包括：

- 系统纪律、阶段提示、输出 schema 与形状示例。
- 实际模型可见的 user 数据投影。
- model、max_tokens、response_format、thinking、stream 等生成参数。
- 当次修复的有限反馈（如有）。

认证 Header、API key、服务端点、客户端实例、超时配置和隐藏推理不在这个投影中。该方法与真实调用共享准备和序列化代码，避免维护两套看似相同的提示。Runner 可以在网络调用之前记录投影及哈希，并为每次修复尝试保留独立证据。

此前的压缩请求采用 `provider-json-v2-context` 投影版本，长请求的 user JSON 包含 `context_compaction` 清单。`with_projection_version("provider-json-v1")` 返回共享客户端的独立旧适配器，恢复已有请求时维持原 JSON 字节表达。经典模式与反身 v1 沿用原投影。格式修复只添加系统反馈，文本池和 user JSON 保持一致；清单中的 `output_chars` 是基础请求长度，修复尝试的实际长度另行记录。

当前新建请求已进一步使用 `provider-json-v3-bounded`，算法为 `source-pages-v1`。上面的 v1 与 v2-context 两个适配器仍保留原字节语义。v3 先把候选人物重复的 `(expression_id, role)` 目录项无损合并，用 `occurrence_count` 保留次数；正文和材料出现位置保持独立。整个请求在池化后能放入物理容量时，优先保留全部选中正文。

仍然过大时，v3 从有限核心逐块装入有来源的页面，每次同时测量 user、当前 schema 和系统提示，并预留格式修复空间。目录、历史、材料与结构都可分页；省略集合只保留一个描述，不为每个省略项制造无限的新元信息。`context_capabilities` 精确列出本页可用的对象、作用域、路由、焦点、表达及历史原索引；输出 schema 和返回编译共同检查这个表，历史原始 ID 也不能绕过页面权限。

材料页使用 `material_index`、历史页使用 `history_index` 保持原出现位置，不重新编号。完整视图若包含本页未展示的材料索引，须依据材料页的省略标记理解；不能把页面当作已经读过整个选择。当前组织 instruction 优先保留并标注原文范围；活跃方法先于普通目录入页，已执行定义也有独立优先预算。`structure_excerpted`、可见数量及 `pages` 共同标记结构省略，正文统计包含没有出现在本页的原始文本。

`verify_bounded_sources(frame, projection)` 从冻结 frame 确定性重建对应版本的 user 投影，核对文本、页承诺、索引映射与组织说明；它不访问网络或密钥配置文件。v2-context 的来源验证器继续用于旧请求。这里的分页缩小的是一次模型调用的可见页面，原始实验、完整 frame、对象与历史保持原值。

`OPENTALE_CONTEXT_TARGET_CHARS` 默认 96000，是单次请求的软目标，独立于实验总 Token 预算且没有配置值上限。强制接口与每项来源的基本覆盖已超过软目标时，系统优先保留仍能放入物理容量的完整正文，并标记 `within_target=false`。它不会为追求软目标而清空材料。原有单次传输边界仍然生效。

Provider 对 frame 做深复制后编译整数引用，异步返回阶段仍使用原请求的对象表。结构失败可按现有 Runner 策略重试；校验失败的已计费用量会随安全错误返回。技术引用、凭据回显和格式错误不会通过“改写成普通正文”的方式掩盖。

预算的 nullable/unlimited 语义保持不变。本协议没有新增实验总调用、总 Token、程序步骤数量或方法层数额度。现有服务商上下文/传输边界、字符格式约束和超时仍然适用，超过边界时返回明确错误。

## 6 离线夹具与验证范围

FixtureProvider 的 v2 路径执行一个明确脚本：选择公共材料，提出关系程序；有旧方法时 inspect → revise → apply；有既存共享回应时，把回应作为新材料真实放入改版后的视图。若已有该方法的实际后果对象，夹具的局部回应会引用其记录作为依据。阅读位置可以把其方法返回人物位置。夹具的正文也读取关系标签，因此关系变化不会完全消失在输出中。

`FixtureProvider.request_projection` 标明 `provider=fixture / fixture_version=2`，保存安全的调用输入投影。夹具没有 HTTP 请求，这不是远端模型请求证据。

覆盖测试包括完整材料保留、上游截断传播、递归程序补丁、版本与整数权限、重复材料出现位置、真实发送请求体等于投影、局部回应引用、v2 撤回、v1 兼容和秘密隔离。MockTransport 测试不访问网络。离线夹具证明引用、改版、结构输入与反馈机制可以运行；它不证明真实模型已经形成叙事意图、提高文学质量或产生自发高阶关注。

后续可以扩展程序原语和受管理的生成子作业。当前应先根据这些精确输入、视图与局部回应检查实际模型行为，再决定是否增加新的调用机制。
