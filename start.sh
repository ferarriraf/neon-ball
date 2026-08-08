#!/bin/sh
# Script de démarrage pour Pterodactyl : certains panels ne gèrent ni && ni
# les guillemets dans la startup command — on met donc tout ici et la
# commande de démarrage devient simplement : bash start.sh
pip install --no-cache-dir -U -r requirements.txt
python bot.py
