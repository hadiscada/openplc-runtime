import sys
import os
import json
import traceback
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional, Literal, List, Dict, Any

try:
    from pymodbus.client import ModbusTcpClient
    from pymodbus.exceptions import ModbusIOException, ConnectionException
    from pymodbus.pdu import ExceptionResponse
except ImportError:
    print("[MODBUS_MASTER] ⚠ pymodbus library not found. Please install it: pip install pymodbus")
    # Define dummy classes to allow initial loading without erroring out immediately
    # but actual Modbus operations will fail.
    class ModbusTcpClient:
        def __init__(self, *args, **kwargs):
            self.connected = False
        def connect(self):
            print("[MODBUS_MASTER_DUMMY] ModbusTcpClient.connect() called. pymodbus not installed.")
            self.connected = False # Simulate connection failure
            return self.connected
        def close(self):
            print("[MODBUS_MASTER_DUMMY] ModbusTcpClient.close() called.")
            self.connected = False
        def read_holding_registers(self, *args, **kwargs):
            raise ConnectionException("pymodbus not installed")
        def read_input_registers(self, *args, **kwargs):
            raise ConnectionException("pymodbus not installed")
        def write_single_register(self, *args, **kwargs):
            raise ConnectionException("pymodbus not installed")
        def write_multiple_registers(self, *args, **kwargs):
            raise ConnectionException("pymodbus not installed")
        def read_coils(self, *args, **kwargs):
            raise ConnectionException("pymodbus not installed")
        def write_single_coil(self, *args, **kwargs):
            raise ConnectionException("pymodbus not installed")
    ModbusIOException = Exception
    ConnectionException = Exception
    ExceptionResponse = Exception


Area = Literal["I", "Q", "M"]
Size = Literal["X", "B", "W", "D", "L"]

ADDR_RE = re.compile(r"^%([IQM])([XBWDL])(\d+)(?:\.(\d+))?$", re.IGNORECASE)

@dataclass
class IECAddress:
    area: Area              # 'I' | 'Q' | 'M'
    size: Size              # 'X' | 'B' | 'W' | 'D' | 'L'
    byte: int               # byte base (para X é o byte do bit; p/ B/W/D/L é o início)
    bit: Optional[int]      # só para X
    index_bits: Optional[int]   # índice linear em bits (só p/ X)
    index_bytes: int            # índice linear em bytes (offset no buffer)
    width_bits: int             # 1, 8, 16, 32, 64

def parse_iec_address(s: str) -> IECAddress:
    m = ADDR_RE.match(s.strip())
    if not m:
        raise ValueError(f"Endereço IEC inválido: {s!r}")
    _area, _size, n1, n2 = m.groups()
    # Cast to satisfy Literal type, regex ensures these values.
    area: Area = _area.upper()  # type: ignore 
    size: Size = _size.upper() # type: ignore
    byte = int(n1)
    bit = int(n2) if n2 is not None else None

    if size == "X":
        if bit is None or not (0 <= bit <= 7):
            raise ValueError("Bit ausente ou fora de 0..7 para endereço do tipo X (bit).")
        index_bits = byte * 8 + bit
        index_bytes = byte
        width_bits = 1
    elif size == "B":
        index_bits = None
        index_bytes = byte
        width_bits = 8
    elif size == "W":
        index_bits = None
        index_bytes = byte * 2
        width_bits = 16
    elif size == "D":
        index_bits = None
        index_bytes = byte * 4
        width_bits = 32
    elif size == "L":
        index_bits = None
        index_bytes = byte * 8
        width_bits = 64
    else:
        raise ValueError(f"Tamanho não suportado: {size}")

    return IECAddress(area, size, byte, bit, index_bits, index_bytes, width_bits)

# Add the parent directory to Python path to find shared module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Import the correct type definitions
from shared.python_plugin_types import (
    PluginRuntimeArgs, 
    safe_extract_runtime_args_from_capsule,
    SafeBufferAccess,
    PluginStructureValidator
)

# Import the configuration model
from shared.plugin_config_decode.modbus_master_config_model import ModbusMasterConfig

# Global variables for plugin lifecycle and configuration
runtime_args = None
modbus_master_config: ModbusMasterConfig = None
safe_buffer_accessor: SafeBufferAccess = None
slave_threads: List[threading.Thread] = []

class ModbusSlaveDevice(threading.Thread):
    def __init__(self, device_config: Any, sba: SafeBufferAccess): # device_config type should be more specific, e.g. ModbusDeviceConfig from pydantic model
        super().__init__(daemon=True) # Set as daemon thread so it exits when main program exits
        self.device_config = device_config
        self.sba = sba
        self._stop_event = threading.Event()
        self.client: Optional[ModbusTcpClient] = None
        self.name = f"ModbusSlave-{device_config.name}-{device_config.host}:{device_config.port}"

    def run(self):
        print(f"[{self.name}] Thread started.")
        
        # Use the correct attributes from ModbusDeviceConfig
        host = self.device_config.host
        port = self.device_config.port
        timeout = self.device_config.timeout_ms / 1000.0 # pymodbus uses seconds
        cycle_time = self.device_config.cycle_time_ms / 1000.0 # pymodbus uses seconds
        io_points = self.device_config.io_points

        if not io_points:
            print(f"[{self.name}] No I/O points defined. Stopping thread.")
            return

        self.client = ModbusTcpClient(
            host=host,
            port=port,
            timeout=timeout,
            # retries=3, # Optional: configure retries
            # retry_on_empty=True # Optional
        )

        try:
            if not self.client.connect():
                print(f"[{self.name}] Failed to connect to {host}:{port}.")
                return
            print(f"[{self.name}] Connected to {host}:{port}.")

            while not self._stop_event.is_set():
                cycle_start_time = time.monotonic()

                for point in io_points:
                    if self._stop_event.is_set():
                        break
                    
                    try:
                        iec_addr_str = point.iec_location
                        fc = point.fc
                        offset = point.offset
                        length = point.length

                        if iec_addr_str is None or fc is None:
                            print(f"[{self.name}] ⚠ Skipping I/O point due to missing iec_location or fc: {point}")
                            continue

                        iec_addr = parse_iec_address(iec_addr_str)

                        try: # Outer try for general point processing errors
                            if fc in [1, 2]: # Read Coils (FC1) or Discrete Inputs (FC2)
                                result = None
                                modbus_error = True
                                try: # Inner try for Modbus communication
                                    if fc == 1:
                                        result = self.client.read_coils(address=offset, count=length)
                                    elif fc == 2:
                                        result = self.client.read_discrete_inputs(address=offset, count=length)
                                    
                                    if not result.isError():
                                        modbus_error = False
                                        # Now, acquire mutex and write to IEC buffer
                                        try: # Innermost try for buffer access
                                            self.sba.acquire_buffer_mutex()
                                            for i, bit_val in enumerate(result.bits):
                                                if iec_addr.index_bits is not None and iec_addr.size == "X":
                                                    if i == 0: 
                                                        self.sba.set_bool_value_at_index(iec_addr.area, iec_addr.index_bits + i, bit_val)
                                                elif iec_addr.size != "X" and length == 1:
                                                    int_val = 1 if bit_val else 0
                                                    self.sba.set_int_value_at_index(iec_addr.area, iec_addr.index_bytes, int_val, iec_addr.width_bits // 8)
                                        finally:
                                            self.sba.release_buffer_mutex()
                                finally: # For Modbus communication part
                                    if modbus_error and result:
                                         print(f"[{self.name}] ✗ Error reading coils/inputs (FC{fc}) at {offset}: {result}")
                                    elif not result: # Should not happen if client call succeeded
                                         print(f"[{self.name}] ✗ No result from reading coils/inputs (FC{fc}) at {offset}")
                                    pass # Ensure finally block is not empty


                            elif fc in [3, 4]: # Read Holding Registers (FC3) or Input Registers (FC4)
                                result = None
                                modbus_error = True
                                try: # Inner try for Modbus communication
                                    if fc == 3:
                                        result = self.client.read_holding_registers(address=offset, count=length)
                                    elif fc == 4:
                                        result = self.client.read_input_registers(address=offset, count=length)

                                    if not result.isError():
                                        modbus_error = False
                                        byte_data = bytearray()
                                        for reg_val in result.registers:
                                            byte_data.extend(reg_val.to_bytes(2, 'big'))
                                        
                                        try: # Innermost try for buffer access
                                            self.sba.acquire_buffer_mutex()
                                            if len(byte_data) == (iec_addr.width_bits // 8) * length:
                                                self.sba.set_byte_array_at_index(iec_addr.area, iec_addr.index_bytes, byte_data)
                                            elif length == 1 and len(result.registers) == 1:
                                                self.sba.set_int_value_at_index(iec_addr.area, iec_addr.index_bytes, result.registers[0], iec_addr.width_bits // 8)
                                            else:
                                                print(f"[{self.name}] ⚠ Mismatch in register count ({len(result.registers)}) and IEC size/length for {iec_addr_str}. Data not written.")
                                        finally:
                                            self.sba.release_buffer_mutex()
                                finally: # For Modbus communication part
                                    if modbus_error and result:
                                        print(f"[{self.name}] ✗ Error reading registers (FC{fc}) at {offset}: {result}")
                                    elif not result:
                                         print(f"[{self.name}] ✗ No result from reading registers (FC{fc}) at {offset}")


                            elif fc == 5: # Write Single Coil (FC5)
                                coil_state = None
                                iec_read_error = True
                                try: # Inner try for IEC buffer read
                                    self.sba.acquire_buffer_mutex()
                                    if iec_addr.size == "X" and iec_addr.index_bits is not None:
                                        coil_state = self.sba.get_bool_value_at_index(iec_addr.area, iec_addr.index_bits)
                                        iec_read_error = False
                                    elif iec_addr.size != "X":
                                        int_val = self.sba.get_int_value_at_index(iec_addr.area, iec_addr.index_bytes, iec_addr.width_bits // 8)
                                        coil_state = int_val != 0
                                        iec_read_error = False
                                    else:
                                        print(f"[{self.name}] ⚠ Unsupported IEC type for FC5 (Write Single Coil) for {iec_addr_str}")
                                        # continue # This continue is now outside the mutex lock
                                finally:
                                    self.sba.release_buffer_mutex()

                                if iec_read_error or coil_state is None: # If read failed or type unsupported
                                    continue # Skip to next point

                                # Now perform Modbus write
                                result = self.client.write_single_coil(address=offset, value=coil_state)
                                if result.isError():
                                    print(f"[{self.name}] ✗ Error writing single coil (FC5) at {offset}: {result}")
                                else:
                                    print(f"[{self.name}] ✓ Wrote single coil (FC5) at {offset} to {coil_state}")

                            elif fc == 6: # Write Single Register (FC6)
                                reg_value = None
                                iec_read_error = True
                                try: # Inner try for IEC buffer read
                                    self.sba.acquire_buffer_mutex()
                                    reg_value = self.sba.get_int_value_at_index(iec_addr.area, iec_addr.index_bytes, iec_addr.width_bits // 8)
                                    iec_read_error = False
                                finally:
                                    self.sba.release_buffer_mutex()
                                
                                if iec_read_error or reg_value is None:
                                    continue

                                result = self.client.write_single_register(address=offset, value=reg_value)
                                if result.isError():
                                    print(f"[{self.name}] ✗ Error writing single register (FC6) at {offset}: {result}")
                                else:
                                    print(f"[{self.name}] ✓ Wrote single register (FC6) at {offset} to {reg_value}")

                            elif fc == 15: # Write Multiple Coils (FC15)
                                coils_to_write = []
                                iec_read_error = True
                                read_from_iec = True
                                if not (iec_addr.size == "X" and iec_addr.index_bits is not None):
                                    print(f"[{self.name}] ⚠ FC15 (Write Multiple Coils) is typically for IEC X type. Found {iec_addr_str}. Skipping.")
                                    read_from_iec = False
                                
                                if read_from_iec:
                                    try: # Inner try for IEC buffer read
                                        self.sba.acquire_buffer_mutex()
                                        for i in range(length):
                                            coils_to_write.append(self.sba.get_bool_value_at_index(iec_addr.area, iec_addr.index_bits + i))
                                        iec_read_error = False
                                    finally:
                                        self.sba.release_buffer_mutex()
                                
                                if read_from_iec and (iec_read_error or len(coils_to_write) != length):
                                    print(f"[{self.name}] ⚠ Could not read {length} coil states from IEC for {iec_addr_str}")
                                    continue
                                
                                if not read_from_iec: # If skipped due to type mismatch
                                    continue

                                result = self.client.write_multiple_coils(address=offset, values=coils_to_write)
                                if result.isError():
                                    print(f"[{self.name}] ✗ Error writing multiple coils (FC15) at {offset}: {result}")
                                else:
                                    print(f"[{self.name}] ✓ Wrote {length} coils (FC15) at {offset}")

                            elif fc == 16: # Write Multiple Registers (FC16)
                                bytes_to_write = None
                                iec_read_error = True
                                try: # Inner try for IEC buffer read
                                    self.sba.acquire_buffer_mutex()
                                    bytes_to_write = self.sba.get_byte_array_at_index(iec_addr.area, iec_addr.index_bytes, length * 2)
                                    iec_read_error = False
                                finally:
                                    self.sba.release_buffer_mutex()

                                if iec_read_error or bytes_to_write is None:
                                    continue
                                
                                registers_to_write = []
                                for i in range(0, len(bytes_to_write), 2):
                                    if i + 1 < len(bytes_to_write):
                                        registers_to_write.append(int.from_bytes(bytes_to_write[i:i+2], 'big'))
                                    else:
                                        registers_to_write.append(int.from_bytes(bytes_to_write[i:i+1] + b'\x00', 'big'))
                                
                                if len(registers_to_write) == length:
                                    result = self.client.write_multiple_registers(address=offset, values=registers_to_write)
                                    if result.isError():
                                        print(f"[{self.name}] ✗ Error writing multiple registers (FC16) at {offset}: {result}")
                                    else:
                                        print(f"[{self.name}] ✓ Wrote {length} registers (FC16) at {offset}")
                                else:
                                    print(f"[{self.name}] ⚠ Mismatch in IEC data length for FC16. Expected {length} registers, got {len(registers_to_write)} for {iec_addr_str}")
                            else:
                                print(f"[{self.name}] ⚠ Unsupported Function Code (FC{fc}) for I/O point {iec_addr_str}")

                        except ValueError as ve:
                            print(f"[{self.name}] ✗ ValueError processing I/O point {iec_addr_str}: {ve}")
                        except ModbusIOException as mioe:
                            print(f"[{self.name}] ✗ Modbus IO Error for {iec_addr_str} (FC{fc}): {mioe}")
                        except ConnectionException as ce:
                            print(f"[{self.name}] ✗ Connection Error for {iec_addr_str} (FC{fc}): {ce}. Attempting to reconnect...")
                            if self.client:
                                self.client.close()
                            if not self.client.connect():
                                print(f"[{self.name}] Reconnect failed. Stopping thread for {host}:{port}.")
                                return # Exit thread if reconnect fails
                            print(f"[{self.name}] Reconnected to {host}:{port}.")
                        except Exception as e:
                            print(f"[{self.name}] ✗ Unexpected error processing I/O point {iec_addr_str}: {e}")
                            traceback.print_exc()
                    except Exception as general_point_error:
                        print(f"[{self.name}] ✗ General error processing I/O point: {general_point_error}")
                        traceback.print_exc()
                
                # Calculate remaining time in cycle and sleep
                cycle_elapsed = time.monotonic() - cycle_start_time
                sleep_duration = max(0, cycle_time - cycle_elapsed)
                if sleep_duration > 0:
                    # Check stop event periodically during sleep for faster shutdown
                    for _ in range(int(sleep_duration * 10)): # Check every 0.1s
                        if self._stop_event.is_set():
                            break
                        time.sleep(0.1)
                    if self._stop_event.is_set(): # if woken by stop event
                         break


        except ConnectionException as ce:
            print(f"[{self.name}] ✗ Initial connection failed to {host}:{port}: {ce}")
        except Exception as e:
            print(f"[{self.name}] ✗ Unexpected error in thread: {e}")
            traceback.print_exc()
        finally:
            if self.client and self.client.connected:
                self.client.close()
            print(f"[{self.name}] Thread finished and connection closed.")

    def stop(self): # Renamed from join to stop to avoid confusion with threading.Thread.join()
        print(f"[{self.name}] Stop signal received.")
        self._stop_event.set()
        # The thread will exit its loop and close connection in run() method.
        # No need to join here, stop_loop will handle joining all threads.

def init(args_capsule):
    """
    Initialize the Modbus Master plugin.
    This function receives the arguments encapsulated by the runtime,
    extracts them, and makes them globally available.
    It also handles parsing the settings from the configuration file.
    """
    global runtime_args, modbus_master_config, safe_buffer_accessor

    print("[MODBUS_MASTER] Python plugin 'modbus_master_plugin' initializing...")

    try:
        # 1. Extract runtime args from capsule using safe method
        print("[MODBUS_MASTER] Attempting to extract runtime arguments...")
        if hasattr(args_capsule, '__class__') and 'PyCapsule' in str(type(args_capsule)):
            # This is a PyCapsule from C - use safe extraction
            runtime_args, error_msg = safe_extract_runtime_args_from_capsule(args_capsule)
            if runtime_args is None:
                print(f"[MODBUS_MASTER] ✗ Failed to extract runtime args: {error_msg}")
                return False
            
            print(f"[MODBUS_MASTER] ✓ Runtime arguments extracted successfully.")
        else:
            # This is a direct object (for testing)
            runtime_args = args_capsule
            print(f"[MODBUS_MASTER] ✓ Using direct runtime args for testing.")

        # 2. Create SafeBufferAccess instance for global use
        print("[MODBUS_MASTER] Creating SafeBufferAccess instance...")
        safe_buffer_accessor = SafeBufferAccess(runtime_args)
        if not safe_buffer_accessor.is_valid:
            print(f"[MODBUS_MASTER] ✗ Failed to create SafeBufferAccess: {safe_buffer_accessor.error_msg}")
            return False
        print(f"[MODBUS_MASTER] ✓ SafeBufferAccess instance created.")

        # 3. Load and parse the configuration file
        print("[MODBUS_MASTER] Attempting to load configuration file...")
        config_file_path = None
        try:
            # Try to get the config file path from runtime_args
            # Assuming plugin_specific_config_file_path is an attribute of runtime_args
            # or accessible via SafeBufferAccess.
            # The modbus_slave example uses SafeBufferAccess(runtime_args).get_config_file_args_as_map()
            # which suggests the path might be embedded or accessed this way.
            # However, ModbusMasterConfig expects a direct file path.
            
            # Let's check if runtime_args has a direct attribute for config path first.
            if hasattr(runtime_args, 'plugin_specific_config_file_path'):
                config_file_path = runtime_args.plugin_specific_config_file_path
            else:
                # If not directly on runtime_args
                print("[MODBUS_MASTER] ⚠ Plugin-specific config file path not found directly in runtime_args.")
                # Fallback to a default path if map loading fails or is empty
                current_dir = os.path.dirname(os.path.abspath(__file__))
                default_config_path = os.path.join(current_dir, "modbus_master.json")
                print(f"[MODBUS_MASTER] Falling back to default config path: {default_config_path}")
                config_file_path = default_config_path


            if not config_file_path or not os.path.exists(config_file_path):
                print(f"[MODBUS_MASTER] ✗ Configuration file not found or path is invalid: {config_file_path}")
                return False

            print(f"[MODBUS_MASTER] ✓ Configuration file path: {config_file_path}")
            
            # Initialize ModbusMasterConfig and load from JSON file
            modbus_master_config = ModbusMasterConfig()
            modbus_master_config.import_config_from_file(file_path=config_file_path)
            
            # Validate the loaded configuration
            modbus_master_config.validate()
            
            print(f"[MODBUS_MASTER] ✓ Configuration loaded and validated successfully.")
            print(f"[MODBUS_MASTER]   Total devices configured: {len(modbus_master_config.devices)}")
            for i, device in enumerate(modbus_master_config.devices):
                print(f"[MODBUS_MASTER]   Device {i+1}: '{device.name}'")
                print(f"[MODBUS_MASTER]     Protocol: {device.protocol}")
                print(f"[MODBUS_MASTER]     Target Host: {device.host}")
                print(f"[MODBUS_MASTER]     Target Port: {device.port}")
                print(f"[MODBUS_MASTER]     Cycle Time: {device.cycle_time_ms}ms")
                print(f"[MODBUS_MASTER]     Timeout: {device.timeout_ms}ms")
                print(f"[MODBUS_MASTER]     Number of I/O Points: {len(device.io_points)}")
                for j, point in enumerate(device.io_points):
                    print(f"[MODBUS_MASTER]       I/O Point {j+1}: FC={point.fc}, Offset='{point.offset}', IEC_Loc='{point.iec_location}', Len={point.length}")


        except FileNotFoundError:
            print(f"[MODBUS_MASTER] ✗ Configuration file not found: {config_file_path}")
            return False
        except json.JSONDecodeError as e:
            print(f"[MODBUS_MASTER] ✗ Error decoding JSON configuration: {e}")
            if config_file_path:
                print(f"[MODBUS_MASTER]   File path: {config_file_path}")
            return False
        except ValueError as e: # Catch validation errors from ModbusMasterConfig
            print(f"[MODBUS_MASTER] ✗ Configuration validation error: {e}")
            return False
        except Exception as config_error:
            print(f"[MODBUS_MASTER] ✗ Unexpected error during configuration loading: {config_error}")
            traceback.print_exc()
            return False

        # 4. Optional: Further initialization based on config and runtime_args
        # For example, initializing Modbus client connections, etc.
        # This will likely go into start_loop or be called from here if needed for init.
        print("[MODBUS_MASTER] ✓ Plugin initialization sequence completed.")

        return True

    except Exception as e:
        print(f"[MODBUS_MASTER] ✗ Plugin initialization failed with an unhandled exception: {e}")
        traceback.print_exc()
        return False

def start_loop():
    """Start the Modbus Master communication loop."""
    global runtime_args, modbus_master_config, safe_buffer_accessor, slave_threads

    if runtime_args is None or modbus_master_config is None or safe_buffer_accessor is None:
        print("[MODBUS_MASTER] Error: Plugin not initialized. Call init() first.")
        return False

    print("[MODBUS_MASTER] Starting Modbus Master communication loop...")
    
    # Clear any old slave threads if any (e.g., if start_loop is called multiple times without stop_loop)
    if slave_threads:
        print("[MODBUS_MASTER] ⚠ Previous slave threads found. Stopping them before starting new ones.")
        stop_loop() # This should clear slave_threads and join old threads

    # Use the devices list from the updated configuration model
    devices_to_connect = modbus_master_config.devices
    
    if not devices_to_connect:
        print("[MODBUS_MASTER] ✗ No Modbus slave devices configured to connect.")
        return False

    print(f"[MODBUS_MASTER] Found {len(devices_to_connect)} device(s) to connect.")
    
    for i, device_config in enumerate(devices_to_connect):
        try:
            print(f"[MODBUS_MASTER] Creating thread for device {i+1}: '{device_config.name}' ({device_config.host}:{device_config.port})")
            slave_device_thread = ModbusSlaveDevice(device_config, safe_buffer_accessor)
            slave_threads.append(slave_device_thread)
            slave_device_thread.start()
            print(f"[MODBUS_MASTER] ✓ Thread started for device '{device_config.name}'")
        except Exception as e:
            print(f"[MODBUS_MASTER] ✗ Failed to create or start thread for device '{device_config.name}': {e}")
            traceback.print_exc()
            # Optionally, stop any already started threads and return False
            # For now, we'll try to start as many as possible.

    if not slave_threads:
        print("[MODBUS_MASTER] ✗ No slave threads were started.")
        return False
        
    print(f"[MODBUS_MASTER] ✓ {len(slave_threads)} Modbus slave communication thread(s) started.")
    return True

def stop_loop():
    """Stop the Modbus Master communication loop."""
    global slave_threads
    print("[MODBUS_MASTER] Stopping Modbus Master communication loop...")

    if not slave_threads:
        print("[MODBUS_MASTER] No active slave threads to stop.")
        return True

    threads_to_join = []
    for thread in slave_threads:
        if isinstance(thread, ModbusSlaveDevice) and thread.is_alive():
            print(f"[MODBUS_MASTER] Signaling thread {thread.name} to stop.")
            thread.stop() # This calls ModbusSlaveDevice.stop() which sets the _stop_event
            threads_to_join.append(thread)
        elif thread.is_alive():
            # Fallback for any other type of thread that might be in the list
            print(f"[MODBUS_MASTER] Attempting to join generic thread: {thread.name}")
            threads_to_join.append(thread) # Will just try to join it

    # Wait for all threads to finish
    for thread in threads_to_join:
        try:
            # Join with a timeout to prevent indefinite blocking if a thread misbehaves
            thread.join(timeout=max(5, getattr(thread.device_config, 'timeout_ms', 1000) / 1000.0 + 1)) 
            if thread.is_alive():
                print(f"[MODBUS_MASTER] ⚠ Thread {thread.name} did not terminate in time. It might be a daemon thread or stuck.")
            else:
                print(f"[MODBUS_MASTER] ✓ Thread {thread.name} joined successfully.")
        except Exception as e:
            print(f"[MODBUS_MASTER] ✗ Error joining thread {thread.name}: {e}")
            traceback.print_exc()
    
    slave_threads.clear() # Clear the list after attempting to stop and join all
    print("[MODBUS_MASTER] All slave threads have been signaled to stop and joined (or timed out). Modbus Master loop stopped.")
    return True

def cleanup():
    """Cleanup plugin resources."""
    global runtime_args, modbus_master_config, safe_buffer_accessor
    print("[MODBUS_MASTER] Cleaning up plugin resources...")
    runtime_args = None
    modbus_master_config = None
    safe_buffer_accessor = None
    print("[MODBUS_MASTER] Plugin resources cleaned up.")
    return True

if __name__ == "__main__":
    print("Modbus Master Plugin - Standalone Test Mode")
    
    # Create a mock runtime_args for testing
    class MockRuntimeArgs:
        def __init__(self, config_path):
            self.plugin_specific_config_file_path = config_path # Simulate C providing path
            self.buffer_size = 1024 # Example value
            self.bits_per_buffer = 8 # Example value
            # Mock other attributes that SafeBufferAccess might expect if it directly inspects runtime_args
            # beyond what's needed for get_config_file_args_as_map or direct path access.
            self.bool_output = None 
            self.bool_input = None
            self.int_output = None
            self.int_input = None
            self.buffer_mutex = None # Mock mutex

        def safe_access_buffer_size(self):
            return self.buffer_size, "Success"

        def validate_pointers(self):
            return True, "Mock validation passed"

        def __str__(self):
            return f"MockRuntimeArgs(config_path='{self.plugin_specific_config_file_path}')"

    # Determine the path to the modbus_master.json for testing
    current_script_dir = os.path.dirname(os.path.abspath(__file__))
    test_config_path = os.path.join(current_script_dir, "modbus_master.json")
    
    mock_args = MockRuntimeArgs(config_path=test_config_path)
    
    print(f"Attempting to initialize with mock args and config: {test_config_path}")
    
    if init(mock_args):
        print("Init successful.")
        if start_loop():
            print("Start loop successful.")
            # Simulate running for a bit
            import time
            print("Running for 2 seconds...")
            time.sleep(2)
            if stop_loop():
                print("Stop loop successful.")
            else:
                print("Stop loop failed.")
        else:
            print("Start loop failed.")
        
        if cleanup():
            print("Cleanup successful.")
        else:
            print("Cleanup failed.")
    else:
        print("Init failed.")
