/*
 * ngs_control.c - Closed-loop pump control. See ngs_control.h for the design.
 */

#include "ngs_control.h"

#include <math.h>
#include <string.h>

/* For NGS_MAX_ADC_CHANNEL only -- the board limits used to validate a
 * configuration. No ngs_board_* function is called from here; this file stays
 * hardware-free so the tests can drive it with scripted measurements. */
#include "ngs_board.h"

/* --------------------------------------------------------------------------
 * Small helpers
 * ------------------------------------------------------------------------ */

static float clampf(float v, float lo, float hi)
{
    if (v < lo) {
        return lo;
    }
    if (v > hi) {
        return hi;
    }
    return v;
}

/* Moves `from` towards `to` by at most `max_step`. Used for both setpoint and
 * output ramping -- the same operation, applied to different quantities. */
static float slew(float from, float to, float max_step)
{
    if (max_step <= 0.0f) {
        return to; /* 0 means "no limit", not "never move" */
    }
    float delta = to - from;
    if (delta > max_step) {
        return from + max_step;
    }
    if (delta < -max_step) {
        return from - max_step;
    }
    return to;
}

/* Unsigned wrap-safe "has `deadline` passed?" -- micros() rolls over every
 * ~71 minutes and a bench run outlasts that. */
static bool elapsed(uint32_t now, uint32_t deadline)
{
    return (int32_t)(now - deadline) >= 0;
}

float ngs_control_convert(const NgsControlCfgPayload *cfg, uint16_t raw)
{
    return ((float)raw - cfg->cal_offset) * cfg->cal_scale;
}

/* --------------------------------------------------------------------------
 * Measurement filtering
 *
 * Median of the last five samples, then a first-order low-pass. The median
 * removes isolated spikes outright; the low-pass smooths the broadband noise
 * that remains. Doing it the other way round would let a single spike bleed
 * into the output for a whole time constant.
 * ------------------------------------------------------------------------ */

static float median5(const float *taps, uint8_t count)
{
    /* Insertion sort over at most five elements: no allocation, no library,
     * and faster than anything cleverer at this size. */
    float sorted[NGS_MEDIAN_TAPS];
    memcpy(sorted, taps, count * sizeof(float));

    for (uint8_t i = 1; i < count; i++) {
        float key = sorted[i];
        int8_t j = (int8_t)i - 1;
        while (j >= 0 && sorted[j] > key) {
            sorted[j + 1] = sorted[j];
            j--;
        }
        sorted[j + 1] = key;
    }
    return sorted[count / 2];
}

static float filter_sample(NgsControl *c, float sample, float dt)
{
    c->median_taps[c->median_pos] = sample;
    c->median_pos = (uint8_t)((c->median_pos + 1u) % NGS_MEDIAN_TAPS);
    if (c->median_count < NGS_MEDIAN_TAPS) {
        c->median_count++;
    }

    float med = median5(c->median_taps, c->median_count);

    float tau = c->cfg.filter_tau_s;
    if (tau <= 0.0f) {
        c->measurement = med;
        c->filter_primed = true;
        return med;
    }

    if (!c->filter_primed) {
        /* Start at the first reading rather than ramping up from zero, which
         * would look exactly like a huge transient to the controller. */
        c->measurement = med;
        c->filter_primed = true;
        return med;
    }

    /* alpha = dt / (tau + dt): the discrete form that stays stable for any dt,
     * unlike dt/tau which goes unstable once dt approaches tau. */
    float alpha = dt / (tau + dt);
    c->measurement += (med - c->measurement) * alpha;
    return c->measurement;
}

/* --------------------------------------------------------------------------
 * Configuration
 * ------------------------------------------------------------------------ */

void ngs_control_defaults(NgsControlCfgPayload *cfg)
{
    memset(cfg, 0, sizeof(*cfg));
    cfg->mode = NGS_PUMP_MODE_MANUAL;
    cfg->out_min = 0.0f;
    cfg->out_max = 100.0f;
    cfg->kp = 0.05f;          /* % per mL/min -- deliberately timid          */
    cfg->ki = 0.02f;          /* % per mL/min-second                         */
    cfg->kd = 0.0f;           /* off: the flow signal is too noisy for it    */
    cfg->filter_tau_s = 1.0f; /* seconds; the loop is allowed to be slow     */
    cfg->deadband = 0.0f;
    cfg->setpoint_slew = 60.0f; /* units/s */
    cfg->output_slew = 25.0f;   /* %/s: full travel in four seconds          */
    cfg->cal_scale = 1.0f;
    cfg->cal_offset = 0.0f;
    cfg->fault_below = 0.0f;
    cfg->period_us = 20000u; /* 50 Hz, far above the process bandwidth      */
}

void ngs_control_init(NgsControl *c)
{
    memset(c, 0, sizeof(*c));
    ngs_control_defaults(&c->cfg);
    c->mode = NGS_PUMP_MODE_MANUAL;
}

static int validate(const NgsControlCfgPayload *cfg)
{
    if (cfg->mode > NGS_PUMP_MODE_AUTO) {
        return NGS_ERR_BAD_ARGUMENT; /* AUTOTUNE is entered via its own message */
    }
    if (cfg->channel > NGS_MAX_ADC_CHANNEL) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    if (cfg->out_min < 0.0f || cfg->out_max > 100.0f || cfg->out_min >= cfg->out_max) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    if (cfg->period_us < 1000u || cfg->period_us > 1000000u) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    /* NaN fails every comparison, so test for it directly rather than hoping a
     * range check catches it. A NaN gain would silently poison the output. */
    if (isnan(cfg->kp) || isnan(cfg->ki) || isnan(cfg->kd) || isnan(cfg->setpoint)) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    if (cfg->kp < 0.0f || cfg->ki < 0.0f || cfg->kd < 0.0f) {
        return NGS_ERR_BAD_ARGUMENT; /* negative gains mean positive feedback */
    }
    if (cfg->filter_tau_s < 0.0f || cfg->deadband < 0.0f) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    if (cfg->cal_scale == 0.0f) {
        return NGS_ERR_BAD_ARGUMENT; /* every reading would be zero units */
    }
    return 0;
}

int ngs_control_configure(NgsControl *c, const NgsControlCfgPayload *cfg, float current_output)
{
    int err = validate(cfg);
    if (err != 0) {
        return err;
    }

    /* Reconfiguring cancels an experiment rather than fighting it. */
    if (c->autotune.state == NGS_AT_SETTLING || c->autotune.state == NGS_AT_RELAY) {
        c->autotune.state = NGS_AT_FAILED;
        c->autotune.fail_reason = NGS_AT_FAIL_ABORTED;
    }

    uint8_t was = c->mode;
    c->cfg = *cfg;
    c->mode = cfg->mode;

    if (cfg->mode == NGS_PUMP_MODE_AUTO) {
        if (was != NGS_PUMP_MODE_AUTO) {
            /* Bumpless transfer: seed the integrator so the very first tick
             * reproduces the output already applied. Without this the pump
             * jumps to whatever P alone says the moment auto is engaged. */
            c->output = current_output;
            c->integral = current_output;
            /* Deliberately NOT seeding setpoint_active from c->measurement
             * here: it holds whatever was last read while the loop was
             * running, which may be minutes old or zero. See seed_setpoint. */
            c->seed_setpoint = true;
            c->updates = 0;
            c->flags = 0;
        }
        /* Re-arm on the next tick either way. */
        c->next_us = 0;
    } else {
        c->output = current_output;
    }

    return 0;
}

void ngs_control_note_manual_output(NgsControl *c, float output_pct)
{
    if (c->mode == NGS_PUMP_MODE_MANUAL) {
        c->output = output_pct;
    }
}

/* --------------------------------------------------------------------------
 * The loop
 * ------------------------------------------------------------------------ */

static void enter_fault(NgsControl *c, float *output_pct)
{
    c->fault_count++;
    c->flags |= NGS_CTRL_FLAG_FAULT;
    c->mode = NGS_PUMP_MODE_MANUAL;
    c->integral = 0.0f;
    c->output = c->cfg.out_min;
    *output_pct = c->output;

    if (c->autotune.state == NGS_AT_SETTLING || c->autotune.state == NGS_AT_RELAY) {
        c->autotune.state = NGS_AT_FAILED;
        c->autotune.fail_reason = NGS_AT_FAIL_SENSOR;
    }
}

static void run_pid(NgsControl *c, float dt, float *output_pct)
{
    const NgsControlCfgPayload *cfg = &c->cfg;

    /* Ramp the setpoint rather than stepping it. */
    float previous_active = c->setpoint_active;
    c->setpoint_active = slew(c->setpoint_active, cfg->setpoint, cfg->setpoint_slew * dt);
    if (c->setpoint_active != cfg->setpoint) {
        c->flags |= NGS_CTRL_FLAG_SLEWING;
    } else {
        c->flags &= (uint8_t)~NGS_CTRL_FLAG_SLEWING;
    }
    (void)previous_active;

    float error = c->setpoint_active - c->measurement;

    c->p_term = cfg->kp * error;

    /* Derivative on the measurement, negated: d/dt(setpoint - meas) with the
     * setpoint term dropped, so a setpoint change contributes nothing. */
    if (cfg->kd > 0.0f && dt > 0.0f) {
        float d_meas = (c->measurement - c->last_measurement) / dt;
        c->d_term = -cfg->kd * d_meas;
    } else {
        c->d_term = 0.0f;
    }
    c->last_measurement = c->measurement;

    /* Integral, with two independent guards. */
    bool integrate = true;
    if (cfg->deadband > 0.0f && fabsf(error) < cfg->deadband) {
        /* Inside the deadband the error is mostly noise; integrating it just
         * walks the output around for no reason. */
        integrate = false;
    }

    float unsaturated = c->p_term + c->integral + c->d_term;
    if ((unsaturated >= cfg->out_max && error > 0.0f) ||
        (unsaturated <= cfg->out_min && error < 0.0f)) {
        /* Conditional integration: already pinned at a limit and the error
         * would push further into it. Accumulating here is pure windup -- the
         * term would have to be unwound before the output could ever come off
         * the limit, which is exactly the overshoot everyone hates. */
        integrate = false;
        c->flags |= NGS_CTRL_FLAG_WINDUP;
    } else {
        c->flags &= (uint8_t)~NGS_CTRL_FLAG_WINDUP;
    }

    if (integrate) {
        c->integral += cfg->ki * error * dt;
        /* Clamp the accumulator itself to the output range. Conditional
         * integration alone still lets it sit far outside the achievable
         * range after a long excursion. */
        c->integral = clampf(c->integral, cfg->out_min, cfg->out_max);
    }
    c->i_term = c->integral;

    float raw_output = c->p_term + c->i_term + c->d_term;
    float limited = clampf(raw_output, cfg->out_min, cfg->out_max);

    if (limited != raw_output) {
        c->flags |= NGS_CTRL_FLAG_SATURATED;
    } else {
        c->flags &= (uint8_t)~NGS_CTRL_FLAG_SATURATED;
    }

    /* Rate-limit the actuator too: a pump asked to jump 0->100 % draws a
     * current spike the bench supply may not enjoy. */
    c->output = slew(c->output, limited, cfg->output_slew * dt);
    c->output = clampf(c->output, cfg->out_min, cfg->out_max);
    *output_pct = c->output;
}

/* --------------------------------------------------------------------------
 * Relay autotune
 *
 * Drive the output between bias+amplitude and bias-amplitude, switching when
 * the measurement crosses the setpoint by more than the hysteresis band. The
 * process settles into a limit cycle; its amplitude `a` and period `Tu` give
 *
 *     Ku = 4d / (pi * sqrt(a^2 - h^2))
 *
 * where d is the relay amplitude and h the hysteresis. The sqrt term is the
 * correction for the hysteresis -- omit it and every gain comes out high,
 * which on a real bench means an oscillating pump.
 * ------------------------------------------------------------------------ */

void ngs_control_apply_rule(uint8_t rule, float ku, float tu, float *kp, float *ki, float *kd)
{
    float p, ti, td;

    switch (rule) {
    case NGS_AT_RULE_ZIEGLER_NICHOLS:
        p = 0.45f * ku;
        ti = tu / 1.2f;
        td = 0.0f;
        break;
    case NGS_AT_RULE_PESSEN:
        p = 0.7f * ku;
        ti = 0.4f * tu;
        td = 0.15f * tu;
        break;
    case NGS_AT_RULE_TYREUS_LUYBEN:
    default:
        /* Tyreus-Luyben: markedly less aggressive than Ziegler-Nichols, which
         * was derived for quarter-amplitude decay -- i.e. for a loop that
         * visibly rings. On a bench pump, settling slowly beats oscillating. */
        p = ku / 3.2f;
        ti = 2.2f * tu;
        td = 0.0f;
        break;
    }

    *kp = p;
    *ki = (ti > 0.0f) ? (p / ti) : 0.0f;
    *kd = p * td;
}

static void autotune_finish(NgsControl *c)
{
    NgsAutotune *at = &c->autotune;

    if (at->cycles < 2u) {
        at->state = NGS_AT_FAILED;
        at->fail_reason = NGS_AT_FAIL_TIMEOUT;
        return;
    }

    /* Average the peaks and troughs, skipping the first cycle: it carries the
     * transient from however the process was sitting when we started. */
    uint8_t first = (at->cycles > 2u) ? 1u : 0u;
    uint8_t n = (uint8_t)(at->cycles - first);

    float amp_sum = 0.0f;
    float period_sum = 0.0f;
    for (uint8_t i = first; i < at->cycles; i++) {
        amp_sum += (at->peaks[i] - at->troughs[i]);
        period_sum += (float)at->periods_us[i];
    }

    float amplitude = (amp_sum / (float)n) * 0.5f; /* peak-to-peak -> amplitude */
    float period_us = period_sum / (float)n;

    /* How consistent were the cycles? A limit cycle that never settled means
     * the numbers below describe noise, not the process. Reported rather than
     * hidden, so the operator can judge. */
    float worst = 0.0f;
    for (uint8_t i = first; i < at->cycles; i++) {
        float dev = fabsf((float)at->periods_us[i] - period_us) / period_us;
        if (dev > worst) {
            worst = dev;
        }
    }
    at->spread = worst;

    float h = at->hysteresis;
    /* Ku divides by sqrt(a^2 - h^2). As the swing approaches the hysteresis
     * band that term collapses towards zero and Ku shoots off to infinity --
     * so a barely-clearing swing does not give a slightly uncertain answer, it
     * gives a confidently enormous one, and gains derived from it will make a
     * real pump oscillate. Demand real margin instead.
     *
     * A swing this small usually means the process has too little dead time
     * for relay feedback to say anything (a first-order lag has no ultimate
     * gain at all), or the relay amplitude was too timid to move it. */
    if (amplitude <= 1.5f * h) {
        at->state = NGS_AT_FAILED;
        at->fail_reason = NGS_AT_FAIL_NO_SWING;
        return;
    }

    /* A period only a few control ticks long is measuring the loop's own
     * sampling, not the process. */
    if (period_us < 10.0f * (float)c->cfg.period_us) {
        at->state = NGS_AT_FAILED;
        at->fail_reason = NGS_AT_FAIL_INCONSISTENT;
        return;
    }

    if (worst > 0.5f) {
        at->state = NGS_AT_FAILED;
        at->fail_reason = NGS_AT_FAIL_INCONSISTENT;
        return;
    }

    at->measured_amplitude = amplitude * 2.0f; /* report peak-to-peak */
    at->tu = period_us / 1e6f;
    at->ku = (4.0f * at->amplitude) / (3.14159265f * sqrtf(amplitude * amplitude - h * h));

    ngs_control_apply_rule(at->rule, at->ku, at->tu, &at->kp, &at->ki, &at->kd);
    at->state = NGS_AT_DONE;
    at->fail_reason = NGS_AT_FAIL_NONE;
}

static void autotune_tick(NgsControl *c, uint32_t now_us, float *output_pct)
{
    NgsAutotune *at = &c->autotune;

    if (elapsed(now_us, at->started_us + at->timeout_us)) {
        /* Out of time. If we gathered enough cycles anyway, use them --
         * a slow process that produced three good cycles is still tuned. */
        autotune_finish(c);
        if (at->state != NGS_AT_DONE) {
            at->state = NGS_AT_FAILED;
            at->fail_reason = NGS_AT_FAIL_TIMEOUT;
        }
        c->mode = NGS_PUMP_MODE_MANUAL;
        c->output = at->bias;
        *output_pct = c->output;
        return;
    }

    float m = c->measurement;

    /* Track the extremes over the whole cycle, not per relay phase.
     *
     * The process peaks *after* the relay switches, not before: the switch
     * happens the instant the measurement crosses the band, and dead time
     * carries it well past that before it turns around. Accumulating only
     * while the relay is high therefore records the switching threshold
     * rather than the peak -- which understates the swing, and since Ku
     * divides by sqrt(a^2 - h^2), understating the swing inflates the gain
     * without limit. */
    if (m > at->peak_acc) {
        at->peak_acc = m;
    }
    if (m < at->trough_acc) {
        at->trough_acc = m;
    }

    bool switch_now = false;
    if (at->relay_high && m > at->setpoint + at->hysteresis) {
        switch_now = true;
    } else if (!at->relay_high && m < at->setpoint - at->hysteresis) {
        switch_now = true;
    }

    if (switch_now) {
        if (!at->relay_high) {
            /* Rising edge: one full cycle has completed. */
            if (at->last_cross_us != 0u && at->cycles < NGS_AT_MAX_CYCLES) {
                at->periods_us[at->cycles] = now_us - at->last_cross_us;
                at->peaks[at->cycles] = at->peak_acc;
                at->troughs[at->cycles] = at->trough_acc;
                at->cycles++;
            }
            at->last_cross_us = now_us;
            at->peak_acc = m;
            at->trough_acc = m;
        }

        at->relay_high = !at->relay_high;
        at->state = NGS_AT_RELAY;

        if (at->cycles >= at->want_cycles) {
            autotune_finish(c);
            c->mode = NGS_PUMP_MODE_MANUAL;
            c->output = at->bias;
            *output_pct = c->output;
            return;
        }
    }

    /* The relay itself. No slew limiting: the step *is* the experiment. */
    c->output = clampf(at->relay_high ? at->bias + at->amplitude : at->bias - at->amplitude,
                       c->cfg.out_min, c->cfg.out_max);
    *output_pct = c->output;
}

int ngs_control_autotune(NgsControl *c, const NgsAutotuneCmdPayload *cmd, uint32_t now_us,
                         float current_output)
{
    NgsAutotune *at = &c->autotune;

    if (cmd->action == NGS_AT_ACTION_ABORT) {
        if (at->state == NGS_AT_SETTLING || at->state == NGS_AT_RELAY) {
            at->state = NGS_AT_FAILED;
            at->fail_reason = NGS_AT_FAIL_ABORTED;
        }
        c->mode = NGS_PUMP_MODE_MANUAL;
        return 0;
    }

    if (cmd->action != NGS_AT_ACTION_START) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    if (cmd->amplitude <= 0.0f || cmd->amplitude > 50.0f) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    if (cmd->hysteresis < 0.0f || isnan(cmd->setpoint) || cmd->setpoint < 0.0f) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    if (cmd->cycles < 2u || cmd->cycles > NGS_AT_MAX_CYCLES) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    if (cmd->rule > NGS_AT_RULE_PESSEN) {
        return NGS_ERR_BAD_ARGUMENT;
    }
    if (cmd->timeout_ms < 1000u) {
        return NGS_ERR_BAD_ARGUMENT;
    }

    memset(at, 0, sizeof(*at));
    at->state = NGS_AT_SETTLING;
    at->rule = cmd->rule;
    at->want_cycles = cmd->cycles;
    at->setpoint = cmd->setpoint;
    at->amplitude = cmd->amplitude;
    at->hysteresis = cmd->hysteresis;
    at->started_us = now_us;
    at->timeout_us = cmd->timeout_ms * 1000u;
    at->relay_high = true;
    at->peak_acc = c->measurement;
    at->trough_acc = c->measurement;

    /* Oscillate about the output already applied. Starting from the current
     * operating point keeps the experiment near the region being tuned --
     * a pump's gain is rarely the same at 5 % as at 80 %. */
    at->bias = clampf(current_output, c->cfg.out_min + cmd->amplitude,
                      c->cfg.out_max - cmd->amplitude);

    c->mode = NGS_PUMP_MODE_AUTOTUNE;
    c->next_us = 0;
    return 0;
}

void ngs_control_get_autotune(const NgsControl *c, NgsAutotuneResultPayload *out)
{
    const NgsAutotune *at = &c->autotune;
    memset(out, 0, sizeof(*out));
    out->state = at->state;
    out->fail_reason = at->fail_reason;
    out->cycles_done = at->cycles;
    out->rule = at->rule;
    out->ku = at->ku;
    out->tu = at->tu;
    out->amplitude = at->measured_amplitude;
    out->kp = at->kp;
    out->ki = at->ki;
    out->kd = at->kd;
    out->spread = at->spread;
}

/* --------------------------------------------------------------------------
 * Entry point
 * ------------------------------------------------------------------------ */

bool ngs_control_tick(NgsControl *c, uint32_t now_us, uint16_t raw, float *output_pct)
{
    if (c->mode == NGS_PUMP_MODE_MANUAL) {
        return false;
    }

    if (c->next_us == 0u) {
        c->next_us = now_us + c->cfg.period_us;
        /* Prime the measurement chain on the first pass so the loop does not
         * act on a half-filled filter. */
        c->measurement_raw = ngs_control_convert(&c->cfg, raw);
        (void)filter_sample(c, c->measurement_raw, (float)c->cfg.period_us / 1e6f);

        /* Now that we know what the flow actually is, start the setpoint ramp
         * from it. Doing this at configure() time would use a stale reading. */
        if (c->seed_setpoint) {
            c->setpoint_active = c->measurement;
            c->last_measurement = c->measurement;
            c->seed_setpoint = false;
        }
        return false;
    }

    if (!elapsed(now_us, c->next_us)) {
        return false;
    }

    float dt = (float)c->cfg.period_us / 1e6f;
    c->next_us += c->cfg.period_us;
    if (elapsed(now_us, c->next_us)) {
        /* Fell behind -- resync rather than trying to catch up with a burst of
         * ticks, which would integrate a chunk of error all at once. */
        c->next_us = now_us + c->cfg.period_us;
    }

    c->measurement_raw = ngs_control_convert(&c->cfg, raw);
    (void)filter_sample(c, c->measurement_raw, dt);

    /* Fault check on the filtered value: a single spurious sample should not
     * drop the loop out, but a genuinely open loop stays low. */
    if ((c->cfg.options & NGS_CTRL_OPT_FAULT_CHECK) && c->measurement < c->cfg.fault_below) {
        enter_fault(c, output_pct);
        return true;
    }
    c->flags &= (uint8_t)~NGS_CTRL_FLAG_FAULT;

    c->updates++;

    if (c->mode == NGS_PUMP_MODE_AUTOTUNE) {
        autotune_tick(c, now_us, output_pct);
        return true;
    }

    run_pid(c, dt, output_pct);
    return true;
}

void ngs_control_get_state(const NgsControl *c, NgsControlStatePayload *out)
{
    memset(out, 0, sizeof(*out));
    out->mode = c->mode;
    out->flags = c->flags;
    out->autotune_state = c->autotune.state;
    out->setpoint = c->setpoint_active;
    out->setpoint_target = c->cfg.setpoint;
    out->measurement = c->measurement;
    out->measurement_raw = c->measurement_raw;
    out->output = c->output;
    out->p_term = c->p_term;
    out->i_term = c->i_term;
    out->d_term = c->d_term;
    out->updates = c->updates;
    out->fault_count = c->fault_count;
}
