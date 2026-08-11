/*
 * test_estop.h - Unity tests for the emergency stop, run on the board.
 *
 * The host has its own tests for this, but they run against fake.py. These are
 * the ones that matter: they exercise the C that actually drives the pins, and
 * in particular the two things a host-side stop cannot do for itself -- the
 * latch, and the watchdog that fires when no host is talking at all.
 *
 * Included by test_main.cpp, which supplies send_request/round_trip/g_fake.
 */

#ifndef TEST_ESTOP_H
#define TEST_ESTOP_H

#include <unity.h>

/* Registers valve-like pin 32 and pump-like PWM pin 33 as the safe state, and
 * sets the watchdog. Mirrors what Bench.register_safe_state() sends. */
static void register_safe_state(uint32_t watchdog_ms)
{
    NgsFrame resp;
    NgsSafeEntryPayload e;

    memset(&e, 0, sizeof(e));
    e.index = NGS_SAFE_INDEX_CLEAR;
    e.watchdog_ms = watchdog_ms;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_SAFE_ENTRY, 1, &e, sizeof(e), &resp));

    memset(&e, 0, sizeof(e));
    e.index = 0;
    e.kind = NGS_SAFE_KIND_GPIO;
    e.pin = 32;
    e.value = 0;
    e.watchdog_ms = watchdog_ms;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_SAFE_ENTRY, 2, &e, sizeof(e), &resp));

    memset(&e, 0, sizeof(e));
    e.index = 1;
    e.kind = NGS_SAFE_KIND_PWM;
    e.pin = 33;
    e.value = 0;
    e.resolution = 12;
    e.watchdog_ms = watchdog_ms;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_SAFE_ENTRY, 3, &e, sizeof(e), &resp));
}

static bool send_estop(uint8_t action)
{
    NgsEstopCmdPayload cmd;
    NgsFrame resp;
    memset(&cmd, 0, sizeof(cmd));
    cmd.action = action;
    if (!round_trip(NGS_MSG_ESTOP, 9, &cmd, sizeof(cmd), &resp)) {
        return false;
    }
    return resp.type == (NGS_MSG_ESTOP | NGS_MSG_RESP);
}

/* Drives pin 32 high and the PWM to something non-zero, the state an
 * emergency stop has to undo. */
static void drive_outputs(void)
{
    NgsGpioSetPayload gpio;
    NgsPwmWritePayload pwm;
    NgsFrame resp;

    memset(&gpio, 0, sizeof(gpio));
    gpio.pin = 32;
    gpio.value = 1;
    gpio.mode = NGS_PIN_MODE_OUTPUT;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_GPIO, 4, &gpio, sizeof(gpio), &resp));

    memset(&pwm, 0, sizeof(pwm));
    pwm.pin = 33;
    pwm.duty = 3000;
    pwm.resolution = 12;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_WRITE_PWM, 5, &pwm, sizeof(pwm), &resp));

    TEST_ASSERT_EQUAL_UINT8(1, g_fake.pin_value[32]);
    TEST_ASSERT_EQUAL_UINT16(3000, g_fake.pwm_duty);
}

/* Returns the error code from a response, or 0xFF if it was not an error. */
static uint8_t response_error(const NgsFrame *resp)
{
    if (resp->type != NGS_MSG_ERROR || resp->len != sizeof(NgsErrorPayload)) {
        return 0xFFu;
    }
    NgsErrorPayload e;
    memcpy(&e, resp->payload, sizeof(e));
    return e.code;
}

static void test_estop_drives_every_registered_output_safe(void)
{
    NgsFrame resp;
    register_safe_state(0);
    drive_outputs();

    TEST_ASSERT_TRUE(send_estop(NGS_ESTOP_ACTION_ENGAGE));

    TEST_ASSERT_EQUAL_UINT8_MESSAGE(0, g_fake.pin_value[32], "valve pin not driven safe");
    TEST_ASSERT_EQUAL_UINT16_MESSAGE(0, g_fake.pwm_duty, "pump not driven to zero");
    (void)resp;
}

static void test_estop_is_reported_in_status(void)
{
    NgsFrame resp;
    NgsStatusPayload st;

    register_safe_state(0);
    TEST_ASSERT_TRUE(send_estop(NGS_ESTOP_ACTION_ENGAGE));
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_GET_STATUS, 6, NULL, 0, &resp));
    memcpy(&st, resp.payload, sizeof(st));

    TEST_ASSERT_EQUAL_UINT8(1, st.estop);
    TEST_ASSERT_EQUAL_UINT8(NGS_ESTOP_SRC_COMMAND, st.estop_source);
    TEST_ASSERT_EQUAL_UINT8(2, st.safe_entries);
}

static void test_outputs_are_refused_while_latched(void)
{
    NgsGpioSetPayload gpio;
    NgsPwmWritePayload pwm;
    NgsFrame resp;

    register_safe_state(0);
    TEST_ASSERT_TRUE(send_estop(NGS_ESTOP_ACTION_ENGAGE));

    memset(&gpio, 0, sizeof(gpio));
    gpio.pin = 32;
    gpio.value = 1;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_GPIO, 7, &gpio, sizeof(gpio), &resp));
    TEST_ASSERT_EQUAL_UINT8(NGS_ERR_ESTOP, response_error(&resp));
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(0, g_fake.pin_value[32], "pin moved despite the latch");

    memset(&pwm, 0, sizeof(pwm));
    pwm.pin = 33;
    pwm.duty = 2000;
    pwm.resolution = 12;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_WRITE_PWM, 8, &pwm, sizeof(pwm), &resp));
    TEST_ASSERT_EQUAL_UINT8(NGS_ERR_ESTOP, response_error(&resp));
    TEST_ASSERT_EQUAL_UINT16(0, g_fake.pwm_duty);
}

static void test_reads_still_work_while_latched(void)
{
    NgsFrame resp;
    register_safe_state(0);
    TEST_ASSERT_TRUE(send_estop(NGS_ESTOP_ACTION_ENGAGE));

    /* A latched bench is exactly when you want to see what it is doing. */
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_PING, 10, NULL, 0, &resp));
    TEST_ASSERT_EQUAL_UINT8(NGS_MSG_PING | NGS_MSG_RESP, resp.type);

    NgsAdcReadPayload adc;
    memset(&adc, 0, sizeof(adc));
    adc.channel = 3;
    adc.samples = 1;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_READ_ADC, 11, &adc, sizeof(adc), &resp));
    TEST_ASSERT_EQUAL_UINT8(NGS_MSG_READ_ADC | NGS_MSG_RESP, resp.type);
}

static void test_auto_is_refused_but_manual_is_not(void)
{
    NgsControlCfgPayload cfg;
    NgsFrame resp;

    register_safe_state(0);
    TEST_ASSERT_TRUE(send_estop(NGS_ESTOP_ACTION_ENGAGE));

    bench_cfg(&cfg);
    cfg.mode = NGS_PUMP_MODE_AUTO;
    cfg.setpoint = 200.0f;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_CONTROL, 12, &cfg, sizeof(cfg), &resp));
    TEST_ASSERT_EQUAL_UINT8(NGS_ERR_ESTOP, response_error(&resp));

    /* Manual is a way *out* of driving something, so it is never refused. */
    cfg.mode = NGS_PUMP_MODE_MANUAL;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_CONTROL, 13, &cfg, sizeof(cfg), &resp));
    TEST_ASSERT_EQUAL_UINT8(NGS_MSG_SET_CONTROL | NGS_MSG_RESP, resp.type);
}

static void test_a_running_loop_is_taken_back(void)
{
    NgsControlCfgPayload cfg;
    NgsFrame resp;

    register_safe_state(0);

    /* Hand the pump to the controller and let it settle somewhere non-zero. */
    NgsPwmWritePayload pwm;
    memset(&pwm, 0, sizeof(pwm));
    pwm.pin = 33;
    pwm.duty = 2000;
    pwm.resolution = 12;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_WRITE_PWM, 14, &pwm, sizeof(pwm), &resp));

    bench_cfg(&cfg);
    cfg.mode = NGS_PUMP_MODE_AUTO;
    cfg.setpoint = 300.0f;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_CONTROL, 15, &cfg, sizeof(cfg), &resp));
    TEST_ASSERT_EQUAL_UINT8(NGS_PUMP_MODE_AUTO, app.control.mode);

    TEST_ASSERT_TRUE(send_estop(NGS_ESTOP_ACTION_ENGAGE));

    /* Left in AUTO it would drive the pump back up on the very next tick. */
    TEST_ASSERT_EQUAL_UINT8(NGS_PUMP_MODE_MANUAL, app.control.mode);
    for (uint32_t i = 0; i < 100u; i++) {
        g_fake.micros += 20000u;
        ngs_app_poll(&app);
    }
    TEST_ASSERT_EQUAL_UINT16_MESSAGE(0, g_fake.pwm_duty, "the loop drove the pump after an estop");
}

static void test_clearing_releases_the_latch_but_moves_nothing(void)
{
    NgsGpioSetPayload gpio;
    NgsFrame resp;

    register_safe_state(0);
    drive_outputs();
    TEST_ASSERT_TRUE(send_estop(NGS_ESTOP_ACTION_ENGAGE));
    TEST_ASSERT_TRUE(send_estop(NGS_ESTOP_ACTION_CLEAR));

    /* Nothing came back on by itself. */
    TEST_ASSERT_EQUAL_UINT8(0, g_fake.pin_value[32]);
    TEST_ASSERT_EQUAL_UINT16(0, g_fake.pwm_duty);

    /* But commands work again. */
    memset(&gpio, 0, sizeof(gpio));
    gpio.pin = 32;
    gpio.value = 1;
    gpio.mode = NGS_PIN_MODE_OUTPUT;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_GPIO, 16, &gpio, sizeof(gpio), &resp));
    TEST_ASSERT_EQUAL_UINT8(1, g_fake.pin_value[32]);
}

static void test_the_watchdog_latches_when_the_host_goes_quiet(void)
{
    register_safe_state(500); /* half a second of silence is enough */
    drive_outputs();

    /* No frames at all from here on -- the host is gone. */
    g_fake.micros += 600000u;
    ngs_app_poll(&app);

    TEST_ASSERT_TRUE_MESSAGE(app.estop, "watchdog did not latch");
    TEST_ASSERT_EQUAL_UINT8(NGS_ESTOP_SRC_WATCHDOG, app.estop_source);
    TEST_ASSERT_EQUAL_UINT8_MESSAGE(0, g_fake.pin_value[32], "valve left driven");
    TEST_ASSERT_EQUAL_UINT16_MESSAGE(0, g_fake.pwm_duty, "pump left running");
}

static void test_the_watchdog_is_held_off_by_traffic(void)
{
    NgsFrame resp;
    register_safe_state(500);
    drive_outputs();

    /* A host that keeps talking keeps its outputs. */
    for (uint32_t i = 0; i < 10u; i++) {
        g_fake.micros += 300000u;
        TEST_ASSERT_TRUE(round_trip(NGS_MSG_PING, 17, NULL, 0, &resp));
    }

    TEST_ASSERT_FALSE(app.estop);
    TEST_ASSERT_EQUAL_UINT8(1, g_fake.pin_value[32]);
}

static void test_the_watchdog_is_off_by_default(void)
{
    register_safe_state(0);
    drive_outputs();

    g_fake.micros += 60000000u; /* a minute of silence */
    ngs_app_poll(&app);

    TEST_ASSERT_FALSE_MESSAGE(app.estop, "watchdog fired when it was disabled");
    TEST_ASSERT_EQUAL_UINT8(1, g_fake.pin_value[32]);
}

static void test_safe_entries_are_validated(void)
{
    NgsSafeEntryPayload e;
    NgsFrame resp;

    memset(&e, 0, sizeof(e));
    e.index = NGS_SAFE_MAX_ENTRIES; /* one past the end */
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_SAFE_ENTRY, 18, &e, sizeof(e), &resp));
    TEST_ASSERT_EQUAL_UINT8(NGS_ERR_BAD_ARGUMENT, response_error(&resp));

    memset(&e, 0, sizeof(e));
    e.index = 0;
    e.kind = NGS_SAFE_KIND_GPIO;
    e.pin = 200; /* no such pin */
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_SAFE_ENTRY, 19, &e, sizeof(e), &resp));
    TEST_ASSERT_EQUAL_UINT8(NGS_ERR_BAD_ARGUMENT, response_error(&resp));

    memset(&e, 0, sizeof(e));
    e.index = 0;
    e.kind = NGS_SAFE_KIND_PWM;
    e.pin = 33;
    e.resolution = 0; /* a duty means nothing without one */
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_SAFE_ENTRY, 20, &e, sizeof(e), &resp));
    TEST_ASSERT_EQUAL_UINT8(NGS_ERR_BAD_ARGUMENT, response_error(&resp));
}

static void test_clearing_the_table_forgets_every_entry(void)
{
    NgsSafeEntryPayload e;
    NgsFrame resp;
    NgsStatusPayload st;

    register_safe_state(0);

    memset(&e, 0, sizeof(e));
    e.index = NGS_SAFE_INDEX_CLEAR;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_SAFE_ENTRY, 21, &e, sizeof(e), &resp));

    TEST_ASSERT_TRUE(round_trip(NGS_MSG_GET_STATUS, 22, NULL, 0, &resp));
    memcpy(&st, resp.payload, sizeof(st));
    TEST_ASSERT_EQUAL_UINT8(0, st.safe_entries);
}

static void register_estop_tests(void)
{
    RUN_TEST(test_estop_drives_every_registered_output_safe);
    RUN_TEST(test_estop_is_reported_in_status);
    RUN_TEST(test_outputs_are_refused_while_latched);
    RUN_TEST(test_reads_still_work_while_latched);
    RUN_TEST(test_auto_is_refused_but_manual_is_not);
    RUN_TEST(test_a_running_loop_is_taken_back);
    RUN_TEST(test_clearing_releases_the_latch_but_moves_nothing);
    RUN_TEST(test_the_watchdog_latches_when_the_host_goes_quiet);
    RUN_TEST(test_the_watchdog_is_held_off_by_traffic);
    RUN_TEST(test_the_watchdog_is_off_by_default);
    RUN_TEST(test_safe_entries_are_validated);
    RUN_TEST(test_clearing_the_table_forgets_every_entry);
}

#endif /* TEST_ESTOP_H */
