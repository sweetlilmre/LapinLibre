// SPDX-License-Identifier: MIT
/*
 * The connection to the platform (ip_server:port_server; port 9123 by
 * default, and none while ip_server is this rabbit) and the controller's
 * own server on the same port, where interactive applications connect. Each message is a
 * PlatformMessage (platform-message.proto) preceded by its length (4 bytes,
 * network order). Sockets are polled from idle callbacks.
 */
#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include <uuid/uuid.h>
#include <vector>

#include <google/protobuf/descriptor.h>

#include "controller.h"

/* ---- messages ---------------------------------------------------------------- */

/* A new lower-case UUID (37 bytes with the terminator). */
void generate_lower_uuid(char *out)
{
    uuid_t uuid;

    uuid_generate(uuid);
    uuid_unparse(uuid, out);
    for (int i = 0; out[i] != '\0'; i++)
        out[i] = tolower((unsigned char)out[i]);
}

/* A message with a fresh id. */
pm::PlatformMessage *new_platform_message(void)
{
    char id[37];

    generate_lower_uuid(id);
    pm::PlatformMessage *msg = new pm::PlatformMessage;
    msg->set_id(id);
    return msg;
}

/* The answer to `request`: its id as correlation id, the same interactive id. */
pm::PlatformMessage *new_response(const pm::PlatformMessage *request, pm::ReturnCode code, const char *description)
{
    pm::PlatformMessage *msg = new_platform_message();

    if (request->has_interactiveid())
        msg->set_interactiveid(request->interactiveid());
    msg->set_correlationid(request->id());
    pm::Response *response = msg->mutable_response();
    response->set_code(code);
    if (description != NULL)
        response->set_description(description);
    return msg;
}

/* Errors are ignored; every message but a ping is logged. */
void platform_send(int sock, const pm::PlatformMessage *msg)
{
    uint32_t size = msg->ByteSize();
    uint32_t length;
    char *buffer = (char *)malloc(size + 4);

    length = htonl(size);
    memcpy(buffer, &length, 4);
    msg->SerializeToArray(buffer + 4, size);
    send(sock, buffer, size + 4, 0);
    if (msg->has_ping() != true)
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Send MSG\n%s", msg->DebugString().c_str());
    free(buffer);
}

/* The names of the fields a message has, but its id and correlation id ("none" if no other). */
static std::string message_fields(const pm::PlatformMessage &msg)
{
    std::vector<const google::protobuf::FieldDescriptor *> fields;
    std::string names;

    msg.GetReflection()->ListFields(msg, &fields);
    for (size_t i = 0; i < fields.size(); i++) {
        if (fields[i]->number() == pm::PlatformMessage::kIdFieldNumber ||
            fields[i]->number() == pm::PlatformMessage::kCorrelationIdFieldNumber)
            continue;
        names += (names.empty() ? "" : ", ") + fields[i]->name();
    }
    return names.empty() ? std::string("none") : names;
}

/*
 * What a message is, for the log, without its values (they can hold
 * credentials): its id and correlation id, and the names of the other
 * fields it has.
 */
static std::string message_trace(const pm::PlatformMessage &msg)
{
    return "id \"" + msg.id() + "\", correlationId \"" + msg.correlationid() + "\", fields: " + message_fields(msg);
}

/* Why a message didn't parse, for its sender: the missing fields, or malformed bytes; and what it had. */
static std::string malformed_description(const pm::PlatformMessage &msg, const char *what)
{
    std::string missing = msg.InitializationErrorString();
    return std::string(what) + " (" + (missing.empty() ? std::string("malformed bytes") : "missing: " + missing) +
           "; fields: " + message_fields(msg) + ")";
}

/*
 * Parse a received message. One that doesn't parse (malformed bytes, or a
 * required field missing) is logged with what can trace it back, and
 * FALSE is returned: it must not be acted on, since what did parse is
 * incomplete and missing fields read as their defaults. `msg` then holds
 * what parsed.
 */
static gboolean parse_platform_message(const std::string &bytes, pm::PlatformMessage *msg)
{
    if (msg->ParseFromString(bytes)) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "message received:\n%s", msg->DebugString().c_str());
        return TRUE;
    }
    g_log(NULL, G_LOG_LEVEL_WARNING, "malformed message (%d bytes) not handled: %s; missing: %s", (int)bytes.size(),
          message_trace(*msg).c_str(), msg->InitializationErrorString().c_str());
    return FALSE;
}

/* Send `to` a Response ERROR with this description, correlated to `correlation_id` unless it is NULL. */
static void send_error(int to, const pm::PlatformMessage &about, const char *correlation_id, const std::string &description)
{
    pm::PlatformMessage *reply = new_platform_message();

    if (about.has_interactiveid())
        reply->set_interactiveid(about.interactiveid());
    if (correlation_id != NULL)
        reply->set_correlationid(correlation_id);
    reply->mutable_response()->set_code(pm::ERROR);
    reply->mutable_response()->set_description(description);
    platform_send(to, reply);
    delete reply;
}

/*
 * The answers to a message that didn't parse, so nobody is left guessing.
 * The description says what was wrong and names the fields it had (no
 * values). Its sender gets ERROR on the connection it came in on:
 * correlated to its id when it has one; without an id it can't be matched
 * to a request, but it still says something sent was refused. When it was
 * a reply (a correlation id), the ERROR is correlated to the reply's own id;
 * and when it was a reply the controller relays to the interactive
 * application (a Karotz message response from the platform), the
 * application gets ERROR in its place, under the correlation id it waits
 * for, so its callback ends instead of waiting.
 */
static void answer_malformed(int sock, const pm::PlatformMessage &msg)
{
    if (!msg.has_correlationid()) {
        send_error(sock, msg, msg.has_id() ? msg.id().c_str() : NULL, malformed_description(msg, "malformed message"));
        return;
    }
    if (msg.has_id())
        send_error(sock, msg, msg.id().c_str(), malformed_description(msg, "malformed reply"));
    if (msg.has_karotzmessageresponse() && global_controller->status.karotz == E_STATUS_KAROTZ_INTERACTIVE &&
        sock != global_controller->interactive_socket)
        send_error(global_controller->interactive_socket, msg, msg.correlationid().c_str(),
                   malformed_description(msg, "malformed reply from the platform"));
}

/* One message's bytes (after its 4-byte length), or FALSE if the connection failed or closed. */
gboolean platform_receive_frame(int sock, std::string *bytes)
{
    uint32_t length;
    unsigned received = 0;
    ssize_t n;

    g_log(NULL, G_LOG_LEVEL_DEBUG, "reading a message on socket %d", sock);
    while (received < 4) {
        n = recv(sock, (char *)&length + received, 4 - received, 0);
        if (n < 0 && errno == EINTR)
            continue;
        if (n == 0) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "socket %d: the other side closed the connection", sock);
            return FALSE;
        }
        if (n < 0) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "Failed to receive bytes from server: %s", strerror(errno));
            return FALSE;
        }
        g_log(NULL, G_LOG_LEVEL_DEBUG, "received %d bytes", (int)n);
        received += n;
    }
    size_t size = ntohl(length);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "message size: %d", (int)size);
    bytes->resize(size);
    received = 0;
    while (received < size) {
        n = recv(sock, &(*bytes)[received], size - received, 0);
        if (n < 0 && errno == EINTR)
            continue;
        if (n == 0) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "socket %d: the other side closed the connection within a message", sock);
            return FALSE;
        }
        if (n < 0) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "Failed to receive bytes message from server: %s", strerror(errno));
            return FALSE;
        }
        received += n;
    }
    return TRUE;
}

/* ---- sockets ----------------------------------------------------------------- */

int platform_create_socket(void)
{
    int sock = socket(AF_INET, SOCK_STREAM, 0);

    if (sock < 0)
        g_error("cannot create socket: %s", strerror(errno));
    return sock;
}

/*
 * Connect with a 5 s timeout. Returns FALSE when the name can't be
 * resolved; any other failure makes the program exit (with status 0).
 */
gboolean connect_server(int sock, const char *host, uint16_t port)
{
    struct sockaddr_in address;
    struct hostent *entry;
    int flags;

    entry = gethostbyname(host);
    if (entry == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "nslookup failed on '%s': %s", host, hstrerror(h_errno));
        return FALSE;
    }
    memset(&address, 0, sizeof address);
    address.sin_family = AF_INET;
    memcpy(&address.sin_addr, entry->h_addr_list[0], 4);
    address.sin_port = htons(port);

    flags = fcntl(sock, F_GETFL, 0);
    if (flags < 0) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "connectServer Error fcntl(..., F_GETFL) (%s)\n", strerror(errno));
        exit(0);
    }
    if (fcntl(sock, F_SETFL, flags | O_NONBLOCK) < 0) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "connectServer Error fcntl(..., F_SETFL) (%s)\n", strerror(errno));
        exit(0);
    }
    if (connect(sock, (struct sockaddr *)&address, sizeof address) < 0) {
        if (errno != EINPROGRESS) {
            g_log(NULL, G_LOG_LEVEL_CRITICAL, "connectServer Error connecting %d - %s\n", errno, strerror(errno));
            exit(0);
        }
        g_log(NULL, G_LOG_LEVEL_DEBUG, "connectServer EINPROGRESS in connect() - selecting\n");
        struct timeval timeout = { 5, 0 };
        fd_set writable;
        FD_ZERO(&writable);
        FD_SET(sock, &writable);
        int ready = select(sock + 1, NULL, &writable, NULL, &timeout);
        if (ready < 0 && errno != EINTR) {
            g_log(NULL, G_LOG_LEVEL_CRITICAL, "connectServer Error connecting %d - %s\n", errno, strerror(errno));
            exit(0);
        }
        if (ready < 1) {
            g_log(NULL, G_LOG_LEVEL_CRITICAL, "connectServer Timeout in select() - Cancelling!\n");
            exit(0);
        }
        int error;
        socklen_t length = sizeof error;
        if (getsockopt(sock, SOL_SOCKET, SO_ERROR, &error, &length) < 0) {
            g_log(NULL, G_LOG_LEVEL_CRITICAL, "connectServer Error in getsockopt() %d - %s\n", errno,
                  strerror(errno));
            exit(0);
        }
        if (error != 0) {
            g_log(NULL, G_LOG_LEVEL_CRITICAL, "connectServer Error in delayed connection() %d - %s\n", error,
                  strerror(error));
            exit(0);
        }
    }
    flags = fcntl(sock, F_GETFL, 0);
    if (flags < 0) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "connectServer Error fcntl(..., F_GETFL) (%s)\n", strerror(errno));
        exit(0);
    }
    if (fcntl(sock, F_SETFL, flags & ~O_NONBLOCK) < 0) {
        g_log(NULL, G_LOG_LEVEL_CRITICAL, "connectServer Error fcntl(..., F_SETFL) (%s)\n", strerror(errno));
        exit(0);
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "connectServer connect done");
    return TRUE;
}

/*
 * TRUE when nothing can be read from the socket within a millisecond.
 * An interrupted select() counts as nothing read yet: a signal arriving
 * meanwhile, in practice the SIGCHLD of karotzVM or of a system() child
 * ending, must not kill the controller (it spends nearly all its time
 * here). Any other failure of select() is fatal.
 */
static gboolean socket_idle(int sock)
{
    fd_set readable;
    struct timeval timeout = { 0, 1000 };

    FD_ZERO(&readable);
    FD_SET(sock, &readable);
    if (select(sock + 1, &readable, NULL, NULL, &timeout) < 0) {
        if (errno == EINTR)
            return TRUE;
        g_error("Failed to select socket");
    }
    return !FD_ISSET(sock, &readable);
}

/*
 * Idle callback polling one socket (the platform's or an application's):
 * reads and handles a message when one is there (one that doesn't parse
 * is logged and answered, not handled; the connection stays open, its
 * length prefix keeping the stream in step). When the connection fails,
 * an application's interactive mode stops, and losing the platform makes
 * the network go offline and online again (which reconnects).
 */
gboolean platform_watch_socket(gpointer data)
{
    int *watched = (int *)data;
    int sock = *watched;
    int state = socket_idle(sock);

    if (state == 0) {
        if (global_controller->platform_socket == sock)
            g_log(NULL, G_LOG_LEVEL_DEBUG, "message coming from platform - socket %d", sock);
        else
            g_log(NULL, G_LOG_LEVEL_DEBUG, "message not coming from platform. sock: %d", sock);
        std::string bytes;
        if (!platform_receive_frame(sock, &bytes)) {
            state = -1;
        } else {
            pm::PlatformMessage msg;
            if (parse_platform_message(bytes, &msg))
                platform_dispatch(global_controller, sock, &msg);
            else
                answer_malformed(sock, msg);
        }
    }
    if (state != -1)
        return TRUE;
    g_log(NULL, G_LOG_LEVEL_WARNING, "socket error on socket %d", sock);
    if (global_controller->status.karotz == E_STATUS_KAROTZ_INTERACTIVE && global_controller->interactive_socket == sock) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "stop interactiveMode");
        controller_set_status(global_controller, E_STATUS_KAROTZ_INTERACTIVE_STOP);
    }
    if (global_controller->platform_socket == sock) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "sock error -> goes E_STATUS_NETWORK_OFFLINE");
        /* This watch ends by returning FALSE, so platform_close() mustn't remove it too. */
        global_controller->platform_watch = 0;
        controller_set_status(global_controller, E_STATUS_NETWORK_OFFLINE);
        controller_set_status(global_controller, E_STATUS_NETWORK_ONLINE);
    }
    return FALSE;
}

/*
 * Close the platform connection and stop polling it. The watch goes with
 * the socket: a watch left behind would poll the descriptor number the
 * next connection gets, and two watches reading one connection split its
 * messages between them. Does nothing without a connection.
 */
void platform_close(ControllerObject *controller)
{
    if (controller->platform_watch != 0) {
        g_source_remove(controller->platform_watch);
        controller->platform_watch = 0;
    }
    if (controller->platform_socket >= 0) {
        close(controller->platform_socket);
        controller->platform_socket = -1;
    }
}

/*
 * Connect to the platform. Attempts are throttled: the first three go
 * through, then one after 10 s, one after 30 s, then one a minute. Returns
 * FALSE when no connection was made.
 */
gboolean platform_connect(ControllerObject *controller)
{
    char default_port[10] = { 0 };
    char connection_id[37];
    time_t now = time(NULL);
    int elapsed = now - controller->last_connect_attempt;
    gboolean retry;

    if (controller->connect_attempts < 3)
        retry = TRUE;
    else if (controller->connect_attempts == 3 && elapsed > 9)
        retry = TRUE;
    else if (controller->connect_attempts == 4 && elapsed > 29)
        retry = TRUE;
    else if (controller->connect_attempts < 5 || elapsed < 60)
        retry = FALSE;
    else
        retry = TRUE;
    if (!retry) {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "Network - attempts: %d, seconds since last attempt: %d", controller->connect_attempts, elapsed);
        return FALSE;
    }

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Network - retry_connection");
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Network - controller_platform_connect");
    controller->connect_attempts++;
    controller->last_connect_attempt = now;
    sprintf(default_port, "%d", DEFAULT_PORT_SERVER);
    char *port_text = controller_get_config(controller, "port_server", default_port);
    int port = atoi(port_text);
    free(port_text);
    char *host = controller_get_config(controller, "ip_server", "127.0.0.1");
    /* The controller's own server listens on port_server on every address, so a platform on this
     * rabbit is the controller itself: no platform server is set (SERVER= in lapinlibre.txt). */
    if (strncmp(host, "127.", 4) == 0 || strcmp(host, "localhost") == 0) {
        g_log(NULL, controller->connect_attempts == 1 ? G_LOG_LEVEL_WARNING : G_LOG_LEVEL_DEBUG,
              "Platform - no platform server: ip_server is this rabbit (%s); set SERVER= in lapinlibre.txt", host);
        free(host);
        return FALSE;
    }
    int sock = platform_create_socket();
    gboolean connected = connect_server(sock, host, port);
    if (!connected) {
        close(sock); /* each attempt makes a new socket, so a failed one is closed */
        g_log(NULL, G_LOG_LEVEL_WARNING, "Platform - cannot connect to: %s:%d", host, port);
    } else {
        controller->platform_socket = sock;
        memset(connection_id, 0, sizeof connection_id);
        generate_lower_uuid(connection_id);
        if (controller->connection_id != NULL)
            free(controller->connection_id);
        controller->connection_id = strdup(connection_id);
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "Platform - successfully connected to: %s:%d", host, port);
        int *watched = (int *)malloc(sizeof *watched);
        *watched = sock;
        controller->platform_watch = g_idle_add_full(G_PRIORITY_DEFAULT_IDLE, platform_watch_socket, watched, free);
        controller_set_status(controller, E_STATUS_PLATFORM_ONLINE);
    }
    free(host);
    return connected;
}

/* ---- the controller's server ------------------------------------------------- */

/* An application connected: poll its socket like the platform's. The
 * connection is kept referenced for good. */
static gboolean server_incoming(GSocketService *service, GSocketConnection *connection, GObject *source,
                                gpointer data)
{
    (void)service;
    (void)source;
    (void)data;
    g_object_ref(connection);
    GSocketAddress *remote = g_socket_connection_get_remote_address(connection, NULL);
    GInetAddress *address = g_inet_socket_address_get_address(G_INET_SOCKET_ADDRESS(remote));
    guint16 port = g_inet_socket_address_get_port(G_INET_SOCKET_ADDRESS(remote));
    g_log(NULL, G_LOG_LEVEL_DEBUG, "New Connection from %s:%d\n", g_inet_address_to_string(address), port);
    int sock = g_socket_get_fd(g_socket_connection_get_socket(connection));
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Connection to socket %d", sock);
    int *watched = (int *)malloc(sizeof *watched);
    *watched = sock;
    g_idle_add_full(G_PRIORITY_DEFAULT_IDLE, platform_watch_socket, watched, free);
    return TRUE;
}

/* Listens on any address once started (when the network goes online). */
GSocketService *platform_bind_server(int port)
{
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Bind server %d", port);
    GSocketService *service = g_socket_service_new();
    GInetAddress *any = g_inet_address_new_any(G_SOCKET_FAMILY_IPV4);
    GSocketAddress *address = g_inet_socket_address_new(any, port & 0xffff);
    g_socket_listener_add_address(G_SOCKET_LISTENER(service), address, G_SOCKET_TYPE_STREAM, G_SOCKET_PROTOCOL_TCP,
                                  NULL, NULL, NULL);
    g_object_unref(address);
    g_object_unref(any);
    g_signal_connect(service, "incoming", G_CALLBACK(server_incoming), NULL);
    return service;
}
