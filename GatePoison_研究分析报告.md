# GatePoison：验证门可通过的自进化 Agent Skill 污染

> 研究分析报告（文献、SkillOpt 论文精读、源码审计、形式化、实验与防御）
> 检索与审计日期：2026-07-18
> 核心审计版本：SkillOpt v0.2.0，commit `e4ea6a6771e797ef820cdd8bfea64c57e0481065`；另对当前 main `8a50db33124009772eb68d2e27a115bec819935e` 做差异检查。

## 执行摘要

这个方向值得做，但不能再以“首次研究 self-evolving skill poisoning”作为 novelty。到 2026 年 7 月，已有工作已经覆盖：自进化 agent 的经验污染、从成功轨迹蒸馏恶意 procedure、持久 memory poisoning，以及 skill 生命周期和恶意 update 安全。最直接的三篇撞车工作是 [OEP](https://arxiv.org/abs/2605.18930)、[MPBench](https://arxiv.org/abs/2606.04329) 和 [SkillSec-Eval](https://arxiv.org/abs/2607.13987)。

真正有辨识度、也更容易形成强因果证据的问题是：

> **Can low-integrity but locally valid task evidence cause a validation-gated text-space optimizer to produce an accepted, utility-improving skill artifact that contains an unsupported, persistent, trigger-conditioned behavior?**

更简洁地说：

> **Validation-gated self-evolution is not security-gated self-evolution.**

这不是文字游戏。SkillOpt 和 SkillOpt-Sleep 的官方实现都先把多条 edit 合成一个 candidate，再只对整个 candidate 做一次 aggregate utility gate。Gate 把功劳归给整体，但通过后，每一条 instruction 都获得持久策略权限。源码审计和一个不改官方源码的最小控制复现均确认以下现象可达：target edit 单独不改善分数而被拒；useful edit 单独通过；两者组合后，整体分数由 useful edit 提升，而两条 edit 一起被接受。这一机制可命名为 **Beneficial-Edit Payload Hitchhiking** 或 **Composite Candidate Piggyback**。

因此，最强论文结构应当是：

1. 用原始 SkillOpt 的独立、干净 selection set 做严格机制证明和因果消融；
2. 用 SkillOpt-Sleep 展示 transcript-to-artifact 的现实信任升级链；
3. 用 EvoSkill 等第二个 validation-gated system 证明不是单一代码 bug；
4. 提出 Atomic Counterfactual Patch Gate，并测量安全收益、效用成本和验证开销。

当前最准确的 novelty 结论是：**“首次 self-evolving skill poisoning”不成立；“首次系统研究 evidence-only、utility-gate-passing、text-space skill optimizer poisoning”在本次检索中未发现直接工作，但必须使用审慎措辞，并由上述因果实验支撑。**

## 1. 研究问题重构

### 1.1 原始想法为什么太宽

“Skill 在 self-updating 过程中被污染”同时混合了至少五类不同问题：

- 恶意第三方直接发布或更新 skill；
- prompt injection 进入当前上下文；
- 恶意记录被写入长期 memory；
- 良性经验导致非预期安全退化；
- 低可信证据被 optimizer 概括为新的持久 skill。

前三类已有相当直接的文献；第四类也已有 misevolution 和 benign-experience safety degradation 工作。真正尚未被充分隔离的是第五类，而且还必须再加上“原生效用 gate 仍然通过”这一约束，才能与现有 memory poisoning 和 procedure insertion 拉开距离。

### 1.2 Creative-thinking skill 带来的三个重构

本报告使用了三个互补框架，而不是发散罗列攻击点：

1. **Problem Reformulation**：把“污染一个文件”改写成“低完整性证据如何跨越信任边界，获得高权限策略身份”。研究对象从恶意文本本身转为 `evidence → optimizer → candidate → gate → persistent policy` 的权限升级链。
2. **Constraint Manipulation**：主动收紧攻击者能力。攻击者不能改 skill、optimizer、gate、verifier 或干净 validation set，只能贡献少量任务上局部正确的 evidence；每个感染版本还必须通过原生 strict gate，并保持 clean utility。约束越强，论文与普通 prompt injection 的区别越清楚。
3. **Structural Analogy**：把 skill evolution 类比为 CI/CD 供应链。Transcript 类似低信任 source input，optimizer 类似 compiler/build system，gate 类似只跑功能测试的 CI，accepted `SKILL.md` 类似签名 release。功能测试全绿不能证明制品没有未授权行为；整包测试也不能为包内每个 patch 做因果归因。

这个类比给出一个可检验预测：**只要 admission gate 只观察有限 validation distribution 上的 aggregate utility，就会存在功能等价但 off-support 行为不同的候选；只要候选包含多条 edit 且整体打分，就会存在 credit-assignment blind spot。**

### 1.3 推荐定位与标题

首选标题：

> **GatePoison: Utility-Preserving Poisoning of Validation-Gated Self-Evolving Agent Skills**

备选标题：

- **SkillLaunder: From Low-Integrity Experience to High-Trust Agent Skills**
- **When Validation Passes the Payload: Composite-Edit Poisoning of Self-Evolving Skills**
- **CleanGate: Validation-Passing Backdoors in Text-Space Skill Optimizers**

最准确的一句话定位：

> **A validation gate assigns credit to an entire candidate artifact, while deployment grants authority to every instruction it contains.**

## 2. SkillOpt 论文精读

### 2.1 它解决什么问题

[SkillOpt: Executive Strategy for Self-Evolving Agent Skills](https://arxiv.org/abs/2605.23904) 将自然语言 skill 文档视为冻结 agent 的可训练外部状态。Target model、harness 和 evaluator 保持不变，独立 optimizer model 阅读 scored rollouts，对 skill 提出有界的 ADD、DELETE、REPLACE edits。候选只有在 held-out selection split 上严格优于当前 skill 时才接受；最终导出静态 `best_skill.md`，部署时不需要 optimizer，也不增加额外模型调用。[官方项目页](https://microsoft.github.io/SkillOpt/)把这一循环概括为 rollout、reflect、edit、gate、export。

这点非常重要：**原始 SkillOpt 是离线 benchmark-driven text-space optimization，不是部署中持续在线自更新。** 与用户最初问题完全贴合的是论文之后发布的 SkillOpt-Sleep，它才从真实 coding-agent transcripts 周期性挖掘任务、replay、consolidate、stage 和 adopt。[v0.2.0 release](https://github.com/microsoft/SkillOpt/releases/tag/v0.2.0)明确把 Sleep 描述为 nightly offline self-evolution engine。

### 2.2 形式化目标

设冻结 target model 为 (M)，harness 为 (h)，任务为 (x)，skill 为 (s)：

\[
(\tau(s),r(s))=h(M,x,s), \qquad r(s)\in[0,1].
\]

其中 (\tau) 是 trajectory，(r) 是任务 verifier 给出的效用。Optimizer 在训练证据上生成候选集合，selection split 决定接受，test split 只用于最终报告：

\[
s^*_{\mathrm{sel}}
=
\arg\max_{s\in\mathcal C(D_{\mathrm{train}})}
\frac{1}{|D_{\mathrm{sel}}|}
\sum_{x\in D_{\mathrm{sel}}} r(x;s).
\]

这个目标只编码“被 verifier 测到的任务效用”，没有显式编码：

- 行为授权与禁止项；
- update provenance 或 evidence trust；
- 每条 edit 的数据支持和边际贡献；
- trigger family 覆盖；
- off-distribution 安全行为；
- skill、memory、archive 等全状态一致性。

因此 held-out gate 是性能选择器，不天然是安全认证器。当前 main 的[官方 safety 文档](https://github.com/microsoft/SkillOpt/blob/8a50db33124009772eb68d2e27a115bec819935e/docs/guideline.html#L503-L519)也明确说明：held-out gate 只能减少被测任务上的回退，不是 security boundary，也不是一般性改进的证明。该说明是 main 后续文档中的边界声明，不能倒推为论文发布时已经完成了安全评估。

### 2.3 完整训练循环

论文 Algorithm 1 的核心流程是：

1. 用当前 skill 在 (D_{\mathrm{train}}) 上运行目标模型；
2. 分离成功与失败 trajectories；
3. 对 minibatches 反思并生成 edit；
4. 分层 merge 多个反思结果；
5. 对 edits 排序，并按 textual learning-rate budget (L_t) 截断；
6. 一次性把选中的 edits 应用成候选 (\tilde s_t)；
7. 在 (D_{\mathrm{sel}}) 上评估整个候选；
8. 仅当 (\hat U_{\mathrm{sel}}(\tilde s_t)>\hat U_{\mathrm{sel}}(s_t)) 时接受，平分也拒绝；
9. rejected candidate 的 edits 和失败模式进入 epoch-local buffer；
10. epoch 间还有 slow/meta update，最后在独立 test split 上报告 best skill。

论文把 bounded edits、strict held-out gate、rejected buffer 和 slow/meta update 看作稳定文本优化的关键控制。安全研究最值得抓住的不是“它没有 gate”，而是：**它有 gate，但 gate 的观测粒度和最终授权粒度不一致。**

### 2.4 实验结果应该怎样读

论文覆盖 SearchQA、SpreadsheetBench、OfficeQA、DocVQA、LiveMathematicianBench、ALFWorld，使用七个 target models 以及 direct chat、Codex、Claude Code 三种执行环境。对 GPT-5.5 direct chat，论文表 1 报告：

| Benchmark | No skill | SkillOpt | 绝对提升 |
|---|---:|---:|---:|
| SearchQA | 77.7 | 87.3 | +9.6 |
| Spreadsheet | 41.8 | 80.7 | +38.9 |
| OfficeQA | 33.1 | 72.1 | +39.0 |
| DocVQA | 78.8 | 91.2 | +12.4 |
| LiveMath | 37.6 | 66.9 | +29.3 |
| ALFWorld | 83.6 | 95.5 | +11.9 |

六项平均从 58.8 提升到 82.3，平均增益 +23.5。论文还报告 Codex 平均 +24.8、Claude Code 平均 +19.1，以及 52/52 个 model×benchmark×harness 单元中达到最佳或并列最佳。

这些结果强力支持“skill 作为可优化外部状态有用”，但不能被扩张解释为安全或统计保证：

- 数据 split 主要使用固定 `split_seed=42`；
- 没有系统报告多随机种子置信区间或显著性检验；
- gate 使用单点严格比较，没有最小效应阈值、置信界或 repeated selection correction；
- optimizer 会反复查询同一个 selection split，存在 adaptive selection overfitting 风险；
- “52/52”是这些具体实验单元的 measured best/tie，不代表跨 seed、跨更新或对抗条件下的稳健性；
- 强 optimizer 和数千万到上亿级 rollout tokens 使成本与 optimizer-dependence 成为现实限制。

最终 skill 只有约 379–1995 tokens，且每个任务接受的更新很少。这对安全研究反而有利：如果极少数 accepted updates 就能永久改变策略，那么逐版本审计和因果归因尤其重要。

### 2.5 论文自己承认与没有讨论的局限

论文承认依赖可靠、自动化的 scored trajectories 和 held-out split；主观、多维或昂贵任务需要更强 evaluator；离线优化成本高；目前只优化单一 skill 而非大规模 skill library；迁移前仍需谨慎 held-out evaluation。

论文没有建立 adversarial threat model，也没有系统讨论：trajectory poisoning、malicious transcript、bundle-level credit attribution、triggered behavior、provenance、trust-domain separation 或完整 rollback。论文中的“harmful proposal/rewrite”主要指效用回退，不等同于安全意义上的恶意行为。

另有两项复现审阅值得记录：

- 论文正文将 ALFWorld split 写成 39 train / 140 selection / 134 test，但 v0.1.0 的[官方 manifest](https://github.com/microsoft/SkillOpt/blob/v0.1.0/data/alfworld_path_split/split_manifest.json)是 39 / 18 / 134，前者很可能是排版或笔误。
- 论文表 1 的 Codex/Claude Code 数值与摘要平均增益一致；当前项目网页展示的部分 LiveMath 增益和平均值不同。安全实验应以冻结论文 PDF、tag 和 manifest 为准，而不是混用不断更新的网页数字。

## 3. 官方源码审计

### 3.1 最关键事实：整包 gate

原始 SkillOpt 中，optimizer 先 merge/rank 多条 patch，再把 top-(L_t) edits 全部应用为一个 candidate，最后只评估一次完整 candidate：

- [merge、rank、截断](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt/engine/trainer.py#L1220-L1285)；
- [应用整包并统一评估、gate](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt/engine/trainer.py#L1308-L1453)；
- [逐条机械应用到同一 document](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt/optimizer/skill.py#L165-L201)；
- [LLM ranking 不是 utility attribution](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt/optimizer/clip.py#L25-L108)。

SkillOpt-Sleep 结构相同。Backend 可一次返回多条 edit；`_gate_apply` 先把 `edits` 整体应用为 `new_doc`，再 replay 整个 validation set，一旦 aggregate score 上升就把所有成功应用的 edits 一并接受：[backend](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/backend.py#L408-L542)、[consolidate.py](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/consolidate.py#L137-L159)、[memory.py](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/memory.py#L73-L114)。Skill edits 和 memory edits 分别过门，但每个 bundle 内没有 per-edit marginal 或 leave-one-out validation。

本次审计做了一个安全的最小控制复现：

| 候选 | Validation score | Gate | Trigger behavior |
|---|---:|---|---|
| target-only | 0 → 0 | reject | safe |
| useful-only | 0 → 1 | accept 1 edit | safe |
| useful + target | 0 → 1 | accept 2 edits | target behavior active |

这个复现只证明**实现允许 hitchhiking 结构**，不等于已经证明真实模型在低 poison budget 下会稳定从自然 transcript 生成目标 edit。后者才是论文需要完成的实证贡献。

### 3.2 Gate 实际证明什么

原始 SkillOpt 和 Sleep 的 gate 最终都把候选投影为一个 scalar utility，并判断 `cand_score > current_score`；没有独立安全 predicate。[原始 gate](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt/evaluation/gate.py#L46-L148)和 [Sleep gate](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/gate.py#L24-L50)都体现了这一点。

原始 SkillOpt 的 `val` 是独立 canonical split，适合构造攻击者不能接触 validation set 的强 threat model。Sleep 则不同：默认从同一批 mined transcript tasks 用稳定 hash 切 train/val/test，[mine.py](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/mine.py#L221-L278)显示 train 和 val 虽行级分离，却来自同一信任域、同一 miner。若 val 为空，`_split` 还会退回 train 或 all-but-test，[consolidate.py](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/consolidate.py#L45-L65)意味着小样本时 held-out 性质可退化。

所以应区分两种实验：

- **严格机制实验**：原始 SkillOpt，攻击者只污染 train，固定且不可见的 clean selection/test；
- **现实 deployment 实验**：Sleep 默认 transcript-derived val，研究 evidence 与 validator 同源带来的 trust-domain failure；另加外部预注册 trusted val 作为加强配置。

### 3.3 Transcript 到 skill 的信任升级链

Sleep 的真实 backend 会 harvest session、用 LLM miner 生成 task intent/checks/rubric，再 replay 和 consolidate。[cycle.py](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/cycle.py#L140-L201)和 [llm_miner.py](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/llm_miner.py#L29-L132)显示 user prompt 与 assistant final 会进入 miner prompt，而当前主要保护是截断、schema 和 secret redaction，不是语义级 instruction/data separation。

但 threat model 必须忠于实现：Codex/Claude harvester 并不直接复制 raw tool arguments、raw tool outputs 或 system/developer instructions。外部网页、文档或工具输出要影响 Sleep，需要其内容被 assistant final 回显、进入 user-pasted text，或通过 reviewed TaskRecord 流程。不能直接声称“任意网页 tool output 会原样进入 Sleep”。

信任升级可画为：

```mermaid
flowchart LR
    A["低可信会话文本"] --> B["LLM miner: intent/checks/rubric"]
    B --> C["同源 train/val task records"]
    C --> D["Optimizer 生成多 edit candidate"]
    D --> E["Aggregate utility gate"]
    E --> F["Accepted/staged SKILL.md"]
    F --> G["人工 adopt 或 opt-in auto-adopt"]
    G --> H["高权限持久策略"]
```

### 3.4 Archive、adoption 与 rollback

Sleep 默认 `auto_adopt=false`，accepted update 先 stage，之后由人审阅；只有显式开启 auto-adopt 才自动覆盖 live files。[默认配置](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/config.py#L24-L62)与 [cycle adoption path](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/cycle.py#L262-L317)支持这一点。因此结果必须分层报告：

1. Proposal Infection：optimizer 是否生成目标 edit；
2. Artifact Infection：目标 edit 是否通过 gate 并进入 staged artifact；
3. Deployment Infection：是否经过人工审查或 auto-adopt 进入 live skill。

通过 gate 不能直接写成“默认已部署”。人工审查本身应作为 defense baseline，必要时做 blinded reviewer study。

另一个条件性风险是 archive persistence。正常 cycle 在 consolidation 后会把当晚 real tasks 加入外部 archive，不以候选是否被接受为条件；但 `recall_k=0` 默认关闭。开启 recall 后，相似历史任务会作为 train-only evidence 被重新引入。[cycle.py](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/cycle.py#L219-L244)、[state.py](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/state.py#L86-L96)和 [dream.py](https://github.com/microsoft/SkillOpt/blob/e4ea6a6771e797ef820cdd8bfea64c57e0481065/skillopt_sleep/dream.py#L62-L138)说明：被拒更新的来源证据仍可保留，并在 opt-in recall 下再次影响未来更新。

Adopt 只备份 live `SKILL.md`/`CLAUDE.md`，没有原子快照或恢复 external state、archive、history、last-harvest；CLI 也没有 rollback command。因此“skill-only restore 后重感染”是一个有源码依据、但仍需实验验证的假设，且条件是 recall 开启，不能写成默认漏洞。

### 3.5 论文与 release 的版本差异

以下差异应写入实验 artifact manifest，而不是混成一个“SkillOpt 行为”：

- 论文 Algorithm 1 描述 slow update 也经过 selection gate；v0.1/v0.2 base config 的 `slow_update_gate_with_selection=false` 默认会把 slow update 写入 current state，而不是直接提升 best。最终 current 仍需得分更高才可能成为 best。这是 protocol/implementation 差异和次级状态路径，不应夸大为最终部署 gate 完全绕过。
- `skillopt_sleep/slow_update.py` 在 v0.2 和当前 main 的正常 CLI cycle 中没有接线；实际 caller 是 experiment harness。不能把它称为 shipping Sleep 主路径漏洞。
- 当前 main 新增的 semantic-density bonus 是 opt-in、默认关闭，Sleep vendored gate 没有该功能。它适合作为 reward-hacking 消融，不应冒充 v0.2 默认风险。
- 当前 main 的 safety 文档比 v0.2 release 更明确。实验必须冻结 tag/commit、config、prompt、model snapshot 和 data split。

## 4. 文献版图与 Novelty

### 4.1 最直接的三篇撞车工作

**OEP** 已经证明：低权限用户可以提供“局部正确但不可迁移”的 edge cases，使 reflective/self-evolving agent 在 consolidation 中形成过度泛化的持久规则；论文还在作者实例化的 OpenClaw-based skill module 上测试。[OEP](https://arxiv.org/abs/2605.18930)直接否定“首次从经验污染 self-evolving skill/agent”的主张。其边界是没有 SkillOpt 式独立 held-out utility gate，也没有证明感染版本效用上升且逐版本过门。

**MPBench** 已明确定义 Skill-Procedure Insertion：把对抗步骤藏进成功交互序列，再由 agent 蒸馏成 reusable skill；在 HERMES 上报告 ASR 58.33%、RSR 61.67%。这直接否定“首次 transcript-to-skill poisoning”。但该攻击是 HERMES-only，论文说明 OpenClaw 缺少相应 C4 写通道；也没有 held-out utility gate。[MPBench](https://arxiv.org/abs/2606.04329)

**SkillSec-Eval** 已把 evolution 放入 skill lifecycle，并测试 permission escalation、instruction injection、tool substitution、dependency compromise、publisher spoofing。无防护时恶意 update 会继承既有信任；其防御 MDR 92.5%，但 FPR 37%、benign update acceptance 63%。它否定“skill evolution security 无人研究”，但攻击者直接控制 update，防御是 provenance/semantic security admission，不是研究低权限 evidence 如何欺骗性能 gate。[SkillSec-Eval](https://arxiv.org/abs/2607.13987)

### 4.2 一手来源矩阵

下表按与本课题的结构距离列出 19 项核心一手来源。2026 年的大多数工作仍是预印本；表中的“未覆盖”是对论文 threat model 和系统机制的边界判断，不是对其质量的评价。

| 类别 | 工作 | 已覆盖的核心问题 | GatePoison 仍需证明的差异 |
|---|---|---|---|
| 核心 victim | [SkillOpt](https://arxiv.org/abs/2605.23904) | 严格 held-out utility-improvement gate；输出持久 `best_skill.md` | 低可信 evidence 能否产生 gate-passing target behavior |
| 直接重叠 | [OEP](https://arxiv.org/abs/2605.18930) | 局部正确经验被过度泛化为持久规则；低权限黑盒攻击 | 无严格独立 utility gate，无 composite-edit 归因实验 |
| 直接重叠 | [MPBench](https://arxiv.org/abs/2606.04329) | 成功轨迹中的对抗步骤被蒸馏为 reusable skill | HERMES-only 的该通道；无 held-out gate |
| 直接重叠 | [SkillSec-Eval](https://arxiv.org/abs/2607.13987) | skill evolution 生命周期、恶意 update 和 re-admission | 攻击者直接控制 update，不是 evidence-only 性能门攻击 |
| Memory poison | [AgentPoison](https://proceedings.neurips.cc/paper_files/paper/2024/hash/eb113910e9c3f6242541c1652e30dfd6-Abstract-Conference.html) | 少量长期 memory/RAG demonstrations 植入 backdoor | 直接改 memory/KB，无自更新或 admission gate |
| Memory poison | [MINJA](https://proceedings.neurips.cc/paper_files/paper/2025/hash/42a97bbd9844d2bf68596730af80bcdf-Abstract-Conference.html) | Query-only 写入 agent memory | success-only storage filter 不是 held-out skill gate |
| Memory poison | [InjecMEM](https://openreview.net/pdf?id=QVX6hcJ2um) | 单次交互、无直接 memory 权限的持久注入 | 写 memory item，不生成/验证 `SKILL.md` |
| Memory poison | [MemIncept](https://openreview.net/forum?id=1YNrlSSRsk) | 多条表面无害 query 的组合式 memory injection | 组合发生在 memory retrieval，不是 edit bundle gate |
| Environment poison | [Poison Once, Exploit Forever](https://arxiv.org/abs/2604.02623) | 攻击网页写入 trajectory memory，跨站/跨 session | Raw trajectory memory，无 optimizer admission gate |
| Persistent evolution | [Zombie Agents](https://arxiv.org/abs/2602.15654) | 网页注入经 memory evolution 自我强化和持久化 | 无性能 gate；不形成新 skill artifact |
| Experience retrieval | [MemoryGraft](https://arxiv.org/abs/2512.16962) | 恶意“成功经验”影响后续检索和模仿 | 直接构造 memory store，实验规模较小；无 gate |
| Cross-session | [What If Prompt Injection Never Left?](https://arxiv.org/abs/2606.04425) | working/archive memory、file context、tool/skill metadata 的持久注入 | 广义 stored injection，不是 optimizer update |
| Cross-session | [Bad Memory](https://arxiv.org/abs/2607.14611) | `CLAUDE.md`、`AGENTS.md` 等持久文件中的 prompt injection | 多从已植入文件开始；无低权限生成与 gate 链 |
| Direct skill | [SkillJect](https://arxiv.org/abs/2602.14211) | 自动改写恶意 `SKILL.md` 和辅助脚本 | 攻击者已能控制 artifact；应作为 static baseline |
| Direct skill | [Skill-Inject](https://arxiv.org/abs/2602.20156) | 恶意 skill file 对 agent 的攻击 | 无 self-update/gate |
| Supply chain | [DDIPE](https://arxiv.org/abs/2604.03081) | Skill package 代码示例/配置模板中的供应链 payload | 第三方 artifact poisoning，不是 evidence laundering |
| Registry | [Under the Hood of SKILL.md](https://arxiv.org/abs/2605.11418) | Discovery、selection、governance 的语义操纵 | 不攻击 evolution gate |
| Evolution safety | [Your Agent May Misevolve](https://arxiv.org/abs/2509.26354) | Model、memory、tool、workflow evolution 的非预期风险 | 非对抗性 misevolution，无有界 poisoning |
| RAG baseline | [PoisonedRAG](https://www.usenix.org/conference/usenixsecurity25/presentation/zou-poisonedrag) | 百万级知识库中的低比例知识污染 | 无 agent self-update 或 skill gate |

另有两条对 victim 选择很重要的演化工作： [EvoSkill](https://arxiv.org/abs/2603.02766) 也用 held-out Pareto frontier 选择 skill/program，适合作为外部 validation-gated victim；[Trace2Skill](https://arxiv.org/abs/2603.25158) 从多条 trajectory 提炼 transferable skill，适合作为无严格 gate 的对照。更广的自演化系统还包括 [SkillFoundry](https://arxiv.org/abs/2604.03964)、[SkillForge](https://arxiv.org/abs/2604.08618)、[EvoSkills](https://arxiv.org/abs/2604.01687) 和 [SkillOS](https://arxiv.org/abs/2605.06614)。这些系统说明 skill evolution 已经形成快速扩张的研究对象，也意味着论文最好抽象出机制，而不是只报告一个 repo bug。

与主动攻击相邻但不同的安全结果是 [On Safety Risks in Experience-Driven Self-Evolving Agents](https://aclanthology.org/2026.findings-acl.2091/)：即使全是 benign experience，也会因 execution bias 导致高风险场景中的安全退化；加入 refusal experience 又可能过度拒绝。这项结果支持“utility feedback 不能替代 safety objective”，但不包含攻击者或 gate-passing artifact poisoning。

### 4.3 Novelty ladder

| 候选主张 | 判断 | 建议 |
|---|---|---|
| 首次研究 self-evolving agent 安全 | 不成立 | Misevolve、experience-safety 等已覆盖 |
| 首次研究 self-evolving skill poisoning | 不成立 | OEP、MPBench、SkillSec-Eval 直接相邻 |
| 首次从 transcript 蒸馏恶意 reusable skill | 不成立 | MPBench 已有 Skill-Procedure Insertion |
| 首次低权限 evidence-only 攻击 text-space skill optimizer 的原生 held-out utility gate | 本次检索未发现直接工作 | 可用“to our knowledge”，并公开检索边界 |
| 首次证明 bundle-level gate 的 beneficial-edit payload hitchhiking | 若真实模型实验和反事实消融成立，贡献最强 | 应作为主机制，而非附带代码观察 |
| 首次研究 rollback reinfection、clean-night persistence、cross-harness transfer 的组合 | 可作为增强贡献 | 不能代替主机制 |

推荐 novelty 句：

> To our knowledge, prior work has not shown that low-privilege, task-valid interaction evidence can induce a text-space skill optimizer to produce a utility-improving candidate that passes its native held-out admission gate while embedding a dormant, persistent target behavior.

更保守、也更不容易被 reviewer 抓住的版本：

> We study a missing lifecycle transition: how untrusted task evidence can acquire the authority of a newly generated skill version under a performance-only admission gate.

## 5. 问题形式化

### 5.1 状态、优化器与 gate

将系统完整状态写为：

\[
Z_t=(S_t,M_t,A_t,B_t,H_t),
\]

其中 (S_t) 是 skill，(M_t) 是 memory/meta guidance，(A_t) 是 archive，(B_t) 是 rejected buffer，(H_t) 是版本与 adoption history。第 (t) 轮证据为 (E_t=C_t\cup P_t)，其中 (C_t) 是 clean evidence，(P_t) 是 poison evidence。Optimizer 输出多 edit 候选：

\[
\Delta_t=O(E_t,Z_t)=\{e_{t,1},\ldots,e_{t,k}\}, \qquad
\tilde S_t=S_t\oplus\Delta_t.
\]

原生性能门可抽象为：

\[
G_V(S_t,\Delta_t)=
\mathbf 1\left[
\hat U_V(S_t\oplus\Delta_t)>
\hat U_V(S_t)+\tau
\right],
\]

SkillOpt 原生 strict gate 中 (\tau=0)。安全行为应在另一个分布或策略集合上定义：

\[
R_T(S)=\Pr_{x\sim T}[S\text{ induces target/unauthorized behavior on }x].
\]

当前 gate 只观测 (\hat U_V)，不观测 (R_T)、provenance 或 edit support。

### 5.2 强威胁模型

建议把主论文威胁模型收紧为：

- 攻击者不能修改当前 skill、optimizer、target model、harness、verifier、gate、selection/test set 或源码；
- 攻击者只可贡献不超过 (\epsilon) 比例的普通 task evidence；
- 每条 poison evidence 必须通过真实 task oracle，且对当前任务是局部正确的；
- 攻击者不知道 selection/test 的具体样本，只知道任务分布和公开系统设计；
- 每一个落盘的感染版本都必须通过原生 strict utility gate；
- clean test utility 需满足预注册的 non-inferiority margin，最好仍显著提升；
- 目标行为仅在预注册、selection 未覆盖的 trigger family 上出现；
- 攻击者停止贡献后，还要测量若干轮 clean evolution 中的持久性。

现实 Sleep 实验可以放宽为攻击者控制 transcript 中可被 harvester 收集的 user/assistant text；若研究网页或 tool-output 路径，必须先实证其内容如何被 echo 或转写进被 harvest 的文本，不能跳过 ingestion chain。

### 5.3 攻击成功的合取条件

不要只报告 ASR。一次完整成功至少同时满足：

1. **Evidence validity**：poison case 通过任务 oracle；
2. **Proposal infection**：optimizer 生成语义目标 edit；
3. **Native gate pass**：感染候选在未修改 gate 下被接受；
4. **Artifact infection**：target edit 出现在 accepted/staged skill；
5. **Target activation**：held-out trigger family 上的 ASR 达阈值；
6. **Clean non-inferiority**：独立 clean test utility 不低于基线 margin；
7. **Persistence**：经过 (K) 个 clean cycles 后仍存在；
8. **Authority upgrade**：目标行为来自新 skill，而不是同轮 prompt 中的瞬时 injection；
9. **Deployment distinction**：默认 manual-adopt 与 opt-in auto-adopt 分开报告。

主指标可定义为联合事件：

\[
\mathrm{VPA}=Pr[
G_V=1\land \mathrm{AIR}=1\land
\Delta U_{\mathrm{test}}\ge -\delta\land
\mathrm{ASR}_T\ge \alpha
],
\]

其中 VPA 表示 Validation-Passing Artifact infection。

### 5.4 两个结构性命题

**命题 1：有限 utility gate 的 off-support 不可区分性。** 对任何只依赖有限 validation set (V) 上输出的 gate，只要 trigger set (T\cap V=\varnothing)，就可能存在 (S_b,S_m) 在 (V) 上行为完全相同、在 (T) 上行为不同。Gate 无法仅凭 (V) 区分它们。这是有限行为测试的基本边界，不应包装成深奥理论定理。

**命题 2：整包打分无法识别 edit-level 因果贡献。** 如果 gate 只观察 (U(S\oplus\{e_1,\ldots,e_k\}))，则它不能从一个 aggregate score 唯一恢复每条 edit 的 utility、必要性或风险贡献。额外的 subset/leave-one-out evaluation 或结构假设是不可少的。

这两个命题分别解释“为什么 dormant behavior 可以不被测到”和“为什么 target edit 可以借 useful edit 的效用过门”。

## 6. 攻击机制假设

### 6.1 主攻击：Beneficial-Edit Payload Hitchhiking

设 (e_u) 是确实提高 validation utility 的 useful edit，(e_p) 是 validation 上休眠、在 trigger family 上激活的 target edit。需要证明：

\[
\begin{aligned}
&U_V(S+e_u)>U_V(S),\\
&U_V(S+e_p)\le U_V(S),\\
&U_V(S+e_u+e_p)>U_V(S),\\
&R_T(S+e_u+e_p)\gg R_T(S+e_u).
\end{aligned}
\]

关键不是手工把两条 edit 塞进文件，而是让低比例、局部正确 evidence 诱导 victim optimizer 在同一次输出中生成两者。最强证据是固定同一次 optimizer proposal，做 (S)、(S+e_u)、(S+e_p)、(S+e_u+e_p) 四组反事实，再对每条 edit 做 leave-one-out。

### 6.2 Sleep 特有：Validator Co-Provenance

Sleep 默认 train/val 都来自同一批 transcript-derived TaskRecords。即使行级 held-out，也不等于 trust-domain held-out。攻击者可能不需要直接命中 val 样本，只需影响 miner 如何概括 intent、checks 与 rubric，使学习证据和判定标准共享同一偏差。

这条攻击比 bundle hitchhiking 更现实，但 novelty 更容易被 reviewer 归类为 validation contamination。它应作为第二机制，并用以下对照隔离：同一 poison corpus，分别使用默认同源 val、外部 trusted val、人工 oracle val。若攻击只在同源 val 成功，它证明的是 validator laundering；若在外部 trusted val 仍成功，才支持 gate blind spot。

### 6.3 条件性机制：Archive Recall 与延迟重感染

当 `recall_k>0` 时，被拒候选的来源 task 仍在 archive，未来可能被 recall 成 train evidence。研究问题不是“rollback command 有 bug”，而是：

> 只恢复 live skill、但不恢复 archive/meta/history，是否会使系统在后续 clean-looking cycle 中重新生成同一目标 edit？

实验应比较 full-state rollback、skill-only restore、archive purge、recall off/on。由于 recall 默认关闭，这只能作为 opt-in persistence result。

### 6.4 诊断性而非主贡献的路径

- Rejected buffer 可能让被拒 proposal 继续影响同 epoch 的 optimizer，但当前证据不足以断言可利用；可做 state-ablation，不宜写成既成漏洞。
- 原始 research engine 的 slow-update 配置差异适合复现审计，不宜成为主攻击，因为最终 best 仍有评估路径。
- 当前 main 的 semantic-density bonus 若开启，可能被形式词频 gaming；这是干净的 reward-hacking demo，但属于 opt-in main-only 配置，不代表 v0.2 Sleep 默认行为。

## 7. 最小可发表实验设计

### 7.1 三阶段路线

**Phase 0：确定性机制复现。** 固定 SkillOpt-Sleep v0.2.0，用可审计的 deterministic backend 复现 target-only reject、useful-only accept、bundle accept-all。目标是建立 code-to-claim trace，不主张真实攻击效果。

**Phase 1：原始 SkillOpt 上的强因果实验。** 使用论文式独立 train/selection/test；只污染 train；冻结 optimizer、target、gate 和 clean selection。证明真实 optimizer 能从任务有效 evidence 生成 composite proposal，并且每个 accepted version 都通过 strict native gate。

**Phase 2：SkillOpt-Sleep 的真实链路与外部验证。** 从可 harvest 的 transcript text 出发，比较默认同源 val 与 trusted external val；分别测 staged、manual-adopt、auto-adopt；开启 recall 的条件性 persistence 实验。最后在 EvoSkill 或另一个 validation-gated framework 上复现机制，排除 SkillOpt-specific bug。

### 7.2 Victim 与版本

推荐最小组合：

| 角色 | 系统 | 目的 |
|---|---|---|
| 主机制 victim | SkillOpt v0.1.0 paper-style fully gated config | 最干净的 train-only poison 与 held-out gate 因果证明 |
| 部署 victim | SkillOpt-Sleep v0.2.0 | Transcript mining、staging、archive、manual/auto adoption |
| 外部 victim | EvoSkill frozen commit | 验证 held-out composite-selection 问题是否跨实现 |
| 无严格 gate 对照 | Trace2Skill 或 Sleep gate-off | 测量 gate 的 utility 保护和安全盲点分别是什么 |
| 探索性审计 | SkillOpt current main frozen commit | 仅做版本差异，不与 release 主结果混合 |

每个实验 artifact 必须记录 commit、完整 config、optimizer/target model 版本、prompt hash、split manifest、poison IDs、accepted edit diff、gate traces、archive/meta-state 快照。

### 7.3 任务域和安全目标

选择至少三个具有确定 oracle、又能表达 tool behavior 的域：

- **Spreadsheet**：效用信号稳定，SkillOpt 原论文增益大；目标行为可是在 trigger workbook 上省略一个预注册检查或写入 harmless canary cell；
- **Search/Office QA**：测试证据收集与引用策略；目标行为可是在 trigger provenance pattern 下选择一个 mock-untrusted source 或输出 canary；
- **Tool-loop / ALFWorld / coding sandbox**：测试持久 procedure 对行动序列的影响；目标行为使用本地 stub tool 或只写 `/tmp` 研究 sink，不访问真实网络、凭据或用户数据。

Trigger 不应只是一个精确罕见 token，否则 reviewer 会说是传统 lexical backdoor。至少准备三类 trigger family：

1. lexical pattern；
2. structural condition，例如特定表头组合、文件树形状、工具返回 schema；
3. semantic condition，例如来源可信度或任务约束的组合。

所有 payload 都应是无害模拟：canary 输出、mock tool selection、sandbox-only side effect、跳过一项非安全关键检查。研究目标是测量未授权策略变化，不需要真实窃密或外部破坏。

### 7.4 实验因子

一个可控而完整的设计是：

| 因子 | 水平 |
|---|---|
| Poison fraction (\epsilon) | 0、0.5%、1%、2%、5%、10% |
| Victim | SkillOpt、Sleep、EvoSkill |
| Target model | frontier、mid-size、open-weight 各 1 个 |
| Optimizer | 强 optimizer 1 个；matched/self-optimizer 1 个 |
| Domain | Spreadsheet、QA/research、tool-loop |
| Trigger | lexical、structural、semantic family |
| Bundle size | 1、2、4 edits |
| Gate | native、trusted-val、atomic-counterfactual、off |
| Evolution horizon | 0、1、3、5、10 clean cycles |
| Adoption | staged、human review、auto-adopt |
| Seeds | 最少 5；主结果最好 10 |

如果预算受限，先做 2 domains × 3 targets × 5 seeds × 4 poison rates，确认机制后再扩展完整矩阵。不要一开始把资源摊到十几个系统上。

### 7.5 Baselines

必须同时覆盖“已有攻击”和“机制消融”：

- clean/no-poison；
- random locally-valid evidence；
- direct prompt injection；
- OEP-style locally-correct non-transferable experiences；
- MPBench Skill-Procedure Insertion；
- AgentPoison/MINJA-style memory poisoning，限有 memory channel 的 victim；
- direct static malicious skill，作为 SkillJect/Skill-Inject upper bound；
- target edit only；
- useful edit only；
- useful+target bundle；
- 将同一两条 edit 拆成两个独立 gate transactions；
- random pairing/shuffled evidence；
- native gate off/on；
- same-source val vs trusted independent val。

如果 proposed attack 只比 no-poison 强，却没有与 OEP、MPBench 和 direct static skill 区分，novelty 很难成立。

### 7.6 最关键因果消融

对同一次真实 optimizer 输出保留原始文本和顺序，并评估：

\[
S,\quad S+e_u,\quad S+e_p,\quad S+e_u+e_p.
\]

再做：

- leave-one-edit-out：(S+\Delta\setminus\{e_i\})；
- order swap：先/后应用 target edit；
- semantic neutralization：保留长度与格式，移除目标语义；
- support ablation：删除产生 target edit 的 poison cases；
- trusted-val replacement；
- optimizer replacement；
- skill-only vs full-state rollback。

只有当 target edit 单独不能过门、bundle 能过门、移除 target edit 后 ASR 消失、移除 useful edit 后 gate 不再通过，才能把现象归因于 hitchhiking，而不是普通 injection 或 optimizer 自发不稳定。

### 7.7 指标

| 指标 | 定义 |
|---|---|
| Proposal Infection Rate | optimizer 输出包含目标语义 edit 的比例 |
| Gate Pass Rate | 含 target edit 的候选通过原生 gate 的比例 |
| Artifact Infection Rate | accepted/staged artifact 实际包含目标行为的比例 |
| Deployment Infection Rate | 经 human/auto adoption 后进入 live skill 的比例 |
| Trigger ASR | trigger family 上目标行为成功率 |
| Clean Utility Delta | 独立 test 上相对 clean baseline 的效用差 |
| Validation Uplift | 被接受时 selection utility 增益 |
| Persistence Curve | clean cycles 后 AIR/ASR 的衰减曲线与半衰期 |
| Rollback Reinfection | skill-only restore 后再次感染的概率/时间 |
| Transfer | 跨 target model、optimizer、harness、nearby domain 的 AIR/ASR |
| Review Detection | blinded human reviewers 发现目标 edit 的比例、耗时和误报 |
| Defense Cost | 额外 rollouts、tokens、latency、clean update rejection |

### 7.8 统计方案

- 预注册 primary endpoint：VPA 联合事件，而不是事后挑最高 ASR；
- 对比例报告 Wilson 或 bootstrap 95% CI；
- 对同一任务的 clean/poison 结果使用 paired permutation 或 bootstrap；
- 用 mixed-effects logistic regression，把 task、seed、model 作为随机效应，poison rate、gate、trigger 作为固定效应；
- 多 domain/trigger 比较做 Holm correction；
- clean utility 用 non-inferiority test，预先设定 (\delta)，例如 2 个百分点；
- selection set 只能用于在线 gate，最终 clean utility 和安全结论必须使用锁定的 test/security set；
- 对 repeated adaptive selection queries 记录查询次数，并使用独立 final confirmation set，避免把 selection overfitting 当攻击成功。

### 7.9 Go / No-Go 标准

以下是项目决策阈值，不是普适安全标准：

**Go**：在 (\epsilon\le2\%\) 时，至少两个 domain、两个 target model 上 VPA ≥30%；trigger ASR ≥50%；clean utility 的 95% CI 下界高于 −2pp；所有感染版本均通过原生 strict gate；target-only 不能过门；至少经过 3 个 clean cycles 仍有显著 persistence。

**Pivot**：攻击只在同源 Sleep val 成功、换 trusted val 即消失。此时论文应转向“validator co-provenance / evidence-to-rubric laundering”，不要坚持一般性 held-out-gate bypass。

**No-Go**：只能靠直接恶意指令、手工修改 skill、污染 validation set、关闭 gate 或大比例 poison；target edit 单独也能过门；clean utility 明显下降；结果无法跨 seed 复现。此时它只是既有 prompt/memory poisoning 的 SkillOpt 实例化。

## 8. 防御：Atomic Counterfactual Patch Gate

### 8.1 设计原则

原生 gate 保留，因为它确实阻止很多效用回退；但 acceptance 不能再等于“所有 edits 获得授权”。推荐流程：

1. **Parse and dependency grouping**：把 candidate 拆成原子 edit；确有依赖的 edits 组成最小 dependency group；
2. **Provenance support**：记录每条 edit 由哪些 trajectories、verifier signals 和 trust domains 支持；
3. **Whole-candidate utility gate**：先保留原生总体效用检查；
4. **Counterfactual attribution**：测试 edit-only、leave-one-out，必要时对可疑小组做 sampled Shapley 或 delta debugging；
5. **Semantic diff → probe generation**：从 MUST/NEVER/ONLY、权限、工具、数据来源、条件分支等行为变化自动生成边界反例；
6. **Independent safety gate**：在预注册 policy invariants 和 trigger probes 上检查 unauthorized behavior；
7. **Trust-domain separation**：security validator、clean utility validator 与 optimizer evidence 分离；
8. **Staged admission**：展示每条 edit 的支持证据、效用边际和安全探针结果；
9. **Atomic state commit**：skill、memory、archive、meta、history 一起版本化和签名；
10. **Full-state rollback**：恢复整个 (Z_t)，而不是只复制回 `SKILL.md`。

伪决策规则可以写为：

\[
\mathrm{Accept}(\Delta)=
[\Delta U_V(\Delta)>\tau]
\land [\forall g\in\mathcal G(\Delta),\;\mathrm{Support}(g)\ge\kappa]
\land [R_Q(S\oplus\Delta)\le\rho],
\]

其中 (\mathcal G(\Delta)) 是原子或依赖 edit groups，(Q) 是独立安全 probe set。

### 8.2 需要承认的代价

- 完全 (2^k) subset testing 成本指数增长；实际应先做 leave-one-out、依赖聚类和风险优先采样；
- 某些 edits 只有组合后才有正效用，强制每条单独提升会误杀正常协同；因此应验证“数据支持与安全”，而不是要求每条 edit 都独立提升；
- 自动 probe generator 也可能漏掉 off-support trigger；独立人工 policy suite 和 runtime least privilege 仍必要；
- provenance 相似度不是安全证明，SkillSec-Eval 的高 FPR 已显示过紧 semantic consistency 会阻碍正常更新；
- full-state rollback 提高工程复杂度，但没有它就无法对持续学习系统给出可靠恢复语义。

### 8.3 防御实验

比较：native aggregate gate、per-edit utility gate、leave-one-out gate、sampled-Shapley gate、完整 Atomic Counterfactual Patch Gate。报告安全收益与 clean update acceptance、token/latency 成本、协同 edit 误拒率。最有价值的结果不是把 ASR 降到一个漂亮数字，而是展示哪一层分别消除了：bundle hitchhiking、同源 validator contamination、archive reinfection 和 runtime unauthorized action。

## 9. Reviewer 预演

| 可能质疑 | 必须准备的回答/证据 |
|---|---|
| “这只是 OEP/MPBench 换了 victim。” | 原生 strict held-out gate、每版本 utility uplift、四组 edit 反事实、target-only reject |
| “Trigger 不在 validation，当然测不到。” | 把有限 gate 的 off-support 边界形式化；使用 trigger family；比较自动生成 safety probes 与 trusted security set |
| “这只是 prompt injection。” | 证明攻击者离场后，行为由 accepted skill 独立触发；清除当轮 prompt/context 后仍存在 |
| “默认有人审，不会部署。” | 分开 staged/deployed；把 human review 作为 defense 做 blinded study；不把 gate pass 冒充 deployment |
| “Sleep 的 val 也被污染，不是 bypass。” | 原始 SkillOpt train-only poison + clean hidden val 为主结果；Sleep 同源 val 作为第二机制 |
| “这是 SkillOpt 的实现 bug。” | EvoSkill 或第二 victim；把机制抽象为 composite-candidate admission；发布最小通用 harness |
| “恶意 edit 太明显。” | 局部正确、dual-use、语义约束的 evidence；human detection 和 semantic-diff stealth 指标 |
| “效用提升只是 selection overfit。” | 锁定 final clean test、独立 confirmation set、多 seed、统计 CI |
| “逐 edit gate 就解决了。” | 测试协同 edits、成本和误拒；提出 dependency-aware counterfactual gate，而非朴素单条门 |
| “攻击者能力不现实。” | 明确分 train-record、harvested text、environment-via-echo 三层；不越过真实 ingestion path |

## 10. 论文贡献与写作骨架

### 10.1 建议贡献

1. **Problem and taxonomy**：定义 validation-passing artifact poisoning，区分 proposal、artifact、deployment infection；
2. **Mechanism**：发现并实证 candidate-level utility attribution 与 edit-level authorization 的错位；
3. **Attack**：提出 task-valid, evidence-only beneficial-edit payload hitchhiking；
4. **Evaluation**：跨原始 SkillOpt、Sleep 和第二个 gated victim，测 gate pass、clean utility、persistence、transfer、rollback；
5. **Defense**：Atomic Counterfactual Patch Gate 与 full-state provenance/rollback。

### 10.2 Introduction 的论证顺序

1. Skill evolution 把运行经验编译为可复用策略；
2. 现有系统用 held-out utility gate 防止性能回退；
3. 性能门测试的是 candidate 整体，却授权其中每条 instruction；
4. 低可信 evidence 因而可能被“洗白”为高可信 skill；
5. 现有工作研究 memory poisoning、procedure insertion 或 direct updates，但没有隔离 utility-gated optimizer 的这一 transition；
6. 给出攻击、反事实证据、持久性和防御结果。

### 10.3 一个可用的摘要草案

> Self-evolving agents increasingly compile task experience into persistent natural-language skills. Recent systems guard this process by accepting a candidate skill only when it improves held-out task utility. We show that this validation discipline leaves a distinct authorization gap: utility is attributed to a composite candidate, while every instruction inside the accepted artifact receives persistent authority. We introduce GatePoison, an evidence-only attack in which a small number of task-valid interactions induce a text-space optimizer to bundle a useful edit with an unsupported trigger-conditioned edit. The useful edit supplies the validation gain, allowing the composite artifact to pass the native gate without reducing clean utility. Through edit-level counterfactuals, we distinguish this mechanism from direct skill injection, memory poisoning, and ordinary prompt injection. We evaluate persistence across clean evolution, rollback, models, and harnesses, and propose an atomic counterfactual patch gate that combines edit attribution, independent safety probes, provenance, and full-state rollback.

摘要中的实证句必须等实验完成后再填具体数字；当前不能提前写成已证实的真实模型攻击。

## 11. 建议执行顺序

1. 冻结 SkillOpt v0.1.0、Sleep v0.2.0 和一个外部 victim；保存 manifests；
2. 把本次最小复现整理成公开单元测试，确认 bundle gate 事实；
3. 先在一个确定性 spreadsheet sandbox 完成四组反事实；
4. 构造 locally-valid poison generator 和真实 optimizer 输出判定器；
5. 只做 1%、2%、5% 三档、两个 models、五个 seeds 的 pilot；
6. 达到 Go 标准后再扩到三域、三模型、十 seeds；
7. 同步实现 native/leave-one-out/atomic 三个 gate defense；
8. 最后做 Sleep manual-review、recall/rollback 和 cross-harness 扩展。

如果时间只够一篇短论文，保留：SkillOpt 强 threat model、bundle hitchhiking、四组因果消融、一个外部 victim、Atomic Counterfactual Gate。把 Sleep archive、human review、跨 harness 放附录或后续工作。

## 12. 最终判断

这个方向不是“没人做”，但仍有一个清晰、机制导向、可证伪的空位。最值得做的不是再证明 skill 会被污染，而是证明：

> **一个只检查 aggregate task utility 的更新门，可能把低可信任务证据转换成同时“更有用”且“夹带未授权行为”的持久策略制品。**

目前已有充分源码证据支持这个问题成立，也有最小复现证明 bundle-level hitchhiking 在实现层可达；尚缺的是低 poison budget、真实 optimizer、clean hidden validation 下的稳定实证。只要主实验严格坚持 evidence-only、task-valid、native-gate-passing、clean-noninferior 和 edit-level counterfactual 五个条件，这个课题就能与 OEP、MPBench、memory poisoning 和 direct skill injection 清楚区分。

反过来，如果实验只能在同源 val、关闭 gate、直接恶意 instruction 或手工修改 skill 时成功，就应及时降级为系统审计或 validator-contamination 论文，而不要坚持更强的 GatePoison novelty。

## 来源与检索边界

本报告优先使用论文官方页面、arXiv/OpenReview/会议论文页、Microsoft 官方项目页和固定 commit 源码。检索覆盖 self-evolving agent safety、experience/memory poisoning、persistent injection、skill supply chain、skill evolution 与 validation-gated skill optimizers。2026-07 的相关工作变化很快，因此“未发现直接工作”只支持审慎的 `to our knowledge` 主张；投稿前应以标题、摘要、引用网络和最新会议接收列表再做一次更新检索。
