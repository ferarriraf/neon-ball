"""Notifications Discord via webhook (facultatif).

Si config.json contient "discord_webhook_url", le bot y envoie des embeds
aux moments clés. Un échec d'envoi ne doit jamais faire planter le bot.
"""

import logging

import requests

log = logging.getLogger("notify")

GREEN = 0x2ECC71
BLUE = 0x3498DB
RED = 0xE74C3C
ORANGE = 0xE67E22


class Notifier:
    def __init__(self, webhook_url=""):
        self.url = (webhook_url or "").strip()

    def send(self, title, description="", color=BLUE):
        if not self.url:
            return
        try:
            requests.post(
                self.url,
                json={"embeds": [{
                    "title": title[:256],
                    "description": description[:4000],
                    "color": color,
                }]},
                timeout=15,
            )
        except Exception as exc:
            log.warning("Notification Discord échouée : %s", exc)

    def send_file(self, path, content):
        """Envoie un fichier (ex. vidéo) sur le webhook. Retourne True si ok."""
        if not self.url:
            return False
        try:
            import os
            with open(path, "rb") as f:
                resp = requests.post(
                    self.url,
                    data={"content": content[:2000]},
                    files={"file": (os.path.basename(path), f, "video/mp4")},
                    timeout=300,
                )
            if resp.status_code >= 300:
                log.warning("Fichier refusé par Discord (HTTP %d) : %s",
                            resp.status_code, resp.text[:200])
                return False
            return True
        except Exception as exc:
            log.warning("Envoi du fichier sur Discord échoué : %s", exc)
            return False
