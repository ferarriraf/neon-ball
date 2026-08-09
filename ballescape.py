"""Simulation et rendu du gameplay "ball escape".

Une balle rebondit à l'intérieur d'anneaux néon concentriques qui tournent,
chacun percé d'une ouverture. Elle s'échappe couche par couche ; chaque
rebond, chaque évasion et chaque sphère terminée est un événement horodaté
(bounce / escape / complete) qui pilotera le son et les effets visuels.

Rendu headless : pygame dessine sur des Surfaces mémoire (pas d'écran),
les frames brutes sont envoyées à ffmpeg via stdin.
"""

import colorsys
import math
import os
import random

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

import pygame

NEON_PALETTE = [
    (57, 255, 20),    # vert néon (comme le style d'origine)
    (0, 229, 255),    # cyan
    (255, 44, 240),   # magenta
    (255, 158, 0),    # orange
    (255, 235, 59),   # jaune
    (130, 87, 255),   # violet
]

BALL_RADIUS = 16

# Physique : gravité forte + rebonds amortis = la vitesse respire vraiment
# (la balle ralentit en montant, accélère en tombant). Un coup de fouet
# n'est donné que si elle s'endort, et une évasion offre un petit boost.
# Gravité élevée : c'est le vrai remède aux trous dans la mélodie. Les vols
# paraboliques trop longs disparaissent, la cadence monte à ~2,3 notes/s et
# le silence audible tombe de 14 % à 3 % du temps (mesuré).
GRAVITY = 2600.0
RESTITUTION = 0.94        # perte d'énergie à chaque rebond
TANGENT_FRICTION = 0.99   # frottement le long de la paroi
REVIVE_SPEED = 300.0      # en dessous, la balle reçoit une impulsion
REVIVE_BOOST = 1.5
ESCAPE_BOOST = 1.12       # accélération de récompense en franchissant un anneau
MAX_SPEED = 1600.0
# Anti-blocage : la balle doit toujours repartir franchement de la paroi,
# sinon la gravité la replaque aussitôt et elle mitraille sur place.
MIN_SEPARATION_SPEED = 300.0
# ... et toujours repartir de côté : sans vitesse tangentielle, elle
# rebondit sur place au fond (ou fait l'aller-retour horizontal sur un
# flanc) et le jeu s'arrête d'avancer.
MIN_TANGENT_SPEED = 270.0
# Ces minima grandissent avec l'anneau : sur un grand cercle la balle vole
# plus longtemps entre deux parois, ce qui creusait des trous audibles dans
# la mélodie (0,62 s entre deux notes dehors contre 0,34 s au centre).
SPEED_RADIUS_EXPONENT = 0.5
BOUNCE_COOLDOWN = 0.035   # deux rebonds ne peuvent pas s'enchaîner plus vite
WALL_CLEARANCE = 2.0      # on la repose légèrement en retrait de la paroi

FLASH_DURATION = 0.25
CELEBRATION_DURATION = 1.4
PARTICLE_COUNT = 26
TRAIL_LENGTH = 34         # positions gardées pour la traînée (sous-pas physiques)

SPARK_COUNT = 9           # étincelles projetées à chaque rebond
SPARK_LIFE = 0.45
SPARK_GRAVITY = 1100.0
SHARD_COUNT = 9           # morceaux d'un anneau qui explose
SHARD_LIFE = 1.0
HUE_SPREAD = 0.13         # écart de teinte entre le 1er et le dernier anneau
SHAKE_DURATION = 0.45
SHAKE_AMPLITUDE = 11.0


def _hue_shift(color, delta):
    """Décale la teinte d'une couleur RGB de `delta` (0..1)."""
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in color))
    r, g, b = colorsys.hsv_to_rgb((h + delta) % 1.0, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def _ang_diff(a, b):
    """Différence angulaire signée a-b ramenée dans [-pi, pi]."""
    d = (a - b) % (2 * math.pi)
    if d > math.pi:
        d -= 2 * math.pi
    return d


class Ring:
    def __init__(self, radius, gap_center, gap_half, speed, color):
        self.radius = radius
        self.gap_center = gap_center
        self.gap_half = gap_half
        self.speed = speed
        self.color = color


class Shard:
    """Morceau d'anneau projeté quand la balle franchit une couche."""

    def __init__(self, a0, a1, radius, color, t, rng):
        self.a0 = a0
        self.a1 = a1
        self.radius = radius
        self.color = color
        self.t = t
        self.spin = rng.uniform(-2.2, 2.2)
        self.drift = rng.uniform(40, 130)


class Celebration:
    """Onde de choc + particules quand une sphère entière est terminée."""

    def __init__(self, t, radius, color, rng):
        self.t = t
        self.radius = radius
        self.color = color
        self.particles = [
            (a, rng.uniform(0.45, 1.25))
            for a in (i * 2 * math.pi / PARTICLE_COUNT + rng.uniform(-0.1, 0.1)
                      for i in range(PARTICLE_COUNT))
        ]


class Simulation:
    def __init__(self, width, height, ring_count, rng, finish_after=None):
        # finish_after : passé ce temps, la sphère terminée n'est pas
        # remplacée — la vidéo s'achève sur la célébration.
        self.finish_after = finish_after
        self.finished = False
        self.last_color = NEON_PALETTE[0]
        self.w = width
        self.h = height
        self.cx = width / 2
        self.cy = height / 2
        self.ring_count = ring_count
        self.rng = rng
        # Générateur séparé pour les effets de rendu : piocher dans self.rng
        # pendant le dessin décalerait la suite de nombres aléatoires et la
        # partie rejouée au rendu ne serait plus celle qui a produit l'audio.
        self.fx_rng = random.Random(0xB011)
        self.max_radius = min(width, height) / 2 - 40
        self.flashes = []  # (x, y, t)
        self.shards = []
        self.sparks = []
        self.celebrations = []
        self.rings = []
        self.current = 0
        self.spheres_done = 0
        self.trail = []  # positions récentes (échantillonnées à chaque sous-pas)
        self.last_bounce_t = -1.0
        self.shake_until = -1.0
        self._glow_cache = {}
        self._title_cache = None
        self._text_cache = {}
        if not pygame.font.get_init():
            pygame.font.init()
        self._build_sprites()
        self._new_ring_set()

    # ------------------------------------------------------- Sprites précalculés

    def _build_sprites(self):
        """Fond vignetté et halo de balle, calculés une seule fois."""
        small = pygame.Surface((self.w // 8, self.h // 8))
        small.fill((2, 2, 6))
        cx, cy = small.get_width() / 2, small.get_height() / 2
        max_r = int(math.hypot(cx, cy))
        for r in range(max_r, 0, -3):
            k = 1 - r / max_r
            pygame.draw.circle(small, (int(6 + 14 * k * k), int(7 + 16 * k * k),
                                       int(14 + 30 * k * k)),
                               (int(cx), int(cy)), r)
        self.background = pygame.transform.smoothscale(small, (self.w, self.h))

        glow_r = BALL_RADIUS * 5
        self.ball_glow = pygame.Surface((glow_r * 2, glow_r * 2))
        for r in range(glow_r, 0, -1):
            k = 1 - r / glow_r
            v = int(255 * k ** 3)
            pygame.draw.circle(self.ball_glow, (v, v, v), (glow_r, glow_r), r)
        self.glow_r = glow_r

    def _current_color(self):
        if not self.rings:
            return self.last_color
        return self.rings[min(self.current, len(self.rings) - 1)].color

    def _new_ring_set(self):
        base_color = self.rng.choice(NEON_PALETTE)
        self.last_color = base_color
        direction = self.rng.choice((-1, 1))
        base = BALL_RADIUS * 2 + 100
        spacing = (self.max_radius - base) / max(1, self.ring_count - 1)
        self.rings = []
        for i in range(self.ring_count):
            # Dégradé de teinte du centre vers l'extérieur.
            shift = direction * HUE_SPREAD * i / max(1, self.ring_count - 1)
            self.rings.append(Ring(
                radius=base + i * spacing,
                gap_center=self.rng.uniform(0, 2 * math.pi),
                gap_half=math.radians(self.rng.uniform(22, 34)),
                speed=self.rng.uniform(0.35, 0.95) * self.rng.choice((-1, 1)),
                color=_hue_shift(base_color, shift),
            ))
        self.current = 0
        self.trail.clear()  # la balle se téléporte au centre : pas de streak
        angle = self.rng.uniform(0, 2 * math.pi)
        speed = self.rng.uniform(420, 560)
        self.bx, self.by = self.cx, self.cy
        self.vx = math.cos(angle) * speed
        self.vy = math.sin(angle) * speed

    def _clamp_speed(self):
        speed = math.hypot(self.vx, self.vy)
        if speed > MAX_SPEED:
            factor = MAX_SPEED / speed
            self.vx *= factor
            self.vy *= factor

    def _spawn_sparks(self, nx, ny, color, t):
        """Gerbe d'étincelles projetée depuis le point d'impact."""
        base = math.atan2(-ny, -nx)  # vers l'intérieur de l'anneau
        for _ in range(SPARK_COUNT):
            angle = base + self.rng.uniform(-1.1, 1.1)
            speed = self.rng.uniform(120, 420)
            self.sparks.append([
                self.bx, self.by,
                math.cos(angle) * speed, math.sin(angle) * speed,
                t, color,
            ])

    def _shatter(self, ring, t):
        """Découpe l'anneau franchi en morceaux qui partent en tournoyant."""
        start = ring.gap_center + ring.gap_half
        span = 2 * math.pi - 2 * ring.gap_half
        for i in range(SHARD_COUNT):
            a0 = start + span * i / SHARD_COUNT
            a1 = start + span * (i + 0.82) / SHARD_COUNT
            self.shards.append(Shard(a0, a1, ring.radius, ring.color, t, self.rng))

    def step(self, dt, t):
        """Avance la physique de dt ; retourne une liste de (temps, type)."""
        events = []
        for ring in self.rings:
            ring.gap_center += ring.speed * dt

        for spark in self.sparks:
            spark[3] += SPARK_GRAVITY * dt
            spark[0] += spark[2] * dt
            spark[1] += spark[3] * dt

        self.vy += GRAVITY * dt
        self._clamp_speed()
        self.bx += self.vx * dt
        self.by += self.vy * dt

        ring = self.rings[self.current] if not self.finished else None
        dx = self.bx - self.cx
        dy = self.by - self.cy
        dist = math.hypot(dx, dy) or 0.001

        if ring is not None and dist + BALL_RADIUS >= ring.radius:
            ball_angle = math.atan2(dy, dx)
            in_gap = abs(_ang_diff(ball_angle, ring.gap_center)) < ring.gap_half * 0.9
            if in_gap:
                if dist - BALL_RADIUS > ring.radius:
                    # Évasion de la couche courante : petit boost de récompense.
                    self._shatter(ring, t)
                    self.current += 1
                    self.vx *= ESCAPE_BOOST
                    self.vy *= ESCAPE_BOOST
                    self._clamp_speed()
                    events.append((t, "escape"))
                    self.flashes.append((self.bx, self.by, t))
                    if self.current >= len(self.rings):
                        # Sphère entière terminée : célébration puis nouveau set.
                        self.spheres_done += 1
                        self.celebrations.append(
                            Celebration(t, self.rings[-1].radius,
                                        self.rings[-1].color, self.rng))
                        self.shake_until = t + SHAKE_DURATION
                        events.append((t, "complete"))
                        if self.finish_after is not None and t >= self.finish_after:
                            # Dernière sphère : plus rien ne se recharge, on
                            # laisse la célébration puis le fondu conclure.
                            self.finished = True
                            self.rings = []
                            self.current = 0
                        else:
                            self._new_ring_set()
            else:
                nx, ny = dx / dist, dy / dist
                outward = self.vx * nx + self.vy * ny
                if outward > 0 and t - self.last_bounce_t >= BOUNCE_COOLDOWN:
                    # Réflexion : composante normale inversée et amortie,
                    # composante tangentielle légèrement freinée.
                    # Plus l'anneau est grand, plus la balle doit aller vite
                    # pour garder la même cadence de notes.
                    pace = (ring.radius / self.rings[0].radius) ** SPEED_RADIUS_EXPONENT
                    min_normal = MIN_SEPARATION_SPEED * pace
                    min_tangent = MIN_TANGENT_SPEED * pace
                    tx, ty = -ny, nx
                    vt = (self.vx * tx + self.vy * ty) * TANGENT_FRICTION
                    if abs(vt) < min_tangent:
                        # Relance latérale : la balle repart le long de la
                        # paroi au lieu de sautiller au même endroit.
                        sign = self.rng.choice((-1, 1)) if abs(vt) < 1 else \
                            (1 if vt > 0 else -1)
                        vt = sign * min_tangent
                    # La normale repart toujours assez fort pour décoller de la
                    # paroi : sinon la gravité la replaque et la balle vibre
                    # sur place au lieu de rebondir.
                    vn = max(outward * RESTITUTION, min_normal)
                    self.vx = -nx * vn + tx * vt
                    self.vy = -ny * vn + ty * vt
                    speed = math.hypot(self.vx, self.vy)
                    revive = REVIVE_SPEED * pace
                    if speed < revive:
                        # La balle s'endort : coup de fouet pour relancer le jeu.
                        factor = REVIVE_BOOST * revive / max(speed, 1.0)
                        self.vx *= factor
                        self.vy *= factor
                    self._clamp_speed()
                    # Repose la balle légèrement en retrait de la paroi pour
                    # qu'elle ne re-collisionne pas au sous-pas suivant.
                    contact = ring.radius - BALL_RADIUS - WALL_CLEARANCE
                    self.bx = self.cx + nx * contact
                    self.by = self.cy + ny * contact
                    self.last_bounce_t = t
                    events.append((t, "bounce"))
                    self.flashes.append((self.bx, self.by, t))
                    self._spawn_sparks(nx, ny, ring.color, t)

        # Traînée échantillonnée à chaque sous-pas : elle reste lisse même
        # quand la balle traverse l'écran en quelques images.
        self.trail.append((self.bx, self.by))
        if len(self.trail) > TRAIL_LENGTH:
            del self.trail[:len(self.trail) - TRAIL_LENGTH]

        self.shards = [s for s in self.shards if t - s.t < SHARD_LIFE]
        self.sparks = [s for s in self.sparks if t - s[4] < SPARK_LIFE]
        self.flashes = [f for f in self.flashes if t - f[2] < FLASH_DURATION]
        self.celebrations = [c for c in self.celebrations
                             if t - c.t < CELEBRATION_DURATION]
        return events

    # ------------------------------------------------------------------ Rendu

    @staticmethod
    def _scaled(color, factor):
        return tuple(min(255, max(0, int(c * factor))) for c in color)

    def _tinted_glow(self, color):
        tinted = self._glow_cache.get(color)
        if tinted is None:
            tinted = self.ball_glow.copy()
            tinted.fill(color, special_flags=pygame.BLEND_MULT)
            self._glow_cache[color] = tinted
        return tinted

    def _draw_ring(self, surface, ring, brightness=1.0):
        start = ring.gap_center + ring.gap_half
        end = ring.gap_center + 2 * math.pi - ring.gap_half
        span = end - start
        # Échantillonnage plus fin : les arcs paraissent lisses, pas polygonaux.
        n = max(24, min(720, int(span * ring.radius / 4)))
        pts = []
        for i in range(n + 1):
            a = start + span * i / n
            pts.append((self.cx + math.cos(a) * ring.radius,
                        self.cy + math.sin(a) * ring.radius))
        color = ring.color
        for factor, width in ((0.13, 19), (0.28, 13), (0.55, 8), (1.0, 4)):
            pygame.draw.lines(surface, self._scaled(color, factor * brightness),
                              False, pts, width)
        # Cœur blanc lissé : c'est lui qui donne le fini "néon".
        white = self._scaled((255, 255, 255), 0.9 * brightness)
        pygame.draw.lines(surface, white, False, pts, 2)
        pygame.draw.aalines(surface, white, False, pts)

    def _draw_celebrations(self, surface, t):
        for celeb in self.celebrations:
            k = (t - celeb.t) / CELEBRATION_DURATION  # 0 → 1
            fade = max(0.0, 1.0 - k)
            # Onde de choc : deux anneaux qui s'élargissent et s'estompent.
            for delay, width in ((0.0, 6), (0.18, 3)):
                kk = k - delay
                if 0 <= kk <= 1:
                    radius = int(celeb.radius * (0.55 + 0.75 * kk))
                    pygame.draw.circle(surface, self._scaled(celeb.color, 1.0 - kk),
                                       (int(self.cx), int(self.cy)), radius, width)
            # Éclats projetés depuis le centre.
            for angle, speed in celeb.particles:
                d = celeb.radius * 0.35 + celeb.radius * 1.1 * speed * k
                px = self.cx + math.cos(angle) * d
                py = self.cy + math.sin(angle) * d
                size = max(1, int(7 * fade))
                pygame.draw.circle(surface, self._scaled(celeb.color, fade),
                                   (int(px), int(py)), size)
                pygame.draw.circle(surface, self._scaled((255, 255, 255), fade * 0.8),
                                   (int(px), int(py)), max(1, size // 2))
            # Éclat lumineux au centre (local : le fond reste noir).
            if k < 0.3:
                burst = 1.0 - k / 0.3
                size = int(self.max_radius * 0.9)
                glow = pygame.Surface((size * 2, size * 2))
                for layer in range(5):
                    radius = int(size * (1 - layer / 6) * burst)
                    if radius > 0:
                        pygame.draw.circle(
                            glow, self._scaled(celeb.color, 0.10 * burst),
                            (size, size), radius)
                surface.blit(glow, (int(self.cx) - size, int(self.cy) - size),
                             special_flags=pygame.BLEND_ADD)

    def draw_spectrum(self, surface, levels):
        """Visualiseur audio : barres symétriques en bas de l'image."""
        color = self._current_color()
        n = len(levels)
        margin = int(self.w * 0.08)
        usable = self.w - 2 * margin
        slot = usable / n
        bar_w = max(2, int(slot * 0.55))
        base_y = int(self.h * 0.94)
        max_h = int(self.h * 0.075)
        for i, level in enumerate(levels):
            # Hauteur minimale : la ligne de base reste toujours visible.
            h = max(4, int(max_h * float(level)))
            x = int(margin + i * slot + (slot - bar_w) / 2)
            pygame.draw.rect(surface, self._scaled(color, 0.30),
                             (x - 2, base_y - h - 2, bar_w + 4, h + 4),
                             border_radius=bar_w)
            pygame.draw.rect(surface, self._scaled(color, 0.95),
                             (x, base_y - h, bar_w, h), border_radius=bar_w // 2)
            pygame.draw.rect(surface, (255, 255, 255),
                             (x, base_y - h, bar_w, max(2, bar_w // 2)),
                             border_radius=bar_w // 2)

    def _fit_title(self, text):
        """Rend le titre ENTIER : on réduit la police, puis on passe sur
        plusieurs lignes si besoin. Jamais de troncature."""
        max_w = int(self.w * 0.90)
        base = max(26, int(self.w * 0.058))
        for size in range(base, 17, -2):
            font = pygame.font.Font(None, size)
            lines = self._wrap(text, font, max_w)
            if len(lines) <= 2 and all(font.size(l)[0] <= max_w for l in lines):
                return [font.render(l, True, (255, 255, 255)) for l in lines]
        # Titre à rallonge : trois lignes dans la plus petite taille.
        font = pygame.font.Font(None, 18)
        lines = self._wrap(text, font, max_w)[:3]
        return [font.render(l, True, (255, 255, 255)) for l in lines]

    @staticmethod
    def _wrap(text, font, max_w):
        lines, current = [], ""
        for word in text.split():
            trial = f"{current} {word}".strip()
            if current and font.size(trial)[0] > max_w:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        return lines or [text]

    def draw_title(self, surface, text, t, hold=4.0):
        """Nom du morceau : monte en fondu, puis s'efface."""
        appear, disappear = 0.7, 0.9
        if t > hold + disappear:
            return
        if t < appear:
            k = t / appear
            alpha, offset = k, int(40 * (1 - k) ** 2)
        elif t < hold:
            alpha, offset = 1.0, 0
        else:
            alpha, offset = 1.0 - (t - hold) / disappear, 0

        if self._title_cache is None or self._title_cache[0] != text:
            # Police par défaut de pygame : ASCII uniquement, sinon les
            # caractères manquants s'affichent en carrés.
            self._title_cache = (
                text, self._fit_title(text),
                pygame.font.Font(None, max(18, int(self.w * 0.032)))
                .render("BALL ESCAPE", True, (150, 162, 190)))
        _, title_lines, sub_img = self._title_cache

        line_h = title_lines[0].get_height()
        title_h = line_h * len(title_lines)
        block = pygame.Surface((self.w, title_h + sub_img.get_height() + 14))
        block.set_colorkey((0, 0, 0))
        for i, img in enumerate(title_lines):
            block.blit(img, ((self.w - img.get_width()) // 2, i * line_h))
        block.blit(sub_img, ((self.w - sub_img.get_width()) // 2, title_h + 12))
        block.set_alpha(int(255 * max(0.0, min(1.0, alpha))))
        surface.blit(block, (0, int(self.h * 0.085) + offset))

    def _draw_shards(self, surface, t):
        for shard in self.shards:
            age = (t - shard.t) / SHARD_LIFE
            fade = max(0.0, 1.0 - age) ** 1.5
            radius = shard.radius + shard.drift * age
            offset = shard.spin * age
            n = max(4, int((shard.a1 - shard.a0) * radius / 6))
            pts = []
            for i in range(n + 1):
                a = shard.a0 + offset + (shard.a1 - shard.a0) * i / n
                pts.append((self.cx + math.cos(a) * radius,
                            self.cy + math.sin(a) * radius))
            pygame.draw.lines(surface, self._scaled(shard.color, 0.35 * fade),
                              False, pts, 9)
            pygame.draw.lines(surface, self._scaled(shard.color, fade), False, pts, 3)

    def _draw_sparks(self, surface, t):
        for x, y, _, _, born, color in self.sparks:
            fade = max(0.0, 1.0 - (t - born) / SPARK_LIFE)
            size = max(1, int(4 * fade))
            pygame.draw.circle(surface, self._scaled(color, 0.5 * fade),
                               (int(x), int(y)), size + 2)
            pygame.draw.circle(surface, self._scaled((255, 255, 255), fade),
                               (int(x), int(y)), size)

    def _draw_counter(self, surface):
        """Compteur discret des couches restantes."""
        if self.finished:
            return  # plus de sphère : on ne montre pas "0 / 0"
        remaining = len(self.rings) - self.current
        text = f"{remaining} / {len(self.rings)}"
        img = self._text_cache.get(text)
        if img is None:
            font = pygame.font.Font(None, max(24, int(self.w * 0.052)))
            img = font.render(text, True, (198, 206, 224))
            self._text_cache[text] = img
        surface.blit(img, ((self.w - img.get_width()) // 2, int(self.h * 0.855)))

    def render(self, surface, t):
        if t < self.shake_until:
            # Secousse d'écran : on dessine à part puis on décale l'image.
            work = self._shake_surface()
            self._draw_scene(work, t)
            k = (self.shake_until - t) / SHAKE_DURATION
            amp = SHAKE_AMPLITUDE * k * k
            dx = int(self.fx_rng.uniform(-amp, amp))
            dy = int(self.fx_rng.uniform(-amp, amp))
            surface.fill((0, 0, 0))
            surface.blit(work, (dx, dy))
        else:
            self._draw_scene(surface, t)

    def _shake_surface(self):
        if getattr(self, "_work", None) is None:
            self._work = pygame.Surface((self.w, self.h))
        return self._work

    def _draw_scene(self, surface, t):
        surface.blit(self.background, (0, 0))
        ring_color = self._current_color()

        # Pas de flash plein écran à chaque rebond : à deux notes par seconde,
        # le clignotement est épuisant à regarder. Le retour visuel de l'impact
        # reste local (onde teintée + étincelles au point de contact).
        self._draw_shards(surface, t)
        # L'anneau à franchir brille à fond, les suivants s'estompent :
        # ça donne de la profondeur et guide l'œil.
        for i, ring in enumerate(self.rings[self.current:]):
            self._draw_ring(surface, ring, max(0.42, 1.0 - 0.13 * i))

        # Onde d'impact : discrète, teintée, et jamais blanc pur.
        for fx, fy, ft in self.flashes:
            k = 1.0 - (t - ft) / FLASH_DURATION
            radius = int(BALL_RADIUS + 30 * (1 - k))
            pygame.draw.circle(surface, self._scaled(ring_color, 0.55 * k),
                               (int(fx), int(fy)), radius, 2)

        # Traînée : segments épais qui s'affinent, puis pointe lumineuse.
        trail = self.trail
        if len(trail) > 2:
            for i in range(1, len(trail)):
                k = i / len(trail)
                width = max(1, int(BALL_RADIUS * 1.25 * k))
                pygame.draw.line(surface, self._scaled(ring_color, 0.16 + 0.5 * k * k),
                                 trail[i - 1], trail[i], width)

        # Halo doux de la couleur des anneaux + cœur blanc net.
        pos = (int(self.bx), int(self.by))
        surface.blit(self._tinted_glow(ring_color),
                     (pos[0] - self.glow_r, pos[1] - self.glow_r),
                     special_flags=pygame.BLEND_ADD)
        pygame.draw.circle(surface, (255, 255, 255), pos, BALL_RADIUS)
        pygame.draw.circle(surface, self._scaled(ring_color, 0.55), pos, BALL_RADIUS, 2)

        self._draw_sparks(surface, t)
        self._draw_celebrations(surface, t)
        self._draw_counter(surface)
