"""Simulation et rendu du gameplay "ball escape".

Une balle rebondit à l'intérieur d'anneaux néon concentriques qui tournent,
chacun percé d'une ouverture. Elle s'échappe couche par couche ; chaque
rebond, chaque évasion et chaque sphère terminée est un événement horodaté
(bounce / escape / complete) qui pilotera le son et les effets visuels.

Rendu headless : pygame dessine sur des Surfaces mémoire (pas d'écran),
les frames brutes sont envoyées à ffmpeg via stdin.
"""

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
GRAVITY = 1500.0
RESTITUTION = 0.94        # perte d'énergie à chaque rebond
TANGENT_FRICTION = 0.99   # frottement le long de la paroi
REVIVE_SPEED = 300.0      # en dessous, la balle reçoit une impulsion
REVIVE_BOOST = 1.5
ESCAPE_BOOST = 1.12       # accélération de récompense en franchissant un anneau
MAX_SPEED = 1600.0
# Anti-blocage : la balle doit toujours repartir franchement de la paroi,
# sinon la gravité la replaque aussitôt et elle mitraille sur place.
MIN_SEPARATION_SPEED = 300.0
BOUNCE_COOLDOWN = 0.035   # deux rebonds ne peuvent pas s'enchaîner plus vite
WALL_CLEARANCE = 2.0      # on la repose légèrement en retrait de la paroi

FLASH_DURATION = 0.25
FADE_DURATION = 0.5
CELEBRATION_DURATION = 1.4
PARTICLE_COUNT = 26
TRAIL_LENGTH = 34         # positions gardées pour la traînée (sous-pas physiques)


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
        self.fade = None  # temps de début de disparition


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
    def __init__(self, width, height, ring_count, rng):
        self.w = width
        self.h = height
        self.cx = width / 2
        self.cy = height / 2
        self.ring_count = ring_count
        self.rng = rng
        self.max_radius = min(width, height) / 2 - 40
        self.flashes = []  # (x, y, t)
        self.fading = []
        self.celebrations = []
        self.rings = []
        self.current = 0
        self.spheres_done = 0
        self.trail = []  # positions récentes (échantillonnées à chaque sous-pas)
        self.last_bounce_t = -1.0
        self._glow_cache = {}
        self._title_cache = None
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

    def _new_ring_set(self):
        color = self.rng.choice(NEON_PALETTE)
        base = BALL_RADIUS * 2 + 100
        spacing = (self.max_radius - base) / max(1, self.ring_count - 1)
        self.rings = []
        for i in range(self.ring_count):
            self.rings.append(Ring(
                radius=base + i * spacing,
                gap_center=self.rng.uniform(0, 2 * math.pi),
                gap_half=math.radians(self.rng.uniform(22, 34)),
                speed=self.rng.uniform(0.35, 0.95) * self.rng.choice((-1, 1)),
                color=color,
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

    def step(self, dt, t):
        """Avance la physique de dt ; retourne une liste de (temps, type)."""
        events = []
        for ring in self.rings:
            ring.gap_center += ring.speed * dt

        self.vy += GRAVITY * dt
        self._clamp_speed()
        self.bx += self.vx * dt
        self.by += self.vy * dt

        ring = self.rings[self.current]
        dx = self.bx - self.cx
        dy = self.by - self.cy
        dist = math.hypot(dx, dy) or 0.001

        if dist + BALL_RADIUS >= ring.radius:
            ball_angle = math.atan2(dy, dx)
            in_gap = abs(_ang_diff(ball_angle, ring.gap_center)) < ring.gap_half * 0.9
            if in_gap:
                if dist - BALL_RADIUS > ring.radius:
                    # Évasion de la couche courante : petit boost de récompense.
                    ring.fade = t
                    self.fading.append(ring)
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
                        events.append((t, "complete"))
                        self._new_ring_set()
            else:
                nx, ny = dx / dist, dy / dist
                outward = self.vx * nx + self.vy * ny
                if outward > 0 and t - self.last_bounce_t >= BOUNCE_COOLDOWN:
                    # Réflexion : composante normale inversée et amortie,
                    # composante tangentielle légèrement freinée.
                    tx, ty = -ny, nx
                    vt = (self.vx * tx + self.vy * ty) * TANGENT_FRICTION
                    # La normale repart toujours assez fort pour décoller de la
                    # paroi : sinon la gravité la replaque et la balle vibre
                    # sur place au lieu de rebondir.
                    vn = max(outward * RESTITUTION, MIN_SEPARATION_SPEED)
                    self.vx = -nx * vn + tx * vt
                    self.vy = -ny * vn + ty * vt
                    speed = math.hypot(self.vx, self.vy)
                    if speed < REVIVE_SPEED:
                        # La balle s'endort : coup de fouet pour relancer le jeu.
                        factor = REVIVE_BOOST * REVIVE_SPEED / max(speed, 1.0)
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

        # Traînée échantillonnée à chaque sous-pas : elle reste lisse même
        # quand la balle traverse l'écran en quelques images.
        self.trail.append((self.bx, self.by))
        if len(self.trail) > TRAIL_LENGTH:
            del self.trail[:len(self.trail) - TRAIL_LENGTH]

        self.fading = [r for r in self.fading if t - r.fade < FADE_DURATION]
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
        color = self.rings[min(self.current, len(self.rings) - 1)].color
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
            font = pygame.font.Font(None, max(26, int(self.w * 0.058)))
            label = text if len(text) <= 30 else text[:29] + "…"
            # Police par défaut de pygame : ASCII uniquement, sinon les
            # caractères manquants s'affichent en carrés.
            self._title_cache = (text, font.render(label, True, (255, 255, 255)),
                                 pygame.font.Font(None, max(18, int(self.w * 0.032)))
                                 .render("BALL ESCAPE", True, (150, 162, 190)))
        _, title_img, sub_img = self._title_cache

        block = pygame.Surface((self.w, title_img.get_height() + sub_img.get_height() + 14))
        block.set_colorkey((0, 0, 0))
        block.blit(title_img, ((self.w - title_img.get_width()) // 2, 0))
        block.blit(sub_img, ((self.w - sub_img.get_width()) // 2,
                             title_img.get_height() + 12))
        block.set_alpha(int(255 * max(0.0, min(1.0, alpha))))
        surface.blit(block, (0, int(self.h * 0.085) + offset))

    def render(self, surface, t):
        surface.blit(self.background, (0, 0))
        ring_color = self.rings[min(self.current, len(self.rings) - 1)].color

        for ring in self.fading:
            brightness = max(0.0, 1.0 - (t - ring.fade) / FADE_DURATION)
            self._draw_ring(surface, ring, brightness * 0.7)
        for ring in self.rings[self.current:]:
            self._draw_ring(surface, ring)

        # Onde blanche à l'endroit de chaque impact.
        for fx, fy, ft in self.flashes:
            k = 1.0 - (t - ft) / FLASH_DURATION
            radius = int(BALL_RADIUS + 30 * (1 - k))
            pygame.draw.circle(surface, self._scaled((255, 255, 255), 0.55 * k),
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

        self._draw_celebrations(surface, t)
