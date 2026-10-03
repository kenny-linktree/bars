// Interposes the wall-clock entry points used by Foundation and SwiftUI. Only CLOCK_REALTIME is
// shifted, so monotonic timers and layout scheduling are unaffected. dyld applies `__interpose`
// tuples from this dylib because the harness links it directly.
#include <CoreFoundation/CoreFoundation.h>
#include <sys/time.h>
#include <time.h>
#include "PreviewClock.h"

static double clock_offset = 0;

void bars_preview_set_clock_offset(double seconds) { clock_offset = seconds; }

static int preview_clock_gettime(clockid_t clock, struct timespec *value) {
    int result = clock_gettime(clock, value);
    if (result == 0 && value && clock == CLOCK_REALTIME && clock_offset != 0) {
        double shifted = (double)value->tv_sec + value->tv_nsec / 1e9 + clock_offset;
        value->tv_sec = (time_t)shifted;
        value->tv_nsec = (long)((shifted - (double)value->tv_sec) * 1e9);
    }
    return result;
}

static int preview_gettimeofday(struct timeval *value, void *zone) {
    int result = gettimeofday(value, zone);
    if (result == 0 && value && clock_offset != 0) {
        double shifted = (double)value->tv_sec + value->tv_usec / 1e6 + clock_offset;
        value->tv_sec = (time_t)shifted;
        value->tv_usec = (suseconds_t)((shifted - (double)value->tv_sec) * 1e6);
    }
    return result;
}

static CFAbsoluteTime preview_absolute_time(void) { return CFAbsoluteTimeGetCurrent() + clock_offset; }

__attribute__((used)) static const struct { const void *replacement, *original; } interposers[]
    __attribute__((section("__DATA,__interpose"))) = {
    { (const void *)preview_clock_gettime, (const void *)clock_gettime },
    { (const void *)preview_gettimeofday, (const void *)gettimeofday },
    { (const void *)preview_absolute_time, (const void *)CFAbsoluteTimeGetCurrent },
};
