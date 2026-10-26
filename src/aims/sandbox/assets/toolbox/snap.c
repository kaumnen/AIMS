/*
 * aims-snap PATH=DIR...
 *
 * Reports how the files under each DIR changed, named as the sandbox sees them at PATH. Each
 * stdin line "S FILE_CAP BLOB_CAP ENTRY_CAP HASH_CAP" takes a snapshot and answers on stdout:
 *
 *   C CHANGE KIND SIZE MODE MTIME_US SHA CONTENT_LEN TIMESTOMP PATH_LEN TARGET_LEN\n
 *     followed by PATH, the symlink TARGET and CONTENT, each exactly its length
 *   E ENTRIES FILES BYTES HASHED UNHASHED SKIPPED TRUNCATED DIGEST\n
 *
 * CHANGE is A added, M content changed, T mode or mtime changed only, D removed, K kind changed.
 * KIND is f, d, l, p, s or o. SHA is the sha256 of a file or link target, "-" when not hashed.
 * A file is hashed again only when its inode, size, mtime or ctime moved. Links are never
 * followed. A walk stopped by ENTRY_CAP reports no removals.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define MAX_DEPTH 128
#define MAX_PATH 4096

typedef struct {
    uint32_t h[8];
    uint64_t len;
    unsigned char buf[64];
    size_t used;
} sha256;

static const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
    0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
    0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
    0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
    0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
    0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
    0xc67178f2};

#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha_block(sha256 *s, const unsigned char *p) {
    uint32_t w[64], a, b, c, d, e, f, g, h;
    for (int i = 0; i < 16; i++)
        w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 |
               (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = s->h[0], b = s->h[1], c = s->h[2], d = s->h[3];
    e = s->h[4], f = s->h[5], g = s->h[6], h = s->h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t t1 = h + (ROR(e, 6) ^ ROR(e, 11) ^ ROR(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i];
        uint32_t t2 = (ROR(a, 2) ^ ROR(a, 13) ^ ROR(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        h = g, g = f, f = e, e = d + t1, d = c, c = b, b = a, a = t1 + t2;
    }
    s->h[0] += a, s->h[1] += b, s->h[2] += c, s->h[3] += d;
    s->h[4] += e, s->h[5] += f, s->h[6] += g, s->h[7] += h;
}

static void sha_init(sha256 *s) {
    static const uint32_t iv[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                   0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    memcpy(s->h, iv, sizeof iv);
    s->len = 0;
    s->used = 0;
}

static void sha_update(sha256 *s, const void *data, size_t n) {
    const unsigned char *p = data;
    s->len += n;
    while (n) {
        size_t take = 64 - s->used < n ? 64 - s->used : n;
        memcpy(s->buf + s->used, p, take);
        s->used += take, p += take, n -= take;
        if (s->used == 64) {
            sha_block(s, s->buf);
            s->used = 0;
        }
    }
}

static void sha_final(sha256 *s, unsigned char out[32]) {
    uint64_t bits = s->len * 8;
    unsigned char pad = 0x80, zero = 0, len[8];
    sha_update(s, &pad, 1);
    while (s->used != 56) sha_update(s, &zero, 1);
    for (int i = 0; i < 8; i++) len[i] = (unsigned char)(bits >> (56 - 8 * i));
    sha_update(s, len, 8);
    for (int i = 0; i < 8; i++) {
        out[4 * i] = (unsigned char)(s->h[i] >> 24), out[4 * i + 1] = (unsigned char)(s->h[i] >> 16);
        out[4 * i + 2] = (unsigned char)(s->h[i] >> 8), out[4 * i + 3] = (unsigned char)s->h[i];
    }
}

static void hex(const unsigned char *sha, char out[65]) {
    for (int i = 0; i < 32; i++) sprintf(out + 2 * i, "%02x", sha[i]);
}

typedef struct {
    char *path;
    char *target;
    unsigned char *content;
    size_t clen;
    char kind;
    int hashed;
    int fresh;
    unsigned mode;
    long long size, mtime_ns, ctime_ns;
    unsigned long long ino, dev;
    unsigned char sha[32];
} entry;

typedef struct {
    entry *v;
    size_t n, cap;
} list;

typedef struct {
    const char *path;
    const char *dir;
} root;

static root *roots;
static int nroots;
static list old;
static long long file_cap, blob_left, entry_left, hash_left;
static long long n_hashed, n_unhashed, n_skipped;
static int truncated;

static int by_path(const void *a, const void *b) {
    return strcmp(((const entry *)a)->path, ((const entry *)b)->path);
}

static entry *find_old(const char *path) {
    entry key = {.path = (char *)path};
    return old.n ? bsearch(&key, old.v, old.n, sizeof(entry), by_path) : NULL;
}

static char kind_of(mode_t m) {
    if (S_ISREG(m)) return 'f';
    if (S_ISDIR(m)) return 'd';
    if (S_ISLNK(m)) return 'l';
    if (S_ISFIFO(m)) return 'p';
    if (S_ISSOCK(m)) return 's';
    return 'o';
}

static entry *push(list *l) {
    if (l->n == l->cap) {
        l->cap = l->cap ? 2 * l->cap : 1024;
        l->v = realloc(l->v, l->cap * sizeof(entry));
        if (!l->v) exit(70);
    }
    entry *e = &l->v[l->n++];
    memset(e, 0, sizeof *e);
    return e;
}

static int same_stat(const entry *a, const entry *b) {
    return a->ino == b->ino && a->dev == b->dev && a->size == b->size &&
           a->mtime_ns == b->mtime_ns && a->ctime_ns == b->ctime_ns;
}

static void hash_file(int dfd, const char *name, entry *e) {
    if (e->size > hash_left) {
        n_unhashed++;
        return;
    }
    int fd = openat(dfd, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_NOCTTY | O_CLOEXEC);
    struct stat st;
    if (fd < 0) {
        n_unhashed++;
        return;
    }
    if (fstat(fd, &st) || (unsigned long long)st.st_ino != e->ino || !S_ISREG(st.st_mode)) {
        close(fd);
        n_unhashed++;
        return;
    }
    int keep = e->size <= file_cap && e->size <= blob_left;
    if (keep && e->size) {
        e->content = malloc((size_t)e->size);
        if (!e->content) keep = 0;
    }
    sha256 s;
    sha_init(&s);
    unsigned char buf[65536];
    long long total = 0;
    ssize_t r;
    while ((r = read(fd, buf, sizeof buf)) > 0) {
        if (total + r > hash_left) break;
        sha_update(&s, buf, (size_t)r);
        if (keep && total + r <= e->size) memcpy(e->content + total, buf, (size_t)r);
        else if (keep) keep = 0;
        total += r;
    }
    close(fd);
    hash_left -= total;
    if (r != 0) {
        free(e->content);
        e->content = NULL;
        n_unhashed++;
        return;
    }
    sha_final(&s, e->sha);
    e->hashed = 1;
    n_hashed += total;
    if (keep && total == e->size) {
        e->clen = (size_t)total;
        blob_left -= total;
    } else {
        free(e->content);
        e->content = NULL;
    }
}

static int is_other_root(const char *path, int self) {
    for (int i = 0; i < nroots; i++)
        if (i != self && strcmp(path, roots[i].path) == 0) return 1;
    return 0;
}

static void walk(int dfd, int self, char *path, size_t plen, int depth, list *out) {
    int copy = dup(dfd);
    DIR *d = copy < 0 ? NULL : fdopendir(copy);
    if (!d) {
        if (copy >= 0) close(copy);
        return;
    }
    struct dirent *de;
    while ((de = readdir(d))) {
        const char *name = de->d_name;
        if (!strcmp(name, ".") || !strcmp(name, "..")) continue;
        size_t nlen = strlen(name);
        if (plen + 1 + nlen >= MAX_PATH || entry_left <= 0) {
            truncated = 1;
            continue;
        }
        struct stat st;
        if (fstatat(dfd, name, &st, AT_SYMLINK_NOFOLLOW)) continue;
        path[plen] = '/';
        memcpy(path + plen + 1, name, nlen + 1);
        entry_left--;
        entry *e = push(out);
        e->path = strdup(path);
        e->kind = kind_of(st.st_mode);
        e->mode = st.st_mode & 07777;
        e->size = st.st_size;
        e->mtime_ns = st.st_mtim.tv_sec * 1000000000LL + st.st_mtim.tv_nsec;
        e->ctime_ns = st.st_ctim.tv_sec * 1000000000LL + st.st_ctim.tv_nsec;
        e->ino = st.st_ino;
        e->dev = st.st_dev;
        entry *was = find_old(e->path);
        if (e->kind == 'f') {
            if (was && was->kind == 'f' && was->hashed && same_stat(was, e)) {
                memcpy(e->sha, was->sha, 32);
                e->hashed = 1;
            } else {
                e->fresh = 1;
                hash_file(dfd, name, e);
            }
        } else if (e->kind == 'l') {
            char target[MAX_PATH];
            ssize_t n = readlinkat(dfd, name, target, sizeof target - 1);
            if (n >= 0) {
                target[n] = 0;
                e->target = strdup(target);
                sha256 s;
                sha_init(&s);
                sha_update(&s, target, (size_t)n);
                sha_final(&s, e->sha);
                e->hashed = 1;
            }
        } else if (e->kind == 'd' && !is_other_root(e->path, self)) {
            if (depth >= MAX_DEPTH) {
                truncated = 1;
            } else {
                int fd = openat(dfd, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
                if (fd >= 0) {
                    walk(fd, self, path, plen + 1 + nlen, depth + 1, out);
                    close(fd);
                }
            }
        }
        path[plen] = 0;
    }
    closedir(d);
}

static void emit(char change, const entry *e, int timestomp, int with_content) {
    char sha[65] = "-";
    if (e->hashed) hex(e->sha, sha);
    size_t clen = with_content ? e->clen : 0;
    if (e->kind == 'f' && (change == 'A' || change == 'M' || change == 'K') && e->fresh &&
        e->size > 0 && !e->clen)
        n_skipped++;
    size_t tlen = e->target ? strlen(e->target) : 0;
    long long mtime_us = e->mtime_ns / 1000;
    printf("C %c %c %lld %u %lld %s %zu %d %zu %zu\n", change, e->kind, e->size, e->mode,
           mtime_us, sha, clen, timestomp, strlen(e->path), tlen);
    fwrite(e->path, 1, strlen(e->path), stdout);
    if (tlen) fwrite(e->target, 1, tlen, stdout);
    if (clen) fwrite(e->content, 1, clen, stdout);
}

static void diff(const entry *was, entry *e) {
    if (was->kind != e->kind) {
        emit('K', e, 0, 1);
        return;
    }
    if (e->kind == 'f' && e->fresh && !(same_stat(was, e) && !was->hashed)) {
        int known = was->hashed && e->hashed;
        int changed = known ? memcmp(was->sha, e->sha, 32) != 0 : 1;
        if (changed) {
            int moved = was->size != e->size || (known && changed);
            emit('M', e, moved && e->mtime_ns <= was->mtime_ns, 1);
            return;
        }
    }
    if (e->kind == 'l' && was->hashed && e->hashed && memcmp(was->sha, e->sha, 32)) {
        emit('M', e, 0, 0);
        return;
    }
    if (was->mode != e->mode || (e->kind != 'd' && was->mtime_ns != e->mtime_ns))
        emit('T', e, 0, 0);
}

static void free_list(list *l) {
    for (size_t i = 0; i < l->n; i++) {
        free(l->v[i].path);
        free(l->v[i].target);
        free(l->v[i].content);
    }
    free(l->v);
    memset(l, 0, sizeof *l);
}

static void snapshot(void) {
    list now = {0};
    char path[MAX_PATH];
    truncated = 0;
    n_hashed = n_unhashed = n_skipped = 0;
    for (int i = 0; i < nroots; i++) {
        int fd = open(roots[i].dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
        if (fd < 0) continue;
        size_t plen = strlen(roots[i].path);
        if (plen >= MAX_PATH) continue;
        memcpy(path, roots[i].path, plen + 1);
        if (plen == 1) plen = 0;
        walk(fd, i, path, plen, 0, &now);
        close(fd);
    }
    qsort(now.v, now.n, sizeof(entry), by_path);
    size_t i = 0, j = 0, walked = now.n;
    while (i < old.n || j < walked) {
        int c = i == old.n ? 1 : j == walked ? -1 : strcmp(old.v[i].path, now.v[j].path);
        if (c < 0) {
            if (truncated) {
                entry *kept = push(&now);
                *kept = old.v[i];
                memset(&old.v[i], 0, sizeof(entry));
            } else {
                emit('D', &old.v[i], 0, 0);
            }
            i++;
        } else if (c > 0) {
            emit('A', &now.v[j], 0, 1);
            j++;
        } else {
            diff(&old.v[i], &now.v[j]);
            i++, j++;
        }
    }
    if (truncated) qsort(now.v, now.n, sizeof(entry), by_path);
    sha256 digest;
    sha_init(&digest);
    long long files = 0, bytes = 0;
    for (size_t k = 0; k < now.n; k++) {
        entry *e = &now.v[k];
        char sha[65] = "-", line[128];
        if (e->hashed) hex(e->sha, sha);
        int n = snprintf(line, sizeof line, "%c %lld %u %s\n", e->kind, e->size, e->mode, sha);
        sha_update(&digest, e->path, strlen(e->path) + 1);
        sha_update(&digest, line, (size_t)n);
        if (e->kind == 'f') files++, bytes += e->size;
        free(e->content);
        e->content = NULL;
        e->clen = 0;
        e->fresh = 0;
    }
    unsigned char out[32];
    char dhex[65];
    sha_final(&digest, out);
    hex(out, dhex);
    printf("E %zu %lld %lld %lld %lld %lld %d %s\n", now.n, files, bytes, n_hashed, n_unhashed,
           n_skipped, truncated, dhex);
    fflush(stdout);
    free_list(&old);
    old = now;
}

int main(int argc, char **argv) {
    nroots = argc - 1;
    roots = calloc((size_t)(nroots ? nroots : 1), sizeof(root));
    for (int i = 0; i < nroots; i++) {
        char *eq = strchr(argv[i + 1], '=');
        if (!eq) {
            fprintf(stderr, "usage: aims-snap PATH=DIR...\n");
            return 64;
        }
        *eq = 0;
        roots[i].path = argv[i + 1];
        roots[i].dir = eq + 1;
    }
    char line[256];
    while (fgets(line, sizeof line, stdin)) {
        long long caps[4];
        if (sscanf(line, "S %lld %lld %lld %lld", &caps[0], &caps[1], &caps[2], &caps[3]) != 4) {
            fprintf(stderr, "aims-snap: bad request\n");
            continue;
        }
        file_cap = caps[0], blob_left = caps[1], entry_left = caps[2], hash_left = caps[3];
        snapshot();
    }
    return 0;
}
