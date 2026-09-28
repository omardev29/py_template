import {{pkg}}.core.world as world


def test_spawn_is_deterministic() -> None:
    a = world.World(800, 600)
    b = world.World(800, 600)
    a.spawn(100.0, 100.0, 50, 4)
    b.spawn(100.0, 100.0, 50, 4)
    assert [(x.vx, x.vy, x.tint) for x in a.bunnies] == [(x.vx, x.vy, x.tint) for x in b.bunnies]
    assert all(0 <= x.tint < 4 for x in a.bunnies)


def test_bunnies_stay_inside() -> None:
    game = world.World(800, 600)
    game.spawn(400.0, 300.0, 200, 6)
    for _ in range(600):
        game.update(1 / 60)
    assert all(0.0 <= b.x <= game.width and 0.0 <= b.y <= game.height for b in game.bunnies)
