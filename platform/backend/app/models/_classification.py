"""每一列属于哪一类 —— 一张必须填满的登记表。

## 这道闸挡的是什么

2026-08 那场拆除，起因是一个会话同时显示「Queued」「转圈」和一个按下去回
409 的停止按钮。往下挖，44 张表里有 18 张是空脚手架、9 组是抄件，而它们全都
败在同一条尺子上：**盘上只许有过去（append-only 事件）和决定（人拍的板）；
关于"现在"的一切现算，答案永不落盘。**

拆干净不难，难的是别再长回来。CRUD 的肌肉记忆会让下一个人（也包括下一次的
我）顺手给新表加一个 `status` 列 —— 那个习惯来自任务是**死数据**的世界
（订单、工单），状态转移只由自己的代码驱动，列就是真相。agent run 是**活
进程**，真相在进程那儿，列只是转述，而转述必然过期。

所以这里不是一份"违规列名单"（名单只枚举已知病灶，新东西默认漏过），而是
一道**登记义务**：`Base.metadata` 里每一列都要在这张表上有归属，漏一个
`test_schema_classification` 就红。它逼每个新列过一遍尺子，而不是逼它避开
某几个词。

## 四类

- `IDENTITY`   身份与外键：这行是谁、挂在谁下面。天然不变。
- `DECISION`   人拍的板：授权、偏好、归档、批准。**人**改它，且改动本身是新事实。
- `PAST`       已经发生的事：append-only，写完不该再改（时间戳、见证、内容哈希）。
- `PROJECTION` 从别处可重建的投影：坏了删掉重算，**任何判据都不许读它**。

不属于这四类的东西 —— 典型的就是"关于现在的落盘判决" —— 没有格子可填。
那正是这张表的用意：它没给那种列留位置。
"""

from __future__ import annotations

IDENTITY = "identity"
DECISION = "decision"
PAST = "past"
PROJECTION = "projection"

#: 到处都有、含义一致的列，集中登记一次，省得 29 张表各抄一遍。
COMMON: dict[str, str] = {
    "id": IDENTITY,
    "tenant_id": IDENTITY,
    "workspace_id": IDENTITY,
    "project_id": IDENTITY,
    "session_id": IDENTITY,
    "run_id": IDENTITY,
    "user_id": IDENTITY,
    "created_at": PAST,
    "created_by_user_id": PAST,
    "updated_at": PROJECTION,  # 「最后被写过」—— 不回答任何问题，别拿它当判据
}

#: 逐表登记。键是列名，值是四类之一。
BY_TABLE: dict[str, dict[str, str]] = {
    "users": {
        "email": IDENTITY, "hashed_password": DECISION, "display_name": DECISION,
        "role": DECISION, "institution_name": DECISION, "is_active": DECISION, "preferences": DECISION,
        "must_change_password": DECISION,
        "personal_context_md": DECISION,
        "institution_id": IDENTITY,
        "group_id": IDENTITY,
        "group_name": DECISION,
    },
    "revoked_access_tokens": {
        # 撤销名单是 append-only 的一行「这一张不算了」：写完谁也不改它，过期
        # 之后整行删掉（`expires_at` 之后签名自己就拦得住）。`expires_at` 抄自
        # token 里那个永不变的 claim —— 它说的是签发那一刻的事，不是「现在」。
        "token_hash": IDENTITY,
        "expires_at": PAST,
    },
    "invitations": {
        # 一张请柬：发出去那一刻就定死了「进来是谁」，没有任何一列描述"现在"。
        # `code_hash` 是它的身份（原文只在发出去那一次出现过）；`role`/机构/组/email
        # 是发的人拍的板；两个时间戳与 `accepted_by_user_id` 是已经发生的事。
        "code_hash": IDENTITY,
        "created_by_user_id": IDENTITY,
        "role": DECISION, "institution_id": DECISION, "institution_name": DECISION,
        "group_id": DECISION, "group_name": DECISION, "email": DECISION,
        "expires_at": DECISION,
        "accepted_at": PAST, "accepted_by_user_id": PAST,
    },
    "projects": {
        "name": DECISION, "description": DECISION, "research_domain": DECISION,
        "entry_type": DECISION, "status": DECISION, "owner_id": IDENTITY,
        "visibility": DECISION,
    },
    "project_configs": {
        "operation_mode": DECISION, "reporting_level": DECISION,
        "preferred_model": DECISION, "tool_whitelist": DECISION,
        "harness_overrides": DECISION, "reflection_mode": DECISION,
        "research_intent": DECISION, "notification_channels": DECISION,
        "autonomous_authorized_risk_classes": DECISION,
        "max_concurrent_branches": DECISION,
        "cycle_soft_limit": DECISION,
        "cycle_hard_limit": DECISION,
    },
    "project_memberships": {
        "role": DECISION, "removed_at": DECISION, "updated_by_user_id": PAST,
    },
    "project_resources": { "name": DECISION, "config": DECISION,
        "secret_ref": DECISION, "is_enabled": DECISION, "disabled_at": DECISION, "description": DECISION,
        "resource_type": DECISION,
        "provider": DECISION,
        "endpoint": DECISION,
        "workspace_binding": DECISION,
        "updated_by_user_id": PAST,
    },
    "user_research_settings": {
        "version": PROJECTION,
        "response_language": DECISION,
        "citation_style": DECISION,
        "evidence_standard": DECISION,
        "instructions": DECISION,
    },
    "model_backend_configs": {
        "provider": DECISION, "display_name": DECISION, "base_url": DECISION,
        "model": DECISION, "is_enabled": DECISION,
        "scope_kind": IDENTITY, "scope_id": IDENTITY, "roles": DECISION,
        "default_for_roles": DECISION,
        # 探针结果是**观测证据**：带时间戳、三态（None = 还不知道）。
        # 状态由 backend_status() 现算 —— 这张表是全库的范本。
        "last_probe_at": PAST, "last_probe_ok": PAST, "last_probe_detail": PAST, "last_vision_ok": PAST, "last_vision_detail": PAST,
        # 这一行替哪个组织、服务器上哪一条记着（组织提供的模型）—— 挂在谁下面，身份。
        "provided_by_connection": IDENTITY, "provided_backend_id": IDENTITY,
        "credential_source": DECISION,
        "encrypted_api_key": DECISION,
        "context_window_tokens": DECISION,
    },
    "user_model_backend_preferences": {"role": IDENTITY, "backend_id": DECISION},
    "sessions": {
        "title": DECISION, "summary": DECISION, "initiating_user_id": IDENTITY,
        "lifecycle_status": DECISION, "archived_at": DECISION,
        "recovered_from_session_id": IDENTITY, "recovery_source_run_id": IDENTITY,
        "git_branch": IDENTITY,
        "git_base_commit_sha": IDENTITY, "git_head_commit_sha": IDENTITY,
        "git_worktree_path": PAST, "policy_snapshot_id": IDENTITY,
        "research_settings_snapshot_id": IDENTITY,
        "research_settings_snapshot": PAST, "model_backend_id": DECISION,
        "platform_context_snapshot": PAST,
        "knowledge_read_watermark": PROJECTION,
        # 消息与事件共用的会话级发号器。用户消息不进 harness 事件流，所以这条
        # 统一时间线是**平台自有的身份**，不是谁的抄件。
        "next_sequence": IDENTITY,
    },
    "session_messages": {
        "sequence": IDENTITY, "actor_user_id": IDENTITY, "role": PAST,
        "content": PAST, "command_id": IDENTITY, "offer_id": IDENTITY,
    },
    "execution_events": {
        "sequence": IDENTITY, "kind": PAST, "payload": PAST, "occurred_at": PAST,
        "origin": PAST, "visibility": PAST, "parent_run_id": IDENTITY,
        "attempt_no": IDENTITY, "file_identity": IDENTITY, "byte_offset": IDENTITY,
        "raw_line_hash": IDENTITY, "adapter_version": PAST,
        "schema_version": PAST,
        "source": PAST,
        "source_identity": IDENTITY,
        "ingested_at": PAST,
    },
    "runs": {
        "parent_run_id": IDENTITY, "node_type": PAST,
        # ⚠️ status 是 13 值枚举的原始投影。判据一律走 execution_view
        # （现算三态），**不许**读这一列。
        "status": PROJECTION,
        "retry_count": PROJECTION,
        "prompt_tokens": PROJECTION, "completion_tokens": PROJECTION,
        "total_tokens": PROJECTION, "cost": PROJECTION, "cost_currency": PROJECTION,
        "usage_coverage": PROJECTION,
        "summary": PAST, "delivery_block": PAST,
        "started_at": PAST, "ended_at": PAST,
    },
    "run_attempts": {
        "attempt_no": IDENTITY,
        # ⚠️ 本战役唯一**留下**的 status 列，理由如下（查清了，不是没敢动）：
        #
        # 它记的是「这一趟派发走到哪一步」——LEASED（派出去了还没开跑）/
        # RUNNING（开跑了）/ 已结束（五种收场）。前两者的区分是真事实且**平台
        # 独有**（harness 侧没有 attempt 这个概念），live-pause 和解那条路
        # （local_execution:378/:411）靠它分辨"这次 attempt 到底起没起来"，
        # `ended_at` 对 LEASED 和 RUNNING 都是 None，答不了。
        #
        # 可证等价的那一半已经删了：sandbox reaper 里那份五值终态名单换成了
        # `ended_at is not None`（见 harness_sessions 的注释）。剩下的是记录，
        # 不是判决 —— 所以归 PAST，不是 PROJECTION。
        "status": PAST,
        # 租约与心跳是**观测**：「T 时刻见过它在动」，永远为真。
        # 判决（还活着吗）由 run_liveness 读时现算 —— 这是不变量的正确形态。
        "worker_id": PAST, "lease_until": PAST, "heartbeat_at": PAST,
        "sandbox_manifest": DECISION, "sandbox_manifest_hash": DECISION,
        "exit_reason": PAST, "started_at": PAST, "ended_at": PAST,
    },
    "commands": {
        "actor_user_id": IDENTITY, "kind": DECISION, "payload": DECISION,
        "idempotency_key": IDENTITY, "result": PAST, "error": PAST,
    },
    "decisions": { "subtype": PAST, "prompt": PAST,
        "context": PAST, "choices": PAST, "recommended_choice_id": PAST,
        "authority_type": DECISION, "authority_subjects": DECISION,
        "required_approval_count": DECISION, "action_set_version": PAST,
        "policy_snapshot_id": IDENTITY, "expires_at": DECISION,
        "accepted_responses": PAST, "status": PROJECTION,
        "selected_choice_id": PROJECTION, "resolved_at": PROJECTION,
        "attempt_no": IDENTITY,
    },
    "artifacts": {
        "name": PROJECTION, "type": PROJECTION, "description": PROJECTION,
        "mime_type": PROJECTION, "current_version": PROJECTION,
        "content_hash": PROJECTION, "extra_data": PROJECTION,
    },
    "artifact_versions": {
        "artifact_id": IDENTITY, "version": PROJECTION, "resource_key": IDENTITY,
        "change_set_id": IDENTITY, "lifecycle_status": PROJECTION,
        "size_bytes": PROJECTION, "checksum": IDENTITY,
        # 字节在 git 里；这两列是那份权威的指针。
        "git_commit_sha": IDENTITY, "repository_path": IDENTITY,
    },
    "feed_sources": {
        "kind": DECISION, "name": DECISION, "config": DECISION,
        "domains": DECISION, "poll_interval_seconds": DECISION,
        "max_consecutive_failures": DECISION, "is_active": DECISION,
        "last_polled_at": PAST, "last_success_at": PAST,
        "last_item_count": PAST, "last_error": PAST,
        "consecutive_failures": PROJECTION,
    },
    "feed_items": {
        "source_id": IDENTITY, "canonical_key": IDENTITY, "kind": PAST,
        "title": PAST, "url": PAST, "summary": PAST, "image_url": PAST,
        "authors": PAST, "venue": PAST, "published_at": PAST,
        "author_user_id": IDENTITY, "organization_id": IDENTITY,
        "visibility": DECISION, "extra": PAST,
        "domains": PAST,
    },
    "feed_engagements": {"item_id": IDENTITY, "action": PAST},
    "feed_daily_picks": {"pick_date": IDENTITY, "picks": PROJECTION},
}
