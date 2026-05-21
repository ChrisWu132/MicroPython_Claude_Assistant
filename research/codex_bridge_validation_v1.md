# Codex CLI hook 桥接验证报告 v1

## 背景

claude-buddy 原本只接 Claude Code 的 hook。Codex CLI（npm `@openai/codex` 0.132.0）原生支持 hook，字段格式跟 CC 几乎一致。本次新增 Codex 支持，目标是同一 daemon / 同一设备能同时反映两个 agent 的工作状态，且 CC 老用户零变化。

## 方案：auto-route 单脚本（最小 diff）

**入口**：`daemon/hook_bridge.py` `main()` 顶端加 6 行 dispatch，按 payload 是否含 `model` 字段判源（Codex 0.130+ 在 5 类 payload 全部 require `model`，CC 不带）。

- Codex 路径 → `daemon/codex_normalizers.py`（新模块，自包含）
- CC 路径 → 原 CC normalizer（字节不动）

两条线在 daemon TCP 端口汇合，daemon `_dispatch` 只看 `event.kind`，跟来源解耦。

## 代码改动汇总

| 文件 | 类型 | 说明 |
|---|---|---|
| `daemon/hook_bridge.py` | 修改 +6 行 | `main()` 顶上 if `"model" in event` 分发；CC 原代码字节不动 |
| `daemon/codex_normalizers.py` | 新增 ~150 行 | 4 个 normalizer + Codex 工具类别表 + 按工具类型分支的失败检测 |
| `hooks/hooks-codex.json` | 新增 | Codex 专属 hooks 注册（matcher 用 `.*` 而非 CC 的 `*`，因 codex 用正则） |
| `.codex-plugin/plugin.json` | 新增 | Codex plugin manifest，`hooks` 字段指向上述 hooks-codex.json |
| `.agents/plugins/marketplace.json` | 新增 | Codex marketplace 描述（用户 `codex plugin marketplace add` 此 repo URL 时被消费） |

零改动：`daemon/ble_daemon.py`、`daemon/risk_config.py`、`daemon/transport.py`、`hooks/hooks.json`（CC manifest）、`.claude-plugin/`、`device/` 全部、所有现有 tests。

## 设计选择

候选方案对比：

| 方案 | CC 改动 | 装一次 | 加 agent | 选择 |
|---|---|---|---|---|
| 共用单脚本混写逻辑 | 大改 | ✓ | 改主路径 | ❌ |
| 双独立脚本各装 | 零 | ✗（两份） | N 脚本 | ❌ |
| 重构 core + adapter | 重构 | ✓ | 干净 | ❌ 违反最小 diff |
| **auto-route + 独立 normalizer 模块** | **+6 行** | **✓** | **+1 elif** | **✅** |

源识别信号：`"model" in event`。
- Codex 0.130+ 在每个 hook payload 都注入此字段（已确认 codex-rs schema）
- CC 截至 0.x 不带（已用 `tests/fixtures/probe_samples/*.json` 全量验证）
- 失配兜底：万一识别错，CC 路径用 `_normalize_fallback`，daemon 收到 `kind="unknown"`，静默忽略，**不崩 hook，不阻塞 CLI**

## 验证：端到端 stub 实测

**daemon `--stub --offline` 启动后**：

### Codex 链路（合成真实 Codex 形态 payload）

输入：UserPromptSubmit → PreToolUse(Bash:ls) → PostToolUse(Bash, success) → Stop

daemon stdout（实测，2026-05-21）：
```
[session] 'codex-test' → display_name='test'
[req v2] session='codex-test' kind='user_prompt'
[stub-send] {"ss": [{"n": "test", "s": "W"}]}
[req v2] session='codex-test' kind='tool_start'
[stub-send] {"ss": [{"n": "test", "s": "W", "m": "Bash: ls"}]}
[req v2] session='codex-test' kind='tool_done'
[stub-send] {"ss": [{"n": "test", "s": "W"}]}
[req v2] session='codex-test' kind='stop'
[stub-send] {"ss": [{"n": "test", "s": "C"}]}
[stub-send] {"ss": [{"n": "test", "s": "I"}]}   # completed 2s 后自然回 I
```

✅ I/W/C 状态完整触发；wire 帧合规；Bash 工具描述带入 `m` 字段。

### CC 回归（拿现有 fixtures 喂同一个 hook_bridge.py）

输入：`UserPromptSubmit.json` → `PreToolUse.json` (Bash) → `PostToolUse.json` → `PostToolUseFailure.json` → `Stop.json`

daemon stdout（实测）：
```
[session] 'SESSION-FIXTURE-0001' → display_name='MicroPython_'
[req v2] kind='user_prompt'  → {"ss": [{"n":"MicroPython_","s":"W"}]}
[req v2] kind='tool_start'   → {"ss": [{"n":"MicroPython_","s":"W","m":"Bash: cd \"..."}]}
[req v2] kind='tool_done'    → {"ss": [{"n":"MicroPython_","s":"W"}]}
[req v2] kind='tool_error'   → {"ss": [{"n":"MicroPython_","s":"E"}]}
[req v2] kind='stop'         → {"ss": [{"n":"MicroPython_","s":"I"}]}
```

✅ CC PostToolUseFailure → E 状态正常；老 CC 行为字节级一致。

## 已知 limitation

### 1. Codex CLI 0.132 plugin 装机路径在本地 marketplace 下未跑通

实测 `codex plugin marketplace add <local-path>` + `codex plugin add claude-buddy-bridge@claude-buddy` 成功显示 `(installed, enabled)`，且 `~/.codex/plugins/cache/claude-buddy/.../0.1.0/` 下文件齐全。但 `codex exec` 真实跑任务时，hook **未触发**（RUST_LOG=trace 显示 `codex_core_plugins::manifest` 加载 openai-curated 的 `.tmp/plugins/plugins/<name>/` 路径下所有插件，从未访问我们的 cache 路径）。

诊断结论：Codex 0.132.0 (Windows) 的 plugin-loader 似乎只扫描 `~/.codex/.tmp/plugins/plugins/` 一处，不扫描 `codex plugin add` 拷到的 `~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/`。可能：
- 缺一步「让 loader 知道我们 marketplace 的 plugin 路径」（codex 内部 bug 或未文档化的步骤）
- 或本地 marketplace 需要严格的 git 仓库结构才能被认可（openai-curated 是从 git clone 来的）

**绕过方案（distribution 路径）**：把本 repo 推到一个公开 git URL，用户走 `codex plugin marketplace add <git-url>` 安装。codex 会 git clone 到 `.tmp/plugins/`，按它惯用路径扫描，hooks 应该自动生效。**本次未验证**。

### 2. User-config hooks（写在 `~/.codex/config.toml` 的 `[[hooks.*]]`）需 TUI enable

实测：在 user config 里直接写 hook + `--dangerously-bypass-hook-trust` 跑 `codex exec`，hook 也未触发。codex stderr 输出"`--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review"——关键词「**enabled** hooks」。Codex 二进制 strings 里有 `SetHookEnabled` / `HookEnabled` 事件（在 TUI 流程中触发）但 `codex exec` 没 UI 没法 enable。

**结论**：user-config hooks 在 `codex exec` 非交互模式下不可用；只能走 plugin route（plugin hooks 装的时候自动 enabled）。

### 3. Codex 缺 3 类 CC 事件 + Bash 失败检测降级

- **`StopFailure` / `Notification` / `SessionEnd`**：Codex 没这 3 类事件，桥不实现，影响：API 失败 / 通知 / 会话强退在设备上看不到独立状态。
- **Bash `tool_response` 是裸字符串**（非 dict）：codex-rs schema 未对其结构化。当前 `_looks_failed` 对 Bash 字符串只匹配明显 `Error:` / `error:` 前缀作为失败信号，宁可漏报。要补失败检测需 codex 上游 schema 升级或更激进的字符串启发式。

## 用户安装路径（distribution）

| 场景 | 操作 |
|---|---|
| 只用 Claude Code | 现有 `claude plugin install claude-buddy` 流程，零变化 |
| 只用 Codex CLI | 等本 repo 推到公开 Git → `codex plugin marketplace add <repo-url>` → `codex plugin add claude-buddy-bridge@claude-buddy`（本次未在 0.132 本地 marketplace 下跑通，git URL 路径待验证） |
| 两个都用 | 各自装一次 plugin，两路 hook 事件汇到同一 daemon，同一台设备显示两个 session（来源不同显示名不同） |

## 后续待办

1. 把本 PR 合进 main 后，把 repo 配置为公开 Git marketplace（FreakStudioCN/MicroPython_Claude_Assistant 已是公开仓库，可直接 `codex plugin marketplace add owner/repo`）
2. 真机验证 Codex 路径（Phase D 不在此 PR 范围）
3. 跟进 codex 上游：本地 marketplace plugin loader 行为是否已知 bug
