import os
import asyncio
from dotenv import load_dotenv
# pyrefly: ignore [missing-import]
from telegram import Update
# pyrefly: ignore [missing-import]
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["TELEGRAM_USER_ID"])

active_dir = os.path.expanduser("~")

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


async def run_claude(prompt: str, cwd: str, timeout: int = 300) -> str:
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return f"Timeout: Claude ne {timeout}s mein respond nahi kiya."
    output = stdout.decode().strip()
    return output or stderr.decode().strip() or "Claude ne koi response nahi diya."


# ---------- Handlers ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text(
        "Bot ready!\n\n"
        "/use <path> — project folder set karo\n"
        "/dir — current folder dekho\n"
        "/add — git add .\n"
        "/commit <message> — git commit\n"
        "/push — git push\n"
        "/status — git status\n"
        "/reset — session reset karo (tabhi reset hogi jab tu bole)\n\n"
        "Session same rehti hai jab tak /reset na karo. Claude sab yaad rakhega!"
    )


async def cmd_dir(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text(f"Active folder: {active_dir}")


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
    await update.message.reply_text(f"Folder set: {active_dir}")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    await update.message.reply_text("Session reset ho gayi. Naya session shuru!")


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    ok, out = await run_shell(f"cd '{active_dir}' && git add .")
    await update.message.reply_text("git add done." if ok else f"Error: {out}")


async def cmd_commit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not auth(update): return
    parts = update.message.text.split(maxsplit=1)
    msg = parts[1].strip() if len(parts) > 1 else "Update via Telegram"
    ok, out = await run_shell(f"cd '{active_dir}' && git commit -m \"{msg}\"")
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

    thinking_msg = await update.message.reply_text("Claude soch raha hai...")

    response = await run_claude(prompt, active_dir)

    # Telegram message limit 4096 chars
    if len(response) > 4096:
        for i in range(0, len(response), 4096):
            await update.message.reply_text(response[i:i+4096])
    else:
        await update.message.reply_text(response)

    await thinking_msg.delete()


# ---------- Main ----------

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("dir", cmd_dir))
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("commit", cmd_commit))
    app.add_handler(CommandHandler("push", cmd_push))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot chal raha hai...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
