from src.config import load_config
from src.llm.client import LLMClient


def test_load_config_loads_llm_connection_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_TEMPERATURE",
        "LLM_MAX_TOKENS",
        "LLM_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(
        """
llm:
  provider: yaml-provider
  model: yaml-model
  base_url: https://yaml.example.com/v1
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "LLM_PROVIDER=dotenv-provider",
                "LLM_MODEL=dotenv-model",
                "LLM_API_KEY=from-dotenv",
                "LLM_BASE_URL=https://dotenv.example.com/v1",
                "LLM_TEMPERATURE=0.2",
                "LLM_MAX_TOKENS=1024",
                "LLM_TIMEOUT_SECONDS=240",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config("config.yaml")
    client = LLMClient.from_config(config.llm)

    assert config.llm.provider == "dotenv-provider"
    assert config.llm.model == "dotenv-model"
    assert config.llm.base_url == "https://dotenv.example.com/v1"
    assert config.llm.temperature == 0.2
    assert config.llm.max_tokens == 1024
    assert config.llm.timeout_seconds == 240
    assert client.api_key == "from-dotenv"
    assert client.timeout_seconds == 240


def test_dotenv_does_not_override_exported_llm_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "from-shell")
    (tmp_path / ".env").write_text("LLM_API_KEY=from-dotenv\n", encoding="utf-8")

    config = load_config("missing-config.yaml")
    client = LLMClient.from_config(config.llm)

    assert client.api_key == "from-shell"
