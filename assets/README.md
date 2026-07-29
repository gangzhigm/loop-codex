# 任务附件目录

每个任务使用独立目录：`assets/<task-id>/`。

- `original.*`：不可修改的原始图片或文档。
- `annotated.*`：标注、裁剪或脱敏后的派生文件。
- `analysis.json`：OCR、可确认事实、合理推断和待确认问题。
- `result.*`：任务产生的交付物附件。

SQLite 的 `task_attachments` 只记录相对路径和 SHA-256，不嵌入 Base64。敏感图片必须先确认保存权限；需要脱敏时，保留策略由人工决定。
