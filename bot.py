import os
import sys
import json
import time
import asyncio
from datetime import datetime
from dotenv import load_dotenv


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# Line-buffered stdout so `nohup ... > bot.log` also flushes promptly
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
# pyrefly: ignore [missing-import]
from telegram import Update
# pyrefly: ignore [missing-import]
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["TELEGRAM_USER_ID"])

active_dir = os.path.expanduser("~")
# Which Claude CLI binary to use: "claude", "claude1", "claude2", etc.
claude_bin = "claude"
# Per-(binary, directory) Claude Code session id, so switching /use or /claude
# doesn't mix contexts and doesn't try to resume a session from a different CLI.
session_ids: dict[tuple[str, str], str] = {}


def _sess_key(cwd: str) -> tuple[str, str]:
    return (claude_bin, cwd)

# Telegram edit-rate throttle (seconds)
EDIT_INTERVAL = 1.5
# Max lines to keep in the live activity feed
MAX_ACTIVITY_LINES = 12


def auth(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ALLOWED_USER_ID


async def run_shell(cmd: str) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out = (stdout + stderr).decode().strip()
    return proc.returncode == 0, out


def _short(s: str, n: int = 80) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_tool(name: str, inp: dict) -> str:
    """One-line human-readable summary of a tool_use event."""
    try:
        if name == "Read":
            return f"📖 Read {_short(str(inp.get('file_path', '')))}"
        if name in ("Edit", "MultiEdit"):
            return f"✏️ Edit {_short(str(inp.get('file_path', '')))}"
        if name == "Write":
            return f"📝 Write {_short(str(inp.get('file_path', '')))}"
        if name == "Bash":
            desc = inp.get("description") or inp.get("command", "")
            return f"⚙️ Bash: {_short(str(desc))}"
        if name == "Grep":
            return f"🔎 Grep {_short(str(inp.get('pattern', '')))}"
        if name == "Glob":
            return f"🗂  Glob {_short(str(inp.get('pattern', '')))}"
        if name == "WebFetch":
            return f"🌐 Fetch {_short(str(inp.get('url', '')))}"
        if name == "WebSearch":
            return f"🔍 Search {_short(str(inp.get('query', '')))}"
        if name == "TodoWrite":
            todos = inp.get("todos", [])
            return f"✅ Todos ({len(todos)})"
        if name == "Task":
            return f"🤖 Subagent: {_short(str(inp.get('description', '')))}"
        # Generic
        first_val = next(iter(inp.values()), "") if isinstance(inp, dict) else ""
        return f"🔧 {name} {_short(str(first_val))}"
    except Exception:
        return f"🔧 {name}"


class LiveStatus:
    """Throttled Telegram message updater for showing Claude's live activity."""

    def __init__(self, message):
        self.message = message
        self.lines: list[str] = []
        self.header = "🤔 Claude soch raha hai..."
        self.last_edit = 0.0
        self.last_text = ""
        self._pending_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def _render(self) -> str:
        body = "\n".join(self.lines[-MAX_ACTIVITY_LINES:])
        return f"{self.header}\n\n{body}" if body else self.header

    async def _do_edit(self, text: str):
        try:
            await self.message.edit_text(text)
            self.last_text = text
            self.last_edit = time.monotonic()
        except Exception:
            # Telegram will complain if text is unchanged or rate-limited; ignore
            pass

    async def _schedule(self):
        """Debounced edit: coalesces bursts of updates into one edit per EDIT_INTERVAL."""
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, EDIT_INTERVAL - (now - self.last_edit))
        if wait:
            await asyncio.sleep(wait)
        text = self._render()
        if text != self.last_text:
            await self._do_edit(text)

    def _kick(self):
        if self._pending_task and not self._pending_task.done():
            return
        self._pending_task = asyncio.create_task(self._schedule())

    def set_header(self, header: str):
        self.header = header
        self._kick()

    def add(self, line: str):
        self.lines.append(line)
        self._kick()

    async def flush(self):
        # Cancel any pending debounced edit and force a final render
        if self._pending_task:
            try:
                await self._pending_task
            except Exception:
                pass
        text = self._render()
        if text != self.last_text:
            await self._do_edit(text)

    async def finish_and_delete(self):
        try:
            await self.message.delete()
        except Exception:
            pass


async def run_claude_streaming(prompt: str, cwd: str, status: LiveStatus, timeout: int = 900) -> str:
    """Run Claude Code CLI in stream-json mode and push progress into LiveStatus."""
    args = [claude_bin, "-p", prompt, "--output-format", "stream-json", "--verbose"]

    key = _sess_key(cwd)
    existing = session_ids.get(key)
    if existing:
        args += ["--resume", existing]

    status.set_header(f"🚀 {claude_bin} start ho raha hai...")
    log(f"▶ RUN {claude_bin} cwd={cwd} resume={existing[:8] + '...' if existing else 'NEW'}")
    log(f"  prompt: {_short(prompt, 200)}")

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    final_result: str | None = None
    stderr_buf: list[str] = []
    turn = 0
    resume_failed = False

    async def drain_stderr():
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            stderr_buf.append(line.decode(errors="replace"))

    stderr_task = asyncio.create_task(drain_stderr())

    async def read_stream():
        nonlocal final_result, turn, resume_failed
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                evt = json.loads(line.decode(errors="replace"))
            except json.JSONDecodeError:
                continue

            etype = evt.get("type")

            if etype == "system" and evt.get("subtype") == "init":
                sid = evt.get("session_id")
                if sid:
                    session_ids[_sess_key(cwd)] = sid
                    status.set_header(f"🤔 {claude_bin} soch raha hai... (session {sid[:8]})")
                    log(f"  session init: {sid}")
                continue

            if etype == "assistant":
                msg = evt.get("message", {})
                content = msg.get("content") or []
                turn += 1
                for block in content:
                    btype = block.get("type")
                    if btype == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            status.add(f"💬 {_short(text, 140)}")
                            log(f"  💬 {_short(text, 200)}")
                    elif btype == "tool_use":
                        line = _fmt_tool(block.get("name", "?"), block.get("input") or {})
                        status.add(line)
                        log(f"  {line}")
                continue

            if etype == "user":
                # tool_result from a previous tool_use — show brief outcome if it's an error
                msg = evt.get("message", {})
                content = msg.get("content") or []
                for block in content:
                    if block.get("type") == "tool_result" and block.get("is_error"):
                        raw = block.get("content")
                        text = ""
                        if isinstance(raw, list):
                            for part in raw:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    text = part.get("text", "")
                                    break
                        elif isinstance(raw, str):
                            text = raw
                        status.add(f"⚠️ Tool error: {_short(text, 120)}")
                        log(f"  ⚠️ tool error: {_short(text, 200)}")
                continue

            if etype == "result":
                sid = evt.get("session_id")
                if sid:
                    session_ids[_sess_key(cwd)] = sid
                if evt.get("subtype") == "success":
                    final_result = evt.get("result") or ""
                    log(f"✅ DONE ({len(final_result)} chars)")
                else:
                    err = evt.get("error") or evt.get("subtype") or "unknown error"
                    if isinstance(err, str) and "session" in err.lower() and existing:
                        resume_failed = True
                    final_result = f"⚠️ {err}"
                    log(f"⚠ ERROR: {err}")
                continue

    try:
        await asyncio.wait_for(read_stream(), timeout=timeout)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"⏱ Timeout: Claude ne {timeout}s mein finish nahi kiya."
    finally:
        try:
            await asyncio.wait_for(stderr_task, timeout=2)
        except Exception:
            stderr_task.cancel()

    # If resume was stale, retry once fresh
    if resume_failed:
        session_ids.pop(_sess_key(cwd), None)
        status.add("🔄 Session stale thi, fresh se retry...")
        await status.flush()
        return await run_claude_streaming(prompt, cwd, status, timeout)

    if final_result is None:
        errtxt = "".join(stderr_buf).strip()
        return errtxt or "Claude ne koi response nahi diya."
    return final_result.strip()


# ---------- Handlers ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text(
        "Bot ready!\n\n"
        "/use <path> — project folder set karo\n"
        "/dir — current folder + session dekho\n"
        "/add — git add .\n"
        "/commit <message> — git commit\n"
        "/push — git push\n"
        "/status — git status\n"
        "/reset — is folder ki session reset karo\n"
        "/stop — chal rahi Claude request cancel karo\n"
        "/claude <1|2|name> — kaunsa Claude CLI use kare switch karo\n"
        "/which — abhi kaunsa Claude use ho raha dekho\n\n"
        "Har (Claude, folder) combo ka apna session hai. Live progress dikhta hai — kya read/edit/run ho raha sab."
    )


async def cmd_dir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    sid = session_ids.get(_sess_key(active_dir))
    tail = f"\nSession: {sid[:8]}..." if sid else "\nSession: (nayi shuru hogi next message pe)"
    await update.message.reply_text(f"Claude: {claude_bin}\nActive folder: {active_dir}{tail}")


async def cmd_which(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    ok, path = await run_shell(f"which {claude_bin}")
    await update.message.reply_text(f"Active Claude: {claude_bin}\nPath: {path or '(not found)'}")


async def cmd_claude(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global claude_bin
    if not auth(update): return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text(
            f"Abhi: {claude_bin}\nUse: /claude 1  ya  /claude 2  ya  /claude <binary-name>"
        )
        return
    arg = parts[1].strip()
    # Shortcuts: "1" -> claude1, "2" -> claude2
    if arg in ("1", "2"):
        target = f"claude{arg}"
    else:
        target = arg

    ok, path = await run_shell(f"which {target}")
    if not ok or not path:
        await update.message.reply_text(f"'{target}' PATH me nahi mila. Install/alias check kar.")
        return

    claude_bin = target
    sid = session_ids.get(_sess_key(active_dir))
    tail = f" (session {sid[:8]}... resume hoga)" if sid else " (nayi session banegi)"
    await update.message.reply_text(f"Switched to {claude_bin}\nPath: {path}{tail}")


async def cmd_use(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global active_dir
    if not auth(update): return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Path do: /use /path/to/project")
        return

    path = parts[1].strip().replace("~", os.path.expanduser("~"))
    if not os.path.isdir(path):
        await update.message.reply_text(f"Folder nahi mila: {path}")
        return

    active_dir = path
    sid = session_ids.get(_sess_key(active_dir))
    tail = f" (existing session resume hoga: {sid[:8]}...)" if sid else " (nayi session banegi)"
    await update.message.reply_text(f"Folder set: {active_dir}\nClaude: {claude_bin}{tail}")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    removed = session_ids.pop(_sess_key(active_dir), None)
    if removed:
        await update.message.reply_text(f"Session reset ({claude_bin} @ {active_dir}). Next message se nayi shuru hogi.")
    else:
        await update.message.reply_text(f"{claude_bin} ke liye is folder me koi active session nahi thi.")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    task: asyncio.Task | None = ctx.chat_data.get("current_task") if ctx.chat_data else None
    if task and not task.done():
        task.cancel()
        await update.message.reply_text("🛑 Cancel signal bhej diya.")
    else:
        await update.message.reply_text("Kuch chal nahi raha abhi.")


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    ok, out = await run_shell(f"cd '{active_dir}' && git add .")
    await update.message.reply_text("git add done." if ok else f"Error: {out}")


async def cmd_commit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    parts = update.message.text.split(maxsplit=1)
    msg = parts[1].strip() if len(parts) > 1 else "Update via Telegram"
    safe_msg = msg.replace('"', '\\"')
    ok, out = await run_shell(f"cd '{active_dir}' && git commit -m \"{safe_msg}\"")
    await update.message.reply_text(out if out else ("Committed!" if ok else "Kuch commit karne ko nahi tha."))


async def cmd_push(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    _, branch = await run_shell(f"cd '{active_dir}' && git branch --show-current")
    if not branch:
        await update.message.reply_text("Git branch detect nahi hua.")
        return
    ok, out = await run_shell(f"cd '{active_dir}' && git push origin {branch}")
    await update.message.reply_text(out if out else f"Pushed to {branch}!")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    _, out = await run_shell(f"cd '{active_dir}' && git status --short")
    text = f"```\n{out}\n```" if out else "Working tree clean."
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    if not update.message or not update.message.text: return
    prompt = update.message.text
    log(f"📨 MSG from {update.effective_user.id}: {_short(prompt, 200)}")

    thinking_msg = await update.message.reply_text("🤔 Claude soch raha hai...")
    status = LiveStatus(thinking_msg)

    async def worker():
        return await run_claude_streaming(prompt, active_dir, status)

    task = asyncio.create_task(worker())
    ctx.chat_data["current_task"] = task

    try:
        response = await task
    except asyncio.CancelledError:
        response = "🛑 Cancel ho gaya."
    except Exception as e:
        response = f"Error: {e}"
    finally:
        ctx.chat_data["current_task"] = None
        await status.flush()

    if not response.strip():
        response = "(Claude ne khali response bheja)"

    for i in range(0, len(response), 4096):
        await update.message.reply_text(response[i:i + 4096])

    await status.finish_and_delete()


# ---------- Main ----------

def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("dir", cmd_dir))
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("claude", cmd_claude))
    app.add_handler(CommandHandler("which", cmd_which))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("commit", cmd_commit))
    app.add_handler(CommandHandler("push", cmd_push))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log(f"Bot chal raha hai... (default claude={claude_bin}, cwd={active_dir})")
    app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
