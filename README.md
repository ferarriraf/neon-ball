# bot-scrap — Repost Instagram → TikTok

Bot Python qui récupère les vidéos d'un compte Instagram **public** (sans login, via
[instaloader](https://instaloader.github.io/)) et les publie sur TikTok via l'**API
officielle Content Posting**, avec la même description/hashtags.

- Vérification **2×/jour** par défaut (`check_interval_hours: 12`), planifiée par le bot
  lui-même : aucun cron nécessaire, il suffit que le process tourne.
- **Rattrapage de l'historique** : les vidéos sont publiées de la plus ancienne à la plus
  récente, `max_posts_per_run` par cycle (2 par défaut, soit 4/jour), puis le bot suit les
  nouvelles publications au fil de l'eau.
- L'état est gardé dans `state.json` : aucune vidéo n'est publiée deux fois, même après
  redémarrage.

## 1. Créer l'app TikTok (une fois)

1. Sur <https://developers.tiktok.com>, crée une app et ajoute les produits **Login Kit**
   et **Content Posting API** (mode *Direct Post*).
2. Demande le scope **`video.publish`** et enregistre une **Redirect URI**
   (ex. `https://localhost:8080/callback/`).
3. Note le **Client key** et le **Client secret**.

> ⚠️ Tant que ton app n'est pas **auditée** par TikTok, les vidéos publiées par l'API sont
> forcées en **visibilité privée** (`SELF_ONLY`). C'est une limite TikTok, pas du bot.
> Une fois l'app auditée, mets `"privacy_level": "PUBLIC_TO_EVERYONE"` dans `config.json`.

## 2. Obtenir le refresh token (une fois, sur ton PC)

```bash
pip install -r requirements.txt
python tiktok_auth.py
```

Le script t'affiche l'URL d'autorisation, tu valides avec ton compte TikTok, tu colles
l'URL de redirection, et il écrit le `refresh_token` dans `config.json`.

## 3. Configurer

Copie `config.example.json` → `config.json` et remplis :

| Champ | Rôle |
|---|---|
| `instagram.username` | Le compte Instagram (public) à surveiller |
| `tiktok.client_key` / `client_secret` / `refresh_token` | Identifiants de l'app TikTok |
| `tiktok.privacy_level` | `SELF_ONLY` tant que l'app n'est pas auditée |
| `schedule.check_interval_hours` | Fréquence de vérification (12 = 2×/jour) |
| `schedule.max_posts_per_run` | Nombre max de vidéos publiées par cycle |
| `caption.append_hashtags` | Texte/hashtags ajoutés à la fin de chaque description |

`config.json` contient tes secrets : il est dans `.gitignore`, **ne le commit pas**.

## 4. Déployer sur Pterodactyl

1. Utilise un egg **Python générique** (Python ≥ 3.9).
2. Dépose les fichiers du repo sur le serveur (ou clone le repo), puis envoie ton
   `config.json` via le **gestionnaire de fichiers** du panel.
3. Commande de démarrage :

```bash
bash -c "pip install --no-cache-dir -U -r requirements.txt && python bot.py"
```

C'est tout : le bot boucle en interne (vérification puis `sleep`), donc le serveur doit
juste rester allumé. Les fichiers `state.json` et `tokens.json` sont créés à côté du bot
et doivent persister entre les redémarrages (c'est le cas sur Pterodactyl).

## Notes

- **Instagram** : le scraping sans login fonctionne pour les profils publics ; à 2
  vérifications/jour le risque de blocage est faible. Si Instagram bloque temporairement
  (erreur 401/429 dans les logs), le bot réessaie simplement au cycle suivant.
- **TikTok** : les tokens sont rafraîchis automatiquement (`tokens.json`). Si le
  refresh token expire (365 jours) ou est révoqué, relance `python tiktok_auth.py` sur
  ton PC et remets le nouveau `config.json` sur le serveur.
- Limites TikTok : vidéos ≤ 4 Go, durée max selon ton compte (généralement 10 min).
