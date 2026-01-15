import errno
import json
import os
import platform
import shutil
import ssl
import threading
from pathlib import Path
from typing import Callable, Final, Optional

import flask
import flask_login

from webserver.credentials import CertGen
from webserver.debug_websocket import init_debug_websocket
from webserver.logger import get_logger
from webserver.plcapp_management import (
    MAX_FILE_SIZE,
    BuildStatus,
    analyze_zip,
    build_state,
    run_compile,
    safe_extract,
    update_plugin_configurations,
)
from webserver.restapi import (
    app_restapi,
    db,
    register_callback_get,
    register_callback_post,
    restapi_bp,
)
from webserver.runtimemanager import RuntimeManager

logger, _ = get_logger("logger", use_buffer=True)

app = flask.Flask(__name__)
app.secret_key = str(os.urandom(16))
login_manager = flask_login.LoginManager()
login_manager.init_app(app)

runtime_manager = RuntimeManager(
    runtime_path="./build/plc_main",
    plc_socket="/run/runtime/plc_runtime.socket",
    log_socket="/run/runtime/log_runtime.socket",
)

runtime_manager.start()

BASE_DIR: Final[Path] = Path(__file__).parent
CERT_FILE: Final[Path] = (BASE_DIR / "certOPENPLC.pem").resolve()
KEY_FILE: Final[Path] = (BASE_DIR / "keyOPENPLC.pem").resolve()
HOSTNAME: Final[str] = "localhost"


def handle_start_plc(data: dict) -> dict:
    response = runtime_manager.start_plc()
    return {"status": response}


def handle_stop_plc(data: dict) -> dict:
    response = runtime_manager.stop_plc()
    return {"status": response}


def handle_runtime_logs(data: dict) -> dict:
    if "id" in data:
        min_id = int(data["id"])
    else:
        min_id = None
    if "level" in data:
        level = data["level"]
    else:
        level = None
    response = runtime_manager.get_logs(min_id=min_id, level=level)
    return {"runtime-logs": response}


def handle_compilation_status(data: dict) -> dict:
    return {
        "status": build_state.status.name,
        "logs": build_state.logs[:],  # all lines
        "exit_code": build_state.exit_code,
    }


def parse_timing_stats(stats_response: Optional[str]) -> Optional[dict]:
    """
    Parse the STATS response from the runtime.
    Expected format: STATS:{json_object}
    Returns the parsed JSON object or None if parsing fails.
    """
    if stats_response is None:
        return None

    # Remove the STATS: prefix
    if stats_response.startswith("STATS:"):
        json_str = stats_response[6:].strip()
    else:
        return None

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


def handle_status(data: dict) -> dict:
    response = runtime_manager.status_plc()
    if response is None:
        return {"status": "No response from runtime"}

    result: dict = {"status": response}

    # Only fetch timing stats if explicitly requested via include_stats parameter.
    # This avoids acquiring the stats mutex on every status poll, which could
    # introduce latency to the critical PLC scan cycle.
    include_stats = data.get("include_stats", "").lower() == "true"
    if include_stats:
        stats_response = runtime_manager.stats_plc()
        timing_stats = parse_timing_stats(stats_response)
        if timing_stats is not None:
            result["timing_stats"] = timing_stats

    return result


def handle_ping(data: dict) -> dict:
    response = runtime_manager.ping()
    return {"status": response}


def handle_scan_canbus(data: dict) -> dict:
    result = scan_canbus()
    return {
        "status": "success", 
        "found_devices_count": len(result), 
        "devices": result
    }


def scan_canbus():
    if can is None:
        return {"error": "Library python-can is not installed"}
    
    # Cek apakah interface down, jika ya, nyalakan sebentar
    interface_was_down = False
    import subprocess
    status = subprocess.getoutput("ip link show can0")
    if "DOWN" in status or "not found" in status:
        interface_was_down = True
        os.system("ip link set can0 type can bitrate 1000000 && ip link set can0 up")
        time.sleep(1)


    devices = []
    # Batasi limit scan untuk kecepatan (biasanya ID 1-32 sudah cukup untuk I/O)
    SCAN_LIMIT = 33 
    
    try:
        # Gunakan timeout dasar 0.03s untuk keseimbangan kecepatan & reliabilitas
        bus = can.interface.Bus(channel='can0', bustype='socketcan', timeout=0.03, receive_own_messages=False)
        
        for node_id in range(1, SCAN_LIMIT):
            # --- FASE 1: DETEKSI CEPAT (DOUBLE-CHECK) ---
            # Reset filter ke mode terbuka agar bisa mendeteksi semua balasan
            bus.set_filters([]) 
            node_found = False
            
            # Coba 2 kali untuk memastikan node tidak terlewat karena tabrakan data
            for attempt in range(2):
                # Bersihkan sisa pesan di buffer (Flush)
                while bus.recv(0.001): pass 
                
                # SDO Read Index 0x1000 (Device Type)
                sdo_detect = can.Message(
                    arbitration_id=0x600 + node_id,
                    data=[0x40, 0x00, 0x10, 0x00, 0x00, 0x00, 0x00, 0x00],
                    is_extended_id=False
                )
                bus.send(sdo_detect)
                
                # Tunggu balasan SDO (0x580 + NodeID)
                reply = bus.recv(0.04) 
                
                if reply and reply.arbitration_id == (0x580 + node_id):
                    node_found = True
                    break
                
                # Jeda sangat singkat sebelum coba lagi
                time.sleep(0.01)

            # --- FASE 2: WAWANCARA DETAIL (Hanya jika Node Ditemukan) ---
            if node_found:
                # Pasang Hardware Filter khusus ID ini agar pembacaan Vendor/Product 100% stabil
                # Kernel Linux hanya akan meloloskan ID balasan dari node ini
                bus.set_filters([{"can_id": 0x580 + node_id, "can_mask": 0x7FF, "extended": False}])
                time.sleep(0.01) # Jeda sinkronisasi filter
                
                vendor_id = "Unknown"
                product_code = "Unknown"

                # Ambil Vendor ID (Index 0x1018 Sub 1)
                bus.send(can.Message(arbitration_id=0x600 + node_id,
                                     data=[0x40, 0x18, 0x10, 0x01, 0x00, 0x00, 0x00, 0x00],
                                     is_extended_id=False))
                v_reply = bus.recv(0.05)
                if v_reply:
                    v_val = int.from_bytes(v_reply.data[4:], 'little')
                    vendor_id = hex(v_val)

                # Ambil Product Code (Index 0x1018 Sub 2)
                bus.send(can.Message(arbitration_id=0x600 + node_id,
                                     data=[0x40, 0x18, 0x10, 0x02, 0x00, 0x00, 0x00, 0x00],
                                     is_extended_id=False))
                p_reply = bus.recv(0.05)
                if p_reply:
                    p_val = int.from_bytes(p_reply.data[4:], 'little')
                    product_code = hex(p_val)

                # Tambahkan ke hasil list
                devices.append({
                    "node_id": node_id,
                    "hex_id": hex(node_id),
                    "vendor_id": vendor_id,
                    "product_code": product_code,
                    "type": "CANopen Device"
                })
            
            # Beri jeda antar node agar tidak membanjiri bus (Bus Flood)
            time.sleep(0.005)
            
        bus.shutdown()
    except Exception as e:
        return {"error": str(e)}
    
    # Jika tadi kita nyalakan paksa, matikan kembali setelah selesai scan
    if interface_was_down:
        os.system("ip link set can0 down")


    return devices


GET_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "start-plc": handle_start_plc,
    "stop-plc": handle_stop_plc,
    "runtime-logs": handle_runtime_logs,
    "compilation-status": handle_compilation_status,
    "status": handle_status,
    "ping": handle_ping,
    "scan-canbus": handle_scan_canbus,
}


def restapi_callback_get(argument: str, data: dict) -> dict:
    """
    Dispatch GET callbacks by argument.
    """
    # logger.debug("GET | Received argument: %s, data: %s", argument, data)
    handler = GET_HANDLERS.get(argument)
    if handler:
        return handler(data)
    return {"error": "Unknown argument"}


def handle_upload_file(data: dict) -> dict:
    if build_state.status == BuildStatus.COMPILING:
        return {
            "UploadFileFail": "Runtime is compiling another program, please wait",
            "CompilationStatus": build_state.status.name,
        }

    build_state.clear()  # remove all previous build logs

    if "file" not in flask.request.files:
        build_state.status = BuildStatus.FAILED
        return {
            "UploadFileFail": "No file part in the request",
            "CompilationStatus": build_state.status.name,
        }

    zip_file = flask.request.files["file"]

    if zip_file.content_length > MAX_FILE_SIZE:
        build_state.status = BuildStatus.FAILED
        return {
            "UploadFileFail": "File is too large",
            "CompilationStatus": build_state.status.name,
        }

    try:
        build_state.status = BuildStatus.UNZIPPING
        safe, valid_files = analyze_zip(zip_file)
        if not safe:
            build_state.status = BuildStatus.FAILED
            return {
                "UploadFileFail": "Uploaded ZIP file failed safety checks",
                "CompilationStatus": build_state.status.name,
            }

        extract_dir = "core/generated"
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)

        safe_extract(zip_file, extract_dir, valid_files)

        # Update plugin configurations based on extracted config files
        update_plugin_configurations(extract_dir)

        # Start compilation in a separate thread
        build_state.status = BuildStatus.COMPILING

        task_compile = threading.Thread(
            target=run_compile,
            args=(runtime_manager,),
            kwargs={"cwd": extract_dir},
            daemon=True,
        )

        task_compile.start()

        return {"UploadFileFail": "", "CompilationStatus": build_state.status.name}

    except (OSError, IOError) as e:
        build_state.status = BuildStatus.FAILED
        build_state.log(f"[ERROR] File system error: {e}")
        return {
            "UploadFileFail": f"File system error: {e}",
            "CompilationStatus": build_state.status.name,
        }
    except Exception as e:
        build_state.status = BuildStatus.FAILED
        build_state.log(f"[ERROR] Unexpected error: {e}")
        return {
            "UploadFileFail": f"Unexpected error: {e}",
            "CompilationStatus": build_state.status.name,
        }


POST_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "upload-file": handle_upload_file,
}


def restapi_callback_post(argument: str, data: dict) -> dict:
    """
    Dispatch POST callbacks by argument.
    """
    # logger.debug("POST | Received argument: %s, data: %s", argument, data)
    handler = POST_HANDLERS.get(argument)

    if not handler:
        return {"PostRequestError": "Unknown argument"}

    return handler(data)


def run_https():
    # rest api register
    app_restapi.register_blueprint(restapi_bp, url_prefix="/api")
    register_callback_get(restapi_callback_get)
    register_callback_post(restapi_callback_post)

    socketio = init_debug_websocket(app_restapi, runtime_manager.runtime_socket)

    with app_restapi.app_context():
        try:
            db.create_all()
            db.session.commit()
            # logger.info("Database tables created successfully.")
        except Exception:
            # logger.error("Error creating database tables: %s", e)
            pass

    # On non-Linux platforms (MSYS2/Cygwin), patch Python SSL recv socket
    # to handle EAGAIN/EWOULDBLOCK errors that cause "Resource temporarily unavailable"
    is_linux = platform.system() == "Linux"
    if not is_linux:
        print(f"Non-Linux platform detected ({platform.system()}). Patching recv socket...")
        _orig_recv = ssl.SSLSocket.recv

        def _patched_recv(self, buflen, flags=0):
            try:
                return _orig_recv(self, buflen, flags)
            except BlockingIOError as e:
                # Only swallow EAGAIN / EWOULDBLOCK (errno 11) - re-raise other errors
                if getattr(e, "errno", None) in (errno.EAGAIN, errno.EWOULDBLOCK, 11):
                    return b""
                raise

        ssl.SSLSocket.recv = _patched_recv

    try:
        cert_gen = CertGen(hostname=HOSTNAME, ip_addresses=["127.0.0.1"])

        # Check if certificate exists. If not, generate one
        if not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
            # logger.info("Generating https certificate...")
            print(
                "Generating https certificate..."
            )  # TODO: remove this temporary print once logger is functional again
            cert_gen.generate_self_signed_cert(cert_file=CERT_FILE, key_file=KEY_FILE)
        else:
            logger.warning("Credentials already generated!")

        context = (CERT_FILE, KEY_FILE)
        socketio.run(
            app_restapi,
            debug=False,
            host="0.0.0.0",
            port=8443,
            ssl_context=context,
            use_reloader=False,
            log_output=False,
            allow_unsafe_werkzeug=True,
        )

    except FileNotFoundError:
        # logger.error("Could not find SSL credentials! %s", e)
        pass
    except ssl.SSLError:
        # logger.error("SSL credentials FAIL! %s", e)
        pass
    except KeyboardInterrupt:
        # logger.info("HTTP server stopped by KeyboardInterrupt")
        pass
    finally:
        logger.info("Runtime manager stopped")
        runtime_manager.stop()


if __name__ == "__main__":
    run_https()
