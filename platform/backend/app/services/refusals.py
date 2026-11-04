"""几句到处都要说的拒绝 —— 找不到、没有权限。两种语言并排写在这一处。

「找不到这个项目」从十几个端点和帮手里抛出来。每处各写一遍两种语言，改一处忘一处时
不报错；所以句子只在这里写一次，调用点用 `say(…, lang)` 按这一屏说。
"""

from __future__ import annotations

PROJECT_NOT_FOUND = {"zh": "找不到这个项目", "en": "Project not found"}
NOT_AUTHORIZED_FOR_PROJECT = {"zh": "你没有权限查看这个项目", "en": "Not authorized to view this project"}
SESSION_NOT_FOUND = {"zh": "找不到这个会话", "en": "Session not found"}
ARTIFACT_NOT_FOUND = {"zh": "找不到这个成果", "en": "Artifact not found"}
FEED_ITEM_NOT_FOUND = {"zh": "找不到这条资讯", "en": "Feed item not found"}
PROJECT_MEMBER_NOT_FOUND = {"zh": "找不到这位项目成员", "en": "Project member not found"}

#: 程序自己拼错了请求才会撞上的 422（界面上没有能走到它的操作）。中文只说一句「请求无效」，
#: 英文保留原来那句技术说明。
def invalid_request(english: str) -> dict[str, str]:
    return {"zh": "请求无效", "en": english}


_MISSING: dict[str, dict[str, str]] = {
    "drive_session": {"zh": "你没有权限在这个会话中操作",
                      "en": "You do not have permission to work in this Session"},
    "manage_settings": {"zh": "你没有权限修改项目设置",
                        "en": "You do not have permission to change the project settings"},
    "publish_changes": {"zh": "你没有权限把改动发布到项目",
                        "en": "You do not have permission to publish changes to the project"},
}


def missing_capability(capability: str) -> dict[str, str]:
    """缺某项项目能力时那句话。没有专门说法的能力，说一句通用的。"""
    return _MISSING.get(capability) or {
        "zh": "你没有权限执行这个操作",
        "en": f"You do not have permission to do this (missing project capability: {capability})",
    }
