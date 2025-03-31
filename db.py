import sqlite3
import logging

logger = logging.getLogger('SMSForwarder')

def init_database(db_file):
    """Initialize SQLite database"""
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
    """Save or update SMS in database"""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    
    if reference is None:
        cursor.execute('INSERT INTO sms_messages (sender, timestamp, message_text) VALUES (?, ?, ?)',
                       (sender, timestamp.isoformat(), text))
        conn.commit()
        sms_id = cursor.lastrowid
        logger.info(f"Saved new SMS: ID={sms_id}")
    else:
        cursor.execute('SELECT id, message_text FROM sms_messages WHERE sender = ? AND reference = ?',
                       (sender, reference))
        result = cursor.fetchone()
        
        if result is None:
            cursor.execute('INSERT INTO sms_messages (sender, timestamp, reference, total_parts, message_text) VALUES (?, ?, ?, ?, ?)',
                           (sender, timestamp.isoformat(), reference, total_parts, text))
            conn.commit()
            sms_id = cursor.lastrowid
            logger.info(f"Saved new multipart SMS: ID={sms_id}")
        else:
            sms_id, existing_text = result
            updated_text = existing_text + text if part_num > 1 else text + existing_text
            cursor.execute('UPDATE sms_messages SET message_text = ? WHERE id = ?', (updated_text, sms_id))
            conn.commit()
            logger.info(f"Updated multipart SMS: ID={sms_id}")
    
    conn.close()
    return sms_id

def mark_as_forwarded(db_file, sms_id, api_forwarded=False, sms_forwarded=False, email_forwarded=False):
    """Update forwarding status"""
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