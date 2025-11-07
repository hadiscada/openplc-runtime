# tests/conftest.py
import importlib
import time
import pytest
import asyncio
import threading

from unittest.mock import AsyncMock, patch


@pytest.fixture(scope="function")
async def modbus_master_plugin(modbus_server):
    """Fixture that initializes and cleans up the Modbus master plugin."""
    # Import plugin module dynamically
    plugin = importlib.import_module(
        "core.src.drivers.plugins.python.modbus_master.modbus_master_plugin"
    )

    config = {"host": "localhost", "port": 5020}

    # Call INIT and START
    await plugin.INIT(config)
    await plugin.START()

    yield plugin  # <-- yield plugin to the test

    # Cleanup
    await plugin.STOP()


@pytest.fixture(scope="session")
def mock_modbus_server():
    """
    Mock a Modbus TCP server behavior (no asyncio, no sockets).
    It responds to read_holding_registers and write_registers calls
    from the Modbus master client.
    """
    class MockModbusServer:
        def __init__(self):
            self.holding_registers = [17] * 100
            self.coils = [False] * 100
            self.running = True

        def start(self):
            # Simulate a server running in background (threaded)
            self.thread = threading.Thread(target=self._run)
            self.thread.daemon = True
            self.thread.start()

        def _run(self):
            # just simulate that the server is "alive"
            while self.running:
                time.sleep(0.1)

        def stop(self):
            self.running = False
            self.thread.join(timeout=1)

        def read_holding_registers(self, address, count):
            return self.holding_registers[address:address+count]

        def write_register(self, address, value):
            self.holding_registers[address] = value
            return True

    server = MockModbusServer()
    server.start()
    yield server
    server.stop()

# @pytest.fixture
# def mocked_modbus_client():
#     with patch(
#         "core.src.drivers.plugins.python.modbus_master.modbus_master_plugin.AsyncModbusTcpClient"
#     ) as mock_class:
#         mock_client = AsyncMock()
#         mock_client.connect.return_value = True
#         mock_client.close.return_value = True

#         mock_response = AsyncMock()
#         mock_response.isError.return_value = False
#         mock_response.registers = [17] * 10
#         mock_client.read_holding_registers.return_value = mock_response

#         mock_class.return_value = mock_client
#         yield mock_client

# @pytest.fixture(scope="module")
# def modbus_server():
#     """Start a Modbus TCP server in the background for tests."""

#     store = ModbusSlaveContext(
#         di=ModbusSequentialDataBlock(0, [17]*100),
#         co=ModbusSequentialDataBlock(0, [17]*100),
#         hr=ModbusSequentialDataBlock(0, [17]*100),
#         ir=ModbusSequentialDataBlock(0, [17]*100),
#     )
#     context = ModbusServerContext(slaves=store, single=True)

#     identity = ModbusDeviceIdentification()
#     identity.VendorName = "pytest-server"
#     identity.ProductCode = "PM"
#     identity.VendorUrl = "http://example.com"
#     identity.ProductName = "Pytest Modbus Server"
#     identity.ModelName = "Test Server"
#     identity.MajorMinorRevision = "1.0"

#     loop = asyncio.new_event_loop()
#     asyncio.set_event_loop(loop)

#     async def start_server():
#         await StartAsyncTcpServer(
#             context=context,
#             identity=identity,
#             address=("localhost", 5020),
#         )

#     # Run in background thread
#     import threading

#     thread = threading.Thread(target=loop.run_until_complete, args=(start_server(),))
#     thread.daemon = True
#     thread.start()

#     yield  # <-- yield control to test

#     # teardown
#     loop.call_soon_threadsafe(loop.stop)
#     thread.join(timeout=2)
