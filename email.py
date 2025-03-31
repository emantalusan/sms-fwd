import smtplib
from email.mime.text import MIMEText
import logging
import time
from db import mark_as_forwarded

logger = logging.getLogger('SMSForwarder')

def email_forward_worker(email_config, db_file, email_queue, failed_services, notify_failure, load_config):
    """Worker thread for email forwarding"""
    config = load_config()
    max_retries = email_config.get("max_retries", config.get("max_retries", 3))
    
    while True:
        sender, timestamp, message, sms_id, retry_count = email_queue.get()
        
        if not email_config.get("recipients"):
            email_queue.task_done()
            continue
        
        smtp_server = email_config["smtp_server"]
        smtp_port = email_config["smtp_port"]
        smtp_user = email_config["smtp_user"]
        smtp_password = email_config["smtp_password"]
        sender_email = email_config.get("sender", smtp_user)
        recipients = email_config["recipients"]
        
        subject = f"SMS from {sender} at {timestamp}" if sms_id else "System Notification"
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
            if sms_id:
                mark_as_forwarded(db_file, sms_id, email_forwarded=True)
            if "Email" in failed_services:
                failed_services.remove("Email")
        except Exception as e:
            retry_count += 1
            if retry_count < max_retries:
                time.sleep(5 * retry_count)
                email_queue.put((sender, timestamp, message, sms_id, retry_count))
            else:
                if "Email" not in failed_services:
                    failed_services.add("Email")
                    if_raise_failure("Email", sms_id)
        
        email_queue.task_done()