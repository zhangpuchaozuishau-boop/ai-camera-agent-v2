"""Real hardware adapters for the DustCar ESP32-S3 firmware (additive layer).

Nothing here changes the frozen Agent core: these classes implement the existing
MotionCompiler / Executor adapter boundaries against the firmware HTTP contract.
calibration.json holds every mapping constant WITH its provenance; constants
marked verified=false are placeholders and make the motion output unreliable.
"""

from .compiler import DustCarCalibration, DustCarMotionCompiler, dustcar_registry
from .corrections import CorrectionCompiler, budget_from
from .loop import VisionLoop, normalize_box
from .dustcar import ChassisKeepalive, DustCarClient, hold_chassis
from .executor import DryRunClient, DustCarExecutor

__all__ = ["DustCarClient", "ChassisKeepalive", "hold_chassis", "DustCarCalibration", "DustCarMotionCompiler",
           "dustcar_registry", "DustCarExecutor", "DryRunClient", "CorrectionCompiler", "budget_from",
           "VisionLoop", "normalize_box"]
