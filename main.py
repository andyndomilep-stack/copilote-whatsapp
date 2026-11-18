import hashlib
import hmac
import logging
import os
import sqlite3
from datetime import datetime, time
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("copilote")

# --- Configuration (voir .env) ---
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "andy_bot_2026")
APP_SECRET = os.environ.get("APP_SECRET", "")
OWNER_NUMBER = os.environ.get("OWNER_NUMBER", "")
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Africa/Douala"))
ONLINE_FROM = time.fromisoformat(os.environ.get("ONLINE_FROM", "08:00"))
ONLINE_TO = time.fromisoformat(os.environ.get("ONLINE_TO", "22:00"))
AWAY_MODE = os.environ.get("AWAY_MODE", "auto").lower()  # auto | on | off
GRAPH_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"

# --- Tes regles ---
RULES = [
    (["bonjour", "salut", "bonsoir", "hello", "yo", "on dit quoi", "cc"],
     "Salut ! Je suis le copilote d'ANDY. Vraiment navre, mais il ne pourra pas vous repondre tout de suite suite a une indisponibilite."),
]
DEFAULT_REPLY = (
    "Merci pour votre message. ANDY n'est pas disponible "
    "et vous repondra des que possible."
)
URGENT_WORDS = ["urgent", "urgence", "important", "vite", "probleme"]

# --- Base de donnees (anti-doublons) ---
db = sqlite3.connect("copilote.db", check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
db.commit()

app = FastAPI()

# Petite memoire pour afficher les messages dans l'interface
MESSAGES_RECUS = []

def is_away() -> bool:
    if AWAY_MODE == "on":
        return True
    if AWAY_MODE == "off":
        return False
    now = datetime.now(TIMEZONE).time()
    return not (ONLINE_FROM <= now <= ONLINE_TO)

def send_whatsapp(to: str, text: str) -> None:
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        log.error("WHATSAPP_TOKEN ou PHONE_NUMBER_ID manquant dans .env")
        return
    r = requests.post(
        GRAPH_URL,
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text[:4000]},
        },
        timeout=15,
    )
    if not r.ok:
        log.error("Echec envoi whatsapp: %s %s", r.status_code, r.text)

def choose_reply(text: str) -> str:
    t = text.lower()
    for keywords, reply in RULES:
        if any(k in t for k in keywords):
            return reply
    return DEFAULT_REPLY

def handle_message(wa_id: str, name: str, text: str) -> None:
    if OWNER_NUMBER and any(w in text.lower() for w in URGENT_WORDS):
        send_whatsapp(OWNER_NUMBER, f"Message urgent de {name} (+{wa_id}) :\n\n{text}")
    send_whatsapp(wa_id, choose_reply(text))

def process(payload: dict) -> None:
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                names = {
                    c["wa_id"]: c.get("profile", {}).get("name", "Inconnu")
                    for c in value.get("contacts", [])
                }
                for m in value.get("messages", []):
                    wa_id, msg_id = m["from"], m["id"]
                    if wa_id == OWNER_NUMBER:
                        continue
                    if db.execute("SELECT 1 FROM seen WHERE id=?", (msg_id,)).fetchone():
                        continue
                    db.execute("INSERT INTO seen (id) VALUES (?)", (msg_id,))
                    db.commit()

                    # Pour le tableau de bord
                    texte_affichage = m.get("text", {}).get("body", "[message non texte]") if m.get("type") == "text" else "[message non texte]"
                    MESSAGES_RECUS.append({
                        "de": names.get(wa_id, "Inconnu"),
                        "texte": texte_affichage,
                        "heure": datetime.now(TIMEZONE).strftime("%d/%m %H:%M")
                    })

                    if not is_away():
                        continue  # tu es en ligne : le co-pilote reste muet
                    if m.get("type") != "text":
                        send_whatsapp(wa_id, "Je ne peux lire que les messages texte pour l'instant.")
                        continue
                    handle_message(wa_id, names.get(wa_id, "Inconnu"), m["text"]["body"])
    except Exception as e:
        log.error(f"Erreur process: {e}")

@app.get("/webhook")
def verify(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == VERIFY_TOKEN:
        return Response(content=p.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403)

@app.post("/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    if APP_SECRET:
        expected = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("X-Hub-Signature-256", "")):
            raise HTTPException(status_code=403)
    background.add_task(process, await request.json())
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def interface():
    messages_html = ""
    for m in reversed(MESSAGES_RECUS[-20:]):
        messages_html += f"<div style='background:white;padding:10px;margin:5px;border-radius:10px;'><b>{m['de']}</b><br>{m['texte']}<br><small>{m['heure']}</small></div>"

    if not messages_html:
        messages_html = "<p style='color:grey'>Aucun message pour le moment. Envoie un message a ton bot pour le voir ici.</p>"

    return f"""
    <html>
    <head><title>Co-pilote - Tableau de bord</title></head>
    <body style="font-family:Arial;background:#e9f5e9;padding:20px">
        <h1>Co-pilote whatsapp</h1>
        <p style="background:#25D366;color:white;padding:10px;border-radius:10px;display:inline-block">EN LIGNE - Serveur demarre</p>
        <h2>Derniers messages recus :</h2>
        {messages_html}
        <br>
        <button onclick="location.reload()" style="padding:10px 20px;font-size:16px">Rafraichir</button>
        <p><small>Ton bot ecoute sur : /webhook</small></p>
    </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
