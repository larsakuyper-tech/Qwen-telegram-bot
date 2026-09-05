"""
Telegram-bot die praat met een lokale llama-server en (na jouw bevestiging)
shell-commando's uitvoert op de server waar de bot draait.

Config via environment variables (zie STAPPENPLAN.md):
  TG_TOKEN        - Telegram bot-token (van BotFather)
  TG_ALLOWED_ID   - jouw Telegram user-id; alleen deze gebruiker mag de bot gebruiken
  LLM_API_BASE    - OpenAI-compatible endpoint, standaard http://127.0.0.1:8080/v1
  BOT_WORKDIR     - map waarin commando's draaien, standaard /workspace
"""

import asyncio
import html
import os
import re
import subprocess
import uuid

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.environ["TG_TOKEN"]
ALLOWED_ID = int(os.environ["TG_ALLOWED_ID"])
API_BASE = os.environ.get("LLM_API_BASE", "http://127.0.0.1:8080/v1")
WORKDIR = os.environ.get("BOT_WORKDIR", "/workspace")

CMD_TIMEOUT = 120        # seconden per commando
MAX_OUTPUT = 3500        # tekens output die terug naar Telegram + model gaan
MAX_HISTORY = 40         # berichten in het geheugen per chat

SYSTEM_PROMPT = f"""Je bent een technische assistent met shell-toegang op een Linux-server.
Werkmap: {WORKDIR}

Regels:
- Alles wat uitgevoerd moet worden zet je in een ```sh codeblok. Alleen die blokken worden uitgevoerd.
- Nieuwe bestanden schrijf je met:  cat > pad/naar/bestand << 'EOF'  ...  EOF
- Bestaande bestanden lees je eerst (cat, sed -n '1,80p'), daarna pas aanpassen (sed -i, of hele bestand opnieuw schrijven).
- Eén logische stap per antwoord. Je krijgt de output terug en gaat dan verder.
- Geen interactieve programma's (vim, nano, top, less). Geen sudo nodig.
- Wees kort: één zin uitleg, dan het codeblok.
"""

history: dict[int, list[dict]] = {}     # chat_id -> messages
pending: dict[str, str] = {}            # callback-id -> commando


# ---------- helpers ----------

def is_allowed(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == ALLOWED_ID


def get_history(chat_id: int) -> list[dict]:
    return history.setdefault(chat_id, [{"role": "system", "content": SYSTEM_PROMPT}])


def ask_llm_sync(chat_id: int, text: str) -> str:
    msgs = get_history(chat_id)
    msgs.append({"role": "user", "content": text})
    r = requests.post(
        f"{API_BASE}/chat/completions",
        json={"model": "local", "messages": msgs, "temperature": 0.3, "max_tokens": 1500},
        timeout=600,
    )
    r.raise_for_status()
    reply = r.json()["choices"][0]["message"]["content"]
    msgs.append({"role": "assistant", "content": reply})
    # geheugen inkorten: system prompt bewaren, oudste paar weggooien
    while len(msgs) > MAX_HISTORY:
        del msgs[1:3]
    return reply


def extract_commands(text: str) -> list[str]:
    blocks = re.findall(r"```(?:sh|bash|shell)?\s*\n(.*?)```", text, re.S)
    return [b.strip() for b in blocks if b.strip()]


def run_sync(cmd: str) -> str:
    try:
        p = subprocess.run(
            cmd, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=CMD_TIMEOUT
        )
        out = (p.stdout + p.stderr).strip() or "(geen output)"
        out += f"\n[exit {p.returncode}]"
    except subprocess.TimeoutExpired:
        out = f"(afgebroken: timeout na {CMD_TIMEOUT}s)"
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + "\n…(afgekapt)"
    return out


async def send_long(msg, text: str, pre: bool = False):
    """Stuur tekst in stukken van max 4000 tekens."""
    for i in range(0, len(text), 4000):
        chunk = text[i : i + 4000]
        if pre:
            await msg.reply_text(f"<pre>{html.escape(chunk)}</pre>", parse_mode="HTML")
        else:
            await msg.reply_text(chunk)


async def offer_commands(msg, cmds: list[str]):
    for cmd in cmds:
        cid = uuid.uuid4().hex[:12]
        pending[cid] = cmd
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Uitvoeren", callback_data=f"run:{cid}"),
                InlineKeyboardButton("❌ Annuleren", callback_data=f"no:{cid}"),
            ]]
        )
        await msg.reply_text(
            f"<pre>{html.escape(cmd)}</pre>", parse_mode="HTML", reply_markup=kb
        )


async def handle_model_reply(msg, chat_id: int, reply: str):
    cmds = extract_commands(reply)
    # tekst zonder de codeblokken
    prose = re.sub(r"```.*?```", "", reply, flags=re.S).strip()
    if prose:
        await send_long(msg, prose)
    if cmds:
        await offer_commands(msg, cmds)


# ---------- handlers ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Bot draait. Stuur een opdracht in gewone taal.\n"
        "/run <cmd> – zelf direct een commando draaien\n"
        "/reset – gespreksgeheugen wissen"
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    history.pop(update.effective_chat.id, None)
    await update.message.reply_text("Geheugen gewist.")


async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Commando dat je zelf typt: direct uitvoeren, geen bevestiging nodig."""
    if not is_allowed(update):
        return
    cmd = " ".join(ctx.args).strip()
    if not cmd:
        await update.message.reply_text("Gebruik: /run <commando>")
        return
    out = await asyncio.to_thread(run_sync, cmd)
    await send_long(update.message, out, pre=True)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id, "typing")
    try:
        reply = await asyncio.to_thread(ask_llm_sync, chat_id, update.message.text)
    except Exception as e:
        await update.message.reply_text(f"LLM-fout: {e}")
        return
    await handle_model_reply(update.message, chat_id, reply)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_allowed(update):
        return
    action, cid = q.data.split(":", 1)
    cmd = pending.pop(cid, None)
    if cmd is None:
        await q.edit_message_reply_markup(None)
        return
    if action == "no":
        await q.edit_message_text("❌ Overgeslagen.")
        return

    await q.edit_message_reply_markup(None)
    out = await asyncio.to_thread(run_sync, cmd)
    await send_long(q.message, out, pre=True)

    # output teruggeven aan het model zodat het de volgende stap kan voorstellen
    chat_id = q.message.chat_id
    await ctx.bot.send_chat_action(chat_id, "typing")
    feedback = f"Output van:\n{cmd}\n\n{out}\n\nGa verder, of zeg dat het klaar is."
    try:
        reply = await asyncio.to_thread(ask_llm_sync, chat_id, feedback)
    except Exception as e:
        await q.message.reply_text(f"LLM-fout: {e}")
        return
    await handle_model_reply(q.message, chat_id, reply)


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print(f"Bot gestart. Werkmap: {WORKDIR}, LLM: {API_BASE}")
    app.run_polling()


if __name__ == "__main__":
    main()
