"""Loop 控制面的共享公开异常。"""

# 中文排查：LoopError 表示已经校验、可以安全展示给本机 Operator 的控制面错误。
# 未知异常不要直接转成 LoopError 并附原文，先确认其中不含路径、凭据或业务敏感内容。

class LoopError(RuntimeError):
    """经过校验、可以安全报告给本机 Operator 的控制面失败。"""
