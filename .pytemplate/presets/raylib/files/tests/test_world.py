from {{pkg}}.core.world import World


def test_spawn_is_deterministic() -> None:
    a = World(800, 600)
    b = World(800, 600)
    a.spawn(100.0, 100.0, 50, 4)
    b.spawn(100.0, 100.0, 50, 4)
    assert [(x.vx, x.vy, x.tint) for x in a.bunnies] == [(x.vx, x.vy, x.tint) for x in b.bunnies]
    assert all(0 <= x.tint < 4 for x in a.bunnies)


def test_bunnies_stay_inside() -> None:
    world = World(800, 600)
    world.spawn(400.0, 300.0, 200, 6)
    for _ in range(600):
        world.update(1 / 60)
    assert all(0.0 <= b.x <= world.width and 0.0 <= b.y <= world.height for b in world.bunnies)
