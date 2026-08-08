import threading
import time

import serial
from flask import Flask, render_template, jsonify, request


app = Flask(__name__)

SERIAL_PORT = "/dev/ttyUSB0"
BAUDRATE = 9600

DISPLAY_IDS = range(1, 41)

# Cache for the 2x20 wall.
wall_state = {
    i: {
        "status": "offline",
        "power": "Off",
        "source": "Unknown",
    }
    for i in DISPLAY_IDS
}

state_lock = threading.Lock()
serial_lock = threading.Lock()


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

    ORIENTATION_CODES = {
        "landscape": 0x00,
        "portrait": 0x01,
        "landscape_180": 0x02,
        "portrait_90": 0x03,
    }

    def __init__(self, port=SERIAL_PORT):
        self.port = port

        self.serial = serial.Serial(
            port=self.port,
            baudrate=BAUDRATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
            write_timeout=1.0,
        )

    def close(self):
        if self.serial.is_open:
            self.serial.close()

    @staticmethod
    def checksum(packet):
        """
        Samsung MDC checksum:
        sum all bytes after AA, modulo 256.
        """
        return sum(packet[1:]) & 0xFF

    def send(self, command, display_id, data=b""):
        """
        Send a Samsung MDC command and return raw response.
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

        self.serial.reset_input_buffer()

        self.serial.write(packet)
        self.serial.flush()

        response = bytearray()

        deadline = time.monotonic() + 1.0

        while time.monotonic() < deadline:
            chunk = self.serial.read(256)

            if chunk:
                response.extend(chunk)

                # Response format:
                # AA FF ID LENGTH ...
                #
                # Total packet length = 5 + LENGTH
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
                f"Unexpected display ID {response[2]}, "
                f"expected {display_id}"
            )

        return bytes(response)

    @staticmethod
    def is_ack(response):
        return len(response) >= 5 and response[4] == 0x41

    @staticmethod
    def raise_if_nak(response):
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
            f"Unknown MDC response: {response.hex(' ')}"
        )

    # ---------------------------------------------------------
    # POWER
    # ---------------------------------------------------------

    def get_power(self, display_id):
        """
        Query power state.

        Request:
            AA 11 ID 00 CHECKSUM

        Example for ID 35:
            AA 11 23 00 34

        Response:
            AA FF 23 03 41 11 STATE CHECKSUM
        """

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
        """
        Set power state.

        ON:
            STATE = 01

        OFF:
            STATE = 00
        """

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

    # ---------------------------------------------------------
    # INPUT SOURCE
    # ---------------------------------------------------------

    def get_source(self, display_id):
        """
        Query input source.

        Samsung MDC command:
            0x14
        """

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

        source_names = {
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

        return source_names.get(
            source_code,
            f"Unknown (0x{source_code:02X})",
        )

    def set_source(self, display_id, source):
        """
        Set input source.

        Supported by this application:
            HDMI1
            HDMI2
            DisplayPort
        """

        if source not in self.SOURCE_CODES:
            raise ValueError(
                f"Unsupported source: {source}"
            )

        source_code = self.SOURCE_CODES[source]

        response = self.send(
            self.CMD_INPUT_SOURCE,
            display_id,
            bytes([source_code]),
        )

        self.raise_if_nak(response)

    # ---------------------------------------------------------
    # SOURCE CONTENT ORIENTATION
    # ---------------------------------------------------------

    def set_orientation(self, display_id, orientation):
        """
        Set source content orientation.

        LH55VCE tested:

            landscape:
                AA C8 ID 02 82 00 CHECKSUM

            portrait:
                AA C8 ID 02 82 01 CHECKSUM

        Modes:
            0 = landscape
            1 = portrait 270°
            2 = landscape 180°
            3 = portrait 90°

        Note:
            LH55VCE accepts SET but returns NAK for GET,
            so orientation is intentionally not polled.
        """

        if orientation not in self.ORIENTATION_CODES:
            raise ValueError(
                f"Unsupported orientation: {orientation}"
            )

        orientation_code = self.ORIENTATION_CODES[orientation]

        data = bytes([
            self.SUBCMD_SOURCE_ORIENTATION,
            orientation_code,
        ])

        response = self.send(
            self.CMD_ORIENTATION,
            display_id,
            data,
        )

        self.raise_if_nak(response)


def open_mdc():
    """
    Open the serial port.

    The serial bus is protected by serial_lock so that
    polling and Flask commands cannot access it simultaneously.
    """
    return SamsungMDC(SERIAL_PORT)


def poll_displays():
    """
    Background loop to update display cache.

    We query power and source for each display.

    Orientation is NOT queried because LH55VCE responds
    with NAK to the orientation GET command.
    """

    while True:

        try:

            with serial_lock:

                mdc = open_mdc()

                try:

                    for display_id in DISPLAY_IDS:

                        try:
                            power = mdc.get_power(display_id)

                            try:
                                source = mdc.get_source(display_id)
                            except Exception:
                                source = "Unknown"

                            with state_lock:
                                wall_state[display_id]["status"] = "online"
                                wall_state[display_id]["power"] = (
                                    "On" if power else "Off"
                                )
                                wall_state[display_id]["source"] = source

                        except Exception:

                            with state_lock:
                                wall_state[display_id]["status"] = "offline"

                finally:
                    mdc.close()

        except Exception as e:
            print(f"Serial Bus Error: {e}")

        time.sleep(2)


# Start polling thread.
threading.Thread(
    target=poll_displays,
    daemon=True,
).start()


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
            raise ValueError(
                f"Invalid display ID: {display_id}"
            )

        with serial_lock:

            mdc = open_mdc()

            try:

                if command == "power":

                    if value == "on":
                        mdc.power_on(display_id)

                    elif value == "off":
                        mdc.power_off(display_id)

                    else:
                        raise ValueError(
                            f"Invalid power value: {value}"
                        )

                elif command == "source":

                    mdc.set_source(
                        display_id,
                        value,
                    )

                elif command == "orientation":

                    mdc.set_orientation(
                        display_id,
                        value,
                    )

                else:

                    raise ValueError(
                        f"Unknown command: {command}"
                    )

            finally:
                mdc.close()

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


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
    )
