"""
Telegram-bot die praat met een lokale llama-server en (na jouw bevestiging)
shell-commando's uitvoert op de server waar de bot draait.

Config via environment variables:
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

CMD_TIMEOUT = 300        # seconden per commando (standaard)
CMD_TIMEOUT_LANG = 1800  # seconden voor bouwen/installeren (gradle, cmake, pip, apt, tests)
MAX_OUTPUT_MODEL = 40000   # tekens commando-output die het model te zien krijgt (~10K tokens)
MAX_TG_MESSAGES = 3        # max aantal Telegram-berichten (à 4000 tekens) per commando-output
MAX_OUTPUT_TG = MAX_TG_MESSAGES * 4000
AUTO_RUN_READONLY = True   # alleen-lezen commando's direct uitvoeren, zonder bevestigingsknop
DENK_STANDAARD = False     # denkstap standaard aan/uit; per chat te wisselen met /denk
AUTO_STAPPEN_STANDAARD = 15  # aantal commando's dat /auto zonder knop uitvoert
AUTO_STAPPEN_MAX = 50
MAX_HISTORY_CHARS = 200000 # totale grootte van het gespreksgeheugen (~50K tokens)

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
- Schrijf nooit meer dan ~150 regels in één codeblok. Grotere bestanden bouw je in meerdere
  stappen op (eerst deel 1 met `cat >`, daarna aanvullen met `cat >>`), zodat je antwoord niet
  halverwege wordt afgekapt.
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
denken: dict[int, bool] = {}            # chat_id -> denkstap aan/uit
planmodus: set[int] = set()             # chats die in plan-modus staan
auto_resterend: dict[int, int] = {}     # chat_id -> aantal commando's dat nog automatisch mag
laatste_finish: dict[int, str] = {}     # chat_id -> finish_reason van het laatste antwoord
pending: dict[str, str] = {}            # callback-id -> commando


# ---------- veiligheidschecks ----------

# Bewust NIET op deze lijst (kunnen ondanks hun onschuldige naam schrijven of
# andere programma's starten):
#   awk   -> BEGIN{system("...")} en print > bestand
#   xargs -> voert elk willekeurig commando uit
#   env   -> `env <programma>` start van alles
#   find  -> staat er wél op, maar met een vlaggencheck hieronder
READONLY_CMDS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "find", "sed",
    "wc", "cut", "sort", "uniq", "tr", "file", "stat", "du", "df", "pwd", "echo", "which",
    "type", "basename", "dirname", "readlink", "realpath", "date", "whoami", "id",
    "printenv", "hostname", "uname", "ps", "free", "uptime", "tree", "diff", "cmp", "md5sum",
    "sha256sum", "column", "nl", "jq", "true", "cd", "test",
}
# find-vlaggen die bestanden aanpassen of programma's starten
FIND_ONVEILIG = {"-delete", "-exec", "-execdir", "-ok", "-okdir",
                 "-fls", "-fprint", "-fprint0", "-fprintf"}
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

# Nooit automatisch, ook niet in /auto-modus: onomkeerbaar, systeembreed of buiten het werkgebied.
NOOIT_AUTO = re.compile(
    r"\brm\s+(-\w*\s+)*-\w*[rf]|\bmkfs|\bdd\s|\bshutdown\b|\breboot\b|\bhalt\b|"
    r"\buserdel\b|\bpasswd\b|\bvisudo\b|\bcrontab\b|\bchown\b|\biptables\b|"
    r"\bgit\s+(push|reset\s+--hard|clean|checkout\s+\.)|\bgit\s+\w*\s*-*\w*\s*push\b|"
    r"\bcurl\b.*\|\s*(ba)?sh|\bwget\b.*\|\s*(ba)?sh|\bpip\s+install|\bapt(-get)?\s+(install|remove|purge)|"
    r"\bsystemctl\s+(stop|disable|mask)|\btruncate\b|>\s*/dev/|\bchmod\s+(-\w+\s+)*777|"
    r"/etc/|/root/|~/\.ssh|\.env\b|bot_data|app_releases"
)


def mag_automatisch(cmd: str) -> bool:
    """In /auto-modus: alles behalve de onomkeerbare/systeembrede dingen hierboven."""
    return not NOOIT_AUTO.search(cmd)


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
            # subcommando zoeken: opties overslaan, incl. hun waarde bij -C/-c/-u/-n
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
        if base == "find":        # -delete/-exec e.d. schrijven of starten iets
            if any(t in FIND_ONVEILIG for t in tokens[1:]):
                return False
            continue
        if base == "sed":         # -i schrijft; een w- of e-commando in het script ook
            if any(t == "-i" or t.startswith("-i") for t in tokens[1:]):
                return False
            script = " ".join(t for t in tokens[1:] if not t.startswith("-"))
            if re.search(r"(^|[;{}\s/])[we]([\s/]|$)", script):
                return False
            continue
        if base not in READONLY_CMDS:
            return False
    return True


def is_allowed(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == ALLOWED_ID


# ---------- model ----------

def get_history(chat_id: int) -> list[dict]:
    return history.setdefault(chat_id, [{"role": "system", "content": SYSTEM_PROMPT}])


PLAN_INSTRUCTIE = (
    "\n\n[PLAN-MODUS] Voer nu NIETS uit. Geef alleen een plan in gewone taal: wat je zou doen, "
    "in welke stappen, welke bestanden je raakt en waar het mis kan gaan. Geen ```sh blokken. "
    "Wacht op akkoord."
)


def ask_llm_sync(chat_id: int, text: str) -> str:
    msgs = get_history(chat_id)
    if chat_id in planmodus:
        text = text + PLAN_INSTRUCTIE
    msgs.append({"role": "user", "content": text})
    denk = denken.get(chat_id, DENK_STANDAARD)
    payload = {
        "model": "local", "messages": msgs, "temperature": 0.3, "max_tokens": 8000,
        "chat_template_kwargs": {"enable_thinking": denk},
    }
    if denk:
        payload["reasoning_budget"] = 2048
    r = requests.post(f"{API_BASE}/chat/completions", json=payload, timeout=900)
    r.raise_for_status()
    _choice = r.json()["choices"][0]
    reply = _choice["message"]["content"] or ""
    laatste_finish[chat_id] = _choice.get("finish_reason", "")
    if chat_id in planmodus:
        msgs[-1]["content"] = msgs[-1]["content"].replace(PLAN_INSTRUCTIE, "")
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


def onafgemaakt_blok(text: str) -> bool:
    """True als er een codeblok is geopend maar niet gesloten (antwoord afgekapt)."""
    return text.count("```") % 2 == 1


# ---------- uitvoeren ----------

# Commando's die lang mogen duren (builds, dependency-downloads, testsuites)
LANGZAAM = re.compile(r"\bgradle\b|\bgradlew\b|\bcmake\b|\bmake\b|\bnpm\b|\byarn\b|"
                      r"\bpip\s+install|\bapt(-get)?\s|\bpytest\b|test_\w+\.py|\bmvn\b|"
                      r"\bcargo\b|\bgo\s+build|\bdocker\s+build")


def timeout_voor(cmd: str) -> int:
    return CMD_TIMEOUT_LANG if LANGZAAM.search(cmd) else CMD_TIMEOUT


def run_sync(cmd: str) -> str:
    tmo = timeout_voor(cmd)
    try:
        p = subprocess.run(
            cmd, shell=True, cwd=WORKDIR, capture_output=True, text=True, timeout=tmo
        )
        out = (p.stdout + p.stderr).strip() or "(geen output)"
        out += f"\n[exit {p.returncode}]"
    except subprocess.TimeoutExpired:
        out = (f"(afgebroken: timeout na {tmo}s. Duurt dit normaal langer, draai het dan in de "
               f"achtergrond met nohup en schrijf de output naar een logbestand, "
               f"bijv. `nohup <cmd> > /tmp/build.log 2>&1 &` en lees daarna dat log.)")
    if len(out) > MAX_OUTPUT_MODEL:
        out = out[:MAX_OUTPUT_MODEL] + "\n…(afgekapt, gebruik head/grep om gerichter te kijken)"
    return out


def for_telegram(out: str) -> str:
    """Weergave voor de telefoon; het model krijgt de volledige output."""
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
        auto_over = auto_resterend.get(chat_id, 0) if chat_id is not None else 0
        auto_nu = auto_over > 0 and mag_automatisch(cmd)
        if auto_nu and chat_id is not None:
            auto_resterend[chat_id] = auto_over - 1
            if auto_resterend[chat_id] == 0:
                await msg.reply_text(
                    "\u26a1 auto-budget op \u2014 vanaf nu weer bevestigen (/auto voor meer)")
        if (AUTO_RUN_READONLY and is_readonly(cmd)) or auto_nu:
            merk = "\u25b6\ufe0f" if not auto_nu else f"\u26a1 auto ({auto_resterend.get(chat_id, 0)} over)"
            await msg.reply_text(f"{merk} <pre>{html.escape(cmd)}</pre>", parse_mode="HTML")
            if timeout_voor(cmd) > CMD_TIMEOUT:
                await msg.reply_text("\u23f3 dit kan een paar minuten duren\u2026")
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
        await msg.reply_text(f"<pre>{html.escape(cmd)}</pre>", parse_mode="HTML", reply_markup=kb)


async def handle_model_reply(msg, chat_id: int, reply: str, ctx=None, diepte: int = 0):
    cmds = extract_commands(reply)
    afgekapt = laatste_finish.get(chat_id) == "length" or onafgemaakt_blok(reply)
    prose = re.sub(r"```.*?```", "", reply, flags=re.S).strip()
    if prose:
        await send_long(msg, prose)

    # antwoord liep tegen de tokenlimiet: automatisch laten afmaken
    if afgekapt and not cmds and ctx is not None and diepte < 3:
        await msg.reply_text("\u2702\ufe0f antwoord afgekapt \u2014 laat het afmaken\u2026")
        await ctx.bot.send_chat_action(chat_id, "typing")
        try:
            vervolg = await asyncio.to_thread(
                ask_llm_sync, chat_id,
                "Je vorige antwoord werd afgekapt. Geef de rest, korter, en zet elk commando "
                "in een compleet ```sh blok. Splits grote bestanden in meerdere stappen.")
        except Exception as e:
            await msg.reply_text(f"LLM-fout: {e}")
            return
        await handle_model_reply(msg, chat_id, vervolg, ctx, diepte + 1)
        return

    if cmds and chat_id in planmodus:
        await msg.reply_text("(plan-modus: commando's niet uitgevoerd — /doe om verder te gaan)")
    elif cmds:
        await offer_commands(msg, cmds, ctx, chat_id)
    elif chat_id not in planmodus and not afgekapt:
        # aankondiging zonder commando: eenmalig aanporren zodat de keten niet stilvalt
        klaar = any(w in reply.lower() for w in ("klaar", "afgerond", "voltooid", "gereed", "?"))
        if not klaar and ctx is not None and diepte < 2:
            await ctx.bot.send_chat_action(chat_id, "typing")
            try:
                vervolg = await asyncio.to_thread(
                    ask_llm_sync, chat_id,
                    "Voer die stap nu uit: geef het commando in een ```sh blok, of zeg dat je klaar bent.")
            except Exception as e:
                await msg.reply_text(f"LLM-fout: {e}")
                return
            await handle_model_reply(msg, chat_id, vervolg, ctx, diepte + 1)


# ---------- handlers ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Bot draait. Stuur een opdracht in gewone taal.\n"
        "/run <cmd> – zelf direct een commando draaien\n"
        "/plan <vraag> – alleen meedenken, niets uitvoeren\n"
        "/doe – plan-modus uit\n"
        "/denk aan|uit – denkstap voor lastige vragen\n"
        "/auto [n] – n commando's zonder bevestiging (standaard 15)\n"
        "/stop – auto-modus uit\n"
        "/ga – verder waar hij gebleven was\n"
        "/reset – gespreksgeheugen wissen"
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    history.pop(update.effective_chat.id, None)
    planmodus.discard(update.effective_chat.id)
    auto_resterend.pop(update.effective_chat.id, None)
    await update.message.reply_text("Geheugen gewist.")


async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Plan-modus aan: het model denkt mee maar voert niets uit."""
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    planmodus.add(chat_id)
    vraag = " ".join(ctx.args).strip()
    if not vraag:
        await update.message.reply_text(
            "Plan-modus aan. Stel je vraag; er wordt niets uitgevoerd. /doe zet hem uit.")
        return
    await ctx.bot.send_chat_action(chat_id, "typing")
    try:
        reply = await asyncio.to_thread(ask_llm_sync, chat_id, vraag)
    except Exception as e:
        await update.message.reply_text(f"LLM-fout: {e}")
        return
    await handle_model_reply(update.message, chat_id, reply, ctx)


async def cmd_doe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Plan-modus uit."""
    if not is_allowed(update):
        return
    planmodus.discard(update.effective_chat.id)
    await update.message.reply_text("Plan-modus uit. Voer het plan uit of stel een nieuwe vraag.")


async def cmd_ga(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Porren als het model is blijven hangen."""
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id, "typing")
    try:
        reply = await asyncio.to_thread(ask_llm_sync, chat_id, "Ga verder waar je gebleven was.")
    except Exception as e:
        await update.message.reply_text(f"LLM-fout: {e}")
        return
    await handle_model_reply(update.message, chat_id, reply, ctx)


async def cmd_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Auto-modus: commando's zonder bevestiging, voor een beperkt aantal stappen."""
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    try:
        n = int(ctx.args[0]) if ctx.args else AUTO_STAPPEN_STANDAARD
    except ValueError:
        n = AUTO_STAPPEN_STANDAARD
    n = max(1, min(n, AUTO_STAPPEN_MAX))
    auto_resterend[chat_id] = n
    await update.message.reply_text(
        f"\u26a1 Auto-modus aan voor {n} commando's. /stop zet hem meteen uit.\n"
        "Onomkeerbare dingen (rm -rf, git push, systeemmappen) vragen nog steeds bevestiging.\n"
        "Tip: commit eerst, dan kun je altijd terug."
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Auto-modus meteen uit."""
    if not is_allowed(update):
        return
    auto_resterend.pop(update.effective_chat.id, None)
    await update.message.reply_text("Auto-modus uit. Commando's vragen weer bevestiging.")


async def cmd_denk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Denkstap aan/uit voor deze chat."""
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    arg = (ctx.args[0].lower() if ctx.args else "")
    if arg in ("aan", "on", "1"):
        denken[chat_id] = True
    elif arg in ("uit", "off", "0"):
        denken[chat_id] = False
    else:
        denken[chat_id] = not denken.get(chat_id, DENK_STANDAARD)
    aan = denken[chat_id]
    await update.message.reply_text(
        f"Denkstap {'aan' if aan else 'uit'}."
        + (" Antwoorden worden trager maar doordachter." if aan else " Sneller, minder tokens.")
    )


async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Commando dat je zelf typt: direct uitvoeren, geen bevestiging nodig."""
    if not is_allowed(update):
        return
    cmd = " ".join(ctx.args).strip()
    if not cmd:
        await update.message.reply_text("Gebruik: /run <commando>")
        return
    if timeout_voor(cmd) > CMD_TIMEOUT:
        await update.message.reply_text("\u23f3 dit kan een paar minuten duren\u2026")
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
    if timeout_voor(cmd) > CMD_TIMEOUT:
        await q.message.reply_text("\u23f3 dit kan een paar minuten duren\u2026")
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
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("doe", cmd_doe))
    app.add_handler(CommandHandler("denk", cmd_denk))
    app.add_handler(CommandHandler("auto", cmd_auto))
    app.add_handler(CommandHandler("ga", cmd_ga))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print(f"Bot gestart. Werkmap: {WORKDIR}, LLM: {API_BASE}")
    app.run_polling()


if __name__ == "__main__":
    main()
