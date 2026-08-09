"""Démarrage : auto-update depuis GitHub, installation des dépendances, lancement.

Commande de démarrage : python start.py

À chaque boot, le script télécharge la dernière version du code depuis la
branche main du repo GitHub et remplace UNIQUEMENT les fichiers de code
(*.py et requirements.txt). Il ne touche jamais à config.json, tokens.json,
state.json ni au dossier musics/. Si le repo est privé ou injoignable, la
mise à jour est simplement sautée et le bot démarre avec le code présent.
"""

import io
import os
import subprocess
import sys
import urllib.request
import zipfile

DEFAULT_REPO = "ferarriraf/bot-scrap"


def _config():
    try:
        import json
        with open("config.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def update_from_github():
    try:
        cfg = _config()
        # Si le dépôt est renommé sur GitHub, il suffit d'ajouter
        # "github_repo": "proprietaire/nouveau-nom" dans config.json.
        repo = cfg.get("github_repo") or DEFAULT_REPO
        token = (cfg.get("github_token") or "").strip()
        if token:
            request = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/zipball/main",
                headers={"Authorization": "Bearer " + token})
        else:
            request = urllib.request.Request(
                f"https://codeload.github.com/{repo}/zip/refs/heads/main")
        data = urllib.request.urlopen(request, timeout=30).read()
        archive = zipfile.ZipFile(io.BytesIO(data))
        updated = 0
        for name in archive.namelist():
            # Fichiers à la racine de l'archive uniquement ("<repo>-main/xxx").
            parts = name.split("/", 1)
            if len(parts) != 2 or "/" in parts[1] or not parts[1]:
                continue
            base = parts[1]
            if base.endswith(".py") or base == "requirements.txt":
                with open(base, "wb") as f:
                    f.write(archive.read(name))
                updated += 1
        print(f"[start] Code mis à jour depuis GitHub ({updated} fichiers)")
    except Exception as exc:
        print(f"[start] Mise à jour GitHub sautée ({exc}) : démarrage avec le code local")


update_from_github()
subprocess.run(
    [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-U",
     "-r", "requirements.txt"],
    check=False,
)
sys.exit(subprocess.run([sys.executable, "bot.py"]).returncode)
