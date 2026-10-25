// SPDX-License-Identifier: MIT
/*
 * The karotzVM process, which runs the JavaScript applications. The
 * controller starts it with a pipe on its standard input and its standard
 * output on /dev/null, and writes one command per line to it
 * ("<apikey> <trigger> <instance>").
 */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#include "controller.h"

typedef struct {
    int to_vm; /* the VM's standard input */
    pid_t pid;
} VirtualMachine;

static VirtualMachine *vm;
/*
 * Only the VM is reaped by the controller, through a GLib child watch on its pid:
 * GLib reaps that pid alone and calls vm_ended() in the main loop. system() children have no
 * watch and are system()'s to reap. (A SIGCHLD handler that called wait() for any child
 * deadlocked on NPTL, where a system() child's SIGCHLD reaches another thread than the one
 * waiting in system().) A reset waits for the VM itself, after vm_unwatch().
 */
static guint vm_watch;  /* the current VM's child watch, 0 for none */

static void vm_ended(GPid pid, gint status, gpointer data)
{
    (void)data;
    if (vm != NULL && vm->pid == pid) {
        if (WIFEXITED(status))
            g_log(NULL, G_LOG_LEVEL_DEBUG, "handChild: end process: %d", (int)pid);
        vm->pid = -1;
        vm_watch = 0;
    }
    g_spawn_close_pid(pid);
}

/* The VM's child watch removed: the caller reaps it (a wait GLib would otherwise race with). */
void vm_unwatch(void)
{
    if (vm_watch != 0) {
        g_source_remove(vm_watch);
        vm_watch = 0;
    }
}

/* In the child: wire the pipe to stdin and /dev/null to stdout, and become the VM.
 * Nothing reads the VM's output: a pipe there could fill and block it. */
static void vm_exec(int to_vm[2])
{
    dup2(to_vm[0], 0);
    close(to_vm[1]);
    int null = open("/dev/null", O_WRONLY);
    if (null >= 0) {
        dup2(null, 1);
        if (null != 1)
            close(null);
    }
    execlp("/usr/karotz/bin/karotzVM", "/usr/karotz/bin/karotzVM", "--app_folder=/usr/karotz/apps", (char *)NULL);
    g_error("cannot run VM: %s", strerror(errno));
}

/*
 * The previous VM's pipe and record are closed and freed (killall is
 * relied on to stop it), and the child's end of the pipe is closed in
 * the parent.
 *
 * The build for the rabbit (uClibc) uses vfork(). When the exec fails, the
 * child aborts through g_error(); with glibc, abort() in a vfork child
 * signals the parent's thread (raise() uses the thread id cached in the
 * shared memory), killing the controller, so other builds use fork(), with
 * the same observable behaviour: only the child dies, then is reaped through
 * its child watch (vm_ended). (qemu-user also runs vfork() as fork().)
 */
void vm_launch(void)
{
    int to_vm[2];
    pid_t pid;

    /* Out of descriptors, most likely: no VM this time, and the previous
     * one's record is dropped (the reset has killed it, and a write to its
     * pipe would raise SIGPIPE), so vm_send() drops commands until the next
     * reset starts a VM. */
    if (pipe(to_vm) != 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "VM not started: pipe failed: %s", strerror(errno));
        if (vm != NULL) {
            close(vm->to_vm);
            free(vm);
            vm = NULL;
        }
        return;
    }
#ifdef __UCLIBC__
    pid = vfork();
#else
    pid = fork();
#endif
    if (pid == 0)
        vm_exec(to_vm);
    close(to_vm[0]);
    if (vm != NULL) {
        close(vm->to_vm);
        free(vm);
    }
    vm = (VirtualMachine *)malloc(sizeof *vm);
    vm->pid = pid;
    vm->to_vm = to_vm[1];
    vm_watch = pid > 0 ? g_child_watch_add(pid, vm_ended, NULL) : 0;
}

/* The running VM's pid, for a reset to wait for; -1 when none is running (none started, the
 * fork failed, or it ended and was reaped already). */
pid_t vm_pid(void)
{
    return vm != NULL ? vm->pid : -1;
}

/* vm_send(): before the first reset there is no VM, and the command is
 * dropped. */
/*
 * The VM reads one "<apikey> <args...>" line per application (fgets, at
 * most VM_LINE_MAX characters) and forks a child for each, so a field with
 * a newline would start a second application of the sender's choosing and
 * a space would shift the arguments. The platform's fields are checked
 * with this before they go in.
 */
gboolean vm_safe_word(const char *s)
{
    if (s == NULL || *s == '\0')
        return FALSE;
    for (; *s != '\0'; s++) {
        if ((unsigned char)*s <= ' ' || *s == 0x7f)
            return FALSE;
    }
    return TRUE;
}

/* s with whitespace and control characters replaced by '_' (as the ASR
 * trigger already has its spaces replaced). */
std::string vm_word(const char *s)
{
    std::string word(s != NULL ? s : "");
    for (size_t i = 0; i < word.length(); i++) {
        if ((unsigned char)word[i] <= ' ' || word[i] == 0x7f)
            word[i] = '_';
    }
    return word;
}

int vm_send(const char *command)
{
    if (vm == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "no VM yet, dropped: %s", command);
        return -1;
    }
    /* The VM ended outside a reset (killed or crashed; vm_ended set its pid to -1): start a
     * new one for this command, as a reset would, rather than losing commands until the next
     * reset. */
    if (vm->pid <= 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "VM gone, starting a new one for: %s", command);
        vm_launch();
        if (vm == NULL)  /* vm_launch() drops the record when it can't make a pipe */
            return -1;
    }
    if (write(vm->to_vm, command, strlen(command)) < 0) {
        /* Logged, otherwise ignored. */
        g_log(NULL, G_LOG_LEVEL_WARNING, "VM command lost (%s): %s", strerror(errno), command);
    }
    return 0;
}

/* On the way out: stop the VM and free its malloc'ed record. */
void vm_kill(void)
{
    if (vm != NULL) {
        /* pid -1 once reaped (vm_ended): kill(-1) would signal every process. */
        if (vm->pid > 0)
            kill(vm->pid, SIGTERM);
        free(vm);
    }
}
