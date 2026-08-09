import threading
import time

import serial
from flask import Flask, render_template, jsonify, request


app = Flask(__name__)

SERIAL_PORT = "/dev/ttyUSB0"
BAUDRATE = 9600
DISPLAY_IDS = range(1, 41)

# ----------------------------------------------------------------------
# Display state
# ----------------------------------------------------------------------

wall_state = {
    display_id: {
        "status": "offline",
        "power": "Off",
        "source": "Unknown",
    }
    for display_id in DISPLAY_IDS
}

state_lock = threading.Lock()


# ----------------------------------------------------------------------
# Samsung MDC
# ----------------------------------------------------------------------

class SamsungMDCError(Exception):
    pass


class SamsungMDC:
    """
    Minimal Samsung MDC driver.

    Tested with Samsung LH55VCE:
        9600 baud
        8 data bits
        No parity
        1 stop bit
        No flow control
    """

    CMD_POWER = 0x11
    CMD_INPUT_SOURCE = 0x14
    CMD_ORIENTATION = 0xC8

    SUBCMD_SOURCE_ORIENTATION = 0x82

    SOURCE_CODES = {
        "HDMI1": 0x21,
        "HDMI2": 0x23,
        "DisplayPort": 0x25,
    }

    SOURCE_NAMES = {
        0x21: "HDMI1",
        0x22: "HDMI1_PC",
        0x23: "HDMI2",
        0x24: "HDMI2_PC",
        0x25: "DisplayPort",
        0x14: "PC",
        0x18: "DVI",
        0x08: "Component",
        0x20: "MagicInfo",
        0x1F: "DVI_video",
        0x30: "RF",
        0x40: "DTV",
    }

    ORIENTATION_CODES = {
        "landscape": 0x00,
        "portrait": 0x01,
        "landscape_180": 0x02,
        "portrait_90": 0x03,
    }

    def __init__(self, port=SERIAL_PORT):
        self.serial = serial.Serial(
            port=port,
            baudrate=BAUDRATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
            write_timeout=1.0,
        )

        # Protect the complete MDC request/response transaction.
        self.lock = threading.Lock()

    def close(self):
        if self.serial.is_open:
            self.serial.close()

    @staticmethod
    def checksum(packet):
        return sum(packet[1:]) & 0xFF

    def send(self, command, display_id, data=b""):
        """
        Send an MDC command and return the raw response.

        Request:
            AA COMMAND ID LENGTH DATA CHECKSUM

        Response:
            AA FF ID LENGTH STATUS ...
        """

        if not 0 <= display_id <= 255:
            raise ValueError("Display ID must be between 0 and 255")

        packet = bytearray([
            0xAA,
            command,
            display_id,
            len(data),
        ])

        packet.extend(data)
        packet.append(self.checksum(packet))

        with self.lock:
            self.serial.reset_input_buffer()

            self.serial.write(packet)
            self.serial.flush()

            response = bytearray()
            deadline = time.monotonic() + 1.0

            while time.monotonic() < deadline:
                chunk = self.serial.read(256)

                if chunk:
                    response.extend(chunk)

                    # Response length = 5 + LENGTH byte.
                    if len(response) >= 4:
                        expected_length = 5 + response[3]

                        if len(response) >= expected_length:
                            break

            if not response:
                raise SamsungMDCError(
                    f"No response from display {display_id}"
                )

            if len(response) < 5:
                raise SamsungMDCError(
                    f"Malformed response: {response.hex(' ')}"
                )

            if response[0] != 0xAA:
                raise SamsungMDCError(
                    f"Invalid MDC header: {response.hex(' ')}"
                )

            if response[1] != 0xFF:
                raise SamsungMDCError(
                    f"Invalid MDC response: {response.hex(' ')}"
                )

            if response[2] != display_id:
                raise SamsungMDCError(
                    f"Unexpected display ID {response[2]}, expected {display_id}"
                )

            return bytes(response)

    @staticmethod
    def raise_if_nak(response):
        """
        MDC status:
            0x41 = ACK
            0x4E = NAK
        """

        if len(response) < 5:
            raise SamsungMDCError(
                f"Short MDC response: {response.hex(' ')}"
            )

        status = response[4]

        if status == 0x41:
            return

        if status == 0x4E:
            raise SamsungMDCError(
                f"Display rejected command: {response.hex(' ')}"
            )

        raise SamsungMDCError(
            f"Unknown MDC response status 0x{status:02X}: {response.hex(' ')}"
        )

    # ------------------------------------------------------------------
    # Power
    # ------------------------------------------------------------------

    def get_power(self, display_id):
        response = self.send(
            self.CMD_POWER,
            display_id,
        )

        self.raise_if_nak(response)

        if len(response) < 7:
            raise SamsungMDCError(
                f"Invalid power response: {response.hex(' ')}"
            )

        return response[6] == 0x01

    def set_power(self, display_id, on):
        state = 0x01 if on else 0x00

        response = self.send(
            self.CMD_POWER,
            display_id,
            bytes([state]),
        )

        self.raise_if_nak(response)

    def power_on(self, display_id):
        self.set_power(display_id, True)

    def power_off(self, display_id):
        self.set_power(display_id, False)

    # ------------------------------------------------------------------
    # Input source
    # ------------------------------------------------------------------

    def get_source(self, display_id):
        response = self.send(
            self.CMD_INPUT_SOURCE,
            display_id,
        )

        self.raise_if_nak(response)

        if len(response) < 7:
            raise SamsungMDCError(
                f"Invalid source response: {response.hex(' ')}"
            )

        source_code = response[6]

        return self.SOURCE_NAMES.get(
            source_code,
            f"Unknown (0x{source_code:02X})",
        )

    def set_source(self, display_id, source):
        if source not in self.SOURCE_CODES:
            raise ValueError(f"Unsupported source: {source}")

        response = self.send(
            self.CMD_INPUT_SOURCE,
            display_id,
            bytes([self.SOURCE_CODES[source]]),
        )

        self.raise_if_nak(response)

    # ------------------------------------------------------------------
    # Source content orientation
    # ------------------------------------------------------------------

    def set_orientation(self, display_id, orientation):
        """
        LH55VCE source content orientation.

        landscape:
            AA C8 ID 02 82 00 CHECKSUM

        portrait:
            AA C8 ID 02 82 01 CHECKSUM

        landscape_180:
            AA C8 ID 02 82 02 CHECKSUM

        portrait_90:
            AA C8 ID 02 82 03 CHECKSUM

        The LH55VCE accepts SET but returns NAK for GET.
        """

        if orientation not in self.ORIENTATION_CODES:
            raise ValueError(f"Unsupported orientation: {orientation}")

        data = bytes([
            self.SUBCMD_SOURCE_ORIENTATION,
            self.ORIENTATION_CODES[orientation],
        ])

        response = self.send(
            self.CMD_ORIENTATION,
            display_id,
            data,
        )

        self.raise_if_nak(response)

    def landscape(self, display_id):
        self.set_orientation(display_id, "landscape")

    def portrait(self, display_id):
        self.set_orientation(display_id, "portrait")

    def landscape_180(self, display_id):
        self.set_orientation(display_id, "landscape_180")

    def portrait_90(self, display_id):
        self.set_orientation(display_id, "portrait_90")


# ----------------------------------------------------------------------
# One shared serial connection
# ----------------------------------------------------------------------

try:
    mdc = SamsungMDC(SERIAL_PORT)
    print(f"Connected to Samsung MDC on {SERIAL_PORT}")
except Exception as e:
    mdc = None
    print(f"Could not open {SERIAL_PORT}: {e}")


# ----------------------------------------------------------------------
# Background polling
# ----------------------------------------------------------------------

def poll_displays():
    while True:

        if mdc is None:
            time.sleep(2)
            continue

        for display_id in DISPLAY_IDS:

            try:
                power = mdc.get_power(display_id)

                try:
                    source = mdc.get_source(display_id)
                except Exception:
                    source = "Unknown"

                with state_lock:
                    wall_state[display_id]["status"] = "online"
                    wall_state[display_id]["power"] = "On" if power else "Off"
                    wall_state[display_id]["source"] = source

            except Exception:
                with state_lock:
                    wall_state[display_id]["status"] = "offline"

        time.sleep(2)


threading.Thread(target=poll_displays, daemon=True).start()


# ----------------------------------------------------------------------
# Flask routes
# ----------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def get_status():
    with state_lock:
        return jsonify(wall_state)


@app.route("/api/command", methods=["POST"])
def send_command():

    data = request.json or {}

    try:
        display_id = int(data.get("id"))
        command = data.get("command")
        value = data.get("value")

        if display_id not in DISPLAY_IDS:
            raise ValueError(f"Invalid display ID: {display_id}")

        if mdc is None:
            raise SamsungMDCError(f"Serial port {SERIAL_PORT} is not available")

        if command == "power":

            if value == "on":
                mdc.power_on(display_id)

            elif value == "off":
                mdc.power_off(display_id)

            else:
                raise ValueError(f"Invalid power value: {value}")

        elif command == "source":

            mdc.set_source(display_id, value)

        elif command == "orientation":

            mdc.set_orientation(display_id, value)

        else:
            raise ValueError(f"Unknown command: {command}")

        return jsonify({
            "success": True,
            "id": display_id,
            "command": command,
            "value": value,
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        threaded=True,
    )
