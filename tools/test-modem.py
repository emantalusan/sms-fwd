import serial
import time
import sys

def connect_to_modem(port):
    try:
        # Initialize serial connection
        modem = serial.Serial(
            port=port,
            baudrate=115200,  # Common baud rate for USB modems
            timeout=5
        )
        return modem
    except serial.SerialException as e:
        print(f"Error connecting to modem: {e}")
        sys.exit(1)

def send_at_command(modem, command, delay=1):
    try:
        # Ensure command ends with carriage return and newline
        modem.write(f"{command}\r\n".encode())
        time.sleep(delay)  # Wait for modem response
        response = modem.read(modem.in_waiting or 1000).decode(errors='ignore')
        return response
    except Exception as e:
        return f"Error sending command: {e}"

def get_modem_info(modem):
    # Dictionary to store modem information
    info = {}
    
    # Basic AT command to test connection
    response = send_at_command(modem, "AT")
    if "OK" not in response:
        return {"Error": "Modem not responding"}

    # Get manufacturer
    info["Manufacturer"] = send_at_command(modem, "AT+CGMI")
    
    # Get model
    info["Model"] = send_at_command(modem, "AT+CGMM")
    
    # Get firmware version
    info["Firmware"] = send_at_command(modem, "AT+CGMR")
    
    # Get IMEI
    info["IMEI"] = send_at_command(modem, "AT+CGSN")
    
    # Get SIM status
    info["SIM Status"] = send_at_command(modem, "AT+CPIN?")
    
    # Get IMSI (International Mobile Subscriber Identity)
    info["IMSI"] = send_at_command(modem, "AT+CIMI")
    
    # Get signal quality
    info["Signal Quality"] = send_at_command(modem, "AT+CSQ")
    
    # Get network registration status
    info["Network Registration"] = send_at_command(modem, "AT+CREG?")
    
    # Get operator name
    info["Operator"] = send_at_command(modem, "AT+COPS?")
    
    return info

def display_info(info):
    print("\nModem and SIM Information:")
    print("-" * 50)
    for key, value in info.items():
        # Clean up the response by splitting and taking relevant parts
        if "Error" in value:
            print(f"{key}: {value}")
        else:
            # Remove command echo and extra whitespace
            lines = value.strip().split('\n')
            clean_value = next((line.strip() for line in lines if line.strip() and not line.startswith('AT')), "N/A")
            print(f"{key}: {clean_value}")

def main():
    # Check if port is provided as command line argument
    if len(sys.argv) != 2:
        print("Usage: python script.py <port>")
        print("Example: python script.py COM3  or  python script.py /dev/ttyUSB0")
        sys.exit(1)
    
    port = sys.argv[1]
    
    # Connect to modem
    print(f"Connecting to modem on {port}...")
    modem = connect_to_modem(port)
    
    # Get and display information
    modem_info = get_modem_info(modem)
    display_info(modem_info)
    
    # Close the connection
    modem.close()
    print("\nConnection closed.")

if __name__ == "__main__":
    main()