# GitHub PR 通知 · AstrBot 插件

将 GitHub PR 动态发送到 QQ，支持仅群聊、仅私聊或同时通知。

- PR 新建或重开：群内 @ 指定成员，或向指定 QQ 发送私聊。
- PR 合并或关闭：给原群通知贴表情（可配置）；私聊不发送关闭通知，也不贴表情。
- 通知状态页：查看送达结果、检查配置、发送测试通知和重试失败项。

## 安装

需要 **AstrBot 4.28.1+（4.x）** 和 **OneBot v11 / NapCat**。群表情功能需要支持 NapCat 的表情接口。

在 AstrBot 插件管理页选择“通过链接安装”，填写：

```text
https://github.com/Shxiao101/astrbot_plugin_pr_notify
```

## 配置

### 1. 填写通知目标

打开 **插件 → GitHub PR 通知 → 配置 → 仓库通知配置**，添加仓库并填写：

| 项目 | 填写说明 |
| --- | --- |
| 仓库名 | `owner/repo` |
| 仓库 Webhook Secret | 可填写独立密钥；留空沿用全局密钥 |
| 平台 ID | AstrBot 机器人页面中的平台 ID，例如 `default` |
| 接收者 QQ | 每项一个 QQ 号；群聊时 @ 这些成员，私聊时分别发送 |
| 通知模式 | `private` 私聊、`group` 群聊、`both` 同时通知 |
| 群号 | 使用群聊通知时必填 |
| 机器人 QQ | 通常留空；连接多个账号时指定 |

可另外填写合并、关闭表情 ID，留空则禁用。使用 NapCat 的表情 ID，不直接填写 emoji 字符。私聊接收者通常需要先加机器人为好友。

**保存并重载插件后生效。** 仓库和接收者都可在面板管理，无需聊天命令。

### 2. 设置 GitHub Webhook

先让 GitHub 能访问插件的 `/github/webhook`：默认监听端口为 **6196**，Docker 部署需映射该端口，可通过反向代理提供公网 HTTPS 地址。仅在面板填写公网地址不会自动配置网络。

打开 GitHub 仓库 **Settings → Webhooks → Add webhook**：

| 字段 | 值 |
| --- | --- |
| Payload URL | 公网回调地址，例如 `https://你的域名/github/webhook` |
| Content type | `application/json` |
| Secret | 复制该仓库的独立密钥；未填写时复制自动生成的全局密钥 |
| Events | 选择 **Let me select individual events → Pull requests** |

多个仓库可以共用回调地址，各自使用不同密钥。修改仓库密钥后，需要同步修改 GitHub Webhook 的 Secret，并重载插件。

## 检查与测试

打开 **插件 → GitHub PR 通知 → 插件页面 → 通知状态**，查看生效配置、机器人连接、最近 GitHub 请求及各目标的发送结果。

- **检查配置**：检查本地配置和机器人连接；收到真实 GitHub 请求后才显示“GitHub 已连通”。
- **测试群聊与表情 / 测试私聊**：向已配置目标发送标有“测试”的通知，群聊同时验证已配置的表情。
- **重试失败项**：明确失败会自动重试，耗尽后可手动重试。“结果未知”不会自动重发，手动重试可能产生重复消息。

GitHub 显示请求成功表示插件已接收，QQ 是否送达请以通知状态页为准。收不到通知时，先检查 Webhook 投递记录、机器人连接和接收者配置。

也支持聊天命令：AstrBot 管理员发送 `/notify` 查看帮助。面板添加的仓库请继续在面板管理。

## 许可证

[MIT License](LICENSE)
