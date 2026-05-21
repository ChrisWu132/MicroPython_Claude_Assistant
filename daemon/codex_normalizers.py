#!/usr/bin/env python3
# codex_normalizers.py —— Codex CLI (openai/codex) hook payload 规整层
#
# 本模块是 Codex CLI 专属。dispatch 入口在 hook_bridge.py（auto-route：
# payload 里 "model" 字段是 Codex 的强信号，CC 不带）。CC 路径不经过本模块。
#
# 设计目标：输出跟 CC 同形状的 v2 envelope，让 daemon 完全无感知来源。
# daemon 只 dispatch 在 event.kind 上，跟 CC / Codex / 任何未来 agent 解耦。
#
# 已知 Codex hooks 字段（codex-rs schema + 2026-05-20 Codex review verify）：
#   通用：session_id, cwd, hook_event_name, transcript_path, permission_mode, model
#   PreToolUse:       + tool_name, tool_input, tool_use_id
#   PostToolUse:      + tool_response（tool-specific schema，Bash 是裸字符串）
#   UserPromptSubmit: + prompt
#   Stop:             + last_assistant_message, stop_hook_active
#   SessionStart:     + source (startup/resume/clear/compact)
#
# Codex 不发：PostToolUseFailure / StopFailure / Notification / SessionEnd / PostToolBatch
# 这些事件 daemon 走兜底（kind="unknown"），静默忽略。

from typing import Any

try:
    from .hook_bridge import _generic, _trunc
except ImportError:
    from hook_bridge import _generic, _trunc


# Codex 工具名 → 5 桶分类。已确认 Codex Bash 工具叫 "Bash"（首字母大写）。
# 其它工具名未亲测；未知 fallback "other"，未知 tool 进 daemon 仍走 W 状态。
_CODEX_TOOL_CATEGORY = {
    "Bash":         "exec",
    "shell":        "exec",
    "local_shell":  "exec",
    "apply_patch":  "edit",
    "Edit":         "edit",
    "Write":        "edit",
    "read_file":    "read",
    "Read":         "read",
    "view_image":   "read",
    "Glob":         "read",
    "Grep":         "read",
    "web_search":   "web",
    "web_fetch":    "web",
    "WebSearch":    "web",
    "WebFetch":     "web",
    "update_plan":  "other",
    "Task":         "agent",
}


def _tool_category(name: str) -> str:
    return _CODEX_TOOL_CATEGORY.get(name, "other")


def _hint_from_tool_input(tool_input: Any) -> str:
    """从 tool_input 抽一句给设备显示用，80 字以内。dict 优先识别常见 key，
    str 直接截断（部分 Codex 工具 tool_input 是字符串如 shell command）。"""
    if isinstance(tool_input, str):
        return tool_input[:80]
    if not isinstance(tool_input, dict):
        return ""
    # 优先级跟 CC 一致：command (Bash/shell) > path-like > description > url
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
        v = tool_input.get(key)
        if isinstance(v, str) and v:
            return v[:80]
    return ""


def _looks_failed(tool_name: str, tool_response: Any) -> bool:
    """按工具类型判断 PostToolUse 是不是失败。保守原则：未知 schema 一律算成功。

    依据（Codex review 2026-05-20）：
    - Bash 的 tool_response 是裸字符串，无 exit_code/error 字段 → 不能判失败
    - MCP 类工具 tool_response 是 dict，用 isError（驼峰）
    - 其它工具实测中观察"""
    # Bash 裸字符串：除非字符串明显是错误模板，否则归成功
    if isinstance(tool_response, str):
        # 保守：只匹配非常显式的"Error:"开头才算失败，避免误把含 stderr 的成功命令归为 E 状态
        s = tool_response.strip()
        return s.startswith("Error:") or s.startswith("error:")
    # MCP/其它 dict：看几个常见失败标记
    if isinstance(tool_response, dict):
        if tool_response.get("isError") is True:
            return True
        if tool_response.get("is_error") is True:
            return True
        if tool_response.get("success") is False:
            return True
        err = tool_response.get("error")
        if isinstance(err, str) and err:
            return True
        if isinstance(err, dict) and err:
            return True
        # exit_code 仅在非 0 时为失败（0 可能不存在 = 成功）
        ec = tool_response.get("exit_code")
        if isinstance(ec, int) and ec != 0:
            return True
    return False


# ── 4 类 normalizer ─────────────────────────────────────────────


def _norm_pre_tool(event: dict) -> dict:
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input")
    return {
        "type": "event",
        "v": 2,
        "event": {
            "kind":           "tool_start",
            "tool":           tool,
            "tool_category":  _tool_category(tool),
            "summary":        _hint_from_tool_input(tool_input),
            "needs_approval": False,
            "tool_use_id":    event.get("tool_use_id", ""),
            "risk_level":     "normal",  # Codex 自带 sandbox，桥这层不重复风险评估
        },
        "generic": _generic(event),
    }


def _norm_post_tool(event: dict) -> dict:
    """成功 / 失败 共用入口。按 tool_name + tool_response 形态判失败。"""
    tool = event.get("tool_name", "")
    tool_response = event.get("tool_response")
    if _looks_failed(tool, tool_response):
        err_msg = ""
        if isinstance(tool_response, dict):
            err = tool_response.get("error")
            if isinstance(err, str):
                err_msg = _trunc(err, 80)
            elif isinstance(err, dict):
                err_msg = _trunc(str(err.get("message", "")), 80)
        elif isinstance(tool_response, str):
            err_msg = _trunc(tool_response, 80)
        return {
            "type": "event",
            "v": 2,
            "event": {
                "kind":          "tool_error",
                "tool":          tool,
                "tool_category": _tool_category(tool),
                "error_msg":     err_msg,
                "is_interrupt":  False,
                "duration_ms":   event.get("duration_ms", 0),
                "tool_use_id":   event.get("tool_use_id", ""),
            },
            "generic": _generic(event),
        }
    return {
        "type": "event",
        "v": 2,
        "event": {
            "kind":          "tool_done",
            "tool":          tool,
            "tool_category": _tool_category(tool),
            "duration_ms":   event.get("duration_ms", 0),
            "tool_use_id":   event.get("tool_use_id", ""),
            "interrupted":   False,
        },
        "generic": _generic(event),
    }


def _norm_user_prompt(event: dict) -> dict:
    prompt = _trunc(event.get("prompt", ""), 80)
    return {
        "type": "event",
        "v": 2,
        "event": {
            "kind":   "user_prompt",
            "prompt": prompt,
        },
        "generic": _generic(event),
    }


def _norm_stop(event: dict) -> dict:
    # Codex Stop 跟 CC Stop 同语义：assistant turn 结束。
    # last_assistant_message 实测可能含 API 错误，但 v5 daemon Stop 处理已经够鲁棒，
    # 不需要在这里再分流 task_error。
    return {
        "type": "event",
        "v": 2,
        "event": {"kind": "stop"},
        "generic": _generic(event),
    }


def _norm_session_start(event: dict) -> dict:
    """Codex SessionStart：把 source 当 turn 起点信号，对齐 user_prompt 效果。
    暂未注册为真 hook，留 normalizer 备用。"""
    return {
        "type": "event",
        "v": 2,
        "event": {"kind": "unknown"},  # daemon 会忽略；如未来要响应，改成 user_prompt
        "generic": _generic(event),
    }


def _norm_fallback(event: dict) -> dict:
    return {
        "type": "event",
        "v": 2,
        "event": {"kind": "unknown"},
        "generic": _generic(event),
    }


_NORMALIZERS = {
    "PreToolUse":       _norm_pre_tool,
    "PostToolUse":      _norm_post_tool,
    "UserPromptSubmit": _norm_user_prompt,
    "Stop":             _norm_stop,
    "SessionStart":     _norm_session_start,
}


def normalize(event: dict) -> dict:
    """单一对外入口。dispatch 在 hook_bridge.py 已经判过 source=Codex。"""
    hook = event.get("hook_event_name", "")
    fn = _NORMALIZERS.get(hook, _norm_fallback)
    return fn(event)
