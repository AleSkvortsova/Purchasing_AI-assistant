import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def test_intake_health_and_endpoint_are_side_effect_free() -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/intake/health").json() == {
            "status": "ok",
            "mode": "deterministic",
        }
        response = client.post(
            "/api/v1/intake/evaluate-step",
            json={
                "draft": {},
                "update": {"values": {"item_name": "Офисные кресла"}},
                "approval_extraction_result": None,
            },
        )
    assert response.status_code == 200
    assert response.json()["metadata"]["persistence_performed"] is False


def test_intake_cli_runs_without_network(tmp_path: Path) -> None:
    draft = tmp_path / "draft.json"
    update = tmp_path / "update.json"
    draft.write_text("{}", encoding="utf-8")
    update.write_text(json.dumps({"values": {"item_name": "Кресло"}}), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_intake_step.py",
            "--draft",
            str(draft),
            "--update",
            str(update),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "collecting"


def test_show_intake_registry_cli_json() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/show_intake_registry.py",
            "--type",
            "goods",
            "--category",
            "G03",
            "--required-only",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    codes = {row["code"] for row in rows}
    assert {"item_name", "analogs_allowed", "delivery_location"} <= codes
    assert "title" not in codes


def test_show_intake_registry_cli_table() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/show_intake_registry.py", "--all"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "priority | code | label" in result.stdout
    assert "category_code" in result.stdout


def test_demo_from_empty_reaches_ready_status() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/demo_intake_dialog.py", "--from-empty"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Итог: ready_for_confirmation" in result.stdout
    assert "Вопросов задано:" in result.stdout
