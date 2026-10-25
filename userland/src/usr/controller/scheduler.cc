// SPDX-License-Identifier: MIT
/*
 * The applications' schedules. Each is registered with the scheduler daemon
 * under a new UUID and remembered here as (apiKey, instance); when the
 * daemon fires it, the application is started (hosted) or the platform told.
 */
#include <map>
#include <signal.h>
#include <string.h>

#include "controller.h"

struct CompareStrings {
    bool operator()(const char *a, const char *b) const { return strcmp(a, b) < 0; }
};

/* UUID (new[]'ed) -> {apiKey, instance} (new[]'ed pair of strings). */
typedef std::map<char *, std::string *, CompareStrings> ScheduleMap;

static ScheduleMap schedules;

static std::string *new_entry(const char *api_key, const char *instance)
{
    std::string *entry = new std::string[2];

    if (api_key != NULL)
        entry[0] = api_key;
    if (instance != NULL)
        entry[1] = instance;
    return entry;
}

static void delete_schedule(ScheduleMap::iterator it)
{
    delete[] it->first;
    delete[] it->second;
}

const std::string *scheduler_find(ControllerObject *controller, const char *uuid)
{
    (void)controller;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Looking up a schedule");
    ScheduleMap::iterator it = schedules.find((char *)uuid);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Schedule lookup done");
    return it == schedules.end() ? NULL : it->second;
}

/*
 * Start the scheduled application: hosted ones through the VM, others by a
 * ScheduleEvent (uuid = key, params = instance) to the platform. An
 * application missing from the configuration is logged and ignored.
 */
void scheduler_send_event(const std::string *entry, const char *uuid)
{
    (void)uuid;
    const pm::ConfigApp *app = controller_find_app(global_controller, entry[0].c_str());
    if (app == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "schedule for %s: not in the configuration", entry[0].c_str());
        return;
    }
    if (strcmp(app->deployment().c_str(), "hosted") == 0) {
        controller_run_app(global_controller, entry[0].c_str(), "SCHEDULER", entry[1].c_str(), NULL);
    } else {
        g_log(NULL, G_LOG_LEVEL_DEBUG, "send schedEvent to Platform");
        pm::PlatformMessage *msg = new_platform_message();
        pm::ScheduleEvent *event = msg->mutable_scheduleevent();
        event->set_uuid(entry[0].c_str());
        event->set_params(entry[1].c_str());
        platform_send(controller_socket(global_controller), msg);
        delete msg;
    }
}

void scheduler_remove(ControllerObject *controller, const char *uuid)
{
    (void)controller;
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Looking up a schedule");
    ScheduleMap::iterator it = schedules.find((char *)uuid);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "Schedule lookup done");
    if (it != schedules.end()) {
        delete_schedule(it);
        schedules.erase(it);
    }
}

/* Register one schedule with the scheduler daemon (periodic, weekly or date). */
static gboolean register_schedule(ControllerObject *controller, const pm::ScheduleConf::Schedule &schedule,
                                  const char *uuid)
{
    gboolean ok = FALSE;

    if (schedule.has_scheduleperiodic()) {
        const pm::ScheduleConf::Schedule::SchedulePeriodic &periodic = schedule.scheduleperiodic();
        ok = schedulerAddPeriodicEntry(controller->scheduler, uuid, periodic.period(), periodic.delta());
    } else if (schedule.has_scheduleweekly()) {
        const pm::ScheduleConf::Schedule::ScheduleWeekly &weekly = schedule.scheduleweekly();
        /* Sunday is 0 for scheduler-daemon; 7 is taken for it too, as in the wake-up times. */
        int day = weekly.dayweek() == 7 ? 0 : weekly.dayweek();
        ok = schedulerAddWeeklyEntry(controller->scheduler, uuid, day, weekly.hour(), weekly.minute(),
                                     weekly.second());
    } else if (schedule.has_scheduledate()) {
        const pm::ScheduleConf::Schedule::ScheduleDate &date = schedule.scheduledate();
        ok = schedulerAddDateEntry(controller->scheduler, uuid, date.year(), date.month(), date.day(), date.hour(),
                                   date.minute(), date.second());
    }
    return ok;
}

/* The new schedule's UUID, or NULL (the UUID is then freed). */
char *scheduler_add_app(ControllerObject *controller, const pm::ScheduleConf::ScheduleApp &app)
{
    char *uuid = new char[37];

    generate_lower_uuid(uuid);
    if (!register_schedule(controller, app.schedule(), uuid)) {
        delete[] uuid;
        return NULL;
    }
    schedules.insert(std::make_pair(uuid, new_entry(app.apikey().c_str(), app.instance().c_str())));
    return uuid;
}
