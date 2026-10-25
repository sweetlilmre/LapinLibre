// SPDX-License-Identifier: MIT
/*
 * KarotzData: the application a karotzVM child runs, parsed from the
 * controller's command line, and the list of listeners its JavaScript
 * registered for unsolicited messages from the controller.
 */
#include "karotzvm.h"

KarotzData::KarotzData()
    : socket(-1), interactive_id(NULL), loop(NULL), listeners(NULL), options(NULL), descriptor(NULL)
{
}

/* The interactive id, the main loop and the listeners are not
 * freed. */
KarotzData::~KarotzData()
{
    delete descriptor;
}

/*
 * "<app> [<launch type> [<instance>]]", split at the first two
 * spaces; anything after a third space stays in the instance name. With no
 * space the whole line is the application name and the launch type and
 * instance stay empty.
 */
void KarotzData::set_cmdline(const char *line)
{
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "setCmdLine %s", line);
    cmdline = line;
    std::string::size_type first = cmdline.find(" ", 0);
    if (first == std::string::npos) {
        app_name = cmdline;
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "APP NAME %s", app_name.c_str());
        return;
    }
    app_name.assign(cmdline, 0, first);
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "APP NAME %s", app_name.c_str());
    std::string::size_type second = cmdline.find(" ", first + 1);
    if (second == std::string::npos) {
        launch_type.assign(cmdline, first + 1, cmdline.length() - first - 1);
        g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "LAUNCH_TYPE %s", launch_type.c_str());
        return;
    }
    launch_type.assign(cmdline, first + 1, second - first - 1);
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "LAUNCH_TYPE %s", launch_type.c_str());
    instance_name.assign(cmdline, second + 1, cmdline.length() - second - 1);
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "INSTANCE_NAME %s", instance_name.c_str());
}

void KarotzData::add_listener(MessageListener *listener)
{
    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "adding listener: [%p]", (void *)listener);
    listeners = g_list_append(listeners, listener);
}

namespace {
struct Dispatch {
    const pm::PlatformMessage *msg;
    KarotzData *karotz;
};
}

/* A listener that declines further messages (on_message() returns
 * whether to keep it) is unlinked and deleted (g_list_foreach has already
 * fetched the next node). An exception a listener throws is reported here,
 * as a timer's is, and the application goes on (poll_controller() has
 * entered the application's context, which reporting needs). */
static void call_listener(gpointer item, gpointer data)
{
    MessageListener *listener = static_cast<MessageListener *>(item);
    Dispatch *dispatch = static_cast<Dispatch *>(data);
    v8::TryCatch try_catch;

    g_log(KVM_LOG, G_LOG_LEVEL_DEBUG, "calling listener: [%p]", (void *)listener);
    bool keep = listener->on_message(*dispatch->msg);
    if (try_catch.HasCaught())
        report_exception(try_catch);
    if (!keep) {
        dispatch->karotz->listeners = g_list_remove(dispatch->karotz->listeners, listener);
        delete listener;
    }
}

bool KarotzData::on_message(const pm::PlatformMessage &msg)
{
    Dispatch dispatch = {&msg, this};

    g_list_foreach(listeners, call_listener, &dispatch);
    return true; /* never looked at */
}
