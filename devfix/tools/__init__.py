"""Tool Layer：Agent 的受控执行工具（对应系统设计第 10 节）。

Agent 不直接访问操作系统，只能通过本包的工具读写仓库、执行 git、
搜索代码——工具返回真实结果，模型无法"假装执行成功"。
"""

from devfix.tools.errors import ToolError
from devfix.tools.file_tool import FileTool
from devfix.tools.git_tool import GitTool
from devfix.tools.maven_tool import MavenTool, find_mvn, tail_lines
from devfix.tools.patch_tool import PatchTool
from devfix.tools.search_tool import SearchTool

__all__ = [
    "FileTool",
    "GitTool",
    "MavenTool",
    "PatchTool",
    "SearchTool",
    "ToolError",
    "find_mvn",
    "tail_lines",
]
