/* ============================================================================
 * tt_link.h — tcp_tool 接入契约 v1 的固件侧单文件参考实现
 * (docs/protocol_contract_v1.md)
 *
 * 设计边界(本文件负责 / 不负责):
 *   [负责]   行收包/发帧、D 帧列序、SET/RATE/GET/PING 解析、!<seq> 回执、
 *            通道调度、数值格式化
 *   [不负责] 传输载体 —— 你给一个 tt_send_fn("把字节发出去");
 *            参数本体 —— 你把"参数名→变量指针"注册进 params[] 表;
 *            采样数据 —— 每个通道一个 emit 回调,自己往里填数
 *
 * 使用三步:
 *   1. #include "tt_link.h"(工程里放这一个文件即可)
 *   2. 注册参数表 + 通道表(改参数 = 改表里的名字和指针)
 *   3. 收字节喂 tt_feed();周期调 tt_tick()
 *
 * 线程/上下文约定:
 *   tt_feed 只往静态缓冲追加字节,可进 UART ISR;
 *   tt_tick 处理行解析/发帧/回执(可能调 send 回调),必须放任务/主循环。
 *
 * 纯 C99;无 malloc;无 libc printf(自带 %g 简化版);所有缓冲静态可配置。
 * ==========================================================================*/
#ifndef TT_LINK_H
#define TT_LINK_H

#include <stdint.h>
#include <string.h>

/* ---- 用户可配置(在包含本文件前用 -D 覆盖,或直接改这里) ---- */
#ifndef TT_RX_BUF
#define TT_RX_BUF     256   /* 接收行缓冲(最长一行的字节数) */
#endif
#ifndef TT_LINE_BUF
#define TT_LINE_BUF   256   /* 发送行缓冲(D 帧/回执最长字节数) */
#endif
#ifndef TT_MAX_PARAMS
#define TT_MAX_PARAMS 32    /* params[] 表最大条目(由调用方数组长度决定) */
#endif
#ifndef TT_MAX_CHANS
#define TT_MAX_CHANS  8     /* chans[] 表最大条目 */
#endif

/* ---- 参数类型 ---- */
#define TT_F32  0           /* float*   */
#define TT_I32  1           /* int32_t* */

/* ---- 参数表条目 ---- */
typedef struct {
    const char *name;       /* 命令里的参数名,如 "kp"           */
    void       *value;      /* 变量指针,如 &g_pid.kp            */
    uint8_t     type;       /* TT_F32 / TT_I32                  */
    uint8_t     has_min;    /* 1 = 检查 min/max,越界回 ERR RANGE */
    uint8_t     has_max;
    float       min, max;   /* 范围(原始值口径)                  */
    float       scale, off; /* 协议值 = raw*scale + off;写入时 raw=(v-off)/scale */
} tt_param_t;

/* 纯直通(无范围、无标定)*/
#define TT_PARAM_F(_name, _var) \
    { _name, (void *)&(_var), TT_F32, 0, 0, 0, 0, 1.0f, 0 }
#define TT_PARAM_I(_name, _var) \
    { _name, (void *)&(_var), TT_I32, 0, 0, 0, 0, 1.0f, 0 }
/* 带范围检查 */
#define TT_PARAM_FR(_name, _var, _mn, _mx) \
    { _name, (void *)&(_var), TT_F32, 1, 1, _mn, _mx, 1.0f, 0 }
#define TT_PARAM_IR(_name, _var, _mn, _mx) \
    { _name, (void *)&(_var), TT_I32, 1, 1, _mn, _mx, 1.0f, 0 }

/* ---- 通道表条目 ---- */
struct tt_ctx;
typedef struct {
    const char *name;                       /* 帧里的通道名,如 "ctl"   */
    void (*emit)(struct tt_ctx *c, uint32_t t_ms);  /* 发帧回调        */
    float hz;                               /* 初始速率,0 = 关(RATE 可改) */
} tt_chan_t;

#define TT_CHAN(_name, _emit_fn, _hz) { _name, _emit_fn, _hz }

/* ---- 用户钩子 ---- */
typedef void    (*tt_send_fn)(const uint8_t *data, uint16_t len);
typedef uint32_t(*tt_ms_fn)(void);          /* 单调毫秒时钟 */

/* ---- 上下文 ---- */
typedef struct tt_ctx {
    tt_send_fn  send;
    tt_ms_fn    now_ms;
    const tt_param_t *params;  uint8_t nparams;
    const tt_chan_t  *chans;   uint8_t nchans;

    uint8_t  rx[TT_RX_BUF];
    uint16_t rx_len;
    uint8_t  rx_discard;        /* 行超长时置 1,丢弃到下一个 \n */
    uint8_t  line[TT_LINE_BUF];
    uint16_t line_len;
    float    hz[TT_MAX_CHANS];   /* 通道当前速率(RATE 可改;表本身保持 const) */
    uint32_t next_due[TT_MAX_CHANS];
} tt_ctx;

void tt_init(tt_ctx *c, tt_send_fn send, tt_ms_fn ms,
             const tt_param_t *p, uint8_t np,
             const tt_chan_t *ch, uint8_t nc);

/* 收字节(ISR 安全,只追加缓冲) */
void tt_feed(tt_ctx *c, const uint8_t *data, uint16_t len);

/* 周期调用(任务/主循环,1~10ms 一次):行解析 + 通道调度 */
void tt_tick(tt_ctx *c, uint32_t t_ms);

/* ---- emit 回调里用的发帧助手(在通道 emit 内调用) ---- */
void tt_begin(tt_ctx *c, const char *chan, uint32_t t_ms);  /* D,<chan>,<t_ms> */
void tt_f(tt_ctx *c, float v);      /* ,<v> %g 格式 */
void tt_i(tt_ctx *c, int32_t v);    /* ,<v> 整数   */
void tt_str(tt_ctx *c, const char *s); /* ,<s> 原始串 */
void tt_end(tt_ctx *c);             /* 补 \n 并发送 */

/* ============================================================================
 * 实现(单头文件,函数全 static inline;不再需要别的文件)
 * ==========================================================================*/
#ifdef TT_LINK_IMPL
#ifndef TT_LINK_IMPL_GUARD
#define TT_LINK_IMPL_GUARD

/* ---------------- 数值文本 ----------------
 * tt_ftoa: 简化 %g,最多 6 位有效数字、去尾零;
 * 按 %e 指数口径分界:e<-4(|v|<1e-4)或 e>=6(|v|>=1e6)用科学计数。 */
static uint16_t tt_utoa(char *out, uint32_t v) {
    char tmp[11]; uint16_t n = 0;
    if (v == 0) { out[0] = '0'; out[1] = '\0'; return 1; }
    while (v) { tmp[n++] = (char)('0' + (v % 10)); v /= 10; }
    for (uint16_t i = 0; i < n; i++) out[i] = tmp[n - 1 - i];
    out[n] = '\0';
    return n;
}

static uint16_t tt_ftoa(char *out, float v) {
    if (v != v) { memcpy(out, "nan\0", 4); return 3; }
    if (v > 3.4e38f) { memcpy(out, "inf\0", 4); return 3; }
    if (v < -3.4e38f) { memcpy(out, "-inf\0", 5); return 4; }
    if (v == 0.0f) { out[0] = '0'; out[1] = '\0'; return 1; }

    uint16_t n = 0;
    int neg = (v < 0.0f);
    float av = neg ? -v : v;
    int e = 0;
    while (av >= 10.0f) { av *= 0.1f; e++; }
    while (av < 1.0f)   { av *= 10.0f; e--; }

    uint32_t d = (uint32_t)(av * 100000.0f + 0.5f);  /* 6 位有效数字 */
    if (d >= 1000000u) { d = 100000u; e++; }
    char digits[7];
    uint32_t t = d;
    for (int i = 5; i >= 0; i--) { digits[i] = (char)('0' + (t % 10)); t /= 10; }
    int trim = 5;
    while (trim > 0 && digits[trim] == '0') trim--;

    if (neg) out[n++] = '-';
    if (e >= -4 && e < 6) {
        /* 定点 */
        if (e >= 0) {
            for (int i = 0; i <= e; i++) out[n++] = digits[i];
            if (trim >= e + 1) {
                out[n++] = '.';
                for (int i = e + 1; i <= trim; i++) out[n++] = digits[i];
            }
        } else {
            out[n++] = '0';
            out[n++] = '.';
            for (int i = 0; i < -e - 1; i++) out[n++] = '0';
            for (int i = 0; i <= trim; i++) out[n++] = digits[i];
        }
    } else {
        /* 科学计数 */
        out[n++] = digits[0];
        if (trim > 0) {
            out[n++] = '.';
            for (int i = 1; i <= trim; i++) out[n++] = digits[i];
        }
        out[n++] = 'e';
        if (e < 0) { out[n++] = '-'; e = -e; }
        n += tt_utoa(out + n, (uint32_t)e);
    }
    out[n] = '\0';
    return n;
}

static float tt_atof(const char *s, uint16_t len, int *ok) {
    float sign = 1.0f, val = 0.0f, frac = 0.1f;
    int in_frac = 0, exp_sign = 1, exp_val = 0, in_exp = 0, any = 0;
    *ok = 0;
    uint16_t i = 0;
    while (i < len && (s[i] == ' ' || s[i] == '\t')) i++;
    if (i < len && (s[i] == '-' || s[i] == '+')) { if (s[i] == '-') sign = -1.0f; i++; }
    for (; i < len; i++) {
        char ch = s[i];
        if (ch >= '0' && ch <= '9') {
            any = 1;
            if (in_exp) { exp_val = exp_val * 10 + (ch - '0'); }
            else if (in_frac) { val += frac * (ch - '0'); frac *= 0.1f; }
            else { val = val * 10.0f + (ch - '0'); }
        } else if (ch == '.' && !in_frac && !in_exp) {
            in_frac = 1;
        } else if ((ch == 'e' || ch == 'E') && !in_exp && any) {
            in_exp = 1;
            if (i + 1 < len && (s[i + 1] == '-' || s[i + 1] == '+')) {
                if (s[i + 1] == '-') exp_sign = -1;
                i++;
            }
        } else {
            break;
        }
    }
    if (!any) return 0.0f;
    *ok = 1;
    while (exp_val-- > 0) {
        if (exp_sign > 0) val *= 10.0f; else val *= 0.1f;
    }
    return sign * val;
}

/* ---------------- 行缓冲(发送侧) ---------------- */
static void tt_putc(tt_ctx *c, char ch) {
    if (c->line_len < TT_LINE_BUF - 1) c->line[c->line_len++] = (uint8_t)ch;
}

static void tt_puts(tt_ctx *c, const char *s) {
    while (*s) tt_putc(c, *s++);
}

static void tt_emit_line(tt_ctx *c) {
    tt_putc(c, '\n');
    if (c->send) c->send(c->line, c->line_len);
    c->line_len = 0;
}

void tt_begin(tt_ctx *c, const char *chan, uint32_t t_ms) {
    c->line_len = 0;
    tt_puts(c, "D,");
    tt_puts(c, chan);
    tt_putc(c, ',');
    c->line_len += tt_utoa((char *)c->line + c->line_len, t_ms);
}

void tt_f(tt_ctx *c, float v) {
    tt_putc(c, ',');
    c->line_len += tt_ftoa((char *)c->line + c->line_len, v);
}

void tt_i(tt_ctx *c, int32_t v) {
    tt_putc(c, ',');
    if (v < 0) {
        tt_putc(c, '-');
        c->line_len += tt_utoa((char *)c->line + c->line_len, (uint32_t)(-(int64_t)v));
    } else {
        c->line_len += tt_utoa((char *)c->line + c->line_len, (uint32_t)v);
    }
}

void tt_str(tt_ctx *c, const char *s) {
    tt_putc(c, ',');
    tt_puts(c, s);
}

void tt_end(tt_ctx *c) {
    tt_emit_line(c);
}

/* ---------------- 回执 ---------------- */
static void tt_reply(tt_ctx *c, int32_t seq, const char *kind, const char *reason) {
    c->line_len = 0;
    tt_puts(c, kind);
    if (seq >= 0) {
        tt_putc(c, ',');
        c->line_len += tt_utoa((char *)c->line + c->line_len, (uint32_t)seq);
    }
    if (reason) { tt_putc(c, ','); tt_puts(c, reason); }
    tt_emit_line(c);
}

/* ---------------- 命令解析 ---------------- */
static const tt_param_t *tt_find_param(tt_ctx *c, const char *name, uint16_t len) {
    for (uint8_t i = 0; i < c->nparams; i++)
        if (strlen(c->params[i].name) == len &&
            memcmp(c->params[i].name, name, len) == 0)
            return &c->params[i];
    return NULL;
}

static const tt_chan_t *tt_find_chan(tt_ctx *c, const char *name, uint16_t len) {
    for (uint8_t i = 0; i < c->nchans; i++)
        if (strlen(c->chans[i].name) == len &&
            memcmp(c->chans[i].name, name, len) == 0)
            return &c->chans[i];
    return NULL;
}

static void tt_handle_line(tt_ctx *c, char *line, uint16_t len) {
    /* 去 CR、去尾空白 */
    while (len && (line[len - 1] == '\r' || line[len - 1] == ' ' ||
                   line[len - 1] == '\t'))
        line[--len] = '\0';

    /* 可选 !<seq> 回执请求 */
    int32_t seq = -1;
    char *bang = NULL;
    for (uint16_t i = 0; i < len; i++) {
        if (line[i] == '!' && (i == 0 || line[i - 1] == ' ')) { bang = &line[i]; break; }
    }
    if (bang) {
        *bang = '\0';
        len = (uint16_t)(bang - line);
        int ok = 0;
        float s = tt_atof(bang + 1, (uint16_t)strlen(bang + 1), &ok);
        if (ok) seq = (int32_t)s;
    }

    /* 切 verb */
    char *p = line;
    while (*p == ' ' || *p == '\t') p++;
    char *verb = p;
    while (*p && *p != ' ' && *p != '\t') p++;
    uint16_t verb_len = (uint16_t)(p - verb);

    if (verb_len == 3 && memcmp(verb, "SET", 3) == 0) {
        while (*p == ' ' || *p == '\t') p++;
        char *key = p;
        while (*p && *p != ' ' && *p != '\t') p++;
        uint16_t key_len = (uint16_t)(p - key);
        while (*p == ' ' || *p == '\t') p++;
        const tt_param_t *prm = tt_find_param(c, key, key_len);
        if (!prm) { tt_reply(c, seq, "ERR", "UNKNOWN_KEY"); return; }
        int ok = 0;
        float v = tt_atof(p, (uint16_t)strlen(p), &ok);
        if (!ok) { tt_reply(c, seq, "ERR", "NAN"); return; }
        if ((prm->has_min && v < prm->min) || (prm->has_max && v > prm->max)) {
            tt_reply(c, seq, "ERR", "RANGE"); return;
        }
        float raw = (v - prm->off) / prm->scale;
        if (prm->type == TT_F32) *(float *)prm->value = raw;
        else *(int32_t *)prm->value = (int32_t)(raw + (raw >= 0 ? 0.5f : -0.5f));
        tt_reply(c, seq, "OK", NULL);
    } else if (verb_len == 4 && memcmp(verb, "RATE", 4) == 0) {
        while (*p == ' ' || *p == '\t') p++;
        char *key = p;
        while (*p && *p != ' ' && *p != '\t') p++;
        uint16_t key_len = (uint16_t)(p - key);
        while (*p == ' ' || *p == '\t') p++;
        const tt_chan_t *ch = tt_find_chan(c, key, key_len);
        if (!ch) { tt_reply(c, seq, "ERR", "UNKNOWN_CHAN"); return; }
        int ok = 0;
        float hz = tt_atof(p, (uint16_t)strlen(p), &ok);
        if (!ok || hz < 0 || hz > 1000.0f) { tt_reply(c, seq, "ERR", "RANGE"); return; }
        c->hz[ch - c->chans] = hz;
        tt_reply(c, seq, "OK", NULL);
    } else if (verb_len == 3 && memcmp(verb, "GET", 3) == 0) {
        while (*p == ' ' || *p == '\t') p++;
        char *key = p;
        while (*p && *p != ' ' && *p != '\t') p++;
        uint16_t key_len = (uint16_t)(p - key);
        const tt_param_t *prm = tt_find_param(c, key, key_len);
        if (!prm) { tt_reply(c, seq, "ERR", "UNKNOWN_KEY"); return; }
        c->line_len = 0;
        tt_puts(c, "P,");
        tt_puts(c, prm->name);
        tt_putc(c, ',');
        float raw = (prm->type == TT_F32) ? *(float *)prm->value
                                          : (float)*(int32_t *)prm->value;
        c->line_len += tt_ftoa((char *)c->line + c->line_len, raw * prm->scale + prm->off);
        tt_emit_line(c);
        if (seq >= 0) tt_reply(c, seq, "OK", NULL);
    } else if (verb_len == 4 && memcmp(verb, "PING", 4) == 0) {
        tt_reply(c, seq, "OK", NULL);
    } else {
        tt_reply(c, seq, "ERR", "UNKNOWN_COMMAND");
    }
}

/* ---------------- 对外 API ---------------- */
void tt_init(tt_ctx *c, tt_send_fn send, tt_ms_fn ms,
             const tt_param_t *p, uint8_t np,
             const tt_chan_t *ch, uint8_t nc) {
    memset(c, 0, sizeof(*c));
    c->send = send; c->now_ms = ms;
    c->params = p; c->nparams = np;
    c->chans = ch; c->nchans = nc;
    for (uint8_t i = 0; i < nc && i < TT_MAX_CHANS; i++)
        c->hz[i] = ch[i].hz;
}

void tt_feed(tt_ctx *c, const uint8_t *data, uint16_t len) {
    for (uint16_t i = 0; i < len; i++) {
        if (c->rx_len >= TT_RX_BUF) {
            c->rx_discard = 1;
            c->rx_len = 0;
        }
        if (c->rx_discard) {
            if (data[i] == '\n') c->rx_discard = 0;
            continue;
        }
        c->rx[c->rx_len++] = data[i];
    }
}

void tt_tick(tt_ctx *c, uint32_t t_ms) {
    /* 1. 行解析(只在任务上下文,回执从这里发) */
    while (c->rx_len) {
        uint16_t nl = 0;
        while (nl < c->rx_len && c->rx[nl] != '\n') nl++;
        if (nl >= c->rx_len) break;             /* 还没有完整行 */
        c->rx[nl] = '\0';
        tt_handle_line(c, (char *)c->rx, nl);
        uint16_t rest = c->rx_len - nl - 1;
        if (rest) memmove(c->rx, c->rx + nl + 1, rest);
        c->rx_len = rest;
    }
    /* 2. 通道调度 */
    for (uint8_t i = 0; i < c->nchans; i++) {
        const tt_chan_t *ch = &c->chans[i];
        float hz = c->hz[i];
        if (hz <= 0.0f || !ch->emit) continue;
        if (t_ms >= c->next_due[i]) {
            uint32_t period = (uint32_t)(1000.0f / hz);
            if (period == 0) period = 1;
            c->next_due[i] = t_ms + period;
            ch->emit(c, t_ms);
        }
    }
}

#endif /* TT_LINK_IMPL_GUARD */
#endif /* TT_LINK_IMPL */

#endif /* TT_LINK_H */
