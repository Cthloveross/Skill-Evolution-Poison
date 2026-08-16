# Self-Evolving Agent Skill 污染：Related Work 与初步方向

## 核心判断

“Self-evolving skill 在更新过程中可能被污染”是一个值得研究的问题，但它已经不是完全空白。现有工作已经证明：agent 的长期记忆可以被污染，反思模块会把局部经验错误地泛化成长期规则，成功执行轨迹中的恶意步骤可以被蒸馏成 reusable skill，第三方 skill 文件和后续更新也可能携带恶意行为。

因此，不能把论文定位成“首次研究 self-evolving skill poisoning”或“首次研究 skill evolution security”。最有潜力的空位是一个更具体的过程：

> 攻击者只提供少量、任务上有效的交互经验；系统自己的 skill optimizer 根据这些经验生成新 skill；新 skill 通过原生性能验证，clean task 表现没有下降，但其中出现了未经授权的持久行为。

这个问题的重点不是“攻击者直接写了一个恶意 skill”，而是“低可信经验如何被系统自己的优化与验证流程升级成高可信 skill”。

## 1. Skill 自动演化

这一组工作回答的是：agent 怎样从执行经验中自动生成、修改和管理 skills。它们构成了我们的 victim landscape，也说明 skill 已经从静态 prompt 变成一种长期、可更新的 agent state。

[SkillOpt](https://arxiv.org/abs/2605.23904) 把自然语言 skill 文档当作冻结 agent 的可训练外部状态。独立 optimizer 读取 scored rollouts，提出 ADD、DELETE、REPLACE edits；候选只有在 held-out selection set 上严格提升才接受。它的突出特点是更新过程比较受控：有 edit budget、rejected-edit buffer、validation gate 和 best checkpoint。对我们的研究而言，SkillOpt 最重要的价值不是它“容易被攻击”，而是它已经有较强的性能验证。如果仍能产生被污染但通过验证的 skill，研究结论会比攻击一个无验证的反思系统更强。

[EvoSkill](https://arxiv.org/abs/2603.02766) 根据执行失败提出新 skill 或修改已有 skill，并使用 held-out validation 上的 Pareto frontier 保留有价值的 agent programs。它说明 validation-gated skill evolution 不只有 SkillOpt 一种实现，因此可以作为外部复现对象，帮助判断发现的是通用 admission problem，还是 SkillOpt 的特定实现问题。

[Trace2Skill](https://arxiv.org/abs/2603.25158) 从大量执行轨迹中提取 trajectory-local lessons，再通过并行分析和分层 consolidation 形成统一 skill。它更强调从广泛经验中归纳可迁移 procedure，而不是逐次使用严格的性能门。对我们而言，它适合作为“trajectory-to-skill 但没有强 gate”的比较对象。

其他系统进一步把 skill evolution 扩展到 library 和 lifecycle。[SkillFoundry](https://arxiv.org/abs/2604.03964) 从代码、文档、API、notebook 和论文等异构资源中生成带 provenance 和 tests 的科学 skills；[SkillForge](https://arxiv.org/abs/2604.08618) 利用历史技术支持工单和失败反馈持续修复 domain skills；[EvoSkills](https://arxiv.org/abs/2604.01687) 让 skill generator 与 surrogate verifier 共同演化；[SkillOS](https://arxiv.org/abs/2605.06614) 训练 skill curator，根据任务流中的延迟反馈更新外部 SkillRepo；[MUSE-Autoskill](https://arxiv.org/abs/2605.27366) 则把创建、存储、选择、评估和修改放入统一 lifecycle。

这些工作的共同结论是：**未来 agent 会越来越多地把运行经验写入长期 skill state。** 但其主要目标仍是任务成功率、泛化、复用和效率。它们通常没有研究恶意 evidence、更新来源可信度，以及“性能验证通过是否足以授权所有新增行为”。这正是安全研究可以切入的地方。

## 2. 自进化安全与经验污染

这一组工作与我们的想法最接近，因为它们直接研究 agent 如何从经验中学坏，而不要求攻击者直接修改模型权重或 memory database。

[Your Agent May Misevolve](https://arxiv.org/abs/2509.26354) 系统研究 model、memory、tool 和 workflow 四类自演化路径中的非预期风险。它表明，即使没有明确攻击者，持续演化也可能造成安全退化、错误工具复用或行为偏移。这篇工作已经否定了“self-evolving agent security 没人研究”的宽泛说法。不过它主要研究自然发生的 misevolution，而不是有界攻击者如何操纵一次具体更新。

[On Safety Risks in Experience-Driven Self-Evolving Agents](https://aclanthology.org/2026.findings-acl.2091/) 进一步发现，即使积累的经验全部来自 benign tasks，也可能因为经验主要教 agent“如何执行”，而没有教它“何时拒绝”，导致 agent 在高风险场景更倾向于行动。加入 refusal experience 能缓解问题，但又会产生 over-refusal。这说明 task utility 与 safety 并不自动一致，但该工作仍属于非对抗性安全退化，不是 poisoning attack。

最直接的 prior 是 [OEP](https://arxiv.org/abs/2605.18930)。OEP 的攻击者只提交局部正确、语义上合理、但不可迁移的 edge cases，并配合严重但看似合理的后果描述。Reflective agent 在 consolidation 时可能把这些特殊经验过度泛化成高优先级长期规则。OEP 不需要直接修改 system prompt 或 memory database，并且已经在包括 OpenClaw-based skill module 在内的自进化设置中测试。

OEP 与我们高度重叠的部分是：低权限、局部正确经验、反思或 consolidation、持久规则和攻击者离场后的长期影响。区别在于，OEP 没有研究 SkillOpt 式的严格 held-out utility gate，也没有要求每个被污染版本都在独立验证任务上表现更好。因此我们的工作不能只复现“局部经验被过度泛化”；必须证明污染行为是在原生性能门仍然通过的条件下进入 skill。

## 3. Memory poisoning 与持久攻击

Agent memory security 已经形成较成熟的攻击链：低可信输入进入持久 memory，后来被重新检索，并在与原始攻击不同的任务或 session 中影响行为。这些工作为 threat model、persistence 指标和 trigger 设计提供了重要基础。

[AgentPoison](https://proceedings.neurips.cc/paper_files/paper/2024/hash/eb113910e9c3f6242541c1652e30dfd6-Abstract-Conference.html) 通过向长期 memory 或 RAG knowledge base 注入极少量带 trigger 的恶意 demonstrations，让后续相关查询检索到攻击记录，同时尽量保持 benign performance。它建立了“低 poison rate、triggered behavior、clean utility 基本不受影响”这一经典评估范式。但它假设攻击者能够向 memory 或知识库加入记录，不涉及系统自己生成新 skill。

[MINJA](https://proceedings.neurips.cc/paper_files/paper/2025/hash/42a97bbd9844d2bf68596730af80bcdf-Abstract-Conference.html) 把攻击者权限进一步降低到 query-only。攻击者通过正常查询和观察输出，让 agent 自己生成并存储未来可检索的恶意 reasoning record。它与我们的共同点是：攻击者不直接修改持久状态，而是操纵系统的正常写入过程。区别仍然在于，MINJA 写入的是 memory record，没有经过 skill optimizer 和独立 utility gate。

[Zombie Agents](https://arxiv.org/abs/2602.15654) 展示了更现实的环境攻击：agent 在正常任务中读取攻击者控制的网页，恶意内容经过 memory evolution 被存储，并在后续 session 中触发未授权工具行为。论文还研究了如何使 payload 抵抗 sliding-window 截断和 retrieval relevance filtering。它已经覆盖“自进化系统把一次间接注入变成持久攻击”，所以 persistence 本身不能作为我们的主要 novelty。

[Hidden in Memory](https://arxiv.org/abs/2605.15338) 研究 sleeper memory poisoning：文档、网页或代码仓库中的恶意内容使 assistant 写入虚假的用户记忆，攻击在之后的会话中才激活。这篇工作说明“写入时看似无害、以后才触发”已经是 memory security 的已知问题。

[MPBench](https://arxiv.org/abs/2606.04329) 是另一篇必须正面讨论的工作。它系统整理四种 memory write channels、九类漏洞和六类攻击。其中 Skill-Procedure Insertion 会把含有对抗步骤的成功交互轨迹蒸馏成 reusable skill；论文在 HERMES 上报告了这一攻击，但相应写通道不适用于 OpenClaw。这已经直接覆盖“成功 transcript 中的恶意 procedure 被写成 skill”，因此不能再声称我们首次提出 transcript-to-skill poisoning。

我们与 memory poisoning 文献的真正区别应当是：持久 artifact 不是一条直接写入或检索的 memory record，而是 optimizer 归纳、编辑和验证之后产生的新 skill version；研究对象是这次从 evidence 到 accepted policy 的信任升级。

## 4. 直接 Skill 与供应链安全

这一组工作把 skill 当作同时包含自然语言指令、代码、工具权限和辅助资源的供应链制品。它们已经充分证明静态恶意 skill 很危险，但其攻击者通常可以直接控制 skill 文件、skill package 或正式 update。

[SkillJect](https://arxiv.org/abs/2602.14211) 自动修改 `SKILL.md` 并把具体行为隐藏在辅助脚本中，通过执行 trace 反馈持续优化诱导文本。这说明 skill-based prompt injection 可以自动化且具有较强隐蔽性。不过攻击者一开始就拥有 skill artifact 的编辑权，它更适合作为 direct static-skill baseline，而不是我们的 threat model。

[Under the Hood of SKILL.md](https://arxiv.org/abs/2605.11418) 研究 skill registry 中的 discovery、selection 和 governance。论文表明 `SKILL.md` 不是被动说明文档，其 description 和 instructions 会影响 skill 是否被检索、被 planner 选择以及是否通过治理过滤。它关注的是第三方 skill 如何进入和影响生态，不是系统怎样从日常经验生成新版本。

[MalSkillBench](https://arxiv.org/abs/2606.07131) 构建了大规模、运行时验证的恶意 skill benchmark，覆盖 prompt injection、code injection 和 agent-control attacks。结果显示，只检查代码或只检查 prompt 都不够，检测器需要同时理解 skill intent、instructions 与运行时代码。它适合为我们的 target behavior 和 defense evaluation 提供分类体系，但不涉及 self-evolution。

[SkillGuard](https://arxiv.org/abs/2606.03024) 把 skill 视为带权限的可执行 artifact，通过 manifests、deny-by-default、runtime access control 和行为监控限制上下文影响与实际副作用。它提醒我们，即使更新 gate 漏掉了目标 edit，runtime least privilege 仍可以作为最后防线。

[SkillSec-Eval](https://arxiv.org/abs/2607.13987) 是与“skill evolution security”最直接相关的工作。它把 repository admission、retrieval、planner selection、execution 和 evolution 放在统一生命周期中，并测试 permission escalation、instruction injection、tool substitution、dependency compromise 和 publisher spoofing。其 evolution 攻击主要是攻击者直接提供恶意 update；防御则把每次 update 当作重新 admission，检查 provenance、依赖和语义一致性。

SkillSec-Eval 意味着我们不能声称首次研究 skill update security。我们的区别必须放在攻击入口：攻击者不控制 update，也不直接修改 skill，而是只控制普通任务 evidence；恶意行为由 victim optimizer 自己生成，并通过以任务性能为目标的原生 gate。

## 5. 文献综合

| 研究线 | 攻击者主要控制什么 | 被污染的持久对象 | 是否经过性能 gate | 与我们的关系 |
|---|---|---|---|---|
| AgentPoison、MINJA | Memory/KB 写入或普通 query | Memory record | 否 | 提供低权限与低 poison-rate 基线 |
| Zombie Agents、Hidden in Memory | 网页、文档等外部内容 | Persistent memory | 否 | 提供间接感染与跨 session persistence |
| OEP | 局部正确经验 | 反思生成的长期规则 | 否 | 最接近 experience poisoning，但没有 utility gate |
| MPBench | 不可信输入或成功轨迹 | Memory 或 reusable procedure | 否 | 已覆盖 transcript-to-skill，必须正面区分 |
| SkillJect、MalSkillBench | 直接控制 skill/package | 静态恶意 skill | 不适用 | Direct artifact baseline 与攻击分类 |
| SkillSec-Eval | 直接控制 skill update | 新 skill version | Security admission | 已覆盖 update security，但攻击权限更高 |
| SkillOpt、EvoSkill | Task trajectories 与 optimizer feedback | 自动生成的新 skill | 是 | 我们要研究的核心 victim |

现有工作之间留下的空位可以概括为：**memory 文献研究了低权限输入和持久性，skill security 文献研究了高权限恶意 artifact，skill evolution 文献研究了如何通过反馈提高性能；但较少工作研究低权限 evidence 如何经过一个性能优化器和 admission gate，变成新的高权限 skill artifact。**

## 6. 初步研究方向

### 研究问题

建议把问题写成：

> 少量、任务上有效的交互经验，能否诱导 validation-gated skill optimizer 生成一个性能提升、通过原生 gate、但包含休眠目标行为的持久 skill？

这里的关键词是：低权限 evidence、任务有效、系统自己生成、原生 gate 通过、clean utility 保持、持久行为。

### 攻击设定

主实验采用较强约束：

- 攻击者不能直接修改 skill、optimizer、gate、verifier 或 validation set；
- 攻击者只能贡献少量普通任务或 transcript；
- 这些任务在当前上下文中必须能正确完成，不能只是明显恶意或失败样本；
- 新 skill 必须通过系统原生性能 gate；
- 独立 clean test 上的任务性能不能明显下降；
- 目标行为只在另外设计的 trigger tasks 上激活；
- 攻击者离开后，仍测量该行为能否跨后续 clean updates 保留。

### 主机制假设

SkillOpt 一次可以把多条 edits 合成一个 candidate，再对整个 candidate 打一次总体分。由此可以测试一个简单假设：

> 一条确实提高任务性能的 useful edit，可能把另一条在 validation tasks 上不产生影响的 target edit 一起带过 gate。

这可以称为 Beneficial-Edit Payload Hitchhiking。为了证明它，必须比较四个版本：原 skill、只加入 useful edit、只加入 target edit、同时加入两条 edits。理想结果是 target edit 单独不能过门，useful edit 单独能过门，两条绑定后一起通过，删除 target edit 后目标行为消失。

### 最小实验

第一阶段只做原始 SkillOpt。使用独立且攻击者不可见的 selection/test split，只污染 training evidence。这样能够排除“攻击者顺便污染了验证集”的解释。

第二阶段再做 SkillOpt-Sleep。攻击入口限定为真实 harvester 会收集的 transcript text，比较默认 transcript-derived validation 与独立 trusted validation。结果还要区分“进入 staged proposal”和“经过人工 adopt 后部署”，因为 Sleep 默认不是自动采用更新。

第三阶段选择 EvoSkill 或其他 validation-gated evolution system 做一个外部复现。如果攻击只在 SkillOpt 成功，论文更像实现审计；如果相同机制跨系统出现，才能支持更一般的结论。

核心指标保持简单：含目标行为的 proposal 比例、通过 gate 的比例、最终 skill 感染率、trigger attack success、clean utility 变化、经过多轮 clean evolution 后的持久率。

### 防御方向

初步防御不需要设计得过重，可以从三点开始：

1. 对 candidate 中每条 edit 做 leave-one-out 或单独检查，找出对任务效用没有贡献但改变行为边界的 edits；
2. 根据每条 edit 的语义变化自动生成安全边界测试，而不只运行原有 utility validation；
3. 记录 edit 的来源 evidence，并让 skill、memory、archive 和 optimizer state 一起版本化，支持完整 rollback。

### 论文定位

不应使用以下主张：

- 首次研究 self-evolving agent security；
- 首次研究 skill poisoning；
- 首次把 transcript 污染成 reusable skill；
- 首次研究 skill update security。

更安全的定位是：

> 我们研究一个尚未被充分覆盖的 lifecycle transition：低可信任务 evidence 如何在只关注性能的 validation gate 下，获得新生成 skill version 的高可信身份。

如果 pilot experiment 能稳定证明 useful edit 带 target edit 通过干净 held-out gate，这将是论文最强的机制贡献。如果攻击只在 SkillOpt-Sleep 的同源 validation 中成功，则应把方向调整为 transcript-derived validator contamination，而不要声称一般性的 gate-passing poisoning。
