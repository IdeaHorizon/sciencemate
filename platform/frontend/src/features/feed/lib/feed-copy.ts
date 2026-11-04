// 相对路径 + `.ts`：`npm test` 跑的是 node --experimental-strip-types，
// 它不认 `@/` 别名、也不补扩展名（本仓库既有写法，见 chat/lib/*.ts）。
import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";

/**
 * 科研资讯里**由前端产生**的每一句话。
 *
 * ## 为什么集中在一张表里，而不是各组件各写各的
 *
 * 分散着写的话，"这一片翻完了吗"这个问题没有答案 —— 只能靠人一个文件一个
 * 文件地看，而下一个人加的那句默认漏过。集中之后判据是机械的：扫这张表，
 * 每个 key 两种语言都得有（见 feed-presentation.test.ts）。
 *
 * ## 为什么后端也有一张
 *
 * 分区标题、"这台部署关掉了外部抓取"那类句子是后端**算**出来的（有没有某个
 * 分区取决于匹配结果），前端按 key 猜不出来。所以后端产生的字在后端翻
 * （`app/services/feed/copy.py`），前端产生的字在这里翻。每一句只有一处
 * 定义，两张表不是彼此的抄件。
 */
export const FEED_COPY = {
  // —— 卡片上的类型徽章 ——
  "kind.paper": { zh: "论文", en: "Paper" },
  "kind.release": { zh: "发布", en: "Release" },
  "kind.deadline": { zh: "截稿", en: "Deadline" },
  "kind.news": { zh: "资讯", en: "News" },
  "kind.digest": { zh: "周报", en: "Digest" },
  "kind.post": { zh: "分享", en: "Shared" },

  // —— 卡片上的强调角标 ——
  "badge.today_pick": { zh: "今日必读", en: "Today's pick" },
  "badge.viewed": { zh: "已看过", en: "Viewed" },

  // —— 出处与截稿 ——
  "attribution.shared_by": { zh: "由 {name} 分享", en: "Shared by {name}" },
  "deadline.passed": { zh: "已截止", en: "Closed" },
  "deadline.today": { zh: "今天截止", en: "Closes today" },
  "deadline.tomorrow": { zh: "明天截止", en: "Closes tomorrow" },
  "deadline.days_left": { zh: "{n} 天后截止", en: "Closes in {n} days" },

  // —— 首页顶部那句话 ——
  "today.title": { zh: "今日精选", en: "Today\u2019s picks" },
  "today.title.personalized": { zh: "为你精选", en: "Picked for you" },
  "today.subtitle.filling": {
    zh: "精选内容正在准备中，请稍后再来查看",
    en: "Today\u2019s picks are still being prepared. Check back later.",
  },
  "today.subtitle.no_signal": {
    zh: "显示最新和最受关注的内容",
    en: "Showing the newest and most-read items",
  },
  "today.subtitle.pick_fields": {
    zh: "选择关注的学科后，这里会按你的研究推荐内容",
    en: "Choose a few fields to get recommendations based on your research",
  },
  "today.subtitle.personalized": {
    zh: "根据你进行中的项目和订阅推荐",
    en: "Based on your active projects and subscriptions",
  },

  // —— 卡片动作 ——
  "action.save": { zh: "收藏", en: "Save" },
  "action.saved": { zh: "已收藏", en: "Saved" },
  "action.unsave": { zh: "取消收藏", en: "Remove from saved" },
  "action.open_source": { zh: "打开原文", en: "Open source" },
  "action.dismiss": { zh: "不感兴趣", en: "Not interested" },
  "action.dismiss.hint": { zh: "不再推荐这条内容", en: "Stop recommending this item" },
  "action.close": { zh: "关闭", en: "Close" },
  "action.cancel": { zh: "取消", en: "Cancel" },

  // —— 在项目中讨论 ——
  "handoff.action": { zh: "在项目中讨论", en: "Discuss in a project" },
  "handoff.action.hint": { zh: "在所选项目中新建会话，并附上这条内容", en: "Start a new session in a project with this item attached" },
  "handoff.loading_projects": { zh: "正在加载项目…", en: "Loading projects\u2026" },
  "handoff.no_projects": { zh: "还没有项目，请先新建一个。", en: "No projects yet. Create one first." },
  "handoff.which_project": { zh: "选择项目：", en: "Choose a project:" },
  "handoff.failed": { zh: "新建会话失败", en: "Could not start a session" },
  "handoff.draft.intro": {
    zh: "我在科研资讯里看到这篇内容，想结合这个项目看看：",
    en: "I came across this in the feed and want to bring it into this project:",
  },
  "handoff.draft.title": { zh: "标题：{value}", en: "Title: {value}" },
  "handoff.draft.url": { zh: "链接：{value}", en: "Link: {value}" },
  "handoff.draft.venue": { zh: "发表于：{value}", en: "Venue: {value}" },
  "handoff.draft.authors": { zh: "作者：{value}", en: "Authors: {value}" },
  "handoff.draft.summary": { zh: "摘要：{value}", en: "Abstract: {value}" },

  // —— 分享链接 ——
  "share.url.placeholder": {
    zh: "粘贴链接，例如论文、预印本、工具发布或文章",
    en: "Paste a link to a paper, preprint, tool release or article",
  },
  "share.url.label": { zh: "要分享的链接", en: "Link to share" },
  "share.comment.placeholder": { zh: "推荐理由（可选）", en: "Why it's worth reading (optional)" },
  "share.comment.label": { zh: "推荐理由", en: "Why it's worth reading" },
  "share.visibility": { zh: "可见范围", en: "Visible to" },
  "share.visibility.org": { zh: "本组织", en: "My organization" },
  "share.visibility.platform": { zh: "全平台", en: "Everyone" },
  "share.submit": { zh: "分享", en: "Share" },
  "share.done": { zh: "已分享：{title}", en: "Shared: {title}" },

  // —— 学科选择 ——
  "picker.search.placeholder": {
    zh: "搜索学科，例如：材料、统计、计算机",
    en: "Search fields, e.g. materials, statistics, computer",
  },
  "picker.search.label": { zh: "搜索学科", en: "Search fields" },
  "picker.vocabulary_down": {
    zh: "无法加载学科列表，请稍后重试。",
    en: "Could not load the list of fields. Please try again later.",
  },
  "picker.count": { zh: "已选 {n} 个学科", en: "{n} fields selected" },
  "picker.clear": { zh: "清空", en: "Clear" },
  "picker.skip": { zh: "先跳过", en: "Skip for now" },
  "picker.save": { zh: "保存", en: "Save" },

  // —— 根据项目自动推荐 ——
  "curation.title": { zh: "根据项目自动推荐", en: "Recommend from my projects" },
  "curation.subtitle": {
    zh: "分析你进行中的项目，推断相关学科和检索词，查找新发表的研究并生成周报。会消耗你的模型额度。",
    en: "Analyzes your active projects to infer fields and search terms, finds new work and writes a weekly digest. Uses your own model quota.",
  },
  "curation.switch_label": { zh: "根据项目自动推荐", en: "Recommend from my projects" },
  "curation.toggle_failed": { zh: "更改设置失败", en: "Could not change this setting" },
  "curation.model_line": { zh: "使用的模型：", en: "Model: " },
  "curation.model_missing": { zh: "未配置", en: "not configured" },
  "curation.model_change": { zh: "更改", en: "Change" },
  "curation.model_configure": { zh: "配置", en: "Configure" },
  "curation.queries_line": { zh: "检索词：", en: "Search terms:" },
  "curation.unavailable": {
    zh: "需要先添加模型连接，请前往 设置 → 模型。",
    en: "Add a model connection first under Settings \u2192 Models.",
  },
  "curation.off": { zh: "已关闭", en: "Off" },
  "curation.failed": { zh: "上次运行失败：{error}", en: "Last run failed: {error}" },
  "curation.inferred": {
    zh: "从你的项目中识别出 {count} 个学科：{fields}",
    en: "Inferred {count} fields from your projects: {fields}",
  },
  "curation.first_run": {
    zh: "已开启，正在分析你的项目，请稍后刷新。",
    en: "On. Analyzing your projects; refresh in a moment.",
  },
  "curation.nothing_inferred": {
    zh: "没有从进行中的项目中识别出明确的学科。可以新建项目，或把项目描述写得更具体。",
    en: "No clear fields were found in your active projects. Create a project or make its description more specific.",
  },

  // —— 页面骨架 ——
  "pane.today": { zh: "推荐", en: "Recommendations" },
  "pane.subscriptions": { zh: "管理订阅", en: "Manage subscriptions" },
  "pane.saved": { zh: "收藏", en: "Saved" },
  "pane.search": { zh: "搜索", en: "Search" },
  "view.list": { zh: "列表", en: "List" },
  "view.list.hint": { zh: "每行一条，不显示配图", en: "One per row, without images" },
  "view.grid": { zh: "卡片", en: "Cards" },
  "view.grid.hint": { zh: "多列卡片，显示配图和较长的摘要", en: "Multi-column cards with images and longer summaries" },
  "home.tabs.label": { zh: "科研资讯", en: "Feed views" },
  "home.layout.label": { zh: "布局", en: "Layout" },
  "home.refresh": { zh: "刷新推荐", en: "Refresh recommendations" },
  "home.refresh.hint": { zh: "重新计算推荐并从头加载", en: "Recalculate recommendations and reload from the top" },
  "home.error.title": { zh: "无法加载科研资讯", en: "Could not load the research feed" },
  "home.error.hint": { zh: "请稍后重试。", en: "Please try again later." },
  "home.onboarding.title": { zh: "选择你关注的学科", en: "Choose the fields you follow" },
  "home.onboarding.subscriptions_title": { zh: "订阅期刊和学者（可选）", en: "Follow journals and scholars (optional)" },
  "home.onboarding.subtitle": {
    zh: "科研资讯会根据你关注的学科和进行中的项目，每天推荐值得读的内容。之后可以随时修改。",
    en: "Each day this picks a few things worth reading, based on the fields you choose and the projects you are running. Change it any time.",
  },
  "home.saved.title": { zh: "收藏", en: "Saved" },
  "home.saved.empty": { zh: "还没有收藏", en: "Nothing saved yet" },
  "home.saved.empty_hint": {
    zh: "在内容卡片上点击「收藏」，即可在这里查看。",
    en: "Save from any card and you will find it here.",
  },
  "home.empty.title": { zh: "还没有推荐内容", en: "No recommendations yet" },
  "home.empty.refreshing_title": { zh: "正在准备推荐内容…", en: "Preparing recommendations\u2026" },
  "home.empty.hint": {
    zh: "内容仍在收集中，请稍后再来查看。",
    en: "Content is still being collected. Check back later.",
  },
  "home.share.title": { zh: "分享链接", en: "Share a link" },
  "home.not_personalized": {
    zh: "订阅学科、期刊或学者，或者新建项目，即可获得个性化推荐。",
    en: "Follow fields, journals or scholars, or create a project, to get personalized recommendations.",
  },
  "home.choose_interests": { zh: "设置订阅", en: "Set up subscriptions" },
  "subscriptions.title": { zh: "订阅", en: "Following" },
  "subscriptions.subtitle": { zh: "订阅学科、期刊和学者，获取它们的最新成果。", en: "Follow fields, journals and scholars, and keep up with their latest work." },
  "subscriptions.domains": { zh: "学科", en: "Fields" },
  "subscriptions.journals": { zh: "期刊", en: "Journals" },
  "subscriptions.scholars": { zh: "学者", en: "Scholars" },
  "subscriptions.search_journals": { zh: "搜索 SCI 期刊名称或 ISSN", en: "Search SCI journals by name or ISSN" },
  "subscriptions.search_scholars": { zh: "按姓名搜索学者", en: "Search scholars by name" },
  "subscriptions.follow": { zh: "关注", en: "Follow" },
  "subscriptions.following": { zh: "已关注", en: "Following" },
  "subscriptions.feed": { zh: "订阅动态", en: "Following feed" },
  "subscriptions.empty": { zh: "还没有订阅动态", en: "No updates from your subscriptions yet" },
  "subscriptions.done": { zh: "完成", en: "Done" },
  "subscriptions.empty_hint": { zh: "关注期刊或学者后，他们的新论文会显示在这里。", en: "Follow journals or scholars and their new papers will appear here." },
} satisfies Record<string, Phrase>;

export type FeedCopyKey = keyof typeof FEED_COPY;

/**
 * 取一句科研资讯文案。
 *
 * `lang` 是**参数**，不是这个函数自己去读的全局状态。
 * 收 `now` 的理由：一个自己去够上下文的纯函数就不再是纯函数，也就测不动了。
 */
export function fc(
  key: FeedCopyKey,
  lang: Language,
  fields?: Record<string, string | number>,
): string {
  return say(FEED_COPY[key], lang, fields);
}
