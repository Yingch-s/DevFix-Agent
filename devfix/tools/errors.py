"""工具层共享异常。"""


class ToolError(Exception):
    """工具执行失败（git 报错、路径越界、文件不存在、搜索失败等）。

    工具层约定：一切失败都以 ToolError 抛出，由调用方（Agent 节点或 CLI）
    决定是反馈给模型自纠还是终止当前阶段。 stderr/细节保留在消息中。
    """
