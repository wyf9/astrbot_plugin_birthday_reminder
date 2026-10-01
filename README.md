# astrbot_plugin_birthday_reminder

记录群成员生日，并在生日当天自动发送祝福的 [AstrBot](https://astrbot.app) 插件。

支持手动录入与从群公告自动抓取，支持 `@全体成员`、年龄动态计算、每日定时检测等。

> 目前生日提醒 / 抓取相关功能主要面向 **aiocqhttp（OneBot v11，如 Napcat / Lagrange）** 平台。

## 功能

- 手动增删查生日记录
- 从群公告抓取生日信息（CSV / 正则两种解析方式）
- 每日定时检测并自动发送生日祝福
- 生日消息模板化，支持 `{uin}`、`{nick}`、`{at}`、`{age}` 占位符
- 可选 `@全体成员`（需 bot 为群管理员/群主，并配置白名单）

## 指令

所有指令默认 **仅管理员** 可用。

| 指令 | 说明 |
| --- | --- |
| `/birthday add <uin> <nick> <year> <month> <day>` | 手动添加生日，例：`/birthday add 2191161566 wyf9 2010 1 1` |
| `/birthday del <uin 或 nick>` | 删除生日（精准匹配，先匹配 uin 再匹配 nick） |
| `/birthday list [all]` | 查看即将过生日的人；默认本群，加 `all` 查看全部 |
| `/birthday crawl [force/--force]` | 手动触发从群公告抓取；添加 `force` 或 `--force` 会重新处理已抓取过的公告 |
| `/birthday trigger` | 手动触发一次生日检测 |
| `/birthday test <uin> <nick> <year> <month> <day>` | 测试生日提醒效果（参数同 add） |

## 记录模式

在插件配置 `record_mode` 中选择，两种模式数据 **分开存储**：

- **per_group（默认）**：每条记录归属某个群。`/birthday list` 默认只显示本群记录；从公告抓取的记录归属来源群。
- **global**：记录以 QQ 号全局唯一。`/birthday list` 在群内时按 **本群成员** 过滤显示（不在本群的用户不显示）。

添加 / 抓取时若记录已存在会提示冲突并跳过；抓取过程中产生的冲突/跳过详情会显示在 `/birthday list` 结果的上方，便于管理员排查（无冲突则不显示）。

## 从群公告抓取

在 `crawl` 配置中设置：

- `group_whitelist`：抓取来源群号白名单
- `trigger_word`：公告触发词，仅包含该词的公告才会被解析（留空则解析全部）
- `parse_mode`：解析方式
  - `csv`：每行 `QQ号,昵称,年,月,日`；公告中的标题、说明和不符合格式的行会被跳过，例如：

    ```csv
    QQ号,昵称,年,月,日
    2191161566,wyf9,2010,1,1
    ```

    公告中的实际换行、`&#10;` / `&#13;`、`&NewLine;`、`<br>` 等换行表示都会被识别。

  - `regex`：使用配置的正则（需包含命名组 `uin,nick,year,month,day`）全局匹配
- `trigger_mode`：自动触发方式
  - `off`：仅手动 `/birthday crawl`
  - `poll`：按 `poll_interval_minutes` 定时轮询公告
  - `event`：监听协议端群通知事件实时触发
  - `both`（推荐）：两者同时，内置去重不会重复处理同一条公告

## 生日提醒

- 每日在 `check_time`（默认 `00:00`）自动检测，也可用 `/birthday trigger` 手动触发。
- `feb29_mode`：2 月 29 日生日在平年的处理方式
  - `feb28`（默认）：平年顺延到 2 月 28 日提醒
  - `mar1`：平年顺延到 3 月 1 日提醒
  - `none`：不顺延，仅闰年 2 月 29 日当天提醒（四年一次）
- 消息模板 `reminder_template`，可用占位符：
  - `{uin}` QQ 号
  - `{nick}` 昵称
  - `{at}` @ 该用户
  - `{age}` 年龄（出生年未知时显示 `?`）
- 合法示例：`今天是 {at} 的生日，让我们祝 {nick} 生日快乐！！！🎂🥳`

### @全体成员

在 `at_all` 配置中启用。仅当满足以下所有条件时才会附加 `@全体成员`：

1. `at_all.enable` 为 `true`
2. 目标群在 `group_whitelist` 中（留空为不限制）
3. 生日者在 `user_whitelist` 中（留空为不限制）
4. bot 在该群为 **管理员或群主**

## 开发

使用 [uv](https://docs.astral.sh/uv/) 管理依赖，使用 [prek](https://github.com/j178/prek) 作为 pre-commit 钩子：

```bash
uv sync
prek install
prek run --all-files
```

提交前钩子按顺序执行：`ty check` → `ruff check --fix` → `ruff format` → `uv export`（生成 `requirements.txt`）。
