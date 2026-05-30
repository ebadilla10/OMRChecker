# https://docs.python.org/3/tutorial/modules.html#:~:text=The%20__init__.py,on%20the%20module%20search%20path.
import os

os.environ.setdefault("MPLCONFIGDIR", "/tmp/omrchecker-matplotlib")

from src.logger import logger

# It takes a few seconds for the imports
logger.info(f"Loading OMRChecker modules...")
