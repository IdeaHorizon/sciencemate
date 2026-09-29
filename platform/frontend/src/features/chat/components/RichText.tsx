"use client";

import { createElement, useId, useMemo, type ReactNode } from "react";
import type { Definition, RootContent } from "mdast";
import type { Artifact, ConceptAnnotation } from "@/lib/api";
import { InlineWorkspaceImage } from "@/features/file-preview/components/InlineWorkspaceImage";
import { useWorkspaceFiles } from "@/features/file-preview/components/WorkspaceFileOpener";
import { looksLikeImagePath, workspaceTargetOf } from "@/features/file-preview/lib/workspace-target";
import { parseMarkdown, renderFormula } from "../lib/rich-text-markdown";
import { useT, useLanguage, say } from "@/shared/i18n";

type RichTextProps = {
  text: string;
  artifacts?: Artifact[];
  onArtifactClick?: (name: string) => void;
  conceptAnnotations?: ConceptAnnotation[];
  onConceptClick?: (conceptId: string) => void;
};

function AnnotatedText({
  text,
  offset,
  artifacts,
  onArtifactClick,
  conceptAnnotations,
  onConceptClick,
}: RichTextProps & { offset: number }) {
  const annotations = conceptAnnotations
    ?.filter((annotation) => annotation.start >= offset && annotation.end <= offset + text.length)
    .sort((left, right) => left.start - right.start) ?? [];

  const renderConcepts = (segment: string, segmentOffset: number): ReactNode[] => {
    if (!annotations.length || !onConceptClick) return [segment];
    const relevant = annotations.filter((annotation) =>
      annotation.start >= segmentOffset && annotation.end <= segmentOffset + segment.length);
    if (!relevant.length) return [segment];
    const parts: ReactNode[] = [];
    let cursor = 0;
    for (const annotation of relevant) {
      const start = annotation.start - segmentOffset;
      const end = annotation.end - segmentOffset;
      if (start > cursor) parts.push(segment.slice(cursor, start));
      parts.push(
        <button
          key={`concept-${annotation.start}`}
          className="concept-link"
          title={annotation.concept_type}
          onClick={() => onConceptClick(annotation.concept_id)}
        >
          {segment.slice(start, end)}
        </button>,
      );
      cursor = end;
    }
    if (cursor < segment.length) parts.push(segment.slice(cursor));
    return parts;
  };

  const names = artifacts?.map((artifact) => artifact.name).filter(Boolean)
    .sort((left, right) => right.length - left.length) ?? [];
  if (!names.length || !onArtifactClick) return <>{renderConcepts(text, offset)}</>;
  const nameSet = new Set(names);
  const escaped = names.map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const parts = text.split(new RegExp(`(${escaped.join("|")})`, "g"));
  let cursor = 0;
  return (
    <>
      {parts.map((part, index) => {
        const partOffset = offset + cursor;
        cursor += part.length;
        return nameSet.has(part) ? (
          <button
            key={`${partOffset}-${index}`}
            className="artifact-link"
            onClick={() => onArtifactClick(part)}
          >
            {part}
          </button>
        ) : (
          <span key={`${partOffset}-${index}`}>{renderConcepts(part, partOffset)}</span>
        );
      })}
    </>
  );
}

/**
 * `![](target)` / `[](target)` 落到界面上是什么 —— **安全策略在这里落地**。
 *
 * 解析器（remark）只认形状，这里决定允不允许。三条：
 *
 * 1. **只有工作区相对路径能变成图**。外部 URL 一律不渲染成 `<img>` ——
 *    `![](https://x/p.png?d=…)` 渲染那一刻请求就发出去了，用户不需要点任何
 *    东西，而 agent 读过的外部内容里可能带着这种注入。判据见 workspaceTargetOf。
 * 2. **外部链接暂不可点**（wangd 尚未拍板）。渲染成 `文字（URL）` 纯文本：
 *    去处仍然看得见，但没有一键跳出去的面。GFM 自动识别出来的裸 URL 也走这条
 *    —— 它在 remark 里同样是 link 节点。要放开的话只改这一个分支。
 * 3. **不在会话语境里**（没有 provider，例如 fixture/demo 模式）时全部退回
 *    成纯文本 —— 与这个功能出现之前完全一致。
 */
function WorkspaceReference({
  token,
}: {
  token: { kind: "image" | "link"; text: string; target: string };
}) {
  const t = useT();
  const workspace = useWorkspaceFiles();
  const path = workspaceTargetOf(token.target);
  const label = token.text || token.target;

  if (!workspace || !path) {
    // 外部 URL / 解析不出的 target：把去处一并写出来，别把它藏进一个词里。
    const suffix = token.target && token.target !== label ? `（${token.target}）` : "";
    return <span>{label}{suffix}</span>;
  }

  if (token.kind === "image" && looksLikeImagePath(path)) {
    return (
      <InlineWorkspaceImage
        projectId={workspace.projectId}
        sessionId={workspace.sessionId}
        path={path}
        alt={token.text}
        onOpen={workspace.openFile}
      />
    );
  }

  // 链接、以及"写成图片但不是图"的（比如 `![](main.pdf)`）→ 可点的文件名，
  // 点开在右栏。PDF 塞进正文当图画不出来，但它照样值得一键打开。
  return (
    <button
      type="button"
      className="chat-run-file-open"
      onClick={() => workspace.openFile(path)}
      title={t({ zh: `在右栏打开 ${path}`, en: `Open ${path} in the side panel` })}
    >
      <code>{label}</code>
    </button>
  );
}

/**
 * 公式。这是整个渲染器里**唯一**一处 dangerouslySetInnerHTML：HTML 由 KaTeX 在
 * `trust: false` 下生成（`\href` / `\includegraphics` 这类会产出 href / img 的
 * 命令一律不执行，见 renderFormula），不是正文里的原始 HTML —— 那些走 `html`
 * 节点，按纯文本画。
 */
function Formula({ text, display = false }: { text: string; display?: boolean }) {
  const html = useMemo(() => renderFormula(text, display), [text, display]);
  return createElement(display ? "div" : "span", {
    className: display ? "message-math-block" : "message-inline-math",
    dangerouslySetInnerHTML: { __html: html },
  });
}

/** 引用式链接 `[text][id]` 的定义可以写在任何层级（引用块、列表项里都算）。 */
function collectDefinitions(nodes: RootContent[], into: Map<string, Definition>) {
  for (const node of nodes) {
    if (node.type === "definition") into.set(node.identifier.toLowerCase(), node);
    else if ("children" in node) collectDefinitions(node.children as RootContent[], into);
  }
}

function plainText(nodes: RootContent[]): string {
  return nodes
    .map((node) => ("value" in node ? node.value : "children" in node ? plainText(node.children as RootContent[]) : ""))
    .join("");
}

/**
 * 标准 Markdown（CommonMark + GFM 表格/删除线/任务清单/脚注 + `$…$` 公式）。
 *
 * 文本节点带着在原文里的偏移（mdast position），概念标注按偏移对回去，所以
 * 这里不做任何会改变偏移的预处理。
 */
export function RichText(props: RichTextProps) {
  const lang = useLanguage();
  const tree = useMemo(() => parseMarkdown(props.text), [props.text]);
  const prefix = useId();
  const definitions = new Map<string, Definition>();
  collectDefinitions(tree.children, definitions);
  const sourceOf = (node: RootContent) =>
    node.position ? props.text.slice(node.position.start.offset, node.position.end.offset) : "";

  const render = (node: RootContent, key: number | string): ReactNode => {
    const children = "children" in node
      ? (node.children as RootContent[]).map((child, index) => render(child, index))
      : null;
    switch (node.type) {
      case "text":
        return <AnnotatedText key={key} {...props} text={node.value} offset={node.position?.start.offset ?? 0} />;
      case "paragraph": return <p className="rich-text-paragraph" key={key}>{children}</p>;
      case "heading": return createElement(`h${node.depth}`, { key }, children);
      case "strong": return <strong key={key}>{children}</strong>;
      case "emphasis": return <em key={key}>{children}</em>;
      case "delete": return <del key={key}>{children}</del>;
      case "inlineCode": return <code className="message-inline-code" key={key}>{node.value}</code>;
      case "code":
        return (
          <pre className="message-code-block" key={key}>
            <code data-language={node.lang ?? undefined}>{node.value}</code>
          </pre>
        );
      case "inlineMath": return <Formula key={key} text={node.value} />;
      case "math": return <Formula key={key} text={node.value} display />;
      case "break": return <br key={key} />;
      case "thematicBreak": return <hr key={key} />;
      case "blockquote": return <blockquote key={key}>{children}</blockquote>;
      case "list":
        return node.ordered
          ? <ol key={key} start={node.start ?? undefined}>{children}</ol>
          : <ul key={key}>{children}</ul>;
      case "listItem":
        return (
          <li key={key}>
            {typeof node.checked === "boolean" && (
              <input type="checkbox" checked={node.checked} readOnly aria-label={node.checked ? say({ zh: "已完成", en: "Completed" }, lang) : say({ zh: "没完成", en: "Not completed" }, lang)} />
            )}
            {children}
          </li>
        );
      case "table": {
        const [head, ...rows] = node.children;
        const align = (column: number) => ({ textAlign: node.align?.[column] ?? undefined });
        return (
          <div className="message-table-scroll" key={key}>
            <table>
              {head && (
                <thead>
                  <tr>{head.children.map((cell, column) => <th key={column} style={align(column)}>{cell.children.map(render)}</th>)}</tr>
                </thead>
              )}
              <tbody>
                {rows.map((row, rowIndex) => (
                  <tr key={rowIndex}>
                    {row.children.map((cell, column) => <td key={column} style={align(column)}>{cell.children.map(render)}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      }
      case "image": return <WorkspaceReference key={key} token={{ kind: "image", target: node.url, text: node.alt ?? "" }} />;
      case "link": return <WorkspaceReference key={key} token={{ kind: "link", target: node.url, text: plainText(node.children) }} />;
      case "imageReference":
      case "linkReference": {
        const definition = definitions.get(node.identifier.toLowerCase());
        // 找不到定义的引用（remark 通常不会产出，防御性分支）：照原文画。
        if (!definition) return <span key={key}>{sourceOf(node)}</span>;
        return node.type === "imageReference"
          ? <WorkspaceReference key={key} token={{ kind: "image", target: definition.url, text: node.alt ?? "" }} />
          : <WorkspaceReference key={key} token={{ kind: "link", target: definition.url, text: plainText(node.children) }} />;
      }
      case "footnoteReference":
        return <sup key={key}><a href={`#${prefix}-footnote-${encodeURIComponent(node.identifier)}`}>{node.label ?? node.identifier}</a></sup>;
      case "footnoteDefinition":
        return (
          <div className="message-footnote" id={`${prefix}-footnote-${encodeURIComponent(node.identifier)}`} key={key}>
            <small>{node.label ?? node.identifier}.</small>{children}
          </div>
        );
      case "definition": return null;
      // 正文里的原始 HTML（包括从论文里抄来的）一律当文本画，不解释。
      case "html": return <span key={key}>{node.value}</span>;
      default: return children;
    }
  };

  return <div className="message-rich-text">{tree.children.map(render)}</div>;
}
