#!/usr/bin/env python3
"""Find the EBB and query its firmware version. Read-only: no motors, no pen, no motion."""
import sys
import serial
from serial.tools import list_ports

EBB_VID, EBB_PID = 0x04D8, 0xFD92


def find_port():
    for p in list_ports.comports():
        if (p.vid, p.pid) == (EBB_VID, EBB_PID):
            return p.device
    return None


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    if not port:
        print("No EBB (04d8:fd92) found. Serial ports seen:")
        for p in list_ports.comports():
            print(f"  {p.device}  {p.vid and hex(p.vid)}:{p.pid and hex(p.pid)}  {p.description}")
        return 1
    with serial.Serial(port, 9600, timeout=2) as s:
        s.reset_input_buffer()
        s.write(b"V\r")
        print(f"{port}: {s.readline().decode(errors='replace').strip()!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
