<div align="center">

# dnsmasq-dhcp-ui

**一个容器 = 局域网 DHCP/DNS 服务器 + 中文 Web 管理界面**

静态保留 · 动态分配 · 按设备下发 DNS · 租约总览（IP / MAC / 主机名 / 剩余租期 / 静态或动态）

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Base Image](https://img.shields.io/badge/base-alpine%203.20-0D597F?logo=alpinelinux&logoColor=white)](https://alpinelinux.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](#技术选型)
[![Platform](https://img.shields.io/badge/platform-linux%2Famd64%20%7C%20linux%2Farm64-lightgrey)](#构建镜像)
[![GitHub stars](https://img.shields.io/github/stars/mmvc175/dnsmasq-dhcp-ui?style=flat-square)](https://github.com/mmvc175/dnsmasq-dhcp-ui/stargazers)

[English](#english) · [功能](#-功能) · [快速开始](#-快速开始) · [配置](#-配置说明) · [批量导入](#-批量导入静态绑定) · [API](#-http-api) · [常见问题](#-常见问题) · [致谢](#-致谢)

</div>

---

## English

**dnsmasq-dhcp-ui** packages [dnsmasq](https://thekelleys.org.uk/dnsmasq/doc.html) together with a self-contained web UI into a single Docker image, turning any Linux host into a LAN DHCP/DNS server you manage from a browser.

**Highlights**

- Dynamic address pool with configurable range, netmask and lease time
- Static reservations by MAC, with **per-device gateway and DNS**
- **Bulk editor** — paste `dhcp-host=` lines, get live parsing with per-line error reporting, then merge or replace
- Lease overview: IP, MAC, hostname, **remaining lease time**, static vs. dynamic, online status
- Optional DNS forwarding/caching (`DNS_ENABLED=0` for a DHCP-only container)
- Zero third-party dependencies — Python standard library only, image ≈ 30 MB
- Every save is validated with `dnsmasq --test` and rolled back automatically if it fails

**Quick start**

```bash
cp .env.example .env    # set DHCP_INTERFACE, address pool, DNS …
docker compose up -d
```

Then open `http://<host-ip>:8080`.

> **Requires a Linux host with `--network host`** — DHCP relies on broadcast, so Docker Desktop on Windows/macOS will not work. Turn off your router's built-in DHCP server first.

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [功能](#-功能)
- [界面说明](#-界面说明)
- [实现要点](#-实现要点)
- [部署前提（重要）](#-部署前提重要)
- [快速开始](#-快速开始)
- [配置说明](#-配置说明)
- [从旧版升级](#-从旧版升级)
- [批量导入静态绑定](#-批量导入静态绑定)
- [HTTP API](#-http-api)
- [开发](#-开发)
- [常见问题](#-常见问题)
- [安全说明](#-安全说明)
- [贡献](#-贡献)
- [致谢](#-致谢)
- [许可](#-许可)

---

## 它解决什么问题

把 dnsmasq 当 DHCP 服务器用很省事，但配置全靠手写 `dnsmasq.conf`，而且**看不到租约**——谁在线、分到了哪个 IP、还剩多久到期，都得自己 `cat /var/lib/misc/dnsmasq.leases` 再算。

现成的开源方案各缺一块：

| 参考项目 | 能做什么 | 缺什么 |
|---|---|---|
| [`jpillora/docker-dnsmasq`](https://github.com/jpillora/docker-dnsmasq) | 把 dnsmasq 装进容器，配 webproc 编辑配置 | 只能改配置文本，看不到租约 |
| DNSmasq Lease Viewer | 查看租约（IP / MAC / 主机名 / 过期时间） | 不是 Docker 部署，只能看不能管 |
| [`goodwe1l/xiaomi-ax6000-dnsmasq-ui`](https://github.com/goodwe1l/xiaomi-ax6000-dnsmasq-ui) | 完整的静态绑定 + 租约管理交互 | 深度依赖 OpenWrt 的 UCI，Docker 里跑不起来 |

本项目把三者的能力合并，**去掉 UCI 依赖，改为直接生成 dnsmasq 原生配置**
（`dhcp-host` + `set:tag` + `dhcp-option=tag:…`），因此可以在任意 Linux 机器上以 Docker 方式运行。

## ✨ 功能

- **DHCP 服务**
  - 动态地址池（起止 IP + 子网掩码 + 租期）
  - 按 MAC 做**静态地址保留**，可逐台指定网关与 DNS
  - **批量编辑**：直接粘贴多行 `dhcp-host=` 文本，实时解析预览、错误行号提示，
    支持"追加并更新"与"整体替换"两种模式，也支持导入 / 导出配置文件
  - 全局下发网关（option 3）与 DNS（option 6），支持一主一备
  - 域名后缀（option 15），静态绑定主机可通过 `主机名.lan` 解析
  - 一键开关 DHCP、权威模式
  - **附加指令**：界面没覆盖的 dnsmasq 指令可直接写进文本框，原样追加到生成的配置
- **DNS 服务**（可关）
  - 上游转发 + 缓存，可指定多个上游 DNS
  - 关闭时 `port=0`，容器只做 DHCP
- **客户端总览**
  - IP、MAC、主机名、**剩余租期**（如"11小时23分" / "永久" / "已过期"）
  - 分配方式：**静态保留 / 动态分配**
  - 在线状态（绿点）、最后活跃时间（"3 分钟前"）
  - 搜索过滤，可一键把动态客户端"设为静态"
  - "刷新在线"按钮：主动 ping 一轮，补充在线判定
- **可靠性**
  - 可选 Basic Auth（`WEB_USER` / `WEB_PASS`）
  - 保存前自动做 `dnsmasq --test` 自检，失败自动回滚（配置不会半途落盘）
  - dnsmasq 崩溃自动拉起（指数退避）
  - 可查看生成的 dnsmasq 配置原文和运行日志

## 📋 界面说明

<!-- 截图占位：把图片放到 docs/screenshot.png 后，删掉下面两行的注释符即可 -->
<!-- ![界面截图](docs/screenshot.png) -->

| 页签 | 内容 |
|---|---|
| **在线客户端** | 租约总表：在线状态、主机名、IP、MAC、静态/动态、剩余租期、最后活跃，支持搜索 |
| **静态绑定** | 增删改 MAC→IP 保留，可逐台指定网关 / DNS / 租期 / 备注，支持临时停用；「批量编辑」直接粘 `dhcp-host=` 文本批量导入，或导出为 .conf 文件 |
| **DHCP 配置** | 地址池、掩码、租期、网关、下发 DNS、域名、权威模式、监听接口、附加指令 |
| **DNS 配置** | 开关 DNS 服务、上游 DNS、是否忽略 resolv.conf |
| **运行日志** | dnsmasq 最近 800 行日志 |

顶部四个数字是总客户端数、在线数、静态绑定数、动态分配数。

### 剩余租期与在线状态怎么算的

- **剩余租期**直接来自 dnsmasq 租约文件的过期时间戳；`0` 表示永久。
- **在线状态**由两部分判定：
  1. dnsmasq 每次 DHCP 交互（分配 / 续约）都会回调 `scripts/lease_notify.py` 记录时间戳；
  2. 点击"刷新在线"会并发 ping 所有已知 IP 再补一轮。

  判定窗口取 `ONLINE_WINDOW` 与"半个租期"的较大值，避免长租期下误判离线。

> 手机、IoT 设备会休眠，长时间不发 DHCP 报文也 ping 不通时会显示为离线，属正常现象。

## 🔧 实现要点

### 按设备下发网关 / DNS 是怎么做到的

dnsmasq 没有"每台设备独立配置"的直接语法，本项目用**标签（tag）**组合实现：

```ini
dhcp-host=11:22:33:44:55:66,set:host_112233445566,192.168.1.20,iot,7d
dhcp-option=tag:host_112233445566,3,192.168.1.254
dhcp-option=tag:host_112233445566,6,223.5.5.5
```

静态绑定条目自动生成 `set:host_<mac去冒号>`，只有带该标签的客户端才会收到对应的 option 3 / 6。

### 技术选型

- **Python 标准库，零第三方依赖。** image 里只有 `alpine + dnsmasq + python3 + tini`，
  体积约 30 MB，也不需要 `pip install`。
- **前端零构建。** 原生 HTML / CSS / JS，改完刷新页面即可，无需 node 工具链。
- **配置事务化。** 任何保存操作都走同一条路径：写临时配置 → `dnsmasq --test` 校验 →
  通过才落盘并重启进程，失败则回滚到上一份配置。

### 已知的并发处理

`dhcp-script` 回调是独立进程，会与主进程同时写状态文件。因此活跃记录（`activity.py`）
采用 **`fcntl` 文件锁 + 原子替换 + 删除集合**，避免多进程覆写丢更新。

## ⚠️ 部署前提（重要）

1. **必须部署在 Linux 宿主机上。** Docker Desktop（Windows / macOS）的 host 网络是虚拟机内的，
   客户端拿不到地址。请部署在 Linux 服务器、NAS（群晖 / UnRAID / OpenMediaVault）或树莓派上。
2. **DHCP 依赖广播，必须使用 host 网络**（或用 macvlan，见下文）。
3. **局域网内请先关掉原有 DHCP**（一般是路由器上的），避免两个 DHCP 抢答。
4. 若同时启用 DNS 服务，宿主机的 **53 端口不能被占用**。
   systemd-resolved 常占用它，处理方式见[常见问题](#-常见问题)。

## 🚀 快速开始

### 方式一：docker compose（推荐）

```bash
git clone https://github.com/mmvc175/dnsmasq-dhcp-ui.git
cd dnsmasq-dhcp-ui
cp .env.example .env      # 按需修改
docker compose up -d
```

### 方式二：docker run

```bash
docker run -d \
  --name dnsmasq-dhcp-ui \
  --network host \
  --cap-add=NET_ADMIN --cap-add=NET_RAW --cap-add=NET_BIND_SERVICE \
  --restart unless-stopped \
  -v /var/docker/dnsmasq-dhcp-ui/data:/data \
  -e WEB_USER=admin \
  -e WEB_PASS=change-me-please \
  -e DHCP_INTERFACE=eth0 \
  -e DHCP_RANGE_START=192.168.1.110 \
  -e DHCP_RANGE_END=192.168.1.250 \
  -e DHCP_NETMASK=255.255.255.0 \
  -e DHCP_GATEWAY=192.168.1.1 \
  -e DHCP_DNS=192.168.1.1,223.5.5.5 \
  -e DNS_ENABLED=0 \
  mmvc175/dnsmasq-dhcp-ui:latest
```

然后浏览器打开 `http://<宿主机IP>:8080`。

> `DHCP_INTERFACE` 要改成实际的网卡名（`ip addr` 查看，常见 `eth0` / `ens18` / `enp0s3`）。
> 留空表示监听除 `lo` 外的所有接口。

### 方式三：macvlan（容器有独立 IP，不用 host 网络）

参考 [`docker-compose.macvlan.yml`](docker-compose.macvlan.yml)，里面有完整的网络创建命令和注意事项。

### 构建镜像

```bash
# 单架构（当前机器）
./scripts/build.sh

# 多架构 amd64 + arm64（需要 buildx）
./scripts/build.sh --multiarch --push mmvc175/dnsmasq-dhcp-ui
```

镜像基于 `alpine:3.20`，包含 `dnsmasq + python3 + tini`，无第三方 Python 依赖，体积约 30 MB。

## ⚙️ 配置说明

### 环境变量（只作为**首次启动**的初始值）

首次启动后配置写入 `/data/config.json`，之后以该文件为准，
环境变量里只有 `WEB_USER` / `WEB_PASS` 每次都会生效。想重置配置：删掉 `config.json` 重启容器。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WEB_PORT` | `8080` | Web 管理端口 |
| `WEB_USER` / `WEB_PASS` | 空 | 同时设置即开启 Basic Auth |
| `DHCP_INTERFACE` | 空 | 监听网卡，留空 = 除 lo 外全部 |
| `DHCP_RANGE_START` / `DHCP_RANGE_END` | `192.168.1.100` / `.200` | 动态地址池 |
| `DHCP_NETMASK` | `255.255.255.0` | 支持 `255.255.255.0` 或 `/24` |
| `DHCP_LEASE_TIME` | `24h` | 支持 `30m` `12h` `7d` `infinite` |
| `DHCP_GATEWAY` | 空 | option 3，留空不下发 |
| `DHCP_DNS` | 空 | option 6，逗号分隔，按顺序下发 |
| `DHCP_DOMAIN` | `lan` | option 15 |
| `DNS_FORWARDERS` | 阿里 / 114 | 上游 DNS，逗号分隔 |
| `DNS_ENABLED` | `1` | 设为 `0` 关闭 DNS 服务 |
| `ONLINE_WINDOW` | `900` | 最后活跃在多少秒内算在线 |

### 数据持久化

容器内 `/data` 目录保存全部状态，挂载它即可在重建容器后保留配置：

```text
/data/config.json        界面上的全部配置
/data/dnsmasq.leases     dnsmasq 租约文件
/data/activity.json      客户端最后活跃时间（判定在线用）
/data/hosts.dnsmasq      静态绑定的本地解析记录
```

## ⬆️ 从旧版升级

代码更新后重新构建并拉起容器即可，**配置与租约都在 `./data` 里，不会丢**：

```bash
cd dnsmasq-dhcp-ui
git pull
docker compose up -d --build
```

用 `docker run` 部署的，把上面的 `docker run` 命令原样重跑一遍（数据卷挂载路径保持不变）。

**回滚**：镜像未打多标签时，先给当前版本补个标签再升级，方便回退：

```bash
docker tag dnsmasq-dhcp-ui:latest dnsmasq-dhcp-ui:backup
# 出问题时
docker compose down && docker tag dnsmasq-dhcp-ui:backup dnsmasq-dhcp-ui:latest && docker compose up -d
```

> 配置格式向前兼容。若新版本新增了配置字段，旧 `config.json` 会以上一次的值补齐默认项，无需手工迁移。

## 📥 批量导入静态绑定

有一大批设备要固定 IP 时，不用一条条点。进入 **静态绑定 → 批量编辑**，
把已有的 `dhcp-host=` 配置整段粘进去即可：

```ini
dhcp-host=6E:53:C6:69:A9:DA,set:host_6e53c669a9da,192.168.1.21,fnos,infinite
dhcp-host=42:F2:2C:11:F9:23,set:host_42f22c11f923,192.168.1.22,win10-edge
dhcp-host=B0:5C:DA:6A:AD:78,set:host_b05cda6aad78,192.168.1.31,M427FN
dhcp-host=58:05:D9:3C:1A:F1,set:host_5805d93c1af1,192.168.1.32,EPSON
```

文本框下方会**实时**给出解析结果与错误行号，确认无误后选择保存模式：

| 模式 | 行为 |
|---|---|
| **追加并更新** | 按 MAC 合并：已存在的覆盖，其余保留 |
| **整体替换** | 清空现有绑定，完全以文本框内容为准 |

解析器支持：

- `set:标签`、`infinite` 永不过期、`ignore` 停用该设备
- 冒号 MAC（`AA:BB:CC:DD:EE:FF`）与点分 MAC（`aabb.ccdd.eeff`）
- 裸行（从表格里直接复制的 `MAC,IP,名称`，允许没有 `dhcp-host=` 前缀）
- `dhcp-option=tag:标签,3,网关` 与 `dhcp-option=tag:标签,6,DNS1,DNS2`
  会自动回填到对应设备的网关 / DNS
- 完整的 dnsmasq.conf 直接整份粘贴也不会报错——不属于绑定类的指令会被识别并跳过

保存前会检查 **MAC 重复**、**IP 冲突**（含与已有绑定、与动态地址池的冲突），并指出具体行号。

也支持 **导出** 当前全部绑定为 `.conf` 文件，便于备份或迁移到别的 dnsmasq。

## 🔌 HTTP API

所有接口返回 `{"ok": true, "message": "...", "data": {...}}`，
出错时 `ok=false` 且 `message` 为中文原因。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 配置 + 租约 + 服务状态（前端首屏一次性取全） |
| GET | `/api/leases` | 租约列表 |
| GET/POST | `/api/config` | 读取 / 保存 DHCP 与 DNS 全局配置 |
| GET/POST | `/api/static` | 读取 / 新增或更新静态绑定 |
| POST/DELETE | `/api/static/delete` | 删除静态绑定（body: `{"mac": "..."}`） |
| POST | `/api/static/preview` | 解析批量文本并返回逐行结果与冲突检查（body: `{"text": "...", "mode": "merge\|replace"}`） |
| POST | `/api/static/import` | 按 `merge` / `replace` 批量导入（字段同 preview） |
| GET | `/api/static/export` | 导出全部启用绑定为 dnsmasq 配置文本 |
| POST | `/api/service/reload` | 重新生成配置并重启 dnsmasq |
| POST | `/api/probe` | 主动 ping 一轮刷新在线状态 |
| GET | `/api/logs?limit=300` | dnsmasq 日志 |
| GET | `/api/raw` | 生成的 dnsmasq 配置原文 |

示例：

```bash
# 导出当前全部静态绑定
curl -s http://192.168.1.10:8080/api/static/export

# 先预览（不落盘），确认无误再导入
curl -s -X POST http://192.168.1.10:8080/api/static/preview \
  -H 'Content-Type: application/json' \
  -d '{"text":"dhcp-host=AA:BB:CC:DD:EE:FF,192.168.1.50,nas","mode":"merge"}'
```

## 💻 开发

```text
app/
  main.py           入口：初始化、拉起 dnsmasq 与 Web
  api.py            HTTP 接口（/api/state、/api/leases、/api/static、/api/config …）
  config.py         配置模型、校验、持久化
  dnsmasq_conf.py   配置渲染 + dnsmasq --test 自检
  dnsmasq_proc.py   子进程守护、日志环形缓冲、崩溃自愈
  leases.py         租约解析、剩余租期、在线判定
  activity.py       活跃记录（文件锁 + 原子写，支持多进程并发）
  bulk.py           批量导入解析器（dhcp-host / dhcp-option 文本 → 结构化条目）
  probe.py          并发 ping 探测
  web/              前端（原生 HTML/CSS/JS，无构建步骤）
scripts/
  lease_notify.py   dnsmasq dhcp-script 回调
  healthcheck.py    容器健康检查
  build.sh          镜像构建
tests/              单元测试 + API 冒烟测试
```

```bash
# 运行测试（需要 Python 3.9+，无第三方依赖）
python3 -m unittest discover -s tests

# 前端语法检查
node --check app/web/app.js
```

测试覆盖配置渲染、租约解析与剩余租期计算、批量解析器（含往返幂等）、
以及真实起 HTTP 服务的 API 冒烟测试与启动流程测试。

## ❓ 常见问题

**Q：容器起来了但客户端拿不到地址？**

- 确认是 Linux 宿主机 + `--network host`；
- 确认 `DHCP_INTERFACE` 填的是实际网卡名（`docker logs dnsmasq-dhcp-ui` 能看到 dnsmasq 绑定信息）；
- 确认路由器上的 DHCP 已关闭；
- 宿主机防火墙是否放通 UDP 67/68。

**Q：启动时报 `address already in use`（53 端口）？**

宿主机的 `systemd-resolved` 占用了 53。任选一种：

```bash
# 方案 A：关闭 systemd-resolved
systemctl disable --now systemd-resolved

# 方案 B：不使用本容器的 DNS 功能，只做 DHCP
-e DNS_ENABLED=0
```

**Q：想让容器有独立 IP，不用 host 网络？**

用 macvlan，参考 [`docker-compose.macvlan.yml`](docker-compose.macvlan.yml)。

**Q：静态 IP 应该在地址池内还是池外？**

都可以。dnsmasq 会自动避免把静态占用的地址分给其它设备。
建议把服务器 / NAS / 打印机这类设备放在池外（如 `.2`–`.50`），池子只给访客和移动终端。

**Q：有一大堆静态地址要配，一条条点太慢？**

见[批量导入静态绑定](#-批量导入静态绑定)。

**Q：想改租期但不想重启服务？**

界面上任何保存操作都会重新生成配置并重启 dnsmasq，重启耗时在毫秒级，
期间不会影响已分配的客户端（租约文件保留）。

**Q：如何备份 / 迁移？**

复制 `/data` 目录即可。换机器时改一下 `DHCP_INTERFACE` 和地址池。

## 🔒 安全说明

- 管理界面**默认不做认证**，设计前提是部署在可信内网。暴露到公网前请务必设置
  `WEB_USER` / `WEB_PASS`，或放在反向代理 / VPN 之后。
- 容器需要 `NET_ADMIN`、`NET_RAW`、`NET_BIND_SERVICE` 能力才能操作 DHCP 与 53 端口，
  这属于该场景的必要权限，请知悉。
- 附加指令（自定义配置片段）会**原样写入** dnsmasq 配置。
  为避免配置被夺管，`conf-file`、`conf-dir`、`dhcp-leasefile`、`dhcp-script`、`addn-hosts`
  等由系统托管的指令会被拒绝。
- 仓库中不包含任何默认口令，`.env` 已被 `.gitignore` 忽略，请勿把真实配置提交进版本库。

## 🤝 贡献

欢迎提 Issue 和 PR。提交前请：

1. 跑通 `python3 -m unittest discover -s tests`（当前 75 项全绿）；
2. `node --check app/web/app.js` 确认前端语法无误；
3. 涉及配置渲染的改动，请附上生成的配置片段便于核对。

报 Bug 时请附上：部署方式（host / macvlan）、`DHCP_INTERFACE`、宿主发行版、
`docker logs` 相关片段。

## 🙏 致谢

本项目的设计思路与数据模型来自以下开源项目，在此致谢：

| 项目 | 作者 | 许可 | 借鉴的内容 |
|---|---|---|---|
| [`jpillora/docker-dnsmasq`](https://github.com/jpillora/docker-dnsmasq) | Jaime Pillora | MIT | 把 dnsmasq 装进容器对外提供服务的整体思路与配置骨架 |
| DNSmasq Lease Viewer | Lars Bengtsson | MIT | 租约文件解析、剩余租期计算，以及"纯 Python 标准库、零依赖"的实现取向 |
| [`goodwe1l/xiaomi-ax6000-dnsmasq-ui`](https://github.com/goodwe1l/xiaomi-ax6000-dnsmasq-ui) | goodwe1l | 未随仓库附许可证 | 静态租约 + 标签模板 + 逐设备网关 / DNS 的数据模型与交互设计 |

本项目为独立实现，未直接复制上述项目的源代码；致谢的是设计启发。为满足 MIT 许可的署名要求，
被参考项目的版权声明原文记录如下：

```text
Copyright (c) 2018 Jaime Pillora        — jpillora/docker-dnsmasq (MIT)
Copyright (c) 2026 Lars Bengtsson       — DNSmasq Lease Viewer (MIT)
```

其他第三方组件：

- [dnsmasq](https://thekelleys.org.uk/dnsmasq/doc.html) — Simon Kelley 等，以 GPL 授权发布，
  在镜像中作为独立程序使用与分发
- [Alpine Linux](https://alpinelinux.org/) — 基础镜像
- [tini](https://github.com/krallin/tini) — 容器 init（PID 1）

## 📄 许可

本项目以 [MIT 许可](LICENSE) 发布。

