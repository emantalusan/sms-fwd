from __future__ import print_function
import json
import os
import logging
import logging.handlers
from gsmmodem.modem import GsmModem
from collections import defaultdict
import queue
import threading
import time
from db import init_database, save_or_update_sms
from sms import sms_forward_worker, handleSms
from email import email_forward_worker
from api import api_forward_worker

# Set up logging to syslog
logger = logging.getLogger('SMSForwarder')
logger.setLevel(logging.INFO)
syslog_handler = logging.handlers.SysLogHandler(address='/dev/log', facility=logging.handlers.SysLogHandler.LOG_USER)
formatter = logging.Formatter('%(name)s: %(levelname)s %(message)s')
syslog_handler.setFormatter(formatter)
logger.addHandler(syslog_handler)

# Global variables
multipart_messages = defaultdict(lambda: defaultdict(dict))
CONFIG_FILE = "config.json"
SAMPLE_CONFIG_FILE = "config.json.sample"
api_queue = queue.Queue()
sms_queue = queue.Queue()
email_queue = queue.Queue()
failed_services = set()

def load_config():
    """Load configuration from config.json"""
    if not os.path.exists(CONFIG_FILE):
        if not os.path.exists(SAMPLE_CONFIG_FILE):
            raise FileNotFoundError(f"Neither {CONFIG_FILE} nor {SAMPLE_CONFIG_FILE} found.")
        with open(SAMPLE_CONFIG_FILE, 'r') as f:
            default_config = json.load(f)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(default_config, f, indent=4)
        logger.info(f"Created default config file {CONFIG_FILE}")
        return default_config
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        required_keys = ["modem", "sms_recipients", "email", "api_providers", "database", "default_timeout"]
        if not all(key in config for key in required_keys):
            raise ValueError(f"Invalid config format: missing one of {required_keys}")
        if config.get("debug", False):
            logger.setLevel(logging.DEBUG)
        logger.info("Config loaded successfully")
        return config
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Error loading config: {e}. Using default config")
        with open(SAMPLE_CONFIG_FILE, 'r') as f:
            return json.load(f)

def main():
    config = load_config()
    
    modem_config = config["modem"]
    db_file = config["database"]["file"]
    sms_recipients = config["sms_recipients"]
    email_config = config["email"]
    api_providers = config["api_providers"]
    
    init_database(db_file)
    
    logger.info("Initializing modem...")
    modem = GsmModem(modem_config["port"], modem_config["baudrate"], smsReceivedCallbackFunc=handleSms)
    modem.smsTextMode = False
    modem.connect(modem_config["pin"])
    modem.waitForNetworkCoverage(10)
    
    api_thread = threading.Thread(target=api_forward_worker, args=(api_providers, db_file), daemon=True, name="API-Forwarder")
    sms_thread = threading.Thread(target=sms_forward_worker, args=(modem, db_file, sms_recipients), daemon=True, name="SMS-Forwarder")
    email_thread = threading.Thread(target=email_forward_worker, args=(email_config, db_file), daemon=True, name="Email-Forwarder")
    api_thread.start()
    sms_thread.start()
    email_thread.start()
    logger.info("Started forwarding threads")
    
    logger.info("Waiting for SMS message...")
    try:
        modem.rxThread.join(2**31)
    finally:
        modem.close()
        logger.info("Modem closed")

if __name__ == '__main__':
    main()