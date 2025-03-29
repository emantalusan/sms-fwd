import serial
import time
import sys

def initialize_modem(port, baud_rate=9600):
    """Initialize the GSM modem with PDU mode settings"""
    try:
        modem = serial.Serial(
            port=port,
            baudrate=baud_rate,
            timeout=1
        )
        print(f"Connected to {port} at {baud_rate} baud")

        modem.reset_input_buffer()
        for _ in range(3):  # Retry up to 3 times
            time.sleep(1)
            modem.write(b'AT\r')
            time.sleep(1)
            response = modem.read(modem.in_waiting).decode('utf-8', errors='ignore')
            if 'OK' in response:
                break
        else:
            raise Exception("Modem not responding with OK after retries")

        modem.write(b'AT+CMGF=0\r')  # Set to PDU mode
        time.sleep(1)
        response = modem.read(modem.in_waiting).decode('utf-8', errors='ignore')
        if 'OK' not in response:
            raise Exception("Failed to set PDU mode")

        print("Modem initialized successfully in PDU mode")
        return modem
    
    except Exception as e:
        print(f"Error initializing modem: {e}")
        return None

def read_full_response(modem, timeout=10):
    """Read all data until OK or timeout"""
    start_time = time.time()
    full_response = ""
    last_data_time = start_time
    while time.time() - start_time < timeout:
        if modem.in_waiting > 0:
            chunk = modem.read(modem.in_waiting).decode('utf-8', errors='ignore')
            full_response += chunk
            last_data_time = time.time()
        if 'OK' in full_response or 'ERROR' in full_response:
            break
        if time.time() - last_data_time > 5:
            break
        time.sleep(0.2)
    return full_response

def decode_gsm7(hex_data, num_chars):
    """Decode GSM 7-bit packed data into text"""
    bytes_data = bytes.fromhex(hex_data)
    bits = []
    for byte in bytes_data:
        byte_bits = bin(byte)[2:].zfill(8)
        bits.extend(byte_bits)
    septets = []
    for i in range(num_chars):
        start = i * 7
        if start + 7 <= len(bits):
            septet_bits = bits[start:start+7]
            septet = int(''.join(septet_bits), 2)
            char = chr(septet) if 32 <= septet <= 126 else '?'
            septets.append(char)
    return ''.join(septets)

def get_sender(pdu):
    """Extract sender number from PDU"""
    sender_len = int(pdu[18:20], 16)  # Length of sender number in digits
    sender_hex = pdu[20:20 + sender_len + 2]  # Include type of number
    if sender_hex.startswith('91'):  # International format
        sender = '+' + ''.join([sender_hex[i+2:i+4] for i in range(0, sender_len, 2)])
        # Swap pairs for correct order
        sender = '+' + ''.join(sender[i+1] + sender[i] for i in range(1, len(sender)-1, 2))
    else:
        sender = sender_hex[2:]  # Fallback, may need adjustment
    return sender

def read_all_messages(modem, storage):
    """Read and display all messages from specified storage (ME or SM)"""
    try:
        # Set storage location
        modem.write(f'AT+CPMS="{storage}"\r'.encode())
        time.sleep(1)
        response = read_full_response(modem)
        if 'OK' not in response:
            print(f"Failed to set storage to {storage}: {response}")
            return []

        # List all messages
        modem.write(b'AT+CMGL=4\r')  # 4 = all messages in PDU mode
        time.sleep(1)
        response = read_full_response(modem)
        if 'OK' not in response:
            print(f"Failed to list messages from {storage}: {response}")
            return []

        messages = []
        lines = response.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if '+CMGL:' in line and i + 1 < len(lines):
                index = line.split(',')[0].split(':')[1].strip()
                pdu = lines[i + 1].strip()
                udl = int(pdu[20:22], 16)
                ud_start = 22 + int(pdu[18:20], 16) * 2
                ud_data = pdu[ud_start:]
                sender = get_sender(pdu)
                message = decode_gsm7(ud_data, udl)
                
                messages.append((index, sender, message))
                print(f"\nStorage: {storage}, Index: {index}")
                print(f"Sender: {sender}")
                print(f"Message: {message}")
                i += 2
            else:
                i += 1
        return messages
    
    except Exception as e:
        print(f"Error reading messages from {storage}: {e}")
        return []

def delete_all_messages(modem, storage, indices):
    """Delete all messages from specified storage using individual indices"""
    try:
        # Set storage location
        modem.write(f'AT+CPMS="{storage}"\r'.encode())
        time.sleep(1)
        response = read_full_response(modem)
        if 'OK' not in response:
            print(f"Failed to set storage to {storage} for deletion: {response}")
            return False

        success = True
        for index in indices:
            modem.write(f'AT+CMGD={index}\r'.encode())
            time.sleep(1)
            response = read_full_response(modem)
            if 'OK' not in response:
                print(f"Failed to delete message at index {index} in {storage}: {response}")
                success = False
            else:
                print(f"Deleted message at index {index} in {storage}")
        if success:
            print(f"All messages deleted from {storage}")
        return success
    
    except Exception as e:
        print(f"Error deleting messages from {storage}: {e}")
        return False

def main():
    if len(sys.argv) != 2:
        print("Usage: python3 sms_reader_deleter.py <port>")
        print("Example: python3 sms_reader_deleter.py /dev/ttyUSB0")
        sys.exit(1)

    PORT = sys.argv[1]
    BAUD_RATE = 115200

    print("Starting SMS reader and deleter...")
    print(f"Using port: {PORT}")

    modem = initialize_modem(PORT, BAUD_RATE)
    if not modem:
        print("Modem initialization failed. Exiting.")
        sys.exit(1)

    try:
        # Read and display messages from ME (modem memory)
        print("\nReading messages from ME (modem memory):")
        me_messages = read_all_messages(modem, "ME")
        me_indices = [msg[0] for msg in me_messages]

        # Read and display messages from SM (SIM card)
        print("\nReading messages from SM (SIM card):")
        sm_messages = read_all_messages(modem, "SM")
        sm_indices = [msg[0] for msg in sm_messages]

        # Delete all messages from ME
        print("\nDeleting all messages from ME...")
        delete_all_messages(modem, "ME", me_indices)

        # Delete all messages from SM
        print("\nDeleting all messages from SM...")
        delete_all_messages(modem, "SM", sm_indices)

        print("\nOperation completed.")

    except Exception as e:
        print(f"Error in main process: {e}")
    finally:
        modem.close()
        print("Modem connection closed.")

if __name__ == "__main__":
    main()


