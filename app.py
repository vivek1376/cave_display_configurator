import threading
import time
from flask import Flask, render_template, jsonify, request
from samsung_mdc import MDC

app = Flask(__name__)
SERIAL_PORT = '/dev/ttyUSB0'

# Cache for the 2x20 wall. IDs are 1-40.
wall_state = {i: {'status': 'offline', 'power': 'Off', 'source': 'Unknown'} for i in range(1, 41)}
state_lock = threading.Lock()

def poll_displays():
    """Background loop to update display cache to avoid blocking the UI."""
    while True:
        try:
            with MDC(SERIAL_PORT) as mdc:
                for display_id in range(1, 41):
                    try:
                        # Fetch power state and input source
                        power_resp = mdc.power(display_id)
                        source_resp = mdc.input_source(display_id)
                        
                        with state_lock:
                            wall_state[display_id]['status'] = 'online'
                            wall_state[display_id]['power'] = power_resp.name if power_resp else 'On'
                            wall_state[display_id]['source'] = source_resp.name if source_resp else 'HDMI1'
                    except Exception:
                        with state_lock:
                            wall_state[display_id]['status'] = 'offline'
        except Exception as e:
            print(f"Serial Bus Error: {e}")
        time.sleep(2) # Brief pause before next full sweep

# Start the polling thread
threading.Thread(target=poll_displays, daemon=True).start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status', methods=['GET'])
def get_status():
    with state_lock:
        return jsonify(wall_state)

@app.route('/api/command', methods=['POST'])
def send_command():
    data = request.json
    display_id = data.get('id')
    command = data.get('command')
    value = data.get('value')
    
    try:
        with MDC(SERIAL_PORT) as mdc:
            if command == 'power':
                # Map string 'on'/'off' to library enums/booleans based on exact samsung-mdc version
                mdc.power(display_id, value) 
            elif command == 'source':
                mdc.input_source(display_id, value)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
