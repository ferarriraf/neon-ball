"""Bot de repost Instagram -> TikTok.

Tourne en boucle : à chaque créneau configuré (10h et 17h par défaut, décalé
aléatoirement dans une fenêtre pour ne pas poster à heure fixe), il liste les
vidéos du compte Instagram, repère celles pas encore publiées sur TikTok
(état dans state.json) et en publie jusqu'à max_posts_per_run, de la plus
ancienne à la plus récente — ce qui rattrape l'historique puis suit les
nouveautés.
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

from instagram_source import fetch_video_posts, download_video
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
        return json.load(f)


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


def build_caption(post, caption_cfg):
    caption = post["caption"].strip()
    extra = caption_cfg.get("append_hashtags", "").strip()
    if extra:
        caption = f"{caption}\n{extra}" if caption else extra
    max_length = caption_cfg.get("max_length", 2200)
    if len(caption) > max_length:
        caption = caption[: max_length - 1].rstrip() + "…"
    return caption


def bootstrap_tokens(tk_cfg):
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


def run_cycle(config, tiktok, state):
    schedule = config.get("schedule", {})
    max_posts = schedule.get("max_posts_per_run", 1)
    pause = schedule.get("seconds_between_posts", 120)
    caption_cfg = config.get("caption", {})

    posts = fetch_video_posts(config["instagram"]["username"])
    pending = [p for p in posts if p["shortcode"] not in state["posted"]]
    log.info("%d vidéo(s) en attente de repost", len(pending))

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    published = 0
    for post in pending[:max_posts]:
        if published:
            time.sleep(pause)
        video_path = os.path.join(DOWNLOAD_DIR, f"{post['shortcode']}.mp4")
        try:
            log.info("Téléchargement de la vidéo Instagram %s (%s)", post["shortcode"], post["date"])
            download_video(post["video_url"], video_path)
            title = build_caption(post, caption_cfg)
            log.info("Upload sur TikTok de %s", post["shortcode"])
            publish_id = tiktok.upload_video(video_path, title)
            state["posted"][post["shortcode"]] = {
                "date": post["date"],
                "publish_id": publish_id,
                "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            save_state(state)
            published += 1
            log.info("Vidéo %s publiée (%d/%d ce cycle)", post["shortcode"], published, max_posts)
        except TikTokError as exc:
            # On s'arrête pour ce cycle : inutile d'enchaîner si TikTok refuse.
            log.error("Erreur TikTok sur %s : %s", post["shortcode"], exc)
            break
        except Exception as exc:
            log.error("Erreur sur %s : %s", post["shortcode"], exc)
        finally:
            if os.path.exists(video_path):
                os.remove(video_path)


def main():
    config = load_config()
    tk_cfg = config["tiktok"]
    for field in ("client_key", "client_secret"):
        if not tk_cfg.get(field) or tk_cfg[field].startswith("COLLE_ICI"):
            log.error("config.json : le champ tiktok.%s n'est pas rempli.", field)
            sys.exit(1)

    refresh_token = tk_cfg.get("refresh_token", "")
    if refresh_token.startswith("COLLE_ICI"):
        refresh_token = ""
    if not refresh_token and not os.path.exists(TOKENS_PATH):
        bootstrap_tokens(tk_cfg)

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

    log.info("Bot démarré : créneaux %s (fenêtre aléatoire de %d min, fuseau %s)",
             ", ".join(schedule.get("post_times", ["10:00", "17:00"])),
             schedule.get("random_window_minutes", 60), tz.key)
    while True:
        now = datetime.now(tz)
        base, run_at = next_slot(schedule, now, state.get("last_slot"))
        wait = (run_at - now).total_seconds()
        log.info("Prochaine publication : %s (dans %.1f h)",
                 run_at.strftime("%d/%m %H:%M"), wait / 3600)
        if wait > 0:
            time.sleep(wait)
        try:
            run_cycle(config, tiktok, state)
        except Exception as exc:
            log.error("Cycle en échec : %s", exc)
        state["last_slot"] = base.isoformat()
        save_state(state)


if __name__ == "__main__":
    main()
