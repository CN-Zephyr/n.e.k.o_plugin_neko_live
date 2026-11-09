# 宿主实际回复读取

## 职责与边界

`adapters/host_reply_reader.py` 按需读取宿主已经生成的直播回复，供下一次输出合约避开最近说法。它不改变直播事件选择、开口频率、回复路由或模型输出，也不把文本生成视为音频播放完成。

插件只接纳宿主能够证明本轮触发来源全部为本插件当前直播会话已提交请求的记录。当前角色相同、时间接近、`turn_type` 为主动回复或出现直播关键词，都不能替代来源证明。触发来源混有用户输入、其他插件或未知项的同一轮回复必须整条丢弃，不能只选其中一个匹配来源。

该能力没有新配置、UI、公开 action、store 或后台 worker。读取失败或宿主不支持来源契约时，继续使用原有插件侧防复读材料，真实输出仍通过原来的 Pipeline、Safety Guard 和 Dispatcher。

## 来源契约

每次真实直播提交在局部传输 metadata 的 `task_id` 中携带随机、无业务含义的请求标识。标识不得包含房间、UID、昵称或正文。只有提交检查成功后，该标识才进入当前会话的有界内存集合；提交回执仍不表示模型已经回复。

宿主在 conversation record 的 metadata 中提供：

```json
{
  "event_id": "opaque-host-event-id",
  "role": "cat",
  "reply_provenance": {
    "version": 1,
    "complete": true,
    "sources": [
      {
        "kind": "plugin",
        "plugin_id": "neko_live",
        "request_id": "opaque-submitted-task-id"
      }
    ]
  }
}
```

`plugin_id` 必须由宿主从可信调用身份确定，不能信任插件自行声明的来源。`request_id` 对应宿主接收的 callback `task_id`。一个生成轮可以包含多个请求；插件必须验证 `sources` 中的**每一项**均属于当前会话已成功提交的 `neko_live` 请求。

接纳条件是：记录角色与当前实际发送目标一致，`metadata.role` 为 `cat`，`turn_type` 为 `assistant_message` 或 `proactive_reply`，存在有效 `metadata.event_id`，来源版本严格为整数 `1`，`complete` 严格为 `true`，非空来源列表最多 16 项且每项全部通过校验。来源标识最多 128 字符。`user`、`unknown`、其他插件、未知请求、其他轮次类型、字段缺失、畸形值、来源截断或不完整记录均跳过。

去重使用宿主 `metadata.event_id`。不得使用 `conversation_id` 去重：它可能是轮次标识，不能保证每条发布记录唯一。时间字段只用于排序，不能赋予来源可信度。该契约只证明本轮触发来源属于本插件的一组请求，不证明生成内容完全不受历史私聊或其他既有模型上下文影响，也不证明它对应其中某一位观众；不得据此写入观众个人记忆。

## Pipeline 与安全检查

```text
已完成路由的 InteractionRequest
  -> 请求及当前会话检查
  -> 按需读取宿主回复（短超时 / 共享同范围在途查询）
  -> await 后重新验证 scope、停止状态、目标与 dry-run
  -> 原有 Safety Guard before-output（只消费一次冷却）
  -> NekoDispatcher.push_roast()
  -> 合并近期负例并执行原有提示词预算检查
  -> plugin.push_message()
  -> 提交成功后登记本次随机请求标识
```

查询位于 `core/pipeline_dispatch.py` 的最终 `before_output` 之前。新增等待之后必须重新检查查询 epoch、session generation、listener operation、实际目标角色、停止状态和当前 dry-run 配置；无效请求沿用现有跳过路径。最终输出仍检查暂停、断开及队列失效，不能在已消费冷却之后新增查询等待，也不能通过重复消费冷却完成复查。

Dispatcher 在真正提交前不再增加异步查询。模块仍负责构造 `InteractionRequest.prompt_text`；Reader 只向 Dispatcher 提供独立的近期文本列表，不修改模块请求、观众档案或事件 payload。

## 范围、容量与生命周期

Reader 的有效范围绑定实际发送目标、直播 session generation 和 listener operation。目标使用当前直播会话冻结的 owner，普通 UI 切猫不改变已连接直播的归属；断开清场失败时旧 owner 可能保留用于重试，因此不能只靠角色名称判断旧查询是否仍有效。

同一范围按需最多每 2 秒发起一次 `bus.conversations.get(max_count=32, timeout=0.2)`。同一范围的并发调用共享一个在途 RPC；取消一个等待者不得误取消其他有效等待者。没有定时轮询、自动重试、watch 订阅或额外 LLM 调用。当前 SDK 的 conversation 查询结果不支持可靠的 `.watch()` 重放，因此本模块只使用 `get()`。

已提交请求集合最多保留 128 条，已见事件标识最多保留 64 条；文本缓存最多 6 条，每条只保留 24 字符的摘录，省略标记另计。已提交请求与文本缓存的保留期为 120 秒，按查询 / 提交时惰性清理，不新增定时器。`get()` 返回的原始记录只在本次调用内短暂读取和筛选，通过校验后立即收束成摘录，不保留原始记录或完整回复。达到容量时淘汰旧项；被淘汰或过期请求的迟到回复不再接纳。单次返回满 32 条仍只处理该页，不追加分页追赶或无界循环；高流量时允许漏掉旧回复，这是一项尽力而为的防复读提示。

`begin_live_session()`、`invalidate_live_session()`、房间 / 平台重连、真实目标变化和 dry-run 模式切换都会清空读取状态并使旧在途结果失效。停止入口在任何清理等待之前关闭 Reader；重新启动时重开空状态。旧 RPC 晚回不能填回新会话缓存，也不能恢复已失效的提交资格。dry-run 和明确不投递的请求不查询宿主。

## 防复读与数据边界

查询得到的文本与已有本地近期输出合并，实际宿主文本优先进入既有 `core/live_output_memory.py` 负例窗口。最终总量仍为最近 6 条，每条最多 24 字符后加省略标记，按现有规则去重；不得各保留 6 条导致预算翻倍。合并结果继续受既有单条提示词预算约束，相关预算规则以[提示词投送与预算](prompt_delivery.md)为准。

摘录会在截短前折叠所有空白，包括 Unicode 行分隔符，使每条负例保持单行。精简和展开两类合约均先渲染主规则，再把合并窗口写成独立的 `Avoid repeating` 行。可信 `Rules` 已含防复读约束时，预算收束器可以按整行省略负例。若仍需截短当前弹幕，Dispatcher 会先从原正文重建无宿主负例的候选，补齐原有输出合约和说话人边界，再按原有预算规则投递。此回退也适用于缺少防复读约束的 builder。

文本只用于当前发送的局部 text part。它不进入 `recent_results`、request metadata、Runtime Timeline、audit、logger、dashboard、UI、配置、观众记忆或磁盘。随机请求标识只用于传输和内存归属验证，不把宿主原始记录复制到公开投影。

查询缺能力、超时、异常或来源不合格时静默跳过，不把查询失败登记成输出失败，也不打印宿主返回正文或异常响应。原有 Safety Guard / Dispatcher Stage 与 Outcome 保持可见；本模块不增加带正文的诊断入口。运行态跳过原因的权威来源仍为 [Runtime Observability](../runtime-observability.md)。

## Decision Points

本阶段已选择以下方案；批准范围是以有界宿主查询改善当前防复读，不包含音频完成闭环、新 UI 设置或额外生成。

| 决策点 | 已选择方案与预算 | 替代方案与取舍 |
|---|---|---|
| 何时读取 | 有真实投递需求时按需查询；同范围最多每 2 秒 1 次 RPC，单次最多 32 条、200 ms 超时，并发共享在途请求 | 每次无条件查询增加延迟与 RPC；后台轮询增加常驻任务，本次不采用 |
| 如何认领回复 | 宿主完整来源证明，并校验全部来源均为本会话已提交请求；稳定 `event_id` 去重 | 按角色、时间或主动回复类型猜测可能把私聊和其他插件内容带入直播，禁止采用 |
| 内存与提示词 | 请求标识 128 条、事件标识 64 条，缓存只留 6 条 × 24 字符摘录（省略标记另计）；请求与摘录保留期 120 秒，惰性清理；最终复用总共 6 条 × 24 字符负例窗口 | 保存完整回复、历史或另加一组提示词会扩大隐私与 token 成本，本次不采用 |
| 接口与架构 | Reader、Dispatcher、pipeline 最终安全检查前准备步骤及既有 lifecycle 清理钩子；不新增依赖、timer、worker 或持久化 | 在最后安全检查之后等待会扩大状态竞态；通过重复扣冷却补救会改变输出节奏 |
| 兼容与回滚 | 旧宿主缺来源字段、查询失败或字段不合法时回到原有防复读行为；撤回 Reader 接入即可去除查询成本 | 不降级为宽松来源匹配，不把缺失字段视为默认可信，不为填满窗口持续追赶历史 |
| 验证与观测 | 验证来源、容量、并发、取消、跨会话晚回、最终门禁及无正文持久化；只沿用既有安全状态和输出结果 | 原始回复日志更易排错但会扩大隐私面，本次不增加；单测不替代真实模型效果验收 |

## 验证方法与限制

在宿主 `pyproject.toml` 所在目录运行：

```powershell
uv run pytest plugin/plugins/neko_live/tests/test_host_reply_reader.py plugin/plugins/neko_live/tests/test_host_reply_budget.py plugin/plugins/neko_live/tests/test_dispatcher_contracts.py plugin/plugins/neko_live/tests/test_live_session_isolation.py -q
uv run pytest plugin/plugins/neko_live/tests -q
uv run python -m plugin.neko_plugin_cli.cli check plugin/plugins/neko_live
```

Reader 测试应覆盖旧宿主无字段、严格版本 / complete 校验、来源混合、同轮多个本插件请求、未知请求、其他角色和轮次类型、稳定事件去重、排序与容量淘汰、缓存只保留短摘录、满页不追赶、超时和异常降级、同范围并发合并、取消与晚回。

集成测试应让查询等待期间发生断开 / 重连、房间 / 平台切换、stop、dry-run false → true → false、暂停与清队列，确认旧结果不能缓存或发送。普通 UI 切猫应保持直播冻结 owner。还需断言冷却只消费一次，dry-run / 不投递请求不查宿主，负例仍为总共 6 条 × 24 字符，查询文本没有进入公开 metadata、recent results、audit 或保存函数参数。

预算回归覆盖精简 / 展开合约的负例整行省略和实际 Dispatcher 在 1000 token 附近的回退。即使 builder 没有防复读规则，宿主负例也应先于当前弹幕被舍弃。弹幕自身超限时，继续按原有规则生成带标记的摘录，保留硬规则和原有字数限制。

这些命令是验证要求，不表示已经执行或通过。测试结果记录在交付或发布证据中；真实模型是否减少复读仍需现场验收。宿主生成文本可能在声音播放之前到达，缓存不用于判断播完、取消、错误或观众已经听到，也不参与音频完成驱动的调度。
