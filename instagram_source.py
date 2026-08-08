"""Récupération des vidéos d'un profil Instagram public via instaloader (sans login)."""

import logging

import instaloader
import requests

log = logging.getLogger("instagram")


def fetch_video_posts(username):
    """Retourne les posts vidéo du profil, du plus ancien au plus récent.

    Chaque élément : {shortcode, caption, video_url, date}.
    """
    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_comments=False,
        save_metadata=False,
        quiet=True,
    )
    profile = instaloader.Profile.from_username(loader.context, username)
    videos = []
    for post in profile.get_posts():
        if not post.is_video:
            continue
        videos.append({
            "shortcode": post.shortcode,
            "caption": post.caption or "",
            "video_url": post.video_url,
            "date": post.date_utc.isoformat(),
        })
    videos.reverse()  # get_posts() renvoie du plus récent au plus ancien
    log.info("%d vidéos trouvées sur @%s", len(videos), username)
    return videos


def download_video(video_url, dest_path):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    with requests.get(video_url, headers=headers, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
    return dest_path
