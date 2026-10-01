"""配置加载与校验。

配置文件为项目根目录的 devfix.yaml（用户私有，gitignore），
API Key 一律通过 api_key_env 指定环境变量名，从环境变量读取，不进配置文件。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, model_validator

ProviderName = Literal["ark", "openai", "deepseek"]

# 各 provider 的默认接入点（OpenAI 兼容接口）
DEFAULT_BASE_URLS: dict[ProviderName, str | None] = {
    "ark": "https://ark.cn-beijing.volcesapi.com/api/v3",
    "deepseek": "https://api.deepseek.com",
    "openai": None,  # 使用 SDK 内置默认值
}

# 各 provider 默认读取哪个环境变量
DEFAULT_API_KEY_ENV: dict[ProviderName, str] = {
    "ark": "ARK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

DEFAULT_CONFIG_FILENAME = "devfix.yaml"

# 与测试无关的质量门禁插件开关：历史提交在现代 JDK 上常被它们挡住
# （实测 jsoup 把 animal-sniffer 绑在 compile 阶段 → 测试根本没机会运行）
QUALITY_GATE_SKIP_ARGS = [
    "-Danimal.sniffer.skip=true",
    "-Dcheckstyle.skip=true",
    "-Dspotbugs.skip=true",
    "-Dpmd.skip=true",
    "-Denforcer.skip=true",
    "-Djapicmp.skip=true",
    "-Dmaven.javadoc.skip=true",
    "-Dlicense.skip=true",
    "-Dforbiddenapis.skip=true",
]


class ConfigError(Exception):
    """配置相关错误（文件缺失、字段非法、凭证缺失等）。"""


class LLMConfig(BaseModel):
    """LLM 接入配置。provider 切换只需改配置文件。"""

    provider: ProviderName
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.0
    max_tokens: int = 8192
    # provider 专有参数直通（如 deepseek 的 thinking 开关），原样合入 API 请求。
    # 注意：字段不能命名为 model_extra——那是 Pydantic v2 保留属性（extras 存储）。
    extra_params: dict = {}

    @model_validator(mode="after")
    def _apply_provider_defaults(self) -> LLMConfig:
        if self.api_key_env is None:
            self.api_key_env = DEFAULT_API_KEY_ENV[self.provider]
        return self

    @property
    def effective_base_url(self) -> str | None:
        return self.base_url or DEFAULT_BASE_URLS[self.provider]

    def resolve_api_key(self) -> str:
        """从环境变量解析 API Key。缺失时抛出带指引的 ConfigError。"""
        assert self.api_key_env is not None  # model_validator 保证不为 None
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ConfigError(
                f"未找到 API Key：请设置环境变量 {self.api_key_env}"
                f"（provider={self.provider}）。"
                f"PowerShell: $env:{self.api_key_env}=\"<key>\"；"
                f"cmd: set {self.api_key_env}=<key>"
            )
        return key


class VerificationConfig(BaseModel):
    """验证层配置（设计文档 2.7 分层超时）。"""

    maven_command: str | None = None  # 留空自动探测（PATH → ~/tools → Program Files）
    focused_timeout_s: int = 300
    related_timeout_s: int = 600
    full_timeout_s: int = 1200
    # 附加给 mvn 的参数（如跳过与测试无关的质量门禁插件）
    maven_args: list[str] = []
    # 跳过质量门禁的预设参数：历史提交在新 JDK 上常被 animal-sniffer /
    # checkstyle 等插件挡住，无法进入测试执行；评测时应聚焦测试本身
    skip_quality_gates: bool = False

    def effective_maven_args(self) -> list[str]:
        """最终传给 mvn 的附加参数。"""
        args = list(self.maven_args)
        if self.skip_quality_gates:
            args += [a for a in QUALITY_GATE_SKIP_ARGS if a not in args]
        return args


class DevFixConfig(BaseModel):
    llm: LLMConfig
    verification: VerificationConfig = VerificationConfig()


def find_config(start: Path | None = None) -> Path | None:
    """按搜索链查找 devfix.yaml：

    1. start 目录（默认当前工作目录）
    2. 从 start 逐级向上直至盘符根目录（类似 git 查找 .git）
    3. 用户主目录 ~/.devfix.yaml 兜底（跨盘符场景，如 cwd 在 D: 而配置在 C:）

    找不到返回 None。
    """
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        p = d / DEFAULT_CONFIG_FILENAME
        if p.exists():
            return p
    home = Path.home() / DEFAULT_CONFIG_FILENAME
    if home.exists():
        return home
    return None


def load_config(path: str | Path | None = None) -> DevFixConfig:
    """加载并校验 devfix.yaml。

    path 为 None 时按 find_config() 的搜索链查找；
    文件缺失或字段非法时抛出 ConfigError。
    """
    if path is None:
        p = find_config()
        if p is None:
            raise ConfigError(
                f"未找到 {DEFAULT_CONFIG_FILENAME}（已搜索当前目录、上级目录及 {Path.home()}）。\n"
                f"在项目根目录运行 `devfix init` 生成，或参考 devfix.example.yaml。"
            )
    else:
        p = Path(path)
        if not p.exists():
            raise ConfigError(
                f"配置文件不存在：{p.resolve()}\n"
                f"可运行 `devfix init` 从模板生成，或参考 devfix.example.yaml。"
            )
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"配置文件 YAML 解析失败：{p}：{e}") from e
    if data is None:
        data = {}
    try:
        return DevFixConfig.model_validate(data)
    except Exception as e:
        raise ConfigError(f"配置文件校验失败：{p}：{e}") from e
