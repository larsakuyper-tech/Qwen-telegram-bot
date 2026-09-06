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
import shlex
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
MAX_OUTPUT_MODEL = 40000   # tekens commando-output die het model te zien krijgt (~10K tokens)
MAX_TG_MESSAGES = 3        # max aantal Telegram-berichten (à 4000 tekens) per commando-output; de rest ziet alleen het model
MAX_OUTPUT_TG = MAX_TG_MESSAGES * 4000
AUTO_RUN_READONLY = True   # alleen-lezen commando's direct uitvoeren, zonder bevestigingsknop
MAX_HISTORY_CHARS = 200000 # totale grootte van het gespreksgeheugen (~50K tokens); oudste eruit als het meer wordt

SYSTEM_PROMPT = f"""Je bent een technische assistent met shell-toegang op een Linux-server.
Werkmap: {WORKDIR}. De projecten daar: accountability-bot (server), AccountabilityBotApp (Android)
en AccountabilityBotWindows.

Regels:
- Alles wat uitgevoerd moet worden zet je in een ```sh codeblok. Alleen die blokken worden uitgevoerd.
- Nieuwe bestanden schrijf je met:  cat > pad/naar/bestand << 'EOF'  ...  EOF
- Bestaande bestanden lees je eerst (cat, sed -n '1,80p'), daarna pas aanpassen (sed -i, of hele bestand opnieuw schrijven).
- Kleine wijzigingen per stap. Eén logische stap per antwoord; je krijgt de output terug en gaat dan verder.
- Geen interactieve programma's (vim, nano, top, less).
- Houd output compact: gebruik head/tail, grep -n met gerichte patronen, sed -n 'a,bp' voor een
  stuk van een bestand, en `wc -l` of `grep -c` als je alleen aantallen nodig hebt. Nooit een heel
  groot bestand of een brede grep zonder limiet dumpen.
- Toon vóór elke herstart eerst `git diff` (bij grote diffs `git diff --stat`) en wacht op akkoord.
- Vóór `sudo systemctl restart accountability-bot` altijd eerst de tests:
  cd {WORKDIR}/accountability-bot && bot_env/bin/python test_<naam>.py
- Herstarten mag alleen met `sudo systemctl restart accountability-bot`; status en log met
  `sudo systemctl status accountability-bot` en `sudo journalctl -u accountability-bot -n 50`.
  Andere sudo-commando's zijn niet toegestaan en werken ook niet.
- Raak nooit bot_data, .env of app_releases aan.
- Commit na een geslaagde herstart met `git add -A && git commit -m "<wat>"`.
- Wees kort: één zin uitleg, dan het codeblok.
"""

history: dict[int, list[dict]] = {}     # chat_id -> messages
pending: dict[str, str] = {}            # callback-id -> commando


# ---------- helpers ----------

# Commando's die niets kunnen wijzigen. Alles wat hier niet op staat, krijgt de bevestigingsknop.
# Bewust NIET op de lijst: find (-delete/-exec), xargs (voert alles uit), env (start een programma),
# awk (system()) — die vragen gewoon de knop. sed staat erop, maar zonder -i en zonder w-commando.
READONLY_CMDS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "sed",
    "wc", "cut", "sort", "uniq", "tr", "file", "stat", "du", "df", "pwd", "echo", "which",
    "type", "basename", "dirname", "readlink", "realpath", "date", "whoami", "id",
    "printenv", "hostname", "uname", "ps", "free", "uptime", "tree", "diff", "cmp", "md5sum",
    "sha256sum", "column", "nl", "jq", "true", "cd", "test",
}
# Subcommando's die per hoofdcommando veilig zijn
READONLY_SUB = {
    "git": {"status", "log", "diff", "show", "branch", "remote", "ls-files", "blame",
            "describe", "rev-parse", "config", "shortlog", "tag", "grep", "cat-file"},
    "systemctl": {"status", "show", "list-units", "is-active", "is-enabled", "cat"},
    "journalctl": None,   # None = altijd veilig
    "pip": {"list", "show", "freeze"},
    "docker": {"ps", "images", "logs", "inspect"},
}
# Tekens/constructies die schrijven of code kunnen injecteren -> nooit automatisch
UNSAFE_PATTERN = re.compile(r"[>`]|\$\(|<\(|>\(|\btee\b|\bdd\b")


def is_readonly(cmd: str) -> bool:
    """True als elk onderdeel van het commando aantoonbaar alleen leest."""
    if UNSAFE_PATTERN.search(cmd):
        return False
    # splitsen op ; && || | en nieuwe regels
    parts = re.split(r"(?:;|&&|\|\||\||\n)", cmd)
    if not any(p.strip() for p in parts):
        return False
    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            tokens = shlex.split(part)
        except ValueError:
            return False          # onbalans in quotes: niet vertrouwen
        if not tokens:
            return False
        # VAR=waarde-prefixes overslaan
        while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            return False
        base = os.path.basename(tokens[0])
        if base in READONLY_SUB:
            subs = READONLY_SUB[base]
            if subs is None:
                continue
            # subcommando zoeken: opties overslaan, incl. hun waarde bij -C/-c/-u
            rest, skip = [], False
            for t in tokens[1:]:
                if skip:
                    skip = False
                    continue
                if t.startswith("-"):
                    if t in ("-C", "-c", "-u", "-n"):
                        skip = True
                    continue
                rest.append(t)
            if not rest or rest[0] not in subs:
                return False
            continue
        if base == "sed":         # sed -i schrijft; een w/W-commando in het script ook ("sed 'w bestand'")
            if any(t == "-i" or t.startswith("-i") or t == "--in-place" for t in tokens[1:]):
                return False
            if any(re.search(r"(^|[;\n{])\s*[wW]\s", t + " ") for t in tokens[1:] if not t.startswith("-")):
                return False
            continue
        if base == "python3":     # alleen python3 -c/-m mag niet blind; -c kan alles
            return False
        if base not in READONLY_CMDS:
            return False
    return True


def is_allowed(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == ALLOWED_ID


def get_history(chat_id: int) -> list[dict]:
    return history.setdefault(chat_id, [{"role": "system", "content": SYSTEM_PROMPT}])


def ask_llm_sync(chat_id: int, text: str) -> str:
    msgs = get_history(chat_id)
    msgs.append({"role": "user", "content": text})
    r = requests.post(
        f"{API_BASE}/chat/completions",
        json={"model": "local", "messages": msgs, "temperature": 0.3, "max_tokens": 8000},
        timeout=600,
    )
    r.raise_for_status()
    reply = r.json()["choices"][0]["message"]["content"]
    msgs.append({"role": "assistant", "content": reply})
    # geheugen inkorten op grootte: system prompt bewaren, oudste paar weggooien
    while len(msgs) > 3 and sum(len(m["content"]) for m in msgs) > MAX_HISTORY_CHARS:
        del msgs[1:3]
    return reply


# Het model schrijft de taal van het codeblok niet altijd in kleine letters ("```Shell") en soms als
# "console"/"zsh"; zonder deze tolerantie verscheen zo'n blok als tekst zonder knop en gebeurde er niets.
CODEBLOK = re.compile(r"```(?:sh|bash|shell|zsh|console|shellscript)?[ \t]*\r?\n(.*?)```", re.S | re.I)


def extract_commands(text: str) -> list[str]:
    blocks = CODEBLOK.findall(text)
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
    if len(out) > MAX_OUTPUT_MODEL:
        out = out[:MAX_OUTPUT_MODEL] + "\n…(afgekapt, gebruik head/grep om gerichter te kijken)"
    return out


def for_telegram(out: str) -> str:
    """Weergave voor de telefoon, in max MAX_TG_MESSAGES berichten; het model krijgt de volledige output."""
    if len(out) <= MAX_OUTPUT_TG:
        return out
    rest = len(out) - MAX_OUTPUT_TG
    return out[:MAX_OUTPUT_TG] + f"\n…(nog {rest} tekens; het model heeft alles)"


async def send_long(msg, text: str, pre: bool = False):
    """Stuur tekst in stukken van max 4000 tekens."""
    for i in range(0, len(text), 4000):
        chunk = text[i : i + 4000]
        if pre:
            await msg.reply_text(f"<pre>{html.escape(chunk)}</pre>", parse_mode="HTML")
        else:
            await msg.reply_text(chunk)


async def offer_commands(msg, cmds: list[str], ctx=None, chat_id=None):
    for cmd in cmds:
        if AUTO_RUN_READONLY and is_readonly(cmd):
            await msg.reply_text(f"\u25b6\ufe0f <pre>{html.escape(cmd)}</pre>", parse_mode="HTML")
            out = await asyncio.to_thread(run_sync, cmd)
            await send_long(msg, for_telegram(out), pre=True)
            if ctx is not None and chat_id is not None:
                feedback = f"Output van:\n{cmd}\n\n{out}\n\nGa verder, of zeg dat het klaar is."
                await ctx.bot.send_chat_action(chat_id, "typing")
                try:
                    reply = await asyncio.to_thread(ask_llm_sync, chat_id, feedback)
                except Exception as e:
                    await msg.reply_text(f"LLM-fout: {e}")
                    return
                await handle_model_reply(msg, chat_id, reply, ctx)
            return
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


async def handle_model_reply(msg, chat_id: int, reply: str, ctx=None):
    cmds = extract_commands(reply)
    # tekst zonder de codeblokken
    prose = re.sub(r"```.*?```", "", reply, flags=re.S).strip()
    if prose:
        await send_long(msg, prose)
    if cmds:
        await offer_commands(msg, cmds, ctx, chat_id)


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
    await send_long(update.message, for_telegram(out), pre=True)


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
    await handle_model_reply(update.message, chat_id, reply, ctx)


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
    await send_long(q.message, for_telegram(out), pre=True)

    # output teruggeven aan het model zodat het de volgende stap kan voorstellen
    chat_id = q.message.chat_id
    await ctx.bot.send_chat_action(chat_id, "typing")
    feedback = f"Output van:\n{cmd}\n\n{out}\n\nGa verder, of zeg dat het klaar is."
    try:
        reply = await asyncio.to_thread(ask_llm_sync, chat_id, feedback)
    except Exception as e:
        await q.message.reply_text(f"LLM-fout: {e}")
        return
    await handle_model_reply(q.message, chat_id, reply, ctx)


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
