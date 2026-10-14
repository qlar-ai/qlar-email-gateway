import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def load() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]


def test_hosted_entry_point_declared():
    project = load()

    assert project["scripts"]["qlar-email-hosted-worker"] == "qlar_email_gateway.hosted.cli:main"
    assert project["scripts"]["qlar-email-gateway"] == "qlar_email_gateway.cli:main"
    assert project["optional-dependencies"]["hosted"] == [
        "azure-identity>=1.17",
        "azure-keyvault-secrets>=4.8",
    ]
    names = sorted(
        dependency.split(">")[0].split("=")[0] for dependency in project["dependencies"]
    )
    assert names == ["beautifulsoup4", "cryptography", "httpx", "imapclient"]
