#pragma once
#include <Arduino.h>

struct UsageData {
    float session_pct;       // 5-hour window utilization (0-100)
    int session_reset_mins;  // minutes until session resets
    float weekly_pct;        // 7-day window utilization (0-100)
    int weekly_reset_mins;   // minutes until weekly resets
    char status[16];         // "allowed" or "limited"
    bool ok;                 // data parse succeeded
    bool valid;              // false until first successful parse
    // mateo/weekly-delta patch: percent change vs previous 7d, computed by the
    // daemon from local Claude Code JSONL logs (the API headers don't expose
    // history). Null/missing for the first week of use.
    float delta_pct;
    bool has_delta;
};
