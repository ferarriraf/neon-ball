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
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from generator import generate_video, list_songs
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


def shuffle_playlist(config, state):
    """Mélange la liste des morceaux : ordre tiré au sort à chaque démarrage.

    Les morceaux passent ensuite un par un dans cet ordre — donc bien
    espacés, sans doublon — et la liste reprend au début une fois épuisée.
    """
    files = list_songs(config.get("music", {}).get("dir", "musics"))
    random.shuffle(files)
    state["playlist"] = files
    state["playlist_index"] = 0
    return files


def next_song(config, state):
    """Chemin du prochain morceau de la playlist, ou None si le dossier est vide."""
    music_dir = config.get("music", {}).get("dir", "musics")
    available = set(list_songs(music_dir))
    if not available:
        return None, 0, 0
    # On retire les morceaux supprimés depuis le mélange initial.
    playlist = [f for f in state.get("playlist", []) if f in available]
    index = state.get("playlist_index", 0)

    if not playlist:
        playlist = shuffle_playlist(config, state)
        index = 0
    elif index >= len(playlist):
        # Tour terminé : on repart au début, en ajoutant les nouveaux fichiers.
        extras = [f for f in available if f not in playlist]
        random.shuffle(extras)
        playlist += extras
        index = 0

    song = playlist[index]
    state["playlist"] = playlist
    state["playlist_index"] = index + 1
    return os.path.join(music_dir, song), index + 1, len(playlist)


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


# Limite d'upload d'un webhook Discord (10 Mo) ; on vise en dessous.
DISCORD_TARGET_MB = 8.0


def video_duration(path):
    from imageio_ffmpeg import get_ffmpeg_exe
    out = subprocess.run([get_ffmpeg_exe(), "-i", path],
                         capture_output=True, text=True).stderr
    for line in out.splitlines():
        if "Duration:" in line:
            h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


def make_discord_preview(video_path):
    """Version compressée calibrée pour tenir sous la limite Discord.

    Le débit est calculé à partir de la durée réelle : une vidéo longue est
    donc davantage compressée, au lieu d'un CRF fixe qui laissait parfois
    passer un fichier trop lourd (erreur 40005 « request entity too large »).
    """
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        duration = max(video_duration(video_path), 1.0)
        audio_kbps = 64
        total_kbps = DISCORD_TARGET_MB * 8192 / duration
        video_kbps = max(180, int(total_kbps - audio_kbps))
        out = video_path.replace(".mp4", "-apercu.mp4")
        # Résolution conservée : c'est elle qui rend l'aperçu lisible. Seul le
        # débit est réduit pour tenir dans la limite Discord.
        subprocess.run(
            [get_ffmpeg_exe(), "-y", "-v", "error", "-i", video_path,
             "-c:v", "libx264", "-preset", "veryfast",
             "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k",
             "-bufsize", f"{video_kbps * 2}k", "-threads", "2",
             "-c:a", "aac", "-b:a", f"{audio_kbps}k", out],
            check=True, capture_output=True)
        size_mb = os.path.getsize(out) / 1e6
        log.info("Aperçu Discord : %.1f Mo (%.0f kbps sur %.0fs)",
                 size_mb, video_kbps, duration)
        return out
    except Exception as exc:
        log.warning("Compression pour Discord échouée : %s", exc)
        return None


def keep_video(config, video_path):
    """Archive la vidéo dans un dossier qui n'est jamais nettoyé.

    `downloads/` est vidé à chaque démarrage : les vidéos qu'on veut garder
    (publication en pause, ou refus TikTok) sont déplacées à part.
    """
    keep_dir = config.get("video", {}).get("keep_dir", "videos")
    os.makedirs(keep_dir, exist_ok=True)
    target = os.path.join(keep_dir, os.path.basename(video_path))
    # Idempotent : appelable deux fois sans casser (une vidéo refusée par
    # TikTok est archivée tout de suite, puis revue par le nettoyage final).
    if os.path.abspath(video_path) != os.path.abspath(target):
        os.replace(video_path, target)
    return target


def send_video_to_discord(notifier, video_path, info):
    """Envoie la vidéo sur Discord, en version compressée si elle est trop lourde."""
    # Inutile de tenter l'originale si elle dépasse déjà la limite : l'envoi
    # échoue au bout de plusieurs minutes pour rien.
    if os.path.getsize(video_path) / 1e6 <= DISCORD_TARGET_MB:
        if notifier.send_file(video_path, info):
            return True
    else:
        log.info("Vidéo de %.0f Mo : envoi direct de la version compressée",
                 os.path.getsize(video_path) / 1e6)
    preview = make_discord_preview(video_path)
    sent = bool(preview) and notifier.send_file(
        preview, info + "\n\n⚠️ **Ceci est un aperçu compressé** (limite Discord). "
        "Pour publier, télécharge l'original en pleine qualité depuis le "
        "dossier `videos/` du serveur.")
    if preview and os.path.exists(preview):
        os.remove(preview)
    return sent


def run_cycle(config, tiktok, state, notifier):
    song_path, position, total = next_song(config, state)
    save_state(state)
    if song_path is None:
        notifier.send("⚠️ Aucune musique",
                      "Dépose au moins un fichier .mid / .mp3 dans le dossier "
                      "musics/. Nouvel essai au prochain créneau.", ORANGE)
        return
    notifier.send("🎬 Génération d'une vidéo...",
                  f"Morceau {position}/{total} de la playlist : "
                  f"**{os.path.splitext(os.path.basename(song_path))[0]}**", BLUE)
    started = time.time()
    result = generate_video(config, DOWNLOAD_DIR, song_path)
    if result is None:
        notifier.send("⚠️ Génération impossible",
                      "Aucune musique lisible dans le dossier musics/ : "
                      "dépose au moins un fichier mp3/wav/m4a/ogg/flac. "
                      "Nouvel essai au prochain créneau.", ORANGE)
        return
    video_path, song_name = result
    caption = next_caption(config, state, song_name)

    if not config.get("tiktok", {}).get("posting_enabled", False):
        # Mode test : pas de publication TikTok. La vidéo est archivée sur le
        # serveur (dossier jamais nettoyé) et envoyée sur Discord.
        kept = keep_video(config, video_path)
        size_mb = os.path.getsize(kept) / 1e6
        log.info("Publication TikTok en pause : vidéo conservée dans %s", kept)
        # La légende est isolée dans un bloc de code : un appui long la copie
        # entièrement sur mobile, prête à coller dans TikTok.
        info = (f"🎬 **{song_name}** — {(time.time() - started) / 60:.1f} min "
                f"de rendu, {size_mb:.1f} Mo\n"
                f"Sur le serveur : `{kept}`\n"
                f"Légende à copier :\n```\n{caption}\n```")
        if not send_video_to_discord(notifier, kept, info):
            notifier.send("🎬 Vidéo générée (trop lourde pour Discord)", info, ORANGE)
        return

    notifier.send("🎞️ Vidéo générée",
                  f"Musique : **{song_name}**\n"
                  f"Rendu en {(time.time() - started) / 60:.1f} min. Upload en cours...",
                  BLUE)
    try:
        log.info("Upload sur TikTok (musique : %s)", song_name)
        publish_id = tiktok.upload_video(video_path, caption)
        state.setdefault("posted", {})[os.path.basename(video_path)] = {
            "song": song_name,
            "publish_id": publish_id,
            "posted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tk = config.get("tiktok", {})
        if tk.get("post_mode") == "inbox":
            log.info("Vidéo envoyée dans la boîte de réception TikTok "
                     "(publish_id %s) : à publier depuis l'appli", publish_id)
            note = ("\n📥 La vidéo t'attend dans les **notifications TikTok** "
                    "(onglet Inbox, pas les brouillons) : tape la notification, "
                    "colle la légende ci-dessous et publie en public.")
        elif tk.get("privacy_level", "SELF_ONLY") == "SELF_ONLY":
            log.info("Vidéo publiée (publish_id %s)", publish_id)
            note = ("\n⚠️ Visibilité `SELF_ONLY` : visible seulement par toi. "
                    "Passe-la en public depuis l'appli (··· → Confidentialité).")
        else:
            log.info("Vidéo publiée (publish_id %s)", publish_id)
            note = ""
        # La vidéo part aussi sur Discord : archive + légende prête à coller.
        send_video_to_discord(
            notifier, video_path,
            f"🚀 **Envoyée sur TikTok** — {song_name}{note}\n"
            f"Légende à copier :\n```\n{caption}\n```")
        notifier.send("🚀 Envoyée sur TikTok !",
                      f"Musique : **{song_name}**\n"
                      f"publish_id : `{publish_id}`{note}", GREEN)
    except TikTokError as exc:
        log.error("Erreur TikTok : %s", exc)
        notifier.send("❌ Échec de la publication TikTok", str(exc), RED)
        # Le rendu a coûté plusieurs minutes : on le garde sur le serveur et
        # on l'envoie sur Discord plutôt que de le perdre sur un refus.
        video_path = keep_video(config, video_path)
        send_video_to_discord(
            notifier, video_path,
            f"💾 Non publiée, gardée dans `{video_path}` — {song_name}\n"
            f"Légende à copier :\n```\n{caption}\n```")
    finally:
        save_state(state)
        # Toujours archiver : même publiée, la vidéo reste récupérable depuis
        # le serveur (pour la reposter, la vérifier, ou parce que le dépôt
        # TikTok n'a pas abouti côté appli).
        if os.path.exists(video_path):
            kept = keep_video(config, video_path)
            log.info("Vidéo conservée : %s", kept)


def main():
    config = load_config()
    tk_cfg = config["tiktok"]
    for field in ("client_key", "client_secret"):
        if not tk_cfg.get(field) or str(tk_cfg[field]).startswith("COLLE_ICI"):
            log.error("config.json : le champ tiktok.%s n'est pas rempli.", field)
            sys.exit(1)

    notifier = Notifier(config.get("discord_webhook_url", ""))

    # `or ""` : la clé peut valoir null dans le JSON (jeton pas encore obtenu).
    refresh_token = (tk_cfg.get("refresh_token") or "").strip()
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
    # Nouvel ordre de passage tiré au sort à chaque démarrage.
    playlist = shuffle_playlist(config, state)
    save_state(state)
    log.info("Playlist mélangée : %d morceau(x)", len(playlist))
    # Quel compte est réellement au bout du jeton ? Une vidéo déposée dans la
    # boîte d'un autre compte que celui qu'on regarde est invisible.
    lines = []
    try:
        user = tiktok.user_info()
        name = user.get("username") or user.get("display_name") or "?"
        lines.append(f"Compte connecté : **{name}**")
        log.info("Compte TikTok connecté : %s", user)
    except Exception as exc:
        lines.append(f"Compte connecté : illisible ({exc})")
        log.warning("user_info indisponible : %s", exc)

    if tk_cfg.get("post_mode") == "inbox":
        lines.append("Mode : dépôt dans les **notifications TikTok** de ce compte")
    else:
        try:
            info = tiktok.creator_info()
            options = info.get("privacy_level_options", [])
            wanted = tk_cfg.get("privacy_level", "SELF_ONLY")
            lines.append(f"Visibilités autorisées : {', '.join(options) or 'aucune'}")
            lines.append(f"Configurée : `{wanted}` → "
                         + ("✅ publication possible" if wanted in options
                            else f"❌ `{wanted}` non autorisée"))
        except Exception as exc:
            lines.append(f"creator_info indisponible : {exc}")
            log.warning("creator_info indisponible : %s", exc)
    notifier.send("🔎 Connexion TikTok", "\n".join(lines), GREEN)

    now = datetime.now(tz)
    upcoming_base, upcoming_run_at = next_slot(schedule, now, state.get("last_slot"))
    post_on_start = schedule.get("post_on_start", True)

    if post_on_start:
        # La vidéo de démarrage prend la place du créneau à venir : le rythme
        # reste de deux vidéos par jour, même en cas de redémarrage.
        # Marqué avant la génération pour qu'un plantage ne double pas le post.
        state["last_slot"] = upcoming_base.isoformat()
        save_state(state)
        _, after_run_at = next_slot(schedule, now, state["last_slot"])
        next_txt = after_run_at.strftime("%d/%m vers %H:%M")
        first_line = (f"Vidéo générée maintenant, à la place du créneau de "
                      f"{upcoming_run_at.strftime('%Hh%M')}")
    else:
        next_txt = upcoming_run_at.strftime("%d/%m vers %H:%M")
        first_line = "Aucune vidéo au démarrage"

    notifier.send("🤖 Bot en ligne",
                  f"Créneaux : {slots_txt} ({tz.key})\n"
                  f"Playlist mélangée : {len(playlist)} morceau(x)\n"
                  f"{first_line}\n"
                  f"Prochaine vidéo planifiée : {next_txt}",
                  GREEN)

    if post_on_start:
        log.info("Vidéo de démarrage (elle consomme le créneau de %s) ; "
                 "prochaine ensuite : %s",
                 upcoming_run_at.strftime("%Hh%M"), next_txt)
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
