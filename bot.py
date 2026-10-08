"""
Asistente de @AIMadeClear para X
--------------------------------
- Revisa los comentarios nuevos en tus posts y te manda a Telegram una
  respuesta en inglés (con traducción al español) y botones para publicarla,
  editarla, pedir otra versión o ignorarla.
- Vigila las cuentas que elijas y te sugiere respuestas para que las
  publiques tú desde X.
Nada se publica sin que tú lo apruebes.
"""
import os
import re
import json
import time
import asyncio
import logging
import threading

import requests
import tweepy
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, ContextTypes, filters)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("asistente")

# ---------- Configuración (variables del servidor) ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CHAT_ID = int(_chat) if _chat else None
MODELO = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
CUENTAS = [c.strip().lstrip("@") for c in os.environ.get("WATCH_ACCOUNTS", "").split(",") if c.strip()]
MIN_COMENTARIOS = int(os.environ.get("MENTIONS_EVERY_MIN", "15"))
MIN_CUENTAS = int(os.environ.get("WATCH_EVERY_MIN", "120"))
ARCHIVO_ESTADO = os.environ.get("STATE_FILE", "estado.json")
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"

ESTILO = """You write the replies for the X account @AIMadeClear, which explains AI
in plain English. Write in the voice of the account owner, a person who follows AI closely.
Voice:
- First person, casual and confident. Never say or hint that the account is an AI or a bot.
- Punchy, curious and a bit provocative: a strong first line, bold but defensible
  opinions, and when it fits, a question that invites people to reply.
- Witty and friendly, never rude. No fearmongering, no insults.
Hard rules:
- Never invent facts, numbers, quotes, events or personal experiences
  (no "I tried...", "I tested..." unless the original text says so).
  If you are not sure something is true, do not state it as fact.
- Max 240 characters. No hashtags. At most one emoji.
- No generic lines like "Great post!" and never ask people to follow.
- If the text is spam, an insult or not worth answering, the reply is just: IGNORE"""

X = None
CLAUDE = None
ESTADO = {}
_candado_x = threading.Lock()
_candado_estado = threading.Lock()


# ---------- Guardar y cargar estado ----------
def cargar():
    try:
        with open(ARCHIVO_ESTADO, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("pendientes", {})
    st.setdefault("cuentas_desde", {})
    st.setdefault("ids_cuentas", {})
    return st


def guardar():
    with _candado_estado:
        pend = ESTADO["pendientes"]
        while len(pend) > 200:  # no dejar crecer el archivo sin fin
            pend.pop(next(iter(pend)))
        carpeta = os.path.dirname(ARCHIVO_ESTADO)
        if carpeta:
            os.makedirs(carpeta, exist_ok=True)
        tmp = ARCHIVO_ESTADO + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ESTADO, f, ensure_ascii=False)
        os.replace(tmp, ARCHIVO_ESTADO)


# ---------- Acceso a X (OAuth 2.0 con renovación automática) ----------
def _renovar_token_x():
    cid = os.environ["X_CLIENT_ID"].strip()
    secreto = os.environ["X_CLIENT_SECRET"].strip()
    r = requests.post(
        X_TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": ESTADO["x_refresh"], "client_id": cid},
        auth=(cid, secreto),
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"No pude renovar el acceso a X ({r.status_code}). Genera un token nuevo en "
            f"console.x.com y ponlo en X_REFRESH_TOKEN. Detalle: {r.text[:200]}")
    d = r.json()
    ESTADO["x_access"] = d["access_token"]
    ESTADO["x_refresh"] = d.get("refresh_token", ESTADO["x_refresh"])
    ESTADO["x_expira"] = time.time() + int(d.get("expires_in", 7200))
    guardar()
    log.info("Acceso a X renovado")


def x():
    """Cliente de X con un token válido; lo renueva cuando está por caducar."""
    global X
    with _candado_x:
        if not ESTADO.get("x_access") or time.time() > ESTADO.get("x_expira", 0) - 300:
            _renovar_token_x()
            X = None
        if X is None:
            X = tweepy.Client(bearer_token=ESTADO["x_access"])
        return X


# ---------- Claude ----------
def _texto(r):
    return "".join(b.text for b in r.content if b.type == "text").strip()


def _etiqueta(nombre, texto):
    m = re.search(rf"<{nombre}>(.*?)</{nombre}>", texto, re.S)
    return m.group(1).strip() if m else ""


def generar(texto, autor, propio):
    """Devuelve (respuesta en inglés, traducción del texto, traducción de la respuesta)."""
    situacion = ("This is a comment someone left on one of our posts. Reply to it."
                 if propio else
                 "This is a new post from an account in our niche. Write a reply that adds value.")
    r = CLAUDE.messages.create(
        model=MODELO,
        max_tokens=700,
        system=ESTILO,
        messages=[{"role": "user", "content":
                   f"{situacion}\n\nAuthor: @{autor}\nText:\n{texto}\n\n"
                   "Answer using exactly this format:\n"
                   "<reply>your reply in English, or IGNORE</reply>\n"
                   "<texto_es>Spanish translation of the original text</texto_es>\n"
                   "<respuesta_es>Spanish translation of your reply</respuesta_es>"}],
    )
    out = _texto(r)
    return (_etiqueta("reply", out) or out, _etiqueta("texto_es", out), _etiqueta("respuesta_es", out))


def pulir(texto):
    """Convierte lo que escribas (en español o inglés) en una respuesta en inglés natural."""
    r = CLAUDE.messages.create(
        model=MODELO,
        max_tokens=500,
        system=ESTILO,
        messages=[{"role": "user", "content":
                   "The account owner wrote this reply. If it is not in English, translate it into "
                   "natural English keeping the meaning. If it is in English, only fix typos.\n\n"
                   f"{texto}\n\nAnswer using exactly this format:\n"
                   "<reply>final reply in English</reply>\n"
                   "<respuesta_es>Spanish translation of the final reply</respuesta_es>"}],
    )
    out = _texto(r)
    return _etiqueta("reply", out) or out, _etiqueta("respuesta_es", out)


# ---------- Telegram: mostrar un borrador ----------
async def mostrar(bot, tid, p):
    link = f"https://x.com/{p['autor']}/status/{tid}"
    ignorar = p["borrador"].strip().upper().rstrip(".") == "IGNORE"
    if p["propio"]:
        cabecera = f"💬 Comentario de @{p['autor']}"
        botones = [
            [InlineKeyboardButton("✅ Publicar", callback_data=f"pub:{tid}"),
             InlineKeyboardButton("✏️ Editar", callback_data=f"edit:{tid}")],
            [InlineKeyboardButton("🔄 Otra versión", callback_data=f"otra:{tid}"),
             InlineKeyboardButton("🗑 Ignorar", callback_data=f"ign:{tid}")],
        ]
    else:
        cabecera = f"👀 Post nuevo de @{p['autor']}"
        botones = [
            [InlineKeyboardButton("🔗 Abrir post en X", url=link)],
            [InlineKeyboardButton("🔄 Otra versión", callback_data=f"otra:{tid}"),
             InlineKeyboardButton("🗑 Descartar", callback_data=f"ign:{tid}")],
        ]
    if ignorar:
        nota = "🤖 Sugerencia: no responder (parece spam o no aporta)."
    elif p["propio"]:
        nota = "✍️ Respuesta propuesta (va en inglés en el siguiente mensaje)."
    else:
        nota = ("✍️ Respuesta sugerida (va en inglés en el siguiente mensaje). "
                "Mantenlo pulsado para copiarla, abre el post y pégala.")
    traduccion = f"\n🇪🇸 {p['texto_es']}" if p.get("texto_es") else ""
    trad_resp = f"\n🇪🇸 En español dice: {p['borrador_es']}" if p.get("borrador_es") and not ignorar else ""
    await bot.send_message(CHAT_ID, f"{cabecera}\n\n{p['texto']}{traduccion}\n\n{link}\n\n{nota}{trad_resp}")
    await bot.send_message(CHAT_ID, p["borrador"], reply_markup=InlineKeyboardMarkup(botones))


# ---------- Revisar comentarios en tus posts ----------
async def revisar_comentarios(bot):
    if not ESTADO.get("mi_id"):
        yo = await asyncio.to_thread(lambda: x().get_me(user_auth=False))
        ESTADO["mi_id"] = str(yo.data.id)
        guardar()
    mi_id = ESTADO["mi_id"]
    args = dict(id=mi_id, max_results=10, expansions="author_id", user_fields="username")
    if ESTADO.get("comentarios_desde"):
        args["since_id"] = ESTADO["comentarios_desde"]
    r = await asyncio.to_thread(lambda: x().get_users_mentions(**args, user_auth=False))
    if not r.data:
        return 0
    ESTADO["comentarios_desde"] = str(r.meta["newest_id"])
    usuarios = {str(u.id): u.username for u in (r.includes or {}).get("users", [])}
    nuevos = 0
    for t in reversed(r.data):
        if str(t.author_id) == mi_id:
            continue
        autor = usuarios.get(str(t.author_id), "usuario")
        borrador, texto_es, borrador_es = await asyncio.to_thread(generar, t.text, autor, True)
        p = {"texto": t.text, "texto_es": texto_es, "autor": autor,
             "borrador": borrador, "borrador_es": borrador_es, "propio": True}
        ESTADO["pendientes"][str(t.id)] = p
        guardar()
        await mostrar(bot, str(t.id), p)
        nuevos += 1
    guardar()
    return nuevos


# ---------- Vigilar otras cuentas ----------
async def revisar_cuentas(bot):
    nuevos = 0
    for usuario in CUENTAS:
        uid = ESTADO["ids_cuentas"].get(usuario)
        if not uid:
            u = await asyncio.to_thread(lambda: x().get_user(username=usuario, user_auth=False))
            if not u.data:
                log.warning("No encontré la cuenta @%s", usuario)
                continue
            uid = str(u.data.id)
            ESTADO["ids_cuentas"][usuario] = uid
        args = dict(id=uid, max_results=5, exclude=["retweets", "replies"])
        desde = ESTADO["cuentas_desde"].get(usuario)
        if desde:
            args["since_id"] = desde
        r = await asyncio.to_thread(lambda: x().get_users_tweets(**args, user_auth=False))
        if not r.data:
            continue
        ESTADO["cuentas_desde"][usuario] = str(r.meta["newest_id"])
        guardar()
        if not desde:
            continue  # primera vez: solo marca desde dónde empezar
        t = r.data[0]  # el post más reciente
        borrador, texto_es, borrador_es = await asyncio.to_thread(generar, t.text, usuario, False)
        p = {"texto": t.text, "texto_es": texto_es, "autor": usuario,
             "borrador": borrador, "borrador_es": borrador_es, "propio": False}
        ESTADO["pendientes"][str(t.id)] = p
        guardar()
        await mostrar(bot, str(t.id), p)
        nuevos += 1
    return nuevos


async def avisar_error(bot, donde, e):
    log.exception("Error en %s", donde)
    msg = f"⚠️ Error al {donde}: {e}"
    if ESTADO.get("ultimo_error") != msg:  # no repetir el mismo aviso
        ESTADO["ultimo_error"] = msg
        guardar()
        await bot.send_message(CHAT_ID, msg)


async def tarea_comentarios(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await revisar_comentarios(ctx.bot)
        ESTADO.pop("ultimo_error", None)
    except Exception as e:
        await avisar_error(ctx.bot, "revisar comentarios", e)


async def tarea_cuentas(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        await revisar_cuentas(ctx.bot)
    except Exception as e:
        await avisar_error(ctx.bot, "revisar otras cuentas", e)


# ---------- Comandos y botones de Telegram ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if CHAT_ID is None:
        await update.message.reply_text(
            f"Tu número de chat es:\n\n{cid}\n\nCópialo en la variable TELEGRAM_CHAT_ID.")
    elif cid == CHAT_ID:
        await update.message.reply_text(
            f"👋 Estoy funcionando.\nReviso comentarios cada {MIN_COMENTARIOS} min"
            + (f" y {len(CUENTAS)} cuentas cada {MIN_CUENTAS} min." if CUENTAS else ".")
            + "\nEscribe /revisar para revisar ahora mismo.")


async def cmd_revisar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    await update.message.reply_text("🔎 Revisando…")
    try:
        n = await revisar_comentarios(ctx.bot)
        m = await revisar_cuentas(ctx.bot) if CUENTAS else 0
        if n + m == 0:
            await update.message.reply_text("Nada nuevo por ahora ✅")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")


async def botones(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message.chat_id != CHAT_ID:
        return
    await q.answer()
    accion, tid = q.data.split(":", 1)
    p = ESTADO["pendientes"].get(tid)
    if not p:
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("Ese borrador ya no está disponible.")
        return

    if accion == "pub":
        try:
            await asyncio.to_thread(lambda: x().create_tweet(
                text=p["borrador"], in_reply_to_tweet_id=tid, user_auth=False))
        except Exception as e:
            await q.message.reply_text(f"❌ No se pudo publicar: {e}")
            return
        ESTADO["pendientes"].pop(tid)
        guardar()
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("✅ Respuesta publicada")
    elif accion == "ign":
        ESTADO["pendientes"].pop(tid)
        guardar()
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("🗑 Listo, lo dejo pasar")
    elif accion == "edit":
        ESTADO["editando"] = tid
        guardar()
        await q.message.reply_text(
            "✏️ Escríbeme aquí la respuesta como la quieres (en español o en inglés). "
            "La paso a inglés y te la muestro antes de publicar.")
    elif accion == "otra":
        await q.edit_message_reply_markup(None)
        p["borrador"], p["texto_es"], p["borrador_es"] = await asyncio.to_thread(
            generar, p["texto"], p["autor"], p["propio"])
        guardar()
        await mostrar(ctx.bot, tid, p)


async def texto_libre(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    tid = ESTADO.get("editando")
    if not tid or tid not in ESTADO["pendientes"]:
        await update.message.reply_text("Escribe /revisar para buscar comentarios nuevos.")
        return
    p = ESTADO["pendientes"][tid]
    await update.message.reply_text("✍️ Preparando la versión en inglés…")
    p["borrador"], p["borrador_es"] = await asyncio.to_thread(pulir, update.message.text.strip())
    ESTADO["editando"] = None
    guardar()
    await mostrar(ctx.bot, tid, p)


def main():
    global CLAUDE, ESTADO
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))

    if CHAT_ID is not None:
        CLAUDE = anthropic.Anthropic()  # usa la variable ANTHROPIC_API_KEY
        ESTADO = cargar()
        # Si pusiste un X_REFRESH_TOKEN nuevo en las variables, se usa ese
        token_variable = os.environ["X_REFRESH_TOKEN"].strip()
        if ESTADO.get("x_refresh_origen") != token_variable:
            ESTADO["x_refresh"] = token_variable
            ESTADO["x_refresh_origen"] = token_variable
            ESTADO.pop("x_access", None)
            guardar()
        app.add_handler(CommandHandler("revisar", cmd_revisar))
        app.add_handler(CallbackQueryHandler(botones))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto_libre))
        app.job_queue.run_repeating(tarea_comentarios, interval=MIN_COMENTARIOS * 60, first=15)
        if CUENTAS:
            app.job_queue.run_repeating(tarea_cuentas, interval=MIN_CUENTAS * 60, first=90)
        log.info("Asistente en marcha")
    else:
        log.info("Falta TELEGRAM_CHAT_ID: escribe /start al bot para conocerlo")

    app.run_polling()


if __name__ == "__main__":
    main()
