import { api } from "@/lib/api";

import type { InstructionFile, SessionInstructions } from "../types";

/**
 * 指令是文件：读它、写它。
 *
 * 从前这里是 253 行的一套「起草 / 发布 / 版本 / 快照」客户端。三张表回答的问题
 * 只有一个 —— 这个课题的指令是什么，而答案是一段文本（RFC X2）。
 */
export const instructionsClient = {
  readPersonal: (): Promise<InstructionFile> => api.readInstructionFile("/me/instructions"),
  writePersonal: (content: string): Promise<InstructionFile> =>
    api.writeInstructionFile("/me/instructions", content),
  readProject: (projectId: string): Promise<InstructionFile> =>
    api.readInstructionFile(`/projects/${encodeURIComponent(projectId)}/instructions`),
  writeProject: (projectId: string, content: string): Promise<InstructionFile> =>
    api.writeInstructionFile(`/projects/${encodeURIComponent(projectId)}/instructions`, content),
  /** 这个会话**现在**读到的那几层。项目层的冻结由它自己的 git 分支给。 */
  readSessionInstructions: (projectId: string, sessionId: string): Promise<SessionInstructions> =>
    api.readSessionInstructions(projectId, sessionId),
};
