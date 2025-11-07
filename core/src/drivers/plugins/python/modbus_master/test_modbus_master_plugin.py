# core/src/drivers/plugins/python/modbus_master/test_modbus_master_plugin.py

import pytest
from unittest.mock import patch

# Assume this is your modbus master module
from core.src.drivers.plugins.python.modbus_master import modbus_master_plugin

def test_modbus_master_reads(mock_modbus_server):
    """
    Test that modbus master reads registers from the mock server.
    """
    # Patch the master so that instead of a real Modbus client,
    # it uses our fake server internally.
    with patch.object(modbus_master_plugin, "read_holding_registers", side_effect=mock_modbus_server.read_holding_registers):
        result = modbus_master_plugin.read_holding_registers(0, 5)
        assert result == [17, 17, 17, 17, 17]

def test_modbus_master_writes(mock_modbus_server):
    """
    Test that modbus master writes registers to the mock server.
    """
    with patch.object(modbus_master_plugin, "write_register", side_effect=mock_modbus_server.write_register):
        ok = modbus_master_plugin.write_register(10, 123)
        assert ok is True
        assert mock_modbus_server.holding_registers[10] == 123


# from pymodbus.client import ModbusTcpClient
# import pytest

# def test_modbus_master_reads(modbus_server):
#     """Ensure the Modbus master can read holding registers."""
#     client = ModbusTcpClient("localhost", port=5020)
#     assert client.connect(), "Client could not connect"

#     rr = client.read_holding_registers(0, 10)
#     assert not rr.isError()
#     assert rr.registers == [17] * 10

#     client.close()


# @pytest.fixture
# def modbus_master_plugin():
#     """Example fixture for your plugin (simplified)."""
#     from core.src.drivers.plugins.python.modbus_master import modbus_master_plugin
#     plugin = modbus_master_plugin.ModbusMasterPlugin("localhost", 5020)
#     yield plugin
#     plugin.stop()


# def test_plugin_reads(modbus_server, modbus_master_plugin):
#     """Test the plugin's synchronous read interface."""
#     modbus_master_plugin.start()
#     data = modbus_master_plugin.read(fc=3, address=0, count=10)
#     assert all(value == 17 for value in data)
#     modbus_master_plugin.stop()
