import sys
import os
import json
import traceback
import re
import threading
import time
from typing import Optional, Literal, List, Dict, Any

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException, ConnectionException
from pymodbus.pdu import ExceptionResponse

def get_batch_read_requests_from_io_points(io_points: List[Any]) -> Dict[int, List[Any]]:
    """
    Groups I/O points by Modbus read function code (1,2,3,4) and creates
    batch read lists to optimize Modbus operations.
    Returns a dictionary mapping FC to lists of points.
    """
    read_requests: Dict[int, List[Any]] = {}
    for point in io_points:
        fc = point.fc
        if fc in [1, 2, 3, 4]:  # Read functions
            if fc not in read_requests:
                read_requests[fc] = []
            read_requests[fc].append(point)
    return read_requests

def get_batch_write_requests_from_io_points(io_points: List[Any]) -> Dict[int, List[Any]]:
    """
    Groups I/O points by Modbus write function code (5,6,15,16) and creates
    batch write lists to optimize Modbus operations.
    Returns a dictionary mapping FC to lists of points.
    """
    write_requests: Dict[int, List[Any]] = {}
    for point in io_points:
        fc = point.fc
        if fc in [5, 6, 15, 16]:  # Write functions
            if fc not in write_requests:
                write_requests[fc] = []
            write_requests[fc].append(point)
    return write_requests

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

def init(args_capsule):
    """
    Initialize the Modbus Master plugin.
    This function is called once when the plugin is loaded.
    """
    global runtime_args, modbus_master_config, safe_buffer_accessor
    
    print("🔧 Modbus Master Plugin - Initializing...")
    
    try:
        # Extract runtime arguments from capsule
        runtime_args, error_msg = safe_extract_runtime_args_from_capsule(args_capsule)
        if not runtime_args:
            print(f"✗ Failed to extract runtime args: {error_msg}")
            return False
        
        print("✓ Runtime arguments extracted successfully")
        
        # Create safe buffer accessor
        safe_buffer_accessor = SafeBufferAccess(runtime_args)
        if not safe_buffer_accessor.is_valid:
            print(f"✗ Failed to create SafeBufferAccess: {safe_buffer_accessor.error_msg}")
            return False
        
        print("✓ SafeBufferAccess created successfully")
        
        # Load configuration
        config_path, config_error = safe_buffer_accessor.get_config_path()
        if not config_path:
            print(f"✗ Failed to get config path: {config_error}")
            return False
        
        print(f"📄 Loading configuration from: {config_path}")
        
        modbus_master_config = ModbusMasterConfig()
        modbus_master_config.import_config_from_file(config_path)
        modbus_master_config.validate()
        
        print(f"✓ Configuration loaded successfully: {len(modbus_master_config.devices)} device(s)")
        
        return True
        
    except Exception as e:
        print(f"✗ Error during initialization: {e}")
        import traceback
        traceback.print_exc()
        return False

def start_loop():
    """
    Start the main loop for all configured Modbus devices.
    This function is called after successful initialization.
    """
    global slave_threads, modbus_master_config, safe_buffer_accessor
    
    print("🚀 Modbus Master Plugin - Starting main loop...")
    
    try:
        if not modbus_master_config or not safe_buffer_accessor:
            print("✗ Plugin not properly initialized")
            return False
        
        # Start a thread for each configured device
        for device_config in modbus_master_config.devices:
            try:
                device_thread = ModbusSlaveDevice(device_config, safe_buffer_accessor)
                device_thread.start()
                slave_threads.append(device_thread)
                print(f"✓ Started thread for device: {device_config.name} ({device_config.host}:{device_config.port})")
            except Exception as e:
                print(f"✗ Failed to start thread for device {device_config.name}: {e}")
        
        if slave_threads:
            print(f"✓ Successfully started {len(slave_threads)} device thread(s)")
            return True
        else:
            print("✗ No device threads started")
            return False
            
    except Exception as e:
        print(f"✗ Error starting main loop: {e}")
        import traceback
        traceback.print_exc()
        return False

def stop_loop():
    """
    Stop the main loop and all running device threads.
    This function is called when the plugin needs to be stopped.
    """
    global slave_threads
    
    print("🛑 Modbus Master Plugin - Stopping main loop...")
    
    try:
        if not slave_threads:
            print("ℹ No threads to stop")
            return True
        
        # Signal all threads to stop
        for thread in slave_threads:
            try:
                if hasattr(thread, 'stop'):
                    thread.stop()
                else:
                    print(f"⚠ Thread {thread.name} does not have a stop method")
            except Exception as e:
                print(f"✗ Error stopping thread {thread.name}: {e}")
        
        # Wait for all threads to finish (with timeout)
        timeout_per_thread = 5.0  # seconds
        for thread in slave_threads:
            try:
                thread.join(timeout=timeout_per_thread)
                if thread.is_alive():
                    print(f"⚠ Thread {thread.name} did not stop within timeout")
                else:
                    print(f"✓ Thread {thread.name} stopped successfully")
            except Exception as e:
                print(f"✗ Error joining thread {thread.name}: {e}")
        
        print("✓ Main loop stopped")
        return True
        
    except Exception as e:
        print(f"✗ Error stopping main loop: {e}")
        import traceback
        traceback.print_exc()
        return False

def cleanup():
    """
    Clean up resources before plugin unload.
    This function is called when the plugin is being unloaded.
    """
    global runtime_args, modbus_master_config, safe_buffer_accessor, slave_threads
    
    print("🧹 Modbus Master Plugin - Cleaning up...")
    
    try:
        # Stop all threads if not already stopped
        stop_loop()
        
        # Clear thread list
        slave_threads.clear()
        
        # Reset global variables
        runtime_args = None
        modbus_master_config = None
        safe_buffer_accessor = None
        
        print("✓ Cleanup completed successfully")
        return True
        
    except Exception as e:
        print(f"✗ Error during cleanup: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    """
    Test mode for development purposes.
    This allows running the plugin standalone for testing.
    """
    print("🧪 Modbus Master Plugin - Test Mode")
    print("This plugin is designed to be loaded by the OpenPLC runtime.")
    print("Standalone testing is not fully supported without runtime integration.")
    
    # You could add basic configuration validation here
    try:
        test_config = ModbusMasterConfig()
        print("✓ Configuration model can be instantiated")
    except Exception as e:
        print(f"✗ Error testing configuration model: {e}")
