// Executed in an isolated world on a CNKI tab. No cookies are read or returned.
// DOM selectors were checked against CNKI on 2026-10-21.
export async function cnkiPage(action, args = {}) {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = element => {
    if (!element) return false;
    const r = element.getBoundingClientRect(), s = getComputedStyle(element);
    return s.display !== "none" && s.visibility !== "hidden" && r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight;
  };
  const challenged = () => /\/verify\//.test(location.pathname) || visible(document.querySelector("#tcaptcha_transform_dy"));
  const guard = () => { if (challenged()) throw new Error("waiting_for_verification"); };
  const rows = () => Array.from(document.querySelectorAll(".result-table-list tbody tr")).flatMap(row => {
    const link = row.querySelector("td.name a.fz14");
    if (!link?.innerText?.trim()) return [];
    const date = row.querySelector("td.date")?.innerText?.trim() || "";
    return [{title: link.innerText.trim(), url: link.href,
      exportId: row.querySelector("input.cbItem")?.value || "",
      authors: Array.from(row.querySelectorAll("td.author a.KnowledgeNetLink")).map(a => a.innerText.trim()),
      venue: row.querySelector("td.source")?.innerText?.trim() || "", pub_date: date,
      year: /^\d{4}/.test(date) ? Number(date.slice(0, 4)) : null,
      document_type: row.querySelector("td.data")?.innerText?.trim() || "期刊",
      citations: Number((row.querySelector("td.quote")?.innerText || "").replace(/\D/g, "")) || 0}];
  });
  const snapshot = () => ({captcha: challenged(), query: document.querySelector("input.search-input")?.value || "",
    emptyConfirmed: /未检索到|没有找到|共找到\s*0\s*条/.test(document.body?.innerText || ""),
    field: document.querySelector(".search-box li.cur[data-val]")?.getAttribute("data-val"),
    dateSorted: !!document.querySelector("#orderList li#PT.cur"),
    page: Number(document.querySelector(".countPageMark")?.innerText?.split("/")[0]) || 1,
    total: Number((document.querySelector(".pagerTitleCell")?.innerText?.match(/[\d,]+/)?.[0] || "0").replaceAll(",", "")),
    hasNext: Array.from(document.querySelectorAll(".pages a")).some(a => a.innerText.trim() === "下一页"),
    rows: rows()});
  const wait = async predicate => {
    for (let n = 0; n < 100; n++) {guard(); if (predicate()) return; await sleep(150);}
    throw new Error("cnki_timeout");
  };
  const fingerprint = () => rows().map(r => r.title).join("\n");
  const updateResults = async click => {
    const before = fingerprint(), beforeRow = document.querySelector(".result-table-list tbody tr"),
      beforeEmpty = snapshot().emptyConfirmed;
    let received = false, failure = "";
    const original = XMLHttpRequest.prototype.open;
    const observe = function(method, url, ...rest) {
      if (/\/brief\//i.test(String(url))) this.addEventListener("loadend", () => {
        received = this.status >= 200 && this.status < 300;
        if (this.status === 403) failure = "access_denied";
        if (this.status === 429) failure = "rate_limited";
      }, {once:true});
      return original.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.open = observe;
    try {
      click();
      await wait(() => {
        if (failure) throw new Error(failure);
        const current = snapshot();
        if (received && !current.rows.length && /暂无数据[，,]?\s*请稍后重试/.test(document.body.innerText)) {
          throw new Error("results_unavailable");
        }
        // A new query may legitimately return exactly the same titles or zero
        // again. Observe replaced DOM nodes / response completion, not only text.
        return current.rows.length ? (fingerprint() !== before || received ||
          document.querySelector(".result-table-list tbody tr") !== beforeRow)
          : current.emptyConfirmed && (!beforeEmpty || received);
      });
    } finally {
      if (XMLHttpRequest.prototype.open === observe) XMLHttpRequest.prototype.open = original;
    }
  };
  try {
    if (action === "state") return snapshot();
    guard();
    if (action === "search") {
      await wait(() => document.querySelector("input.search-input"));
      const field = document.querySelector(`.search-box li[data-val="${args.field || "SU"}"]`)
        || document.querySelector(`li[data-val="${args.field || "SU"}"]`);
      if (!field) throw new Error("search_field_unavailable");
      (field.querySelector("a") || field).click();
      await wait(() => snapshot().field === (args.field || "SU"));
      const input = document.querySelector("input.search-input");
      input.value = args.query; input.dispatchEvent(new Event("input", {bubbles: true}));
      await updateResults(() => document.querySelector("input.search-btn").click());
      const sort = document.querySelector("#orderList li#PT");
      if (sort && !sort.classList.contains("cur")) {
        await updateResults(() => sort.click());
      }
      return snapshot();
    }
    if (action === "next") {
      const before = fingerprint(), page = snapshot().page;
      const next = Array.from(document.querySelectorAll(".pages a")).find(a => a.innerText.trim() === "下一页");
      if (!next) return {...snapshot(), hasNext: false};
      next.click();
      await wait(() => snapshot().page !== page && fingerprint() !== before);
      return snapshot();
    }
    if (action === "export") {
      const response = await fetch("https://kns.cnki.net/dm8/API/GetExport", {
        method: "POST", headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: new URLSearchParams({filename: args.exportId, displaymode: "GBTREFER,elearning,EndNote", uniplatform: "NZKPT"}),
        signal: AbortSignal.timeout(20000),
      });
      guard();
      if (!response.ok) throw new Error(response.status === 429 ? "rate_limited" : "metadata_unavailable");
      const text = await response.text();
      let payload; try {payload = JSON.parse(text);} catch {throw new Error(/验证/.test(text) ? "waiting_for_verification" : "metadata_unavailable");}
      if (payload.code !== 1) throw new Error("metadata_unavailable");
      const modes = Object.fromEntries(payload.data.map(item => [item.mode.toUpperCase(), item.value[0] || ""]));
      const doc = new DOMParser().parseFromString((modes.ELEARNING || "").replace(/<br\s*\/?\s*>/gi, "\n"), "text/html");
      const data = doc.body.textContent.replaceAll("\r", "");
      const value = (...keys) => {
        for (const key of keys) {const match = data.match(new RegExp(`(?:^|\\n)${key}\\s*[:：]\\s*([^\\n]*)`)); if (match) return match[1].trim();}
        return "";
      };
      const abstract = value("Summary-摘要");
      const endnote = (modes.ENDNOTE || "").replace(/<br\s*\/?\s*>/gi, "\n");
      return {title: value("Title-题名"), authors: value("Author-作者").split(/[;；]/).map(x => x.trim()).filter(Boolean),
        venue: value("Source-刊名", "Source-学位授予单位", "Source-会议名称"),
        pub_date: value("PubTime-出版时间", "PubTime-发表时间"),
        year: Number(value("Year-年")) || null, abstract,
        keywords: value("Keyword-关键词").split(/[;；]/).map(x => x.trim()).filter(Boolean),
        doi: value("DOI-DOI") || endnote.match(/(?:^|\n)%R\s+(10\.\S+)/)?.[1] || "",
        volume: value("Roll-卷"), issue: value("Period-期"), pages: value("Page-页码"),
        institutions: value("Organ-机构", "Organ-大学"), abstract_truncated: /(?:\.\.\.|…)$/.test(abstract), metadata_attempted: true};
    }
    if (action === "abstract") {
      await wait(() => document.querySelector("#ChDivSummary,.abstract-text"));
      const element = document.querySelector("#ChDivSummary,.abstract-text");
      const before = element.textContent.trim();
      if (/(?:\.\.\.|…)$/.test(before)) {
        document.querySelector("#ChDivSummaryMore")?.click();
        await wait(() => element.textContent.trim().length > before.length);
      }
      const abstract = element.textContent.trim();
      return {title: document.querySelector(".brief h1")?.innerText?.trim(), abstract, abstract_truncated: /(?:\.\.\.|…)$/.test(abstract)};
    }
    throw new Error("unknown_action");
  } catch (error) {
    return {error: error.message || "cnki_error"};
  }
}
