/**
 * @file s7comm_plugin.cpp
 * @brief S7Comm Plugin Implementation for OpenPLC Runtime v4
 *
 * This plugin implements a Siemens S7 communication server using the Snap7 library.
 * It allows S7-compatible HMIs and SCADA systems to read/write OpenPLC I/O buffers.
 *
 * Phase 2 Implementation:
 * - JSON configuration parsing with cJSON
 * - Dynamic data block allocation based on config
 * - Configurable server parameters
 * - PLC identity configuration
 * - All areas mapped from configuration file
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <pthread.h>

/* Snap7 includes */
#include "snap7_libmain.h"
#include "s7_types.h"

/* Plugin includes */
extern "C" {
#include "plugin_logger.h"
#include "plugin_types.h"
#include "s7comm_plugin.h"
#include "s7comm_config.h"
}

/*
 * =============================================================================
 * Constants
 * =============================================================================
 */
#define S7COMM_MAX_DB_SIZE  65536   /* Maximum size for a single DB buffer */

/*
 * =============================================================================
 * Data Block Runtime Structure
 * =============================================================================
 */
typedef struct {
    int db_number;                  /* S7 DB number */
    s7comm_buffer_type_t type;      /* Mapping type */
    int start_buffer;               /* Starting buffer index */
    int size_bytes;                 /* Size in bytes */
    bool bit_addressing;            /* Bit-level access enabled */
    uint8_t *buffer;                /* Allocated data buffer */
} s7comm_db_runtime_t;

/*
 * =============================================================================
 * Plugin State
 * =============================================================================
 */
static plugin_logger_t g_logger;
static plugin_runtime_args_t g_runtime_args;
static s7comm_config_t g_config;
static bool g_initialized = false;
static bool g_running = false;
static bool g_config_loaded = false;

/* Snap7 server handle (S7Object is uintptr_t, use 0 for null) */
static S7Object g_server = 0;

/* Runtime data blocks (dynamically allocated based on config) */
static s7comm_db_runtime_t g_db_runtime[S7COMM_MAX_DATA_BLOCKS];
static int g_num_db_runtime = 0;

/* System area buffers (dynamically allocated based on config) */
static uint8_t *g_pe_area = NULL;
static uint8_t *g_pa_area = NULL;
static uint8_t *g_mk_area = NULL;

/*
 * =============================================================================
 * Forward Declarations
 * =============================================================================
 */
static void s7comm_event_callback(void *usrPtr, PSrvEvent PEvent, int Size);
static void sync_openplc_to_s7(void);
static void sync_s7_to_openplc(void);
static int allocate_data_blocks(void);
static void free_data_blocks(void);
static int register_all_areas(void);

/*
 * =============================================================================
 * Endianness Conversion Helpers
 * S7 protocol uses big-endian (network byte order)
 * =============================================================================
 */
static inline uint16_t swap16(uint16_t val)
{
    return ((val & 0xFF00) >> 8) | ((val & 0x00FF) << 8);
}

static inline uint32_t swap32(uint32_t val)
{
    return ((val & 0xFF000000) >> 24) |
           ((val & 0x00FF0000) >> 8)  |
           ((val & 0x0000FF00) << 8)  |
           ((val & 0x000000FF) << 24);
}

static inline uint64_t swap64(uint64_t val)
{
    return ((val & 0xFF00000000000000ULL) >> 56) |
           ((val & 0x00FF000000000000ULL) >> 40) |
           ((val & 0x0000FF0000000000ULL) >> 24) |
           ((val & 0x000000FF00000000ULL) >> 8)  |
           ((val & 0x00000000FF000000ULL) << 8)  |
           ((val & 0x0000000000FF0000ULL) << 24) |
           ((val & 0x000000000000FF00ULL) << 40) |
           ((val & 0x00000000000000FFULL) << 56);
}

/*
 * =============================================================================
 * Memory Management
 * =============================================================================
 */

/**
 * @brief Allocate data block buffers based on configuration
 */
static int allocate_data_blocks(void)
{
    g_num_db_runtime = 0;

    /* Allocate system areas */
    if (g_config.pe_area.enabled && g_config.pe_area.size_bytes > 0) {
        g_pe_area = (uint8_t *)calloc(1, g_config.pe_area.size_bytes);
        if (g_pe_area == NULL) {
            plugin_logger_error(&g_logger, "Failed to allocate PE area buffer");
            return -1;
        }
    }

    if (g_config.pa_area.enabled && g_config.pa_area.size_bytes > 0) {
        g_pa_area = (uint8_t *)calloc(1, g_config.pa_area.size_bytes);
        if (g_pa_area == NULL) {
            plugin_logger_error(&g_logger, "Failed to allocate PA area buffer");
            return -1;
        }
    }

    if (g_config.mk_area.enabled && g_config.mk_area.size_bytes > 0) {
        g_mk_area = (uint8_t *)calloc(1, g_config.mk_area.size_bytes);
        if (g_mk_area == NULL) {
            plugin_logger_error(&g_logger, "Failed to allocate MK area buffer");
            return -1;
        }
    }

    /* Allocate data blocks */
    for (int i = 0; i < g_config.num_data_blocks; i++) {
        const s7comm_data_block_t *db_cfg = &g_config.data_blocks[i];

        if (db_cfg->size_bytes <= 0 || db_cfg->size_bytes > S7COMM_MAX_DB_SIZE) {
            plugin_logger_warn(&g_logger, "DB%d: invalid size %d, skipping",
                              db_cfg->db_number, db_cfg->size_bytes);
            continue;
        }

        s7comm_db_runtime_t *db_rt = &g_db_runtime[g_num_db_runtime];
        db_rt->db_number = db_cfg->db_number;
        db_rt->type = db_cfg->mapping.type;
        db_rt->start_buffer = db_cfg->mapping.start_buffer;
        db_rt->size_bytes = db_cfg->size_bytes;
        db_rt->bit_addressing = db_cfg->mapping.bit_addressing;

        db_rt->buffer = (uint8_t *)calloc(1, db_cfg->size_bytes);
        if (db_rt->buffer == NULL) {
            plugin_logger_error(&g_logger, "Failed to allocate DB%d buffer", db_cfg->db_number);
            return -1;
        }

        g_num_db_runtime++;
        plugin_logger_debug(&g_logger, "Allocated DB%d: %d bytes, type=%s, start=%d",
                           db_cfg->db_number, db_cfg->size_bytes,
                           s7comm_buffer_type_name(db_cfg->mapping.type),
                           db_cfg->mapping.start_buffer);
    }

    return 0;
}

/**
 * @brief Free all allocated data block buffers
 */
static void free_data_blocks(void)
{
    /* Free system areas */
    if (g_pe_area != NULL) {
        free(g_pe_area);
        g_pe_area = NULL;
    }
    if (g_pa_area != NULL) {
        free(g_pa_area);
        g_pa_area = NULL;
    }
    if (g_mk_area != NULL) {
        free(g_mk_area);
        g_mk_area = NULL;
    }

    /* Free data blocks */
    for (int i = 0; i < g_num_db_runtime; i++) {
        if (g_db_runtime[i].buffer != NULL) {
            free(g_db_runtime[i].buffer);
            g_db_runtime[i].buffer = NULL;
        }
    }
    g_num_db_runtime = 0;
}

/**
 * @brief Register all areas with the Snap7 server
 */
static int register_all_areas(void)
{
    int result;

    /* Register system areas */
    if (g_config.pe_area.enabled && g_pe_area != NULL) {
        result = Srv_RegisterArea(g_server, srvAreaPE, 0, g_pe_area, g_config.pe_area.size_bytes);
        if (result != 0) {
            plugin_logger_warn(&g_logger, "Failed to register PE area: 0x%08X", result);
        } else {
            plugin_logger_debug(&g_logger, "Registered PE area: %d bytes", g_config.pe_area.size_bytes);
        }
    }

    if (g_config.pa_area.enabled && g_pa_area != NULL) {
        result = Srv_RegisterArea(g_server, srvAreaPA, 0, g_pa_area, g_config.pa_area.size_bytes);
        if (result != 0) {
            plugin_logger_warn(&g_logger, "Failed to register PA area: 0x%08X", result);
        } else {
            plugin_logger_debug(&g_logger, "Registered PA area: %d bytes", g_config.pa_area.size_bytes);
        }
    }

    if (g_config.mk_area.enabled && g_mk_area != NULL) {
        result = Srv_RegisterArea(g_server, srvAreaMK, 0, g_mk_area, g_config.mk_area.size_bytes);
        if (result != 0) {
            plugin_logger_warn(&g_logger, "Failed to register MK area: 0x%08X", result);
        } else {
            plugin_logger_debug(&g_logger, "Registered MK area: %d bytes", g_config.mk_area.size_bytes);
        }
    }

    /* Register data blocks */
    for (int i = 0; i < g_num_db_runtime; i++) {
        s7comm_db_runtime_t *db = &g_db_runtime[i];
        result = Srv_RegisterArea(g_server, srvAreaDB, db->db_number, db->buffer, db->size_bytes);
        if (result != 0) {
            plugin_logger_warn(&g_logger, "Failed to register DB%d: 0x%08X", db->db_number, result);
        } else {
            plugin_logger_debug(&g_logger, "Registered DB%d: %d bytes", db->db_number, db->size_bytes);
        }
    }

    return 0;
}

/*
 * =============================================================================
 * Plugin Lifecycle Functions
 * =============================================================================
 */

/**
 * @brief Initialize the S7Comm plugin
 */
extern "C" int init(void *args)
{
    /* Initialize logger first (before we have runtime_args) */
    plugin_logger_init(&g_logger, "S7COMM", NULL);
    plugin_logger_info(&g_logger, "Initializing S7Comm plugin...");

    if (!args) {
        plugin_logger_error(&g_logger, "init args is NULL");
        return -1;
    }

    /* Copy runtime args (critical - pointer is freed after init returns) */
    memcpy(&g_runtime_args, args, sizeof(plugin_runtime_args_t));

    /* Re-initialize logger with runtime_args for central logging */
    plugin_logger_init(&g_logger, "S7COMM", args);

    plugin_logger_info(&g_logger, "Buffer size: %d", g_runtime_args.buffer_size);

    /* Parse configuration file */
    const char *config_path = g_runtime_args.plugin_specific_config_file_path;
    if (config_path == NULL || config_path[0] == '\0') {
        plugin_logger_warn(&g_logger, "No config file specified, using defaults");
        s7comm_config_init_defaults(&g_config);
    } else {
        plugin_logger_info(&g_logger, "Loading config: %s", config_path);
        int result = s7comm_config_parse(config_path, &g_config);
        if (result != 0) {
            plugin_logger_error(&g_logger, "Failed to parse config file (error %d)", result);
            plugin_logger_warn(&g_logger, "Using default configuration");
            s7comm_config_init_defaults(&g_config);
        } else {
            plugin_logger_info(&g_logger, "Configuration loaded successfully");
            g_config_loaded = true;
        }
    }

    /* Check if server is enabled */
    if (!g_config.enabled) {
        plugin_logger_info(&g_logger, "S7Comm server is disabled in configuration");
        g_initialized = true;
        return 0;
    }

    /* Log configuration summary */
    plugin_logger_info(&g_logger, "Server config: port=%d, max_clients=%d, pdu_size=%d",
                       g_config.port, g_config.max_clients, g_config.pdu_size);
    plugin_logger_info(&g_logger, "PLC identity: %s (%s)", g_config.identity.name, g_config.identity.module_type);
    plugin_logger_info(&g_logger, "Data blocks configured: %d", g_config.num_data_blocks);

    /* Allocate data block buffers */
    if (allocate_data_blocks() != 0) {
        plugin_logger_error(&g_logger, "Failed to allocate data block buffers");
        free_data_blocks();
        return -1;
    }

    /* Create Snap7 server */
    g_server = Srv_Create();
    if (g_server == 0) {
        plugin_logger_error(&g_logger, "Failed to create Snap7 server");
        free_data_blocks();
        return -1;
    }

    /* Configure server parameters from config */
    uint16_t port = g_config.port;
    int max_clients = g_config.max_clients;
    int work_interval = g_config.work_interval_ms;
    int send_timeout = g_config.send_timeout_ms;
    int recv_timeout = g_config.recv_timeout_ms;
    int ping_timeout = g_config.ping_timeout_ms;
    int pdu_size = g_config.pdu_size;

    Srv_SetParam(g_server, p_u16_LocalPort, &port);
    Srv_SetParam(g_server, p_i32_MaxClients, &max_clients);
    Srv_SetParam(g_server, p_i32_WorkInterval, &work_interval);
    Srv_SetParam(g_server, p_i32_SendTimeout, &send_timeout);
    Srv_SetParam(g_server, p_i32_RecvTimeout, &recv_timeout);
    Srv_SetParam(g_server, p_i32_PingTimeout, &ping_timeout);
    Srv_SetParam(g_server, p_i32_PDURequest, &pdu_size);

    /* Set event mask based on logging configuration */
    longword event_mask = 0;
    if (g_config.logging.log_connections) {
        event_mask |= evcServerStarted | evcServerStopped |
                      evcClientAdded | evcClientDisconnected | evcClientRejected;
    }
    if (g_config.logging.log_errors) {
        event_mask |= evcListenerCannotStart | evcClientException;
    }
    if (g_config.logging.log_data_access) {
        event_mask |= evcDataRead | evcDataWrite;
    }
    Srv_SetMask(g_server, mkEvent, event_mask);

    /* Set event callback for logging */
    Srv_SetEventsCallback(g_server, s7comm_event_callback, NULL);

    /* Register all areas with the server */
    register_all_areas();

    g_initialized = true;
    plugin_logger_info(&g_logger, "S7Comm plugin initialized successfully");

    /* Log registered areas summary */
    if (g_config.pe_area.enabled) {
        plugin_logger_info(&g_logger, "PE area: %d bytes -> %s",
                          g_config.pe_area.size_bytes,
                          s7comm_buffer_type_name(g_config.pe_area.mapping.type));
    }
    if (g_config.pa_area.enabled) {
        plugin_logger_info(&g_logger, "PA area: %d bytes -> %s",
                          g_config.pa_area.size_bytes,
                          s7comm_buffer_type_name(g_config.pa_area.mapping.type));
    }
    if (g_config.mk_area.enabled) {
        plugin_logger_info(&g_logger, "MK area: %d bytes -> %s",
                          g_config.mk_area.size_bytes,
                          s7comm_buffer_type_name(g_config.mk_area.mapping.type));
    }
    for (int i = 0; i < g_num_db_runtime; i++) {
        plugin_logger_info(&g_logger, "DB%d: %d bytes -> %s[%d]",
                          g_db_runtime[i].db_number,
                          g_db_runtime[i].size_bytes,
                          s7comm_buffer_type_name(g_db_runtime[i].type),
                          g_db_runtime[i].start_buffer);
    }

    return 0;
}

/**
 * @brief Start the S7 server
 */
extern "C" void start_loop(void)
{
    if (!g_initialized) {
        plugin_logger_error(&g_logger, "Cannot start - plugin not initialized");
        return;
    }

    if (!g_config.enabled) {
        plugin_logger_info(&g_logger, "S7 server disabled in configuration");
        return;
    }

    if (g_running) {
        plugin_logger_warn(&g_logger, "Server already running");
        return;
    }

    plugin_logger_info(&g_logger, "Starting S7 server on %s:%d...",
                       g_config.bind_address, g_config.port);

    /* Start the server */
    int result;
    if (strcmp(g_config.bind_address, "0.0.0.0") == 0) {
        result = Srv_Start(g_server);
    } else {
        result = Srv_StartTo(g_server, g_config.bind_address);
    }

    if (result != 0) {
        plugin_logger_error(&g_logger, "Failed to start S7 server: 0x%08X", result);
        if (g_config.port < 1024) {
            plugin_logger_error(&g_logger, "Note: Port %d requires root privileges on Linux", g_config.port);
        }
        return;
    }

    g_running = true;
    plugin_logger_info(&g_logger, "S7 server started successfully");
}

/**
 * @brief Stop the S7 server
 */
extern "C" void stop_loop(void)
{
    if (!g_running) {
        plugin_logger_debug(&g_logger, "Server already stopped");
        return;
    }

    plugin_logger_info(&g_logger, "Stopping S7 server...");

    Srv_Stop(g_server);
    g_running = false;

    plugin_logger_info(&g_logger, "S7 server stopped");
}

/**
 * @brief Cleanup plugin resources
 */
extern "C" void cleanup(void)
{
    plugin_logger_info(&g_logger, "Cleaning up S7Comm plugin...");

    if (g_running) {
        stop_loop();
    }

    if (g_server != 0) {
        Srv_Destroy(g_server);
        g_server = 0;
    }

    free_data_blocks();

    g_initialized = false;
    g_config_loaded = false;
    plugin_logger_info(&g_logger, "S7Comm plugin cleanup complete");
}

/**
 * @brief Called at the start of each PLC scan cycle
 *
 * Synchronizes OpenPLC input buffers to S7 data areas.
 * Called with buffer mutex already held by PLC cycle manager.
 */
extern "C" void cycle_start(void)
{
    if (!g_initialized || !g_running || !g_config.enabled) {
        return;
    }

    /* Sync OpenPLC inputs to S7 buffers */
    sync_openplc_to_s7();
}

/**
 * @brief Called at the end of each PLC scan cycle
 *
 * Synchronizes S7 data areas to OpenPLC output buffers.
 * Called with buffer mutex already held by PLC cycle manager.
 */
extern "C" void cycle_end(void)
{
    if (!g_initialized || !g_running || !g_config.enabled) {
        return;
    }

    /* Sync S7 buffers to OpenPLC outputs */
    sync_s7_to_openplc();
}

/*
 * =============================================================================
 * Snap7 Callbacks
 * =============================================================================
 */

/**
 * @brief Snap7 event callback for logging connections and errors
 */
static void s7comm_event_callback(void *usrPtr, PSrvEvent PEvent, int Size)
{
    (void)usrPtr;
    (void)Size;

    switch (PEvent->EvtCode) {
        case evcServerStarted:
            plugin_logger_info(&g_logger, "S7 server started");
            break;
        case evcServerStopped:
            plugin_logger_info(&g_logger, "S7 server stopped");
            break;
        case evcClientAdded:
            if (g_config.logging.log_connections) {
                plugin_logger_info(&g_logger, "Client connected (ID: %d)", PEvent->EvtSender);
            }
            break;
        case evcClientDisconnected:
            if (g_config.logging.log_connections) {
                plugin_logger_info(&g_logger, "Client disconnected (ID: %d)", PEvent->EvtSender);
            }
            break;
        case evcClientRejected:
            plugin_logger_warn(&g_logger, "Client rejected (ID: %d)", PEvent->EvtSender);
            break;
        case evcListenerCannotStart:
            plugin_logger_error(&g_logger, "Listener cannot start - port may be in use or requires root");
            break;
        case evcClientException:
            if (g_config.logging.log_errors) {
                plugin_logger_warn(&g_logger, "Client exception (ID: %d)", PEvent->EvtSender);
            }
            break;
        case evcDataRead:
            if (g_config.logging.log_data_access) {
                plugin_logger_debug(&g_logger, "Data read by client %d", PEvent->EvtSender);
            }
            break;
        case evcDataWrite:
            if (g_config.logging.log_data_access) {
                plugin_logger_debug(&g_logger, "Data write by client %d", PEvent->EvtSender);
            }
            break;
        default:
            /* Ignore other events */
            break;
    }
}

/*
 * =============================================================================
 * Buffer Synchronization Functions
 * =============================================================================
 */

/**
 * @brief Sync a bool buffer to S7 byte array
 */
static void sync_bool_to_s7(uint8_t *s7_buf, int s7_size, s7comm_buffer_type_t type, int start_buffer)
{
    IEC_BOOL *(*buffer)[8] = NULL;

    switch (type) {
        case BUFFER_TYPE_BOOL_INPUT:
            buffer = g_runtime_args.bool_input;
            break;
        case BUFFER_TYPE_BOOL_OUTPUT:
            buffer = g_runtime_args.bool_output;
            break;
        case BUFFER_TYPE_BOOL_MEMORY:
            buffer = g_runtime_args.bool_memory;
            break;
        default:
            return;
    }

    int max_bytes = g_runtime_args.buffer_size - start_buffer;
    if (max_bytes > s7_size) max_bytes = s7_size;

    for (int byte_idx = 0; byte_idx < max_bytes; byte_idx++) {
        uint8_t byte_val = 0;
        int plc_idx = start_buffer + byte_idx;
        for (int bit_idx = 0; bit_idx < 8; bit_idx++) {
            IEC_BOOL *ptr = buffer[plc_idx][bit_idx];
            if (ptr != NULL && *ptr) {
                byte_val |= (1 << bit_idx);
            }
        }
        s7_buf[byte_idx] = byte_val;
    }
}

/**
 * @brief Sync S7 byte array to bool buffer
 */
static void sync_s7_to_bool(uint8_t *s7_buf, int s7_size, s7comm_buffer_type_t type, int start_buffer)
{
    IEC_BOOL *(*buffer)[8] = NULL;

    switch (type) {
        case BUFFER_TYPE_BOOL_OUTPUT:
            buffer = g_runtime_args.bool_output;
            break;
        case BUFFER_TYPE_BOOL_MEMORY:
            buffer = g_runtime_args.bool_memory;
            break;
        default:
            return; /* Don't write to inputs */
    }

    int max_bytes = g_runtime_args.buffer_size - start_buffer;
    if (max_bytes > s7_size) max_bytes = s7_size;

    for (int byte_idx = 0; byte_idx < max_bytes; byte_idx++) {
        uint8_t byte_val = s7_buf[byte_idx];
        int plc_idx = start_buffer + byte_idx;
        for (int bit_idx = 0; bit_idx < 8; bit_idx++) {
            IEC_BOOL *ptr = buffer[plc_idx][bit_idx];
            if (ptr != NULL) {
                *ptr = (byte_val >> bit_idx) & 0x01;
            }
        }
    }
}

/**
 * @brief Sync int buffer to S7 word array (with big-endian conversion)
 */
static void sync_int_to_s7(uint8_t *s7_buf, int s7_size, s7comm_buffer_type_t type, int start_buffer)
{
    IEC_UINT **buffer = NULL;

    switch (type) {
        case BUFFER_TYPE_INT_INPUT:
            buffer = g_runtime_args.int_input;
            break;
        case BUFFER_TYPE_INT_OUTPUT:
            buffer = g_runtime_args.int_output;
            break;
        case BUFFER_TYPE_INT_MEMORY:
            buffer = g_runtime_args.int_memory;
            break;
        default:
            return;
    }

    uint16_t *s7_words = (uint16_t *)s7_buf;
    int num_words = s7_size / 2;
    int max_words = g_runtime_args.buffer_size - start_buffer;
    if (max_words > num_words) max_words = num_words;

    for (int i = 0; i < max_words; i++) {
        IEC_UINT *ptr = buffer[start_buffer + i];
        if (ptr != NULL) {
            s7_words[i] = swap16(*ptr);
        }
    }
}

/**
 * @brief Sync S7 word array to int buffer (with big-endian conversion)
 */
static void sync_s7_to_int(uint8_t *s7_buf, int s7_size, s7comm_buffer_type_t type, int start_buffer)
{
    IEC_UINT **buffer = NULL;

    switch (type) {
        case BUFFER_TYPE_INT_OUTPUT:
            buffer = g_runtime_args.int_output;
            break;
        case BUFFER_TYPE_INT_MEMORY:
            buffer = g_runtime_args.int_memory;
            break;
        default:
            return; /* Don't write to inputs */
    }

    uint16_t *s7_words = (uint16_t *)s7_buf;
    int num_words = s7_size / 2;
    int max_words = g_runtime_args.buffer_size - start_buffer;
    if (max_words > num_words) max_words = num_words;

    for (int i = 0; i < max_words; i++) {
        IEC_UINT *ptr = buffer[start_buffer + i];
        if (ptr != NULL) {
            *ptr = swap16(s7_words[i]);
        }
    }
}

/**
 * @brief Sync dint buffer to S7 dword array (with big-endian conversion)
 */
static void sync_dint_to_s7(uint8_t *s7_buf, int s7_size, s7comm_buffer_type_t type, int start_buffer)
{
    IEC_UDINT **buffer = NULL;

    switch (type) {
        case BUFFER_TYPE_DINT_INPUT:
            buffer = g_runtime_args.dint_input;
            break;
        case BUFFER_TYPE_DINT_OUTPUT:
            buffer = g_runtime_args.dint_output;
            break;
        case BUFFER_TYPE_DINT_MEMORY:
            buffer = g_runtime_args.dint_memory;
            break;
        default:
            return;
    }

    uint32_t *s7_dwords = (uint32_t *)s7_buf;
    int num_dwords = s7_size / 4;
    int max_dwords = g_runtime_args.buffer_size - start_buffer;
    if (max_dwords > num_dwords) max_dwords = num_dwords;

    for (int i = 0; i < max_dwords; i++) {
        IEC_UDINT *ptr = buffer[start_buffer + i];
        if (ptr != NULL) {
            s7_dwords[i] = swap32(*ptr);
        }
    }
}

/**
 * @brief Sync S7 dword array to dint buffer (with big-endian conversion)
 */
static void sync_s7_to_dint(uint8_t *s7_buf, int s7_size, s7comm_buffer_type_t type, int start_buffer)
{
    IEC_UDINT **buffer = NULL;

    switch (type) {
        case BUFFER_TYPE_DINT_OUTPUT:
            buffer = g_runtime_args.dint_output;
            break;
        case BUFFER_TYPE_DINT_MEMORY:
            buffer = g_runtime_args.dint_memory;
            break;
        default:
            return; /* Don't write to inputs */
    }

    uint32_t *s7_dwords = (uint32_t *)s7_buf;
    int num_dwords = s7_size / 4;
    int max_dwords = g_runtime_args.buffer_size - start_buffer;
    if (max_dwords > num_dwords) max_dwords = num_dwords;

    for (int i = 0; i < max_dwords; i++) {
        IEC_UDINT *ptr = buffer[start_buffer + i];
        if (ptr != NULL) {
            *ptr = swap32(s7_dwords[i]);
        }
    }
}

/**
 * @brief Sync lint buffer to S7 lword array (with big-endian conversion)
 */
static void sync_lint_to_s7(uint8_t *s7_buf, int s7_size, s7comm_buffer_type_t type, int start_buffer)
{
    IEC_ULINT **buffer = NULL;

    switch (type) {
        case BUFFER_TYPE_LINT_INPUT:
            buffer = g_runtime_args.lint_input;
            break;
        case BUFFER_TYPE_LINT_OUTPUT:
            buffer = g_runtime_args.lint_output;
            break;
        case BUFFER_TYPE_LINT_MEMORY:
            buffer = g_runtime_args.lint_memory;
            break;
        default:
            return;
    }

    uint64_t *s7_lwords = (uint64_t *)s7_buf;
    int num_lwords = s7_size / 8;
    int max_lwords = g_runtime_args.buffer_size - start_buffer;
    if (max_lwords > num_lwords) max_lwords = num_lwords;

    for (int i = 0; i < max_lwords; i++) {
        IEC_ULINT *ptr = buffer[start_buffer + i];
        if (ptr != NULL) {
            s7_lwords[i] = swap64(*ptr);
        }
    }
}

/**
 * @brief Sync S7 lword array to lint buffer (with big-endian conversion)
 */
static void sync_s7_to_lint(uint8_t *s7_buf, int s7_size, s7comm_buffer_type_t type, int start_buffer)
{
    IEC_ULINT **buffer = NULL;

    switch (type) {
        case BUFFER_TYPE_LINT_OUTPUT:
            buffer = g_runtime_args.lint_output;
            break;
        case BUFFER_TYPE_LINT_MEMORY:
            buffer = g_runtime_args.lint_memory;
            break;
        default:
            return; /* Don't write to inputs */
    }

    uint64_t *s7_lwords = (uint64_t *)s7_buf;
    int num_lwords = s7_size / 8;
    int max_lwords = g_runtime_args.buffer_size - start_buffer;
    if (max_lwords > num_lwords) max_lwords = num_lwords;

    for (int i = 0; i < max_lwords; i++) {
        IEC_ULINT *ptr = buffer[start_buffer + i];
        if (ptr != NULL) {
            *ptr = swap64(s7_lwords[i]);
        }
    }
}

/**
 * @brief Dispatch sync from OpenPLC to S7 based on buffer type
 */
static void sync_buffer_to_s7(uint8_t *s7_buf, int s7_size, s7comm_buffer_type_t type, int start_buffer)
{
    switch (type) {
        case BUFFER_TYPE_BOOL_INPUT:
        case BUFFER_TYPE_BOOL_OUTPUT:
        case BUFFER_TYPE_BOOL_MEMORY:
            sync_bool_to_s7(s7_buf, s7_size, type, start_buffer);
            break;

        case BUFFER_TYPE_INT_INPUT:
        case BUFFER_TYPE_INT_OUTPUT:
        case BUFFER_TYPE_INT_MEMORY:
            sync_int_to_s7(s7_buf, s7_size, type, start_buffer);
            break;

        case BUFFER_TYPE_DINT_INPUT:
        case BUFFER_TYPE_DINT_OUTPUT:
        case BUFFER_TYPE_DINT_MEMORY:
            sync_dint_to_s7(s7_buf, s7_size, type, start_buffer);
            break;

        case BUFFER_TYPE_LINT_INPUT:
        case BUFFER_TYPE_LINT_OUTPUT:
        case BUFFER_TYPE_LINT_MEMORY:
            sync_lint_to_s7(s7_buf, s7_size, type, start_buffer);
            break;

        default:
            break;
    }
}

/**
 * @brief Dispatch sync from S7 to OpenPLC based on buffer type
 */
static void sync_s7_to_buffer(uint8_t *s7_buf, int s7_size, s7comm_buffer_type_t type, int start_buffer)
{
    switch (type) {
        case BUFFER_TYPE_BOOL_OUTPUT:
        case BUFFER_TYPE_BOOL_MEMORY:
            sync_s7_to_bool(s7_buf, s7_size, type, start_buffer);
            break;

        case BUFFER_TYPE_INT_OUTPUT:
        case BUFFER_TYPE_INT_MEMORY:
            sync_s7_to_int(s7_buf, s7_size, type, start_buffer);
            break;

        case BUFFER_TYPE_DINT_OUTPUT:
        case BUFFER_TYPE_DINT_MEMORY:
            sync_s7_to_dint(s7_buf, s7_size, type, start_buffer);
            break;

        case BUFFER_TYPE_LINT_OUTPUT:
        case BUFFER_TYPE_LINT_MEMORY:
            sync_s7_to_lint(s7_buf, s7_size, type, start_buffer);
            break;

        default:
            /* Don't write to input buffers */
            break;
    }
}

/**
 * @brief Sync OpenPLC buffers to S7 data areas
 *
 * Copies current OpenPLC input/output/memory values to S7 buffers
 * so S7 clients can read them.
 */
static void sync_openplc_to_s7(void)
{
    /* Sync system areas */
    if (g_config.pe_area.enabled && g_pe_area != NULL) {
        sync_buffer_to_s7(g_pe_area, g_config.pe_area.size_bytes,
                         g_config.pe_area.mapping.type,
                         g_config.pe_area.mapping.start_buffer);
    }

    if (g_config.pa_area.enabled && g_pa_area != NULL) {
        sync_buffer_to_s7(g_pa_area, g_config.pa_area.size_bytes,
                         g_config.pa_area.mapping.type,
                         g_config.pa_area.mapping.start_buffer);
    }

    if (g_config.mk_area.enabled && g_mk_area != NULL) {
        sync_buffer_to_s7(g_mk_area, g_config.mk_area.size_bytes,
                         g_config.mk_area.mapping.type,
                         g_config.mk_area.mapping.start_buffer);
    }

    /* Sync data blocks */
    for (int i = 0; i < g_num_db_runtime; i++) {
        s7comm_db_runtime_t *db = &g_db_runtime[i];
        sync_buffer_to_s7(db->buffer, db->size_bytes, db->type, db->start_buffer);
    }
}

/**
 * @brief Sync S7 data areas to OpenPLC buffers
 *
 * Copies values written by S7 clients back to OpenPLC output/memory buffers.
 */
static void sync_s7_to_openplc(void)
{
    /* Sync system areas (only outputs and markers) */
    if (g_config.pa_area.enabled && g_pa_area != NULL) {
        sync_s7_to_buffer(g_pa_area, g_config.pa_area.size_bytes,
                         g_config.pa_area.mapping.type,
                         g_config.pa_area.mapping.start_buffer);
    }

    if (g_config.mk_area.enabled && g_mk_area != NULL) {
        sync_s7_to_buffer(g_mk_area, g_config.mk_area.size_bytes,
                         g_config.mk_area.mapping.type,
                         g_config.mk_area.mapping.start_buffer);
    }

    /* Sync data blocks (only outputs and memory) */
    for (int i = 0; i < g_num_db_runtime; i++) {
        s7comm_db_runtime_t *db = &g_db_runtime[i];
        sync_s7_to_buffer(db->buffer, db->size_bytes, db->type, db->start_buffer);
    }
}
