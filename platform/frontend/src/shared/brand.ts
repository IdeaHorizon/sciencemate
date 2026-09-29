/**
 * 产品名 —— 全前端唯一出处。
 *
 * 2026-09-08 wangd 定名 **ScienceMate**（此前依次是「IEIT Research Platform」→
 * 「Agent for Science 科研平台」）。名字就是名字：中文副标题属于版面，不进这个常量。
 * 之前这个名字在 layout / 登录页 / 侧栏 / 折叠侧栏 CSS 各写了一份，改名要找四处；
 * 现在文案从这里取，折叠侧栏的缩写通过 data-mark 交给 CSS `attr()`，CSS 里不再写死。
 * brand.test.ts 扫盘：src 里除本文件外不许出现产品名字面量。
 */
export const PRODUCT_NAME = "ScienceMate";

/** 折叠侧栏里的短标（宽 30px 只放得下三四个字符）。 */
export const PRODUCT_MARK = "SM";
