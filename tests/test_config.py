"""config 模块单元测试。

覆盖：provider 默认值、API Key 环境变量解析、配置文件加载与报错路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devfix.config import (
    ConfigError,
    DevFixConfig,
    LLMConfig,
    find_config,
    load_config,
)

VALID_YAML = """\
llm:
  provider: ark
  model: doubao-test-model
"""


class TestLLMConfigDefaults:
    def test_ark_defaults(self) -> None:
        cfg = LLMConfig(provider="ark", model="m")
        assert cfg.api_key_env == "ARK_API_KEY"
        assert cfg.effective_base_url == "https://ark.cn-beijing.volcesapi.com/api/v3"

    def test_openai_defaults(self) -> None:
        cfg = LLMConfig(provider="openai", model="m")
        assert cfg.api_key_env == "OPENAI_API_KEY"
        assert cfg.effective_base_url is None  # SDK 默认

    def test_deepseek_defaults(self) -> None:
        cfg = LLMConfig(provider="deepseek", model="m")
        assert cfg.api_key_env == "DEEPSEEK_API_KEY"
        assert cfg.effective_base_url == "https://api.deepseek.com"

    def test_explicit_values_override_defaults(self) -> None:
        cfg = LLMConfig(
            provider="ark", model="m",
            base_url="http://localhost:9999/v1", api_key_env="MY_KEY",
        )
        assert cfg.effective_base_url == "http://localhost:9999/v1"
        assert cfg.api_key_env == "MY_KEY"

    def test_default_temperature_and_max_tokens(self) -> None:
        cfg = LLMConfig(provider="ark", model="m")
        assert cfg.temperature == 0.0
        assert cfg.max_tokens == 8192


class TestResolveApiKey:
    def test_missing_env_raises_with_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARK_API_KEY", raising=False)
        cfg = LLMConfig(provider="ark", model="m")
        with pytest.raises(ConfigError, match="ARK_API_KEY"):
            cfg.resolve_api_key()

    def test_env_value_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARK_API_KEY", "  test-key-123  ")
        cfg = LLMConfig(provider="ark", model="m")
        assert cfg.resolve_api_key() == "test-key-123"


class TestLoadConfig:
    def test_valid_file(self, tmp_path) -> None:
        p = tmp_path / "devfix.yaml"
        p.write_text(VALID_YAML, encoding="utf-8")
        cfg = load_config(p)
        assert isinstance(cfg, DevFixConfig)
        assert cfg.llm.provider == "ark"
        assert cfg.llm.model == "doubao-test-model"

    def test_missing_file_raises(self, tmp_path) -> None:
        with pytest.raises(ConfigError, match="配置文件不存在"):
            load_config(tmp_path / "nope.yaml")

    def test_invalid_yaml_raises(self, tmp_path) -> None:
        p = tmp_path / "devfix.yaml"
        p.write_text("llm: [unclosed", encoding="utf-8")
        with pytest.raises(ConfigError, match="YAML"):
            load_config(p)

    def test_missing_required_field_raises(self, tmp_path) -> None:
        p = tmp_path / "devfix.yaml"
        p.write_text("llm:\n  provider: ark\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="校验失败"):
            load_config(p)

    def test_unknown_provider_raises(self, tmp_path) -> None:
        p = tmp_path / "devfix.yaml"
        p.write_text("llm:\n  provider: claude\n  model: m\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="校验失败"):
            load_config(p)


class TestFindConfig:
    def test_found_in_start_dir(self, tmp_path) -> None:
        (tmp_path / "devfix.yaml").write_text(VALID_YAML, encoding="utf-8")
        assert find_config(tmp_path) == tmp_path / "devfix.yaml"

    def test_found_in_parent_dir(self, tmp_path) -> None:
        nested = tmp_path / "repo" / "subdir"
        nested.mkdir(parents=True)
        (tmp_path / "devfix.yaml").write_text(VALID_YAML, encoding="utf-8")
        assert find_config(nested) == tmp_path / "devfix.yaml"

    def test_not_found_returns_none(self, tmp_path) -> None:
        assert find_config(tmp_path) is None

    def test_home_fallback(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        # cwd 所在目录树里找不到时，回退到用户主目录的 devfix.yaml（跨盘符兜底）
        far = tmp_path / "elsewhere"
        far.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        (home / "devfix.yaml").write_text(VALID_YAML, encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        assert find_config(far) == home / "devfix.yaml"

    def test_load_config_uses_search_chain(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nested = tmp_path / "repo" / "subdir"
        nested.mkdir(parents=True)
        (tmp_path / "devfix.yaml").write_text(VALID_YAML, encoding="utf-8")
        monkeypatch.setattr(Path, "cwd", classmethod(lambda cls: nested))
        cfg = load_config()  # 不传 path → 走搜索链
        assert cfg.llm.provider == "ark"


class TestProviderFactory:
    """provider 工厂行为（provider 专有 quirks 的归属层）。"""

    def test_deepseek_disables_thinking_via_extra_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from devfix.llm.provider import get_chat_model

        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        m = get_chat_model(LLMConfig(provider="deepseek", model="deepseek-v4-pro"))
        # thinking 必须走 extra_body（DeepSeek 文档要求），顶层传会被 SDK 拒绝
        assert m.extra_body.get("thinking") == {"type": "disabled"}
        assert "thinking" not in (m.model_kwargs or {})

    def test_extra_params_overrides_thinking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from devfix.llm.provider import get_chat_model

        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        m = get_chat_model(
            LLMConfig(
                provider="deepseek", model="m",
                extra_params={"thinking": {"type": "enabled"}},
            )
        )
        assert m.extra_body["thinking"] == {"type": "enabled"}

    def test_other_providers_no_thinking_injection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from devfix.llm.provider import get_chat_model

        monkeypatch.setenv("OPENAI_API_KEY", "k")
        m = get_chat_model(LLMConfig(provider="openai", model="gpt-4o-mini"))
        assert "thinking" not in (m.extra_body or {})
