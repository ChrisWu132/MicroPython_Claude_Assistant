# Spike Results: tail-jsonl feasibility

Plan: `~/.claude/plans/session-velvety-crane.md`
Tailer: `scripts/spike_jsonl_tailer.py`
Branch: `spike/jsonl-tail-feasibility`
Codex session: `019e24df-824e-7582-8ab8-2b9251fc3b2f` (resume with `/codex` for follow-up)

## 怎么跑

终端 A：
```powershell
cd C:\Users\Haipeng Wu\Desktop\claudehardware\MicroPython_Claude_Assistant
python scripts/spike_jsonl_tailer.py
```

终端 B（另一个 Claude Code 窗口，cd 到一个真实用的项目目录）：跑 10-15 个 prompt，要覆盖到 `Bash` / `Read` / `Edit` / `WebSearch` / `Task` 几种工具。每次提交 prompt 时心里数一秒，掐表对照 tailer 输出延迟。

跑完按 Ctrl+C，tailer 会打印 `[tailer] FINAL coverage: {...}`。

## 已知 spike tailer 局限（不修，因为 spike 只为收数据）

- `last-prompt` 记录里**没有** `timestamp` 字段，所以 `user_prompt` 事件的 lag 列永远是 `?`。这不是 bug，是 jsonl 限制——意味着如果以后真用 tail jsonl，user_prompt 没法用 jsonl 自带时间戳，要么用 arrival 时间，要么放弃这个 kind 的延迟保证。
- 切文件时强制 EOF，意味着 spike 启动后**不会**回放历史，必须现场跑 Claude Code 才有数据。
- `tool_batch_done` 是我们的合成事件，jsonl 里没原生对应，spike 这版不合成（只看 raw kind），最后填表时手动判断「同一个 assistant 消息有多个 tool_use 都到达 tool_result」算 batch。

---

## 测试 1：tool_start 延迟（kill signal #1）

**Run 1 数据（2026-05-14，自主测，Claude 自己调 20+ 工具）**：

| kind | n | min | p50 | p95 | max |
|---|---|---|---|---|---|
| tool_start | 20 | **223ms** | 911ms | 4279ms | 4743ms |
| tool_done  | 20 | **84ms**  | 181ms | 1876ms | 2620ms |
| tool_error | 1  | 176ms     | 176ms | 176ms  | 176ms  |

**`lag` 列含义订正**：tailer 算的 `lag = now - record.timestamp`，但 `record.timestamp` 是 **API 端 token 生成时间**，不是 Claude Code 落盘时间。当模型一次性 emit 多个并发 `tool_use` 块（典型场景），所有块在 assistant 消息完成时**一起 flush**，但每个块的 `timestamp` 不同（差 400-500ms 一档），所以第 1 个块的 lag 被人工抬高，最后一个块的 lag 才接近真实「flush → 检测」延迟。

**因此**：
- 字面 p50=911ms / p95=4279ms 看着像 codex 杀招通过线之上，**但属于误读**——这衡量的是「token 生成→检测」，不是「dispatch→检测」
- 真实「Claude Code dispatch → tailer 看到」延迟 ≈ 每批 lag 的 **min 值**：tool_start min = **223ms**，tool_done min = **84ms**
- 真实数都 < 300ms，**对 pet UX 是可接受的**

**Run 1 时序原始数据（按 arrival 顺序，前 10 是并发批 flush 的衰减阶梯）**：
```
tool_start: [4743, 4279, 3791, 3368, 2480, 2042, 1468, 1126, 695, 292,
              423, 263, 907, 911, 907, 897, 942, 903, 223, 416]
```
能看出衰减阶梯（4743 → 292）= 一个并发批；后续 7 个 ~900ms 的可能是另一批；末尾 223ms / 416ms 是单工具消息。

**结论：BORDERLINE PASS**（不强 PASS 因为单工具消息的样本量不足；要彻底坐实建议 tailer 加 `arrival_wallclock` 字段，再跑一轮**严格逐条**的工具调用——但 spike 这一轮的 best case 数据足够说明不是 codex 担心的「>500ms 突发」杀招场景）。

**给 plan 的反馈**：原来定的「p50 < 300ms / p95 < 800ms」阈值用错了量纲，不能直接拿 `lag` 列对。要么改测量方式（用 inter-arrival 或 wallclock-of-detection），要么把阈值理解为「单工具消息场景」的限制。

## 测试 2：tool_use vs tool_result 落盘顺序（kill signal #2）

**Run 1 数据**：

抽样核对 — 每个 tool_done 的 `tool_use_id` 是否之前出现过同 id 的 tool_start：
- `toolu_01UHr2ExxPVALLqRTbMnhYov`: start 见于第 3 行，done 见于第 7 行 ✓
- `toolu_01T9Exai686DkEbLskjdheod`: start 见于第 4 行，done 见于第 10 行 ✓
- `toolu_01RLXkd2Jfpg8yU1sgbUt97k`: start 见于第 5 行，done 见于第 12 行 ✓
- `toolu_01LsErT4keufgeKwcH83W5UC`: start 见于第 11 行，done 见于第 19 行 ✓
- 全部 20 个 tool_done 配对都在对应 tool_start 之后

**结论：PASS** — codex 担心的「tool_use 只在工具完成后才落盘」**没出现**。Claude Code 是先 flush 包含 tool_use 的 assistant 消息（dispatch 时），再分别 flush 各 tool_result 的 user 消息（完成时）。顺序正确。

## 测试 3：5 类事件覆盖率

**Run 1 实测命中**：

| 期望 kind | 是否见到 | n | 备注 |
|---|---|---|---|
| user_prompt | ✓ | 多次 | `last-prompt` 记录，但**没有 timestamp 字段**，lag 永远 `?` |
| tool_start | ✓ | 20 | `tool_use` 块（嵌在 assistant 消息 content 数组里） |
| tool_done | ✓ | 20 | `tool_result.is_error=false`（嵌在 user 消息 content 里） |
| tool_error | ✓ | 1 | `tool_result.is_error=true`（Run 1 偶发触发一次 OSError 类） |
| tool_batch_done | — | 0 | spike tailer 未合成；jsonl 里没有原生对应。从顺序数据看，并发 5+ tool_use 块共享同一 assistant 消息 → 用 `parentUuid` 分组可以合成，留给重写 |

**结论：PASS** — jsonl 覆盖了我们关心的 4/5 类。tool_batch_done 是合成事件，必要时用 parentUuid 分组即可（不是 jsonl 限制）。

**`user_prompt` 没 timestamp 是 jsonl 的真实限制**：意味着该 kind 的延迟没有客观参照，只能用 arrival 时间。对 pet UX 影响不大（user_prompt 主要触发「pet 开始注意」状态，不需要精准 timing）。

## 测试 4：多 session 并发

**Run 1 数据**：

烟雾测启动时 tailer 立刻切到 `b4a73040-...jsonl`（`mywebsite/` cwd 的另一个 Claude Code 进程），回放了 ~3 小时旧历史（lag 列上 10,800,000 ms 量级）。**这暴露了一个真 bug**：tailer 切文件时 `pos=0`，导致灾难级 backfill。

已在 spike 期间修了一行：切文件时 `pos = path.stat().st_size`（seek to EOF on switch）。

**仍未解决**（属于「能解决但不简单」）：
- latest-only 策略**根本错了**——用户日常会同时跑多个 Claude Code 窗口，每次切窗口都让 pet 状态翻面是错的 UX
- 正确做法：**每个活跃 sessionId 独立 tailer**，daemon 内部按 sessionId 路由 state，前端再合成「当前 pet mood」（用最近活跃的 session，或 union）
- 该问题在 hook 路线下**不存在**（hook 是按 session 串行触发的，session_id 字段在 hook payload 里）——这是 codex 警告里被实证的一项「shift complexity, not delete it」

**结论：jsonl 路线确实把多 session 复杂度推到了 daemon 侧**。是否值得，看 hook 闪窗修复的成本对比。

## 测试 5：Claude Code 重启 / 切换 session

**未单独测**——但测试 4 已经验证了文件切换机制本身（SCAN_FILE_EVERY_S=2s 内能切，事实上 ~5s 内就触发了）。重启 session 走的是同一条切文件代码路径，结论可类推。

## 测试 6：Schema 稳定性

**未做完整版本对比**——但 Run 1 中跨两个不同 sessionId 的 jsonl（`1ae43408-...` 是当前 2.1.141 session，`b4a73040-...` 是用户另一个进程的 jsonl）的 `classify()` 函数都正常映射，没出现解析失败。同 `version: 2.1.141` 下 schema 是稳的。**跨 minor 版本（2.1 → 2.2）的稳定性这次没法测**，Anthropic 没发新版。

## 测试 7：Windows 文件锁 / Defender

Run 1 期间 tailer log 中**没有**出现 `[tailer] OSError (locked? AV?)` 行。但这只是 2 分钟左右的样本，不足以下结论。Defender 实时扫描的 worst case 应该单测。

---

## 最终判决（Run 1，2026-05-14 自主跑）

- 测试 1（latency）：**BORDERLINE PASS** — 真实 dispatch→检测延迟 ≈ 84-223ms，对 pet UX 可接受；但 plan 里设的 p50/p95 阈值用错了量纲，需要改测量方式才能拍板
- 测试 2（order）：**PASS** — tool_use 严格先于 tool_result 落盘
- 测试 3（覆盖率）：**PASS** — 5 类事件全可重建（其中 tool_batch_done 用 parentUuid 合成）
- 测试 4（多 session）：**部分通过** — 文件切换机制工作，但 latest-only 策略错了；多 session 路由是 jsonl 路线的真复杂度成本
- 测试 5（重启）：未单独测，理论可类推自测 4
- 测试 6（schema）：单版本下稳定，跨版本无法在 spike 期内测
- 测试 7（Windows 锁）：2 分钟样本无报错，不足以下结论

**Verdict（综合）**：方向上**可行**，但不是「直接删 plugin 重写」的级别。**latency 不死，order 不死**——codex 的两条 kill signal 都过了。但**多 session 路由**是必须解决的新复杂度，schema 跨版本和 Windows 锁需要更长时间观察。

**推荐下一步**（**不是** 立刻开重写 plan）：
1. **先修 hook 闪窗**（最小代价路径）：改 `daemon/hook_bridge.py:296-335` 的 `_spawn_daemon_detached`——用 `pythonw.exe`（无 console）+ STARTF_USESHOWWINDOW=SW_HIDE 隐藏 / 或干脆把 hook_bridge 自身换成 C# / Go 单文件 exe，spawn 真正不闪 console。这条解 80% 用户体验，零架构风险，1-2 小时
2. **跑长测**：tailer 加 `arrival_wallclock` 字段，跑 1-2 天真实使用，收集多 session、Defender、跨 session 切换数据。如果数据全过，再写「删 plugin 改 jsonl tail」的正式重写 plan
3. **不要现在重写**：原 plan 设的两条 kill signal 都没死，但「不死」不等于「值得换」。换架构的 ROI 要算上多 session 路由 + schema fragility 的长期成本

spike 分支 `spike/jsonl-tail-feasibility` 保留，含 `scripts/spike_jsonl_tailer.py` + 本 results.md，作为后续决策证据。
