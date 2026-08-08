"""Client pour la Content Posting API officielle de TikTok.

Gère le rafraîchissement du token OAuth (les refresh tokens TikTok tournent :
le nouveau est persisté dans tokens.json) et l'upload de vidéos par chunks.
"""

import json
import logging
import os
import time

import requests

log = logging.getLogger("tiktok")

API_BASE = "https://open.tiktokapis.com/v2"

# Limites de chunk imposées par TikTok : 5 Mo min, 64 Mo max par chunk,
# un fichier <= 64 Mo peut être envoyé en un seul chunk.
SINGLE_CHUNK_MAX = 64 * 1024 * 1024
CHUNK_SIZE = 32 * 1024 * 1024

STATUS_POLL_INTERVAL = 10
STATUS_POLL_TIMEOUT = 600


class TikTokError(Exception):
    pass


class TikTokClient:
    def __init__(self, client_key, client_secret, refresh_token,
                 token_store_path="tokens.json", privacy_level="SELF_ONLY"):
        self.client_key = client_key
        self.client_secret = client_secret
        self.initial_refresh_token = refresh_token
        self.token_store_path = token_store_path
        self.privacy_level = privacy_level
        self._tokens = self._load_tokens()

    # ------------------------------------------------------------------ OAuth

    def _load_tokens(self):
        if os.path.exists(self.token_store_path):
            try:
                with open(self.token_store_path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                log.warning("tokens.json illisible, repart du refresh_token de config.json")
        return {"refresh_token": self.initial_refresh_token,
                "access_token": None, "expires_at": 0}

    def _save_tokens(self):
        with open(self.token_store_path, "w", encoding="utf-8") as f:
            json.dump(self._tokens, f, indent=2)

    def _refresh_access_token(self):
        resp = requests.post(
            f"{API_BASE}/oauth/token/",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key": self.client_key,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self._tokens["refresh_token"],
            },
            timeout=30,
        )
        data = resp.json()
        if "access_token" not in data:
            raise TikTokError(f"Échec du refresh du token TikTok : {data}")
        self._tokens["access_token"] = data["access_token"]
        self._tokens["expires_at"] = time.time() + data.get("expires_in", 86400)
        # TikTok peut renvoyer un nouveau refresh_token : il faut le garder.
        if data.get("refresh_token"):
            self._tokens["refresh_token"] = data["refresh_token"]
        self._save_tokens()
        log.info("Token TikTok rafraîchi")

    def _access_token(self):
        if not self._tokens.get("access_token") or time.time() > self._tokens.get("expires_at", 0) - 300:
            self._refresh_access_token()
        return self._tokens["access_token"]

    def _auth_headers(self):
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json; charset=UTF-8",
        }

    # ----------------------------------------------------------------- Upload

    @staticmethod
    def _chunk_plan(video_size):
        if video_size <= SINGLE_CHUNK_MAX:
            return video_size, 1
        total_chunk_count = video_size // CHUNK_SIZE
        return CHUNK_SIZE, total_chunk_count

    def upload_video(self, video_path, title):
        video_size = os.path.getsize(video_path)
        chunk_size, total_chunk_count = self._chunk_plan(video_size)

        resp = requests.post(
            f"{API_BASE}/post/publish/video/init/",
            headers=self._auth_headers(),
            json={
                "post_info": {
                    "title": title,
                    "privacy_level": self.privacy_level,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": video_size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": total_chunk_count,
                },
            },
            timeout=30,
        )
        data = resp.json()
        if data.get("error", {}).get("code") not in (None, "ok"):
            raise TikTokError(f"Init upload refusé : {data['error']}")
        publish_id = data["data"]["publish_id"]
        upload_url = data["data"]["upload_url"]

        self._upload_chunks(upload_url, video_path, video_size, chunk_size, total_chunk_count)
        self._wait_for_publish(publish_id)
        return publish_id

    def _upload_chunks(self, upload_url, video_path, video_size, chunk_size, total_chunk_count):
        with open(video_path, "rb") as f:
            for i in range(total_chunk_count):
                start = i * chunk_size
                # Le dernier chunk absorbe le reste du fichier.
                end = video_size - 1 if i == total_chunk_count - 1 else start + chunk_size - 1
                f.seek(start)
                payload = f.read(end - start + 1)
                resp = requests.put(
                    upload_url,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(payload)),
                        "Content-Range": f"bytes {start}-{end}/{video_size}",
                    },
                    data=payload,
                    timeout=300,
                )
                if resp.status_code not in (200, 201, 206):
                    raise TikTokError(f"Upload chunk {i + 1}/{total_chunk_count} échoué "
                                      f"(HTTP {resp.status_code}) : {resp.text[:300]}")
                log.info("Chunk %d/%d envoyé", i + 1, total_chunk_count)

    def _wait_for_publish(self, publish_id):
        deadline = time.time() + STATUS_POLL_TIMEOUT
        while time.time() < deadline:
            resp = requests.post(
                f"{API_BASE}/post/publish/status/fetch/",
                headers=self._auth_headers(),
                json={"publish_id": publish_id},
                timeout=30,
            )
            data = resp.json().get("data", {})
            status = data.get("status")
            if status == "PUBLISH_COMPLETE":
                log.info("Publication TikTok terminée (%s)", publish_id)
                return
            if status == "FAILED":
                raise TikTokError(f"Publication TikTok échouée : {data.get('fail_reason')}")
            log.info("Statut TikTok : %s, nouvelle vérification dans %ds", status, STATUS_POLL_INTERVAL)
            time.sleep(STATUS_POLL_INTERVAL)
        raise TikTokError(f"Timeout en attendant la publication {publish_id}")
