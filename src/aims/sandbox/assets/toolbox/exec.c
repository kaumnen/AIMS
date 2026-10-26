/*
 * aims-exec TIMEOUT_MS GRACE_MS PROG [ARG...]
 *
 * Runs PROG in a new process group. At the deadline the whole group gets
 * SIGTERM, then SIGKILL after GRACE_MS. Exits with PROG's status (128 + signal
 * when PROG was killed), 124 when SIGTERM ended it after the deadline and 137
 * when SIGKILL was needed.
 */
#define _GNU_SOURCE
#include <errno.h>
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

static int reap_until(pid_t pid, long long deadline, int *status, const sigset_t *chld) {
    for (;;) {
        pid_t r = waitpid(pid, status, WNOHANG);
        if (r == pid) return 1;
        if (r < 0 && errno != EINTR) return -1;
        long long left = deadline - now_ms();
        if (left <= 0) return 0;
        struct timespec ts = {left / 1000, (left % 1000) * 1000000};
        sigtimedwait(chld, NULL, &ts);
    }
}

static int code(int status) {
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: aims-exec TIMEOUT_MS GRACE_MS PROG [ARG...]\n");
        return 125;
    }
    long long timeout = atoll(argv[1]);
    long long grace = atoll(argv[2]);
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
        execvp(argv[3], argv + 3);
        int err = errno;
        fprintf(stderr, "aims-exec: %s: %s\n", argv[3], strerror(err));
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
