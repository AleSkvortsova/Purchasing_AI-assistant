from scripts.demo_request_lifecycle import main


def test_lifecycle_demo_runs_offline(capsys) -> None:
    assert main() == 0
    output = capsys.readouterr().out
    assert "A confirm:" in output
    assert "replay=true" in output
    assert "B edit:" in output
    assert "amount=220000" in output
    assert "C cancel:" in output
    assert "status=cancelled" in output
