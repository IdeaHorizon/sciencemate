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
    title: { zh: "资讯", en: "Feed" },
    body: { zh: "每天按你选的方向和在跑的课题，替你把值得看的挑出来。没配挖掘模型也能用 —— 那时是按方向排序，不替你推断。", en: "Each day it picks what is worth reading from the fields you chose and the work you have running. It works without a curation model too — then it sorts by your fields instead of inferring them." },
  },
  {
    anchor: '[data-tour="projects"]',
    title: { zh: "项目", en: "Projects" },
    body: { zh: "研究在这里发生：一个课题一个项目，里面是会话、证据和产出。想开工就从这儿新建一个。", en: "This is where the research happens: one project per line of work, holding its sessions, evidence and outputs. Start a new one here." },
  },
  {
    anchor: '[data-tour="compute"]',
    title: { zh: "算力", en: "Compute" },
    body: { zh: "会话能用到的机器和调度器都摆在这儿 —— 谁在跑、还剩多少，出了事先看这一页。", en: "The machines and schedulers your sessions can reach: what is running and what is left. When something stalls, look here first." },
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
    body: { zh: "干活的地方。在这儿开一轮会话，把想法交给 agent —— 它去查文献、跑计算、写稿，每一步都摆在你眼前，随时能插话、能停。", en: "Where the work happens. Start a session here and hand the agent a question — it searches the literature, runs computations and writes, with every step in front of you, interruptible at any time." },
  },
  {
    anchor: '[data-tour="outputs"]',
    title: { zh: "研究产出", en: "Research outputs" },
    body: { zh: "做出来的东西在这儿：稿子、图、数据集。每一件都带着它是怎么来的 —— 哪一轮、用了哪些证据。", en: "What the work produced: drafts, figures, datasets. Each carries where it came from — which run, and on what evidence." },
  },
  {
    anchor: '[data-tour="files"]',
    title: { zh: "项目文件", en: "Project files" },
    body: { zh: "你交给它的数据和它产生的文件，都在这一棵树里。拖进来的东西 agent 就能用 —— 不用另外说在哪。", en: "Your data and everything it produces live in one tree. Anything you drop in is available to the agent — you do not have to tell it where things are." },
  },
  {
    anchor: '[data-tour="research-state"]',
    title: { zh: "研究进展", en: "Research progress" },
    body: { zh: "这个课题现在推到哪了：哪些问题有答案了、哪些还悬着、下一步卡在什么上。隔几天回来先看这一页。", en: "Where this line of work stands: what is answered, what is still open, and what the next step is waiting on. Come back after a few days and start here." },
  },
];
