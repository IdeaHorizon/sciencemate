import type { Artifact, ConceptAnnotation } from "@/lib/api";

export type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
  runId?: string | null;
  /**
   * 这条消息**就是**哪一次呈递（后端 `session_messages.offer_id`）。
   *
   * run 停下来问人时，问句会原样写成一条 assistant 消息 —— 页面上于是有两份：
   * 对话流里这一条，和能点的那张卡片。从前靠文案一字不差去掉后者，而"拿文案
   * 当身份"正是决策卡无限重现那次事故的引擎。有了身份，卡片就画在**这条消息
   * 的位置**上，正文不再画第二遍。
   */
  offerId?: string | null;
  /**
   * 会话级 sequence。消息与执行事件共用**同一个**发生器（后端
   * `sessions.next_sequence`），所以"这段活动发生在哪两条消息之间"是库里的
   * 事实。乐观插入的本地消息还没有号。
   */
  sequence?: number | null;
  conceptAnnotations?: ConceptAnnotation[];
};

export type ChatScope =
  | { kind: "global" }
  | { kind: "project"; projectId: string };

export type ChatActivity = {
  id: string;
  label: string;
  detail?: string;
  status?: string;
  raw: Record<string, unknown>;
};

export type { ChatPause, ChatPauseOption, ChatTerminalState } from "./lib/chat-terminal";

export type ArtifactLinkContext = {
  artifacts?: Artifact[];
  onArtifactClick?: (name: string) => void;
};
