"""LLM Provider 工厂。

业务代码只依赖本模块的 get_chat_model()，对具体厂商零感知。
ark / openai / deepseek 三家均提供 OpenAI 兼容接口，统一用
langchain-openai 的 ChatOpenAI 接入；未来新增 provider 只需在
工厂中加一个分支，不影响任何业务代码。
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from devfix.config import LLMConfig

# 走 OpenAI 兼容接口的 provider 集合
_OPENAI_COMPATIBLE = {"ark", "openai", "deepseek"}


def get_chat_model(cfg: LLMConfig) -> BaseChatModel:
    """根据配置构造 LangChain ChatModel。

    Args:
        cfg: LLMConfig（provider/model/base_url/温度等）。

    Returns:
        LangChain BaseChatModel，可直接用于 tool calling / structured output。

    Raises:
        ValueError: provider 不受支持。
        ConfigError: API Key 缺失（见 cfg.resolve_api_key）。
    """
    if cfg.provider in _OPENAI_COMPATIBLE:
        # 延迟导入：让 `devfix --version` 等不触碰 LLM 的命令
        # 在未安装 langchain-openai 的环境下也能工作
        from langchain_openai import ChatOpenAI

        # provider 专有参数（如 deepseek 的 thinking 开关）必须经 extra_body 传递：
        # 顶层传会被 OpenAI SDK 当作未知参数直接拒绝
        # （TypeError: create() got an unexpected keyword argument 'thinking'）。
        extra_body: dict = dict(cfg.extra_params)
        if cfg.provider == "deepseek":
            # DeepSeek V4 Pro 默认开启思考模式（effort=high），而思考模式不支持
            # 强制 tool_choice（A400 "Thinking mode does not support this tool_choice"），
            # 与 structured output 冲突。结构化抽取调用显式关闭思考，
            # 推理链由 prompt 要求写在 observations / inference 字段中。
            # 如需开启思考（会失去强制工具调用能力，见技术选型文档「Provider 兼容性」），
            # 在 devfix.yaml 中设置 llm.extra_params.thinking。
            extra_body.setdefault("thinking", {"type": "disabled"})
        kwargs: dict = {
            "model": cfg.model,
            "api_key": cfg.resolve_api_key(),
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "timeout": 60,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        base_url = cfg.effective_base_url
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)

    raise ValueError(
        f"不支持的 provider: {cfg.provider}（当前支持: {sorted(_OPENAI_COMPATIBLE)}）"
    )
