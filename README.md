# iCloud 日历

这是一个原生 AstrBot 插件，让 AstrBot 的 AI 通过 CalDAV 查询、搜索、创建、修改和删除 Apple iCloud 日历日程。

## 安装

把发布 ZIP 上传到 AstrBot WebUI 的插件页面。安装完成后进入插件配置：

1. 填写 Apple Account 邮箱。
2. 在 [Apple Account](https://account.apple.com/) 的“登录与安全 → App 专用密码”中生成密码，并填入插件配置；不要使用主密码。
3. 在“新建日程目标日历”中填写日历名称或 `calendar_id`；插件创建的所有日程都会进入该日历。
4. 可在“过滤日历”中填写日历名称关键词或 `calendar_id`，多个值用逗号或换行分隔。命中的日历及其日程不会返回给 AI。
5. 确认默认时区。
6. 如需创建、修改或删除日程，打开“允许 AI 修改日历”。
7. 重载插件。

密码字段在 WebUI 中会被遮罩，但 AstrBot 配置文件本身并不加密，因此应保护 AstrBot 的 `data/config` 目录。

## AI 工具

- `icloud_list_calendars`
- `icloud_list_events`
- `icloud_get_event`
- `icloud_create_event`
- `icloud_update_event`
- `icloud_delete_event`
- `icloud_refresh_index`
- `icloud_index_status`

写工具除全局 `write_enabled` 外还要求 `confirmed=true`。AI 只有在用户明确要求该写操作时才能设置它。

`icloud_create_event` 不接受目标日历参数，目标只由管理员配置决定，避免 AI 把日程写入错误日历。过滤在数据交给 AI 之前完成，可减少无关日程占用的上下文和 token。

插件使用 AstrBot 的 `FunctionTool.call(context, **kwargs)` 注册工具。工具结果以字符串返回给 Agent，由模型整理成自然语言后再回复用户；插件不会把内部 JSON 作为 `tool_direct_result` 直接发送到聊天窗口，也不会使用存在版本差异的绑定方法 `handler` 调用方式。

## 巨量日程处理

- 所有远端日程查询都要求有界时间范围，单次默认最多 366 天。
- 查询按 7 天切片；单片响应超过 16 MiB 时继续二分，最小到 1 小时。
- 展开的循环日程写入 `data/plugin_data/astrbot_plugin_icloud_calendar/calendar-index.sqlite3`。
- SQLite 使用 WAL、范围索引和键集游标分页，避免大偏移分页。
- 更新与删除使用 ETag/`If-Match`，不会静默覆盖其他设备刚修改的日程。

## 访问控制

默认禁止群聊访问，默认禁止写操作。可通过 `allowed_sessions` 进一步限制允许访问的会话。插件不会记录或返回 Apple 凭据。

## 说明

插件结构和 Tool 注册方式依据 [AstrBot 官方插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html) 与 [AI Tool 指南](https://docs.astrbot.app/dev/star/guides/ai.html)。
