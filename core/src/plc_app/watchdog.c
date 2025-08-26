#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <pthread.h>
#include <unistd.h>
#include <stdatomic.h>

extern atomic_long plc_heartbeat;

void* watchdog_thread(void* arg) {
    (void) arg;
    long last = atomic_load(&plc_heartbeat);

    while (1) {
        sleep(2); // Watch every 2 seconds

        long now = atomic_load(&plc_heartbeat);
        if (now == last) {
            fprintf(stderr, "[Watchdog] No heartbeat! PLC unresponsive.\n");
            exit(EXIT_FAILURE);
        }

        last = now;
    }

    return NULL;
}
