#include <dlfcn.h>
#include <stdlib.h>

#include "image_tables.h"
#include "log.h"
#include "utils/utils.h"


//Internal buffers for I/O and memory.
//Booleans
IEC_BOOL *bool_input[BUFFER_SIZE][8];
IEC_BOOL *bool_output[BUFFER_SIZE][8];

//Bytes
IEC_BYTE *byte_input[BUFFER_SIZE];
IEC_BYTE *byte_output[BUFFER_SIZE];

//Analog I/O
IEC_UINT *int_input[BUFFER_SIZE];
IEC_UINT *int_output[BUFFER_SIZE];

//32bit I/O
IEC_UDINT *dint_input[BUFFER_SIZE];
IEC_UDINT *dint_output[BUFFER_SIZE];

//64bit I/O
IEC_ULINT *lint_input[BUFFER_SIZE];
IEC_ULINT *lint_output[BUFFER_SIZE];

//Memory
IEC_UINT *int_memory[BUFFER_SIZE];
IEC_UDINT *dint_memory[BUFFER_SIZE];
IEC_ULINT *lint_memory[BUFFER_SIZE];

void (*ext_setBufferPointers)(IEC_BOOL *input_bool[BUFFER_SIZE][8], IEC_BOOL *output_bool[BUFFER_SIZE][8],
                              IEC_BYTE *input_byte[BUFFER_SIZE], IEC_BYTE *output_byte[BUFFER_SIZE],
                              IEC_UINT *input_int[BUFFER_SIZE], IEC_UINT *output_int[BUFFER_SIZE],
                              IEC_UDINT *input_dint[BUFFER_SIZE], IEC_UDINT *output_dint[BUFFER_SIZE],
                              IEC_ULINT *input_lint[BUFFER_SIZE], IEC_ULINT *output_lint[BUFFER_SIZE],
                              IEC_UINT *int_memory[BUFFER_SIZE], IEC_UDINT *dint_memory[BUFFER_SIZE], IEC_ULINT *lint_memory[BUFFER_SIZE]);
void (*ext_config_run__)(unsigned long tick);
void (*ext_config_init__)(void);
void (*ext_glueVars)(void);
void (*ext_updateTime)(void);

int symbols_init(void){
    char *error = dlerror();

    // find shared object file
    void *handle = dlopen(libplc_file, RTLD_LAZY);
    if (!handle)
    {
        log_error("dlopen failed: %s\n", dlerror());
        return -1;
    }

    // Clear any existing error
    dlerror();

    // Get pointer to external functions
    *(void **)(&ext_config_run__) = dlsym(handle, "config_run__");
    error = dlerror();
    if (error)
    {
        log_error("dlsym function error: %s\n", error);
        dlclose(handle);
        return -1;
    }

    *(void **)(&ext_config_init__) = dlsym(handle, "config_init__");
    error = dlerror();
    if (error)
    {
        log_error("dlsym function error: %s\n", error);
        dlclose(handle);
        return -1;
    }

    *(void **)(&ext_glueVars) = dlsym(handle, "glueVars");
    error = dlerror();
    if (error)
    {
        log_error("dlsym function error: %s\n", error);
        dlclose(handle);
        return -1;
    }

    *(void **)(&ext_updateTime) = dlsym(handle, "updateTime");
    error = dlerror();
    if (error)
    {
        log_error("dlsym function error: %s\n", error);
        dlclose(handle);
        return -1;
    }

    *(void **)(&ext_setBufferPointers) = dlsym(handle, "setBufferPointers");
    error = dlerror();
    if (error)
    {
        log_error("dlsym function error: %s\n", error);
        dlclose(handle);
        return -1;
    }

    *(void **)(&ext_common_ticktime__) = dlsym(handle, "common_ticktime__");
    error = dlerror();
    if (error)
    {
        log_error("dlsym function error: %s\n", error);
        dlclose(handle);
        return -1;
    }

    // Get pointer to variables in .so
    /*
    ext_bool_output = (IEC_BOOL *(*)[8])dlsym(handle, "bool_output");
    error = dlerror();
    if (error)
    {
        fprintf(stderr, "dlsym buffer error: %s\n", error);
        dlclose(handle);
        exit(1);
    }
    */

    // Send buffer pointers to .so
    ext_setBufferPointers(bool_input, bool_output,
        byte_input, byte_output,
        int_input, int_output,
        dint_input, dint_output,
        lint_input, lint_output,
        int_memory, dint_memory, lint_memory);

    return 0;
}
