# firmware_kit — 固件侧接入包

`tt_link.h` 一个文件 = 接入契约 v1(`docs/protocol_contract_v1.md`)的固件侧参考实现。
放进工程、改两张表(参数表 + 通道表)、接两个钩子(发字节 + 时钟),完事。

纯 C99 单头文件:无 malloc、不依赖 libc printf(自带 %g 简化版)、静态缓冲可配置。
多核/RTOS/裸机通吃——只有一个上下文约定:**tt_feed 可进 ISR,tt_tick 放任务/主循环**。

---

## 1. 三步接入

```c
#define TT_LINK_IMPL
#include "tt_link.h"      /* 全工程只在一个 .c 里定义 TT_LINK_IMPL */

/* ① 你的参数(标量、可写;见下方检查清单) */
volatile float g_kp = 2.0f, g_ki = 0.0f, g_kd = 0.1f;

/* ② 参数表:改参数 = 改这张表(名字 + 变量指针) */
static const tt_param_t tt_params[] = {
    TT_PARAM_FR("kp", g_kp, 0.0f, 50.0f),
    TT_PARAM_F ("ki", g_ki),
    TT_PARAM_F ("kd", g_kd),
};

/* ③ 通道表:每个通道一个 emit 回调,列序与 profiles/<name>/config.json
 *   的 protocol.channels 逐列一致 */
static void emit_ctl(tt_ctx *c, uint32_t t_ms) {
    tt_begin(c, "ctl", t_ms);
    tt_f(c, g_setpoint); tt_f(c, g_pos); tt_f(c, g_err); tt_f(c, g_duty);
    tt_end(c);
}
static void emit_params(tt_ctx *c, uint32_t t_ms) {
    tt_begin(c, "params", t_ms);
    tt_f(c, g_kp); tt_f(c, g_ki); tt_f(c, g_kd);
    tt_end(c);
}
static const tt_chan_t tt_chans[] = {
    TT_CHAN("ctl",    emit_ctl,    50),
    TT_CHAN("params", emit_params, 5),
};

/* ④ 钩子:把字节发出去(接 UART TX / socket / wifi 帧) + 单调毫秒 */
static void hw_send(const uint8_t *data, uint16_t len) { /* 你的发送 */ }
static uint32_t hw_ms(void) { return hal_get_ms(); }

/* ⑤ 初始化 + 周期驱动 */
static tt_ctx g_tt;
void app_comm_init(void) { tt_init(&g_tt, hw_send, hw_ms, tt_params, 3, tt_chans, 2); }
void app_comm_tick_10ms(void) { tt_tick(&g_tt, hw_ms()); }
/* 收到字节时(ISR 里也行): tt_feed(&g_tt, buf, n); */
```

跑起来后,PC 侧:`python tuning_tool.py --profile <你的profile>`,
浏览器打开 `http://127.0.0.1:9898/ui` 即可看曲线、改参数。

---

## 2. 参数改造检查清单(现有变量要满足什么)

**已替你做完**:协议值→变量是指针直写(`SET kp 2.0` → `*(float*)&g_kp = 2.0f`),
变量长什么样、叫什么、在哪,协议层不关心。范围检查用 `TT_PARAM_FR` / `TT_PARAM_IR`,
单位换算用结构体字段 `scale/off`(协议值 = raw×scale + off)。

**仍需自己处理的三条**:

| # | 条件 | 不满足怎么办 |
|---|---|---|
| 1 | 可写标量:`float` / `int32_t` | `#define` 宏→改变量;`const`→去掉;位域/打包成员→提出独立变量;枚举→用 int32 接收自己 switch |
| 2 | 跨任务可见性(通信任务写,控制任务读) | 变量加 `volatile`;多核(如 TC387 CPU0/CPU1)确认所在内存区跨核一致、不落在单核本地 cache 上 |
| 3 | 生效时机由你定 | 口径 A/B 见下,选一个即可 |

32 位对齐的单值写是原子的,不会撕裂;但**一组关联参数**(如 kp+ki 要一起换)逐条 SET
中间存在半新半旧窗口——调参场景可接受,介意就用口径 B。

**生效时机两个口径**:

```c
/* 口径 A:直写 volatile,下个控制周期自然看到(最简单) */
volatile float g_kp = 2.0f;

/* 口径 B:影子参数,周期边界原子切换(关联参数同拍生效;
          原车"档位值发车才灌运行时"同款思想) */
struct ctrl_params { float kp, ki, kd; };
struct ctrl_params g_live   = {2.0f, 0, 0.1f};  /* 控制任务只读这份 */
struct ctrl_params g_shadow = {2.0f, 0, 0.1f};  /* tt_link 写这份   */
void ctrl_tick(void) { g_live = g_shadow; ... }
```

**flash 持久化不在契约里**:tt_link 只写 RAM,断电丢失(故意的,保持轻量)。
要断电保持就自己加个 `SAVE` 领域命令——普通字符串命令,协议不拦。

---

## 3. 命令/帧速查(固件侧义务)

| 命令 | 行为 | 回执(带 `!<seq>` 时) |
|---|---|---|
| `SET <key> <v>` | 查参数表直写变量,范围/标定按表定义 | `OK,<seq>` / `ERR,<seq>,UNKNOWN_KEY|RANGE|NAN` |
| `RATE <chan> <hz>` | 改通道速率,0=关,上限 1000 | `OK` / `ERR,UNKNOWN_CHAN|RANGE` |
| `GET <key>` | 回一条 `P,<key>,<值>`(异步) | 另回 `OK` |
| `PING` | 链路自检 | `OK` |
| 其他 | — | `ERR,UNKNOWN_COMMAND` |

帧:`D,<chan>,<t_ms>,<v1>,<v2>,...`,列序由 profile 的 config.json 定义,
**固件与 profile 逐列对齐靠人盯**(契约故意不做运行时协商)。

---

## 4. 自测与对照

- `tt_link_test.c`:**协议逻辑自测**(27 项,PC 侧 gcc 即跑,不依赖硬件)。
  `gcc -std=c99 -Wall -Wextra tt_link_test.c -o tt_link_test && ./tt_link_test`
- `profiles/virtual/virtual_device.py` 是本文件的 Python 等价物——先拿它把
  桥/UI/agent 全链跑通(零硬件),再对着它验真固件行为。两边行为差异 = 固件 bug 或
  列序没对齐,不是协议问题。
