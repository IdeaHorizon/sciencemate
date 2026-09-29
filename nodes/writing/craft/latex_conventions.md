# LaTeX 写法约定（正文片段）

你写的是正文片段，不是完整文档。渲染器负责文档类、导言区、标题块、摘要环境、参考文献。

- 只用：\section \subsection \subsubsection \label \ref \cite \emph \textbf \texttt \begin{itemize}/\begin{enumerate}、table/tabular（booktabs）、figure/includegraphics、数学环境。
- 不写：\documentclass \usepackage \begin{document} \maketitle \title \author \bibliography \printbibliography \section{参考文献}。
- 中文标点用全角；英文专名与数字前后不加空格问题由 ctex 处理，不要手动加空格。
- 下划线出现在正文里要写 \_，或用 \texttt{}。
- 百分号写 \%。
- 数学变量与公式一律用 LaTeX 数学模式（$T_A$、$\Delta t$），不要粘贴 Unicode 数学字母（𝑇、𝐴、𝛥），字体里没有会变成乱码。
- 引用 key 只能来自给你的参考文献 key 列表；图文件名只能来自给你的图清单；\ref 的 label 只能引用你自己在本片段或已起草片段里定义过的。
- 章节交叉引用一律写「第~\ref{sec:xxx}~节」，不要手写「第 7 节」这种数字（章节号由渲染器决定，手写的会错）。每个 \section / \subsection 给一个 \label{sec:...}。
- 结论要逐条对应引言里编号的研究问题：引言列了几个问题，结论就回答几条，编号一致。
- 表格：\begin{table}[htbp]\centering\caption{...}\label{tab:xxx}\begin{tabular}{...}\toprule ... \midrule ... \bottomrule\end{tabular}\end{table}。列数与每行单元数一致。
- 表宽不许超出版心（约 16 cm）：列数不超过 6；数值列保留两到三位有效数字；长文字列用 p{4cm} 让它换行；列数多或单元格长时整张表用 \small，仍然超宽就拆成两张表。表头单位写在表头里，不写在每个单元格。
- 图：\begin{figure}[htbp]\centering\includegraphics[width=0.9\linewidth]{figures/<file>}\caption{...}\label{fig:xxx}\end{figure}。
- 不输出解释性的话，只输出 LaTeX 片段。
