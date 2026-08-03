"""Aether — VRChat 虚拟角色系统

架构分层：
    contracts/  跨服务数据契约（无依赖）
    core/       编排机制：事实库、调度器、心跳、紧急停止
    services/   可独立检修与替换的功能模块
    infra/      配置、日志、外部客户端
    runtime/    装配与生命周期
"""

__version__ = "0.1.0"
