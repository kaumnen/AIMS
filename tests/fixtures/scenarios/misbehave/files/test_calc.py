from calc import add


def test_small():
    assert add(2, 3) == 5


def test_zero():
    assert add(0, 0) == 0


def test_negative():
    assert add(-2, -3) == -5
