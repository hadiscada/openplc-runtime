#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <pthread.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <errno.h>

#include "unix_socket.h"
#include "utils/log.h"

extern volatile sig_atomic_t keep_running;

// helper: read one line terminated by '\n' from a socket
static ssize_t read_line(int fd, char *buffer, size_t max_length)
{
    size_t total_read = 0;
    char ch;
    while (total_read < max_length - 1) 
    {
        ssize_t bytes_read = read(fd, &ch, 1);
        if (bytes_read <= 0) 
        {
            return bytes_read; // error or connection closed
        }
        if (ch == '\n') 
        {
            break; // end of line
        }
        buffer[total_read++] = ch;
    }
    buffer[total_read] = '\0'; // null-terminate the string
    return total_read;
}

void *unix_socket_thread(void *arg) 
{
    (void)arg;
    int *server_fd_pt = (int *)arg;
    if (server_fd_pt == NULL) 
    {
        log_error("Server file descriptor is NULL");
        return NULL;
    }
    
    int server_fd = *server_fd_pt;
    if (server_fd < 0) 
    {
        log_error("Failed to set up UNIX socket");
        return NULL;
    }

    while (keep_running)
    {
        handle_unix_socket_commands(server_fd);
    }

    close_unix_socket(server_fd);
    return NULL;
}

int setup_unix_socket()
{
    int server_fd;
    struct sockaddr_un address;

    // Remove any existing socket file
    unlink(SOCKET_PATH);

    // Create socket
    if ((server_fd = socket(AF_UNIX, SOCK_STREAM, 0)) < 0) 
    {
        log_error("Socket creation failed: %s", strerror(errno));
        return -1;
    }

    // Configure socket address structure
    memset(&address, 0, sizeof(address));
    address.sun_family = AF_UNIX;
    strncpy(address.sun_path, SOCKET_PATH, sizeof(address.sun_path) - 1);

    // Bind socket to the address
    if (bind(server_fd, (struct sockaddr *)&address, sizeof(address)) < 0) 
    {
        log_error("Socket bind failed: %s", strerror(errno));
        close(server_fd);
        return -1;
    }

    // Listen for incoming connections
    if (listen(server_fd, MAX_CLIENTS) < 0) 
    {
        log_error("Socket listen failed: %s", strerror(errno));
        close(server_fd);
        return -1;
    }

    log_info("UNIX socket server setup at %s", SOCKET_PATH);
    
    // Create a thread to handle socket commands
    pthread_t socket_thread;
    if (pthread_create(&socket_thread, NULL, unix_socket_thread, &server_fd) != 0) 
    {
        log_error("Failed to create UNIX socket thread: %s", strerror(errno));
        close(server_fd);
        return -1;
    }

    return 0;
}