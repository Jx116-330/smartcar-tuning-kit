# Virtual Profile — 契约 v1 演示(零硬件全链跑通)

一个"假固件" `virtual_device.py`:TCP 客户端连桥 8080,推 D 帧
(ctl 50Hz / params 5Hz / stats 1Hz),响应 SET / RATE / GET / PING。

## 跑法

```bash
# 1. 起虚拟设备(模拟固件侧)
python profiles/virtual/virtual_device.py

# 2. 另开终端,起桥(加载本 profile;exe 同理:YawTuningTool.exe --profile virtual)
python tuning_tool.py --profile virtual

# 3. 验证(浏览器开 http://127.0.0.1:9898/ui 或 curl)
curl -s http://127.0.0.1:9898/latest          # packets.ctl/params/stats 三通道
curl -s http://127.0.0.1:9898/schema          # schema_hash = virtual-demo-v2
curl -s -X POST http://127.0.0.1:9898/batch -d "{\"commands\":[{\"cmd\":\"SET kp 2.0\",\"expect\":\"ACK\"}]}"
curl -s http://127.0.0.1:9898/latest          # packets.params.kp 回读 = 2.0
```

## 通道列序(与 config.json protocol.channels 逐列一致)

| 通道 | 帧 | 字段 |
|---|---|---|
| ctl | `D,ctl,<t_ms>,<setpoint>,<pos>,<err>,<duty>` | 正弦设定值跟踪的 PID 仿真 |
| params | `D,params,<t_ms>,<kp>,<ki>,<kd>,<max_speed>,<amp>,<freq>,<noise>` | 参数读回通道 |
| stats | `D,stats,<t_ms>,<uptime_s>,<frames>,<dropped>` | 链路统计 |

## 命令

- `SET <kp|ki|kd|max_speed|amp|freq|noise> <v>` — 设参(带 `!<seq>` 回 OK/ERR)
- `RATE <ctl|params|stats> <hz>` — 通道速率,0=关
- `GET <key>` — 异步回 `P,<key>,<value>`(console 可见)
- `PING` — 链路自检

schema 另有 planner 兼容别名 `PID_SET/PID_GET/PLANT_SET/PLANT_GET`(线上格式
与 SET/GET 完全一致),调参面板由此在本 profile 可设可读;`actions` 声明了
一条无害 PING 演示动作,用于验证前端安全动作链路(渲染/发送/状态)。

## 容错演示点

- 不带 `!<seq>` 的 SET 静默生效 → 靠 params 通道读回验证(agent 的正确性口径)
- 杀掉/重启设备进程 → 桥 connections 归零再回 1,无任何状态机卡死
