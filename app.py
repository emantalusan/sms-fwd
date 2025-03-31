from __future__ import print_function
import json
import os
import logging
import logging.handlers
from gsmmodem.modem import GsmModem
from gsmmodem.pdu import Concatenation
from collections import defaultdict
import smtplib
from email.mime.text import MIMEText
import sqlite3
import queue
import threading
import time
import requests

# Set up logging to syslog
logger = logging.getLogger('SMSForwarder')
logger.setLevel(logging.INFO)  # Default level, can be overridden by config 'debug'

# Syslog handler for Debian (/dev/log is the default syslog socket)
syslog_handler = logging.handlers.SysLogHandler(address='/dev/log', facility=logging.handlers.SysLogHandler.LOG_USER)
formatter = logging.Formatter('%(name)s: %(levelname)s %(message)s')
syslog_handler.setFormatter(formatter)
logger.addHandler(syslog_handler)

# Dictionary to store multi-part messages: {sender_number: {ref_num: {'parts': [(part_num, text)], 'total_parts': int, 'timestamp': timestamp}}}
multipart_messages = defaultdict(lambda: defaultdict(dict))
CONFIG_FILE = "config.json"
SAMPLE_CONFIG_FILE = "config.json.sample"

# Queues for forwarding services
api_queue = queue.Queue()
sms_queue = queue.Queue()
email_queue = queue.Queue()

# Track failed services to prevent notification loops
failed_services = set()

def load_config():
    """Load modem config, SMS recipients, email config, API providers, and database config from config.json"""
    if not os.path.exists(CONFIG_FILE):
        if not os.path.exists(SAMPLE_CONFIG_FILE):
            raise FileNotFoundError(f"Neither {CONFIG_FILE} nor {SAMPLE_CONFIG_FILE} found. Please create a config file.")
        with open(SAMPLE_CONFIG_FILE, 'r') as f:
            default_config = json.load(f)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(default_config, f, indent=4)
        logger.info(f"Created default config file {CONFIG_FILE} from {SAMPLE_CONFIG_FILE}")
        return default_config
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        required_keys = ["modem", "sms_recipients", "email", "api_providers", "database", "default_timeout"]
        if not all(key in config for key in required_keys):
            raise ValueError(f"Invalid config format: missing one of {required_keys}")
        # Set logging level based on debug flag
        if config.get("debug", False):
            logger.setLevel(logging.DEBUG)
        logger.info("Config loaded successfully")
        return config
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Error loading config: {e}. Using default config from {SAMPLE_CONFIG_FILE}")
        with open(SAMPLE_CONFIG_FILE, 'r') as f:
            default_config = json.load(f)
        return default_config

def init_database(db_file):
    """Initialize SQLite database and create SMS table"""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sms_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            reference INTEGER,
            total_parts INTEGER,
            message_text TEXT NOT NULL,
            api_forwarded INTEGER DEFAULT 0,
            sms_forwarded INTEGER DEFAULT 0,
            email_forwarded INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {db_file}")

def save_or_update_sms(sender, timestamp, text, db_file, reference=None, total_parts=None, part_num=None):
    """Save new SMS or update existing multi-part SMS in the database"""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    
    if reference is None:
        cursor.execute('INSERT INTO sms_messages (sender, timestamp, message_text) VALUES (?, ?, ?)',
                       (sender, timestamp.isoformat(), text))
        conn.commit()
        sms_id = cursor.lastrowid
        logger.info(f"Saved new SMS: ID={sms_id}, Sender={sender}, Text={text}")
    else:
        cursor.execute('SELECT id, message_text FROM sms_messages WHERE sender = ? AND reference = ?',
                       (sender, reference))
        result = cursor.fetchone()
        
        if result is None:
            cursor.execute('INSERT INTO sms_messages (sender, timestamp, reference, total_parts, message_text) VALUES (?, ?, ?, ?, ?)',
                           (sender, timestamp.isoformat(), reference, total_parts, text))
            conn.commit()
            sms_id = cursor.lastrowid
            logger.info(f"Saved new multipart SMS: ID={sms_id}, Sender={sender}, Reference={reference}, Part={part_num}/{total_parts}")
        else:
            sms_id, existing_text = result
            updated_text = existing_text + text if part_num > 1 else text + existing_text
            cursor.execute('UPDATE sms_messages SET message_text = ? WHERE id = ?', (updated_text, sms_id))
            conn.commit()
            logger.info(f"Updated multipart SMS: ID={sms_id}, Sender={sender}, Reference={reference}, Part={part_num}/{total_parts}")
    
    conn.close()
    return sms_id

def mark_as_forwarded(db_file, sms_id, api_forwarded=False, sms_forwarded=False, email_forwarded=False):
    """Update forwarding status in the database"""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    if api_forwarded:
        cursor.execute('UPDATE sms_messages SET api_forwarded = 1 WHERE id = ?', (sms_id,))
        logger.info(f"Marked API forwarded for SMS ID={sms_id}")
    if sms_forwarded:
        cursor.execute('UPDATE sms_messages SET sms_forwarded = 1 WHERE id = ?', (sms_id,))
        logger.info(f"Marked SMS forwarded for SMS ID={sms_id}")
    if email_forwarded:
        cursor.execute('UPDATE sms_messages SET email_forwarded = 1 WHERE id = ?', (sms_id,))
        logger.info(f"Marked Email forwarded for SMS ID={sms_id}")
    conn.commit()
    conn.close()

def notify_failure(service_name, sms_id):
    """Queue failure notification using existing service queues"""
    config = load_config()
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    message = f"Service {service_name} failed after max retries for SMS ID: {sms_id} at {timestamp}"
    
    logger.info(f"Preparing failure notification for {service_name}, SMS ID={sms_id}, Failed services={failed_services}")
    
    if "SMS" not in failed_services and config["sms_recipients"]:
        sms_queue.put(("System", timestamp, message, None, 0))
        logger.info(f"Queued SMS failure notification: {message}")
    
    if "Email" not in failed_services and config["email"].get("recipients"):
        email_queue.put(("System", timestamp, message, None, 0))
        logger.info(f"Queued email failure notification: {message}")
    
    if "API" not in failed_services and config["api_providers"]:
        api_queue.put(("System", timestamp, message, None, 0, None))
        logger.info(f"Queued API failure notification: {message}")

def send_to_api_providers(api_providers, sender, timestamp, message, provider_name=None):
    """Send requests to a specific API provider by name, or all default providers if not specified"""
    success = False
    config = load_config()
    default_timeout = config.get("default_timeout", 10)
    
    if provider_name:
        selected_providers = [p for p in api_providers if p["name"] == provider_name]
        if not selected_providers:
            logger.warning(f"No API provider found with name: {provider_name}")
            return False
    else:
        selected_providers = [p for p in api_providers if p.get("default", False)]
        if not selected_providers:
            logger.warning("No default API providers configured")
            return False
    
    for provider in selected_providers:
        try:
            method = provider.get("method", "POST").upper()
            endpoint = provider["endpoint"].format(sender=sender, timestamp=timestamp, message=message)
            headers = {k: v.format(sender=sender, timestamp=timestamp, message=message) 
                      for k, v in provider.get("headers", {}).items()}
            
            raw_payload = provider.get("payload", {})
            payload = {}
            for k, v in raw_payload.items():
                if isinstance(v, str):
                    payload[k] = v.format(sender=sender, timestamp=timestamp, message=message)
                else:
                    payload[k] = v
            
            timeout = provider.get("timeout", default_timeout)

            logger.debug(f"Sending to {provider['name']} API: Method={method}, Endpoint={endpoint}, Headers={headers}, Payload={payload}, Timeout={timeout}s")

            if method == "POST":
                response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
            elif method == "GET":
                if payload:
                    response = requests.get(endpoint, headers=headers, params=payload, timeout=timeout)
                else:
                    response = requests.get(endpoint, headers=headers, timeout=timeout)
            elif method == "PUT":
                response = requests.put(endpoint, headers=headers, json=payload, timeout=timeout)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            logger.info(f"Successfully sent to {provider['name']} API")
            success = True
        except Exception as e:
            logger.error(f"Failed to send to {provider['name']} API: {e}")
    return success

def api_forward_worker(api_providers, db_file):
    """Worker thread for API forwarding"""
    config = load_config()
    global_max_retries = config.get("max_retries", 3)
    
    while True:
        task = api_queue.get()
        sender, timestamp, message, sms_id, retry_count, provider = task
        logger.info(f"API worker processing task: Sender={sender}, SMS ID={sms_id}, Retry={retry_count}, Provider={provider}, Message={message}")
        logger.info(f"Started forwarding SMS ID={sms_id} to API")
        
        if not api_providers:
            logger.warning("API forwarding skipped: No providers configured")
            api_queue.task_done()
            continue
        
        success = send_to_api_providers(api_providers, sender, timestamp, message, provider_name=provider)
        
        if success:
            if sms_id:
                mark_as_forwarded(db_file, sms_id, api_forwarded=True)
            if "API" in failed_services:
                failed_services.remove("API")
                logger.info("API service restored")
            logger.info(f"API forwarding succeeded for SMS ID={sms_id}")
            logger.info(f"SMS ID={sms_id} forwarded to API successfully")
        else:
            retry_count += 1
            max_retries = max(provider.get("max_retries", global_max_retries) 
                            for provider in api_providers)
            logger.warning(f"API forwarding failed: SMS ID={sms_id}, Attempt={retry_count}/{max_retries}")
            
            if retry_count < max_retries:
                logger.info(f"Retrying API forwarding (attempt {retry_count + 1}/{max_retries})")
                time.sleep(5 * retry_count)
                api_queue.put((sender, timestamp, message, sms_id, retry_count, provider))
            else:
                logger.error(f"Max retries ({max_retries}) reached for API forwarding (ID: {sms_id})")
                if "API" not in failed_services:
                    failed_services.add("API")
                    if sms_id:
                        notify_failure("API", sms_id)
                logger.info(f"API failed services updated: {failed_services}")
                logger.error(f"SMS ID={sms_id} failed to forward to API after {max_retries} retries")
        
        api_queue.task_done()

def sms_forward_worker(modem, db_file, recipients):
    """Worker thread for SMS forwarding"""
    config = load_config()
    global_max_retries = config.get("max_retries", 3)
    max_retries = config.get("sms_max_retries", global_max_retries)
    
    while True:
        task = sms_queue.get()
        sender, timestamp, message, sms_id, retry_count = task
        logger.info(f"SMS worker processing task: Sender={sender}, SMS ID={sms_id}, Retry={retry_count}, Message={message}")
        logger.info(f"Started forwarding SMS ID={sms_id} to SMS recipients")
        
        if not recipients:
            logger.warning("SMS forwarding skipped: No recipients configured")
            sms_queue.task_done()
            continue
        
        success = False
        for recipient in recipients:
            try:
                logger.debug(f"Attempting SMS forward to {recipient}")
                modem.sendSms(recipient, f"From: {sender}\nTime: {timestamp}\nMessage: {message}")
                logger.info(f"Successfully forwarded SMS to {recipient}")
                success = True
            except Exception as e:
                logger.error(f"Failed to forward SMS to {recipient}: {e}")
        
        if success:
            if sms_id:
                mark_as_forwarded(db_file, sms_id, sms_forwarded=True)
            if "SMS" in failed_services:
                failed_services.remove("SMS")
                logger.info("SMS service restored")
            logger.info(f"SMS forwarding succeeded for SMS ID={sms_id}")
            logger.info(f"SMS ID={sms_id} forwarded to SMS recipients successfully")
        else:
            retry_count += 1
            logger.warning(f"SMS forwarding failed: SMS ID={sms_id}, Attempt={retry_count}/{max_retries}")
            
            if retry_count < max_retries:
                logger.info(f"Retrying SMS forwarding (attempt {retry_count + 1}/{max_retries})")
                time.sleep(5 * retry_count)
                sms_queue.put((sender, timestamp, message, sms_id, retry_count))
            else:
                logger.error(f"Max retries ({max_retries}) reached for SMS forwarding (ID: {sms_id})")
                if "SMS" not in failed_services:
                    failed_services.add("SMS")
                    if sms_id:
                        notify_failure("SMS", sms_id)
                logger.info(f"SMS failed services updated: {failed_services}")
                logger.error(f"SMS ID={sms_id} failed to forward to SMS recipients after {max_retries} retries")
        
        sms_queue.task_done()

def email_forward_worker(email_config, db_file):
    """Worker thread for email forwarding"""
    config = load_config()
    global_max_retries = config.get("max_retries", 3)
    max_retries = email_config.get("max_retries", global_max_retries)
    
    while True:
        task = email_queue.get()
        sender, timestamp, message, sms_id, retry_count = task
        logger.info(f"Email worker processing task: Sender={sender}, SMS ID={sms_id}, Retry={retry_count}, Message={message}")
        logger.info(f"Started forwarding SMS ID={sms_id} to email recipients")
        
        if not email_config.get("recipients"):
            logger.warning("Email forwarding skipped: No recipients configured")
            email_queue.task_done()
            continue
        
        smtp_server = email_config["smtp_server"]
        smtp_port = email_config["smtp_port"]
        smtp_user = email_config["smtp_user"]
        smtp_password = email_config["smtp_password"]
        sender_email = email_config.get("sender", smtp_user)
        recipients = email_config["recipients"]
        
        subject = f"SMS from {sender} at {timestamp}" if sms_id else f"System Notification"
        body = f"From: {sender}\nTime: {timestamp}\nMessage: {message}"
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = sender_email
        msg['To'] = ", ".join(recipients)
        
        try:
            logger.debug(f"Attempting email forward to {recipients}")
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
            logger.info(f"Successfully forwarded to email recipients: {', '.join(recipients)}")
            if sms_id:
                mark_as_forwarded(db_file, sms_id, email_forwarded=True)
            if "Email" in failed_services:
                failed_services.remove("Email")
                logger.info("Email service restored")
            logger.info(f"Email forwarding succeeded for SMS ID={sms_id}")
            logger.info(f"SMS ID={sms_id} forwarded to email recipients successfully")
        except Exception as e:
            retry_count += 1
            logger.warning(f"Email forwarding failed: SMS ID={sms_id}, Attempt={retry_count}/{max_retries}, Error={e}")
            
            if retry_count < max_retries:
                logger.info(f"Retrying email forwarding (attempt {retry_count + 1}/{max_retries})")
                time.sleep(5 * retry_count)
                email_queue.put((sender, timestamp, message, sms_id, retry_count))
            else:
                logger.error(f"Max retries ({max_retries}) reached for email forwarding (ID: {sms_id})")
                if "Email" not in failed_services:
                    failed_services.add("Email")
                    if sms_id:
                        notify_failure("Email", sms_id)
                logger.info(f"Email failed services updated: {failed_services}")
                logger.error(f"SMS ID={sms_id} failed to forward to email recipients after {max_retries} retries")
        
        email_queue.task_done()

def handleSms(sms, provider=None):
    """Handle incoming SMS, save to database, and queue for forwarding with optional API provider"""
    sender = sms.number
    timestamp = sms.time
    text = sms.text
    config = load_config()
    api_providers = config["api_providers"]
    sms_recipients = config["sms_recipients"]
    email_config = config["email"]
    db_file = config["database"]["file"]
    
    if hasattr(sms, 'udh') and sms.udh:
        logger.debug(f"UDH contents: {sms.udh}")
        for udh_element in sms.udh:
            logger.debug(f"UDH element: {udh_element.__dict__}")
            if isinstance(udh_element, Concatenation):
                ref_num = udh_element.reference
                total_parts = udh_element.parts
                part_num = udh_element.number
                
                sms_id = save_or_update_sms(sender, timestamp, text, db_file, ref_num, total_parts, part_num)
                
                message_data = multipart_messages[sender][ref_num]
                if 'parts' not in message_data:
                    message_data['parts'] = []
                    message_data['total_parts'] = total_parts
                    message_data['timestamp'] = timestamp
                
                message_data['parts'].append((part_num, text))
                
                logger.info(f"Received part {part_num}/{total_parts} from {sender} (Reference: {ref_num})")
                
                if part_num == 1:
                    api_queue.put((sender, timestamp, text, sms_id, 0, provider))
                    logger.info(f"Queued API for first part: SMS ID={sms_id}, Provider={provider}")
                
                if len(message_data['parts']) == total_parts:
                    message_data['parts'].sort(key=lambda x: x[0])
                    complete_message = ''.join(part[1] for part in message_data['parts'])
                    
                    logger.info(f"== SMS message received (complete) ==\nFrom: {sender}\nTime: {timestamp}\nMessage:\n{complete_message}")
                    
                    sms_queue.put((sender, timestamp, complete_message, sms_id, 0))
                    email_queue.put((sender, timestamp, complete_message, sms_id, 0))
                    api_queue.put((sender, timestamp, complete_message, sms_id, 0, provider))
                    logger.info(f"Queued SMS, Email, and API for complete message: SMS ID={sms_id}, Provider={provider}")
                    
                    del multipart_messages[sender][ref_num]
                    if not multipart_messages[sender]:
                        del multipart_messages[sender]
                else:
                    logger.info(f"Waiting for {total_parts - len(message_data['parts'])} more parts "
                                f"for message from {sender} (Reference: {ref_num})")
                return
    
    sms_id = save_or_update_sms(sender, timestamp, text, db_file)
    logger.info(f"== SMS message received ==\nFrom: {sender}\nTime: {timestamp}\nMessage:\n{text}")
    
    api_queue.put((sender, timestamp, text, sms_id, 0, provider))
    sms_queue.put((sender, timestamp, text, sms_id, 0))
    email_queue.put((sender, timestamp, text, sms_id, 0))
    logger.info(f"Queued all services for SMS ID={sms_id}, API Provider={provider}")

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
    logger.info("Started forwarding threads: API-Forwarder, SMS-Forwarder, Email-Forwarder")
    
    logger.info("Waiting for SMS message...")
    try:
        modem.rxThread.join(2**31)
    finally:
        modem.close()
        logger.info("Modem closed")

if __name__ == '__main__':
    main()