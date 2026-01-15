import os
import json
import time
import threading
import can
import openplc_python as openplc

class CanbusMaster:
    def __init__(self):
        self.config_path = "/root/openplc-runtime/core/generated/conf/canbus_conf.json"
        self.devices = []
        self.running = False
        self.bus = None

    def load_config(self):
        if not os.path.exists(self.config_path):
            return False
        try:
            with open(self.config_path, 'r') as f:
                data = json.load(f)
                if data.get("canbus_enabled") != "true":
                    return False
                self.devices = data.get("devices", [])
            return True
        except Exception as e:
            print(f"CANbus Master: Config Error: {e}")
            return False

    def parse_iec_address(self, iec_str):
        """
        Input: '%IX2.0' -> Output: (Type='%IX', Byte=2, Bit=0)
        """
        try:
            addr_type = iec_str[:3] # %IX atau %QX
            parts = iec_str[3:].split('.')
            return addr_type, int(parts[0]), int(parts[1])
        except:
            return None, 0, 0

    def process_pdo_in(self, msg):
        """
        Hardware -> PLC (%IX)
        Standard CANopen: Node 1 PDO1 In = 0x181, Node 2 = 0x182, dst.
        """
        node_id = msg.arbitration_id - 0x180
        if node_id < 1 or node_id > 127: return

        for device in self.devices:
            if device["node_id"] == node_id:
                for group in device.get("io_groups", []):
                    if group["type"] == "DI":
                        addr_type, start_byte, start_bit = self.parse_iec_address(group["iec_location"])
                        
                        for i in range(group["len"]):
                            # Hitung posisi bit di payload CAN (max 8 byte / 64 bit)
                            can_byte_idx = i // 8
                            can_bit_idx = i % 8
                            bit_val = (msg.data[can_byte_idx] >> can_bit_idx) & 1
                            
                            # Hitung alamat IEC tujuan (OpenPLC %IX Byte.Bit)
                            # Logika: Bit ke-i dari offset akan mengisi Byte.(Bit + i)
                            total_bits = (start_byte * 8) + start_bit + i
                            target_byte = total_bits // 8
                            target_bit = total_bits % 8
                            iec_addr = f"%IX{target_byte}.{target_bit}"
                            
                            openplc.set_iec_variable(iec_addr, bit_val)

    def write_outputs_loop(self):
        """
        PLC (%QX) -> Hardware
        Standard CANopen: Node 1 PDO1 Out = 0x201, Node 2 = 0x202, dst.
        """
        while self.running:
            for device in self.devices:
                cob_id = 0x200 + device["node_id"]
                payload = [0] * 8 # Inisialisasi 8 byte data CAN
                has_outputs = False

                for group in device.get("io_groups", []):
                    if group["type"] == "DO":
                        has_outputs = True
                        addr_type, start_byte, start_bit = self.parse_iec_address(group["iec_location"])
                        
                        for i in range(group["len"]):
                            total_bits = (start_byte * 8) + start_bit + i
                            current_byte = total_bits // 8
                            current_bit = total_bits % 8
                            iec_addr = f"%QX{current_byte}.{current_bit}"
                            
                            val = openplc.get_iec_variable(iec_addr)
                            if val:
                                can_byte_idx = i // 8
                                can_bit_idx = i % 8
                                payload[can_byte_idx] |= (1 << can_bit_idx)
                
                if has_outputs and self.bus:
                    msg = can.Message(arbitration_id=cob_id, data=payload[:2], is_extended_id=False)
                    try:
                        self.bus.send(msg)
                    except: pass
            
            time.sleep(0.02) # Scan rate 20ms

    def start(self):
        if not self.load_config():
            print("CANbus Master: Disabled or no config.")
            return
        
        try:
            # Gunakan socketcan can0
            self.bus = can.interface.Bus(channel='can0', bustype='socketcan')
            self.running = True
            
            # Jalankan loop output di thread terpisah
            threading.Thread(target=self.write_outputs_loop, daemon=True).start()
            
            print("CANbus Master Plugin: Operational")
            
            # Loop input (blocking)
            while self.running:
                msg = self.bus.recv(0.5)
                if msg:
                    # Filter COB-ID untuk PDO1 (0x181 - 0x1FF)
                    if 0x181 <= msg.arbitration_id <= 0x1FF:
                        self.process_pdo_in(msg)
        except Exception as e:
            print(f"CANbus Master Runtime Error: {e}")

if __name__ == "__main__":
    master = CanbusMaster()
    master.start()