/* cpu_top: top processes by CPU time over one second.
 *
 * Built from the two functions of module 2: core_list_procs (chapter 2.1)
 * gives the process list, core_proc_cpu (chapter 2.3) gives user and kernel
 * microseconds. The spawn-based version of this program appears in chapter
 * 2.3 together with core_spawn from chapter 2.2; this file keeps the same
 * command line instead: "cpu_top [pid ...]" prints the CPU time of the given
 * processes, and with no arguments it shows the busiest processes.
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include "core.h"

#ifdef _WIN32
#include <windows.h>
#define CORE_TOO_SMALL ERROR_INSUFFICIENT_BUFFER
static void pause_ms(int ms) { Sleep(ms); }
static int64_t now_us(void) { return (int64_t)GetTickCount64() * 1000; }
#else
#include <errno.h>
#include <time.h>
#define CORE_TOO_SMALL ERANGE
static void pause_ms(int ms) {
    struct timespec t = {ms / 1000, (ms % 1000) * 1000000L};
    nanosleep(&t, NULL);
}
static int64_t now_us(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000000 + t.tv_nsec / 1000;
}
#endif

#define MAX 4096
#define TOP 8
#define INTERVAL_MS 1000

struct row {
    int64_t pid;
    int64_t pct;
    char    name[CORE_NAME_MAX];
};

static int by_pct(const void *a, const void *b) {
    const struct row *x = a, *y = b;
    if (x->pct < y->pct)
        return 1;
    if (x->pct > y->pct)
        return -1;
    return 0;
}

static void print_cpu(int64_t pid) {
    struct core_cpu cpu;
    int err = core_proc_cpu(pid, &cpu);
    if (err != 0) {
        printf("pid %" PRId64 ": error %d\n", pid, err);
        return;
    }
    printf("pid %" PRId64 ": user %" PRId64 " us, kernel %" PRId64 " us\n",
           pid, cpu.user_us, cpu.kernel_us);
}

int main(int argc, char *argv[]) {
    static struct core_proc procs[MAX];
    static int64_t before[MAX];
    static struct row rows[MAX];
    struct core_cpu cpu;
    int32_t room = 256, count = 0, used = 0;

    if (argc > 1) {
        for (int i = 1; i < argc; i++)
            print_cpu(atoll(argv[i]));
        return 0;
    }

    for (;;) {
        int err = core_list_procs(procs, room, &count);
        if (err == 0)
            break;
        if (err != CORE_TOO_SMALL || count + 64 > MAX) {
            printf("core_list_procs: error %d, need %d places\n", err, (int)count);
            return 1;
        }
        room = count + 64;
    }

    for (int32_t i = 0; i < count; i++) {
        before[i] = -1;
        if (core_proc_cpu(procs[i].pid, &cpu) == 0)
            before[i] = cpu.user_us + cpu.kernel_us;
    }
    int64_t start = now_us();
    pause_ms(INTERVAL_MS);
    int64_t wall_us = now_us() - start;
    if (wall_us <= 0)
        wall_us = INTERVAL_MS * 1000;

    for (int32_t i = 0; i < count; i++) {
        if (before[i] < 0)
            continue;
        if (core_proc_cpu(procs[i].pid, &cpu) != 0)
            continue;
        rows[used].pid = procs[i].pid;
        rows[used].pct = (cpu.user_us + cpu.kernel_us - before[i]) * 100 / wall_us;
        snprintf(rows[used].name, sizeof rows[used].name, "%s", procs[i].name);
        used++;
    }
    qsort(rows, used, sizeof rows[0], by_pct);

    if (used > TOP)
        used = TOP;
    for (int32_t i = 0; i < used; i++)
        printf("%3" PRId64 "%% %7" PRId64 "  %s\n", rows[i].pct, rows[i].pid,
               rows[i].name);
    printf("processes %d, sizeof %zu\n", (int)count, sizeof procs[0]);
    return 0;
}
