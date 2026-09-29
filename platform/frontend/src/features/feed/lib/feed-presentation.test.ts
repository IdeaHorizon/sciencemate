import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import type { FeedItem } from "@/lib/api";
import {
  attribution,
  authorLine,
  deadlineCountdown,
  hasExternalLink,
  relativeTime,
  todayHeadline,
  truncate,
  visibleDomainTags,
} from "./feed-presentation.ts";

function item(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: "i1",
    kind: "paper",
    title: "A paper",
    url: "https://arxiv.org/abs/2401.00001",
    summary: null,
    authors: [],
    venue: null,
    published_at: null,
    domains: [],
    domain_labels: [],
    source_name: null,
    author_display_name: null,
    saved: false,
    extra: {},
    ...overrides,
  };
}

test("出处先说发在哪儿，再说我们从哪捞到的", () => {
  // 读者关心的是刊物，不是采集管道。两者都有时，刊物赢。
  assert.equal(
    attribution(item({ venue: "Nature Materials", source_name: "Nature Materials RSS" }), "zh"),
    "Nature Materials",
  );
  // 没有刊物才退回源名 —— 总比什么都不说好。
  assert.equal(attribution(item({ source_name: "arXiv cs.LG" }), "zh"), "arXiv cs.LG");
});

test("用户分享的条目署分享人", () => {
  const shared = item({
    kind: "post",
    author_display_name: "Mia Zhang",
    venue: "arxiv.org",
  });
  assert.match(attribution(shared, "zh"), /由 Mia Zhang 分享/);
  assert.match(attribution(shared, "zh"), /arxiv\.org/);
  // 换语言时署名句也跟着换 —— 名字本身当然不翻。
  assert.match(attribution(shared, "en"), /Shared by Mia Zhang/);
});

test("作者超过三个折成 et al.", () => {
  assert.equal(authorLine([]), "");
  assert.equal(authorLine(["A", "B", "C"]), "A, B, C");
  assert.equal(authorLine(["A", "B", "C", "D"]), "A, B, C et al.");
});

/**
 * 判据自己不许依赖运行环境：`now` 是参数，不是函数里读的时钟。
 * 读时钟的版本只在某些时刻通过，而它会绿着骗过 CI。
 */
test("相对时间按传进来的时刻算，不读真实时钟", () => {
  const now = new Date("2026-08-22T12:00:00Z");
  assert.equal(relativeTime("2026-08-22T11:59:40Z", now, "zh"), "刚刚");
  assert.equal(relativeTime("2026-08-22T11:30:00Z", now, "zh"), "30 分钟前");
  assert.equal(relativeTime("2026-08-22T06:00:00Z", now, "zh"), "6 小时前");
  assert.equal(relativeTime("2026-08-19T12:00:00Z", now, "zh"), "3 天前");
  assert.equal(relativeTime("2026-01-02T12:00:00Z", now, "zh"), "2026-01-02");
  assert.equal(relativeTime("2026-08-22T11:30:00Z", now, "en"), "30 min ago");
  // 源没给日期是常态（RSS 里 pubDate 可缺）—— 不能因此崩，也不能编一个。
  assert.equal(relativeTime(null, now, "zh"), "");
  assert.equal(relativeTime("not-a-date", now, "zh"), "");
});

test("截稿倒计时说还剩几天，过期的说已截止", () => {
  const now = new Date("2026-08-22T12:00:00Z");
  const deadline = (iso: string) =>
    deadlineCountdown(item({ kind: "deadline", extra: { deadline_at: iso } }), now, "zh");
  assert.equal(deadline("2026-08-22T20:00:00Z"), "今天截止");
  assert.equal(deadline("2026-08-23T20:00:00Z"), "明天截止");
  assert.equal(deadline("2026-09-01T12:00:00Z"), "还有 10 天");
  assert.equal(deadline("2026-08-01T12:00:00Z"), "已截止");
  // extra 里没有 deadline_at 就什么都不说，不要显示一个 NaN。
  assert.equal(deadlineCountdown(item({ kind: "deadline" }), now, "zh"), "");
});

test("摘要截断留省略号，短的原样返回", () => {
  assert.equal(truncate("short", 20), "short");
  assert.equal(truncate(null, 20), "");
  assert.equal(truncate("x".repeat(30), 10), `${"x".repeat(10)}…`);
});

/**
 * 域周报是平台自己生成的，没有外部出处。给它渲染一个死链接比不渲染更糟 ——
 * 用户点了没反应，而他不会知道是因为这条本来就没有出处。
 */
test("没有 url 的条目不该被当成可点开的外链", () => {
  assert.equal(hasExternalLink(item()), true);
  assert.equal(hasExternalLink(item({ url: null })), false);
  assert.equal(hasExternalLink(item({ kind: "digest", url: null })), false);
});

/**
 * 没有个性化依据时不许说"为你挑的"。这不是文案洁癖：一个新用户看到
 * "Top 3 of the day / 按你的课题挑的"，而那三条其实只是最新的三条，
 * 他会得出"这个推荐很烂"的结论，而不是"我还没告诉它我关心什么"。
 */
test("首页那句话必须说实话", () => {
  const personalized = todayHeadline({ personalized: true, onboarded: true, pickCount: 3, lang: "zh" });
  assert.equal(personalized.title, "为你精选");
  assert.match(personalized.subtitle, /课题/);

  const cold = todayHeadline({ personalized: false, onboarded: false, pickCount: 3, lang: "zh" });
  assert.notEqual(cold.title, "为你精选");
  assert.doesNotMatch(cold.subtitle, /为你挑/);
  assert.match(cold.subtitle, /选几个关注方向/);

  const empty = todayHeadline({ personalized: true, onboarded: true, pickCount: 0, lang: "zh" });
  assert.match(empty.subtitle, /内容池/);

  // 英文下同样不许把冷启动说成"为你挑的"—— 说实话这条规矩不随语言变。
  const coldEn = todayHeadline({ personalized: false, onboarded: false, pickCount: 3, lang: "en" });
  assert.notEqual(coldEn.title,
    todayHeadline({ personalized: true, onboarded: true, pickCount: 3, lang: "en" }).title);
});

/**
 * `cs.LG` 与 `stat.ML` 的人读名都是 "Machine Learning"。按标签渲染会在卡片上
 * 显示两个一模一样的标签，并触发 React 的重复 key（React 明确说那种情况下
 * 子节点"可能被复制或被丢弃"）。
 *
 * 355 条前端测试和 typecheck 对它全绿 —— 只有把部署起起来、在浏览器里打开
 * 一篇同属两个分类的论文才看得见。
 */
test("同名不同分类只显示一次，且 key 取 slug", () => {
  const both = item({
    domains: ["cs.LG", "stat.ML"],
    domain_labels: ["Machine Learning", "Machine Learning"],
  });
  const tags = visibleDomainTags(both, 2);
  assert.equal(tags.length, 1);
  assert.equal(tags[0].label, "Machine Learning");
  // key 必须是 slug：标签重了，slug 不会重。
  assert.equal(tags[0].domain, "cs.LG");

  const distinct = item({
    domains: ["cond-mat.mtrl-sci", "cond-mat.soft", "cs.LG"],
    domain_labels: ["Materials Science", "Soft Condensed Matter", "Machine Learning"],
  });
  assert.deepEqual(
    visibleDomainTags(distinct, 2).map((t) => t.domain),
    ["cond-mat.mtrl-sci", "cond-mat.soft"],
  );
  // slug 数量多于标签时不能崩，也不能显示 undefined。
  const ragged = item({ domains: ["cs.LG", "stat.ML"], domain_labels: [] });
  assert.deepEqual(visibleDomainTags(ragged, 2).map((t) => t.label), ["cs.LG", "stat.ML"]);
});

/**
 * 资讯卡上的三个出口。
 *
 * ⚠️ 这条判据**曾经骗过我**：上一版它断言的是 "接入课题" 这四个字出现在
 * FeedItemCard.tsx 里。字在，测试绿，而那个按钮的目的地
 * （`/projects?feed_item=…`）根本没人读那个参数 —— 点了只会跳到一个普通的
 * 课题列表，论文什么都没跟过去。
 *
 * 教训：断言"文案还在"约等于什么都没断言。所以现在收藏/忽略断言的是**它们
 * 调了哪个动作**，交接断言的是**它接到了那个真的会做事的组件**，而交接本身
 * 做得对不对由上面那两条（带出处、真会话地址、会话侧真读参数）负责。
 */
test("资讯卡保留通往研究的三个出口", () => {
  const source = readFileSync(
    new URL("../components/FeedItemCard.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /action: "save"/);
  assert.match(source, /action: "dismiss"/);
  assert.match(source, /<HandoffAction\b/);
  // 外链一律新窗口打开，且必须带 noopener —— 少了它，被打开的页面能通过
  // window.opener 操纵我们这一页。
  assert.match(source, /rel="noopener noreferrer"/);

  const handoff = readFileSync(
    new URL("../components/HandoffAction.tsx", import.meta.url),
    "utf8",
  );
  assert.match(handoff, /接入课题/);
  // 它必须真的建会话再跳，而不是拼一个没人读的查询参数。
  assert.match(handoff, /createProjectSession/);
  assert.match(handoff, /handoffHref/);
  // 判据落在**代码**上，不落在注释上：那段注释正是在讲当年那个坏 URL 长什么
  // 样，而讲它不等于又写了它。（这条断言第一版就被自己的注释绊倒了。）
  assert.ok(!stripComments(handoff).includes("feed_item="), "又拼回那个没人读的参数了");
});

/** 去掉块注释与行注释 —— 判"代码里有没有"时，注释不算数。 */
function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

/**
 * 「接入课题」是这个功能和 RSS 阅读器的分界，也是我最容易把它做成幽灵路径的
 * 地方 —— 第一版就是：`<a href="/projects?feed_item=…">`，跳得走，但 Projects
 * 页根本不读那个参数，那篇论文什么都没跟过来。而当时那条"按钮文案还在"的
 * 测试一直是绿的。
 *
 * 所以这里锁三件**具体**的事：交接文本带得走出处、目的地是真会话地址、
 * 以及不替用户把问题写好。
 */
test("交接给课题时必须把出处一起带过去", async () => {
  const { handoffDraft, handoffHref, handoffSessionTitle } = await import("./handoff.ts");
  const paper = item({
    title: "Universal machine-learning molecular dynamics",
    url: "https://arxiv.org/abs/2608.19041",
    venue: "arXiv",
    authors: ["A One", "B Two", "C Three", "D Four", "E Five", "F Six"],
    summary: "We present a universal interatomic potential.",
  });
  const draft = handoffDraft(paper, "zh");
  // 只带标题的话 agent 只能凭标题猜内容 —— 那正是这个平台最不该做的事。
  assert.match(draft, /https:\/\/arxiv\.org\/abs\/2608\.19041/);
  assert.match(draft, /Universal machine-learning molecular dynamics/);
  assert.match(draft, /universal interatomic potential/);
  assert.match(draft, /et al\./);
  // 末尾不替用户写问题：他要问什么只有他知道。
  assert.match(draft, /\n\n$/);

  // 目的地必须是一个真的会话地址，不是一个没人读的查询参数。
  const href = handoffHref("p 1", "s/2", draft);
  assert.match(href, /^\/projects\/p%201\/sessions\/s%2F2\?draft=/);
  assert.ok(!href.includes("feed_item="));

  assert.equal(handoffSessionTitle(paper), paper.title);
  assert.equal(handoffSessionTitle(item({ title: "x".repeat(80) })).length, 61);
});

/**
 * 交接的另一半在 SessionWorkspace：它得真的去读 `?draft=`。两边只要有一边
 * 没接上，这条路就又是空的 —— 而两边各自都"没报错"。
 */
test("会话侧真的读了 draft 参数", () => {
  const workspace = readFileSync(
    new URL("../../sessions/components/SessionWorkspace.tsx", import.meta.url),
    "utf8",
  );
  assert.match(workspace, /useSeededDraft/);
  assert.match(workspace, /get\("draft"\)/);
  // ⚠️ 必须走 `useSearchParams`，不能读 `window.location`。
  //
  // 读 window.location 的那版在**刷新页面时是对的**，从资讯流点过来（客户端
  // 跳转）时永远拿到空 —— App Router 跳转中先渲染新页面，那一刻地址栏还停在
  // 上一个页面。而"从资讯流点过来"恰恰是这个功能唯一的真实用法。
  assert.match(workspace, /useSearchParams\(\)/);
  assert.ok(
    !/window\.location\.search/.test(stripComments(workspace)),
    "又回去读 window.location 了 —— 客户端跳转时它还是上一页的地址",
  );
});

/**
 * 开关的状态字是**会说错话**的地方。
 *
 * 最危险的一句：开着、但因为没配模型而根本不会工作，如果显示成"已开启"，
 * 用户会一直等一个永远不来的结果。所以 available 和 enabled 必须分别说。
 */
test("自动挖掘开关不许把「没配模型」说成「已开启」", async () => {
  const { curationStatusLine } = await import("./curation-presentation.ts");
  const base = {
    enabled: false, available: true, model_label: "x · y",
    last_run_at: null, last_error: null,
    inferred_domains: [], inferred_domain_labels: [], inferred_queries: [],
  };

  // 平台没配模型：不管用户开没开，都要说清是**平台**缺东西、以及去哪配。
  const unavailable = curationStatusLine({ ...base, available: false, enabled: true }, "zh");
  assert.equal(unavailable.tone, "error");
  assert.match(unavailable.text, /不可用/);
  assert.match(unavailable.text, /设置/);

  // 开着但上次失败：必须说出原因，否则和"确实没什么可推断的"长得一样。
  const failed = curationStatusLine({ ...base, enabled: true, last_error: "模型超时" }, "zh");
  assert.equal(failed.tone, "error");
  assert.match(failed.text, /模型超时/);

  // 开着且推断出了东西：把方向名说出来，用户能当场判断准不准。
  const ok = curationStatusLine({
    ...base, enabled: true, last_run_at: "2026-08-22T00:00:00Z",
    inferred_domains: ["cs.LG"], inferred_domain_labels: ["Machine Learning"],
  }, "zh");
  assert.equal(ok.tone, "ok");
  assert.match(ok.text, /Machine Learning/);

  // 跑过、没报错、也没结果：说实话并给下一步，不要假装在忙。
  const empty = curationStatusLine({ ...base, enabled: true, last_run_at: "2026-08-22T00:00:00Z" }, "zh");
  assert.doesNotMatch(empty.text, /正在第一次/);
  assert.match(empty.text, /课题描述/);

  // 还没跑过：这时候说"正在挖掘"才是对的。
  const first = curationStatusLine({ ...base, enabled: true }, "zh");
  assert.match(first.text, /第一次/);

  assert.match(curationStatusLine(base, "zh").text, /当前关闭/);
});

/**
 * 「用哪个大模型」在这个平台上只有一个真相源：模型角色体系。开关里再放一个
 * 选择器就是第二处答案，而两处不一致时**两边都不报错** —— 只是运行时用的和
 * 界面显示的不是同一个模型。
 */
test("开关只显示挖掘模型，不在这里改", () => {
  const source = readFileSync(
    new URL("../components/CurationSwitch.tsx", import.meta.url),
    "utf8",
  );
  // 指向唯一那处去改。
  assert.match(source, /\/settings\/models/);
  // 这里不许出现选择模型的控件，也不许自己调改模型的接口。
  assert.ok(!/<select/.test(source), "开关里不该有模型选择器");
  assert.ok(!/setModelBackend|saveModelBackend|updateSession/.test(source));
});

/**
 * 排布切换与图片。
 *
 * 默认必须是**带图的**那种：图片是这个功能明确要有的东西，藏在开关后面等于
 * 没做 —— 用户不会为了看图去找开关，他只会觉得这里没有图。
 */
test("排布默认带图，且能记住选择", async () => {
  const m = await import("./view-mode.ts");
  assert.equal(m.DEFAULT_VIEW_MODE, "grid");
  // 没存过、存了脏值、以及 storage 直接抛（隐私模式）时，都退到默认。
  assert.equal(m.readStoredViewMode(null), "grid");
  assert.equal(m.readStoredViewMode({ getItem: () => null }), "grid");
  assert.equal(m.readStoredViewMode({ getItem: () => "nonsense" }), "grid");
  assert.equal(m.readStoredViewMode({ getItem: () => { throw new Error("blocked"); } }), "grid");
  // 存过的合法值要被采纳 —— 否则"记住选择"是句空话。
  assert.equal(m.readStoredViewMode({ getItem: () => "list" }), "list");
});

/**
 * 格子视图里**每一条**都要去取图，不看条目上有没有"我有图"的标志位。
 *
 * 早先出参里有个 `has_image`，前端据它决定要不要发请求。后端改成三级供给
 * （feed 自带图 → PDF 里裁的插图 → 生成封面）之后那个字段恒为真，
 * 于是它不再是事实、只是一句永远成立的话 —— 留着它只会让下一个人以为
 * "这条没图"是一种真实状态，并据此写出成片的灰底占位。
 *
 * 判据钉在**呈现层不再问这个问题**上：卡片不许再引用任何这类标志位。
 */
test("格子视图对每一条都取图，不再问条目有没有图", () => {
  const card = stripComments(readFileSync(
    new URL("../components/FeedItemCard.tsx", import.meta.url),
    "utf8",
  ));
  assert.ok(!/has_image|hasImage/.test(card), "卡片又开始按标志位决定取不取图了");
  // 取图这件事本身还得在（否则上面那条断言在一个没有图的卡片上也成立）。
  assert.match(card, /useFeedImage\(/);
});

/**
 * 图必须**由后端代取**，前端连出版商的外链都不该拿到。
 *
 * 直接渲染外链等于每次打开首页就向出版商广播一次「这个用户在读这条」
 * （IP + Referer）。平台在别处已经定过同一件事（PR#642 摆图进对话）。
 */
test("配图走后端代理，前端拿不到出版商外链", () => {
  const card = readFileSync(
    new URL("../components/FeedItemCard.tsx", import.meta.url),
    "utf8",
  );
  const hook = readFileSync(
    new URL("../hooks/useFeedImage.ts", import.meta.url),
    "utf8",
  );
  // 出参里根本没有 image_url —— 前端拿不到出版商外链，只有 item id。
  assert.ok(!stripComments(card).includes("image_url"));
  // 代理端点要鉴权，`<img>` 带不了 Authorization 头，所以必须自己取。
  assert.match(hook, /fetchWithAuth/);
  assert.match(hook, /createObjectURL/);
  // 取完要释放，否则翻几页攒下几十个 blob。
  assert.match(hook, /revokeObjectURL/);
  // ⚠️ 令牌不进 URL：URL 会进 access log、Referer、浏览器历史。
  assert.ok(!/token=/.test(stripComments(hook)));
  // ⚠️ 不许加回 loading="lazy"：图是 fetch 取回来的，blob 到手时网络请求
  // 已经发生过，惰性加载省不下东西，反而让图停在 complete:false 不显示。
  assert.ok(!/loading=["']lazy["']/.test(stripComments(card)), "又加回 lazy 了");
});

/**
 * 每一句文案两种语言都得有。
 *
 * 判据扫的是**整张表**，不是几个具体的 key —— 写名单的话，下一个人加的
 * 那句默认漏过（本仓库既有纪律：护栏要扫盘，不要写名单）。缺英文的症状是
 * 英文界面里突然冒出一句中文；缺中文的症状是反过来。两种都不该到用户眼前。
 */
test("资讯流每一句文案都有中英两版", async () => {
  const { FEED_COPY } = await import("./feed-copy.ts");
  const hasCJK = (text: string) => /[一-鿿]/.test(text);

  const entries = Object.entries(FEED_COPY) as Array<[string, { zh: string; en: string }]>;
  assert.ok(entries.length > 40, "这张表小得可疑，是不是被谁清了");

  for (const [key, phrase] of entries) {
    assert.ok(phrase.zh?.trim(), `${key} 缺中文`);
    assert.ok(phrase.en?.trim(), `${key} 缺英文`);
    // 英文那栏里混着中文，等于这一句实际上没翻 —— 而它会照样通过
    // 「两栏都非空」的检查。
    assert.ok(!hasCJK(phrase.en), `${key} 的英文里还有中文：${phrase.en}`);
  }
});

/**
 * 换语言必须**真的换掉输出的字**，不是只把设置存下来。
 *
 * "存得进、读得出、但没人按它做事"是这个仓库反复栽过的形状。所以判据落在
 * 几个真实呈现函数的返回值上，而不是落在"有没有 language 这个字段"上。
 */
test("换语言换掉的是呈现函数返回的字", async () => {
  const { kindLabel, todayHeadline } = await import("./feed-presentation.ts");
  const { curationStatusLine } = await import("./curation-presentation.ts");
  const hasCJK = (text: string) => /[一-鿿]/.test(text);

  assert.ok(hasCJK(kindLabel("paper", "zh")));
  assert.ok(!hasCJK(kindLabel("paper", "en")), "选了英文，徽章还是中文");

  for (const params of [
    { personalized: true, onboarded: true, pickCount: 3 },
    { personalized: false, onboarded: false, pickCount: 3 },
    { personalized: true, onboarded: true, pickCount: 0 },
  ] as const) {
    const zh = todayHeadline({ ...params, lang: "zh" });
    const en = todayHeadline({ ...params, lang: "en" });
    assert.ok(hasCJK(zh.title) && hasCJK(zh.subtitle), `中文版还是英文：${zh.title}`);
    assert.ok(!hasCJK(en.title), `英文版标题里有中文：${en.title}`);
    assert.ok(!hasCJK(en.subtitle), `英文版副标题里有中文：${en.subtitle}`);
  }

  // 状态行里那个列表分隔符也是文案：中文用顿号，英文用逗号。
  const state = {
    enabled: true, available: true, model_label: "x · y",
    last_run_at: "2026-08-22T00:00:00Z", last_error: null,
    inferred_domains: ["cs.LG", "cond-mat"],
    inferred_domain_labels: ["Machine Learning", "Condensed Matter"],
    inferred_queries: [],
  };
  assert.match(curationStatusLine(state, "zh").text, /Machine Learning、Condensed Matter/);
  assert.match(curationStatusLine(state, "en").text, /Machine Learning, Condensed Matter/);
});

/**
 * 详情窗：卡上的摘要是截断的，而"这条到底讲什么"往往就差被截掉的那半段。
 * 所以详情里的摘要**不许截断** —— 否则打开它和不打开没区别。
 */
test("详情窗给完整摘要，且能用键盘关掉", () => {
  const dialog = readFileSync(
    new URL("../components/FeedItemDialog.tsx", import.meta.url),
    "utf8",
  );
  const code = stripComments(dialog);
  // 摘要直接渲染，不经 truncate。有译文时显示译文（`summaryZh || item.summary`）：
  // 与学术搜索结果卡片同一处理，但**详情里必须完整**，所以仍然不许出现 truncate。
  assert.match(code, /\{summaryZh \|\| item\.summary\}/);
  assert.ok(!/truncate\(item\.summary/.test(code), "详情里的摘要被截断了");
  // Esc 能关 —— 只能鼠标点叉的浮层，键盘用户出不去。
  assert.match(code, /Escape/);
  // 背景滚动要锁：不锁的话滚轮滚的是背后的列表，而用户以为在滚详情。
  assert.match(code, /body\.style\.overflow/);
  assert.match(code, /aria-modal/);
});

/**
 * 整张卡可点开详情，但**卡上的按钮不算** —— 点"收藏"不该顺手弹出详情。
 * 用 `closest` 扫盘而不是逐个 stopPropagation：后者要求每加一个控件都记得
 * 加一次，漏一个就是一次意外弹窗。
 */
test("点卡片开详情，点卡上的按钮不开", () => {
  const card = readFileSync(
    new URL("../components/FeedItemCard.tsx", import.meta.url),
    "utf8",
  );
  const code = stripComments(card);
  assert.match(code, /closest\("button, a, input, select, textarea"\)/);
  // 键盘也要能开，否则这条路径对键盘用户不存在。
  assert.match(code, /onKeyDown/);
  assert.match(code, /role=\{onOpenDetail \? "button" : undefined\}/);
});
