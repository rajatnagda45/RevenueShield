from fastapi.testclient import TestClient


def test_health_check_status_code(client: TestClient) -> None:
    """Test that GET /health returns HTTP 200 OK."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_database_health_check(client: TestClient) -> None:
    """Test that GET /health/db returns HTTP 200 OK and database connected status."""
    response = client.get("/health/db")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "connected"}
