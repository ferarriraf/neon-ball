"""Bot de repost Instagram -> TikTok.

Tourne en boucle : toutes les N heures (12h par défaut), il liste les vidéos
du compte Instagram, repère celles pas encore publiées sur TikTok (état dans
state.json) et en publie jusqu'à max_posts_per_run, de la plus ancienne à la
plus récente — ce qui rattrape l'historique puis suit les nouveautés.
"""

import json
import logging
import os
import sys
import time

from instagram_source import fetch_video_posts, download_video
from tiktok_client import TikTokClient, TikTokError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("bot")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.json")
STATE_PATH = os.environ.get("STATE_PATH", "state.json")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "downloads")


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


def run_cycle(config, tiktok, state):
    schedule = config.get("schedule", {})
    max_posts = schedule.get("max_posts_per_run", 2)
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
    for field in ("client_key", "client_secret", "refresh_token"):
        if not tk_cfg.get(field) or tk_cfg[field].startswith("COLLE_ICI"):
            log.error("config.json : le champ tiktok.%s n'est pas rempli.", field)
            sys.exit(1)

    tiktok = TikTokClient(
        client_key=tk_cfg["client_key"],
        client_secret=tk_cfg["client_secret"],
        refresh_token=tk_cfg["refresh_token"],
        token_store_path=os.environ.get("TOKENS_PATH", "tokens.json"),
        privacy_level=tk_cfg.get("privacy_level", "SELF_ONLY"),
    )
    state = load_state()
    interval = config.get("schedule", {}).get("check_interval_hours", 12) * 3600

    log.info("Bot démarré : vérification toutes les %.1f heures", interval / 3600)
    while True:
        try:
            run_cycle(config, tiktok, state)
        except Exception as exc:
            log.error("Cycle en échec : %s", exc)
        log.info("Prochain cycle dans %.1f heures", interval / 3600)
        time.sleep(interval)


if __name__ == "__main__":
    main()
