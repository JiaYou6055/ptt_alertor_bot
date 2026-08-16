# Domain Context: PTT Alertor Bot

## Glossary

### System Error (系統異常)
An unexpected runtime exception or failure that occurs within the bot execution, crawler fetching loop, or Telegram update processing.

### Pending Error (待處理異常)
A `System Error` that has been recorded in the database and requires admin attention or monitoring. Its status remains `pending` until explicitly marked as resolved by an admin.

### Resolved Error (已修復/歸檔異常)
A `System Error` whose status has been updated to `resolved` via the `/err_resolve` command, indicating that the root cause has been investigated or fixed.

### Error Throttling (異常收斂/防洗版)
A mechanism that groups identical errors occurring within a short time window (e.g. 10 minutes) so that only the first occurrence triggers a Telegram alert to the Admin, while subsequent occurrences silently increment `occurrence_count` and update `last_seen` in the database.
