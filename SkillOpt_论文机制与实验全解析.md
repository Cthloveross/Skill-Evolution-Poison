# SkillOpt：机制、优化模型与实验全解析

SkillOpt 的核心不是微调模型，也不是让执行任务的模型在线改写自己，而是：冻结任务模型和执行环境，把一份 Markdown `skill` 当作可训练的外部状态；另一个 LLM 角色阅读带分数的执行轨迹，提出结构化文本补丁；程序把补丁组成候选 skill，再用独立 selection split 重跑任务，只有候选平均分严格上升才接受。[论文 v2](https://arxiv.org/pdf/2605.23904v2)和[官方项目页](https://microsoft.github.io/SkillOpt/)都将其概括为 rollout、reflect、bounded edit、validation gate 和 export。

## 1. 四个角色

| 组件 | 实际作用 | 是否更新权重 |
|---|---|---:|
| Target / training model | 带着当前 skill 执行任务，产生消息、工具调用、答案与分数 | 否 |
| Optimizer model | 读取当前 skill 和 scored trajectories，生成、合并、排序文本 edits | 否 |
| Skill document | 需要被训练的 Markdown 操作手册，最终导出为 `best_skill.md` | 文本被修改 |
| Harness + evaluator | 执行任务并给出 hard/exact-match 或 benchmark-native score | 否 |

因此，“optimizer model”在架构上是另一个 LLM 调用角色，但不必是另一个模型 checkpoint。论文默认强 optimizer 是 GPT-5.5：当 target 也是 GPT-5.5 时，两者可以是同一模型、不同调用；当 target 是 GPT-5.4、GPT-5.4-mini、GPT-5.4-nano 或 Qwen 时，optimizer 仍可使用 GPT-5.5。官方默认配置也把 optimizer 和 target 分开配置。[默认配置](https://github.com/microsoft/SkillOpt/blob/v0.1.0/configs/_base_/default.yaml#L4-L10)

Optimizer 本身没有再训练、没有梯度下降，也没有 RL 更新。它的“学习”来自上下文中的两类文本状态：同一 epoch 的 rejected-step buffer，以及跨 epoch 的 optimizer-side meta skill。

## 2. 完整优化循环

设冻结模型为 \(M\)，任务为 \(x\)，执行环境为 \(h\)，当前 skill 为 \(s\)。一次执行产生轨迹与分数：

\[
(\tau(s),r(s))=h(M,x,s),\qquad r(s)\in[0,1].
\]

数据被分成三个互不重叠的用途：

- \(D_{train}\)：只用来生成 rollout evidence，让 optimizer 提议 edits；
- \(D_{sel}\)：只用来接受或拒绝候选 skill；
- \(D_{test}\)：训练结束以后，才评估最终 `best_skill.md`。

一次 step 的实际顺序如下。

1. Target model 带当前 skill 在一批 train tasks 上运行。系统记录任务描述、对话、工具调用、观察、命令输出、最终答案、verifier feedback，以及 spreadsheet preview、文档引用或精简 agent trace。
2. 按 hard score 把轨迹分为 failures 和 successes，再分别切成 reflection minibatches。默认 minibatch size 是 8。
3. 同一个 optimizer LLM 用 failure-analyst prompt 找跨多个失败重复出现的缺失规则，用 success-analyst prompt找跨多个成功共享且值得保留的行为。
4. 每个 minibatch 返回 JSON edits。多个 failure patches 先层次化合并，多个 success patches 也先合并，最后 failure-first 合并，去重、解决冲突并过滤样例专属规则。合并结果中的 `support_count` 是 LLM 根据源 patches 估计的支持度，不是程序对独立样本做出的严格统计计数。
5. Optimizer 再按系统性影响、互补性、通用性和可执行性排序，只保留当前 edit budget \(L_t\) 以内的 edits。
6. 程序按顺序把这些 edits 应用到同一份 current skill，形成一个完整 candidate skill。
7. 用同一个冻结 target model、同一个 harness，在整个 selection split 上重跑候选，计算 aggregate selection score。
8. 仅当候选分数严格大于 current score 时接受；相同分数也拒绝。如果又高于历史 best score，就更新 `best_skill.md`。
9. 被拒候选的 edits、分数下降和当前失败模式进入 epoch-local step buffer，供后续 optimizer calls 参考。
10. 训练结束，只用 best skill 在 test split 上做最终报告。

Selection gate 可写成：

\[
U_{sel}(s)=\frac{1}{|D_{sel}|}\sum_{x\in D_{sel}}r(h(M,x,s)),
\]

\[
\text{Accept}(s')\iff U_{sel}(s')>U_{sel}(s_{current}).
\]

这不是“修改前先证明它一定有效”。真实顺序是先由 LLM 根据训练轨迹猜一个可能有用的方向，机械生成候选，再用 selection rollout 做反事实试验。通过才提交；失败就回滚到旧 skill，并把失败当作自然语言负反馈。官方 Algorithm 1 明确采用这一 propose–apply–evaluate–accept/reject 顺序。[论文 Algorithm 1](https://arxiv.org/pdf/2605.23904v2#page=21)

## 3. Optimizer 看见什么

Failure analyst 的输入至少包含：

- 当前完整 skill；
- 本次 edit budget；
- 前面 step 的失败模式和 rejected edits；
- optimizer-side meta skill；
- 多条失败轨迹，包括 task、failure reason、对话/工具轨迹、target system/user prompt 和可用的 verifier 信息。

Prompt 明确要求它找“多个轨迹共同的系统性失败”，禁止硬编码具体任务值，也禁止重复 skill 已有规则。Success analyst 的结构类似，但目标是找成功轨迹中值得保留、尚未写入 skill 的通用模式。[轨迹格式化和 analyst 调用](https://github.com/microsoft/SkillOpt/blob/v0.1.0/skillopt/gradient/reflect.py#L56-L222)

随后还有同一 optimizer 模型的其他调用角色：failure merger、success merger、final merger、edit ranker、slow-update advisor 和 meta-skill coach。它们不是六个训练过的专用模型，而是同一个或同类通用 LLM 配上不同 system prompts。

## 4. 它不靠行号修改

论文默认 patch mode 允许四种原子操作：

```json
{
  "edits": [
    {"op": "append", "content": "<要追加的 Markdown>"},
    {"op": "insert_after", "target": "<skill 中的精确文本锚点>", "content": "<新文本>"},
    {"op": "replace", "target": "<要替换的精确旧文本>", "content": "<新文本>"},
    {"op": "delete", "target": "<要删除的精确文本>"}
  ]
}
```

Optimizer 看过当前完整 skill，因此由 LLM 复制一段现有标题或句子作为 `target`。实现随后使用普通字符串查找和首次替换，而不是行号、梯度、AST 或 embedding 定位：

- `replace` 和 `delete` 只处理第一次匹配；目标不存在就跳过；
- `insert_after` 找到锚点后，在锚点所在行后插入；当前 v0.1.0 实现若锚点找不到，会退化为 append；
- `append` 加在主 skill 尾部；如果存在 protected slow-update region，则插在该区域之前；
- 多条 edits 按顺序应用，所以后一条看到的是前一条修改后的文本；
- 每条的 applied/skipped/error 状态写入 `edit_apply_report.json`；这个报告证明补丁是否机械执行成功，不证明它对任务分数有贡献。

对应实现见[patch application](https://github.com/microsoft/SkillOpt/blob/v0.1.0/skillopt/optimizer/skill.py#L39-L164)。

一个具体例子是：如果 optimizer 在多条 SpreadsheetBench 失败轨迹中发现 agent 写完公式却没有可供 grader 读取的计算值，它可能输出：

```json
{
  "op": "insert_after",
  "target": "## Verification",
  "content": "- Re-open the saved workbook and verify every requested target cell contains an evaluated static value."
}
```

这里不是系统算出“第 17 行梯度最大”，而是 LLM 在语义上判断新规则应属于 `## Verification`，再用这个精确标题作文本锚点。

## 5. Textual learning rate

\(L_t\) 不是数值参数更新步长，而是一次最多提交多少条 edits。默认初始 budget 为 4，cosine schedule 最低衰减到 2。它的目标是限制相邻 skill 的文本距离，避免一次 full rewrite 删除已经验证有效的规则。

合并后如果 edit pool 超过 budget，optimizer LLM 只返回 `selected_indices`；程序检查索引、去重并保留前 \(L_t\) 条，失败时退化为顺序截断。[rank and clip](https://github.com/microsoft/SkillOpt/blob/v0.1.0/skillopt/optimizer/clip.py#L25-L109)

论文也支持 `rewrite_from_suggestions`，即让选中的建议条件化一次全文重写，但默认实验采用 patch mode。论文把“without lr”作为无有界 edit budget 的对照，而不是默认工作方式。

## 6. Rejected buffer、slow update 与 meta skill

Rejected buffer 是 intra-epoch 负反馈：候选没有过 selection gate 时，系统保存 rejected edits、score before/after 和仍存在的失败模式。后续 analyst prompt 会看到这些内容。它不会更新 optimizer 权重，只是改变下一次 LLM 的上下文。

Slow update 是 target-facing 的跨 epoch 指导。第 2 个 epoch 起，系统抽取同一批训练任务，分别用上一个 epoch skill 和当前 skill 重跑，并分成 improvement、regression、persistent failure、stable success。Optimizer据此写一段长期指导，放在：

```markdown
<!-- SLOW_UPDATE_START -->
...
<!-- SLOW_UPDATE_END -->
```

普通 step edits 无权修改该区域。论文协议要求 slow-update candidate 也过同一个 selection gate。

Meta skill 是 optimizer-facing 的教练笔记，记录什么类型的 edit 有效、什么太模糊/重复/脆弱、哪些回退风险要防。它被加到未来 analyst、merge、ranking prompts 中，但不进入部署给 target 的 `best_skill.md`。

因此二者不同：slow update 修改运动员能看到的手册；meta skill 修改教练以后如何写手册。

复现时要注意：官方 v0.1.0 README 说明，论文协议与发布的 paper-aligned checkpoints 使用 gated slow update，但后续仓库默认值是 force-accept。也就是说，直接按当前默认配置运行会无条件把 slow guidance 写入 current/best skill；要匹配论文，必须把 `optimizer.slow_update_gate_with_selection` 设为 `true`。[官方复现说明](https://github.com/microsoft/SkillOpt/blob/v0.1.0/README.md#L283-L313)

## 7. 验证门实际验证什么

Gate 比较的是整个 candidate skill 的一个 aggregate scalar，而不是逐条 edit 的边际作用。默认情况下，多条 top-\(L_t\) edits 被合成一个 candidate，只重跑一次 selection set。只要整体分数上升，整包 edits 一起进入 current skill。

因此需要区分三件事：

1. `edit_apply_report.json`：某条 patch 是否成功落到文本里；
2. selection gate：整份 candidate 在测量的任务效用上是否优于 current；
3. test score：被 selection 选中的 best skill 是否泛化到未用于选择的数据。

它没有做 per-edit leave-one-out、Shapley attribution 或“删除这一条以后分数会怎样”的测试。论文中的“accepted edit”更准确地说是“包含这些 edits 的 candidate bundle 被接受”。[trainer 的 bundle application 与统一评估](https://github.com/microsoft/SkillOpt/blob/v0.1.0/skillopt/engine/trainer.py#L1122-L1183)以及[严格 gate](https://github.com/microsoft/SkillOpt/blob/v0.1.0/skillopt/evaluation/gate.py#L76-L148)体现了这一点。

Gate 也不是安全证明。它只保证候选在 selection tasks 和选定 metric 上提高，不保证每条 instruction 必要、来源可信、没有罕见 trigger，也不保证分布外行为安全。

## 8. 实验设计

### 8.1 主实验矩阵

六个主 benchmark 覆盖不同交互形态：

| Benchmark | 能力/执行形态 | 主指标 |
|---|---|---|
| SearchQA | 单轮抽取式 QA | exact/hard accuracy |
| SpreadsheetBench | 真实 `openpyxl`/`pandas` 运行时，多轮代码执行，最多 30 turns | native hard score |
| OfficeQA | 本地文档检索与工具循环，最多 24 tool calls | native hard score |
| DocVQA | 文档图像视觉问答 | exact/hard accuracy |
| LiveMathematicianBench | 数学 MCQ | exact accuracy |
| ALFWorld | 持久具身环境，最多 50 steps | task success |

在公开实现里，SearchQA 和 OfficeQA 使用归一化 exact match；SpreadsheetBench 要求一个任务的 grader cases 全部通过才记 hard success；DocVQA 虽会计算 ANLS，但论文 hard 结果要求 ANLS 近似 1；LiveMath 是正确选项 exact match；ALFWorld 是 episode win。Appendix C 还出现了一次 SealQA，但主表、消融和六 benchmark 叙述都没有 SealQA 结果，像是未清理的遗留文本。

七个 direct-chat target models 是 GPT-5.5、GPT-5.4、GPT-5.4-mini、GPT-5.4-nano、GPT-5.2、Qwen3.5-4B、Qwen3.6-35B-A3B。每个模型测六个 benchmark，得到 42 cells。GPT-5.5 另外在 Codex 和 Claude Code harness 各测五个 benchmark；ALFWorld 因需要持久具身环境而不放进这两个 harness。总计 \(42+5+5=52\) cells。

Direct chat 把 skill 加到 system/developer instruction 中，调用一次 target。Codex 模式把当前 skill 渲染成每个任务工作区里的 `SKILL.md`，执行 Codex CLI，并把精简执行 trace 提供给 optimizer；Claude Code 使用相同 workspace contract。三种 harness 最后都消费相同的 `best_skill.md`。

### 8.2 Baselines

论文比较：

- No skill；
- expert-written human skill；
- GPT-5.5 根据高层任务描述一次性生成、之后不更新的 LLM skill；
- Trace2Skill；
- TextGrad；
- GEPA；
- harness 侧的 EvoSkill。

这些 baseline 并没有全都出现在每个 cell：direct-chat 表包含 Trace2Skill、TextGrad、GEPA，但没有 EvoSkill；Codex/Claude Code 表包含 EvoSkill，却没有前三者。因此“52/52 最好或并列最好”是指每个 cell 相对该 cell 实际测过的最强竞争者，不是七种 baseline 在 52 个 cell 上全部做了全笛卡尔积比较。

所有比较使用相同 target、test split 和 scorer。论文没有给出一套严格等训练 token/等 API 成本的 baseline 预算，因此可以说它在统一 target/test/scorer 下领先，不能进一步说所有方法在完全等算力条件下被击败。公开仓库也没有提供所有 baseline 的完整实现、运行日志和逐 cell 预算，主表不能仅凭当前仓库做端到端独立复算。

### 8.3 数据切分

发布的 paper split manifests 是：

| Benchmark | Train | Selection/Val | Test |
|---|---:|---:|---:|
| SearchQA | 400 | 200 | 1,400 |
| SpreadsheetBench | 80 | 40 | 280 |
| OfficeQA | 50 | 24 | 172 |
| DocVQA | 107 | 53 | 374 |
| LiveMathematicianBench | 35 | 18 | 124 |
| ALFWorld | 39 | 18 | 134 |

这些 manifest 和默认数据代码采用 seed 42，整体接近 2:1:7。[官方 split manifests](https://github.com/microsoft/SkillOpt/blob/v0.1.0/data/README.md#L27-L38)

论文这里存在两处复现性不一致：Table 2 caption 把 train-size ablation 写成 4:1:5，但 Appendix C 写成 2:1:7；正文把 ALFWorld 写成 39/140/134，而 release manifest 是 39/18/134。后一处很像多写了一个 0。解释结果时应以具体 run artifact 和 manifest 为准。

### 8.4 默认超参数

- 4 epochs；
- rollout batch size 40；
- reflection minibatch 8；
- 16 parallel analyst workers；
- hierarchical merge batch size 8；
- 论文与配置把每个 minibatch 的 teacher refinement 上限写为 3；不过 paper-era/current reflection 主路径里，`max_analyst_rounds=3` 虽被读取，未清楚体现为三轮内容级自我修订，而 API/解析 `retries=3` 只是失败重试，不能等同于三轮语义 refinement；
- textual learning rate 4，cosine decay，floor 2；
- strict hard gate，ties reject；
- slow update 每 epoch 20 个同任务对照样本；
- meta skill 开启；
- patch mode；
- target 与 optimizer reasoning effort 均为 medium。

LiveMath 和 ALFWorld 因训练池较小采用 benchmark-specific batch 设置，但保留相同 gate、scheduler 和 slow/meta machinery。

## 9. 主结果

GPT-5.5 direct-chat 的完整六项结果是：

| Benchmark | No skill | SkillOpt | 绝对提升 |
|---|---:|---:|---:|
| SearchQA | 77.7 | 87.3 | +9.6 |
| SpreadsheetBench | 41.8 | 80.7 | +38.9 |
| OfficeQA | 33.1 | 72.1 | +39.0 |
| DocVQA | 78.8 | 91.2 | +12.4 |
| LiveMath | 37.6 | 66.9 | +29.3 |
| ALFWorld | 83.6 | 95.5 | +11.9 |

六项平均从 58.8 提升到 82.3，即 +23.5 points；按每个 cell 选择最强竞争 baseline 的 oracle 平均为 76.9，SkillOpt 仍高 +5.4。

其余 direct-chat target 的六 benchmark 平均提升约为：GPT-5.4 +12.7、GPT-5.4-mini +15.4、GPT-5.4-nano +26.7、GPT-5.2 +16.6、Qwen3.5-4B +19.2、Qwen3.6-35B-A3B +9.1。Codex 上 GPT-5.5 五项平均 +24.8，Claude Code 上 +19.1。论文报告 SkillOpt 在 52/52 cells 中最好或并列最好。

这证明的是广泛的实证覆盖，不是数学定理。尤其是较弱 target 常受益很大，符合“外部 procedure 补充权重中缺失的操作习惯”这一解释，但不能单凭这一趋势断言因果机制只有这一种。

## 10. 论文怎样论证 optimizer 有效

“有效”有两个层次。

第一层是运行时 admission：candidate 在 held-out selection 上严格优于 current，就被系统认为这一步有效。这个判断由 benchmark evaluator 的真实执行分数完成，不由 optimizer 自评。

第二层是论文级方法有效性，作者用了五组证据。

### 10.1 与强 baselines 比较

52/52 best-or-tied、GPT-5.5 direct +23.5、以及对 per-cell oracle baseline +5.4，是最强的总体证据。它排除了“只是比 no-skill 好”这一弱结论。

### 10.2 组件消融

在 GPT-5.5 同时作为 target 和 optimizer、三个 benchmark 上，Table 3 报告：

| 组件变化 | SearchQA | Spreadsheet | LiveMath | 相对默认的含义 |
|---|---:|---:|---:|---|
| bounded lr=4 | 87.1 | 77.5 | 61.3 | 默认参照 |
| without lr | 84.6 | 75.7 | 57.3 | 分别低 2.5、1.8、4.0 |
| with rejected buffer | 87.1 | 77.5 | 61.3 | 默认参照 |
| without buffer | 85.5 | 72.9 | 58.9 | 分别低 1.6、4.6、2.4 |
| meta + slow | 87.1 | 77.5 | 61.3 | 默认参照 |
| without meta | 85.1 | 75.7 | 58.1 | 三项下降 |
| without meta and slow | 86.3 | 55.0 | 59.7 | Spreadsheet 下降 22.5 |

这支持 bounded update、negative feedback 和 long-horizon memory 的贡献。但它主要是三个 benchmark 的单配置对照，不是多 seed 因果效应估计。

论文没有给出 matched 的“完全移除 validation gate”消融。因此 gate 的直接证据主要是严格 accept/reject 轨迹、Figure 3 的 selection/test 对齐，以及大量 proposal 最终只留下 1–4 次 accepted updates；它的组件因果证据弱于 rejected buffer 和 slow/meta 的显式移除实验。

Table 2 还分别扫了 train evidence、minibatch size、rollout batch size、learning rate 1/2/4/8/16、constant/cosine/linear scheduler 和 slow-update sample count。结果通常非单调：不存在一个设置在三项都最好。更合理的结论是方法对若干中等配置较稳健，并且足够证据、有限 edit budget 很重要，而不是“默认超参数已被证明最优”。

此外，Table 2(d) 中名义上的 \(L=4\) 得到 86.5/78.2/56.5，而其他默认面板和 Table 3 的 default row 是 87.1/77.5/61.3；论文没有解释这是否来自独立随机 run。因此不要把不同表中的单点小差值当成高精度、可直接配对的效应。

### 10.3 Selection 与 test 对齐

Figure 3 在 SearchQA、SpreadsheetBench 和 LiveMath 上画出 train rollout、selection-best 和 unseen test 的 epoch checkpoint。Selection 选择的 skill 通常伴随 test 改善，支持 gate 没有只拟合 train。但这只是三个 benchmark 的趋势图，不是 selection generalization 的形式化保证。

### 10.4 Transfer

不再优化、直接搬运 skill 后，论文报告的 transfer 行全部高于 target no-skill baseline：

- GPT-5.4 的 Spreadsheet skill 转到 GPT-5.4-mini / nano：+9.4 / +3.0；
- GPT-5.4 的 LiveMath skill 转到 mini / nano：+4.5 / +5.6；
- Codex skill 转 Claude Code：Spreadsheet +59.7，LiveMath +1.6；
- Claude Code skill 转 Codex：Spreadsheet +43.6，LiveMath +12.8；
- OlympiadBench skill 转 Omni-MATH：GPT-5.4 / mini / nano 分别 +3.7 / +1.8 / +1.3。

这支持学到的内容至少部分是可迁移 procedure，而不只是针对某个 train instance 的字符串。但 transfer 样本方向有限，不能推出任意模型、任意 harness 都通用。

### 10.5 Optimizer strength

Table 5 固定其余流程，只把 GPT-5.5 strong optimizer 换成与 target 同型号的 optimizer：

| Benchmark / target | Strong GPT-5.5 optimizer | Target-matched optimizer |
|---|---:|---:|
| Spreadsheet / GPT-5.4-mini | +11.4 | +7.1 |
| Spreadsheet / GPT-5.4-nano | +19.0 | +11.9 |
| SearchQA / GPT-5.4-mini | +4.3 | +2.4 |
| SearchQA / GPT-5.4-nano | +19.0 | +14.1 |

强 optimizer 四项都更好，说明 optimizer LLM 的分析和写 patch 能力确实是训练时杠杆；target-matched optimizer 仍保留约 56%–74% 的 strong-optimizer gain，说明方法不等同于“把强模型答案蒸馏给弱模型”。不过这里只测了两个 benchmark、两个 target scale。

## 11. 输出形态与训练成本

GPT-5.5 target / GPT-5.5 optimizer 的六个代表 run 为：

| Benchmark | 初始 tokens | 最终 tokens | accepted updates | 训练 tokens |
|---|---:|---:|---:|---:|
| SearchQA | 16 | 857 | 4 | 213.8M |
| SpreadsheetBench | 224 | 1,995 | 4 | 21.4M |
| OfficeQA | 145 | 883 | 1 | 20.8M |
| DocVQA | 81 | 959 | 3 | 188.2M |
| LiveMath | 154 | 379 | 1 | 23.2M |
| ALFWorld | 516 | 1,321 | 2 | 59.3M |

最终 skill 只有 379–1,995 tokens，接受 1–4 次 bounded updates，但训练消耗 20.8M–213.8M tokens。部署没有 optimizer call，只多一份短 skill；成本是一次性离线训练，而不是消失了。

Table 6 还列出 cost/point，但分母定义不清，并且与同表 train tokens 和 Table 1 的 no-skill test gain 不能一致复算。例如 OfficeQA 的 20.8M tokens 除以 +39.0 points 约为 0.53M/point，不是论文表里的 1.1M/point。论文的“训练量级很大、部署成本为零 optimizer calls”仍成立，但每 point 成本数字应谨慎引用。

学到的规则也主要是 procedure：Spreadsheet skill 学会先检查 workbook 结构、处理公式与静态值、填满完整 target range、保存后重新打开验证；ALFWorld skill 学会精确对象名、visited/frontier memory、progress lock 和 loop breaker。论文将这些案例与 transfer 结果共同作为“不是简单记忆实例”的证据。

## 12. 论文没有证明什么

论文没有形式化 convergence/optimality theorem，也没有证明 LLM optimizer 总能找到正确 edit。它依赖“提出很多候选，held-out gate 拒掉坏候选”的搜索逻辑。

它也没有系统报告多随机种子、置信区间或显著性检验。主结果主要来自固定 split seed 42，所以 52/52 是强覆盖性结果，不等于任何随机 seed 下都必胜。

同一个 selection split 被反复查询，存在 adaptive selection overfitting 的可能；独立 test 能发现一部分问题，Figure 3 也显示一定对齐，但没有给出候选次数增加时的系统性过拟合曲线。

Aggregate gate 只证明 bundle 整体在 selection metric 上提升，不能把功劳分配给每条 edit，也不能当作安全认证。

该方法依赖可靠 scorer。Exact match、可执行检查、环境 success 比较适合；主观、开放式、多目标任务需要人评或更可靠的 model judge，gate 的噪声会直接决定错误接受和错误拒绝。

论文保证了确定 evaluator 下 accepted checkpoints 的 selection score 单调上升；它没有保证 test score、真实部署分布或安全性也单调上升。小幅 transfer 增益如 +1.3、+1.6 也没有置信区间支撑。

原论文是离线 skill optimization，训练完导出静态 `best_skill.md`。它不是部署后根据每个用户会话持续自动学习；后来加入仓库的 SkillOpt-Sleep 是另一条 nightly transcript-driven pipeline，不能倒推成原论文实验设置。

## 13. 最准确的结论

SkillOpt 的真正创新是把自然语言 procedure 变成具有 current state、candidate、bounded step、negative feedback、validation gate 和 best checkpoint 的外部可训练对象。Optimizer model 是一个用多个 prompt 角色工作的通用 LLM，不需要被训练；它根据轨迹语义提出精确文本锚点和 patch，程序负责机械应用，held-out evaluator 才负责判断整份候选是否真的更好。

论文对“这种系统在六类 benchmark、七个 target 和三类执行方式中很有用”给出了很强的实证证据；对“每条修改都必要、安全，或者 optimizer 具有一般性最优性”则没有给出证明。
