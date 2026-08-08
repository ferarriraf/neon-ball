"""Bot ball escape -> TikTok.

Tourne en boucle : à chaque créneau configuré (10h et 17h par défaut, décalé
aléatoirement dans une fenêtre pour ne pas poster à heure fixe), il génère
une vidéo "ball escape" — la balle joue la mélodie d'une musique piochée au
hasard dans le dossier musics/ à chaque rebond — puis la publie sur TikTok
avec une description tirée en rotation de la liste `captions` de config.json.
"""

import json
import logging
import os
import random
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from generator import generate_video
from notify import Notifier, GREEN, BLUE, RED, ORANGE
from tiktok_client import TikTokClient, TikTokError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("bot")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")
STATE_PATH = os.environ.get("STATE_PATH", "state.json")
TOKENS_PATH = os.environ.get("TOKENS_PATH", "tokens.json")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")
AUTH_REDIRECT_FILE = "auth_redirect.txt"


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log.error(
            "Fichier %s introuvable. Copie config.example.json vers config.json "
            "via le gestionnaire de fichiers Pterodactyl et remplis-le.",
            CONFIG_PATH,
        )
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as exc:
            log.error("config.json invalide : %s (ligne %d, colonne %d).",
                      exc.msg, exc.lineno, exc.colno)
            log.error("Vérifie autour de cette ligne : virgule en fin de ligne "
                      "manquante ou en trop, guillemets \" abîmés par le "
                      "copier-coller, accolade en trop. Compare avec "
                      "config.example.json.")
            sys.exit(1)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"posted": {}}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def next_caption(config, state, song_name):
    """Description suivante de la liste, en rotation, {song} remplacé."""
    captions = config.get("captions") or ["#ballescape #satisfying #fyp"]
    index = state.get("caption_index", 0) % len(captions)
    state["caption_index"] = (index + 1) % len(captions)
    return captions[index].replace("{song}", song_name)[:2200]


def bootstrap_tokens(tk_cfg, notifier):
    """Première connexion TikTok, 100 % depuis le panel Pterodactyl (pas de terminal).

    Affiche l'URL d'autorisation dans les logs du serveur. L'utilisateur
    l'ouvre dans son navigateur (téléphone ou PC), autorise son compte
    TikTok, puis colle l'URL complète de redirection dans un fichier
    auth_redirect.txt créé via le gestionnaire de fichiers du panel.
    Le bot échange alors le code contre les tokens et démarre.
    """
    redirect_uri = tk_cfg.get("redirect_uri", "")
    if not redirect_uri:
        log.error("config.json : remplis tiktok.redirect_uri (la même Redirect URI "
                  "que dans ton app/sandbox TikTok).")
        sys.exit(1)
    scopes = ("user.info.basic,video.upload"
              if tk_cfg.get("post_mode", "direct") == "inbox"
              else "user.info.basic,video.publish")
    auth_url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": tk_cfg["client_key"],
        "response_type": "code",
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": "bootstrap",
    })

    notifier.send(
        "🔑 Autorisation TikTok requise",
        "1. Ouvre ce lien et autorise ton compte :\n" + auth_url +
        "\n2. Copie l'URL complète de redirection (avec ?code=...)\n"
        "3. Colle-la dans un fichier auth_redirect.txt à la racine du serveur "
        "(gestionnaire de fichiers du panel). Le code expire en quelques minutes.",
        ORANGE,
    )
    log.info("=" * 70)
    log.info("PREMIÈRE CONNEXION TIKTOK NÉCESSAIRE")
    log.info("1. Ouvre cette URL dans ton navigateur et autorise ton compte :")
    log.info("   %s", auth_url)
    log.info("2. Après validation tu es redirigé vers %s", redirect_uri)
    log.info("   Copie l'URL COMPLÈTE de la barre d'adresse (elle contient ?code=...)")
    log.info("3. Dans le gestionnaire de fichiers du panel, crée le fichier")
    log.info("   auth_redirect.txt et colle cette URL dedans, puis sauvegarde.")
    log.info("=" * 70)

    while not os.path.exists(AUTH_REDIRECT_FILE):
        time.sleep(10)
    with open(AUTH_REDIRECT_FILE, encoding="utf-8") as f:
        pasted = f.read().strip()

    # Accepte l'URL complète de redirection ou juste la valeur du code.
    if "code=" in pasted:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        code = query.get("code", [""])[0]
    else:
        code = pasted
    if not code:
        log.error("auth_redirect.txt ne contient pas de code : supprime le fichier, "
                  "recommence l'autorisation et colle l'URL complète.")
        os.remove(AUTH_REDIRECT_FILE)
        sys.exit(1)

    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": tk_cfg["client_key"],
            "client_secret": tk_cfg["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    data = resp.json()
    os.remove(AUTH_REDIRECT_FILE)
    if "refresh_token" not in data:
        log.error("Échange du code refusé par TikTok : %s", data)
        log.error("Le code n'est valable que quelques minutes : recommence "
                  "l'autorisation (le bot va redémarrer la procédure).")
        sys.exit(1)

    with open(TOKENS_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "refresh_token": data["refresh_token"],
            "access_token": data["access_token"],
            "expires_at": time.time() + data.get("expires_in", 86400),
        }, f, indent=2)
    log.info("✔ Connexion TikTok réussie (scopes : %s), tokens sauvegardés.",
             data.get("scope"))
    notifier.send("✅ Connexion TikTok réussie",
                  f"Scopes : {data.get('scope')}", GREEN)


def next_slot(schedule, now, last_slot):
    """Prochain créneau : heure configurée + décalage aléatoire dans la fenêtre.

    Retourne (base, run_at) où base est le créneau nominal (sert à ne pas
    publier deux fois dans le même créneau après un redémarrage) et run_at
    l'heure effective, décalée aléatoirement.
    """
    times = schedule.get("post_times", ["10:00", "17:00"])
    window = schedule.get("random_window_minutes", 60)
    bases = []
    for day_offset in (0, 1):
        day = now.date() + timedelta(days=day_offset)
        for t in times:
            hour, minute = map(int, t.split(":"))
            bases.append(datetime(day.year, day.month, day.day, hour, minute,
                                  tzinfo=now.tzinfo))
    bases.sort()
    for base in bases:
        if last_slot and base.isoformat() <= last_slot:
            continue
        run_at = base + timedelta(minutes=random.uniform(0, window))
        if run_at > now:
            return base, run_at
    # Tous les créneaux d'aujourd'hui et demain sont passés (impossible en
    # pratique) : on retombe sur le dernier + 1 jour.
    base = bases[-1] + timedelta(days=1)
    return base, base + timedelta(minutes=random.uniform(0, window))


def run_cycle(config, tiktok, state, notifier):
    notifier.send("🎬 Génération d'une vidéo...",
                  "La publication suivra automatiquement à la fin du rendu.", BLUE)
    started = time.time()
    result = generate_video(config, DOWNLOAD_DIR)
    if result is None:
        notifier.send("⚠️ Génération impossible",
                      "Aucune musique lisible dans le dossier musics/ : "
                      "dépose au moins un fichier mp3/wav/m4a/ogg/flac. "
                      "Nouvel essai au prochain créneau.", ORANGE)
        return
    video_path, song_name = result
    notifier.send("🎞️ Vidéo générée",
                  f"Musique : **{song_name}**\n"
                  f"Rendu en {(time.time() - started) / 60:.1f} min. Upload en cours...",
                  BLUE)
    try:
        caption = next_caption(config, state, song_name)
        log.info("Upload sur TikTok (musique : %s)", song_name)
        publish_id = tiktok.upload_video(video_path, caption)
        state.setdefault("posted", {})[os.path.basename(video_path)] = {
            "song": song_name,
            "publish_id": publish_id,
            "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        log.info("Vidéo publiée (publish_id %s)", publish_id)
        notifier.send("🚀 Publiée sur TikTok !",
                      f"Musique : **{song_name}**\nDescription : {caption}\n"
                      f"publish_id : `{publish_id}`", GREEN)
    except TikTokError as exc:
        log.error("Erreur TikTok : %s", exc)
        notifier.send("❌ Échec de la publication TikTok", str(exc), RED)
    finally:
        save_state(state)
        if os.path.exists(video_path):
            os.remove(video_path)


def main():
    config = load_config()
    tk_cfg = config["tiktok"]
    for field in ("client_key", "client_secret"):
        if not tk_cfg.get(field) or tk_cfg[field].startswith("COLLE_ICI"):
            log.error("config.json : le champ tiktok.%s n'est pas rempli.", field)
            sys.exit(1)

    notifier = Notifier(config.get("discord_webhook_url", ""))

    refresh_token = tk_cfg.get("refresh_token", "")
    if refresh_token.startswith("COLLE_ICI"):
        refresh_token = ""
    if not refresh_token and not os.path.exists(TOKENS_PATH):
        bootstrap_tokens(tk_cfg, notifier)

    tiktok = TikTokClient(
        client_key=tk_cfg["client_key"],
        client_secret=tk_cfg["client_secret"],
        refresh_token=refresh_token,
        token_store_path=TOKENS_PATH,
        privacy_level=tk_cfg.get("privacy_level", "SELF_ONLY"),
        post_mode=tk_cfg.get("post_mode", "direct"),
    )
    state = load_state()
    schedule = config.get("schedule", {})
    tz = ZoneInfo(schedule.get("timezone", "Europe/Paris"))

    # Nettoie les vidéos temporaires laissées par un éventuel crash.
    if os.path.isdir(DOWNLOAD_DIR):
        for name in os.listdir(DOWNLOAD_DIR):
            path = os.path.join(DOWNLOAD_DIR, name)
            if os.path.isfile(path):
                os.remove(path)

    slots_txt = ", ".join(schedule.get("post_times", ["10:00", "17:00"]))
    log.info("Bot démarré : créneaux %s (fenêtre aléatoire de %d min, fuseau %s)",
             slots_txt, schedule.get("random_window_minutes", 60), tz.key)
    now = datetime.now(tz)
    _, first_run_at = next_slot(schedule, now, state.get("last_slot"))
    notifier.send("🤖 Bot en ligne",
                  f"Créneaux : {slots_txt} ({tz.key})\n"
                  f"Prochaine vidéo planifiée : {first_run_at.strftime('%d/%m vers %H:%M')}",
                  GREEN)

    if schedule.get("post_on_start", True):
        log.info("Vidéo de démarrage dans 30 secondes...")
        time.sleep(30)
        try:
            run_cycle(config, tiktok, state, notifier)
        except Exception as exc:
            log.error("Vidéo de démarrage en échec : %s", exc)
            notifier.send("❌ Vidéo de démarrage en échec", str(exc), RED)

    while True:
        now = datetime.now(tz)
        base, run_at = next_slot(schedule, now, state.get("last_slot"))
        wait = (run_at - now).total_seconds()
        log.info("Prochaine publication : %s (dans %.1f h)",
                 run_at.strftime("%d/%m %H:%M"), wait / 3600)
        if wait > 0:
            time.sleep(wait)
        try:
            run_cycle(config, tiktok, state, notifier)
        except Exception as exc:
            log.error("Cycle en échec : %s", exc)
            notifier.send("❌ Cycle en échec",
                          f"{exc}\nNouvelle tentative dans 3 minutes...", RED)
            time.sleep(180)
            try:
                run_cycle(config, tiktok, state, notifier)
            except Exception as exc2:
                log.error("Deuxième échec : %s", exc2)
                notifier.send("❌ Deuxième échec",
                              f"{exc2}\nOn attend le prochain créneau.", RED)
        state["last_slot"] = base.isoformat()
        save_state(state)


if __name__ == "__main__":
    main()
