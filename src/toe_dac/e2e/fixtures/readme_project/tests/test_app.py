from app import greet


def test_greet():
    assert greet("Andy") == "Hello, Andy!"
