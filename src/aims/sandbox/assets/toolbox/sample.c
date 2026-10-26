/*
 * aims-sample HZ DIR
 *
 * Reads the cgroup v2 files in DIR HZ times a second and prints one line per
 * sample: the boot clock in us, cpu usage, user and system us, throttled
 * periods and us, io read and write bytes and ops, the some and full stall
 * totals of cpu, io and memory pressure in us, memory bytes and pids. A file
 * that cannot be opened reads as -1. Lines go out in batches every 100 ms. A
 * tick that falls behind is skipped, not bunched. Exits 0 once the cgroup is
 * gone or the reader has left.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

enum { CPU, IO, CPU_P, IO_P, MEM_P, MEM, PIDS, FILES };
static const char *names[FILES] = {
    "cpu.stat", "io.stat", "cpu.pressure", "io.pressure", "memory.pressure", "memory.current",
    "pids.current",
};

enum { COLUMNS = 17 };
static const long long BATCH_NS = 100000000LL;
static int fds[FILES];
static char buf[16384];

static int load(int i) {
    if (fds[i] < 0) return 0;
    ssize_t n = pread(fds[i], buf, sizeof buf - 1, 0);
    if (n < 0) return errno == ENODEV || errno == ENOENT ? -1 : 0;
    buf[n] = 0;
    return 1;
}

static long long field(const char *text, const char *key) {
    size_t len = strlen(key);
    for (const char *p = text; (p = strstr(p, key)) != NULL; p += len) {
        if ((p == text || p[-1] == '\n' || p[-1] == ' ') && p[len] == ' ') return atoll(p + len);
    }
    return -1;
}

static void pressure(long long *some, long long *full) {
    const char *f = strstr(buf, "\nfull ");
    const char *s = strstr(buf, "total=");
    *some = s && (!f || s < f) ? atoll(s + 6) : -1;
    s = f ? strstr(f, "total=") : NULL;
    *full = s ? atoll(s + 6) : -1;
}

static void io(long long *out) {
    static const char *keys[4] = {" rbytes=", " wbytes=", " rios=", " wios="};
    for (int k = 0; k < 4; k++) {
        out[k] = 0;
        for (const char *p = buf; (p = strstr(p, keys[k])) != NULL; p++) {
            out[k] += atoll(p + strlen(keys[k]));
        }
    }
}

static int sample(long long *v) {
    for (int i = 0; i < COLUMNS; i++) v[i] = -1;
    int r = load(CPU);
    if (r < 0) return -1;
    if (r) {
        v[0] = field(buf, "usage_usec");
        v[1] = field(buf, "user_usec");
        v[2] = field(buf, "system_usec");
        v[3] = field(buf, "nr_throttled");
        v[4] = field(buf, "throttled_usec");
    }
    if ((r = load(IO)) < 0) return -1;
    if (r) io(v + 5);
    for (int i = 0; i < 3; i++) {
        if ((r = load(CPU_P + i)) < 0) return -1;
        if (r) pressure(v + 9 + 2 * i, v + 10 + 2 * i);
    }
    if ((r = load(MEM)) < 0) return -1;
    if (r) v[15] = atoll(buf);
    if ((r = load(PIDS)) < 0) return -1;
    if (r) v[16] = atoll(buf);
    return 0;
}

static long long ns(struct timespec t) { return t.tv_sec * 1000000000LL + t.tv_nsec; }

int main(int argc, char **argv) {
    if (argc != 3 || atoi(argv[1]) < 1 || atoi(argv[1]) > 1000) {
        fprintf(stderr, "usage: aims-sample HZ DIR\n");
        return 2;
    }
    signal(SIGPIPE, SIG_IGN);
    long long period = 1000000000LL / atoi(argv[1]);
    char path[4096];
    for (int i = 0; i < FILES; i++) {
        snprintf(path, sizeof path, "%s/%s", argv[2], names[i]);
        fds[i] = open(path, O_RDONLY | O_CLOEXEC);
    }
    if (fds[CPU] < 0) {
        perror(argv[2]);
        return 1;
    }
    struct timespec next, now, boot;
    clock_gettime(CLOCK_MONOTONIC, &next);
    long long flushed = ns(next);
    char out[8192];
    int used = 0;
    long long v[COLUMNS];
    for (;;) {
        clock_gettime(CLOCK_BOOTTIME, &boot);
        int gone = sample(v) < 0;
        if (!gone) {
            used += snprintf(out + used, sizeof out - used, "%lld", ns(boot) / 1000);
            for (int i = 0; i < COLUMNS; i++) {
                used += snprintf(out + used, sizeof out - used, " %lld", v[i]);
            }
            out[used++] = '\n';
        }
        clock_gettime(CLOCK_MONOTONIC, &now);
        if (gone || used > (int)sizeof out - 512 || ns(now) - flushed >= BATCH_NS) {
            if (used && write(1, out, used) != used) return 0;
            used = 0;
            flushed = ns(now);
        }
        if (gone) return 0;
        long long t = ns(next) + period;
        while (t <= ns(now)) t += period;
        next.tv_sec = t / 1000000000LL;
        next.tv_nsec = t % 1000000000LL;
        while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL) == EINTR) {
        }
    }
}
