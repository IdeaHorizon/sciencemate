/**
 * 对面没按契约来：后端回的形状不对、游标没往前走、别人的 run 混了进来；或者一个
 * 类型上不可能出现的值真的出现了（穷尽检查 `unreachable(value: never)`）。
 *
 * 这是**一个缺陷的诊断**，不是一句说给人听的话 —— 人对它做不了任何事，
 * 能做事的是读日志、读诊断包的那个人。所以 message 只写一种语言（英文，
 * 和契约里的字段名一个口径），界面要告诉人时**按类**说一句人话
 * （`SessionExecutionView` 的 `describeApiProblem`），不照念 message。
 *
 * ## 为什么要一个基类
 *
 * 双语闸（`shared/i18n/every-visible-string-has-both-languages.test.ts`）要分清
 * 「这句英文会不会落到人眼前」。只按写法分（`throw new Error(…)` 一律放行）
 * 是错的：界面有六十来处 `pushError(error.message)` 照念 message，
 * `lib/api.ts` 里那句 "Enter a new API key to save this backend." 就是这样一路
 * 原样弹到中文界面的模型设置页上（2026-10-20）。
 *
 * 所以「这句是诊断」要在**声明处**说一次：继承它。闸只放行它的子类的
 * message；裸的 `new Error("一句英文")` 仍要两种语言都写。
 */
export class ContractViolation extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ContractViolation";
  }
}
