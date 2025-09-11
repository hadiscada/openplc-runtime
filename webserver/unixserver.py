import asyncio
import logging
import os
import queue
import re
from typing import Set

logger = logging.getLogger(__name__)


class AsyncUnixServer:
    def __init__(
        self,
        command_queue: queue.Queue,
        socket_path="/tmp/openplc.sock",
        max_clients=100,
    ):
        self.socket_path = socket_path
        self.max_clients = max_clients
        self.clients: Set[asyncio.StreamWriter] = set()
        self.message_rate = 0.1
        self.command_queue = command_queue

        # Clean up any existing socket file
        if os.path.exists(self.socket_path):
            logger.info("Removing existing socket file: %s", self.socket_path)
            os.unlink(self.socket_path)

    def validate_message(self, message: str) -> bool:
        """Validate message format"""
        if not message or len(message) > 100:
            return False
        if not re.match(r"^[\w\s.,!?\-]+$", message):
            return False
        return True

    async def process_command_queue(self):
        """Continuously process commands from the queue."""
        while True:
            try:
                command = self.command_queue.get_nowait()
                logger.info("Processing command from queue: %s", command)

                action = command.get("action")
                data = command.get("data")
                if action == "start-plc":
                    await self.handle_start_plc(data)
                # elif action == "stop-plc":
                #     await self.handle_stop_plc(data)
                # elif action == "runtime-logs":
                #     await self.handle_runtime_logs(data)
                # elif action == "compilation-status":
                #     await self.handle_compilation_status(data)
                # elif action == "status":
                #     await self.handle_status(data)
                # elif action == "ping":
                #     await self.handle_ping(data)

                self.command_queue.task_done()

            except queue.Empty:
                await asyncio.sleep(0.1)

            except Exception as e:
                logger.error("Error processing command from queue: %s", e)

    async def handle_start_plc(self, data):
        print(f"Starting PLC with data: {data}")

    async def handle_stop_plc(self, data):
        print(f"Stopping PLC with data: {data}")

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Handle individual client connection"""
        try:
            logger.info("Client connected")

            # Store client info
            self.clients.add(writer)

            while True:
                try:
                    logger.info("Waiting for data from client...")

                    # Peek at the first few bytes to detect protocol
                    peek_data = await reader.read(4)

                    if not peek_data:
                        logger.info("No data received (connection closed)")
                        break

                    # Check if this looks like a length prefix or a simple message
                    if len(peek_data) == 4:
                        # Try to interpret as length prefix
                        potential_length = int.from_bytes(peek_data, "big")

                        if potential_length <= 100:  # Reasonable message length
                            logger.info(
                                "Detected length-prefixed protocol, length: %d",
                                potential_length,
                            )

                            # Read the actual message
                            message_data = await reader.read(potential_length)
                            if (
                                not message_data
                                or len(message_data) != potential_length
                            ):
                                logger.warning("Incomplete message data")
                                break

                            try:
                                message = message_data.decode("utf-8")
                                logger.info("Received message: '%s'", message)

                                # Process and respond with same protocol
                                response = f"PONG: {message}"
                                response_bytes = response.encode("utf-8")
                                length_prefix = len(response_bytes).to_bytes(4, "big")
                                writer.write(length_prefix + response_bytes)
                                await writer.drain()
                                logger.info("Response sent: '%s'", response)

                            except UnicodeDecodeError:
                                logger.warning("Invalid UTF-8 encoding")
                                break

                        else:
                            # This might be a simple text message starting with "PING"
                            try:
                                message = peek_data.decode("utf-8")
                                logger.info(
                                    "Detected simple text protocol: '%s'", message
                                )

                                if message == "PING":
                                    response = "PONG"
                                    writer.write(response.encode("utf-8"))
                                    await writer.drain()
                                    logger.info("Responded with: '%s'", response)
                                else:
                                    logger.warning(
                                        "Unknown simple message: '%s'", message
                                    )
                                    break

                            except UnicodeDecodeError:
                                print("Invalid data format")
                                break

                    else:
                        # Handle shorter messages
                        try:
                            message = peek_data.decode("utf-8")
                            logger.info("Received short message: '%s'", message)

                            if message == "PING":
                                response = "PONG"
                                writer.write(response.encode("utf-8"))
                                await writer.drain()
                                logger.info("Responded with: '%s'", response)

                        except UnicodeDecodeError:
                            logger.error("Invalid short message data")
                            break

                except asyncio.TimeoutError:
                    logger.warning("Timeout with client")
                    break
                except ConnectionResetError:
                    logger.warning("Connection reset by client")
                    break
                except Exception as e:
                    logger.error("Error with client: %s: %s", type(e).__name__, e)
                    break

        except Exception as e:
            logger.error("Client handler error: %s: %s", type(e).__name__, e)
        finally:
            logger.info("Client disconnected")
            self.clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except asyncio.CancelledError:
                pass
            except BrokenPipeError:
                pass

    async def run_server(self):
        """Start the async Unix socket server"""
        try:
            # Create the Unix socket server
            server = await asyncio.start_unix_server(
                self.handle_client, self.socket_path, limit=1024, start_serving=True
            )

            print(f"Unix socket server running on {self.socket_path}")
            print("Server supports both protocols:")
            print("1. Length-prefixed: [4-byte length][message]")
            print("2. Simple text: plain text messages like 'PING'")

            # Set appropriate permissions for the socket file
            os.chmod(self.socket_path, 0o666)

            async with server:
                logger.info("Server started successfully. Waiting for connections...")
                await server.serve_forever()

        except Exception as e:
            logger.error("Failed to start server: %s: %s", type(e).__name__, e)
            raise
        finally:
            # Clean up
            logger.info("Cleaning up resources...")
            for writer in list(self.clients):
                try:
                    writer.close()
                    await writer.wait_closed()
                except asyncio.CancelledError:
                    pass

            # Remove socket file
            if os.path.exists(self.socket_path):
                try:
                    os.unlink(self.socket_path)
                except FileNotFoundError:
                    pass
