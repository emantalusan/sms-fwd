import logging
import time
from gsmmodem.pdu import Concatenation
from .main import multipart_messages, sms_queue, email_queue, api_queue, load_config
from .db import save_or_update_sms

logger = logging.getLogger('SMSForwarder')

def sms_forward_worker(modem, db_file, recipients):
    """Worker thread for SMS forwarding"""
    config = load_config()
    max_retries = config.get("sms_max_retries", config.get("max_retries", 3))
    
    while True:
        sender, timestamp, message, sms_id, retry_count = sms_queue.get()
        success = False
        for recipient in recipients:
            try:
                modem.sendSms(recipient, f"From: {sender}\nTime: {timestamp}\nMessage: {message}")
                success = True
            except Exception as e:
                logger.error(f"Failed to forward SMS to {recipient}: {e}")
        
        from .db import mark_as_forwarded
        from .main import failed_services, notify_failure
        
        if success:
            if sms_id:
                mark_as_forwarded(db_file, sms_id, sms_forwarded=True)
            if "SMS" in failed_services:
                failed_services.remove("SMS")
        else:
            retry_count += 1
            if retry_count < max_retries:
                time.sleep(5 * retry_count)
                sms_queue.put((sender, timestamp, message, sms_id, retry_count))
            else:
                if "SMS" not in failed_services:
                    failed_services.add("SMS")
                    if sms_id:
                        notify_failure("SMS", sms_id)
        
        sms_queue.task_done()

def handleSms(sms, provider=None):
    """Handle incoming SMS"""
    sender = sms.number
    timestamp = sms.time
    text = sms.text
    config = load_config()
    db_file = config["database"]["file"]
    
    if hasattr(sms, 'udh') and sms.udh:
        for udh_element in sms.udh:
            if isinstance(udh_element, Concatenation):
                ref_num = udh_element.reference
                total_parts = udh_element.parts
                part_num = udh_element.number
                
                sms_id = save_or_update_sms(sender, timestamp, text, db_file, ref_num, total_parts, part_num)
                
                message_data = multipart_messages[sender][ref_num]
                if 'parts' not in message_data:
                    message_data.update({'parts': [], 'total_parts': total_parts, 'timestamp': timestamp})
                
                message_data['parts'].append((part_num, text))
                
                if part_num == 1:
                    api_queue.put((sender, timestamp, text, sms_id, 0, provider))
                
                if len(message_data['parts']) == total_parts:
                    complete_message = ''.join(part[1] for part in sorted(message_data['parts']))
                    sms_queue.put((sender, timestamp, complete_message, sms_id, 0))
                    email_queue.put((sender, timestamp, complete_message, sms_id, 0))
                    api_queue.put((sender, timestamp, complete_message, sms_id, 0, provider))
                    del multipart_messages[sender][ref_num]
                    if not multipart_messages[sender]:
                        del multipart_messages[sender]
                return
    
    sms_id = save_or_update_sms(sender, timestamp, text, db_file)
    api_queue.put((sender, timestamp, text, sms_id, 0, provider))
    sms_queue.put((sender, timestamp, text, sms_id, 0))
    email_queue.put((sender, timestamp, text, sms_id, 0))