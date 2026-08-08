"""À lancer UNE FOIS sur ton PC (pas sur le serveur) pour obtenir le refresh_token TikTok.

Prérequis sur https://developers.tiktok.com :
  - une app avec le produit "Login Kit" et "Content Posting API" activés,
  - le scope video.publish approuvé pour l'app,
  - une Redirect URI enregistrée (ex. https://localhost:8080/callback/).

Usage : python tiktok_auth.py
"""

import json
import os
import secrets
import urllib.parse

import requests

API_BASE = "https://open.tiktokapis.com/v2"
# video.publish = publication automatique (mode "direct")
# video.upload  = envoi dans la boîte de réception TikTok (mode "inbox")
DEFAULT_SCOPES = "user.info.basic,video.publish"


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix} : ").strip()
    return value or default


def main():
    config = {}
    if os.path.exists("config.json"):
        with open("config.json", encoding="utf-8") as f:
            config = json.load(f)
    tk = config.get("tiktok", {})

    client_key = ask("Client key de ton app TikTok", tk.get("client_key"))
    client_secret = ask("Client secret", tk.get("client_secret"))
    redirect_uri = ask("Redirect URI enregistrée dans l'app", "https://localhost:8080/callback/")
    scopes = ask("Scopes à demander (doivent être activés sur l'app)", DEFAULT_SCOPES)

    state = secrets.token_urlsafe(16)
    auth_url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": client_key,
        "response_type": "code",
        "scope": scopes,
        "redirect_uri": redirect_uri,
        "state": state,
    })
    print("\n1. Ouvre cette URL dans ton navigateur et autorise ton compte TikTok :\n")
    print(auth_url)
    print("\n2. Après validation, le navigateur redirige vers ta Redirect URI")
    print("   (la page peut afficher une erreur, c'est normal : copie l'URL complète).")
    redirected = input("\nColle ici l'URL complète de redirection : ").strip()

    query = urllib.parse.parse_qs(urllib.parse.urlparse(redirected).query)
    if query.get("state", [""])[0] != state:
        print("ATTENTION : le paramètre state ne correspond pas, recommence.")
        return
    code = query.get("code", [""])[0]
    if not code:
        print("Pas de paramètre code dans l'URL, recommence.")
        return

    resp = requests.post(
        f"{API_BASE}/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    data = resp.json()
    if "refresh_token" not in data:
        print(f"Échec de l'échange du code : {data}")
        return

    print("\n✔ Tokens obtenus.")
    print(f"  refresh_token : {data['refresh_token']}")
    print(f"  scopes        : {data.get('scope')}")

    if ask("\nÉcrire ces valeurs dans config.json ? (o/n)", "o").lower().startswith("o"):
        config.setdefault("tiktok", {})
        config["tiktok"].update({
            "client_key": client_key,
            "client_secret": client_secret,
            "refresh_token": data["refresh_token"],
        })
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        print("config.json mis à jour : envoie-le sur le serveur Pterodactyl.")


if __name__ == "__main__":
    main()
