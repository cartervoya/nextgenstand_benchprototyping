/*
 * test_store.h - The control configuration in NVM, tested on the board.
 *
 * The interesting cases are all failure cases. A stored configuration that
 * loads when it should not is worse than no stored configuration at all: the
 * loop comes up with gains that look deliberate and are not.
 *
 * Included by test_main.cpp, which supplies the helpers and g_fake.
 */

#ifndef TEST_STORE_H
#define TEST_STORE_H

#include <unity.h>

extern "C" {
#include "ngs_store.h"
}

static void store_test_cfg(NgsControlCfgPayload *cfg)
{
    bench_cfg(cfg);
    cfg->mode = NGS_PUMP_MODE_MANUAL;
    cfg->kp = 0.1625f;
    cfg->ki = 0.0192f;
    cfg->out_deadzone = 18.5f;
}

static void test_store_round_trips(void)
{
    NgsControlCfgPayload written, read;
    store_test_cfg(&written);

    TEST_ASSERT_TRUE(ngs_store_save(&written));
    TEST_ASSERT_TRUE(ngs_store_load(&read));

    TEST_ASSERT_EQUAL_FLOAT(0.1625f, read.kp);
    TEST_ASSERT_EQUAL_FLOAT(0.0192f, read.ki);
    TEST_ASSERT_EQUAL_FLOAT(18.5f, read.out_deadzone);
    TEST_ASSERT_EQUAL_FLOAT(written.cal_scale, read.cal_scale);
    TEST_ASSERT_EQUAL_UINT32(written.period_us, read.period_us);
}

static void test_store_never_stores_auto(void)
{
    /* A board that powers up already driving a pump, because that is what it
     * was doing when it was saved, is not a thing this bench should do. */
    NgsControlCfgPayload written, read;
    store_test_cfg(&written);
    written.mode = NGS_PUMP_MODE_AUTO;
    written.setpoint = 350.0f;

    TEST_ASSERT_TRUE(ngs_store_save(&written));
    TEST_ASSERT_TRUE(ngs_store_load(&read));

    TEST_ASSERT_EQUAL_UINT8(NGS_PUMP_MODE_MANUAL, read.mode);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, read.setpoint);
}

static void test_store_load_fails_on_blank_nvm(void)
{
    NgsControlCfgPayload read;
    memset(g_fake.nvm, 0, sizeof(g_fake.nvm));
    TEST_ASSERT_FALSE(ngs_store_load(&read));

    /* And on erased flash, which reads as 0xFF rather than 0x00. */
    memset(g_fake.nvm, 0xFF, sizeof(g_fake.nvm));
    TEST_ASSERT_FALSE(ngs_store_load(&read));
}

static void test_store_load_fails_on_a_corrupt_block(void)
{
    /* What a brownout part way through a write leaves behind. Flash writes are
     * not atomic, and a configuration made of two different ones would look
     * perfectly plausible without the CRC. */
    NgsControlCfgPayload written, read;
    store_test_cfg(&written);
    TEST_ASSERT_TRUE(ngs_store_save(&written));

    g_fake.nvm[NGS_STORE_HEADER_SIZE + 5] ^= 0xFFu;
    TEST_ASSERT_FALSE(ngs_store_load(&read));
}

static void test_store_load_fails_on_a_different_version(void)
{
    NgsControlCfgPayload written, read;
    store_test_cfg(&written);
    TEST_ASSERT_TRUE(ngs_store_save(&written));

    g_fake.nvm[4] = (uint8_t)(NGS_STORE_VERSION + 1u);
    TEST_ASSERT_FALSE_MESSAGE(ngs_store_load(&read),
                              "a config from another layout must not be reinterpreted");
}

static void test_store_erase_invalidates(void)
{
    NgsControlCfgPayload written, read;
    store_test_cfg(&written);
    TEST_ASSERT_TRUE(ngs_store_save(&written));
    TEST_ASSERT_TRUE(ngs_store_load(&read));

    TEST_ASSERT_TRUE(ngs_store_erase());
    TEST_ASSERT_FALSE(ngs_store_load(&read));
}

static void test_store_reports_a_refused_write(void)
{
    NgsControlCfgPayload written;
    store_test_cfg(&written);
    g_fake.nvm_fail = true;

    TEST_ASSERT_FALSE_MESSAGE(ngs_store_save(&written),
                              "a refused write must not be reported as success");
    g_fake.nvm_fail = false;
}

static void test_store_leaves_the_load_target_alone_when_it_fails(void)
{
    /* The caller keeps its defaults, so a failed load must not half-fill the
     * struct on the way out. */
    NgsControlCfgPayload read;
    memset(g_fake.nvm, 0, sizeof(g_fake.nvm));
    ngs_control_defaults(&read);
    read.kp = 1.25f;

    TEST_ASSERT_FALSE(ngs_store_load(&read));
    TEST_ASSERT_EQUAL_FLOAT(1.25f, read.kp);
}

/* -- through the protocol ------------------------------------------------- */

static bool send_store(uint8_t action, NgsFrame *resp)
{
    NgsStoreCmdPayload cmd;
    memset(&cmd, 0, sizeof(cmd));
    cmd.action = action;
    return round_trip(NGS_MSG_STORE_CONTROL, 40, &cmd, sizeof(cmd), resp);
}

static void test_get_control_cfg_returns_what_the_board_holds(void)
{
    NgsControlCfgPayload cfg, got;
    NgsFrame resp;

    bench_cfg(&cfg);
    cfg.mode = NGS_PUMP_MODE_MANUAL;
    cfg.kp = 0.321f;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_CONTROL, 41, &cfg, sizeof(cfg), &resp));

    TEST_ASSERT_TRUE(round_trip(NGS_MSG_GET_CONTROL_CFG, 42, NULL, 0, &resp));
    TEST_ASSERT_EQUAL_UINT16(sizeof(NgsControlCfgPayload), resp.len);
    memcpy(&got, resp.payload, sizeof(got));
    TEST_ASSERT_EQUAL_FLOAT(0.321f, got.kp);
}

static void test_saving_over_the_wire_persists_across_a_reboot(void)
{
    NgsControlCfgPayload cfg;
    NgsFrame resp;

    bench_cfg(&cfg);
    cfg.mode = NGS_PUMP_MODE_MANUAL;
    cfg.kp = 0.777f;
    cfg.out_deadzone = 22.0f;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_CONTROL, 43, &cfg, sizeof(cfg), &resp));
    TEST_ASSERT_TRUE(send_store(NGS_STORE_ACTION_SAVE, &resp));
    TEST_ASSERT_EQUAL_UINT8(NGS_MSG_STORE_CONTROL | NGS_MSG_RESP, resp.type);

    /* Reboot: everything in RAM goes, the NVM does not. */
    ngs_app_init(&app);

    TEST_ASSERT_EQUAL_FLOAT(0.777f, app.control.cfg.kp);
    TEST_ASSERT_EQUAL_FLOAT(22.0f, app.control.cfg.out_deadzone);
    TEST_ASSERT_EQUAL_UINT8(NGS_PUMP_MODE_MANUAL, app.control.mode);
    TEST_ASSERT_TRUE(app.control_stored);
}

static void test_the_state_reports_whether_a_config_is_stored(void)
{
    NgsControlStatePayload st;
    NgsFrame resp;

    TEST_ASSERT_TRUE(round_trip(NGS_MSG_GET_CONTROL, 44, NULL, 0, &resp));
    memcpy(&st, resp.payload, sizeof(st));
    TEST_ASSERT_EQUAL_UINT8(0, st.stored);

    TEST_ASSERT_TRUE(send_store(NGS_STORE_ACTION_SAVE, &resp));
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_GET_CONTROL, 45, NULL, 0, &resp));
    memcpy(&st, resp.payload, sizeof(st));
    TEST_ASSERT_EQUAL_UINT8(1, st.stored);
}

static void test_erasing_over_the_wire_returns_to_defaults_after_a_reboot(void)
{
    NgsControlCfgPayload cfg;
    NgsFrame resp;

    bench_cfg(&cfg);
    cfg.mode = NGS_PUMP_MODE_MANUAL;
    cfg.kp = 0.9f;
    TEST_ASSERT_TRUE(round_trip(NGS_MSG_SET_CONTROL, 46, &cfg, sizeof(cfg), &resp));
    TEST_ASSERT_TRUE(send_store(NGS_STORE_ACTION_SAVE, &resp));
    TEST_ASSERT_TRUE(send_store(NGS_STORE_ACTION_ERASE, &resp));

    ngs_app_init(&app);

    NgsControlCfgPayload defaults;
    ngs_control_defaults(&defaults);
    TEST_ASSERT_EQUAL_FLOAT(defaults.kp, app.control.cfg.kp);
    TEST_ASSERT_FALSE(app.control_stored);
}

static void test_a_refused_save_is_reported_as_an_error(void)
{
    NgsFrame resp;
    g_fake.nvm_fail = true;
    TEST_ASSERT_TRUE(send_store(NGS_STORE_ACTION_SAVE, &resp));
    TEST_ASSERT_EQUAL_UINT8(NGS_ERR_NOT_SUPPORTED, response_error(&resp));
    g_fake.nvm_fail = false;
}

static void register_store_tests(void)
{
    RUN_TEST(test_store_round_trips);
    RUN_TEST(test_store_never_stores_auto);
    RUN_TEST(test_store_load_fails_on_blank_nvm);
    RUN_TEST(test_store_load_fails_on_a_corrupt_block);
    RUN_TEST(test_store_load_fails_on_a_different_version);
    RUN_TEST(test_store_erase_invalidates);
    RUN_TEST(test_store_reports_a_refused_write);
    RUN_TEST(test_store_leaves_the_load_target_alone_when_it_fails);
    RUN_TEST(test_get_control_cfg_returns_what_the_board_holds);
    RUN_TEST(test_saving_over_the_wire_persists_across_a_reboot);
    RUN_TEST(test_the_state_reports_whether_a_config_is_stored);
    RUN_TEST(test_erasing_over_the_wire_returns_to_defaults_after_a_reboot);
    RUN_TEST(test_a_refused_save_is_reported_as_an_error);
}

#endif /* TEST_STORE_H */
