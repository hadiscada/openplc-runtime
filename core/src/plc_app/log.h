#ifndef LOG_H
#define LOG_H

#include <stdio.h>

typedef enum {
    LOG_LEVEL_DEBUG,
    LOG_LEVEL_INFO,
    LOG_LEVEL_WARN,
    LOG_LEVEL_ERROR
} LogLevel;

void log_set_level(LogLevel level);
void log_info(const char *fmt, ...);
void log_debug(const char *fmt, ...);
void log_warn(const char *fmt, ...);
void log_error(const char *fmt, ...);

#endif

