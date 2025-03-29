#!/usr/bin/env python3

"""
Handle incoming SMS messages with queuing and threading for forwarding

Listens for incoming SMS messages, saves them to SQLite, and forwards to SMS/email recipients
using separate queues and threads for each service, defined in config.json.
"""

from __future__ import print_function
import logging
import json
import os
from gsmmodem.modem import GsmModem
from gsmmodem.pdu import Concatenation
from collections import defaultdict
import smtplib
from email.mime.text import MIMEText
import sqlite3
import queue
import threading
import time

# Dictionary to store multi-part messages: {sender_number: {ref_num: {'parts': [(part_num, text)], 'total_parts': int, 'timestamp': timestamp}}}
multipart_messages = defaultdict(lambda: defaultdict(dict))
CONFIG_FILE = "config.json"

# Queues for forwarding services
sms_queue = queue.Queue()
email_queue = queue.Queue()

def load_config():
    """Load modem config, SMS recipients, email config, and database config from config.json"""
    default_config = {
        "modem": {
            "port": "/dev/ttyUSB0",
            "baudrate": 115200,
            "pin": None
        },
        "sms_recipients": [
            "+1234567890",
            "+0987654321"
        ],
        "email": {
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
            "smtp_user": "your_email@gmail.com",
            "smtp_password": "your_password",
            "sender": "custom_sender@example.com",
            "recipients": [
                "recipient1@example.com",
                "recipient2@example.com"
            ]
        },
        "database": {
            "file": "sms_database.db"
        }
    }
    
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(default_config, f, indent=4)
        print(f"Created default config file: {CONFIG_FILE}")
        return default_config
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        required_keys = ["modem", "sms_recipients", "email", "database"]
        if not all(key in config for key in required_keys):
            raise ValueError(f"Invalid config format: missing one of {required_keys}")
        return config
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Error loading config: {e}. Using default config.")
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
            sms_forwarded INTEGER DEFAULT 0,
            email_forwarded INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def save_or_update_sms(sender, timestamp, text, db_file, reference=None, total_parts=None, part_num=None):
    """Save new SMS or update existing multi-part SMS in the database"""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    
    if reference is None:
        cursor.execute('INSERT INTO sms_messages (sender, timestamp, message_text) VALUES (?, ?, ?)',
                       (sender, timestamp.isoformat(), text))
        conn.commit()
        sms_id = cursor.lastrowid
    else:
        cursor.execute('SELECT id, message_text FROM sms_messages WHERE sender = ? AND reference = ?',
                       (sender, reference))
        result = cursor.fetchone()
        
        if result is None:
            cursor.execute('INSERT INTO sms_messages (sender, timestamp, reference, total_parts, message_text) VALUES (?, ?, ?, ?, ?)',
                           (sender, timestamp.isoformat(), reference, total_parts, text))
            conn.commit()
            sms_id = cursor.lastrowid
        else:
            sms_id, existing_text = result
            updated_text = existing_text + text if part_num > 1 else text + existing_text
            cursor.execute('UPDATE sms_messages SET message_text = ? WHERE id = ?', (updated_text, sms_id))
            conn.commit()
    
    conn.close()
    return sms_id

def mark_as_forwarded(db_file, sms_id, sms_forwarded=False, email_forwarded=False):
    """Update forwarding status in the database"""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    if sms_forwarded:
        cursor.execute('UPDATE sms_messages SET sms_forwarded = 1 WHERE id = ?', (sms_id,))
    if email_forwarded:
        cursor.execute('UPDATE sms_messages SET email_forwarded = 1 WHERE id = ?', (sms_id,))
    conn.commit()
    conn.close()

def sms_forward_worker(modem, db_file, recipients):
    """Worker thread for SMS forwarding"""
    while True:
        task = sms_queue.get()
        sender, timestamp, message, sms_id, retry_count = task
        if not recipients:
            logging.info("SMS forwarding skipped: No recipients configured")
            sms_queue.task_done()
            continue
        
        success = False
        for recipient in recipients:
            try:
                logging.info(f"Forwarding SMS to {recipient}")
                modem.sendSms(recipient, f"From: {sender}\nTime: {timestamp}\nMessage: {message}")
                logging.info(f"Successfully forwarded SMS to {recipient}")
                success = True
            except Exception as e:
                logging.error(f"Failed to forward SMS to {recipient}: {e}")
        
        if success:
            mark_as_forwarded(db_file, sms_id, sms_forwarded=True)
        else:
            retry_count += 1
            if retry_count < 3:  # Max 3 retries
                logging.info(f"Retrying SMS forwarding (attempt {retry_count + 1})")
                time.sleep(5 * retry_count)  # Exponential backoff: 5s, 10s, 15s
                sms_queue.put((sender, timestamp, message, sms_id, retry_count))
            else:
                logging.error(f"Max retries reached for SMS forwarding (ID: {sms_id})")
        
        sms_queue.task_done()

def email_forward_worker(email_config, db_file):
    """Worker thread for email forwarding"""
    while True:
        task = email_queue.get()
        sender, timestamp, message, sms_id, retry_count = task
        if not email_config.get("recipients"):
            logging.info("Email forwarding skipped: No recipients configured")
            email_queue.task_done()
            continue
        
        smtp_server = email_config["smtp_server"]
        smtp_port = email_config["smtp_port"]
        smtp_user = email_config["smtp_user"]
        smtp_password = email_config["smtp_password"]
        sender_email = email_config.get("sender", smtp_user)
        recipients = email_config["recipients"]
        
        subject = f"SMS from {sender} at {timestamp}"
        body = f"From: {sender}\nTime: {timestamp}\nMessage: {message}"
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = sender_email
        msg['To'] = ", ".join(recipients)
        
        try:
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
            logging.info(f"Successfully forwarded to email recipients: {', '.join(recipients)}")
            mark_as_forwarded(db_file, sms_id, email_forwarded=True)
        except Exception as e:
            retry_count += 1
            if retry_count < 3:  # Max 3 retries
                logging.error(f"Failed to forward to email: {e}. Retrying (attempt {retry_count + 1})")
                time.sleep(5 * retry_count)  # Exponential backoff
                email_queue.put((sender, timestamp, message, sms_id, retry_count))
            else:
                logging.error(f"Max retries reached for email forwarding (ID: {sms_id})")
        
        email_queue.task_done()

def handleSms(sms):
    """Handle incoming SMS, save to database, and queue for forwarding"""
    sender = sms.number
    timestamp = sms.time
    text = sms.text
    config = load_config()
    sms_recipients = config["sms_recipients"]
    email_config = config["email"]
    db_file = config["database"]["file"]
    
    if hasattr(sms, 'udh') and sms.udh:
        print(f"UDH contents: {sms.udh}")
        for udh_element in sms.udh:
            print(f"UDH element: {udh_element.__dict__}")
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
                
                print(f"Received part {part_num}/{total_parts} from {sender} (Reference: {ref_num})")
                
                if len(message_data['parts']) == total_parts:
                    message_data['parts'].sort(key=lambda x: x[0])
                    complete_message = ''.join(part[1] for part in message_data['parts'])
                    
                    print(u'== SMS message received (complete) ==\nFrom: {0}\nTime: {1}\nMessage:\n{2}\n'.format(sender, timestamp, complete_message))
                    
                    # Queue forwarding tasks
                    sms_queue.put((sender, timestamp, complete_message, sms_id, 0))
                    email_queue.put((sender, timestamp, complete_message, sms_id, 0))
                    
                    del multipart_messages[sender][ref_num]
                    if not multipart_messages[sender]:
                        del multipart_messages[sender]
                else:
                    print(f"Waiting for {total_parts - len(message_data['parts'])} more parts "
                          f"for message from {sender} (Reference: {ref_num})")
                return
    
    sms_id = save_or_update_sms(sender, timestamp, text, db_file)
    print(u'== SMS message received ==\nFrom: {0}\nTime: {1}\nMessage:\n{2}\n'.format(sender, timestamp, text))
    sms_queue.put((sender, timestamp, text, sms_id, 0))
    email_queue.put((sender, timestamp, text, sms_id, 0))

def main():
    config = load_config()
    modem_config = config["modem"]
    db_file = config["database"]["file"]
    sms_recipients = config["sms_recipients"]
    email_config = config["email"]
    
    init_database(db_file)
    logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)
    
    # Initialize modem first
    print('Initializing modem...')
    modem = GsmModem(modem_config["port"], modem_config["baudrate"], smsReceivedCallbackFunc=handleSms)
    modem.smsTextMode = False
    modem.connect(modem_config["pin"])
    modem.waitForNetworkCoverage(10)
    
    # Start forwarding workers with initialized modem
    sms_thread = threading.Thread(target=sms_forward_worker, args=(modem, db_file, sms_recipients), daemon=True)
    email_thread = threading.Thread(target=email_forward_worker, args=(email_config, db_file), daemon=True)
    sms_thread.start()
    email_thread.start()
    
    print('Waiting for SMS message...')
    try:
        modem.rxThread.join(2**31)
    finally:
        modem.close()

if __name__ == '__main__':
    main()
