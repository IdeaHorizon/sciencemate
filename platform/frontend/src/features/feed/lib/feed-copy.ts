// 相对路径 + `.ts`：`npm test` 跑的是 node --experimental-strip-types，
// 它不认 `@/` 别名、也不补扩展名（本仓库既有写法，见 chat/lib/*.ts）。
import { say, type Language, type Phrase } from "../../../shared/i18n/language.ts";

/**
 * 资讯流里**由前端产生**的每一句话。
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

  // —— 出处与时间 ——
  "attribution.shared_by": { zh: "由 {name} 分享", en: "Shared by {name}" },
  "time.just_now": { zh: "刚刚", en: "just now" },
  "time.minutes": { zh: "{n} 分钟前", en: "{n} min ago" },
  "time.hours": { zh: "{n} 小时前", en: "{n} h ago" },
  "time.days": { zh: "{n} 天前", en: "{n} d ago" },
  "deadline.passed": { zh: "已截止", en: "Closed" },
  "deadline.today": { zh: "今天截止", en: "Closes today" },
  "deadline.tomorrow": { zh: "明天截止", en: "Closes tomorrow" },
  "deadline.days_left": { zh: "还有 {n} 天", en: "{n} days left" },

  // —— 首页顶部那句话 ——
  "today.title": { zh: "今日精选", en: "Today\u2019s picks" },
  "today.title.personalized": { zh: "为你精选", en: "Picked for you" },
  "today.subtitle.filling": {
    zh: "内容池还在填充，稍后回来看看",
    en: "The pool is still filling up \u2014 check back shortly",
  },
  "today.subtitle.no_signal": {
    zh: "你还没有在跑的课题，也没订阅方向 —— 先按最新和最受关注给你看",
    en: "No running projects and no chosen fields yet \u2014 showing what is newest and most read",
  },
  "today.subtitle.pick_fields": {
    zh: "选几个关注方向，之后这里会跟着你在做的研究走",
    en: "Pick a few fields and this will follow the research you are actually doing",
  },
  "today.subtitle.personalized": {
    zh: "按你在跑的课题和关注方向挑的",
    en: "Chosen from your running projects and stated interests",
  },

  // —— 卡片动作 ——
  "action.save": { zh: "收藏", en: "Save" },
  "action.saved": { zh: "已收藏", en: "Saved" },
  "action.unsave": { zh: "取消收藏", en: "Remove from saved" },
  "action.open_source": { zh: "打开原文", en: "Open source" },
  "action.dismiss": { zh: "不感兴趣", en: "Not interested" },
  "action.dismiss.hint": { zh: "不感兴趣，别再推给我", en: "Not interested \u2014 stop showing me this" },
  "action.close": { zh: "关闭", en: "Close" },
  "action.cancel": { zh: "取消", en: "Cancel" },

  // —— 接入课题 ——
  "handoff.action": { zh: "接入课题", en: "Take to a project" },
  "handoff.action.hint": { zh: "带着这条开一个研究会话", en: "Open a research session carrying this item" },
  "handoff.loading_projects": { zh: "读取课题…", en: "Loading projects\u2026" },
  "handoff.no_projects": {
    zh: "还没有课题 —— 先建一个，再把资讯接进去",
    en: "No projects yet \u2014 create one first, then bring items into it",
  },
  "handoff.which_project": { zh: "接入哪个课题？", en: "Into which project?" },
  "handoff.failed": { zh: "接入课题失败", en: "Could not take this into a project" },
  "handoff.draft.intro": {
    zh: "我在资讯流里看到这条，想放进这个课题看看：",
    en: "I came across this in the feed and want to bring it into this project:",
  },
  "handoff.draft.title": { zh: "标题：{value}", en: "Title: {value}" },
  "handoff.draft.url": { zh: "出处：{value}", en: "Source: {value}" },
  "handoff.draft.venue": { zh: "来源：{value}", en: "Venue: {value}" },
  "handoff.draft.authors": { zh: "作者：{value}", en: "Authors: {value}" },
  "handoff.draft.summary": { zh: "摘要：{value}", en: "Abstract: {value}" },

  // —— 分享链接 ——
  "share.url.placeholder": {
    zh: "贴一个链接：论文、预印本、工具发布、一篇写得好的文章…",
    en: "Paste a link: a paper, preprint, tool release, or a well-written post\u2026",
  },
  "share.url.label": { zh: "要分享的链接", en: "Link to share" },
  "share.comment.placeholder": { zh: "一句话说说为什么值得看（可选）", en: "One line on why it is worth reading (optional)" },
  "share.comment.label": { zh: "分享附言", en: "Note on the share" },
  "share.visibility": { zh: "可见范围", en: "Visible to" },
  "share.visibility.org": { zh: "本组织", en: "My organization" },
  "share.visibility.platform": { zh: "全平台", en: "Everyone" },
  "share.submit": { zh: "分享", en: "Share" },
  "share.done": { zh: "已分享：{title}", en: "Shared: {title}" },
  "share.failed": { zh: "分享失败", en: "Could not share this link" },

  // —— 兴趣选择 ——
  "picker.search.placeholder": {
    zh: "搜索方向，例如 materials、machine learning、statistical",
    en: "Search fields \u2014 materials, machine learning, statistical\u2026",
  },
  "picker.search.label": { zh: "搜索研究方向", en: "Search research fields" },
  "picker.vocabulary_down": {
    zh: "方向词表暂时读不到（平台侧故障）。这不影响你继续看资讯流 —— 只是现在改不了关注方向。",
    en: "The field vocabulary cannot be read right now (a platform fault). The feed still works \u2014 you just cannot change your interests at the moment.",
  },
  "picker.inferred.header": { zh: "从你的课题推断的方向", en: "Inferred from your projects" },
  "picker.inferred.drop": { zh: "不要 {label}", en: "Drop {label}" },
  "picker.inferred.drop_hint": { zh: "推错了，别再推给我", en: "Wrong guess \u2014 stop suggesting it" },
  "picker.suggested_hint": {
    zh: "下面这几个是按你已有课题的方向猜的，改掉不合适的就行。",
    en: "These were guessed from your existing projects \u2014 just change what does not fit.",
  },
  "picker.whole_archive": { zh: "订阅整个 {label}", en: "Follow all of {label}" },
  "picker.archive_hint": { zh: "整个大类", en: "Whole archive" },
  "picker.suggested_tag": { zh: "建议", en: "suggested" },
  "picker.count": { zh: "已选 {n} 个方向", en: "{n} fields selected" },
  "picker.clear": { zh: "清空", en: "Clear" },
  "picker.clear_hint": { zh: "清掉所有已选方向", en: "Clear every selected field" },
  "picker.expand": { zh: "展开二级学科", en: "Show subfields" },
  "picker.collapse": { zh: "收起二级学科", en: "Hide subfields" },
  "picker.group_selected": { zh: "{n} 已选", en: "{n} selected" },
  "picker.skip": { zh: "先跳过", en: "Skip for now" },
  "picker.save": { zh: "保存", en: "Save" },
  "picker.save_failed": { zh: "保存关注方向失败", en: "Could not save your interests" },

  // —— 自动挖掘开关 ——
  "curation.title": { zh: "让平台自动替我挖掘", en: "Let the platform curate for me" },
  "curation.subtitle": {
    zh: "读你正在跑的课题，推断你该关注的方向、定向去找相关的新工作，并把一周的动向缩成简报。用的是你自己的模型额度。",
    en: "Reads your running projects, infers which fields you should follow, searches along them, and condenses the week into a digest. It spends your own model quota.",
  },
  "curation.switch_label": { zh: "自动挖掘", en: "Automatic curation" },
  "curation.toggle_failed": { zh: "改不了这个开关", en: "Could not change this switch" },
  "curation.model_line": { zh: "挖掘模型：", en: "Curation model: " },
  "curation.model_missing": { zh: "未配置", en: "not configured" },
  "curation.model_change": { zh: "换一个", en: "Change" },
  "curation.model_configure": { zh: "去配置", en: "Configure" },
  "curation.queries_line": { zh: "正在用这些词替你找：", en: "Searching on your behalf with:" },
  "curation.unavailable": {
    zh: "平台还没有配资讯挖掘模型 —— 这项能力当前不可用。到 设置 → 模型 给一个连接勾上「资讯挖掘模型」。",
    en: "No feed-curation model is configured yet, so this is unavailable. Give a connection the \u201cFeed curation model\u201d role under Settings \u2192 Models.",
  },
  "curation.off": {
    zh: "当前关闭。打开后会用你自己的模型额度，每天最多挖一次。",
    en: "Currently off. Turning it on spends your own model quota, at most once a day.",
  },
  "curation.failed": { zh: "开着，但上次没能完成：{error}", en: "On, but the last run did not finish: {error}" },
  "curation.inferred": {
    zh: "已从你的课题推断出 {count} 个方向：{fields}",
    en: "Inferred {count} fields from your projects: {fields}",
  },
  "curation.first_run": {
    zh: "已打开，正在第一次挖掘 —— 稍后刷新看看。",
    en: "On, and running for the first time \u2014 refresh in a moment.",
  },
  "curation.nothing_inferred": {
    zh: "跑过了，但没从你的课题里看出明确的方向 —— 把课题描述写具体些会好很多。",
    en: "It ran but could not read a clear direction from your projects \u2014 a more specific project description helps a lot.",
  },

  // —— 页面骨架 ——
  "pane.today": { zh: "推荐", en: "Recommendations" },
  "pane.subscriptions": { zh: "订阅", en: "Following" },
  "pane.saved": { zh: "收藏", en: "Saved" },
  "pane.interests": { zh: "关注方向", en: "Interests" },
  "pane.search": { zh: "学术搜索", en: "Academic search" },
  "view.list": { zh: "列表", en: "List" },
  "view.list.hint": { zh: "一行一条：一屏能扫过更多标题", en: "One per row: scan more titles at once" },
  "view.grid": { zh: "图卡", en: "Cards" },
  "view.grid.hint": { zh: "一行三块：能显示配图和更长的摘要", en: "Three per row: images and longer summaries" },
  // —— 分页 ——
  "pagination.label": { zh: "翻页", en: "Pagination" },
  "pagination.prev": { zh: "上一页", en: "Previous page" },
  "pagination.next": { zh: "下一页", en: "Next page" },
  "pagination.page": { zh: "{current} / {total}", en: "{current} / {total}" },
  "home.tabs.label": { zh: "资讯流视图", en: "Feed views" },
  "home.layout.label": { zh: "排布方式", en: "Layout" },
  "home.error.title": { zh: "资讯流暂时打不开", en: "The feed cannot be opened right now" },
  "home.error.hint": { zh: "稍后再试", en: "Try again shortly" },
  "home.onboarding.title": { zh: "先选几个你关心的方向", en: "Start by picking a few fields you follow" },
  "home.onboarding.subtitle": {
    zh: "这里会按你选的方向、以及你正在跑的课题，每天给你挑几条值得看的。随时可以改。",
    en: "Each day this picks a few things worth reading, based on the fields you choose and the projects you are running. Change it any time.",
  },
  "home.interests.title": { zh: "关注方向", en: "Interests" },
  "home.interests.subtitle": {
    zh: "改完立刻生效 —— 今天的选摘会按新方向重算。",
    en: "Changes take effect immediately \u2014 today\u2019s picks are recomputed.",
  },
  "home.saved.title": { zh: "收藏", en: "Saved" },
  "home.saved.empty": { zh: "还没有收藏", en: "Nothing saved yet" },
  "home.saved.empty_hint": {
    zh: "在卡片上点「收藏」，之后在这里找得到。",
    en: "Save from any card and you will find it here.",
  },
  "home.empty.title": { zh: "今天还没有可挑的内容", en: "Nothing to pick from today" },
  "home.empty.hint": {
    zh: "采集还在进行，稍后回来看看。",
    en: "Collection is still running \u2014 check back shortly.",
  },
  "home.share.title": { zh: "分享一条", en: "Share something" },
  "home.share.subtitle": {
    zh: "看到值得同事一起看的东西，贴个链接就行。",
    en: "Seen something your colleagues should read? Paste the link.",
  },
  "home.not_personalized": {
    zh: "这些还不是为你挑的 —— 选几个关注方向，或者开一个 Project，这里就会跟着你在做的事变。",
    en: "These are not picked for you yet \u2014 choose a few fields or start a Project and this will follow what you are actually doing.",
  },
  "home.choose_interests": { zh: "选关注方向", en: "Choose fields" },
  "subscriptions.title": { zh: "订阅", en: "Following" },
  "subscriptions.subtitle": { zh: "关注方向、具体期刊和学者，只看他们相关的新成果。", en: "Follow fields, journals and scholars, and keep up with their latest work." },
  "subscriptions.domains": { zh: "学科", en: "Fields" },
  "subscriptions.journals": { zh: "期刊", en: "Journals" },
  "subscriptions.scholars": { zh: "学者", en: "Scholars" },
  "subscriptions.search_journals": { zh: "搜索 SCI 期刊名称或 ISSN", en: "Search SCI journals by name or ISSN" },
  "subscriptions.search_scholars": { zh: "搜索 KB 或本地文献库中的学者", en: "Search scholars in the KB or local literature catalog" },
  "subscriptions.follow": { zh: "关注", en: "Follow" },
  "subscriptions.following": { zh: "已关注", en: "Following" },
  "subscriptions.feed": { zh: "订阅动态", en: "Following feed" },
  "subscriptions.empty": { zh: "还没有订阅动态", en: "No updates from your subscriptions yet" },
  "subscriptions.done": { zh: "完成，进入推荐", en: "Done, show recommendations" },
  "subscriptions.empty_hint": { zh: "先搜索并关注期刊或学者；已有文献会立即显示，新成果会随采集进入。", en: "Follow journals or scholars first. Existing papers appear immediately and new work arrives with collection." },
} satisfies Record<string, Phrase>;

export type FeedCopyKey = keyof typeof FEED_COPY;

/**
 * 取一句资讯流文案。
 *
 * `lang` 是**参数**，不是这个函数自己去读的全局状态 —— 同 `relativeTime`
 * 收 `now` 的理由：一个自己去够上下文的纯函数就不再是纯函数，也就测不动了。
 */
export function fc(
  key: FeedCopyKey,
  lang: Language,
  fields?: Record<string, string | number>,
): string {
  return say(FEED_COPY[key], lang, fields);
}
