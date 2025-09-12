import asyncio
import logging
import os
import sqlite3
import ssl
import threading
from pathlib import Path

import flask
import flask_login
import openplc
from credentials import CertGen
from restapi import (
    app_restapi,
    db,
    register_callback_get,
    register_callback_post,
    restapi_bp,
)
from unixclient import AsyncUnixClient, queue

app = flask.Flask(__name__)
app.secret_key = str(os.urandom(16))
login_manager = flask_login.LoginManager()
login_manager.init_app(app)

logger = logging.getLogger(__name__)
command_queue: queue.Queue = queue.Queue()

openplc_runtime = openplc.runtime()


BASE_DIR = Path(__file__).parent
CERT_FILE = (BASE_DIR / "certOPENPLC.pem").resolve()
KEY_FILE = (BASE_DIR / "keyOPENPLC.pem").resolve()
HOSTNAME = "localhost"

""" Create a connection to the database file """


def create_connection(db_file):
    try:
        conn = sqlite3.connect(db_file)
        return conn
    except sqlite3.Error as e:
        logger.error("Error creating database connection: %s", e)

    return None


def restapi_callback_get(argument: str, data: dict) -> dict:
    """
    This is the central callback function that handles the logic
    based on the 'argument' from the URL and 'data' from the request.
    """
    global command_queue
    logger.debug("GET | Received argument: %s, data: %s", argument, data)

    if argument == "start-plc":
        # openplc_runtime.start_runtime()
        # configure_runtime()
        command_queue.put({"action": "start-plc", "data": data})
        return {"status": "runtime started"}

    elif argument == "stop-plc":
        # openplc_runtime.stop_runtime()
        command_queue.put({"action": "stop-plc", "data": data})
        return {"status": "runtime stopped"}

    elif argument == "runtime-logs":
        logs = openplc_runtime.logs()
        return {"runtime-logs": logs}

    elif argument == "compilation-status":
        try:
            logs = openplc_runtime.compilation_status()
            _logs = logs
        except Exception as e:
            logger.error("Error retrieving compilation logs: %s", e)
            _logs = str(e)

        status = _logs
        if status is not str:
            _status = "No compilation in progress"
        if "Compilation finished successfully!" in status:
            _status = "Success"
            _error = "No error"
        elif "Compilation finished with errors!" in status:
            _status = "Error"
            _error = openplc_runtime.get_compilation_error()
        else:
            _status = "Compiling"
            _error = openplc_runtime.get_compilation_error()
        logger.debug(
            "Compilation status: %s, logs: %s", _status, _logs, extra={"error": _error}
        )

        return {"status": _status, "logs": _logs, "error": _error}

    elif argument == "status":
        return {"current_status": "operational", "details": data}

    elif argument == "ping":
        return {"status": "pong"}
    else:
        return {"error": "Unknown argument"}


# file upload POST handler
def restapi_callback_post(argument: str, data: dict) -> dict:
    logger.debug("POST | Received argument: %s, data: %s", argument, data)

    if argument == "upload-file":
        try:
            # validate filename
            if "file" not in flask.request.files:
                return {"UploadFileFail": "No file part in the request"}
            st_file = flask.request.files["file"]
            # validate file size
            if st_file.content_length > 32 * 1024 * 1024:  # 32 MB limit
                return {"UploadFileFail": "File is too large"}

            # replace program file on database
            try:
                database = "openplc.db"
                conn = create_connection(database)
                logger.info("%s connected", database)
                if conn is not None:
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT * FROM Programs WHERE Name = 'webserver_program'"
                        )
                        row = cur.fetchone()

                        filename = str(row[3])
                        st_file.save(f"st_files/{filename}")

                        cur.close()
                    except Exception as e:
                        return {"UploadFileFail": e}
            except Exception as e:
                return {"UploadFileFail": f"Error connecting to the database: {e}"}

        except Exception as e:
            return {"UploadFileFail": e}

        if openplc_runtime.status() == "Compiling":
            return {"RuntimeStatus": "Compiling"}

        try:
            openplc_runtime.compile_program(f"{filename}")
            return {"CompilationStatus": "Starting program compilation"}
        except Exception as e:
            return {"CompilationStatusFail": e}

    else:
        return {"PostRequestError": "Unknown argument"}


def run_https():
    # rest api register
    app_restapi.register_blueprint(restapi_bp, url_prefix="/api")
    register_callback_get(restapi_callback_get)
    register_callback_post(restapi_callback_post)

    with app_restapi.app_context():
        try:
            db.create_all()
            db.session.commit()
            logger.info("Database tables created successfully.")
        except Exception as e:
            logger.error("Error creating database tables: %s", e)

    try:
        # CertGen class is used to generate SSL certificates and verify their validity
        cert_gen = CertGen(hostname=HOSTNAME, ip_addresses=["127.0.0.1"])
        # Generate certificate if it doesn't exist
        if not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
            cert_gen.generate_self_signed_cert(cert_file=CERT_FILE, key_file=KEY_FILE)
        # Verify expiration date
        elif cert_gen.is_certificate_valid(CERT_FILE):
            print(
                cert_gen.generate_self_signed_cert(
                    cert_file=CERT_FILE, key_file=KEY_FILE
                )
            )
        # Credentials already created
        else:
            print("Credentials already generated!")

        try:
            context = (CERT_FILE, KEY_FILE)
            app_restapi.run(
                debug=False,
                host="0.0.0.0",
                threaded=True,
                port=8443,
                ssl_context=context,
            )
        except KeyboardInterrupt as e:
            logger.info("Exiting OpenPLC Webserver...%s", e)
            openplc_runtime.stop_runtime()
        except Exception as e:
            logger.error("An error occurred: %s", e)
            openplc_runtime.stop_runtime()

    except FileNotFoundError as e:
        logger.error("Could not find SSL credentials! %s", e)
    except ssl.SSLError as e:
        logger.error("SSL credentials FAIL! %s", e)


async def async_unix_socket(command_queue: queue.Queue):
    """Main Unix client loop that runs in the background."""
    client = AsyncUnixClient(command_queue, "/tmp/plc_runtime.socket")

    # Wait until server socket exists before connecting
    for _ in range(50):  # ~5 seconds max
        if os.path.exists(client.socket_path):
            break
        logger.info("Waiting for server socket %s...", client.socket_path)
        await asyncio.sleep(0.1)
    else:
        logger.error("Server socket was never created!")
        return

    try:
        await client.connect()
    except (FileNotFoundError, ConnectionRefusedError) as e:
        logger.error("Failed to connect to Unix socket: %s", e)
        return

    logger.info("Unix client connected successfully")
    
    # Kick off background processing of the command queue
    # asyncio.create_task(client.process_command_queue())

    # Keep the client alive
    try:
        while True:
            await client.process_command_queue()
            # await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        await client.close()
        logger.info("Unix client stopped")


def start_asyncio_loop(async_loop):
    """Run asyncio loop in its own thread"""
    asyncio.set_event_loop(async_loop)
    async_loop.run_forever()


if __name__ == "__main__":
    threading.Thread(target=run_https, daemon=True).start()

    loop = asyncio.new_event_loop()
    async_thread = threading.Thread(
        target=start_asyncio_loop, args=(loop,), daemon=True
    )
    async_thread.start()

    future = asyncio.run_coroutine_threadsafe(
        async_unix_socket(command_queue), loop
    )

    logger.info("Main thread is running (REST API + Unix client).")
    try:
        future.result()
    except KeyboardInterrupt:
        logger.error("Stopping services...")
        loop.call_soon_threadsafe(loop.stop)
        async_thread.join()
    finally:
        logger.warning("Program finished.")