# maya-ledger

一个记账本。网页自己用，Claude 通过 MCP 也能记。同一份数据。

---

## 地址

| 用途 | 地址 |
|---|---|
| 记账网页 | https://ledger.ob1009.top |
| MCP 接口 | https://ledger-mcp.ob1009.top/`令牌`/mcp |
| 健康检查 | https://ledger-mcp.ob1009.top/health |

---

## 东西都在哪

```
/home/maya-ledger/
├── index.html          网页（nginx 容器挂载这个目录，只读）
├── icon.png            桌面图标
├── server.py           后端
├── requirements.txt
├── venv/               Python 环境
└── data/ledger.db      所有账目和纸条都在这里
```

- **后端**：systemd 服务 `maya-ledger`，端口 18002
- **网页**：Docker 容器 `maya-ledger`（nginx:alpine），端口 8080
- **外网**：Cloudflare Tunnel，两条 Public Hostname 分别指向 8080 和 18002
- **防火墙**：ufw 只对 Docker 内网放行 18002

两把钥匙，都在 `/etc/systemd/system/maya-ledger.service` 里：

- `LEDGER_TOKEN` —— Claude 用，在 MCP 地址里
- `LEDGER_WEB_PASSWORD` —— 打开网页用

**这两个不在 GitHub 上，仓库是公开的。**

---

## 常用命令

改完代码（在 GitHub 上改）之后更新：

```bash
cd /home/maya-ledger && git pull && systemctl restart maya-ledger
```

网页改动 `git pull` 完立刻生效，不用重启容器。

看后端还活着吗：

```bash
systemctl status maya-ledger
curl -s localhost:18002/health
```

出问题看日志：

```bash
journalctl -u maya-ledger -n 50 --no-pager
```

换密码或令牌：改 service 文件里那一行 → `systemctl daemon-reload && systemctl restart maya-ledger`。
换了令牌，Claude 那边的连接器 URL 也要跟着换。

---

## 备份

**已经自动了。** 每天凌晨三点，`/root/backup-ledger.sh` 把数据库推到私有仓库
`ledger-backup`，存两份：`ledger.db`（原始）和 `ledger.json`（能直接看）。

想立刻备份一次：

```bash
/root/backup-ledger.sh
```

看定时任务还在不在：

```bash
crontab -l
```

服务器真没了怎么恢复：新开一台机器，把仓库里的 `ledger.db` 放回
`/home/maya-ledger/data/`，其余按本文档重装一遍。

---

## 踩过的坑

- **Vultr 网页 Console 粘长文本会吞字** —— 所以代码都走 GitHub，Console 只粘命令
- **421 Invalid Host** —— MCP 库默认只认 localhost，要在 `FastMCP()` 里配 `transport_security`
- **Tunnel 502** —— 多半是 ufw 没放行那个端口。Docker 发布的端口不受 ufw 管，普通进程受
- **MCP 地址必须以 `/mcp` 结尾**
- **加了新连接器要开新对话** —— 旧对话不会加载
- **改了图标或状态栏设置，要删掉桌面图标重新添加** —— iOS 只在添加那一刻读这些设置
- **手机上看不到改动** —— Safari 缓存，等一会儿或用无痕窗口确认
- **Console 输不了中文** —— 带中文的命令粘进去会被吞成空字符串，看着像成功了其实是空的。
  中文内容让 Claude 通过 MCP 写，或者走 GitHub 传文件

---

## 别碰

同一台服务器上跑着 ob1009（端口 18001，Docker 容器 `ombre-brain`），跟这个项目没关系。
改这边的时候别动它。
