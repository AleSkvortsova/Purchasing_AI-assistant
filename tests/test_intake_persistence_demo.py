import subprocess
import sys


def test_persistent_demo_restores_replays_and_completes() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/demo_persistent_intake.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Replay: replayed=true" in result.stdout
    assert "Итог: ready_for_confirmation" in result.stdout
    assert "Message logs: 14" in result.stdout
    assert "Карточка: Монитор" in result.stdout
