// Executed in an isolated world on a CNKI tab. No cookies are read or returned.
// DOM selectors were checked against CNKI on 2026-10-21.
export async function cnkiPage(action, args = {}) {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = element => {
    if (!element) return false;
    const r = element.getBoundingClientRect(), s = getComputedStyle(element);
    return s.display !== "none" && s.visibility !== "hidden" && r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight;
  };
  const challenged = () => /\/verify\//.test(location.pathname) ||
    Array.from(document.querySelectorAll("#tcaptcha_transform_dy,.tencent-captcha-dy__footer-title,iframe[id^='tcaptcha_iframe'],.verifybox,.verify-img-panel")).some(visible);
  const advancedInput = () => {
    const input = document.querySelector('input[data-tipid="gradetxt-1"]');
    return input && input.getBoundingClientRect().width > 0 ? input : null;
  };
  const guard = () => { if (challenged()) throw new Error("waiting_for_verification"); };
  let releaseActivation = () => {}, activationMissed = false;
  const activate = element => {
    guard();
    if (!element) throw new Error("search_control_unavailable");
    if (args.prepareOnly) {
      element.scrollIntoView({block: "center", inline: "nearest", behavior: "instant"});
      const box = element.getBoundingClientRect();
      const x = box.x + box.width / 2, y = box.y + box.height / 2;
      const target = document.elementFromPoint(x, y);
      if (!visible(element) || !target || !(target === element || element.contains(target))) {
        throw new Error("browser_activation_failed");
      }
      // The site's delayed scroll/layout updates can move this control after
      // its coordinates reach CDP. Reject a displaced native click before it
      // can hit a download/login link. Never retry an already accepted click.
      const types = ["pointerdown", "mousedown", "pointerup", "mouseup", "click"];
      const intercept = event => {
        if (!activationMissed && !challenged() && element.isConnected !== false
            && (event.target === element || element.contains(event.target))) {
          if (event.type === "click") releaseActivation();
          return;
        }
        activationMissed = true;
        event.preventDefault();
        event.stopImmediatePropagation();
      };
      releaseActivation = () => types.forEach(type => document.removeEventListener(type, intercept, true));
      types.forEach(type => document.addEventListener(type, intercept, true));
      return {x, y};
    } else {
      element.click();
    }
  };
  const rows = () => Array.from(document.querySelectorAll(".result-table-list tbody tr")).flatMap(row => {
    const link = row.querySelector("td.name a.fz14");
    if (!link?.innerText?.trim()) return [];
    const date = row.querySelector("td.date")?.innerText?.trim() || "";
    return [{title: link.innerText.trim(), url: link.href,
      exportId: row.querySelector("input.cbItem")?.value || "",
      authors: Array.from(row.querySelectorAll("td.author a.KnowledgeNetLink")).map(a => a.innerText.trim()),
      venue: row.querySelector("td.source")?.innerText?.trim() || "", pub_date: date,
      year: /^\d{4}/.test(date) ? Number(date.slice(0, 4)) : null,
      // Filtered result tables may omit the redundant type column (e.g. books).
      // Use the selected collection as evidence, never default every row to a journal.
      document_type: row.querySelector("td.data")?.innerText?.trim() ||
        ({JOURNAL:"期刊",DISSERTATION:"学位论文",CONFERENCE:"会议",BOOK:"图书"}[selectedResource()] || "其他"),
      citations: Number((row.querySelector("td.quote")?.innerText || "").replace(/\D/g, "")) || 0}];
  });
  const selectedResource = () => Array.from(document.querySelectorAll('a[name="classify"][resource]'))
    .find(e => e.getAttribute("classid") === document.querySelector("#classid")?.value)?.getAttribute("resource")
    || document.querySelector("#resource")?.value || "CROSSDB";
  const snapshot = () => ({captcha: challenged(), query: document.querySelector("input.search-input")?.value || document.querySelector('input[data-tipid="gradetxt-1"]')?.value || "",
    resource: selectedResource(),
    emptyConfirmed: /未检索到|没有找到|共找到\s*0\s*条/.test(document.body?.innerText || ""),
    field: document.querySelector('#gradetxt .input-box .sort-default span[data-value]')?.getAttribute("data-value") || document.querySelector(".search-box li.cur[data-val]")?.getAttribute("data-val"),
    dateSorted: !!document.querySelector("#orderList li#PT.cur"),
    page: Number(document.querySelector(".countPageMark")?.innerText?.split("/")[0]) || 1,
    total: Number((document.querySelector(".pagerTitleCell")?.innerText?.match(/[\d,]+/)?.[0] || "0").replaceAll(",", "")),
    hasNext: Array.from(document.querySelectorAll(".pages a")).some(a => a.innerText.trim() === "下一页"),
    rows: rows()});
  const wait = async predicate => {
    const deadline = Date.now() + 15000;
    do {guard(); if (predicate()) return; await sleep(150);} while (Date.now() < deadline);
    throw new Error("cnki_timeout");
  };
  const fingerprint = () => rows().map(r => r.title).join("\n");
  const updateResults = async (click, expectedPage = null, expectedResource = null) => {
    globalThis.__sciencemateCnkiPending?.cancel();
    const before = fingerprint(), beforeRow = document.querySelector(".result-table-list tbody tr"),
      beforeEmpty = snapshot().emptyConfirmed;
    let received = false, failure = "";
    const original = XMLHttpRequest.prototype.open;
    const observe = function(method, url, ...rest) {
      if (/\/brief\//i.test(String(url))) this.addEventListener("loadend", () => {
        received = this.status >= 200 && this.status < 300;
        if (this.status === 403) failure = "access_denied";
        // CNKI's own search handler opens its human-verification dialog on
        // 417. Pause immediately, even before the dialog finishes rendering.
        if (this.status === 417) failure = "waiting_for_verification";
        if (this.status === 429) failure = "rate_limited";
        if (this.status >= 500) failure = "results_unavailable";
        if (this.status >= 400 && !failure) failure = `search_http_${this.status}`;
      }, {once:true});
      return original.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.open = observe;
    const cancel = () => {
      releaseActivation();
      if (XMLHttpRequest.prototype.open === observe) XMLHttpRequest.prototype.open = original;
      delete globalThis.__sciencemateCnkiPending;
    };
    const finish = async () => {
      try {await wait(() => {
        if (activationMissed) throw new Error("browser_activation_moved");
        if (failure) throw new Error(failure);
        const current = snapshot();
        if (expectedResource !== null && current.resource !== expectedResource) return false;
        if (received && !current.rows.length && /暂无数据[，,]?\s*请(?:稍后重试|重新检索)/.test(document.body.innerText)) {
          throw new Error("results_unavailable");
        }
        // A new query may legitimately return exactly the same titles or zero
        // again. Observe replaced DOM nodes / response completion, not only text.
        if (expectedPage !== null) {
          return current.page === expectedPage && current.rows.length > 0 &&
            (fingerprint() !== before || document.querySelector(".result-table-list tbody tr") !== beforeRow);
        }
        return current.rows.length ? (fingerprint() !== before || received ||
          document.querySelector(".result-table-list tbody tr") !== beforeRow)
          : current.emptyConfirmed && (!beforeEmpty || received);
      }); return snapshot();} finally {cancel();}
    };
    try {
      const control = click();
      if (args.prepareOnly) {
        globalThis.__sciencemateCnkiPending = {finish, cancel};
        return {activation: control};
      }
      return await finish();
    } catch (error) {
      cancel();
      throw error;
    }
  };
  try {
    if (action === "state") return snapshot();
    if (action === "cancel") {globalThis.__sciencemateCnkiPending?.cancel(); return {cancelled: true};}
    if (action === "wait") {
      if (!globalThis.__sciencemateCnkiPending) throw new Error("browser_activation_failed");
      return await globalThis.__sciencemateCnkiPending.finish();
    }
    guard();
    if (action === "search") {
      if (!advancedInput() && document.querySelector('input[data-tipid="gradetxt-1"]') && document.querySelector(".btn-unfold")) {
        document.querySelector(".btn-unfold").click();
        await wait(() => advancedInput());
      }
      await wait(() => advancedInput() || document.querySelector("input.search-input"));
      const advanced = advancedInput();
      let result;
      if (advanced) {
        // AdvSearch also contains a hidden legacy result-search widget. Only
        // the active advanced form submits a new advanced query.
        const box = advanced.closest(".input-box");
        await wait(() => box.querySelector(`li[data-val="${args.field || "SU"}"]`));
        const field = box.querySelector(`li[data-val="${args.field || "SU"}"]`);
        if (!field) throw new Error("search_field_unavailable");
        if (box.querySelector(".sort-default span[data-value]")?.getAttribute("data-value") !== (args.field || "SU")) (field.querySelector("a") || field).click();
        await wait(() => box.querySelector(".sort-default span[data-value]")?.getAttribute("data-value") === (args.field || "SU"));
        // Do not accidentally retain an author/source constraint from an older
        // query in the same private collection tab.
        for (const input of document.querySelectorAll('input[data-tipid^="gradetxt-"]')) {
          input.value = input === advanced ? args.query : "";
          input.dispatchEvent(new Event("input", {bubbles:true}));
        }
        result = await updateResults(() => activate(document.querySelector("input.btn-search")));
      } else {
        await wait(() => document.querySelector(`.search-box li[data-val="${args.field || "SU"}"]`)
          || document.querySelector(`li[data-val="${args.field || "SU"}"]`));
        const field = document.querySelector(`.search-box li[data-val="${args.field || "SU"}"]`)
          || document.querySelector(`li[data-val="${args.field || "SU"}"]`);
        if (!field) throw new Error("search_field_unavailable");
        if (snapshot().field !== (args.field || "SU")) (field.querySelector("a") || field).click();
        await wait(() => snapshot().field === (args.field || "SU"));
        const input = document.querySelector("input.search-input");
        input.value = args.query; input.dispatchEvent(new Event("input", {bubbles: true}));
        result = await updateResults(() => activate(document.querySelector("input.search-btn")));
      }
      if (args.prepareOnly) return result;
      const sort = document.querySelector("#orderList li#PT");
      if (sort && !sort.classList.contains("cur")) {
        await updateResults(() => activate(sort));
      }
      return snapshot();
    }
    if (action === "sort") {
      const sort = document.querySelector("#orderList li#PT");
      if (!sort || sort.classList.contains("cur")) return snapshot();
      return await updateResults(() => activate(sort));
    }
    if (action === "resource") {
      if (!["CROSSDB", "JOURNAL", "DISSERTATION", "CONFERENCE", "BOOK"].includes(args.resource)) throw new Error("invalid_resource");
      if (snapshot().resource === args.resource) return snapshot();
      // Selecting an already selected search field can leave its menu open
      // over the journal tab. Close it through the site's own toggle.
      for (const menu of document.querySelectorAll(".search-box .sort-list")) {
        if (visible(menu)) menu.closest(".sort")?.querySelector(".sort-default")?.click();
      }
      const control = Array.from(document.querySelectorAll('a[name="classify"][resource]'))
        .find(e => e.getAttribute("resource") === args.resource && !e.hasAttribute("prcode") && visible(e));
      if (!control) throw new Error("resource_control_unavailable");
      return await updateResults(() => activate(control), null, args.resource);
    }
    if (action === "next") {
      const before = fingerprint(), page = snapshot().page;
      const next = Array.from(document.querySelectorAll(".pages a")).find(a => a.innerText.trim() === "下一页");
      if (!next) return {...snapshot(), hasNext: false};
      const result = await updateResults(() => activate(next), page + 1);
      if (args.prepareOnly) return result;
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
      if (!response.ok) return {error: response.status === 403 ? "access_denied" :
        response.status === 417 ? "waiting_for_verification" :
        response.status === 429 ? "rate_limited" : "metadata_unavailable",
        httpStatus:response.status, endpoint:"/dm8/API/GetExport"};
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
