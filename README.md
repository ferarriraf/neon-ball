# bot-scrap — Repost Instagram → TikTok

Bot Python qui récupère les vidéos d'un compte Instagram **public** (sans login, via
[instaloader](https://instaloader.github.io/)) et les publie sur TikTok via l'**API
officielle Content Posting**, avec la même description/hashtags.

- **2 publications/jour** par défaut : une vers **10h** et une vers **17h** (heure de
  Paris), chacune décalée aléatoirement dans une fenêtre de 60 min pour ne pas poster à
  heure fixe. Planifié par le bot lui-même : aucun cron nécessaire, il suffit que le
  process tourne.
- **Rattrapage de l'historique** : les vidéos sont publiées de la plus ancienne à la plus
  récente, 1 par créneau, puis le bot suit les nouvelles publications au fil de l'eau.
- L'état est gardé dans `state.json` : aucune vidéo n'est publiée deux fois, même après
  redémarrage.

## 1. Créer l'app TikTok (une fois)

1. Sur <https://developers.tiktok.com>, crée une app et ajoute les produits **Login Kit**
   et **Content Posting API** (mode *Direct Post*).
2. Demande le scope **`video.publish`** et enregistre une **Redirect URI**
   (ex. `https://localhost:8080/callback/`).
3. Note le **Client key** et le **Client secret**.

> ℹ️ Si la liste "Add scopes" ne propose que `user.info.*` et `video.list`, c'est que le
> produit **Content Posting API** n'a pas encore été ajouté à l'app : le scope
> `video.publish` n'apparaît qu'après l'ajout de ce produit (section *Products* → *Add
> products* de l'app). En attendant l'approbation de l'app, tu peux utiliser un
> **Sandbox** (Developer Portal) en y ajoutant ton propre compte TikTok comme
> utilisateur cible pour tester le bot.

> ⚠️ Tant que ton app n'est pas **auditée** par TikTok, les vidéos publiées par l'API sont
> forcées en **visibilité privée** (`SELF_ONLY`). C'est une limite TikTok, pas du bot.
> Une fois l'app auditée, mets `"privacy_level": "PUBLIC_TO_EVERYONE"` dans `config.json`.

## 2. Première connexion TikTok (100 % depuis le panel, téléphone OK)

Pas besoin de PC ni de terminal : au premier démarrage, si `refresh_token` est vide,
le bot affiche dans les **logs du panel Pterodactyl** une URL d'autorisation.

1. Ouvre cette URL dans ton navigateur et autorise ton compte TikTok.
2. Tu es redirigé vers ta Redirect URI : **copie l'URL complète** de la barre
   d'adresse (elle contient `?code=...`).
3. Dans le **gestionnaire de fichiers** du panel, crée un fichier `auth_redirect.txt`,
   colle l'URL dedans, sauvegarde.

Le bot détecte le fichier en quelques secondes, échange le code, sauvegarde les tokens
dans `tokens.json` et démarre. (Alternative sur PC : `python tiktok_auth.py`.)

> Le code d'autorisation expire en quelques minutes : fais les étapes 1 à 3 d'affilée.

## 3. Configurer

Copie `config.example.json` → `config.json` et remplis :

| Champ | Rôle |
|---|---|
| `instagram.username` | Le compte Instagram (public) à surveiller |
| `tiktok.client_key` / `client_secret` | Identifiants de l'app (ou du sandbox) TikTok |
| `tiktok.redirect_uri` | La même Redirect URI que celle enregistrée dans l'app TikTok |
| `tiktok.refresh_token` | Laisse vide : rempli automatiquement à la première connexion |
| `tiktok.privacy_level` | `SELF_ONLY` tant que l'app n'est pas auditée |
| `tiktok.post_mode` | `direct` (scope `video.publish`, publication auto) ou `inbox` (scope `video.upload`, la vidéo arrive dans ta boîte de réception TikTok et tu la valides dans l'appli) |
| `schedule.post_times` | Heures nominales des publications (`["10:00", "17:00"]`) |
| `schedule.random_window_minutes` | Décalage aléatoire ajouté après chaque heure nominale |
| `schedule.timezone` | Fuseau horaire des créneaux (`Europe/Paris`) |
| `schedule.max_posts_per_run` | Nombre max de vidéos publiées par créneau (1) |
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
