# JSONL Tail 替代架构评估（前因后果）

> 状态：**暂存（不实施）**，等触发条件 / 2026-05-14 留档
> Spike 分支：`spike/jsonl-tail-feasibility`（保留作证据）
> Codex 会话：`019e24df-824e-7582-8ab8-2b9251fc3b2f`（`codex resume` 可续聊）
> 配套文件：`scripts/spike_jsonl_tailer.py` / `scripts/spike_jsonl_results.md`

---

## 1. 起因：为什么会考虑换掉 hook+TCP 桥

现有架构（v5）：

```
Claude Code
  → Hook 事件（UserPromptSubmit / PreToolUse / PostToolUse）
  → spawn python hook_bridge.py（每次 hook 都新 spawn 一个进程）
  → v2 envelope（TCP 127.0.0.1:57320）
  → ble_daemon.py（常驻进程，状态机）
  → BLE → ESP32
```

两个长期痛点：

1. **Windows 闪窗**：Claude Code 在 Windows 上 spawn `python.exe` 时不传 `CREATE_NO_WINDOW`，每次 hook 触发都会瞬闪一个 console 黑框；DETACHED_PROCESS / STARTF_USESHOWWINDOW 反复出 bug，是 V1 demo 实机交付里**第一时间被用户看见**的 UX 缺陷。
2. **装机摩擦**：plugin 装好后还得用户手动把 hook 配置写到 settings.json；hook bridge 自启逻辑（detect→spawn→TCP listen）三层每一层都有失败模式（端口占用、daemon 没起来、TCP 协议解析不对齐）。

## 2. 新思路：让 daemon 直接 tail Claude Code 自己写的 JSONL

Claude Code 把整段会话写到：

```
~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
```

append-only、一行一个 JSON 记录、顶层 `type` 字段直接覆盖到我们需要的全部 5 类事件：

| jsonl 顶层字段 | 对应 v2 envelope kind |
|---|---|
| `type:"last-prompt"` | `user_prompt` |
| `assistant.message.content[].type:"tool_use"` | `tool_start` |
| `user.message.content[].tool_result, is_error=false` | `tool_done` |
| `user.message.content[].tool_result, is_error=true` | `tool_error` |
| 同一 `parentUuid` 下多个 tool_use 都见到对应 tool_result | `tool_batch_done`（合成） |

**理论收益**：

- 不再 spawn 子进程 → **闪窗根除**（不依赖 pythonw / CREATE_NO_WINDOW 兜底）
- 不再依赖 settings.json hook 配置 → **装机一步到位**（用户只装 daemon）
- 不再依赖 TCP socket → 少一个失败面
- 不再写 hook 协议 → 协议漂移问题自动消失

理论代价（codex 在 spike 前就警告过）：

- jsonl 是事后写盘，不是事前触发 → 引入额外延迟
- 多 session 并发时 daemon 要自己路由
- Schema 是 Anthropic 内部协议，会跨版本飘
- Windows 文件锁 / AV 扫描会让 tail 不稳

## 3. Codex 裁决（spike 前 consult）

> Session `019e24df-824e-7582-8ab8-2b9251fc3b2f`
> Verdict: **viable for pet UI, not for correctness-critical integration**

Codex 给出两条 kill signal——任何一条不过就立即停手：

| # | Kill signal | 通过线 |
|---|---|---|
| 1 | tool_start 落盘延迟 | `dispatch → tailer 看到` < 300ms (p50) / < 800ms (p95) |
| 2 | tool_use 是否在 tool_result 之前落盘 | tool_use 行必须先于对应 tool_result 行 |

Codex 还点名了 5 个**不致命但要解**的复杂度：多 session 路由、Windows 文件锁、schema 漂移、startup backfill 风暴、partial line。这些是「能解决，但每条都让 daemon 复杂度涨一截」。

## 4. Spike 实测（Run 1，2026-05-14）

Spike tailer：`scripts/spike_jsonl_tailer.py`（129 行，纯 read-only，不接 BLE 不接 daemon）。

测试方法：Claude Code 自跑 ~20 个工具调用（Bash / Read / Edit / WebSearch / Task / Glob 等），tailer 实时打印每行的 kind 和 lag。

### 结果摘要

| codex 杀招 | 数据 | 判决 |
|---|---|---|
| #1 latency | 真实 dispatch→检测 ≈ **min 84-223ms**（每批 lag 的 min 值） | **BORDERLINE PASS** |
| #2 order | 20 对 tool_use/tool_result 全部顺序正确 | **PASS** |
| 5 类覆盖 | last-prompt / tool_use / tool_result(ok+err) / assistant 内嵌 tool_use 全见到，tool_batch_done 用 `parentUuid` 可合成 | **PASS** |
| 多 session | 自动切到 latest 文件，但 latest-only 策略**根本错**——用户日常多窗口，切窗口让 pet 翻面是错 UX | **部分通过**（暴露真复杂度） |
| Schema | 同一 `version` 下稳；跨 minor 版本未测 | 单版本 PASS |
| Windows 锁 | 2 分钟样本无报错 | 数据不足 |

### Spike 过程发现的真 bug

切文件时 `pos=0` 导致**灾难级 backfill**：tailer 切到用户另一个 Claude Code 进程的 jsonl 时，回放了 ~3 小时旧历史（lag 10,800,000 ms 量级）。spike 期间已修（切文件时 `pos = path.stat().st_size`，seek to EOF）。这条 bug 不是 jsonl 路线的硬伤，但说明**任何长期方案都要 fixture 跑全 backfill / restart 场景**。

### Lag 列量纲订正（重要）

Plan 原定阈值「p50 < 300ms / p95 < 800ms」**用错了量纲**：tailer 算的 `lag = now - record.timestamp`，而 `record.timestamp` 是 **API 端 token 生成时间**，不是 Claude Code 落盘时间。当模型一次性 emit 多个并发 `tool_use` 块，所有块在 assistant 消息完成时**一起 flush**，但每个块的 timestamp 不同（差 400-500ms 一档），所以字面 p50=911ms 是误读。

**真实 dispatch→检测延迟**取每批 lag 的 **min 值**，结果 84-223ms，对 pet UX 完全可接受。

未来重新跑这套测试要做的事：tailer 加 `arrival_wallclock` 字段，由 daemon 自己打时间戳，再跑严格逐条工具调用样本。

## 5. 当前决策（2026-05-14）

**不重写**，**先修闪窗**（最小代价路径）。

### 决策理由

| 维度 | 重写 jsonl tail | 改 hooks.json `python`→`pythonw` |
|---|---|---|
| 解闪窗 | 是（顺带） | 是（直接） |
| 工作量 | 重写 daemon 事件源 + 多 session 路由 + schema 适配层 + 长测 → 数天 | 三处 string 替换 → 1-2 小时 |
| 架构风险 | 多 session 路由 / schema 漂移 / Windows AV 都是未知成本 | 零，回滚一行 |
| 解装机摩擦 | 是（不再需要 hook 配置） | 否 |
| 解 TCP 失败面 | 是 | 否 |

对一个**已经 demo 在卖**的产品，「1-2 小时拿 80% 体感提升、零风险」赢「数天换 100% 体感提升 + 架构债务」。

### 落地的 PR（这次合入的）

```
hooks/hooks.json:
  "python ..." → "pythonw ..." (×3)
```

只改 `hooks.json` 三处，**不动 daemon 代码**——`hook_bridge.py` 里的 `_spawn_daemon_detached` 已经有 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`，单独这俩 flag 已足够防 daemon 子进程闪窗（Microsoft docs 明文：`CREATE_NO_WINDOW` 与 `DETACHED_PROCESS` 一起用时会被忽略，本来加的 CREATE_NO_WINDOW 是误导性冗余，spike 期间 review 时撤回了）。

**已知限制**（PR description 里写明）：

- `pythonw` 是 Windows-only binary，hooks.json 在 macOS/Linux 会 ENOENT
- 当前 repo 实际是 Windows-targeted（ESP32 BLE + Windows changelog），跨平台支持留给后续 issue
- pythonw 启动期的 stderr 仍通过 Claude Code pipe 可见（pipe handle 继承），运行时报错可调试性不变

## 6. 什么条件下重新评估 jsonl tail 路线

按重要度排序，任意**一条**触发就回来重审：

1. **装机摩擦真成瓶颈**：用户量到一定规模，hook settings.json 配置出错率 / TCP 端口冲突报案变多
2. **跨平台需求落地**：要正式支持 macOS / Linux（`pythonw` 路线天然不通，jsonl tail 反而是跨平台优势）
3. **Anthropic 出官方事件流 API**：如果 Claude Code 自己出 IPC / event stream，那 jsonl tail 是过渡，正式 API 是终点
4. **hook 协议反复改**：每次 Anthropic 改 hook payload 我们都要跟，jsonl 也漂但漂的是同一份数据
5. **多 daemon 协作场景**：以后桌宠要同时显示多个 Claude Code 窗口的状态（多 session），daemon 自己路由 sessionId 比 hook 单点广播更合理

如果上面任何一条触发，**不要从零写新 daemon**——直接 `git checkout spike/jsonl-tail-feasibility`，把 `scripts/spike_jsonl_tailer.py` 接到 `ble_daemon._handle_envelope()` 的输入端，跑一周长测，长测过了再做正式重写 plan。

## 7. 参考资料

- Spike 分支：`spike/jsonl-tail-feasibility` (commit `d78b135`)
- Spike tailer 代码：`scripts/spike_jsonl_tailer.py`
- Spike 实测数据：`scripts/spike_jsonl_results.md`
- Codex 会话：`019e24df-824e-7582-8ab8-2b9251fc3b2f`（`codex resume <id>`）
- 现有 hook bridge：`daemon/hook_bridge.py:296-335`（`_spawn_daemon_detached`）
- 现有 ble_daemon 入口：`daemon/ble_daemon.py:269-354`（`_handle_envelope`）
- Microsoft 进程创建 flag 文档：<https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags>
- Plan 原文（spike 通过的版本）：`~/.claude/plans/session-velvety-crane.md`
