from plugin import run


def test_image_environment_needs_no_network_and_does_not_enqueue(tmp_path):
    before = run({"args": {"mode": "status"}}, tmp_path)
    environment = run({"args": {"mode": "image_environment"}}, tmp_path)
    assert environment["python"] and environment["platform"]
    assert isinstance(environment["pillow"], bool)
    assert run({"args": {"mode": "status"}}, tmp_path) == before
