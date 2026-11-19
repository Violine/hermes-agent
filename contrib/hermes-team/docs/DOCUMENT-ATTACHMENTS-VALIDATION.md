# Document attachments

Attachments can be included from the dashboard or added through Telegram before requirements are approved. Uploading a file alone does not create a request or advance an approval gate. New requests preserve the original attachment separately from bounded extracted text. File type, size, total count, duplicate delivery, and owner/request association are validated.

Treat uploaded documents as untrusted input. Text extraction has resource limits, images remain attachments where supported, and attachment contents cannot override workflow rules or an explicit owner decision.

Unit and UI tests cover intake forms, retry-safe attachment association, access control, attachment context generation, and Telegram handler registration. They do not establish that a live bot, PDF parser, or current server installation is configured.
