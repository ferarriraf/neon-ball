"""Démarrage pour les panels dont la startup command doit être du Python.

Commande de démarrage : python start.py
Installe/actualise les dépendances puis lance le bot.
"""

import subprocess
import sys

subprocess.run(
    [sys.executable, "-m", "pip", "install", "--no-cache-dir", "-U",
     "-r", "requirements.txt"],
    check=False,
)
sys.exit(subprocess.run([sys.executable, "bot.py"]).returncode)
