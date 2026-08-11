/*
 * ngs_control.h - Closed-loop pump control. Pure C, no hardware, no I/O.
 *
 * The caller reads the ADC and writes the PWM; this decides what to write.
 * That keeps the whole loop testable off-target with scripted measurements,
 * which is the only practical way to test a controller -- you cannot step a
 * real pump through a thousand scenarios in a unit test.
 *
 * Design notes, all of which exist because a bench pump is not a simulation:
 *
 *   Filtering       A median-of-5 prefilter kills the isolated spikes an ADC
 *                   on a long wire produces, then a first-order low-pass
 *                   smooths what is left. Median first: an IIR filter smears a
 *                   spike out over its whole time constant instead of
 *                   removing it.
 *
 *   Derivative      Taken on the measurement, not the error, so a setpoint
 *                   step does not produce a derivative spike into the output.
 *                   Defaults to zero: on a noisy flow signal, D amplifies
 *                   noise far more reliably than it improves response.
 *
 *   Anti-windup     Conditional integration -- the integral only accumulates
 *                   when doing so does not drive further into a limit -- plus
 *                   a hard clamp on the integral term itself. Both, because
 *                   the clamp alone still lets the term sit pinned at the
 *                   limit long after the error reverses.
 *
 *   Bumpless        Entering AUTO seeds the integrator from the output already
 *                   applied, so the pump does not jump when the mode changes.
 *
 *   Setpoint slew   A step change is ramped at a configurable rate. This is
 *                   the honest way to handle "operator typed 500": the loop
 *                   sees a trajectory it can follow instead of an error step
 *                   that saturates the output and charges the integrator.
 *
 *   Sensor fault    A reading below `fault_below` means the 4-20 mA loop is
 *                   open, not that the flow is low. Chasing it would ramp the
 *                   pump to full against a sensor that cannot report back, so
 *                   the loop drops to manual at the fail-safe output and makes
 *                   the operator re-enable it.
 */

#ifndef NGS_CONTROL_H
#define NGS_CONTROL_H

#include <stdbool.h>
#include <stdint.h>

#include "ngs_protocol.h"

#ifdef __cplusplus
extern "C" {
#endif

#define NGS_MEDIAN_TAPS 5u

/* Autotune needs a bounded history to average over. Eight limit cycles is far
 * more than the 3-5 worth trusting, and costs 64 bytes. */
#define NGS_AT_MAX_CYCLES 8u

typedef struct {
    /* Relay experiment state. */
    uint8_t state;       /* NGS_AT_* */
    uint8_t fail_reason; /* NGS_AT_FAIL_* */
    uint8_t rule;        /* NGS_AT_RULE_* */
    uint8_t want_cycles;
    bool relay_high;

    float setpoint;
    float amplitude;
    float hysteresis;
    float bias; /* output the experiment oscillates about */

    uint32_t started_us;
    uint32_t timeout_us;
    uint32_t last_cross_us;

    /* Per-cycle observations. */
    uint32_t periods_us[NGS_AT_MAX_CYCLES];
    float peaks[NGS_AT_MAX_CYCLES];    /* max while the relay was high */
    float troughs[NGS_AT_MAX_CYCLES];  /* min while the relay was low  */
    uint8_t cycles;

    float peak_acc;   /* running extreme within the current cycle */
    float trough_acc;

    /* Settle phase: hold the bias, let the process stop moving, and watch how
     * much the reading wanders while it does. That wander is the noise floor,
     * and it is what the relay band has to clear. */
    uint32_t settle_us;
    float settle_min;
    float settle_max;
    float noise;

    /* Results, valid once state == NGS_AT_DONE. */
    float ku;
    float tu;
    float measured_amplitude;
    float spread;
    float kp;
    float ki;
    float kd;
} NgsAutotune;

typedef struct {
    NgsControlCfgPayload cfg;

    uint8_t mode;  /* NGS_PUMP_MODE_* */
    uint8_t flags; /* NGS_CTRL_FLAG_* */

    /* Measurement chain. */
    float median_taps[NGS_MEDIAN_TAPS];
    uint8_t median_count;
    uint8_t median_pos;
    float measurement;     /* filtered */
    float measurement_raw; /* this tick's sample, unfiltered */
    bool filter_primed;
    /* Set when entering AUTO, cleared on the first tick. The setpoint ramp has
     * to start from the *current* flow, but at the moment the mode changes the
     * measurement chain has not run yet -- it only runs while the loop is
     * active -- so the value available then is stale. Seeding on the first
     * tick, after the filter is primed, is the difference between a bumpless
     * transfer and the pump slamming to a limit. */
    bool seed_setpoint;

    /* Loop state. */
    float setpoint_active;
    /* Demand, 0-100 %, before the deadzone mapping. The loop reasons in this
     * space -- limits, integral, slew -- and only the emitted duty is mapped,
     * so a pump that does nothing below 20 % does not appear to the controller
     * as a fifth of its range being dead. */
    float demand;
    float integral;
    float last_measurement;
    float output;
    float p_term;
    float i_term;
    float d_term;

    uint32_t next_us;
    uint32_t updates;
    uint32_t fault_count;

    NgsAutotune autotune;
} NgsControl;

/* Sensible defaults for a slow flow loop: conservative gains, a one-second
 * measurement filter, and setpoint ramping that crosses full scale in about
 * ten seconds. Tuned for not surprising anyone rather than for performance. */
void ngs_control_defaults(NgsControlCfgPayload *cfg);

void ngs_control_init(NgsControl *c);

/* Apply a configuration. Returns an NGS_ERR_* code, or 0.
 *
 * `current_output` is the duty applied right now; switching MANUAL -> AUTO
 * seeds the integrator from it so the transfer is bumpless.
 */
int ngs_control_configure(NgsControl *c, const NgsControlCfgPayload *cfg, float current_output);

/* Called from the poll loop. `now_us` is the current time and `raw` the latest
 * ADC reading for the configured channel. Returns true when `*output_pct` has
 * been updated and the caller should write it to the PWM.
 *
 * Returns false when the loop is idle or the period has not elapsed -- so a
 * manual-mode caller can ignore it entirely.
 */
bool ngs_control_tick(NgsControl *c, uint32_t now_us, uint16_t raw, float *output_pct);

/* Manual mode bookkeeping: tell the controller what the operator set, so a
 * later switch to AUTO can pick up from there. */
void ngs_control_note_manual_output(NgsControl *c, float output_pct);

void ngs_control_get_state(const NgsControl *c, NgsControlStatePayload *out);

/* Start or abort a relay autotune. Returns an NGS_ERR_* code, or 0. */
int ngs_control_autotune(NgsControl *c, const NgsAutotuneCmdPayload *cmd, uint32_t now_us,
                         float current_output);

void ngs_control_get_autotune(const NgsControl *c, NgsAutotuneResultPayload *out);

/* Exposed for tests: counts -> engineering units, and the tuning rules. */
float ngs_control_convert(const NgsControlCfgPayload *cfg, uint16_t raw);
void ngs_control_apply_rule(uint8_t rule, float ku, float tu, float *kp, float *ki, float *kd);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* NGS_CONTROL_H */
