"""运行时装配层"""

from .agent import AetherRuntime
from .cli import run_cli
from .coordinator import CognitiveCoordinator
from .executors import BodyExecutorSuite
from .motion_wiring import compute_plan_io_allowance, create_motion_provider
from .perception_gate import PerceptionGate, compute_pcm16_rms

__all__ = [
    "AetherRuntime",
    "BodyExecutorSuite",
    "CognitiveCoordinator",
    "PerceptionGate",
    "compute_pcm16_rms",
    "compute_plan_io_allowance",
    "create_motion_provider",
    "run_cli",
]
