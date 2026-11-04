import type { Phrase } from "@/shared/i18n";

/**
 * 气泡贴在哪、说什么。
 *
 * `anchor` 是一个选择器，配的是侧栏那几项上的 `data-tour` —— 它取的是 href 的
 * **最后一段**（`/projects/<id>/research` → `research`）。取整条的话，项目里的
 * 地址带着项目 id，就只能贴在某一个项目上；而带斜杠的键还会被 navigation-state
 * 那道闸当成一个不存在的页面地址（它判得没错）。
 *
 * 每一站说的是**这块地方替你做什么**，不是它叫什么 —— 名字侧栏上已经写着了，
 * 再念一遍等于什么都没说。
 */
export type TourStop = { anchor: string; title: Phrase; body: Phrase };

/** 进门那一圈：工作区的三块。 */
export const WORKSPACE_STOPS: TourStop[] = [
  {
    anchor: '[data-tour="feed"]',
    title: { zh: "科研资讯", en: "Feed" },
    body: { zh: "每天根据你订阅的学科和进行中的项目，挑出值得看的内容。", en: "Each day it picks what is worth reading based on the fields you follow and your active projects." },
  },
  {
    anchor: '[data-tour="projects"]',
    title: { zh: "项目", en: "Projects" },
    body: { zh: "每项研究对应一个项目，项目里有会话、证据和成果。从这里新建项目开始研究。", en: "Each line of research is a project, holding its sessions, evidence and outputs. Create one here to get started." },
  },
  {
    anchor: '[data-tour="compute"]',
    title: { zh: "算力", en: "Compute" },
    body: { zh: "查看可供会话使用的 CPU、内存、存储和 GPU，以及正在运行的会话数。", en: "See the CPU, memory, storage and GPUs available to your sessions, and how many sessions are running." },
  },
];

/**
 * 第一次进一个项目时那一圈。
 *
 * 只挑四站，**顺着侧栏从上往下**（次序不对的话气泡会在屏幕上跳上跳下，实测
 * top 12 → 84 → 44 → 124）。连起来正好是一轮研究的形状：在哪干活、做出了什么、
 * 材料放哪、现在推到哪了。项目记忆和项目设置不在这一圈里：它们是用着用着自然会去的地方，
 * 而每多一站，前面几站被读完的可能就少一分。
 */
export const PROJECT_STOPS: TourStop[] = [
  {
    anchor: '[data-tour="research"]',
    title: { zh: "研究", en: "Research" },
    body: { zh: "在这里新建会话，把研究问题交给助手。助手会检索文献、运行计算、撰写文稿，每一步都实时显示，你可以随时补充说明或让它停下。", en: "Start a session here and hand your question to the assistant. It searches the literature, runs computations and writes, showing every step as it goes; you can add instructions or stop it at any time." },
  },
  {
    anchor: '[data-tour="outputs"]',
    title: { zh: "研究成果", en: "Research outputs" },
    body: { zh: "项目的成果都在这里：文稿、图表、数据集等，点一下就能打开。", en: "The project's outputs live here: drafts, figures, datasets and more, each one click away." },
  },
  {
    anchor: '[data-tour="files"]',
    title: { zh: "项目文件", en: "Project files" },
    body: { zh: "你添加的资料和助手生成的文件都在这里。在会话里用「添加文件」上传的资料，助手可以直接使用。", en: "Your files and everything the assistant produces are here. Files you add with Add a file in a session are available to the assistant right away." },
  },
  {
    anchor: '[data-tour="research-state"]',
    title: { zh: "研究进展", en: "Research progress" },
    body: { zh: "这个项目目前的进展：哪些问题已有结论、哪些还待验证，以及下一步计划。", en: "Where this project stands: what has been settled, what is still open, and what comes next." },
  },
];
