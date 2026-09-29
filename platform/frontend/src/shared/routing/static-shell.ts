/**
 * 静态导出的占位参数。
 *
 * 个人档要把 UI 装进安装包：构建期用 Node，运行期只剩 Python。`output: "export"`
 * 因此是必须的，而它要求每个动态段在**构建时**就报出自己的取值 —— 可项目 id 与
 * 会话 id 是运行时才存在的东西，构建时一个都不知道。
 *
 * 所以每个动态段只导出一个占位值，产出一份外壳 HTML；App Server 把
 * `/projects/<真 id>/...` 映射到对应的外壳，页面在浏览器里用 `useParams()` 从
 * **地址栏**读真 id。关键在于：动态段上的页面一律是客户端组件，不许在服务端
 * `await params` —— 那样读到的是占位值 `_`，会被烤进 HTML，用户硬刷新后看到的
 * 就是一个不存在的项目。
 */
export const STATIC_SHELL_PARAM = "_";

/** 一个动态段的 generateStaticParams：只产出外壳那一份。 */
export function staticShellParams<K extends string>(key: K): Array<Record<K, string>> {
  return [{ [key]: STATIC_SHELL_PARAM } as Record<K, string>];
}
