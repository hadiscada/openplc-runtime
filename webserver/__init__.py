import logging

__version__ = "0.1"
__author__ = "Autonomy"
__license__ = "MIT"
__description__ = "RestAPI interface for runtime core"

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(levelname)s] %(asctime)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

__all__ = ["logger"]
