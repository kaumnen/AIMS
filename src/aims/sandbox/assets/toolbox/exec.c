/*
 * aims-exec [--id ID] TIMEOUT_MS GRACE_MS PROG [ARG...]
 * aims-exec -k ARG...
 *
 * Runs PROG in a new process group. At the deadline the whole group gets
 * SIGTERM, then SIGKILL after GRACE_MS. Exits with PROG's status (128 + signal
 * when PROG was killed), 124 when SIGTERM ended it after the deadline and 137
 * when SIGKILL was needed.
 *
 * ID is not used. It makes the command line of each exec unique, so -k ends
 * only that exec. PROG never sees it.
 *
 * Time the sandbox spends frozen (docker pause) does not count: the wait runs
 * in short slices, and a slice that took far longer than asked is taken as a
 * freeze and moves the deadline on by the excess.
 *
 * -k ends what a cancelled exec started: each process whose command line is
 * ARG... exactly gets SIGKILL to the process group of each of its children,
 * the group the deadline ends, then SIGKILL itself. The process is stopped
 * first, so it forks no child between the listing of its children and its
 * own end. It reads /proc and forks nothing, so it works in a sandbox at its
 * pids limit. Exits 0.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static long long now_ms(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000LL + t.tv_nsec / 1000000;
}

#define SLICE_MS 200
#define FROZEN_MS 1000

static int reap_until(pid_t pid, long long deadline, int *status, const sigset_t *chld) {
    for (;;) {
        pid_t r = waitpid(pid, status, WNOHANG);
        if (r == pid) return 1;
        if (r < 0 && errno != EINTR) return -1;
        long long start = now_ms();
        long long left = deadline - start;
        if (left <= 0) return 0;
        long long step = left < SLICE_MS ? left : SLICE_MS;
        struct timespec ts = {step / 1000, (step % 1000) * 1000000};
        sigtimedwait(chld, NULL, &ts);
        long long took = now_ms() - start;
        if (took > step + FROZEN_MS) deadline += took - step;
    }
}

static int code(int status) {
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}

/* Up to `size` bytes of /proc/PID/NAME into `buf`; -1 when it cannot be read. */
static ssize_t proc_read(pid_t pid, const char *name, char *buf, size_t size) {
    char path[64];
    snprintf(path, sizeof path, "/proc/%d/%s", pid, name);
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    size_t got = 0;
    while (got < size) {
        ssize_t n = read(fd, buf + got, size - got);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) break;
        got += (size_t)n;
    }
    close(fd);
    return (ssize_t)got;
}

/* The pids in /proc, other than this process; NULL when that fails. */
static pid_t *pids(size_t *count) {
    DIR *dir = opendir("/proc");
    if (dir == NULL) return NULL;
    size_t cap = 256;
    pid_t *out = malloc(cap * sizeof *out);
    *count = 0;
    struct dirent *entry;
    while (out != NULL && (entry = readdir(dir)) != NULL) {
        char *end;
        long pid = strtol(entry->d_name, &end, 10);
        if (*end != '\0' || pid <= 0 || pid == getpid()) continue;
        if (*count == cap) {
            pid_t *more = realloc(out, (cap *= 2) * sizeof *out);
            if (more == NULL) free(out);
            out = more;
            if (out == NULL) break;
        }
        out[(*count)++] = (pid_t)pid;
    }
    closedir(dir);
    return out;
}

static pid_t parent_of(pid_t pid) {
    char buf[1024];
    ssize_t n = proc_read(pid, "status", buf, sizeof buf - 1);
    if (n <= 0) return 0;
    buf[n] = '\0';
    char *line = strstr(buf, "\nPPid:");
    return line == NULL ? 0 : (pid_t)strtol(line + 6, NULL, 10);
}

/* Whether PID runs no more code: stopped, a zombie or gone. */
static int halted(pid_t pid) {
    char buf[512];
    ssize_t n = proc_read(pid, "stat", buf, sizeof buf - 1);
    if (n <= 0) return 1;
    buf[n] = '\0';
    /* The state follows the name, which may itself hold ")". */
    char *end = strrchr(buf, ')');
    return end == NULL || end[1] != ' ' || strchr("TtZX", end[2]) != NULL;
}

#define STOP_MS 200

/* SIGKILL to the process group of each child of PID, then to PID. */
static void end_tree(pid_t p) {
    kill(p, SIGSTOP);
    struct timespec ms = {0, 1000000};
    for (int i = 0; i < STOP_MS && !halted(p); i++) nanosleep(&ms, NULL);
    size_t count;
    pid_t *all = pids(&count);
    for (size_t j = 0; all != NULL && j < count; j++) {
        pid_t c = all[j];
        if (parent_of(c) != p) continue;
        kill(-c, SIGKILL);
        kill(c, SIGKILL);
    }
    free(all);
    kill(p, SIGKILL);
}

static int end_exec(int argc, char **argv) {
    size_t len = 0;
    for (int i = 0; i < argc; i++) len += strlen(argv[i]) + 1;
    char *want = malloc(len);
    char *got = malloc(len + 1);
    size_t count;
    pid_t *all = pids(&count);
    if (want == NULL || got == NULL || all == NULL) {
        perror("aims-exec: -k");
        return 125;
    }
    char *at = want;
    for (int i = 0; i < argc; i++) at = stpcpy(at, argv[i]) + 1;

    for (size_t i = 0; i < count; i++) {
        pid_t p = all[i];
        if (proc_read(p, "cmdline", got, len + 1) != (ssize_t)len) continue;
        if (memcmp(got, want, len) != 0) continue;
        end_tree(p);
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc > 2 && strcmp(argv[1], "-k") == 0) return end_exec(argc - 2, argv + 2);
    int first = argc > 2 && strcmp(argv[1], "--id") == 0 ? 3 : 1;
    if (argc < first + 3) {
        fprintf(stderr,
                "usage: aims-exec [--id ID] TIMEOUT_MS GRACE_MS PROG [ARG...]\n"
                "       aims-exec -k ARG...\n");
        return 125;
    }
    long long timeout = atoll(argv[first]);
    long long grace = atoll(argv[first + 1]);
    char **prog = argv + first + 2;
    sigset_t chld;
    sigemptyset(&chld);
    sigaddset(&chld, SIGCHLD);
    sigprocmask(SIG_BLOCK, &chld, NULL);

    pid_t pid = fork();
    if (pid < 0) {
        perror("aims-exec: fork");
        return 125;
    }
    if (pid == 0) {
        sigprocmask(SIG_UNBLOCK, &chld, NULL);
        setpgid(0, 0);
        execvp(prog[0], prog);
        int err = errno;
        fprintf(stderr, "aims-exec: %s: %s\n", prog[0], strerror(err));
        _exit(err == ENOENT ? 127 : 126);
    }
    setpgid(pid, pid);

    int status;
    int r = reap_until(pid, now_ms() + timeout, &status, &chld);
    if (r == 1) return code(status);
    if (r < 0) return 125;

    kill(-pid, SIGTERM);
    r = reap_until(pid, now_ms() + grace, &status, &chld);
    kill(-pid, SIGKILL);
    if (r == 1) return 124;
    waitpid(pid, &status, 0);
    return 137;
}
