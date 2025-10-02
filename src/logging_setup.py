
import logging
from logging.handlers import RotatingFileHandler
import sys

# Custom filter to allow only INFO and WARNING level logs
class InfoFilter(logging.Filter):
    def filter(self, record):
        return record.levelno in (logging.INFO, logging.WARNING)

def setup_logging():
    """Configures logging to file and console."""
    # Get the root logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Create a formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # --- Info Log Handler (log.txt) ---
    # This handler will write INFO and WARNING messages to log.txt
    info_handler = RotatingFileHandler(
        'log.txt', 
        maxBytes=5*1024*1024,  # 5 MB
        backupCount=5
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)
    info_handler.addFilter(InfoFilter()) # Apply the custom filter
    logger.addHandler(info_handler)

    # --- Error Log Handler (error.txt) ---
    # This handler will write ERROR and CRITICAL messages to error.txt
    error_handler = RotatingFileHandler(
        'error.txt', 
        maxBytes=5*1024*1024, # 5 MB
        backupCount=5
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)

    # --- Console Log Handler (stdout) ---
    # This handler will print all logs (INFO and above) to the console
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
