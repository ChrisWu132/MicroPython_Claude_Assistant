"""Spike: tail Claude Code's own JSONL session log and synthesize v2 envelopes.

Read-only probe. Does NOT connect to BLE, does NOT spawn ble_daemon, does NOT
touch hooks. Sole purpose: collect latency + coverage data to decide whether
tail-jsonl can replace hook_bridge. See plans/session-velvety-crane.md.

Run:  python scripts/spike_jsonl_tailer.py
"""
from __future__ import annotations
import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home() / ".claude" / "projects"
POLL_MS = 50
SCAN_FILE_EVERY_S = 2.0

def now_ms() -> int:
    return int(time.time() * 1000)

def parse_ts(s: str) -> int | None:
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None

def latest_jsonl() -> Path | None:
    cands = list(ROOT.glob("*/*.jsonl"))
    return max(cands, key=lambda p: p.stat().st_mtime, default=None) if cands else None

def classify(rec: dict) -> tuple[str, dict] | None:
    """Map a JSONL record to a v2-envelope-like (kind, fields). Return None to skip."""
    t = rec.get("type")
    if t == "last-prompt":
        return "user_prompt", {"session": rec.get("sessionId")}
    if t == "tool_use":
        return "tool_start", {"name": rec.get("name"), "id": rec.get("id")}
    if t == "tool_result":
        kind = "tool_error" if rec.get("is_error") else "tool_done"
        return kind, {"tool_use_id": rec.get("tool_use_id")}
    if t == "assistant":
        msg = rec.get("message", {})
        for c in msg.get("content", []) or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                return "tool_start", {"name": c.get("name"), "id": c.get("id")}
    if t == "user":
        msg = rec.get("message", {})
        content = msg.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    kind = "tool_error" if c.get("is_error") else "tool_done"
                    return kind, {"tool_use_id": c.get("tool_use_id")}
    return None

def print_event(kind: str, fields: dict, rec: dict, file_path: Path) -> None:
    record_ts = parse_ts(rec.get("timestamp", ""))
    arrival = now_ms()
    lag = (arrival - record_ts) if record_ts else None
    lag_str = f"{lag:>5}ms" if lag is not None else "  ?  "
    sid = (rec.get("sessionId") or "")[:8]
    cwd = (rec.get("cwd") or "").split("\\")[-1][:20]
    print(f"[lag={lag_str}] sid={sid} cwd={cwd:20} kind={kind:18} fields={fields}", flush=True)

def follow(path: Path) -> None:
    print(f"[tailer] following {path.name} (size={path.stat().st_size})", flush=True)
    counts: dict[str, int] = {}
    buf = b""
    pos = path.stat().st_size
    last_scan = time.time()
    last_path = path
    while True:
        try:
            now = time.time()
            if now - last_scan >= SCAN_FILE_EVERY_S:
                last_scan = now
                latest = latest_jsonl()
                if latest and latest != last_path:
                    print(f"[tailer] switched to newer file: {latest.name}", flush=True)
                    print(f"[tailer] coverage so far: {counts}", flush=True)
                    last_path = latest
                    path = latest
                    pos = path.stat().st_size  # seek to EOF on switch — measure live, not historical
                    buf = b""
            st = path.stat()
            if st.st_size < pos:
                pos = 0
                buf = b""
            if st.st_size > pos:
                with path.open("rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue
                    out = classify(rec)
                    if out:
                        kind, fields = out
                        counts[kind] = counts.get(kind, 0) + 1
                        print_event(kind, fields, rec, path)
            time.sleep(POLL_MS / 1000)
        except KeyboardInterrupt:
            print(f"\n[tailer] FINAL coverage: {counts}", flush=True)
            return
        except OSError as e:
            print(f"[tailer] OSError (locked? AV?): {e!r}", flush=True)
            time.sleep(0.5)

def main() -> int:
    if not ROOT.exists():
        print(f"[tailer] {ROOT} does not exist", file=sys.stderr)
        return 1
    p = latest_jsonl()
    if not p:
        print(f"[tailer] no jsonl found under {ROOT}", file=sys.stderr)
        return 1
    print(f"[tailer] root={ROOT}  poll={POLL_MS}ms  starting at EOF", flush=True)
    follow(p)
    return 0

if __name__ == "__main__":
    sys.exit(main())
