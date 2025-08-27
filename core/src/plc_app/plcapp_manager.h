#ifndef PLUGIN_MANAGER_H
#define PLUGIN_MANAGER_H

#include <stdbool.h>

typedef struct PluginManager PluginManager;

// Create a plugin manager for a given .so path
PluginManager *plugin_manager_create(const char *so_path);

// Destroy the plugin manager and unload the library
void plugin_manager_destroy(PluginManager *pm);

// Ensure the library is loaded
bool plugin_manager_load(PluginManager *pm);

// Get a raw symbol (void*), you normally won’t call this directly
void *plugin_manager_get_symbol(PluginManager *pm, const char *symbol_name);

// Type-safe function getter
#define plugin_manager_get_func(pm, type, name) \
    ((type) plugin_manager_get_symbol((pm), (name)))

#endif // PLUGIN_MANAGER_H
