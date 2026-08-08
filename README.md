# bot-scrap — Générateur de vidéos "Ball Escape" → TikTok

Bot Python qui **génère** des vidéos ball escape (balle qui rebondit dans des anneaux
néon rotatifs et s'échappe couche par couche) et les publie sur TikTok via l'**API
officielle Content Posting**.

- **La balle joue la mélodie** : à chaque rebond/évasion, la note suivante de la musique
  est jouée. La musique est piochée au hasard dans le dossier `musics/` du serveur.
- **2 publications/jour** par défaut : une vers **10h** et une vers **17h** (heure de
  Paris), chacune décalée aléatoirement dans une fenêtre de 60 min. Planifié par le bot
  lui-même : aucun cron nécessaire.
- Vidéos verticales **720×1280, 30 fps, ~68 s** par défaut (réglable — passe à
  1080×1920/60 fps/`"encoder_preset": "veryfast"` si le serveur a ≥ 1 Go de RAM),
  couleur néon différente à chaque nouveau jeu d'anneaux.
- **Descriptions en rotation** : le bot alterne entre les textes de la liste `captions`
  de `config.json` (`{song}` est remplacé par le nom du fichier musique).

## 1. Créer l'app TikTok (une fois)

1. Sur <https://developers.tiktok.com>, crée une app (ou un **Sandbox** pour tester sans
   review) et ajoute les produits **Login Kit** et **Content Posting API** (*Direct Post*).
2. Coche le scope **`video.publish`** et enregistre une **Redirect URI** (n'importe
   quelle URL https que tu peux ouvrir, ex. l'URL de ce repo).
3. En sandbox : ajoute ton compte TikTok comme **utilisateur cible**.
4. Note le **Client key** et le **Client secret**.

> ⚠️ Tant que l'app n'est pas **auditée** par TikTok, les vidéos publiées par l'API sont
> forcées en **visibilité privée** (`SELF_ONLY`). Une fois l'app approuvée, mets
> `"privacy_level": "PUBLIC_TO_EVERYONE"` dans `config.json`.
> Pour la review : `TERMS.md` et `PRIVACY.md` de ce repo servent de Terms of Service /
> Privacy Policy URL, et une capture d'écran vidéo du flux complet sert de démo.

## 2. Configurer

Copie `config.example.json` → `config.json` :

| Champ | Rôle |
|---|---|
| `tiktok.client_key` / `client_secret` | Identifiants de l'app (ou du sandbox) |
| `tiktok.redirect_uri` | La même Redirect URI que dans l'app TikTok |
| `tiktok.refresh_token` | Laisse vide : rempli automatiquement à la première connexion |
| `tiktok.privacy_level` | `SELF_ONLY` tant que l'app n'est pas auditée |
| `schedule.post_times` / `random_window_minutes` / `timezone` | Créneaux de publication |
| `music.dir` | Dossier des musiques (`musics/` par défaut) |
| `music.note_duration_ms` | Durée de chaque note jouée par la balle (320 ms) |
| `video.width/height/fps/duration_seconds/rings` | Format de la vidéo générée |
| `captions` | Liste des descriptions TikTok, utilisées en rotation |

`config.json` contient tes secrets : il est dans `.gitignore`, **ne le commit pas**.

## 3. Déployer sur Pterodactyl (100 % faisable depuis un téléphone)

1. Egg **Python générique** (Python ≥ 3.9, prends 3.11+ si possible).
2. Dépose les fichiers du repo via le **gestionnaire de fichiers** du panel
   (Code → Download ZIP sur GitHub, upload du zip, "Unarchive").
3. Crée `config.json` (voir ci-dessus).
4. Crée un dossier **`musics/`** et dépose-y tes musiques (`.mp3`, `.wav`, `.m4a`,
   `.ogg`, `.flac`) — au moins une, tu peux en rajouter à tout moment.
5. Commande de démarrage, selon ce que ton panel accepte :

```bash
python start.py
```

ou `bash start.sh`, ou si le panel gère un shell complet :
`bash -c "pip install --no-cache-dir -U -r requirements.txt && python bot.py"`.

6. Démarre le serveur. **RAM nécessaire** : ~250 Mo minimum avec les réglages par
   défaut (720p30/ultrafast), ≥ 1 Go pour du 1080p60.

## 4. Première connexion TikTok (une fois, depuis les logs)

Au premier démarrage, le bot affiche dans la console une URL d'autorisation :

1. Ouvre-la dans ton navigateur, autorise ton compte TikTok.
2. Copie l'**URL complète** de redirection (elle contient `?code=...`).
3. Crée le fichier `auth_redirect.txt` via le gestionnaire de fichiers et colle l'URL
   dedans. Fais les 3 étapes d'affilée : le code expire en quelques minutes.

Le bot confirme `✔ Connexion TikTok réussie` et devient autonome.

## Notes

- La génération d'une vidéo prend de **2 à 30 min** selon le CPU du serveur (le rendu
  logge sa progression). Si c'est trop lent, baisse dans `config.json` :
  `"fps": 30` et/ou `"width": 720, "height": 1280`.
- Aucun ffmpeg à installer : le binaire est fourni par le paquet pip `imageio-ffmpeg`.
- `state.json` mémorise les publications et la rotation des descriptions ;
  `tokens.json` garde les tokens TikTok rafraîchis automatiquement.
- L'ancien mode repost Instagram (`instagram_source.py`, dépendance `instaloader`) est
  désactivé mais conservé dans le repo.
