/* tt_link.h 逻辑自测(PC 侧,纯协议逻辑,不含硬件)。
 * 编译: gcc -std=c99 -Wall -Wextra tt_link_test.c -o tt_link_test && ./tt_link_test */
#include <stdio.h>
#include <string.h>
#include <stdint.h>

#define TT_LINK_IMPL
#include "tt_link.h"

static char g_out[4096];
static int g_out_len = 0;
static uint32_t fake_ms = 0;

static void capture_send(const uint8_t *data, uint16_t len) {
    if (g_out_len + (int)len < (int)sizeof(g_out)) {
        memcpy(g_out + g_out_len, data, len);
        g_out_len += len;
    }
}

static volatile float g_kp = 1.0f, g_ki = 0.0f, g_kd = 0.1f;
static volatile int32_t g_mode = 0;

static const tt_param_t params[] = {
    TT_PARAM_FR("kp", g_kp, 0.0f, 50.0f),
    TT_PARAM_F ("ki", g_ki),
    TT_PARAM_F ("kd", g_kd),
    TT_PARAM_IR("mode", g_mode, 0, 2),
};

static void emit_ctl(tt_ctx *c, uint32_t t_ms) {
    tt_begin(c, "ctl", t_ms);
    tt_f(c, 1.5f);
    tt_f(c, -2.25f);
    tt_i(c, -7);
    tt_end(c);
}

static const tt_chan_t chans[] = {
    TT_CHAN("ctl", emit_ctl, 50.0f),
};

static tt_ctx g_tt;
static int failures = 0;

static int has(const char *sub) {
    return strstr(g_out, sub) != NULL;
}

static void check(const char *what, int cond) {
    if (!cond) {
        failures++;
        printf("FAIL: %s\n  out=[%s]\n", what, g_out);
    } else {
        printf("ok:   %s\n", what);
    }
}

static void feed_bytes(const char *s) {
    tt_feed(&g_tt, (const uint8_t *)s, (uint16_t)strlen(s));
    tt_tick(&g_tt, fake_ms);
}

static void feed_line(const char *s) {
    feed_bytes(s);
    feed_bytes("\n");
}

static void reset_out(void) { g_out_len = 0; g_out[0] = '\0'; }

int main(void) {
    tt_init(&g_tt, capture_send, NULL, params, 4, chans, 1);

    reset_out(); feed_line("SET kp 2.0 !7");
    check("SET valid -> OK,7 + g_kp=2", has("OK,7") && g_kp == 2.0f);

    reset_out(); feed_line("SET kp 99 !8");
    check("SET out of range -> ERR,8,RANGE", has("ERR,8,RANGE") && g_kp == 2.0f);

    reset_out(); feed_line("SET nope 1 !9");
    check("SET unknown key -> ERR,9,UNKNOWN_KEY", has("ERR,9,UNKNOWN_KEY"));

    reset_out(); feed_line("SET mode 1.7 !10");
    check("SET int rounds 1.7->2 -> OK,10", has("OK,10") && g_mode == 2);

    reset_out(); feed_line("SET kp xyz !11");
    check("SET nan -> ERR,11,NAN", has("ERR,11,NAN"));

    reset_out(); feed_line("RATE ctl 100 !12");
    check("RATE valid -> OK,12", has("OK,12"));

    reset_out(); feed_line("RATE nope 10 !13");
    check("RATE unknown chan -> ERR,13,UNKNOWN_CHAN", has("ERR,13,UNKNOWN_CHAN"));

    reset_out(); feed_line("RATE ctl -1 !14");
    check("RATE negative -> ERR,14,RANGE", has("ERR,14,RANGE"));

    reset_out(); feed_line("GET kp");
    check("GET -> P,kp,2", has("P,kp,2"));

    reset_out(); feed_line("GET kp !15");
    check("GET with seq -> P,kp,2 + OK,15", has("P,kp,2") && has("OK,15"));

    reset_out(); feed_line("PING !20");
    check("PING -> OK,20", has("OK,20"));

    reset_out(); feed_line("BLAH !21");
    check("unknown cmd -> ERR,21,UNKNOWN_COMMAND", has("ERR,21,UNKNOWN_COMMAND"));

    reset_out(); feed_line("PING !30\r\n");
    check("CRLF tolerated -> OK,30", has("OK,30"));

    reset_out();
    feed_bytes("SE"); feed_bytes("T kp 3 !31"); feed_bytes("\n");
    check("fragmented feed -> OK,31 + g_kp=3", has("OK,31") && g_kp == 3.0f);

    reset_out(); tt_tick(&g_tt, 1000);
    check("channel emit -> D,ctl,1000,1.5,-2.25,-7", has("D,ctl,1000,1.5,-2.25,-7"));

    reset_out(); feed_line("RATE ctl 0 !50");
    reset_out(); tt_tick(&g_tt, 3000);
    check("RATE 0 silences channel", !has("D,ctl"));

    {
        char big[512];
        memset(big, 'A', sizeof(big) - 1);
        big[sizeof(big) - 1] = '\0';
        reset_out(); feed_line(big);
        reset_out(); feed_line("PING !40");
        check("overlong line discarded, buffer recovers -> OK,40", has("OK,40"));
    }

    {
        char b[32];
        tt_ftoa(b, 0.0f);        check("ftoa(0)=\"0\"", strcmp(b, "0") == 0);
        tt_ftoa(b, -0.5f);       check("ftoa(-0.5)=\"-0.5\"", strcmp(b, "-0.5") == 0);
        tt_ftoa(b, 3.14f);       check("ftoa(3.14)=\"3.14\"", strcmp(b, "3.14") == 0);
        tt_ftoa(b, 123.456f);    check("ftoa(123.456)=\"123.456\"", strcmp(b, "123.456") == 0);
        tt_ftoa(b, 0.0001f);     check("ftoa(0.0001)=\"0.0001\"", strcmp(b, "0.0001") == 0);
        tt_ftoa(b, 0.00001f);    check("ftoa(0.00001)=\"1e-5\"", strcmp(b, "1e-5") == 0);
        tt_ftoa(b, 1e-8f);       check("ftoa(1e-8)=\"1e-8\"", strcmp(b, "1e-8") == 0);
        tt_ftoa(b, -9999999.5f); check("ftoa(-9999999.5)=\"-1e7\"", strcmp(b, "-1e7") == 0);
        tt_ftoa(b, 1000000.0f);  check("ftoa(1000000)=\"1e6\"", strcmp(b, "1e6") == 0);
        tt_ftoa(b, 999999.0f);   check("ftoa(999999)=\"999999\"", strcmp(b, "999999") == 0);
    }

    if (failures == 0) printf("\nALL PASS\n");
    else printf("\n%d FAILURES\n", failures);
    return failures ? 1 : 0;
}
