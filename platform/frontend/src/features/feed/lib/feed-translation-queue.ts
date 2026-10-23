import type { LiteratureTranslatePaperInput, LiteratureTranslateResponse, LiteratureTranslation } from "@/lib/api";

export const FEED_TRANSLATION_BATCH_SIZE = 3;
export const needsTranslation = (text: string | null | undefined): boolean =>
  Boolean(text && /[A-Za-z]/.test(text) && !/[\u4e00-\u9fff]/.test(text));

/** One bounded worker keeps earlier page results alive when scrolling appends cards. */
export class FeedTranslationQueue {
  private pending: LiteratureTranslatePaperInput[] = [];
  private seen = new Set<string>();
  private values = new Map<string, LiteratureTranslation>();
  private worker: Promise<void> | null = null;
  private disposed = false;

  private translate: (papers: LiteratureTranslatePaperInput[]) => Promise<LiteratureTranslateResponse>;
  private publish: (translations: LiteratureTranslation[]) => void;
  private retryDelayMs: number;
  private requiredFields: ReadonlyArray<"title" | "abstract">;

  constructor(
    translate: (papers: LiteratureTranslatePaperInput[]) => Promise<LiteratureTranslateResponse>,
    publish: (translations: LiteratureTranslation[]) => void,
    retryDelayMs = 2000,
    requiredFields: ReadonlyArray<"title" | "abstract"> = ["title", "abstract"],
  ) {
    this.translate = translate;
    this.publish = publish;
    this.retryDelayMs = retryDelayMs;
    this.requiredFields = requiredFields;
  }

  enqueue(papers: LiteratureTranslatePaperInput[]): Promise<void> {
    if (this.disposed) return Promise.resolve();
    for (const paper of papers) {
      if (this.seen.has(paper.key) || !(needsTranslation(paper.title) || needsTranslation(paper.abstract))) continue;
      this.seen.add(paper.key);
      this.pending.push(paper);
    }
    if (!this.worker) {
      this.worker = this.run().finally(() => { this.worker = null; });
    }
    return this.worker;
  }

  dispose(): void {
    this.disposed = true;
    this.pending = [];
  }

  private complete(paper: LiteratureTranslatePaperInput): boolean {
    const value = this.values.get(paper.key);
    return this.requiredFields.every((field) => {
      const source = field === "title" ? paper.title : paper.abstract;
      const translated = field === "title" ? value?.title_zh : value?.abstract_zh;
      return !needsTranslation(source) || Boolean(translated);
    });
  }

  private async run(): Promise<void> {
    while (!this.disposed && this.pending.length) {
      let missing = this.pending.splice(0, FEED_TRANSLATION_BATCH_SIZE);
      for (let attempt = 0; attempt < 2 && missing.length && !this.disposed; attempt++) {
        try {
          const response = await this.translate(missing);
          if (this.disposed) return;
          const expected = new Set(missing.map((paper) => paper.key));
          const updates: LiteratureTranslation[] = [];
          for (const value of response.translations) {
            if (!expected.has(value.key)) continue;
            const prior = this.values.get(value.key);
            const merged = {
              key: value.key,
              title_zh: value.title_zh || prior?.title_zh || null,
              abstract_zh: value.abstract_zh || prior?.abstract_zh || null,
              ai_summary: value.ai_summary || prior?.ai_summary || null,
            };
            this.values.set(value.key, merged);
            if (merged.title_zh || merged.abstract_zh) updates.push(merged);
          }
          if (updates.length) this.publish(updates);
        } catch {
          // A failed batch gets one retry; later pages continue even if it fails.
        }
        missing = missing.filter((paper) => !this.complete(paper));
        if (attempt === 0 && missing.length && !this.disposed) {
          await new Promise((resolve) => setTimeout(resolve, this.retryDelayMs));
        }
      }
    }
  }
}
