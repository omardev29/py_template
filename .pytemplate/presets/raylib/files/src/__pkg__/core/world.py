"""Estado y física del juego (sin raylib): lo compila mypyc.

- Clases nativas (@final, atributos float): mypyc las guarda como dobles de C, sin
  objetos intermedios.
- Constantes Final: un global sin Final se busca en un diccionario en cada acceso.
- Aleatoriedad propia (LCG): determinista e igual en CPython, PyPy y mypyc.
- Sintaxis de Python 3.11 (PyPy): nada de `type X = ...` ni genéricos PEP 695.
"""

from __future__ import annotations

from typing import Final, final

SPRITE: Final = 32
GRAVITY: Final = 900.0  # px/s²
BOUNCE: Final = 0.85
MAX_SPEED: Final = 600.0


@final
class Bunny:
    x: float
    y: float
    vx: float
    vy: float
    tint: int

    def __init__(self, x: float, y: float, vx: float, vy: float, tint: int) -> None:
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.tint = tint


@final
class World:
    width: float
    height: float
    bunnies: list[Bunny]
    seed: int

    def __init__(self, width: int, height: int, seed: int = 12345) -> None:
        self.width = float(width - SPRITE)
        self.height = float(height - SPRITE)
        self.bunnies = []
        self.seed = seed

    def random(self) -> float:
        """Número en [0, 1) con un generador congruencial lineal."""
        self.seed = (self.seed * 1103515245 + 12345) & 0x7FFFFFFF
        return self.seed / 2147483648.0

    def spawn(self, x: float, y: float, count: int, tints: int) -> None:
        for _ in range(count):
            vx = (self.random() - 0.5) * MAX_SPEED
            vy = (self.random() - 0.5) * MAX_SPEED
            self.bunnies.append(Bunny(x, y, vx, vy, int(self.random() * tints)))

    def update(self, dt: float) -> None:
        width = self.width
        height = self.height
        for b in self.bunnies:
            b.vy += GRAVITY * dt
            b.x += b.vx * dt
            b.y += b.vy * dt
            if b.x < 0.0:
                b.x = 0.0
                b.vx = -b.vx
            elif b.x > width:
                b.x = width
                b.vx = -b.vx
            if b.y > height:
                b.y = height
                b.vy = -b.vy * BOUNCE
            elif b.y < 0.0:
                b.y = 0.0
                b.vy = 0.0
