"""Simulation et rendu du gameplay "ball escape".

Une balle rebondit à l'intérieur d'anneaux néon concentriques qui tournent,
chacun percé d'une ouverture. Elle s'échappe couche par couche ; chaque
rebond et chaque évasion est un événement horodaté qui servira à jouer la
note suivante de la musique. Quand toutes les couches sont passées, un
nouveau jeu d'anneaux apparaît avec une autre couleur néon.

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

GRAVITY = 900.0
BALL_RADIUS = 16
RESTITUTION = 1.0
SPEED_GAIN = 1.015     # légère accélération à chaque rebond
MAX_SPEED = 1500.0
MIN_SPEED = 350.0
FLASH_DURATION = 0.25
FADE_DURATION = 0.5


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
        self.rings = []
        self.current = 0
        self.trail = []  # dernières positions de la balle (traînée lumineuse)
        self._new_ring_set()

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

    def step(self, dt, t):
        """Avance la physique de dt secondes ; retourne les temps des événements."""
        events = []
        for ring in self.rings:
            ring.gap_center += ring.speed * dt

        self.vy += GRAVITY * dt
        speed = math.hypot(self.vx, self.vy)
        if speed > MAX_SPEED:
            factor = MAX_SPEED / speed
            self.vx *= factor
            self.vy *= factor
        self.bx += self.vx * dt
        self.by += self.vy * dt

        if self.current >= len(self.rings):
            self._new_ring_set()
            return events

        ring = self.rings[self.current]
        dx = self.bx - self.cx
        dy = self.by - self.cy
        dist = math.hypot(dx, dy) or 0.001

        if dist + BALL_RADIUS >= ring.radius:
            ball_angle = math.atan2(dy, dx)
            in_gap = abs(_ang_diff(ball_angle, ring.gap_center)) < ring.gap_half * 0.9
            if in_gap:
                if dist - BALL_RADIUS > ring.radius:
                    # Évasion de la couche courante.
                    ring.fade = t
                    self.fading.append(ring)
                    self.current += 1
                    events.append(t)
                    self.flashes.append((self.bx, self.by, t))
                    if self.current >= len(self.rings):
                        self._new_ring_set()
            else:
                nx, ny = dx / dist, dy / dist
                outward = self.vx * nx + self.vy * ny
                if outward > 0:
                    self.vx -= 2 * outward * nx
                    self.vy -= 2 * outward * ny
                    self.vx *= RESTITUTION * SPEED_GAIN
                    self.vy *= RESTITUTION * SPEED_GAIN
                    speed = math.hypot(self.vx, self.vy)
                    if speed < MIN_SPEED:
                        factor = MIN_SPEED / speed
                        self.vx *= factor
                        self.vy *= factor
                    # Repositionne la balle contre la paroi.
                    contact = ring.radius - BALL_RADIUS
                    self.bx = self.cx + nx * contact
                    self.by = self.cy + ny * contact
                    events.append(t)
                    self.flashes.append((self.bx, self.by, t))

        self.fading = [r for r in self.fading if t - r.fade < FADE_DURATION]
        self.flashes = [f for f in self.flashes if t - f[2] < FLASH_DURATION]
        return events

    # ------------------------------------------------------------------ Rendu

    @staticmethod
    def _scaled(color, factor):
        return tuple(min(255, int(c * factor)) for c in color)

    def _draw_ring(self, surface, ring, brightness=1.0):
        start = ring.gap_center + ring.gap_half
        end = ring.gap_center + 2 * math.pi - ring.gap_half
        span = end - start
        n = max(16, min(360, int(span * ring.radius / 8)))
        pts = []
        for i in range(n + 1):
            a = start + span * i / n
            pts.append((self.cx + math.cos(a) * ring.radius,
                        self.cy + math.sin(a) * ring.radius))
        color = ring.color
        for factor, width in ((0.18, 15), (0.45, 8), (1.0, 4)):
            pygame.draw.lines(surface, self._scaled(color, factor * brightness),
                              False, pts, width)
        white = self._scaled((255, 255, 255), 0.85 * brightness)
        pygame.draw.lines(surface, white, False, pts, 1)

    def render(self, surface, t):
        surface.fill((4, 4, 10))
        for ring in self.fading:
            brightness = max(0.0, 1.0 - (t - ring.fade) / FADE_DURATION)
            self._draw_ring(surface, ring, brightness * 0.7)
        for ring in self.rings[self.current:]:
            self._draw_ring(surface, ring)
        for fx, fy, ft in self.flashes:
            k = 1.0 - (t - ft) / FLASH_DURATION
            radius = int(BALL_RADIUS + 26 * (1 - k))
            pygame.draw.circle(surface, self._scaled((255, 255, 255), 0.6 * k),
                               (int(fx), int(fy)), radius, 2)
        # Traînée lumineuse de la couleur des anneaux actuels.
        ring_color = self.rings[min(self.current, len(self.rings) - 1)].color
        self.trail.append((self.bx, self.by))
        if len(self.trail) > 14:
            self.trail.pop(0)
        for i, (tx, ty) in enumerate(self.trail[:-1]):
            k = (i + 1) / len(self.trail)
            pygame.draw.circle(surface, self._scaled(ring_color, 0.35 * k),
                               (int(tx), int(ty)), max(2, int(BALL_RADIUS * 0.7 * k)))
        # Balle avec halo coloré + cœur blanc.
        pos = (int(self.bx), int(self.by))
        pygame.draw.circle(surface, self._scaled(ring_color, 0.35), pos, BALL_RADIUS + 11)
        pygame.draw.circle(surface, self._scaled((255, 255, 255), 0.6), pos, BALL_RADIUS + 4)
        pygame.draw.circle(surface, (255, 255, 255), pos, BALL_RADIUS)
