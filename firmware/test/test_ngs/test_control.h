/*
 * test_control.h - Unity tests for ngs_control.c, run on the board.
 *
 * The controller never touches hardware, so these drive it with scripted
 * measurements and a simulated plant instead. That is the only way to test a
 * controller properly: you cannot put a real pump through a windup scenario a
 * hundred times in five seconds.
 *
 * Included by test_main.cpp rather than compiled separately -- PlatformIO
 * builds one Unity binary per test directory, and splitting the registration
 * across files buys nothing.
 */

#ifndef TEST_CONTROL_H
#define TEST_CONTROL_H

#include <unity.h>

extern "C" {
#include "ngs_control.h"
}

/* --------------------------------------------------------------------------
 * A first-order plant: flow lags the pump output with time constant tau.
 *
 *     d(flow)/dt = (gain * output - flow) / tau
 *
 * Crude, but it has the two properties that matter for testing a controller:
 * it lags, and it saturates. Counts are what the controller actually sees, so
 * the model converts back through the same calibration.
 * ------------------------------------------------------------------------ */
typedef struct {
    float flow;    /* engineering units */
    float gain;    /* units per % output */
    float tau;     /* seconds */
    float noise;   /* amplitude of the deterministic dither below */
    uint32_t step; /* tick counter, drives the dither */
} TestPlant;

static void plant_init(TestPlant *p, float gain, float tau)
{
    p->flow = 0.0f;
    p->gain = gain;
    p->tau = tau;
    p->noise = 0.0f;
    p->step = 0;
}

static uint16_t plant_step(TestPlant *p, float output_pct, float dt, const NgsControlCfgPayload *cfg)
{
    float target = p->gain * output_pct;
    float alpha = dt / (p->tau + dt);
    p->flow += (target - p->flow) * alpha;
    if (p->flow < 0.0f) {
        p->flow = 0.0f;
    }

    /* Deterministic dither rather than a PRNG: a test that fails only on some
     * runs is worse than no test. The sequence still exercises the filters. */
    p->step++;
    float dither = 0.0f;
    if (p->noise > 0.0f) {
        const float pattern[] = {0.3f, -0.7f, 1.0f, -0.2f, 0.6f, -1.0f, 0.1f, -0.5f};
        dither = p->noise * pattern[p->step % 8u];
    }

    float counts = (p->flow + dither) / cfg->cal_scale + cfg->cal_offset;
    if (counts < 0.0f) {
        counts = 0.0f;
    }
    if (counts > 4095.0f) {
        counts = 4095.0f;
    }
    return (uint16_t)counts;
}

/* Runs the loop for `seconds`, returning the final output. */
static float run_loop(NgsControl *c, TestPlant *plant, float seconds, uint32_t *now_us)
{
    float dt = (float)c->cfg.period_us / 1e6f;
    uint32_t ticks = (uint32_t)(seconds / dt);
    float output = c->output;
    uint16_t raw = plant_step(plant, output, 0.0f, &c->cfg);

    for (uint32_t i = 0; i < ticks; i++) {
        *now_us += c->cfg.period_us;
        if (ngs_control_tick(c, *now_us, raw, &output)) {
            raw = plant_step(plant, output, dt, &c->cfg);
        }
    }
    return output;
}

/* A configuration matching the real bench: 12-bit ADC, 0.6-3.0 V mapped to
 * 0-600 mL/min. cal_offset is the count at 0 mL/min, cal_scale the units per
 * count over that span. */
static void bench_cfg(NgsControlCfgPayload *cfg)
{
    ngs_control_defaults(cfg);
    cfg->mode = NGS_PUMP_MODE_AUTO;
    cfg->channel = 13;
    cfg->cal_offset = 744.0f;   /* 0.6 V */
    cfg->cal_scale = 0.2016f;   /* (600 mL/min) / (3722 - 744 counts) */
    cfg->period_us = 20000u;    /* 50 Hz */
    cfg->setpoint_slew = 0.0f;  /* most tests want the step, not the ramp */
    cfg->output_slew = 0.0f;
    cfg->filter_tau_s = 0.0f;   /* enabled explicitly where it is the subject */
}

/* -- unit conversion ------------------------------------------------------ */

static void test_control_converts_counts_to_units(void)
{
    NgsControlCfgPayload cfg;
    bench_cfg(&cfg);

    TEST_ASSERT_FLOAT_WITHIN(0.5f, 0.0f, ngs_control_convert(&cfg, 744));
    TEST_ASSERT_FLOAT_WITHIN(2.0f, 600.0f, ngs_control_convert(&cfg, 3722));
    TEST_ASSERT_FLOAT_WITHIN(2.0f, 300.0f, ngs_control_convert(&cfg, 2233));
}

/* -- configuration -------------------------------------------------------- */

static void test_control_rejects_bad_configuration(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.out_min = 50.0f;
    cfg.out_max = 20.0f; /* inverted */
    TEST_ASSERT_EQUAL(NGS_ERR_BAD_ARGUMENT, ngs_control_configure(&c, &cfg, 0.0f));

    bench_cfg(&cfg);
    cfg.kp = -1.0f; /* positive feedback */
    TEST_ASSERT_EQUAL(NGS_ERR_BAD_ARGUMENT, ngs_control_configure(&c, &cfg, 0.0f));

    bench_cfg(&cfg);
    cfg.channel = 99;
    TEST_ASSERT_EQUAL(NGS_ERR_BAD_ARGUMENT, ngs_control_configure(&c, &cfg, 0.0f));

    bench_cfg(&cfg);
    cfg.cal_scale = 0.0f; /* every reading would be zero units */
    TEST_ASSERT_EQUAL(NGS_ERR_BAD_ARGUMENT, ngs_control_configure(&c, &cfg, 0.0f));

    bench_cfg(&cfg);
    cfg.period_us = 10u; /* faster than the ADC can be read */
    TEST_ASSERT_EQUAL(NGS_ERR_BAD_ARGUMENT, ngs_control_configure(&c, &cfg, 0.0f));
}

static void test_control_accepts_the_bench_configuration(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    ngs_control_init(&c);
    bench_cfg(&cfg);
    TEST_ASSERT_EQUAL(0, ngs_control_configure(&c, &cfg, 0.0f));
    TEST_ASSERT_EQUAL(NGS_PUMP_MODE_AUTO, c.mode);
}

/* -- closed loop ---------------------------------------------------------- */

static void test_control_reaches_the_setpoint(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    TestPlant plant;
    uint32_t now = 1000000u;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.setpoint = 300.0f;
    ngs_control_configure(&c, &cfg, 0.0f);
    plant_init(&plant, 6.0f, 1.0f); /* 100 % -> 600 mL/min, 1 s lag */

    run_loop(&c, &plant, 60.0f, &now);

    NgsControlStatePayload st;
    ngs_control_get_state(&c, &st);
    TEST_ASSERT_FLOAT_WITHIN(5.0f, 300.0f, st.measurement);
}

static void test_control_holds_setpoint_against_a_disturbance(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    TestPlant plant;
    uint32_t now = 1000000u;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.setpoint = 300.0f;
    ngs_control_configure(&c, &cfg, 0.0f);
    plant_init(&plant, 6.0f, 1.0f);
    run_loop(&c, &plant, 60.0f, &now);

    /* The pump gets weaker -- a partly blocked line, say. */
    plant.gain = 4.0f;
    run_loop(&c, &plant, 90.0f, &now);

    NgsControlStatePayload st;
    ngs_control_get_state(&c, &st);
    TEST_ASSERT_FLOAT_WITHIN(8.0f, 300.0f, st.measurement);
}

static void test_control_settles_without_sustained_oscillation(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    TestPlant plant;
    uint32_t now = 1000000u;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.setpoint = 300.0f;
    ngs_control_configure(&c, &cfg, 0.0f);
    plant_init(&plant, 6.0f, 1.0f);
    run_loop(&c, &plant, 60.0f, &now);

    /* Sample the flow over ten seconds once settled: a loop that is ringing
     * shows up as spread here even when the average looks perfect. */
    float lo = 1e9f, hi = -1e9f;
    float dt = (float)cfg.period_us / 1e6f;
    float output = c.output;
    uint16_t raw = plant_step(&plant, output, 0.0f, &cfg);
    for (uint32_t i = 0; i < 500u; i++) {
        now += cfg.period_us;
        if (ngs_control_tick(&c, now, raw, &output)) {
            raw = plant_step(&plant, output, dt, &cfg);
        }
        if (plant.flow < lo) {
            lo = plant.flow;
        }
        if (plant.flow > hi) {
            hi = plant.flow;
        }
    }
    TEST_ASSERT_TRUE_MESSAGE((hi - lo) < 5.0f, "loop is oscillating once settled");
}

static void test_control_handles_a_setpoint_step(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    TestPlant plant;
    uint32_t now = 1000000u;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.setpoint = 150.0f;
    ngs_control_configure(&c, &cfg, 0.0f);
    plant_init(&plant, 6.0f, 1.0f);
    run_loop(&c, &plant, 60.0f, &now);

    /* Step it to double, then check both that it gets there and that it did
     * not massively overshoot on the way. */
    cfg.setpoint = 300.0f;
    ngs_control_configure(&c, &cfg, c.output);

    float peak = 0.0f;
    float dt = (float)cfg.period_us / 1e6f;
    float output = c.output;
    uint16_t raw = plant_step(&plant, output, 0.0f, &cfg);
    for (uint32_t i = 0; i < 4000u; i++) {
        now += cfg.period_us;
        if (ngs_control_tick(&c, now, raw, &output)) {
            raw = plant_step(&plant, output, dt, &cfg);
        }
        if (plant.flow > peak) {
            peak = plant.flow;
        }
    }

    NgsControlStatePayload st;
    ngs_control_get_state(&c, &st);
    TEST_ASSERT_FLOAT_WITHIN(6.0f, 300.0f, st.measurement);
    TEST_ASSERT_TRUE_MESSAGE(peak < 345.0f, "overshoot above 15 % on a setpoint step");
}

/* -- anti-windup ---------------------------------------------------------- */

static void test_control_does_not_wind_up_against_an_unreachable_setpoint(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    TestPlant plant;
    uint32_t now = 1000000u;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.setpoint = 900.0f; /* the plant tops out at 600 */
    ngs_control_configure(&c, &cfg, 0.0f);
    plant_init(&plant, 6.0f, 1.0f);

    run_loop(&c, &plant, 120.0f, &now); /* two minutes pinned at the limit */

    NgsControlStatePayload st;
    ngs_control_get_state(&c, &st);
    TEST_ASSERT_TRUE((st.flags & NGS_CTRL_FLAG_SATURATED) != 0u);
    TEST_ASSERT_TRUE_MESSAGE(st.i_term <= 100.5f, "integral wound past the output range");

    /* Now ask for something reachable. A wound-up integrator would hold the
     * output at 100 % for a long time before the flow came down. */
    cfg.setpoint = 150.0f;
    ngs_control_configure(&c, &cfg, c.output);
    run_loop(&c, &plant, 30.0f, &now);

    ngs_control_get_state(&c, &st);
    TEST_ASSERT_FLOAT_WITHIN(15.0f, 150.0f, st.measurement);
}

static void test_control_deadband_stops_integration(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    TestPlant plant;
    uint32_t now = 1000000u;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.setpoint = 300.0f;
    /* Wider than the largest error the run can produce -- the flow starts at
     * zero, so the error starts at the full 300. An earlier version of this
     * test used 200 and integrated for the first third of the run. */
    cfg.deadband = 400.0f;
    ngs_control_configure(&c, &cfg, 20.0f);
    plant_init(&plant, 6.0f, 1.0f);

    run_loop(&c, &plant, 20.0f, &now);

    NgsControlStatePayload st;
    ngs_control_get_state(&c, &st);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 20.0f, st.i_term);
}

/* -- transfer and limits -------------------------------------------------- */

static void test_control_transfer_to_auto_is_bumpless(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    uint32_t now = 1000000u;
    float output = 0.0f;

    ngs_control_init(&c);
    ngs_control_note_manual_output(&c, 42.0f);

    bench_cfg(&cfg);
    cfg.setpoint = 300.0f;
    /* The ramp has to be on for this to mean anything: with setpoint_slew at
     * 0 the setpoint jumps straight to its target and a badly seeded ramp
     * never shows up. */
    cfg.setpoint_slew = 60.0f;
    TEST_ASSERT_EQUAL(0, ngs_control_configure(&c, &cfg, 42.0f));

    /* Already flowing at the setpoint when auto is engaged -- the case that
     * matters. An earlier version fed 744 counts (zero flow) here and passed
     * while the setpoint ramp was being seeded from a stale zero, because
     * zero happened to be right. At 300 the bug drove the output to nothing. */
    uint16_t at_setpoint = (uint16_t)(300.0f / cfg.cal_scale + cfg.cal_offset);
    now += cfg.period_us;
    ngs_control_tick(&c, now, at_setpoint, &output);
    now += cfg.period_us;
    ngs_control_tick(&c, now, at_setpoint, &output);

    TEST_ASSERT_TRUE_MESSAGE(output > 30.0f, "output jumped down on entering auto");
    TEST_ASSERT_TRUE_MESSAGE(output < 70.0f, "output jumped up on entering auto");
}

static void test_control_respects_output_limits(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    TestPlant plant;
    uint32_t now = 1000000u;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.setpoint = 900.0f;
    cfg.out_max = 60.0f;
    ngs_control_configure(&c, &cfg, 0.0f);
    plant_init(&plant, 6.0f, 1.0f);

    float output = run_loop(&c, &plant, 60.0f, &now);
    TEST_ASSERT_TRUE(output <= 60.0f + 0.01f);
}

static void test_control_output_slew_is_respected(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    uint32_t now = 1000000u;
    float output = 0.0f;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.setpoint = 600.0f;
    cfg.output_slew = 10.0f; /* %/s */
    ngs_control_configure(&c, &cfg, 0.0f);

    now += cfg.period_us;
    ngs_control_tick(&c, now, 744, &output);
    /* One second of ticks, reading zero flow the whole time: the controller
     * wants full output immediately and must not be allowed to take it. */
    for (uint32_t i = 0; i < 50u; i++) {
        now += cfg.period_us;
        ngs_control_tick(&c, now, 744, &output);
    }
    TEST_ASSERT_TRUE_MESSAGE(output <= 11.0f, "output moved faster than the slew limit");
}

static void test_control_setpoint_slew_ramps(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    uint32_t now = 1000000u;
    float output = 0.0f;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.setpoint = 600.0f;
    cfg.setpoint_slew = 60.0f; /* units/s: ten seconds to full scale */
    ngs_control_configure(&c, &cfg, 0.0f);

    now += cfg.period_us;
    ngs_control_tick(&c, now, 744, &output);
    for (uint32_t i = 0; i < 50u; i++) { /* one second */
        now += cfg.period_us;
        ngs_control_tick(&c, now, 744, &output);
    }

    NgsControlStatePayload st;
    ngs_control_get_state(&c, &st);
    TEST_ASSERT_FLOAT_WITHIN(15.0f, 60.0f, st.setpoint);
    TEST_ASSERT_EQUAL_FLOAT(600.0f, st.setpoint_target);
    TEST_ASSERT_TRUE((st.flags & NGS_CTRL_FLAG_SLEWING) != 0u);
}

/* -- measurement filtering ------------------------------------------------ */

static void test_control_median_filter_rejects_a_spike(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    uint32_t now = 1000000u;
    float output = 0.0f;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.setpoint = 300.0f;
    cfg.filter_tau_s = 0.0f; /* isolate the median stage */
    ngs_control_configure(&c, &cfg, 0.0f);

    /* Steady at ~300, then one wild sample. */
    uint16_t steady = (uint16_t)(300.0f / cfg.cal_scale + cfg.cal_offset);
    for (uint32_t i = 0; i < 10u; i++) {
        now += cfg.period_us;
        ngs_control_tick(&c, now, steady, &output);
    }
    now += cfg.period_us;
    ngs_control_tick(&c, now, 4095, &output); /* the spike */

    NgsControlStatePayload st;
    ngs_control_get_state(&c, &st);
    TEST_ASSERT_FLOAT_WITHIN(5.0f, 300.0f, st.measurement);
    TEST_ASSERT_TRUE_MESSAGE(st.measurement_raw > 600.0f, "raw should show the spike");
}

static void test_control_low_pass_smooths_noise(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    TestPlant plant;
    uint32_t now = 1000000u;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.setpoint = 300.0f;
    cfg.filter_tau_s = 1.0f;
    ngs_control_configure(&c, &cfg, 0.0f);
    plant_init(&plant, 6.0f, 1.0f);
    plant.noise = 25.0f; /* +/- 25 mL/min of dither on every sample */

    run_loop(&c, &plant, 60.0f, &now);

    /* The output must not be chasing the noise. Watch how far it travels over
     * a few seconds once settled. */
    float lo = 1e9f, hi = -1e9f;
    float dt = (float)cfg.period_us / 1e6f;
    float output = c.output;
    uint16_t raw = plant_step(&plant, output, 0.0f, &cfg);
    for (uint32_t i = 0; i < 250u; i++) {
        now += cfg.period_us;
        if (ngs_control_tick(&c, now, raw, &output)) {
            raw = plant_step(&plant, output, dt, &cfg);
        }
        if (output < lo) {
            lo = output;
        }
        if (output > hi) {
            hi = output;
        }
    }
    TEST_ASSERT_TRUE_MESSAGE((hi - lo) < 6.0f, "output is chasing sensor noise");
}

/* -- sensor fault --------------------------------------------------------- */

static void test_control_drops_out_on_a_sensor_fault(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    uint32_t now = 1000000u;
    float output = 0.0f;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.setpoint = 300.0f;
    /* Negative on purpose: under 4 mA reads below zero flow. This is the case
     * that exposed "0 disables the check" as unusable -- the threshold you
     * actually want on a current loop is on the wrong side of the sentinel. */
    cfg.options |= NGS_CTRL_OPT_FAULT_CHECK;
    cfg.fault_below = -10.0f;
    ngs_control_configure(&c, &cfg, 30.0f);

    now += cfg.period_us;
    ngs_control_tick(&c, now, 744, &output);

    /* The transmitter loses power: counts collapse toward zero. */
    for (uint32_t i = 0; i < 10u; i++) {
        now += cfg.period_us;
        ngs_control_tick(&c, now, 0, &output);
    }

    NgsControlStatePayload st;
    ngs_control_get_state(&c, &st);
    TEST_ASSERT_EQUAL(NGS_PUMP_MODE_MANUAL, st.mode);
    TEST_ASSERT_TRUE((st.flags & NGS_CTRL_FLAG_FAULT) != 0u);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, output);
    TEST_ASSERT_TRUE(st.fault_count >= 1u);
}

/* -- tuning rules --------------------------------------------------------- */

static void test_tuning_rules_are_ordered_by_aggressiveness(void)
{
    float ku = 2.0f, tu = 4.0f;
    float zn_kp, zn_ki, zn_kd, tl_kp, tl_ki, tl_kd;

    ngs_control_apply_rule(NGS_AT_RULE_ZIEGLER_NICHOLS, ku, tu, &zn_kp, &zn_ki, &zn_kd);
    ngs_control_apply_rule(NGS_AT_RULE_TYREUS_LUYBEN, ku, tu, &tl_kp, &tl_ki, &tl_kd);

    /* Tyreus-Luyben is the conservative one, which is why it is the default. */
    TEST_ASSERT_TRUE(tl_kp < zn_kp);
    TEST_ASSERT_TRUE(tl_ki < zn_ki);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, tl_kd);
}

/* -- autotune ------------------------------------------------------------- */

static void test_autotune_validates_its_arguments(void)
{
    NgsControl c;
    NgsAutotuneCmdPayload cmd;
    ngs_control_init(&c);

    memset(&cmd, 0, sizeof(cmd));
    cmd.action = NGS_AT_ACTION_START;
    cmd.cycles = 4;
    cmd.setpoint = 300.0f;
    cmd.amplitude = 0.0f; /* no relay step at all */
    cmd.timeout_ms = 60000u;
    TEST_ASSERT_EQUAL(NGS_ERR_BAD_ARGUMENT, ngs_control_autotune(&c, &cmd, 0, 0.0f));

    cmd.amplitude = 10.0f;
    cmd.cycles = 1; /* cannot measure a period from one crossing */
    TEST_ASSERT_EQUAL(NGS_ERR_BAD_ARGUMENT, ngs_control_autotune(&c, &cmd, 0, 0.0f));

    cmd.cycles = 4;
    cmd.timeout_ms = 10u; /* would expire before the first cycle */
    TEST_ASSERT_EQUAL(NGS_ERR_BAD_ARGUMENT, ngs_control_autotune(&c, &cmd, 0, 0.0f));

    cmd.timeout_ms = 60000u;
    TEST_ASSERT_EQUAL(0, ngs_control_autotune(&c, &cmd, 0, 20.0f));
    TEST_ASSERT_EQUAL(NGS_PUMP_MODE_AUTOTUNE, c.mode);
}

static void test_autotune_measures_the_limit_cycle(void)
{
    NgsControl c;
    NgsControlCfgPayload cfg;
    NgsAutotuneCmdPayload cmd;
    TestPlant plant;
    uint32_t now = 1000000u;
    float output = 30.0f;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.mode = NGS_PUMP_MODE_MANUAL;
    ngs_control_configure(&c, &cfg, 30.0f);
    plant_init(&plant, 6.0f, 1.0f);
    plant.flow = 180.0f; /* already at the operating point */

    memset(&cmd, 0, sizeof(cmd));
    cmd.action = NGS_AT_ACTION_START;
    cmd.cycles = 4;
    cmd.rule = NGS_AT_RULE_TYREUS_LUYBEN;
    cmd.setpoint = 180.0f;
    cmd.amplitude = 15.0f;
    cmd.hysteresis = 3.0f;
    cmd.timeout_ms = 120000u;
    TEST_ASSERT_EQUAL(0, ngs_control_autotune(&c, &cmd, now, 30.0f));

    float dt = (float)cfg.period_us / 1e6f;
    uint16_t raw = plant_step(&plant, output, 0.0f, &cfg);
    for (uint32_t i = 0; i < 20000u; i++) {
        now += cfg.period_us;
        if (ngs_control_tick(&c, now, raw, &output)) {
            raw = plant_step(&plant, output, dt, &cfg);
        }
        if (c.autotune.state == NGS_AT_DONE || c.autotune.state == NGS_AT_FAILED) {
            break;
        }
    }

    NgsAutotuneResultPayload res;
    ngs_control_get_autotune(&c, &res);
    TEST_ASSERT_EQUAL_MESSAGE(NGS_AT_DONE, res.state, "autotune did not complete");
    TEST_ASSERT_TRUE_MESSAGE(res.tu > 0.0f, "no ultimate period measured");
    TEST_ASSERT_TRUE_MESSAGE(res.ku > 0.0f, "no ultimate gain measured");
    TEST_ASSERT_TRUE_MESSAGE(res.kp > 0.0f, "no gains suggested");
    /* It must hand the pump back, not leave it oscillating. */
    TEST_ASSERT_EQUAL(NGS_PUMP_MODE_MANUAL, c.mode);
}

static void test_autotune_gains_actually_control_the_plant(void)
{
    /* The real test of a tuner: feed its own numbers back in and see whether
     * the loop is stable and lands on the setpoint. */
    NgsControl c;
    NgsControlCfgPayload cfg;
    NgsAutotuneCmdPayload cmd;
    TestPlant plant;
    uint32_t now = 1000000u;
    float output = 30.0f;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.mode = NGS_PUMP_MODE_MANUAL;
    ngs_control_configure(&c, &cfg, 30.0f);
    plant_init(&plant, 6.0f, 1.0f);
    plant.flow = 180.0f;

    memset(&cmd, 0, sizeof(cmd));
    cmd.action = NGS_AT_ACTION_START;
    cmd.cycles = 4;
    cmd.rule = NGS_AT_RULE_TYREUS_LUYBEN;
    cmd.setpoint = 180.0f;
    cmd.amplitude = 15.0f;
    cmd.hysteresis = 3.0f;
    cmd.timeout_ms = 120000u;
    ngs_control_autotune(&c, &cmd, now, 30.0f);

    float dt = (float)cfg.period_us / 1e6f;
    uint16_t raw = plant_step(&plant, output, 0.0f, &cfg);
    for (uint32_t i = 0; i < 20000u; i++) {
        now += cfg.period_us;
        if (ngs_control_tick(&c, now, raw, &output)) {
            raw = plant_step(&plant, output, dt, &cfg);
        }
        if (c.autotune.state == NGS_AT_DONE || c.autotune.state == NGS_AT_FAILED) {
            break;
        }
    }

    NgsAutotuneResultPayload res;
    ngs_control_get_autotune(&c, &res);
    TEST_ASSERT_EQUAL(NGS_AT_DONE, res.state);

    cfg.mode = NGS_PUMP_MODE_AUTO;
    cfg.kp = res.kp;
    cfg.ki = res.ki;
    cfg.kd = res.kd;
    cfg.setpoint = 300.0f;
    TEST_ASSERT_EQUAL(0, ngs_control_configure(&c, &cfg, c.output));

    run_loop(&c, &plant, 180.0f, &now);

    NgsControlStatePayload st;
    ngs_control_get_state(&c, &st);
    TEST_ASSERT_FLOAT_WITHIN_MESSAGE(15.0f, 300.0f, st.measurement,
                                     "autotuned gains do not reach the setpoint");
}

static void test_autotune_aborts_cleanly(void)
{
    NgsControl c;
    NgsAutotuneCmdPayload cmd;
    ngs_control_init(&c);

    memset(&cmd, 0, sizeof(cmd));
    cmd.action = NGS_AT_ACTION_START;
    cmd.cycles = 4;
    cmd.setpoint = 300.0f;
    cmd.amplitude = 10.0f;
    cmd.timeout_ms = 60000u;
    ngs_control_autotune(&c, &cmd, 0, 20.0f);

    cmd.action = NGS_AT_ACTION_ABORT;
    TEST_ASSERT_EQUAL(0, ngs_control_autotune(&c, &cmd, 1000, 20.0f));

    NgsAutotuneResultPayload res;
    ngs_control_get_autotune(&c, &res);
    TEST_ASSERT_EQUAL(NGS_AT_FAILED, res.state);
    TEST_ASSERT_EQUAL(NGS_AT_FAIL_ABORTED, res.fail_reason);
    TEST_ASSERT_EQUAL(NGS_PUMP_MODE_MANUAL, c.mode);
}

static void test_autotune_reports_a_dead_process(void)
{
    /* Nothing moves no matter what the relay does -- a closed valve, say. The
     * tuner must say so rather than invent gains from noise. */
    NgsControl c;
    NgsControlCfgPayload cfg;
    NgsAutotuneCmdPayload cmd;
    uint32_t now = 1000000u;
    float output = 20.0f;

    ngs_control_init(&c);
    bench_cfg(&cfg);
    cfg.mode = NGS_PUMP_MODE_MANUAL;
    ngs_control_configure(&c, &cfg, 20.0f);

    memset(&cmd, 0, sizeof(cmd));
    cmd.action = NGS_AT_ACTION_START;
    cmd.cycles = 3;
    cmd.setpoint = 100.0f;
    cmd.amplitude = 10.0f;
    cmd.hysteresis = 5.0f;
    cmd.timeout_ms = 5000u;
    ngs_control_autotune(&c, &cmd, now, 20.0f);

    uint16_t flat = (uint16_t)(100.0f / cfg.cal_scale + cfg.cal_offset);
    for (uint32_t i = 0; i < 2000u; i++) {
        now += cfg.period_us;
        ngs_control_tick(&c, now, flat, &output);
        if (c.autotune.state == NGS_AT_DONE || c.autotune.state == NGS_AT_FAILED) {
            break;
        }
    }

    NgsAutotuneResultPayload res;
    ngs_control_get_autotune(&c, &res);
    TEST_ASSERT_EQUAL(NGS_AT_FAILED, res.state);
    TEST_ASSERT_EQUAL(NGS_PUMP_MODE_MANUAL, c.mode);
}

/* -- the shared vector ---------------------------------------------------- */

/* The same scenario and the same expected numbers as
 * host/tests/test_control_vector.py. host/ngs_host/control.py mirrors this
 * file so the simulator behaves like the board; duplicated logic drifts, and
 * this is what catches it. If the two disagree, this one is right.
 *
 * See that file for why this particular sequence: bumpless entry, the median
 * prefilter eating a full-scale spike, and the error changing sign. */
static void test_control_vector(void)
{
    static const uint16_t raw[] = {744, 1000, 1500, 2000, 2233, 2233,
                                   2500, 3000, 2233, 4095, 2233, 2233};
    static const float expected[] = {32.518876f, 32.618232f, 27.637268f, 27.696305f, 22.675021f, 20.326308f, 20.326235f, 20.326162f, 17.613198f, 17.591594f, 20.282881f};

    NgsControl c;
    NgsControlCfgPayload cfg;
    ngs_control_init(&c);
    ngs_control_defaults(&cfg);

    cfg.mode = NGS_PUMP_MODE_AUTO;
    cfg.channel = 13;
    cfg.options = 0;
    cfg.setpoint = 300.0f;
    cfg.kp = 0.05f;
    cfg.ki = 0.02f;
    cfg.kd = 0.0f;
    cfg.out_min = 0.0f;
    cfg.out_max = 100.0f;
    cfg.filter_tau_s = 0.0f; /* median only, so the vector is exactly reproducible */
    cfg.deadband = 0.0f;
    cfg.setpoint_slew = 0.0f;
    cfg.output_slew = 0.0f;
    cfg.cal_scale = 0.2016f;
    cfg.cal_offset = 744.0f;
    cfg.fault_below = 0.0f;
    cfg.period_us = 20000u;

    TEST_ASSERT_EQUAL(0, ngs_control_configure(&c, &cfg, 20.0f));
    TEST_ASSERT_EQUAL_FLOAT(20.0f, c.integral); /* seeded from the manual duty */

    uint32_t now = 1000000u;
    float output = 0.0f;
    uint8_t produced = 0;

    for (uint8_t i = 0; i < sizeof(raw) / sizeof(raw[0]); i++) {
        now += cfg.period_us;
        if (ngs_control_tick(&c, now, raw[i], &output)) {
            TEST_ASSERT_TRUE_MESSAGE(produced < sizeof(expected) / sizeof(expected[0]),
                                     "more outputs than the vector expects");
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(1e-3f, expected[produced], output,
                                             "output diverged from the shared vector");
            produced++;
        } else {
            TEST_ASSERT_EQUAL_MESSAGE(0, i, "only the first tick may produce no output");
        }
    }
    TEST_ASSERT_EQUAL_UINT8(sizeof(expected) / sizeof(expected[0]), produced);
}

static void register_control_tests(void)
{
    RUN_TEST(test_control_converts_counts_to_units);
    RUN_TEST(test_control_rejects_bad_configuration);
    RUN_TEST(test_control_accepts_the_bench_configuration);
    RUN_TEST(test_control_reaches_the_setpoint);
    RUN_TEST(test_control_holds_setpoint_against_a_disturbance);
    RUN_TEST(test_control_settles_without_sustained_oscillation);
    RUN_TEST(test_control_handles_a_setpoint_step);
    RUN_TEST(test_control_does_not_wind_up_against_an_unreachable_setpoint);
    RUN_TEST(test_control_deadband_stops_integration);
    RUN_TEST(test_control_transfer_to_auto_is_bumpless);
    RUN_TEST(test_control_respects_output_limits);
    RUN_TEST(test_control_output_slew_is_respected);
    RUN_TEST(test_control_setpoint_slew_ramps);
    RUN_TEST(test_control_median_filter_rejects_a_spike);
    RUN_TEST(test_control_low_pass_smooths_noise);
    RUN_TEST(test_control_drops_out_on_a_sensor_fault);
    RUN_TEST(test_tuning_rules_are_ordered_by_aggressiveness);
    RUN_TEST(test_autotune_validates_its_arguments);
    RUN_TEST(test_autotune_measures_the_limit_cycle);
    RUN_TEST(test_autotune_gains_actually_control_the_plant);
    RUN_TEST(test_autotune_aborts_cleanly);
    RUN_TEST(test_autotune_reports_a_dead_process);
    RUN_TEST(test_control_vector);
}

#endif /* TEST_CONTROL_H */
