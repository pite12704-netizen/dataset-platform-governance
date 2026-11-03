import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import add_user
from app.config import Settings
from app.main import create_app
from app.schemas import Register


@pytest.fixture
def app(tmp_path):
    cfg = Settings(_env_file=None, environment="test", deployment="public", secret_key="test-secret-key-012345678901234567890123",
                   database_url=f"sqlite:///{tmp_path / 'platform.db'}", storage_backend="local", storage_root=tmp_path / "objects",
                   task_mode="inline", scan_mode="development",
                   model_url='', vision_model_url='', model_name='', smtp_host='',
                   require_admin_mfa=False, require_verification=False, mail_spool=tmp_path / "mail",
                   part_size_bytes=16, rate_limit_per_minute=10000, transfer_limit_per_minute=10000,
                   login_limit_per_minute=10000)
    return create_app(cfg, initialize=True)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def actors(app, client):
    result = {}
    for name in ["owner", "reader", "stranger", "admin"]:
        with app.state.db.Session() as db:
            user = add_user(db, Register(email=f"{name}@example.test", name=name, password="A-long-test-password!"),
                            app.state.settings, verified=True, admin=name == "admin")
            db.commit()
            identifier = user.id
        response = client.post("/api/v1/auth/login", json={"email": f"{name}@example.test", "password": "A-long-test-password!"})
        assert response.status_code == 200, response.text
        header = {"Authorization": "Bearer " + response.json()["access_token"]}
        spaces = client.get("/api/v1/spaces", headers=header).json()
        result[name] = {"id": identifier, "headers": header, "space_id": spaces[0]["id"]}
        client.cookies.clear()
    return result
